use super::*;
use std::{io::BufRead, net::TcpListener, thread, time::Instant};

struct Home(PathBuf);

impl Home {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("tunnel-run-{}", Uuid::new_v4()));
        state::private_dir(&root).unwrap();
        Self(root)
    }
}

impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn prepared(root: &Path) -> Prepared<'_> {
    let payload = json!({
        "name":"dot", "daemon_url":"http://127.0.0.1:9", "token_file":root.join("daemon-token"),
        "state":"ready", "consent":true, "tunnel_id":"tunnel_abcdef"
    });
    let profile_bytes = serde_json::to_vec(&payload).unwrap();
    let (_, profile) = store::decode_profile(&profile_bytes, "dot").unwrap();
    let locks = launch_locks(root, "dot").unwrap();
    Prepared {
        root,
        profile,
        profile_bytes,
        key: key::Key::fixture(),
        previous_key: false,
        _locks: locks,
    }
}

fn launch_fixture(root: &Path) -> Prepared<'_> {
    state::atomic_create(&root.join("daemon-token"), b"fixture-daemon-token").unwrap();
    prepared(root)
}

const FIXTURE: &str = "cli::tunnel::runtime::tests::vendor_fixture";
const CONFIG_ENV: &str = "PSEUDOLIFE_TEST_TUNNEL_VENDOR_CONFIG";
const FIXTURE_WATCHDOG: Duration = Duration::from_secs(90);

fn wait_for_ready(path: &Path, mut finished: impl FnMut() -> bool) -> Option<LaunchRecord> {
    // Hashing and staging are outside the supervisor's readiness deadline.
    // Wait for the published state or exit. This bound only limits a hung
    // test fixture, it does not measure startup performance.
    let watchdog = Instant::now() + FIXTURE_WATCHDOG;
    loop {
        if let Ok(record) = read_record(path)
            && record.ready
        {
            return Some(record);
        }
        if finished() || Instant::now() >= watchdog {
            return None;
        }
        thread::sleep(Duration::from_millis(20));
    }
}

fn fixture_command(exe: &Path, config: &Path, mode: &str) -> Command {
    let mut command = Command::new(exe);
    command
        .args(["--exact", FIXTURE, "--nocapture"])
        .env_clear()
        .envs(vendor::probe_environment(std::env::vars_os()))
        .env(CONFIG_ENV, config)
        .env("PSEUDOLIFE_TEST_TUNNEL_VENDOR_MODE", mode);
    command
}

#[test]
fn vendor_fixture() {
    let Some(path) = std::env::var_os(CONFIG_ENV) else {
        return;
    };
    let mode = std::env::var("PSEUDOLIFE_TEST_TUNNEL_VENDOR_MODE").unwrap();
    #[cfg(any(target_os = "linux", windows))]
    if mode == "supervisor" {
        let root = PathBuf::from(path);
        let exe = std::env::current_exe().unwrap();
        let launched = launch(launch_fixture(&root), &exe, STARTUP, |config| {
            fixture_command(&exe, config, "ready")
        });
        assert!(launched.is_ok());
        return;
    }
    if mode == "legacy-lock" {
        let file = state::open_with_write(Path::new(&path), true).unwrap().file;
        assert!(legacy_lock(&file));
        return;
    }
    let config: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    let reference = config["control_plane"]["api_key"].as_str().unwrap();
    let mut bytes = Vec::new();
    File::open(reference.strip_prefix("file:").unwrap())
        .unwrap()
        .read_to_end(&mut bytes)
        .unwrap();
    assert!(bytes == b"fixture-runtime-key");
    assert!(std::env::args_os().all(|arg| !arg.to_string_lossy().contains("fixture-runtime-key")));
    assert!(
        std::env::vars_os().all(|(_, val)| !val.to_string_lossy().contains("fixture-runtime-key"))
    );
    if mode == "early-exit" {
        return;
    }
    let diagnostic = match mode.as_str() {
        "bad-key" => Some("control plane API key is malformed"),
        "unauthorized" => Some("controlplane client: unexpected status 401"),
        "unknown-tunnel" => Some("controlplane client: unexpected metadata status 404"),
        "network" => Some("controlplane client: poll transport failed (network_error)"),
        _ => None,
    };
    if let Some(diagnostic) = diagnostic {
        eprintln!("{diagnostic} [synthetic-untrusted-suffix]");
        return;
    }
    let poll = match mode.as_str() {
        "trial-refused" => Some(
            r#"{"schema_version":1,"component":"control-plane","status":"degraded","details":{"http_status":403}}"#,
        ),
        "trial-qualified" | "trial-early" => Some(
            r#"{"schema_version":1,"component":"control-plane","status":"ok","details":{"last_success":"2026-11-20T00:00:00Z"}}"#,
        ),
        "trial-local" => Some(
            r#"{"schema_version":1,"component":"control-plane","status":"unknown","details":{}}"#,
        ),
        _ => None,
    };
    if mode == "ready" || poll.is_some() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let health = config["health"]["url_file"].as_str().unwrap();
        fs::write(health, format!("http://{}", listener.local_addr().unwrap())).unwrap();
        let watchdog = Instant::now() + FIXTURE_WATCHDOG;
        while Instant::now() < watchdog {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    stream.set_nonblocking(false).unwrap();
                    stream
                        .set_read_timeout(Some(Duration::from_secs(1)))
                        .unwrap();
                    let mut request = String::new();
                    if std::io::BufReader::new(&mut stream)
                        .read_line(&mut request)
                        .is_ok()
                    {
                        let readiness = request.starts_with("GET /readyz HTTP/1.1");
                        let body = if readiness {
                            Some("")
                        } else if request.starts_with("GET /health/control-plane HTTP/1.1") {
                            poll
                        } else {
                            None
                        };
                        // A probe may have timed out while the fixture was
                        // descheduled. The next probe can still get a reply.
                        if let Some(body) = body {
                            let status = if readiness && mode == "trial-early" {
                                "503 Starting"
                            } else {
                                "200 OK"
                            };
                            let _ = write!(
                                stream,
                                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                                body.len()
                            );
                            if matches!(mode.as_str(), "trial-local" | "trial-early")
                                && !body.is_empty()
                            {
                                return;
                            }
                        }
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(20));
                }
                Err(error) => panic!("fixture health accept failed: {error}"),
            }
        }
        return;
    }
    // Bounds a fixture even if a mutation causes the supervisor to lose it.
    thread::sleep(Duration::from_secs(20));
}

#[test]
fn replacement_releases_previous_key_only_after_a_successful_poll() {
    for (mode, confirmed, becomes_ready) in [
        ("trial-refused", false, false),
        ("trial-local", false, false),
        ("trial-qualified", true, true),
        ("trial-early", true, false),
    ] {
        let home = Home::new();
        let mut prepared = launch_fixture(&home.0);
        let mut older: serde_json::Value = serde_json::from_slice(&prepared.profile_bytes).unwrap();
        older["runtime_key_expires_at"] = json!("2020-01-01T00:00:00Z");
        let older = older.to_string();
        let prefix = if cfg!(windows) {
            b"DPAPI\0"
        } else {
            b"PLAIN\0"
        };
        let old_key = [prefix.as_slice(), b"prior opaque fixture"].concat();
        let new_key = [prefix.as_slice(), b"replacement opaque fixture"].concat();
        let profile = home.0.join("dot.profile.json");
        let key = home.0.join("dot.key");
        state::atomic_create(&profile, older.as_bytes()).unwrap();
        state::atomic_create(&key, &old_key).unwrap();
        setup::seed_trial(
            &home.0,
            "dot",
            (older.as_bytes(), &old_key),
            (&prepared.profile_bytes, &new_key),
        )
        .unwrap();
        prepared.previous_key = setup::validate_previous_key(&home.0, "dot").unwrap();
        let marker = home.0.join("dot.key-previous");
        let expected = store::private_read(&marker, 131072).unwrap();
        let exe = std::env::current_exe().unwrap();
        let record_path = record::path(&home.0, "dot");
        let (ready, outcome) = thread::scope(|scope| {
            let run = scope.spawn(|| {
                launch(prepared, &exe, STARTUP, |config| {
                    fixture_command(&exe, config, mode)
                })
            });
            let ready = wait_for_ready(&record_path, || run.is_finished());
            if ready.is_some() {
                stop(&home.0, "dot").unwrap();
            }
            (ready, run.join().unwrap())
        });
        assert_eq!(ready.is_some(), becomes_ready);
        assert!(fs::read(&key).unwrap() == new_key);
        if confirmed {
            assert!(!marker.exists());
            assert_eq!(outcome.is_ok(), becomes_ready);
        } else {
            assert!(store::private_read(&marker, 131072).unwrap() == expected);
            assert!(matches!(&outcome, Err(Fail::Tunnel(message))
                    if message.contains("previous key is retained")
                    && message.contains("pseudolife-mcp tunnel setup --restore-key")
                    && !message.contains("--discard-previous-key")));
            if mode == "trial-refused" {
                assert_eq!(outcome, Err(TRIAL_AUTH));
            }
        }
    }
}

#[test]
fn foreground_launch_owns_one_child_and_stop_waits_for_cleanup() {
    let home = Home::new();
    let exe = std::env::current_exe().unwrap();
    let record_path = record::path(&home.0, "dot");
    let (run, stopped, busy) = thread::scope(|scope| {
        let run = scope.spawn(|| {
            launch(launch_fixture(&home.0), &exe, STARTUP, |config| {
                fixture_command(&exe, config, "ready")
            })
        });
        let ready = wait_for_ready(&record_path, || run.is_finished());
        let Some(ready) = ready else {
            panic!("fixture did not become ready: {:?}", run.join().unwrap());
        };
        let lock = open_lock(&home.0, "dot", &lock_path(&home.0, "dot")).unwrap();
        let busy = matches!(lock.try_lock(), Err(fs::TryLockError::WouldBlock));
        let stage = stage_path(&home.0, launch_id(&ready.native_launch).unwrap());
        let owner = state::open_with_write(&stage.join(OWNER_LOCK), true).unwrap();
        assert!(matches!(
            owner.file.try_lock_shared(),
            Err(fs::TryLockError::WouldBlock)
        ));
        drop(owner);
        let wrong = Uuid::new_v4().simple().to_string();
        state::atomic_create(&stage.join("stop"), wrong.as_bytes()).unwrap();
        let refused = stop(&home.0, "dot");
        assert!(refused.is_err());
        assert!(!run.is_finished());
        fs::remove_file(stage.join("stop")).unwrap();
        let stop = stop(&home.0, "dot");
        let run = run.join().unwrap();
        assert!(!stage.exists());
        (run, stop, busy)
    });
    assert!(busy);
    assert_eq!(run, Ok(("Tunnel dot stopped.\n".into(), 0)));
    assert_eq!(stopped, Ok(("Tunnel dot is stopped.\n".into(), 0)));
    assert!(!record_path.exists());
    assert_eq!(
        stop(&home.0, "dot"),
        Ok(("Tunnel dot is stopped.\n".into(), 0))
    );
}

#[test]
fn early_exit_and_readiness_timeout_release_the_launch_files() {
    let exe = std::env::current_exe().unwrap();
    for (mode, timeout, expected) in [
        ("early-exit", Duration::from_secs(60), NOT_READY),
        ("never-ready", Duration::from_millis(350), READINESS_TIMEOUT),
    ] {
        let home = Home::new();
        let result = launch(launch_fixture(&home.0), &exe, timeout, |config| {
            fixture_command(&exe, config, mode)
        });
        assert_eq!(result, Err(expected));
        let mut names: Vec<_> = fs::read_dir(&home.0)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect();
        names.sort();
        assert_eq!(
            names,
            [
                ".tunnel-identity-key",
                "daemon-token",
                "dot.lock",
                "dot.native-run.lock"
            ]
        );
        assert!(launch_locks(&home.0, "dot").is_ok());
    }
}

#[test]
fn an_early_vendor_failure_names_only_a_bounded_safe_log() {
    let exe = std::env::current_exe().unwrap();
    for (mode, cause) in [
        ("bad-key", "The vendor rejected the runtime key format."),
        (
            "unauthorized",
            "The tunnel control plane refused authentication or access.",
        ),
        (
            "unknown-tunnel",
            "The tunnel control plane could not find the saved tunnel.",
        ),
        (
            "network",
            "The tunnel client could not reach the control plane.",
        ),
    ] {
        let home = Home::new();
        let log = home.0.join("dot.native-runtime.log");
        let result = launch(launch_fixture(&home.0), &exe, STARTUP, |config| {
            fixture_command(&exe, config, mode)
        });
        assert!(matches!(result, Err(Fail::Detail(message))
            if message.contains(cause) && message.contains(&log.display().to_string())
                && !message.contains("synthetic-untrusted-suffix")));
        assert_eq!(
            store::private_read(&log, 1024).unwrap(),
            format!("{cause}\n").into_bytes()
        );
    }
}

#[test]
fn the_runtime_key_must_differ_from_the_private_daemon_credential() {
    let home = Home::new();
    let launch = prepared(&home.0);
    assert_eq!(
        verify_credential(&launch.profile, &launch.key),
        Err(CREDENTIAL)
    );
    let file = home.0.join("daemon-token");
    state::atomic_create(&file, b"fixture-runtime-key").unwrap();
    assert!(
        matches!(verify_credential(&launch.profile, &launch.key), Err(Fail::Tunnel(message))
        if message.contains("matches the daemon credential"))
    );
    state::atomic_write(&file, b"fixture-daemon-credential", None).unwrap();
    assert_eq!(verify_credential(&launch.profile, &launch.key), Ok(()));
}

#[test]
fn a_legacy_or_unlocked_record_cannot_authorize_stop() {
    let home = Home::new();
    let lock = open_lock(&home.0, "dot", &lock_path(&home.0, "dot")).unwrap();
    lock.try_lock().unwrap();
    assert!(stop(&home.0, "dot").is_err());
    drop(lock);
    let path = record::path(&home.0, "dot");
    let bytes = br#"{"pid":1,"ready":true}"#;
    state::atomic_create(&path, bytes).unwrap();
    assert!(
        matches!(stop(&home.0, "dot"), Err(Fail::Detail(message)) if message.contains("cannot prove native ownership"))
    );
    assert_eq!(fs::read(&path).unwrap(), bytes);
    let id = Uuid::new_v4();
    let owner = LaunchRecord {
        native_launch: id.simple().to_string(),
        launch_identity: None,
        spawn_started: None,
        supervisor: Identity {
            pid: std::process::id(),
            exe: std::env::current_exe().unwrap(),
        },
        target: None,
        ready: true,
    };
    state::atomic_write(&path, &serde_json::to_vec(&owner).unwrap(), None).unwrap();
    let _lock = open_lock(&home.0, "dot", &lock_path(&home.0, "dot")).unwrap();
    assert!(
        matches!(stop(&home.0, "dot"), Err(Fail::Detail(message)) if message.contains("no longer holding its launch lock"))
    );
    assert!(!stage_path(&home.0, id).exists());
}

#[test]
#[cfg(any(target_os = "linux", windows))]
fn killed_supervisor_is_recovered_but_live_launch_files_are_preserved() {
    let home = Home::new();
    let fixture = prepared(&home.0);
    state::atomic_create(&home.0.join("dot.profile.json"), &fixture.profile_bytes).unwrap();
    #[cfg(target_os = "linux")]
    state::atomic_create(&home.0.join("dot.key"), b"PLAIN\0fixture-runtime-key").unwrap();
    drop(fixture);
    let mut supervisor = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", FIXTURE])
        .env(CONFIG_ENV, &home.0)
        .env("PSEUDOLIFE_TEST_TUNNEL_VENDOR_MODE", "supervisor")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let path = record::path(&home.0, "dot");
    let live = wait_for_ready(&path, || supervisor.try_wait().unwrap().is_some());
    let evidence = live.map(|record| {
        let stage = stage_path(&home.0, launch_id(&record.native_launch).unwrap());
        let refused = prepare(&home.0, "dot").is_err();
        let key_preserved = stage.join("profile.yaml").is_file()
            && stage.join("key").is_file() == cfg!(target_os = "linux");
        #[cfg(target_os = "linux")]
        let bound = store::load(&stage, "dot").is_ok_and(|profile| {
            identity::Bound::capture(&stage, &record.native_launch, &profile).is_ok()
        });
        #[cfg(windows)]
        let bound = state::open_with_write(&stage.join(OWNER_LOCK), true).is_ok_and(|owner| {
            matches!(
                owner.file.try_lock_shared(),
                Err(fs::TryLockError::WouldBlock)
            )
        });
        (stage, refused, key_preserved, bound)
    });
    let _ = supervisor.kill();
    supervisor.wait().unwrap();
    let (stage, refused, key_preserved, bound) = evidence.expect("fixture never became ready");
    assert!(refused && key_preserved && bound);
    let profile = home.0.join("dot.profile.json");
    let original = fs::read(&profile).unwrap();
    let mut expired: serde_json::Value = serde_json::from_slice(&original).unwrap();
    expired["runtime_key_expires_at"] = json!("2020-01-01T00:00:00Z");
    state::atomic_write(&profile, expired.to_string().as_bytes(), None).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if matches!(prepare(&home.0, "dot"), Err(Fail::Tunnel(KEY_RENEWAL)))
            && !stage.exists()
            && !path.exists()
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "old launch could not be recovered"
        );
        thread::sleep(Duration::from_millis(20));
    }
    assert!(!stage.exists());
    assert!(!path.exists());
    state::atomic_write(&profile, &original, None).unwrap();
    #[cfg(target_os = "linux")]
    assert!(prepare(&home.0, "dot").is_ok());
    #[cfg(windows)]
    assert!(prepare_locks(&home.0, "dot").is_ok());
}

#[test]
fn recovery_preserves_unknown_ownership_before_target_publication() {
    let home = Home::new();
    let _locks = launch_locks(&home.0, "dot").unwrap();
    let id = Uuid::new_v4();
    let directory = stage_path(&home.0, id);
    state::private_dir(&directory).unwrap();
    let key = directory.join("key");
    state::atomic_create(&key, b"fixture-runtime-key").unwrap();
    let mut launch = LaunchRecord {
        native_launch: id.simple().to_string(),
        launch_identity: None,
        spawn_started: Some(true),
        supervisor: Identity {
            pid: u32::MAX,
            exe: std::env::current_exe().unwrap(),
        },
        target: None,
        ready: false,
    };
    let path = record::path(&home.0, "dot");
    state::atomic_create(&path, &serde_json::to_vec(&launch).unwrap()).unwrap();
    assert!(
        matches!(recover_launch(&home.0, "dot"), Err(Fail::Detail(message))
            if message.contains(&directory.display().to_string())
                && message.contains(&path.display().to_string()))
    );
    assert!(key.is_file() && path.is_file());
    launch.spawn_started = Some(false);
    state::atomic_write(&path, &serde_json::to_vec(&launch).unwrap(), None).unwrap();
    #[cfg(not(target_os = "macos"))]
    {
        recover_launch(&home.0, "dot").unwrap();
        assert!(!directory.exists() && !path.exists());
    }
}

#[test]
fn launch_retries_one_transient_lock_probe() {
    let home = Home::new();
    let path = lock_path(&home.0, "dot");
    let probe = open_lock(&home.0, "dot", &path).unwrap();
    let launch = open_lock(&home.0, "dot", &path).unwrap();
    probe.try_lock_shared().unwrap();
    assert!(try_launch_lock(&launch, || drop(probe)).is_ok());
    let contender = open_lock(&home.0, "dot", &path).unwrap();
    let mut retries = 0;
    let busy = try_launch_lock(&contender, || retries += 1);
    assert!(matches!(busy, Err(fs::TryLockError::WouldBlock)));
    assert_eq!(retries, 1);
}

#[test]
fn readiness_accepts_only_the_bounded_literal_loopback_port() {
    let home = Home::new();
    let file = home.0.join("health.url");
    for invalid in [
        "http://localhost:7",
        "http://127.0.0.1:0",
        "http://127.0.0.1:7/a",
        "https://127.0.0.1:7",
        "http://127.0.0.1:7/?key=no",
    ] {
        fs::write(&file, invalid).unwrap();
        assert!(health_url(&file).is_none());
    }
    fs::write(&file, "http://127.0.0.1:1776/\r\n").unwrap();
    assert_eq!(
        health_url(&file).as_deref(),
        Some("http://127.0.0.1:1776/readyz")
    );
}

#[test]
fn native_and_legacy_launch_locks_exclude_each_other() {
    let home = Home::new();
    let legacy_path = home.0.join("dot.lock");
    state::atomic_create(&legacy_path, b"0").unwrap();
    let probe = || {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", FIXTURE])
            .env(CONFIG_ENV, &legacy_path)
            .env("PSEUDOLIFE_TEST_TUNNEL_VENDOR_MODE", "legacy-lock")
            .output()
            .unwrap();
        output.status.success()
    };
    let locks = launch_locks(&home.0, "dot").unwrap();
    assert!(!probe());
    drop(locks);
    assert!(probe());

    let legacy = state::open_with_write(&legacy_path, true).unwrap().file;
    assert!(legacy_lock(&legacy));
    assert!(launch_locks(&home.0, "dot").is_err());
    assert!(stop(&home.0, "dot").is_err());
    drop(legacy);
    assert!(launch_locks(&home.0, "dot").is_ok());
}

#[cfg(unix)]
fn legacy_lock(file: &File) -> bool {
    rustix::fs::flock(file, rustix::fs::FlockOperation::NonBlockingLockExclusive).is_ok()
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn legacy_lock(file: &File) -> bool {
    use std::os::windows::io::AsRawHandle;
    // Like msvcrt.locking(fd, LK_NBLCK, 1) at offset zero.
    unsafe {
        windows_sys::Win32::Storage::FileSystem::LockFile(file.as_raw_handle(), 0, 0, 1, 0) != 0
    }
}
