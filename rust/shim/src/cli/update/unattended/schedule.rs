use super::*;

const TASK: &str = "Pseudolife Unattended Update";
const CLIENT_TASK: &str = "Pseudolife Client Update";
const UNIT: &str = "pseudolife-update";
const CLIENT_UNIT: &str = "pseudolife-client-update";

/// The scheduled task and systemd unit names one schedule writes and reads:
/// the real ones, or a test's unique ones.
#[derive(Clone, Copy)]
pub(crate) struct Names<'a> {
    pub task: &'a str,
    pub client_task: &'a str,
    pub unit: &'a str,
    pub client_unit: &'a str,
}

pub(crate) const NAMES: Names<'static> = Names {
    task: TASK,
    client_task: CLIENT_TASK,
    unit: UNIT,
    client_unit: CLIENT_UNIT,
};

#[path = "reschedule.rs"]
mod reschedule;
pub(crate) use reschedule::{
    Blocker, Installed, Stop, TARGET, installed, installed_kind, preflight, reschedule,
};

const LAYOUT: &[&str] = &[
    "PSEUDOLIFE_SHIM_RUNTIMES",
    "PSEUDOLIFE_SHIM_LAUNCHER",
    "XDG_DATA_HOME",
    "PSEUDOLIFE_MCP_DATA_DIR",
];

fn at(when: &str) -> Result<(u8, u8)> {
    let when = when.trim();
    let value = when
        .split_once(':')
        .filter(|(h, m)| {
            (1..=2).contains(&h.len())
                && m.len() == 2
                && h.bytes().chain(m.bytes()).all(|c| c.is_ascii_digit())
        })
        .and_then(|(h, m)| Some((h.parse::<u8>().ok()?, m.parse::<u8>().ok()?)))
        .filter(|(h, m)| *h < 24 && *m < 60);
    value.ok_or_else(|| UpdateError::usage("--schedule takes a time of day as HH:MM"))
}

fn checked(text: &str) -> Result<&str> {
    if text.chars().any(char::is_control) {
        Err(UpdateError::usage(
            "a scheduled path or environment value contains a control character; nothing was installed",
        ))
    } else {
        Ok(text)
    }
}

/// systemd splits its own argv, expands specifiers and expands standalone $.
fn unit_quote(text: &str, command: bool) -> Result<String> {
    let mut out = String::from("\"");
    for c in checked(text)?.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '%' => out.push_str("%%"),
            '$' if command => out.push_str("$$"),
            _ => out.push(c),
        }
    }
    out.push('"');
    Ok(out)
}

/// CommandLineToArgvW/CRT quoting, including quotes and trailing backslashes.
fn windows_quote(text: &str) -> Result<String> {
    checked(text)?;
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in text.chars() {
        if c == '\\' {
            slashes += 1;
            continue;
        }
        if c == '"' {
            out.push_str(&"\\".repeat(slashes * 2 + 1));
            out.push('"');
        } else {
            out.push_str(&"\\".repeat(slashes));
            out.push(c);
        }
        slashes = 0;
    }
    out.push_str(&"\\".repeat(slashes * 2));
    out.push('"');
    Ok(out)
}

fn command(url: &str) -> Result<Vec<String>> {
    let binary = crate::lifecycle::native_update_binary().map_err(|_| {
        UpdateError::usage("the invoking native binary could not be located; nothing was installed")
    })?;
    let binary = binary.to_str().ok_or_else(|| {
        UpdateError::usage("the native launcher path must be UTF-8; nothing was installed")
    })?;
    let url = crate::daemon_url::validate(url).map_err(UpdateError::usage)?;
    Ok(vec![
        checked(binary)?.into(),
        "update".into(),
        "--unattended".into(),
        "--daemon-url".into(),
        url,
    ])
}

fn systemd_dir() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| seams::home().join(".config"))
        .join("systemd/user")
}

fn user_environment(name: &str) -> Option<String> {
    #[cfg(windows)]
    {
        // stdout stays internal. In particular, the user's bearer is never a
        // diagnostic, command argument, unit value or inherited-shell guess.
        let script = format!(
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); [Environment]::GetEnvironmentVariable('{name}','User') | ConvertTo-Json -Compress"
        );
        let shell = std::env::var_os("SystemRoot")
            .map(PathBuf::from)?
            .join("System32/WindowsPowerShell/v1.0/powershell.exe");
        let (code, out) = seams::run_cli(
            &[
                shell.to_str()?.into(),
                "-NoLogo".into(),
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
                script,
            ],
            None,
            20,
            false,
        );
        if code != 0 {
            return None;
        }
        serde_json::from_str::<Option<String>>(out.trim_start_matches('\u{feff}').trim())
            .ok()
            .flatten()
            .filter(|s| !s.is_empty())
    }
    #[cfg(not(windows))]
    {
        let _ = name;
        None
    }
}

fn credential(file: Option<String>, token: Option<String>) -> bool {
    CredentialProvider::new(
        if file.is_some() { None } else { token },
        file.map(PathBuf::from),
    )
    .and_then(|provider| provider.snapshot())
    .is_ok_and(|snapshot| snapshot.token().is_some_and(|s| !s.is_empty()))
}

fn no_bearer(update: &mut Update, windows: bool) -> Result<()> {
    let text = no_bearer_text(windows);
    if update.o.allow_no_bearer {
        update.warn(format!("{text}. Installed anyway (--allow-no-bearer)"));
        Ok(())
    } else {
        Err(UpdateError::usage(format!(
            "{text}; or pass --allow-no-bearer to install it anyway. Nothing was installed"
        )))
    }
}

/// systemd's `ExecStart=` value for `argv`.
fn exec_line(argv: &[String]) -> Result<String> {
    Ok(argv
        .iter()
        .map(|s| unit_quote(s, true))
        .collect::<Result<Vec<_>>>()?
        .join(" "))
}

fn no_bearer_text(windows: bool) -> String {
    let fix = if windows {
        "Set PSEUDOLIFE_MCP_TOKEN_FILE once in your Windows User-scope environment with [Environment]::SetEnvironmentVariable('PSEUDOLIFE_MCP_TOKEN_FILE','<private token file>','User'), then run --schedule again"
    } else {
        "Run --schedule again from a shell with PSEUDOLIFE_MCP_TOKEN_FILE=<private token file>"
    };
    format!(
        "no bearer resolves for the scheduled run{}: the scheduled run cannot read the board, so it would hold off every day (exit 4, which the scheduler counts as success) and never apply a release. {fix}",
        if windows {
            " in your User-scope environment, which the task inherits"
        } else {
            " in this shell, whose token file the unit carries"
        }
    )
}

fn environment(client: bool) -> Result<Vec<String>> {
    environment_lines(client, true)
}

/// The unit's `Environment=` lines; with `write` false, the lines alone,
/// nothing written (the token file is named where `--schedule` would put it).
fn environment_lines(client: bool, write: bool) -> Result<Vec<String>> {
    let mut lines = Vec::new();
    // Both jobs need the same selected runtime and data directory on next run.
    for key in LAYOUT {
        if let Ok(value) = std::env::var(key) {
            lines.push(format!(
                "Environment={}",
                unit_quote(&format!("{key}={value}"), false)?
            ));
        }
    }
    if client {
        return Ok(lines);
    }
    let mut token_file = std::env::var("PSEUDOLIFE_MCP_TOKEN_FILE")
        .ok()
        .filter(|s| !s.is_empty());
    if token_file.is_none()
        && let Some(token) = bearer()
    {
        let path = seams::data_dir().join("unattended-update.token");
        if write {
            seams::write_atomic(&path, token.as_bytes()).map_err(|_| {
                UpdateError::usage("could not write the private token file; nothing was installed")
            })?;
            let provider = CredentialProvider::new(None, Some(path.clone()))
                .and_then(|p| p.snapshot())
                .map_err(|_| {
                    UpdateError::usage(
                        "could not validate the private token file; nothing was installed",
                    )
                })?;
            if provider.token() != Some(token.as_str()) {
                return Err(UpdateError::usage(
                    "the private token file could not be verified; nothing was installed",
                ));
            }
        }
        token_file = Some(
            path.to_str()
                .ok_or_else(|| UpdateError::usage("the token file path must be UTF-8"))?
                .into(),
        );
    }
    if let Some(file) = token_file {
        lines.push(format!(
            "Environment={}",
            unit_quote(&format!("PSEUDOLIFE_MCP_TOKEN_FILE={file}"), false)?
        ));
    }
    Ok(lines)
}

fn atomic_unit(path: &Path, bytes: &[u8]) -> Result<()> {
    if fs::symlink_metadata(path).is_ok_and(|m| !m.is_file() || m.file_type().is_symlink()) {
        return Err(UpdateError::usage(
            "the scheduled unit path is not a regular file; nothing was installed",
        ));
    }
    let parent = path.parent().unwrap();
    fs::create_dir_all(parent)?;
    let temp = parent.join(format!(
        ".pseudolife-unit-{}",
        uuid::Uuid::new_v4().simple()
    ));
    let result = (|| {
        seams::write_atomic(&temp, bytes)?;
        fs::rename(&temp, path)?;
        Ok(())
    })();
    let _ = fs::remove_file(temp);
    result
}

fn unit_label(name: &str) -> &'static str {
    if name.starts_with(CLIENT_UNIT) {
        "client"
    } else {
        "daemon"
    }
}

/// The `.service` file `--schedule` writes for `argv` with `environment`.
fn service_body(name: &str, argv: &[String], environment: &[String]) -> Result<String> {
    let exec = exec_line(argv)?;
    let label = unit_label(name);
    Ok(format!(
        "[Unit]\nDescription=Pseudolife-MCP unattended {label} update\n\n[Service]\nType=oneshot\n{}\nExecStart={exec}\nSuccessExitStatus=3 4\n",
        environment.join("\n")
    ))
}

/// The `.timer` file `--schedule` writes for a daily run at `hour:minute`.
fn timer_body(name: &str, hour: u8, minute: u8) -> String {
    let label = unit_label(name);
    format!(
        "[Unit]\nDescription=Pseudolife-MCP unattended {label} update (daily)\n\n[Timer]\nOnCalendar=*-*-* {hour:02}:{minute:02}:00\nPersistent=true\nRandomizedDelaySec=300\n\n[Install]\nWantedBy=timers.target\n"
    )
}

fn install_systemd(
    unit_dir: &Path,
    name: &str,
    hour: u8,
    minute: u8,
    argv: &[String],
    environment: &[String],
    mut run: impl FnMut(&[String]) -> (i32, String),
) -> Result<()> {
    let service = unit_dir.join(format!("{name}.service"));
    let timer = unit_dir.join(format!("{name}.timer"));
    let previous = |path: &Path| -> Result<Option<Vec<u8>>> {
        match fs::read(path) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    };
    let old_service = previous(&service)?;
    let old_timer = previous(&timer)?;
    let body = service_body(name, argv, environment)?;
    let timer_body = timer_body(name, hour, minute);
    let result = (|| {
        atomic_unit(&service, body.as_bytes())?;
        atomic_unit(&timer, timer_body.as_bytes())?;
        for command in [
            vec!["systemctl".into(), "--user".into(), "daemon-reload".into()],
            vec![
                "systemctl".into(),
                "--user".into(),
                "enable".into(),
                "--now".into(),
                format!("{name}.timer"),
            ],
        ] {
            if run(&command).0 != 0 {
                return Err(UpdateError::usage(
                    "systemctl --user could not install the unattended timer; check your user systemd session and run --schedule again",
                ));
            }
        }
        Ok(())
    })();
    if result.is_err() {
        if old_timer.is_none() {
            run(&[
                "systemctl".into(),
                "--user".into(),
                "disable".into(),
                "--now".into(),
                format!("{name}.timer"),
            ]);
        }
        let restore = |path: &Path, previous: &Option<Vec<u8>>| -> Result<()> {
            if let Some(bytes) = previous {
                atomic_unit(path, bytes)
            } else {
                match fs::remove_file(path) {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(e.into()),
                }
            }
        };
        if restore(&service, &old_service).is_err() || restore(&timer, &old_timer).is_err() {
            return Err(UpdateError::usage(
                "timer installation failed and its previous files could not be restored; inspect the user systemd unit files before running --schedule again",
            ));
        }
        run(&["systemctl".into(), "--user".into(), "daemon-reload".into()]);
    }
    result
}

fn powershell_literal(text: &str) -> Result<String> {
    Ok(format!("'{}'", checked(text)?.replace('\'', "''")))
}

fn windows_powershell() -> Result<String> {
    std::env::var_os("SystemRoot")
        .map(PathBuf::from)
        .ok_or_else(|| UpdateError::usage("SystemRoot is missing; cannot locate Windows PowerShell for the scheduled environment"))?
        .join("System32/WindowsPowerShell/v1.0/powershell.exe")
        .to_str()
        .map(str::to_owned)
        .ok_or_else(|| UpdateError::usage("the scheduler executable path must be UTF-8"))
}

fn schedule_windows(argv: &[String]) -> Result<(String, String)> {
    // Task Scheduler inherits the User environment, never the calling shell.
    // Carry only layout overrides in an encoded, noninteractive wrapper. No
    // bearer goes in task arguments, even when it exists only in this shell.
    let layout: Vec<_> = LAYOUT
        .iter()
        .filter_map(|key| std::env::var(key).ok().map(|v| (*key, v)))
        .collect();
    if layout.is_empty() {
        let executable = argv
            .first()
            .ok_or_else(|| UpdateError::usage("scheduled command is empty"))?;
        checked(executable)?;
        return argv[1..]
            .iter()
            .map(|s| windows_quote(s))
            .collect::<Result<Vec<_>>>()
            .map(|v| (executable.clone(), v.join(" ")));
    }
    let mut script = String::new();
    for (key, value) in layout {
        script.push_str(&format!("$env:{key}={};", powershell_literal(&value)?));
    }
    script.push_str("& ");
    script.push_str(
        &argv
            .iter()
            .map(|s| powershell_literal(s))
            .collect::<Result<Vec<_>>>()?
            .join(" "),
    );
    script.push_str(";exit $LASTEXITCODE");
    let bytes: Vec<_> = script.encode_utf16().flat_map(u16::to_le_bytes).collect();
    // The repository already uses a small base64 encoder for process surfaces;
    // keeping this local avoids adding a dependency for one task argument.
    let encoded = base64(&bytes);
    Ok((
        windows_powershell()?,
        format!("-NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"),
    ))
}

/// The extractor-shim autostart script, which holds the one copy in this
/// crate of the PowerShell functions that read a task's owner.
const AUTOSTART_SCRIPT: &str = include_str!("../../../extractor_shim/autostart.ps1");

/// The exit code of a Windows task script that found the task owned by
/// someone this shell cannot act for, and changed nothing.
const TASK_LOCKED: i32 = 3;

/// The exit code of a Windows task script that could not read the task's
/// owner, and changed nothing.
const TASK_OWNER_UNREAD: i32 = 4;

/// The script that reads `task`'s lock into `$lock`, or prints why it could
/// not and exits [`TASK_OWNER_UNREAD`]: a failed read is never a pass.
fn lock_read(task: &str) -> Result<String> {
    Ok(format!(
        "try{{$lock=Get-TaskLock {}}}catch{{[Console]::Out.WriteLine(\"could not read the owner of the scheduled task \"+{}+\": \"+$_.Exception.Message+\"; nothing was changed\");exit {TASK_OWNER_UNREAD}}};",
        powershell_literal(task)?,
        powershell_literal(&format!("'{task}'"))?
    ))
}

/// The `Get-TaskLock` and `Format-TaskLock` functions: whether this shell may
/// change or remove a scheduled task, read without changing it. A task
/// registered from an elevated shell is owned by Administrators, and an
/// unelevated shell can neither replace nor remove it.
fn task_lock_functions() -> &'static str {
    let start = AUTOSTART_SCRIPT
        .find("# >>> task lock >>>")
        .expect("autostart.ps1 carries the task lock block");
    let end = AUTOSTART_SCRIPT
        .find("# <<< task lock <<<")
        .expect("autostart.ps1 closes the task lock block");
    &AUTOSTART_SCRIPT[start..end]
}

/// A script that changes nothing and exits [`TASK_LOCKED`] with the one-line
/// reason and fix when this shell may not remove `task`, else exits 0.
fn windows_removal_check(task: &str) -> Result<String> {
    Ok(format!(
        "{}$ErrorActionPreference='Stop';{}if($lock){{[Console]::Out.WriteLine((Format-TaskLock $lock -Removal));exit {TASK_LOCKED}}};exit 0",
        task_lock_functions(),
        lock_read(task)?
    ))
}

/// What a Windows task script exiting [`TASK_LOCKED`] printed: its last line.
fn locked_reason(output: &str) -> String {
    output
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("the scheduled task is owned by another account, so this shell cannot change it; nothing was changed")
        .trim()
        .to_owned()
}

/// The exit code of a scheduler script whose file changed between being
/// written and being read: nothing was run.
const SCRIPT_CHANGED: i32 = 5;

/// The Windows PowerShell command that runs the script file at `path` only
/// if its bytes still hash to `digest`. A short `-EncodedCommand` reads the
/// file once, checks the hash and runs those same bytes, so a process that
/// can write this user's temporary folder (an unelevated one, when this
/// command runs elevated) cannot swap the script in between.
fn windows_script_argv(path: &std::path::Path, digest: &str) -> Result<Vec<String>> {
    Ok(vec![
        windows_powershell()?,
        "-NoLogo".into(),
        "-NoProfile".into(),
        "-NonInteractive".into(),
        "-EncodedCommand".into(),
        base64(
            &windows_script_bootstrap(path, digest)?
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        ),
    ])
}

/// The bootstrap [`windows_script_argv`] encodes: read the file once, check
/// its hash, run those same bytes.
fn windows_script_bootstrap(path: &std::path::Path, digest: &str) -> Result<String> {
    let path = path
        .to_str()
        .ok_or_else(|| UpdateError::usage("the temporary scheduler script path must be UTF-8"))?;
    Ok(format!(
        "$b=[IO.File]::ReadAllBytes({});$s=[Security.Cryptography.SHA256]::Create();$h=-join ($s.ComputeHash($b)|ForEach-Object {{$_.ToString('x2')}});if($h -ne '{digest}'){{[Console]::Error.WriteLine('the scheduler script changed after it was written; nothing was run');exit {SCRIPT_CHANGED}}};. ([scriptblock]::Create([Text.Encoding]::UTF8.GetString($b)))",
        powershell_literal(path)?
    ))
}

/// Write `script` to a new temporary file as UTF-8; its path and SHA-256.
fn write_windows_script(script: &str) -> Result<(std::path::PathBuf, String)> {
    use sha2::{Digest, Sha256};
    let path =
        std::env::temp_dir().join(format!("pseudolife-schedule-{}.ps1", uuid::Uuid::new_v4()));
    fs::write(&path, script.as_bytes()).map_err(|e| {
        UpdateError::usage(format!(
            "could not write the scheduler script to {}: {e}",
            path.display()
        ))
    })?;
    Ok((path, format!("{:x}", Sha256::digest(script.as_bytes()))))
}

/// Run a Windows PowerShell `script` through a temporary file. Not as
/// `-EncodedCommand` itself: the task-lock functions plus a long action
/// (layout overrides with long paths) would pass Windows' 32,767-character
/// command-line limit.
fn run_windows_script(script: &str) -> Result<(i32, String)> {
    let (path, digest) = write_windows_script(script)?;
    let result =
        windows_script_argv(&path, &digest).map(|argv| seams::run_cli(&argv, None, 30, false));
    let _ = fs::remove_file(&path);
    result
}

/// The registration failure a Windows task script's exit code means.
fn registration_failure(code: i32, output: &str) -> UpdateError {
    UpdateError::usage(script_failure(
        code,
        output,
        "Task Scheduler registration failed. This per-user daily task needs no elevation; check that the Windows ScheduledTasks PowerShell module is available (Get-Command Register-ScheduledTask) and run this command again in a normal PowerShell",
    ))
}

/// The true line for a Windows task script that exited `code`: its own
/// refusal (a locked task, an owner it could not read), a script file that
/// changed before it ran, or the last line it printed with "nothing was
/// changed"; `fallback` only when it printed nothing.
fn script_failure(code: i32, output: &str, fallback: &str) -> String {
    let last = output
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty());
    match code {
        TASK_LOCKED | TASK_OWNER_UNREAD => locked_reason(output),
        SCRIPT_CHANGED => {
            "the scheduler script changed after it was written; nothing was run".to_owned()
        }
        _ => match last {
            Some(line) => format!("{line}; nothing was changed"),
            None => fallback.to_owned(),
        },
    }
}

fn windows_registration(
    task: &str,
    hour: u8,
    minute: u8,
    executable: &str,
    arguments: &str,
) -> Result<String> {
    // schtasks /TR is limited to 261 characters. Register the action through
    // Task Scheduler's object API so layout overrides and long paths survive.
    // A daily task for this user with a limited token needs no elevation;
    // one of the same name registered from an elevated shell cannot be
    // replaced from an unelevated one, so that is read first and the script
    // changes nothing and exits TASK_LOCKED with the reason.
    let name = powershell_literal(task)?;
    Ok(format!(
        "{}$ErrorActionPreference='Stop';{}if($lock){{[Console]::Out.WriteLine((Format-TaskLock $lock));exit {TASK_LOCKED}}};$action=New-ScheduledTaskAction -Execute {} -Argument {};$trigger=New-ScheduledTaskTrigger -Daily -At '{hour:02}:{minute:02}';$principal=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited;Register-ScheduledTask -TaskName {name} -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null",
        task_lock_functions(),
        lock_read(task)?,
        powershell_literal(executable)?,
        powershell_literal(arguments)?,
    ))
}

fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let a = chunk[0] as usize;
        let b = chunk.get(1).copied().unwrap_or(0) as usize;
        let c = chunk.get(2).copied().unwrap_or(0) as usize;
        out.push(ALPHABET[a >> 2] as char);
        out.push(ALPHABET[((a & 3) << 4) | (b >> 4)] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[((b & 15) << 2) | (c >> 6)] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[c & 63] as char
        } else {
            '='
        });
    }
    out
}

/// Whether `--schedule` writes the client schedule here (a shim runtime with
/// no daemon container) rather than the daemon's, or why it writes neither.
fn client_kind(update: &Update) -> Result<bool> {
    if !cfg!(any(windows, target_os = "linux")) {
        return Err(UpdateError::usage(
            "--schedule installs a Windows scheduled task or a systemd --user timer; on this platform run pseudolife-mcp update --unattended from your own scheduler",
        ));
    }
    let tier = update.detect_tier()?;
    let client = tier != "docker" && on_shim_runtime();
    if tier != "docker" && !client {
        return Err(UpdateError::usage(
            "--schedule needs a daemon container or a shim runtime; this install is neither. Run it through the launcher the client install put on PATH. Nothing was installed",
        ));
    }
    Ok(client)
}

pub(crate) fn schedule(update: &mut Update, when: &str) -> Result<()> {
    schedule_named(update, when, &NAMES)
}

fn schedule_named(update: &mut Update, when: &str, names: &Names) -> Result<()> {
    schedule_checked(update, when, names, &|| Ok(()))
}

/// `--schedule`, with `before_write` run after every check and immediately
/// before the task or unit is written (`connect` re-reads the schedule
/// there); `--schedule` itself passes one that always succeeds.
fn schedule_checked(
    update: &mut Update,
    when: &str,
    names: &Names,
    before_write: &dyn Fn() -> Result<()>,
) -> Result<()> {
    at(when)?;
    let client = client_kind(update)?;
    schedule_for(update, when, names, client, &command, before_write)
}

/// `--schedule` past its machine-kind check: the daemon's `/health` for a
/// client, the command (`argv_for`), the bearer for the daemon's run, then
/// `before_write`, then the write.
fn schedule_for(
    update: &mut Update,
    when: &str,
    names: &Names,
    client: bool,
    argv_for: &dyn Fn(&str) -> Result<Vec<String>>,
    before_write: &dyn Fn() -> Result<()>,
) -> Result<()> {
    let (hour, minute) = at(when)?;
    if client && update.daemon_health().is_none() {
        return Err(UpdateError::usage(format!(
            "this is a client machine, and the daemon at {} did not answer /health; the scheduled run follows that daemon's release. Check that the daemon is up. Nothing was installed. {}",
            update.o.daemon_url,
            super::client_rearm_hint(&format!("{hour:02}:{minute:02}"))
        )));
    }
    let argv = argv_for(&update.o.daemon_url)?;
    if !client && !daemon_bearer_resolves() {
        no_bearer(update, cfg!(windows))?;
    }
    before_write()?;
    write(update, names, client, hour, minute, &argv)?;
    if client {
        update.step(format!("the run moves this machine's clients to the release the daemon at {} runs, never past it; running sessions keep their runtime. The log is {}",update.o.daemon_url,seams::data_dir().join(LOG_NAME).display()));
        update.step(super::client_rearm_hint(&format!("{hour:02}:{minute:02}")));
    } else {
        update.step(format!("the run applies a release only while updates.unattended_daemon is on in the daemon's config.yaml (off by default) and no session is active on the board; the log is {}",seams::data_dir().join(LOG_NAME).display()));
        update.step("its board notice needs its own principal: a PSEUDOLIFE_MCP_TOKENS entry token:<name> on the daemon, <name> in coordination.daemon_notice_principals and allowed_principals, and that token as this run's bearer; until then the notice is refused and the log is the only record");
    }
    Ok(())
}

/// A bearer the daemon schedule's run will find: the User-scope environment
/// the Windows task inherits, or this shell's, whose token file the unit
/// carries.
fn daemon_bearer_resolves() -> bool {
    let file = if cfg!(windows) {
        user_environment("PSEUDOLIFE_MCP_TOKEN_FILE")
    } else {
        std::env::var("PSEUDOLIFE_MCP_TOKEN_FILE").ok()
    };
    let token = if cfg!(windows) {
        user_environment("PSEUDOLIFE_MCP_TOKEN")
    } else {
        std::env::var("PSEUDOLIFE_MCP_TOKEN").ok()
    };
    credential(file, token)
}

/// Register the task or install the timer that runs `argv` daily at
/// `hour:minute`, replacing one of the same name.
fn write(
    update: &mut Update,
    names: &Names,
    client: bool,
    hour: u8,
    minute: u8,
    argv: &[String],
) -> Result<()> {
    if cfg!(windows) {
        let task = if client {
            names.client_task
        } else {
            names.task
        };
        let (executable, arguments) = schedule_windows(argv)?;
        let script = windows_registration(task, hour, minute, &executable, &arguments)?;
        let (code, output) = run_windows_script(&script)?;
        if code != 0 {
            return Err(registration_failure(code, &output));
        }
        update.step(format!("registered the scheduled task '{task}' (daily at {hour:02}:{minute:02}); the task inherits your User-scope bearer environment, not this shell's"));
    } else {
        let environment = environment(client)?;
        let dir = systemd_dir();
        let name = if client {
            names.client_unit
        } else {
            names.unit
        };
        install_systemd(&dir, name, hour, minute, argv, &environment, |args| {
            seams::run_cli(args, None, 30, false)
        })?;
        update.step(format!("installed {name}.timer under {} (daily at {hour:02}:{minute:02}); the unit carries its own token file",dir.display()));
    }
    Ok(())
}

pub(crate) fn unschedule(update: &mut Update) -> Result<()> {
    if cfg!(windows) {
        // Read-only first, for both tasks: a task registered from an
        // elevated shell cannot be removed from an unelevated one, and
        // removing one task before refusing the other would leave half a
        // removal behind.
        for task in [TASK, CLIENT_TASK] {
            // Only a clean read that found nothing in the way lets the
            // delete run: a lock, an unread owner, a timeout or a script that
            // could not start all refuse, so an elevated shell never deletes
            // a task it could not prove is this user's.
            let (code, output) = run_windows_script(&windows_removal_check(task)?)?;
            if code != 0 {
                return Err(UpdateError::usage(script_failure(
                    code,
                    &output,
                    &format!(
                        "could not check who owns the scheduled task '{task}' (the check exited {code}); nothing was removed. Run --unschedule again; if it fails again, check that Windows PowerShell and its ScheduledTasks module work: Get-ScheduledTask -TaskName '{task}'"
                    ),
                )));
            }
        }
        let mut removed = false;
        for task in [TASK, CLIENT_TASK] {
            if seams::run_cli(
                &[
                    "schtasks".into(),
                    "/Query".into(),
                    "/TN".into(),
                    task.into(),
                ],
                None,
                30,
                false,
            )
            .0 != 0
            {
                continue;
            }
            if seams::run_cli(
                &[
                    "schtasks".into(),
                    "/Delete".into(),
                    "/F".into(),
                    "/TN".into(),
                    task.into(),
                ],
                None,
                30,
                false,
            )
            .0 != 0
            {
                return Err(UpdateError::usage(format!(
                    "schtasks /Delete failed for the scheduled task '{task}'. Run --unschedule again; if it fails again, run schtasks /Delete /TN \"{task}\" /F yourself to see the reason Task Scheduler gives"
                )));
            }
            update.step(format!("removed the scheduled task '{task}'"));
            removed = true;
        }
        if !removed {
            update.step(format!(
                "no scheduled task '{TASK}' or '{CLIENT_TASK}' is registered; nothing to remove"
            ));
        }
    } else if cfg!(target_os = "linux") {
        let dir = systemd_dir();
        let mut removed = false;
        for name in [UNIT, CLIENT_UNIT] {
            let paths = [
                dir.join(format!("{name}.service")),
                dir.join(format!("{name}.timer")),
            ];
            if !paths.iter().any(|p| p.exists()) {
                continue;
            }
            if seams::run_cli(
                &[
                    "systemctl".into(),
                    "--user".into(),
                    "disable".into(),
                    "--now".into(),
                    format!("{name}.timer"),
                ],
                None,
                30,
                false,
            )
            .0 != 0
            {
                return Err(UpdateError::usage(
                    "systemctl --user could not disable the timer; its files were retained. Check your user systemd session and run --unschedule again",
                ));
            }
            for path in paths {
                match fs::remove_file(path) {
                    Ok(()) => {}
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    Err(e) => return Err(e.into()),
                }
            }
            update.step(format!(
                "removed {name}.timer and its service from {}",
                dir.display()
            ));
            removed = true;
        }
        if seams::run_cli(
            &["systemctl".into(), "--user".into(), "daemon-reload".into()],
            None,
            30,
            false,
        )
        .0 != 0
        {
            return Err(UpdateError::usage(
                "systemctl --user daemon-reload failed; run --unschedule again after restoring your user systemd session",
            ));
        }
        match fs::remove_file(seams::data_dir().join("unattended-update.token")) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
        if !removed {
            update.step(format!(
                "no {UNIT}.timer or {CLIENT_UNIT}.timer is installed under {}; nothing to remove",
                dir.display()
            ));
        }
    } else {
        return Err(UpdateError::usage("nothing to remove on this platform"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A loopback `/health` that answers `{"status":"ok"}`; its URL.
    fn health_listener() -> String {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            use std::io::{Read, Write};
            for mut stream in listener.incoming().flatten() {
                let mut request = [0u8; 2048];
                let _ = stream.read(&mut request);
                let body = "{\"status\":\"ok\"}";
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: {}
Connection: close

{body}",
                    body.len()
                );
            }
        });
        format!("http://127.0.0.1:{port}")
    }

    #[test]
    fn the_check_before_the_write_runs_after_the_health_probe_and_the_command() {
        let names = Names {
            task: "unused",
            client_task: "unused",
            unit: "unused",
            client_unit: "unused",
        };
        let argv = |url: &str| -> Result<Vec<String>> {
            Ok(vec![
                "/fixture/pseudolife-mcp".into(),
                "update".into(),
                "--unattended".into(),
                "--daemon-url".into(),
                url.into(),
            ])
        };
        let calls = std::cell::Cell::new(0);
        let refuse = || -> Result<()> {
            calls.set(calls.get() + 1);
            Err(UpdateError::usage("refused before the write"))
        };
        // A client whose daemon does not answer /health stops there: the
        // check is not reached.
        let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://127.0.0.1:{}", closed.local_addr().unwrap().port());
        drop(closed);
        let mut update = Update::new(crate::cli::update::Options {
            daemon_url: url,
            ..crate::cli::update::Options::default()
        });
        let error = schedule_for(&mut update, "04:30", &names, true, &argv, &refuse).unwrap_err();
        assert!(
            error.message.contains("did not answer /health"),
            "{}",
            error.message
        );
        assert_eq!(calls.get(), 0);
        // A command that cannot be built stops before it too.
        let healthy = health_listener();
        let mut update = Update::new(crate::cli::update::Options {
            daemon_url: healthy.clone(),
            ..crate::cli::update::Options::default()
        });
        let no_argv = |_: &str| -> Result<Vec<String>> { Err(UpdateError::usage("no launcher")) };
        assert_eq!(
            schedule_for(&mut update, "04:30", &names, true, &no_argv, &refuse)
                .unwrap_err()
                .message,
            "no launcher"
        );
        assert_eq!(calls.get(), 0);
        // Past both, the check runs, and its refusal is the result: nothing
        // is written (the write would need a real scheduler).
        let mut update = Update::new(crate::cli::update::Options {
            daemon_url: healthy,
            ..crate::cli::update::Options::default()
        });
        assert_eq!(
            schedule_for(&mut update, "04:30", &names, true, &argv, &refuse)
                .unwrap_err()
                .message,
            "refused before the write"
        );
        assert_eq!(calls.get(), 1);
        assert!(update.report.steps.is_empty());
    }
    #[test]
    fn client_rearm_hint_uses_the_chosen_time_or_an_explicit_placeholder() {
        let known = super::super::client_rearm_hint("07:05");
        assert!(known.contains("pseudolife-mcp update --schedule 07:05 --daemon-url <new-url>"));
        assert!(!known.contains("chosen daily time"));
        let unknown = super::super::client_rearm_hint("HH:MM");
        assert!(unknown.contains("pseudolife-mcp update --schedule HH:MM --daemon-url <new-url>"));
        assert!(unknown.contains("your chosen daily time"));
    }
    #[test]
    fn schedule_time_is_ascii_bounded_and_injection_safe() {
        for input in ["0:00", "03:30", " 23:59 "] {
            assert!(at(input).is_ok());
        }
        for input in ["24:00", "0:0", "٣:30", "03:30\n[Service]", "03:30:00"] {
            assert!(at(input).is_err());
        }
    }
    #[test]
    fn systemd_argv_preserves_utf8_quotes_percent_dollars_and_backslashes() {
        assert_eq!(
            unit_quote("/x y/é%$\\\"", true).unwrap(),
            "\"/x y/é%%$$\\\\\\\"\""
        );
        assert_eq!(unit_quote("KEY=x%$", false).unwrap(), "\"KEY=x%%$\"");
        assert!(unit_quote("x\nExecStart=bad", true).is_err());
    }
    #[test]
    fn windows_task_argv_quotes_embedded_quotes_and_final_slashes() {
        assert_eq!(windows_quote("x y\\").unwrap(), "\"x y\\\\\"");
        assert_eq!(windows_quote("x\\\"y").unwrap(), "\"x\\\\\\\"y\"");
        assert!(windows_quote("x\r/y").is_err());
    }
    #[test]
    fn encoded_task_script_is_utf16_base64() {
        assert_eq!(base64(&[]), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
    }
    #[test]
    fn windows_registration_keeps_long_action_arguments_out_of_schtasks() {
        let arguments = format!("-EncodedCommand {}", "A".repeat(400));
        let script =
            windows_registration("fixture ' task", 3, 30, "C:/fixture é/cli.exe", &arguments)
                .unwrap();
        assert!(script.contains("New-ScheduledTaskAction -Execute 'C:/fixture é/cli.exe'"));
        assert!(script.contains(&format!("-Argument '{arguments}'")));
        assert!(script.contains("-TaskName 'fixture '' task'"));
        assert!(script.contains("-LogonType Interactive -RunLevel Limited"));
        assert!(!script.contains("schtasks"));
        assert!(windows_registration("bad\nname", 3, 30, "cli", "update").is_err());
    }
    #[test]
    fn windows_registration_reads_the_tasks_owner_before_it_registers() {
        // A task registered from an elevated shell is owned by Administrators
        // and cannot be replaced unelevated: the script reads that first and
        // exits TASK_LOCKED having changed nothing.
        let script = windows_registration("fixture ' task", 3, 30, "cli.exe", "update").unwrap();
        let check = script
            .find("try{$lock=Get-TaskLock 'fixture '' task'}catch{")
            .unwrap();
        let register = script
            .find("Register-ScheduledTask -TaskName 'fixture '' task'")
            .unwrap();
        assert!(check < register);
        assert!(script[check..register].contains(&format!("exit {TASK_LOCKED}")));
        assert!(script.starts_with("# >>> task lock >>>"));
        assert!(script.contains("function Get-TaskLock"));
        assert!(!script.contains("RunLevel Highest"));
    }
    #[test]
    fn a_failed_registration_says_why_without_asking_for_elevation() {
        let line = "the scheduled task 'Pseudolife Client Update' was registered from an elevated PowerShell, so Administrators own it, and this unelevated shell cannot change or remove it; nothing was changed";
        let locked = registration_failure(TASK_LOCKED, &format!("noise\n{line}\r\n\n"));
        assert_eq!(locked.message, line);
        // An owner that could not be read refuses with its own line, never a
        // fall-through to registration or deletion.
        let unread =
            "could not read the owner of the scheduled task 'X': fixture; nothing was changed";
        assert_eq!(
            registration_failure(TASK_OWNER_UNREAD, unread).message,
            unread
        );
        let other = registration_failure(1, "Access is denied.");
        assert!(
            other.message.contains("needs no elevation"),
            "{}",
            other.message
        );
        assert!(
            !other.message.contains("from an elevated PowerShell"),
            "{}",
            other.message
        );
        assert_eq!(
            locked_reason(""),
            "the scheduled task is owned by another account, so this shell cannot change it; nothing was changed"
        );
    }
    #[test]
    fn every_script_failure_says_its_own_cause() {
        assert_eq!(
            script_failure(
                SCRIPT_CHANGED,
                "the scheduler script changed after it was written; nothing was run",
                "x"
            ),
            "the scheduler script changed after it was written; nothing was run"
        );
        assert_eq!(
            script_failure(1, "noise\nTerm 'Get-TaskLock' is not recognized\r\n", "x"),
            "Term 'Get-TaskLock' is not recognized; nothing was changed"
        );
        assert_eq!(script_failure(1, "", "the fallback"), "the fallback");
        let line =
            "could not read the owner of the scheduled task 'X': denied; nothing was changed";
        assert_eq!(script_failure(TASK_OWNER_UNREAD, line, "x"), line);
    }
    #[test]
    fn the_unschedule_check_reads_and_never_removes() {
        let script = windows_removal_check("Pseudolife Client Update").unwrap();
        assert!(script.contains("$lock=Get-TaskLock 'Pseudolife Client Update'"));
        assert!(script.contains("Format-TaskLock $lock -Removal"));
        assert!(script.ends_with("exit 0"));
        let read = script
            .find("try{$lock=Get-TaskLock 'Pseudolife Client Update'}catch{")
            .unwrap();
        assert!(script[read..].contains(&format!("exit {TASK_OWNER_UNREAD}}}")));
        // Only the printed fix names Unregister-ScheduledTask; nothing calls it.
        let body = &script[script.find("$ErrorActionPreference").unwrap()..];
        assert!(!body.contains("Unregister-ScheduledTask") && !body.contains("schtasks"));
        assert!(windows_removal_check("bad\nname").is_err());
    }
    #[test]
    fn the_scheduler_script_runs_from_a_file_not_the_command_line() {
        // The lock functions alone are several kilobytes; with long layout
        // paths the encoded form passed the 32,767-character limit. Only a
        // short bootstrap goes on the command line, and it runs the file's
        // bytes only when they still hash to what this process wrote.
        let digest = "ab".repeat(32);
        let bootstrap = windows_script_bootstrap(
            std::path::Path::new("C:/temp/pseudolife-schedule-x.ps1"),
            &digest,
        )
        .unwrap();
        // UTF-16 base64 of the bootstrap stays far below the limit.
        assert!(bootstrap.len() * 2 * 4 / 3 < 2000);
        assert!(task_lock_functions().len() > 4000);
        assert!(bootstrap.contains("ReadAllBytes('C:/temp/pseudolife-schedule-x.ps1')"));
        let check = bootstrap.find(&format!("if($h -ne '{digest}')")).unwrap();
        assert!(check < bootstrap.find("[scriptblock]::Create").unwrap());
        assert!(bootstrap.contains(&format!("exit {SCRIPT_CHANGED}")));
    }
    #[cfg(windows)]
    #[test]
    fn a_scheduler_script_changed_after_it_was_written_is_not_run() {
        let (path, digest) =
            write_windows_script("[Console]::Out.WriteLine('original');exit 0").unwrap();
        fs::write(&path, "[Console]::Out.WriteLine('swapped');exit 0").unwrap();
        let (code, output) = seams::run_cli(
            &windows_script_argv(&path, &digest).unwrap(),
            None,
            30,
            false,
        );
        let _ = fs::remove_file(&path);
        assert_eq!(code, SCRIPT_CHANGED, "{output}");
        assert!(!output.contains("swapped"), "{output}");
    }
    #[cfg(windows)]
    #[test]
    fn a_scheduler_script_longer_than_the_command_line_limit_runs() {
        let script = format!(
            "$x='{}';[Console]::Out.WriteLine('ran');exit 3",
            "a".repeat(40_000)
        );
        let (code, output) = run_windows_script(&script).unwrap();
        assert_eq!(code, 3, "{output}");
        assert!(output.contains("ran"), "{output}");
    }
    #[test]
    fn the_task_lock_functions_are_the_embedded_autostart_copy() {
        let functions = task_lock_functions();
        assert!(functions.starts_with("# >>> task lock >>>"));
        for name in [
            "Get-TaskOwnerSddl",
            "Get-TaskLock",
            "Format-TaskLock",
            "Test-TaskShellElevated",
        ] {
            assert!(functions.contains(&format!("function {name}")), "{name}");
        }
        assert!(!functions.contains("# <<< task lock <<<"));
    }
    /// A fake Schedule.Service whose one task is owned by Administrators, and
    /// an unelevated token, so the real lock functions run without reading a
    /// real task.
    #[cfg(windows)]
    const LOCKED_TASK_MOCKS: &str = r#"
function New-Object { param([string]$TypeName,[object[]]$ArgumentList,[string]$ComObject)
 if ($ComObject) {
  $task = [pscustomobject]@{}; $task | Add-Member ScriptMethod GetSecurityDescriptor { param($i) 'O:BAG:SYD:(A;ID;FA;;;BA)' }
  $folder = [pscustomobject]@{ Task=$task }; $folder | Add-Member ScriptMethod GetTask { param($n) $this.Task }
  $service = [pscustomobject]@{ Folder=$folder }; $service | Add-Member ScriptMethod Connect { }
  $service | Add-Member ScriptMethod GetFolder { param($p) $this.Folder }; return $service }
 if ($TypeName -eq 'Security.Principal.WindowsPrincipal') { $p=[pscustomobject]@{}; $p | Add-Member ScriptMethod IsInRole { param($r) $false }; return $p }
 if ($ArgumentList) { Microsoft.PowerShell.Utility\New-Object -TypeName $TypeName -ArgumentList $ArgumentList }
 else { Microsoft.PowerShell.Utility\New-Object -TypeName $TypeName }
}
function Register-ScheduledTask { throw 'registered over a locked task' }
function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction)
 [pscustomobject]@{ TaskName=$TaskName; Principal=[pscustomobject]@{ UserId=[Security.Principal.WindowsIdentity]::GetCurrent().Name } }
}
"#;
    #[cfg(windows)]
    fn run_mocked(script: &str) -> (i32, String) {
        // Through the product's own file runner: the script is longer than
        // a command line may be.
        run_windows_script(&format!("{LOCKED_TASK_MOCKS}{script}")).unwrap()
    }
    #[cfg(windows)]
    #[test]
    fn a_task_administrators_own_is_left_alone_with_its_one_elevated_command() {
        let (code, output) =
            run_mocked(&windows_registration(CLIENT_TASK, 4, 30, "C:/cli.exe", "update").unwrap());
        assert_eq!(code, TASK_LOCKED, "{output}");
        let reason = locked_reason(&output);
        assert!(reason.contains("'Pseudolife Client Update' was registered from an elevated PowerShell, so Administrators own it"), "{reason}");
        assert!(reason.contains("nothing was changed"), "{reason}");
        assert!(
            reason.contains(
                "Unregister-ScheduledTask -TaskName 'Pseudolife Client Update' -Confirm:$false"
            ),
            "{reason}"
        );
        let (code, output) = run_mocked(&windows_removal_check(CLIENT_TASK).unwrap());
        assert_eq!(code, TASK_LOCKED, "{output}");
        assert!(locked_reason(&output).contains("To remove it, run this once"));
    }
    #[cfg(windows)]
    #[test]
    fn windows_registration_passes_long_utf8_action_to_scheduler_objects() {
        let arguments = format!("-EncodedCommand {}", "A".repeat(400));
        let mut script = String::from(
            r#"
function New-Object { param([string]$TypeName,[object[]]$ArgumentList,[string]$ComObject)
 # No task of this name: the owner check never reads the real Task Scheduler.
 if (!$ComObject) { throw "unexpected New-Object $TypeName" }
 $folder = [pscustomobject]@{}; $folder | Add-Member ScriptMethod GetTask { param($n) throw [Runtime.InteropServices.COMException]::new('absent', -2147024894) }
 $service = [pscustomobject]@{ Folder=$folder }; $service | Add-Member ScriptMethod Connect { }
 $service | Add-Member ScriptMethod GetFolder { param($p) $this.Folder }; $service
}
function New-ScheduledTaskAction { param($Execute,$Argument)
 if ($Execute -ne 'C:/fixture é/cli.exe' -or $Argument.Length -ne 416) { throw 'action changed' }
 [pscustomobject]@{Execute=$Execute;Arguments=$Argument}
}
function New-ScheduledTaskTrigger { param([switch]$Daily,$At)
 if (!$Daily -or $At -ne '03:30') { throw 'trigger changed' }; 'trigger'
}
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel)
 if (!$UserId -or $LogonType -ne 'Interactive' -or $RunLevel -ne 'Limited') { throw 'principal changed' }; 'principal'
}
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Principal,[switch]$Force)
 if ($TaskName -ne "fixture ' task" -or !$Force -or $Action.Arguments.Length -ne 416 -or $Trigger -ne 'trigger' -or $Principal -ne 'principal') { throw 'registration changed' }
}
"#,
        );
        script.push_str(
            &windows_registration("fixture ' task", 3, 30, "C:/fixture é/cli.exe", &arguments)
                .unwrap(),
        );
        let (code, output) = run_windows_script(&script).unwrap();
        assert_eq!(code, 0, "{output}");
    }
    #[test]
    fn failed_systemd_enable_restores_existing_utf8_units() {
        let root =
            std::env::temp_dir().join(format!("unattended-schedule-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let service = root.join("pseudolife-update.service");
        let timer = root.join("pseudolife-update.timer");
        fs::write(&service, "previous é\n").unwrap();
        fs::write(&timer, "old timer\n").unwrap();
        let mut commands = vec![];
        let result = install_systemd(
            &root,
            UNIT,
            3,
            30,
            &["/fixture/é binary".into(), "update".into()],
            &[],
            |argv| {
                commands.push(argv.to_vec());
                (
                    if argv.iter().any(|s| s == "enable") {
                        1
                    } else {
                        0
                    },
                    String::new(),
                )
            },
        );
        assert!(result.is_err());
        assert_eq!(fs::read_to_string(service).unwrap(), "previous é\n");
        assert_eq!(fs::read_to_string(timer).unwrap(), "old timer\n");
        assert!(
            !commands
                .iter()
                .any(|args| args.iter().any(|s| s == "disable"))
        );
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn new_systemd_job_has_only_success_codes_three_and_four() {
        let root =
            std::env::temp_dir().join(format!("unattended-new-unit-{}", uuid::Uuid::new_v4()));
        let mut commands = vec![];
        install_systemd(
            &root,
            CLIENT_UNIT,
            3,
            5,
            &[
                "/fixture/é binary".into(),
                "update".into(),
                "--unattended".into(),
            ],
            &["Environment=\"XDG_DATA_HOME=/fixture/data\"".into()],
            |argv| {
                commands.push(argv.to_vec());
                (0, String::new())
            },
        )
        .unwrap();
        let body = fs::read_to_string(root.join("pseudolife-client-update.service")).unwrap();
        assert!(body.contains("SuccessExitStatus=3 4\n"));
        assert!(body.contains("ExecStart=\"/fixture/é binary\" \"update\" \"--unattended\""));
        assert!(!body.contains("TOKEN"));
        assert!(
            fs::read_to_string(root.join("pseudolife-client-update.timer"))
                .unwrap()
                .contains("OnCalendar=*-*-* 03:05:00")
        );
        assert_eq!(commands.len(), 2);
        fs::remove_dir_all(root).unwrap();
    }
}
