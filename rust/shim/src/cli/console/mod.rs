//! `console`: `pseudolife-mcp console`, every shape the oracle answers.
//!
//! Oracle: `pseudolife_memory/console_cli.py` (`main`, `credentials`,
//! `request`, `_refusal`, `_list`, `_opened`, `open_browser`,
//! `has_display`) with CPython 3.11's `webbrowser`. Answered natively: help
//! and argparse's errors (at `COLUMNS=80`), `--list [--json]`, `--revoke ID
//! [--json]`, the sign-in (`[--no-open] [--to PAGE] [--json]
//! [--unless-signed-in]`, an empty `--revoke` included) and every refusal
//! those reach. Which browser the sign-in would start is decided before the
//! code is minted (`browser.rs`), so a launcher this port does not
//! reproduce defers before any effect; once the daemon answers the sign-in
//! with a 200 object the command answers every branch itself, an oracle
//! crash included (its terminal exception line on stderr, exit 1).
//! Anything else outside the reproduced domain that can be decided before a
//! request defers before it
//! (`rust/cli_harness/rows/console.md`).
mod args;
mod browser;
mod launch;
mod pyint;
mod stamp;

// urllib.parse.urlsplit (CPython 3.11) for printable ASCII.
use super::pairing::url;

use super::hook_http::{self, NoReply};
use super::pyjson::{self, Dict, J};
use std::{ffi::OsString, io, process::ExitCode, time::Duration};

/// Outside the reproduced domain: the dispatcher's deferral line.
#[derive(Debug)]
struct Defer;
type Res<T> = Result<T, Defer>;

const EXIT_OK: u8 = 0;
const EXIT_USAGE: u8 = 2;
const EXIT_NO_DAEMON: u8 = 3;
const EXIT_REFUSED: u8 = 4;
/// `console_cli.TIMEOUT_S`.
const TIMEOUT: Duration = Duration::from_secs(10);
/// `console_cli.MAX_RESPONSE_BYTES`: `response.read(n)` and `exc.read(n)`.
const MAX_RESPONSE_BYTES: usize = 262_144;
/// Answers nested deeper defer. The pairing commands' read paths, measured
/// on CPython 3.11.9 through the oracle CLI, raise RecursionError past 987
/// and 985 levels (`pyjson::PAIR_DEPTH`, `INVITE_DEPTH`); console's was not
/// measured, and the answer is later re-dumped or `repr`-ed, so this stays
/// far inside any stage's limit.
const DEPTH: usize = 500;

const URL_KEY: &str = "PSEUDOLIFE_MCP_DAEMON_URL";
const FILE_KEY: &str = "PSEUDOLIFE_MCP_TOKEN_FILE";
const TOKEN_KEY: &str = "PSEUDOLIFE_MCP_TOKEN";

const BAD_URL: &str =
    "PSEUDOLIFE_MCP_DAEMON_URL is not an http(s) address (scheme, host and optional port only)";
const NO_TOKEN: &str = "no token found: set PSEUDOLIFE_MCP_TOKEN_FILE (or PSEUDOLIFE_MCP_TOKEN), or run this on a machine where Claude Code or Codex already uses Pseudolife";
/// `CredentialError` texts whose Rust and Python raising conditions the
/// harness row compares; any other credential failure defers.
const CREDENTIAL_ERRORS: [&str; 3] = [
    "configured credential file is missing",
    "configured credential file path is empty",
    "credential file contains an invalid bearer token",
];
const REFUSALS: [(&str, Option<&str>); 6] = [
    ("signin_origin_unsupported", None),
    ("signin_unavailable", None),
    ("session_cannot_issue", None),
    ("operator_principal_required", None),
    (
        "unauthorized",
        Some(
            "the daemon refused this machine's token: check that PSEUDOLIFE_MCP_TOKEN_FILE (or PSEUDOLIFE_MCP_TOKEN) holds the token this machine's clients use; a paired machine whose token was revoked pairs again with `pseudolife-mcp pair`",
        ),
    ),
    (
        "principals_unavailable",
        Some(
            "the daemon cannot check tokens right now (its bank is unreachable); try again in a minute",
        ),
    ),
];
const NO_LINK: &str = "pseudolife-mcp console: the daemon's answer had no sign-in link";

/// Python `repr()` of a str.
fn py_repr(text: &str) -> String {
    pyjson::py_value_repr(&J::Str(text.to_owned()))
}

/// What the oracle's argument handling depends on besides argv.
struct Context {
    /// argparse wraps help and usage at `COLUMNS - 2`; only 80 is reproduced.
    columns_80: bool,
    /// argparse could colour what it writes (newer CPython).
    colour_stdout: bool,
    colour_stderr: bool,
    /// CPython decodes this argv as UTF-8.
    utf8_argv: bool,
}

/// One answer: the text one `print` (or several) writes, and the exit code.
#[derive(Debug, PartialEq, Eq)]
struct Out {
    stdout: bool,
    text: String,
    code: u8,
    /// An oracle crash after `text` (on stdout): the terminal
    /// `ExceptionType: message` line it writes to stderr (exit 1). Only its
    /// traceback header and frames are not reproduced
    /// (`python-traceback-not-contract`).
    crash: Option<String>,
}

impl Out {
    fn stdout(text: String, code: u8) -> Self {
        Self {
            stdout: true,
            text,
            code,
            crash: None,
        }
    }

    fn stderr(text: String, code: u8) -> Self {
        Self {
            stdout: false,
            text,
            code,
            crash: None,
        }
    }

    /// What reached stdout, then the uncaught exception.
    fn crashed(text: String, line: String) -> Self {
        Self {
            stdout: true,
            text,
            code: 1,
            crash: Some(line),
        }
    }
}

/// `print(text, file=stdout)`: strict UTF-8 refuses a lone surrogate (the
/// oracle raises), so that answer defers.
fn stdout_text(units: &[u16]) -> Res<String> {
    String::from_utf16(units).map_err(|_| Defer)
}

/// `print(text, file=sys.stderr)`: stderr writes a lone surrogate as `\uXXXX`.
fn stderr_text(units: &[u16]) -> String {
    pyjson::backslashreplace(units)
}

fn units(text: &str) -> Vec<u16> {
    text.encode_utf16().collect()
}

fn dict(items: Vec<(&str, J)>) -> J {
    J::Dict(
        items
            .into_iter()
            .map(|(key, value)| (J::from(key), value))
            .collect(),
    )
}

/// `_emit`: one JSON object on stdout, else the text on stdout (exit 0) or
/// stderr.
fn emit(out: J, as_json: bool, text: &[u16], code: u8) -> Res<Out> {
    if as_json {
        return Ok(Out::stdout(pyjson::dumps_line(&out) + "\n", code));
    }
    Ok(if code == 0 {
        Out::stdout(stdout_text(text)? + "\n", code)
    } else {
        Out::stderr(stderr_text(text) + "\n", code)
    })
}

/// `d.get(key)` keeping a JSON null (Python's `None` value) apart from an
/// absent key: `str(row.get("session_id", ""))` prints `None` for one and
/// nothing for the other.
fn raw<'a>(dict: &'a Dict, key: &str) -> Option<&'a J> {
    dict.iter()
        .find(|(name, _)| name.as_str() == Some(key))
        .map(|(_, value)| value)
}

fn or_null(value: Option<&J>) -> J {
    value.cloned().unwrap_or(J::Null)
}

/// `console_sessions.valid_route_id`: `[a-z][a-z0-9-]{0,31}`, whole.
fn valid_route_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 32
        && bytes[0].is_ascii_lowercase()
        && bytes[1..]
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
}

/// `console_sessions.normalize_origin` for a str.
fn normalize_origin(value: &str) -> Res<Option<String>> {
    if value.is_empty() || value.chars().count() > 255 {
        return Ok(None);
    }
    if value.chars().any(|c| (c as u32) < 33 || c as u32 == 127) {
        return Ok(None);
    }
    let parts = match url::urlsplit(value).map_err(|_| Defer)? {
        Ok(parts) => parts,
        Err(()) => return Ok(None),
    };
    let Ok(port) = parts.port() else {
        return Ok(None);
    };
    let scheme = parts.scheme.as_str();
    if !matches!(scheme, "http" | "https") {
        return Ok(None);
    }
    // `parts.username` is not None exactly when the netloc holds an '@'.
    if parts.netloc.contains('@') {
        return Ok(None);
    }
    if !matches!(parts.path.as_str(), "" | "/")
        || !parts.query.is_empty()
        || !parts.fragment.is_empty()
        || value.contains('#')
    {
        return Ok(None);
    }
    let Some(mut host) = parts.hostname() else {
        return Ok(None);
    };
    if host.contains(':') {
        host = format!("[{host}]");
    }
    let default = if scheme == "http" { 80 } else { 443 };
    Ok(Some(match port.filter(|port| *port != default) {
        Some(port) => format!("{scheme}://{host}:{port}"),
        None => format!("{scheme}://{host}"),
    }))
}

/// A bearer every platform sends alike (doctor's rule): printable ASCII.
fn sendable(token: &str) -> bool {
    token.bytes().all(|byte| (0x20..0x7f).contains(&byte))
}

/// `credentials()`: `Ok(Ok((origin, token)))`, or `Ok(Err(why))` for the
/// `no_credentials` answer.
fn credentials() -> Res<Result<(String, String), String>> {
    let (overrides, _label) = super::doctor::registration_credentials().ok_or(Defer)?;
    let lookup = |key: &str| -> Res<Option<String>> {
        if let Some((_, value)) = overrides.iter().find(|(name, _)| name == key) {
            return Ok(Some(value.clone()));
        }
        match std::env::var_os(key) {
            None => Ok(None),
            Some(value) => value.into_string().map(Some).map_err(|_| Defer),
        }
    };
    let address = lookup(URL_KEY)?
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| crate::daemon_url::DEFAULT_URL.to_owned());
    let Some(origin) = normalize_origin(&address)? else {
        return Ok(Err(BAD_URL.to_owned()));
    };
    let unusable = |error: crate::credentials::CredentialError| -> Res<String> {
        if CREDENTIAL_ERRORS.contains(&error.0) {
            Ok(format!("this machine's token is unusable ({error})"))
        } else {
            Err(Defer)
        }
    };
    let provider = match lookup(FILE_KEY)? {
        Some(path) => crate::credentials::CredentialProvider::new(None, Some(path.into())),
        None => crate::credentials::CredentialProvider::new(
            lookup(TOKEN_KEY)?.filter(|value| !value.is_empty()),
            None,
        ),
    };
    let snapshot = match provider.and_then(|provider| provider.snapshot()) {
        Ok(snapshot) => snapshot,
        Err(error) => return Ok(Err(unusable(error)?)),
    };
    match snapshot.token().filter(|token| !token.is_empty()) {
        None => Ok(Err(NO_TOKEN.to_owned())),
        Some(token) if sendable(token) => Ok(Ok((origin, token.to_owned()))),
        Some(_) => Err(Defer),
    }
}

/// Which daemon call `main` makes.
enum Call {
    List,
    Revoke(String),
    /// The sign-in: the `--to` page, whether a browser may open (no
    /// `--no-open`), and `--unless-signed-in`.
    Link {
        to: Option<String>,
        open: bool,
        unless: bool,
    },
}

/// Everything decided before the first request: an answer, or the call.
enum Step {
    Done(Out),
    Call {
        call: Call,
        origin: String,
        token: String,
        json: bool,
    },
}

/// argparse and `main`'s argument checks: an answer, or the call to make
/// and whether `--json` was given. Reads only argv and `context`.
fn plan(argv: &[String], context: &Context) -> Res<Result<Out, (Call, bool)>> {
    if !context.utf8_argv && !argv.iter().all(|argument| argument.is_ascii()) {
        return Err(Defer);
    }
    let args = match args::parse(argv) {
        args::Parsed::Defer => return Err(Defer),
        args::Parsed::Help if context.columns_80 && !context.colour_stdout => {
            return Ok(Ok(Out::stdout(args::HELP.to_owned(), EXIT_OK)));
        }
        args::Parsed::Error(message) if context.columns_80 && !context.colour_stderr => {
            return Ok(Ok(Out::stderr(
                format!(
                    "{}pseudolife-mcp console: error: {message}\n",
                    args::usage()
                ),
                EXIT_USAGE,
            )));
        }
        args::Parsed::Help | args::Parsed::Error(_) => return Err(Defer),
        args::Parsed::Args(args) => args,
    };
    // `args.revoke` is tested for truth: an empty id is no revoke.
    let revoke = args.revoke.clone().filter(|id| !id.is_empty());
    if args.list && revoke.is_some() {
        return Ok(Ok(Out::stderr(
            "--list and --revoke are separate commands; give one\n".to_owned(),
            EXIT_USAGE,
        )));
    }
    if let Some(to) = &args.to
        && !valid_route_id(to)
    {
        return Ok(Ok(Out::stderr(
            format!(
                "--to takes a Console page id such as board, not {}\n",
                py_repr(to)
            ),
            EXIT_USAGE,
        )));
    }
    let call = if args.list {
        Call::List
    } else if let Some(id) = revoke {
        Call::Revoke(id)
    } else {
        // `elif args.revoke:` is false for an empty id: the sign-in path.
        Call::Link {
            to: args.to,
            open: !args.no_open,
            unless: args.unless_signed_in,
        }
    };
    Ok(Err((call, args.json)))
}

/// [`plan`], then `credentials()`.
fn prepare(argv: &[String], context: &Context) -> Res<Step> {
    let (call, json) = match plan(argv, context)? {
        Ok(out) => return Ok(Step::Done(out)),
        Err(call) => call,
    };
    Ok(match credentials()? {
        Err(why) => Step::Done(emit(
            dict(vec![
                ("state", J::from("no_credentials")),
                ("error", J::Str(why.clone())),
            ]),
            json,
            &units(&format!("pseudolife-mcp console: {why}")),
            EXIT_NO_DAEMON,
        )?),
        Ok((origin, token)) => Step::Call {
            call,
            origin,
            token,
            json,
        },
    })
}

/// The refused connect's error code (`ConnectionRefusedError`): the one
/// connect failure the harness row compares. WSAECONNREFUSED on Windows.
#[cfg(windows)]
const REFUSED: i32 = 10061;
#[cfg(not(windows))]
const REFUSED: i32 = libc::ECONNREFUSED;

/// `str(exc.reason)` for a refused connect: CPython's `OSError` text. Any
/// other connect failure (a timeout, an unreachable network) is not
/// compared, so it defers; no request was sent.
fn reason(error: Option<&io::Error>) -> Res<String> {
    let error = error.ok_or(Defer)?;
    let code = error
        .raw_os_error()
        .filter(|code| *code == REFUSED)
        .ok_or(Defer)?;
    let text = io::Error::from_raw_os_error(code).to_string();
    let message = text
        .strip_suffix(&format!(" (os error {code})"))
        .ok_or(Defer)?;
    Ok(if cfg!(windows) {
        // PyErr_SetFromWindowsErr drops trailing whitespace and full stops.
        let message = message.trim_end_matches(|c: char| c <= ' ' || c == '.');
        format!("[WinError {code}] {message}")
    } else {
        format!("[Errno {code}] {message}")
    })
}

/// `request()`: `Ok(Ok((status, payload)))`, or `Ok(Err(reason))` when no
/// HTTP answer arrived.
async fn request(
    origin: &str,
    target: &str,
    token: &str,
    body: Option<&J>,
) -> Res<Result<(u16, Option<Dict>), String>> {
    let data = body.map(pyjson::dumps_line);
    let authorization = format!("Bearer {token}");
    let reply = match hook_http::call_json(
        origin,
        target,
        Some(authorization.as_bytes()),
        data.as_deref().map(str::as_bytes),
        TIMEOUT,
        (Some(MAX_RESPONSE_BYTES), Some(MAX_RESPONSE_BYTES)),
    )
    .await
    {
        Ok(reply) => reply,
        Err(NoReply::Connect(error)) => return Ok(Err(reason(error.as_ref())?)),
        Err(NoReply::Other) => return Err(Defer),
    };
    let success = (200..300).contains(&reply.status);
    // A 2xx body that fails to read raises out of `request`; an HTTPError's
    // `exc.read` failure is caught and reads as no body.
    if success && reply.body_failed {
        return Err(Defer);
    }
    let body = if reply.body_failed {
        Vec::new()
    } else {
        reply.body
    };
    let payload = match std::str::from_utf8(&body) {
        Err(_) => None,
        Ok(text) => match pyjson::loads(text, DEPTH) {
            Ok(J::Dict(dict)) => Some(dict),
            Ok(_) | Err(pyjson::Refused::NotJson) => None,
            Err(pyjson::Refused::TooDeep) => return Err(Defer),
        },
    };
    Ok(Ok((reply.status, payload)))
}

/// `_refusal(status, payload)`, as the value `_emit` writes (`detail` keeps
/// a hint's JSON type; the text is its `str()`).
fn refusal(status: u16, payload: Option<&Dict>) -> Res<J> {
    let code = payload.and_then(|payload| pyjson::get(payload, "error"));
    let hint = payload.and_then(|payload| pyjson::get(payload, "hint"));
    // `code in _REFUSALS` raises TypeError for a list or dict.
    if matches!(code, Some(J::List(_) | J::Dict(_))) {
        return Err(Defer);
    }
    let truthy = |value: Option<&J>| value.filter(|value| value.truthy()).cloned();
    if let Some(name) = code.and_then(J::as_str)
        && let Some((_, message)) = REFUSALS.iter().find(|(key, _)| *key == name)
    {
        return Ok(match message {
            Some(message) => J::from(*message),
            None => truthy(hint).unwrap_or_else(|| code.cloned().unwrap_or(J::Null)),
        });
    }
    if matches!(status, 404 | 405) {
        return Ok(J::Str(format!(
            "this daemon does not offer Console sign-in yet (HTTP {status}): update it with `pseudolife-mcp update`, or open the Console and paste a token under Advanced"
        )));
    }
    if let Some(hint) = truthy(hint) {
        return Ok(hint);
    }
    let mut text = units(&format!("the daemon answered HTTP {status}"));
    if let Some(code) = truthy(code) {
        text.extend(units(" ("));
        text.extend(code.py_str_units());
        text.extend(units(")"));
    }
    Ok(pyjson::text(text))
}

/// `str(value)` padded as `format(value, "<width")` pads: by code point.
fn padded(mut text: Vec<u16>, width: usize) -> Vec<u16> {
    let points = char::decode_utf16(text.iter().copied()).count();
    text.extend(std::iter::repeat_n(
        u16::from(b' '),
        width.saturating_sub(points),
    ));
    text
}

/// `_stamp(value)`.
fn stamp_cell(value: Option<&J>) -> Res<String> {
    let seconds = match value {
        Some(J::Bool(flag)) => i64::from(*flag),
        Some(J::Int(text)) => text.parse::<i64>().map_err(|_| Defer)?,
        Some(J::Float(value)) => {
            // time.localtime floors a float; NaN and the infinities raise.
            if !value.is_finite() || value.abs() > 1e15 {
                return Err(Defer);
            }
            value.floor() as i64
        }
        _ => return Ok("-".to_owned()),
    };
    stamp::minute(seconds).ok_or(Defer)
}

/// `_list(payload, as_json)`.
fn listed(payload: &Dict, as_json: bool) -> Res<Out> {
    let rows = match raw(payload, "sessions") {
        Some(J::List(rows)) => rows.clone(),
        _ => Vec::new(),
    };
    if as_json {
        let out = dict(vec![
            ("state", J::from("listed")),
            ("sessions", J::List(rows)),
            ("operator", or_null(pyjson::get(payload, "operator"))),
        ]);
        return Ok(Out::stdout(pyjson::dumps_line(&out) + "\n", EXIT_OK));
    }
    if rows.is_empty() {
        return Ok(Out::stdout(
            "No browsers are signed in. `pseudolife-mcp console` signs one in.\n".to_owned(),
            EXIT_OK,
        ));
    }
    let mut text = units(&format!(
        "{:<17} {:<20} {:<8} {:<17} {:<17} BROWSER\n",
        "ID", "SIGNED IN AS", "HOW", "SINCE", "LAST USED"
    ));
    for row in &rows {
        let J::Dict(row) = row else {
            continue;
        };
        let cell = |key: &str| raw(row, key).map(J::py_str_units).unwrap_or_default();
        let label = match raw(row, "label") {
            Some(value) if value.truthy() => value.py_str_units(),
            _ => units("-"),
        };
        for (piece, width) in [
            (cell("session_id"), 17),
            (cell("principal"), 20),
            (cell("method"), 8),
            (units(&stamp_cell(raw(row, "created_at"))?), 17),
            (units(&stamp_cell(raw(row, "last_used_at"))?), 17),
        ] {
            text.extend(padded(piece, width));
            text.push(u16::from(b' '));
        }
        text.extend(label);
        text.push(u16::from(b'\n'));
    }
    if raw(payload, "operator") == Some(&J::Bool(false)) {
        text.extend(units(
            "(Only your own sign-ins are shown: this machine's token is a paired one.)\n",
        ));
    }
    Ok(Out::stdout(stdout_text(&text)?, EXIT_OK))
}

/// `link_is_safe(url, origin)`. An origin this port cannot split (one
/// holding a non-ASCII character or a lone surrogate) and a URL holding a
/// lone surrogate read as unsafe: the link is refused where the oracle
/// might accept it (a declared, fail-safe divergence; only a daemon that
/// does not echo the request's ASCII origin answers so).
fn link_is_safe(url: Option<&J>, origin: Option<&J>) -> bool {
    let origin = match origin {
        Some(J::Str(origin)) => normalize_origin(origin).ok().flatten(),
        _ => None,
    };
    let Some(origin) = origin else {
        return false;
    };
    match url {
        Some(J::Str(url)) => {
            !url.chars().any(|c| (c as u32) < 33 || c as u32 == 127)
                && url.starts_with(&format!("{origin}/ui/#/signin?code="))
        }
        _ => false,
    }
}

/// `print(line)` to strict UTF-8 stdout: the line and its newline, or the
/// `UnicodeEncodeError` line a lone surrogate raises (positions in code
/// points of the line; a run of surrogates is reported whole).
fn print_line(out: &mut String, line: &[u16]) -> Result<(), String> {
    let items: Vec<Result<char, u16>> = char::decode_utf16(line.iter().copied())
        .map(|item| item.map_err(|error| error.unpaired_surrogate()))
        .collect();
    if let Some(start) = items.iter().position(Result::is_err) {
        let run = items[start..]
            .iter()
            .take_while(|item| item.is_err())
            .count();
        let what = match items[start] {
            Err(unit) if run == 1 => format!("character '\\u{unit:04x}' in position {start}"),
            _ => format!("characters in position {start}-{}", start + run - 1),
        };
        return Err(format!(
            "UnicodeEncodeError: 'utf-8' codec can't encode {what}: surrogates not allowed"
        ));
    }
    out.extend(items.into_iter().flatten());
    out.push('\n');
    Ok(())
}

/// `_opened(payload, args, environ)`. `launch` is `open_browser` when a
/// browser may open (no `--no-open`, and `has_display`); it runs only once
/// the link has passed `link_is_safe`, before anything is printed, as in
/// the oracle.
fn opened(payload: &Dict, as_json: bool, launch: Option<&mut dyn FnMut(&str) -> bool>) -> Out {
    let url = pyjson::get(payload, "url");
    let code = pyjson::get(payload, "code");
    let origin = pyjson::get(payload, "origin");
    let safe = matches!(code, Some(J::Str(_) | J::Wtf(_))) && link_is_safe(url, origin);
    let (true, Some(J::Str(link)), Some(J::Str(at)), Some(code)) = (safe, url, origin, code) else {
        return if as_json {
            Out::stdout(
                "{\"state\": \"refused\", \"error\": \"bad_answer\"}\n".to_owned(),
                EXIT_REFUSED,
            )
        } else {
            Out::stderr(format!("{NO_LINK}\n"), EXIT_REFUSED)
        };
    };
    let opened = match launch {
        Some(launch) => launch(link),
        None => false,
    };
    let expires_in = pyjson::get(payload, "expires_in");
    if as_json {
        let out = dict(vec![
            ("state", J::from(if opened { "opened" } else { "printed" })),
            ("url", J::Str(link.clone())),
            ("code", code.clone()),
            ("origin", J::Str(at.clone())),
            ("principal", or_null(pyjson::get(payload, "principal"))),
            ("expires_in", or_null(expires_in)),
        ]);
        return Out::stdout(pyjson::dumps_line(&out) + "\n", EXIT_OK);
    }
    let minutes = match pyint::minutes(expires_in) {
        Ok(minutes) => minutes,
        Err(line) => return Out::crashed(String::new(), line),
    };
    let lead = if opened {
        "Opened the Console in your browser: confirm the sign-in there."
    } else {
        "Open this link to sign in to the Console:"
    };
    let mut code_line = units("Or type the code ");
    code_line.extend(code.py_str_units());
    code_line.extend(units(&format!(
        " into the Console's sign-in dialog at {at}/ui/. It works once, within {minutes} minutes."
    )));
    let mut text = String::new();
    for line in [units(lead), units(&format!("  {link}")), code_line] {
        if let Err(crash) = print_line(&mut text, &line) {
            return Out::crashed(text, crash);
        }
    }
    Out::stdout(text, EXIT_OK)
}

/// The `no_daemon` answer: no HTTP answer arrived.
fn no_daemon(origin: &str, reason: &str, as_json: bool) -> Res<Out> {
    emit(
        dict(vec![
            ("state", J::from("no_daemon")),
            ("error", J::Str(reason.to_owned())),
        ]),
        as_json,
        &units(&format!(
            "pseudolife-mcp console: no daemon answered at {origin} ({reason}); check it runs (`pseudolife-mcp doctor`)"
        )),
        EXIT_NO_DAEMON,
    )
}

/// What `open_browser` would start, decided before the code is minted.
type Chooser<'a> = &'a dyn Fn() -> Result<browser::Plan, browser::Unsupported>;

/// `main` from the first request on.
async fn answer(
    call: Call,
    origin: &str,
    token: &str,
    as_json: bool,
    browser: Chooser<'_>,
) -> Res<Out> {
    let mut plan = None;
    let (target, body) = match &call {
        Call::List => ("/api/console/sessions", None),
        Call::Revoke(id) => (
            "/api/console/sessions/revoke",
            Some(dict(vec![("session_id", J::Str(id.clone()))])),
        ),
        Call::Link { to, open, unless } => {
            if *unless {
                // A read: whatever defers here comes before any code is minted.
                match request(origin, "/api/console/sessions", token, None).await? {
                    Err(reason) => return no_daemon(origin, &reason, as_json),
                    Ok((200, Some(listed)))
                        if matches!(
                            pyjson::get(&listed, "sessions"),
                            Some(J::List(rows)) if !rows.is_empty()
                        ) =>
                    {
                        return emit(
                            dict(vec![("state", J::from("already_signed_in"))]),
                            as_json,
                            &units(
                                "Console: a browser is already signed in (to sign in another: pseudolife-mcp console).",
                            ),
                            EXIT_OK,
                        );
                    }
                    Ok(_) => {}
                }
            }
            if *open {
                plan = Some(browser().map_err(|_| Defer)?);
            }
            let mut body = vec![("origin", J::Str(origin.to_owned()))];
            if let Some(to) = to {
                body.push(("to", J::Str(to.clone())));
            }
            ("/api/console/link", Some(dict(body)))
        }
    };
    let (status, payload) = match request(origin, target, token, body.as_ref()).await? {
        Ok(answer) => answer,
        Err(reason) => return no_daemon(origin, &reason, as_json),
    };
    let payload = match payload {
        Some(payload) if status == 200 => payload,
        payload => {
            let detail = refusal(status, payload.as_ref())?;
            let mut text = units("pseudolife-mcp console: ");
            text.extend(detail.py_str_units());
            let error = or_null(
                payload
                    .as_ref()
                    .and_then(|payload| pyjson::get(payload, "error")),
            );
            return emit(
                dict(vec![
                    ("state", J::from("refused")),
                    ("status", J::Int(status.to_string())),
                    ("error", error),
                    ("detail", detail),
                ]),
                as_json,
                &text,
                EXIT_REFUSED,
            );
        }
    };
    match call {
        Call::List => listed(&payload, as_json),
        Call::Revoke(id) => {
            let revoked = pyjson::get(&payload, "revoked").is_some_and(J::truthy);
            let (state, text, code) = if revoked {
                ("revoked", format!("Signed out {id}."), EXIT_OK)
            } else {
                (
                    "not_found",
                    format!("No signed-in browser {id} that you may sign out; see --list."),
                    EXIT_REFUSED,
                )
            };
            emit(
                dict(vec![("state", J::from(state))]),
                as_json,
                &units(&text),
                code,
            )
        }
        Call::Link { .. } => Ok(match plan {
            // `open_browser` returns False without starting anything.
            None | Some(browser::Plan::Refused) => opened(&payload, as_json, None),
            Some(plan) => opened(
                &payload,
                as_json,
                Some(&mut |url: &str| launch::open(&plan, url)),
            ),
        }),
    }
}

fn decide(argv: &[String], context: &Context) -> Res<Out> {
    match prepare(argv, context)? {
        Step::Done(out) => Ok(out),
        Step::Call {
            call,
            origin,
            token,
            json,
        } => tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| Defer)?
            .block_on(answer(call, &origin, &token, json, &|| {
                let host = launch::Machine;
                if browser::has_display(&host, browser::Platform::HERE) {
                    browser::choose(&host, browser::Platform::HERE)
                } else {
                    // `open_browser` is never called: the link is printed.
                    Ok(browser::Plan::Refused)
                }
            })),
    }
}

/// `None` defers to the dispatcher.
pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    use std::io::IsTerminal;
    // json.loads reads integers under this digit limit (4300 unset).
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
    let bytes = super::text_bytes(&out.text);
    let written = if out.stdout {
        super::move_cli::emit(&mut io::stdout().lock(), &bytes)
    } else {
        super::move_cli::emit(&mut io::stderr().lock(), &bytes)
    };
    if let Some(line) = &out.crash {
        // The uncaught exception's last line, after whatever stdout took.
        let line = super::text_bytes(&format!("{line}\n"));
        let _ = super::move_cli::emit(&mut io::stderr().lock(), &line);
    }
    super::move_cli::settle(out.code, written).map(ExitCode::from)
}

#[cfg(test)]
mod tests;
