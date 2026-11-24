pub mod auth;
pub mod client;
pub mod count_tokens;
pub mod translate;

use async_trait::async_trait;
use axum::Json;
use axum::response::{IntoResponse, Response};
use http::StatusCode;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::anthropic::error::json_error;
use crate::anthropic::schema::{CountTokensResponse, MessagesRequest};
use crate::monitor::usage_from_anthropic_sse;
use crate::provider::{
    CliHandlers, Generation, GenerationBody, Provider, ProviderError, ProviderErrorKind,
    RequestContext,
};
use crate::providers::kimi::auth::token_store::file_store;
use crate::providers::kimi::translate::model_allowlist::public_model_ids;
use crate::providers::kimi::translate::model_allowlist::{
    allowed_on, assert_allowed_model, resolve_model, resolve_model_for_endpoint,
};
use crate::providers::kimi::translate::request::{TranslateOptions, translate_request};

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

pub struct KimiProvider;

impl Default for KimiProvider {
    fn default() -> Self {
        Self::new()
    }
}

impl KimiProvider {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl Provider for KimiProvider {
    /// Kimi signs in through this proxy: its login file or its API key file is the whole test.
    fn availability(&self) -> crate::provider::Availability {
        if client::api_key().is_some() {
            return crate::provider::Availability::Ready;
        }
        crate::provider::Availability::from_file(
            &crate::paths::provider_auth_file("kimi"),
            "run `claude-codex kimi login`",
        )
    }
    fn name(&self) -> &'static str {
        "kimi"
    }

    fn supported_models(&self) -> Vec<String> {
        public_model_ids().iter().map(|s| s.to_string()).collect()
    }

    fn cli(&self) -> &'static dyn CliHandlers {
        &KIMI_CLI
    }

    async fn handle_messages(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        let want_stream = body.stream;
        let model = body
            .model
            .clone()
            .unwrap_or_else(|| "kimi-for-coding".into());
        match self.generate_anthropic_stream(body, ctx).await {
            Ok(generation) => {
                let body = match generation.body {
                    GenerationBody::LiveSse(body) => body,
                    GenerationBody::BufferedSse(bytes) => axum::body::Body::from(bytes),
                };
                if want_stream {
                    (
                        [
                            (http::header::CONTENT_TYPE, "text/event-stream"),
                            (http::header::CACHE_CONTROL, "no-cache"),
                        ],
                        body,
                    )
                        .into_response()
                } else {
                    match axum::body::to_bytes(body, usize::MAX).await {
                        Ok(bytes) => {
                            if crate::anthropic::sse::parse_sse_events(&bytes)
                                .iter()
                                .any(|event| event.event.as_deref() == Some("error"))
                            {
                                return json_error(
                                    StatusCode::BAD_GATEWAY,
                                    "api_error",
                                    "Kimi upstream stream failed",
                                );
                            }
                            match crate::anthropic::accumulate::accumulate_response(
                                &bytes,
                                &format!("msg_{}", uuid::Uuid::new_v4().simple()),
                                &model,
                            ) {
                                Ok(json) => Json(json).into_response(),
                                Err(error) => json_error(
                                    StatusCode::BAD_GATEWAY,
                                    "api_error",
                                    error.to_string(),
                                ),
                            }
                        }
                        Err(error) => {
                            json_error(StatusCode::BAD_GATEWAY, "api_error", error.to_string())
                        }
                    }
                }
            }
            Err(error) => {
                let mut response = json_error(error.status, error.error_type(), error.message);
                if let Some(retry_after) = error
                    .retry_after
                    .and_then(|value| value.parse::<http::HeaderValue>().ok())
                {
                    response
                        .headers_mut()
                        .insert(http::header::RETRY_AFTER, retry_after);
                }
                response
            }
        }
    }

    async fn handle_count_tokens(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        let model = body.model.as_deref().unwrap_or("kimi-for-coding");
        let resolved = resolve_model(model);
        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.model_resolved(&ctx.req_id, &resolved);
        }
        let tokens = count_tokens::count_tokens(&body);
        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.usage_updated(&ctx.req_id, Some(tokens), None);
        }
        (
            StatusCode::OK,
            Json(CountTokensResponse {
                input_tokens: tokens,
            }),
        )
            .into_response()
    }

    async fn generate_anthropic_stream(
        &self,
        mut body: MessagesRequest,
        ctx: RequestContext,
    ) -> Result<Generation, ProviderError> {
        body.stream = true;
        let requested = body
            .model
            .clone()
            .unwrap_or_else(|| "kimi-for-coding".to_string());
        assert_allowed_model(&resolve_model(&requested)).map_err(|error| {
            ProviderError::new(
                StatusCode::BAD_REQUEST,
                ProviderErrorKind::InvalidRequest,
                format!(
                    "Model \"{requested}\" resolves to unsupported model \"{}\"",
                    error.model
                ),
            )
        })?;
        let mut translated = translate_request(
            &body,
            TranslateOptions {
                session_id: ctx.session_id.clone(),
            },
        )
        .map_err(|error| {
            ProviderError::new(
                StatusCode::BAD_REQUEST,
                ProviderErrorKind::InvalidRequest,
                error.to_string(),
            )
        })?;
        let (upstream, resolved) = self
            .post_first_lane(&mut translated, &requested, &ctx)
            .await?;
        let message_id = format!("msg_{}", uuid::Uuid::new_v4().simple());
        let translator = translate::stream::LiveTranslator::new(&message_id, &requested);
        use futures_util::StreamExt;
        let stream = futures_util::stream::unfold(
            (upstream.body.bytes_stream(), translator, ctx, false),
            |(mut upstream, mut translator, ctx, terminal)| async move {
                use futures_util::StreamExt;
                if terminal {
                    return None;
                }
                loop {
                    let result = match upstream.next().await {
                        Some(Ok(bytes)) => translator.push(&bytes),
                        Some(Err(error)) => Err(anyhow::anyhow!(error)),
                        None => translator.finish(),
                    };
                    match result {
                        Ok(bytes) if bytes.is_empty() && !translator.finished => continue,
                        Ok(bytes) => {
                            let terminal = translator.finished;
                            if let Some(monitor) = ctx.monitor.as_ref() {
                                let (input, output) = usage_from_anthropic_sse(&bytes);
                                monitor.stream_progress(
                                    &ctx.req_id,
                                    bytes.len() as u64,
                                    count_sse_events(&bytes),
                                    input,
                                    output,
                                );
                            }
                            return Some((
                                Ok::<bytes::Bytes, ProviderError>(bytes.into()),
                                (upstream, translator, ctx, terminal),
                            ));
                        }
                        Err(error) => {
                            let error = ProviderError::new(
                                StatusCode::BAD_GATEWAY,
                                ProviderErrorKind::Api,
                                error.to_string(),
                            );
                            return Some((Err(error), (upstream, translator, ctx, true)));
                        }
                    }
                }
            },
        );
        let mut stream = Box::pin(stream);
        let first = stream.next().await.transpose()?;
        let stream = futures_util::stream::iter(first.map(Ok))
            .chain(stream)
            .map(|result| {
                let bytes = result.unwrap_or_else(|error| {
                    let data = serde_json::json!({"type":"error", "error":{"type":error.error_type(), "message":error.message}});
                    crate::anthropic::sse::encode_sse_event(Some("error"), &data.to_string()).into()
                });
                Ok::<_, std::convert::Infallible>(bytes)
            });
        Ok(Generation {
            body: GenerationBody::LiveSse(axum::body::Body::from_stream(stream)),
            resolved_model: resolved,
        })
    }
}

impl KimiProvider {
    /// Login first, then the API key. A lane out of quota or credit, or rejecting its
    /// credential, hands over to the next; the last lane's error goes to the fallback chain.
    async fn post_first_lane(
        &self,
        translated: &mut translate::request::KimiChatRequest,
        requested: &str,
        ctx: &RequestContext,
    ) -> Result<(client::KimiResponse, String), ProviderError> {
        let http = client::KimiHttpClient::new();
        let mut last = None;
        for lane in client::lanes() {
            let model = resolve_model_for_endpoint(requested, lane.endpoint);
            if !allowed_on(&model, lane.endpoint) {
                continue;
            }
            translated.model = model.clone();
            if let Some(traffic) = ctx.traffic.as_ref() {
                traffic.write_json(
                    "020-upstream-request",
                    &serde_json::to_value(&*translated).unwrap_or_default(),
                );
            }
            if let Some(monitor) = ctx.monitor.as_ref() {
                monitor.model_resolved(&ctx.req_id, &model);
                monitor.upstream_started(&ctx.req_id);
            }
            match http.post_kimi(translated, &lane).await {
                Ok(upstream) => return Ok((upstream, model)),
                Err(error) if error.lane_exhausted() => last = Some(error),
                Err(error) => return Err(kimi_provider_error(error)),
            }
        }
        Err(last.map(kimi_provider_error).unwrap_or_else(|| {
            ProviderError::new(
                StatusCode::BAD_REQUEST,
                ProviderErrorKind::InvalidRequest,
                format!("Model \"{requested}\" is not served by any configured Kimi credential"),
            )
        }))
    }
}

fn count_sse_events(bytes: &[u8]) -> u64 {
    String::from_utf8_lossy(bytes).matches("event:").count() as u64
}

fn kimi_provider_error(err: client::KimiError) -> ProviderError {
    let (status, kind) = match err.status {
        400 | 404 | 422 => (StatusCode::BAD_REQUEST, ProviderErrorKind::InvalidRequest),
        401 => (StatusCode::UNAUTHORIZED, ProviderErrorKind::Authentication),
        403 => (StatusCode::FORBIDDEN, ProviderErrorKind::Permission),
        429 => (StatusCode::TOO_MANY_REQUESTS, ProviderErrorKind::RateLimit),
        _ => (StatusCode::BAD_GATEWAY, ProviderErrorKind::Api),
    };
    let mut error = ProviderError::new(status, kind, err.detail.unwrap_or(err.message));
    if err.status == 429 {
        error.retry_after = Some(err.retry_after.unwrap_or_else(|| "5".to_string()));
    }
    error
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

pub(crate) struct KimiCli;

impl CliHandlers for KimiCli {
    fn login(&self) -> Result<(), anyhow::Error> {
        let tokens = auth::login::run_device_login()?;
        let store = file_store();
        let manager = auth::manager::KimiAuthManager::new(store);
        let saved = manager.persist_initial_tokens(&tokens)?;
        println!("Auth saved in {}", manager.store.auth_path());
        if let Some(ref uid) = saved.user_id {
            println!("User: {uid}");
        }
        println!("Authentication complete");
        Ok(())
    }

    fn device(&self) -> Result<(), anyhow::Error> {
        self.login()
    }

    fn status(&self) -> Result<(), anyhow::Error> {
        let store = file_store();
        let stored = store.load_auth()?;
        match stored {
            Some(auth) => {
                println!("Auth path: {}", store.auth_path());
                println!("Authenticated: true");
                if let Some(ref uid) = auth.user_id {
                    println!("User: {uid}");
                }
                if let Some(ref scope) = auth.scope {
                    println!("Scope: {scope}");
                }
                let remaining = auth.expires.saturating_sub(now_ms()) / 1000;
                println!("Expires in {remaining}s");
                Ok(())
            }
            None => {
                anyhow::bail!("Not authenticated");
            }
        }
    }

    fn logout(&self) -> Result<(), anyhow::Error> {
        let store = file_store();
        store.clear_auth()?;
        println!("Logged out");
        Ok(())
    }
}

pub(crate) static KIMI_CLI: KimiCli = KimiCli;
