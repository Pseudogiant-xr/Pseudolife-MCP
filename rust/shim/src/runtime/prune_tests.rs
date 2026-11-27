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
fn macos_login_shells_allow_idle_pruning_and_direct_launches_still_pin() {
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
fn a_macos_comm_that_is_not_an_image_path_leaves_removal_to_the_usage_lock() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let mut evidence = f.windows_evidence();
    // Rows a hosted macos-15 runner printed: a daemon, a driver with spaces,
    // PATH-started tools of this account and a zombie.
    append_macos_process_images(
        &mut evidence.processes,
        b"   93     1 endpointsecurityd\n  248     1 Core Audio Driver (NullAudio.driver)\n  929   927 sudo\n 1288  1281 awk\n 1292  1290 <defunct>\n 1300     1 -zsh/unknown\n 1301     1 pseudolife-mcp-helper\n",
    )
    .unwrap();
    for pid in [93, 248, 929, 1288, 1292, 1300, 1301] {
        assert!(
            evidence
                .processes
                .iter()
                .any(|p| p.0 == pid && p.2.as_os_str().is_empty()),
            "{pid}: {:?}",
            evidence.processes
        );
    }
    let pin = launch::pin(&old).unwrap();
    let report = f.prune(Ok(evidence.clone()));
    assert_eq!(report["removed"], json!([]), "{report}");
    assert_eq!(report["held"][0]["path"], json!(old.path), "{report}");
    drop(pin);
    assert_eq!(f.prune(Ok(evidence))["removed"], json!([old.path]));
}

#[test]
fn a_malformed_macos_process_row_still_blocks_pruning() {
    for line in ["", "x 1 /bin/zsh", "123 /bin/zsh", "123 y /bin/zsh"] {
        assert!(
            append_macos_process_images(&mut vec![], format!("{line}\n").as_bytes()).is_err(),
            "{line:?}"
        );
    }
}

/// A runtime that runs without its usage lock (a user's symlink to it found
/// through PATH, another data home) shows as the runtime's bare file name, so
/// that name, and only that name, still removes nothing.
#[test]
fn a_macos_process_named_like_the_runtime_without_a_path_blocks_pruning() {
    for line in [
        "123 1 pseudolife-mcp",
        "123 1 relative/pseudolife-mcp",
        "123 1 ./pseudolife-mcp",
    ] {
        assert!(
            append_macos_process_images(&mut vec![], format!("{line}\n").as_bytes()).is_err(),
            "{line:?}"
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
/// makes itself non-dumpable, as ssh-agent does, says so on stdout and waits
/// for its parent to close stdin. A no-op in an ordinary test run.
#[cfg(target_os = "linux")]
#[test]
fn linux_nondumpable_child() {
    if std::env::var_os(NONDUMPABLE_CHILD).is_none() {
        return;
    }
    rustix::process::set_dumpable_behavior(rustix::process::DumpableBehavior::NotDumpable).unwrap();
    println!("{NONDUMPABLE_READY}");
    let _ = io::Read::read(&mut io::stdin(), &mut [0u8; 1]);
}

/// What [`linux_nondumpable_child`] prints once it is non-dumpable.
#[cfg(target_os = "linux")]
const NONDUMPABLE_READY: &str = "pseudolife-prune-test-nondumpable-ready";

/// A same-account non-dumpable process whose `exe` link refuses this account,
/// stopped and reaped on drop.
#[cfg(target_os = "linux")]
struct Nondumpable(std::process::Child);

#[cfg(target_os = "linux")]
impl Nondumpable {
    /// The helper, or `None` where this account may read a non-dumpable
    /// process's `exe` link anyway (root, or `CAP_SYS_PTRACE`), so the case
    /// under test cannot arise here.
    fn spawn() -> Option<Self> {
        use std::io::BufRead;
        let mut child = Self(
            std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "runtime::prune::tests::linux_nondumpable_child",
                    "--test-threads=1",
                    "--nocapture",
                ])
                .env(NONDUMPABLE_CHILD, "1")
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .spawn()
                .unwrap(),
        );
        let stdout = child.0.stdout.take().unwrap();
        // libtest prints the helper's `test <name> ... ` on the marker's own
        // line, so the marker is found within a line; a reader thread keeps a
        // helper that never prints it from hanging the test.
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let ready = io::BufReader::new(stdout)
                .lines()
                .map_while(std::result::Result::ok)
                .any(|line| line.contains(NONDUMPABLE_READY));
            let _ = ready_tx.send(ready);
        });
        let ready = ready_rx
            .recv_timeout(std::time::Duration::from_secs(20))
            .unwrap_or(false);
        assert!(ready, "the helper never said it became non-dumpable");
        let exe = Path::new("/proc").join(child.pid().to_string()).join("exe");
        match fs::read_link(&exe) {
            Err(e) if e.kind() == io::ErrorKind::PermissionDenied => Some(child),
            other => {
                eprintln!(
                    "skipped: this account reads a non-dumpable process's exe link ({other:?})"
                );
                None
            }
        }
    }

    fn pid(&self) -> u32 {
        self.0.id()
    }
}

/// The live table as pruning reads it. A same-account process caught between
/// exit and reaping (another test's child) briefly reads as an unreadable
/// image, which refuses pruning by design, so a scan that hits one is
/// retried; a refusal that persists is the answer.
#[cfg(target_os = "linux")]
fn live_linux_rows() -> Result<Vec<Process>> {
    let mut last = Ok(vec![]);
    for _ in 0..10 {
        let mut rows = vec![];
        last = verify_process_evidence(&mut rows).map(|()| rows);
        if last.is_ok() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    last
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
    let Some(child) = Nondumpable::spawn() else {
        return;
    };
    let rows = live_linux_rows().expect("a non-dumpable process must not stop pruning");
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
    let Some(_child) = Nondumpable::spawn() else {
        return;
    };
    let evidence = || Evidence {
        processes: live_linux_rows().unwrap(),
        registrations: BTreeSet::new(),
        tunnels: BTreeSet::new(),
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
    assert!(
        verdict.is_ok(),
        "{verdict:?}; rows it refuses: {refused:#?}"
    );
}
