//! Installer-only helpers (`pseudolife-mcp installer ...`), called by
//! `ops/install.sh` and `ops/install.ps1` after they installed the native
//! client. Not listed in `--help`: people run the installers, not these.
//! Nothing here starts an interpreter, pip or pipx.
use crate::runtime::{self, Layout, registrations};
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    io::{self, Write},
    path::{Path, PathBuf},
};

const USAGE: &str = "usage: pseudolife-mcp installer migrate-registrations [--client claude-code|claude-desktop|codex|gemini]... [--json]";
const CLIENTS: [&str; 4] = ["claude-code", "claude-desktop", "codex", "gemini"];

/// Arguments follow `installer`. Exit 0 when every selected registration
/// runs the native launcher or was left alone on purpose, 1 when the native
/// client is missing or a move failed, 2 for a usage error (nothing read).
pub fn run(arguments: Vec<OsString>) -> u8 {
    let arguments: Option<Vec<String>> = arguments
        .into_iter()
        .map(|a| a.into_string().ok())
        .collect();
    let Some(arguments) = arguments else {
        return usage("arguments must be valid Unicode");
    };
    match arguments.first().map(String::as_str) {
        Some("migrate-registrations") => match parse_migrate(&arguments[1..]) {
            Ok(Some(options)) => migrate_registrations(&options),
            Ok(None) => emit(USAGE),
            Err(message) => usage(&message),
        },
        Some("--help" | "-h") => emit(USAGE),
        _ => usage("expected an installer step: migrate-registrations"),
    }
}

#[derive(Debug, Default, PartialEq, Eq)]
struct MigrateOptions {
    clients: Vec<String>,
    json: bool,
}

fn parse_migrate(arguments: &[String]) -> Result<Option<MigrateOptions>, String> {
    let mut options = MigrateOptions::default();
    let mut items = arguments.iter();
    while let Some(item) = items.next() {
        match item.as_str() {
            "--help" | "-h" => return Ok(None),
            "--json" if !options.json => options.json = true,
            "--client" => {
                let client = items.next().ok_or("--client needs a client name")?;
                if !CLIENTS.contains(&client.as_str()) {
                    return Err(format!(
                        "unknown client {client:?}; use one of {}",
                        CLIENTS.join(", ")
                    ));
                }
                if !options.clients.contains(client) {
                    options.clients.push(client.clone());
                }
            }
            other => return Err(format!("unknown or repeated option {other:?}")),
        }
    }
    Ok(Some(options))
}

/// What happens to a registration of each kind (`Registration::install_kind`):
/// `managed` (already names the launcher or a numbered runtime) and `legacy`
/// (a pipx, `pip --user` or Python venv install this repository made) move to
/// the native launcher with a private backup; `editable` (a contributor's
/// checkout), `spawning` (a client that starts its own daemon) and
/// `unknown`/`unmanaged` are left as they are, because the installer cannot
/// tell they belong to it.
fn migrate_registrations(options: &MigrateOptions) -> u8 {
    let report = (|| -> Result<Value, String> {
        let layout = runtime::default_layout()?;
        if runtime::current_runtime(&layout).is_none() || !layout.launcher.is_file() {
            return Err(format!(
                "the native client is not installed at {}; run the installer again so it installs the native client first; nothing was changed",
                layout.launcher.display()
            ));
        }
        let _lock = runtime::UpdateLock::acquire(&layout)?;
        let found: Vec<_> = registrations::find_registrations()?
            .into_iter()
            .filter(|r| options.clients.is_empty() || options.clients.contains(&r.client))
            .collect();
        let mut moved = Vec::new();
        let mut left = Vec::new();
        for registration in &found {
            let kind = registration.install_kind(&layout, None);
            if matches!(kind, "managed" | "legacy") {
                let mut row = registrations::migrate(registration, &layout);
                row["previous"] = json!(registration.command);
                moved.push(row);
            } else {
                left.push(json!({
                    "client": registration.client,
                    "file": registration.file,
                    "state": kind,
                    "command": registration.command,
                    "detail": left_detail(kind),
                }));
            }
        }
        let failed = moved.iter().any(|row| row["state"] == "failed");
        let python_runtimes: Vec<_> = python_runtimes(&layout)
            .into_iter()
            .map(|p| json!(p))
            .collect();
        Ok(json!({
            "state": if failed { "failed" } else { "ok" },
            "launcher": layout.launcher,
            "registrations": moved,
            "left": left,
            "python_runtimes": python_runtimes,
        }))
    })();
    let report = report.unwrap_or_else(|detail| json!({"state": "failed", "detail": detail}));
    let code = u8::from(report["state"] != "ok");
    if options.json {
        if emit(&report.to_string()) != 0 {
            return 1;
        }
    } else if emit(&report_lines(&report)) != 0 {
        return 1;
    }
    code
}

fn left_detail(kind: &str) -> &'static str {
    match kind {
        "editable" => "an editable checkout install; left as it is (a contributor's own choice)",
        "spawning" => "a registration that starts its own daemon; left as it is",
        _ => "not a Pseudolife install this installer made; left as it is",
    }
}

/// Numbered runtime directories holding a Python virtual environment: the
/// old Python client, kept on disk (unregistered) for rollback.
fn python_runtimes(layout: &Layout) -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = std::fs::read_dir(&layout.root)
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_str().and_then(runtime::sequence).is_some())
        .map(|e| e.path())
        .filter(|p| p.join("pyvenv.cfg").is_file())
        .collect();
    found.sort();
    found
}

fn report_lines(report: &Value) -> String {
    if report["state"] == "failed" && report.get("detail").is_some() {
        return format!(
            "native client registrations: {}",
            report["detail"].as_str().unwrap_or("failed")
        );
    }
    let mut lines = Vec::new();
    let rows = report["registrations"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let names = |state: &str| -> Vec<String> {
        rows.iter()
            .filter(|r| r["state"] == state)
            .filter_map(|r| r["client"].as_str().map(str::to_owned))
            .collect()
    };
    let migrated = names("migrated");
    let current = names("current");
    if !migrated.is_empty() {
        lines.push(format!(
            "moved {} to the native client at {} (each changed file has a private backup)",
            migrated.join(", "),
            report["launcher"].as_str().unwrap_or("")
        ));
    }
    if migrated.is_empty() && !current.is_empty() {
        lines.push(format!(
            "{} already run the native client; nothing changed",
            current.join(", ")
        ));
    }
    if rows.is_empty() && report["left"].as_array().is_none_or(Vec::is_empty) {
        lines.push("no Pseudolife registration found; nothing changed".into());
    }
    for row in rows.iter().filter(|r| r["state"] == "failed") {
        lines.push(format!(
            "WARNING: {} was not moved: {}",
            row["client"].as_str().unwrap_or("?"),
            row["detail"].as_str().unwrap_or("failed")
        ));
    }
    for row in report["left"].as_array().into_iter().flatten() {
        lines.push(format!(
            "{} still runs {}: {}",
            row["client"].as_str().unwrap_or("?"),
            row["command"].as_str().unwrap_or("?"),
            row["detail"].as_str().unwrap_or("left as it is")
        ));
    }
    let legacy: Vec<&str> = rows
        .iter()
        .filter(|r| r["state"] == "migrated")
        .filter_map(|r| r["previous"].as_str())
        .filter(|p| {
            !runtime::same_path(
                Path::new(p),
                Path::new(report["launcher"].as_str().unwrap_or("")),
            )
        })
        .collect();
    let python: Vec<&str> = report["python_runtimes"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    // Named once, on the run that moved something; a rerun stays one line.
    if !migrated.is_empty() && (!legacy.is_empty() || !python.is_empty()) {
        let mut places: Vec<&str> = legacy;
        places.extend(python);
        lines.push(format!(
            "the old Python client stays on disk, unregistered: {} (delete it, or `pipx uninstall pseudolife-mcp` for a pipx install, once you no longer need it)",
            places.join(", ")
        ));
    }
    lines.join("\n")
}

fn usage(message: &str) -> u8 {
    let _ = writeln!(io::stderr().lock(), "installer: {message}\n{USAGE}");
    2
}

fn emit(text: &str) -> u8 {
    if text.is_empty() {
        return 0;
    }
    u8::from(writeln!(io::stdout().lock(), "{text}").is_err())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn migrate_options_parse_clients_once_and_refuse_unknown_names() {
        assert_eq!(
            parse_migrate(&strings(&[
                "--client", "codex", "--client", "codex", "--json"
            ])),
            Ok(Some(MigrateOptions {
                clients: strings(&["codex"]),
                json: true
            }))
        );
        assert!(
            parse_migrate(&strings(&["--client", "claude"]))
                .unwrap_err()
                .contains("unknown client")
        );
        assert!(parse_migrate(&strings(&["--client"])).is_err());
        assert!(parse_migrate(&strings(&["--json", "--json"])).is_err());
        assert_eq!(parse_migrate(&strings(&["--help"])), Ok(None));
    }

    #[test]
    fn report_names_moved_clients_and_the_python_client_left_on_disk() {
        let report = json!({
            "state": "ok", "launcher": "/home/u/bin/pseudolife-mcp",
            "registrations": [
                {"client": "codex", "state": "migrated", "previous": "/home/u/.local/bin/pseudolife-mcp"},
                {"client": "gemini", "state": "current", "previous": "/home/u/bin/pseudolife-mcp"},
            ],
            "left": [{"client": "claude-code", "command": "/src/.venv/bin/pseudolife-mcp", "state": "editable", "detail": "an editable checkout install; left as it is (a contributor's own choice)"}],
            "python_runtimes": ["/home/u/runtimes/000001"],
        });
        let text = report_lines(&report);
        assert!(
            text.contains("moved codex to the native client at /home/u/bin/pseudolife-mcp"),
            "{text}"
        );
        assert!(!text.contains("already run"), "{text}");
        assert!(
            text.contains("claude-code still runs /src/.venv/bin/pseudolife-mcp"),
            "{text}"
        );
        assert!(
            text.contains("/home/u/.local/bin/pseudolife-mcp, /home/u/runtimes/000001"),
            "{text}"
        );
    }

    #[test]
    fn report_says_one_line_when_nothing_changes() {
        let report = json!({
            "state": "ok", "launcher": "/l",
            "registrations": [{"client": "codex", "state": "current", "previous": "/l"}],
            "left": [], "python_runtimes": [],
        });
        assert_eq!(
            report_lines(&report),
            "codex already run the native client; nothing changed"
        );
    }
}
