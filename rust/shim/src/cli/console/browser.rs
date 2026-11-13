//! Which browser `console_cli.open_browser` would use: CPython 3.11's
//! `webbrowser.get()` (`register_standard_browsers`, the `BROWSER`
//! variable, `_synthesize`) reduced to the outcomes `open_browser` can
//! have, decided before the sign-in code is minted so that every shape this
//! port does not reproduce defers before any effect.
//!
//! `open_browser` refuses a `GenericBrowser` that is not a
//! `BackgroundBrowser` (a terminal browser) and treats any exception from
//! `webbrowser.get()` as no browser: both print the link. The reproduced
//! launchers are a `BackgroundBrowser` (from `BROWSER` or the standard X
//! list) and, on Windows, `WindowsDefault` (`os.startfile`). Every other
//! controller (the `UnixBrowser` family but Elinks, which `open_browser`
//! refuses, Konqueror, Grail, the macOS AppleScript launchers) and a clone
//! `_synthesize` could make of one defers.
use std::ffi::OsString;

/// Which `sys.platform` branch of `webbrowser` runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Platform {
    Windows,
    Mac,
    /// Linux and any other POSIX system.
    Posix,
}

impl Platform {
    pub(super) const HERE: Platform = if cfg!(windows) {
        Platform::Windows
    } else if cfg!(target_os = "macos") {
        Platform::Mac
    } else {
        Platform::Posix
    };

    /// `os.pathsep`.
    fn pathsep(self) -> char {
        if self == Platform::Windows { ';' } else { ':' }
    }
}

/// What `open_browser(url)` does.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum Plan {
    /// It returns False without starting anything: no controller, a
    /// terminal browser, or an exception from `webbrowser.get()`.
    Refused,
    /// `BackgroundBrowser.open`: start `program` with `args`, each `%s`
    /// replaced by the URL, and report whether it is still running.
    Background { program: String, args: Vec<String> },
    /// `WindowsDefault.open`: `os.startfile(url)`.
    StartFile,
}

/// The `xdg-settings get default-web-browser` call
/// `register_standard_browsers` makes when a display is set.
#[derive(Clone, Debug, PartialEq, Eq)]
// Windows never runs the probe.
#[cfg_attr(not(unix), allow(dead_code))]
pub(super) enum Probe {
    /// `FileNotFoundError`, `PermissionError`, `NotADirectoryError` or a
    /// non-zero exit: caught, no preferred browser.
    Missing,
    /// Any other error (another `OSError`, output that is not UTF-8): it
    /// propagates out of `webbrowser.get()`.
    Raised,
    /// The output, stripped.
    Answer(String),
}

/// What the choice reads from the machine.
pub(super) trait Host {
    /// `os.environ.get(key)`, as the raw value.
    fn var(&self, key: &str) -> Option<OsString>;
    /// `shutil.which(name) is not None` (POSIX only).
    fn which(&self, name: &str) -> bool;
    /// `xdg-settings get default-web-browser` (POSIX only).
    fn probe(&self) -> Probe;
}

/// Outside the reproduced domain: defer before the code is minted.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct Unsupported;

/// `has_display(None)`: always on Windows and macOS, else a non-empty
/// `DISPLAY` or `WAYLAND_DISPLAY`.
pub(super) fn has_display(host: &dyn Host, platform: Platform) -> bool {
    platform != Platform::Posix || display(host)
}

fn display(host: &dyn Host) -> bool {
    ["DISPLAY", "WAYLAND_DISPLAY"]
        .iter()
        .any(|key| host.var(key).is_some_and(|value| !value.is_empty()))
}

/// One standard registration: how `webbrowser.get()` would build it.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Controller {
    Background(Vec<&'static str>),
    /// A plain `GenericBrowser`: refused.
    Generic,
    /// `WindowsDefault`.
    StartFile,
    /// Any controller this port does not launch.
    Other,
}

/// `register_X_browsers`, in order, for the names `which` finds.
fn x_browsers(host: &dyn Host) -> Vec<(&'static str, Controller)> {
    use Controller::{Background, Other};
    let mut found = Vec::new();
    let mut add = |name: &'static str, controller: Controller| {
        if host.which(name) {
            found.push((name, controller));
        }
    };
    add("xdg-open", Background(vec!["xdg-open", "%s"]));
    add("gio", Background(vec!["gio", "open", "--", "%s"]));
    if host.var("GNOME_DESKTOP_SESSION_ID").is_some() {
        add("gvfs-open", Background(vec!["gvfs-open", "%s"]));
    }
    if host.var("KDE_FULL_SESSION").is_some() {
        add("kfmclient", Other);
    }
    add("x-www-browser", Background(vec!["x-www-browser", "%s"]));
    for name in ["firefox", "iceweasel", "iceape", "seamonkey"] {
        add(name, Other);
    }
    for name in [
        "mozilla-firefox",
        "mozilla-firebird",
        "firebird",
        "mozilla",
        "netscape",
    ] {
        add(name, Other);
    }
    if host.which("kfm") {
        add("kfm", Other);
    } else {
        add("konqueror", Other);
    }
    for name in ["galeon", "epiphany"] {
        add(name, Other);
    }
    add("skipstone", Background(vec!["skipstone", "%s"]));
    for name in ["google-chrome", "chrome", "chromium", "chromium-browser"] {
        add(name, Other);
    }
    add("opera", Other);
    add("mosaic", Background(vec!["mosaic", "%s"]));
    add("grail", Other);
    found
}

/// The console browsers registered when `TERM` is set. `Elinks` is a
/// `UnixBrowser`, not a `GenericBrowser`, but `open_browser` refuses it by
/// name, so it reads as one here.
fn console_browsers(host: &dyn Host) -> Vec<(&'static str, Controller)> {
    use Controller::Generic;
    [
        ("www-browser", Generic),
        ("links", Generic),
        ("elinks", Generic),
        ("lynx", Generic),
        ("w3m", Generic),
    ]
    .into_iter()
    .filter(|(name, _)| host.which(name))
    .collect()
}

/// `register_standard_browsers` up to the `BROWSER` step: the try order,
/// or `Err(())` when it raises.
fn standard(host: &dyn Host, platform: Platform) -> Result<Vec<(&'static str, Controller)>, ()> {
    let mut order: Vec<(&'static str, Controller)> = Vec::new();
    if platform == Platform::Mac {
        for name in ["MacOSX", "chrome", "firefox", "safari"] {
            order.push((name, Controller::Other));
        }
    }
    if platform == Platform::Windows {
        // The detected Windows browsers come after it and never decide.
        order.push(("windows-default", Controller::StartFile));
        return Ok(order);
    }
    let mut preferred = None;
    let mut registered = Vec::new();
    if display(host) {
        match host.probe() {
            Probe::Raised => return Err(()),
            Probe::Missing => {}
            Probe::Answer(text) => preferred = Some(text),
        }
        registered.extend(x_browsers(host));
    }
    if host.var("TERM").is_some_and(|value| !value.is_empty()) {
        registered.extend(console_browsers(host));
    }
    for (name, controller) in registered {
        // `register`: a name inside xdg-settings' answer goes first.
        if preferred
            .as_deref()
            .is_some_and(|text| !text.is_empty() && text.contains(name))
        {
            order.insert(0, (name, controller));
        } else {
            order.push((name, controller));
        }
    }
    Ok(order)
}

/// Every name a standard registration can carry an instance under, on any
/// platform: a `BROWSER` entry whose first word has one of these basenames
/// may be cloned from that controller (`_synthesize`), so it defers.
const STANDARD_NAMES: [&str; 35] = [
    "macosx",
    "chrome",
    "firefox",
    "safari",
    "browser",
    "windows-default",
    "firebird",
    "seamonkey",
    "mozilla",
    "netscape",
    "opera",
    "xdg-open",
    "gio",
    "gvfs-open",
    "kfmclient",
    "x-www-browser",
    "iceweasel",
    "iceape",
    "mozilla-firefox",
    "mozilla-firebird",
    "kfm",
    "konqueror",
    "galeon",
    "epiphany",
    "skipstone",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "mosaic",
    "grail",
    "www-browser",
    "links",
    "elinks",
    "lynx",
    "w3m",
];

/// `os.path.basename`.
fn basename(path: &str, platform: Platform) -> &str {
    let split = |c: char| c == '/' || (platform == Platform::Windows && c == '\\');
    let path = if platform == Platform::Windows {
        // ntpath.splitdrive: whatever precedes a colon in second place is
        // the drive (any character, not only a letter), not part of the name.
        let mut rest = path.chars();
        match (rest.next(), rest.next()) {
            (Some(_), Some(':')) => rest.as_str(),
            _ => path,
        }
    } else {
        path
    };
    path.rsplit(split).next().unwrap_or(path)
}

/// Whether a `BackgroundBrowser` program starts the same way in both
/// launchers. POSIX: CPython's `execve` over `PATH` is reproduced exactly.
/// Windows: `CreateProcess(NULL, cmdline)` searches the interpreter's own
/// directory and the current directory for a bare name, where Rust does
/// not, and runs a batch file through `cmd.exe`; only a drive-absolute
/// `.exe` path with no quote is reproduced.
fn launchable(program: &str, platform: Platform) -> bool {
    if platform != Platform::Windows {
        return true;
    }
    let bytes = program.as_bytes();
    bytes.len() > 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'\\' | b'/')
        && !program.contains('"')
        && program
            .get(program.len() - 4..)
            .is_some_and(|ext| ext.eq_ignore_ascii_case(".exe"))
}

/// `webbrowser.get()` as `open_browser` sees it, when a display is there.
pub(super) fn choose(host: &dyn Host, platform: Platform) -> Result<Plan, Unsupported> {
    let order = match standard(host, platform) {
        Ok(order) => order,
        Err(()) => return Ok(Plan::Refused),
    };
    let choices = match host.var("BROWSER") {
        None => Vec::new(),
        Some(value) => value
            .into_string()
            .map_err(|_| Unsupported)?
            .split(platform.pathsep())
            .filter(|entry| !entry.is_empty())
            .map(str::to_owned)
            .collect(),
    };
    // `_synthesize(cmdline)` runs for every entry, last first, and raises
    // IndexError on one that is only whitespace.
    if choices
        .iter()
        .any(|entry| entry.split(super::pyjson::py_space).all(str::is_empty))
    {
        return Ok(Plan::Refused);
    }
    if let Some(first) = choices.first() {
        // Every entry goes to the front of the try order, the first one first.
        return from_browser_variable(first, platform);
    }
    Ok(match order.into_iter().next() {
        None => Plan::Refused,
        Some((_, Controller::StartFile)) => Plan::StartFile,
        Some((_, Controller::Generic)) => Plan::Refused,
        Some((_, Controller::Other)) => return Err(Unsupported),
        Some((_, Controller::Background(command))) => Plan::Background {
            program: command[0].to_owned(),
            args: command[1..].iter().map(|arg| (*arg).to_owned()).collect(),
        },
    })
}

/// `get()` for the first `BROWSER` entry.
fn from_browser_variable(entry: &str, platform: Platform) -> Result<Plan, Unsupported> {
    if entry.contains("%s") {
        let Ok(mut words) = shlex_split(entry) else {
            return Ok(Plan::Refused);
        };
        if words.last().map(String::as_str) != Some("&") {
            return Ok(Plan::Refused);
        }
        words.pop();
        // `%s` is in some word, so one is left.
        let program = words.remove(0);
        if !launchable(&program, platform) {
            return Err(Unsupported);
        }
        return Ok(Plan::Background {
            program,
            args: words,
        });
    }
    let first = entry
        .split(super::pyjson::py_space)
        .find(|word| !word.is_empty())
        .unwrap_or_default();
    let name = basename(first, platform).to_lowercase();
    if STANDARD_NAMES.contains(&name.as_str()) {
        return Err(Unsupported);
    }
    // `register(cmdline, None, GenericBrowser(cmdline))`: refused.
    Ok(Plan::Refused)
}

/// `shlex.split(text)` (POSIX mode, no comments): the words, or `Err` for
/// the `ValueError` it raises on an open quote or a trailing backslash.
pub(super) fn shlex_split(text: &str) -> Result<Vec<String>, ()> {
    #[derive(Clone, Copy, PartialEq)]
    enum State {
        Space,
        Word,
        Quote(char),
        Escape(char),
    }
    let whitespace = |c: char| matches!(c, ' ' | '\t' | '\r' | '\n');
    let mut words = Vec::new();
    let mut token = String::new();
    let mut quoted = false;
    let mut state = State::Space;
    for c in text.chars() {
        match state {
            State::Space | State::Word => {
                if whitespace(c) {
                    if state == State::Word || quoted {
                        words.push(std::mem::take(&mut token));
                        quoted = false;
                    }
                    state = State::Space;
                } else if c == '\\' {
                    state = State::Escape('a');
                } else if c == '\'' || c == '"' {
                    state = State::Quote(c);
                } else {
                    token.push(c);
                    state = State::Word;
                }
            }
            State::Quote(quote) => {
                quoted = true;
                if c == quote {
                    state = State::Word;
                } else if c == '\\' && quote == '"' {
                    state = State::Escape(quote);
                } else {
                    token.push(c);
                }
            }
            State::Escape(before) => {
                // Inside double quotes only the quote or a backslash is escaped.
                if before == '"' && c != '\\' && c != '"' {
                    token.push('\\');
                }
                token.push(c);
                state = if before == 'a' {
                    State::Word
                } else {
                    State::Quote(before)
                };
            }
        }
    }
    match state {
        State::Quote(_) | State::Escape(_) => Err(()),
        State::Word => {
            words.push(token);
            Ok(words)
        }
        State::Space => {
            if quoted {
                words.push(token);
            }
            Ok(words)
        }
    }
}

#[cfg(test)]
#[path = "browser_tests.rs"]
mod tests;
