#![allow(dead_code)]
#[cfg(windows)]
use pseudolife_stdio::credentials;
mod auth_fixture;

#[cfg(windows)]
mod windows {
    use super::{auth_fixture::DisposableHome, credentials::CredentialProvider};
    use std::{
        fs, io,
        path::Path,
        time::{Duration, Instant},
    };

    #[allow(unsafe_code)]
    fn dacl(path: &Path, sddl: &str) -> io::Result<()> {
        use std::{os::windows::ffi::OsStrExt, ptr};
        use windows_sys::Win32::{
            Foundation::LocalFree,
            Security::{self, Authorization::ConvertStringSecurityDescriptorToSecurityDescriptorW},
        };
        let sddl: Vec<u16> = sddl.encode_utf16().chain([0]).collect();
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        // SAFETY: live NUL-terminated input buffers and a descriptor allocated
        // by Win32, freed immediately after SetFileSecurityW consumes it.
        unsafe {
            let mut descriptor = ptr::null_mut();
            if ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                1,
                &mut descriptor,
                ptr::null_mut(),
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
            let success = Security::SetFileSecurityW(
                wide.as_ptr(),
                Security::DACL_SECURITY_INFORMATION | Security::PROTECTED_DACL_SECURITY_INFORMATION,
                descriptor,
            );
            LocalFree(descriptor);
            if success == 0 {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(())
    }

    #[test]
    fn test_windows_acl_rejects_foreign_allow_aces_and_unfamiliar_aces() {
        let home = DisposableHome::new();
        for sddl in [
            "D:P(A;;FA;;;OW)(A;;FR;;;WD)",
            "D:P(A;;FA;;;OW)(A;;FR;;;BA)",
            "D:P(A;;FA;;;OW)(OA;;FR;;;WD)",
        ] {
            let path = home.private_token("token", b"fixture-secret");
            dacl(&path, sddl).unwrap();
            let error = CredentialProvider::new(None, Some(path))
                .unwrap()
                .snapshot()
                .unwrap_err();
            assert_eq!(error.0, "credential file must be owner-only");
        }
    }

    #[test]
    fn test_windows_acl_permits_owner_rights_and_ignores_deny_aces() {
        let home = DisposableHome::new();
        let path = home.private_token("token", b"fixture-secret");
        // A deny on execute does not prevent reading; Python ignores deny ACEs.
        dacl(&path, "D:P(D;;0x20;;;WD)(A;;FA;;;OW)").unwrap();
        assert!(
            CredentialProvider::new(None, Some(path))
                .unwrap()
                .snapshot()
                .is_ok()
        );
    }

    #[test]
    fn test_windows_reparse_ancestor_is_rejected() {
        let home = DisposableHome::new();
        fs::create_dir(home.path("real")).unwrap();
        home.private_token("real/token", b"fixture-secret");
        let redirect = home.path("redirect");
        let output = std::process::Command::new("cmd.exe")
            .args(["/d", "/c", "mklink", "/J"])
            .arg(&redirect)
            .arg(home.path("real"))
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "disposable junction creation failed"
        );
        assert_eq!(
            CredentialProvider::new(None, Some(redirect.join("token")))
                .unwrap()
                .snapshot()
                .unwrap_err()
                .0,
            "credential path must not contain redirects"
        );
    }

    #[test]
    fn test_windows_transient_permission_error_is_retried() {
        use std::os::windows::fs::OpenOptionsExt;
        let home = DisposableHome::new();
        let path = home.private_token("token", b"fixture-secret");
        let lock = fs::OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(&path)
            .unwrap();
        let worker = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(40));
            drop(lock);
        });
        let start = Instant::now();
        assert!(
            CredentialProvider::new(None, Some(path))
                .unwrap()
                .snapshot()
                .is_ok()
        );
        assert!(start.elapsed() >= Duration::from_millis(10));
        worker.join().unwrap();
    }

    #[test]
    fn test_windows_permission_error_retries_are_bounded_and_sanitized() {
        use std::os::windows::fs::OpenOptionsExt;
        let home = DisposableHome::new();
        let path = home.private_token("token", b"fixture-secret");
        let _lock = fs::OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(&path)
            .unwrap();
        let start = Instant::now();
        let error = CredentialProvider::new(None, Some(path))
            .unwrap()
            .snapshot()
            .unwrap_err();
        assert_eq!(error.0, "configured credential file is unavailable");
        assert!(!error.to_string().contains("fixture-secret"));
        assert!(start.elapsed() >= Duration::from_millis(80));
        assert!(start.elapsed() < Duration::from_secs(3));
    }
}
