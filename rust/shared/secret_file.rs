//! The lite bank's superuser password file (`<data_dir>/embedded_pg.secret`):
//! created owner-only and read back with the owner-only checks the oracle's
//! token files get (`credentials._write_token_file` and `_open_token`).
//!
//! `create` is the one writer both crates use. `read_secret` is the daemon's
//! reader; the shim reads through its credential provider, which applies
//! the oracle's full credential-file validation. Both refuse a file another
//! account could write, a link in its place and a link among its ancestors.
//! The including module provides `SecretStore` (`super::embedded`) and, on
//! Windows, `super::windows_security`.

use std::{
    fs, io,
    io::{Read, Write},
    path::Path,
};

/// The oracle's `MAX_TOKEN_BYTES`.
const MAX_SECRET_BYTES: u64 = 4096;

/// A fresh 64-hex secret, owner-only at `path`, which must not exist: it is
/// written to a temporary file beside `path`, made owner-only before a byte
/// is written, synced and renamed into place, so a crash leaves no partial
/// secret. Callers hold the lite tier's start lock, so no other writer
/// races the existence check.
pub fn create(path: &Path) -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| e.to_string())?;
    let secret: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    reject_ancestor_redirects(path)?;
    let parent = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    match fs::symlink_metadata(path) {
        Ok(_) => return Err("a secret file already exists".to_owned()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.to_string()),
    }
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut tag = [0u8; 8];
    getrandom::fill(&mut tag).map_err(|e| e.to_string())?;
    let tag: String = tag.iter().map(|b| format!("{b:02x}")).collect();
    let temporary = parent.join(format!(".{name}.{tag}"));
    let mut file = private_file(&temporary).map_err(|e| e.to_string())?;
    let written = (|| {
        make_private(&file)?;
        file.write_all(secret.as_bytes())
            .and_then(|()| file.sync_all())
            .map_err(|e| e.to_string())?;
        drop(file);
        fs::rename(&temporary, path).map_err(|e| e.to_string())?;
        if read_secret(path)?.as_deref() != Some(secret.as_str()) {
            return Err("credential file validation failed".to_owned());
        }
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    if written.is_err() && read_secret(path).ok().flatten().as_deref() == Some(secret.as_str()) {
        // Renamed into place but failed afterwards: this call created it.
        let _ = fs::remove_file(path);
    }
    written.map(|()| secret)
}

/// The secret at `path`: `None` when there is no file, an error when the
/// file is not one owner-only regular file holding one token.
pub fn read_secret(path: &Path) -> Result<Option<String>, String> {
    reject_ancestor_redirects(path)?;
    let info = match fs::symlink_metadata(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("credential file is unavailable: {e}")),
        Ok(info) => info,
    };
    if !info.file_type().is_file() {
        return Err("credential file must be a private regular file".to_owned());
    }
    let file = open_no_follow(path).map_err(|e| format!("credential file is unavailable: {e}"))?;
    check_private(&file)?;
    let mut data = Vec::new();
    file.take(MAX_SECRET_BYTES + 1)
        .read_to_end(&mut data)
        .map_err(|e| format!("credential file is unavailable: {e}"))?;
    // `_decode_token`: at most MAX_TOKEN_BYTES of UTF-8, trailing line ends
    // dropped, then one token with no whitespace or control characters.
    let invalid = || "credential file contains an invalid bearer token".to_owned();
    if data.is_empty() || data.len() as u64 > MAX_SECRET_BYTES {
        return Err(invalid());
    }
    let text = std::str::from_utf8(&data).map_err(|_| invalid())?;
    let token = text.trim_end_matches(['\r', '\n']);
    if token.is_empty()
        || token
            .chars()
            .any(|c| c.is_whitespace() || (c as u32) < 0x20)
    {
        return Err(invalid());
    }
    Ok(Some(token.to_owned()))
}

/// A symlink or junction in any ancestor is refused, outermost first; an
/// ancestor that cannot be inspected fails closed.
fn reject_ancestor_redirects(path: &Path) -> Result<(), String> {
    #[cfg(windows)]
    {
        super::windows_security::reject_ancestor_redirects(path).map_err(|e| e.0.to_owned())
    }
    #[cfg(not(windows))]
    {
        let ancestors: Vec<_> = path
            .parent()
            .into_iter()
            .flat_map(Path::ancestors)
            .collect();
        for ancestor in ancestors.into_iter().rev() {
            match fs::symlink_metadata(ancestor) {
                Ok(info) if info.file_type().is_symlink() => {
                    return Err("credential path must not contain redirects".to_owned());
                }
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(_) => {
                    return Err("credential path ancestors cannot be inspected".to_owned());
                }
            }
        }
        Ok(())
    }
}

fn private_file(path: &Path) -> io::Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // Read/write plus WRITE_DAC | WRITE_OWNER, so the new handle can be
        // made owner-only before a byte is written.
        options.access_mode(0x0012019f | 0x000c0000);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)
}

fn make_private(file: &fs::File) -> Result<(), String> {
    #[cfg(windows)]
    {
        super::windows_security::make_private(file).map_err(|e| e.0.to_owned())?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        // The umask may have narrowed 0o600 only; set it exactly.
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|e| e.to_string())?;
    }
    check_private(file)
}

fn open_no_follow(path: &Path) -> io::Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // FILE_FLAG_OPEN_REPARSE_POINT: open a link itself, never its target.
        options.custom_flags(0x0020_0000);
    }
    // A scanner briefly holding a just-written file open on Windows fails
    // the open with a sharing violation; the oracle retries 20 times at
    // 5 ms (`credentials._open_token`), and so does this.
    let mut attempt = 0;
    loop {
        match options.open(path) {
            Err(e)
                if cfg!(windows)
                    && attempt < 19
                    && (e.kind() == io::ErrorKind::PermissionDenied
                        || matches!(e.raw_os_error(), Some(32 | 33))) =>
            {
                attempt += 1;
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            other => return other,
        }
    }
}

/// Owner-only: on Windows the handle's owner and DACL
/// (`windows_security::validate`); elsewhere this account owns it and no
/// group or other permission bit is set.
fn check_private(file: &fs::File) -> Result<(), String> {
    #[cfg(windows)]
    {
        super::windows_security::validate(file)
            .map(drop)
            .map_err(|e| e.0.to_owned())
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let info = file.metadata().map_err(|e| e.to_string())?;
        if !info.is_file() || info.nlink() != 1 {
            // A hard link is a second name another directory could hold.
            return Err("credential file must be a private regular file".to_owned());
        }
        if info.uid() != rustix::process::geteuid().as_raw() {
            return Err("credential file is owned by another account".to_owned());
        }
        if info.mode() & 0o077 != 0 {
            return Err("credential file must be owner-only".to_owned());
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "secret_file_tests.rs"]
mod tests;
