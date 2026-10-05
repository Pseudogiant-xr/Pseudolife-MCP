use super::json::{Value, json};
use super::{args::Args, board, lock};
use chrono::{Local, TimeZone};
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
fn clock(stamp: f64) -> String {
    if !stamp.is_finite() || stamp.abs() > i64::MAX as f64 {
        return "?".into();
    }
    let Some(local) = Local.timestamp_opt(stamp.floor() as i64, 0).single() else {
        return "?".into();
    };
    local
        .format(if local.date_naive() == Local::now().date_naive() {
            "%H:%M"
        } else {
            "%Y-%m-%d %H:%M"
        })
        .to_string()
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
    let started = local["started"].as_str().and_then(|s| {
        chrono::DateTime::parse_from_rfc3339(s)
            .map(|d| d.format("%H:%M").to_string())
            .ok()
            .or_else(|| {
                chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f")
                    .map(|d| d.format("%H:%M").to_string())
                    .ok()
            })
    });
    format!(
        "held (pid {}, worktree {}{})",
        clean(local.get("pid").unwrap_or(&json!("?")), 120),
        clean(local.get("worktree").unwrap_or(&json!("?")), 240),
        started.map_or_else(String::new, |s| format!(", since {s}"))
    )
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
        lease["local_lock"] = local
            .get(&lock::file_name(lease["name"].as_str().unwrap_or("")))
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
            .any(|l| lock::file_name(l["name"].as_str().unwrap_or("")) == *file)
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
