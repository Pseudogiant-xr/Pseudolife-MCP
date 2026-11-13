//! The hook definitions Codex approves, spelled exactly as the frozen
//! Python setup (`ops/setup-codex-hooks.py`) spells them: Codex approves a
//! hook by its definition (command, commandWindows, timeout, async,
//! statusMessage, matcher) and its key (source file, event, group and
//! handler index), so one changed character asks every user to approve
//! again. `codex_hooks/testdata/definitions-<os>.json` (recorded from the Python by
//! `tests/test_codex_hook_native_golden.py`) pins them.
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

pub const PLUGIN_ID: &str = "pseudolife-memory@pseudolife-mcp";

/// Codex's event names and the hooks.json section each lives in.
pub const EVENTS: [(&str, &str); 3] = [
    ("sessionStart", "SessionStart"),
    ("userPromptSubmit", "UserPromptSubmit"),
    ("sessionEnd", "SessionEnd"),
];

/// The manual roles per Codex event, in the order setup appends them.
pub fn manual_roles(event: &str) -> &'static [&'static str] {
    match event {
        "sessionStart" => &["SessionStart", "MemoryPolicy", "CoordinationStart"],
        "userPromptSubmit" => &["UserPromptSubmit", "CoordinationPrompt"],
        "sessionEnd" => &["SessionEnd"],
        _ => &[],
    }
}

/// Every plugin event Codex may list, by its Codex name.
pub fn plugin_event(event: &str) -> Option<&'static str> {
    Some(match event {
        "sessionStart" => "SessionStart",
        "userPromptSubmit" => "UserPromptSubmit",
        "sessionEnd" => "SessionEnd",
        "stop" => "Stop",
        "stopFailure" => "StopFailure",
        "subagentStart" => "SubagentStart",
        "subagentStop" => "SubagentStop",
        "preToolUse" => "PreToolUse",
        _ => return None,
    })
}

/// Plugin events a Codex may list none or some of, with how many handlers
/// each has in the shipped hooks.json.
pub const OPTIONAL_PLUGIN_EVENTS: [(&str, usize); 5] = [
    ("stop", 1),
    ("stopFailure", 1),
    ("subagentStart", 1),
    ("subagentStop", 2),
    ("preToolUse", 1),
];

pub fn optional_most(event: &str) -> Option<usize> {
    OPTIONAL_PLUGIN_EVENTS
        .iter()
        .find(|(name, _)| *name == event)
        .map(|(_, most)| *most)
}

/// The scripts a manual bundle holds, in digest order.
pub const SCRIPTS: [&str; 6] = [
    "lifecycle.ps1",
    "session-start.sh",
    "user-prompt-submit.sh",
    "coordination-start.sh",
    "coordination-prompt.sh",
    "session-end.sh",
];
/// The bundle before the coordination hooks split off (2026-09).
pub const LEGACY_SCRIPTS: [&str; 4] = [
    "lifecycle.ps1",
    "session-start.sh",
    "user-prompt-submit.sh",
    "session-end.sh",
];
pub const PLUGIN_SCRIPTS: [&str; 9] = [
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

pub const POINTER: &str = "current";
pub const LAUNCHED: &str = "launched";

/// The launchers, byte for byte what every release since 2026-09-30
/// shipped: Codex approves the command naming them, and both the frozen
/// Python setup and the native updater refuse any other bytes (their
/// sha256 is pinned in `tests/test_codex_hook_launcher.py`). Their
/// "rerun: python ops/setup-codex-hooks.py" text is therefore kept.
pub const RUN_SH: &[u8] = b"#!/usr/bin/env bash
# PseudoLife's Codex hooks: runs the script named by $1 from the bundle that
# ./current names. Codex approves the command naming this file, not the
# scripts it runs, so a new bundle needs no new approval. Written and
# verified byte for byte by ops/setup-codex-hooks.py.
here=$(dirname \"$0\")
fail() { printf 'PseudoLife hooks: %s; rerun: python ops/setup-codex-hooks.py\\n' \"$1\" >&2; exit 1; }
bundle=$(head -c 64 \"$here/current\" 2>/dev/null | tr -d '\\r\\n ')
case \"$bundle\" in *[!0-9a-f]*|'') fail \"$here/current names no script bundle\" ;; esac
[ \"${#bundle}\" -eq 20 ] || fail \"$here/current names no script bundle\"
case \"${1:-}\" in
    session-start.sh|user-prompt-submit.sh|coordination-start.sh|coordination-prompt.sh|session-end.sh) ;;
    *) fail \"no hook script named ${1:-(none)}\" ;;
esac
script=$1
shift
exec bash \"$here/$bundle/$script\" \"$@\"
";
pub const RUN_PS1: &[u8] = b"# PseudoLife's Codex hooks: runs lifecycle.ps1 from the bundle that
# .\\current names. Codex approves the command naming this file, not the
# scripts it runs, so a new bundle needs no new approval. Written and
# verified byte for byte by ops/setup-codex-hooks.py.
param([string]$Event)
$bundle = ''
try { $bundle = ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'current'))).Trim() } catch {}
if ($bundle -cnotmatch '^[0-9a-f]{20}$') {
    [Console]::Error.WriteLine(\"PseudoLife hooks: $PSScriptRoot\\current names no script bundle; rerun: python ops/setup-codex-hooks.py\")
    exit 1
}
$global:LASTEXITCODE = 0
& (Join-Path (Join-Path $PSScriptRoot $bundle) 'lifecycle.ps1') -Event $Event
exit $LASTEXITCODE
";
pub const LAUNCHERS: [(&str, &[u8]); 2] = [("run.sh", RUN_SH), ("run.ps1", RUN_PS1)];
/// The sha256 of every launcher an earlier release shipped, by name (none
/// yet: the launchers never changed since they shipped).
pub const PREVIOUS_LAUNCHERS: [(&str, &[&str]); 2] = [("run.sh", &[]), ("run.ps1", &[])];

pub fn launcher_shipped(name: &str, data: &[u8]) -> bool {
    LAUNCHERS
        .iter()
        .any(|(known, bytes)| *known == name && *bytes == data)
        || PREVIOUS_LAUNCHERS.iter().any(|(known, hashes)| {
            *known == name && hashes.contains(&format!("{:x}", Sha256::digest(data)).as_str())
        })
}

pub fn hooks_root(home: &Path) -> PathBuf {
    home.join("pseudolife").join("hooks")
}

/// Python's `shlex.quote`.
pub fn shell_quote(text: &str) -> String {
    if !text.is_empty()
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
    {
        return text.to_owned();
    }
    format!("'{}'", text.replace('\'', "'\"'\"'"))
}

fn text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

/// Role -> (script, timeout), in the order setup lists them.
const MANUAL: [(&str, &str, u64); 6] = [
    ("SessionStart", "session-start.sh", 15),
    ("MemoryPolicy", "session-start.sh", 15),
    ("UserPromptSubmit", "user-prompt-submit.sh", 5),
    ("CoordinationStart", "coordination-start.sh", 5),
    ("CoordinationPrompt", "coordination-prompt.sh", 5),
    ("SessionEnd", "session-end.sh", 3),
];

/// One definition per role. `bash_target(script)` is the bash command's
/// script and arguments; literal single quotes protect `$`, backticks and
/// spaces in the PowerShell path.
fn definitions(
    bash_target: impl Fn(&str) -> String,
    ps_path: &Path,
    codex_marker: bool,
) -> Map<String, Value> {
    let ps = text(ps_path).replace('\'', "''");
    let prefix = if codex_marker {
        "env PSEUDOLIFE_CODEX_HOOK=1 bash "
    } else {
        "bash "
    };
    MANUAL
        .iter()
        .map(|(role, script, timeout)| {
            let argument = if *role == "MemoryPolicy" {
                " memory-policy"
            } else {
                ""
            };
            let mut handler = Map::new();
            handler.insert("type".into(), json!("command"));
            handler.insert(
                "command".into(),
                json!(format!("{prefix}{}{argument}", bash_target(script))),
            );
            handler.insert(
                "commandWindows".into(),
                json!(format!("pwsh -NoProfile -File '{ps}' -Event {role}")),
            );
            handler.insert("timeout".into(), json!(timeout));
            ((*role).to_owned(), Value::Object(handler))
        })
        .collect()
}

/// The definitions naming one bundle directory directly: what setup wrote
/// before the launcher, recognized so that setup can replace them.
pub fn manual_definitions(directory: &Path, codex_marker: bool) -> Map<String, Value> {
    definitions(
        |script| shell_quote(&text(&directory.join(script))),
        &directory.join("lifecycle.ps1"),
        codex_marker,
    )
}

/// The manual hooks' definitions through the launchers. They name no
/// bundle, so a refresh leaves them, and Codex's approval, as they are.
pub fn launcher_definitions(home: &Path) -> Map<String, Value> {
    let root = hooks_root(home);
    definitions(
        |script| format!("{} {script}", shell_quote(&text(&root.join("run.sh")))),
        &root.join("run.ps1"),
        true,
    )
}

pub fn command_of<'a>(definitions: &'a Map<String, Value>, role: &str, field: &str) -> &'a str {
    definitions[role][field].as_str().unwrap_or_default()
}

/// The bundle name: the first 20 hex digits of sha256 over each script's
/// name and LF-normalized bytes.
pub fn bundle_digest(files: &[(String, Vec<u8>)]) -> String {
    let mut digest = Sha256::new();
    for (name, data) in files {
        digest.update(name.as_bytes());
        digest.update([0]);
        digest.update(data);
        digest.update([0]);
    }
    format!("{:x}", digest.finalize())[..20].to_owned()
}

/// `names` from `directory`, UTF-8 checked and CRLF normalized to LF.
pub fn bundle_bytes(directory: &Path, names: &[&str]) -> Result<Vec<(String, Vec<u8>)>, String> {
    names
        .iter()
        .map(|name| {
            let path = directory.join(name);
            let bytes = std::fs::read(&path)
                .map_err(|_| format!("{} could not be read", path.display()))?;
            let text =
                String::from_utf8(bytes).map_err(|_| format!("{} is not UTF-8", path.display()))?;
            Ok(((*name).to_owned(), text.replace("\r\n", "\n").into_bytes()))
        })
        .collect()
}

pub fn is_bundle_name(name: &str) -> bool {
    name.len() == 20
        && name
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The commands only shipped legacy installers wrote; a substring match
/// would remove or approve arbitrary user code. The two echo lines are read
/// from the checkout's `ops/install-hook.ps1`, as the Python setup does.
pub fn legacy_commands(checkout: &Path) -> Result<Vec<String>, String> {
    let path = checkout.join("ops").join("install-hook.ps1");
    let script = std::fs::read_to_string(&path).map_err(|_| {
        format!(
            "{} could not be read; pass --checkout with this repository's root",
            path.display()
        )
    })?;
    let line = |name: &str| -> Result<String, String> {
        let marker = format!("${name} = \"");
        script
            .lines()
            .find_map(|l| l.find(&marker).map(|at| &l[at + marker.len()..]))
            .and_then(|rest| rest.rfind('"').map(|end| rest[..end].to_owned()))
            .ok_or_else(|| format!("{} has no {name}", path.display()))
    };
    let discipline = line("disciplineLine")?;
    let coordination = line("coordinationLine")?;
    let briefings = [
        "pseudolife-mcp briefing --hook-json",
        "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json",
    ];
    let prompts = [
        "pseudolife-mcp prompt-hook",
        "docker exec -i pseudolife-mcp-daemon pseudolife-mcp prompt-hook",
    ];
    let mut commands: Vec<String> = briefings.iter().map(|c| (*c).to_owned()).collect();
    commands.extend(briefings.iter().map(|c| format!("{c} --coordination")));
    commands.extend(prompts.iter().map(|c| (*c).to_owned()));
    for text in [&discipline, &coordination] {
        commands.push(format!("echo '{text}'"));
        commands.push(format!("Write-Output '{text}'"));
    }
    Ok(commands)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_quote_matches_python_shlex() {
        assert_eq!(
            shell_quote("/home/u/.codex/run.sh"),
            "/home/u/.codex/run.sh"
        );
        assert_eq!(shell_quote(""), "''");
        assert_eq!(shell_quote("a b"), "'a b'");
        assert_eq!(shell_quote("o'brien"), "'o'\"'\"'brien'");
        assert_eq!(shell_quote("C:\\x"), "'C:\\x'");
        assert_eq!(shell_quote("$x"), "'$x'");
    }

    #[test]
    fn the_launchers_are_the_bytes_every_release_shipped() {
        let hash = |data: &[u8]| format!("{:x}", Sha256::digest(data));
        assert_eq!(
            hash(RUN_SH),
            "23dc357bbddc1b4687f9ddfe54f8591b03fbc6e1b19bd41c3aeb2834b0f0a1ce"
        );
        assert_eq!(
            hash(RUN_PS1),
            "0ff3ff20c091588aa5b8bf2d2f6859c0ade5a922ba83f9ac8b824415f09d2bc7"
        );
        assert!(launcher_shipped("run.sh", RUN_SH));
        assert!(!launcher_shipped("run.sh", b"echo changed\n"));
        assert!(!launcher_shipped("run.ps1", RUN_SH));
    }

    #[test]
    fn bundle_names_are_twenty_lowercase_hex_digits() {
        assert!(is_bundle_name("0123456789abcdef0123"));
        assert!(!is_bundle_name("0123456789ABCDEF0123"));
        assert!(!is_bundle_name("../../elsewhere"));
        assert!(!is_bundle_name("0123456789abcdef012"));
    }

    #[test]
    fn the_optional_handler_counts_match_the_shipped_manifest() {
        let manifest: Value = serde_json::from_slice(
            &std::fs::read(
                Path::new(env!("CARGO_MANIFEST_DIR")).join("../../plugin/hooks/hooks.json"),
            )
            .unwrap(),
        )
        .unwrap();
        let hooks = manifest["hooks"].as_object().unwrap();
        for (event, most) in OPTIONAL_PLUGIN_EVENTS {
            let section = plugin_event(event).unwrap();
            let count: usize = hooks[section]
                .as_array()
                .unwrap()
                .iter()
                .map(|g| g["hooks"].as_array().unwrap().len())
                .sum();
            assert_eq!(count, most, "{section}");
        }
        // Every event the plugin ships is one setup knows.
        for section in hooks.keys() {
            assert!(
                [
                    "SessionStart",
                    "UserPromptSubmit",
                    "SessionEnd",
                    "Stop",
                    "StopFailure",
                    "SubagentStart",
                    "SubagentStop",
                    "PreToolUse"
                ]
                .contains(&section.as_str()),
                "{section}"
            );
        }
    }
}
