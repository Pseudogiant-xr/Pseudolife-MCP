//! The native updater as the child of a client-step hand-off, given exactly
//! what the Python updater's `Update._hand_off` gives its child (the frozen
//! 0.23.x Python parent itself only ever starts a Python runtime; a bridge in
//! that child may pass the same inputs on): the update lock held by the
//! parent, `PSEUDOLIFE_UPDATE_HANDOFF*` naming the parent and the report
//! file, `update --clients-only --tag <v> --daemon-url <url>`. Everything runs
//! in a throwaway home with no Docker, no clients and a test-local /health.
use serde_json::Value;
use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    path::PathBuf,
    process::{Command, Output},
};

const TARGET: &str = "9.9.9";

struct Home {
    path: PathBuf,
    url: String,
}
impl Home {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("native-handoff-child-{}", uuid::Uuid::new_v4()));
        pseudolife_stdio::board::state::private_dir(&path).unwrap();
        fs::create_dir_all(path.join("data")).unwrap();
        // /health of a daemon at TARGET, answered until the test ends.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let mut request = [0u8; 4096];
                let _ = stream.read(&mut request);
                let body = format!("{{\"version\":\"{TARGET}\",\"status\":\"ok\"}}");
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        Self { path, url }
    }
    fn data(&self) -> PathBuf {
        self.path.join("data")
    }
    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
        command.env_clear();
        for key in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        let p = &self.path;
        command
            .env("HOME", p)
            .env("USERPROFILE", p)
            .env("LOCALAPPDATA", p.join("Local"))
            .env("APPDATA", p.join("Roaming"))
            .env("XDG_DATA_HOME", p.join("share"))
            .env("CODEX_HOME", p.join("codex"))
            .env("CLAUDE_CONFIG_DIR", p.join("claude"))
            .env("PATH", p.join("empty-path"))
            .env("PSEUDOLIFE_MCP_DATA_DIR", self.data())
            .env("PSEUDOLIFE_LEASE_LOCK_DIR", p.join("locks"))
            .env("PSEUDOLIFE_SHIM_RUNTIMES", p.join("runtimes"))
            .env(
                "PSEUDOLIFE_SHIM_LAUNCHER",
                p.join("bin").join(if cfg!(windows) {
                    "pseudolife-mcp.exe"
                } else {
                    "pseudolife-mcp"
                }),
            )
            // Never the real Docker: the child must take the client tier.
            .env("PSEUDOLIFE_DOCKER", p.join("no-docker-here"))
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1")
            .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
            .env("BROWSER", "")
            .current_dir(p);
        command
    }
    /// The child given what a Python parent gives its child
    /// (`Update._hand_off`): the same arguments after the module, the same
    /// three variables and the two Python stream settings it adds.
    fn handed_off(&self, from: &str, report: &PathBuf) -> Output {
        self.command()
            .args([
                "update",
                "--clients-only",
                "--tag",
                TARGET,
                "--daemon-url",
                &self.url,
                "--allow-downgrade",
            ])
            .env("PSEUDOLIFE_UPDATE_HANDOFF", from)
            .env("PSEUDOLIFE_UPDATE_HANDOFF_REPORT", report)
            .env(
                "PSEUDOLIFE_UPDATE_HANDOFF_RUNTIME",
                self.path.join("py-runtime"),
            )
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUNBUFFERED", "1")
            .output()
            .unwrap()
    }
    /// Hold the update lock as the parent does, and name this process in
    /// its pid file. Both engines lock byte zero (Windows) or flock (POSIX);
    /// std's whole-file lock overlaps byte zero and excludes the child too.
    fn hold_lock(&self) -> fs::File {
        let lock = fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.data().join("update.lock"))
            .unwrap();
        lock.try_lock().unwrap();
        fs::write(
            self.data().join("update.lock.pid"),
            format!("{}\n", std::process::id()),
        )
        .unwrap();
        lock
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

fn text(output: &Output) -> String {
    format!(
        "exit {:?}\nstdout:\n{}\nstderr:\n{}",
        output.status.code(),
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

#[test]
fn a_child_started_as_python_starts_it_borrows_the_lock_and_writes_its_report() {
    let home = Home::new();
    let _lock = home.hold_lock();
    let pid = std::process::id().to_string();
    let report = home.path.join("report.json");
    let output = home.handed_off(&pid, &report);
    let shown = text(&output);
    assert!(
        !shown.contains("another pseudolife-mcp update is running"),
        "{shown}"
    );
    assert!(
        String::from_utf8_lossy(&output.stdout).contains(&format!(
            "continuing the client side with {}'s own code",
            env!("CARGO_PKG_VERSION")
        )),
        "{shown}"
    );
    // The fields the Python parent reads back, in its own names.
    let child: Value = serde_json::from_slice(&fs::read(&report).unwrap()).unwrap();
    assert!(child["clients"].is_object(), "{child:#}\n{shown}");
    assert!(child["steps"].is_array());
    assert!(child["codex_reapproval"].is_string());
    assert!(child["ok"].is_boolean());
    assert_eq!(
        child["exit_code"].as_i64(),
        output.status.code().map(i64::from),
        "{shown}"
    );
    assert_eq!(child["target"], TARGET);
    // The borrower neither rewrote nor removed the holder's pid.
    assert_eq!(
        fs::read_to_string(home.data().join("update.lock.pid"))
            .unwrap()
            .trim(),
        pid
    );
}

#[test]
fn without_the_marker_or_with_another_holder_the_lock_is_refused() {
    let home = Home::new();
    let _lock = home.hold_lock();
    let pid = std::process::id().to_string();
    let report = home.path.join("report.json");
    // Another holder: the marker names a pid that does not hold the lock.
    let other = (std::process::id() + 1).to_string();
    let output = home.handed_off(&other, &report);
    let shown = text(&output);
    assert_eq!(output.status.code(), Some(2), "{shown}");
    assert!(
        shown.contains(&format!(
            "another pseudolife-mcp update is running (pid {pid})"
        )),
        "{shown}"
    );
    let child: Value = serde_json::from_slice(&fs::read(&report).unwrap()).unwrap();
    assert!(child["clients"].is_null(), "{child:#}");
    // No marker at all: an ordinary run, which neither borrows nor reports.
    fs::remove_file(&report).unwrap();
    let output = home
        .command()
        .args([
            "update",
            "--clients-only",
            "--tag",
            TARGET,
            "--daemon-url",
            &home.url,
        ])
        .env("PSEUDOLIFE_UPDATE_HANDOFF_REPORT", &report)
        .output()
        .unwrap();
    let shown = text(&output);
    assert_eq!(output.status.code(), Some(2), "{shown}");
    assert!(
        shown.contains("another pseudolife-mcp update is running"),
        "{shown}"
    );
    assert!(!report.exists(), "{shown}");
}

#[test]
fn the_runtime_answers_the_hand_off_probe() {
    let home = Home::new();
    let output = home
        .command()
        .args(["update", "--client-handoff-protocols"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0), "{}", text(&output));
    assert_eq!(
        String::from_utf8_lossy(&output.stdout).replace("\r\n", "\n"),
        format!("{}\nclient-handoff-v1\n", env!("CARGO_PKG_VERSION"))
    );
    // The probe a parent runs first, which every native updater answers.
    let output = home
        .command()
        .args(["update", "--native-capability"])
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8_lossy(&output.stdout).trim(),
        "pseudolife-mcp native-update-v1"
    );
}
