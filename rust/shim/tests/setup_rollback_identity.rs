//! Rollback ownership survives only while our published file identity survives.
use pseudolife_stdio::{
    board::state,
    setup::codex::{self, Rpc, SetupError},
};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "pseudolife-rollback-identity-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(path.join("pseudolife")).unwrap();
        fs::write(path.join("config.toml"), "# disposable configuration\n").unwrap();
        let user = codex::user_config(&Fake::new(&path).config(), &path).unwrap();
        Self(user.path.parent().unwrap().to_owned())
    }
    fn token(&self) -> PathBuf {
        self.0.join("pseudolife").join("token")
    }
    fn connection(&self) -> PathBuf {
        self.0.join("pseudolife").join("connection.json")
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct Fake {
    home: PathBuf,
    values: Value,
    version: u64,
    connection_contender: bool,
}
impl Fake {
    fn new(home: &Path) -> Self {
        Self {
            home: home.into(),
            values: json!({"mcp_servers":{"pseudolife-memory":{
                "command":"fixture-launcher",
                "env":{"PSEUDOLIFE_MCP_DAEMON_URL":"http://localhost:8765",
                    "PSEUDOLIFE_MCP_TOKEN":"synthetic-new-token", "UNRELATED":"preserve"}
            }}}),
            version: 0,
            connection_contender: false,
        }
    }
    fn config(&self) -> Value {
        json!({"config":self.values,"layers":[{
            "name":{"type":"user","file":self.home.join("config.toml"),"profile":null},
            "version":format!("version-{}",self.version),"config":self.values
        }]})
    }
}
impl Rpc for Fake {
    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
        match method {
            "config/read" => Ok(self.config()),
            "config/batchWrite" => {
                assert_eq!(
                    params["expectedVersion"],
                    format!("version-{}", self.version)
                );
                for edit in params["edits"].as_array().unwrap() {
                    assert_eq!(
                        edit["keyPath"],
                        codex::dotted(&["mcp_servers", codex::SERVER, "env"])
                    );
                    self.values["mcp_servers"][codex::SERVER]["env"] = edit["value"].clone();
                }
                self.version += 1;
                if self.connection_contender {
                    self.connection_contender = false;
                    state::atomic_create(
                        &self.home.join("pseudolife/connection.json"),
                        b"competing first creator",
                    )
                    .unwrap();
                }
                Ok(json!({"version":format!("version-{}",self.version)}))
            }
            _ => panic!("unexpected fixture RPC {method}"),
        }
    }
}

async fn apply(home: &Home, client: &mut Fake) -> codex::AppliedConnection {
    let plan =
        codex::plan_replace(&client.config(), &home.0, "https://example.com", None, true).unwrap();
    codex::apply_replace_detailed(client, &home.0, plan)
        .await
        .unwrap()
}
fn replace_with_same_bytes(path: &Path) -> Vec<u8> {
    let original = state::open(path).unwrap().identity;
    let bytes = state::read(path, 16384).unwrap();
    state::atomic_write(path, &bytes, Some(&original)).unwrap();
    assert_ne!(state::open(path).unwrap().identity, original);
    bytes
}
fn left(events: &[Value], path: &Path) -> bool {
    events
        .iter()
        .any(|event| event["file"] == json!(path) && event["state"] == "left")
}

#[tokio::test]
async fn same_bytes_connection_replacement_is_left_with_its_token_and_pending_recovery() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let mut applied = apply(&home, &mut client).await;
    let bytes = replace_with_same_bytes(&home.connection());
    let competitor = state::open(&home.connection()).unwrap().identity;
    let token = state::read(&home.token(), 4096).unwrap();
    let failure = applied
        .rollback_detailed(&mut client, &home.0)
        .await
        .unwrap_err();
    assert!(failure.recovery_pending);
    assert!(left(&failure.rollback, &home.connection()));
    assert!(failure.reason.to_string().contains("another writer"));
    assert_eq!(
        state::open(&home.connection()).unwrap().identity,
        competitor
    );
    assert_eq!(state::read(&home.connection(), 16384).unwrap(), bytes);
    assert_eq!(state::read(&home.token(), 4096).unwrap(), token);
    // Repeating a retained rollback never adopts the competing inode.
    assert!(
        applied
            .rollback_detailed(&mut client, &home.0)
            .await
            .unwrap_err()
            .recovery_pending
    );
    assert_eq!(
        state::open(&home.connection()).unwrap().identity,
        competitor
    );
}

#[tokio::test]
async fn same_bytes_token_replacement_is_neither_removed_nor_restored() {
    for preexisting in [false, true] {
        let home = Home::new();
        if preexisting {
            state::atomic_create(&home.token(), b"synthetic-previous-token").unwrap();
        }
        let mut client = Fake::new(&home.0);
        let mut applied = apply(&home, &mut client).await;
        let bytes = replace_with_same_bytes(&home.token());
        let competitor = state::open(&home.token()).unwrap().identity;
        let failure = applied
            .rollback_detailed(&mut client, &home.0)
            .await
            .unwrap_err();
        assert!(failure.recovery_pending);
        assert!(left(&failure.rollback, &home.token()));
        assert_eq!(state::read(&home.token(), 4096).unwrap(), bytes);
        assert_eq!(state::open(&home.token()).unwrap().identity, competitor);
        assert!(!home.connection().exists());
    }
}

#[tokio::test]
async fn preexisting_connection_same_bytes_replacement_is_not_restored_to_older_bytes() {
    let home = Home::new();
    let old = br#"{"version":1,"daemon_url":"aHR0cDovL2xvY2FsaG9zdDo4NzY1","token_file":""}"#;
    state::atomic_create(&home.connection(), old).unwrap();
    let mut client = Fake::new(&home.0);
    let mut applied = apply(&home, &mut client).await;
    let bytes = replace_with_same_bytes(&home.connection());
    assert_ne!(bytes, old);
    let competitor = state::open(&home.connection()).unwrap().identity;
    let failure = applied
        .rollback_detailed(&mut client, &home.0)
        .await
        .unwrap_err();
    assert!(failure.recovery_pending);
    assert!(left(&failure.rollback, &home.connection()));
    assert_eq!(state::read(&home.connection(), 16384).unwrap(), bytes);
    assert_eq!(
        state::open(&home.connection()).unwrap().identity,
        competitor
    );
    assert!(home.token().exists());
}

#[tokio::test]
async fn legitimate_rollback_restores_private_files_and_can_be_repeated_then_rerun() {
    let home = Home::new();
    let old = br#"{"version":1,"daemon_url":"aHR0cDovL2xvY2FsaG9zdDo4NzY1","token_file":""}"#;
    state::atomic_create(&home.connection(), old).unwrap();
    state::atomic_create(&home.token(), b"synthetic-previous-token").unwrap();
    let mut client = Fake::new(&home.0);
    for _ in 0..2 {
        let original_values = client.values.clone();
        let mut applied = apply(&home, &mut client).await;
        let events = applied
            .rollback_detailed(&mut client, &home.0)
            .await
            .unwrap();
        assert!(
            events
                .iter()
                .any(|e| e["file"] == json!(home.connection()) && e["state"] == "restored")
        );
        assert!(
            events
                .iter()
                .any(|e| e["file"] == json!(home.token()) && e["state"] == "restored")
        );
        assert_eq!(client.values, original_values);
        assert_eq!(state::read(&home.connection(), 16384).unwrap(), old);
        assert_eq!(
            state::read(&home.token(), 4096).unwrap(),
            b"synthetic-previous-token"
        );
        assert!(
            applied
                .rollback_detailed(&mut client, &home.0)
                .await
                .unwrap()
                .is_empty()
        );
    }
}

#[tokio::test]
async fn legitimate_rollback_removes_only_its_first_creations_and_is_idempotent() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let mut applied = apply(&home, &mut client).await;
    let events = applied
        .rollback_detailed(&mut client, &home.0)
        .await
        .unwrap();
    for path in [home.connection(), home.token()] {
        assert!(!path.exists());
        assert!(
            events
                .iter()
                .any(|e| e["file"] == json!(path) && e["state"] == "removed")
        );
    }
    assert!(
        applied
            .rollback_detailed(&mut client, &home.0)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn same_inode_changed_bytes_still_refuse_rollback() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    let mut applied = apply(&home, &mut client).await;
    let identity = state::open(&home.token()).unwrap().identity;
    {
        use std::io::Write;
        let mut opened = state::open_with_write(&home.token(), true).unwrap();
        opened.file.set_len(0).unwrap();
        opened.file.write_all(b"synthetic-in-place-edit").unwrap();
        opened.file.sync_all().unwrap();
    }
    assert_eq!(state::open(&home.token()).unwrap().identity, identity);
    let failure = applied
        .rollback_detailed(&mut client, &home.0)
        .await
        .unwrap_err();
    assert!(failure.recovery_pending);
    assert!(left(&failure.rollback, &home.token()));
    assert_eq!(
        state::read(&home.token(), 4096).unwrap(),
        b"synthetic-in-place-edit"
    );
}

#[tokio::test]
async fn first_creation_does_not_replace_a_competing_connection() {
    let home = Home::new();
    let mut client = Fake::new(&home.0);
    client.connection_contender = true;
    let plan =
        codex::plan_replace(&client.config(), &home.0, "https://example.com", None, true).unwrap();
    let failure = codex::apply_replace_detailed(&mut client, &home.0, plan)
        .await
        .err()
        .unwrap();
    assert!(failure.recovery_pending);
    assert!(left(&failure.rollback, &home.connection()));
    assert_eq!(
        state::read(&home.connection(), 16384).unwrap(),
        b"competing first creator"
    );
    assert!(home.token().exists());
}
