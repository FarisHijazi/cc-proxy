//! Anthropic passthrough backend.
//!
//! Unlike the other providers, this one performs no translation. Claude Code already
//! speaks the Anthropic Messages API and, when pointed at a custom base URL, forwards
//! its own subscription credentials (`Authorization: Bearer sk-ant-oat...`) plus the
//! `anthropic-beta` flags that drive prompt caching. So the correct behavior is a
//! transparent reverse proxy: relay the original body bytes and headers to
//! api.anthropic.com and stream the response straight back. The proxy holds zero
//! Anthropic credentials and never touches the cache-keyed request prefix.

use async_trait::async_trait;
use axum::body::{Body, HttpBody};
use axum::http::{HeaderMap, Method, Request, StatusCode};
use axum::response::Response;
use serde_json::Value;

use crate::anthropic::error::json_error;
use crate::anthropic::schema::MessagesRequest;
use crate::logging::create_logger;
use crate::provider::{CliHandlers, Provider, RequestContext};
use crate::providers::translate_shared::wrap_reasoning;
use crate::registry::ANTHROPIC_STYLE_ALIASES;

/// Rewrite an outgoing Anthropic request body so it survives a mid-conversation switch
/// away from the codex backend.
///
/// Claude Code stores the codex backend's reconstructed reasoning as `thinking` blocks
/// carrying an empty signature. Anthropic rejects those on replay (400
/// `Invalid signature in thinking block`), so every post-switch turn would otherwise pay
/// a failed round-trip plus Claude Code's strip-and-retry. A native Anthropic turn does
/// carry prior-turn reasoning forward, so instead of dropping it we convert each
/// signature-less `thinking` block into a tagged `text` block: Anthropic accepts text
/// without a signature and the reasoning stays in context. Genuine Anthropic reasoning
/// (a non-empty signature) is left untouched.
///
/// Returns rewritten bytes only when something changed; `None` forwards the body
/// verbatim, keeping the byte-identical cache prefix for pure-Anthropic conversations.
fn sanitize_anthropic_request(raw: &[u8], req_id: &str) -> Option<Vec<u8>> {
    let mut doc: Value = serde_json::from_slice(raw).ok()?;
    let obj = doc.as_object_mut()?;

    detect_hosted_web_search_regression(obj, req_id);

    let messages = obj.get_mut("messages")?.as_array_mut()?;
    let mut changed = false;
    for message in messages.iter_mut() {
        if message.get("role").and_then(Value::as_str) != Some("assistant") {
            continue;
        }
        let Some(content) = message.get_mut("content").and_then(Value::as_array_mut) else {
            continue;
        };
        for block in content.iter_mut() {
            changed |= rehydrate_unsigned_thinking(block);
        }
    }

    changed.then(|| serde_json::to_vec(&doc).unwrap_or_else(|_| raw.to_vec()))
}

/// Convert one signature-less `thinking` block into a tagged `text` block in place.
/// Returns whether the block was rewritten.
fn rehydrate_unsigned_thinking(block: &mut Value) -> bool {
    let Some(map) = block.as_object() else {
        return false;
    };
    if map.get("type").and_then(Value::as_str) != Some("thinking") {
        return false;
    }
    let signed = map
        .get("signature")
        .and_then(Value::as_str)
        .is_some_and(|sig| !sig.is_empty());
    if signed {
        return false;
    }
    let reasoning = map.get("thinking").and_then(Value::as_str).unwrap_or("");
    *block = serde_json::json!({
        "type": "text",
        "text": wrap_reasoning(reasoning),
    });
    true
}

/// Regression tripwire. Claude Code drives its `WebSearch` tool through an isolated,
/// history-free inner call, so the hosted `web_search_20250305` tool and its
/// reconstructed `server_tool_use` / `web_search_tool_result` blocks never appear in the
/// outer transcript. If that ever changes (hosted web search reaching a request that
/// already carries assistant history), those blocks would ride the transcript across a
/// model switch and this warning flags it so the assumption can be re-checked.
fn detect_hosted_web_search_regression(obj: &serde_json::Map<String, Value>, req_id: &str) {
    let messages = obj.get("messages").and_then(Value::as_array);
    let has_assistant_history = messages.is_some_and(|ms| {
        ms.iter()
            .any(|m| m.get("role").and_then(Value::as_str) == Some("assistant"))
    });
    let hosted_tool = obj
        .get("tools")
        .and_then(Value::as_array)
        .is_some_and(|ts| {
            ts.iter()
                .any(|t| t.get("type").and_then(Value::as_str) == Some("web_search_20250305"))
        });
    let reconstructed_block = messages.is_some_and(|ms| {
        ms.iter().any(|m| {
            m.get("content")
                .and_then(Value::as_array)
                .is_some_and(|blocks| {
                    blocks.iter().any(|b| {
                        matches!(
                            b.get("type").and_then(Value::as_str),
                            Some("server_tool_use") | Some("web_search_tool_result")
                        )
                    })
                })
        })
    });

    if (hosted_tool && has_assistant_history) || reconstructed_block {
        let mut fields = serde_json::Map::new();
        fields.insert("reqId".into(), Value::String(req_id.to_string()));
        fields.insert("hostedWebSearchTool".into(), Value::Bool(hosted_tool));
        fields.insert(
            "reconstructedSearchBlock".into(),
            Value::Bool(reconstructed_block),
        );
        create_logger("anthropic").warn("hosted_web_search_in_history", Some(fields));
    }
}

/// Transport framing and the proxy's own credential never cross the upstream boundary.
fn is_stripped_request_header(name: &str) -> bool {
    matches!(
        name,
        "host"
            | "connection"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "te"
            | "trailer"
            | "transfer-encoding"
            | "upgrade"
            | "content-length"
            | "proxy-connection"
            | crate::inbound_auth::PROXY_KEY_HEADER
            | crate::fallback::POOL_HEADER
    )
}

/// Response headers that must not be relayed back to Claude Code. Hop-by-hop and
/// framing headers are re-derived by axum for the streamed body. Encoding and
/// repeated end-to-end headers survive because bytes are never decompressed.
fn is_stripped_response_header(name: &str) -> bool {
    matches!(
        name,
        "connection"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "te"
            | "trailer"
            | "transfer-encoding"
            | "upgrade"
            | "proxy-connection"
    )
}

fn relay_headers(source: &HeaderMap, request: bool, websocket: bool) -> HeaderMap {
    let connection_fields: Vec<_> = source
        .get_all("connection")
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .map(|name| name.trim().to_ascii_lowercase())
        .collect();
    let mut headers = HeaderMap::with_capacity(source.len());
    for (name, value) in source {
        if websocket && matches!(name.as_str(), "connection" | "upgrade") {
            continue; // Set the two negotiated transport headers explicitly below.
        }
        if (if request {
            is_stripped_request_header(name.as_str())
        } else {
            is_stripped_response_header(name.as_str())
        }) || connection_fields.iter().any(|field| field == name.as_str())
        {
            continue;
        }
        headers.append(name.clone(), value.clone());
    }
    if websocket {
        headers.insert("connection", http::HeaderValue::from_static("upgrade"));
        headers.insert("upgrade", http::HeaderValue::from_static("websocket"));
    }
    // Preserve trailer frames and negotiate TE explicitly for this transport hop.
    if let Some(trailer) = source.get("trailer") {
        headers.insert("trailer", trailer.clone());
    }
    if request
        && source
            .get_all("te")
            .iter()
            .filter_map(|value| value.to_str().ok())
            .flat_map(|value| value.split(','))
            .any(|value| value.trim().eq_ignore_ascii_case("trailers"))
    {
        headers.insert("te", http::HeaderValue::from_static("trailers"));
    }
    headers
}

pub struct AnthropicProvider {
    client: reqwest::Client,
    base_url: String,
}

impl AnthropicProvider {
    pub fn new() -> Self {
        Self::with_base_url(crate::config::anthropic_base_url())
    }

    /// Select the fixed account origin (also used by integration tests).
    pub fn with_base_url(base_url: impl Into<String>) -> Self {
        let mut builder = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .no_gzip()
            .no_brotli()
            .no_deflate()
            .no_zstd();
        if let Some((proxy, ca)) = crate::config::anthropic_upstream_proxy() {
            let pem = std::fs::read(&ca).expect("failed to read CCP_ANTHROPIC_PROXY_CA");
            builder = builder
                .proxy(reqwest::Proxy::https(&proxy).expect("invalid CCP_ANTHROPIC_PROXY"))
                .add_root_certificate(
                    reqwest::Certificate::from_pem(&pem).expect("invalid CCP_ANTHROPIC_PROXY_CA"),
                );
        }
        let client = builder
            .build()
            .expect("failed to build anthropic passthrough client");
        Self {
            client,
            base_url: base_url.into().trim_end_matches('/').to_string(),
        }
    }

    async fn relay(&self, ctx: RequestContext) -> Response {
        let RequestContext {
            req_id,
            monitor,
            passthrough,
            ..
        } = ctx;
        let Some(passthrough) = passthrough else {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "api_error",
                "anthropic passthrough is missing the original request",
            );
        };

        if let Some(monitor) = monitor.as_ref() {
            monitor.upstream_started(&req_id);
        }

        // Rehydrate signature-less codex `thinking` blocks so a mid-conversation switch
        // to Anthropic does not 400. Unchanged bodies are forwarded verbatim.
        let outgoing = match sanitize_anthropic_request(&passthrough.raw_body, &req_id) {
            Some(bytes) => Body::from(bytes),
            None => Body::from(passthrough.raw_body),
        };
        let mut request = Request::new(outgoing);
        *request.method_mut() = Method::POST;
        *request.uri_mut() = passthrough
            .path_and_query
            .parse()
            .expect("original request URI");
        *request.headers_mut() = passthrough.headers;
        self.forward(request).await
    }

    async fn forward(&self, mut request: Request<Body>) -> Response {
        if request.method() == Method::CONNECT {
            return json_error(
                StatusCode::METHOD_NOT_ALLOWED,
                "invalid_request_error",
                "CONNECT is not an account endpoint",
            );
        }
        let websocket = request.method() == Method::GET
            && request
                .headers()
                .get("upgrade")
                .and_then(|v| v.to_str().ok())
                .is_some_and(|v| v.eq_ignore_ascii_case("websocket"))
            && request
                .headers()
                .get_all("connection")
                .iter()
                .filter_map(|v| v.to_str().ok())
                .flat_map(|v| v.split(','))
                .any(|v| v.trim().eq_ignore_ascii_case("upgrade"));
        let downstream_upgrade = websocket.then(|| hyper::upgrade::on(&mut request));
        // Never use the incoming Host or an absolute-form target as the destination.
        let path = request
            .uri()
            .path_and_query()
            .map(|p| p.as_str())
            .unwrap_or("/");
        let url = format!("{}{path}", self.base_url);
        let headers = relay_headers(request.headers(), true, websocket);
        let (parts, body) = request.into_parts();
        let mut outgoing = self.client.request(parts.method, url).headers(headers);
        if let Some(length) = body.size_hint().exact() {
            outgoing = outgoing.header(http::header::CONTENT_LENGTH, length);
        }
        if websocket {
            outgoing = outgoing.version(http::Version::HTTP_11);
        }
        let upstream = match outgoing
            .body(reqwest::Body::wrap(http_body_util::StreamBody::new(
                sync_wrapper::SyncStream::new(http_body_util::BodyStream::new(body)),
            )))
            .send()
            .await
        {
            Ok(response) => response,
            Err(_) => {
                return json_error(
                    StatusCode::BAD_GATEWAY,
                    "api_error",
                    "Anthropic upstream connection failed",
                );
            }
        };
        let status = upstream.status();
        let upgraded = status == StatusCode::SWITCHING_PROTOCOLS;
        if upgraded && !websocket {
            return json_error(
                StatusCode::BAD_GATEWAY,
                "api_error",
                "Unexpected upstream upgrade",
            );
        }
        let headers = relay_headers(upstream.headers(), false, upgraded);
        let mut response = if upgraded {
            let downstream_upgrade = downstream_upgrade.expect("requested websocket upgrade");
            tokio::spawn(async move {
                if let (Ok(client), Ok(mut server)) =
                    tokio::join!(downstream_upgrade, upstream.upgrade())
                {
                    let mut client = hyper_util::rt::TokioIo::new(client);
                    let _ = tokio::io::copy_bidirectional(&mut client, &mut server).await;
                }
            });
            Response::new(Body::empty())
        } else {
            let upstream: http::Response<reqwest::Body> = upstream.into();
            Response::new(Body::new(upstream.into_body()))
        };
        *response.status_mut() = status;
        *response.headers_mut() = headers;
        response
    }
}

impl Default for AnthropicProvider {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Provider for AnthropicProvider {
    fn name(&self) -> &'static str {
        "anthropic"
    }

    fn supports_message_threads(&self) -> bool {
        true
    }

    async fn handle_account_request(&self, request: Request<Body>) -> Response {
        self.forward(request).await
    }

    fn supported_models(&self) -> Vec<String> {
        ANTHROPIC_STYLE_ALIASES
            .iter()
            .map(|alias| (*alias).to_string())
            .collect()
    }

    fn cli(&self) -> &'static dyn CliHandlers {
        &ANTHROPIC_CLI
    }

    async fn handle_messages(&self, _body: MessagesRequest, ctx: RequestContext) -> Response {
        self.relay(ctx).await
    }

    async fn handle_count_tokens(&self, _body: MessagesRequest, ctx: RequestContext) -> Response {
        self.relay(ctx).await
    }
}

pub struct AnthropicCli;
pub static ANTHROPIC_CLI: AnthropicCli = AnthropicCli;

impl CliHandlers for AnthropicCli {
    fn login(&self) -> anyhow::Result<()> {
        anyhow::bail!(
            "The Claude backend reuses Claude Code's own login; no separate authentication is required"
        )
    }
    fn device(&self) -> anyhow::Result<()> {
        anyhow::bail!(
            "The Claude backend reuses Claude Code's own login; no separate authentication is required"
        )
    }
    fn status(&self) -> anyhow::Result<()> {
        println!("Claude backend: transparent passthrough to api.anthropic.com");
        println!("Auth: forwarded from Claude Code (no proxy credentials stored)");
        Ok(())
    }
    fn logout(&self) -> anyhow::Result<()> {
        println!("Claude backend stores no credentials; nothing to remove");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::providers::translate_shared::{REASONING_CLOSE, REASONING_OPEN};

    #[test]
    fn strips_hop_by_hop_and_encoding_from_request() {
        assert!(is_stripped_request_header("host"));
        assert!(is_stripped_request_header("content-length"));
        assert!(!is_stripped_request_header("accept-encoding"));
        assert!(is_stripped_request_header(
            crate::inbound_auth::PROXY_KEY_HEADER
        ));
        assert!(is_stripped_request_header("connection"));
        // credentials and cache-relevant headers must survive
        assert!(!is_stripped_request_header("authorization"));
        assert!(!is_stripped_request_header("anthropic-beta"));
        assert!(!is_stripped_request_header("anthropic-version"));
        assert!(!is_stripped_request_header("content-type"));
    }

    #[test]
    fn strips_framing_from_response() {
        assert!(!is_stripped_response_header("content-length"));
        assert!(!is_stripped_response_header("content-encoding"));
        assert!(is_stripped_response_header("transfer-encoding"));
        // rate-limit and request-id headers must reach Claude Code
        assert!(!is_stripped_response_header("content-type"));
        assert!(!is_stripped_response_header("request-id"));
        assert!(!is_stripped_response_header(
            "anthropic-ratelimit-requests-remaining"
        ));
    }

    #[test]
    fn provider_reports_name_and_models() {
        let provider = AnthropicProvider::new();
        assert_eq!(provider.name(), "anthropic");
        assert!(provider.supported_models().iter().any(|m| m == "opus"));
    }

    fn parse(bytes: &[u8]) -> Value {
        serde_json::from_slice(bytes).unwrap()
    }

    #[test]
    fn unsigned_thinking_becomes_tagged_text() {
        let body = serde_json::json!({
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "codex reasoning", "signature": ""},
                    {"type": "text", "text": "391"}
                ]}
            ]
        });
        let raw = serde_json::to_vec(&body).unwrap();
        let out = sanitize_anthropic_request(&raw, "req1").expect("should rewrite");
        let doc = parse(&out);
        let blocks = doc["messages"][1]["content"].as_array().unwrap();
        // the thinking block is gone, replaced by tagged text; the real answer survives
        assert!(blocks.iter().all(|b| b["type"] != "thinking"));
        let tagged = blocks[0]["text"].as_str().unwrap();
        assert!(tagged.starts_with(REASONING_OPEN), "{tagged}");
        assert!(tagged.contains("codex reasoning"), "{tagged}");
        assert!(tagged.ends_with(REASONING_CLOSE), "{tagged}");
        assert_eq!(blocks[1]["text"], "391");
    }

    #[test]
    fn signed_thinking_is_forwarded_verbatim() {
        // A genuine Anthropic reasoning block (non-empty signature) must not be touched,
        // so a pure-Anthropic conversation keeps its byte-identical cache prefix.
        let body = serde_json::json!({
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "opus reasoning", "signature": "abc123"}
                ]}
            ]
        });
        let raw = serde_json::to_vec(&body).unwrap();
        assert!(sanitize_anthropic_request(&raw, "req2").is_none());
    }

    #[test]
    fn missing_signature_is_treated_as_unsigned() {
        let body = serde_json::json!({
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "r"}
                ]}
            ]
        });
        let raw = serde_json::to_vec(&body).unwrap();
        let out = sanitize_anthropic_request(&raw, "req3").expect("should rewrite");
        assert_eq!(parse(&out)["messages"][0]["content"][0]["type"], "text");
    }

    #[test]
    fn plain_request_is_forwarded_verbatim() {
        let body = serde_json::json!({
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
            ]
        });
        let raw = serde_json::to_vec(&body).unwrap();
        assert!(sanitize_anthropic_request(&raw, "req4").is_none());
    }

    #[test]
    fn rewrite_is_deterministic() {
        let body = serde_json::json!({
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "same", "signature": ""}
                ]}
            ]
        });
        let raw = serde_json::to_vec(&body).unwrap();
        let a = sanitize_anthropic_request(&raw, "r").unwrap();
        let b = sanitize_anthropic_request(&raw, "r").unwrap();
        assert_eq!(
            a, b,
            "rewrite must be byte-stable to preserve the cache prefix"
        );
    }

    #[test]
    fn non_json_body_is_forwarded_verbatim() {
        assert!(sanitize_anthropic_request(b"not json", "req5").is_none());
    }
}
