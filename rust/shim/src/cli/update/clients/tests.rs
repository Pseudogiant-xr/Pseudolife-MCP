use super::*;
use command::{Output, Runner};
use std::{cell::RefCell, collections::BTreeMap};

struct Fixture {
    path: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("native-client-test-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&path).unwrap();
        Self { path }
    }
    fn write(&self, path: &Path, value: &Value) {
        crate::board::state::private_dir(path.parent().unwrap()).unwrap();
        let bytes = serde_json::to_vec_pretty(value).unwrap();
        if path.exists() {
            crate::board::state::atomic_write(path, &bytes, None).unwrap();
        } else {
            crate::board::state::atomic_create(path, &bytes).unwrap();
        }
    }
    fn hooks(&self, root: &Path, body: &str) {
        crate::board::state::private_dir(root).unwrap();
        for name in codex::SCRIPTS {
            fs::write(root.join(name), body).unwrap();
        }
        self.write(&root.join("hooks.json"),&json!({"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"fixture-command","timeout":3}]}]}}));
    }
    fn plugin(&self) -> (PathBuf, PathBuf, PathBuf) {
        let root = self.path.join("plugins");
        let clone = root.join("marketplaces/pseudolife-mcp/plugin");
        let cache = root.join("cache/old");
        self.write(
            &clone.join(".claude-plugin/plugin.json"),
            &json!({"name":"pseudolife-memory"}),
        );
        self.write(
            &cache.join(".claude-plugin/plugin.json"),
            &json!({"name":"pseudolife-memory"}),
        );
        self.write(&root.join("installed_plugins.json"),&json!({"plugins":{(plugin::PLUGIN):[{"version":"fixture-old","installPath":cache,"scope":"user"}]}}));
        self.write(&root.join("known_marketplaces.json"),&json!({"pseudolife-mcp":{"source":{"source":"git","url":"https://fixture.invalid/plugin.git"},"installLocation":clone.parent().unwrap()}}));
        (root, clone, cache)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}
struct Fake<F> {
    calls: RefCell<Vec<Vec<String>>>,
    f: F,
}
impl<F> Fake<F> {
    fn new(f: F) -> Self {
        Self {
            calls: RefCell::new(vec![]),
            f,
        }
    }
}
impl<F: Fn(&[&str], Option<&Path>) -> Output> Runner for Fake<F> {
    fn run(
        &self,
        _executable: &Path,
        args: &[&str],
        cwd: Option<&Path>,
        _timeout: Duration,
        _env: &[(&str, &str)],
    ) -> Output {
        assert!(!args.contains(&"uninstall"));
        assert!(!args.contains(&"remove"));
        self.calls
            .borrow_mut()
            .push(args.iter().map(|s| s.to_string()).collect());
        (self.f)(args, cwd)
    }
}
fn output(code: i32, text: &str) -> Output {
    Output {
        code,
        bytes: text.as_bytes().to_vec(),
    }
}

#[test]
fn shared_consent_cases_match_oracle() {
    let cases: Value = serde_json::from_str(include_str!(
        "../../../../../cli_harness/fixtures/native_codex_consent.json"
    ))
    .unwrap();
    for case in cases.as_array().unwrap() {
        assert_eq!(
            codex::manifest(case["input"].as_str().unwrap().as_bytes()),
            case.get("normalized").filter(|v| !v.is_null()).cloned(),
            "{}",
            case["name"]
        );
    }
}

#[test]
fn codex_consent_normalizes_only_clamped_timeout_and_preserves_types() {
    let base=br#"{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"fixture","timeout":99}]}]}}"#;
    let same=br#"{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"fixture","timeout":3}]}]}}"#;
    assert_eq!(codex::manifest(base), codex::manifest(same));
    for changed in [
        r#"{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"fixture","timeout":3}]}]}}"#,
        r#"{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"changed","timeout":3}]}]}}"#,
        r#"{"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"fixture","timeout":3,"custom":true}]}]}}"#,
    ] {
        assert_ne!(codex::manifest(base), codex::manifest(changed.as_bytes()));
    }
    assert_ne!(
        codex::manifest(br#"{"hooks":{},"description":null}"#),
        codex::manifest(br#"{"hooks":{}}"#)
    );
    assert_ne!(
        codex::manifest(
            br#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"fixture","x":true}]}]}}"#
        ),
        codex::manifest(
            br#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"fixture","x":1}]}]}}"#
        )
    );
}
#[test]
fn malformed_or_ambiguous_codex_definitions_fail_closed() {
    for bytes in [
        br#"{"hooks":{},"hooks":{}}"#.as_slice(),
        br#"{"hooks":{"Stop":[],"\u0053top":[]}}"#,
        br#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"fixture","timeout":true}]}]}}"#,
        br#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"fixture","timeout":-0}]}]}}"#,
        br#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"fixture","timeout":18446744073709551616}]}]}}"#,
        br#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"fixture","timeout":1.0}]}]}}"#,
        br#"{"hooks":{},"description":"\ud800"}"#,
        br#"{"hooks":{"Stop":[{"hooks":[{"type":"prompt","command":"fixture"}]}]}}"#,
        br#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"fixture","commandWindows":"a","command_windows":"b"}]}]}}"#,
        br#"{"hooks":{},"extra":true}"#,
    ] {assert!(codex::manifest(bytes).is_none(),"{}",String::from_utf8_lossy(bytes));}
}

#[test]
fn negative_zero_numeric_tokens_are_distinct_from_strings_and_integer_zero() {
    let text=br#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"fixture -0 and \\\"quoted\\\"","custom":-0}]}]}}"#;
    let manifest = codex::manifest(text).unwrap();
    let handler = &manifest["hooks"]["Stop"][0]["hooks"][0];
    assert!(handler["command"].as_str().unwrap().contains("-0"));
    assert_eq!(handler["custom"].to_string(), "-0.0");
    assert_ne!(handler["custom"], json!(0));
}
#[test]
fn codex_reports_missing_approval_and_preserves_disabled_handler() {
    let f = Fixture::new();
    let home = f.path.join("codex");
    let repo = f.path.join("repo");
    f.hooks(&repo.join("plugin/hooks"), "same scripts\n");
    f.hooks(
        &home.join("plugins/cache/pseudolife-mcp/pseudolife-memory/local/hooks"),
        "same scripts\n",
    );
    let path = home.join("config.toml");
    fs::write(
        &path,
        "[plugins.\"pseudolife-memory@pseudolife-mcp\"]\nenabled = true\n",
    )
    .unwrap();
    let runner =
        Fake::new(|_: &[&str], _: Option<&Path>| panic!("approval checks must not spawn a client"));
    let report = codex::check_with(&home, Some(&repo), None, false, &runner);
    assert_eq!(report["state"], "needs-approval");
    fs::write(&path,"[plugins.\"pseudolife-memory@pseudolife-mcp\"]\nenabled = true\n[hooks.state.\"pseudolife-memory@pseudolife-mcp:hooks/hooks.json:session_end:0:0\"]\nenabled = false\n").unwrap();
    let before = fs::read(&path).unwrap();
    assert_eq!(
        codex::check_with(&home, Some(&repo), None, false, &runner)["state"],
        "current"
    );
    assert_eq!(fs::read(&path).unwrap(), before);
}
#[test]
fn codex_distinguishes_script_drift_from_definition_drift() {
    let f = Fixture::new();
    let home = f.path.join("codex");
    let repo = f.path.join("repo");
    let installed = home.join("plugins/cache/pseudolife-mcp/pseudolife-memory/local/hooks");
    f.hooks(&repo.join("plugin/hooks"), "new\n");
    f.hooks(&installed, "old\n");
    fs::write(
        home.join("config.toml"),
        "[plugins.\"pseudolife-memory@pseudolife-mcp\"]\nenabled = true\n",
    )
    .unwrap();
    let runner =
        Fake::new(|_: &[&str], _: Option<&Path>| panic!("read-only hook checks must not spawn"));
    assert_eq!(
        codex::check_with(&home, Some(&repo), None, false, &runner)["state"],
        "behind"
    );
    f.write(&installed.join("hooks.json"),&json!({"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"changed","timeout":3}]}]}}));
    let report = codex::check_with(&home, Some(&repo), None, false, &runner);
    assert_eq!(report["state"], "stale");
    assert!(codex_reapproval_text(Some(&report)).contains("/hooks"));
    assert!(codex_reapproval_text(Some(&result("behind", "scripts only"))).is_empty());
}
#[test]
fn plugin_tree_ignores_crlf_and_client_bookkeeping() {
    let f = Fixture::new();
    let a = f.path.join("a");
    let b = f.path.join("b");
    f.hooks(&a, "same\r\n");
    f.hooks(&b, "same\n");
    fs::create_dir(a.join(".in_use")).unwrap();
    fs::write(a.join(".in_use/session"), "bookkeeping").unwrap();
    fs::create_dir(a.join(".git")).unwrap();
    fs::write(a.join(".git/HEAD"), "metadata").unwrap();
    assert_eq!(
        plugin::tree_files(&a).unwrap(),
        plugin::tree_files(&b).unwrap()
    );
    fs::write(b.join("session-end.sh"), "changed").unwrap();
    assert_ne!(
        plugin::tree_files(&a).unwrap(),
        plugin::tree_files(&b).unwrap()
    );
}
#[test]
fn failed_marketplace_refresh_cannot_claim_matching_cache_current_in_any_locale() {
    let f = Fixture::new();
    let (root, _, _) = f.plugin();
    for message in [
        "Failed to refresh marketplace fixture",
        "✘ Failed to update marketplace(s)",
        "âœ˜ Failed to update marketplace(s)",
    ] {
        let runner = Fake::new(|_: &[&str], _: Option<&Path>| output(0, message));
        let report = plugin::update_with(
            None,
            &root,
            &f.path.join("settings.json"),
            &f.path.join("fixture-claude"),
            &runner,
        );
        assert_eq!(report["state"], "failed");
        assert_eq!(report["marketplace_update"], "failed");
        assert!(
            !runner
                .calls
                .borrow()
                .iter()
                .any(|c| c.len() > 1 && c[0] == "plugin" && c[1] == "update")
        );
    }
}
#[test]
fn refused_marketplace_migration_restores_only_its_entry() {
    let f = Fixture::new();
    let (root, _, _) = f.plugin();
    let settings = f.path.join("settings.json");
    let original = json!({"source":{"source":"github","repo":"Pseudogiant-xr/Pseudolife-MCP"},"autoUpdate":true});
    f.write(&settings,&json!({"theme":"original","extraKnownMarketplaces":{"pseudolife-mcp":original,"other":{"source":{"source":"directory","path":"fixture"}}}}));
    f.write(&root.join("known_marketplaces.json"),&json!({"pseudolife-mcp":{"source":{"source":"github","repo":"Pseudogiant-xr/Pseudolife-MCP"}}}));
    let runner = Fake::new(|_: &[&str], _: Option<&Path>| {
        let mut data = read_json(&settings).unwrap();
        data["theme"] = json!("concurrent-writer");
        f.write(&settings, &data);
        output(1, "refused")
    });
    let report = plugin::migrate_with(&root, &settings, &f.path.join("fixture-claude"), &runner);
    assert_eq!(report["state"], "failed");
    let after = read_json(&settings).unwrap();
    assert_eq!(after["theme"], "concurrent-writer");
    assert_eq!(after["extraKnownMarketplaces"]["pseudolife-mcp"], original);
    assert!(after["extraKnownMarketplaces"].get("other").is_some());
}

#[test]
fn migration_failure_preserves_absent_settings_and_reports_its_cause() {
    let f = Fixture::new();
    let (root, clone, _) = f.plugin();
    let settings = f.path.join("settings.json");
    f.write(
        &root.join("known_marketplaces.json"),
        &json!({"pseudolife-mcp":{
            "source":{"source":"github","repo":"Pseudogiant-xr/Pseudolife-MCP"},
            "installLocation":clone.parent().unwrap()
        }}),
    );
    let runner = Fake::new(|args: &[&str], _: Option<&Path>| {
        output(if args.contains(&"add") { 23 } else { 0 }, "fixture")
    });
    let report = plugin::update_with(
        None,
        &root,
        &settings,
        &f.path.join("fixture-claude"),
        &runner,
    );
    assert_eq!(report["state"], "failed");
    assert!(
        report["detail"]
            .as_str()
            .unwrap()
            .contains("marketplace add did not verify (exit 23)")
    );
    assert!(
        !settings.exists(),
        "rollback must not create settings that never existed"
    );
}

#[test]
fn ordinary_client_children_keep_the_callers_ssh_configuration() {
    let expected = std::env::var("GIT_SSH_COMMAND").unwrap_or_default();
    let output = command::NativeRunner.run(
        &std::env::current_exe().unwrap(),
        &[
            "--exact",
            "cli::update::clients::tests::ssh_configuration_child",
            "--ignored",
        ],
        None,
        Duration::from_secs(30),
        &[("PSEUDOLIFE_FIXTURE_EXPECTED_SSH", &expected)],
    );
    assert_eq!(output.code, 0, "{}", output.text());
}

#[test]
fn refused_migration_does_not_create_previously_absent_settings() {
    let f = Fixture::new();
    let (root, _, _) = f.plugin();
    let settings = f.path.join("settings.json");
    f.write(
        &root.join("known_marketplaces.json"),
        &json!({"pseudolife-mcp":{
            "source":{"source":"github","repo":"Pseudogiant-xr/Pseudolife-MCP"}
        }}),
    );
    let runner = Fake::new(|_: &[&str], _: Option<&Path>| output(23, "refused"));
    let report = plugin::migrate_with(&root, &settings, &f.path.join("fixture-claude"), &runner);
    assert_eq!(report["state"], "failed");
    assert!(!settings.exists());
}

#[test]
#[ignore = "subprocess fixture"]
fn ssh_configuration_child() {
    let expected = std::env::var("PSEUDOLIFE_FIXTURE_EXPECTED_SSH").unwrap();
    assert!(std::env::var("GIT_SSH_COMMAND").unwrap_or_default() == expected);
}
#[test]
fn add_failure_after_record_moves_keeps_settings_consistent_and_fails() {
    let f = Fixture::new();
    let (root, _, _) = f.plugin();
    let settings = f.path.join("settings.json");
    let github = json!({"source":"github","repo":"Pseudogiant-xr/Pseudolife-MCP"});
    f.write(
        &settings,
        &json!({"extraKnownMarketplaces":{"pseudolife-mcp":{"source":github}}}),
    );
    f.write(
        &root.join("known_marketplaces.json"),
        &json!({"pseudolife-mcp":{"source":github}}),
    );
    let runner = Fake::new(|_: &[&str], _: Option<&Path>| {
        f.write(&root.join("known_marketplaces.json"),&json!({"pseudolife-mcp":{"source":{"source":"git","url":"https://fixture.invalid/plugin.git"}}}));
        output(124, "")
    });
    assert_eq!(
        plugin::migrate_with(&root, &settings, &f.path.join("fixture-claude"), &runner)["state"],
        "failed"
    );
    assert_eq!(
        read_json(&settings).unwrap()["extraKnownMarketplaces"]["pseudolife-mcp"]["source"]["source"],
        "git"
    );
}
#[test]
fn user_selected_marketplace_source_is_never_rewritten() {
    let f = Fixture::new();
    let (root, _, _) = f.plugin();
    let settings = f.path.join("settings.json");
    f.write(&settings,&json!({"extraKnownMarketplaces":{"pseudolife-mcp":{"source":{"source":"directory","path":"fixture-custom"}}}}));
    let before = fs::read(&settings).unwrap();
    let runner =
        Fake::new(|_: &[&str], _: Option<&Path>| panic!("user source must not trigger migration"));
    assert_eq!(
        plugin::migrate_with(&root, &settings, &f.path.join("fixture-claude"), &runner)["state"],
        "kept"
    );
    assert_eq!(fs::read(&settings).unwrap(), before);
}
#[test]
fn plugin_update_keeps_old_in_use_tree_and_verifies_new_record() {
    let f = Fixture::new();
    let (root, clone, cache) = f.plugin();
    fs::write(clone.join("new-file"), "new").unwrap();
    fs::write(cache.join("old-file"), "running").unwrap();
    let new_cache = root.join("cache/new");
    let runner = Fake::new(|args: &[&str], _: Option<&Path>| {
        if args.get(1) == Some(&"update") {
            f.write(
                &new_cache.join(".claude-plugin/plugin.json"),
                &json!({"name":"pseudolife-memory"}),
            );
            fs::write(new_cache.join("new-file"), "new").unwrap();
            f.write(&root.join("installed_plugins.json"),&json!({"plugins":{(plugin::PLUGIN):[{"version":"fixture-new","installPath":new_cache,"scope":"user"}]}}));
        }
        output(0, "okay")
    });
    assert_eq!(
        plugin::update_with(
            None,
            &root,
            &f.path.join("settings.json"),
            &f.path.join("fixture-claude"),
            &runner
        )["state"],
        "refreshed:fixture-new"
    );
    assert_eq!(fs::read(cache.join("old-file")).unwrap(), b"running");
}
#[test]
fn plugin_project_scope_needs_its_recorded_working_directory() {
    let f = Fixture::new();
    let (root, clone, cache) = f.plugin();
    fs::write(clone.join("new-file"), "new").unwrap();
    let project = f.path.join("project");
    f.write(&root.join("installed_plugins.json"),&json!({"plugins":{(plugin::PLUGIN):[{"version":"fixture-old","installPath":cache,"scope":"project","projectPath":project}]}}));
    let seen = RefCell::new(false);
    let runner = Fake::new(|args: &[&str], cwd: Option<&Path>| {
        if args.get(1) == Some(&"update") {
            assert_eq!(cwd, Some(project.as_path()));
            *seen.borrow_mut() = true;
        }
        output(1, "failure")
    });
    plugin::update_with(
        None,
        &root,
        &f.path.join("settings.json"),
        &f.path.join("fixture-claude"),
        &runner,
    );
    assert!(*seen.borrow());
}

#[test]
fn script_digest_matches_name_nul_bytes_nul_protocol() {
    use sha2::{Digest, Sha256};
    let f = Fixture::new();
    let root = f.path.join("hooks");
    f.hooks(&root, "fixture\r\n");
    let mut expected = Sha256::new();
    for name in codex::SCRIPTS {
        expected.update(name.as_bytes());
        expected.update(b"\0fixture\n\0");
    }
    assert_eq!(
        codex::digest(&root, codex::SCRIPTS).unwrap(),
        format!("{:x}", expected.finalize())
    );
    let _ = BTreeMap::<String, String>::new();
}

// Run NativeRunner in a separate process: signal tests must never signal the
// test harness, and the runner must own the complete primary/helper tree.
struct ClientProcess {
    fixture: Fixture,
    child: std::process::Child,
}
impl ClientProcess {
    fn start(mode: &str) -> Self {
        let fixture = Fixture::new();
        let child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "cli::update::clients::tests::client_runner_child",
                "--ignored",
            ])
            .env("PSEUDOLIFE_CLIENT_FIXTURE", &fixture.path)
            .env("PSEUDOLIFE_CLIENT_MODE", mode)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap();
        Self { fixture, child }
    }
    fn wait_file(&self, name: &str) {
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        while !self.fixture.path.join(name).exists() {
            assert!(std::time::Instant::now() < deadline, "missing {name}");
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    fn wait(&mut self) -> std::process::ExitStatus {
        let deadline = std::time::Instant::now() + Duration::from_secs(15);
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                return status;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "client runner did not finish"
            );
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    fn heartbeat(&self) -> Vec<u8> {
        fs::read(self.fixture.path.join("heartbeat")).unwrap()
    }
    fn wait_helper_stopped(&self) -> std::io::Result<()> {
        let lock = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(self.fixture.path.join("helper.lock"))?;
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            match lock.try_lock() {
                Ok(()) => return Ok(()),
                Err(fs::TryLockError::WouldBlock) => {
                    if std::time::Instant::now() >= deadline {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::TimedOut,
                            "client helper still holds its lifetime lock",
                        ));
                    }
                    std::thread::sleep(Duration::from_millis(20));
                }
                Err(error) => return Err(std::io::Error::other(error)),
            }
        }
    }
}
impl Drop for ClientProcess {
    fn drop(&mut self) {
        let _ = fs::write(self.fixture.path.join("stop"), b"stop");
        let _ = self.child.kill();
        let _ = self.child.wait();
        // The OS releases this lock even when the helper was killed. A helper
        // that survived a failed assertion also exits on the stop marker.
        let _ = self.wait_helper_stopped();
    }
}

#[test]
fn native_client_runner_preserves_completed_background_helpers() {
    let mut process = ClientProcess::start("complete");
    process.wait_file("heartbeat");
    assert_eq!(process.wait().code(), Some(23));
    let first = process.heartbeat();
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while process.heartbeat() == first {
        assert!(
            std::time::Instant::now() < deadline,
            "completed client helper was killed"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn native_client_runner_timeout_stops_descendants() {
    let mut process = ClientProcess::start("timeout");
    process.wait_file("heartbeat");
    assert_eq!(process.wait().code(), Some(124));
    process
        .wait_helper_stopped()
        .expect("timed-out client helper survived");
}

#[cfg(unix)]
#[test]
#[allow(unsafe_code)]
fn native_client_runner_forwards_cancellation_to_descendants() {
    use std::os::unix::process::ExitStatusExt;
    for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP, libc::SIGQUIT] {
        for mode in ["cooperative", "resistant"] {
            let mut process = ClientProcess::start(mode);
            process.wait_file("heartbeat");
            // SAFETY: this is the still-running child created above, never the
            // shared test harness or its process group.
            assert_eq!(unsafe { libc::kill(process.child.id() as i32, signal) }, 0);
            let status = process.wait();
            assert!(status.signal() == Some(signal) || status.code() == Some(128 + signal));
            assert_eq!(
                fs::read_to_string(process.fixture.path.join("signal")).ok(),
                Some(signal.to_string()),
                "client helper missed {signal} ({mode})"
            );
            process
                .wait_helper_stopped()
                .expect("cancelled client helper survived");
        }
    }
}

#[test]
#[ignore = "subprocess fixture"]
#[allow(unsafe_code)]
fn client_runner_child() {
    #[cfg(unix)]
    for signal in [
        libc::SIGINT,
        libc::SIGTERM,
        libc::SIGHUP,
        libc::SIGQUIT,
        libc::SIGTSTP,
    ] {
        // SAFETY: this isolated fixture, not the outer test process, owns its
        // initial dispositions even when a background shell ignored INT/HUP.
        unsafe {
            libc::signal(signal, libc::SIG_DFL);
        }
    }
    let root = std::env::var("PSEUDOLIFE_CLIENT_FIXTURE").unwrap();
    let mode = std::env::var("PSEUDOLIFE_CLIENT_MODE").unwrap();
    let result = command::NativeRunner.run(
        &std::env::current_exe().unwrap(),
        &[
            "--exact",
            "cli::update::clients::tests::client_primary_child",
            "--ignored",
        ],
        Some(Path::new(&root)),
        Duration::from_secs(if mode == "timeout" { 10 } else { 60 }),
        &[],
    );
    std::process::exit(result.code);
}

#[test]
#[ignore = "subprocess fixture"]
#[allow(unsafe_code)]
fn client_primary_child() {
    // A cooperative primary waits for its helper to finish signal cleanup.
    // Without this, the default disposition exits the primary immediately and
    // the runner correctly kills the helper before its polling loop can record
    // receipt; that cannot distinguish forwarding from forced cleanup.
    #[cfg(unix)]
    let _signals = super::super::signals::Guard::new().unwrap();
    #[cfg(windows)]
    {
        use windows_sys::Win32::{
            Foundation::{HANDLE_FLAG_INHERIT, SetHandleInformation},
            System::Console::{GetStdHandle, STD_ERROR_HANDLE, STD_OUTPUT_HANDLE},
        };
        // Rust's Windows spawn can inherit other inheritable handles even
        // with null stdio. This fixture models a detached helper that closes
        // the primary's captured pipes, not one that keeps them open.
        for stream in [STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
            // SAFETY: change only this isolated fixture's standard handles.
            assert_ne!(
                unsafe { SetHandleInformation(GetStdHandle(stream), HANDLE_FLAG_INHERIT, 0) },
                0
            );
        }
    }
    let root = PathBuf::from(std::env::var_os("PSEUDOLIFE_CLIENT_FIXTURE").unwrap());
    let mut helper = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "cli::update::clients::tests::client_helper_child",
            "--ignored",
        ])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    while !root.join("heartbeat").exists() {
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    if std::env::var("PSEUDOLIFE_CLIENT_MODE").unwrap() == "complete" {
        std::process::exit(23);
    }
    let _ = helper.wait();
}

#[test]
#[ignore = "subprocess fixture"]
fn client_helper_child() {
    let root = PathBuf::from(std::env::var_os("PSEUDOLIFE_CLIENT_FIXTURE").unwrap());
    #[cfg(unix)]
    let _signals = super::super::signals::Guard::new().unwrap();
    // Keep this handle alive until after the heartbeat closes. The parent
    // waits on lock release rather than guessing when a killed helper exited.
    let active = fs::OpenOptions::new()
        .create_new(true)
        .read(true)
        .write(true)
        .open(root.join("helper.lock"))
        .unwrap();
    active.try_lock().unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(60);
    use std::io::Write;
    let mut heartbeat = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(root.join("heartbeat"))
        .unwrap();
    #[cfg(unix)]
    let mut recorded_signal = false;
    while std::time::Instant::now() < deadline && root.exists() && !root.join("stop").exists() {
        #[cfg(unix)]
        if !recorded_signal && super::super::signals::received() != 0 {
            fs::write(
                root.join("signal"),
                super::super::signals::received().to_string(),
            )
            .unwrap();
            recorded_signal = true;
            if std::env::var("PSEUDOLIFE_CLIENT_MODE").unwrap() == "cooperative" {
                std::process::exit(0);
            }
        }
        heartbeat.write_all(b"x").unwrap();
        std::thread::sleep(Duration::from_millis(20));
    }
}
