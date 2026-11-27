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
        remove_unused_with(&self.layout, evidence, |_| Ok(BTreeSet::new()))
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
fn supervised_refresh_pins_an_image_until_its_child_is_proven_gone() {
    let f = Fixture::new();
    let selected = f.install("1.0.0");
    f.install("1.0.1");
    let root = f.path.join("tunnel");
    crate::board::state::private_dir(&root).unwrap();
    let path = root.join("fixture.process.json");
    let mut record = json!({"native_launch":"generation", "spawn_started":true,
        "supervisor":{"pid":88}, "target":null, "refresh":{"image":{"path":selected.executable()}}});
    let save = |record: &Value| {
        crate::board::state::atomic_write(&path, record.to_string().as_bytes(), None).unwrap()
    };
    save(&record);
    assert!(
        tunnel_runtimes(&f.layout, &root, &[])
            .unwrap()
            .contains(&selected.path)
    );
    record["target"] = json!({"pid":99});
    save(&record);
    assert!(
        tunnel_runtimes(&f.layout, &root, &f.windows_evidence().processes)
            .unwrap()
            .contains(&selected.path)
    );
    assert!(tunnel_runtimes(&f.layout, &root, &[]).unwrap().is_empty());
}

#[test]
fn pruning_rechecks_for_an_uncertain_refresh_under_the_usage_pin() {
    let f = Fixture::new();
    let selected = f.install("1.0.0");
    f.install("1.0.1");
    let root = f.path.join("tunnel");
    crate::board::state::private_dir(&root).unwrap();
    let evidence = f.windows_evidence();
    let _guard = UpdateLock::acquire(&f.layout).unwrap();
    let result = remove_unused_with(&f.layout, Ok(evidence), |processes| {
        let usage = launch::usage_file(&selected.usage_lock_path()).unwrap();
        assert!(matches!(
            usage.try_lock_shared(),
            Err(fs::TryLockError::WouldBlock)
        ));
        let record = json!({"native_launch":"new-generation", "spawn_started":true,
            "supervisor":{"pid":88}, "target":null, "refresh":{"image":{"path":selected.executable()}}});
        crate::board::state::atomic_create(
            &root.join("fixture.process.json"),
            record.to_string().as_bytes(),
        )
        .unwrap();
        tunnel_runtimes(&f.layout, &root, processes)
    });
    assert!(result["removed"].as_array().unwrap().is_empty() && selected.path.is_dir());
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

#[cfg(windows)]
#[test]
fn a_record_whose_name_differs_only_in_case_still_pins_its_runtime() {
    // Windows opens `fixture.PROCESS.JSON` for `fixture.process.json`, and
    // `tunnel status` reads it as the profile's record: pruning reads it too.
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let root = f.path.join("tunnel");
    crate::board::state::private_dir(&root).unwrap();
    crate::board::state::atomic_create(
        &root.join("fixture.PROCESS.JSON"),
        &serde_json::to_vec(&json!({"pid":88,"runtime_path":old.path})).unwrap(),
    )
    .unwrap();
    let evidence = f.windows_evidence();
    let pinned = tunnel_runtimes(&f.layout, &root, &evidence.processes).unwrap();
    assert!(pinned.contains(&old.path), "{pinned:?}");
}

/// Set by [`linux_nondumpable_child`]'s parent: run as the helper process.
#[cfg(target_os = "linux")]
const NONDUMPABLE_CHILD: &str = "PSEUDOLIFE_PRUNE_TEST_NONDUMPABLE_CHILD";

/// The helper half of the non-dumpable tests: a same-account process that
/// makes itself non-dumpable, as ssh-agent and gpg-agent do, and then waits
/// for its parent to close stdin. A no-op in an ordinary test run.
#[cfg(target_os = "linux")]
#[test]
fn linux_nondumpable_child() {
    if std::env::var_os(NONDUMPABLE_CHILD).is_none() {
        return;
    }
    rustix::process::set_dumpable_behavior(rustix::process::DumpableBehavior::NotDumpable)
        .unwrap();
    let _ = io::Read::read(&mut io::stdin(), &mut [0u8; 1]);
}

/// A same-account non-dumpable process whose `exe` link refuses this account,
/// stopped and reaped on drop.
#[cfg(target_os = "linux")]
struct Nondumpable(std::process::Child);

#[cfg(target_os = "linux")]
impl Nondumpable {
    fn spawn() -> Self {
        let child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "runtime::prune::tests::linux_nondumpable_child",
                "--test-threads=1",
                "--quiet",
            ])
            .env(NONDUMPABLE_CHILD, "1")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let child = Self(child);
        let exe = Path::new("/proc").join(child.0.id().to_string()).join("exe");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        loop {
            match fs::read_link(&exe) {
                Err(e) if e.kind() == io::ErrorKind::PermissionDenied => return child,
                _ if std::time::Instant::now() > deadline => {
                    panic!("the helper never became non-dumpable ({})", exe.display())
                }
                _ => std::thread::sleep(std::time::Duration::from_millis(20)),
            }
        }
    }

    fn pid(&self) -> u32 {
        self.0.id()
    }
}

#[cfg(target_os = "linux")]
impl Drop for Nondumpable {
    fn drop(&mut self) {
        drop(self.0.stdin.take());
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[cfg(target_os = "linux")]
#[test]
fn a_nondumpable_linux_process_is_liveness_evidence_not_a_refusal() {
    let child = Nondumpable::spawn();
    let mut rows = vec![];
    verify_process_evidence(&mut rows).expect("a non-dumpable process must not stop pruning");
    assert!(
        rows.iter()
            .any(|(pid, _, image)| *pid == child.pid() && image.as_os_str().is_empty()),
        "the non-dumpable pid is kept, with no image: {rows:?}"
    );
}

#[cfg(target_os = "linux")]
#[test]
fn a_nondumpable_linux_process_leaves_removal_to_the_usage_lock() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let current = f.install("1.0.1");
    let _child = Nondumpable::spawn();
    let evidence = || {
        let mut processes = vec![];
        verify_process_evidence(&mut processes).unwrap();
        Evidence {
            processes,
            registrations: BTreeSet::new(),
            tunnels: BTreeSet::new(),
        }
    };
    let pin = launch::pin(&old).unwrap();
    let report = f.prune(Ok(evidence()));
    assert_eq!(report["removed"], json!([]), "{report}");
    assert_eq!(report["held"][0]["path"], json!(old.path), "{report}");
    assert!(old.path.is_dir());
    drop(pin);
    let report = f.prune(Ok(evidence()));
    assert_eq!(report["removed"], json!([old.path]), "{report}");
    assert!(current.path.is_dir());
}

/// The live table of this Mac, as `ps` prints it to this account, is
/// evidence pruning can act on: no row of an ordinary host refuses it.
#[cfg(target_os = "macos")]
#[test]
fn the_live_macos_process_table_is_pruning_evidence() {
    let output = std::process::Command::new("/bin/ps")
        .args(["-axo", "pid=,ppid=,comm="])
        .output()
        .unwrap();
    let mut rows = vec![];
    let verdict = verify_process_evidence(&mut rows);
    let refused: Vec<_> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| {
            append_macos_process_images(&mut vec![], format!("{line}\n").as_bytes()).is_err()
        })
        .map(str::to_owned)
        .collect();
    assert!(verdict.is_ok(), "{verdict:?}; rows it refuses: {refused:#?}");
}
