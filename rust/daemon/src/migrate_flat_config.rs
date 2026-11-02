//! Pure configuration planning for the CLI's opt-in flat migration.
//! Reuses the Console's PyYAML constructor and emitter; never opens a bank.

use crate::config::Node;
use crate::config_io::{yaml_dump, yaml_load};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

fn quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
    {
        value.into()
    } else {
        format!("'{}'", value.replace('\'', "'\"'\"'"))
    }
}

pub(crate) fn pending_refusal(
    marker: &Value,
    path: &Path,
    bands: &[crate::config::StartupBandSpec],
) -> Result<(), String> {
    let capacity = marker["capacity"].as_u64().filter(|n| *n >= 5250).ok_or("flat migration marker is invalid; keep the daemon stopped and restore the bank and config from the migration backup pair")?;
    if !(bands.len() == 1
        && bands[0].name == "flat"
        && bands[0].max_entries as u64 >= capacity
        && bands[0].retention_policy == "balanced")
    {
        return Err(format!(
            "this bank was migrated but the loaded config is not the matching flat layout; restart with the migrated config or restore bank and config together: use your rehearsed external recovery tooling for this same bank AND config; bank dump {}; state archive {}; no automatic restore is available",
            quote(marker["backup"]["dump"].as_str().unwrap_or("<dump>")),
            quote(marker["backup"]["state"].as_str().unwrap_or("<archive>"))
        ));
    }
    if marker["complete"] == true {
        return Ok(());
    }
    if std::fs::read(path).is_ok_and(|raw| {
        format!("{:x}", Sha256::digest(raw)) == marker["target_sha256"].as_str().unwrap_or("")
    }) {
        return Ok(());
    }
    Err(format!(
        "flat migration config finalization is pending; keep the daemon stopped and run migrate-flat --apply; restore bank and config together if needed: use your rehearsed external recovery tooling for this same bank AND config; bank dump {}; state archive {}; no automatic restore is available",
        quote(marker["backup"]["dump"].as_str().unwrap_or("<dump>")),
        quote(marker["backup"]["state"].as_str().unwrap_or("<archive>"))
    ))
}

pub(crate) async fn require_config_finalized(
    client: &tokio_postgres::Client,
    data_dir: &Path,
    bands: &[crate::config::StartupBandSpec],
) -> Result<(), String> {
    let row = client
        .query_opt(
            "SELECT value FROM public.meta WHERE key='flat_band_migration_v1'",
            &[],
        )
        .await
        .map_err(|_| "flat migration marker could not be read; retry startup")?;
    if let Some(row) = row {
        let marker: Value = row.get(0);
        let path = std::env::var_os("PSEUDOLIFE_MCP_CONFIG")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| data_dir.join("config.yaml"));
        pending_refusal(&marker, &path, bands)?;
    }
    Ok(())
}

fn mapping<'a>(node: &'a Node, path: &str) -> Result<&'a [(Node, Node)], String> {
    match node {
        Node::Map(fields) => Ok(fields),
        _ => Err(format!(
            "{path} must be a mapping; restore a valid config and retry migrate-flat --apply"
        )),
    }
}

fn get<'a>(fields: &'a [(Node, Node)], name: &str) -> Option<&'a Node> {
    fields
        .iter()
        .find_map(|(key, value)| matches!(key, Node::Str(s) if s == name).then_some(value))
}

fn put(fields: &mut Vec<(Node, Node)>, name: &str, value: Node) {
    if let Some((_, old)) = fields
        .iter_mut()
        .find(|(key, _)| matches!(key, Node::Str(s) if s == name))
    {
        *old = value;
    } else {
        fields.push((Node::Str(name.into()), value));
    }
}

fn string<'a>(value: Option<&'a Node>, default: &'static str) -> Option<&'a str> {
    match value {
        None => Some(default),
        Some(Node::Str(value)) => Some(value),
        _ => None,
    }
}

fn map(fields: Vec<(&str, Node)>) -> Node {
    Node::Map(
        fields
            .into_iter()
            .map(|(key, value)| (Node::Str(key.into()), value))
            .collect(),
    )
}

pub(crate) fn plan(text: &str, entries: i64) -> Result<Value, String> {
    let config = yaml_load::load_migrating(text).map_err(|error| {
        if [yaml_load::SHARED, yaml_load::BIG_INT, yaml_load::UNWRITABLE].contains(&error.as_str()) {
            error
        } else {
            "config.yaml is not valid UTF-8 YAML; restore a valid config and retry migrate-flat --apply".into()
        }
    })?.filter(|node| !matches!(node, Node::Null)).unwrap_or(Node::Map(vec![]));
    let empty = Node::Map(vec![]);
    let mut root = mapping(&config, "config.yaml")?.to_vec();
    let mut memory = mapping(get(&root, "memory").unwrap_or(&empty), "memory")?.to_vec();
    let miras = mapping(get(&memory, "miras").unwrap_or(&empty), "memory.miras")?;
    let preset = string(get(miras, "preset"), "flat");
    if !matches!(
        preset,
        Some("flat" | "continuum" | "titans" | "moneta" | "yaad" | "memora" | "custom")
    ) {
        return Err(
            "memory.miras.preset is unknown; restore a valid config and retry migrate-flat --apply"
                .into(),
        );
    }
    let mut capacity = 5250i64;
    let mut flat = preset == Some("flat");
    if preset == Some("custom") {
        let Some(Node::Seq(bands)) = get(miras, "bands") else {
            return Err("memory.miras.bands must be a nonempty list for custom; restore a valid config and retry migrate-flat --apply".into());
        };
        if bands.is_empty() {
            return Err("memory.miras.bands must be a nonempty list for custom; restore a valid config and retry migrate-flat --apply".into());
        }
        capacity = 0;
        for band in bands {
            let fields = mapping(band, "memory.miras.bands[]")?;
            let size = match get(fields, "max_entries") {
                None => 5000,
                Some(Node::Int(size)) if *size > 0 => *size,
                _ => return Err("memory.miras.bands[].max_entries must be a positive 64-bit integer; restore a valid config and retry migrate-flat --apply".into()),
            };
            let policy = string(get(fields, "retention_policy"), "balanced");
            if !matches!(
                policy,
                Some("balanced" | "recency_heavy" | "surprise_heavy")
            ) {
                return Err("memory.miras.bands[].retention_policy is unknown; restore a valid config and retry migrate-flat --apply".into());
            }
            capacity = capacity
                .checked_add(size)
                .ok_or("aggregate band capacity exceeds a 64-bit integer; nothing changed")?;
            flat = bands.len() == 1
                && string(get(fields, "name"), "band") == Some("flat")
                && policy == Some("balanced")
                && size >= entries.max(5250);
        }
    } else {
        flat &= entries <= 5250;
    }
    capacity = capacity.max(entries).max(5250);
    let target = if capacity == 5250 {
        map(vec![("preset", Node::Str("flat".into()))])
    } else {
        map(vec![
            ("preset", Node::Str("custom".into())),
            (
                "bands",
                Node::Seq(vec![map(vec![
                    ("name", Node::Str("flat".into())),
                    ("max_entries", Node::Int(capacity)),
                    ("retention_policy", Node::Str("balanced".into())),
                    ("update_interval", Node::Int(1000000000)),
                    ("promotion_access_count", Node::Int(1000000000)),
                    ("promotion_surprise", Node::Float(1.1)),
                ])]),
            ),
        ])
    };
    put(&mut memory, "miras", target);
    put(&mut root, "memory", Node::Map(memory));
    Ok(
        json!({"yaml": yaml_dump::safe_dump_pyyaml(&Node::Map(root)), "capacity": capacity, "flat": flat}),
    )
}

pub(crate) fn command(args: &[std::ffi::OsString]) -> i32 {
    let entries = match args {
        [count] => count
            .to_str()
            .and_then(|s| s.parse::<i64>().ok())
            .filter(|n| *n >= 0),
        _ => None,
    };
    let Some(entries) = entries else {
        eprintln!(
            "migrate-flat-config requires a nonnegative entry count and config.yaml on stdin"
        );
        return 2;
    };
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        eprintln!("config.yaml is not readable UTF-8; nothing changed");
        return 1;
    }
    match plan(text.trim_start_matches('\u{feff}'), entries) {
        Ok(result) => match writeln!(std::io::stdout().lock(), "{result}") {
            Ok(()) => 0,
            Err(_) => 1,
        },
        Err(reason) => {
            eprintln!("migrate-flat refused: {reason}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pending_marker_refuses_before_hydration() {
        let bands = crate::config::Config::default().memory.band_specs;
        let marker = json!({"capacity": 5250, "complete": false, "target_sha256": "target", "backup": {"dump": "bank.sql.gz", "state": "state.tgz"}});
        let error =
            pending_refusal(&marker, Path::new("missing-fixture-config.yaml"), &bands).unwrap_err();
        assert!(error.contains("migrate-flat --apply"));
        assert!(error.contains(
            "external recovery tooling for this same bank AND config; bank dump bank.sql.gz; state archive state.tgz"
        ));
        assert!(!error.contains("ops/restore"));
        let mut complete = marker;
        complete["complete"] = true.into();
        assert!(
            pending_refusal(&complete, Path::new("missing-fixture-config.yaml"), &bands).is_ok()
        );
        assert!(
            pending_refusal(&complete, Path::new("missing-fixture-config.yaml"), &[])
                .unwrap_err()
                .contains("loaded config")
        );
    }

    #[test]
    fn collapse_preserves_yaml11_values_and_capacity() {
        let result = plan(
            "enabled: yes\nmemory:\n  miras:\n    preset: continuum\n",
            6001,
        )
        .unwrap();
        assert_eq!(result["capacity"], 6001);
        assert_eq!(result["flat"], false);
        let text = result["yaml"].as_str().unwrap();
        assert!(text.starts_with("enabled: true\n"));
        assert!(text.contains("max_entries: 6001"));
        assert!(text.contains("retention_policy: balanced"));
    }

    #[test]
    fn custom_aggregate_and_unknown_policy() {
        let text = "memory:\n  miras:\n    preset: custom\n    bands:\n    - {name: a, max_entries: 7000, retention_policy: surprise_heavy}\n    - {name: b, max_entries: 9000}\n";
        assert_eq!(plan(text, 0).unwrap()["capacity"], 16000);
        assert!(plan(&text.replace("surprise_heavy", "typo"), 0).is_err());
        assert!(plan(&text.replace("7000", "-1"), 0).is_err());
    }

    #[test]
    fn migration_hash_bytes_use_yaml11_string_emission() {
        let result = plan(
            "model: 1e5\nother: 0o17\nmemory: {miras: {preset: continuum}}\n",
            0,
        )
        .unwrap();
        assert_eq!(
            result["yaml"],
            "model: 1e5\nother: 0o17\nmemory:\n  miras:\n    preset: flat\n"
        );
        let node = Node::Str("1e5".into());
        assert_eq!(yaml_dump::safe_dump(&node), "'1e5'\n");
        assert_eq!(yaml_dump::safe_dump_pyyaml(&node), "1e5\n...\n");
    }

    #[test]
    fn migration_preserves_non_specific_scalar_tags() {
        let result = plan(
            "updates: {check_releases: ! 'on'}\nmemory: {miras: {preset: continuum}}\n",
            0,
        )
        .unwrap();
        assert_eq!(
            result["yaml"],
            "updates:\n  check_releases: true\nmemory:\n  miras:\n    preset: flat\n"
        );
    }
}
