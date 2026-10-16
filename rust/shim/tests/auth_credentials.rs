#![allow(dead_code)]
use pseudolife_stdio::credentials;
mod auth_fixture;
use auth_fixture::DisposableHome;
use credentials::{CredentialProvider, decode_token};
use std::{ffi::OsString, fs};

#[test]
fn test_static_snapshot_is_frozen_stable_and_secret_free() {
    let provider = CredentialProvider::new(Some("fixture-secret".into()), None).unwrap();
    let first = provider.snapshot().unwrap();
    assert!(first.token() == Some("fixture-secret"));
    assert_eq!(
        first.generation(),
        provider.snapshot().unwrap().generation()
    );
    for debug in [
        format!("{provider:?}"),
        format!("{first:?}"),
        format!("{:?}", first.generation()),
    ] {
        assert!(!debug.contains("fixture-secret"));
    }
    let another = CredentialProvider::new(Some("fixture-secret".into()), None).unwrap();
    assert_ne!(first.generation(), another.snapshot().unwrap().generation());
}

#[test]
fn test_file_snapshot_observes_atomic_rotation() {
    let home = DisposableHome::new();
    let path = home.private_token("token", b"first-secret");
    let provider = CredentialProvider::new(None, Some(path.clone())).unwrap();
    let first = provider.snapshot().unwrap();
    let next = home.private_token("replacement", b"second-secret");
    fs::rename(next, path).unwrap();
    let second = provider.snapshot().unwrap();
    assert!(first.token() == Some("first-secret"));
    assert!(second.token() == Some("second-secret"));
    assert_ne!(first.generation(), second.generation());
    assert_eq!(
        second.generation(),
        provider.snapshot().unwrap().generation()
    );
    assert_eq!(
        provider.require_current(&first).unwrap_err().0,
        "credential generation changed"
    );
    provider.require_current(&second).unwrap();
}

#[test]
fn test_generation_detects_unobserved_a_b_a_replacement() {
    let home = DisposableHome::new();
    let path = home.private_token("token", b"same-secret");
    let provider = CredentialProvider::new(None, Some(path.clone())).unwrap();
    let before = provider.snapshot().unwrap();
    for value in [b"other-secret".as_slice(), b"same-secret".as_slice()] {
        let next = home.private_token("replacement", value);
        fs::rename(next, &path).unwrap();
    }
    let after = provider.snapshot().unwrap();
    assert!(before.token() == after.token());
    assert_ne!(before.generation(), after.generation());
}

#[test]
fn test_from_environment_prefers_explicit_file_and_never_falls_back() {
    let home = DisposableHome::new();
    let path = home.private_token("token", b"file-secret");
    let provider = CredentialProvider::from_lookup(|key| match key {
        "PSEUDOLIFE_MCP_TOKEN_FILE" => Some(path.clone().into_os_string()),
        "PSEUDOLIFE_MCP_TOKEN" => Some(OsString::from("stale-secret")),
        _ => None,
    })
    .unwrap();
    assert!(provider.snapshot().unwrap().token() == Some("file-secret"));
    fs::remove_file(path).unwrap();
    let error = provider.snapshot().unwrap_err();
    assert_eq!(error.0, "configured credential file is missing");
    assert!(!error.to_string().contains("stale-secret"));
}

#[test]
fn test_empty_explicit_file_path_never_falls_back() {
    let error = CredentialProvider::from_lookup(|key| match key {
        "PSEUDOLIFE_MCP_TOKEN_FILE" => Some(OsString::new()),
        "PSEUDOLIFE_MCP_TOKEN" => Some("stale-secret".into()),
        _ => None,
    })
    .unwrap_err();
    assert_eq!(error.0, "configured credential file path is empty");
}
#[test]
fn test_tokenless_environment_is_valid() {
    assert!(
        CredentialProvider::from_lookup(|_| None)
            .unwrap()
            .snapshot()
            .unwrap()
            .token()
            .is_none()
    );
    assert!(
        CredentialProvider::from_lookup(|key| (key == "PSEUDOLIFE_MCP_TOKEN").then(OsString::new))
            .unwrap()
            .snapshot()
            .unwrap()
            .token()
            .is_none()
    );
}
#[test]
fn test_constructor_rejects_ambiguous_sources_without_echoing_values() {
    let home = DisposableHome::new();
    let error = CredentialProvider::new(Some("fixture-secret".into()), Some(home.path("token")))
        .unwrap_err();
    assert_eq!(error.0, "configure exactly one credential source");
    assert!(!error.to_string().contains("fixture-secret"));
}
#[test]
fn test_malformed_or_oversized_file_is_rejected_without_payload() {
    let home = DisposableHome::new();
    for payload in [
        vec![],
        b"two\nlines".to_vec(),
        b"surrounded ".to_vec(),
        vec![b'x'; 4097],
        vec![0xff],
        b"bad\x00byte".to_vec(),
        "bad\u{85}space".as_bytes().to_vec(),
    ] {
        let path = home.private_token("token", &payload);
        let error = CredentialProvider::new(None, Some(path))
            .unwrap()
            .snapshot()
            .unwrap_err();
        assert_eq!(error.0, "credential file contains an invalid bearer token");
    }
    assert!(decode_token(&vec![b'x'; 4096]).is_ok());
}
#[test]
fn test_one_or_more_terminal_newlines_are_accepted() {
    for value in [b"fixture\r\n".as_slice(), b"fixture\n\r\n".as_slice()] {
        assert!(decode_token(value).unwrap() == "fixture");
    }
    // Python validates a static value but preserves it verbatim in snapshots.
    let provider = CredentialProvider::new(Some("fixture\r\n".into()), None).unwrap();
    assert!(provider.snapshot().unwrap().token() == Some("fixture\r\n"));
}
#[test]
fn test_hardlink_and_nonregular_file_are_rejected() {
    let home = DisposableHome::new();
    let source = home.private_token("source", b"fixture-secret");
    let hardlink = home.path("hardlink");
    fs::hard_link(&source, &hardlink).unwrap();
    assert_eq!(
        CredentialProvider::new(None, Some(hardlink))
            .unwrap()
            .snapshot()
            .unwrap_err()
            .0,
        "credential file must be a private regular file"
    );
    assert!(
        CredentialProvider::new(None, Some(home.0.clone()))
            .unwrap()
            .snapshot()
            .is_err()
    );
}
#[test]
fn test_unprotected_default_acl_is_rejected() {
    let home = DisposableHome::new();
    let path = home.path("unprotected");
    fs::write(&path, b"fixture-secret").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
    }
    assert_eq!(
        CredentialProvider::new(None, Some(path))
            .unwrap()
            .snapshot()
            .unwrap_err()
            .0,
        "credential file must be owner-only"
    );
}
#[cfg(unix)]
#[test]
fn test_symlink_and_symlink_ancestor_are_rejected() {
    use std::os::unix::fs::symlink;
    let home = DisposableHome::new();
    let source = home.private_token("source", b"fixture-secret");
    symlink(source, home.path("symlink")).unwrap();
    assert_eq!(
        CredentialProvider::new(None, Some(home.path("symlink")))
            .unwrap()
            .snapshot()
            .unwrap_err()
            .0,
        "credential file must be a private regular file"
    );
    fs::create_dir(home.path("real")).unwrap();
    let token = home.private_token("real/token", b"fixture-secret");
    symlink(token.parent().unwrap(), home.path("redirect")).unwrap();
    assert_eq!(
        CredentialProvider::new(None, Some(home.path("redirect/token")))
            .unwrap()
            .snapshot()
            .unwrap_err()
            .0,
        "credential path must not contain redirects"
    );
}

#[test]
fn test_missing_dynamic_credential_fails_closed_then_recovers() {
    let home = DisposableHome::new();
    let provider = CredentialProvider::new(None, Some(home.path("token"))).unwrap();
    assert!(provider.snapshot().is_err());
    home.private_token("token", b"fixture-secret");
    assert!(provider.snapshot().is_ok());
}

#[test]
fn test_concurrent_atomic_rotation_never_returns_torn_credentials() {
    let home = DisposableHome::new();
    let path = home.private_token("token", &vec![b'a'; 2000]);
    let provider = CredentialProvider::new(None, Some(path.clone())).unwrap();
    let root = home.0.clone();
    let worker = std::thread::spawn(move || {
        for index in 0..40 {
            let next = root.join("replacement");
            fs::write(&next, vec![if index % 2 == 0 { b'a' } else { b'b' }; 2000]).unwrap();
            auth_fixture::protect(&next).unwrap();
            for attempt in 0..20 {
                match fs::rename(&next, &path) {
                    Ok(()) => break,
                    Err(error)
                        if cfg!(windows)
                            && (error.kind() == std::io::ErrorKind::PermissionDenied
                                || matches!(error.raw_os_error(), Some(32 | 33)))
                            && attempt < 19 =>
                    {
                        std::thread::sleep(std::time::Duration::from_millis(5))
                    }
                    Err(_) => panic!("disposable rotation failed"),
                }
            }
        }
    });
    for _ in 0..100 {
        let snapshot = provider.snapshot().unwrap();
        let token = snapshot.token().unwrap();
        assert!(token.as_bytes() == vec![b'a'; 2000] || token.as_bytes() == vec![b'b'; 2000]);
    }
    worker.join().unwrap();
}
