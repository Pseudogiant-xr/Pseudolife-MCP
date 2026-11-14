//! `conductor-watch`: wake the conductor session with board mail only when
//! something needs a decision.
//!
//! This file answers what comes before the watch: help and argument
//! errors, `--stop` (by `--state`, `--owner` or the session's digest), the
//! owner refusals, and a start that finds a watcher already running on its
//! state (same owner: exit 0; another: exit 2), with or without `--detach`.
//! `start.rs` runs the watch (`watch.rs`) in the foreground or detached.
//! The argument spellings listed in `rust/cli_harness/rows/conductor_watch.md`
//! still defer before any effect.
//!
//! `--stop` never kills: it writes the `.stop` request only when the pid
//! record names a live process whose start time is the one recorded, and
//! the watcher, which checks for the request every second, exits cleanly.
mod args;
mod events;
mod sources;
mod start;
mod watch;

use super::pyjson::{self, J};
use std::{
    ffi::OsString,
    fs,
    io::{self, Read},
    path::{Path, PathBuf},
    process::ExitCode,
};

/// Outside the reproduced domain: the dispatcher's deferral line.
#[derive(Debug)]
struct Defer;
type Res<T> = Result<T, Defer>;

const EXIT_OK: u8 = 0;
const EXIT_SETUP: u8 = 2;
const EXIT_USAGE: u8 = 2;
const PROG: &str = "pseudolife-mcp conductor-watch";
/// `conductor_watch_cli.DEPRECATED_MODE`, printed by `cli.py` first.
const DEPRECATED_MODE: &str =
    "pseudolife-mcp delegate-watch is deprecated: use pseudolife-mcp conductor-watch";
const CLEARS: &str = "decision events";
/// Pid records nested deeper defer: the oracle's `json.loads` raises
/// RecursionError (read as no watcher) somewhere past 900 levels on this
/// path, which was not measured, and parses anything shallower, where the
/// record may still name a live watcher; this stays far inside it.
const DEPTH: usize = 500;

/// Python `repr()` of a str.
fn py_repr(text: &str) -> String {
    pyjson::py_value_repr(&J::Str(text.to_owned()))
}

/// `str(value)` (an f-string field) as the reconfigured streams write it:
/// UTF-8 with `errors="replace"`, so a lone surrogate prints as `?`.
fn py_str(value: Option<&J>) -> String {
    let units = value.map_or_else(|| "None".encode_utf16().collect(), J::py_str_units);
    char::decode_utf16(units)
        .map(|unit| unit.unwrap_or('?'))
        .collect()
}

/// `str(path)`: pathlib's spelling, which must be text to be printed here.
fn shown(path: &Path) -> Res<String> {
    path.to_str().map(str::to_owned).ok_or(Defer)
}

/// `Path.with_suffix(suffix)` (CPython 3.11), for a last component that is
/// UTF-8 text, neither empty nor ending in a dot (where versions differ or
/// pathlib raises).
fn with_suffix(path: &Path, suffix: &str) -> Res<PathBuf> {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or(Defer)?;
    if name.is_empty() || name.ends_with('.') {
        return Err(Defer);
    }
    let stem = match name.rfind('.') {
        Some(index) if index > 0 => &name[..index],
        _ => name,
    };
    Ok(path.with_file_name(format!("{stem}{suffix}")))
}

/// What one run answers: the streams it writes, in order, and its code.
#[derive(Debug, Default, PartialEq, Eq)]
struct Out {
    stdout: String,
    stderr: String,
    code: u8,
    /// `--stop`'s request file, written before the streams.
    stop_file: Option<PathBuf>,
    /// No watcher runs on the state: start one (after the notice).
    start: Option<start::Start>,
}

impl Out {
    fn stdout(text: String, code: u8) -> Self {
        Self {
            stdout: text + "\n",
            code,
            ..Self::default()
        }
    }
    fn say(text: &str, code: u8) -> Self {
        Self {
            stderr: format!("conductor-watch: {text}\n"),
            code,
            ..Self::default()
        }
    }
}

/// The process environment and stream facts a decision reads.
struct Context<'a> {
    var: &'a dyn Fn(&str) -> Option<OsString>,
    columns_80: bool,
    colour_stdout: bool,
    colour_stderr: bool,
    utf8_argv: bool,
}

impl Context<'_> {
    fn text(&self, name: &str) -> Res<Option<String>> {
        match (self.var)(name) {
            None => Ok(None),
            Some(value) => value.into_string().map(Some).map_err(|_| Defer),
        }
    }
}

/// `lease_cli._pid_alive` for a pid inside every platform's range.
fn alive(pid: i64) -> bool {
    pid > 0 && u32::try_from(pid).is_ok_and(super::lease::pid_alive)
}

/// `_process_start(pid)`: an opaque token for when `pid` started, `None`
/// when it cannot be read. The oracle reads Windows's creation FILETIME and
/// Linux's `/proc/<pid>/stat` field 22; elsewhere it runs `ps`, which this
/// port does not, so that defers.
#[cfg_attr(any(windows, target_os = "macos"), allow(unsafe_code))]
fn process_start(pid: u32) -> Res<Option<String>> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::{
            Foundation::{CloseHandle, FILETIME},
            System::Threading::{GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
        };
        let empty = || FILETIME {
            dwLowDateTime: 0,
            dwHighDateTime: 0,
        };
        // SAFETY: a query-only handle, closed on every path; the four
        // FILETIME out-parameters are live locals.
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return Ok(None);
            }
            let (mut created, mut exited, mut kernel, mut user) =
                (empty(), empty(), empty(), empty());
            let read = GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user);
            CloseHandle(handle);
            if read == 0 {
                return Ok(None);
            }
            Ok(Some(
                ((u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime))
                    .to_string(),
            ))
        }
    }
    #[cfg(target_os = "linux")]
    {
        // A stat that cannot be read, does not decode or has no field 22
        // falls through to `ps` there, which this port does not run: defer.
        let raw = fs::read(format!("/proc/{pid}/stat")).map_err(|_| Defer)?;
        let text = String::from_utf8(raw).map_err(|_| Defer)?;
        let tail = text.rsplit_once(')').ok_or(Defer)?.1;
        tail.split_whitespace()
            .nth(19)
            .map(|field| Some(field.to_owned()))
            .ok_or(Defer)
    }
    #[cfg(target_os = "macos")]
    {
        // The kernel's start time for `pid`, in place of the oracle's `ps`.
        // SAFETY: `info` is a live, zeroed, correctly sized out-parameter.
        let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
        let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
        let Ok(pid) = libc::c_int::try_from(pid) else {
            return Ok(None);
        };
        // SAFETY: `info` outlives the call, and `size` is its exact size, so
        // the kernel writes inside it.
        let read = unsafe {
            libc::proc_pidinfo(
                pid,
                libc::PROC_PIDTBSDINFO,
                0,
                (&raw mut info).cast(),
                size,
            )
        };
        if read != size {
            return Ok(None);
        }
        Ok(Some(format!(
            "{}.{:06}",
            info.pbi_start_tvsec, info.pbi_start_tvusec
        )))
    }
    #[cfg(all(unix, not(any(target_os = "linux", target_os = "macos"))))]
    {
        let _ = pid;
        Err(Defer)
    }
}

/// `int(record["pid"])`: `Ok(None)` where the oracle's `_running` catches
/// the failure (ValueError, KeyError, TypeError, OverflowError) or refuses
/// the pid (at or past `_PID_LIMIT`, 2**31, or at or below 0 for
/// `_pid_alive`), and so reads no watcher.
fn record_pid(value: Option<&J>) -> Res<Option<i64>> {
    // Decimal digits with an optional sign: outside i32 the pid is past
    // the limit or not positive, no watcher either way.
    let small = |text: &str| text.parse::<i32>().ok().map(i64::from);
    Ok(match value {
        None | Some(J::Null | J::List(_) | J::Dict(_)) => None,
        Some(J::Bool(flag)) => Some(i64::from(*flag)),
        Some(J::Int(text)) => small(text),
        Some(J::Float(number)) if !number.is_finite() => None,
        Some(J::Float(number)) => {
            let whole = number.trunc();
            if whole.abs() > f64::from(i32::MAX) {
                return Ok(None);
            }
            // Bounded above: the cast is exact.
            #[allow(clippy::cast_possible_truncation)]
            Some(whole as i64)
        }
        Some(J::Wtf(_)) => None,
        Some(J::Str(text)) => {
            if !text.is_ascii() || text.contains('_') {
                return Err(Defer);
            }
            // An ASCII str reaches `PyLong_FromString` unchanged, which
            // strips C `isspace` only; U+001C..U+001F stay and raise.
            let digits = text
                .trim_matches(|c: char| matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r'));
            let unsigned = digits.strip_prefix(['+', '-']).unwrap_or(digits);
            if unsigned.is_empty() || !unsigned.bytes().all(|b| b.is_ascii_digit()) {
                None
            } else {
                small(digits.strip_prefix('+').unwrap_or(digits))
            }
        }
    })
}

/// `_running(state)`: the pid file's record when it names a live process
/// that started when the record says.
fn running(state: &Path) -> Res<Option<pyjson::Dict>> {
    let Ok(raw) = fs::read(with_suffix(state, ".pid")?) else {
        return Ok(None);
    };
    let Ok(text) = String::from_utf8(raw) else {
        return Ok(None);
    };
    let record = match pyjson::loads(&text, DEPTH) {
        Ok(J::Dict(record)) => record,
        Ok(_) | Err(pyjson::Refused::NotJson) => return Ok(None),
        Err(pyjson::Refused::TooDeep) => return Err(Defer),
    };
    // `int(record["pid"])`: a missing key and a JSON null both fail there.
    let Some(pid) = record_pid(pyjson::get(&record, "pid"))? else {
        return Ok(None);
    };
    if !alive(pid) {
        return Ok(None);
    }
    let recorded = pyjson::get(&record, "process_start");
    #[allow(clippy::cast_sign_loss, clippy::cast_possible_truncation)]
    let started = process_start(pid as u32)?;
    if let (Some(recorded), Some(current)) = (recorded, started.clone())
        && recorded != &J::Str(current)
        && !legacy_start_matches(recorded, started.as_deref())
    {
        return Ok(None);
    }
    Ok(Some(record))
}

/// Whether `recorded` is the start time a Python watcher wrote for the
/// process whose native token is `current`: on macOS it ran `ps -o
/// lstart=`, local time as `Fri Nov 14 14:09:28 2026`, so a watcher still
/// running from before an upgrade is still found. Elsewhere the Python and
/// native tokens are the same.
fn legacy_start_matches(recorded: &J, current: Option<&str>) -> bool {
    let (J::Str(recorded), Some(current)) = (recorded, current) else {
        return false;
    };
    cfg!(target_os = "macos")
        && current
            .split_once('.')
            .and_then(|(seconds, _)| seconds.parse::<i64>().ok())
            .and_then(lstart)
            .is_some_and(|rendered| rendered == recorded.trim())
}

/// `ps -o lstart=` for a start at `seconds` since the epoch, in local time.
fn lstart(seconds: i64) -> Option<String> {
    use chrono::TimeZone;
    chrono::Local
        .timestamp_opt(seconds, 0)
        .single()
        .map(|at| at.format("%a %b %e %H:%M:%S %Y").to_string())
}

/// `resolve_digest_path(session)`: the session's digest file.
fn digest(session: &str) -> Res<PathBuf> {
    super::wait_mail::digest_path(&OsString::from(session)).ok_or(Defer)
}

/// `CLAUDE_CODE_SESSION_ID`, `None` when unset or empty (`if not session`).
fn session(context: &Context<'_>) -> Res<Option<String>> {
    Ok(context
        .text("CLAUDE_CODE_SESSION_ID")?
        .filter(|session| !session.is_empty()))
}

fn is_agent_id(bytes: &[u8]) -> bool {
    bytes.len() == 32
        && bytes
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
}

/// `resolve_owner(flag)`: the owner, or the `OwnerError` text.
fn resolve_owner(flag: Option<&str>, context: &Context<'_>) -> Res<Result<String, String>> {
    if let Some(flag) = flag {
        return Ok(if is_agent_id(flag.as_bytes()) {
            Ok(flag.to_owned())
        } else {
            Err(format!(
                "--owner {} is not an agent id (32 lowercase hex)",
                py_repr(flag)
            ))
        });
    }
    let Some(session) = session(context)? else {
        return Ok(Err(
            "no session to read the owner from (CLAUDE_CODE_SESSION_ID is unset)".into(),
        ));
    };
    let path = with_suffix(&digest(&session)?, ".agent")?;
    let name = shown(&path)?;
    let info = match fs::symlink_metadata(&path) {
        Ok(info) => info,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(Err(format!("no board address file at {name}")));
        }
        // Unreadable in a way not reproduced here.
        Err(_) => return Err(Defer),
    };
    if info.file_type().is_symlink() {
        return Ok(Err(format!("{name} is a link, not a regular file")));
    }
    if info.is_dir() {
        // os.open refuses a directory on Windows (EACCES) and opens it on
        // POSIX, where fstat then says it is not a regular file.
        return Ok(Err(if cfg!(windows) {
            format!("{name} could not be read (Permission denied)")
        } else {
            format!("{name} is not a regular file")
        }));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(&path).map_err(|_| Defer)?;
    if !file.metadata().map_err(|_| Defer)?.is_file() {
        return Ok(Err(format!("{name} is not a regular file")));
    }
    let mut head = Vec::new();
    file.take(256).read_to_end(&mut head).map_err(|_| Defer)?;
    let first = head.split(|b| *b == b'\n').next().unwrap_or_default();
    let mut end = first.len();
    while end > 0 && first[end - 1] == b'\r' {
        end -= 1;
    }
    let first = &first[..end];
    Ok(if is_agent_id(first) {
        Ok(String::from_utf8_lossy(first).into_owned())
    } else {
        Err(format!(
            "{name} does not hold an agent id (32 lowercase hex)"
        ))
    })
}

/// `default_state(environ, owner)`.
fn default_state(owner: Option<&str>, context: &Context<'_>) -> Res<Option<PathBuf>> {
    if let Some(session) = session(context)? {
        return with_suffix(&digest(&session)?, ".dwatch.json").map(Some);
    }
    let Some(owner) = owner else {
        return Ok(None);
    };
    #[cfg(unix)]
    if context.text("HOME")?.is_some_and(|home| home.is_empty()) {
        // POSIX `Path.home()` with an empty HOME: not reproduced.
        return Err(Defer);
    }
    let home = crate::credentials::python_home_with(|key| (context.var)(key));
    Ok(Some(super::wait_mail::pathlib(
        &home
            .join(".pseudolife-mcp")
            .join("watch")
            .join(format!("{owner}.json")),
    )))
}

/// `_park_hint(agent_id)`.
fn park_hint(agent: &str) -> String {
    format!("park with park_needs=\"{CLEARS}\" park_clear_by={agent}")
}

/// `_foreign` once it found another owner: exit 2.
fn foreign(record: &pyjson::Dict, owner: &str, state: &Path) -> Res<Out> {
    Ok(Out::say(
        &format!(
            "{} belongs to the watcher for {}; pass --state <another path> to watch for {owner}",
            shown(state)?,
            py_str(pyjson::get(record, "owner"))
        ),
        EXIT_SETUP,
    ))
}

/// `_already(record)`: exit 0.
fn already(record: &pyjson::Dict) -> Out {
    let agent = py_str(pyjson::get(record, "agent_id"));
    Out::stdout(
        format!(
            "conductor-watch {agent} already running for {}; {}",
            py_str(pyjson::get(record, "owner")),
            park_hint(&agent)
        ),
        EXIT_OK,
    )
}

/// `main` up to the state's lock; `Err(Defer)` past it.
fn decide(argv: &[String], context: &Context<'_>) -> Res<Out> {
    // Outside UTF-8 decoding CPython sees other characters than these.
    if !context.utf8_argv && !argv.iter().all(|argument| argument.is_ascii()) {
        return Err(Defer);
    }
    // json.loads reads integers under this digit limit (4300 unset).
    if context.text("PYTHONINTMAXSTRDIGITS")?.is_some() {
        return Err(Defer);
    }
    let parsed = args::parse(argv);
    let parser_output = matches!(parsed, args::Parsed::Help | args::Parsed::Error(_));
    if parser_output && !context.columns_80 {
        return Err(Defer);
    }
    let args = match parsed {
        args::Parsed::Help if context.colour_stdout => return Err(Defer),
        args::Parsed::Help => return Ok(Out::stdout(args::HELP.trim_end().to_owned(), EXIT_OK)),
        args::Parsed::Error(_) if context.colour_stderr => return Err(Defer),
        args::Parsed::Error(message) => {
            return Ok(Out {
                stderr: format!("{}{PROG}: error: {message}\n", args::usage()),
                code: EXIT_USAGE,
                ..Out::default()
            });
        }
        args::Parsed::Defer => return Err(Defer),
        args::Parsed::Args(args) => args,
    };
    let owner = resolve_owner(args.owner.as_deref(), context)?;
    let state = match &args.state {
        Some(state) => Some(super::wait_mail::pathlib(Path::new(state))),
        None => default_state(owner.as_ref().ok().map(String::as_str), context)?,
    };
    if args.stop {
        let Some(state) = state else {
            return Ok(Out::say(
                "no session to find the watcher by; pass --owner <id> or --state <path>",
                EXIT_SETUP,
            ));
        };
        return stop(&state);
    }
    let owner = match owner {
        Ok(owner) => owner,
        Err(why) => {
            return Ok(Out::say(
                &format!("{why}; pass --owner <32-hex board agent id of the session to wake>"),
                EXIT_SETUP,
            ));
        }
    };
    let state = state.ok_or(Defer)?;
    let Some(record) = running(&state)? else {
        // `--detach` starts a watcher; the foreground run takes the lock.
        let files = watch::Files {
            pid: with_suffix(&state, ".pid")?,
            lock: with_suffix(&state, ".lock")?,
            stop: with_suffix(&state, ".stop")?,
            log: with_suffix(&state, ".log")?,
            ledger: with_suffix(&state, ".ledger")?,
            state,
        };
        let home = crate::credentials::python_home_with(|key| (context.var)(key));
        return Ok(Out {
            start: Some(start::Start {
                owner,
                files,
                args,
                argv: argv.to_vec(),
                home,
            }),
            ..Out::default()
        });
    };
    if pyjson::get(&record, "owner") != Some(&J::Str(owner.clone())) {
        return foreign(&record, &owner, &state);
    }
    Ok(already(&record))
}

/// `_stop(state)`.
fn stop(state: &Path) -> Res<Out> {
    let Some(record) = running(state)? else {
        return Ok(Out::stdout(
            format!(
                "conductor-watch: no watcher running for {}; nothing changed",
                shown(state)?
            ),
            EXIT_OK,
        ));
    };
    let mut out = Out::stdout(
        format!(
            "conductor-watch {} asked to stop; it exits at its next poll",
            py_str(pyjson::get(&record, "agent_id"))
        ),
        EXIT_OK,
    );
    out.stop_file = Some(with_suffix(state, ".stop")?);
    Ok(out)
}

/// `exc.strerror` of the `OSError` Python's `open(path, "w")` raises for
/// the failure `error` stands for, `None` where it is not reproduced.
/// POSIX: the C library's `strerror`, the text Rust prints before ` (os
/// error N)`. Windows: Python's `open` reports the C runtime's errno for the
/// Win32 code (`_dosmaperr`), then that errno's message. Codes 3, 5 and 32
/// were measured against CPython 3.11 (2026-11-13: a missing directory, a
/// directory or hidden file, a file held with no sharing); 2, the rest of
/// 19..=36 and 112 follow the C runtime's documented table. Others defer.
fn strerror(error: &io::Error) -> Option<String> {
    let code = error.raw_os_error()?;
    if cfg!(windows) {
        let text = match code {
            2 | 3 => "No such file or directory",
            // ERROR_ACCESS_DENIED, and the CRT's EACCES range from
            // ERROR_WRITE_PROTECT to ERROR_SHARING_BUFFER_EXCEEDED.
            5 | 19..=36 => "Permission denied",
            112 => "No space left on device",
            _ => return None,
        };
        return Some(text.to_owned());
    }
    error
        .to_string()
        .strip_suffix(&format!(" (os error {code})"))
        .map(str::to_owned)
}

/// `_stop`'s refusal when its request file cannot be written: `reason` is
/// the error's `strerror`.
fn stop_refusal(stop: &Path, reason: &str) -> Res<String> {
    // pathlib's parent of a bare name is ".".
    let parent = stop
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    Ok(format!(
        "cannot write {} ({reason}); nothing changed. Make {} writable, then run --stop again",
        shown(stop)?,
        shown(parent)?
    ))
}

/// `stop.write_text("")`: create or empty the request file.
fn write_stop(stop: &Path) -> io::Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        // The C runtime opens with CREATE_ALWAYS, which Windows refuses
        // (ERROR_ACCESS_DENIED) over a hidden or system file; Rust's
        // `File::create` empties one in place.
        const HIDDEN_OR_SYSTEM: u32 = 0x2 | 0x4;
        if fs::metadata(stop).is_ok_and(|info| info.file_attributes() & HIDDEN_OR_SYSTEM != 0) {
            return Err(io::Error::from_raw_os_error(5));
        }
    }
    fs::write(stop, b"")
}

/// Write `text` whole; `Err(written)` with how much left before failing.
fn emit(stream: &mut impl io::Write, text: &str) -> Result<(), usize> {
    if text.is_empty() {
        return Ok(());
    }
    super::move_cli::emit(stream, &super::text_bytes(text))
}

/// `None` defers to the dispatcher before any effect.
pub(super) fn run(mode: &str, values: Vec<OsString>) -> Option<ExitCode> {
    use std::io::IsTerminal;
    let argv = values
        .into_iter()
        .map(OsString::into_string)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let colour = super::move_cli::colour_env(|name| std::env::var_os(name).is_some());
    let var = |name: &str| std::env::var_os(name);
    let context = Context {
        var: &var,
        columns_80: std::env::var_os("COLUMNS").is_some_and(|value| value == "80"),
        colour_stdout: colour || io::stdout().is_terminal(),
        colour_stderr: colour || io::stderr().is_terminal(),
        utf8_argv: super::move_cli::utf8_argv(),
    };
    let out = decide(&argv, &context).ok()?;
    let notice = if mode == "delegate-watch" {
        format!(
            "{DEPRECATED_MODE}
"
        )
    } else {
        String::new()
    };
    // The oracle prints the deprecation notice before it does anything
    // else: a notice that cannot be written stops it before the stop
    // request, so the request must not have been made by then.
    let mut wrote = match emit(&mut io::stderr().lock(), &notice) {
        Ok(()) => !notice.is_empty(),
        Err(0) => return None,
        Err(_) => return Some(ExitCode::from(1)),
    };
    let mut out = out;
    if let Some(start) = out.start.take() {
        let code = if start.args.detach {
            start::detach(&start)
        } else {
            start::foreground(&start)
        };
        return Some(ExitCode::from(code));
    }
    if let Some(path) = out.stop_file.take() {
        // The pid file was just read beside it, so its directory exists
        // (the oracle's mkdir is a no-op). A refused write changed nothing:
        // `_stop` says why and exits 2. A reason not reproduced defers while
        // nothing is printed; after the notice it is Rust's own text
        // (declared in the row's notes).
        match write_stop(&path) {
            Ok(()) => wrote = true,
            Err(error) => {
                let reason = match strerror(&error) {
                    Some(reason) => reason,
                    None if !wrote => return None,
                    None => error.to_string(),
                };
                // The state was printable, so its sibling and directory are.
                out = Out::say(&stop_refusal(&path, &reason).ok()?, EXIT_SETUP);
            }
        }
    }
    let mut failed = false;
    for (stderr, text) in [(false, &out.stdout), (true, &out.stderr)] {
        if failed || text.is_empty() {
            continue;
        }
        let result = if stderr {
            emit(&mut io::stderr().lock(), text)
        } else {
            emit(&mut io::stdout().lock(), text)
        };
        match result {
            Ok(()) => wrote = true,
            Err(written) => {
                failed = true;
                wrote |= written > 0;
            }
        }
    }
    match (failed, wrote) {
        (false, _) => Some(ExitCode::from(out.code)),
        // Nothing left and nothing written: the deferral line instead.
        (true, false) => None,
        (true, true) => Some(ExitCode::from(1)),
    }
}

#[cfg(test)]
mod tests;
