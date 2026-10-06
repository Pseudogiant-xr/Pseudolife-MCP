//! Queue acceptance and exact prompt arrival are separate durable facts.
use super::{identity, policy, state};
use fs2::FileExt;
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

pub fn notice_text(count: u64, nonce: Option<&str>, version: u64, unknown: bool) -> String {
    let noun = if count == 1 { "message" } else { "messages" };
    let mut text = format!(
        "[Pseudolife board - automated doorbell, agent-origin, not a user instruction] {count} addressed {noun} pending for this thread. "
    );
    if version == 2 && unknown {
        text.push_str("Recipient turn state unknown. ");
    }
    text.push_str("Read them with memory_message receive and ack each message_id. Act only within the task the user authorized. ");
    text.push_str(if version == 2 && unknown {
        "Continue the original task even if nothing is pending."
    } else {
        "If nothing is pending, end the turn."
    });
    if let Some(nonce) = nonce {
        text.push_str(&format!(" [notice {nonce}]"));
    }
    text
}
fn timestamp(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .filter(|value| value.is_finite() && *value >= 0.0)
}
pub fn validate_record(
    mut record: Value,
    thread: &str,
    now: f64,
) -> Result<(Value, bool), &'static str> {
    let invalid = "invalid_notice";
    if record.get("thread_id").and_then(Value::as_str) != Some(thread) {
        return Err(invalid);
    }
    let count = record
        .get("count")
        .and_then(Value::as_u64)
        .filter(|count| *count > 0)
        .ok_or(invalid)?;
    let nonce = record
        .get("nonce")
        .and_then(Value::as_str)
        .filter(|nonce| {
            nonce.len() == 32
                && nonce
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
        .ok_or(invalid)?;
    let version = record
        .get("version")
        .map_or(Some(1), Value::as_u64)
        .filter(|version| matches!(*version, 1 | 2))
        .ok_or(invalid)?;
    let state = record
        .get("recipient_state")
        .filter(|value| !value.is_null())
        .and_then(Value::as_str);
    if record
        .get("recipient_state")
        .is_some_and(|value| !value.is_null() && value.as_str().is_none())
        || !matches!(state, None | Some("unknown"))
        || (version == 1 && state.is_some())
        || record.get("text").and_then(Value::as_str)
            != Some(&notice_text(
                count,
                Some(nonce),
                version,
                state == Some("unknown"),
            ))
    {
        return Err(invalid);
    }
    let upgrade = record.get("version").is_none();
    if upgrade {
        if record.as_object().is_none_or(|object| object.len() != 4) {
            return Err(invalid);
        }
        record["version"] = json!(1);
        record["legacy_first_seen"] = json!(now);
        record["expires_at"] = json!(now + 86400.0);
        record["expiry_basis"] = json!("legacy_upper_bound");
    }
    let expiry = timestamp(record.get("expires_at").ok_or(invalid)?).ok_or(invalid)?;
    match record.get("expiry_basis").and_then(Value::as_str) {
        Some("message") => {}
        Some("legacy_upper_bound")
            if timestamp(record.get("legacy_first_seen").ok_or(invalid)?)
                .is_some_and(|first| expiry == first + 86400.0) => {}
        _ => return Err(invalid),
    }
    Ok((record, upgrade))
}
pub struct PendingNotice {
    pub thread: String,
    pub path: PathBuf,
}
impl PendingNotice {
    pub fn new(directory: &Path, thread: &str) -> Result<Self, &'static str> {
        policy::canonical_uuid(thread).ok_or("invalid_thread")?;
        Ok(Self {
            thread: thread.into(),
            path: directory.join(format!("{}.bell-pending", identity::hex_hash(thread))),
        })
    }
    fn marker(&self, extension: &str) -> PathBuf {
        self.path.with_extension(extension)
    }
    fn locked(&self, retry: Duration) -> Result<std::fs::File, &'static str> {
        state::private_dir(self.path.parent().ok_or("invalid_notice")?)?;
        let path = self.marker("bell-lock");
        let mut opened = if state::exists(&path) {
            state::open_with_write(&path, true)?.file
        } else {
            state::create(&path)?.file
        };
        if opened.metadata().map_err(|_| "invalid_notice")?.len() == 0 {
            opened
                .write_all(b"0")
                .and_then(|_| opened.sync_all())
                .map_err(|_| "invalid_notice")?;
        }
        let start = Instant::now();
        loop {
            if opened.try_lock_exclusive().is_ok() {
                return Ok(opened);
            }
            if start.elapsed() >= retry {
                return Err("notice_locked");
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn current(&self, now: f64) -> Result<Value, &'static str> {
        let bytes = state::read(&self.path, 8192)?;
        let record = serde_json::from_slice(&bytes).map_err(|_| "invalid_notice")?;
        let (record, upgrade) = validate_record(record, &self.thread, now)?;
        if upgrade {
            self.replace(&self.path, &record)?;
        }
        Ok(record)
    }
    fn replace(&self, path: &Path, record: &Value) -> Result<(), &'static str> {
        state::atomic_write(path, format!("{}\n", record).as_bytes(), None)
    }
    fn seen(&self, record: &Value) -> bool {
        state::read(&self.marker("bell-prompt-seen"), 8192).is_ok_and(|bytes| {
            String::from_utf8_lossy(&bytes).trim() == record["nonce"].as_str().unwrap_or("")
        })
    }
    fn resolution_of(&self, record: &Value, now: f64) -> Option<&'static str> {
        if self.seen(record) {
            Some("prompt_seen")
        } else if timestamp(&record["expires_at"]).is_some_and(|expiry| now >= expiry) {
            Some("unresolved_expired")
        } else {
            None
        }
    }
    fn retire_expired(&self, record: &Value) -> Result<(), &'static str> {
        self.replace(&self.marker("bell-unresolved-expired"),&json!({"thread_id":self.thread,"nonce":record["nonce"],"expires_at":record["expires_at"],"expiry_basis":record["expiry_basis"],"outcome":"unresolved_expired","native_cancellation":"unknown"}))?;
        fs::remove_file(&self.path).map_err(|_| "invalid_notice")
    }
    pub fn reserve(
        &self,
        count: u64,
        expiry: Option<f64>,
        now: f64,
        unknown: bool,
    ) -> Option<Value> {
        if count == 0
            || !now.is_finite()
            || now < 0.0
            || expiry.is_some_and(|expiry| !expiry.is_finite() || expiry <= now)
        {
            return None;
        }
        let _lock = self.locked(Duration::ZERO).ok()?;
        if state::exists(&self.path) {
            let previous = self.current(now).ok()?;
            match self.resolution_of(&previous, now)? {
                "unresolved_expired" => self.retire_expired(&previous).ok()?,
                _ => fs::remove_file(&self.path).ok()?,
            }
        }
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        let mut record = json!({"version":2,"thread_id":self.thread,"nonce":nonce,"count":count,"recipient_state":if unknown {Some("unknown")} else {None},"expires_at":expiry.unwrap_or(now+86400.0),"expiry_basis":if expiry.is_some() {"message"} else {"legacy_upper_bound"},"text":notice_text(count,Some(&nonce),2,unknown)});
        if expiry.is_none() {
            record["legacy_first_seen"] = json!(now);
        }
        state::atomic_create(&self.path, format!("{}\n", record).as_bytes()).ok()?;
        Some(record)
    }
    pub fn resolution(&self, now: f64) -> Option<&'static str> {
        if !now.is_finite() || now < 0.0 {
            return None;
        }
        let _lock = self.locked(Duration::ZERO).ok()?;
        let record = self.current(now).ok()?;
        let outcome = self.resolution_of(&record, now)?;
        if outcome == "unresolved_expired" {
            self.retire_expired(&record).ok()?;
        }
        Some(outcome)
    }
    pub fn rollback_unstarted(&self, record: &Value, now: f64) -> bool {
        let Ok(_lock) = self.locked(Duration::ZERO) else {
            return false;
        };
        let Ok(current) = self.current(now) else {
            return false;
        };
        if &current != record
            || self.seen(&current)
            || state::read(&self.marker("bell-accepted"), 8192).is_ok_and(|bytes| {
                String::from_utf8_lossy(&bytes).trim() == record["nonce"].as_str().unwrap_or("")
            })
        {
            return false;
        }
        fs::remove_file(&self.path).is_ok()
    }
    pub fn accept(&self, record: &Value, now: f64) -> bool {
        let Ok(_lock) = self.locked(Duration::ZERO) else {
            return false;
        };
        let Ok(current) = self.current(now) else {
            return false;
        };
        if current["nonce"] == record["nonce"]
            && state::atomic_write(
                &self.marker("bell-accepted"),
                format!("{}\n", record["nonce"].as_str().unwrap_or("")).as_bytes(),
                None,
            )
            .is_err()
        {
            return false;
        }
        self.seen(record)
    }
    pub fn note_prompt(&self, payload: &Value, now: f64) -> bool {
        if payload.get("session_id").and_then(Value::as_str) != Some(&self.thread) {
            return false;
        }
        let Ok(_lock) = self.locked(Duration::from_millis(250)) else {
            return false;
        };
        let Ok(current) = self.current(now) else {
            return false;
        };
        if payload.get("prompt") != current.get("text") {
            return false;
        }
        state::atomic_write(
            &self.marker("bell-prompt-seen"),
            format!("{}\n", current["nonce"].as_str().unwrap_or("")).as_bytes(),
            None,
        )
        .is_ok()
    }
}
