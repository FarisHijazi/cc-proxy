use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::model_allowlist::{KIMI_DEFAULT_MODEL, assert_allowed_model, is_k3, resolve_model};
use crate::anthropic::schema::MessagesRequest;
use crate::providers::translate_shared::{
    ContentBlock, flatten_system_text, image_block_to_url, image_source_to_url, normalize_content,
    parallel_tool_calls, read_effort,
};

// ---------------------------------------------------------------------------
// Kimi OpenAI-compatible chat-completions types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KimiChatRequest {
    pub model: String,
    pub messages: Vec<KimiMessage>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<KimiTool>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_choice: Option<KimiToolChoice>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parallel_tool_calls: Option<bool>,
    pub stream: bool,
    pub stream_options: KimiStreamOptions,
    pub max_tokens: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning_effort: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thinking: Option<KimiThinking>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prompt_cache_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub top_p: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub response_format: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KimiStreamOptions {
    pub include_usage: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KimiThinking {
    #[serde(rename = "type")]
    pub kind: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(into = "Value", try_from = "Value")]
pub enum KimiToolChoice {
    Auto,
    None,
    Required,
    Function {
        #[serde(rename = "type")]
        kind: String,
        function: KimiToolChoiceFunction,
    },
}

impl From<KimiToolChoice> for Value {
    fn from(choice: KimiToolChoice) -> Value {
        match choice {
            KimiToolChoice::Auto => Value::String("auto".into()),
            KimiToolChoice::None => Value::String("none".into()),
            KimiToolChoice::Required => Value::String("required".into()),
            KimiToolChoice::Function { kind, function } => {
                serde_json::json!({"type":kind, "function":{"name":function.name}})
            }
        }
    }
}

impl TryFrom<Value> for KimiToolChoice {
    type Error = String;
    fn try_from(value: Value) -> Result<Self, Self::Error> {
        match value.as_str() {
            Some("auto") => Ok(Self::Auto),
            Some("none") => Ok(Self::None),
            Some("required") => Ok(Self::Required),
            _ if value["type"] == "function" => value["function"]["name"]
                .as_str()
                .filter(|name| !name.is_empty())
                .map(|name| Self::Function {
                    kind: "function".into(),
                    function: KimiToolChoiceFunction { name: name.into() },
                })
                .ok_or_else(|| "named tool choice requires a name".into()),
            _ => Err("invalid Kimi tool_choice".into()),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KimiToolChoiceFunction {
    pub name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum KimiMessage {
    System {
        role: String,
        content: String,
    },
    User {
        role: String,
        content: serde_json::Value,
    },
    Assistant {
        role: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        content: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        reasoning_content: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tool_calls: Option<Vec<KimiAssistantToolCall>>,
    },
    Tool {
        role: String,
        tool_call_id: String,
        content: serde_json::Value,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KimiAssistantToolCall {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub function: KimiToolCallFunction,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KimiToolCallFunction {
    pub name: String,
    pub arguments: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KimiTool {
    #[serde(rename = "type")]
    pub kind: String,
    pub function: KimiToolFunction,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KimiToolFunction {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    pub parameters: serde_json::Value,
}

pub struct TranslateOptions {
    pub session_id: Option<String>,
}

const DEFAULT_MAX_TOKENS: u32 = 32000;

// ---------------------------------------------------------------------------
// Translation entry point
// ---------------------------------------------------------------------------

pub fn translate_request(
    req: &MessagesRequest,
    opts: TranslateOptions,
) -> Result<KimiChatRequest, anyhow::Error> {
    let model = req.model.as_deref().unwrap_or(KIMI_DEFAULT_MODEL);
    let resolved = resolve_model(model);
    assert_allowed_model(&resolved).map_err(|e| anyhow::anyhow!("{e}"))?;

    let k3 = is_k3(&resolved);
    validate_request(req)?;
    let thinking = read_thinking(req, k3)?;
    let messages = build_messages(req, &resolved)?;
    let tools = read_tools(req)?;
    let tool_choice = read_tool_choice(req)?;

    let mut out = KimiChatRequest {
        model: resolved,
        messages,
        stream: true,
        stream_options: KimiStreamOptions {
            include_usage: true,
        },
        max_tokens: clamp_max_tokens(req.max_tokens, k3),
        reasoning_effort: Some(map_reasoning_effort(read_effort(req)?, k3)),
        thinking,
        tools: if tools.is_empty() { None } else { Some(tools) },
        tool_choice,
        parallel_tool_calls: parallel_tool_calls(req),
        prompt_cache_key: opts.session_id,
        temperature: read_sampling(req, "temperature", 2.0)?,
        top_p: read_sampling(req, "top_p", 1.0)?,
        stop: read_stop(req)?,
        response_format: read_response_format(req)?,
    };

    if out
        .thinking
        .as_ref()
        .is_some_and(|thinking| thinking.kind == "disabled")
    {
        out.reasoning_effort = None;
    }
    if let Some(KimiToolChoice::Function { function, .. }) = &out.tool_choice {
        anyhow::ensure!(
            out.tools
                .as_ref()
                .is_some_and(|tools| tools.iter().any(|tool| tool.function.name == function.name)),
            "tool_choice names an unregistered tool"
        );
    }
    // Moonshot rejects a named tool_choice while thinking is on; `required` with only that tool forces the same call.
    if out.reasoning_effort.is_some()
        && let Some(KimiToolChoice::Function { function, .. }) = &out.tool_choice
    {
        let name = function.name.clone();
        if let Some(tools) = out.tools.as_mut() {
            tools.retain(|tool| tool.function.name == name);
        }
        out.tool_choice = Some(KimiToolChoice::Required);
    }
    // Collapse auto tool_choice to None (default behavior)
    if matches!(out.tool_choice, Some(KimiToolChoice::Auto)) {
        out.tool_choice = None;
    }

    Ok(out)
}

fn read_sampling(req: &MessagesRequest, key: &str, max: f64) -> Result<Option<f64>, anyhow::Error> {
    req.extra
        .get(key)
        .map(|value| {
            let number = value
                .as_f64()
                .ok_or_else(|| anyhow::anyhow!("{key} must be numeric"))?;
            anyhow::ensure!(
                (0.0..=max).contains(&number),
                "{key} must be between 0 and {max}"
            );
            Ok(number)
        })
        .transpose()
}

fn read_stop(req: &MessagesRequest) -> Result<Option<Vec<String>>, anyhow::Error> {
    req.extra
        .get("stop_sequences")
        .map(|value| {
            let items = value
                .as_array()
                .ok_or_else(|| anyhow::anyhow!("stop_sequences must be an array"))?;
            anyhow::ensure!(
                items.len() <= 5,
                "Kimi supports at most five stop sequences"
            );
            items
                .iter()
                .map(|item| {
                    let stop = item
                        .as_str()
                        .ok_or_else(|| anyhow::anyhow!("stop sequence must be a string"))?;
                    anyhow::ensure!(
                        !stop.is_empty() && stop.len() <= 32,
                        "Kimi stop sequences must contain 1..32 bytes"
                    );
                    Ok(stop.to_string())
                })
                .collect()
        })
        .transpose()
}

fn read_response_format(req: &MessagesRequest) -> Result<Option<Value>, anyhow::Error> {
    let format = req
        .extra
        .get("output_config")
        .and_then(|config| config.get("format"))
        .or_else(|| req.extra.get("output_format"));
    format.map(|format| {
        anyhow::ensure!(format["type"] == "json_schema" && format["schema"].is_object(), "Kimi structured output requires a json_schema object");
        Ok(serde_json::json!({"type":"json_schema", "json_schema":{"name":"response", "schema":format["schema"]}}))
    }).transpose()
}

fn read_thinking(req: &MessagesRequest, k3: bool) -> Result<Option<KimiThinking>, anyhow::Error> {
    let mode = req
        .extra
        .get("thinking")
        .map(|thinking| {
            thinking
                .get("type")
                .and_then(Value::as_str)
                .ok_or_else(|| anyhow::anyhow!("thinking requires type"))
        })
        .transpose()?;
    anyhow::ensure!(
        mode.is_none() || matches!(mode, Some("enabled" | "disabled" | "adaptive")),
        "unsupported thinking type"
    );
    anyhow::ensure!(
        !k3 || mode != Some("disabled"),
        "K3 cannot disable thinking; use output_config.effort=low to reduce reasoning"
    );
    Ok((!k3).then(|| KimiThinking {
        kind: if mode == Some("disabled") {
            "disabled"
        } else {
            "enabled"
        }
        .into(),
    }))
}

fn validate_request(req: &MessagesRequest) -> Result<(), anyhow::Error> {
    for key in ["top_k", "mcp_servers", "container"] {
        anyhow::ensure!(!req.extra.contains_key(key), "Kimi does not support {key}");
    }
    if let Some(system) = req.extra.get("system") {
        anyhow::ensure!(
            system.is_string()
                || system.as_array().is_some_and(|blocks| blocks
                    .iter()
                    .all(|block| block["type"] == "text" && block["text"].is_string())),
            "Kimi system content must contain text only"
        );
    }
    for message in &req.messages {
        let Some(blocks) = message.content.as_array() else {
            anyhow::ensure!(
                message.content.is_string(),
                "message content must be text or an array"
            );
            continue;
        };
        for block in blocks {
            let kind = block.get("type").and_then(Value::as_str).unwrap_or("");
            let supported = match message.role.as_str() {
                "assistant" => matches!(kind, "text" | "thinking" | "tool_use"),
                "user" => matches!(kind, "text" | "image" | "tool_result"),
                "system" | "developer" => kind == "text",
                _ => false,
            };
            anyhow::ensure!(
                supported,
                "Kimi does not support {kind} content in {} messages",
                message.role
            );
            if kind == "tool_result" {
                if let Some(parts) = block.get("content").and_then(Value::as_array) {
                    for part in parts {
                        let kind = part["type"].as_str().unwrap_or("");
                        anyhow::ensure!(
                            matches!(kind, "text" | "image" | "tool_reference"),
                            "Kimi does not support tool result content {kind}"
                        );
                        if kind == "tool_reference" {
                            let name = part["tool_name"]
                                .as_str()
                                .filter(|name| !name.is_empty())
                                .ok_or_else(|| {
                                anyhow::anyhow!("tool_reference requires tool_name")
                            })?;
                            anyhow::ensure!(
                                req.extra
                                    .get("tools")
                                    .and_then(Value::as_array)
                                    .is_some_and(|tools| tools
                                        .iter()
                                        .any(|tool| tool["name"] == name)),
                                "tool_reference has no registered function schema: {name}"
                            );
                        }
                    }
                } else {
                    anyhow::ensure!(
                        block.get("content").is_none_or(Value::is_string),
                        "tool result content must be text or an array"
                    );
                }
            }
        }
    }
    Ok(())
}

fn clamp_max_tokens(requested: Option<u32>, k3: bool) -> u32 {
    if k3 {
        // K3's context is 1M tokens. Kimi Code passes max_context_size as
        // max_tokens and lets the server clamp to the remaining output budget.
        // Pass through the requested value (or the full context window).
        requested.filter(|v| *v > 0).unwrap_or(1_048_576)
    } else {
        match requested {
            Some(v) if v > 0 => v.min(DEFAULT_MAX_TOKENS),
            _ => DEFAULT_MAX_TOKENS,
        }
    }
}

fn map_reasoning_effort(effort: Option<&str>, k3: bool) -> String {
    if k3 {
        // k3 supports: low, high, max (default: high)
        match effort {
            Some("max") => "max".to_string(),
            Some("xhigh" | "high") => "high".to_string(),
            Some("low") => "low".to_string(),
            _ => "high".to_string(),
        }
    } else {
        match effort {
            Some("max" | "xhigh") => "high".to_string(),
            Some(v) => v.to_string(),
            None => "medium".to_string(),
        }
    }
}

// ---------------------------------------------------------------------------
// Tool & tool_choice reading
// ---------------------------------------------------------------------------

fn map_tool_choice(choice: &serde_json::Map<String, Value>) -> KimiToolChoice {
    match choice.get("type").and_then(|v| v.as_str()) {
        Some("auto") => KimiToolChoice::Auto,
        Some("none") => KimiToolChoice::None,
        Some("any") => KimiToolChoice::Required,
        Some("tool") => {
            if let Some(name) = choice.get("name").and_then(|v| v.as_str()) {
                KimiToolChoice::Function {
                    kind: "function".to_string(),
                    function: KimiToolChoiceFunction {
                        name: name.to_string(),
                    },
                }
            } else {
                KimiToolChoice::Required
            }
        }
        _ => KimiToolChoice::Auto,
    }
}

fn read_tool_choice(req: &MessagesRequest) -> Result<Option<KimiToolChoice>, anyhow::Error> {
    match req.extra.get("tool_choice") {
        Some(Value::Object(choice)) => {
            anyhow::ensure!(
                matches!(
                    choice.get("type").and_then(Value::as_str),
                    Some("auto" | "none" | "any" | "tool")
                ),
                "unsupported tool_choice type"
            );
            if choice.get("type").and_then(Value::as_str) == Some("tool") {
                anyhow::ensure!(
                    choice
                        .get("name")
                        .and_then(Value::as_str)
                        .is_some_and(|name| !name.is_empty()),
                    "named tool_choice requires name"
                );
            }
            Ok(Some(map_tool_choice(choice)))
        }
        Some(Value::String(s)) => Ok(Some(match s.as_str() {
            "auto" => KimiToolChoice::Auto,
            "none" => KimiToolChoice::None,
            "any" | "required" => KimiToolChoice::Required,
            _ => anyhow::bail!("unsupported tool_choice: {s}"),
        })),
        None => Ok(None),
        _ => anyhow::bail!("tool_choice must be an object or string"),
    }
}

fn read_tools(req: &MessagesRequest) -> Result<Vec<KimiTool>, anyhow::Error> {
    let Some(tools) = req.extra.get("tools") else {
        return Ok(Vec::new());
    };
    let tools_arr = match tools {
        Value::Array(a) => a,
        _ => anyhow::bail!("tools must be an array"),
    };
    let mut out = Vec::new();
    for tool in tools_arr {
        let kind = tool.get("type").and_then(Value::as_str).unwrap_or("custom");
        anyhow::ensure!(
            matches!(kind, "custom" | "function"),
            "Kimi does not support hosted tool type {kind}; use a client-executed function tool"
        );
        let name = tool
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        anyhow::ensure!(!name.is_empty(), "function tool requires name");
        let description = tool
            .get("description")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let parameters = tool
            .get("input_schema")
            .cloned()
            .unwrap_or(serde_json::json!({}));
        out.push(KimiTool {
            kind: "function".to_string(),
            function: KimiToolFunction {
                name,
                description,
                parameters,
            },
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Message building
// ---------------------------------------------------------------------------

fn build_messages(req: &MessagesRequest, model: &str) -> Result<Vec<KimiMessage>, anyhow::Error> {
    let mut out: Vec<KimiMessage> = Vec::new();
    let k3_mode = is_k3(model);

    // For k3: collect system text and prepend to first user message
    // For other models: emit as system role
    let mut k3_system_parts: Vec<String> = Vec::new();

    // System field
    if let Some(system) = flatten_system_text(req.extra.get("system")) {
        if k3_mode {
            k3_system_parts.push(system);
        } else {
            out.push(KimiMessage::System {
                role: "system".to_string(),
                content: system,
            });
        }
    }

    // Convert each message
    for msg in &req.messages {
        let blocks = normalize_content(&msg.content, serde_json::json!({}));
        match msg.role.as_str() {
            "user" => {
                if k3_mode && !k3_system_parts.is_empty() {
                    let system_prefix = k3_system_parts.join("\n\n");
                    k3_system_parts.clear();

                    // Emit tool results as tool-role messages
                    for block in &blocks {
                        if let ContentBlock::ToolResult {
                            tool_use_id,
                            content,
                            is_error,
                        } = block
                        {
                            out.push(KimiMessage::Tool {
                                role: "tool".to_string(),
                                tool_call_id: tool_use_id.clone(),
                                content: tool_result_content(content, *is_error),
                            });
                        }
                    }

                    // Build user content from text + image blocks, prepending system text
                    let has_text = blocks
                        .iter()
                        .any(|b| matches!(b, ContentBlock::Text { .. }));
                    let has_images = blocks
                        .iter()
                        .any(|b| matches!(b, ContentBlock::Image { .. }));

                    if has_images {
                        // Mixed content: build array with system prefix as first text part
                        let mut parts: Vec<KimiUserContentPart> = Vec::new();
                        parts.push(KimiUserContentPart::Text {
                            text: system_prefix,
                        });
                        for block in &blocks {
                            match block {
                                ContentBlock::Text { text } if !text.is_empty() => {
                                    parts.push(KimiUserContentPart::Text { text: text.clone() });
                                }
                                ContentBlock::Image { source } => {
                                    parts.push(KimiUserContentPart::ImageUrl {
                                        image_url: KimiImageUrl {
                                            url: image_source_to_url(source),
                                        },
                                    });
                                }
                                _ => {}
                            }
                        }
                        out.push(KimiMessage::User {
                            role: "user".to_string(),
                            content: serde_json::to_value(parts).unwrap_or_default(),
                        });
                    } else if has_text {
                        let user_text: String = blocks
                            .iter()
                            .filter_map(|b| match b {
                                ContentBlock::Text { text } => Some(text.as_str()),
                                _ => None,
                            })
                            .collect::<Vec<_>>()
                            .join("");
                        let merged = format!("{system_prefix}\n\n{user_text}");
                        out.push(KimiMessage::User {
                            role: "user".to_string(),
                            content: serde_json::Value::String(merged),
                        });
                    } else {
                        // Tool-result-only turn: emit system text as its own user message
                        out.push(KimiMessage::User {
                            role: "user".to_string(),
                            content: serde_json::Value::String(system_prefix),
                        });
                    }
                } else {
                    push_user_messages(&mut out, &blocks);
                }
            }
            "assistant" => push_assistant_message(&mut out, &blocks),
            "system" | "developer" => {
                let text = blocks
                    .iter()
                    .filter_map(|b| match b {
                        ContentBlock::Text { text } => Some(text.as_str()),
                        _ => None,
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                if !text.is_empty() {
                    if k3_mode {
                        k3_system_parts.push(text);
                    } else {
                        out.push(KimiMessage::System {
                            role: "system".to_string(),
                            content: text,
                        });
                    }
                }
            }
            other => {
                anyhow::bail!("unexpected message role: {other}");
            }
        }
    }

    // Flush any remaining system text (e.g. system-only conversation or trailing system messages)
    if k3_mode && !k3_system_parts.is_empty() {
        let text = k3_system_parts.join("\n\n");
        out.push(KimiMessage::User {
            role: "user".to_string(),
            content: serde_json::Value::String(text),
        });
    }

    Ok(out)
}

fn push_user_messages(out: &mut Vec<KimiMessage>, blocks: &[ContentBlock]) {
    let mut buffer: Vec<KimiUserContentPart> = Vec::new();

    let flush_buffer = |out: &mut Vec<KimiMessage>, buffer: &mut Vec<KimiUserContentPart>| {
        if buffer.is_empty() {
            return;
        }
        let all_text = buffer
            .iter()
            .all(|p| matches!(p, KimiUserContentPart::Text { .. }));
        if all_text {
            let joined: String = buffer
                .iter()
                .map(|p| match p {
                    KimiUserContentPart::Text { text } => text.as_str(),
                    _ => "",
                })
                .collect();
            out.push(KimiMessage::User {
                role: "user".to_string(),
                content: Value::String(joined),
            });
        } else {
            let parts: Vec<KimiUserContentPart> = std::mem::take(buffer);
            out.push(KimiMessage::User {
                role: "user".to_string(),
                content: serde_json::to_value(parts).unwrap_or_default(),
            });
            return;
        }
        buffer.clear();
    };

    for block in blocks {
        match block {
            ContentBlock::Text { text } => {
                buffer.push(KimiUserContentPart::Text { text: text.clone() });
            }
            ContentBlock::Image { source } => {
                buffer.push(KimiUserContentPart::ImageUrl {
                    image_url: KimiImageUrl {
                        url: image_source_to_url(source),
                    },
                });
            }
            ContentBlock::ToolResult {
                tool_use_id,
                content,
                is_error,
            } => {
                // flush any buffered user content first
                let mut temp = Vec::new();
                std::mem::swap(&mut buffer, &mut temp);
                flush_buffer(out, &mut temp);

                out.push(KimiMessage::Tool {
                    role: "tool".to_string(),
                    tool_call_id: tool_use_id.clone(),
                    content: tool_result_content(content, *is_error),
                });
            }
            _ => {}
        }
    }

    // flush remaining buffer
    flush_buffer(out, &mut buffer);
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum KimiUserContentPart {
    Text { text: String },
    ImageUrl { image_url: KimiImageUrl },
}

#[derive(Debug, Clone, Serialize)]
struct KimiImageUrl {
    url: String,
}

fn tool_result_content(content: &Value, is_error: Option<bool>) -> Value {
    let prefix = if is_error.unwrap_or(false) {
        "[tool execution error]\n"
    } else {
        ""
    };

    match content {
        Value::String(s) => Value::String(format!("{prefix}{s}")),
        Value::Array(arr) => {
            let mut parts: Vec<KimiToolResultPart> = Vec::new();
            if !prefix.is_empty() {
                parts.push(KimiToolResultPart::Text {
                    text: prefix.to_string(),
                });
            }
            for b in arr {
                match b.get("type").and_then(|v| v.as_str()) {
                    Some("text") => {
                        let text = b.get("text").and_then(|v| v.as_str()).unwrap_or("");
                        parts.push(KimiToolResultPart::Text {
                            text: text.to_string(),
                        });
                    }
                    Some("image") => {
                        let url = image_block_to_url(b);
                        parts.push(KimiToolResultPart::ImageUrl {
                            image_url: KimiImageUrl { url },
                        });
                    }
                    Some("tool_reference") => {
                        let name = b["tool_name"].as_str().unwrap_or("");
                        parts.push(KimiToolResultPart::Text {
                            text: format!(
                                "Tool available: {name}. Use its registered function schema."
                            ),
                        });
                    }
                    Some(_) => unreachable!("tool result validated before translation"),
                    None => {}
                }
            }

            // Collapse to string when only one text part
            if parts.len() == 1
                && let KimiToolResultPart::Text { text } = &parts[0]
            {
                return Value::String(text.clone());
            }

            serde_json::to_value(parts).unwrap_or(Value::String(prefix.to_string()))
        }
        _ => Value::String(prefix.to_string()),
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum KimiToolResultPart {
    Text { text: String },
    ImageUrl { image_url: KimiImageUrl },
}

fn push_assistant_message(out: &mut Vec<KimiMessage>, blocks: &[ContentBlock]) {
    let mut text_parts: Vec<String> = Vec::new();
    let mut thinking_parts: Vec<String> = Vec::new();
    let mut tool_calls: Vec<KimiAssistantToolCall> = Vec::new();

    for block in blocks {
        match block {
            ContentBlock::Text { text } => {
                if !text.is_empty() {
                    text_parts.push(text.clone());
                }
            }
            ContentBlock::Thinking {
                thinking,
                signature,
            } => {
                if !thinking.is_empty() {
                    if signature
                        .as_deref()
                        .and_then(crate::providers::translate_shared::foreign_reasoning_provider)
                        == Some("kimi")
                    {
                        thinking_parts.push(thinking.clone());
                    } else {
                        text_parts
                            .push(crate::providers::translate_shared::wrap_reasoning(thinking));
                    }
                }
            }
            ContentBlock::ToolUse { id, name, input } => {
                let args = serde_json::to_string(input).unwrap_or_else(|_| "{}".to_string());
                tool_calls.push(KimiAssistantToolCall {
                    id: id.clone(),
                    kind: "function".to_string(),
                    function: KimiToolCallFunction {
                        name: name.clone(),
                        arguments: args,
                    },
                });
            }
            // Image blocks from assistant are dropped
            _ => {}
        }
    }

    if text_parts.is_empty() && tool_calls.is_empty() && thinking_parts.is_empty() {
        return;
    }

    let content = if text_parts.is_empty() {
        Some(String::new())
    } else {
        Some(text_parts.join(""))
    };

    let reasoning_content = if thinking_parts.is_empty() {
        None
    } else {
        Some(thinking_parts.join("\n\n"))
    };

    let tool_calls_val = if tool_calls.is_empty() {
        None
    } else {
        Some(tool_calls)
    };

    out.push(KimiMessage::Assistant {
        role: "assistant".to_string(),
        content,
        reasoning_content,
        tool_calls: tool_calls_val,
    });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn translate_text_request_defaults_like_reference() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "haiku",
            "max_tokens": 10,
            "system": "sys",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name":"search","description":"Search","input_schema":{"type":"object"}}],
            "tool_choice": {"type":"tool", "name":"search"},
            "output_config": {"effort":"max"}
        }))
        .unwrap();
        let translated = translate_request(
            &req,
            TranslateOptions {
                session_id: Some("sid".into()),
            },
        )
        .unwrap();
        assert_eq!(translated.model, "kimi-for-coding");
        assert_eq!(translated.reasoning_effort.as_deref(), Some("high"));
        assert_eq!(translated.prompt_cache_key.as_deref(), Some("sid"));
        assert_eq!(translated.max_tokens, 10);
    }

    #[test]
    fn translate_tool_result_with_unsupported_blocks() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [
                        {"type": "text", "text": "visible output"},
                        {"type": "thinking", "thinking": "hidden thought"}
                    ]
                }]
            }]
        }))
        .unwrap();
        let error = translate_request(&req, TranslateOptions { session_id: None }).unwrap_err();
        assert!(error.to_string().contains("tool result content thinking"));
    }

    #[test]
    fn translate_tool_result_with_image() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [
                        {"type": "text", "text": "caption"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}
                    ]
                }]
            }]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.messages.len(), 1);
        match &translated.messages[0] {
            KimiMessage::Tool {
                role,
                tool_call_id,
                content,
            } => {
                assert_eq!(role, "tool");
                assert_eq!(tool_call_id, "toolu_1");
                let parts: Vec<&Value> = match content {
                    Value::Array(a) => a.iter().collect(),
                    _ => panic!("expected array"),
                };
                assert_eq!(parts.len(), 2);
                assert_eq!(
                    parts[1]
                        .get("image_url")
                        .and_then(|u| u.get("url"))
                        .and_then(|v| v.as_str()),
                    Some("data:image/png;base64,abc")
                );
            }
            _ => panic!("expected Tool message"),
        }
    }

    #[test]
    fn translate_assistant_with_thinking_tool_use_and_text() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me think..."},
                    {"type": "text", "text": "here's the answer"},
                    {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "rust"}}
                ]
            }]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.messages.len(), 1);
        match &translated.messages[0] {
            KimiMessage::Assistant {
                role,
                content,
                reasoning_content,
                tool_calls,
            } => {
                assert_eq!(role, "assistant");
                assert_eq!(
                    content.as_deref(),
                    Some(
                        "<previous_reasoning>\nlet me think...\n</previous_reasoning>here's the answer"
                    )
                );
                assert!(reasoning_content.is_none());
                assert!(tool_calls.is_some());
                let tcs = tool_calls.as_ref().unwrap();
                assert_eq!(tcs.len(), 1);
                assert_eq!(tcs[0].function.name, "search");
            }
            _ => panic!("expected Assistant message"),
        }
    }

    #[test]
    fn translate_empty_assistant_content_emits_empty_string() {
        // When there are no text blocks and no tool calls but there is thinking
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "thinking..."}
                ]
            }]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.messages.len(), 1);
        match &translated.messages[0] {
            KimiMessage::Assistant {
                content,
                reasoning_content,
                ..
            } => {
                assert_eq!(
                    content.as_deref(),
                    Some("<previous_reasoning>\nthinking...\n</previous_reasoning>")
                );
                assert!(reasoning_content.is_none());
            }
            _ => panic!("expected Assistant message"),
        }
    }

    #[test]
    fn translate_user_text_and_image_collapse_correctly() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "xyz"}}
                ]
            }]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.messages.len(), 1);
        match &translated.messages[0] {
            KimiMessage::User { role, content } => {
                assert_eq!(role, "user");
                // Mixed text+image produces array, not string
                assert!(content.is_array());
                let parts = content.as_array().unwrap();
                assert_eq!(parts.len(), 2);
                assert_eq!(
                    parts[0].get("text").and_then(|v| v.as_str()),
                    Some("describe this")
                );
                assert!(parts[1].get("image_url").is_some());
            }
            _ => panic!("expected User message"),
        }
    }

    #[test]
    fn user_content_parts_carry_a_type_discriminant() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "xyz"}}
                ]
            }]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        match &translated.messages[0] {
            KimiMessage::User { content, .. } => {
                let parts = content.as_array().unwrap();
                assert_eq!(parts[0].get("type").and_then(|v| v.as_str()), Some("text"));
                assert_eq!(
                    parts[1].get("type").and_then(|v| v.as_str()),
                    Some("image_url")
                );
            }
            _ => panic!("expected User message"),
        }
    }

    #[test]
    fn tool_result_parts_carry_a_type_discriminant() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [
                        {"type": "text", "text": "caption"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}
                    ]
                }]
            }]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        match &translated.messages[0] {
            KimiMessage::Tool { content, .. } => {
                let parts = content.as_array().unwrap();
                assert_eq!(parts[0].get("type").and_then(|v| v.as_str()), Some("text"));
                assert_eq!(
                    parts[1].get("type").and_then(|v| v.as_str()),
                    Some("image_url")
                );
            }
            _ => panic!("expected Tool message"),
        }
    }

    #[test]
    fn translate_text_only_user_collapses_to_string() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{
                "role": "user",
                "content": "hello"
            }]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        match &translated.messages[0] {
            KimiMessage::User { role, content } => {
                assert_eq!(role, "user");
                assert_eq!(content.as_str(), Some("hello"));
            }
            _ => panic!("expected User message"),
        }
    }

    #[test]
    fn max_tokens_defaults_to_32000() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.max_tokens, 32000);
    }

    #[test]
    fn max_tokens_clamps_at_32000() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "max_tokens": 99999,
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.max_tokens, 32000);
    }

    #[test]
    fn invalid_effort_rejected() {
        let req: Result<MessagesRequest, _> = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "extreme"}
        }));
        // The serde flatten extra captures it, so it parses. Error comes at translate time.
        let req = req.unwrap();
        let result = translate_request(&req, TranslateOptions { session_id: None });
        assert!(result.is_err());
    }

    #[test]
    fn effort_xhigh_maps_to_high() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "xhigh"}
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.reasoning_effort.as_deref(), Some("high"));
    }

    #[test]
    fn auto_tool_choice_is_collapsed() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "auto"}
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert!(translated.tool_choice.is_none());
    }

    #[test]
    fn any_tool_choice_becomes_required() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name":"search","input_schema":{"type":"object"}}],
            "tool_choice": {"type": "any"}
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert!(matches!(
            translated.tool_choice,
            Some(KimiToolChoice::Required)
        ));
    }

    #[test]
    fn named_tool_choice_with_thinking_forces_only_that_tool() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-k3.1-pro",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"name":"search","input_schema":{"type":"object"}},
                {"name":"lookup","input_schema":{"type":"object"}}
            ],
            "tool_choice": {"type": "tool", "name": "lookup"}
        }))
        .unwrap();
        let wire = serde_json::to_value(
            translate_request(&req, TranslateOptions { session_id: None }).unwrap(),
        )
        .unwrap();
        assert_eq!(wire["tool_choice"], "required");
        assert_eq!(wire["tools"].as_array().unwrap().len(), 1);
        assert_eq!(wire["tools"][0]["function"]["name"], "lookup");
    }

    #[test]
    fn system_text_excludes_billing_headers() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-for-coding",
            "system": [
                {"type": "text", "text": "You are a helpful assistant."},
                {"type": "text", "text": "x-anthropic-billing-header: secret"}
            ],
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        let system_msg = translated
            .messages
            .iter()
            .find(|m| matches!(m, KimiMessage::System { .. }));
        assert!(system_msg.is_some());
        if let Some(KimiMessage::System { content, .. }) = system_msg {
            assert!(!content.contains("x-anthropic-billing-header"));
            assert!(content.contains("helpful"));
        }
    }

    #[test]
    fn k3_system_merged_into_user() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-k3",
            "messages": [{"role": "user", "content": "hello"}],
            "system": "Be helpful."
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.model, "k3");
        assert!(
            !translated
                .messages
                .iter()
                .any(|m| matches!(m, KimiMessage::System { .. }))
        );
        match &translated.messages[0] {
            KimiMessage::User { content, .. } => {
                let text = content.as_str().unwrap();
                assert!(text.contains("Be helpful."));
                assert!(text.contains("hello"));
            }
            _ => panic!("expected User message"),
        }
    }

    #[test]
    fn k3_system_with_images_preserves_image_blocks() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-k3",
            "system": "Describe images.",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}
            ]}]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        match &translated.messages[0] {
            KimiMessage::User { content, .. } => {
                let parts = content
                    .as_array()
                    .expect("expected array for mixed content");
                assert!(parts.len() >= 3);
                assert!(
                    parts.iter().any(|p| p.get("image_url").is_some()),
                    "image block missing"
                );
                let texts: Vec<&str> = parts
                    .iter()
                    .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                    .collect();
                assert!(
                    texts.iter().any(|t| t.contains("Describe images.")),
                    "system text missing"
                );
                assert!(
                    texts.iter().any(|t| t.contains("what is this?")),
                    "user text missing"
                );
            }
            _ => panic!("expected User message"),
        }
    }

    #[test]
    fn k3_system_not_lost_on_tool_result_only_turn() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-k3",
            "messages": [
                {"role": "user", "content": "do something"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}}
                ]},
                {"role": "system", "content": [{"type": "text", "text": "reminder text"}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "output"}
                ]}
            ]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        let user_msgs: Vec<&KimiMessage> = translated
            .messages
            .iter()
            .filter(|m| matches!(m, KimiMessage::User { .. }))
            .collect();
        let all_text: String = user_msgs
            .iter()
            .map(|m| match m {
                KimiMessage::User { content, .. } => content.as_str().unwrap_or("").to_string(),
                _ => String::new(),
            })
            .collect();
        assert!(
            all_text.contains("reminder text"),
            "system reminder lost on tool-result-only turn"
        );
        assert!(
            translated
                .messages
                .iter()
                .any(|m| matches!(m, KimiMessage::Tool { .. })),
            "tool result missing"
        );
    }

    #[test]
    fn k3_effort_defaults_to_high() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-k3",
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.reasoning_effort.as_deref(), Some("high"));
    }

    #[test]
    fn k3_effort_max_preserved() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-k3",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "max"}
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.reasoning_effort.as_deref(), Some("max"));
    }

    #[test]
    fn k3_max_tokens_passes_through() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-k3",
            "max_tokens": 500000,
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.max_tokens, 500000);
    }

    #[test]
    fn endpoint_model_ids_preserve_k3_wire_shape() {
        for (base_url, expected) in [
            ("https://api.kimi.ai/coding/v1", "k3.1-pro"),
            ("https://api.kimi.com/coding/v1", "k3.1-pro"),
            ("https://api.moonshot.ai/v1", "kimi-k3.1-pro"),
        ] {
            let req: MessagesRequest = serde_json::from_value(json!({
                "model": "kimi-k3.1-pro",
                "system": "system instructions",
                "max_tokens": 100_000,
                "messages": [{"role": "user", "content": "hi"}]
            }))
            .unwrap();
            let old = std::env::var_os("CCP_KIMI_BASE_URL");
            unsafe { std::env::set_var("CCP_KIMI_BASE_URL", base_url) };
            let result = translate_request(&req, TranslateOptions { session_id: None });
            unsafe {
                match old {
                    Some(value) => std::env::set_var("CCP_KIMI_BASE_URL", value),
                    None => std::env::remove_var("CCP_KIMI_BASE_URL"),
                }
            }
            let wire = serde_json::to_value(result.unwrap()).unwrap();
            assert_eq!(wire["model"], expected, "{base_url}");
            assert_eq!(wire["max_tokens"], 100_000, "{base_url}");
            assert_eq!(wire["reasoning_effort"], "high", "{base_url}");
            assert_eq!(wire["messages"][0]["role"], "user", "{base_url}");
            assert!(
                wire["messages"][0]["content"]
                    .as_str()
                    .unwrap()
                    .contains("system instructions")
            );
        }
    }

    #[test]
    fn k3_max_tokens_defaults_to_context_window() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model": "kimi-k3",
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .unwrap();
        let translated = translate_request(&req, TranslateOptions { session_id: None }).unwrap();
        assert_eq!(translated.max_tokens, 1_048_576);
    }
}
