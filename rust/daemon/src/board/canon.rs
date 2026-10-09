//! Python's `json.dumps` spellings the board hashes and stores (spec BA1-BA3):
//! `_canonical` (sorted keys, raw non-ASCII, compact), the audit row
//! material, the body commitment and the send fingerprint.

use serde_json::Value;
use sha2::{Digest, Sha256};

/// `AUDIT_FORMAT` (`storage/coordination.py:439`).
pub const AUDIT_FORMAT: &str = "pseudolife-coordination-audit-v1";
/// `GENESIS_HASH`.
pub const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// Whether a JSON number is a Python float after `json.loads` (it has a
/// fraction or an exponent); anything else is a Python int.
pub fn is_float_text(text: &str) -> bool {
    text.bytes().any(|b| matches!(b, b'.' | b'e' | b'E'))
}

/// `float.__repr__` as `json.dumps` writes a finite float: the shortest
/// round-trip digits, fixed notation for decimal exponents -4..=15, else
/// `d[.ddd]e+XX` with at least two exponent digits. Non-finite values are
/// `json.dumps`'s `NaN`, `Infinity` and `-Infinity`.
pub fn py_float(x: f64) -> String {
    if x.is_nan() {
        return "NaN".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    // Rust's LowerExp is the shortest round-trip spelling: "1.5e16", "-0e0".
    let sci = format!("{x:e}");
    let (mantissa, exp) = sci.split_once('e').expect("LowerExp has an exponent");
    let exp: i32 = exp.parse().expect("integer exponent");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..=15).contains(&exp) {
        let point = exp + 1;
        let body = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point as usize >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            let (whole, frac) = digits.split_at(point as usize);
            format!("{whole}.{frac}")
        };
        return format!("{sign}{body}");
    }
    let (first, rest) = digits.split_at(1);
    let frac = if rest.is_empty() {
        String::new()
    } else {
        format!(".{rest}")
    };
    let esign = if exp < 0 { '-' } else { '+' };
    format!("{sign}{first}{frac}e{esign}{:02}", exp.abs())
}

/// `json.dumps(s, ensure_ascii=False)` for one string.
pub fn py_str(s: &str, out: &mut String) {
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
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn number(text: &str, out: &mut String) {
    if is_float_text(text) {
        out.push_str(&py_float(text.parse::<f64>().unwrap_or(f64::NAN)));
    } else if text == "-0" {
        out.push('0');
    } else {
        out.push_str(text);
    }
}

fn write(value: &Value, sort: bool, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => number(&n.to_string(), out),
        Value::String(s) => py_str(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(item, sort, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut keys: Vec<&String> = map.keys().collect();
            if sort {
                // Python sorts str keys by code point; UTF-8 byte order agrees.
                keys.sort();
            }
            for (i, key) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                py_str(key, out);
                out.push(':');
                write(&map[key], sort, out);
            }
            out.push('}');
        }
    }
}

/// `_canonical`: `json.dumps(value, ensure_ascii=False, sort_keys=True,
/// separators=(",", ":"))`.
pub fn canonical(value: &Value) -> String {
    let mut out = String::new();
    write(value, true, &mut out);
    out
}

/// `json.dumps(value, ensure_ascii=False, separators=(",", ":"))`, keys in
/// insertion order (used only on lists of scalars here).
pub fn compact(value: &Value) -> String {
    let mut out = String::new();
    write(value, false, &mut out);
    out
}

pub fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// `_hash`: sha256 of the UTF-8 text, lowercase hex.
pub fn hash_text(text: &str) -> String {
    sha256_hex(text.as_bytes())
}

/// The hashed columns of one audit row (`audit_hash`'s material).
pub struct AuditRow<'a> {
    pub seq: i64,
    pub event: &'a str,
    pub actor: &'a str,
    pub principal: &'a str,
    pub agent_id: &'a str,
    pub recipient_agent_id: Option<&'a str>,
    pub project: &'a str,
    pub task: &'a str,
    pub message_id: Option<&'a str>,
    pub created_at: f64,
    pub hlc: &'a str,
    pub payload: &'a str,
}

/// `audit_hash(prev_hash, row)` (`storage/coordination.py:957-971`).
pub fn audit_hash(prev_hash: &str, row: &AuditRow<'_>) -> String {
    let mut m = String::from("[");
    py_str(AUDIT_FORMAT, &mut m);
    m.push(',');
    m.push_str(&row.seq.to_string());
    for s in [row.event, row.actor, row.principal, row.agent_id] {
        m.push(',');
        py_str(s, &mut m);
    }
    m.push(',');
    match row.recipient_agent_id {
        Some(r) => py_str(r, &mut m),
        None => m.push_str("null"),
    }
    for s in [row.project, row.task] {
        m.push(',');
        py_str(s, &mut m);
    }
    m.push(',');
    match row.message_id {
        Some(r) => py_str(r, &mut m),
        None => m.push_str("null"),
    }
    m.push(',');
    m.push_str(&py_float(row.created_at));
    m.push(',');
    py_str(row.hlc, &mut m);
    m.push(',');
    py_str(row.payload, &mut m);
    m.push(']');
    let mut material = String::with_capacity(prev_hash.len() + m.len());
    material.push_str(prev_hash);
    material.push_str(&m);
    sha256_hex(material.as_bytes())
}

/// `body_commitment(salt_hex, body)`: sha256(bytes.fromhex(salt) || utf8(body)).
pub fn body_commitment(salt_hex: &str, body: &str) -> String {
    let mut bytes = hex::decode(salt_hex).expect("salt written as 32 hex characters");
    bytes.extend_from_slice(body.as_bytes());
    sha256_hex(&bytes)
}

/// `_fingerprint` (`storage/coordination.py:698-706`).
pub fn fingerprint(
    to: &str,
    text: &str,
    reply_to: Option<&str>,
    expires_at: Option<f64>,
    clears: Option<&str>,
    urgent: bool,
) -> String {
    let mut items = vec![
        Value::String(to.to_string()),
        Value::String(text.to_string()),
        reply_to.map_or(Value::Null, |r| Value::String(r.to_string())),
        expires_at.map_or(Value::Null, float_value),
    ];
    if clears.is_some() || urgent {
        items.push(clears.map_or(Value::Null, |c| Value::String(c.to_string())));
        items.push(Value::Bool(urgent));
    }
    hash_text(&compact(&Value::Array(items)))
}

/// A Python float as a JSON value (its text keeps the fraction, so the
/// canonical writer spells it back as a float).
pub fn float_value(x: f64) -> Value {
    serde_json::Number::from_f64(x).map_or(Value::Null, Value::Number)
}

/// A JSON number read back as Python would: `Some(f64)` for any number.
pub fn as_f64(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.to_string().parse::<f64>().ok(),
        _ => None,
    }
}

/// `type(v) is int` after `json.loads`: an integer literal, never a bool.
pub fn is_py_int(v: &Value) -> bool {
    matches!(v, Value::Number(n) if !is_float_text(&n.to_string()))
}

/// The value of a Python int that fits an i64; None for a float, a bool, or
/// an int beyond i64 (Python keeps those exact; the board's columns cannot).
pub fn py_int_i64(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) if !is_float_text(&n.to_string()) => n.to_string().parse::<i64>().ok(),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn python_float_repr() {
        // Values checked against CPython 3.11 `repr`.
        let cases: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (60.0, "60.0"),
            (1.5, "1.5"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1e15, "1000000000000000.0"),
            (123456789012345.6, "123456789012345.6"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.25e-7, "1.25e-07"),
            (1791519474.147151, "1791519474.147151"),
            (1791600000.0, "1791600000.0"),
            (1e100, "1e+100"),
            (-2.5e-300, "-2.5e-300"),
            (0.1 + 0.2, "0.30000000000000004"),
        ];
        for (x, want) in cases {
            assert_eq!(py_float(*x), *want, "{x}");
        }
    }

    #[test]
    fn canonical_matches_python_json_dumps() {
        // json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        let v = json!({"b": [1, 2.0, null, true], "a": "é\n\u{1}\"\\", "Z": {"y": 1, "x": "/"}});
        assert_eq!(
            canonical(&v),
            "{\"Z\":{\"x\":\"/\",\"y\":1},\"a\":\"é\\n\\u0001\\\"\\\\\",\"b\":[1,2.0,null,true]}"
        );
        assert_eq!(canonical(&json!({"\u{e9}": 1, "z": 2})), "{\"z\":2,\"é\":1}");
    }

    #[test]
    fn audit_hash_golden() {
        // Computed with CPython: audit_hash("0"*64, {...}) from storage/coordination.py.
        let row = AuditRow {
            seq: 1,
            event: "bank_identity",
            actor: "agent",
            principal: "default",
            agent_id: "",
            recipient_agent_id: None,
            project: "",
            task: "",
            message_id: None,
            created_at: 1791519474.5,
            hlc: "",
            payload: "{\"bank_id\":\"00000000-0000-4000-8000-000000000000\"}",
        };
        let mut material = String::from("0".repeat(64));
        material.push_str("[\"pseudolife-coordination-audit-v1\",1,\"bank_identity\",\"agent\",\"default\",\"\",null,\"\",\"\",null,1791519474.5,\"\",\"{\\\"bank_id\\\":\\\"00000000-0000-4000-8000-000000000000\\\"}\"]");
        assert_eq!(audit_hash(GENESIS_HASH, &row), sha256_hex(material.as_bytes()));
    }

    #[test]
    fn fingerprint_shape() {
        let plain = fingerprint("abc", "hi", None, Some(10.5), None, false);
        assert_eq!(plain, hash_text("[\"abc\",\"hi\",null,10.5]"));
        let urgent = fingerprint("abc", "hi", None, None, None, true);
        assert_eq!(urgent, hash_text("[\"abc\",\"hi\",null,null,null,true]"));
    }
}
