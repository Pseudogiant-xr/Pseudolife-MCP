#![forbid(unsafe_code)]
//! `test-login create` without a database: the refusals that come before any
//! connection, and every native refusal. Server-backed behaviour (the --admin-url
//! path on a throwaway cluster, the container path on a disposable container)
//! is proven against the Python oracle by `rust/cli_harness --row test_login`.
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

/// A loopback port nothing listens on: a case that failed to defer would
/// answer with a connection refusal, never reach a server.
const CLOSED_URL: &str = "postgresql://postgres@127.0.0.1:1/postgres";
const ABSENT: &str = "pl-cf-w1c-testlogin-absent";

#[test]
fn help_does_not_require_an_admin_connection_or_write_a_login() {
    let home = Home::new("help");
    for arguments in [&[][..], &["--help"], &["create", "--help"]] {
        let output = run(home.path(), arguments, &[]);
        assert_eq!(output.status.code(), Some(0));
        assert!(
            output
                .stdout
                .starts_with(b"usage: pseudolife-mcp test-login")
        );
        assert!(output.stderr.is_empty());
    }
    assert!(home.listing().is_empty());
}

struct Home(PathBuf);

impl Home {
    fn new(label: &str) -> Self {
        let path =
            std::env::temp_dir().join(format!("test-login-{label}-{}", uuid::Uuid::new_v4()));
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

/// A command with a disposable home, no PATH (so no docker) and no
/// PostgreSQL environment.
fn command(home: &Path) -> Command {
    let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("test-login")
        .current_dir(home)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("APPDATA", home.join("AppData"));
    command
}

fn run(home: &Path, arguments: &[&str], env: &[(&str, &str)]) -> Output {
    let mut command = command(home);
    for (key, value) in env {
        command.env(key, value);
    }
    command.args(arguments).output().unwrap()
}

const FILE_CANONICAL: &str = "--file/PSEUDOLIFE_TEST_PG_LOGIN_FILE is non-canonical or contains control characters; use a canonical printable file path";
const ENV_FILE_CANONICAL: &str = "PSEUDOLIFE_TEST_PG_LOGIN_FILE is non-canonical; use a path without dot components, repeated or trailing separators";
const HOME_CAUSE: &str = "HOME/USERPROFILE is missing, non-Unicode or non-canonical; set an absolute canonical home directory";
const DAEMON_CAUSE: &str = "PSEUDOLIFE_MCP_DATABASE_URL cannot be read unambiguously; use a supported PostgreSQL DSN with user and database in the authority and path";
const PATH_CAUSE: &str =
    "PATH is missing or not Unicode; set a Unicode PATH containing Docker or supply --admin-url";
const FILE_PREFIX: &str = "--file/PSEUDOLIFE_TEST_PG_LOGIN_FILE ";
const ANCESTOR_CAUSE: &str =
    "ancestor is a link or not a directory; choose a path through real directories";

// Parser errors are plain LF stderr; parsed preflight reports use stdout's
// platform text encoding. Every caller pins both status and the whole cause.
fn assert_refused(home: &Home, arguments: &[&str], env: &[(&str, &str)], code: i32, cause: &str) {
    let before = home.listing();
    let output = run(home.path(), arguments, env);
    assert_error(&output, code, cause);
    assert_eq!(home.listing(), before);
}

fn assert_error(output: &Output, code: i32, cause: &str) {
    assert_eq!(output.status.code(), Some(code));
    let expected = format!("test-login: {cause}\n");
    if code == 2 {
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, expected.as_bytes());
    } else {
        assert_eq!(output.stdout, cli::native_text(&expected));
        assert!(output.stderr.is_empty());
    }
    for bytes in [&output.stdout, &output.stderr] {
        let text = String::from_utf8_lossy(bytes);
        assert!(!text.contains("fixture-secret"));
        assert!(!text.contains("postgresql://"));
        assert!(!text.contains("deferred"));
    }
}

fn assert_answer(home: &Home, arguments: &[&str], env: &[(&str, &str)], code: i32, stdout: &str) {
    let before = home.listing();
    let output = run(home.path(), arguments, env);
    assert_eq!(output.status.code(), Some(code), "{arguments:?}");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&cli::native_text(stdout)),
        "{arguments:?}"
    );
    assert!(output.stderr.is_empty(), "{arguments:?}");
    assert_eq!(home.listing(), before, "{arguments:?} changed the home");
}

#[test]
fn malformed_argv_is_refused_before_any_effect() {
    let home = Home::new("argv");
    for (arguments, code, cause) in [
        (
            vec!["--json", "create"],
            2,
            "expected create; run test-login --help for usage",
        ),
        (
            vec!["create", "extra"],
            2,
            "unexpected argument; run test-login create --help for supported options",
        ),
        (
            vec!["create", "--rot"],
            2,
            "abbreviated option is unsupported; use --rotate",
        ),
        (
            vec!["create", "--rotate", "--rotate", "--admin-url", CLOSED_URL],
            2,
            "--rotate was repeated; supply it once",
        ),
        (
            vec!["create", "--json", "--json", "--admin-url", CLOSED_URL],
            2,
            "--json was repeated; supply it once",
        ),
        (
            vec![
                "create",
                "--role",
                "a",
                "--role",
                "b",
                "--admin-url",
                CLOSED_URL,
            ],
            2,
            "--role was repeated; supply it once",
        ),
        (
            vec!["create", "--role", "-x"],
            2,
            "--role value must not start with '-'; supply a non-option value",
        ),
        (
            vec!["create", "--bank", "-x"],
            2,
            "--bank value must not start with '-'; supply a non-option value",
        ),
        (
            vec!["create", "--bank", ""],
            2,
            "--bank requires a nonempty value; supply --bank=VALUE",
        ),
        (
            vec!["create", "--admin-url", ""],
            2,
            "--admin-url requires a nonempty value; supply --admin-url=VALUE",
        ),
        (
            vec![
                "create",
                "--admin-url",
                CLOSED_URL,
                "--admin-url",
                CLOSED_URL,
            ],
            2,
            "--admin-url was repeated; supply it once",
        ),
        (
            vec!["create", "--container", ""],
            2,
            "--container requires a nonempty value; supply --container=VALUE",
        ),
        (
            vec!["create", "--file", ""],
            2,
            "--file requires a nonempty value; supply --file=VALUE",
        ),
        (
            vec!["create", "--file", "a/../b.env", "--admin-url", CLOSED_URL],
            4,
            FILE_CANONICAL,
        ),
        (
            vec!["create", "--file", "./b.env", "--admin-url", CLOSED_URL],
            4,
            FILE_CANONICAL,
        ),
        (
            vec!["create", "--", "--admin-url", CLOSED_URL],
            2,
            "unexpected argument; run test-login create --help for supported options",
        ),
        (
            vec!["create", "--file", "a", "--file", "b"],
            2,
            "--file was repeated; supply it once",
        ),
    ] {
        assert_refused(&home, &arguments, &[], code, cause);
    }
}

#[test]
fn ambiguous_abbreviations_name_every_matching_option() {
    let home = Home::new("ambiguous");
    assert_refused(
        &home,
        &["create", "--ro", "fixture-secret"],
        &[],
        2,
        "ambiguous option abbreviation; use --role or --rotate",
    );
    assert_refused(
        &home,
        &["create", "--rol", "fixture-secret"],
        &[],
        2,
        "abbreviated option is unsupported; use --role",
    );
    assert_refused(
        &home,
        &["create", "--rot"],
        &[],
        2,
        "abbreviated option is unsupported; use --rotate",
    );
    assert_refused(
        &home,
        &["create", "--json", "--ro"],
        &[],
        2,
        "ambiguous option abbreviation; use --role or --rotate",
    );
}

#[test]
fn missing_values_are_distinct_from_non_unicode_values() {
    let home = Home::new("missing");
    for option in ["--role", "--file", "--admin-url", "--container", "--bank"] {
        assert_refused(
            &home,
            &["create", option],
            &[],
            2,
            &format!("{option} requires a value; supply {option}=VALUE"),
        );
    }
}

#[test]
fn boolean_flags_refuse_values_and_accept_the_flag_alone() {
    let home = Home::new("boolean");
    for option in ["--rotate", "--json"] {
        let with_value = format!("{option}=fixture-secret");
        assert_refused(
            &home,
            &["create", &with_value],
            &[],
            2,
            &format!("{option} takes no value; use {option} alone"),
        );
        let output = run(
            home.path(),
            &["create", option, "--admin-url", CLOSED_URL],
            &[],
        );
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stderr.is_empty());
        if option == "--json" {
            let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(value["exit"], 1);
            assert!(
                value["error"]
                    .as_str()
                    .unwrap()
                    .starts_with("the database refused:")
            );
        } else {
            assert!(
                String::from_utf8_lossy(&output.stdout)
                    .starts_with("test-login: the database refused:")
            );
        }
    }
    assert!(home.listing().is_empty());
}

#[test]
fn administrator_dsn_refusals_name_the_bad_setting_without_values() {
    let home = Home::new("admin-grammar");
    for (url, cause) in [
        (
            "postgresql:///postgres",
            "--admin-url has an invalid host setting; supply a supported PostgreSQL TCP DSN",
        ),
        (
            "postgresql://u:fixture-secret@127.0.0.1:1/db?application_name=x",
            "--admin-url has unsupported option application_name; remove that option and supply a supported PostgreSQL TCP DSN",
        ),
        (
            "host=127.0.0.1 port=1 service=fixture-secret",
            "--admin-url has unsupported option service; remove that option and supply a supported PostgreSQL TCP DSN",
        ),
        (
            "postgresql://u:fixture-secret@127.0.0.1:1/db#frag",
            "--admin-url is not a supported PostgreSQL TCP DSN; supply a PostgreSQL DSN with one explicit TCP host",
        ),
    ] {
        assert_refused(&home, &["create", "--admin-url", url], &[], 4, cause);
    }
}

#[test]
fn unsupported_settings_are_refused_before_connecting() {
    let home = Home::new("shapes");
    let create = ["create", "--admin-url", CLOSED_URL];
    for name in ["PGHOST", "PGUSER", "PGSSLMODE", "PGPASSFILE", "PGSERVICE"] {
        assert_refused(
            &home,
            &create,
            &[(name, "fixture-secret")],
            4,
            &format!(
                "{name} is unsupported with --admin-url; unset that PostgreSQL environment control"
            ),
        );
    }
    #[cfg(windows)]
    assert_refused(
        &home,
        &create,
        &[("pghost", "fixture-secret")],
        4,
        "pghost is unsupported with --admin-url; unset that PostgreSQL environment control",
    );
    for dsn in [
        "postgresql:///pseudolife_memory",
        "postgresql://u@h/db?user=v",
        "postgresql://u@h/db?dbname=other",
        "host=h user=a xuser=b",
        " postgresql://u@h/db",
    ] {
        assert_refused(
            &home,
            &create,
            &[("PSEUDOLIFE_MCP_DATABASE_URL", dsn)],
            4,
            DAEMON_CAUSE,
        );
    }
    fs::create_dir_all(home.path().join("dir.env")).unwrap();
    fs::write(home.path().join("plain"), "x").unwrap();
    assert_refused(
        &home,
        &["create", "--file", "dir.env", "--admin-url", CLOSED_URL],
        &[],
        4,
        &format!(
            "{FILE_PREFIX}is a link or not a regular file; choose a regular private login file"
        ),
    );
    assert_refused(
        &home,
        &[
            "create",
            "--file",
            &format!("plain{}x.env", std::path::MAIN_SEPARATOR),
            "--admin-url",
            CLOSED_URL,
        ],
        &[],
        4,
        &format!("{FILE_PREFIX}{ANCESTOR_CAUSE}"),
    );
    let as_dir = home.path().join("dir.env");
    assert_refused(
        &home,
        &create,
        &[("PSEUDOLIFE_TEST_PG_LOGIN_FILE", as_dir.to_str().unwrap())],
        4,
        &format!(
            "{FILE_PREFIX}is a link or not a regular file; choose a regular private login file"
        ),
    );
    assert_refused(
        &home,
        &create,
        &[("PSEUDOLIFE_TEST_PG_LOGIN_FILE", "a/../b")],
        4,
        ENV_FILE_CANONICAL,
    );
    assert_refused(
        &home,
        &["create", "--container", ABSENT],
        &[],
        4,
        PATH_CAUSE,
    );
}

/// A directory this process cannot add entries to, restored on drop.
struct Locked(PathBuf);

impl Locked {
    fn new(path: PathBuf) -> Option<Self> {
        fs::create_dir_all(&path).unwrap();
        #[cfg(windows)]
        {
            // Everyone: no "add file" (WD) and no "add subdirectory" (AD).
            let status = Command::new("icacls")
                .arg(&path)
                .args(["/deny", "*S-1-1-0:(WD,AD)"])
                .output()
                .unwrap()
                .status;
            assert!(status.success());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if rustix::process::geteuid().is_root() {
                return None; // root writes anywhere: nothing to defer
            }
            fs::set_permissions(&path, fs::Permissions::from_mode(0o555)).unwrap();
        }
        Some(Self(path))
    }
}

impl Drop for Locked {
    fn drop(&mut self) {
        #[cfg(windows)]
        let _ = Command::new("icacls")
            .arg(&self.0)
            .args(["/remove:d", "*S-1-1-0"])
            .output();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(&self.0, fs::Permissions::from_mode(0o755));
        }
    }
}

#[test]
fn unwritable_paths_are_refused_before_connecting() {
    let home = Home::new("unwritable");
    if let Some(locked) = Locked::new(home.path().join("locked")) {
        let inside = locked.0.join("x.env");
        let nested = locked.0.join("sub").join("x.env");
        let blocks_directory = match fs::create_dir(nested.parent().unwrap()) {
            Ok(()) => {
                fs::remove_dir(nested.parent().unwrap()).unwrap();
                false
            }
            Err(_) => true,
        };
        for file in std::iter::once(&inside).chain(blocks_directory.then_some(&nested)) {
            assert_refused(
                &home,
                &[
                    "create",
                    "--file",
                    file.to_str().unwrap(),
                    "--admin-url",
                    CLOSED_URL,
                ],
                &[],
                4,
                &format!(
                    "{FILE_PREFIX}directory is not writable; choose a writable login directory"
                ),
            );
        }
        assert_refused(
            &home,
            &["create", "--admin-url", CLOSED_URL],
            &[("PSEUDOLIFE_TEST_PG_LOGIN_FILE", inside.to_str().unwrap())],
            4,
            &format!("{FILE_PREFIX}directory is not writable; choose a writable login directory"),
        );
    }
    let long = format!("{}.env", "n".repeat(244));
    assert_refused(
        &home,
        &["create", "--file", &long, "--admin-url", CLOSED_URL],
        &[],
        4,
        &format!("{FILE_PREFIX}file name exceeds the staging limit; choose a shorter file name"),
    );
    #[cfg(windows)]
    {
        let deep = format!("{}\\{}.env", "d".repeat(240), "e".repeat(20));
        assert_refused(
            &home,
            &["create", "--file", &deep, "--admin-url", CLOSED_URL],
            &[],
            4,
            &format!("{FILE_PREFIX}path exceeds Windows limits; choose a shorter login path"),
        );
        let target = home.path().join("readonly.env");
        fs::write(&target, "PSEUDOLIFE_TEST_PG_USER=pseudolife_test\n").unwrap();
        let mut permissions = fs::metadata(&target).unwrap().permissions();
        permissions.set_readonly(true);
        fs::set_permissions(&target, permissions).unwrap();
        assert_refused(
            &home,
            &[
                "create",
                "--file",
                "readonly.env",
                "--admin-url",
                CLOSED_URL,
            ],
            &[],
            4,
            &format!("{FILE_PREFIX}is read-only; make the login file writable"),
        );
        let mut permissions = fs::metadata(&target).unwrap().permissions();
        #[allow(clippy::permissions_set_readonly_false)]
        permissions.set_readonly(false);
        fs::set_permissions(&target, permissions).unwrap();
    }
}

#[test]
fn an_unknown_home_is_refused() {
    let home = Home::new("nohome");
    let output = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")))
        .args(["test-login", "create", "--admin-url", CLOSED_URL])
        .current_dir(home.path())
        .output()
        .unwrap();
    assert_error(&output, 4, HOME_CAUSE);
    assert!(home.listing().is_empty());
}

#[test]
fn usage_refusals_come_before_any_connection() {
    let home = Home::new("usage");
    let pattern = "[a-z_][a-z0-9_]{0,62}";
    assert_answer(
        &home,
        &["create", "--role", "Bad-Name", "--admin-url", CLOSED_URL],
        &[],
        2,
        &format!("test-login: role name 'Bad-Name' must match {pattern}\n"),
    );
    assert_answer(
        &home,
        &["create", "--role", "", "--json"],
        &[],
        2,
        &format!(
            "{{\"exit\": 2, \"error\": \"role name '' must match {pattern}\", \"changes\": []}}\n"
        ),
    );
    assert_answer(
        &home,
        &["create", "--role", "it's"],
        &[],
        2,
        &format!("test-login: role name \"it's\" must match {pattern}\n"),
    );
    assert_answer(
        &home,
        &["create", "--role", "a\u{200b}"],
        &[],
        2,
        &format!("test-login: role name 'a\\u200b' must match {pattern}\n"),
    );
    assert_answer(
        &home,
        &["create", "--bank", "x", "--bank", "template0"],
        &[],
        2,
        "test-login: template0 is not a bank and stays open\n",
    );
    assert_answer(
        &home,
        &["create", "--json", "--bank", "postgres"],
        &[],
        2,
        "{\"exit\": 2, \"error\": \"postgres is not a bank and stays open\", \"changes\": []}\n",
    );
}

#[test]
fn without_docker_or_an_admin_url_it_refuses_before_any_change() {
    let home = Home::new("nodocker");
    let empty = home.path().join("nobin");
    fs::create_dir(&empty).unwrap();
    let path = empty.to_str().unwrap();
    let text = format!(
        "no superuser connection: run this on the Docker host, where the {ABSENT} container \
         runs, or give --admin-url with a superuser URL"
    );
    assert_answer(
        &home,
        &["create", "--container", ABSENT],
        &[("PATH", path)],
        4,
        &format!("test-login: {text}\n"),
    );
    assert_answer(
        &home,
        &["create", "--json", "--container", ABSENT],
        &[("PATH", path)],
        4,
        &format!("{{\"exit\": 4, \"error\": \"{text}\", \"changes\": []}}\n"),
    );
    // An empty PATH finds nothing either.
    assert_answer(
        &home,
        &["create", "--container", ABSENT],
        &[("PATH", "")],
        4,
        &format!("test-login: {text}\n"),
    );
}

#[test]
fn an_unreachable_admin_url_fails_before_any_change() {
    let home = Home::new("closed");
    let before = home.listing();
    let output = run(home.path(), &["create", "--admin-url", CLOSED_URL], &[]);
    assert_eq!(output.status.code(), Some(1));
    // The prefix is the contract; the client library's own text is free.
    assert!(
        String::from_utf8_lossy(&output.stdout).starts_with("test-login: the database refused: "),
        "{:?}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert!(output.stderr.is_empty());
    assert_eq!(home.listing(), before);
}

#[test]
fn invalid_homes_paths_and_ambient_tls_files_are_named() {
    let home = Home::new("preflight");
    for path in ["line\nfixture-secret.env", "."] {
        assert_refused(&home, &["create", "--file", path], &[], 4, FILE_CANONICAL);
    }
    for value in ["relative", "line\nfixture-secret"] {
        assert_refused(
            &home,
            &["create", "--admin-url", CLOSED_URL],
            &[("HOME", value), ("USERPROFILE", value)],
            4,
            HOME_CAUSE,
        );
    }
    let directory = if cfg!(windows) {
        home.path().join("AppData").join("postgresql")
    } else {
        home.path().join(".postgresql")
    };
    fs::create_dir_all(&directory).unwrap();
    let crl = directory.join("root.crl");
    fs::write(&crl, "fixture-secret").unwrap();
    assert_refused(
        &home,
        &["create", "--admin-url", CLOSED_URL],
        &[],
        4,
        "--admin-url cannot use ambient PostgreSQL certificate/key/CRL files; move those files out of the default PostgreSQL directory before retrying",
    );
    fs::remove_file(crl).unwrap();
    let output = run(home.path(), &["create", "--admin-url", CLOSED_URL], &[]);
    assert_eq!(output.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&output.stdout).starts_with("test-login: the database refused:")
    );
}

#[test]
fn equals_options_and_terminal_separator_reach_the_native_connection() {
    let home = Home::new("equals");
    for suffix in [vec![], vec!["--"]] {
        let mut arguments = vec![
            "create",
            "--role=ci_login",
            "--file=login.env",
            "--bank=extra",
            "--container=absent",
            "--admin-url=postgresql://postgres@127.0.0.1:1/postgres",
        ];
        arguments.extend(suffix);
        let output = run(home.path(), &arguments, &[]);
        assert_eq!(output.status.code(), Some(1));
        assert!(
            String::from_utf8_lossy(&output.stdout)
                .starts_with("test-login: the database refused:")
        );
        assert!(output.stderr.is_empty());
    }
    assert!(home.listing().is_empty());
}

#[test]
fn equals_values_cannot_turn_a_container_name_into_a_docker_option() {
    let home = Home::new("option-values");
    for option in ["--container", "--role", "--file", "--admin-url", "--bank"] {
        let argument = format!("{option}=--privileged");
        let output = run(home.path(), &["create", &argument], &[]);
        assert_eq!(output.status.code(), Some(2));
        let text = String::from_utf8_lossy(&output.stderr);
        assert!(text.contains(option));
        assert!(text.contains("must not start with '-'"));
        assert!(!text.contains("--privileged"));
    }
    assert!(home.listing().is_empty());
}

fn non_unicode_argument() -> std::ffi::OsString {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStringExt;
        std::ffi::OsString::from_vec(b"fixture-secret\xff".to_vec())
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStringExt;
        std::ffi::OsString::from_wide(&[0xd800])
    }
}

#[test]
fn non_unicode_arguments_and_values_have_distinct_errors() {
    let home = Home::new("argv-unicode");
    let output = command(home.path())
        .arg("create")
        .arg(non_unicode_argument())
        .args(["--admin-url", CLOSED_URL])
        .output()
        .unwrap();
    assert_error(&output, 2, "arguments must be UTF-8; use Unicode arguments");
    let output = command(home.path())
        .args(["create", "--role"])
        .arg(non_unicode_argument())
        .output()
        .unwrap();
    assert_error(
        &output,
        2,
        "--role value must be UTF-8; supply --role=VALUE",
    );
    let output = run(
        home.path(),
        &["create", "--role", "ci_login", "--admin-url", CLOSED_URL],
        &[],
    );
    assert_eq!(output.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&output.stdout).starts_with("test-login: the database refused:")
    );
    assert!(home.listing().is_empty());
}

#[cfg(unix)]
#[test]
fn non_utf8_settings_are_named_without_disclosing_bytes() {
    use std::{ffi::OsString, os::unix::ffi::OsStringExt};
    let home = Home::new("nonutf8");
    for (name, cause) in [
        ("HOME", HOME_CAUSE),
        (
            "PSEUDOLIFE_TEST_PG_LOGIN_FILE",
            "PSEUDOLIFE_TEST_PG_LOGIN_FILE is not Unicode; set it to a canonical Unicode file path",
        ),
        (
            "PSEUDOLIFE_MCP_DATABASE_URL",
            "PSEUDOLIFE_MCP_DATABASE_URL is not Unicode; set a supported PostgreSQL DSN",
        ),
        ("PATH", PATH_CAUSE),
    ] {
        let output = command(home.path())
            .args(["create", "--container", ABSENT])
            .env(name, OsString::from_vec(b"fixture-secret\xff".to_vec()))
            .output()
            .unwrap();
        assert_error(&output, 4, cause);
    }
}
