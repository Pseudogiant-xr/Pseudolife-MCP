#[path = "../../../../rust/shim/src/lifecycle_executable.rs"]
mod resolver;

use serde_json::{Value, json};
use std::{
    ffi::OsStr,
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

struct Home(PathBuf);

impl Home {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("which-comparison-{}-{nonce}", std::process::id()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn file(&self, name: impl AsRef<Path>) -> PathBuf {
        let path = self.0.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, b"disposable executable fixture").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        path
    }
}

impl Drop for Home {
    fn drop(&mut self) {
        // Only the fresh directory this process created is owned by the comparison.
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn compare(rows: &mut Vec<Value>, id: &str, program: impl AsRef<OsStr>, expected: Option<PathBuf>) {
    let old = which::which(program.as_ref()).ok();
    let new = resolver::find_executable(program.as_ref()).ok();
    assert_eq!(old, expected, "real which 8.0.6: {id}");
    assert_eq!(new, expected, "production resolver: {id}");
    rows.push(json!({"id": id, "relation": "public_api_equal", "accepted": expected.is_some(), "passed": true}));
}

fn child(group: &str, home: &Home) -> Vec<Value> {
    let mut rows = Vec::new();
    let suffix = if cfg!(windows) { ".EXE" } else { "" };
    let first = home.file(format!("first/tool{suffix}"));
    home.file(format!("second/tool{suffix}"));
    match group {
        "ordinary" => {
            compare(&mut rows, "ordered-path", "tool", Some(first.clone()));
            compare(&mut rows, "absolute-path", &first, Some(first.clone()));
            compare(
                &mut rows,
                "relative-command",
                Path::new("first").join(format!("tool{suffix}")),
                Some(first),
            );
            compare(&mut rows, "missing-command", "absent", None);
            compare(&mut rows, "directory-rejected", &home.0, None);
        }
        "relative-path" => compare(&mut rows, "relative-path-entry", "tool", Some(first)),
        "empty-path" => {
            let current = home.file(format!("tool{suffix}"));
            let expected = if cfg!(windows) { first } else { current };
            compare(&mut rows, "empty-path-entry-policy", "tool", Some(expected));
        }
        "missing-path" => {
            compare(&mut rows, "missing-path-rejects-bare", "tool", None);
            compare(
                &mut rows,
                "missing-path-allows-absolute",
                &first,
                Some(first.clone()),
            );
        }
        "tilde" => compare(&mut rows, "standalone-home-component", "tool", Some(first)),
        "literal-tilde" => {
            let expected = home.file(Path::new("~other").join(format!("tool{suffix}")));
            compare(&mut rows, "literal-tilde-prefix", "tool", Some(expected));
        }
        #[cfg(unix)]
        "unix-permissions" => {
            use std::os::unix::fs::{PermissionsExt, symlink};
            let denied = home.file("denied");
            fs::set_permissions(&denied, fs::Permissions::from_mode(0o600)).unwrap();
            compare(&mut rows, "unix-execute-denied", &denied, None);
            let link = home.0.join("executable-link");
            symlink(&first, &link).unwrap();
            compare(&mut rows, "unix-file-symlink", &link, Some(link.clone()));
            let broken = home.0.join("broken-link");
            symlink(home.0.join("absent"), &broken).unwrap();
            compare(&mut rows, "unix-dangling-symlink", &broken, None);
            let denied_link = home.0.join("denied-link");
            symlink(&denied, &denied_link).unwrap();
            compare(&mut rows, "unix-denied-target-symlink", &denied_link, None);
        }
        #[cfg(unix)]
        "unix-non-unicode" => {
            use std::{ffi::OsString, os::unix::ffi::OsStringExt};
            let expected =
                home.file(PathBuf::from(OsString::from_vec(b"bin-\xff".to_vec())).join("tool"));
            compare(&mut rows, "unix-non-unicode-path", "tool", Some(expected));
            let name = OsString::from_vec(b"tool-\xff".to_vec());
            let expected = home.file(&name);
            compare(
                &mut rows,
                "unix-non-unicode-name",
                &expected,
                Some(expected.clone()),
            );
        }
        #[cfg(windows)]
        "windows-extensions" => {
            let exe = home.file("first/Mixed.ExE");
            let cmd = home.file("first/Mixed.CmD");
            compare(
                &mut rows,
                "windows-suffix-order-and-casing",
                "mixed",
                Some(exe),
            );
            compare(&mut rows, "windows-known-extension", "mixed.cmd", Some(cmd));
            let raw = home.file("first/raw.odd");
            home.file("first/raw.odd.CMD");
            compare(
                &mut rows,
                "windows-raw-extension-first",
                "raw.odd",
                Some(raw),
            );
            let appended = home.file("first/extra.odd.CMD");
            compare(
                &mut rows,
                "windows-unknown-extension-appends",
                "extra.odd",
                Some(appended),
            );
            home.file("first/known.CMD.CMD");
            compare(
                &mut rows,
                "windows-known-extension-not-appended",
                "known.CMD",
                None,
            );
        }
        #[cfg(windows)]
        "windows-extensionless" => {
            let native = home.0.join("first/native");
            fs::copy(std::env::current_exe().unwrap(), &native).unwrap();
            home.file("first/native.CMD");
            compare(
                &mut rows,
                "windows-extensionless-native-first",
                "native",
                Some(native),
            );
            home.file("first/text");
            let cmd = home.file("first/text.CMD");
            compare(
                &mut rows,
                "windows-extensionless-text-rejected",
                "text",
                Some(cmd),
            );
        }
        #[cfg(windows)]
        "windows-pathext-policy" => {
            let initial = home.file("first/initial.CMD");
            let later = home.file("first/later.CMD");
            compare(
                &mut rows,
                "windows-pathext-initial",
                "initial",
                Some(initial.clone()),
            );
            let owned = |name| {
                which::WhichConfig::new_with_sys(which::sys::RealSys)
                    .binary_name(name)
                    .first_result()
                    .ok()
            };
            assert_eq!(owned("initial".into()), Some(initial));
            // SAFETY: This disposable child is single-threaded and owns its environment.
            unsafe {
                std::env::set_var("PATHEXT", ".EXE");
            }
            compare(
                &mut rows,
                "windows-public-api-rereads-pathext",
                "later",
                None,
            );
            assert_eq!(owned("later".into()), Some(later));
            assert!(resolver::find_executable("later").is_err());
            rows.push(json!({"id": "windows-owned-realsys-cache-difference", "relation": "intentional_owned_config_difference", "old_owned_config_accepts": true, "replacement_accepts": false, "passed": true}));
        }
        #[cfg(windows)]
        "windows-non-unicode-pathext" => {
            use std::{ffi::OsString, os::windows::ffi::OsStringExt};
            home.file("first/initial.CMD");
            // SAFETY: This disposable child is single-threaded and owns its environment.
            unsafe {
                std::env::set_var("PATHEXT", OsString::from_wide(&[0xd800]));
            }
            compare(
                &mut rows,
                "windows-non-unicode-pathext-empty",
                "initial",
                None,
            );
        }
        #[cfg(windows)]
        "windows-links" => {
            use std::os::windows::fs::symlink_file;
            use std::os::windows::process::CommandExt;
            let link = home.0.join("file-link.EXE");
            match symlink_file(&first, &link) {
                Ok(()) => compare(&mut rows, "windows-file-symlink", &link, Some(link.clone())),
                Err(error) => rows.push(json!({"id": "windows-file-symlink", "relation": "unavailable", "creation_error": error.raw_os_error(), "passed": false})),
            }
            let target = home.0.join("directory-target");
            fs::create_dir(&target).unwrap();
            for (name, id) in [
                ("junction.CMD", "windows-directory-junction"),
                ("broken.CMD", "windows-dangling-junction"),
            ] {
                let junction = home.0.join(name);
                let command =
                    PathBuf::from(std::env::var_os("SystemRoot").unwrap()).join("System32/cmd.exe");
                let output = Command::new(command)
                    .args(["/D", "/S", "/C"])
                    .raw_arg(format!(
                        "\"mklink /J \"{}\" \"{}\"\"",
                        junction.display(),
                        target.display()
                    ))
                    .output()
                    .unwrap();
                assert!(
                    output.status.success(),
                    "owned junction creation failed: {}",
                    String::from_utf8_lossy(&output.stderr)
                );
                if name == "broken.CMD" {
                    fs::remove_dir(&target).unwrap();
                }
                assert!(
                    fs::symlink_metadata(&junction)
                        .unwrap()
                        .file_type()
                        .is_symlink()
                );
                compare(&mut rows, id, &junction, Some(junction.clone()));
            }
        }
        _ => panic!("unknown comparison group: {group}"),
    }
    rows
}

fn run(smoke: bool, selected: Option<&str>) -> Value {
    let mut groups = vec!["ordinary"];
    if !smoke {
        groups.extend([
            "relative-path",
            "empty-path",
            "missing-path",
            "tilde",
            "literal-tilde",
        ]);
        #[cfg(unix)]
        groups.extend(["unix-permissions", "unix-non-unicode"]);
        #[cfg(windows)]
        groups.extend([
            "windows-extensions",
            "windows-extensionless",
            "windows-pathext-policy",
            "windows-non-unicode-pathext",
            "windows-links",
        ]);
    }
    if let Some(selected) = selected {
        assert!(groups.contains(&selected), "unknown comparison group");
        groups.retain(|group| *group == selected);
    }
    let mut rows = Vec::new();
    for group in groups {
        let home = Home::new();
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args(["--child", group])
            .arg(&home.0)
            .env_clear()
            .env("HOME", &home.0)
            .env("USERPROFILE", &home.0)
            .current_dir(&home.0);
        #[cfg(windows)]
        if let Some(root) = std::env::var_os("SystemRoot") {
            command.env("SystemRoot", root);
        }
        let entries = match group {
            "relative-path" => vec![PathBuf::from("first")],
            "empty-path" => vec![PathBuf::new(), home.0.join("first")],
            "tilde" => vec![PathBuf::from("~/first")],
            "literal-tilde" => vec![PathBuf::from("~other")],
            #[cfg(unix)]
            "unix-non-unicode" => {
                use std::{ffi::OsString, os::unix::ffi::OsStringExt};
                vec![home.0.join(OsString::from_vec(b"bin-\xff".to_vec()))]
            }
            _ => vec![home.0.join("first"), home.0.join("second")],
        };
        if group != "missing-path" {
            command.env("PATH", std::env::join_paths(entries).unwrap());
        }
        #[cfg(windows)]
        command.env(
            "PATHEXT",
            if group == "windows-pathext-policy" {
                ".CMD"
            } else {
                "bad;;.EXE;.CMD"
            },
        );
        let output = command.output().unwrap();
        assert!(
            output.status.success(),
            "comparison {group} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let child_rows: Vec<Value> = serde_json::from_slice(&output.stdout).unwrap();
        rows.extend(child_rows);
        let owned_path = home.0.clone();
        drop(home);
        assert!(!owned_path.exists(), "owned fixture was not removed");
    }
    json!({"schema": 1, "oracle_crate": "which", "oracle_version": "8.0.6", "platform": std::env::consts::OS, "smoke_only": smoke, "selected_group": selected, "owned_fixture_cleanup": true, "rows": rows})
}

fn main() {
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    if args.first().is_some_and(|value| value == "--child") {
        let home = std::mem::ManuallyDrop::new(Home(PathBuf::from(&args[2])));
        // The parent owns this fixture and verifies cleanup after the child exits.
        let rows = child(args[1].to_str().unwrap(), &home);
        println!("{}", serde_json::to_string(&rows).unwrap());
    } else {
        let selected = if args.first().is_some_and(|value| value == "--group") {
            Some(args[1].to_str().unwrap())
        } else {
            None
        };
        assert!(
            args.is_empty() || args == ["--smoke"] || (selected.is_some() && args.len() == 2),
            "use no arguments, --smoke or --group NAME"
        );
        println!(
            "{}",
            serde_json::to_string(&run(args == ["--smoke"], selected)).unwrap()
        );
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn production_resolver_and_actual_crate_are_linked() {
        let current = std::env::current_exe().unwrap();
        assert_eq!(
            which::which(&current).unwrap(),
            super::resolver::find_executable(&current).unwrap()
        );
    }
}
