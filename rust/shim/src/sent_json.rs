//! Own response bytes for the sent HTTP slice; no Python serialization bridge.
//!
//! Objects retain producer order, including database-observed JSONB order.
use std::fmt::Write as _;

#[derive(Clone, Debug)]
pub enum Json {
    Null,
    Bool(bool),
    Integer(String),
    Float(f64),
    String(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
    // Supplemental default=str producers are not reachable from sent SQL.
    #[cfg(test)]
    FixtureUuid(uuid::Uuid),
    #[cfg(test)]
    FixtureTimestamp(chrono::NaiveDateTime),
    #[cfg(test)]
    FixtureUtcTimestamp(chrono::DateTime<chrono::Utc>),
}

#[derive(Debug)]
pub struct InvalidInteger;

fn quoted(text: &str, out: &mut String) {
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
                    let high = 0xd800 + (point >> 10);
                    let low = 0xdc00 + (point & 0x3ff);
                    let _ = write!(out, "\\u{high:04x}\\u{low:04x}");
                }
            }
        }
    }
    out.push('"');
}

fn float(value: f64, out: &mut String) -> Result<(), InvalidInteger> {
    #[cfg(not(test))]
    if !value.is_finite() {
        return Err(InvalidInteger);
    }
    #[cfg(test)]
    if value.is_nan() {
        out.push_str("NaN");
        return Ok(());
    }
    #[cfg(test)]
    if value.is_infinite() {
        out.push_str(if value.is_sign_negative() {
            "-Infinity"
        } else {
            "Infinity"
        });
        return Ok(());
    }
    out.push_str(&crate::float_repr::finite_float(value).map_err(|_| InvalidInteger)?);
    Ok(())
}

fn append(value: &Json, out: &mut String) -> Result<(), InvalidInteger> {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(value) => out.push_str(if *value { "true" } else { "false" }),
        Json::Integer(value) => {
            let digits = value.strip_prefix('-').unwrap_or(value);
            if digits.is_empty()
                || !digits.bytes().all(|c| c.is_ascii_digit())
                || (digits.len() > 1 && digits.starts_with('0'))
                || value == "-0"
            {
                return Err(InvalidInteger);
            }
            out.push_str(value);
        }
        Json::Float(value) => float(*value, out)?,
        Json::String(value) => quoted(value, out),
        Json::Array(values) => {
            out.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                append(value, out)?;
            }
            out.push(']');
        }
        Json::Object(values) => {
            out.push('{');
            for (index, (key, value)) in values.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                quoted(key, out);
                out.push_str(": ");
                append(value, out)?;
            }
            out.push('}');
        }
        #[cfg(test)]
        Json::FixtureUuid(value) => quoted(&value.to_string(), out),
        #[cfg(test)]
        Json::FixtureTimestamp(value) => {
            let mut text = value.format("%Y-%m-%d %H:%M:%S").to_string();
            let micros = value.and_utc().timestamp_subsec_micros();
            if micros != 0 {
                let _ = write!(text, ".{micros:06}");
            }
            quoted(&text, out);
        }
        #[cfg(test)]
        Json::FixtureUtcTimestamp(value) => {
            let mut text = value.format("%Y-%m-%d %H:%M:%S").to_string();
            let micros = value.timestamp_subsec_micros();
            if micros != 0 {
                let _ = write!(text, ".{micros:06}");
            }
            text.push_str("+00:00");
            quoted(&text, out);
        }
    }
    Ok(())
}

pub fn encode(value: &Json) -> Result<Vec<u8>, InvalidInteger> {
    let mut out = String::new();
    append(value, &mut out)?;
    Ok(out.into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_float_shortest_ties() {
        // Python writer: pseudolife_memory/web/api.py:103 (json.dumps).
        let mut wrong = Vec::new();
        for (bits, expected) in [
            (0x430c6bf526340002, "1000000000000000.2"),
            (0xc308130f222a4572, "-847044394961070.2"),
            (0x42d526daef896bc8, "93026504287663.12"),
        ] {
            let actual = encode(&Json::Float(f64::from_bits(bits))).unwrap();
            if actual != expected.as_bytes() {
                wrong.push(format!(
                    "{bits:016x}: {} != {expected}",
                    String::from_utf8(actual).unwrap()
                ));
            }
        }
        assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    }

    #[test]
    fn python_float_full_cpython_table() {
        let table = include_str!("../tests/cli_audit_float_repr.tsv");
        let mut count = 0;
        for line in table
            .lines()
            .filter(|line| !line.starts_with('#') && !line.is_empty())
        {
            let (bits, expected) = line.split_once('\t').unwrap();
            let value = f64::from_bits(u64::from_str_radix(bits, 16).unwrap());
            assert_eq!(
                encode(&Json::Float(value)).unwrap(),
                expected.as_bytes(),
                "bits {bits}"
            );
            count += 1;
        }
        assert_eq!(count, 5358);
    }

    #[test]
    fn python_float_spelling() {
        for (value, expected) in [
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (1e-7, "1e-07"),
            (-0.0, "-0.0"),
            (0.0, "0.0"),
            (1.25, "1.25"),
            (f64::INFINITY, "Infinity"),
        ] {
            assert_eq!(encode(&Json::Float(value)).unwrap(), expected.as_bytes());
        }
    }

    #[test]
    fn named_default_str_producers() {
        let uuid = uuid::Uuid::parse_str("12345678-1234-5678-1234-567812345678").unwrap();
        assert_eq!(
            encode(&Json::FixtureUuid(uuid)).unwrap(),
            br#""12345678-1234-5678-1234-567812345678""#
        );
        let timestamp = chrono::NaiveDate::from_ymd_opt(2026, 1, 2)
            .unwrap()
            .and_hms_micro_opt(3, 4, 5, 123456)
            .unwrap();
        assert_eq!(
            encode(&Json::FixtureTimestamp(timestamp)).unwrap(),
            br#""2026-01-02 03:04:05.123456""#
        );
        assert_eq!(
            encode(&Json::FixtureUtcTimestamp(timestamp.and_utc())).unwrap(),
            br#""2026-01-02 03:04:05.123456+00:00""#
        );
    }

    #[test]
    fn integers_and_producer_order_are_not_normalized() {
        let value = Json::Object(vec![
            (
                "a".into(),
                Json::Integer("10000000000000000000000000000000000000000".into()),
            ),
            ("b".into(), Json::Array(vec![Json::Null, Json::Bool(false)])),
            ("aa".into(), Json::Object(vec![])),
        ]);
        assert_eq!(
            encode(&value).unwrap(),
            br#"{"a": 10000000000000000000000000000000000000000, "b": [null, false], "aa": {}}"#
        );
        assert!(encode(&Json::Integer("01".into())).is_err());
        assert!(encode(&Json::Integer("1.0".into())).is_err());
    }

    #[test]
    fn separators_and_ascii_escape() {
        let value = Json::Object(vec![
            ("text".into(), Json::String("é雪😀\n\"\\\u{7f}".into())),
            (
                "items".into(),
                Json::Array(vec![Json::Integer("1".into()), Json::Float(1.0)]),
            ),
        ]);
        assert_eq!(
            encode(&value).unwrap(),
            br#"{"text": "\u00e9\u96ea\ud83d\ude00\n\"\\\u007f", "items": [1, 1.0]}"#
        );
    }
}
