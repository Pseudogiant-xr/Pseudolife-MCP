//! A home or data folder reached through a link the user owns works for
//! the token file and the board's state (#1639), through the public entry
//! points only, so the same file runs against a client from before the
//! change (where every case here was refused). The rule's own cases,
//! refusals included, are `private_path`'s unit tests.
#![cfg(unix)]

use pseudolife_stdio::{board::state, credentials::CredentialProvider};
use std::{
    fs,
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
};

/// A disposable folder under the temporary directory as given, not
/// resolved: under macOS's `/var` or a linked `TMPDIR` it sits behind a link
/// too, which is one of the cases.
struct Scratch(PathBuf);
impl Scratch {
    fn new(name: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "pl-behind-links-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// A folder only this user may write, whatever the umask (002 under
/// Debian's user private groups makes new folders group-writable).
fn dir(path: &Path) {
    fs::create_dir(path).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
}

fn token_in(folder: &Path) -> PathBuf {
    fs::create_dir_all(folder).unwrap();
    for made in [folder, folder.parent().unwrap()] {
        fs::set_permissions(made, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let file = folder.join("token");
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&file)
        .unwrap()
        .write_all(b"fixture-token")
        .unwrap();
    file
}

#[test]
fn the_token_file_is_read_through_a_home_behind_the_users_link() {
    let scratch = Scratch::new("token");
    token_in(&scratch.0.join("real-home/.pseudolife-mcp"));
    symlink(scratch.0.join("real-home"), scratch.0.join("home")).unwrap();
    let provider =
        CredentialProvider::new(None, Some(scratch.0.join("home/.pseudolife-mcp/token"))).unwrap();
    assert_eq!(provider.snapshot().unwrap().token(), Some("fixture-token"));
}

#[test]
fn the_board_keeps_its_state_under_a_home_behind_the_users_link() {
    let scratch = Scratch::new("board");
    dir(&scratch.0.join("real-home"));
    symlink(scratch.0.join("real-home"), scratch.0.join("home")).unwrap();
    let folder = scratch.0.join("home/.pseudolife-mcp/agents");
    state::private_dir(&folder).unwrap();
    let file = folder.join("record.json");
    drop(state::create(&file).unwrap());
    state::open(&file).unwrap();
    assert_eq!(state::diagnose(&file), None);
}

#[test]
fn a_data_folder_moved_to_another_disk_with_a_link_works() {
    let scratch = Scratch::new("data");
    dir(&scratch.0.join("big-disk"));
    dir(&scratch.0.join("home"));
    symlink(
        scratch.0.join("big-disk"),
        scratch.0.join("home/.pseudolife-mcp"),
    )
    .unwrap();
    let data = scratch.0.join("home/.pseudolife-mcp");
    state::private_dir(&data).unwrap();
    let file = data.join("record.json");
    drop(state::create(&file).unwrap());
    state::open(&file).unwrap();
}
