use crate::{
    anthropic::{json_error, schema::MessagesRequest},
    config::AliasProvider,
    provider::{Availability, CliHandlers, Provider, RequestContext},
    providers::codex::translate::model_allowlist::{is_valid_model_for_codex, selectable_models},
};
use anyhow::{Result, anyhow};
use async_trait::async_trait;
use axum::{http::StatusCode, response::Response};
use std::collections::BTreeMap;
use std::sync::Arc;

pub const ANTHROPIC_STYLE_ALIASES: &[&str] = &[
    "haiku",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "sonnet",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "opus",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "fable",
    "claude-fable-5",
];

pub const CURSOR_PREFIXES: &[&str] = &["cursor:", "cursor-plan:", "cursor-ask:"];

const CURSOR_LEGACY_MODELS: &[&str] = &[
    "cursor",
    "cursor-agent",
    "cursor-composer",
    "cursor-composer-fast",
    "cursor-plan",
    "cursor-ask",
    "composer-2.5",
    "composer-2.5-fast",
];

pub(crate) use crate::providers::gemini::models::GEMINI_MODELS;
pub(crate) const GROK_MODELS: &[&str] = &["grok-composer-2.5-fast", "grok-4.5"];

pub struct Registry {
    alias_provider: AliasProvider,
    models: BTreeMap<String, Vec<String>>,
    handlers: BTreeMap<String, Arc<dyn Provider>>,
    pub(crate) cooldowns: crate::fallback::Cooldowns,
    pub(crate) routes: crate::fallback::Routes,
}

impl Registry {
    pub fn new(alias_provider: AliasProvider) -> Self {
        let mut models: BTreeMap<String, Vec<String>> = BTreeMap::new();
        models.insert(
            "anthropic".into(),
            ANTHROPIC_STYLE_ALIASES
                .iter()
                .map(|alias| (*alias).to_string())
                .collect(),
        );
        models.insert("codex".into(), Vec::new());
        models.insert(
            "kimi".into(),
            crate::providers::kimi::translate::model_allowlist::all_public_model_ids()
                .iter()
                .map(|m| (*m).to_string())
                .collect(),
        );
        models.insert("cursor".into(), build_cursor_models());
        models.insert(
            "gemini".into(),
            GEMINI_MODELS.iter().map(|m| (*m).to_string()).collect(),
        );
        // Bare selectors only; `all_supported_models` adds the per-model ids
        // from `cursor-agent --list-models` for `/v1/models`, never at startup.
        models.insert(
            "cursor-cli".into(),
            crate::providers::cursor_cli::models::supported_models(),
        );
        models.insert(
            "grok".into(),
            GROK_MODELS
                .iter()
                .map(|model| (*model).to_string())
                .collect(),
        );
        models.insert(
            "zai".into(),
            crate::providers::zai::ZAI_MODELS
                .iter()
                .map(|model| (*model).to_string())
                .collect(),
        );
        models.insert(
            "local".into(),
            crate::providers::gemini::GeminiProvider::local().supported_models(),
        );
        let mut handlers = BTreeMap::new();
        for (name, entries) in &models {
            let handler: Arc<dyn Provider> = match name.as_str() {
                "anthropic" => Arc::new(crate::providers::anthropic::AnthropicProvider::new()),
                "codex" => Arc::new(crate::providers::codex::CodexProvider::new()),
                "kimi" => Arc::new(crate::providers::kimi::KimiProvider::new()),
                "cursor" => Arc::new(crate::providers::cursor::CursorProvider::new()),
                "cursor-cli" => Arc::new(crate::providers::cursor_cli::CursorCliProvider::new()),
                "gemini" => Arc::new(crate::providers::gemini::GeminiProvider::new()),
                "local" => Arc::new(crate::providers::gemini::GeminiProvider::local()),
                "grok" => Arc::new(crate::providers::grok::GrokProvider::new()),
                "zai" => Arc::new(crate::providers::zai::ZaiProvider::new()),
                _ => Arc::new(PlaceholderProvider::new(name, entries.clone())),
            };
            handlers.insert(name.clone(), handler);
        }

        Self {
            alias_provider,
            models,
            handlers,
            cooldowns: crate::fallback::Cooldowns::default(),
            routes: crate::fallback::Routes::default(),
        }
    }

    pub fn with_default_alias() -> Self {
        Self::new(crate::config::alias_provider())
    }

    pub fn from_providers(
        alias_provider: AliasProvider,
        providers: impl IntoIterator<Item = Arc<dyn Provider>>,
    ) -> Self {
        let mut models = BTreeMap::new();
        let mut handlers = BTreeMap::new();
        for provider in providers {
            let name = provider.name().to_string();
            models.insert(name.clone(), provider.supported_models());
            handlers.insert(name, provider);
        }
        Self {
            alias_provider,
            models,
            handlers,
            cooldowns: crate::fallback::Cooldowns::default(),
            routes: crate::fallback::Routes::default(),
        }
    }

    pub fn list_provider_names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.handlers.keys().cloned().collect();
        names.sort_unstable();
        names
    }

    pub fn provider(&self, name: &str) -> Option<Arc<dyn Provider>> {
        self.handlers.get(name).cloned()
    }

    pub fn supported_models_for(&self, provider: &str) -> Vec<String> {
        let live = (provider == "anthropic")
            .then(crate::providers::anthropic::live_models)
            .flatten();
        let mut models = if provider == "kimi" {
            crate::providers::kimi::translate::model_allowlist::public_model_ids()
                .into_iter()
                .map(str::to_string)
                .collect()
        } else if provider == "codex" {
            selectable_models()
        } else if let Some(live) = live {
            ANTHROPIC_STYLE_ALIASES
                .iter()
                .filter(|alias| !alias.contains('-'))
                .map(|alias| (*alias).to_string())
                .chain(live)
                .collect()
        } else {
            self.models.get(provider).cloned().unwrap_or_default()
        };
        if provider == self.alias_provider.as_str() && provider != "anthropic" {
            for alias in ANTHROPIC_STYLE_ALIASES {
                if !models.iter().any(|value| value == alias) {
                    models.push((*alias).to_string());
                }
            }
        }
        models.sort_unstable();
        models
    }

    /// Backends that can serve a request right now, with why the others cannot.
    ///
    /// Probed on demand rather than cached: this runs only when models are
    /// listed, and a fresh answer means signing into a backend takes effect
    /// without restarting the proxy.
    pub fn availability(&self) -> BTreeMap<String, Availability> {
        self.handlers
            .iter()
            .map(|(name, provider)| (name.clone(), provider.availability()))
            .collect()
    }

    /// Provider names worth listing — everything, if `CCP_SHOW_ALL_MODELS` is set.
    fn listable_providers(&self) -> Vec<String> {
        if crate::config::show_all_models() {
            return self.handlers.keys().cloned().collect();
        }
        self.handlers
            .iter()
            .filter(|(_, provider)| provider.availability().is_ready())
            .map(|(name, _)| name.clone())
            .collect()
    }

    /// The full `/v1/models` listing, including every `cursor-cli:<id>` the
    /// signed-in CLI offers. That can spawn `cursor-agent`, so async callers
    /// run it on a blocking thread. The grouped summaries stay static.
    pub fn all_supported_models(&self) -> Vec<(String, String)> {
        let mut out = Vec::new();
        for provider in self.listable_providers() {
            let mut models = self.supported_models_for(&provider);
            if provider == "cursor-cli" {
                models.extend(crate::providers::cursor_cli::models::discovered_model_ids());
            }
            for model in models {
                out.push((model, provider.clone()));
            }
        }
        out
    }

    pub fn grouped_models(&self) -> BTreeMap<String, Vec<String>> {
        let mut out = BTreeMap::new();
        for provider in self.listable_providers() {
            let models = self.supported_models_for(&provider);
            out.insert(provider, models);
        }
        out
    }

    /// Every backend's models, available or not. For diagnostics only.
    pub fn grouped_models_all(&self) -> BTreeMap<String, Vec<String>> {
        self.handlers
            .keys()
            .map(|provider| (provider.clone(), self.supported_models_for(provider)))
            .collect()
    }

    /// Model identities remain available for routing views even when a login is absent.
    pub fn provider_catalog(&self) -> serde_json::Value {
        let groups = self.grouped_models_all();
        let labels = [
            ("anthropic", "Claude"),
            ("codex", "Codex"),
            ("kimi", "Kimi"),
            ("cursor", "Cursor API"),
            ("cursor-cli", "cursor-agent CLI"),
            ("gemini", "Gemini"),
            ("grok", "Grok"),
            ("local", "Local"),
            ("zai", "Z.ai GLM"),
        ];
        let mut catalog = serde_json::Map::new();
        for (provider, models) in groups {
            let label = labels
                .iter()
                .find(|(name, _)| *name == provider)
                .map(|(_, label)| *label)
                .unwrap_or(&provider);
            let family = if provider == "anthropic" {
                "claude"
            } else {
                &provider
            };
            let mut entry = serde_json::json!({"label": label, "family": family, "models": models});
            if provider == "kimi" {
                entry["models"] = serde_json::json!(
                    crate::providers::kimi::translate::model_allowlist::all_public_model_ids()
                );
                let base = crate::config::kimi_base_url();
                entry["balance_base_url"] = serde_json::json!(if base.trim_end_matches('/')
                    == "https://api.moonshot.ai/v1"
                    || crate::providers::kimi::client::api_key().is_some()
                {
                    Some("https://api.moonshot.ai/v1")
                } else {
                    None
                });
            }
            catalog.insert(provider, entry);
        }
        serde_json::Value::Object(catalog)
    }

    pub async fn prepare_model_catalog(&self, raw_model: &str) {
        let normalized = normalize_incoming_model(raw_model);
        let provider = self.provider_for_model(&normalized, None);
        if provider
            .as_ref()
            .is_some_and(|provider| provider.name() == "codex")
            || (provider.is_none() && normalized.starts_with("gpt-"))
        {
            crate::providers::codex::translate::model_allowlist::prepare_model_catalog(Some(
                &normalized,
            ))
            .await;
        }
    }

    pub fn provider_for_model(
        &self,
        raw_model: &str,
        session_affinity: Option<&AliasProvider>,
    ) -> Option<Arc<dyn Provider>> {
        let normalized = route_model(raw_model);
        // Claude-shaped models always resolve to the configured alias target (the
        // Anthropic passthrough by default). Session affinity is deliberately NOT
        // consulted here: a codex request earlier in the same session must never drag
        // the opus/haiku slots off the Anthropic backend. This is what lets the opus
        // slot stay on Max while the sonnet slot runs on codex within one session.
        let _ = session_affinity;
        if is_anthropic_alias(&normalized) || normalized.starts_with("claude-") {
            return self.handlers.get(self.alias_provider.as_str()).cloned();
        }
        // Checked before the API-backed cursor backend so a `cursor-cli*` id
        // can never be swallowed by a `cursor*` prefix match.
        if crate::providers::cursor_cli::models::is_cursor_cli_model(&normalized) {
            return self.handlers.get("cursor-cli").cloned();
        }
        if is_cursor_model(&normalized) {
            return self.handlers.get("cursor").cloned();
        }

        if is_valid_model_for_codex(&normalized) {
            return self.handlers.get("codex").cloned();
        }

        // Exact model-name match reaches a specific backend regardless of the alias
        // target: this is how `ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5.6-terra` sends the
        // sonnet slot to codex even while aliases default to the Anthropic passthrough.
        for (name, models) in &self.models {
            if name == "anthropic" {
                continue;
            }
            if models.iter().any(|candidate| candidate == &normalized) {
                return self.handlers.get(name).cloned();
            }
        }
        // Any other gemini id goes to gemini too: the backend forwards unlisted
        // `gemini-*` ids verbatim (`gemini::models::resolve_model`), so a model
        // the server adds, or one we deliberately stop listing, still routes.
        if normalized.starts_with("gemini-") {
            return self.handlers.get("gemini").cloned();
        }
        // Z.ai serves every GLM id behind one endpoint, listed or not.
        if normalized.starts_with("glm-") {
            return self.handlers.get("zai").cloned();
        }

        None
    }

    pub fn unknown_model_message(&self) -> String {
        let mut parts = Vec::new();
        for (provider, models) in self.grouped_models() {
            let mut models = models;
            models.sort_unstable();
            parts.push(format!("{}: {}", provider, models.join(", ")));
        }
        let mut message = format!("Supported: {}.", parts.join("; "));
        // Name what is missing and how to get it, or a signed-out backend looks
        // like a backend that was never built.
        let hidden: Vec<String> = self
            .availability()
            .into_iter()
            .filter_map(|(provider, state)| match state {
                Availability::Unavailable { hint } => Some(format!("{provider} ({hint})")),
                Availability::Ready => None,
            })
            .collect();
        if !hidden.is_empty() {
            message.push_str(&format!(" Not signed in: {}.", hidden.join("; ")));
        }
        message
    }
}

/// Claude ids served by another backend's model instead of Anthropic (owner's rule).
const REROUTED_MODELS: &[(&str, &str)] = &[("claude-opus-4-8", "kimi-k3.1-pro")];

/// The model a request runs as: [`normalize_incoming_model`], then [`REROUTED_MODELS`].
pub fn route_model(model: &str) -> String {
    let normalized = normalize_incoming_model(model);
    REROUTED_MODELS
        .iter()
        .find(|(from, _)| normalized.eq_ignore_ascii_case(from))
        .map_or(normalized, |(_, to)| (*to).to_string())
}

pub fn normalize_incoming_model(model: &str) -> String {
    let suffix = "[1m]";
    if model.len() >= suffix.len() && model.to_ascii_lowercase().ends_with(suffix) {
        return model[..model.len() - suffix.len()].to_string();
    }
    model.to_string()
}

pub fn is_anthropic_alias(model: &str) -> bool {
    ANTHROPIC_STYLE_ALIASES.contains(&model)
}

pub fn is_cursor_model(model: &str) -> bool {
    if CURSOR_LEGACY_MODELS.contains(&model) {
        return true;
    }

    CURSOR_PREFIXES
        .iter()
        .any(|prefix| model.starts_with(prefix))
}

struct PlaceholderProvider {
    name: &'static str,
    models: Vec<String>,
}

impl PlaceholderProvider {
    fn new(name: &str, models: Vec<String>) -> Self {
        let name = match name {
            "codex" => "codex",
            "kimi" => "kimi",
            "cursor" => "cursor",
            "cursor-cli" => "cursor-cli",
            "gemini" => "gemini",
            "grok" => "grok",
            _ => "codex",
        };
        Self { name, models }
    }
}

#[async_trait]
impl Provider for PlaceholderProvider {
    fn name(&self) -> &'static str {
        self.name
    }

    fn supported_models(&self) -> Vec<String> {
        self.models.clone()
    }

    fn cli(&self) -> &'static dyn CliHandlers {
        match self.name {
            "codex" => &CODEX_CLI,
            "kimi" => &KIMI_CLI,
            "cursor" => &CURSOR_CLI,
            "cursor-cli" => &CURSOR_CLI_PLACEHOLDER,
            "gemini" => &GEMINI_PLACEHOLDER,
            "grok" => &GROK_CLI,
            _ => &CODEX_CLI,
        }
    }

    async fn handle_messages(&self, _body: MessagesRequest, ctx: RequestContext) -> Response {
        placeholder_provider_response("messages", &ctx.provider)
    }

    async fn handle_count_tokens(&self, _body: MessagesRequest, ctx: RequestContext) -> Response {
        placeholder_provider_response("count_tokens", &ctx.provider)
    }
}

fn placeholder_provider_response(route: &str, provider: &str) -> Response {
    let _ = route;
    json_error(
        StatusCode::NOT_IMPLEMENTED,
        "unsupported_provider_error",
        format!("provider '{}' is not yet implemented", provider),
    )
}

#[derive(Clone, Copy)]
struct PlaceholderCli {
    provider: &'static str,
}

impl CliHandlers for PlaceholderCli {
    fn login(&self) -> Result<()> {
        Err(anyhow!("{}: browser login not supported", self.provider))
    }

    fn device(&self) -> Result<()> {
        Err(anyhow!("{}: device login not supported", self.provider))
    }

    fn status(&self) -> Result<()> {
        use serde_json::Value;
        let path = crate::paths::provider_auth_file(self.provider);
        let legacy = crate::paths::provider_legacy_auth_file(self.provider);
        if crate::auth::load_auth_file_with_legacy::<Value>(&path, &legacy).is_some() {
            Ok(())
        } else {
            Err(anyhow!("Not authenticated"))
        }
    }

    fn logout(&self) -> Result<()> {
        let path = crate::paths::provider_auth_file(self.provider);
        let legacy = crate::paths::provider_legacy_auth_file(self.provider);
        let _ = crate::auth::delete_auth_file(&path, &legacy);
        Ok(())
    }
}

const CODEX_CLI: PlaceholderCli = PlaceholderCli { provider: "codex" };
const KIMI_CLI: PlaceholderCli = PlaceholderCli { provider: "kimi" };
const CURSOR_CLI: PlaceholderCli = PlaceholderCli { provider: "cursor" };
const GROK_CLI: PlaceholderCli = PlaceholderCli { provider: "grok" };
const CURSOR_CLI_PLACEHOLDER: PlaceholderCli = PlaceholderCli {
    provider: "cursor-cli",
};
const GEMINI_PLACEHOLDER: PlaceholderCli = PlaceholderCli { provider: "gemini" };
fn build_cursor_models() -> Vec<String> {
    let mut out: Vec<String> = CURSOR_LEGACY_MODELS
        .iter()
        .map(|s| (*s).to_string())
        .collect();
    out.sort_unstable();
    out
}

#[cfg(test)]
mod tests {
    #[test]
    fn kimi_catalogue_keeps_hidden_identities_but_lists_current_endpoint_models() {
        let registry = Registry::with_default_alias();
        let catalog = registry.provider_catalog();
        let all = crate::providers::kimi::translate::model_allowlist::all_public_model_ids();
        assert_eq!(catalog["kimi"]["models"], serde_json::json!(all));
        assert_eq!(catalog["kimi"]["family"], "kimi");
        let base = &catalog["kimi"]["balance_base_url"];
        assert!(base.is_null() || base == "https://api.moonshot.ai/v1");
        for id in all {
            assert_eq!(
                registry.provider_for_model(id, None).unwrap().name(),
                "kimi"
            );
        }
        assert_eq!(registry.supported_models_for("kimi"), {
            let mut ids: Vec<_> =
                crate::providers::kimi::translate::model_allowlist::public_model_ids()
                    .into_iter()
                    .map(str::to_string)
                    .collect();
            ids.sort();
            ids
        });
        assert!(registry.provider_for_model("kimi-k2.6", None).is_none());
    }

    /// Every id we list must route somewhere.
    #[test]
    fn everything_listed_can_actually_be_routed() {
        let registry = Registry::with_default_alias();
        for (provider, offered) in registry.grouped_models_all() {
            for model in offered {
                assert!(
                    registry.provider_for_model(&model, None).is_some(),
                    "{provider} offers {model} but it routes nowhere"
                );
            }
        }
    }

    #[test]
    fn only_glm_ids_route_to_zai() {
        let registry = Registry::with_default_alias();
        for model in ["glm-4.6", "glm-5.4", "glm-4.6[1m]", "glm-9-unlisted"] {
            let provider = registry.provider_for_model(model, None).unwrap();
            assert_eq!(provider.name(), "zai", "{model}");
        }
        for model in [
            "opus",
            "claude-sonnet-5-5",
            "kimi-k3",
            "gpt-6-luna",
            "claude-opus-4-8",
        ] {
            let provider = registry.provider_for_model(model, None);
            assert_ne!(provider.map(|p| p.name()), Some("zai"), "{model}");
        }
    }

    #[test]
    fn gemini_thinking_ids_route_without_being_offered() {
        let registry = Registry::with_default_alias();
        let offered = registry.supported_models_for("gemini");
        assert!(!offered.iter().any(|m| m.contains("thinking")));
        for model in [
            "gemini-3-flash-thinking",
            "gemini-3-flash-thinking-advanced",
        ] {
            let provider = registry
                .provider_for_model(model, None)
                .unwrap_or_else(|| panic!("{model} should still route"));
            assert_eq!(provider.name(), "gemini");
        }
    }

    use super::*;

    #[test]
    fn normalize_model_trims_hint() {
        assert_eq!(normalize_incoming_model("gpt-5.4-fast[1m]"), "gpt-5.4-fast");
        assert_eq!(normalize_incoming_model("gpt-5.4-fast"), "gpt-5.4-fast");
    }

    #[test]
    fn alias_routes_to_configured_provider() {
        let registry = Registry::new(AliasProvider::Kimi);
        let p = registry.provider_for_model("haiku", None);
        assert!(p.is_some());
        assert_eq!(p.expect("provider").name(), "kimi");
    }

    #[test]
    fn opus_4_8_runs_as_kimi_k3_1_pro_whatever_the_alias_target() {
        for model in ["claude-opus-4-8", "claude-opus-4-8[1m]", "CLAUDE-OPUS-4-8"] {
            assert_eq!(route_model(model), "kimi-k3.1-pro", "{model}");
        }
        for alias in [
            AliasProvider::Anthropic,
            AliasProvider::Codex,
            AliasProvider::Kimi,
        ] {
            let registry = Registry::new(alias);
            for model in ["claude-opus-4-8", "claude-opus-4-8[1m]"] {
                let p = registry.provider_for_model(model, Some(&AliasProvider::Anthropic));
                assert_eq!(p.expect("provider").name(), "kimi", "{model}");
            }
        }
    }

    #[test]
    fn other_claude_ids_keep_their_own_model() {
        for model in [
            "claude-opus-5-5",
            "claude-opus-5",
            "claude-opus-4-7",
            "opus",
        ] {
            assert_eq!(route_model(model), model);
        }
        assert_eq!(route_model("claude-opus-5-5[1m]"), "claude-opus-5-5");
    }

    #[test]
    fn claude_5_aliases_route_to_configured_provider() {
        let registry = Registry::new(AliasProvider::Codex);
        for model in [
            "claude-sonnet-5",
            "claude-opus-5",
            "fable",
            "claude-fable-5",
        ] {
            let p = registry.provider_for_model(model, None);
            assert!(p.is_some(), "{model} should route to a provider");
            assert_eq!(p.expect("provider").name(), "codex");
        }
    }

    #[test]
    fn claude_models_route_to_anthropic_passthrough_by_default() {
        let registry = Registry::new(AliasProvider::Anthropic);
        for model in [
            "opus",
            "claude-opus-5-5",
            "claude-opus-4-7",
            "sonnet",
            "haiku",
            "claude-3-5-haiku-20241022",
        ] {
            let p = registry.provider_for_model(model, None);
            assert!(p.is_some(), "{model} should route");
            assert_eq!(p.expect("provider").name(), "anthropic", "{model}");
        }
    }

    #[test]
    fn explicit_codex_models_route_to_codex_while_default_is_anthropic() {
        let registry = Registry::new(AliasProvider::Anthropic);
        for model in [
            "gpt-5.6-terra",
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
            "gpt-6.1-sol-fast",
            "gpt-6.1-sol[1m]",
        ] {
            let provider = registry
                .provider_for_model(model, None)
                .unwrap_or_else(|| panic!("{model} should route"));
            assert_eq!(provider.name(), "codex", "{model}");
        }
    }

    #[test]
    fn session_affinity_cannot_hijack_claude_slot() {
        // Even if a prior codex request set Codex affinity, claude aliases stay on anthropic.
        let registry = Registry::new(AliasProvider::Anthropic);
        let p = registry.provider_for_model("claude-opus-5-5", Some(&AliasProvider::Codex));
        assert_eq!(p.expect("provider").name(), "anthropic");
    }

    #[test]
    fn cursor_prefix_routes() {
        let registry = Registry::new(AliasProvider::Codex);
        assert_eq!(
            registry
                .provider_for_model("cursor:gpt-5.5", None)
                .unwrap()
                .name(),
            "cursor"
        );
        assert_eq!(
            registry
                .provider_for_model("cursor-plan:gpt-5.5", None)
                .unwrap()
                .name(),
            "cursor"
        );
        assert_eq!(
            registry
                .provider_for_model("cursor-ask:gpt-5.5", None)
                .unwrap()
                .name(),
            "cursor"
        );
    }
}
