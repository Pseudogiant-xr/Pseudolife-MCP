//! Keeps a machine on the Python client while a tunnel is registered to
//! start by itself there (issue #1632).
//!
//! The Python client's `tunnel service install` registers a systemd unit, a
//! launchd job or a scheduled task whose command is the stable launcher
//! (`<layout.launcher> tunnel run --profile <name> --profile-dir <store>`).
//! Moving the machine to the native client replaces that launcher with the
//! native binary, which cannot run a tunnel yet: `tunnel run` exits 1, and
//! systemd and launchd then restart the service every few seconds for ever
//! (reproduced on a disposable systemd user unit). So an update that would
//! move the launcher refuses before it changes anything when such a service
//! is registered.
//!
//! The signal is exact: a definition this product wrote, carrying the
//! `PseudolifeTunnelOwner:<sha256>` marker that `render_service` puts in it.
//! On Linux and macOS the definition files are read directly (the Python
//! client's `tunnel service remove` deletes exactly these files), so no
//! service manager is asked anything. On Windows the registered task can be
//! read only through `schtasks /Query`, which changes nothing. A disabled
//! service is refused too: it is still registered, enabling it again would
//! start the same failing loop, and after the move no client could remove
//! it, since the native client has no `tunnel service`.
//!
//! Detection never starts, stops, enables, disables or edits a service.
//! When a definition cannot be read (an unreadable directory, `schtasks`
//! that does not answer), the move goes ahead with one line saying what was
//! not checked: blocking every update on a query that cannot run would
//! strand the machine, and a Windows task fails at most a few times per
//! logon rather than looping.

use super::clients::{Output, Runner};
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
    /// systemd: the user unit directory, and the system one when `root`.
    Linux { root: bool },
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
            Host::Linux { root }
        }
    }
}

#[derive(Debug, PartialEq)]
pub(crate) enum Decision {
    /// The update may go on; each line says what could not be checked.
    Proceed(Vec<String>),
    /// The one refusal line: nothing was changed.
    Refuse(String),
}

#[derive(Debug)]
struct Registered {
    profile: String,
    place: String,
    system: bool,
}

/// The decision for this machine as the native update would see it.
pub(crate) fn probe(repo: Option<&Path>) -> Decision {
    let Ok(layout) = runtime::default_layout() else {
        // The shim step fails on the same layout before it activates anything.
        return Decision::Proceed(vec![]);
    };
    decide(
        &layout,
        &Host::current(),
        &runtime::home(),
        &super::clients::NativeRunner,
        || managed_registrations(&layout, repo),
    )
}

/// `managed` says whether the shim step has a registration to move; without
/// one it leaves the launcher as it is (`runtime::update_shim`).
pub(crate) fn decide(
    layout: &Layout,
    host: &Host,
    home: &Path,
    runner: &dyn Runner,
    managed: impl FnOnce() -> bool,
) -> Decision {
    // Cheapest checks first: a machine whose launcher is already native, or
    // that has none, moves nothing, and no service manager is asked.
    if !moves_launcher(layout) || !managed() {
        return Decision::Proceed(vec![]);
    }
    let (found, notes) = registered(host, home, runner);
    if found.is_empty() {
        Decision::Proceed(notes)
    } else {
        Decision::Refuse(refusal(&found, matches!(host, Host::Windows { .. })))
    }
}

/// Whether activating the native runtime would replace the launcher a
/// Python-era service runs: it exists and is not already one of this
/// layout's verified native binaries (a Windows distlib launcher is a PE
/// file too, so file magic alone cannot tell).
fn moves_launcher(layout: &Layout) -> bool {
    if !runtime::plain_file(&layout.launcher) {
        return false;
    }
    let Ok(digest) = runtime::hash_file(&layout.launcher) else {
        // Activation reads the same file and fails before replacing it.
        return false;
    };
    !runtime::list_runtimes(layout)
        .iter()
        .any(|runtime| runtime.marker.binary_sha256 == digest)
}

fn managed_registrations(layout: &Layout, repo: Option<&Path>) -> bool {
    runtime::registrations::find_registrations().is_ok_and(|registrations| {
        registrations
            .iter()
            .any(|r| matches!(r.install_kind(layout, repo), "managed" | "legacy"))
    })
}

fn registered(host: &Host, home: &Path, runner: &dyn Runner) -> (Vec<Registered>, Vec<String>) {
    let mut found = Vec::new();
    let mut notes = Vec::new();
    match host {
        Host::Linux { root } => {
            let user = home.join(".config").join("systemd").join("user");
            definitions(
                &user,
                ".service",
                unit_owned,
                false,
                "systemd user unit",
                &mut found,
                &mut notes,
            );
            if *root {
                definitions(
                    Path::new("/etc/systemd/system"),
                    ".service",
                    unit_owned,
                    true,
                    "systemd system unit",
                    &mut found,
                    &mut notes,
                );
            }
        }
        Host::Mac => {
            let agents = home.join("Library").join("LaunchAgents");
            definitions(
                &agents,
                ".plist",
                plist_owned,
                false,
                "launchd job",
                &mut found,
                &mut notes,
            );
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
    owned: fn(&str) -> bool,
    system: bool,
    kind: &str,
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
        let name = entry.file_name();
        let Some(profile) = name
            .to_str()
            .and_then(|n| n.strip_prefix(PREFIX))
            .and_then(|n| n.strip_suffix(suffix))
        else {
            continue;
        };
        let path = entry.path();
        // The Python client writes a plain file; anything else is not its own.
        let plain =
            fs::symlink_metadata(&path).is_ok_and(|m| m.is_file() && m.len() <= DEFINITION_LIMIT);
        if !plain {
            continue;
        }
        match fs::read(&path) {
            Ok(bytes) => {
                if owned(&String::from_utf8_lossy(&bytes)) {
                    found.push(Registered {
                        profile: display_profile(profile),
                        place: format!("{kind} {}", path.display()),
                        system,
                    });
                }
            }
            Err(_) => notes.push(unchecked(&format!("{} could not be read", path.display()))),
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
        let profile = display_profile(&name[1 + PREFIX.len()..]);
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
        } else if task_owned(&decode(&definition)) {
            found.push(Registered {
                profile,
                place: format!("scheduled task {name}"),
                system: false,
            });
        }
    }
}

/// Task names (`\pseudolife-tunnel-<profile>`, root folder) from
/// `schtasks /Query /FO CSV /NH`, whose first field is the quoted full name.
fn task_names(listing: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for line in listing.lines() {
        let Some(name) = line
            .trim_start()
            .strip_prefix('"')
            .and_then(|rest| rest.split('"').next())
        else {
            continue;
        };
        let wanted = format!("\\{PREFIX}");
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

/// `schtasks` writes UTF-16 with a byte-order mark for /XML and the console
/// code page for CSV; the parts read here are ASCII either way.
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

/// A task `render_service` wrote: its registration source is the marker.
fn task_owned(text: &str) -> bool {
    let open = format!("<Source>{OWNER}");
    text.match_indices(&open)
        .any(|(at, _)| marked(&text[at + open.len()..], "</Source>"))
}

fn marked(rest: &str, close: &str) -> bool {
    rest.get(..64).is_some_and(runtime::valid_digest) && rest[64..].starts_with(close)
}

/// A profile name as the Python client validates it, or a placeholder for
/// anything else, so the printed command never carries stray characters.
fn display_profile(name: &str) -> String {
    let plain = !name.is_empty()
        && name.len() <= 64
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.');
    if plain {
        name.to_owned()
    } else {
        "<profile>".into()
    }
}

fn unchecked(what: &str) -> String {
    format!(
        "{what}, so this update could not check for a tunnel registered to start by itself; if one is, it fails at each start once this machine runs the native client: remove it with the Python client (pseudolife-mcp tunnel service remove --profile <name>) before moving, or move back"
    )
}

fn refusal(found: &[Registered], windows: bool) -> String {
    let places = found
        .iter()
        .map(|r| r.place.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    let removes = found
        .iter()
        .map(|r| {
            format!(
                "pseudolife-mcp tunnel service remove --profile {}{}",
                r.profile,
                if r.system { " --system-service" } else { "" }
            )
        })
        .collect::<Vec<_>>()
        .join("; ");
    let unset = if windows {
        format!("in PowerShell: Remove-Item Env:{ENGINE}")
    } else {
        format!("unset {ENGINE}")
    };
    format!(
        "this machine stays on the Python client: a tunnel is registered to start by itself here ({places}) and the native client cannot run a tunnel yet, so after the move that service would fail at every start. Nothing was changed, and the tunnel keeps starting with the Python client. To update this machine, rerun pseudolife-mcp update without {ENGINE}=native ({unset}, or set it to python); to move to the native client anyway, first remove the autostart with the Python client ({removes}, adding the --profile-dir it was set up with, if any), then rerun"
    )
}

#[cfg(test)]
#[path = "tunnel_guard_tests.rs"]
mod tests;
