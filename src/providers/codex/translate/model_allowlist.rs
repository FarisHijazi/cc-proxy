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
    "gpt-6.1-terra",
];

pub fn selectable_models() -> Vec<String> {
    catalog().selectable_models()
}

pub fn allowed_models_display() -> String {
    selectable_models().join(", ")
}

/// Models whose Codex catalog entry offers the `ultrafast` service tier.
/// Every other model offers `priority` at most, so it gets no `-ultrafast` form.
pub const ULTRAFAST_MODELS: &[&str] = &["gpt-6-astra", "gpt-6.1-sol"];

const FAST_SUFFIX: &str = "-fast";
const ULTRAFAST_SUFFIX: &str = "-ultrafast";

/// Splits a tier suffix off a name without checking the model offers it.
pub(super) fn split_tier_name(model: &str) -> (&str, Option<ServiceTier>) {
    if let Some(base) = model.strip_suffix(ULTRAFAST_SUFFIX) {
        (base, Some(ServiceTier::Ultrafast))
    } else if let Some(base) = model.strip_suffix(FAST_SUFFIX) {
        (base, Some(ServiceTier::Priority))
    } else {
        (model, None)
    }
}

pub(super) fn offers_tier(model: &str, tier: &ServiceTier) -> bool {
    *tier != ServiceTier::Ultrafast || ULTRAFAST_MODELS.contains(&model)
}

pub(super) fn tier_suffixes(model: &str) -> &'static [&'static str] {
    if ULTRAFAST_MODELS.contains(&model) {
        &[FAST_SUFFIX, ULTRAFAST_SUFFIX]
    } else {
        &[FAST_SUFFIX]
    }
}

/// Local tier-suffixed names advertised next to a registered model.
pub fn tier_model_variants(model: &str) -> Vec<String> {
    tier_suffixes(model)
        .iter()
        .map(|suffix| format!("{model}{suffix}"))
        .collect()
}

/// Narrows a requested tier to one `model` offers. The backend serves an
/// unlisted tier at standard speed without an error, so ultrafast on a model
/// outside [`ULTRAFAST_MODELS`] falls back to priority, the fastest tier there.
pub fn service_tier_for_model(model: &str, tier: ServiceTier) -> ServiceTier {
    if offers_tier(model, &tier) {
        tier
    } else {
        ServiceTier::Priority
    }
}

pub const MODEL_ALIASES: &[(&str, &str)] = &[
    ("haiku", "gpt-6-luna"),
    ("claude-haiku-4-5", "gpt-6-luna"),
    ("claude-haiku-4-5-20251001", "gpt-6-luna"),
    ("sonnet", "gpt-5.6-terra"),
    ("claude-sonnet-4-6", "gpt-5.6-terra"),
    ("claude-sonnet-5", "gpt-5.6-terra"),
    ("opus", "gpt-6-sol"),
    ("claude-opus-4-7", "gpt-6-sol"),
    ("claude-opus-4-8", "gpt-6-sol"),
    ("claude-opus-5", "gpt-6-sol"),
    ("claude-opus-5-5", "gpt-6-sol"),
    ("fable", "gpt-6-sol"),
    ("claude-fable-5", "gpt-6-sol"),
];

#[derive(Debug, Clone)]
pub struct ResolvedModel {
    pub model: String,
    pub service_tier: Option<ServiceTier>,
}

pub async fn prepare_model_catalog(model: Option<&str>) {
    let needs_discovery = |model: &str| {
        let normalized = crate::registry::normalize_incoming_model(model);
        let (base, _) = split_tier_name(&normalized);
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
    let (base, _) = split_tier_name(&normalized);
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
    let override_model = apply_config_override.then(config::codex_model).flatten();
    resolve_with_model_override(model, override_model.as_deref())
}

fn resolve_with_model_override(model: &str, override_model: Option<&str>) -> ResolvedModel {
    let requested = resolve_selection(model);
    let resolved = match override_model {
        Some(val) if !val.is_empty() => resolve_selection(val),
        _ => requested.clone(),
    };
    // A tier suffix on the override wins; otherwise the requested suffix
    // carries over to the override model, which may not offer that tier.
    let service_tier = resolved
        .service_tier
        .or(requested.service_tier)
        .map(|tier| service_tier_for_model(&resolved.model, tier));
    ResolvedModel {
        model: resolved.model,
        service_tier,
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
    matches!(
        model,
        "gpt-5.6-luna"
            | "gpt-5.6-sol"
            | "gpt-5.6-terra"
            | "gpt-6-astra"
            | "gpt-6-luna"
            | "gpt-6-sol"
            | "gpt-6.1-sol"
            | "gpt-6.1-terra"
    )
}

/// `CCP_CODEX_FULL_LANE` opts the Lite models that also exist on the full lane
/// out of Lite; luna has no full-lane twin.
pub fn uses_responses_lite_with_full_lane(model: &str, full_lane: bool) -> bool {
    if full_lane
        && matches!(
            model,
            "gpt-5.6-sol"
                | "gpt-5.6-terra"
                | "gpt-6-astra"
                | "gpt-6-sol"
                | "gpt-6.1-sol"
                | "gpt-6.1-terra"
        )
    {
        return false;
    }
    uses_responses_lite(model)
}

/// Luna models exist only behind the Responses Lite lane; the full
/// Responses API resolves them to a `-free` variant and returns 404 (Model not
/// found gpt-5.6-luna-free-...). Hosted web_search requests must run on the
/// full lane, so luna is upgraded to its nearest full-lane sibling.
pub fn full_lane_web_search_model(model: &str) -> &str {
    match model {
        "gpt-5.6-luna" => "gpt-5.6-sol",
        "gpt-6-luna" => "gpt-6-sol",
        _ => model,
    }
}

pub fn is_valid_model_for_codex(model: &str) -> bool {
    let normalized = crate::registry::normalize_incoming_model(model);
    let (base, _) = split_tier_name(&normalized);
    let catalog = if ALLOWED_MODELS.contains(&base)
        || MODEL_ALIASES.iter().any(|(alias, _)| *alias == base)
    {
        ModelCatalog::default()
    } else if base.starts_with("gpt-") || FAMILY_ALIASES.contains(&base) {
        catalog()
    } else {
        return false;
    };
    catalog.contains(&catalog.resolve(model).model)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn haiku_resolves_to_luna() {
        let r = resolve_model_request("haiku");
        assert_eq!(r.model, "gpt-6-luna");
    }

    #[test]
    fn web_search_upgrades_luna_to_full_lane_sibling() {
        assert_eq!(full_lane_web_search_model("gpt-5.6-luna"), "gpt-5.6-sol");
        assert_eq!(full_lane_web_search_model("gpt-5.6-sol"), "gpt-5.6-sol");
        assert_eq!(full_lane_web_search_model("gpt-5.6-terra"), "gpt-5.6-terra");
        assert_eq!(full_lane_web_search_model("gpt-5.4"), "gpt-5.4");
        assert_eq!(full_lane_web_search_model("gpt-6-luna"), "gpt-6-sol");
        assert_eq!(full_lane_web_search_model("gpt-6-sol"), "gpt-6-sol");
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
        assert_eq!(r.model, "gpt-6-sol");
    }

    #[test]
    fn opus_aliases_resolve_to_sol() {
        for model in ["claude-opus-4-8", "claude-opus-5"] {
            let r = resolve_model_request(model);
            assert_eq!(r.model, "gpt-6-sol");
        }
    }

    #[test]
    fn fable_5_resolves_to_sol() {
        for model in ["fable", "claude-fable-5"] {
            let r = resolve_model_request(model);
            assert_eq!(r.model, "gpt-6-sol");
        }
    }

    #[test]
    fn fast_suffix_adds_priority() {
        let r = resolve_model_request("gpt-6.1-sol-fast");
        assert_eq!(r.model, "gpt-6.1-sol");
        assert_eq!(r.service_tier, Some(ServiceTier::Priority));
    }

    #[test]
    fn gpt_6_models_use_responses_lite() {
        for model in [
            "gpt-6.1-sol",
            "gpt-6.1-terra",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
        ] {
            assert!(uses_responses_lite(model), "{model}");
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
        assert!(assert_allowed_model("gpt-7-fast").is_err());
    }

    #[test]
    fn full_lane_opts_lite_models_out_except_luna() {
        for model in [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6.1-sol",
            "gpt-6.1-terra",
        ] {
            assert!(uses_responses_lite_with_full_lane(model, false), "{model}");
            assert!(!uses_responses_lite_with_full_lane(model, true), "{model}");
        }
        for model in ["gpt-5.6-luna", "gpt-6-luna"] {
            assert!(uses_responses_lite_with_full_lane(model, true), "{model}");
        }
        for model in ["gpt-5.2", "gpt-5.4", "gpt-5.5"] {
            assert!(!uses_responses_lite_with_full_lane(model, false), "{model}");
        }
    }

    #[test]
    fn gpt_6_fast_adds_priority() {
        for model in ["gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra"] {
            let r = resolve_model_request(&format!("{model}-fast"));
            assert_eq!(r.model, model);
            assert_eq!(r.service_tier, Some(ServiceTier::Priority));
        }
    }

    #[test]
    fn ultrafast_suffix_adds_ultrafast_tier() {
        for model in ULTRAFAST_MODELS {
            assert!(ALLOWED_MODELS.contains(model), "{model}");
            let name = format!("{model}-ultrafast");
            assert!(is_valid_model_for_codex(&name), "{name}");
            let r = resolve_with_model_override(&name, None);
            assert_eq!(r.model, *model);
            assert_eq!(r.service_tier, Some(ServiceTier::Ultrafast));
        }
    }

    #[test]
    fn ultrafast_suffix_is_rejected_for_models_without_the_tier() {
        for model in ALLOWED_MODELS {
            if ULTRAFAST_MODELS.contains(model) {
                continue;
            }
            let name = format!("{model}-ultrafast");
            assert!(!is_valid_model_for_codex(&name), "{name}");
            let r = resolve_with_model_override(&name, None);
            assert_eq!(r.service_tier, None, "{name}");
            assert!(assert_allowed_model(&r.model).is_err(), "{name}");
        }
    }

    #[test]
    fn service_tier_narrows_to_what_the_model_offers() {
        for (model, tier, expected) in [
            (
                "gpt-6-astra",
                ServiceTier::Ultrafast,
                ServiceTier::Ultrafast,
            ),
            (
                "gpt-6.1-sol",
                ServiceTier::Ultrafast,
                ServiceTier::Ultrafast,
            ),
            ("gpt-6-sol", ServiceTier::Ultrafast, ServiceTier::Priority),
            ("gpt-6-sol", ServiceTier::Flex, ServiceTier::Flex),
        ] {
            assert_eq!(
                service_tier_for_model(model, tier.clone()),
                expected,
                "{model} {tier:?}"
            );
        }
    }

    #[test]
    fn tier_model_variants_lists_supported_suffixes() {
        assert_eq!(
            tier_model_variants("gpt-6-astra"),
            ["gpt-6-astra-fast", "gpt-6-astra-ultrafast"]
        );
        assert_eq!(tier_model_variants("gpt-6-sol"), ["gpt-6-sol-fast"]);
        let listed = selectable_models();
        assert!(listed.iter().any(|m| m == "gpt-6-astra-ultrafast"));
        assert!(!listed.iter().any(|m| m == "gpt-6-sol-ultrafast"));
    }

    #[test]
    fn model_override_merges_tier_suffixes() {
        let cases: &[(&str, Option<&str>, &str, Option<ServiceTier>)] = &[
            ("gpt-6-sol", None, "gpt-6-sol", None),
            ("gpt-6-sol", Some(""), "gpt-6-sol", None),
            (
                "gpt-6-sol-fast",
                None,
                "gpt-6-sol",
                Some(ServiceTier::Priority),
            ),
            (
                "gpt-6-sol",
                Some("gpt-6-astra-fast"),
                "gpt-6-astra",
                Some(ServiceTier::Priority),
            ),
            (
                "gpt-6-sol-fast",
                Some("gpt-6-astra"),
                "gpt-6-astra",
                Some(ServiceTier::Priority),
            ),
            (
                "gpt-6-sol",
                Some("gpt-6-astra-ultrafast"),
                "gpt-6-astra",
                Some(ServiceTier::Ultrafast),
            ),
            (
                "gpt-6-astra-ultrafast",
                Some("gpt-6-astra-fast"),
                "gpt-6-astra",
                Some(ServiceTier::Priority),
            ),
            (
                "gpt-6-astra-ultrafast",
                Some("gpt-6-astra"),
                "gpt-6-astra",
                Some(ServiceTier::Ultrafast),
            ),
            (
                "gpt-6-astra-ultrafast",
                Some("gpt-6-sol"),
                "gpt-6-sol",
                Some(ServiceTier::Priority),
            ),
            ("opus", Some("gpt-6-astra"), "gpt-6-astra", None),
        ];
        for (requested, override_model, model, tier) in cases {
            let r = resolve_with_model_override(requested, *override_model);
            assert_eq!(r.model, *model, "{requested} + {override_model:?}");
            assert_eq!(&r.service_tier, tier, "{requested} + {override_model:?}");
        }
    }
}
