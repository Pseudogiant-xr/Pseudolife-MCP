//! The shim's `SecretStore`: a lite bank's superuser password file, read and
//! created through the credential code that guards bearer-token files (the
//! oracle uses its token-file writer, `credentials._write_token_file`, and
//! validator, `credentials._open_token`, the same way).
use super::embedded::SecretStore;
use crate::credentials::{self, CredentialProvider};
use std::{
    fs, io,
    io::Write,
    path::{Path, PathBuf},
};

pub struct CredentialSecrets;

fn read_back(path: &Path) -> Result<String, String> {
    let provider =
        CredentialProvider::new(None, Some(path.to_path_buf())).map_err(|e| e.to_string())?;
    let snapshot = provider.snapshot().map_err(|e| e.to_string())?;
    snapshot
        .token()
        .map(str::to_owned)
        .ok_or_else(|| "credential file holds no secret".to_owned())
}

fn private_file(path: &PathBuf) -> io::Result<fs::File> {
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
        credentials::windows_security::make_private(file).map_err(|e| e.to_string())?;
        credentials::windows_security::validate(file).map_err(|e| e.to_string())?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        // The umask may have narrowed 0o600 only; set it exactly.
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

impl SecretStore for CredentialSecrets {
    fn read(&self, path: &Path) -> Result<Option<String>, String> {
        match fs::symlink_metadata(path) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.to_string()),
            Ok(_) => read_back(path).map(Some),
        }
    }

    fn create(&self, path: &Path) -> Result<String, String> {
        let mut bytes = [0u8; 32];
        getrandom::fill(&mut bytes).map_err(|e| e.to_string())?;
        let secret: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
        credentials::reject_ancestor_redirects(path).map_err(|e| e.to_string())?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let target = path.to_path_buf();
        let mut file = private_file(&target).map_err(|e| e.to_string())?;
        let written = (|| {
            // Owner-only before a byte is written.
            make_private(&file)?;
            file.write_all(secret.as_bytes())
                .and_then(|()| file.sync_all())
                .map_err(|e| e.to_string())?;
            drop(file);
            if read_back(&target)? != secret {
                return Err("credential file validation failed".to_owned());
            }
            Ok(())
        })();
        if written.is_err() {
            // This call created the file, so removing a half-written one is safe.
            let _ = fs::remove_file(&target);
        }
        written.map(|()| secret)
    }
}

#[cfg(test)]
#[path = "embedded_secret_tests.rs"]
mod tests;
