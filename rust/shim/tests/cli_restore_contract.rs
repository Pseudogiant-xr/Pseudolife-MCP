#![forbid(unsafe_code)]
//! `restore` through the binary: help and usage, the dump, state-archive
//! and confirmation refusals, every argv answered (no deferral), and,
//! confirmed, a missing Docker, a relative Docker program and a lite bank
//! refused under the update lock. The destructive steps are tested with a
//! scripted Docker in `src/cli/restore_cli/execute_tests.rs`.
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
};

use flate2::{Compression, write::GzEncoder};

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
    run_with(home, columns, args, &[])
}

fn run_with(home: &Path, columns: &str, args: &[&str], env: &[(&str, &Path)]) -> Output {
    let mut command: Command =
        cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("restore")
        .args(args)
        .current_dir(home)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("LOCALAPPDATA", home.join("local"))
        .env("XDG_DATA_HOME", home.join("xdg"))
        .env("PATH", home)
        .env("PSEUDOLIFE_DOCKER", home.join("no-docker"))
        .env("COLUMNS", columns)
        .stdin(Stdio::null());
    for (key, value) in env {
        command.env(key, value);
    }
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
fn help_and_usage_are_answered_at_any_width() {
    let home = Home::new("help");
    let help = HELP.replace("\r\n", "\n");
    for columns in ["80", "100", "40"] {
        assert_answer(&run(&home.0, columns, &["--help"]), 0, &help, "");
    }
    let usage = &help[..=help.find("\n\n").unwrap()];
    let required = format!(
        "{usage}pseudolife-mcp restore: error: the following arguments are required: --backup-file\n"
    );
    assert_answer(&run(&home.0, "80", &[]), 2, "", &required);
    assert_answer(
        &run(&home.0, "80", &["--backup-file", "x", "--db-port", "abc"]),
        2,
        "",
        &format!(
            "{usage}pseudolife-mcp restore: error: argument --db-port: invalid int value: 'abc'\n"
        ),
    );
    assert_answer(&run(&home.0, "120", &["-hx"]), 0, &help, "");
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
    let mut sparse = tar_header(b"s", b'S', b"");
    sparse.extend_from_slice(&[0u8; 1024]);
    fs::write(home.0.join("sparse.tgz"), gz(&sparse)).unwrap();
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
    assert_answer(
        &run(
            &home.0,
            "80",
            &[
                "--backup-file",
                "good.sql.gz",
                "--state-archive",
                "sparse.tgz",
            ],
        ),
        2,
        "",
        "restore failed: the state archive contains a sparse member, which restore cannot check safely; re-create it as a plain GNU tar archive (tar --format=gnu -czf <archive> -C <state directory> .) and retry. Nothing was restored\n",
    );
    // stdin is the null device and stdout a pipe: nobody can answer a
    // prompt. Abbreviations and `--` are read as argparse reads them.
    for args in [
        &[
            "--backup-file",
            "good.sql.gz",
            "--state-archive",
            "plain.tgz",
        ][..],
        &["--back", "good.sql.gz"],
        &["--backup-file", "good.sql.gz", "--"],
    ] {
        assert_answer(&run(&home.0, "80", args), 2, "", NEEDS_YES);
    }
    assert_eq!(listing(&home.0), before);
}

const CONFIRMED: [&str; 18] = [
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
];

const NO_DOCKER: &str = "restore failed: Docker was not found: no `docker` program is on PATH (only absolute PATH entries are searched); install Docker, or set PSEUDOLIFE_DOCKER to the docker program's full path, then retry. Nothing was restored\n";

#[test]
fn a_confirmed_restore_without_docker_refuses_under_the_lock() {
    let home = Home::new("valid");
    fs::write(
        home.0.join("good.sql.gz"),
        gz(b"-- PostgreSQL database dump complete\n"),
    )
    .unwrap();
    let before = listing(&home.0);
    assert_answer(&run(&home.0, "80", &CONFIRMED), 2, "", NO_DOCKER);
    // The update lock file stays; its pid file and no safety directory do.
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
fn a_lite_bank_is_refused_with_the_docker_install() {
    let home = Home::new("lite");
    fs::write(
        home.0.join("good.sql.gz"),
        gz(b"-- PostgreSQL database dump complete\n"),
    )
    .unwrap();
    let lite = home.0.join("lite");
    fs::create_dir_all(lite.join("embedded_pg")).unwrap();
    fs::write(lite.join("embedded_pg").join("PG_VERSION"), b"18\n").unwrap();
    let output = run_with(
        &home.0,
        "80",
        &CONFIRMED,
        &[("PSEUDOLIFE_MCP_DATA_DIR", &lite)],
    );
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).unwrap();
    // The shared lite-tier refusal, whose move can complete (back up with the
    // release that opens the bank before installing the Docker tier).
    let expected = format!(
        "restore failed: no Docker bank was found, and {} Nothing was restored",
        pseudolife_stdio::cli::retired::lite_bank_refusal(&lite)
    );
    assert_eq!(stderr.trim_end(), expected, "{stderr}");
    assert!(output.stdout.is_empty());
}

#[test]
fn a_relative_docker_program_is_refused() {
    let home = Home::new("relative");
    fs::write(
        home.0.join("good.sql.gz"),
        gz(b"-- PostgreSQL database dump complete\n"),
    )
    .unwrap();
    // A docker in the current directory is never run.
    fs::write(home.0.join("docker"), b"").unwrap();
    let output = run_with(
        &home.0,
        "80",
        &CONFIRMED,
        &[("PSEUDOLIFE_DOCKER", Path::new("./docker"))],
    );
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.starts_with("restore failed: PSEUDOLIFE_DOCKER names a relative path;"),
        "{stderr}"
    );
    // A bare name is looked up only on absolute PATH entries: with PATH
    // `.`, the `docker` file in the current directory is not found.
    let output = run_with(
        &home.0,
        "80",
        &CONFIRMED,
        &[
            ("PSEUDOLIFE_DOCKER", Path::new("docker")),
            ("PATH", Path::new(".")),
        ],
    );
    assert_answer(&output, 2, "", NO_DOCKER);
}

#[test]
fn processes_network_and_writes_stay_in_their_modules() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/cli/restore_cli");
    for entry in fs::read_dir(&root).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|extension| extension != "rs") {
            continue;
        }
        let file = path.file_name().unwrap().to_str().unwrap();
        if file == "execute_tests.rs" {
            continue;
        }
        let text = fs::read_to_string(&path).unwrap();
        // Test modules may write scratch files and run servers.
        let text = text.split("#[cfg(test)]").next().unwrap();
        // docker.rs runs the docker program and opens its stdout files;
        // mod.rs holds the update lock and asks the daemon's /health;
        // safety.rs writes the safety backup; execute.rs the note and the
        // journal; docker_backup.rs removes the old backups its rotation
        // names. No module reaches into the updater.
        let allowed: &[&str] = match file {
            "docker.rs" => &["std::process", "Command", "OpenOptions"],
            "mod.rs" => &["fs::write", "remove_file", "std::net", "TcpStream"],
            "safety.rs" => &["OpenOptions", "remove_file", "create_dir", "fs::rename"],
            "execute.rs" => &["fs::write", "remove_file", "fs::rename"],
            "docker_backup.rs" => &["remove_file"],
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
            "fs::rename",
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
