//! Native Windows owner-only state: open, inspect and protect credential and
//! coordination files and directories (PORTING.md, "Unsafe code and
//! dependencies"). It lives in `rust/shared/` so every crate that writes
//! private state uses the same code; the shim includes it as
//! `credentials::windows_security`. The including module provides
//! `CredentialError(&'static str)` and `reject_ancestor_redirects`.
use super::{CredentialError, reject_ancestor_redirects};
use std::{
    ffi::c_void,
    mem,
    os::windows::{ffi::OsStrExt, io::AsRawHandle},
    ptr,
};
use std::{
    fs::{File, OpenOptions},
    io,
    path::Path,
};
use windows_sys::Win32::{
    Foundation::{CloseHandle, HANDLE, LocalFree},
    Security::{
        self,
        Authorization::{
            ConvertStringSecurityDescriptorToSecurityDescriptorW, GetSecurityInfo, SE_FILE_OBJECT,
            SetSecurityInfo,
        },
    },
    Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, GetFileAttributesW, GetFileInformationByHandle,
        INVALID_FILE_ATTRIBUTES,
    },
    System::Threading::{GetCurrentProcess, OpenProcessToken},
};

pub(crate) fn inspect_redirect(path: &Path) -> io::Result<bool> {
    let path: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    if path[..path.len() - 1].contains(&0) {
        return Err(io::Error::from(io::ErrorKind::InvalidInput));
    }
    // SAFETY: live NUL-terminated path; GetFileAttributesW returns attributes
    // of the reparse point itself, without opening or following its target.
    let attributes = unsafe { GetFileAttributesW(path.as_ptr()) };
    if attributes == INVALID_FILE_ATTRIBUTES {
        return Err(io::Error::last_os_error());
    }
    Ok(attributes & 0x400 != 0)
}

// pub(crate) for the shim's private-state tests, which build one.
pub(crate) struct Descriptor(pub(crate) *mut c_void);
impl Drop for Descriptor {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
}
struct Token(HANDLE);
impl Drop for Token {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

/// Read public filesystem identity without inspecting or changing its ACL.
/// The caller supplies a resolved Git common directory; redirects fail closed.
pub(crate) fn directory_identity(path: &Path) -> Result<(u64, u64), CredentialError> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    let invalid = CredentialError("repository directory is unavailable");
    reject_ancestor_redirects(path).map_err(|_| invalid)?;
    let file = OpenOptions::new()
        .read(true)
        .access_mode(FILE_READ_ATTRIBUTES)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .open(path)
        .map_err(|_| invalid)?;
    // SAFETY: the borrowed directory handle stays live while Win32 fills
    // the correctly sized initialized structure. Attributes are checked on
    // this same handle before its public volume/index identity is returned.
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as HANDLE, &mut info) } == 0
        || info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0
        || info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || (info.nFileIndexHigh == 0 && info.nFileIndexLow == 0)
    {
        return Err(invalid);
    }
    Ok((
        info.dwVolumeSerialNumber as u64,
        ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    ))
}

/// Secure a newly created, still-empty file or directory through its handle.
/// Caller must hold WRITE_OWNER | WRITE_DAC, exclude delete sharing, reject
/// ancestor redirects, and retain leaf identity fences before writing data.
/// This cannot authenticate contents of a previously existing state record.
pub(crate) fn make_private(file: &File) -> Result<(), CredentialError> {
    let invalid = CredentialError("credential file permissions could not be protected");
    // SAFETY: File retains the borrowed handle for this call. Win32 output
    // storage is correctly sized and initialized. TokenUser is queried into
    // usize-aligned storage; its SID stays live until SetSecurityInfo returns.
    // The converted self-relative descriptor owns the DACL's storage under
    // Descriptor; a missing/null DACL is rejected, never passed to the setter.
    // No pointer or handle ownership escapes; no pathname is reopened.
    unsafe {
        let handle = file.as_raw_handle() as HANDLE;
        let mut info: BY_HANDLE_FILE_INFORMATION = mem::zeroed();
        if GetFileInformationByHandle(handle, &mut info) == 0
            || info.nNumberOfLinks != 1
            || info.dwFileAttributes & 0x400 != 0
        {
            return Err(invalid);
        }
        let owner_error = CredentialError("credential file owner could not be determined");
        let mut token_handle = ptr::null_mut();
        if OpenProcessToken(
            GetCurrentProcess(),
            Security::TOKEN_QUERY,
            &mut token_handle,
        ) == 0
        {
            return Err(owner_error);
        }
        let _token = Token(token_handle);
        let mut needed = 0;
        Security::GetTokenInformation(
            token_handle,
            Security::TokenUser,
            ptr::null_mut(),
            0,
            &mut needed,
        );
        if needed < mem::size_of::<Security::TOKEN_USER>() as u32 {
            return Err(owner_error);
        }
        let mut buffer = vec![0usize; (needed as usize).div_ceil(mem::size_of::<usize>())];
        if Security::GetTokenInformation(
            token_handle,
            Security::TokenUser,
            buffer.as_mut_ptr().cast(),
            needed,
            &mut needed,
        ) == 0
        {
            return Err(owner_error);
        }
        let owner = (*(buffer.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid;
        if owner.is_null() || Security::IsValidSid(owner) == 0 {
            return Err(owner_error);
        }
        // OWNER RIGHTS refers only to the owner we explicitly set below,
        // including when an elevated token's default owner is a group.
        let sddl: Vec<u16> = "D:P(A;;FA;;;OW)".encode_utf16().chain([0]).collect();
        let mut descriptor = ptr::null_mut();
        let converted = ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut descriptor,
            ptr::null_mut(),
        );
        let _descriptor = Descriptor(descriptor);
        if converted == 0 || descriptor.is_null() {
            return Err(invalid);
        }
        let mut present = 0;
        let mut defaulted = 0;
        let mut dacl = ptr::null_mut();
        if Security::GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl, &mut defaulted)
            == 0
            || present == 0
            || dacl.is_null()
        {
            return Err(invalid);
        }
        if SetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            Security::OWNER_SECURITY_INFORMATION
                | Security::DACL_SECURITY_INFORMATION
                | Security::PROTECTED_DACL_SECURITY_INFORMATION,
            owner,
            ptr::null_mut(),
            dacl,
            ptr::null(),
        ) != 0
        {
            return Err(invalid);
        }
        Ok(())
    }
}

pub(crate) fn validate(file: &File) -> Result<Vec<u128>, CredentialError> {
    // SAFETY: file owns a valid, live OS handle for this entire operation.
    // Every output structure has its exact Win32 size and initialized storage.
    // GetSecurityInfo allocations remain live under Descriptor until all ACE
    // and SID reads finish; token storage is usize-aligned and lives through
    // the comparison. No pointer escapes this function.
    unsafe {
        let handle = file.as_raw_handle() as HANDLE;
        let mut info: BY_HANDLE_FILE_INFORMATION = mem::zeroed();
        if GetFileInformationByHandle(handle, &mut info) == 0 {
            return Err(CredentialError("configured credential file is unavailable"));
        }
        if info.nNumberOfLinks != 1 || info.dwFileAttributes & 0x400 != 0 {
            return Err(CredentialError(
                "credential file must be a private regular file",
            ));
        }
        let mut owner = ptr::null_mut();
        let mut dacl = ptr::null_mut();
        let mut descriptor = ptr::null_mut();
        let result = GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            Security::OWNER_SECURITY_INFORMATION | Security::DACL_SECURITY_INFORMATION,
            &mut owner,
            ptr::null_mut(),
            &mut dacl,
            ptr::null_mut(),
            &mut descriptor,
        );
        let _descriptor = Descriptor(descriptor);
        let invalid = CredentialError("credential file must be owner-only");
        if result != 0 || descriptor.is_null() || owner.is_null() || dacl.is_null() {
            return Err(invalid);
        }
        let mut token_handle = ptr::null_mut();
        if OpenProcessToken(
            GetCurrentProcess(),
            Security::TOKEN_QUERY,
            &mut token_handle,
        ) == 0
        {
            return Err(invalid);
        }
        let _token = Token(token_handle);
        let mut needed = 0;
        Security::GetTokenInformation(
            token_handle,
            Security::TokenUser,
            ptr::null_mut(),
            0,
            &mut needed,
        );
        if needed < mem::size_of::<Security::TOKEN_USER>() as u32 {
            return Err(invalid);
        }
        let mut buffer = vec![0usize; (needed as usize).div_ceil(mem::size_of::<usize>())];
        if Security::GetTokenInformation(
            token_handle,
            Security::TokenUser,
            buffer.as_mut_ptr().cast(),
            needed,
            &mut needed,
        ) == 0
        {
            return Err(invalid);
        }
        let current_owner = (*(buffer.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid;
        if Security::EqualSid(owner, current_owner) == 0 {
            return Err(invalid);
        }
        let mut control = 0;
        let mut revision = 0;
        if Security::GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) == 0
            || control & Security::SE_DACL_PROTECTED == 0
        {
            return Err(invalid);
        }
        let acl_start = dacl as usize;
        let acl_end = acl_start + (*dacl).AclSize as usize;
        let mut owner_allowed = false;
        for index in 0..(*dacl).AceCount as u32 {
            let mut ace = ptr::null_mut();
            if Security::GetAce(dacl, index, &mut ace) == 0 || ace.is_null() {
                return Err(invalid);
            }
            let start = ace as usize;
            if start < acl_start + mem::size_of::<Security::ACL>()
                || start + mem::size_of::<Security::ACE_HEADER>() > acl_end
            {
                return Err(invalid);
            }
            let header = &*ace.cast::<Security::ACE_HEADER>();
            if start + header.AceSize as usize > acl_end {
                return Err(invalid);
            }
            if header.AceType == 1 {
                continue;
            }
            if header.AceType != 0 || header.AceSize < 16 {
                return Err(invalid);
            }
            let sid = ace.cast::<u8>().add(8).cast::<c_void>();
            // A SID has an 8-byte header followed by four bytes per subauthority.
            let sid_size = 8 + (*ace.cast::<u8>().add(9) as usize) * 4;
            if 8 + sid_size > header.AceSize as usize || Security::IsValidSid(sid) == 0 {
                return Err(invalid);
            }
            if Security::EqualSid(owner, sid) == 0
                && Security::IsWellKnownSid(sid, Security::WinCreatorOwnerRightsSid) == 0
            {
                return Err(invalid);
            }
            owner_allowed = true;
        }
        if !owner_allowed {
            return Err(invalid);
        }
        let combine = |high: u32, low: u32| ((high as u128) << 32) | low as u128;
        Ok(vec![
            info.dwVolumeSerialNumber as u128,
            combine(info.nFileIndexHigh, info.nFileIndexLow),
            combine(info.nFileSizeHigh, info.nFileSizeLow),
            combine(
                info.ftLastWriteTime.dwHighDateTime,
                info.ftLastWriteTime.dwLowDateTime,
            ),
            combine(
                info.ftCreationTime.dwHighDateTime,
                info.ftCreationTime.dwLowDateTime,
            ),
        ])
    }
}
