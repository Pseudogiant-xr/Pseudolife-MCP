// Included only inside credentials::windows_security's native unit-test scope.
// All fixtures are fresh empty handles; no pathname security setter is used.
use super::*;
use std::{fs, io::Write, os::windows::fs::OpenOptionsExt};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ALL_ACCESS, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_GENERIC_READ,
    FILE_GENERIC_WRITE, FILE_SHARE_READ, FILE_SHARE_WRITE, WRITE_DAC, WRITE_OWNER,
};

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "pseudolife-auth-private-state-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn open_file(path: &Path, create: bool, setter: bool) -> File {
    let access = if setter {
        FILE_GENERIC_READ | FILE_GENERIC_WRITE | WRITE_DAC | WRITE_OWNER
    } else {
        FILE_GENERIC_READ
    };
    OpenOptions::new()
        .read(true)
        .write(setter)
        .create_new(create)
        .access_mode(access)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .open(path)
        .unwrap()
}
fn open_dir(path: &Path, setter: bool) -> File {
    let access = FILE_GENERIC_READ | if setter { WRITE_DAC | WRITE_OWNER } else { 0 };
    OpenOptions::new()
        .read(true)
        .access_mode(access)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .open(path)
        .unwrap()
}
fn opened_identity(file: &File) -> [u32; 3] {
    // SAFETY: live borrowed handle, correctly sized initialized output.
    unsafe {
        let mut info: BY_HANDLE_FILE_INFORMATION = mem::zeroed();
        assert_ne!(
            GetFileInformationByHandle(file.as_raw_handle() as HANDLE, &mut info),
            0
        );
        [
            info.dwVolumeSerialNumber,
            info.nFileIndexHigh,
            info.nFileIndexLow,
        ]
    }
}
fn acl_snapshot(file: &File, expect_private: bool) -> (Vec<u8>, u16) {
    // SAFETY: GetSecurityInfo produces a valid descriptor. Its returned DACL,
    // ACEs and SID remain within that live allocation until Descriptor drops.
    // Only an owned copy of the ACL bytes leaves this function.
    unsafe {
        let mut descriptor = ptr::null_mut();
        let mut dacl = ptr::null_mut();
        assert_eq!(
            GetSecurityInfo(
                file.as_raw_handle() as HANDLE,
                SE_FILE_OBJECT,
                Security::DACL_SECURITY_INFORMATION,
                ptr::null_mut(),
                ptr::null_mut(),
                &mut dacl,
                ptr::null_mut(),
                &mut descriptor
            ),
            0
        );
        let _descriptor = Descriptor(descriptor);
        assert!(!descriptor.is_null() && !dacl.is_null());
        let mut control = 0;
        let mut revision = 0;
        assert_ne!(
            Security::GetSecurityDescriptorControl(descriptor, &mut control, &mut revision),
            0
        );
        if expect_private {
            assert_ne!(control & Security::SE_DACL_PROTECTED, 0);
            assert_eq!((*dacl).AceCount, 1);
            let mut ace = ptr::null_mut();
            assert_ne!(Security::GetAce(dacl, 0, &mut ace), 0);
            assert!(!ace.is_null());
            let ace = &*ace.cast::<Security::ACCESS_ALLOWED_ACE>();
            assert_eq!(ace.Header.AceType, 0);
            assert_eq!(ace.Header.AceFlags, 0); // no inherited/inheritable access
            assert_eq!(ace.Mask, FILE_ALL_ACCESS);
            let sid = (&ace.SidStart as *const u32).cast_mut().cast();
            assert_ne!(
                Security::IsWellKnownSid(sid, Security::WinCreatorOwnerRightsSid),
                0
            );
        }
        (
            std::slice::from_raw_parts(dacl.cast::<u8>(), (*dacl).AclSize as usize).to_vec(),
            control,
        )
    }
}
fn seed_dacl(file: &File, sddl: &str) {
    // SAFETY: fixture-only literal descriptor, live borrowed handle, initialized
    // outputs. DACL remains descriptor-owned throughout SetSecurityInfo.
    unsafe {
        let sddl: Vec<u16> = sddl.encode_utf16().chain([0]).collect();
        let mut descriptor = ptr::null_mut();
        assert_ne!(
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
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
                Security::DACL_SECURITY_INFORMATION | Security::PROTECTED_DACL_SECURITY_INFORMATION,
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
fn test_windows_writer_assigns_token_user_before_protecting_file() {
    // Native equivalent of pinned test_credentials.py's writer regression.
    let home = Home::new();
    let path = home.path("state");
    let mut file = open_file(&path, true, true);
    let identity = opened_identity(&file);
    assert_eq!(file.metadata().unwrap().len(), 0);
    make_private(&file).unwrap();
    // validate compares the actual owner with TokenUser, not TokenOwner/group.
    assert!(validate(&file).is_ok());
    acl_snapshot(&file, true);
    assert_eq!(opened_identity(&file), identity);
    file.write_all(b"disposable-state").unwrap();
    file.sync_all().unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"disposable-state");
}

#[test]
fn test_windows_private_directory_has_protected_owner_only_noninheriting_acl() {
    let home = Home::new();
    let path = home.path("directory");
    fs::create_dir(&path).unwrap();
    let directory = open_dir(&path, true);
    let identity = opened_identity(&directory);
    make_private(&directory).unwrap();
    assert!(validate(&directory).is_ok());
    acl_snapshot(&directory, true);
    assert_eq!(opened_identity(&directory), identity);
    let child = open_file(&path.join("state"), true, true);
    assert_eq!(child.metadata().unwrap().len(), 0);
    make_private(&child).unwrap();
    assert!(validate(&child).is_ok());
}

#[test]
fn test_windows_private_handle_replaces_foreign_allow_without_merging() {
    let home = Home::new();
    let file = open_file(&home.path("state"), true, true);
    seed_dacl(&file, "D:P(A;;FA;;;OW)(A;;FR;;;WD)");
    assert!(validate(&file).is_err());
    make_private(&file).unwrap();
    assert!(validate(&file).is_ok());
    acl_snapshot(&file, true);
}

#[test]
fn test_windows_private_handle_requires_setter_rights_on_files_and_directories() {
    let home = Home::new();
    let file_path = home.path("state");
    let directory_path = home.path("directory");
    drop(open_file(&file_path, true, true));
    fs::create_dir(&directory_path).unwrap();
    for file in [
        open_file(&file_path, false, false),
        open_dir(&directory_path, false),
    ] {
        let before = acl_snapshot(&file, false);
        let error = make_private(&file).unwrap_err();
        assert_eq!(
            error.to_string(),
            "credential file permissions could not be protected"
        );
        // Compare internally so failure never prints a SID/descriptor.
        assert!(acl_snapshot(&file, false) == before);
    }
    assert_eq!(fs::metadata(&file_path).unwrap().len(), 0);
}

#[test]
fn test_windows_private_handle_rejects_hardlinks_before_acl_mutation() {
    let home = Home::new();
    let path = home.path("state");
    let file = open_file(&path, true, true);
    fs::hard_link(&path, home.path("alias")).unwrap();
    let before = acl_snapshot(&file, false);
    assert!(make_private(&file).is_err());
    assert!(acl_snapshot(&file, false) == before);
    assert_eq!(file.metadata().unwrap().len(), 0);
}

#[test]
fn test_windows_private_handle_preserves_no_delete_sharing_identity_fence() {
    let home = Home::new();
    let path = home.path("state");
    let moved = home.path("moved");
    let file = open_file(&path, true, true);
    let identity = opened_identity(&file);
    make_private(&file).unwrap();
    assert!(fs::rename(&path, &moved).is_err());
    assert!(fs::remove_file(&path).is_err());
    assert_eq!(opened_identity(&file), identity);
    drop(file);
    fs::rename(&path, &moved).unwrap();
    assert_eq!(opened_identity(&open_file(&moved, false, false)), identity);
}

fn disposable_command(program: &str, home: &Home) -> std::process::Command {
    let executable = crate::lifecycle::find_executable(program).unwrap();
    let mut command = std::process::Command::new(executable);
    command
        .env_clear()
        .current_dir(&home.0)
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0);
    for name in ["SystemRoot", "PATH", "TEMP", "TMP"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
}

#[test]
fn test_windows_git_directory_identity_matches_python_stat_without_acl_mutation() {
    let home = Home::new();
    let common = home.path("git-common");
    let git = disposable_command("git", &home)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", home.path("absent-global-config"))
        .args(["-c", "init.templateDir=", "init", "--quiet", "--bare"])
        .arg(&common)
        .output()
        .unwrap();
    assert!(git.status.success());
    let directory = open_dir(&common, false);
    let before = acl_snapshot(&directory, false);
    let actual = directory_identity(&common).unwrap();
    let python = disposable_command("python", &home)
        .args(["-I", "-S", "-c", "import json, os, sys; s = os.stat(sys.argv[1]); print(json.dumps([s.st_dev, s.st_ino]))"])
        .arg(&common).output().unwrap();
    assert!(python.status.success());
    let expected: Vec<u64> = serde_json::from_slice(&python.stdout).unwrap();
    assert_eq!([actual.0, actual.1], expected.as_slice());
    assert!(acl_snapshot(&directory, false) == before);
}

#[test]
fn test_windows_directory_identity_rejects_regular_and_missing_paths() {
    let home = Home::new();
    let path = home.path("regular");
    drop(open_file(&path, true, true));
    for path in [&path, &home.path("missing")] {
        let error = directory_identity(path).unwrap_err();
        assert_eq!(error.to_string(), "repository directory is unavailable");
    }
}
