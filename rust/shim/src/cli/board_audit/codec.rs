//! Compact audit hash bytes, separate from the ordinary ASCII response codec.
use serde::Serialize;
use serde_json::{Value, ser::Formatter};
use sha2::{Digest, Sha256};
use std::{fmt::Write as _, io};

#[path = "../../../../shared/float_repr.rs"]
mod float_repr;
use float_repr::finite_float;

struct AuditFormatter;

impl Formatter for AuditFormatter {
    fn write_f64<W: io::Write + ?Sized>(&mut self, writer: &mut W, value: f64) -> io::Result<()> {
        writer.write_all(finite_float(value)?.as_bytes())
    }

    fn write_number_str<W: io::Write + ?Sized>(
        &mut self,
        writer: &mut W,
        value: &str,
    ) -> io::Result<()> {
        if value.contains(['.', 'e', 'E']) {
            let number = value.parse::<f64>().map_err(io::Error::other)?;
            self.write_f64(writer, number)
        } else {
            writer.write_all(if value == "-0" {
                b"0"
            } else {
                value.as_bytes()
            })
        }
    }
}

fn compact(value: &impl Serialize) -> Result<Vec<u8>, serde_json::Error> {
    let mut serializer = serde_json::Serializer::with_formatter(Vec::new(), AuditFormatter);
    value.serialize(&mut serializer)?;
    Ok(serializer.into_inner())
}

pub(super) fn hash(row: &Value, created_at: f64) -> Result<String, serde_json::Error> {
    let payload = if let Some(text) = row["payload"].as_str() {
        text.to_owned()
    } else {
        canonical(&row["payload"])?
    };
    let material = compact(&(
        "pseudolife-coordination-audit-v1",
        &row["seq"],
        &row["event"],
        &row["actor"],
        &row["principal"],
        &row["agent_id"],
        &row["recipient_agent_id"],
        &row["project"],
        &row["task"],
        &row["message_id"],
        created_at,
        &row["hlc"],
        payload,
    ))?;
    let mut hasher = Sha256::new();
    hasher.update(row["prev_hash"].as_str().expect("validated previous hash"));
    hasher.update(material);
    Ok(format!("{:x}", hasher.finalize()))
}

/// `_canonical`: `json.dumps(sort_keys=True, ensure_ascii=False)`, compact.
pub(super) fn canonical(value: &Value) -> Result<String, serde_json::Error> {
    let mut value = value.clone();
    value.sort_all_objects();
    Ok(String::from_utf8(compact(&value)?).expect("JSON is UTF-8"))
}

/// CPython's default `int` conversion refuses more than 4,300 digits, so a
/// value `json.loads` cannot read is a deferral here, never a reading.
pub(super) fn python_int_domain(value: &Value) -> bool {
    match value {
        Value::Number(number) => {
            let token = number.to_string();
            token.contains(['.', 'e', 'E']) || token.trim_start_matches('-').len() <= 4300
        }
        Value::Array(items) => items.iter().all(python_int_domain),
        Value::Object(map) => map.values().all(python_int_domain),
        _ => true,
    }
}

/// `json.loads` of stored payload text, or `None` where Python's reading is
/// not reproduced (NaN/Infinity tokens, lone surrogates, deep nesting, the
/// integer digit limit, or text Python would refuse).
pub(super) fn python_loads(text: &str) -> Option<Value> {
    super::super::doorbell_seen::json::from_str(text)
        .ok()
        .filter(python_int_domain)
}

fn ascii_string(text: &str, out: &mut String) {
    out.push('"');
    for value in text.chars() {
        match value {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{20}'..='\u{7e}' => out.push(value),
            value => {
                let point = value as u32;
                if point <= 0xffff {
                    let _ = write!(out, "\\u{point:04x}");
                } else {
                    let point = point - 0x10000;
                    let _ = write!(
                        out,
                        "\\u{:04x}\\u{:04x}",
                        0xd800 + (point >> 10),
                        0xdc00 + (point & 0x3ff)
                    );
                }
            }
        }
    }
    out.push('"');
}

/// `json.dumps(value, ensure_ascii=True, separators=(",", ":"))` of a value
/// Python read with `json.loads`: integers print as Python ints, floats as
/// float repr, object order as read. `None` defers (a non-finite float).
pub(super) fn ascii_compact(value: &Value, out: &mut String) -> Option<()> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => {
            let token = number.to_string();
            if token.contains(['.', 'e', 'E']) {
                out.push_str(&finite_float(token.parse::<f64>().ok()?).ok()?);
            } else if token == "-0" {
                out.push('0');
            } else {
                out.push_str(&token);
            }
        }
        Value::String(text) => ascii_string(text, out),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                ascii_compact(item, out)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                ascii_string(key, out);
                out.push(':');
                ascii_compact(item, out)?;
            }
            out.push('}');
        }
    }
    Some(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Random and boundary binary64 values against CPython's own repr,
    /// recorded once from the oracle interpreter.
    #[test]
    fn finite_float_matches_cpython_repr_table() {
        let table = include_str!("../../../tests/cli_audit_float_repr.tsv");
        let mut checked = 0;
        for line in table.lines().filter(|line| !line.starts_with('#')) {
            let (bits, expected) = line.split_once('\t').expect("bits TAB repr");
            let value = f64::from_bits(u64::from_str_radix(bits, 16).expect("hex bits"));
            assert_eq!(finite_float(value).unwrap(), expected, "bits {bits}");
            checked += 1;
        }
        assert!(checked > 5000, "{checked}");
    }

    #[test]
    fn scalar_bytes_and_number_token_classes() {
        let mut value: Value = serde_json::from_str(
            r#"{"z":1e0,"a":{"z":-0.0,"a":9007199254740993},"text":"é雪😀\u007f\n\"\\"}"#,
        )
        .unwrap();
        value.sort_all_objects();
        assert_eq!(
            String::from_utf8(compact(&value).unwrap()).unwrap(),
            "{\"a\":{\"a\":9007199254740993,\"z\":-0.0},\"text\":\"é雪😀\u{7f}\\n\\\"\\\\\",\"z\":1.0}"
        );
        for (value, expected) in [
            (1000.0, "1000.0"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (f64::from_bits(1), "5e-324"),
        ] {
            assert_eq!(finite_float(value).unwrap(), expected);
        }
        assert!(finite_float(f64::INFINITY).is_err());
    }
}
