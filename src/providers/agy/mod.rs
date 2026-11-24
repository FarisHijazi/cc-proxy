//! `agy` backend: the locally installed Antigravity CLI, run headless.
//!
//! Same shape as `cursor-cli`: agy runs its own tool loop on its own login, so
//! the proxy holds no credentials, advertises none of Claude Code's tools and
//! never returns `tool_use`. Each turn re-sends the flattened conversation on
//! stdin (`--input-format stream-json`), which also keeps a long history off
//! the argument list.
//!
//! Read-only (`agy:`) passes deny rules for every write and command, which win
//! over any allow rule in the user's agy settings. `agy-agent:` runs agy's
//! `auto` mode and needs `CCP_AGY_ALLOW_WRITE` / `agy.allowWrite` as well.

pub mod events;
pub mod models;

use std::process::Stdio;
use std::time::Duration;

use async_trait::async_trait;
use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

use crate::anthropic::{
    accumulate::accumulate_response,
    error::json_error,
    schema::{CountTokensResponse, MessagesRequest},
};
use crate::monitor::usage_from_anthropic_sse;
use crate::provider::{
    CliHandlers, Generation, GenerationBody, Provider, ProviderError, ProviderErrorKind,
    RequestContext,
};
use crate::providers::cursor_cli::process::{RunningAgent, binary_is_runnable, installed_binary};
use crate::providers::cursor_cli::prompt::render_prompt_for;
use crate::providers::cursor_cli::{collect_agent_sse, stream_agent_response};

use self::events::EventTranslator;
use self::models::{ParsedModel, parse_model};

/// Deny wins over allow in agy's permission engine, so these hold whatever the
/// user's own settings allow.
pub const READ_ONLY_SETTINGS: &str = r#"{"permissions":{"deny":["write_file(*)","command(*)"]}}"#;

/// agy drops the end of a user turn past roughly 50k tokens (a ~230 KB turn
/// lost its last line, ~170 KB arrived whole; agy 1.5.16), and the end is the
/// newest message. So the oldest history goes first.
pub const MAX_PROMPT_CHARS: usize = 150_000;

pub fn keep_newest(prompt: String, limit: usize) -> String {
    let total = prompt.chars().count();
    if total <= limit {
        return prompt;
    }
    let tail: String = prompt.chars().skip(total - limit).collect();
    format!("[earlier conversation omitted]\n{tail}")
}

pub fn agy_binary() -> String {
    installed_binary(crate::config::agy_binary(), "agy")
}

#[derive(Debug, Clone)]
pub struct RunOptions {
    pub binary: String,
    pub parsed: ParsedModel,
    pub workspace: Option<String>,
    pub timeout: Duration,
}

impl RunOptions {
    pub fn from_request(parsed: ParsedModel, req: &MessagesRequest) -> Self {
        Self {
            binary: agy_binary(),
            parsed,
            workspace: crate::config::agy_workspace()
                .or_else(|| crate::providers::cursor_cli::workspace::detect(req)),
            timeout: Duration::from_secs(crate::config::agy_timeout_secs()),
        }
    }

    pub fn args(&self) -> Vec<String> {
        let mut args: Vec<String> = [
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
        ]
        .map(String::from)
        .to_vec();
        if let Some(model) = &self.parsed.model {
            args.extend(["--model".into(), model.clone()]);
        }
        if self.parsed.write {
            args.extend(["--mode".into(), "auto".into()]);
        } else {
            args.extend(["--settings".into(), READ_ONLY_SETTINGS.into()]);
        }
        // `-p` with no prompt of its own reads the turn from stdin; a bare `-p`
        // would swallow the next flag as its prompt.
        args.push("-p=".into());
        args
    }
}

/// The one stdin line agy reads for the turn.
pub fn stdin_turn(prompt: &str) -> String {
    let mut line = serde_json::json!({"event": "user", "message": {"content": prompt}}).to_string();
    line.push('\n');
    line
}

async fn spawn(options: &RunOptions, prompt: &str) -> Result<RunningAgent, String> {
    let mut command = Command::new(&options.binary);
    command
        .args(options.args())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(workspace) = options.workspace.as_deref() {
        command.current_dir(workspace);
    }
    let mut child = command.spawn().map_err(|error| {
        format!(
            "cannot run `{}` ({error}). Install the Antigravity CLI \
             (https://antigravity.google/docs/getting-started), run `agy` once to sign in, \
             or set CCP_AGY_BINARY.",
            options.binary
        )
    })?;
    let mut stdin = child.stdin.take().expect("stdin was piped");
    stdin
        .write_all(stdin_turn(prompt).as_bytes())
        .await
        .map_err(|error| format!("failed to send the prompt to agy: {error}"))?;
    drop(stdin);
    Ok(RunningAgent::from_child(child))
}

fn check_write_permission(parsed: &ParsedModel) -> Result<(), ProviderError> {
    if !parsed.write || crate::config::agy_allow_write() {
        return Ok(());
    }
    Err(ProviderError::new(
        StatusCode::BAD_REQUEST,
        ProviderErrorKind::Permission,
        "agy-agent runs agy in auto mode, which edits files and runs commands. It is disabled \
         by default. Enable it with CCP_AGY_ALLOW_WRITE=1 or `{\"agy\":{\"allowWrite\":true}}` \
         in the proxy config. Use agy: for read-only runs."
            .to_string(),
    ))
}

struct Prepared {
    options: RunOptions,
    prompt: String,
    resolved: String,
}

fn prepare(body: &MessagesRequest) -> Result<Prepared, ProviderError> {
    let parsed = parse_model(body.model.as_deref().unwrap_or("agy"));
    check_write_permission(&parsed)?;
    let prompt = render_prompt_for(body, "agy").map_err(|error| {
        ProviderError::new(
            StatusCode::BAD_REQUEST,
            ProviderErrorKind::InvalidRequest,
            error.to_string(),
        )
    })?;
    let resolved = parsed.model.clone().unwrap_or_else(|| "agy".to_string());
    Ok(Prepared {
        options: RunOptions::from_request(parsed, body),
        prompt: keep_newest(prompt, MAX_PROMPT_CHARS),
        resolved,
    })
}

pub struct AgyProvider;

impl AgyProvider {
    pub fn new() -> Self {
        Self
    }
}

impl Default for AgyProvider {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Provider for AgyProvider {
    /// Installed is enough to list; signed-out runs answer with agy's own error.
    fn availability(&self) -> crate::provider::Availability {
        if binary_is_runnable(&agy_binary()) {
            crate::provider::Availability::Ready
        } else {
            crate::provider::Availability::unavailable(
                "install the Antigravity CLI (https://antigravity.google/docs/getting-started) \
                 and run `agy` once to sign in",
            )
        }
    }

    fn name(&self) -> &'static str {
        "agy"
    }

    fn supported_models(&self) -> Vec<String> {
        models::supported_models()
    }

    fn cli(&self) -> &'static dyn CliHandlers {
        &AGY_CLI
    }

    async fn handle_messages(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        let prepared = match prepare(&body) {
            Ok(prepared) => prepared,
            Err(error) => return json_error(error.status, error.error_type(), error.message),
        };
        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.model_resolved(&ctx.req_id, &prepared.resolved);
        }
        if let Some(traffic) = ctx.traffic.as_ref() {
            traffic.write_bytes("020-agy-prompt.txt", prepared.prompt.as_bytes());
        }
        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.upstream_started(&ctx.req_id);
        }
        let agent = match spawn(&prepared.options, &prepared.prompt).await {
            Ok(agent) => agent,
            Err(error) => return json_error(StatusCode::SERVICE_UNAVAILABLE, "api_error", error),
        };
        let message_id = format!("msg_{}", uuid::Uuid::new_v4().simple());
        let model = body.model.clone().unwrap_or_else(|| "agy".to_string());
        let translator = EventTranslator::new(message_id.clone(), model.clone());
        if body.stream {
            return stream_agent_response(
                agent,
                prepared.options.timeout,
                translator,
                "agy",
                ctx.monitor.clone(),
                ctx.req_id.clone(),
            );
        }
        let sse = match collect_agent_sse(agent, prepared.options.timeout, translator, "agy").await
        {
            Ok(sse) => sse,
            Err(error) => return json_error(StatusCode::BAD_GATEWAY, "api_error", error),
        };
        match accumulate_response(&sse, &message_id, &model) {
            Ok(value) => {
                if let Some(monitor) = ctx.monitor.as_ref() {
                    let (input, output) = usage_from_anthropic_sse(&sse);
                    monitor.usage_updated(&ctx.req_id, input, output);
                }
                (StatusCode::OK, Json(value)).into_response()
            }
            Err(error) => json_error(
                StatusCode::BAD_GATEWAY,
                "api_error",
                format!("Accumulation error: {error}"),
            ),
        }
    }

    async fn handle_count_tokens(&self, body: MessagesRequest, ctx: RequestContext) -> Response {
        if let Err(error) = render_prompt_for(&body, "agy") {
            return json_error(
                StatusCode::BAD_REQUEST,
                "invalid_request_error",
                error.to_string(),
            );
        }
        let tokens = crate::providers::kimi::count_tokens::count_tokens(&body);
        if let Some(monitor) = ctx.monitor.as_ref() {
            monitor.usage_updated(&ctx.req_id, Some(tokens), None);
        }
        (
            StatusCode::OK,
            Json(CountTokensResponse {
                input_tokens: tokens,
            }),
        )
            .into_response()
    }

    async fn generate_anthropic_stream(
        &self,
        body: MessagesRequest,
        ctx: RequestContext,
    ) -> Result<Generation, ProviderError> {
        let prepared = prepare(&body)?;
        let agent = spawn(&prepared.options, &prepared.prompt)
            .await
            .map_err(|error| {
                ProviderError::new(
                    StatusCode::SERVICE_UNAVAILABLE,
                    ProviderErrorKind::Api,
                    error,
                )
            })?;
        let message_id = format!("msg_{}", uuid::Uuid::new_v4().simple());
        let model = body.model.as_deref().unwrap_or("agy");
        let translator = EventTranslator::new(message_id, model);
        let sse = collect_agent_sse(agent, prepared.options.timeout, translator, "agy")
            .await
            .map_err(|error| {
                ProviderError::new(StatusCode::BAD_GATEWAY, ProviderErrorKind::Api, error)
            })?;
        if let Some(monitor) = ctx.monitor.as_ref() {
            let (input, output) = usage_from_anthropic_sse(&sse);
            monitor.stream_progress(&ctx.req_id, sse.len() as u64, 1, input, output);
        }
        Ok(Generation {
            body: GenerationBody::BufferedSse(sse.into()),
            resolved_model: prepared.resolved,
        })
    }
}

pub(crate) struct AgyCli;

impl CliHandlers for AgyCli {
    fn login(&self) -> anyhow::Result<()> {
        anyhow::bail!(
            "agy reuses the Antigravity CLI's own login. Run `agy` once and sign in, \
             then `claude-codex agy auth status`."
        )
    }

    fn device(&self) -> anyhow::Result<()> {
        self.login()
    }

    fn status(&self) -> anyhow::Result<()> {
        let binary = agy_binary();
        println!("Binary: {binary}");
        println!(
            "Write access: {}",
            if crate::config::agy_allow_write() {
                "enabled (agy-agent: may edit files and run commands)"
            } else {
                "disabled (read-only; set CCP_AGY_ALLOW_WRITE=1 to allow)"
            }
        );
        let discovered = models::discover_models();
        if discovered.is_empty() {
            anyhow::bail!("`{binary} models` listed nothing: install agy and run `agy` to sign in");
        }
        println!("Models: use any as agy:<model>");
        for model in discovered {
            println!("  {model}");
        }
        Ok(())
    }

    fn logout(&self) -> anyhow::Result<()> {
        println!("agy stores no credentials in the proxy; run /logout inside `agy` instead.");
        Ok(())
    }
}

pub(crate) static AGY_CLI: AgyCli = AgyCli;

#[cfg(test)]
mod tests {
    use super::*;

    fn options(model: &str) -> RunOptions {
        RunOptions {
            binary: "agy".into(),
            parsed: parse_model(model),
            workspace: None,
            timeout: Duration::from_secs(60),
        }
    }

    fn flag_value(args: &[String], flag: &str) -> Option<String> {
        let index = args.iter().position(|arg| arg == flag)?;
        args.get(index + 1).cloned()
    }

    /// The safety-critical assertion: a read-only run carries the deny rules and
    /// none of the flags that grant writes.
    #[test]
    fn read_only_runs_deny_writes_and_commands() {
        for model in ["agy", "agy:gemini-3.8-flash-high"] {
            let args = options(model).args();
            assert_eq!(
                flag_value(&args, "--settings").as_deref(),
                Some(READ_ONLY_SETTINGS)
            );
            assert!(!args.contains(&"--mode".to_string()), "{args:?}");
            assert!(
                !args.iter().any(|arg| arg.contains("dangerously")),
                "{args:?}"
            );
        }
        let settings: serde_json::Value = serde_json::from_str(READ_ONLY_SETTINGS).unwrap();
        let deny = settings["permissions"]["deny"].as_array().unwrap();
        for rule in ["write_file(*)", "command(*)"] {
            assert!(deny.contains(&serde_json::json!(rule)), "{rule}");
        }
    }

    #[test]
    fn agent_runs_use_auto_mode_without_deny_rules() {
        let args = options("agy-agent:gemini-3.8-flash-high").args();
        assert_eq!(flag_value(&args, "--mode").as_deref(), Some("auto"));
        assert!(!args.contains(&"--settings".to_string()));
    }

    #[test]
    fn prompt_goes_on_stdin_and_p_takes_no_value() {
        let args = options("agy:claude-sonnet-4-6").args();
        assert_eq!(args.last().map(String::as_str), Some("-p="));
        assert_eq!(
            flag_value(&args, "--input-format").as_deref(),
            Some("stream-json")
        );
        assert_eq!(
            flag_value(&args, "--model").as_deref(),
            Some("claude-sonnet-4-6")
        );
        assert!(!options("agy").args().contains(&"--model".to_string()));
        let line = stdin_turn("line one\nline \"two\"");
        assert!(line.ends_with('\n') && line.matches('\n').count() == 1);
        let parsed: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
        assert_eq!(parsed["event"], "user");
        assert_eq!(parsed["message"]["content"], "line one\nline \"two\"");
    }

    #[test]
    fn an_oversized_prompt_keeps_its_newest_part() {
        assert_eq!(keep_newest("short".into(), 10), "short");
        let kept = keep_newest("old old old é new question".into(), 14);
        assert_eq!(kept, "[earlier conversation omitted]\né new question");
    }

    #[test]
    fn write_mode_is_refused_without_opt_in() {
        if crate::config::agy_allow_write() {
            return;
        }
        let error = check_write_permission(&parse_model("agy-agent:x")).expect_err("gated");
        assert!(error.message.contains("CCP_AGY_ALLOW_WRITE"));
        assert!(check_write_permission(&parse_model("agy:x")).is_ok());
    }
}
