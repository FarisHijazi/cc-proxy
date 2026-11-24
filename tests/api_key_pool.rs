//! A Console API key as a pool member: sent as `x-api-key`, tried first when it leads the pool, and
//! dropped until next month once its credit or workspace spend limit runs out.
use axum::{
    Router,
    body::Body,
    http::{HeaderMap, Request, StatusCode},
    response::IntoResponse,
};
use claude_codex::{
    config::AliasProvider, provider::Provider, providers::anthropic::AnthropicProvider,
    registry::Registry, server::app,
};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tokio::{net::TcpListener, task::JoinHandle};
use tower::ServiceExt;

const POOL: &str = "x-claude-codex-account-pool";
const BETAS: &str = "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14";
const CREDIT: &str = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.";
/// Recorded on ftower 2026-11-23 (traffic DB), served with `content-encoding: br`.
const REAL_CREDIT: &str = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.";
const LIMIT: &str = "You have reached your specified workspace API usage limits. You will regain access on 2026-12-01 at 00:00 UTC.";

#[derive(Debug, Clone, PartialEq)]
struct Seen {
    credential: String,
    beta: Option<String>,
    both: bool,
    encoding: Option<String>,
}

struct Upstream {
    base: String,
    seen: Arc<Mutex<Vec<Seen>>>,
    task: JoinHandle<()>,
}
impl Drop for Upstream {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn header(headers: &HeaderMap, name: &str) -> Option<String> {
    headers.get(name).map(|v| v.to_str().unwrap().to_string())
}

/// Anthropic stand-in. Keys: `key-broke` has no credit, `key-limit` hit its workspace limit,
/// `key-busy` is rate limited, `key-invalid` sends a plain bad-request 400; anything else answers.
async fn upstream() -> Upstream {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let router = Router::new().fallback(move |req: Request<Body>| {
        let log = log.clone();
        async move {
            let headers = req.headers();
            let key = header(headers, "x-api-key");
            let auth = header(headers, "authorization");
            let credential = key
                .clone()
                .map(|k| format!("key:{k}"))
                .or(auth.clone())
                .unwrap_or_default();
            log.lock().unwrap().push(Seen {
                credential: credential.clone(),
                beta: header(headers, "anthropic-beta"),
                both: key.is_some() && auth.is_some(),
                encoding: header(headers, "accept-encoding"),
            });
            // Anthropic compresses error bodies when asked; the router never decompresses.
            let compressed = |status: StatusCode| {
                (
                    status,
                    [
                        ("content-type", "application/json"),
                        ("content-encoding", "br"),
                    ],
                    vec![0x1b_u8, 0x8f, 0x01, 0xf8, 0x05],
                )
                    .into_response()
            };
            let accepts_br = header(headers, "accept-encoding").is_some_and(|e| e.contains("br"));
            let error = |status: StatusCode, kind: &str, message: &str| {
                (
                    status,
                    axum::Json(
                        json!({"type": "error", "error": {"type": kind, "message": message}}),
                    ),
                )
                    .into_response()
            };
            match key.as_deref() {
                Some("key-broke-real") if accepts_br => compressed(StatusCode::BAD_REQUEST),
                Some("key-broke-real") => error(
                    StatusCode::BAD_REQUEST,
                    "invalid_request_error",
                    REAL_CREDIT,
                ),
                Some("key-garbled") => compressed(StatusCode::BAD_REQUEST),
                Some("key-billing-400") => error(
                    StatusCode::BAD_REQUEST,
                    "billing_error",
                    "Credit balance is too low",
                ),
                Some("key-broke") => {
                    error(StatusCode::BAD_REQUEST, "invalid_request_error", CREDIT)
                }
                Some("key-limit") => error(StatusCode::BAD_REQUEST, "invalid_request_error", LIMIT),
                Some("key-billing") => {
                    error(StatusCode::PAYMENT_REQUIRED, "billing_error", "billing")
                }
                Some("key-busy") => error(
                    StatusCode::TOO_MANY_REQUESTS,
                    "rate_limit_error",
                    "slow down",
                ),
                Some("key-invalid") => error(
                    StatusCode::BAD_REQUEST,
                    "invalid_request_error",
                    "messages: bad",
                ),
                _ => axum::Json(
                    json!({"type": "message", "stop_reason": "end_turn", "servedBy": credential}),
                )
                .into_response(),
            }
        }
    });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    Upstream { base, seen, task }
}

fn gateway(base: &str) -> Router {
    let providers: Vec<Arc<dyn Provider>> = vec![Arc::new(AnthropicProvider::with_base_url(base))];
    app(Arc::new(Registry::from_providers(
        AliasProvider::Anthropic,
        providers,
    )))
}

/// The key leads the pool and owns the thread, as `account_inject` builds it for a preferred key.
fn pool(key: &str) -> String {
    json!([
        {"alias": "console", "kind": "api_key", "api_key": key, "primary": true},
        {"alias": "sub", "authorization": "Bearer sub-token", "primary": false},
    ])
    .to_string()
}

async fn send(
    router: &Router,
    model: &str,
    pool: &str,
    thread: bool,
    count: bool,
) -> (StatusCode, Value) {
    let mut payload =
        json!({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]});
    if thread {
        payload["thread"] = json!({"type": "continue", "previous_message_id": "msg_toy"});
    }
    let path = if count {
        "/v1/messages/count_tokens?beta=true"
    } else {
        "/v1/messages?beta=true"
    };
    let request = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .header("authorization", "Bearer sub-token")
        .header("anthropic-beta", BETAS)
        .header("accept-encoding", "gzip, deflate, br, zstd")
        .header(POOL, pool)
        .body(Body::from(payload.to_string()))
        .unwrap();
    let response = router.clone().oneshot(request).await.unwrap();
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), 100_000)
        .await
        .unwrap();
    (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
}

fn credentials(upstream: &Upstream) -> Vec<String> {
    upstream
        .seen
        .lock()
        .unwrap()
        .iter()
        .map(|s| s.credential.clone())
        .collect()
}

#[tokio::test]
async fn a_key_is_sent_as_x_api_key_without_the_login_and_with_the_request_unchanged() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    let (status, body) = send(&router, "claude-haiku-4-5", &pool("key-live"), false, false).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "key:key-live");
    let seen = upstream.seen.lock().unwrap()[0].clone();
    assert!(
        !seen.both,
        "the subscription login never rides along with the key"
    );
    // Measured live: a Console key accepts Claude Code's OAuth beta flags, so they stay.
    assert_eq!(seen.beta.as_deref(), Some(BETAS));
}

#[tokio::test]
async fn a_subscription_after_the_key_gets_its_own_login_and_the_original_betas() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    let (status, body) = send(&router, "claude-haiku-4-5", &pool("key-busy"), false, false).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["servedBy"], "Bearer sub-token");
    let seen = upstream.seen.lock().unwrap()[1].clone();
    assert_eq!(
        seen,
        Seen {
            credential: "Bearer sub-token".into(),
            beta: Some(BETAS.into()),
            both: false,
            encoding: Some("gzip, deflate, br, zstd".into()),
        }
    );
}

#[tokio::test]
async fn an_empty_credit_balance_falls_back_and_drops_the_key_for_every_model() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    let (status, body) = send(
        &router,
        "claude-haiku-4-5",
        &pool("key-broke"),
        false,
        false,
    )
    .await;
    assert_eq!(
        (status, body["servedBy"].clone()),
        (StatusCode::OK, json!("Bearer sub-token"))
    );
    send(
        &router,
        "claude-haiku-4-5",
        &pool("key-broke"),
        false,
        false,
    )
    .await;
    send(&router, "claude-opus-5-5", &pool("key-broke"), false, false).await;
    send(&router, "claude-haiku-4-5", &pool("key-broke"), false, true).await;
    assert_eq!(
        credentials(&upstream),
        [
            "key:key-broke",
            "Bearer sub-token",
            "Bearer sub-token",
            "Bearer sub-token",
            "Bearer sub-token"
        ],
        "messages and counts skip the exhausted key on every model"
    );
}

#[tokio::test]
async fn a_reached_workspace_spend_limit_or_a_billing_error_falls_back_like_empty_credit() {
    for key in ["key-limit", "key-billing"] {
        let upstream = upstream().await;
        let router = gateway(&upstream.base);
        send(&router, "claude-haiku-4-5", &pool(key), false, false).await;
        send(&router, "claude-sonnet-5-5", &pool(key), false, false).await;
        assert_eq!(
            credentials(&upstream),
            [
                format!("key:{key}"),
                "Bearer sub-token".into(),
                "Bearer sub-token".into()
            ]
        );
    }
}

#[tokio::test]
async fn any_other_bad_request_from_the_key_is_returned_and_never_retried() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    let (status, body) = send(
        &router,
        "claude-haiku-4-5",
        &pool("key-invalid"),
        false,
        false,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["message"], "messages: bad");
    assert_eq!(credentials(&upstream), ["key:key-invalid"]);
}

#[tokio::test]
async fn a_rate_limited_key_cools_only_that_model() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    send(&router, "claude-haiku-4-5", &pool("key-busy"), false, false).await;
    send(&router, "claude-opus-5-5", &pool("key-busy"), false, false).await;
    assert_eq!(
        credentials(&upstream),
        [
            "key:key-busy",
            "Bearer sub-token",
            "key:key-busy",
            "Bearer sub-token"
        ]
    );
}

#[tokio::test]
async fn a_thread_started_on_the_key_continues_there_and_never_moves_when_it_runs_out() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    let (status, body) = send(&router, "claude-haiku-4-5", &pool("key-live"), true, false).await;
    assert_eq!(
        (status, body["servedBy"].clone()),
        (StatusCode::OK, json!("key:key-live"))
    );
    send(
        &router,
        "claude-haiku-4-5",
        &pool("key-broke"),
        false,
        false,
    )
    .await;
    let (status, body) = send(&router, "claude-haiku-4-5", &pool("key-broke"), true, false).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body["error"]["details"]["error_code"],
        "thread_unsupported_request"
    );
}

#[tokio::test]
async fn when_everything_is_exhausted_the_client_sees_a_retryable_429_not_the_credit_error() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    let pool = json!([
        {"alias": "console", "kind": "api_key", "api_key": "key-broke", "primary": true},
        {"alias": "busy", "kind": "api_key", "api_key": "key-busy", "primary": false},
    ])
    .to_string();
    let (status, body) = send(&router, "claude-haiku-4-5", &pool, false, false).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(body["error"]["message"], "slow down");
}

#[tokio::test]
async fn the_real_compressed_credit_error_falls_back_and_never_reaches_the_client() {
    // 2026-11-23: Anthropic sent this 400 brotli-compressed, the router could not read it, and
    // Claude Code showed "credit balance is too low" instead of being served by a subscription.
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    let (status, body) = send(
        &router,
        "claude-opus-5-5",
        &pool("key-broke-real"),
        false,
        false,
    )
    .await;
    assert_eq!(
        (status, body["servedBy"].clone()),
        (StatusCode::OK, json!("Bearer sub-token"))
    );
    send(
        &router,
        "claude-sonnet-5-5",
        &pool("key-broke-real"),
        false,
        false,
    )
    .await;
    assert_eq!(
        credentials(&upstream),
        ["key:key-broke-real", "Bearer sub-token", "Bearer sub-token"]
    );
    let seen = upstream.seen.lock().unwrap().clone();
    assert_eq!(
        seen[0].encoding.as_deref(),
        Some("identity"),
        "key errors come back readable"
    );
    assert_eq!(
        seen[1].encoding.as_deref(),
        Some("gzip, deflate, br, zstd"),
        "a login's request is unchanged"
    );
}

#[tokio::test]
async fn an_unreadable_key_error_still_falls_back_but_does_not_drop_the_key() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    let (status, _) = send(
        &router,
        "claude-opus-5-5",
        &pool("key-garbled"),
        false,
        false,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    send(
        &router,
        "claude-opus-5-5",
        &pool("key-garbled"),
        false,
        false,
    )
    .await;
    assert_eq!(
        credentials(&upstream),
        [
            "key:key-garbled",
            "Bearer sub-token",
            "key:key-garbled",
            "Bearer sub-token"
        ]
    );
}

#[tokio::test]
async fn a_billing_error_400_counts_as_out_of_credit() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    send(
        &router,
        "claude-opus-5-5",
        &pool("key-billing-400"),
        false,
        false,
    )
    .await;
    send(
        &router,
        "claude-opus-5-5",
        &pool("key-billing-400"),
        false,
        false,
    )
    .await;
    assert_eq!(
        credentials(&upstream),
        [
            "key:key-billing-400",
            "Bearer sub-token",
            "Bearer sub-token"
        ]
    );
}

fn budgeted(budget: f64) -> String {
    json!([
        {"alias": "console", "kind": "api_key", "api_key": "key-live", "primary": true, "budget_usd": budget},
        {"alias": "sub", "authorization": "Bearer sub-token", "primary": false},
    ])
    .to_string()
}

#[tokio::test]
async fn a_request_that_would_pass_the_remaining_budget_never_reaches_the_key() {
    let upstream = upstream().await;
    let router = gateway(&upstream.base);
    // max_tokens 1 on opus 5.5: a few millionths of a dollar fit in 1 cent, not in 0.
    let (status, body) = send(&router, "claude-opus-5-5", &budgeted(0.01), false, false).await;
    assert_eq!(
        (status, body["servedBy"].clone()),
        (StatusCode::OK, json!("key:key-live"))
    );
    let (_, body) = send(&router, "claude-opus-5-5", &budgeted(0.0), false, false).await;
    assert_eq!(body["servedBy"], "Bearer sub-token");
    let (_, body) = send(&router, "claude-unknown-9", &budgeted(100.0), false, false).await;
    assert_eq!(
        body["servedBy"], "Bearer sub-token",
        "a model without a known price never spends the key"
    );
    send(&router, "claude-opus-5-5", &budgeted(0.0), false, true).await;
    assert_eq!(
        credentials(&upstream),
        [
            "key:key-live",
            "Bearer sub-token",
            "Bearer sub-token",
            "key:key-live"
        ],
        "token counts are free, so the budget does not gate them"
    );
}
