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
    /// A systemd drop-in that sets a key the run depends on, or one connect
    /// cannot read or parse, as a clause ("the systemd drop-in X sets
    /// ExecStart"). The effective schedule is then not the base files, so
    /// it is never current and never re-created.
    pub overridden: Option<String>,
    /// A task setting `--schedule` never writes, named ("an end date"):
    /// re-registering the task would drop it.
    pub differs: Option<String>,
    /// The one command that removes exactly this schedule.
    pub remove: String,
}

/// The daemon and client schedules installed on this machine. One that
/// cannot be read is left out, as it always was.
pub(crate) fn installed(names: &Names) -> Vec<Installed> {
    installed_with(names, &|url: &str| command(url))
}

fn installed_with(names: &Names, expected: &dyn Fn(&str) -> Result<Vec<String>>) -> Vec<Installed> {
    [false, true]
        .into_iter()
        .filter_map(|client| installed_kind_with(names, client, expected).ok().flatten())
        .collect()
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
    if cfg!(windows) {
        let task = if client {
            names.client_task
        } else {
            names.task
        };
        let (code, xml) = seams::run_cli(
            &[
                "schtasks".into(),
                "/Query".into(),
                "/TN".into(),
                task.into(),
                "/XML".into(),
            ],
            None,
            30,
            false,
        );
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
    } else if cfg!(target_os = "linux") {
        let name = if client {
            names.client_unit
        } else {
            names.unit
        };
        let dir = systemd_dir();
        let roots = systemd_roots(&dir, &|key| std::env::var(key).ok(), &seams::home());
        systemd_installed(
            &dir,
            &roots,
            name,
            client,
            &|| {
                let code = seams::run_cli(
                    &[
                        "systemctl".into(),
                        "--user".into(),
                        "is-enabled".into(),
                        "--quiet".into(),
                        format!("{name}.timer"),
                    ],
                    None,
                    30,
                    false,
                )
                .0;
                if crate::cli::update::schedules::cancelled() {
                    return Err("interrupted".to_owned());
                }
                Ok(code == 0)
            },
            &|url| service_body(name, &expected(url)?, &environment_lines(client, false)?),
        )
    } else {
        Ok(None)
    }
}

/// A systemd unit `name` in `dir`, its timer, whether the timer is enabled
/// and the drop-ins under `roots` that apply to either, read back.
fn systemd_installed(
    dir: &Path,
    roots: &[PathBuf],
    name: &str,
    client: bool,
    enabled: &dyn Fn() -> std::result::Result<bool, String>,
    expected: &dyn Fn(&str) -> Result<String>,
) -> std::result::Result<Option<Installed>, String> {
    let unit = dir.join(format!("{name}.service"));
    let service = match fs::read_to_string(&unit) {
        Ok(service) => service,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("{} cannot be read ({error})", unit.display())),
    };
    let timer = match fs::read_to_string(unit.with_extension("timer")) {
        Ok(timer) => timer,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(error) => {
            return Err(format!(
                "{} cannot be read ({error})",
                unit.with_extension("timer").display()
            ));
        }
    };
    let enabled = enabled()?;
    let overridden = systemd_override(roots, name);
    Ok(Some(systemd_parse(
        dir, name, client, &service, &timer, enabled, overridden, expected,
    )))
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
    roots.extend(set("XDG_CONFIG_DIRS").as_deref().map(list).unwrap_or_default());
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
/// In the service, what the run executes and with which environment: the
/// command names the daemon URL, and the environment carries the bearer
/// file and layout variables the base unit's shape check compares. In the
/// timer, any key: every `[Timer]` key decides when it runs or which unit
/// it starts, and `connect` reports and keeps that daily time. Any other
/// key (`Type=`, `TimeoutStopFailureMode=`, `[Unit]`, `[Install]`) changes
/// neither, and survives a re-creation, which rewrites only the base files.
fn blocking_key(kind: &str, section: &str, key: &str) -> bool {
    match (kind, section) {
        ("service", "Service") => matches!(
            key,
            "ExecStart"
                | "ExecStartPre"
                | "ExecStartPost"
                | "Environment"
                | "EnvironmentFile"
                | "UnsetEnvironment"
                | "PassEnvironment"
        ),
        ("timer", "Timer") => true,
        _ => false,
    }
}

/// The first blocking key `text`, a drop-in for a unit of `kind`, sets, or
/// the 1-based line systemd's syntax cannot read.
fn dropin_sets(kind: &str, text: &str) -> std::result::Result<Option<String>, usize> {
    let mut section = String::new();
    let mut lines = text.lines().enumerate();
    while let Some((index, raw)) = lines.next() {
        let mut line = raw.trim().to_owned();
        // A trailing backslash continues the line.
        while line.ends_with('\\') {
            line.pop();
            match lines.next() {
                Some((_, next)) => {
                    line.push(' ');
                    line.push_str(next.trim());
                }
                None => break,
            }
        }
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if let Some(name) = line
            .strip_prefix('[')
            .and_then(|rest| rest.strip_suffix(']'))
        {
            section = name.to_owned();
            continue;
        }
        let Some((key, _)) = line.split_once('=') else {
            return Err(index + 1);
        };
        let key = key.trim();
        if blocking_key(kind, &section, key) {
            return Ok(Some(format!("{key}=")));
        }
    }
    Ok(None)
}

/// A drop-in for the unit or its timer that sets a blocking key, or that
/// connect cannot read or parse, as a clause. It looks at every `.conf` in
/// the unit's own `<name>.<kind>.d`, its dash-prefix ones
/// (`pseudolife-.service.d`) and the top-level `<kind>.d`, under each of
/// `roots`; of files sharing a name in one such directory, only the one in
/// the highest-priority root applies, as systemd reads them.
fn systemd_override(roots: &[PathBuf], name: &str) -> Option<String> {
    let mut stems: Vec<String> = name
        .match_indices('-')
        .map(|(at, _)| name[..=at].to_owned())
        .collect();
    stems.push(name.to_owned());
    for kind in ["service", "timer"] {
        let directories = stems
            .iter()
            .rev()
            .map(|stem| format!("{stem}.{kind}.d"))
            .chain([format!("{kind}.d")]);
        for directory in directories {
            let mut seen = std::collections::BTreeSet::new();
            for root in roots {
                let path = root.join(&directory);
                let entries = match fs::read_dir(&path) {
                    Ok(entries) => entries,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                    Err(error) => {
                        return Some(format!(
                            "connect cannot read the systemd drop-in directory {} ({error})",
                            path.display()
                        ));
                    }
                };
                let mut files: Vec<_> = entries
                    .flatten()
                    .map(|entry| entry.path())
                    .filter(|file| file.extension().is_some_and(|ext| ext == "conf"))
                    .collect();
                files.sort();
                for file in files {
                    if !seen.insert(file.file_name().map(|n| n.to_owned())) {
                        continue;
                    }
                    let text = match fs::read_to_string(&file) {
                        Ok(text) => text,
                        Err(error) => {
                            return Some(format!(
                                "connect cannot read the systemd drop-in {} ({error})",
                                file.display()
                            ));
                        }
                    };
                    match dropin_sets(kind, &text) {
                        Ok(None) => {}
                        Ok(Some(key)) => {
                            return Some(format!(
                                "the systemd drop-in {} sets {key}",
                                file.display()
                            ));
                        }
                        Err(line) => {
                            return Some(format!(
                                "connect cannot parse the systemd drop-in {} (line {line})",
                                file.display()
                            ));
                        }
                    }
                }
            }
        }
    }
    None
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
    same_kind(&update, client).map_err(|refusal| Stop::Failed(refusal.reason))?;
    let changed = std::cell::RefCell::new(None);
    let gate = || -> Result<()> {
        check().map_err(|cause| {
            let error = UpdateError::usage(cause.clone());
            *changed.borrow_mut() = Some(cause);
            error
        })
    };
    match schedule_checked(&mut update, when, names, &gate) {
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
            "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\n<Task version=\"1.3\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\n  <RegistrationInfo>\n    <URI>\\T</URI>\n  </RegistrationInfo>\n  <Principals>\n    <Principal id=\"Author\">\n      <UserId>S-1-5-21-0-0-0-1001</UserId>\n      <LogonType>InteractiveToken</LogonType>\n    </Principal>\n  </Principals>\n  <Settings>\n    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>\n    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>\n    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n    <IdleSettings>\n      <Duration>PT10M</Duration>\n      <WaitTimeout>PT1H</WaitTimeout>\n      <StopOnIdleEnd>true</StopOnIdleEnd>\n      <RestartOnIdle>false</RestartOnIdle>\n    </IdleSettings>\n    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>\n  </Settings>\n  <Triggers>\n    <CalendarTrigger>\n      <StartBoundary>2026-11-14T03:07:00+11:00</StartBoundary>\n      <ScheduleByDay>\n        <DaysInterval>1</DaysInterval>\n      </ScheduleByDay>\n    </CalendarTrigger>\n  </Triggers>\n  <Actions Context=\"Author\">\n    <Exec>\n      <Command>{}</Command>\n      <Arguments>{}</Arguments>\n    </Exec>\n  </Actions>\n</Task>\n",
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
        // A line systemd's syntax cannot read blocks, naming the line.
        let file = put(
            "pseudolife-update.service.d",
            "50-test.conf",
            "[Service]\nnot a key\n",
        );
        assert_eq!(
            systemd_override(&roots, name),
            Some(format!(
                "connect cannot parse the systemd drop-in {} (line 2)",
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
    fn a_drop_in_in_a_higher_priority_root_masks_one_of_the_same_name() {
        let (high, low) = (scratch(), scratch());
        let roots = [high.clone(), low.clone()];
        for root in [&high, &low] {
            fs::create_dir_all(root.join("pseudolife-update.service.d")).unwrap();
        }
        let low_file = low.join("pseudolife-update.service.d/override.conf");
        fs::write(&low_file, "[Service]\nExecStart=/x\n").unwrap();
        assert!(systemd_override(&roots, "pseudolife-update").is_some());
        // An empty file of the same name above it (as a /dev/null mask
        // reads) masks it.
        fs::write(high.join("pseudolife-update.service.d/override.conf"), "").unwrap();
        assert_eq!(systemd_override(&roots, "pseudolife-update"), None);
        fs::remove_dir_all(&high).unwrap();
        fs::remove_dir_all(&low).unwrap();
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
        let read = || systemd_installed(&dir, &roots, NAME, false, &enabled, &expected);
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
                .ends_with("override.conf sets ExecStart=")
        );
        // An enabled-check that was interrupted is a read failure.
        let interrupted = || Err("interrupted".to_owned());
        assert_eq!(
            systemd_installed(&dir, &roots, NAME, false, &interrupted, &expected)
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
