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
    assert_eq!(
        existing.stderr,
        cli::native_text("pseudolife-stdio: mode 'backup' is deferred in this candidate\n")
    );
}

fn assert_missing_spelling(home: &Path, argument: &str, expected: &str) {
    let before = fs::read_dir(home).unwrap().count();
    let output = command(home)
        .arg("--data-dir")
        .arg(argument)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        cli::native_text(&format!(
            "data dir {expected} does not exist — nothing to back up.\n"
        ))
    );
    assert!(!Path::new(argument).exists());
    assert_eq!(fs::read_dir(home).unwrap().count(), before);
}

#[test]
fn missing_directory_spelling_collapses_separators_and_dots() {
    let home = std::env::temp_dir().join(format!("backup-spelling-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let argument = format!("{}/./missing-µ//", home.display()).replace('\\', "/");
    assert_missing_spelling(
        &home,
        &argument,
        &home.join("missing-µ").display().to_string(),
    );
    let existing = format!("{}//./", home.display()).replace('\\', "/");
    let output = command(&home)
        .arg("--data-dir")
        .arg(existing)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        cli::native_text("pseudolife-stdio: mode 'backup' is deferred in this candidate\n")
    );
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(&home).unwrap();
}

#[test]
fn missing_directory_spelling_preserves_parent_components() {
    let home = std::env::temp_dir().join(format!("backup-parent-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    fs::create_dir(home.join("anchor")).unwrap();
    let argument = format!("{}/anchor/../missing.µ/", home.display()).replace('\\', "/");
    let expected = home.join("anchor").join("..").join("missing.µ");
    assert_missing_spelling(&home, &argument, &expected.display().to_string());
    assert_eq!(fs::read_dir(&home).unwrap().count(), 1);
    fs::remove_dir_all(&home).unwrap();
}

#[cfg(not(windows))]
#[test]
fn missing_directory_spelling_preserves_exactly_two_leading_slashes() {
    let home = std::env::temp_dir().join(format!("backup-root-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let tail = home.to_str().unwrap().trim_start_matches('/');
    for (root, printed_root) in [("//", "//"), ("///", "/")] {
        let argument = format!("{root}{tail}//missing/");
        let expected = format!("{printed_root}{tail}/missing");
        assert_missing_spelling(&home, &argument, &expected);
    }
    fs::remove_dir_all(&home).unwrap();
}

#[cfg(windows)]
#[test]
fn missing_directory_spelling_preserves_extended_drive_prefix() {
    let home = std::env::temp_dir().join(format!("backup-prefix-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let argument = format!("\\\\?\\{}\\\\missing-µ\\", home.display());
    let expected = format!("\\\\?\\{}\\missing-µ", home.display());
    assert_missing_spelling(&home, &argument, &expected);
    fs::remove_dir_all(&home).unwrap();
}
