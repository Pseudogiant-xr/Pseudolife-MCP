//! Setup's private host helper: backups, unrelated settings, and safe retry.
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    process::{Command, Output},
};
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("r1-setup-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
    fn path(&self) -> PathBuf {
        self.0.join("config.yaml")
    }
    fn run(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .args(["maintainer", "setup"])
            .args(args)
            .env("PSEUDOLIFE_MCP_CONFIG", self.path())
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("PSEUDOLIFE_MCP_DATA_DIR", self.0.join("data"))
            .env("LOCALAPPDATA", self.0.join("local"))
            .env("PSEUDOLIFE_DAEMON_EXEC", "1")
            .env_remove("PSEUDOLIFE_MCP_DATABASE_URL")
            .env_remove("PSEUDOLIFE_MCP_TOKEN")
            .env_remove("PSEUDOLIFE_MCP_TOKEN_FILE")
            .output()
            .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn native_write_preserves_values_and_byte_exact_backup_after_a_refusal() {
    let f = Fixture::new();
    let before = "memory:\n  preset: flat\n  nested: {flag: yes, octal: 017, label: 'on', float_string: 1.2e3, mixed: yEs, invalid_octal: 08}\ncoordination:\n  enabled: false\n  maintainer:\n    maintainer_per_recipient_per_hour: 7\n";
    fs::write(f.path(), before).unwrap();
    let refused = f.run(&["--host-write", "localhost", "http://localhost:80"]);
    assert_eq!(refused.status.code(), Some(2));
    assert_eq!(fs::read_to_string(f.path()).unwrap(), before);
    assert_eq!(fs::read_dir(&f.0).unwrap().count(), 1);
    let written = f.run(&["--host-write", "localhost", "http://localhost:8765"]);
    assert!(
        written.status.success(),
        "{}",
        String::from_utf8_lossy(&written.stderr)
    );
    let report: Value = serde_json::from_slice(&written.stdout).unwrap();
    assert_eq!(
        fs::read_to_string(report["backup"].as_str().unwrap()).unwrap(),
        before
    );
    let after: Value = serde_json::from_slice(&fs::read(f.path()).unwrap()).unwrap();
    assert_eq!(
        after,
        json!({"memory":{"preset":"flat","nested":{"flag":true,"octal":15,"label":"on","float_string":"1.2e3","mixed":"yEs","invalid_octal":"08"}},"coordination":{"enabled":false,"maintainer":{"maintainer_per_recipient_per_hour":7,"rp_id":"localhost","origin":"http://localhost:8765"}}})
    );
    let state = f.run(&["--host-state"]);
    assert!(state.status.success());
    let state: Value = serde_json::from_slice(&state.stdout).unwrap();
    assert!(state["problem"].is_null());
    assert_eq!(state["origin"], "http://localhost:8765");
    // The name remains readable after a bank/read failure; a subsequent setup
    // keeps it rather than selecting a different browser passkey identity.
    assert!(state["keys"].is_null());
    assert!(state["keys_error"].is_string());
}

#[test]
fn unsafe_rewrites_leave_the_config_and_directory_unchanged() {
    let f = Fixture::new();
    for before in [
        "coordination: []\n",
        "coordination:\n  maintainer: null\n",
        "shared: &x {value: 1}\nother: *x\n",
        "saved: 2026-11-02\n",
        "n: 9999999999999999999999999999999999999999\n",
    ] {
        fs::write(f.path(), before).unwrap();
        let output = f.run(&["--host-write", "localhost", "http://localhost:8765"]);
        assert!(!output.status.success());
        assert_eq!(fs::read_to_string(f.path()).unwrap(), before);
        assert_eq!(
            fs::read_dir(&f.0).unwrap().count(),
            1,
            "refusal created a backup or temporary file"
        );
    }
}

#[test]
fn check_never_mutates_an_unconfigured_host() {
    let f = Fixture::new();
    fs::write(f.path(), "memory: {preset: flat}\n").unwrap();
    let output = f.run(&["--check"]);
    assert_eq!(output.status.code(), Some(1));
    assert!(
        String::from_utf8(output.stdout)
            .unwrap()
            .contains("not set up")
    );
    assert_eq!(
        fs::read_to_string(f.path()).unwrap(),
        "memory: {preset: flat}\n"
    );
    assert_eq!(fs::read_dir(&f.0).unwrap().count(), 1);
}
