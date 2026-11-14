use super::*;

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("native-config-test-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&path).unwrap();
        Self(path)
    }
    fn recovery(&self) -> PathBuf {
        fs::read_dir(&self.0)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with(".client-native-backup-")
            })
            .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn successful_edit_retains_only_the_returned_displaced_backup() {
    let f = Fixture::new();
    let path = f.0.join("client.json");
    crate::board::state::atomic_create(&path, b"before").unwrap();
    let backup = write_config(&path, b"before", b"after").unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"after");
    assert_eq!(fs::read(&backup).unwrap(), b"before");
    assert_eq!(backup.file_name().unwrap(), "previous");
    assert_eq!(fs::read_dir(backup.parent().unwrap()).unwrap().count(), 2);
    assert!(backup.parent().unwrap().join(COMPLETION).is_file());
}

#[test]
fn successful_creation_retains_the_returned_observed_backup() {
    let f = Fixture::new();
    let path = f.0.join("client.json");
    let backup = write_config(&path, b"", b"after").unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"after");
    assert_eq!(fs::read(&backup).unwrap(), b"");
    assert_eq!(backup.file_name().unwrap(), "observed");
    assert_eq!(fs::read_dir(backup.parent().unwrap()).unwrap().count(), 2);
    assert!(backup.parent().unwrap().join(COMPLETION).is_file());
}

#[test]
fn four_successful_writes_keep_three_backups_per_exact_target() {
    let f = Fixture::new();
    let other = f.0.join("other.json");
    let other_backup = write_config(&other, b"", b"unrelated").unwrap();
    let path = f.0.join("client.json");
    crate::board::state::atomic_create(&path, b"0").unwrap();
    let mut backups = Vec::new();
    for index in 0..4 {
        backups.push(
            write_config(
                &path,
                index.to_string().as_bytes(),
                (index + 1).to_string().as_bytes(),
            )
            .unwrap(),
        );
    }
    assert!(!backups[0].parent().unwrap().exists());
    for (index, backup) in backups.iter().enumerate().skip(1) {
        assert_eq!(fs::read(backup).unwrap(), index.to_string().as_bytes());
    }
    assert!(other_backup.is_file());
    let target = fs::canonicalize(&path).unwrap();
    assert_eq!(
        fs::read_dir(&f.0)
            .unwrap()
            .filter_map(|entry| { completed_backup(&entry.ok()?.path(), &target) })
            .count(),
        3
    );
}

#[test]
fn a_conflicted_recovery_directory_survives_later_successful_writes() {
    let f = Fixture::new();
    let path = f.0.join("client.json");
    crate::board::state::atomic_create(&path, b"before").unwrap();
    assert!(
        write_config_with(&path, b"before", b"after", || {
            fs::write(&path, b"concurrent edit").unwrap();
        })
        .is_err()
    );
    let conflict = f.recovery();
    for index in 0..4 {
        let before = fs::read(&path).unwrap();
        write_config(&path, &before, format!("successful-{index}").as_bytes()).unwrap();
    }
    assert!(!conflict.join(COMPLETION).exists());
    assert_eq!(fs::read(conflict.join("observed")).unwrap(), b"before");
    assert_eq!(
        fs::read(conflict.join("previous")).unwrap(),
        b"concurrent edit"
    );
}

#[test]
fn changed_or_unrecognized_completed_recovery_evidence_is_retained() {
    let f = Fixture::new();
    let path = f.0.join("client.json");
    let changed = write_config(&path, b"", b"one").unwrap();
    fs::write(&changed, b"changed backup").unwrap();
    let unknown = write_config(&path, b"one", b"two").unwrap();
    fs::write(
        unknown.parent().unwrap().join(COMPLETION),
        b"unrecognized completion",
    )
    .unwrap();
    for index in 0..4 {
        let before = fs::read(&path).unwrap();
        write_config(&path, &before, format!("successful-{index}").as_bytes()).unwrap();
    }
    assert_eq!(fs::read(changed).unwrap(), b"changed backup");
    assert_eq!(fs::read(unknown).unwrap(), b"one");
}

#[test]
fn collision_preserves_the_concurrent_file_and_all_recovery_evidence() {
    let f = Fixture::new();
    let path = f.0.join("client.json");
    let error = write_config_with(&path, b"", b"after", || {
        crate::board::state::atomic_create(&path, b"concurrent creation").unwrap();
    })
    .unwrap_err();
    let recovery = f.recovery();
    assert!(error.contains("recovery files:"), "{error}");
    assert_eq!(fs::read(&path).unwrap(), b"concurrent creation");
    assert_eq!(fs::read(recovery.join("observed")).unwrap(), b"");
    assert_eq!(fs::read(recovery.join("pending")).unwrap(), b"after");
}

#[test]
fn replacement_conflict_keeps_observed_and_actual_displaced_contents() {
    let f = Fixture::new();
    let path = f.0.join("client.json");
    crate::board::state::atomic_create(&path, b"before").unwrap();
    let error = write_config_with(&path, b"before", b"after", || {
        fs::write(&path, b"concurrent edit").unwrap();
    })
    .unwrap_err();
    let recovery = f.recovery();
    assert!(
        error.contains("actual displaced file was preserved"),
        "{error}"
    );
    assert_eq!(fs::read(&path).unwrap(), b"after");
    assert_eq!(fs::read(recovery.join("observed")).unwrap(), b"before");
    assert_eq!(
        fs::read(recovery.join("previous")).unwrap(),
        b"concurrent edit"
    );
}

/// Issue #1545: umask 002 (a private group per user, Debian and Ubuntu)
/// makes client files 0664; group or other write is never a reason to refuse,
/// the target keeps its mode, and the backup is 0600.
#[cfg(unix)]
mod modes {
    use super::*;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    fn edit_with(file_mode: u32, parent_mode: Option<u32>) {
        let f = Fixture::new();
        let parent = match parent_mode {
            Some(mode) => {
                let dir = f.0.join("config-dir");
                fs::create_dir(&dir).unwrap();
                fs::set_permissions(&dir, fs::Permissions::from_mode(mode)).unwrap();
                dir
            }
            None => f.0.clone(),
        };
        let path = parent.join("client.json");
        fs::write(&path, b"before").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(file_mode)).unwrap();
        let backup = write_config(&path, b"before", b"after")
            .unwrap_or_else(|e| panic!("mode {file_mode:o} parent {parent_mode:?}: {e}"));
        assert_eq!(fs::read(&path).unwrap(), b"after");
        assert_eq!(
            fs::metadata(&path).unwrap().mode() & 0o7777,
            file_mode,
            "the target keeps its mode"
        );
        assert_eq!(fs::read(&backup).unwrap(), b"before");
        assert_eq!(fs::metadata(&backup).unwrap().mode() & 0o777, 0o600);
        assert_eq!(
            fs::metadata(backup.parent().unwrap()).unwrap().mode() & 0o777,
            0o700
        );
        assert!(backup.parent().unwrap().join(COMPLETION).is_file());
        if let Some(mode) = parent_mode {
            assert_eq!(
                fs::metadata(&parent).unwrap().mode() & 0o777,
                mode,
                "parent untouched"
            );
        }
    }

    #[test]
    fn group_writable_and_private_configs_are_edited_keeping_their_mode() {
        for mode in [0o664, 0o660, 0o644, 0o600, 0o666] {
            edit_with(mode, None);
        }
    }

    #[test]
    fn a_group_writable_parent_directory_is_not_a_reason_to_refuse() {
        for mode in [0o664, 0o644, 0o600] {
            edit_with(mode, Some(0o775));
        }
    }

    #[test]
    fn another_accounts_file_is_refused_naming_the_path_and_the_fix() {
        let f = Fixture::new();
        // As root, a fixture file given to nobody (65534); as anyone else, a
        // file root owns. The owner check refuses before anything is created.
        let path = if rustix::process::getuid().as_raw() == 0 {
            let path = f.0.join("client.json");
            fs::write(&path, b"before").unwrap();
            std::os::unix::fs::chown(&path, Some(65534), Some(65534)).unwrap();
            path
        } else {
            PathBuf::from("/etc/passwd")
        };
        let content = fs::read(&path).unwrap();
        let error = write_config(&path, &content, b"after").unwrap_err();
        assert!(
            error.contains("is owned by another account") && error.contains("sudo chown"),
            "{error}"
        );
        assert_eq!(fs::read(&path).unwrap(), content);
        assert!(
            !fs::read_dir(path.parent().unwrap()).unwrap().any(|e| e
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".client-native-backup-")),
            "no recovery files were created"
        );
    }

    #[test]
    fn a_hard_link_is_refused_without_changing_either_name() {
        let f = Fixture::new();
        let path = f.0.join("client.json");
        fs::write(&path, b"before").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o664)).unwrap();
        let link = f.0.join("dotfiles-copy.json");
        fs::hard_link(&path, &link).unwrap();
        let error = write_config(&path, b"before", b"after").unwrap_err();
        assert!(error.contains("another hard link"), "{error}");
        assert_eq!(fs::read(&path).unwrap(), b"before");
        assert_eq!(fs::metadata(&link).unwrap().mode() & 0o777, 0o664);
    }

    #[test]
    fn the_replacement_keeps_the_original_group() {
        let f = Fixture::new();
        let path = f.0.join("client.json");
        fs::write(&path, b"before").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o664)).unwrap();
        let gid = fs::metadata(&path).unwrap().gid();
        write_config(&path, b"before", b"after").unwrap();
        let meta = fs::metadata(&path).unwrap();
        assert_eq!((meta.gid(), meta.mode() & 0o777), (gid, 0o664));
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
mod windows {
    use super::*;
    use std::{
        os::windows::{fs::OpenOptionsExt, io::AsRawHandle},
        ptr,
    };
    use windows_sys::Win32::{
        Foundation::{HANDLE, LocalFree},
        Security::{
            self,
            Authorization::{
                ConvertStringSecurityDescriptorToSecurityDescriptorW, SE_FILE_OBJECT,
                SetSecurityInfo,
            },
        },
        Storage::FileSystem::{
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_GENERIC_READ, WRITE_DAC,
            WRITE_OWNER,
        },
    };

    struct Descriptor(*mut std::ffi::c_void);
    impl Drop for Descriptor {
        fn drop(&mut self) {
            // SAFETY: this guard owns one LocalAlloc security descriptor.
            unsafe { LocalFree(self.0) };
        }
    }
    fn opened(path: &Path, directory: bool) -> fs::File {
        fs::OpenOptions::new()
            .read(true)
            .access_mode(FILE_GENERIC_READ | WRITE_DAC | WRITE_OWNER)
            .custom_flags(
                FILE_FLAG_OPEN_REPARSE_POINT
                    | if directory {
                        FILE_FLAG_BACKUP_SEMANTICS
                    } else {
                        0
                    },
            )
            .open(path)
            .unwrap()
    }
    fn set_acl(file: &fs::File, sddl: &str) {
        // SAFETY: fixture SDDL and its descriptor remain live through the
        // handle-based setter; no real user configuration is accessed.
        unsafe {
            let text: Vec<_> = sddl.encode_utf16().chain([0]).collect();
            let mut descriptor = ptr::null_mut();
            assert_ne!(
                ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    text.as_ptr(),
                    1,
                    &mut descriptor,
                    ptr::null_mut()
                ),
                0
            );
            let _descriptor = Descriptor(descriptor);
            let mut present = 0;
            let mut defaulted = 0;
            let mut dacl = ptr::null_mut();
            assert_ne!(
                Security::GetSecurityDescriptorDacl(
                    descriptor,
                    &mut present,
                    &mut dacl,
                    &mut defaulted
                ),
                0
            );
            assert!(present != 0 && !dacl.is_null());
            assert_eq!(
                SetSecurityInfo(
                    file.as_raw_handle() as HANDLE,
                    SE_FILE_OBJECT,
                    Security::DACL_SECURITY_INFORMATION
                        | Security::PROTECTED_DACL_SECURITY_INFORMATION,
                    ptr::null_mut(),
                    ptr::null_mut(),
                    dacl,
                    ptr::null()
                ),
                0
            );
        }
    }

    #[test]
    fn locked_pending_refuses_before_publication_and_retry_has_one_link() {
        let f = Fixture::new();
        let target = f.0.join("client.json");
        let pending = f.0.join("pending.json");
        let previous = f.0.join("previous.json");
        crate::board::state::atomic_create(&target, b"before").unwrap();
        crate::board::state::atomic_create(&pending, b"after").unwrap();
        let held = fs::OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(&pending)
            .unwrap();
        assert!(replace_preserving(&target, &pending, &previous).is_err());
        assert_eq!(fs::read(&target).unwrap(), b"before");
        assert!(!previous.exists());
        assert_eq!(fs::read_dir(&f.0).unwrap().count(), 2);
        drop(held);
        assert_eq!(fs::read(&pending).unwrap(), b"after");
        replace_preserving(&target, &pending, &previous).unwrap();
        assert!(!pending.exists());
        assert_eq!(read_optional(&target).unwrap().unwrap(), b"after");
        windows_config_security::validate(&fs::File::open(&target).unwrap(), false).unwrap();
        assert_eq!(fs::read(&previous).unwrap(), b"before");
        assert_eq!(fs::read_dir(&f.0).unwrap().count(), 2);
    }

    #[test]
    fn ordinary_client_created_file_keeps_its_default_owner() {
        // Isolate from host-specific Temp ACLs that may grant other accounts
        // write access; the client file itself still uses fs::write defaults.
        let f = Fixture::new();
        let path = f.0.join("client.json");
        // Match client applications: inherit the normal parent ACL and token
        // default owner, including Administrators for an elevated process.
        fs::write(&path, b"before").unwrap();
        let backup = write_config(&path, b"before", b"after").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"after");
        assert_eq!(fs::read(&backup).unwrap(), b"before");
    }

    #[test]
    fn ordinary_inherited_system_and_administrator_acl_is_accepted() {
        let f = Fixture::new();
        let directory = opened(&f.0, true);
        set_acl(
            &directory,
            "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
        );
        let path = f.0.join("client.json");
        fs::write(&path, b"before").unwrap();
        let file = opened(&path, false);
        // Leave the client-created file's token-default ownership untouched.
        assert!(crate::credentials::windows_security::validate(&file).is_err());
        drop(file);
        let backup = write_config(&path, b"before", b"after").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"after");
        assert_eq!(fs::read(&backup).unwrap(), b"before");
    }

    #[test]
    fn a_file_other_accounts_may_write_is_edited_keeping_its_acl_with_a_private_backup() {
        // Issue #1545: the client already trusts this file as it is; the
        // edit keeps its ACL (never loosened, never changed) and the backup
        // of the displaced file is this user's alone.
        let f = Fixture::new();
        let path = f.0.join("client.json");
        crate::board::state::atomic_create(&path, b"before").unwrap();
        set_acl(&opened(&path, false), "D:P(A;;FA;;;OW)(A;;FW;;;WD)");
        let backup = write_config(&path, b"before", b"after").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"after");
        assert_eq!(fs::read(&backup).unwrap(), b"before");
        // The target still grants Everyone write, exactly as before.
        assert!(windows_config_security::validate(&fs::File::open(&path).unwrap(), false).is_err());
        windows_config_security::validate(&fs::File::open(&path).unwrap(), true).unwrap();
        // The backup is private, so its completion is recorded and pruning works.
        windows_config_security::validate(&fs::File::open(&backup).unwrap(), false).unwrap();
        assert!(backup.parent().unwrap().join(COMPLETION).is_file());
    }

    #[test]
    fn a_store_desktop_config_with_an_inherited_package_capability_ace_is_edited() {
        // The Store (MSIX) Claude Desktop folder grants a package capability
        // SID (S-1-15-3-*) full control by inheritance, so the config Desktop
        // creates there carries it; that file is the client's own.
        let f = Fixture::new();
        let directory = opened(&f.0, true);
        set_acl(
            &directory,
            "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-15-3-1024-1065365936-1281604716-3511738428-1654721687-432734479-3232135806-4053264122-3456934681)",
        );
        let path = f.0.join("claude_desktop_config.json");
        fs::write(&path, b"before").unwrap();
        // The inherited capability ACE is really there: the strict check sees it.
        let strict =
            || windows_config_security::validate(&fs::File::open(&path).unwrap(), false).is_err();
        assert!(strict());
        let backup = write_config(&path, b"before", b"after").unwrap();
        assert!(strict(), "the target keeps its ACL");
        assert_eq!(fs::read(&path).unwrap(), b"after");
        assert_eq!(fs::read(&backup).unwrap(), b"before");
        windows_config_security::validate(&fs::File::open(&backup).unwrap(), false).unwrap();
    }

    #[test]
    fn another_principals_read_only_acl_does_not_prevent_editing() {
        let f = Fixture::new();
        let path = f.0.join("client.json");
        crate::board::state::atomic_create(&path, b"before").unwrap();
        set_acl(&opened(&path, false), "D:P(A;;FA;;;OW)(A;;FR;;;WD)");
        let backup = write_config(&path, b"before", b"after").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"after");
        assert_eq!(fs::read(&backup).unwrap(), b"before");
    }

    #[test]
    fn a_hard_link_is_rejected_without_changing_either_name() {
        let f = Fixture::new();
        let path = f.0.join("client.json");
        crate::board::state::atomic_create(&path, b"before").unwrap();
        let link = f.0.join("other.json");
        fs::hard_link(&path, &link).unwrap();
        assert!(write_config(&path, b"before", b"after").is_err());
        assert_eq!(fs::read(&path).unwrap(), b"before");
        assert_eq!(fs::read(&link).unwrap(), b"before");
    }
}
