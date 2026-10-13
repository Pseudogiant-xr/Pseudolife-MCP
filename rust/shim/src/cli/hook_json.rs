//! Strict hook JSON with bounded nesting and arbitrary-precision ignored numbers.
use serde_json::Value;

pub(super) fn input(text: &str) -> Result<Value, ()> {
    // Keep serde_json's default nesting limit. Do not recreate interpreter stack
    // limits or disable the bound for untrusted host input and daemon metadata.
    super::doorbell_seen::json::value_from_str(text).map_err(|_| ())
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
    fn private_number_keys_remain_ordinary_hook_metadata() {
        // briefing_cli.py:55,183: json.loads retains nested objects verbatim.
        for token in ["not a number", "123"] {
            let text = format!(
                r#"{{"session_id":"sess-1","markdown":"ok","other":[{{"$serde_json::private::Number":"{token}"}}]}}"#
            );
            let value = input(&text).unwrap();
            assert!(value["other"][0].is_object());
            assert_eq!(value["other"][0]["$serde_json::private::Number"], token);
        }
    }

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
    fn bounded_json_nesting_records_the_native_boundary() {
        let arrays = |depth| format!("{}0{}", "[".repeat(depth), "]".repeat(depth));
        let first_refused = (1..=200)
            .find(|depth| input(&arrays(*depth)).is_err())
            .unwrap();
        eprintln!(
            "hook-bounded-json-nesting: accepted array depth {}; refused {}",
            first_refused - 1,
            first_refused
        );
        assert_eq!(first_refused, 128);
        let metadata = |depth| format!("{{\"session_id\":\"sess-1\",\"other\":{}}}", arrays(depth));
        assert!(input(&metadata(126)).is_ok());
        assert!(input(&metadata(127)).is_err());
        eprintln!(
            "hook-bounded-json-nesting: accepted object plus 126 arrays; refused object plus 127 arrays"
        );
    }

    #[test]
    fn wire_escaping_retains_ascii_json_and_surrogate_pairs() {
        assert_eq!(
            quoted(" café — 🐍\n\u{1c}\"\\"),
            "\" caf\\u00e9 \\u2014 \\ud83d\\udc0d\\n\\u001c\\\"\\\\\""
        );
    }
}
