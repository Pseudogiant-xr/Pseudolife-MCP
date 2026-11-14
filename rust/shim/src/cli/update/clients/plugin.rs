use super::*;
use command::Runner;
use std::collections::BTreeMap;

pub(super) const PLUGIN: &str = "pseudolife-memory@pseudolife-mcp";
pub(super) const MARKETPLACE: &str = "pseudolife-mcp";
const HTTPS: &str = "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git";

fn normalized(path: &Path) -> Result<Vec<u8>, String> {
    let bytes = fs::read(path).map_err(|_| "plugin file could not be read")?;
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i..].starts_with(b"\r\n") {
            out.push(b'\n');
            i += 2;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    Ok(out)
}
pub(super) fn tree_files(root: &Path) -> Result<BTreeMap<PathBuf, Vec<u8>>, String> {
    fn walk(
        root: &Path,
        path: &Path,
        files: &mut BTreeMap<PathBuf, Vec<u8>>,
        depth: usize,
    ) -> Result<(), String> {
        if depth > 64 || files.len() > 100000 {
            return Err("plugin tree exceeds inspection limits".into());
        }
        for entry in fs::read_dir(path).map_err(|_| "plugin tree could not be read")? {
            let entry = entry.map_err(|_| "plugin tree could not be read")?;
            let path = entry.path();
            let relative = path
                .strip_prefix(root)
                .map_err(|_| "plugin tree escaped its root")?;
            let first = relative
                .components()
                .next()
                .unwrap()
                .as_os_str()
                .to_string_lossy();
            if (first.starts_with('.') && first != ".claude-plugin")
                || relative.components().any(|c| c.as_os_str() == ".git")
            {
                continue;
            }
            let metadata =
                fs::symlink_metadata(&path).map_err(|_| "plugin tree could not be read")?;
            if runtime::redirect(&metadata) {
                return Err("plugin tree contains a redirect; no cache changed".into());
            }
            if metadata.is_dir() {
                walk(root, &path, files, depth + 1)?;
            } else if metadata.is_file() {
                files.insert(relative.to_owned(), normalized(&path)?);
            } else {
                return Err("plugin tree contains a non-regular file".into());
            }
        }
        Ok(())
    }
    let mut files = BTreeMap::new();
    walk(root, root, &mut files, 0)?;
    Ok(files)
}
pub(super) fn plugin_record(root: &Path) -> Result<Option<Value>, String> {
    let data = read_json(&root.join("installed_plugins.json"))?;
    Ok(data
        .get("plugins")
        .and_then(|v| v.get(PLUGIN))
        .and_then(Value::as_array)
        .and_then(|v| v.first())
        .cloned())
}
fn clone_dir(root: &Path) -> Result<PathBuf, String> {
    let data = read_json(&root.join("known_marketplaces.json"))?;
    Ok(data
        .get(MARKETPLACE)
        .and_then(|v| v.get("installLocation"))
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("marketplaces").join(MARKETPLACE))
        .join("plugin"))
}
fn source_kind(source: Option<&Value>) -> &'static str {
    let Some(source) = source.filter(|v| !v.is_null()) else {
        return "none";
    };
    if source.as_object().is_some_and(|m| m.len() == 2)
        && source["source"] == "github"
        && source["repo"]
            .as_str()
            .is_some_and(|s| s.eq_ignore_ascii_case("pseudogiant-xr/pseudolife-mcp"))
    {
        "github"
    } else if source["source"] == "git"
        && source["url"]
            .as_str()
            .is_some_and(|s| s.starts_with("https://"))
    {
        "https"
    } else {
        "other"
    }
}
fn known_source(root: &Path) -> Result<Option<Value>, String> {
    Ok(read_json(&root.join("known_marketplaces.json"))?
        .get(MARKETPLACE)
        .and_then(|v| v.get("source"))
        .cloned())
}

pub(super) fn migrate_with(
    root: &Path,
    settings: &Path,
    claude: &Path,
    runner: &impl Runner,
) -> Value {
    let outcome = (|| -> Result<Value, String> {
        let known = known_source(root)?;
        let bytes = registrations::read_optional(settings)?;
        let parsed = bytes.as_deref().map(registrations::json_bytes).transpose();
        let mut data=match parsed {
            Ok(value)=>value.unwrap_or_else(||json!({})),
            Err(_) if matches!(source_kind(known.as_ref()),"https"|"other")=>json!({}),
            Err(_)=>return Err("settings.json is not valid JSON; fix it before marketplace migration; nothing changed".into()),
        };
        let original = data
            .get("extraKnownMarketplaces")
            .and_then(|v| v.get(MARKETPLACE))
            .cloned();
        let declared = original.as_ref().and_then(|v| v.get("source"));
        let kinds = [source_kind(known.as_ref()), source_kind(declared)];
        if kinds == ["none", "none"] {
            return Ok(result(
                "absent",
                "no pseudolife-mcp marketplace is recorded",
            ));
        }
        if kinds.contains(&"other") {
            return Ok(result(
                "kept",
                "the marketplace has a user-selected source; left as it is",
            ));
        }
        if !kinds.contains(&"github") {
            return Ok(result(
                "current",
                "the marketplace already fetches over HTTPS",
            ));
        }
        let backup = root.join(format!(
            "backup-native-marketplace-{}",
            uuid::Uuid::new_v4()
        ));
        crate::board::state::private_dir(&backup).map_err(str::to_owned)?;
        for path in [
            settings.to_owned(),
            root.join("known_marketplaces.json"),
            root.join("installed_plugins.json"),
        ] {
            if let Some(bytes) = registrations::read_optional(&path)? {
                crate::board::state::atomic_create(&backup.join(path.file_name().unwrap()), &bytes)
                    .map_err(str::to_owned)?;
            }
        }
        let before_record = plugin_record(root)?;
        if source_kind(declared) == "github" {
            data["extraKnownMarketplaces"][MARKETPLACE]["source"] =
                json!({"source":"git","url":HTTPS});
            registrations::write_config(
                settings,
                bytes.as_deref().unwrap_or_default(),
                &serde_json::to_vec_pretty(&data).unwrap(),
            )?;
        }
        let output = runner.run(
            claude,
            &["plugin", "marketplace", "add", HTTPS],
            None,
            Duration::from_secs(300),
            &[],
        );
        let now = known_source(root)?;
        if source_kind(now.as_ref()) == "https" && output.code != 0 {
            return Ok(result(
                "failed",
                format!(
                    "marketplace add ended with exit {} after moving the record to HTTPS; settings stays on HTTPS to match; run claude plugin marketplace update pseudolife-mcp; private backups retained",
                    output.code
                ),
            ));
        }
        if output.code != 0 || source_kind(now.as_ref()) != "https" {
            // Re-read after the CLI: undo only this entry, keeping concurrent
            // writes to unrelated settings. Never remove the marketplace.
            let after_bytes = registrations::read_optional(settings)?;
            if after_bytes.is_none() && original.is_none() {
                return Ok(result(
                    "failed",
                    format!(
                        "marketplace add did not verify (exit {}); no settings entry existed to restore; installed plugin retained",
                        output.code
                    ),
                ));
            }
            let after_bytes = after_bytes.unwrap_or_default();
            let mut after = if after_bytes.is_empty() {
                json!({})
            } else {
                registrations::json_bytes(&after_bytes)?
            };
            if let Some(original) = original {
                if after.get("extraKnownMarketplaces").is_none() {
                    after["extraKnownMarketplaces"] = json!({});
                }
                let entries=after["extraKnownMarketplaces"].as_object_mut().ok_or("marketplace rollback could not preserve changed settings; restore from private backups")?;
                entries.insert(MARKETPLACE.into(), original);
            } else if let Some(entries) = after
                .get_mut("extraKnownMarketplaces")
                .and_then(Value::as_object_mut)
            {
                entries.remove(MARKETPLACE);
            }
            registrations::write_config(
                settings,
                &after_bytes,
                &serde_json::to_vec_pretty(&after).unwrap(),
            )?;
            if bytes.is_none() && after == json!({"extraKnownMarketplaces":{}}) {
                fs::remove_file(settings)
                    .map_err(|_| "marketplace rollback could not remove its empty settings file")?;
            }
            return Ok(result(
                "failed",
                format!(
                    "marketplace add did not verify (exit {}); original marketplace settings entry restored; installed plugin retained",
                    output.code
                ),
            ));
        }
        if let Some(original) = original.and_then(|v| v.as_object().cloned()) {
            let after_bytes = registrations::read_optional(settings)?
                .ok_or("marketplace add removed settings.json; restore private backup")?;
            let mut after = registrations::json_bytes(&after_bytes)?;
            let entry = after
                .get_mut("extraKnownMarketplaces")
                .and_then(|v| v.get_mut(MARKETPLACE))
                .and_then(Value::as_object_mut)
                .ok_or("marketplace add removed its settings entry; restore private backup")?;
            for (key, value) in original {
                if key != "source" {
                    entry.entry(key).or_insert(value);
                }
            }
            registrations::write_config(
                settings,
                &after_bytes,
                &serde_json::to_vec_pretty(&after).unwrap(),
            )?;
        }
        if before_record.is_some() && plugin_record(root)?.is_none() {
            return Ok(result(
                "failed",
                "marketplace now uses HTTPS but its plugin record disappeared; run claude plugin install pseudolife-memory@pseudolife-mcp; private backups retained",
            ));
        }
        Ok(result(
            "migrated",
            "marketplace moved from the legacy GitHub source to HTTPS; private backups retained",
        ))
    })();
    outcome.unwrap_or_else(|detail| result("failed", detail))
}

/// The Claude Code marketplace source migration on its own, for the
/// installers (which used to run `ops/plugin_marketplace.py`), with the same
/// defaults as [`update_plugin`]. The report is `{"state", "detail"}`; the
/// states are stable because callers map them: "absent", "current",
/// "migrated", "kept", "failed", and "no-cli" when `claude` is not on PATH.
#[allow(
    dead_code,
    reason = "the native installer (W3's installer pull request) calls it"
)]
pub fn migrate_marketplace() -> Value {
    marketplace_with_cli(registrations::absolute_command("claude"))
}
#[allow(dead_code, reason = "used by migrate_marketplace and its test")]
pub(super) fn marketplace_with_cli(claude: Option<PathBuf>) -> Value {
    let Some(claude) = claude else {
        return result("no-cli", "the claude CLI is not on PATH");
    };
    migrate_with(
        &plugins_root(),
        &registrations::claude_config_dir().join("settings.json"),
        &claude,
        &NativeRunner,
    )
}
pub fn update_plugin(repo: Option<&Path>) -> Value {
    let Some(claude) = registrations::absolute_command("claude") else {
        return result("no-cli", "the claude CLI is not on PATH");
    };
    update_with(
        repo,
        &plugins_root(),
        &registrations::claude_config_dir().join("settings.json"),
        &claude,
        &NativeRunner,
    )
}
pub(super) fn update_with(
    _repo: Option<&Path>,
    root: &Path,
    settings: &Path,
    claude: &Path,
    runner: &impl Runner,
) -> Value {
    let outcome = (|| -> Result<Value, String> {
        let Some(record) = plugin_record(root)? else {
            return Ok(result(
                "not-installed",
                "pseudolife-memory@pseudolife-mcp is not installed; rerun native setup with Claude as a client",
            ));
        };
        let migrated = migrate_with(root, settings, claude, runner);
        let refreshed = runner.run(
            claude,
            &["plugin", "marketplace", "update", MARKETPLACE],
            None,
            Duration::from_secs(600),
            &[],
        );
        let failure = refreshed.code != 0
            || [
                "Failed to update marketplace",
                "Failed to refresh marketplace",
            ]
            .iter()
            .any(|s| refreshed.text().contains(s));
        let mut report = install_from_clone(root, &record, claude, runner)?;
        report["marketplace_source"] = migrated["state"].clone();
        report["marketplace_update"] = json!(if failure { "failed" } else { "ok" });
        if failure || migrated["state"] == "failed" {
            report["state"] = json!("failed");
            report["detail"] = json!(format!(
                "marketplace refresh did not verify (exit {}); {}; the cache may match an older clone; installed plugin was retained; fix the marketplace source, then run pseudolife-mcp update --clients-only; {}",
                refreshed.code,
                migrated["detail"].as_str().unwrap_or(""),
                report["detail"].as_str().unwrap_or("")
            ));
        }
        Ok(report)
    })();
    outcome.unwrap_or_else(|detail| result("failed", detail))
}
fn install_from_clone(
    root: &Path,
    record: &Value,
    claude: &Path,
    runner: &impl Runner,
) -> Result<Value, String> {
    let version = record["version"].as_str().unwrap_or("");
    let cache = PathBuf::from(record["installPath"].as_str().unwrap_or(""));
    let clone = clone_dir(root)?;
    if !clone.join(".claude-plugin/plugin.json").is_file() {
        return Ok(result(
            "failed",
            format!("no marketplace clone; run claude plugin marketplace add {HTTPS}"),
        ));
    }
    let offered = tree_files(&clone)?;
    if cache.is_dir() && tree_files(&cache)? == offered {
        return Ok(result(
            &format!("current:{version}"),
            format!("cache matches the marketplace clone (v{version})"),
        ));
    }
    let scope = record["scope"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or("user");
    let project = record["projectPath"].as_str().map(Path::new);
    if ["project", "local"].contains(&scope) && project.is_none() {
        return Ok(result(
            "failed",
            "project-scoped plugin names no project; run claude plugin update from that project; existing copy retained",
        ));
    }
    let output = runner.run(
        claude,
        &["plugin", "update", PLUGIN, "--scope", scope],
        if ["project", "local"].contains(&scope) {
            project
        } else {
            None
        },
        Duration::from_secs(600),
        &[],
    );
    let Some(updated) = plugin_record(root)? else {
        return Ok(result(
            "failed",
            "plugin record disappeared after update; rerun native client setup; running sessions keep their loaded copy",
        ));
    };
    let new_cache = PathBuf::from(updated["installPath"].as_str().unwrap_or(""));
    if output.code == 0 && new_cache.is_dir() && tree_files(&new_cache)? == offered {
        return Ok(result(
            &format!("refreshed:{}", updated["version"].as_str().unwrap_or("")),
            "plugin installed beside the copy running sessions loaded; new sessions use the refreshed cache",
        ));
    }
    Ok(result(
        "failed",
        format!(
            "plugin update did not leave a cache matching the clone (exit {}); no uninstall was attempted; run claude plugin marketplace update pseudolife-mcp, then pseudolife-mcp update --clients-only",
            output.code
        ),
    ))
}

pub(super) fn normalized_file(path: &Path) -> Result<Vec<u8>, String> {
    normalized(path)
}
