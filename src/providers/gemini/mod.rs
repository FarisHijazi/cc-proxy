//! Gemini backend, via a local [gemini-web-api] server.
//!
//! [gemini-web-api] exposes the gemini.google.com web app as an
//! OpenAI-compatible API, so this backend is a plain Anthropic <-> OpenAI
//! chat-completions translation over a configurable base URL. It needs no
//! credentials of its own: the Google session lives in that server.
//!
//! Chat only. The server can also generate images and Veo video, but the
//! Anthropic Messages surface has nowhere to express either, so those routes
//! are deliberately not wired up.
//!
//! Because the base URL is configurable, this also works against any other
//! OpenAI-compatible server.
//!
//! [gemini-web-api]: https://github.com/FarisHijazi/gemini-web-api

pub mod client;
pub mod models;
pub mod translate;

use std::convert::Infallible;

use async_trait::async_trait;
use axum::{
    Json,
    body::Body,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use bytes::Bytes;
use futures_util::{Stream, StreamExt, TryStreamExt};

use crate::anthropic::{
    accumulate::accumulate_response,
    error::json_error,
    schema::{CountTokensResponse, MessagesRequest},
};
use crate::monitor::{MonitorHandle, usage_from_anthropic_sse};
use crate::provider::{
    CliHandlers, Generation, GenerationBody, Provider, ProviderError, ProviderErrorKind,
    RequestContext,
};

use self::client::{GeminiClient, GeminiError};
use self::models::{GEMINI_DEFAULT_MODEL, GEMINI_MODELS, assert_allowed_model, resolve_model};
use self::translate::request::translate_request_with;
use self::translate::stream::StreamTranslator;

/// Shared OpenAI chat-completions transport. Gemini retains its existing
/// defaults; the optional local backend has independent endpoint/model config.
pub struct GeminiProvider {
    local: bool,
}

impl Default for GeminiProvider {
    fn default() -> Self {
        Self::new()
    }
}

impl GeminiProvider {
    pub fn new() -> Self {
        Self { local: false }
    }

    pub fn local() -> Self {
        Self { local: true }
    }

    fn client(&self) -> anyhow::Result<GeminiClient> {
        if !self.local {
            return GeminiClient::from_config();
        }
        crate::config::local_context_window()?;
        let config = crate::config::local_config();
        let base_url = config.base_url.ok_or_else(|| {
            anyhow::anyhow!("local backend disabled: set CCP_LOCAL_BASE_URL or local.baseUrl")
        })?;
        GeminiClient::with_timeout(
            base_url,
            config.api_key,
            std::time::Duration::from_secs(config.timeout_secs.unwrap_or(3600).max(1)),
        )
    }

    /// Shared prologue: resolve the model and build the upstream request.
    fn prepare(
        &self,
        body: &MessagesRequest,
        ctx: &RequestContext,
    ) -> Result<(String, translate::request::GeminiChatRequest), ProviderError> {
        let requested = body.model.as_deref().unwrap_or(GEMINI_DEFAULT_MODEL);
        let resolved = if self.local {
            let config = crate::config::local_config();
            config.upstream_model.unwrap_or_else(|| {
                config
                    .model
                    .unwrap_or_else(|| "local-model".into())
            })
        } else {
            resolve_model(requested)
        };
        if !self.local {
            assert_allowed_model(&resolved).map_err(|error| {
                ProviderError::new(
                    StatusCode::BAD_REQUEST,
                    ProviderErrorKind::InvalidRequest,
                    format!(
                        "Model \"{requested}\" resolves to unsupported model \"{}\"",
                        error.model
                    ),
                )
            })?;
        }
        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.model_resolved(&ctx.req_id, &resolved);
        }
        let mut translated = translate_request_with(body, &resolved, self.local).map_err(|error| {
            ProviderError::new(
                StatusCode::BAD_REQUEST,
                ProviderErrorKind::InvalidRequest,
                error.to_string(),
            )
        })?;
        if self.local {
            translated.stream_options = Some(serde_json::json!({"include_usage": true}));
            // Some local servers accept only string tool_choice values. A
            // single advertised function + required preserves named selection.
            if let Some(name) = body
                .extra
                .get("tool_choice")
                .filter(|choice| {
                    choice.get("type").and_then(serde_json::Value::as_str) == Some("tool")
                })
                .and_then(|choice| choice.get("name"))
                .and_then(serde_json::Value::as_str)
            {
                if let Some(tools) = translated.tools.as_mut() {
                    tools.retain(|tool| tool.function.name == name);
                }
                if translated.tools.as_ref().is_none_or(Vec::is_empty) {
                    return Err(ProviderError::new(
                        StatusCode::BAD_REQUEST,
                        ProviderErrorKind::InvalidRequest,
                        "named local tool_choice must reference an advertised tool",
                    ));
                }
                translated.tool_choice = Some(serde_json::json!("required"));
            }
        }
        Ok((resolved, translated))
    }
}

#[async_trait]
impl Provider for GeminiProvider {
    /// Reachability, not credentials: the Google session lives in the
    /// gemini-web-api server, so "is that server up" is the whole question.
    ///
    /// A TCP connect rather than an HTTP request — enough to tell a listening
    /// server from nothing at all, and bounded so a listing never hangs on it.
    fn availability(&self) -> crate::provider::Availability {
        let base_url = if self.local {
            if let Err(error) = crate::config::local_context_window() {
                return crate::provider::Availability::unavailable(error.to_string());
            }
            let Some(url) = crate::config::local_config().base_url else {
                return crate::provider::Availability::unavailable(
                    "set CCP_LOCAL_BASE_URL or local.baseUrl",
                );
            };
            url
        } else {
            crate::config::gemini_base_url()
        };
        if server_is_listening(&base_url) {
            crate::provider::Availability::Ready
        } else {
            crate::provider::Availability::unavailable(format!(
                "start {} upstream at {}",
                self.name(),
                base_url
            ))
        }
    }

    fn name(&self) -> &'static str {
        if self.local { "local" } else { "gemini" }
    }

    fn supported_models(&self) -> Vec<String> {
        if self.local {
            return vec![
                crate::config::local_config()
                    .model
                    .unwrap_or_else(|| "local-model".into()),
            ];
        }
        GEMINI_MODELS
            .iter()
            .map(|model| (*model).to_string())
            .collect()
    }

    fn cli(&self) -> &'static dyn CliHandlers {
        if self.local { &LOCAL_CLI } else { &GEMINI_CLI }
    }

    async fn handle_messages(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        let want_stream = body.stream;
        let message_id = format!("msg_{}", uuid::Uuid::new_v4().simple());
        // gemini-web-api reports no usage, so carry an estimate for the
        // translator to fall back on. Same estimator as /count_tokens, so the
        // two agree.
        let estimated_input_tokens = crate::providers::kimi::count_tokens::count_tokens(&body);

        let (upstream_model, mut translated) = match self.prepare(&body, &ctx) {
            Ok(prepared) => prepared,
            Err(error) => return provider_error_response(&error),
        };
        let resolved = if self.local {
            body.model.clone().unwrap_or(upstream_model)
        } else {
            upstream_model
        };
        // Always stream upstream: one code path feeds both reply shapes, and
        // a long browser-backed turn is less likely to hit an idle timeout.
        translated.stream = true;

        if let Some(traffic) = ctx.traffic.as_ref() {
            traffic.write_json(
                "020-upstream-request",
                &serde_json::to_value(&translated).unwrap_or_default(),
            );
        }

        let client = match self.client() {
            Ok(client) => client,
            Err(error) => {
                return json_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "api_error",
                    format!("{} client setup failed: {error}", self.name()),
                );
            }
        };

        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.upstream_started(&ctx.req_id);
        }
        if self.local && want_stream {
            // Start downstream streaming before even the upstream headers arrive:
            // CPU prefill can exceed a forward proxy's response-idle deadline.
            let upstream = futures_util::stream::once(async move {
                client
                    .post_chat(&translated)
                    .await
                    .map(|response| Box::pin(response.into_stream()))
            })
            .try_flatten();
            return stream_anthropic_response(
                Box::pin(upstream),
                message_id,
                resolved,
                estimated_input_tokens,
                ctx.monitor.clone(),
                ctx.req_id.clone(),
                Some(std::time::Duration::from_secs(15)),
            );
        }
        let upstream = match client.post_chat(&translated).await {
            Ok(response) => response,
            Err(error) => return gemini_error_response(&error),
        };

        if want_stream {
            return stream_anthropic_response(
                Box::pin(upstream.into_stream()),
                message_id,
                resolved,
                estimated_input_tokens,
                ctx.monitor.clone(),
                ctx.req_id.clone(),
                None,
            );
        }

        let bytes = match upstream.into_bytes().await {
            Ok(bytes) => bytes,
            Err(error) => return gemini_error_response(&error),
        };
        if let Some(traffic) = ctx.traffic.as_ref() {
            traffic.write_bytes("032-upstream-response-body.sse", &bytes);
        }
        let mut translator = StreamTranslator::new(&message_id, &resolved)
            .with_fallback_input_tokens(estimated_input_tokens);
        let mut sse = translator.push_bytes(&bytes);
        translator.finish(&mut sse);
        match accumulate_response(&sse, &message_id, &resolved) {
            Ok(value) => {
                if let Some(monitor) = ctx.monitor.as_ref() {
                    monitor.usage_updated(
                        &ctx.req_id,
                        value
                            .pointer("/usage/input_tokens")
                            .and_then(|v| v.as_u64()),
                        value
                            .pointer("/usage/output_tokens")
                            .and_then(|v| v.as_u64()),
                    );
                }
                (StatusCode::OK, Json(value)).into_response()
            }
            Err(error) => json_error(
                StatusCode::BAD_GATEWAY,
                "api_error",
                format!("Accumulation error: {error}"),
            ),
        }
    }

    async fn handle_count_tokens(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        // Gemini's tokenizer is not public; Claude Code only needs a
        // monotonic estimate for its compaction logic. Shared with kimi so
        // both backends estimate identically.
        let tokens = crate::providers::kimi::count_tokens::count_tokens(&body);
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
        let message_id = format!("msg_{}", uuid::Uuid::new_v4().simple());
        let (upstream_model, mut translated) = self.prepare(&body, &ctx)?;
        let resolved = if self.local {
            body.model.clone().unwrap_or(upstream_model)
        } else {
            upstream_model
        };
        translated.stream = true;

        let client = self.client().map_err(|error| {
            ProviderError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                ProviderErrorKind::Api,
                format!("{} client setup failed: {error}", self.name()),
            )
        })?;
        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.upstream_started(&ctx.req_id);
        }
        let upstream = client
            .post_chat(&translated)
            .await
            .map_err(gemini_provider_error)?;
        let bytes = upstream.into_bytes().await.map_err(gemini_provider_error)?;
        if let Some(traffic) = ctx.traffic.as_ref() {
            traffic.write_bytes("032-upstream-response-body.sse", &bytes);
        }
        let mut translator = StreamTranslator::new(&message_id, &resolved)
            .with_fallback_input_tokens(crate::providers::kimi::count_tokens::count_tokens(&body));
        let mut sse = translator.push_bytes(&bytes);
        translator.finish(&mut sse);
        if let Some(traffic) = ctx.traffic.as_ref() {
            traffic.write_bytes("050-anthropic-intermediate.sse", &sse);
        }
        if let Some(monitor) = ctx.monitor.as_ref() {
            let (input_tokens, output_tokens) = usage_from_anthropic_sse(&sse);
            monitor.stream_progress(
                &ctx.req_id,
                sse.len() as u64,
                sse.windows(6)
                    .filter(|window| **window == b"event:"[..])
                    .count() as u64,
                input_tokens,
                output_tokens,
            );
        }
        Ok(Generation {
            body: GenerationBody::BufferedSse(sse.into()),
            resolved_model: resolved,
        })
    }
}

/// Pipe upstream bytes through the translator as they arrive, so the caller
/// sees tokens at the same pace the backend produces them.
fn stream_anthropic_response<S>(
    upstream: S,
    message_id: String,
    model: String,
    fallback_input_tokens: u64,
    monitor: Option<MonitorHandle>,
    req_id: String,
    keepalive_interval: Option<std::time::Duration>,
) -> Response
where
    S: Stream<Item = Result<Bytes, GeminiError>> + Unpin + Send + 'static,
{
    struct State<S> {
        upstream: S,
        translator: StreamTranslator,
        monitor: Option<MonitorHandle>,
        req_id: String,
        started: bool,
        terminal: bool,
        heartbeat: Option<tokio::time::Interval>,
    }

    // `unfold` owns the upstream for the life of the response body.
    let state = State {
        upstream,
        translator: StreamTranslator::new(message_id, model)
            .with_fallback_input_tokens(fallback_input_tokens),
        monitor,
        req_id,
        started: false,
        terminal: false,
        heartbeat: keepalive_interval.map(tokio::time::interval),
    };

    let stream = futures_util::stream::unfold(state, |mut state| async move {
        if state.terminal {
            return None;
        }
        loop {
            let item = if let Some(heartbeat) = state.heartbeat.as_mut() {
                tokio::select! {
                    biased;
                    item = state.upstream.next() => item,
                    _ = heartbeat.tick() => {
                        return Some((Ok(Bytes::from_static(b": keep-alive\n\n")), state));
                    }
                }
            } else {
                state.upstream.next().await
            };
            let item = match item {
                None if state.heartbeat.is_some() => Some(Err(GeminiError {
                    status: StatusCode::BAD_GATEWAY,
                    message: "local upstream stream ended before its [DONE] marker".into(),
                    retry_after: None,
                })),
                item => item,
            };
            match item {
                Some(Ok(chunk)) => {
                    if !state.started {
                        state.started = true;
                        if let Some(monitor) = state.monitor.as_ref() {
                            monitor.generation_started(&state.req_id);
                        }
                    }
                    let out = state.translator.push_bytes(&chunk);
                    if let Some(monitor) = state.monitor.as_ref() {
                        monitor.stream_progress(&state.req_id, chunk.len() as u64, 1, None, None);
                    }
                    if state.translator.is_finished() {
                        state.terminal = true;
                        return Some((Ok::<Bytes, Infallible>(Bytes::from(out)), state));
                    }
                    if out.is_empty() {
                        // Nothing complete yet; keep reading rather than
                        // emitting an empty frame.
                        continue;
                    }
                    return Some((Ok::<Bytes, Infallible>(Bytes::from(out)), state));
                }
                // A dropped connection still gets a terminated message, so the
                // client is not left waiting on a stream that will never close.
                Some(Err(error)) if state.heartbeat.is_some() => {
                    state.terminal = true;
                    let error = gemini_provider_error(error);
                    let payload = serde_json::json!({
                        "type": "error",
                        "error": {"type": error.error_type(), "message": error.message},
                    });
                    let frame = format!("event: error\ndata: {payload}\n\n");
                    return Some((Ok(Bytes::from(frame)), state));
                }
                Some(Err(_)) | None => {
                    state.terminal = true;
                    let mut out = Vec::new();
                    state.translator.finish(&mut out);
                    if let Some(monitor) = state.monitor.as_ref() {
                        let (input_tokens, output_tokens) = usage_from_anthropic_sse(&out);
                        monitor.usage_updated(&state.req_id, input_tokens, output_tokens);
                    }
                    return Some((Ok::<Bytes, Infallible>(Bytes::from(out)), state));
                }
            }
        }
    });

    (
        [
            (http::header::CONTENT_TYPE, "text/event-stream"),
            (http::header::CACHE_CONTROL, "no-cache"),
        ],
        Body::from_stream(stream),
    )
        .into_response()
}

/// A local server's context overflow, rewritten as Anthropic's own wording.
/// Claude Code matches `prompt is too long: N tokens > M maximum` and compacts
/// then retries; any other 400 just ends the turn with an error.
fn context_overflow(error: &GeminiError) -> Option<ProviderError> {
    if error.status != StatusCode::BAD_REQUEST {
        return None;
    }
    let body: serde_json::Value = serde_json::from_str(&error.message).ok()?;
    let detail = body.get("error")?;
    if detail.get("type")?.as_str()? != "exceed_context_size_error" {
        return None;
    }
    let prompt = detail.get("n_prompt_tokens")?.as_u64()?;
    let window = detail.get("n_ctx")?.as_u64()?;
    Some(ProviderError::new(
        StatusCode::BAD_REQUEST,
        ProviderErrorKind::InvalidRequest,
        format!("prompt is too long: {prompt} tokens > {window} maximum"),
    ))
}

fn gemini_provider_error(error: GeminiError) -> ProviderError {
    if let Some(overflow) = context_overflow(&error) {
        return overflow;
    }
    let kind = match error.status {
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => ProviderErrorKind::Authentication,
        StatusCode::TOO_MANY_REQUESTS => ProviderErrorKind::RateLimit,
        _ => ProviderErrorKind::Api,
    };
    let status = match error.status {
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => StatusCode::UNAUTHORIZED,
        StatusCode::TOO_MANY_REQUESTS => StatusCode::TOO_MANY_REQUESTS,
        _ => StatusCode::BAD_GATEWAY,
    };
    let mut provider_error = ProviderError::new(status, kind, error.message);
    provider_error.retry_after = error.retry_after;
    provider_error
}

fn provider_error_response(error: &ProviderError) -> Response {
    json_error(error.status, error.error_type(), error.message.clone())
}

fn gemini_error_response(error: &GeminiError) -> Response {
    let provider_error = gemini_provider_error(GeminiError {
        status: error.status,
        message: error.message.clone(),
        retry_after: error.retry_after.clone(),
    });
    provider_error_response(&provider_error)
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

pub(crate) struct GeminiCli;

impl CliHandlers for GeminiCli {
    fn login(&self) -> anyhow::Result<()> {
        anyhow::bail!(
            "gemini has no login of its own: the Google session lives in the gemini-web-api \
             server. Start it (see https://github.com/FarisHijazi/gemini-web-api) and make sure \
             Chrome is signed in to gemini.google.com, then run `claude-codex gemini auth status`."
        )
    }

    fn device(&self) -> anyhow::Result<()> {
        self.login()
    }

    /// Reachability check rather than a credential check — that is the failure
    /// this backend actually has.
    fn status(&self) -> anyhow::Result<()> {
        let base_url = crate::config::gemini_base_url();
        let client = GeminiClient::new(base_url.clone(), crate::config::gemini_api_key())?;
        println!("Base URL: {base_url}");
        println!("Endpoint: {}", client.endpoint());
        println!(
            "API key: {}",
            if crate::config::gemini_api_key().is_some() {
                "configured"
            } else {
                "not set (the server accepts any key by default)"
            }
        );

        let models_url = format!("{}/models", base_url.trim_end_matches('/'));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let reachable = runtime.block_on(async {
            reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(5))
                .timeout(std::time::Duration::from_secs(15))
                .build()
                .ok()?
                .get(&models_url)
                .send()
                .await
                .ok()
        });

        match reachable {
            Some(response) if response.status().is_success() => {
                println!("Server: reachable");
                Ok(())
            }
            Some(response) => {
                anyhow::bail!("server responded {} at {models_url}", response.status())
            }
            None => anyhow::bail!(
                "cannot reach the gemini-web-api server at {models_url}. Start it with \
                 `uvx --from git+https://github.com/FarisHijazi/gemini-web-api gemini-web-api`, \
                 or point CCP_GEMINI_BASE_URL somewhere else."
            ),
        }
    }

    fn logout(&self) -> anyhow::Result<()> {
        println!(
            "gemini stores no credentials in the proxy; sign out inside the gemini-web-api server."
        );
        Ok(())
    }
}

pub(crate) static GEMINI_CLI: GeminiCli = GeminiCli;

struct LocalCli;
static LOCAL_CLI: LocalCli = LocalCli;

impl CliHandlers for LocalCli {
    fn login(&self) -> anyhow::Result<()> {
        anyhow::bail!(
            "local has no interactive login; configure local.baseUrl and optional local.apiKey"
        )
    }
    fn device(&self) -> anyhow::Result<()> {
        self.login()
    }
    fn status(&self) -> anyhow::Result<()> {
        match GeminiProvider::local().availability() {
            crate::provider::Availability::Ready => {
                println!("Local upstream: reachable");
                Ok(())
            }
            crate::provider::Availability::Unavailable { hint } => anyhow::bail!(hint),
        }
    }
    fn logout(&self) -> anyhow::Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn local_context_overflow_becomes_anthropic_prompt_too_long() {
        let error = super::GeminiError {
            status: http::StatusCode::BAD_REQUEST,
            message: r#"{"error":{"code":400,"message":"request (140052 tokens) exceeds the available context size (131072 tokens), try increasing it","type":"exceed_context_size_error","n_prompt_tokens":140052,"n_ctx":131072}}"#.into(),
            retry_after: None,
        };
        let mapped = super::gemini_provider_error(error);
        assert_eq!(mapped.status, http::StatusCode::BAD_REQUEST);
        assert_eq!(mapped.message, "prompt is too long: 140052 tokens > 131072 maximum");
        assert_eq!(mapped.error_type(), "invalid_request_error");
    }

    #[test]
    fn other_bad_requests_are_not_rewritten() {
        let error = super::GeminiError {
            status: http::StatusCode::BAD_REQUEST,
            message: "plain failure".into(),
            retry_after: None,
        };
        assert_eq!(super::gemini_provider_error(error).message, "plain failure");
    }

    use super::*;

    #[tokio::test(start_paused = true)]
    async fn local_keepalive_covers_pending_headers_and_first_token_without_extending_gemini() {
        use http_body_util::BodyExt;
        use std::time::Duration;

        fn delayed_upstream() -> impl Stream<Item = Result<Bytes, GeminiError>> + Send {
            // The outer future represents waiting for response headers; the
            // inner future represents an idle response body before generation.
            futures_util::stream::once(async {
                tokio::time::sleep(Duration::from_secs(60)).await;
                Ok::<_, GeminiError>(Box::pin(futures_util::stream::once(async {
                    tokio::time::sleep(Duration::from_secs(60)).await;
                    Ok(Bytes::from_static(b"data: {\"choices\":[{\"delta\":{\"content\":\"finished\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"))
                })))
            })
            .try_flatten()
        }

        let response = stream_anthropic_response(
            Box::pin(delayed_upstream()),
            "msg_local".into(),
            "local".into(),
            1,
            None,
            "req".into(),
            Some(Duration::from_secs(15)),
        );
        let mut body = response.into_body();
        let started = tokio::time::Instant::now();
        let mut frames = Vec::new();
        loop {
            let frame = tokio::time::timeout(Duration::from_secs(20), body.frame())
                .await
                .expect("local stream must survive a shorter read-idle deadline");
            let Some(frame) = frame else { break };
            frames.push(frame.unwrap().into_data().unwrap());
        }
        assert!(started.elapsed() >= Duration::from_secs(120));
        assert!(
            frames
                .iter()
                .filter(|f| f.starts_with(b": keep-alive"))
                .count()
                >= 8
        );
        let text = frames
            .iter()
            .map(|f| String::from_utf8_lossy(f))
            .collect::<String>();
        assert!(text.contains("finished"));
        assert!(text.contains("message_stop"));

        let response = stream_anthropic_response(
            Box::pin(delayed_upstream()),
            "msg_gemini".into(),
            "gemini".into(),
            1,
            None,
            "req".into(),
            None,
        );
        let mut body = response.into_body();
        assert!(
            tokio::time::timeout(Duration::from_secs(20), body.frame())
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn local_lazy_upstream_error_is_an_error_event_without_false_completion() {
        use http_body_util::BodyExt;
        let upstream = futures_util::stream::once(async {
            Err::<Bytes, _>(GeminiError {
                status: StatusCode::TOO_MANY_REQUESTS,
                message: "toy upstream unavailable".into(),
                retry_after: None,
            })
        });
        let response = stream_anthropic_response(
            Box::pin(upstream),
            "msg_error".into(),
            "local".into(),
            1,
            None,
            "req".into(),
            Some(std::time::Duration::from_secs(15)),
        );
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let text = String::from_utf8(bytes.to_vec()).unwrap();
        assert!(text.contains("event: error"));
        assert!(text.contains("rate_limit_error"));
        assert!(!text.contains("message_stop"));
    }

    #[tokio::test]
    async fn dropping_local_body_cancels_pending_upstream_without_a_background_task() {
        use http_body_util::BodyExt;
        use std::sync::{
            Arc,
            atomic::{AtomicBool, Ordering},
        };
        struct PendingRequest(Arc<AtomicBool>);
        impl Drop for PendingRequest {
            fn drop(&mut self) {
                self.0.store(true, Ordering::SeqCst);
            }
        }
        let cancelled = Arc::new(AtomicBool::new(false));
        let guard = PendingRequest(cancelled.clone());
        let upstream = futures_util::stream::once(async move {
            let _pending_request = guard;
            futures_util::future::pending::<Result<Bytes, GeminiError>>().await
        });
        let response = stream_anthropic_response(
            Box::pin(upstream),
            "msg_cancel".into(),
            "local".into(),
            1,
            None,
            "req".into(),
            Some(std::time::Duration::from_secs(15)),
        );
        let mut body = response.into_body();
        body.frame().await.unwrap().unwrap();
        assert!(!cancelled.load(Ordering::SeqCst));
        drop(body);
        assert!(cancelled.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn local_premature_eof_is_not_a_successful_empty_or_partial_completion() {
        use http_body_util::BodyExt;
        for chunks in [
            vec![],
            vec![Bytes::from_static(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n",
            )],
        ] {
            let response = stream_anthropic_response(
                futures_util::stream::iter(chunks.into_iter().map(Ok)),
                "msg_eof".into(),
                "local".into(),
                1,
                None,
                "req".into(),
                Some(std::time::Duration::from_secs(15)),
            );
            let bytes = response.into_body().collect().await.unwrap().to_bytes();
            let text = String::from_utf8(bytes.to_vec()).unwrap();
            assert!(text.contains("event: error"));
            assert!(!text.contains("message_stop"));
        }
        let response = stream_anthropic_response(
            futures_util::stream::empty(),
            "msg_gemini".into(),
            "gemini".into(),
            1,
            None,
            "req".into(),
            None,
        );
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        assert!(String::from_utf8_lossy(&bytes).contains("message_stop"));
    }

    #[test]
    fn provider_advertises_its_models() {
        let provider = GeminiProvider::new();
        assert_eq!(provider.name(), "gemini");
        let models = provider.supported_models();
        assert!(models.contains(&"gemini-3-pro".to_string()));
        assert!(models.contains(&"gemini-3-flash".to_string()));
        assert_eq!(models.len(), GEMINI_MODELS.len());
    }

    /// Routing only sends gemini ids here, so a foreign id is not an error to
    /// report — it resolves to the default rather than failing a request that
    /// reached this backend deliberately (via an alias remap, say).
    #[test]
    fn foreign_model_id_resolves_to_the_default() {
        let body = MessagesRequest {
            model: Some("gpt-5.6-sol".into()),
            max_tokens: Some(16),
            messages: vec![],
            stream: false,
            bypass_provider_model_override: false,
            extra: serde_json::Map::new(),
        };
        let ctx = RequestContext {
            req_id: "req".into(),
            session_id: None,
            session_seq: None,
            provider: "gemini".into(),
            traffic: None,
            monitor: None,
            passthrough: None,
        };
        let (resolved, _) = GeminiProvider::new().prepare(&body, &ctx).expect("prepare");
        assert_eq!(resolved, GEMINI_DEFAULT_MODEL);
    }
}

/// Whether something accepts TCP connections at `base_url`'s host and port.
fn server_is_listening(base_url: &str) -> bool {
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::Duration;

    let Some((host, port)) = host_and_port(base_url) else {
        return false;
    };
    let Ok(addrs) = (host.as_str(), port).to_socket_addrs() else {
        return false;
    };
    addrs.into_iter().any(|addr| {
        TcpStream::connect_timeout(&addr, Duration::from_millis(PROBE_TIMEOUT_MS)).is_ok()
    })
}

const PROBE_TIMEOUT_MS: u64 = 400;

/// Host and port from a base URL, defaulting the port by scheme.
fn host_and_port(base_url: &str) -> Option<(String, u16)> {
    let (scheme, rest) = base_url.split_once("://").unwrap_or(("http", base_url));
    let authority = rest.split(['/', '?', '#']).next()?;
    let authority = authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host);
    match authority.rsplit_once(':') {
        // Not a port if it is part of a bare IPv6 address.
        Some((host, port)) if !host.contains(':') => Some((host.to_string(), port.parse().ok()?)),
        _ => Some((
            authority.trim_matches(['[', ']']).to_string(),
            if scheme == "https" { 443 } else { 80 },
        )),
    }
}

#[cfg(test)]
mod availability_tests {
    use super::{host_and_port, server_is_listening};

    #[test]
    fn splits_host_and_port_out_of_a_base_url() {
        assert_eq!(
            host_and_port("http://localhost:8100/v1"),
            Some(("localhost".into(), 8100))
        );
        assert_eq!(
            host_and_port("https://gemini.example.com/v1"),
            Some(("gemini.example.com".into(), 443))
        );
        assert_eq!(
            host_and_port("http://example.com/v1"),
            Some(("example.com".into(), 80))
        );
    }

    #[test]
    fn nothing_listens_on_a_closed_port() {
        // Port 1 is reserved and never bound by this test suite.
        assert!(!server_is_listening("http://127.0.0.1:1/v1"));
    }
}
