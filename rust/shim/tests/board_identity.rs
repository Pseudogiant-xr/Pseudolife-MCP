use pseudolife_stdio::board::{identity, policy};
use serde_json::json;

#[test]
fn board_invalid_codex_metadata_never_uses_coordination_identity() {
    for raw in [
        json!({}),
        json!({"threadId":"bad"}),
        json!({"threadId":"AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"}),
    ] {
        assert!(policy::thread_id(&raw).is_none());
    }
    let id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    assert_eq!(
        policy::thread_id(&json!({"threadId":id})).as_deref(),
        Some(id)
    );
}

#[test]
fn board_shared_host_refuses_writes_before_claim_preparation() {
    assert!(policy::shared_host(Some("claude-desktop"), None));
    assert!(policy::shared_host(Some("Tunnel"), Some("0")));
    assert!(policy::shared_host(Some("codex"), Some("yes")));
    assert!(!policy::shared_host(Some("codex"), Some("false")));
    assert!(policy::requires_identity(
        "memory_message",
        &json!({"action":"receive"})
    ));
    assert!(policy::requires_identity(
        "memory_agents",
        &json!({"action":"claim"})
    ));
    assert!(!policy::requires_identity(
        "memory_agents",
        &json!({"action":"list"})
    ));
}

#[test]
fn board_context_identity_rejects_untrusted_values() {
    let bank = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    let context = identity::Context::parse(&json!({"bank_id":bank,"principal":"peer é"})).unwrap();
    assert_eq!(context.encoded_principal(), "peer%20%C3%A9");
    for value in [
        json!({"bank_id":bank.to_uppercase(),"principal":"peer"}),
        json!({"bank_id":bank,"principal":"a\nb"}),
        json!({"bank_id":bank,"principal":""}),
    ] {
        assert!(identity::Context::parse(&value).is_err());
    }
    let state = json!({"version":2,"bank_id":bank,"principal":"peer é"});
    assert!(context.check_binding(&state).is_ok());
    assert!(
        context
            .check_binding(&json!({"version":1,"bank_id":bank,"principal":"peer é"}))
            .is_err()
    );
}

#[test]
fn board_state_namespace_is_bound_to_endpoint_not_bearer() {
    let root = std::path::Path::new("private");
    let a = identity::bound_state_path(root, "http://localhost:1/", "thread");
    assert_eq!(
        a,
        identity::bound_state_path(root, "http://localhost:1", "thread")
    );
    assert_ne!(
        a,
        identity::bound_state_path(root, "http://localhost:2", "thread")
    );
}
