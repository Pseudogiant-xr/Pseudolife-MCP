#![forbid(unsafe_code)]
#[path = "common/cli.rs"]
mod cli;
use std::{fs, path::Path, process::Command};

fn command(home: &Path) -> Command {
    let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("backup")
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("COLUMNS", "80");
    command
}

#[test]
fn captured_public_help_precedes_bank_resolution() {
    let home = std::env::temp_dir().join(format!("backup-help-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let output = command(&home).arg("--help").output().unwrap();
    let remaining = fs::read_dir(&home).unwrap().count();
    fs::remove_dir_all(&home).unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        output.stdout,
        cli::native_text(include_str!("../src/cli/backup_help.txt"))
    );
    assert!(output.stderr.is_empty());
    assert_eq!(remaining, 0);
}

#[test]
fn help_at_another_width_defers() {
    let home = std::env::temp_dir().join(format!("backup-width-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    for columns in [Some("120"), None] {
        let mut command = command(&home);
        match columns {
            Some(value) => command.env("COLUMNS", value),
            None => command.env_remove("COLUMNS"),
        };
        let output = command.arg("--help").output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, cli::native_text(DEFERRED));
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(&home).unwrap();
}

#[test]
fn missing_directory_refusal_uses_the_real_filesystem() {
    let home = std::env::temp_dir().join(format!("backup-missing-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let absent = home.join("missing-bank");
    let output = command(&home)
        .arg("--data-dir")
        .arg(&absent)
        .output()
        .unwrap();
    let remaining = fs::read_dir(&home).unwrap().count();
    let still_absent = !absent.exists();
    let existing = command(&home)
        .arg("--data-dir")
        .arg(&home)
        .output()
        .unwrap();
    fs::remove_dir_all(&home).unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        cli::native_text(&format!(
            "data dir {} does not exist — nothing to back up.\n",
            absent.display()
        ))
    );
    assert!(still_absent);
    assert_eq!(remaining, 0);
    assert_eq!(existing.status.code(), Some(1));
    assert_eq!(existing.stderr, cli::native_text(DEFERRED));
}

const DEFERRED: &str = "pseudolife-stdio: mode 'backup' is deferred in this candidate\n";

fn assert_missing_spelling(home: &Path, argument: &str, expected: &str) {
    let before = fs::read_dir(home).unwrap().count();
    let output = command(home)
        .arg("--data-dir")
        .arg(argument)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, cli::native_text(expected));
    assert!(!Path::new(argument).exists());
    assert_eq!(fs::read_dir(home).unwrap().count(), before);
}

#[test]
fn canonical_parent_components_are_echoed_verbatim() {
    let home = std::env::temp_dir().join(format!("backup-parent-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    fs::create_dir(home.join("anchor")).unwrap();
    let argument = home.join("anchor").join("..").join("missing.µ");
    let argument = argument.to_str().unwrap();
    assert_missing_spelling(
        &home,
        argument,
        &format!("data dir {argument} does not exist — nothing to back up.\n"),
    );
    assert_eq!(fs::read_dir(&home).unwrap().count(), 1);
    fs::remove_dir_all(&home).unwrap();
}

#[test]
fn noncanonical_spellings_defer_without_effects() {
    let home = std::env::temp_dir().join(format!("backup-spelling-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let base = home.to_str().unwrap();
    let separator = std::path::MAIN_SEPARATOR;
    for argument in [
        format!("{base}{separator}.{separator}missing-µ"),
        format!("{base}{separator}{separator}missing-µ"),
        format!("{base}{separator}missing-µ{separator}"),
        String::from("relative-missing-µ"),
    ] {
        assert_missing_spelling(&home, &argument, DEFERRED);
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(&home).unwrap();
}

#[cfg(not(windows))]
#[test]
fn leading_double_slash_defers() {
    let home = std::env::temp_dir().join(format!("backup-root-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let argument = format!("/{}/missing", home.display());
    assert_missing_spelling(&home, &argument, DEFERRED);
    fs::remove_dir_all(&home).unwrap();
}

#[cfg(windows)]
#[test]
fn extended_and_unc_prefixes_defer_without_effects() {
    let home = std::env::temp_dir().join(format!("backup-prefix-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    for argument in [
        format!("{}/missing-µ", home.display()),
        format!("\\\\?\\{}\\missing-µ", home.display()),
        String::from("\\\\localhost\\absent-share-µ\\missing"),
    ] {
        assert_missing_spelling(&home, &argument, DEFERRED);
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(&home).unwrap();
}
