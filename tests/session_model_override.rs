//! Dashboard-forced session models: `account_inject` sets `x-claude-codex-session-model`,
//! the router applies it before provider selection and never relays it.
use async_trait::async_trait;
use axum::{
    Router,
    body::{Body, Bytes},
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

const OVERRIDE: &str = "x-claude-codex-session-model";
const POOL: &str = "x-claude-codex-account-pool";

#[derive(Clone, Debug)]
struct Seen {
    path: String,
    auth: String,
    header_names: Vec<String>,
    body: Bytes,
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

async fn anthropic_upstream() -> Upstream {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let router = Router::new().fallback(move |req: Request<Body>| {
        let log = log.clone();
        async move {
            let path = req.uri().path().to_string();
            let header_names = req.headers().keys().map(|k| k.to_string()).collect();
            let auth = req.headers()["authorization"].to_str().unwrap().to_string();
            let body = axum::body::to_bytes(req.into_body(), 1_000_000)
                .await
                .unwrap();
            // Only the second account's plan offers the locked model.
            let locked = String::from_utf8_lossy(&body).contains("claude-mythos-locked")
                && auth != "Bearer account-b";
            log.lock().unwrap().push(Seen {
                path,
                auth,
                header_names,
                body,
            });
            if locked {
                return (
                    StatusCode::NOT_FOUND,
                    axum::Json(json!({"type":"error","error":{"type":"not_found_error","message":"model: claude-mythos-locked"}})),
                )
                    .into_response();
            }
            axum::Json(json!({"input_tokens": 1})).into_response()
        }
    });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    Upstream { base, seen, task }
}

#[derive(Default)]
struct Codex {
    messages: Mutex<Vec<String>>,
    counts: Mutex<Vec<String>>,
}
#[async_trait]
impl Provider for Codex {
    fn name(&self) -> &'static str {
        "codex"
    }
    fn supported_models(&self) -> Vec<String> {
        vec!["gpt-5.6-sol".to_string()]
    }
    fn cli(&self) -> &'static dyn CliHandlers {
        unreachable!()
    }
    async fn handle_messages(&self, body: MessagesRequest, _ctx: RequestContext) -> Response {
        self.messages
            .lock()
            .unwrap()
            .push(body.model.unwrap_or_default());
        axum::Json(json!({"ok": true})).into_response()
    }
    async fn handle_count_tokens(&self, body: MessagesRequest, _ctx: RequestContext) -> Response {
        self.counts
            .lock()
            .unwrap()
            .push(body.model.unwrap_or_default());
        axum::Json(json!({"input_tokens": 1})).into_response()
    }
}

fn gateway(base: &str, codex: Arc<Codex>) -> Router {
    let providers: Vec<Arc<dyn Provider>> =
        vec![Arc::new(AnthropicProvider::with_base_url(base)), codex];
    app(Arc::new(Registry::from_providers(
        AliasProvider::Anthropic,
        providers,
    )))
}

/// Deliberately unusual spacing and key order, so any re-serialization shows.
fn raw_body(model: &str, thread: bool) -> String {
    let thread = if thread {
        r#", "thread":{"type":"continue","previous_message_id":"msg_toy"}"#
    } else {
        ""
    };
    format!(
        r#"{{"max_tokens": 1,  "model": "{model}", "messages": [{{"role":"user","content":"hi"}}]{thread}}}"#
    )
}

async fn send(
    router: Router,
    path: &str,
    body: String,
    forced: Option<&str>,
    pool: bool,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .header("authorization", "Bearer own-session-token")
        .header(
            "x-claude-code-session-id",
            "11111111-1111-4111-8111-111111111111",
        );
    if let Some(forced) = forced {
        request = request.header(OVERRIDE, forced);
    }
    if pool {
        request = request.header(
            POOL,
            json!([
                {"alias": "a", "authorization": "Bearer account-a", "primary": true},
                {"alias": "b", "authorization": "Bearer account-b", "primary": false},
            ])
            .to_string(),
        );
    }
    let response = router
        .oneshot(request.body(Body::from(body)).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 100_000)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

fn model_of(seen: &Seen) -> String {
    serde_json::from_slice::<Value>(&seen.body).unwrap()["model"]
        .as_str()
        .unwrap()
        .to_string()
}

#[tokio::test]
async fn without_an_override_the_passthrough_bytes_are_untouched() {
    let upstream = anthropic_upstream().await;
    let body = raw_body("claude-opus-4-7", false);
    for path in ["/v1/messages", "/v1/messages/count_tokens"] {
        let (status, _) = send(
            gateway(&upstream.base, Arc::default()),
            path,
            body.clone(),
            None,
            false,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
    }
    let seen = upstream.seen.lock().unwrap().clone();
    assert_eq!(seen.len(), 2);
    assert!(
        seen.iter().all(|s| s.body == body.as_bytes()),
        "ordinary bytes must be relayed as-is"
    );
}

#[tokio::test]
async fn an_override_equal_to_the_requested_model_keeps_bytes() {
    let upstream = anthropic_upstream().await;
    let body = raw_body("claude-opus-4-7", false);
    send(
        gateway(&upstream.base, Arc::default()),
        "/v1/messages",
        body.clone(),
        Some("claude-opus-4-7[1m]"),
        false,
    )
    .await;
    let seen = upstream.seen.lock().unwrap().clone();
    assert_eq!(seen[0].body, body.as_bytes());
    assert!(!seen[0].header_names.iter().any(|h| h == OVERRIDE));
}

#[tokio::test]
async fn messages_and_counts_follow_a_claude_override_and_the_header_is_never_relayed() {
    let upstream = anthropic_upstream().await;
    let body = raw_body("claude-sonnet-4-5", false);
    for path in [
        "/v1/messages?beta=true",
        "/v1/messages/count_tokens?beta=true",
    ] {
        let (status, _) = send(
            gateway(&upstream.base, Arc::default()),
            path,
            body.clone(),
            Some("claude-mythos-1[1m]"),
            false,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
    }
    let (status, _) = send(
        gateway(&upstream.base, Arc::default()),
        "/v1/messages",
        body.clone(),
        Some("claude-mythos-1"),
        true,
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "the forced model also walks the account pool"
    );
    let seen = upstream.seen.lock().unwrap().clone();
    assert_eq!(
        seen.iter().map(|s| s.path.as_str()).collect::<Vec<_>>(),
        ["/v1/messages", "/v1/messages/count_tokens", "/v1/messages"]
    );
    for request in &seen {
        assert_eq!(
            model_of(request),
            "claude-mythos-1",
            "raw passthrough body carries the forced model"
        );
        assert!(
            !request
                .header_names
                .iter()
                .any(|h| h == OVERRIDE || h == POOL)
        );
    }
}

#[tokio::test]
async fn an_override_can_move_a_session_to_another_provider() {
    let upstream = anthropic_upstream().await;
    let codex = Arc::new(Codex::default());
    let body = raw_body("claude-opus-4-7", false);
    for path in ["/v1/messages", "/v1/messages/count_tokens"] {
        let (status, _) = send(
            gateway(&upstream.base, codex.clone()),
            path,
            body.clone(),
            Some("gpt-5.6-sol"),
            false,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
    }
    assert!(
        upstream.seen.lock().unwrap().is_empty(),
        "Anthropic must not see a session forced to Codex"
    );
    assert_eq!(*codex.messages.lock().unwrap(), ["gpt-5.6-sol"]);
    assert_eq!(*codex.counts.lock().unwrap(), ["gpt-5.6-sol"]);
}

#[tokio::test]
async fn a_thread_delta_under_a_changed_forced_model_is_refused() {
    let upstream = anthropic_upstream().await;
    let (status, body) = send(
        gateway(&upstream.base, Arc::default()),
        "/v1/messages",
        raw_body("claude-opus-4-7", true),
        Some("claude-mythos-1"),
        false,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        body["error"]["details"]["error_code"],
        "thread_unsupported_request"
    );
    assert!(
        upstream.seen.lock().unwrap().is_empty(),
        "a contextless delta must never be relayed"
    );

    let (status, _) = send(
        gateway(&upstream.base, Arc::default()),
        "/v1/messages",
        raw_body("claude-mythos-1", true),
        Some("claude-mythos-1"),
        false,
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "a delta already on the forced model continues"
    );
}

/// A forced model the session's own login lacks: counts and messages both walk the
/// account pool to the login that offers it, with the forced bytes and no internal headers.
#[tokio::test]
async fn a_forced_model_missing_on_the_sessions_login_is_counted_and_served_by_another() {
    let upstream = anthropic_upstream().await;
    let body = raw_body("claude-opus-4-7", false);
    for path in [
        "/v1/messages/count_tokens?beta=true",
        "/v1/messages?beta=true",
    ] {
        let (status, reply) = send(
            gateway(&upstream.base, Arc::default()),
            path,
            body.clone(),
            Some("claude-mythos-locked"),
            true,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{path}");
        assert_eq!(reply["input_tokens"], 1);
    }
    let seen = upstream.seen.lock().unwrap().clone();
    assert_eq!(
        seen.iter()
            .map(|s| (s.path.as_str(), s.auth.as_str()))
            .collect::<Vec<_>>(),
        [
            ("/v1/messages/count_tokens", "Bearer account-a"),
            ("/v1/messages/count_tokens", "Bearer account-b"),
            ("/v1/messages", "Bearer account-a"),
            ("/v1/messages", "Bearer account-b"),
        ]
    );
    let forced = body.replace("claude-opus-4-7", "claude-mythos-locked");
    for request in &seen {
        assert_eq!(model_of(request), "claude-mythos-locked");
        assert_eq!(
            serde_json::from_slice::<Value>(&request.body).unwrap(),
            serde_json::from_str::<Value>(&forced).unwrap()
        );
        assert!(
            !request
                .header_names
                .iter()
                .any(|h| h == OVERRIDE || h == POOL || h == "x-claude-codex-fallback")
        );
    }
}

/// Without an override, a pooled count keeps its exact bytes and the pool is never relayed.
#[tokio::test]
async fn a_pooled_count_without_an_override_keeps_bytes_and_strips_the_pool() {
    let upstream = anthropic_upstream().await;
    let body = raw_body("claude-opus-4-7", false);
    let (status, _) = send(
        gateway(&upstream.base, Arc::default()),
        "/v1/messages/count_tokens",
        body.clone(),
        None,
        true,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let seen = upstream.seen.lock().unwrap().clone();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].auth, "Bearer account-a");
    assert_eq!(seen[0].body, body.as_bytes());
    assert!(
        !seen[0]
            .header_names
            .iter()
            .any(|h| h == OVERRIDE || h == POOL)
    );
}
