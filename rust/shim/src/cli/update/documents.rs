//! The documents folder the Rust daemon's `document_ingest` reads from: a
//! host folder the Compose overlays mount read-only at `/documents`
//! (`~/.pseudolife-mcp/documents`). Install and update create it before
//! the daemon starts; when it exists nothing changes.
//!
//! On Unix the folder is `0755`, because the container's daemon account
//! (uid 10001) reads it through the bind mount, inside `~/.pseudolife-mcp`,
//! created `0700` when missing, which keeps other users of the machine out.
//! An existing parent keeps its mode; one that others can enter is named
//! with its fix.

use serde_json::Value;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// Where the overlays mount the folder inside the container.
pub(crate) const TARGET: &str = "/documents";

/// What [`ensure`] did.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Outcome {
    Created,
    Unchanged,
}

/// The host folder bound at `/documents` in a resolved Compose
/// configuration (`docker compose config --format json`), if any.
pub(crate) fn bind_source(config: &Value, service: &str) -> Option<PathBuf> {
    config["services"][service]["volumes"]
        .as_array()?
        .iter()
        .find(|v| {
            v["type"].as_str() == Some("bind")
                && v["target"].as_str().map(|t| t.trim_end_matches('/')) == Some(TARGET)
        })
        .and_then(|v| v["source"].as_str())
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
}

/// The folder the overlays name, `~/.pseudolife-mcp/documents`, as Compose
/// resolves `~` (the home directory of the user running it).
pub(crate) fn default_folder(home: &Path) -> PathBuf {
    home.join(".pseudolife-mcp").join("documents")
}

/// Create `folder` (and a missing parent, owner-only) if it is missing.
/// A path that exists but is not a directory is an error naming it.
pub(crate) fn ensure(folder: &Path) -> io::Result<Outcome> {
    match fs::metadata(folder) {
        Ok(meta) if meta.is_dir() => return Ok(Outcome::Unchanged),
        Ok(_) => {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                format!(
                    "{} exists but is not a folder; move it aside so the documents folder can be created there",
                    folder.display()
                ),
            ));
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    if let Some(parent) = folder.parent()
        && !parent.exists()
    {
        create_dir(parent, 0o700)?;
    }
    create_dir(folder, 0o755)?;
    Ok(Outcome::Created)
}

#[cfg(unix)]
fn create_dir(path: &Path, mode: u32) -> io::Result<()> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true).mode(mode);
    builder.create(path)?;
    // The umask may have cleared bits the mode asked for.
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
}

#[cfg(not(unix))]
fn create_dir(path: &Path, _mode: u32) -> io::Result<()> {
    fs::create_dir_all(path)
}

/// The one-line note for a parent folder other users can enter, or `None`.
#[cfg(unix)]
pub(crate) fn open_parent_note(folder: &Path) -> Option<String> {
    use std::os::unix::fs::PermissionsExt;
    let parent = folder.parent()?;
    let mode = fs::metadata(parent).ok()?.permissions().mode();
    (mode & 0o077 != 0).then(|| {
        format!(
            "{} lets other users of this machine in, so they can read the documents folder too; run chmod 700 {} to keep it to you",
            parent.display(),
            parent.display()
        )
    })
}

#[cfg(not(unix))]
pub(crate) fn open_parent_note(_folder: &Path) -> Option<String> {
    None
}

/// The one-line note for a folder another account owns (Docker creates a
/// missing bind source as root, as an updater older than this folder
/// leaves it), so its user cannot copy documents in, or `None`.
#[cfg(unix)]
pub(crate) fn owner_note(folder: &Path) -> Option<String> {
    use std::os::unix::fs::MetadataExt;
    let owner = fs::metadata(folder).ok()?.uid();
    let me = rustix::process::geteuid().as_raw();
    (owner != me).then(|| {
        format!(
            "{} belongs to another account (uid {owner}), so you cannot copy documents into it; run sudo chown {me} {} once",
            folder.display(),
            folder.display()
        )
    })
}

#[cfg(not(unix))]
pub(crate) fn owner_note(_folder: &Path) -> Option<String> {
    None
}

/// The home directory Compose resolves `~` against when it runs as this
/// user: `HOME` on Unix (where `USERPROFILE` can leak in from Windows, as
/// under WSL), the profile directory on Windows.
pub(crate) fn compose_home() -> PathBuf {
    #[cfg(unix)]
    if let Some(home) = std::env::var_os("HOME").filter(|h| !h.is_empty()) {
        return PathBuf::from(home);
    }
    super::home()
}

/// Why the release cannot mount the documents folder where the running
/// daemon already mounts something else at `/documents`, or `None`: the
/// release overlay comes last and would replace that mount.
pub(crate) fn mount_conflict(config: &Value, running: &Value) -> Option<String> {
    let folder = bind_source(config, super::DAEMON_SERVICE)?;
    let mount = running["Mounts"]
        .as_array()?
        .iter()
        .find(|m| m["Destination"].as_str().map(|d| d.trim_end_matches('/')) == Some(TARGET))?;
    let source = mount["Source"]
        .as_str()
        .or(mount["Name"].as_str())
        .unwrap_or("");
    let same = mount["Type"] == "bind"
        && super::engine::same_bind_source(source, &folder.to_string_lossy());
    (!same).then(|| {
        format!(
            "the running daemon mounts {source} at /documents, where this release mounts the documents folder {}; move that mount to another container path (for example /srv/documents), add that path to memory.reference.ingest_roots in config.yaml, then retry; nothing was changed",
            folder.display()
        )
    })
}

impl super::Update {
    /// Create the documents folder the resolved project binds at
    /// `/documents`, before anything mounts it (the image's config check,
    /// then the new daemon). A project without that bind has nothing to do.
    pub(super) fn documents_folder(&mut self, config: &Value) -> super::Result<()> {
        let Some(folder) = bind_source(config, super::DAEMON_SERVICE) else {
            return Ok(());
        };
        match ensure(&folder) {
            Ok(Outcome::Unchanged) => {
                if let Some(note) = owner_note(&folder) {
                    self.warn(note);
                }
                Ok(())
            }
            Ok(Outcome::Created) => {
                self.step(format!(
                    "Created the documents folder {}: document_ingest reads files copied there as /documents/<name>",
                    folder.display()
                ));
                if let Some(note) = open_parent_note(&folder) {
                    self.warn(note);
                }
                Ok(())
            }
            Err(e) => Err(super::UpdateError::usage(format!(
                "cannot create the documents folder {}: {e}; create that folder yourself, then retry; nothing was deployed",
                folder.display()
            ))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn scratch(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "pl-documents-folder-{tag}-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn the_bind_source_is_read_from_the_resolved_config() {
        let config = json!({"services": {"pseudolife-daemon": {"volumes": [
            {"type": "volume", "source": "models", "target": "/models"},
            {"type": "bind", "source": "/home/u/.pseudolife-mcp/documents", "target": "/documents",
             "read_only": true},
        ]}}});
        assert_eq!(
            bind_source(&config, "pseudolife-daemon"),
            Some(PathBuf::from("/home/u/.pseudolife-mcp/documents"))
        );
        // Near misses: another target, a named volume at the target, no volumes.
        let other = json!({"services": {"pseudolife-daemon": {"volumes": [
            {"type": "bind", "source": "/srv/docs", "target": "/srv/docs"},
            {"type": "volume", "source": "docs", "target": "/documents"},
        ]}}});
        assert_eq!(bind_source(&other, "pseudolife-daemon"), None);
        assert_eq!(bind_source(&json!({}), "pseudolife-daemon"), None);
    }

    #[test]
    fn a_missing_folder_is_created_once_and_a_rerun_changes_nothing() {
        let home = scratch("create");
        let folder = default_folder(&home);
        assert_eq!(ensure(&folder).unwrap(), Outcome::Created);
        assert!(folder.is_dir());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = |p: &Path| fs::metadata(p).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode(&folder), 0o755);
            assert_eq!(mode(folder.parent().unwrap()), 0o700);
            assert_eq!(open_parent_note(&folder), None);
        }
        std::fs::write(folder.join("kept.txt"), b"x").unwrap();
        assert_eq!(ensure(&folder).unwrap(), Outcome::Unchanged);
        assert!(folder.join("kept.txt").is_file());
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn a_file_in_the_folders_place_is_refused_by_name() {
        let home = scratch("file");
        let folder = default_folder(&home);
        fs::create_dir_all(folder.parent().unwrap()).unwrap();
        fs::write(&folder, b"not a folder").unwrap();
        let error = ensure(&folder).unwrap_err().to_string();
        assert!(error.contains(&folder.display().to_string()), "{error}");
        assert_eq!(fs::read(&folder).unwrap(), b"not a folder");
        let _ = fs::remove_dir_all(&home);
    }

    #[cfg(unix)]
    #[test]
    fn an_existing_open_parent_keeps_its_mode_and_is_named() {
        use std::os::unix::fs::PermissionsExt;
        let home = scratch("parent");
        let parent = home.join(".pseudolife-mcp");
        fs::create_dir_all(&parent).unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o755)).unwrap();
        let folder = default_folder(&home);
        assert_eq!(ensure(&folder).unwrap(), Outcome::Created);
        assert_eq!(
            fs::metadata(&parent).unwrap().permissions().mode() & 0o777,
            0o755
        );
        let note = open_parent_note(&folder).unwrap();
        assert!(note.contains("chmod 700"), "{note}");
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn a_mount_already_at_documents_is_named_before_the_release_replaces_it() {
        let config = json!({"services": {"pseudolife-daemon": {"volumes": [
            {"type": "bind", "source": "/home/u/.pseudolife-mcp/documents", "target": "/documents"},
        ]}}});
        let running = |source: &str| {
            json!({"Mounts": [
                {"Type": "volume", "Name": "state", "Destination": "/data"},
                {"Type": "bind", "Source": source, "Destination": "/documents", "RW": false},
            ]})
        };
        let conflict = mount_conflict(&config, &running("/srv/mine")).unwrap();
        assert!(
            conflict.contains("/srv/mine") && conflict.contains("ingest_roots"),
            "{conflict}"
        );
        // Near misses: the same folder (a rerun), and no mount there yet.
        assert_eq!(
            mount_conflict(&config, &running("/home/u/.pseudolife-mcp/documents")),
            None
        );
        let fresh =
            json!({"Mounts": [{"Type": "volume", "Name": "state", "Destination": "/data"}]});
        assert_eq!(mount_conflict(&config, &fresh), None);
        assert_eq!(mount_conflict(&json!({}), &running("/srv/mine")), None);
    }

    #[cfg(unix)]
    #[test]
    fn a_folder_another_account_owns_is_named_with_its_fix() {
        if rustix::process::geteuid().is_root() {
            eprintln!("skipped: root owns the folder it would check");
            return;
        }
        let home = scratch("owner");
        assert_eq!(owner_note(&home), None);
        // The system's temporary folder belongs to root.
        let note = owner_note(Path::new("/tmp")).unwrap();
        assert!(note.contains("sudo chown"), "{note}");
        let _ = fs::remove_dir_all(&home);
    }
}
