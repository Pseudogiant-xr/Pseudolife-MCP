mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{
    adapter::{Adapter, View},
    doorbell::{Doorbell, resolve_command},
    notice::PendingNotice,
    state,
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

#[tokio::test]
async fn checked_watch_rejects_invalid_thread_before_subscribing_or_queueing() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let bell = Doorbell::with_timing(
        wrapper(&home, "success"),
        home.0.join("notices"),
        Duration::ZERO,
        Duration::from_secs(2),
    );
    let error = bell
        .watch_checked("invalid-thread", adapter.clone(), false)
        .await
        .unwrap_err();
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
    bell.observe("invalid-thread", &view(&[1], 1, 1, 1.0)).await;
    assert!(!home.0.join("notices").exists());
    assert!(!home.0.join("leader.pid").exists());
    for invalid in [
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}",
        " aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ] {
        assert_eq!(
            bell.watch_checked(invalid, adapter.clone(), false)
                .await
                .unwrap_err()
                .kind(),
            std::io::ErrorKind::InvalidInput
        );
    }
    bell.close().await;
    assert!(
        bell.watch_checked(BANK, adapter.clone(), false)
            .await
            .is_ok()
    );
    // Identifier validation remains an error even after close.
    assert_eq!(
        bell.watch_checked("invalid-thread", adapter.clone(), false)
            .await
            .unwrap_err()
            .kind(),
        std::io::ErrorKind::InvalidInput
    );
    adapter.close().await;
}

#[tokio::test]
async fn native_spawn_error_matrix_rolls_back_only_definite_failures() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let missing = home.0.join("missing-cli");
    let directory = home.0.join("directory-cli");
    std::fs::create_dir(&directory).unwrap();
    let bad_format = home.0.join(if cfg!(windows) {
        "bad-format.exe"
    } else {
        "bad-format"
    });
    executable(&bad_format, "");
    let mut commands = vec![(missing, true), (directory, true), (bad_format, true)];
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let denied = home.0.join("denied-cli");
        std::fs::write(&denied, "#!/bin/sh\nexit 0\n").unwrap();
        std::fs::set_permissions(&denied, std::fs::Permissions::from_mode(0o600)).unwrap();
        commands.push((denied, true));
        let no_shebang = home.0.join("no-shebang-cli");
        executable(&no_shebang, "exit 0\n");
        commands.push((no_shebang, true));
    }
    #[cfg(windows)]
    {
        let incompatible = home.0.join("incompatible.exe");
        executable(&incompatible, "invalid native executable fixture");
        commands.push((incompatible, false));
    }
    for (index, (command, definite)) in commands.into_iter().enumerate() {
        let native_error = tokio::process::Command::new(&command).spawn().unwrap_err();
        assert_eq!(
            matches!(
                native_error.kind(),
                std::io::ErrorKind::NotFound
                    | std::io::ErrorKind::PermissionDenied
                    | std::io::ErrorKind::NotADirectory
            ) || matches!(native_error.raw_os_error(), Some(8 | 193)),
            definite,
            "unexpected native fixture error: {native_error}"
        );
        let notices = home.0.join(format!("notices-{index}"));
        let pending = PendingNotice::new(&notices, BANK).unwrap();
        let bell = Doorbell::with_timing(command, notices, Duration::ZERO, Duration::from_secs(2));
        bell.watch_checked(BANK, adapter.clone(), true)
            .await
            .unwrap();
        bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
        assert!(
            state::exists(&pending.path),
            "fixture did not reserve before launch"
        );
        if definite {
            tokio::time::timeout(Duration::from_secs(5), async {
                while state::exists(&pending.path) {
                    tokio::time::sleep(Duration::from_millis(15)).await;
                }
            })
            .await
            .unwrap_or_else(|_| {
                panic!("reservation not rolled back for native case {index}: {native_error}")
            });
        } else {
            // The initial reservation remains after an ambiguous native error.
            // Joining the queue task ensures the assertion covers its error path.
            tokio::task::yield_now().await;
        }
        bell.close().await;
        assert_eq!(state::exists(&pending.path), !definite);
        assert!(!pending.path.with_extension("bell-accepted").exists());
        let ledger = std::fs::read_to_string(pending.path.parent().unwrap().join("ledger.log"))
            .unwrap_or_default();
        assert!(
            ledger
                .lines()
                .all(|line| line.split('\t').nth(1) != Some("bell"))
        );
    }
    adapter.close().await;
}

#[cfg(unix)]
#[tokio::test]
async fn posix_execve_keeps_kernel_elf_shebang_and_session_setup() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    for (index, command) in [PathBuf::from("/bin/true"), wrapper(&home, "session")]
        .into_iter()
        .enumerate()
    {
        let notices = home.0.join(format!("formats-{index}"));
        let pending = PendingNotice::new(&notices, BANK).unwrap();
        let bell = Doorbell::with_timing(command, notices, Duration::ZERO, Duration::from_secs(2));
        bell.watch_checked(BANK, adapter.clone(), true)
            .await
            .unwrap();
        bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
        wait_for(|| pending.path.with_extension("bell-accepted").exists()).await;
        bell.close().await;
        let ledger =
            std::fs::read_to_string(pending.path.parent().unwrap().join("ledger.log")).unwrap();
        assert_eq!(
            ledger
                .lines()
                .filter(|line| line.split('\t').nth(1) == Some("bell"))
                .count(),
            1
        );
    }
    let proof: Value =
        serde_json::from_slice(&std::fs::read(home.0.join("session-proof")).unwrap()).unwrap();
    assert_eq!(proof["pid"], proof["sid"]);
    assert_eq!(proof["pid"], proof["pgid"]);
    cleanup_evidence(
        json!({"scenario":"posix_kernel_formats_session", "platform":"linux", "elf_accepted":true, "shebang_accepted":true, "session_leader_before_cli":true, "pid":proof["pid"]}),
    );
    adapter.close().await;
}

fn executable(path: &Path, body: &str) {
    std::fs::write(path, body).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
}

fn wrapper(home: &Home, mode: &str) -> PathBuf {
    let path = home
        .0
        .join(if cfg!(windows) { "codex.cmd" } else { "codex" });
    let runner = std::env::current_exe().unwrap();
    let body = if cfg!(windows) {
        format!(
            "@echo off\r\nset DOORBELL_FIXTURE_ROOT={}\r\nset DOORBELL_FIXTURE_ROLE=launcher\r\nset DOORBELL_FIXTURE_MODE={mode}\r\n\"{}\" --exact fixture_process --nocapture\r\nexit /b %errorlevel%\r\n",
            home.0.display(),
            runner.display()
        )
    } else {
        format!(
            "#!/bin/sh\nexport DOORBELL_FIXTURE_ROOT='{}' DOORBELL_FIXTURE_ROLE=launcher DOORBELL_FIXTURE_MODE='{mode}'\nexec '{}' --exact fixture_process --nocapture\n",
            home.0.display(),
            runner.display()
        )
    };
    executable(&path, &body);
    path
}

fn child(role: &str) -> std::process::Child {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", "fixture_process", "--nocapture"])
        .env("DOORBELL_FIXTURE_ROLE", role)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command.spawn().unwrap()
}

// The test executable is also the disposable native CLI, intermediate and worker.
// No interpreter, installed CLI or native Codex home participates.
#[test]
fn fixture_process() {
    let Some(root) = std::env::var_os("DOORBELL_FIXTURE_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    match std::env::var("DOORBELL_FIXTURE_ROLE").unwrap().as_str() {
        "launcher" => {
            #[cfg(unix)]
            if std::env::var("DOORBELL_FIXTURE_MODE").unwrap() == "session" {
                std::fs::write(
                    root.join("session-proof"),
                    json!({
                        "pid":rustix::process::getpid().as_raw_nonzero().get(),
                        "sid":rustix::process::getsid(None).unwrap().as_raw_nonzero().get(),
                        "pgid":rustix::process::getpgrp().as_raw_nonzero().get()
                    })
                    .to_string(),
                )
                .unwrap();
                return;
            }
            if std::env::var("DOORBELL_FIXTURE_MODE").unwrap() == "receipt" {
                std::fs::write(root.join("waiting"), "waiting").unwrap();
                let end = std::time::Instant::now() + Duration::from_secs(10);
                while !root.join("release").exists() && std::time::Instant::now() < end {
                    std::thread::sleep(Duration::from_millis(15));
                }
                return;
            }
            if std::env::var("DOORBELL_FIXTURE_MODE").unwrap() == "environment" {
                let clean = std::env::vars_os().all(|(key, _)| {
                    !key.to_string_lossy()
                        .to_uppercase()
                        .starts_with("PSEUDOLIFE_")
                });
                let home_kept = std::env::var_os("CODEX_HOME")
                    == std::env::var_os("DOORBELL_EXPECTED_CODEX_HOME");
                let unrelated_kept =
                    std::env::var("DOORBELL_FIXTURE_KEEP").ok().as_deref() == Some("fixture-value");
                std::fs::write(
                    root.join("environment-proof"),
                    json!({"clean":clean,"home_kept":home_kept,"unrelated_kept":unrelated_kept})
                        .to_string(),
                )
                .unwrap();
                return;
            }
            std::fs::write(root.join("leader.pid"), std::process::id().to_string()).unwrap();
            assert!(child("intermediate").wait().unwrap().success());
            if std::env::var("DOORBELL_FIXTURE_MODE").unwrap() == "success" {
                return;
            }
            std::thread::sleep(Duration::from_secs(12));
        }
        "intermediate" => {
            // Deliberate orphan: cleanup must reach a worker whose parent has exited.
            #[allow(clippy::zombie_processes)]
            let worker = child("worker");
            std::fs::write(root.join("observed-worker.pid"), worker.id().to_string()).unwrap();
        }
        "worker" => {
            std::fs::write(root.join("worker.pid"), std::process::id().to_string()).unwrap();
            let end = std::time::Instant::now() + Duration::from_secs(10);
            let mut ticks = 0;
            while std::time::Instant::now() < end && !root.join("stop").exists() {
                ticks += 1;
                std::fs::write(root.join("ticks"), ticks.to_string()).unwrap();
                std::thread::sleep(Duration::from_millis(30));
            }
        }
        _ => panic!("invalid fixture role"),
    }
}

struct StopWorker(PathBuf);
impl Drop for StopWorker {
    fn drop(&mut self) {
        let _ = std::fs::write(self.0.join("stop"), "stop");
    }
}

async fn wait_for(mut condition: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while !condition() {
            tokio::time::sleep(Duration::from_millis(15)).await;
        }
    })
    .await
    .unwrap();
}

fn terminated(pid: u32) -> bool {
    #[cfg(unix)]
    {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat"));
        match stat {
            Err(error) => error.kind() == std::io::ErrorKind::NotFound,
            Ok(stat) => stat
                .rsplit_once(") ")
                .is_some_and(|(_, rest)| rest.starts_with('Z')),
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        Command::new("powershell.exe").args(["-NoProfile", "-NonInteractive", "-Command", &format!("$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($null -eq $p) {{ exit 0 }}; if ($p.WaitForExit(0)) {{ exit 0 }}; exit 1")])
            .creation_flags(0x08000000).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .status().unwrap().success()
    }
}

fn cleanup_evidence(value: Value) {
    use fs2::FileExt;
    use std::io::Write;
    if let Some(path) = std::env::var_os("DOORBELL_CLEANUP_EVIDENCE") {
        let mut output = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .open(path)
            .unwrap();
        output.lock_exclusive().unwrap();
        output.write_all(format!("{value}\n").as_bytes()).unwrap();
        FileExt::unlock(&output).unwrap();
    }
}

fn view(ids: &[u64], count: u64, watermark: u64, ring_at: f64) -> View {
    View {
        count: Some(count),
        preview: ids.iter().map(|id| json!({"message_id":format!("m{id}"),"created_at":1000.0+*id as f64,"excerpt":"peer payload"})).collect(),
        watermark, delivered: 0,
        wake: Some(json!({"decision":"rung","reason":"fixture","ring_at":ring_at,"message_expires_at":now()+3600.0})),
        wake_watermark: Some(watermark),
    }
}

#[tokio::test]
async fn native_timeout_kills_orphaned_grandchild_and_keeps_unresolved_notice() {
    native_cleanup(false).await;
}

#[tokio::test]
async fn native_close_kills_orphaned_grandchild_and_keeps_unresolved_notice() {
    native_cleanup(true).await;
}

async fn native_cleanup(cancel: bool) {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let _stop = StopWorker(home.0.clone());
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let directory = home.0.join("notices");
    let bell = Doorbell::with_timing(
        wrapper(&home, "hang"),
        directory.clone(),
        Duration::ZERO,
        if cancel {
            Duration::from_secs(8)
        } else {
            Duration::from_secs(2)
        },
    );
    bell.watch(BANK, adapter.clone(), true).await;
    bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    wait_for(|| home.0.join("ticks").exists() && home.0.join("observed-worker.pid").exists()).await;
    let pid: u32 = std::fs::read_to_string(home.0.join("worker.pid"))
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(
        std::fs::read_to_string(home.0.join("observed-worker.pid")).unwrap(),
        pid.to_string()
    );
    assert!(!terminated(pid), "worker was not alive before cleanup");
    if cancel {
        bell.close().await;
    } else {
        wait_for(|| terminated(pid)).await;
        bell.close().await;
    }
    assert!(
        terminated(pid),
        "native orphan worker still alive after cleanup"
    );
    let leader: u32 = std::fs::read_to_string(home.0.join("leader.pid"))
        .unwrap()
        .parse()
        .unwrap();
    assert!(
        terminated(leader),
        "native launcher still alive after cleanup"
    );
    cleanup_evidence(
        json!({"scenario":if cancel {"cancel"} else {"timeout"},"platform":std::env::consts::OS,"leader_pid":leader,"worker_pid":pid,"worker_alive_before_cleanup":true,"parent_reported_worker_pid_matches":true,"leader_terminated":true,"orphan_worker_terminated":true}),
    );
    let pending = PendingNotice::new(&directory, BANK).unwrap();
    assert!(pending.path.exists());
    assert_eq!(pending.resolution(now()), None);
    assert!(!pending.path.with_extension("bell-accepted").exists());
    adapter.close().await;
}

#[tokio::test]
async fn native_successful_launcher_worker_survives_queue_and_close() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let _stop = StopWorker(home.0.clone());
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let directory = home.0.join("notices");
    let bell = Doorbell::with_timing(
        wrapper(&home, "success"),
        directory.clone(),
        Duration::ZERO,
        Duration::from_secs(2),
    );
    bell.watch(BANK, adapter.clone(), true).await;
    bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    let pending = PendingNotice::new(&directory, BANK).unwrap();
    wait_for(|| pending.path.with_extension("bell-accepted").exists()).await;
    wait_for(|| home.0.join("ticks").exists()).await;
    let before = std::fs::read_to_string(home.0.join("ticks")).unwrap();
    bell.close().await;
    wait_for(|| std::fs::read_to_string(home.0.join("ticks")).is_ok_and(|ticks| ticks != before))
        .await;
    assert_eq!(pending.resolution(now()), None);
    std::fs::write(home.0.join("stop"), "stop").unwrap();
    let pid = std::fs::read_to_string(home.0.join("worker.pid"))
        .unwrap()
        .parse()
        .unwrap();
    wait_for(|| terminated(pid)).await;
    cleanup_evidence(
        json!({"scenario":"success","platform":std::env::consts::OS,"worker_pid":pid,"worker_ticked_after_close":true,"worker_terminated_after_fixture_stop":true}),
    );
    adapter.close().await;
}

#[test]
fn resolver_exact_override_absolute_path_and_directory_matrix() {
    let home = Home::new();
    let suffix = if cfg!(windows) { "codex.exe" } else { "codex" };
    let path = home.0.join(suffix);
    executable(&path, "fixture");
    let mut env = HashMap::from([
        ("PATH", format!("  \"{}\"  ", home.0.display())),
        ("PATHEXT", ".EXE".into()),
    ]);
    assert_eq!(
        resolve_command(|key| env.get(key).cloned()),
        Some(
            home.0
                .join(if cfg!(windows) { "codex.EXE" } else { "codex" })
        )
    );
    for explicit in [
        "relative/codex".into(),
        home.0.join("missing").to_string_lossy().into_owned(),
        home.0.to_string_lossy().into_owned(),
        format!("\"{}\"", path.display()),
    ] {
        env.insert("PSEUDOLIFE_CODEX_BIN", explicit);
        assert_eq!(resolve_command(|key| env.get(key).cloned()), None);
    }
    env.insert("PSEUDOLIFE_CODEX_BIN", format!("  {}  ", path.display()));
    assert_eq!(resolve_command(|key| env.get(key).cloned()), Some(path));
    env.insert("PSEUDOLIFE_CODEX_BIN", "  ".into());
    assert!(resolve_command(|key| env.get(key).cloned()).is_some());
    env.insert("PATH", ".".into());
    assert_eq!(resolve_command(|key| env.get(key).cloned()), None);
}

#[cfg(windows)]
#[test]
fn resolver_pathext_and_quoted_newest_desktop_fallback_matrix() {
    let home = Home::new();
    let native = home.0.join("codex.exe");
    let batch = home.0.join("codex.cmd");
    executable(&native, "fixture");
    executable(&batch, "fixture");
    let mut env = HashMap::from([
        ("PATH", home.0.to_string_lossy().into_owned()),
        ("PATHEXT", ".PY;.CMD;.EXE".into()),
    ]);
    assert_eq!(
        resolve_command(|key| env.get(key).cloned()),
        Some(home.0.join("codex.CMD"))
    );
    env.insert("PATHEXT", "".into());
    assert_eq!(
        resolve_command(|key| env.get(key).cloned()),
        Some(home.0.join("codex.EXE"))
    );
    env.insert("PATHEXT", ".PY;. EXE".into());
    assert_eq!(resolve_command(|key| env.get(key).cloned()), None);
    env.insert("PATH", "".into());
    let root = home.0.join("OpenAI/Codex/bin");
    let old = root.join("old/codex.exe");
    let new = root.join("new/codex.exe");
    for path in [&old, &new] {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        executable(path, "fixture");
    }
    let stamp = std::time::SystemTime::now();
    std::fs::File::options()
        .write(true)
        .open(&old)
        .unwrap()
        .set_times(std::fs::FileTimes::new().set_modified(stamp - Duration::from_secs(30)))
        .unwrap();
    env.insert("LOCALAPPDATA", format!("  \"{}\"  ", home.0.display()));
    assert_eq!(resolve_command(|key| env.get(key).cloned()), Some(new));
    env.insert("LOCALAPPDATA", ".".into());
    assert_eq!(resolve_command(|key| env.get(key).cloned()), None);
}

#[cfg(unix)]
#[test]
fn resolver_path_requires_executable_but_explicit_override_only_requires_file() {
    use std::os::unix::fs::PermissionsExt;
    let home = Home::new();
    let path = home.0.join("codex");
    executable(&path, "fixture");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut env = HashMap::from([("PATH", home.0.to_string_lossy().into_owned())]);
    assert_eq!(resolve_command(|key| env.get(key).cloned()), None);
    env.insert("PSEUDOLIFE_CODEX_BIN", path.to_string_lossy().into_owned());
    assert_eq!(resolve_command(|key| env.get(key).cloned()), Some(path));
}

#[tokio::test]
async fn shown_attach_is_baseline_and_successful_receive_covers_only_current_arrival() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let command = home
        .0
        .join(if cfg!(windows) { "codex.cmd" } else { "codex" });
    executable(
        &command,
        if cfg!(windows) {
            "@exit /b 0\r\n"
        } else {
            "#!/bin/sh\nexit 0\n"
        },
    );
    let directory = home.0.join("notices");
    let bell = Doorbell::with_timing(
        command,
        directory.clone(),
        Duration::ZERO,
        Duration::from_secs(2),
    );
    bell.watch(BANK, adapter.clone(), true).await;
    let baseline = adapter.view().await;
    let mut offered = baseline.clone();
    offered.wake = view(&[], 1, baseline.watermark, 1.0).wake;
    offered.wake_watermark = Some(baseline.watermark);
    bell.observe(BANK, &offered).await;
    let pending = PendingNotice::new(&directory, BANK).unwrap();
    assert!(
        !pending.path.exists(),
        "shown baseline was mistaken for an arrival"
    );
    let mut withheld = view(&[1, 2], 2, 2, 1.0);
    withheld.wake = None;
    bell.observe(BANK, &withheld).await;
    bell.note_call(BANK, "memory_message", &json!({"action":"receive"}), true)
        .await;
    bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    assert!(
        !pending.path.exists(),
        "successful receive did not cover the current arrival"
    );
    bell.observe(BANK, &view(&[1, 2, 3], 3, 3, 1.0)).await;
    wait_for(|| pending.path.with_extension("bell-accepted").exists()).await;
    assert!(state::read(&pending.path, 8192).is_ok());
    bell.close().await;
    adapter.close().await;
}

struct Harness {
    _fixture: Fixture,
    home: Home,
    adapter: std::sync::Arc<Adapter>,
    bell: std::sync::Arc<Doorbell>,
    pending: PendingNotice,
}
impl Harness {
    async fn new(shown: bool, quiet: Duration) -> Self {
        let fixture = Fixture::new(0);
        let home = Home::new();
        let adapter = Adapter::enter(adapter_config(&fixture, &home))
            .await
            .unwrap();
        let command = home
            .0
            .join(if cfg!(windows) { "codex.cmd" } else { "codex" });
        let log = home.0.join("queues");
        let body = if cfg!(windows) {
            format!(
                "@echo off\r\n>>\"{}\" echo queued\r\nexit /b 0\r\n",
                log.display()
            )
        } else {
            format!("#!/bin/sh\nprintf 'queued\\n' >> '{}'\n", log.display())
        };
        executable(&command, &body);
        let directory = home.0.join("notices");
        let pending = PendingNotice::new(&directory, BANK).unwrap();
        let bell = Doorbell::with_timing(command, directory, quiet, Duration::from_secs(2));
        bell.watch(BANK, adapter.clone(), shown).await;
        Self {
            _fixture: fixture,
            home,
            adapter,
            bell,
            pending,
        }
    }
    async fn accepted(&self) -> Value {
        wait_for(|| self.pending.path.with_extension("bell-accepted").exists()).await;
        serde_json::from_slice(&state::read(&self.pending.path, 8192).unwrap()).unwrap()
    }
    fn calls(&self) -> usize {
        std::fs::read_to_string(self.home.0.join("queues"))
            .unwrap_or_default()
            .lines()
            .count()
    }
    fn ledger(&self) -> Vec<Vec<String>> {
        std::fs::read_to_string(self.pending.path.parent().unwrap().join("ledger.log"))
            .unwrap_or_default()
            .lines()
            .map(|line| line.split('\t').map(str::to_owned).collect())
            .collect()
    }
    async fn close(&self) {
        self.bell.close().await;
        self.adapter.close().await;
    }
}

#[tokio::test]
async fn quiet_tool_activity_and_delivered_hint_do_not_spend_a_future_offer() {
    let rig = Harness::new(true, Duration::from_millis(80)).await;
    let offered = view(&[1, 2], 2, 2, 1.0);
    rig.bell.observe(BANK, &offered).await;
    assert!(!rig.pending.path.exists());
    tokio::time::sleep(Duration::from_millis(90)).await;
    rig.bell
        .note_call(BANK, "memory_search", &json!({}), false)
        .await;
    rig.bell.observe(BANK, &offered).await;
    assert!(!rig.pending.path.exists());
    tokio::time::sleep(Duration::from_millis(90)).await;
    let mut informed = offered.clone();
    informed.delivered = 2;
    rig.bell.observe(BANK, &informed).await;
    rig.bell.observe(BANK, &offered).await;
    assert!(!rig.pending.path.exists());
    rig.bell.observe(BANK, &view(&[1, 2, 3], 3, 3, 1.0)).await;
    rig.accepted().await;
    assert_eq!(rig.calls(), 1);
    rig.close().await;
}

#[tokio::test]
async fn fallback_watch_rings_initial_mail_but_only_successful_receive_covers_arrival() {
    let rig = Harness::new(false, Duration::ZERO).await;
    let offered = view(&[], 1, 1, 1.0);
    rig.bell
        .note_call(BANK, "memory_message", &json!({"action":"receive"}), false)
        .await;
    rig.bell.observe(BANK, &offered).await;
    rig.accepted().await;
    assert_eq!(rig.calls(), 1);
    rig.close().await;
}

#[tokio::test]
async fn mailbox_receive_ack_empty_and_withdrawal_never_resolve_native_queue() {
    let rig = Harness::new(true, Duration::ZERO).await;
    rig.bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    let record = rig.accepted().await;
    let original = state::read(&rig.pending.path, 8192).unwrap();
    for action in ["receive", "ack"] {
        rig.bell
            .note_call(BANK, "memory_message", &json!({"action":action}), true)
            .await;
        let mut empty = view(&[], 0, 3, 1.0);
        empty.wake = None;
        empty.wake_watermark = None;
        rig.bell.observe(BANK, &empty).await;
        rig.bell.observe(BANK, &view(&[4], 1, 4, 2.0)).await;
        assert_eq!(state::read(&rig.pending.path, 8192).unwrap(), original);
        assert_eq!(rig.pending.resolution(now()), None);
    }
    assert_eq!(rig.calls(), 1);
    assert!(!rig.pending.note_prompt(
        &json!({"session_id":BANK,"prompt":"ordinary user turn"}),
        now()
    ));
    assert!(
        rig.pending
            .note_prompt(&json!({"session_id":BANK,"prompt":record["text"]}), now())
    );
    rig.bell.observe(BANK, &view(&[4, 5], 2, 5, 3.0)).await;
    wait_for(|| rig.calls() == 2).await;
    let next: Value =
        serde_json::from_slice(&state::read(&rig.pending.path, 8192).unwrap()).unwrap();
    assert_ne!(next["nonce"], record["nonce"]);
    rig.close().await;
}

#[tokio::test]
async fn arrival_preview_complete_and_incomplete_ack_expiry_matrix() {
    // Complete previews detect a simultaneous arrival and acknowledgement.
    let rig = Harness::new(true, Duration::ZERO).await;
    let mut baseline = view(&[1, 2], 2, 2, 1.0);
    baseline.wake = None;
    rig.bell.observe(BANK, &baseline).await;
    rig.bell
        .note_call(BANK, "memory_message", &json!({"action":"receive"}), true)
        .await;
    rig.bell.observe(BANK, &view(&[3], 1, 3, 1.0)).await;
    let record = rig.accepted().await;
    assert_eq!(record["count"], 1);
    rig.close().await;
    // Incomplete previews may expose old unseen messages as mail is acked.
    let rig = Harness::new(true, Duration::ZERO).await;
    let mut backlog = view(&[10, 11, 12, 13, 14], 8, 2, 1.0);
    backlog.wake = None;
    rig.bell.observe(BANK, &backlog).await;
    rig.bell
        .note_call(BANK, "memory_message", &json!({"action":"receive"}), true)
        .await;
    rig.bell
        .observe(BANK, &view(&[11, 12, 13, 14, 2], 7, 3, 1.0))
        .await;
    rig.bell
        .observe(BANK, &view(&[11, 12, 13, 14, 3], 7, 4, 1.0))
        .await;
    assert!(!rig.pending.path.exists());
    rig.bell
        .observe(BANK, &view(&[11, 12, 13, 14, 20], 7, 5, 1.0))
        .await;
    rig.accepted().await;
    assert_eq!(rig.calls(), 1);
    rig.close().await;
}

#[tokio::test]
async fn timed_offer_withdrawal_and_attention_permission_matrix() {
    let rig = Harness::new(true, Duration::ZERO).await;
    let mut offered = view(&[1, 2], 2, 2, now() + 0.12);
    rig.bell.observe(BANK, &offered).await;
    assert!(!rig.pending.path.exists());
    let mut withdrawn = offered.clone();
    withdrawn.wake = None;
    withdrawn.wake_watermark = None;
    rig.bell.observe(BANK, &withdrawn).await;
    tokio::time::sleep(Duration::from_millis(140)).await;
    rig.bell.observe(BANK, &withdrawn).await;
    assert!(!rig.pending.path.exists());
    for wake in [
        json!({"decision":"withheld","reason":"fixture","ring_at":1.0}),
        json!({"decision":"attention","reason":"fixture","ring_at":1.0,"queue_allowed":false,"recipient_state":"unknown"}),
        json!({"decision":"attention","reason":"fixture","ring_at":1.0,"queue_allowed":true,"recipient_state":"busy"}),
        json!({"decision":"rung","reason":"fixture","ring_at":1.0,"message_expires_at":true}),
    ] {
        offered.wake = Some(wake);
        rig.bell.observe(BANK, &offered).await;
        assert!(!rig.pending.path.exists());
    }
    offered.wake = Some(
        json!({"decision":"attention","reason":"fixture","ring_at":1.0,"queue_allowed":true,"recipient_state":"unknown","message_expires_at":now()+3600.0}),
    );
    rig.bell.observe(BANK, &offered).await;
    let record = rig.accepted().await;
    assert_eq!(record["recipient_state"], "unknown");
    assert!(
        record["text"]
            .as_str()
            .unwrap()
            .contains("Continue the original task even if nothing is pending.")
    );
    assert!(!record["text"].as_str().unwrap().contains("peer payload"));
    rig.close().await;
}

#[tokio::test]
async fn failed_reservation_restores_offer_without_phantom_outstanding() {
    use fs2::FileExt;
    let rig = Harness::new(true, Duration::ZERO).await;
    state::private_dir(rig.pending.path.parent().unwrap()).unwrap();
    let lock = state::create(&rig.pending.path.with_extension("bell-lock"))
        .unwrap()
        .file;
    lock.lock_exclusive().unwrap();
    let offered = view(&[1, 2], 2, 2, 1.0);
    rig.bell.observe(BANK, &offered).await;
    assert!(!rig.pending.path.exists());
    assert_eq!(rig.calls(), 0);
    let ledger = rig.ledger();
    assert_eq!(ledger.len(), 1);
    assert_eq!(ledger[0][1], "bell_deferred");
    assert_eq!(ledger[0][4], "0");
    assert_eq!(ledger[0][5], "reservation_unavailable no_queue_attempt");
    FileExt::unlock(&lock).unwrap();
    rig.bell.observe(BANK, &offered).await;
    rig.accepted().await;
    assert_eq!(rig.calls(), 1);
    rig.close().await;
    let ledger = rig.ledger();
    assert_eq!(ledger.len(), 2);
    assert_eq!(ledger[1][1], "bell");
    assert_eq!(
        ledger[1][5],
        "rung fixture queue_accepted pending recipient_state_unknown"
    );
}

#[test]
fn native_queue_environment_strips_all_pseudolife_keys_and_preserves_codex_home() {
    let home = Home::new();
    let mut command = Command::new(std::env::current_exe().unwrap());
    for (key, _) in std::env::vars_os() {
        if key
            .to_string_lossy()
            .to_uppercase()
            .starts_with("PSEUDOLIFE_")
        {
            command.env_remove(key);
        }
    }
    command
        .args(["--exact", "queue_environment_child", "--nocapture"])
        .env("DOORBELL_ENV_TEST", "1")
        .env("PSEUDOLIFE_MCP_TOKEN", "fixture-bank-sentinel")
        .env("pSeUdOlIfE_fixture", "fixture-host-sentinel")
        .env("PSEUDOLIFE_CODEX_BIN", "fixture-command-sentinel")
        .env("DOORBELL_FIXTURE_KEEP", "fixture-value")
        .env("CODEX_HOME", &home.0)
        .env("DOORBELL_EXPECTED_CODEX_HOME", &home.0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    assert!(
        command.status().unwrap().success(),
        "isolated native environment proof failed"
    );
}

#[test]
fn queue_environment_child() {
    if std::env::var_os("DOORBELL_ENV_TEST").is_none() {
        return;
    }
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let fixture = Fixture::new(0);
            let home = Home::new();
            let adapter = Adapter::enter(adapter_config(&fixture, &home))
                .await
                .unwrap();
            let directory = home.0.join("notices");
            let bell = Doorbell::with_timing(
                wrapper(&home, "environment"),
                directory.clone(),
                Duration::ZERO,
                Duration::from_secs(2),
            );
            bell.watch(BANK, adapter.clone(), true).await;
            bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
            let pending = PendingNotice::new(&directory, BANK).unwrap();
            wait_for(|| pending.path.with_extension("bell-accepted").exists()).await;
            let proof: Value =
                serde_json::from_slice(&std::fs::read(home.0.join("environment-proof")).unwrap())
                    .unwrap();
            assert_eq!(
                proof,
                json!({"clean":true,"home_kept":true,"unrelated_kept":true})
            );
            bell.close().await;
            adapter.close().await;
        });
}

#[tokio::test]
async fn subscription_coalesces_mailbox_changes_and_timed_offer_reaches_queue() {
    let rig = Harness::new(true, Duration::from_millis(80)).await;
    let mut update = json!({"generation":1,"pending_count":2,"pending_preview":[
        {"message_id":"m1","sender_agent_id":"fixture-peer","sender_label":"peer","excerpt":"peer payload","created_at":1001.0},
        {"message_id":"m2","sender_agent_id":"fixture-peer","sender_label":"peer","excerpt":"peer payload","created_at":1002.0}],
        "wake":{"decision":"rung","reason":"fixture","ring_at":now()+0.15,"message_expires_at":now()+3600.0}});
    rig.adapter.update_mailbox(&update).await;
    update["pending_count"] = json!(3);
    update["pending_preview"].as_array_mut().unwrap().push(json!({"message_id":"m3","sender_agent_id":"fixture-peer","sender_label":"peer","excerpt":"peer payload","created_at":1003.0}));
    update["wake"]["ring_at"] = json!(now() + 0.18);
    rig.adapter.update_mailbox(&update).await;
    assert!(!rig.pending.path.exists());
    wait_for(|| rig.home.0.join("digest.ring").exists()).await;
    // Python rechecks quiet and ring_due on the next mailbox heartbeat.
    rig.adapter.update_mailbox(&update).await;
    let record = rig.accepted().await;
    assert_eq!(record["count"], 3);
    assert_eq!(rig.calls(), 1);
    rig.close().await;
}

#[tokio::test]
async fn watch_replacement_invalid_thread_and_closed_boundary_do_not_queue() {
    let rig = Harness::new(false, Duration::ZERO).await;
    rig.bell.watch(BANK, rig.adapter.clone(), true).await;
    rig.bell
        .watch("NOT-A-CANONICAL-THREAD", rig.adapter.clone(), false)
        .await;
    rig.bell
        .note_call(
            "NOT-A-CANONICAL-THREAD",
            "memory_message",
            &json!({"action":"receive"}),
            true,
        )
        .await;
    rig.bell
        .observe("NOT-A-CANONICAL-THREAD", &view(&[1, 2], 2, 2, 1.0))
        .await;
    rig.bell.observe(BANK, &view(&[], 1, 1, 1.0)).await;
    assert!(
        !rig.pending.path.exists(),
        "replacement shown watch retained fallback arrival"
    );
    rig.bell.close().await;
    rig.bell.watch(BANK, rig.adapter.clone(), false).await;
    rig.bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    assert!(!rig.pending.path.exists());
    assert_eq!(rig.calls(), 0);
    rig.adapter.close().await;
}

#[tokio::test]
async fn restart_preserves_notice_and_exact_prompt_is_required_for_rearming() {
    let rig = Harness::new(true, Duration::ZERO).await;
    rig.bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    let first = rig.accepted().await;
    rig.bell.close().await;
    let directory = rig.pending.path.parent().unwrap().to_path_buf();
    let command = rig
        .home
        .0
        .join(if cfg!(windows) { "codex.cmd" } else { "codex" });
    let next = Doorbell::with_timing(command, directory, Duration::ZERO, Duration::from_secs(2));
    next.watch(BANK, rig.adapter.clone(), false).await;
    next.note_call(BANK, "memory_message", &json!({"action":"receive"}), true)
        .await;
    next.observe(BANK, &view(&[1, 2, 3], 3, 3, 2.0)).await;
    assert_eq!(rig.calls(), 1);
    assert_eq!(rig.pending.resolution(now()), None);
    assert!(!rig.pending.note_prompt(
        &json!({"session_id":"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb","prompt":first["text"]}),
        now()
    ));
    assert!(!rig.pending.note_prompt(
        &json!({"session_id":BANK,"prompt":format!("{} trailing",first["text"].as_str().unwrap())}),
        now()
    ));
    assert!(
        rig.pending
            .note_prompt(&json!({"session_id":BANK,"prompt":first["text"]}), now())
    );
    next.observe(BANK, &view(&[1, 2, 3, 4], 4, 4, 3.0)).await;
    wait_for(|| rig.calls() == 2).await;
    next.close().await;
    rig.adapter.close().await;
}

#[tokio::test]
async fn nonzero_native_exit_disables_queue_and_retains_unresolved_reservation() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let command = home
        .0
        .join(if cfg!(windows) { "codex.cmd" } else { "codex" });
    let exit = home.0.join("started");
    executable(
        &command,
        &if cfg!(windows) {
            format!(
                "@echo off\r\n>\"{}\" echo started\r\nexit /b 23\r\n",
                exit.display()
            )
        } else {
            format!("#!/bin/sh\necho started > '{}'\nexit 23\n", exit.display())
        },
    );
    let directory = home.0.join("notices");
    let pending = PendingNotice::new(&directory, BANK).unwrap();
    let bell = Doorbell::with_timing(command, directory, Duration::ZERO, Duration::from_secs(2));
    bell.watch(BANK, adapter.clone(), true).await;
    bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    wait_for(|| exit.exists()).await;
    tokio::time::sleep(Duration::from_millis(60)).await;
    let before = state::read(&pending.path, 8192).unwrap();
    assert_eq!(pending.resolution(now()), None);
    assert!(!pending.path.with_extension("bell-accepted").exists());
    bell.observe(BANK, &view(&[1, 2, 3], 3, 3, 2.0)).await;
    assert_eq!(state::read(&pending.path, 8192).unwrap(), before);
    // A disabled queue still lazily resolves an origin-expired reservation.
    let mut record: Value = serde_json::from_slice(&before).unwrap();
    record["expires_at"] = json!(now() - 1.0);
    state::atomic_write(&pending.path, record.to_string().as_bytes(), None).unwrap();
    bell.observe(BANK, &view(&[1, 2, 3], 3, 3, 3.0)).await;
    assert!(!pending.path.exists());
    let receipt: Value = serde_json::from_slice(
        &state::read(
            &pending.path.with_extension("bell-unresolved-expired"),
            8192,
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(receipt["nonce"], record["nonce"]);
    assert_eq!(receipt["native_cancellation"], "unknown");
    bell.close().await;
    let ledger =
        std::fs::read_to_string(pending.path.parent().unwrap().join("ledger.log")).unwrap();
    let rows = ledger.lines().collect::<Vec<_>>();
    assert_eq!(rows.len(), 1);
    assert!(rows[0].contains("\tunresolved_expired\t"));
    assert!(
        rows[0]
            .ends_with("availability_fallback native_cancellation_unknown origin_message_expiry")
    );
    adapter.close().await;
}

#[tokio::test]
async fn external_unresolved_reservation_reports_pending_then_expiry_once() {
    for legacy in [false, true] {
        let rig = Harness::new(true, Duration::ZERO).await;
        let record = rig
            .pending
            .reserve(1, Some(now() + 3600.0), now(), false)
            .unwrap();
        let offered = view(&[1, 2], 2, 2, 1.0);
        rig.bell.observe(BANK, &offered).await;
        assert_eq!(rig.calls(), 0);
        assert_eq!(rig.ledger()[0][1], "bell_pending");
        assert_eq!(rig.ledger()[0][5], "unresolved queue transport");
        let mut expired = record.clone();
        expired["expires_at"] = json!(now() - 1.0);
        if legacy {
            expired["expiry_basis"] = json!("legacy_upper_bound");
            expired["legacy_first_seen"] = json!(expired["expires_at"].as_f64().unwrap() - 86400.0);
        }
        state::atomic_write(&rig.pending.path, expired.to_string().as_bytes(), None).unwrap();
        rig.bell.observe(BANK, &offered).await;
        rig.accepted().await;
        rig.bell.observe(BANK, &offered).await;
        rig.close().await;
        let rows = rig.ledger();
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[1][1], "unresolved_expired");
        assert_eq!(rows[1][4], "0");
        assert_eq!(
            rows[1][5],
            if legacy {
                "availability_fallback native_cancellation_unknown origin_expiry_unknown legacy_upper_bound"
            } else {
                "availability_fallback native_cancellation_unknown origin_message_expiry"
            }
        );
        assert_eq!(rows[2][1], "bell");
        let key = pseudolife_stdio::board::identity::hex_hash(BANK);
        for row in &rows {
            assert_eq!(row.len(), 6);
            assert!(row[0].parse::<u64>().is_ok());
            assert_eq!(row[2], key[..8]);
            assert_eq!(row[3], rig.adapter.view().await.watermark.to_string());
        }
        assert_ne!(rig.accepted().await["nonce"], record["nonce"]);
    }
}

#[tokio::test]
async fn native_acceptance_ledger_reports_prompt_hook_arriving_first() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let directory = home.0.join("notices");
    let pending = PendingNotice::new(&directory, BANK).unwrap();
    let bell = Doorbell::with_timing(
        wrapper(&home, "receipt"),
        directory.clone(),
        Duration::ZERO,
        Duration::from_secs(3),
    );
    bell.watch(BANK, adapter.clone(), true).await;
    let mut offered = view(&[1, 2], 2, 2, 1.0);
    offered.wake.as_mut().unwrap()["reason"] = json!("  fixture\n[] \tready!  ");
    bell.observe(BANK, &offered).await;
    wait_for(|| home.0.join("waiting").exists()).await;
    let record: Value = serde_json::from_slice(&state::read(&pending.path, 8192).unwrap()).unwrap();
    assert!(pending.note_prompt(&json!({"session_id":BANK,"prompt":record["text"]}), now()));
    std::fs::write(home.0.join("release"), "release").unwrap();
    wait_for(|| pending.path.with_extension("bell-accepted").exists()).await;
    bell.close().await;
    let ledger = std::fs::read_to_string(directory.join("ledger.log")).unwrap();
    let rows = ledger.lines().collect::<Vec<_>>();
    assert_eq!(rows.len(), 1);
    let row = rows[0].split('\t').collect::<Vec<_>>();
    assert_eq!(row[1], "bell");
    assert_eq!(
        row[4],
        (record["text"].as_str().unwrap().chars().count() + 1).to_string()
    );
    assert_eq!(
        row[5],
        "rung fixture  ready queue_accepted prompt_seen recipient_state_unknown"
    );
    adapter.close().await;
}

#[tokio::test]
async fn native_wrapper_receives_canonical_thread_and_exact_fixed_nonce_notice() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let log = home.0.join("argv");
    let command = home
        .0
        .join(if cfg!(windows) { "codex.cmd" } else { "codex" });
    let body = if cfg!(windows) {
        format!(
            "@echo off\r\n>\"{}\" (\r\necho %~1\r\necho %~2\r\necho %~3\r\necho %~4\r\necho %~5\r\n)\r\nexit /b 0\r\n",
            log.display()
        )
    } else {
        format!("#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}'\n", log.display())
    };
    executable(&command, &body);
    let directory = home.0.join("notices");
    let pending = PendingNotice::new(&directory, BANK).unwrap();
    let bell = Doorbell::with_timing(command, directory, Duration::ZERO, Duration::from_secs(2));
    bell.watch(BANK, adapter.clone(), true).await;
    bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    wait_for(|| pending.path.with_extension("bell-accepted").exists()).await;
    bell.close().await;
    let record: Value = serde_json::from_slice(&state::read(&pending.path, 8192).unwrap()).unwrap();
    let text = std::fs::read_to_string(&log).unwrap();
    assert_eq!(
        text.lines().collect::<Vec<_>>(),
        vec![
            "queue",
            "--thread",
            BANK,
            "--message",
            record["text"].as_str().unwrap()
        ]
    );
    assert!(!text.contains("peer payload"));
    assert_eq!(record["nonce"].as_str().unwrap().len(), 32);
    adapter.close().await;
}

#[tokio::test]
async fn reserve_retires_external_expiry_and_prompt_resolution_never_relogs_old_expiry() {
    let rig = Harness::new(true, Duration::ZERO).await;
    let mut old = rig
        .pending
        .reserve(1, Some(now() + 3600.0), now(), false)
        .unwrap();
    old["expires_at"] = json!(now() - 1.0);
    state::atomic_write(&rig.pending.path, old.to_string().as_bytes(), None).unwrap();
    rig.bell.observe(BANK, &view(&[1, 2], 2, 2, 1.0)).await;
    let first = rig.accepted().await;
    wait_for(|| rig.ledger().len() == 2).await;
    assert_eq!(rig.ledger()[0][1], "unresolved_expired");
    assert_eq!(rig.ledger()[1][1], "bell");
    assert!(
        rig.pending
            .note_prompt(&json!({"session_id":BANK,"prompt":first["text"]}), now())
    );
    rig.bell.observe(BANK, &view(&[1, 2, 3], 3, 3, 2.0)).await;
    wait_for(|| rig.calls() == 2).await;
    wait_for(|| rig.ledger().len() == 3).await;
    rig.close().await;
    let ledger = rig.ledger();
    assert_eq!(ledger.len(), 3);
    assert_eq!(
        ledger
            .iter()
            .filter(|row| row[1] == "unresolved_expired")
            .count(),
        1
    );
}

#[cfg(windows)]
#[test]
fn resolver_pathext_preserves_supported_extensions_and_path_directory_order() {
    let home = Home::new();
    let first = home.0.join("first");
    let second = home.0.join("second");
    std::fs::create_dir(&first).unwrap();
    std::fs::create_dir(&second).unwrap();
    executable(&second.join("codex.EXE"), "fixture");
    for extension in [".COM", ".BAT", ".CMD", ".EXE"] {
        let selected = first.join(format!("codex{extension}"));
        executable(&selected, "fixture");
        let env = HashMap::from([
            (
                "PATH",
                format!(
                    ".;relative;\\bin;C:bin;{};{}",
                    first.display(),
                    second.display()
                ),
            ),
            ("PATHEXT", format!(".PY;{extension};.EXE")),
        ]);
        assert_eq!(
            resolve_command(|key| env.get(key).cloned()),
            Some(selected.clone())
        );
        std::fs::remove_file(selected).unwrap();
    }
}
