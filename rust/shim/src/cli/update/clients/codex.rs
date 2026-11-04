use super::*;
use plugin::{MARKETPLACE, PLUGIN, normalized_file};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub(super) const SCRIPTS: &[&str] = &[
    "lifecycle.ps1",
    "session-start.sh",
    "user-prompt-submit.sh",
    "coordination-start.sh",
    "coordination-prompt.sh",
    "session-end.sh",
    "stop-wake.sh",
    "subagent-board-guard.sh",
    "subagent-board.sh",
];
const MANUAL: &[&str] = &[
    "lifecycle.ps1",
    "session-start.sh",
    "user-prompt-submit.sh",
    "coordination-start.sh",
    "coordination-prompt.sh",
    "session-end.sh",
];
pub(super) fn digest(directory: &Path, names: &[&str]) -> Option<String> {
    let mut digest = Sha256::new();
    for name in names {
        digest.update(name.as_bytes());
        digest.update([0]);
        digest.update(normalized_file(&directory.join(name)).ok()?);
        digest.update([0]);
    }
    Some(format!("{:x}", digest.finalize()))
}

/// Typed, duplicate-free, conservative consent comparison. This is deliberately
/// not Codex's trust hash; it never writes a trusted_hash or enables a handler.
pub(super) fn manifest(bytes: &[u8]) -> Option<Value> {
    fn normalize_numbers(value: &mut Value) -> Option<()> {
        match value {
            Value::Number(number) => {
                let token = number.to_string();
                let float = token.parse::<f64>().ok()?;
                if !float.is_finite() {
                    return None;
                }
                if token == "-0" || token.contains(['.', 'e', 'E']) {
                    *number = serde_json::Number::from_f64(float)?;
                }
            }
            Value::Array(items) => {
                for item in items {
                    normalize_numbers(item)?;
                }
            }
            Value::Object(items) => {
                for item in items.values_mut() {
                    normalize_numbers(item)?;
                }
            }
            _ => {}
        }
        Some(())
    }
    // The shared scanner detects escaped duplicate keys. serde's arbitrary
    // precision path collapses the integer token -0 to i64 zero, whereas the
    // consent oracle explicitly treats it as a float (and rejects it in u64
    // fields). Preserve that one numeric spelling outside JSON strings first.
    crate::cli::pyjson::loads_unique(std::str::from_utf8(bytes).ok()?, 127).ok()?;
    let mut value: Value = serde_json::from_slice(&preserve_negative_zero(bytes)).ok()?;
    normalize_numbers(&mut value)?;
    let top = value.as_object_mut()?;
    if top.keys().any(|k| k != "description" && k != "hooks")
        || top
            .get("description")
            .is_some_and(|v| !v.is_null() && !v.is_string())
    {
        return None;
    }
    for (event, groups) in top.get_mut("hooks")?.as_object_mut()? {
        for group in groups.as_array_mut()? {
            if group
                .get("matcher")
                .is_some_and(|v| !v.is_null() && !v.is_string())
            {
                return None;
            }
            for handler in group.get_mut("hooks")?.as_array_mut()? {
                let handler = handler.as_object_mut()?;
                if handler.get("type")?.as_str() != Some("command")
                    || handler.get("command")?.as_str()?.trim().is_empty()
                {
                    return None;
                }
                for field in ["commandWindows", "command_windows", "statusMessage"] {
                    if handler
                        .get(field)
                        .is_some_and(|v| !v.is_null() && !v.is_string())
                    {
                        return None;
                    }
                }
                if handler.contains_key("commandWindows") && handler.contains_key("command_windows")
                {
                    return None;
                }
                if handler.get("async").is_some_and(|v| !v.is_boolean()) {
                    return None;
                }
                for field in ["timeout", "additionalContextLimit"] {
                    if handler.get(field).is_some_and(|v| {
                        !v.is_null() && (v.as_u64().is_none() || v.to_string().starts_with('-'))
                    }) {
                        return None;
                    }
                }
                if event == "SessionEnd" || event == "Interrupt" {
                    let timeout = handler
                        .get("timeout")
                        .and_then(Value::as_u64)
                        .unwrap_or(1)
                        .clamp(1, 3);
                    handler.insert("timeout".into(), json!(timeout));
                }
            }
        }
    }
    Some(value)
}

fn preserve_negative_zero(bytes: &[u8]) -> Vec<u8> {
    let mut result = Vec::with_capacity(bytes.len());
    let mut quoted = false;
    let mut escaped = false;
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if quoted {
            result.push(byte);
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                quoted = false;
            }
            index += 1;
            continue;
        }
        if byte == b'"' {
            quoted = true;
        }
        if bytes[index..].starts_with(b"-0")
            && bytes
                .get(index + 2)
                .is_none_or(|b| matches!(*b, b',' | b'}' | b']' | b' ' | b'\t' | b'\r' | b'\n'))
        {
            result.extend_from_slice(b"-0.0");
            index += 2;
        } else {
            result.push(byte);
            index += 1;
        }
    }
    result
}
fn definitions_match(a: &Path, b: &Path) -> bool {
    normalized_file(&a.join("hooks.json"))
        .ok()
        .and_then(|v| manifest(&v))
        .is_some_and(|left| {
            normalized_file(&b.join("hooks.json"))
                .ok()
                .and_then(|v| manifest(&v))
                == Some(left)
        })
}
fn changed_files(a: &Path, b: &Path) -> Vec<String> {
    SCRIPTS
        .iter()
        .copied()
        .chain(["hooks.json"])
        .filter(|name| normalized_file(&a.join(name)).ok() != normalized_file(&b.join(name)).ok())
        .map(str::to_owned)
        .collect()
}
fn plugin_hooks(home: &Path) -> PathBuf {
    let installed = home
        .join("plugins/cache")
        .join(MARKETPLACE)
        .join("pseudolife-memory/local/hooks");
    if installed.is_dir() {
        installed
    } else {
        home.join(".tmp/marketplaces")
            .join(MARKETPLACE)
            .join("plugin/hooks")
    }
}
fn reference(repo: Option<&Path>, daemon_digest: Option<&str>) -> Option<PathBuf> {
    if let Some(repo) = repo {
        return Some(repo.join("plugin/hooks"));
    }
    let record = plugin::plugin_record(&plugins_root()).ok()??;
    let path = PathBuf::from(record["installPath"].as_str()?).join("hooks");
    (digest(&path, SCRIPTS).as_deref() == daemon_digest && daemon_digest.is_some()).then_some(path)
}
fn handler_keys(manifest: &Value) -> Option<Vec<String>> {
    let mut keys = Vec::new();
    for (event, groups) in manifest.get("hooks")?.as_object()? {
        let event = event
            .chars()
            .enumerate()
            .fold(String::new(), |mut s, (i, c)| {
                if i > 0 && c.is_ascii_uppercase() {
                    s.push('_');
                }
                s.extend(c.to_lowercase());
                s
            });
        for (g, group) in groups.as_array()?.iter().enumerate() {
            for (h, _) in group["hooks"].as_array()?.iter().enumerate() {
                let key = format!("{PLUGIN}:hooks/hooks.json:{event}:{g}:{h}");
                keys.push(key);
            }
        }
    }
    Some(keys)
}

fn unapproved(manifest: &Value, config: &toml::Value) -> Option<usize> {
    Some(
        handler_keys(manifest)?
            .into_iter()
            .filter(|key| {
                let state = config
                    .get("hooks")
                    .and_then(|v| v.get("state"))
                    .and_then(|v| v.get(key));
                !state.is_some_and(|v| {
                    v.get("trusted_hash")
                        .and_then(toml::Value::as_str)
                        .is_some_and(|s| !s.is_empty())
                        || v.get("enabled").and_then(toml::Value::as_bool) == Some(false)
                })
            })
            .count(),
    )
}

fn runtime_status(result: &Value, expected: &[String]) -> &'static str {
    let Some(entries) = result["data"].as_array() else {
        return "unknown";
    };
    if entries.len() != 1
        || entries[0]["errors"]
            .as_array()
            .is_none_or(|v| !v.is_empty())
    {
        return "unknown";
    }
    let Some(hooks) = entries[0]["hooks"].as_array() else {
        return "unknown";
    };
    let mut own = BTreeMap::new();
    let prefix = format!("{PLUGIN}:");
    for hook in hooks {
        let Some(key) = hook["key"].as_str().filter(|key| !key.is_empty()) else {
            return "unknown";
        };
        if !key.starts_with(&prefix) {
            continue;
        }
        if !expected.iter().any(|expected| expected == key) || own.insert(key, hook).is_some() {
            return "unknown";
        }
    }
    if expected.is_empty() || own.len() != expected.len() {
        return "not-loaded";
    }
    if own.values().any(|h| {
        h["enabled"] == true && matches!(h["trustStatus"].as_str(), Some("modified" | "untrusted"))
    }) {
        return "needs-approval";
    }
    if own.values().any(|h| h["enabled"] == false) {
        return "disabled";
    }
    if own
        .values()
        .all(|h| h["enabled"] == true && h["trustStatus"] == "trusted")
    {
        "current"
    } else {
        "unknown"
    }
}

pub async fn runtime_hook_status() -> &'static str {
    use crate::setup::codex::{Client, Rpc};
    let home = registrations::codex_home();
    let Some(executable) = codex_executable(&home) else {
        return "unknown";
    };
    let Some(expected) = normalized_file(&plugin_hooks(&home).join("hooks.json"))
        .ok()
        .and_then(|v| manifest(&v))
        .and_then(|v| handler_keys(&v))
    else {
        return "unknown";
    };
    let Ok(cwd) = std::env::current_dir() else {
        return "unknown";
    };
    let Ok(mut client) = Client::open(&executable, &home, &cwd).await else {
        return "unknown";
    };
    let result = client.rpc("hooks/list", json!({"cwds":[cwd]})).await;
    client.close().await;
    result
        .map(|v| runtime_status(&v, &expected))
        .unwrap_or("unknown")
}

#[cfg(test)]
mod runtime_trust_tests {
    use super::*;
    #[test]
    fn runtime_verdict_distinguishes_trust_disabled_and_foreign_handlers() {
        let key = format!("{PLUGIN}:hooks/hooks.json:session_start:0:0");
        let expected = vec![key.clone()];
        for (trust, enabled, answer) in [
            ("modified", true, "needs-approval"),
            ("untrusted", true, "needs-approval"),
            ("trusted", true, "current"),
            ("trusted", false, "disabled"),
            ("future-state", true, "unknown"),
        ] {
            let value = json!({"data":[{"errors":[],"hooks":[{"key":key,"enabled":enabled,"trustStatus":trust},
                {"key":"other-plugin:session_start:0:0","enabled":true,"trustStatus":"modified"}]}]});
            assert_eq!(runtime_status(&value, &expected), answer);
        }
        for added in [
            key.clone(),
            format!("{PLUGIN}:hooks/hooks.json:session_start:99:0"),
        ] {
            let value = json!({"data":[{"errors":[],"hooks":[
                {"key":key,"enabled":true,"trustStatus":"trusted"},
                {"key":added,"enabled":true,"trustStatus":"modified"}]}]});
            assert_eq!(runtime_status(&value, &expected), "unknown");
        }
        assert_eq!(
            runtime_status(&json!({"data":[{"errors":[],"hooks":[]}]}), &expected),
            "not-loaded"
        );
        assert_eq!(
            runtime_status(
                &json!({"data":[{"errors":["fixture"],"hooks":[]}]}),
                &expected
            ),
            "unknown"
        );
    }
}

pub fn check_codex_hooks(repo: Option<&Path>, daemon_digest: Option<&str>, refresh: bool) -> Value {
    check_with(
        &registrations::codex_home(),
        repo,
        daemon_digest,
        refresh,
        &NativeRunner,
    )
}
pub(super) fn check_with(
    home: &Path,
    repo: Option<&Path>,
    daemon_digest: Option<&str>,
    refresh: bool,
    runner: &impl Runner,
) -> Value {
    let outcome = (|| -> Result<Value, String> {
        let manual = home.join("pseudolife/hooks");
        if manual.is_dir() {
            return manual_hooks(home, repo, refresh);
        }
        let bytes = registrations::read_optional(&home.join("config.toml"))?;
        let Some(bytes) = bytes else {
            return Ok(result(
                "not-configured",
                "no Codex hook copies or plugin under the Codex home",
            ));
        };
        let text = std::str::from_utf8(&bytes)
            .map_err(|_| "Codex config is not UTF-8; hook approval is unknown")?;
        let config: toml::Value = toml::from_str(text)
            .map_err(|_| "Codex config is not valid TOML; hook approval is unknown")?;
        if config.get("plugins").and_then(|v| v.get(PLUGIN)).is_none()
            && !text.contains(&format!("\"{PLUGIN}\""))
        {
            return Ok(result(
                "not-configured",
                "no Codex hook copies or plugin under the Codex home",
            ));
        }
        let installed = plugin_hooks(home);
        let reference = reference(repo, daemon_digest);
        let current = repo
            .and_then(|r| digest(&r.join("plugin/hooks"), SCRIPTS))
            .or_else(|| daemon_digest.map(str::to_owned));
        let Some(current) = current else {
            return Ok(result(
                "unknown",
                "Codex runs the plugin's hooks; no checkout or daemon hooks_digest to compare",
            ));
        };
        if !installed.is_dir() {
            return Ok(
                json!({"state":"plugin-managed","source":"plugin","changed_files":null,"detail":"Codex runs the plugin's hooks; refresh its missing copy through Codex's plugin manager, then inspect /hooks"}),
            );
        }
        let changed = reference.as_ref().map(|r| changed_files(&installed, r));
        let definition = normalized_file(&installed.join("hooks.json"))
            .ok()
            .and_then(|v| manifest(&v));
        let definitions = reference
            .as_ref()
            .is_none_or(|r| definitions_match(&installed, r));
        if digest(&installed, SCRIPTS).as_deref() == Some(&current) && definitions {
            if refresh && changed.as_ref().is_some_and(|v| !v.is_empty()) {
                return Ok(upgrade(home, repo, daemon_digest, changed.unwrap(), runner));
            }
            let Some(definition) = definition else {
                return Ok(
                    json!({"state":"unknown","source":"plugin","changed_files":[],"detail":"Codex hooks.json could not be verified; handler approval is unknown"}),
                );
            };
            if unapproved(&definition, &config).is_some_and(|n| n > 0) {
                return Ok(
                    json!({"state":"needs-approval","source":"plugin","changed_files":[],"detail":"Codex skips plugin hook handlers without approval; open /hooks in a Codex session and approve the PseudoLife handlers"}),
                );
            }
            return Ok(result(
                "current",
                "Codex's plugin hook copy matches the current scripts",
            ));
        }
        if definitions && changed.as_ref().is_some_and(|v| !v.is_empty()) {
            if refresh {
                return Ok(upgrade(home, repo, daemon_digest, changed.unwrap(), runner));
            }
            return Ok(
                json!({"state":"behind","source":"plugin","changed_files":changed,"detail":"Codex's plugin scripts are behind; the installed approved definitions are equivalent"}),
            );
        }
        Ok(
            json!({"state":"stale","source":"plugin","changed_files":changed,"detail":"Codex's hook definition changed or could not be verified; update through Codex's plugin manager and review /hooks before approving"}),
        )
    })();
    outcome.unwrap_or_else(|detail| result("failed", detail))
}

fn codex_executable(home: &Path) -> Option<PathBuf> {
    let explicit = std::env::var("PSEUDOLIFE_CODEX_BIN")
        .ok()
        .is_some_and(|v| !v.trim().is_empty());
    if let Some(path) = crate::board::doorbell::resolve_command(|k| std::env::var(k).ok()) {
        return Some(path);
    }
    if explicit {
        return None;
    }
    let path = home
        .join("packages/standalone/current/bin")
        .join(if cfg!(windows) { "codex.exe" } else { "codex" });
    path.is_file().then_some(path)
}
fn offered_manifest(home: &Path, runner: &impl Runner) -> Result<Value, String> {
    let git = registrations::absolute_command("git")
        .ok_or("git was not found on an absolute PATH entry")?;
    let config = registrations::read_optional(&home.join("config.toml"))?
        .ok_or("Codex configuration is missing")?;
    let config: toml::Value =
        toml::from_str(std::str::from_utf8(&config).map_err(|_| "Codex config is not UTF-8")?)
            .map_err(|_| "Codex config is not valid TOML")?;
    let table = config
        .get("marketplaces")
        .and_then(|v| v.get(MARKETPLACE))
        .map(serde_json::to_value)
        .transpose()
        .map_err(|_| "invalid marketplace table")?;
    let table = if table
        .as_ref()
        .and_then(|v| v.get("source"))
        .and_then(Value::as_str)
        .is_some()
    {
        table.unwrap()
    } else {
        read_json(
            &home
                .join(".tmp/marketplaces")
                .join(MARKETPLACE)
                .join(".codex-marketplace-install.json"),
        )?
    };
    if table
        .get("source_type")
        .and_then(Value::as_str)
        .unwrap_or("git")
        != "git"
    {
        return Err("marketplace is not a git source".into());
    }
    let source = table["source"]
        .as_str()
        .ok_or("marketplace names no git source")?;
    let reference = table
        .get("ref")
        .or_else(|| table.get("ref_name"))
        .and_then(Value::as_str);
    let scratch =
        std::env::temp_dir().join(format!("pseudolife-native-codex-{}", uuid::Uuid::new_v4()));
    crate::board::state::private_dir(&scratch).map_err(str::to_owned)?;
    let outcome = (|| {
        let clone = scratch.join("m");
        let clone_text = clone.to_str().ok_or("temporary directory is not Unicode")?;
        let mut args = vec![
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--filter=blob:none",
            "--no-checkout",
        ];
        if let Some(reference) = reference {
            args.extend(["--branch", reference]);
        }
        args.extend(["--", source, clone_text]);
        let ssh_default = if std::env::var_os("GIT_SSH_COMMAND").is_none() {
            vec![("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")]
        } else {
            vec![]
        };
        if runner
            .run(&git, &args, None, Duration::from_secs(120), &ssh_default)
            .code
            != 0
        {
            return Err("could not fetch marketplace definition; existing copy retained".into());
        }
        let output = runner.run(
            &git,
            &["-C", clone_text, "show", "HEAD:plugin/hooks/hooks.json"],
            None,
            Duration::from_secs(120),
            &[],
        );
        if output.code != 0 {
            return Err("could not read marketplace definition; existing copy retained".into());
        }
        manifest(&output.bytes).ok_or_else(|| {
            "marketplace definition is invalid or unsupported; existing copy retained".into()
        })
    })();
    let _ = fs::remove_dir_all(&scratch);
    outcome
}
fn upgrade(
    home: &Path,
    repo: Option<&Path>,
    daemon_digest: Option<&str>,
    changed: Vec<String>,
    runner: &impl Runner,
) -> Value {
    let stays = |detail: &str| json!({"state":"behind","source":"plugin","changed_files":changed,"detail":detail});
    let Some(codex) = codex_executable(home) else {
        return stays(
            "Codex CLI was not found or its configured path is invalid; installed hooks retained; update in Codex's plugin manager",
        );
    };
    let offered = match offered_manifest(home, runner) {
        Ok(value) => value,
        Err(detail) => return stays(&detail),
    };
    let installed = normalized_file(&plugin_hooks(home).join("hooks.json"))
        .ok()
        .and_then(|v| manifest(&v));
    if installed.as_ref() != Some(&offered) {
        return stays(
            "the marketplace changes the approved hook definition; automatic refresh skipped; update in Codex's plugin manager, then review and approve /hooks",
        );
    }
    let output = runner.run(
        &codex,
        &["plugin", "marketplace", "upgrade", MARKETPLACE, "--json"],
        None,
        Duration::from_secs(300),
        &[("CODEX_HOME", home.to_str().unwrap_or(""))],
    );
    let report = output.json();
    if output.code != 0
        || report.as_ref().is_none_or(|r| {
            r.get("errors")
                .and_then(Value::as_array)
                .is_some_and(|e| !e.is_empty())
        })
    {
        return stays(
            "Codex marketplace upgrade did not verify; installed hooks were not marked current; rerun codex plugin marketplace upgrade pseudolife-mcp",
        );
    }
    let after = check_with(home, repo, daemon_digest, false, runner);
    if after["state"] == "current"
        && report
            .as_ref()
            .and_then(|r| r.get("upgradedRoots"))
            .and_then(Value::as_array)
            .is_some_and(|v| !v.is_empty())
    {
        return json!({"state":"refreshed","source":"plugin","changed_files":changed,"detail":"Codex's plugin copy refreshed through its marketplace manager; normalized hook definitions stayed equivalent, so approvals carry over"});
    }
    if after["state"] == "current" || after["state"] == "behind" {
        return stays(
            "Codex's marketplace copy still differs from the target scripts or had nothing newer; existing approvals were preserved",
        );
    }
    after
}

fn shell_quote(path: &str) -> String {
    if !path.is_empty()
        && path
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
    {
        path.into()
    } else {
        format!("'{}'", path.replace('\'', "'\"'\"'"))
    }
}
fn manual_hooks(home: &Path, repo: Option<&Path>, refresh: bool) -> Result<Value, String> {
    let root = home.join("pseudolife/hooks");
    let Some(repo) = repo else {
        return Ok(result(
            "bundle-present",
            "manual hook copies present; current scripts need a checkout; rerun native client setup and inspect Codex /hooks",
        ));
    };
    let definitions = read_json(&home.join("hooks.json"))?;
    let handlers: Vec<&Value> = definitions
        .get("hooks")
        .and_then(Value::as_object)
        .into_iter()
        .flat_map(|m| m.values())
        .filter_map(Value::as_array)
        .flatten()
        .filter_map(|g| g.get("hooks"))
        .filter_map(Value::as_array)
        .flatten()
        .collect();
    let base = format!(
        "env PSEUDOLIFE_CODEX_HOOK=1 bash {}",
        shell_quote(&root.join("run.sh").to_string_lossy())
    );
    let scripts = [
        "session-start.sh",
        "session-start.sh memory-policy",
        "user-prompt-submit.sh",
        "coordination-start.sh",
        "coordination-prompt.sh",
        "session-end.sh",
    ];
    if !scripts.iter().all(|s| {
        handlers
            .iter()
            .any(|h| h["command"].as_str() == Some(&format!("{base} {s}")))
    }) {
        return Ok(
            json!({"state":"stale","source":"manual","detail":"manual hooks still name a script bundle or a custom command; rerun native setup and review Codex /hooks to move them to a stable launcher"}),
        );
    }
    // These are the exact launchers shipped by the Python oracle, checked
    // without loading or executing its setup module. Modified launchers fail
    // closed; refreshing scripts does not silently approve modified commands.
    for (name, expected) in [
        (
            "run.sh",
            "23dc357bbddc1b4687f9ddfe54f8591b03fbc6e1b19bd41c3aeb2834b0f0a1ce",
        ),
        (
            "run.ps1",
            "0ff3ff20c091588aa5b8bf2d2f6859c0ade5a922ba83f9ac8b824415f09d2bc7",
        ),
    ] {
        if runtime::hash_file(&root.join(name)).as_deref() != Ok(expected) {
            return Err("a manual hook launcher is missing or modified; rerun native setup with consent to repair it".into());
        }
    }
    let pointer = fs::read_to_string(root.join("current"))
        .map_err(|_| "manual hook bundle pointer is missing")?;
    let previous = pointer.trim();
    if previous.len() != 20
        || !previous
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || digest(&root.join(previous), MANUAL).is_none_or(|d| !d.starts_with(previous))
    {
        return Err("manual hook bundle was modified or its pointer is invalid; review it before native setup".into());
    }
    let scripts = repo.join("plugin/hooks");
    let target = digest(&scripts, MANUAL)
        .ok_or("checkout manual hook scripts could not be read")?[..20]
        .to_owned();
    if target == previous {
        return Ok(
            json!({"state":"current","source":"manual","detail":"manual hooks run the checkout's verified scripts through the stable launcher"}),
        );
    }
    if !refresh {
        return Ok(
            json!({"state":"behind","source":"manual","detail":"manual hook scripts are behind; update refreshes the bundle without changing approved commands"}),
        );
    }
    let directory = root.join(&target);
    crate::board::state::private_dir(&directory).map_err(str::to_owned)?;
    let mut files = BTreeMap::new();
    for name in MANUAL {
        files.insert(*name, normalized_file(&scripts.join(name))?);
    }
    for (name, bytes) in files {
        let path = directory.join(name);
        if path.exists() {
            if fs::read(&path).map_err(|_| "manual bundle file could not be read")? != bytes {
                return Err("existing manual bundle was modified; pointer unchanged".into());
            }
        } else {
            crate::board::state::atomic_create(&path, &bytes).map_err(str::to_owned)?;
        }
    }
    if digest(&directory, MANUAL).is_none_or(|d| !d.starts_with(&target)) {
        return Err("new manual bundle did not verify; pointer unchanged".into());
    }
    registrations::write_config(
        &root.join("current"),
        pointer.as_bytes(),
        format!("{target}\n").as_bytes(),
    )?;
    Ok(
        json!({"state":"current","source":"manual","detail":"manual hook scripts refreshed through the stable launcher; approved commands and old bundles retained"}),
    )
}

pub fn codex_reapproval_text(report: Option<&Value>) -> String {
    let Some(report) = report else {
        return String::new();
    };
    if !matches!(report["state"].as_str(), Some("stale" | "needs-approval")) {
        return String::new();
    }
    if report["source"] == "manual" {
        return "Codex: its manual hook commands need approval to move to a stable launcher.\n  1. Rerun native client setup for Codex and review the requested hook changes.\n  2. In Codex run /hooks and approve the PseudoLife handlers.\n  3. Verify: pseudolife-mcp doctor reports the manual hook bundle present.".into();
    }
    let changed = report["changed_files"]
        .as_array()
        .map(|v| {
            v.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "new or changed hook handlers".into());
    format!(
        "Codex: its hook copy needs approval ({changed}); until then unapproved memory and coordination handlers do not run.\n  1. Update the plugin in Codex's plugin manager, or run codex plugin marketplace upgrade pseudolife-mcp.\n  2. In Codex run /hooks and approve the PseudoLife handlers.\n  3. Verify: pseudolife-mcp doctor reports codex_hooks = current."
    )
}
