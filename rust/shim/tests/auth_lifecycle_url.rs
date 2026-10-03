#![allow(dead_code)]
use pseudolife_stdio::daemon_url;

#[test]
fn test_daemon_url_rejects_secret_bearing_components_without_echo() {
    for value in [
        "http://user:fixture-secret@example.com",
        "http://example.com/private",
        "http://example.com/?secret=value",
        "http://example.com/#fragment",
        "http://example.com:65536",
        " http://example.com",
        "http://example.com\t",
    ] {
        let error = daemon_url::validate(value).unwrap_err();
        assert_eq!(error, daemon_url::INVALID_URL_MESSAGE);
        assert!(!error.contains("fixture-secret"));
    }
}

#[test]
fn test_is_loopback_url_is_keyed_on_the_host_only() {
    for (value, expected) in [
        ("http://localhost:8765", true),
        ("http://LOCALHOST:8765", true),
        ("http://127.8.9.1:8765", true),
        ("http://[::1]:8765", true),
        ("http://[::ffff:127.0.0.1]:8765", true),
        ("http://10.0.0.7:8765", false),
        ("http://localhost.example.com:8765", false),
    ] {
        let url = daemon_url::validate(value).unwrap();
        assert_eq!(daemon_url::is_loopback(&url), expected);
    }
}

#[test]
fn test_daemon_origin_preserves_authority_spelling() {
    assert_eq!(
        daemon_url::validate("HTTP://LOCALHOST:80/").unwrap(),
        "http://LOCALHOST:80"
    );
    assert_eq!(
        daemon_url::validate("http://example.com?#").unwrap(),
        "http://example.com"
    );
}
