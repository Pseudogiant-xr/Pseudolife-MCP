//! Installed-home proofs. The fixture child is native Rust; neither launcher
//! nor selected runtime can find an interpreter on PATH.
mod native_binary_fixture;
use pseudolife_stdio::runtime::{self, BINARY, Evidence, Layout, UpdateLock};
use serde_json::json;
use std::{
    collections::BTreeSet,
    fs,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("native-installed-proof-{}", uuid::Uuid::new_v4()));
        pseudolife_stdio::board::state::private_dir(&path).unwrap();
        Self(path)
    }
    fn layout(&self) -> Layout {
        Layout {
            root: self.0.join("runtimes"),
            launcher: self.0.join("bin").join(BINARY),
            user_bin: None,
            user_path: false,
        }
    }
    fn command(&self, executable: impl AsRef<std::ffi::OsStr>) -> Command {
        let mut command = Command::new(executable);
        command.env_clear();
        for key in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        let layout = self.layout();
        command
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("LOCALAPPDATA", self.0.join("Local"))
            .env("APPDATA", self.0.join("Roaming"))
            .env("CODEX_HOME", self.0.join("codex"))
            .env("CLAUDE_CONFIG_DIR", self.0.join("claude"))
            .env("PATH", self.0.join("empty-path"))
            .env("PSEUDOLIFE_SHIM_RUNTIMES", layout.root)
            .env("PSEUDOLIFE_SHIM_LAUNCHER", layout.launcher)
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1")
            .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
            .current_dir(&self.0);
        command
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn installed_production_launcher_ignores_incomplete_and_forged_runtime() {
    let home = Home::new();
    let layout = home.layout();
    let candidate = native_binary_fixture::installable(
        std::path::Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")),
        &home.0,
    );
    let _lock = UpdateLock::acquire(&layout).unwrap();
    let binary = fs::read(&candidate).unwrap();
    let archive = release_archive(&binary);
    use sha2::{Digest, Sha256};
    let name = format!(
        "pseudolife-mcp-1.0.0-{}.{}",
        runtime::target_triple().unwrap(),
        if cfg!(windows) { "zip" } else { "tar.gz" }
    );
    let sums = format!("{:x}  {name}\n", Sha256::digest(&archive));
    runtime::install_archive(&layout, "1.0.0", "pseudolife-mcp==1.0.0", &archive, &sums).unwrap();
    for (name, marker) in [
        ("000002", None),
        (
            "999999",
            Some(json!({"format":1,"kind":"native","version":"9.0.0"})),
        ),
    ] {
        let directory = layout.root.join(name);
        fs::create_dir(&directory).unwrap();
        fs::write(directory.join(BINARY), b"not executable").unwrap();
        if let Some(marker) = marker {
            pseudolife_stdio::board::state::atomic_create(
                &directory.join("runtime.json"),
                &serde_json::to_vec(&marker).unwrap(),
            )
            .unwrap();
        }
    }
    drop(_lock);
    let result = home
        .command(&layout.launcher)
        .arg("--help")
        .output()
        .unwrap();
    let direct = home.command(&candidate).arg("--help").output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(result.stdout, direct.stdout);
    assert_eq!(result.stderr, direct.stderr);
    for missing in ["PSEUDOLIFE_SHIM_RUNTIMES", "PSEUDOLIFE_SHIM_LAUNCHER"] {
        let result = home
            .command(&layout.launcher)
            .env_remove(missing)
            .arg("--help")
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(1));
        assert!(result.stdout.is_empty());
        assert!(String::from_utf8_lossy(&result.stderr).contains("set both"));
    }
}

#[cfg(windows)]
fn release_archive(binary: &[u8]) -> Vec<u8> {
    let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
    writer
        .start_file(
            BINARY,
            zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Deflated)
                .unix_permissions(0o755),
        )
        .unwrap();
    writer.write_all(binary).unwrap();
    writer.finish().unwrap().into_inner()
}
#[cfg(unix)]
fn release_archive(binary: &[u8]) -> Vec<u8> {
    let encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
    let mut writer = tar::Builder::new(encoder);
    let mut header = tar::Header::new_gnu();
    header.set_size(binary.len() as u64);
    header.set_mode(0o755);
    header.set_cksum();
    writer.append_data(&mut header, BINARY, binary).unwrap();
    writer.into_inner().unwrap().finish().unwrap()
}

#[test]
fn installed_cli_archive_install_retains_inputs_and_reports_checksum_failure() {
    use sha2::{Digest, Sha256};
    let home = Home::new();
    let layout = home.layout();
    let candidate = native_binary_fixture::installable(
        std::path::Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")),
        &home.0,
    );
    let data = release_archive(&fs::read(&candidate).unwrap());
    let archive = home.0.join("retained-release-archive");
    let sums = home.0.join("SHA256SUMS");
    fs::write(&archive, &data).unwrap();
    let name = format!(
        "pseudolife-mcp-1.0.0-{}.{}",
        runtime::target_triple().unwrap(),
        if cfg!(windows) { "zip" } else { "tar.gz" }
    );
    fs::write(&sums, format!("{:x}  {name}\n", Sha256::digest(&data))).unwrap();
    let invoke = || {
        home.command(&candidate)
            .args(["runtime", "install", "--archive"])
            .arg(&archive)
            .arg("--sha256sums")
            .arg(&sums)
            .args(["--version", "1.0.0", "--json"])
            .output()
            .unwrap()
    };
    let output = invoke();
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["state"], "installed");
    assert_eq!(report["version"], "1.0.0");
    assert_eq!(report["launcher"], json!(layout.launcher));
    assert_eq!(fs::read(&archive).unwrap(), data);
    assert!(sums.is_file());
    fs::write(&sums, format!("{}  {name}\n", "0".repeat(64))).unwrap();
    let output = invoke();
    assert_eq!(output.status.code(), Some(1));
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["state"], "failed");
    assert!(
        report["detail"]
            .as_str()
            .unwrap()
            .contains("checksum mismatch")
    );
    assert_eq!(runtime::current_runtime(&layout).unwrap().sequence, 1);
    assert!(archive.is_file());
    assert!(sums.is_file());
}

// Executed only as an owned child in the next test. It exercises inherited
// argv/env/stdin/stdout, runtime pinning and same-process POSIX exec semantics.
#[test]
fn native_runtime_fixture_child() {
    if std::env::var_os("PSEUDOLIFE_NATIVE_FIXTURE_CHILD").is_none() {
        return;
    }
    if std::env::var_os("PSEUDOLIFE_NATIVE_FIXTURE_BEFORE_PIN").is_some()
        && std::env::current_exe()
            .unwrap()
            .parent()
            .unwrap()
            .file_name()
            .unwrap()
            == "000001"
    {
        println!("fixture-before-pin");
        std::io::stdout().flush().unwrap();
        std::io::stdin().read_line(&mut String::new()).unwrap();
    }
    if let Some(code) = runtime::maybe_launch() {
        std::process::exit(i32::from(code));
    }
    let path = std::env::current_exe().unwrap();
    let name = path
        .parent()
        .unwrap()
        .file_name()
        .unwrap()
        .to_string_lossy();
    println!("fixture-ready:{name}");
    std::io::stdout().flush().unwrap();
    for line in std::io::stdin().lock().lines() {
        let line = line.unwrap();
        if line == "quit" {
            std::process::exit(37);
        }
        println!("fixture-echo:{line}");
        std::io::stdout().flush().unwrap();
    }
}
struct Session {
    child: Child,
    output: mpsc::Receiver<String>,
}
impl Session {
    fn start(home: &Home) -> Self {
        Self::start_with_pause(home, false)
    }
    fn start_with_pause(home: &Home, pause: bool) -> Self {
        let mut command = home.command(&home.layout().launcher);
        if pause {
            command.env("PSEUDOLIFE_NATIVE_FIXTURE_BEFORE_PIN", "1");
        }
        let mut child = command
            .args(["--exact", "native_runtime_fixture_child", "--nocapture"])
            .env("PSEUDOLIFE_NATIVE_FIXTURE_CHILD", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let pipe = child.stdout.take().unwrap();
        let (send, output) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(pipe).lines() {
                let Ok(line) = line else {
                    break;
                };
                if send.send(line).is_err() {
                    break;
                }
            }
        });
        Self { child, output }
    }
    fn until(&mut self, expected: &str) {
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let line = self
                .output
                .recv_timeout(remaining)
                .unwrap_or_else(|_| panic!("child did not report {expected} before the deadline"));
            if line.trim() == expected {
                break;
            }
        }
    }
    fn send(&mut self, line: &str) {
        let input = self.child.stdin.as_mut().unwrap();
        writeln!(input, "{line}").unwrap();
        input.flush().unwrap();
    }
}

#[cfg(unix)]
#[test]
fn launcher_holds_runtime_lock_across_exec_before_child_initializes() {
    let home = Home::new();
    let layout = home.layout();
    let guard = UpdateLock::acquire(&layout).unwrap();
    let old = runtime::install_binary(
        &layout,
        "1.0.0",
        "fixture",
        None,
        &std::env::current_exe().unwrap(),
    )
    .unwrap();
    drop(guard);
    let mut session = Session::start_with_pause(&home, true);
    session.until("fixture-before-pin");
    let lock =
        pseudolife_stdio::board::state::open_with_write(&old.usage_lock_path(), true).unwrap();
    assert!(
        lock.file.try_lock().is_err(),
        "exec released the runtime usage lock before the child could pin it"
    );
    session.send("continue");
    session.until("fixture-ready:000001");
    session.send("quit");
    assert_eq!(session.child.wait().unwrap().code(), Some(37));
}
impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(windows)]
#[test]
fn installed_launcher_activation_recovers_after_windows_file_lock() {
    use std::os::windows::fs::OpenOptionsExt;
    let home = Home::new();
    let layout = home.layout();
    let candidate = PathBuf::from(env!("CARGO_BIN_EXE_pseudolife-stdio"));
    let _guard = UpdateLock::acquire(&layout).unwrap();
    let old = runtime::install_binary(&layout, "1.0.0", "fixture", None, &candidate).unwrap();
    let locked = fs::OpenOptions::new()
        .read(true)
        .share_mode(0)
        .open(&layout.launcher)
        .unwrap();
    assert!(runtime::install_binary(&layout, "1.0.1", "fixture", None, &candidate).is_err());
    drop(locked);
    assert_eq!(
        runtime::current_runtime(&layout).unwrap().sequence,
        old.sequence
    );
    assert_eq!(
        runtime::install_binary(&layout, "1.0.1", "fixture", None, &candidate)
            .unwrap()
            .version,
        "1.0.1"
    );
}

#[cfg(windows)]
#[test]
fn terminating_installed_windows_launcher_closes_its_owned_runtime_job() {
    let home = Home::new();
    let layout = home.layout();
    let binary = std::env::current_exe().unwrap();
    let guard = UpdateLock::acquire(&layout).unwrap();
    let runtime = runtime::install_binary(&layout, "1.0.0", "fixture", None, &binary).unwrap();
    drop(guard);
    let mut session = Session::start(&home);
    session.until("fixture-ready:000001");
    let pin =
        pseudolife_stdio::board::state::open_with_write(&runtime.usage_lock_path(), true).unwrap();
    assert!(pin.file.try_lock().is_err());
    session.child.kill().unwrap();
    session.child.wait().unwrap();
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        if pin.file.try_lock().is_ok() {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "selected runtime outlived its terminated launcher job"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn installed_launcher_keeps_old_session_while_new_session_selects_new_runtime() {
    let home = Home::new();
    let layout = home.layout();
    let binary = std::env::current_exe().unwrap();
    let _lock = UpdateLock::acquire(&layout).unwrap();
    let old = runtime::install_binary(&layout, "1.0.0", "fixture", None, &binary).unwrap();
    drop(_lock);
    let mut first = Session::start(&home);
    first.until("fixture-ready:000001");
    let _lock = UpdateLock::acquire(&layout).unwrap();
    let new = runtime::install_binary(&layout, "1.0.1", "fixture", None, &binary).unwrap();
    drop(_lock);
    first.send("日本語");
    first.until("fixture-echo:日本語");
    let mut second = Session::start(&home);
    second.until("fixture-ready:000002");
    let evidence = Evidence {
        processes: vec![(first.child.id(), 0, old.executable())],
        registrations: BTreeSet::new(),
        tunnels: BTreeSet::new(),
    };
    let _lock = UpdateLock::acquire(&layout).unwrap();
    assert!(
        runtime::remove_unused(&layout, Ok(evidence))["removed"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    drop(_lock);
    assert!(old.path.is_dir());
    assert!(new.path.is_dir());
    first.send("quit");
    assert_eq!(first.child.wait().unwrap().code(), Some(37));
    second.send("quit");
    assert_eq!(second.child.wait().unwrap().code(), Some(37));
}
