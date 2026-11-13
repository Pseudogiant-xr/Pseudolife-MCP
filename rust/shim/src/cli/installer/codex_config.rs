//! `installer codex-credentials` and `installer codex-runtime-defaults`: the
//! installers' Codex connection steps, written through Codex's own versioned
//! configuration API (`codex app-server`), never by editing `config.toml`
//! text. Each prints one JSON report on stdout; human text goes to stderr.
//!
//! `codex-credentials [--installer-daemon-url URL] [--installer-token-stdin]`
//! moves the Codex registration onto a private, rotatable token file
//! (`setup::codex::configure_credential_file`) and records the hooks'
//! connection file. Report: `status` ("ready" with a credential file,
//! "tokenless" without one, "needs-configuration" on refusal),
//! `credential_file_configured`, `connection_configured`,
//! `credential_file_path`, `daemon_url`, `migrated_literal`, `backup`,
//! `connection_backup`, and `recovery` on refusal. Exit 0, or 1 on refusal.
//!
//! `codex-runtime-defaults` adds `startup_timeout_sec = 240`,
//! `tool_timeout_sec = 240` and `required = true` to the Pseudolife server
//! where Codex has no value for them; an explicit value (false and zero
//! included) is kept. Report: `status` ("ready" or "needs-configuration"),
//! `runtime_defaults` ("configured" or "preserved"), `backup`, `recovery`.
use crate::setup::codex::{self, Client, Edit, Rpc, SERVER, SetupError, WritePlan};
use serde_json::{Map, Value, json};
use std::{
    io::{self, Read, Write},
    path::{Path, PathBuf},
};

pub(super) const CREDENTIALS_USAGE: &str = "usage: pseudolife-mcp installer codex-credentials [--installer-daemon-url URL] [--installer-token-stdin]";
pub(super) const RUNTIME_USAGE: &str = "usage: pseudolife-mcp installer codex-runtime-defaults";

/// The defaults a first install gives the Pseudolife server. Zero, false and
/// any other explicit value are the user's choice and stay.
pub(super) const RUNTIME_DEFAULTS: [(&str, f64); 2] =
    [("startup_timeout_sec", 240.0), ("tool_timeout_sec", 240.0)];

/// The largest token the installer may pipe in (a token file's bound).
const TOKEN_LIMIT: usize = 4096;

/// The Codex home the installers configure: `CODEX_HOME` (with a leading
/// `~` expanded) or `~/.codex`, resolved through links the way Codex and
/// the frozen Python setup spell it, so commands naming files below it
/// stay byte for byte what an earlier setup approved.
pub(super) fn codex_home() -> Result<PathBuf, SetupError> {
    let home = std::env::var_os("CODEX_HOME")
        .filter(|value| !value.is_empty())
        .map(|value| expand_user(Path::new(&value)))
        .unwrap_or_else(|| crate::runtime::registrations::codex_home());
    codex::resolved_path(&home)
}

fn expand_user(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    let user = || dirs::home_dir().unwrap_or_default();
    if text == "~" {
        return user();
    }
    if let Some(rest) = text.strip_prefix("~/").or_else(|| text.strip_prefix("~\\")) {
        return user().join(rest);
    }
    path.to_path_buf()
}

/// A private scratch directory used as the app-server's working directory,
/// so no project configuration layer applies. Removed on drop.
pub(super) struct Neutral(pub PathBuf);
impl Neutral {
    pub fn new(prefix: &str) -> Result<Self, SetupError> {
        let path = std::env::temp_dir().join(format!("{prefix}{}", uuid::Uuid::new_v4().simple()));
        crate::board::state::private_dir(&path).map_err(|_| {
            SetupError("A private temporary directory could not be created for Codex setup.")
        })?;
        Ok(Self(path))
    }
}
impl Drop for Neutral {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

pub(super) fn runtime() -> Option<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()
}

/// Print `report` as one line of JSON; the exit code follows `status`.
pub(super) fn emit_report(report: &Value, ok: bool) -> u8 {
    let written = writeln!(io::stdout().lock(), "{report}").is_ok();
    u8::from(!(written && ok))
}

pub(super) fn usage(message: &str, text: &str) -> u8 {
    let _ = writeln!(io::stderr().lock(), "installer: {message}\n{text}");
    2
}

#[derive(Debug, Default, PartialEq, Eq)]
struct CredentialOptions {
    daemon_url: Option<String>,
    token_stdin: bool,
}

fn parse_credentials(arguments: &[String]) -> Result<Option<CredentialOptions>, String> {
    let mut options = CredentialOptions::default();
    let mut items = arguments.iter();
    while let Some(item) = items.next() {
        match item.as_str() {
            "--help" | "-h" => return Ok(None),
            "--installer-token-stdin" if !options.token_stdin => options.token_stdin = true,
            "--installer-daemon-url" if options.daemon_url.is_none() => {
                let url = items
                    .next()
                    .ok_or("--installer-daemon-url needs a daemon URL")?;
                options.daemon_url = Some(url.clone());
            }
            other => return Err(format!("unknown or repeated option {other:?}")),
        }
    }
    if options.token_stdin && options.daemon_url.is_none() {
        return Err("--installer-token-stdin needs --installer-daemon-url".into());
    }
    Ok(Some(options))
}

/// The installer's token, piped on stdin (never an argument): one line, at
/// most [`TOKEN_LIMIT`] bytes, valid as a bearer credential.
fn read_token(mut input: impl Read) -> Result<String, SetupError> {
    let invalid = SetupError("The installer credential input is invalid.");
    let mut data = Vec::new();
    input
        .by_ref()
        .take(TOKEN_LIMIT as u64 + 1)
        .read_to_end(&mut data)
        .map_err(|_| invalid.clone())?;
    if data.len() > TOKEN_LIMIT {
        return Err(invalid);
    }
    let text = String::from_utf8(data).map_err(|_| invalid.clone())?;
    let token = text.trim_end_matches(['\r', '\n']).to_owned();
    crate::credentials::CredentialProvider::new(Some(token.clone()), None)
        .map_err(|_| invalid.clone())?;
    Ok(token)
}

pub(super) fn run_credentials(arguments: &[String]) -> u8 {
    let options = match parse_credentials(arguments) {
        Ok(Some(options)) => options,
        Ok(None) => {
            let _ = writeln!(io::stdout().lock(), "{CREDENTIALS_USAGE}");
            return 0;
        }
        Err(message) => return usage(&message, CREDENTIALS_USAGE),
    };
    let report = (|| -> Result<Value, SetupError> {
        let token = if options.token_stdin {
            Some(read_token(io::stdin().lock())?)
        } else {
            None
        };
        let home = codex_home()?;
        let neutral = Neutral::new("pseudolife-codex-config-")?;
        let executable = codex::resolve_codex()?;
        let runtime = runtime().ok_or(SetupError("The setup runtime could not start."))?;
        runtime.block_on(async {
            let mut client = Client::open(&executable, &home, &neutral.0).await?;
            let connection = options
                .daemon_url
                .as_deref()
                .map(|url| (url, token.as_deref()));
            let result =
                codex::configure_credential_file(&mut client, &home, &neutral.0, None, connection)
                    .await;
            client.close().await;
            result
        })
    })();
    let report = credentials_report(report);
    let ok = report["status"] != "needs-configuration";
    emit_report(&report, ok)
}

fn credentials_report(result: Result<Value, SetupError>) -> Value {
    match result {
        Ok(mut report) => {
            let ready = report["credential_file_configured"] == true;
            report["status"] = json!(if ready { "ready" } else { "tokenless" });
            report
        }
        Err(error) => json!({"status":"needs-configuration","recovery":error.to_string()}),
    }
}

pub(super) fn run_runtime_defaults(arguments: &[String]) -> u8 {
    match arguments.first().map(String::as_str) {
        None => {}
        Some("--help" | "-h") if arguments.len() == 1 => {
            let _ = writeln!(io::stdout().lock(), "{RUNTIME_USAGE}");
            return 0;
        }
        Some(other) => return usage(&format!("unknown option {other:?}"), RUNTIME_USAGE),
    }
    let report = (|| -> Result<Value, SetupError> {
        let home = codex_home()?;
        let neutral = Neutral::new("pseudolife-codex-config-")?;
        let executable = codex::resolve_codex()?;
        let runtime = runtime().ok_or(SetupError("The setup runtime could not start."))?;
        runtime.block_on(async {
            let mut client = Client::open(&executable, &home, &neutral.0).await?;
            let result = configure_runtime_defaults(&mut client, &home, &neutral.0).await;
            client.close().await;
            result
        })
    })();
    let report = report.unwrap_or_else(
        |error| json!({"status":"needs-configuration","recovery":error.to_string()}),
    );
    let ok = report["status"] == "ready";
    emit_report(&report, ok)
}

/// The defaults Codex has no value for. Codex's typed API reports an omitted
/// optional setting as null; false and zero are explicit choices.
fn missing_defaults(server: &Map<String, Value>) -> Vec<(&'static str, Value)> {
    RUNTIME_DEFAULTS
        .iter()
        .map(|(key, value)| (*key, json!(value)))
        .chain([("required", json!(true))])
        .filter(|(key, _)| server.get(*key).is_none_or(Value::is_null))
        .collect()
}

/// Add the first-run defaults in the user layer, one scoped key each, and
/// verify Codex's effective configuration carries them (rolling the write
/// back if it does not).
pub(super) async fn configure_runtime_defaults(
    client: &mut impl Rpc,
    home: &Path,
    cwd: &Path,
) -> Result<Value, SetupError> {
    let config = client
        .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
        .await?;
    let effective = config["config"]["mcp_servers"][SERVER]
        .as_object()
        .filter(|server| !server.is_empty())
        .ok_or(SetupError(
            "The Pseudolife MCP server is not registered in Codex; runtime defaults were not changed. Register it (rerun the installer), then retry.",
        ))?;
    let missing = missing_defaults(effective);
    if missing.is_empty() {
        return Ok(json!({"status":"ready","runtime_defaults":"preserved","backup":null}));
    }
    let user = codex::user_config(&config, home).map_err(|_| {
        SetupError(
            "Codex did not expose a versioned user configuration; runtime defaults were not changed.",
        )
    })?;
    // Writing a key without the server's own table would leave Codex a
    // server with no command.
    if !user.config["mcp_servers"][SERVER].is_object() {
        return Err(SetupError(
            "The Pseudolife registration is not in Codex's user configuration (another layer sets it); runtime defaults were not changed. Set startup_timeout_sec, tool_timeout_sec and required where that layer registers the server.",
        ));
    }
    let edits = missing
        .into_iter()
        .map(|(key, value)| Edit::new(&["mcp_servers", SERVER, key], value))
        .collect();
    let plan = WritePlan::new(&config, home, edits)?;
    let applied = plan.apply(client, cwd).await.map_err(|error| {
        SetupError::detail(format!(
            "{error} Runtime defaults were not confirmed; rerun the installer, or set startup_timeout_sec = 240, tool_timeout_sec = 240 and required = true under [mcp_servers.{SERVER}] in Codex config.toml."
        ))
    })?;
    Ok(json!({"status":"ready","runtime_defaults":"configured","backup":applied.backup}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn credential_options_need_a_url_with_stdin_and_refuse_repeats() {
        assert_eq!(
            parse_credentials(&strings(&[
                "--installer-daemon-url",
                "http://127.0.0.1:8765",
                "--installer-token-stdin"
            ])),
            Ok(Some(CredentialOptions {
                daemon_url: Some("http://127.0.0.1:8765".into()),
                token_stdin: true
            }))
        );
        assert!(parse_credentials(&strings(&["--installer-token-stdin"])).is_err());
        assert!(parse_credentials(&strings(&["--installer-daemon-url"])).is_err());
        assert!(
            parse_credentials(&strings(&[
                "--installer-daemon-url",
                "a",
                "--installer-daemon-url",
                "b"
            ]))
            .is_err()
        );
        assert_eq!(parse_credentials(&strings(&["--help"])), Ok(None));
        assert_eq!(
            parse_credentials(&[]),
            Ok(Some(CredentialOptions::default()))
        );
    }

    #[test]
    fn installer_token_is_one_bounded_valid_line() {
        assert_eq!(read_token(&b"fixture-token\r\n"[..]).unwrap(), "fixture-token");
        assert_eq!(read_token(&b"fixture-token"[..]).unwrap(), "fixture-token");
        assert!(read_token(&[b'a'; TOKEN_LIMIT + 1][..]).is_err());
        assert!(read_token(&b""[..]).is_err());
        assert!(read_token(&b"\xff\xfe"[..]).is_err());
        assert!(read_token(&b"two\nlines"[..]).is_err());
    }

    #[test]
    fn credential_report_names_ready_tokenless_and_refusal() {
        let ready = credentials_report(Ok(json!({"credential_file_configured":true})));
        assert_eq!(ready["status"], "ready");
        let tokenless = credentials_report(Ok(json!({"credential_file_configured":false})));
        assert_eq!(tokenless["status"], "tokenless");
        let refused = credentials_report(Err(SetupError("fixture refusal")));
        assert_eq!(
            refused,
            json!({"status":"needs-configuration","recovery":"fixture refusal"})
        );
    }

    use crate::cli::installer::fake_codex::FakeCodex;

    pub(crate) struct TempHome(pub PathBuf);
    impl TempHome {
        pub fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("w7-codex-home-{}", uuid::Uuid::new_v4().simple()));
            std::fs::create_dir_all(&path).unwrap();
            let path = codex::resolved_path(&path).unwrap();
            std::fs::write(path.join("config.toml"), "# fixture\n").unwrap();
            Self(path)
        }
    }
    impl Drop for TempHome {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn registered(extra: Value) -> Value {
        let mut server = json!({"command":"/opt/pseudolife-mcp","args":[],"env":{}});
        server.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        json!({"mcp_servers":{SERVER: server}})
    }

    #[tokio::test]
    async fn runtime_defaults_fill_only_missing_keys_and_keep_explicit_ones() {
        let home = TempHome::new();
        let mut codex = FakeCodex::new(&home.0, registered(json!({"tool_timeout_sec":0})));
        let report = configure_runtime_defaults(&mut codex, &home.0, &home.0)
            .await
            .unwrap();
        assert_eq!(report["status"], "ready");
        assert_eq!(report["runtime_defaults"], "configured");
        let server = &codex.user["mcp_servers"][SERVER];
        assert_eq!(server["startup_timeout_sec"], json!(240.0));
        assert_eq!(server["tool_timeout_sec"], json!(0), "explicit zero kept");
        assert_eq!(server["required"], json!(true));
        assert_eq!(server["command"], "/opt/pseudolife-mcp");
        // Each key is its own scoped edit: the server table is never replaced.
        let edits = &codex.writes()[0]["edits"];
        assert_eq!(edits.as_array().unwrap().len(), 2);
        let backup = PathBuf::from(report["backup"].as_str().unwrap());
        assert_eq!(std::fs::read(&backup).unwrap(), b"# fixture\n");
        // A rerun finds nothing to do and writes nothing.
        let again = configure_runtime_defaults(&mut codex, &home.0, &home.0)
            .await
            .unwrap();
        assert_eq!(again["runtime_defaults"], "preserved");
        assert_eq!(again["backup"], Value::Null);
        assert_eq!(codex.writes().len(), 1);
    }

    #[tokio::test]
    async fn runtime_defaults_refuse_an_unregistered_or_foreign_layer_server() {
        let home = TempHome::new();
        let mut codex = FakeCodex::new(&home.0, json!({}));
        let error = configure_runtime_defaults(&mut codex, &home.0, &home.0)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("not registered"), "{error}");
        let mut codex = FakeCodex::new(&home.0, json!({}));
        codex.overlay = registered(json!({}));
        let error = configure_runtime_defaults(&mut codex, &home.0, &home.0)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("another layer"), "{error}");
        assert!(codex.writes().is_empty());
    }

    #[tokio::test]
    async fn runtime_defaults_shadowed_by_another_layer_are_rolled_back_and_refused() {
        let home = TempHome::new();
        let mut codex = FakeCodex::new(&home.0, registered(json!({})));
        codex.shadow = Some((
            vec!["mcp_servers".into(), SERVER.into(), "required".into()],
            Value::Null,
        ));
        let error = configure_runtime_defaults(&mut codex, &home.0, &home.0)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("were not confirmed"), "{error}");
        // The write was undone: the user layer has no defaults left.
        let server = &codex.user["mcp_servers"][SERVER];
        for key in ["startup_timeout_sec", "tool_timeout_sec", "required"] {
            assert!(server[key].is_null(), "{key} left behind: {server}");
        }
    }

    #[test]
    fn only_absent_or_null_defaults_are_added() {
        let server = json!({"command":"x","startup_timeout_sec":null,"tool_timeout_sec":0,"required":false});
        let missing = missing_defaults(server.as_object().unwrap());
        assert_eq!(missing, vec![("startup_timeout_sec", json!(240.0))]);
        let all = missing_defaults(json!({"command":"x"}).as_object().unwrap());
        assert_eq!(
            all.iter().map(|(k, _)| *k).collect::<Vec<_>>(),
            ["startup_timeout_sec", "tool_timeout_sec", "required"]
        );
    }
}
