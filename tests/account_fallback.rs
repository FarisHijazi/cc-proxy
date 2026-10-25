//! Quota failover: Claude accounts in order, then the Codex tier, then Cursor.
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
use std::sync::{Arc, Mutex};
use tokio::{net::TcpListener, task::JoinHandle};
use tower::ServiceExt;

const POOL: &str = "x-claude-codex-account-pool";

struct Upstream {
    base: String,
    seen: Arc<Mutex<Vec<(String, bool)>>>,
    task: JoinHandle<()>,
}
impl Drop for Upstream {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Anthropic stand-in: any bearer starting with `Bearer dead` is out of quota.
async fn anthropic_upstream() -> Upstream {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let router = Router::new().fallback(move |req: Request<Body>| {
        let log = log.clone();
        async move {
            let auth = req.headers()["authorization"].to_str().unwrap().to_string();
            log.lock()
                .unwrap()
                .push((auth.clone(), req.headers().contains_key(POOL)));
            let body = axum::body::to_bytes(req.into_body(), 1_000_000).await.unwrap();
            let model = serde_json::from_slice::<Value>(&body).ok().and_then(|v| v["model"].as_str().map(str::to_string));
            if auth.starts_with("Bearer invalid") {
                return (StatusCode::UNAUTHORIZED, axum::Json(json!({"type":"error","error":{"type":"authentication_error","message":"expired"}}))).into_response();
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
            axum::Json(json!({"servedBy": auth, "model": model})).into_response()
        }
    });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    Upstream { base, seen, task }
}

struct Stub {
    name: &'static str,
    model: &'static str,
    status: StatusCode,
    seen: Arc<Mutex<Vec<String>>>,
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
        (
            self.status,
            axum::Json(json!({"servedBy": self.name, "model": model})),
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
        status,
        seen: Arc::default(),
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
async fn all_accounts_exhausted_uses_codex_tier_then_cursor() {
    let upstream = anthropic_upstream().await;
    let codex = stub("codex", "gpt-6.1-sol", StatusCode::TOO_MANY_REQUESTS);
    let cursor = stub("cursor", "cursor:claude-opus-5", StatusCode::OK);
    let router = gateway(&upstream.base, vec![codex.clone(), cursor.clone()]);
    let (status, body) = send(
        router,
        "claude-opus-5-5[1m]",
        Some(pool(&[("c1", "dead-c1"), ("c2", "dead-c2")])),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "cursor");
    assert_eq!(*codex.seen.lock().unwrap(), ["gpt-6.1-sol"]);
    assert_eq!(*cursor.seen.lock().unwrap(), ["cursor:claude-opus-5"]);
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
