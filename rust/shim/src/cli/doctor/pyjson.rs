//! `json.dumps(report, indent=2)`: ensure_ascii escaping, `", "`-free item
//! separators with newlines, `": "` key separators and insertion order.
use serde_json::Value;

/// Python's integer spelling of a JSON integer token (`-0` is `0`).
pub(super) fn python_int(text: &str) -> String {
    if text
        .strip_prefix('-')
        .is_some_and(|rest| rest.bytes().all(|b| b == b'0'))
    {
        "0".into()
    } else {
        text.into()
    }
}

/// True for a JSON number token Python's decoder makes an `int`.
pub(super) fn is_int_token(text: &str) -> bool {
    !text.contains(['.', 'e', 'E'])
}

fn escape(out: &mut String, text: &str) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(character),
            other => {
                let mut units = [0u16; 2];
                for unit in other.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn write(out: &mut String, value: &Value, level: usize) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        // The report carries integers only (counts and the daemon's caps).
        Value::Number(number) => out.push_str(&python_int(&number.to_string())),
        Value::String(text) => escape(out, text),
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Object(items) if items.is_empty() => out.push_str("{}"),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                newline(out, level + 1);
                write(out, item, level + 1);
            }
            newline(out, level);
            out.push(']');
        }
        Value::Object(items) => {
            out.push('{');
            for (index, (key, item)) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                newline(out, level + 1);
                escape(out, key);
                out.push_str(": ");
                write(out, item, level + 1);
            }
            newline(out, level);
            out.push('}');
        }
    }
}

fn newline(out: &mut String, level: usize) {
    out.push('\n');
    out.push_str(&"  ".repeat(level));
}

pub(super) fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write(&mut out, value, 0);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn indent_and_ascii_escaping_follow_python() {
        let value: Value = serde_json::from_str(
            r#"{"a": [], "b": {}, "c": [1, "x"], "d": {"e": null, "f": true}, "g": "é😀\u007f\u0001\"\\\n"}"#,
        )
        .unwrap();
        assert_eq!(
            dumps(&value),
            "{\n  \"a\": [],\n  \"b\": {},\n  \"c\": [\n    1,\n    \"x\"\n  ],\n  \"d\": {\n    \"e\": null,\n    \"f\": true\n  },\n  \"g\": \"\\u00e9\\ud83d\\ude00\\u007f\\u0001\\\"\\\\\\n\"\n}"
        );
    }

    #[test]
    fn negative_zero_integer_is_zero() {
        assert_eq!(python_int("-0"), "0");
        assert_eq!(python_int("-5"), "-5");
        assert!(is_int_token("12") && !is_int_token("1.0") && !is_int_token("1e2"));
    }
}
