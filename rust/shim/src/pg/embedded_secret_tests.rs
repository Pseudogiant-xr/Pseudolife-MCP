use super::*;
use std::path::PathBuf;

fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "pl-secret-{label}-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn a_created_secret_is_64_hex_owner_only_and_reads_back() {
    let dir = scratch("create");
    let path = dir.join("embedded_pg.secret");
    assert_eq!(CredentialSecrets.read(&path).unwrap(), None);
    let secret = CredentialSecrets.create(&path).unwrap();
    assert_eq!(secret.len(), 64);
    assert!(
        secret
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    );
    // Read back through the owner-only validator bearer-token files use.
    assert_eq!(
        CredentialSecrets.read(&path).unwrap().as_deref(),
        Some(secret.as_str())
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    // Two secrets differ.
    let other = CredentialSecrets.create(&dir.join("other.secret")).unwrap();
    assert_ne!(other, secret);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn an_existing_file_is_never_replaced() {
    let dir = scratch("exists");
    let path = dir.join("embedded_pg.secret");
    let first = CredentialSecrets.create(&path).unwrap();
    assert!(CredentialSecrets.create(&path).is_err());
    assert_eq!(
        CredentialSecrets.read(&path).unwrap().as_deref(),
        Some(first.as_str())
    );
    fs::remove_dir_all(dir).unwrap();
}

#[cfg(unix)]
#[test]
fn a_secret_other_accounts_can_read_is_refused() {
    use std::os::unix::fs::PermissionsExt;
    let dir = scratch("open");
    let path = dir.join("embedded_pg.secret");
    CredentialSecrets.create(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(CredentialSecrets.read(&path).is_err());
    fs::remove_dir_all(dir).unwrap();
}

/// Python lite's own lifecycle (`pseudolife_memory/storage/embedded_pg.py`)
/// against the same data dir, with this owner-only store on the Rust side.
/// Set `PSEUDOLIFE_TEST_PG0_PYTHON` to an interpreter with `pg0-embedded`
/// installed, beside `PSEUDOLIFE_TEST_PG_RUNTIME` naming the pg0
/// installation it extracted.
const PYTHON_SIDE: &str = r#"
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from pseudolife_memory.storage import embedded_pg
import pg0
data_dir, action = Path(sys.argv[1]), sys.argv[2]
pgdata = data_dir / "embedded_pg"
if action == "start":
    try:
        dsn, started = embedded_pg.attach_or_start(data_dir)
        print(json.dumps({"dsn": dsn, "owned": started is not None}))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
elif action == "stop":
    pg0.Pg0(name=embedded_pg._instance_name(pgdata), data_dir=str(pgdata)).stop()
    print(json.dumps({"stopped": True}))
"#;

fn python_side(python: &std::ffi::OsString, data_dir: &Path, action: &str) -> serde_json::Value {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
    let output = std::process::Command::new(python)
        .arg("-c")
        .arg(PYTHON_SIDE)
        .arg(data_dir)
        .arg(action)
        .arg(repo)
        .stdin(std::process::Stdio::null())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.lines().last().unwrap_or("")).unwrap()
}

fn port_of(dsn: &str) -> u16 {
    dsn.rsplit_once(':')
        .unwrap()
        .1
        .split('/')
        .next()
        .unwrap()
        .parse()
        .unwrap()
}

#[test]
fn a_bank_opens_on_both_sides() {
    use crate::pg::embedded::{
        attach_or_start,
        tests::{psql_in, real_runtime},
    };
    let Some(runtime) = real_runtime() else {
        return;
    };
    let Some(python) = std::env::var_os("PSEUDOLIFE_TEST_PG0_PYTHON").filter(|v| !v.is_empty())
    else {
        eprintln!("PSEUDOLIFE_TEST_PG0_PYTHON unset: cross-side case not run");
        return;
    };
    let dir = scratch("cross-side");

    // 1. Python lite creates the bank (its secret, SCRAM) and leaves its
    // server running; the Rust side attaches with the same secret.
    let python_start = python_side(&python, &dir, "start");
    assert_eq!(python_start["owned"], true, "{python_start}");
    let attached = attach_or_start(&dir, &runtime, &CredentialSecrets).unwrap();
    assert!(!attached.owned());
    assert_eq!(
        attached.port,
        port_of(python_start["dsn"].as_str().unwrap())
    );
    let secret = CredentialSecrets
        .read(&dir.join("embedded_pg.secret"))
        .unwrap()
        .unwrap();
    assert_eq!(attached.password(), secret);
    psql_in(
        &runtime,
        &attached,
        "CREATE TABLE written_by_rust (x int); INSERT INTO written_by_rust VALUES (1)",
    );
    attached.stop().unwrap();
    // 2. Python stops its server; the Rust side starts Python's pgdata.
    python_side(&python, &dir, "stop");
    let started = attach_or_start(&dir, &runtime, &CredentialSecrets).unwrap();
    assert!(started.owned());
    assert_eq!(
        psql_in(&runtime, &started, "SELECT x FROM written_by_rust"),
        "1"
    );
    // 3. While the Rust side owns the server, Python lite attaches to it
    // through its postmaster.pid, with the same secret.
    let python_attached = python_side(&python, &dir, "start");
    assert_eq!(python_attached["owned"], false, "{python_attached}");
    assert_eq!(
        port_of(python_attached["dsn"].as_str().unwrap()),
        started.port
    );
    started.stop().unwrap();
    // 4. After the Rust side stops, Python lite starts the bank again.
    let python_again = python_side(&python, &dir, "start");
    assert_eq!(python_again["owned"], true, "{python_again}");
    python_side(&python, &dir, "stop");

    // 5. A bank the Rust side created opens under Python lite.
    let fresh = scratch("cross-side-rust");
    let rust_bank = attach_or_start(&fresh, &runtime, &CredentialSecrets).unwrap();
    psql_in(&runtime, &rust_bank, "CREATE TABLE made_by_rust (y int)");
    rust_bank.stop().unwrap();
    let python_opens = python_side(&python, &fresh, "start");
    assert_eq!(python_opens["owned"], true, "{python_opens}");
    let reopened = attach_or_start(&fresh, &runtime, &CredentialSecrets).unwrap();
    assert!(!reopened.owned());
    assert_eq!(
        psql_in(&runtime, &reopened, "SELECT count(*) FROM made_by_rust"),
        "0"
    );
    python_side(&python, &fresh, "stop");

    fs::remove_dir_all(dir).unwrap();
    fs::remove_dir_all(fresh).unwrap();
}

#[test]
fn a_leftover_temporary_file_does_not_block_creation() {
    // A crash between writing the temporary file and renaming it leaves
    // the temporary behind, never a partial secret at the real path.
    let dir = scratch("leftover");
    fs::write(dir.join(".embedded_pg.secret.deadbeef"), "partial").unwrap();
    let path = dir.join("embedded_pg.secret");
    let secret = CredentialSecrets.create(&path).unwrap();
    assert_eq!(
        CredentialSecrets.read(&path).unwrap().as_deref(),
        Some(secret.as_str())
    );
    // The create left no temporary file of its own behind.
    let names: Vec<String> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(names.len(), 2, "{names:?}");
    fs::remove_dir_all(dir).unwrap();
}

/// Python lite holds its own first-boot lock (`_start_lock`: msvcrt byte 0
/// on Windows, fcntl.flock elsewhere) in another process; the Rust lock
/// must wait for it, then get it once Python lets go.
const PYTHON_LOCK: &str = r#"
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from pseudolife_memory.storage import embedded_pg
with embedded_pg._start_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read()
"#;

#[test]
fn the_rust_lock_and_python_lite_s_lock_exclude_each_other() {
    use crate::pg::embedded::{START_LOCK_TIMEOUT, StartLock};
    use std::io::{BufRead, BufReader};
    let Some(python) = std::env::var_os("PSEUDOLIFE_TEST_PG0_PYTHON").filter(|v| !v.is_empty())
    else {
        eprintln!("PSEUDOLIFE_TEST_PG0_PYTHON unset: cross-process lock case not run");
        return;
    };
    let dir = scratch("pylock");
    let pgdata = dir.join("embedded_pg");
    fs::create_dir_all(&pgdata).unwrap();
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
    let mut child = std::process::Command::new(python)
        .arg("-c")
        .arg(PYTHON_LOCK)
        .arg(&pgdata)
        .arg(repo)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let mut line = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert_eq!(line.trim(), "locked");
    let (sent, acquired) = std::sync::mpsc::channel();
    let contender = {
        let pgdata = pgdata.clone();
        std::thread::spawn(move || {
            let lock = StartLock::acquire(&pgdata, START_LOCK_TIMEOUT);
            sent.send(lock.is_ok()).unwrap();
            lock
        })
    };
    // Negative check: no Rust lock while Python holds its own.
    assert!(
        acquired
            .recv_timeout(std::time::Duration::from_secs(1))
            .is_err()
    );
    drop(child.stdin.take());
    child.wait().unwrap();
    assert!(
        acquired
            .recv_timeout(std::time::Duration::from_secs(60))
            .unwrap()
    );
    drop(contender.join().unwrap());
    fs::remove_dir_all(dir).unwrap();
}
