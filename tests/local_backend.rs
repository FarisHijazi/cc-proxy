//! Exercises the real local provider against a toy OpenAI-compatible upstream.
use axum::{
    Json, Router,
    body::Body,
    http::{Request, StatusCode},
    routing::post,
};
use claude_codex::{config::AliasProvider, registry::Registry, server::app};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tower::ServiceExt;

#[tokio::test]
async fn local_backend_preserves_tools_usage_alias_and_existing_routes() {
    let captured = Arc::new(Mutex::new(Vec::<Value>::new()));
    let received = captured.clone();
    let upstream = Router::new().route("/v1/chat/completions", post(move |headers: axum::http::HeaderMap, Json(body): Json<Value>| {
        let received = received.clone();
        async move {
            // Delay both response headers and the first response-body bytes.
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
            assert_eq!(headers.get("authorization").unwrap(), "Bearer local-test-key");
            received.lock().unwrap().push(body);
            let chunks = [
                json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_local","type":"function","function":{"name":"read_file","arguments":"{\"path\":\"sample.txt\"}"}}]},"finish_reason":null}]}),
                json!({"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":42,"completion_tokens":12}}),
            ];
            let sse = chunks.iter().map(|chunk| format!("data: {chunk}\n\n")).collect::<String>() + "data: [DONE]\n\n";
            let delayed_body = futures_util::stream::once(async move {
                tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                Ok::<_, std::convert::Infallible>(bytes::Bytes::from(sse))
            });
            ([("content-type", "text/event-stream")], Body::from_stream(delayed_body))
        }
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, upstream).await.unwrap();
    });
    let config = tempfile::tempdir().unwrap();
    std::fs::write(
        config.path().join("config.json"),
        json!({"local":{
            "baseUrl":format!("http://{addr}/v1"), "apiKey":"local-test-key",
            "model":"local-model", "upstreamModel":"toy-upstream",
            "timeoutSecs":3600, "contextTokens":200000
        }})
        .to_string(),
    )
    .unwrap();
    // This integration-test executable contains one test; no concurrent env users.
    unsafe {
        std::env::set_var("CCP_CONFIG_DIR", config.path());
    }
    let registry = Registry::new(AliasProvider::Anthropic);
    assert_eq!(
        registry
            .provider_for_model("local-model", None)
            .unwrap()
            .name(),
        "local"
    );
    assert_eq!(
        registry
            .provider_for_model("gemini-3-pro", None)
            .unwrap()
            .name(),
        "gemini"
    );
    assert_eq!(
        registry.provider_for_model("opus", None).unwrap().name(),
        "anthropic"
    );
    assert!(
        !registry
            .provider_for_model("local-model", None)
            .unwrap()
            .supports_message_threads()
    );
    let router = app(Arc::new(registry));
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let models: Value = serde_json::from_slice(&bytes).unwrap();
    let local = models["data"]
        .as_array()
        .unwrap()
        .iter()
        .find(|model| model["id"] == "local-model")
        .unwrap();
    assert_eq!(local["context_window"], 200000);
    assert_eq!(local["experimental"], true);
    unsafe {
        std::env::set_var("CCP_LOCAL_CONTEXT_WINDOW", "32768");
    }
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let models: Value = serde_json::from_slice(&bytes).unwrap();
    let local = models["data"]
        .as_array()
        .unwrap()
        .iter()
        .find(|model| model["id"] == "local-model")
        .unwrap();
    assert_eq!(local["context_window"], 32768);
    unsafe {
        std::env::set_var("CCP_LOCAL_CONTEXT_WINDOW", "0");
    }
    assert!(
        !Registry::new(AliasProvider::Anthropic)
            .provider("local")
            .unwrap()
            .availability()
            .is_ready()
    );
    unsafe {
        std::env::set_var("CCP_LOCAL_CONTEXT_WINDOW", "32768");
    }
    for stream in [false, true] {
        let request = Request::builder().method("POST").uri("/v1/messages")
            .header("content-type", "application/json")
            .header("authorization", "Bearer caller-must-not-leak")
            .body(Body::from(json!({"model":"local-model", "max_tokens":64,"stream":stream,
                "tool_choice":{"type":"auto"},
                "messages":[{"role":"user","content":"Read sample.txt"}],
                "tools":[{"name":"read_file","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}]
            }).to_string())).unwrap();
        let response = if stream {
            tokio::time::timeout(
                std::time::Duration::from_millis(100),
                router.clone().oneshot(request),
            )
            .await
            .expect("local headers must not wait for upstream headers")
            .unwrap()
        } else {
            router.clone().oneshot(request).await.unwrap()
        };
        assert_eq!(response.status(), StatusCode::OK);
        let mut response_body = response.into_body();
        if stream {
            let first =
                tokio::time::timeout(std::time::Duration::from_millis(100), response_body.frame())
                    .await
                    .expect("local heartbeat must arrive before upstream headers/body")
                    .unwrap()
                    .unwrap()
                    .into_data()
                    .unwrap();
            assert_eq!(first, ": keep-alive\n\n");
        }
        let bytes = response_body.collect().await.unwrap().to_bytes();
        let text = String::from_utf8(bytes.to_vec()).unwrap();
        if stream {
            assert!(text.contains("tool_use"));
            assert!(text.contains("input_json_delta"));
            assert!(text.contains("message_stop"));
        } else {
            let result: Value = serde_json::from_str(&text).unwrap();
            assert_eq!(result["model"], "local-model");
            assert_eq!(result["content"][0]["type"], "tool_use");
            assert_eq!(result["content"][0]["input"]["path"], "sample.txt");
            assert_eq!(result["usage"]["input_tokens"], 42);
            assert_eq!(result["usage"]["output_tokens"], 12);
        }
    }
    let named_request = Request::builder().method("POST").uri("/v1/messages")
        .header("content-type", "application/json")
        .body(Body::from(json!({"model":"local-model", "max_tokens":64,
            "messages":[{"role":"user","content":"Read sample.txt"}],
            "tool_choice":{"type":"tool","name":"read_file"},
            "tools":[
                {"name":"other_tool","input_schema":{"type":"object"}},
                {"name":"read_file","input_schema":{"type":"object","properties":{"path":{"type":"string"}}}}
            ]
        }).to_string())).unwrap();
    assert_eq!(
        router
            .clone()
            .oneshot(named_request)
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    let unknown_named_request = Request::builder()
        .method("POST")
        .uri("/v1/messages")
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"model":"local-model", "max_tokens":64,
                "messages":[{"role":"user","content":"Read sample.txt"}],
                "tool_choice":{"type":"tool","name":"missing_tool"},
                "tools":[{"name":"read_file","input_schema":{"type":"object"}}]
            })
            .to_string(),
        ))
        .unwrap();
    assert_eq!(
        router
            .clone()
            .oneshot(unknown_named_request)
            .await
            .unwrap()
            .status(),
        StatusCode::BAD_REQUEST
    );
    let requests = captured.lock().unwrap();
    assert_eq!(requests.len(), 3);
    assert!(requests.iter().all(|body| body["model"] == "toy-upstream"));
    assert_eq!(requests[0]["tools"][0]["function"]["name"], "read_file");
    assert_eq!(requests[0]["stream_options"]["include_usage"], true);
    assert_eq!(requests[0]["tool_choice"], "auto");
    assert_eq!(requests[2]["tool_choice"], "required");
    assert_eq!(requests[2]["tools"].as_array().unwrap().len(), 1);
    assert_eq!(requests[2]["tools"][0]["function"]["name"], "read_file");
    drop(requests);
    server.abort();
    std::fs::write(config.path().join("config.json"), "{}").unwrap();
    assert!(
        !Registry::new(AliasProvider::Anthropic)
            .provider("local")
            .unwrap()
            .availability()
            .is_ready()
    );
    unsafe {
        std::env::remove_var("CCP_CONFIG_DIR");
        std::env::remove_var("CCP_LOCAL_CONTEXT_WINDOW");
    }
}
