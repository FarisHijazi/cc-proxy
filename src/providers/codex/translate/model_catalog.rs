use std::{
    io::Read,
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Duration, Instant},
};

use serde::Deserialize;
use sha2::{Digest, Sha256};

use super::{
    model_allowlist::{ALLOWED_MODELS, FAMILY_ALIASES, MODEL_ALIASES, ResolvedModel},
    request::ServiceTier,
};
use crate::providers::codex::auth::{
    constants::CODEX_API_ENDPOINT,
    token_store::{codex_auth_file, file_store},
};

const TTL: Duration = Duration::from_secs(300);
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(3);
const BODY_LIMIT: u64 = 4 * 1024 * 1024;
const CLIENT_VERSION: &str = "0.165.0";

#[derive(Clone)]
pub(super) struct ModelCatalog {
    bases: Vec<String>,
}

impl Default for ModelCatalog {
    fn default() -> Self {
        Self {
            bases: ALLOWED_MODELS.iter().map(|s| (*s).to_string()).collect(),
        }
    }
}

impl ModelCatalog {
    fn extend(&mut self, models: Vec<String>) {
        self.bases.extend(models);
        self.bases.sort_unstable();
        self.bases.dedup();
    }

    pub(super) fn contains(&self, model: &str) -> bool {
        self.bases.iter().any(|m| m == model)
    }

    pub(super) fn resolve(&self, model: &str) -> ResolvedModel {
        let normalized = crate::registry::normalize_incoming_model(model);
        let (base, fast) = normalized
            .strip_suffix("-fast")
            .map(|base| (base, true))
            .unwrap_or((&normalized, false));
        let alias = MODEL_ALIASES
            .iter()
            .find(|(alias, _)| *alias == base)
            .map(|(_, target)| *target)
            .unwrap_or(base);
        let latest = FAMILY_ALIASES
            .contains(&alias)
            .then(|| {
                self.bases
                    .iter()
                    .filter_map(|model| {
                        let (version, family) = numeric_family(model)?;
                        (family == alias).then_some((version, model))
                    })
                    .max_by(|a, b| a.0.cmp(&b.0))
                    .map(|(_, model)| model.as_str())
            })
            .flatten();
        let target = latest.unwrap_or(alias);
        let recognized = self.contains(target);
        ResolvedModel {
            model: if recognized {
                target.to_string()
            } else {
                normalized
            },
            service_tier: (fast && recognized).then_some(ServiceTier::Priority),
        }
    }

    pub(super) fn selectable_models(&self) -> Vec<String> {
        let mut models = self.bases.clone();
        models.extend(
            FAMILY_ALIASES
                .iter()
                .filter(|family| self.contains(&self.resolve(family).model))
                .map(|s| (*s).to_string()),
        );
        models.extend(models.clone().iter().map(|m| format!("{m}-fast")));
        models.sort_unstable();
        models
    }
}

fn numeric_family(model: &str) -> Option<(Vec<u32>, &str)> {
    let (version, family) = model.strip_prefix("gpt-")?.rsplit_once('-')?;
    if !FAMILY_ALIASES.contains(&family) {
        return None;
    }
    let version = version
        .split('.')
        .map(|part| {
            if part.is_empty() || !part.bytes().all(|c| c.is_ascii_digit()) {
                return None;
            }
            part.parse().ok()
        })
        .collect::<Option<Vec<u32>>>()?;
    Some((version, family))
}

#[derive(Deserialize)]
struct ModelsResponse {
    models: Vec<ModelInfo>,
    #[serde(default)]
    identity: Option<String>,
}
#[derive(Deserialize)]
struct ModelInfo {
    slug: String,
    #[serde(default)]
    supported_in_api: bool,
    #[serde(default)]
    visibility: String,
}

fn decode_models(bytes: &[u8]) -> Option<Vec<String>> {
    let response: ModelsResponse = serde_json::from_slice(bytes).ok()?;
    Some(
        response
            .models
            .into_iter()
            .filter(|model| {
                model.supported_in_api
                    && model.visibility != "hide"
                    && (ALLOWED_MODELS.contains(&model.slug.as_str())
                        || numeric_family(&model.slug).is_some())
            })
            .map(|model| model.slug)
            .collect(),
    )
}

fn decode_cli_models(bytes: &[u8]) -> Option<Vec<String>> {
    let response: ModelsResponse = serde_json::from_slice(bytes).ok()?;
    // Codex's opaque digest includes provider configuration and account claims
    // unavailable to this access-only reader. Never guess that it matches.
    if response.identity.is_some() {
        return None;
    }
    decode_models(bytes)
}

#[derive(Clone)]
struct CachedCatalog {
    source: (PathBuf, PathBuf, String, Option<[u8; 32]>),
    checked: Instant,
    catalog: ModelCatalog,
    allow_cli_cache: bool,
}

static CACHE: Mutex<Option<CachedCatalog>> = Mutex::new(None);
static REFRESH: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn cache_path() -> PathBuf {
    std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".codex")
        })
        .join("models_cache.json")
}

fn credential_generation(path: &Path) -> Option<[u8; 32]> {
    std::fs::read(path)
        .ok()
        .map(|bytes| Sha256::digest(bytes).into())
}

pub(super) async fn prepare_catalog() {
    let _refresh = REFRESH.lock().await;
    let _ = tokio::task::spawn_blocking(refresh_catalog).await;
}

pub(super) fn catalog() -> ModelCatalog {
    if tokio::runtime::Handle::try_current().is_ok() {
        let mut snapshot = CACHE.lock().unwrap_or_else(|e| e.into_inner()).clone();
        cached_catalog(
            &mut snapshot,
            codex_auth_file(),
            &cache_path(),
            Instant::now(),
            || None,
        )
    } else {
        refresh_catalog()
    }
}

fn refresh_catalog() -> ModelCatalog {
    let mut snapshot = CACHE.lock().unwrap_or_else(|e| e.into_inner()).clone();
    let auth_path = codex_auth_file();
    let path = cache_path();
    let catalog = cached_catalog(
        &mut snapshot,
        auth_path.clone(),
        &path,
        Instant::now(),
        || {
            let auth = file_store().load_auth().ok()??;
            let base = crate::config::codex_base_url(CODEX_API_ENDPOINT);
            let base = base.trim_end_matches('/');
            let root = base.strip_suffix("/responses").unwrap_or(base);
            discover_models(&format!("{root}/models"), &auth)
        },
    );
    if snapshot
        .as_ref()
        .is_some_and(|c| c.source.3 == credential_generation(&auth_path))
    {
        *CACHE.lock().unwrap_or_else(|e| e.into_inner()) = snapshot;
        catalog
    } else {
        ModelCatalog::default()
    }
}

fn cached_catalog(
    cache: &mut Option<CachedCatalog>,
    auth_path: PathBuf,
    cache_path: &Path,
    now: Instant,
    fetch: impl FnOnce() -> Option<Vec<String>>,
) -> ModelCatalog {
    let generation = credential_generation(&auth_path);
    let source = (
        auth_path,
        cache_path.to_path_buf(),
        crate::config::codex_base_url(CODEX_API_ENDPOINT),
        generation,
    );
    if let Some(cached) = cache
        .as_ref()
        .filter(|c| c.source == source && now.duration_since(c.checked) < TTL)
    {
        return cached.catalog.clone();
    }
    let allow_cli_cache = cache
        .as_ref()
        .map(|cached| {
            if cached.source.0 == source.0
                && cached.source.1 == source.1
                && cached.source.2 == source.2
            {
                cached.allow_cli_cache && cached.source == source
            } else {
                true
            }
        })
        .unwrap_or(true);
    let discovered = fetch();
    let from_provider = discovered.is_some();
    let mut catalog = if let Some(models) = discovered {
        let mut catalog = ModelCatalog::default();
        catalog.extend(models);
        catalog
    } else {
        cache
            .as_ref()
            .filter(|c| c.source == source)
            .map(|c| c.catalog.clone())
            .unwrap_or_default()
    };
    if !from_provider
        && allow_cli_cache
        && let Some(models) = std::fs::read(cache_path)
            .ok()
            .and_then(|b| decode_cli_models(&b))
    {
        catalog.extend(models);
    }
    *cache = Some(CachedCatalog {
        source,
        checked: now,
        catalog: catalog.clone(),
        allow_cli_cache,
    });
    catalog
}

fn discover_models(
    url: &str,
    auth: &crate::providers::codex::auth::token_store::StoredAuth,
) -> Option<Vec<String>> {
    let client = reqwest::blocking::Client::builder()
        .timeout(DISCOVERY_TIMEOUT)
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .ok()?;
    let mut request = client
        .get(url)
        .query(&[("client_version", CLIENT_VERSION)])
        .bearer_auth(&auth.access)
        .header("originator", "codex_cli_rs")
        .header(
            reqwest::header::USER_AGENT,
            format!("codex_cli_rs/{CLIENT_VERSION}"),
        );
    if let Some(account_id) = &auth.account_id {
        request = request.header("ChatGPT-Account-Id", account_id);
    }
    let response = request.send().ok()?;
    if !response.status().is_success() {
        return None;
    }
    let mut bytes = Vec::new();
    response.take(BODY_LIMIT + 1).read_to_end(&mut bytes).ok()?;
    if bytes.len() as u64 > BODY_LIMIT {
        return None;
    }
    decode_models(&bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mock_models(status: &str, body: &str) -> (String, std::thread::JoinHandle<String>) {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/models", listener.local_addr().unwrap());
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let task = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut bytes = Vec::new();
            while !bytes.ends_with(b"\r\n\r\n") {
                let mut byte = [0];
                if stream.read(&mut byte).unwrap() == 0 {
                    break;
                }
                bytes.push(byte[0]);
            }
            stream.write_all(response.as_bytes()).unwrap();
            String::from_utf8(bytes).unwrap()
        });
        (url, task)
    }

    fn toy_auth() -> crate::providers::codex::auth::token_store::StoredAuth {
        crate::providers::codex::auth::token_store::StoredAuth {
            access: "toy-access".into(),
            refresh: "never-spend".into(),
            expires: 0,
            account_id: Some("toy-account".into()),
        }
    }

    #[test]
    fn discovery_uses_authenticated_get_and_native_schema_without_refreshing() {
        let (url, task) = mock_models(
            "200 OK",
            r#"{"models":[{"slug":"gpt-6.1-astra","visibility":"list","supported_in_api":true,"context_window":272000}]}"#,
        );
        assert_eq!(
            discover_models(&url, &toy_auth()).unwrap(),
            ["gpt-6.1-astra"]
        );
        let request = task.join().unwrap().to_ascii_lowercase();
        assert!(request.starts_with("get /models?client_version=0.165.0 http/1.1"));
        assert!(request.contains("authorization: bearer toy-access\r\n"));
        assert!(request.contains("chatgpt-account-id: toy-account\r\n"));
        assert!(request.contains("originator: codex_cli_rs\r\n"));
        assert!(!request.contains("never-spend"));
    }

    #[test]
    fn discovery_does_not_retry_auth_errors_or_follow_redirects() {
        for status in ["401 Unauthorized", "302 Found", "500 Internal Server Error"] {
            let (url, task) = mock_models(status, "{}");
            assert!(discover_models(&url, &toy_auth()).is_none());
            task.join().unwrap();
        }
    }

    #[test]
    fn ttl_refresh_advances_family_and_failure_retains_read_only_fallback() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("models_cache.json");
        let raw =
            r#"{"models":[{"slug":"gpt-6.9-astra","visibility":"list","supported_in_api":true}]}"#;
        std::fs::write(&path, raw).unwrap();
        let auth_path = dir.path().join("auth.json");
        std::fs::write(&auth_path, "never change credentials").unwrap();
        let now = Instant::now();
        let mut cache = None;
        let catalog = cached_catalog(&mut cache, auth_path.clone(), &path, now, || {
            Some(vec!["gpt-6-astra".into()])
        });
        assert_eq!(
            catalog.resolve("astra").model,
            "gpt-6-astra",
            "provider wins over stale disk cache"
        );
        let catalog = cached_catalog(
            &mut cache,
            auth_path.clone(),
            &path,
            now + TTL - Duration::from_secs(1),
            || panic!("must not fetch before TTL"),
        );
        assert_eq!(catalog.resolve("astra").model, "gpt-6-astra");
        let catalog = cached_catalog(&mut cache, auth_path.clone(), &path, now + TTL, || {
            Some(vec!["gpt-6.1-astra".into()])
        });
        assert_eq!(catalog.resolve("astra-fast[1m]").model, "gpt-6.1-astra");
        assert_eq!(catalog.resolve("gpt-6-astra").model, "gpt-6-astra");
        std::fs::write(&path, "malformed").unwrap();
        let catalog = cached_catalog(&mut cache, auth_path.clone(), &path, now + TTL * 2, || None);
        assert_eq!(catalog.resolve("astra").model, "gpt-6.1-astra");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "malformed");
        assert_eq!(
            std::fs::read_to_string(&auth_path).unwrap(),
            "never change credentials"
        );
        std::fs::write(&path, raw).unwrap();
        let catalog = cached_catalog(&mut None, auth_path, &path, now, || None);
        assert_eq!(catalog.resolve("astra").model, "gpt-6.9-astra");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), raw);
    }

    #[test]
    fn credential_replacement_invalidates_before_ttl_and_rejects_old_fallback() {
        let dir = tempfile::tempdir().unwrap();
        let auth_path = dir.path().join("auth.json");
        let path = dir.path().join("models_cache.json");
        std::fs::write(
            &auth_path,
            r#"{"tokens":{"account_id":"account-a","access_token":"token-a"}}"#,
        )
        .unwrap();
        std::fs::write(
            &path,
            r#"{"models":[{"slug":"gpt-6.9-astra","supported_in_api":true,"visibility":"list"}]}"#,
        )
        .unwrap();
        let now = Instant::now();
        let mut cache = None;
        let first = cached_catalog(&mut cache, auth_path.clone(), &path, now, || {
            Some(vec!["gpt-6.1-astra".into()])
        });
        assert_eq!(first.resolve("astra").model, "gpt-6.1-astra");
        let replacement = r#"{"tokens":{"account_id":"account-b","access_token":"token-b"}}"#;
        std::fs::write(&auth_path, replacement).unwrap();
        let mut fetched = false;
        let second = cached_catalog(
            &mut cache,
            auth_path.clone(),
            &path,
            now + Duration::from_secs(1),
            || {
                fetched = true;
                None
            },
        );
        assert!(fetched, "replacement must invalidate the still-fresh cache");
        assert_eq!(second.resolve("astra").model, "gpt-6-astra");
        let later = cached_catalog(&mut cache, auth_path.clone(), &path, now + TTL * 2, || None);
        assert_eq!(later.resolve("astra").model, "gpt-6-astra");
        assert_eq!(std::fs::read_to_string(&auth_path).unwrap(), replacement);
    }

    #[test]
    fn opaque_cli_cache_identity_is_not_assumed_to_match_current_account() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("models_cache.json");
        let raw = r#"{"identity":"opaque-other-provider-account-digest","models":[{"slug":"gpt-6.9-astra","supported_in_api":true,"visibility":"list"}]}"#;
        std::fs::write(&path, raw).unwrap();
        let catalog = cached_catalog(
            &mut None,
            dir.path().join("auth.json"),
            &path,
            Instant::now(),
            || None,
        );
        assert_eq!(catalog.resolve("astra").model, "gpt-6-astra");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), raw);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn claude_alias_on_codex_prepares_effective_override_and_advances_after_ttl() {
        use super::super::model_allowlist::resolve_model_request;
        use crate::{config::AliasProvider, registry::Registry, server::app};
        use axum::{
            Json, Router,
            body::Body,
            http::{Request, StatusCode},
            routing::get,
        };
        use std::sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        };
        use tower::ServiceExt;

        struct Restore(Vec<(&'static str, Option<std::ffi::OsString>)>);
        impl Drop for Restore {
            fn drop(&mut self) {
                for (key, value) in self.0.drain(..) {
                    unsafe {
                        match value {
                            Some(value) => std::env::set_var(key, value),
                            None => std::env::remove_var(key),
                        }
                    }
                }
            }
        }
        let home = tempfile::tempdir().unwrap();
        let auth_path = home.path().join("auth.json");
        std::fs::write(
            &auth_path,
            r#"{"tokens":{"access_token":"toy-access","account_id":"toy-account"}}"#,
        )
        .unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let captured = calls.clone();
        let metadata = Router::new().route("/models", get(move || {
            let captured = captured.clone();
            async move {
                let model = if captured.fetch_add(1, Ordering::SeqCst) == 0 { "gpt-6.1-astra" } else { "gpt-6.10-astra" };
                Json(serde_json::json!({"models":[{"slug":model,"visibility":"list","supported_in_api":true}]}))
            }
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}/responses", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, metadata).await.unwrap() });
        let mut restore = Restore(Vec::new());
        for (key, value) in [
            ("CODEX_HOME", home.path().to_string_lossy().into_owned()),
            (
                "CCP_CODEX_AUTH_FILE",
                auth_path.to_string_lossy().into_owned(),
            ),
            ("CCP_CODEX_BASE_URL", base),
            ("CCP_CODEX_MODEL", "astra".into()),
            ("NO_PROXY", "127.0.0.1".into()),
        ] {
            restore.0.push((key, std::env::var_os(key)));
            unsafe { std::env::set_var(key, value) };
        }
        *CACHE.lock().unwrap() = None;
        Registry::new(AliasProvider::Anthropic)
            .prepare_model_catalog("sonnet")
            .await;
        assert_eq!(
            calls.load(Ordering::SeqCst),
            0,
            "native Anthropic must not discover Codex"
        );
        let router = app(Arc::new(Registry::new(AliasProvider::Codex)));
        for (index, expected) in ["gpt-6.1-astra", "gpt-6.10-astra"].into_iter().enumerate() {
            if index == 1 {
                CACHE.lock().unwrap().as_mut().unwrap().checked = Instant::now() - TTL;
            }
            let request = Request::post("/v1/messages/count_tokens").header("content-type", "application/json")
                .body(Body::from(serde_json::json!({"model":"sonnet","messages":[{"role":"user","content":"hi"}]}).to_string())).unwrap();
            assert_eq!(
                router.clone().oneshot(request).await.unwrap().status(),
                StatusCode::OK
            );
            assert_eq!(calls.load(Ordering::SeqCst), index + 1);
            assert_eq!(resolve_model_request("sonnet").model, expected);
        }
        server.abort();
        *CACHE.lock().unwrap() = None;
    }

    #[test]
    fn malformed_catalogs_fall_back_without_guessing_models() {
        for raw in [b"broken".as_slice(), b"{}", b"{\"models\":null}"] {
            assert!(decode_models(raw).is_none());
        }
        let catalog = ModelCatalog::default();
        assert_eq!(catalog.resolve("astra[1m]").model, "gpt-6-astra");
        assert_eq!(catalog.resolve("terra").model, "gpt-5.6-terra");
        assert_eq!(
            catalog.resolve("gpt-7-astra-fast").model,
            "gpt-7-astra-fast"
        );
    }
}
