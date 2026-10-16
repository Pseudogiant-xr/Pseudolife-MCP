use pseudolife_stdio::board::claims;
use sha2::{Digest, Sha256};

#[test]
fn board_claim_paths_refuse_traversal_git_aliases_devices_and_python_category_c() {
    for path in [
        "../a",
        "a/../b",
        ".Git/a",
        "aux.txt",
        "COM¹/a",
        "x:stream",
        "x\u{200b}",
        "x\u{e000}",
        "x\u{1c89}",
        "a/",
        "a.",
        "a ",
        "/a",
    ] {
        assert!(claims::normalize(path, true).is_err(), "{path:?}");
    }
    assert_eq!(claims::normalize("A\\.\\B//C", true).unwrap(), "A/B/C");
    assert_eq!(claims::normalize("Straße/É", false).unwrap(), "strasse/é");
    assert!(claims::normalize("future\u{1c89}", false).is_err());
}
#[test]
fn board_casefold_matches_complete_pinned_python_311_table() {
    let mut digest = Sha256::new();
    for point in 0..0x110000 {
        let folded = char::from_u32(point)
            .map(|c| claims::fold(&c.to_string()))
            .unwrap_or_default();
        // Surrogates cannot appear in a Rust string; Python leaves them unchanged.
        let words = if (0xd800..=0xdfff).contains(&point) {
            format!("{point:x}")
        } else {
            folded
                .chars()
                .map(|c| format!("{:x}", c as u32))
                .collect::<Vec<_>>()
                .join(",")
        };
        digest.update(format!("{point:x}:{words}\n"));
    }
    assert_eq!(
        format!("{:x}", digest.finalize()),
        "1ec2cc721d9f3e241787d4cf07ef74b49e0df64130b7b618538d50a35f6f2beb"
    );
}
#[tokio::test(flavor = "current_thread")]
async fn board_claim_preparation_refuses_conflicting_local_arguments() {
    let arguments =
        serde_json::json!({"action":"claim","worktree":"private","lease":"x","path":"a"})
            .as_object()
            .unwrap()
            .clone();
    assert_eq!(
        claims::prepare_arguments("memory_agents", arguments)
            .await
            .unwrap_err(),
        "unexpected_parameter"
    );
}
