#![forbid(unsafe_code)]
//! Host maintainer argument and connection-setting refusals, with isolated
//! homes and closed loopback endpoints. Bank-backed behavior is exercised
//! by the native maintainer row on guarded disposable databases.
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

fn assert_native_error(arguments: &[&str], dsn: Option<&str>, expected: &str) {
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
    assert_eq!(output.stderr, cli::native_text(expected), "{arguments:?}");
    let error = String::from_utf8_lossy(&output.stderr);
    if let Some(value) = dsn.filter(|value| !value.is_empty()) {
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
    for (arguments, refusal) in [
        (
            &[][..],
            "an action is required; run pseudolife-mcp maintainer --help",
        ),
        (
            &["list", "extra"],
            "list takes no arguments; use pseudolife-mcp maintainer list",
        ),
        (
            &["enrol-code", "--no-w"],
            "abbreviated option; use --no-wait in full",
        ),
        (
            &["confirm"],
            "confirm needs one credential prefix; use pseudolife-mcp maintainer confirm -- PREFIX",
        ),
        (
            &["confirm", "--"],
            "confirm needs one credential prefix; use pseudolife-mcp maintainer confirm -- PREFIX",
        ),
        (
            &["revoke", "abcdef", "ghijkl"],
            "revoke needs one credential prefix; use pseudolife-mcp maintainer revoke -- PREFIX",
        ),
        (
            &["reset", "--yes", "--yes"],
            "reset accepts --yes once; use pseudolife-mcp maintainer reset --yes",
        ),
        (&["reset", "-y"], "abbreviated option; use --yes in full"),
        (
            &["lis"],
            "abbreviated maintainer action; spell list in full",
        ),
    ] {
        assert_native_error(arguments, Some(UNREACHABLE), &format!("error: {refusal}\n"));
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
        for dsn in [None, Some(""), Some(UNREACHABLE)] {
            let expected = if dsn.is_some_and(|value| !value.is_empty()) {
                "error: OperationalError; check PSEUDOLIFE_MCP_DATABASE_URL\n"
            } else {
                "error: no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp maintainer ...` on its host)\n"
            };
            assert_native_error(arguments, dsn, expected);
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
    assert_eq!(
        output.stderr,
        cli::native_text(
            "error: PSEUDOLIFE_MCP_DATABASE_URL is not Unicode; set it to a Unicode PostgreSQL URL\n"
        )
    );
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn malformed_database_settings_never_echo_credentials_or_option_keys() {
    const MARKER: &str = "SYNTHETIC_PRIVATE_VALUE_6312";
    for (value, refusal) in [
        (
            format!("postgresql://fixture:fixture@127.0.0.1:9/pl_cf_none?{MARKER}=x"),
            "has an unsupported option; use only host, port, user, password, dbname, sslmode, sslrootcert, application_name and connect_timeout",
        ),
        (
            format!("invalid-{MARKER}"),
            "is not an admitted PostgreSQL connection setting; set it to a valid single-host PostgreSQL URL",
        ),
        (
            format!("postgresql://fixture:{MARKER}@127.0.0.1:9/db?sslmode=invalid"),
            "has an unsupported sslmode; use disable, prefer, require, verify-ca or verify-full",
        ),
        (
            format!("host=one,two user=fixture password={MARKER}"),
            "needs one TCP host; set it to a single-host PostgreSQL URL",
        ),
        (
            format!("postgresql://fixture:{MARKER}@127.0.0.1:9/db?port=0"),
            "has an invalid port; set its port to 1 through 65535",
        ),
        (
            format!("postgresql://fixture:{MARKER}@127.0.0.1:9/db?connect_timeout=invalid"),
            "has an invalid connect_timeout; use nonnegative whole seconds",
        ),
    ] {
        assert_native_error(
            &["list"],
            Some(&value),
            &format!("error: PSEUDOLIFE_MCP_DATABASE_URL {refusal}\n"),
        );
    }
}

#[test]
fn ordinary_connection_options_are_admitted_before_connection() {
    let home = std::env::temp_dir().join(format!("maintainer-options-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    for query in [
        "application_name=",
        "application_name=fixture",
        "connect_timeout=1",
        "application_name=&connect_timeout=1",
    ] {
        let value =
            format!("postgresql://fixture:fixture@127.0.0.1:9/pl_cf_none?sslmode=disable&{query}");
        let output = command(&home)
            .arg("list")
            .env("PSEUDOLIFE_MCP_DATABASE_URL", value)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            cli::native_text("error: OperationalError; check PSEUDOLIFE_MCP_DATABASE_URL\n")
        );
    }
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn invalid_waiting_poll_refuses_before_connection_and_nearby_value_is_admitted() {
    let refusal = "error: --poll needs finite seconds greater than 0 and below 18446744073709551616; use --poll 2\n";
    for value in ["-1", "0", "-0", "nan", "NaN", "inf", "-inf", "1e300"] {
        // A different settings error would prove that parsing ran too late.
        assert_native_error(
            &["enrol-code", "--poll", value],
            Some("invalid-dsn"),
            refusal,
        );
        assert_native_error(
            &["enrol-code", &format!("--poll={value}")],
            Some("invalid-dsn"),
            refusal,
        );
    }
    assert_native_error(
        &["enrol-code", "--poll", "0.01"],
        Some(UNREACHABLE),
        "error: OperationalError; check PSEUDOLIFE_MCP_DATABASE_URL\n",
    );
}
