use super::*;
use std::fs;

/// A disposable folder under the temporary directory as the OS gives it,
/// deliberately not resolved: on macOS, and under a `TMPDIR` reached through
/// a link, the folder sits behind a link the rule must accept.
pub(crate) struct Scratch(pub(crate) PathBuf);
impl Scratch {
    pub(crate) fn new(name: &str) -> Self {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "pl-private-path-{name}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        #[cfg(windows)]
        {
            // A protected, owner-only folder, as a hosted runner's private
            // temporary folder is: this host's TEMP passes Modify to other
            // groups, which would make every link made here untrusted.
            let me = crate::credentials::windows_security::current_user_sid().unwrap();
            crate::credentials::windows_security::set_link_dacl_sddl(
                &path,
                &format!("D:P(A;OICI;FA;;;{me})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"),
                true,
            )
            .unwrap();
        }
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        // Neither platform's remove_dir_all follows a link inside the tree.
        let _ = fs::remove_dir_all(&self.0);
    }
}

// ---- The POSIX decision, over injected owners and modes (every OS) ----

const ME: u32 = 1000;
const OTHER: u32 = 1001;

#[test]
fn posix_links_nobody_else_can_change_are_trusted() {
    for (link, holder, mode) in [
        (0, 0, 0o755),    // /home -> /data/home; macOS /var, /tmp
        (ME, ME, 0o755),  // the user's own link in their home
        (ME, ME, 0o700),  // ... in a private folder
        (ME, 0, 0o1777),  // the user's own link in /tmp (sticky)
        (0, 0, 0o1777),   // root's link in /tmp
        (ME, ME, 0o1770), // sticky and group-writable
    ] {
        assert_eq!(
            posix_link_cause(ME, link, holder, mode),
            None,
            "{link} {holder} {mode:o}"
        );
    }
}

#[test]
fn posix_links_another_account_can_change_are_refused() {
    use PosixDistrust::*;
    for (link, holder, mode, expected) in [
        // T1: another user's link, anywhere.
        (OTHER, 0, 0o755, LinkOwner(OTHER)),
        (OTHER, ME, 0o700, LinkOwner(OTHER)),
        // T3: another user's link in a sticky folder.
        (OTHER, 0, 0o1777, LinkOwner(OTHER)),
        // The folder holding it belongs to another user, who can replace it.
        (ME, OTHER, 0o755, HolderOwner(OTHER)),
        (0, OTHER, 0o1777, HolderOwner(OTHER)),
        // T2: a folder others can write, not sticky.
        (ME, ME, 0o775, HolderWritable(Writer::Group)),
        (ME, ME, 0o757, HolderWritable(Writer::Others)),
        (0, 0, 0o777, HolderWritable(Writer::GroupAndOthers)),
    ] {
        assert_eq!(
            posix_link_cause(ME, link, holder, mode),
            Some(expected),
            "{link} {holder} {mode:o}"
        );
    }
}

#[test]
fn root_itself_trusts_only_root() {
    assert_eq!(posix_link_cause(0, 0, 0, 0o755), None);
    assert_eq!(
        posix_link_cause(0, ME, 0, 0o755),
        Some(PosixDistrust::LinkOwner(ME))
    );
}

// ---- Windows reparse tags (every OS) ----

#[test]
fn only_symlinks_and_junctions_are_links() {
    assert_eq!(reparse_kind(TAG_MOUNT_POINT), ReparseKind::Link);
    assert_eq!(reparse_kind(TAG_SYMLINK), ReparseKind::Link);
}

#[test]
fn the_sixteen_cloud_files_tags_are_placeholders() {
    // IO_REPARSE_TAG_CLOUD (0x9000001A) and _CLOUD_1 to _CLOUD_F
    // (0x9000101A ... 0x9000F01A), [MS-FSCC] 2.1.2.1.
    for n in 0..16u32 {
        assert_eq!(
            reparse_kind(0x9000_001A | (n << 12)),
            ReparseKind::Placeholder,
            "{n:x}"
        );
    }
}

#[test]
fn every_other_reparse_tag_is_refused() {
    for tag in [
        0x8000_0013, // DEDUP
        0x8000_0017, // WOF
        0x8000_0018, // WCI
        0x9000_1018, // WCI_1
        0xA000_0019, // GLOBAL_REPARSE: a name surrogate, but not a link
        0x9000_001C, // PROJFS
        0x8000_0021, // ONEDRIVE (the legacy tag, not the Cloud Files filter)
        0x8000_001B, // APPEXECLINK
        0x8000_0023, // AF_UNIX
        0x9001_001A, // a cloud-like tag with a reserved bit set
        0xA000_001A, // the cloud value with the name-surrogate bit
        0,
    ] {
        assert_eq!(reparse_kind(tag), ReparseKind::Other, "{tag:#x}");
    }
}

// ---- The message: the element, the cause and one fix ----

#[test]
fn each_refusal_names_the_element_and_one_fix() {
    let element = PathBuf::from("/srv/shared/state");
    let holder = PathBuf::from("/srv/shared");
    let target = PathBuf::from("/data/state");
    let cases = [
        (
            Cause::LinkOwner {
                owner: "the account with uid 1001".into(),
                target: target.clone(),
            },
            "fix: replace the link with a folder you own and move what it led to there.",
        ),
        (
            Cause::HolderOwner {
                holder: holder.clone(),
                owner: "the account with uid 1001".into(),
                target: target.clone(),
            },
            "fix: replace the link with a folder you own and move what it led to there.",
        ),
        (
            Cause::HolderWritable {
                holder: holder.clone(),
                writer: Writer::GroupAndOthers,
                owned: true,
                target: target.clone(),
            },
            "fix: chmod go-w '/srv/shared'.",
        ),
        (
            Cause::HolderWritable {
                holder: holder.clone(),
                writer: Writer::Group,
                owned: true,
                target: target.clone(),
            },
            "fix: chmod g-w '/srv/shared'.",
        ),
        // A writable folder this user does not own (Debian's root:staff
        // /usr/local is 2775) is never theirs to chmod.
        (
            Cause::HolderWritable {
                holder: PathBuf::from("/usr/local"),
                writer: Writer::Group,
                owned: false,
                target: target.clone(),
            },
            "fix: replace the link with a folder you own and move what it led to there.",
        ),
        (
            Cause::HolderWritable {
                holder: PathBuf::from(r"C:\shared"),
                writer: Writer::Account("Everyone".into()),
                owned: false,
                target: target.clone(),
            },
            "fix: replace the link with a folder you own and move what it led to there.",
        ),
        // A target that cannot be checked is not a permissions problem.
        (
            Cause::UncheckableTarget {
                detail: "a link target in a form this client cannot check".into(),
            },
            "fix: replace the link with a folder you own and move what it led to there.",
        ),
        (
            Cause::LinkWriter {
                writer: "Everyone".into(),
                target: target.clone(),
            },
            "fix: replace the link with a folder you own and move what it led to there.",
        ),
        (Cause::TooManyLinks, "fix: point it at the folder itself."),
        (
            Cause::NotALink { tag: 0x8000_0017 },
            "fix: keep this client's files in a plain folder",
        ),
    ];
    for (cause, fix) in cases {
        let text = Refusal {
            element: element.clone(),
            cause: Box::new(cause.clone()),
        }
        .to_string();
        assert!(text.contains(&element.display().to_string()), "{text}");
        assert!(text.contains(fix), "{text}");
        assert_eq!(text.matches("fix:").count(), 1, "{text}");
        assert!(!text.contains("invalid_state"), "{text}");
        assert!(!text.contains("chmod") || fix.contains("chmod"), "{text}");
    }
}

#[test]
fn a_quote_in_the_folder_name_is_escaped_for_the_shell() {
    let text = Refusal {
        element: PathBuf::from("/home/o'brien/link"),
        cause: Box::new(Cause::HolderWritable {
            holder: PathBuf::from("/home/o'brien"),
            writer: Writer::Others,
            owned: true,
            target: PathBuf::from("/data"),
        }),
    }
    .to_string();
    assert!(
        text.ends_with(r"fix: chmod o-w '/home/o'\''brien'."),
        "{text}"
    );
}

// ---- POSIX, on the real filesystem (Linux; macOS: not yet run) ----

#[cfg(unix)]
mod posix {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

    /// A folder only this user may write, whatever the umask (002 under
    /// Debian's user private groups makes new folders group-writable).
    fn dir(path: &Path) {
        fs::create_dir(path).unwrap();
        mode(path, 0o700);
    }
    fn mode(path: &Path, bits: u32) {
        fs::set_permissions(path, fs::Permissions::from_mode(bits)).unwrap();
    }
    fn token_at(folder: &Path) -> PathBuf {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let file = folder.join("token");
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&file)
            .unwrap()
            .write_all(b"fixture-token")
            .unwrap();
        file
    }

    #[test]
    fn a_home_behind_the_users_own_link_works_for_the_token_and_the_board() {
        let scratch = Scratch::new("home");
        let real = scratch.0.join("real-home");
        dir(&real);
        let home = scratch.0.join("home");
        symlink(&real, &home).unwrap();
        // The token file under the linked home.
        let data = home.join(".pseudolife-mcp");
        dir(&data);
        mode(&data, 0o700);
        let token = token_at(&data);
        let provider = crate::credentials::CredentialProvider::new(None, Some(token)).unwrap();
        assert_eq!(provider.snapshot().unwrap().token(), Some("fixture-token"));
        // The board's private folder and a state file in it.
        let state = data.join("agents");
        crate::board::state::private_dir(&state).unwrap();
        let file = state.join("record.json");
        drop(crate::board::state::create(&file).unwrap());
        crate::board::state::open(&file).unwrap();
        assert_eq!(check_ancestors(&file), Ok(()));
    }

    #[test]
    fn a_private_folder_that_is_itself_the_users_link_works() {
        let scratch = Scratch::new("folder-link");
        let real = scratch.0.join("big-disk-state");
        dir(&real);
        let state = scratch.0.join("state");
        symlink(&real, &state).unwrap();
        crate::board::state::private_dir(&state).unwrap();
        let file = state.join("record.json");
        drop(crate::board::state::create(&file).unwrap());
        crate::board::state::open(&file).unwrap();
        assert_eq!(crate::board::state::diagnose(&file), None);
    }

    #[test]
    fn relative_links_through_dotdot_and_chains_are_followed_and_checked() {
        let scratch = Scratch::new("chain");
        let real = scratch.0.join("real");
        dir(&scratch.0.join("a"));
        dir(&real);
        symlink("../real", scratch.0.join("a/up")).unwrap();
        symlink(scratch.0.join("a/up"), scratch.0.join("second")).unwrap();
        assert_eq!(check_ancestors(&scratch.0.join("a/up/file")), Ok(()));
        assert_eq!(check_ancestors(&scratch.0.join("second/file")), Ok(()));
    }

    #[test]
    fn a_link_in_a_folder_others_can_write_is_refused_with_its_fix() {
        let scratch = Scratch::new("writable");
        let real = scratch.0.join("real");
        let shared = scratch.0.join("shared");
        dir(&real);
        dir(&shared);
        symlink(&real, shared.join("state")).unwrap();
        mode(&shared, 0o777);
        let refusal = check_ancestors(&shared.join("state/token")).unwrap_err();
        assert_eq!(refusal.element, shared.join("state"));
        assert!(
            refusal
                .to_string()
                .contains(&format!("fix: chmod go-w '{}'", shared.display())),
            "{refusal}"
        );
        // The credential and board callers refuse it too.
        assert!(crate::board::state::private_dir(&shared.join("state/agents")).is_err());
        assert!(
            crate::board::state::diagnose(&shared.join("state/agents/x.json"))
                .is_some_and(|line| line.contains("chmod go-w"))
        );
        mode(&shared, 0o700);
    }

    #[test]
    fn the_users_link_in_a_sticky_shared_folder_is_trusted() {
        let scratch = Scratch::new("sticky");
        let real = scratch.0.join("real");
        let sticky = scratch.0.join("tmp");
        dir(&real);
        dir(&sticky);
        symlink(&real, sticky.join("link")).unwrap();
        mode(&sticky, 0o1777);
        assert_eq!(check_ancestors(&sticky.join("link/file")), Ok(()));
        mode(&sticky, 0o700);
    }

    #[test]
    fn a_trusted_link_into_an_untrusted_one_is_refused_at_the_untrusted_one() {
        // T4: the walk checks the target, not only the link.
        let scratch = Scratch::new("through");
        let real = scratch.0.join("real");
        let shared = scratch.0.join("shared");
        dir(&real);
        dir(&shared);
        symlink(&real, shared.join("inner")).unwrap();
        mode(&shared, 0o777);
        symlink(shared.join("inner"), scratch.0.join("outer")).unwrap();
        let refusal = check_ancestors(&scratch.0.join("outer/file")).unwrap_err();
        assert_eq!(refusal.element, shared.join("inner"));
        mode(&shared, 0o700);
    }

    #[test]
    fn a_loop_is_refused_as_too_many_links() {
        let scratch = Scratch::new("loop");
        symlink(scratch.0.join("b"), scratch.0.join("a")).unwrap();
        symlink(scratch.0.join("a"), scratch.0.join("b")).unwrap();
        let refusal = check_ancestors(&scratch.0.join("a/file")).unwrap_err();
        assert_eq!(*refusal.cause, Cause::TooManyLinks);
    }

    #[test]
    fn a_target_written_with_a_trailing_slash_or_dot_is_still_checked() {
        // `inner/` and `inner/.` make a plain lstat follow `inner`; the walk
        // inspects `inner` itself (Codex review of 14973a30d, P1).
        for suffix in ["/", "/."] {
            let scratch = Scratch::new("suffix");
            let real = scratch.0.join("real");
            let shared = scratch.0.join("shared");
            dir(&real);
            dir(&shared);
            symlink(&real, shared.join("inner")).unwrap();
            mode(&shared, 0o777);
            let target = format!("{}{suffix}", shared.join("inner").display());
            symlink(&target, scratch.0.join("outer")).unwrap();
            let refusal = check_ancestors(&scratch.0.join("outer/file")).unwrap_err();
            assert_eq!(refusal.element, shared.join("inner"), "{suffix}");
            // The same for a path the caller writes that way.
            let given = PathBuf::from(format!("{}{suffix}", shared.join("inner").display()));
            assert!(check_path(&given).is_err(), "{suffix}");
            mode(&shared, 0o700);
        }
    }

    #[test]
    fn nested_relative_links_are_counted_once_each() {
        // Each level links to the next relative to itself; a walk that
        // recounted every prefix's links ran past the cap with six of them
        // (Codex review of 14973a30d, P2).
        let scratch = Scratch::new("nested");
        let mut folder = scratch.0.clone();
        for level in 0..12 {
            dir(&folder.join(format!("real{level}")));
            symlink(format!("real{level}"), folder.join(format!("l{level}"))).unwrap();
            folder = folder.join(format!("l{level}"));
        }
        assert_eq!(check_ancestors(&folder.join("token")), Ok(()));
        assert_eq!(check_path(&folder), Ok(()));
    }

    #[test]
    fn a_token_file_that_is_itself_a_link_is_still_refused() {
        // T6: the leaf rule is unchanged.
        let scratch = Scratch::new("leaf");
        let token = token_at(&scratch.0);
        let alias = scratch.0.join("alias");
        symlink(&token, &alias).unwrap();
        let provider = crate::credentials::CredentialProvider::new(None, Some(alias)).unwrap();
        assert_eq!(
            provider.snapshot().unwrap_err().0,
            "credential file must be a private regular file"
        );
    }

    #[test]
    fn the_hook_command_prints_its_marker_only_when_the_folders_pass() {
        let scratch = Scratch::new("hook");
        let real = scratch.0.join("real");
        dir(&real);
        symlink(&real, scratch.0.join("home")).unwrap();
        let ok = scratch.0.join("home/token");
        assert_eq!(hook_command(vec![ok.into()]), 0);
        let shared = scratch.0.join("shared");
        dir(&shared);
        symlink(&real, shared.join("home")).unwrap();
        mode(&shared, 0o777);
        assert_eq!(hook_command(vec![shared.join("home/token").into()]), 1);
        assert_eq!(hook_command(vec!["relative/token".into()]), 2);
        assert_eq!(hook_command(vec![]), 2);
        mode(&shared, 0o700);
    }
}

// ---- Windows, on the real filesystem (the Windows dev VM, hosted CI) ----

#[cfg(windows)]
mod windows {
    use super::*;
    use crate::credentials::windows_security::{
        current_user_sid, link_holder_distrust_sddl, make_junction_for_test, set_link_dacl_sddl,
        target_path_for_test,
    };

    #[test]
    fn substitute_names_map_to_paths_this_process_can_open() {
        assert_eq!(
            target_path_for_test(r"\??\C:\Users\<user>", false),
            Some(PathBuf::from(r"C:\Users\<user>"))
        );
        assert_eq!(
            target_path_for_test(r"\??\UNC\server\share\home", false),
            Some(PathBuf::from(r"\\server\share\home"))
        );
        assert_eq!(
            target_path_for_test(r"\??\Volume{0b6d2f44-0000-0000-0000-100000000000}\", false),
            Some(PathBuf::from(
                r"\\?\Volume{0b6d2f44-0000-0000-0000-100000000000}\"
            ))
        );
        // Relative symlinks are resolved by the caller against their folder's
        // real path. Every other form is refused, not walked as written:
        // NT paths, and the \??\GLOBALROOT\Device form an ordinary user's
        // `mklink /J` can store (security review of 14973a30d, finding 1).
        assert_eq!(target_path_for_test(r"..\real", true), None);
        for name in [
            r"\Device\HarddiskVolume3\x",
            r"\??\GLOBALROOT\Device\HarddiskVolume2\pl",
            r"\??\GLOBALROOT\Device\HarddiskVolume2",
            r"\??\UNC\",
            r"\??\C:",
            r"\??\pipe\x",
            r"C:\plain",
        ] {
            assert_eq!(target_path_for_test(name, false), None, "{name}");
        }
    }

    #[test]
    fn a_relative_target_is_read_against_its_folders_real_path() {
        use crate::credentials::windows_security::lexical_for_test;
        // The folder's real path (GetFinalPathNameByHandleW) joined to the
        // link's text, `..` taken against that real path (security review of
        // 14973a30d, finding 3).
        for (joined, expected) in [
            (r"\\?\E:\real\..\x", r"\\?\E:\x"),
            (r"\\?\E:\real\.\a\..\..\x", r"\\?\E:\x"),
            (r"\\?\E:\..\x", r"\\?\E:\x"),
            (r"\\?\UNC\server\share\a\..\b", r"\\?\UNC\server\share\b"),
        ] {
            assert_eq!(
                lexical_for_test(Path::new(joined)),
                PathBuf::from(expected),
                "{joined}"
            );
        }
    }

    #[test]
    fn the_link_holder_rule_ignores_add_rights_and_refuses_delete_child() {
        let me = current_user_sid().unwrap();
        // C:\ as Windows ships it: TrustedInstaller owns it, Authenticated
        // Users may add subfolders (AD = 0x4) and inherit Modify onto what
        // they create (inherit-only).
        let root = "O:S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464D:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICI;0x1200a9;;;BU)(A;OICIIO;SDGRGWGX;;;AU)(A;;0x4;;;AU)";
        assert_eq!(link_holder_distrust_sddl(root, Some(&me)), None);
        let root = "O:SYD:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICI;0x1200a9;;;BU)(A;OICIIO;SDGRGWGX;;;AU)(A;;0x4;;;AU)";
        assert_eq!(link_holder_distrust_sddl(root, Some(&me)), None);
        // C:\Users: Users read only.
        let users = "O:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;;0x1200a9;;;BU)(A;;0x1200a9;;;WD)";
        assert_eq!(link_holder_distrust_sddl(users, Some(&me)), None);
        // Delete-child, WRITE_DAC or WRITE_OWNER for another account: refused.
        for right in ["0x40", "WD", "WO", "GA", "FA"] {
            let sddl = format!("O:{me}D:P(A;OICI;FA;;;{me})(A;;{right};;;AU)");
            assert!(
                matches!(
                    link_holder_distrust_sddl(&sddl, Some(&me)),
                    Some(crate::credentials::windows_security::MarkerDistrust::Writer { .. })
                ),
                "{right}"
            );
        }
        // Owned by another account: refused.
        assert!(matches!(
            link_holder_distrust_sddl("O:AUD:P(A;;FA;;;SY)", Some(&me)),
            Some(crate::credentials::windows_security::MarkerDistrust::Owner(
                _
            ))
        ));
    }

    #[test]
    fn a_profile_behind_the_users_own_junction_in_a_private_folder_works() {
        let scratch = Scratch::new("junction");
        let real = scratch.0.join("real-profile");
        fs::create_dir(&real).unwrap();
        let profile = scratch.0.join("profile");
        make_junction_for_test(&profile, &real).unwrap();
        assert_eq!(check_ancestors(&profile.join("token")), Ok(()));
        let state = profile.join("agents");
        crate::board::state::private_dir(&state).unwrap();
        let file = state.join("record.json");
        drop(crate::board::state::create(&file).unwrap());
        crate::board::state::open(&file).unwrap();
    }

    #[test]
    fn a_private_folder_that_is_itself_the_users_junction_works() {
        let scratch = Scratch::new("junction-folder");
        let real = scratch.0.join("real-state");
        fs::create_dir(&real).unwrap();
        let state = scratch.0.join("state");
        make_junction_for_test(&state, &real).unwrap();
        crate::board::state::private_dir(&state).unwrap();
        let file = state.join("record.json");
        drop(crate::board::state::create(&file).unwrap());
        crate::board::state::open(&file).unwrap();
    }

    #[test]
    fn a_junction_another_account_may_delete_is_refused() {
        // T5: an inherited Modify entry, as a junction made directly in C:\
        // carries, lets Authenticated Users delete or rewrite it.
        let scratch = Scratch::new("junction-writable");
        let real = scratch.0.join("real");
        fs::create_dir(&real).unwrap();
        let link = scratch.0.join("link");
        make_junction_for_test(&link, &real).unwrap();
        let me = current_user_sid().unwrap();
        set_link_dacl_sddl(&link, &format!("D:(A;;FA;;;{me})(A;;0x1301bf;;;AU)"), true).unwrap();
        let refusal = check_ancestors(&link.join("token")).unwrap_err();
        assert_eq!(refusal.element, link);
        assert!(
            matches!(*refusal.cause, Cause::LinkWriter { .. }),
            "{refusal}"
        );
    }

    #[test]
    fn a_junction_in_a_folder_where_others_may_delete_entries_is_refused() {
        let scratch = Scratch::new("junction-holder");
        let real = scratch.0.join("real");
        let shared = scratch.0.join("shared");
        fs::create_dir(&real).unwrap();
        fs::create_dir(&shared).unwrap();
        let link = shared.join("link");
        make_junction_for_test(&link, &real).unwrap();
        let me = current_user_sid().unwrap();
        // Delete-child for Authenticated Users on the folder itself only,
        // so the junction inherits nothing from it.
        set_link_dacl_sddl(
            &shared,
            &format!("D:(A;OICI;FA;;;{me})(A;OICI;FA;;;SY)(A;;0x40;;;AU)"),
            true,
        )
        .unwrap();
        let refusal = check_ancestors(&link.join("token")).unwrap_err();
        assert_eq!(refusal.element, link);
        assert!(
            matches!(
                &*refusal.cause,
                Cause::HolderWritable {
                    writer: Writer::Account(_),
                    ..
                }
            ),
            "{refusal}"
        );
    }
}
