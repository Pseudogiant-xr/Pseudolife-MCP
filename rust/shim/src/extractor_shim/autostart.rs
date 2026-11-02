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

pub(super) fn install(args: &[OsString], remove: bool) -> Result<(), String> {
    if args.is_empty() || args.iter().any(|a| a == "--help") {
        println!(
            "usage: pseudolife-mcp extractor-shim install-autostart claude|codex [--env-file PATH] [--repo PATH] [--name NAME] [--native-exe PATH] [--replace-sibling] [--startup-timeout SECONDS] [--task-logon-type Interactive|S4U] [shim settings]\nDefault config: ~/.pseudolife-mcp/extractor-shims.env. Settings include --cli, --model, --host, --port, --prompt-file, --health-ttl and --log. An explicit --repo reads that checkout's ops/.env. Registration requires systemd --user on Linux, or an elevated PowerShell on Windows. S4U is an explicit local-only task principal for unattended hosts."
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
    if name.is_empty() || name.chars().any(|c| c.is_control() || "/\\".contains(c)) {
        return Err("invalid registration name".into());
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
            Err(e) => return Err(e.to_string()),
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
        return Err(format!("native launcher not found: {}", native.display()));
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
        // Pin the installer's resolved directory: systemd and Task Scheduler
        // need not inherit its XDG_CACHE_HOME / LOCALAPPDATA environment.
        serve_args.extend([
            "--receipt-dir".into(),
            receipt_dir.to_string_lossy().into_owned(),
        ]);
    }
    let status = if cfg!(windows) {
        let script = scratch.0.join("register.ps1");
        let data = scratch.0.join("registration.json");
        fs::write(&script,include_str!("autostart.ps1")).map_err(|e|e.to_string())?;
        fs::write(&data,serde_json::to_vec(&json!({"Native":native,"OwnerExe":owner_exe,"Kind":kind,"EnvFile":env_file,"Name":name,"ReplaceSibling":replace,"Remove":remove,"Port":port,"Host":host,"Timeout":wait,"LogonType":logon,"ConfigArgs":config_args,"ServeArgs":serve_args})).expect("registration JSON")).map_err(|e|e.to_string())?;
        Command::new("powershell.exe").args(["-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File"]).arg(script).arg(data).status()
    } else if cfg!(target_os="linux") {
        let script = scratch.0.join("register.sh");
        fs::write(&script,include_str!("autostart.sh")).map_err(|e|e.to_string())?;
        Command::new("bash").arg(script).arg(if remove {"remove"} else {"install"}).arg(&owner_exe).arg(&native).arg(kind).arg(&env_file).arg(name).arg(if replace {"yes"}else{"no"}).arg(port.to_string()).arg(host).arg(wait.to_string()).arg(serve_args.len().to_string()).args(serve_args).args(config_args).status()
    } else { return Err("native autostart requires Windows Task Scheduler or Linux systemd --user".into()); }
    .map_err(|e|format!("cannot run the platform registration helper: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "autostart registration failed ({status}); see the diagnostic above"
        ))
    }
}
