//! Installer-only helpers (`pseudolife-mcp installer ...`), called by
//! `ops/install.sh` and `ops/install.ps1` after they installed the native
//! client. Not listed in `--help`: people run the installers, not these.
//! Nothing here starts an interpreter, pip or pipx.
mod claude_desktop;
mod codex_config;
#[cfg(test)]
mod fake_codex;

use crate::runtime::{self, Layout, registrations};
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    io::{self, Write},
    path::{Path, PathBuf},
};

const USAGE: &str = "usage: pseudolife-mcp installer migrate-registrations [--client claude-code|claude-desktop|codex|gemini]... [--json]
       pseudolife-mcp installer codex-credentials [--installer-daemon-url URL] [--installer-token-stdin]
       pseudolife-mcp installer codex-runtime-defaults
       pseudolife-mcp installer claude-desktop --command ABSOLUTE_PATH [options; see --help]";
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
        Some("codex-credentials") => codex_config::run_credentials(&arguments[1..]),
        Some("codex-runtime-defaults") => codex_config::run_runtime_defaults(&arguments[1..]),
        Some("claude-desktop") => claude_desktop::run(&arguments[1..]),
        Some("--help" | "-h") => emit(USAGE),
        _ => usage(
            "expected an installer step: migrate-registrations, codex-credentials, codex-runtime-defaults, claude-desktop",
        ),
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
        // The stable launcher must be the selected native binary itself: a
        // Python updater may have rewritten it while the runtime stayed.
        let native = runtime::current_runtime(&layout).is_some_and(|current| {
            runtime::hash_file(&layout.launcher).ok().as_deref()
                == Some(current.marker.binary_sha256.as_str())
        });
        if !native {
            return Err(format!(
                "the native client is not installed at {}; run the installer again so it installs the native client first; nothing was changed",
                layout.launcher.display()
            ));
        }
        let _lock = runtime::UpdateLock::acquire(&layout)?;
        let selected = |client: &str| {
            options.clients.is_empty() || options.clients.iter().any(|c| c == client)
        };
        let found = registrations::find_selected_registrations(selected)
            .map_err(|detail| unreadable(&detail, &selected))?;
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
        let mut old: Vec<PathBuf> = moved
            .iter()
            .filter(|row| row["state"] == "migrated")
            .filter_map(|row| row["previous"].as_str().map(PathBuf::from))
            .filter(|path| !runtime::same_path(path, &layout.launcher))
            .map(|path| venv_root(&path).unwrap_or(path))
            .collect();
        old.extend(python_runtimes(&layout));
        old.sort();
        old.dedup();
        // An old install is unregistered only when no client still runs it:
        // another client, an unselected one or a failed move may.
        let (unregistered, in_use, unconfirmed) = match registrations::find_registrations() {
            Ok(remaining) => {
                let (in_use, unregistered): (Vec<_>, Vec<_>) = old.into_iter().partition(|place| {
                    remaining.iter().any(|r| uses(Path::new(&r.command), place))
                });
                (unregistered, in_use, false)
            }
            Err(_) => (Vec::new(), old, true),
        };
        Ok(json!({
            "state": if failed { "failed" } else { "ok" },
            "launcher": layout.launcher,
            "registrations": moved,
            "left": left,
            "old_unregistered": unregistered,
            "old_in_use": in_use,
            "old_unconfirmed": unconfirmed,
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
        "unknown" => "could not verify which install this is; left as it is",
        _ => "not a Pseudolife install this installer made; left as it is",
    }
}

/// The discovery error, naming the client whose configuration failed.
fn unreadable(detail: &str, selected: &dyn Fn(&str) -> bool) -> String {
    let culprit = CLIENTS
        .iter()
        .copied()
        .filter(|c| selected(c))
        .find(|client| registrations::find_selected_registrations(|c| c == *client).is_err());
    match culprit {
        Some(client) => format!(
            "the {client} configuration cannot be read ({detail}); nothing was changed: fix or remove that file (or rerun with only the other clients), then rerun"
        ),
        None => format!("client configurations cannot be read ({detail}); nothing was changed"),
    }
}

/// The venv a console script or interpreter belongs to, when it is one.
fn venv_root(command: &Path) -> Option<PathBuf> {
    let root = command.parent()?.parent()?;
    root.join("pyvenv.cfg")
        .is_file()
        .then(|| root.to_path_buf())
}

/// Whether a registered `command` runs the old install at `place`: the same
/// file, a file inside that directory, or a script of the same venv.
fn uses(command: &Path, place: &Path) -> bool {
    if runtime::same_path(command, place) || command.starts_with(place) {
        return true;
    }
    let root = |path: &Path| path.parent().and_then(Path::parent).map(Path::to_path_buf);
    match (root(command), root(place)) {
        (Some(a), Some(b)) => a.join("pyvenv.cfg").is_file() && runtime::same_path(&a, &b),
        _ => false,
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
    let paths = |key: &str| -> Vec<String> {
        report[key]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| v.as_str().map(str::to_owned))
            .collect()
    };
    let migrated = names("migrated");
    let current = names("current");
    let failed: Vec<&Value> = rows.iter().filter(|r| r["state"] == "failed").collect();
    let left: Vec<String> = report["left"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|row| {
            format!(
                "{} still runs {}: {}",
                row["client"].as_str().unwrap_or("?"),
                row["command"].as_str().unwrap_or("?"),
                row["detail"].as_str().unwrap_or("left as it is")
            )
        })
        .collect();
    let unregistered = paths("old_unregistered");
    if migrated.is_empty() && failed.is_empty() {
        // Nothing changed: one line, whatever else it has to mention.
        let mut line = if !current.is_empty() {
            let verb = if current.len() == 1 {
                "already runs"
            } else {
                "already run"
            };
            format!(
                "{} {verb} the native client; nothing changed",
                current.join(", ")
            )
        } else if left.is_empty() {
            "no Pseudolife registration found; nothing changed".to_owned()
        } else {
            "no registration moved; nothing changed".to_owned()
        };
        let mut notes = left;
        if !unregistered.is_empty() {
            notes.push(format!(
                "the old Python client is still on disk, unregistered: {}",
                unregistered.join(", ")
            ));
        }
        if !notes.is_empty() {
            line.push_str(&format!(" ({})", notes.join("; ")));
        }
        return line;
    }
    let mut lines = Vec::new();
    if !migrated.is_empty() {
        lines.push(format!(
            "moved {} to the native client at {} (each changed file has a private backup)",
            migrated.join(", "),
            report["launcher"].as_str().unwrap_or("")
        ));
    }
    for row in failed {
        lines.push(format!(
            "WARNING: {} was not moved: {}",
            row["client"].as_str().unwrap_or("?"),
            row["detail"].as_str().unwrap_or("failed")
        ));
    }
    lines.extend(left);
    if !unregistered.is_empty() {
        lines.push(format!(
            "the old Python client stays on disk, unregistered: {} (remove it with the tool that installed it, such as `pipx uninstall pseudolife-mcp`, or delete the directory, once you no longer need it)",
            unregistered.join(", ")
        ));
    }
    let in_use = paths("old_in_use");
    if !in_use.is_empty() {
        lines.push(format!(
            "the old Python client at {} stays installed: {}",
            in_use.join(", "),
            if report["old_unconfirmed"] == true {
                "the client configurations could not be reread to confirm nothing else runs it"
            } else {
                "a registration that was not moved still runs it"
            }
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
            "old_unregistered": ["/home/u/.local/bin/pseudolife-mcp", "/home/u/runtimes/000001"],
            "old_in_use": [], "old_unconfirmed": false,
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
            "left": [], "old_unregistered": ["/old"], "old_in_use": [], "old_unconfirmed": false,
        });
        assert_eq!(
            report_lines(&report),
            "codex already runs the native client; nothing changed (the old Python client is still on disk, unregistered: /old)"
        );
    }
}
