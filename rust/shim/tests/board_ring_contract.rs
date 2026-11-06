mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{adapter::Adapter, liveness, state};
use serde_json::json;
use std::time::Duration;
fn write(home: &Home, name: &str, text: &str) {
    state::atomic_write(&home.0.join(name), text.as_bytes(), None).unwrap();
}
/// The ring marker is written off the runtime, on the digest's queue behind
/// any digest sync, so it can land after its due time on a slow disk.
async fn ring_written(home: &Home) -> String {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if let Ok(bytes) = state::read(&home.0.join("digest.ring"), 256) {
                return String::from_utf8(bytes).unwrap();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("ring marker not written")
}
/// Let the ring worker finish what it has queued, so a check for the absence
/// of a marker, or for a recorded failed write, is not just early.
async fn ring_settled(home: &Home) {
    tokio::time::sleep(Duration::from_millis(20)).await;
    state::drain(&home.0.join("digest.txt")).await;
    tokio::time::sleep(Duration::from_millis(10)).await;
}
#[test]
fn board_ring_reason_uses_python_unicode14_predicates() {
    assert!(liveness::whitespace('\u{1c}'));
    assert!(liveness::whitespace('\u{2003}'));
    assert!(liveness::alphanumeric('é'));
    assert!(!liveness::alphanumeric('\u{0345}'));
    assert!(!liveness::alphanumeric('\u{093e}'));
    assert_eq!(liveness::reason("alpha\u{1c}beta <>!"), "alpha beta ");
    assert_eq!(liveness::reason("<>!"), "unknown");
    assert_eq!(liveness::reason(&"a".repeat(80)).len(), 60);
}
#[test]
fn board_ring_unicode14_full_range_contract() {
    use sha2::{Digest, Sha256};
    let mut digest = Sha256::new();
    for point in 0..0x110000 {
        let predicates = char::from_u32(point).map_or([0, 0], |value| {
            [
                u8::from(liveness::alphanumeric(value)),
                u8::from(liveness::whitespace(value)),
            ]
        });
        digest.update(predicates);
    }
    // evals/rust_port/unicode14_reason.py computes this at the oracle pin.
    assert_eq!(
        format!("{:x}", digest.finalize()),
        "f4a77aec4b67a7770e67e22fe029b97d93300d0eafcf13b0968a885d2b9ff2fe"
    );
}
#[tokio::test]
async fn board_digest_sweeps_only_stale_regular_foreign_markers_and_cleans_own_subagents() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    for name in [
        "old.txt",
        "old.seen",
        "old.ring",
        "old.agent",
        "old.turn",
        "old.reprint",
        "old.wait-armed",
        "old.wake-armed",
        "old.sub-child",
        "untouched.json",
        "digest.seen",
    ] {
        write(&home, name, "7\n");
        let opened = state::open_with_write(&home.0.join(name), true).unwrap();
        opened
            .file
            .set_times(
                std::fs::FileTimes::new()
                    .set_modified(std::time::SystemTime::now() - Duration::from_secs(86401)),
            )
            .unwrap();
    }
    write(&home, "fresh.txt", "fresh\n");
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    for name in [
        "old.txt",
        "old.seen",
        "old.ring",
        "old.agent",
        "old.turn",
        "old.reprint",
        "old.wait-armed",
        "old.wake-armed",
        "old.sub-child",
    ] {
        assert!(!home.0.join(name).exists());
    }
    assert!(home.0.join("untouched.json").exists());
    assert!(home.0.join("fresh.txt").exists());
    assert_eq!(adapter.view().await.watermark, 8);
    write(&home, "digest.sub-child", "live\n");
    assert!(adapter.deliver_hint().await.is_none());
    assert!(home.0.join("digest.sub-child").exists());
    adapter.close().await;
    assert!(!home.0.join("digest.sub-child").exists());
}
#[test]
fn board_ring_liveness_requires_bounded_finite_matching_listener_records() {
    let home = Home::new();
    let digest = home.0.join("task.txt");
    let now = 1000.0;
    for (name, text, expected) in [
        ("task.wait-armed", "old\n1020\n", 1020.0),
        ("task.new.wait-armed", "new\n1040\n", 1040.0),
        ("task.bad.wait-armed", "other\n1050\n", 1040.0),
        ("task.future.wait-armed", "future\n1061\n", 1040.0),
        ("task.expired.wait-armed", "expired\n999\n", 1040.0),
        ("task.nan.wait-armed", "nan\nNaN\n", 1040.0),
        ("task.extra.wait-armed", "extra\n1050\nthird\n", 1040.0),
    ] {
        write(&home, name, text);
        assert_eq!(liveness::armed_until_at(Some(&digest), now), expected);
    }
    write(&home, "task.waker.wake-armed", "waker\n1050\n");
    assert_eq!(liveness::armed_until_at(Some(&digest), now), 1040.0);
    write(&home, "task.wake", "other\n");
    assert_eq!(liveness::armed_until_at(Some(&digest), now), 1040.0);
    write(&home, "task.wake", "waker\n");
    assert_eq!(liveness::armed_until_at(Some(&digest), now), 1050.0);
    write(
        &home,
        "task.big.wait-armed",
        &format!("big\n{}\n", "1".repeat(256)),
    );
    assert_eq!(liveness::armed_until_at(Some(&digest), now), 1050.0);
    std::fs::hard_link(
        home.0.join("task.new.wait-armed"),
        home.0.join("linked.wait-armed"),
    )
    .unwrap();
    assert_eq!(liveness::armed_until_at(Some(&digest), now), 1050.0);
    assert_eq!(liveness::armed_until_at(None, now), 0.0);
}
#[tokio::test]
async fn board_ring_attach_and_heartbeat_remember_ordered_old_daemon_fallbacks() {
    let fixture = Fixture::new(0);
    fixture.answer(
        "attach",
        Answer::json(400, json!({"error":"unexpected_parameter"})),
    );
    fixture.answer(
        "attach",
        Answer::json(400, json!({"error":"unexpected_parameter"})),
    );
    let home = Home::new();
    let mut config = adapter_config(&fixture, &home);
    config.ring = true;
    let adapter = Adapter::enter(config).await.unwrap();
    adapter.note_turn().await;
    fixture.answer(
        "heartbeat",
        Answer::json(400, json!({"error":"unexpected_parameter"})),
    );
    adapter.heartbeat().await.unwrap();
    adapter.reattach().await.unwrap();
    adapter.heartbeat().await.unwrap();
    {
        let requests = fixture.requests.lock().unwrap();
        let attach = requests
            .iter()
            .filter(|request| request.path.ends_with("/attach"))
            .collect::<Vec<_>>();
        assert!(attach[0].body.get("ring_armed_until").is_some());
        assert_eq!(attach[0].body["ring"], true);
        assert!(attach[1].body.get("ring_armed_until").is_none());
        assert_eq!(attach[1].body["ring"], true);
        for request in &attach[2..] {
            assert!(request.body.get("ring").is_none());
            assert!(request.body.get("ring_armed_until").is_none());
        }
        let beats = requests
            .iter()
            .filter(|request| request.path.ends_with("/heartbeat"))
            .collect::<Vec<_>>();
        assert_eq!(beats[0].body["active"], true);
        assert!(beats[1].body.get("active").is_none());
        assert!(beats[2].body.get("active").is_none());
    }
    adapter.close().await;
}
#[tokio::test]
async fn board_ring_marker_staggers_sanitizes_deduplicates_and_withdraws() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    write(&home, "digest.txt", "5\nstale\n");
    write(&home, "digest.seen", "9\n");
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    assert_eq!(adapter.view().await.watermark, 10);
    // Far enough out that a slow digest write inside update_mailbox cannot
    // reach it, so the marker's absence right after the update means "not yet
    // due", not "not yet written".
    let at = liveness::now() + 1.0;
    let wake = json!({"generation":1,"pending_count":2,"pending_preview":[],"wake":{"decision":"rung","reason":"  peer\n sent<> mail! ","ring_at":at}});
    adapter.update_mailbox(&wake).await;
    assert!(!home.0.join("digest.ring").exists());
    assert_eq!(ring_written(&home).await, "11\nrung peer sent mail\n");
    std::fs::remove_file(home.0.join("digest.ring")).unwrap();
    adapter.update_mailbox(&wake).await;
    ring_settled(&home).await;
    assert!(!home.0.join("digest.ring").exists());
    // The ring worker waits on tokio's clock: pause it, withdraw the wake,
    // then advance past the due time and let the worker run, so a withdrawn
    // ring that still fired is queued before ring_settled drains the queue.
    tokio::time::pause();
    adapter.update_mailbox(&json!({"pending_count":3,"wake":{"decision":"rung","reason":"new","ring_at":liveness::now()+0.1}})).await;
    adapter.update_mailbox(&json!({"pending_count":0})).await;
    tokio::time::advance(Duration::from_secs(1)).await;
    tokio::task::yield_now().await;
    tokio::time::resume();
    ring_settled(&home).await;
    assert!(!home.0.join("digest.ring").exists());
    assert!(adapter.view().await.wake.is_none());
    adapter.close().await;
    assert!(!home.0.join("digest.txt").exists());
}
#[tokio::test]
async fn board_ring_failed_replace_retries_original_watermark_unless_seen() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    state::private_dir(&home.0.join("digest.ring")).unwrap();
    let wake = json!({"pending_count":1,"wake":{"decision":"rung","reason":"peer","ring_at":liveness::now()}});
    adapter.update_mailbox(&wake).await;
    // The failed write must have been recorded for retry before the next update.
    ring_settled(&home).await;
    std::fs::remove_dir(home.0.join("digest.ring")).unwrap();
    adapter
        .update_mailbox(&json!({"pending_count":2,"wake":wake["wake"]}))
        .await;
    assert!(
        String::from_utf8(state::read(&home.0.join("digest.ring"), 256).unwrap())
            .unwrap()
            .starts_with("1\n")
    );
    std::fs::remove_file(home.0.join("digest.ring")).unwrap();
    state::private_dir(&home.0.join("digest.ring")).unwrap();
    let wake = json!({"pending_count":2,"wake":{"decision":"rung","reason":"later","ring_at":liveness::now()}});
    adapter.update_mailbox(&wake).await;
    ring_settled(&home).await;
    write(&home, "digest.seen", "2\n");
    std::fs::remove_dir(home.0.join("digest.ring")).unwrap();
    adapter.update_mailbox(&wake).await;
    assert!(!home.0.join("digest.ring").exists());
    adapter.close().await;
}
#[tokio::test]
async fn board_ring_local_listener_lease_and_attention_never_write_idle_marker() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let mut config = adapter_config(&fixture, &home);
    config.ring = true;
    let adapter = Adapter::enter(config).await.unwrap();
    adapter.set_ring_listener(std::sync::Arc::new(|| true));
    let before = liveness::now();
    adapter.heartbeat().await.unwrap();
    {
        let requests = fixture.requests.lock().unwrap();
        let beat = requests
            .iter()
            .find(|request| request.path.ends_with("/heartbeat"))
            .unwrap();
        assert!(beat.body["ring_armed_until"].as_f64().unwrap() >= before + 59.0);
    }
    adapter.update_mailbox(&json!({"pending_count":1,"wake":{"decision":"attention","reason":"busy","recipient_state":"unknown","queue_allowed":true,"ring_at":liveness::now()}})).await;
    tokio::time::sleep(Duration::from_millis(10)).await;
    assert!(!home.0.join("digest.ring").exists());
    assert_eq!(adapter.view().await.wake.unwrap()["decision"], "attention");
    adapter.close().await;
}
#[tokio::test]
async fn board_ring_late_hint_write_never_lowers_a_hook_seen_mark() {
    // Reported by the Codex review of the state-write offload (2026-10-19):
    // a slow `.seen` write from a delivered hint landed after a hook had
    // shown the newer digest and marked it, lowering the mark again.
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let seen = home.0.join("digest.seen");
    // Hold every queued step on the `.seen` path until the hook has run.
    let (release, held) = std::sync::mpsc::channel::<()>();
    let (started, ready) = tokio::sync::oneshot::channel();
    let gate = state::offload(&seen, move || {
        let _ = started.send(());
        let _ = held.recv();
        Ok(())
    });
    ready.await.unwrap();
    let first = adapter.view().await.watermark;
    assert!(adapter.deliver_hint().await.is_some());
    adapter.update_mailbox(&json!({"pending_count": 2})).await;
    let second = adapter.view().await.watermark;
    assert!(second > first);
    // The prompt or Stop hook shows the newer digest and marks it seen.
    write(&home, "digest.seen", &format!("{second}\n"));
    release.send(()).unwrap();
    gate.await.unwrap();
    state::drain(&seen).await;
    state::drain(&home.0.join("digest.txt")).await;
    let marked = String::from_utf8(state::read(&seen, 128).unwrap()).unwrap();
    adapter.close().await;
    assert_eq!(
        marked.trim(),
        second.to_string(),
        "a late hint write lowered .seen"
    );
}
#[tokio::test]
async fn board_ring_queued_hint_write_keeps_a_higher_seen_mark() {
    // The hint's `.seen` write is queued behind the digest; a hook that
    // raised the mark meanwhile keeps it.
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let digest = home.0.join("digest.txt");
    let (release, held) = std::sync::mpsc::channel::<()>();
    let (started, ready) = tokio::sync::oneshot::channel();
    let gate = state::offload(&digest, move || {
        let _ = started.send(());
        let _ = held.recv();
        Ok(())
    });
    ready.await.unwrap();
    let watermark = adapter.view().await.watermark;
    assert!(adapter.deliver_hint().await.is_some());
    let raised = watermark + 5;
    write(&home, "digest.seen", &format!("{raised}\n"));
    release.send(()).unwrap();
    gate.await.unwrap();
    state::drain(&digest).await;
    let marked = String::from_utf8(state::read(&home.0.join("digest.seen"), 128).unwrap()).unwrap();
    adapter.close().await;
    assert_eq!(
        marked.trim(),
        raised.to_string(),
        "the hint write lowered .seen"
    );
}
/// A marker as the plugin's shell hooks wrote it before they wrote 0600:
/// `printf > tmp && mv` under the user's umask, not the shim's owner-only
/// writer. On POSIX that is 0644 under the common umask 022. POSIX only:
/// the Windows marker check is the unchanged private-state ACL rule.
#[cfg(unix)]
fn hook_write(home: &Home, name: &str, text: &str) {
    hook_write_mode(home, name, text, 0o644);
}
#[cfg(unix)]
fn hook_write_mode(home: &Home, name: &str, text: &str, mode: u32) {
    let temp = home.0.join(format!("{name}.{}", std::process::id()));
    std::fs::write(&temp, text).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&temp, std::fs::Permissions::from_mode(mode)).unwrap();
    }
    std::fs::rename(&temp, home.0.join(name)).unwrap();
}
#[cfg(unix)]
#[test]
fn board_ring_liveness_reads_the_listener_records_the_stop_hook_writes() {
    // Found 2026-11-06 in the client/hook matrix: under the native shim the
    // Stop hook's wake never fired, because the listener records it writes
    // (`<key>.wake`, `<key>.<token>.wake-armed`) are not owner-only and the
    // shim refused to read them, so the daemon was never told a listener
    // was armed. Python's wake_liveness.armed_until reads them.
    let home = Home::new();
    let digest = home.0.join("task.txt");
    hook_write(&home, "task.wake", "waker\n");
    hook_write(&home, "task.waker.wake-armed", "waker\n1050\n");
    assert_eq!(liveness::armed_until_at(Some(&digest), 1000.0), 1050.0);
    // The other refusals still hold for hook-written records: a second
    // hard link, or a symlink in place of the owner file, arms nothing.
    std::fs::hard_link(
        home.0.join("task.waker.wake-armed"),
        home.0.join("spare.wake-armed"),
    )
    .unwrap();
    assert_eq!(liveness::armed_until_at(Some(&digest), 1000.0), 0.0);
    std::fs::remove_file(home.0.join("spare.wake-armed")).unwrap();
    assert_eq!(liveness::armed_until_at(Some(&digest), 1000.0), 1050.0);
    #[cfg(unix)]
    {
        hook_write(&home, "real.wake", "waker\n");
        std::fs::remove_file(home.0.join("task.wake")).unwrap();
        std::os::unix::fs::symlink(home.0.join("real.wake"), home.0.join("task.wake")).unwrap();
        assert_eq!(liveness::armed_until_at(Some(&digest), 1000.0), 0.0);
    }
}
#[cfg(unix)]
#[tokio::test]
async fn board_ring_reads_and_raises_a_seen_mark_the_hook_wrote() {
    // The prompt and Stop hooks mark mail seen in `<key>.seen` under the
    // user's umask. The shim must honour that mark (no repeated hint for
    // mail the session was shown) and still be able to raise it later.
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let watermark = adapter.view().await.watermark;
    hook_write(&home, "digest.seen", &format!("{watermark}\n"));
    assert_eq!(adapter.view().await.delivered, watermark);
    assert!(
        adapter.deliver_hint().await.is_none(),
        "the shim re-hinted mail the hook already marked seen"
    );
    adapter.update_mailbox(&json!({"pending_count": 2})).await;
    let newer = adapter.view().await.watermark;
    assert!(newer > watermark);
    assert!(adapter.deliver_hint().await.is_some());
    state::drain(&home.0.join("digest.txt")).await;
    state::drain(&home.0.join("digest.seen")).await;
    let marked = std::fs::read_to_string(home.0.join("digest.seen")).unwrap();
    adapter.close().await;
    assert_eq!(
        marked.trim(),
        newer.to_string(),
        "the shim could not raise a hook-written .seen"
    );
}
#[cfg(unix)]
#[test]
fn board_ring_liveness_refuses_listener_records_others_can_write_or_own() {
    // Readable by others is harmless (0644 above); writable by others, or
    // owned by another user, could forge a wake or suppress delivery.
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let home = Home::new();
    let digest = home.0.join("task.txt");
    hook_write(&home, "task.waker.wake-armed", "waker\n1050\n");
    for mode in [0o664, 0o666, 0o646] {
        hook_write_mode(&home, "task.wake", "waker\n", mode);
        assert_eq!(
            liveness::armed_until_at(Some(&digest), 1000.0),
            0.0,
            "{mode:o} owner file"
        );
        hook_write(&home, "task.wake", "waker\n");
        hook_write_mode(&home, "task.waker.wake-armed", "waker\n1050\n", mode);
        assert_eq!(
            liveness::armed_until_at(Some(&digest), 1000.0),
            0.0,
            "{mode:o} record"
        );
        hook_write(&home, "task.waker.wake-armed", "waker\n1050\n");
        assert_eq!(liveness::armed_until_at(Some(&digest), 1000.0), 1050.0);
    }
    // A foreign group or owner needs privilege to arrange; as root (a CI
    // container) both are refused, elsewhere this part is skipped.
    if rustix::process::getuid().is_root() {
        let record = home.0.join("task.waker.wake-armed");
        let nobody = 65534;
        std::os::unix::fs::lchown(&record, None, Some(nobody)).unwrap();
        std::fs::set_permissions(&record, std::fs::Permissions::from_mode(0o664)).unwrap();
        assert_eq!(std::fs::metadata(&record).unwrap().gid(), nobody);
        assert_eq!(
            liveness::armed_until_at(Some(&digest), 1000.0),
            0.0,
            "foreign group 0664"
        );
        std::fs::set_permissions(&record, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::os::unix::fs::lchown(&record, Some(nobody), None).unwrap();
        assert_eq!(
            liveness::armed_until_at(Some(&digest), 1000.0),
            0.0,
            "foreign owner"
        );
    } else {
        eprintln!("not root: foreign group and owner cases skipped");
    }
}
