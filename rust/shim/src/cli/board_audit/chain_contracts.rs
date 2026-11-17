//! Defensive error paths that the archive reader normally rejects first.
use super::*;

#[test]
fn nonnumeric_cutoff_never_becomes_a_verified_anchor() {
    let secret = J::Str("synthetic-secret-must-not-escape".into());
    for error in [
        equals_integer(&secret, 0).unwrap_err(),
        cutoff_json(&secret).unwrap_err(),
    ] {
        let diagnostic = format!("{error:?}");
        assert!(diagnostic.contains("cutoff is not numeric"));
        assert!(!diagnostic.contains("synthetic-secret"));
    }
    assert!(equals_integer(&J::Int("0".into()), 0).unwrap());
    assert!(cutoff_json(&J::Int("0".into())).is_ok());
}

#[test]
fn nonfinite_cutoff_never_becomes_a_verified_anchor() {
    for now in [f64::INFINITY, f64::NEG_INFINITY, f64::NAN] {
        let diagnostic = format!("{:?}", audit_cutoff(now, 1).unwrap_err());
        assert!(diagnostic.contains("created_at"));
    }
    assert!(audit_cutoff(1_700_000_000.0, 1).is_ok());
}

#[test]
fn unencodable_hash_material_is_refused_without_row_contents() {
    let value = serde_json::json!({"seq":1,"prev_hash":GENESIS,"hash":"",
        "payload":"synthetic-secret-must-not-escape"});
    let mut unsupported = value.clone();
    unsupported["recipient_agent_id"] = serde_json::from_str("1e400").unwrap();
    let row = |value| Row {
        value,
        seq: 1,
        created_at: 1.0,
        pending_payload: None,
    };
    let diagnostic = format!(
        "{:?}",
        Walker::new(None).push(row(unsupported)).unwrap_err()
    );
    assert!(diagnostic.contains("hash material"));
    assert!(!diagnostic.contains("synthetic-secret"));
    // Encodable material continues to the ordinary integrity check.
    assert!(Walker::new(None).push(row(value)).unwrap().is_some());
}
