//! Native Console sign-in, session listing and revocation.
//!
//! Arguments and credentials are checked before any request. A browser is
//! selected before minting a link, and started only after validating the reply.
//! Every result is reported here, including transport and output failures.
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

const EXIT_OK: u8 = 0;
const EXIT_USAGE: u8 = 2;
const EXIT_NO_DAEMON: u8 = 3;
const EXIT_REFUSED: u8 = 4;
/// `console_cli.TIMEOUT_S`.
const TIMEOUT: Duration = Duration::from_secs(10);
/// `console_cli.MAX_RESPONSE_BYTES`: `response.read(n)` and `exc.read(n)`.
const MAX_RESPONSE_BYTES: usize = 262_144;
/// Answers nested deeper are refused. The pairing commands' read paths, measured
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
/// harness row compares.
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
const NO_LINK: &str = "the daemon's answer had no sign-in link";
const MISMATCH: &str = "the daemon's sign-in code does not match its link";
const BAD_EXPIRY: &str = "the daemon's answer had no usable expiry time (expires_in)";
const LIST_TEXT: &str =
    "the daemon's session list had text that cannot be printed (a lone surrogate)";
const EMPTY_REVOKE: &str = "--revoke takes a session id from --list, not an empty one\n";
/// `console_cli._CODE_CHARS`: both daemons' pairing-code alphabet
/// (`principals.PAIRING_CODE_ALPHABET`) and the group separator.
const CODE_CHARS: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ-";

/// One answer: the text one `print` (or several) writes, and the exit code.
/// `note` goes to stderr before it (empty: nothing); only a printed sign-in
/// link carries one, the reason no browser opened.
#[derive(PartialEq, Eq)]
struct Out {
    stdout: bool,
    text: String,
    code: u8,
    note: String,
}

#[cfg(test)]
impl std::fmt::Debug for Out {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Out")
            .field("stdout", &self.stdout)
            .field("code", &self.code)
            .finish_non_exhaustive()
    }
}

impl Out {
    fn stdout(text: String, code: u8) -> Self {
        Self {
            stdout: true,
            text,
            code,
            note: String::new(),
        }
    }

    fn stderr(text: String, code: u8) -> Self {
        Self {
            stdout: false,
            text,
            code,
            note: String::new(),
        }
    }
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
fn emit(out: J, as_json: bool, text: &[u16], code: u8) -> Out {
    if as_json {
        return Out::stdout(pyjson::dumps_line(&out) + "\n", code);
    }
    if code != EXIT_OK {
        return Out::stderr(pyjson::backslashreplace(text) + "\n", code);
    }
    match String::from_utf16(text) {
        Ok(text) => Out::stdout(text + "\n", code),
        Err(_) => bad_answer("the daemon's answer had text that cannot be printed", false),
    }
}

/// `_bad_answer(detail, as_json)`: an answer this command cannot use,
/// refused (exit 4) before anything is opened or printed.
fn bad_answer(detail: &str, as_json: bool) -> Out {
    if as_json {
        let out = dict(vec![
            ("state", J::from("refused")),
            ("error", J::from("bad_answer")),
            ("detail", J::from(detail)),
        ]);
        Out::stdout(pyjson::dumps_line(&out) + "\n", EXIT_REFUSED)
    } else {
        Out::stderr(format!("pseudolife-mcp console: {detail}\n"), EXIT_REFUSED)
    }
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
fn normalize_origin(value: &str) -> Option<String> {
    if value.is_empty() || value.chars().count() > 255 {
        return None;
    }
    if value.chars().any(|c| (c as u32) < 33 || c as u32 == 127) {
        return None;
    }
    let parts = url::urlsplit(value).ok()?.ok()?;
    let port = parts.port().ok()?;
    let scheme = parts.scheme.as_str();
    if !matches!(scheme, "http" | "https") {
        return None;
    }
    // `parts.username` is not None exactly when the netloc holds an '@'.
    if parts.netloc.contains('@') {
        return None;
    }
    if !matches!(parts.path.as_str(), "" | "/")
        || !parts.query.is_empty()
        || !parts.fragment.is_empty()
        || value.contains('#')
    {
        return None;
    }
    let mut host = parts.hostname()?;
    if host.contains(':') {
        host = format!("[{host}]");
    }
    let default = if scheme == "http" { 80 } else { 443 };
    Some(match port.filter(|port| *port != default) {
        Some(port) => format!("{scheme}://{host}:{port}"),
        None => format!("{scheme}://{host}"),
    })
}

/// A bearer every platform sends alike (doctor's rule): printable ASCII.
fn sendable(token: &str) -> bool {
    token.bytes().all(|byte| (0x20..0x7f).contains(&byte))
}

/// Caller-supplied credential settings are validated before loading a saved registration.
fn setting(key: &str) -> Result<Option<String>, String> {
    std::env::var_os(key)
        .map(|value| {
            value
                .into_string()
                .map_err(|_| format!("{key} is not valid Unicode; set a Unicode value"))
        })
        .transpose()
}

fn credentials() -> Result<(String, String), String> {
    let address = setting(URL_KEY)?;
    let file = setting(FILE_KEY)?;
    let token = setting(TOKEN_KEY)?;
    let (overrides, _label) = super::doctor::registration_credentials().ok_or_else(|| {
        "could not read the saved client registration; check it with pseudolife-mcp doctor"
            .to_owned()
    })?;
    let lookup = |key: &str, environment: Option<String>| {
        overrides
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.clone())
            .or(environment)
    };
    let address = lookup(URL_KEY, address)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| crate::daemon_url::DEFAULT_URL.to_owned());
    let origin = normalize_origin(&address).ok_or_else(|| {
        if address.is_ascii() {
            BAD_URL.to_owned()
        } else {
            format!("{BAD_URL}; use the hostname's ASCII spelling and a numeric port")
        }
    })?;
    let file = lookup(FILE_KEY, file);
    let (provider, key) = match file {
        Some(path) => (
            crate::credentials::CredentialProvider::new(None, Some(path.into())),
            FILE_KEY,
        ),
        None => (
            crate::credentials::CredentialProvider::new(
                lookup(TOKEN_KEY, token).filter(|value| !value.is_empty()),
                None,
            ),
            TOKEN_KEY,
        ),
    };
    let snapshot = provider
        .and_then(|provider| provider.snapshot())
        .map_err(|error| {
            if CREDENTIAL_ERRORS.contains(&error.0) {
                format!("this machine's token is unusable ({error})")
            } else {
                format!("{key}: {error}; set {FILE_KEY} to a readable private token file")
            }
        })?;
    let token = snapshot
        .token()
        .filter(|token| !token.is_empty())
        .ok_or_else(|| NO_TOKEN.to_owned())?;
    if !sendable(token) {
        let fix = if key == FILE_KEY {
            "point it at a private file containing this machine's token"
        } else {
            "set it to this machine's token"
        };
        return Err(format!(
            "{key} does not contain a printable ASCII bearer token; {fix}"
        ));
    }
    Ok((origin, token.to_owned()))
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

/// Help and usage errors are answered before reading any credentials.
fn plan(argv: &[String]) -> Result<(Call, bool), Out> {
    let args = match args::parse(argv) {
        args::Parsed::Help => return Err(Out::stdout(args::HELP.to_owned(), EXIT_OK)),
        args::Parsed::Error(message) => return Err(usage_error(&message)),
        args::Parsed::Args(args) => args,
    };
    if args.revoke.as_deref() == Some("") {
        return Err(Out::stderr(EMPTY_REVOKE.to_owned(), EXIT_USAGE));
    }
    if args.list && args.revoke.is_some() {
        return Err(Out::stderr(
            "--list and --revoke are separate commands; give one\n".to_owned(),
            EXIT_USAGE,
        ));
    }
    if args.to.as_deref().is_some_and(|to| !valid_route_id(to)) {
        return Err(Out::stderr(
            "--to takes a Console page id such as board; use lowercase letters, digits or hyphens\n".to_owned(),
            EXIT_USAGE,
        ));
    }
    let call = if args.list {
        Call::List
    } else if let Some(id) = args.revoke {
        Call::Revoke(id)
    } else {
        Call::Link {
            to: args.to,
            open: !args.no_open,
            unless: args.unless_signed_in,
        }
    };
    Ok((call, args.json))
}

fn usage_error(message: &str) -> Out {
    Out::stderr(
        format!(
            "{}pseudolife-mcp console: error: {message}\n",
            args::usage()
        ),
        EXIT_USAGE,
    )
}

fn prepare(argv: &[String]) -> Step {
    let (call, json) = match plan(argv) {
        Ok(call) => call,
        Err(out) => return Step::Done(out),
    };
    match credentials() {
        Err(why) => Step::Done(emit(
            dict(vec![
                ("state", J::from("no_credentials")),
                ("error", J::Str(why.clone())),
            ]),
            json,
            &units(&format!("pseudolife-mcp console: {why}")),
            EXIT_NO_DAEMON,
        )),
        Ok((origin, token)) => Step::Call {
            call,
            origin,
            token,
            json,
        },
    }
}

/// The refused connect's error code (`ConnectionRefusedError`): the one
/// connect failure the harness row compares. WSAECONNREFUSED on Windows.
#[cfg(windows)]
const REFUSED: i32 = 10061;
#[cfg(not(windows))]
const REFUSED: i32 = libc::ECONNREFUSED;

/// Preserve the familiar refused-connection message. Other connection
/// failures are described by kind without printing transport internals.
fn reason(error: Option<&io::Error>) -> String {
    let Some(error) = error else {
        return "the connection timed out".to_owned();
    };
    if error.raw_os_error() == Some(REFUSED) {
        let text = io::Error::from_raw_os_error(REFUSED).to_string();
        let message = text
            .strip_suffix(&format!(" (os error {REFUSED})"))
            .unwrap_or(&text);
        return if cfg!(windows) {
            let message = message.trim_end_matches(|c: char| c <= ' ' || c == '.');
            format!("[WinError {REFUSED}] {message}")
        } else {
            format!("[Errno {REFUSED}] {message}")
        };
    }
    match error.kind() {
        io::ErrorKind::TimedOut => "the connection timed out",
        io::ErrorKind::ConnectionRefused => "the connection was refused",
        io::ErrorKind::NetworkUnreachable => "the network is unreachable",
        io::ErrorKind::HostUnreachable => "the host is unreachable",
        io::ErrorKind::PermissionDenied => "the system denied the connection",
        _ => "could not establish a connection",
    }
    .to_owned()
}

enum RequestError {
    NoReply(String),
    BadReply(&'static str),
}

impl RequestError {
    fn report(self, origin: &str, as_json: bool) -> Out {
        match self {
            Self::NoReply(reason) => no_daemon(origin, &reason, as_json),
            Self::BadReply(cause) => bad_answer(cause, as_json),
        }
    }
}

async fn request(
    origin: &str,
    target: &str,
    token: &str,
    body: Option<&J>,
) -> Result<(u16, Option<Dict>), RequestError> {
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
        Err(NoReply::Connect(error)) => return Err(RequestError::NoReply(reason(error.as_ref()))),
        // The transport does not distinguish DNS, TLS and HTTP failures here.
        Err(NoReply::Io(_) | NoReply::Other) => {
            return Err(RequestError::NoReply("no usable HTTP reply".to_owned()));
        }
    };
    let success = (200..300).contains(&reply.status);
    // An unreadable successful reply is a protocol error. For an HTTP
    // error the response status can still explain the refusal.
    if success && reply.body_failed {
        return Err(RequestError::BadReply(
            "the daemon's response body could not be read; check the daemon with pseudolife-mcp doctor",
        ));
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
            Err(pyjson::Refused::TooDeep) => {
                return Err(RequestError::BadReply(
                    "the daemon's response is nested too deeply; check the daemon with pseudolife-mcp doctor",
                ));
            }
        },
    };
    Ok((reply.status, payload))
}

/// `_refusal(status, payload)`, as the value `_emit` writes (`detail` keeps
/// a hint's JSON type; the text is its `str()`).
fn refusal(status: u16, payload: Option<&Dict>) -> J {
    let code = payload.and_then(|payload| pyjson::get(payload, "error"));
    let hint = payload.and_then(|payload| pyjson::get(payload, "hint"));
    // Only a str is looked up: a list or dict reads as an unknown code.
    let truthy = |value: Option<&J>| value.filter(|value| value.truthy()).cloned();
    if let Some(name) = code.and_then(J::as_str)
        && let Some((_, message)) = REFUSALS.iter().find(|(key, _)| *key == name)
    {
        return match message {
            Some(message) => J::from(*message),
            None => truthy(hint).unwrap_or_else(|| code.cloned().unwrap_or(J::Null)),
        };
    }
    if matches!(status, 404 | 405) {
        return J::Str(format!(
            "this daemon does not offer Console sign-in yet (HTTP {status}): update it with `pseudolife-mcp update`, or open the Console and paste a token under Advanced"
        ));
    }
    if let Some(hint) = truthy(hint) {
        return hint;
    }
    let mut text = units(&format!("the daemon answered HTTP {status}"));
    if let Some(code) = truthy(code) {
        text.extend(units(" ("));
        text.extend(code.py_str_units());
        text.extend(units(")"));
    }
    pyjson::text(text)
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

/// `_stamp(value)`: the cell, or `Ok(None)` where `time.localtime` raises
/// (the list's refusal).
fn stamp_cell(value: Option<&J>) -> Option<String> {
    let seconds = match value {
        Some(J::Bool(flag)) => i64::from(*flag),
        // Past time_t: OverflowError.
        Some(J::Int(text)) => match text.parse::<i64>() {
            Ok(seconds) => seconds,
            Err(_) => return None,
        },
        Some(J::Float(value)) => {
            // `_PyTime_ObjectToTime_t` floors a float; NaN raises ValueError,
            // the infinities and anything past time_t OverflowError (2**63
            // itself passes its check and casts to a time no localtime takes).
            let floor = value.floor();
            if !floor.is_finite()
                || !(-9.223_372_036_854_776e18..9.223_372_036_854_776e18).contains(&floor)
            {
                return None;
            }
            floor as i64
        }
        _ => return Some("-".to_owned()),
    };
    stamp::minute(seconds)
}

/// `_list(payload, as_json)`.
fn listed(payload: &Dict, as_json: bool) -> Out {
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
        return Out::stdout(pyjson::dumps_line(&out) + "\n", EXIT_OK);
    }
    if rows.is_empty() {
        return Out::stdout(
            "No browsers are signed in. `pseudolife-mcp console` signs one in.\n".to_owned(),
            EXIT_OK,
        );
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
        let [since, last] = ["created_at", "last_used_at"].map(|field| stamp_cell(raw(row, field)));
        let (Some(since), Some(last)) = (since.as_deref(), last.as_deref()) else {
            let field = if since.is_none() {
                "created_at"
            } else {
                "last_used_at"
            };
            return bad_answer(
                &format!("the daemon's session list had a time this machine cannot show ({field})"),
                false,
            );
        };
        let label = match raw(row, "label") {
            Some(value) if value.truthy() => value.py_str_units(),
            _ => units("-"),
        };
        for (piece, width) in [
            (cell("session_id"), 17),
            (cell("principal"), 20),
            (cell("method"), 8),
            (units(since), 17),
            (units(last), 17),
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
    match String::from_utf16(&text) {
        Ok(text) => Out::stdout(text, EXIT_OK),
        Err(_) => bad_answer(LIST_TEXT, false),
    }
}

/// `_ORIGIN.fullmatch(origin)`: `https?://` then a host of `[a-z0-9._~-]`
/// or a bracketed `[0-9a-f:.]` address, and an optional 1-5 digit port.
fn origin_is_plain(origin: &str) -> bool {
    let Some(rest) = origin
        .strip_prefix("http://")
        .or_else(|| origin.strip_prefix("https://"))
    else {
        return false;
    };
    let (host_ok, port) = match rest.strip_prefix('[') {
        Some(bracketed) => match bracketed.split_once(']') {
            Some((inner, port)) => (
                !inner.is_empty()
                    && inner.bytes().all(|b| {
                        b.is_ascii_digit() || (b'a'..=b'f').contains(&b) || b == b':' || b == b'.'
                    }),
                port,
            ),
            None => (false, ""),
        },
        None => {
            let end = rest.find(':').unwrap_or(rest.len());
            let (host, port) = rest.split_at(end);
            (
                !host.is_empty()
                    && host.bytes().all(|b| {
                        b.is_ascii_lowercase() || b.is_ascii_digit() || b"._~-".contains(&b)
                    }),
                port,
            )
        }
    };
    host_ok
        && (port.is_empty()
            || port.strip_prefix(':').is_some_and(|digits| {
                (1..=5).contains(&digits.len()) && digits.bytes().all(|b| b.is_ascii_digit())
            }))
}

/// `link_is_safe(url, origin, to)`: after `<origin>/ui/#/signin?code=`
/// only a code of [`CODE_CHARS`] and, when page `to` was asked for, an
/// optional `&to=<to>`. An origin holding anything but ASCII (the port
/// cannot split it) fails [`origin_is_plain`] in the oracle too, as does a
/// URL holding a lone surrogate.
fn link_is_safe(url: Option<&J>, origin: Option<&J>, to: Option<&str>) -> bool {
    let origin = match origin {
        Some(J::Str(origin)) => normalize_origin(origin),
        _ => None,
    };
    let (Some(origin), Some(J::Str(url))) = (origin, url) else {
        return false;
    };
    if !origin_is_plain(&origin) {
        return false;
    }
    let Some(rest) = url.strip_prefix(&format!("{origin}/ui/#/signin?code=")) else {
        return false;
    };
    let (code, page) = rest.split_at(rest.find('&').unwrap_or(rest.len()));
    !code.is_empty()
        && code.chars().all(|c| CODE_CHARS.contains(c))
        && (page.is_empty() || to.is_some_and(|to| page == format!("&to={to}")))
}

/// `_opened(payload, args, environ)`. `launch` starts the browser when one
/// may open (no `--no-open`); it runs only once the answer has passed every
/// check, before anything is printed, as in the oracle. When it reports a
/// cause, the link is printed with that cause: on stderr beside the text,
/// as `browser_error` in the JSON.
fn opened(
    payload: &Dict,
    as_json: bool,
    to: Option<&str>,
    launch: Option<&mut dyn FnMut(&str) -> launch::Launched>,
) -> Out {
    let url = pyjson::get(payload, "url");
    let code = pyjson::get(payload, "code");
    let origin = pyjson::get(payload, "origin");
    let safe = matches!(code, Some(J::Str(_) | J::Wtf(_))) && link_is_safe(url, origin, to);
    let (true, Some(J::Str(link)), Some(J::Str(at)), Some(code)) = (safe, url, origin, code) else {
        return bad_answer(NO_LINK, as_json);
    };
    let shown = link
        .split_once("?code=")
        .map(|(_, rest)| rest.split('&').next().unwrap_or(rest))
        .unwrap_or_default();
    if code.as_str() != Some(shown) {
        return bad_answer(MISMATCH, as_json);
    }
    let expires_in = pyjson::get(payload, "expires_in");
    let Ok(minutes) = pyint::minutes(expires_in) else {
        return bad_answer(BAD_EXPIRY, as_json);
    };
    let (opened, cause) = match launch.map(|launch| launch(link)) {
        Some(Ok(())) => (true, None),
        Some(Err(cause)) => (false, cause),
        None => (false, None),
    };
    if as_json {
        let mut items = vec![
            ("state", J::from(if opened { "opened" } else { "printed" })),
            ("url", J::Str(link.clone())),
            ("code", code.clone()),
            ("origin", J::Str(at.clone())),
            ("principal", or_null(pyjson::get(payload, "principal"))),
            ("expires_in", or_null(expires_in)),
        ];
        if let Some(cause) = cause {
            items.push(("browser_error", J::Str(cause)));
        }
        return Out::stdout(pyjson::dumps_line(&dict(items)) + "\n", EXIT_OK);
    }
    let lead = if opened {
        "Opened the Console in your browser: confirm the sign-in there."
    } else {
        "Open this link to sign in to the Console:"
    };
    let mut out = Out::stdout(
        format!(
            "{lead}\n  {link}\nOr type the code {shown} into the Console's sign-in dialog at {at}/ui/. It works once, within {minutes} minutes.\n"
        ),
        EXIT_OK,
    );
    if let Some(cause) = cause {
        out.note = format!("pseudolife-mcp console: no browser opened: {cause}\n");
    }
    out
}

/// The configured origin is already stripped of credentials, paths and queries.
fn no_daemon(origin: &str, reason: &str, as_json: bool) -> Out {
    const RECOVERY: &str = "check the daemon address and connection with pseudolife-mcp doctor";
    emit(
        dict(vec![
            ("state", J::from("no_daemon")),
            ("error", J::from(reason)),
            ("origin", J::from(origin)),
            ("recovery", J::from(RECOVERY)),
        ]),
        as_json,
        &units(&format!(
            "pseudolife-mcp console: no daemon answered at {origin} ({reason}); {RECOVERY}"
        )),
        EXIT_NO_DAEMON,
    )
}

/// The native browser choice, decided before the code is minted.
type Chooser<'a> = &'a dyn Fn() -> browser::Plan;
/// Starts a chosen plan for the link ([`launch::open`] outside the tests).
type Launcher<'a> = &'a dyn Fn(&browser::Plan, &str) -> launch::Launched;

/// `main` from the first request on.
async fn answer(
    call: Call,
    origin: &str,
    token: &str,
    as_json: bool,
    browser: Chooser<'_>,
    launcher: Launcher<'_>,
) -> Out {
    let mut plan = None;
    let (target, body) = match &call {
        Call::List => ("/api/console/sessions", None),
        Call::Revoke(id) => (
            "/api/console/sessions/revoke",
            Some(dict(vec![("session_id", J::Str(id.clone()))])),
        ),
        Call::Link { to, open, unless } => {
            if *unless {
                // A transport failure here stops before minting a code.
                match request(origin, "/api/console/sessions", token, None).await {
                    Err(error) => return error.report(origin, as_json),
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
                plan = Some(browser());
            }
            let mut body = vec![("origin", J::Str(origin.to_owned()))];
            if let Some(to) = to {
                body.push(("to", J::Str(to.clone())));
            }
            ("/api/console/link", Some(dict(body)))
        }
    };
    let (status, payload) = match request(origin, target, token, body.as_ref()).await {
        Ok(answer) => answer,
        Err(error) => return error.report(origin, as_json),
    };
    let payload = match payload {
        Some(payload) if status == 200 => payload,
        payload => {
            let detail = refusal(status, payload.as_ref());
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
        Call::Link { to, .. } => match plan {
            // `--no-open`: nothing is chosen, nothing starts.
            None => opened(&payload, as_json, to.as_deref(), None),
            Some(plan) => opened(
                &payload,
                as_json,
                to.as_deref(),
                Some(&mut |url: &str| launcher(&plan, url)),
            ),
        },
    }
}

fn decide(argv: &[String]) -> Out {
    match prepare(argv) {
        Step::Done(out) => out,
        Step::Call {
            call,
            origin,
            token,
            json,
        } => {
            let runtime = match tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            {
                Ok(runtime) => runtime,
                Err(error) => {
                    return Out::stderr(
                        format!(
                            "pseudolife-mcp console: could not start the I/O runtime ({:?}); free operating-system resources and retry\n",
                            error.kind()
                        ),
                        1,
                    );
                }
            };
            runtime.block_on(answer(
                call,
                &origin,
                &token,
                json,
                &|| browser::choose(&launch::Machine, browser::Platform::HERE),
                &launch::open,
            ))
        }
    }
}

pub(super) fn run(values: Vec<OsString>) -> ExitCode {
    let argv = values
        .into_iter()
        .map(OsString::into_string)
        .collect::<Result<Vec<_>, _>>();
    let out = match argv {
        Ok(argv) => decide(&argv),
        Err(_) => usage_error("an argument is not valid Unicode; supply Unicode arguments"),
    };
    if !out.note.is_empty() {
        // Best effort: the link on stdout is what the user needs.
        let _ = super::move_cli::emit(&mut io::stderr().lock(), &super::text_bytes(&out.note));
    }
    let bytes = super::text_bytes(&out.text);
    let written = if out.stdout {
        super::move_cli::emit(&mut io::stdout().lock(), &bytes)
    } else {
        super::move_cli::emit(&mut io::stderr().lock(), &bytes)
    };
    if written.is_ok() {
        return ExitCode::from(out.code);
    }
    if out.stdout {
        let text = "pseudolife-mcp console: stdout could not be written; restore stdout before running console again\n";
        let _ = super::move_cli::emit(&mut io::stderr().lock(), &super::text_bytes(text));
        if written == Err(0) {
            return ExitCode::from(120);
        }
    }
    ExitCode::FAILURE
}

#[cfg(test)]
mod tests;
