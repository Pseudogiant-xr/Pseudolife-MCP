//! The `tailscale` CLI as `expose` uses it: the lookup (absolute `PATH`
//! directories only, never the current directory, then the default install
//! locations), the runner (stdin closed, both pipes read, 30 s bound), and
//! what is read from it (`status --json`, `serve status --json`, `ip -4`,
//! the port's state).
//!
//! Values are `pyjson::J`, so `dict.get`, truthiness, `str()` and `repr()`
//! are Python's. Text is strict UTF-8 with newlines translated; output that
//! is not UTF-8 is `unreadable`.
use super::super::program_lookup::{self, is_ipv4};
use super::super::pyjson::{self, Dict, J};
use super::Host;
use std::{
    path::PathBuf,
    process::Stdio,
    sync::{Arc, Mutex, PoisonError},
    time::Duration,
};

/// The fix for a user Tailscale does not let change its serve config, by OS.
/// Forwarding a port needs neither root nor an Administrator console
/// (Tailscale's Serve documentation, "Limitations"); what it needs is a user
/// Tailscale trusts. On Linux that is its operator, a grant made once with
/// one privileged command; on Windows and macOS it is the account that owns
/// the Tailscale configuration, and there is no grant to give another one.
/// The one wording for `expose` and `maintainer setup`, keyed by
/// `std::env::consts::OS` so either platform's text is testable anywhere.
pub(crate) fn permission_fix(os: &str) -> &'static str {
    match os {
        "windows" => {
            "run it as the Windows user that owns this device's Tailscale configuration (the one signed in to the Tailscale app, or the one that turned on unattended mode)"
        }
        "macos" => "run it as the macOS user logged in to the Tailscale app",
        _ => {
            "make this user Tailscale's operator once with `sudo tailscale set --operator=$USER` (the one privileged step; after it this user changes serves without sudo)"
        }
    }
}

/// `TAILSCALE_TIMEOUT_S`: a design bound, not a measurement; a tailscale
/// CLI call that takes longer is wedged.
pub(super) const TIMEOUT: Duration = Duration::from_secs(30);
/// After a timeout's kill, how long the pipes may stay open (a descendant
/// that inherited them) before what they gave so far is kept. A design
/// bound: the kill closes the child's own ends at once.
const DRAIN: Duration = Duration::from_secs(2);
/// `JSON_DEPTH_LIMIT`: an answer with more brackets open at once is
/// refused as unreadable (`_too_deep`).
pub(super) const DEPTH: usize = pyjson::EXPOSE_DEPTH;

/// The `tailscale` program found: the path to start and how reports name it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Binary {
    pub(super) path: PathBuf,
    pub(super) shown: String,
}

/// One finished `tailscale` call. `unreadable`: its output was not UTF-8,
/// so `out` and `err` are empty. `timed_out`: it was killed at the bound;
/// `out` is what it printed before (decoded with replacement), `err` says
/// so, `code` is 124, and `err_before` is what it wrote to stderr before.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub(super) struct Proc {
    pub(super) code: i64,
    pub(super) out: String,
    pub(super) err: String,
    pub(super) unreadable: bool,
    pub(super) timed_out: bool,
    pub(super) err_before: String,
}

impl Proc {
    /// It ran to the end and exited 0.
    pub(super) fn succeeded(&self) -> bool {
        self.code == 0 && !self.timed_out
    }
}

// ── the lookup ───────────────────────────────────────────────────────────────

/// `find_tailscale()`: an absolute `PATH` directory's `tailscale`, then the
/// default install location. Where the oracle's spelling of the path is not
/// reproduced (an entry that is not text, a share root, an unusual Program
/// Files value), the program is still found and named as the OS spells it.
pub(super) fn find() -> Option<Binary> {
    if let Ok(found) = program_lookup::find_tailscale() {
        return found.map(|shown| Binary {
            path: PathBuf::from(&shown),
            shown,
        });
    }
    program_lookup::find_program_path("tailscale")
        .or_else(|| program_lookup::tailscale_install_location().ok().flatten())
        .map(|path| Binary {
            shown: path.to_string_lossy().into_owned(),
            path,
        })
}

// ── the runner ───────────────────────────────────────────────────────────────

/// `str(exc)` for the `OSError` a program that does not start raises:
/// `[WinError N] message` (trailing spaces and full stops dropped, as
/// `PyErr_SetFromWindowsErr` does), or `[Errno N] strerror: 'path'` (POSIX
/// names the executable); the error's own text when it has no OS code.
fn spawn_text(error: &std::io::Error, binary: &str) -> String {
    let Some(code) = error.raw_os_error() else {
        return error.to_string();
    };
    let text = std::io::Error::from_raw_os_error(code).to_string();
    let Some(message) = text.strip_suffix(&format!(" (os error {code})")) else {
        return text;
    };
    if cfg!(windows) {
        let message = message.trim_end_matches(|c: char| c <= ' ' || c == '.');
        format!("[WinError {code}] {message}")
    } else {
        format!("[Errno {code}] {message}: {}", super::py_repr(binary))
    }
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

/// Newlines as text mode reads them: `\r\n` and `\r` as `\n`.
fn newlines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

/// Text mode's decoding: strict UTF-8, then newlines translated.
fn text(bytes: &[u8]) -> Option<String> {
    std::str::from_utf8(bytes).ok().map(newlines)
}

/// A pipe read to its end in the background, into a buffer that keeps what
/// arrived even if the read is abandoned.
fn drain<R>(pipe: Option<R>) -> (Arc<Mutex<Vec<u8>>>, tokio::task::JoinHandle<()>)
where
    R: tokio::io::AsyncRead + Unpin + Send + 'static,
{
    use tokio::io::AsyncReadExt;
    let buffer = Arc::new(Mutex::new(Vec::new()));
    let sink = Arc::clone(&buffer);
    let task = tokio::spawn(async move {
        let Some(mut pipe) = pipe else {
            return;
        };
        let mut chunk = [0_u8; 8192];
        while let Ok(count @ 1..) = pipe.read(&mut chunk).await {
            sink.lock()
                .unwrap_or_else(PoisonError::into_inner)
                .extend_from_slice(&chunk[..count]);
        }
    });
    (buffer, task)
}

fn taken(buffer: &Mutex<Vec<u8>>) -> Vec<u8> {
    std::mem::take(&mut *buffer.lock().unwrap_or_else(PoisonError::into_inner))
}

/// `run_tailscale(binary, args)` with [`TIMEOUT`]. Always answers: a
/// program that does not start is exit 127 with the OS error's text, and
/// one still running at the bound is killed and reported as timed out with
/// what it printed so far (a serve waiting for Serve to be enabled prints
/// the link that does it, then waits).
pub(super) async fn run(binary: &Binary, args: &[&str]) -> Proc {
    run_within(binary, args, TIMEOUT).await
}

/// [`run`] with its timeout given.
async fn run_within(binary: &Binary, args: &[&str], timeout: Duration) -> Proc {
    let mut command = tokio::process::Command::new(&binary.path);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return Proc {
                code: 127,
                err: spawn_text(&error, &binary.shown),
                ..Proc::default()
            };
        }
    };
    let (out, mut out_task) = drain(child.stdout.take());
    let (err, mut err_task) = drain(child.stderr.take());
    // Both pipes to their end and then the child, inside the one deadline:
    // a descendant that inherited a pipe keeps it open after the child has
    // gone, so the reads are bounded too.
    let finished = tokio::time::timeout(timeout, async {
        let _ = (&mut out_task).await;
        let _ = (&mut err_task).await;
        child.wait().await
    })
    .await;
    match finished {
        Ok(Ok(status)) => {
            let code = returncode(status);
            match (text(&taken(&out)), text(&taken(&err))) {
                (Some(out), Some(err)) => Proc {
                    code,
                    out,
                    err,
                    ..Proc::default()
                },
                _ => Proc {
                    code,
                    unreadable: true,
                    ..Proc::default()
                },
            }
        }
        Ok(Err(error)) => Proc {
            code: 1,
            err: format!("could not wait for it to finish: {error}"),
            ..Proc::default()
        },
        Err(_) => {
            let _ = child.start_kill();
            let _ = tokio::time::timeout(DRAIN, child.wait()).await;
            // A handle the first wait already saw finish must not be
            // polled again (Tokio panics on that).
            let _ = tokio::time::timeout(DRAIN, async {
                if !out_task.is_finished() {
                    let _ = (&mut out_task).await;
                }
                if !err_task.is_finished() {
                    let _ = (&mut err_task).await;
                }
            })
            .await;
            out_task.abort();
            err_task.abort();
            Proc {
                code: 124,
                out: newlines(&String::from_utf8_lossy(&taken(&out))),
                err: format!("timed out after {}s", timeout.as_secs()),
                unreadable: false,
                timed_out: true,
                err_before: newlines(&String::from_utf8_lossy(&taken(&err))),
            }
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

/// `_too_deep(text)`.
fn too_deep(text: &str) -> bool {
    pyjson::brackets_past(text, DEPTH)
}

/// `_json(text)`: `Ok(Some(value))`, `Ok(None)` for text that is not JSON,
/// `Err(why)` for text nested past the bound.
pub(super) fn json(text: &str) -> Result<Option<J>, String> {
    let nested = || format!("it is nested more than {DEPTH} levels deep");
    if too_deep(text) {
        return Err(nested());
    }
    match pyjson::loads(text, DEPTH) {
        Ok(value) => Ok(Some(value)),
        Err(pyjson::Refused::NotJson) => Ok(None),
        Err(pyjson::Refused::TooDeep) => Err(nested()),
    }
}

/// `_unreadable(binary, args, why)`'s text.
fn unreadable(binary: &Binary, args: &[&str], why: &str) -> String {
    format!(
        "could not read what `tailscale {}` printed ({why}): check that {} is Tailscale's own CLI, and reinstall Tailscale if it is",
        args.join(" "),
        binary.shown
    )
}

/// `_read(binary, args)`: output that is not UTF-8 is refused.
async fn read(host: &impl Host, binary: &Binary, args: &[&str]) -> Result<Proc, String> {
    let proc = host.run(binary, args).await;
    if proc.unreadable {
        return Err(unreadable(binary, args, "it is not UTF-8"));
    }
    Ok(proc)
}

/// `_running(binary)`: the status dict, or the refusal.
pub(super) async fn running(host: &impl Host, binary: &Binary) -> Result<Dict, String> {
    let args = ["status", "--json"];
    let proc = read(host, binary, &args).await?;
    let status = if pyjson::py_strip(&proc.out).is_empty() {
        None
    } else {
        json(&proc.out).map_err(|why| unreadable(binary, &args, &why))?
    };
    let Some(J::Dict(status)) = status else {
        return Err(format!(
            "tailscale is not running ({}): start Tailscale and run `tailscale up`",
            detail(&proc)
        ));
    };
    let state = pyjson::get(&status, "BackendState");
    if state.and_then(J::as_str) != Some("Running") {
        let shown = state.map_or_else(|| "None".to_owned(), pyjson::py_value_repr);
        return Err(format!(
            "tailscale reports BackendState {shown}, not 'Running': run `tailscale up` (and log in) first"
        ));
    }
    Ok(status)
}

/// `_serve_config(binary)`: an empty or `null` answer is an empty config.
pub(super) async fn serve_config(host: &impl Host, binary: &Binary) -> Result<Dict, String> {
    let args = ["serve", "status", "--json"];
    let proc = read(host, binary, &args).await?;
    if !proc.succeeded() {
        return Err(format!(
            "could not read `tailscale serve status --json` ({})",
            detail(&proc)
        ));
    }
    let text = pyjson::py_strip(&proc.out);
    if text.is_empty() || text == "null" {
        return Ok(Vec::new());
    }
    match json(text).map_err(|why| unreadable(binary, &args, &why))? {
        None => Err("`tailscale serve status --json` printed something that is not JSON".into()),
        Some(J::Dict(config)) => Ok(config),
        Some(_) => Err("`tailscale serve status --json` printed an unexpected shape".into()),
    }
}

/// `_tailnet_ipv4(binary, status)`: `tailscale ip -4`'s words (unless it
/// failed or printed something that is not UTF-8), then the strings of
/// `Self.TailscaleIPs` when it is a list; the first address.
pub(super) async fn tailnet_ipv4(
    host: &impl Host,
    binary: &Binary,
    status: &Dict,
) -> Option<String> {
    let proc = host.run(binary, &["ip", "-4"]).await;
    let mut candidates: Vec<Vec<u16>> = if proc.succeeded() && !proc.unreadable {
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
    candidates
        .into_iter()
        .filter_map(|units| String::from_utf16(&units).ok())
        .find(|word| is_ipv4(word))
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

/// The background handler on the port is still this daemon's plain
/// forward to `127.0.0.1:<port>`, whatever is set on the port beside it
/// (Funnel reads as someone else's port in [`port_state`], but would hide
/// the forward): what a removal is judged by.
pub(super) fn still_forwards(config: &Dict, port: u16) -> bool {
    handler(Some(config), port).is_some_and(|handler| is_ours(handler, port))
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

    #[cfg(unix)]
    fn sh() -> Binary {
        Binary {
            path: "sh".into(),
            shown: "sh".into(),
        }
    }

    /// A descendant that inherited the pipes keeps them open after the
    /// direct child has gone: the reads are bounded too, and what arrived
    /// before the timeout is kept.
    #[cfg(unix)]
    #[tokio::test]
    async fn the_timeout_also_bounds_pipes_a_descendant_holds_open() {
        let started = std::time::Instant::now();
        let ran = run_within(
            &sh(),
            &["-c", "echo waiting; sleep 30 & exit 0"],
            Duration::from_millis(200),
        )
        .await;
        assert!(ran.timed_out, "{ran:?}");
        assert_eq!(ran.code, 124);
        assert_eq!(
            ran.out,
            "waiting
"
        );
        assert_eq!(ran.err, "timed out after 0s");
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "{:?}",
            started.elapsed()
        );
    }

    /// Stdout reaches its end before the bound while stderr and the child
    /// stay open: the timeout is still reported, never a panic.
    #[cfg(unix)]
    #[tokio::test]
    async fn a_pipe_that_closed_before_the_timeout_is_not_awaited_again() {
        let ran = run_within(
            &sh(),
            &["-c", "echo early; exec 1>&-; exec sleep 30"],
            Duration::from_millis(300),
        )
        .await;
        assert!(ran.timed_out, "{ran:?}");
        assert_eq!(ran.out, "early\n");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn what_a_wedged_program_wrote_to_stderr_is_kept_too() {
        let ran = run_within(
            &sh(),
            &[
                "-c",
                "echo 'visit https://login.example.com/e' >&2; exec sleep 30",
            ],
            Duration::from_millis(300),
        )
        .await;
        assert!(ran.timed_out, "{ran:?}");
        assert_eq!(ran.err_before, "visit https://login.example.com/e\n");
    }

    /// A program still running at the bound is killed; its partial output
    /// (a serve waiting for Serve to be enabled prints the link) is kept.
    #[cfg(unix)]
    #[tokio::test]
    async fn a_wedged_program_is_killed_and_what_it_printed_is_kept() {
        let started = std::time::Instant::now();
        let ran = run_within(
            &sh(),
            &[
                "-c",
                "printf 'To enable, visit:\n  https://login.example.com/x\n'; exec sleep 30",
            ],
            Duration::from_millis(300),
        )
        .await;
        assert!(ran.timed_out, "{ran:?}");
        assert!(!ran.succeeded());
        assert_eq!(
            ran.out,
            "To enable, visit:
  https://login.example.com/x
"
        );
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_finished_program_answers_its_code_and_text() {
        let ran = run_within(&sh(), &["-c", "echo out; echo err >&2; exit 3"], TIMEOUT).await;
        assert_eq!(
            ran,
            Proc {
                code: 3,
                out: "out
"
                .into(),
                err: "err
"
                .into(),
                ..Proc::default()
            }
        );
        let ran = run_within(&sh(), &["-c", r"printf '\377'"], TIMEOUT).await;
        assert!(ran.unreadable && ran.out.is_empty());
    }

    #[tokio::test]
    async fn a_program_that_does_not_start_is_exit_127_with_the_os_text() {
        let missing = Binary {
            path: std::env::temp_dir().join("pseudolife-no-such-program-w15"),
            shown: "missing".into(),
        };
        let ran = run_within(&missing, &[], TIMEOUT).await;
        assert_eq!(ran.code, 127);
        assert!(!ran.err.is_empty() && !ran.timed_out);
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

    /// Re-executed by the Windows timeout test: given [`WEDGE_MARKER`] as a
    /// second filter (which matches no test) it prints its process id and
    /// wedges; in any other run (a libtest `--ignored` run, or nextest's
    /// `--run-ignored`, which pass no such word) it returns at once.
    #[test]
    #[ignore = "a helper the Windows timeout test re-executes"]
    fn wedge_helper() {
        use std::io::Write;
        if std::env::args().any(|arg| arg == WEDGE_MARKER) {
            println!("wedged pid={}", std::process::id());
            let _ = std::io::stdout().flush();
            std::thread::sleep(Duration::from_secs(120));
        }
    }

    /// The helper's full name, as the Windows timeout test filters on it.
    #[cfg(windows)]
    const WEDGE_HELPER: &str = "cli::expose::tailscale::tests::wedge_helper";
    const WEDGE_MARKER: &str = "pseudolife-expose-wedge-helper-marker";

    /// The Windows kill path: a child still running at the bound (this test
    /// binary, re-executed as [`wedge_helper`]; a console program that
    /// shares this console, so no window opens) is killed and reaped, what
    /// it printed is kept, and the answer is the timeout's.
    #[cfg(windows)]
    #[allow(unsafe_code)]
    #[tokio::test]
    async fn a_wedged_program_on_windows_is_killed_reaped_and_reported() {
        use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0};
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_SYNCHRONIZE, WaitForSingleObject,
        };
        let me = Binary {
            path: std::env::current_exe().expect("the test binary"),
            shown: "the test binary".into(),
        };
        let started = std::time::Instant::now();
        let ran = run_within(
            &me,
            &[
                WEDGE_HELPER,
                WEDGE_MARKER,
                "--exact",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ],
            Duration::from_secs(10),
        )
        .await;
        let elapsed = started.elapsed();
        assert!(ran.timed_out, "{ran:?}");
        assert!(!ran.succeeded());
        assert_eq!((ran.code, ran.err.as_str()), (124, "timed out after 10s"));
        // The bound, then the kill: without it the 2 s reap and the 2 s
        // drain would both run out (about 14 s), and only `kill_on_drop`
        // would end the child.
        assert!(elapsed < Duration::from_secs(13), "{elapsed:?}");
        let pid: u32 = ran
            .out
            .split("wedged pid=")
            .nth(1)
            .and_then(|rest| rest.split_whitespace().next())
            .and_then(|pid| pid.parse().ok())
            .unwrap_or_else(|| panic!("the helper printed its pid: {ran:?}"));
        // SAFETY: a process handle opened here and closed before returning.
        let gone = unsafe {
            let handle = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
            if handle.is_null() {
                true
            } else {
                let waited = WaitForSingleObject(handle, 2000);
                CloseHandle(handle);
                waited == WAIT_OBJECT_0
            }
        };
        assert!(gone, "the wedged child {pid} is still running");
    }

    #[test]
    fn a_forward_is_judged_by_its_handler_whatever_else_is_on_the_port() {
        let config = read(
            r#"{"TCP": {"8765": {"TCPForward": "127.0.0.1:8765", "TerminateTLS": "n"}}, "AllowFunnel": {"n:8765": true}}"#,
        );
        assert!(
            !still_forwards(&config, 8765),
            "a TLS forward is not this daemon's"
        );
        let funnel_beside = read(
            r#"{"TCP": {"8765": {"TCPForward": "127.0.0.1:8765"}}, "AllowFunnel": {"n:8765": true}}"#,
        );
        assert!(still_forwards(&funnel_beside, 8765));
        assert!(!still_forwards(
            &read(r#"{"TCP": {"8765": {"HTTPS": true}}}"#),
            8765
        ));
        assert!(!still_forwards(
            &read(r#"{"TCP": {"9": {"TCPForward": "127.0.0.1:8765"}}}"#),
            8765
        ));
        assert!(!still_forwards(&read("{}"), 8765));
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
        assert!(matches!(json(&nest(DEPTH)), Ok(Some(_))));
        assert!(json(&nest(DEPTH + 1)).is_err());
        // Invalid text is counted too; brackets inside strings are not.
        assert!(too_deep(&format!("x{}", "{".repeat(DEPTH + 1))));
        let quoted = format!("[\"{}\\\"{}\"]", "[".repeat(200), "{".repeat(200));
        assert!(!too_deep(&quoted));
        assert!(matches!(json(&quoted), Ok(Some(_))));
        // Closing brackets below zero are counted as Python counts them.
        assert!(!too_deep(&format!("]]{}", "[".repeat(DEPTH + 1))));
    }

    #[test]
    fn spawn_failures_read_as_cpython_oserror_text() {
        let error = std::io::Error::from_raw_os_error(if cfg!(windows) { 193 } else { 8 });
        let text = spawn_text(&error, "/x/it's");
        if cfg!(windows) {
            assert_eq!(text, "[WinError 193] %1 is not a valid Win32 application");
        } else {
            assert_eq!(text, "[Errno 8] Exec format error: \"/x/it's\"");
        }
        let plain = std::io::Error::other("no code");
        assert_eq!(spawn_text(&plain, "x"), "no code");
    }
}
