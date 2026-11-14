//! Preserve the actual displaced configuration, including concurrent writes.
use super::*;
use registrations::read_optional;
use std::io::Write;

pub fn write_config(path: &Path, before: &[u8], after: &[u8]) -> Result<PathBuf> {
    write_config_with(path, before, after, || {})
}

pub(super) fn write_config_with(
    path: &Path,
    before: &[u8],
    after: &[u8],
    before_replace: impl FnOnce(),
) -> Result<PathBuf> {
    let target = if path.exists() {
        fs::canonicalize(path).map_err(io_error)?
    } else {
        path.to_owned()
    };
    crate::credentials::reject_ancestor_redirects(&target)
        .map_err(|_| "client config target is redirected")?;
    let parent = target.parent().ok_or("client config has no parent")?;
    if !parent.is_dir() {
        crate::board::state::private_dir(parent).map_err(str::to_owned)?;
    }
    let meta = match fs::metadata(&target) {
        Ok(meta) => Some(meta),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(io_error(error)),
    };
    // Refuse only what makes this write unsafe: another account's file (its
    // replacement would cross accounts), a redirect (above) or a hard link
    // (the other name would keep the old content, and the backup's privacy
    // would reach it). Who else may write the file is not a reason: the
    // client already reads and trusts it, an account that can write it can
    // already redirect the client, and the edit keeps its owner, group and
    // mode (or ACL) or narrows them, so refusing protected nothing and
    // blocked umask-002 hosts (issue #1545). The displaced copy kept as the
    // backup is made private below.
    #[cfg(unix)]
    if let Some(meta) = &meta {
        use std::os::unix::fs::MetadataExt;
        let shown = target.display().to_string().replace('\'', "'\\''");
        if meta.uid() != rustix::process::getuid().as_raw() {
            return Err(format!(
                "{} is owned by another account, so it was not replaced; run this as its owner, or make this user its owner: sudo chown \"$USER\" '{shown}'",
                target.display()
            ));
        }
        if meta.nlink() != 1 {
            return Err(format!(
                "{} has another hard link, which would keep the old content, so it was not replaced; replace the links with one file (cp '{shown}' '{shown}.copy' && mv '{shown}.copy' '{shown}'), then retry",
                target.display()
            ));
        }
    }
    #[cfg(windows)]
    if meta.is_some() {
        use std::os::windows::fs::OpenOptionsExt;
        let opened = fs::OpenOptions::new()
            .read(true)
            .custom_flags(windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&target)
            .map_err(io_error)?;
        windows_config_security::validate(&opened, true).map_err(|error| {
            let shown = display_path(&target);
            format!("{shown}: {}", error.replace("<path>", &shown))
        })?;
    }
    let recovery = parent.join(format!(".client-native-backup-{}", uuid::Uuid::new_v4()));
    crate::board::state::private_dir(&recovery).map_err(str::to_owned)?;
    let observed = recovery.join("observed");
    crate::board::state::atomic_create(&observed, before).map_err(str::to_owned)?;
    let pending = recovery.join("pending");
    let previous = recovery.join("previous");
    let mut opened = crate::board::state::create(&pending).map_err(str::to_owned)?;
    opened
        .file
        .write_all(after)
        .and_then(|_| opened.file.sync_all())
        .map_err(io_error)?;
    #[cfg(unix)]
    if let Some(meta) = &meta {
        keep_group_and_mode(&opened.file, &target, meta).map_err(io_error)?;
    }
    drop(opened);
    let result = (|| {
        if read_optional(&target)?.as_deref().unwrap_or_default() != before {
            return Err(
                "client configuration changed during the edit; newer content was preserved".into(),
            );
        }
        before_replace();
        let backup = if meta.is_some() {
            replace_preserving(&target, &pending, &previous).map_err(io_error)?;
            if read_optional(&previous)?.as_deref() != Some(before) {
                return Err("client configuration changed during replacement; the actual displaced file was preserved; review the recovery copy before retrying".into());
            }
            // The displaced file kept the original's mode or ACL, which may
            // let other accounts write it; the backup is this user's alone.
            // Best effort: a backup left as it was is kept, never pruned.
            make_backup_private(&previous);
            previous
        } else {
            // Atomic create-if-absent: a concurrent new file is never replaced.
            fs::hard_link(&pending, &target).map_err(io_error)?;
            observed.clone()
        };
        if read_optional(&target)?.as_deref() != Some(after) {
            return Err(
                "client configuration changed after the edit; newer content was preserved".into(),
            );
        }
        Ok(backup)
    })();
    if let Ok(backup) = &result {
        // Retain the returned backup, including the actual displaced file.
        // Failed or conflicted publications keep every recovery artifact.
        for redundant in [&observed, &pending] {
            if redundant != backup {
                let _ = fs::remove_file(redundant);
            }
        }
        complete_backup(&target, backup);
    }
    result.map_err(|error: String| format!("{error}; recovery files: {}", recovery.display()))
}

/// The replacement keeps the original's group as well as its mode: a mode
/// copied onto another group (the new file's default) would widen who may
/// write it. When the group cannot be kept, or a POSIX ACL widened the group
/// bits, group write is dropped, so the edit only ever narrows access.
#[cfg(unix)]
fn keep_group_and_mode(file: &fs::File, target: &Path, meta: &fs::Metadata) -> io::Result<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let mut mode = meta.mode() & 0o7777;
    let kept = rustix::fs::fchown(file, None, Some(rustix::fs::Gid::from_raw(meta.gid()))).is_ok();
    if !kept || has_access_acl(target) {
        mode &= !0o020;
    }
    file.set_permissions(fs::Permissions::from_mode(mode))
}
/// Whether the file carries a POSIX access ACL (Linux), whose mask the
/// group bits show; macOS ACLs are not copied either, and are not detected.
#[cfg(unix)]
fn has_access_acl(path: &Path) -> bool {
    #[cfg(target_os = "linux")]
    {
        // A zero-length read returns the attribute's size without its value.
        let mut empty: [u8; 0] = [];
        rustix::fs::getxattr(path, "system.posix_acl_access", &mut empty[..]).is_ok_and(|n| n > 0)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = path;
        false
    }
}

#[cfg(windows)]
/// A path as a user types it: Windows canonical paths drop the verbatim
/// prefix (`\\?\`), which `takeown` and most tools do not accept.
fn display_path(path: &Path) -> String {
    let text = path.display().to_string();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return format!(r"\\{rest}");
    }
    text.strip_prefix(r"\\?\").unwrap_or(&text).to_owned()
}

fn make_backup_private(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        // A link made after the target's check must not lose access through
        // the backup: only a sole name is narrowed (Codex review).
        if fs::symlink_metadata(path).is_ok_and(|m| m.is_file() && m.nlink() == 1) {
            let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_GENERIC_READ, WRITE_DAC, WRITE_OWNER,
        };
        // make_private sets the owner as well as the DACL.
        if let Ok(file) = fs::OpenOptions::new()
            .access_mode(FILE_GENERIC_READ | WRITE_DAC | WRITE_OWNER)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
        {
            let _ = crate::credentials::windows_security::make_private(&file);
        }
    }
}

#[derive(PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct CompletedBackup {
    format: u8,
    target: PathBuf,
    backup: String,
    backup_sha256: String,
    completed_at: String,
}

const COMPLETION: &str = "complete.json";

fn complete_backup(target: &Path, backup: &Path) {
    let Some(directory) = backup.parent() else {
        return;
    };
    let Ok(target) = fs::canonicalize(target) else {
        return;
    };
    let Some(digest) = backup_digest(backup) else {
        return;
    };
    let Some(name) = backup.file_name().and_then(|name| name.to_str()) else {
        return;
    };
    let completion = CompletedBackup {
        format: 1,
        target: target.clone(),
        backup: name.into(),
        backup_sha256: digest,
        completed_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Nanos, true),
    };
    let Ok(bytes) = serde_json::to_vec(&completion) else {
        return;
    };
    if crate::board::state::atomic_create(&directory.join(COMPLETION), &bytes).is_ok() {
        prune_completed_backups(&target, directory.parent().unwrap_or(directory));
    }
}

fn backup_digest(path: &Path) -> Option<String> {
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options.open(path).ok()?;
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.len() > 16 * 1024 * 1024 {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != rustix::process::getuid().as_raw()
            || meta.mode() & 0o022 != 0
            || meta.nlink() != 1
        {
            return None;
        }
    }
    #[cfg(windows)]
    windows_config_security::validate(&file, false).ok()?;
    let mut bytes = Vec::new();
    file.take(16 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() > 16 * 1024 * 1024 {
        return None;
    }
    Some(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
    )
}

fn completed_backup(directory: &Path, target: &Path) -> Option<CompletedBackup> {
    crate::credentials::reject_ancestor_redirects(&directory.join(COMPLETION)).ok()?;
    let meta = fs::symlink_metadata(directory).ok()?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != rustix::process::getuid().as_raw() || meta.mode() & 0o077 != 0 {
            return None;
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES,
            READ_CONTROL,
        };
        let opened = fs::OpenOptions::new()
            .read(true)
            .access_mode(FILE_READ_ATTRIBUTES | READ_CONTROL)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(directory)
            .ok()?;
        crate::credentials::windows_security::validate(&opened).ok()?;
    }
    let bytes = crate::board::state::read(&directory.join(COMPLETION), 256 * 1024).ok()?;
    let completion: CompletedBackup = serde_json::from_slice(&bytes).ok()?;
    if completion.format != 1
        || completion.target != target
        || !matches!(completion.backup.as_str(), "previous" | "observed")
    {
        return None;
    }
    let timestamp = chrono::DateTime::parse_from_rfc3339(&completion.completed_at).ok()?;
    if timestamp
        .with_timezone(&chrono::Utc)
        .to_rfc3339_opts(chrono::SecondsFormat::Nanos, true)
        != completion.completed_at
    {
        return None;
    }
    let entries: Vec<_> = fs::read_dir(directory)
        .ok()?
        .take(3)
        .collect::<io::Result<_>>()
        .ok()?;
    if entries.len() != 2
        || entries.iter().any(|entry| {
            !entry.file_type().is_ok_and(|kind| kind.is_file())
                || (entry.file_name() != COMPLETION
                    && entry.file_name() != completion.backup.as_str())
        })
        || backup_digest(&directory.join(&completion.backup))? != completion.backup_sha256
    {
        return None;
    }
    Some(completion)
}

fn prune_completed_backups(target: &Path, parent: &Path) {
    let Ok(entries) = fs::read_dir(parent) else {
        return;
    };
    let mut completed = Vec::new();
    // Bound incidental cleanup work even in a directory full of unrelated files.
    for (index, entry) in entries.enumerate() {
        if index >= 4096 {
            return;
        }
        let Ok(entry) = entry else { return };
        let name = entry.file_name();
        let Some(suffix) = name
            .to_str()
            .and_then(|name| name.strip_prefix(".client-native-backup-"))
        else {
            continue;
        };
        if !uuid::Uuid::parse_str(suffix).is_ok_and(|id| id.to_string() == suffix) {
            continue;
        }
        if let Some(completion) = completed_backup(&entry.path(), target) {
            completed.push((completion, entry.path()));
        }
    }
    completed.sort_by(|(left, left_path), (right, right_path)| {
        left.completed_at
            .cmp(&right.completed_at)
            .then_with(|| left_path.cmp(right_path))
    });
    let remove = completed.len().saturating_sub(3);
    for (observed, directory) in completed.into_iter().take(remove) {
        // Recheck the exact completion and backup before deleting only known
        // files. Extra, modified or unreadable recovery evidence is retained.
        let Some(completion) = completed_backup(&directory, target) else {
            continue;
        };
        if completion != observed {
            continue;
        }
        let removing = parent.join(format!(".client-native-pruning-{}", uuid::Uuid::new_v4()));
        if fs::rename(&directory, &removing).is_err() {
            continue;
        }
        // Quarantine first so a replaced source directory cannot redirect
        // deletion elsewhere. An unexpected or unreadable quarantine survives.
        if completed_backup(&removing, target).as_ref() != Some(&observed) {
            continue;
        }
        if fs::remove_file(removing.join(&completion.backup)).is_ok()
            && fs::remove_file(removing.join(COMPLETION)).is_ok()
        {
            let _ = fs::remove_dir(removing);
        }
    }
}

// Client applications normally inherit user, SYSTEM and Administrators ACEs;
// credential files have a stricter, separate owner-only contract.
#[cfg(windows)]
#[allow(unsafe_code)]
mod windows_config_security {
    use super::*;
    use std::{ffi::c_void, mem, os::windows::io::AsRawHandle, ptr};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, GENERIC_ALL, GENERIC_WRITE, HANDLE, LocalFree},
        Security::{
            self,
            Authorization::{GetSecurityInfo, SE_FILE_OBJECT},
        },
        Storage::FileSystem::{
            BY_HANDLE_FILE_INFORMATION, DELETE, FILE_APPEND_DATA, FILE_ATTRIBUTE_DIRECTORY,
            FILE_ATTRIBUTE_REPARSE_POINT, FILE_WRITE_ATTRIBUTES, FILE_WRITE_DATA, FILE_WRITE_EA,
            GetFileInformationByHandle, WRITE_DAC, WRITE_OWNER,
        },
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };

    struct Descriptor(*mut c_void);
    impl Drop for Descriptor {
        fn drop(&mut self) {
            // SAFETY: GetSecurityInfo allocated this descriptor with LocalAlloc.
            unsafe { LocalFree(self.0) };
        }
    }
    struct Token(HANDLE);
    impl Drop for Token {
        fn drop(&mut self) {
            // SAFETY: this guard owns the OpenProcessToken handle.
            unsafe { CloseHandle(self.0) };
        }
    }

    fn token_information(
        token: HANDLE,
        class: Security::TOKEN_INFORMATION_CLASS,
        minimum: usize,
    ) -> Option<Vec<usize>> {
        // SAFETY: the borrowed token remains live; both output buffers are
        // initialized, and the final usize-aligned allocation is size-bounded.
        unsafe {
            let mut needed = 0;
            Security::GetTokenInformation(token, class, ptr::null_mut(), 0, &mut needed);
            if needed < minimum as u32 || needed > 65536 {
                return None;
            }
            let mut buffer = vec![0usize; (needed as usize).div_ceil(mem::size_of::<usize>())];
            (Security::GetTokenInformation(
                token,
                class,
                buffer.as_mut_ptr().cast(),
                needed,
                &mut needed,
            ) != 0)
                .then_some(buffer)
        }
    }

    /// A regular file without links or redirects, owned by this user (or by
    /// the token's default owner). `others_may_write` accepts an ACL that
    /// lets other accounts write it: true for a client's own configuration,
    /// which the client already trusts as it is; false for this tool's own
    /// backups, which must be private.
    pub(super) fn validate(file: &fs::File, others_may_write: bool) -> Result<()> {
        let invalid = if others_may_write {
            "its owner and permissions could not be read, so it was not replaced; check that this account can read its security settings (icacls \"<path>\"), then retry"
        } else {
            "client configuration must be owned by this user and not writable by other users"
        };
        let not_owner = "it is owned by another account, so it was not replaced; run this as its owner, or take ownership of it as this user (takeown /f \"<path>\")";
        // SAFETY: the borrowed file handle and aligned token buffer remain live
        // throughout inspection. Descriptor owns every ACL/ACE/SID pointer.
        // ACE and SID bounds are checked before their contents are inspected.
        unsafe {
            let handle = file.as_raw_handle() as HANDLE;
            let mut info: BY_HANDLE_FILE_INFORMATION = mem::zeroed();
            if GetFileInformationByHandle(handle, &mut info) == 0
                || info.nNumberOfLinks != 1
                || info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
                    != 0
            {
                return Err(
                    "client configuration must be a regular file without redirects or hard links"
                        .into(),
                );
            }
            let mut owner = ptr::null_mut();
            let mut dacl = ptr::null_mut();
            let mut descriptor = ptr::null_mut();
            let status = GetSecurityInfo(
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
            if status != 0
                || descriptor.is_null()
                || owner.is_null()
                || (dacl.is_null() && !others_may_write)
                || Security::IsValidSid(owner) == 0
                || (!dacl.is_null() && Security::IsValidAcl(dacl) == 0)
            {
                return Err(invalid.into());
            }
            let mut token = ptr::null_mut();
            if OpenProcessToken(GetCurrentProcess(), Security::TOKEN_QUERY, &mut token) == 0 {
                return Err(invalid.into());
            }
            let _token = Token(token);
            let user = token_information(
                token,
                Security::TokenUser,
                mem::size_of::<Security::TOKEN_USER>(),
            )
            .ok_or(invalid)?;
            let default = token_information(
                token,
                Security::TokenOwner,
                mem::size_of::<Security::TOKEN_OWNER>(),
            )
            .ok_or(invalid)?;
            let current = (*user.as_ptr().cast::<Security::TOKEN_USER>()).User.Sid;
            let default_owner = (*default.as_ptr().cast::<Security::TOKEN_OWNER>()).Owner;
            // Elevated clients can create files owned by their token's default
            // owner group. Membership alone never authorizes another owner.
            if current.is_null()
                || Security::IsValidSid(current) == 0
                || default_owner.is_null()
                || Security::IsValidSid(default_owner) == 0
                || (Security::EqualSid(owner, current) == 0
                    && Security::EqualSid(owner, default_owner) == 0)
            {
                return Err(if others_may_write { not_owner } else { invalid }.into());
            }
            if others_may_write {
                // Who else may write it is not a reason to refuse (see
                // write_config_with); nothing below applies.
                return Ok(());
            }
            let acl_start = dacl as usize;
            let acl_end = acl_start + (*dacl).AclSize as usize;
            let mutable = FILE_WRITE_DATA
                | FILE_APPEND_DATA
                | FILE_WRITE_EA
                | FILE_WRITE_ATTRIBUTES
                | DELETE
                | WRITE_DAC
                | WRITE_OWNER
                | GENERIC_WRITE
                | GENERIC_ALL;
            for index in 0..(*dacl).AceCount as u32 {
                let mut ace = ptr::null_mut();
                if Security::GetAce(dacl, index, &mut ace) == 0 || ace.is_null() {
                    return Err(invalid.into());
                }
                let start = ace as usize;
                if start < acl_start + mem::size_of::<Security::ACL>()
                    || start + mem::size_of::<Security::ACE_HEADER>() > acl_end
                {
                    return Err(invalid.into());
                }
                let header = &*ace.cast::<Security::ACE_HEADER>();
                if header.AceSize < mem::size_of::<Security::ACE_HEADER>() as u16
                    || start + header.AceSize as usize > acl_end
                {
                    return Err(invalid.into());
                }
                // Deny ACEs cannot grant write access; inherit-only ACEs do not
                // apply to this file. Unknown effective ACE types fail closed.
                if header.AceType == 1
                    || u32::from(header.AceFlags) & Security::INHERIT_ONLY_ACE != 0
                {
                    continue;
                }
                if header.AceType != 0 || header.AceSize < 16 {
                    return Err(invalid.into());
                }
                let allowed = &*ace.cast::<Security::ACCESS_ALLOWED_ACE>();
                let sid = ace.cast::<u8>().add(8).cast::<c_void>();
                let sid_size = 8 + (*ace.cast::<u8>().add(9) as usize) * 4;
                if 8 + sid_size > header.AceSize as usize || Security::IsValidSid(sid) == 0 {
                    return Err(invalid.into());
                }
                if !others_may_write
                    && allowed.Mask & mutable != 0
                    && Security::EqualSid(owner, sid) == 0
                    && Security::EqualSid(current, sid) == 0
                    && Security::IsWellKnownSid(sid, Security::WinCreatorOwnerRightsSid) == 0
                    && Security::IsWellKnownSid(sid, Security::WinLocalSystemSid) == 0
                    && Security::IsWellKnownSid(sid, Security::WinBuiltinAdministratorsSid) == 0
                {
                    return Err(invalid.into());
                }
            }
            Ok(())
        }
    }
}

#[cfg(test)]
#[path = "config_file_tests.rs"]
mod tests;

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn replace_preserving(target: &Path, pending: &Path, previous: &Path) -> io::Result<()> {
    rustix::fs::renameat_with(
        rustix::fs::CWD,
        pending,
        rustix::fs::CWD,
        target,
        rustix::fs::RenameFlags::EXCHANGE,
    )?;
    // If this rename fails, the displaced file remains in the private pending
    // path. Never delete it or roll back over a newer writer's target.
    fs::rename(pending, previous)
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn replace_preserving(target: &Path, pending: &Path, previous: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    let wide = |path: &Path| -> io::Result<Vec<u16>> {
        let units: Vec<_> = path.as_os_str().encode_wide().collect();
        if units.contains(&0) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "path contains NUL",
            ));
        }
        Ok(units.into_iter().chain([0]).collect())
    };
    let target_w = wide(target)?;
    let previous_w = wide(previous)?;
    // ReplaceFileW resolves inherited target ACEs against the replacement's
    // parent. Replacing directly from the private recovery directory can
    // produce an empty DACL for an ordinary, non-elevated client. A private
    // move beside the target keeps the correct inheritance context. Move the
    // sole pending name before publication: an AV deletion refusal must not
    // leave a changed, hard-linked target that subsequent retries reject.
    let staged = target
        .parent()
        .ok_or_else(|| io::Error::other("configuration target has no parent"))?
        .join(format!(".client-native-pending-{}", uuid::Uuid::new_v4()));
    let staged_w = wide(&staged)?;
    let pending_w = wide(pending)?;
    let move_absent = |from: &[u16], to: &[u16]| -> io::Result<()> {
        // Flags zero prohibit replacing an existing destination and copying
        // across volumes. All names and buffers remain live during the call.
        if unsafe {
            windows_sys::Win32::Storage::FileSystem::MoveFileExW(from.as_ptr(), to.as_ptr(), 0)
        } == 0
        {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    };
    move_absent(&pending_w, &staged_w)?;
    // ReplaceFileW retains the actual displaced file and its attributes. All
    // buffers live for the call; no ACL-merge errors are ignored.
    let replaced = unsafe {
        windows_sys::Win32::Storage::FileSystem::ReplaceFileW(
            target_w.as_ptr(),
            staged_w.as_ptr(),
            previous_w.as_ptr(),
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if replaced != 0 {
        return Ok(());
    }
    let error = io::Error::last_os_error();
    // ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 can leave the original at the backup
    // path. Restore only into an absent target; never overwrite another writer.
    if error.raw_os_error() == Some(1177) {
        let _ = move_absent(&previous_w, &target_w);
    }
    if let Err(cleanup) = move_absent(&staged_w, &pending_w)
        && cleanup.kind() != io::ErrorKind::NotFound
    {
        return Err(io::Error::new(
            error.kind(),
            format!(
                "{error}; replacement staging file retained at {}",
                staged.display()
            ),
        ));
    }
    Err(error)
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn replace_preserving(_: &Path, _: &Path, _: &Path) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "atomic configuration exchange is unavailable",
    ))
}
