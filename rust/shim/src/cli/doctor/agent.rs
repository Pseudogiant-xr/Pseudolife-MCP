//! `--agent-state`: `probe_registration`'s read-only nonce proof for an
//! explicit saved instance file. The file is read before any request; the
//! context request runs where Python runs it, after the handshake.
use super::pyenv::{Defer, Res, json_loads};
use hmac::{Hmac, Mac};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path, time::Duration};

/// The saved instance file as `read_legacy` would find it.
pub(super) enum Saved {
    /// `state_path.exists()` is false.
    Missing,
    /// `read_legacy` raises.
    Invalid,
    State(Map<String, Value>),
}

/// A name-surrogate reparse point (symlink or junction) on Windows, a
/// symlink elsewhere: pathlib's `is_symlink` sees only symlinks, so either
/// defers.
fn redirect(path: &Path) -> Res<bool> {
    let meta = std::fs::symlink_metadata(path).map_err(|_| Defer)?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        Ok(meta.file_type().is_symlink() || meta.file_attributes() & 0x400 != 0)
    }
    #[cfg(not(windows))]
    {
        Ok(meta.file_type().is_symlink())
    }
}

/// `Path(value).exists()` then `coordination_identity.read_legacy`.
pub(super) fn read(value: &str, url: &str) -> Res<Saved> {
    let path = Path::new(value);
    if path
        .components()
        .any(|part| matches!(part, std::path::Component::ParentDir))
    {
        return Err(Defer);
    }
    match std::fs::metadata(path) {
        Ok(_) => {}
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
            ) =>
        {
            return Ok(Saved::Missing);
        }
        Err(_) => return Err(Defer),
    }
    let absolute = std::path::absolute(path).map_err(|_| Defer)?;
    for ancestor in absolute.ancestors() {
        if redirect(ancestor)? {
            return Err(Defer);
        }
    }
    // open_private: owner-only regular file, one link (board::state::open).
    let Ok(opened) = crate::board::state::open(path) else {
        return Ok(Saved::Invalid);
    };
    // A text read of 16385 characters: more than 65540 bytes is more than
    // 16384 characters or undecodable, both a ValueError.
    let mut bytes = Vec::new();
    if opened.file.take(65541).read_to_end(&mut bytes).is_err() {
        return Ok(Saved::Invalid);
    }
    if bytes.len() > 65540 {
        return Ok(Saved::Invalid);
    }
    let Ok(text) = String::from_utf8(bytes) else {
        return Ok(Saved::Invalid);
    };
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    if text.chars().count() > 16384 {
        return Ok(Saved::Invalid);
    }
    let Some(Value::Object(state)) = json_loads(&text)? else {
        return Ok(Saved::Invalid);
    };
    let named = |key: &str| {
        state
            .get(key)
            .and_then(Value::as_str)
            .is_some_and(|v| !v.is_empty())
    };
    if state.get("bank_url").and_then(Value::as_str) != Some(url)
        || !named("agent_id")
        || !named("credential")
    {
        return Ok(Saved::Invalid);
    }
    Ok(Saved::State(state))
}

/// Python's `==` against the integer 2 for a JSON value.
fn is_two(value: &Value) -> bool {
    match value {
        Value::Number(number) => number.as_f64() == Some(2.0),
        _ => false,
    }
}

/// `json.dumps([...], separators=(",", ":"), ensure_ascii=True)`.
fn ascii_array(items: &[&str]) -> String {
    let mut out = String::from("[");
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push('"');
        for character in item.chars() {
            match character {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                ' '..='~' => out.push(character),
                other => {
                    let mut units = [0u16; 2];
                    for unit in other.encode_utf16(&mut units) {
                        out.push_str(&format!("\\u{unit:04x}"));
                    }
                }
            }
        }
        out.push('"');
    }
    out.push(']');
    out
}

fn proof(credential: &str, items: &[&str]) -> String {
    let key = Sha256::digest(credential.as_bytes());
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(&key).expect("HMAC takes any key length");
    mac.update(ascii_array(items).as_bytes());
    mac.finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// The registration state `probe_registration` returns for a readable
/// saved instance.
pub(super) async fn probe(
    url: &str,
    token: &str,
    state: &Map<String, Value>,
    timeout: Duration,
) -> &'static str {
    let agent = state
        .get("agent_id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let nonce = uuid::Uuid::new_v4().simple().to_string();
    let Ok(client) = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .connect_timeout(timeout)
        .read_timeout(timeout)
        .build()
    else {
        return "unavailable";
    };
    let sent = client
        .post(format!("{url}/api/coordination/context"))
        .header("Authorization", format!("Bearer {token}"))
        .json(&json!({"agent_id": agent, "nonce": nonce, "read_only": true}))
        .send()
        .await;
    let Ok(response) = sent else {
        return "unavailable";
    };
    let status = response.status().as_u16();
    let Ok(body) = response.bytes().await else {
        return "unavailable";
    };
    let parsed = || match std::str::from_utf8(&body) {
        Ok(text) => serde_json::from_str::<Value>(text).ok(),
        Err(_) => None,
    };
    match status {
        200 => {
            let Some(Value::Object(value)) = parsed() else {
                return "unsupported_capability";
            };
            let field = |key: &str| value.get(key).and_then(Value::as_str);
            let (Some(bank_id), Some(principal), Some(answer)) =
                (field("bank_id"), field("principal"), field("proof"))
            else {
                return "unsupported_capability";
            };
            let version = state.get("version");
            if version.is_some_and(is_two)
                && (state.get("bank_id").and_then(Value::as_str) != Some(bank_id)
                    || state.get("principal").and_then(Value::as_str) != Some(principal))
            {
                return "bank_identity_mismatch";
            }
            if !matches!(version, None | Some(Value::Null)) && !version.is_some_and(is_two) {
                return "invalid_state";
            }
            let credential = state
                .get("credential")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let expected = proof(
                credential,
                &["pseudolife-context-v1", bank_id, principal, agent, &nonce],
            );
            // hmac.compare_digest refuses non-ASCII str operands (TypeError).
            if !answer.is_ascii() {
                return "unsupported_capability";
            }
            if answer == expected {
                "authenticated"
            } else {
                "invalid_credential"
            }
        }
        401 => "unauthorized",
        404 | 405 => "unsupported_capability",
        400 | 403 => {
            let Some(value) = parsed() else {
                return "unsupported_capability";
            };
            match value.get("error").and_then(Value::as_str) {
                Some("instance_not_found") => "missing_registration",
                Some("invalid_credential") => "invalid_credential",
                Some("principal_not_allowed") => "principal_not_allowed",
                Some("unexpected_parameter") => "unsupported_capability",
                _ => "refused",
            }
        }
        500.. => "unavailable",
        _ => "refused",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proof_message_is_python_ascii_compact_json() {
        assert_eq!(
            ascii_array(&["a\u{7f}", "\u{e9}\u{1f600}", "\"\\"]),
            "[\"a\\u007f\",\"\\u00e9\\ud83d\\ude00\",\"\\\"\\\\\"]"
        );
        // hmac.new(sha256("ké".encode()).digest(), json.dumps([...],
        // separators=(",", ":"), ensure_ascii=True).encode(), sha256).hexdigest()
        assert_eq!(
            proof(
                "k\u{e9}",
                &["pseudolife-context-v1", "b", "p", "a\u{7f}", "n\u{e9}"]
            ),
            "f1aad97ee6eda42f4d6eee91e4550127c36012e2bd0eb5ec86270f942ffa7273"
        );
    }
}
