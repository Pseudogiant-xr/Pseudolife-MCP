use super::*;
use std::{net::TcpListener, thread, time::Instant};

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
        _locks: locks,
    }
}

const FIXTURE: &str = "cli::tunnel::runtime::tests::vendor_fixture";
const CONFIG_ENV: &str = "PSEUDOLIFE_TEST_TUNNEL_VENDOR_CONFIG";

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
    if mode == "ready" {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let health = config["health"]["url_file"].as_str().unwrap();
        fs::write(health, format!("http://{}", listener.local_addr().unwrap())).unwrap();
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut request = [0; 512];
        let count = stream.read(&mut request).unwrap();
        assert!(request[..count].starts_with(b"GET /readyz HTTP/1.1"));
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            .unwrap();
    }
    // Bounds a fixture even if a mutation causes the supervisor to lose it.
    thread::sleep(Duration::from_secs(20));
}

#[test]
fn foreground_launch_owns_one_child_and_stop_waits_for_cleanup() {
    let home = Home::new();
    let exe = std::env::current_exe().unwrap();
    let record_path = record::path(&home.0, "dot");
    let (run, stopped, busy) = thread::scope(|scope| {
        let run = scope.spawn(|| {
            launch(prepared(&home.0), &exe, Duration::from_secs(8), |config| {
                fixture_command(&exe, config, "ready")
            })
        });
        let deadline = Instant::now() + Duration::from_secs(10);
        let ready = loop {
            if let Ok(record) = read_record(&record_path)
                && record.ready
            {
                break Some(record);
            }
            if Instant::now() >= deadline || run.is_finished() {
                break None;
            }
            thread::sleep(Duration::from_millis(20));
        };
        let Some(ready) = ready else {
            panic!("fixture did not become ready: {:?}", run.join().unwrap());
        };
        let lock = open_lock(&home.0, "dot", &lock_path(&home.0, "dot")).unwrap();
        let busy = matches!(lock.try_lock(), Err(fs::TryLockError::WouldBlock));
        let stage = stage_path(&home.0, launch_id(&ready.native_launch).unwrap());
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
    for (mode, timeout) in [
        ("early-exit", Duration::from_secs(6)),
        ("never-ready", Duration::from_millis(350)),
    ] {
        let home = Home::new();
        let started = Instant::now();
        let result = launch(prepared(&home.0), &exe, timeout, |config| {
            fixture_command(&exe, config, mode)
        });
        assert_eq!(result, Err(NOT_READY));
        assert!(started.elapsed() < Duration::from_secs(10));
        assert_eq!(fs::read_dir(&home.0).unwrap().count(), 2);
        assert!(launch_locks(&home.0, "dot").is_ok());
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
        matches!(stop(&home.0, "dot"), Err(Fail::Detail(message)) if message.contains("cannot be verified"))
    );
    assert!(!stage_path(&home.0, id).exists());
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
