//! Native client update ladder; user approvals remain user-owned.
mod codex;
mod command;
mod plugin;
#[cfg(test)]
mod tests;

use crate::runtime::{self, registrations};
use command::{NativeRunner, Runner};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

pub use codex::{check_codex_hooks, codex_reapproval_text, runtime_hook_line, runtime_hook_status};
use plugin::update_plugin;

pub fn run_steps(
    steps: &[&str],
    repo: Option<&Path>,
    source: &str,
    daemon_digest: Option<&str>,
    reinstall: bool,
) -> Value {
    let mut report = json!({});
    if steps.contains(&"shim") {
        report["shim"] = runtime::update_shim(source, repo, reinstall);
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
    report["ok"] = json!(
        report
            .as_object()
            .unwrap()
            .values()
            .all(|r| r["state"] != "failed")
    );
    report
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
    let result = (|| -> Result<Option<Value>, String> {
        crate::credentials::reject_ancestor_redirects(&root)
            .map_err(|_| "private tunnel profiles could not be checked; run tunnel doctor")?;
        let entries = fs::read_dir(&root)
            .map_err(|_| "private tunnel profiles could not be checked; run tunnel doctor")?;
        let mut count = 0;
        for entry in entries {
            let entry = entry
                .map_err(|_| "private tunnel profiles could not be checked; run tunnel doctor")?;
            let name = entry.file_name();
            let name = name
                .to_str()
                .ok_or("private tunnel profile filename is invalid")?;
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
        let layout = runtime::default_layout()?;
        if !layout.launcher.is_file() || runtime::current_runtime(&layout).is_none() {
            return Ok(Some(
                json!({"state":"skipped","detail":"running tunnels were not refreshed: no installed native launcher; rerun native setup, then pseudolife-mcp tunnel update"}),
            ));
        }
        let output = runner.run(
            &layout.launcher,
            &["tunnel", "update"],
            None,
            Duration::from_secs(60 + 90 * count),
            &[],
        );
        if let Some(mut report) = output.json().filter(|r| {
            matches!(r["state"].as_str(), Some("current" | "failed")) && r["detail"].is_string()
        }) && (output.code == 0) == (report["state"] == "current")
        {
            report["detail"] = json!(format!(
                "{}; run by the installed native launcher",
                report["detail"].as_str().unwrap()
            ));
            return Ok(Some(report));
        }
        Ok(Some(
            json!({"state":"failed","detail":format!("installed tunnel update ended with exit {}; run pseudolife-mcp tunnel status, then tunnel update and tunnel doctor",output.code)}),
        ))
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
