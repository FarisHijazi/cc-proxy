//! Quota failover between Claude accounts, Codex, Cursor and any other backend.
//!
//! The account proxy (`scripts/account_inject.py`) owns every Claude credential and
//! attaches the ordered account pool to inference requests in [`POOL_HEADER`], plus
//! the accounts file's `fallback` section in [`POLICY_HEADER`]. A request walks one
//! chain for its model family. Without a configured chain, a Claude model tries each
//! account in order, then the Codex equivalent, and a Codex model tries Codex, then
//! the Claude equivalent on each account. Cursor is never a default step; it runs
//! only when a configured chain names it. A 429 or
//! Claude-account 401 moves on before output, as does an account answering 404 for
//! a model it does not offer (5xx too when enabled). Nothing is
//! replayed after the client saw a token. Token counts walk only the same model
//! across the accounts, with the same triggers and no cooldowns. A conversation whose
//! turn completed on a fallback keeps trying that target first until its requested
//! model changes, so a recovered earlier target never switches it back mid-task.
//! A pool member may be a Console API key instead of a login: it is sent as `x-api-key`,
//! and a reply saying its credit or spend limit ran out moves on and drops the key, for
//! every model, until the 1st of next month (UTC). Cooldowns live in memory only.

use crate::anthropic::schema::MessagesRequest;
use crate::logging::Logger;
use crate::provider::{Provider, RequestContext};
use crate::registry::Registry;
use crate::request_identity::ConversationIdentity;
use axum::body::Body;
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::Response;
use http_body_util::{BodyExt, StreamBody};
use hyper::body::Body as _;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const POOL_HEADER: &str = "x-claude-codex-account-pool";
/// The accounts file's `fallback` section, attached next to the pool and stripped with it.
pub const POLICY_HEADER: &str = "x-claude-codex-fallback";
/// Response header naming the account or backend that served the request.
pub const SERVED_BY_HEADER: &str = "x-claude-codex-served-by";
const DEFAULT_COOLDOWN: Duration = Duration::from_secs(60);
const ROUTE_IDLE: Duration = Duration::from_secs(24 * 60 * 60);
const MAX_ROUTES: usize = 10_000;

#[derive(Clone, Deserialize)]
pub struct PoolAccount {
    pub alias: String,
    #[serde(default)]
    pub authorization: String,
    /// A Console API key (`"kind": "api_key"`), sent as `x-api-key` in place of a login.
    #[serde(default)]
    pub api_key: Option<String>,
    /// USD left of a key's monthly budget (`account_inject` reads the spend); a request whose
    /// estimate does not fit never reaches the key.
    #[serde(default)]
    pub budget_usd: Option<f64>,
    #[serde(default)]
    pub primary: bool,
}

impl std::fmt::Debug for PoolAccount {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PoolAccount")
            .field("alias", &self.alias)
            .field("api_key", &self.api_key.is_some())
            .field("primary", &self.primary)
            .finish_non_exhaustive()
    }
}

impl PoolAccount {
    /// Put this member's credential on an attempt; false when it is not a valid header value.
    fn sign(&self, headers: &mut HeaderMap) -> bool {
        let (name, value) = match &self.api_key {
            Some(key) => ("x-api-key", key.as_str()),
            None => ("authorization", self.authorization.as_str()),
        };
        let Ok(mut value) = HeaderValue::from_str(value) else {
            return false;
        };
        value.set_sensitive(true);
        if self.api_key.is_some() {
            // Measured: a key accepts Claude Code's request as is, OAuth beta flags included.
            headers.remove("authorization");
            // Its refusals must stay readable: the router never decompresses, and Anthropic
            // sent the credit error brotli-compressed (2026-11-23), so it reached the client.
            headers.insert("accept-encoding", HeaderValue::from_static("identity"));
        }
        headers.insert(name, value);
        true
    }
}

/// When a failed attempt moves on to the next target.
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Triggers {
    pub rate_limit: bool,
    pub auth: bool,
    pub server_error: bool,
    /// A Claude account answering 404 for the model (another plan may offer it).
    pub model_unavailable: bool,
}

impl Default for Triggers {
    fn default() -> Self {
        Self {
            rate_limit: true,
            auth: true,
            server_error: false,
            model_unavailable: true,
        }
    }
}

/// Per family (`claude`, `codex`, or a provider name such as `gemini`), the ordered
/// targets: `accounts` (each Claude account), a provider name (the same tier on that
/// backend, skipped when the tier has no model there), the family's own name (the
/// requested model untouched), or an exact model id (a `claude-*` id runs on each
/// account). [`GLOBAL`] is the order every family without its own chain inherits.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Policy {
    pub chains: HashMap<String, Vec<String>>,
    pub on: Triggers,
    /// Replaces every built-in equivalence group when non-empty.
    #[serde(default, deserialize_with = "deserialize_tiers")]
    pub tiers: Vec<Tier>,
}

fn deserialize_tiers<'de, D>(deserializer: D) -> Result<Vec<Tier>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = Value::deserialize(deserializer)?;
    Ok(value
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|tier| serde_json::from_value(tier.clone()).ok())
        .collect())
}

pub struct Pool {
    pub accounts: Vec<PoolAccount>,
    pub policy: Policy,
}

/// One equivalence group: a request whose model matches `match_keys` may run as
/// the group's model on another provider (`anthropic`, `codex`, `cursor`).
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Tier {
    pub name: String,
    #[serde(rename = "match")]
    pub match_keys: Vec<String>,
    pub models: HashMap<String, String>,
}

fn builtin(name: &str, keys: &[&str], models: [(&str, &str); 4]) -> Tier {
    Tier {
        name: name.into(),
        match_keys: keys.iter().map(|key| key.to_string()).collect(),
        models: models
            .into_iter()
            .map(|(provider, id)| (provider.to_string(), id.to_string()))
            .collect(),
    }
}

fn default_tiers() -> &'static [Tier] {
    static TIERS: OnceLock<Vec<Tier>> = OnceLock::new();
    TIERS.get_or_init(|| {
        vec![
            builtin(
                "opus-sol",
                &[
                    "opus",
                    "sol",
                    "kimi-k3.1-pro-highspeed",
                    "k3.1-pro-highspeed",
                ],
                [
                    ("anthropic", "claude-opus-5-5"),
                    ("codex", "gpt-6.1-sol"),
                    ("cursor", "cursor:claude-opus-5"),
                    ("kimi", "kimi-k3.1-pro-highspeed"),
                ],
            ),
            builtin(
                "fable-astra",
                &["fable", "astra", "kimi-k3.1-pro", "k3.1-pro"],
                [
                    ("anthropic", "claude-fable-5-1"),
                    ("codex", "gpt-6-astra"),
                    ("cursor", "cursor:claude-fable-5"),
                    ("kimi", "kimi-k3.1-pro"),
                ],
            ),
            builtin(
                "sonnet-luna",
                &[
                    "sonnet",
                    "haiku",
                    "luna",
                    "kimi-for-coding",
                    "kimi-for-coding-highspeed",
                    "kimi-k3",
                    "k3",
                    "kimi-k3-256k",
                    "k3-256k",
                    "glm-",
                ],
                [
                    ("anthropic", "claude-sonnet-5-5"),
                    ("codex", "gpt-6-luna"),
                    ("cursor", "cursor:claude-sonnet-5"),
                    ("kimi", "kimi-k3"),
                ],
            ),
        ]
    })
}

impl Policy {
    fn tiers(&self) -> &[Tier] {
        if self.tiers.is_empty() {
            default_tiers()
        } else {
            &self.tiers
        }
    }

    /// The group serving `model`: an exact id or key match anywhere beats a family
    /// key contained in the name. Exact means a match key equal to the whole model id.
    fn tier(&self, model: &str) -> Option<&Tier> {
        let model = normalize(model);
        let tiers = self.tiers();
        tiers
            .iter()
            .find(|tier| tier.match_keys.iter().any(|key| normalize(key) == model))
            .or_else(|| {
                tiers.iter().find(|tier| {
                    tier.match_keys
                        .iter()
                        .any(|key| model.contains(&key.to_ascii_lowercase()))
                })
            })
    }
}

fn normalize(model: &str) -> String {
    let model = model.to_ascii_lowercase();
    model.strip_suffix("[1m]").unwrap_or(&model).to_string()
}

/// The router's own defaults, for dashboards to show and edit from.
pub fn defaults() -> Value {
    json!({
        "chains": {
            "claude": DEFAULT_CLAUDE,
            "codex": DEFAULT_CODEX,
            "kimi": DEFAULT_KIMI,
            "zai": DEFAULT_ZAI,
        },
        "tiers": default_tiers(),
    })
}

#[derive(Debug, Clone, PartialEq)]
enum Step {
    /// Requested model on its own backend, untouched.
    Original,
    /// A Claude account, with the Claude model to ask for.
    Account(usize, String),
    /// Another backend's model.
    Substitute(String),
}

/// The chain key every family without its own chain uses, with its own provider first.
pub const GLOBAL: &str = "*";
const DEFAULT_CLAUDE: &[&str] = &["accounts", "codex"];
const DEFAULT_CODEX: &[&str] = &["codex", "accounts"];
const DEFAULT_KIMI: &[&str] = &["kimi", "accounts", "codex"];
const DEFAULT_ZAI: &[&str] = &["zai", "accounts", "codex"];

/// Built-in tier providers retain their provider-target meaning, as do other backend
/// names that are not routable model ids. `cursor-cli` remains a literal model.
pub fn provider_targets(registry: &Registry) -> Vec<String> {
    registry
        .list_provider_names()
        .into_iter()
        .filter(|name| {
            default_tiers().iter().any(|t| t.models.contains_key(name))
                || registry.provider_for_model(name, None).is_none()
        })
        .collect()
}

/// The ordered chain for a request first routed to `provider`. `providers` are the
/// backend names that mean tier targets, skipped when the tier has no cell.
fn chain(
    provider: &str,
    model: &str,
    accounts: usize,
    policy: &Policy,
    providers: &[String],
) -> Vec<Step> {
    let family = if provider == "anthropic" {
        "claude"
    } else {
        provider
    };
    let targets: Vec<&str> = match (policy.chains.get(family), policy.chains.get(GLOBAL)) {
        (Some(targets), _) => targets.iter().map(String::as_str).collect(),
        (None, Some(global)) => own_first(family, global, accounts),
        (None, None) if family == "claude" => DEFAULT_CLAUDE.to_vec(),
        (None, None) if family == "codex" => DEFAULT_CODEX.to_vec(),
        (None, None) if family == "kimi" => DEFAULT_KIMI.to_vec(),
        (None, None) if family == "zai" => DEFAULT_ZAI.to_vec(),
        (None, None) => Vec::new(),
    };
    let tier = policy.tier(model);
    let mut steps = Vec::new();
    let mut next_account = 0;
    for target in targets {
        if let Some(count) = account_count(target, accounts) {
            let model = if family == "claude" {
                Some(model.to_string())
            } else {
                tier.and_then(|t| t.models.get("anthropic").cloned())
            };
            let end = (next_account + count).min(accounts);
            if let Some(model) = model {
                steps.extend((next_account..end).map(|index| Step::Account(index, model.clone())));
            }
            next_account = end;
            continue;
        }
        let substitute = match target {
            own if own == family => {
                steps.push(Step::Original);
                continue;
            }
            name if tier.is_some_and(|t| t.models.contains_key(name)) => {
                tier.and_then(|t| t.models.get(name).cloned())
            }
            name if providers.iter().any(|known| known == name) => None,
            id => Some(id.to_string()),
        };
        let Some(substitute) = substitute else {
            continue;
        };
        if substitute.starts_with("claude-") {
            steps.extend((0..accounts).map(|index| Step::Account(index, substitute.clone())));
        } else {
            steps.push(Step::Substitute(substitute));
        }
    }
    steps
}

/// The global order with the request's own target moved to the front (for Claude, its
/// first run of accounts), or added there when the order leaves it out or off.
fn own_first<'a>(family: &'a str, global: &'a [String], accounts: usize) -> Vec<&'a str> {
    let is_own = |target: &str| {
        target == family || (family == "claude" && account_count(target, accounts).is_some())
    };
    let mut targets: Vec<&str> = global.iter().map(String::as_str).collect();
    let own = match targets.iter().position(|target| is_own(target)) {
        Some(index) => targets.remove(index),
        None if family == "claude" => "accounts",
        None => family,
    };
    targets.insert(0, own);
    targets
}

/// `accounts` takes every Claude account not yet in the chain; `accounts:N` the next N in pool order,
/// so another backend can sit between accounts while the session's own account stays first.
fn account_count(target: &str, accounts: usize) -> Option<usize> {
    match target.strip_prefix("accounts") {
        Some("") => Some(accounts),
        Some(rest) => rest.strip_prefix(':')?.parse().ok(),
        None => None,
    }
}

/// Whether the chain does anything beyond serving the request as routed.
pub fn handles(provider: &str, model: &str, policy: &Policy, providers: &[String]) -> bool {
    chain(provider, model, 1, policy, providers)
        .iter()
        .any(|step| *step != Step::Original)
}

/// Cooldown expiry per (account or provider, model), as unix seconds.
#[derive(Default)]
pub(crate) struct Cooldowns(Mutex<HashMap<(String, String), u64>>);
/// The cooldown "model" of a credential that is unusable for every model.
const EVERY_MODEL: &str = "*";

#[derive(Debug, Clone, PartialEq)]
enum Served {
    Original,
    Account(String, String),
    Substitute(String),
}

impl Served {
    fn of(step: &Step, pool: &[PoolAccount]) -> Self {
        match step {
            Step::Original => Self::Original,
            Step::Account(index, model) => Self::Account(pool[*index].alias.clone(), model.clone()),
            Step::Substitute(model) => Self::Substitute(model.clone()),
        }
    }
}

struct Route {
    requested: String,
    served: Option<Served>,
    seen: u64,
    generation: u64,
}

type RouteKey = ConversationIdentity;

#[derive(Default)]
struct RouteStore {
    entries: HashMap<RouteKey, Route>,
    generation: u64,
}

#[derive(Default, Clone)]
pub(crate) struct Routes(Arc<Mutex<RouteStore>>);

pub struct RouteAttempt {
    key: RouteKey,
    pinned: Option<Served>,
    generation: u64,
}

impl Routes {
    pub(crate) fn begin(
        &self,
        identity: Option<&ConversationIdentity>,
        pool: Option<&Pool>,
        requested: &str,
    ) -> Option<RouteAttempt> {
        pool?;
        let key = identity?.clone();
        let mut store = self.0.lock().unwrap_or_else(|e| e.into_inner());
        store.generation += 1;
        let generation = store.generation;
        let routes = &mut store.entries;
        let now = now();
        let fresh = |route: &Route| now.saturating_sub(route.seen) < ROUTE_IDLE.as_secs();
        if routes.len() >= MAX_ROUTES && !routes.contains_key(&key) {
            routes.retain(|_, route| fresh(route));
            let oldest = routes
                .iter()
                .min_by_key(|(_, route)| route.seen)
                .map(|(key, _)| key.clone());
            if let Some(oldest) = oldest.filter(|_| routes.len() >= MAX_ROUTES) {
                routes.remove(&oldest);
            }
        }
        let route = routes.entry(key.clone()).or_insert_with(|| Route {
            requested: requested.to_string(),
            served: None,
            seen: now,
            generation: 0,
        });
        if route.requested != requested || !fresh(route) {
            route.requested = requested.to_string();
            route.served = None;
        }
        route.seen = now;
        route.generation = generation;
        Some(RouteAttempt {
            key,
            pinned: route.served.clone(),
            generation,
        })
    }

    fn complete(&self, key: &RouteKey, generation: u64, served: Served) {
        let mut store = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(route) = store
            .entries
            .get_mut(key)
            .filter(|route| route.generation == generation)
        {
            route.served = Some(served);
        }
    }
}

enum Completion {
    Stream {
        decoder: crate::openai_compat::stream::SseDecoder,
        stop_reason: Option<String>,
        stopped: bool,
    },
    Message(Vec<u8>),
}

fn completes(stop_reason: Option<&str>) -> bool {
    stop_reason.is_some_and(|reason| reason != "refusal")
}

impl Completion {
    fn new(headers: &HeaderMap) -> Self {
        let streamed = headers
            .get(axum::http::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value.starts_with("text/event-stream"));
        if streamed {
            Self::Stream {
                decoder: Default::default(),
                stop_reason: None,
                stopped: false,
            }
        } else {
            Self::Message(Vec::new())
        }
    }

    fn push(&mut self, data: &[u8]) -> bool {
        match self {
            Self::Stream {
                decoder,
                stop_reason,
                stopped,
            } => {
                let Ok(events) = decoder.push(data) else {
                    return false;
                };
                for event in events {
                    match event.data["type"].as_str() {
                        Some("error") => return false,
                        Some("message_delta") => {
                            *stop_reason = event.data["delta"]["stop_reason"]
                                .as_str()
                                .map(str::to_string)
                        }
                        Some("message_stop") => *stopped = true,
                        _ => {}
                    }
                }
                true
            }
            Self::Message(body) => {
                body.extend_from_slice(data);
                body.len() <= crate::openai_compat::MAX_SSE_EVENT_BYTES
            }
        }
    }

    fn finished(&self) -> bool {
        match self {
            Self::Stream {
                decoder,
                stop_reason,
                stopped,
            } => *stopped && completes(stop_reason.as_deref()) && decoder.finish().is_ok(),
            Self::Message(body) => serde_json::from_slice::<Value>(body)
                .is_ok_and(|message| completes(message["stop_reason"].as_str())),
        }
    }
}

fn remember_on_completion(response: Response, route: (Routes, RouteKey, u64, Served)) -> Response {
    let completion = Completion::new(response.headers());
    let (parts, body) = response.into_parts();
    let state = (body, completion, Some(route));
    let stream =
        futures_util::stream::unfold(state, |(mut body, mut completion, mut route)| async move {
            match body.frame().await {
                Some(Ok(frame)) => {
                    if let (Some(data), Some(_)) = (frame.data_ref(), route.as_ref())
                        && !completion.push(data)
                    {
                        route = None;
                    }
                    // HTTP/1 may stop polling after Content-Length bytes, without a None poll.
                    if (body.is_end_stream() || body.size_hint().exact() == Some(0))
                        && let Some((routes, key, generation, served)) = route.take()
                        && completion.finished()
                    {
                        routes.complete(&key, generation, served);
                    }
                    Some((Ok(frame), (body, completion, route)))
                }
                Some(Err(err)) => Some((Err(err), (body, completion, None))),
                None => {
                    if let Some((routes, key, generation, served)) = route
                        && completion.finished()
                    {
                        routes.complete(&key, generation, served);
                    }
                    None
                }
            }
        });
    Response::from_parts(parts, Body::new(StreamBody::new(stream)))
}

/// A cross-provider substitute keeps the caller's effort but replaces a thinking mode its
/// destination rejects: the built-in Claude tier models take adaptive thinking, not a
/// budget or `disabled`, and Kimi K3 cannot disable it. `disabled` becomes low effort
/// unless the caller chose one.
fn fit_thinking(request: &mut Map<String, Value>, destination: &str) {
    use crate::providers::kimi::translate::model_allowlist::{all_public_model_ids, is_k3};
    let adaptive_only = default_tiers().iter().any(|tier| {
        tier.models
            .get("anthropic")
            .is_some_and(|m| m == destination)
    });
    let k3 = all_public_model_ids().contains(&destination) && is_k3(destination);
    let mode = request
        .get("thinking")
        .and_then(|thinking| thinking.get("type"))
        .and_then(Value::as_str);
    match mode {
        Some("disabled") if adaptive_only || k3 => {
            let config = request.entry("output_config").or_insert_with(|| json!({}));
            if let Some(config) = config.as_object_mut() {
                config.entry("effort").or_insert_with(|| json!("low"));
            }
        }
        Some("enabled") if adaptive_only => {}
        _ => return,
    }
    request.insert("thinking".into(), json!({"type": "adaptive"}));
}

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
        let mut cooldowns = self.0.lock().unwrap_or_else(|e| e.into_inner());
        [model, EVERY_MODEL].into_iter().any(|model| {
            let key = (owner.to_string(), model.to_string());
            match cooldowns.get(&key) {
                Some(until) if *until > now() => true,
                Some(_) => {
                    cooldowns.remove(&key);
                    false
                }
                None => false,
            }
        })
    }

    fn exhaust(&self, owner: &str, until: u64) {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert((owner.to_string(), EVERY_MODEL.to_string()), until);
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

/// How a Console key refused a request.
#[derive(Debug, PartialEq)]
enum Refusal {
    /// Its credit or spend limit ran out: any `billing_error`, or a 400 "credit balance is too low"
    /// (recorded live) / "reached your specified (workspace) API usage limits".
    OutOfCredit,
    /// Not readable JSON (still encoded, say): may be the credit error, so another account answers.
    Unreadable,
    /// A real bad request, returned to the client.
    Other,
}

fn refusal(status: StatusCode, body: &[u8]) -> Refusal {
    let Ok(reply) = serde_json::from_slice::<Value>(body) else {
        return Refusal::Unreadable;
    };
    let error = &reply["error"];
    let message = error["message"].as_str().unwrap_or_default().to_lowercase();
    let spent = error["type"] == "billing_error"
        || status == StatusCode::BAD_REQUEST
            && error["type"] == "invalid_request_error"
            && (message.contains("credit balance is too low")
                || message.contains("reached your specified")
                    && message.contains("api usage limits"));
    if spent {
        Refusal::OutOfCredit
    } else {
        Refusal::Other
    }
}

/// Unix seconds of the 1st of the month after `now`, 00:00 UTC, when Console credit and spend limits reset.
fn next_month(now: u64) -> u64 {
    use time::{Date, Month, OffsetDateTime};
    let fallback = now + 31 * 24 * 60 * 60;
    let Ok(today) = OffsetDateTime::from_unix_timestamp(now as i64).map(|t| t.date()) else {
        return fallback;
    };
    let (year, month) = match today.month() {
        Month::December => (today.year() + 1, Month::January),
        month => (today.year(), month.next()),
    };
    Date::from_calendar_date(year, month, 1)
        .map(|day| day.midnight().assume_utc().unix_timestamp() as u64)
        .unwrap_or(fallback)
}

fn is_thread_continue(body: &MessagesRequest) -> bool {
    body.extra
        .get("thread")
        .and_then(|thread| thread.get("type"))
        == Some(&json!("continue"))
}

/// The passthrough relays raw bytes, so a Claude substitute rewrites `model` there,
/// and its thinking mode too when it stands in for another provider.
fn with_model(raw: &axum::body::Bytes, model: &str, cross: bool) -> Option<axum::body::Bytes> {
    let mut value: Value = serde_json::from_slice(raw).ok()?;
    let request = value.as_object_mut()?;
    request.insert("model".into(), json!(model));
    if cross {
        fit_thinking(request, model);
    }
    serde_json::to_vec(&value).ok().map(Into::into)
}

fn step_model<'a>(step: &'a Step, requested: &'a str) -> &'a str {
    match step {
        Step::Original => requested,
        Step::Account(_, model) | Step::Substitute(model) => model,
    }
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
/// A token count (`count_tokens`) tries only the requested model on each account:
/// counts are model-specific, and their limits must not cool messages down.
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
    count_tokens: bool,
    route: Option<RouteAttempt>,
) -> Response {
    let requested = body.model.clone().unwrap_or_default();
    let threaded = is_thread_continue(&body);
    let anthropic =
        registry.provider_for_model(default_tiers()[0].models["anthropic"].as_str(), None);
    let mut rejection: Option<Response> = None;
    let mut skipped_cooling = false;

    let mut steps = chain(
        provider.name(),
        &requested,
        pool.len(),
        &policy,
        &provider_targets(registry),
    );
    if count_tokens {
        steps.retain(|step| matches!(step, Step::Account(_, model) if *model == requested));
        if steps.is_empty() {
            return provider.handle_count_tokens(body, ctx).await;
        }
    }
    if let Some(index) = route
        .as_ref()
        .and_then(|route| route.pinned.as_ref())
        .and_then(|pinned| {
            steps
                .iter()
                .position(|step| &Served::of(step, &pool) == pinned)
        })
    {
        // A remembered account serving the requested model keeps the conversation there; a
        // remembered substitute model only leads the fallbacks, so the requested model is
        // always tried first and a recovered one answers again.
        let step = steps.remove(index);
        let front = if step_model(&step, &requested) == requested {
            0
        } else {
            steps
                .iter()
                .position(|other| step_model(other, &requested) != requested)
                .unwrap_or(steps.len())
        };
        steps.insert(front, step);
    }
    let cross = provider.name() != "anthropic";
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
                if cross {
                    fit_thinking(&mut substitute.extra, model);
                }
                (
                    pool[*index].alias.clone(),
                    model.clone(),
                    anthropic,
                    substitute,
                )
            }
            Step::Substitute(model) => {
                registry.prepare_model_catalog(model).await;
                let Some(target) = registry.provider_for_model(model, None) else {
                    continue;
                };
                if !target.availability().is_ready() {
                    continue;
                }
                let mut substitute = body.clone();
                substitute.model = Some(model.clone());
                substitute.bypass_provider_model_override = true;
                if target.name() != provider.name() {
                    fit_thinking(&mut substitute.extra, model);
                }
                (target.name().to_string(), model.clone(), target, substitute)
            }
        };
        let cooling = if count_tokens {
            registry.cooldowns.cooling(&owner, EVERY_MODEL)
        } else {
            registry.cooldowns.cooling(&owner, &model)
        };
        // A key's budget is checked before it is spent: a request that may not fit goes elsewhere.
        let over_budget = !count_tokens
            && matches!(&step, Step::Account(index, model) if pool[*index].budget_usd.is_some_and(|left| {
                !crate::key_budget::estimate_usd(model, &attempt_body).is_some_and(|cost| cost <= left)
            }));
        if cooling || over_budget {
            skipped_cooling = true;
            continue;
        }
        let first_choice = matches!(step, Step::Original)
            || matches!(step, Step::Account(index, _) if provider.name() == "anthropic" && pool[index].primary);
        // Thread state lives with the account that started it. Anything else gets
        // the full conversation, which Claude Code resends on this refusal.
        if threaded && !first_choice {
            if count_tokens {
                break;
            }
            return thread_unsupported();
        }
        let mut attempt = ctx.clone();
        attempt.provider = target.name().to_string();
        if let Step::Account(index, model) = &step
            && let Some(passthrough) = attempt.passthrough.as_mut()
        {
            if !pool[*index].sign(&mut passthrough.headers) {
                continue;
            }
            if *model != requested {
                match with_model(&passthrough.raw_body, model, cross) {
                    Some(raw) => passthrough.raw_body = raw,
                    None => continue,
                }
            }
        }
        // Only the chain's final option may wait out its provider's own retries.
        let response = crate::retry::fail_fast(position < last, async {
            if count_tokens {
                target.handle_count_tokens(attempt_body, attempt).await
            } else {
                target
                    .handle_messages_with_conversation_identity(
                        attempt_body,
                        attempt,
                        identity.clone(),
                    )
                    .await
            }
        })
        .await;
        let status = response.status();
        let keyed = matches!(step, Step::Account(index, _) if pool[index].api_key.is_some());
        let (response, refused) = if keyed
            && matches!(
                status,
                StatusCode::BAD_REQUEST | StatusCode::PAYMENT_REQUIRED
            ) {
            let (parts, body) = response.into_parts();
            let body = axum::body::to_bytes(body, 1 << 20)
                .await
                .unwrap_or_default();
            let refused = refusal(status, &body);
            (Response::from_parts(parts, Body::from(body)), refused)
        } else {
            (response, Refusal::Other)
        };
        let spent = refused == Refusal::OutOfCredit;
        if spent && policy.on.rate_limit {
            let until = next_month(now());
            registry.cooldowns.exhaust(&owner, until);
            log.info(
                "api key out of credit",
                Some(Map::from_iter([
                    ("reqId".to_string(), json!(ctx.req_id)),
                    ("account".to_string(), json!(owner)),
                    ("until".to_string(), json!(until)),
                ])),
            );
        }
        let quota =
            (status == StatusCode::TOO_MANY_REQUESTS || spent || refused == Refusal::Unreadable)
                && policy.on.rate_limit;
        let credential_rejected = matches!(step, Step::Account(_, _))
            && status == StatusCode::UNAUTHORIZED
            && policy.on.auth;
        let server_error = status.is_server_error() && policy.on.server_error;
        let unavailable = matches!(step, Step::Account(_, _))
            && status == StatusCode::NOT_FOUND
            && policy.on.model_unavailable;
        if !quota && !credential_rejected && !server_error && !unavailable {
            let served = format!("{owner}/{model}");
            if !first_choice && response.status().is_success() {
                note(log, &ctx.req_id, &requested, &served);
            }
            let mut response = response;
            // The --serve proxy's traffic recorder reads which account answered.
            if let Ok(value) = HeaderValue::from_str(&served) {
                response.headers_mut().insert(SERVED_BY_HEADER, value);
            }
            return match route.filter(|_| response.status().is_success()) {
                Some(route) => remember_on_completion(
                    response,
                    (
                        registry.routes.clone(),
                        route.key,
                        route.generation,
                        Served::of(&step, &pool),
                    ),
                ),
                None => response,
            };
        }
        // Model access can change without a reset header. Only quota/auth failures cool down.
        if !count_tokens && !server_error && !unavailable && refused == Refusal::Other {
            registry
                .cooldowns
                .record_rejection(&owner, &model, response.headers());
        }
        // Lack of model access or credit must not hide a usable account's quota/auth failure:
        // Claude Code retries a 429 but stops at a 400.
        let weak = |status: StatusCode| {
            matches!(
                status,
                StatusCode::NOT_FOUND | StatusCode::BAD_REQUEST | StatusCode::PAYMENT_REQUIRED
            )
        };
        if rejection
            .as_ref()
            .is_none_or(|prior| weak(prior.status()) && !weak(status))
        {
            rejection = Some(response);
        }
    }

    rejection
        .filter(|response| response.status() != StatusCode::NOT_FOUND || !skipped_cooling)
        .unwrap_or_else(|| {
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

    const PROVIDERS: &[&str] = &[
        "anthropic",
        "codex",
        "cursor",
        "gemini",
        "grok",
        "kimi",
        "local",
        "zai",
    ];

    fn names() -> Vec<String> {
        PROVIDERS.iter().map(|name| name.to_string()).collect()
    }

    fn chain(provider: &str, model: &str, accounts: usize, policy: &Policy) -> Vec<Step> {
        super::chain(provider, model, accounts, policy, &names())
    }

    fn handles(provider: &str, model: &str, policy: &Policy) -> bool {
        super::handles(provider, model, policy, &names())
    }

    fn opus(index: usize) -> Step {
        Step::Account(index, "claude-opus-5-5".into())
    }

    #[test]
    fn provider_targets_keep_builtin_tier_providers_but_not_cursor_cli() {
        let registry = Registry::with_default_alias();
        let mut expected = names();
        expected.sort_unstable();
        assert_eq!(provider_targets(&registry), expected);
    }

    #[test]
    fn the_global_chain_is_every_familys_order_with_its_own_provider_first() {
        let global = policy(json!({"chains": {"*": ["kimi", "accounts:1", "codex", "accounts"]}}));
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 2, &global),
            [
                opus(0),
                sub("kimi-k3.1-pro-highspeed"),
                sub("gpt-6.1-sol"),
                opus(1)
            ]
        );
        assert_eq!(
            chain("codex", "gpt-6.1-sol", 2, &global),
            [
                Step::Original,
                sub("kimi-k3.1-pro-highspeed"),
                opus(0),
                opus(1)
            ]
        );
        assert_eq!(
            chain("kimi", "kimi-k3.1-pro-highspeed", 2, &global),
            [Step::Original, opus(0), sub("gpt-6.1-sol"), opus(1)]
        );
    }

    #[test]
    fn reordering_the_global_chain_reorders_every_family_both_ways() {
        let kimi_first =
            policy(json!({"chains": {"*": ["accounts:1", "kimi", "codex", "accounts"]}}));
        let codex_first =
            policy(json!({"chains": {"*": ["accounts:1", "codex", "kimi", "accounts"]}}));
        let kimi = sub("kimi-k3.1-pro-highspeed");
        let sol = sub("gpt-6.1-sol");
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 2, &kimi_first),
            [opus(0), kimi.clone(), sol.clone(), opus(1)]
        );
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 2, &codex_first),
            [opus(0), sol.clone(), kimi.clone(), opus(1)]
        );
        assert_eq!(
            chain("codex", "gpt-6.1-sol", 2, &kimi_first),
            [Step::Original, opus(0), kimi.clone(), opus(1)]
        );
        assert_eq!(
            chain("kimi", "kimi-k3.1-pro-highspeed", 2, &codex_first),
            [Step::Original, opus(0), sol.clone(), opus(1)]
        );
        let claude_last = policy(json!({"chains": {"*": ["kimi", "codex", "accounts"]}}));
        assert_eq!(
            chain("codex", "gpt-6.1-sol", 1, &claude_last),
            [Step::Original, kimi.clone(), opus(0)]
        );
        let claude_first = policy(json!({"chains": {"*": ["accounts", "kimi", "codex"]}}));
        assert_eq!(
            chain("codex", "gpt-6.1-sol", 1, &claude_first),
            [Step::Original, opus(0), kimi]
        );
    }

    #[test]
    fn a_per_family_chain_overrides_the_global_chain() {
        let configured = policy(json!({"chains": {
            "*": ["accounts", "kimi", "codex"],
            "codex": ["accounts", "codex"],
        }}));
        assert_eq!(
            chain("codex", "gpt-6.1-sol", 1, &configured),
            [opus(0), Step::Original]
        );
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &configured),
            [opus(0), sub("kimi-k3.1-pro-highspeed"), sub("gpt-6.1-sol")]
        );
    }

    #[test]
    fn the_requested_provider_runs_first_when_the_global_chain_omits_it() {
        let configured = policy(json!({"chains": {"*": ["codex"]}}));
        assert_eq!(
            chain("kimi", "kimi-k3.1-pro-highspeed", 1, &configured),
            [Step::Original, sub("gpt-6.1-sol")]
        );
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 2, &configured),
            [opus(0), opus(1), sub("gpt-6.1-sol")]
        );
        assert_eq!(chain("grok", "grok-4", 1, &configured), [Step::Original]);
        assert!(!handles("grok", "grok-4", &configured));
    }

    #[test]
    fn a_provider_without_a_tier_cell_is_skipped_never_a_model_id() {
        for chains in [
            json!({"*": ["accounts", "gemini", "grok", "local"]}),
            json!({"claude": ["accounts", "gemini", "grok", "local"]}),
        ] {
            let configured = policy(json!({ "chains": chains }));
            assert_eq!(
                chain("anthropic", "claude-opus-5-5", 1, &configured),
                [opus(0)]
            );
        }
        let exact = policy(json!({"chains": {"*": ["accounts", "gemini-3-pro", "gpt-6-luna"]}}));
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &exact),
            [opus(0), sub("gemini-3-pro"), sub("gpt-6-luna")]
        );
        let cursor = policy(json!({"chains": {"claude": ["accounts", "cursor"]}}));
        assert_eq!(
            chain("anthropic", "claude-unknown", 1, &cursor),
            [Step::Account(0, "claude-unknown".into())]
        );
        let cursor_cli = policy(json!({"chains": {
            "*": ["accounts", "cursor-cli"],
            "gemini": ["gemini", "claude-sonnet-5", "cursor-cli"],
        }}));
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &cursor_cli),
            [opus(0), sub("cursor-cli")]
        );
        assert_eq!(
            chain("gemini", "gemini-3-flash", 1, &cursor_cli),
            [
                Step::Original,
                Step::Account(0, "claude-sonnet-5".into()),
                sub("cursor-cli")
            ]
        );
        let gemini = policy(json!({"chains": {"*": ["accounts", "gemini", "codex"]}}));
        assert_eq!(
            chain("gemini", "gemini-3-flash", 1, &gemini),
            [Step::Original]
        );
    }

    fn default_chain(provider: &str, model: &str, accounts: usize) -> Vec<Step> {
        chain(provider, model, accounts, &Policy::default())
    }

    fn sub(model: &str) -> Step {
        Step::Substitute(model.into())
    }

    #[test]
    fn claude_requests_try_accounts_then_codex() {
        assert_eq!(
            default_chain("anthropic", "claude-opus-5-5", 2),
            [
                Step::Account(0, "claude-opus-5-5".into()),
                Step::Account(1, "claude-opus-5-5".into()),
                sub("gpt-6.1-sol"),
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
    fn codex_requests_try_codex_then_claude_accounts() {
        assert_eq!(
            default_chain("codex", "gpt-6-astra", 1),
            [Step::Original, Step::Account(0, "claude-fable-5-1".into())]
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
    fn malformed_or_missing_policy_defaults_never_include_cursor() {
        let malformed: Policy =
            serde_json::from_value(json!({"chains": "bad"})).unwrap_or_default();
        let missing: Policy = serde_json::from_value(json!({})).unwrap();
        for policy in [Policy::default(), malformed, missing] {
            for (provider, model) in [("anthropic", "claude-opus-5-5"), ("codex", "gpt-6.1-sol")] {
                assert!(
                    !chain(provider, model, 2, &policy)
                        .iter()
                        .any(|step| matches!(step, Step::Substitute(m) if m.starts_with("cursor"))),
                );
            }
        }
    }

    #[test]
    fn malformed_tiers_preserve_custom_chains_and_triggers() {
        let parsed = serde_json::from_value::<Policy>(json!({
            "chains": {"claude": ["accounts"]},
            "on": {"server_error": true},
            "tiers": [
                {"match": ["opus"], "models": {"codex": "sol"}},
                {"name": "valid", "match": ["sonnet"], "models": {"codex": "luna"}}
            ]
        }))
        .unwrap_or_default();
        assert_eq!(
            parsed.chains.get("claude"),
            Some(&vec!["accounts".to_string()])
        );
        assert!(parsed.on.server_error);
        assert_eq!(parsed.tiers.len(), 1);
        assert_eq!(parsed.tiers[0].name, "valid");
    }

    fn policy(value: Value) -> Policy {
        serde_json::from_value(value).unwrap()
    }

    #[test]
    fn glm_falls_back_to_claude_then_codex_and_claude_never_reaches_glm() {
        let policy = Policy::default();
        assert_eq!(
            chain("zai", "glm-4.6", 1, &policy),
            [
                Step::Original,
                Step::Account(0, "claude-sonnet-5-5".into()),
                sub("gpt-6-luna")
            ]
        );
        for (provider, model) in [("anthropic", "claude-opus-5-5"), ("kimi", "kimi-k3")] {
            assert!(
                !chain(provider, model, 1, &policy)
                    .iter()
                    .any(|step| matches!(step, Step::Substitute(m) if m.starts_with("glm-"))),
                "{model}"
            );
        }
    }

    #[test]
    fn every_kimi_source_has_one_exact_tier_and_routes_both_directions() {
        let policy = Policy::default();
        for id in crate::providers::kimi::translate::model_allowlist::all_public_model_ids() {
            let exact: Vec<_> = policy
                .tiers()
                .iter()
                .filter(|tier| tier.match_keys.iter().any(|key| key == id))
                .collect();
            assert_eq!(exact.len(), 1, "{id} needs one canonical source tier");
            let tier = exact[0];
            assert_eq!(policy.tier(&format!("{id}[1m]")).unwrap().name, tier.name);
            for destination in ["codex", "cursor", "kimi"] {
                let configured = super::tests::policy(json!({"chains": {"kimi": [destination]}}));
                let expected = if destination == "kimi" {
                    Step::Original
                } else {
                    sub(&tier.models[destination])
                };
                assert_eq!(
                    chain("kimi", id, 1, &configured),
                    [expected],
                    "{id} -> {destination}"
                );
            }
            assert_eq!(
                chain("kimi", id, 1, &policy),
                [
                    Step::Original,
                    Step::Account(0, tier.models["anthropic"].clone()),
                    sub(&tier.models["codex"])
                ]
            );
            for source in ["anthropic", "codex", "cursor"] {
                let family = if source == "anthropic" {
                    "claude"
                } else {
                    source
                };
                let configured = super::tests::policy(json!({"chains": {family: ["kimi"]}}));
                assert_eq!(
                    chain(source, &tier.models[source], 1, &configured),
                    [sub(&tier.models["kimi"])]
                );
            }
        }
    }

    #[test]
    fn defaults_expose_the_chains_and_tiers_the_router_uses() {
        let defaults = defaults();
        assert_eq!(defaults["chains"]["claude"], json!(["accounts", "codex"]));
        assert_eq!(defaults["chains"]["codex"], json!(["codex", "accounts"]));
        let tiers = defaults["tiers"].as_array().unwrap();
        assert_eq!(tiers.len(), 3);
        assert_eq!(
            tiers[0]["match"],
            json!([
                "opus",
                "sol",
                "kimi-k3.1-pro-highspeed",
                "k3.1-pro-highspeed"
            ])
        );
        assert_eq!(tiers[0]["models"]["codex"], "gpt-6.1-sol");
        let round_trip = policy(json!({"tiers": defaults["tiers"]}));
        assert_eq!(
            chain("anthropic", "claude-haiku-5-5", 1, &round_trip),
            default_chain("anthropic", "claude-haiku-5-5", 1)
        );
    }

    #[test]
    fn current_fable_source_keeps_pinned_reverse_destination() {
        let configured = policy(json!({"chains": {"claude": ["kimi"]}}));
        assert_eq!(
            chain("anthropic", "claude-fable-5-2", 1, &configured),
            [sub("kimi-k3.1-pro")]
        );
        assert_eq!(
            Policy::default().tier("k3.1-pro").unwrap().models["anthropic"],
            "claude-fable-5-1"
        );
    }

    #[test]
    fn configured_tiers_replace_every_builtin_group() {
        let custom = policy(json!({"tiers": [
            {"name": "mine", "match": ["opus"], "models": {"anthropic": "claude-opus-5-5", "codex": "gpt-custom"}},
        ]}));
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &custom)[1],
            sub("gpt-custom")
        );
        assert_eq!(
            chain("anthropic", "claude-sonnet-5-5", 1, &custom),
            [Step::Account(0, "claude-sonnet-5-5".into())]
        );
        let empty = policy(json!({"tiers": []}));
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &empty),
            default_chain("anthropic", "claude-opus-5-5", 1)
        );
    }

    #[test]
    fn an_exact_match_beats_a_family_key_and_aliases_are_destinations() {
        let custom = policy(json!({"tiers": [
            {"name": "family", "match": ["opus"], "models": {"anthropic": "claude-opus-5-5", "codex": "gpt-family"}},
            {"name": "pin", "match": ["claude-opus-5-5"], "models": {"anthropic": "claude-opus-5-5", "codex": "sol"}},
        ]}));
        assert_eq!(
            chain("anthropic", "claude-opus-5-5[1m]", 1, &custom)[1],
            sub("sol")
        );
        assert_eq!(
            chain("anthropic", "Claude-Opus-9", 1, &custom)[1],
            sub("gpt-family")
        );
    }

    #[test]
    fn a_missing_destination_skips_and_cursor_is_never_reenabled() {
        let custom = policy(json!({"tiers": [
            {"name": "t", "match": ["opus"], "models": {"anthropic": "claude-opus-5-5"}},
        ]}));
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &custom),
            [Step::Account(0, "claude-opus-5-5".into())]
        );
        let with_cursor = policy(json!({"tiers": [
            {"name": "t", "match": ["opus"], "models": {"anthropic": "claude-opus-5-5", "cursor": "cursor:x"}},
        ]}));
        assert!(!chain("anthropic", "claude-opus-5-5", 1, &with_cursor).contains(&sub("cursor:x")));
        let explicit = policy(
            json!({"chains": {"claude": ["accounts", "cursor"]}, "tiers": with_cursor.tiers}),
        );
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &explicit)[1],
            sub("cursor:x")
        );
    }

    #[test]
    fn other_backends_can_sit_between_claude_accounts() {
        let policy: Policy = serde_json::from_value(json!({"chains": {
            "claude": ["accounts:1", "codex", "accounts:1", "cursor", "accounts"],
            "codex": ["codex", "accounts:5", "cursor", "accounts"],
        }}))
        .unwrap();
        let opus = |i| Step::Account(i, "claude-opus-5-5".into());
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 3, &policy),
            [
                opus(0),
                sub("gpt-6.1-sol"),
                opus(1),
                sub("cursor:claude-opus-5"),
                opus(2)
            ]
        );
        assert_eq!(
            chain("codex", "gpt-6.1-sol", 2, &policy),
            [
                Step::Original,
                opus(0),
                opus(1),
                sub("cursor:claude-opus-5")
            ]
        );
        let bad: Policy =
            serde_json::from_value(json!({"chains": {"claude": ["accounts:x", "accountsy"]}}))
                .unwrap();
        assert_eq!(
            chain("anthropic", "claude-opus-5-5", 1, &bad),
            [sub("accounts:x"), sub("accountsy")]
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
            serde_json::from_slice(&with_model(&raw, "claude-sonnet-5-5", false).unwrap()).unwrap();
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
    fn a_completion_from_an_evicted_route_cannot_overwrite_its_replacement() {
        let routes = Routes::default();
        let pool = Pool {
            accounts: vec![PoolAccount {
                api_key: None,
                budget_usd: None,
                alias: "owner".into(),
                authorization: "Bearer token".into(),
                primary: true,
            }],
            policy: Policy::default(),
        };
        let identity = ConversationIdentity::Main("session".into());
        let older = routes.begin(Some(&identity), Some(&pool), "opus").unwrap();
        routes.0.lock().unwrap().entries.clear();
        let newer = routes.begin(Some(&identity), Some(&pool), "opus").unwrap();
        routes.complete(
            &newer.key,
            newer.generation,
            Served::Substitute("new".into()),
        );
        routes.complete(
            &older.key,
            older.generation,
            Served::Substitute("old".into()),
        );
        assert_eq!(
            routes
                .begin(Some(&identity), Some(&pool), "opus")
                .unwrap()
                .pinned,
            Some(Served::Substitute("new".into()))
        );
    }

    #[test]
    fn credit_runs_out_until_the_first_of_next_month_utc() {
        // 2026-11-23 21:30 UTC -> 2026-12-01; 2026-12-31 23:59:59 UTC -> 2027-01-01.
        assert_eq!(next_month(1_795_469_400), 1_796_083_200);
        assert_eq!(next_month(1_798_761_599), 1_798_761_600);
        let cooldowns = Cooldowns::default();
        cooldowns.exhaust("key", next_month(now()));
        assert!(
            cooldowns.cooling("key", "claude-opus-5-5") && cooldowns.cooling("key", EVERY_MODEL)
        );
        assert!(!cooldowns.cooling("other", "claude-opus-5-5"));
    }

    #[test]
    fn only_credit_errors_count_as_out_of_credit_and_unreadable_ones_are_flagged() {
        let error = |kind: &str, message: &str| {
            json!({"type": "error", "error": {"type": kind, "message": message}}).to_string()
        };
        let credit = error(
            "invalid_request_error",
            "Your credit balance is too low to access the Anthropic API.",
        );
        let org = error(
            "invalid_request_error",
            "You have reached your specified API usage limits. You will regain access on 2026-12-01 at 00:00 UTC.",
        );
        let billing = error("billing_error", "Credit balance is too low");
        let bad = error(
            "invalid_request_error",
            "messages: text content blocks must be non-empty",
        );
        assert_eq!(
            refusal(StatusCode::BAD_REQUEST, credit.as_bytes()),
            Refusal::OutOfCredit
        );
        assert_eq!(
            refusal(StatusCode::BAD_REQUEST, org.as_bytes()),
            Refusal::OutOfCredit
        );
        assert_eq!(
            refusal(StatusCode::BAD_REQUEST, billing.as_bytes()),
            Refusal::OutOfCredit
        );
        assert_eq!(
            refusal(StatusCode::PAYMENT_REQUIRED, billing.as_bytes()),
            Refusal::OutOfCredit
        );
        assert_eq!(
            refusal(StatusCode::BAD_REQUEST, bad.as_bytes()),
            Refusal::Other
        );
        assert_eq!(
            refusal(StatusCode::BAD_REQUEST, &[0x1b, 0x8f, 0x01]),
            Refusal::Unreadable
        );
    }

    #[test]
    fn a_pool_member_never_prints_its_credential() {
        let member: PoolAccount = serde_json::from_value(
            json!({"alias": "console", "kind": "api_key", "api_key": "sk-ant-fake-secret", "primary": true}),
        )
        .unwrap();
        assert!(!format!("{member:?}").contains("sk-ant-fake-secret"));
        let mut headers = HeaderMap::new();
        headers.insert("authorization", HeaderValue::from_static("Bearer login"));
        assert!(member.sign(&mut headers));
        assert!(headers.get("authorization").is_none());
        assert!(headers["x-api-key"].is_sensitive());
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
