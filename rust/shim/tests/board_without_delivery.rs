#![cfg(not(feature = "codex-delivery"))]

mod board_fixture;
use board_fixture::*;
use pseudolife_stdio::board::{Board, Preparation};
use serde_json::json;
use std::{io::ErrorKind, net::TcpListener, time::Duration};

#[tokio::test]
async fn disabled_delivery_keeps_pull_and_doorbell_without_connecting_to_bridge() {
    let fixture = Fixture::new(0);
    let home = Home::new();
    let bridge = TcpListener::bind("127.0.0.1:0").unwrap();
    bridge.set_nonblocking(true).unwrap();
    let command = home
        .0
        .join(if cfg!(windows) { "codex.cmd" } else { "codex" });
    std::fs::write(
        &command,
        if cfg!(windows) {
            "@exit /b 0\r\n"
        } else {
            "#!/bin/sh\nexit 0\n"
        },
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&command, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let runtime = runtime(&fixture, true);
    let mut options = options(&runtime, &home, "1");
    options.wake = true;
    options.delivery_url = Some(format!("ws://{}", bridge.local_addr().unwrap()));
    options.delivery_token = Some("fixture-host-bridge".into());
    options.doorbell_setting = Some("1".into());
    options.command_environment.insert(
        "PSEUDOLIFE_CODEX_BIN".into(),
        command.to_string_lossy().into_owned(),
    );
    options.timing.startup = Duration::from_secs(1);
    let board = Board::attach_options(runtime, options).await;
    let Preparation::Forward(call) = board
        .prepare_call(
            "memory_message",
            Some(json!({"action":"receive"}).as_object().unwrap().clone()),
            &json!({"threadId":BANK}),
        )
        .await
        .unwrap()
    else {
        panic!("pull receive must remain available");
    };
    assert_eq!(call.operation.headers["x-pl-agent"], "fixture-agent");
    {
        let requests = fixture.requests.lock().unwrap();
        let register = requests
            .iter()
            .find(|request| request.path.ends_with("/register"))
            .unwrap();
        assert_eq!(register.body["wake_enabled"], false);
        assert_eq!(register.body["capabilities"]["pull"], true);
        assert_eq!(register.body["capabilities"]["codex"], false);
        assert_eq!(register.body["capabilities"]["ring"], true);
    }
    board.close().await;
    assert!(matches!(bridge.accept(), Err(error) if error.kind() == ErrorKind::WouldBlock));
}
