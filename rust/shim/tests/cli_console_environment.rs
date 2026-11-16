//! Python-only settings cannot disable the native Console command.
//! Unix process test; Windows covers the same values in test_console_browser.py.
#![cfg(unix)]
use std::process::Command;

#[test]
fn python_integer_limit_never_defers_console() {
    let home = std::env::temp_dir().join(format!("console-environment-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&home).unwrap();
    for value in ["0", "640", "4300", "invalid"] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
        command.env_clear();
        for key in ["HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"] {
            command.env(key, &home);
        }
        let output = command
            .args(["console", "--help"])
            .env("COLUMNS", "80")
            .env("PYTHONINTMAXSTRDIGITS", value)
            .env("BROWSER", "console-test-no-browser")
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1")
            .output()
            .unwrap();
        assert!(output.status.success(), "{value}: {:?}", output);
        assert!(String::from_utf8_lossy(&output.stdout).contains("usage:"));
    }
    std::fs::remove_dir_all(home).unwrap();
}
