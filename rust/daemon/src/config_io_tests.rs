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

#[test]
fn maintenance_and_presentation_patch_is_applied_without_restart() {
    let dir = scratch("live-maintenance");
    let patch = json!({
        "memory.surprise_threshold": 0.9,
        "memory.compaction.enabled": false,
        "memory.compaction.keep_per_slot": 1,
        "memory.compaction.min_age_days": 7,
        "memory.retrieval_log.retention_days": 30,
        "memory.lessons.signal_retention_days": 60,
        "memory.dream.runs_keep": 5
    });
    let service = crate::service::Service::new(
        Config::default(),
        crate::config::DaemonEnv::from_env(&no_env).unwrap(),
        dir.clone(),
        String::new(),
        false,
    );
    let boot = service.runtime_config();
    let answer = write_config(&service, &no_env, &patch).unwrap();
    let paths: Vec<_> = patch.as_object().unwrap().keys().cloned().collect();
    assert_eq!(answer["applied"], json!(paths));
    assert_eq!(answer["restart_required"], json!([]));
    let live = service.runtime_config();
    assert_eq!(boot.memory.surprise_threshold, 0.0);
    for (path, value) in patch.as_object().unwrap() {
        assert!(py_eq(&setting(&live, path).unwrap(), value), "{path}");
        assert_eq!(
            running(&live, knob(path).unwrap()),
            coerce(knob(path).unwrap(), value).unwrap(),
            "{path}"
        );
    }
    let _ = std::fs::remove_dir_all(dir);
}

fn live_service(dir: std::path::PathBuf) -> crate::service::Service {
    crate::service::Service::new(
        Config::default(),
        crate::config::DaemonEnv::from_env(&no_env).unwrap(),
        dir,
        String::new(),
        false,
    )
}

#[test]
fn failed_config_persistence_never_changes_the_running_snapshot() {
    let dir = scratch("live-failure");
    std::fs::create_dir(dir.join("config.yaml")).unwrap();
    let service = live_service(dir.clone());
    let before = service.runtime_config();
    assert!(
        write_config(
            &service,
            &no_env,
            &json!({"memory.surprise_threshold": 0.9})
        )
        .is_err()
    );
    assert!(std::sync::Arc::ptr_eq(&before, &service.runtime_config()));
    assert_eq!(service.runtime_config().memory.surprise_threshold, 0.0);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn live_patch_leaves_unimplemented_and_restart_settings_at_their_boot_values() {
    let dir = scratch("live-mixed");
    let service = live_service(dir.clone());
    let before = service.runtime_config();
    let patch = json!({"memory.surprise_threshold": 0.9,
                       "memory.top_k": 9, "memory.reranker.top_n": 30});
    let answer = write_config(&service, &no_env, &patch).unwrap();
    assert_eq!(answer["applied"], json!(["memory.surprise_threshold"]));
    assert_eq!(
        answer["restart_required"],
        json!(["memory.top_k", "memory.reranker.top_n"])
    );
    let after = service.runtime_config();
    assert_eq!(after.memory.top_k, before.memory.top_k);
    assert_eq!(after.memory.reranker.top_n, before.memory.reranker.top_n);
    assert_eq!(after.memory.surprise_threshold, 0.9);
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn concurrent_config_patches_preserve_both_saved_and_running_changes() {
    let dir = scratch("live-concurrent");
    let service = std::sync::Arc::new(live_service(dir.clone()));
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let threads: Vec<_> = [
        json!({"memory.surprise_threshold": 0.9}),
        json!({"memory.compaction.keep_per_slot": 1}),
    ]
    .into_iter()
    .map(|patch| {
        let service = service.clone();
        let barrier = barrier.clone();
        std::thread::spawn(move || {
            barrier.wait();
            write_config(&service, &no_env, &patch).unwrap()
        })
    })
    .collect();
    for thread in threads {
        assert_eq!(thread.join().unwrap()["restart_required"], json!([]));
    }
    let saved = crate::config::load(&dir.join("config.yaml")).unwrap();
    let live = service.runtime_config();
    assert_eq!(saved.memory.surprise_threshold, 0.9);
    assert_eq!(live.memory.surprise_threshold, 0.9);
    assert_eq!(saved.memory.compaction_keep_per_slot, 1);
    assert_eq!(live.memory.compaction_keep_per_slot, 1);
    let _ = std::fs::remove_dir_all(dir);
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

/// v59: `coordination.not_owner_principals` is refused as protected, like
/// `coordination.maintainer*` (tests/test_maintainer_web.py pins Python's).
#[test]
fn protected_paths_include_the_owner_list() {
    for path in [
        "coordination.maintainer",
        "coordination.maintainer.rp_id",
        "coordination.not_owner_principals",
        "Coordination.Not_Owner_Principals",
        " coordination.not_owner_principals.0",
    ] {
        assert!(protected(path), "{path}");
    }
    for path in [
        "coordination",
        "coordination.not_owner",
        "coordination.allowed_principals",
    ] {
        assert!(!protected(path), "{path}");
    }
}
