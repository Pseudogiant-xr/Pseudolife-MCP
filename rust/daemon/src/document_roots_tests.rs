//! `document_roots`: which paths `document_ingest` may read.

use super::*;

/// A fresh directory with `root/` (one file, one subdirectory with a file)
/// and `outside/` (one file) beside it.
struct Tree {
    base: PathBuf,
}

impl Tree {
    fn new(tag: &str) -> Tree {
        let base = std::env::temp_dir().join(format!(
            "pl-document-roots-{tag}-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(base.join("root").join("sub")).unwrap();
        std::fs::create_dir_all(base.join("outside")).unwrap();
        std::fs::write(base.join("root").join("guide.md"), b"inside").unwrap();
        std::fs::write(base.join("root").join("sub").join("deep.txt"), b"deeper").unwrap();
        std::fs::write(base.join("outside").join("secret.txt"), b"not a document").unwrap();
        Tree { base }
    }

    fn at(&self, rel: &str) -> String {
        let mut p = self.base.clone();
        for part in rel.split('/') {
            p.push(part);
        }
        p.to_str().unwrap().to_string()
    }

    fn roots(&self) -> Vec<String> {
        vec![self.at("root")]
    }
}

impl Drop for Tree {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.base);
    }
}

fn refused(r: Result<Admitted, ToolError>) -> (String, String) {
    match r {
        Err(ToolError::Refused {
            code,
            detail,
            param,
            ..
        }) => {
            assert_eq!(param, Some(Value::from("path")));
            (code, detail.unwrap_or_default())
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
}

fn file(r: Result<Admitted, ToolError>) -> PathBuf {
    match r {
        Ok(Admitted::File { path, .. }) => path,
        other => panic!("expected an admitted file, got {other:?}"),
    }
}

#[test]
fn a_file_inside_the_root_or_below_it_is_admitted_resolved() {
    let t = Tree::new("inside");
    let got = file(admit(&t.at("root/guide.md"), &t.roots()));
    assert_eq!(got, std::fs::canonicalize(t.at("root/guide.md")).unwrap());
    let got = file(admit(&t.at("root/sub/deep.txt"), &t.roots()));
    assert_eq!(
        got,
        std::fs::canonicalize(t.at("root/sub/deep.txt")).unwrap()
    );
    // A `..` that stays inside is refused with the plain spelling to use.
    let (code, detail) = refused(admit(&t.at("root/sub/../guide.md"), &t.roots()));
    assert_eq!(code, DOTDOT);
    assert!(detail.contains(&t.at("root/guide.md")), "{detail}");
    // A missing file inside a root says so (it tells nothing outside).
    assert_eq!(
        admit(&t.at("root/missing.txt"), &t.roots()).unwrap(),
        Admitted::Missing
    );
    assert_eq!(
        admit(&t.at("root/nodir/missing.txt"), &t.roots()).unwrap(),
        Admitted::Missing
    );
}

#[test]
fn a_path_outside_every_root_is_refused_alike_whether_or_not_it_exists() {
    let t = Tree::new("outside");
    let (code, existing) = refused(admit(&t.at("outside/secret.txt"), &t.roots()));
    assert_eq!(code, OUTSIDE);
    let (code, missing) = refused(admit(&t.at("outside/missing.txt"), &t.roots()));
    assert_eq!(code, OUTSIDE);
    // The same answer but for the path itself: no existence oracle.
    assert_eq!(
        existing.replace("secret.txt", "X"),
        missing.replace("missing.txt", "X")
    );
    // The answer names the key, the roots and the one fix.
    assert!(existing.contains(ROOTS_KEY), "{existing}");
    assert!(existing.contains(ROOTS_ENV), "{existing}");
    assert!(existing.contains(&t.at("root")), "{existing}");
    assert!(
        existing.contains(&format!("copy the file into {}", t.at("root"))),
        "{existing}"
    );
    // A sibling whose name starts with the root's is not inside it.
    std::fs::create_dir_all(t.at("rootless")).unwrap();
    std::fs::write(t.at("rootless/x.txt"), b"x").unwrap();
    assert_eq!(
        refused(admit(&t.at("rootless/x.txt"), &t.roots())).0,
        OUTSIDE
    );
}

#[test]
fn a_path_outside_is_refused_before_the_disk_is_asked_about_it() {
    let t = Tree::new("unlooked");
    let looked = std::cell::RefCell::new(Vec::new());
    let look = |p: &Path| looked.borrow_mut().push(p.to_path_buf());
    for path in [
        t.at("outside/secret.txt"),
        t.at("outside/missing.txt"),
        t.at("root/../outside/secret.txt"),
    ] {
        assert_eq!(refused(admit_observed(&path, &t.roots(), &look)).0, OUTSIDE);
    }
    assert!(looked.borrow().is_empty(), "{:?}", looked.borrow());
    // The near miss: a path inside is looked at, and admitted.
    file(admit_observed(&t.at("root/guide.md"), &t.roots(), &look));
    assert_eq!(looked.borrow().len(), 1);
}

#[test]
fn a_nul_in_the_path_is_refused() {
    let t = Tree::new("nul");
    let path = format!("{}\0x", t.at("root/guide.md"));
    assert_eq!(refused(admit(&path, &t.roots())).0, OUTSIDE);
    assert!(root_problem(&format!("{}\0x", t.at("root"))).is_some());
}

#[test]
fn a_dotdot_escape_is_refused() {
    let t = Tree::new("dotdot");
    for path in [
        t.at("root/../outside/secret.txt"),
        t.at("root/sub/../../outside/secret.txt"),
        t.at("root/../outside/missing.txt"),
    ] {
        assert_eq!(refused(admit(&path, &t.roots())).0, OUTSIDE, "{path}");
    }
}

#[test]
fn a_relative_path_is_refused_with_the_absolute_form() {
    let t = Tree::new("relative");
    let (code, detail) = refused(admit("guide.md", &t.roots()));
    assert_eq!(code, NOT_ABSOLUTE);
    let example = Path::new(&t.at("root")).join("guide.md");
    assert!(detail.contains(&example.display().to_string()), "{detail}");
}

#[test]
fn no_roots_refuses_every_ingest_naming_the_setting() {
    let t = Tree::new("none");
    for path in [
        t.at("root/guide.md"),
        t.at("outside/secret.txt"),
        "x".into(),
    ] {
        let (code, detail) = refused(admit(&path, &[]));
        assert_eq!(code, NONE_CONFIGURED);
        assert!(detail.contains("none is configured"), "{detail}");
        assert!(
            detail.contains(ROOTS_KEY) && detail.contains(ROOTS_ENV),
            "{detail}"
        );
    }
}

#[test]
fn roots_that_are_not_directories_are_inert_and_named() {
    let t = Tree::new("inert");
    let missing = t.at("no-such-dir");
    let a_file = t.at("outside/secret.txt");
    let (code, detail) = refused(admit(
        &t.at("root/guide.md"),
        &[missing.clone(), a_file.clone()],
    ));
    assert_eq!(code, NONE_CONFIGURED);
    assert!(
        detail.contains(&missing) && detail.contains(&a_file),
        "{detail}"
    );
    // One usable root among inert ones is enough.
    let got = file(admit(&t.at("root/guide.md"), &[missing, t.at("root")]));
    assert_eq!(got, std::fs::canonicalize(t.at("root/guide.md")).unwrap());
}

#[cfg(unix)]
mod links {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn a_link_inside_a_root_pointing_outside_is_refused() {
        let t = Tree::new("link-out");
        symlink(t.at("outside/secret.txt"), t.at("root/link.txt")).unwrap();
        symlink(t.at("outside"), t.at("root/linkdir")).unwrap();
        symlink(t.at("outside/gone.txt"), t.at("root/dangling.txt")).unwrap();
        for path in [
            t.at("root/link.txt"),
            t.at("root/linkdir/secret.txt"),
            // A missing name below a link out: the same refusal, so the
            // answer cannot tell what exists out there.
            t.at("root/linkdir/missing.txt"),
            t.at("root/dangling.txt"),
        ] {
            assert_eq!(refused(admit(&path, &t.roots())).0, OUTSIDE, "{path}");
        }
    }

    #[test]
    fn a_link_inside_a_root_pointing_inside_it_is_admitted() {
        let t = Tree::new("link-in");
        symlink(t.at("root/sub/deep.txt"), t.at("root/alias.txt")).unwrap();
        let got = file(admit(&t.at("root/alias.txt"), &t.roots()));
        assert_eq!(
            got,
            std::fs::canonicalize(t.at("root/sub/deep.txt")).unwrap()
        );
    }

    #[test]
    fn a_root_that_is_a_link_to_a_real_directory_works() {
        let t = Tree::new("root-link");
        symlink(t.at("root"), t.at("root-link")).unwrap();
        let roots = vec![t.at("root-link")];
        // Spelled through the link, as the root is written.
        let got = file(admit(&t.at("root-link/guide.md"), &roots));
        assert_eq!(got, std::fs::canonicalize(t.at("root/guide.md")).unwrap());
        // Spelled as the directory it resolves to.
        let got = file(admit(&t.at("root/sub/deep.txt"), &roots));
        assert_eq!(
            got,
            std::fs::canonicalize(t.at("root/sub/deep.txt")).unwrap()
        );
        assert_eq!(
            refused(admit(&t.at("outside/secret.txt"), &roots)).0,
            OUTSIDE
        );
    }

    #[test]
    fn a_root_resolving_into_a_pseudo_filesystem_is_inert() {
        let t = Tree::new("root-pseudo");
        symlink("/proc", t.at("to-proc")).unwrap();
        symlink("/", t.at("to-slash")).unwrap();
        for root in [t.at("to-proc"), t.at("to-slash")] {
            let (code, _) = refused(admit(
                &format!("{root}/self/status"),
                std::slice::from_ref(&root),
            ));
            assert_eq!(code, NONE_CONFIGURED, "{root}");
        }
    }
}

#[cfg(unix)]
#[test]
fn pseudo_filesystems_and_slash_are_never_roots() {
    for root in [
        "/proc",
        "/proc/self",
        "/sys",
        "/sys/kernel",
        "/dev",
        "/dev/shm",
        "/",
        "/proc/../proc",
    ] {
        let problem = root_problem(root).unwrap_or_else(|| panic!("{root} accepted"));
        assert!(problem.contains("/proc, /sys or /dev"), "{problem}");
    }
    // Near misses: names that only start like one.
    for root in [
        "/documents",
        "/data/docs",
        "/procedures",
        "/devices",
        "/system",
    ] {
        assert_eq!(root_problem(root), None, "{root}");
    }
    assert!(root_problem("relative/dir").is_some());
    assert!(root_problem("").is_some());
}

#[cfg(unix)]
#[test]
fn the_environment_list_is_split_and_checked() {
    assert_eq!(env_roots(None).unwrap(), Vec::<String>::new());
    assert_eq!(
        env_roots(Some("/documents::/srv/docs")).unwrap(),
        vec!["/documents".to_string(), "/srv/docs".to_string()]
    );
    let error = env_roots(Some("/documents:/proc")).unwrap_err();
    assert!(
        error.starts_with(ROOTS_ENV) && error.contains("/proc"),
        "{error}"
    );
    // The union: the environment's first, each once.
    assert_eq!(
        configured(
            &["/srv/docs".into(), "/documents".into()],
            Some("/documents")
        )
        .unwrap(),
        vec!["/documents".to_string(), "/srv/docs".to_string()]
    );
    let error = configured(&["/sys".into()], None).unwrap_err();
    assert!(error.starts_with(ROOTS_KEY), "{error}");
}

#[cfg(unix)]
#[test]
fn the_config_key_is_read_and_a_pseudo_root_refuses_the_load() {
    let dir = std::env::temp_dir().join(format!(
        "pl-document-roots-config-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let load = |text: &str| {
        let file = dir.join("config.yaml");
        std::fs::write(&file, text).unwrap();
        crate::config::load(&file)
    };
    let ok = load("memory:\n  reference:\n    ingest_roots: [/documents, /srv/docs]\n").unwrap();
    assert_eq!(
        ok.memory.reference.ingest_roots,
        vec!["/documents".to_string(), "/srv/docs".to_string()]
    );
    assert!(
        load("memory:\n  reference:\n    chunk_size: 40\n")
            .unwrap()
            .memory
            .reference
            .ingest_roots
            .is_empty()
    );
    for bad in [
        "memory:\n  reference:\n    ingest_roots: [/proc]\n",
        "memory:\n  reference:\n    ingest_roots: [/documents, /dev]\n",
        "memory:\n  reference:\n    ingest_roots: [/]\n",
        "memory:\n  reference:\n    ingest_roots: [docs]\n",
        "memory:\n  reference:\n    ingest_roots: /documents\n",
    ] {
        let error = load(bad).unwrap_err().to_string();
        assert!(error.contains(ROOTS_KEY), "{bad}: {error}");
    }
    let _ = std::fs::remove_dir_all(&dir);
}

/// The open handle is checked too: whatever the path said, a file that is
/// open outside the resolved roots is not read.
#[cfg(target_os = "linux")]
#[test]
fn the_opened_file_is_checked_against_the_roots() {
    let t = Tree::new("opened");
    let roots = vec![std::fs::canonicalize(t.at("root")).unwrap()];
    let inside = std::fs::File::open(t.at("root/guide.md")).unwrap();
    assert!(opened_inside(&inside, &roots));
    let outside = std::fs::File::open(t.at("outside/secret.txt")).unwrap();
    assert!(!opened_inside(&outside, &roots));
    // Through the document read: the same file reads without roots and is
    // refused against roots that do not hold it.
    let path = t.at("outside/secret.txt");
    assert!(crate::documents::read_document_bytes_within(&path, 1024, None).is_ok());
    match crate::documents::read_document_bytes_within(&path, 1024, Some(&roots)) {
        Err(crate::documents::ReadError::Refused(m)) => {
            assert!(m.starts_with(OUTSIDE), "{m}")
        }
        other => panic!("{other:?}"),
    }
}

/// A path that passes through a folder outside and comes back is refused
/// without the disk being asked anything, so whether the folder it passed
/// through exists cannot change the answer.
#[test]
fn a_path_that_wanders_outside_and_back_tells_nothing_about_outside() {
    let t = Tree::new("wander");
    let looked = std::cell::RefCell::new(Vec::new());
    let look = |p: &Path| looked.borrow_mut().push(p.to_path_buf());
    let mut answers = Vec::new();
    for through in ["outside", "no-such-folder"] {
        for name in ["guide.md", "missing.txt"] {
            let path = t.at(&format!("root/../{through}/../root/{name}"));
            let (code, detail) = refused(admit_observed(&path, &t.roots(), &look));
            assert_eq!(code, DOTDOT, "{path}");
            answers.push(detail.replace(through, "X"));
        }
    }
    assert_eq!(answers[0], answers[2]);
    assert_eq!(answers[1], answers[3]);
    assert!(looked.borrow().is_empty(), "{:?}", looked.borrow());
}

/// A folder inside a root that the daemon cannot search: the file is named
/// as unreadable, not missing (the cause, and the fix).
#[cfg(unix)]
#[test]
fn a_file_below_an_unsearchable_folder_is_named_unreadable() {
    use std::os::unix::fs::PermissionsExt;
    let t = Tree::new("locked");
    std::fs::create_dir_all(t.at("root/locked")).unwrap();
    std::fs::write(t.at("root/locked/f.txt"), b"x").unwrap();
    std::fs::set_permissions(t.at("root/locked"), std::fs::Permissions::from_mode(0o000)).unwrap();
    if std::fs::metadata(t.at("root/locked/f.txt")).is_ok() {
        std::fs::set_permissions(t.at("root/locked"), std::fs::Permissions::from_mode(0o755))
            .unwrap();
        eprintln!("skipped: this account searches any folder (root)");
        return;
    }
    let got = admit(&t.at("root/locked/f.txt"), &t.roots());
    std::fs::set_permissions(t.at("root/locked"), std::fs::Permissions::from_mode(0o755)).unwrap();
    match got {
        Ok(Admitted::Unreadable(why)) => assert!(why.contains("ermission"), "{why}"),
        other => panic!("expected unreadable, got {other:?}"),
    }
    let text = unreadable("/documents/locked/f.txt", "Permission denied (os error 13)");
    assert!(
        text.starts_with(UNREADABLE) && text.contains("chmod a+r"),
        "{text}"
    );
}

#[cfg(windows)]
#[test]
fn a_whole_drive_is_never_a_root() {
    assert!(root_problem(r"C:\").is_some());
    assert!(root_problem(r"C:\documents").is_none());
}
