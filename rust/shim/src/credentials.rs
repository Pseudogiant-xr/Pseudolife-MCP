//! Bounded, coherent bearer snapshots. Diagnostics never include credential bytes.
#[cfg(unix)]
mod unix_accounts;

use sha2::{Digest, Sha256};
#[cfg(not(windows))]
use std::fs;
use std::{
    fmt,
    fs::{File, Metadata, OpenOptions},
    io::{self, Read},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};

pub const MAX_TOKEN_BYTES: usize = 4096;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CredentialError(pub &'static str);
impl fmt::Display for CredentialError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for CredentialError {}

#[derive(Clone, PartialEq, Eq)]
pub struct Generation(GenerationMarker);
#[derive(Clone, PartialEq, Eq)]
enum GenerationMarker {
    Static(u64),
    File {
        identity: Vec<u128>,
        digest: [u8; 32],
    },
}
impl fmt::Debug for Generation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("<credential generation>")
    }
}
/// A coherent snapshot exposes no mutable bearer or generation fields.
/// ```compile_fail
/// use pseudolife_stdio::credentials::CredentialProvider;
/// let mut snapshot = CredentialProvider::new(None, None).unwrap().snapshot().unwrap();
/// snapshot.token = None;
/// ```
#[derive(Clone)]
pub struct CredentialSnapshot {
    token: Option<String>,
    generation: Generation,
}
impl CredentialSnapshot {
    pub fn token(&self) -> Option<&str> {
        self.token.as_deref()
    }
    pub fn generation(&self) -> &Generation {
        &self.generation
    }
}
impl fmt::Debug for CredentialSnapshot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CredentialSnapshot")
            .field("generation", &self.generation)
            .finish_non_exhaustive()
    }
}
struct Source {
    token: Option<String>,
    path: Option<PathBuf>,
    generation: Generation,
    lock: Mutex<()>,
}
#[derive(Clone)]
pub struct CredentialProvider(Arc<Source>);
impl fmt::Debug for CredentialProvider {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CredentialProvider")
            .field(
                "source",
                &if self.path().is_some() {
                    "file"
                } else {
                    "static"
                },
            )
            .finish()
    }
}
static NEXT_GENERATION: AtomicU64 = AtomicU64::new(1);
impl CredentialProvider {
    pub fn new(token: Option<String>, path: Option<PathBuf>) -> Result<Self, CredentialError> {
        if token.is_some() && path.is_some() {
            return Err(CredentialError("configure exactly one credential source"));
        }
        if let Some(token) = &token {
            decode_token(token.as_bytes())?;
        }
        let path = path
            .map(|p| {
                if p.as_os_str().is_empty() {
                    return Err(CredentialError("configured credential file path is empty"));
                }
                absolute_expanded(&p)
                    .map_err(|_| CredentialError("configured credential file is unavailable"))
            })
            .transpose()?;
        Ok(Self(Arc::new(Source {
            token,
            path,
            generation: Generation(GenerationMarker::Static(
                NEXT_GENERATION.fetch_add(1, Ordering::Relaxed),
            )),
            lock: Mutex::new(()),
        })))
    }
    pub fn from_environment() -> Result<Self, CredentialError> {
        Self::from_lookup(|key| std::env::var_os(key))
    }
    pub fn from_lookup(
        mut lookup: impl FnMut(&str) -> Option<std::ffi::OsString>,
    ) -> Result<Self, CredentialError> {
        if let Some(path) = lookup("PSEUDOLIFE_MCP_TOKEN_FILE") {
            return Self::new(None, Some(path.into()));
        }
        let token = lookup("PSEUDOLIFE_MCP_TOKEN")
            .map(|v| {
                v.into_string().map_err(|_| {
                    CredentialError("credential file contains an invalid bearer token")
                })
            })
            .transpose()?
            .filter(|v| !v.is_empty());
        Self::new(token, None)
    }
    pub fn path(&self) -> Option<&Path> {
        self.0.path.as_deref()
    }
    pub fn snapshot(&self) -> Result<CredentialSnapshot, CredentialError> {
        self.snapshot_checked(|_| Ok(()))
    }
    /// Additional lease admission runs after private-file checks, before decoding.
    pub(crate) fn snapshot_checked(
        &self,
        check: fn(&[u8]) -> Result<(), CredentialError>,
    ) -> Result<CredentialSnapshot, CredentialError> {
        let _guard = self
            .0
            .lock
            .lock()
            .map_err(|_| CredentialError("configured credential file is unavailable"))?;
        match self.path() {
            Some(path) => open_token_checked(path, check),
            None => Ok(CredentialSnapshot {
                token: self.0.token.clone(),
                generation: self.0.generation.clone(),
            }),
        }
    }
    /// Invoke at each operation boundary, including after upstream cleanup.
    pub fn require_current(&self, snapshot: &CredentialSnapshot) -> Result<(), CredentialError> {
        if self.snapshot()?.generation != snapshot.generation {
            return Err(CredentialError("credential generation changed"));
        }
        Ok(())
    }
}

pub fn decode_token(data: &[u8]) -> Result<String, CredentialError> {
    let invalid = CredentialError("credential file contains an invalid bearer token");
    if data.is_empty() || data.len() > MAX_TOKEN_BYTES {
        return Err(invalid);
    }
    let token = std::str::from_utf8(data)
        .map_err(|_| invalid)?
        .trim_end_matches(['\r', '\n']);
    if token.is_empty() || token.chars().any(|c| c.is_whitespace() || c < '\u{20}') {
        return Err(invalid);
    }
    Ok(token.to_owned())
}

pub(crate) fn absolute_expanded(path: &Path) -> io::Result<PathBuf> {
    let expanded = expand_user(path)?;
    // On Windows this also resolves drive-relative paths through the OS,
    // matching os.path.abspath instead of merely joining the current directory.
    let absolute = std::path::absolute(expanded)?;
    let mut clean = PathBuf::new();
    for component in absolute.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                clean.pop();
            }
            _ => clean.push(component.as_os_str()),
        }
    }
    Ok(clean)
}

pub(crate) fn expand_user(path: &Path) -> io::Result<PathBuf> {
    #[cfg(windows)]
    let home = std::env::var_os("USERPROFILE")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOMEPATH").map(|path| {
                PathBuf::from(std::env::var_os("HOMEDRIVE").unwrap_or_default()).join(path)
            })
        });
    #[cfg(unix)]
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .or_else(dirs::home_dir);
    expand_user_with(
        path,
        home.as_deref(),
        std::env::var_os("USERNAME").as_deref(),
        cfg!(windows),
        |name| {
            #[cfg(unix)]
            {
                unix_accounts::home_by_name(name)
            }
            #[cfg(windows)]
            {
                let _ = name;
                None
            }
        },
    )
}

fn expand_user_with(
    path: &Path,
    home: Option<&Path>,
    current_user: Option<&std::ffi::OsStr>,
    windows: bool,
    mut lookup: impl FnMut(&str) -> Option<PathBuf>,
) -> io::Result<PathBuf> {
    let mut components = path.components();
    let Some(first) = components.next().and_then(|c| c.as_os_str().to_str()) else {
        return Ok(path.to_path_buf());
    };
    let Some(name) = first.strip_prefix('~') else {
        return Ok(path.to_path_buf());
    };
    let root = if name.is_empty() {
        home.map(Path::to_path_buf)
    } else if !windows {
        lookup(name)
    } else {
        home.and_then(|home| {
            if current_user == Some(std::ffi::OsStr::new(name)) {
                Some(home.to_path_buf())
            } else if current_user.is_some() && home.file_name() == current_user {
                home.parent().map(|parent| parent.join(name))
            } else {
                None
            }
        })
    }
    .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
    Ok(root.join(components.as_path()))
}

fn is_redirect(info: &Metadata) -> bool {
    if info.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if info.file_attributes() & 0x400 != 0 {
            return true;
        }
    }
    false
}
fn inspect_redirect(path: &Path) -> io::Result<bool> {
    #[cfg(windows)]
    {
        windows_security::inspect_redirect(path)
    }
    #[cfg(not(windows))]
    {
        fs::symlink_metadata(path).map(|info| is_redirect(&info))
    }
}
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
fn open_failure(error: io::Error) -> CredentialError {
    CredentialError(if error.kind() == io::ErrorKind::NotFound {
        "configured credential file is missing"
    } else {
        "configured credential file is unavailable"
    })
}
#[cfg(test)]
fn open_token(path: &Path) -> Result<CredentialSnapshot, CredentialError> {
    open_token_checked(path, |_| Ok(()))
}
fn open_token_checked(
    path: &Path,
    check: fn(&[u8]) -> Result<(), CredentialError>,
) -> Result<CredentialSnapshot, CredentialError> {
    reject_ancestor_redirects(path)?;
    if inspect_redirect(path).map_err(open_failure)? {
        return Err(CredentialError(
            "credential file must be a private regular file",
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };
        // Python's CRT os.open does not grant delete sharing. Retain that
        // lifetime: a concurrent replacement waits until this snapshot closes.
        options
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE);
    }
    let mut attempt = 0;
    let file = loop {
        match options.open(path) {
            Ok(file) => break file,
            // Python maps Windows sharing/lock violations to PermissionError;
            // Rust 1.94 leaves ERROR_SHARING_VIOLATION as Uncategorized.
            Err(error)
                if cfg!(windows)
                    && (error.kind() == io::ErrorKind::PermissionDenied
                        || matches!(error.raw_os_error(), Some(32 | 33)))
                    && attempt < 19 =>
            {
                attempt += 1;
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            Err(error) => return Err(open_failure(error)),
        }
    };
    snapshot_file_checked(file, check)
}
#[cfg(test)]
fn snapshot_file(file: File) -> Result<CredentialSnapshot, CredentialError> {
    snapshot_file_checked(file, |_| Ok(()))
}
fn snapshot_file_checked(
    file: File,
    check: fn(&[u8]) -> Result<(), CredentialError>,
) -> Result<CredentialSnapshot, CredentialError> {
    let info = file.metadata().map_err(open_failure)?;
    if !info.is_file() || is_redirect(&info) {
        return Err(CredentialError(
            "credential file must be a private regular file",
        ));
    }
    #[cfg(unix)]
    let identity = {
        use std::os::unix::fs::MetadataExt;
        if info.nlink() > 1 {
            return Err(CredentialError(
                "credential file must be a private regular file",
            ));
        }
        if info.uid() != rustix::process::geteuid().as_raw() || info.mode() & 0o077 != 0 {
            return Err(CredentialError("credential file must be owner-only"));
        }
        vec![
            info.dev() as u128,
            info.ino() as u128,
            info.size() as u128,
            info.mtime() as u128,
            info.mtime_nsec() as u128,
            info.ctime() as u128,
            info.ctime_nsec() as u128,
        ]
    };
    #[cfg(windows)]
    let identity = windows_security::validate(&file)?;
    let mut data = Vec::with_capacity(MAX_TOKEN_BYTES + 1);
    file.take((MAX_TOKEN_BYTES + 1) as u64)
        .read_to_end(&mut data)
        .map_err(open_failure)?;
    check(&data)?;
    let token = decode_token(&data)?;
    Ok(CredentialSnapshot {
        token: Some(token),
        generation: Generation(GenerationMarker::File {
            identity,
            digest: Sha256::digest(&data).into(),
        }),
    })
}

// Native Windows inspection is confined to this module. The crate's unsafe
// policy must explicitly authorize it before this module is integrated.
#[cfg(windows)]
#[allow(unsafe_code)]
pub(crate) mod windows_security {
    use super::*;
    use std::{
        ffi::c_void,
        mem,
        os::windows::{ffi::OsStrExt, io::AsRawHandle},
        ptr,
    };
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE, LocalFree},
        Security::{
            self,
            Authorization::{
                ConvertStringSecurityDescriptorToSecurityDescriptorW, GetSecurityInfo,
                SE_FILE_OBJECT, SetSecurityInfo,
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

    struct Descriptor(*mut c_void);
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
            if Security::GetSecurityDescriptorDacl(
                descriptor,
                &mut present,
                &mut dacl,
                &mut defaulted,
            ) == 0
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
    #[cfg(test)]
    mod auth_private_state {
        include!("../tests/auth_windows_private_state/mod.rs");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn test_named_user_expansion_matches_python_without_reading_live_homes() {
        let home = Path::new("/fixture/profiles/current");
        let current = std::ffi::OsStr::new("current");
        let lookup = |name: &str| (name == "other").then(|| PathBuf::from("/fixture/other-home"));
        assert_eq!(
            expand_user_with(
                Path::new("~/token"),
                Some(home),
                Some(current),
                false,
                lookup
            )
            .unwrap(),
            home.join("token")
        );
        assert_eq!(
            expand_user_with(
                Path::new("~other/token"),
                Some(home),
                Some(current),
                false,
                lookup
            )
            .unwrap(),
            PathBuf::from("/fixture/other-home/token")
        );
        assert!(
            expand_user_with(
                Path::new("~missing/token"),
                Some(home),
                Some(current),
                false,
                lookup
            )
            .is_err()
        );
        assert_eq!(
            expand_user_with(
                Path::new("~current/token"),
                Some(home),
                Some(current),
                true,
                lookup
            )
            .unwrap(),
            home.join("token")
        );
        assert_eq!(
            expand_user_with(
                Path::new("~other/token"),
                Some(home),
                Some(current),
                true,
                lookup
            )
            .unwrap(),
            PathBuf::from("/fixture/profiles/other/token")
        );
        assert!(
            expand_user_with(
                Path::new("~other/token"),
                Some(Path::new("/fixture/unrelated")),
                Some(current),
                true,
                lookup
            )
            .is_err()
        );
    }
    #[cfg(unix)]
    #[test]
    fn test_atomic_replacement_after_open_retains_private_snapshot() {
        let root = std::env::temp_dir().join(format!(
            "pseudolife-auth-open-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir(&root).unwrap();
        let path = root.join("token");
        let replacement = root.join("replacement");
        fs::write(&path, b"old-fixture-token").unwrap();
        fs::write(&replacement, b"new-fixture-token").unwrap();
        for path in [&path, &replacement] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
        }
        let file = File::open(&path).unwrap();
        fs::rename(replacement, &path).unwrap();
        let old = snapshot_file(file).unwrap();
        let new = open_token(&path).unwrap();
        assert!(old.token.as_deref() == Some("old-fixture-token"));
        assert!(new.token.as_deref() == Some("new-fixture-token"));
        assert_ne!(old.generation, new.generation);
        fs::remove_dir_all(root).unwrap();
    }
}
