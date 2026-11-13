//! Small installer reads that used to be inline `python -c` programs:
//! a field of a JSON report, a JSON string literal, `connect --json` report
//! lines, the loopback test of a daemon URL and the pairing-code test.
//! Each reads its input from stdin or one argument and writes plain text.
use serde_json::Value;
use std::io::{self, Read, Write};

const STDIN_LIMIT: u64 = 4 * 1024 * 1024;

fn stdin_text() -> String {
    let mut bytes = Vec::new();
    let _ = io::stdin().lock().take(STDIN_LIMIT).read_to_end(&mut bytes);
    String::from_utf8_lossy(&bytes).into_owned()
}

fn stdin_json() -> Option<Value> {
    let text = stdin_text();
    serde_json::from_str(text.trim_start_matches('\u{feff}')).ok()
}

fn out(text: &str) -> u8 {
    let mut stdout = io::stdout().lock();
    u8::from(writeln!(stdout, "{text}").is_err())
}

/// `json-field FIELD`: the field of the JSON object on stdin when it is a
/// string, else an empty line (pair's report, the compose plan).
pub(super) fn json_field(field: &str) -> u8 {
    let value = stdin_json().and_then(|report| report.get(field).cloned());
    out(value.as_ref().and_then(Value::as_str).unwrap_or(""))
}

/// `json-string`: the JSON string literal on stdin as raw text, exit 1 when
/// it is not one (a recorded instruction target).
pub(super) fn json_string() -> u8 {
    match stdin_json() {
        Some(Value::String(text)) => {
            let mut stdout = io::stdout().lock();
            u8::from(stdout.write_all(text.as_bytes()).is_err())
        }
        _ => 1,
    }
}

/// `connect-report valid|error|plan|manual|handled` over `connect --json`.
pub(super) fn connect_report(mode: &str) -> u8 {
    let report = stdin_json();
    let valid = report.as_ref().is_some_and(|r| {
        r.is_object() && r.get("rows").is_some_and(Value::is_array) && r.get("exit").is_some()
    });
    if mode == "valid" {
        return u8::from(!valid);
    }
    let Some(report) = report.filter(|_| valid) else {
        return 0;
    };
    if mode == "error" {
        return out(report.get("error").and_then(Value::as_str).unwrap_or(""));
    }
    let rows: Vec<&serde_json::Map<String, Value>> = report["rows"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
        .collect();
    let text = |row: &serde_json::Map<String, Value>, key: &str| -> String {
        match row.get(key) {
            None | Some(Value::Null) => "None".to_owned(),
            Some(Value::String(s)) => s.clone(),
            Some(other) => other.to_string(),
        }
    };
    if mode == "handled" {
        let names: Vec<String> = rows
            .iter()
            .filter(|row| {
                row.get("place").and_then(Value::as_str) == Some("registration")
                    && matches!(
                        row.get("state").and_then(Value::as_str),
                        Some("current" | "change")
                    )
            })
            .map(|row| text(row, "client"))
            .collect();
        return out(&names.join(" "));
    }
    let mut lines = Vec::new();
    for row in rows {
        let state = row.get("state").and_then(Value::as_str);
        if state == Some("absent") || (mode == "manual" && state != Some("manual")) {
            continue;
        }
        let file = row.get("file").and_then(Value::as_str).unwrap_or("");
        let key = match row.get("key") {
            Some(Value::String(k)) if !k.is_empty() => format!("[{k}]"),
            _ => String::new(),
        };
        let where_ = [file, key.as_str()]
            .iter()
            .filter(|p| !p.is_empty())
            .copied()
            .collect::<Vec<_>>()
            .join(" ");
        lines.push(format!(
            "    {:<8} {} {}{}",
            text(row, "state"),
            text(row, "client"),
            text(row, "place"),
            if where_.is_empty() {
                String::new()
            } else {
                format!(": {where_}")
            }
        ));
        if let Some(changes) = row.get("changes").and_then(Value::as_object) {
            for (name, change) in changes {
                let pair = change.as_array().cloned().unwrap_or_default();
                let shown = |value: Option<&Value>, none: &str| match value {
                    None | Some(Value::Null) => none.to_owned(),
                    Some(Value::String(s)) => s.clone(),
                    Some(other) => other.to_string(),
                };
                lines.push(format!(
                    "             {name}: {} -> {}",
                    shown(pair.first(), "(unset)"),
                    shown(pair.get(1), "(removed)")
                ));
            }
        }
        if let Some(detail) = row
            .get("detail")
            .and_then(Value::as_str)
            .filter(|d| !d.is_empty())
        {
            lines.push(format!("             {detail}"));
        }
    }
    if lines.is_empty() {
        return 0;
    }
    out(&lines.join("\n"))
}

/// `codex-fields credentials|hooks|runtime-defaults`: the fields the
/// installers read from a Codex step's JSON report on stdin, one per line,
/// after checking its shape; exit 1 (nothing printed) for any other report.
pub(super) fn codex_fields(kind: &str) -> u8 {
    match codex_lines(kind, stdin_json()) {
        Some(lines) => out(&lines.join("
")),
        None => 1,
    }
}

fn codex_lines(kind: &str, report: Option<Value>) -> Option<Vec<String>> {
    let report = report.filter(Value::is_object)?;
    let field = |key: &str| report.get(key).unwrap_or(&Value::Null);
    let one_of = |key: &str, allowed: &[&str]| field(key).as_str().is_some_and(|v| allowed.contains(&v));
    // Python's `str(value or "")`, kept on one line.
    let text = |key: &str| -> String {
        let shown = match field(key) {
            Value::Null | Value::Bool(false) => String::new(),
            Value::String(s) => s.clone(),
            Value::Bool(true) => "True".to_owned(),
            Value::Number(n) if n.as_f64() == Some(0.0) => String::new(),
            other => other.to_string(),
        };
        shown.lines().collect::<Vec<_>>().join(" ")
    };
    match kind {
        "credentials" => {
            if !one_of("status", &["ready", "tokenless"]) {
                return None;
            }
            let (Some(file), Some(connection)) =
                (field("credential_file_configured").as_bool(), field("connection_configured").as_bool())
            else {
                return None;
            };
            let flag = |b: bool| if b { "1" } else { "0" }.to_owned();
            Some(vec![flag(file), flag(connection), text("credential_file_path"), text("daemon_url")])
        }
        "hooks" => {
            let valid = one_of("status", &["ready", "pending", "unavailable", "skipped"])
                && one_of("source", &["manual", "plugin", "skip"])
                && one_of("instructions", &["present", "appended", "skipped", "covered-by-hooks"]);
            valid.then(|| ["status", "source", "instructions", "recovery"].map(text).to_vec())
        }
        "runtime-defaults" => (field("status") == "ready"
            && one_of("runtime_defaults", &["configured", "preserved"]))
        .then(|| vec![text("runtime_defaults")]),
        _ => None,
    }
}

/// `is-loopback URL`: exit 10 when the URL names this machine, 11 otherwise.
pub(super) fn is_loopback(url: &str) -> u8 {
    if crate::daemon_url::is_loopback(url) {
        10
    } else {
        11
    }
}

/// `pairing-code-valid`: the first stdin line; exit 10 when it is a pairing
/// code, 11 otherwise. The code itself is never echoed.
pub(super) fn pairing_code_valid() -> u8 {
    let text = stdin_text();
    let line = text
        .lines()
        .next()
        .unwrap_or("")
        .trim_end_matches(['\r', '\n']);
    if crate::cli::pairing::normalize_for_connect(line).is_some() {
        10
    } else {
        11
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    #[test]
    fn codex_reports_read_as_the_installers_python_did() {
        let lines = |kind: &str, v: serde_json::Value| super::codex_lines(kind, Some(v));
        assert_eq!(
            lines("credentials", json!({"status": "tokenless", "credential_file_configured": false, "connection_configured": true, "credential_file_path": "", "daemon_url": null})),
            Some(vec!["0".into(), "1".into(), "".into(), "".into()])
        );
        assert_eq!(lines("credentials", json!({"status": "ready", "credential_file_configured": 1, "connection_configured": true})), None);
        assert_eq!(
            lines("hooks", json!({"status": "pending", "source": "plugin", "instructions": "present", "recovery": "line one
line two"})),
            Some(vec!["pending".into(), "plugin".into(), "present".into(), "line one line two".into()])
        );
        assert_eq!(lines("hooks", json!({"status": "ready", "source": "elsewhere", "instructions": "present"})), None);
        assert_eq!(lines("runtime-defaults", json!({"status": "ready", "runtime_defaults": "preserved"})), Some(vec!["preserved".into()]));
        assert_eq!(lines("runtime-defaults", json!({"status": "needs-configuration"})), None);
        assert_eq!(super::codex_lines("hooks", None), None);
    }

    #[test]
    fn loopback_answers_with_the_installer_exit_codes() {
        assert_eq!(super::is_loopback("http://127.0.0.1:8765"), 10);
        assert_eq!(super::is_loopback("http://localhost:8765"), 10);
        assert_eq!(super::is_loopback("http://[::1]:8765"), 10);
        assert_eq!(super::is_loopback("http://100.64.0.2:8765"), 11);
        assert_eq!(super::is_loopback("http://daemon.example.com"), 11);
    }
}
