//! Quota failover: Claude accounts in order, then the Codex tier; Cursor only when configured.
use async_trait::async_trait;
use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode},
    response::{IntoResponse, Response},
};
use claude_codex::{
    MessagesRequest, RequestContext,
    config::AliasProvider,
    provider::{CliHandlers, Provider},
    providers::anthropic::AnthropicProvider,
    registry::Registry,
    server::app,
};
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use tokio::{net::TcpListener, task::JoinHandle};
use tower::ServiceExt;

const POOL: &str = "x-claude-codex-account-pool";
const POLICY: &str = "x-claude-codex-fallback";

struct Upstream {
    base: String,
    seen: Arc<Mutex<Vec<(String, bool)>>>,
    flaky: Arc<AtomicBool>,
    gate: Arc<tokio::sync::Notify>,
    task: JoinHandle<()>,
}
impl Drop for Upstream {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Anthropic stand-in: any bearer starting with `Bearer dead` is out of quota, and
/// `Bearer basic` has no `claude-mythos-*` models.
async fn anthropic_upstream() -> Upstream {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let flaky = Arc::new(AtomicBool::new(false));
    let gate = Arc::new(tokio::sync::Notify::new());
    let log = seen.clone();
    let failing = flaky.clone();
    let opened = gate.clone();
    let router = Router::new().fallback(move |req: Request<Body>| {
        let log = log.clone();
        let failing = failing.clone();
        let opened = opened.clone();
        async move {
            let auth = req.headers()["authorization"].to_str().unwrap().to_string();
            log.lock()
                .unwrap()
                .push((auth.clone(), req.headers().contains_key(POOL) || req.headers().contains_key(POLICY)));
            let body = axum::body::to_bytes(req.into_body(), 1_000_000).await.unwrap();
            let request = serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null);
            let model = request["model"].as_str().map(str::to_string);
            if auth.starts_with("Bearer flaky") && failing.load(Ordering::SeqCst) {
                return (StatusCode::INTERNAL_SERVER_ERROR, axum::Json(json!({"type":"error","error":{"type":"api_error","message":"flaky"}}))).into_response();
            }
            if auth.starts_with("Bearer refuse") {
                return axum::Json(json!({"type": "message", "servedBy": auth, "model": model, "stop_reason": "refusal"})).into_response();
            }
            if auth.starts_with("Bearer tool-error") {
                return axum::Json(json!({"type": "message", "servedBy": auth, "stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t", "name": "report", "input": {"type": "error"}}]})).into_response();
            }
            if let Some(name) = auth.strip_prefix("Bearer sse-") {
                return ([("content-type", "text/event-stream")], sse_reply(name)).into_response();
            }
            if auth.starts_with("Bearer slow") {
                let parts = futures_util::stream::unfold(0, move |step| {
                    let opened = opened.clone();
                    async move {
                        match step {
                            0 => Some((Ok::<_, std::io::Error>(SSE_START.to_string()), 1)),
                            1 => {
                                opened.notified().await;
                                Some((Ok(SSE_END.to_string()), 2))
                            }
                            _ => None,
                        }
                    }
                });
                return ([("content-type", "text/event-stream")], Body::from_stream(parts)).into_response();
            }
            if auth.starts_with("Bearer abort") {
                let cut = futures_util::stream::unfold(0, |step| async move {
                    match step {
                        0 => Some((Ok("{\"partial\":"), 1)),
                        1 => {
                            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                            Some((Err(std::io::Error::other("cut")), 2))
                        }
                        _ => None,
                    }
                });
                return Body::from_stream(cut).into_response();
            }
            if auth.starts_with("Bearer invalid") {
                return (StatusCode::UNAUTHORIZED, axum::Json(json!({"type":"error","error":{"type":"authentication_error","message":"expired"}}))).into_response();
            }
            if auth.starts_with("Bearer basic") && model.as_deref().is_some_and(|m| m.starts_with("claude-mythos")) {
                return (StatusCode::NOT_FOUND, axum::Json(json!({"type":"error","error":{"type":"not_found_error","message":format!("model: {}", model.unwrap())}}))).into_response();
            }
            if auth.starts_with("Bearer dead") {
                return (
                    StatusCode::TOO_MANY_REQUESTS,
                    [
                        ("anthropic-ratelimit-unified-status", "rejected"),
                        ("anthropic-ratelimit-unified-reset", "4102444800"),
                    ],
                    axum::Json(json!({"type":"error","error":{"type":"rate_limit_error","message":"exhausted"}})),
                )
                    .into_response();
            }
            axum::Json(json!({"type": "message", "stop_reason": "end_turn", "servedBy": auth, "model": model, "thinking": request["thinking"], "effort": request["output_config"]["effort"]})).into_response()
        }
    });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    Upstream {
        base,
        seen,
        flaky,
        gate,
        task,
    }
}

const SSE_START: &str =
    "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{}}\n\n";
const SSE_END: &str = "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"}}\n\nevent: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";

fn sse_reply(name: &str) -> String {
    let delta = |reason: &str| {
        format!(
            "event: message_delta\ndata: {{\"type\" : \"message_delta\", \"delta\" : {{\"stop_reason\" : \"{reason}\"}}}}\n\n"
        )
    };
    let stop = "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";
    match name {
        "done" => format!("{SSE_START}{SSE_END}"),
        "spaced-refusal" => format!("{SSE_START}{}{stop}", delta("refusal")),
        "spaced-done" => format!("{SSE_START}{}{stop}", delta("end_turn")),
        "truncated" => format!("{SSE_START}{}", delta("end_turn")),
        "unterminated" => format!(
            "{SSE_START}{}{stop}event: ping\ndata: {{",
            delta("end_turn")
        ),
        "error" => format!(
            "{SSE_START}{}event: error\ndata: {{\"type\":\"error\",\"error\":{{\"type\":\"overloaded_error\"}}}}\n\n{stop}",
            delta("end_turn")
        ),
        other => panic!("no SSE reply {other}"),
    }
}

struct Stub {
    name: &'static str,
    model: &'static str,
    status: Mutex<StatusCode>,
    seen: Arc<Mutex<Vec<String>>>,
    thinking: Mutex<Vec<(Value, Value)>>,
}
#[async_trait]
impl Provider for Stub {
    fn name(&self) -> &'static str {
        self.name
    }
    fn supported_models(&self) -> Vec<String> {
        vec![self.model.to_string()]
    }
    fn cli(&self) -> &'static dyn CliHandlers {
        unreachable!()
    }
    async fn handle_messages(&self, body: MessagesRequest, _ctx: RequestContext) -> Response {
        let model = body.model.unwrap_or_default();
        self.seen.lock().unwrap().push(model.clone());
        self.thinking.lock().unwrap().push((
            body.extra.get("thinking").cloned().unwrap_or(Value::Null),
            body.extra
                .get("output_config")
                .and_then(|config| config.get("effort"))
                .cloned()
                .unwrap_or(Value::Null),
        ));
        (
            *self.status.lock().unwrap(),
            axum::Json(json!({"type": "message", "stop_reason": "end_turn", "servedBy": self.name, "model": model})),
        )
            .into_response()
    }
    async fn handle_count_tokens(&self, _b: MessagesRequest, _c: RequestContext) -> Response {
        unreachable!()
    }
}

fn stub(name: &'static str, model: &'static str, status: StatusCode) -> Arc<Stub> {
    Arc::new(Stub {
        name,
        model,
        status: Mutex::new(status),
        seen: Arc::default(),
        thinking: Mutex::default(),
    })
}

fn gateway(base: &str, extra: Vec<Arc<dyn Provider>>) -> Router {
    let mut providers: Vec<Arc<dyn Provider>> =
        vec![Arc::new(AnthropicProvider::with_base_url(base))];
    providers.extend(extra);
    app(Arc::new(Registry::from_providers(
        AliasProvider::Anthropic,
        providers,
    )))
}

fn pool(accounts: &[(&str, &str)]) -> String {
    json!(
        accounts
            .iter()
            .enumerate()
            .map(
                |(index, (alias, token))| json!({"alias": alias, "authorization": format!("Bearer {token}"), "primary": index == 0})
            )
            .collect::<Vec<_>>()
    )
    .to_string()
}

async fn send(
    router: Router,
    model: &str,
    pool: Option<String>,
    thread: bool,
) -> (StatusCode, Value) {
    send_with(router, model, pool, thread, None).await
}

async fn send_with(
    router: Router,
    model: &str,
    pool: Option<String>,
    thread: bool,
    policy: Option<Value>,
) -> (StatusCode, Value) {
    let mut payload =
        json!({"model": model, "max_tokens": 1, "messages": [{"role":"user","content":"hi"}]});
    if thread {
        payload["thread"] = json!({"type": "continue", "previous_message_id": "msg_toy"});
    }
    let mut request = Request::builder()
        .method("POST")
        .uri("/v1/messages?beta=true")
        .header("content-type", "application/json")
        .header("authorization", "Bearer own-session-token");
    if let Some(pool) = pool {
        request = request.header(POOL, pool);
    }
    if let Some(policy) = policy {
        request = request.header(POLICY, policy.to_string());
    }
    let response = router
        .oneshot(request.body(Body::from(payload.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), 100_000)
        .await
        .unwrap();
    (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
}

#[tokio::test]
async fn exhausted_account_falls_to_the_next_and_stays_skipped() {
    let upstream = anthropic_upstream().await;
    let accounts = pool(&[("a1", "dead-a1"), ("a2", "live-a2")]);
    let router = gateway(&upstream.base, vec![]);
    let (status, body) = send(
        router.clone(),
        "claude-opus-5-5",
        Some(accounts.clone()),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "Bearer live-a2");
    let (status, _) = send(router, "claude-opus-5-5", Some(accounts), false).await;
    assert_eq!(status, StatusCode::OK);
    let seen = upstream.seen.lock().unwrap().clone();
    assert_eq!(
        seen.iter()
            .map(|(auth, _)| auth.as_str())
            .collect::<Vec<_>>(),
        ["Bearer dead-a1", "Bearer live-a2", "Bearer live-a2"],
        "the exhausted account cools down until its reset"
    );
    assert!(
        seen.iter().all(|(_, pool)| !pool),
        "the pool never reaches the upstream"
    );
}

#[tokio::test]
async fn cooldown_is_per_model() {
    let upstream = anthropic_upstream().await;
    let accounts = pool(&[("b1", "dead-b1"), ("b2", "live-b2")]);
    send(
        gateway(&upstream.base, vec![]),
        "claude-opus-5-5",
        Some(accounts.clone()),
        false,
    )
    .await;
    send(
        gateway(&upstream.base, vec![]),
        "claude-sonnet-5-5",
        Some(accounts),
        false,
    )
    .await;
    let seen = upstream.seen.lock().unwrap().clone();
    assert_eq!(
        seen.iter()
            .filter(|(auth, _)| auth == "Bearer dead-b1")
            .count(),
        2
    );
}

#[tokio::test]
async fn all_accounts_exhausted_uses_codex_tier_then_a_configured_cursor() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::TOO_MANY_REQUESTS);
    let cursor = stub("cursor", "cursor:claude-opus-5", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone(), cursor.clone()]);
    let policy = json!({"chains": {"claude": ["accounts", "codex", "cursor"]}});
    let (status, body) = send_with(
        router,
        "claude-opus-5-5[1m]",
        Some(pool(&[("c1", "dead-c1"), ("c2", "dead-c2")])),
        false,
        Some(policy),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "cursor");
    assert_eq!(*codex.seen.lock().unwrap(), ["gpt-6.1-sol"]);
    assert_eq!(*cursor.seen.lock().unwrap(), ["cursor:claude-opus-5"]);
}

#[tokio::test]
async fn a_configured_chain_replaces_the_default_order() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::OK);
    let gemini = stub("gemini", "gemini-3-pro", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone(), gemini.clone()]);
    let policy = json!({"chains": {"claude": ["accounts", "gemini-3-pro", "codex"]}});
    let (status, body) = send_with(
        router,
        "claude-opus-5-5",
        Some(pool(&[("h1", "dead-h1")])),
        false,
        Some(policy),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "gemini");
    assert!(codex.seen.lock().unwrap().is_empty());
    assert!(
        upstream.seen.lock().unwrap().iter().all(|(_, pool)| !pool),
        "neither the pool nor the policy reaches the upstream"
    );
}

/// The shape the dashboard saves under `fallback.tiers` (tests/account_ui_http_test.py writes the same).
#[tokio::test]
async fn configured_tiers_choose_the_destination_model() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-custom", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    let policy = json!({"tiers": [
        {"name": "mine", "match": ["opus"], "models": {"anthropic": "claude-opus-5-5", "codex": "gpt-custom"}},
    ]});
    let (status, body) = send_with(
        router,
        "claude-opus-5-5",
        Some(pool(&[("h1", "dead-h1")])),
        false,
        Some(policy),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "codex");
    assert_eq!(*codex.seen.lock().unwrap(), ["gpt-custom"]);
}

#[tokio::test]
async fn server_errors_move_on_only_when_enabled() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6-luna", StatusCode::BAD_GATEWAY);
    let cursor = stub("cursor", "cursor:claude-sonnet-5", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone(), cursor.clone()]);
    let chains = json!({"codex": ["codex", "cursor"]});
    let (status, _) = send_with(
        router.clone(),
        "gpt-6-luna",
        Some("[]".into()),
        false,
        Some(json!({"chains": chains})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert!(cursor.seen.lock().unwrap().is_empty());
    let (status, body) = send_with(
        router.clone(),
        "gpt-6-luna",
        Some("[]".into()),
        false,
        Some(json!({"chains": chains, "on": {"server_error": true}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "cursor");
    // A server error is transient, so it never puts Codex on cooldown.
    send_with(
        router,
        "gpt-6-luna",
        Some("[]".into()),
        false,
        Some(json!({"chains": chains})),
    )
    .await;
    assert_eq!(codex.seen.lock().unwrap().len(), 3);
}

#[tokio::test]
async fn quota_fallback_can_be_switched_off() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    let (status, _) = send_with(
        router,
        "claude-opus-5-5",
        Some(pool(&[("i1", "dead-i1"), ("i2", "live-i2")])),
        false,
        Some(json!({"on": {"rate_limit": false}})),
    )
    .await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(upstream.seen.lock().unwrap().len(), 1);
    assert!(codex.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn exhausted_codex_falls_to_the_claude_tier_on_the_account_pool() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6-astra", StatusCode::TOO_MANY_REQUESTS);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    let (status, body) = send(
        router,
        "gpt-6-astra[1m]",
        Some(pool(&[("f1", "dead-f1"), ("f2", "live-f2")])),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "Bearer live-f2");
    assert_eq!(body["model"], "claude-fable-5-1");
    assert_eq!(*codex.seen.lock().unwrap(), ["gpt-6-astra"]);
}

#[tokio::test]
async fn healthy_codex_is_used_without_touching_claude_accounts() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6-luna-fast", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    let (status, body) = send(
        router,
        "gpt-6-luna-fast",
        Some(pool(&[("g1", "live-g1")])),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "codex");
    assert!(upstream.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn everything_exhausted_returns_the_first_anthropic_rejection() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6-luna", StatusCode::TOO_MANY_REQUESTS);
    let (status, body) = send(
        gateway(&upstream.base, vec![codex]),
        "claude-haiku-5-5",
        Some(pool(&[("d1", "dead-d1")])),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(body["error"]["message"], "exhausted");
}

#[tokio::test]
async fn thread_deltas_are_never_sent_to_a_different_account() {
    let upstream = anthropic_upstream().await;
    let (status, body) = send(
        gateway(&upstream.base, vec![]),
        "claude-fable-5-1",
        Some(pool(&[("e1", "dead-e1"), ("e2", "live-e2")])),
        true,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body["error"]["details"]["error_code"],
        "thread_unsupported_request"
    );
    let (status, body) = send(
        gateway(&upstream.base, vec![]),
        "claude-fable-5-1",
        Some(pool(&[("e1", "dead-e1"), ("e2", "live-e2")])),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "the resent full history is served");
    assert_eq!(body["servedBy"], "Bearer live-e2");
}

#[tokio::test]
async fn rejected_credentials_fall_to_a_healthy_account() {
    let upstream = anthropic_upstream().await;
    let accounts = pool(&[("auth-a", "invalid-a"), ("auth-b", "live-b")]);
    let router = gateway(&upstream.base, vec![]);
    for _ in 0..2 {
        let (status, body) = send(
            router.clone(),
            "claude-opus-5-5",
            Some(accounts.clone()),
            false,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["servedBy"], "Bearer live-b");
    }
    assert_eq!(
        upstream
            .seen
            .lock()
            .unwrap()
            .iter()
            .filter(|(auth, _)| auth == "Bearer invalid-a")
            .count(),
        1
    );
}

#[tokio::test]
async fn an_empty_claude_pool_can_use_codex() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6-astra", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    for model in ["gpt-6-astra", "claude-fable-5-1"] {
        let (status, body) = send(router.clone(), model, Some("[]".into()), false).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["servedBy"], "codex");
    }
    assert!(upstream.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn missing_thread_owner_is_not_replaced_by_the_first_usable_account() {
    let upstream = anthropic_upstream().await;
    let accounts =
        json!([{"alias":"thread-backup", "authorization":"Bearer live-backup", "primary":false}])
            .to_string();
    let (status, body) = send(
        gateway(&upstream.base, vec![]),
        "claude-opus-5-5",
        Some(accounts),
        true,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body["error"]["details"]["error_code"],
        "thread_unsupported_request"
    );
    assert!(upstream.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn healthy_thread_owner_receives_its_continuation() {
    let upstream = anthropic_upstream().await;
    let (status, body) = send(
        gateway(&upstream.base, vec![]),
        "claude-opus-5-5",
        Some(pool(&[("thread-owner", "live-owner")])),
        true,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "Bearer live-owner");
}

#[tokio::test]
async fn without_a_pool_the_rejection_passes_through_unchanged() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    let mut request = Request::builder()
        .method("POST")
        .uri("/v1/messages")
        .header("content-type", "application/json")
        .header("authorization", "Bearer dead-own");
    request = request.header("x-unrelated", "1");
    let response = router
        .oneshot(
            request
                .body(Body::from(
                    json!({"model":"claude-opus-5-5","max_tokens":1,"messages":[]}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
    assert!(codex.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_model_missing_on_one_account_is_served_by_another() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    let accounts = pool(&[("plain", "basic-a"), ("team", "live-b")]);
    let (status, body) = send(
        router.clone(),
        "claude-mythos-5-1",
        Some(accounts.clone()),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "Bearer live-b");
    // A model rejection must not disable other models or hide newly granted access.
    send(
        router.clone(),
        "claude-mythos-5-1",
        Some(accounts.clone()),
        false,
    )
    .await;
    let (_, body) = send(router, "claude-opus-5-5", Some(accounts), false).await;
    assert_eq!(body["servedBy"], "Bearer basic-a");
    let seen: Vec<_> = upstream
        .seen
        .lock()
        .unwrap()
        .iter()
        .map(|(auth, _)| auth.clone())
        .collect();
    assert_eq!(
        seen,
        [
            "Bearer basic-a",
            "Bearer live-b",
            "Bearer basic-a",
            "Bearer live-b",
            "Bearer basic-a"
        ]
    );
    assert!(codex.seen.lock().unwrap().is_empty());
}

#[tokio::test]
async fn model_unavailable_fallback_can_be_switched_off() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let (status, body) = send_with(
        router,
        "claude-mythos-5-1",
        Some(pool(&[("plain", "basic-a"), ("team", "live-b")])),
        false,
        Some(json!({"on": {"model_unavailable": false}})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["type"], "not_found_error");
    assert_eq!(upstream.seen.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn a_model_unavailable_thread_never_reaches_a_backup_without_full_history() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("plain", "basic-a"), ("team", "live-b")]);
    let (status, body) = send(
        router.clone(),
        "claude-mythos-5-1",
        Some(accounts.clone()),
        true,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body["error"]["details"]["error_code"],
        "thread_unsupported_request"
    );
    assert_eq!(upstream.seen.lock().unwrap().len(), 1);
    let (status, body) = send(router, "claude-mythos-5-1", Some(accounts), false).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "Bearer live-b");
}

#[tokio::test]
async fn a_model_rejection_does_not_hide_another_accounts_quota_limit() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("no-access", "basic-a"), ("with-access", "dead-b")]);
    for _ in 0..2 {
        let (status, body) = send(
            router.clone(),
            "claude-mythos-5-1",
            Some(accounts.clone()),
            false,
        )
        .await;
        assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(body["error"]["type"], "rate_limit_error");
    }
}

#[tokio::test]
async fn a_model_rejection_does_not_hide_another_accounts_expired_login() {
    let upstream = anthropic_upstream().await;
    let (status, body) = send(
        gateway(&upstream.base, vec![]),
        "claude-mythos-5-1",
        Some(pool(&[("no-access", "basic-a"), ("expired", "invalid-b")])),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(body["error"]["type"], "authentication_error");
}

#[tokio::test]
async fn a_model_missing_everywhere_returns_the_404() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("a", "basic-a"), ("b", "basic-b")]);
    for _ in 0..2 {
        let (status, body) = send(
            router.clone(),
            "claude-mythos-5-1",
            Some(accounts.clone()),
            false,
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(body["error"]["type"], "not_found_error");
    }
}

async fn count(
    router: Router,
    model: &str,
    pool: Option<String>,
    thread: bool,
) -> (StatusCode, Value) {
    let mut payload = json!({"model": model, "messages": [{"role":"user","content":"hi"}]});
    if thread {
        payload["thread"] = json!({"type": "continue", "previous_message_id": "msg_toy"});
    }
    let mut request = Request::builder()
        .method("POST")
        .uri("/v1/messages/count_tokens?beta=true")
        .header("content-type", "application/json")
        .header("authorization", "Bearer own-session-token");
    if let Some(pool) = pool {
        request = request.header(POOL, pool);
    }
    let response = router
        .oneshot(request.body(Body::from(payload.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), 100_000)
        .await
        .unwrap();
    (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
}

fn seen_auths(upstream: &Upstream) -> Vec<String> {
    upstream
        .seen
        .lock()
        .unwrap()
        .iter()
        .map(|(auth, _)| auth.clone())
        .collect()
}

#[tokio::test]
async fn a_count_for_a_model_missing_on_one_account_is_answered_by_another() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    let (status, body) = count(
        router,
        "claude-mythos-5-1",
        Some(pool(&[("plain", "basic-a"), ("team", "live-b")])),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "Bearer live-b");
    assert_eq!(body["model"], "claude-mythos-5-1");
    assert_eq!(seen_auths(&upstream), ["Bearer basic-a", "Bearer live-b"]);
    assert!(
        upstream
            .seen
            .lock()
            .unwrap()
            .iter()
            .all(|(_, pooled)| !pooled)
    );
    assert!(
        codex.seen.lock().unwrap().is_empty(),
        "counts never change vendor"
    );
}

#[tokio::test]
async fn a_count_for_a_model_missing_everywhere_returns_the_404() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("a", "basic-a"), ("b", "basic-b")]);
    for _ in 0..2 {
        let (status, body) = count(
            router.clone(),
            "claude-mythos-5-1",
            Some(accounts.clone()),
            false,
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(body["error"]["type"], "not_found_error");
    }
    assert_eq!(
        seen_auths(&upstream).len(),
        4,
        "a 404 never cools an account down"
    );
}

#[tokio::test]
async fn counts_neither_cool_down_messages_nor_move_thread_deltas() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("spent", "dead-a"), ("fresh", "live-b")]);
    let (status, body) = count(
        router.clone(),
        "claude-opus-5-5",
        Some(accounts.clone()),
        false,
    )
    .await;
    assert_eq!(
        (status, body["servedBy"].clone()),
        (StatusCode::OK, json!("Bearer live-b"))
    );
    // The count's 429 recorded nothing: the message still tries the first account.
    send(
        router.clone(),
        "claude-opus-5-5",
        Some(accounts.clone()),
        false,
    )
    .await;
    assert_eq!(
        seen_auths(&upstream),
        [
            "Bearer dead-a",
            "Bearer live-b",
            "Bearer dead-a",
            "Bearer live-b"
        ]
    );
    let mythos = pool(&[("plain", "basic-a"), ("team", "live-b")]);
    let (status, _) = count(router, "claude-mythos-5-1", Some(mythos), true).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "a contextless delta stays on its own account"
    );
    assert_eq!(seen_auths(&upstream).len(), 5);
}

#[tokio::test]
async fn custom_tier_discovers_a_cold_codex_destination_before_routing() {
    if std::env::var_os("CCP_TEST_COLD_CATALOG_CHILD").is_none() {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "custom_tier_discovers_a_cold_codex_destination_before_routing",
                "--nocapture",
            ])
            .env("CCP_TEST_COLD_CATALOG_CHILD", "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "isolated catalog test failed:\n{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }
    let home = tempfile::tempdir().unwrap();
    std::fs::write(
        home.path().join("auth.json"),
        json!({"tokens":{"access_token":"toy-token","account_id":"toy-account"}}).to_string(),
    )
    .unwrap();
    let hits = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let seen = hits.clone();
    let catalog = Router::new().route("/models", axum::routing::get(move || {
        let seen = seen.clone();
        async move {
            seen.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            axum::Json(json!({"models":[{"slug":"gpt-6.10-sol","visibility":"list","supported_in_api":true}]}))
        }
    }));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}/responses", listener.local_addr().unwrap());
    let catalog_task = tokio::spawn(async move { axum::serve(listener, catalog).await.unwrap() });
    let previous: Vec<_> = ["CODEX_HOME", "CCP_CODEX_BASE_URL", "NO_PROXY"]
        .into_iter()
        .map(|key| (key, std::env::var_os(key)))
        .collect();
    unsafe {
        std::env::set_var("CODEX_HOME", home.path());
        std::env::set_var("CCP_CODEX_BASE_URL", base);
        std::env::set_var("NO_PROXY", "127.0.0.1");
    }
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::OK);
    let (status, body) = send_with(
        gateway(&upstream.base, vec![codex]),
        "claude-opus-5-5",
        Some(pool(&[("a", "dead-a")])),
        false,
        Some(
            json!({"tiers":[{"name":"custom","match":["opus"],"models":{"codex":"gpt-6.10-sol"}}]}),
        ),
    )
    .await;
    for (key, value) in previous {
        unsafe {
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
    }
    catalog_task.abort();
    assert_eq!(
        status,
        StatusCode::OK,
        "a discovered custom destination must not be silently skipped"
    );
    assert_eq!(body["model"], "gpt-6.10-sol");
    assert_eq!(hits.load(std::sync::atomic::Ordering::SeqCst), 1);
}

const SERVER_ERRORS: &str = r#"{"on":{"server_error":true}}"#;

async fn turn(
    router: Router,
    path: &str,
    who: &[&str],
    payload: Value,
    pool: String,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .header("authorization", "Bearer own-session-token")
        .header(POOL, pool)
        .header(POLICY, SERVER_ERRORS);
    for (header, value) in ["x-claude-code-session-id", "x-claude-code-agent-id"]
        .into_iter()
        .zip(who)
    {
        request = request.header(header, *value);
    }
    let response = router
        .oneshot(request.body(Body::from(payload.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), 100_000).await;
    let body = body
        .ok()
        .and_then(|body| serde_json::from_slice(&body).ok())
        .unwrap_or(Value::Null);
    (status, body)
}

fn hi(model: &str) -> Value {
    json!({"model": model, "max_tokens": 1, "messages": [{"role":"user","content":"hi"}]})
}

async fn served(router: &Router, who: &[&str], model: &str, accounts: &str) -> Value {
    let (status, body) = turn(
        router.clone(),
        "/v1/messages",
        who,
        hi(model),
        accounts.into(),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    body["servedBy"].clone()
}

#[tokio::test]
async fn a_conversation_stays_on_its_fallback_after_the_first_target_recovers() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("first", "flaky-1"), ("second", "live-2")]);
    upstream.flaky.store(true, Ordering::SeqCst);
    assert_eq!(
        served(&router, &["s1"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    upstream.flaky.store(false, Ordering::SeqCst);
    assert_eq!(
        served(&router, &["s1"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    assert_eq!(
        served(&router, &["s2"], "claude-opus-5-5", &accounts).await,
        "Bearer flaky-1",
        "another session keeps its own route"
    );
    assert_eq!(
        served(&router, &["s1", "agent"], "claude-opus-5-5", &accounts).await,
        "Bearer flaky-1",
        "a subagent keeps its own route"
    );
    let other_owner = pool(&[("third", "flaky-3"), ("second", "live-2")]);
    assert_eq!(
        served(&router, &["s1"], "claude-opus-5-5", &other_owner).await,
        "Bearer live-2",
        "the conversation's route does not depend on the primary alias"
    );
    assert_eq!(
        served(&router, &["s1"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
}

async fn continuity_over_tcp(backup: &str, streamed: bool) {
    let upstream = anthropic_upstream().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/v1/messages", listener.local_addr().unwrap());
    let router = gateway(&upstream.base, vec![]);
    let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let client = reqwest::Client::builder()
        .no_proxy()
        .http1_only()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .unwrap();
    let accounts = pool(&[("first", "flaky-1"), ("second", backup)]);
    for failing in [true, false] {
        upstream.flaky.store(failing, Ordering::SeqCst);
        let mut payload = hi("claude-opus-5-5");
        payload["stream"] = json!(streamed);
        let response = client
            .post(&url)
            .header("authorization", "Bearer own-session-token")
            .header("x-claude-code-session-id", "tcp-session")
            .header(POOL, &accounts)
            .header(POLICY, SERVER_ERRORS)
            .json(&payload)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let length = response.headers()["content-length"]
            .to_str()
            .unwrap()
            .parse::<usize>()
            .unwrap();
        let bytes = response.bytes().await.unwrap();
        assert_eq!(bytes.len(), length);
        if streamed {
            assert_eq!(bytes.as_ref(), sse_reply("done").as_bytes());
        } else {
            let body: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(body["servedBy"], format!("Bearer {backup}"));
        }
    }
    assert_eq!(
        seen_auths(&upstream),
        [
            "Bearer flaky-1".to_string(),
            format!("Bearer {backup}"),
            format!("Bearer {backup}")
        ]
    );
    task.abort();
}

#[tokio::test]
async fn non_streaming_continuity_survives_content_length_over_tcp() {
    continuity_over_tcp("live-2", false).await;
}

#[tokio::test]
async fn streaming_continuity_survives_content_length_over_tcp() {
    continuity_over_tcp("sse-done", true).await;
}

#[tokio::test]
async fn continuity_survives_an_absent_primary_returning_but_not_an_absent_target() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let absent =
        json!([{"alias":"second", "authorization":"Bearer live-2", "primary":false}]).to_string();
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &absent).await,
        "Bearer live-2"
    );
    let restored = pool(&[("first", "live-1"), ("second", "live-2")]);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &restored).await,
        "Bearer live-2"
    );
    assert_eq!(
        served(&router, &["s", "agent"], "claude-opus-5-5", &restored).await,
        "Bearer live-1"
    );
    let removed = pool(&[("first", "live-1")]);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &removed).await,
        "Bearer live-1"
    );
    assert_eq!(
        seen_auths(&upstream),
        [
            "Bearer live-2",
            "Bearer live-2",
            "Bearer live-1",
            "Bearer live-1"
        ]
    );
}

#[tokio::test]
async fn changing_the_requested_model_forgets_the_route_even_after_changing_back() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("first", "flaky-1"), ("second", "live-2")]);
    upstream.flaky.store(true, Ordering::SeqCst);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    upstream.flaky.store(false, Ordering::SeqCst);
    assert_eq!(
        served(&router, &["s"], "claude-sonnet-5-5", &accounts).await,
        "Bearer flaky-1"
    );
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer flaky-1"
    );
}

#[tokio::test]
async fn the_route_follows_the_alias_and_never_revives_a_removed_target() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    upstream.flaky.store(true, Ordering::SeqCst);
    let accounts = pool(&[
        ("first", "flaky-1"),
        ("second", "live-2"),
        ("third", "live-3"),
    ]);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    upstream.flaky.store(false, Ordering::SeqCst);
    let reordered = pool(&[
        ("first", "flaky-1"),
        ("third", "live-3"),
        ("second", "live-2"),
    ]);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &reordered).await,
        "Bearer live-2"
    );
    let removed = pool(&[("first", "flaky-1"), ("third", "live-3")]);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &removed).await,
        "Bearer flaky-1"
    );
}

#[tokio::test]
async fn a_failing_remembered_target_falls_back_in_the_configured_order() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    upstream.flaky.store(true, Ordering::SeqCst);
    let accounts = pool(&[
        ("first", "flaky-1"),
        ("second", "live-2"),
        ("third", "live-3"),
    ]);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    upstream.flaky.store(false, Ordering::SeqCst);
    upstream.seen.lock().unwrap().clear();
    let spent = pool(&[
        ("first", "flaky-1"),
        ("second", "dead-2"),
        ("third", "live-3"),
    ]);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &spent).await,
        "Bearer flaky-1"
    );
    assert_eq!(seen_auths(&upstream), ["Bearer dead-2", "Bearer flaky-1"]);
}

#[tokio::test]
async fn counts_never_set_a_route() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("first", "flaky-1"), ("second", "live-2")]);
    upstream.flaky.store(true, Ordering::SeqCst);
    let count = json!({"model": "claude-opus-5-5", "messages": [{"role":"user","content":"hi"}]});
    let (status, body) = turn(
        router.clone(),
        "/v1/messages/count_tokens",
        &["s"],
        count,
        accounts.clone(),
    )
    .await;
    assert_eq!(
        (status, &body["servedBy"]),
        (StatusCode::OK, &json!("Bearer live-2"))
    );
    upstream.flaky.store(false, Ordering::SeqCst);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer flaky-1"
    );
}

#[tokio::test]
async fn refusals_and_broken_bodies_never_set_a_route() {
    for (backup, broken) in [("refuse-2", false), ("abort-2", true)] {
        let upstream = anthropic_upstream().await;
        let router = gateway(&upstream.base, vec![]);
        let accounts = pool(&[("first", "flaky-1"), ("second", backup)]);
        upstream.flaky.store(true, Ordering::SeqCst);
        let (status, body) = turn(
            router.clone(),
            "/v1/messages",
            &["s"],
            hi("claude-opus-5-5"),
            accounts.clone(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body.is_null(), broken, "{body}");
        upstream.flaky.store(false, Ordering::SeqCst);
        assert_eq!(
            served(&router, &["s"], "claude-opus-5-5", &accounts).await,
            "Bearer flaky-1"
        );
        assert_eq!(seen_auths(&upstream).len(), 3, "the backup was not retried");
    }
}

#[tokio::test]
async fn a_thread_delta_never_follows_the_route_to_another_account() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("first", "flaky-1"), ("second", "live-2")]);
    upstream.flaky.store(true, Ordering::SeqCst);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    upstream.flaky.store(false, Ordering::SeqCst);
    let mut delta = hi("claude-opus-5-5");
    delta["thread"] = json!({"type": "continue", "previous_message_id": "msg_toy"});
    let (status, body) = turn(router, "/v1/messages", &["s"], delta, accounts).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body["error"]["details"]["error_code"],
        "thread_unsupported_request"
    );
    assert_eq!(
        seen_auths(&upstream).len(),
        2,
        "nothing reached any account"
    );
}

#[tokio::test]
async fn a_cross_provider_claude_destination_gets_thinking_it_accepts() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6-astra", StatusCode::TOO_MANY_REQUESTS);
    let router = gateway(&upstream.base, vec![codex]);
    let accounts = pool(&[("only", "live-1")]);
    let cases = [
        (
            json!({"type": "enabled", "budget_tokens": 4000}),
            json!("high"),
            json!("high"),
        ),
        (
            json!({"type": "enabled", "budget_tokens": 4000}),
            Value::Null,
            Value::Null,
        ),
        (json!({"type": "disabled"}), Value::Null, json!("low")),
        (json!({"type": "disabled"}), json!("max"), json!("max")),
    ];
    for (thinking, effort, expected) in cases {
        let mut payload = hi("gpt-6-astra");
        payload["thinking"] = thinking;
        if !effort.is_null() {
            payload["output_config"] = json!({"effort": effort});
        }
        let (status, body) = turn(
            router.clone(),
            "/v1/messages",
            &[],
            payload,
            accounts.clone(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["model"], "claude-fable-5-1");
        assert_eq!(body["thinking"], json!({"type": "adaptive"}));
        assert_eq!(body["effort"], expected);
    }
    let mut direct = hi("claude-fable-5-1");
    direct["thinking"] = json!({"type": "enabled", "budget_tokens": 4000});
    let (_, body) = turn(router, "/v1/messages", &[], direct.clone(), accounts).await;
    assert_eq!(
        body["thinking"], direct["thinking"],
        "a direct request is untouched"
    );
}

#[tokio::test]
async fn a_kimi_k3_substitute_never_receives_disabled_thinking() {
    let upstream = anthropic_upstream().await;
    let kimi = stub("kimi", "kimi-k3", StatusCode::OK);
    let router = gateway(&upstream.base, vec![kimi.clone()]);
    let chains = r#"{"chains":{"claude":["accounts","kimi"]}}"#;
    for thinking in [
        json!({"type": "disabled"}),
        json!({"type": "enabled", "budget_tokens": 2000}),
    ] {
        let mut payload = hi("claude-sonnet-5-5");
        payload["thinking"] = thinking;
        let request = Request::builder()
            .method("POST")
            .uri("/v1/messages")
            .header("content-type", "application/json")
            .header(POOL, pool(&[("spent", "dead-1")]))
            .header(POLICY, chains)
            .body(Body::from(payload.to_string()))
            .unwrap();
        let response = router.clone().oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }
    assert_eq!(
        *kimi.thinking.lock().unwrap(),
        [
            (json!({"type": "adaptive"}), json!("low")),
            (
                json!({"type": "enabled", "budget_tokens": 2000}),
                Value::Null
            ),
        ]
    );
}

fn last_auth(upstream: &Upstream) -> String {
    seen_auths(upstream).pop().unwrap_or_default()
}

#[tokio::test]
async fn only_a_completed_turn_sets_a_route() {
    for (backup, completes) in [
        ("sse-done", true),
        ("sse-spaced-done", true),
        ("tool-error", true),
        ("sse-spaced-refusal", false),
        ("sse-truncated", false),
        ("sse-unterminated", false),
        ("sse-error", false),
    ] {
        let upstream = anthropic_upstream().await;
        let router = gateway(&upstream.base, vec![]);
        let accounts = pool(&[("first", "flaky-1"), ("second", backup)]);
        upstream.flaky.store(true, Ordering::SeqCst);
        let (status, _) = turn(
            router.clone(),
            "/v1/messages",
            &["s"],
            hi("claude-opus-5-5"),
            accounts.clone(),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{backup}");
        upstream.flaky.store(false, Ordering::SeqCst);
        turn(
            router,
            "/v1/messages",
            &["s"],
            hi("claude-opus-5-5"),
            accounts,
        )
        .await;
        let expected = if completes {
            format!("Bearer {backup}")
        } else {
            "Bearer flaky-1".into()
        };
        assert_eq!(last_auth(&upstream), expected, "{backup}");
    }
}

async fn open(router: &Router, model: &str, accounts: &str) -> Response {
    let request = Request::builder()
        .method("POST")
        .uri("/v1/messages")
        .header("content-type", "application/json")
        .header(POOL, accounts)
        .header(POLICY, SERVER_ERRORS)
        .header("x-claude-code-session-id", "s")
        .body(Body::from(hi(model).to_string()))
        .unwrap();
    router.clone().oneshot(request).await.unwrap()
}

#[tokio::test]
async fn an_older_turn_finishing_late_never_overwrites_a_newer_one() {
    for newer_model in ["claude-opus-5-5", "claude-sonnet-5-5"] {
        let upstream = anthropic_upstream().await;
        let router = gateway(&upstream.base, vec![]);
        let accounts = pool(&[("first", "flaky-1"), ("second", "slow-2")]);
        upstream.flaky.store(true, Ordering::SeqCst);
        let older = open(&router, "claude-opus-5-5", &accounts).await;
        assert_eq!(older.status(), StatusCode::OK);
        assert_eq!(last_auth(&upstream), "Bearer slow-2");
        upstream.flaky.store(false, Ordering::SeqCst);
        assert_eq!(
            served(&router, &["s"], newer_model, &accounts).await,
            "Bearer flaky-1"
        );
        upstream.gate.notify_one();
        let finished = axum::body::to_bytes(older.into_body(), 100_000)
            .await
            .unwrap();
        assert!(String::from_utf8_lossy(&finished).contains("message_stop"));
        upstream.gate.notify_one();
        assert_eq!(
            served(&router, &["s"], "claude-opus-5-5", &accounts).await,
            "Bearer flaky-1",
            "after {newer_model}"
        );
    }
}

#[tokio::test]
async fn helper_calls_never_move_or_reset_the_route() {
    let upstream = anthropic_upstream().await;
    let router = gateway(&upstream.base, vec![]);
    let accounts = pool(&[("first", "flaky-1"), ("second", "live-2")]);
    upstream.flaky.store(true, Ordering::SeqCst);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    upstream.flaky.store(false, Ordering::SeqCst);
    let system = |text: &str| json!([{"type": "text", "text": text}]);
    let mut title = hi("claude-haiku-5-5");
    title["system"] = system(
        "You are naming a coding session so the user can pick it out of a long list of sessions. The title is a name.",
    );
    let mut review = hi("claude-sonnet-5-5");
    review["system"] =
        system("You are a security monitor for autonomous AI coding agents. Decide.");
    let mut compaction = hi("claude-sonnet-5-5");
    compaction["system"] =
        system("You are a helpful AI assistant tasked with summarizing conversations.");
    for helper in [title, review, compaction] {
        let (status, body) = turn(
            router.clone(),
            "/v1/messages",
            &["s"],
            helper,
            accounts.clone(),
        )
        .await;
        assert_eq!(
            (status, &body["servedBy"]),
            (StatusCode::OK, &json!("Bearer flaky-1"))
        );
    }
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    assert_eq!(
        served(&router, &["s"], "claude-haiku-5-5", &accounts).await,
        "Bearer flaky-1",
        "an ordinary model change still resets"
    );
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer flaky-1"
    );
}

#[tokio::test]
async fn a_model_change_without_a_fallback_chain_still_forgets_the_route() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone()]);
    let accounts = pool(&[("first", "flaky-1"), ("second", "live-2")]);
    upstream.flaky.store(true, Ordering::SeqCst);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer live-2"
    );
    upstream.flaky.store(false, Ordering::SeqCst);
    let request = Request::builder()
        .method("POST")
        .uri("/v1/messages")
        .header("content-type", "application/json")
        .header(POOL, accounts.clone())
        .header(
            POLICY,
            r#"{"chains":{"codex":["codex"]},"on":{"server_error":true}}"#,
        )
        .header("x-claude-code-session-id", "s")
        .body(Body::from(hi("gpt-6.1-sol").to_string()))
        .unwrap();
    assert_eq!(
        router.clone().oneshot(request).await.unwrap().status(),
        StatusCode::OK
    );
    assert_eq!(*codex.seen.lock().unwrap(), ["gpt-6.1-sol"]);
    assert_eq!(
        served(&router, &["s"], "claude-opus-5-5", &accounts).await,
        "Bearer flaky-1"
    );
}

async fn served_by(router: Router, model: &str, accounts: &str) -> (StatusCode, String, Value) {
    let request = Request::builder()
        .method("POST")
        .uri("/v1/messages")
        .header("content-type", "application/json")
        .header(POOL, accounts)
        .body(Body::from(hi(model).to_string()))
        .unwrap();
    let response = router.oneshot(request).await.unwrap();
    let status = response.status();
    let header = response
        .headers()
        .get("x-claude-codex-served-by")
        .map(|value| value.to_str().unwrap().to_string())
        .unwrap_or_default();
    let body = axum::body::to_bytes(response.into_body(), 100_000)
        .await
        .unwrap();
    (status, header, serde_json::from_slice(&body).unwrap())
}

#[tokio::test]
async fn opus_4_8_is_served_by_kimi_k3_1_pro_and_falls_back_along_kimis_chain() {
    let upstream = anthropic_upstream().await;
    let kimi = stub("kimi", "kimi-k3.1-pro", StatusCode::OK);
    let router = gateway(&upstream.base, vec![kimi.clone()]);
    let accounts = pool(&[("a1", "live-a1")]);

    let (status, header, body) = served_by(router.clone(), "claude-opus-4-8[1m]", &accounts).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(header, "kimi/kimi-k3.1-pro");
    assert_eq!(*kimi.seen.lock().unwrap(), ["kimi-k3.1-pro"]);
    assert!(
        upstream.seen.lock().unwrap().is_empty(),
        "Anthropic never sees it"
    );

    let (status, header, body) = served_by(router.clone(), "claude-opus-5-5", &accounts).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(header, "a1/claude-opus-5-5");
    assert_eq!(body["model"], "claude-opus-5-5");

    *kimi.status.lock().unwrap() = StatusCode::TOO_MANY_REQUESTS;
    let (status, header, body) = served_by(router, "claude-opus-4-8", &accounts).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        header, "a1/claude-fable-5-1",
        "kimi's chain, in kimi-k3.1-pro's tier"
    );
}

#[tokio::test]
async fn a_conversation_returns_to_its_requested_model_once_that_model_recovers() {
    let upstream = anthropic_upstream().await;
    let kimi = stub("kimi", "kimi-k3.1-pro", StatusCode::SERVICE_UNAVAILABLE);
    let router = gateway(&upstream.base, vec![kimi.clone()]);
    let accounts = pool(&[("a1", "live-a1"), ("a2", "live-a2")]);
    assert_eq!(
        served(&router, &["s1"], "kimi-k3.1-pro", &accounts).await,
        "Bearer live-a1",
        "kimi down: its tier's Claude model answers"
    );
    *kimi.status.lock().unwrap() = StatusCode::OK;
    assert_eq!(
        served(&router, &["s1"], "kimi-k3.1-pro", &accounts).await,
        "kimi",
        "a remembered fallback to another model never outranks the requested model"
    );
    *kimi.status.lock().unwrap() = StatusCode::SERVICE_UNAVAILABLE;
    let other_first = pool(&[("a2", "live-a2"), ("a1", "live-a1")]);
    assert_eq!(
        served(&router, &["s2"], "kimi-k3.1-pro", &accounts).await,
        "Bearer live-a1"
    );
    assert_eq!(
        served(&router, &["s2"], "kimi-k3.1-pro", &other_first).await,
        "Bearer live-a1",
        "while kimi is down the conversation keeps its fallback account"
    );
    assert_eq!(
        *kimi.seen.lock().unwrap(),
        ["kimi-k3.1-pro"; 4],
        "kimi is asked first on every turn"
    );
}
