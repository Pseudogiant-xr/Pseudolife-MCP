//! The plugin's hooks as Codex lists them must be exactly the checkout's:
//! the same hooks.json, the same scripts, and each listed command one
//! distinct handler of its event in that manifest.
use super::definitions::*;
use super::manual::complete_set;
use super::{Fail, Hook};
use serde_json::Value;
use std::{collections::BTreeMap, path::Path};

const DIFFERS: &str = "Installed PseudoLife hooks differ from this installer. Update the plugin and the installer together (codex plugin marketplace upgrade pseudolife-mcp), or review them in Codex /hooks.";

fn manifest(path: &Path) -> Option<Value> {
    let bytes = std::fs::read(path).ok()?;
    let text = String::from_utf8(bytes).ok()?;
    serde_json::from_str::<Value>(&text)
        .ok()?
        .get("hooks")
        .cloned()
}

pub fn vet(hooks: &[&Hook], checkout: &Path) -> Result<(), Fail> {
    if !complete_set(hooks, "plugin") {
        return Err(Fail::new(
            "The enabled PseudoLife plugin is missing or has unexpected hooks; update it (codex plugin marketplace upgrade pseudolife-mcp) and retry.",
        ));
    }
    let reference = checkout.join("plugin").join("hooks");
    let expected = manifest(&reference.join("hooks.json")).ok_or_else(|| {
        Fail::detail(format!(
            "{} could not be read; pass --checkout with this repository's root.",
            reference.join("hooks.json").display()
        ))
    })?;
    let reference_scripts = bundle_bytes(&reference, &PLUGIN_SCRIPTS).map_err(Fail::detail)?;
    let roles: BTreeMap<String, Vec<Value>> = expected
        .as_object()
        .into_iter()
        .flatten()
        .map(|(event, groups)| {
            let handlers = groups
                .as_array()
                .into_iter()
                .flatten()
                .flat_map(|g| g["hooks"].as_array().cloned().unwrap_or_default())
                .collect();
            (event.clone(), handlers)
        })
        .collect();
    let mut seen: BTreeMap<&str, Vec<usize>> = roles.keys().map(|e| (e.as_str(), vec![])).collect();
    for hook in hooks {
        let path = Path::new(&hook.source_path);
        let directory = path.parent().unwrap_or(Path::new(""));
        let files_match =
            bundle_bytes(directory, &PLUGIN_SCRIPTS).is_ok_and(|f| f == reference_scripts);
        if manifest(path).as_ref() != Some(&expected)
            || !files_match
            || hook.handler_type != "command"
        {
            return Err(Fail::new(DIFFERS));
        }
        let event = plugin_event(&hook.event).ok_or_else(|| Fail::new(DIFFERS))?;
        let root = directory.parent().unwrap_or(Path::new(""));
        let spellings = [
            root.to_string_lossy().into_owned(),
            root.to_string_lossy().replace('\\', "/"),
        ];
        let handlers = roles.get(event).ok_or_else(|| Fail::new(DIFFERS))?;
        let matches: Vec<usize> = handlers
            .iter()
            .enumerate()
            .filter(|(_, handler)| {
                ["command", "commandWindows"].iter().any(|field| {
                    let Some(command) = handler[*field].as_str() else {
                        return false;
                    };
                    hook.command == command
                        || spellings.iter().any(|root| {
                            hook.command
                                == command
                                    .replace("${CLAUDE_PLUGIN_ROOT}", root)
                                    .replace("$env:CLAUDE_PLUGIN_ROOT", root)
                        })
                })
            })
            .map(|(index, _)| index)
            .collect();
        let slot = seen.get_mut(event).ok_or_else(|| Fail::new(DIFFERS))?;
        let [index] = matches.as_slice() else {
            return Err(Fail::new(DIFFERS));
        };
        if slot.contains(index) {
            return Err(Fail::new(DIFFERS));
        }
        slot.push(*index);
    }
    // Every lifecycle role must be listed; an optional event may list any
    // of its handlers, each already matched to a distinct role above.
    let optional: Vec<&str> = OPTIONAL_PLUGIN_EVENTS
        .iter()
        .filter_map(|(event, _)| plugin_event(event))
        .collect();
    if seen
        .iter()
        .any(|(event, found)| !optional.contains(event) && found.len() != roles[*event].len())
    {
        return Err(Fail::new(DIFFERS));
    }
    Ok(())
}
