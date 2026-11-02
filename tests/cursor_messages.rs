//! Optional Cursor Messages broker: real Registry + server app against a mock broker.
#![allow(clippy::await_holding_lock)]
use axum::{
    Router,
    body::{Body, Bytes},
    http::{HeaderMap, Request, StatusCode},
    response::{IntoResponse, Response},
};
use claude_codex::{config::AliasProvider, registry::Registry, server::app_with_options};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex, MutexGuard};
use tokio::{net::TcpListener, task::JoinHandle};
use tower::ServiceExt;

static ENV_LOCK: Mutex<()> = Mutex::new(());
const VARS: &[&str] = &[
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "CCP_CONFIG_DIR",
    "CCP_CURSOR_MESSAGES_BASE_URL",
    "CCP_CURSOR_MESSAGES_API_KEY",
    "CCP_CURSOR_AUTH_TOKEN",
    "CURSOR_AUTH_TOKEN",
    "CCP_CURSOR_BASE_URL",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "CCP_ANTHROPIC_PROXY",
    "CCP_ANTHROPIC_PROXY_CA",
];

struct Env {
    saved: Vec<(&'static str, Option<String>)>,
    _home: tempfile::TempDir,
    _lock: MutexGuard<'static, ()>,
}
impl Env {
    fn new(broker_url: Option<&str>) -> Self {
        let lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let saved = VARS.iter().map(|v| (*v, std::env::var(v).ok())).collect();
        let home = tempfile::tempdir().unwrap();
        unsafe {
            for var in VARS {
                std::env::remove_var(var);
            }
            std::env::set_var("HOME", home.path());
            std::env::set_var("XDG_CONFIG_HOME", home.path().join("c"));
            std::env::set_var("XDG_STATE_HOME", home.path().join("s"));
            if let Some(url) = broker_url {
                std::env::set_var("CCP_CURSOR_MESSAGES_BASE_URL", url);
                std::env::set_var("CCP_CURSOR_MESSAGES_API_KEY", "toy-broker-key");
            }
        }
        Self {
            saved,
            _home: home,
            _lock: lock,
        }
    }
}
impl Drop for Env {
    fn drop(&mut self) {
        unsafe {
            for (var, value) in &self.saved {
                match value {
                    Some(v) => std::env::set_var(var, v),
                    None => std::env::remove_var(var),
                }
            }
        }
    }
}

#[derive(Clone)]
struct Seen {
    path: String,
    headers: HeaderMap,
    body: Value,
}
struct Mock {
    base: String,
    seen: Arc<Mutex<Vec<Seen>>>,
    task: JoinHandle<()>,
}
impl Drop for Mock {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl Mock {
    fn seen(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }
}

const TOOL_SSE: &str = "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m1\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"model\":\"x\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n\
event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"toolu_1\",\"name\":\"Read\",\"input\":{}}}\n\n\
event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"file_path\\\":\\\"/m\\\"}\"}}\n\n\
event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n\
event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"},\"usage\":{\"output_tokens\":2}}\n\n\
event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";
const TEXT_SSE: &str = "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m2\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"model\":\"x\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n\
event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n\
event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"marker-done\"}}\n\n\
event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n\
event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":2}}\n\n\
event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";

fn sse(text: &'static str) -> Response {
    ([("content-type", "text/event-stream")], text).into_response()
}

async fn mock_broker() -> Mock {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let router = Router::new().fallback(move |req: Request<Body>| {
        let log = log.clone();
        async move {
            let path = req.uri().path().to_string();
            let headers = req.headers().clone();
            let bytes = req.into_body().collect().await.unwrap().to_bytes();
            let body: Value = serde_json::from_slice(&bytes).unwrap();
            let text = body.to_string();
            log.lock().unwrap().push(Seen {
                path: path.clone(),
                headers,
                body,
            });
            if path.ends_with("count_tokens") {
                return axum::Json(json!({"input_tokens": 42})).into_response();
            }
            for (marker, status) in [("S401", 401), ("S429", 429), ("S502", 502)] {
                if text.contains(marker) {
                    let mut r = (
                        StatusCode::from_u16(status).unwrap(),
                        axum::Json(
                            json!({"type":"error","error":{"type":"api_error","message":marker}}),
                        ),
                    )
                        .into_response();
                    r.headers_mut().insert("retry-after", "7".parse().unwrap());
                    return r;
                }
            }
            if text.contains("DROP") {
                let stream = futures_util::stream::unfold(0, |step| async move {
                    match step {
                        0 => Some((Ok(Bytes::from(&TOOL_SSE[..120])), 1)),
                        1 => {
                            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                            Some((Err(std::io::Error::other("broker died")), 2))
                        }
                        _ => None,
                    }
                });
                return (
                    [("content-type", "text/event-stream")],
                    Body::from_stream(stream),
                )
                    .into_response();
            }
            if text.contains("tool_result") {
                sse(TEXT_SSE)
            } else {
                sse(TOOL_SSE)
            }
        }
    });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    Mock { base, seen, task }
}

fn router() -> Router {
    app_with_options(
        Arc::new(Registry::new(AliasProvider::Anthropic)),
        None,
        true,
    )
}

async fn post(path: &str, session: &str, body: Value) -> (StatusCode, HeaderMap, Vec<u8>, bool) {
    let request = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .header("authorization", "Bearer client-claude-secret")
        .header("x-api-key", "client-api-key")
        .header("cookie", "sid=client-cookie")
        .header("anthropic-beta", "client-beta")
        .header("x-agent-session-id", "spoofed")
        .header("x-claude-codex-account-pool", "pool-secret")
        .header("x-claude-code-session-id", session)
        .body(Body::from(body.to_string()))
        .unwrap();
    let response = router().oneshot(request).await.unwrap();
    let (parts, mut body) = response.into_parts();
    let (mut out, mut errored) = (Vec::new(), false);
    while let Some(frame) = body.frame().await {
        match frame {
            Ok(frame) => out.extend_from_slice(frame.data_ref().map(|b| b.as_ref()).unwrap_or(&[])),
            Err(_) => {
                errored = true;
                break;
            }
        }
    }
    (parts.status, parts.headers, out, errored)
}

fn req(model: &str, text: &str) -> Value {
    json!({"model": model, "max_tokens": 64, "stream": true,
        "system": [{"type":"text","text":"sys","cache_control":{"type":"ephemeral"}}],
        "tools": [{"name":"Read","description":"d","input_schema":{"type":"object"}}],
        "x_custom": {"a": [1, 2]},
        "messages": [{"role":"user","content":text}]})
}

#[tokio::test(flavor = "current_thread")]
async fn tool_use_stream_and_tool_result_continuation_are_opaque() {
    let mock = mock_broker().await;
    let _env = Env::new(Some(&format!("{}/v1", mock.base)));
    let (status, _, out, errored) =
        post("/v1/messages", "sess-a", req("cursor:gpt-5.5", "go")).await;
    assert!(status.is_success() && !errored);
    assert_eq!(out, TOOL_SSE.as_bytes());
    let mut next = req("cursor:gpt-5.5", "go");
    let result = json!({"type":"tool_result","tool_use_id":"toolu_1","content":"file body","is_error":false});
    next["messages"] = json!([
        {"role":"user","content":"go"},
        {"role":"assistant","content":[{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"/m"}}]},
        {"role":"user","content":[result.clone()]}
    ]);
    let (_, _, out, _) = post("/v1/messages", "sess-a", next).await;
    assert!(String::from_utf8_lossy(&out).contains("marker-done"));
    let seen = mock.seen();
    assert_eq!(seen.len(), 2);
    assert_eq!(seen[1].body["messages"][2]["content"][0], result);
    for s in &seen {
        assert_eq!(s.path, "/v1/messages");
        assert_eq!(s.body["model"], "gpt-5.5");
        assert_eq!(s.body["x_custom"], json!({"a": [1, 2]}));
        assert_eq!(s.body["tools"][0]["name"], "Read");
        assert_eq!(s.body["system"][0]["cache_control"]["type"], "ephemeral");
    }
}

#[tokio::test(flavor = "current_thread")]
async fn only_minimal_headers_reach_the_broker_and_sessions_stay_distinct() {
    let mock = mock_broker().await;
    let _env = Env::new(Some(&mock.base));
    post("/v1/messages", "sess-one", req("cursor:m", "a")).await;
    post("/v1/messages", "sess-two", req("cursor:m", "b")).await;
    let seen = mock.seen();
    let ids: Vec<_> = seen
        .iter()
        .map(|s| {
            s.headers["x-agent-session-id"]
                .to_str()
                .unwrap()
                .to_string()
        })
        .collect();
    assert_eq!(ids, ["sess-one", "sess-two"]);
    for s in &seen {
        assert_eq!(s.headers["authorization"], "Bearer toy-broker-key");
        assert_eq!(s.headers["anthropic-version"], "2023-06-01");
        for leaked in [
            "x-api-key",
            "cookie",
            "anthropic-beta",
            "x-claude-codex-account-pool",
            "x-claude-code-session-id",
        ] {
            assert!(!s.headers.contains_key(leaked), "{leaked} leaked");
        }
        let all = format!("{:?}", s.headers);
        assert!(
            !all.contains("client-") && !all.contains("pool-secret") && !all.contains("spoofed"),
            "{all}"
        );
    }
}

#[tokio::test(flavor = "current_thread")]
async fn count_tokens_uses_broker_route() {
    let mock = mock_broker().await;
    let _env = Env::new(Some(&mock.base));
    let (status, _, out, _) = post(
        "/v1/messages/count_tokens",
        "s",
        req("cursor:gpt-5.5", "hi"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        serde_json::from_slice::<Value>(&out).unwrap()["input_tokens"],
        42
    );
    let seen = mock.seen();
    assert_eq!(seen[0].path, "/v1/messages/count_tokens");
    assert_eq!(seen[0].body["model"], "gpt-5.5");
}

#[tokio::test(flavor = "current_thread")]
async fn upstream_errors_keep_their_status() {
    let mock = mock_broker().await;
    let _env = Env::new(Some(&mock.base));
    for (marker, code) in [("S401", 401), ("S429", 429), ("S502", 502)] {
        let (status, headers, out, _) = post("/v1/messages", "s", req("cursor:m", marker)).await;
        assert_eq!(status.as_u16(), code);
        assert!(String::from_utf8_lossy(&out).contains(marker));
        if code == 429 {
            assert_eq!(headers["retry-after"], "7");
        }
    }
}

#[tokio::test(flavor = "current_thread")]
async fn midstream_drop_is_an_error_not_a_synthetic_end_turn() {
    let mock = mock_broker().await;
    let _env = Env::new(Some(&mock.base));
    let (status, _, out, errored) = post("/v1/messages", "s", req("cursor:m", "DROP")).await;
    assert!(status.is_success());
    assert!(errored, "drop must surface as a body error");
    let text = String::from_utf8_lossy(&out);
    assert!(
        !text.contains("end_turn") && !text.contains("message_stop"),
        "{text}"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn dead_broker_never_falls_back_to_native() {
    let native = mock_broker().await;
    let dead = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        format!("http://{}", l.local_addr().unwrap())
    };
    let _env = Env::new(Some(&dead));
    unsafe {
        std::env::set_var("CCP_CURSOR_AUTH_TOKEN", "toy-native-token");
        std::env::set_var("CCP_CURSOR_BASE_URL", &native.base);
    }
    let (status, _, _, _) = post("/v1/messages", "s", req("cursor:m", "x")).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert!(native.seen().is_empty());
    let provider = Registry::new(AliasProvider::Anthropic)
        .provider("cursor")
        .unwrap();
    assert!(!provider.availability().is_ready());
}

#[tokio::test(flavor = "current_thread")]
async fn invalid_url_and_unsupported_modes_are_400_without_upstream_traffic() {
    let mock = mock_broker().await;
    {
        let _env = Env::new(Some(&mock.base));
        for model in [
            "cursor-ask:m",
            "cursor-plan:m",
            "cursor:m-fast",
            "cursor",
            "cursor-agent",
        ] {
            let (status, _, out, _) = post("/v1/messages", "s", req(model, "x")).await;
            assert_eq!(status, StatusCode::BAD_REQUEST, "{model}");
            assert!(String::from_utf8_lossy(&out).contains("broker"), "{model}");
        }
        let (status, ..) = post("/v1/messages/count_tokens", "s", req("cursor-ask:m", "x")).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }
    for url in [
        "https://127.0.0.1:1",
        "http://example.com:1",
        "http://u:p@127.0.0.1:1",
        "http://127.0.0.1:1/x",
    ] {
        let _env = Env::new(Some(url));
        let (status, ..) = post("/v1/messages", "s", req("cursor:m", "x")).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{url}");
    }
    assert!(mock.seen().is_empty());
}

#[tokio::test(flavor = "current_thread")]
async fn chat_completions_generation_reuses_the_relay() {
    let mock = mock_broker().await;
    let _env = Env::new(Some(&mock.base));
    let chat = |text: &str| json!({"model":"cursor:gpt-5.5","stream":true,"messages":[{"role":"user","content":text}]});
    let (status, _, out, _) = post("/v1/chat/completions", "sess-g", chat("go")).await;
    assert!(status.is_success(), "{}", String::from_utf8_lossy(&out));
    let seen = mock.seen();
    assert_eq!(seen[0].body["model"], "gpt-5.5");
    assert_eq!(seen[0].body["stream"], true);
    assert_eq!(seen[0].headers["x-agent-session-id"], "sess-g");
    let (status, headers, ..) = post("/v1/chat/completions", "sess-g", chat("S429")).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(headers["retry-after"], "7");
    let (status, ..) = post("/v1/chat/completions", "sess-g", chat("S401")).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn ambient_proxy_settings_never_receive_loopback_broker_traffic() {
    let mock = mock_broker().await;
    let _env = Env::new(Some(&mock.base));
    unsafe {
        std::env::set_var("HTTP_PROXY", "http://127.0.0.1:9");
        std::env::set_var("ALL_PROXY", "http://127.0.0.1:9");
    }
    let (status, _headers, _body, _ended) = post(
        "/v1/messages/count_tokens",
        "proxy-session",
        json!({"model":"cursor:gpt-5.5","messages":[{"role":"user","content":"proxy check"}]}),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "loopback broker request must bypass ambient proxies"
    );
    assert_eq!(mock.seen.lock().unwrap().len(), 1);
}
