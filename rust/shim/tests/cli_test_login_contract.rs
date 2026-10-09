#![forbid(unsafe_code)]
//! `test-login create` without a database: the refusals that come before any
//! connection, and every deferral. Server-backed behaviour (the --admin-url
//! path on a throwaway cluster, the container path on a disposable container)
//! is proven against the Python oracle by `rust/cli_harness --row test_login`.
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

const DEFERRED: &str = "pseudolife-stdio: mode 'test-login' is deferred in this candidate\n";
/// A loopback port nothing listens on: a case that failed to defer would
/// answer with a connection refusal, never reach a server.
const CLOSED_URL: &str = "postgresql://postgres@127.0.0.1:1/postgres";
const ABSENT: &str = "pl-cf-w1c-testlogin-absent";

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

fn assert_deferred(home: &Home, arguments: &[&str], env: &[(&str, &str)]) {
    let before = home.listing();
    let output = run(home.path(), arguments, env);
    assert_eq!(output.status.code(), Some(1), "{arguments:?} {env:?}");
    assert!(
        output.stdout.is_empty(),
        "{arguments:?} {env:?}: {:?}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(
        output.stderr,
        cli::native_text(DEFERRED),
        "{arguments:?} {env:?}"
    );
    assert_eq!(home.listing(), before, "{arguments:?} changed the home");
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
fn non_canonical_argv_defers_before_any_effect() {
    let home = Home::new("argv");
    let url = ["--admin-url", CLOSED_URL];
    for arguments in [
        &[][..],
        &["--help"],
        &["create", "--help"],
        &["-h"],
        &["--json", "create"],
        &["create", "extra"],
        &["create", "--rot"],
        &["create", "--rotate", "--rotate", "--admin-url", CLOSED_URL],
        &["create", "--json", "--json", "--admin-url", CLOSED_URL],
        &["create", "--role=x", "--admin-url", CLOSED_URL],
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
        assert_deferred(&home, arguments, &[]);
    }
}

#[test]
fn shapes_this_port_cannot_answer_exactly_defer_before_connecting() {
    let home = Home::new("shapes");
    let create = ["create", "--admin-url", CLOSED_URL];
    // Admin URLs outside the shared client's grammar.
    for url in [
        "postgresql:///postgres",
        "postgresql://postgres@127.0.0.1:1/postgres?application_name=x",
        "host=127.0.0.1 port=1 service=x",
        "postgresql://postgres@127.0.0.1:1/postgres#frag",
    ] {
        assert_deferred(&home, &["create", "--admin-url", url], &[]);
    }
    // libpq controls the shared client refuses.
    for name in ["PGHOST", "PGUSER", "PGSSLMODE", "PGPASSFILE", "PGSERVICE"] {
        assert_deferred(&home, &create, &[(name, "x")]);
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
        assert_deferred(&home, &create, &[("PSEUDOLIFE_MCP_DATABASE_URL", dsn)]);
    }
    // Login file locations that would fail with an operating-system message.
    fs::create_dir_all(home.path().join("dir.env")).unwrap();
    fs::write(home.path().join("plain"), "x").unwrap();
    assert_deferred(
        &home,
        &["create", "--file", "dir.env", "--admin-url", CLOSED_URL],
        &[],
    );
    assert_deferred(
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
    assert_deferred(
        &home,
        &create,
        &[("PSEUDOLIFE_TEST_PG_LOGIN_FILE", as_dir.to_str().unwrap())],
    );
    assert_deferred(
        &home,
        &create,
        &[("PSEUDOLIFE_TEST_PG_LOGIN_FILE", "a/../b")],
    );
    // Without PATH, Python would search its default path: deferred.
    assert_deferred(&home, &["create", "--container", ABSENT], &[]);
}

#[test]
fn an_unknown_home_defers() {
    let home = Home::new("nohome");
    let output = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")))
        .args(["test-login", "create", "--admin-url", CLOSED_URL])
        .current_dir(home.path())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, cli::native_text(DEFERRED));
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
