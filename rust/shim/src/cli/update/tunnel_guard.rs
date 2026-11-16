//! Keeps a machine on the Python client while a tunnel is registered to
//! start by itself there (issue #1632).
//!
//! The Python client's `tunnel service install` registers a systemd unit, a
//! launchd job or a scheduled task whose command is the stable launcher
//! (`<layout.launcher> tunnel run --profile <name> --profile-dir <store>`).
//! Moving the machine to the native client replaces that launcher with the
//! native binary, which cannot run a tunnel yet: `tunnel run` exits 1, and
//! systemd and launchd then restart the service every few seconds for ever
//! (reproduced on a disposable systemd user unit). So a run that would move
//! the launcher refuses before it changes anything while such a service is
//! registered. On a machine already on the native client the run goes on
//! and says how to stop the failing service.
//!
//! The signal is exact: a definition this product wrote, carrying the
//! `PseudolifeTunnelOwner:<sha256>` marker that `render_service` puts in it,
//! whose command is this layout's launcher. A service that runs another
//! program (a pip install's script, an interpreter) does not move with the
//! launcher and is left alone. On Linux and macOS the definition files are
//! read directly (the Python client's `tunnel service remove` deletes
//! exactly these files), so no service manager is asked anything. On Windows
//! the registered task can be read only through `schtasks /Query`, which
//! changes nothing. A disabled service is refused too: it is still
//! registered, enabling it again would start the same failing loop, and
//! after the move no client could remove it, since the native client has no
//! `tunnel service`.
//!
//! Detection never starts, stops, enables, disables or edits a service.
//! When a definition cannot be read (an unreadable directory, `schtasks`
//! that does not answer), the move goes ahead with one line saying what was
//! not checked: blocking every update on a query that cannot run would
//! strand the machine, and a Windows task fails at most a few times per
//! logon rather than looping.

use super::clients::{NativeRunner, Output, Runner};
use crate::runtime::{self, Layout};
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

const OWNER: &str = "PseudolifeTunnelOwner:";
const PREFIX: &str = "pseudolife-tunnel-";
const ENGINE: &str = "PSEUDOLIFE_SHIM_ENGINE";
const DEFINITION_LIMIT: u64 = 1024 * 1024;
const QUERY_TIMEOUT: Duration = Duration::from_secs(30);

/// Where a tunnel service could be registered for this user.
pub(crate) enum Host {
    /// systemd: the user unit directory, and `system` (the system unit
    /// directory) when running as root, where `--system-service` installs.
    Linux { system: Option<PathBuf> },
    /// launchd: the user's LaunchAgents.
    Mac,
    /// Task Scheduler, read through this `schtasks.exe`.
    Windows { schtasks: PathBuf },
}

impl Host {
    pub(crate) fn current() -> Self {
        if cfg!(windows) {
            let root = std::env::var_os("SystemRoot")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
            Host::Windows {
                schtasks: root.join("System32").join("schtasks.exe"),
            }
        } else if cfg!(target_os = "macos") {
            Host::Mac
        } else {
            #[cfg(unix)]
            let root = rustix::process::geteuid().is_root();
            #[cfg(not(unix))]
            let root = false;
            Host::Linux {
                system: root.then(|| PathBuf::from("/etc/systemd/system")),
            }
        }
    }
}

/// Who is moving the launcher, for the way out the refusal names.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Mover {
    /// `pseudolife-mcp update` (the bridge from a Python client included):
    /// moves only when a client registration names the launcher
    /// (`runtime::update_shim`).
    Update,
    /// A checkout update with `--all` (`ops/update.sh`, `ops/update.ps1`).
    Checkout,
    /// `update --unattended`, the daily client job.
    Unattended,
    /// `runtime install`, which the native installers run: always activates.
    Installer,
}

#[derive(Debug, PartialEq)]
pub(crate) enum Decision {
    /// The run may go on; each line is a warning to show.
    Proceed(Vec<String>),
    /// The one refusal line: nothing was changed.
    Refuse(String),
}

#[derive(Debug, Clone, Copy)]
enum Kind {
    UserUnit,
    SystemUnit,
    LaunchAgent,
    Task,
}

#[derive(Debug)]
struct Registered {
    kind: Kind,
    /// The unit, plist label or task name.
    name: String,
    /// Where it is, as shown to the user.
    place: String,
    /// The definition file (systemd and launchd).
    file: Option<PathBuf>,
    /// The service's program and arguments, when they could be read.
    command: Option<Vec<String>>,
}

impl Registered {
    fn argument(&self, flag: &str) -> Option<&str> {
        let command = self.command.as_ref()?;
        let at = command.iter().position(|a| a == flag)?;
        command.get(at + 1).map(String::as_str)
    }
    fn profile(&self) -> String {
        let named = self
            .argument("--profile")
            .map(str::to_owned)
            .unwrap_or_else(|| {
                let stem = self.name.trim_start_matches('\\');
                stem.strip_prefix(PREFIX)
                    .and_then(|n| n.strip_suffix(".service").or(Some(n)))
                    .unwrap_or("")
                    .to_owned()
            });
        if plain_name(&named) {
            named
        } else {
            "<profile>".into()
        }
    }
    /// The Python client's removal, with the options the service names.
    fn removal(&self) -> String {
        let mut line = format!(
            "pseudolife-mcp tunnel service remove --profile {}",
            self.profile()
        );
        if let Some(dir) = self.argument("--profile-dir") {
            line.push_str(" --profile-dir ");
            line.push_str(&quote(dir, matches!(self.kind, Kind::Task)));
        }
        if matches!(self.kind, Kind::SystemUnit) {
            line.push_str(" --system-service");
        }
        line
    }
    /// The service manager's own commands that stop it and remove its
    /// definition, so this warning does not come back.
    fn remove_native(&self) -> String {
        let file = self
            .file
            .as_ref()
            .map(|f| quote(&f.to_string_lossy(), false))
            .unwrap_or_default();
        match self.kind {
            Kind::UserUnit => format!(
                "systemctl --user disable --now {0}; rm {file}; systemctl --user daemon-reload",
                self.name
            ),
            Kind::SystemUnit => format!(
                "(as root) systemctl disable --now {0}; rm {file}; systemctl daemon-reload",
                self.name
            ),
            Kind::LaunchAgent => format!("launchctl bootout gui/$(id -u)/{}; rm {file}", self.name),
            Kind::Task => format!("schtasks /Delete /TN \"{}\" /F", self.name),
        }
    }
}

/// The decision for this machine as `mover` would see it.
pub(crate) fn probe(mover: Mover, repo: Option<&Path>) -> Decision {
    let Ok(layout) = runtime::default_layout() else {
        // The move fails on the same layout before it activates anything.
        return Decision::Proceed(vec![]);
    };
    decide(
        &layout,
        &Host::current(),
        &runtime::home(),
        &NativeRunner,
        mover,
        || match mover {
            Mover::Installer => true,
            _ => managed_registrations(&layout, repo),
        },
    )
}

/// `moves` says whether the run would activate the native runtime at all
/// (for an update: whether a client registration names the launcher).
pub(crate) fn decide(
    layout: &Layout,
    host: &Host,
    home: &Path,
    runner: &dyn Runner,
    mover: Mover,
    moves: impl FnOnce() -> bool,
) -> Decision {
    // Cheapest checks first: no launcher, or nothing to move, asks no
    // service manager anything.
    let native = match launcher(layout) {
        Launcher::Missing => return Decision::Proceed(vec![]),
        Launcher::Native => true,
        Launcher::Other => false,
    };
    if !native && !moves() {
        return Decision::Proceed(vec![]);
    }
    if native && !home.join(".pseudolife-mcp").join("tunnel").exists() {
        // Only warns, so no Task Scheduler query on a machine that never
        // saved a tunnel profile in the default store.
        return Decision::Proceed(vec![]);
    }
    let (found, notes) = registered(host, home, runner);
    let affected: Vec<_> = found
        .into_iter()
        .filter(|r| runs_launcher(r, layout))
        .collect();
    if native {
        // Already moved: refusing changes nothing, so say how to stop it.
        return Decision::Proceed(affected.iter().map(failing).collect());
    }
    if affected.is_empty() {
        Decision::Proceed(notes)
    } else {
        Decision::Refuse(refusal(
            &affected,
            matches!(host, Host::Windows { .. }),
            mover,
        ))
    }
}

enum Launcher {
    Missing,
    /// One of this layout's verified native binaries (a Windows distlib
    /// launcher is a PE file too, so file magic alone cannot tell).
    Native,
    /// The Python client's launcher, or anything else the move replaces.
    Other,
}

fn launcher(layout: &Layout) -> Launcher {
    if !runtime::plain_file(&layout.launcher) {
        return Launcher::Missing;
    }
    let Ok(digest) = runtime::hash_file(&layout.launcher) else {
        // Activation reads the same file and fails before replacing it.
        return Launcher::Missing;
    };
    if runtime::list_runtimes(layout)
        .iter()
        .any(|runtime| runtime.marker.binary_sha256 == digest)
        || native_magic(&layout.launcher)
    {
        Launcher::Native
    } else {
        Launcher::Other
    }
}

/// A native binary even when its runtime is gone. Off Windows the Python
/// client's launcher is a `/bin/sh` script, so ELF or Mach-O magic says so;
/// on Windows it is a distlib launcher, a PE file with a zip carrying
/// `__main__.py` appended, so a PE file without one is native.
fn native_magic(path: &Path) -> bool {
    use std::io::Read;
    let Ok(mut file) = fs::File::open(path) else {
        return false;
    };
    let mut magic = [0; 4];
    if file.read_exact(&mut magic).is_err() {
        return false;
    }
    if cfg!(windows) {
        return magic.starts_with(b"MZ")
            && zip::ZipArchive::new(file)
                .map_or(true, |mut archive| archive.by_name("__main__.py").is_err());
    }
    magic == [0x7f, b'E', b'L', b'F']
        || [
            [0xfe, 0xed, 0xfa, 0xce],
            [0xce, 0xfa, 0xed, 0xfe],
            [0xfe, 0xed, 0xfa, 0xcf],
            [0xcf, 0xfa, 0xed, 0xfe],
            [0xca, 0xfe, 0xba, 0xbe],
        ]
        .contains(&magic)
}

fn managed_registrations(layout: &Layout, repo: Option<&Path>) -> bool {
    runtime::registrations::find_registrations().is_ok_and(|registrations| {
        registrations
            .iter()
            .any(|r| matches!(r.install_kind(layout, repo), "managed" | "legacy"))
    })
}

/// Whether the service starts this layout's launcher. A definition whose
/// command cannot be read counts: it carries this product's marker, and the
/// launcher is the only command `render_service` writes for it.
fn runs_launcher(service: &Registered, layout: &Layout) -> bool {
    service
        .command
        .as_ref()
        .and_then(|c| c.first())
        // Single-byte schtasks output in a non-UTF-8 code page, or a numeric
        // character reference, is not the path it names: count it.
        .filter(|program| !program.contains('\u{fffd}') && !program.contains("&#"))
        .is_none_or(|program| runtime::same_path(Path::new(program), &layout.launcher))
}

fn registered(host: &Host, home: &Path, runner: &dyn Runner) -> (Vec<Registered>, Vec<String>) {
    let mut found = Vec::new();
    let mut notes = Vec::new();
    match host {
        Host::Linux { system } => {
            let user = home.join(".config").join("systemd").join("user");
            definitions(&user, ".service", Kind::UserUnit, &mut found, &mut notes);
            if let Some(system) = system {
                definitions(system, ".service", Kind::SystemUnit, &mut found, &mut notes);
            }
        }
        Host::Mac => {
            let agents = home.join("Library").join("LaunchAgents");
            definitions(&agents, ".plist", Kind::LaunchAgent, &mut found, &mut notes);
        }
        Host::Windows { schtasks } => tasks(schtasks, runner, &mut found, &mut notes),
    }
    (found, notes)
}

/// Reads `<dir>/pseudolife-tunnel-*<suffix>` definitions; a missing
/// directory holds none.
fn definitions(
    dir: &Path,
    suffix: &str,
    kind: Kind,
    found: &mut Vec<Registered>,
    notes: &mut Vec<String>,
) {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
        Err(_) => {
            notes.push(unchecked(&format!("{} could not be read", dir.display())));
            return;
        }
    };
    for entry in entries.flatten() {
        let file = entry.file_name();
        let Some(file) = file.to_str().filter(|n| {
            n.starts_with(PREFIX) && n.len() > PREFIX.len() + suffix.len() && n.ends_with(suffix)
        }) else {
            continue;
        };
        let path = entry.path();
        // The Python client writes a plain file; anything else is not its own.
        let plain =
            fs::symlink_metadata(&path).is_ok_and(|m| m.is_file() && m.len() <= DEFINITION_LIMIT);
        if !plain {
            continue;
        }
        let text = match fs::read(&path) {
            Ok(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
            Err(_) => {
                notes.push(unchecked(&format!("{} could not be read", path.display())));
                continue;
            }
        };
        let (owned, command, name, label) = match kind {
            Kind::LaunchAgent => (
                plist_owned(&text),
                plist_command(&text),
                file.trim_end_matches(suffix).to_owned(),
                "launchd job",
            ),
            Kind::SystemUnit => (
                unit_owned(&text),
                unit_command(&text),
                file.to_owned(),
                "systemd system unit",
            ),
            _ => (
                unit_owned(&text),
                unit_command(&text),
                file.to_owned(),
                "systemd user unit",
            ),
        };
        if owned {
            found.push(Registered {
                kind,
                name,
                place: format!("{label} {}", path.display()),
                file: Some(path.clone()),
                command,
            });
        }
    }
}

fn tasks(
    schtasks: &Path,
    runner: &dyn Runner,
    found: &mut Vec<Registered>,
    notes: &mut Vec<String>,
) {
    let listing = runner.run(
        schtasks,
        &["/Query", "/FO", "CSV", "/NH"],
        None,
        QUERY_TIMEOUT,
        &[],
    );
    if listing.code != 0 {
        notes.push(unchecked(&format!(
            "the scheduled tasks could not be listed (schtasks /Query exited {})",
            listing.code
        )));
        return;
    }
    for name in task_names(&decode(&listing)) {
        let definition = runner.run(
            schtasks,
            &["/Query", "/TN", &name, "/XML"],
            None,
            QUERY_TIMEOUT,
            &[],
        );
        if definition.code != 0 {
            notes.push(unchecked(&format!(
                "the scheduled task {name} could not be read (schtasks /Query exited {})",
                definition.code
            )));
            continue;
        }
        let text = decode(&definition);
        if task_owned(&text) {
            found.push(Registered {
                kind: Kind::Task,
                place: format!("scheduled task {name}"),
                file: None,
                command: task_command(&text),
                name,
            });
        }
    }
}

/// Task names (`\pseudolife-tunnel-<profile>`, root folder) from
/// `schtasks /Query /FO CSV /NH`, whose first field is the quoted full name.
fn task_names(listing: &str) -> Vec<String> {
    let wanted = format!("\\{PREFIX}");
    let mut names: Vec<String> = Vec::new();
    for line in listing.lines() {
        let Some(name) = line
            .trim_start()
            .strip_prefix('"')
            .and_then(|rest| rest.split('"').next())
        else {
            continue;
        };
        let matches = name.len() > wanted.len()
            && name.is_char_boundary(wanted.len())
            && name[..wanted.len()].eq_ignore_ascii_case(&wanted)
            && !name[1..].contains('\\');
        if matches && !names.iter().any(|n| n.eq_ignore_ascii_case(name)) {
            names.push(name.to_owned());
        }
    }
    names
}

/// `schtasks` writes UTF-16 with a byte-order mark to a console and
/// single-byte text to a pipe; the parts read here are ASCII either way.
fn decode(output: &Output) -> String {
    let bytes = &output.bytes;
    let utf16 = |big: bool| {
        let units: Vec<u16> = bytes[2..]
            .chunks_exact(2)
            .map(|c| {
                if big {
                    u16::from_be_bytes([c[0], c[1]])
                } else {
                    u16::from_le_bytes([c[0], c[1]])
                }
            })
            .collect();
        String::from_utf16_lossy(&units)
    };
    if bytes.starts_with(&[0xff, 0xfe]) {
        utf16(false)
    } else if bytes.starts_with(&[0xfe, 0xff]) {
        utf16(true)
    } else {
        let text = String::from_utf8_lossy(bytes);
        text.strip_prefix('\u{feff}').unwrap_or(&text).to_owned()
    }
}

/// A systemd unit `render_service` wrote: its first line is the marker.
fn unit_owned(text: &str) -> bool {
    text.lines()
        .next()
        .and_then(|line| line.trim_end().strip_prefix("# "))
        .and_then(|line| line.strip_prefix(OWNER))
        .is_some_and(runtime::valid_digest)
}

/// `ExecStart=` as `render_service` quotes it: each word in double quotes,
/// `\` and `"` backslash-escaped, `%` and `$` doubled.
fn unit_command(text: &str) -> Option<Vec<String>> {
    let line = text
        .lines()
        .find_map(|line| line.trim_end().strip_prefix("ExecStart="))?;
    let mut words = Vec::new();
    let mut chars = line.chars().peekable();
    loop {
        while chars.next_if(|c| c.is_whitespace()).is_some() {}
        let Some(first) = chars.next() else { break };
        let mut word = String::new();
        if first == '"' {
            loop {
                match chars.next()? {
                    '"' => break,
                    '\\' => word.push(chars.next()?),
                    c => word.push(c),
                }
            }
        } else {
            word.push(first);
            while let Some(c) = chars.next_if(|c| !c.is_whitespace()) {
                word.push(c);
            }
        }
        words.push(word.replace("%%", "%").replace("$$", "$"));
    }
    (!words.is_empty()).then_some(words)
}

/// A launchd plist `render_service` wrote: the `PseudolifeTunnelOwner` key
/// holds the marker string.
fn plist_owned(text: &str) -> bool {
    text.match_indices("<key>PseudolifeTunnelOwner</key>")
        .any(|(at, key)| {
            text[at + key.len()..]
                .trim_start()
                .strip_prefix("<string>")
                .and_then(|rest| rest.strip_prefix(OWNER))
                .is_some_and(|rest| marked(rest, "</string>"))
        })
}

/// `ProgramArguments`: the strings of the array after that key.
fn plist_command(text: &str) -> Option<Vec<String>> {
    let (_, rest) = text.split_once("<key>ProgramArguments</key>")?;
    let rest = rest.trim_start().strip_prefix("<array>")?;
    let (array, _) = rest.split_once("</array>")?;
    let mut words = Vec::new();
    let mut rest = array;
    while let Some((_, after)) = rest.split_once("<string>") {
        let (word, tail) = after.split_once("</string>")?;
        words.push(unescape_xml(word));
        rest = tail;
    }
    (!words.is_empty()).then_some(words)
}

/// A task `render_service` wrote: its registration source is the marker.
fn task_owned(text: &str) -> bool {
    let open = format!("<Source>{OWNER}");
    text.match_indices(&open)
        .any(|(at, _)| marked(&text[at + open.len()..], "</Source>"))
}

/// The task's `<Command>` and its `<Arguments>`, split as Windows does.
fn task_command(text: &str) -> Option<Vec<String>> {
    let element = |name: &str| {
        let open = format!("<{name}>");
        let close = format!("</{name}>");
        let (_, rest) = text.split_once(&open)?;
        let (value, _) = rest.split_once(&close)?;
        Some(unescape_xml(value.trim()))
    };
    let program = element("Command")?;
    let program = program
        .strip_prefix('"')
        .and_then(|p| p.strip_suffix('"'))
        .unwrap_or(&program)
        .to_owned();
    let mut words = vec![program];
    words.extend(split_windows(&element("Arguments").unwrap_or_default()));
    Some(words)
}

/// Splits a command line as `subprocess.list2cmdline` writes it.
fn split_windows(line: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quoted = false;
    let mut started = false;
    let mut chars = line.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\\' => {
                let mut slashes = 1;
                while chars.next_if_eq(&'\\').is_some() {
                    slashes += 1;
                }
                if chars.peek() == Some(&'"') {
                    word.extend(std::iter::repeat_n('\\', slashes / 2));
                    if slashes % 2 == 1 {
                        word.push(chars.next().unwrap());
                    }
                } else {
                    word.extend(std::iter::repeat_n('\\', slashes));
                }
                started = true;
            }
            '"' => {
                quoted = !quoted;
                started = true;
            }
            c if c.is_whitespace() && !quoted => {
                if started {
                    words.push(std::mem::take(&mut word));
                    started = false;
                }
            }
            c => {
                word.push(c);
                started = true;
            }
        }
    }
    if started {
        words.push(word);
    }
    words
}

fn unescape_xml(text: &str) -> String {
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

fn marked(rest: &str, close: &str) -> bool {
    rest.get(..64).is_some_and(runtime::valid_digest) && rest[64..].starts_with(close)
}

/// A profile name as the Python client validates it; anything else is shown
/// as a placeholder, so the printed command never carries stray characters.
fn plain_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.')
}

/// A path as a shell reads it back: bare when it holds only safe
/// characters, else in single quotes (PowerShell doubles a quote inside;
/// POSIX shells close, escape and reopen).
fn quote(path: &str, powershell: bool) -> String {
    let safe = !path.is_empty()
        && path
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "/\\._-:+@,".contains(c));
    if safe {
        path.to_owned()
    } else if powershell {
        format!("'{}'", path.replace('\'', "''"))
    } else {
        format!("'{}'", path.replace('\'', "'\\''"))
    }
}

fn unchecked(what: &str) -> String {
    format!(
        "{what}, so this run could not check for a tunnel registered to start by itself; if one is, it fails at each start once this machine runs the native client: stop it with its service manager, or go back to the Python client as \"The native client\" in docs/guide/configuration.md describes and remove it there (pseudolife-mcp tunnel service remove --profile <name>)"
    )
}

fn failing(service: &Registered) -> String {
    let how = match service.kind {
        Kind::Task => "it fails at each logon",
        _ => {
            "it fails at each start, and while it is enabled its service manager starts it again every few seconds"
        }
    };
    format!(
        "the tunnel service {} runs this machine's launcher, now the native client, which cannot run a tunnel yet, so {how}; this client has no tunnel service command, so remove it with: {}",
        service.place,
        service.remove_native()
    )
}

fn refusal(found: &[Registered], windows: bool, mover: Mover) -> String {
    let places = found
        .iter()
        .map(|r| r.place.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    let removes = found
        .iter()
        .map(Registered::removal)
        .collect::<Vec<_>>()
        .join("; ");
    let unset = if windows {
        format!("in PowerShell: Remove-Item Env:{ENGINE}")
    } else {
        format!("unset {ENGINE}")
    };
    // Cause and fix first: a 0.23.x hand-off parent keeps 400 characters.
    let (stay, again) = match mover {
        Mover::Update => (
            format!("to stay on it, rerun the same command without {ENGINE}=native ({unset}, or set it to python; on the daemon's host --daemon-only updates the daemon alone)"),
            "rerun it",
        ),
        Mover::Checkout => (
            "to stay on it, rerun ops/update.sh or ops/update.ps1 without --native, PSEUDOLIFE_NATIVE_UPDATE and PSEUDOLIFE_NATIVE_CLI (or pseudolife-mcp update --checkout without --all, which leaves the clients alone)".to_owned(),
            "rerun it",
        ),
        Mover::Unattended => (
            "this unattended run leaves the machine as it is, its daemon included, and tries again at its next run (an attended pseudolife-mcp update --daemon-only updates the daemon alone)".to_owned(),
            "the next run moves it",
        ),
        Mover::Installer => (
            format!("to stay on it, rerun the installer without {ENGINE}=native ({unset}, or set it to python)"),
            "rerun the installer",
        ),
    };
    format!(
        "this machine stays on the Python client: a tunnel is registered to start by itself and the native client cannot run a tunnel yet. Nothing was changed; {stay}. To move to the native client, first remove the autostart with the Python client ({removes}), then {again}. Registered: {places}"
    )
}

#[cfg(test)]
#[path = "tunnel_guard_tests.rs"]
mod tests;
