//! The Claude shim's opt-in, eval-only tool transcript and reply adapter.
use super::{cli::strip_fence, json};
use serde_json::Value;
use std::collections::HashMap;

pub(super) const RETRY: &str = "Your previous reply looked like a tool call but was not valid JSON. Reply with ONLY the JSON object {\"tool_call\": {\"name\": ..., \"arguments\": {...}}} and nothing else, or with a plain-text answer containing no JSON at all.";

fn as_text(v: &Value) -> String {
    match v {
        Value::Array(a) => a
            .iter()
            .filter(|p| p.is_object() && json::truth(&p["text"]))
            .map(|p| p["text"].as_str().unwrap_or(""))
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Null => String::new(),
        _ => json::text(v),
    }
}
fn args_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "{}".into(),
        _ => json::dumps(v, false),
    }
}

pub(super) fn enabled(v: &Value) -> bool {
    match v {
        Value::String(s) => s.trim().to_lowercase() != "none",
        Value::Object(_) => json::text(&v["type"]).to_lowercase() != "none",
        _ => true,
    }
}

pub(super) fn render(v: &Value) -> String {
    let Some(specs) = v.as_array() else {
        return String::new();
    };
    let mut blocks = vec![];
    for spec in specs {
        let f = if spec["function"].is_object() {
            &spec["function"]
        } else {
            spec
        };
        let Some(name) = f["name"].as_str().filter(|s| !s.is_empty()) else {
            continue;
        };
        let empty = json!({"type":"object","properties":{}});
        let params = if f["parameters"].is_object() {
            &f["parameters"]
        } else {
            &empty
        };
        let description = if json::truth(&f["description"]) {
            json::text(&f["description"])
        } else {
            String::new()
        };
        blocks.push(format!(
            "- name: {name}\n  description: {description}\n  parameters (JSON Schema): {}",
            json::dumps(params, false)
        ));
    }
    if blocks.is_empty() {
        String::new()
    } else {
        format!(
            "{}\n{}",
            include_str!("tool_preamble.txt").replace("\r\n", "\n"),
            blocks.join("\n\n")
        )
    }
}

pub(super) fn fold(msgs: &[Value]) -> (String, String) {
    let mut system = vec![];
    let mut turns = vec![];
    let mut names = HashMap::new();
    for m in msgs {
        if !m.is_object() {
            continue;
        }
        let content = &m["content"];
        if m["role"] == "system" {
            if json::truth(content) {
                system.push(as_text(content));
            }
            continue;
        }
        if m["role"] == "tool" {
            let name = names
                .get(m["tool_call_id"].as_str().unwrap_or(""))
                .copied()
                .or(m["name"].as_str())
                .unwrap_or("tool");
            turns.push(format!("[tool {name} returned: {}]", as_text(content)));
            continue;
        }
        if json::truth(content) {
            turns.push(format!(
                "{}: {}",
                if m["role"] == "assistant" {
                    "Assistant"
                } else {
                    "User"
                },
                as_text(content)
            ));
        }
        if m["role"] == "assistant"
            && let Some(calls) = m["tool_calls"].as_array()
        {
            for call in calls {
                let f = &call["function"];
                let name = f["name"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("tool");
                if let Some(id) = call["id"].as_str().filter(|s| !s.is_empty()) {
                    names.insert(id, name);
                }
                turns.push(format!(
                    "[assistant called tool {name} with {}]",
                    args_text(&f["arguments"])
                ));
            }
        }
    }
    (system.join("\n\n"), turns.join("\n\n"))
}

fn span(s: &str) -> Option<&str> {
    let first = s.find('{')?;
    let last = s.rfind('}')?;
    if last <= first {
        return None;
    }
    let s = &s[first..=last];
    s.contains("tool_call").then_some(s)
}
fn marker(s: &str) -> Option<Value> {
    let re = regex::Regex::new(
        r"(?s)^\s*\[assistant called tool\s+([A-Za-z0-9_.-]+)\s+with\s+(\{.*\})\s*\]\s*$",
    )
    .expect("static regex");
    let s = strip_fence(s);
    let m = re.captures(&s)?;
    Some(json!({"tool_call":{"name":&m[1],"arguments":&m[2]}}))
}
pub(super) fn looks_like(s: &str) -> bool {
    let text = strip_fence(s);
    text.starts_with('{') && text.contains("tool_call") || marker(s).is_some() || span(s).is_some()
}
fn balance(s: &str) -> String {
    let (mut depth, mut quoted, mut escaped) = (0i32, false, false);
    for c in s.chars() {
        if quoted {
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                quoted = false;
            }
        } else {
            match c {
                '"' => quoted = true,
                '{' => depth += 1,
                '}' => depth -= 1,
                _ => {}
            }
        }
    }
    format!("{s}{}", "}".repeat(depth.max(0) as usize))
}
pub(super) fn parse(s: &str) -> Option<Vec<Value>> {
    let value = if let Some(v) = marker(s) {
        v
    } else {
        let text = strip_fence(s);
        let text = if text.starts_with('{') {
            text.as_str()
        } else {
            span(s)?
        };
        serde_json::from_str::<Value>(text)
            .or_else(|_| serde_json::from_str(&balance(text)))
            .ok()?
    };
    let items = if !value["tool_call"].is_null() {
        vec![value["tool_call"].clone()]
    } else {
        value["tool_calls"].as_array()?.clone()
    };
    let mut calls = vec![];
    for item in items {
        let f = if item["function"].is_object() {
            &item["function"]
        } else {
            &item
        };
        let Some(name) = f["name"].as_str().filter(|s| !s.is_empty()) else {
            continue;
        };
        let args = if let Some(s) = f["arguments"].as_str() {
            serde_json::from_str(s).unwrap_or(Value::Null)
        } else {
            f["arguments"].clone()
        };
        let args = if args.is_object() { args } else { json!({}) };
        calls.push(json!({"id":format!("call_{}",&uuid::Uuid::new_v4().simple().to_string()[..8]),"type":"function","function":{"name":name,"arguments":json::dumps(&args,false)}}));
    }
    (!calls.is_empty()).then_some(calls)
}
