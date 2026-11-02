//! Optional loopback Anthropic Messages broker for `cursor:<id>` models.
//!
//! Enabled only by an explicit `CCP_CURSOR_MESSAGES_BASE_URL`. Requests are rebuilt
//! with the exact catalog id and a minimal header set, then relayed opaquely by
//! `AnthropicProvider`. A configured broker never falls back to the native path.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpStream};
use std::time::Duration;

use axum::body::Bytes;
use axum::response::Response;
use http::{HeaderMap, HeaderValue, StatusCode};
use serde_json::{Value, json};

use crate::anthropic::error::json_error;
use crate::anthropic::schema::MessagesRequest;
use crate::provider::{
    Availability, Generation, GenerationBody, Passthrough, Provider, ProviderError,
    ProviderErrorKind, RequestContext,
};
use crate::providers::anthropic::AnthropicProvider;
use crate::providers::cursor::model::{CursorAgentMode, resolve_cursor_model};

const BASE_URL_ENV: &str = "CCP_CURSOR_MESSAGES_BASE_URL";
const API_KEY_ENV: &str = "CCP_CURSOR_MESSAGES_API_KEY";
const SESSION_HEADER: &str = "x-agent-session-id";
const PROBE_BUDGET: Duration = Duration::from_millis(200);

pub struct Broker {
    origin: String,
    addrs: Vec<SocketAddr>,
    auth: Option<HeaderValue>,
    relay: AnthropicProvider,
}

/// `None` when unset or blank; `Some(Err)` when set but unusable.
pub fn from_env() -> Option<Result<Broker, String>> {
    let url = std::env::var(BASE_URL_ENV).ok()?;
    if url.trim().is_empty() {
        return None;
    }
    let key = std::env::var(API_KEY_ENV).ok().filter(|k| !k.is_empty());
    Some(Broker::new(&url, key.as_deref()))
}

pub fn config_error(message: &str) -> Response {
    json_error(
        StatusCode::BAD_REQUEST,
        "invalid_request_error",
        format!("Cursor Messages broker: {message}"),
    )
}

impl Broker {
    fn new(url: &str, key: Option<&str>) -> Result<Self, String> {
        let (origin, addrs) = parse_origin(url)?;
        let auth = key
            .map(|key| {
                HeaderValue::from_str(&format!("Bearer {key}"))
                    .map(|mut value| {
                        value.set_sensitive(true);
                        value
                    })
                    .map_err(|_| format!("{API_KEY_ENV} is not a valid header value"))
            })
            .transpose()?;
        Ok(Self {
            relay: AnthropicProvider::with_loopback_base_url(origin.clone()),
            origin,
            addrs,
            auth,
        })
    }

    pub fn availability(&self) -> Availability {
        let each = PROBE_BUDGET / self.addrs.len() as u32;
        if self
            .addrs
            .iter()
            .any(|addr| TcpStream::connect_timeout(addr, each).is_ok())
        {
            Availability::Ready
        } else {
            Availability::unavailable(format!(
                "Cursor Messages broker is not listening at {}",
                self.origin
            ))
        }
    }

    pub async fn messages(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        match self.prepare("/v1/messages", &body, ctx) {
            Ok(ctx) => self.relay.handle_messages(body, ctx).await,
            Err(message) => config_error(&message),
        }
    }

    pub async fn count_tokens(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        match self.prepare("/v1/messages/count_tokens", &body, ctx) {
            Ok(ctx) => self.relay.handle_count_tokens(body, ctx).await,
            Err(message) => config_error(&message),
        }
    }

    pub async fn generate(
        &self,
        mut body: MessagesRequest,
        ctx: RequestContext,
    ) -> Result<Generation, ProviderError> {
        body.stream = true;
        let monitor = ctx.monitor.clone();
        let req_id = ctx.req_id.clone();
        let ctx = self
            .prepare("/v1/messages", &body, ctx)
            .map_err(|message| {
                ProviderError::new(
                    StatusCode::BAD_REQUEST,
                    ProviderErrorKind::InvalidRequest,
                    message,
                )
            })?;
        let model = exact_model(body.model.as_deref()).expect("validated in prepare");
        if let Some(monitor) = monitor {
            monitor.model_resolved(&req_id, &model);
        }
        let response = self.relay.handle_messages(body, ctx).await;
        if !response.status().is_success() {
            return Err(provider_error(response).await);
        }
        Ok(Generation {
            body: GenerationBody::LiveSse(response.into_body()),
            resolved_model: model,
        })
    }

    /// Validate the request and swap in the broker-bound passthrough material.
    fn prepare(
        &self,
        path: &str,
        body: &MessagesRequest,
        mut ctx: RequestContext,
    ) -> Result<RequestContext, String> {
        let model = exact_model(body.model.as_deref())?;
        let raw = ctx.passthrough.as_ref().map(|p| p.raw_body.as_ref());
        let raw_body = upstream_body(raw, body, &model)?;
        ctx.passthrough = Some(Passthrough {
            raw_body,
            headers: self.headers(ctx.session_id.as_deref()),
            path_and_query: path.to_string(),
        });
        Ok(ctx)
    }

    fn headers(&self, session_id: Option<&str>) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert("content-type", HeaderValue::from_static("application/json"));
        headers.insert("anthropic-version", HeaderValue::from_static("2023-06-01"));
        if let Some(auth) = &self.auth {
            headers.insert("authorization", auth.clone());
        }
        if let Some(id) = session_id.and_then(|id| HeaderValue::from_str(id).ok()) {
            headers.insert(SESSION_HEADER, id);
        }
        headers
    }
}

/// The catalog id after `cursor:`; modes other than agent and fast are refused.
fn exact_model(requested: Option<&str>) -> Result<String, String> {
    let requested = requested.unwrap_or("cursor").trim();
    let resolved = resolve_cursor_model(requested)?;
    if resolved.mode != CursorAgentMode::Agent {
        return Err("ask and plan modes are not supported; use cursor:<id>".into());
    }
    if resolved.fast {
        return Err("fast mode is not supported; use the exact cursor:<id>".into());
    }
    if !requested.contains(':') || resolved.model_id.is_empty() {
        return Err(format!(
            "\"{requested}\" has no default id; request an explicit cursor:<catalog-id>"
        ));
    }
    Ok(resolved.model_id)
}

/// Prefer the client's raw JSON so unknown fields keep their exact shape.
fn upstream_body(raw: Option<&[u8]>, body: &MessagesRequest, model: &str) -> Result<Bytes, String> {
    let mut doc = match raw.and_then(|raw| serde_json::from_slice::<Value>(raw).ok()) {
        Some(doc) if doc.is_object() => doc,
        _ => {
            let mut doc = serde_json::to_value(body).map_err(|e| e.to_string())?;
            let map = doc.as_object_mut().ok_or("request is not an object")?;
            map.retain(|k, v| match k.as_str() {
                "max_tokens" => !v.is_null(),
                "stream" => *v != json!(false),
                _ => true,
            });
            doc
        }
    };
    if body.stream {
        doc["stream"] = json!(true);
    }
    doc["model"] = json!(model);
    serde_json::to_vec(&doc)
        .map(Bytes::from)
        .map_err(|e| e.to_string())
}

async fn provider_error(response: Response) -> ProviderError {
    let status = response.status();
    let retry_after = response
        .headers()
        .get(http::header::RETRY_AFTER)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let bytes = axum::body::to_bytes(response.into_body(), 16 * 1024)
        .await
        .unwrap_or_default();
    let message = serde_json::from_slice::<Value>(&bytes)
        .ok()
        .and_then(|v| {
            v.pointer("/error/message")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .unwrap_or_else(|| String::from_utf8_lossy(&bytes).into_owned());
    let kind = match status.as_u16() {
        401 => ProviderErrorKind::Authentication,
        403 => ProviderErrorKind::Permission,
        429 => ProviderErrorKind::RateLimit,
        400 | 404 | 413 | 422 => ProviderErrorKind::InvalidRequest,
        _ => ProviderErrorKind::Api,
    };
    let mut error = ProviderError::new(status, kind, format!("Cursor Messages broker: {message}"));
    error.retry_after = retry_after;
    error
}

/// Loopback-only `http://host[:port][/v1]`; returns the origin and probe addresses.
fn parse_origin(raw: &str) -> Result<(String, Vec<SocketAddr>), String> {
    let bad = |why: &str| format!("{BASE_URL_ENV} {why}");
    let rest = raw
        .trim()
        .strip_prefix("http://")
        .ok_or_else(|| bad("must be an http:// loopback URL"))?;
    if rest.contains(['?', '#', '@', ' ']) {
        return Err(bad("must not carry credentials, query or fragment"));
    }
    let (authority, path) = rest.find('/').map_or((rest, ""), |i| rest.split_at(i));
    if !matches!(path, "" | "/" | "/v1" | "/v1/") {
        return Err(bad("path must be empty or /v1"));
    }
    let (host, port) = match authority.strip_prefix('[') {
        Some(v6) => {
            let (host, tail) = v6
                .split_once(']')
                .ok_or_else(|| bad("has an unclosed [ipv6]"))?;
            let port = match tail {
                "" => None,
                t => Some(
                    t.strip_prefix(':')
                        .ok_or_else(|| bad("has an invalid port"))?,
                ),
            };
            (format!("[{host}]"), port)
        }
        None => match authority.split_once(':') {
            Some((host, port)) => (host.to_string(), Some(port)),
            None => (authority.to_string(), None),
        },
    };
    let port = match port {
        None => 80,
        Some(port) => port
            .parse::<u16>()
            .ok()
            .filter(|p| *p != 0)
            .ok_or_else(|| bad("has an invalid port"))?,
    };
    let ips: Vec<IpAddr> = if host.eq_ignore_ascii_case("localhost") {
        vec![Ipv4Addr::LOCALHOST.into(), Ipv6Addr::LOCALHOST.into()]
    } else {
        let ip = host
            .trim_matches(['[', ']'])
            .parse::<IpAddr>()
            .ok()
            .filter(|ip| ip.is_loopback() && host.starts_with('[') == ip.is_ipv6())
            .ok_or_else(|| bad("host must be localhost, 127.0.0.1 or [::1]"))?;
        vec![ip]
    };
    let addrs = ips
        .into_iter()
        .map(|ip| SocketAddr::new(ip, port))
        .collect();
    Ok((format!("http://{host}:{port}"), addrs))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_accepts_only_plain_loopback() {
        for ok in [
            "http://127.0.0.1:9",
            "http://localhost:9/v1/",
            "http://[::1]:9/v1",
            "http://127.0.0.1",
        ] {
            assert!(parse_origin(ok).is_ok(), "{ok}");
        }
        for bad in [
            "https://127.0.0.1:9",
            "http://example.com:9",
            "http://10.0.0.1:9",
            "http://u:p@127.0.0.1:9",
            "http://127.0.0.1:9/x",
            "http://127.0.0.1:9/?a=b",
            "http://127.0.0.1:9#f",
            "http://::1:9",
            "http://127.0.0.1:0",
            "http://[127.0.0.1]:9",
            "http://127.0.0.1:9/v1/messages",
            "http://[::1]x:9",
            "http://[::1]garbage",
            "http://[::1]:",
            "",
        ] {
            assert!(parse_origin(bad).is_err(), "{bad}");
        }
        assert_eq!(
            parse_origin("http://localhost:9/v1/").unwrap().0,
            "http://localhost:9"
        );
    }

    #[test]
    fn model_needs_explicit_agent_id() {
        assert_eq!(exact_model(Some("cursor:gpt-5.5")).unwrap(), "gpt-5.5");
        for bad in [
            "cursor",
            "cursor-agent",
            "cursor-ask:x",
            "cursor-plan:x",
            "cursor:x-fast",
            "cursor:",
            "claude",
        ] {
            assert!(exact_model(Some(bad)).is_err(), "{bad}");
        }
        assert!(exact_model(None).is_err());
    }

    #[test]
    fn stream_true_body_forces_stream_true_over_raw_false() {
        let body: MessagesRequest = serde_json::from_value(json!({
            "model":"cursor:m","stream":true,"max_tokens":1,
            "messages":[{"role":"user","content":"x"}]
        }))
        .unwrap();
        let raw = br#"{"model":"cursor:m","stream":false,"messages":[]}"#;
        let out = upstream_body(Some(raw), &body, "m").unwrap();
        let doc: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(doc["stream"], true);
        let out = upstream_body(
            Some(br#"{"stream":false}"#),
            &MessagesRequest {
                stream: false,
                ..body
            },
            "m",
        )
        .unwrap();
        let doc: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(doc["stream"], false);
    }
}
