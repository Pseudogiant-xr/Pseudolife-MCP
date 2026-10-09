#![forbid(unsafe_code)]
//! `pseudolife-mcp connect` without Python or PostgreSQL: canonical-argv
//! deferrals, refusals before any connection, and file-only plans against a
//! stand-in `/health`. Expected bytes are the Python oracle's
//! (`connect_cli.py`), captured by the CLI-CONNECT harness row; the
//! differential row proves verification, writes and rollback.
mod common;
use common::cli::{cleared_command, native_text};
use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};

const DEFERRED: &str = "pseudolife-stdio: mode 'connect' is deferred in this candidate\n";
const DEAD: &str = "http://127.0.0.1:9";

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("connect-contract-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(path.join("cwd")).unwrap();
        Self(path)
    }
    fn command(&self, ambient_url: &str) -> Command {
        let mut command = cleared_command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")));
        let home = &self.0;
        command
            .current_dir(home.join("cwd"))
            .env("HOME", home)
            .env("USERPROFILE", home)
            .env("APPDATA", home.join("AppData").join("Roaming"))
            .env("LOCALAPPDATA", home.join("AppData").join("Local"))
            .env("CODEX_HOME", home.join(".codex"))
            .env("XDG_CONFIG_HOME", home.join(".config"))
            .env("XDG_DATA_HOME", home.join(".local").join("share"))
            .env("PSEUDOLIFE_MCP_DAEMON_URL", ambient_url)
            .stdin(Stdio::piped());
        command
    }
    fn run(&self, ambient_url: &str, args: &[&str]) -> Output {
        self.command(ambient_url)
            .arg("connect")
            .args(args)
            .output()
            .unwrap()
    }
    fn tree(&self) -> BTreeMap<PathBuf, Option<Vec<u8>>> {
        fn walk(dir: &Path, out: &mut BTreeMap<PathBuf, Option<Vec<u8>>>) {
            for entry in std::fs::read_dir(dir).unwrap().flatten() {
                let path = entry.path();
                if path.is_dir() {
                    out.insert(path.clone(), None);
                    walk(&path, out);
                } else {
                    out.insert(path.clone(), Some(std::fs::read(&path).unwrap()));
                }
            }
        }
        let mut out = BTreeMap::new();
        walk(&self.0, &mut out);
        out
    }
    fn write(&self, relative: &str, text: &str) -> PathBuf {
        let path = self.0.join(relative);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, text).unwrap();
        path
    }
    fn show(&self, relative: &str) -> String {
        self.0.join(relative).to_str().unwrap().to_owned()
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// `/health` answering `{"status": "ok", ...}`; every request is recorded.
struct Health {
    url: String,
    requests: Arc<Mutex<Vec<String>>>,
    stop: Arc<AtomicBool>,
}
impl Health {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let (seen, stopped) = (requests.clone(), stop.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                if stopped.load(Ordering::SeqCst) {
                    break;
                }
                let Ok(mut stream) = stream else { continue };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new();
                let _ = reader.read_line(&mut line);
                loop {
                    let mut header = String::new();
                    if reader.read_line(&mut header).unwrap_or(0) <= 2 {
                        break;
                    }
                }
                let target = line.split_whitespace().nth(1).unwrap_or("").to_owned();
                seen.lock().unwrap().push(target.clone());
                let (status, body) = if target == "/health" {
                    (
                        "200 OK",
                        r#"{"status": "ok", "auth": true, "version": "0.0.0-fixture"}"#,
                    )
                } else {
                    ("404 Not Found", "{}")
                };
                let _ = write!(
                    stream,
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        Self {
            url,
            requests,
            stop,
        }
    }
    fn requests(&self) -> Vec<String> {
        self.requests.lock().unwrap().clone()
    }
}
impl Drop for Health {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = std::net::TcpStream::connect(self.url.trim_start_matches("http://"));
    }
}

fn deferred(output: &Output) {
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert!(output.stdout.is_empty(), "{output:?}");
    assert_eq!(output.stderr, native_text(DEFERRED));
}

fn refused(output: &Output, code: i32, stderr: &str) {
    assert_eq!(output.status.code(), Some(code), "{output:?}");
    assert!(output.stdout.is_empty(), "{output:?}");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        String::from_utf8_lossy(&native_text(stderr))
    );
}

fn absent_rows(url: &str) -> String {
    format!(
        "  absent   claude-code registration\n             no registration; connect never creates one. To register: claude mcp add --scope user pseudolife-memory -e PSEUDOLIFE_WRITER_ID=claude-code -e PSEUDOLIFE_MCP_TOKEN_FILE=<token file> -e PSEUDOLIFE_MCP_DAEMON_URL={url} -- <shim path>\n  absent   codex registration\n             no registration; connect never creates one. To register: codex mcp add pseudolife-memory --env PSEUDOLIFE_WRITER_ID=codex --env PSEUDOLIFE_MCP_TOKEN_FILE=<token file> --env PSEUDOLIFE_MCP_DAEMON_URL={url} -- <shim path>\n  absent   claude-desktop registration\n             no registration; connect never creates one. To register: python ops/register_claude_desktop.py --command <shim path> --daemon-url {url} --token-file <token file> (from a checkout; the installers run it for --client claude-desktop)\n  absent   gemini registration\n             no registration; connect never creates one. To register: gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_TOKEN_FILE=<token file> -e PSEUDOLIFE_MCP_DAEMON_URL={url} pseudolife-memory <shim path>\n"
    )
}

#[test]
fn non_canonical_argv_defers_without_effect() {
    let home = Home::new();
    home.write(".claude.json", "{\"mcpServers\": {}}\n");
    let before = home.tree();
    for shape in [
        &[][..],
        &["--dry-run", DEAD],
        &[DEAD, "--code", "7K3Q-M9XA-2PDF"],
        &[DEAD, "--read-code"],
        &[DEAD, "--help"],
        &[DEAD, "-h"],
        &[DEAD, "--yes", "--yes"],
        &[DEAD, "--token-file=t"],
        &[DEAD, "--token-file", "-t"],
        &[DEAD, "--tok", "t"],
        &[DEAD, "--client"],
        &[DEAD, "--", "--yes"],
        &[DEAD, "extra"],
        &["http://ex\u{e4}mple:9", "--dry-run"],
        &["http://[v1.x]:9", "--dry-run"],
    ] {
        deferred(&home.run(DEAD, shape));
    }
    assert_eq!(home.tree(), before);
}

#[test]
fn usage_refusals_match_the_oracle() {
    let home = Home::new();
    refused(
        &home.run(DEAD, &["http://127.0.0.1:8765/mcp", "--dry-run"]),
        2,
        "connect: the daemon URL given (http://127.0.0.1:8765) is not an http(s) origin: give one such as http://100.64.0.2:8765, with no path, query, fragment or credentials\n",
    );
    refused(
        &home.run(DEAD, &["100.64.0.2:8765"]),
        2,
        "connect: the daemon URL given (<unreadable URL>) is not an http(s) origin: give one such as http://100.64.0.2:8765, with no path, query, fragment or credentials\n",
    );
    refused(
        &home.run(DEAD, &["ftp://Host.Example:21"]),
        2,
        "connect: the daemon URL given (ftp://host.example:21) is not an http(s) origin: give one such as http://100.64.0.2:8765, with no path, query, fragment or credentials\n",
    );
    refused(
        &home.run(DEAD, &[DEAD, "--client", "claude-code,cursor"]),
        2,
        "connect: --client takes claude-code, codex, claude-desktop, gemini or all (got 'claude-code,cursor')\n",
    );
    refused(
        &home.run(DEAD, &[DEAD, "--read-token", "--yes"]),
        2,
        "connect: --read-token creates the file --token-file names; pass both\n",
    );
    home.write(".pseudolife-mcp/claude-code.token", "x");
    let token = home.0.join(".pseudolife-mcp").join("claude-code.token");
    refused(
        &home.run(
            DEAD,
            &[
                DEAD,
                "--token-file",
                "~/.pseudolife-mcp/claude-code.token",
                "--read-token",
                "--yes",
            ],
        ),
        2,
        &format!(
            "connect: {} already exists; --read-token creates a token file but never replaces one\n",
            token.display()
        ),
    );
    let output = home.run(DEAD, &["http://user:pw@h:1/x", "--json"]);
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stderr.is_empty());
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&native_text(
            "{\n  \"url\": null,\n  \"remote\": null,\n  \"dry_run\": false,\n  \"daemon\": null,\n  \"rows\": [],\n  \"verification\": [],\n  \"warnings\": [],\n  \"notes\": [],\n  \"restart\": null,\n  \"rollback\": [],\n  \"error\": \"the daemon URL given (http://h:1) is not an http(s) origin: give one such as http://100.64.0.2:8765, with no path, query, fragment or credentials\",\n  \"exit\": 2\n}\n"
        ))
    );
}

#[test]
fn an_unreachable_daemon_is_refused_with_nothing_changed() {
    let home = Home::new();
    let before = home.tree();
    refused(
        &home.run(DEAD, &[DEAD, "--dry-run"]),
        4,
        "connect: the daemon at http://127.0.0.1:9 did not answer /health with status ok; nothing was changed\n",
    );
    assert_eq!(home.tree(), before);
}

#[test]
fn a_codex_config_defers_before_any_request() {
    let home = Home::new();
    home.write(
        ".codex/config.toml",
        "[mcp_servers.pseudolife-memory]\ncommand = \"x\"\n",
    );
    let health = Health::start();
    let before = home.tree();
    deferred(&home.run(&health.url, &[&health.url, "--dry-run"]));
    deferred(&home.run(&health.url, &[&health.url, "--client", "codex", "--yes"]));
    assert!(health.requests().is_empty());
    assert_eq!(home.tree(), before);
}

#[test]
fn no_registration_exits_3_with_the_register_commands() {
    let home = Home::new();
    let health = Health::start();
    let url = health.url.clone();
    let output = home.run(&url, &[&url, "--yes"]);
    assert_eq!(output.status.code(), Some(3), "{output:?}");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&native_text(&format!(
            "connect: {url} (this machine; daemon 0.0.0-fixture, auth on)\n{}",
            absent_rows(&url)
        )))
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        String::from_utf8_lossy(&native_text(
            "connect: no registration of the shim was found for claude-code, codex, claude-desktop, gemini; register a client first (the commands are above), or run the installer.\n"
        ))
    );
    assert_eq!(health.requests(), ["/health"]);
}

fn claude_registration(home: &Home, url: &str) -> String {
    let command = home.show("shim-bin");
    let text = format!(
        "{{\n  \"mcpServers\": {{\n    \"pseudolife-memory\": {{\n      \"command\": {},\n      \"env\": {{\n        \"PSEUDOLIFE_MCP_DAEMON_URL\": \"{url}\",\n        \"PSEUDOLIFE_MCP_TOKEN_FILE\": \"t.token\"\n      }}\n    }}\n  }}\n}}\n",
        serde_json::to_string(&command).unwrap()
    );
    home.write(".claude.json", &text);
    text
}

#[test]
fn a_dry_run_plans_without_writing_or_sending_a_token() {
    let home = Home::new();
    let health = Health::start();
    let url = health.url.clone();
    claude_registration(&home, "http://100.64.0.1:8765");
    let before = home.tree();
    let output = home.run(&url, &[&url, "--client", "claude-code", "--dry-run"]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let config = home.show(".claude.json");
    let settings = Path::new(&home.0)
        .join(".claude")
        .join("settings.json")
        .to_str()
        .unwrap()
        .to_owned();
    let command = home.show("shim-bin");
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&native_text(&format!(
            "connect: {url} (this machine; daemon 0.0.0-fixture, auth on)\n  change   claude-code registration: {config} [pseudolife-memory]\n             PSEUDOLIFE_MCP_DAEMON_URL: http://100.64.0.1:8765 -> {url}\n             note: runs {command}, not the shim launcher; connect leaves the command alone (`pseudolife-mcp update --clients-only`, or `python ops/shim_runtime.py migrate` from a checkout, moves it)\n  change   claude-code settings: {settings} [env]\n             PSEUDOLIFE_MCP_DAEMON_URL: (unset) -> {url}\n             PSEUDOLIFE_MCP_TOKEN_FILE: (unset) -> t.token\ndry run: nothing was written and no token was sent; the credentials are verified only on a real run\n"
        )))
    );
    assert!(output.stderr.is_empty());
    assert_eq!(health.requests(), ["/health"]);
    assert_eq!(home.tree(), before);
}

#[test]
fn a_current_registration_has_nothing_to_write() {
    let home = Home::new();
    let health = Health::start();
    let url = health.url.clone();
    claude_registration(&home, &url);
    home.write(
        ".claude/settings.json",
        &format!(
            "{{\"env\": {{\"PSEUDOLIFE_MCP_TOKEN_FILE\": \"t.token\", \"PSEUDOLIFE_MCP_DAEMON_URL\": \"{url}\"}}}}"
        ),
    );
    let before = home.tree();
    let output = home.run(&url, &[&url, "--client", "claude-code", "--yes"]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(
        text.ends_with(
            &String::from_utf8(native_text(&format!(
                "every place already names {url}; nothing to write\n"
            )))
            .unwrap()
        ),
        "{text}"
    );
    assert!(text.contains("  current  claude-code registration: "));
    assert_eq!(health.requests(), ["/health"]);
    assert_eq!(home.tree(), before);
}

#[test]
fn inputs_outside_the_native_domain_defer_before_any_request() {
    let health = Health::start();
    let url = health.url.clone();
    // A lone surrogate: a Python str that no Rust String holds.
    let home = Home::new();
    home.write(".claude.json", "{\"mcpServers\": {}, \"x\": \"\\ud800\"}\n");
    let before = home.tree();
    deferred(&home.run(&url, &[&url, "--client", "claude-code", "--dry-run"]));
    assert_eq!(home.tree(), before);
    // A config directory pathlib would print differently.
    let home = Home::new();
    let before = home.tree();
    let mut command = home.command(&url);
    command
        .env("CLAUDE_CONFIG_DIR", format!("{}/claude/", home.0.display()))
        .args(["connect", &url, "--client", "claude-code", "--dry-run"]);
    deferred(&command.output().unwrap());
    assert_eq!(home.tree(), before);
    // Discovery runs before the health probe: nothing was asked of the daemon.
    assert!(health.requests().is_empty());
}

#[test]
fn a_nan_config_is_read_as_python_reads_it() {
    let health = Health::start();
    let url = health.url.clone();
    let home = Home::new();
    home.write(".claude.json", "{\"mcpServers\": {}, \"x\": NaN}\n");
    let output = home.run(&url, &[&url, "--client", "claude-code", "--dry-run"]);
    assert_eq!(output.status.code(), Some(3), "{output:?}");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        String::from_utf8_lossy(&native_text(
            "connect: no registration of the shim was found for claude-code; register a client first (the commands are above), or run the installer.\n"
        ))
    );
}

#[test]
fn a_header_urllib_cannot_send_is_refused_without_a_request() {
    // http.client refuses a header value ending in LF, so the oracle's
    // credential check returns False before anything is sent.
    let health = Health::start();
    let url = health.url.clone();
    let home = Home::new();
    home.write(
        ".gemini/settings.json",
        "{\"mcpServers\": {\"pseudolife-memory\": {\"command\": \"x\", \"env\": {\"PSEUDOLIFE_MCP_TOKEN\": \"abc\\n\"}}}}",
    );
    let settings = home.0.join(".gemini").join("settings.json");
    let before = home.tree();
    let output = home.run(&url, &[&url, "--client", "gemini", "--yes"]);
    assert_eq!(output.status.code(), Some(4), "{output:?}");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        String::from_utf8_lossy(&native_text(&format!(
            "connect: the daemon at {url} refused the literal PSEUDOLIFE_MCP_TOKEN of gemini registration ({}) (an authenticated request that follows no redirects did not succeed). Nothing was written.\n",
            settings.display()
        )))
    );
    assert_eq!(health.requests(), ["/health"]);
    assert_eq!(home.tree(), before);
}

#[test]
fn a_dry_run_never_reads_a_token_or_stdin() {
    // A token file starting with a BOM fails only the real run's check.
    let health = Health::start();
    let url = health.url.clone();
    let home = Home::new();
    home.write(".pseudolife-mcp/bom.token", "\u{feff}token-value");
    home.write(
        ".claude.json",
        "{\"mcpServers\": {\"pseudolife-memory\": {\"command\": \"x\", \"env\": {\"PSEUDOLIFE_MCP_TOKEN_FILE\": \"bom.token\"}}}}",
    );
    let output = home.run(&url, &[&url, "--client", "claude-code", "--dry-run"]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert_eq!(health.requests(), ["/health"]);
}

#[test]
fn a_question_on_a_character_device_defers() {
    // Python's isatty is true for any Windows character device, NUL included,
    // so `connect <url>` there would ask; elsewhere /dev/null is no terminal.
    let home = Home::new();
    let health = Health::start();
    let url = health.url.clone();
    claude_registration(&home, "http://100.64.0.1:8765");
    let before = home.tree();
    let output = home
        .command(&url)
        .args(["connect", &url])
        .stdin(Stdio::null())
        .output()
        .unwrap();
    if cfg!(windows) {
        deferred(&output);
        assert!(health.requests().is_empty());
    } else {
        assert_eq!(output.status.code(), Some(2), "{output:?}");
        assert!(String::from_utf8_lossy(&output.stderr).ends_with(
            "connect: this run is not interactive: re-run with --yes to apply the plan above (nothing was changed)\n"
        ));
    }
    assert_eq!(home.tree(), before);
}

/// The handshake cache the shim child writes during connect's verification:
/// `shim._store_handshake_cache` bytes (`json.dumps({"url": url, **cached})`),
/// captured from the oracle for this exact sequence of stores.
#[test]
fn the_handshake_cache_is_written_as_the_oracle_writes_it() {
    let home = Home::new();
    let cache = pseudolife_stdio::cache::HandshakeCache::new("http://127.0.0.1:8765", &home.0);
    cache.remember_tools(vec![serde_json::json!({
        "name": "t\u{e9}",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": true}
    })]);
    let tools = r#""tools": [{"name": "t\u00e9", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": true}}]"#;
    let read = || String::from_utf8(std::fs::read(cache.path()).unwrap()).unwrap();
    assert_eq!(
        read(),
        format!(r#"{{"url": "http://127.0.0.1:8765", {tools}}}"#)
    );
    cache.remember_instructions(Some("Fixture \u{96ea}\u{1f600}"));
    assert_eq!(
        read(),
        format!(
            r#"{{"url": "http://127.0.0.1:8765", {tools}, "instructions": "Fixture \u96ea\ud83d\ude00"}}"#
        )
    );
    cache.remember_instructions(None);
    assert_eq!(
        read(),
        format!(r#"{{"url": "http://127.0.0.1:8765", "instructions": null, {tools}}}"#)
    );
}
