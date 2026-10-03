use pseudolife_stdio::board::{identity, policy};
use serde_json::json;

#[test]
fn board_codex_turn_metadata_hands_parent_thread_to_registry() {
    let child = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    let parent = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
    let turn = json!({"thread_source":"subagent","thread_id":child,"parent_thread_id":parent});
    for value in [turn.clone(), json!(turn.to_string())] {
        assert_eq!(
            policy::parent_thread(&json!({"x-codex-turn-metadata":value}), child).as_deref(),
            Some(parent)
        );
    }
    assert!(policy::parent_thread(&json!({"x-codex-turn-metadata": {"thread_source":"subagent","thread_id":parent,"parent_thread_id":parent}}), child).is_none());
    assert!(policy::parent_thread(&json!({"x-codex-turn-metadata": {"thread_source":"subagent","thread_id":child,"parent_thread_id":child}}), child).is_none());
}

#[test]
fn board_cached_unread_hint_preserves_upstream_error_and_existing_structured_hint() {
    for is_error in [false, true] {
        let mut result: rmcp::model::CallToolResult = serde_json::from_value(json!({"content":[{"type":"text","text":"upstream"}],"structuredContent":{"result":7,"coordination_hint":"existing"},"isError":is_error})).unwrap();
        policy::append_hint(&mut result, "new");
        let value = serde_json::to_value(result).unwrap();
        assert_eq!(value["isError"], is_error);
        assert_eq!(value["content"][0]["text"], "upstream");
        assert_eq!(value["content"][1]["text"], "new");
        assert_eq!(value["structuredContent"]["coordination_hint"], "existing");
        assert_eq!(value["structuredContent"]["result"], 7);
    }
}

#[test]
fn board_legacy_context_proof_matches_python_ascii_hmac() {
    let context = identity::Context {
        bank_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa".into(),
        principal: "peer é😀".into(),
    };
    assert_eq!(
        identity::ascii_json(&json!(["peer é😀"])),
        "[\"peer \\u00e9\\ud83d\\ude00\"]"
    );
    let proof = identity::legacy_proof("fixture-key", &context, "fixture-agent", "fixture-nonce");
    assert_eq!(
        proof,
        "615e93a9c71ac3e8cefe7822e860696403039bdcf8f1703f23554f0453d8ab69"
    );
    assert!(identity::constant_time_equal(
        proof.as_bytes(),
        proof.as_bytes()
    ));
    assert!(!identity::constant_time_equal(b"same", b"sane"));
}
