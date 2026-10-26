//! The lite tier's file locks: the first-boot lock
//! (`pg::embedded::StartLock`) and the runtime install lock
//! (`pg::native_provision`), in their own test binary so the hosted stress
//! loop can select them (file locks behave differently on `windows-latest`
//! than on a dev machine).
#![forbid(unsafe_code)]
use pseudolife_stdio::pg::embedded::{START_LOCK_TIMEOUT, StartLock};
use pseudolife_stdio::pg::native_provision::{self as native, Fetch, Manifest, Pinned};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{
        Barrier, Condvar, Mutex,
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    time::Duration,
};

fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "pl-embedded-{label}-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn the_start_lock_excludes_a_second_holder_until_released() {
    let dir = scratch("lock");
    let pgdata = dir.join("embedded_pg");
    let held = StartLock::acquire(&pgdata, START_LOCK_TIMEOUT).unwrap();
    let (sent, acquired) = mpsc::channel();
    let contender = {
        let pgdata = pgdata.clone();
        std::thread::spawn(move || {
            let lock = StartLock::acquire(&pgdata, START_LOCK_TIMEOUT);
            sent.send(lock.is_ok()).unwrap();
            lock
        })
    };
    // Negative check: the contender must not get the lock while it is held.
    assert!(acquired.recv_timeout(Duration::from_secs(1)).is_err());
    drop(held);
    assert!(acquired.recv_timeout(Duration::from_secs(60)).unwrap());
    drop(contender.join().unwrap());
    fs::remove_dir_all(dir).unwrap();
}

#[cfg(windows)]
#[test]
fn the_windows_start_lock_gives_up_at_its_deadline() {
    let dir = scratch("locktimeout");
    let pgdata = dir.join("embedded_pg");
    let held = StartLock::acquire(&pgdata, START_LOCK_TIMEOUT).unwrap();
    let error = StartLock::acquire(&pgdata, Duration::ZERO).err().unwrap();
    assert!(matches!(
        error,
        pseudolife_stdio::pg::embedded::Error::LockTimeout(_)
    ));
    assert!(
        error
            .to_string()
            .contains("another process has been starting this bank for over 120s")
    );
    drop(held);
    fs::remove_dir_all(dir).unwrap();
}

struct Payload {
    sha256: String,
}
impl Pinned for Payload {
    fn url(&self) -> &str {
        "https://example.com/payload.bin"
    }
    fn sha256(&self) -> &str {
        &self.sha256
    }
}

const WAITING: &str = "waiting for another process";

/// Holds the lock holder's download until the other provisioner has said
/// it is waiting for the lock, so the two contend whatever the scheduling;
/// without the lock nobody waits and the download fails after the bound.
struct GatedFetch<'a> {
    calls: AtomicUsize,
    logs: &'a Mutex<Vec<String>>,
    logged: &'a Condvar,
}
impl Fetch for GatedFetch<'_> {
    fn fetch(&self, _url: &str, sink: &mut dyn Write) -> Result<(), String> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let logs = self.logs.lock().unwrap();
        let (_logs, wait) = self
            .logged
            .wait_timeout_while(logs, Duration::from_secs(30), |logs| {
                !logs.iter().any(|line| line.starts_with(WAITING))
            })
            .unwrap();
        if wait.timed_out() {
            return Err("the other provisioner never waited for the install lock".into());
        }
        sink.write_all(b"payload").map_err(|e| e.to_string())
    }
}

#[test]
fn two_provisioners_on_one_root_install_once() {
    use sha2::{Digest, Sha256};
    let root = scratch("install-lock");
    let pins = [Payload {
        sha256: native::hex(&Sha256::digest(b"payload")),
    }];
    let manifest = Manifest {
        label: "test runtime",
        version: "1.0",
        artifacts: &pins,
        record: serde_json::Map::new(),
    };
    let logs = Mutex::new(Vec::<String>::new());
    let logged = Condvar::new();
    let fetch = GatedFetch {
        calls: AtomicUsize::new(0),
        logs: &logs,
        logged: &logged,
    };
    let unpacks = Mutex::new(0);
    let start = Barrier::new(2);
    let outcomes: Vec<bool> = std::thread::scope(|scope| {
        let run = || {
            start.wait();
            let mut unpack = |_: &Payload, download: &Path, tree: &Path| {
                *unpacks.lock().unwrap() += 1;
                fs::copy(download, tree.join("payload"))
                    .map(drop)
                    .map_err(|e| e.to_string())
            };
            let validate = |dir: &Path| {
                dir.join("payload")
                    .is_file()
                    .then_some(())
                    .ok_or_else(|| "payload is missing".to_owned())
            };
            let mut log = |line: &str| {
                logs.lock().unwrap().push(line.to_owned());
                logged.notify_all();
            };
            native::provision(&root, &manifest, &fetch, &mut log, &mut unpack, &validate)
                .unwrap()
                .1
        };
        let first = scope.spawn(run);
        let second = scope.spawn(run);
        vec![first.join().unwrap(), second.join().unwrap()]
    });
    // The lock serializes them: one installs, the other then finds it.
    assert_eq!(outcomes.iter().filter(|installed| **installed).count(), 1);
    assert_eq!(fetch.calls.load(Ordering::SeqCst), 1);
    assert_eq!(*unpacks.lock().unwrap(), 1);
    let logs = logs.into_inner().unwrap();
    let waited = logs.iter().filter(|line| line.starts_with(WAITING)).count();
    assert_eq!(waited, 1, "{logs:?}");
    let dir = manifest.install_dir(&root);
    assert_eq!(fs::read(dir.join("payload")).unwrap(), b"payload");
    fs::remove_dir_all(root).unwrap();
}
