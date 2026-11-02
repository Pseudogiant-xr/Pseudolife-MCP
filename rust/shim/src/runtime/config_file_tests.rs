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
    fn broadly_writable_acl_is_rejected_without_creating_recovery_files() {
        let f = Fixture::new();
        let path = f.0.join("client.json");
        crate::board::state::atomic_create(&path, b"before").unwrap();
        set_acl(&opened(&path, false), "D:P(A;;FA;;;OW)(A;;FW;;;WD)");
        assert!(write_config(&path, b"before", b"after").is_err());
        assert_eq!(fs::read(&path).unwrap(), b"before");
        assert_eq!(fs::read_dir(&f.0).unwrap().count(), 1);
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
