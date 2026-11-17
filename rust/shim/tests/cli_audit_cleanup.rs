#![forbid(unsafe_code)]
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
