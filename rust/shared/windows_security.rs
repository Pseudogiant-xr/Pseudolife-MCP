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
    /// This principal, not this user, SYSTEM or Administrators, can write
    /// it: its name as people read it, and its SID as `S-1-...` text.
    Writer { name: String, sid: String },
    /// An entry this rule does not read (a callback or object entry), or a
    /// malformed list: it may grant anything, so it is not trusted.
    Unreadable,
    /// The file has no DACL, so every principal can write it.
    NoDacl,
}

/// Rights that let a principal change a marker's contents or its
/// protection, delete it, or backdate it: deleting `.wake` or `.wake-armed`
/// between reads suppresses a wake (DELETE on the file allows that whatever
/// its folder grants), and a backdated marker is swept as a day old.
const MARKER_WRITE_RIGHTS: u32 = 0x0002 // FILE_WRITE_DATA
    | 0x0004 // FILE_APPEND_DATA
    | 0x0010 // FILE_WRITE_EA (part of FILE_GENERIC_WRITE)
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
#[cfg(test)]
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
        sid_string(sid).unwrap_or_else(|| "an unreadable principal".into())
    }
}

/// A SID as `S-1-...` text, which icacls takes as `*S-1-...` whatever the
/// display language and whatever characters the account name holds.
unsafe fn sid_string(sid: *mut c_void) -> Option<String> {
    unsafe {
        let mut text = ptr::null_mut();
        if windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW(sid, &mut text) == 0
            || text.is_null()
        {
            return None;
        }
        let _text = Descriptor(text.cast());
        let length = (0..).take_while(|&i| *text.add(i) != 0).count();
        Some(String::from_utf16_lossy(std::slice::from_raw_parts(
            text, length,
        )))
    }
}

/// This process's account as `S-1-...` text (see [`sid_string`]).
pub(crate) fn current_user_sid() -> Option<String> {
    // SAFETY: the token buffer owns the SID for the duration of the call.
    unsafe {
        let user = token_user()?;
        sid_string((*(user.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid)
    }
}

/// The display name Windows gives the SID written as `text` (`S-1-1-0`):
/// account names are localised, so tests compare against this, not English.
#[cfg(test)]
pub(crate) fn sid_name_of(text: &str) -> String {
    use windows_sys::Win32::Security::Authorization::ConvertStringSidToSidW;
    let wide: Vec<u16> = text.encode_utf16().chain([0]).collect();
    // SAFETY: a live NUL-terminated buffer; the converted SID is freed by its
    // guard after the lookup copied the name out.
    unsafe {
        let mut sid = ptr::null_mut();
        assert!(
            ConvertStringSidToSidW(wide.as_ptr(), &mut sid) != 0,
            "{text}"
        );
        let _sid = Descriptor(sid);
        sid_name(sid)
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
    unsafe { access_distrust(owner, dacl, user, false) }
}

/// FILE_DELETE_CHILD: on a folder, removing any entry in it.
const FOLDER_DELETE_CHILD: u32 = 0x0040;

/// The trust rule over an owner and DACL. For a file it is the marker rule.
/// For a folder (`folder`) another principal may also not delete children,
/// and inherit-only entries count too, because they decide what the files
/// created in it grant; CREATOR OWNER, which becomes each file's creator, is
/// trusted there.
///
/// # Safety
/// As [`marker_distrust`].
unsafe fn access_distrust(
    owner: *mut c_void,
    dacl: *const Security::ACL,
    user: *mut c_void,
    folder: bool,
) -> Option<MarkerDistrust> {
    let rights = if folder {
        MARKER_WRITE_RIGHTS | FOLDER_DELETE_CHILD
    } else {
        MARKER_WRITE_RIGHTS
    };
    unsafe {
        if !trusted_marker_principal(owner, user)
            || Security::IsWellKnownSid(owner, Security::WinCreatorOwnerRightsSid) != 0
        {
            return Some(MarkerDistrust::Owner(sid_name(owner)));
        }
        let Some(acl) = dacl.as_ref() else {
            return Some(MarkerDistrust::NoDacl);
        };
        let unreadable = || Some(MarkerDistrust::Unreadable);
        // A malformed list (an entry count or size its entries do not
        // match) is refused before any entry is read.
        if Security::IsValidAcl(dacl) == 0 {
            return unreadable();
        }
        for index in 0..acl.AceCount as u32 {
            let Some((header, bytes)) = ace_at(dacl, index) else {
                return unreadable();
            };
            // An inherit-only entry grants nothing on this file (on a folder
            // it decides what new files in it grant); a deny entry takes
            // rights away.
            let inherit_only = header.AceFlags as u32 & Security::INHERIT_ONLY_ACE != 0;
            if (inherit_only && !folder) || header.AceType == 1 {
                continue;
            }
            // Any other allow form (callback, object) is not read here, so
            // it fails closed rather than being guessed at.
            if header.AceType != 0 || bytes.len() < 16 {
                return unreadable();
            }
            // ACCESS_ALLOWED_ACE: header (4), mask (4), then the SID.
            let mask = u32::from_le_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]);
            if mask & rights == 0 {
                continue;
            }
            // A SID has an 8-byte header and four bytes per subauthority.
            let sid_size = 8 + bytes[9] as usize * 4;
            if 8 + sid_size > bytes.len() {
                return unreadable();
            }
            let sid = bytes[8..].as_ptr().cast_mut().cast::<c_void>();
            if Security::IsValidSid(sid) == 0 {
                return unreadable();
            }
            // CREATOR OWNER, inherited onto a new file, becomes its creator.
            let creator = folder
                && inherit_only
                && Security::IsWellKnownSid(sid, Security::WinCreatorOwnerSid) != 0;
            if !(trusted_marker_principal(sid, user) || creator) {
                return Some(MarkerDistrust::Writer {
                    name: sid_name(sid),
                    sid: sid_string(sid).unwrap_or_default(),
                });
            }
        }
        None
    }
}

/// The entry at `index` of `acl` as its header and its bytes, when it lies
/// wholly inside the list and is at least a header long; `None` otherwise.
///
/// # Safety
/// `acl` must be null or point to an ACL whose `AclSize` bytes are all
/// readable, live for `'a`, and every entry before `index` must lie inside
/// those bytes: `GetAce` reads the size of each earlier entry to step past
/// it, without checking it against `AclSize`. Callers walk the entries in
/// order and stop at the first `None`, which keeps that true even for a list
/// `IsValidAcl` was not asked about; this function's own bounds then keep
/// the entry it returns inside the list.
unsafe fn ace_at<'a>(
    acl: *const Security::ACL,
    index: u32,
) -> Option<(&'a Security::ACE_HEADER, &'a [u8])> {
    unsafe {
        // The list's own size bounds every entry. GetAce is given the raw
        // pointer the caller holds, not one derived from a reference to the
        // 8-byte header, since it reads the entries past it.
        let start = acl as usize;
        let end = start + acl.as_ref()?.AclSize as usize;
        let mut ace = ptr::null_mut();
        if Security::GetAce(acl, index, &mut ace) == 0 {
            return None;
        }
        let at = ace as usize;
        if at < start + mem::size_of::<Security::ACL>()
            || at + mem::size_of::<Security::ACE_HEADER>() > end
        {
            return None;
        }
        let header = ace.cast::<Security::ACE_HEADER>().as_ref()?;
        let size = header.AceSize as usize;
        if size < mem::size_of::<Security::ACE_HEADER>() || at + size > end {
            return None;
        }
        Some((header, std::slice::from_raw_parts(ace.cast::<u8>(), size)))
    }
}

/// [`marker_distrust`] over raw ACL bytes, with this token's user as owner
/// and user: lets tests hand the rule a malformed list. Safe because it
/// asserts the header fits and its `AclSize` does not run past the buffer,
/// which bounds every read the rule makes.
#[cfg(test)]
pub(crate) fn marker_distrust_raw(acl: &mut [u32]) -> Option<MarkerDistrust> {
    assert!(acl.len() >= 2, "an ACL header is 8 bytes");
    assert!(
        (acl[0] >> 16) as usize <= acl.len() * 4,
        "AclSize runs past the buffer"
    );
    // SAFETY: the buffer is live, u32-aligned and as long as its AclSize for
    // the call; the token buffer outlives the SID it holds.
    unsafe {
        let token = token_user().expect("token user");
        let user = (*(token.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid;
        marker_distrust(user, acl.as_mut_ptr().cast(), user)
    }
}

/// [`ace_at`] on raw list bytes that skip `IsValidAcl`, as (type, size):
/// lets tests reach its own bounds checks. Safe for the same reason as
/// [`marker_distrust_raw`].
#[cfg(test)]
pub(crate) fn ace_at_raw(acl: &[u32], index: u32) -> Option<(u8, u16)> {
    assert!(acl.len() >= 2 && (acl[0] >> 16) as usize <= acl.len() * 4);
    // SAFETY: as above; the earlier entries are walked first, as ace_at
    // requires, and the header and slice do not outlive the buffer.
    unsafe {
        for earlier in 0..index {
            ace_at(acl.as_ptr().cast(), earlier)?;
        }
        ace_at(acl.as_ptr().cast(), index).map(|(header, bytes)| {
            assert_eq!(bytes.len(), header.AceSize as usize);
            (header.AceType, header.AceSize)
        })
    }
}

/// `path` as a NUL-terminated verbatim path, as std's own open uses, so a
/// path longer than MAX_PATH (the rollback sibling of a long target) still
/// opens.
fn verbatim(path: &Path) -> io::Result<Vec<u16>> {
    const SLASH: u16 = b'\\' as u16;
    let absolute = std::path::absolute(path)?;
    let plain: Vec<u16> = absolute.as_os_str().encode_wide().collect();
    let prefix = [SLASH, SLASH, b'?' as u16, SLASH];
    let mut wide: Vec<u16> = if plain.starts_with(&prefix) {
        plain
    } else if plain.starts_with(&[SLASH, SLASH]) {
        // A share: \\server\share becomes \\?\UNC\server\share.
        prefix
            .iter()
            .copied()
            .chain("UNC".encode_utf16())
            .chain(plain[1..].iter().copied())
            .collect()
    } else {
        prefix.iter().copied().chain(plain).collect()
    };
    if wide.contains(&0) {
        return Err(io::Error::from(io::ErrorKind::InvalidInput));
    }
    wide.push(0);
    Ok(wide)
}

/// Create the folder `path` with an owner-only DACL from the moment it
/// exists. A folder created plainly and protected afterwards gives anything
/// another task creates in it during that gap (the handshake cache's own
/// folder) inherited entries the protection then strips, leaving it with
/// none (#1538 review); its protected DACL passes nothing on, so what is
/// created in it later gets explicit default entries instead. The caller
/// still applies [`make_private`] to set the owner explicitly.
pub(crate) fn create_dir_owner_only(path: &Path) -> io::Result<()> {
    use windows_sys::Win32::{
        Security::SECURITY_ATTRIBUTES, Storage::FileSystem::CreateDirectoryW,
    };
    let wide = verbatim(path)?;
    let sddl: Vec<u16> = "D:P(A;;FA;;;OW)".encode_utf16().chain([0]).collect();
    // SAFETY: live NUL-terminated buffers; the converted descriptor is owned
    // by its guard and outlives CreateDirectoryW, which copies it.
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
        let _descriptor = Descriptor(descriptor);
        let attributes = SECURITY_ATTRIBUTES {
            nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor,
            bInheritHandle: 0,
        };
        if CreateDirectoryW(wide.as_ptr(), &attributes) == 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

/// Create `path` as a new file whose DACL is owner-only from the moment it
/// exists (`CREATE_NEW`, not following a reparse point), opened for the
/// shim's read, write, attribute and security changes, sharing read and
/// write. A file created in a folder another account can read would
/// otherwise inherit that read access until [`make_private`] replaced it,
/// and a handle opened in that window keeps it (#1538 review); the caller
/// still applies [`make_private`] to set the owner explicitly.
pub(crate) fn create_owner_only(path: &Path) -> io::Result<File> {
    use std::os::windows::io::FromRawHandle;
    use windows_sys::Win32::{
        Foundation::INVALID_HANDLE_VALUE,
        Security::SECURITY_ATTRIBUTES,
        Storage::FileSystem::{
            CREATE_NEW, CreateFileW, FILE_ATTRIBUTE_NORMAL, FILE_FLAG_OPEN_REPARSE_POINT,
            FILE_SHARE_READ, FILE_SHARE_WRITE,
        },
    };
    let wide = verbatim(path)?;
    let sddl: Vec<u16> = "D:P(A;;FA;;;OW)".encode_utf16().chain([0]).collect();
    // SAFETY: live NUL-terminated buffers; the converted descriptor is owned
    // by its guard and outlives CreateFileW, which copies it; the returned
    // handle is valid and owned by the File it is moved into exactly once.
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
        let _descriptor = Descriptor(descriptor);
        let attributes = SECURITY_ATTRIBUTES {
            nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor,
            bInheritHandle: 0,
        };
        // The access `state::options` asks for on create (read, write,
        // attributes, READ_CONTROL, WRITE_DAC, WRITE_OWNER).
        let handle = CreateFileW(
            wide.as_ptr(),
            0x0012019f | 0x000c0000,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            &attributes,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            ptr::null_mut(),
        );
        if handle == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        Ok(File::from_raw_handle(handle as _))
    }
}

/// A file's DACL as SDDL, for tests that pin what a creation produced.
#[cfg(test)]
pub(crate) fn dacl_sddl(file: &File) -> String {
    use windows_sys::Win32::Security::Authorization::ConvertSecurityDescriptorToStringSecurityDescriptorW;
    // SAFETY: the handle is live; both descriptors are freed by their guards
    // after the string is copied out.
    unsafe {
        let (mut dacl, mut descriptor) = (ptr::null_mut(), ptr::null_mut());
        assert_eq!(
            GetSecurityInfo(
                file.as_raw_handle() as HANDLE,
                SE_FILE_OBJECT,
                Security::DACL_SECURITY_INFORMATION,
                ptr::null_mut(),
                ptr::null_mut(),
                &mut dacl,
                ptr::null_mut(),
                &mut descriptor,
            ),
            0
        );
        let _descriptor = Descriptor(descriptor);
        let (mut text, mut length) = (ptr::null_mut(), 0u32);
        assert!(
            ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor,
                1,
                Security::DACL_SECURITY_INFORMATION,
                &mut text,
                &mut length,
            ) != 0
        );
        let _text = Descriptor(text.cast());
        let length = (0..).take_while(|&i| *text.add(i) != 0).count();
        String::from_utf16_lossy(std::slice::from_raw_parts(text, length))
    }
}

/// The (volume, file index) of the open folder `directory`, refusing a
/// reparse point or anything not a folder: read through the same handle a
/// check is made on, so a cached result belongs to that object.
pub(crate) fn folder_identity(directory: &File) -> Result<(u64, u64), CredentialError> {
    let invalid = CredentialError("folder must be a plain folder");
    // SAFETY: the handle stays live for the call; the structure is exactly
    // sized and initialized.
    unsafe {
        let mut info: BY_HANDLE_FILE_INFORMATION = mem::zeroed();
        if GetFileInformationByHandle(directory.as_raw_handle() as HANDLE, &mut info) == 0
            || info.dwFileAttributes & 0x10 == 0
            || info.dwFileAttributes & 0x400 != 0
        {
            return Err(invalid);
        }
        Ok((
            info.dwVolumeSerialNumber as u64,
            ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
        ))
    }
}

/// Check a folder this client keeps private state in but did not create
/// (or that already holds what another client put there): rather than
/// replacing its DACL, which would leave everything in it that has only
/// inherited access with none (#1538), it must already be safe. The owner is
/// this user, SYSTEM or Administrators, and no other principal can add,
/// delete or replace what is in it. `Ok(None)` trusts it.
pub(crate) fn validate_folder(directory: &File) -> Result<Option<MarkerDistrust>, CredentialError> {
    // SAFETY: as validate_marker; the handle is a directory opened with
    // READ_CONTROL that stays live for the whole call.
    unsafe {
        let handle = directory.as_raw_handle() as HANDLE;
        let mut info: BY_HANDLE_FILE_INFORMATION = mem::zeroed();
        if GetFileInformationByHandle(handle, &mut info) == 0
            || info.dwFileAttributes & 0x10 == 0
            || info.dwFileAttributes & 0x400 != 0
        {
            return Err(CredentialError("folder must be a plain folder"));
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
            return Err(CredentialError("folder permissions cannot be read"));
        }
        let user = token_user().ok_or(CredentialError("folder owner cannot be checked"))?;
        let user_sid = (*(user.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid;
        Ok(access_distrust(owner, dacl, user_sid, true))
    }
}

/// Whether the folder at `path` has a protected DACL in which no entry is
/// inherited by what is created in it: the protection that leaves files
/// that had only inherited access with none (#1538).
pub(crate) fn protected_without_inheritance(path: &Path) -> bool {
    use windows_sys::Win32::Security::Authorization::GetNamedSecurityInfoW;
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    if wide[..wide.len() - 1].contains(&0) {
        return false;
    }
    // SAFETY: a live NUL-terminated path; the descriptor owns the DACL until
    // it is freed; every ACE read is bounded by the ACL's own count.
    unsafe {
        let mut dacl: *mut Security::ACL = ptr::null_mut();
        let mut descriptor = ptr::null_mut();
        if GetNamedSecurityInfoW(
            wide.as_ptr(),
            SE_FILE_OBJECT,
            Security::DACL_SECURITY_INFORMATION,
            ptr::null_mut(),
            ptr::null_mut(),
            &mut dacl,
            ptr::null_mut(),
            &mut descriptor,
        ) != 0
        {
            return false;
        }
        let _descriptor = Descriptor(descriptor);
        let (mut control, mut revision) = (0, 0);
        let Some(acl) = dacl.as_ref() else {
            return false;
        };
        if Security::IsValidAcl(dacl) == 0
            || Security::GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) == 0
            || control & Security::SE_DACL_PROTECTED == 0
        {
            return false;
        }
        for index in 0..acl.AceCount as u32 {
            let Some((header, _)) = ace_at(dacl, index) else {
                return false;
            };
            let flags = header.AceFlags as u32;
            if flags & (Security::OBJECT_INHERIT_ACE | Security::CONTAINER_INHERIT_ACE) != 0 {
                return false;
            }
        }
        true
    }
}

/// Whether `path` is owned by this user (or Administrators, for an elevated
/// caller) and has an access list with no entries: what an earlier native client's folder protection left on files
/// that had only inherited access (#1538). Read with READ_CONTROL alone,
/// which the owner always has.
pub(crate) fn empty_dacl_owned(path: &Path) -> bool {
    use windows_sys::Win32::Security::Authorization::GetNamedSecurityInfoW;
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    if wide[..wide.len() - 1].contains(&0) {
        return false;
    }
    // SAFETY: a live NUL-terminated path; the descriptor owns the owner and
    // DACL pointers until it is freed; the token buffer outlives the
    // comparison. GetNamedSecurityInfoW asks for READ_CONTROL only, which
    // the owner keeps however empty the DACL is.
    unsafe {
        let mut owner = ptr::null_mut();
        let mut dacl: *mut Security::ACL = ptr::null_mut();
        let mut descriptor = ptr::null_mut();
        let result = GetNamedSecurityInfoW(
            wide.as_ptr(),
            SE_FILE_OBJECT,
            Security::OWNER_SECURITY_INFORMATION | Security::DACL_SECURITY_INFORMATION,
            &mut owner,
            ptr::null_mut(),
            &mut dacl,
            ptr::null_mut(),
            &mut descriptor,
        );
        let _descriptor = Descriptor(descriptor);
        let Some(acl) = dacl.as_ref() else {
            return false;
        };
        if result != 0 || owner.is_null() || acl.AceCount != 0 {
            return false;
        }
        let Some(user) = token_user() else {
            return false;
        };
        let user_sid = (*(user.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid;
        // An elevated process (an installer run as administrator) gives the
        // files it creates to Administrators; only an elevated caller can
        // read such a file's owner here at all.
        Security::EqualSid(owner, user_sid) != 0
            || Security::IsWellKnownSid(owner, Security::WinBuiltinAdministratorsSid) != 0
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
    distrust_sddl(sddl, user, false)
}

/// [`marker_distrust_sddl`] for the folder rule ([`validate_folder`]).
#[cfg(test)]
pub(crate) fn folder_distrust_sddl(sddl: &str, user: Option<&str>) -> Option<MarkerDistrust> {
    distrust_sddl(sddl, user, true)
}

#[cfg(test)]
fn distrust_sddl(sddl: &str, user: Option<&str>, folder: bool) -> Option<MarkerDistrust> {
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
        access_distrust(owner, dacl, user_sid, folder)
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
