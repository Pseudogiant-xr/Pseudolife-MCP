#![forbid(unsafe_code)]
//! `move` without a daemon: argparse help and usage, the refusals made
//! before any effect, and the deferral of every argv that passes them. The
//! differential proof against the Python oracle is the CLI harness
//! (`rust/cli_harness --row move`).
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    io::ErrorKind,
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Command, Output},
};

const DEFERRED: &str = "pseudolife-stdio: mode 'move' is deferred in this candidate\n";
const BAD_TO: &str = "move: --to takes an ssh target such as root@box or a ~/.ssh/config alias\n";
const BAD_URL: &str = "move: --target-url must be an http(s) origin such as \
                       http://100.64.0.2:8765 (no path, query or credentials)\n";
const BAD_CHECKOUT: &str = "move: --target-checkout holds a control character\n";
const USAGE: &str = "usage: pseudolife-mcp move [-h] --to SSH-TARGET [--target-checkout PATH]
                           [--target-url URL] [--no-keep-tokens] [--resume]
                           [--dry-run] [--yes] [--json]
";

struct Home(PathBuf);

impl Home {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!("move-{label}-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}

impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// A cleared environment whose PATH holds nothing (no docker or ssh) and
/// whose every proxy and daemon variable names `listener`.
fn run(home: &Path, listener: &str, columns: &str, args: &[&str]) -> Output {
    let mut command: Command =
        cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("move")
        .args(args)
        .current_dir(home)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("PATH", home)
        .env("COLUMNS", columns)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", listener);
    for key in [
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ] {
        command.env(key, listener);
    }
    command.output().unwrap()
}

fn listener() -> (TcpListener, String) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    (listener, url)
}

fn assert_quiet(listener: &TcpListener) {
    match listener.accept() {
        Err(error) if error.kind() == ErrorKind::WouldBlock => {}
        Ok((_, peer)) => panic!("the leaf opened a connection from {peer}"),
        Err(error) => panic!("accept failed: {error}"),
    }
}

fn assert_answer(output: &Output, code: i32, stdout: &str, stderr: &str) {
    assert_eq!(output.status.code(), Some(code));
    assert_eq!(output.stdout, cli::native_text(stdout));
    assert_eq!(output.stderr, cli::native_text(stderr));
}

fn listing(home: &Path) -> Vec<PathBuf> {
    fs::read_dir(home)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect()
}

#[test]
fn every_argv_that_passes_validation_defers_without_a_connection() {
    let home = Home::new("valid");
    let (socket, url) = listener();
    for args in [
        &["--to", "root@box"][..],
        &["--to", "root@box", "--dry-run"],
        &["--to", "root@box", "--resume", "--yes"],
        &["--to", "root@box", "--json"],
        &[
            "--to",
            "root@box",
            "--target-checkout",
            "/srv/pl",
            "--target-url",
            "http://100.64.0.2:8765",
            "--no-keep-tokens",
            "--resume",
            "--dry-run",
            "--yes",
            "--json",
        ],
        &["--to", "box", "--target-url", "https://box.example.com/"],
        &["--to", "box", "--target-checkout", "a\tb"],
    ] {
        let output = run(&home.0, &url, "80", args);
        assert_answer(&output, 1, "", DEFERRED);
        assert_quiet(&socket);
    }
    assert!(listing(&home.0).is_empty(), "the deferral wrote a file");
}

#[test]
fn argparse_shapes_defer() {
    let home = Home::new("shapes");
    let (socket, url) = listener();
    for args in [
        &["--to=root@box"][..],
        &["--to"],
        &["--to", "-1"],
        &["--to", "-oProxyCommand=evil"],
        &["--dry", "--to", "root@box"],
        &["--yes", "--yes", "--to", "root@box"],
        &["--to", "root@box", "extra"],
        &["--to", "box", "--target-url", "http://[::1]:8765"],
    ] {
        assert_answer(&run(&home.0, &url, "80", args), 1, "", DEFERRED);
    }
    assert_answer(&run(&home.0, &url, "100", &["--help"]), 1, "", DEFERRED);
    assert_answer(&run(&home.0, &url, "100", &[]), 1, "", DEFERRED);
    assert_quiet(&socket);
}

#[test]
fn help_and_usage_at_80_columns() {
    let home = Home::new("help");
    let (socket, url) = listener();
    for flag in ["--help", "-h"] {
        let output = run(&home.0, &url, "80", &[flag]);
        assert_eq!(output.status.code(), Some(0));
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(text.starts_with(&String::from_utf8(cli::native_text(USAGE)).unwrap()));
        assert!(text.contains("--json                one JSON report on stdout"));
        assert!(output.stderr.is_empty());
    }
    let required =
        format!("{USAGE}pseudolife-mcp move: error: the following arguments are required: --to\n");
    assert_answer(&run(&home.0, &url, "80", &[]), 2, "", &required);
    assert_answer(
        &run(&home.0, &url, "80", &["--yes", "--json"]),
        2,
        "",
        &required,
    );
    assert_quiet(&socket);
}

#[test]
fn refusals_before_any_effect() {
    let home = Home::new("refuse");
    let (socket, url) = listener();
    for (args, stderr) in [
        (
            &["--to", "root@box", "--target-url", "http://box:8765/api"][..],
            BAD_URL,
        ),
        (&["--to", "", "--target-url", "ftp://box"], BAD_URL),
        (&["--to", "root@box", "--target-url", "ftp://x/y"], BAD_URL),
        (&["--to", ""], BAD_TO),
        (&["--to", "root@box extra"], BAD_TO),
        (&["--to", "root\u{1c}box", "--json"], BAD_TO),
        (&["--to", "root\u{1}box"], BAD_TO),
        (&["--to", "a b", "--target-url", "http://box:8765/"], BAD_TO),
        (
            &["--to", "root@box", "--target-checkout", "/srv/x\ny"],
            BAD_CHECKOUT,
        ),
        (
            &["--to", "root@box", "--target-checkout", "\r//"],
            BAD_CHECKOUT,
        ),
    ] {
        assert_answer(&run(&home.0, &url, "80", args), 2, "", stderr);
    }
    // Under --json, Mover.run's refusals are a JSON report with a fresh move id.
    for args in [
        &["--json", "--to", "root\u{1}box"][..],
        &["--to", "box", "--target-checkout", "x\n", "--json"],
    ] {
        assert_answer(&run(&home.0, &url, "80", args), 1, "", DEFERRED);
    }
    assert_quiet(&socket);
    assert!(listing(&home.0).is_empty(), "a refusal wrote a file");
}

/// The leaf's own source, as compiled into the binary under test.
const LEAF: &str = include_str!("../src/cli/move_cli.rs");

/// Names through which Rust code reaches the network, a child process, the
/// file system or raw OS handles. A direct `TcpStream::connect` to the live
/// daemon's port would not cross the proxy and listener checks above, so the
/// leaf's source is held to this list instead.
const IO_NAMES: &[&str] = &[
    "std::net",
    "TcpStream",
    "TcpListener",
    "UdpSocket",
    "ToSocketAddrs",
    "reqwest",
    "hyper",
    "tokio",
    "std::process",
    "Command",
    "std::fs",
    "fs::",
    "File::",
    "OpenOptions",
    "std::os",
    "libc",
    "windows_sys",
    "winapi",
    "unsafe",
    "extern",
];

#[test]
fn the_leaf_source_reaches_no_io_beyond_its_answer() {
    let hits: Vec<&str> = IO_NAMES
        .iter()
        .copied()
        .filter(|name| LEAF.contains(name))
        .collect();
    assert!(hits.is_empty(), "move_cli.rs names I/O APIs: {hits:?}");
    // No crate module (they hold the daemon, board and database clients):
    // only the dispatcher's stream encoder and the tests' glob import.
    assert!(!LEAF.contains("crate::"), "move_cli.rs uses a crate module");
    for (at, _) in LEAF.match_indices("super::") {
        let rest = &LEAF[at..];
        assert!(
            rest.starts_with("super::text_bytes(") || rest.starts_with("super::*;"),
            "move_cli.rs reaches {}",
            &rest[..rest.len().min(40)]
        );
    }
}
