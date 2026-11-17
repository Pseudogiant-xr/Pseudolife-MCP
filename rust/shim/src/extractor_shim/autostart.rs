//! Registration uses the platform's scheduler; neither installation nor launch needs Python.
use super::config;
use serde_json::json;
use std::{ffi::OsString, fs, path::PathBuf, process::Command};

struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn windows_registration_command(
    script: &std::path::Path,
    data: &std::path::Path,
    script_bytes: &[u8],
    data_bytes: &[u8],
) -> Result<Command, String> {
    use sha2::{Digest, Sha256};
    let literal = |path: &std::path::Path| {
        path.to_str()
            .map(|p| format!("'{}'", p.replace('\'', "''")))
            .ok_or("the temporary autostart registration path must be UTF-8")
    };
    // Read each file once, verify those bytes, then run/parse only those
    // bytes. Neither the script nor its JSON is reopened after verification.
    let script_digest = format!("{:x}", Sha256::digest(script_bytes));
    let data_digest = format!("{:x}", Sha256::digest(data_bytes));
    let bootstrap = format!(
        "$ErrorActionPreference='Stop';try{{$s=[Security.Cryptography.SHA256]::Create();$b=[IO.File]::ReadAllBytes({script});$h=-join ($s.ComputeHash($b)|ForEach-Object {{$_.ToString('x2')}});if($h -ne '{script_digest}'){{[Console]::Error.WriteLine('autostart registration script changed after it was written; nothing was run; rerun install-autostart');exit 5}};$d=[IO.File]::ReadAllBytes({data});$h=-join ($s.ComputeHash($d)|ForEach-Object {{$_.ToString('x2')}});if($h -ne '{data_digest}'){{[Console]::Error.WriteLine('autostart registration data changed after it was written; nothing was run; rerun install-autostart');exit 5}}}}catch{{[Console]::Error.WriteLine('cannot verify autostart registration files; rerun install-autostart: '+$_.Exception.Message);exit 1}}finally{{if($s){{$s.Dispose()}}}};& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString($b))) -ConfigJson ([Text.Encoding]::UTF8.GetString($d))",
        script = literal(script)?,
        data = literal(data)?,
    );
    let argv = crate::windows_powershell::encoded(&bootstrap)?;
    let mut command = Command::new(&argv[0]);
    command.args(&argv[1..]);
    Ok(command)
}

/// The exit code of a refusal over a scheduled task this shell may not
/// change (one an elevated shell registered, or another account's): the
/// script printed its one line and changed nothing, so callers print no
/// more than that.
pub(super) const TASK_LOCKED_EXIT: u8 = 3;

/// Why registration did not complete.
pub(super) enum Failure {
    /// Refused over a task this shell may not change; the line is printed.
    Locked,
    /// Anything else, with its message.
    Other(String),
}

impl From<String> for Failure {
    fn from(message: String) -> Self {
        Failure::Other(message)
    }
}

impl From<&str> for Failure {
    fn from(message: &str) -> Self {
        Failure::Other(message.to_owned())
    }
}

pub(super) fn install(args: &[OsString], remove: bool) -> Result<(), Failure> {
    if args.is_empty() || args.iter().any(|a| a == "--help") {
        println!(
            "usage: pseudolife-mcp extractor-shim install-autostart claude|codex [--env-file PATH] [--repo PATH] [--name NAME] [--native-exe PATH] [--replace-sibling] [--startup-timeout SECONDS] [--task-logon-type Interactive|S4U] [shim settings]\nDefault config: ~/.pseudolife-mcp/extractor-shims.env. Settings include --cli, --model, --host, --port, --prompt-file, --health-ttl and --log. An explicit --repo reads that checkout's ops/.env. Registration requires systemd --user on Linux. On Windows it registers a task for this user only and needs no elevation; a task of the same name registered from an elevated PowerShell is never re-registered: when it already runs this action it is restarted with the new settings, otherwise nothing changes and the error names the one elevated command that removes it. S4U is an explicit local-only task principal for unattended hosts, and Windows lets only an elevated PowerShell register it."
        );
        return Ok(());
    }
    let kind = args[0].to_str().ok_or("provider must be Unicode")?;
    if !["claude", "codex"].contains(&kind) {
        return Err("expected claude or codex".into());
    }
    let mut settings = vec![args[0].clone()];
    let mut name = None;
    let mut launcher = None;
    let mut replace = false;
    let mut timeout = "90".to_owned();
    let mut logon = "Interactive".to_owned();
    let mut at = 1;
    while at < args.len() {
        let key = args[at].to_str().ok_or("option must be Unicode")?;
        at += 1;
        if key == "--replace-sibling" {
            replace = true;
            continue;
        }
        let value = args
            .get(at)
            .ok_or_else(|| format!("{key} needs a value"))?
            .clone();
        at += 1;
        match key {
            "--native-exe" => launcher = Some(PathBuf::from(value)),
            "--name" => name = Some(value.to_string_lossy().into_owned()),
            "--startup-timeout" => timeout = value.to_string_lossy().into_owned(),
            "--task-logon-type" => logon = value.to_string_lossy().into_owned(),
            _ => settings.extend([key.into(), value]),
        }
    }
    let wait = timeout
        .parse::<u32>()
        .map_err(|_| "startup timeout must be positive seconds")?;
    if wait == 0 {
        return Err("startup timeout must be positive seconds".into());
    }
    if !["Interactive", "S4U"].contains(&logon.as_str()) {
        return Err("task logon type must be Interactive or S4U".into());
    }
    let find = |key: &str| {
        settings
            .windows(2)
            .find(|a| a[0] == key)
            .map(|a| PathBuf::from(&a[1]))
    };
    let repo = find("--repo")
        .map(|p| config::absolute_path(&p))
        .transpose()?;
    let env_file = find("--env-file")
        .or_else(|| repo.as_ref().map(|p| p.join("ops/.env")))
        .unwrap_or(
            dirs::home_dir()
                .ok_or("home directory unavailable")?
                .join(".pseudolife-mcp/extractor-shims.env"),
        );
    let env_file = config::absolute_path(&env_file)?;
    settings.extend(["--env-file".into(), env_file.as_os_str().into()]);
    if let Some(repo) = &repo {
        settings.extend(["--repo".into(), repo.as_os_str().into()]);
    }
    if !settings.iter().any(|a| a == "--log") {
        let path = dirs::home_dir()
            .ok_or("home directory unavailable")?
            .join(format!(".pseudolife-mcp/{kind}-shim.log"));
        settings.extend(["--log".into(), path.into_os_string()]);
    }
    for at in 0..settings.len().saturating_sub(1) {
        if settings[at] == "--log" && !settings[at + 1].to_string_lossy().starts_with("~/") {
            settings[at + 1] =
                config::absolute_path(&PathBuf::from(&settings[at + 1]))?.into_os_string();
        }
    }
    // Resolve and validate everything without invoking a provider or modifying registration.
    let config = if remove {
        None
    } else {
        Some(config::parse(settings.clone())?.ok_or("missing shim configuration")?)
    };
    if let Some(config) = &config {
        if config.port == 0 {
            return Err("autostart requires a fixed nonzero port".into());
        }
        // The official Windows Codex installer rotates its hashed directory.
        // An implicit discovery must be repeated at every service start.
        if !(cfg!(windows) && kind == "codex" && !settings.iter().any(|a| a == "--cli")) {
            settings.extend([
                "--cli".into(),
                config::absolute_path(&config.cli)?.into_os_string(),
            ]);
        }
    }
    let port = config.as_ref().map(|c| c.port).unwrap_or(0);
    let host = config.as_ref().map(|c| c.host.as_str()).unwrap_or("");
    let name = name.unwrap_or_else(|| {
        if cfg!(windows) {
            format!(
                "Pseudolife {} Shim",
                if kind == "claude" { "Claude" } else { "Codex" }
            )
        } else if kind == "claude" {
            "pseudolife-sonnet-shim.service".into()
        } else {
            "pseudolife-codex-shim.service".into()
        }
    });
    if name.is_empty()
        || name.chars().any(|c| c.is_control() || "/\\".contains(c))
        // ScheduledTasks uses wildcard matching, including backtick escaping.
        || (cfg!(windows) && name.contains(['*', '?', '[', ']', '`']))
    {
        return Err("invalid registration name; use a literal name without control characters, slashes or wildcard characters".into());
    }
    if !cfg!(windows)
        && (!name.ends_with(".service")
            || !name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "_.@-".contains(c)))
    {
        return Err("invalid systemd unit name".into());
    }
    if remove && cfg!(target_os = "linux") {
        let config_dir = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or(
                dirs::home_dir()
                    .ok_or("home directory unavailable")?
                    .join(".config"),
            );
        // Endpoint installs clean up optional extractors even on hosts without
        // a service manager. An absent registration needs no external command.
        match fs::symlink_metadata(config_dir.join("systemd/user").join(&name)) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e.to_string().into()),
            Ok(_) => {}
        }
        if !std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
            .any(|p| p.join("systemctl").is_file())
        {
            println!("systemctl unavailable; skipping removal of {name}");
            return Ok(());
        }
    }
    let owner_exe = config::absolute_path(&std::env::current_exe().map_err(|e| e.to_string())?)?;
    let native = config::absolute_path(&launcher.unwrap_or_else(|| owner_exe.clone()))?;
    if !native.is_file() {
        return Err(format!("native launcher not found: {}", native.display()).into());
    }
    let scratch = Scratch(
        std::env::temp_dir().join(format!("pseudolife-shim-install-{}", uuid::Uuid::new_v4())),
    );
    fs::create_dir(&scratch.0).map_err(|e| e.to_string())?;
    let mut config_args = vec!["extractor-shim".to_owned(), "config".into()];
    config_args.extend(settings.iter().map(|s| s.to_string_lossy().into_owned()));
    let mut serve_args = vec![
        "extractor-shim".to_owned(),
        kind.into(),
        "--env-file".into(),
        env_file.to_string_lossy().into_owned(),
    ];
    if let Some(repo) = repo {
        serve_args.extend(["--repo".into(), repo.to_string_lossy().into_owned()]);
    }
    if let Some(receipt_dir) = config.as_ref().and_then(|c| c.receipt_dir.as_ref()) {
        // Pin the installer's resolved directory. In particular, systemd
        // need not inherit its XDG_CACHE_HOME override.
        serve_args.extend([
            "--receipt-dir".into(),
            receipt_dir.to_string_lossy().into_owned(),
        ]);
    }
    let status = if cfg!(windows) {
        let script = scratch.0.join("register.ps1");
        let data = scratch.0.join("registration.json");
        let script_bytes = include_bytes!("autostart.ps1");
        let data_bytes = serde_json::to_vec(&json!({"Native":native,"OwnerExe":owner_exe,"Kind":kind,"EnvFile":env_file,"Name":name,"ReplaceSibling":replace,"Remove":remove,"Port":port,"Host":host,"Timeout":wait,"LogonType":logon,"LockExitCode":TASK_LOCKED_EXIT,"ConfigArgs":config_args,"ServeArgs":serve_args})).expect("registration JSON");
        fs::write(&script,script_bytes).map_err(|e|e.to_string())?;
        fs::write(&data,&data_bytes).map_err(|e|e.to_string())?;
        windows_registration_command(&script, &data, script_bytes, &data_bytes)?.status()
    } else if cfg!(target_os="linux") {
        let script = scratch.0.join("register.sh");
        fs::write(&script,include_str!("autostart.sh")).map_err(|e|e.to_string())?;
        Command::new("bash").arg(script).arg(if remove {"remove"} else {"install"}).arg(&owner_exe).arg(&native).arg(kind).arg(&env_file).arg(name).arg(if replace {"yes"}else{"no"}).arg(port.to_string()).arg(host).arg(wait.to_string()).arg(serve_args.len().to_string()).args(serve_args).args(config_args).status()
    } else { return Err("native autostart requires Windows Task Scheduler or Linux systemd --user".into()); }
    .map_err(|e|format!("cannot run the platform registration helper: {e}"))?;
    if status.success() {
        Ok(())
    } else if cfg!(windows) && status.code() == Some(i32::from(TASK_LOCKED_EXIT)) {
        Err(Failure::Locked)
    } else {
        Err(format!("autostart registration failed ({status}); see the diagnostic above").into())
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    #[test]
    fn registration_uses_system_windows_powershell() {
        let command = windows_registration_command(
            "C:/fixture/register.ps1".as_ref(),
            "C:/fixture/registration.json".as_ref(),
            b"",
            b"",
        )
        .unwrap();
        let expected = PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32/WindowsPowerShell/v1.0/powershell.exe");
        assert!(std::path::Path::new(command.get_program()).is_absolute());
        assert_eq!(command.get_program(), expected.as_os_str());
        assert!(command.get_args().any(|arg| arg == "-EncodedCommand"));
    }

    #[test]
    fn registration_refuses_a_changed_script() {
        registration_fixture("script");
    }

    #[test]
    fn registration_refuses_changed_data() {
        registration_fixture("data");
    }

    #[test]
    fn registration_accepts_unchanged_files() {
        registration_fixture("neither");
    }

    #[test]
    fn registration_accepts_paths_with_typographic_quotes() {
        registration_fixture("typographic");
    }

    fn registration_fixture(changed: &str) {
        let scratch = Scratch(
            std::env::temp_dir().join(format!("autostart-bootstrap-{}", uuid::Uuid::new_v4())),
        );
        fs::create_dir(&scratch.0).unwrap();
        let script = scratch.0.join(if changed == "typographic" {
            "register ' é ‘ ’ “ ”.ps1"
        } else {
            "register ' é.ps1"
        });
        let data = scratch.0.join(if changed == "typographic" {
            "registration ' é ‘ ’ “ ”.json"
        } else {
            "registration ' é.json"
        });
        let script_bytes = br#"param([string]$ConfigPath, [string]$ConfigJson)
if (!$ConfigJson) { $ConfigJson = [IO.File]::ReadAllText($ConfigPath) }
$c = $ConfigJson | ConvertFrom-Json
[Console]::Out.WriteLine('registered ' + $c.Name)
exit 0"#;
        let data_bytes = br#"{"Name":"fixture"}"#;
        // Each case uses the production command builder, with no real scheduler.
        {
            fs::write(&script, script_bytes).unwrap();
            fs::write(&data, data_bytes).unwrap();
            let mut command =
                windows_registration_command(&script, &data, script_bytes, data_bytes).unwrap();
            if changed == "script" {
                fs::write(
                    &script,
                    "[Console]::Out.WriteLine('changed script ran');exit 0",
                )
                .unwrap();
            } else if changed == "data" {
                fs::write(&data, br#"{"Name":"changed data read"}"#).unwrap();
            }
            let output = command.output().unwrap();
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);
            if matches!(changed, "neither" | "typographic") {
                assert_eq!(output.status.code(), Some(0), "{stdout} {stderr}");
                assert_eq!(stdout.trim(), "registered fixture");
            } else {
                assert_eq!(
                    output.status.code(),
                    Some(5),
                    "{changed}: {stdout} {stderr}"
                );
                assert!(stdout.is_empty(), "{stdout}");
                assert!(
                    stderr.contains(&format!("autostart registration {changed} changed")),
                    "{stderr}"
                );
            }
        }
    }
}
