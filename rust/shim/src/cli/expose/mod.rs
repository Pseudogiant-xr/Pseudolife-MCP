//! `expose`: `pseudolife-mcp expose {tailscale,off,status}`.
//!
//! `tailscale` checks the local daemon (`/health` answers `status: ok` and
//! `"auth": true`), finds the `tailscale` CLI (absolute `PATH` directories
//! only, then the default install location), reads its state, shows the
//! plan, asks once at a terminal (or takes `--yes`), runs `tailscale serve
//! --bg --tcp=N tcp://127.0.0.1:N`, reads the serve state back and reports
//! what is there: exposed at the tailnet address, or the failure with the
//! state the host is left in. `off` removes only this daemon's forward the
//! same way. `status` only reads. Behaviour follows
//! `pseudolife_memory/expose_cli.py` where it is sensible; the differences
//! are listed in `rust/cli_harness/rows/expose.md`.
//!
//! Every effect goes through [`Host`], so the tests drive the whole flow
//! with a fake `tailscale`, daemon and terminal.
mod args;
mod tailscale;

use super::hook_http;
use super::pyjson::{self, Dict, J};
use super::restore_cli::move_native::protocol::MOVE_EXPOSURE_REPORT_ENV;
use args::{Action, Args, Parsed};
use std::{
    ffi::OsString,
    io,
    process::ExitCode,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tailscale::{Binary, Proc, State};

const EXIT_OK: u8 = 0;
const EXIT_FAILED: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_REFUSED: u8 = 4;
/// Changed, but the result is not the one asked for (undone where that is
/// safe), or it could not be read back.
const EXIT_UNDONE: u8 = 5;
/// Ctrl-C at the question.
const EXIT_INTERRUPTED: u8 = 130;
/// `DEFAULT_PORT`.
pub(super) const DEFAULT_PORT: u16 = 8765;
/// `LOCAL_HEALTH_TIMEOUT_S`: a loopback `/health` answers in milliseconds.
const LOCAL_HEALTH_TIMEOUT: Duration = Duration::from_secs(3);
/// `TAILNET_HEALTH_TIMEOUT_S`: the advisory probe may cross a relay.
const TAILNET_HEALTH_TIMEOUT: Duration = Duration::from_secs(5);
/// How long after an answer that is not yes a Ctrl-C still counts as the
/// reason (a console read can end before the interrupt is delivered).
/// A design bound, far below what a person notices.
const INTERRUPT_GRACE: Duration = Duration::from_millis(200);
const INSTALL_FIX: &str =
    "install Tailscale (https://tailscale.com/download), then run `tailscale up`";
const NO_TERMINAL: &str = "no terminal to confirm on, so nothing changed: rerun with --yes";
const NOT_CONFIRMED: &str = "not confirmed (only y or yes goes ahead); nothing changed";
const INTERRUPTED: &str = "interrupted at the question; nothing changed";

/// Python `repr()` of a str.
pub(super) fn py_repr(text: &str) -> String {
    pyjson::py_value_repr(&J::Str(text.to_owned()))
}

fn units(text: &str) -> Vec<u16> {
    text.encode_utf16().collect()
}

/// One answer: the stdout text (code units, written with a lone surrogate
/// escaped as `_write` escapes it), the stderr text, the exit code.
#[derive(Debug, PartialEq, Eq)]
struct Out {
    stdout: Vec<u16>,
    stderr: String,
    code: u8,
}

/// Bytes as the streams take them: a lone surrogate escaped, and CRLF on
/// Windows.
fn encode(stdout: &[u16], stderr: &str) -> (Vec<u8>, Vec<u8>) {
    (
        super::text_bytes(&pyjson::backslashreplace(stdout)),
        super::text_bytes(stderr),
    )
}

// ── what the command reaches ─────────────────────────────────────────────────

/// `fetch_health(url, timeout)`'s kinds.
#[derive(Debug, Clone)]
enum Health {
    Ok(Dict),
    None,
    Other,
}

/// The answer to the confirmation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Answer {
    Yes,
    No,
    Interrupted,
}

/// Everything outside the process: the `tailscale` CLI, the daemon's
/// `/health`, the terminal and Ctrl-C.
#[allow(async_fn_in_trait)]
trait Host {
    fn find(&self) -> Option<Binary>;
    async fn run(&self, binary: &Binary, args: &[&str]) -> Proc;
    async fn health(&self, origin: &str, timeout: Duration) -> Health;
    /// Whether stdin is a terminal to ask on.
    fn interactive(&self) -> bool;
    /// Write what the report printed so far (the plan) before a question;
    /// `false` when a stream refused it.
    fn show(&self, stdout: &[u8], stderr: &[u8]) -> bool;
    async fn ask(&self, question: &str) -> Answer;
    /// From the change on, Ctrl-C is noted rather than fatal, so the result
    /// is still read back and reported (the `tailscale` child receives the
    /// same interrupt and stops on its own).
    fn hold_interrupts(&self);
    fn interrupted(&self) -> bool;
}

/// Ctrl-C (SIGINT on POSIX, the console's Ctrl-C on Windows), listened for
/// from the moment this is made.
struct Interrupts(
    #[cfg(unix)] tokio::signal::unix::Signal,
    #[cfg(windows)] tokio::signal::windows::CtrlC,
);

impl Interrupts {
    fn listen() -> Option<Self> {
        #[cfg(unix)]
        {
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
                .ok()
                .map(Self)
        }
        #[cfg(windows)]
        {
            tokio::signal::windows::ctrl_c().ok().map(Self)
        }
    }

    async fn recv(&mut self) {
        if self.0.recv().await.is_none() {
            std::future::pending::<()>().await;
        }
    }
}

/// This machine.
#[derive(Default)]
struct Real {
    interrupted: Arc<AtomicBool>,
}

impl Host for Real {
    fn find(&self) -> Option<Binary> {
        tailscale::find()
    }

    async fn run(&self, binary: &Binary, args: &[&str]) -> Proc {
        tailscale::run(binary, args).await
    }

    async fn health(&self, origin: &str, timeout: Duration) -> Health {
        fetch_health(origin, timeout).await
    }

    /// A character device that is also a terminal: on Windows `NUL` is the
    /// first but not the second, and nobody can answer a question there.
    fn interactive(&self) -> bool {
        use std::io::IsTerminal;
        super::connect::stdin_is_tty() && io::stdin().is_terminal()
    }

    fn show(&self, stdout: &[u8], stderr: &[u8]) -> bool {
        (stdout.is_empty() || super::move_cli::emit(&mut io::stdout().lock(), stdout).is_ok())
            && (stderr.is_empty()
                || super::move_cli::emit(&mut io::stderr().lock(), stderr).is_ok())
    }

    async fn ask(&self, question: &str) -> Answer {
        let Some(mut interrupts) = Interrupts::listen() else {
            return if super::connect::terminal::ask(question).await {
                Answer::Yes
            } else {
                Answer::No
            };
        };
        let said = tokio::select! {
            biased;
            () = interrupts.recv() => None,
            yes = super::connect::terminal::ask(question) => Some(yes),
        };
        match said {
            Some(true) => Answer::Yes,
            Some(false) => {
                if tokio::time::timeout(INTERRUPT_GRACE, interrupts.recv())
                    .await
                    .is_ok()
                {
                    Answer::Interrupted
                } else {
                    Answer::No
                }
            }
            None => Answer::Interrupted,
        }
    }

    fn hold_interrupts(&self) {
        if let Some(mut interrupts) = Interrupts::listen() {
            let flag = Arc::clone(&self.interrupted);
            tokio::spawn(async move {
                loop {
                    interrupts.recv().await;
                    flag.store(true, Ordering::SeqCst);
                }
            });
        }
    }

    fn interrupted(&self) -> bool {
        self.interrupted.load(Ordering::SeqCst)
    }
}

/// `fetch_health("<origin>/health", timeout)` over a no-proxy,
/// no-redirect opener; an answer nested past [`tailscale::DEPTH`] is
/// "other", as `_json` refuses it.
async fn fetch_health(origin: &str, timeout: Duration) -> Health {
    let Some(reply) =
        hook_http::get_bounded(origin, "/health", None, timeout, false, (None, None)).await
    else {
        return Health::None;
    };
    if (300..400).contains(&reply.status) {
        return Health::Other;
    }
    if reply.body_failed {
        // A 2xx body that fails to read is nothing answering; an error's
        // body failing is "other".
        return if (200..300).contains(&reply.status) {
            Health::None
        } else {
            Health::Other
        };
    }
    let Ok(text) = std::str::from_utf8(&reply.body) else {
        return Health::Other;
    };
    match tailscale::json(text) {
        Ok(Some(J::Dict(payload))) => Health::Ok(payload),
        // Not an object, not JSON, or nested past the bound.
        _ => Health::Other,
    }
}

// ── the report ───────────────────────────────────────────────────────────────

/// `_Report`: the JSON report's fields, and the lines printed without
/// `--json`.
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
    probe: Option<J>,
    warnings: Vec<Vec<u16>>,
    notes: Vec<String>,
    error: Option<J>,
    include_move_failure: bool,
    move_failure: Option<&'static str>,
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
            probe: None,
            warnings: Vec::new(),
            notes: Vec::new(),
            error: None,
            include_move_failure: std::env::var_os(MOVE_EXPOSURE_REPORT_ENV).as_deref()
                == Some(std::ffi::OsStr::new("1")),
            move_failure: matches!(args.action, Action::Tailscale).then_some("not_attempted"),
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

    /// A warning: on stderr without `--json`, in `warnings` always.
    fn warn(&mut self, line: &[u16]) {
        self.warnings.push(line.to_vec());
        if !self.as_json {
            self.stderr
                .push_str(&format!("{}\n", pyjson::backslashreplace(line)));
        }
    }

    fn warn_text(&mut self, line: &str) {
        self.warn(&units(line));
    }

    /// A next step: printed, and in `notes`.
    fn note(&mut self, line: String) {
        self.say_text(&line);
        self.notes.push(line);
    }

    /// What is buffered so far, to be written now (before a question).
    fn take_pending(&mut self) -> (Vec<u16>, String) {
        (
            std::mem::take(&mut self.stdout),
            std::mem::take(&mut self.stderr),
        )
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
            let mut fields = vec![
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
                (J::from("probe"), self.probe.take().unwrap_or(J::Null)),
                (
                    J::from("warnings"),
                    J::List(
                        std::mem::take(&mut self.warnings)
                            .into_iter()
                            .map(pyjson::text)
                            .collect(),
                    ),
                ),
                (
                    J::from("notes"),
                    J::List(
                        std::mem::take(&mut self.notes)
                            .into_iter()
                            .map(J::Str)
                            .collect(),
                    ),
                ),
                (J::from("error"), self.error.take().unwrap_or(J::Null)),
                (J::from("exit"), J::Int(code.to_string())),
            ];
            if self.include_move_failure
                && let Some(failure) = self.move_failure
            {
                fields.push((J::from("move_failure"), J::from(failure)));
            }
            let data = J::Dict(fields);
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

fn field(payload: &Dict, key: &str) -> J {
    pyjson::get(payload, key).cloned().unwrap_or(J::Null)
}

// ── consent ──────────────────────────────────────────────────────────────────

/// `_confirm`: `--yes` goes ahead; without it, no terminal refuses and a
/// terminal is asked once. `Err` is the finished answer: nothing changed.
/// After a question (which may have waited a long time) the port must
/// still be as the plan found it (`expected`), or nothing is changed.
async fn consent(
    host: &impl Host,
    args: &Args,
    mut report: Report,
    question: &str,
    (binary, expected, url): (&Binary, State, Option<&str>),
) -> Result<Report, Out> {
    if args.yes {
        return Ok(report);
    }
    if !host.interactive() {
        return Err(report.fail_text(EXIT_USAGE, NO_TERMINAL));
    }
    let (stdout, stderr) = report.take_pending();
    let (stdout, stderr) = encode(&stdout, &stderr);
    if !host.show(&stdout, &stderr) {
        return Err(report.fail_text(
            EXIT_FAILED,
            "could not write the plan to the terminal, so nothing changed: rerun with stdout and stderr open, or pass --yes to skip the question",
        ));
    }
    match host.ask(question).await {
        Answer::Yes => unchanged(host, binary, (args.port, expected, url), report).await,
        Answer::No => Err(report.fail_text(EXIT_USAGE, NOT_CONFIRMED)),
        Answer::Interrupted => Err(report.fail_text(EXIT_INTERRUPTED, INTERRUPTED)),
    }
}

/// The port read again after the question: unchanged goes ahead; already
/// in the state asked for (this daemon's forward put there, or removed,
/// meanwhile) is the goal met, in one line; anything else is named and
/// nothing is changed.
async fn unchanged(
    host: &impl Host,
    binary: &Binary,
    (port, expected, url): (u16, State, Option<&str>),
    mut report: Report,
) -> Result<Report, Out> {
    let (state, detail) = match read_back(host, binary, port).await {
        Ok(now) => now,
        Err(why) => {
            return Err(report.fail_text(
                EXIT_REFUSED,
                &format!(
                    "could not read the serve state again after the answer ({why}); nothing changed: check `tailscale serve status`, then rerun"
                ),
            ));
        }
    };
    if state == expected {
        return Ok(report);
    }
    match (expected, state, url) {
        (State::None, State::Ours, Some(url)) => {
            report.command = None;
            report.state = Some("already_exposed");
            report.url = Some(url.to_owned());
            report.say_text(&format!("already exposed: clients use {url}"));
            return Err(report.finish(EXIT_OK));
        }
        (State::Ours, State::None, _) => {
            report.command = None;
            report.state = Some("not_exposed");
            report.say_text(&format!("not exposed: nothing serves tailnet port {port}"));
            return Err(report.finish(EXIT_OK));
        }
        _ => {}
    }
    let now = match state {
        State::None => units("nothing"),
        State::Ours => units(&format!("this daemon's forward to 127.0.0.1:{port}")),
        State::Foreign => detail.unwrap_or_default(),
    };
    let line = [
        units(&format!(
            "tailnet port {port} changed while the question waited: it now serves "
        )),
        now,
        units("; nothing changed. Rerun to see the plan for it now"),
    ]
    .concat();
    Err(report.fail(EXIT_REFUSED, &line))
}

// ── the change and what it left ──────────────────────────────────────────────

/// `_PERMISSION`: how the CLI words a refusal for this user ("Access
/// denied: serve config denied" on Linux without root or an operator
/// grant, and on Windows for a user other than the one logged in to
/// Tailscale; or a hint naming `--operator`).
fn permission_refused(proc: &Proc) -> bool {
    static PERMISSION: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let pattern = PERMISSION.get_or_init(|| {
        regex::RegexBuilder::new(
            r"access denied|permission denied|not permitted|--operator\b|unauthori[sz]ed|must be (?:root|admin)",
        )
        .case_insensitive(true)
        .build()
        .expect("a valid pattern")
    });
    !proc.succeeded() && !proc.timed_out && pattern.is_match(&proc.err)
}

/// `_permission_fix()`: what lets this user change the serve config, worded
/// once for `expose` and `maintainer setup`
/// ([`crate::cli::maintainer::setup::tailscale_permission_fix`]).
fn permission_fix() -> &'static str {
    crate::cli::maintainer::setup::tailscale_permission_fix(std::env::consts::OS)
}

/// Said only once the serve state was read back unchanged.
fn permission_message(proc: &Proc) -> String {
    format!(
        "tailscale refused the change for this user ({}); nothing changed: {}",
        tailscale::first_line(&proc.err),
        permission_fix()
    )
}

/// What a call did, for a message: its first stderr line, `exit N`, or the
/// timeout.
fn ran(proc: &Proc) -> String {
    if proc.timed_out {
        proc.err.clone()
    } else {
        tailscale::detail(proc)
    }
}

/// `exit N, <first stderr line>`, or the timeout.
fn ran_fully(proc: &Proc) -> String {
    if proc.timed_out {
        return proc.err.clone();
    }
    let line = tailscale::first_line(&proc.err);
    if line.is_empty() {
        format!("exit {}", proc.code)
    } else {
        format!("exit {}, {line}", proc.code)
    }
}

/// The link a wedged serve printed on either stream (Serve not enabled for
/// the tailnet prints the page that enables it, then waits), else its
/// first line.
fn printed(proc: &Proc) -> Option<String> {
    let link = proc
        .out
        .split(pyjson::py_space)
        .chain(proc.err_before.split(pyjson::py_space))
        .find(|word| word.starts_with("https://"));
    if let Some(link) = link {
        return Some(link.to_owned());
    }
    [&proc.out, &proc.err_before]
        .into_iter()
        .map(|text| tailscale::first_line(text))
        .find(|line| !line.is_empty())
}

fn words(items: &[String]) -> Vec<&str> {
    items.iter().map(String::as_str).collect()
}

fn serve_words(port: u16) -> Vec<String> {
    vec![
        "serve".to_owned(),
        "--bg".to_owned(),
        format!("--tcp={port}"),
        format!("tcp://127.0.0.1:{port}"),
    ]
}

fn off_words(port: u16) -> Vec<String> {
    vec![
        "serve".to_owned(),
        format!("--tcp={port}"),
        "off".to_owned(),
    ]
}

fn command(words: &[String]) -> Vec<String> {
    std::iter::once("tailscale".to_owned())
        .chain(words.iter().cloned())
        .collect()
}

/// The port's state after a removal, judged by its handler itself: still
/// this daemon's forward is `Ours` even beside Funnel, which
/// [`tailscale::port_state`] reads as someone else's port and would hide
/// the forward behind.
async fn read_owned(
    host: &impl Host,
    binary: &Binary,
    port: u16,
) -> Result<(State, Option<Vec<u16>>), String> {
    tailscale::serve_config(host, binary).await.map(|config| {
        if tailscale::still_forwards(&config, port) {
            (State::Ours, None)
        } else {
            tailscale::port_state(&config, port)
        }
    })
}

/// The port's state read back after a change, or why it could not be.
async fn read_back(
    host: &impl Host,
    binary: &Binary,
    port: u16,
) -> Result<(State, Option<Vec<u16>>), String> {
    tailscale::serve_config(host, binary)
        .await
        .map(|config| tailscale::port_state(&config, port))
}

fn note_interrupt(host: &impl Host, report: &mut Report) {
    if host.interrupted() {
        report.warn_text(
            "interrupted during the change: what follows is the serve state tailscale reports now",
        );
    }
}

/// `serve --bg` on a port that was free, then what the serve state shows.
async fn serve(
    host: &impl Host,
    binary: &Binary,
    port: u16,
    url: &str,
    bank: &J,
    mut report: Report,
) -> Out {
    report.move_failure = None;
    host.hold_interrupts();
    let proc = host.run(binary, &words(&serve_words(port))).await;
    let after = read_back(host, binary, port).await;
    note_interrupt(host, &mut report);
    let (state, detail) = match after {
        Ok(after) => after,
        Err(why) => {
            // Nothing to judge an undo by: removing a serve that may have
            // taken, or reporting an undo that may not have been needed,
            // would both mislead.
            return report.fail_text(
                EXIT_UNDONE,
                &format!(
                    "the serve command ran ({}) but could not verify its result: {why}. Check `tailscale serve status`; if port {port} forwards to 127.0.0.1:{port} and you do not want it, run `pseudolife-mcp expose off --port {port}`",
                    ran_fully(&proc)
                ),
            );
        }
    };
    if proc.settled && state == State::None {
        report.move_failure = Some(if permission_refused(&proc) {
            "permission_denied"
        } else if proc.timed_out
            && printed(&proc)
                .is_some_and(|hint| hint.starts_with("https://login.tailscale.com/f/serve"))
        {
            "serve_not_enabled"
        } else {
            "not_created"
        });
    }
    match (proc.succeeded(), state) {
        (true, State::Ours) => {}
        (_, State::Foreign) => {
            // Not the forward this run asked for: it may be someone else's,
            // so it is named and left alone.
            let line = [
                units(&format!(
                    "after `tailscale serve` ({}), tailnet port {port} serves ",
                    ran(&proc)
                )),
                detail.unwrap_or_default(),
                units(", not the forward this command asked for; left alone. Check `tailscale serve status`"),
            ]
            .concat();
            return report.fail(EXIT_UNDONE, &line);
        }
        (false, State::None) if permission_refused(&proc) => {
            return report.fail_text(EXIT_REFUSED, &permission_message(&proc));
        }
        (false, State::None) if host.interrupted() => {
            return report.fail_text(
                EXIT_INTERRUPTED,
                &format!(
                    "`tailscale serve` was interrupted ({}); nothing changed",
                    ran(&proc)
                ),
            );
        }
        (false, State::None) if proc.timed_out => {
            let said = printed(&proc);
            let step = if said
                .as_deref()
                .is_some_and(|said| said.starts_with("https://"))
            {
                "Open that link to enable Serve for this tailnet"
            } else {
                "Check that Tailscale answers (`tailscale status`)"
            };
            let said = said.map_or_else(String::new, |said| format!(" ({said})"));
            return report.fail_text(
                EXIT_REFUSED,
                &format!(
                    "`tailscale serve` did not finish within {}s{said}; nothing changed. {step}, then rerun `pseudolife-mcp expose tailscale --port {port}`",
                    tailscale::TIMEOUT.as_secs()
                ),
            );
        }
        (false, State::None) => {
            return report.fail_text(
                EXIT_REFUSED,
                &format!(
                                        "tailscale refused the serve ({}); nothing changed: fix what tailscale reports, then rerun this command",
                    ran(&proc)
                ),
            );
        }
        (true, State::None) => {
            return report.fail_text(
                EXIT_UNDONE,
                "the serve did not take (`tailscale serve` exited 0, but the serve status does not show the forward); nothing changed. Check `tailscale serve status`, then rerun",
            );
        }
        (false, State::Ours) => return undo(host, binary, port, &proc, report).await,
    }
    report.state = Some("exposed");
    report.url = Some(url.to_owned());
    report.say_text(&format!("exposed: clients use {url}"));
    probe(host, &mut report, url, bank).await;
    report.note(format!(
        "next, for each joining machine: run `pseudolife-mcp invite <machine>` here, then give the code it prints to that machine's installer (option 2), or run `pseudolife-mcp pair {url} <code>` there"
    ));
    report.note(format!(
        "to remove the forward: pseudolife-mcp expose off --port {port}"
    ));
    report.finish(EXIT_OK)
}

/// The serve reported failure but its forward is in place: the port was
/// free before, so the forward is this run's; take it back off and check.
async fn undo(
    host: &impl Host,
    binary: &Binary,
    port: u16,
    proc: &Proc,
    mut report: Report,
) -> Out {
    let removal = host.run(binary, &words(&off_words(port))).await;
    let left = match read_owned(host, binary, port).await {
        Ok((State::None, _)) => None,
        Ok((State::Foreign, detail)) => {
            // Something else is on the port now: not this run's to remove.
            let line = [
                units(&format!(
                    "`tailscale serve` failed ({}) although the forward appeared; after removing it, tailnet port {port} serves ",
                    ran(proc)
                )),
                detail.unwrap_or_default(),
                units(", which this command did not create; left alone. Check `tailscale serve status`"),
            ]
            .concat();
            return report.fail(EXIT_UNDONE, &line);
        }
        Ok((_, _)) => Some(format!("port {port} still forwards to 127.0.0.1:{port}")),
        Err(why) => Some(why),
    };
    match left {
        None => {
            if proc.settled && removal.settled {
                report.move_failure = Some("not_created");
            }
            report.fail_text(
            EXIT_UNDONE,
            &format!(
                "`tailscale serve` failed ({}) although the forward appeared; it was removed again, so nothing changed",
                ran(proc)
            ),
            )
        }
        Some(why) => {
            let why = if removal.succeeded() {
                why
            } else {
                format!("{}; {why}", ran(&removal))
            };
            report.fail_text(
                EXIT_UNDONE,
                &format!(
                    "`tailscale serve` failed ({}) although the forward appeared, and removing it again did not finish ({why}): run `tailscale serve --tcp={port} off` yourself",
                    ran(proc)
                ),
            )
        }
    }
}

/// `_probe`: the advisory check through the tailnet address. Never rolls
/// back: a host cannot always reach its own serve address.
async fn probe(host: &impl Host, report: &mut Report, url: &str, bank: &J) {
    let Health::Ok(remote) = host.health(url, TAILNET_HEALTH_TIMEOUT).await else {
        report.probe = Some(J::Dict(vec![
            (J::from("reachable"), J::Bool(false)),
            (J::from("same_bank"), J::Null),
        ]));
        report.warn_text(&format!(
            "this host could not reach {url}/health through the tailnet, which is common for a host's own serve address; check from another machine: curl {url}/health"
        ));
        return;
    };
    let other = field(&remote, "bank");
    let same = (bank.truthy() && other.truthy()).then(|| other == *bank);
    report.probe = Some(J::Dict(vec![
        (J::from("reachable"), J::Bool(true)),
        (J::from("same_bank"), same.map_or(J::Null, J::Bool)),
    ]));
    match same {
        Some(true) => report.say(
            &[
                units(&format!(
                    "checked: {url}/health answers with the same bank ("
                )),
                bank.py_str_units(),
                units(")"),
            ]
            .concat(),
        ),
        Some(false) => report.warn(
            &[
                units(&format!("{url}/health answers with a different bank (")),
                other.py_str_units(),
                units(", this daemon has "),
                bank.py_str_units(),
                units("); something else may be serving that address"),
            ]
            .concat(),
        ),
        None => report.say_text(&format!(
            "checked: {url}/health answers (bank fingerprint not reported yet)"
        )),
    }
}

/// `serve --tcp=N off` on this daemon's forward, then what the serve state
/// shows.
async fn remove(host: &impl Host, binary: &Binary, port: u16, mut report: Report) -> Out {
    host.hold_interrupts();
    let proc = host.run(binary, &words(&off_words(port))).await;
    let after = read_owned(host, binary, port).await;
    note_interrupt(host, &mut report);
    match after {
        Err(why) => report.fail_text(
            EXIT_UNDONE,
            &format!(
                "the removal ran ({}) but could not verify its result: {why}. Check `tailscale serve status`; if port {port} still forwards to 127.0.0.1:{port}, run `tailscale serve --tcp={port} off` yourself",
                ran_fully(&proc)
            ),
        ),
        Ok((State::Ours, _)) if permission_refused(&proc) => {
            report.fail_text(EXIT_REFUSED, &permission_message(&proc))
        }
        Ok((State::Ours, _)) => report.fail_text(
            EXIT_REFUSED,
            &format!(
                "tailscale did not remove the forward ({}); it is still in place, so nothing changed: check `tailscale serve status`, then run the plan's `tailscale serve` command yourself",
                ran(&proc)
            ),
        ),
        Ok((state, detail)) => {
            report.state = Some("removed");
            if state == State::Foreign {
                report.say_text(&format!(
                    "removed: this daemon's forward on tailnet port {port}"
                ));
            } else {
                report.say_text(&format!(
                    "removed: tailnet port {port} no longer forwards to this daemon"
                ));
            }
            if state == State::Foreign {
                report.warn(
                    &[
                        units(&format!("tailnet port {port} now serves ")),
                        detail.unwrap_or_default(),
                        units(", which this command did not create; left alone"),
                    ]
                    .concat(),
                );
            }
            report.finish(EXIT_OK)
        }
    }
}

// ── the actions ──────────────────────────────────────────────────────────────

/// `_running` then `_serve_config`, as the actions call them in one `try`.
async fn running_and_config(host: &impl Host, binary: &Binary) -> Result<(Dict, Dict), String> {
    let status = tailscale::running(host, binary).await?;
    Ok((status, tailscale::serve_config(host, binary).await?))
}

/// `_expose(args, report)`.
async fn expose(host: &impl Host, args: &Args, mut report: Report) -> Out {
    let port = args.port;
    let local = format!("http://127.0.0.1:{port}");
    let health = match host.health(&local, LOCAL_HEALTH_TIMEOUT).await {
        Health::Other => {
            return report.fail_text(
                EXIT_REFUSED,
                &format!(
                    "something that is not the Pseudolife daemon answers on 127.0.0.1:{port} (a redirect or a body that is not its /health JSON); pass --port for the daemon's port"
                ),
            );
        }
        Health::None => {
            return report.fail_text(
                EXIT_REFUSED,
                &format!("no daemon answers at {local}/health: start it first (or pass --port)"),
            );
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
        return report.fail_text(
            EXIT_REFUSED,
            &format!("the daemon at {local} reports status {shown}: fix it before exposing it"),
        );
    }
    if pyjson::get(&health, "auth") != Some(&J::Bool(true)) {
        return report.fail_text(
            EXIT_REFUSED,
            &format!(
                "the daemon at {local} does not report \"auth\": true, so anyone on the tailnet could read and write the bank. Give it a token first (PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKENS, see docs/guide/remote-bank.md); an unauthenticated bank is never exposed"
            ),
        );
    }
    let Some(binary) = host.find() else {
        return report.fail_text(
            EXIT_REFUSED,
            &format!("the tailscale CLI was not found: {INSTALL_FIX}"),
        );
    };
    report.tailscale = Some(binary.shown.clone());
    let (status, config) = match running_and_config(host, &binary).await {
        Ok(both) => both,
        Err(why) => return report.fail_text(EXIT_REFUSED, &why),
    };
    let (state, detail) = tailscale::port_state(&config, port);
    if state == State::Foreign {
        let line = [
            units(&format!("tailnet port {port} already serves ")),
            detail.unwrap_or_default(),
            units("; expose never replaces another serve. Remove it yourself, or pass --port for the daemon's port if this is not it"),
        ]
        .concat();
        return report.fail(EXIT_REFUSED, &line);
    }
    let Some(ip) = tailscale::tailnet_ipv4(host, &binary, &status).await else {
        return report.fail_text(
            EXIT_REFUSED,
            "tailscale reports no tailnet IPv4 address for this host",
        );
    };
    let url = format!("http://{ip}:{port}");
    if state == State::Ours {
        report.state = Some("already_exposed");
        report.url = Some(url.clone());
        report.say_text(&format!("already exposed: clients use {url}"));
        return report.finish(EXIT_OK);
    }
    let serve_args = serve_words(port);
    report.command = Some(command(&serve_args));
    report.say_text("plan:");
    report.say_text(&format!("  run      tailscale {}", serve_args.join(" ")));
    report.say_text(&format!("  clients  {url}"));
    let question = format!("expose this daemon on the tailnet at {url}? [y/N] ");
    let report = match consent(
        host,
        args,
        report,
        &question,
        (&binary, State::None, Some(&url)),
    )
    .await
    {
        Ok(report) => report,
        Err(out) => return out,
    };
    serve(host, &binary, port, &url, &field(&health, "bank"), report).await
}

/// `_off(args, report)`.
async fn off(host: &impl Host, args: &Args, mut report: Report) -> Out {
    let port = args.port;
    let Some(binary) = host.find() else {
        return report.fail_text(
            EXIT_REFUSED,
            &format!("the tailscale CLI was not found: {INSTALL_FIX}"),
        );
    };
    report.tailscale = Some(binary.shown.clone());
    let (_status, config) = match running_and_config(host, &binary).await {
        Ok(both) => both,
        Err(why) => return report.fail_text(EXIT_REFUSED, &why),
    };
    match tailscale::port_state(&config, port) {
        (State::None, _) => {
            report.state = Some("not_exposed");
            report.say_text(&format!("not exposed: nothing serves tailnet port {port}"));
            report.finish(EXIT_OK)
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
            report.fail(EXIT_REFUSED, &line)
        }
        (State::Ours, _) => {
            let off_args = off_words(port);
            report.command = Some(command(&off_args));
            report.say_text("plan:");
            report.say_text(&format!("  run      tailscale {}", off_args.join(" ")));
            let question = format!("remove the tailnet forward on port {port}? [y/N] ");
            let report =
                match consent(host, args, report, &question, (&binary, State::Ours, None)).await {
                    Ok(report) => report,
                    Err(out) => return out,
                };
            remove(host, &binary, port, report).await
        }
    }
}

/// `_status(args, report)`: reads only.
async fn status(host: &impl Host, args: &Args, mut report: Report) -> Out {
    let port = args.port;
    let Some(binary) = host.find() else {
        report.state = Some("unavailable");
        report.detail = Some(J::from("the tailscale CLI was not found"));
        report.say_text("not exposed: the tailscale CLI was not found");
        return report.finish(EXIT_OK);
    };
    report.tailscale = Some(binary.shown.clone());
    let (status, config) = match running_and_config(host, &binary).await {
        Ok(both) => both,
        Err(why) => {
            report.state = Some("unavailable");
            report.detail = Some(J::Str(why.clone()));
            report.say_text(&format!("not exposed: {why}"));
            return report.finish(EXIT_OK);
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
            report.finish(EXIT_OK)
        }
        (State::None, _) => {
            report.state = Some("not_exposed");
            report.say_text("not exposed");
            report.finish(EXIT_OK)
        }
        (State::Ours, _) => {
            let Some(ip) = tailscale::tailnet_ipv4(host, &binary, &status).await else {
                report.state = Some("unavailable");
                report.detail = Some(J::from("no tailnet IPv4 address for this host"));
                report.say_text(
                    "not exposed: tailscale reports no tailnet IPv4 address for this host",
                );
                return report.finish(EXIT_OK);
            };
            let url = format!("http://{ip}:{port}");
            report.state = Some("exposed");
            report.url = Some(url.clone());
            report.say_text(&format!("exposed: clients use {url}"));
            report.finish(EXIT_OK)
        }
    }
}

/// Parse before any host access, then run the selected action.
fn decide(argv: &[String], host: &impl Host) -> Out {
    let args = match args::parse(argv) {
        // The help and usage text are argparse's at 80 columns, whatever
        // the terminal's width.
        Parsed::MainHelp => {
            return Out {
                stdout: units(args::MAIN_HELP),
                stderr: String::new(),
                code: EXIT_OK,
            };
        }
        Parsed::Help(action) => {
            return Out {
                stdout: units(&action.help()),
                stderr: String::new(),
                code: EXIT_OK,
            };
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
            return Out {
                stdout: Vec::new(),
                stderr: format!("{usage}{prog}: error: {message}\n"),
                code: EXIT_USAGE,
            };
        }
        Parsed::Args(args) => args,
    };
    let report = Report::new(&args);
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            return report.fail_text(
                EXIT_FAILED,
                &format!("could not start ({error}); nothing changed"),
            );
        }
    };
    let out = runtime.block_on(async {
        match args.action {
            Action::Tailscale => expose(host, &args, report).await,
            Action::Off => off(host, &args, report).await,
            Action::Status => status(host, &args, report).await,
        }
    });
    // A question's stdin read may still be pending after an interrupt; the
    // process ends now rather than waiting for its line.
    runtime.shutdown_background();
    out
}

/// Every invocation is answered natively.
pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    let out = match values.into_iter().map(OsString::into_string).collect::<Result<Vec<_>, _>>() {
        Ok(argv) => decide(&argv, &Real::default()),
        Err(_) => Out {
            stdout: Vec::new(),
            stderr: "pseudolife-mcp expose: error: argument is not valid Unicode; retype it as Unicode text; nothing changed\n".into(),
            code: EXIT_USAGE,
        },
    };
    let (stdout, stderr) = encode(&out.stdout, &out.stderr);
    let wrote_out =
        stdout.is_empty() || super::move_cli::emit(&mut io::stdout().lock(), &stdout).is_ok();
    let wrote_err =
        stderr.is_empty() || super::move_cli::emit(&mut io::stderr().lock(), &stderr).is_ok();
    Some(if wrote_out && wrote_err {
        ExitCode::from(out.code)
    } else {
        ExitCode::from(EXIT_FAILED)
    })
}

#[cfg(test)]
mod tests;
