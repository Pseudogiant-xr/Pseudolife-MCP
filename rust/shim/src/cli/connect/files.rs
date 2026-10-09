//! `client_config`'s private file IO: JSON reads, owner-only atomic writes,
//! backups beside the file, and `write_token_file`.
use super::Defer;
use super::pyjson::{self, Dict, J};
use std::{
    fs,
    io::{self, Write},
    path::Path,
};

pub(super) enum Read {
    Missing,
    Error(String),
    Ok(Dict),
}

/// A failure inside a write, named as Python names it.
#[derive(Debug)]
pub(super) enum Fail {
    /// `client_config.HelperError`: its own message is shown.
    Helper(String),
    /// Anything else: only the exception's class name is shown.
    Name(&'static str),
}

/// The Python exception class an OS error becomes (CPython's errno and
/// Windows error maps for the cases a config write meets).
pub(super) fn os_error_name(error: &io::Error) -> &'static str {
    let code = error.raw_os_error();
    let errno = if cfg!(windows) {
        match code {
            Some(2 | 3 | 15 | 18 | 53 | 123 | 161 | 206) => 2,
            Some(5 | 19 | 21 | 32 | 33 | 65 | 108 | 4390) => 13,
            Some(80 | 183) => 17,
            Some(267) => 20,
            _ => 0,
        }
    } else {
        code.unwrap_or(0)
    };
    match errno {
        1 | 13 => "PermissionError",
        2 => "FileNotFoundError",
        17 => "FileExistsError",
        20 => "NotADirectoryError",
        21 if !cfg!(windows) => "IsADirectoryError",
        _ => "OSError",
    }
}

fn name(path: &Path) -> Result<&str, Defer> {
    path.file_name().and_then(|n| n.to_str()).ok_or(Defer)
}

/// `client_config._load_json`.
pub(super) fn load_json(path: &Path) -> Result<Read, Defer> {
    if fs::metadata(path).is_err() {
        return Ok(Read::Ok(Vec::new()));
    }
    let unreadable = format!("{} is not readable JSON; fix it, then re-run", name(path)?);
    let Ok(bytes) = fs::read(path) else {
        return Ok(Read::Error(unreadable));
    };
    let Ok(text) = String::from_utf8(bytes) else {
        return Ok(Read::Error(unreadable));
    };
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    if pyjson::py_strip(text).is_empty() {
        return Ok(Read::Ok(Vec::new()));
    }
    Ok(match pyjson::loads(text)? {
        None => Read::Error(unreadable),
        Some(J::Dict(data)) => Read::Ok(data),
        Some(_) => Read::Error(format!(
            "{} is not a JSON object; fix it, then re-run",
            name(path)?
        )),
    })
}

/// `connect_cli._read_json`.
pub(super) fn read_json(path: &Path) -> Result<Read, Defer> {
    if fs::metadata(path).is_err() {
        return Ok(Read::Missing);
    }
    load_json(path)
}

/// `connect_cli._bytes`.
pub(super) fn bytes(path: &Path) -> Option<Vec<u8>> {
    fs::read(path).ok()
}

fn suffix() -> String {
    uuid::Uuid::new_v4().simple().to_string()[..8].to_owned()
}

fn private_options() -> fs::OpenOptions {
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
    options
}

fn make_private(file: &fs::File) -> Result<(), Fail> {
    #[cfg(windows)]
    {
        crate::credentials::windows_security::make_private(file)
            .map_err(|_| Fail::Name("CredentialError"))
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| Fail::Name(os_error_name(&error)))
    }
}

/// `client_config._write_private`: an owner-only temporary file beside the
/// (symlink-resolved) target, then an atomic replace.
pub(super) fn write_private(path: &Path, data: &[u8]) -> Result<(), Fail> {
    let path = super::paths::realpath(path).unwrap_or_else(|| path.to_path_buf());
    let parent = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(parent).map_err(|error| Fail::Name(os_error_name(&error)))?;
    let mut temporary = None;
    let mut last = None;
    for _ in 0..16 {
        let candidate = parent.join(format!(".pseudolife-{}", suffix()));
        match private_options().open(&candidate) {
            Ok(file) => {
                temporary = Some((candidate, file));
                break;
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => last = Some(error),
            Err(error) => return Err(Fail::Name(os_error_name(&error))),
        }
    }
    let Some((temporary, mut file)) = temporary else {
        return Err(Fail::Name(last.as_ref().map_or("OSError", os_error_name)));
    };
    let result = (|| {
        make_private(&file)?;
        file.write_all(data)
            .and_then(|()| file.sync_all())
            .map_err(|error| Fail::Name(os_error_name(&error)))?;
        drop(file);
        fs::rename(&temporary, &path).map_err(|error| Fail::Name(os_error_name(&error)))
    })();
    if fs::symlink_metadata(&temporary).is_ok() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

/// `client_config._backup` for a file that exists.
pub(super) fn backup(path: &Path) -> Result<String, Fail> {
    let stamp = chrono::Utc::now().format("%Y%m%d-%H%M%S-%6f");
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or(Fail::Name("OSError"))?;
    let target = path.with_file_name(format!("{name}.bak-pseudolife-{stamp}"));
    let data = fs::read(path).map_err(|error| Fail::Name(os_error_name(&error)))?;
    write_private(&target, &data)?;
    target
        .to_str()
        .map(str::to_owned)
        .ok_or(Fail::Name("OSError"))
}

/// `client_config._write_json`.
pub(super) fn write_json(path: &Path, data: &Dict) -> Result<(), Fail> {
    let mut text = pyjson::dumps(&J::Dict(data.clone()), false);
    text.push('\n');
    write_private(path, text.as_bytes())
}

/// How `write_token_file` refused, in connect's terms.
pub(super) enum TokenFail {
    /// HelperError: connect prints `{message}; nothing was changed`.
    Helper(String),
    /// OSError or CredentialError: connect prints the class name.
    Name(&'static str),
}

fn already_exists(target: &Path) -> TokenFail {
    TokenFail::Helper(format!(
        "{} already exists; the installer creates a token file but never replaces one",
        target.display()
    ))
}

/// `client_config.write_token_file(path, stdin)` with stdin's first bytes.
pub(super) fn write_token_file(target: &Path, data: &[u8]) -> Result<(), TokenFail> {
    if fs::symlink_metadata(target).is_ok() {
        return Err(already_exists(target));
    }
    let Ok(text) = std::str::from_utf8(data) else {
        return Err(TokenFail::Helper(
            "the token read on stdin is not UTF-8 text".to_owned(),
        ));
    };
    let token = text
        .strip_prefix('\u{feff}')
        .unwrap_or(text)
        .trim_end_matches(['\r', '\n']);
    if crate::credentials::decode_token(token.as_bytes()).is_err() {
        return Err(TokenFail::Helper(
            "the token read on stdin is empty, too long, or holds whitespace or control characters"
                .to_owned(),
        ));
    }
    crate::credentials::reject_ancestor_redirects(target)
        .map_err(|_| TokenFail::Name("CredentialError"))?;
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|error| TokenFail::Name(os_error_name(&error)))?;
    }
    let mut file = match private_options().open(target) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return Err(already_exists(target));
        }
        Err(error) => return Err(TokenFail::Name(os_error_name(&error))),
    };
    let written = (|| {
        make_private(&file).map_err(|_| TokenFail::Name("CredentialError"))?;
        #[cfg(windows)]
        crate::credentials::windows_security::validate(&file)
            .map_err(|_| TokenFail::Name("CredentialError"))?;
        file.write_all(token.as_bytes())
            .and_then(|()| file.sync_all())
            .map_err(|error| TokenFail::Name(os_error_name(&error)))?;
        drop(file);
        let read = crate::credentials::CredentialProvider::new(None, Some(target.to_path_buf()))
            .and_then(|provider| provider.snapshot())
            .map_err(|_| TokenFail::Name("CredentialError"))?;
        if read.token() != Some(token) {
            return Err(TokenFail::Name("CredentialError"));
        }
        Ok(())
    })();
    if written.is_err() {
        let _ = fs::remove_file(target);
    }
    written
}
