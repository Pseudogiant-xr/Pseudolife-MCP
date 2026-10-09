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
fn non_canonical_argv_defers_before_the_bank() {
    for arguments in [
        &[][..],
        &["setup"],
        &["setup", "--check"],
        &["setup", "--local", "--yes"],
        &["--help"],
        &["-h"],
        &["list", "--help"],
        &["list", "extra"],
        &["enrol-code", "--poll", "0.01"],
        &["enrol-code", "--poll=0.01", "--no-wait"],
        &["enrol-code", "--no-wait", "--no-wait"],
        &["enrol-code", "--no-w"],
        &["confirm"],
        &["confirm", "-h"],
        &["confirm", "--"],
        &["confirm", "--", "-abcdef"],
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
fn canonical_argv_without_a_native_bank_defers() {
    for arguments in [
        &["list"][..],
        &["enrol-code"],
        &["enrol-code", "--no-wait"],
        &["confirm", "-abcdef"],
        &["revoke", "abcdef"],
        &["reset"],
        &["reset", "--yes"],
    ] {
        // No DSN (the lite bank and the daemon container are deferred), an
        // empty one, a DSN option the native client does not admit, and a
        // server that does not answer.
        assert_deferred(arguments, None);
        assert_deferred(arguments, Some(""));
        assert_deferred(
            arguments,
            Some("postgresql://user:secret@127.0.0.1:9/db?application_name=x"),
        );
        assert_deferred(arguments, Some(UNREACHABLE));
    }
}
