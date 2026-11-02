//! Offline recovery must refuse before any database or private-state effect.
use std::{
    fs,
    path::PathBuf,
    process::{Command, Output},
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("r1-recovery-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
    fn config(&self, text: &str) -> PathBuf {
        let path = self.0.join("config.yaml");
        fs::write(&path, text).unwrap();
        path
    }
    fn run(&self, config: &std::path::Path, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .arg("coordination-recovery")
            .args(args)
            .arg("--config")
            .arg(config)
            .env_remove("PSEUDOLIFE_MCP_DATABASE_URL")
            .env_remove("PSEUDOLIFE_MCP_TOKENS")
            .env_remove("PSEUDOLIFE_AGENT_COORDINATION")
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .current_dir(&self.0)
            .output()
            .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn diagnostic(output: &Output) -> String {
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    String::from_utf8(output.stderr.clone()).unwrap()
}

#[test]
fn default_enabled_and_both_confirmations_fail_before_database_resolution() {
    let f = Fixture::new();
    let enabled = f.config("memory:\n  preset: flat\n");
    assert!(
        diagnostic(&f.run(
            &enabled,
            &["recover", "--confirm-daemon-stopped", "--confirm-restore"]
        ))
        .contains("coordination.enabled: false")
    );
    let disabled = f.config("coordination:\n  enabled: false\n");
    assert!(
        diagnostic(&f.run(&disabled, &["recover", "--confirm-restore"]))
            .contains("--confirm-daemon-stopped")
    );
    assert!(
        diagnostic(&f.run(&disabled, &["recover", "--confirm-daemon-stopped"]))
            .contains("--confirm-restore is required")
    );
    assert!(
        diagnostic(&f.run(
            &disabled,
            &["recover", "--confirm-daemon-stopped", "--confirm-restore"]
        ))
        .contains("no database is started automatically")
    );
}

#[test]
fn repository_output_existing_state_and_reserved_owner_are_never_mutated() {
    let f = Fixture::new();
    let config = f.config(
        "coordination:\n  enabled: false\n  allowed_principals: [alice, daemon, maintainer]\n",
    );
    let repo = f.0.join("checkout");
    fs::create_dir(&repo).unwrap();
    fs::write(repo.join(".git"), "gitdir: fixture").unwrap();
    let state = repo.join("agent.json");
    let invoke = |principal: &str, path: &std::path::Path| {
        f.run(
            &config,
            &[
                "rebind",
                "--confirm-daemon-stopped",
                "--agent",
                "1234567890abcdef1234567890abcdef",
                "--principal",
                principal,
                "--bank-url",
                "http://localhost:8765",
                "--state",
                path.to_str().unwrap(),
            ],
        )
    };
    assert!(diagnostic(&invoke("alice", &state)).contains("outside a Git repository"));
    assert!(!state.exists());
    let state = f.0.join("existing.json");
    fs::write(&state, "keep").unwrap();
    assert!(diagnostic(&invoke("alice", &state)).contains("existing files are never replaced"));
    assert_eq!(fs::read(&state).unwrap(), b"keep");
    for principal in ["daemon", "maintainer"] {
        assert!(
            diagnostic(&invoke(principal, &f.0.join("reserved.json")))
                .contains("requested owner must be")
        );
    }
    assert!(!f.0.join("reserved.json").exists());
}

#[test]
fn failed_connection_keeps_the_empty_reservation_for_explicit_diagnosis() {
    let f = Fixture::new();
    let config = f.config("coordination:\n  enabled: false\n  allowed_principals: [alice]\n");
    let state = f.0.join("new.json");
    let output = f.run(
        &config,
        &[
            "rebind",
            "--confirm-daemon-stopped",
            "--agent",
            "1234567890abcdef1234567890abcdef",
            "--principal",
            "alice",
            "--bank-url",
            "http://localhost:8765",
            "--state",
            state.to_str().unwrap(),
        ],
    );
    assert!(diagnostic(&output).contains("no database is started automatically"));
    assert_eq!(fs::read(&state).unwrap(), b"");
}
