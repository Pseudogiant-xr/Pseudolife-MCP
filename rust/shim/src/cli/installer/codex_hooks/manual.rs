//! Manual hook copies: the scripts as a content-addressed bundle under
//! `<CODEX_HOME>/pseudolife/hooks/<digest>`, the two launchers beside it,
//! `current` naming the bundle, and `<CODEX_HOME>/hooks.json` handlers that
//! run the launchers. Also which listed hooks are ours, and the checks that
//! what runs is exactly what setup wrote.
use super::definitions::*;
use super::{Fail, Hook, Report};
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

fn fail(message: &str) -> Fail {
    Fail::new(message)
}

fn same_file(a: &Path, b: &Path) -> bool {
    let resolve = |p: &Path| crate::setup::codex::resolved_path(p).unwrap_or_else(|_| p.to_owned());
    resolve(a) == resolve(b)
}

/// The 20-hex bundle directories setup ever wrote, sorted.
fn bundles(home: &Path) -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = fs::read_dir(hooks_root(home))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|e| e.file_name().to_str().is_some_and(is_bundle_name))
        .map(|e| e.path())
        .collect();
    found.sort();
    found
}

fn matches_role(
    hook: &Hook,
    definitions: &Map<String, Value>,
    legacy: Option<&Map<String, Value>>,
) -> bool {
    manual_roles(&hook.event).iter().any(|role| {
        hook.command == command_of(definitions, role, "command")
            || hook.command == command_of(definitions, role, "commandWindows")
            || legacy.is_some_and(|l| hook.command == command_of(l, role, "command"))
    })
}

/// Recognize our installed hooks by an exact generated command.
pub fn owned(hook: &Hook, home: &Path) -> bool {
    if hook.source == "plugin" || !same_file(Path::new(&hook.source_path), &home.join("hooks.json"))
    {
        return false;
    }
    if matches_role(hook, &launcher_definitions(home), None) {
        return true;
    }
    bundles(home).iter().any(|directory| {
        matches_role(
            hook,
            &manual_definitions(directory, true),
            Some(&manual_definitions(directory, false)),
        )
    })
}

pub fn is_legacy(hook: &Hook, home: &Path, legacy: &[String]) -> bool {
    hook.source != "plugin"
        && same_file(Path::new(&hook.source_path), &home.join("hooks.json"))
        && legacy.contains(&hook.command)
}

/// Which source runs PseudoLife's hooks, its hooks, and the ones it
/// replaces. A disabled optional plugin handler (Stop's park gate, the
/// PreToolUse guard, the subagent liveness no-ops) is the user's choice and
/// is kept, not refused.
pub fn select<'a>(
    hooks: &'a [Hook],
    home: &Path,
    source: &str,
    legacy: &[String],
) -> Result<(String, Vec<&'a Hook>, Vec<&'a Hook>), Fail> {
    let plugin: Vec<&Hook> = hooks
        .iter()
        .filter(|h| h.plugin_id == PLUGIN_ID && (h.enabled || optional_most(&h.event).is_none()))
        .collect();
    let manual: Vec<&Hook> = hooks.iter().filter(|h| owned(h, home)).collect();
    let old: Vec<&Hook> = hooks
        .iter()
        .filter(|h| is_legacy(h, home, legacy))
        .collect();
    let contains = |list: &[&Hook], hook: &Hook| list.iter().any(|h| std::ptr::eq(*h, hook));
    if hooks
        .iter()
        .any(|h| !contains(&old, h) && !contains(&plugin, h) && legacy.contains(&h.command))
    {
        return Err(fail(
            "Existing PseudoLife hooks use another configuration source; review them in Codex /hooks before migrating.",
        ));
    }
    let chosen = match source {
        "auto" if !plugin.is_empty() => "plugin",
        "auto" => "manual",
        other => other,
    };
    if chosen == "manual" && !plugin.is_empty() {
        return Err(fail(
            "The PseudoLife plugin already owns hooks. Use auto or plugin hooks to avoid running them twice.",
        ));
    }
    if plugin.iter().chain(&manual).chain(&old).any(|h| !h.enabled) {
        return Err(fail(
            "A PseudoLife hook is disabled. Re-enable it in Codex /hooks if desired; setup preserves disabled hooks.",
        ));
    }
    Ok(if chosen == "plugin" {
        let mut obsolete = manual;
        obsolete.extend(old);
        ("plugin".into(), plugin, obsolete)
    } else {
        (chosen.to_owned(), manual, old)
    })
}

/// Memory and coordination each have their own start and prompt hooks, and
/// the memory-policy block a start hook of its own.
pub fn complete_set(hooks: &[&Hook], source: &str) -> bool {
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for hook in hooks {
        *counts.entry(hook.event.as_str()).or_default() += 1;
    }
    if source == "plugin" {
        counts.retain(|event, count| {
            optional_most(event).is_none_or(|most| *count < 1 || *count > most)
        });
    }
    let required: BTreeMap<&str, usize> = EVENTS
        .iter()
        .map(|(event, _)| (*event, manual_roles(event).len()))
        .collect();
    let unique = |field: fn(&Hook) -> &str| {
        let mut seen: Vec<&str> = hooks.iter().map(|h| field(h)).collect();
        seen.sort();
        seen.dedup();
        seen.len() == hooks.len()
    };
    counts == required && unique(|h| h.command.as_str()) && unique(|h| h.key.as_str())
}

fn roles_complete(hooks: &[&Hook], definitions: &Map<String, Value>) -> Result<(), Fail> {
    for (event, _) in EVENTS {
        let roles = manual_roles(event);
        let seen: Vec<&str> = hooks
            .iter()
            .filter(|h| h.event == event)
            .map(|h| h.command.as_str())
            .collect();
        if seen.len() != roles.len()
            || roles.iter().any(|role| {
                !seen.iter().any(|command| {
                    *command == command_of(definitions, role, "command")
                        || *command == command_of(definitions, role, "commandWindows")
                })
            })
        {
            return Err(fail(
                "The manual PseudoLife hook roles are incomplete; rerun setup with approval to repair them.",
            ));
        }
    }
    Ok(())
}

fn pointer(home: &Path) -> Option<String> {
    let name = fs::read_to_string(hooks_root(home).join(POINTER)).ok()?;
    let name = name.trim();
    is_bundle_name(name).then(|| name.to_owned())
}

/// The launchers are ones a release shipped, `current` names a bundle, and
/// that bundle's scripts still digest to its name. `repairable` (setup with
/// approval, which rewrites the launchers and the pointer next) checks only
/// the bundle, and only when the pointer names one.
fn vet_launcher(home: &Path, repairable: bool) -> Result<(), Fail> {
    let root = hooks_root(home);
    if !repairable {
        for (name, _) in LAUNCHERS {
            let intact = fs::read(root.join(name)).is_ok_and(|data| launcher_shipped(name, &data));
            if !intact {
                return Err(fail(
                    "A PseudoLife hook launcher is missing or was modified; rerun the installer and approve the Codex hooks to repair it.",
                ));
            }
        }
    }
    let Some(name) = pointer(home) else {
        if repairable {
            return Ok(());
        }
        return Err(fail(
            "The PseudoLife hook pointer is missing or malformed; rerun the installer and approve the Codex hooks to repair it.",
        ));
    };
    let matches =
        bundle_bytes(&root.join(&name), &SCRIPTS).is_ok_and(|files| bundle_digest(&files) == name);
    if !(matches || repairable && !root.join(&name).exists()) {
        return Err(fail(
            "An installed PseudoLife script was modified; restore or review it before running verification.",
        ));
    }
    Ok(())
}

pub fn vet(hooks: &[&Hook], home: &Path, complete: bool) -> Result<(), Fail> {
    if complete && !complete_set(hooks, "manual") {
        return Err(fail(
            "The manual PseudoLife hook set is incomplete; rerun setup with approval to repair it.",
        ));
    }
    let launcher = launcher_definitions(home);
    if !hooks.is_empty() && hooks.iter().all(|h| matches_role(h, &launcher, None)) {
        vet_launcher(home, !complete)?;
        if complete {
            roles_complete(hooks, &launcher)?;
        }
        return Ok(());
    }
    let directories: Vec<PathBuf> = bundles_any(home)
        .into_iter()
        .filter(|directory| {
            let current = manual_definitions(directory, true);
            let legacy = manual_definitions(directory, false);
            hooks
                .iter()
                .all(|h| matches_role(h, &current, Some(&legacy)))
        })
        .collect();
    let [directory] = directories.as_slice() else {
        return Err(fail(
            "Manual PseudoLife hooks reference mixed or unknown script bundles; review Codex /hooks.",
        ));
    };
    if complete {
        roles_complete(hooks, &manual_definitions(directory, true))?;
    }
    let name = directory.file_name().unwrap_or_default().to_string_lossy();
    let mut matches = bundle_bytes(directory, &SCRIPTS).is_ok_and(|f| bundle_digest(&f) == name);
    if !matches && !complete {
        // Only an approved upgrade may accept the exact pre-split bundle;
        // its directory name still authenticates the four original files.
        let entries: Vec<_> = fs::read_dir(directory)
            .into_iter()
            .flatten()
            .filter_map(Result::ok)
            .collect();
        let mut names: Vec<String> = entries
            .iter()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        let mut expected: Vec<String> = LEGACY_SCRIPTS.iter().map(|s| (*s).to_owned()).collect();
        expected.sort();
        let plain = entries
            .iter()
            .all(|e| e.file_type().is_ok_and(|t| t.is_file() && !t.is_symlink()));
        if names == expected && plain {
            matches =
                bundle_bytes(directory, &LEGACY_SCRIPTS).is_ok_and(|f| bundle_digest(&f) == name);
        }
    }
    if !matches {
        return Err(fail(
            "An installed PseudoLife script was modified; restore or review it before running verification.",
        ));
    }
    Ok(())
}

/// Every entry of the hooks folder, as the Python setup's `glob("*")` sees
/// it: a bundle-named command can only match its own directory anyway.
fn bundles_any(home: &Path) -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = fs::read_dir(hooks_root(home))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|e| e.path())
        .collect();
    found.sort();
    found
}

// -- writing ------------------------------------------------------------------

/// Create `path` (owner-only) with `data`; its folder is created private
/// only when it does not exist yet, never re-protected.
fn create_file(path: &Path, data: &[u8]) -> Result<(), Fail> {
    let parent = path
        .parent()
        .ok_or_else(|| fail("A hook file path has no folder."))?;
    if !parent.is_dir() {
        crate::board::state::private_dir(parent).map_err(|_| {
            Fail::detail(format!(
                "The Codex hook folder {} could not be created.",
                parent.display()
            ))
        })?;
    }
    crate::board::state::atomic_create(path, data).map_err(|_| {
        Fail::detail(format!(
            "The Codex hook file {} could not be written.",
            path.display()
        ))
    })
}

/// Replace (or create) `path` atomically with `data`, keeping a private
/// copy of what it displaced; returns that copy's path when a file existed.
fn replace_file(path: &Path, data: &[u8]) -> Result<Option<PathBuf>, Fail> {
    let before = crate::runtime::registrations::read_optional(path).map_err(|error| {
        Fail::detail(format!("{} could not be read ({error}).", path.display()))
    })?;
    replace_snapshot(path, before.as_deref(), data)
}

/// [`replace_file`] against the content setup read earlier (`None`: the
/// file was absent), so an edit another program made since is refused
/// instead of overwritten.
pub fn replace_snapshot(
    path: &Path,
    before: Option<&[u8]>,
    data: &[u8],
) -> Result<Option<PathBuf>, Fail> {
    if before == Some(data) {
        return Ok(None);
    }
    if before.is_none() {
        let parent = path
            .parent()
            .ok_or_else(|| fail("A hook file path has no folder."))?;
        if !parent.is_dir() {
            crate::board::state::private_dir(parent).map_err(|_| {
                Fail::detail(format!(
                    "The Codex hook folder {} could not be created.",
                    parent.display()
                ))
            })?;
        }
    }
    let backup =
        crate::cli::installer::codex_config::write_retrying(path, before.unwrap_or_default(), data)
            .map_err(|error| {
                Fail::detail(format!(
                    "{} could not be written ({error}); it was left as it was.",
                    path.display()
                ))
            })?;
    Ok(before.map(|_| backup))
}

fn launched(root: &Path) -> Vec<String> {
    fs::read_to_string(root.join(LAUNCHED))
        .map(|text| {
            text.split_whitespace()
                .filter(|n| is_bundle_name(n))
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn lines(names: &[String]) -> Vec<u8> {
    names
        .iter()
        .map(|n| format!("{n}\n"))
        .collect::<String>()
        .into_bytes()
}

/// Write `files` as their content-addressed bundle. A bundle this call
/// writes into is recorded as a launcher bundle (and so may be pruned
/// later); one that already held every file is not: it may be a bundle from
/// before the launcher that a running session's commands still name.
fn write_bundle(root: &Path, files: &[(String, Vec<u8>)]) -> Result<String, Fail> {
    let name = bundle_digest(files);
    let directory = root.join(&name);
    let mut wrote = false;
    for (file, data) in files {
        let destination = directory.join(file);
        if destination.exists() {
            if fs::read(&destination).ok().as_deref() != Some(data.as_slice()) {
                return Err(fail(
                    "An installed PseudoLife script was modified; restore or review it before rerunning setup.",
                ));
            }
        } else {
            create_file(&destination, data)?;
            wrote = true;
        }
    }
    let mut recorded = launched(root);
    if wrote && !recorded.contains(&name) {
        recorded.push(name.clone());
        replace_file(&root.join(LAUNCHED), &lines(&recorded))?;
    }
    Ok(name)
}

fn write_launchers(root: &Path) -> Result<(), Fail> {
    for (name, data) in LAUNCHERS {
        replace_file(&root.join(name), data)?;
    }
    Ok(())
}

/// Remove launcher bundles other than `keep`. A hook still running from one
/// can hold a file on Windows; what cannot go now goes on a later run.
fn prune(root: &Path, keep: &str) -> Result<(), Fail> {
    let mut kept = Vec::new();
    for name in launched(root) {
        if name != keep {
            let _ = fs::remove_dir_all(root.join(&name));
        }
        if root.join(&name).exists() {
            kept.push(name);
        }
    }
    replace_file(&root.join(LAUNCHED), &lines(&kept))?;
    Ok(())
}

/// Write the manual hooks (unless `plugin`, which only removes ours and the
/// legacy ones), replacing handlers setup wrote before and keeping
/// everything else. Group order is setup's: kept groups, then ours in role
/// order, so the keys Codex approved stay put.
pub fn install(
    home: &Path,
    report: &mut Report,
    plugin: bool,
    scripts: &Path,
    legacy: &[String],
) -> Result<(), Fail> {
    let path = home.join("hooks.json");
    let original = crate::runtime::registrations::read_optional(&path).map_err(|error| {
        Fail::detail(format!("{} could not be read ({error}).", path.display()))
    })?;
    let mut object: Map<String, Value> = match &original {
        Some(bytes) if !bytes.is_empty() => serde_json::from_slice::<Value>(bytes)
            .ok()
            .and_then(|v| v.as_object().cloned())
            .ok_or_else(|| {
                Fail::detail(format!(
                    "{} is not a JSON object; fix or move it, then rerun.",
                    path.display()
                ))
            })?,
        _ => Map::new(),
    };
    let mut known: Vec<String> = legacy.to_vec();
    for directory in bundles(home) {
        for marker in [true, false] {
            for definition in manual_definitions(&directory, marker).values() {
                known.push(
                    definition["command"]
                        .as_str()
                        .unwrap_or_default()
                        .to_owned(),
                );
                known.push(
                    definition["commandWindows"]
                        .as_str()
                        .unwrap_or_default()
                        .to_owned(),
                );
            }
        }
    }
    for definition in launcher_definitions(home).values() {
        known.push(
            definition["command"]
                .as_str()
                .unwrap_or_default()
                .to_owned(),
        );
        known.push(
            definition["commandWindows"]
                .as_str()
                .unwrap_or_default()
                .to_owned(),
        );
    }
    let is_known = |value: &Value| value.as_str().is_some_and(|c| known.iter().any(|k| k == c));
    let hooks = object.entry("hooks").or_insert_with(|| json!({}));
    let Some(hooks) = hooks.as_object_mut() else {
        return Err(Fail::detail(format!(
            "{} has a hooks value that is not an object; fix it, then rerun.",
            path.display()
        )));
    };
    for groups in hooks.values().filter_map(Value::as_array) {
        for group in groups {
            let malformed = !group.is_object()
                || group.get("hooks").is_some_and(|h| !h.is_array())
                || group["hooks"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .any(|h| !h.is_object());
            if malformed {
                return Err(Fail::detail(format!(
                    "{} has a hook group that is not an object with a hooks list; fix it, then rerun.",
                    path.display()
                )));
            }
            for handler in group["hooks"].as_array().into_iter().flatten() {
                let commands = [&handler["command"], &handler["commandWindows"]];
                if commands.iter().any(|c| is_known(c))
                    && commands
                        .iter()
                        .any(|c| c.as_str().is_some_and(|c| !c.is_empty()) && !is_known(c))
                {
                    return Err(fail(
                        "A PseudoLife hook has a custom platform command. Review it in Codex /hooks before migration; existing hooks were preserved.",
                    ));
                }
            }
        }
    }
    let mut current = None;
    let mut definitions = Map::new();
    if !plugin {
        let root = hooks_root(home);
        let files = bundle_bytes(scripts, &SCRIPTS).map_err(|e| {
            Fail::detail(format!(
                "The checkout's hook scripts could not be read: {e}."
            ))
        })?;
        let name = write_bundle(&root, &files)?;
        write_launchers(&root)?;
        replace_file(&root.join(POINTER), format!("{name}\n").as_bytes())?;
        current = Some(name);
        definitions = launcher_definitions(home);
    }
    for (codex_event, section) in EVENTS {
        let mut groups = Vec::new();
        for group in hooks
            .get(section)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
        {
            // An unknown platform override is user customization, even when
            // its other command matches a legacy installer exactly.
            let kept: Vec<Value> = group["hooks"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|h| {
                    h["type"] != "command"
                        || !is_known(&h["command"])
                        || (h["commandWindows"].as_str().is_some_and(|c| !c.is_empty())
                            && !is_known(&h["commandWindows"]))
                })
                .cloned()
                .collect();
            if !kept.is_empty() {
                let mut group = group.as_object().cloned().unwrap_or_default();
                group.insert("hooks".into(), Value::Array(kept));
                groups.push(Value::Object(group));
            }
        }
        for role in manual_roles(codex_event) {
            if let Some(definition) = definitions.get(*role) {
                groups.push(json!({"hooks": [definition]}));
            }
        }
        hooks.insert(section.into(), Value::Array(groups));
    }
    let value = Value::Object(object);
    // Compare parsed values so formatting alone never creates a backup.
    let unchanged = original
        .as_deref()
        .and_then(|b| serde_json::from_slice::<Value>(b).ok())
        .is_some_and(|before| before == value);
    if !unchanged {
        let mut data = python_json(&value);
        data.push('\n');
        if let Some(backup) = replace_snapshot(&path, original.as_deref(), data.as_bytes())? {
            report.backups.push(backup);
        }
    }
    if let Some(current) = current {
        // Only now does nothing name the older bundles.
        prune(&hooks_root(home), &current)?;
    }
    Ok(())
}

/// `json.dumps(value, indent=2)`: two-space indent and non-ASCII escaped as
/// `\uXXXX`, the bytes the Python setup wrote, so a rerun of either leaves
/// the file as it is.
pub fn python_json(value: &Value) -> String {
    let pretty = serde_json::to_string_pretty(value).expect("JSON value");
    let mut out = String::with_capacity(pretty.len());
    for c in pretty.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}
