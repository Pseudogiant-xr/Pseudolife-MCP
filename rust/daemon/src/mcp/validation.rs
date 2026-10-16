//! Pinned SDK parameter validation. The generated table contains both the
//! protocol schema and the registered handler's schema; they are separate
//! validation stages in MCP 2.2.0. See harness/mcp_validation_schema.py.

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

/// The SDK transport parses bodies with `pydantic_core.from_json` (jiter),
/// which refuses a number whose integer part, sign included, is longer than
/// 4,300 characters ("number out of range"): a parse error before any
/// validation. `serde_json` with arbitrary precision keeps such literals.
pub fn number_out_of_range(value: &Value) -> bool {
    match value {
        Value::Number(n) => {
            let text = n.to_string();
            text.split(['.', 'e', 'E']).next().map_or(0, str::len) > 4300
        }
        Value::Array(items) => items.iter().any(number_out_of_range),
        Value::Object(map) => map.values().any(number_out_of_range),
        _ => false,
    }
}

pub fn notification(method: &str, params: &Value) -> bool {
    layer("notifications", method, params)
}

/// This slice never mints sealed round-trip state. Refuse an inbound token
/// before dispatch, as the SDK middleware does for an untrusted token.
pub fn has_untrusted_state(method: &str, params: &serde_json::Map<String, Value>) -> bool {
    table()["sealed_state_methods"]
        .as_array()
        .expect("state method table")
        .iter()
        .any(|name| name.as_str() == Some(method))
        && params.get("requestState").is_some_and(|v| !v.is_null())
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
        Value::String(s) => integer_string(s),
        _ => false,
    }
}

fn integer_string(raw: &str) -> bool {
    clean_int_string(raw).is_some()
}

/// The pinned SDK's clean_int_str order, including its permissive
/// leading-zero cleanup, then the resulting JSON integer spelling: the
/// integer's canonical digits (with `-` when negative), or None.
pub(super) fn clean_int_string(raw: &str) -> Option<String> {
    let mut s = raw.trim();
    if let Some(rest) = s.strip_prefix('+') {
        if rest.starts_with('-') {
            return None;
        }
        s = rest;
    }
    let negative = s.starts_with('-');
    if negative {
        s = &s[1..];
        if s.starts_with(['-', '+']) {
            return None;
        }
    }
    match s.as_bytes().first() {
        Some(b'0') => {
            let mut start = s.len() - 1;
            for (index, byte) in s.bytes().enumerate().skip(1) {
                match byte {
                    b'0' | b'_' => continue,
                    b'1'..=b'9' | b'-' => start = index,
                    b'.' => start = index - 1,
                    _ => return None,
                }
                break;
            }
            s = &s[start..];
        }
        Some(b'1'..=b'9' | b'-') => (),
        _ => return None,
    }
    if let Some((whole, fraction)) = s.split_once('.')
        && !fraction.is_empty()
        && fraction.bytes().all(|b| b == b'0')
    {
        s = whole;
    }
    let mut cleaned =
        if s.contains('_') && !s.starts_with('_') && !s.ends_with('_') && !s.contains("__") {
            s.replace('_', "")
        } else {
            s.to_owned()
        };
    if negative {
        cleaned.insert(0, '-');
    }
    // The pinned integer parser's 4,300-byte limit includes a minus sign.
    let digits = cleaned.strip_prefix('-').unwrap_or(&cleaned);
    let valid = cleaned.len() <= 4300
        && !digits.is_empty()
        && (digits.len() == 1 || !digits.starts_with('0'))
        && digits.bytes().all(|b| b.is_ascii_digit());
    let zero = digits == "0";
    // Python's int has no negative zero.
    valid.then(|| if zero { "0".to_string() } else { cleaned })
}

pub(super) fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(v) => Some(u8::from(*v) as f64),
        // The SDK preserves overflow literals as infinity for float fields.
        Value::Number(n) => n.to_string().parse().ok(),
        Value::String(s) => s.trim().parse().ok().or_else(|| {
            // The SDK retries underscore removal on the original string,
            // without trimming again or requiring digit-adjacent separators.
            if s.starts_with('_') || s.ends_with('_') || !s.contains('_') || s.contains("__") {
                None
            } else {
                s.replace('_', "").parse().ok()
            }
        }),
        _ => None,
    }
}

pub(super) fn boolean(value: &Value) -> bool {
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
                    || reqwest::Url::parse(s).is_ok_and(|url| {
                        schema
                            .get("x-allowed-schemes")
                            .and_then(Value::as_array)
                            .is_none_or(|schemes| {
                                schemes
                                    .iter()
                                    .any(|scheme| scheme.as_str() == Some(url.scheme()))
                            })
                    }))
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sdk_number_accepts_cleaned_underscores() {
        assert_eq!(number(&json!("0_._1")), Some(0.1));
    }

    #[test]
    fn sdk_number_refuses_whitespace_after_underscore_cleanup() {
        assert_eq!(number(&json!(" 0_1 ")), None);
    }

    #[test]
    fn sdk_file_urls_refuse_other_schemes() {
        for method in ["prompts/get", "tools/call"] {
            for (uri, expected) in [
                ("file:///fixture", true),
                ("https://example.test/resource", false),
            ] {
                let params = json!({"name":"memory_toolset", "arguments":{},
                    "inputResponses":{"fixture":{"roots":[{"uri":uri}]}}});
                assert_eq!(request(method, &params, true), expected, "{method}: {uri}");
            }
        }
    }
}
