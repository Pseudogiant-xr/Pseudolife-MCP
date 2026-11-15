//! The daemon's startup security refusals, as a process: off loopback it
//! starts only with a token or the exact `PSEUDOLIFE_MCP_TRUST_BIND`
//! opt-in, and a token map that parses to nothing (reserved names
//! included) refuses even where the bind would be trusted.
//!
//! No case ever opens a listener. Every run sets `PSEUDOLIFE_MCP_PORT=70000`,
//! which `DaemonEnv` accepts as an integer and the listener step refuses
//! (exit 1, "port out of range") after every startup guard has passed. So a
//! refusal is exit 2 with its own message, and an admission is exit 1 with
//! the port message, which a refusal can never print. The environment is
//! cleared, so a token in the shell running the tests cannot turn a refusal
//! case into an admission, and the database address is a closed local port
//! the daemon never reaches before the listener step.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const REFUSE_BIND: &str = "Refusing to bind";
const REFUSE_MAP: &str = "PSEUDOLIFE_MCP_TOKENS is set but no entry parsed";
const ADMITTED: &str = ":70000: port out of range";
const TRUSTED: &str = "without a token because PSEUDOLIFE_MCP_TRUST_BIND is set";

fn scratch(name: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    // Case labels carry quotes and colons, which a Windows path refuses.
    let name: String = name
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    let root = std::env::temp_dir().join(format!(
        "pl-security-startup-{name}-{}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir_all(root.join("data")).unwrap();
    root
}

/// Run the daemon in `home` with only `vars` set beyond a minimal base, and
/// return its exit code and standard error.
fn start(home: &Path, vars: &[(&str, &str)]) -> (i32, String) {
    let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-daemon"));
    command
        .env_clear()
        .current_dir(home)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    // What a process needs to start at all on this OS; nothing of the
    // caller's Pseudolife setup.
    for name in [
        "PATH",
        "SystemRoot",
        "SYSTEMROOT",
        "windir",
        "TEMP",
        "TMP",
        "TMPDIR",
    ] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    for name in [
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ] {
        command.env(name, home);
    }
    command
        .env("PSEUDOLIFE_MCP_DATA_DIR", home.join("data"))
        .env(
            "PSEUDOLIFE_MCP_DATABASE_URL",
            "postgresql://nobody:nothing@127.0.0.1:9/pl_security_startup_none",
        )
        .env("PSEUDOLIFE_RELEASE_CHECK", "0")
        .env("PSEUDOLIFE_MCP_PORT", "70000");
    for (name, value) in vars {
        command.env(name, value);
    }
    #[cfg(windows)]
    {
        // No console window for the daemon whatever the test runner has.
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().expect("spawn the daemon");
    let deadline = Instant::now() + Duration::from_secs(60);
    let status = loop {
        if let Some(status) = child.try_wait().expect("wait for the daemon") {
            break status;
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("the daemon did not exit within 60 s with {vars:?}");
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let mut err = String::new();
    std::io::Read::read_to_string(&mut child.stderr.take().unwrap(), &mut err).unwrap();
    (status.code().unwrap_or(-1), err)
}

fn refused(name: &str, vars: &[(&str, &str)], message: &str) {
    let home = scratch(name);
    let (code, err) = start(&home, vars);
    assert_eq!(
        code, 2,
        "{name}: want a startup refusal (exit 2), got {code}: {err}"
    );
    assert!(err.contains(message), "{name}: want {message:?} in: {err}");
    assert!(
        !err.contains(ADMITTED),
        "{name}: refused after the guards: {err}"
    );
    let _ = std::fs::remove_dir_all(home);
}

fn admitted(name: &str, vars: &[(&str, &str)]) -> String {
    let home = scratch(name);
    let (code, err) = start(&home, vars);
    assert_eq!(
        code, 1,
        "{name}: want every startup guard passed (exit 1 at the port), got {code}: {err}"
    );
    assert!(
        err.contains(ADMITTED),
        "{name}: want {ADMITTED:?} in: {err}"
    );
    let _ = std::fs::remove_dir_all(home);
    err
}

#[test]
fn off_loopback_without_a_token_refuses_to_start() {
    // An empty host binds every interface, so it counts as off loopback.
    for host in ["0.0.0.0", "", "10.0.0.5", "::"] {
        refused(
            &format!("no-token {host:?}"),
            &[("PSEUDOLIFE_MCP_HOST", host)],
            REFUSE_BIND,
        );
    }
    // The same binds start once a credential is configured.
    for (name, value) in [
        ("PSEUDOLIFE_MCP_TOKEN", "tok-startup-fixture"),
        ("PSEUDOLIFE_MCP_TOKENS", "tok-startup-fixture:alice"),
    ] {
        let err = admitted(
            &format!("credential {name}"),
            &[("PSEUDOLIFE_MCP_HOST", "0.0.0.0"), (name, value)],
        );
        assert!(!err.contains(TRUSTED), "{err}");
    }
    // Loopback needs no token.
    for host in ["127.0.0.1", "::1", "localhost"] {
        admitted(
            &format!("loopback {host}"),
            &[("PSEUDOLIFE_MCP_HOST", host)],
        );
    }
}

#[test]
fn only_the_exact_trust_bind_opt_in_starts_off_loopback_without_a_token() {
    for value in ["1", "true", "TRUE", "yes", "On"] {
        let err = admitted(
            &format!("trust {value:?}"),
            &[
                ("PSEUDOLIFE_MCP_HOST", "0.0.0.0"),
                ("PSEUDOLIFE_MCP_TRUST_BIND", value),
            ],
        );
        assert!(err.contains(TRUSTED), "trust {value:?}: {err}");
    }
    // `security-refusals` runs " true " and "0"; the rest are the near
    // misses a looser check would admit.
    for value in [" true ", "0", "", "false", "off", "2", "y", " 1", "true\n"] {
        refused(
            &format!("trust {value:?}"),
            &[
                ("PSEUDOLIFE_MCP_HOST", "0.0.0.0"),
                ("PSEUDOLIFE_MCP_TRUST_BIND", value),
            ],
            REFUSE_BIND,
        );
    }
}

#[test]
fn a_token_map_naming_only_reserved_principals_refuses_to_start() {
    for map in ["x:maintainer", "x:default", "x:Maintainer, y:default"] {
        refused(
            &format!("reserved map {map:?}"),
            &[("PSEUDOLIFE_MCP_TOKENS", map)],
            REFUSE_MAP,
        );
    }
    // The control: the same shape naming an ordinary principal starts.
    admitted("ordinary map", &[("PSEUDOLIFE_MCP_TOKENS", "x:alice")]);
    // A singular token keeps the daemon closed, so it starts with a warning.
    admitted(
        "reserved map with a token",
        &[
            ("PSEUDOLIFE_MCP_TOKENS", "x:maintainer"),
            ("PSEUDOLIFE_MCP_TOKEN", "tok-startup-fixture"),
        ],
    );
}

#[test]
fn an_unparsable_token_map_cannot_open_a_trusted_wildcard_bind() {
    // The map guard runs before the bind guard, so even the trust opt-in
    // does not turn a broken map into an open daemon.
    refused(
        "invalid map trust-open",
        &[
            ("PSEUDOLIFE_MCP_TOKENS", "junk,x:default"),
            ("PSEUDOLIFE_MCP_HOST", "0.0.0.0"),
            ("PSEUDOLIFE_MCP_TRUST_BIND", "1"),
        ],
        REFUSE_MAP,
    );
    refused(
        "invalid map loopback",
        &[("PSEUDOLIFE_MCP_TOKENS", "junk,,nocolon")],
        REFUSE_MAP,
    );
    // The control: one parsable entry is a configured credential.
    admitted(
        "partly valid map off loopback",
        &[
            ("PSEUDOLIFE_MCP_TOKENS", "junk,x:alice"),
            ("PSEUDOLIFE_MCP_HOST", "0.0.0.0"),
        ],
    );
}
