//! `expose_cli`'s seams and readers over the `tailscale` CLI: the lookup
//! (`find_tailscale`: `program_lookup.find_program`, absolute `PATH`
//! directories only, then the default install locations),
//! the runner (`run_tailscale`: `subprocess.run(capture_output=True,
//! stdin=DEVNULL, timeout=30)` decoded by `_text`), and what the oracle
//! reads from it (`_running`, `_serve_config`, `_tailnet_ipv4`,
//! `_port_state`).
//!
//! Values are `pyjson::J`, so `dict.get`, truthiness, `str()` and `repr()`
//! are Python's. Text is `_text`'s: strict UTF-8 with newlines translated;
//! output that is not UTF-8 is `unreadable`.
use super::super::pyjson::{self, Dict, J};
use super::Fault;
use std::{ffi::OsString, process::Stdio, time::Duration};

/// `TAILSCALE_TIMEOUT_S`.
const TIMEOUT: Duration = Duration::from_secs(30);
/// `JSON_DEPTH_LIMIT`: an answer with more brackets open at once is
/// refused as unreadable (`_too_deep`), in both arms alike.
pub(super) const DEPTH: usize = 100;

/// `CompletedProcess` as the readers use it. `unreadable`: its output was
/// not UTF-8, so `out` and `err` are empty (`run_tailscale`'s flag).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub(super) struct Proc {
    pub(super) code: i64,
    pub(super) out: String,
    pub(super) err: String,
    pub(super) unreadable: bool,
}

// ── the lookup ───────────────────────────────────────────────────────────────

/// `os.path.join(dir, name)` for a relative `name` with no drive.
fn join(dir: &str, name: &str) -> String {
    let seps: &[char] = if cfg!(windows) { &['\\', '/'] } else { &['/'] };
    if dir.is_empty() || dir.ends_with(seps) {
        format!("{dir}{name}")
    } else {
        format!("{dir}{}{name}", if cfg!(windows) { '\\' } else { '/' })
    }
}

/// `program_lookup.is_program(path)`: a regular file (following links),
/// executable for this user outside Windows.
fn usable(path: &str) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    if !meta.is_file() {
        return false;
    }
    #[cfg(unix)]
    if rustix::fs::access(path, rustix::fs::Access::EXEC_OK).is_err() {
        return false;
    }
    true
}

/// `program_lookup.DEFAULT_PATHEXT`.
const DEFAULT_PATHEXT: &str = ".COM;.EXE;.BAT;.CMD";
/// `program_lookup.WINDOWS_LAUNCHABLE`: what `CreateProcess` starts.
const LAUNCHABLE: [&str; 4] = [".com", ".exe", ".bat", ".cmd"];

/// `Path(entry).is_absolute()`: `Some(answer)`, or `None` for a spelling
/// this port does not classify as pathlib does (Windows UNC and device
/// paths, which start with two separators).
fn absolute(entry: &str) -> Option<bool> {
    if !cfg!(windows) {
        return Some(entry.starts_with('/'));
    }
    let sep = |b: u8| b == b'\\' || b == b'/';
    let bytes = entry.as_bytes();
    if bytes.len() >= 2 && sep(bytes[0]) && sep(bytes[1]) {
        return None;
    }
    // A drive is an ASCII letter and a colon; it is absolute with a root.
    Some(bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && sep(bytes[2]))
}

/// `program_lookup.find_program("tailscale")`: only absolute `PATH`
/// directories, never the current directory. `Err` defers: a `PATH` or
/// `PATHEXT` that is not UTF-8, or a program found under an entry
/// [`absolute`] cannot classify.
fn which(path: Option<OsString>, pathext: Option<OsString>) -> Result<Option<String>, ()> {
    let Some(path) = path else {
        return Ok(None);
    };
    let path = path.into_string().map_err(|_| ())?;
    let separator = if cfg!(windows) { ';' } else { ':' };
    let names: Vec<String> = if cfg!(windows) {
        let pathext = pathext
            .map(|value| value.into_string().map_err(|_| ()))
            .transpose()?
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| DEFAULT_PATHEXT.to_owned());
        pathext
            .split(';')
            .filter(|ext| LAUNCHABLE.contains(&ext.to_lowercase().as_str()))
            .map(|ext| format!("tailscale{ext}"))
            .collect()
    } else {
        vec!["tailscale".to_owned()]
    };
    for entry in path.split(separator) {
        let dir = pyjson::py_strip(entry).trim_matches('"');
        if dir.is_empty() {
            continue;
        }
        let known = absolute(dir);
        if known == Some(false) {
            continue;
        }
        for name in &names {
            let candidate = join(dir, name);
            if usable(&candidate) {
                return known.map(|_| Some(candidate)).ok_or(());
            }
        }
    }
    Ok(None)
}

/// `str(Path(base) / "Tailscale" / "tailscale.exe")` for a base pathlib
/// leaves as written; any other spelling defers.
fn program_files_exe(base: &str) -> Result<String, ()> {
    let plain = base.len() >= 3
        && base.as_bytes()[0].is_ascii_alphabetic()
        && &base[1..3] == ":\\"
        && !base.contains('/')
        && !base[2..].contains("\\\\")
        && !base[3..]
            .split('\\')
            .any(|part| part == "." || part.is_empty() && base.len() > 3);
    if !plain {
        return Err(());
    }
    Ok(join(base, "Tailscale\\tailscale.exe"))
}

/// `default_locations()`'s Windows base: the first of `ProgramW6432` and
/// `ProgramFiles` that is set and absolute (a relative one would resolve
/// against the current directory), else `C:\Program Files`. `Err` defers:
/// a value that is not UTF-8, or one [`absolute`] cannot classify.
fn program_files_base(values: [Option<OsString>; 2]) -> Result<String, ()> {
    for value in values.into_iter().flatten() {
        let value = value.into_string().map_err(|_| ())?;
        if !value.is_empty() && absolute(&value).ok_or(())? {
            return Ok(value);
        }
    }
    Ok(r"C:\Program Files".to_owned())
}

/// `find_tailscale()`: `Ok(None)` when it is nowhere, `Err` defers.
pub(super) fn find() -> Result<Option<String>, ()> {
    if let Some(found) = which(std::env::var_os("PATH"), std::env::var_os("PATHEXT"))? {
        return Ok(Some(found));
    }
    let candidate = if cfg!(windows) {
        program_files_exe(&program_files_base([
            std::env::var_os("ProgramW6432"),
            std::env::var_os("ProgramFiles"),
        ])?)?
    } else if cfg!(target_os = "macos") {
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale".to_owned()
    } else {
        return Ok(None);
    };
    Ok(std::fs::metadata(&candidate)
        .is_ok_and(|meta| meta.is_file())
        .then_some(candidate))
}

// ── the runner ───────────────────────────────────────────────────────────────

/// `str(exc)` for the `OSError` `subprocess.run` raises when the program
/// does not start: `[WinError N] message` (trailing spaces and full stops
/// dropped, as `PyErr_SetFromWindowsErr` does), or `[Errno N] strerror:
/// 'path'` (POSIX names the executable).
fn spawn_text(error: &std::io::Error, binary: &str) -> Result<String, ()> {
    let code = error.raw_os_error().ok_or(())?;
    let text = std::io::Error::from_raw_os_error(code).to_string();
    let message = text
        .strip_suffix(&format!(" (os error {code})"))
        .ok_or(())?;
    Ok(if cfg!(windows) {
        let message = message.trim_end_matches(|c: char| c <= ' ' || c == '.');
        format!("[WinError {code}] {message}")
    } else {
        format!("[Errno {code}] {message}: {}", super::py_repr(binary))
    })
}

/// `returncode`: a POSIX signal as its negative number, a Windows exit code
/// as the unsigned DWORD.
fn returncode(status: std::process::ExitStatus) -> i64 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return -i64::from(signal);
        }
    }
    match status.code() {
        Some(code) if cfg!(windows) => i64::from(code as u32),
        Some(code) => i64::from(code),
        None => 1,
    }
}

/// Text mode's decoding: strict UTF-8, then `\r\n` and `\r` as `\n`.
fn text(bytes: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(bytes).ok()?;
    Some(text.replace("\r\n", "\n").replace('\r', "\n"))
}

/// `run_tailscale(binary, args)`. `Err` defers: output that is not UTF-8
/// (the oracle raises, or on Windows prints a reader thread's traceback and
/// goes on with `None`), or a spawn failure without an OS error code.
pub(super) async fn run(binary: &str, args: &[&str]) -> Result<Proc, ()> {
    run_within(binary, args, TIMEOUT).await
}

/// [`run`] with its timeout given (`TAILSCALE_TIMEOUT_S` outside tests).
async fn run_within(binary: &str, args: &[&str], timeout: Duration) -> Result<Proc, ()> {
    use tokio::io::AsyncReadExt;
    let mut command = tokio::process::Command::new(binary);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return Ok(Proc {
                code: 127,
                err: spawn_text(&error, binary)?,
                ..Proc::default()
            });
        }
    };
    let mut stdout = child.stdout.take().ok_or(())?;
    let mut stderr = child.stderr.take().ok_or(())?;
    let mut out = tokio::spawn(async move {
        let mut bytes = Vec::new();
        let _ = stdout.read_to_end(&mut bytes).await;
        bytes
    });
    let mut err = tokio::spawn(async move {
        let mut bytes = Vec::new();
        let _ = stderr.read_to_end(&mut bytes).await;
        bytes
    });
    // `communicate(timeout=...)`: both pipes to their end and then the
    // child, inside the one deadline. A descendant that inherited a pipe
    // keeps it open after the child has gone, so the reads are bounded too.
    let (mut read_out, mut read_err) = (None, None);
    let finished = tokio::time::timeout(timeout, async {
        read_out = Some((&mut out).await);
        read_err = Some((&mut err).await);
        child.wait().await
    })
    .await;
    match finished {
        Ok(status) => {
            let status = status.map_err(|_| ())?;
            let out = read_out.ok_or(())?.map_err(|_| ())?;
            let err = read_err.ok_or(())?.map_err(|_| ())?;
            let code = returncode(status);
            Ok(match (text(&out), text(&err)) {
                (Some(out), Some(err)) => Proc {
                    code,
                    out,
                    err,
                    unreadable: false,
                },
                _ => Proc {
                    code,
                    unreadable: true,
                    ..Proc::default()
                },
            })
        }
        Err(_) => {
            // `process.kill()`. POSIX then keeps what was read so far;
            // Windows' `communicate()` reads both pipes to their end. Only
            // stdout is kept, decoded with "replace": this port answers
            // when it is UTF-8 and defers otherwise.
            let _ = child.start_kill();
            let _ = child.wait().await;
            let out = if cfg!(windows) {
                let out = match read_out {
                    Some(done) => done,
                    None => out.await,
                }
                .map_err(|_| ())?;
                match read_err {
                    Some(done) => done,
                    None => err.await,
                }
                .map_err(|_| ())?;
                text(&out).ok_or(())?
            } else {
                tokio::task::yield_now().await;
                out.abort();
                err.abort();
                // What arrived before the kill is not reproduced here.
                return Err(());
            };
            Ok(Proc {
                code: 124,
                out,
                err: format!("timed out after {}s", timeout.as_secs()),
                unreadable: false,
            })
        }
    }
}

// ── Python text helpers ──────────────────────────────────────────────────────

/// `str.splitlines()`'s boundaries.
fn line_break(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\u{b}'
            | '\u{c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// `_first_line(text)`: the first line with something but whitespace in it,
/// stripped.
pub(super) fn first_line(text: &str) -> String {
    text.split(line_break)
        .find(|line| !pyjson::py_strip(line).is_empty())
        .map(|line| pyjson::py_strip(line).to_owned())
        .unwrap_or_default()
}

/// `_first_line(proc.stderr) or f"exit {proc.returncode}"`.
pub(super) fn detail(proc: &Proc) -> String {
    let line = first_line(&proc.err);
    if line.is_empty() {
        format!("exit {}", proc.code)
    } else {
        line
    }
}

/// `str.split()` with no argument.
fn split(text: &str) -> Vec<&str> {
    text.split(pyjson::py_space)
        .filter(|part| !part.is_empty())
        .collect()
}

// ── the readers ──────────────────────────────────────────────────────────────

/// `_too_deep(text)`: more than `JSON_DEPTH_LIMIT` brackets open at once,
/// read lexically (`[` and `{` outside strings open, `]` and `}` close),
/// whether or not the rest is valid JSON.
fn too_deep(text: &str) -> bool {
    let mut depth: i64 = 0;
    let (mut in_string, mut escaped) = (false, false);
    for byte in text.bytes() {
        if in_string {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
        } else if byte == b'"' {
            in_string = true;
        } else if byte == b'[' || byte == b'{' {
            depth += 1;
            if depth > DEPTH as i64 {
                return true;
            }
        } else if byte == b']' || byte == b'}' {
            depth -= 1;
        }
    }
    false
}

/// `_json(text)`: `Ok(Some(value))`, `Ok(None)` for text that is not JSON,
/// `Err(why)` for text nested past the bound. Under the bound `loads` never
/// reports `TooDeep`; if it did, that defers.
pub(super) fn json(text: &str) -> Result<Result<Option<J>, String>, Fault> {
    if too_deep(text) {
        return Ok(Err(format!("it is nested more than {DEPTH} levels deep")));
    }
    match pyjson::loads(text, DEPTH) {
        Ok(value) => Ok(Ok(Some(value))),
        Err(pyjson::Refused::NotJson) => Ok(Ok(None)),
        Err(pyjson::Refused::TooDeep) => Err(Fault::Defer),
    }
}

/// `_unreadable(binary, args, why)`'s text.
fn unreadable(binary: &str, args: &[&str], why: &str) -> Fault {
    Fault::Refused(format!(
        "could not read what `tailscale {}` printed ({why}): check that {binary} is Tailscale's own CLI, and reinstall Tailscale if it is",
        args.join(" ")
    ))
}

/// `_read(binary, args)`: output that is not UTF-8 is refused.
async fn read(binary: &str, args: &[&str]) -> Result<Proc, Fault> {
    let proc = run(binary, args).await.map_err(|()| Fault::Defer)?;
    if proc.unreadable {
        return Err(unreadable(binary, args, "it is not UTF-8"));
    }
    Ok(proc)
}

/// `_running(binary)`: the status dict, or the refusal.
pub(super) async fn running(binary: &str) -> Result<Dict, Fault> {
    let args = ["status", "--json"];
    let proc = read(binary, &args).await?;
    let status = if pyjson::py_strip(&proc.out).is_empty() {
        None
    } else {
        json(&proc.out)?.map_err(|why| unreadable(binary, &args, &why))?
    };
    let Some(J::Dict(status)) = status else {
        return Err(Fault::Refused(format!(
            "tailscale is not running ({}): start Tailscale and run `tailscale up`",
            detail(&proc)
        )));
    };
    let state = pyjson::get(&status, "BackendState");
    if state.and_then(J::as_str) != Some("Running") {
        let shown = state.map_or_else(|| "None".to_owned(), pyjson::py_value_repr);
        return Err(Fault::Refused(format!(
            "tailscale reports BackendState {shown}, not 'Running': run `tailscale up` (and log in) first"
        )));
    }
    Ok(status)
}

/// `_serve_config(binary)`: an empty or `null` answer is an empty config.
pub(super) async fn serve_config(binary: &str) -> Result<Dict, Fault> {
    let args = ["serve", "status", "--json"];
    let proc = read(binary, &args).await?;
    if proc.code != 0 {
        return Err(Fault::Refused(format!(
            "could not read `tailscale serve status --json` ({})",
            detail(&proc)
        )));
    }
    let text = pyjson::py_strip(&proc.out);
    if text.is_empty() || text == "null" {
        return Ok(Vec::new());
    }
    match json(text)?.map_err(|why| unreadable(binary, &args, &why))? {
        None => Err(Fault::Refused(
            "`tailscale serve status --json` printed something that is not JSON".into(),
        )),
        Some(J::Dict(config)) => Ok(config),
        Some(_) => Err(Fault::Refused(
            "`tailscale serve status --json` printed an unexpected shape".into(),
        )),
    }
}

/// `_is_ipv4(word)`: four dot-separated octets, each `0` or 1 to 3 ASCII
/// digits without a leading zero, at most 255.
fn is_ipv4(word: &str) -> bool {
    let parts: Vec<&str> = word.split('.').collect();
    parts.len() == 4
        && parts.iter().all(|part| {
            (1..=3).contains(&part.len())
                && part.bytes().all(|b| b.is_ascii_digit())
                && (*part == "0" || !part.starts_with('0'))
                && part.parse::<u16>().is_ok_and(|octet| octet <= 255)
        })
}

/// `_tailnet_ipv4(binary, status)`: `tailscale ip -4`'s words (unless it
/// failed or printed something that is not UTF-8), then the strings of
/// `Self.TailscaleIPs` when it is a list; the first address. `Err` defers.
pub(super) async fn tailnet_ipv4(binary: &str, status: &Dict) -> Result<Option<String>, ()> {
    let proc = run(binary, &["ip", "-4"]).await?;
    let mut candidates: Vec<Vec<u16>> = if proc.code == 0 && !proc.unreadable {
        split(&proc.out)
            .into_iter()
            .map(|word| word.encode_utf16().collect())
            .collect()
    } else {
        Vec::new()
    };
    let own = match pyjson::get(status, "Self") {
        Some(J::Dict(own)) => pyjson::get(own, "TailscaleIPs"),
        _ => None,
    };
    if let Some(J::List(values)) = own {
        candidates.extend(
            values
                .iter()
                .filter(|value| matches!(value, J::Str(_) | J::Wtf(_)))
                .map(J::py_str_units),
        );
    }
    // A word with a lone surrogate is no address.
    Ok(candidates
        .into_iter()
        .filter_map(|units| String::from_utf16(&units).ok())
        .find(|word| is_ipv4(word)))
}

// ── the port's state ─────────────────────────────────────────────────────────

/// `_port_state`'s first element.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum State {
    Ours,
    Foreign,
    None,
}

fn dict(value: Option<&J>) -> Option<&Dict> {
    match value {
        Some(J::Dict(dict)) => Some(dict),
        _ => None,
    }
}

/// `_handler(config, port)`.
fn handler(config: Option<&Dict>, port: u16) -> Option<&J> {
    dict(pyjson::get(config?, "TCP")).and_then(|tcp| pyjson::get(tcp, &port.to_string()))
}

fn truthy(handler: &Dict, key: &str) -> bool {
    pyjson::get(handler, key).is_some_and(J::truthy)
}

/// `_is_ours(handler, port)`.
fn is_ours(handler: &J, port: u16) -> bool {
    let J::Dict(handler) = handler else {
        return false;
    };
    pyjson::get(handler, "TCPForward").and_then(J::as_str)
        == Some(format!("127.0.0.1:{port}").as_str())
        && !truthy(handler, "TerminateTLS")
        && !truthy(handler, "HTTPS")
        && !truthy(handler, "HTTP")
        && !truthy(handler, "ProxyProtocol")
}

fn units(text: &str) -> Vec<u16> {
    text.encode_utf16().collect()
}

/// `_describe(handler)`, as UTF-16 code units (a str value may hold a lone
/// surrogate).
fn describe(handler: &J) -> Vec<u16> {
    let J::Dict(handler) = handler else {
        return units("an unreadable handler");
    };
    let value = |key: &str| {
        pyjson::get(handler, key)
            .map(J::py_str_units)
            .unwrap_or_default()
    };
    let mut parts: Vec<Vec<u16>> = Vec::new();
    if truthy(handler, "TCPForward") {
        parts.push([units("a TCP forward to "), value("TCPForward")].concat());
    }
    if truthy(handler, "TerminateTLS") {
        parts.push([units("TLS terminated for "), value("TerminateTLS")].concat());
    }
    if truthy(handler, "HTTPS") {
        parts.push(units("HTTPS"));
    }
    if truthy(handler, "HTTP") {
        parts.push(units("HTTP"));
    }
    if truthy(handler, "ProxyProtocol") {
        parts.push([units("PROXY protocol v"), value("ProxyProtocol")].concat());
    }
    if parts.is_empty() {
        return units("a handler of an unknown kind");
    }
    parts.join(&units(", ")[..])
}

/// `_funnelled(config, port)`.
fn funnelled(config: Option<&Dict>, port: u16) -> bool {
    let suffix = units(&format!(":{port}"));
    config
        .and_then(|config| dict(pyjson::get(config, "AllowFunnel")))
        .is_some_and(|funnel| {
            funnel
                .iter()
                .any(|(key, on)| on.truthy() && key.py_str_units().ends_with(&suffix))
        })
}

/// `_port_state(config, port)`, the detail as code units.
pub(super) fn port_state(config: &Dict, port: u16) -> (State, Option<Vec<u16>>) {
    let sessions: Vec<&J> = dict(pyjson::get(config, "Foreground"))
        .map(|sessions| sessions.iter().map(|(_, session)| session).collect())
        .unwrap_or_default();
    if funnelled(Some(config), port) || sessions.iter().any(|s| funnelled(dict(Some(s)), port)) {
        return (
            State::Foreign,
            Some(units(&format!("Funnel (public internet) on port {port}"))),
        );
    }
    if let Some(background) = handler(Some(config), port) {
        if is_ours(background, port) {
            return (State::Ours, None);
        }
        return (State::Foreign, Some(describe(background)));
    }
    for session in sessions {
        if let Some(held) = handler(dict(Some(session)), port) {
            return (
                State::Foreign,
                Some([units("a foreground serve ("), describe(held), units(")")].concat()),
            );
        }
    }
    (State::None, None)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A descendant that inherited the pipes keeps them open after the
    /// direct child has gone. `subprocess.run(timeout=...)` bounds the
    /// reads too and raises at the timeout, which on POSIX this port defers.
    #[cfg(unix)]
    #[tokio::test]
    async fn the_timeout_also_bounds_pipes_a_descendant_holds_open() {
        let started = std::time::Instant::now();
        let ran = run_within(
            "sh",
            &["-c", "sleep 3 & exit 0"],
            Duration::from_millis(200),
        )
        .await;
        assert!(ran.is_err());
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "{:?}",
            started.elapsed()
        );
    }

    fn read(text: &str) -> Dict {
        match pyjson::loads(text, DEPTH).unwrap() {
            J::Dict(dict) => dict,
            other => panic!("{other:?}"),
        }
    }

    fn state(text: &str, port: u16) -> (State, Option<String>) {
        let (state, detail) = port_state(&read(text), port);
        (state, detail.map(|units| String::from_utf16_lossy(&units)))
    }

    #[test]
    fn a_plain_forward_to_the_daemon_is_ours() {
        assert_eq!(
            state(
                r#"{"TCP": {"8765": {"TCPForward": "127.0.0.1:8765"}}}"#,
                8765
            ),
            (State::Ours, None)
        );
        assert_eq!(
            state(r#"{"TCP": {"8765": null}}"#, 8765),
            (State::None, None)
        );
        assert_eq!(state("{}", 8765), (State::None, None));
        // A repeated key keeps its last value.
        assert_eq!(
            state(
                r#"{"TCP": {"8765": {"HTTPS": true}, "8765": {"TCPForward": "127.0.0.1:8765"}}}"#,
                8765
            ),
            (State::Ours, None)
        );
    }

    #[test]
    fn anything_else_on_the_port_is_described_as_foreign() {
        for (handler, expected) in [
            (
                r#"{"TCPForward": "127.0.0.1:8765", "TerminateTLS": "n.example.com"}"#,
                "a TCP forward to 127.0.0.1:8765, TLS terminated for n.example.com",
            ),
            (r#"{"HTTPS": true}"#, "HTTPS"),
            (
                r#"{"HTTP": 1, "TCPForward": 5, "ProxyProtocol": 2.0}"#,
                "a TCP forward to 5, HTTP, PROXY protocol v2.0",
            ),
            (
                r#"{"TCPForward": "", "HTTPS": false}"#,
                "a handler of an unknown kind",
            ),
            ("[]", "an unreadable handler"),
            ("5", "an unreadable handler"),
        ] {
            let text = format!(r#"{{"TCP": {{"8765": {handler}}}}}"#);
            assert_eq!(
                state(&text, 8765),
                (State::Foreign, Some(expected.to_owned())),
                "{handler}"
            );
        }
        assert_eq!(
            state(
                r#"{"Foreground": {"s": {"TCP": {"8765": {"TCPForward": "127.0.0.1:8765"}}}}}"#,
                8765
            ),
            (
                State::Foreign,
                Some("a foreground serve (a TCP forward to 127.0.0.1:8765)".into())
            )
        );
    }

    #[test]
    fn funnel_on_the_port_is_never_ours() {
        let funnel = "Funnel (public internet) on port 8765".to_owned();
        assert_eq!(
            state(
                r#"{"TCP": {"8765": {"TCPForward": "127.0.0.1:8765"}}, "AllowFunnel": {"n:8765": true}}"#,
                8765
            ),
            (State::Foreign, Some(funnel.clone()))
        );
        assert_eq!(
            state(
                r#"{"Foreground": {"a": {"AllowFunnel": {"n:8765": 1}}}}"#,
                8765
            ),
            (State::Foreign, Some(funnel))
        );
        assert_eq!(
            state(
                r#"{"AllowFunnel": {"n:443": true, "n:8765": false, "n:18765": true}}"#,
                8765
            ),
            (State::None, None)
        );
    }

    #[test]
    fn first_line_is_pythons() {
        assert_eq!(first_line("\n  \n  boom  \nmore"), "boom");
        assert_eq!(first_line("\u{b}x\u{2028}y"), "x");
        assert_eq!(first_line("\u{a0}\u{1f}\n"), "");
        assert_eq!(
            detail(&Proc {
                code: 3,
                err: " \n".into(),
                ..Proc::default()
            }),
            "exit 3"
        );
    }

    #[test]
    fn split_and_the_address_shape_are_pythons() {
        assert_eq!(split("x\u{a0}127.0.0.2\u{1c}y\n"), ["x", "127.0.0.2", "y"]);
        for word in ["100.64.0.1", "0.0.0.0", "255.255.255.255", "10.0.0.250"] {
            assert!(is_ipv4(word), "{word}");
        }
        for word in [
            "1.2.3",
            "1.2.3.4.5",
            "1234.1.1.1",
            "1..1.1",
            "a.b.c.d",
            "127.0.0.1\n",
            "256.0.0.1",
            "999.1.1.1",
            "010.0.0.1",
            "1.2.3.04",
            "\u{661}\u{662}\u{667}.0.0.1",
            "\u{ff11}.2.3.4",
        ] {
            assert!(!is_ipv4(word), "{word}");
        }
    }

    #[test]
    fn nesting_is_counted_lexically_against_the_bound() {
        let nest = |depth: usize| format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        assert!(!too_deep(&nest(DEPTH)));
        assert!(too_deep(&nest(DEPTH + 1)));
        assert!(matches!(json(&nest(DEPTH)), Ok(Ok(Some(_)))));
        assert!(matches!(json(&nest(DEPTH + 1)), Ok(Err(_))));
        // Invalid text is counted too; brackets inside strings are not.
        assert!(too_deep(&format!("x{}", "{".repeat(DEPTH + 1))));
        let quoted = format!("[\"{}\\\"{}\"]", "[".repeat(200), "{".repeat(200));
        assert!(!too_deep(&quoted));
        assert!(matches!(json(&quoted), Ok(Ok(Some(_)))));
        // Closing brackets below zero are counted as Python counts them.
        assert!(!too_deep(&format!("]]{}", "[".repeat(DEPTH + 1))));
    }

    #[test]
    fn the_lookup_joins_as_os_path_join() {
        if cfg!(windows) {
            assert_eq!(join(r"C:\x", "t.CMD"), r"C:\x\t.CMD");
            assert_eq!(join("C:/x/", "t"), "C:/x/t");
            assert_eq!(join(r"C:\x/bin", "t"), r"C:\x/bin\t");
            assert_eq!(
                program_files_exe(r"C:\Program Files"),
                Ok(r"C:\Program Files\Tailscale\tailscale.exe".into())
            );
            assert_eq!(
                program_files_exe(r"C:\"),
                Ok(r"C:\Tailscale\tailscale.exe".into())
            );
            for odd in [
                "C:/Program Files",
                r"C:\a\\b",
                r"C:\a\",
                r"C:\.\a",
                "relative",
                r"\\server\share",
            ] {
                assert_eq!(program_files_exe(odd), Err(()), "{odd}");
            }
        } else {
            assert_eq!(join("/x", "t"), "/x/t");
            assert_eq!(join("/x/", "t"), "/x/t");
        }
        // An empty or unset PATH finds nothing without touching the disk.
        assert_eq!(which(Some(OsString::new()), None), Ok(None));
        assert_eq!(which(None, None), Ok(None));
    }

    /// A directory of its own under the system temporary directory.
    fn scratch(tag: &str) -> std::path::PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.as_nanos());
        let dir = std::env::temp_dir().join(format!(
            "expose-lookup-{tag}-{}-{nanos}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A stand-in program file (never run: the lookup only finds it).
    fn plant(dir: &std::path::Path) -> String {
        let path = dir.join(if cfg!(windows) {
            "tailscale.cmd"
        } else {
            "tailscale"
        });
        std::fs::write(&path, "exit 99\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        path.to_str().unwrap().to_owned()
    }

    fn path_of(entries: &[&str]) -> Option<OsString> {
        Some(OsString::from(entries.join(if cfg!(windows) {
            ";"
        } else {
            ":"
        })))
    }

    #[test]
    fn relative_and_empty_path_entries_are_never_searched() {
        let dir = scratch("relative");
        let real = dir.join("real");
        std::fs::create_dir_all(&real).unwrap();
        let found = plant(&real);
        let real = real.to_str().unwrap();
        let pathext = Some(OsString::from(".EXE;.CMD"));
        // Relative entries name a directory under the test's current one,
        // which holds no stand-in: skipping them is what is checked.
        for entry in ["", ".", "bin", "\"\"", "  "] {
            assert_eq!(
                which(path_of(&[entry]), pathext.clone()),
                Ok(None),
                "{entry:?}"
            );
            let got = which(path_of(&[entry, real]), pathext.clone())
                .unwrap()
                .unwrap();
            assert!(got.eq_ignore_ascii_case(&found), "{entry:?}: {got}");
        }
        if cfg!(windows) {
            for entry in ["C:bin", r"\bin", "/bin"] {
                assert_eq!(absolute(entry), Some(false), "{entry}");
            }
            assert_eq!(absolute("C:/x"), Some(true));
            // A relative Program Files base is passed over.
            let base = |a: &str, b: &str| program_files_base([Some(a.into()), Some(b.into())]);
            assert_eq!(base("Program Files", r"D:\pf"), Ok(r"D:\pf".into()));
            assert_eq!(base("", "C:pf"), Ok(r"C:\Program Files".into()));
            assert_eq!(base(r"E:\pf64", r"D:\pf"), Ok(r"E:\pf64".into()));
            assert_eq!(base(r"\\server\pf", r"D:\pf"), Err(()));
            assert_eq!(absolute(r"\\server\share"), None);
            assert_eq!(absolute("//server/share"), None);
            // Quotes and space around an entry are removed; the extension
            // is spelled as PATHEXT spells it.
            let quoted = format!(" \"{real}\" ");
            assert_eq!(
                which(path_of(&[&quoted]), pathext.clone()),
                Ok(Some(format!(r"{real}\tailscale.CMD")))
            );
            // Only what CreateProcess starts.
            std::fs::write(dir.join("real").join("tailscale.js"), "").unwrap();
            assert_eq!(which(path_of(&[real]), Some(".JS".into())), Ok(None));
        } else {
            assert_eq!(absolute("bin"), Some(false));
            assert_eq!(absolute("/bin"), Some(true));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn spawn_failures_read_as_cpython_oserror_text() {
        let error = std::io::Error::from_raw_os_error(if cfg!(windows) { 193 } else { 8 });
        let text = spawn_text(&error, "/x/it's").unwrap();
        if cfg!(windows) {
            assert_eq!(text, "[WinError 193] %1 is not a valid Win32 application");
        } else {
            assert_eq!(text, "[Errno 8] Exec format error: \"/x/it's\"");
        }
    }
}
