//! Strict hook JSON with bounded nesting and arbitrary-precision ignored numbers.
use serde_json::Value;

pub(super) fn input(text: &str) -> Result<Value, ()> {
    // Keep serde_json's default nesting limit. Do not recreate interpreter stack
    // limits or disable the bound for untrusted host input and daemon metadata.
    serde_json::from_str(text).map_err(|_| ())
}

pub(super) fn quoted(text: &str) -> String {
    use std::fmt::Write;
    let mut output = String::from("\"");
    for c in text.chars() {
        match c {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{8}' => output.push_str("\\b"),
            '\u{c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            ' '..='~' => output.push(c),
            _ => {
                let mut units = [0; 2];
                for unit in c.encode_utf16(&mut units) {
                    let _ = write!(output, "\\u{unit:04x}");
                }
            }
        }
    }
    output.push('"');
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ignored_metadata_retains_valid_large_numbers_and_duplicate_keys() {
        let value = input(r#"{"session_id":"old","session_id":"sess-1","other":{"n":18446744073709551616,"f":1e400},"markdown":"ok"}"#).unwrap();
        assert_eq!(value["session_id"], "sess-1");
        assert_eq!(value["markdown"], "ok");
        assert!(
            input(&format!(
                "{{\"other\":{},\"markdown\":\"ok\"}}",
                "9".repeat(5000)
            ))
            .is_ok()
        );
    }

    #[test]
    fn malformed_ignored_fields_and_hostile_depth_are_refused() {
        for text in [
            r#"{"session_id":"sess-1","other":NaN}"#,
            r#"{"session_id":"sess-1","other":Infinity}"#,
            r#"{"session_id":"sess-1","other":-Infinity}"#,
            r#"{"session_id":"sess-1","other":"\ud800"}"#,
            r#"{"session_id":"sess-1","\udfff":0}"#,
            r#"{"session_id":"sess-1","other":"\ud800","other":0}"#,
            r#"{"session_id":"sess-1"} trailing"#,
        ] {
            assert!(input(text).is_err());
        }
        assert!(input(&format!("{}0{}", "[".repeat(10000), "]".repeat(10000))).is_err());
    }

    #[test]
    fn wire_escaping_retains_ascii_json_and_surrogate_pairs() {
        assert_eq!(
            quoted(" café — 🐍\n\u{1c}\"\\"),
            "\" caf\\u00e9 \\u2014 \\ud83d\\udc0d\\n\\u001c\\\"\\\\\""
        );
    }
}
