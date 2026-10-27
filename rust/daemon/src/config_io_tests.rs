use super::*;
use crate::pyjson::dumps;

fn golden() -> Value {
    serde_json::from_str(KNOBS_JSON).expect("knob golden")
}

/// A golden input: JSON, with `{"$float": "inf"}` for an infinity.
fn input(v: &Value) -> Value {
    match v.get("$float").and_then(Value::as_str) {
        Some("inf") => serde_json::from_str("1e400").unwrap(),
        Some("-inf") => serde_json::from_str("-1e400").unwrap(),
        _ => v.clone(),
    }
}

/// No environment: the tests never read (or overwrite) the user's own
/// `PSEUDOLIFE_MCP_CONFIG` file.
fn no_env(_: &str) -> Option<String> {
    None
}

fn scratch(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("pl-config-io-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn every_knob_defaults_to_the_python_daemons_value() {
    let g = golden();
    let defaults = g["defaults"].as_object().unwrap();
    let cfg = Config::default();
    assert_eq!(knobs().len(), defaults.len());
    for knob in knobs() {
        let want = &defaults[knob.path()];
        // Exact text: an int knob serves `365`, never `365.0`.
        assert_eq!(dumps(&running(&cfg, knob)), dumps(want), "{}", knob.path());
    }
}

#[test]
fn coerce_matches_python() {
    let g = golden();
    for case in g["coerce"].as_array().unwrap() {
        let path = case["path"].as_str().unwrap();
        let value = input(&case["input"]);
        let got = coerce(knob(path).unwrap(), &value);
        let label = format!("{path} <- {}", dumps(&case["input"]));
        match case.get("ok") {
            Some(want) => assert_eq!(dumps(&got.expect(&label)), dumps(want), "{label}"),
            None => {
                let text = case["text"].as_str().unwrap().to_string();
                let want = match case["error"].as_str().unwrap() {
                    "ValueError" => Refused::Value(text),
                    "TypeError" => Refused::Type(text),
                    "OverflowError" => Refused::Overflow(text),
                    other => panic!("{other}"),
                };
                assert_eq!(got, Err(want), "{label}");
            }
        }
    }
}

#[test]
fn read_config_serves_the_running_values_and_restart_knobs_saved_values() {
    let g = golden();
    let boot_dir = scratch("boot");
    let boot = boot_dir.join("boot.yaml");
    std::fs::write(&boot, g["boot"].as_str().unwrap()).unwrap();
    let cfg = crate::config::load(&boot).expect("boot config loads");
    for case in g["read"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let dir = scratch(name);
        std::fs::write(dir.join("config.yaml"), case["saved"].as_str().unwrap()).unwrap();
        let got = read_config_with(&cfg, &no_env, &dir, &HashSet::new());
        match case.get("ok") {
            Some(want) => {
                let mut got = got.expect(name);
                let path = got.as_object_mut().unwrap().shift_remove("config_path");
                assert_eq!(
                    path,
                    Some(json!(config_path_for(&no_env, &dir))),
                    "{name}: config_path"
                );
                assert_eq!(dumps(&got), dumps(want), "{name}");
            }
            None => assert_eq!(got, Err(case["text"].as_str().unwrap().into()), "{name}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
    let _ = std::fs::remove_dir_all(&boot_dir);
}

#[test]
fn a_knob_saved_through_this_process_shows_its_saved_value() {
    // Option C: a live knob written here is not applied until a restart,
    // so the editor sees what the next start will use.
    let dir = scratch("pending");
    std::fs::write(dir.join("config.yaml"), "memory:\n  top_k: 12\n").unwrap();
    let cfg = Config::default();
    let top_k = |v: &Value| -> Value {
        v["groups"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|g| g["knobs"].as_array().unwrap().iter())
            .find(|k| k["path"] == "memory.top_k")
            .unwrap()
            .clone()
    };
    let mut pending = HashSet::new();
    let before = top_k(&read_config_with(&cfg, &no_env, &dir, &pending).unwrap());
    assert_eq!(before.get("saved"), None, "not restart, not pending");
    pending.insert("memory.top_k".to_string());
    let after = top_k(&read_config_with(&cfg, &no_env, &dir, &pending).unwrap());
    assert_eq!(after["value"], json!(8));
    assert_eq!(after["saved"], json!(12));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_pending_live_knob_never_escapes_an_overflow() {
    // Python coerces only a restart knob's saved value, so an infinite
    // value under a live knob saved here is shown as no `saved`, not 500.
    let dir = scratch("pending-overflow");
    std::fs::write(dir.join("config.yaml"), "memory:\n  top_k: .inf\n").unwrap();
    let pending: HashSet<String> = ["memory.top_k".to_string()].into();
    let got = read_config_with(&Config::default(), &no_env, &dir, &pending).expect("200");
    let knob = got["groups"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|g| g["knobs"].as_array().unwrap().iter())
        .find(|k| k["path"] == "memory.top_k")
        .unwrap();
    assert_eq!(knob.get("saved"), None);
    // A restart knob's overflow is Python's 500.
    std::fs::write(
        dir.join("config.yaml"),
        "memory:\n  reranker:\n    top_n: .inf\n",
    )
    .unwrap();
    assert_eq!(
        read_config_with(&Config::default(), &no_env, &dir, &HashSet::new()),
        Err("cannot convert float infinity to integer".into())
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn config_path_follows_pathlib() {
    let dir = std::path::Path::new(".");
    // `Path(".") / "config.yaml"`.
    assert_eq!(config_path_for(&no_env, dir), "config.yaml");
    #[cfg(not(windows))]
    assert_eq!(
        config_path_for(&no_env, std::path::Path::new("/data//x/./")),
        "/data/x/config.yaml"
    );
    // Set and non-empty wins; empty falls back (config_io's truthiness).
    let set = |v: &'static str| move |_: &str| Some(v.to_string());
    #[cfg(not(windows))]
    assert_eq!(
        config_path_for(&set("/etc//pl/c.yaml"), dir),
        "/etc/pl/c.yaml"
    );
    assert_eq!(config_path_for(&set(""), dir), "config.yaml");
}

/// Python rewrites these; this daemon refuses them, file untouched (the
/// declared rows: no exact rewrite of a timestamp, a shared node or an
/// integer past 64 bits here).
fn refused_here(name: &str) -> Option<&'static str> {
    match name {
        "timestamp-in-file" => Some(yaml_load::UNWRITABLE),
        "shared-anchor" => Some(yaml_load::SHARED),
        "big-int-in-file" => Some(yaml_load::BIG_INT),
        _ => None,
    }
}

/// The file this daemon writes where it quotes a string its own start-up
/// loader would read as another type (declared byte difference).
fn quoted_here(name: &str) -> Option<&'static str> {
    match name {
        "boot-ambiguous-string" => {
            Some("memory:\n  dream:\n    fallback_model: '0o17'\n    extractor_model: '1e5'\n")
        }
        _ => None,
    }
}

#[test]
fn write_config_matches_python() {
    let g = golden();
    for case in g["write"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let dir = scratch(&format!("write-{name}"));
        let cfg = dir.join("config.yaml");
        let before = case["before"].as_str();
        if let Some(text) = before {
            std::fs::write(&cfg, text).unwrap();
        }
        let pending = Mutex::new(HashSet::new());
        let got = write_config_with(&no_env, &dir, &case["patch"], &pending);
        let unchanged = || {
            assert_eq!(
                std::fs::read_to_string(&cfg).ok().as_deref(),
                before,
                "{name}: a refusal leaves the file as it was"
            );
        };
        if let Some(reason) = refused_here(name) {
            assert_eq!(got, Err(WriteError::Failed(reason.into())), "{name}");
            unchanged();
        } else if let Some(want) = case.get("ok") {
            let answer = got.unwrap_or_else(|e| panic!("{name}: {e:?}"));
            let file = std::fs::read_to_string(&cfg).unwrap().replace("\r\n", "\n");
            let want_file = quoted_here(name).unwrap_or_else(|| want["file"].as_str().unwrap());
            assert_eq!(file, want_file, "{name}: file");
            // This daemon starts on what it wrote.
            crate::config::load(&cfg).unwrap_or_else(|e| panic!("{name}: boot refuses: {e}"));
            // Every patched knob needs a restart here, in patch order.
            assert_eq!(answer["applied"], json!([]), "{name}");
            let strs = |v: &Value| -> Vec<String> {
                v.as_array()
                    .unwrap()
                    .iter()
                    .map(|p| p.as_str().unwrap().to_string())
                    .collect()
            };
            let paths = strs(&answer["restart_required"]);
            let order: Vec<String> = case["patch"].as_object().unwrap().keys().cloned().collect();
            assert_eq!(paths, order, "{name}: patch order");
            let (mut a, mut b) = (paths, strs(&want["paths"]));
            a.sort_unstable();
            b.sort_unstable();
            assert_eq!(a, b, "{name}: Python's applied and restart_required");
            assert_eq!(
                answer["config_path"],
                json!(config_path_for(&no_env, &dir)),
                "{name}"
            );
            match answer["backup"].as_str() {
                None => assert!(!want["backup"].as_bool().unwrap(), "{name}: backup"),
                Some(path) => {
                    assert!(want["backup"].as_bool().unwrap(), "{name}: backup");
                    let file = std::path::Path::new(path);
                    assert!(file.is_absolute(), "{name}: {path}");
                    let shown = file.file_name().unwrap().to_string_lossy().into_owned();
                    // config.yaml.<%Y%m%d-%H%M%S>.<8 random>.bak
                    let rest = shown.strip_prefix("config.yaml.").unwrap_or("");
                    assert!(
                        rest.ends_with(".bak")
                            && rest.len() == "20261027-120000.abcdefgh.bak".len(),
                        "{name}: {shown}"
                    );
                    assert_eq!(
                        std::fs::read_to_string(file).ok().as_deref(),
                        before,
                        "{name}"
                    );
                }
            }
            let left: Vec<String> = std::fs::read_dir(&dir)
                .unwrap()
                .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
                .filter(|n| n.ends_with(".tmp"))
                .collect();
            assert!(left.is_empty(), "{name}: temp files left {left:?}");
            assert_eq!(
                pending.lock().unwrap().len(),
                order.len(),
                "{name}: pending"
            );
        } else {
            let text = case["text"].as_str().unwrap().to_string();
            let want = if case["error"] == "ValueError" {
                WriteError::Invalid(text)
            } else {
                WriteError::Failed(text)
            };
            assert_eq!(got, Err(want), "{name}");
            unchanged();
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
