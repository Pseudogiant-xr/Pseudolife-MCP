//! DB-free audit material pinned by unchanged coordination.audit_hash.
use super::*;

#[test]
fn append_material_matches_python_non_ascii_null_recipient() {
    let event = Event::new(
        "lease_break",
        "operator",
        "fixture-agent".into(),
        json!({"name": "资源", "reason": "operator"}),
    )
    .scoped("プロジェクト".into(), "café".into());
    let material = audit_material(7, &event, &audit_timestamp(1767323045.0).unwrap());
    assert_eq!(
        material,
        r#"["pseudolife-coordination-audit-v1",7,"lease_break","operator","","fixture-agent",null,"プロジェクト","café",null,1767323045.0,"","{\"name\":\"资源\",\"reason\":\"operator\"}"]"#
    );
    assert_eq!(
        hex_hash(format!("{}{}", "a".repeat(64), material)),
        "ff8a4ad68dc5af590ec71e5ca7a7e28a00681bf2a5b18daacc48f034ba6379b4"
    );
    assert_ne!(
        hex_hash(format!("{}{}", "b".repeat(64), material)),
        "ff8a4ad68dc5af590ec71e5ca7a7e28a00681bf2a5b18daacc48f034ba6379b4"
    );
    let altered = r#"["pseudolife-coordination-audit-v1",7,"lease_break","operator","","fixture-agent",null,"プロジェクト","café",null,1767323045.0,"","{\"name\":\"资源\",\"reason\":\"operator\"}"]"#.replace("资源", "資源");
    assert_ne!(
        hex_hash(format!("{}{}", "a".repeat(64), altered)),
        "ff8a4ad68dc5af590ec71e5ca7a7e28a00681bf2a5b18daacc48f034ba6379b4"
    );
}

#[test]
fn audit_timestamp_retains_python_decimal_and_rejects_nonfinite() {
    for (number, expected) in [
        (0.0, "0.0"),
        (-0.0, "-0.0"),
        (1.0, "1.0"),
        (0.00001, "1e-05"),
        (1e16, "1e+16"),
        (1767323045.0, "1767323045.0"),
    ] {
        assert_eq!(audit_timestamp(number).unwrap(), expected);
    }
    for number in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert!(matches!(audit_timestamp(number), Err(Error::Clock)));
    }
}
