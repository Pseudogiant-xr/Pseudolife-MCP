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

fn assert_refused(home: &Home, arguments: &[&str], env: &[(&str, &str)]) {
    let before = home.listing();
    let output = run(home.path(), arguments, env);
    assert!(matches!(output.status.code(), Some(2 | 4)));
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(text.contains("test-login:"));
    assert!(!text.contains("deferred"));
    assert!(!text.contains("fixture-secret"));
    assert!(!text.contains("postgresql://"));
    assert_eq!(home.listing(), before);
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
    let url = ["--admin-url", CLOSED_URL];
    for arguments in [
        &["--json", "create"][..],
        &["create", "extra"],
        &["create", "--rot"],
        &["create", "--rotate", "--rotate", "--admin-url", CLOSED_URL],
        &["create", "--json", "--json", "--admin-url", CLOSED_URL],
        &[
            "create",
            "--role",
            "a",
            "--role",
            "b",
            "--admin-url",
            CLOSED_URL,
        ],
        &["create", "--role"],
        &["create", "--role", "-x", "--admin-url", CLOSED_URL],
        &["create", "--bank", "-x", "--admin-url", CLOSED_URL],
        &["create", "--bank", "", "--admin-url", CLOSED_URL],
        &["create", "--admin-url", ""],
        &[
            "create",
            "--admin-url",
            CLOSED_URL,
            "--admin-url",
            CLOSED_URL,
        ],
        &["create", "--container", "", "--admin-url", CLOSED_URL],
        &["create", "--file", "", "--admin-url", CLOSED_URL],
        &["create", "--file", "a/../b.env", "--admin-url", CLOSED_URL],
        &["create", "--file", "./b.env", "--admin-url", CLOSED_URL],
        &["create", "--", "--admin-url", CLOSED_URL],
        &[&["create", "--file", "a", "--file", "b"][..], &url[..]].concat()[..],
    ] {
        assert_refused(&home, arguments, &[]);
    }
}

#[test]
fn unsupported_settings_are_refused_before_connecting() {
    let home = Home::new("shapes");
    let create = ["create", "--admin-url", CLOSED_URL];
    // Admin URLs outside the shared client's grammar.
    for url in [
        "postgresql:///postgres",
        "postgresql://postgres@127.0.0.1:1/postgres?application_name=x",
        "host=127.0.0.1 port=1 service=x",
        "postgresql://postgres@127.0.0.1:1/postgres#frag",
    ] {
        assert_refused(&home, &["create", "--admin-url", url], &[]);
    }
    // libpq controls the shared client refuses.
    for name in ["PGHOST", "PGUSER", "PGSSLMODE", "PGPASSFILE", "PGSERVICE"] {
        assert_refused(&home, &create, &[(name, "x")]);
    }
    // Daemon DSNs that psycopg and the standard library would read differently,
    // or that the shared client cannot read at all.
    for dsn in [
        "postgresql:///pseudolife_memory",
        "postgresql://u@h/db?user=v",
        "postgresql://u@h/db?dbname=other",
        "host=h user=a xuser=b",
        " postgresql://u@h/db",
    ] {
        assert_refused(&home, &create, &[("PSEUDOLIFE_MCP_DATABASE_URL", dsn)]);
    }
    // Login file locations that would fail with an operating-system message.
    fs::create_dir_all(home.path().join("dir.env")).unwrap();
    fs::write(home.path().join("plain"), "x").unwrap();
    assert_refused(
        &home,
        &["create", "--file", "dir.env", "--admin-url", CLOSED_URL],
        &[],
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
    );
    let as_dir = home.path().join("dir.env");
    assert_refused(
        &home,
        &create,
        &[("PSEUDOLIFE_TEST_PG_LOGIN_FILE", as_dir.to_str().unwrap())],
    );
    assert_refused(
        &home,
        &create,
        &[("PSEUDOLIFE_TEST_PG_LOGIN_FILE", "a/../b")],
    );
    assert_refused(&home, &["create", "--container", ABSENT], &[]);
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
    let sep = std::path::MAIN_SEPARATOR;
    if let Some(locked) = Locked::new(home.path().join("locked")) {
        let inside = locked.0.join("x.env");
        let nested = locked.0.join("sub").join("x.env");
        // Some Windows accounts can create directories despite the deny ACL
        // (the Python oracle can too). Only assert refusal for a blocked shape.
        let blocks_directory = match fs::create_dir(nested.parent().unwrap()) {
            Ok(()) => {
                fs::remove_dir(nested.parent().unwrap()).unwrap();
                false
            }
            Err(_) => true,
        };
        let files = std::iter::once(&inside).chain(blocks_directory.then_some(&nested));
        for file in files {
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
            );
        }
        assert_refused(
            &home,
            &["create", "--admin-url", CLOSED_URL],
            &[("PSEUDOLIFE_TEST_PG_LOGIN_FILE", inside.to_str().unwrap())],
        );
        drop(locked);
    }
    // The staged name `.<name>.<pid>.new` would exceed a file name's limit.
    let long = format!("{}.env", "n".repeat(244));
    assert_refused(
        &home,
        &["create", "--file", &long, "--admin-url", CLOSED_URL],
        &[],
    );
    #[cfg(windows)]
    {
        // Past MAX_PATH, which a Python without long-path support cannot open.
        let deep = format!("{}{sep}{}.env", "d".repeat(240), "e".repeat(20));
        assert_refused(
            &home,
            &["create", "--file", &deep, "--admin-url", CLOSED_URL],
            &[],
        );
        // A read-only target: Windows refuses to replace it.
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
        );
        let mut permissions = fs::metadata(&target).unwrap().permissions();
        #[allow(clippy::permissions_set_readonly_false)]
        permissions.set_readonly(false);
        fs::set_permissions(&target, permissions).unwrap();
    }
    let _ = sep;
}

#[test]
fn an_unknown_home_is_refused() {
    let home = Home::new("nohome");
    let output = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")))
        .args(["test-login", "create", "--admin-url", CLOSED_URL])
        .current_dir(home.path())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(4));
    assert!(String::from_utf8_lossy(&output.stdout).contains("HOME/USERPROFILE"));
    assert!(output.stderr.is_empty());
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
fn native_parser_and_environment_errors_name_the_fix_without_values() {
    let home = Home::new("messages");
    for (arguments, setting) in [
        (vec!["foo"], "create"),
        (vec!["create", "--rol", "fixture-secret"], "--role"),
        (vec!["create", "--rotate", "--rotate"], "--rotate"),
        (vec!["create", "--json", "--json"], "--json"),
        (
            vec![
                "create",
                "--admin-url",
                "postgresql://u:fixture-secret@h/db?service=x",
            ],
            "--admin-url",
        ),
        (vec!["create", "--file", "a/../fixture-secret"], "--file"),
    ] {
        let output = run(home.path(), &arguments, &[]);
        assert!(matches!(output.status.code(), Some(2 | 4)));
        let text = format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(text.contains(setting));
        assert!(!text.contains("fixture-secret"));
        assert!(!text.contains("postgresql://"));
        assert!(!text.contains("deferred"));
    }
    for (name, value) in [
        ("PGHOST", "fixture-secret"),
        (
            "PSEUDOLIFE_MCP_DATABASE_URL",
            "postgresql:///fixture-secret",
        ),
    ] {
        let output = run(
            home.path(),
            &["create", "--admin-url", CLOSED_URL],
            &[(name, value)],
        );
        assert_eq!(output.status.code(), Some(4));
        let text = String::from_utf8_lossy(&output.stdout);
        assert!(text.contains(name));
        assert!(!text.contains("fixture-secret"));
    }
    assert!(home.listing().is_empty());
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

#[cfg(unix)]
#[test]
fn non_utf8_settings_are_named_without_disclosing_bytes() {
    use std::{ffi::OsString, os::unix::ffi::OsStringExt};
    let home = Home::new("nonutf8");
    for name in [
        "HOME",
        "PSEUDOLIFE_TEST_PG_LOGIN_FILE",
        "PSEUDOLIFE_MCP_DATABASE_URL",
        "PATH",
    ] {
        let mut command = command(home.path());
        command.args(["create", "--container", ABSENT]);
        command.env(name, OsString::from_vec(b"fixture-secret\xff".to_vec()));
        let output = command.output().unwrap();
        assert_eq!(output.status.code(), Some(4));
        let text = String::from_utf8_lossy(&output.stdout);
        assert!(text.contains(name));
        assert!(!text.contains("fixture-secret"));
    }
}
