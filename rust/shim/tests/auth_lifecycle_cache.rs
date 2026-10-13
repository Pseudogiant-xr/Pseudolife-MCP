mod auth_fixture;
use auth_fixture::DisposableHome;
use pseudolife_stdio::{
    cache::{HANDSHAKE_CACHE_KEEP, HandshakeCache},
    credentials::CredentialProvider,
    lifecycle::{CHANNEL_SAFETY, Runtime, SessionIdentity, with_board_checkin},
};
use serde_json::{Value, json};
use std::{
    fs,
    time::{Duration, SystemTime},
};

#[test]
fn test_handshake_cache_lives_under_the_agent_state_dir() {
    let home = DisposableHome::new();
    let state = home.path("agent-state");
    let url = "http://127.0.0.1:18765";
    let cache = HandshakeCache::from_locations(url, Some(&state), Some(&home.0)).unwrap();
    assert!(cache.path().starts_with(state.join("handshake-cache")));
    for state in [None, Some(std::path::Path::new(""))] {
        let cache = HandshakeCache::from_locations(url, state, Some(&home.0)).unwrap();
        assert!(
            cache
                .path()
                .starts_with(home.path(".pseudolife-mcp/handshake-cache"))
        );
    }
    assert!(HandshakeCache::from_locations(url, None, None).is_none());
}
#[test]
fn test_handshake_cache_round_trips_per_daemon_url() {
    let home = DisposableHome::new();
    let a = HandshakeCache::new("http://127.0.0.1:18765", &home.0);
    let b = HandshakeCache::new("http://127.0.0.1:18766", &home.0);
    a.remember_instructions(Some("fixture instructions"));
    a.remember_tools(vec![
        json!({"name":"fixture", "inputSchema":{"type":"object"}}),
    ]);
    assert_eq!(a.load()["instructions"], "fixture instructions");
    assert_eq!(a.load()["tools"][0]["name"], "fixture");
    assert!(b.load().is_empty());
    a.remember_instructions(None);
    assert!(!a.load().contains_key("instructions"));
    assert_eq!(a.load()["tools"].as_array().unwrap().len(), 1);
}
#[test]
fn test_handshake_cache_is_best_effort() {
    let home = DisposableHome::new();
    let cache = HandshakeCache::new("http://127.0.0.1:18765", &home.0);
    assert!(cache.load().is_empty());
    fs::create_dir_all(cache.path().parent().unwrap()).unwrap();
    for invalid in [
        "not json",
        "[]",
        "{\"url\":\"http://wrong.invalid\",\"instructions\":\"wrong\"}",
        "{\"url\":\"http://127.0.0.1:18765\",\"instructions\":4,\"tools\":{}}",
    ] {
        fs::write(cache.path(), invalid).unwrap();
        assert!(cache.load().is_empty());
    }
    let blocked = home.path("blocked");
    fs::write(&blocked, b"fixture").unwrap();
    let blocked = HandshakeCache::new("http://127.0.0.1:18765", blocked);
    blocked.remember_instructions(Some("unwritable"));
    assert!(blocked.load().is_empty());
}
#[test]
fn test_handshake_cache_leaves_no_scratch_file_when_the_replace_fails() {
    let home = DisposableHome::new();
    let cache = HandshakeCache::new("http://127.0.0.1:18765", &home.0);
    fs::create_dir_all(cache.path()).unwrap();
    cache.remember_instructions(Some("fixture"));
    assert!(
        fs::read_dir(cache.path().parent().unwrap())
            .unwrap()
            .all(|entry| entry.unwrap().path().extension().is_none_or(|e| e != "tmp"))
    );
}
#[test]
fn test_handshake_cache_keeps_only_the_most_recent_urls() {
    let home = DisposableHome::new();
    for index in 0..24 {
        let cache = HandshakeCache::new(&format!("http://127.0.0.1:{}", 19000 + index), &home.0);
        cache.remember_instructions(Some("fixture"));
        // Use fixture timestamps for deterministic retention even on coarse filesystems.
        fs::OpenOptions::new()
            .write(true)
            .open(cache.path())
            .unwrap()
            .set_times(
                fs::FileTimes::new()
                    .set_modified(SystemTime::now() - Duration::from_secs(100 - index)),
            )
            .unwrap();
    }
    assert_eq!(
        fs::read_dir(home.path("handshake-cache")).unwrap().count(),
        HANDSHAKE_CACHE_KEEP
    );
    assert!(
        !HandshakeCache::new("http://127.0.0.1:19000", &home.0)
            .path()
            .exists()
    );
    assert!(
        HandshakeCache::new("http://127.0.0.1:19023", &home.0)
            .path()
            .exists()
    );
}

fn runtime(home: &DisposableHome, unreachable: bool) -> Runtime {
    Runtime {
        url: "http://127.0.0.1:18765".into(),
        provider: CredentialProvider::new(None, None).unwrap(),
        session: SessionIdentity::new(None, None),
        health: (!unreachable).then(|| json!({"status":"ok"})),
        instructions_note: String::new(),
        cache: Some(HandshakeCache::new("http://127.0.0.1:18765", &home.0)),
    }
}
#[test]
fn test_shim_starts_without_its_daemon_using_cached_instructions_and_tools() {
    let home = DisposableHome::new();
    let mut runtime = runtime(&home, true);
    let note = runtime
        .instructions(None, false, "checkin fixture", false)
        .unwrap();
    assert!(note.contains("Its tools are listed once it answers."));
    runtime
        .cache
        .as_ref()
        .unwrap()
        .remember_instructions(Some("cached instructions"));
    runtime
        .cache
        .as_ref()
        .unwrap()
        .remember_tools(vec![json!({"name":"fixture"})]);
    runtime.instructions_note = "version mismatch".into();
    let note = runtime
        .instructions(
            Some("must not fetch".into()),
            false,
            "checkin fixture",
            false,
        )
        .unwrap();
    assert_eq!(
        note,
        "Pseudolife-MCP: the memory daemon did not answer when this session started. These instructions and the tool list are the last ones it gave this machine. Each tool call retries it, and the tool list refreshes when it answers.\n\nversion mismatch\n\ncached instructions"
    );
}
#[test]
fn test_instruction_assembly_appends_checkin_prepends_version_and_channel_safety() {
    let home = DisposableHome::new();
    let mut runtime = runtime(&home, false);
    assert_eq!(
        runtime
            .instructions(
                Some("daemon instructions".into()),
                false,
                "checkin fixture",
                false
            )
            .as_deref(),
        Some("daemon instructions")
    );
    runtime.instructions_note = "version mismatch".into();
    let instructions = runtime
        .instructions(
            Some("daemon instructions".into()),
            true,
            "memory_agents checkin fixture",
            true,
        )
        .unwrap();
    assert_eq!(
        instructions,
        format!(
            "version mismatch\n\ndaemon instructions memory_agents checkin fixture{CHANNEL_SAFETY}"
        )
    );
    assert_eq!(
        with_board_checkin(
            Some("daemon memory_agents already".into()),
            "checkin fixture"
        )
        .as_deref(),
        Some("daemon memory_agents already")
    );
    assert_eq!(
        with_board_checkin(None, "checkin fixture").as_deref(),
        Some("checkin fixture")
    );
    assert_eq!(runtime.cache.unwrap().load().get("tools"), None::<&Value>);
}
