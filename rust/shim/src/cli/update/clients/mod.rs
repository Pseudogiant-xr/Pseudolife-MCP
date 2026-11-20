//! Native client update ladder; user approvals remain user-owned.
mod codex;
mod command;
pub(crate) mod handoff;
mod plugin;
#[cfg(test)]
mod tests;

use crate::runtime::{self, registrations};
pub(crate) use command::{NativeRunner, Output, Runner, WindowlessRunner};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

pub use codex::{check_codex_hooks, codex_reapproval_text, runtime_hook_line, runtime_hook_status};
#[allow(
    unused_imports,
    reason = "the native installer (W3's installer pull request) calls it"
)]
pub use plugin::migrate_marketplace;
use plugin::update_plugin;

/// Sees the shim step's result; a returned report replaces the rest of the ladder.
pub type AfterShim<'a> = dyn FnMut(&Value) -> Option<Value> + 'a;

/// The client-side ladder. `after_shim` sees the shim step's result; when it
/// returns a report, that report is the ladder's and no later step runs here
/// (the release updater handed them to the runtime just installed).
pub fn run_steps(
    steps: &[&str],
    repo: Option<&Path>,
    source: &str,
    daemon_digest: Option<&str>,
    reinstall: bool,
    after_shim: Option<&mut AfterShim>,
) -> Value {
    let mut report = json!({});
    if steps.contains(&"shim") {
        report["shim"] = runtime::update_shim(source, repo, reinstall);
        if let Some(after_shim) = after_shim
            && let Some(taken) = after_shim(&report["shim"])
        {
            return taken;
        }
    }
    if steps.contains(&"plugin") {
        report["plugin"] = update_plugin(repo);
    }
    if steps.contains(&"codex") {
        report["codex"] = check_codex_hooks(repo, daemon_digest, true);
    }
    if repo.is_some() {
        report["autostart"] = check_autostart(repo);
    }
    if crate::board::state::exists(&runtime::home().join(".pseudolife-mcp/tunnel"))
        && let Some(tunnel) = update_tunnels(report.get("shim"), &NativeRunner)
    {
        report["tunnel"] = tunnel;
    }
    report["ok"] = json!(ladder_ok(&report));
    report
}

/// The ladder passes unless a step failed; `stale`, `skipped` and the
/// other states are reported, not failures.
fn ladder_ok(report: &Value) -> bool {
    report
        .as_object()
        .unwrap()
        .values()
        .all(|r| r["state"] != "failed")
}

fn check_autostart(_repo: Option<&Path>) -> Value {
    let mut notes = Vec::new();
    let mut registered = 0;
    for (kind, task, unit) in [
        (
            "claude",
            "Pseudolife Claude Shim",
            "pseudolife-sonnet-shim.service",
        ),
        (
            "codex",
            "Pseudolife Codex Shim",
            "pseudolife-codex-shim.service",
        ),
    ] {
        let (binary, args) = if cfg!(windows) {
            let root = std::env::var_os("SystemRoot")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
            (
                root.join("System32/schtasks.exe"),
                vec!["/Query", "/TN", task, "/XML"],
            )
        } else {
            let Some(binary) = registrations::absolute_command("systemctl") else {
                continue;
            };
            (binary, vec!["--user", "cat", unit])
        };
        let output = NativeRunner.run(&binary, &args, None, Duration::from_secs(30), &[]);
        if output.code != 0 {
            continue;
        } // Informational, as in the Python oracle.
        registered += 1;
        let text = registration_text(&output.bytes);
        if text
            .to_ascii_lowercase()
            .contains("<enabled>false</enabled>")
        {
            notes.push(format!("{kind}: the extractor autostart task is disabled; rerun native extractor setup if it should start at logon"));
        } else if text.contains("shim_autostart.py") {
            notes.push(format!("{kind}: extractor autostart still names the legacy Python runner; rerun native extractor setup to migrate it"));
        } else if text.contains("--model") || text.contains("-Model") {
            notes.push(format!("{kind}: extractor autostart carries model values on its command line; rerun native extractor setup to read current configuration at logon"));
        }
    }
    if !notes.is_empty() {
        return json!({"state":"stale","detail":notes.join("; "),"notes":notes});
    }
    result(
        if registered > 0 { "current" } else { "none" },
        if registered > 0 {
            "registered extractor autostart has no legacy runner or baked model override"
        } else {
            "no extractor autostart task or unit was found"
        },
    )
}
fn registration_text(bytes: &[u8]) -> String {
    // schtasks output may be UTF-16 even when the surrounding console uses a
    // legacy locale. File and command bytes never pass through that locale.
    let mut text = if bytes.starts_with(&[0xff, 0xfe]) {
        String::from_utf16_lossy(
            &bytes[2..]
                .chunks_exact(2)
                .map(|b| u16::from_le_bytes([b[0], b[1]]))
                .collect::<Vec<_>>(),
        )
    } else {
        String::from_utf8_lossy(bytes).into_owned()
    };
    let original = text.clone();
    for word in original.split_whitespace().collect::<Vec<_>>().windows(2) {
        if word[0].eq_ignore_ascii_case("-EncodedCommand")
            && let Some(bytes) = base64_bytes(word[1].split('<').next().unwrap_or(""))
        {
            text.push_str(&String::from_utf16_lossy(
                &bytes
                    .chunks_exact(2)
                    .map(|b| u16::from_le_bytes([b[0], b[1]]))
                    .collect::<Vec<_>>(),
            ));
        }
    }
    text
}
fn base64_bytes(text: &str) -> Option<Vec<u8>> {
    let mut bits = 0u32;
    let mut count = 0;
    let mut bytes = Vec::new();
    for byte in text.bytes() {
        if byte == b'=' {
            break;
        }
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        };
        bits = (bits << 6) | u32::from(value);
        count += 6;
        if count >= 8 {
            count -= 8;
            bytes.push((bits >> count) as u8);
        }
    }
    Some(bytes)
}

fn update_tunnels(shim: Option<&Value>, runner: &impl Runner) -> Option<Value> {
    let root = runtime::home().join(".pseudolife-mcp/tunnel");
    tunnel_step(
        &root,
        shim,
        || {
            let layout = runtime::default_layout()?;
            Ok(
                (layout.launcher.is_file() && runtime::current_runtime(&layout).is_some())
                    .then_some(layout.launcher),
            )
        },
        runner,
    )
}

/// The tunnel step over the store at `root`: `None` when it holds no process
/// record. `launcher` finds the installed native launcher, if any. The
/// launcher's `tunnel update` asks a compatible supervisor to reconnect on
/// the verified installed runtime. Older launches report `stale` with the
/// steps to reconnect. A failure carries the command's diagnosis.
fn tunnel_step(
    root: &Path,
    shim: Option<&Value>,
    launcher: impl FnOnce() -> Result<Option<PathBuf>, String>,
    runner: &impl Runner,
) -> Option<Value> {
    let result = (|| -> Result<Option<Value>, String> {
        let unreadable = || {
            format!(
                "private tunnel profiles in {} could not be checked; make it a directory only your account can access, then run pseudolife-mcp tunnel update",
                root.display()
            )
        };
        crate::credentials::reject_ancestor_redirects(root).map_err(|_| {
            format!(
                "{} is reached through a link or junction, or a folder above it cannot be inspected, so its private tunnel profiles are not read; make the path plain directories your account can read, then run pseudolife-mcp tunnel update",
                root.display()
            )
        })?;
        let entries = fs::read_dir(root).map_err(|_| unreadable())?;
        let mut count = 0;
        for entry in entries {
            let entry = entry.map_err(|_| unreadable())?;
            let name = entry.file_name();
            let name = name.to_str().ok_or_else(|| {
                format!(
                    "{} holds a file name that is not Unicode; move it out, then run pseudolife-mcp tunnel update",
                    root.display()
                )
            })?;
            if name.ends_with(".process.json") {
                count += 1;
            }
        }
        if count == 0 {
            return Ok(None);
        }
        if shim.is_some_and(|r| r["state"] == "failed") {
            return Ok(Some(
                json!({"state":"skipped","detail":"running tunnels stay on their current runtime because the shim step failed; after it passes run pseudolife-mcp tunnel update"}),
            ));
        }
        let Some(launcher) = launcher()? else {
            return Ok(Some(
                json!({"state":"skipped","detail":"cannot check running tunnels because the native launcher is missing. Rerun native setup, then run pseudolife-mcp tunnel update"}),
            ));
        };
        // The command reads the very directory this step scanned: its own
        // default home can differ (USERPROFILE against HOME under WSL).
        let directory = root.to_str().ok_or_else(|| {
            format!(
                "{} is not a Unicode path, so its tunnels cannot be checked; run pseudolife-mcp tunnel update --profile-dir <that directory> yourself",
                root.display()
            )
        })?;
        let timeout = Duration::from_secs(60 + 120 * count);
        let output = runner.run(
            &launcher,
            &["tunnel", "update", "--profile-dir", directory],
            None,
            timeout,
            &[],
        );
        // A refused request or successful rollback can leave the tunnel
        // available on the previous runtime. The report carries its cause.
        let answered = |state: &str| matches!(state, "current" | "stale" | "refreshed");
        if let Some(mut report) = output.json().filter(|r| {
            matches!(
                r["state"].as_str(),
                Some("current" | "stale" | "refreshed" | "failed")
            ) && r["detail"].is_string()
        }) && (output.code == 0) == answered(report["state"].as_str().unwrap_or(""))
        {
            report["detail"] = json!(format!(
                "{}; run by the installed native launcher",
                report["detail"].as_str().unwrap()
            ));
            return Ok(Some(report));
        }
        // A refusal is one fixed sentence on stderr (no key, token or file
        // contents), so it is the cause the user acts on.
        let text = output.text();
        let last = text.lines().map(str::trim).rfind(|line| !line.is_empty());
        let said = last.filter(|line| line.len() <= 1024);
        let left = format!("tunnels in {} were left as they are", root.display());
        let inspect = format!(
            "a supervisor may still finish a refresh it accepted. Check with `pseudolife-mcp tunnel status --profile NAME --profile-dir {}`",
            super::seams::quote(directory)
        );
        let launcher = launcher.display();
        let answer = output.json();
        let detail = match said {
            // A JSON answer whose exit code disagrees with its state.
            _ if answer.is_some() => format!(
                "installed tunnel update answered {} with exit {}, a pairing this step does not accept. {inspect}",
                answer
                    .as_ref()
                    .and_then(|a| a["state"].as_str())
                    .unwrap_or("an unknown state"),
                output.code
            ),
            // An older native runtime that does not run tunnel update yet.
            Some(said) if said.contains("is deferred in this candidate") => format!(
                "the installed launcher {launcher} is a client runtime that does not run tunnel update; {left}; install the current client with pseudolife-mcp update --clients-only, then run pseudolife-mcp tunnel update"
            ),
            // The refusal names its own next step ("..., then retry").
            Some(said) if said.ends_with("then retry") => format!(
                "installed tunnel update refused (exit {}): {said} with pseudolife-mcp tunnel update; {left}",
                output.code
            ),
            Some(said) if output.code == 2 => format!(
                "installed tunnel update ended with exit {}: {said}; {left}; after fixing that, run pseudolife-mcp tunnel update",
                output.code
            ),
            Some(said) => format!(
                "installed tunnel update ended with exit {}: {said}. {inspect}",
                output.code
            ),
            None if last.is_some() => format!(
                "installed tunnel update ended with exit {} and printed a line too long to show here. {inspect}",
                output.code
            ),
            // The runner's own codes: no output at all.
            None if output.code == 124 => format!(
                "the installed launcher {launcher} did not answer tunnel update within {} s. {inspect}",
                timeout.as_secs()
            ),
            // The runner reports 128+n when this update itself took signal
            // n while the tunnel step ran (a child's own signal reads as 1).
            None if cfg!(unix) && output.code > 128 => format!(
                "this update was interrupted by signal {} while the tunnel step ran. {inspect}",
                output.code - 128
            ),
            // The runner gives 1 for a spawn failure and for a child a
            // signal ended (no exit code); any other code is the child's.
            None if output.code == 1 => format!(
                "the installed launcher {launcher} could not be started, was stopped by a signal, or ended with exit 1 and printed nothing. {inspect}"
            ),
            None => format!(
                "the installed launcher {launcher} ended with exit {} and printed nothing. {inspect}",
                output.code
            ),
        };
        Ok(Some(json!({"state":"failed","detail":detail})))
    })();
    result.unwrap_or_else(|detail| Some(json!({"state":"failed","detail":detail})))
}

pub(crate) fn plugins_root() -> PathBuf {
    std::env::var_os("CLAUDE_CODE_PLUGIN_CACHE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| registrations::claude_config_dir().join("plugins"))
}
pub(crate) fn read_json(path: &Path) -> Result<Value, String> {
    registrations::read_optional(path)?
        .map(|b| registrations::json_bytes(&b))
        .transpose()
        .map(|v| v.unwrap_or_else(|| json!({})))
}
pub(crate) fn result(state: &str, detail: impl Into<String>) -> Value {
    json!({"state":state,"detail":detail.into()})
}
