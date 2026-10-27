//! Quota failover between Claude accounts, Codex, Cursor and any other backend.
//!
//! The account proxy (`scripts/account_inject.py`) owns every Claude credential and
//! attaches the ordered account pool to inference requests in [`POOL_HEADER`], plus
//! the accounts file's `fallback` section in [`POLICY_HEADER`]. A request walks one
//! chain for its model family. Without a configured chain, a Claude model tries each
//! account in order, then the Codex equivalent, then Cursor, and a Codex model tries
//! Codex, then the Claude equivalent on each account, then Cursor. A 429 or
//! Claude-account 401 moves on before output (5xx too when enabled). Nothing is
//! replayed after the client saw a token.

use crate::anthropic::schema::MessagesRequest;
use crate::logging::Logger;
use crate::provider::{Provider, RequestContext};
use crate::registry::Registry;
use crate::request_identity::ConversationIdentity;
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::Response;
use serde::Deserialize;
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const POOL_HEADER: &str = "x-claude-codex-account-pool";
/// The accounts file's `fallback` section, attached next to the pool and stripped with it.
pub const POLICY_HEADER: &str = "x-claude-codex-fallback";
/// Response header naming the account or backend that served the request.
pub const SERVED_BY_HEADER: &str = "x-claude-codex-served-by";
const DEFAULT_COOLDOWN: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, Deserialize)]
pub struct PoolAccount {
    pub alias: String,
    pub authorization: String,
    #[serde(default)]
    pub primary: bool,
}

/// When a failed attempt moves on to the next target.
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Triggers {
    pub rate_limit: bool,
    pub auth: bool,
    pub server_error: bool,
}

impl Default for Triggers {
    fn default() -> Self {
        Self {
            rate_limit: true,
            auth: true,
            server_error: false,
        }
    }
}

/// Per family (`claude`, `codex`, or a provider name such as `gemini`), the ordered
/// targets: `accounts` (each Claude account), `codex` / `cursor` (the same tier on
/// that backend), the family's own name (the requested model untouched), or an
/// exact model id (a `claude-*` id runs on each account).
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Policy {
    pub chains: HashMap<String, Vec<String>>,
    pub on: Triggers,
}

pub struct Pool {
    pub accounts: Vec<PoolAccount>,
    pub policy: Policy,
}

/// Equivalent models per tier. `keys` match both Claude and Codex model names.
struct Tier {
    keys: &'static [&'static str],
    claude: &'static str,
    codex: &'static str,
    cursor: &'static str,
}

const TIERS: &[Tier] = &[
    Tier {
        keys: &["opus", "sol"],
        claude: "claude-opus-5-5",
        codex: "gpt-6.1-sol",
        cursor: "cursor:claude-opus-5",
    },
    Tier {
        keys: &["fable", "astra"],
        claude: "claude-fable-5-1",
        codex: "gpt-6-astra",
        cursor: "cursor:claude-fable-5",
    },
    Tier {
        keys: &["sonnet", "haiku", "luna"],
        claude: "claude-sonnet-5-5",
        codex: "gpt-6-luna",
        cursor: "cursor:claude-sonnet-5",
    },
];

fn tier(model: &str) -> Option<&'static Tier> {
    let model = model.to_ascii_lowercase();
    TIERS
        .iter()
        .find(|tier| tier.keys.iter().any(|key| model.contains(key)))
}

#[derive(Debug, PartialEq)]
enum Step {
    /// Requested model on its own backend, untouched.
    Original,
    /// A Claude account, with the Claude model to ask for.
    Account(usize, String),
    /// Another backend's model.
    Substitute(String),
}

const DEFAULT_CLAUDE: &[&str] = &["accounts", "codex", "cursor"];
const DEFAULT_CODEX: &[&str] = &["codex", "accounts", "cursor"];

/// The ordered chain for a request first routed to `provider`.
fn chain(provider: &str, model: &str, accounts: usize, policy: &Policy) -> Vec<Step> {
    let family = if provider == "anthropic" {
        "claude"
    } else {
        provider
    };
    let targets: Vec<&str> = match policy.chains.get(family) {
        Some(targets) => targets.iter().map(String::as_str).collect(),
        None if family == "claude" => DEFAULT_CLAUDE.to_vec(),
        None if family == "codex" => DEFAULT_CODEX.to_vec(),
        None => Vec::new(),
    };
    let tier = tier(model);
    let mut steps = Vec::new();
    for target in targets {
        let substitute = match target {
            "accounts" if family == "claude" => Some(model.to_string()),
            "accounts" => tier.map(|t| t.claude.to_string()),
            own if own == family => {
                steps.push(Step::Original);
                continue;
            }
            "codex" => tier.map(|t| t.codex.to_string()),
            "cursor" => tier.map(|t| t.cursor.to_string()),
            id => Some(id.to_string()),
        };
        let Some(substitute) = substitute else {
            continue;
        };
        if target == "accounts" || substitute.starts_with("claude-") {
            steps.extend((0..accounts).map(|index| Step::Account(index, substitute.clone())));
        } else {
            steps.push(Step::Substitute(substitute));
        }
    }
    steps
}

/// Whether the chain does anything beyond serving the request as routed.
pub fn handles(provider: &str, model: &str, policy: &Policy) -> bool {
    chain(provider, model, 1, policy)
        .iter()
        .any(|step| *step != Step::Original)
}

/// Cooldown expiry per (account or provider, model), as unix seconds.
#[derive(Default)]
pub(crate) struct Cooldowns(Mutex<HashMap<(String, String), u64>>);

/// Remove the pool and policy headers so they can never be relayed or recorded, and
/// parse them. An unreadable policy keeps the default chains.
pub fn take_pool(headers: &mut HeaderMap) -> Option<Pool> {
    let policy = headers
        .remove(POLICY_HEADER)
        .and_then(|raw| serde_json::from_slice::<Policy>(raw.as_bytes()).ok())
        .unwrap_or_default();
    let raw = headers.remove(POOL_HEADER)?;
    let accounts = serde_json::from_slice::<Vec<PoolAccount>>(raw.as_bytes()).ok()?;
    Some(Pool { accounts, policy })
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

impl Cooldowns {
    fn cooling(&self, owner: &str, model: &str) -> bool {
        let key = (owner.to_string(), model.to_string());
        let mut cooldowns = self.0.lock().unwrap_or_else(|e| e.into_inner());
        match cooldowns.get(&key) {
            Some(until) if *until > now() => true,
            Some(_) => {
                cooldowns.remove(&key);
                false
            }
            None => false,
        }
    }

    fn record_rejection(&self, owner: &str, model: &str, headers: &HeaderMap) {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).insert(
            (owner.to_string(), model.to_string()),
            cooldown_until(headers),
        );
    }
}

/// Anthropic sends the exhausted window's reset epoch; others may send `retry-after`.
fn cooldown_until(headers: &HeaderMap) -> u64 {
    let header = |name: &str| {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.trim().parse::<u64>().ok())
    };
    let rejected = headers
        .get("anthropic-ratelimit-unified-status")
        .and_then(|v| v.to_str().ok())
        == Some("rejected");
    if rejected && let Some(reset) = header("anthropic-ratelimit-unified-reset") {
        return reset;
    }
    now() + header("retry-after").unwrap_or(DEFAULT_COOLDOWN.as_secs())
}

fn is_thread_continue(body: &MessagesRequest) -> bool {
    body.extra
        .get("thread")
        .and_then(|thread| thread.get("type"))
        == Some(&json!("continue"))
}

/// The passthrough relays raw bytes, so a Claude substitute rewrites `model` there.
fn with_model(raw: &axum::body::Bytes, model: &str) -> Option<axum::body::Bytes> {
    let mut value: Value = serde_json::from_slice(raw).ok()?;
    value.as_object_mut()?.insert("model".into(), json!(model));
    serde_json::to_vec(&value).ok().map(Into::into)
}

fn note(log: &Logger, req_id: &str, requested: &str, served: &str) {
    log.info(
        "quota fallback",
        Some(Map::from_iter([
            ("reqId".to_string(), json!(req_id)),
            ("requested".to_string(), json!(requested)),
            ("servedBy".to_string(), json!(served)),
        ])),
    );
}

/// Serve a request from the first account or backend in its tier with quota left.
#[allow(clippy::too_many_arguments)]
pub async fn dispatch(
    registry: &Registry,
    provider: Arc<dyn Provider>,
    body: MessagesRequest,
    ctx: RequestContext,
    identity: Option<ConversationIdentity>,
    Pool {
        accounts: pool,
        policy,
    }: Pool,
    log: &Logger,
    thread_unsupported: fn() -> Response,
) -> Response {
    let requested = body.model.clone().unwrap_or_default();
    let threaded = is_thread_continue(&body);
    let anthropic = registry.provider_for_model(TIERS[0].claude, None);
    let mut rejection: Option<Response> = None;

    let steps = chain(provider.name(), &requested, pool.len(), &policy);
    let last = steps.len().saturating_sub(1);
    for (position, step) in steps.into_iter().enumerate() {
        let (owner, model, target, attempt_body) = match &step {
            Step::Original => (
                provider.name().to_string(),
                requested.clone(),
                provider.clone(),
                body.clone(),
            ),
            Step::Account(index, model) => {
                let Some(anthropic) = anthropic.clone() else {
                    continue;
                };
                let mut substitute = body.clone();
                substitute.model = Some(model.clone());
                (
                    pool[*index].alias.clone(),
                    model.clone(),
                    anthropic,
                    substitute,
                )
            }
            Step::Substitute(model) => {
                let Some(target) = registry.provider_for_model(model, None) else {
                    continue;
                };
                if !target.availability().is_ready() {
                    continue;
                }
                let mut substitute = body.clone();
                substitute.model = Some(model.clone());
                substitute.bypass_provider_model_override = true;
                (target.name().to_string(), model.clone(), target, substitute)
            }
        };
        if registry.cooldowns.cooling(&owner, &model) {
            continue;
        }
        let first_choice = matches!(step, Step::Original)
            || matches!(step, Step::Account(index, _) if provider.name() == "anthropic" && pool[index].primary);
        // Thread state lives with the account that started it. Anything else gets
        // the full conversation, which Claude Code resends on this refusal.
        if threaded && !first_choice {
            return thread_unsupported();
        }
        let mut attempt = ctx.clone();
        attempt.provider = target.name().to_string();
        if let Step::Account(index, model) = &step {
            let Ok(authorization) = HeaderValue::from_str(&pool[*index].authorization) else {
                continue;
            };
            if let Some(passthrough) = attempt.passthrough.as_mut() {
                passthrough.headers.insert("authorization", authorization);
                if *model != requested {
                    match with_model(&passthrough.raw_body, model) {
                        Some(raw) => passthrough.raw_body = raw,
                        None => continue,
                    }
                }
            }
        }
        // Only the chain's final option may wait out its provider's own retries.
        let response = crate::retry::fail_fast(
            position < last,
            target.handle_messages_with_conversation_identity(
                attempt_body,
                attempt,
                identity.clone(),
            ),
        )
        .await;
        let status = response.status();
        let quota = status == StatusCode::TOO_MANY_REQUESTS && policy.on.rate_limit;
        let credential_rejected = matches!(step, Step::Account(_, _))
            && status == StatusCode::UNAUTHORIZED
            && policy.on.auth;
        let server_error = status.is_server_error() && policy.on.server_error;
        if !quota && !credential_rejected && !server_error {
            let served = format!("{owner}/{model}");
            if !first_choice && response.status().is_success() {
                note(log, &ctx.req_id, &requested, &served);
            }
            let mut response = response;
            // The --serve proxy's traffic recorder reads which account answered.
            if let Ok(value) = HeaderValue::from_str(&served) {
                response.headers_mut().insert(SERVED_BY_HEADER, value);
            }
            return response;
        }
        // A server error is transient: try the next target now, the same one next time.
        if !server_error {
            registry
                .cooldowns
                .record_rejection(&owner, &model, response.headers());
        }
        rejection.get_or_insert(response);
    }

    rejection.unwrap_or_else(|| {
        crate::anthropic::json_error(
            StatusCode::TOO_MANY_REQUESTS,
            "rate_limit_error",
            "Every Claude account and fallback provider is rate limited",
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_chain(provider: &str, model: &str, accounts: usize) -> Vec<Step> {
        chain(provider, model, accounts, &Policy::default())
    }

    fn sub(model: &str) -> Step {
        Step::Substitute(model.into())
    }

    #[test]
    fn claude_requests_try_accounts_then_codex_then_cursor() {
        assert_eq!(
            default_chain("anthropic", "claude-opus-5-5", 2),
            [
                Step::Account(0, "claude-opus-5-5".into()),
                Step::Account(1, "claude-opus-5-5".into()),
                sub("gpt-6.1-sol"),
                sub("cursor:claude-opus-5"),
            ]
        );
        assert_eq!(
            default_chain("anthropic", "claude-haiku-5-5", 1)[1],
            sub("gpt-6-luna")
        );
        assert_eq!(
            default_chain("anthropic", "claude-fable-5-1", 1)[1],
            sub("gpt-6-astra")
        );
    }

    #[test]
    fn codex_requests_try_codex_then_claude_accounts_then_cursor() {
        assert_eq!(
            default_chain("codex", "gpt-6-astra", 1),
            [
                Step::Original,
                Step::Account(0, "claude-fable-5-1".into()),
                sub("cursor:claude-fable-5"),
            ]
        );
        assert_eq!(
            default_chain("codex", "gpt-6.1-sol-fast", 1)[1],
            Step::Account(0, "claude-opus-5-5".into())
        );
        assert_eq!(
            default_chain("codex", "gpt-6-luna", 1)[1],
            Step::Account(0, "claude-sonnet-5-5".into())
        );
        assert!(default_chain("cursor", "cursor:claude-opus-5", 1).is_empty());
        // An untiered Claude model still walks the accounts, just no other vendor.
        assert_eq!(
            default_chain("anthropic", "claude-unknown", 1),
            [Step::Account(0, "claude-unknown".into())]
        );
    }

    #[test]
    fn configured_chains_replace_the_default_per_family() {
        let policy: Policy = serde_json::from_value(json!({"chains": {
            "claude": ["codex", "accounts", "gemini-3-pro"],
            "gemini": ["gemini", "claude-sonnet-5-5", "cursor-cli:auto"],
        }}))
        .unwrap();
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &policy),
            [
                sub("gpt-6.1-sol"),
                Step::Account(0, "claude-opus-5-5".into()),
                sub("gemini-3-pro"),
            ]
        );
        assert_eq!(
            chain("gemini", "gemini-3-flash", 2, &policy),
            [
                Step::Original,
                Step::Account(0, "claude-sonnet-5-5".into()),
                Step::Account(1, "claude-sonnet-5-5".into()),
                sub("cursor-cli:auto"),
            ]
        );
        assert!(handles("gemini", "gemini-3-flash", &policy));
        // Unconfigured families keep today's chain; tier keywords need a tier.
        assert_eq!(
            chain("codex", "gpt-6-astra", 1, &policy),
            default_chain("codex", "gpt-6-astra", 1)
        );
        assert!(!handles("gemini", "gemini-3-flash", &Policy::default()));
        assert!(!handles("codex", "gpt-unknown", &Policy::default()));
        assert!(policy.on.rate_limit && policy.on.auth && !policy.on.server_error);
    }

    #[test]
    fn substitute_model_is_rewritten_in_the_raw_body() {
        let raw = axum::body::Bytes::from_static(br#"{"model":"gpt-6-luna","max_tokens":1}"#);
        let rewritten: Value =
            serde_json::from_slice(&with_model(&raw, "claude-sonnet-5-5").unwrap()).unwrap();
        assert_eq!(rewritten["model"], "claude-sonnet-5-5");
        assert_eq!(rewritten["max_tokens"], 1);
    }

    #[tokio::test]
    async fn provider_retries_are_skipped_only_when_a_fallback_follows() {
        use crate::retry::{compute_backoff_delay, fail_fast};
        assert!(
            fail_fast(true, async { compute_backoff_delay(0, None) })
                .await
                .exceeds_budget
        );
        assert!(
            !fail_fast(false, async { compute_backoff_delay(0, None) })
                .await
                .exceeds_budget
        );
        assert!(!compute_backoff_delay(0, None).exceeds_budget);
    }

    #[test]
    fn pool_header_is_removed_even_when_invalid() {
        let mut headers = HeaderMap::new();
        headers.insert(POOL_HEADER, HeaderValue::from_static("not json"));
        headers.insert(POLICY_HEADER, HeaderValue::from_static("{bad"));
        assert!(take_pool(&mut headers).is_none());
        assert!(!headers.contains_key(POOL_HEADER));
        assert!(!headers.contains_key(POLICY_HEADER));
        headers.insert(
            POOL_HEADER,
            HeaderValue::from_static(r#"[{"alias":"a","authorization":"Bearer x"}]"#),
        );
        headers.insert(
            POLICY_HEADER,
            HeaderValue::from_static(r#"{"on":{"server_error":true},"threshold":90}"#),
        );
        let pool = take_pool(&mut headers).unwrap();
        assert_eq!(pool.accounts[0].alias, "a");
        assert!(pool.policy.on.server_error && pool.policy.on.rate_limit);
        assert!(!headers.contains_key(POOL_HEADER));
        assert!(!headers.contains_key(POLICY_HEADER));
    }

    #[test]
    fn rejected_account_cools_until_reset_and_transient_uses_retry_after() {
        let cooldowns = Cooldowns::default();
        let mut headers = HeaderMap::new();
        headers.insert(
            "anthropic-ratelimit-unified-status",
            HeaderValue::from_static("rejected"),
        );
        let reset = now() + 3600;
        headers.insert(
            "anthropic-ratelimit-unified-reset",
            HeaderValue::from_str(&reset.to_string()).unwrap(),
        );
        assert_eq!(cooldown_until(&headers), reset);
        cooldowns.record_rejection("acct", "claude-opus-5-5", &headers);
        assert!(cooldowns.cooling("acct", "claude-opus-5-5"));
        assert!(!cooldowns.cooling("acct", "claude-sonnet-5-5"));
        let mut transient = HeaderMap::new();
        transient.insert("retry-after", HeaderValue::from_static("5"));
        assert!(cooldown_until(&transient) <= now() + 5);
        assert!(cooldown_until(&HeaderMap::new()) >= now() + 59);
    }
}
