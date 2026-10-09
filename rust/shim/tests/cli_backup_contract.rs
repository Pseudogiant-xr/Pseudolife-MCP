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
fn hard_links_in_the_tree_defer() {
    let home = Home::new("hardlink");
    let bank = home.path().join("bank");
    fs::create_dir_all(bank.join("sub")).unwrap();
    fs::write(bank.join("sub").join("a"), "x").unwrap();
    fs::hard_link(bank.join("sub").join("a"), bank.join("b")).unwrap();
    assert_deferred(&home, &["--data-dir", &text(&bank)]);
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
#[test]
fn a_tz_override_defers_on_windows() {
    let home = Home::new("tz");
    fs::create_dir(home.path().join("bank")).unwrap();
    let mut command = command(home.path());
    command
        .env("TZ", "UTC")
        .args(["--data-dir", &text(&home.path().join("bank"))]);
    let before = home.listing();
    let output = command.output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(output.stderr, cli::native_text(DEFERRED));
    assert_eq!(home.listing(), before);
}

#[cfg(windows)]
#[test]
fn non_ascii_names_in_the_backups_dir_defer_on_windows() {
    let home = Home::new("glob");
    let backups = home.path().join("bank").join("backups");
    fs::create_dir_all(&backups).unwrap();
    fs::write(
        backups.join("pseudolıfe_lite_state-20200101-000000.tar.gz"),
        "x",
    )
    .unwrap();
    assert_deferred(&home, &["--data-dir", &text(&home.path().join("bank"))]);
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
