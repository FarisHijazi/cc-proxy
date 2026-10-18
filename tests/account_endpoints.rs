//! Account traffic must be opaque, streamed, fixed-origin, and independent of models.
use axum::{
    Router,
    body::{Body, Bytes},
    http::{Request, StatusCode},
    response::Response,
};
use claude_codex::{
    config::AliasProvider,
    inbound_auth::{InboundAuth, require_auth},
    provider::Provider,
    providers::anthropic::AnthropicProvider,
    registry::Registry,
    server::app,
};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::{
    convert::Infallible,
    io::Write,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    net::TcpListener,
    sync::{mpsc, oneshot},
    task::JoinHandle,
};
use tokio_tungstenite::{
    accept_hdr_async, connect_async,
    tungstenite::{Message, client::IntoClientRequest},
};
use tower::ServiceExt;

struct Server {
    base: String,
    task: JoinHandle<()>,
}
impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}
async fn serve(router: Router) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    Server { base, task }
}
fn gateway(base: &str) -> Router {
    let provider: Arc<dyn Provider> = Arc::new(AnthropicProvider::with_base_url(base));
    app(Arc::new(Registry::from_providers(
        AliasProvider::Anthropic,
        [provider],
    )))
}
async fn echo(req: Request<Body>) -> Response {
    let method = req.method().to_string();
    let uri = req.uri().to_string();
    let headers = req.headers().clone();
    let bytes = axum::body::to_bytes(req.into_body(), 10_000_000)
        .await
        .unwrap();
    let header = |name| headers.get(name).and_then(|v| v.to_str().ok());
    axum::Json(json!({"method":method,"uri":uri,"body":bytes.to_vec(),
        "authorization":header("authorization"),"cookie":header("cookie"),
        "proxyKey":header("x-claude-codex-key"),"privateHop":header("x-private-hop"),
        "beta":header("anthropic-beta"),"contentType":header("content-type"),
        "encoding":header("content-encoding")}))
    .into_response()
}
use axum::response::IntoResponse;

#[tokio::test]
async fn anthropic_message_threads_pass_through_with_their_history_owned_upstream() {
    let origin = serve(Router::new().fallback(echo)).await;
    let payload = json!({"model":"claude-sonnet-4-6", "max_tokens":1,
        "messages":[{"role":"user", "content":"delta"}],
        "thread":{"type":"continue", "previous_message_id":"msg_toy"}})
    .to_string();
    let response = gateway(&origin.base)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/messages?beta=true")
                .header("content-type", "application/json")
                .body(Body::from(payload.clone()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 100_000)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["body"], json!(payload.as_bytes()));
}

#[tokio::test]
async fn all_account_methods_paths_queries_and_binary_bodies_are_relayed() {
    let origin = serve(Router::new().fallback(echo)).await;
    for (method, path) in [
        ("GET", "/api/oauth/usage?scope=a%2Fb&scope=c"),
        ("POST", "/v1/code/sessions/test/events?beta=true"),
        ("PATCH", "/api/organizations/test/artifacts/item"),
        ("DELETE", "/v1/files/file-test"),
        ("PUT", "/future/account/path"),
        ("OPTIONS", "/api/oauth/usage"),
        ("GET", "/v1/models/model-id"),
        ("GET", "/v1/messages"),
    ] {
        let body = b"\0\xff\r\nopaque-body";
        let result = gateway(&origin.base)
            .oneshot(
                Request::builder()
                    .method(method)
                    .uri(path)
                    .header("authorization", "Bearer toy-account")
                    .header("cookie", "session=toy")
                    .header("anthropic-beta", "toy-beta")
                    .header("x-claude-codex-key", "private-router-key")
                    .header("connection", "x-private-hop")
                    .header("x-private-hop", "do-not-forward")
                    .body(Body::from(body.as_slice()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(result.status(), StatusCode::OK, "{method} {path}");
        let data: Value = serde_json::from_slice(
            &axum::body::to_bytes(result.into_body(), 100_000)
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(data["method"], method);
        assert_eq!(data["uri"], path);
        assert_eq!(data["body"], json!(body));
        assert_eq!(data["authorization"], "Bearer toy-account");
        assert_eq!(data["cookie"], "session=toy");
        assert_eq!(data["beta"], "toy-beta");
        assert!(data["proxyKey"].is_null());
        assert!(data["privateHop"].is_null());
    }
}

#[tokio::test]
async fn uploads_keep_multipart_bytes_and_have_no_json_body_limit() {
    let origin = serve(Router::new().fallback(echo)).await;
    let mut payload =
        b"--toy\r\nContent-Disposition: form-data; name=\"file\"; filename=\"toy.bin\"\r\n\r\n"
            .to_vec();
    payload.extend(vec![0xff; 2_100_000]);
    payload.extend(b"\r\n--toy--\r\n");
    let result = gateway(&origin.base)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/files")
                .header("content-type", "multipart/form-data; boundary=toy")
                .body(Body::from(payload.clone()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(result.status(), StatusCode::OK);
    let data: Value = serde_json::from_slice(
        &axum::body::to_bytes(result.into_body(), 20_000_000)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(data["body"], json!(payload));
    assert_eq!(data["contentType"], "multipart/form-data; boundary=toy");
}

#[tokio::test]
async fn status_redirects_duplicate_headers_compression_and_head_survive() {
    let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    encoder.write_all(b"opaque artifact payload").unwrap();
    let compressed = encoder.finish().unwrap();
    let expected = compressed.clone();
    let origin = serve(Router::new().fallback(move |req: Request<Body>| {
        let bytes = compressed.clone();
        async move {
            let status = if req.uri().path() == "/redirect" {
                StatusCode::TEMPORARY_REDIRECT
            } else {
                StatusCode::TOO_MANY_REQUESTS
            };
            Response::builder()
                .status(status)
                .header("content-encoding", "gzip")
                .header("content-length", bytes.len())
                .header("location", "https://example.com/untouched")
                .header("set-cookie", "one=1; HttpOnly")
                .header("set-cookie", "two=2; Secure")
                .header("retry-after", "120")
                .header("connection", "x-origin-hop")
                .header("x-origin-hop", "private")
                .body(Body::from(bytes))
                .unwrap()
        }
    }))
    .await;
    for (method, path) in [
        ("GET", "/redirect"),
        ("GET", "/artifact"),
        ("HEAD", "/artifact"),
    ] {
        let result = gateway(&origin.base)
            .oneshot(
                Request::builder()
                    .method(method)
                    .uri(path)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            result.status(),
            if path == "/redirect" {
                StatusCode::TEMPORARY_REDIRECT
            } else {
                StatusCode::TOO_MANY_REQUESTS
            }
        );
        assert_eq!(result.headers()["content-encoding"], "gzip");
        assert_eq!(
            result.headers()["content-length"],
            expected.len().to_string()
        );
        assert_eq!(result.headers().get_all("set-cookie").iter().count(), 2);
        assert_eq!(result.headers()["retry-after"], "120");
        assert_eq!(
            result.headers()["location"],
            "https://example.com/untouched"
        );
        assert!(!result.headers().contains_key("x-origin-hop"));
        let bytes = axum::body::to_bytes(result.into_body(), 1000)
            .await
            .unwrap();
        assert_eq!(
            bytes.as_ref(),
            if method == "HEAD" {
                b"".as_slice()
            } else {
                &expected
            }
        );
    }
}

#[tokio::test]
async fn inbound_auth_also_protects_the_account_fallback() {
    let origin = serve(Router::new().fallback(echo)).await;
    let router = gateway(&origin.base).layer(axum::middleware::from_fn_with_state(
        InboundAuth::new(Some("toy-router".into())),
        require_auth,
    ));
    let result = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/oauth/usage")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(result.status(), StatusCode::UNAUTHORIZED);
    let result = router
        .oneshot(
            Request::builder()
                .uri("/api/oauth/usage")
                .header("authorization", "Bearer toy-account")
                .header("x-claude-codex-key", "toy-router")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(result.status(), StatusCode::OK);
}

#[tokio::test]
async fn proxy_bearer_is_consumed_locally_and_health_cannot_bypass_auth() {
    let origin = serve(Router::new().fallback(echo)).await;
    let router = gateway(&origin.base).layer(axum::middleware::from_fn_with_state(
        InboundAuth::new(Some("toy-router".into())),
        require_auth,
    ));
    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/oauth/usage")
                .header("authorization", "Bearer toy-router")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let data: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 100_000)
            .await
            .unwrap(),
    )
    .unwrap();
    assert!(data["authorization"].is_null());
    let response = router
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn model_dispatch_local_features_and_fixed_origin_stay_authoritative() {
    let origin = serve(Router::new().fallback(echo)).await;
    let router = gateway(&origin.base);
    let models = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let models: Value = serde_json::from_slice(
        &axum::body::to_bytes(models.into_body(), 100_000)
            .await
            .unwrap(),
    )
    .unwrap();
    assert!(models["data"].is_array());
    for path in [
        "/v1/responses",
        "/v1/images/edits",
        "/v1/audio/transcriptions",
    ] {
        let result = router
            .clone()
            .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(result.status(), StatusCode::NOT_FOUND);
    }
    let result = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/messages")
                .body(Body::from(r#"{"model":"unknown","messages":[]}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(result.status(), StatusCode::BAD_REQUEST);
    let result = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("http://untrusted.invalid/api/oauth/usage")
                .header("host", "untrusted.invalid")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(result.status(), StatusCode::OK);
    let result = router
        .oneshot(
            Request::builder()
                .method("CONNECT")
                .uri("untrusted.invalid:443")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(result.status(), StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn response_stream_arrives_before_completion() {
    let (release, wait) = oneshot::channel::<()>();
    let gate = Arc::new(Mutex::new(Some(wait)));
    let origin = serve(Router::new().fallback(move || {
        let gate = gate.clone();
        async move {
            let wait = gate.lock().unwrap().take().unwrap();
            let chunks = futures_util::stream::once(async {
                Ok::<_, Infallible>(Bytes::from_static(b"data: first\n\n"))
            })
            .chain(futures_util::stream::once(async move {
                wait.await.unwrap();
                Ok(Bytes::from_static(b"data: last\n\n"))
            }));
            Response::builder()
                .header("content-type", "text/event-stream")
                .body(Body::from_stream(chunks))
                .unwrap()
        }
    }))
    .await;
    let proxy = serve(gateway(&origin.base)).await;
    let result = reqwest::get(format!("{}/v1/code/events", proxy.base))
        .await
        .unwrap();
    let mut chunks = result.bytes_stream();
    let first = tokio::time::timeout(Duration::from_secs(3), chunks.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(first, "data: first\n\n");
    release.send(()).unwrap();
    assert_eq!(chunks.next().await.unwrap().unwrap(), "data: last\n\n");
}

#[tokio::test]
async fn upload_stream_reaches_upstream_before_client_finishes() {
    let (observed, wait) = oneshot::channel();
    let observed = Arc::new(Mutex::new(Some(observed)));
    let origin = serve(Router::new().fallback(move |req: Request<Body>| {
        let observed = observed.clone();
        async move {
            let mut chunks = req.into_body().into_data_stream();
            assert_eq!(chunks.next().await.unwrap().unwrap(), "first");
            observed.lock().unwrap().take().unwrap().send(()).unwrap();
            assert_eq!(chunks.next().await.unwrap().unwrap(), "last");
            Response::new(Body::from("uploaded"))
        }
    }))
    .await;
    let proxy = serve(gateway(&origin.base)).await;
    let (sender, receiver) = mpsc::channel::<Result<Bytes, Infallible>>(2);
    let body = futures_util::stream::unfold(receiver, |mut receiver| async {
        receiver.recv().await.map(|value| (value, receiver))
    });
    sender.send(Ok(Bytes::from_static(b"first"))).await.unwrap();
    let request = tokio::spawn(async move {
        reqwest::Client::new()
            .post(format!("{}/v1/files", proxy.base))
            .body(reqwest::Body::wrap_stream(body))
            .send()
            .await
            .unwrap()
            .text()
            .await
            .unwrap()
    });
    tokio::time::timeout(Duration::from_secs(3), wait)
        .await
        .unwrap()
        .unwrap();
    sender.send(Ok(Bytes::from_static(b"last"))).await.unwrap();
    drop(sender);
    assert_eq!(request.await.unwrap(), "uploaded");
}

#[tokio::test]
async fn websocket_handshake_frames_subprotocol_and_auth_are_tunnelled() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let origin_base = format!("http://{}", listener.local_addr().unwrap());
    let origin_task = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        #[allow(clippy::result_large_err)]
        let mut socket = accept_hdr_async(
            stream,
            |request: &http::Request<()>, mut response: http::Response<()>| {
                assert_eq!(request.uri(), "/v1/code/socket?session=toy");
                assert_eq!(request.headers()["authorization"], "Bearer toy-account");
                assert!(!request.headers().contains_key("x-claude-codex-key"));
                response
                    .headers_mut()
                    .insert("sec-websocket-protocol", "toy-protocol".parse().unwrap());
                Ok(response)
            },
        )
        .await
        .unwrap();
        while let Some(message) = socket.next().await {
            let message = message.unwrap();
            let close = message.is_close();
            socket.send(message).await.unwrap();
            if close {
                break;
            }
        }
    });
    let proxy = serve(gateway(&origin_base)).await;
    let mut request = format!(
        "{}/v1/code/socket?session=toy",
        proxy.base.replace("http://", "ws://")
    )
    .into_client_request()
    .unwrap();
    request
        .headers_mut()
        .insert("authorization", "Bearer toy-account".parse().unwrap());
    request
        .headers_mut()
        .insert("x-claude-codex-key", "private-router".parse().unwrap());
    request
        .headers_mut()
        .insert("sec-websocket-protocol", "toy-protocol".parse().unwrap());
    let (mut socket, response) =
        tokio::time::timeout(Duration::from_secs(3), connect_async(request))
            .await
            .unwrap()
            .unwrap();
    assert_eq!(response.status(), StatusCode::SWITCHING_PROTOCOLS);
    assert_eq!(response.headers()["sec-websocket-protocol"], "toy-protocol");
    for message in [
        Message::Text("hello".into()),
        Message::Binary(vec![0, 255]),
        Message::Ping(vec![1, 2]),
    ] {
        socket.send(message.clone()).await.unwrap();
        let echoed = tokio::time::timeout(Duration::from_secs(3), socket.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert!(echoed == message || (message.is_ping() && echoed.is_pong()));
    }
    socket.close(None).await.unwrap();
    origin_task.abort();
}

#[tokio::test]
async fn trailer_frames_survive_in_both_directions() {
    use http_body_util::{BodyExt, StreamBody};
    use hyper::body::Frame;
    let origin = serve(Router::new().fallback(|request: Request<Body>| async move {
        let body = request.into_body().collect().await.unwrap();
        assert_eq!(body.trailers().unwrap()["x-checksum"], "toy-checksum");
        assert_eq!(body.to_bytes(), "upload");
        let mut trailers = http::HeaderMap::new();
        trailers.insert("grpc-status", "0".parse().unwrap());
        let frames = futures_util::stream::iter(vec![
            Ok::<_, Infallible>(Frame::data(Bytes::from_static(b"download"))),
            Ok(Frame::trailers(trailers)),
        ]);
        Response::builder()
            .header("trailer", "grpc-status")
            .body(Body::new(StreamBody::new(frames)))
            .unwrap()
    }))
    .await;
    let mut trailers = http::HeaderMap::new();
    trailers.insert("x-checksum", "toy-checksum".parse().unwrap());
    let frames = futures_util::stream::iter(vec![
        Ok::<_, Infallible>(Frame::data(Bytes::from_static(b"upload"))),
        Ok(Frame::trailers(trailers)),
    ]);
    let response = gateway(&origin.base)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/stream")
                .header("trailer", "x-checksum")
                .header("te", "trailers")
                .body(Body::new(StreamBody::new(frames)))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap();
    assert_eq!(body.trailers().unwrap()["grpc-status"], "0");
    assert_eq!(body.to_bytes(), "download");
}
