//! Z.ai GLM backend: Z.ai serves the Anthropic Messages API itself, so the request is
//! relayed as JSON with only the Z.ai API key attached. The caller's Claude credentials,
//! cookies and session headers never leave the proxy.

use async_trait::async_trait;
use axum::body::Body;
use axum::response::Response;
use bytes::Bytes;
use futures_util::StreamExt;
use http::{HeaderValue, StatusCode};
use serde_json::Value;

use crate::anthropic::error::json_error;
use crate::anthropic::schema::MessagesRequest;
use crate::logging::create_logger;
use crate::provider::{Availability, CliHandlers, Provider, RequestContext};
use crate::providers::translate_shared::ProxyReasoning;

pub const DEFAULT_BASE_URL: &str = "https://api.z.ai/api/anthropic";

/// Ids Z.ai lists at `/v1/models` and answered for the owner's key on 2026-11-24.
/// Any other `glm-` id still routes here (`registry::provider_for_model`).
pub const ZAI_MODELS: &[&str] = &[
    "glm-4.5",
    "glm-4.5-air",
    "glm-4.6",
    "glm-4.7",
    "glm-5",
    "glm-5-turbo",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.4",
];

/// Cooldown for a rejected key, a spent balance or a model outside the plan.
const UNUSABLE_RETRY_AFTER: &str = "300";
const RATE_LIMIT_RETRY_AFTER: &str = "5";

pub fn api_key() -> Option<String> {
    read_key(&key_path())
}

fn read_key(path: &std::path::Path) -> Option<String> {
    let key = std::fs::read_to_string(path).ok()?.trim().to_string();
    (!key.is_empty()).then_some(key)
}

pub struct ZaiProvider {
    client: reqwest::Client,
    base_url: String,
}

impl Default for ZaiProvider {
    fn default() -> Self {
        Self::new()
    }
}

impl ZaiProvider {
    pub fn new() -> Self {
        Self::with_base_url(crate::config::zai_base_url())
    }

    pub fn with_base_url(base_url: impl Into<String>) -> Self {
        Self {
            client: reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(10))
                .read_timeout(std::time::Duration::from_secs(300))
                .build()
                .expect("failed to build Z.ai client"),
            base_url: base_url.into().trim_end_matches('/').to_string(),
        }
    }

    async fn relay(&self, path: &str, body: MessagesRequest, ctx: RequestContext) -> Response {
        let Some(key) = api_key() else {
            return unusable("no Z.ai API key is configured");
        };
        let mut request = match serde_json::to_value(&body) {
            Ok(value) => value,
            Err(error) => {
                return json_error(
                    StatusCode::BAD_REQUEST,
                    "invalid_request_error",
                    error.to_string(),
                );
            }
        };
        prepare_request(&mut request);
        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.upstream_started(&ctx.req_id);
        }
        let headers = ctx.passthrough.as_ref().map(|p| &p.headers);
        let header = |name: &str| headers.and_then(|h| h.get(name)).cloned();
        let mut outgoing = self
            .client
            .post(format!("{}{path}", self.base_url))
            .header("x-api-key", key)
            .header(
                "anthropic-version",
                header("anthropic-version").unwrap_or(HeaderValue::from_static("2023-06-01")),
            )
            .json(&request);
        if let Some(beta) = header("anthropic-beta") {
            outgoing = outgoing.header("anthropic-beta", beta);
        }
        let upstream = match outgoing.send().await {
            Ok(response) => response,
            Err(_) => {
                return json_error(
                    StatusCode::BAD_GATEWAY,
                    "api_error",
                    "Z.ai upstream connection failed",
                );
            }
        };
        let status = upstream.status();
        if !status.is_success() {
            let retry_after = upstream.headers().get(http::header::RETRY_AFTER).cloned();
            let text = upstream.text().await.unwrap_or_default();
            return upstream_error(status, &text, retry_after, &ctx.req_id);
        }
        let content_type = upstream
            .headers()
            .get(http::header::CONTENT_TYPE)
            .cloned()
            .unwrap_or(HeaderValue::from_static("application/json"));
        let streamed = content_type
            .to_str()
            .is_ok_and(|value| value.starts_with("text/event-stream"));
        let body = if streamed {
            let mut lines = LineMarker::default();
            let stream = upstream.bytes_stream().map(move |chunk| {
                chunk
                    .map(|bytes| lines.push(&bytes))
                    .map_err(std::io::Error::other)
            });
            Body::from_stream(stream)
        } else {
            match upstream.bytes().await {
                Ok(bytes) => Body::from(mark_json(&bytes)),
                Err(_) => {
                    return json_error(
                        StatusCode::BAD_GATEWAY,
                        "api_error",
                        "Z.ai upstream response was interrupted",
                    );
                }
            }
        };
        let mut response = Response::new(body);
        *response.status_mut() = status;
        response
            .headers_mut()
            .insert(http::header::CONTENT_TYPE, content_type);
        response
    }
}

/// Z.ai's own reasoning signatures go back to it unwrapped; `max_tokens` is omitted when unset.
fn prepare_request(request: &mut Value) {
    if let Some(object) = request.as_object_mut()
        && object.get("max_tokens").is_some_and(Value::is_null)
    {
        object.remove("max_tokens");
    }
    let Some(messages) = request.get_mut("messages").and_then(Value::as_array_mut) else {
        return;
    };
    let prefix = ProxyReasoning::Zai.prefix();
    for block in messages
        .iter_mut()
        .filter_map(|message| message.get_mut("content")?.as_array_mut())
        .flatten()
    {
        if let Some(Value::String(signature)) = block.get_mut("signature")
            && let Some(own) = signature.strip_prefix(prefix)
        {
            *signature = own.to_string();
        }
    }
}

/// Mark Z.ai's reasoning signatures as this proxy's, so Anthropic never receives one
/// (it rejects a signature it did not issue) and Z.ai gets its own back.
fn mark_signatures(value: &mut Value) {
    match value {
        Value::Object(object) => {
            let signed = matches!(
                object.get("type").and_then(Value::as_str),
                Some("thinking" | "signature_delta")
            );
            for (key, child) in object.iter_mut() {
                match child {
                    Value::String(signature)
                        if signed && key == "signature" && !signature.is_empty() =>
                    {
                        *signature = format!("{}{signature}", ProxyReasoning::Zai.prefix());
                    }
                    _ => mark_signatures(child),
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(mark_signatures),
        _ => {}
    }
}

fn mark_json(bytes: &[u8]) -> Bytes {
    match serde_json::from_slice::<Value>(bytes) {
        Ok(mut value) => {
            mark_signatures(&mut value);
            serde_json::to_vec(&value).map_or_else(|_| Bytes::copy_from_slice(bytes), Bytes::from)
        }
        Err(_) => Bytes::copy_from_slice(bytes),
    }
}

/// Rewrites signed SSE `data:` lines as they complete; everything else passes through.
#[derive(Default)]
struct LineMarker {
    pending: Vec<u8>,
}

impl LineMarker {
    fn push(&mut self, chunk: &[u8]) -> Bytes {
        self.pending.extend_from_slice(chunk);
        let Some(end) = self.pending.iter().rposition(|byte| *byte == b'\n') else {
            return Bytes::new();
        };
        let complete: Vec<u8> = self.pending.drain(..=end).collect();
        let mut out = Vec::with_capacity(complete.len());
        for line in complete.split_inclusive(|byte| *byte == b'\n') {
            out.extend_from_slice(&mark_line(line));
        }
        Bytes::from(out)
    }
}

fn mark_line(line: &[u8]) -> Vec<u8> {
    let Some(data) = line.strip_prefix(b"data:") else {
        return line.to_vec();
    };
    if !data.windows(11).any(|window| window == b"\"signature\"") {
        return line.to_vec();
    }
    let ending = if line.ends_with(b"\r\n") {
        "\r\n"
    } else {
        "\n"
    };
    match serde_json::from_slice::<Value>(data) {
        Ok(mut value) => {
            mark_signatures(&mut value);
            format!("data: {value}{ending}").into_bytes()
        }
        Err(_) => line.to_vec(),
    }
}

/// Z.ai reports quota, plan and balance trouble as 429 (codes 1113, 1309, 1311) and a bad
/// key as 401. All of them become a plain 429 so the fallback chain answers instead;
/// the vendor's billing text is logged, never shown to the client.
fn upstream_error(
    status: StatusCode,
    text: &str,
    retry_after: Option<HeaderValue>,
    req_id: &str,
) -> Response {
    let code =
        serde_json::from_str::<Value>(text)
            .ok()
            .and_then(|reply| match &reply["error"]["code"] {
                Value::String(code) => Some(code.clone()),
                Value::Number(code) => Some(code.to_string()),
                _ => None,
            });
    let unusable = matches!(status.as_u16(), 401..=403)
        || matches!(code.as_deref(), Some("1113" | "1309" | "1311"));
    if !unusable && status != StatusCode::TOO_MANY_REQUESTS {
        if status.is_server_error() {
            return json_error(StatusCode::BAD_GATEWAY, "api_error", "Z.ai upstream error");
        }
        let mut response = Response::new(Body::from(text.to_string()));
        *response.status_mut() = status;
        response.headers_mut().insert(
            http::header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        );
        return response;
    }
    let mut fields = serde_json::Map::new();
    fields.insert("reqId".into(), Value::String(req_id.to_string()));
    fields.insert("status".into(), Value::from(status.as_u16()));
    fields.insert(
        "zaiError".into(),
        code.clone().map_or(Value::Null, Value::String),
    );
    create_logger("zai").warn("zai unavailable", Some(fields));
    let mut response = if unusable {
        unusable_response("Z.ai GLM is unavailable for this key (credential, plan or balance)")
    } else {
        json_error(
            StatusCode::TOO_MANY_REQUESTS,
            "rate_limit_error",
            "Z.ai GLM is rate limited",
        )
    };
    let retry_after = match (unusable, retry_after) {
        (false, Some(value)) => value,
        (false, None) => HeaderValue::from_static(RATE_LIMIT_RETRY_AFTER),
        (true, _) => HeaderValue::from_static(UNUSABLE_RETRY_AFTER),
    };
    response
        .headers_mut()
        .insert(http::header::RETRY_AFTER, retry_after);
    response
}

fn unusable_response(message: &str) -> Response {
    json_error(StatusCode::TOO_MANY_REQUESTS, "rate_limit_error", message)
}

fn unusable(message: &str) -> Response {
    let mut response = unusable_response(message);
    response.headers_mut().insert(
        http::header::RETRY_AFTER,
        HeaderValue::from_static(UNUSABLE_RETRY_AFTER),
    );
    response
}

#[async_trait]
impl Provider for ZaiProvider {
    fn name(&self) -> &'static str {
        "zai"
    }

    fn supported_models(&self) -> Vec<String> {
        ZAI_MODELS
            .iter()
            .map(|model| (*model).to_string())
            .collect()
    }

    fn cli(&self) -> &'static dyn CliHandlers {
        &ZAI_CLI
    }

    fn availability(&self) -> Availability {
        Availability::from_file(
            &key_path(),
            format!("put a Z.ai API key in {}", key_path().display()),
        )
    }

    async fn handle_messages(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        self.relay("/v1/messages", body, ctx).await
    }

    async fn handle_count_tokens(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        self.relay("/v1/messages/count_tokens", body, ctx).await
    }
}

fn key_path() -> std::path::PathBuf {
    crate::paths::zai_api_key_file(&crate::paths::DirResolverEnv::default())
}

pub struct ZaiCli;
pub static ZAI_CLI: ZaiCli = ZaiCli;

impl CliHandlers for ZaiCli {
    fn login(&self) -> anyhow::Result<()> {
        anyhow::bail!(
            "Z.ai uses an API key: write it to {} (mode 600)",
            key_path().display()
        )
    }
    fn device(&self) -> anyhow::Result<()> {
        self.login()
    }
    fn status(&self) -> anyhow::Result<()> {
        println!("Key path: {}", key_path().display());
        println!("Configured: {}", api_key().is_some());
        Ok(())
    }
    fn logout(&self) -> anyhow::Result<()> {
        anyhow::bail!("Delete {} to remove the Z.ai key", key_path().display())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn own_signatures_round_trip_and_mark_as_proxy_reasoning() {
        let mut reply = json!({"content": [
            {"type": "thinking", "thinking": "t", "signature": "aa7681f1ff8c44bc"},
            {"type": "text", "text": "pong", "signature": "untouched"}
        ]});
        mark_signatures(&mut reply);
        let marked = reply["content"][0]["signature"].as_str().unwrap();
        assert_eq!(ProxyReasoning::of(marked), Some(ProxyReasoning::Zai));
        assert_eq!(reply["content"][1]["signature"], "untouched");

        let mut request = json!({"model": "glm-4.6", "max_tokens": null, "messages": [
            {"role": "assistant", "content": [reply["content"][0].clone()]}
        ]});
        prepare_request(&mut request);
        assert_eq!(
            request["messages"][0]["content"][0]["signature"],
            "aa7681f1ff8c44bc"
        );
        assert!(request.get("max_tokens").is_none());
    }

    #[test]
    fn sse_signature_lines_are_marked_across_chunk_boundaries() {
        let mut marker = LineMarker::default();
        let first = marker.push(b"event: content_block_delta\ndata: {\"type\": \"content_block_delta\", \"index\": 0, \"delta\": {\"type\": \"signature_delta\", \"sig");
        let second = marker.push(b"nature\": \"abc\"}}\n\ndata: {\"type\": \"ping\"}\n");
        let text = String::from_utf8([first, second].concat()).unwrap();
        assert!(text.starts_with("event: content_block_delta\n"));
        let data: Value = serde_json::from_str(
            text.lines()
                .find(|line| line.contains("signature"))
                .unwrap()
                .strip_prefix("data: ")
                .unwrap(),
        )
        .unwrap();
        assert_eq!(data["delta"]["signature"], "ccp:zai:v1:abc");
        assert!(text.ends_with("\n\ndata: {\"type\": \"ping\"}\n"));
    }

    async fn error_parts(status: u16, body: Value) -> (StatusCode, Option<String>, String) {
        let response = upstream_error(
            StatusCode::from_u16(status).unwrap(),
            &body.to_string(),
            None,
            "req",
        );
        let status = response.status();
        let retry = response
            .headers()
            .get(http::header::RETRY_AFTER)
            .map(|v| v.to_str().unwrap().to_string());
        let bytes = axum::body::to_bytes(response.into_body(), 1 << 16)
            .await
            .unwrap();
        (status, retry, String::from_utf8(bytes.to_vec()).unwrap())
    }

    #[tokio::test]
    async fn billing_and_credential_failures_become_a_plain_rate_limit() {
        for (status, code) in [(429, "1113"), (429, "1309"), (429, "1311"), (401, "401")] {
            let body = json!({"error": {"type": "rate_limit_error", "code": code,
                "message": "Insufficient balance or no resource package. Please recharge."}});
            let (status, retry, text) = error_parts(status, body).await;
            assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{code}");
            assert_eq!(retry.as_deref(), Some(UNUSABLE_RETRY_AFTER));
            assert!(!text.contains("recharge"), "{text}");
        }
        let (status, retry, _) = error_parts(429, json!({"error": {"code": "1302"}})).await;
        assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(retry.as_deref(), Some(RATE_LIMIT_RETRY_AFTER));
    }

    #[tokio::test]
    async fn invalid_requests_pass_through_and_server_errors_are_generic() {
        let (status, _, text) =
            error_parts(400, json!({"error": {"message": "prompt is too long"}})).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(text.contains("prompt is too long"));
        let (status, _, _) = error_parts(503, json!({})).await;
        assert_eq!(status, StatusCode::BAD_GATEWAY);
    }

    #[test]
    fn key_file_is_trimmed_and_empty_means_absent() {
        let dir = std::env::temp_dir().join(format!("ccp-zai-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(dir.join("zai")).unwrap();
        let deps = crate::paths::DirResolverEnv {
            platform: "linux".into(),
            home: "/nonexistent".into(),
            env: [("CCP_CONFIG_DIR".to_string(), dir.display().to_string())].into(),
        };
        let path = crate::paths::zai_api_key_file(&deps);
        assert_eq!(path, dir.join("zai").join("api_key"));
        std::fs::write(&path, "  secret-value\n").unwrap();
        assert_eq!(read_key(&path).as_deref(), Some("secret-value"));
        std::fs::write(&path, "\n").unwrap();
        assert_eq!(read_key(&path), None);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
