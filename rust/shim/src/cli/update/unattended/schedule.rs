use super::*;

const TASK: &str = "Pseudolife Unattended Update";
const CLIENT_TASK: &str = "Pseudolife Client Update";
const UNIT: &str = "pseudolife-update";
const CLIENT_UNIT: &str = "pseudolife-client-update";
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
    let fix = if windows {
        "Set PSEUDOLIFE_MCP_TOKEN_FILE once in your Windows User-scope environment with [Environment]::SetEnvironmentVariable('PSEUDOLIFE_MCP_TOKEN_FILE','<private token file>','User'), then run --schedule again"
    } else {
        "Run --schedule again from a shell with PSEUDOLIFE_MCP_TOKEN_FILE=<private token file>"
    };
    let text = format!(
        "no bearer resolves for the scheduled run{}: the scheduled run cannot read the board, so it would hold off every day (exit 4, which the scheduler counts as success) and never apply a release. {fix}",
        if windows {
            " in your User-scope environment, which the task inherits"
        } else {
            " in this shell, whose token file the unit carries"
        }
    );
    if update.o.allow_no_bearer {
        update.warn(format!("{text}. Installed anyway (--allow-no-bearer)"));
        Ok(())
    } else {
        Err(UpdateError::usage(format!(
            "{text}; or pass --allow-no-bearer to install it anyway. Nothing was installed"
        )))
    }
}

fn environment(client: bool) -> Result<Vec<String>> {
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
    let exec = argv
        .iter()
        .map(|s| unit_quote(s, true))
        .collect::<Result<Vec<_>>>()?
        .join(" ");
    let label = if name == CLIENT_UNIT {
        "client"
    } else {
        "daemon"
    };
    let body = format!(
        "[Unit]\nDescription=Pseudolife-MCP unattended {label} update\n\n[Service]\nType=oneshot\n{}\nExecStart={exec}\nSuccessExitStatus=3 4\n",
        environment.join("\n")
    );
    let timer_body = format!(
        "[Unit]\nDescription=Pseudolife-MCP unattended {label} update (daily)\n\n[Timer]\nOnCalendar=*-*-* {hour:02}:{minute:02}:00\nPersistent=true\nRandomizedDelaySec=300\n\n[Install]\nWantedBy=timers.target\n"
    );
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

fn windows_registration(
    task: &str,
    hour: u8,
    minute: u8,
    executable: &str,
    arguments: &str,
) -> Result<String> {
    // schtasks /TR is limited to 261 characters. Register the action through
    // Task Scheduler's object API so layout overrides and long paths survive.
    Ok(format!(
        "$ErrorActionPreference='Stop';$action=New-ScheduledTaskAction -Execute {} -Argument {};$trigger=New-ScheduledTaskTrigger -Daily -At '{hour:02}:{minute:02}';$principal=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited;Register-ScheduledTask -TaskName {} -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null",
        powershell_literal(executable)?,
        powershell_literal(arguments)?,
        powershell_literal(task)?
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

pub(crate) fn schedule(update: &mut Update, when: &str) -> Result<()> {
    let (hour, minute) = at(when)?;
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
    if client && update.daemon_health().is_none() {
        return Err(UpdateError::usage(
            "this is a client machine, and the daemon did not answer /health; the scheduled run follows that daemon's release. Check --daemon-url and run --schedule again. Nothing was installed",
        ));
    }
    let argv = command(&update.o.daemon_url)?;
    if !client {
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
        if !credential(file, token) {
            no_bearer(update, cfg!(windows))?;
        }
    }
    if cfg!(windows) {
        let task = if client { CLIENT_TASK } else { TASK };
        let (executable, arguments) = schedule_windows(&argv)?;
        let script = windows_registration(task, hour, minute, &executable, &arguments)?;
        let encoded = base64(
            &script
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        );
        let code = seams::run_cli(
            &[
                windows_powershell()?,
                "-NoLogo".into(),
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-EncodedCommand".into(),
                encoded,
            ],
            None,
            30,
            false,
        )
        .0;
        if code != 0 {
            return Err(UpdateError::usage(
                "Task Scheduler registration failed. Check that the Windows ScheduledTasks PowerShell module is available; an unelevated administrator account may need to run this command once from an elevated PowerShell fresh from the Start menu",
            ));
        }
        update.step(format!("registered the scheduled task '{task}' (daily at {hour:02}:{minute:02}); the task inherits your User-scope bearer environment, not this shell's"));
    } else {
        let environment = environment(client)?;
        let dir = systemd_dir();
        let name = if client { CLIENT_UNIT } else { UNIT };
        install_systemd(&dir, name, hour, minute, &argv, &environment, |args| {
            seams::run_cli(args, None, 30, false)
        })?;
        update.step(format!("installed {name}.timer under {} (daily at {hour:02}:{minute:02}); the unit carries its own token file",dir.display()));
    }
    if client {
        update.step(format!("the run moves this machine's clients to the release the daemon at {} runs, never past it; running sessions keep their runtime. The log is {}",update.o.daemon_url,seams::data_dir().join(LOG_NAME).display()));
    } else {
        update.step(format!("the run applies a release only while updates.unattended_daemon is on in the daemon's config.yaml (off by default) and no session is active on the board; the log is {}",seams::data_dir().join(LOG_NAME).display()));
        update.step("its board notice needs its own principal: a PSEUDOLIFE_MCP_TOKENS entry token:<name> on the daemon, <name> in coordination.daemon_notice_principals and allowed_principals, and that token as this run's bearer; until then the notice is refused and the log is the only record");
    }
    Ok(())
}

pub(crate) fn unschedule(update: &mut Update) -> Result<()> {
    if cfg!(windows) {
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
                return Err(UpdateError::usage(
                    "schtasks /Delete failed; run --unschedule again from an elevated PowerShell",
                ));
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
    #[cfg(windows)]
    #[test]
    fn windows_registration_passes_long_utf8_action_to_scheduler_objects() {
        let arguments = format!("-EncodedCommand {}", "A".repeat(400));
        let mut script = String::from(
            r#"
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
        let encoded = base64(
            &script
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        );
        let output = std::process::Command::new(windows_powershell().unwrap())
            .args([
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                &encoded,
            ])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
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
