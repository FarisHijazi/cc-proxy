use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use claude_codex::{
    providers::codex::translate::model_allowlist::resolve_model_request, registry::Registry,
    server::app,
};
use serde_json::json;
use std::{
    io::{Read, Write},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tower::ServiceExt;

#[tokio::test(flavor = "current_thread")]
async fn slow_cold_discovery_does_not_block_timers_or_health_and_is_shared() {
    let home = tempfile::tempdir().unwrap();
    std::fs::write(
        home.path().join("auth.json"),
        json!({"tokens":{"access_token":"toy-access","account_id":"toy-account"}}).to_string(),
    )
    .unwrap();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}/responses", listener.local_addr().unwrap());
    unsafe {
        std::env::set_var("CODEX_HOME", home.path());
        std::env::set_var("CCP_CODEX_BASE_URL", base);
        std::env::set_var("NO_PROXY", "127.0.0.1");
    }
    let requests = Arc::new(AtomicUsize::new(0));
    let captured = requests.clone();
    let upstream = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        captured.fetch_add(1, Ordering::SeqCst);
        let mut head = Vec::new();
        while !head.ends_with(b"\r\n\r\n") {
            let mut byte = [0];
            stream.read_exact(&mut byte).unwrap();
            head.push(byte[0]);
        }
        assert!(String::from_utf8(head).unwrap().starts_with("GET /models?"));
        std::thread::sleep(Duration::from_millis(500));
        let body =
            r#"{"models":[{"slug":"gpt-6.1-astra","visibility":"list","supported_in_api":true}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
    });
    let router = app(Arc::new(Registry::with_default_alias()));
    let mut calls = Vec::new();
    for _ in 0..8 {
        let router = router.clone();
        calls.push(tokio::spawn(async move {
            router
                .oneshot(
                    Request::post("/v1/messages/count_tokens")
                        .header("content-type", "application/json")
                        .body(Body::from(
                            json!({"model":"astra","messages":[{"role":"user","content":"hi"}]})
                                .to_string(),
                        ))
                        .unwrap(),
                )
                .await
                .unwrap()
        }));
    }
    let start = Instant::now();
    tokio::time::sleep(Duration::from_millis(25)).await;
    let health = router
        .oneshot(Request::get("/healthz").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(health.status(), StatusCode::OK);
    assert!(
        start.elapsed() < Duration::from_millis(200),
        "timer/health stalled for {:?}",
        start.elapsed()
    );
    for call in calls {
        assert_eq!(call.await.unwrap().status(), StatusCode::OK);
    }
    assert_eq!(resolve_model_request("astra").model, "gpt-6.1-astra");
    assert_eq!(requests.load(Ordering::SeqCst), 1);
    upstream.join().unwrap();
    unsafe {
        std::env::remove_var("CODEX_HOME");
        std::env::remove_var("CCP_CODEX_BASE_URL");
        std::env::remove_var("NO_PROXY");
    }
}
