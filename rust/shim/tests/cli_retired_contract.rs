#![forbid(unsafe_code)]
//! What the native client no longer does refuses plainly: `serve` (the
//! daemon is the separate `pseudolife-daemon` binary in the Docker tier),
//! `embedded` (retired with file mode) and every command that finds a
//! lite-tier bank. Each refusal has its near miss beside it: the path that
//! still runs.
#[path = "common/cli.rs"]
mod cli;
use pseudolife_stdio::cli::retired;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

struct Home(PathBuf);

impl Home {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!("retired-{label}-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(path.join("cwd")).unwrap();
        fs::create_dir_all(path.join("bin")).unwrap();
        Self(path)
    }
    /// A cleared environment: an emptied home, no `docker` on `PATH`, a closed
    /// daemon address and a browser that is no program.
    fn command(&self, arguments: &[&str]) -> Command {
        let mut command = cli::cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
        command
            .args(arguments)
            .current_dir(self.0.join("cwd"))
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("APPDATA", self.0.join("AppData").join("Roaming"))
            .env("LOCALAPPDATA", self.0.join("AppData").join("Local"))
            .env("XDG_DATA_HOME", self.0.join("share"))
            .env("XDG_CONFIG_HOME", self.0.join("config"))
            .env("PATH", self.0.join("bin"))
            .env("COLUMNS", "80")
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9")
            .env("PSEUDOLIFE_DOCKER", self.0.join("bin").join("no-docker"))
            .env("BROWSER", "pl-retired-no-browser");
        command
    }
    /// A data directory holding a lite-tier bank's marker.
    fn lite_bank(&self, name: &str) -> PathBuf {
        let bank = self.0.join(name);
        fs::create_dir_all(bank.join("embedded_pg")).unwrap();
        fs::write(bank.join("embedded_pg").join("PG_VERSION"), "18\n").unwrap();
        bank
    }
}

impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn line(text: &str) -> Vec<u8> {
    cli::native_text(&format!("{text}\n"))
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn assert_never_deferred(output: &Output) {
    assert!(
        !stderr(output).contains("deferred"),
        "a retired path still defers: {}",
        stderr(output)
    );
}

/// Run `command` to its exit within `limit`; a child still running then (a
/// `serve` that started serving instead of refusing) is killed and fails.
fn bounded(mut command: Command, limit: std::time::Duration) -> Output {
    use std::process::Stdio;
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let start = std::time::Instant::now();
    while child.try_wait().unwrap().is_none() {
        if start.elapsed() > limit {
            let _ = child.kill();
            let _ = child.wait();
            panic!("still running after {limit:?}: it served instead of refusing");
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    child.wait_with_output().unwrap()
}

#[test]
fn serve_refuses_and_names_the_docker_daemon() {
    let home = Home::new("serve");
    for arguments in [&["serve"][..], &["serve", "--port", "8765"]] {
        // An unused port, so a serving regression can never meet a real daemon.
        let mut command = home.command(arguments);
        command.env("PSEUDOLIFE_MCP_PORT", "9");
        let output = bounded(command, std::time::Duration::from_secs(20));
        assert_eq!(output.status.code(), Some(1), "{arguments:?}");
        assert!(output.stdout.is_empty(), "{arguments:?}");
        assert_eq!(
            output.stderr,
            line(&retired::serve_message()),
            "{arguments:?}"
        );
    }
}

#[test]
fn serve_still_reaches_the_maintainer_testing_candidate_by_its_opt_in() {
    // The near miss: the harness's opt-in admits the bounded candidate, which
    // then refuses an unparsable port before it binds anything.
    let home = Home::new("serve-candidate");
    let output = home
        .command(&["serve"])
        .env(retired::SENT_CANDIDATE_ENV, "1")
        .env("PSEUDOLIFE_MCP_PORT", "not-a-port")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(output.stderr, line("pseudolife-stdio serve: invalid port"));
    // Any other value is no opt-in.
    let output = home
        .command(&["serve"])
        .env(retired::SENT_CANDIDATE_ENV, "true")
        .env("PSEUDOLIFE_MCP_PORT", "not-a-port")
        .output()
        .unwrap();
    assert_eq!(output.stderr, line(&retired::serve_message()));
}

#[test]
fn embedded_refuses_as_retired() {
    let home = Home::new("embedded");
    for arguments in [&["embedded"][..], &["embedded", "--help"]] {
        let output = home.command(arguments).output().unwrap();
        assert_eq!(output.status.code(), Some(1), "{arguments:?}");
        assert!(output.stdout.is_empty(), "{arguments:?}");
        assert_eq!(
            output.stderr,
            line(&retired::embedded_message()),
            "{arguments:?}"
        );
    }
}

#[test]
fn maintainer_refuses_a_lite_bank_and_still_reports_no_bank_without_one() {
    let home = Home::new("maintainer");
    let bank = home.lite_bank("bank");
    let output = home
        .command(&["maintainer", "list"])
        .env("PSEUDOLIFE_MCP_DATA_DIR", &bank)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(
        output.stderr,
        line(&format!("error: {}", retired::lite_bank_refusal(&bank)))
    );
    // The near miss: a data dir without a bank, and no daemon container.
    let plain = home.0.join("plain");
    fs::create_dir_all(&plain).unwrap();
    let output = home
        .command(&["maintainer", "list"])
        .env("PSEUDOLIFE_MCP_DATA_DIR", &plain)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(
        output.stderr,
        line(
            "error: no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp maintainer ...` on its host)"
        )
    );
    // A database URL is used whatever the data dir holds.
    let output = home
        .command(&["maintainer", "list"])
        .env("PSEUDOLIFE_MCP_DATA_DIR", &bank)
        .env(
            "PSEUDOLIFE_MCP_DATABASE_URL",
            "postgresql://nobody@127.0.0.1:9/none",
        )
        .output()
        .unwrap();
    assert!(
        !stderr(&output).contains("lite-tier"),
        "{}",
        stderr(&output)
    );
    assert_never_deferred(&output);
}

#[test]
fn maintainer_setup_refuses_a_lite_bank_without_naming_python() {
    let home = Home::new("maintainer-setup");
    let bank = home.lite_bank("bank");
    let output = home
        .command(&[
            "maintainer",
            "setup",
            "--host-write",
            "example.com",
            "https://example.com",
        ])
        .env("PSEUDOLIFE_MCP_DATA_DIR", &bank)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(4));
    let text = stderr(&output);
    assert!(text.contains(&retired::lite_bank_refusal(&bank)), "{text}");
    assert!(!text.contains("python"), "{text}");
    assert!(!text.contains("phase4"), "{text}");
    assert!(
        !bank.join("config.yaml").exists(),
        "a refused setup wrote the config"
    );
}

#[test]
fn board_audit_refuses_a_lite_bank_and_still_reports_no_bank_without_one() {
    let home = Home::new("board-audit");
    let bank = home.lite_bank("bank");
    let output = home
        .command(&["board-audit", "verify"])
        .env("PSEUDOLIFE_MCP_DATA_DIR", &bank)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(
        output.stderr,
        line(&format!(
            "board-audit: {}",
            retired::lite_bank_refusal(&bank)
        ))
    );
    let plain = home.0.join("plain");
    fs::create_dir_all(&plain).unwrap();
    let output = home
        .command(&["board-audit", "verify"])
        .env("PSEUDOLIFE_MCP_DATA_DIR", &plain)
        .output()
        .unwrap();
    assert_eq!(
        output.stderr,
        line(
            "board-audit: no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp board-audit ...` on its host)"
        )
    );
}

#[test]
fn invite_refuses_a_lite_bank_and_still_needs_the_daemon_without_one() {
    let home = Home::new("invite");
    let bank = home.lite_bank("bank");
    let output = home
        .command(&["invite", "laptop"])
        .env("PSEUDOLIFE_MCP_DATA_DIR", &bank)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(4));
    assert_eq!(
        output.stderr,
        line(&format!("invite: {}", retired::lite_bank_refusal(&bank)))
    );
    let plain = home.0.join("plain");
    fs::create_dir_all(&plain).unwrap();
    let output = home
        .command(&["invite", "laptop"])
        .env("PSEUDOLIFE_MCP_DATA_DIR", &plain)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(4));
    assert!(!stderr(&output).contains("lite"), "{}", stderr(&output));
    assert_never_deferred(&output);
}

#[test]
fn the_lite_default_is_a_lite_bank_only_when_it_holds_one() {
    let home = Home::new("default");
    let lite = if cfg!(windows) {
        home.0.join("AppData").join("Local").join("pseudolife-mcp")
    } else if cfg!(target_os = "macos") {
        home.0
            .join("Library")
            .join("Application Support")
            .join("pseudolife-mcp")
    } else {
        home.0.join("share").join("pseudolife-mcp")
    };
    // The native client installs its runtimes here: no bank, no refusal.
    fs::create_dir_all(lite.join("runtimes")).unwrap();
    let output = home.command(&["board-audit", "verify"]).output().unwrap();
    assert!(
        !stderr(&output).contains("lite-tier"),
        "{}",
        stderr(&output)
    );
    fs::create_dir_all(lite.join("embedded_pg")).unwrap();
    fs::write(lite.join("embedded_pg").join("PG_VERSION"), "18\n").unwrap();
    for (arguments, prefix, code) in [
        (&["board-audit", "verify"][..], "board-audit: ", 2),
        (&["maintainer", "list"], "error: ", 2),
        (&["invite", "laptop"], "invite: ", 4),
    ] {
        let output = home.command(arguments).output().unwrap();
        assert_eq!(output.status.code(), Some(code), "{arguments:?}");
        assert_eq!(
            output.stderr,
            line(&format!("{prefix}{}", retired::lite_bank_refusal(&lite))),
            "{arguments:?}"
        );
    }
}
