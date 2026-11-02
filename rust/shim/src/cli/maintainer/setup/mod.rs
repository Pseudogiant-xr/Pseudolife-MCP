//! Native guided host setup and private in-image helpers for maintainer passkeys.
macro_rules! println {
    ($($argument:tt)*) => { crate::cli::maintainer::setup::line(format_args!($($argument)*)) };
}
mod config;
mod system;
mod tailnet;

use crate::pg;
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    io::{self, IsTerminal, Write},
    sync::{Arc, Mutex},
};
use system::Tier;
use tokio::io::AsyncBufReadExt;

const SETUP: &str = "pseudolife-mcp maintainer setup";
static OUTPUT_FAILED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
fn line(arguments: std::fmt::Arguments<'_>) {
    if super::print(&format!("{arguments}\n")).is_err() {
        OUTPUT_FAILED.store(true, std::sync::atomic::Ordering::Relaxed);
    }
}
#[derive(Debug)]
struct Error {
    message: String,
    code: u8,
}
impl Error {
    fn new(message: impl Into<String>, code: u8) -> Self {
        Self {
            message: message.into(),
            code,
        }
    }
    fn refused(message: impl Into<String>) -> Self {
        Self::new(message, 4)
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
type Result<T> = std::result::Result<T, Error>;
#[derive(Default)]
struct Args {
    local: bool,
    port: u16,
    https_port: u16,
    yes: bool,
    check: bool,
}
fn parse(values: &[String]) -> Result<Args> {
    let mut args = Args {
        port: 8765,
        https_port: 8443,
        ..Default::default()
    };
    let mut values = values.iter();
    while let Some(value) = values.next() {
        let (flag, inline) = value
            .split_once('=')
            .map_or((value.as_str(), None), |(a, b)| (a, Some(b)));
        match (flag, inline) {
            ("--local", None) => args.local = true,
            ("--yes", None) => args.yes = true,
            ("--check", None) => args.check = true,
            ("--port" | "--https-port", _) => {
                let value = inline
                    .or_else(|| values.next().map(String::as_str))
                    .ok_or_else(|| {
                        Error::new(format!("argument {flag}: expected one argument"), 2)
                    })?;
                let port = value
                    .parse::<u16>()
                    .ok()
                    .filter(|v| *v > 0)
                    .ok_or_else(|| {
                        Error::new(
                            format!("argument {flag}: port must be a whole number from 1 to 65535"),
                            2,
                        )
                    })?;
                if flag == "--port" {
                    args.port = port;
                } else {
                    args.https_port = port;
                }
            }
            _ => return Err(Error::new(format!("unrecognized arguments: {value}"), 2)),
        }
    }
    Ok(args)
}
fn interactive() -> bool {
    io::stdin().is_terminal() && io::stdout().is_terminal()
}
async fn ask(question: String) -> String {
    tokio::task::spawn_blocking(move || {
        print!("{question}");
        let _ = io::stdout().flush();
        let mut answer = String::new();
        let _ = io::stdin().read_line(&mut answer);
        answer.trim().to_lowercase()
    })
    .await
    .unwrap_or_default()
}
async fn consent(args: &Args) -> Result<()> {
    if args.yes {
        return Ok(());
    }
    if !interactive() {
        return Err(Error::new(
            "no terminal to confirm on: run this in a terminal, or rerun with --yes",
            2,
        ));
    }
    if !matches!(
        ask("make these changes? [y/N] ".into()).await.as_str(),
        "y" | "yes"
    ) {
        return Err(Error::new("declined; nothing changed", 2));
    }
    Ok(())
}

/// Called by the parent maintainer dispatcher with the arguments after `setup`.
/// `--host-state` and `--host-write` are the environment boundary used by
/// `docker exec ... pseudolife-mcp maintainer setup ...`.
pub fn run(values: Vec<OsString>) -> u8 {
    std::thread::spawn(move || run_inner(values))
        .join()
        .unwrap_or(4)
}
fn run_inner(values: Vec<OsString>) -> u8 {
    let Some(values) = values
        .into_iter()
        .map(|v| v.into_string().ok())
        .collect::<Option<Vec<_>>>()
    else {
        return 2;
    };
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        return 4;
    };
    let pending: Pending = Arc::new(Mutex::new(None));
    let result=runtime.block_on(async {
        tokio::select! {
            result=entry(&values,Arc::clone(&pending))=>result,
            _=tokio::signal::ctrl_c()=>{
                let pending=pending.lock().unwrap_or_else(std::sync::PoisonError::into_inner).clone();
                if let Some((tier,prefix,fresh))=pending {
                    if fresh {
                        let command=system::command(&tier,&["maintainer","revoke","--",&prefix])?;
                        let revoked=system::run(&command,120).await.code==0;
                        crate::stderrln!("\nmaintainer setup: passkey {prefix} {}",if revoked {"revoked".into()} else {format!("NOT revoked: run `pseudolife-mcp maintainer revoke {prefix}`")});
                    } else {crate::stderrln!("\nmaintainer setup: passkey {prefix} is still pending");}
                }
                Err(Error::new(format!("interrupted. What was done stays done; run `{SETUP}` again to pick up from there"),130))
            }
        }
    });
    // An interrupted prompt's blocking stdin reader must not hold process exit.
    runtime.shutdown_background();
    if OUTPUT_FAILED.load(std::sync::atomic::Ordering::Relaxed) {
        return 120;
    }
    match result {
        Ok(code) => code,
        Err(error) => {
            if values.first().is_some_and(|v| v.starts_with("--host-")) {
                crate::stderrln!("refused: {error}");
            } else {
                crate::stderrln!("maintainer setup: {error}");
            }
            error.code
        }
    }
}
type Pending = Arc<Mutex<Option<(Tier, String, bool)>>>;

async fn entry(values: &[String], pending: Pending) -> Result<u8> {
    if values.first().is_some_and(|v| v.starts_with("--host-")) {
        let refs: Vec<_> = values.iter().map(String::as_str).collect();
        let value = host(&refs).await?;
        println!("{}", value);
        return Ok(0);
    }
    if values.iter().any(|v| matches!(v.as_str(), "-h" | "--help")) {
        return Ok(
            if super::print(&include_str!("help.txt").replace("\r\n", "\n")).is_ok() {
                0
            } else {
                120
            },
        );
    }
    let args = parse(values)?;
    if args.check {
        let state = match system::tier().await {
            Ok(tier) => system::host(&tier, &["--host-state"]).await.ok(),
            Err(_) => None,
        };
        let (code, text) = match state {
            None => (2, "could not read the daemon's state"),
            Some(state) if !state["problem"].is_null() => (1, "not set up"),
            Some(state) if !state["keys"].is_array() => (2, "could not read the daemon's state"),
            Some(state)
                if state["keys"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|v| v["state"] == "active") =>
            {
                (0, "in place")
            }
            _ => (1, "not set up"),
        };
        println!("maintainer passkeys: {text}");
        return Ok(code);
    }
    setup(args, pending).await
}

async fn host(args: &[&str]) -> Result<Value> {
    match args {
        ["--host-state"] => {
            let path = config::path(false)?;
            let data = config::read(&path, false)?;
            let section = &data["coordination"]["maintainer"];
            let rp = section.get("rp_id").unwrap_or(&Value::Null);
            let origin = section.get("origin").unwrap_or(&Value::Null);
            let rp = if rp.is_null() && section.get("rp_id").is_none() {
                json!("")
            } else {
                rp.clone()
            };
            let origin = if origin.is_null() && section.get("origin").is_none() {
                json!("")
            } else {
                origin.clone()
            };
            let problem = match (rp.as_str(), origin.as_str()) {
                (Some(rp), Some(origin)) => {
                    config::rate_limit_problem(section).or_else(|| config::problem(rp, origin))
                }
                _ => Some("coordination.maintainer.rp_id and .origin must be strings".into()),
            };
            let (keys, error) = match keys().await {
                Ok(v) => (Some(v), None),
                Err(e) => (None, Some(e.message)),
            };
            Ok(
                json!({"config_path":path,"rp_id":rp,"origin":origin,"problem":problem,"keys":keys,"keys_error":error}),
            )
        }
        ["--host-write", rp, origin] => {
            refuse_lite()?;
            config::write(rp, origin)
        }
        _ => Err(Error::new(
            "usage: pseudolife-mcp maintainer setup --host-state | --host-write <rp_id> <origin>",
            2,
        )),
    }
}

async fn keys() -> Result<Value> {
    let (data, _) = config::data_dir()?;
    let dsn = if let Some(value) = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")
        .ok()
        .filter(|v| !v.is_empty())
    {
        value
    } else if data.join("embedded_pg/PG_VERSION").is_file() {
        return Err(lite_refusal());
    } else {
        return Err(Error::refused("NoBank"));
    };
    async {
        let dsn = pg::Dsn::parse(&dsn).map_err(|_| Error::refused("OperationalError"))?;
        let session = tokio::time::timeout(std::time::Duration::from_secs(5), pg::Session::open(&dsn))
            .await.map_err(|_| Error::refused("OperationalError"))?
            .map_err(|_| Error::refused("OperationalError"))?;
        let present: bool = session.client().query_one(
            "SELECT to_regclass('public.maintainer_passkeys') IS NOT NULL", &[]
        ).await.map_err(bank_error)?.try_get(0).map_err(bank_error)?;
        if !present {
            return Err(Error::refused("this bank has no maintainer tables yet (schema older than v54); start a v54 daemon once to create them"));
        }
        let rows = session.client().query(
            "SELECT credential_id,label,state FROM maintainer_passkeys ORDER BY created_at,credential_id", &[]
        ).await.map_err(bank_error)?;
        let mut keys = Vec::new();
        for row in rows {
            let id: String = row.try_get(0).map_err(bank_error)?;
            let label: String = row.try_get(1).map_err(bank_error)?;
            let state: String = row.try_get(2).map_err(bank_error)?;
            keys.push(json!({"prefix": super::display::key_prefix(&id), "label": label, "state": state}));
        }
        let _ = session.close().await;
        Ok(json!(keys))
    }.await
}

fn lite_refusal() -> Error {
    Error::refused(
        "phase4-embedded-pg-deferred: native maintainer setup does not support the lite tier; run `python -m pseudolife_memory.cli maintainer setup` for this bank",
    )
}

fn refuse_lite() -> Result<()> {
    if std::env::var_os("PSEUDOLIFE_MCP_DATABASE_URL").is_some_and(|v| !v.is_empty()) {
        return Ok(());
    }
    if config::data_dir()?
        .0
        .join("embedded_pg/PG_VERSION")
        .is_file()
    {
        return Err(lite_refusal());
    }
    Ok(())
}

fn bank_error(error: tokio_postgres::Error) -> Error {
    Error::refused(
        error
            .code()
            .map(|code| super::sqlstate::class_name(code.code()))
            .unwrap_or("OperationalError"),
    )
}

async fn setup(args: Args, pending: Pending) -> Result<u8> {
    let url = format!("http://127.0.0.1:{}", args.port);
    let answer = system::health(args.port).await.ok_or_else(|| {
        Error::refused(format!(
            "no daemon answers at {url}/health: start it first (or pass --port)"
        ))
    })?;
    if answer["auth"] != true {
        return Err(Error::refused(format!(
            "the daemon at {url} does not report \"auth\": true; maintainer passkeys need a daemon that requires a bearer token (an install without --no-token, or PSEUDOLIFE_MCP_TOKEN(S) in its environment)"
        )));
    }
    let tier = system::tier().await?;
    if matches!(tier, Tier::Local) {
        refuse_lite()?;
    }
    let state = system::host(&tier, &["--host-state"]).await?;
    let configured = state["problem"].is_null();
    let ts = if configured {
        if state["origin"]
            .as_str()
            .unwrap_or("")
            .starts_with("https://")
        {
            tailnet::detect().await.ok().flatten()
        } else {
            None
        }
    } else if args.local {
        None
    } else {
        tailnet::detect().await?
    };
    let (rp, origin) = if configured {
        (
            state["rp_id"].as_str().unwrap_or("").into(),
            state["origin"].as_str().unwrap_or("").into(),
        )
    } else {
        tailnet::choose(&args, ts.as_ref())?
    };
    let mut plan = Vec::new();
    let mut serve = None;
    if origin.starts_with("https://")
        && let Some(ts) = &ts
        && ts.dns == rp
    {
        let https = reqwest::Url::parse(&origin)
            .ok()
            .and_then(|u| u.port_or_known_default())
            .unwrap_or(443);
        let config = tailnet::configuration(ts).await?;
        let (served, detail) = tailnet::state(&config, https, &rp, args.port);
        if served == "foreign" {
            return Err(Error::refused(if configured {
                format!(
                    "tailnet port {https} serves {}, not this daemon: the Console at {origin} may not reach it. Fix the serve yourself",
                    detail.unwrap_or_default()
                )
            } else {
                format!(
                    "tailnet port {https} already serves {}; setup never replaces another serve. Remove it yourself, or pass --https-port for a free port",
                    detail.unwrap_or_default()
                )
            }));
        }
        if served == "none" {
            serve = Some(https);
            plan.push(format!(
                "run      tailscale serve --bg --https={https} http://127.0.0.1:{}",
                args.port
            ));
        }
    }
    let write = !configured;
    if write {
        if state["problem"] != "unset" {
            plan.push(format!(
                "replace  the daemon's coordination.maintainer ({})",
                state["problem"].as_str().unwrap_or("invalid")
            ));
        }
        plan.push(format!("write    coordination.maintainer rp_id {rp}, origin {origin} into {} (a backup beside it)",state["config_path"].as_str().unwrap_or("")));
    }
    let loaded = if configured {
        system::running_name(args.port).await
    } else {
        None
    };
    let restart = write || loaded.is_some_and(|v| v != (rp.clone(), origin.clone()));
    if restart && matches!(tier, Tier::Docker(_)) {
        plan.push(format!(
            "restart  the daemon container ({}) so it reads them",
            system::CONTAINER
        ));
    }
    if origin.starts_with("http://localhost") {
        println!("Console name: {origin} (passkeys work in a browser on this machine only)");
    }
    if !plan.is_empty() {
        println!("plan:");
        for line in &plan {
            println!("  {line}");
        }
        consent(&args).await?;
        if let (Some(https), Some(ts)) = (serve, &ts) {
            tailnet::serve(ts, https, args.port).await?;
        }
        if write {
            let written = match system::host(&tier, &["--host-write", &rp, &origin]).await {
                Ok(written) => written,
                Err(error) => {
                    if let (Some(https), Some(ts)) = (serve, &ts) {
                        if tailnet::off(ts, https).await {
                            println!("took the new serve on port {https} back off");
                        } else {
                            crate::stderrln!(
                                "maintainer setup: the config write failed and the new serve remains; run `tailscale serve --https={https} off` yourself"
                            );
                        }
                    }
                    return Err(error);
                }
            };
            println!(
                "wrote {}{}",
                written["config_path"].as_str().unwrap_or(""),
                written["backup"]
                    .as_str()
                    .map(|v| format!(" (previous: {v})"))
                    .unwrap_or_default()
            );
        }
        if restart && matches!(tier, Tier::Docker(_)) {
            system::restart(&tier, args.port).await?;
        }
    }
    if restart && matches!(tier, Tier::Local) {
        println!(
            "Restart the daemon so it reads the name (stop the `pseudolife-mcp serve` process; your next session starts it again), then run `{SETUP}` again to enrol your passkey."
        );
        return Ok(if write { 0 } else { 4 });
    }
    let keys=state["keys"].as_array().ok_or_else(||Error::refused(format!("could not read the passkeys ({}); the name is in place: run `{SETUP}` again once the bank answers",state["keys_error"].as_str().unwrap_or("None"))))?;
    let active = keys.iter().filter(|v| v["state"] == "active").count();
    if active != 0 {
        println!(
            "in place: maintainer passkeys on {origin} (rp_id {rp}), {active} active key(s){}",
            if plan.is_empty() {
                "; nothing changed"
            } else {
                ""
            }
        );
        return Ok(0);
    }
    if !interactive() {
        println!(
            "The name is in place ({origin}). Enrolling a passkey needs you at the Console: run `{SETUP}` in a terminal."
        );
        return Ok(0);
    }
    if let Some(key) = keys.iter().find(|v| v["state"] == "pending") {
        let prefix = key["prefix"].as_str().unwrap_or("");
        let label = key["label"].as_str().unwrap_or("");
        println!("A passkey is pending: {prefix} ({label}).");
        return check_key(&tier, prefix, label, false, pending).await;
    }
    enrol(&tier, &origin, pending).await
}

async fn check_key(
    tier: &Tier,
    prefix: &str,
    label: &str,
    fresh: bool,
    pending: Pending,
) -> Result<u8> {
    *pending
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner) =
        Some((tier.clone(), prefix.into(), fresh));
    let quoted = python_repr(label);
    let answer=ask(format!("Does the Console (Settings, Your passkeys) show {prefix} labelled {quoted}? Activate it [y/N] ({}): ",if fresh {"N revokes it"} else {"N leaves it pending"})).await;
    if matches!(answer.as_str(), "y" | "yes") {
        let output = system::run(
            &system::command(tier, &["maintainer", "confirm", "--", prefix])?,
            120,
        )
        .await;
        if output.code != 0 {
            crate::stderrln!("maintainer setup: confirm failed: {}", output.detail());
            return Ok(4);
        }
        *pending
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
        println!("active: passkey {prefix} ({label}) now signs your Console messages and roles");
        return Ok(0);
    }
    if fresh {
        let output = system::run(
            &system::command(tier, &["maintainer", "revoke", "--", prefix])?,
            120,
        )
        .await;
        let state = if output.code == 0 {
            "revoked".into()
        } else {
            format!("NOT revoked (run `pseudolife-mcp maintainer revoke {prefix}`)")
        };
        println!(
            "passkey {prefix} {state}. If someone else redeemed the code, check `pseudolife-mcp maintainer list`; to try again, run `{SETUP}`"
        );
    } else {
        println!(
            "left pending. Revoke it if it is not yours: pseudolife-mcp maintainer revoke {prefix}"
        );
    }
    *pending
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
    Ok(4)
}
fn python_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut text = String::from(quote);
    for c in value.chars() {
        match c {
            '\\' => text.push_str("\\\\"),
            '\n' => text.push_str("\\n"),
            '\r' => text.push_str("\\r"),
            '\t' => text.push_str("\\t"),
            v if v == quote => {
                text.push('\\');
                text.push(v);
            }
            v => text.push(v),
        }
    }
    text.push(quote);
    text
}
async fn enrol(tier: &Tier, origin: &str, pending: Pending) -> Result<u8> {
    println!(
        "\nNext, in your browser: open {origin}/ui/. A browser keeps the Console's token per address: if this one says it did not accept the console's token, click \"Set a bearer token\" and paste the token your Console uses at its usual address: the daemon's PSEUDOLIFE_MCP_TOKEN, which a single-token install also keeps in your client's token file, ~/.pseudolife-mcp/<principal>.token. Then open Settings, Your passkeys, enter the code below with a label for this passkey, and create the passkey when the browser asks."
    );
    let command = system::command(tier, &["maintainer", "enrol-code"])?;
    // Python's stream() uses stderr=STDOUT. Share one OS pipe so child
    // diagnostics retain both their destination and their ordering.
    let (reader, writer) =
        io::pipe().map_err(|_| Error::refused("could not start passkey enrolment"))?;
    let stderr = writer
        .try_clone()
        .map_err(|_| Error::refused("could not start passkey enrolment"))?;
    let mut child = tokio::process::Command::new(&command[0])
        .args(&command[1..])
        .stdin(std::process::Stdio::null())
        .stdout(writer)
        .stderr(stderr)
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| Error::refused("could not start passkey enrolment"))?;
    #[cfg(unix)]
    let reader = std::fs::File::from(std::os::fd::OwnedFd::from(reader));
    #[cfg(windows)]
    let reader = std::fs::File::from(std::os::windows::io::OwnedHandle::from(reader));
    let mut lines = tokio::io::BufReader::new(tokio::fs::File::from_std(reader)).lines();
    let mut enrolled = None;
    while let Some(line) = lines
        .next_line()
        .await
        .map_err(|_| Error::refused("could not read passkey enrolment"))?
    {
        if let Some(rest) = line.strip_prefix("Enrolled (pending): ")
            && let Some((prefix, label)) = rest.split_once("  label: ")
        {
            enrolled = Some((prefix.to_owned(), label.trim().to_owned()));
            *pending
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) =
                Some((tier.clone(), prefix.into(), true));
            println!("{line}");
        } else if enrolled.is_none() {
            println!("{line}");
        }
        let _ = io::stdout().flush();
    }
    let _ = child.wait().await;
    let Some((prefix, label)) = enrolled else {
        crate::stderrln!(
            "maintainer setup: no passkey was enrolled; run `{SETUP}` again for a new code"
        );
        return Ok(4);
    };
    check_key(tier, &prefix, &label, true, pending).await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn yes_only_consents_to_host_changes() {
        let args = parse(&["--yes".into(), "--local".into(), "--https-port=443".into()]).unwrap();
        assert!(args.yes);
        assert_eq!(args.https_port, 443);
        for value in ["0", "65536", "bad"] {
            assert!(parse(&["--port".into(), value.into()]).is_err());
        }
        assert_eq!(python_repr("Tom's phone"), "\"Tom's phone\"");
    }
}
