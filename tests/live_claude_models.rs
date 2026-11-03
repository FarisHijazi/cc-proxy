//! `/v1/models` lists the Claude models Anthropic offers the caller's login, not a fixed list.
use axum::{
    Router,
    body::Body,
    http::{HeaderMap, Request, StatusCode},
    routing::get,
};
use claude_codex::{
    config::AliasProvider, provider::Provider, providers::anthropic::AnthropicProvider,
    registry::Registry, server::app,
};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;
use tower::ServiceExt;

async fn listed(router: &Router, authorization: Option<&str>) -> Vec<String> {
    let mut request = Request::builder().uri("/v1/models");
    if let Some(value) = authorization {
        request = request.header("authorization", value);
    }
    let response = router
        .clone()
        .oneshot(request.body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value =
        serde_json::from_slice(&axum::body::to_bytes(response.into_body(), 1 << 20).await.unwrap())
            .unwrap();
    body["data"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["id"].as_str().unwrap().to_string())
        .collect()
}

#[tokio::test]
async fn claude_models_come_from_anthropic_once_a_signed_in_caller_asks() {
    let seen: Arc<Mutex<Vec<HeaderMap>>> = Arc::default();
    let record = seen.clone();
    let origin = Router::new().route(
        "/v1/models",
        get(move |headers: HeaderMap| {
            let record = record.clone();
            async move {
                record.lock().unwrap().push(headers);
                axum::Json(json!({"data": [{"id": "claude-new-9"}, {"id": "claude-opus-4-8"}]}))
            }
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, origin).await.unwrap() });
    let provider: Arc<dyn Provider> = Arc::new(AnthropicProvider::with_base_url(&base));
    let router = app(Arc::new(Registry::from_providers(AliasProvider::Anthropic, [provider])));

    let before = listed(&router, None).await;
    assert!(before.contains(&"claude-opus-4-8".to_string()), "built-in list without a login");
    assert!(!before.contains(&"claude-new-9".to_string()));
    assert!(seen.lock().unwrap().is_empty(), "no login, no upstream call");

    let after = listed(&router, Some("Bearer token-a")).await;
    assert!(after.contains(&"claude-new-9".to_string()), "live list once a login asked: {after:?}");
    assert!(after.contains(&"opus".to_string()), "family aliases stay listed");
    assert!(!after.contains(&"claude-sonnet-4-6".to_string()), "built-in ids Anthropic did not list are gone");
    {
        let calls = seen.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0]["authorization"], "Bearer token-a");
        assert_eq!(calls[0]["anthropic-beta"], "oauth-2025-04-20");
    }

    assert!(listed(&router, None).await.contains(&"claude-new-9".to_string()), "callers without a login see the cached list");
    listed(&router, Some("Bearer token-a")).await;
    assert_eq!(seen.lock().unwrap().len(), 1, "a fresh list is not fetched again");
    task.abort();
}
