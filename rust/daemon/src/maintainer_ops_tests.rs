use super::*;

#[test]
fn labels_follow_check_label() {
    assert_eq!(
        check_label(Some(&json!("  Laptop key "))).unwrap(),
        "Laptop key"
    );
    // NFC first: a decomposed é counts as one code point.
    let decomposed = format!("{}{}", "e\u{301}", "x".repeat(63));
    assert_eq!(
        check_label(Some(&json!(decomposed)))
            .unwrap()
            .chars()
            .count(),
        64
    );
    for bad in [
        json!(""),
        json!("   "),
        json!("a\u{2028}b"),
        json!("a\u{a0}b"),
        json!("a\tb"),
        json!("a\u{200b}b"),
        json!("x".repeat(65)),
        json!(1),
        Value::Null,
    ] {
        assert!(check_label(Some(&bad)).is_err(), "{bad}");
    }
    assert!(check_label(None).is_err());
}

#[test]
fn holds_are_ints_within_the_delegation_bounds() {
    assert_eq!(hold(Some(&json!(60))).unwrap(), 60);
    assert_eq!(hold(Some(&json!(604800))).unwrap(), 604800);
    for bad in [
        json!(59),
        json!(604801),
        json!(60.0),
        json!(true),
        json!("60"),
        Value::Null,
    ] {
        assert!(hold(Some(&bad)).is_err(), "{bad}");
    }
}

#[test]
fn projects_are_stripped_and_bounded() {
    assert_eq!(
        project(Some(&json!("Pseudolife-MCP"))).unwrap(),
        "Pseudolife-MCP"
    );
    for bad in [
        json!(""),
        json!(" x"),
        json!("x "),
        json!("x".repeat(109)),
        json!(1),
    ] {
        assert!(project(Some(&bad)).is_err(), "{bad}");
    }
    assert!(project(Some(&json!("x".repeat(108)))).is_ok());
}

#[test]
fn the_options_carry_the_payloads_challenge() {
    let payload = r#"{"expires_at":1.5,"label":"k","nonce":"00","purpose":"enrol-bootstrap"}"#;
    let create = create_options("console.example.com", payload, &["cred-a".into()]);
    // `b64url(sha256(payload))`.
    assert_eq!(create["challenge"], json!(challenge_b64(payload)));
    assert_eq!(create["challenge"].as_str().unwrap().len(), 43);
    assert_eq!(create["timeout"], json!(120000));
    assert_eq!(create["user"]["id"].as_str().unwrap().len(), 22);
    assert_eq!(
        create["excludeCredentials"],
        json!([{"type": "public-key", "id": "cred-a"}])
    );
    let get = get_options("console.example.com", payload, &[]);
    assert_eq!(get["allowCredentials"], json!([]));
    assert_eq!(get["userVerification"], json!("required"));
}
