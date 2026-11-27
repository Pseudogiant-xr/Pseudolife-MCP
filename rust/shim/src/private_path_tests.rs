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
        // Each of the 12 links counts once, by its own device and inode, on
        // top of any link above the scratch folder (macOS `/var`, a linked
        // `TMPDIR`).
        let above = links_followed(&scratch.0.join("token")).unwrap();
        assert_eq!(links_followed(&folder.join("token")), Ok(12 + above));
    }

    #[test]
    fn hard_linked_relative_symlinks_are_walked_at_each_destination() {
        // `a/l` and `a/b/l` are one symlink (a hard link: one inode, one
        // stored target `child`), but a relative target resolves against
        // each entry's own folder: `a/child` and `a/b/child` are different
        // places, and the second leads through a link in a folder others
        // may write (Codex review of 327d5d4e6, P1).
        let scratch = Scratch::new("hardlinked");
        let a = scratch.0.join("a");
        dir(&a);
        dir(&a.join("b"));
        dir(&a.join("child"));
        let shared = scratch.0.join("shared");
        dir(&shared);
        dir(&scratch.0.join("real"));
        symlink(scratch.0.join("real"), shared.join("x")).unwrap();
        symlink(shared.join("x"), a.join("b/child")).unwrap();
        symlink("child", a.join("l")).unwrap();
        fs::hard_link(a.join("l"), a.join("b/l")).unwrap();
        mode(&shared, 0o777);
        symlink(a.join("l/../b/l"), scratch.0.join("outer")).unwrap();
        let refusal = check_ancestors(&scratch.0.join("outer/file")).unwrap_err();
        assert_eq!(refusal.element, shared.join("x"), "{refusal}");
        mode(&shared, 0o700);
    }

    #[test]
    fn a_remembered_link_never_skips_the_checks_of_another_entry_for_it() {
        // `a/l` and `shared/l` are one symlink (a hard link) with one
        // absolute target, so both entries share one key. `a/l` is accepted
        // first; `shared/l` sits in a folder others may write, and the walk
        // must still refuse it there (#1644 verifier, F2).
        let scratch = Scratch::new("shared-key");
        let real = scratch.0.join("real");
        let a = scratch.0.join("a");
        let shared = scratch.0.join("shared");
        dir(&real);
        dir(&a);
        dir(&shared);
        symlink(&real, a.join("l")).unwrap();
        fs::hard_link(a.join("l"), shared.join("l")).unwrap();
        symlink(a.join("l"), scratch.0.join("outer")).unwrap();
        symlink(shared.join("l"), real.join("inner")).unwrap();
        mode(&shared, 0o777);
        let refusal = check_ancestors(&scratch.0.join("outer/inner/file")).unwrap_err();
        assert_eq!(refusal.element, shared.join("l"), "{refusal}");
        assert!(
            matches!(*refusal.cause, Cause::HolderWritable { .. }),
            "{refusal}"
        );
        mode(&shared, 0o700);
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

    /// A `~/.config` left group-writable by umask 002, holding the user's own
    /// link, and the account files that say whose group that is.
    fn group_writable_config(scratch: &Scratch) -> (PathBuf, u32, u32) {
        use std::os::unix::fs::MetadataExt;
        let real = scratch.0.join("dotfiles-claude");
        dir(&real);
        let config = scratch.0.join("config");
        dir(&config);
        symlink(&real, config.join("Claude")).unwrap();
        mode(&config, 0o775);
        let gid = fs::metadata(&config).unwrap().gid();
        let uid = rustix::process::geteuid().as_raw();
        (config.join("Claude/claude_desktop_config.json"), uid, gid)
    }

    fn accounts(
        uid: u32,
        primary: u32,
        gid: u32,
        group: &str,
        members: &str,
        extra: &str,
    ) -> Accounts {
        Accounts::from_text(
            &format!("root:x:0:0::/root:/bin/sh\nme:x:{uid}:{primary}::/home/me:/bin/sh\n{extra}"),
            &format!("root:x:0:\n{group}:x:{gid}:{members}\n"),
        )
    }

    /// Whether the scratch folder's filesystem is one the rule accepts; the
    /// positive cases skip, saying why, when the temporary folder is not.
    #[cfg(target_os = "linux")]
    fn local_scratch(scratch: &Scratch) -> bool {
        let local = on_local_filesystem(&scratch.0);
        if !local {
            eprintln!(
                "private-group case skipped: {} is not on a filesystem the rule accepts",
                scratch.0.display()
            );
        }
        local
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_users_private_group_folder_is_trusted_for_a_client_config() {
        let scratch = Scratch::new("private-group");
        let (file, uid, gid) = group_writable_config(&scratch);
        // The plain rule is unchanged: the token file would still refuse it.
        assert!(check_ancestors(&file).is_err());
        if !local_scratch(&scratch) {
            mode(&scratch.0.join("config"), 0o700);
            return;
        }
        // The user's own private group: the umask-002 case passes.
        for members in ["", "me"] {
            assert_eq!(
                check_ancestors_with(&file, accounts(uid, gid, gid, "me", members, "")),
                Ok(()),
                "members {members:?}"
            );
        }
        mode(&scratch.0.join("config"), 0o700);
    }

    /// Off Linux (macOS: its primary group is the shared `staff`) the
    /// client-config check is the plain rule, even for a private group.
    #[cfg(not(target_os = "linux"))]
    #[test]
    fn off_linux_a_private_group_folder_keeps_the_plain_rule() {
        let scratch = Scratch::new("private-group-elsewhere");
        let (file, uid, gid) = group_writable_config(&scratch);
        let refusal =
            check_ancestors_with(&file, accounts(uid, gid, gid, "me", "", "")).unwrap_err();
        assert_eq!(refusal.element, scratch.0.join("config/Claude"));
        mode(&scratch.0.join("config"), 0o700);
    }

    #[test]
    fn a_shared_or_foreign_group_folder_is_still_refused_for_a_client_config() {
        let scratch = Scratch::new("shared-group");
        let (file, uid, gid) = group_writable_config(&scratch);
        for (why, accounts) in [
            (
                "another member",
                accounts(uid, gid, gid, "me", "me,other", ""),
            ),
            (
                "not the user's primary group",
                accounts(uid, gid + 1, gid, "me", "", ""),
            ),
            (
                "named for someone else",
                accounts(uid, gid, gid, "staff", "", ""),
            ),
            (
                "another account's primary group too",
                accounts(
                    uid,
                    gid,
                    gid,
                    "me",
                    "",
                    &format!("other:x:{}:{gid}::/home/o:/bin/sh\n", uid + 1),
                ),
            ),
            ("not listed at all", Accounts::from_text("", "")),
            (
                "accounts also from LDAP (nsswitch)",
                Accounts::from_files(
                    &format!("me:x:{uid}:{gid}::/home/me:/bin/sh\n"),
                    &format!("me:x:{gid}:\n"),
                    "passwd: files sss\ngroup: files [SUCCESS=merge] sss\n",
                ),
            ),
            (
                "a compat-mode NIS reference in the group file",
                Accounts::from_text(
                    &format!("me:x:{uid}:{gid}::/home/me:/bin/sh\n"),
                    &format!("me:x:{gid}:\n+:::\n"),
                ),
            ),
            (
                "a group password anyone who knows it can use with newgrp",
                Accounts::from_text(
                    &format!("me:x:{uid}:{gid}::/home/me:/bin/sh\n"),
                    &format!("me:$6$salt$hash:{gid}:\n"),
                ),
            ),
            (
                "a second entry for the same gid",
                Accounts::from_text(
                    &format!("me:x:{uid}:{gid}::/home/me:/bin/sh\n"),
                    &format!("me:x:{gid}:\nshared:x:{gid}:other\n"),
                ),
            ),
            (
                "a group line whose gid glibc reads past a space",
                Accounts::from_text(
                    &format!("me:x:{uid}:{gid}::/home/me:/bin/sh\n"),
                    &format!("me:x:{gid}:\nshared:x: {gid}:mallory\n"),
                ),
            ),
            (
                "a passwd line whose primary gid glibc reads past a space",
                Accounts::from_text(
                    &format!(
                        "me:x:{uid}:{gid}::/home/me:/bin/sh\nother:x:{}: {gid}::/home/o:/bin/sh\n",
                        uid + 1
                    ),
                    &format!("me:x:{gid}:\n"),
                ),
            ),
            (
                "a group line glibc reads after leading whitespace",
                Accounts::from_text(
                    &format!("me:x:{uid}:{gid}::/home/me:/bin/sh\n"),
                    &format!("me:x:{gid}:\n shared:x:{gid}:mallory\n"),
                ),
            ),
            (
                "a group line with a field too many",
                Accounts::from_text(
                    &format!("me:x:{uid}:{gid}::/home/me:/bin/sh\n"),
                    &format!("me:x:{gid}::other\n"),
                ),
            ),
            (
                "a compat-mode reference in the passwd file",
                Accounts::from_text(
                    &format!(
                        "me:x:{uid}:{gid}::/home/me:/bin/sh\n+other:x:{}:{}::/home/o:/bin/sh\n",
                        uid + 1,
                        gid + 1
                    ),
                    &format!("me:x:{gid}:\n"),
                ),
            ),
            (
                "a second passwd name for the same uid",
                Accounts::from_text(
                    &format!(
                        "me:x:{uid}:{gid}::/home/me:/bin/sh\nalias:x:{uid}:{gid}::/home/me:/bin/sh\n"
                    ),
                    &format!("me:x:{gid}:\n"),
                ),
            ),
            (
                "groups also from systemd while its userdb may serve more",
                Accounts::from_files(
                    &format!("me:x:{uid}:{gid}::/home/me:/bin/sh\n"),
                    &format!("me:x:{gid}:\n"),
                    "passwd: files systemd\ngroup: files [SUCCESS=merge] systemd\n",
                ),
            ),
        ] {
            let refusal = check_ancestors_with(&file, accounts).unwrap_err();
            assert_eq!(refusal.element, scratch.0.join("config/Claude"), "{why}");
        }
        // Writable by others as well is never relaxed.
        mode(&scratch.0.join("config"), 0o777);
        assert!(check_ancestors_with(&file, accounts(uid, gid, gid, "me", "", "")).is_err());
        mode(&scratch.0.join("config"), 0o700);
    }

    #[test]
    fn only_local_account_sources_count_as_files_only() {
        for (text, files_only) in [
            // An empty file names neither database: glibc's default, unread.
            ("", false),
            ("hosts: files dns\n", false),
            ("passwd: files\n", false),
            ("passwd: files\ngroup: files\n", true),
            ("passwd: files\ngroup: files\nhosts: files dns\n", true),
            // A line with no source is glibc's default, not read here.
            ("passwd: files\ngroup:\n", false),
            ("passwd: files\ngroup: [NOTFOUND=return]\n", false),
            // systemd also serves userdb and homed records.
            ("passwd: files systemd\ngroup: files systemd\n", false),
            (
                "passwd: files\ngroup: files [SUCCESS=merge] systemd\n",
                false,
            ),
            (
                "passwd: files\ngroup: files\ninitgroups: files systemd\n",
                false,
            ),
            ("passwd: files systemd\ngroup: files\n", false),
            ("passwd: compat\ngroup: compat\nshadow: compat\n", false),
            ("# group: ldap\npasswd: files\ngroup: files\n", true),
            // glibc ends the database name at whitespace or `:`.
            ("passwd: files\ngroup files ldap\n", false),
            ("passwd files\ngroup\tfiles\n", true),
            ("passwd: files\ngroup: files\nGROUP: ldap\n", false),
            ("passwd: files\n  group : files nis\n", false),
            ("passwd: files sss\ngroup: files sss\n", false),
            ("passwd: files\ngroup: files [SUCCESS=merge] ldap\n", false),
            (
                "passwd: files\ngroup: files [NOTFOUND=return] winbind\n",
                false,
            ),
            (
                "passwd: files\ngroup: files\ninitgroups: files sss\n",
                false,
            ),
            ("passwd: files\ngroup:\tfiles nis\n", false),
            // Each with a `passwd` line, so only the brackets decide.
            ("passwd: files\ngroup: files [NOTFOUND=return]sss\n", false),
            ("passwd: files\ngroup: files [NOTFOUND=return\n", false),
            (
                "passwd: files\ngroup: files [ NOTFOUND=return ] files\n",
                true,
            ),
            (
                "passwd: files\ngroup: files [ NOTFOUND=return ] systemd\n",
                false,
            ),
        ] {
            assert_eq!(nss_files_only(text, false), files_only, "{text:?}");
        }
        // With systemd's userdb serving nothing beyond the files.
        for (text, files_only) in [
            ("passwd: files systemd\ngroup: files systemd\n", true),
            (
                "passwd: files\ngroup: files [SUCCESS=merge] systemd\n",
                true,
            ),
            (
                "passwd: files\ngroup: files\ninitgroups: files systemd\n",
                true,
            ),
            ("passwd: files systemd\ngroup: files systemd sss\n", false),
            ("passwd: files\ngroup: systemd\n", false),
            ("passwd: files\ngroup: files systemd ldap\n", false),
        ] {
            assert_eq!(nss_files_only(text, true), files_only, "{text:?}");
        }
    }

    /// With a POSIX ACL the mode's group bits are only its mask, so a named
    /// entry could let another account write: the exemption does not apply.
    #[cfg(target_os = "linux")]
    #[test]
    fn a_private_group_folder_with_an_access_acl_is_still_refused() {
        let scratch = Scratch::new("private-group-acl");
        let (file, uid, gid) = group_writable_config(&scratch);
        let config = scratch.0.join("config");
        let mine = accounts(uid, gid, gid, "me", "", "");
        if !local_scratch(&scratch) {
            mode(&config, 0o700);
            return;
        }
        assert_eq!(check_ancestors_with(&file, mine.clone()), Ok(()));
        let added = std::process::Command::new("setfacl")
            .args(["-m", "u:nobody:rwx"])
            .arg(&config)
            .status();
        if !added.is_ok_and(|status| status.success()) {
            mode(&config, 0o700);
            if std::env::var_os("CI").is_some() {
                panic!("setfacl is needed for this test");
            }
            eprintln!("ACL case unverified locally: setfacl unavailable or unsupported here");
            return;
        }
        assert!(check_ancestors_with(&file, mine).is_err());
        mode(&config, 0o700);
    }

    /// Only a filesystem on the allowlist counts: procfs and sysfs (and a
    /// path statfs cannot read) do not.
    #[cfg(target_os = "linux")]
    #[test]
    fn only_a_local_filesystem_on_the_allowlist_counts() {
        for path in ["/proc", "/sys", "/nonexistent-pl-private-path"] {
            assert!(!on_local_filesystem(Path::new(path)), "{path}");
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn only_a_folder_with_no_acl_on_a_filesystem_that_supports_them_passes() {
        use rustix::io::Errno;
        for (posix, nfs4, allowed) in [
            (Err(Errno::NODATA), Err(Errno::NODATA), true),
            (Ok(0), Err(Errno::NOTSUP), true),
            (Err(Errno::NODATA), Ok(0), true),
            // A POSIX access ACL: the group bits are only its mask.
            (Ok(44), Err(Errno::NODATA), false),
            // No POSIX ACL support: unknown.
            (Err(Errno::NOTSUP), Err(Errno::NOTSUP), false),
            // An NFSv4 ACL.
            (Err(Errno::NODATA), Ok(80), false),
            (Err(Errno::NOTSUP), Ok(80), false),
            // Any other error.
            (Err(Errno::NODATA), Err(Errno::ACCESS), false),
            (Err(Errno::ACCESS), Err(Errno::NODATA), false),
        ] {
            assert_eq!(acls_allow(posix, nfs4), allowed, "{posix:?} {nfs4:?}");
        }
    }

    #[test]
    fn the_system_accounts_are_read_from_the_root_and_fail_closed() {
        let scratch = Scratch::new("accounts-root");
        let root = &scratch.0;
        let write = |file: &str, text: &str| {
            let path = root.join(file);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, text).unwrap();
        };
        write(
            "etc/passwd",
            "root:x:0:0::/root:/bin/sh\nme:x:1000:1000::/home/me:/bin/sh\n",
        );
        write("etc/group", "root:x:0:\nme:x:1000:\n");
        // No nsswitch.conf at all: glibc's built-in default, not read here.
        assert!(
            !Accounts::under(root).private_group(1000, 1000),
            "no nsswitch.conf"
        );
        write("etc/nsswitch.conf", "passwd: files\ngroup: files\n");
        assert!(
            Accounts::under(root).private_group(1000, 1000),
            "files only"
        );
        assert!(
            !Accounts::under(root).private_group(1000, 1001),
            "another gid"
        );
        // openSUSE's /usr/etc copy is read too.
        write(
            "usr/etc/nsswitch.conf",
            "passwd: files\ngroup: files ldap\n",
        );
        assert!(
            !Accounts::under(root).private_group(1000, 1000),
            "/usr/etc ldap"
        );
        write("usr/etc/nsswitch.conf", "passwd: files\ngroup: files\n");
        assert!(
            Accounts::under(root).private_group(1000, 1000),
            "/usr/etc files"
        );
        fs::remove_file(root.join("etc/nsswitch.conf")).unwrap();
        assert!(
            Accounts::under(root).private_group(1000, 1000),
            "/usr/etc alone"
        );
        write("etc/nsswitch.conf", "passwd: files\ngroup: files\n");
        // Any userdb drop-in record: members the group file does not show.
        for (dir, record) in [
            ("etc/userdb", "other.user"),
            ("run/userdb", "me.group"),
            ("usr/lib/userdb", "other:me.membership"),
        ] {
            write(&format!("{dir}/{record}"), "{}");
            assert!(
                !Accounts::under(root).private_group(1000, 1000),
                "{dir}/{record}"
            );
            fs::remove_dir_all(root.join(dir)).unwrap();
        }
        assert!(
            Accounts::under(root).private_group(1000, 1000),
            "drop-ins removed"
        );
    }

    #[test]
    fn stock_systemd_counts_only_while_its_userdb_serves_nothing_else() {
        let scratch = Scratch::new("accounts-systemd");
        let root = &scratch.0;
        let write = |file: &str, text: &str| {
            let path = root.join(file);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, text).unwrap();
        };
        let trusted = || Accounts::under(root).private_group(1000, 1000);
        write(
            "etc/passwd",
            "root:x:0:0::/root:/bin/sh\nme:x:1000:1000::/home/me:/bin/sh\n",
        );
        write("etc/group", "root:x:0:\nme:x:1000:\n");
        // Debian's and Ubuntu's stock lines.
        write(
            "etc/nsswitch.conf",
            "passwd: files systemd\ngroup: files systemd\nshadow: files systemd\n",
        );
        // No /run/systemd/userdb: nss-systemd has no service to ask.
        assert!(trusted(), "no userdb folder");
        let services = root.join("run/systemd/userdb");
        fs::create_dir_all(&services).unwrap();
        assert!(trusted(), "an empty userdb folder");
        for service in [
            "io.systemd.DynamicUser",
            "io.systemd.Multiplexer",
            "io.systemd.NameServiceSwitch",
            "io.systemd.DropIn",
        ] {
            fs::write(services.join(service), "").unwrap();
        }
        assert!(trusted(), "only the standard services");
        for other in [
            "io.systemd.Home",
            "io.systemd.Machine",
            "org.example.Directory",
            ".hidden",
        ] {
            fs::write(services.join(other), "").unwrap();
            assert!(!trusted(), "{other}");
            fs::remove_file(services.join(other)).unwrap();
        }
        // A plain file where the folder should be cannot be listed.
        let kept = root.join("run/systemd/userdb-kept");
        fs::rename(&services, &kept).unwrap();
        fs::write(&services, "").unwrap();
        assert!(!trusted(), "a plain file in place of the userdb folder");
        fs::remove_file(&services).unwrap();
        fs::rename(&kept, &services).unwrap();
        assert!(trusted(), "the folder back");
        // Any drop-in record, a membership above all.
        for (dir, record) in [
            ("etc/userdb", "other:me.membership"),
            ("run/userdb", "me.group"),
            ("usr/local/lib/userdb", "other.user"),
        ] {
            write(&format!("{dir}/{record}"), "{}");
            assert!(!trusted(), "{dir}/{record}");
            fs::remove_dir_all(root.join(dir)).unwrap();
        }
        assert!(trusted(), "drop-ins removed");
        // A userdb folder that cannot be listed (root can list anything).
        if rustix::process::geteuid().as_raw() != 0 {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&services, fs::Permissions::from_mode(0o000)).unwrap();
            let unreadable = trusted();
            fs::set_permissions(&services, fs::Permissions::from_mode(0o700)).unwrap();
            assert!(!unreadable, "an unreadable userdb folder");
        }
        // With files alone the services do not matter.
        write("etc/nsswitch.conf", "passwd: files\ngroup: files\n");
        fs::write(services.join("io.systemd.Home"), "").unwrap();
        assert!(trusted(), "files only");
    }

    #[test]
    fn a_gid_systemd_hands_out_or_synthesizes_is_never_private() {
        // DynamicUser=, nobody and 65535, containers, nsresourced, foreign.
        for id in [
            61184_u32,
            61200,
            65519,
            65534,
            65535,
            0x0008_0000,
            0x6FFF_FFFF,
            0x7000_0000,
            0x7FFD_FFFF,
            0x7FFE_0000,
            0x7FFE_FFFF,
        ] {
            let accounts = Accounts::from_text(
                &format!("me:x:{id}:{id}::/home/me:/bin/sh\n"),
                &format!("me:x:{id}:\n"),
            );
            assert!(!accounts.private_group(id, id), "{id:#x}");
        }
        // The ids either side are ordinary.
        for id in [61183_u32, 65520, 65533, 65536, 0x0007_FFFF, 0x7FFF_0000] {
            let accounts = Accounts::from_text(
                &format!("me:x:{id}:{id}::/home/me:/bin/sh\n"),
                &format!("me:x:{id}:\n"),
            );
            assert!(accounts.private_group(id, id), "{id}");
        }
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
            Some(PathBuf::from(r"\\?\C:\Users\<user>"))
        );
        assert_eq!(
            target_path_for_test(r"\??\UNC\server\share\home", false),
            Some(PathBuf::from(r"\\?\UNC\server\share\home"))
        );
        // Verbatim, so Win32 cannot normalise the name into another object:
        // `state.` stays `state.`, never the plain folder `state` (Codex
        // review of 5645e025a).
        assert_eq!(
            target_path_for_test(r"\??\C:\data\state.", false),
            Some(PathBuf::from(r"\\?\C:\data\state."))
        );
        assert_eq!(
            target_path_for_test(r"\??\C:\data\state ", false),
            Some(PathBuf::from(r"\\?\C:\data\state "))
        );
        assert_eq!(
            target_path_for_test(r"\??\UNC\server\share\home.", false),
            Some(PathBuf::from(r"\\?\UNC\server\share\home."))
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
        use crate::credentials::windows_security::resolve_relative_for_test;
        // The folder's real path (GetFinalPathNameByHandleW) with the link's
        // text applied part by part, as Windows applies it (security review
        // of 14973a30d, finding 3; verifier of df15d6875, N-c).
        for (real, target, expected) in [
            (r"\\?\E:\real", r"..\x", Some(r"\\?\E:\x")),
            (r"\\?\E:\real", r".\a\..\..\x", Some(r"\\?\E:\x")),
            (r"\\?\E:\", r"..\x", Some(r"\\?\E:\x")),
            (
                r"\\?\UNC\server\share\a",
                r"..\b",
                Some(r"\\?\UNC\server\share\b"),
            ),
            (r"\\?\E:\real\deep", r"\x", Some(r"\\?\E:\x")),
            (r"\\?\E:\real", r"x.", Some(r"\\?\E:\real\x.")),
            // `/` is no separator in a stored target, nor a valid NTFS name.
            (r"\\?\E:\real", r"../x", None),
            (r"\\?\E:\real", r"a\b/c", None),
        ] {
            assert_eq!(
                resolve_relative_for_test(Path::new(real), target),
                expected.map(PathBuf::from),
                "{real} + {target}"
            );
        }
    }

    #[test]
    fn an_absolute_target_with_a_dot_or_slash_component_is_refused() {
        // The walk would fold `.`/`..` or split at `/` where the link follows
        // its name literally (verifier of df15d6875, N-c).
        for name in [
            r"\??\C:\a\..\b",
            r"\??\C:\a\.\b",
            r"\??\C:\a\b/c",
            r"\??\UNC\server\share\..\other",
            r"\??\Volume{0b6d2f44-0000-0000-0000-100000000000}\..\x",
        ] {
            assert_eq!(target_path_for_test(name, false), None, "{name}");
        }
        // A trailing dot in a name is a name, not a `.` component.
        assert!(target_path_for_test(r"\??\C:\a\b.", false).is_some());
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
        for right in ["0x40", "SD", "WD", "WO", "GA", "FA"] {
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
    fn a_target_name_with_an_unpaired_surrogate_is_kept_as_it_is() {
        use crate::credentials::windows_security::target_path_wide_for_test;
        use std::os::windows::ffi::OsStringExt;
        // A valid Windows name may hold an unpaired surrogate, which no
        // string conversion keeps: the target is parsed on its UTF-16 units
        // and carried through unchanged (Codex review of de08bccdd, P2).
        let mut name: Vec<u16> = r"\??\C:\data\x".encode_utf16().collect();
        name.push(0xD800);
        let mut expected: Vec<u16> = r"\\?\C:\data\x".encode_utf16().collect();
        expected.push(0xD800);
        assert_eq!(
            target_path_wide_for_test(&name, false),
            Some(PathBuf::from(std::ffi::OsString::from_wide(&expected)))
        );
    }

    #[test]
    fn a_link_met_by_differently_cased_paths_is_counted_once() {
        // Windows folders are case-insensitive: each junction's target names
        // the junctions before it in upper case, so the walk meets every one
        // by two spellings. Links are told apart by filesystem identity, so
        // each still counts once (Codex review of de08bccdd, P2).
        let scratch = Scratch::new("cased-junctions");
        let mut folder = scratch.0.clone();
        let mut made = 0;
        for level in 0..40 {
            let real = folder.join(format!("r{level}"));
            if fs::create_dir(&real).is_err() {
                break;
            }
            let link = folder.join(format!("l{level}"));
            let shouted = PathBuf::from(real.display().to_string().to_uppercase());
            make_junction_for_test(&link, &shouted).unwrap();
            folder = link;
            made += 1;
        }
        assert!(made >= 7, "only {made} nested junctions could be made");
        // On top of any link above the scratch folder (a junctioned TEMP).
        let above = links_followed(&scratch.0.join("token")).unwrap();
        assert_eq!(links_followed(&folder.join("token")), Ok(made + above));
    }

    #[test]
    fn nested_junctions_reached_by_both_spellings_are_counted_once_each() {
        // Each junction's target runs through the junctions before it, so the
        // walk meets each one by the path as given (a DOS path) and again in
        // a target (a verbatim path). Counted once each, 21 stay well under
        // the 40-link cap (Codex review of 73649236d, P2).
        let scratch = Scratch::new("nested-junctions");
        let mut folder = scratch.0.clone();
        let mut made = 0;
        for level in 0..40 {
            let real = folder.join(format!("r{level}"));
            // Windows stops resolving such a chain itself at some depth
            // (ERROR_CANT_RESOLVE_FILENAME); the test stops there too.
            if fs::create_dir(&real).is_err() {
                break;
            }
            let link = folder.join(format!("l{level}"));
            make_junction_for_test(&link, &real).unwrap();
            folder = link;
            made += 1;
        }
        eprintln!("nested junctions Windows resolves: {made}");
        assert!(made >= 7, "only {made} nested junctions could be made");
        // The deepest folder Windows still resolves, behind `made` links.
        assert_eq!(
            check_ancestors(&folder.join("token")),
            Ok(()),
            "{made} links"
        );
        // On top of any link above the scratch folder (a junctioned TEMP).
        let above = links_followed(&scratch.0.join("token")).unwrap();
        assert_eq!(links_followed(&folder.join("token")), Ok(made + above));
    }

    #[test]
    fn a_junction_to_a_trailing_dot_name_is_checked_at_that_name() {
        // `state.` and `state` are different objects, but Win32 normalises a
        // plain `...\state.` to `...\state`. The trusted outer junction leads
        // to `state.`, a junction another account may rewrite; the walk must
        // inspect `state.` (verbatim), not the plain folder `state` beside it
        // (Codex review of 5645e025a, P1).
        let scratch = Scratch::new("trailing-dot");
        let verbatim = |path: &Path| PathBuf::from(format!(r"\\?\{}", path.display()));
        let plain = scratch.0.join("state");
        fs::create_dir(&plain).unwrap();
        let real = scratch.0.join("real");
        fs::create_dir(&real).unwrap();
        let dotted = verbatim(&scratch.0).join("state.");
        make_junction_for_test(&dotted, &real).unwrap();
        let me = current_user_sid().unwrap();
        set_link_dacl_sddl(
            &dotted,
            &format!("D:(A;;FA;;;{me})(A;;0x1301bf;;;AU)"),
            true,
        )
        .unwrap();
        let outer = scratch.0.join("outer");
        make_junction_for_test(&outer, &scratch.0.join("state.")).unwrap();
        let refusal = check_ancestors(&outer.join("token")).unwrap_err();
        assert!(
            refusal.element.to_string_lossy().ends_with(r"\state."),
            "{refusal}"
        );
        assert!(
            matches!(*refusal.cause, Cause::LinkWriter { .. }),
            "{refusal}"
        );
        fs::remove_dir(&dotted).unwrap(); // the junction itself, not its target
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
