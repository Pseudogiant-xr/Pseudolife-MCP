//! Native Windows owner-only state: open, inspect and protect credential and
//! coordination files and directories (PORTING.md, "Unsafe code and
//! dependencies"). It lives in `rust/shared/` so every crate that writes
//! private state uses the same code; the shim includes it as
//! `credentials::windows_security`. The including module provides
//! `CredentialError(&'static str)`.
use super::CredentialError;
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

/// Refuses a path with a redirect (a symlink or junction) in any ancestor,
/// outermost first, and fails closed when an existing ancestor cannot be
/// inspected; an ancestor that does not exist yet passes.
pub(crate) fn reject_ancestor_redirects(path: &Path) -> Result<(), CredentialError> {
    let ancestors: Vec<_> = path
        .parent()
        .into_iter()
        .flat_map(Path::ancestors)
        .collect();
    for ancestor in ancestors.into_iter().rev() {
        match inspect_redirect(ancestor) {
            Ok(true) => {
                return Err(CredentialError(
                    "credential path must not contain redirects",
                ));
            }
            Ok(_) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(_) => {
                return Err(CredentialError(
                    "credential path ancestors cannot be inspected",
                ));
            }
        }
    }
    Ok(())
}

pub(crate) struct Descriptor(*mut c_void);
impl Descriptor {
    /// For the shim's private-state tests, which read descriptors back.
    ///
    /// # Safety
    /// `descriptor` must be a security descriptor the system allocated with
    /// `LocalAlloc` (as `GetSecurityInfo` returns it) that nothing else
    /// frees: dropping the guard frees it exactly once.
    #[cfg(test)]
    pub(crate) unsafe fn from_local_alloc(descriptor: *mut c_void) -> Self {
        Self(descriptor)
    }
}
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

/// Why a marker file the plugin hooks share with this process may not be
/// trusted (see [`validate_marker`]).
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum MarkerDistrust {
    /// Owned by a principal other than this user, SYSTEM or Administrators.
    Owner(String),
    /// This principal, not this user, SYSTEM or Administrators, can write it.
    Writer(String),
    /// The file has no DACL, so every principal can write it.
    NoDacl,
}

/// Rights that let a principal change a marker's contents or its
/// protection, delete it, or backdate it: deleting `.wake` or `.wake-armed`
/// between reads suppresses a wake (DELETE on the file allows that whatever
/// its folder grants), and a backdated marker is swept as a day old.
const MARKER_WRITE_RIGHTS: u32 = 0x0002 // FILE_WRITE_DATA
    | 0x0004 // FILE_APPEND_DATA
    | 0x0100 // FILE_WRITE_ATTRIBUTES
    | 0x0001_0000 // DELETE
    | 0x0004_0000 // WRITE_DAC
    | 0x0008_0000 // WRITE_OWNER
    | 0x1000_0000 // GENERIC_ALL
    | 0x4000_0000; // GENERIC_WRITE

/// The current token's user SID, in usize-aligned storage that keeps the SID
/// pointer valid for as long as the buffer lives.
unsafe fn token_user() -> Option<Vec<usize>> {
    unsafe {
        let mut token_handle = ptr::null_mut();
        if OpenProcessToken(
            GetCurrentProcess(),
            Security::TOKEN_QUERY,
            &mut token_handle,
        ) == 0
        {
            return None;
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
            return None;
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
            return None;
        }
        let sid = (*(buffer.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid;
        (!sid.is_null() && Security::IsValidSid(sid) != 0).then_some(buffer)
    }
}

/// This process's account as people read it (`DOMAIN\name`), for a fix line
/// that must name the account rather than a shell variable.
pub(crate) fn current_user_name() -> Option<String> {
    // SAFETY: the token buffer owns the SID for the duration of the lookup.
    unsafe {
        let user = token_user()?;
        let sid = (*(user.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid;
        Some(sid_name(sid))
    }
}

/// A SID as people read it: `DOMAIN\name` when it resolves, else `S-1-...`.
unsafe fn sid_name(sid: *mut c_void) -> String {
    unsafe {
        let (mut name, mut domain) = ([0u16; 256], [0u16; 256]);
        let (mut name_len, mut domain_len, mut kind) = (256u32, 256u32, 0);
        if Security::LookupAccountSidW(
            ptr::null(),
            sid,
            name.as_mut_ptr(),
            &mut name_len,
            domain.as_mut_ptr(),
            &mut domain_len,
            &mut kind,
        ) != 0
        {
            let name = String::from_utf16_lossy(&name[..name_len as usize]);
            let domain = String::from_utf16_lossy(&domain[..domain_len as usize]);
            return if domain.is_empty() {
                name
            } else {
                format!("{domain}\\{name}")
            };
        }
        let mut text = ptr::null_mut();
        if windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW(sid, &mut text) == 0
            || text.is_null()
        {
            return "an unreadable principal".into();
        }
        let _text = Descriptor(text.cast());
        let length = (0..).take_while(|&i| *text.add(i) != 0).count();
        String::from_utf16_lossy(std::slice::from_raw_parts(text, length))
    }
}

/// This user, SYSTEM, Administrators or OWNER RIGHTS (the owner, which is
/// checked to be one of these first): principals that can already write any
/// file this user can, so a marker they own or write forges nothing.
unsafe fn trusted_marker_principal(sid: *mut c_void, user: *mut c_void) -> bool {
    unsafe {
        Security::EqualSid(sid, user) != 0
            || Security::IsWellKnownSid(sid, Security::WinLocalSystemSid) != 0
            || Security::IsWellKnownSid(sid, Security::WinBuiltinAdministratorsSid) != 0
            || Security::IsWellKnownSid(sid, Security::WinCreatorOwnerRightsSid) != 0
    }
}

/// The marker rule over an owner and DACL (see [`validate_marker`]); `None`
/// trusts them.
///
/// # Safety
/// `owner` and `user` must be valid SIDs and `dacl` null or a valid ACL, all
/// live for the call.
pub(crate) unsafe fn marker_distrust(
    owner: *mut c_void,
    dacl: *const Security::ACL,
    user: *mut c_void,
) -> Option<MarkerDistrust> {
    unsafe {
        if !trusted_marker_principal(owner, user)
            || Security::IsWellKnownSid(owner, Security::WinCreatorOwnerRightsSid) != 0
        {
            return Some(MarkerDistrust::Owner(sid_name(owner)));
        }
        if dacl.is_null() {
            return Some(MarkerDistrust::NoDacl);
        }
        let unreadable = || {
            Some(MarkerDistrust::Writer(
                "an access rule that cannot be read".into(),
            ))
        };
        let acl_start = dacl as usize;
        let acl_end = acl_start + (*dacl).AclSize as usize;
        for index in 0..(*dacl).AceCount as u32 {
            let mut ace = ptr::null_mut();
            if Security::GetAce(dacl, index, &mut ace) == 0 || ace.is_null() {
                return unreadable();
            }
            let start = ace as usize;
            if start < acl_start + mem::size_of::<Security::ACL>()
                || start + mem::size_of::<Security::ACE_HEADER>() > acl_end
            {
                return unreadable();
            }
            let header = &*ace.cast::<Security::ACE_HEADER>();
            if start + header.AceSize as usize > acl_end {
                return unreadable();
            }
            // An inherit-only entry grants nothing on this file; a deny
            // entry takes rights away.
            if header.AceFlags as u32 & Security::INHERIT_ONLY_ACE != 0 || header.AceType == 1 {
                continue;
            }
            // Any other allow form (callback, object) is not read here, so
            // it fails closed rather than being guessed at.
            if header.AceType != 0 || header.AceSize < 16 {
                return unreadable();
            }
            let mask = *ace.cast::<u8>().add(4).cast::<u32>();
            if mask & MARKER_WRITE_RIGHTS == 0 {
                continue;
            }
            let sid = ace.cast::<u8>().add(8).cast::<c_void>();
            let sid_size = 8 + (*ace.cast::<u8>().add(9) as usize) * 4;
            if 8 + sid_size > header.AceSize as usize || Security::IsValidSid(sid) == 0 {
                return unreadable();
            }
            if !trusted_marker_principal(sid, user) {
                return Some(MarkerDistrust::Writer(sid_name(sid)));
            }
        }
        None
    }
}

/// Check a marker the plugin hooks write and this process reads (`.wake`,
/// `.wake-armed`, `.seen`, `.reprint`). Hooks write them through Git Bash or
/// PowerShell, whose files carry inherited, unprotected DACLs, so the
/// owner-only rule of [`validate`] does not fit. What it guards still holds:
/// a marker another user owns or can write could forge or suppress a board
/// wake. So the owner must be this user, SYSTEM or Administrators (an
/// elevated token's default owner), and no allow entry may give write access
/// to any other principal. Returns the file's identity (volume, index) and
/// why it is distrusted, if it is.
pub(crate) fn validate_marker(
    file: &File,
) -> Result<(Vec<u128>, Option<MarkerDistrust>), CredentialError> {
    // SAFETY: file owns a live handle for the whole call; output structures
    // are exactly sized; the descriptor and token buffers outlive every SID
    // and ACE read, and no pointer escapes.
    unsafe {
        let handle = file.as_raw_handle() as HANDLE;
        let mut info: BY_HANDLE_FILE_INFORMATION = mem::zeroed();
        if GetFileInformationByHandle(handle, &mut info) == 0 {
            return Err(CredentialError("marker file is unavailable"));
        }
        if info.nNumberOfLinks != 1
            || info.dwFileAttributes & 0x400 != 0
            || info.dwFileAttributes & 0x10 != 0
        {
            return Err(CredentialError("marker must be a regular file"));
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
        if result != 0 || descriptor.is_null() || owner.is_null() {
            return Err(CredentialError("marker permissions cannot be read"));
        }
        let user = token_user().ok_or(CredentialError("marker owner cannot be checked"))?;
        let user_sid = (*(user.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid;
        let distrust = marker_distrust(owner, dacl, user_sid);
        let combine = |high: u32, low: u32| ((high as u128) << 32) | low as u128;
        Ok((
            vec![
                info.dwVolumeSerialNumber as u128,
                combine(info.nFileIndexHigh, info.nFileIndexLow),
            ],
            distrust,
        ))
    }
}

/// [`marker_distrust`] over an SDDL descriptor naming its owner (`O:`) and
/// DACL (`D:`), with `user` (SDDL SID text, or `None` for this token's user)
/// as the current user: the rule's tests need no privilege to set an owner.
#[cfg(test)]
pub(crate) fn marker_distrust_sddl(sddl: &str, user: Option<&str>) -> Option<MarkerDistrust> {
    use windows_sys::Win32::Security::Authorization::ConvertStringSidToSidW;
    // SAFETY: converted descriptors and SIDs are freed by their guards after
    // the rule read them; the token buffer outlives its SID.
    unsafe {
        let wide: Vec<u16> = sddl.encode_utf16().chain([0]).collect();
        let mut descriptor = ptr::null_mut();
        assert!(
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                wide.as_ptr(),
                1,
                &mut descriptor,
                ptr::null_mut()
            ) != 0,
            "invalid SDDL {sddl}"
        );
        let _descriptor = Descriptor(descriptor);
        let (mut owner, mut defaulted) = (ptr::null_mut(), 0);
        assert!(Security::GetSecurityDescriptorOwner(descriptor, &mut owner, &mut defaulted) != 0);
        let (mut present, mut dacl) = (0, ptr::null_mut());
        assert!(
            Security::GetSecurityDescriptorDacl(
                descriptor,
                &mut present,
                &mut dacl,
                &mut defaulted
            ) != 0
        );
        if present == 0 {
            dacl = ptr::null_mut();
        }
        let token = token_user().expect("token user");
        let mut converted = ptr::null_mut();
        let _converted;
        let user_sid = match user {
            Some(text) => {
                let wide: Vec<u16> = text.encode_utf16().chain([0]).collect();
                assert!(ConvertStringSidToSidW(wide.as_ptr(), &mut converted) != 0);
                _converted = Descriptor(converted);
                converted
            }
            None => (*(token.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid,
        };
        marker_distrust(owner, dacl, user_sid)
    }
}

/// Replace a path's DACL (not its protection flag) from SDDL, as its owner
/// may: for tests that need a file or folder other principals can write.
#[cfg(test)]
pub(crate) fn set_dacl_sddl(path: &Path, sddl: &str) -> io::Result<()> {
    let wide: Vec<u16> = sddl.encode_utf16().chain([0]).collect();
    let name: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    // SAFETY: live NUL-terminated buffers; the descriptor is freed by its
    // guard after SetFileSecurityW copied it.
    unsafe {
        let mut descriptor = ptr::null_mut();
        if ConvertStringSecurityDescriptorToSecurityDescriptorW(
            wide.as_ptr(),
            1,
            &mut descriptor,
            ptr::null_mut(),
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        let _descriptor = Descriptor(descriptor);
        if Security::SetFileSecurityW(
            name.as_ptr(),
            Security::DACL_SECURITY_INFORMATION,
            descriptor,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
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
