//! Model ids for the Antigravity `agy` CLI backend.
//!
//! ```text
//! agy                         -> read-only, agy's own default model
//! agy:gemini-3.8-flash-high   -> read-only, that model
//! agy-agent:<model>           -> auto mode, CAN EDIT FILES (gated by allowWrite)
//! ```
//!
//! The prefix is required: agy serves ids such as `claude-sonnet-4-6`, which
//! unprefixed would route to the Anthropic passthrough.

use std::sync::Mutex;
use std::time::{Duration, Instant};

pub const AGY_PREFIXES: &[&str] = &["agy-agent:", "agy:"];
pub const AGY_BARE: &[&str] = &["agy", "agy-agent"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedModel {
    /// `None` leaves the choice to agy's own default.
    pub model: Option<String>,
    pub write: bool,
}

pub fn is_agy_model(model: &str) -> bool {
    AGY_BARE.contains(&model) || AGY_PREFIXES.iter().any(|prefix| model.starts_with(prefix))
}

pub fn parse_model(raw: &str) -> ParsedModel {
    let raw = raw.trim();
    let (write, rest) = if let Some(rest) = raw.strip_prefix("agy-agent:") {
        (true, rest)
    } else if let Some(rest) = raw.strip_prefix("agy:") {
        (false, rest)
    } else {
        (raw == "agy-agent", "")
    };
    let rest = rest.trim();
    ParsedModel {
        model: (!rest.is_empty()).then(|| rest.to_string()),
        write,
    }
}

/// Bare selectors. The write id is listed only once write access is enabled.
pub fn supported_models() -> Vec<String> {
    let mut out = vec!["agy".to_string()];
    if crate::config::agy_allow_write() {
        out.push("agy-agent".to_string());
    }
    out
}

/// Every model the signed-in CLI offers, as a read-only `agy:<id>`.
pub fn discovered_model_ids() -> Vec<String> {
    discover_models()
        .into_iter()
        .map(|model| format!("agy:{model}"))
        .collect()
}

/// `agy models` prints `<id>\t<label>` lines after a progress line.
pub fn parse_model_list(stdout: &str) -> Vec<String> {
    let mut models: Vec<String> = Vec::new();
    for line in stdout.lines() {
        let Some((id, _label)) = line.split_once('\t') else {
            continue;
        };
        let id = id.trim();
        if !id.is_empty()
            && !id.contains(char::is_whitespace)
            && !models.iter().any(|known| known == id)
        {
            models.push(id.to_string());
        }
    }
    models
}

struct Discovery {
    models: Vec<String>,
    expires_at: Instant,
}

static DISCOVERY: Mutex<Option<Discovery>> = Mutex::new(None);
const SUCCESS_TTL: Duration = Duration::from_secs(600);
const FAILURE_TTL: Duration = Duration::from_secs(30);
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(10);

/// `agy models`, cached. Empty when the CLI is missing or signed out; the bare
/// `agy` id is still listed and routes.
pub fn discover_models() -> Vec<String> {
    let mut guard = DISCOVERY
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if let Some(entry) = guard.as_ref()
        && Instant::now() < entry.expires_at
    {
        return entry.models.clone();
    }
    let models = list_models(&super::agy_binary(), DISCOVERY_TIMEOUT).unwrap_or_default();
    let ttl = if models.is_empty() {
        FAILURE_TTL
    } else {
        SUCCESS_TTL
    };
    *guard = Some(Discovery {
        models: models.clone(),
        expires_at: Instant::now() + ttl,
    });
    models
}

fn list_models(binary: &str, timeout: Duration) -> Option<Vec<String>> {
    use std::io::Read;
    use std::process::{Command, Stdio};

    let mut child = Command::new(binary)
        .arg("models")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take().expect("stdout was piped");
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut text = String::new();
        let _ = stdout.read_to_string(&mut text);
        let _ = sender.send(text);
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    if !status.success() {
        return None;
    }
    let text = receiver
        .recv_timeout(deadline.saturating_duration_since(Instant::now()))
        .ok()?;
    Some(parse_model_list(&text))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prefixes_select_mode_and_model() {
        assert_eq!(
            parse_model("agy"),
            ParsedModel {
                model: None,
                write: false
            }
        );
        assert_eq!(
            parse_model("agy:claude-sonnet-4-6"),
            ParsedModel {
                model: Some("claude-sonnet-4-6".into()),
                write: false
            }
        );
        assert_eq!(
            parse_model("agy-agent:gemini-3.8-flash-high"),
            ParsedModel {
                model: Some("gemini-3.8-flash-high".into()),
                write: true
            }
        );
        assert!(parse_model("agy-agent").write);
        assert_eq!(parse_model("agy:").model, None);
    }

    #[test]
    fn only_agy_ids_belong_to_the_backend() {
        for id in ["agy", "agy-agent", "agy:x", "agy-agent:x"] {
            assert!(is_agy_model(id), "{id}");
        }
        for id in [
            "agyx",
            "claude-sonnet-4-6",
            "gemini-3-flash",
            "cursor-cli:agy",
        ] {
            assert!(!is_agy_model(id), "{id}");
        }
    }

    /// Captured from `agy models` (1.5.16): a progress line, then tab-separated rows.
    #[test]
    fn parses_the_real_models_listing() {
        let stdout = "Fetching available models...\n\
            gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n\
            gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n\
            gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n\
            claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n\
            claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n\
            gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n";
        assert_eq!(
            parse_model_list(stdout),
            [
                "gemini-3.8-flash-high",
                "gemini-3.8-flash-medium",
                "gemini-3.8-flash-low",
                "claude-sonnet-4-6",
                "claude-opus-4-6-thinking",
                "gpt-oss-120b-medium"
            ]
        );
        assert!(parse_model_list("Error: Please sign in to view available models.\n").is_empty());
    }
}
