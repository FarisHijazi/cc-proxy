//! The signed-in `cursor-agent` path end to end, against a stand-in CLI found
//! only at the installer's `~/.local/bin` (a systemd unit's bare `PATH`): the
//! backend is available, every model it lists is advertised as
//! `cursor-cli:<id>`, and picking one runs the CLI read-only with exactly that
//! model. Its own test binary, because HOME and PATH are process-wide.
#![cfg(unix)]

use axum::body::Body;
use axum::http::{Method, Request, StatusCode};
use claude_codex::{config::AliasProvider, registry::Registry, server::app};
use serde_json::{Value, json};
use std::os::unix::fs::PermissionsExt;
use std::sync::Arc;
use tower::util::ServiceExt;

const FAKE_CURSOR_AGENT: &str = r#"#!/bin/sh
if [ "$1" = "--list-models" ]; then
  printf 'Available models\n\nauto - Auto (current, default)\nclaude-opus-5-5-high - Claude Opus 5.5 1M High\ngpt-5.6-sol-high - GPT-5.6 Sol 1M High\n'
  exit 0
fi
printf '%s\n' "$@" > "$(dirname "$0")/argv"
model=auto
while [ $# -gt 0 ]; do
  [ "$1" = "--model" ] && model=$2
  shift
done
printf '{"type":"system","subtype":"init","session_id":"s","model":"%s"}\n' "$model"
printf '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"PONG %s"}]}}\n' "$model"
printf '{"type":"result","subtype":"success","is_error":false,"result":"PONG %s","usage":{"inputTokens":3,"outputTokens":2}}\n' "$model"
"#;

async fn send(registry: &Arc<Registry>, request: Request<Body>) -> (Value, axum::http::HeaderMap) {
    let response = app(registry.clone())
        .oneshot(request)
        .await
        .expect("response");
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("body");
    assert_eq!(
        status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&bytes)
    );
    (serde_json::from_slice(&bytes).expect("json"), headers)
}

#[tokio::test]
async fn every_listed_cursor_cli_model_is_advertised_and_runs_read_only() {
    let home = tempfile::tempdir().expect("tempdir");
    let bin = home.path().join(".local/bin");
    std::fs::create_dir_all(&bin).expect("mkdir");
    let agent = bin.join("cursor-agent");
    std::fs::write(&agent, FAKE_CURSOR_AGENT).expect("write fake cli");
    std::fs::set_permissions(&agent, std::fs::Permissions::from_mode(0o755)).expect("chmod");
    // SAFETY: the only test in this binary, set before any thread reads it.
    unsafe {
        std::env::set_var("HOME", home.path());
        std::env::set_var("PATH", "/usr/bin:/bin");
        std::env::remove_var("XDG_CONFIG_HOME");
        std::env::remove_var("CCP_CURSOR_CLI_BINARY");
        std::env::remove_var("CCP_SHOW_ALL_MODELS");
        std::env::remove_var("CCP_CURSOR_CLI_ALLOW_WRITE");
        std::env::remove_var("CCP_CURSOR_CLI_DEFAULT_MODEL");
    }
    let registry = Arc::new(Registry::new(AliasProvider::Anthropic));

    let (models, _) = send(
        &registry,
        Request::builder()
            .method(Method::GET)
            .uri("/v1/models")
            .body(Body::empty())
            .expect("request"),
    )
    .await;
    let ids: Vec<&str> = models["data"]
        .as_array()
        .expect("data")
        .iter()
        .filter_map(|entry| entry["id"].as_str())
        .collect();
    for expected in [
        "cursor-cli",
        "cursor-cli:auto",
        "cursor-cli:claude-opus-5-5-high",
        "cursor-cli:gpt-5.6-sol-high",
    ] {
        assert!(ids.contains(&expected), "{expected} missing: {ids:?}");
    }
    assert!(
        !ids.iter().any(|id| id.starts_with("cursor-cli-agent")),
        "write ids leaked: {ids:?}"
    );
    let unknown = registry.unknown_model_message();
    assert!(unknown.contains("cursor-cli"), "{unknown}");
    assert!(!unknown.contains("cursor-cli:claude-opus"), "{unknown}");
    // The legacy alias keeps routing to the API-backed backend.
    assert_eq!(
        registry
            .provider_for_model("cursor-agent", None)
            .expect("routes")
            .name(),
        "cursor"
    );

    let (reply, headers) = send(
        &registry,
        Request::builder()
            .method(Method::POST)
            .uri("/v1/messages")
            .header("content-type", "application/json")
            .body(Body::from(
                json!({
                    "model": "cursor-cli:claude-opus-5-5-high",
                    "max_tokens": 50,
                    "stream": false,
                    "messages": [{"role": "user", "content": "Reply with exactly PONG"}]
                })
                .to_string(),
            ))
            .expect("request"),
    )
    .await;
    assert_eq!(reply["content"][0]["text"], "PONG claude-opus-5-5-high");
    assert_eq!(
        headers["x-claude-codex-served-by"],
        "cursor-cli/cursor-cli:claude-opus-5-5-high"
    );

    let argv = std::fs::read_to_string(bin.join("argv")).expect("cli was run");
    let argv: Vec<&str> = argv.lines().collect();
    let model_at = argv
        .iter()
        .position(|arg| *arg == "--model")
        .expect("--model");
    assert_eq!(argv[model_at + 1], "claude-opus-5-5-high", "{argv:?}");
    assert!(
        argv.windows(2).any(|pair| pair == ["--mode", "ask"]),
        "{argv:?}"
    );
    assert!(!argv.contains(&"--force"), "{argv:?}");
}
