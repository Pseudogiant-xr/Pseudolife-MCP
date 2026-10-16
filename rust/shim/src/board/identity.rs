use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Context {
    pub bank_id: String,
    pub principal: String,
}

impl Context {
    pub fn parse(value: &Value) -> Result<Self, &'static str> {
        let invalid = "bank_identity_mismatch";
        let bank_id = value
            .get("bank_id")
            .and_then(Value::as_str)
            .and_then(super::policy::canonical_uuid)
            .ok_or(invalid)?;
        let principal = value
            .get("principal")
            .and_then(Value::as_str)
            .filter(|v| (1..=256).contains(&v.chars().count()) && !v.chars().any(|c| c < '\u{20}'))
            .ok_or(invalid)?;
        Ok(Self {
            bank_id,
            principal: principal.to_owned(),
        })
    }
    pub fn check_binding(&self, state: &Value) -> Result<(), &'static str> {
        if state.get("version").and_then(Value::as_u64) == Some(2)
            && state.get("bank_id").and_then(Value::as_str) == Some(&self.bank_id)
            && state.get("principal").and_then(Value::as_str) == Some(&self.principal)
        {
            Ok(())
        } else {
            Err("bank_identity_mismatch")
        }
    }
    pub fn encoded_principal(&self) -> String {
        let mut value = String::new();
        for byte in self.principal.bytes() {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                value.push(char::from(byte));
            } else {
                value.push_str(&format!("%{byte:02X}"));
            }
        }
        value
    }
}

pub fn hex_hash(bytes: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(bytes.as_ref()))
}
pub fn bound_state_path(root: &Path, url: &str, thread: &str) -> PathBuf {
    let namespace = hex_hash(format!("coordination-v2\0{}", url.trim_end_matches('/')));
    root.join(&namespace[..32])
        .join(format!("{}.json", hex_hash(thread)))
}
pub fn legacy_state_path(root: &Path, url: &str, token: &str, thread: &str) -> PathBuf {
    let mut bytes = url.trim_end_matches('/').as_bytes().to_vec();
    bytes.push(0);
    bytes.extend(Sha256::digest(token.as_bytes()));
    let namespace = hex_hash(bytes);
    root.join(&namespace[..32])
        .join(format!("{}.json", hex_hash(thread)))
}

/// Python ensure_ascii compact JSON is part of the legacy identity proof.
pub fn ascii_json(value: &Value) -> String {
    serde_json::to_string(value)
        .expect("JSON value serializes")
        .chars()
        .flat_map(|c| {
            if c.is_ascii() {
                c.to_string()
            } else {
                let mut words = [0u16; 2];
                c.encode_utf16(&mut words)
                    .iter()
                    .map(|word| format!("\\u{word:04x}"))
                    .collect::<String>()
            }
            .chars()
            .collect::<Vec<_>>()
        })
        .collect()
}
pub fn legacy_proof(credential: &str, context: &Context, agent: &str, nonce: &str) -> String {
    let key = Sha256::digest(credential.as_bytes());
    let mut inner = [0x36u8; 64];
    let mut outer = [0x5cu8; 64];
    for (index, byte) in key.iter().enumerate() {
        inner[index] ^= byte;
        outer[index] ^= byte;
    }
    let message = ascii_json(&serde_json::json!([
        "pseudolife-context-v1",
        context.bank_id,
        context.principal,
        agent,
        nonce
    ]));
    let mut digest = Sha256::new();
    digest.update(inner);
    digest.update(message.as_bytes());
    let mut final_digest = Sha256::new();
    final_digest.update(outer);
    final_digest.update(digest.finalize());
    format!("{:x}", final_digest.finalize())
}
pub fn constant_time_equal(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter()
        .zip(b)
        .fold(0u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}
