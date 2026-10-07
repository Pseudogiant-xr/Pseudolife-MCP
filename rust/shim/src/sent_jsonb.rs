//! The named bounded-jsonb-depth / jsonb-no-digit-limit read domain.
use crate::pg::types::{FromSql, Type};
use serde::Deserialize;
use serde_json::Value;
use std::error::Error;

pub(crate) const MAX_DEPTH: usize = 2048;
// Covers conversion, encoding and recursive drop as well as deserialization.
pub(crate) const STACK_BYTES: usize = 32 * 1024 * 1024;
pub(crate) struct Jsonb(pub Value);

// Keep real object keys out of serde_json's internal numeric-token protocol.
// JSON syntax permits a quoted string followed by ':' only as an object key.
// Prefix every such key, including escaped spellings, and remove exactly one
// prefix after normal serde validation. This changes no value or member order.
fn object_keys(raw: &[u8]) -> Vec<u8> {
    let mut output = Vec::with_capacity(raw.len());
    let (mut index, mut copied) = (0, 0);
    while index < raw.len() {
        if raw[index] != b'"' {
            index += 1;
            continue;
        }
        let start = index;
        index += 1;
        while index < raw.len() {
            match raw[index] {
                b'\\' => index += 2,
                b'"' => break,
                _ => index += 1,
            }
        }
        let mut next = index.saturating_add(1);
        while next < raw.len() && matches!(raw[next], b' ' | b'\t' | b'\r' | b'\n') {
            next += 1;
        }
        if raw.get(next) == Some(&b':') {
            output.extend_from_slice(&raw[copied..=start]);
            output.push(b'_');
            copied = start + 1;
        }
        index = index.saturating_add(1);
    }
    output.extend_from_slice(&raw[copied..]);
    output
}

fn restore_keys(value: Value) -> Result<Value, &'static str> {
    Ok(match value {
        Value::Object(members) => {
            let mut restored = serde_json::Map::new();
            for (key, value) in members {
                let key = key
                    .strip_prefix('_')
                    .ok_or("JSONB object key missing prefix")?;
                restored.insert(key.to_owned(), restore_keys(value)?);
            }
            Value::Object(restored)
        }
        Value::Array(items) => Value::Array(
            items
                .into_iter()
                .map(restore_keys)
                .collect::<Result<_, _>>()?,
        ),
        scalar => scalar,
    })
}

fn within_depth(raw: &[u8]) -> bool {
    let (mut depth, mut quoted, mut escaped) = (0usize, false, false);
    for byte in raw {
        if quoted {
            if escaped {
                escaped = false;
            } else if *byte == b'\\' {
                escaped = true;
            } else if *byte == b'"' {
                quoted = false;
            }
        } else {
            match byte {
                b'"' => quoted = true,
                b'[' | b'{' => {
                    depth += 1;
                    if depth > MAX_DEPTH {
                        return false;
                    }
                }
                b']' | b'}' => {
                    depth = depth.saturating_sub(1);
                }
                _ => (),
            }
        }
    }
    true
}

impl<'a> FromSql<'a> for Jsonb {
    fn from_sql(_: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        let Some((&1, raw)) = raw.split_first() else {
            return Err("unsupported JSONB version".into());
        };
        if !within_depth(raw) {
            return Err("bounded-jsonb-depth: exceeds 2048".into());
        }
        let protected = object_keys(raw);
        let mut decoder = serde_json::Deserializer::from_slice(&protected);
        decoder.disable_recursion_limit();
        let value = Value::deserialize(serde_stacker::Deserializer::new(&mut decoder))?;
        decoder.end()?;
        let value = restore_keys(value)?;
        Ok(Self(value))
    }
    fn accepts(ty: &Type) -> bool {
        *ty == Type::JSONB
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn application_keys_never_become_numeric_tokens() {
        for raw in [
            r#"{"label":{"$serde_json::private::Number":"1"}}"#,
            r#"{"label":{"$serde_json::private::Number":"text"}}"#,
            r#"{"label":{"$serde_json::private::Number":1}}"#,
            r#"{"$serde_json::private::Number":"1"}"#,
            r#"{"items":[{"$serde_json::private::Number":"1"},{"$serde_json::private::RawValue":"{}"}],"number":10000000000000000000000000000000000000000}"#,
        ] {
            let mut protocol = vec![1];
            protocol.extend(raw.as_bytes());
            stacker::grow(STACK_BYTES, || {
                let value = Jsonb::from_sql(&Type::JSONB, &protocol).unwrap().0;
                // Serialization must retain the object structure and all keys.
                assert_eq!(serde_json::to_string(&value).unwrap(), raw);
            });
        }
    }
    #[test]
    fn key_prefix_and_escaped_spelling_round_trip() {
        let raw = br#"{"\u0024serde_json::private::Number":"1","_":"prefix","__":"two","":"empty","quote\"\\:":"text: \"colon\"","array":[{"_":"nested"}]}"#;
        let mut protocol = vec![1];
        protocol.extend(raw);
        stacker::grow(STACK_BYTES, || {
            let value = Jsonb::from_sql(&Type::JSONB, &protocol).unwrap().0;
            assert_eq!(
                serde_json::to_string(&value).unwrap(),
                r#"{"$serde_json::private::Number":"1","_":"prefix","__":"two","":"empty","quote\"\\:":"text: \"colon\"","array":[{"_":"nested"}]}"#
            );
        });
    }
    #[test]
    fn depth_boundary_and_string_brackets() {
        assert!(within_depth(br#"{"s":"[[[\"\\"}"#));
        for (depth, accepted) in [(200, true), (2048, true), (2049, false)] {
            let raw = format!("{}0{}", "[".repeat(depth), "]".repeat(depth));
            assert_eq!(within_depth(raw.as_bytes()), accepted);
            let mut protocol = vec![1];
            protocol.extend(raw.as_bytes());
            stacker::grow(STACK_BYTES, || {
                assert_eq!(Jsonb::from_sql(&Type::JSONB, &protocol).is_ok(), accepted);
            });
        }
    }
}
