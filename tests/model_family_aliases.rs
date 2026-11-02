use claude_codex::{
    config::AliasProvider,
    providers::codex::{
        chat_completions::request::translate_request,
        native::validate_native_request_model,
        translate::model_allowlist::{
            assert_allowed_model, resolve_model_request, selectable_models,
        },
    },
    registry::Registry,
};
use serde_json::json;

#[tokio::test]
async fn provider_cache_drives_aliases_validation_and_registry_without_rewriting_pins() {
    let home = tempfile::tempdir().unwrap();
    unsafe { std::env::set_var("CODEX_HOME", home.path()) };
    std::fs::write(
        home.path().join("models_cache.json"),
        json!({"models":[
            {"slug":"gpt-6.1-astra","supported_in_api":true,"visibility":"list"},
            {"slug":"gpt-6.9-sol","supported_in_api":true,"visibility":"list"},
            {"slug":"gpt-6.10-sol","supported_in_api":true,"visibility":"list"},
            {"slug":"gpt-6.1-terra","supported_in_api":true,"visibility":"list"},
            {"slug":"gpt-99-astra","supported_in_api":false,"visibility":"list"},
            {"slug":"gpt-98-astra","supported_in_api":true,"visibility":"hide"},
            {"slug":"gpt-next-astra","supported_in_api":true,"visibility":"list"}
        ]})
        .to_string(),
    )
    .unwrap();
    let registry = Registry::new(AliasProvider::Anthropic);
    for (alias, target) in [
        ("astra", "gpt-6.1-astra"),
        ("sol", "gpt-6.10-sol"),
        ("luna", "gpt-6-luna"),
        ("terra", "gpt-6.1-terra"),
    ] {
        for model in [
            alias.to_string(),
            format!("{alias}[1m]"),
            format!("{alias}-fast[1m]"),
        ] {
            let resolved = resolve_model_request(&model);
            assert_eq!(resolved.model, target);
            assert_eq!(resolved.service_tier.is_some(), model.contains("-fast"));
            assert!(assert_allowed_model(&resolved.model).is_ok());
            assert_eq!(
                registry.provider_for_model(&model, None).unwrap().name(),
                "codex"
            );
            assert!(validate_native_request_model(&json!({"model":model})).is_ok());
            let chat = translate_request(
                json!({"model":model,"messages":[{"role":"user","content":"hi"}]}),
            )
            .unwrap();
            assert_eq!(chat.model, target);
        }
    }
    for model in ["gpt-6-astra", "gpt-6-sol"] {
        assert_eq!(resolve_model_request(model).model, model);
    }
    let listed = selectable_models();
    for model in ["astra", "astra-fast", "gpt-6.1-astra", "gpt-6.1-astra-fast"] {
        assert!(listed.iter().any(|v| v == model));
        assert!(
            registry
                .supported_models_for("codex")
                .iter()
                .any(|v| v == model)
        );
    }
    for model in ["gpt-99-astra", "gpt-98-astra", "gpt-next-astra"] {
        assert!(assert_allowed_model(model).is_err());
        assert!(registry.provider_for_model(model, None).is_none());
    }
    for model in ["opus[1m]", "sonnet[1m]", "haiku[1m]", "fable[1m]"] {
        assert_eq!(
            registry.provider_for_model(model, None).unwrap().name(),
            "anthropic"
        );
    }
    use tower::ServiceExt;
    let registry = std::sync::Arc::new(registry);
    for model in ["astra[1m]", "astra-fast[1m]", "terra", "gpt-6-astra[1m]"] {
        let request = axum::http::Request::post("/v1/messages/count_tokens")
            .header("content-type", "application/json")
            .body(axum::body::Body::from(
                json!({"model":model,"messages":[{"role":"user","content":"hi"}]}).to_string(),
            ))
            .unwrap();
        let response = claude_codex::server::app(registry.clone())
            .oneshot(request)
            .await
            .unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::OK, "{model}");
    }
    unsafe { std::env::remove_var("CODEX_HOME") };
}
