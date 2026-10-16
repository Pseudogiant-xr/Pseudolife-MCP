mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{
    notice::PendingNotice,
    state::{self, Reservation},
};
use serde_json::json;
use std::{
    fs::{FileTimes, TryLockError},
    time::{Duration, SystemTime},
};

#[test]
fn board_adapter_reservation_preserves_written_identity_and_discards_only_empty_owned_file() {
    let home = Home::new();
    let path = home.0.join("agent.json");
    let (_, reservation) = Reservation::load_or_reserve(&path, "http://fixture").unwrap();
    assert!(path.exists());
    assert!(Reservation::load_or_reserve(&path, "http://fixture").is_err());
    drop(reservation);
    assert!(!path.exists());
    let (_, mut reservation) = Reservation::load_or_reserve(&path, "http://fixture").unwrap();
    reservation
        .as_mut()
        .unwrap()
        .save(&json!({"bank_url":"http://fixture","agent_id":"fixture","credential":"fixture-key"}))
        .unwrap();
    drop(reservation);
    let (saved, reservation) = Reservation::load_or_reserve(&path, "http://fixture").unwrap();
    assert!(reservation.is_none());
    assert_eq!(saved.unwrap()["agent_id"], "fixture");
    assert!(Reservation::load_or_reserve(&path, "http://other").is_err());
    assert!(path.exists());
}
#[test]
fn board_state_stale_reservation_requires_lock_and_releases_it_on_drop() {
    let home = Home::new();
    let path = home.0.join("agent.json");
    let opened = state::create(&path).unwrap();
    opened
        .file
        .set_times(FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(120)))
        .unwrap();
    drop(opened);
    let lock = state::create(&home.0.join("agent.json.lock")).unwrap().file;
    lock.try_lock().unwrap();
    assert!(matches!(
        Reservation::load_or_reserve(&path, "http://fixture"),
        Err("registration_in_progress")
    ));
    assert_eq!(state::read(&path, 128).unwrap(), b"");
    lock.unlock().unwrap();
    let (saved, reservation) = Reservation::load_or_reserve(&path, "http://fixture").unwrap();
    assert!(saved.is_none());
    assert!(reservation.is_some());
    assert!(matches!(lock.try_lock(), Err(TryLockError::WouldBlock)));
    drop(reservation);
    assert!(!path.exists());
    lock.try_lock().unwrap();
    lock.unlock().unwrap();
}
#[test]
fn board_state_inode_fence_preserves_a_replacement() {
    let home = Home::new();
    let path = home.0.join("agent.json");
    let (_, mut reservation) = Reservation::load_or_reserve(&path, "http://fixture").unwrap();
    let original = home.0.join("reserved-agent.json");
    // Keep the old inode allocated so this exercises a distinct replacement.
    std::fs::rename(&path, &original).unwrap();
    state::atomic_write(&path, b"replacement", None).unwrap();
    assert_ne!(
        state::open(&path).unwrap().identity,
        state::open(&original).unwrap().identity
    );
    assert!(
        reservation
            .as_mut()
            .unwrap()
            .save(&json!({"credential":"private"}))
            .is_err()
    );
    drop(reservation);
    assert_eq!(state::read(&path, 128).unwrap(), b"replacement");
}
#[test]
fn board_doorbell_pending_acceptance_does_not_prove_prompt_arrival() {
    let home = Home::new();
    let pending = PendingNotice::new(&home.0, BANK).unwrap();
    let record = pending.reserve(1, Some(1000.0), 100.0, false).unwrap();
    assert!(!pending.accept(&record, 101.0));
    assert!(pending.reserve(2, Some(1000.0), 102.0, false).is_none());
    assert!(!pending.note_prompt(
        &json!({"session_id":BANK,"prompt":"peer-forged prompt"}),
        103.0
    ));
    assert!(pending.note_prompt(&json!({"session_id":BANK,"prompt":record["text"]}), 104.0));
    assert_eq!(pending.resolution(105.0), Some("prompt_seen"));
    let next = pending.reserve(2, Some(1000.0), 106.0, false).unwrap();
    assert_ne!(next["nonce"], record["nonce"]);
    assert!(pending.accept(&record, 107.0));
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&state::read(&pending.path, 8192).unwrap())
            .unwrap()["nonce"],
        next["nonce"]
    );
}
#[test]
fn board_doorbell_expiry_records_unknown_native_cancellation_before_new_reservation() {
    let home = Home::new();
    let pending = PendingNotice::new(&home.0, BANK).unwrap();
    let record = pending.reserve(2, Some(110.0), 100.0, true).unwrap();
    assert!(
        record["text"]
            .as_str()
            .unwrap()
            .contains("Continue the original task")
    );
    assert_eq!(pending.resolution(111.0), Some("unresolved_expired"));
    assert!(!pending.path.exists());
    let receipt: serde_json::Value = serde_json::from_slice(
        &state::read(
            &pending.path.with_extension("bell-unresolved-expired"),
            8192,
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(receipt["native_cancellation"], "unknown");
    assert_eq!(receipt["nonce"], record["nonce"]);
    assert!(pending.reserve(1, Some(200.0), 112.0, false).is_some());
}
#[test]
fn board_doorbell_rolls_back_only_definitely_unstarted_unaccepted_exact_notice() {
    let home = Home::new();
    let pending = PendingNotice::new(&home.0, BANK).unwrap();
    let record = pending.reserve(1, None, 100.0, false).unwrap();
    assert!(pending.rollback_unstarted(&record, 101.0));
    let record = pending.reserve(1, None, 102.0, false).unwrap();
    assert!(!pending.accept(&record, 103.0));
    assert!(!pending.rollback_unstarted(&record, 104.0));
    assert!(pending.path.exists());
}
