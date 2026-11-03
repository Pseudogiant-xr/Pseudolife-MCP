//! Exercise start's actual env_clear/cwd/child wiring without models or PostgreSQL.
use super::*;
use std::{ffi::OsString, fs};

struct Home(PathBuf);
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn node(name: &str) -> String {
    // libtest names omit the crate name; both the CLI and contract target include us.
    format!("{}::{name}", module_path!().split_once("::").unwrap().1)
}

#[test]
fn child_environment_capture() {
    if !std::env::args().any(|arg| arg == node("child_environment_capture")) {
        return;
    }
    let Some(config) = std::env::var_os("PSEUDOLIFE_MCP_CONFIG") else {
        return;
    };
    let root = PathBuf::from(config);
    if !root.with_file_name("capture-enabled").is_file() {
        return;
    }
    assert!(
        std::env::var_os("R2_PROOF_LOADER_ROOT").is_none(),
        "isolated capture child inherited the parent fixture sentinel"
    );
    let value = std::env::var_os("ORT_DYLIB_PATH");
    let bytes = value.as_ref().and_then(|path| fs::read(path).ok());
    fs::write(
        root.with_file_name("received.json"),
        serde_json::to_vec(&json!({"value": value, "bytes": bytes})).unwrap(),
    )
    .unwrap();
}

#[tokio::test]
async fn child_environment_start() {
    let Some(root) = std::env::var_os("R2_PROOF_LOADER_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let mut fixture = Fixture {
        root: root.join("private"),
        child: None,
        adapters: Vec::new(),
        digest_paths: Vec::new(),
        diagnostics: Default::default(),
    };
    state::private_dir(&fixture.root).unwrap();
    fs::write(fixture.root.join("capture-enabled"), b"synthetic fixture").unwrap();
    let result = tokio::time::timeout(
        Duration::from_secs(15),
        start(
            &root.join("bin/daemon.exe"),
            &root,
            "synthetic",
            &mut fixture,
        ),
    )
    .await;
    let received = fs::read(fixture.root.join("received.json"));
    fixture.close().await.unwrap();
    assert_eq!(result, Ok(Err("NativeDaemonStartupError")));
    fs::write(root.join("result.json"), received.unwrap()).unwrap();
}

#[tokio::test]
async fn runtime_library_reaches_the_isolated_child() {
    let home = Home(std::env::temp_dir().join(format!("r2-proof-loader-{}", uuid::Uuid::new_v4())));
    let root = &home.0;
    let bin = root.join("bin");
    let work = root.join("work");
    fs::create_dir_all(&bin).unwrap();
    fs::create_dir_all(work.join("runtime")).unwrap();
    // getcwd resolves /var to /private/var on macOS. Windows canonicalize
    // instead adds a verbatim prefix, so keep its ordinary drive spelling.
    #[cfg(unix)]
    let work = fs::canonicalize(work).unwrap();
    let daemon = bin.join("daemon.exe");
    // A compiled native trampoline passes one libtest filter to this executable;
    // no Python, compiler or model installation is needed on a stress runner.
    fs::write(&daemon, pseudolife_extractor_fixtures::STUB).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&daemon, fs::Permissions::from_mode(0o700)).unwrap();
    }
    fs::write(
        daemon.with_extension("python"),
        std::env::current_exe().unwrap().to_str().unwrap(),
    )
    .unwrap();
    fs::write(
        daemon.with_extension("script"),
        node("child_environment_capture"),
    )
    .unwrap();
    let library = work.join("runtime").join("library");
    fs::write(&library, b"caller library").unwrap();
    fs::write(work.join("library"), b"caller library").unwrap();
    let mut cases: Vec<(&str, Option<OsString>, Option<OsString>, bool)> = vec![
        ("unset", None, None, false),
        ("empty", Some("".into()), None, false),
        (
            "bare",
            Some("library".into()),
            Some("library".into()),
            false,
        ),
        (
            "cwd-relative",
            Some("runtime/library".into()),
            Some(library.clone().into_os_string()),
            true,
        ),
        (
            "absolute",
            Some(library.clone().into_os_string()),
            Some(library.clone().into_os_string()),
            true,
        ),
    ];
    #[cfg(windows)]
    {
        use std::path::{Component, Prefix};
        let Some(Component::Prefix(prefix)) = work.components().next() else {
            panic!("drive fixture")
        };
        let Prefix::Disk(drive) = prefix.kind() else {
            panic!("drive fixture")
        };
        for relative in ["library", "runtime\\library"] {
            cases.push((
                "drive-relative",
                Some(format!("{}:{relative}", drive as char).into()),
                Some(work.join(relative).into_os_string()),
                true,
            ));
        }
    }
    for (label, input, expected, readable) in cases.drain(..) {
        let mut command = tokio::process::Command::new(std::env::current_exe().unwrap());
        command
            .args(["--exact", &node("child_environment_start"), "--nocapture"])
            .env_clear()
            .env("R2_PROOF_LOADER_ROOT", root)
            .current_dir(&work)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        for name in ["SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
        if let Some(value) = input {
            command.env("ORT_DYLIB_PATH", value);
        }
        #[cfg(windows)]
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
        let mut child = command.spawn().unwrap();
        let status = tokio::time::timeout(Duration::from_secs(25), child.wait()).await;
        if status.is_err() {
            let _ = child.kill().await;
        }
        let output = child.wait_with_output().await.unwrap();
        assert!(
            matches!(&status, Ok(Ok(status)) if status.success()),
            "{label}: wait={status:?}; {output:?}"
        );
        let received: Value =
            serde_json::from_slice(&fs::read(root.join("result.json")).unwrap()).unwrap();
        assert_eq!(received["value"], json!(expected), "{label}");
        assert_eq!(
            received["bytes"],
            if readable {
                json!(b"caller library".to_vec())
            } else {
                Value::Null
            },
            "{label}"
        );
        assert!(!root.join("private").exists());
        fs::remove_file(root.join("result.json")).unwrap();
    }
}

#[cfg(target_os = "linux")]
#[test]
fn runtime_library_uses_resolved_daemon_directory() {
    let cwd = std::env::current_dir().unwrap();
    let home = Home(cwd.join(format!("r2-proof-link-{}", uuid::Uuid::new_v4())));
    let root = &home.0;
    let real = root.join("real");
    fs::create_dir_all(&real).unwrap();
    fs::write(real.join("daemon"), b"fixture executable").unwrap();
    let link = root.join("daemon");
    std::os::unix::fs::symlink(real.join("daemon"), &link).unwrap();
    // Use a unique caller-relative name without mutating this process's cwd.
    let relative = root.strip_prefix(&cwd).unwrap().join("runtime/library");
    let caller_library = cwd.join(&relative);
    let link_library = root.join(&relative);
    for path in [&caller_library, &link_library] {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, b"caller library").unwrap();
    }
    assert_eq!(
        runtime_library(&link, relative.clone().into_os_string()),
        Some(caller_library.into_os_string()),
        "a link-directory match must not hide the caller-relative fallback"
    );
    fs::remove_file(link_library).unwrap();
    let target_library = real.join(&relative);
    fs::create_dir_all(target_library.parent().unwrap()).unwrap();
    fs::write(target_library, b"target library").unwrap();
    assert_eq!(
        runtime_library(&link, relative.clone().into_os_string()),
        Some(relative.into_os_string()),
        "the real daemon directory must take precedence over the caller"
    );
}
