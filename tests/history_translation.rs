//! The canonical Messages history is the hub for every registered provider class.
use claude_codex::{
    anthropic::schema::MessagesRequest,
    config::GrokToolImageMode,
    providers::{codex, cursor, cursor_cli, gemini, grok, kimi, translate_shared::ProxyReasoning},
};
use serde_json::{Value, json};

const PRIVATE: &str = "PRIVATE_REASONING_SENTINEL";
const VISIBLE: &str = "VISIBLE_ANSWER_SENTINEL <thinking>ordinary text</thinking> <previous_reasoning>literal</previous_reasoning>";
const RESULT: &str = "PUBLIC_TOOL_RESULT_SENTINEL";

fn request(block: Value) -> MessagesRequest {
    serde_json::from_value(json!({
        "model":"kimi-k3.1-pro-highspeed", "max_tokens":1024,
        "tools":[{"name":"lookup","input_schema":{"type":"object"}}],
        "messages":[
            {"role":"user","content":"question"},
            {"role":"assistant","content":[
                block,
                {"type":"text","text":VISIBLE},
                {"type":"tool_use","id":"call_1","name":"lookup","input":{"q":"x"}}
            ]},
            {"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":RESULT}]}
        ]
    })).unwrap()
}

fn translate(destination: &str, req: &MessagesRequest) -> anyhow::Result<Value> {
    Ok(match destination {
        "codex" => serde_json::to_value(
            codex::translate::request::translate_openai_compatible_request(
                req,
                "gpt-arbitrary-supported-id".into(),
                None,
            )?,
        )?,
        "kimi" => serde_json::to_value(kimi::translate::request::translate_request(
            req,
            kimi::translate::request::TranslateOptions { session_id: None },
        )?)?,
        "gemini" | "local" => {
            serde_json::to_value(gemini::translate::request::translate_request_with(
                req,
                "arbitrary-upstream-model",
                destination == "local",
            )?)?
        }
        "grok" => serde_json::to_value(grok::translate::request::translate_request_with_mode(
            req,
            "grok-arbitrary-supported-id".into(),
            GrokToolImageMode::Reject,
        )?)?,
        "cursor" => json!(cursor::request::render_cursor_prompt(req)?),
        "cursor-cli" => json!(cursor_cli::prompt::render_prompt(req)?),
        _ => unreachable!(),
    })
}

const DESTINATIONS: [&str; 7] = [
    "codex",
    "kimi",
    "gemini",
    "local",
    "grok",
    "cursor",
    "cursor-cli",
];

fn history_sources() -> Vec<(&'static str, Value)> {
    let mut sources = vec![
        (
            "anthropic",
            json!({"type":"thinking","thinking":PRIVATE,"signature":"EqQBsig=="}),
        ),
        (
            "anthropic-redacted",
            json!({"type":"redacted_thinking","data":PRIVATE}),
        ),
        (
            "cursor",
            json!({"type":"thinking","thinking":PRIVATE,"signature":""}),
        ),
        ("grok", json!({"type":"thinking","thinking":PRIVATE})),
    ];
    for (source, origin) in [
        ("codex", ProxyReasoning::Codex),
        ("kimi", ProxyReasoning::Kimi),
        ("gemini", ProxyReasoning::Gemini),
        ("local", ProxyReasoning::Gemini),
        ("cursor-cli", ProxyReasoning::CursorCli),
    ] {
        for signature in [
            origin.sign("msg_1", 0),
            format!("{}msg_1:0", origin.prefix()),
        ] {
            sources.push((
                source,
                json!({"type":"thinking","thinking":PRIVATE,"signature":signature}),
            ));
        }
    }
    sources
}

fn strip_native_channels(value: &mut Value) {
    match value {
        Value::Object(object) => {
            object.remove("reasoning_content");
            object.remove("encrypted_content");
            for child in object.values_mut() {
                strip_native_channels(child);
            }
        }
        Value::Array(array) => {
            for child in array {
                strip_native_channels(child);
            }
        }
        _ => {}
    }
}

#[test]
fn all_provider_histories_keep_public_content_and_never_replay_private_reasoning_as_text() {
    for destination in DESTINATIONS {
        for (source, block) in history_sources() {
            let req = request(block);
            let mut payload = translate(destination, &req)
                .unwrap_or_else(|error| panic!("{source} -> {destination}: {error}"));
            let native = (source == "kimi" && destination == "kimi")
                || (["gemini", "local"].contains(&source) && destination == "local");
            assert_eq!(
                payload.to_string().contains(PRIVATE),
                native,
                "{source} -> {destination}: {payload}"
            );
            strip_native_channels(&mut payload);
            let serialized = payload.to_string();
            assert!(
                !serialized.contains(PRIVATE),
                "{source} -> {destination}: {payload}"
            );
            assert!(
                serialized.contains(VISIBLE),
                "{source} -> {destination}: {payload}"
            );
            assert!(
                serialized.contains(RESULT),
                "{source} -> {destination}: {payload}"
            );
            if destination != "cursor-cli" {
                assert_eq!(
                    serialized.matches("call_1").count(),
                    2,
                    "{source} -> {destination}: {payload}"
                );
                assert!(serialized.find(VISIBLE).unwrap() < serialized.find(RESULT).unwrap());
            }
        }
    }
}

#[test]
fn codex_replays_only_its_opaque_continuation_not_its_visible_summary() {
    let replay = codex::translate::reasoning_signature::ReasoningReplay {
        id: "rs_native".into(),
        encrypted_content: "OPAQUE_NATIVE_CONTENT".into(),
    };
    let signature =
        codex::translate::reasoning_signature::encode_reasoning_signature(&replay).unwrap();
    let req = request(json!({"type":"thinking","thinking":PRIVATE,"signature":signature}));
    for destination in DESTINATIONS {
        let payload = translate(destination, &req).unwrap();
        assert!(
            !payload.to_string().contains(PRIVATE),
            "{destination}: {payload}"
        );
        assert_eq!(
            payload.to_string().contains("OPAQUE_NATIVE_CONTENT"),
            destination == "codex"
        );
        if destination == "codex" {
            assert_eq!(
                payload["input"][1],
                json!({"type":"reasoning","id":"rs_native","summary":[],"encrypted_content":"OPAQUE_NATIVE_CONTENT"})
            );
        }
    }
}

#[test]
fn unsupported_data_is_an_explicit_error_not_a_silently_empty_translation() {
    for destination in DESTINATIONS {
        for kind in ["document", "audio", "video", "future_content"] {
            for nested in [false, true] {
                let block = json!({"type":kind,"source":{"type":"base64","media_type":"application/pdf","data":"JVBERi0="}});
                let content = if nested {
                    json!([{"type":"tool_result","tool_use_id":"call_1","content":[block]}])
                } else {
                    json!([block])
                };
                let mut req = request(json!({"type":"thinking","thinking":PRIVATE}));
                req.messages[2].content = content;
                let error = translate(destination, &req).unwrap_err().to_string();
                assert!(error.contains(kind), "{destination} ({nested}): {error}");
            }
        }
    }
}

#[test]
fn image_inputs_are_pixels_or_an_explicit_capability_error() {
    for destination in DESTINATIONS {
        for nested in [false, true] {
            let block = json!({"type":"image","source":{"type":"base64","media_type":"image/png","data":"QUJD"}});
            let content = if nested {
                json!([{"type":"tool_result","tool_use_id":"call_1","content":[block]}])
            } else {
                json!([block])
            };
            let mut req = request(json!({"type":"thinking","thinking":PRIVATE}));
            req.messages[2].content = content;
            match translate(destination, &req) {
                Ok(payload) if destination == "cursor" => {
                    assert_eq!(
                        cursor::request::cursor_selected_images(&req)[0].data,
                        "QUJD"
                    );
                    assert!(payload.to_string().contains("image/png"));
                }
                Ok(payload) => assert!(
                    payload.to_string().contains("data:image/png;base64,QUJD"),
                    "{destination}: {payload}"
                ),
                Err(error) => {
                    assert!(
                        ["cursor-cli", "grok"].contains(&destination),
                        "{destination}: {error}"
                    );
                    assert!(error.to_string().contains("image"));
                }
            }
        }
    }
}

#[test]
fn openai_compatible_unsigned_reasoning_cannot_become_a_native_or_public_replay() {
    use claude_codex::openai_compat::{OpenAiSurface, request::parse_request};
    for destination in DESTINATIONS {
        let parsed = parse_request(
            OpenAiSurface::ChatCompletions,
            json!({"model":"kimi-k3.1-pro-highspeed", "messages":[
                {"role":"user","content":"question"},
                {"role":"assistant","content":VISIBLE,"reasoning_content":PRIVATE},
                {"role":"user","content":"continue"}
            ]}),
            destination,
            None,
        )
        .unwrap();
        let payload = translate(destination, &parsed.messages).unwrap();
        assert!(
            !payload.to_string().contains(PRIVATE),
            "{destination}: {payload}"
        );
        assert!(
            payload.to_string().contains(VISIBLE),
            "{destination}: {payload}"
        );
    }
}

#[path = "fixtures/hosted_history.rs"]
mod hosted_history;

#[test]
fn complete_emitted_search_histories_preserve_public_records_and_interleaved_tools() {
    for (source, response) in hosted_history::emitted_histories() {
        let req: MessagesRequest = serde_json::from_value(json!({
            "model":"kimi-k3.1-pro-highspeed", "max_tokens":1024,
            "tools":[{"name":"lookup","input_schema":{"type":"object"}}],
            "messages":hosted_history::messages(&response["content"])
        }))
        .unwrap();
        for destination in DESTINATIONS {
            let payload = translate(destination, &req)
                .unwrap_or_else(|error| panic!("{source} -> {destination}: {error}"));
            let text = payload.to_string();
            assert!(
                !text.contains(hosted_history::PRIVATE),
                "{source} -> {destination}: {payload}"
            );
            for public in [
                hosted_history::PUBLIC,
                hosted_history::URL,
                hosted_history::CITATION,
                "search_1",
                "RESULT_FIRST",
                "RESULT_SECOND",
            ] {
                assert!(
                    text.contains(public),
                    "{source} -> {destination} lost {public}: {payload}"
                );
            }
            assert!(
                text.find("RESULT_SECOND").unwrap() < text.find("RESULT_FIRST").unwrap(),
                "{source} -> {destination}: {payload}"
            );
            if destination != "cursor-cli" {
                assert_eq!(
                    text.matches("call_1").count(),
                    2,
                    "{source} -> {destination}: {payload}"
                );
                assert_eq!(
                    text.matches("call_2").count(),
                    2,
                    "{source} -> {destination}: {payload}"
                );
                assert!(
                    text.find("call_1").unwrap() < text.find("call_2").unwrap(),
                    "{source} -> {destination}: {payload}"
                );
            }
        }
    }
}
