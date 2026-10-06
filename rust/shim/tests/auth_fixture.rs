#![allow(dead_code)]
use std::{
    fs, io,
    path::{Path, PathBuf},
};

pub struct DisposableHome(pub PathBuf);
impl Default for DisposableHome {
    fn default() -> Self {
        Self::new()
    }
}
impl DisposableHome {
    pub fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "pseudolife-auth-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
    pub fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    pub fn private_token(&self, name: &str, value: &[u8]) -> PathBuf {
        let path = self.path(name);
        fs::write(&path, value).unwrap();
        protect(&path).unwrap();
        path
    }
}
impl Drop for DisposableHome {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
#[cfg(unix)]
pub fn protect(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
}

#[cfg(windows)]
#[allow(unsafe_code)]
pub fn protect(path: &Path) -> io::Result<()> {
    use std::{mem, os::windows::ffi::OsStrExt, ptr};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, LocalFree},
        Security::{
            self,
            Authorization::{
                ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW,
            },
        },
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };
    // SAFETY: test-only private file; each Win32 output allocation remains live
    // until its final use and is released even when a subsequent call fails.
    unsafe {
        let mut handle = ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), Security::TOKEN_QUERY, &mut handle) == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut size = 0;
        Security::GetTokenInformation(handle, Security::TokenUser, ptr::null_mut(), 0, &mut size);
        let mut buffer = vec![0usize; (size as usize).div_ceil(mem::size_of::<usize>())];
        let success = Security::GetTokenInformation(
            handle,
            Security::TokenUser,
            buffer.as_mut_ptr().cast(),
            size,
            &mut size,
        );
        CloseHandle(handle);
        if success == 0 {
            return Err(io::Error::last_os_error());
        }
        let sid = (*buffer.as_ptr().cast::<Security::TOKEN_USER>()).User.Sid;
        let mut sid_text = ptr::null_mut();
        if ConvertSidToStringSidW(sid, &mut sid_text) == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut len = 0;
        while *sid_text.add(len) != 0 {
            len += 1;
        }
        let sid = String::from_utf16_lossy(std::slice::from_raw_parts(sid_text, len));
        LocalFree(sid_text.cast());
        let sddl: Vec<u16> = format!("O:{sid}D:P(A;;FA;;;OW)")
            .encode_utf16()
            .chain([0])
            .collect();
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
        let path: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        let success = Security::SetFileSecurityW(
            path.as_ptr(),
            Security::OWNER_SECURITY_INFORMATION
                | Security::DACL_SECURITY_INFORMATION
                | Security::PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor,
        );
        LocalFree(descriptor);
        if success == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}
