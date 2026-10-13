//! Pinned SDK parameter validation. The generated table contains both the
//! protocol schema and the registered handler's schema; they are separate
//! validation stages in MCP 2.1.1. See harness/mcp_validation_schema.py.

use serde_json::Value;
use std::sync::OnceLock;

fn table() -> &'static Value {
    static TABLE: OnceLock<Value> = OnceLock::new();
    TABLE.get_or_init(|| {
        serde_json::from_str(include_str!("validation-schema.json")).expect("SDK schema")
    })
}

pub fn request(method: &str, params: &Value, served: bool) -> bool {
    layer("requests", method, params) && (!served || layer("handlers", method, params))
}

pub fn notification(method: &str, params: &Value) -> bool {
    layer("notifications", method, params)
}

fn layer(name: &str, method: &str, params: &Value) -> bool {
    let layer = &table()[name];
    layer["params"]
        .get(method)
        .is_none_or(|schema| valid(schema, params, &layer["definitions"]))
}

fn integer(value: &Value) -> bool {
    match value {
        Value::Bool(_) => true,
        Value::Number(n) => {
            let text = n.to_string();
            if !text.bytes().any(|b| matches!(b, b'.' | b'e' | b'E')) {
                return true;
            }
            n.as_f64().is_some_and(|n| {
                n.is_finite() && n.fract() == 0.0 && n.abs() < 9223372036854775808.0
            })
        }
        Value::String(s) => {
            static PATTERN: OnceLock<regex::Regex> = OnceLock::new();
            // Pydantic's integer string grammar permits underscores and a
            // zero fractional part, but not exponent notation or Unicode digits.
            let s = s.trim();
            PATTERN
                .get_or_init(|| regex::Regex::new(r"^[+-]?[0-9](?:_?[0-9])*(?:\.0+)?$").unwrap())
                .is_match(s)
                && s.trim_start_matches(['+', '-'])
                    .split('.')
                    .next()
                    .unwrap()
                    .replace('_', "")
                    .trim_start_matches('0')
                    .len()
                    <= 4300
        }
        _ => false,
    }
}

fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(v) => Some(u8::from(*v) as f64),
        Value::Number(n) => n.as_f64(),
        Value::String(s) => {
            let s = s.trim();
            // Underscores are accepted only between ASCII digits.
            let bytes = s.as_bytes();
            if bytes.iter().enumerate().any(|(i, b)| {
                *b == b'_'
                    && (i == 0
                        || i + 1 == bytes.len()
                        || !bytes[i - 1].is_ascii_digit()
                        || !bytes[i + 1].is_ascii_digit())
            }) {
                return None;
            }
            s.replace('_', "").parse().ok()
        }
        _ => None,
    }
}

fn boolean(value: &Value) -> bool {
    value.is_boolean()
        || value.as_f64().is_some_and(|n| n == 0.0 || n == 1.0)
        || value.as_str().is_some_and(|s| {
            matches!(
                s.to_ascii_lowercase().as_str(),
                "0" | "1" | "off" | "on" | "f" | "t" | "false" | "true" | "n" | "y" | "no" | "yes"
            )
        })
}

fn valid(schema: &Value, value: &Value, defs: &Value) -> bool {
    if let Some(allowed) = schema.as_bool() {
        return allowed;
    }
    if let Some(reference) = schema.get("$ref").and_then(Value::as_str) {
        return valid(
            defs.get(
                reference
                    .strip_prefix("#/$defs/")
                    .expect("local schema reference"),
            )
            .expect("defined SDK schema reference"),
            value,
            defs,
        );
    }
    if let Some(options) = schema.get("anyOf").and_then(Value::as_array) {
        return options.iter().any(|option| valid(option, value, defs));
    }
    if schema
        .get("const")
        .is_some_and(|expected| value != expected)
        || schema
            .get("enum")
            .and_then(Value::as_array)
            .is_some_and(|choices| !choices.contains(value))
    {
        return false;
    }
    let shape = match schema.get("type").and_then(Value::as_str) {
        None => true,
        Some("null") => value.is_null(),
        Some("boolean") => boolean(value),
        Some("integer") => integer(value),
        Some("number") => number(value).is_some(),
        Some("string") => value.as_str().is_some_and(|s| {
            let len = s.chars().count() as u64;
            schema
                .get("minLength")
                .and_then(Value::as_u64)
                .is_none_or(|min| len >= min)
                && schema
                    .get("maxLength")
                    .and_then(Value::as_u64)
                    .is_none_or(|max| len <= max)
                && (schema.get("format").and_then(Value::as_str) != Some("uri")
                    || reqwest::Url::parse(s).is_ok())
        }),
        Some("array") => value.as_array().is_some_and(|items| {
            schema
                .get("items")
                .is_none_or(|s| items.iter().all(|item| valid(s, item, defs)))
        }),
        Some("object") => value.as_object().is_some_and(|object| {
            if schema
                .get("required")
                .and_then(Value::as_array)
                .is_some_and(|required| {
                    required
                        .iter()
                        .any(|key| !object.contains_key(key.as_str().unwrap()))
                })
            {
                return false;
            }
            object.iter().all(|(key, value)| {
                schema
                    .get("properties")
                    .and_then(|p| p.get(key))
                    .or_else(|| schema.get("additionalProperties"))
                    .is_none_or(|s| valid(s, value, defs))
            })
        }),
        Some(other) => panic!("unsupported generated schema type {other}"),
    };
    shape
        && schema
            .get("minimum")
            .and_then(Value::as_f64)
            .is_none_or(|min| number(value).is_some_and(|n| n >= min))
        && schema
            .get("maximum")
            .and_then(Value::as_f64)
            .is_none_or(|max| number(value).is_some_and(|n| n <= max))
}
