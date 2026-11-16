//! Which browser the Console sign-in starts, decided before the sign-in
//! code is minted.
//!
//! macOS, Linux and the other POSIX systems: a `BROWSER` entry decides when
//! one is set (the first non-empty one, split on `:`); otherwise macOS
//! runs `/usr/bin/open URL` and a POSIX desktop the first of
//! [`POSIX_LAUNCHERS`] that `PATH` holds. Nothing there defers: every
//! outcome either starts one program or prints the link with its cause.
//!
//! Windows uses its default browser without `BROWSER`; an explicit value
//! must name a drive-absolute `.exe`. Quotes group words and backslashes
//! stay literal, so ordinary Windows paths work. No browser choice defers.
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
}

/// What the sign-in does once it has a safe link.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum Plan {
    /// Start nothing and print the link. The text is the cause and its next
    /// step, printed beside the link.
    Refused(Option<String>),
    /// Start `program` with `args`, each `%s` replaced by the URL (the URL
    /// is always one argument, never read by a shell).
    Background { program: String, args: Vec<String> },
    /// Windows `os.startfile(url)`.
    StartFile,
}

/// What the choice reads from the machine.
pub(super) trait Host {
    /// `os.environ.get(key)`, as the raw value.
    fn var(&self, key: &str) -> Option<OsString>;
    /// Whether `PATH` holds an executable `name` (POSIX only).
    fn which(&self, name: &str) -> bool;
}

/// A POSIX graphical session: a non-empty `DISPLAY` or `WAYLAND_DISPLAY`
/// (macOS and Windows never read them).
fn display(host: &dyn Host) -> bool {
    ["DISPLAY", "WAYLAND_DISPLAY"]
        .iter()
        .any(|key| host.var(key).is_some_and(|value| !value.is_empty()))
}

/// The default launchers of a POSIX desktop without `BROWSER`, in the order
/// tried; only the first one `PATH` holds is started. The desktop's own
/// opener comes first because it follows the user's default browser
/// (`xdg-open`, then GLib's `gio open`), then Debian's alternatives link,
/// then the common browsers themselves.
pub(super) const POSIX_LAUNCHERS: [(&str, &[&str]); 7] = [
    ("xdg-open", &["%s"]),
    ("gio", &["open", "--", "%s"]),
    ("x-www-browser", &["%s"]),
    ("firefox", &["%s"]),
    ("google-chrome", &["%s"]),
    ("chromium", &["%s"]),
    ("chromium-browser", &["%s"]),
];

/// The macOS launcher, by absolute path, so no `PATH` entry can stand in.
pub(super) const MAC_OPEN: &str = "/usr/bin/open";

/// Terminal browsers: started in the background they would fight this
/// command for the terminal, so a `BROWSER` naming one prints the link.
const TERMINAL_BROWSERS: [&str; 7] = [
    "www-browser",
    "links",
    "links2",
    "elinks",
    "lynx",
    "w3m",
    "browsh",
];

/// The next step every POSIX `BROWSER` refusal and launch failure names.
pub(super) const SET_BROWSER: &str = "set BROWSER to the command that opens your browser (for example BROWSER='firefox %s'), or open the printed link yourself";

/// Why a POSIX host without `BROWSER` and without a display starts nothing.
pub(super) const NO_DISPLAY: &str =
    "no graphical display here (DISPLAY and WAYLAND_DISPLAY are unset) and no BROWSER set";

/// The sign-in's choice when a browser may open (no `--no-open`).
pub(super) fn choose(host: &dyn Host, platform: Platform) -> Plan {
    if platform == Platform::Windows {
        return choose_windows(host);
    }
    choose_posix(host, platform)
}

fn refused(cause: String) -> Plan {
    Plan::Refused(Some(cause))
}

/// macOS and the other POSIX systems.
fn choose_posix(host: &dyn Host, platform: Platform) -> Plan {
    if let Some(value) = host.var("BROWSER") {
        let Ok(value) = value.into_string() else {
            return refused(format!(
                "the BROWSER variable is not valid UTF-8; {SET_BROWSER}"
            ));
        };
        // A BROWSER with any entry decides alone, display or not (a remote
        // terminal names its own opener there): it never falls back to the
        // system default, which is what keeps a test's stand-in in charge.
        // An empty BROWSER (or only `:`) is no BROWSER.
        if let Some(entry) = value.split(':').find(|entry| !entry.is_empty()) {
            return from_posix_entry(entry);
        }
    }
    if platform == Platform::Mac {
        if ["SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"]
            .iter()
            .any(|key| host.var(key).is_some_and(|value| !value.is_empty()))
        {
            return refused(format!(
                "this is an SSH session; the Mac's browser is on its own screen; {SET_BROWSER}"
            ));
        }
        return Plan::Background {
            program: MAC_OPEN.to_owned(),
            args: vec!["%s".to_owned()],
        };
    }
    if !display(host) {
        return refused(format!("{NO_DISPLAY}; {SET_BROWSER}"));
    }
    for (name, args) in POSIX_LAUNCHERS {
        if host.which(name) {
            return Plan::Background {
                program: name.to_owned(),
                args: args.iter().map(|arg| (*arg).to_owned()).collect(),
            };
        }
    }
    let names: Vec<&str> = POSIX_LAUNCHERS.iter().map(|(name, _)| *name).collect();
    refused(format!(
        "no browser launcher found on PATH (looked for {}); install xdg-utils, or {SET_BROWSER}",
        names.join(", ")
    ))
}

/// One `BROWSER` entry on POSIX: a command line split into words as a
/// POSIX shell splits them (no expansion), each `%s` the URL, the URL
/// appended when no word holds `%s`, and a trailing `&` word dropped (the
/// program always starts in the background, in a session of its own).
fn from_posix_entry(entry: &str) -> Plan {
    let Ok(mut words) = shlex_split(entry) else {
        return refused(format!(
            "the BROWSER variable has an unclosed quote or a trailing backslash; {SET_BROWSER}"
        ));
    };
    if words.last().map(String::as_str) == Some("&") {
        words.pop();
    }
    if words.first().is_none_or(String::is_empty) {
        return refused(format!(
            "the BROWSER variable names no program; {SET_BROWSER}"
        ));
    }
    let program = words.remove(0);
    let name = basename(&program, Platform::Posix).to_lowercase();
    if TERMINAL_BROWSERS.contains(&name.as_str()) {
        return refused(format!(
            "BROWSER names the terminal browser {name}, which cannot open beside this command; {SET_BROWSER}"
        ));
    }
    if !words.iter().any(|word| word.contains("%s")) {
        words.push("%s".to_owned());
    }
    Plan::Background {
        program,
        args: words,
    }
}

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

/// An explicit executable avoids implicit current-directory lookup and
/// batch-file shell interpretation on Windows.
fn launchable(program: &str) -> bool {
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

/// One recovery for an explicit Windows launcher that cannot be used.
pub(super) const SET_WINDOWS_BROWSER: &str =
    "set BROWSER to a full .exe path (quote paths with spaces), or open the printed link yourself";

fn choose_windows(host: &dyn Host) -> Plan {
    let Some(value) = host.var("BROWSER") else {
        return Plan::StartFile;
    };
    let Ok(value) = value.into_string() else {
        return refused(format!(
            "the BROWSER variable is not valid Unicode; {SET_WINDOWS_BROWSER}"
        ));
    };
    let Some(entry) = value.split(';').find(|entry| !entry.is_empty()) else {
        return Plan::StartFile;
    };
    let Ok(mut words) = split_words(entry, true) else {
        return refused(format!(
            "the BROWSER variable has an unclosed quote; {SET_WINDOWS_BROWSER}"
        ));
    };
    if words.last().map(String::as_str) == Some("&") {
        words.pop();
    }
    if words.first().is_none_or(String::is_empty) {
        return refused(format!(
            "the BROWSER variable names no program; {SET_WINDOWS_BROWSER}"
        ));
    }
    let program = words.remove(0);
    if !launchable(&program) {
        return refused(format!(
            "BROWSER must name a drive-absolute .exe, not a bare name, relative path or script; {SET_WINDOWS_BROWSER}"
        ));
    }
    if !words.iter().any(|word| word.contains("%s")) {
        words.push("%s".to_owned());
    }
    Plan::Background {
        program,
        args: words,
    }
}

/// `shlex.split(text)` (POSIX mode, no comments): the words, or `Err` for
/// the `ValueError` it raises on an open quote or a trailing backslash.
pub(super) fn shlex_split(text: &str) -> Result<Vec<String>, ()> {
    split_words(text, false)
}

fn split_words(text: &str, windows: bool) -> Result<Vec<String>, ()> {
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
                } else if c == '\\' && !windows {
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
                } else if c == '\\' && quote == '"' && !windows {
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
