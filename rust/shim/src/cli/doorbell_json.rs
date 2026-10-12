//! Standard scalar JSON under serde's ordinary 128-level recursion budget.
pub(super) use serde_json::Value;

pub(in crate::cli) fn from_str(text: &str) -> Result<Value, ()> {
    // arbitrary_precision preserves ignored integer tokens, but also admits
    // overflowing float tokens. Check every token, including overwritten keys,
    // before serde applies last-value-wins object semantics.
    let bytes = text.as_bytes();
    let mut position = 0;
    while position < bytes.len() {
        match bytes[position] {
            b'"' => skip_string(bytes, &mut position),
            b'-' | b'0'..=b'9' => {
                let start = position;
                position += 1;
                while position < bytes.len()
                    && matches!(
                        bytes[position],
                        b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-'
                    )
                {
                    position += 1;
                }
                let token = &text[start..position];
                if token.contains(['.', 'e', 'E'])
                    && !token.parse::<f64>().is_ok_and(f64::is_finite)
                {
                    return Err(());
                }
            }
            _ => position += 1,
        }
    }
    value_from_str(text).map_err(|_| ())
}

/// Decode ordinary object keys without changing the caller's number policy.
pub(in crate::cli) fn value_from_str(text: &str) -> Result<Value, serde_json::Error> {
    let encoded = protect_keys(text);
    let mut value = serde_json::from_str(&encoded)?;
    restore_keys(&mut value);
    Ok(value)
}

fn skip_string(bytes: &[u8], position: &mut usize) {
    *position += 1;
    while *position < bytes.len() {
        match bytes[*position] {
            b'\\' => *position += 2,
            b'"' => {
                *position += 1;
                break;
            }
            _ => *position += 1,
        }
    }
}

pub(in crate::cli) fn protect_keys(text: &str) -> String {
    // serde's arbitrary_precision transport recognizes a private Number key.
    // Prefix every real object key injectively before decoding, so a user JSON
    // object cannot impersonate that numeric transport. This pass does not
    // validate values: archive duplicate detection must retain scan-error order.
    let bytes = text.as_bytes();
    let mut position = 0;
    let mut keys = Vec::new();
    while position < bytes.len() {
        if bytes[position] == b'"' {
            let start = position;
            skip_string(bytes, &mut position);
            let mut next = position;
            while bytes.get(next).is_some_and(u8::is_ascii_whitespace) {
                next += 1;
            }
            if bytes.get(next) == Some(&b':') {
                keys.push(start + 1);
            }
        } else {
            position += 1;
        }
    }
    let mut encoded = String::with_capacity(text.len() + keys.len() * 6);
    let mut start = 0;
    for key in keys {
        encoded.push_str(&text[start..key]);
        encoded.push_str("\\u0000");
        start = key;
    }
    encoded.push_str(&text[start..]);
    encoded
}

fn restore_keys(value: &mut Value) {
    match value {
        Value::Object(object) => {
            *object = std::mem::take(object)
                .into_iter()
                .map(|(key, mut value)| {
                    restore_keys(&mut value);
                    (key[1..].to_owned(), value)
                })
                .collect();
        }
        Value::Array(array) => array.iter_mut().for_each(restore_keys),
        _ => (),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn standard_json_checks_ignored_and_overwritten_values() {
        for token in [
            "NaN",
            "Infinity",
            "-Infinity",
            "1e99999",
            "-1e99999",
            "\"\\ud800\"",
            "\"\\udfff\"",
        ] {
            assert!(from_str(&format!("{{\"extra\":{token}}}")).is_err());
            assert!(from_str(&format!("{{\"extra\":{token},\"extra\":0}}")).is_err());
        }
        assert!(from_str("{\"\\ud800\":0}").is_err());
        assert_eq!(from_str("\"\\ud83d\\ude00\"").unwrap().as_str(), Some("😀"));
        assert!(from_str(r#"{"extra":"1e99999\"-Infinity","n":-0.0}"#).is_ok());
    }

    #[test]
    fn ignored_integer_tokens_have_no_python_digit_limit() {
        for digits in [640, 641, 4300, 4301, 5001, 65000] {
            assert!(from_str(&format!("{{\"extra\":{}}}", "9".repeat(digits))).is_ok());
        }
    }

    #[test]
    fn ordinary_native_recursion_budget_is_bounded() {
        assert!(from_str(&format!("{}0{}", "[".repeat(127), "]".repeat(127))).is_ok());
        assert!(from_str(&format!("{}0{}", "[".repeat(128), "]".repeat(128))).is_err());
        assert!(from_str(&format!("{}0{}", "[".repeat(2000), "]".repeat(2000))).is_err());
    }

    #[test]
    fn duplicates_keep_first_key_position_and_last_value() {
        assert_eq!(
            from_str(r#"{"a":1,"b":2,"a":3}"#).unwrap().to_string(),
            r#"{"a":3,"b":2}"#
        );
    }

    #[test]
    fn user_object_keys_cannot_impersonate_numeric_transport() {
        for text in [
            r#"{"$serde_json::private::Number":"1"}"#,
            r#"{"$serde_json::private::Number":"not a number"}"#,
            r#"{"$serde_json::private::Number":"1e99999","\u0000key":2,"key":3}"#,
        ] {
            let value = from_str(text).unwrap();
            assert!(value.is_object());
            assert!(value["$serde_json::private::Number"].is_string());
        }
        assert_eq!(
            from_str(r#"{"\u0000key":2,"key":3}"#).unwrap().to_string(),
            r#"{"\u0000key":2,"key":3}"#
        );
    }

    #[test]
    fn fixed_legacy_fields_preserve_ascii_serialization() {
        let text = r#"{"text":"fixed notice","count":1,"nonce":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","thread_id":"00000000-0000-0000-0000-000000000001"}"#;
        let mut record = from_str(text).unwrap();
        record["version"] = 1.into();
        record["legacy_first_seen"] = Value::Number(serde_json::Number::from_f64(1.5).unwrap());
        record["expires_at"] = Value::Number(serde_json::Number::from_f64(86401.5).unwrap());
        record["expiry_basis"] = "legacy_upper_bound".into();
        assert_eq!(
            record.to_string(),
            format!(
                "{},\"version\":1,\"legacy_first_seen\":1.5,\"expires_at\":86401.5,\"expiry_basis\":\"legacy_upper_bound\"}}",
                &text[..text.len() - 1]
            )
        );
    }
}
