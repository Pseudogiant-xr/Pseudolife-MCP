use super::*;
use crate::cli::installer::fake_codex::FakeCodex;
use crate::setup::codex::{FILE_KEY, SERVER, URL_KEY, token_copy_path};

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

#[test]
fn options_need_the_url_and_the_file_once_each() {
    assert_eq!(
        parse(&strings(&[
            "--daemon-url",
            "https://daemon.example.com",
            "--token-file",
            "/fixture/operator.token"
        ])),
        Ok(Some(Options {
            daemon_url: "https://daemon.example.com".into(),
            token_file: PathBuf::from("/fixture/operator.token"),
        }))
    );
    assert_eq!(parse(&strings(&["--help"])), Ok(None));
    for bad in [
        &["--daemon-url", "https://daemon.example.com"][..],
        &["--token-file", "/fixture/operator.token"],
        &["--daemon-url"],
        &[
            "--daemon-url",
            "https://a.example.com",
            "--daemon-url",
            "https://b.example.com",
        ],
        &["--token-stdin"],
    ] {
        assert!(parse(&strings(bad)).is_err(), "{bad:?}");
    }
}

/// The step reads Codex's configuration through the app-server and never
/// writes it: one `config/read`, no `config/batchWrite`.
#[tokio::test]
async fn the_step_reads_codexs_configuration_and_writes_nothing() {
    let home = std::env::temp_dir().join(format!("codex-token-copy-cli-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&home).unwrap();
    let home = codex::resolved_path(&home).unwrap();
    let copy = token_copy_path(&home);
    let env = json!({
        "PSEUDOLIFE_WRITER_ID": "codex", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
        URL_KEY: "https://daemon.example.com", FILE_KEY: copy,
    });
    let mut fake = FakeCodex::new(
        &home,
        json!({"mcp_servers": {SERVER: {"command": "/opt/pseudolife-mcp", "args": [], "env": env}}}),
    );
    let options = Options {
        daemon_url: "https://daemon.example.com".into(),
        token_file: home.join("operator.token"),
    };
    let report = state(&mut fake, &home, &home, &options).await.unwrap();
    // This home has no hooks' connection file, which connect would write.
    assert_eq!(report["state"], token_copy::OTHER_CHANGES, "{report}");
    assert_eq!(
        fake.calls
            .iter()
            .map(|(m, _)| m.as_str())
            .collect::<Vec<_>>(),
        ["config/read"]
    );
    assert!(fake.writes().is_empty());
    let _ = std::fs::remove_dir_all(&home);
}
