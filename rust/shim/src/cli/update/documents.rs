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

/// What [`fix_owner`] did about the documents folder's owner.
#[derive(Debug, PartialEq, Eq)]
// Only the Unix repair (and tests) construct the non-`Correct` variants.
#[cfg_attr(not(unix), allow(dead_code))]
pub(crate) enum OwnerFix {
    /// It belongs to this user (or is not a plain folder here): nothing was
    /// done and there is nothing to say.
    Correct,
    /// A one-off container gave it back to this user; the line says so.
    Repaired(String),
    /// It could not be given back automatically; the line names the fix.
    Manual(String),
}

/// A `docker run --mount` value. Docker parses it as CSV, so a field holding
/// a comma or quote is quoted, with its quotes doubled.
#[cfg(any(unix, test))]
fn mount_spec(fields: &[(&str, &str)]) -> String {
    fields
        .iter()
        .map(|(key, value)| {
            let field = format!("{key}={value}");
            if field.contains([',', '"', '\n', '\r']) {
                format!("\"{}\"", field.replace('"', "\"\""))
            } else {
                field
            }
        })
        .collect::<Vec<_>>()
        .join(",")
}

/// The one-off repair container: the daemon image already on this host
/// (never pulled), as uid 0 holding only CHOWN, with no network and a
/// read-only root filesystem, mounting only the documents folder and giving
/// that folder alone (not its contents) to `uid:gid`. Every value is its own
/// argv word; no shell runs.
#[cfg(any(unix, test))]
pub(crate) fn repair_args(
    folder: &Path,
    image: &str,
    uid: u32,
    gid: u32,
    name: &str,
) -> Vec<String> {
    let source = folder.to_string_lossy();
    let mount = mount_spec(&[("type", "bind"), ("source", &source), ("dst", TARGET)]);
    let owner = format!("{uid}:{gid}");
    [
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        "chown",
        "--mount",
        &mount,
        image,
        &owner,
        TARGET,
    ]
    .into_iter()
    .map(str::to_owned)
    .collect()
}

/// The manual fix for a folder `owner` holds instead of `uid:gid`, naming
/// why the automatic repair did not do it when it was tried.
#[cfg(any(unix, test))]
fn sudo_line(folder: &Path, owner: u32, uid: u32, gid: u32, failed: Option<&str>) -> String {
    let failed = failed
        .map(|why| format!("; the automatic repair failed ({why})"))
        .unwrap_or_default();
    format!(
        "{} belongs to another account (uid {owner}), so you cannot copy documents into it{failed}; run sudo chown {uid}:{gid} {} once",
        folder.display(),
        folder.display()
    )
}

/// The first non-empty line of what Docker printed, bounded, or its exit code.
#[cfg(any(unix, test))]
fn docker_reason(code: i32, out: &str) -> String {
    out.lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .map(|l| l.chars().take(200).collect())
        .unwrap_or_else(|| format!("Docker exited {code}"))
}

/// Give the documents folder back to this user (`uid:gid`) when another
/// account owns it, as Docker leaves a bind source it had to create (an
/// updater older than this folder does that), so the user can copy
/// documents in again.
///
/// Only a folder root owns, at the default place (`expected`), is repaired
/// automatically, by a one-off container of `image` ([`repair_args`]), and
/// only a re-read owner proves the repair: user-namespace remapping or a
/// network filesystem can let the container succeed without changing it.
/// The folder then gets the documents mode, `0755`, from this process, its
/// owner. A folder another non-root account owns, or one at another place,
/// may be meant that way, so it is only named with the fix, as it is when
/// Docker or the image is unavailable.
#[cfg(any(unix, test))]
pub(crate) fn fix_owner_with(
    folder: &Path,
    expected: &Path,
    image: Option<&str>,
    (uid, gid): (u32, u32),
    owner_of: &dyn Fn(&Path) -> Option<u32>,
    docker: &mut dyn FnMut(&[String]) -> (i32, String),
) -> OwnerFix {
    let Some(owner) = owner_of(folder) else {
        return OwnerFix::Correct;
    };
    if owner == uid {
        return OwnerFix::Correct;
    }
    let failed = |why: &str| OwnerFix::Manual(sudo_line(folder, owner, uid, gid, Some(why)));
    let at_default = match (fs::canonicalize(folder), fs::canonicalize(expected)) {
        (Ok(actual), Ok(expected)) => actual == expected,
        _ => false,
    };
    if owner != 0 || !at_default {
        return OwnerFix::Manual(sudo_line(folder, owner, uid, gid, None));
    }
    let Some(image) = image.filter(|i| !i.is_empty()) else {
        return failed("no daemon image is on this host");
    };
    // Read again just before the run: a link or a non-folder put in its
    // place meanwhile is never handed to Docker.
    if owner_of(folder) != Some(0) {
        return failed("the folder changed while it was being checked");
    }
    let name = format!(
        "pseudolife-documents-owner-{}",
        uuid::Uuid::new_v4().simple()
    );
    let (code, out) = docker(&repair_args(folder, image, uid, gid, &name));
    if code != 0 {
        // `--rm` removes a container that ran; this removes one a timeout left.
        let _ = docker(&["rm".into(), "-f".into(), name]);
        return failed(&docker_reason(code, &out));
    }
    if owner_of(folder) != Some(uid) {
        return failed("the container ran but the folder's owner did not change");
    }
    if let Err(e) = set_mode(folder, 0o755) {
        return OwnerFix::Manual(format!(
            "{} is yours again, but its mode could not be set ({e}); run chmod 755 {} once",
            folder.display(),
            folder.display()
        ));
    }
    OwnerFix::Repaired(format!(
        "{} belonged to root (Docker created it), so you could not copy documents into it; it is yours again",
        folder.display()
    ))
}

/// `fix_owner_with` (Unix only) for this process's user, reading the folder's owner
/// without following a link. Windows has nothing to repair: Docker Desktop
/// creates the folder as the user.
pub(crate) fn fix_owner(
    folder: &Path,
    image: Option<&str>,
    docker: &mut dyn FnMut(&[String]) -> (i32, String),
) -> OwnerFix {
    fix_owner_in(folder, &compose_home(), image, docker)
}

/// [`fix_owner`] with the home directory the default folder is under.
pub(crate) fn fix_owner_in(
    folder: &Path,
    home: &Path,
    image: Option<&str>,
    docker: &mut dyn FnMut(&[String]) -> (i32, String),
) -> OwnerFix {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let me = (
            rustix::process::geteuid().as_raw(),
            rustix::process::getegid().as_raw(),
        );
        let owner_of = |path: &Path| {
            fs::symlink_metadata(path)
                .ok()
                .filter(|m| m.is_dir())
                .map(|m| m.uid())
        };
        fix_owner_with(folder, &default_folder(home), image, me, &owner_of, docker)
    }
    #[cfg(not(unix))]
    {
        let _ = (folder, home, image, docker);
        OwnerFix::Correct
    }
}

/// The one-line note for an installer run as root under sudo, or `None`:
/// the documents folder is checked for the account running it (root), not
/// for the account sudo was run from.
pub(crate) fn sudo_note(folder: &Path) -> Option<String> {
    #[cfg(unix)]
    {
        let sudo_uid = std::env::var("SUDO_UID").ok();
        sudo_note_for(
            folder,
            rustix::process::geteuid().as_raw(),
            sudo_uid.as_deref(),
        )
    }
    #[cfg(not(unix))]
    {
        let _ = folder;
        None
    }
}

#[cfg(any(unix, test))]
pub(crate) fn sudo_note_for(folder: &Path, euid: u32, sudo_uid: Option<&str>) -> Option<String> {
    let sudo_uid = sudo_uid.filter(|s| !s.is_empty())?;
    (euid == 0).then(|| {
        format!(
            "this runs as root under sudo (from uid {sudo_uid}), so the documents folder is checked for root, the account running it ({}); run it without sudo to set up your own account's folder",
            folder.display()
        )
    })
}

#[cfg(unix)]
fn set_mode(path: &Path, mode: u32) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
}

#[cfg(all(not(unix), test))]
fn set_mode(_path: &Path, _mode: u32) -> io::Result<()> {
    Ok(())
}

/// The documents folder the running daemon container binds at `/documents`
/// and the image it runs (by ID), from `docker inspect`.
pub(crate) fn running_folder(running: &Value) -> Option<(PathBuf, Option<String>)> {
    let source = running["Mounts"].as_array()?.iter().find(|m| {
        m["Type"] == "bind"
            && m["Destination"].as_str().map(|d| d.trim_end_matches('/')) == Some(TARGET)
    })?["Source"]
        .as_str()
        .filter(|s| !s.is_empty())?;
    let image = running["Image"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    Some((PathBuf::from(source), image))
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
    /// A folder that exists but belongs to root is given back to the user
    /// with `image` (already on this host).
    pub(super) fn documents_folder(&mut self, config: &Value, image: &str) -> super::Result<()> {
        let Some(folder) = bind_source(config, super::DAEMON_SERVICE) else {
            return Ok(());
        };
        match ensure(&folder) {
            Ok(Outcome::Unchanged) => {
                self.documents_owner(&folder, Some(image));
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

    /// The client steps' check (the first upgrade from an updater older than
    /// the folder included): the folder the running daemon binds at
    /// `/documents`, given back to the user with the daemon's own image when
    /// root owns it. No daemon container or no such bind says nothing, and a
    /// folder that cannot be repaired only warns: the client steps go on.
    pub(super) fn documents_owner_of_running(&mut self) {
        let (code, out) = self.docker_call(&["inspect", super::DAEMON_CONTAINER]);
        if code != 0 {
            return;
        }
        let Some((folder, image)) = serde_json::from_str::<Value>(&out)
            .ok()
            .and_then(|v| v.as_array().and_then(|a| a.first()).cloned())
            .as_ref()
            .and_then(running_folder)
        else {
            return;
        };
        self.documents_owner(&folder, image.as_deref());
    }

    fn documents_owner(&mut self, folder: &Path, image: Option<&str>) {
        let fix = self.owner_fix(folder, image);
        self.report_owner(fix);
    }

    fn owner_fix(&self, folder: &Path, image: Option<&str>) -> OwnerFix {
        let mut docker = |args: &[String]| self.command(args, None, 120, false);
        // Tests name the owner (uid 1000 is this user), since no test can
        // make root own a folder.
        #[cfg(test)]
        if let Some(probe) = self.owner_probe {
            return fix_owner_with(folder, folder, image, (1000, 1000), &probe, &mut docker);
        }
        fix_owner(folder, image, &mut docker)
    }

    fn report_owner(&mut self, fix: OwnerFix) {
        match fix {
            OwnerFix::Correct => {}
            OwnerFix::Repaired(line) => self.step(line),
            OwnerFix::Manual(line) => self.warn(line),
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

    /// What the fake Docker was asked.
    struct FakeDocker {
        calls: Vec<Vec<String>>,
    }

    /// [`fix_owner_with`] with this user as uid 1000, the folder at the
    /// default place, its owner `owner` until a helper that exits `code`
    /// (0 sets it to `repaired_owner`), and a fake Docker.
    fn owner_fix(
        folder: &Path,
        image: Option<&str>,
        owner: u32,
        repaired_owner: u32,
        code: i32,
    ) -> (OwnerFix, FakeDocker) {
        let owner = std::cell::Cell::new(owner);
        let mut fake = FakeDocker { calls: vec![] };
        let owner_of = |_: &Path| Some(owner.get());
        let fix = fix_owner_with(
            folder,
            folder,
            image,
            (1000, 1000),
            &owner_of,
            &mut |args| {
                fake.calls.push(args.to_vec());
                if args[0] == "run" && code == 0 {
                    owner.set(repaired_owner);
                }
                (code, String::new())
            },
        );
        (fix, fake)
    }

    #[test]
    fn a_folder_root_owns_is_given_back_by_a_one_off_container() {
        let home = scratch("repair");
        let folder = home.join("documents");
        fs::create_dir_all(&folder).unwrap();
        set_mode(&folder, 0o700).unwrap();
        let (fix, fake) = owner_fix(&folder, Some("sha256:abc"), 0, 1000, 0);
        let OwnerFix::Repaired(line) = fix else {
            panic!("{fix:?}")
        };
        assert!(line.contains("it is yours again"), "{line}");
        assert_eq!(fake.calls.len(), 1, "{:?}", fake.calls);
        let args = &fake.calls[0];
        let name = &args[args.iter().position(|a| a == "--name").unwrap() + 1];
        assert_eq!(args, &repair_args(&folder, "sha256:abc", 1000, 1000, name));
        // The helper: never pulled, no network, a read-only root, uid 0 with
        // CHOWN alone, only this folder mounted (writable), no shell.
        for pair in [
            ["--pull", "never"],
            ["--network", "none"],
            ["--user", "0:0"],
            ["--cap-drop", "ALL"],
            ["--cap-add", "CHOWN"],
            ["--entrypoint", "chown"],
        ] {
            assert!(args.windows(2).any(|w| w == pair), "{pair:?} in {args:?}");
        }
        assert!(args.contains(&"--read-only".to_owned()));
        assert_eq!(args.iter().filter(|a| *a == "--mount").count(), 1);
        let mount = &args[args.iter().position(|a| a == "--mount").unwrap() + 1];
        assert_eq!(
            mount,
            &format!("type=bind,source={},dst=/documents", folder.display())
        );
        assert_eq!(
            &args[args.len() - 3..],
            ["sha256:abc", "1000:1000", "/documents"]
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(&folder).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o755);
        }
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn without_docker_the_sudo_line_names_the_fix() {
        let home = scratch("manual");
        let folder = home.join("documents");
        fs::create_dir_all(&folder).unwrap();
        let sudo = format!("sudo chown 1000:1000 {}", folder.display());
        // Docker could not run the helper: a leftover is removed by name.
        let (fix, fake) = owner_fix(&folder, Some("sha256:abc"), 0, 1000, 125);
        assert!(
            matches!(fix, OwnerFix::Manual(ref l) if l.contains(&sudo)),
            "{fix:?}"
        );
        assert_eq!(fake.calls.len(), 2, "{:?}", fake.calls);
        assert_eq!(fake.calls[1][..2], ["rm", "-f"]);
        // No image on this host: nothing runs.
        let (fix, fake) = owner_fix(&folder, None, 0, 1000, 0);
        assert!(
            matches!(fix, OwnerFix::Manual(ref l) if l.contains(&sudo)),
            "{fix:?}"
        );
        assert!(fake.calls.is_empty());
        // The helper ran but the owner did not change (a remapped user
        // namespace): only the re-read owner counts.
        let (fix, _) = owner_fix(&folder, Some("sha256:abc"), 0, 0, 0);
        assert!(
            matches!(fix, OwnerFix::Manual(ref l) if l.contains(&sudo)),
            "{fix:?}"
        );
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn a_folder_the_user_owns_is_left_alone_and_says_nothing() {
        let home = scratch("correct");
        let folder = home.join("documents");
        fs::create_dir_all(&folder).unwrap();
        set_mode(&folder, 0o750).unwrap();
        for _ in 0..2 {
            let (fix, fake) = owner_fix(&folder, Some("sha256:abc"), 1000, 1000, 0);
            assert_eq!(fix, OwnerFix::Correct);
            assert!(fake.calls.is_empty());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(&folder).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o750, "a correct folder keeps its mode");
        }
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn only_a_folder_root_owns_at_the_default_place_is_repaired() {
        let home = scratch("nearmiss");
        let folder = home.join("documents");
        fs::create_dir_all(&folder).unwrap();
        // Another (non-root) account's folder may be meant that way.
        let (fix, fake) = owner_fix(&folder, Some("sha256:abc"), 1001, 1000, 0);
        assert!(
            matches!(fix, OwnerFix::Manual(ref l) if l.contains("uid 1001")),
            "{fix:?}"
        );
        assert!(fake.calls.is_empty());
        // A folder root owns somewhere other than the default place.
        let elsewhere = home.join("elsewhere");
        fs::create_dir_all(&elsewhere).unwrap();
        let mut calls = 0;
        let fix = fix_owner_with(
            &elsewhere,
            &folder,
            Some("sha256:abc"),
            (1000, 1000),
            &|_| Some(0),
            &mut |_| {
                calls += 1;
                (0, String::new())
            },
        );
        assert!(matches!(fix, OwnerFix::Manual(_)), "{fix:?}");
        assert_eq!(calls, 0);
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn the_running_daemons_documents_bind_and_image_are_read() {
        let running = json!({"Image": "sha256:abc", "Mounts": [
            {"Type": "volume", "Name": "state", "Destination": "/data"},
            {"Type": "bind", "Source": "/home/u/.pseudolife-mcp/documents", "Destination": "/documents"},
        ]});
        assert_eq!(
            running_folder(&running),
            Some((
                PathBuf::from("/home/u/.pseudolife-mcp/documents"),
                Some("sha256:abc".into())
            ))
        );
        // A named volume at /documents: Docker reports its data directory as
        // the Source, which is not a folder to give back.
        let volume = json!({"Image": "x", "Mounts": [
            {"Type": "volume", "Name": "docs", "Source": "/var/lib/docker/volumes/docs/_data",
             "Destination": "/documents"}]});
        assert_eq!(running_folder(&volume), None);
        assert_eq!(running_folder(&json!({})), None);
    }

    thread_local! {
        static RUNNING: std::cell::RefCell<String> = const { std::cell::RefCell::new(String::new()) };
        static REPAIRED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
        static HELPER_CODE: std::cell::Cell<i32> = const { std::cell::Cell::new(0) };
        static CALLS: std::cell::RefCell<Vec<Vec<String>>> = const { std::cell::RefCell::new(vec![]) };
    }

    /// An update whose Docker answers `inspect` with [`RUNNING`] and runs
    /// the helper with [`HELPER_CODE`], and whose folder root owns until
    /// the helper succeeded.
    fn client_step_update() -> super::super::Update {
        let mut update = super::super::Update::new(super::super::Options::parse(vec![]).unwrap());
        update.o.as_json = true;
        update.command_probe = Some(|args| {
            CALLS.with(|c| c.borrow_mut().push(args.to_vec()));
            match args[0].as_str() {
                "inspect" => RUNNING.with(|r| {
                    let r = r.borrow();
                    (if r.is_empty() { 1 } else { 0 }, r.clone())
                }),
                "run" => {
                    let code = HELPER_CODE.with(|c| c.get());
                    REPAIRED.with(|f| f.set(code == 0));
                    (code, String::new())
                }
                _ => (0, String::new()),
            }
        });
        update.owner_probe = Some(|_| Some(if REPAIRED.with(|f| f.get()) { 1000 } else { 0 }));
        update
    }

    #[test]
    fn the_client_steps_give_back_the_folder_the_running_daemon_binds() {
        let home = scratch("client-step");
        let folder = home.join("documents");
        fs::create_dir_all(&folder).unwrap();
        let running = json!([{"Image": "sha256:daemon", "Mounts": [
            {"Type": "bind", "Source": folder.to_string_lossy(), "Destination": "/documents", "RW": false}]}]);
        RUNNING.with(|r| *r.borrow_mut() = running.to_string());
        REPAIRED.with(|f| f.set(false));
        HELPER_CODE.with(|c| c.set(0));
        CALLS.with(|c| c.borrow_mut().clear());
        let mut update = client_step_update();
        update.documents_owner_of_running();
        assert_eq!(update.report.steps.len(), 1, "{:?}", update.report.steps);
        assert!(update.report.steps[0].contains("it is yours again"));
        let calls: Vec<Vec<String>> = CALLS.with(|c| c.borrow().clone());
        assert!(
            calls
                .iter()
                .any(|a| a[0] == "run" && a.contains(&"sha256:daemon".to_owned())),
            "{calls:?}"
        );
        // A rerun on the repaired folder does nothing and says nothing.
        CALLS.with(|c| c.borrow_mut().clear());
        let mut again = client_step_update();
        again.documents_owner_of_running();
        assert!(again.report.steps.is_empty(), "{:?}", again.report.steps);
        assert!(CALLS.with(|c| c.borrow().iter().all(|a| a[0] == "inspect")));
        // Docker cannot run the helper: the sudo line, as a warning only.
        REPAIRED.with(|f| f.set(false));
        HELPER_CODE.with(|c| c.set(125));
        let mut manual = client_step_update();
        manual.documents_owner_of_running();
        assert_eq!(manual.report.steps.len(), 1, "{:?}", manual.report.steps);
        assert!(manual.report.steps[0].starts_with("WARNING: "));
        assert!(manual.report.steps[0].contains("sudo chown 1000:1000"));
        assert!(manual.report.ok);
        // No daemon container here (a client machine): nothing is said.
        RUNNING.with(|r| r.borrow_mut().clear());
        let mut none = client_step_update();
        none.documents_owner_of_running();
        assert!(none.report.steps.is_empty(), "{:?}", none.report.steps);
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn a_folder_replaced_before_the_run_is_never_handed_to_docker() {
        let home = scratch("swapped");
        let folder = home.join("documents");
        fs::create_dir_all(&folder).unwrap();
        // Root's folder on the first read, a link (no folder) on the re-read
        // just before the run.
        let reads = std::cell::Cell::new(0);
        let owner_of = |_: &Path| {
            reads.set(reads.get() + 1);
            (reads.get() == 1).then_some(0)
        };
        let mut runs = 0;
        let fix = fix_owner_with(
            &folder,
            &folder,
            Some("sha256:abc"),
            (1000, 1000),
            &owner_of,
            &mut |_| {
                runs += 1;
                (0, String::new())
            },
        );
        assert!(
            matches!(fix, OwnerFix::Manual(ref l) if l.contains("changed while it was being checked")),
            "{fix:?}"
        );
        assert_eq!(runs, 0);
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn the_sudo_line_names_why_docker_could_not_repair_it() {
        let home = scratch("reason");
        let folder = home.join("documents");
        fs::create_dir_all(&folder).unwrap();
        let fix = fix_owner_with(
            &folder,
            &folder,
            Some("sha256:abc"),
            (1000, 1000),
            &|_| Some(0),
            &mut |args| {
                if args[0] == "run" {
                    (125, "\ndocker: Error response from daemon: no such image\nRun 'docker run --help'".into())
                } else {
                    (0, String::new())
                }
            },
        );
        let OwnerFix::Manual(line) = fix else {
            panic!("{fix:?}")
        };
        assert!(
            line.contains(
                "the automatic repair failed (docker: Error response from daemon: no such image)"
            ),
            "{line}"
        );
        assert!(line.contains("sudo chown 1000:1000"), "{line}");
        // Silent Docker: its exit code is the reason.
        let fix = fix_owner_with(
            &folder,
            &folder,
            Some("sha256:abc"),
            (1000, 1000),
            &|_| Some(0),
            &mut |_| (125, String::new()),
        );
        assert!(
            matches!(fix, OwnerFix::Manual(ref l) if l.contains("(Docker exited 125)")),
            "{fix:?}"
        );
        let _ = fs::remove_dir_all(&home);
    }

    /// Whether this test process runs as root, which owns the folders the
    /// real-owner tests rely on another account owning.
    #[cfg(unix)]
    fn as_root() -> bool {
        let root = rustix::process::geteuid().is_root();
        if root {
            eprintln!("skipped: root owns the folders this test needs another account to own");
        }
        root
    }

    #[cfg(unix)]
    #[test]
    fn the_real_check_leaves_roots_folder_elsewhere_to_the_sudo_line() {
        if as_root() {
            return;
        }
        let home = scratch("real-elsewhere");
        let mut runs = 0;
        // `/` is root's real folder, not the default one under this home.
        let fix = fix_owner_in(Path::new("/"), &home, Some("sha256:abc"), &mut |_| {
            runs += 1;
            (0, String::new())
        });
        assert!(
            matches!(fix, OwnerFix::Manual(ref l) if l.contains("uid 0") && l.contains("sudo chown")),
            "{fix:?}"
        );
        assert_eq!(runs, 0);
        let _ = fs::remove_dir_all(&home);
    }

    #[cfg(unix)]
    #[test]
    fn the_real_check_never_follows_a_link_at_the_default_place() {
        if as_root() {
            return;
        }
        let home = scratch("real-link");
        let folder = default_folder(&home);
        fs::create_dir_all(folder.parent().unwrap()).unwrap();
        // A link where the folder goes, to a folder root owns.
        std::os::unix::fs::symlink("/", &folder).unwrap();
        let mut runs = 0;
        let fix = fix_owner_in(&folder, &home, Some("sha256:abc"), &mut |_| {
            runs += 1;
            (0, String::new())
        });
        assert_eq!(fix, OwnerFix::Correct);
        assert_eq!(runs, 0);
        assert_eq!(fs::read_link(&folder).unwrap(), Path::new("/"));
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn a_run_as_root_under_sudo_is_named_once() {
        let folder = Path::new("/root/.pseudolife-mcp/documents");
        let note = sudo_note_for(folder, 0, Some("1000")).unwrap();
        assert!(
            note.contains("uid 1000") && note.contains("checked for root"),
            "{note}"
        );
        // Near misses: not root, root without sudo, an empty SUDO_UID.
        assert_eq!(sudo_note_for(folder, 1000, Some("1000")), None);
        assert_eq!(sudo_note_for(folder, 0, None), None);
        assert_eq!(sudo_note_for(folder, 0, Some("")), None);
    }
}
