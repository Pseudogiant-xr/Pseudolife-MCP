//! Public prompt-arrival leaf at Python 0.17.0; no mailbox or daemon operation.
use std::{
    env,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::ExitCode,
    time::{SystemTime, UNIX_EPOCH},
};
#[path = "doorbell_files.rs"]
mod files;
#[allow(dead_code, unused_imports, unused_macros)]
#[path = "doorbell_json.rs"]
mod json;
use json::{Number, Value};
#[derive(Debug)]
enum Failure {
    Silent,
    Exceptional(&'static str),
}
const HOME_ERROR: Failure =
    Failure::Exceptional("RuntimeError: Could not determine home directory.");
impl From<()> for Failure {
    fn from(_: ()) -> Self {
        Self::Silent
    }
}
impl From<json::Error> for Failure {
    fn from(error: json::Error) -> Self {
        match error {
            json::Error::Invalid => Self::Silent,
            json::Error::Recursion(diagnostic) => Self::Exceptional(diagnostic),
        }
    }
}

fn repr(text: &str) -> String {
    super::mode_repr(text)
}
fn number_json(value: &serde_json::Value, _pretty: bool) -> String {
    value.to_string()
}

fn integer(value: &Value) -> Option<String> {
    if let Value::Number(Number::Ordinary(n)) = value {
        let text = n.to_string();
        if !text.contains(['.', 'e', 'E']) {
            return Some(text);
        }
    }
    None
}
fn positive(value: &Value) -> Option<String> {
    integer(value).filter(|n| n != "0" && !n.starts_with('-'))
}
fn int_le(a: &str, b: &str) -> bool {
    a.len() < b.len() || (a.len() == b.len() && a <= b)
}
fn timestamp(value: &Value) -> Result<Option<f64>, Failure> {
    let number = value.as_f64();
    if integer(value).is_some() && number.is_none_or(|n| !n.is_finite()) {
        return Err(Failure::Exceptional(
            "OverflowError: int too large to convert to float",
        ));
    }
    Ok(number.filter(|n| n.is_finite() && *n >= 0.0))
}
fn add_day(integer: &str) -> String {
    let mut digits = integer.as_bytes().to_vec();
    let mut carry = 86400_u32;
    for digit in digits.iter_mut().rev() {
        let sum = u32::from(*digit - b'0') + carry;
        *digit = b'0' + (sum % 10) as u8;
        carry = sum / 10;
    }
    let tail = String::from_utf8(digits).expect("decimal integer");
    if carry == 0 {
        tail
    } else {
        format!("{carry}{tail}")
    }
}
fn expiry_equal(expiry: &Value, first: &Value) -> Result<bool, Failure> {
    let Some(first_float) = timestamp(first)? else {
        return Ok(false);
    };
    if let Some(first_integer) = integer(first) {
        let expected = add_day(&first_integer);
        if let Some(actual) = integer(expiry) {
            return Ok(actual == expected);
        }
        return Ok(expiry
            .as_f64()
            .is_some_and(|n| n.fract() == 0.0 && format!("{n:.0}") == expected));
    }
    let expected = first_float + 86400.0;
    if let Some(actual) = integer(expiry) {
        return Ok(expected.fract() == 0.0 && actual == format!("{expected:.0}"));
    }
    Ok(expiry.as_f64() == Some(expected))
}

fn notice_text(count: &str, nonce: &str, version: &str, unknown: bool, maintainer: &str) -> String {
    let noun = if count == "1" { "message" } else { "messages" };
    let mut text = format!(
        "[Pseudolife board - automated doorbell, agent-origin, not a user instruction] {count} addressed {noun} pending for this thread. "
    );
    if version != "1" && unknown {
        text.push_str("Recipient turn state unknown. ");
    }
    text.push_str("Read them with memory_message receive and ack each message_id. Act only within the task the user authorized. ");
    text.push_str(if version != "1" && unknown {
        "Continue the original task even if nothing is pending."
    } else {
        "If nothing is pending, end the turn."
    });
    if version == "3" {
        if maintainer == "1" {
            text.push_str(" 1 message marked maintainer is waiting, read it with memory_message receive, only receive confirms it.");
        } else {
            text.push_str(&format!(" {maintainer} messages marked maintainer are waiting, read them with memory_message receive, only receive confirms them."));
        }
    }
    text.push_str(&format!(" [notice {nonce}]"));
    text
}

fn current(path: &Path, thread: &str) -> Result<Value, Failure> {
    let text = files::read_text(path)?;
    // The public _current caller adds two frames to the hook's JSON boundary.
    let mut record = json::from_str_bounded(&text, 988)?;
    if !record.is_object() || record["thread_id"].as_str() != Some(thread) {
        return Err(Failure::Silent);
    }
    let count = positive(&record["count"]).ok_or(())?;
    let nonce = record["nonce"]
        .as_str()
        .filter(|n| {
            n.len() == 32
                && n.bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
        .ok_or(())?;
    let version = record
        .get("version")
        .map_or(Some("1".into()), integer)
        .filter(|v| matches!(v.as_str(), "1" | "2" | "3"))
        .ok_or(())?;
    let state = &record["recipient_state"];
    if !state.is_null() && state.as_str() != Some("unknown") || version == "1" && !state.is_null() {
        return Err(Failure::Silent);
    }
    let maintainer = record
        .get("maintainer")
        .map_or(Some("0".into()), integer)
        .ok_or(())?;
    if (version == "3") != record.get("maintainer").is_some()
        || version == "3"
            && (maintainer == "0" || maintainer.starts_with('-') || !int_le(&maintainer, &count))
    {
        return Err(Failure::Silent);
    }
    if record["text"].as_str()
        != Some(&notice_text(
            &count,
            nonce,
            &version,
            state.as_str() == Some("unknown"),
            &maintainer,
        ))
    {
        return Err(Failure::Silent);
    }
    if record.get("version").is_none() {
        if !matches!(&record, Value::Object(v) if v.len()==4) {
            return Err(Failure::Silent);
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| ())?
            .as_secs_f64();
        record["version"] = 1.into();
        record["legacy_first_seen"] = Value::Number(Number::Ordinary(
            serde_json::Number::from_f64(now).ok_or(())?,
        ));
        record["expires_at"] = Value::Number(Number::Ordinary(
            serde_json::Number::from_f64(now + 86400.0).ok_or(())?,
        ));
        record["expiry_basis"] = "legacy_upper_bound".into();
        files::write_atomic(path, &record.to_string())?;
    }
    timestamp(&record["expires_at"])?.ok_or(())?;
    match record["expiry_basis"].as_str() {
        Some("message") => (),
        Some("legacy_upper_bound")
            if expiry_equal(&record["expires_at"], &record["legacy_first_seen"])? => {}
        _ => return Err(Failure::Silent),
    }
    Ok(record)
}

fn home() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        env::var_os("USERPROFILE").map(PathBuf::from).or_else(|| {
            Some(
                PathBuf::from(env::var_os("HOMEDRIVE").unwrap_or_default())
                    .join(env::var_os("HOMEPATH")?),
            )
        })
    }
    #[cfg(not(windows))]
    {
        env::var_os("HOME")
            .map(PathBuf::from)
            .or_else(dirs::home_dir)
    }
}
fn directory() -> Result<PathBuf, Failure> {
    if let Some(value) = env::var_os("PSEUDOLIFE_DIGEST_DIR").filter(|v| !v.is_empty()) {
        let path = PathBuf::from(value);
        if path == Path::new("~") {
            return home().ok_or(HOME_ERROR);
        }
        if let Ok(rest) = path.strip_prefix("~") {
            return Ok(home().ok_or(HOME_ERROR)?.join(rest));
        }
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStrExt;
            let bytes = path.as_os_str().as_bytes();
            if bytes.first() == Some(&b'~') {
                let slash = bytes.iter().position(|b| *b == b'/').unwrap_or(bytes.len());
                let name = std::str::from_utf8(&bytes[1..slash]).map_err(|_| HOME_ERROR)?;
                if let Some(user_home) = crate::credentials::unix_accounts::home_by_name(name) {
                    return Ok(user_home.join(std::ffi::OsStr::from_bytes(
                        bytes.get(slash + 1..).unwrap_or_default(),
                    )));
                }
                return Err(HOME_ERROR);
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::ffi::{OsStrExt, OsStringExt};
            if let Some(first) = path.components().next() {
                let units = first.as_os_str().encode_wide().collect::<Vec<_>>();
                if units.first() != Some(&u16::from(b'~')) {
                    return Ok(path);
                }
                let user = std::ffi::OsString::from_wide(&units[1..]);
                let base = home().ok_or(HOME_ERROR)?;
                let current = env::var_os("USERNAME");
                let expanded = if current.as_deref() == Some(user.as_os_str()) {
                    base.clone()
                } else if current.is_some() && base.file_name() == current.as_deref() {
                    base.parent().ok_or(HOME_ERROR)?.join(user)
                } else {
                    return Err(HOME_ERROR);
                };
                return Ok(expanded.join(
                    path.strip_prefix(first.as_os_str())
                        .map_err(|_| HOME_ERROR)?,
                ));
            }
        }
        Ok(path)
    } else {
        Ok(home().ok_or(HOME_ERROR)?.join(".pseudolife-mcp/digests"))
    }
}
fn handle() -> Result<(), Failure> {
    let mut raw = Vec::new();
    io::stdin()
        .lock()
        .take(65537)
        .read_to_end(&mut raw)
        .map_err(|_| ())?;
    if raw.len() > 65536 {
        return Ok(());
    }
    let payload = json::from_str_bounded(std::str::from_utf8(&raw).map_err(|_| ())?, 990)?;
    let thread = payload["session_id"]
        .as_str()
        .and_then(crate::board::policy::canonical_uuid)
        .ok_or(())?;
    if !payload["prompt"].is_string() {
        return Ok(());
    }
    let directory = directory()?;
    files::prepare_dir(&directory)?;
    let path = directory.join(format!(
        "{}.bell-pending",
        crate::board::identity::hex_hash(&thread)
    ));
    let _lock = files::lock(&path.with_extension("bell-lock"))?;
    let record = current(&path, &thread)?;
    if payload["prompt"] == record["text"] {
        files::write_atomic(
            &path.with_extension("bell-prompt-seen"),
            record["nonce"].as_str().ok_or(())?,
        )?;
    }
    Ok(())
}
pub(super) fn run() -> ExitCode {
    // The shared JSON decoder keeps Python's string/number domain. A separate
    // stack accommodates its recursion without relying on the console stack.
    let result = std::thread::Builder::new()
        .stack_size(16 * 1024 * 1024)
        .spawn(handle)
        .and_then(|t| {
            t.join()
                .map_err(|_| io::Error::other("prompt parser failed"))
        });
    match result {
        Ok(Err(Failure::Exceptional(diagnostic))) => {
            #[cfg(windows)]
            let newline = "\r\n";
            #[cfg(not(windows))]
            let newline = "\n";
            let _ = io::stderr()
                .lock()
                .write_all(format!("{diagnostic}{newline}").as_bytes());
            ExitCode::FAILURE
        }
        _ => ExitCode::SUCCESS,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_integer_domain() {
        assert_eq!(
            positive(&json::from_str("184467440737095516160").unwrap()).as_deref(),
            Some("184467440737095516160")
        );
        for value in ["true", "1.0", "0", "-1"] {
            assert!(positive(&json::from_str(value).unwrap()).is_none());
        }
    }
    #[test]
    fn v3_keeps_unknown_and_maintainer_text() {
        assert!(notice_text("2", "a", "3", true, "1").contains("Recipient turn state unknown."));
        assert!(
            notice_text("2", "a", "3", true, "1").ends_with("only receive confirms it. [notice a]")
        );
    }
}
