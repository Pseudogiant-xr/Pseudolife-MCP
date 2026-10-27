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
        let got = read_config(&cfg, &dir);
        match case.get("ok") {
            Some(want) => {
                let mut got = got.expect(name);
                let path = got.as_object_mut().unwrap().shift_remove("config_path");
                assert_eq!(
                    path,
                    Some(json!(config_path_for(&dir))),
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
fn config_path_follows_pathlib() {
    let dir = std::path::Path::new(".");
    // `Path(".") / "config.yaml"`.
    if std::env::var("PSEUDOLIFE_MCP_CONFIG").map_or(true, |v| v.is_empty()) {
        assert_eq!(config_path_for(dir), "config.yaml");
        #[cfg(not(windows))]
        assert_eq!(
            config_path_for(std::path::Path::new("/data//x/./")),
            "/data/x/config.yaml"
        );
    }
}
