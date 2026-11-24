use axum::{body::Body, response::IntoResponse};
use claude_codex::{
    anthropic::schema::MessagesRequest,
    provider::{GenerationBody, Provider, RequestContext},
    providers::kimi::{
        KimiProvider,
        translate::request::{TranslateOptions, translate_request},
    },
};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use std::{
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::sync::{Notify, mpsc};

static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

struct Env {
    old: Vec<(&'static str, Option<std::ffi::OsString>)>,
    _lock: std::sync::MutexGuard<'static, ()>,
}
impl Env {
    fn set(values: &[(&'static str, String)]) -> Self {
        let guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let mut old = Vec::new();
        for (key, value) in values {
            old.push((*key, std::env::var_os(key)));
            unsafe {
                std::env::set_var(key, value);
            }
        }
        Self { old, _lock: guard }
    }
}
impl Drop for Env {
    fn drop(&mut self) {
        for (key, value) in &self.old {
            unsafe {
                match value {
                    Some(value) => std::env::set_var(key, value),
                    None => std::env::remove_var(key),
                }
            }
        }
    }
}

fn context() -> RequestContext {
    RequestContext {
        req_id: "kimi-test".into(),
        session_id: Some("toy-session".into()),
        session_seq: None,
        provider: "kimi".into(),
        traffic: None,
        monitor: None,
        passthrough: None,
    }
}
fn request() -> MessagesRequest {
    serde_json::from_value(json!({"model":"kimi-k3.1-pro", "messages":[{"role":"user","content":"hello"}], "stream":true, "max_tokens":64})).unwrap()
}
async fn collect(body: Body) -> String {
    String::from_utf8(
        tokio::time::timeout(
            Duration::from_secs(3),
            axum::body::to_bytes(body, 1024 * 1024),
        )
        .await
        .unwrap()
        .unwrap()
        .to_vec(),
    )
    .unwrap()
}

#[test]
fn kimi_request_wire_controls_and_replay_are_not_silently_lost() {
    for (choice, expected) in [
        (json!({"type":"none"}), json!("none")),
        (json!({"type":"any"}), json!("required")),
        (json!({"type":"tool","name":"lookup"}), json!("required")),
    ] {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model":"kimi-k3.1-pro", "messages":[{"role":"user","content":"hello"}],
            "tools":[{"name":"lookup","defer_loading":true,"input_schema":{"type":"object"}}],
            "tool_choice":choice,"temperature":0.3,"top_p":0.8,"stop_sequences":["END"],
            "output_config":{"effort":"low","format":{"type":"json_schema","schema":{"type":"object","properties":{"ok":{"type":"boolean"}}}}}
        })).unwrap();
        let value = serde_json::to_value(
            translate_request(&req, TranslateOptions { session_id: None }).unwrap(),
        )
        .unwrap();
        assert_eq!(value["tool_choice"], expected);
        assert_eq!(value["temperature"], 0.3);
        assert_eq!(value["top_p"], 0.8);
        assert_eq!(value["stop"], json!(["END"]));
        assert_eq!(
            value["response_format"]["json_schema"]["schema"]["properties"]["ok"]["type"],
            "boolean"
        );
        assert_eq!(value["reasoning_effort"], "low");
        assert!(value.get("thinking").is_none());
    }
    let req: MessagesRequest = serde_json::from_value(json!({"model":"kimi-k3", "messages":[
        {"role":"assistant","content":[{"type":"thinking","thinking":"native", "signature":"genuine-claude-signature"},{"type":"tool_use","id":"call_1","name":"lookup","input":{"q":"x"}}]},
        {"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":[{"type":"tool_reference","tool_name":"lookup"},{"type":"text","text":"result"}]}]}
    ],"tools":[{"name":"lookup","input_schema":{"type":"object"}}]})).unwrap();
    let value = serde_json::to_value(
        translate_request(&req, TranslateOptions { session_id: None }).unwrap(),
    )
    .unwrap();
    assert_eq!(value["messages"][0]["content"], "");
    assert!(value["messages"][0].get("reasoning_content").is_none());
    assert_eq!(
        value["messages"][0]["tool_calls"][0]["function"]["arguments"],
        "{\"q\":\"x\"}"
    );
    assert_eq!(value["messages"][1]["tool_call_id"], "call_1");
    assert!(
        value["messages"][1]["content"][0]["text"]
            .as_str()
            .unwrap()
            .contains("Tool available: lookup")
    );

    for extra in [
        json!({"thinking":{"type":"disabled"}}),
        json!({"tools":[{"type":"web_search_20250305","name":"web_search"}]}),
        json!({"stop_sequences":["a".repeat(33)]}),
        json!({"top_k":1}),
    ] {
        let mut req = request();
        req.extra.extend(extra.as_object().unwrap().clone());
        assert!(translate_request(&req, TranslateOptions { session_id: None }).is_err());
    }
    for (role, block) in [
        ("user", json!({"type":"document","source":{}})),
        ("assistant", json!({"type":"image","source":{}})),
        ("user", json!({"type":"audio","data":"abc"})),
    ] {
        let mut req = request();
        req.messages[0].role = role.into();
        req.messages[0].content = json!([block]);
        assert!(translate_request(&req, TranslateOptions { session_id: None }).is_err());
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn kimi_wire_stream_is_incremental_cancellable_and_never_retries_after_output() {
    let temp = tempfile::tempdir().unwrap();
    std::fs::create_dir(temp.path().join("kimi")).unwrap();
    std::fs::write(
        temp.path().join("kimi/auth.json"),
        json!({"access":"toy-access","refresh":"","expires":4102444800000u64}).to_string(),
    )
    .unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let mode = Arc::new(AtomicUsize::new(0));
    let calls = Arc::new(AtomicUsize::new(0));
    let gate = Arc::new(Notify::new());
    let closed = Arc::new(Notify::new());
    let router = axum::Router::new().route("/chat/completions", axum::routing::post({
        let (mode, calls, gate, closed) = (mode.clone(),calls.clone(),gate.clone(),closed.clone());
        move |headers: http::HeaderMap, axum::Json(body): axum::Json<Value>| {
            let (mode, calls, gate, closed) = (mode.clone(),calls.clone(),gate.clone(),closed.clone());
            async move {
                calls.fetch_add(1, Ordering::SeqCst);
                assert_eq!(headers["authorization"], "Bearer toy-access");
                assert!(headers.get("x-claude-codex-account-pool").is_none());
                assert_eq!(body["model"], "k3.1-pro");
                assert!(body.get("thinking").is_none());
                if mode.load(Ordering::SeqCst) == 3 {
                    return (http::StatusCode::TOO_MANY_REQUESTS, [("retry-after","60")], "quota").into_response();
                }
                let scenario = mode.load(Ordering::SeqCst);
                let (tx, rx) = mpsc::channel::<Result<bytes::Bytes, std::io::Error>>(1);
                tokio::spawn(async move {
                    let first = "data: {\"choices\":[{\"delta\":{\"content\":\"مرحبا\"}}]}\r\n\r\n".as_bytes();
                    for byte in first {
                        if tx.send(Ok(bytes::Bytes::copy_from_slice(&[*byte]))).await.is_err() { closed.notify_one(); return; }
                    }
                    tokio::select! {
                        _ = gate.notified() => {},
                        _ = tx.closed() => { closed.notify_one(); return; }
                    }
                    let tail = match scenario {
                        1 => "data: {\"error\":{\"message\":\"late quota\",\"type\":\"rate_limit\"}}\n\n",
                        2 => "",
                        _ => concat!("data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n", "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":3,\"prompt_tokens_details\":{\"cached_tokens\":4}}}\n\n", "data: [DONE]\n\n"),
                    };
                    let _ = tx.send(Ok(bytes::Bytes::from_static(tail.as_bytes()))).await;
                });
                ([("content-type","text/event-stream")], Body::from_stream(futures_util::stream::unfold(rx, |mut rx| async { rx.recv().await.map(|item|(item,rx)) }))).into_response()
            }
        }
    }));
    let server = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    struct Abort(tokio::task::JoinHandle<()>);
    impl Drop for Abort {
        fn drop(&mut self) {
            self.0.abort();
        }
    }
    let _server = Abort(server);
    let _env = Env::set(&[
        ("CCP_CONFIG_DIR", temp.path().display().to_string()),
        ("CCP_KIMI_BASE_URL", format!("http://{address}")),
        ("NO_PROXY", "127.0.0.1,localhost".into()),
    ]);
    for scenario in 0..=2 {
        mode.store(scenario, Ordering::SeqCst);
        let generation = tokio::time::timeout(
            Duration::from_secs(3),
            KimiProvider::new().generate_anthropic_stream(request(), context()),
        )
        .await
        .unwrap()
        .unwrap();
        let GenerationBody::LiveSse(mut body) = generation.body else {
            panic!("stream was buffered")
        };
        let first = tokio::time::timeout(Duration::from_secs(3), body.frame())
            .await
            .unwrap()
            .unwrap()
            .unwrap()
            .into_data()
            .unwrap();
        assert!(String::from_utf8_lossy(&first).contains("مرحبا"));
        assert!(!String::from_utf8_lossy(&first).contains("message_stop"));
        gate.notify_one();
        let tail = collect(body).await;
        if scenario == 0 {
            assert!(tail.contains("\"cache_read_input_tokens\":4"));
            assert!(tail.contains("\"input_tokens\":6"));
            assert!(tail.contains("\"output_tokens\":3"));
            assert!(tail.contains("message_stop"));
        } else {
            assert!(tail.contains("event: error"));
            assert!(!tail.contains("message_stop"));
        }
        assert_eq!(calls.load(Ordering::SeqCst), scenario + 1);
    }
    mode.store(0, Ordering::SeqCst);
    let generation = KimiProvider::new()
        .generate_anthropic_stream(request(), context())
        .await
        .unwrap();
    let GenerationBody::LiveSse(mut body) = generation.body else {
        panic!()
    };
    body.frame().await.unwrap().unwrap();
    drop(body);
    tokio::time::timeout(Duration::from_secs(3), closed.notified())
        .await
        .unwrap();
    mode.store(3, Ordering::SeqCst);
    let before = calls.load(Ordering::SeqCst);
    let error = claude_codex::retry::fail_fast(
        true,
        KimiProvider::new().generate_anthropic_stream(request(), context()),
    )
    .await
    .err()
    .unwrap();
    assert_eq!(error.status, http::StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(calls.load(Ordering::SeqCst), before + 1);
}

#[tokio::test]
async fn kimi_initial_sse_error_returns_502_and_falls_back_but_late_error_does_not() {
    use claude_codex::{config::AliasProvider, registry::Registry, server::app};
    use tower::ServiceExt;

    let temp = tempfile::tempdir().unwrap();
    std::fs::create_dir(temp.path().join("kimi")).unwrap();
    std::fs::write(
        temp.path().join("kimi/auth.json"),
        json!({"access":"toy-access","refresh":"","expires":4102444800000u64}).to_string(),
    )
    .unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let late = Arc::new(AtomicUsize::new(0));
    let router = axum::Router::new().route("/chat/completions", axum::routing::post({
        let (calls, late) = (calls.clone(), late.clone());
        move |axum::Json(body): axum::Json<Value>| {
            let (calls, late) = (calls.clone(), late.clone());
            async move {
                calls.fetch_add(1, Ordering::SeqCst);
                let stream = if body["model"] == "k3" {
                    concat!(
                        "data: {\"choices\":[{\"delta\":{\"content\":\"fallback answer\"}}]}\n\n",
                        "data: {\"choices\":[{\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
                    ).to_string()
                } else {
                    let mut stream = "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n".to_string();
                    if late.load(Ordering::SeqCst) != 0 {
                        stream.push_str("data: {\"choices\":[{\"delta\":{\"content\":\"partial answer\"}}]}\n\n");
                    }
                    stream.push_str("data: {\"error\":{\"message\":\"upstream failed\",\"type\":\"server_error\"}}\n\n");
                    stream
                };
                ([("content-type", "text/event-stream")], stream)
            }
        }
    }));
    let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let _env = Env::set(&[
        ("CCP_CONFIG_DIR", temp.path().display().to_string()),
        ("CCP_KIMI_BASE_URL", format!("http://{address}")),
        ("NO_PROXY", "127.0.0.1,localhost".into()),
    ]);
    let response = KimiProvider::new()
        .handle_messages(request(), context())
        .await;
    assert_eq!(response.status(), http::StatusCode::BAD_GATEWAY);
    assert!(collect(response.into_body()).await.contains("api_error"));
    let error = KimiProvider::new()
        .generate_anthropic_stream(request(), context())
        .await
        .err()
        .unwrap();
    assert_eq!(error.status, http::StatusCode::BAD_GATEWAY);
    assert_eq!(error.kind, claude_codex::provider::ProviderErrorKind::Api);

    for is_late in [false, true] {
        late.store(usize::from(is_late), Ordering::SeqCst);
        let before = calls.load(Ordering::SeqCst);
        let registry = Registry::from_providers(
            AliasProvider::Kimi,
            [Arc::new(KimiProvider::new()) as Arc<dyn Provider>],
        );
        let response = app(Arc::new(registry))
            .oneshot(
                http::Request::builder()
                    .method("POST")
                    .uri("/v1/messages")
                    .header("content-type", "application/json")
                    .header("x-claude-codex-account-pool", "[]")
                    .header(
                        "x-claude-codex-fallback",
                        r#"{"chains":{"kimi":["kimi","kimi-k3"]},"on":{"server_error":true}}"#,
                    )
                    .body(Body::from(serde_json::to_vec(&request()).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), http::StatusCode::OK);
        let output = collect(response.into_body()).await;
        if is_late {
            assert!(output.contains("partial answer"));
            assert!(output.contains("event: error"));
            assert!(!output.contains("fallback answer"));
            assert_eq!(calls.load(Ordering::SeqCst), before + 1);
        } else {
            assert!(output.contains("fallback answer"));
            assert!(!output.contains("event: error"));
            assert_eq!(calls.load(Ordering::SeqCst), before + 2);
        }
    }
    server.abort();
}

#[test]
fn kimi_coalesced_malformed_frames_preserve_content_and_end_with_sse_error() {
    for tail in [
        b"data: \xff\n\n".as_slice(),
        b"data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"incomplete\"}]}}]}\n\ndata: [DONE]\n\n".as_slice(),
    ] {
        let mut translator = claude_codex::providers::kimi::translate::stream::LiveTranslator::new("msg", "kimi-k3");
        let mut upstream = b"data: {\"choices\":[{\"delta\":{\"content\":\"partial answer\"}}]}\n\n".to_vec();
        upstream.extend_from_slice(tail);
        let output = String::from_utf8(translator.push(&upstream).unwrap()).unwrap();
        assert!(output.contains("partial answer"));
        assert!(output.contains("event: error"));
        assert!(!output.contains("message_stop"));
        assert!(translator.finished);
    }
}

#[test]
fn kimi_accepts_claude_code_tool_metadata_and_rewrites_only_proxy_reasoning() {
    let req: MessagesRequest = serde_json::from_value(json!({
        "model":"kimi-k3", "thinking":{"type":"enabled","budget_tokens":1024},
        "context_management":{"edits":[]},
        "tools":[{"name":"ToolSearch","description":"find tools","strict":true,"defer_loading":true,"cache_control":{"type":"ephemeral"},"input_schema":{"type":"object"}}],
        "messages":[
            {"role":"assistant","content":[{"type":"thinking","thinking":"kimi native","signature":"Y2NwOmtpbWk6djE6bXNnXzE6MA"}]},
            {"role":"user","content":"continue"}
        ]
    })).unwrap();
    let value = serde_json::to_value(
        translate_request(&req, TranslateOptions { session_id: None }).unwrap(),
    )
    .unwrap();
    assert_eq!(value["tools"][0]["function"]["name"], "ToolSearch");
    assert_eq!(value["messages"][0]["reasoning_content"], "kimi native");
    assert_eq!(value["reasoning_effort"], "high");
}

#[test]
fn kimi_reducer_waits_for_fragmented_tool_identity() {
    let stream = concat!(
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"{\\\"q\\\"\"}}]}}]}\n\n",
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\"}]}}]}\n\n",
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"name\":\"lookup\",\"arguments\":\":1}\"}}]}}]}\n\n",
        "data: {\"choices\":[{\"finish_reason\":\"tool_calls\"}]}\n\ndata: [DONE]\n\n"
    );
    let out = String::from_utf8(
        claude_codex::providers::kimi::translate::stream::translate_stream_bytes(
            stream.as_bytes(),
            "msg",
            "kimi-k3",
        )
        .unwrap(),
    )
    .unwrap();
    let json =
        claude_codex::anthropic::accumulate::accumulate_response(out.as_bytes(), "msg", "kimi-k3")
            .unwrap();
    assert_eq!(json["content"][0]["id"], "call_1");
    assert_eq!(json["content"][0]["input"], json!({"q":1}));
    assert_eq!(json["stop_reason"], "tool_use");
    assert!(claude_codex::providers::kimi::translate::stream::translate_stream_bytes(b"data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"x\"}]}}]}\n\ndata: [DONE]\n\n","m","k").is_err());
}

#[tokio::test]
async fn kimi_login_quota_falls_back_to_api_key_then_to_the_chain_without_surfacing_balance() {
    use claude_codex::{config::AliasProvider, registry::Registry, server::app};
    use tower::ServiceExt;

    let temp = tempfile::tempdir().unwrap();
    std::fs::create_dir(temp.path().join("kimi")).unwrap();
    let login = temp.path().join("kimi/auth.json");
    std::fs::write(
        &login,
        json!({"access":"toy-login","refresh":"","expires":4102444800000u64}).to_string(),
    )
    .unwrap();
    std::fs::write(temp.path().join("kimi/api_key"), "toy-key\n").unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let seen = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let key_has_balance = Arc::new(AtomicUsize::new(1));
    let router = axum::Router::new().route("/chat/completions", axum::routing::post({
        let (seen, key_has_balance) = (seen.clone(), key_has_balance.clone());
        move |headers: http::HeaderMap, axum::Json(body): axum::Json<Value>| {
            let (seen, key_has_balance) = (seen.clone(), key_has_balance.clone());
            async move {
                let auth = headers["authorization"].to_str().unwrap().to_string();
                let model = body["model"].as_str().unwrap().to_string();
                seen.lock().unwrap().push(format!("{auth}|{model}"));
                let broke = auth == "Bearer toy-login"
                    || key_has_balance.load(Ordering::SeqCst) == 0 && model.contains("3.1");
                if broke {
                    let error = json!({"error":{"type":"exceeded_current_quota_error","message":"Your account is suspended due to insufficient balance"}});
                    return (http::StatusCode::TOO_MANY_REQUESTS, error.to_string()).into_response();
                }
                (
                    [("content-type", "text/event-stream")],
                    format!(
                        "data: {{\"choices\":[{{\"delta\":{{\"content\":\"answered by {model}\"}}}}]}}\n\ndata: {{\"choices\":[{{\"finish_reason\":\"stop\"}}]}}\n\ndata: [DONE]\n\n"
                    ),
                )
                    .into_response()
            }
        }
    }));
    let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let _env = Env::set(&[
        ("CCP_CONFIG_DIR", temp.path().display().to_string()),
        ("CCP_KIMI_BASE_URL", format!("http://{address}")),
        ("CCP_KIMI_API_KEY_BASE_URL", format!("http://{address}")),
        ("NO_PROXY", "127.0.0.1,localhost".into()),
    ]);
    let take = || std::mem::take(&mut *seen.lock().unwrap());

    let generation = KimiProvider::new()
        .generate_anthropic_stream(request(), context())
        .await
        .unwrap();
    assert_eq!(generation.resolved_model, "kimi-k3.1-pro");
    let GenerationBody::LiveSse(body) = generation.body else {
        panic!("stream was buffered")
    };
    assert!(collect(body).await.contains("answered by kimi-k3.1-pro"));
    assert_eq!(
        take(),
        ["Bearer toy-login|k3.1-pro", "Bearer toy-key|kimi-k3.1-pro"]
    );

    key_has_balance.store(0, Ordering::SeqCst);
    let error = KimiProvider::new()
        .generate_anthropic_stream(request(), context())
        .await
        .err()
        .unwrap();
    assert_eq!(error.status, http::StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(take().len(), 2, "a spent balance is not retried");

    let registry = Registry::from_providers(
        AliasProvider::Kimi,
        [Arc::new(KimiProvider::new()) as Arc<dyn Provider>],
    );
    let response = app(Arc::new(registry))
        .oneshot(
            http::Request::builder()
                .method("POST")
                .uri("/v1/messages")
                .header("content-type", "application/json")
                .header("x-claude-codex-account-pool", "[]")
                .header(
                    "x-claude-codex-fallback",
                    r#"{"chains":{"kimi":["kimi","kimi-k3"]},"on":{"rate_limit":true}}"#,
                )
                .body(Body::from(serde_json::to_vec(&request()).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), http::StatusCode::OK);
    let output = collect(response.into_body()).await;
    assert!(output.contains("answered by kimi-k3"), "{output}");
    assert!(!output.contains("insufficient balance"));
    assert!(!output.contains("event: error"));
    take();

    std::fs::remove_file(&login).unwrap();
    key_has_balance.store(1, Ordering::SeqCst);
    let generation = KimiProvider::new()
        .generate_anthropic_stream(request(), context())
        .await
        .unwrap();
    assert_eq!(generation.resolved_model, "kimi-k3.1-pro");
    assert_eq!(take(), ["Bearer toy-key|kimi-k3.1-pro"]);
    assert!(KimiProvider::new().availability().is_ready());
    server.abort();
}
