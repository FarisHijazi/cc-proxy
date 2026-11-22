//! Over real sockets: a request replaying another backend's reasoning reaches Anthropic
//! with that reasoning dropped, never rewritten into visible assistant text.
use axum::{Router, body::Body, http::Request};
use claude_codex::{
    config::AliasProvider, provider::Provider, providers::anthropic::AnthropicProvider,
    registry::Registry, server::app,
};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;

async fn serve(router: Router) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    base
}

#[tokio::test]
async fn outbound_anthropic_body_drops_only_foreign_thinking() {
    let seen: Arc<Mutex<Vec<(String, Value)>>> = Arc::default();
    let log = seen.clone();
    let upstream = serve(Router::new().fallback(move |req: Request<Body>| {
        let log = log.clone();
        async move {
            let path = req.uri().path().to_string();
            let body = axum::body::to_bytes(req.into_body(), 1_000_000)
                .await
                .unwrap();
            log.lock()
                .unwrap()
                .push((path, serde_json::from_slice(&body).unwrap()));
            axum::Json(json!({"input_tokens": 1}))
        }
    }))
    .await;
    let providers: Vec<Arc<dyn Provider>> =
        vec![Arc::new(AnthropicProvider::with_base_url(upstream))];
    let proxy = serve(app(Arc::new(Registry::from_providers(
        AliasProvider::Anthropic,
        providers,
    ))))
    .await;

    let signed = json!({"type": "thinking", "thinking": "opus r", "signature": "EqQBsig=="});
    let redacted = json!({"type": "redacted_thinking", "data": "EmwKAhgBEgy="});
    let tool_use =
        json!({"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}});
    let tool_result = json!({"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"});
    let messages = |assistant: Vec<Value>, codex_only: Vec<Value>| {
        json!([
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": assistant},
            {"role": "user", "content": [tool_result.clone()]},
            {"role": "assistant", "content": codex_only},
            {"role": "user", "content": "thanks"}
        ])
    };
    let sent = messages(
        vec![
            signed.clone(),
            json!({"type": "thinking", "thinking": "codex r", "signature": "ccp:codex:v1:abc"}),
            redacted.clone(),
            json!({"type": "text", "text": "Listing."}),
            json!({"type": "thinking", "thinking": "kimi r", "signature": "Y2NwOmtpbWk6djE6bXNnXzE6MA"}),
            tool_use.clone(),
        ],
        vec![json!({"type": "thinking", "thinking": "unsigned r", "signature": ""})],
    );
    let expected = json!([
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": [
            signed, redacted, {"type": "text", "text": "Listing."}, tool_use
        ]},
        {"role": "user", "content": [tool_result.clone()]},
        {"role": "user", "content": "thanks"}
    ]);

    let client = reqwest::Client::new();
    for path in ["/v1/messages", "/v1/messages/count_tokens"] {
        let status = client
            .post(format!("{proxy}{path}"))
            .header("authorization", "Bearer session-token")
            .header("content-type", "application/json")
            .body(
                json!({"model": "claude-opus-4-7", "max_tokens": 1, "messages": sent}).to_string(),
            )
            .send()
            .await
            .unwrap()
            .status();
        assert!(status.is_success(), "{path}: {status}");
    }

    let seen = seen.lock().unwrap().clone();
    assert_eq!(seen.len(), 2);
    for (path, body) in seen {
        assert_eq!(body["messages"], expected, "{path}");
        assert!(!body.to_string().contains("previous_reasoning"), "{path}");
    }
}

#[path = "fixtures/hosted_history.rs"]
mod hosted_history;

#[tokio::test]
async fn emitted_search_histories_reach_anthropic_without_foreign_reasoning_or_unknown_tools() {
    let seen: Arc<Mutex<Vec<Value>>> = Arc::default();
    let received = seen.clone();
    let upstream = serve(Router::new().fallback(move |req: Request<Body>| {
        let received = received.clone();
        async move {
            let body = axum::body::to_bytes(req.into_body(), 1_000_000)
                .await
                .unwrap();
            received
                .lock()
                .unwrap()
                .push(serde_json::from_slice(&body).unwrap());
            axum::Json(json!({"input_tokens":1}))
        }
    }))
    .await;
    let proxy = serve(app(Arc::new(Registry::from_providers(
        AliasProvider::Anthropic,
        vec![Arc::new(AnthropicProvider::with_base_url(upstream)) as Arc<dyn Provider>],
    ))))
    .await;
    for (source, response) in hosted_history::emitted_histories() {
        let messages = hosted_history::messages(&response["content"]);
        let status = reqwest::Client::new()
            .post(format!("{proxy}/v1/messages/count_tokens"))
            .header("authorization", "Bearer toy-token")
            .json(&json!({"model":"claude-opus-4-7","max_tokens":1,"messages":messages}))
            .send()
            .await
            .unwrap()
            .status();
        assert!(status.is_success(), "{source}: {status}");
        let body = seen.lock().unwrap().last().unwrap().clone();
        let text = body.to_string();
        assert!(!text.contains(hosted_history::PRIVATE), "{source}: {body}");
        for public in [
            hosted_history::PUBLIC,
            hosted_history::URL,
            hosted_history::CITATION,
            "RESULT_FIRST",
            "RESULT_SECOND",
        ] {
            assert!(text.contains(public), "{source} lost {public}: {body}");
        }
        assert_eq!(text.matches("call_1").count(), 2);
        assert_eq!(text.matches("call_2").count(), 2);
        assert!(text.find("call_1").unwrap() < text.find("call_2").unwrap());
        assert!(text.find("RESULT_SECOND").unwrap() < text.find("RESULT_FIRST").unwrap());
        let blocks = body["messages"][1]["content"].as_array().unwrap();
        if source == "codex" {
            for original in response["content"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|block| {
                    matches!(
                        block["type"].as_str(),
                        Some("server_tool_use" | "web_search_tool_result")
                    )
                })
            {
                assert!(
                    blocks.contains(original),
                    "native web-search shape changed: {body}"
                );
            }
        } else {
            assert!(
                blocks
                    .iter()
                    .all(|block| block["type"] != "x_search_tool_result"
                        && block["type"] != "server_tool_use")
            );
            assert_eq!(
                blocks
                    .iter()
                    .filter(|block| block["text"]
                        .as_str()
                        .is_some_and(|text| text.contains("search_1")))
                    .count(),
                2
            );
        }
    }
}
