use claude_codex::providers::{codex, grok};
use serde_json::{Value, json};

pub const PRIVATE: &str = "PRIVATE_REASONING_SENTINEL";
pub const PUBLIC: &str = "PUBLIC_SEARCH_SENTINEL";
pub const URL: &str = "https://example.invalid/public-search-result";
pub const CITATION: &str = "PUBLIC_CITATION_SENTINEL";

fn sse(events: Vec<Value>) -> Vec<u8> {
    events
        .into_iter()
        .map(|event| format!("data: {event}\n\n"))
        .collect::<String>()
        .into_bytes()
}

fn tool_events() -> Vec<Value> {
    vec![
        json!({"type":"response.output_item.added","output_index":3,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"lookup"}}),
        json!({"type":"response.output_item.added","output_index":4,"item":{"type":"function_call","id":"fc_2","call_id":"call_2","name":"lookup"}}),
        json!({"type":"response.function_call_arguments.delta","output_index":3,"item_id":"fc_1","call_id":"call_1","delta":"{\"q\":"}),
        json!({"type":"response.function_call_arguments.delta","output_index":4,"item_id":"fc_2","call_id":"call_2","delta":"{\"q\":"}),
        json!({"type":"response.function_call_arguments.delta","output_index":3,"item_id":"fc_1","call_id":"call_1","delta":"1}"}),
        json!({"type":"response.function_call_arguments.delta","output_index":4,"item_id":"fc_2","call_id":"call_2","delta":"2}"}),
        json!({"type":"response.output_item.done","output_index":4,"item":{"type":"function_call","id":"fc_2","call_id":"call_2","name":"lookup","arguments":"{\"q\":2}"}}),
        json!({"type":"response.output_item.done","output_index":3,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"lookup","arguments":"{\"q\":1}"}}),
        json!({"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":3,"output_tokens":1}}}),
    ]
}

pub fn emitted_histories() -> Vec<(&'static str, Value)> {
    let mut codex_events = vec![
        json!({"type":"response.reasoning_summary_text.delta","output_index":0,"delta":PRIVATE}),
        json!({"type":"response.output_item.added","output_index":1,"item":{"type":"web_search_call","id":"search_1"}}),
        json!({"type":"response.web_search_call.in_progress","output_index":1,"item_id":"search_1"}),
        json!({"type":"response.web_search_call.completed","output_index":1,"item_id":"search_1"}),
        json!({"type":"response.output_item.done","output_index":1,"item":{"type":"web_search_call","id":"search_1","action":{"query":PUBLIC}}}),
        json!({"type":"response.output_item.added","output_index":2,"item":{"type":"message","id":"msg_up"}}),
        json!({"type":"response.output_text.delta","output_index":2,"delta":format!("{PUBLIC} [{CITATION}]({URL})")}),
        json!({"type":"response.output_item.done","output_index":2,"item":{"type":"message"}}),
    ];
    codex_events.extend(tool_events());
    let codex = codex::translate::accumulate::accumulate_response(
        &sse(codex_events),
        "msg_codex",
        "gpt-5.5",
    )
    .unwrap();
    let mut grok_events = vec![
        json!({"type":"response.reasoning_text.delta","delta":PRIVATE}),
        json!({"type":"response.reasoning_text.done"}),
        json!({"type":"response.output_item.added","item":{"type":"custom_tool_call","name":"x_keyword_search","id":"search_1"}}),
        json!({"type":"response.custom_tool_call_input.delta","item_id":"search_1","delta":json!({"query":PUBLIC}).to_string()}),
        json!({"type":"response.custom_tool_call_input.done","item_id":"search_1"}),
        json!({"type":"response.output_item.done","item":{"type":"custom_tool_call","name":"x_keyword_search","id":"search_1"}}),
        json!({"type":"response.output_text.delta","delta":PUBLIC}),
        json!({"type":"response.output_text.annotation.added","annotation":{"type":"url_citation","title":CITATION,"url":URL,"text":"cited source"}}),
        json!({"type":"response.output_text.done"}),
    ];
    grok_events.extend(tool_events());
    let grok =
        grok::translate::accumulate::accumulate_response(&sse(grok_events), "msg_grok", "grok-4.5")
            .unwrap();
    for response in [&codex, &grok] {
        let content = response["content"].as_array().unwrap();
        assert!(
            content
                .iter()
                .any(|block| block["type"] == "server_tool_use")
        );
        assert_eq!(
            content
                .iter()
                .filter(|block| block["type"] == "tool_use")
                .count(),
            2
        );
        assert!(response.to_string().contains(PRIVATE));
        assert!(response.to_string().contains(URL));
        assert!(response.to_string().contains(CITATION));
    }
    assert!(
        codex["content"]
            .as_array()
            .unwrap()
            .iter()
            .any(|block| block["type"] == "web_search_tool_result"
                && !block["content"].as_array().unwrap().is_empty())
    );
    assert!(
        grok["content"]
            .as_array()
            .unwrap()
            .iter()
            .any(|block| block["type"] == "x_search_tool_result")
    );
    vec![("codex", codex), ("grok", grok)]
}

pub fn messages(content: &Value) -> Value {
    json!([
        {"role":"user","content":"search"},
        {"role":"assistant","content":content},
        {"role":"user","content":[
            {"type":"tool_result","tool_use_id":"call_2","content":"RESULT_SECOND"},
            {"type":"tool_result","tool_use_id":"call_1","content":"RESULT_FIRST"}
        ]}
    ])
}
