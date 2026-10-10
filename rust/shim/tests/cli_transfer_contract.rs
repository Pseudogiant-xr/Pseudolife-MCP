#![forbid(unsafe_code)]
//! export/import behaviour that needs no PostgreSQL: help, bank resolution
//! refusals and deferrals, argv deferrals, and the archive refusals that the
//! oracle raises before it connects. Database behaviour is proven by the
//! differential harness row `transfer` (rust/cli_harness/rows/transfer.py).
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Output},
};

const NO_DATABASE: &str = "no database configured — set PSEUDOLIFE_MCP_DATABASE_URL (Docker tier: postgresql://pseudolife:<POSTGRES_PASSWORD from ops/.env>@127.0.0.1:5433/pseudolife_memory) or use a lite data dir that holds a bank.\n";
/// Parses, and nothing listens there: no test reaches a connection attempt
/// except the one that checks the deferral after a refused connection.
const UNREACHABLE: &str = "postgresql://nobody@127.0.0.1:9/pl_cf_absent";

struct Home(PathBuf);
impl Home {
    fn new(tag: &str) -> Self {
        let path = std::env::temp_dir().join(format!("transfer-{tag}-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(path.join("cwd")).unwrap();
        Self(path)
    }
    fn cwd(&self) -> PathBuf {
        self.0.join("cwd")
    }
    fn command(&self, mode: &str) -> Command {
        let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
        command
            .arg(mode)
            .current_dir(self.cwd())
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("LOCALAPPDATA", self.0.join("AppData").join("Local"))
            .env("XDG_DATA_HOME", self.0.join("share"))
            .env("COLUMNS", "80");
        command
    }
    fn entries(&self) -> Vec<PathBuf> {
        let mut found = Vec::new();
        let mut stack = vec![self.0.clone()];
        while let Some(dir) = stack.pop() {
            for entry in fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    stack.push(path.clone());
                }
                found.push(path);
            }
        }
        found.sort();
        found
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn deferred(mode: &str) -> Vec<u8> {
    cli::native_text(&format!(
        "pseudolife-stdio: mode '{mode}' is deferred in this candidate\n"
    ))
}

fn assert_stderr(output: &Output, code: i32, stderr: &[u8]) {
    assert_eq!(output.status.code(), Some(code), "{output:?}");
    assert!(output.stdout.is_empty(), "{output:?}");
    assert_eq!(
        output.stderr,
        stderr,
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn archive(path: &Path, members: &[(&str, &str)]) {
    let mut zip = zip::ZipWriter::new(fs::File::create(path).unwrap());
    for (name, body) in members {
        zip.start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(body.as_bytes()).unwrap();
    }
    zip.finish().unwrap();
}

#[test]
fn captured_help_for_both_modes() {
    let home = Home::new("help");
    for (mode, help) in [
        (
            "export",
            include_str!("../src/cli/transfer/export_help.txt"),
        ),
        (
            "import",
            include_str!("../src/cli/transfer/import_help.txt"),
        ),
    ] {
        let output = home.command(mode).arg("--help").output().unwrap();
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(output.stdout, cli::native_text(help));
        assert!(output.stderr.is_empty());
        let wide = home
            .command(mode)
            .arg("--help")
            .env("COLUMNS", "120")
            .output()
            .unwrap();
        assert_stderr(&wide, 1, &deferred(mode));
    }
}

#[test]
fn without_a_dsn_or_lite_bank_both_modes_refuse() {
    let home = Home::new("nodb");
    let before = home.entries();
    for (mode, argv) in [
        ("export", vec![]),
        ("import", vec!["x.zip"]),
        ("export", vec!["--data-dir", "elsewhere"]),
    ] {
        let mut command = home.command(mode);
        command.args(&argv);
        let output = command.output().unwrap();
        assert_stderr(&output, 1, &cli::native_text(NO_DATABASE));
        // An empty DSN is no DSN, exactly as `if not dsn` reads it.
        let output = home
            .command(mode)
            .args(&argv)
            .env("PSEUDOLIFE_MCP_DATABASE_URL", "")
            .output()
            .unwrap();
        assert_stderr(&output, 1, &cli::native_text(NO_DATABASE));
    }
    assert_eq!(home.entries(), before);
}

#[test]
fn an_embedded_bank_marker_defers_by_name_without_touching_it() {
    let home = Home::new("lite");
    let explicit = home.0.join("bank");
    fs::create_dir_all(explicit.join("embedded_pg")).unwrap();
    fs::write(explicit.join("embedded_pg").join("PG_VERSION"), "18\n").unwrap();
    let lite_default = if cfg!(windows) {
        home.0.join("AppData").join("Local").join("pseudolife-mcp")
    } else if cfg!(target_os = "macos") {
        home.0
            .join("Library")
            .join("Application Support")
            .join("pseudolife-mcp")
    } else {
        home.0.join("share").join("pseudolife-mcp")
    };
    fs::create_dir_all(lite_default.join("embedded_pg")).unwrap();
    fs::write(lite_default.join("embedded_pg").join("PG_VERSION"), "18\n").unwrap();
    let before = home.entries();
    for mode in ["export", "import"] {
        let line = cli::native_text(&format!(
            "pseudolife-stdio: mode '{mode}' on the embedded lite tier is deferred in this candidate (needs native embedded_pg)\n"
        ));
        let mut tail: Vec<&str> = if mode == "import" {
            vec!["x.zip"]
        } else {
            vec![]
        };
        let output = home.command(mode).args(&tail).output().unwrap();
        assert_stderr(&output, 1, &line);
        tail.extend(["--data-dir", explicit.to_str().unwrap()]);
        let output = home.command(mode).args(&tail).output().unwrap();
        assert_stderr(&output, 1, &line);
        let output = home
            .command(mode)
            .args(if mode == "import" {
                vec!["x.zip"]
            } else {
                vec![]
            })
            .env("PSEUDOLIFE_MCP_DATA_DIR", &explicit)
            .output()
            .unwrap();
        assert_stderr(&output, 1, &line);
    }
    assert_eq!(home.entries(), before);
    // An existing lite default without a bank resolves to it and refuses.
    fs::remove_dir_all(lite_default.join("embedded_pg")).unwrap();
    let output = home.command("export").output().unwrap();
    assert_stderr(&output, 1, &cli::native_text(NO_DATABASE));
}

#[test]
fn non_canonical_argv_defers_before_any_effect() {
    let home = Home::new("argv");
    let before = home.entries();
    let cases: &[(&str, &[&str])] = &[
        ("export", &["--ou", "x.zip"]),
        ("export", &["--out=x.zip"]),
        ("export", &["--out", "a.zip", "--out", "b.zip"]),
        ("export", &["--out"]),
        ("export", &["--out", ""]),
        ("export", &["--out", "-x.zip"]),
        ("export", &["--force"]),
        ("export", &["-h"]),
        ("export", &["--help", "--out", "x.zip"]),
        ("export", &["extra"]),
        ("import", &[]),
        ("import", &["a.zip", "b.zip"]),
        ("import", &["--force", "--force", "a.zip"]),
        ("import", &["-a.zip"]),
        ("import", &["a.zip", "--out", "x"]),
        ("import", &["a.zip", "--data-dir"]),
        ("import", &["a.zip", "--data-dir", "d", "--data-dir", "e"]),
    ];
    for (mode, argv) in cases {
        let output = home
            .command(mode)
            .args(*argv)
            .env("PSEUDOLIFE_MCP_DATABASE_URL", UNREACHABLE)
            .output()
            .unwrap();
        assert_stderr(&output, 1, &deferred(mode));
    }
    // Spellings whose printed form Python's Path would rewrite.
    let spelled: &[&str] = if cfg!(windows) {
        &[
            "sub/x.zip",
            "sub\\\\x.zip",
            ".\\x.zip",
            "x.zip.",
            "C:x.zip",
            "nul.zip",
        ]
    } else {
        &["sub//x.zip", "./x.zip", "sub/", "sub/./x.zip"]
    };
    for value in spelled {
        for (mode, argv) in [("export", vec!["--out", value]), ("import", vec![value])] {
            let output = home
                .command(mode)
                .args(&argv)
                .env("PSEUDOLIFE_MCP_DATABASE_URL", UNREACHABLE)
                .output()
                .unwrap();
            assert_stderr(&output, 1, &deferred(mode));
        }
    }
    assert_eq!(home.entries(), before);
}

#[test]
fn archive_refusals_precede_the_connection() {
    let home = Home::new("archive");
    archive(&home.cwd().join("empty.zip"), &[("meta.jsonl", "")]);
    archive(
        &home.cwd().join("format9.zip"),
        &[("manifest.json", "{\"format_version\": 9}")],
    );
    archive(
        &home.cwd().join("formatstr.zip"),
        &[("manifest.json", "{\"format_version\": \"1\", \"x\": [1]}")],
    );
    archive(
        &home.cwd().join("formatnone.zip"),
        &[("manifest.json", "{}")],
    );
    archive(
        &home.cwd().join("formatfalse.zip"),
        &[("manifest.json", "{\"format_version\": false}")],
    );
    let before = home.entries();
    let refusal = |message: &str| cli::native_text(&format!("import refused: {message}\n"));
    for (name, message) in [
        (
            "empty.zip",
            "empty.zip carries no manifest.json — not a pseudolife-mcp export.".to_owned(),
        ),
        ("format9.zip", "export format 9 is not the format 1 this build reads — upgrade pseudolife-mcp and retry.".to_owned()),
        ("formatstr.zip", "export format '1' is not the format 1 this build reads — upgrade pseudolife-mcp and retry.".to_owned()),
        ("formatnone.zip", "export format None is not the format 1 this build reads — upgrade pseudolife-mcp and retry.".to_owned()),
        ("formatfalse.zip", "export format False is not the format 1 this build reads — upgrade pseudolife-mcp and retry.".to_owned()),
    ] {
        let output = home
            .command("import")
            .arg(name)
            .env("PSEUDOLIFE_MCP_DATABASE_URL", UNREACHABLE)
            .output()
            .unwrap();
        assert_stderr(&output, 1, &refusal(&message));
    }
    // Not a zip, a missing file and an unparsed manifest are oracle
    // tracebacks: the candidate defers.
    fs::write(home.cwd().join("plain.zip"), "not a zip").unwrap();
    archive(
        &home.cwd().join("badjson.zip"),
        &[("manifest.json", "{\"format_version\": 1,}")],
    );
    archive(&home.cwd().join("list.zip"), &[("manifest.json", "[1]")]);
    for name in ["plain.zip", "missing.zip", "badjson.zip", "list.zip"] {
        let output = home
            .command("import")
            .arg(name)
            .env("PSEUDOLIFE_MCP_DATABASE_URL", UNREACHABLE)
            .output()
            .unwrap();
        assert_stderr(&output, 1, &deferred("import"));
    }
    let after: Vec<_> = home
        .entries()
        .into_iter()
        .filter(|path| {
            !["plain.zip", "badjson.zip", "list.zip"]
                .iter()
                .any(|name| path.ends_with(name))
        })
        .collect();
    assert_eq!(after, before);
}

#[test]
fn an_unreachable_bank_defers_without_creating_the_destination() {
    let home = Home::new("unreachable");
    let before = home.entries();
    let target = home.cwd().join("made").join("x.zip");
    let output = home
        .command("export")
        .arg("--out")
        .arg(&target)
        .env("PSEUDOLIFE_MCP_DATABASE_URL", UNREACHABLE)
        .output()
        .unwrap();
    assert_stderr(&output, 1, &deferred("export"));
    assert_eq!(home.entries(), before);
}
