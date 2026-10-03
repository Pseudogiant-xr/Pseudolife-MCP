mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::adapter::Adapter;
use serde_json::json;

#[tokio::test]
async fn board_heartbeat_reports_only_seen_turn_and_remembers_old_daemon_fallback() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    adapter.heartbeat().await.unwrap();
    adapter.note_turn().await;
    fixture
        .heartbeat
        .lock()
        .unwrap()
        .push_back((400, json!({"error":"unexpected_parameter"})));
    adapter.heartbeat().await.unwrap();
    adapter.note_turn().await;
    adapter.heartbeat().await.unwrap();
    let bodies = fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .filter(|request| request.path.ends_with("/heartbeat"))
        .map(|request| request.body.clone())
        .collect::<Vec<_>>();
    assert_eq!(bodies.len(), 4);
    assert!(bodies[0].get("active").is_none());
    assert_eq!(bodies[1]["active"], true);
    assert!(bodies[2].get("active").is_none());
    assert!(bodies[3].get("active").is_none());
    adapter.close().await;
}

#[tokio::test]
async fn board_heartbeat_retries_transient_http_three_times_and_refuses_changed_generation() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    fixture.heartbeat.lock().unwrap().extend([
        (503, json!({"error":"principals_unavailable"})),
        (429, json!({"error":"rate_limited"})),
        (
            200,
            json!({"generation":1,"pending_count":0,"pending_preview":[]}),
        ),
    ]);
    adapter.heartbeat().await.unwrap();
    assert_eq!(fixture.count("/heartbeat"), 3);
    assert_eq!(adapter.view().await.count, Some(0));
    fixture.heartbeat.lock().unwrap().push_back((
        200,
        json!({"generation":2,"pending_count":5,"pending_preview":[]}),
    ));
    assert_eq!(
        adapter.heartbeat().await.unwrap_err().code,
        "attachment_not_current"
    );
    assert_eq!(adapter.view().await.count, Some(0));
    adapter.close().await;
}

#[tokio::test]
async fn board_identity_rejection_pauses_background_instance_requests() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let config = adapter_config(&fixture, &home);
    let provider = config.provider.clone();
    let adapter = Adapter::enter(config).await.unwrap();
    *fixture.bank.lock().unwrap() = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb".into();
    assert_eq!(
        adapter
            .validated_headers(&provider.snapshot().unwrap())
            .await
            .unwrap_err()
            .code,
        "bank_identity_mismatch"
    );
    let count = fixture.requests.lock().unwrap().len();
    tokio::time::sleep(std::time::Duration::from_millis(350)).await;
    assert_eq!(fixture.requests.lock().unwrap().len(), count);
    assert_eq!(fixture.count("/heartbeat"), 0);
    assert_eq!(fixture.count("/attach"), 1);
    adapter.close().await;
}
