#![forbid(unsafe_code)]
//! `pseudolife-mcp maintainer` paths that need no database: every argv shape
//! outside the canonical five, and every bank the native leaf does not
//! reach (no, empty or unsupported DSN; an unreachable server), defer with
//! the dispatcher's line before any effect. Bank-backed behaviour is proven
//! by the CLI harness row `maintainer` against the Python oracle.
#[path = "common/cli.rs"]
mod cli;
use std::{fs, path::Path, process::Command};

fn command(home: &Path) -> Command {
    let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("maintainer")
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("PSEUDOLIFE_DAEMON_EXEC", "1")
        .env("PSEUDOLIFE_MCP_DATA_DIR", home.join("no-bank"));
    command
}

fn assert_native_error(arguments: &[&str], dsn: Option<&str>) {
    let home = std::env::temp_dir().join(format!("maintainer-defer-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let mut command = command(&home);
    if let Some(dsn) = dsn {
        command.env("PSEUDOLIFE_MCP_DATABASE_URL", dsn);
    }
    let output = command.args(arguments).output().unwrap();
    let remaining = fs::read_dir(&home).unwrap().count();
    fs::remove_dir_all(&home).unwrap();
    assert_eq!(output.status.code(), Some(2), "{arguments:?}");
    assert!(output.stdout.is_empty(), "{arguments:?}");
    assert!(output.stderr.starts_with(&cli::native_text("error: ")));
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(!error.contains("is deferred in this candidate"));
    assert!(
        error.contains("use ")
            || error.contains("run ")
            || error.contains("set ")
            || error.contains("spell ")
    );
    if let Some(value) = dsn {
        assert!(!error.contains(value), "database URL was printed");
    }
    assert_eq!(remaining, 0, "{arguments:?} wrote under the home");
}

/// A DSN no server answers: the closed discard port on loopback.
const UNREACHABLE: &str = "postgresql://user:secret@127.0.0.1:9/pl_cf_none?sslmode=disable";

#[test]
fn help_does_not_open_the_bank_or_enter_the_container() {
    let home = std::env::temp_dir().join(format!("maintainer-help-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    for args in [
        &["--help"][..],
        &["list", "--help"],
        &["enrol-code", "--help"],
    ] {
        let output = command(&home)
            .args(args)
            .env("PSEUDOLIFE_MCP_DATABASE_URL", UNREACHABLE)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert!(
            output
                .stdout
                .starts_with(b"usage: pseudolife-mcp maintainer")
        );
        assert!(output.stderr.is_empty());
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn non_canonical_argv_refuses_before_the_bank() {
    for arguments in [
        &[][..],
        &["list", "extra"],
        &["enrol-code", "--no-w"],
        &["confirm"],
        &["confirm", "--"],
        &["revoke", "abcdef", "ghijkl"],
        &["reset", "--yes", "--yes"],
        &["reset", "-y"],
        &["lis"],
    ] {
        // Even with a reachable-looking DSN: nothing connects first.
        assert_native_error(arguments, Some(UNREACHABLE));
    }
}

#[test]
fn canonical_argv_without_a_bank_reports_the_native_error() {
    for arguments in [
        &["list"][..],
        &["enrol-code"],
        &["enrol-code", "--no-wait"],
        &["confirm", "-abcdef"],
        &["revoke", "abcdef"],
        &["reset"],
        &["reset", "--yes"],
    ] {
        assert_native_error(
            arguments,
            Some("postgresql://user:secret@127.0.0.1:9/db?application_name=x"),
        );
        for dsn in [None, Some(""), Some(UNREACHABLE)] {
            let home =
                std::env::temp_dir().join(format!("maintainer-no-bank-{}", uuid::Uuid::new_v4()));
            fs::create_dir(&home).unwrap();
            let mut command = command(&home);
            if let Some(dsn) = dsn {
                command.env("PSEUDOLIFE_MCP_DATABASE_URL", dsn);
            }
            let output = command.args(arguments).output().unwrap();
            assert_eq!(output.status.code(), Some(2));
            assert!(output.stdout.is_empty());
            let error = String::from_utf8_lossy(&output.stderr);
            assert!(error.starts_with("error: ") && error.contains("PSEUDOLIFE_MCP_DATABASE_URL"));
            assert!(!error.contains("secret"));
            assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
            fs::remove_dir_all(home).unwrap();
        }
    }
}

#[test]
fn non_unicode_database_setting_is_refused_without_falling_through() {
    use std::ffi::OsString;
    #[cfg(unix)]
    let value = {
        use std::os::unix::ffi::OsStringExt;
        OsString::from_vec(vec![0xff])
    };
    #[cfg(windows)]
    let value = {
        use std::os::windows::ffi::OsStringExt;
        OsString::from_wide(&[0xd800])
    };
    let home = std::env::temp_dir().join(format!("maintainer-setting-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let output = command(&home)
        .arg("list")
        .env("PSEUDOLIFE_MCP_DATABASE_URL", value)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let error = String::from_utf8_lossy(&output.stderr);
    assert!(error.contains("PSEUDOLIFE_MCP_DATABASE_URL") && error.contains("Unicode"));
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn malformed_database_settings_never_echo_credentials_or_option_keys() {
    const MARKER: &str = "SYNTHETIC_PRIVATE_VALUE_6312";
    let home = std::env::temp_dir().join(format!("maintainer-private-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    for value in [
        format!("postgresql://fixture:{MARKER}@127.0.0.1:9/pl_cf_none?application_name=x"),
        format!("postgresql://fixture:fixture@127.0.0.1:9/pl_cf_none?{MARKER}=x"),
        format!("invalid-{MARKER}"),
    ] {
        let output = command(&home)
            .arg("list")
            .env("PSEUDOLIFE_MCP_DATABASE_URL", &value)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let error = String::from_utf8_lossy(&output.stderr);
        assert!(error.contains("PSEUDOLIFE_MCP_DATABASE_URL"));
        assert!(
            !error.contains(MARKER) && !error.contains(&value),
            "private input was printed"
        );
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(home).unwrap();
}
