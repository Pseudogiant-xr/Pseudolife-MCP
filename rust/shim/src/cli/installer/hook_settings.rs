//! `pseudolife-mcp installer hook-settings install|remove-legacy`: the
//! settings.json / hooks.json edits `ops/install-hook.sh` made with python3.
//!
//! The script keeps its arguments, messages and backups, and passes what it
//! computed through the same environment variables its Python heredocs read:
//!
//! - `install`: SETTINGS_PATH, BRIEFING_COMMAND, PROMPT_COMMAND, UPS_COMMAND,
//!   LEGACY_UPS_COMMAND, DISCIPLINE_LINE, COORDINATION_COMMAND and
//!   LEGACY_COORDINATION_LINE. Adds the briefing, coordination and
//!   memory-change hooks where absent, removes the static and obsolete ones.
//! - `remove-legacy`: SETTINGS_PATH, DRY_RUN, UPS_COMMAND,
//!   LEGACY_UPS_COMMAND and COORDINATION_LINE. Removes only exact
//!   installer-written entries, only while the plugin is enabled for all
//!   projects. Exit 0 found (dry run) or removed, 3 none found, 4 plugin not
//!   active, 1 error.
//!
//! Python's json output is reproduced: `indent=2`, `ensure_ascii` on for
//! install and off for remove-legacy, key order kept, a repeated key kept in
//! its first place (install) or refused (remove-legacy).
use crate::cli::connect::pyjson::{self, Dict, J};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};

const PLUGIN_ID: &str = "pseudolife-memory@pseudolife-mcp";

fn env(name: &str) -> Result<String, String> {
    std::env::var(name).map_err(|_| format!("installer hook-settings: {name} is not set"))
}

fn say(line: &str) {
    let _ = writeln!(io::stdout().lock(), "{line}");
}

fn complain(line: &str) {
    let _ = writeln!(io::stderr().lock(), "{line}");
}

/// The value at `key`, JSON null included (`d.get(key)` with a present key).
fn raw<'a>(dict: &'a Dict, key: &str) -> Option<&'a J> {
    dict.iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

fn str_of(value: Option<&J>) -> Option<&str> {
    match value {
        Some(J::Str(text)) => Some(text),
        _ => None,
    }
}

/// `" ".join(command.split())`, cut to 100 characters as `show` does.
fn show(event: &str, command: &str) -> String {
    let command = command
        .split(pyjson::py_space)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    let shown = if command.chars().count() <= 100 {
        command
    } else {
        format!("{}...", command.chars().take(97).collect::<String>())
    };
    format!("  {event}: {shown}")
}

pub(super) fn run(args: &[String]) -> u8 {
    let result = match args {
        [mode] if mode == "install" => install(),
        [mode] if mode == "remove-legacy" => remove_legacy(),
        _ => {
            complain(
                "installer hook-settings: usage: install | remove-legacy (settings from the environment)",
            );
            return 2;
        }
    };
    result.unwrap_or_else(|message| {
        complain(&message);
        1
    })
}

// -- remove-legacy -------------------------------------------------------------

struct Shipped {
    session_start: Vec<String>,
    user_prompt_submit: Vec<String>,
    session_end: Vec<String>,
}
impl Shipped {
    fn new() -> Result<Self, String> {
        let briefings = [
            "pseudolife-mcp briefing --hook-json",
            "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json",
        ];
        let mut session_start: Vec<String> = briefings.iter().map(|b| (*b).to_owned()).collect();
        session_start.extend(briefings.iter().map(|b| format!("{b} --coordination")));
        session_start.push(format!("echo '{}'", env("COORDINATION_LINE")?));
        session_start.push("pseudolife-mcp episode-start".into());
        Ok(Self {
            session_start,
            user_prompt_submit: vec![
                "pseudolife-mcp prompt-hook".into(),
                "docker exec -i pseudolife-mcp-daemon pseudolife-mcp prompt-hook".into(),
                env("UPS_COMMAND")?,
                env("LEGACY_UPS_COMMAND")?,
            ],
            session_end: vec!["pseudolife-mcp episode-end".into()],
        })
    }
    fn known(&self, event: &str) -> Option<&[String]> {
        match event {
            "SessionStart" => Some(&self.session_start),
            "UserPromptSubmit" => Some(&self.user_prompt_submit),
            "SessionEnd" => Some(&self.session_end),
            _ => None,
        }
    }
    /// `is_shipped`: a command hook whose command (and commandWindows, when
    /// set) is exactly one this script or the installers wrote.
    fn is_shipped(&self, event: &str, hook: &J) -> bool {
        let J::Dict(hook) = hook else { return false };
        if str_of(raw(hook, "type")) != Some("command") {
            return false;
        }
        let known = self.known(event).unwrap_or(&[]);
        let command = str_of(raw(hook, "command"));
        let windows = if pyjson::has(hook, "commandWindows") {
            str_of(raw(hook, "commandWindows"))
        } else {
            command
        };
        command.is_some_and(|c| known.iter().any(|k| k == c))
            && windows.is_some_and(|w| known.iter().any(|k| k == w))
    }
}

const NEEDLES: [&str; 6] = [
    "pseudolife-mcp briefing",
    "pseudolife-mcp prompt-hook",
    "mid-session discipline",
    "Pseudolife coordination:",
    "pseudolife-mcp episode-start",
    "pseudolife-mcp episode-end",
];

/// `os.path.abspath`: absolute, without resolving links.
fn absolute(path: &Path) -> PathBuf {
    std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf())
}

fn plugin_active(settings: &J, path: &Path) -> bool {
    let J::Dict(settings) = settings else {
        return false;
    };
    let Some(J::Dict(enabled)) = raw(settings, "enabledPlugins") else {
        return false;
    };
    if !matches!(raw(enabled, PLUGIN_ID), Some(J::Bool(true))) {
        return false;
    }
    let record = absolute(path)
        .parent()
        .map(|dir| dir.join("plugins").join("installed_plugins.json"))
        .unwrap_or_default();
    let Ok(bytes) = fs::read(&record) else {
        return false;
    };
    let Ok(text) = String::from_utf8(bytes) else {
        return false;
    };
    // encoding="utf-8-sig": one byte-order mark.
    let Ok(Some(J::Dict(data))) = pyjson::loads(text.strip_prefix('\u{feff}').unwrap_or(&text))
    else {
        return false;
    };
    let Some(J::Dict(plugins)) = raw(&data, "plugins") else {
        return false;
    };
    let Some(records) = raw(plugins, PLUGIN_ID) else {
        return false;
    };
    let records: Vec<&J> = match records {
        J::List(items) => items.iter().collect(),
        other => vec![other],
    };
    // A project-scoped install runs in that project only.
    records.iter().any(|record| match record {
        J::Dict(record) => {
            !pyjson::has(record, "scope") || str_of(raw(record, "scope")) == Some("user")
        }
        _ => false,
    })
}

fn groups_of(groups: &J) -> &[J] {
    match groups {
        J::List(items) => items,
        _ => &[],
    }
}

fn remove_legacy() -> Result<u8, String> {
    let path = PathBuf::from(env("SETTINGS_PATH")?);
    let dry_run = std::env::var("DRY_RUN").is_ok_and(|v| !v.is_empty());
    let shipped = Shipped::new()?;
    // os.path.exists: a dangling link counts as missing.
    if fs::metadata(&path).is_err() {
        say(&format!(
            "No settings file at {} - nothing to remove.",
            path.display()
        ));
        return Ok(3);
    }
    let raw_bytes =
        fs::read(&path).map_err(|e| format!("Could not read {}: {e}", path.display()))?;
    let text = String::from_utf8(raw_bytes.clone())
        .map_err(|_| format!("Could not read {}: the file is not UTF-8", path.display()))?;
    let (mut settings, duplicate) =
        match pyjson::loads_noting_duplicates(text.strip_prefix('\u{feff}').unwrap_or(&text)) {
            Ok(Some(parsed)) => parsed,
            _ => {
                return Err(format!(
                    "Could not read {}: the file is not valid JSON",
                    path.display()
                ));
            }
        };
    if let Some(key) = duplicate {
        // A duplicate would be silently dropped on rewrite: leave the file.
        return Err(format!(
            "Could not read {}: duplicate key {}",
            path.display(),
            pyjson::py_repr(&key)
        ));
    }
    let (mut found, mut lookalikes) = (Vec::new(), Vec::new());
    if let J::Dict(top) = &settings
        && let Some(J::Dict(hooks)) = raw(top, "hooks")
    {
        for (event, groups) in hooks {
            for group in groups_of(groups) {
                let J::Dict(group) = group else { continue };
                let Some(J::List(entries)) = raw(group, "hooks") else {
                    continue;
                };
                for hook in entries {
                    let J::Dict(entry) = hook else { continue };
                    let Some(command) = str_of(raw(entry, "command")) else {
                        continue;
                    };
                    if shipped.is_shipped(event, hook) {
                        found.push(show(event, command));
                    } else if NEEDLES.iter().any(|n| command.contains(n)) {
                        lookalikes.push(show(event, command));
                    }
                }
            }
        }
    }
    if found.is_empty() {
        say(&format!(
            "No installer-written Pseudolife hooks in {}.",
            path.display()
        ));
    } else {
        say(&format!(
            "Installer-written hooks in {} (the pseudolife-memory plugin provides these now):",
            path.display()
        ));
        say(&found.join("\n"));
    }
    if !lookalikes.is_empty() {
        say("Left alone - not an exact installer-written entry; review by hand:");
        say(&lookalikes.join("\n"));
    }
    if found.is_empty() {
        return Ok(3);
    }
    if !plugin_active(&settings, &path) {
        say(
            "Not removed: the pseudolife-memory plugin is not installed and enabled for all projects, so these may be the only Pseudolife hooks that run.",
        );
        return Ok(4);
    }
    if dry_run {
        return Ok(0);
    }
    // Emptied groups go; an emptied event stays an empty list.
    if let J::Dict(top) = &mut settings
        && let Some(J::Dict(hooks)) = pyjson::get_mut(top, "hooks")
    {
        for (event, groups) in hooks.iter_mut() {
            let (true, J::List(items)) = (shipped.known(event).is_some(), &mut *groups) else {
                continue;
            };
            let mut kept_groups = Vec::new();
            for group in items.drain(..) {
                let mut group = group;
                if let J::Dict(fields) = &mut group
                    && let Some(J::List(entries)) = pyjson::get_mut(fields, "hooks")
                {
                    let before = entries.len();
                    entries.retain(|hook| !shipped.is_shipped(event, hook));
                    if entries.len() != before && entries.is_empty() {
                        continue;
                    }
                }
                kept_groups.push(group);
            }
            *items = kept_groups;
        }
    }
    let mut text = pyjson::dumps(&settings, false) + "\n";
    if raw_bytes.windows(2).any(|w| w == b"\r\n") {
        text = text.replace('\n', "\r\n");
    }
    write_through(&path, text.as_bytes())?;
    say(&format!(
        "Removed {} installer-written hook entries from {}.",
        found.len(),
        path.display()
    ));
    Ok(0)
}

/// A backup beside `path`, then the new text written to the file it points
/// at (a dotfile setup's link stays a link), through a temporary file that
/// takes the target's permissions.
fn write_through(path: &Path, text: &[u8]) -> Result<(), String> {
    let failed = |e: io::Error| format!("Could not write {}: {e}", path.display());
    let target = fs::canonicalize(path).map_err(failed)?;
    let stamp = chrono::Local::now().format("%Y%m%d-%H%M%S").to_string();
    let mut backup = PathBuf::from(format!("{}.bak-{stamp}", path.display()));
    let mut n = 1;
    while fs::symlink_metadata(&backup).is_ok() {
        backup = PathBuf::from(format!("{}.bak-{stamp}-{n}", path.display()));
        n += 1;
    }
    fs::copy(path, &backup).map_err(failed)?;
    say(&format!("Backed up -> {}", backup.display()));
    let directory = target.parent().unwrap_or(Path::new("."));
    let name = target
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temporary = directory.join(format!(
        "{name}.{}.tmp-pseudolife",
        uuid::Uuid::new_v4().simple()
    ));
    let result = (|| -> io::Result<()> {
        // Created owner-only and new, as tempfile.mkstemp did, before any of
        // the settings (which can hold credentials) are in it; it takes the
        // target's permissions only once written.
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        options.open(&temporary)?.write_all(text)?;
        fs::set_permissions(&temporary, fs::metadata(&target)?.permissions())?;
        fs::rename(&temporary, &target)
    })();
    if let Err(error) = result {
        if fs::symlink_metadata(&temporary).is_ok() {
            if let Ok(meta) = fs::metadata(&temporary) {
                let mut permissions = meta.permissions();
                #[allow(clippy::permissions_set_readonly_false)]
                permissions.set_readonly(false);
                let _ = fs::set_permissions(&temporary, permissions);
            }
            let _ = fs::remove_file(&temporary);
        }
        return Err(failed(error));
    }
    Ok(())
}

// -- install -------------------------------------------------------------------

fn command_of(hook: &J) -> &str {
    match hook {
        J::Dict(hook) => str_of(raw(hook, "command")).unwrap_or(""),
        _ => "",
    }
}

fn hooks_of(group: &J) -> &[J] {
    match group {
        J::Dict(group) => match raw(group, "hooks") {
            Some(J::List(items)) => items,
            _ => &[],
        },
        _ => &[],
    }
}

/// `has_command`: a hook whose command contains `needle`, with or without
/// `--coordination` as asked.
fn has_command(groups: &[J], needle: &str, coordination: bool) -> bool {
    groups.iter().flat_map(hooks_of).any(|hook| {
        let command = command_of(hook);
        command.contains(needle) && command.contains("--coordination") == coordination
    })
}

fn add_group(groups: &mut Vec<J>, command: &str) {
    groups.push(J::Dict(vec![(
        "hooks".into(),
        J::List(vec![J::Dict(vec![
            ("type".into(), J::Str("command".into())),
            ("command".into(), J::Str(command.into())),
        ])]),
    )]));
}

/// `g["hooks"] = [kept]` for every group, then emptied groups dropped; true
/// when anything was removed.
fn drop_where(groups: &mut Vec<J>, mut ours: impl FnMut(&J) -> bool) -> bool {
    let mut removed = false;
    for group in groups.iter_mut() {
        if let J::Dict(fields) = group {
            let mut entries: Vec<J> = match raw(fields, "hooks") {
                Some(J::List(items)) => items.clone(),
                _ => Vec::new(),
            };
            let before = entries.len();
            entries.retain(|hook| !ours(hook));
            removed |= entries.len() != before;
            pyjson::set(fields, "hooks", J::List(entries));
        }
    }
    groups.retain(|group| !hooks_of(group).is_empty());
    removed
}

/// `drop_exact`: a hook whose command (and commandWindows, when set) is
/// exactly one of `commands`; a user's hook that mentions the words stays.
fn drop_exact(groups: &mut Vec<J>, commands: &[String]) -> bool {
    drop_where(groups, |hook| {
        let J::Dict(hook) = hook else { return false };
        let command = str_of(raw(hook, "command"));
        let windows = if pyjson::has(hook, "commandWindows") {
            str_of(raw(hook, "commandWindows"))
        } else {
            command
        };
        command.is_some_and(|c| commands.iter().any(|k| k == c))
            && windows.is_some_and(|w| commands.iter().any(|k| k == w))
    })
}

fn event_list<'a>(hooks: &'a mut Dict, event: &str) -> Result<&'a mut Vec<J>, String> {
    if !pyjson::has(hooks, event) {
        hooks.push((event.into(), J::List(Vec::new())));
    }
    match pyjson::get_mut(hooks, event) {
        Some(J::List(items)) => {
            // The heredoc called .get on every group and hook here, so any
            // other shape stopped it before it wrote anything: refuse the
            // same file rather than drop what it cannot read.
            let well_formed = items.iter().all(|group| match group {
                J::Dict(fields) => match raw(fields, "hooks") {
                    Some(J::List(entries)) => entries.iter().all(|hook| matches!(hook, J::Dict(_))),
                    Some(other) => !other.truthy(),
                    None => true,
                },
                _ => false,
            });
            if !well_formed {
                return Err(format!(
                    "installer hook-settings: hooks.{event} holds an entry that is not a hook group (an object whose \"hooks\" is a list of objects); fix it by hand and rerun. Nothing was changed."
                ));
            }
            Ok(items)
        }
        _ => Err(format!(
            "installer hook-settings: hooks.{event} is not a list; fix it by hand and rerun. Nothing was changed."
        )),
    }
}

fn install() -> Result<u8, String> {
    let path = PathBuf::from(env("SETTINGS_PATH")?);
    let briefing = env("BRIEFING_COMMAND")?;
    let prompt = env("PROMPT_COMMAND")?;
    let discipline = env("DISCIPLINE_LINE")?;
    let static_commands = vec![
        env("UPS_COMMAND")?,
        env("LEGACY_UPS_COMMAND")?,
        format!("Write-Output '{discipline}'"),
    ];
    let coordination = env("COORDINATION_COMMAND")?;
    let legacy_line = env("LEGACY_COORDINATION_LINE")?;
    let unreadable = |reason: &str| format!("Could not read {}: {reason}", path.display());
    let mut settings = if fs::metadata(&path).is_ok() {
        // open(encoding="utf-8"): a byte-order mark is not JSON here either.
        let bytes = fs::read(&path).map_err(|e| unreadable(&e.to_string()))?;
        let text = String::from_utf8(bytes).map_err(|_| unreadable("the file is not UTF-8"))?;
        match pyjson::loads(&text) {
            Ok(Some(value)) => value,
            _ => return Err(unreadable("the file is not valid JSON")),
        }
    } else {
        J::Dict(Vec::new())
    };
    let J::Dict(top) = &mut settings else {
        return Err(unreadable("the settings are not a JSON object"));
    };
    if !pyjson::has(top, "hooks") {
        top.push(("hooks".into(), J::Dict(Vec::new())));
    }
    let Some(J::Dict(hooks)) = pyjson::get_mut(top, "hooks") else {
        return Err(unreadable("hooks is not a JSON object"));
    };
    let shown = path.display();
    event_list(hooks, "SessionStart")?;
    event_list(hooks, "SessionEnd")?;
    {
        let start = event_list(hooks, "SessionStart")?;
        if has_command(start, "pseudolife-mcp briefing", false) {
            say(&format!(
                "Briefing hook already present in {shown} - skipping."
            ));
        } else {
            add_group(start, &briefing);
            say(&format!("Installed SessionStart briefing hook -> {shown}"));
            say(&format!("  command: {briefing}"));
        }
        if drop_exact(
            start,
            &[
                format!("echo '{legacy_line}'"),
                format!("Write-Output '{legacy_line}'"),
            ],
        ) {
            say("Removed the unconditional coordination check-in hook.");
        }
        if !has_command(start, "pseudolife-mcp briefing", true) {
            add_group(start, &coordination);
            say(&format!(
                "Installed SessionStart coordination hook -> {shown}"
            ));
            say(&format!("  command: {coordination}"));
        }
    }
    {
        let prompt_groups = event_list(hooks, "UserPromptSubmit")?;
        if drop_exact(prompt_groups, &static_commands) {
            say("Removed the static mid-session discipline hook.");
        }
        if has_command(prompt_groups, "pseudolife-mcp prompt-hook", false) {
            say(&format!(
                "Memory-change hook already present in {shown} - skipping."
            ));
        } else {
            add_group(prompt_groups, &prompt);
            say(&format!(
                "Installed UserPromptSubmit memory-change hook -> {shown}"
            ));
            say(&format!("  command: {prompt}"));
        }
    }
    // Episode hooks are obsolete since the daemon owns session episodes:
    // remove any an earlier installer added.
    if drop_where(event_list(hooks, "SessionStart")?, |hook| {
        command_of(hook).contains("pseudolife-mcp episode-start")
    }) {
        say("Removed obsolete episode-start hook (daemon owns episodes now).");
    }
    if drop_where(event_list(hooks, "SessionEnd")?, |hook| {
        command_of(hook).contains("pseudolife-mcp episode-end")
    }) {
        say("Removed obsolete episode-end hook (daemon owns episodes now).");
    }
    // json.dump(obj, f, indent=2) + "\n" in text mode: ensure_ascii on, and
    // the platform's line ending; written in place, so a link stays a link.
    let mut text = pyjson::dumps(&settings, true) + "\n";
    if cfg!(windows) {
        text = text.replace('\n', "\r\n");
    }
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(parent)
            .map_err(|e| format!("Could not write {}: {e}", path.display()))?;
    }
    fs::write(&path, text).map_err(|e| format!("Could not write {}: {e}", path.display()))?;
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn show_collapses_whitespace_and_cuts_at_one_hundred_characters() {
        assert_eq!(show("SessionStart", "a \t b\n c"), "  SessionStart: a b c");
        let long = "x".repeat(101);
        assert_eq!(show("E", &long), format!("  E: {}...", "x".repeat(97)));
        assert_eq!(
            show("E", &"y".repeat(100)),
            format!("  E: {}", "y".repeat(100))
        );
    }

    #[test]
    fn exact_drops_keep_a_users_hook_that_only_mentions_the_words() {
        let mut groups = vec![J::Dict(vec![(
            "hooks".into(),
            J::List(vec![
                J::Dict(vec![("command".into(), J::Str("echo 'x'".into()))]),
                J::Dict(vec![("command".into(), J::Str("echo 'x' && more".into()))]),
            ]),
        )])];
        assert!(drop_exact(&mut groups, &["echo 'x'".into()]));
        assert_eq!(hooks_of(&groups[0]).len(), 1);
        assert!(!drop_exact(&mut groups, &["echo 'x'".into()]));
    }
}
