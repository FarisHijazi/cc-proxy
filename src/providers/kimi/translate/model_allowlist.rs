use std::collections::HashMap;

pub const KIMI_DEFAULT_MODEL: &str = "kimi-for-coding";

static ALIAS_TARGETS: once_cell::sync::Lazy<HashMap<&'static str, &'static str>> =
    once_cell::sync::Lazy::new(|| {
        let mut m = HashMap::new();
        m.insert("haiku", KIMI_DEFAULT_MODEL);
        m.insert("claude-haiku-4-5", KIMI_DEFAULT_MODEL);
        m.insert("claude-haiku-4-5-20251001", KIMI_DEFAULT_MODEL);
        m.insert("sonnet", KIMI_DEFAULT_MODEL);
        m.insert("claude-sonnet-4-6", KIMI_DEFAULT_MODEL);
        m.insert("claude-sonnet-5", KIMI_DEFAULT_MODEL);
        m.insert("opus", KIMI_DEFAULT_MODEL);
        m.insert("claude-opus-4-7", KIMI_DEFAULT_MODEL);
        m.insert("claude-opus-4-8", KIMI_DEFAULT_MODEL);
        m.insert("claude-opus-5", KIMI_DEFAULT_MODEL);
        m.insert("fable", KIMI_DEFAULT_MODEL);
        m.insert("claude-fable-5", KIMI_DEFAULT_MODEL);
        m.insert("kimi-for-coding", KIMI_DEFAULT_MODEL);
        for upstream in ALLOWED_MODELS {
            m.insert(upstream, upstream);
        }
        m.insert("kimi-k3", "k3");
        m.insert("kimi-k3-256k", "k3-256k");
        m.insert("kimi-k3.1-pro", "k3.1-pro");
        m.insert("kimi-k3.1-pro-highspeed", "k3.1-pro-highspeed");
        m
    });

const ALLOWED_MODELS: &[&str] = &[
    "kimi-for-coding",
    "kimi-for-coding-highspeed",
    "k3",
    "k3-256k",
    "k3.1-pro",
    "k3.1-pro-highspeed",
];

const MOONSHOT_MODELS: &[(&str, &str)] = &[
    ("k3", "kimi-k3"),
    ("k3.1-pro", "kimi-k3.1-pro"),
    ("k3.1-pro-highspeed", "kimi-k3.1-pro-highspeed"),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EndpointKind {
    Coding,
    MoonshotOpenPlatform,
}

impl EndpointKind {
    fn from_base_url(base_url: &str) -> Self {
        match url::Url::parse(base_url) {
            Ok(url)
                if url.scheme() == "https"
                    && url.host_str() == Some("api.moonshot.ai")
                    && url.path().trim_end_matches('/') == "/v1" =>
            {
                Self::MoonshotOpenPlatform
            }
            _ => Self::Coding,
        }
    }
}

pub fn resolve_model(model: &str) -> String {
    resolve_model_for_endpoint(
        model,
        EndpointKind::from_base_url(&crate::providers::kimi::auth::constants::api_base_url()),
    )
}

fn resolve_model_for_endpoint(model: &str, endpoint: EndpointKind) -> String {
    let coding_model = ALIAS_TARGETS.get(model).copied().unwrap_or(model);
    match endpoint {
        EndpointKind::Coding => coding_model,
        EndpointKind::MoonshotOpenPlatform => MOONSHOT_MODELS
            .iter()
            .find(|(coding, _)| *coding == coding_model)
            .map(|(_, open)| *open)
            .unwrap_or(coding_model),
    }
    .to_string()
}

pub fn all_public_model_ids() -> &'static [&'static str] {
    &[
        "kimi-for-coding",
        "kimi-for-coding-highspeed",
        "k3",
        "k3-256k",
        "k3.1-pro",
        "k3.1-pro-highspeed",
        "kimi-k3",
        "kimi-k3-256k",
        "kimi-k3.1-pro",
        "kimi-k3.1-pro-highspeed",
    ]
}

pub fn public_model_ids() -> Vec<&'static str> {
    public_model_ids_for_endpoint(EndpointKind::from_base_url(
        &crate::providers::kimi::auth::constants::api_base_url(),
    ))
}

fn public_model_ids_for_endpoint(endpoint: EndpointKind) -> Vec<&'static str> {
    all_public_model_ids()
        .iter()
        .copied()
        .filter(|id| {
            let resolved = resolve_model_for_endpoint(id, endpoint);
            match endpoint {
                EndpointKind::Coding => ALLOWED_MODELS.contains(&resolved.as_str()),
                EndpointKind::MoonshotOpenPlatform => {
                    MOONSHOT_MODELS.iter().any(|(_, open)| *open == resolved)
                }
            }
        })
        .collect()
}

pub fn is_k3(model: &str) -> bool {
    ALIAS_TARGETS
        .get(model)
        .copied()
        .unwrap_or(model)
        .starts_with("k3")
}

pub fn assert_allowed_model(model: &str) -> Result<(), ModelNotAllowedError> {
    let endpoint =
        EndpointKind::from_base_url(&crate::providers::kimi::auth::constants::api_base_url());
    let allowed = match endpoint {
        EndpointKind::Coding => ALLOWED_MODELS.contains(&model),
        EndpointKind::MoonshotOpenPlatform => {
            MOONSHOT_MODELS.iter().any(|(_, open)| *open == model)
        }
    };
    if allowed {
        Ok(())
    } else {
        Err(ModelNotAllowedError {
            model: model.to_string(),
        })
    }
}

#[derive(Debug)]
pub struct ModelNotAllowedError {
    pub model: String,
}

impl std::fmt::Display for ModelNotAllowedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Model not allowed: {}", self.model)
    }
}

impl std::error::Error for ModelNotAllowedError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoint_classification_matches_only_moonshot_open_platform() {
        for base_url in ["https://api.moonshot.ai/v1", "https://api.moonshot.ai/v1/"] {
            assert_eq!(
                EndpointKind::from_base_url(base_url),
                EndpointKind::MoonshotOpenPlatform
            );
        }
        for base_url in [
            "https://api.kimi.ai/coding/v1",
            "https://api.kimi.com/coding/v1",
            "https://api.moonshot.ai/coding/v1",
            "https://api.moonshot.ai.example.com/v1",
            "https://example.com/v1",
            "http://api.moonshot.ai/v1",
            "not a URL",
        ] {
            assert_eq!(EndpointKind::from_base_url(base_url), EndpointKind::Coding);
        }
    }

    #[test]
    fn endpoint_catalog_resolves_both_public_and_coding_ids() {
        for (coding, open) in MOONSHOT_MODELS {
            for requested in [*coding, *open] {
                assert_eq!(
                    resolve_model_for_endpoint(requested, EndpointKind::Coding),
                    *coding
                );
                assert_eq!(
                    resolve_model_for_endpoint(requested, EndpointKind::MoonshotOpenPlatform),
                    *open
                );
                assert!(
                    public_model_ids_for_endpoint(EndpointKind::MoonshotOpenPlatform)
                        .contains(open)
                );
                assert!(is_k3(open));
            }
        }
        assert_eq!(
            resolve_model_for_endpoint("kimi-for-coding", EndpointKind::Coding),
            KIMI_DEFAULT_MODEL
        );
        assert_eq!(
            resolve_model_for_endpoint("kimi-k3-256k", EndpointKind::Coding),
            "k3-256k"
        );
    }

    #[test]
    fn resolve_haiku_to_default() {
        assert_eq!(resolve_model("haiku"), KIMI_DEFAULT_MODEL);
    }

    #[test]
    fn resolve_opus_4_8_to_default() {
        assert_eq!(resolve_model("claude-opus-4-8"), KIMI_DEFAULT_MODEL);
    }

    #[test]
    fn resolve_claude_5_aliases_to_default() {
        for model in [
            "claude-sonnet-5",
            "claude-opus-5",
            "fable",
            "claude-fable-5",
        ] {
            assert_eq!(resolve_model(model), KIMI_DEFAULT_MODEL);
        }
    }

    #[test]
    fn resolve_unknown_is_not_silently_substituted() {
        assert_eq!(resolve_model("unknown-model"), "unknown-model");
        assert!(assert_allowed_model(&resolve_model("unknown-model")).is_err());
    }

    #[test]
    fn resolve_kimi_for_coding() {
        assert_eq!(resolve_model("kimi-for-coding"), KIMI_DEFAULT_MODEL);
    }

    #[test]
    fn resolve_kimi_k3_to_k3() {
        assert_eq!(resolve_model("kimi-k3"), "k3");
    }

    #[test]
    fn resolve_k3_to_k3() {
        assert_eq!(resolve_model("k3"), "k3");
    }

    #[test]
    fn resolve_k3_1_models() {
        assert_eq!(resolve_model("kimi-k3.1-pro"), "k3.1-pro");
        assert_eq!(resolve_model("k3.1-pro-highspeed"), "k3.1-pro-highspeed");
        assert_eq!(
            resolve_model("kimi-for-coding-highspeed"),
            "kimi-for-coding-highspeed"
        );
        assert!(is_k3("k3.1-pro"));
        assert!(assert_allowed_model("k3.1-pro").is_ok());
    }

    #[test]
    fn moonshot_catalog_excludes_unsupported_coding_models() {
        let ids = public_model_ids_for_endpoint(EndpointKind::MoonshotOpenPlatform);
        assert_eq!(ids.len(), 6);
        assert!(ids.contains(&"kimi-k3.1-pro"));
        assert!(!ids.contains(&"kimi-for-coding"));
        assert!(!ids.contains(&"k3-256k"));
        assert!(!all_public_model_ids().contains(&"kimi-k2.6"));
    }

    #[test]
    fn k3_detected() {
        assert!(is_k3("k3"));
        assert!(!is_k3("kimi-for-coding"));
    }

    #[test]
    fn assert_allowed_accepts_default() {
        assert!(assert_allowed_model(KIMI_DEFAULT_MODEL).is_ok());
    }

    #[test]
    fn assert_allowed_accepts_k3() {
        assert!(assert_allowed_model("k3").is_ok());
    }

    #[test]
    fn assert_allowed_rejects_other() {
        assert!(assert_allowed_model("kimi-k2.6").is_err());
    }
}
