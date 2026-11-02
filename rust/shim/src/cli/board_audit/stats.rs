//! Local files remain on the invoking host, including container stats output.
use super::{PythonLines, args::Stats, bank, codec, metrics};
use serde_json::{Value, json};
use std::{io::Write, path::PathBuf};

pub(super) fn error(message: impl std::fmt::Display) -> u8 {
    crate::stderrln!("board-audit: {message}");
    2
}

pub(super) fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |v| v.as_secs_f64())
}

pub(super) fn window(args: &Stats, now: f64) -> Result<(f64, f64), u8> {
    let until = args.until.unwrap_or(now);
    let since = args.since.unwrap_or(until - 86400.0);
    if since >= until {
        return Err(error("--since must be before --until"));
    }
    if let Some(out) = &args.out {
        if std::path::Path::new(out).exists() {
            return Err(error(format!(
                "{out} exists; stats never replaces a file (--append adds a line to a running log instead)"
            )));
        }
    }
    Ok((since, until))
}

pub(super) fn durations_path(args: &Stats) -> PathBuf {
    if let Some(path) = &args.durations {
        return path.into();
    }
    let directory = std::env::var_os("PSEUDOLIFE_SUITE_LOCK_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            dirs::home_dir()
                .unwrap_or_default()
                .join(".pseudolife-mcp")
                .join("locks")
        });
    directory.join("full-suite.durations.jsonl")
}

fn durations(args: &Stats) -> (Vec<Value>, bool) {
    let Ok(text) = std::fs::read_to_string(durations_path(args)) else {
        return (Vec::new(), false);
    };
    let records = text
        .split([
            '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
            '\u{2029}',
        ])
        .filter_map(|s| codec::python_loads(s).ok())
        .filter_map(|v| {
            use super::super::pyjson::J;
            let J::Dict(fields) = v else {
                return None;
            };
            let mut record = serde_json::Map::new();
            for (key, value) in fields {
                let Some(key) = key.as_str() else {
                    continue;
                };
                if ["ended", "seconds", "queued_at", "started_at"].contains(&key) {
                    record.insert(key.to_owned(), payload(value).unwrap_or(Value::Null));
                }
            }
            Some(Value::Object(record))
        })
        .filter(Value::is_object)
        .collect();
    (records, true)
}

/// Telemetry's numeric readers reject non-finite values. Keep their parent
/// records, since valid queue stamps or unrelated event fields still count.
pub(super) fn payload(value: super::super::pyjson::J) -> Result<Value, ()> {
    use super::super::pyjson::J;
    Ok(match value {
        J::Null => Value::Null,
        J::Bool(v) => Value::Bool(v),
        J::Int(v) => serde_json::from_str(&v).map_err(|_| ())?,
        J::Float(v) => serde_json::Number::from_f64(v).map_or(Value::Null, Value::Number),
        J::Str(v) => Value::String(v),
        J::Wtf(_) => return Err(()),
        J::List(values) => Value::Array(values.into_iter().map(payload).collect::<Result<_, _>>()?),
        J::Dict(values) => {
            let mut object = serde_json::Map::new();
            for (key, value) in values {
                let J::Str(key) = key else {
                    return Err(());
                };
                object.insert(key, payload(value)?);
            }
            Value::Object(object)
        }
    })
}

fn export_events(input: &str) -> Result<Vec<Value>, u8> {
    let bytes = std::fs::read(input).map_err(|_| error(format!("cannot read {input}")))?;
    let mut lines = PythonLines::new(&bytes);
    let mut events = Vec::new();
    let mut line_number = 0;
    loop {
        let line = match lines.next() {
            Ok(Some(line)) => line,
            Ok(None) => break,
            Err(()) => {
                return Err(error(format!(
                    "{input} is not UTF-8 JSON lines (write exports with --out rather than a shell redirect)"
                )));
            }
        };
        line_number += 1;
        if super::archive::blank(&line) {
            continue;
        }
        let (mut row,_,_,payload) = super::archive::row(&line).map_err(|e| match e {
            super::archive::Error::Deferred => super::deferred(),
            super::archive::Error::Encoding => super::failed(),
            e => error(format!("{input} line {line_number} {}", match e {
                super::archive::Error::Duplicate => "has a duplicate key, so a reader and verify could see different values; it cannot be checked",
                super::archive::Error::NotJson => "is not JSON", _ => "is not an exported audit event",
            })),
        })?;
        if let Some(payload) = payload {
            row["payload"] = self::payload(payload).map_err(|_| super::deferred())?;
        }
        events.push(row);
    }
    Ok(events)
}

pub(super) fn emit(args: &Stats, line: &[u8]) -> u8 {
    if let Some(out) = &args.out {
        if let Err(e) = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(out)
            .and_then(|mut f| f.write_all(line).and_then(|()| f.flush()))
        {
            return error(format!(
                "cannot write {out}: {}",
                bank::write_strerror(&e).unwrap_or_else(|| "I/O error".into())
            ));
        }
    }
    if let Some(append) = &args.append {
        if let Err(e) = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(append)
            .and_then(|mut f| f.write_all(line).and_then(|()| f.flush()))
        {
            return error(format!(
                "cannot append to {append}: {}",
                bank::write_strerror(&e).unwrap_or_else(|| "I/O error".into())
            ));
        }
    }
    if args.out.is_none() && args.append.is_none() {
        let text = String::from_utf8_lossy(line);
        if std::io::stdout()
            .lock()
            .write_all(&super::super::text_bytes(&text))
            .and_then(|()| std::io::stdout().flush())
            .is_err()
        {
            return error("the output was closed before the export finished");
        }
    }
    0
}

pub(super) fn run(args: Stats) -> u8 {
    let now = now();
    let (since, until) = match window(&args, now) {
        Ok(v) => v,
        Err(code) => return code,
    };
    let (durations, found) = durations(&args);
    let (mut events, wakes, sources) = if let Some(input) = &args.input {
        let events = match export_events(input) {
            Ok(e) => e,
            Err(code) => return code,
        };
        (
            events,
            Vec::new(),
            json!({"durations":if found { "file" } else { "absent" },"events":"export","wakes":"none"}),
        )
    } else {
        let Some(dsn) = bank::dsn() else {
            return super::transport::stats(&args);
        };
        let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
        else {
            return super::failed();
        };
        let (events, wakes) =
            match runtime.block_on(bank::stats(&dsn, since - 604800.0, until, since)) {
                Ok(v) => v,
                Err(bank::Ending::Exit(code)) => return code,
                Err(_) => return super::failed(),
            };
        let source = if wakes.is_some() { "bank" } else { "absent" };
        (
            events,
            wakes.unwrap_or_default(),
            json!({"durations":if found { "file" } else { "absent" },"events":"bank","wakes":source}),
        )
    };
    events.retain(|e| {
        metrics::number(&e["created_at"]).is_some_and(|t| since - 604800.0 <= t && t < until)
    });
    // The oracle raises on a truthy non-object payload when it reads fields.
    if events.iter().any(|e| {
        let kind = e["event"].as_str().unwrap_or("");
        let window = metrics::number(&e["created_at"]).unwrap_or(f64::NEG_INFINITY) >= since;
        (kind == "update"
            || window && matches!(kind, "send" | "attach" | "prune")
            || window && kind == "read" && e["actor"] == "agent")
            && !e["payload"].is_object()
            && !e["payload"].is_null()
            && !matches!(&e["payload"],Value::String(s) if s.is_empty())
            && !matches!(&e["payload"],Value::Array(a) if a.is_empty())
            && e["payload"] != false
            && e["payload"] != 0
    }) {
        return super::failed();
    }
    let report = metrics::compute(events, wakes, durations, since, until, now, sources);
    let mut text = String::new();
    if codec::ascii_compact(&report, &mut text).is_none() {
        return super::failed();
    }
    text.push('\n');
    emit(&args, text.as_bytes())
}
