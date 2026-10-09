//! Signed exact entry membership (`dream_token.py`). PostgreSQL producer shapes.

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use hmac::{Hmac, Mac};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashSet;

pub const MAX_ENTRY_IDS: usize = 4096;
pub const MAX_TOKEN_CHARS: usize = 262_144;

#[derive(Debug, PartialEq)]
pub struct Payload {
    pub entry_ids: Vec<i64>,
    pub display_timestamp: f64,
}

fn secret_bytes(secret: &str) -> Result<Vec<u8>, String> {
    hex::decode(secret)
        .ok()
        .filter(|s| s.len() == 32)
        .ok_or_else(|| "invalid_dream_ack_secret".into())
}

pub fn generation(secret: &str) -> Result<String, String> {
    Ok(hex::encode(Sha256::digest(secret_bytes(secret)?)))
}

fn valid_ids(ids: &[i64]) -> bool {
    !ids.is_empty()
        && ids.len() <= MAX_ENTRY_IDS
        && ids.iter().all(|i| *i > 0)
        && ids.iter().collect::<HashSet<_>>().len() == ids.len()
}

pub fn issue(
    secret: &str,
    generation: &str,
    ids: &[i64],
    timestamp: f64,
) -> Result<String, String> {
    let invalid = || "invalid_dream_commit_payload".to_string();
    if !valid_ids(ids) || !timestamp.is_finite() {
        return Err(invalid());
    }
    let key = secret_bytes(secret).map_err(|_| invalid())?;
    // Sorted keys and compact separators, with the existing Python float writer.
    let payload = format!(
        "{{\"b\":\"postgres\",\"g\":{},\"i\":[{}],\"t\":{}}}",
        crate::pyjson::dumps(&Value::String(generation.into())),
        ids.iter().map(i64::to_string).collect::<Vec<_>>().join(","),
        crate::pyjson::float_repr(timestamp)
    );
    let message = format!("v1.{}", URL_SAFE_NO_PAD.encode(payload));
    let mut mac = Hmac::<Sha256>::new_from_slice(&key).map_err(|_| invalid())?;
    mac.update(message.as_bytes());
    let token = format!(
        "{message}.{}",
        URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
    );
    if token.len() > MAX_TOKEN_CHARS {
        return Err(invalid());
    }
    Ok(token)
}

pub fn verify(token: &str, secret: &str, generation: &str) -> Result<Payload, String> {
    let invalid = || "invalid_dream_commit_token".to_string();
    if token.len() > MAX_TOKEN_CHARS {
        return Err(invalid());
    }
    let parts: Vec<_> = token.split('.').collect();
    if parts.len() != 3 || parts[0] != "v1" || !token.is_ascii() {
        return Err(invalid());
    }
    let key = secret_bytes(secret).map_err(|_| invalid())?;
    let mut mac = Hmac::<Sha256>::new_from_slice(&key).map_err(|_| invalid())?;
    mac.update(format!("v1.{}", parts[1]).as_bytes());
    let signature = URL_SAFE_NO_PAD.decode(parts[2]).map_err(|_| invalid())?;
    if !crate::mutants::active("dream-token-no-signature") {
        mac.verify_slice(&signature).map_err(|_| invalid())?;
    }
    let bytes = URL_SAFE_NO_PAD.decode(parts[1]).map_err(|_| invalid())?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
    if value["b"].as_str() != Some("postgres") || value["g"].as_str() != Some(generation) {
        return Err(invalid());
    }
    let ids: Vec<i64> = value["i"]
        .as_array()
        .ok_or_else(invalid)?
        .iter()
        .map(|i| i.as_i64().ok_or_else(invalid))
        .collect::<Result<_, _>>()?;
    let timestamp = value["t"].as_f64().ok_or_else(invalid)?;
    if !valid_ids(&ids) || !timestamp.is_finite() {
        return Err(invalid());
    }
    Ok(Payload {
        entry_ids: ids,
        display_timestamp: timestamp,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const ORACLE: &str = "v1.eyJiIjoicG9zdGdyZXMiLCJnIjoiMDJkNDQ5YTMxZmJiMjY3YzhmMzUyZTk5NjhhNzllM2U1ZmM5NWMxYmJlYWE1MDJmZDY0NTRlYmRlNWE0YmVkYyIsImkiOlsyLDFdLCJ0Ijo0Mi41fQ.fvg87WdOXPTiEWkRClLaE32QFie5uzbE1AjJIwQTtsg";

    #[test]
    fn signed_membership_matches_python_and_rejects_tampering() {
        let secret = "11".repeat(32);
        let g = generation(&secret).unwrap();
        assert_eq!(issue(&secret, &g, &[2, 1], 42.5).unwrap(), ORACLE);
        assert_eq!(
            verify(ORACLE, &secret, &g).unwrap(),
            Payload {
                entry_ids: vec![2, 1],
                display_timestamp: 42.5,
            }
        );
        assert!(verify(&format!("{ORACLE}x"), &secret, &g).is_err());
        assert!(verify(ORACLE, &secret, "another bank").is_err());
    }

    #[test]
    fn membership_and_timestamp_are_bounded_not_watermarks() {
        let secret = "11".repeat(32);
        let g = generation(&secret).unwrap();
        for ids in [vec![], vec![0], vec![1, 1], (1..=4097).collect()] {
            assert!(issue(&secret, &g, &ids, 1.0).is_err());
        }
        for t in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert!(issue(&secret, &g, &[1], t).is_err());
        }
        let signed = issue(&secret, &g, &[1], -100.0).unwrap();
        assert_eq!(
            verify(&signed, &secret, &g).unwrap().display_timestamp,
            -100.0
        );
        assert!(verify(&"x".repeat(MAX_TOKEN_CHARS + 1), &secret, &g).is_err());
        assert!(generation("invalid").is_err());
    }
}
