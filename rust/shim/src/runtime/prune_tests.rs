use super::*;

struct Fixture {
    path: PathBuf,
    layout: Layout,
}

impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("native-prune-test-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&path).unwrap();
        let layout = Layout {
            root: path.join("runtimes"),
            launcher: path.join("bin").join(BINARY),
            user_bin: None,
            user_path: false,
        };
        Self { path, layout }
    }

    fn install(&self, version: &str) -> Runtime {
        let source = self.path.join(format!("fixture-{version}"));
        #[cfg(windows)]
        let header = b"MZ".as_slice();
        #[cfg(target_os = "linux")]
        let header = b"\x7fELF".as_slice();
        #[cfg(target_os = "macos")]
        let header = b"\xcf\xfa\xed\xfe".as_slice();
        let mut bytes = header.to_vec();
        bytes.extend_from_slice(version.as_bytes());
        fs::write(&source, bytes).unwrap();
        let _lock = UpdateLock::acquire(&self.layout).unwrap();
        install_binary(&self.layout, version, "fixture", None, &source).unwrap()
    }

    fn prune(&self, evidence: Result<Evidence>) -> Value {
        let _lock = UpdateLock::acquire(&self.layout).unwrap();
        remove_unused(&self.layout, evidence)
    }

    fn windows_evidence(&self) -> Evidence {
        let mut processes = vec![(42, 1, self.path.join("unrelated-image"))];
        windows_snapshot_evidence(&mut processes, &[(0, 0), (4, 0), (42, 1), (88, 4)], true)
            .unwrap();
        Evidence {
            processes,
            registrations: BTreeSet::new(),
            tunnels: BTreeSet::new(),
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

#[test]
fn unopenable_windows_system_pids_allow_pruning_an_idle_runtime() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let current = f.install("1.0.1");
    let evidence = f.windows_evidence();
    assert!(
        evidence
            .processes
            .iter()
            .any(|p| p.0 == 88 && p.2.as_os_str().is_empty())
    );
    let report = f.prune(Ok(evidence));
    assert_eq!(report["removed"], json!([old.path]), "{report}");
    assert!(current.path.is_dir());
}

#[test]
fn unopenable_windows_pids_do_not_override_a_live_usage_pin() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let pin = launch::pin(&old).unwrap();
    let report = f.prune(Ok(f.windows_evidence()));
    assert_eq!(report["removed"], json!([]), "{report}");
    assert_eq!(report["held"][0]["path"], json!(old.path));
    assert!(old.path.is_dir());
    drop(pin);
    assert_eq!(
        f.prune(Ok(f.windows_evidence()))["removed"],
        json!([old.path])
    );
}

#[test]
fn readable_direct_launch_still_pins_a_runtime_without_a_usage_lock() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let mut evidence = f.windows_evidence();
    evidence.processes.push((99, 1, old.executable()));
    let report = f.prune(Ok(evidence));
    assert_eq!(report["removed"], json!([]), "{report}");
    assert_eq!(report["held"][0]["processes"], 1);
    assert!(old.path.is_dir());
}

#[test]
fn registration_still_pins_a_runtime_with_unopenable_windows_pids() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let mut evidence = f.windows_evidence();
    evidence.registrations.insert(old.path.clone());
    let report = f.prune(Ok(evidence));
    assert_eq!(report["removed"], json!([]), "{report}");
    assert!(old.path.is_dir());
}

#[test]
fn tunnel_with_an_unopenable_windows_pid_still_pins_its_runtime() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let root = f.path.join("tunnel");
    crate::board::state::private_dir(&root).unwrap();
    crate::board::state::atomic_create(
        &root.join("fixture.process.json"),
        &serde_json::to_vec(&json!({"pid":88,"runtime_path":old.path})).unwrap(),
    )
    .unwrap();
    let mut evidence = f.windows_evidence();
    evidence.tunnels = tunnel_runtimes(&f.layout, &root, &evidence.processes).unwrap();
    assert!(evidence.tunnels.contains(&old.path));
    let report = f.prune(Ok(evidence));
    assert_eq!(report["removed"], json!([]), "{report}");
    assert!(old.path.is_dir());
}

#[test]
fn readable_ambiguous_windows_image_blocks_pruning() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let mut rows = vec![(42, 1, PathBuf::from("relative/pseudolife-mcp"))];
    let error = windows_snapshot_evidence(&mut rows, &[(42, 1), (88, 4)], true).unwrap_err();
    assert!(error.contains("readable process image is ambiguous"));
    let report = f.prune(Err(error));
    assert_eq!(report["removed"], json!([]), "{report}");
    assert!(old.path.is_dir());
}

#[test]
fn missing_snapshot_and_registration_evidence_block_pruning() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let error = windows_snapshot_evidence(&mut vec![], &[], true).unwrap_err();
    for error in [error, "client configuration could not be read".into()] {
        let report = f.prune(Err(error));
        assert_eq!(report["removed"], json!([]), "{report}");
        assert!(old.path.is_dir());
    }
}

#[test]
fn partial_windows_snapshot_failure_preserves_every_runtime() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let current = f.install("1.0.1");
    let mut rows = vec![(42, 1, f.path.join("unrelated-image"))];
    let before = rows.clone();
    let error = windows_snapshot_evidence(&mut rows, &[(42, 1), (88, 4)], false).unwrap_err();
    assert_eq!(rows, before);
    let report = f.prune(Err(error));
    assert_eq!(report["removed"], json!([]), "{report}");
    assert!(old.path.is_dir());
    assert!(current.path.is_dir());
}

#[test]
fn unopenable_windows_tunnel_pid_does_not_hide_a_missing_bridge() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let root = f.path.join("tunnel");
    crate::board::state::private_dir(&root).unwrap();
    crate::board::state::atomic_create(
        &root.join("fixture.process.json"),
        &serde_json::to_vec(&json!({
            "pid":88,"snapshot":{"kind":"native","digest":"a".repeat(64)}
        }))
        .unwrap(),
    )
    .unwrap();
    let evidence = f.windows_evidence();
    let error = tunnel_runtimes(&f.layout, &root, &evidence.processes).unwrap_err();
    assert!(error.contains("native tunnel snapshot is unreadable"));
    assert_eq!(f.prune(Err(error))["removed"], json!([]));
    assert!(old.path.is_dir());
}

#[test]
fn macos_login_shell_comm_allows_idle_pruning_and_retains_direct_launches() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let mut evidence = f.windows_evidence();
    for shell in ["sh", "bash", "zsh", "ksh", "csh", "tcsh", "fish"] {
        append_macos_process_images(
            &mut evidence.processes,
            format!("123 1 -{shell}\n").as_bytes(),
        )
        .unwrap();
    }
    let mut pinned = evidence.clone();
    pinned.processes.push((99, 1, old.executable()));
    assert_eq!(f.prune(Ok(pinned))["removed"], json!([]));
    assert!(old.path.is_dir());
    assert_eq!(f.prune(Ok(evidence))["removed"], json!([old.path]));
}

#[test]
fn macos_readable_ambiguous_comm_still_blocks_pruning() {
    for image in [
        "pseudolife-mcp",
        "relative/pseudolife-mcp",
        "-zsh/unknown",
        "-unknown",
    ] {
        let line = format!("123 1 {image}\n");
        assert!(
            append_macos_process_images(&mut vec![], line.as_bytes()).is_err(),
            "{image}"
        );
    }
    let mut rows = vec![];
    append_macos_process_images(
        &mut rows,
        b"123 1 /Applications/runtime with spaces/pseudolife-mcp\n",
    )
    .unwrap();
    assert_eq!(
        rows[0].2,
        PathBuf::from("/Applications/runtime with spaces/pseudolife-mcp")
    );
}
