use super::*;

fn scratch(label: &str) -> std::path::PathBuf {
    let mut tag = [0u8; 8];
    getrandom::fill(&mut tag).unwrap();
    let tag: String = tag.iter().map(|b| format!("{b:02x}")).collect();
    let dir = std::env::temp_dir().join(format!("pl-secret-{label}-{}-{tag}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn a_created_secret_reads_back_and_is_never_replaced() {
    let dir = scratch("create");
    let path = dir.join("embedded_pg.secret");
    assert_eq!(read_secret(&path).unwrap(), None);
    let secret = create(&path).unwrap();
    assert_eq!(secret.len(), 64);
    assert!(
        secret
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    );
    assert_eq!(
        read_secret(&path).unwrap().as_deref(),
        Some(secret.as_str())
    );
    assert!(create(&path).unwrap_err().contains("already exists"));
    assert_eq!(
        read_secret(&path).unwrap().as_deref(),
        Some(secret.as_str())
    );
    // No temporary file is left beside it.
    let names: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(names, vec!["embedded_pg.secret".to_owned()]);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn a_secret_file_s_token_follows_the_oracle_s_grammar() {
    let dir = scratch("grammar");
    let path = dir.join("embedded_pg.secret");
    for (body, expected) in [
        (&b"abc\n"[..], Some("abc")),
        (b"abc\r\n\r\n", Some("abc")),
        (b"ab c", None),
        (b"\nabc", None),
        (b"", None),
        (b"\xff\xfe", None),
    ] {
        let _ = fs::remove_file(&path);
        let mut file = private_file(&path).unwrap();
        make_private(&file).unwrap();
        file.write_all(body).unwrap();
        drop(file);
        match expected {
            Some(token) => assert_eq!(read_secret(&path).unwrap().as_deref(), Some(token)),
            None => assert!(
                read_secret(&path)
                    .unwrap_err()
                    .contains("invalid bearer token"),
                "{body:?}"
            ),
        }
    }
    let _ = fs::remove_file(&path);
    let mut file = private_file(&path).unwrap();
    make_private(&file).unwrap();
    file.write_all(&[b'a'; 4097]).unwrap();
    drop(file);
    assert!(
        read_secret(&path)
            .unwrap_err()
            .contains("invalid bearer token")
    );
    fs::remove_dir_all(dir).unwrap();
}

#[cfg(unix)]
#[test]
fn a_secret_file_others_can_read_or_a_link_in_its_place_is_refused() {
    use std::os::unix::fs::PermissionsExt;
    let dir = scratch("unix");
    let path = dir.join("embedded_pg.secret");
    create(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(read_secret(&path).unwrap_err().contains("owner-only"));
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    // A hard link is a second name for the secret: refused, as the oracle
    // refuses it.
    let hard = dir.join("hard.secret");
    fs::hard_link(&path, &hard).unwrap();
    assert!(
        read_secret(&path)
            .unwrap_err()
            .contains("private regular file")
    );
    fs::remove_file(&hard).unwrap();
    assert!(read_secret(&path).unwrap().is_some());
    let link = dir.join("link.secret");
    std::os::unix::fs::symlink(&path, &link).unwrap();
    assert!(read_secret(&link).unwrap_err().contains("regular file"));
    // A link among the ancestors is refused too.
    let aliased = dir.join("alias");
    std::os::unix::fs::symlink(&dir, &aliased).unwrap();
    assert!(
        read_secret(&aliased.join("embedded_pg.secret"))
            .unwrap_err()
            .contains("redirects")
    );
    assert!(
        create(&aliased.join("other.secret"))
            .unwrap_err()
            .contains("redirects")
    );
    fs::remove_dir_all(dir).unwrap();
}
