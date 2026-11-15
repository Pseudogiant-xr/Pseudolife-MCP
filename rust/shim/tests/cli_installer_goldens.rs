//! The native installer steps answer as the Python helpers they replace.
//!
//! `tests/fixtures/installer_native_goldens.json` (repository root) records
//! what `ops/installer_state.py` and `ops/client_credentials.py` print, exit
//! with and leave on disk for each case; `tests/test_installer_native_goldens.py`
//! holds the Python helpers to the record and this test holds
//! `pseudolife-mcp installer state|credentials ...` to it.
use serde_json::Value;
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

fn goldens() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/installer_native_goldens.json");
    serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap()
}

struct Home(PathBuf);
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn home_forms(home: &str) -> Vec<String> {
    let mut forms = vec![
        home.to_owned(),
        home.replace('\\', "/"),
        serde_json::to_string(home)
            .unwrap()
            .trim_matches('"')
            .to_owned(),
    ];
    forms.sort_by_key(|f| std::cmp::Reverse(f.len()));
    forms.dedup();
    forms
}

/// The Python test's `placeholder`: the home, backup stamps and (for a case
/// that prints paths) separators normalized.
fn placeholder(text: &str, home: &str, paths: bool) -> String {
    let mut text = text.to_owned();
    for form in home_forms(home) {
        text = text.replace(&form, "{HOME}");
    }
    let marker = ".bak-pseudolife-";
    let mut out = String::new();
    let mut rest = text.as_str();
    while let Some(at) = rest.find(marker) {
        out.push_str(&rest[..at + marker.len()]);
        rest = &rest[at + marker.len()..];
        let stamp = rest.as_bytes();
        let shape = stamp.len() >= 22
            && stamp[..22].iter().enumerate().all(|(i, b)| match i {
                8 | 15 => *b == b'-',
                _ => b.is_ascii_digit(),
            });
        if shape {
            out.push_str("<stamp>");
            rest = &rest[22..];
        }
    }
    out.push_str(rest);
    if paths {
        out = out.replace("\\\\", "/").replace('\\', "/");
    }
    out
}

fn expand(value: &str, home: &str) -> String {
    value.replace("{HOME}", home)
}

fn run_case(case: &Value) -> Value {
    let root = std::env::temp_dir().join(format!(
        "installer-goldens-{}",
        uuid::Uuid::new_v4().simple()
    ));
    pseudolife_stdio::board::state::private_dir(&root).unwrap();
    let home = Home(root);
    let home_text = home.0.to_string_lossy().into_owned();
    if let Some(files) = case["files"].as_object() {
        for (name, text) in files {
            let target = home.0.join(name);
            fs::create_dir_all(target.parent().unwrap()).unwrap();
            fs::write(&target, expand(text.as_str().unwrap(), &home_text)).unwrap();
        }
    }
    let paths = case["paths"].as_bool().unwrap_or(false);
    fs::create_dir_all(home.0.join("empty-path")).unwrap();
    let mut steps = Vec::new();
    for step in case["steps"].as_array().unwrap() {
        let argv: Vec<String> = step["argv"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| expand(a.as_str().unwrap(), &home_text))
            .collect();
        let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
        command.env_clear();
        for key in ["SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        command
            .env("HOME", &home.0)
            .env("USERPROFILE", &home.0)
            .env("LOCALAPPDATA", home.0.join("Local"))
            .env("APPDATA", home.0.join("Roaming"))
            .env("PATH", home.0.join("empty-path"))
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1");
        if let Some(env) = case["env"].as_object() {
            for (key, value) in env {
                command.env(key, expand(value.as_str().unwrap(), &home_text));
            }
        }
        let stdin = step["stdin"].as_str().map(|s| expand(s, &home_text));
        command
            .arg("installer")
            .args(&argv)
            .current_dir(&home.0)
            .stdin(if stdin.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn().unwrap();
        if let Some(text) = stdin {
            child
                .stdin
                .take()
                .unwrap()
                .write_all(text.as_bytes())
                .unwrap();
        }
        let output = child.wait_with_output().unwrap();
        let text = |bytes: &[u8]| {
            placeholder(
                &String::from_utf8_lossy(bytes).replace("\r\n", "\n"),
                &home_text,
                paths,
            )
        };
        steps.push(serde_json::json!({
            "argv": step["argv"],
            "stdin": step["stdin"],
            "exit": output.status.code(),
            "stdout": text(&output.stdout),
            "stderr": if case["compare_stderr"] == false {
                Value::Null
            } else {
                Value::String(text(&output.stderr))
            },
        }));
    }
    let mut after = serde_json::Map::new();
    if let Some(files) = case["files_after"].as_object() {
        for name in files.keys() {
            let target = home.0.join(name);
            after.insert(
                name.clone(),
                match fs::read(&target) {
                    // Compared as text, as the Python test compares them.
                    Ok(bytes) => Value::String(placeholder(
                        &String::from_utf8_lossy(&bytes).replace("\r\n", "\n"),
                        &home_text,
                        paths,
                    )),
                    Err(_) => Value::Null,
                },
            );
        }
    }
    serde_json::json!({"steps": steps, "files_after": after})
}

#[test]
fn native_installer_steps_answer_as_the_recorded_python_helpers() {
    let mut differences = Vec::new();
    for case in goldens()["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let observed = run_case(case);
        if observed["steps"] != case["steps"] {
            differences.push(format!(
                "{name}: steps\n  recorded {}\n  native   {}",
                case["steps"], observed["steps"]
            ));
        }
        if case.get("files_after").is_some() && observed["files_after"] != case["files_after"] {
            differences.push(format!(
                "{name}: files\n  recorded {}\n  native   {}",
                case["files_after"], observed["files_after"]
            ));
        }
    }
    assert!(differences.is_empty(), "{}", differences.join("\n\n"));
}

#[test]
fn the_stamp_and_home_placeholders_match_the_python_test() {
    let home = if cfg!(windows) { r"C:\T\h" } else { "/t/h" };
    let text = format!("{home}/a.json.bak-pseudolife-20261114-081500-123456 and {home}");
    assert_eq!(
        placeholder(&text, home, true),
        "{HOME}/a.json.bak-pseudolife-<stamp> and {HOME}"
    );
}
