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

/// One element of a private path, as the private-path rule sees it
/// (`private_path`, which holds the rule itself).
pub(crate) enum PathEntry {
    Missing,
    Plain,
    /// A cloud-files placeholder: a reparse point that redirects nothing.
    Placeholder,
    /// A symlink or junction, where it leads (as a path this process can
    /// open), and why it is not trusted, if it is not.
    Link {
        target: std::path::PathBuf,
        distrust: Option<LinkDistrust>,
        /// The link entry's own (volume serial, file index): one object
        /// however its path is spelled or cased.
        identity: (u64, u64),
    },
    /// Any other reparse point, by tag.
    Other(u32),
    /// A link whose target cannot be checked, and why.
    Uncheckable(String),
}

/// Why a link is not trusted: the link's own owner or access list, or the
/// owner or access list of the folder holding it.
pub(crate) enum LinkDistrust {
    Link(MarkerDistrust),
    Holder(std::path::PathBuf, MarkerDistrust),
}

/// `FSCTL_GET_REPARSE_POINT` (winioctl.h): read a reparse point's data.
const FSCTL_GET_REPARSE_POINT: u32 = 0x0009_00A8;
/// `MAXIMUM_REPARSE_DATA_BUFFER_SIZE` (winnt.h).
const MAX_REPARSE_DATA: usize = 16 * 1024;
/// `SYMLINK_FLAG_RELATIVE`: the symlink's substitute name is relative to the
/// folder holding it.
const SYMLINK_FLAG_RELATIVE: u32 = 1;
/// `IO_REPARSE_TAG_SYMLINK`, whose reparse data carries a flags field.
const TAG_SYMLINK: u32 = crate::private_path::TAG_SYMLINK;

/// Inspect one existing or missing path element without following it: what
/// it is, and for a link, where it leads and whether its owner and access
/// list, and its folder's, let no other account remove, replace or rewrite
/// it (the rule `private_path` documents).
pub(crate) fn inspect_entry(path: &Path) -> io::Result<PathEntry> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_REPARSE_POINT, FILE_ATTRIBUTE_TAG_INFO, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ,
        FILE_SHARE_WRITE, FileAttributeTagInfo, GetFileInformationByHandleEx,
    };
    const READ_CONTROL: u32 = 0x0002_0000;
    let open = |target: &Path, access: u32, follow: bool| {
        let flags = if follow {
            FILE_FLAG_BACKUP_SEMANTICS
        } else {
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
        };
        OpenOptions::new()
            .access_mode(access)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(flags)
            .open(target)
    };
    // The cheap look first, as before this rule: a plain folder costs one
    // attribute read and needs no more access than it ever did.
    match inspect_redirect(path) {
        Ok(false) => return Ok(PathEntry::Plain),
        Ok(true) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(PathEntry::Missing),
        Err(error) => return Err(error),
    }
    // One handle for everything read about the element (tag, target, owner
    // and access list), so nothing can be swapped between the reads. A
    // reparse point that is not a link needs only its attributes, so where
    // READ_CONTROL is refused it is read without: a link read that way is
    // refused below.
    let (entry, readable) = match open(path, READ_CONTROL | FILE_READ_ATTRIBUTES, false) {
        Ok(entry) => (entry, true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(PathEntry::Missing),
        Err(error) if error.kind() == io::ErrorKind::PermissionDenied => {
            match open(path, FILE_READ_ATTRIBUTES, false) {
                Ok(entry) => (entry, false),
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    return Ok(PathEntry::Missing);
                }
                Err(error) => return Err(error),
            }
        }
        Err(error) => return Err(error),
    };
    let mut tag_info: FILE_ATTRIBUTE_TAG_INFO = FILE_ATTRIBUTE_TAG_INFO {
        FileAttributes: 0,
        ReparseTag: 0,
    };
    // SAFETY: the handle stays live for the call; the output structure is
    // exactly the size passed.
    if unsafe {
        GetFileInformationByHandleEx(
            entry.as_raw_handle() as HANDLE,
            FileAttributeTagInfo,
            (&mut tag_info as *mut FILE_ATTRIBUTE_TAG_INFO).cast(),
            mem::size_of::<FILE_ATTRIBUTE_TAG_INFO>() as u32,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    if tag_info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT == 0 {
        return Ok(PathEntry::Plain);
    }
    let tag = tag_info.ReparseTag;
    // The one classifier of reparse tags, beside the rule's other parts.
    match crate::private_path::reparse_kind(tag) {
        crate::private_path::ReparseKind::Placeholder => return Ok(PathEntry::Placeholder),
        crate::private_path::ReparseKind::Other => return Ok(PathEntry::Other(tag)),
        crate::private_path::ReparseKind::Link => {}
    }
    if !readable {
        return Err(io::Error::other(
            "this account may not read the link's permissions",
        ));
    }
    // Where the link leads, when that can be checked: an unrecognised form,
    // or reparse data that is malformed or changed while it was read, is a
    // refusal of its own, not a permissions problem.
    let target = match reparse_target(&entry, tag) {
        Ok(target) => target,
        Err(error) => return Ok(PathEntry::Uncheckable(error.to_string())),
    };
    // SAFETY: the handle stays live for the call; the output structure is
    // exactly sized and initialized before Win32 fills it.
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { mem::zeroed() };
    if unsafe { GetFileInformationByHandle(entry.as_raw_handle() as HANDLE, &mut info) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let identity = (
        info.dwVolumeSerialNumber as u64,
        ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    );
    // SAFETY: token_user returns an owned, usize-aligned TOKEN_USER buffer;
    // it outlives every use of its SID below.
    let user =
        unsafe { token_user() }.ok_or_else(|| io::Error::other("this account cannot be read"))?;
    let user_sid = unsafe { (*(user.as_ptr().cast::<Security::TOKEN_USER>())).User.Sid };
    if let Some(why) = security_distrust(&entry, user_sid, Rule::File)? {
        return Ok(PathEntry::Link {
            target,
            distrust: Some(LinkDistrust::Link(why)),
            identity,
        });
    }
    let Some(holder_path) = path.parent() else {
        return Ok(PathEntry::Uncheckable(
            "a link with no folder above it".into(),
        ));
    };
    // Followed: the folders above it were checked first.
    let holder = open(holder_path, READ_CONTROL, true)?;
    // Windows resolves a relative symlink against where its folder really
    // is, after the links above it, not against the text of the path: so
    // is the walk told where it leads.
    let target = if target.is_absolute() {
        target
    } else {
        use std::os::windows::ffi::OsStrExt;
        let wide: Vec<u16> = target.as_os_str().encode_wide().collect();
        match final_path(&holder) {
            Ok(real) => match resolve_relative(&real, &wide) {
                Some(resolved) => resolved,
                None => {
                    return Ok(PathEntry::Uncheckable(
                        "a relative link target this client cannot check".into(),
                    ));
                }
            },
            Err(error) => return Ok(PathEntry::Uncheckable(error.to_string())),
        }
    };
    let distrust = security_distrust(&holder, user_sid, Rule::LinkHolder)?
        .map(|why| LinkDistrust::Holder(holder_path.to_path_buf(), why));
    Ok(PathEntry::Link {
        target,
        distrust,
        identity,
    })
}

/// The path `file`'s handle names once every link is followed
/// (`GetFinalPathNameByHandleW`, as `\\?\C:\...` or `\\?\UNC\...`).
fn final_path(file: &File) -> io::Result<std::path::PathBuf> {
    use std::os::windows::ffi::OsStringExt;
    use windows_sys::Win32::Storage::FileSystem::GetFinalPathNameByHandleW;
    let mut buffer = vec![0u16; 1024];
    loop {
        // SAFETY: the handle is live; the buffer is writable for the length
        // passed, and only the returned count of units is read.
        let written = unsafe {
            GetFinalPathNameByHandleW(
                file.as_raw_handle() as HANDLE,
                buffer.as_mut_ptr(),
                buffer.len() as u32,
                0,
            )
        } as usize;
        if written == 0 {
            return Err(io::Error::last_os_error());
        }
        if written < buffer.len() {
            return Ok(std::ffi::OsString::from_wide(&buffer[..written]).into());
        }
        if written > 32 * 1024 {
            return Err(io::Error::other("a folder path too long to check"));
        }
        buffer.resize(written + 1, 0);
    }
}

/// A relative link target `wide` (UTF-16, as stored) resolved against
/// `real`, the verbatim real path of the link's folder, the way Windows
/// resolves it: split on `\` only, `.` dropped, each `..` taken against the
/// part before it (never past the volume root), a leading `\` starting at
/// the volume root. Each part is added as one name, so nothing is re-parsed
/// or folded behind the walk's back. A part holding `/` (not a separator
/// in a stored target, and not a valid NTFS name) is refused as `None`.
fn resolve_relative(real: &Path, wide: &[u16]) -> Option<std::path::PathBuf> {
    use std::os::windows::ffi::OsStringExt;
    use std::path::Component;
    let (backslash, slash, dot) = (u16::from(b'\\'), u16::from(b'/'), u16::from(b'.'));
    // The volume root: the prefix and root components of `real`.
    let root: std::path::PathBuf = real
        .components()
        .take_while(|c| matches!(c, Component::Prefix(_) | Component::RootDir))
        .collect();
    let mut out = if wide.first() == Some(&backslash) {
        root.clone()
    } else {
        real.to_path_buf()
    };
    for part in wide.split(|unit| *unit == backslash) {
        if part.contains(&slash) {
            return None;
        }
        match part {
            [] => {}
            [d] if *d == dot => {}
            [d, e] if *d == dot && *e == dot => {
                if out != root {
                    out.pop();
                }
            }
            name => out.push(std::ffi::OsString::from_wide(name)),
        }
    }
    Some(out)
}

#[cfg(test)]
pub(crate) fn resolve_relative_for_test(real: &Path, target: &str) -> Option<std::path::PathBuf> {
    resolve_relative(real, &target.encode_utf16().collect::<Vec<_>>())
}

/// [`access_distrust`] over the owner and DACL read from `file`'s handle.
fn security_distrust(
    file: &File,
    user: *mut c_void,
    rule: Rule,
) -> io::Result<Option<MarkerDistrust>> {
    // SAFETY: the handle is live for the call; the descriptor owns the owner
    // and DACL pointers until its guard frees it, after the rule read them;
    // `user` is a SID the caller keeps alive.
    unsafe {
        let mut owner = ptr::null_mut();
        let mut dacl = ptr::null_mut();
        let mut descriptor = ptr::null_mut();
        let result = GetSecurityInfo(
            file.as_raw_handle() as HANDLE,
            SE_FILE_OBJECT,
            Security::OWNER_SECURITY_INFORMATION | Security::DACL_SECURITY_INFORMATION,
            &mut owner,
            ptr::null_mut(),
            &mut dacl,
            ptr::null_mut(),
            &mut descriptor,
        );
        let _descriptor = Descriptor(descriptor);
        if result != 0 {
            return Err(io::Error::from_raw_os_error(result as i32));
        }
        if descriptor.is_null() || owner.is_null() {
            return Err(io::Error::other("its owner could not be read"));
        }
        Ok(access_distrust(owner, dacl, user, rule))
    }
}

/// Where the symlink or junction open on `entry` (not followed) leads, from
/// its substitute name, as a verbatim path this process can open:
/// `\??\C:\x` is `\\?\C:\x`, `\??\UNC\server\share` is
/// `\\?\UNC\server\share` and `\??\Volume{...}\` is `\\?\Volume{...}\`. A relative symlink's name is
/// returned as it is, for the caller to join to the link's folder. Any
/// other form is an error, so the path is refused rather than guessed at.
fn reparse_target(entry: &File, tag: u32) -> io::Result<std::path::PathBuf> {
    use std::os::windows::ffi::OsStringExt;
    use windows_sys::Win32::System::IO::DeviceIoControl;
    let malformed = || io::Error::other("malformed reparse data");
    let mut buffer = vec![0u8; MAX_REPARSE_DATA];
    let mut returned = 0u32;
    // SAFETY: the handle is live; the output buffer is writable for the
    // size passed, and only the `returned` bytes are read below.
    if unsafe {
        DeviceIoControl(
            entry.as_raw_handle() as HANDLE,
            FSCTL_GET_REPARSE_POINT,
            ptr::null(),
            0,
            buffer.as_mut_ptr().cast(),
            buffer.len() as u32,
            &mut returned,
            ptr::null_mut(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let data = &buffer[..(returned as usize).min(buffer.len())];
    let u16_at = |at: usize| {
        data.get(at..at + 2)
            .map(|b| u16::from_le_bytes([b[0], b[1]]) as usize)
            .ok_or_else(malformed)
    };
    let u32_at = |at: usize| {
        data.get(at..at + 4)
            .map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
            .ok_or_else(malformed)
    };
    if u32_at(0)? != tag {
        return Err(io::Error::other(
            "the reparse point changed while it was read",
        ));
    }
    // REPARSE_DATA_BUFFER: tag (4), data length (2), reserved (2), the
    // substitute and print names' offsets and lengths (2 each), a symlink's
    // flags (4), then the path buffer the offsets count from.
    let (start, relative) = if tag == TAG_SYMLINK {
        (20, u32_at(16)? & SYMLINK_FLAG_RELATIVE != 0)
    } else {
        (16, false)
    };
    let (offset, length) = (u16_at(8)?, u16_at(10)?);
    let name = data
        .get(start + offset..start + offset + length)
        .filter(|bytes| bytes.len() % 2 == 0)
        .ok_or_else(malformed)?;
    let wide: Vec<u16> = name
        .chunks_exact(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .collect();
    if relative {
        return Ok(std::ffi::OsString::from_wide(&wide).into());
    }
    // An unrecognised form (an NT device path, say) is refused, never walked
    // as written: a path Win32 reads differently from the kernel could leave
    // the folders the link really crosses unchecked.
    target_path(&wide, false)
        .ok_or_else(|| io::Error::other("a link target in a form this client cannot check"))
}

/// [`reparse_target`]'s mapping of a substitute name: exactly the three
/// forms the walk can check, `\??\X:\...`, `\??\UNC\server\share...` and
/// `\??\Volume{...}\...`; `None` (refused) for anything else, the
/// `\??\GLOBALROOT\Device\...` form an ordinary user can store included.
///
/// Each is returned as a verbatim (`\\?\`) path, never a plain DOS one: the
/// link follows its NT name literally, while Win32 would normalise a plain
/// path (dropping a trailing `.` or space, collapsing `..`) and so inspect a
/// different object from the one the link leads to.
fn target_path(wide: &[u16], relative: bool) -> Option<std::path::PathBuf> {
    if relative {
        return None;
    }
    use std::os::windows::ffi::OsStringExt;
    // Parsed on the UTF-16 units: a valid Windows name may hold an unpaired
    // surrogate, which no string conversion keeps, so the rest is carried
    // through as it is.
    let units = |text: &str| text.encode_utf16().collect::<Vec<u16>>();
    let rest = wide.strip_prefix(units("\\??\\").as_slice())?;
    let verbatim = |lead: &str, tail: &[u16]| {
        let mut out = units(lead);
        out.extend_from_slice(tail);
        std::path::PathBuf::from(std::ffi::OsString::from_wide(&out))
    };
    // A component that is `.` or `..`, or holds `/`: the walk would fold or
    // split it where the link follows it literally, so it is refused rather
    // than guessed at.
    let (backslash, slash, dot) = (u16::from(b'\\'), u16::from(b'/'), u16::from(b'.'));
    if rest
        .split(|unit| *unit == backslash)
        .any(|part| part.contains(&slash) || part == [dot] || part == [dot, dot])
    {
        return None;
    }
    if let Some(share) = rest.strip_prefix(units("UNC\\").as_slice()) {
        return (!share.is_empty()).then(|| verbatim("\\\\?\\UNC\\", share));
    }
    if rest.starts_with(units("Volume{").as_slice()) {
        return Some(verbatim("\\\\?\\", rest));
    }
    let drive = rest.len() >= 3
        && u8::try_from(rest[0]).is_ok_and(|letter| letter.is_ascii_alphabetic())
        && rest[1] == u16::from(b':')
        && rest[2] == u16::from(b'\\');
    drive.then(|| verbatim("\\\\?\\", rest))
}

#[cfg(test)]
pub(crate) fn target_path_wide_for_test(
    wide: &[u16],
    relative: bool,
) -> Option<std::path::PathBuf> {
    target_path(wide, relative)
}

#[cfg(test)]
pub(crate) fn target_path_for_test(name: &str, relative: bool) -> Option<std::path::PathBuf> {
    target_path(&name.encode_utf16().collect::<Vec<_>>(), relative)
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
    super::reject_ancestor_redirects(path).map_err(|_| invalid)?;
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
    unsafe { access_distrust(owner, dacl, user, Rule::File) }
}

/// FILE_DELETE_CHILD: on a folder, removing any entry in it.
const FOLDER_DELETE_CHILD: u32 = 0x0040;

/// Rights on the folder holding a link that let a principal remove or
/// replace that link, or grant itself the right to: deleting a child,
/// deleting (so renaming away) the folder itself, and changing its access
/// list or owner. Adding files or subfolders (`C:\` grants that to
/// Authenticated Users) cannot replace an entry that exists, so those
/// rights are not counted.
const LINK_HOLDER_RIGHTS: u32 = FOLDER_DELETE_CHILD
    | 0x0001_0000 // DELETE
    | 0x0004_0000 // WRITE_DAC
    | 0x0008_0000 // WRITE_OWNER
    | 0x1000_0000; // GENERIC_ALL

/// `NT SERVICE\TrustedInstaller`, the owner of `C:\` and the system folders.
const TRUSTED_INSTALLER: &str = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";

/// Which trust rule [`access_distrust`] applies.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Rule {
    /// A marker, or a link itself: no other principal may change it.
    File,
    /// A folder this client keeps state in: no other principal may change
    /// what is in it.
    Folder,
    /// The folder holding a link: no other principal may remove or replace
    /// the link.
    LinkHolder,
}

/// The trust rule over an owner and DACL. For a file it is the marker rule.
/// For a folder ([`Rule::Folder`]) another principal may also not delete
/// children, and inherit-only entries count too, because they decide what
/// the files created in it grant; CREATOR OWNER, which becomes each file's
/// creator, is trusted there. For a link's folder ([`Rule::LinkHolder`])
/// only the rights that could remove or replace the link count.
///
/// # Safety
/// As [`marker_distrust`].
unsafe fn access_distrust(
    owner: *mut c_void,
    dacl: *const Security::ACL,
    user: *mut c_void,
    rule: Rule,
) -> Option<MarkerDistrust> {
    let folder = rule == Rule::Folder;
    let rights = match rule {
        Rule::File => MARKER_WRITE_RIGHTS,
        Rule::Folder => MARKER_WRITE_RIGHTS | FOLDER_DELETE_CHILD,
        Rule::LinkHolder => LINK_HOLDER_RIGHTS,
    };
    unsafe {
        // TrustedInstaller owns `C:\` and the system's folders; it can change
        // anything this user can, so a link's folder it owns is trusted.
        let installer =
            rule == Rule::LinkHolder && sid_string(owner).as_deref() == Some(TRUSTED_INSTALLER);
        if !(trusted_marker_principal(owner, user) || installer)
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
            let installer =
                rule == Rule::LinkHolder && sid_string(sid).as_deref() == Some(TRUSTED_INSTALLER);
            if !(trusted_marker_principal(sid, user) || creator || installer) {
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
        Ok(access_distrust(owner, dacl, user_sid, Rule::Folder))
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
    distrust_sddl(sddl, user, Rule::File)
}

/// [`marker_distrust_sddl`] for the folder rule ([`validate_folder`]).
#[cfg(test)]
pub(crate) fn folder_distrust_sddl(sddl: &str, user: Option<&str>) -> Option<MarkerDistrust> {
    distrust_sddl(sddl, user, Rule::Folder)
}

/// [`marker_distrust_sddl`] for the rule on the folder holding a link
/// ([`inspect_entry`]).
#[cfg(test)]
pub(crate) fn link_holder_distrust_sddl(sddl: &str, user: Option<&str>) -> Option<MarkerDistrust> {
    distrust_sddl(sddl, user, Rule::LinkHolder)
}

#[cfg(test)]
fn distrust_sddl(sddl: &str, user: Option<&str>, rule: Rule) -> Option<MarkerDistrust> {
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
        access_distrust(owner, dacl, user_sid, rule)
    }
}

/// Make the empty folder `link` a junction to `target` through the Windows
/// API (`FSCTL_SET_REPARSE_POINT`), for the private-path tests: no shell, no
/// child process, and no privilege beyond owning `link`.
#[cfg(test)]
pub(crate) fn make_junction_for_test(link: &Path, target: &Path) -> io::Result<()> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::System::IO::DeviceIoControl;
    std::fs::create_dir(link)?;
    let substitute: Vec<u16> = format!("\\??\\{}", target.display())
        .encode_utf16()
        .collect();
    let print: Vec<u16> = target.as_os_str().encode_wide().collect();
    let mut names = Vec::new();
    for unit in substitute.iter().chain([&0]).chain(&print).chain([&0]) {
        names.extend_from_slice(&unit.to_le_bytes());
    }
    let mut data = Vec::new();
    data.extend_from_slice(&0xA000_0003u32.to_le_bytes());
    data.extend_from_slice(&((8 + names.len()) as u16).to_le_bytes());
    data.extend_from_slice(&0u16.to_le_bytes());
    data.extend_from_slice(&0u16.to_le_bytes());
    data.extend_from_slice(&((substitute.len() * 2) as u16).to_le_bytes());
    data.extend_from_slice(&(((substitute.len() + 1) * 2) as u16).to_le_bytes());
    data.extend_from_slice(&((print.len() * 2) as u16).to_le_bytes());
    data.extend_from_slice(&names);
    // GENERIC_WRITE, opening the folder itself (backup semantics, no follow).
    let folder = OpenOptions::new()
        .access_mode(0x4000_0000)
        .custom_flags(0x0200_0000 | 0x0020_0000)
        .open(link)?;
    let mut returned = 0u32;
    // SAFETY: the handle is live; the input buffer is readable for its
    // length; nothing is written back but the returned count.
    if unsafe {
        DeviceIoControl(
            folder.as_raw_handle() as HANDLE,
            0x0009_00A4, // FSCTL_SET_REPARSE_POINT
            data.as_ptr().cast(),
            data.len() as u32,
            ptr::null_mut(),
            0,
            &mut returned,
            ptr::null_mut(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Replace the DACL of `path` itself from SDDL, not following a link there
/// (as [`set_dacl_sddl`] would): for tests of a link's own access list.
#[cfg(test)]
pub(crate) fn set_link_dacl_sddl(path: &Path, sddl: &str, protected: bool) -> io::Result<()> {
    use std::os::windows::fs::OpenOptionsExt;
    let wide: Vec<u16> = sddl.encode_utf16().chain([0]).collect();
    // WRITE_DAC | READ_CONTROL, the folder itself (backup semantics, no follow).
    let link = OpenOptions::new()
        .access_mode(0x0004_0000 | 0x0002_0000)
        .custom_flags(0x0200_0000 | 0x0020_0000)
        .open(path)?;
    // SAFETY: live NUL-terminated buffer; the descriptor owns the DACL until
    // its guard frees it, after SetSecurityInfo copied it; the handle is live.
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
        let (mut present, mut dacl, mut defaulted) = (0, ptr::null_mut(), 0);
        if Security::GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl, &mut defaulted)
            == 0
        {
            return Err(io::Error::last_os_error());
        }
        let information = Security::DACL_SECURITY_INFORMATION
            | if protected {
                Security::PROTECTED_DACL_SECURITY_INFORMATION
            } else {
                Security::UNPROTECTED_DACL_SECURITY_INFORMATION
            };
        let result = SetSecurityInfo(
            link.as_raw_handle() as HANDLE,
            SE_FILE_OBJECT,
            information,
            ptr::null_mut(),
            ptr::null_mut(),
            dacl,
            ptr::null_mut(),
        );
        if result != 0 {
            return Err(io::Error::from_raw_os_error(result as i32));
        }
    }
    Ok(())
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
