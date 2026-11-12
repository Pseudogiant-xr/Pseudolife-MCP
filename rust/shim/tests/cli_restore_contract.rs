#![forbid(unsafe_code)]
//! `restore`: argparse help and usage, the dump, state-archive and
//! confirmation refusals, the deferral of shapes the leaf does not read, and,
//! confirmed, a Docker that cannot run refused under the update lock. The
//! differential proof against the Python oracle is the CLI harness
//! (`rust/cli_harness --row restore`).
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
};

use flate2::{Compression, write::GzEncoder};

const DEFERRED: &str = "pseudolife-stdio: mode 'restore' is deferred in this candidate\n";
const HELP: &str = include_str!("../src/cli/restore_cli/restore_help.txt");
const INCOMPLETE: &str = "restore failed: the backup is unreadable or incomplete; use a complete .sql.gz dump. Nothing was restored\n";
const UNSAFE: &str = "restore failed: the state archive is unreadable or contains unsafe paths; use a complete, unmodified state backup. Nothing was restored\n";
const NEEDS_YES: &str = "restore failed: restore replaces the existing bank; rerun with --yes to confirm. Nothing was restored\n";

struct Home(PathBuf);

impl Home {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!("restore-{label}-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}

impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn gz(data: &[u8]) -> Vec<u8> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(data).unwrap();
    encoder.finish().unwrap()
}

fn tar_header(name: &[u8], kind: u8, linkname: &[u8]) -> Vec<u8> {
    let mut header = vec![0u8; 512];
    header[..name.len()].copy_from_slice(name);
    header[100..108].copy_from_slice(b"0000644\0");
    header[108..116].copy_from_slice(b"0000000\0");
    header[116..124].copy_from_slice(b"0000000\0");
    header[124..136].copy_from_slice(b"00000000000\0");
    header[136..148].copy_from_slice(b"00000000000\0");
    header[156] = kind;
    header[157..157 + linkname.len()].copy_from_slice(linkname);
    header[148..156].copy_from_slice(b"        ");
    let sum: u32 = header.iter().map(|byte| u32::from(*byte)).sum();
    header[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
    header
}

/// A cleared environment whose PATH holds only the empty home, so no
/// docker can be found, with the home as the per-user directory.
fn run(home: &Path, columns: &str, args: &[&str]) -> Output {
    let mut command: Command =
        cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("restore")
        .args(args)
        .current_dir(home)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("PATH", home)
        .env("PSEUDOLIFE_DOCKER", home.join("no-docker"))
        .env("COLUMNS", columns)
        .stdin(Stdio::null());
    command.output().unwrap()
}

fn assert_answer(output: &Output, code: i32, stdout: &str, stderr: &str) {
    assert_eq!(output.status.code(), Some(code));
    assert_eq!(output.stdout, cli::native_text(stdout));
    assert_eq!(output.stderr, cli::native_text(stderr));
}

fn listing(home: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(home)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[test]
fn help_and_usage_are_argparse_at_80_columns() {
    let home = Home::new("help");
    assert_answer(
        &run(&home.0, "80", &["--help"]),
        0,
        &HELP.replace("\r\n", "\n"),
        "",
    );
    let usage = HELP.replace("\r\n", "\n");
    let usage = &usage[..=usage.find("\n\n").unwrap()];
    assert_answer(
        &run(&home.0, "80", &[]),
        2,
        "",
        &format!(
            "{usage}pseudolife-mcp restore: error: the following arguments are required: --backup-file\n"
        ),
    );
    assert_answer(
        &run(&home.0, "80", &["--backup-file", "x", "--db-port", "abc"]),
        2,
        "",
        &format!(
            "{usage}pseudolife-mcp restore: error: argument --db-port: invalid int value: 'abc'\n"
        ),
    );
    // Another width defers rather than guess argparse's wrapping.
    assert_answer(&run(&home.0, "100", &["--help"]), 1, "", DEFERRED);
    assert_eq!(listing(&home.0), Vec::<String>::new());
}

#[test]
fn the_gates_refuse_in_order_and_write_nothing() {
    let home = Home::new("gates");
    let dump = gz(b"SELECT 1;\n-- PostgreSQL database dump complete\n");
    fs::write(home.0.join("good.sql.gz"), &dump).unwrap();
    fs::write(home.0.join("bad.sql.gz"), gz(b"SELECT 1;\n")).unwrap();
    let mut escape = tar_header(b"l", b'2', b"../..");
    escape.extend_from_slice(&[0u8; 1024]);
    fs::write(home.0.join("escape.tgz"), gz(&escape)).unwrap();
    let mut plain = tar_header(b"d", b'5', b"");
    plain.extend_from_slice(&[0u8; 1024]);
    fs::write(home.0.join("plain.tgz"), gz(&plain)).unwrap();
    let before = listing(&home.0);
    assert_answer(
        &run(&home.0, "80", &["--backup-file", "missing.sql.gz"]),
        2,
        "",
        INCOMPLETE,
    );
    assert_answer(
        &run(
            &home.0,
            "80",
            &[
                "--backup-file",
                "bad.sql.gz",
                "--state-archive",
                "escape.tgz",
            ],
        ),
        2,
        "",
        INCOMPLETE,
    );
    assert_answer(
        &run(
            &home.0,
            "80",
            &[
                "--backup-file",
                "good.sql.gz",
                "--state-archive",
                "escape.tgz",
            ],
        ),
        2,
        "",
        UNSAFE,
    );
    // stdin is the null device and stdout a pipe: nobody can answer a prompt.
    assert_answer(
        &run(
            &home.0,
            "80",
            &[
                "--backup-file",
                "good.sql.gz",
                "--state-archive",
                "plain.tgz",
            ],
        ),
        2,
        "",
        NEEDS_YES,
    );
    assert_eq!(listing(&home.0), before);
}

const NO_DOCKER: &str = "restore failed: Docker inspect failed for the backup/restore; check the containers with docker logs pg and docker logs daemon, then retry\n";

#[test]
fn a_confirmed_restore_without_docker_refuses_under_the_lock() {
    let home = Home::new("valid");
    fs::write(
        home.0.join("good.sql.gz"),
        gz(b"-- PostgreSQL database dump complete\n"),
    )
    .unwrap();
    let before = listing(&home.0);
    for args in [&[
        "--backup-file",
        "good.sql.gz",
        "--yes",
        "--leave-stopped",
        "--container",
        "pg",
        "--daemon-container",
        "daemon",
        "--db",
        "bank",
        "--user",
        "role",
        "--db-port",
        "5432",
        "--daemon-url",
        "http://127.0.0.1:9",
        "--safety-dir",
        "safety",
    ][..]]
    {
        // The first inspect cannot start: the oracle's UpdateError, exit 1.
        assert_answer(&run(&home.0, "80", args), 1, "", NO_DOCKER);
    }
    // The update lock file stays, as the oracle's does; its pid file and
    // no safety directory remain.
    let mut after = before.clone();
    after.push(".pseudolife-mcp".to_owned());
    after.sort();
    assert_eq!(listing(&home.0), after);
    assert_eq!(
        listing(&home.0.join(".pseudolife-mcp")),
        vec!["update.lock"]
    );
}

#[test]
fn shapes_the_leaf_does_not_read_defer_before_any_effect() {
    let home = Home::new("shapes");
    fs::write(
        home.0.join("good.sql.gz"),
        gz(b"-- PostgreSQL database dump complete\n"),
    )
    .unwrap();
    let before = listing(&home.0);
    for args in [
        &["--back", "good.sql.gz"][..],
        &["--backup-file", "good.sql.gz", "--"],
        &["-hx"],
    ] {
        assert_answer(&run(&home.0, "80", args), 1, "", DEFERRED);
    }
    // No update lock, safety directory or other file appeared.
    assert_eq!(listing(&home.0), before);
}

#[test]
fn the_leaf_source_runs_processes_and_writes_only_where_ported() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/cli/restore_cli");
    for entry in fs::read_dir(&root).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|extension| extension != "rs") {
            continue;
        }
        let text = fs::read_to_string(&path).unwrap();
        // Test modules may write scratch files; the leaf itself writes none.
        let text = text.split("#[cfg(test)]").next().unwrap();
        let file = path.file_name().unwrap().to_str().unwrap();
        // docker.rs runs the docker program; mod.rs holds the update lock
        // and its pid file around a refusal. Nothing else does either.
        let allowed: &[&str] = match file {
            "docker.rs" => &["std::process", "Command"],
            "mod.rs" => &["fs::write", "remove_file"],
            _ => &[],
        };
        for name in [
            "std::process",
            "Command",
            "std::net",
            "TcpStream",
            "reqwest",
            "hyper",
            "tokio",
            "UpdateLock",
            "update::",
            "fs::write",
            "create_dir",
            "OpenOptions",
            "remove_file",
        ] {
            if allowed.contains(&name) {
                continue;
            }
            let hits: Vec<_> = text
                .lines()
                .filter(|line| line.contains(name) && !line.trim_start().starts_with("//"))
                .collect();
            assert!(hits.is_empty(), "{} uses {name}: {hits:?}", path.display());
        }
    }
}
