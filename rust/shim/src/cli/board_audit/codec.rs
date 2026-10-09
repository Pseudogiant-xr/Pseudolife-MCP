//! Compact audit hash bytes, separate from the ordinary ASCII response codec.
use serde::Serialize;
use serde_json::{Value, ser::Formatter};
use sha2::{Digest, Sha256};
use std::{fmt::Write as _, io};

pub(super) fn finite_float(value: f64) -> io::Result<String> {
    if !value.is_finite() {
        return Err(io::Error::other("nonfinite audit number is deferred"));
    }
    if value == 0.0 {
        return Ok(if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into());
    }
    // Locked serde_json supplies shortest digits; this reshapes notation only.
    // Exact agreement with every CPython binary64 tie is not yet qualified.
    let text = serde_json::to_string(&value.abs())?;
    let (mantissa, power) = text
        .split_once(['e', 'E'])
        .map_or((text.as_str(), 0), |(m, p)| {
            (m, p.parse::<i32>().expect("finite float exponent"))
        });
    let decimal = mantissa.find('.').unwrap_or(mantissa.len());
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let first = digits.find(|c| c != '0').expect("nonzero finite float");
    let significant = digits[first..].trim_end_matches('0');
    let exponent = decimal as i32 - first as i32 - 1 + power;
    let mut out = String::new();
    if value.is_sign_negative() {
        out.push('-');
    }
    if !(-4..16).contains(&exponent) {
        out.push(significant.as_bytes()[0] as char);
        if significant.len() > 1 {
            out.push('.');
            out.push_str(&significant[1..]);
        }
        let sign = if exponent < 0 { '-' } else { '+' };
        let _ = write!(out, "e{sign}{:02}", exponent.abs());
    } else {
        let point = exponent + 1;
        if point <= 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n('0', (-point) as usize));
            out.push_str(significant);
        } else if point as usize >= significant.len() {
            out.push_str(significant);
            out.extend(std::iter::repeat_n('0', point as usize - significant.len()));
            out.push_str(".0");
        } else {
            let point = point as usize;
            out.push_str(&significant[..point]);
            out.push('.');
            out.push_str(&significant[point..]);
        }
    }
    Ok(out)
}

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
        let mut value = row["payload"].clone();
        value.sort_all_objects();
        String::from_utf8(compact(&value)?).expect("JSON is UTF-8")
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

#[cfg(test)]
mod tests {
    use super::*;

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
