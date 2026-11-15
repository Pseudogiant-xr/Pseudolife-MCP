//! What `connect` needs from the unattended-update schedule: the schedules
//! installed here, read back beside the code that writes them, and their
//! re-creation for a new daemon URL through `--schedule` itself.
use super::*;
use crate::cli::update::Options;

/// One installed schedule, as `connect` reports and re-creates it.
pub(crate) struct Installed {
    /// The client schedule (what `--schedule` writes on a shim runtime with
    /// no daemon container) rather than the daemon's.
    pub client: bool,
    /// Where it lives, as a report names it.
    pub place: String,
    /// Its daily time, `HH:MM`, when it can be read.
    pub time: Option<String>,
    /// The daemon URL its command names.
    pub url: Option<String>,
    /// It is exactly what `--schedule` writes here for `url` at `time`:
    /// the command, and (systemd) the unit, its environment and its timer,
    /// enabled; (Task Scheduler) one daily trigger, enabled, interactive.
    pub shaped: bool,
    /// Why that cannot be told (no launcher to compare with, a description
    /// that cannot be read), when it cannot.
    pub why: Option<String>,
    /// Why `connect` leaves it as it is because of what systemd reads beyond
    /// the base files (a drop-in that sets what the run depends on, one it
    /// cannot read or parse, a unit file loaded from elsewhere) or because
    /// the unit itself cannot be read, as the whole sentence a row prints:
    /// the cause, then the next step that resolves it. The schedule is then
    /// never current and never re-created.
    pub overridden: Option<String>,
    /// A task setting `--schedule` never writes, named ("an end date"):
    /// re-registering the task would drop it.
    pub differs: Option<String>,
    /// The task's command is `--schedule`'s `-EncodedCommand` layout
    /// wrapper, so Task Scheduler shows its daemon URL only as base64.
    pub encoded: bool,
    /// The one command that removes exactly this schedule.
    pub remove: String,
}

/// Where a row's repair command names the daemon URL `connect` moves to,
/// which only `connect` knows: it replaces this with that URL.
pub(crate) const TARGET: &str = "{the new daemon URL}";

/// What discovery reads the schedules through: the platform, the unit
/// directory `--schedule` writes to, the environment and home the drop-in
/// search path is built from, and the commands it runs (`schtasks`,
/// `systemctl`). `Host::here` is this machine; tests pass a fake one.
struct Host<'a> {
    windows: bool,
    linux: bool,
    dir: PathBuf,
    env: &'a dyn Fn(&str) -> Option<String>,
    home: PathBuf,
    run: &'a dyn Fn(&[String]) -> (i32, String),
}

fn run_here(argv: &[String]) -> (i32, String) {
    seams::run_cli(argv, None, 30, false)
}

fn env_here(key: &str) -> Option<String> {
    std::env::var(key).ok()
}

impl Host<'static> {
    fn here() -> Self {
        Host {
            windows: cfg!(windows),
            linux: cfg!(target_os = "linux"),
            dir: systemd_dir(),
            env: &env_here,
            home: seams::home(),
            run: &run_here,
        }
    }
}

/// The daemon and client schedules installed on this machine. On Linux one
/// that exists but cannot be read is shown with that cause; on Windows one
/// whose query failed is left out, as it always was.
pub(crate) fn installed(names: &Names) -> Vec<Installed> {
    installed_with(names, &|url: &str| command(url))
}

fn installed_with(names: &Names, expected: &dyn Fn(&str) -> Result<Vec<String>>) -> Vec<Installed> {
    installed_on(&Host::here(), names, expected)
}

fn installed_on(
    host: &Host,
    names: &Names,
    expected: &dyn Fn(&str) -> Result<Vec<String>>,
) -> Vec<Installed> {
    [false, true]
        .into_iter()
        .filter_map(|client| {
            let name = if client {
                names.client_unit
            } else {
                names.unit
            };
            discovered(
                installed_kind_on(host, names, client, expected),
                host.linux,
                &host.dir,
                name,
                client,
            )
        })
        .collect()
}

/// What discovery reports for one kind's read. On Linux a unit or timer
/// that exists but cannot be read stays visible, as a schedule connect
/// cannot vouch for, with the read error and the real fix (repair or remove
/// the file: `--schedule` would fail on the same read), so a move never
/// leaves it behind unreported. On Windows a failed query cannot be told
/// from a task that does not exist (schtasks exits 1 for both), so it is
/// left out, as it always was.
fn discovered(
    read: std::result::Result<Option<Installed>, String>,
    linux: bool,
    dir: &Path,
    name: &str,
    client: bool,
) -> Option<Installed> {
    match read {
        Ok(found) => found,
        Err(cause) if linux => Some(unvouched(dir, name, client, &|remove| {
            format!(
                "{cause}, so connect cannot read the schedule and leaves it as it is (`update --schedule` would fail on the same read): repair that file's permissions, or remove the schedule with `{remove}`, then re-run connect"
            )
        })),
        Err(_) => None,
    }
}

/// A systemd schedule connect cannot vouch for, shown with `said`, which is
/// given the command that removes it.
fn unvouched(dir: &Path, name: &str, client: bool, said: &dyn Fn(&str) -> String) -> Installed {
    let file = |suffix: &str| format!("'{}'", dir.join(format!("{name}.{suffix}")).display());
    let remove = format!(
        "systemctl --user disable --now {name}.timer && rm {} {} && systemctl --user daemon-reload",
        file("service"),
        file("timer")
    );
    Installed {
        client,
        place: dir.join(format!("{name}.service")).display().to_string(),
        time: None,
        url: None,
        shaped: false,
        why: None,
        overridden: Some(said(&remove)),
        differs: None,
        encoded: false,
        remove,
    }
}

/// The schedule of one kind, read again: `None` when it is not installed,
/// and why when it could not be read (a failed, timed-out or interrupted
/// query, an unreadable unit).
pub(crate) fn installed_kind(
    names: &Names,
    client: bool,
) -> std::result::Result<Option<Installed>, String> {
    installed_kind_with(names, client, &|url: &str| command(url))
}

fn installed_kind_with(
    names: &Names,
    client: bool,
    expected: &dyn Fn(&str) -> Result<Vec<String>>,
) -> std::result::Result<Option<Installed>, String> {
    installed_kind_on(&Host::here(), names, client, expected)
}

fn installed_kind_on(
    host: &Host,
    names: &Names,
    client: bool,
    expected: &dyn Fn(&str) -> Result<Vec<String>>,
) -> std::result::Result<Option<Installed>, String> {
    if host.windows {
        let task = if client {
            names.client_task
        } else {
            names.task
        };
        let (code, xml) = (host.run)(&[
            "schtasks".into(),
            "/Query".into(),
            "/TN".into(),
            task.into(),
            "/XML".into(),
        ]);
        if code != 0 {
            // schtasks exits 1 both for a task that is gone and for a query
            // that failed or timed out, so the cause names all of them.
            return Err(format!(
                "Task Scheduler did not return it (`schtasks /Query` exited {code}: it is gone, or the query failed or timed out)"
            ));
        }
        Ok(Some(windows_parse(task, client, &xml, &|url| {
            schedule_windows(&expected(url)?)
        })))
    } else if host.linux {
        let name = if client {
            names.client_unit
        } else {
            names.unit
        };
        let roots = systemd_roots(&host.dir, host.env, &host.home);
        let systemctl = |args: &[&str]| {
            let mut argv: Vec<String> = vec!["systemctl".into(), "--user".into()];
            argv.extend(args.iter().map(|arg| (*arg).to_owned()));
            let result = (host.run)(&argv);
            if crate::cli::update::schedules::cancelled() {
                return Err("interrupted".to_owned());
            }
            Ok(result)
        };
        systemd_installed(
            &host.dir,
            &roots,
            name,
            client,
            &|| Ok(systemctl(&["is-enabled", "--quiet", &format!("{name}.timer")])?.0 == 0),
            &|unit| {
                let (code, out) = systemctl(&[
                    "show",
                    "--property=FragmentPath,DropInPaths,NeedDaemonReload",
                    unit,
                ])?;
                if code != 0 {
                    return Err(format!("`systemctl --user show {unit}` exited {code}"));
                }
                Ok(out)
            },
            &|url| service_body(name, &expected(url)?, &environment_lines(client, false)?),
        )
    } else {
        Ok(None)
    }
}

/// A systemd unit `name` in `dir`, its timer, whether the timer is enabled
/// and what systemd reads beyond the base files for either: the drop-ins
/// under `roots` (`systemd_dropins`), checked against what systemd's own
/// user manager reports for each unit (`show`: its `FragmentPath` and
/// `DropInPaths`), so a file systemd applies that this search did not read
/// leaves the schedule alone rather than passing.
fn systemd_installed(
    dir: &Path,
    roots: &[PathBuf],
    name: &str,
    client: bool,
    enabled: &dyn Fn() -> std::result::Result<bool, String>,
    show: &dyn Fn(&str) -> std::result::Result<String, String>,
    expected: &dyn Fn(&str) -> Result<String>,
) -> std::result::Result<Option<Installed>, String> {
    let unit = dir.join(format!("{name}.service"));
    // `--schedule` saves and replaces these files as bytes, so one that is
    // not UTF-8 text is something it repairs; one that cannot be read at all
    // is not.
    let not_text = |path: &Path| {
        Ok(Some(unvouched(dir, name, client, &|remove| {
            format!(
                "{} is not UTF-8 text, so connect cannot compare it with what `update --schedule` writes and leaves the schedule as it is: re-create it with `pseudolife-mcp update --schedule HH:MM --daemon-url {TARGET}` at your time (which replaces the file), or remove it with `{remove}`, then re-run connect",
                path.display()
            )
        })))
    };
    // A unit path that is a directory cannot be read, repaired by
    // permissions or removed by `rm`, so the row says to replace it.
    let timer_file = unit.with_extension("timer");
    for path in [&unit, &timer_file] {
        if path.is_dir() {
            return Ok(Some(unvouched(dir, name, client, &|_| {
                format!(
                    "{} is a directory, not a unit file, so connect cannot read the schedule and leaves it as it is: remove that directory (`rm -r '{}'`) and run `systemctl --user daemon-reload`, then re-create the schedule with `pseudolife-mcp update --schedule HH:MM --daemon-url {TARGET}` at your time, then re-run connect",
                    path.display(),
                    path.display()
                )
            })));
        }
    }
    let service = match fs::read_to_string(&unit) {
        Ok(service) => service,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) if error.kind() == std::io::ErrorKind::InvalidData => return not_text(&unit),
        Err(error) => return Err(format!("{} cannot be read ({error})", unit.display())),
    };
    let timer = match fs::read_to_string(&timer_file) {
        Ok(timer) => timer,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(error) if error.kind() == std::io::ErrorKind::InvalidData => {
            return not_text(&timer_file);
        }
        Err(error) => {
            return Err(format!("{} cannot be read ({error})", timer_file.display()));
        }
    };
    let enabled = enabled()?;
    let overridden = match systemd_dropins(roots, name) {
        Err(blocked) => Some(blocked),
        Ok([service_read, timer_read]) => [("service", service_read), ("timer", timer_read)]
            .into_iter()
            .find_map(|(kind, read)| {
                let unit = format!("{name}.{kind}");
                match show(&unit) {
                    Err(cause) => Some(unasked(&cause)),
                    Ok(out) => systemd_disagrees(dir, &unit, &out, &read),
                }
            }),
    };
    Ok(Some(systemd_parse(
        dir, name, client, &service, &timer, enabled, overridden, expected,
    )))
}

fn unasked(cause: &str) -> String {
    format!(
        "connect could not ask systemd's user manager which files make up the unit ({cause}), so it cannot tell whether a drop-in changes the run and leaves the schedule as it is: re-run connect where the user manager runs (a login session), or manage this schedule by hand"
    )
}

/// The path as systemd would name it (`chase`, which resolves symlinks),
/// or as given when it cannot be resolved.
fn canonical(path: &Path) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// Where systemd's report for `unit` (`show --property=FragmentPath,
/// DropInPaths`) disagrees with this search: a unit file loaded from
/// anywhere but `dir`, or a drop-in it applies that `read` (the files this
/// search read as applying) does not hold. Either leaves the schedule alone.
fn systemd_disagrees(
    dir: &Path,
    unit: &str,
    out: &str,
    read: &std::collections::BTreeSet<PathBuf>,
) -> Option<String> {
    let mut fragment = None;
    let mut dropins = Vec::new();
    let mut stale = true;
    for line in out.lines() {
        if let Some(value) = line.strip_prefix("FragmentPath=") {
            fragment = Some(value.trim().to_owned());
        } else if let Some(value) = line.strip_prefix("DropInPaths=") {
            match show_paths(value) {
                Some(paths) => dropins.extend(paths),
                None => {
                    return Some(format!(
                        "connect cannot decode the drop-in paths systemd's user manager reports for {unit} (`{value}`), so it cannot tell whether one changes the run and leaves the schedule as it is: manage this schedule by hand, or move its drop-ins to a path without unusual characters and run `systemctl --user daemon-reload`"
                    ));
                }
            }
        } else if let Some(value) = line.strip_prefix("NeedDaemonReload=") {
            stale = value.trim() != "no";
        }
    }
    // What runs is what the user manager loaded: a file changed on disk
    // since then (or a report that does not say) is not yet in force, so
    // what connect read is not what runs.
    if stale {
        return Some(format!(
            "systemd's user manager has not loaded {unit}'s files as they are on disk (it reports NeedDaemonReload), so what runs may differ from what connect reads and it leaves the schedule as it is: run `systemctl --user daemon-reload`, then re-run connect"
        ));
    }
    let written = canonical(&dir.join(unit));
    match fragment.as_deref() {
        Some(path) if !path.is_empty() && canonical(Path::new(path)) == written => {}
        Some(path) if !path.is_empty() => {
            return Some(format!(
                "systemd's user manager loads {unit} from {path}, not from {} that `update --schedule` writes, so connect leaves the schedule as it is: remove the other unit file and run `systemctl --user daemon-reload`, or manage this schedule by hand",
                written.display()
            ));
        }
        _ if dir.join(unit).exists() => {
            return Some(format!(
                "systemd's user manager does not find {unit} although {} exists, so it searches other directories than this shell's environment names (its `XDG_CONFIG_HOME` differs; `systemctl --user show-environment` shows it), and connect leaves the schedule as it is: re-run connect with the manager's environment, or move the schedule's files to a directory the manager searches and run `systemctl --user daemon-reload`",
                dir.join(unit).display()
            ));
        }
        _ => {
            return Some(format!(
                "systemd's user manager has no unit file for {unit} ({} is gone), so the schedule is incomplete and connect leaves it as it is: re-create it with `pseudolife-mcp update --schedule HH:MM --daemon-url {TARGET}` at your time, or remove what is left of it, then re-run connect",
                written.display()
            ));
        }
    }
    let read: std::collections::BTreeSet<PathBuf> =
        read.iter().map(|path| canonical(path)).collect();
    dropins.into_iter().find(|path| !read.contains(&canonical(path))).map(|path| {
        format!(
            "systemd's user manager applies the drop-in {}, which connect's own search did not read (the manager's search path can differ from this shell's), so connect cannot tell whether it changes the run and leaves the schedule as it is: if it should not apply, remove it and run `systemctl --user daemon-reload`, then re-run connect; otherwise manage this schedule by hand",
            path.display()
        )
    })
}

/// A `show` value listing paths, as systemd v259 prints a string array
/// (`bus-print-properties.c` through `shell_maybe_quote(…, 0)`, escape.c):
/// space-separated, each either bare or in double quotes with `\`, `"`,
/// `` ` `` and `$` backslash-escaped and control bytes C-escaped. `None`
/// for anything else, which the caller treats as unknown.
fn show_paths(value: &str) -> Option<Vec<PathBuf>> {
    let mut paths = Vec::new();
    let mut chars = value.trim().chars().peekable();
    loop {
        while chars.peek() == Some(&' ') {
            chars.next();
        }
        let Some(first) = chars.next() else {
            return Some(paths);
        };
        let mut path = String::new();
        if first == '"' {
            loop {
                match chars.next()? {
                    '"' => break,
                    '\\' => match chars.next()? {
                        c @ ('\\' | '"' | '`' | '$' | '\'') => path.push(c),
                        'n' => path.push('\n'),
                        't' => path.push('\t'),
                        'r' => path.push('\r'),
                        'a' => path.push('\u{7}'),
                        'b' => path.push('\u{8}'),
                        'f' => path.push('\u{c}'),
                        'v' => path.push('\u{b}'),
                        'x' => {
                            let high = chars.next()?.to_digit(16)?;
                            let low = chars.next()?.to_digit(16)?;
                            let byte = (high * 16 + low) as u8;
                            // A C-escaped byte above 0x7f is part of invalid
                            // UTF-8, which a Rust path string cannot hold.
                            if byte > 0x7f {
                                return None;
                            }
                            path.push(byte as char);
                        }
                        _ => return None,
                    },
                    c => path.push(c),
                }
            }
            if chars.peek().is_some_and(|c| *c != ' ') {
                return None;
            }
        } else {
            if matches!(first, '\'' | '\\') {
                return None;
            }
            path.push(first);
            while let Some(c) = chars.peek().copied() {
                if c == ' ' {
                    break;
                }
                if matches!(c, '"' | '\'' | '\\') {
                    return None;
                }
                path.push(c);
                chars.next();
            }
        }
        paths.push(PathBuf::from(path));
    }
}

/// The user unit search path of systemd.unit(5) ("User Unit Search Path"),
/// highest priority first, with its XDG variables read through `env`;
/// `dir` is `$XDG_CONFIG_HOME/systemd/user`. The XDG lists follow what
/// `systemd-analyze --user unit-paths` printed (systemd 259, WSL Ubuntu, 15
/// November 2026): an unset `XDG_CONFIG_DIRS` adds nothing, and a set
/// `XDG_DATA_DIRS` keeps the vendor share directories after their `lib`
/// siblings. A relative or empty XDG entry is ignored, and a repeated
/// directory is listed once.
fn systemd_roots(dir: &Path, env: &dyn Fn(&str) -> Option<String>, home: &Path) -> Vec<PathBuf> {
    let absolute = |value: &str| Some(PathBuf::from(value)).filter(|path| path.is_absolute());
    let set = |key: &str| env(key).filter(|value| !value.is_empty());
    let list = |value: &str| -> Vec<PathBuf> {
        value
            .split(':')
            .filter_map(absolute)
            .map(|entry| entry.join("systemd/user"))
            .collect()
    };
    let runtime = set("XDG_RUNTIME_DIR").as_deref().and_then(absolute);
    let run = |leaf: &str| {
        runtime
            .iter()
            .map(|run| run.join("systemd").join(leaf))
            .collect::<Vec<_>>()
    };
    let data_home = set("XDG_DATA_HOME")
        .as_deref()
        .and_then(absolute)
        .unwrap_or_else(|| home.join(".local/share"));
    let vendor = |paths: &[&str]| paths.iter().map(PathBuf::from).collect::<Vec<_>>();
    let mut roots = vec![dir.with_file_name("user.control")];
    roots.extend(run("user.control"));
    roots.extend(run("transient"));
    roots.extend(run("generator.early"));
    roots.push(dir.to_path_buf());
    roots.extend(
        set("XDG_CONFIG_DIRS")
            .as_deref()
            .map(list)
            .unwrap_or_default(),
    );
    roots.push(PathBuf::from("/etc/systemd/user"));
    roots.extend(run("user"));
    roots.push(PathBuf::from("/run/systemd/user"));
    roots.extend(run("generator"));
    roots.push(data_home.join("systemd/user"));
    match set("XDG_DATA_DIRS") {
        Some(value) => {
            roots.extend(list(&value));
            roots.extend(vendor(&[
                "/usr/local/lib/systemd/user",
                "/usr/local/share/systemd/user",
                "/usr/lib/systemd/user",
                "/usr/share/systemd/user",
            ]));
        }
        None => roots.extend(vendor(&[
            "/usr/local/share/systemd/user",
            "/usr/share/systemd/user",
            "/usr/local/lib/systemd/user",
            "/usr/lib/systemd/user",
        ])),
    }
    roots.extend(run("generator.late"));
    let mut seen = std::collections::BTreeSet::new();
    roots.retain(|root| seen.insert(root.clone()));
    roots
}

/// The keys a drop-in may not set for `connect` to vouch for the schedule.
/// In the service, what the run executes and with which environment: every
/// command-running directive of systemd.service(5) (any of them may name a
/// daemon URL) and `ExecSearchPath=`, which decides which executable runs;
/// the environment carries the bearer
/// file and layout variables the base unit's shape check compares. In the
/// timer, any key: every `[Timer]` key decides when it runs or which unit
/// it starts, and `connect` reports and keeps that daily time. Any other
/// key (`Type=`, `TimeoutStopFailureMode=`, `[Unit]`, `[Install]`) changes
/// neither, and survives a re-creation, which rewrites only the base files.
fn blocking_key(kind: &str, section: &str, key: &str) -> bool {
    match (kind, section) {
        ("service", "Service") => matches!(
            key,
            "ExecCondition"
                | "ExecStartPre"
                | "ExecStart"
                | "ExecStartPost"
                | "ExecReload"
                | "ExecStop"
                | "ExecStopPost"
                | "ExecSearchPath"
                | "Environment"
                | "EnvironmentFile"
                | "UnsetEnvironment"
                | "PassEnvironment"
        ),
        ("timer", "Timer") => true,
        _ => false,
    }
}

/// `text` split into lines as systemd's `read_line` splits a file
/// (src/basic/fileio.c, v259): a line ends at `\n`, `\r` or NUL, and at a
/// run of them in which no marker repeats (`\r\n`, `\n\r`, each optionally
/// followed by NUL), so a lone CR ends a line too.
fn systemd_lines(text: &str) -> Vec<&str> {
    const NUL: u8 = 1;
    const LF: u8 = 2;
    const CR: u8 = 4;
    let marker = |byte: u8| match byte {
        b'\0' => NUL,
        b'\n' => LF,
        b'\r' => CR,
        _ => 0,
    };
    let bytes = text.as_bytes();
    let (mut lines, mut start, mut at) = (Vec::new(), 0, 0);
    while at < bytes.len() {
        if marker(bytes[at]) == 0 {
            at += 1;
            continue;
        }
        lines.push(&text[start..at]);
        let mut seen = 0u8;
        while at < bytes.len() {
            let eol = marker(bytes[at]);
            if eol == 0 || seen & NUL != 0 || seen & eol != 0 {
                break;
            }
            seen |= eol;
            at += 1;
        }
        start = at;
    }
    if start < bytes.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// Why a drop-in cannot be read as systemd reads it.
enum Unreadable {
    /// systemd itself rejects the line (an unterminated section header, or
    /// one with characters it refuses), so the unit does not load as
    /// `--schedule` wrote it.
    Rejected(usize, &'static str),
}

/// The first blocking key `text`, a drop-in for a unit of `kind`, sets,
/// read exactly as systemd's `config_parse` and `parse_line` read a file
/// (src/shared/conf-parser.c, v259):
/// - a line whose first non-whitespace character is `#` or `;` is a
///   comment, skipped whole, also in the middle of a continuation, before
///   anything else is decided about it;
/// - a UTF-8 byte-order mark is dropped from the first line that has one;
/// - a line continues onto the next when the text so far ends in an
///   unescaped backslash (an odd run of trailing backslashes, nothing after
///   it, decided on the raw line: `x \` followed by a space does not
///   continue, nor does `x\\`); the backslash becomes a space;
/// - the joined line is trimmed; a `[...]` line opens a section (an
///   unterminated one, or one with unsafe characters, makes systemd reject
///   the file); a line with no `=`, or an empty key, is ignored, as is a key
///   outside any section;
/// - a continuation still open at the end of the file is a last line.
fn dropin_sets(kind: &str, text: &str) -> std::result::Result<Option<String>, Unreadable> {
    let whitespace = |c: char| matches!(c, ' ' | '\t' | '\n' | '\r');
    let mut section: Option<String> = None;
    let mut continuation: Option<String> = None;
    let mut bom_seen = false;
    let mut line_number = 0;
    let mut handle =
        |line: &str, number: usize| -> std::result::Result<Option<String>, Unreadable> {
            let line = line.trim_matches(whitespace);
            if line.is_empty() {
                return Ok(None);
            }
            if let Some(rest) = line.strip_prefix('[') {
                let Some(name) = rest.strip_suffix(']') else {
                    return Err(Unreadable::Rejected(
                        number,
                        "an unterminated section header",
                    ));
                };
                // systemd's string_is_safe (src/basic/string-util.c, v259),
                // byte for byte: no ASCII control byte, DEL, quote or
                // backslash; bytes above 0x7f (a signed char) pass.
                if name
                    .bytes()
                    .any(|b| (1..b' ').contains(&b) || matches!(b, b'"' | b'\'' | b'\\' | 0x7f))
                {
                    return Err(Unreadable::Rejected(
                        number,
                        "a section header with characters systemd refuses",
                    ));
                }
                section = Some(name.to_owned());
                return Ok(None);
            }
            let Some(section) = &section else {
                return Ok(None);
            };
            let Some((key, _)) = line.split_once('=') else {
                return Ok(None);
            };
            let key = key.trim_matches(whitespace);
            if key.is_empty() {
                return Ok(None);
            }
            Ok(blocking_key(kind, section, key).then(|| format!("{key}=")))
        };
    for raw in systemd_lines(text) {
        line_number += 1;
        if raw.trim_start_matches(whitespace).starts_with(['#', ';']) {
            continue;
        }
        let mut line = raw;
        if !bom_seen && let Some(rest) = raw.strip_prefix('\u{feff}') {
            line = rest;
            bom_seen = true;
        }
        let mut joined = continuation.take().unwrap_or_default();
        joined.push_str(line);
        let mut escaped = false;
        for c in joined.chars() {
            escaped = if escaped { false } else { c == '\\' };
        }
        if escaped {
            joined.pop();
            joined.push(' ');
            continuation = Some(joined);
            continue;
        }
        if let Some(key) = handle(&joined, line_number)? {
            return Ok(Some(key));
        }
    }
    if let Some(rest) = continuation {
        return handle(&rest, line_number + 1);
    }
    Ok(None)
}

/// The drop-ins for the unit and its timer, as connect reads them: every
/// `.conf` file in the unit's own `<name>.<kind>.d`, its dash-prefix ones
/// (`pseudolife-update.service.d`, `pseudolife-.service.d`) and the
/// top-level `<kind>.d`, under each of `roots`. Every file is read and none
/// hides another: systemd lets a same-named file in an earlier directory
/// mask a later one (`conf_files_list_strv`), but whether it applies the
/// one that blocks or the one that does not depends on its loaded state,
/// so a blocking file blocks wherever it is, and its row says when systemd
/// may be ignoring it. The files read, the service's and the timer's apart
/// (each was checked only for its own unit's keys), or the sentence that
/// leaves the schedule alone: a blocking key set, or a directory or file
/// that cannot be read or that systemd rejects.
fn systemd_dropins(
    roots: &[PathBuf],
    name: &str,
) -> std::result::Result<[std::collections::BTreeSet<PathBuf>; 2], String> {
    let mut stems: Vec<String> = name
        .match_indices('-')
        .map(|(at, _)| name[..=at].to_owned())
        .collect();
    stems.push(name.to_owned());
    let mut read: [std::collections::BTreeSet<PathBuf>; 2] = Default::default();
    for (index, kind) in ["service", "timer"].into_iter().enumerate() {
        let specific = roots.iter().flat_map(|root| {
            stems
                .iter()
                .rev()
                .map(move |stem| root.join(format!("{stem}.{kind}.d")))
        });
        let generic = roots.iter().map(|root| root.join(format!("{kind}.d")));
        let mut files = Vec::new();
        for directory in specific.chain(generic) {
            let entries = match fs::read_dir(&directory) {
                Ok(entries) => entries,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    return Err(format!(
                        "connect cannot read the systemd drop-in directory {} ({error}), so it cannot tell whether a drop-in there changes the run and leaves the schedule as it is: fix that directory's permissions or remove it, then re-run connect",
                        directory.display()
                    ));
                }
            };
            let mut found: Vec<_> = entries
                .flatten()
                .map(|entry| entry.path())
                .filter(|file| file.extension().is_some_and(|ext| ext == "conf"))
                .collect();
            found.sort();
            files.extend(found);
        }
        // A name that appears in more than one directory: systemd applies
        // only one of them.
        let shared = |file: &Path| {
            files
                .iter()
                .filter(|other| other.file_name() == file.file_name())
                .count()
                > 1
        };
        for file in &files {
            let masked = if shared(file) {
                " (systemd may be ignoring this file because a same-named one takes precedence; remove it or the override to let connect manage the schedule)"
            } else {
                ""
            };
            let text = match fs::read_to_string(file) {
                Ok(text) => text,
                Err(error) => {
                    return Err(format!(
                        "connect cannot read the systemd drop-in {}{masked} ({error}), so it cannot tell whether that file changes the run and leaves the schedule as it is: fix or remove the file, then re-run connect",
                        file.display()
                    ));
                }
            };
            match dropin_sets(kind, &text) {
                Ok(None) => {
                    read[index].insert(file.clone());
                }
                Ok(Some(key)) => {
                    return Err(format!(
                        "the systemd drop-in {}{masked} sets {key}, which takes precedence over the files `update --schedule` writes, so connect cannot tell which daemon the run targets, and re-scheduling would not change it. The schedule stays hand-managed while that drop-in sets it: remove the drop-in and run `systemctl --user daemon-reload` for connect to re-create the schedule on its next run, or keep it and point it at the new daemon yourself",
                        file.display()
                    ));
                }
                Err(Unreadable::Rejected(line, what)) => {
                    return Err(format!(
                        "systemd rejects the drop-in {}{masked} (line {line}: {what}), so the unit does not load as `update --schedule` writes it, and connect leaves the schedule as it is: fix or remove that file, run `systemctl --user daemon-reload`, then re-run connect",
                        file.display()
                    ));
                }
            }
        }
    }
    Ok(read)
}

fn between<'a>(text: &'a str, open: &str, close: &str) -> Option<&'a str> {
    let start = text.find(open)? + open.len();
    let end = text[start..].find(close)?;
    Some(&text[start..start + end])
}

fn xml_unescape(text: &str) -> String {
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

/// `HH:MM` from the start of `text`, when it is one.
fn clock(text: &str) -> Option<String> {
    let time = text.get(..5)?;
    let bytes = time.as_bytes();
    (bytes[2] == b':'
        && [0, 1, 3, 4].iter().all(|&i| bytes[i].is_ascii_digit())
        && at(time).is_ok())
    .then(|| time.to_owned())
}

/// The value after `flag` in a command line that quotes each argument with
/// `quote`, as `--schedule` writes it.
fn flag_value(text: &str, quote: char) -> Option<String> {
    let open = format!("{quote}--daemon-url{quote} {quote}");
    let start = text.find(&open)? + open.len();
    let end = text[start..].find(quote)?;
    Some(text[start..start + end].to_owned())
}

fn decode_base64(text: &str) -> Option<Vec<u8>> {
    let value = |c: u8| -> Option<u32> {
        Some(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32)
    };
    let bytes = text.trim().as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::new();
    let last = bytes.len() / 4;
    for (index, chunk) in bytes.chunks(4).enumerate() {
        let pad = chunk.iter().rev().take_while(|&&c| c == b'=').count();
        // At most two pad characters, and only at the very end.
        if pad > 2 || (pad > 0 && index + 1 != last) {
            return None;
        }
        let mut n = 0u32;
        for &c in &chunk[..4 - pad] {
            n = (n << 6) | value(c)?;
        }
        n <<= 6 * pad as u32;
        let decoded = [(n >> 16) as u8, (n >> 8) as u8, n as u8];
        out.extend_from_slice(&decoded[..3 - pad]);
    }
    Some(out)
}

/// The daemon URL a task's arguments name: plain, or inside the encoded
/// wrapper `--schedule` writes when layout overrides are set.
fn windows_url(arguments: &str) -> Option<String> {
    if let Some(encoded) = arguments
        .split_once("-EncodedCommand ")
        .map(|(_, encoded)| encoded)
    {
        let bytes = decode_base64(encoded)?;
        let units: Vec<u16> = bytes
            .chunks_exact(2)
            .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
            .collect();
        return flag_value(&String::from_utf16(&units).ok()?, '\'');
    }
    flag_value(arguments, '"')
}

/// A scheduled task's `/XML`, as `--schedule` registers it: one action
/// exactly as it registers it for the URL it names (`expected` gives that),
/// one daily calendar trigger, nothing disabled, an interactive logon.
fn windows_parse(
    task: &str,
    client: bool,
    xml: &str,
    expected: &dyn Fn(&str) -> Result<(String, String)>,
) -> Installed {
    let executable = between(xml, "<Command>", "</Command>").map(xml_unescape);
    let arguments = between(xml, "<Arguments>", "</Arguments>").map(xml_unescape);
    let time = between(xml, "<StartBoundary>", "</StartBoundary>")
        .and_then(|start| start.split_once('T'))
        .and_then(|(_, time)| clock(time));
    let url = arguments.as_deref().and_then(windows_url);
    let triggers = between(xml, "<Triggers>", "</Triggers>").unwrap_or_default();
    // Settings `--schedule` never writes, which re-registering would drop:
    // an end date would revive an expired task, a repetition or delay would
    // change when it runs, and elevation would be lost. `RunLevel` is read
    // only for `HighestAvailable`: the task `--schedule` registers
    // (`-RunLevel Limited`) exports no `RunLevel` at all (Windows dev VM,
    // 15 November 2026), and an absent element means least privilege.
    let mut found: Vec<&str> = [
        ("<EndBoundary>", "an end date"),
        ("<Repetition>", "a repetition"),
        ("<RandomDelay>", "a random delay"),
        ("<Delay>", "a start delay"),
        (
            "<ExecutionTimeLimit>",
            "an execution time limit on its trigger",
        ),
    ]
    .iter()
    .filter(|(element, _)| triggers.contains(element))
    .map(|(_, setting)| *setting)
    .collect();
    if xml.contains("<RunLevel>HighestAvailable</RunLevel>") {
        found.push("highest privileges");
    }
    let differs = match found.as_slice() {
        [] => None,
        [one] => Some((*one).to_owned()),
        [rest @ .., last] => Some(format!("{} and {last}", rest.join(", "))),
    };
    let as_registered = differs.is_none()
        && xml.matches("<Exec>").count() == 1
        && triggers.matches("Trigger>").count() == 2
        && triggers.contains("<CalendarTrigger>")
        && triggers.contains("<DaysInterval>1</DaysInterval>")
        && !xml.contains("<Enabled>false</Enabled>")
        && xml.contains("<LogonType>InteractiveToken</LogonType>");
    let mut why = None;
    // schtasks writes its XML in the console code page; a path outside it
    // arrives as replacement characters and cannot be compared.
    if xml.contains('\u{fffd}') {
        why = Some("Task Scheduler's description of it holds characters connect cannot read, such as a path outside the console code page".to_owned());
    }
    let shaped = why.is_none()
        && as_registered
        && match (&executable, &arguments, &url) {
            (Some(executable), Some(arguments), Some(url)) => match expected(url) {
                Ok((want, args)) => &want == executable && &args == arguments,
                Err(error) => {
                    why = Some(error.message);
                    false
                }
            },
            _ => false,
        };
    Installed {
        client,
        place: format!("the scheduled task '{task}'"),
        time,
        url,
        shaped,
        why,
        overridden: None,
        differs,
        encoded: arguments
            .as_deref()
            .is_some_and(|arguments| arguments.contains("-EncodedCommand ")),
        remove: format!("schtasks /Delete /TN \"{task}\" /F"),
    }
}

/// A systemd unit and its timer, as `--schedule` installs them: the service
/// file exactly as it writes it for the URL it names (`expected` gives it),
/// the timer exactly as it writes it for the time it names, enabled.
#[allow(clippy::too_many_arguments)]
fn systemd_parse(
    dir: &Path,
    name: &str,
    client: bool,
    service: &str,
    timer: &str,
    enabled: bool,
    overridden: Option<String>,
    expected: &dyn Fn(&str) -> Result<String>,
) -> Installed {
    let url = service
        .lines()
        .find_map(|line| line.strip_prefix("ExecStart="))
        .and_then(|exec| flag_value(exec, '"'));
    let time = timer
        .lines()
        .filter_map(|line| line.strip_prefix("OnCalendar="))
        .find_map(|calendar| calendar.split_whitespace().nth(1).and_then(clock));
    let timer_shaped = time
        .as_deref()
        .and_then(|time| at(time).ok())
        .is_some_and(|(hour, minute)| timer == timer_body(name, hour, minute));
    let mut why = None;
    let shaped = overridden.is_none()
        && enabled
        && timer_shaped
        && match url.as_deref() {
            Some(url) => match expected(url) {
                Ok(want) => want == service,
                Err(error) => {
                    why = Some(error.message);
                    false
                }
            },
            None => false,
        };
    let file = |suffix: &str| format!("'{}'", dir.join(format!("{name}.{suffix}")).display());
    Installed {
        client,
        place: dir.join(format!("{name}.service")).display().to_string(),
        time,
        url,
        shaped,
        why,
        overridden,
        differs: None,
        encoded: false,
        remove: format!(
            "systemctl --user disable --now {name}.timer && rm {} {} && systemctl --user daemon-reload",
            file("service"),
            file("timer")
        ),
    }
}

fn options(url: &str) -> Update {
    Update::new(Options {
        daemon_url: url.to_owned(),
        as_json: true,
        ..Options::default()
    })
}

/// Why `--schedule` here would not re-create a schedule: `other_kind` when
/// it would write the other kind of schedule instead.
pub(crate) struct Refusal {
    pub other_kind: bool,
    pub reason: String,
}

fn same_kind(update: &Update, client: bool) -> std::result::Result<(), Refusal> {
    let other = |reason: &str| Refusal {
        other_kind: true,
        reason: reason.to_owned(),
    };
    match client_kind(update) {
        Ok(here) if here == client => Ok(()),
        Ok(true) => Err(other(
            "this machine runs no daemon container, so `update --schedule` here writes the client schedule instead",
        )),
        Ok(false) => Err(other(
            "this machine runs the daemon container, so `update --schedule` here writes the daemon's schedule instead",
        )),
        Err(error) => Err(Refusal {
            other_kind: false,
            reason: error.message,
        }),
    }
}

/// Why `--schedule` here would not re-create the schedule of this kind for
/// `url` (a different kind, an unlocatable launcher, no bearer for the
/// daemon's run), checked before anything is written.
pub(crate) fn preflight(client: bool, url: &str) -> std::result::Result<(), Refusal> {
    let update = options(url);
    same_kind(&update, client)?;
    let refuse = |reason: String| Refusal {
        other_kind: false,
        reason,
    };
    command(url).map_err(|error| refuse(error.message))?;
    if !client && !daemon_bearer_resolves() {
        return Err(refuse(no_bearer_text(cfg!(windows))));
    }
    Ok(())
}

/// Why a re-creation stopped.
pub(crate) enum Stop {
    /// `check`, run immediately before the write, refused: nothing was
    /// written.
    Changed(String),
    /// `--schedule` itself refused or failed.
    Failed(String),
}

/// Re-create the schedule of this kind daily at `when` against `url`
/// through `--schedule`, running `check` after its own checks (the machine
/// kind, the daemon's `/health` for a client) and immediately before it
/// writes; the steps it reports, or why it stopped.
pub(crate) fn reschedule(
    names: &Names,
    client: bool,
    when: &str,
    url: &str,
    check: &dyn Fn() -> std::result::Result<(), String>,
) -> std::result::Result<Vec<String>, Stop> {
    let mut update = options(url);
    update.o.schedule = Some(when.to_owned());
    // The kind is worked out once: `same_kind` asks `client_kind`, and the
    // verified answer is what `schedule_for` writes (`schedule_checked` would
    // ask again, and a daemon container started or stopped in between would
    // make it write the other kind).
    same_kind(&update, client).map_err(|refusal| Stop::Failed(refusal.reason))?;
    let changed = std::cell::RefCell::new(None);
    let gate = || -> Result<()> {
        check().map_err(|cause| {
            let error = UpdateError::usage(cause.clone());
            *changed.borrow_mut() = Some(cause);
            error
        })
    };
    match schedule_for(&mut update, when, names, client, &command, &gate) {
        Ok(()) => Ok(update.report.steps),
        Err(error) => Err(match changed.take() {
            Some(cause) => Stop::Changed(cause),
            None => Stop::Failed(error.message),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const URL: &str = "http://127.0.0.1:8765";
    const NAME: &str = "pseudolife-update";

    fn argv(url: &str) -> Result<Vec<String>> {
        Ok(vec![
            r"C:\fixture\bin\pseudolife-mcp.exe".into(),
            "update".into(),
            "--unattended".into(),
            "--daemon-url".into(),
            url.into(),
        ])
    }

    /// A task's `/XML` as `--schedule` registers it, with `edit` applied:
    /// the shape `schtasks /Query /XML` exported on the Windows dev VM on 15
    /// November 2026 for a task registered as `--schedule` registers it (no
    /// `RunLevel`, no `Enabled` element), its user id redacted.
    fn task_xml(command: &str, arguments: &str, edit: impl Fn(String) -> String) -> String {
        let escape = |text: &str| text.replace('&', "&amp;").replace('"', "&quot;");
        edit(format!(
            "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\n<Task version=\"1.3\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\n  <RegistrationInfo>\n    <URI>\\T</URI>\n  </RegistrationInfo>\n  <Principals>\n    <Principal id=\"Author\">\n      <UserId>user</UserId>\n      <LogonType>InteractiveToken</LogonType>\n    </Principal>\n  </Principals>\n  <Settings>\n    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>\n    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>\n    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n    <IdleSettings>\n      <Duration>PT10M</Duration>\n      <WaitTimeout>PT1H</WaitTimeout>\n      <StopOnIdleEnd>true</StopOnIdleEnd>\n      <RestartOnIdle>false</RestartOnIdle>\n    </IdleSettings>\n    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>\n  </Settings>\n  <Triggers>\n    <CalendarTrigger>\n      <StartBoundary>2026-11-14T03:07:00+11:00</StartBoundary>\n      <ScheduleByDay>\n        <DaysInterval>1</DaysInterval>\n      </ScheduleByDay>\n    </CalendarTrigger>\n  </Triggers>\n  <Actions Context=\"Author\">\n    <Exec>\n      <Command>{}</Command>\n      <Arguments>{}</Arguments>\n    </Exec>\n  </Actions>\n</Task>\n",
            escape(command),
            escape(arguments)
        ))
    }

    fn windows_expected(url: &str) -> Result<(String, String)> {
        let argv = argv(url)?;
        let arguments = argv[1..]
            .iter()
            .map(|s| windows_quote(s))
            .collect::<Result<Vec<_>>>()?
            .join(" ");
        Ok((argv[0].clone(), arguments))
    }

    #[test]
    fn a_task_as_schedule_registers_it_reads_back_its_url_and_time() {
        let (command, arguments) = windows_expected(URL).unwrap();
        let found = windows_parse(
            "T",
            true,
            &task_xml(&command, &arguments, |x| x),
            &windows_expected,
        );
        assert!(found.shaped);
        assert!(found.client);
        assert!(found.why.is_none());
        assert_eq!(found.url.as_deref(), Some(URL));
        assert_eq!(found.time.as_deref(), Some("03:07"));
        assert_eq!(found.place, "the scheduled task 'T'");
        assert_eq!(found.remove, "schtasks /Delete /TN \"T\" /F");
    }

    #[test]
    fn a_task_changed_from_what_schedule_registers_is_not_shaped() {
        let (command, arguments) = windows_expected(URL).unwrap();
        let shaped = |xml: String| windows_parse("T", false, &xml, &windows_expected).shaped;
        // Near misses: unchanged, and with the least-privilege default
        // written out, it is shaped.
        assert!(shaped(task_xml(&command, &arguments, |x| x)));
        assert!(shaped(task_xml(&command, &arguments, |x| x.replace(
            "</LogonType>",
            "</LogonType>\n      <RunLevel>LeastPrivilege</RunLevel>"
        ))));
        // The same URL, with an argument added by hand.
        let edited = windows_parse(
            "T",
            false,
            &task_xml(
                &command,
                &format!("{arguments} \"--tag\" \"0.23.1\""),
                |x| x,
            ),
            &windows_expected,
        );
        assert_eq!(edited.url.as_deref(), Some(URL));
        assert!(!edited.shaped);
        assert!(!shaped(task_xml(
            r"C:\elsewhere\pseudolife-mcp.exe",
            &arguments,
            |x| x
        )));
        for (what, edit) in [
            (
                "a second action",
                Box::new(|x: String| {
                    x.replace("</Actions>", "<Exec><Command>x</Command></Exec></Actions>")
                }) as Box<dyn Fn(String) -> String>,
            ),
            (
                "disabled",
                Box::new(|x: String| {
                    x.replace("<Settings>", "<Settings>\n    <Enabled>false</Enabled>")
                }),
            ),
            (
                "weekly",
                Box::new(|x: String| {
                    x.replace("ScheduleByDay>", "ScheduleByWeek>").replace(
                        "<DaysInterval>1</DaysInterval>",
                        "<WeeksInterval>1</WeeksInterval>",
                    )
                }),
            ),
            (
                "a second trigger",
                Box::new(|x: String| {
                    x.replace(
                        "</Triggers>",
                        "<LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>",
                    )
                }),
            ),
            (
                "run whether logged on or not",
                Box::new(|x: String| x.replace("InteractiveToken", "Password")),
            ),
        ] {
            assert!(!shaped(task_xml(&command, &arguments, edit)), "{what}");
        }
    }

    #[test]
    fn a_task_setting_schedule_never_writes_is_named_and_not_shaped() {
        let (command, arguments) = windows_expected(URL).unwrap();
        let read = |edit: &dyn Fn(String) -> String| {
            windows_parse(
                "T",
                false,
                &task_xml(&command, &arguments, edit),
                &windows_expected,
            )
        };
        let plain = read(&|x| x);
        assert!(plain.shaped && plain.differs.is_none());
        let start = "</StartBoundary>";
        for (setting, edit) in [
            (
                "an end date",
                Box::new(move |x: String| {
                    x.replace(
                        start,
                        "</StartBoundary><EndBoundary>2026-11-20T00:00:00</EndBoundary>",
                    )
                }) as Box<dyn Fn(String) -> String>,
            ),
            (
                "a repetition",
                Box::new(move |x: String| {
                    x.replace(
                        start,
                        "</StartBoundary><Repetition><Interval>PT1H</Interval></Repetition>",
                    )
                }),
            ),
            (
                "a random delay",
                Box::new(|x: String| {
                    x.replace(
                        "</ScheduleByDay>",
                        "</ScheduleByDay><RandomDelay>PT1H</RandomDelay>",
                    )
                }),
            ),
            (
                "a start delay",
                Box::new(move |x: String| x.replace(start, "</StartBoundary><Delay>PT5M</Delay>")),
            ),
            (
                "an execution time limit on its trigger",
                Box::new(move |x: String| {
                    x.replace(
                        start,
                        "</StartBoundary><ExecutionTimeLimit>PT1H</ExecutionTimeLimit>",
                    )
                }),
            ),
            (
                "highest privileges",
                Box::new(|x: String| {
                    x.replace(
                        "</LogonType>",
                        "</LogonType><RunLevel>HighestAvailable</RunLevel>",
                    )
                }),
            ),
        ] {
            let found = read(&edit);
            assert!(!found.shaped, "{setting}");
            assert_eq!(found.differs.as_deref(), Some(setting));
        }
        // Several are listed together.
        let both = read(&|x: String| {
            x.replace(
                start,
                "</StartBoundary><EndBoundary>2026-11-20T00:00:00</EndBoundary><Delay>PT5M</Delay>",
            )
            .replace(
                "</LogonType>",
                "</LogonType><RunLevel>HighestAvailable</RunLevel>",
            )
        });
        assert_eq!(
            both.differs.as_deref(),
            Some("an end date, a start delay and highest privileges")
        );
        // Settings' own ExecutionTimeLimit (outside the trigger) is the
        // default every registration carries, not a trigger setting.
        let settings_limit = read(&|x: String| {
            x.replace(
                "</Settings>",
                "<ExecutionTimeLimit>PT72H</ExecutionTimeLimit></Settings>",
            )
        });
        assert!(settings_limit.shaped && settings_limit.differs.is_none());
    }

    #[test]
    fn a_task_that_cannot_be_compared_says_why() {
        let (command, arguments) = windows_expected(URL).unwrap();
        let no_launcher = |_: &str| -> Result<(String, String)> {
            Err(UpdateError::usage("no verified stable native launcher"))
        };
        let found = windows_parse(
            "T",
            false,
            &task_xml(&command, &arguments, |x| x),
            &no_launcher,
        );
        assert!(!found.shaped);
        assert_eq!(
            found.why.as_deref(),
            Some("no verified stable native launcher")
        );
        let lossy = task_xml(&command.replace("fixture", "Zo\u{fffd}"), &arguments, |x| x);
        let found = windows_parse("T", false, &lossy, &windows_expected);
        assert!(!found.shaped);
        assert!(found.why.unwrap().contains("console code page"));
    }

    #[test]
    fn an_encoded_layout_wrapper_names_its_url() {
        let script = format!(
            "$env:XDG_DATA_HOME='C:\\x';& 'C:\\b.exe' 'update' '--unattended' '--daemon-url' '{URL}';exit $LASTEXITCODE"
        );
        let encoded = base64(
            &script
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        );
        assert_eq!(
            windows_url(&format!(
                "-NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"
            ))
            .as_deref(),
            Some(URL)
        );
        // Malformed padding (a hand-edited task) reads as no URL, never a panic.
        for text in ["====", "A===", "AB=C", "AB==AB==", "A"] {
            assert_eq!(decode_base64(text), None, "{text}");
        }
        assert_eq!(windows_url("-EncodedCommand ===="), None);
        for text in ["", "a", "ab", "abc", "abcd", "\u{e9}t\u{e9}"] {
            assert_eq!(
                decode_base64(&base64(text.as_bytes())).unwrap(),
                text.as_bytes()
            );
        }
    }

    #[test]
    fn a_unit_as_schedule_installs_it_reads_back_and_anything_else_is_not_shaped() {
        let environment = vec![format!(
            "Environment={}",
            unit_quote(
                "PSEUDOLIFE_MCP_TOKEN_FILE=/home/u/.pseudolife-mcp/notice.token",
                false
            )
            .unwrap()
        )];
        let service = service_body(NAME, &argv(URL).unwrap(), &environment).unwrap();
        let timer = timer_body(NAME, 22, 45);
        let expected = |url: &str| service_body(NAME, &argv(url)?, &environment);
        let dir = Path::new("/home/u/.config/systemd/user");
        let parse = |service: &str, timer: &str, enabled: bool| {
            systemd_parse(dir, NAME, false, service, timer, enabled, None, &expected)
        };
        let found = parse(&service, &timer, true);
        assert!(found.shaped);
        assert!(!found.client);
        assert_eq!(found.url.as_deref(), Some(URL));
        assert_eq!(found.time.as_deref(), Some("22:45"));
        assert_eq!(
            found.place,
            dir.join("pseudolife-update.service").display().to_string()
        );
        assert!(
            found
                .remove
                .starts_with("systemctl --user disable --now pseudolife-update.timer && rm ")
        );
        // Near misses: a flag added by hand, another bearer file, a second
        // ExecStart, a monthly calendar, no timer, a disabled timer.
        let edited = service.replace("--unattended\"", "--unattended\" \"--tag\" \"0.23.1\"");
        assert!(!parse(&edited, &timer, true).shaped);
        let other_bearer = service.replace("notice.token", "other.token");
        assert!(!parse(&other_bearer, &timer, true).shaped);
        let twice = format!("{service}ExecStart=/bin/true\n");
        assert!(!parse(&twice, &timer, true).shaped);
        let monthly = timer.replace("OnCalendar=*-*-* 22:45:00", "OnCalendar=*-*-01 22:45:00");
        let found = parse(&service, &monthly, true);
        assert!(!found.shaped);
        assert_eq!(found.time.as_deref(), Some("22:45"));
        assert_eq!(parse(&service, "", true).time, None);
        assert!(!parse(&service, "", true).shaped);
        assert!(!parse(&service, &timer, false).shaped);
        // An override beside the matching files: not shaped, and carried.
        let found = systemd_parse(
            dir,
            NAME,
            false,
            &service,
            &timer,
            true,
            Some("the systemd drop-in X sets ExecStart=".to_owned()),
            &expected,
        );
        assert!(!found.shaped);
        assert!(found.why.is_none());
        assert_eq!(
            found.overridden.as_deref(),
            Some("the systemd drop-in X sets ExecStart=")
        );
    }

    /// The cause clause of `systemd_dropins`'s refusal (up to its ", which"
    /// or ", so"), or `None` when it reads every drop-in as harmless.
    fn systemd_override(roots: &[PathBuf], name: &str) -> Option<String> {
        systemd_dropins(roots, name).err().map(|text| {
            let cut = [", which", ", so"]
                .iter()
                .filter_map(|mark| text.find(mark))
                .min()
                .unwrap_or(text.len());
            text[..cut].to_owned()
        })
    }

    /// `path` as systemd's `shell_maybe_quote(…, 0)` prints it in a `show`
    /// path list: bare, or in double quotes with `\`, `"`, `` ` `` and `$`
    /// escaped when it holds whitespace or a shell-special character (a
    /// Windows path's backslashes included).
    fn show_quote(path: &Path) -> String {
        let text = path.display().to_string();
        let special = |c: char| c.is_whitespace() || "\"\\`$*?[]'()<>|&;!".contains(c);
        if !text.chars().any(special) {
            return text;
        }
        let mut quoted = String::from("\"");
        for c in text.chars() {
            if matches!(c, '\\' | '"' | '`' | '$') {
                quoted.push('\\');
            }
            quoted.push(c);
        }
        quoted.push('"');
        quoted
    }

    /// A throwaway directory under the system temp directory.
    fn scratch() -> PathBuf {
        let root =
            std::env::temp_dir().join(format!("pseudolife-test-{}", uuid::Uuid::new_v4().simple()));
        fs::create_dir(&root).unwrap();
        root
    }

    #[test]
    fn a_drop_in_blocks_only_when_it_sets_a_key_the_run_depends_on() {
        let root = scratch();
        let roots = [root.clone()];
        let name = "pseudolife-update";
        let put = |dir: &str, file: &str, text: &str| {
            let path = root.join(dir);
            fs::create_dir_all(&path).unwrap();
            fs::write(path.join(file), text).unwrap();
            path.join(file)
        };
        // Near misses: nothing; another unit's override; an empty directory;
        // backups only; a vendor top-level drop-in that sets an unrelated key
        // (Fedora's 10-timeout-abort.conf, as reported); a [Unit] key; a
        // comment and a continuation line.
        assert_eq!(systemd_override(&roots, name), None);
        put(
            "pseudolife-other.service.d",
            "override.conf",
            "[Service]\nExecStart=/bin/true\n",
        );
        fs::create_dir_all(root.join("pseudolife-update.service.d")).unwrap();
        put(
            "pseudolife-update.service.d",
            "override.conf~",
            "[Service]\nExecStart=\n",
        );
        put(
            "pseudolife-update.service.d",
            "override.conf.bak",
            "[Service]\nExecStart=\n",
        );
        put(
            "service.d",
            "10-timeout-abort.conf",
            "[Service]\nTimeoutStopFailureMode=abort\n",
        );
        put(
            "pseudolife-.service.d",
            "10-unit.conf",
            "# a comment\n[Unit]\nDescription=x \\\n  continued\n",
        );
        assert_eq!(systemd_override(&roots, name), None);
        // A comment ending in a backslash continues nothing: the key after
        // it is read (the comment inside a continuation is skipped too).
        let file = put(
            "pseudolife-update.timer.d",
            "60-comment.conf",
            "[Timer]\n# note \\\nUnit=other.service\n",
        );
        assert_eq!(
            systemd_override(&roots, name),
            Some(format!("the systemd drop-in {} sets Unit=", file.display()))
        );
        fs::remove_file(&file).unwrap();
        let file = put(
            "pseudolife-update.service.d",
            "60-comment.conf",
            "[Service]\nDescription=a \\\n# skipped\n  b\nExecStart=/x\n",
        );
        assert_eq!(
            systemd_override(&roots, name),
            Some(format!(
                "the systemd drop-in {} sets ExecStart=",
                file.display()
            ))
        );
        fs::remove_file(&file).unwrap();
        // A key the run depends on, in each place systemd reads one.
        for (dir, text, key) in [
            (
                "pseudolife-update.service.d",
                "[Service]\nExecStart=\nExecStart=/x --daemon-url http://OLD\n",
                "ExecStart=",
            ),
            (
                "pseudolife-.service.d",
                "[Service]\nEnvironment=PSEUDOLIFE_MCP_TOKEN_FILE=/x\n",
                "Environment=",
            ),
            (
                "service.d",
                "[Service]\n EnvironmentFile = /x\n",
                "EnvironmentFile=",
            ),
            (
                "pseudolife-update.service.d",
                "[Service]\nExecStopPost=/x --daemon-url http://OLD\n",
                "ExecStopPost=",
            ),
            (
                "pseudolife-update.service.d",
                "[Service]\nExecStop=/x\n",
                "ExecStop=",
            ),
            (
                "pseudolife-update.service.d",
                "[Service]\nExecCondition=/x\n",
                "ExecCondition=",
            ),
            (
                "pseudolife-update.service.d",
                "[Service]\nExecSearchPath=/opt/old\n",
                "ExecSearchPath=",
            ),
            (
                "pseudolife-update.timer.d",
                "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 05:00:00\n",
                "OnCalendar=",
            ),
            (
                "pseudolife-.timer.d",
                "[Timer]\nUnit=other.service\n",
                "Unit=",
            ),
            ("timer.d", "[Timer]\nOnBootSec=5min\n", "OnBootSec="),
        ] {
            let file = put(dir, "50-test.conf", text);
            assert_eq!(
                systemd_override(&roots, name),
                Some(format!("the systemd drop-in {} sets {key}", file.display())),
                "{dir}"
            );
            fs::remove_file(&file).unwrap();
        }
        // A service key in a timer drop-in, or a timer key in a service one,
        // is not read by systemd for that unit.
        let file = put(
            "pseudolife-update.timer.d",
            "50-test.conf",
            "[Service]\nExecStart=/x\n",
        );
        assert_eq!(systemd_override(&roots, name), None);
        fs::remove_file(&file).unwrap();
        // A line with no `=`, which systemd ignores, is ignored here too.
        let file = put(
            "pseudolife-update.service.d",
            "50-test.conf",
            "[Service]\nnot a key\n=no key\n",
        );
        assert_eq!(systemd_override(&roots, name), None);
        fs::remove_file(&file).unwrap();
        // A section header systemd rejects blocks, naming the line.
        let file = put(
            "pseudolife-update.service.d",
            "50-test.conf",
            "[Service\nNice=5\n",
        );
        assert_eq!(
            systemd_override(&roots, name),
            Some(format!(
                "systemd rejects the drop-in {} (line 1: an unterminated section header)",
                file.display()
            ))
        );
        fs::remove_file(&file).unwrap();
        // A file that is not UTF-8 cannot be read, and blocks.
        let file = root
            .join("pseudolife-update.service.d")
            .join("50-test.conf");
        fs::write(&file, b"[Service]\n\xff\n").unwrap();
        assert!(
            systemd_override(&roots, name)
                .unwrap()
                .starts_with(&format!(
                    "connect cannot read the systemd drop-in {}",
                    file.display()
                ))
        );
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn a_drop_in_is_read_line_by_line_as_systemd_reads_it() {
        let sets = |text: &str| match dropin_sets("service", text) {
            Ok(key) => key,
            Err(Unreadable::Rejected(line, what)) => Some(format!("rejected {line}: {what}")),
        };
        let post = "ExecStartPost=/x --daemon-url http://OLD";
        // A backslash followed by a space, or an escaped backslash, does not
        // continue the line in systemd: the next line is read on its own.
        assert_eq!(
            sets(&format!("[Service]\nNice=5 \\ \n{post}\n")).as_deref(),
            Some("ExecStartPost=")
        );
        assert_eq!(
            sets(&format!("[Service]\nNice=5\\\\\n{post}\n")).as_deref(),
            Some("ExecStartPost=")
        );
        // Near miss: an odd trailing backslash continues it, so the next line
        // is part of `Nice=` and sets nothing.
        assert_eq!(sets(&format!("[Service]\nNice=5 \\\n{post}\n")), None);
        assert_eq!(sets(&format!("[Service]\nNice=5 \\\\\\\n{post}\n")), None);
        // A lone CR ends a line, as systemd's read_line ends one.
        assert_eq!(
            sets(&format!("[Service]\rNice=5\r{post}\r")).as_deref(),
            Some("ExecStartPost=")
        );
        assert_eq!(
            systemd_lines("a\r\nb\n\rc\n\nd\re\0f"),
            ["a", "b", "c", "", "d", "e", "f"]
        );
        // A byte-order mark is dropped, so a harmless file stays harmless and
        // a blocking one still blocks.
        assert_eq!(sets("\u{feff}[Service]\nNice=5\n"), None);
        assert_eq!(
            sets(&format!("\u{feff}[Service]\n{post}\n")).as_deref(),
            Some("ExecStartPost=")
        );
        // A continuation still open at the end of the file is a last line.
        assert_eq!(
            sets("[Service]\nExecStart=/x \\").as_deref(),
            Some("ExecStart=")
        );
        // A section name with a non-ASCII control character (U+0085) is safe
        // to systemd; one with an ASCII control byte or a quote is not.
        assert_eq!(
            sets(&format!("[X-\u{85}]\nNice=5\n[Service]\n{post}\n")).as_deref(),
            Some("ExecStartPost=")
        );
        assert_eq!(sets("[X-\u{85}]\nNice=5\n"), None);
        assert!(
            sets("[X-\u{1}]\nNice=5\n")
                .unwrap()
                .starts_with("rejected 1")
        );
        assert!(sets("[X-\"q]\nNice=5\n").unwrap().starts_with("rejected 1"));
        // A key before any section, or in another section, is not the
        // service's.
        assert_eq!(sets(&format!("{post}\n[Unit]\n{post}\n")), None);
    }

    #[test]
    fn every_drop_in_is_read_and_none_hides_another() {
        let (high, low) = (scratch(), scratch());
        let roots = [high.clone(), low.clone()];
        let name = "pseudolife-update";
        let put = |root: &Path, dir: &str, file: &str, text: &str| {
            fs::create_dir_all(root.join(dir)).unwrap();
            fs::write(root.join(dir).join(file), text).unwrap();
            root.join(dir).join(file)
        };
        let post = "[Service]\nExecStartPost=/x --daemon-url http://OLD\n";
        let hint = "systemd may be ignoring this file because a same-named one takes precedence; remove it or the override to let connect manage the schedule";
        // The `cp -p` case: a harmless file in the higher root's prefix
        // directory shares its name with an old-URL one in the lower root's
        // exact directory. Which one systemd runs depends on what it loaded,
        // so the old-URL one blocks, and says it may be masked.
        put(&high, "pseudolife-.service.d", "override.conf", "");
        let old = put(&low, "pseudolife-update.service.d", "override.conf", post);
        assert_eq!(
            systemd_override(&roots, name),
            Some(format!(
                "the systemd drop-in {} ({hint}) sets ExecStartPost=",
                old.display()
            ))
        );
        fs::remove_dir_all(high.join("pseudolife-.service.d")).unwrap();
        fs::remove_dir_all(low.join("pseudolife-update.service.d")).unwrap();
        // The other way round, and a top-level one under an exact-name one:
        // each blocking file blocks wherever it is.
        put(&high, "pseudolife-update.service.d", "override.conf", "");
        let prefix = put(&low, "pseudolife-.service.d", "override.conf", post);
        assert!(
            systemd_override(&roots, name)
                .unwrap()
                .starts_with(&format!(
                    "the systemd drop-in {} ({hint})",
                    prefix.display()
                ))
        );
        fs::remove_dir_all(low.join("pseudolife-.service.d")).unwrap();
        let generic = put(&high, "service.d", "override.conf", post);
        assert!(
            systemd_override(&roots, name)
                .unwrap()
                .starts_with(&format!(
                    "the systemd drop-in {} ({hint})",
                    generic.display()
                ))
        );
        fs::remove_dir_all(high.join("service.d")).unwrap();
        fs::remove_dir_all(high.join("pseudolife-update.service.d")).unwrap();
        // A name no other directory holds: no masking sentence. A same name
        // in the timer's directories is another unit's set and does not count.
        put(&high, "pseudolife-update.timer.d", "override.conf", "");
        let alone = put(&low, "pseudolife-update.service.d", "override.conf", post);
        assert_eq!(
            systemd_override(&roots, name),
            Some(format!(
                "the systemd drop-in {} sets ExecStartPost=",
                alone.display()
            ))
        );
        fs::remove_dir_all(low.join("pseudolife-update.service.d")).unwrap();
        fs::remove_dir_all(high.join("pseudolife-update.timer.d")).unwrap();
        // A hidden name is read too.
        let hidden = put(&high, "pseudolife-update.service.d", ".hidden.conf", post);
        assert!(
            systemd_override(&roots, name)
                .unwrap()
                .starts_with(&format!("the systemd drop-in {}", hidden.display()))
        );
        fs::remove_dir_all(high.join("pseudolife-update.service.d")).unwrap();
        // Near miss: harmless files sharing a name block nothing, and are all
        // in the set read (each is checked against systemd's report).
        put(&high, "pseudolife-.service.d", "override.conf", "");
        put(
            &low,
            "pseudolife-update.service.d",
            "override.conf",
            "[Service]\nTimeoutStopFailureMode=abort\n",
        );
        let [service, timer] = systemd_dropins(&roots, name).unwrap();
        assert_eq!(service.len(), 2);
        assert!(timer.is_empty());
        fs::remove_dir_all(&high).unwrap();
        fs::remove_dir_all(&low).unwrap();
    }

    #[test]
    fn what_systemd_reports_beyond_this_search_leaves_the_schedule_alone() {
        let dir = scratch();
        let unit = "pseudolife-update.service";
        fs::write(dir.join(unit), "").unwrap();
        let fragment = format!(
            "NeedDaemonReload=no\nFragmentPath={}",
            dir.join(unit).display()
        );
        let read: std::collections::BTreeSet<PathBuf> = [dir.join("a.conf")].into();
        fs::write(dir.join("a.conf"), "").unwrap();
        // Near miss: the unit from `dir`, and only drop-ins this search read.
        assert_eq!(
            systemd_disagrees(&dir, unit, &format!("{fragment}\nDropInPaths=\n"), &read),
            None
        );
        let listed = format!(
            "{fragment}\nDropInPaths={}\n",
            show_quote(&dir.join("a.conf"))
        );
        assert_eq!(systemd_disagrees(&dir, unit, &listed, &read), None);
        // A drop-in systemd applies that this search did not read.
        let unseen = format!(
            "{fragment}\nDropInPaths={} /opt/x/pseudolife-update.service.d/b.conf\n",
            show_quote(&dir.join("a.conf"))
        );
        assert!(
            systemd_disagrees(&dir, unit, &unseen, &read)
                .unwrap()
                .starts_with("systemd's user manager applies the drop-in /opt/x/pseudolife-update.service.d/b.conf")
        );
        // The unit loaded from another file, or from nowhere.
        let shadowed = "NeedDaemonReload=no\nFragmentPath=/run/user/1000/systemd/transient/pseudolife-update.service\nDropInPaths=\n";
        assert!(
            systemd_disagrees(&dir, unit, shadowed, &read)
                .unwrap()
                .contains("loads pseudolife-update.service from /run/user/1000/systemd/transient/")
        );
        // A unit file this search read but the manager does not find: its
        // search path differs, and re-scheduling into the same place would
        // not help.
        let unfound = systemd_disagrees(
            &dir,
            unit,
            "NeedDaemonReload=no\nFragmentPath=\nDropInPaths=\n",
            &read,
        )
        .unwrap();
        assert!(
            unfound.contains("does not find pseudolife-update.service although"),
            "{unfound}"
        );
        assert!(!unfound.contains("update --schedule"), "{unfound}");
        // No unit file at all: the cause is that it is gone, and the fix is
        // re-creating it, not removing another file.
        fs::remove_file(dir.join(unit)).unwrap();
        let gone = systemd_disagrees(
            &dir,
            unit,
            "NeedDaemonReload=no\nFragmentPath=\nDropInPaths=\n",
            &read,
        )
        .unwrap();
        assert!(
            gone.contains("has no unit file for pseudolife-update.service"),
            "{gone}"
        );
        assert!(
            gone.contains(&format!(
                "re-create it with `pseudolife-mcp update --schedule HH:MM --daemon-url {TARGET}`"
            )),
            "{gone}"
        );
        assert!(!gone.contains("remove the other unit file"), "{gone}");
        fs::write(dir.join(unit), "").unwrap();
        // A drop-in path systemd quotes (it holds a space) is decoded and
        // matched, not mistaken for one this search did not read.
        let spaced = dir.join("with space.conf");
        fs::write(&spaced, "").unwrap();
        let read_spaced: std::collections::BTreeSet<PathBuf> = [spaced.clone()].into();
        let quoted = format!("{fragment}\nDropInPaths={}\n", show_quote(&spaced));
        assert!(quoted.contains("DropInPaths=\""), "{quoted}");
        assert_eq!(
            systemd_disagrees(&dir, unit, &quoted, &read_spaced),
            None,
            "{quoted}"
        );
        // A report this decoder cannot read leaves the schedule alone, saying so.
        let odd = format!("{fragment}\nDropInPaths=$'x'\n");
        assert!(
            systemd_disagrees(&dir, unit, &odd, &read)
                .unwrap()
                .starts_with("connect cannot decode the drop-in paths")
        );
        // Files changed on disk since the user manager loaded them: what runs
        // is the loaded configuration, so it is left alone; a report that
        // does not say is treated the same.
        for report in [
            format!(
                "NeedDaemonReload=yes\n{}\nDropInPaths=\n",
                &fragment["NeedDaemonReload=no\n".len()..]
            ),
            format!(
                "{}\nDropInPaths=\n",
                &fragment["NeedDaemonReload=no\n".len()..]
            ),
        ] {
            assert!(
                systemd_disagrees(&dir, unit, &report, &read)
                    .unwrap()
                    .contains(
                        "has not loaded pseudolife-update.service's files as they are on disk"
                    ),
                "{report}"
            );
        }
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn each_unit_is_checked_against_the_drop_ins_read_for_it() {
        let dir = scratch();
        let roots = [dir.clone()];
        let environment: Vec<String> = Vec::new();
        let expected = |url: &str| service_body(NAME, &argv(url)?, &environment);
        fs::write(
            dir.join(format!("{NAME}.service")),
            service_body(NAME, &argv(URL).unwrap(), &environment).unwrap(),
        )
        .unwrap();
        fs::write(dir.join(format!("{NAME}.timer")), timer_body(NAME, 3, 7)).unwrap();
        // A timer drop-in with a `[Service]` key is harmless for the timer.
        let timer_dropins = dir.join(format!("{NAME}.timer.d"));
        fs::create_dir(&timer_dropins).unwrap();
        let file = timer_dropins.join("override.conf");
        fs::write(
            &file,
            "\u{feff}[Service]\nExecStart=\nExecStart=/x --daemon-url http://OLD\n",
        )
        .unwrap();
        let report = |listed: String| {
            let dir = dir.clone();
            move |unit: &str| {
                let dropins = if unit.ends_with(".service") {
                    listed.clone()
                } else {
                    String::new()
                };
                Ok(format!(
                    "NeedDaemonReload=no\nFragmentPath={}\nDropInPaths={dropins}\n",
                    dir.join(unit).display()
                ))
            }
        };
        let enabled = || Ok(true);
        // Near miss: systemd applies it to the timer only.
        let timer_only = |unit: &str| {
            let dropins = if unit.ends_with(".timer") {
                show_quote(&file)
            } else {
                String::new()
            };
            Ok(format!(
                "NeedDaemonReload=no\nFragmentPath={}\nDropInPaths={dropins}\n",
                dir.join(unit).display()
            ))
        };
        let found = systemd_installed(&dir, &roots, NAME, false, &enabled, &timer_only, &expected)
            .unwrap()
            .unwrap();
        assert!(
            found.shaped && found.overridden.is_none(),
            "{:?}",
            found.overridden
        );
        // systemd also applies that file to the service (an alias directory
        // linked to the timer's): it was never read as a service drop-in.
        let to_service = report(show_quote(&file));
        let found = systemd_installed(&dir, &roots, NAME, false, &enabled, &to_service, &expected)
            .unwrap()
            .unwrap();
        assert!(!found.shaped);
        assert!(
            found
                .overridden
                .unwrap()
                .starts_with("systemd's user manager applies the drop-in")
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_path_list_is_decoded_as_systemd_quotes_it() {
        let paths = |value: &str| {
            show_paths(value).map(|paths| {
                paths
                    .into_iter()
                    .map(|p| p.display().to_string())
                    .collect::<Vec<_>>()
            })
        };
        assert_eq!(paths(""), Some(vec![]));
        assert_eq!(
            paths("/a/b.conf /c/d.conf"),
            Some(vec!["/a/b.conf".to_owned(), "/c/d.conf".to_owned()])
        );
        assert_eq!(
            paths("\"/home/u x/a.conf\" /c.conf \"/q\\\"\\$\\`\\\\.conf\" \"/t\\tz\\x01.conf\""),
            Some(vec![
                "/home/u x/a.conf".to_owned(),
                "/c.conf".to_owned(),
                "/q\"$`\\.conf".to_owned(),
                "/t\tz\u{1}.conf".to_owned()
            ])
        );
        for unknown in [
            "$'x'",
            "\"/a\\q.conf\"",
            "\"/a\"b",
            "\"/unterminated",
            "/a'b",
            "\"/a\\xff\"",
        ] {
            assert_eq!(paths(unknown), None, "{unknown}");
        }
    }

    #[test]
    fn a_unit_that_is_not_text_is_left_for_schedule_to_replace() {
        let dir = scratch();
        let roots = [dir.clone()];
        let environment: Vec<String> = Vec::new();
        let expected = |url: &str| service_body(NAME, &argv(url)?, &environment);
        let enabled = || Ok(true);
        let show = |unit: &str| {
            Ok(format!(
                "NeedDaemonReload=no\nFragmentPath={}\nDropInPaths=\n",
                dir.join(unit).display()
            ))
        };
        fs::write(dir.join(format!("{NAME}.service")), b"[Service]\n# \xff\n").unwrap();
        let found = systemd_installed(&dir, &roots, NAME, false, &enabled, &show, &expected)
            .unwrap()
            .unwrap();
        assert!(!found.shaped);
        let said = found.overridden.unwrap();
        assert!(said.contains("is not UTF-8 text"), "{said}");
        assert!(
            said.contains(&format!(
                "re-create it with `pseudolife-mcp update --schedule HH:MM --daemon-url {TARGET}`"
            )),
            "{said}"
        );
        assert!(!said.contains("would fail on the same read"), "{said}");
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn discovery_builds_its_search_from_the_host_it_reads() {
        let home = scratch();
        let dir = home.join("config/systemd/user");
        fs::create_dir_all(&dir).unwrap();
        let data = home.join("data").display().to_string();
        let env = move |key: &str| (key == "XDG_DATA_HOME").then(|| data.clone());
        let listed = std::cell::RefCell::new(String::new());
        let run = |argv: &[String]| -> (i32, String) {
            match argv.get(2).map(String::as_str) {
                Some("is-enabled") => (0, String::new()),
                Some("show") => {
                    let unit = argv.last().unwrap();
                    (
                        0,
                        format!(
                            "NeedDaemonReload=no\nFragmentPath={}\nDropInPaths={}\n",
                            dir.join(unit).display(),
                            listed.borrow()
                        ),
                    )
                }
                _ => (1, String::new()),
            }
        };
        let host = Host {
            windows: false,
            linux: true,
            dir: dir.clone(),
            env: &env,
            home: home.clone(),
            run: &run,
        };
        let names = Names {
            task: "unused",
            client_task: "unused",
            unit: NAME,
            client_unit: "pseudolife-client-update-absent",
        };
        let environment = environment_lines(false, false).unwrap();
        let expected = |url: &str| argv(url);
        fs::write(
            dir.join(format!("{NAME}.service")),
            service_body(NAME, &argv(URL).unwrap(), &environment).unwrap(),
        )
        .unwrap();
        fs::write(dir.join(format!("{NAME}.timer")), timer_body(NAME, 3, 7)).unwrap();
        // Near miss: as `--schedule` writes it, with nothing beyond it.
        let found = installed_on(&host, &names, &expected);
        assert_eq!(found.len(), 1);
        assert!(
            found[0].shaped && found[0].overridden.is_none(),
            "{:?} {:?}",
            found[0].why,
            found[0].overridden
        );
        // A drop-in under $XDG_DATA_HOME, which only the host's environment
        // names, is found.
        let data_dropins = home.join("data/systemd/user/pseudolife-update.service.d");
        fs::create_dir_all(&data_dropins).unwrap();
        fs::write(
            data_dropins.join("override.conf"),
            "[Service]\nExecStart=\nExecStart=/x\n",
        )
        .unwrap();
        let found = installed_on(&host, &names, &expected);
        assert!(
            found[0]
                .overridden
                .as_deref()
                .unwrap()
                .contains("override.conf sets ExecStart=")
        );
        fs::remove_dir_all(home.join("data")).unwrap();
        // A drop-in systemd reports that this search did not read.
        *listed.borrow_mut() = "/somewhere/else/pseudolife-update.service.d/x.conf".to_owned();
        let found = installed_on(&host, &names, &expected);
        assert!(
            found[0]
                .overridden
                .as_deref()
                .unwrap()
                .starts_with("systemd's user manager applies the drop-in /somewhere/else/")
        );
        listed.borrow_mut().clear();
        // A timer path that is a directory is shown, with its own fix.
        fs::remove_file(dir.join(format!("{NAME}.timer"))).unwrap();
        fs::create_dir(dir.join(format!("{NAME}.timer"))).unwrap();
        let shown = installed_on(&host, &names, &expected);
        assert_eq!(shown.len(), 1);
        assert!(
            shown[0]
                .overridden
                .as_deref()
                .unwrap()
                .contains("is a directory, not a unit file")
        );
        // On Windows a failed `schtasks /Query` (this fake exits 1) cannot be
        // told from a missing task, so discovery leaves it out.
        let windows = Host {
            windows: true,
            linux: false,
            dir: dir.clone(),
            home: home.clone(),
            ..host
        };
        assert!(installed_on(&windows, &names, &expected).is_empty());
        let other = Host {
            linux: false,
            ..host
        };
        assert!(installed_on(&other, &names, &expected).is_empty());
        fs::remove_dir_all(&home).unwrap();
    }

    #[test]
    fn this_machine_is_read_as_the_platform_it_is() {
        let here = Host::here();
        assert_eq!(
            (here.windows, here.linux),
            (cfg!(windows), cfg!(target_os = "linux"))
        );
        assert_eq!(here.dir, systemd_dir());
    }

    // Linux path semantics: absolute XDG entries begin with '/'.
    #[cfg(unix)]
    #[test]
    fn the_drop_in_roots_follow_the_user_unit_search_path() {
        let home = Path::new("/home/u");
        let dir = Path::new("/home/u/.config/systemd/user");
        let none = |_: &str| None::<String>;
        let roots = systemd_roots(dir, &none, home);
        let want = |paths: &[&str]| paths.iter().map(PathBuf::from).collect::<Vec<_>>();
        assert_eq!(
            roots,
            want(&[
                "/home/u/.config/systemd/user.control",
                "/home/u/.config/systemd/user",
                "/etc/systemd/user",
                "/run/systemd/user",
                "/home/u/.local/share/systemd/user",
                "/usr/local/share/systemd/user",
                "/usr/share/systemd/user",
                "/usr/local/lib/systemd/user",
                "/usr/lib/systemd/user",
            ])
        );
        let set = |key: &str| match key {
            "XDG_RUNTIME_DIR" => Some("/run/user/1000".to_owned()),
            "XDG_CONFIG_DIRS" => Some("/opt/a:relative:/opt/b".to_owned()),
            "XDG_DATA_HOME" => Some("/data".to_owned()),
            "XDG_DATA_DIRS" => Some("/opt/d:/usr/share".to_owned()),
            _ => None,
        };
        assert_eq!(
            systemd_roots(dir, &set, home),
            want(&[
                "/home/u/.config/systemd/user.control",
                "/run/user/1000/systemd/user.control",
                "/run/user/1000/systemd/transient",
                "/run/user/1000/systemd/generator.early",
                "/home/u/.config/systemd/user",
                "/opt/a/systemd/user",
                "/opt/b/systemd/user",
                "/etc/systemd/user",
                "/run/user/1000/systemd/user",
                "/run/systemd/user",
                "/run/user/1000/systemd/generator",
                "/data/systemd/user",
                "/opt/d/systemd/user",
                "/usr/share/systemd/user",
                "/usr/local/lib/systemd/user",
                "/usr/local/share/systemd/user",
                "/usr/lib/systemd/user",
                "/run/user/1000/systemd/generator.late",
            ])
        );
    }

    #[test]
    fn a_unit_read_back_carries_its_drop_in_and_reports_a_read_failure() {
        let dir = scratch();
        let roots = [dir.clone()];
        let environment: Vec<String> = Vec::new();
        let expected = |url: &str| service_body(NAME, &argv(url)?, &environment);
        let enabled = || Ok(true);
        let show = |unit: &str| {
            Ok(format!(
                "NeedDaemonReload=no\nFragmentPath={}\nDropInPaths=\n",
                dir.join(unit).display()
            ))
        };
        let read = || systemd_installed(&dir, &roots, NAME, false, &enabled, &show, &expected);
        // Not installed: no unit file.
        assert!(matches!(read(), Ok(None)));
        fs::write(
            dir.join(format!("{NAME}.service")),
            service_body(NAME, &argv(URL).unwrap(), &environment).unwrap(),
        )
        .unwrap();
        fs::write(dir.join(format!("{NAME}.timer")), timer_body(NAME, 3, 7)).unwrap();
        // Near miss: the files as `--schedule` writes them, no drop-in.
        let found = read().unwrap().unwrap();
        assert!(
            found.shaped && found.overridden.is_none(),
            "{:?}",
            found.why
        );
        assert_eq!(found.url.as_deref(), Some(URL));
        // The drop-in under a root is carried into what is read back.
        fs::create_dir(dir.join(format!("{NAME}.service.d"))).unwrap();
        fs::write(
            dir.join(format!("{NAME}.service.d/override.conf")),
            "[Service]\nExecStart=\nExecStart=/x\n",
        )
        .unwrap();
        let found = read().unwrap().unwrap();
        assert!(!found.shaped);
        assert!(
            found
                .overridden
                .unwrap()
                .contains("override.conf sets ExecStart=, which takes precedence")
        );
        // A timer path that is a directory: neither permissions nor `rm` fix
        // it, so the row says to remove the directory and re-create the
        // schedule against the target URL.
        fs::remove_dir_all(dir.join(format!("{NAME}.service.d"))).unwrap();
        fs::remove_file(dir.join(format!("{NAME}.timer"))).unwrap();
        fs::create_dir(dir.join(format!("{NAME}.timer"))).unwrap();
        let found = read().unwrap().unwrap();
        assert!(!found.shaped);
        let said = found.overridden.unwrap();
        assert!(
            said.contains(&format!("{NAME}.timer is a directory, not a unit file")),
            "{said}"
        );
        assert!(said.contains("remove that directory (`rm -r '"), "{said}");
        assert!(said.contains(&format!("--daemon-url {TARGET}")), "{said}");
        assert!(!said.contains("permissions"), "{said}");
        // A unit or timer that exists but cannot be read stays visible to
        // discovery on Linux with that cause and the real fix (`--schedule`
        // would fail on the same read); on Windows a failed read is left out.
        let unreadable = || {
            Err::<Option<Installed>, _>(format!("{NAME}.timer cannot be read (permission denied)"))
        };
        let shown = discovered(unreadable(), true, &dir, NAME, false).expect("visible on Linux");
        assert!(!shown.shaped);
        let said = shown.overridden.unwrap();
        assert!(
            said.starts_with(&format!("{NAME}.timer cannot be read")),
            "{said}"
        );
        assert!(said.contains("repair that file's permissions"), "{said}");
        assert!(!said.contains("update --schedule HH:MM"), "{said}");
        assert!(
            shown
                .remove
                .starts_with("systemctl --user disable --now pseudolife-update.timer")
        );
        assert!(discovered(unreadable(), false, &dir, NAME, false).is_none());
        // Near miss: a schedule that is simply not installed is no row.
        assert!(discovered(Ok(None), true, &dir, NAME, false).is_none());
        fs::remove_dir(dir.join(format!("{NAME}.timer"))).unwrap();
        fs::write(dir.join(format!("{NAME}.timer")), timer_body(NAME, 3, 7)).unwrap();
        // A `systemctl show` that failed leaves the schedule alone.
        let failed = |_: &str| Err("`systemctl --user show` exited 1".to_owned());
        let found = systemd_installed(&dir, &roots, NAME, false, &enabled, &failed, &expected)
            .unwrap()
            .unwrap();
        assert!(!found.shaped);
        assert!(
            found
                .overridden
                .unwrap()
                .starts_with("connect could not ask systemd's user manager")
        );
        // An enabled-check that was interrupted is a read failure.
        let interrupted = || Err("interrupted".to_owned());
        assert_eq!(
            systemd_installed(&dir, &roots, NAME, false, &interrupted, &show, &expected)
                .err()
                .as_deref(),
            Some("interrupted")
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_time_must_be_a_clock_reading() {
        assert_eq!(clock("03:07:00").as_deref(), Some("03:07"));
        for text in ["3:07", "24:00", "03:60", "0a:07", "03-07", ""] {
            assert_eq!(clock(text), None, "{text}");
        }
    }
}

/// A real scheduler under a name unique to the run: registered as
/// `--schedule` writes it, read back, re-created against a new URL keeping
/// its time, then removed and checked gone. Run only on a dev machine.
#[cfg(all(test, any(windows, target_os = "linux")))]
mod real_tests {
    use super::*;

    #[test]
    #[ignore = "creates a real scheduled task or systemd user timer under a unique name; dev machines only"]
    fn a_real_schedule_reads_back_and_is_re_created_against_a_new_url() {
        let unique = format!("test-{}", uuid::Uuid::new_v4().simple());
        let task = format!("Pseudolife Client Update {unique}");
        let unit = format!("{CLIENT_UNIT}-{unique}");
        let names = Names {
            task: "unused",
            client_task: &task,
            unit: "unused",
            client_unit: &unit,
        };
        let binary = std::env::current_exe()
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned();
        let argv = |url: &str| -> Result<Vec<String>> {
            Ok(vec![
                binary.clone(),
                "update".into(),
                "--unattended".into(),
                "--daemon-url".into(),
                url.into(),
            ])
        };
        let remove = || {
            if cfg!(windows) {
                seams::run_cli(
                    &[
                        "schtasks".into(),
                        "/Delete".into(),
                        "/F".into(),
                        "/TN".into(),
                        task.clone(),
                    ],
                    None,
                    30,
                    false,
                );
            } else {
                seams::run_cli(
                    &[
                        "systemctl".into(),
                        "--user".into(),
                        "disable".into(),
                        "--now".into(),
                        format!("{unit}.timer"),
                    ],
                    None,
                    30,
                    false,
                );
                for suffix in ["service", "timer"] {
                    let _ = fs::remove_file(systemd_dir().join(format!("{unit}.{suffix}")));
                }
                seams::run_cli(
                    &["systemctl".into(), "--user".into(), "daemon-reload".into()],
                    None,
                    30,
                    false,
                );
            }
        };
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut update = options("http://127.0.0.1:18765");
            write(
                &mut update,
                &names,
                true,
                4,
                9,
                &argv("http://127.0.0.1:18765").unwrap(),
            )
            .unwrap();
            let found: Vec<_> = installed_with(&names, &argv)
                .into_iter()
                .filter(|i| i.client)
                .collect();
            assert_eq!(found.len(), 1);
            assert!(found[0].shaped, "{} {:?}", found[0].place, found[0].why);
            assert_eq!(found[0].url.as_deref(), Some("http://127.0.0.1:18765"));
            assert_eq!(found[0].time.as_deref(), Some("04:09"));
            assert!(found[0].differs.is_none() && found[0].overridden.is_none());
            if cfg!(windows) {
                // The shape the real export has, for the record (its user id
                // is the caller's to redact).
                let (code, xml) = seams::run_cli(
                    &[
                        "schtasks".into(),
                        "/Query".into(),
                        "/TN".into(),
                        task.clone(),
                        "/XML".into(),
                    ],
                    None,
                    30,
                    false,
                );
                eprintln!("schtasks /Query /XML exit {code}:\n{xml}");
            }
            // Re-created against the new URL at the time read back, through
            // `reschedule` (`--schedule`'s own path) where this machine's kind
            // is the client one: a client run needs the daemon's /health, so a
            // listener here answers it.
            let health = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let port = health.local_addr().unwrap().port();
            std::thread::spawn(move || {
                use std::io::{Read, Write};
                for mut stream in health.incoming().flatten() {
                    let mut request = [0u8; 2048];
                    let _ = stream.read(&mut request);
                    let body = "{\"status\":\"ok\"}";
                    let _ = write!(
                        stream,
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                }
            });
            let new_url = format!("http://127.0.0.1:{port}");
            let time = found[0].time.clone().unwrap();
            // `reschedule` writes the verified launcher: run this test with
            // PSEUDOLIFE_SHIM_LAUNCHER (this test binary) and PSEUDOLIFE_SHIM_RUNTIMES
            // set, or it reports why and re-creates through `write`.
            let refused = reschedule(&names, true, &time, &new_url, &|| {
                Err("the re-read refused".to_owned())
            });
            let ran = match refused {
                Err(Stop::Changed(cause)) => {
                    assert_eq!(cause, "the re-read refused");
                    // Refused immediately before the write: nothing written.
                    let still: Vec<_> = installed_with(&names, &argv)
                        .into_iter()
                        .filter(|i| i.client)
                        .collect();
                    assert_eq!(still[0].url.as_deref(), Some("http://127.0.0.1:18765"));
                    let check = || match installed_kind_with(&names, true, &argv) {
                        Ok(Some(now))
                            if now.shaped
                                && now.url.as_deref() == Some("http://127.0.0.1:18765") =>
                        {
                            Ok(())
                        }
                        other => Err(format!("changed: {:?}", other.map(|o| o.map(|i| i.url)))),
                    };
                    reschedule(&names, true, &time, &new_url, &check)
                        .map_err(|stop| match stop {
                            Stop::Changed(cause) | Stop::Failed(cause) => cause,
                        })
                        .unwrap();
                    true
                }
                Err(Stop::Failed(reason)) => {
                    eprintln!("reschedule not run here ({reason}); re-created through write");
                    false
                }
                Ok(_) => panic!("a refusing check let the write happen"),
            };
            if !ran {
                let (hour, minute) = at(&time).unwrap();
                write(
                    &mut update,
                    &names,
                    true,
                    hour,
                    minute,
                    &argv(&new_url).unwrap(),
                )
                .unwrap();
            }
            let again: Vec<_> = installed_with(&names, &argv)
                .into_iter()
                .filter(|i| i.client)
                .collect();
            assert_eq!(again.len(), 1);
            assert!(again[0].shaped);
            assert_eq!(again[0].url.as_deref(), Some(new_url.as_str()));
            assert_eq!(again[0].time.as_deref(), Some("04:09"));
        }));
        remove();
        let gone = installed_with(&names, &argv).into_iter().all(|i| !i.client);
        assert!(
            gone,
            "the test schedule {task} / {unit} was not removed; remove it by hand"
        );
        if let Err(panic) = result {
            std::panic::resume_unwind(panic);
        }
    }
}
