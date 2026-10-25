//! The shim's `SecretStore`: a lite bank's superuser password file, created
//! by the shared writer (`shared/secret_file.rs`, which the daemon uses too)
//! and read through the credential code that guards bearer-token files (the
//! oracle uses its token-file writer, `credentials._write_token_file`, and
//! validator, `credentials._open_token`, the same way).
use super::embedded::SecretStore;
use super::secret_file;
use crate::credentials::CredentialProvider;
use std::{fs, io, path::Path};

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

impl SecretStore for CredentialSecrets {
    fn read(&self, path: &Path) -> Result<Option<String>, String> {
        match fs::symlink_metadata(path) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.to_string()),
            Ok(_) => read_back(path).map(Some),
        }
    }

    fn create(&self, path: &Path) -> Result<String, String> {
        let secret = secret_file::create(path)?;
        // The writer read it back through its own checks; the credential
        // provider's must accept it too, or the next start could not read it.
        if read_back(path)? != secret {
            return Err("credential file validation failed".to_owned());
        }
        Ok(secret)
    }
}

#[cfg(test)]
#[path = "embedded_secret_tests.rs"]
mod tests;
