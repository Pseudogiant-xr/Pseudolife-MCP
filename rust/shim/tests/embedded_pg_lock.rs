//! The lite tier's first-boot lock (`pg::embedded::StartLock`), in its own
//! test binary so the hosted stress loop can select it (it rests on file
//! locks, which `windows-latest` runs differently from a dev machine).
#![forbid(unsafe_code)]
use pseudolife_stdio::pg::embedded::{START_LOCK_TIMEOUT, StartLock};
use std::{fs, path::PathBuf, sync::mpsc, time::Duration};

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
