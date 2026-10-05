use super::json::{Text, Value, json};
use super::{args::Args, board, lock};
use chrono::Datelike;
use std::{collections::BTreeMap, fs, path::PathBuf};

pub fn truth(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
fn pystr(value: &Value) -> String {
    value.python(false)
}
pub fn clean(value: &Value, limit: usize) -> String {
    clean_text(&pystr(value), limit)
}
pub fn clean_text(text: &str, limit: usize) -> String {
    let text: String = text
        .chars()
        .map(|c| {
            if crate::board::claims::forbidden(c) {
                '?'
            } else {
                c
            }
        })
        .collect();
    if text.chars().count() > limit {
        text.chars()
            .take(limit.saturating_sub(3))
            .collect::<String>()
            + "..."
    } else {
        text
    }
}
pub fn printable(text: &str) -> String {
    text.chars()
        .map(|c| if c.is_control() { '?' } else { c })
        .take(120)
        .collect()
}
pub fn span(seconds: f64) -> String {
    if seconds >= u64::MAX as f64 {
        let integer = format!("{:.0}", seconds.trunc());
        let mut remainder = 0u64;
        let mut days = String::new();
        for digit in integer.bytes() {
            remainder = remainder * 10 + u64::from(digit - b'0');
            let quotient = remainder / 86400;
            if !days.is_empty() || quotient != 0 {
                days.push(char::from(b'0' + quotient as u8));
            }
            remainder %= 86400;
        }
        return format!("{days}d{:02}h", remainder / 3600);
    }
    let s = seconds.max(0.0) as u64;
    if s < 60 {
        format!("{s}s")
    } else if s < 3600 {
        format!("{}m", s / 60)
    } else if s < 86400 {
        format!("{}h{:02}m", s / 3600, s % 3600 / 60)
    } else {
        format!("{}d{:02}h", s / 86400, s % 86400 / 3600)
    }
}
fn number(value: &Value) -> Option<f64> {
    value.as_f64().filter(|n| n.is_finite())
}
// CPython 3.11 uses the host CRT's localtime and strftime, whose timestamp
// domains, time-zone rules and year formatting differ from Chrono's.
#[allow(unsafe_code)]
fn clock(stamp: f64) -> String {
    if !stamp.is_finite() || stamp < i64::MIN as f64 || stamp >= -(i64::MIN as f64) {
        return "?".into();
    }
    #[cfg(unix)]
    use libc::{strftime, tm as Tm};
    #[cfg(windows)]
    #[repr(C)]
    // UCRT <corecrt_wtime.h>: struct tm has nine C ints; __time64_t is i64.
    struct Tm {
        tm_sec: i32,
        tm_min: i32,
        tm_hour: i32,
        tm_mday: i32,
        tm_mon: i32,
        tm_year: i32,
        tm_wday: i32,
        tm_yday: i32,
        tm_isdst: i32,
    }
    #[cfg(windows)]
    unsafe extern "C" {
        fn _localtime64_s(local: *mut Tm, stamp: *const i64) -> i32;
        fn strftime(
            out: *mut std::ffi::c_char,
            size: usize,
            format: *const std::ffi::c_char,
            local: *const Tm,
        ) -> usize;
    }
    let convert = |stamp: i64| {
        // SAFETY: both CRT tm layouts permit zero initialization. The source
        // and output pointers stay valid for the call; only success is read.
        let mut local: Tm = unsafe { std::mem::zeroed() };
        #[cfg(windows)]
        let success = unsafe { _localtime64_s(&mut local, &stamp) == 0 };
        #[cfg(unix)]
        let success = {
            let stamp: libc::time_t = stamp.try_into().ok()?;
            unsafe { !libc::localtime_r(&stamp, &mut local).is_null() }
        };
        success.then_some(local)
    };
    let Some(local) = convert(stamp.floor() as i64) else {
        return "?".into();
    };
    let today = convert(now().floor() as i64).is_some_and(|today| {
        (local.tm_year, local.tm_mon, local.tm_mday) == (today.tm_year, today.tm_mon, today.tm_mday)
    });
    let format = if today { c"%H:%M" } else { c"%Y-%m-%d %H:%M" };
    let mut output = [0u8; 64];
    // SAFETY: the CRT receives a valid tm, a NUL-terminated static format,
    // and the writable buffer's exact length. Its count excludes the NUL.
    let count = unsafe {
        strftime(
            output.as_mut_ptr().cast(),
            output.len(),
            format.as_ptr(),
            &local,
        )
    };
    if count == 0 || count >= output.len() {
        return "?".into();
    }
    String::from_utf8(output[..count].to_vec()).unwrap_or_else(|_| "?".into())
}
pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn who(holder: &Value) -> String {
    let value = if truth(&holder["label"]) {
        holder["label"].clone()
    } else {
        let id = if truth(&holder["agent_id"]) {
            pystr(&holder["agent_id"])
        } else {
            String::new()
        };
        json!(if id.is_empty() {
            "an agent".to_owned()
        } else {
            id.chars().take(8).collect::<String>()
        })
    };
    clean(&value, 120)
}
pub fn holder(holder: &Value, now: f64) -> String {
    let mut text = who(holder);
    if truth(&holder["principal"]) {
        text += &format!(" ({})", clean(&holder["principal"], 120));
    }
    if let Some(acquired) = number(&holder["acquired_at"]) {
        text += &format!(" for {}", span(now - acquired));
    }
    if truth(&holder["purpose"]) {
        text += &format!(", purpose \"{}\"", clean(&holder["purpose"], 240));
    }
    text
}
pub fn expected(stamp: &Value, now: f64, stale: bool) -> String {
    let Some(stamp) = number(stamp) else {
        return String::new();
    };
    format!(
        ", expected end {}{}",
        clock(stamp),
        if stale || stamp < now {
            " (stale: past it)"
        } else {
            ""
        }
    )
}
fn waiter(entry: &Value) -> String {
    let mut text = who(entry);
    if let Some(since) = number(&entry["enqueued_at"]) {
        text += &format!(" since {}", clock(since));
    }
    if truth(&entry["purpose"]) {
        text += &format!(", purpose \"{}\"", clean(&entry["purpose"], 240));
    }
    text
}
pub fn queued_notice(name: &str, reply: &Value) -> String {
    let position = &reply["position"];
    let queued = &reply["queued"];
    let place = if position.is_integer() && queued.is_integer() {
        format!("position {} of {}", pystr(position), pystr(queued))
    } else {
        "queued".into()
    };
    let situation = if reply["holder"].is_object() {
        holder(&reply["holder"], now()) + &expected(&reply["holder"]["expected_end"], now(), false)
    } else {
        "no holder right now; the board is passing it down the queue".into()
    };
    format!(
        "lease: waiting for {} on the board ({place}); {}",
        super::repr(name),
        if reply["holder"].is_object() {
            format!("held by {situation}")
        } else {
            situation
        }
    )
}

pub fn json_text(value: &Value) -> String {
    value.pretty(0) + "\n"
}
fn suite_directory() -> PathBuf {
    std::env::var_os("PSEUDOLIFE_SUITE_LOCK_DIR")
        .filter(|v| !v.is_empty())
        .map(|v| PathBuf::from(v).components().collect())
        .unwrap_or_else(lock::directory)
}
fn is_suite(name: &str) -> bool {
    name == "full-suite" || name.starts_with("full-suite@")
}
fn local_suite() -> String {
    let raw = std::env::var("PSEUDOLIFE_SUITE_LEASE").ok().or_else(|| {
        fs::read_to_string(suite_directory().join("full-suite.lease"))
            .ok()
            .map(|s| s.trim_start_matches('\u{feff}').to_owned())
    });
    let Some(raw) = raw else {
        return "full-suite".into();
    };
    let name = raw.trim_matches(super::whitespace);
    if name == "full-suite"
        || name.strip_prefix("full-suite@").is_some_and(|host| {
            !host.is_empty()
                && host.len() <= 40
                && host
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
        })
    {
        name.into()
    } else {
        "full-suite".into()
    }
}
fn state(path: &std::path::Path) -> Value {
    match lock::probe(path).ok().flatten() {
        Some(true) => json!("held"),
        Some(false) => json!("free"),
        None => Value::Null,
    }
}
fn local_state(name: &str) -> Value {
    if is_suite(name) && name != local_suite() {
        return json!({"file":"full-suite.lock","state":Value::Null,"elsewhere":local_suite()});
    }
    if !is_suite(name) {
        let file = lock::file_name(name);
        return json!({"file":&file,"state":state(&lock::directory().join(&file))});
    }
    let directory = suite_directory();
    let mut seen = None;
    for slot in 0..8 {
        let file = if slot == 0 {
            "full-suite.lock".into()
        } else {
            format!("full-suite.{slot}.lock")
        };
        let s = state(&directory.join(&file));
        if s.is_null() {
            if slot == 0 {
                seen = Some(json!({"file":file,"state":Value::Null}));
            }
            continue;
        }
        if s == "free" {
            if seen.is_none() {
                seen = Some(json!({"file":file,"state":"free"}));
            }
            continue;
        }
        let mut record = json!({"file":file,"state":"held"});
        let holder = if slot == 0 {
            "full-suite.holder.json".into()
        } else {
            format!("full-suite.{slot}.holder.json")
        };
        if let Some(value) = fs::read_to_string(directory.join(holder))
            .ok()
            .and_then(|s| super::json::from_str(&s).ok())
            .filter(Value::is_object)
        {
            for key in ["pid", "worktree", "started"] {
                if let Some(v) = value.get(key) {
                    record[key] = v.clone();
                }
            }
        }
        return record;
    }
    seen.unwrap_or_else(|| json!({"file":"full-suite.lock","state":Value::Null}))
}
fn local_text(local: &Value) -> String {
    if truth(&local["elsewhere"]) {
        return format!(
            "none here (this machine's suite lease is {})",
            clean(&local["elsewhere"], 120)
        );
    }
    if local["state"].is_null() {
        return "absent".into();
    }
    if local["state"] != "held" {
        return pystr(&local["state"]);
    }
    if local.get("pid").is_none() && local.get("worktree").is_none() {
        return "held".into();
    }
    let started = match &local["started"] {
        Value::String(s) => iso_clock(s),
        _ => None,
    };
    format!(
        "held (pid {}, worktree {}{})",
        clean(local.get("pid").unwrap_or(&json!("?")), 120),
        clean(local.get("worktree").unwrap_or(&json!("?")), 240),
        started.map_or_else(String::new, |s| format!(", since {s}"))
    )
}

// Python's fromisoformat accepts basic/calendar/week dates, one arbitrary
// separator, reduced-precision times and fractional seconds or UTC offsets.
// Only the original hour and minute are displayed; no timezone conversion occurs.
fn iso_clock(text: &Text) -> Option<String> {
    // CPython sanitizes a surrogate only at the first possible date separator;
    // every other surrogate must still fail UTF-8 admission.
    let separator = [7, 8, 10].into_iter().find(|&i| {
        text.codepoints()
            .get(i)
            .is_some_and(|c| (0xd800..=0xdfff).contains(c))
    });
    let sanitized: String = text
        .codepoints()
        .iter()
        .enumerate()
        .map(|(i, &c)| {
            if separator == Some(i) {
                Some('T')
            } else {
                char::from_u32(c)
            }
        })
        .collect::<Option<_>>()?;
    let text = sanitized.as_str();
    fn digits(bytes: &[u8]) -> Option<u32> {
        bytes.iter().try_fold(0, |value, byte| {
            byte.is_ascii_digit()
                .then(|| value * 10 + u32::from(byte - b'0'))
        })
    }
    // Follow CPython 3.11's separator selection before validating the date.
    // A digit separator is ambiguous with an ISO weekday; the digit-run
    // parity and extended-week dash rule select the boundary, not date validity.
    fn date_end(bytes: &[u8]) -> Option<usize> {
        if bytes.len() < 7 {
            return None;
        }
        if bytes.len() == 7 {
            return Some(7);
        }
        if bytes[4] == b'-' {
            if bytes[5] != b'W' {
                return Some(10);
            }
            if bytes.len() > 8 && bytes[8] == b'-' {
                if bytes.len() == 9 {
                    return None;
                }
                return Some(if bytes.get(10).is_some_and(u8::is_ascii_digit) {
                    8
                } else {
                    10
                });
            }
            Some(8)
        } else if bytes[4] == b'W' {
            let end = (7..bytes.len())
                .find(|&i| !bytes[i].is_ascii_digit())
                .unwrap_or(bytes.len());
            Some(if end < 9 {
                end
            } else if end % 2 == 0 {
                7
            } else {
                8
            })
        } else {
            Some(8)
        }
    }
    // The C parser reads a separator after each two-digit component and can
    // treat surplus digits after seconds as a fraction without a decimal mark.
    // Its nonnegative trailing marker is allowed for the clock when a timezone
    // follows, but rejected for a timezone or a clock without a timezone.
    fn time(bytes: &[u8], end: usize) -> Option<([u32; 3], bool)> {
        let mut fields = [0; 3];
        let mut pos = 0;
        let mut separated = false;
        for (i, field) in fields.iter_mut().enumerate() {
            *field = digits(bytes.get(pos..pos + 2)?)?;
            pos += 2;
            let separator = bytes.get(pos).copied().unwrap_or(0);
            pos += 1;
            if i == 0 {
                separated = separator == b':';
            }
            if pos >= end {
                return Some((fields, separator != 0));
            }
            if separated && separator == b':' {
                continue;
            }
            if matches!(separator, b'.' | b',') {
                break;
            }
            if separated {
                return None;
            }
            pos -= 1;
        }
        let fraction_end = end.min(pos + 6);
        digits(bytes.get(pos..fraction_end)?)?;
        pos = fraction_end;
        while bytes.get(pos).is_some_and(u8::is_ascii_digit) {
            pos += 1;
        }
        Some((fields, bytes.get(pos).copied().unwrap_or(0) != 0))
    }
    let bytes = text.as_bytes();
    let year = digits(bytes.get(..4)?)? as i32;
    if !(1..=9999).contains(&year) {
        return None;
    }
    let end = date_end(bytes)?;
    let date = text.get(..end)?;
    let separated = bytes[4] == b'-';
    let pos = 4 + usize::from(separated);
    let valid = if bytes.get(pos) == Some(&b'W') {
        let week = digits(bytes.get(pos + 1..pos + 3)?)?;
        let weekday = if end > pos + 3 {
            let day_pos = pos + 3 + usize::from(separated);
            if separated && bytes.get(pos + 3) != Some(&b'-') {
                return None;
            }
            digits(bytes.get(day_pos..day_pos + 1)?)?
        } else {
            1
        };
        let weekday = match weekday {
            1 => chrono::Weekday::Mon,
            2 => chrono::Weekday::Tue,
            3 => chrono::Weekday::Wed,
            4 => chrono::Weekday::Thu,
            5 => chrono::Weekday::Fri,
            6 => chrono::Weekday::Sat,
            7 => chrono::Weekday::Sun,
            _ => return None,
        };
        chrono::NaiveDate::from_isoywd_opt(year, week, weekday)
            .is_some_and(|date| (1..=9999).contains(&date.year()))
    } else {
        let pattern = if separated { "dddd-dd-dd" } else { "dddddddd" };
        date.len() == pattern.len()
            && date.bytes().zip(pattern.bytes()).all(|(c, p)| {
                if p == b'd' {
                    c.is_ascii_digit()
                } else {
                    c == p
                }
            })
            && chrono::NaiveDate::parse_from_str(
                date,
                if separated { "%Y-%m-%d" } else { "%Y%m%d" },
            )
            .is_ok()
    };
    if !valid {
        return None;
    }
    let remaining = &text[end..];
    if remaining.is_empty() {
        return Some("00:00".into());
    }
    let remaining = &remaining[remaining.chars().next()?.len_utf8()..];
    let zone_pos = remaining.find(['+', '-', 'Z']).unwrap_or(remaining.len());
    let (fields, trailing) = time(remaining.as_bytes(), zone_pos)?;
    if fields[0] > 23 || fields[1] > 59 || fields[2] > 59 {
        return None;
    }
    if zone_pos == remaining.len() {
        if trailing {
            return None;
        }
    } else {
        let zone = &remaining[zone_pos..];
        if zone.starts_with('Z') {
            // CPython checks the byte after Z for NUL, including the implicit
            // terminator. Whole-string surrogate admission already ran above.
            if zone.as_bytes().get(1).is_some_and(|&byte| byte != 0) {
                return None;
            }
        } else {
            let zone = &zone[1..];
            let (fields, trailing) = time(zone.as_bytes(), zone.len())?;
            if trailing || fields[0] * 3600 + fields[1] * 60 + fields[2] >= 86400 {
                return None;
            }
        }
    }
    Some(format!("{:02}:{:02}", fields[0], fields[1]))
}
pub async fn check(args: &Args) -> i32 {
    let name = args.name.as_deref().unwrap_or("");
    let local = local_state(name);
    let mut report = json!({"available":false,"reason":Value::Null,"holder":Value::Null,"expected_end":Value::Null,"stale":false,"queued":0,"queue":json!([])});
    let mut reason = None;
    match board::connect(false) {
        Err(e) => reason = Some(e),
        Ok(client) => match client.leases(Some(name)).await {
            Err(e) => reason = Some(e.text),
            Ok((leases, _)) => {
                report["available"] = json!(true);
                for lease in leases {
                    if lease["name"] == name && lease["holder"].is_object() {
                        report["holder"] = lease["holder"].clone();
                        report["expected_end"] = lease["expected_end"].clone();
                        report["stale"] = json!(lease["stale"] == true);
                        report["queued"] = if truth(&lease["queued"]) {
                            lease["queued"].clone()
                        } else {
                            json!(0)
                        };
                        report["queue"] = if truth(&lease["queue"]) {
                            lease["queue"].clone()
                        } else {
                            json!([])
                        };
                    }
                }
            }
        },
    }
    report["reason"] = json!(reason.as_ref());
    let here = if is_suite(name) {
        suite_directory()
    } else {
        lock::directory()
    };
    let ours = lock::instance_id(&here);
    let stale = local["state"] == "free"
        && ours.is_some_and(|id| report["holder"]["label"] == format!("lease-hold@{id}"));
    let held = local["state"] == "held" || (!report["holder"].is_null() && !stale);
    let text = if args.json {
        json_text(&json!({"name":name,"held":held,"local":local,"board":report}))
    } else {
        let mut lines = vec![
            format!(
                "lease {}: {}",
                clean_text(name, 120),
                if held { "held" } else { "free" }
            ),
            format!(
                "  local lock ({}): {}",
                pystr(&local["file"]),
                local_text(&local)
            ),
        ];
        if report["available"] == true {
            if !report["holder"].is_null() {
                lines.push(format!("  board: held by {}{}{}",holder(&report["holder"],now()),expected(&report["expected_end"],now(),report["stale"]==true),if stale{"; stale: the local lock is free, so this record outlived its holder and lapses at its ttl"}else{""}));
                if truth(&report["queued"]) {
                    let kind = match &report["queue"] {
                        Value::Bool(_) => Some("bool"),
                        Value::Number(n) => Some(
                            if matches!(n, super::json::Number::Ordinary(v) if !v.to_string().contains(['.','e','E']))
                            {
                                "int"
                            } else {
                                "float"
                            },
                        ),
                        Value::Null => Some("NoneType"),
                        _ => None,
                    };
                    if let Some(kind) = kind {
                        return failed_check(
                            name,
                            &format!("TypeError: '{kind}' object is not iterable"),
                        );
                    }
                    lines.push(format!(
                        "  board queue ({}): {}",
                        pystr(&report["queued"]),
                        report["queue"].as_array().map_or_else(String::new, |q| q
                            .iter()
                            .filter(|e| e.is_object())
                            .map(waiter)
                            .collect::<Vec<_>>()
                            .join("; "))
                    ));
                }
            } else {
                lines.push("  board: free".into());
            }
        } else {
            lines.push(format!(
                "  board unavailable: {}",
                reason.unwrap_or_else(|| "None".into())
            ));
        }
        lines.join("\n") + "\n"
    };
    if let Err(error) = super::write(&text, false) {
        // CPython's redirected TextIOWrapper buffers up to 8192 encoded bytes.
        // A failed large print raises inside _check; a buffered small print
        // instead fails at interpreter shutdown and changes the exit to 120.
        let encoded = super::super::text_bytes(&text);
        let newline = if cfg!(windows) { 2 } else { 1 };
        if encoded.len().saturating_sub(newline) > 8192 {
            return failed_check(name, &super::output_error(&error));
        }
        super::flush_failure(&error);
        return 120;
    }
    i32::from(held)
}
fn failed_check(name: &str, error: &str) -> i32 {
    super::say(&format!(
        "lease: check of {} failed ({}); neither held nor free is known",
        super::repr(name),
        clean_text(error, 240)
    ));
    70
}
fn list_output(text: &str) -> i32 {
    if let Err(error) = super::write(text, false) {
        let encoded = super::super::text_bytes(text);
        let newline = if cfg!(windows) { 2 } else { 1 };
        if encoded.len().saturating_sub(newline) > 8192 {
            // Python raises from print here. Retain the terminal exception;
            // interpreter traceback presentation is explicitly deferred.
            super::say(&super::output_error(&error));
            return 1;
        }
        super::flush_failure(&error);
        return 120;
    }
    0
}
pub async fn list(args: &Args) -> i32 {
    let directory = lock::directory();
    let mut local = BTreeMap::new();
    let wanted = args.name.as_ref().map(|n| lock::file_name(n));
    if let Ok(files) = fs::read_dir(&directory) {
        for file in files.flatten() {
            let file = file.file_name().to_string_lossy().to_string();
            if file.starts_with("lease-")
                && file.ends_with(".lock")
                && wanted.as_ref().is_none_or(|w| w == &file)
            {
                let s = state(&directory.join(&file));
                if !s.is_null() {
                    local.insert(file, s);
                }
            }
        }
    }
    let suite = if args.name.is_none() {
        state(&directory.join("full-suite.lock"))
    } else {
        Value::Null
    };
    let mut url = None;
    let mut reason = None;
    let mut leases = vec![];
    let mut truncated = false;
    match board::connect(false) {
        Err(e) => reason = Some(e),
        Ok(client) => {
            url = Some(client.url.clone());
            match client.leases(args.name.as_deref()).await {
                Err(e) => reason = Some(e.text),
                Ok((l, t)) => {
                    leases = l;
                    truncated = t;
                }
            }
        }
    }
    for lease in &mut leases {
        let name = match lease["name"].utf8() {
            Ok(name) => name,
            Err(error) => {
                super::say(&error);
                return 1;
            }
        };
        lease["local_lock"] = local
            .get(&lock::file_name(name))
            .cloned()
            .unwrap_or(Value::Null);
    }
    let available = reason.is_none();
    if args.json {
        return list_output(&json_text(
            &json!({"board":json!({"url":url,"available":available,"reason":reason,"truncated":truncated,"leases":leases}),"lock_dir":directory.to_string_lossy(),"local":local.iter().map(|(f,s)|json!({"file":f,"state":s})).collect::<Vec<_>>(),"test_suite_lock":suite}),
        ));
    }
    let mut lines = vec![];
    if available {
        lines.push(format!("board: {}", url.unwrap_or_default()));
        if leases.is_empty() {
            lines.push("  no leases on the board".into());
        }
        for lease in &leases {
            let name = clean(&lease["name"], 120);
            lines.push(if lease["holder"].is_object() {
                format!(
                    "lease {name}: held by {}{}",
                    holder(&lease["holder"], now()),
                    expected(&lease["expected_end"], now(), lease["stale"] == true)
                )
            } else {
                format!("lease {name}: free")
            });
            let queue: Vec<_> = lease["queue"]
                .as_array()
                .map_or_else(Vec::new, |q| q.iter().filter(|e| e.is_object()).collect());
            let queued = if lease["queued"].is_integer() {
                lease["queued"].clone()
            } else {
                json!(queue.len() as u64)
            };
            if truth(&queued) {
                let queued = pystr(&queued);
                let waiting = queue.into_iter().map(waiter).collect::<Vec<_>>().join("; ");
                lines.push(if waiting.is_empty() {
                    format!("  queue ({queued})")
                } else {
                    format!("  queue ({queued}): {waiting}")
                });
            }
            lines.push(format!(
                "  local lock: {}",
                if lease["local_lock"].is_null() {
                    "absent".into()
                } else {
                    pystr(&lease["local_lock"])
                }
            ));
        }
        if truncated {
            lines.push(format!(
                "  (the board listed only the first {})",
                leases.len()
            ));
        }
    } else {
        lines.push(format!(
            "board unavailable: {}; showing local locks only",
            reason.unwrap_or_default()
        ));
    }
    for (file, s) in &local {
        if !leases
            .iter()
            .any(|l| lock::file_name(l["name"].as_str().expect("validated UTF-8 name")) == *file)
        {
            lines.push(format!(
                "local lock {file}: {}{}",
                pystr(s),
                if available { " (not on the board)" } else { "" }
            ));
        }
    }
    if local.is_empty() {
        lines.push(format!("no local lease locks in {}", directory.display()));
    }
    if !suite.is_null() {
        lines.push(format!("test-suite lock: {}", pystr(&suite)));
    }
    list_output(&(lines.join("\n") + "\n"))
}

#[cfg(test)]
mod tests {
    #[test]
    fn holder_iso_clock_matches_cpython_311_grammar_grid() {
        // Expectations captured with datetime.fromisoformat, including its
        // week-date separator disambiguation and rejected lexical forms.
        let grid = super::super::json::from_str(include_str!(
            "../../../tests/fixtures/lease_iso_clock_cpython311.json"
        ))
        .unwrap();
        let failures: Vec<_> = grid["cases"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|case| {
                let super::super::json::Value::String(input) = &case["input"] else {
                    panic!("expected string input");
                };
                let expected = case["clock"].as_str();
                let actual = super::iso_clock(input);
                (actual.as_deref() != expected).then(|| {
                    format!(
                        "{}: expected {expected:?}, got {actual:?}",
                        case["input"].python(false)
                    )
                })
            })
            .collect();
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }
}
