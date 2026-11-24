use std::time::{SystemTime, UNIX_EPOCH};

use crate::providers::kimi::auth::constants::api_base_url;
use crate::providers::kimi::auth::headers::common_headers;
use crate::providers::kimi::auth::manager::KimiAuthManager;
use crate::providers::kimi::auth::token_store::{StoredAuth, file_store};
use crate::providers::kimi::translate::model_allowlist::EndpointKind;
use crate::providers::kimi::translate::request::KimiChatRequest;
use crate::retry::{MAX_RATE_LIMIT_RETRIES, compute_backoff_delay};

#[derive(Debug)]
pub struct KimiError {
    pub status: u16,
    pub message: String,
    pub detail: Option<String>,
    pub retry_after: Option<String>,
}

/// One way to reach Kimi. The subscription login comes first; the paid Moonshot API key
/// is tried only when the login is missing, out of quota or rejected.
pub struct Lane {
    pub base_url: String,
    pub endpoint: EndpointKind,
    api_key: Option<String>,
}

pub fn api_key() -> Option<String> {
    let path = crate::paths::kimi_api_key_file(&crate::paths::DirResolverEnv::default());
    let key = std::fs::read_to_string(path).ok()?.trim().to_string();
    (!key.is_empty()).then_some(key)
}

pub fn lanes() -> Vec<Lane> {
    let key = api_key();
    let has_login = matches!(file_store().load_auth(), Ok(Some(_)));
    let mut lanes = Vec::new();
    if has_login || key.is_none() {
        let base_url = api_base_url();
        lanes.push(Lane {
            endpoint: EndpointKind::from_base_url(&base_url),
            base_url,
            api_key: None,
        });
    }
    if let Some(key) = key {
        lanes.push(Lane {
            base_url: crate::config::kimi_api_key_base_url(),
            endpoint: EndpointKind::MoonshotOpenPlatform,
            api_key: Some(key),
        });
    }
    lanes
}

impl KimiError {
    /// Quota, credit or credential trouble that another lane might not share.
    pub fn lane_exhausted(&self) -> bool {
        matches!(self.status, 401 | 402 | 403 | 429)
    }

    fn quota_spent(&self) -> bool {
        let detail = self.detail.as_deref().unwrap_or_default().to_lowercase();
        self.status == 429
            && ["quota", "insufficient", "balance"]
                .iter()
                .any(|word| detail.contains(word))
    }
}

pub struct KimiResponse {
    pub body: reqwest::Response,
    pub status: u16,
    pub request_start_time: u64,
}

pub struct KimiHttpClient {
    client: reqwest::Client,
    auth_manager: std::sync::Arc<KimiAuthManager<crate::auth::FileAuthStore<StoredAuth>>>,
}

impl Default for KimiHttpClient {
    fn default() -> Self {
        Self::new()
    }
}

impl KimiHttpClient {
    pub fn new() -> Self {
        Self {
            client: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(120))
                .build()
                .expect("failed to create HTTP client"),
            auth_manager: std::sync::Arc::new(KimiAuthManager::new(file_store())),
        }
    }

    pub fn auth_manager(&self) -> &KimiAuthManager<crate::auth::FileAuthStore<StoredAuth>> {
        &self.auth_manager
    }

    pub async fn post_kimi(
        &self,
        body: &KimiChatRequest,
        lane: &Lane,
    ) -> Result<KimiResponse, KimiError> {
        let mut auth = match &lane.api_key {
            Some(key) => StoredAuth {
                access: key.clone(),
                refresh: String::new(),
                expires: u64::MAX,
                scope: None,
                user_id: None,
            },
            None => self.login().await?,
        };

        let mut attempt = 0u32;
        loop {
            let result = self.attempt_post(&lane.base_url, &auth.access, body).await;

            match result {
                Err(error) if error.status == 401 && attempt == 0 && !auth.refresh.is_empty() => {
                    // First 401: try refresh
                    let manager = self.auth_manager.clone();
                    match tokio::task::spawn_blocking(move || manager.force_refresh()).await {
                        Ok(Ok(new_auth)) => {
                            auth = new_auth;
                            attempt += 1;
                            continue;
                        }
                        other => {
                            return Err(KimiError {
                                status: 401,
                                message: "Unauthorized".to_string(),
                                detail: Some(format!("OAuth refresh failed: {other:?}")),
                                retry_after: None,
                            });
                        }
                    }
                }
                Ok(response) => return Ok(response),
                Err(err @ KimiError { status: 429, .. }) => {
                    if attempt < MAX_RATE_LIMIT_RETRIES && !err.quota_spent() {
                        let delay = compute_backoff_delay(attempt, err.retry_after.as_deref());
                        if delay.exceeds_budget {
                            return Err(err);
                        }
                        crate::retry::sleep(delay.wait_ms).await;
                        attempt += 1;
                        continue;
                    }
                    return Err(err);
                }
                Err(err) => return Err(err),
            }
        }
    }

    async fn login(&self) -> Result<StoredAuth, KimiError> {
        let manager = self.auth_manager.clone();
        tokio::task::spawn_blocking(move || manager.get_auth())
            .await
            .map_err(|e| KimiError {
                status: 500,
                message: e.to_string(),
                detail: None,
                retry_after: None,
            })?
            .map_err(|e| KimiError {
                status: 401,
                message: "Auth error".to_string(),
                detail: Some(e.to_string()),
                retry_after: None,
            })
    }

    async fn attempt_post(
        &self,
        base_url: &str,
        access_token: &str,
        body: &KimiChatRequest,
    ) -> Result<KimiResponse, KimiError> {
        let headers = common_headers().map_err(|e| KimiError {
            status: 500,
            message: "Failed to build headers".to_string(),
            detail: Some(e.to_string()),
            retry_after: None,
        })?;

        let url = format!("{base_url}/chat/completions");
        let body_json = serde_json::to_string(body).map_err(|e| KimiError {
            status: 500,
            message: "Failed to serialize request".to_string(),
            detail: Some(e.to_string()),
            retry_after: None,
        })?;

        let request_start_time = now_ms();

        let mut req_builder = self
            .client
            .post(&url)
            .header("Content-Type", "application/json")
            .header("Accept", "text/event-stream")
            .header("Authorization", format!("Bearer {access_token}"));

        // Add common headers
        for (k, v) in &headers {
            if let Ok(name) = reqwest::header::HeaderName::from_bytes(k.as_bytes())
                && let Ok(value) = reqwest::header::HeaderValue::from_str(v)
            {
                req_builder = req_builder.header(name, value);
            }
        }

        let resp = match req_builder.body(body_json).send().await {
            Ok(r) => r,
            Err(e) => {
                return Err(KimiError {
                    status: 0,
                    message: "Network error".to_string(),
                    detail: Some(e.to_string()),
                    retry_after: None,
                });
            }
        };

        let status = resp.status().as_u16();

        if status == 429 {
            let retry_after = resp
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.to_string());
            let text = resp.text().await.unwrap_or_default();
            return Err(KimiError {
                status: 429,
                message: "Rate limited".to_string(),
                detail: if text.is_empty() { None } else { Some(text) },
                retry_after,
            });
        }

        if status == 401 || status == 403 {
            let text = resp.text().await.unwrap_or_default();
            return Err(KimiError {
                status,
                message: if status == 401 {
                    "Unauthorized"
                } else {
                    "Forbidden"
                }
                .to_string(),
                detail: if text.is_empty() { None } else { Some(text) },
                retry_after: None,
            });
        }

        if !resp.status().is_success() {
            let text = resp.text().await.unwrap_or_default();
            return Err(KimiError {
                status,
                message: "Upstream error".to_string(),
                detail: if text.is_empty() { None } else { Some(text) },
                retry_after: None,
            });
        }

        Ok(KimiResponse {
            body: resp,
            status,
            request_start_time,
        })
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
