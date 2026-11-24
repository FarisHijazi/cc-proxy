use serde_json::Value;

use crate::anthropic::schema::MessagesRequest;

/// A `thinking` signature this proxy minted for a non-Anthropic backend. Anthropic
/// never issues these prefixes, so they mark reasoning that only its own backend
/// may replay natively. Codex writes the raw prefix followed by its replay payload;
/// the others write the URL-safe base64 of `prefix + message_id:index`. Both
/// spellings are recognised for every source, so old transcripts stay readable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProxyReasoning {
    Codex,
    Kimi,
    Gemini,
    CursorCli,
    Agy,
    Zai,
}

impl ProxyReasoning {
    pub const ALL: [Self; 6] = [
        Self::Codex,
        Self::Kimi,
        Self::Gemini,
        Self::CursorCli,
        Self::Agy,
        Self::Zai,
    ];

    pub const fn prefix(self) -> &'static str {
        match self {
            Self::Codex => "ccp:codex:v1:",
            Self::Kimi => "ccp:kimi:v1:",
            Self::Gemini => "ccp:gemini:v1:",
            Self::CursorCli => "ccp:cursor-cli:v1:",
            Self::Agy => "ccp:agy:v1:",
            Self::Zai => "ccp:zai:v1:",
        }
    }

    /// The synthetic signature for a backend that returns reasoning without one.
    pub fn sign(self, message_id: &str, index: usize) -> String {
        use base64::Engine;
        let input = format!("{}{message_id}:{index}", self.prefix());
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(input.as_bytes())
    }

    pub fn of(signature: &str) -> Option<Self> {
        use base64::Engine;
        // 24 base64 characters decode to 18 bytes, the longest prefix.
        let head = signature.as_bytes();
        let head = head.get(..24).unwrap_or(head);
        let decoded = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(head)
            .unwrap_or_default();
        Self::ALL.into_iter().find(|origin| {
            let prefix = origin.prefix().as_bytes();
            signature.as_bytes().starts_with(prefix) || decoded.starts_with(prefix)
        })
    }

    /// Whether a `thinking` block's signature names this backend as its writer.
    pub fn wrote(self, signature: Option<&str>) -> bool {
        signature.and_then(Self::of) == Some(self)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ImageInput {
    Unsupported,
    Base64,
    Any,
}

/// What a backend can take from an Anthropic request. Content outside it is
/// refused before the upstream call instead of being dropped or replaced.
#[derive(Clone, Copy, Debug)]
pub struct InputSupport {
    pub backend: &'static str,
    pub images: ImageInput,
    pub tool_result_images: ImageInput,
}

impl InputSupport {
    pub fn ensure(&self, req: &MessagesRequest) -> anyhow::Result<()> {
        let backend = self.backend;
        if let Some(system) = req.extra.get("system") {
            ensure_text_content(system, backend, "system")?;
        }
        for message in &req.messages {
            let role = message.role.as_str();
            anyhow::ensure!(
                message.content.is_string() || message.content.is_array(),
                "{backend} message content must be text or an array"
            );
            if matches!(role, "system" | "developer") {
                ensure_text_content(&message.content, backend, role)?;
                continue;
            }
            for block in message.content.as_array().into_iter().flatten() {
                match block_kind(block) {
                    "text" | "tool_use" | "thinking" | "redacted_thinking" => {}
                    "server_tool_use"
                        if role == "assistant" && public_history_text(block).is_some() => {}
                    "web_search_tool_result" | "x_search_tool_result" => {}
                    "image" if role != "assistant" => {
                        ensure_image(self.images, block, backend, "")?
                    }
                    "tool_result" => {
                        let parts = block.get("content").and_then(Value::as_array);
                        for part in parts.into_iter().flatten() {
                            match block_kind(part) {
                                "text" | "tool_reference" => {}
                                "image" => ensure_image(
                                    self.tool_result_images,
                                    part,
                                    backend,
                                    " in tool results",
                                )?,
                                other => anyhow::bail!(
                                    "{backend} does not support {other} content in tool results"
                                ),
                            }
                        }
                    }
                    other => {
                        anyhow::bail!(
                            "{backend} does not support {other} content in {role} messages"
                        )
                    }
                }
            }
        }
        Ok(())
    }
}

fn ensure_text_content(content: &Value, backend: &str, role: &str) -> anyhow::Result<()> {
    if content.is_string() {
        return Ok(());
    }
    let blocks = content
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("{backend} {role} content must be text or an array"))?;
    for block in blocks {
        let kind = block_kind(block);
        anyhow::ensure!(
            kind == "text",
            "{backend} does not support {kind} content in {role} messages"
        );
    }
    Ok(())
}

fn block_kind(block: &Value) -> &str {
    block
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("untyped")
}

pub fn ensure_image(
    mode: ImageInput,
    block: &Value,
    backend: &str,
    place: &str,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        mode != ImageInput::Unsupported,
        "{backend} does not support image content{place}"
    );
    match block.pointer("/source/type").and_then(Value::as_str) {
        Some("url") => {
            anyhow::ensure!(
                mode == ImageInput::Any,
                "{backend} does not support url image content{place}"
            );
            anyhow::ensure!(
                block
                    .pointer("/source/url")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty()),
                "{backend} image url is missing{place}"
            );
        }
        Some("base64") => {
            let media = block
                .pointer("/source/media_type")
                .and_then(Value::as_str)
                .unwrap_or("");
            anyhow::ensure!(
                matches!(
                    media,
                    "image/jpeg" | "image/png" | "image/gif" | "image/webp"
                ),
                "{backend} does not support image media_type {media}{place}"
            );
            let data = block
                .pointer("/source/data")
                .and_then(Value::as_str)
                .unwrap_or("");
            anyhow::ensure!(
                decode_image_data(data).is_some(),
                "{backend} image requires valid base64 data{place}"
            );
        }
        other => anyhow::bail!("{backend} does not support image source {other:?}{place}"),
    }
    Ok(())
}

/// Decode the image spelling accepted at the request boundary and by image senders.
pub fn decode_image_data(data: &str) -> Option<Vec<u8>> {
    use base64::Engine;
    let compact: String = data.chars().filter(|c| !c.is_ascii_whitespace()).collect();
    if compact.is_empty() {
        return None;
    }
    base64::engine::general_purpose::STANDARD
        .decode(&compact)
        .or_else(|_| base64::engine::general_purpose::STANDARD_NO_PAD.decode(&compact))
        .ok()
}

/// Hosted searches have already run: replay their public record, not a function
/// call that the next backend might execute again. Citations are public too.
/// Ordinary text remains unchanged.
pub fn public_history_text(block: &Value) -> Option<String> {
    match block_kind(block) {
        "server_tool_use"
            if matches!(
                block.get("name").and_then(Value::as_str),
                Some("web_search" | "x_search")
            ) =>
        {
            Some(block.to_string())
        }
        "web_search_tool_result" | "x_search_tool_result" => Some(block.to_string()),
        "text"
            if block
                .get("citations")
                .and_then(Value::as_array)
                .is_some_and(|c| !c.is_empty()) =>
        {
            Some(format!(
                "{}\n\n{}",
                block.get("text").and_then(Value::as_str).unwrap_or(""),
                serde_json::json!({"citations": block["citations"]})
            ))
        }
        _ => None,
    }
}

/// Claude Code's ToolSearch result: a pointer to a schema already in `tools`.
pub fn tool_reference_text(name: &str) -> String {
    format!("Tool available: {name}. Use its registered function schema.")
}

#[derive(Debug)]
pub enum ContentBlock {
    Text {
        text: String,
    },
    Image {
        source: ImageSource,
    },
    ToolUse {
        id: String,
        name: String,
        input: Value,
    },
    ToolResult {
        tool_use_id: String,
        content: Value,
        is_error: Option<bool>,
    },
    Thinking {
        thinking: String,
        signature: Option<String>,
    },
}

#[derive(Debug)]
pub struct ImageSource {
    pub media_type: String,
    pub data: String,
    pub source_type: String,
}

pub fn flatten_system_text(system_val: Option<&Value>) -> Option<String> {
    let system = system_val?;
    let texts: Vec<String> = match system {
        Value::String(s) => vec![s.clone()],
        Value::Array(arr) => arr
            .iter()
            .filter_map(|b| {
                let text = b.get("text").and_then(|v| v.as_str())?;
                if text.starts_with("x-anthropic-billing-header:") {
                    None
                } else {
                    Some(text.to_string())
                }
            })
            .collect(),
        _ => return None,
    };
    if texts.is_empty() {
        None
    } else {
        Some(texts.join("\n\n"))
    }
}

pub fn parallel_tool_calls(req: &MessagesRequest) -> Option<bool> {
    req.extra
        .get("tool_choice")
        .and_then(Value::as_object)
        .and_then(|choice| choice.get("disable_parallel_tool_use"))
        .and_then(Value::as_bool)
        .map(|disabled| !disabled)
}

pub fn read_effort(req: &MessagesRequest) -> Result<Option<&str>, anyhow::Error> {
    let output_config = match req.extra.get("output_config") {
        Some(Value::Object(m)) => m,
        _ => return Ok(None),
    };
    match output_config.get("effort") {
        Some(Value::String(s)) => {
            let valid = ["low", "medium", "high", "xhigh", "max"];
            if valid.contains(&s.as_str()) {
                Ok(Some(s.as_str()))
            } else {
                anyhow::bail!("Invalid output_config.effort: {s}")
            }
        }
        _ => Ok(None),
    }
}

pub fn normalize_content(content: &Value, missing_tool_input: Value) -> Vec<ContentBlock> {
    match content {
        Value::String(s) => {
            vec![ContentBlock::Text { text: s.clone() }]
        }
        Value::Array(arr) => {
            let mut blocks = Vec::new();
            for item in arr {
                if let Some(block) = parse_content_block(item, missing_tool_input.clone()) {
                    blocks.push(block);
                }
            }
            blocks
        }
        _ => Vec::new(),
    }
}

pub fn image_source_to_url(source: &ImageSource) -> String {
    if source.source_type == "url" {
        source.data.clone()
    } else {
        format!("data:{};base64,{}", source.media_type, source.data)
    }
}

pub fn image_block_to_url(block: &Value) -> String {
    let source_type = block
        .get("source")
        .and_then(|s| s.get("type"))
        .and_then(|v| v.as_str())
        .unwrap_or("base64");
    if source_type == "url" {
        block
            .get("source")
            .and_then(|s| s.get("url"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    } else {
        let media_type = block
            .get("source")
            .and_then(|s| s.get("media_type"))
            .and_then(|v| v.as_str())
            .unwrap_or("image/png");
        let data = block
            .get("source")
            .and_then(|s| s.get("data"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        format!("data:{media_type};base64,{data}")
    }
}

fn parse_content_block(value: &Value, missing_tool_input: Value) -> Option<ContentBlock> {
    if let Some(text) = public_history_text(value) {
        return Some(ContentBlock::Text { text });
    }
    let kind = value.get("type").and_then(|v| v.as_str())?;
    match kind {
        "text" => {
            let text = value
                .get("text")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            Some(ContentBlock::Text { text })
        }
        "image" => {
            let source = value.get("source")?;
            let media_type = source
                .get("media_type")
                .and_then(|v| v.as_str())
                .unwrap_or("image/png")
                .to_string();
            let source_type = source
                .get("type")
                .and_then(|v| v.as_str())
                .unwrap_or("base64")
                .to_string();
            let data = if source_type == "url" {
                source.get("url").and_then(|v| v.as_str()).unwrap_or("")
            } else {
                source.get("data").and_then(|v| v.as_str()).unwrap_or("")
            }
            .to_string();
            Some(ContentBlock::Image {
                source: ImageSource {
                    media_type,
                    data,
                    source_type,
                },
            })
        }
        "tool_use" => {
            let id = value
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let name = value
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let input = value.get("input").cloned().unwrap_or(missing_tool_input);
            Some(ContentBlock::ToolUse { id, name, input })
        }
        "tool_result" => {
            let tool_use_id = value
                .get("tool_use_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let content = value
                .get("content")
                .cloned()
                .unwrap_or(Value::String(String::new()));
            let is_error = value.get("is_error").and_then(|v| v.as_bool());
            Some(ContentBlock::ToolResult {
                tool_use_id,
                content,
                is_error,
            })
        }
        "thinking" => {
            let thinking = value
                .get("thinking")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let signature = value
                .get("signature")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            Some(ContentBlock::Thinking {
                thinking,
                signature,
            })
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn signatures_keep_their_persisted_spelling() {
        assert_eq!(
            ProxyReasoning::Kimi.sign("msg_1", 2),
            "Y2NwOmtpbWk6djE6bXNnXzE6Mg"
        );
        assert_eq!(
            ProxyReasoning::of("Y2NwOmtpbWk6djE6bXNnXzE6MA"),
            Some(ProxyReasoning::Kimi)
        );
    }

    #[test]
    fn every_source_is_recognised_raw_and_encoded_and_nothing_else_is() {
        for origin in ProxyReasoning::ALL {
            let raw = format!("{}msg_01ABCdef:3", origin.prefix());
            assert_eq!(ProxyReasoning::of(&raw), Some(origin), "{raw}");
            assert_eq!(ProxyReasoning::of(&origin.sign("m", 0)), Some(origin));
            assert_eq!(
                ProxyReasoning::of(&origin.sign("msg_01ABCdef-long-id", 12)),
                Some(origin)
            );
            assert!(origin.wrote(Some(&origin.sign("m", 0))));
            assert!(!origin.wrote(None));
        }
        assert!(!ProxyReasoning::Kimi.wrote(Some(&ProxyReasoning::Gemini.sign("m", 0))));
        for genuine in [
            "",
            "EqQBsig==",
            "ErUBCkYIBxgCIkB3n2xTqk1oD+5kZy/8w3Qp0Yc6H1tM9zV0wqG5x2RkUuFzLr1kQ==",
            "ccp:other:v1:x",
            "Y2NwOm90aGVyOnYxOng",
        ] {
            assert_eq!(ProxyReasoning::of(genuine), None, "{genuine}");
        }
    }

    fn request(messages: Value) -> MessagesRequest {
        serde_json::from_value(json!({"model": "m", "messages": messages})).unwrap()
    }

    const TEXT_ONLY: InputSupport = InputSupport {
        backend: "toy",
        images: ImageInput::Unsupported,
        tool_result_images: ImageInput::Unsupported,
    };

    #[test]
    fn history_metadata_and_tool_flow_pass() {
        let req = request(json!([
            {"role": "user", "content": "plain"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "t", "signature": "s"},
                {"type": "redacted_thinking", "data": "d"},
                {"type": "server_tool_use", "id": "srv", "name": "web_search", "input": {}},
                {"type": "web_search_tool_result", "tool_use_id": "srv", "content": []},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "x"}, {"type": "tool_reference", "tool_name": "Read"}
                ]}
            ]}
        ]));
        TEXT_ONLY.ensure(&req).unwrap();
    }

    #[test]
    fn unsupported_content_is_named() {
        let base64_image = json!({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ"}});
        let url_image = json!({"type": "image", "source": {"type": "url", "url": "https://example.invalid/a.png"}});
        let tool_result =
            |part: &Value| json!({"type": "tool_result", "tool_use_id": "t", "content": [part]});
        let base64_only = InputSupport {
            images: ImageInput::Base64,
            tool_result_images: ImageInput::Base64,
            ..TEXT_ONLY
        };
        for (support, role, block, message) in [
            (
                TEXT_ONLY,
                "user",
                json!({"type": "document", "source": {}}),
                "toy does not support document content in user messages",
            ),
            (
                TEXT_ONLY,
                "user",
                json!({"type": "audio"}),
                "toy does not support audio content in user messages",
            ),
            (
                TEXT_ONLY,
                "user",
                json!({"text": "no type"}),
                "toy does not support untyped content in user messages",
            ),
            (
                TEXT_ONLY,
                "assistant",
                base64_image.clone(),
                "toy does not support image content in assistant messages",
            ),
            (
                TEXT_ONLY,
                "user",
                base64_image.clone(),
                "toy does not support image content",
            ),
            (
                TEXT_ONLY,
                "user",
                tool_result(&base64_image),
                "toy does not support image content in tool results",
            ),
            (
                TEXT_ONLY,
                "user",
                tool_result(&json!({"type": "document"})),
                "toy does not support document content in tool results",
            ),
            (
                base64_only,
                "user",
                url_image.clone(),
                "toy does not support url image content",
            ),
            (
                base64_only,
                "user",
                tool_result(&url_image),
                "toy does not support url image content in tool results",
            ),
        ] {
            let req = request(json!([{"role": role, "content": [block]}]));
            assert_eq!(support.ensure(&req).unwrap_err().to_string(), message);
        }
        let req = request(
            json!([{"role": "user", "content": [base64_image.clone(), tool_result(&base64_image)]}]),
        );
        base64_only.ensure(&req).unwrap();
    }
}
