//! Client-side reads: registration credentials, each client's configured
//! wake path, Codex's hook copy, Git Bash, `pseudolife-mcp` on PATH and
//! saved tunnels. File reads only; nothing here touches the network.
use super::pyenv::{
    self, Defer, Env, Res, Text, is_dir, is_file, join, json_loads, path_str, read_text, strip,
    toml_loads,
};
use serde_json::{Map, Value, json};

const SERVER: &str = "pseudolife-memory";
const PLUGIN_ID: &str = "pseudolife-memory@pseudolife-mcp";
const TOKEN_KEYS: [&str; 2] = ["PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE"];
const URL_KEY: &str = "PSEUDOLIFE_MCP_DAEMON_URL";
const OFF: [&str; 4] = ["0", "false", "no", "off"];
const YES: [&str; 4] = ["1", "true", "yes", "on"];

fn claude_registration_path(env: &Env) -> Res<String> {
    Ok(match env.truthy("CLAUDE_CONFIG_DIR")? {
        Some(dir) => join(&path_str(&dir)?, ".claude.json"),
        None => join(&pyenv::home(env)?, ".claude.json"),
    })
}

fn claude_config_dir(env: &Env) -> Res<String> {
    Ok(match env.truthy("CLAUDE_CONFIG_DIR")? {
        Some(dir) => path_str(&dir)?,
        None => join(&pyenv::home(env)?, ".claude"),
    })
}

fn codex_config_path(env: &Env) -> Res<String> {
    let home = match env.truthy("CODEX_HOME")? {
        Some(dir) => path_str(&dir)?,
        None => join(&pyenv::home(env)?, ".codex"),
    };
    Ok(join(&home, "config.toml"))
}

/// A TOML value as `str(value)` would print it, for the types doctor reads.
fn toml_text(value: &toml::Value) -> Res<String> {
    match value {
        toml::Value::String(text) => Ok(text.clone()),
        toml::Value::Integer(number) => Ok(number.to_string()),
        toml::Value::Boolean(flag) => Ok(if *flag { "True" } else { "False" }.into()),
        _ => Err(Defer),
    }
}

/// Python truthiness of a TOML value of the types [`toml_text`] admits.
fn toml_truthy(value: &toml::Value) -> Res<bool> {
    match value {
        toml::Value::String(text) => Ok(!text.is_empty()),
        toml::Value::Integer(number) => Ok(*number != 0),
        toml::Value::Boolean(flag) => Ok(*flag),
        _ => Err(Defer),
    }
}

/// Environment overrides taken from a registration, in insertion order.
pub(super) type Overrides = Vec<(String, String)>;

/// `registration_credentials(os.environ)`: the overrides to apply and the
/// credential source label.
pub(super) fn registration_credentials(env: &Env) -> Res<(Overrides, Option<String>)> {
    if env.truthy("PSEUDOLIFE_MCP_TOKEN")?.is_some()
        || env.var("PSEUDOLIFE_MCP_TOKEN_FILE")?.is_some()
    {
        return Ok((Vec::new(), Some("environment".into())));
    }
    // Claude Code first: ~/.claude.json's mcpServers.
    let claude = claude_registration_path(env)?;
    if let Text::Ok(text) = read_text(&claude, true)
        && let Some(data) = json_loads(&text)?
        && let Some(block) = data
            .get("mcpServers")
            .and_then(Value::as_object)
            .and_then(|servers| servers.get(SERVER))
            .and_then(Value::as_object)
            .and_then(|server| server.get("env"))
            .and_then(Value::as_object)
    {
        let text_of = |key: &str| {
            block
                .get(key)
                .and_then(Value::as_str)
                .filter(|v| !v.is_empty())
                .map(str::to_owned)
        };
        if let Some(found) = credential_overrides(env, text_of)? {
            return Ok((found, Some(format!("Claude Code registration ({claude})"))));
        }
    }
    // Then Codex: config.toml's mcp_servers.
    let codex = codex_config_path(env)?;
    if let Text::Ok(text) = read_text(&codex, true)
        && let Some(data) = toml_loads(&text)?
        && let Some(block) = data
            .get("mcp_servers")
            .and_then(toml::Value::as_table)
            .and_then(|servers| servers.get(SERVER))
            .and_then(toml::Value::as_table)
            .and_then(|server| server.get("env"))
            .and_then(toml::Value::as_table)
    {
        let text_of = |key: &str| {
            block
                .get(key)
                .and_then(toml::Value::as_str)
                .filter(|v| !v.is_empty())
                .map(str::to_owned)
        };
        if let Some(found) = credential_overrides(env, text_of)? {
            return Ok((found, Some(format!("Codex registration ({codex})"))));
        }
    }
    Ok((Vec::new(), None))
}

fn credential_overrides(
    env: &Env,
    text_of: impl Fn(&str) -> Option<String>,
) -> Res<Option<Vec<(String, String)>>> {
    let mut found: Vec<(String, String)> = TOKEN_KEYS
        .iter()
        .filter_map(|key| text_of(key).map(|value| ((*key).to_owned(), value)))
        .collect();
    if found.is_empty() {
        return Ok(None);
    }
    if env.truthy(URL_KEY)?.is_none()
        && let Some(url) = text_of(URL_KEY)
    {
        found.push((URL_KEY.to_owned(), url));
    }
    Ok(Some(found))
}

/// `_wake_state` with `effective` already resolved.
fn wake_state(health_enabled: Option<bool>, master: &str, flag: &str, value: &str) -> String {
    if health_enabled == Some(false) {
        return "off (coordination disabled on the daemon)".into();
    }
    let lower = |text: &str| text.is_ascii().then(|| text.to_ascii_lowercase());
    if !master.is_empty() {
        let low = lower(master);
        if !low.as_deref().is_some_and(|l| YES.contains(&l)) {
            let shown = low
                .filter(|l| OFF.contains(&l.as_str()))
                .unwrap_or_else(|| "invalid".into());
            return format!("off (PSEUDOLIFE_AGENT_COORDINATION={shown})");
        }
    }
    if !value.is_empty() {
        let low = lower(value);
        let off = low.as_deref().is_some_and(|l| OFF.contains(&l));
        if !low.as_deref().is_some_and(|l| YES.contains(&l))
            && (flag != "PSEUDOLIFE_AGENT_WAKE_HOOK" || off)
        {
            let shown = if off {
                low.unwrap_or_default()
            } else {
                "invalid".into()
            };
            return format!("off ({flag}={shown})");
        }
    }
    "on".into()
}

/// The claude_code wake inputs, resolved before the daemon answers.
pub(super) enum ClaudeWake {
    Fixed(Value),
    Hook { master: String, flag: String },
}

impl ClaudeWake {
    pub(super) fn report(&self, enabled: Option<bool>) -> Value {
        match self {
            Self::Fixed(value) => value.clone(),
            Self::Hook { master, flag } => json!({"registered": true,
                "stop_hook": wake_state(enabled, master, "PSEUDOLIFE_AGENT_WAKE_HOOK", flag)}),
        }
    }
}

/// Python's `str(value or "").strip()` for a JSON settings value.
fn json_effective(value: Option<&Value>) -> Res<Option<String>> {
    Ok(match value {
        None => None,
        Some(Value::Null) | Some(Value::Bool(false)) => Some(String::new()),
        Some(Value::Bool(true)) => Some("True".into()),
        Some(Value::String(text)) => Some(strip(text).to_owned()),
        Some(Value::Number(number)) if super::pyjson::is_int_token(&number.to_string()) => {
            let text = super::pyjson::python_int(&number.to_string());
            Some(if text == "0" { String::new() } else { text })
        }
        Some(Value::Array(items)) if items.is_empty() => Some(String::new()),
        Some(Value::Object(items)) if items.is_empty() => Some(String::new()),
        Some(_) => return Err(Defer),
    })
}

/// `_claude_code_wake`'s file reads.
pub(super) fn claude_code_wake(env: &Env) -> Res<ClaudeWake> {
    let registration = claude_registration_path(env)?;
    let config = claude_config_dir(env)?;
    let installed = join(&join(&config, "plugins"), "installed_plugins.json");
    let unreadable =
        || ClaudeWake::Fixed(json!({"registered": "unknown (unreadable ~/.claude.json)"}));
    let mut registered = false;
    let mut plugin_installed = false;
    if is_file(&registration)? {
        let Text::Ok(text) = read_text(&registration, false) else {
            return Ok(unreadable());
        };
        if json_loads(&text)?.is_none() {
            return Ok(unreadable());
        }
        registered = text.contains(SERVER);
    }
    if is_file(&installed)? {
        let Text::Ok(text) = read_text(&installed, false) else {
            return Ok(unreadable());
        };
        let Some(record) = json_loads(&text)? else {
            return Ok(unreadable());
        };
        let plugins = match &record {
            Value::Object(map) => map.get("plugins").unwrap_or(&record),
            _ => &Value::Null,
        };
        plugin_installed = match plugins {
            Value::Object(map) => map.get(PLUGIN_ID).is_some_and(super::truthy),
            _ => false,
        };
    }
    if !(registered || plugin_installed) {
        return Ok(ClaudeWake::Fixed(json!({"registered": false})));
    }
    let settings = join(&config, "settings.json");
    let mut data = Map::new();
    if is_file(&settings)? {
        let parsed = match read_text(&settings, false) {
            Text::Ok(text) => json_loads(&text)?,
            _ => None,
        };
        let Some(loaded) = parsed else {
            return Ok(ClaudeWake::Fixed(
                json!({"registered": true, "stop_hook": "unknown (unreadable settings.json)"}),
            ));
        };
        if let Value::Object(map) = loaded {
            data = map;
        }
    }
    let block = data
        .get("env")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if !plugin_installed {
        return Ok(ClaudeWake::Fixed(
            json!({"registered": true, "stop_hook": "off (plugin not installed)"}),
        ));
    }
    if let Some(Value::Object(enabled)) = data.get("enabledPlugins")
        && enabled.get(PLUGIN_ID) == Some(&Value::Bool(false))
    {
        return Ok(ClaudeWake::Fixed(
            json!({"registered": true, "stop_hook": "off (plugin disabled)"}),
        ));
    }
    let effective = |key: &str| -> Res<String> {
        match json_effective(block.get(key))? {
            Some(value) => Ok(value),
            None => Ok(strip(&env.var(key)?.unwrap_or_default()).to_owned()),
        }
    };
    Ok(ClaudeWake::Hook {
        master: effective("PSEUDOLIFE_AGENT_COORDINATION")?,
        flag: effective("PSEUDOLIFE_AGENT_WAKE_HOOK")?,
    })
}

/// The codex wake inputs, resolved before the daemon answers.
pub(super) enum CodexWake {
    Fixed(Value),
    Doorbell {
        master: String,
        flag: String,
        writer: String,
        bearer: bool,
        command: bool,
    },
}

impl CodexWake {
    pub(super) fn report(&self, enabled: Option<bool>) -> Value {
        match self {
            Self::Fixed(value) => value.clone(),
            Self::Doorbell {
                master,
                flag,
                writer,
                bearer,
                command,
            } => {
                let mut state = wake_state(enabled, master, "PSEUDOLIFE_CODEX_DOORBELL", flag);
                let writer_codex = writer.is_ascii() && writer.eq_ignore_ascii_case("codex");
                if state == "on" && !writer_codex {
                    state = "off (PSEUDOLIFE_WRITER_ID is not codex)".into();
                }
                if state == "on" && !bearer {
                    state = "off (no bearer token)".into();
                }
                if state == "on" && !command {
                    state = "off (no codex CLI)".into();
                }
                json!({"registered": true, "doorbell": state})
            }
        }
    }
}

/// `_codex_wake`'s file reads and the Codex CLI lookup.
pub(super) fn codex_wake(env: &Env) -> Res<CodexWake> {
    let config = codex_config_path(env)?;
    if !is_file(&config)? {
        return Ok(CodexWake::Fixed(json!({"registered": false})));
    }
    let unreadable = || CodexWake::Fixed(json!({"registered": "unknown (unreadable config.toml)"}));
    let Text::Ok(text) = read_text(&config, false) else {
        return Ok(unreadable());
    };
    let Some(data) = toml_loads(&text)? else {
        return Ok(unreadable());
    };
    let server = match data.get("mcp_servers") {
        None => None,
        Some(toml::Value::Table(servers)) => servers.get(SERVER),
        // dict.get on a non-dict: _wake_report names the exception.
        Some(_) => {
            return Ok(CodexWake::Fixed(
                json!({"registered": "unknown (AttributeError)"}),
            ));
        }
    };
    let Some(toml::Value::Table(server)) = server else {
        return Ok(CodexWake::Fixed(json!({"registered": false})));
    };
    let block = match server.get("env") {
        Some(toml::Value::Table(block)) => block.clone(),
        _ => toml::Table::new(),
    };
    let forwarded: Vec<&str> = match server.get("env_vars") {
        Some(toml::Value::Array(items)) => items.iter().filter_map(toml::Value::as_str).collect(),
        _ => Vec::new(),
    };
    let effective = |key: &str| -> Res<String> {
        if let Some(value) = block.get(key) {
            return Ok(if toml_truthy(value)? {
                strip(&toml_text(value)?).to_owned()
            } else {
                String::new()
            });
        }
        if forwarded.contains(&key) {
            return Ok(strip(&env.var(key)?.unwrap_or_default()).to_owned());
        }
        Ok(String::new())
    };
    let bearer = !effective("PSEUDOLIFE_MCP_TOKEN")?.is_empty()
        || !effective("PSEUDOLIFE_MCP_TOKEN_FILE")?.is_empty();
    Ok(CodexWake::Doorbell {
        master: effective("PSEUDOLIFE_AGENT_COORDINATION")?,
        flag: effective("PSEUDOLIFE_CODEX_DOORBELL")?,
        writer: effective("PSEUDOLIFE_WRITER_ID")?,
        bearer,
        command: codex_command(env, &block)?,
    })
}

/// Whether `resolve_codex_command({**os.environ, **str(env)})` finds a CLI.
fn codex_command(env: &Env, block: &toml::Table) -> Res<bool> {
    // os.environ's keys are upper-case on Windows; the registration's env
    // table keeps its own spelling and only an exact key overrides.
    let lookup = |key: &str| -> Res<Option<String>> {
        if let Some(value) = block.get(key) {
            return toml_text(value).map(Some);
        }
        if pyenv::WINDOWS && block.keys().any(|name| name.eq_ignore_ascii_case(key)) {
            return Err(Defer);
        }
        env.var(key)
    };
    let explicit = lookup("PSEUDOLIFE_CODEX_BIN")?.unwrap_or_default();
    let explicit = strip(&explicit);
    if !explicit.is_empty() {
        if explicit.starts_with('~') {
            return Err(Defer);
        }
        let path = std::path::Path::new(explicit);
        let absolute = if pyenv::WINDOWS {
            let p = path_str(explicit)?;
            p.as_bytes().get(1) == Some(&b':') && p.as_bytes().get(2) == Some(&b'\\')
        } else {
            path.is_absolute()
        };
        return Ok(absolute && is_file(explicit)?);
    }
    let names: Vec<String> = if pyenv::WINDOWS {
        let pathext = lookup("PATHEXT")?
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD".into());
        if !pathext.is_ascii() || pathext.contains(['\\', '/', ':']) {
            return Err(Defer);
        }
        pathext
            .split(';')
            .filter(|ext| {
                [".com", ".exe", ".bat", ".cmd"].contains(&ext.to_ascii_lowercase().as_str())
            })
            .map(|ext| format!("codex{ext}"))
            .collect()
    } else {
        vec!["codex".into()]
    };
    for directory in lookup("PATH")?
        .unwrap_or_default()
        .split(if pyenv::WINDOWS { ';' } else { ':' })
    {
        let directory = strip(directory).trim_matches('"');
        if directory.is_empty() {
            continue;
        }
        let absolute = if pyenv::WINDOWS {
            let b = directory.as_bytes();
            if directory.replace('/', "\\").starts_with("\\\\") {
                return Err(Defer);
            }
            b.len() >= 3 && b[1] == b':' && (b[2] == b'\\' || b[2] == b'/')
        } else {
            directory.starts_with('/')
        };
        if !absolute {
            continue;
        }
        for name in &names {
            let candidate = std::path::Path::new(directory).join(name);
            let candidate = candidate.to_str().ok_or(Defer)?;
            if is_file(candidate)? {
                #[cfg(unix)]
                if rustix::fs::access(candidate, rustix::fs::Access::EXEC_OK).is_err() {
                    continue;
                }
                return Ok(true);
            }
        }
    }
    if pyenv::WINDOWS {
        let local = lookup("LOCALAPPDATA")?.unwrap_or_default();
        let local = strip(&local).trim_matches('"');
        let b = local.as_bytes();
        if !local.is_empty() && b.len() >= 3 && b[1] == b':' && (b[2] == b'\\' || b[2] == b'/') {
            let bin = std::path::Path::new(local)
                .join("OpenAI")
                .join("Codex")
                .join("bin");
            if let Ok(entries) = std::fs::read_dir(&bin) {
                for entry in entries {
                    let entry = entry.map_err(|_| Defer)?;
                    let candidate = entry.path().join("codex.exe");
                    if candidate.is_file() {
                        return Ok(true);
                    }
                }
            }
        }
    }
    Ok(false)
}

/// What `check_codex_hooks(None, daemon_digest)` will answer, decided
/// before the daemon's digest is known.
pub(super) enum CodexHooks {
    Line(String),
    /// The plugin copy: `unknown` without a digest, else beyond this port.
    Plugin,
}

pub(super) fn codex_hooks(env: &Env) -> Res<CodexHooks> {
    let codex_home = match env.truthy("CODEX_HOME")? {
        Some(dir) => {
            if dir.starts_with('~') {
                return Err(Defer);
            }
            path_str(&dir)?
        }
        None => join(&pyenv::user_home(env)?, ".codex"),
    };
    let hooks_root = join(&join(&codex_home, "pseudolife"), "hooks");
    if is_dir(&hooks_root)? {
        return Ok(CodexHooks::Line("bundle-present".into()));
    }
    let config_text = match read_text(&join(&codex_home, "config.toml"), false) {
        Text::Ok(text) => text,
        Text::Unreadable => String::new(),
        Text::Undecodable => return Ok(CodexHooks::Line("unknown (UnicodeDecodeError)".into())),
    };
    if !config_text.contains(&format!("\"{PLUGIN_ID}\"")) {
        return Ok(CodexHooks::Line("not-configured".into()));
    }
    Ok(CodexHooks::Plugin)
}

/// `git_bash_report(os.environ)` (Windows only).
pub(super) fn git_bash_report(env: &Env) -> Res<Vec<(&'static str, Value)>> {
    // CLAUDE_CODE_GIT_BASH_PATH from the Claude Code settings env block wins.
    let base = match env.truthy("CLAUDE_CONFIG_DIR")? {
        Some(dir) => path_str(&dir)?,
        None => join(&pyenv::home(env)?, ".claude"),
    };
    let mut configured = None;
    if let Text::Ok(text) = read_text(&join(&base, "settings.json"), true)
        && let Some(Value::Object(settings)) = json_loads(&text)?
        && let Some(Value::Object(block)) = settings.get("env")
        && let Some(Value::String(value)) = block.get("CLAUDE_CODE_GIT_BASH_PATH")
        && !value.is_empty()
    {
        configured = Some(value.clone());
    }
    let configured = match configured {
        Some(value) => Some(value),
        None => env.truthy("CLAUDE_CODE_GIT_BASH_PATH")?,
    };
    let mut git_bash = None;
    if let Some(value) = &configured {
        let normalized = path_str(value)?;
        let file = pyenv::name(&normalized).to_ascii_lowercase();
        if pyenv::name(&normalized).is_ascii()
            && ["bash.exe", "sh.exe", "bash", "sh"].contains(&file.as_str())
            && is_file(value)?
        {
            git_bash = Some(value.clone());
        }
    }
    if git_bash.is_none() {
        for candidate in [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ] {
            if is_file(candidate)? {
                git_bash = Some(candidate.to_owned());
                break;
            }
        }
    }
    if git_bash.is_none()
        && let Some(git) = pyenv::which("git", env)?
    {
        let normalized = path_str(&git)?;
        let candidate = join(
            &join(&pyenv::parent(&pyenv::parent(&normalized)), "bin"),
            "bash.exe",
        );
        if is_file(&candidate)? {
            git_bash = Some(candidate);
        }
    }
    let bash_on_path = pyenv::which("bash", env)?;
    let launcher = match &bash_on_path {
        Some(bash) => {
            let parent = pyenv::parent(&path_str(bash)?);
            let dir = pyenv::name(&parent);
            dir.is_ascii()
                && ["system32", "windowsapps"].contains(&dir.to_ascii_lowercase().as_str())
        }
        None => false,
    };
    let mut report = vec![
        ("git_bash", json!(git_bash)),
        ("bash_on_path", json!(bash_on_path)),
        ("bash_on_path_is_wsl_launcher", json!(launcher)),
    ];
    match (&git_bash, launcher) {
        (None, _) => report.push((
            "git_bash_recovery",
            json!(
                "Claude Code runs the plugin's hook commands through Git Bash and found \
none: install Git for Windows (the default location, or put its cmd \
directory on PATH), or set CLAUDE_CODE_GIT_BASH_PATH to its bin\\bash.exe \
in the env block of ~/.claude/settings.json; then restart Claude Code. \
Codex hooks run in PowerShell and are unaffected."
            ),
        )),
        (Some(git_bash), true) => report.push((
            "git_bash_recovery",
            json!(format!(
                "`bash` on PATH is the WSL launcher ({}). Claude Code still \
runs the plugin hooks through {git_bash}, but a bare `bash` in a \
terminal or another tool opens WSL, which cannot read the plugin's \
Windows paths: put Git's bin directory ahead of System32 on PATH, or \
call that bash.exe by its full path.",
                bash_on_path.clone().unwrap_or_default()
            )),
        )),
        _ => {}
    }
    Ok(report)
}

/// `path_resolution()`.
pub(super) fn path_resolution(env: &Env) -> Res<Value> {
    let found = pyenv::which("pseudolife-mcp", env)?;
    let launcher = match (
        env.truthy("PSEUDOLIFE_SHIM_RUNTIMES")?,
        env.truthy("PSEUDOLIFE_SHIM_LAUNCHER")?,
    ) {
        (Some(_), Some(launcher)) => {
            let normalized = path_str(&launcher)?;
            let file = pyenv::name(&normalized);
            let suffix = match file.rfind('.') {
                Some(index) if index > 0 && index + 1 < file.len() => &file[index..],
                _ => "",
            };
            if !suffix.is_ascii() {
                return Err(Defer);
            }
            (suffix.eq_ignore_ascii_case(".exe") == pyenv::WINDOWS).then_some(normalized)
        }
        _ => Some(if pyenv::WINDOWS {
            let local = match env.truthy("LOCALAPPDATA")? {
                Some(local) => path_str(&local)?,
                None => join(&join(&pyenv::user_home(env)?, "AppData"), "Local"),
            };
            join(
                &join(&join(&local, "pseudolife-mcp"), "bin"),
                "pseudolife-mcp.exe",
            )
        } else {
            let data = match env.truthy("XDG_DATA_HOME")? {
                Some(data) => path_str(&data)?,
                None => join(&join(&pyenv::user_home(env)?, ".local"), "share"),
            };
            join(
                &join(&join(&data, "pseudolife-mcp"), "bin"),
                "pseudolife-mcp",
            )
        }),
    };
    let launcher = match launcher {
        Some(path) if is_file(&path)? => Some(path),
        _ => None,
    };
    let mut report = Map::new();
    report.insert("on_path".into(), json!(found));
    report.insert("launcher".into(), json!(launcher));
    if let Some(launcher) = &launcher {
        let same = match &found {
            Some(found) => pyenv::real_normcase(found)? == pyenv::real_normcase(launcher)?,
            None => false,
        };
        if !same {
            report.insert("warning".into(), json!(format!(
                "`pseudolife-mcp` on PATH is {}, not the launcher {launcher}, so a terminal runs \
another install. Run \"{launcher}\" update --clients-only to point the name at the launcher, then \
open a new terminal",
                found.as_deref().unwrap_or("not found")
            )));
        }
    }
    Ok(Value::Object(report))
}

/// Saved tunnels exist: their diagnostics are beyond this port.
pub(super) fn tunnels_present(env: &Env) -> Res<bool> {
    pyenv::lexists(&join(
        &join(&pyenv::home(env)?, ".pseudolife-mcp"),
        "tunnel",
    ))
}
