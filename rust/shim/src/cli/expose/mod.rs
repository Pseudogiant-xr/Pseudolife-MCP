//! `expose`: `pseudolife-mcp expose {tailscale,off,status}` up to the change.
//!
//! Oracle: `pseudolife_memory/expose_cli.py` (`main`, `_expose`, `_off`,
//! `_status`, `_Report`). Answered natively: help and argparse's errors (at
//! `COLUMNS=80`), all of `status`, and `tailscale` / `off` through every
//! check that only reads (the local daemon's `/health`, the `tailscale`
//! lookup, `status --json`, `serve status --json`, `ip -4`): each refusal,
//! "already exposed", "not exposed", and the refusal to change anything
//! without `--yes` when there is no terminal to confirm on. The change itself
//! (`serve --bg ...` / `serve --tcp=N off`), the interactive confirmation,
//! and anything outside the reproduced domain defer before any call that
//! changes the serve config (`rust/cli_harness/rows/expose.md`).
mod args;
mod tailscale;

/// `find_tailscale()` and `_is_ipv4`, which `invite` reads through
/// `expose status`.
pub(super) use tailscale::{find as find_tailscale, is_ipv4};

use super::hook_http;
use super::pyjson::{self, Dict, J};
use args::{Action, Args, Parsed};
use std::{ffi::OsString, io, process::ExitCode, time::Duration};
use tailscale::State;

/// Outside the reproduced domain: the dispatcher's deferral line.
#[derive(Debug)]
struct Defer;
type Res<T> = Result<T, Defer>;

/// Why a reader stopped: the oracle's `_Refused` text, or a deferral.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Fault {
    Refused(String),
    Defer,
}

const EXIT_OK: u8 = 0;
const EXIT_USAGE: u8 = 2;
const EXIT_REFUSED: u8 = 4;
/// `DEFAULT_PORT`.
pub(super) const DEFAULT_PORT: u16 = 8765;
/// `LOCAL_HEALTH_TIMEOUT_S`.
const LOCAL_HEALTH_TIMEOUT: Duration = Duration::from_secs(3);
const INSTALL_FIX: &str =
    "install Tailscale (https://tailscale.com/download), then run `tailscale up`";

/// Python `repr()` of a str.
pub(super) fn py_repr(text: &str) -> String {
    pyjson::py_value_repr(&J::Str(text.to_owned()))
}

fn units(text: &str) -> Vec<u16> {
    text.encode_utf16().collect()
}

/// What the oracle's argument handling depends on besides argv.
struct Context {
    /// argparse wraps help and usage at `COLUMNS - 2`; only 80 is reproduced.
    columns_80: bool,
    colour_stdout: bool,
    colour_stderr: bool,
    utf8_argv: bool,
}

/// One answer: the stdout text (code units, written with a lone surrogate
/// escaped as `_write` escapes it), the stderr text, the exit code.
#[derive(Debug, PartialEq, Eq)]
struct Out {
    stdout: Vec<u16>,
    stderr: String,
    code: u8,
}

/// `_Report`: the JSON report's fields, and the lines `say` and `fail`
/// print without `--json`.
struct Report {
    action: Action,
    port: u16,
    as_json: bool,
    tailscale: Option<String>,
    state: Option<&'static str>,
    url: Option<String>,
    detail: Option<J>,
    command: Option<Vec<String>>,
    daemon: Option<J>,
    error: Option<J>,
    stdout: Vec<u16>,
    stderr: String,
}

impl Report {
    fn new(args: &Args) -> Self {
        Self {
            action: args.action,
            port: args.port,
            as_json: args.json,
            tailscale: None,
            state: None,
            url: None,
            detail: None,
            command: None,
            daemon: None,
            error: None,
            stdout: Vec::new(),
            stderr: String::new(),
        }
    }

    fn say(&mut self, line: &[u16]) {
        if !self.as_json {
            self.stdout.extend_from_slice(line);
            self.stdout.push(u16::from(b'\n'));
        }
    }

    fn say_text(&mut self, line: &str) {
        self.say(&units(line));
    }

    /// `fail(code, line)`: the line on stderr (`expose: ...`), written with
    /// `backslashreplace` as Python's stderr writes a lone surrogate.
    fn fail(mut self, code: u8, line: &[u16]) -> Out {
        self.error = Some(pyjson::text(line.to_vec()));
        if !self.as_json {
            self.stderr
                .push_str(&format!("expose: {}\n", pyjson::backslashreplace(line)));
        }
        self.finish(code)
    }

    fn fail_text(self, code: u8, line: &str) -> Out {
        self.fail(code, &units(line))
    }

    fn finish(mut self, code: u8) -> Out {
        if self.as_json {
            let text = |value: Option<String>| value.map_or(J::Null, J::Str);
            let data = J::Dict(vec![
                (J::from("action"), J::from(self.action.name())),
                (J::from("port"), J::Int(self.port.to_string())),
                (J::from("tailscale"), text(self.tailscale.take())),
                (J::from("state"), text(self.state.map(str::to_owned))),
                (J::from("url"), text(self.url.take())),
                (J::from("detail"), self.detail.take().unwrap_or(J::Null)),
                (
                    J::from("command"),
                    self.command.take().map_or(J::Null, |words| {
                        J::List(words.into_iter().map(J::Str).collect())
                    }),
                ),
                (J::from("daemon"), self.daemon.take().unwrap_or(J::Null)),
                (J::from("probe"), J::Null),
                (J::from("warnings"), J::List(Vec::new())),
                (J::from("notes"), J::List(Vec::new())),
                (J::from("error"), self.error.take().unwrap_or(J::Null)),
                (J::from("exit"), J::Int(code.to_string())),
            ]);
            self.stdout
                .extend(units(&(pyjson::dumps(&data, true) + "\n")));
        }
        Out {
            stdout: self.stdout,
            stderr: self.stderr,
            code,
        }
    }
}

/// `fetch_health(url, timeout)`'s kinds.
enum Health {
    Ok(Dict),
    None,
    Other,
}

/// `fetch_health("http://127.0.0.1:<port>", 3)` over a no-proxy,
/// no-redirect opener; an answer nested past [`tailscale::DEPTH`] is
/// "other", as `_json` refuses it.
async fn fetch_health(port: u16) -> Res<Health> {
    let reply = hook_http::get_bounded(
        &format!("http://127.0.0.1:{port}"),
        "/health",
        None,
        LOCAL_HEALTH_TIMEOUT,
        false,
        (None, None),
    )
    .await;
    let Some(reply) = reply else {
        return Ok(Health::None);
    };
    if (300..400).contains(&reply.status) {
        return Ok(Health::Other);
    }
    if reply.body_failed {
        // A 2xx body that fails to read raises out of the `with` block
        // (nothing answered); an error's `exc.read()` failing is "other".
        return Ok(if (200..300).contains(&reply.status) {
            Health::None
        } else {
            Health::Other
        });
    }
    let Ok(text) = std::str::from_utf8(&reply.body) else {
        return Ok(Health::Other);
    };
    Ok(match tailscale::json(text).map_err(|_| Defer)? {
        Ok(Some(J::Dict(payload))) => Health::Ok(payload),
        // Not an object, not JSON, or nested past the bound.
        Ok(_) | Err(_) => Health::Other,
    })
}

fn field(payload: &Dict, key: &str) -> J {
    pyjson::get(payload, key).cloned().unwrap_or(J::Null)
}

/// `_confirm`: `--yes` goes on to the change, which defers here; without
/// it, a terminal would be asked (defers) and no terminal refuses.
fn confirm(args: &Args, report: Report) -> Res<Out> {
    if !args.yes {
        if super::connect::stdin_is_tty() {
            return Err(Defer);
        }
        return Ok(report.fail_text(EXIT_USAGE, "no terminal to confirm on: rerun with --yes"));
    }
    Err(Defer)
}

/// `find_tailscale()`.
fn find() -> Res<Option<String>> {
    tailscale::find().map_err(|()| Defer)
}

/// A reader's answer: the value, the refusal text, or a deferral.
fn read<T>(result: Result<T, Fault>) -> Res<Result<T, String>> {
    match result {
        Ok(value) => Ok(Ok(value)),
        Err(Fault::Refused(why)) => Ok(Err(why)),
        Err(Fault::Defer) => Err(Defer),
    }
}

/// `_running` then `_serve_config`, as the actions call them in one `try`.
async fn running_and_config(binary: &str) -> Res<Result<(Dict, Dict), String>> {
    let status = match read(tailscale::running(binary).await)? {
        Ok(status) => status,
        Err(why) => return Ok(Err(why)),
    };
    Ok(read(tailscale::serve_config(binary).await)?.map(|config| (status, config)))
}

async fn tailnet_ipv4(binary: &str, status: &Dict) -> Res<Option<String>> {
    tailscale::tailnet_ipv4(binary, status)
        .await
        .map_err(|()| Defer)
}

/// `_expose(args, report)` up to the change.
async fn expose(args: &Args, mut report: Report) -> Res<Out> {
    let port = args.port;
    let local = format!("http://127.0.0.1:{port}");
    let health = match fetch_health(port).await? {
        Health::Other => {
            return Ok(report.fail_text(
                EXIT_REFUSED,
                &format!(
                    "something that is not the Pseudolife daemon answers on 127.0.0.1:{port} (a redirect or a body that is not its /health JSON); pass --port for the daemon's port"
                ),
            ));
        }
        Health::None => {
            return Ok(report.fail_text(
                EXIT_REFUSED,
                &format!("no daemon answers at {local}/health: start it first (or pass --port)"),
            ));
        }
        Health::Ok(health) => health,
    };
    report.daemon = Some(J::Dict(vec![
        (J::from("status"), field(&health, "status")),
        (J::from("auth"), field(&health, "auth")),
        (J::from("bank"), field(&health, "bank")),
    ]));
    let status = pyjson::get(&health, "status");
    if status.and_then(J::as_str) != Some("ok") {
        let shown = status.map_or_else(|| "None".to_owned(), pyjson::py_value_repr);
        return Ok(report.fail_text(
            EXIT_REFUSED,
            &format!("the daemon at {local} reports status {shown}: fix it before exposing it"),
        ));
    }
    if pyjson::get(&health, "auth") != Some(&J::Bool(true)) {
        return Ok(report.fail_text(
            EXIT_REFUSED,
            &format!(
                "the daemon at {local} does not report \"auth\": true, so anyone on the tailnet could read and write the bank. Give it a token first (PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKENS, see docs/guide/remote-bank.md); an unauthenticated bank is never exposed"
            ),
        ));
    }
    let Some(binary) = find()? else {
        return Ok(report.fail_text(
            EXIT_REFUSED,
            &format!("the tailscale CLI was not found: {INSTALL_FIX}"),
        ));
    };
    report.tailscale = Some(binary.clone());
    let (status, config) = match running_and_config(&binary).await? {
        Ok(both) => both,
        Err(why) => return Ok(report.fail_text(EXIT_REFUSED, &why)),
    };
    let (state, detail) = tailscale::port_state(&config, port);
    if state == State::Foreign {
        let line = [
            units(&format!("tailnet port {port} already serves ")),
            detail.unwrap_or_default(),
            units("; expose never replaces another serve. Remove it yourself, or pass --port for the daemon's port if this is not it"),
        ]
        .concat();
        return Ok(report.fail(EXIT_REFUSED, &line));
    }
    let Some(ip) = tailnet_ipv4(&binary, &status).await? else {
        return Ok(report.fail_text(
            EXIT_REFUSED,
            "tailscale reports no tailnet IPv4 address for this host",
        ));
    };
    let url = format!("http://{ip}:{port}");
    if state == State::Ours {
        report.state = Some("already_exposed");
        report.url = Some(url.clone());
        report.say_text(&format!("already exposed: clients use {url}"));
        return Ok(report.finish(EXIT_OK));
    }
    let serve = [
        "serve".to_owned(),
        "--bg".to_owned(),
        format!("--tcp={port}"),
        format!("tcp://127.0.0.1:{port}"),
    ];
    report.command = Some(
        std::iter::once("tailscale".to_owned())
            .chain(serve.iter().cloned())
            .collect(),
    );
    report.say_text("plan:");
    report.say_text(&format!("  run      tailscale {}", serve.join(" ")));
    report.say_text(&format!("  clients  {url}"));
    confirm(args, report)
}

/// `_off(args, report)` up to the change.
async fn off(args: &Args, mut report: Report) -> Res<Out> {
    let port = args.port;
    let Some(binary) = find()? else {
        return Ok(report.fail_text(
            EXIT_REFUSED,
            &format!("the tailscale CLI was not found: {INSTALL_FIX}"),
        ));
    };
    report.tailscale = Some(binary.clone());
    let (_status, config) = match running_and_config(&binary).await? {
        Ok(both) => both,
        Err(why) => return Ok(report.fail_text(EXIT_REFUSED, &why)),
    };
    match tailscale::port_state(&config, port) {
        (State::None, _) => {
            report.state = Some("not_exposed");
            report.say_text(&format!("not exposed: nothing serves tailnet port {port}"));
            Ok(report.finish(EXIT_OK))
        }
        (State::Foreign, detail) => {
            let detail = detail.unwrap_or_default();
            report.state = Some("foreign");
            report.detail = Some(pyjson::text(detail.clone()));
            let line = [
                units(&format!("tailnet port {port} serves ")),
                detail,
                units(&format!(
                    ", not this daemon's forward to 127.0.0.1:{port}; left alone"
                )),
            ]
            .concat();
            Ok(report.fail(EXIT_REFUSED, &line))
        }
        (State::Ours, _) => {
            let words = [
                "serve".to_owned(),
                format!("--tcp={port}"),
                "off".to_owned(),
            ];
            report.command = Some(
                std::iter::once("tailscale".to_owned())
                    .chain(words.iter().cloned())
                    .collect(),
            );
            report.say_text("plan:");
            report.say_text(&format!("  run      tailscale {}", words.join(" ")));
            confirm(args, report)
        }
    }
}

/// `_status(args, report)`: reads only.
async fn status(args: &Args, mut report: Report) -> Res<Out> {
    let port = args.port;
    let Some(binary) = find()? else {
        report.state = Some("unavailable");
        report.detail = Some(J::from("the tailscale CLI was not found"));
        report.say_text("not exposed: the tailscale CLI was not found");
        return Ok(report.finish(EXIT_OK));
    };
    report.tailscale = Some(binary.clone());
    let (status, config) = match running_and_config(&binary).await? {
        Ok(both) => both,
        Err(why) => {
            report.state = Some("unavailable");
            report.detail = Some(J::Str(why.clone()));
            report.say_text(&format!("not exposed: {why}"));
            return Ok(report.finish(EXIT_OK));
        }
    };
    match tailscale::port_state(&config, port) {
        (State::Foreign, detail) => {
            let detail = detail.unwrap_or_default();
            report.state = Some("foreign");
            report.detail = Some(pyjson::text(detail.clone()));
            report.say(
                &[
                    units(&format!("not exposed: tailnet port {port} serves ")),
                    detail,
                ]
                .concat(),
            );
            Ok(report.finish(EXIT_OK))
        }
        (State::None, _) => {
            report.state = Some("not_exposed");
            report.say_text("not exposed");
            Ok(report.finish(EXIT_OK))
        }
        (State::Ours, _) => {
            let Some(ip) = tailnet_ipv4(&binary, &status).await? else {
                report.state = Some("unavailable");
                report.detail = Some(J::from("no tailnet IPv4 address for this host"));
                report.say_text(
                    "not exposed: tailscale reports no tailnet IPv4 address for this host",
                );
                return Ok(report.finish(EXIT_OK));
            };
            let url = format!("http://{ip}:{port}");
            report.state = Some("exposed");
            report.url = Some(url.clone());
            report.say_text(&format!("exposed: clients use {url}"));
            Ok(report.finish(EXIT_OK))
        }
    }
}

/// argparse, then the action.
fn decide(argv: &[String], context: &Context) -> Res<Out> {
    if !context.utf8_argv && !argv.iter().all(|argument| argument.is_ascii()) {
        return Err(Defer);
    }
    let args = match args::parse(argv) {
        Parsed::Defer => return Err(Defer),
        Parsed::MainHelp | Parsed::Help(_) if !context.columns_80 || context.colour_stdout => {
            return Err(Defer);
        }
        Parsed::Error(..) if !context.columns_80 || context.colour_stderr => return Err(Defer),
        Parsed::MainHelp => {
            return Ok(Out {
                stdout: units(args::MAIN_HELP),
                stderr: String::new(),
                code: EXIT_OK,
            });
        }
        Parsed::Help(action) => {
            return Ok(Out {
                stdout: units(&action.help()),
                stderr: String::new(),
                code: EXIT_OK,
            });
        }
        Parsed::Error(parser, message) => {
            let (usage, prog) = match parser {
                Some(action) => (
                    action.usage(),
                    format!("pseudolife-mcp expose {}", action.name()),
                ),
                None => (
                    args::main_usage().to_owned(),
                    "pseudolife-mcp expose".to_owned(),
                ),
            };
            return Ok(Out {
                stdout: Vec::new(),
                stderr: format!("{usage}{prog}: error: {message}\n"),
                code: EXIT_USAGE,
            });
        }
        Parsed::Args(args) => args,
    };
    let report = Report::new(&args);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| Defer)?;
    runtime.block_on(async {
        match args.action {
            Action::Tailscale => expose(&args, report).await,
            Action::Off => off(&args, report).await,
            Action::Status => status(&args, report).await,
        }
    })
}

/// `None` defers to the dispatcher. Nothing is written before the answer
/// is complete, so a deferral leaves both streams untouched.
pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    use std::io::IsTerminal;
    // json.loads and int() read integers under this digit limit (4300 unset).
    if std::env::var_os("PYTHONINTMAXSTRDIGITS").is_some() {
        return None;
    }
    let argv = values
        .into_iter()
        .map(OsString::into_string)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let colour = super::move_cli::colour_env(|name| std::env::var_os(name).is_some());
    let context = Context {
        columns_80: std::env::var_os("COLUMNS").is_some_and(|value| value == "80"),
        colour_stdout: colour || io::stdout().is_terminal(),
        colour_stderr: colour || io::stderr().is_terminal(),
        utf8_argv: super::move_cli::utf8_argv(),
    };
    let out = decide(&argv, &context).ok()?;
    // `_write` escapes a lone surrogate as `\udXXX` on stdout too.
    let stdout = super::text_bytes(&pyjson::backslashreplace(&out.stdout));
    let stderr = super::text_bytes(&out.stderr);
    if !stdout.is_empty() {
        match super::move_cli::emit(&mut io::stdout().lock(), &stdout) {
            Ok(()) => {}
            Err(0) => return None,
            Err(_) => return Some(ExitCode::FAILURE),
        }
    }
    if !stderr.is_empty() {
        match super::move_cli::emit(&mut io::stderr().lock(), &stderr) {
            Ok(()) => {}
            Err(0) if stdout.is_empty() => return None,
            Err(_) => return Some(ExitCode::FAILURE),
        }
    }
    Some(ExitCode::from(out.code))
}

#[cfg(test)]
mod tests;
