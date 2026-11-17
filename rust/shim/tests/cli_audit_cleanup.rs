#![forbid(unsafe_code)]
mod audit_fault_wire;
mod common;
#[path = "common/pg_wire.rs"]
#[allow(dead_code)]
mod pg_wire;
use std::{path::PathBuf, time::Duration};

struct Directory(PathBuf);
impl Directory {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("pseudolife-audit-cleanup-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Directory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

async fn snapshot_or_catalog_failure(mode: &str, fault: &str) {
    for inject in [true, false] {
        let directory = Directory::new();
        let out = directory.0.join("out.jsonl");
        let (dsn, task) = audit_fault_wire::start(if inject { fault } else { "none" }).await;
        let mut command = common::cli::cleared_command(std::path::Path::new(env!(
            "CARGO_BIN_EXE_pseudolife-stdio"
        )));
        command
            .args(["board-audit", mode])
            .env("PSEUDOLIFE_MCP_DATABASE_URL", dsn);
        if mode == "export" {
            command.arg("--out").arg(&out);
        }
        if mode == "redact" {
            command.args(["--message-id", "example", "--reason", "wrong paste"]);
        }
        let output = tokio::time::timeout(
            Duration::from_secs(10),
            tokio::process::Command::from(command).output(),
        )
        .await
        .unwrap()
        .unwrap();
        task.await.unwrap();
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(!stderr.contains(audit_fault_wire::SECRET));
        assert!(!String::from_utf8_lossy(&output.stdout).contains(audit_fault_wire::SECRET));
        if inject {
            assert_eq!(output.status.code(), Some(2), "{mode}/{fault}: {stderr}");
            assert!(output.stdout.is_empty());
            assert!(stderr.contains(if fault == "snapshot" {
                "snapshot"
            } else {
                "inspect"
            }));
            assert!(!stderr.contains("defer"));
            assert_eq!(std::fs::read_dir(&directory.0).unwrap().count(), 0);
        } else {
            assert_eq!(
                output.status.code(),
                Some(if mode == "redact" { 1 } else { 0 }),
                "{mode}/{fault}: {stderr}"
            );
            if mode == "redact" {
                assert!(String::from_utf8_lossy(&output.stdout).contains("message_not_found"));
            }
        }
    }
}

macro_rules! fault_test {
    ($name:ident, $mode:literal, $fault:literal) => {
        #[tokio::test]
        async fn $name() {
            snapshot_or_catalog_failure($mode, $fault).await;
        }
    };
}
fault_test!(verify_snapshot_refusal, "verify", "snapshot");
fault_test!(export_snapshot_refusal, "export", "snapshot");
fault_test!(verify_table_probe_refusal, "verify", "table");
fault_test!(export_table_probe_refusal, "export", "table");
fault_test!(redact_table_probe_refusal, "redact", "table");
fault_test!(verify_body_probe_refusal, "verify", "body");
fault_test!(export_body_probe_refusal, "export", "body");
fault_test!(redact_body_probe_refusal, "redact", "body");

#[tokio::test]
async fn failed_export_preserves_a_replacement_created_while_the_query_waits() {
    let directory = Directory::new();
    let out = directory.0.join("out.jsonl");
    let (fixture, seen, release) = pg_wire::fail_after_output_created().await;
    let mut command =
        common::cli::cleared_command(std::path::Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .args(["board-audit", "export", "--out"])
        .arg(&out)
        .env("PSEUDOLIFE_MCP_DATABASE_URL", &fixture.dsn)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let child = tokio::process::Command::from(command).spawn().unwrap();
    tokio::time::timeout(Duration::from_secs(10), seen)
        .await
        .unwrap()
        .unwrap();
    let appeared_before_success = out.exists();
    if !appeared_before_success {
        std::fs::write(&out, b"replacement must survive\n").unwrap();
    }
    release.send(()).unwrap();
    let result = tokio::time::timeout(Duration::from_secs(10), child.wait_with_output())
        .await
        .unwrap()
        .unwrap();
    assert!(
        !appeared_before_success,
        "a failed export must never publish its destination"
    );
    assert!(!result.status.success());
    assert!(result.stdout.is_empty());
    assert!(
        std::str::from_utf8(&result.stderr)
            .unwrap()
            .contains("audit")
    );
    assert_eq!(std::fs::read(&out).unwrap(), b"replacement must survive\n");
    assert_eq!(std::fs::read_dir(&directory.0).unwrap().count(), 1);
    fixture.task.await.unwrap();
}

#[tokio::test]
async fn failed_export_removes_its_own_unreplaced_destination() {
    let directory = Directory::new();
    let out = directory.0.join("out.jsonl");
    let (fixture, seen, release) = pg_wire::fail_after_output_created().await;
    let mut command =
        common::cli::cleared_command(std::path::Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
    command
        .args(["board-audit", "export", "--out"])
        .arg(&out)
        .env("PSEUDOLIFE_MCP_DATABASE_URL", &fixture.dsn)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let child = tokio::process::Command::from(command).spawn().unwrap();
    tokio::time::timeout(Duration::from_secs(10), seen)
        .await
        .unwrap()
        .unwrap();
    let appeared_before_success = out.exists();
    release.send(()).unwrap();
    let result = tokio::time::timeout(Duration::from_secs(10), child.wait_with_output())
        .await
        .unwrap()
        .unwrap();
    assert!(
        !appeared_before_success,
        "the final path was visible during a failing export"
    );
    assert!(!result.status.success());
    assert!(result.stdout.is_empty());
    assert!(
        std::str::from_utf8(&result.stderr)
            .unwrap()
            .contains("audit")
    );
    fixture.task.await.unwrap();
    assert!(
        !out.exists(),
        "a failed export must remove the file it still owns"
    );
    assert_eq!(std::fs::read_dir(&directory.0).unwrap().count(), 0);
}
