//! `installer codex-hooks`: install, approve and verify only Pseudolife's
//! Codex lifecycle hooks (the plugin's, or manual copies run through a
//! stable launcher), with Codex's own configuration API and the trust
//! hashes Codex computes. Replaces `ops/setup-codex-hooks.py`.
//!
//! `codex-hooks --checkout DIR [--source auto|manual|plugin|skip]
//! [--trust ask|yes|no] [--instructions auto|append|skip] [--non-interactive]`
//!
//! `--checkout` is this repository's root: the manual bundle copies its
//! `plugin/hooks` scripts, the installed plugin is checked against them, and
//! the standing-instructions fallback is its `examples/CLAUDE.memory.md`.
//! The daemon the check talks to is `PSEUDOLIFE_MCP_DAEMON_URL` with the
//! bearer in `PSEUDOLIFE_MCP_TOKEN_FILE` (as Codex's registration names
//! them after the credential step).
//!
//! One JSON report on stdout. Fields the installers read: `status` ("ready",
//! "pending", "unavailable", "skipped"), `source` ("manual", "plugin",
//! "skip"), `instructions` ("present", "appended", "skipped",
//! "covered-by-hooks") and `recovery` (one line, or null). Also `backups`,
//! the credential fields of `codex-credentials`, `verified`,
//! `mailbox_approval` ("set", "already", "kept-explicit", "declined",
//! "unavailable"), `mailbox_approval_detail`, `mailbox_approval_notice` and
//! `instructions_path`. Exit 0 when ready or the instructions are present or
//! appended, 1 otherwise (still a valid report), 2 for a usage error.
mod definitions;
mod instructions;
mod manual;
mod plugin;
mod trust;
mod verify;

use crate::setup::codex::{Rpc, SetupError};
use serde_json::{Map, Value, json};
use std::{
    fmt,
    io::{self, BufRead, IsTerminal, Write},
    path::{Path, PathBuf},
};
pub use verify::{Codex, Daemon, Session};

pub(super) const USAGE: &str = "usage: pseudolife-mcp installer codex-hooks --checkout DIR [--source auto|manual|plugin|skip] [--trust ask|yes|no] [--instructions auto|append|skip] [--non-interactive]";

const RECOVERY: &str =
    "Open Codex /hooks to review PseudoLife hooks; rerun the installer after correcting the reported problem.";

/// A refusal: fixed text and locally chosen paths only, never a credential
/// or an untrusted response body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fail(String);
impl Fail {
    pub fn new(message: &str) -> Self {
        Self(message.to_owned())
    }
    pub fn detail(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}
impl fmt::Display for Fail {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl From<SetupError> for Fail {
    fn from(error: SetupError) -> Self {
        Self(error.to_string())
    }
}

/// One handler as Codex's `hooks/list` reports it.
#[derive(Debug, Clone, PartialEq)]
pub struct Hook {
    pub key: String,
    pub event: String,
    pub enabled: bool,
    pub trust_status: String,
    pub source_path: String,
    pub source: String,
    pub plugin_id: String,
    pub command: String,
    pub current_hash: String,
    pub is_managed: bool,
    pub handler_type: String,
}

impl Hook {
    fn parse(value: &Value) -> Option<Self> {
        for key in ["key", "currentHash", "eventName", "enabled", "trustStatus", "sourcePath"] {
            value.get(key)?;
        }
        let text = |key: &str| value[key].as_str().unwrap_or_default().to_owned();
        Some(Self {
            key: text("key"),
            event: text("eventName"),
            enabled: value["enabled"].as_bool()?,
            trust_status: text("trustStatus"),
            source_path: text("sourcePath"),
            source: text("source"),
            plugin_id: text("pluginId"),
            command: text("command"),
            current_hash: text("currentHash"),
            is_managed: value["isManaged"].as_bool().unwrap_or(false),
            handler_type: text("handlerType"),
        })
    }
}

#[derive(Debug, Default)]
pub struct Report {
    pub source: String,
    pub status: String,
    pub instructions: String,
    pub recovery: Option<String>,
    pub backups: Vec<PathBuf>,
    pub credential: Map<String, Value>,
    pub verified: Option<Value>,
    pub mailbox_approval: Option<String>,
    pub mailbox_approval_detail: Option<String>,
    pub mailbox_approval_notice: Option<String>,
    pub instructions_path: Option<PathBuf>,
}

impl Report {
    pub fn json(&self) -> Value {
        let mut out = Map::new();
        out.insert("source".into(), json!(self.source));
        out.insert("status".into(), json!(self.status));
        out.insert("instructions".into(), json!(self.instructions));
        out.insert("recovery".into(), json!(self.recovery));
        out.insert("backups".into(), json!(self.backups));
        for (key, value) in &self.credential {
            out.insert(key.clone(), value.clone());
        }
        for (key, value) in [
            ("verified", json!(self.verified)),
            ("mailbox_approval", json!(self.mailbox_approval)),
            ("mailbox_approval_detail", json!(self.mailbox_approval_detail)),
            ("mailbox_approval_notice", json!(self.mailbox_approval_notice)),
            ("instructions_path", json!(self.instructions_path)),
        ] {
            if !value.is_null() {
                out.insert(key.into(), value);
            }
        }
        Value::Object(out)
    }
    /// The process exit code the installers expect.
    pub fn exit(&self) -> u8 {
        u8::from(!(self.status == "ready" || matches!(self.instructions.as_str(), "present" | "appended")))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Options {
    pub source: String,
    pub trust: String,
    pub instructions: String,
    pub non_interactive: bool,
    pub checkout: PathBuf,
}

fn parse(arguments: &[String]) -> Result<Option<Options>, String> {
    let mut source = None;
    let mut trust = None;
    let mut instructions = None;
    let mut checkout = None;
    let mut non_interactive = false;
    let mut items = arguments.iter();
    while let Some(item) = items.next() {
        let (slot, allowed): (&mut Option<String>, &[&str]) = match item.as_str() {
            "--help" | "-h" => return Ok(None),
            "--non-interactive" if !non_interactive => {
                non_interactive = true;
                continue;
            }
            "--checkout" if checkout.is_none() => (&mut checkout, &[]),
            "--source" if source.is_none() => (&mut source, &["auto", "manual", "plugin", "skip"]),
            "--trust" if trust.is_none() => (&mut trust, &["ask", "yes", "no"]),
            "--instructions" if instructions.is_none() => {
                (&mut instructions, &["auto", "append", "skip"])
            }
            other => return Err(format!("unknown or repeated option {other:?}")),
        };
        let value = items.next().ok_or_else(|| format!("{item} needs a value"))?;
        if !allowed.is_empty() && !allowed.contains(&value.as_str()) {
            return Err(format!("{item} must be one of {}", allowed.join(", ")));
        }
        *slot = Some(value.clone());
    }
    Ok(Some(Options {
        source: source.unwrap_or_else(|| "auto".into()),
        trust: trust.unwrap_or_else(|| "ask".into()),
        instructions: instructions.unwrap_or_else(|| "auto".into()),
        non_interactive,
        checkout: PathBuf::from(checkout.ok_or("--checkout (this repository's root) is required")?),
    }))
}

/// `(approved, fallback_allowed)`, asking only on a terminal; the menu may
/// turn the run into "instructions only" or "skip both".
fn consent(options: &mut Options, terminal: bool, input: &mut dyn BufRead, prompt: &mut dyn Write) -> (bool, bool) {
    if options.source == "skip" {
        return (false, options.instructions == "append");
    }
    if options.trust == "yes" {
        return (true, true);
    }
    if options.trust == "no" || options.non_interactive || !terminal {
        return (false, options.instructions == "append");
    }
    let mut answer = String::new();
    if options.instructions != "auto" {
        let _ = write!(
            prompt,
            "Approve PseudoLife's hooks (briefing, reminders, cleanup, agent-board check-in, new-mail hint) to run outside the sandbox, including the scripts later PseudoLife updates install, and its memory_message tool (board mail: receive, ack, send) so a thread woken by mail does not stall on an approval prompt? [y/N] "
        );
        let _ = prompt.flush();
        let _ = input.read_line(&mut answer);
        let approved = matches!(answer.trim().to_lowercase().as_str(), "y" | "yes");
        return (approved, options.instructions == "append");
    }
    let _ = write!(
        prompt,
        "PseudoLife memory setup:\n  1. Enable automatic briefings, reminders, and session cleanup (recommended).\n     Where the agent board is on, also an agent-board check-in at session start\n     and a new-mail hint when a peer's message is waiting.\n     Approves only PseudoLife's hooks to run outside the sandbox, including the\n     scripts later PseudoLife updates install, and its memory_message tool\n     (board mail: receive, ack, send) so a thread woken by mail does not stall\n     on an approval prompt; adds standing memory instructions if verification fails.\n  2. Standing memory instructions only.\n  3. Skip both.\nChoose [1/2/3, default 1]: "
    );
    let _ = prompt.flush();
    if input.read_line(&mut answer).unwrap_or(0) == 0 {
        // End of input is not approval.
        return (false, false);
    }
    match answer.trim() {
        "" | "1" => (true, true),
        "2" => {
            options.source = "skip".into();
            (false, true)
        }
        _ => {
            options.source = "skip".into();
            options.instructions = "skip".into();
            (false, false)
        }
    }
}

pub async fn inventory(client: &mut impl Rpc, cwd: &Path) -> Result<(Value, Vec<Hook>), Fail> {
    let config = client
        .rpc("config/read", json!({"includeLayers": true, "cwd": cwd}))
        .await?;
    let result = client.rpc("hooks/list", json!({"cwds": [cwd]})).await?;
    let entries = result["data"].as_array().cloned().unwrap_or_default();
    if entries.len() != 1 || entries[0]["errors"].as_array().is_some_and(|e| !e.is_empty()) {
        return Err(Fail::new("Codex could not load its hook configuration."));
    }
    let unsupported = || Fail::new("This Codex hook-list schema is unsupported; use Codex /hooks.");
    let hooks = entries[0]["hooks"]
        .as_array()
        .ok_or_else(unsupported)?
        .iter()
        .map(|h| Hook::parse(h).ok_or_else(unsupported))
        .collect::<Result<Vec<_>, _>>()?;
    Ok((config, hooks))
}

/// What the setup reads besides its options: Codex, the daemon and the
/// environment, injected so tests never reach the real ones.
#[allow(async_fn_in_trait)]
pub trait Host {
    type Codex: Codex;
    type Daemon: Daemon;
    fn codex(&self, home: &Path, cwd: &Path) -> Result<Self::Codex, Fail>;
    fn daemon(&self, url: String, token: Option<String>) -> Self::Daemon;
    fn var(&self, key: &str) -> Option<String>;
}

const FILE_KEY: &str = "PSEUDOLIFE_MCP_TOKEN_FILE";
const TOKEN_KEY: &str = "PSEUDOLIFE_MCP_TOKEN";
const URL_KEY: &str = "PSEUDOLIFE_MCP_DAEMON_URL";

/// The connection the check uses: the credential step's token file and
/// URL over this process's environment (the hooks Codex starts inherit the
/// same), and the bearer read from that file.
fn connection(host: &impl Host, report: &Report) -> Result<(Vec<(String, Option<String>)>, String, Option<String>), Fail> {
    let mut environment = Vec::new();
    let mut file = host.var(FILE_KEY).filter(|v| !v.is_empty());
    let mut literal = host.var(TOKEN_KEY).filter(|v| !v.is_empty());
    if let Some(path) = report.credential.get("credential_file_path").and_then(Value::as_str) {
        file = (!path.is_empty()).then(|| path.to_owned());
        environment.push((FILE_KEY.to_owned(), file.clone()));
        environment.push((TOKEN_KEY.to_owned(), None));
        literal = None;
    }
    let mut url = host.var(URL_KEY).filter(|v| !v.is_empty());
    if let Some(chosen) = report.credential.get("daemon_url").and_then(Value::as_str).filter(|v| !v.is_empty()) {
        url = Some(chosen.to_owned());
        environment.push((URL_KEY.to_owned(), url.clone()));
    }
    let url = url.unwrap_or_else(|| crate::daemon_url::DEFAULT_URL.to_owned());
    let token = match file {
        Some(path) => crate::credentials::CredentialProvider::new(None, Some(PathBuf::from(&path)))
            .and_then(|p| p.snapshot())
            .map_err(|_| {
                Fail::detail(format!(
                    "The configured credential file {path} is missing, unsafe, or malformed; repair it (rerun the installer), then retry."
                ))
            })?
            .token()
            .map(str::to_owned),
        None => literal,
    };
    Ok((environment, url, token))
}

fn hooks_disabled(config: &Value) -> bool {
    config["config"]["features"]["hooks"] == false
}

pub async fn setup<H: Host>(options: &Options, home: &Path, host: &H, approved: bool, fallback_allowed: bool) -> Report {
    let mut report = Report {
        source: if options.source == "auto" { "skip".into() } else { options.source.clone() },
        status: "pending".into(),
        instructions: "skipped".into(),
        ..Report::default()
    };
    if options.source == "skip" {
        report.status = "skipped".into();
    } else if let Err(error) = hook_setup(options, home, host, approved, &mut report).await {
        report.status = "unavailable".into();
        report.recovery = Some(error.to_string());
    }
    match instructions::standing(
        home,
        &options.checkout,
        &options.instructions,
        fallback_allowed,
        report.status == "ready",
        &mut report,
    ) {
        Ok(state) => report.instructions = state.into(),
        Err(error) => {
            report.instructions = "skipped".into();
            let lead = report.recovery.take().map(|r| format!("{r} ")).unwrap_or_default();
            report.recovery = Some(format!(
                "{lead}Standing instructions could not be written: {error}"
            ));
        }
    }
    report
}

async fn hook_setup<H: Host>(options: &Options, home: &Path, host: &H, approved: bool, report: &mut Report) -> Result<(), Fail> {
    let legacy = definitions::legacy_commands(&options.checkout).map_err(Fail::detail)?;
    let scripts = options.checkout.join("plugin").join("hooks");
    let neutral = super::codex_config::Neutral::new("pseudolife-hook-check-")?;
    let cwd = neutral.0.clone();
    let codex = host.codex(home, &cwd)?;
    let (config, hooks) = {
        let mut client = codex.open(&json!({}), &[]).await?;
        let outcome = async {
            let (config, hooks) = inventory(&mut client, &cwd).await?;
            let credential =
                crate::setup::codex::configure_credential_file(&mut client, home, &cwd, Some(config.clone()), None).await?;
            for key in ["backup", "connection_backup"] {
                if let Some(path) = credential[key].as_str() {
                    report.backups.push(PathBuf::from(path));
                }
            }
            for (key, value) in credential.as_object().into_iter().flatten() {
                if key != "backup" && key != "connection_backup" {
                    report.credential.insert(key.clone(), value.clone());
                }
            }
            if credential["credential_file_configured"] == true {
                inventory(&mut client, &cwd).await
            } else {
                Ok((config, hooks))
            }
        }
        .await;
        client.close().await;
        outcome?
    };
    if hooks_disabled(&config) {
        return Err(Fail::new(
            "Codex hooks are disabled in configuration (features.hooks = false); setup preserves that choice.",
        ));
    }
    if options.source == "auto" && config["config"]["plugins"][definitions::PLUGIN_ID]["enabled"] == false {
        return Err(Fail::new(
            "The PseudoLife plugin is disabled in Codex. Re-enable it if desired, or choose manual hooks (--codex-hooks manual; install.ps1: -CodexHooks manual).",
        ));
    }
    let (source, selected, obsolete) = manual::select(&hooks, home, &options.source, &legacy)?;
    let mut selected: Vec<Hook> = selected.into_iter().cloned().collect();
    let obsolete_any = !obsolete.is_empty();
    report.source = source.clone();
    let vet = |selected: &[&Hook], complete: bool| -> Result<(), Fail> {
        if source == "plugin" {
            plugin::vet(selected, &options.checkout)
        } else if !selected.is_empty() || complete {
            manual::vet(selected, home, complete)
        } else {
            Ok(())
        }
    };
    {
        let selected: Vec<&Hook> = selected.iter().collect();
        if source == "plugin" || !selected.is_empty() {
            vet(&selected, !approved)?;
        }
    }
    if approved {
        if source == "manual" || obsolete_any {
            manual::install(home, report, source == "plugin", &scripts, &legacy)?;
        }
        let mut client = codex.open(&json!({}), &[]).await?;
        let outcome = async {
            let (fresh, listed) = inventory(&mut client, &cwd).await?;
            let (_, mine, _) = manual::select(&listed, home, &source, &legacy)?;
            if !manual::complete_set(&mine, &source) {
                return Err(Fail::new("Codex did not discover the installed PseudoLife hooks."));
            }
            vet(&mine, true)?;
            trust::trust_hooks(&mut client, &fresh, &mine, home, &cwd, report).await?;
            trust::mailbox_approval(&fresh, home, report, Some((&mut client, cwd.as_path()))).await;
            Ok::<_, Fail>((fresh.clone(), listed.clone(), mine.into_iter().cloned().collect::<Vec<_>>()))
        }
        .await;
        client.close().await;
        let (_, _, mine) = outcome?;
        selected = mine;
    } else {
        trust::mailbox_approval::<<H::Codex as Codex>::Session>(&config, home, report, None).await;
    }
    if obsolete_any && !approved {
        return Err(Fail::new(
            "Duplicate PseudoLife hooks need migration. Rerun the installer and approve the Codex hooks, or remove the duplicates in Codex /hooks.",
        ));
    }
    let chosen: Vec<&Hook> = selected.iter().collect();
    if !manual::complete_set(&chosen, &source) {
        report.recovery = Some(
            "Hooks are not installed. Rerun the installer interactively or with --codex-hook-trust yes (install.ps1: -CodexHookTrust yes); --instructions append adds the standing-instructions fallback."
                .into(),
        );
        return Ok(());
    }
    let (config, hooks, selected) = {
        let mut client = codex.open(&json!({}), &[]).await?;
        let outcome = async {
            let (config, listed) = inventory(&mut client, &cwd).await?;
            let mine: Vec<Hook> = manual::select(&listed, home, &source, &legacy)?.1.into_iter().cloned().collect();
            Ok::<_, Fail>((config, listed, mine))
        }
        .await;
        client.close().await;
        outcome?
    };
    let chosen: Vec<&Hook> = selected.iter().collect();
    vet(&chosen, true)?;
    if chosen.iter().any(|h| h.trust_status != "trusted") {
        report.recovery = Some(
            "PseudoLife hooks await approval. Rerun the installer interactively, or review them in Codex /hooks.".into(),
        );
        return Ok(());
    }
    let (environment, url, token) = connection(host, report)?;
    let daemon = host.daemon(url, token);
    let digest_dir = verify::digest_dir(&|key| {
        environment
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.clone())
            .unwrap_or_else(|| host.var(key))
    });
    report.verified = Some(
        verify::verify(
            &codex,
            &daemon,
            verify::Verification {
                cwd: &cwd,
                effective: &config,
                all: &hooks,
                selected: &chosen,
                environment: &environment,
                digest_dir,
                coordination_setting: host.var("PSEUDOLIFE_AGENT_COORDINATION"),
            },
        )
        .await?,
    );
    report.status = "ready".into();
    report.mailbox_approval_notice = Some(trust::mailbox_notice(
        report.mailbox_approval.as_deref().unwrap_or("declined"),
        report.mailbox_approval_detail.as_deref(),
    ));
    Ok(())
}

// -- the real host -------------------------------------------------------------

struct RealHost;

impl Host for RealHost {
    type Codex = verify::RealCodex;
    type Daemon = verify::RealDaemon;
    fn codex(&self, home: &Path, cwd: &Path) -> Result<Self::Codex, Fail> {
        Ok(verify::RealCodex {
            executable: crate::setup::codex::resolve_codex()?,
            home: home.to_owned(),
            cwd: cwd.to_owned(),
        })
    }
    fn daemon(&self, url: String, token: Option<String>) -> Self::Daemon {
        verify::RealDaemon { url, token }
    }
    fn var(&self, key: &str) -> Option<String> {
        std::env::var(key).ok()
    }
}

pub(super) fn run(arguments: &[String]) -> u8 {
    let mut options = match parse(arguments) {
        Ok(Some(options)) => options,
        Ok(None) => {
            let _ = writeln!(io::stdout().lock(), "{USAGE}");
            return 0;
        }
        Err(message) => return super::codex_config::usage(&message, USAGE),
    };
    let terminal = io::stdin().is_terminal();
    let (approved, fallback_allowed) =
        consent(&mut options, terminal, &mut io::stdin().lock(), &mut io::stderr().lock());
    let report = match (super::codex_config::codex_home(), super::codex_config::runtime()) {
        (Ok(home), Some(runtime)) => runtime.block_on(setup(&options, &home, &RealHost, approved, fallback_allowed)),
        (Err(error), _) => Report {
            source: if options.source == "auto" { "skip".into() } else { options.source.clone() },
            status: "unavailable".into(),
            instructions: "skipped".into(),
            recovery: Some(format!("{error} {RECOVERY}")),
            ..Report::default()
        },
        (_, None) => Report {
            source: "skip".into(),
            status: "unavailable".into(),
            instructions: "skipped".into(),
            recovery: Some(format!("The setup runtime could not start. {RECOVERY}")),
            ..Report::default()
        },
    };
    if writeln!(io::stdout().lock(), "{}", report.json()).is_err() {
        return 1;
    }
    if report.status == "ready"
        && let Some(notice) = &report.mailbox_approval_notice
    {
        let _ = writeln!(io::stderr().lock(), "{notice}");
    }
    report.exit()
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
