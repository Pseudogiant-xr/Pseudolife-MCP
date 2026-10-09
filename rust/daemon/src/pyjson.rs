//! Python's `json.dumps(payload, default=str)` byte for byte (`web/api.py:103`):
//! `", "` and `": "` separators, `ensure_ascii=True`, Python's float `repr`,
//! integers as Python ints, keys in insertion order.
//!
//! The workspace builds `serde_json` with `arbitrary_precision`, so a number
//! read from the bank keeps its stored text (`1.10`, `-0`, `1E2`). Python
//! reads the same jsonb into `float`/`int` and writes it back in canonical
//! form; this writer does the same, so the daemon's answers never depend on
//! how a number was spelled in storage.

use serde_json::{Number, Value};
use std::fmt::Write;

/// Python `repr(float)`: the shortest round-trip digits, positional for
/// `1e-4 <= |x| < 1e16`, otherwise `d.ddde+XX` (two exponent digits minimum).
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "NaN".into(); // json.dumps(allow_nan=True)
    }
    if x.is_infinite() {
        return if x > 0.0 {
            "Infinity".into()
        } else {
            "-Infinity".into()
        };
    }
    let sci = format!("{x:e}"); // shortest round-trip digits, e.g. "-1.5e16"
    let (mantissa, exp) = sci.split_once('e').expect("LowerExp has an exponent");
    let exp: i32 = exp.parse().expect("integer exponent");
    if x != 0.0 && !(-4..16).contains(&exp) {
        let sign = if exp < 0 { '-' } else { '+' };
        return format!("{mantissa}e{sign}{:02}", exp.abs());
    }
    let mut s = format!("{x}");
    if !s.contains('.') {
        s.push_str(".0");
    }
    s
}

/// A JSON number as Python would hold it after `json.loads`: an int literal
/// stays an integer (`-0` is `0`), anything else is a float.
fn number_text(n: &Number) -> String {
    let raw = n.to_string();
    if !raw.contains(['.', 'e', 'E']) {
        let (neg, digits) = match raw.strip_prefix('-') {
            Some(d) => (true, d),
            None => (false, raw.as_str()),
        };
        let digits = digits.trim_start_matches('0');
        return match (neg, digits.is_empty()) {
            (_, true) => "0".into(),
            (true, false) => format!("-{digits}"),
            (false, false) => digits.to_string(),
        };
    }
    float_repr(raw.parse::<f64>().unwrap_or(f64::NAN))
}

fn string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            // json.encoder ESCAPE_ASCII: everything outside ' '..'~', DEL included.
            c if (c as u32) < 0x20
                || ((c as u32) > 0x7e
                    && !(c == '\u{7f}' && crate::mutants::active("del-unescaped"))) =>
            {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&number_text(n)),
        Value::String(s) => string(out, s),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (k, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                string(out, k);
                out.push_str(": ");
                write_value(out, item);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(v)`.
pub fn dumps(v: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, v);
    out
}

/// Python `str(x)` for a value loaded from JSON (`f"{value}"` in slot
/// tokens): ints and floats as Python prints them, `True`/`False`/`None`,
/// strings bare, lists and dicts as their repr.
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => number_text(n),
        Value::String(s) => crate::storage::py_repr(s),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(map) => {
            let inner: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("{}: {}", crate::storage::py_repr(k), py_repr(v)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn float_repr_matches_python() {
        // repr() in CPython 3.11, probed 2026-10-09.
        for (x, want) in [
            (1.1, "1.1"),
            (1.0, "1.0"),
            (-0.0, "-0.0"),
            (0.0, "0.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1e15, "1000000000000000.0"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.23e-7, "1.23e-07"),
            (1791516389.4399626, "1791516389.4399626"),
            (100.0, "100.0"),
            (1e100, "1e+100"),
            (0.1 + 0.2, "0.30000000000000004"),
        ] {
            assert_eq!(float_repr(x), want, "{x}");
        }
    }

    #[test]
    fn stored_number_spellings_come_out_as_python_writes_them() {
        let v: Value =
            serde_json::from_str(r#"[1.10, -0, -0.0, 1E2, 12345678901234567890, 0.5e1, 7]"#)
                .unwrap();
        assert_eq!(
            dumps(&v),
            "[1.1, 0, -0.0, 100.0, 12345678901234567890, 5.0, 7]"
        );
    }

    #[test]
    fn ascii_escapes_and_separators_match_json_dumps() {
        let v = json!({"q": "caf\u{e9} \u{1F600} \"x\"\n", "n": null, "b": [true, false]});
        assert_eq!(
            dumps(&v),
            // json.dumps in CPython 3.11, probed 2026-10-09.
            "{\"q\": \"caf\\u00e9 \\ud83d\\ude00 \\\"x\\\"\\n\", \"n\": null, \"b\": [true, false]}"
        );
    }

    #[test]
    fn del_is_escaped_like_json_dumps() {
        // json.dumps("a\x7fb") in CPython 3.11: "a\u007fb".
        assert_eq!(dumps(&json!("a\u{7f}b")), "\"a\\u007fb\"");
    }

    #[test]
    fn py_str_matches_python_formatting() {
        let v: Value =
            serde_json::from_str(r#"[1.10, true, null, "it's", ["a"], {"k": 1}]"#).unwrap();
        let got: Vec<String> = v.as_array().unwrap().iter().map(py_str).collect();
        assert_eq!(
            got,
            vec!["1.1", "True", "None", "it's", "['a']", "{'k': 1}"]
        );
    }
}
