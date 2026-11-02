#![forbid(unsafe_code)]
//! `pseudolife-mcp maintainer` paths that need no database: every argv shape
//! outside the canonical five, and every bank the native leaf does not
//! reach (no, empty or unsupported DSN; an unreachable server), defer with
//! the dispatcher's line before any effect. Bank-backed behaviour is proven
//! by the CLI harness row `maintainer` against the Python oracle.
#[path = "common/cli.rs"]
mod cli;
use std::{fs, path::Path, process::Command};

const DEFERRED: &str = "pseudolife-stdio: mode 'maintainer' is deferred in this candidate\n";

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

fn assert_deferred(arguments: &[&str], dsn: Option<&str>) {
    let home = std::env::temp_dir().join(format!("maintainer-defer-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let mut command = command(&home);
    if let Some(dsn) = dsn {
        command.env("PSEUDOLIFE_MCP_DATABASE_URL", dsn);
    }
    let output = command.args(arguments).output().unwrap();
    let remaining = fs::read_dir(&home).unwrap().count();
    fs::remove_dir_all(&home).unwrap();
    assert_eq!(output.status.code(), Some(1), "{arguments:?} {dsn:?}");
    assert!(output.stdout.is_empty(), "{arguments:?} {dsn:?}");
    assert_eq!(
        output.stderr,
        cli::native_text(DEFERRED),
        "{arguments:?} {dsn:?}"
    );
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
fn non_canonical_argv_defers_before_the_bank() {
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
        assert_deferred(arguments, Some(UNREACHABLE));
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
        assert_deferred(
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
