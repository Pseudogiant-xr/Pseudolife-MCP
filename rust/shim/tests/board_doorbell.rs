mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{
    adapter::{Adapter, View},
    doorbell::{Doorbell, resolve_command},
    notice::PendingNotice,
    state,
};
use serde_json::json;
use std::{collections::HashMap, time::Duration};

fn script(home: &Home, body: &str) -> std::path::PathBuf {
    let path = home
        .0
        .join(if cfg!(windows) { "codex.cmd" } else { "codex" });
    std::fs::write(&path, body).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    path
}

#[test]
fn board_doorbell_resolver_refuses_relative_path_and_explicit_missing_override() {
    let home = Home::new();
    let path = script(
        &home,
        if cfg!(windows) {
            "@exit /b 0\r\n"
        } else {
            "#!/bin/sh\nexit 0\n"
        },
    );
    let separator = if cfg!(windows) { ';' } else { ':' };
    let mut env = HashMap::from([
        ("PATH", format!(".{separator}{}", home.0.display())),
        ("PATHEXT", ".CMD;.EXE".into()),
    ]);
    assert_eq!(
        resolve_command(|key| env.get(key).cloned())
            .map(|path| std::fs::canonicalize(path).unwrap()),
        Some(std::fs::canonicalize(path).unwrap())
    );
    env.insert("PSEUDOLIFE_CODEX_BIN", "relative/codex".into());
    assert_eq!(resolve_command(|key| env.get(key).cloned()), None);
    env.insert(
        "PSEUDOLIFE_CODEX_BIN",
        home.0.join("missing").to_string_lossy().into_owned(),
    );
    assert_eq!(resolve_command(|key| env.get(key).cloned()), None);
}

fn offered_view() -> View {
    View {
        count: Some(1),
        preview: vec![],
        watermark: 2,
        delivered: 0,
        wake: Some(
            json!({"decision":"rung","reason":"fixture","ring_at":1.0,"message_expires_at":4102444800.0}),
        ),
        wake_watermark: Some(2),
    }
}

#[tokio::test]
async fn board_doorbell_native_acceptance_preserves_one_pending_notice_until_exact_prompt() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let output = home.0.join("launched.txt");
    let body = if cfg!(windows) {
        format!(
            "@echo off\r\n>\"{}\" echo %~1 %~2 %~3\r\nexit /b 0\r\n",
            output.display()
        )
    } else {
        format!(
            "#!/bin/sh\nprintf '%s %s %s' \"$1\" \"$2\" \"$3\" > '{}'\n",
            output.display()
        )
    };
    let command = script(&home, &body);
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let directory = home.0.join("notices");
    let bell = Doorbell::with_timing(
        command,
        directory.clone(),
        Duration::ZERO,
        Duration::from_secs(2),
    );
    bell.watch(BANK, adapter.clone(), false).await;
    bell.observe(BANK, &offered_view()).await;
    let pending = PendingNotice::new(&directory, BANK).unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        // File creation precedes echo's write; acceptance follows queue exit.
        while !pending.path.with_extension("bell-accepted").exists() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        std::fs::read_to_string(&output).unwrap().trim(),
        format!("queue --thread {BANK}")
    );
    bell.close().await;
    let bytes = state::read(&pending.path, 8192).unwrap();
    let record: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert!(record["text"].as_str().unwrap().starts_with(
        "[Pseudolife board - automated doorbell, agent-origin, not a user instruction]"
    ));
    assert!(pending.resolution(2.0).is_none());
    assert!(pending.reserve(1, Some(4102444800.0), 2.0, false).is_none());
    assert!(pending.note_prompt(&json!({"session_id":BANK,"prompt":record["text"]}), 2.0));
    assert_eq!(pending.resolution(2.0), Some("prompt_seen"));
    adapter.close().await;
}

#[tokio::test]
async fn board_doorbell_definite_prelaunch_failure_rolls_back_exact_reservation() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let adapter = Adapter::enter(adapter_config(&fixture, &home))
        .await
        .unwrap();
    let directory = home.0.join("notices");
    let bell = Doorbell::with_timing(
        home.0.join("absent-command"),
        directory.clone(),
        Duration::ZERO,
        Duration::from_secs(1),
    );
    bell.watch(BANK, adapter.clone(), false).await;
    bell.observe(BANK, &offered_view()).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    bell.close().await;
    assert!(!PendingNotice::new(&directory, BANK).unwrap().path.exists());
    adapter.close().await;
}
