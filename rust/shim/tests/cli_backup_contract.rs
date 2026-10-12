#![forbid(unsafe_code)]
//! Backup without a database: help, refusals, file mode and every deferral.
//! Explicit-DSN dumps are proven against the Python oracle by the CLI harness
//! (`rust/cli_harness --row backup`), which needs a disposable PostgreSQL.
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Output},
};

const DEFERRED: &str = "pseudolife-stdio: mode 'backup' is deferred in this candidate\n";
const SKIPPED: &str =
    "bank dump:     skipped (no database configured — file-mode state archived only)\n";

#[cfg(any(windows, target_os = "linux"))]
fn in_ci() -> bool {
    std::env::var_os("CI").is_some() || std::env::var_os("GITHUB_ACTIONS").is_some()
}

#[cfg(windows)]
fn symlink_created(result: std::io::Result<()>) -> bool {
    match result {
        Ok(()) => true,
        Err(error) if error.raw_os_error() == Some(1314) && !in_ci() => {
            eprintln!("Windows symlink fixture unverified locally: privilege unavailable (1314)");
            false
        }
        Err(error) => panic!("Windows symlink fixture failed: {error}"),
    }
}

struct Home(PathBuf);

impl Home {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!("backup-{label}-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn path(&self) -> &Path {
        &self.0
    }
    fn listing(&self) -> Vec<String> {
        let mut names = Vec::new();
        collect(&self.0, &self.0, &mut names);
        names.sort();
        names
    }
}

impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn collect(root: &Path, dir: &Path, names: &mut Vec<String>) {
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        names.push(path.strip_prefix(root).unwrap().display().to_string());
        if path.is_dir() && !path.is_symlink() {
            collect(root, &path, names);
        }
    }
}

fn command(home: &Path) -> Command {
    let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("backup")
        .current_dir(home)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("COLUMNS", "80");
    command
}

fn run(home: &Path, arguments: &[&str]) -> Output {
    command(home).args(arguments).output().unwrap()
}

fn assert_deferred(home: &Home, arguments: &[&str]) {
    let before = home.listing();
    let output = run(home.path(), arguments);
    assert_eq!(output.status.code(), Some(1), "{arguments:?}");
    assert!(output.stdout.is_empty(), "{arguments:?}");
    assert_eq!(output.stderr, cli::native_text(DEFERRED), "{arguments:?}");
    assert_eq!(home.listing(), before, "{arguments:?} changed the home");
}

fn assert_refused(home: &Home, argument: &str) {
    let before = home.listing();
    let output = run(home.path(), &["--data-dir", argument]);
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        cli::native_text(&format!(
            "data dir {argument} does not exist — nothing to back up.\n"
        ))
    );
    assert_eq!(home.listing(), before);
}

fn text(path: &Path) -> String {
    path.to_str().unwrap().to_owned()
}

#[test]
fn captured_public_help_precedes_bank_resolution() {
    let home = Home::new("help");
    let output = run(home.path(), &["--help"]);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        output.stdout,
        cli::native_text(include_str!("../src/cli/backup_help.txt"))
    );
    assert!(output.stderr.is_empty());
    assert!(home.listing().is_empty());
}

#[test]
fn help_at_another_width_defers() {
    let home = Home::new("width");
    for columns in [Some("120"), None] {
        let mut command = command(home.path());
        match columns {
            Some(value) => command.env("COLUMNS", value),
            None => command.env_remove("COLUMNS"),
        };
        let output = command.arg("--help").output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, cli::native_text(DEFERRED));
    }
    assert!(home.listing().is_empty());
}

#[test]
fn missing_canonical_paths_are_refused_verbatim() {
    let home = Home::new("missing");
    fs::create_dir(home.path().join("anchor")).unwrap();
    assert_refused(&home, &text(&home.path().join("missing-bank")));
    assert_refused(
        &home,
        &text(&home.path().join("anchor").join("..").join("missing.µ")),
    );
    assert_refused(&home, "relative-missing-µ");
}

#[test]
fn noncanonical_argv_defers_without_effects() {
    let home = Home::new("argv");
    fs::create_dir(home.path().join("bank")).unwrap();
    let bank = text(&home.path().join("bank"));
    let separator = std::path::MAIN_SEPARATOR;
    let dotted = format!("{bank}{separator}.{separator}missing-µ");
    let doubled = format!("{bank}{separator}{separator}missing-µ");
    let trailing = format!("{bank}{separator}");
    let joined = format!("--data-dir={bank}");
    for arguments in [
        vec!["--data-dir", dotted.as_str()],
        vec!["--data-dir", doubled.as_str()],
        vec!["--data-dir", trailing.as_str()],
        vec![joined.as_str()],
        vec!["--data", bank.as_str()],
        vec!["--data-dir", bank.as_str(), "--data-dir", bank.as_str()],
        vec!["--data-dir", bank.as_str(), "--keep-days", "1e3"],
        vec!["--data-dir", bank.as_str(), "--keep-days", "-1"],
        vec!["--data-dir", bank.as_str(), "--keep-days", ".5"],
        vec!["--data-dir", bank.as_str(), "--keep-days", "0"],
        vec!["--data-dir", bank.as_str(), "--keep-days", "0.0006"],
        vec!["--data-dir", bank.as_str(), "--out"],
        vec!["--data-dir", bank.as_str(), "--out", "-x"],
        vec!["--data-dir", bank.as_str(), "extra"],
        vec!["-h"],
    ] {
        assert_deferred(&home, &arguments);
    }
}

#[cfg(not(windows))]
#[test]
fn leading_double_slash_defers() {
    let home = Home::new("root");
    let argument = format!("/{}/missing", home.path().display());
    assert_deferred(&home, &["--data-dir", &argument]);
}

#[cfg(windows)]
#[test]
fn extended_unc_and_slash_spellings_defer_without_effects() {
    let home = Home::new("prefix");
    let display = home.path().display();
    for argument in [
        format!("{display}/missing-µ"),
        format!("\\\\?\\{display}\\missing-µ"),
        String::from("\\\\localhost\\absent-share-µ\\missing"),
        String::from("C:relative-µ"),
    ] {
        assert_deferred(&home, &["--data-dir", &argument]);
    }
}

#[test]
fn the_embedded_tier_defers_before_any_effect() {
    let home = Home::new("embedded");
    let pgdata = home.path().join("bank").join("embedded_pg");
    fs::create_dir_all(&pgdata).unwrap();
    fs::write(pgdata.join("PG_VERSION"), "18\n").unwrap();
    assert_deferred(&home, &["--data-dir", &text(&home.path().join("bank"))]);
    // No argument and an existing lite default dir: Python may attach pg0.
    let lite = if cfg!(windows) {
        home.path()
            .join("AppData")
            .join("Local")
            .join("pseudolife-mcp")
    } else if cfg!(target_os = "macos") {
        home.path()
            .join("Library")
            .join("Application Support")
            .join("pseudolife-mcp")
    } else {
        home.path()
            .join(".local")
            .join("share")
            .join("pseudolife-mcp")
    };
    fs::create_dir_all(&lite).unwrap();
    assert_deferred(&home, &[]);
}

#[test]
fn outputs_that_would_archive_themselves_defer() {
    let home = Home::new("nested");
    let bank = home.path().join("bank");
    fs::create_dir_all(bank.join("sub")).unwrap();
    fs::write(bank.join("config.yaml"), "x").unwrap();
    let bank_text = text(&bank);
    let parent = text(&bank.join("sub").join("..").join("sub"));
    for out in [
        bank_text.clone(),
        text(&bank.join("sub").join("bk")),
        parent.clone(),
    ] {
        assert_deferred(&home, &["--data-dir", &bank_text, "--out", &out]);
    }
    assert_deferred(&home, &["--data-dir", &parent]);
}

#[test]
fn hard_links_in_the_tree_are_archived() {
    let home = Home::new("hardlink");
    let bank = home.path().join("bank");
    fs::create_dir_all(bank.join("sub")).unwrap();
    fs::write(bank.join("sub").join("a"), "x").unwrap();
    fs::hard_link(bank.join("sub").join("a"), bank.join("b")).unwrap();
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let archive = fs::read_dir(bank.join("backups"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let reader = fs::File::open(archive).unwrap();
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(reader));
    let kinds: Vec<_> = archive
        .entries()
        .unwrap()
        .map(|entry| {
            let entry = entry.unwrap();
            (
                entry.path().unwrap().into_owned(),
                entry.header().entry_type(),
                entry.link_name().unwrap().map(|name| name.into_owned()),
            )
        })
        .collect();
    assert_eq!(
        kinds
            .iter()
            .filter(|(_, kind, _)| *kind == tar::EntryType::Regular)
            .count(),
        1
    );
    assert_eq!(
        kinds
            .iter()
            .filter(|(_, kind, _)| *kind == tar::EntryType::Link)
            .count(),
        1
    );
    assert!(
        kinds
            .iter()
            .any(|(name, kind, link)| name == Path::new("sub/a")
                && *kind == tar::EntryType::Link
                && link.as_deref() == Some(Path::new("b")))
    );
}

#[cfg(unix)]
#[test]
fn symlink_loops_and_aliased_outputs_defer() {
    let home = Home::new("symlink");
    let bank = home.path().join("bank");
    fs::create_dir_all(bank.join("sub")).unwrap();
    std::os::unix::fs::symlink("loop", bank.join("loop")).unwrap();
    assert_deferred(&home, &["--data-dir", &text(&bank)]);
    fs::remove_file(bank.join("loop")).unwrap();
    // An output reached through an outside alias of an archived child.
    std::os::unix::fs::symlink(bank.join("sub"), home.path().join("alias")).unwrap();
    let out = text(&home.path().join("alias").join("bk"));
    assert_deferred(&home, &["--data-dir", &text(&bank), "--out", &out]);
}

#[cfg(windows)]
fn python() -> PathBuf {
    std::env::var_os("PSEUDOLIFE_MCP_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("python"))
}

#[cfg(windows)]
#[test]
fn tz_overrides_match_python_crt_timestamp_names() {
    for tz in [
        "",
        "UTC0",
        "GMT0",
        "JST-9",
        "IST-5:30",
        "PST8PDT",
        "Europe/Paris",
        "nonsense",
    ] {
        let home = Home::new("tz");
        let bank = home.path().join("bank");
        fs::create_dir(&bank).unwrap();
        let stamp = || {
            let output = Command::new(python())
                .env("TZ", tz)
                .args(["-c", "import time; print(time.strftime('%Y%m%d-%H%M%S'))"])
                .output()
                .unwrap();
            assert!(output.status.success(), "{output:?}");
            String::from_utf8(output.stdout).unwrap().trim().to_owned()
        };
        let before = stamp();
        let output = command(home.path())
            .env("TZ", tz)
            .args(["--data-dir", &text(&bank)])
            .output()
            .unwrap();
        let after = stamp();
        assert_eq!(output.status.code(), Some(0), "TZ={tz:?} {output:?}");
        let name = fs::read_dir(bank.join("backups"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .file_name()
            .into_string()
            .unwrap();
        let actual = name
            .strip_prefix("pseudolife_lite_state-")
            .unwrap()
            .strip_suffix(".tar.gz")
            .unwrap();
        assert!(
            actual >= before.as_str() && actual <= after.as_str(),
            "TZ={tz:?}: {before} <= {actual} <= {after}"
        );
    }
}

#[cfg(windows)]
#[test]
fn unicode_rotation_matches_python_and_preserves_unrelated_files() {
    let home = Home::new("glob");
    let bank = home.path().join("bank");
    let backups = bank.join("backups");
    fs::create_dir_all(&backups).unwrap();
    let matched = [
        "pseudolıfe_lite_state-old.tar.gz",
        "pseudolİfe_lite_state-old.tar.gz",
        "pſeudolife_lite_state-old.tar.gz",
        "PSEUDOLIFE_LITE_STATE-old.TAR.GZ",
    ];
    for name in matched
        .into_iter()
        .chain(["注意.txt", "pseudolife_lite_state-old.tar.gż"])
    {
        let path = backups.join(name);
        fs::write(&path, "x").unwrap();
        fs::File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(std::time::SystemTime::UNIX_EPOCH))
            .unwrap();
    }
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    for name in matched {
        assert!(!backups.join(name).exists(), "{name}");
    }
    for name in ["注意.txt", "pseudolife_lite_state-old.tar.gż"] {
        assert_eq!(fs::read(backups.join(name)).unwrap(), b"x");
    }
}

#[cfg(windows)]
fn junction(link: &Path, target: &Path) {
    let output = Command::new("cmd")
        .args(["/C", "mklink", "/J", &text(link), &text(target)])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
}

#[cfg(windows)]
#[test]
fn internal_junctions_roundtrip_as_python_directories() {
    let home = Home::new("junction");
    let bank = home.path().join("bank");
    fs::create_dir_all(bank.join("directory")).unwrap();
    fs::write(bank.join("directory/regular"), b"durable state").unwrap();
    fs::hard_link(bank.join("directory/regular"), bank.join("hardlink")).unwrap();
    junction(&bank.join("junction"), &bank.join("directory"));
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let archive = fs::read_dir(bank.join("backups"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let restored = home.path().join("restored");
    let output = Command::new(python()).args(["-c", r#"
import os, stat, sys, tarfile
from pathlib import Path
source, archive, restored = map(Path, sys.argv[1:])
with tarfile.open(archive) as tar:
    junction = tar.getmember('junction')
    assert junction.isdir(), junction
    tar.extractall(restored, filter='fully_trusted')
for name in ('directory', 'directory/regular', 'hardlink', 'junction', 'junction/regular'):
    a, b = os.lstat(source / name), os.lstat(restored / name)
    assert (stat.S_IFMT(a.st_mode), stat.S_IMODE(a.st_mode), a.st_uid, a.st_gid) == (stat.S_IFMT(b.st_mode), stat.S_IMODE(b.st_mode), b.st_uid, b.st_gid), name
    if stat.S_ISREG(a.st_mode):
        assert (source / name).read_bytes() == (restored / name).read_bytes(), name
assert os.path.samefile(restored / 'directory/regular', restored / 'hardlink')
assert os.path.samefile(restored / 'directory/regular', restored / 'junction/regular')
"#, &text(&bank), &text(&archive), &text(&restored)]).output().unwrap();
    assert!(output.status.success(), "{output:?}");
    fs::remove_dir(bank.join("junction")).unwrap();
}

#[cfg(windows)]
#[test]
fn windows_symlinks_roundtrip_without_following_external_targets() {
    let home = Home::new("windows-links");
    let bank = home.path().join("bank");
    fs::create_dir(&bank).unwrap();
    fs::write(home.path().join("outside"), b"outside state").unwrap();
    if !symlink_created(std::os::windows::fs::symlink_file(
        home.path().join("outside"),
        bank.join("file-link"),
    )) {
        return;
    }
    if !symlink_created(std::os::windows::fs::symlink_file(
        "absent",
        bank.join("dangling"),
    )) || !symlink_created(std::os::windows::fs::symlink_dir(
        home.path(),
        bank.join("directory-link"),
    )) {
        return;
    }
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let archive = fs::read_dir(bank.join("backups"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let restored = home.path().join("restored");
    let output = Command::new(python()).args(["-c", r#"
import os, stat, sys, tarfile
from pathlib import Path
source, archive, restored = map(Path, sys.argv[1:])
with tarfile.open(archive) as tar:
    assert all(member.issym() for member in tar.getmembers())
    tar.extractall(restored, filter='fully_trusted')
for name in ('file-link', 'dangling', 'directory-link'):
    a, b = os.lstat(source / name), os.lstat(restored / name)
    assert (stat.S_IFMT(a.st_mode), stat.S_IMODE(a.st_mode), a.st_uid, a.st_gid) == (stat.S_IFMT(b.st_mode), stat.S_IMODE(b.st_mode), b.st_uid, b.st_gid), name
    assert os.readlink(source / name) == os.readlink(restored / name), name
"#, &text(&bank), &text(&archive), &text(&restored)]).output().unwrap();
    assert!(output.status.success(), "{output:?}");
}

#[cfg(windows)]
#[test]
fn external_and_cyclic_junctions_defer_before_effects() {
    let home = Home::new("junction-boundary");
    let bank = home.path().join("bank");
    let outside = home.path().join("outside");
    fs::create_dir(&bank).unwrap();
    fs::create_dir(&outside).unwrap();
    for target in [&outside, &bank] {
        junction(&bank.join("junction"), target);
        let output = run(home.path(), &["--data-dir", &text(&bank)]);
        assert_eq!(output.status.code(), Some(1), "{output:?}");
        assert_eq!(output.stderr, cli::native_text(DEFERRED));
        assert!(!bank.join("backups").exists());
        fs::remove_dir(bank.join("junction")).unwrap();
    }
}

fn members(archive: &Path) -> Vec<(String, Vec<u8>)> {
    let file = fs::File::open(archive).unwrap();
    let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(file));
    let mut members = Vec::new();
    for entry in tar.entries().unwrap() {
        let mut entry = entry.unwrap();
        let name = entry
            .path()
            .unwrap()
            .display()
            .to_string()
            .replace('\\', "/");
        let mut data = Vec::new();
        entry.read_to_end(&mut data).unwrap();
        members.push((name, data));
    }
    members
}

#[test]
fn file_mode_archives_state_and_rotates_only_its_own_state_files() {
    let home = Home::new("file");
    let bank = home.path().join("bank");
    let backups = bank.join("backups");
    fs::create_dir_all(bank.join("notes-ü")).unwrap();
    fs::create_dir_all(bank.join("embedded_pg")).unwrap();
    fs::create_dir_all(&backups).unwrap();
    fs::write(bank.join("config.yaml"), "name: ✓\n").unwrap();
    fs::write(bank.join("B-top.txt"), "B").unwrap();
    fs::write(bank.join("a-top.txt"), "a").unwrap();
    fs::write(bank.join("notes-ü").join("a.txt"), "a").unwrap();
    fs::write(bank.join("notes-ü").join("B.txt"), "B").unwrap();
    fs::write(bank.join("embedded_pg").join("postmaster.opts"), "x").unwrap();
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(30 * 86400);
    for name in [
        "pseudolife_lite_state-20200101-000000.tar.gz",
        "pseudolife_lite_memory-20200101-000000.sql.gz",
        "pseudolife_memory-20200101-000000.sql.gz",
    ] {
        let file = fs::File::create(backups.join(name)).unwrap();
        file.set_modified(old).unwrap();
    }
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert!(output.stderr.is_empty());
    let stdout = String::from_utf8(output.stdout)
        .unwrap()
        .replace("\r\n", "\n");
    let mut lines = stdout.lines();
    assert_eq!(format!("{}\n", lines.next().unwrap()), SKIPPED);
    let state = lines
        .next()
        .unwrap()
        .strip_prefix("state archive: ")
        .unwrap()
        .to_owned();
    let rotated = backups.join("pseudolife_lite_state-20200101-000000.tar.gz");
    assert_eq!(
        lines.next(),
        Some(format!("rotated out:   {}", text(&rotated)).as_str())
    );
    assert_eq!(lines.next(), None);
    let name = Path::new(&state).file_name().unwrap().to_str().unwrap();
    assert!(name.starts_with("pseudolife_lite_state-") && name.ends_with(".tar.gz"));
    assert_eq!(Path::new(&state).parent(), Some(backups.as_path()));
    assert!(!rotated.exists());
    assert!(
        backups
            .join("pseudolife_lite_memory-20200101-000000.sql.gz")
            .exists()
    );
    assert!(
        backups
            .join("pseudolife_memory-20200101-000000.sql.gz")
            .exists()
    );
    let names: Vec<_> = members(Path::new(&state))
        .into_iter()
        .map(|(name, data)| (name, String::from_utf8(data).unwrap()))
        .collect();
    // Top level sorts as Python sorts Path objects (case-folded on Windows);
    // inside a directory, sorted(os.listdir()) is code-point order everywhere.
    let top: [(&str, &str); 2] = if cfg!(windows) {
        [("a-top.txt", "a"), ("B-top.txt", "B")]
    } else {
        [("B-top.txt", "B"), ("a-top.txt", "a")]
    };
    let expected = [
        top[0],
        top[1],
        ("config.yaml", "name: ✓\n"),
        ("notes-ü/", ""),
        ("notes-ü/B.txt", "B"),
        ("notes-ü/a.txt", "a"),
    ];
    let names: Vec<(&str, &str)> = names
        .iter()
        .map(|(n, d)| (n.as_str(), d.as_str()))
        .collect();
    assert_eq!(names, expected);
}

#[cfg(unix)]
#[test]
fn raw_names_and_symlink_targets_are_preserved() {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;
    let home = Home::new("raw-names");
    let bank = home.path().join("bank");
    fs::create_dir(&bank).unwrap();
    let raw = OsString::from_vec(b"sort-\xff".to_vec());
    fs::write(bank.join(&raw), b"raw state").unwrap();
    fs::write(bank.join("sort-🚀"), b"unicode state").unwrap();
    std::os::unix::fs::symlink(&raw, bank.join("raw-target")).unwrap();
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let archive = fs::read_dir(bank.join("backups"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let file = fs::File::open(archive).unwrap();
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(file));
    let names: Vec<_> = archive
        .entries()
        .unwrap()
        .map(|entry| {
            let entry = entry.unwrap();
            if entry.header().entry_type() == tar::EntryType::Symlink {
                assert_eq!(entry.link_name_bytes().unwrap().as_ref(), b"sort-\xff");
            }
            entry.path_bytes().into_owned()
        })
        .collect();
    assert_eq!(
        names,
        [
            b"raw-target".to_vec(),
            b"sort-\xff".to_vec(),
            "sort-🚀".as_bytes().to_vec()
        ]
    );
}

#[cfg(target_os = "linux")]
#[test]
fn device_members_roundtrip_when_mknod_is_permitted() {
    let python = std::env::var_os("PSEUDOLIFE_MCP_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("python3"));
    let home = Home::new("devices");
    let output = Command::new(&python).args(["-c", r#"
import errno, os, shutil, stat, subprocess, sys, tarfile
from pathlib import Path
home, executable = map(Path, sys.argv[1:])
bank = home / 'bank'
bank.mkdir()
# This disposable bank contains no database and never opens either device.
worker = r'''
import errno, os, shutil, stat, sys, tarfile
from pathlib import Path
bank, restored = map(Path, sys.argv[2:])
if sys.argv[1] == 'create':
    try:
        os.mknod(bank / 'character', stat.S_IFCHR | 0o640, os.makedev(1, 3))
        os.mknod(bank / 'block', stat.S_IFBLK | 0o600, os.makedev(7, 0))
    except OSError as error:
        for name in ('character', 'block'):
            (bank / name).unlink(missing_ok=True)
        if error.errno in (errno.EPERM, errno.EACCES):
            sys.exit(77)
        raise
elif sys.argv[1] == 'restore':
    try:
        archive = next((bank / 'backups').glob('*.tar.gz'))
        with tarfile.open(archive) as tar:
            members = {member.name: member for member in tar.getmembers()}
            assert members['character'].ischr() and members['block'].isblk()
            tar.extractall(restored, filter='fully_trusted')
        for name in ('character', 'block'):
            a, b = os.lstat(bank / name), os.lstat(restored / name)
            assert (a.st_mode, a.st_uid, a.st_gid, a.st_rdev) == (b.st_mode, b.st_uid, b.st_gid, b.st_rdev), name
    finally:
        shutil.rmtree(restored, ignore_errors=True)
elif sys.argv[1] == 'cleanup':
    for name in ('character', 'block'):
        (bank / name).unlink(missing_ok=True)
'''
restored = home / 'restored'
def unavailable(reason):
    if 'CI' in os.environ or 'GITHUB_ACTIONS' in os.environ:
        raise RuntimeError('CI device round-trip requires ' + reason)
    print('device restore unverified locally: ' + reason)
    sys.exit(77)
if os.geteuid() == 0:
    prefix = [sys.executable]
else:
    sudo = shutil.which('sudo')
    if sudo is None:
        unavailable('sudo availability')
    prefix = [sudo, '-n', sys.executable]
    probe = subprocess.run([*prefix, '-c', 'pass'], capture_output=True)
    if probe.returncode:
        unavailable('non-interactive sudo permission')
def run(action):
    return subprocess.run([*prefix, '-c', worker, action, str(bank), str(restored)])
created = run('create')
if created.returncode == 77:
    unavailable('mknod permission')
created.check_returncode()
try:
    subprocess.run([str(executable), 'backup', '--data-dir', str(bank)], cwd=home,
        env={'HOME': str(home), 'COLUMNS': '80'}, check=True)
    run('restore').check_returncode()
finally:
    run('cleanup').check_returncode()
"#, &text(home.path()), env!("CARGO_BIN_EXE_pseudolife-stdio")]).output().unwrap();
    if output.status.code() == Some(77) && !in_ci() {
        eprintln!("{}", String::from_utf8_lossy(&output.stdout));
        return;
    }
    assert!(output.status.success(), "{output:?}");
    if !output.stdout.is_empty() {
        eprintln!("{}", String::from_utf8_lossy(&output.stdout));
    }
}

#[cfg(unix)]
#[test]
fn matching_raw_rotation_names_defer_before_utf8_stdout_failure() {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;
    let home = Home::new("raw-rotation-output");
    let bank = home.path().join("bank");
    let backups = bank.join("backups");
    fs::create_dir_all(&backups).unwrap();
    let raw = OsString::from_vec(b"pseudolife_lite_state-old-\xff.tar.gz".to_vec());
    fs::write(backups.join(raw), b"old state").unwrap();
    assert_deferred(&home, &["--data-dir", &text(&bank)]);
}

#[cfg(windows)]
#[test]
fn windows_symlink_flags_that_python_cannot_restore_defer_before_effects() {
    use std::os::windows::ffi::OsStrExt;
    let home = Home::new("link-flags");
    let bank = home.path().join("bank");
    fs::create_dir(&bank).unwrap();
    let link = bank.join("link");
    if !symlink_created(std::os::windows::fs::symlink_dir("absent", &link)) {
        return;
    }
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(output.stderr, cli::native_text(DEFERRED));
    assert!(!bank.join("backups").exists());
    fs::remove_dir(&link).unwrap();
    fs::create_dir(bank.join("z-directory")).unwrap();
    if !symlink_created(std::os::windows::fs::symlink_dir("z-directory", &link)) {
        return;
    }
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(output.stderr, cli::native_text(DEFERRED));
    assert!(!bank.join("backups").exists());
    fs::remove_dir(&link).unwrap();
    let mut long_target = home.path().join("long-target");
    while long_target.as_os_str().encode_wide().count() < 280 {
        long_target.push("directory-component");
    }
    fs::create_dir_all(&long_target).unwrap();
    let long_target = fs::canonicalize(long_target).unwrap();
    if !symlink_created(std::os::windows::fs::symlink_dir(&long_target, &link)) {
        return;
    }
    let output = run(home.path(), &["--data-dir", &text(&bank)]);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(output.stderr, cli::native_text(DEFERRED));
    assert!(!bank.join("backups").exists());
    fs::remove_dir(&link).unwrap();
}

#[cfg(windows)]
#[test]
fn nested_self_junction_cycle_defers_with_output_outside_the_bank() {
    let home = Home::new("nested-cycle");
    let bank = home.path().join("bank");
    let directory = bank.join("a");
    let out = home.path().join("outside-backups");
    fs::create_dir_all(&directory).unwrap();
    let link = directory.join("j");
    junction(&link, &directory);
    assert_deferred(&home, &["--data-dir", &text(&bank), "--out", &text(&out)]);
    assert!(!out.exists());
    fs::remove_dir(link).unwrap();
}
