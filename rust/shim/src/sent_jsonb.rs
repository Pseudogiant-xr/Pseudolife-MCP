//! The named bounded-jsonb-depth / jsonb-no-digit-limit read domain.
use crate::pg::types::{FromSql, Type};
use serde::Deserialize;
use serde_json::Value;
use std::error::Error;

pub(crate) const MAX_DEPTH: usize = 2048;
// Covers conversion, encoding and recursive drop as well as deserialization.
pub(crate) const STACK_BYTES: usize = 32 * 1024 * 1024;
pub(crate) struct Jsonb(pub Value);

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
        let mut decoder = serde_json::Deserializer::from_slice(raw);
        decoder.disable_recursion_limit();
        let value = Value::deserialize(serde_stacker::Deserializer::new(&mut decoder))?;
        decoder.end()?;
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
