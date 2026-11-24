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
use crate::registry::ANTHROPIC_STYLE_ALIASES;

/// How long a model list Anthropic gave a signed-in caller stays fresh.
pub const LIVE_MODELS_TTL: std::time::Duration = std::time::Duration::from_secs(600);

static LIVE_MODELS: std::sync::RwLock<Option<(std::time::Instant, Vec<String>)>> =
    std::sync::RwLock::new(None);

/// The Claude model ids Anthropic last listed; `None` until a signed-in caller asked.
pub fn live_models() -> Option<Vec<String>> {
    LIVE_MODELS
        .read()
        .ok()?
        .as_ref()
        .map(|(_, ids)| ids.clone())
}

pub fn live_models_stale() -> bool {
    LIVE_MODELS
        .read()
        .map(|live| {
            live.as_ref()
                .is_none_or(|(at, _)| at.elapsed() >= LIVE_MODELS_TTL)
        })
        .unwrap_or(true)
}

/// Keep the ids of an Anthropic `/v1/models` reply. A reply without any keeps the previous list.
pub fn record_live_models(body: &[u8]) -> bool {
    let ids: Vec<String> = serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|reply| reply.get("data")?.as_array().cloned())
        .unwrap_or_default()
        .iter()
        .filter_map(|model| model.get("id")?.as_str())
        .filter(|id| id.starts_with("claude-"))
        .map(str::to_string)
        .collect();
    if ids.is_empty() {
        return false;
    }
    if let Ok(mut live) = LIVE_MODELS.write() {
        *live = Some((std::time::Instant::now(), ids));
    }
    true
}

/// Rewrite an outgoing Anthropic request body so it survives a mid-conversation switch
/// away from another backend.
///
/// Claude Code stores another backend's reasoning as `thinking` blocks whose signature
/// is empty or a proxy `ccp:` marker. Anthropic rejects those on replay (400
/// `Invalid signature in thinking block`), and replaying them as visible assistant text
/// can trip Opus's `reasoning_extraction` refusal, so they are dropped. Genuine Anthropic
/// `thinking` and `redacted_thinking` blocks, and all text and tool blocks, stay as sent.
/// An assistant turn left empty is removed; Anthropic merges the neighbours it leaves
/// adjacent.
///
/// Returns rewritten bytes only when something changed; `None` forwards the body
/// verbatim, keeping the byte-identical cache prefix for pure-Anthropic conversations.
fn sanitize_anthropic_request(raw: &[u8], req_id: &str) -> Option<Vec<u8>> {
    let mut doc: Value = serde_json::from_slice(raw).ok()?;
    let obj = doc.as_object_mut()?;

    detect_hosted_web_search_regression(obj, req_id);

    let messages = obj.get_mut("messages")?.as_array_mut()?;
    let mut changed = false;
    messages.retain_mut(|message| {
        if message.get("role").and_then(Value::as_str) != Some("assistant") {
            return true;
        }
        let Some(content) = message.get_mut("content").and_then(Value::as_array_mut) else {
            return true;
        };
        let before = content.len();
        content.retain(|block| !is_foreign_thinking(block));
        let mut rewritten = content.len() != before;
        for block in content.iter_mut() {
            let foreign_search = block.get("type").and_then(Value::as_str)
                == Some("x_search_tool_result")
                || (block.get("type").and_then(Value::as_str) == Some("server_tool_use")
                    && block.get("name").and_then(Value::as_str) == Some("x_search"));
            if foreign_search
                && let Some(text) = crate::providers::translate_shared::public_history_text(block)
            {
                *block = serde_json::json!({"type": "text", "text": text});
                rewritten = true;
            }
        }
        changed |= rewritten;
        !rewritten || !content.is_empty()
    });

    changed.then(|| serde_json::to_vec(&doc).unwrap_or_else(|_| raw.to_vec()))
}

/// A `thinking` block Anthropic did not sign: no signature, an empty one, or a proxy
/// marker from another backend.
fn is_foreign_thinking(block: &Value) -> bool {
    if block.get("type").and_then(Value::as_str) != Some("thinking") {
        return false;
    }
    block
        .get("signature")
        .and_then(Value::as_str)
        .is_none_or(|sig| {
            sig.is_empty() || crate::providers::translate_shared::ProxyReasoning::of(sig).is_some()
        })
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
            | crate::fallback::POLICY_HEADER
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
    /// WebSocket upgrades exist only in HTTP/1.1; an ALPN-negotiated h2 connection
    /// would silently drop `Upgrade` and send a plain GET.
    upgrade_client: reqwest::Client,
    base_url: String,
}

impl AnthropicProvider {
    pub fn new() -> Self {
        Self::with_base_url(crate::config::anthropic_base_url())
    }

    /// Select the fixed account origin (also used by integration tests).
    pub fn with_base_url(base_url: impl Into<String>) -> Self {
        Self::build(base_url.into(), false)
    }

    /// Loopback broker relay: ignores ambient and explicit proxies, bounded timeouts.
    pub fn with_loopback_base_url(base_url: impl Into<String>) -> Self {
        Self::build(base_url.into(), true)
    }

    fn build(base_url: String, direct: bool) -> Self {
        let proxy = (!direct)
            .then(crate::config::anthropic_upstream_proxy)
            .flatten();
        Self::from_builder(base_url, move || {
            let mut builder = reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .no_zstd();
            if direct {
                builder = builder
                    .no_proxy()
                    .connect_timeout(std::time::Duration::from_secs(5))
                    .read_timeout(std::time::Duration::from_secs(120));
            } else if let Some((proxy, ca)) = &proxy {
                let pem = std::fs::read(ca).expect("failed to read CCP_ANTHROPIC_PROXY_CA");
                builder = builder
                    .proxy(reqwest::Proxy::https(proxy).expect("invalid CCP_ANTHROPIC_PROXY"))
                    .add_root_certificate(
                        reqwest::Certificate::from_pem(&pem)
                            .expect("invalid CCP_ANTHROPIC_PROXY_CA"),
                    );
            }
            builder
        })
    }

    fn from_builder(base_url: String, builder: impl Fn() -> reqwest::ClientBuilder) -> Self {
        let build = |builder: reqwest::ClientBuilder| {
            builder
                .build()
                .expect("failed to build anthropic passthrough client")
        };
        Self {
            client: build(builder()),
            upgrade_client: build(builder().http1_only()),
            base_url: base_url.trim_end_matches('/').to_string(),
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

        // Drop another backend's `thinking` blocks so a mid-conversation switch to
        // Anthropic does not 400. Unchanged bodies are forwarded verbatim.
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
        let client = if websocket {
            &self.upgrade_client
        } else {
            &self.client
        };
        let mut outgoing = client.request(parts.method, url).headers(headers);
        if let Some(length) = body.size_hint().exact() {
            outgoing = outgoing.header(http::header::CONTENT_LENGTH, length);
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
    fn preserves_native_tool_documents(&self) -> bool {
        true
    }

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
    use crate::providers::translate_shared::ProxyReasoning;

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

    fn sanitize(body: Value) -> Value {
        let raw = serde_json::to_vec(&body).unwrap();
        parse(&sanitize_anthropic_request(&raw, "req").expect("should rewrite"))
    }

    const CODEX_SIG: &str = "ccp:codex:v1:abc";
    const KIMI_B64_SIG: &str = "Y2NwOmtpbWk6djE6bXNnXzE6MA";

    #[test]
    fn foreign_and_unsigned_thinking_are_dropped_not_turned_into_text() {
        let mut signatures = vec![serde_json::json!(""), Value::Null];
        for origin in ProxyReasoning::ALL {
            signatures.push(serde_json::json!(origin.sign("msg_1", 0)));
            signatures.push(serde_json::json!(format!("{}msg_1:0", origin.prefix())));
        }
        for signature in signatures {
            let thinking = serde_json::json!({"type": "thinking", "thinking": "foreign r", "signature": signature});
            let out = sanitize(serde_json::json!({"messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [thinking.clone(), {"type": "text", "text": "391"}]}
            ]}));
            assert_eq!(
                out["messages"][1]["content"],
                serde_json::json!([{"type": "text", "text": "391"}]),
                "{thinking}"
            );
            assert!(!out.to_string().contains("foreign r"), "{thinking}");
        }
    }

    #[test]
    fn mixed_turn_keeps_genuine_reasoning_and_tool_flow_in_order() {
        let signed =
            serde_json::json!({"type": "thinking", "thinking": "opus r", "signature": "EqQBsig=="});
        let redacted = serde_json::json!({"type": "redacted_thinking", "data": "EmwKAhgBEgy="});
        let tool_use = serde_json::json!({"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}});
        let tool_result = serde_json::json!({"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"});
        let out = sanitize(serde_json::json!({"messages": [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": [
                signed.clone(),
                {"type": "thinking", "thinking": "codex r", "signature": CODEX_SIG},
                redacted.clone(),
                {"type": "text", "text": "Listing."},
                {"type": "thinking", "thinking": "kimi r", "signature": KIMI_B64_SIG},
                tool_use.clone()
            ]},
            {"role": "user", "content": [tool_result.clone()]}
        ]}));
        assert_eq!(
            out["messages"],
            serde_json::json!([
                {"role": "user", "content": "list files"},
                {"role": "assistant", "content": [
                    signed, redacted, {"type": "text", "text": "Listing."}, tool_use
                ]},
                {"role": "user", "content": [tool_result]}
            ])
        );
    }

    #[test]
    fn assistant_turn_left_empty_is_removed() {
        let out = sanitize(serde_json::json!({"messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "codex r", "signature": CODEX_SIG}
            ]},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "unsigned r", "signature": ""}
            ]}
        ]}));
        assert_eq!(
            out["messages"],
            serde_json::json!([
                {"role": "user", "content": "first"},
                {"role": "user", "content": "second"},
                {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
                {"role": "user", "content": "third"}
            ])
        );
    }

    #[test]
    fn ordinary_text_mentioning_reasoning_tags_is_left_alone() {
        let body = serde_json::json!({"messages": [
            {"role": "user", "content": [{"type": "text", "text": "<previous_reasoning>x</previous_reasoning>"}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "opus r", "signature": "EqQBsig=="},
                {"type": "redacted_thinking", "data": "EmwKAhgBEgy="},
                {"type": "text", "text": "<previous_reasoning>y</previous_reasoning>"}
            ]}
        ]});
        let raw = serde_json::to_vec(&body).unwrap();
        assert!(sanitize_anthropic_request(&raw, "req").is_none());
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
    fn native_signed_reasoning_and_hosted_web_search_stay_byte_exact() {
        let raw = br#"{ "model": "claude-opus-4-7", "messages": [{"role":"assistant", "content":[
            {"type":"thinking","thinking":"native","signature":"EqQBsig=="},
            {"type":"redacted_thinking","data":"EmwKAhgBEgy="},
            {"type":"server_tool_use","id":"s","name":"web_search","input":{"query":"q"}},
            {"type":"web_search_tool_result","tool_use_id":"s","content":[{"type":"web_search_result","url":"https://example.invalid","title":"result"}]},
            {"type":"text","text":"answer"}
        ]}] }"#;
        assert!(sanitize_anthropic_request(raw, "native").is_none());
    }

    #[test]
    fn non_json_body_is_forwarded_verbatim() {
        assert!(sanitize_anthropic_request(b"not json", "req5").is_none());
    }

    #[tokio::test]
    async fn websocket_upgrade_stays_http1_when_the_origin_offers_h2() {
        use std::sync::Arc;
        let cert = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let root = reqwest::Certificate::from_pem(cert.cert.pem().as_bytes()).unwrap();
        let mut tls = rustls::ServerConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(
            vec![cert.cert.der().clone()],
            rustls::pki_types::PrivateKeyDer::Pkcs8(cert.key_pair.serialize_der().into()),
        )
        .unwrap();
        tls.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(tls));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let origin = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let stream = acceptor.accept(tcp).await.unwrap();
            if stream.get_ref().1.alpn_protocol() == Some(b"http/1.1") {
                let _ = tokio_tungstenite::accept_async(stream).await;
            }
        });
        let provider = AnthropicProvider::from_builder(format!("https://localhost:{port}"), || {
            reqwest::Client::builder().add_root_certificate(root.clone())
        });
        let request = Request::builder()
            .method(Method::GET)
            .uri("/api/frame/sync?slug=toy")
            .header("connection", "Upgrade")
            .header("upgrade", "websocket")
            .header("sec-websocket-version", "13")
            .header("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ==")
            .body(Body::empty())
            .unwrap();
        let response = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            provider.handle_account_request(request),
        )
        .await
        .unwrap();
        assert_eq!(response.status(), StatusCode::SWITCHING_PROTOCOLS);
        origin.abort();
    }
}
