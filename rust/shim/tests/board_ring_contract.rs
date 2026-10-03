mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{adapter::Adapter, liveness, state};
use serde_json::json;
use std::time::Duration;
fn write(home: &Home, name: &str, text: &str) {
    state::atomic_write(&home.0.join(name), text.as_bytes(), None).unwrap();
}
#[test]
fn board_ring_reason_matches_all_pinned_python_character_predicates() {
    use sha2::{Digest, Sha256};
    let mut digest = Sha256::new();
    for value in 0..0x110000 {
        let flags = char::from_u32(value).map_or(0, |value| {
            u8::from(liveness::alphanumeric(value)) | (u8::from(liveness::whitespace(value)) << 1)
        });
        digest.update([flags]);
    }
    assert_eq!(
        format!("{:x}", digest.finalize()),
        "906d1540ff9f88ff762090cae4951b36c7de882bed4ff457b5977892c8db1e96"
    );
    assert_eq!(
        liveness::reason("alpha\u{1c}beta\u{345} <>!"),
        "alpha beta "
    );
    assert_eq!(liveness::reason("<>!"), "unknown");
    assert_eq!(liveness::reason(&"a".repeat(80)).len(), 60);
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
    let at = liveness::now() + 0.08;
    let wake = json!({"generation":1,"pending_count":2,"pending_preview":[],"wake":{"decision":"rung","reason":"  peer\n sent<> mail! ","ring_at":at}});
    adapter.update_mailbox(&wake).await;
    assert!(!home.0.join("digest.ring").exists());
    tokio::time::sleep(Duration::from_millis(110)).await;
    assert_eq!(
        String::from_utf8(state::read(&home.0.join("digest.ring"), 256).unwrap()).unwrap(),
        "11\nrung peer sent mail\n"
    );
    std::fs::remove_file(home.0.join("digest.ring")).unwrap();
    adapter.update_mailbox(&wake).await;
    tokio::time::sleep(Duration::from_millis(15)).await;
    assert!(!home.0.join("digest.ring").exists());
    adapter.update_mailbox(&json!({"pending_count":3,"wake":{"decision":"rung","reason":"new","ring_at":liveness::now()+0.1}})).await;
    adapter.update_mailbox(&json!({"pending_count":0})).await;
    tokio::time::sleep(Duration::from_millis(130)).await;
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
    tokio::time::sleep(Duration::from_millis(20)).await;
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
    tokio::time::sleep(Duration::from_millis(20)).await;
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
