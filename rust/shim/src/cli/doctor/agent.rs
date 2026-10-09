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
    // argparse's type=Path: pathlib's spelling (trailing separators and `.`
    // parts dropped) is the path every later call sees.
    let normalized = super::pyenv::path_str(value)?;
    let path = Path::new(&normalized);
    // `exists()` comes before read_legacy's ancestor walk, so a missing file
    // is missing_registration whatever its ancestors are.
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

/// The deepest nesting `response.json()` decodes inside
/// `probe_registration` before CPython 3.11 raises RecursionError: measured
/// 2026-10-09 against the oracle doctor (Windows, CPython 3.11.9; the
/// recursion limit of 1000 less the frames above the call). Deeper answers
/// are RecursionError, which the doctor reports as `unavailable`.
const PYTHON_JSON_DEPTH: usize = 978;

/// `response.json()` as httpx decodes the body.
enum Decoded {
    Value(Value),
    /// ValueError: not JSON, or an integer literal over 4300 digits.
    Invalid,
    /// RecursionError: nesting past the interpreter's limit.
    TooDeep,
}

fn decode(body: &[u8]) -> Decoded {
    // detect_encoding's utf-8-sig removes one BOM.
    let body = body.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(body);
    let Ok(text) = std::str::from_utf8(body) else {
        return Decoded::Invalid;
    };
    if super::pyenv::json_depth(text.as_bytes()) > PYTHON_JSON_DEPTH {
        return Decoded::TooDeep;
    }
    match super::pyenv::json_unbounded(text) {
        Some(value) if !super::pyenv::long_integer(&value) => Decoded::Value(value),
        _ => Decoded::Invalid,
    }
}

/// One `decompressobj` pass over `data`: what decodes before the stream
/// ends or is cut short (zlib returns a truncated stream's output without
/// an error), or `Err` for data that is not that format.
fn inflate(mut reader: impl Read) -> Result<Vec<u8>, ()> {
    let mut out = Vec::new();
    match reader.read_to_end(&mut out) {
        Ok(_) => Ok(out),
        Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => Ok(out),
        Err(_) => Err(()),
    }
}

/// httpx's `Content-Encoding` decoders with neither brotli nor zstandard
/// installed (the oracle environments have neither): comma-separated,
/// stripped, lower-cased codings; `gzip` and `deflate` decoded in reverse
/// order of listing, `identity` and unknown codings left as they are.
/// `deflate` tries a zlib stream first and falls back to raw deflate, as
/// httpx's `DeflateDecoder` does on its first chunk.
fn content_decode(values: &[String], raw: &[u8]) -> Result<Vec<u8>, ()> {
    let codings: Vec<String> = values
        .iter()
        .flat_map(|value| value.split(','))
        .map(|coding| coding.trim().to_ascii_lowercase())
        .filter(|coding| coding == "gzip" || coding == "deflate")
        .collect();
    let mut data = raw.to_vec();
    for coding in codings.iter().rev() {
        data = if coding == "gzip" {
            inflate(flate2::read::GzDecoder::new(data.as_slice()))?
        } else {
            inflate(flate2::read::ZlibDecoder::new(data.as_slice()))
                .or_else(|()| inflate(flate2::read::DeflateDecoder::new(data.as_slice())))?
        };
    }
    Ok(data)
}

/// The registration state `probe_registration` returns for a readable
/// saved instance. `token` is the credential snapshot taken afresh, as
/// `CredentialProvider.from_environment().snapshot()` is at :650; its
/// failure is the doctor's `unavailable`.
pub(super) async fn probe(
    url: &str,
    token: Result<Option<String>, ()>,
    state: &Map<String, Value>,
    timeout: Duration,
) -> &'static str {
    let Ok(token) = token else {
        return "unavailable";
    };
    let Some(token) = token.filter(|token| !token.is_empty()) else {
        return "missing_bearer";
    };
    // httpx encodes header values as ASCII: anything else raises
    // UnicodeEncodeError (a ValueError) before the request is sent.
    if !token.is_ascii() {
        return "unsupported_capability";
    }
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
        // httpx's own agent string in the oracle environment.
        .user_agent("python-httpx/0.28.1")
        .build()
    else {
        return "unavailable";
    };
    let Ok(bearer) = reqwest::header::HeaderValue::from_str(&format!("Bearer {token}")) else {
        // h11 refuses the value: a LocalProtocolError, a TransportError.
        return "unavailable";
    };
    let sent = client
        .post(format!("{url}/api/coordination/context"))
        // httpx's default request fields besides Host, User-Agent and the
        // body's (reqwest sends `Accept: */*` itself). The daemon never
        // compresses, so the advertised encodings are never decoded here.
        .header("Accept-Encoding", "gzip, deflate")
        .header("Connection", "keep-alive")
        .header("Authorization", bearer)
        .json(&json!({"agent_id": agent, "nonce": nonce, "read_only": true}))
        .send()
        .await;
    let Ok(response) = sent else {
        return "unavailable";
    };
    let status = response.status().as_u16();
    let encodings: Vec<String> = response
        .headers()
        .get_all(reqwest::header::CONTENT_ENCODING)
        .iter()
        .filter_map(|value| value.to_str().ok().map(str::to_owned))
        .collect();
    let Ok(raw) = response.bytes().await else {
        return "unavailable";
    };
    // httpx decodes the body as it reads it inside client.post; a
    // DecodingError is no TransportError and reaches the doctor's
    // `except`, which reports `unavailable`.
    let Ok(body) = content_decode(&encodings, &raw) else {
        return "unavailable";
    };
    match status {
        200 => {
            let value = match decode(&body) {
                Decoded::Value(Value::Object(value)) => value,
                Decoded::TooDeep => return "unavailable",
                _ => return "unsupported_capability",
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
            let value = match decode(&body) {
                Decoded::Value(value) => value,
                Decoded::TooDeep => return "unavailable",
                Decoded::Invalid => return "unsupported_capability",
            };
            match value.as_object().and_then(|value| value.get("error")) {
                // dict.get with a list or dict key: TypeError (unhashable).
                Some(Value::Array(_) | Value::Object(_)) => "unsupported_capability",
                Some(Value::String(code)) => match code.as_str() {
                    "instance_not_found" => "missing_registration",
                    "invalid_credential" => "invalid_credential",
                    "principal_not_allowed" => "principal_not_allowed",
                    "unexpected_parameter" => "unsupported_capability",
                    _ => "refused",
                },
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
    fn content_codings_decode_as_httpx_does() {
        use flate2::{Compression, write};
        use std::io::Write;
        let plain = br#"{"bank_id": "b"}"#.to_vec();
        let gzip = {
            let mut encoder = write::GzEncoder::new(Vec::new(), Compression::default());
            encoder.write_all(&plain).unwrap();
            encoder.finish().unwrap()
        };
        let zlib = {
            let mut encoder = write::ZlibEncoder::new(Vec::new(), Compression::default());
            encoder.write_all(&plain).unwrap();
            encoder.finish().unwrap()
        };
        let raw_deflate = {
            let mut encoder = write::DeflateEncoder::new(Vec::new(), Compression::default());
            encoder.write_all(&plain).unwrap();
            encoder.finish().unwrap()
        };
        let both = {
            let mut encoder = write::ZlibEncoder::new(Vec::new(), Compression::default());
            encoder.write_all(&gzip).unwrap();
            encoder.finish().unwrap()
        };
        let decode = |values: &[&str], raw: &[u8]| {
            content_decode(
                &values.iter().map(|v| (*v).to_owned()).collect::<Vec<_>>(),
                raw,
            )
        };
        assert_eq!(decode(&[], &plain), Ok(plain.clone()));
        assert_eq!(decode(&[" GZIP "], &gzip), Ok(plain.clone()));
        assert_eq!(decode(&["deflate"], &zlib), Ok(plain.clone()));
        assert_eq!(decode(&["deflate"], &raw_deflate), Ok(plain.clone()));
        assert_eq!(decode(&["gzip, deflate"], &both), Ok(plain.clone()));
        assert_eq!(decode(&["gzip", "deflate"], &both), Ok(plain.clone()));
        // Unknown codings (br without brotli) and identity pass through.
        assert_eq!(decode(&["br", "identity"], &plain), Ok(plain.clone()));
        // Not gzip at all: zlib.error, httpx's DecodingError.
        assert_eq!(decode(&["gzip"], &plain), Err(()));
        // A truncated stream yields what decoded, as decompressobj does.
        let cut = decode(&["gzip"], &gzip[..gzip.len() - 8]).unwrap();
        assert!(plain.starts_with(&cut));
    }

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
