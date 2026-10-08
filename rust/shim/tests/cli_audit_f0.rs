#![forbid(unsafe_code)]
#[path = "common/cli.rs"]
mod cli;
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

fn command(home: &Path) -> Command {
    let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .arg("board-audit")
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("PSEUDOLIFE_MCP_PYTHON", "missing-interpreter")
        .env("PSEUDOLIFE_MCP_DATABASE_URL", "invalid-disposable-sentinel");
    command
}

fn archive(home: &Path, content: &[u8]) -> Output {
    let path = home.join("archive.jsonl");
    fs::write(&path, content).unwrap();
    let output = command(home)
        .args(["verify", "--input"])
        .arg(&path)
        .output()
        .unwrap();
    assert_eq!(fs::read(&path).unwrap(), content);
    assert_eq!(fs::read_dir(home).unwrap().count(), 1);
    output
}

#[test]
fn empty_archive_verifies_without_python_or_bank_attachment() {
    let home = std::env::temp_dir().join(format!("audit-f0-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let output = archive(&home, b"");
    fs::remove_dir_all(&home).unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        output.stdout,
        cli::native_text(concat!(
            "{\"ok\": true, \"events\": 0, \"first_seq\": null, \"head_seq\": null, ",
            "\"head_hash\": null, \"head_created_at\": null, \"start_cut\": null}\n"
        ))
    );
    assert!(output.stderr.is_empty());
}

#[test]
fn nonempty_and_blank_archives_cannot_claim_an_intact_chain() {
    let home = std::env::temp_dir().join(format!("audit-f0-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    for content in [b"{}\n".as_slice(), b"\n", b"\xff"] {
        let output = archive(&home, content);
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert!(
            String::from_utf8(output.stderr)
                .unwrap()
                .contains("deferred")
        );
    }
    fs::remove_dir_all(&home).unwrap();
}

#[test]
fn missing_input_and_incomplete_argv_defer_without_creating_a_bank() {
    let home = std::env::temp_dir().join(format!("audit-f0-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let missing = command(&home)
        .args(["verify", "--input"])
        .arg(home.join("missing"))
        .output()
        .unwrap();
    assert_eq!(missing.status.code(), Some(1));
    assert!(missing.stdout.is_empty());
    assert!(
        String::from_utf8(missing.stderr)
            .unwrap()
            .contains("deferred")
    );
    let incomplete = command(&home).args(["verify", "--input"]).output().unwrap();
    assert_eq!(incomplete.status.code(), Some(1));
    assert!(incomplete.stdout.is_empty());
    assert!(
        String::from_utf8(incomplete.stderr)
            .unwrap()
            .contains("deferred")
    );
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(&home).unwrap();
}

#[test]
fn joined_input_option_defers_even_for_an_empty_archive() {
    let home = std::env::temp_dir().join(format!("audit-f0-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let path = home.join("archive.jsonl");
    fs::write(&path, b"").unwrap();
    let mut option = std::ffi::OsString::from("--input=");
    option.push(&path);
    let output = command(&home).arg("verify").arg(option).output().unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"");
    assert_eq!(fs::read_dir(&home).unwrap().count(), 1);
    fs::remove_dir_all(&home).unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert!(
        String::from_utf8(output.stderr)
            .unwrap()
            .contains("deferred")
    );
}

#[test]
fn option_looking_input_tokens_defer_even_when_literal_empty_files_exist() {
    let home = std::env::temp_dir().join(format!("audit-f0-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let names = [
        "--bogus",
        "--input=archive.jsonl",
        "--input=archive name",
        "-h name",
        "--",
        "-1e3",
        "-1.",
    ];
    let outputs: Vec<_> = names
        .iter()
        .map(|name| {
            fs::write(home.join(name), b"").unwrap();
            command(&home)
                .current_dir(&home)
                .args(["verify", "--input", name])
                .output()
                .unwrap()
        })
        .collect();
    for name in names {
        assert_eq!(fs::read(home.join(name)).unwrap(), b"");
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), names.len());
    fs::remove_dir_all(&home).unwrap();
    for output in outputs {
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert!(
            String::from_utf8(output.stderr)
                .unwrap()
                .contains("deferred")
        );
    }
}

#[test]
fn negative_and_space_prefixed_input_tokens_defer() {
    let home = std::env::temp_dir().join(format!("audit-f0-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let names = ["-1", "-1.5", "-.5", "-١.٥", "--bogus name"];
    #[cfg(unix)]
    let names = [names.as_slice(), &["-1\n"]].concat();
    let outputs: Vec<_> = names
        .iter()
        .map(|name| {
            fs::write(home.join(name), b"").unwrap();
            command(&home)
                .current_dir(&home)
                .args(["verify", "--input", name])
                .output()
                .unwrap()
        })
        .collect();
    for name in &names {
        assert_eq!(fs::read(home.join(name)).unwrap(), b"");
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), names.len());
    fs::remove_dir_all(&home).unwrap();
    for output in outputs {
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert!(
            String::from_utf8(output.stderr)
                .unwrap()
                .contains("deferred")
        );
    }
}

#[test]
fn single_dash_input_verifies_literal_empty_file() {
    let home = std::env::temp_dir().join(format!("audit-f0-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    fs::write(home.join("-"), b"").unwrap();
    let output = command(&home)
        .current_dir(&home)
        .args(["verify", "--input", "-"])
        .output()
        .unwrap();
    assert_eq!(fs::read(home.join("-")).unwrap(), b"");
    assert_eq!(fs::read_dir(&home).unwrap().count(), 1);
    fs::remove_dir_all(&home).unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        output.stdout,
        cli::native_text(concat!(
            "{\"ok\": true, \"events\": 0, \"first_seq\": null, \"head_seq\": null, ",
            "\"head_hash\": null, \"head_created_at\": null, \"start_cut\": null}\n"
        ))
    );
    assert!(output.stderr.is_empty());
}

#[test]
fn remaining_audit_operations_stay_explicitly_deferred() {
    let home = std::env::temp_dir().join(format!("audit-f0-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    for args in [
        vec![],
        vec!["export"],
        vec!["stats"],
        vec!["redact"],
        vec!["verify"],
        vec!["verify", "--expect-head", "1:hash"],
        vec!["--help"],
    ] {
        let output = command(&home).args(args).output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert!(
            String::from_utf8(output.stderr)
                .unwrap()
                .contains("deferred")
        );
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    fs::remove_dir_all(&home).unwrap();
}
