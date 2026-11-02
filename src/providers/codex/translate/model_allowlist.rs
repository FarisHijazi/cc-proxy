use crate::config;

use super::model_catalog::{ModelCatalog, catalog};
use super::request::ServiceTier;

pub const FAMILY_ALIASES: &[&str] = &["astra", "sol", "luna", "terra"];

pub const ALLOWED_MODELS: &[&str] = &[
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-6-luna",
    "gpt-6-sol",
    "gpt-6-astra",
    "gpt-6.1-sol",
];

pub fn selectable_models() -> Vec<String> {
    catalog().selectable_models()
}

pub const MODEL_ALIASES: &[(&str, &str)] = &[
    ("haiku", "gpt-5.6-luna"),
    ("claude-haiku-4-5", "gpt-5.6-luna"),
    ("claude-haiku-4-5-20251001", "gpt-5.6-luna"),
    ("sonnet", "gpt-5.6-terra"),
    ("claude-sonnet-4-6", "gpt-5.6-terra"),
    ("claude-sonnet-5", "gpt-5.6-terra"),
    ("opus", "gpt-5.6-sol"),
    ("claude-opus-4-7", "gpt-5.6-sol"),
    ("claude-opus-4-8", "gpt-5.6-sol"),
    ("claude-opus-5", "gpt-5.6-sol"),
    ("fable", "gpt-5.6-sol"),
    ("claude-fable-5", "gpt-5.6-sol"),
];

#[derive(Debug, Clone)]
pub struct ResolvedModel {
    pub model: String,
    pub service_tier: Option<ServiceTier>,
}

pub async fn prepare_model_catalog(model: Option<&str>) {
    let needs_discovery = |model: &str| {
        let normalized = crate::registry::normalize_incoming_model(model);
        let base = normalized.strip_suffix("-fast").unwrap_or(&normalized);
        FAMILY_ALIASES.contains(&base)
            || (base.starts_with("gpt-") && !ALLOWED_MODELS.contains(&base))
    };
    if model.is_some_and(|model| !needs_discovery(model))
        && !config::codex_model()
            .as_deref()
            .is_some_and(needs_discovery)
    {
        return;
    }
    super::model_catalog::prepare_catalog().await;
}

fn resolve_selection(model: &str) -> ResolvedModel {
    let normalized = crate::registry::normalize_incoming_model(model);
    let base = normalized.strip_suffix("-fast").unwrap_or(&normalized);
    if ALLOWED_MODELS.contains(&base) || MODEL_ALIASES.iter().any(|(alias, _)| *alias == base) {
        ModelCatalog::default().resolve(model)
    } else {
        catalog().resolve(model)
    }
}

pub fn resolve_model_request(model: &str) -> ResolvedModel {
    resolve_model_request_with_config_override(model, true)
}

pub fn resolve_model_request_with_config_override(
    model: &str,
    apply_config_override: bool,
) -> ResolvedModel {
    let requested = resolve_selection(model);
    let override_model = apply_config_override.then(config::codex_model).flatten();
    let resolved = match override_model {
        Some(ref val) if !val.is_empty() => resolve_selection(val),
        _ => requested.clone(),
    };

    ResolvedModel {
        model: resolved.model,
        service_tier: if requested.service_tier == Some(ServiceTier::Priority)
            || resolved.service_tier == Some(ServiceTier::Priority)
        {
            Some(ServiceTier::Priority)
        } else {
            resolved.service_tier
        },
    }
}

pub fn resolve_model(model: &str) -> String {
    resolve_model_request(model).model
}

#[derive(Debug, Clone)]
pub struct ModelNotAllowedError {
    pub model: String,
}

impl std::fmt::Display for ModelNotAllowedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Model not allowed: {}", self.model)
    }
}

pub fn assert_allowed_model(model: &str) -> Result<(), ModelNotAllowedError> {
    if ALLOWED_MODELS.contains(&model) || catalog().contains(model) {
        Ok(())
    } else {
        Err(ModelNotAllowedError {
            model: model.to_string(),
        })
    }
}

pub fn uses_responses_lite(model: &str) -> bool {
    matches!(model, "gpt-5.6-luna" | "gpt-5.6-sol" | "gpt-5.6-terra")
}

/// `gpt-5.6-luna` exists only behind the Responses Lite lane; the full
/// Responses API resolves it to a `-free` variant and returns 404 (Model not
/// found gpt-5.6-luna-free-...). Hosted web_search requests must run on the
/// full lane, so luna is upgraded to its nearest full-lane sibling.
pub fn full_lane_web_search_model(model: &str) -> &str {
    if model == "gpt-5.6-luna" {
        "gpt-5.6-sol"
    } else {
        model
    }
}

pub fn is_valid_model_for_codex(model: &str) -> bool {
    let normalized = crate::registry::normalize_incoming_model(model);
    let base = normalized.strip_suffix("-fast").unwrap_or(&normalized);
    if ALLOWED_MODELS.contains(&base) || MODEL_ALIASES.iter().any(|(alias, _)| *alias == base) {
        return true;
    }
    if !base.starts_with("gpt-") && !FAMILY_ALIASES.contains(&base) {
        return false;
    }
    let catalog = catalog();
    catalog.contains(&catalog.resolve(model).model)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn haiku_resolves_to_luna() {
        let r = resolve_model_request("haiku");
        assert_eq!(r.model, "gpt-5.6-luna");
    }

    #[test]
    fn web_search_upgrades_luna_to_full_lane_sibling() {
        assert_eq!(full_lane_web_search_model("gpt-5.6-luna"), "gpt-5.6-sol");
        assert_eq!(full_lane_web_search_model("gpt-5.6-sol"), "gpt-5.6-sol");
        assert_eq!(full_lane_web_search_model("gpt-5.6-terra"), "gpt-5.6-terra");
        assert_eq!(full_lane_web_search_model("gpt-5.4"), "gpt-5.4");
    }

    #[test]
    fn sonnet_resolves_to_terra() {
        let r = resolve_model_request("sonnet");
        assert_eq!(r.model, "gpt-5.6-terra");
    }

    #[test]
    fn sonnet_5_resolves_to_terra() {
        let r = resolve_model_request("claude-sonnet-5");
        assert_eq!(r.model, "gpt-5.6-terra");
    }

    #[test]
    fn opus_resolves_to_sol() {
        let r = resolve_model_request("opus");
        assert_eq!(r.model, "gpt-5.6-sol");
    }

    #[test]
    fn opus_aliases_resolve_to_sol() {
        for model in ["claude-opus-4-8", "claude-opus-5"] {
            let r = resolve_model_request(model);
            assert_eq!(r.model, "gpt-5.6-sol");
        }
    }

    #[test]
    fn fable_5_resolves_to_sol() {
        for model in ["fable", "claude-fable-5"] {
            let r = resolve_model_request(model);
            assert_eq!(r.model, "gpt-5.6-sol");
        }
    }

    #[test]
    fn fast_suffix_adds_priority() {
        let r = resolve_model_request("gpt-6.1-sol-fast");
        assert_eq!(r.model, "gpt-6.1-sol");
        assert_eq!(r.service_tier, Some(ServiceTier::Priority));
    }

    #[test]
    fn new_gpt_6_models_use_the_full_responses_lane() {
        for model in ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"] {
            assert!(!uses_responses_lite(model), "{model}");
        }
    }

    #[test]
    fn allowed_models_accept_base() {
        for model in [
            "gpt-5.4",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
        ] {
            assert!(assert_allowed_model(model).is_ok(), "{model}");
        }
    }

    #[test]
    fn not_allowed_rejected() {
        assert!(assert_allowed_model("gpt-7").is_err());
    }
}
