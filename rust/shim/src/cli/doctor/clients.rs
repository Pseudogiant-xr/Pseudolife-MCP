//! Client-side reads: registration credentials, each client's configured
//! wake path, Codex's hook copy, Git Bash, `pseudolife-mcp` on PATH and
//! saved tunnels. File reads only; nothing here touches the network.
use super::pyenv::{
    self, Env, Finding, Res, is_dir, is_file, join, json_loads, path_str, strip, toml_loads,
};
use serde_json::{Map, Value, json};

const SERVER: &str = "pseudolife-memory";
const PLUGIN_ID: &str = "pseudolife-memory@pseudolife-mcp";
const TOKEN_KEYS: [&str; 2] = ["PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE"];
const URL_KEY: &str = "PSEUDOLIFE_MCP_DAEMON_URL";
const OFF: [&str; 4] = ["0", "false", "no", "off"];
const YES: [&str; 4] = ["1", "true", "yes", "on"];

/// An absent file is distinct from one the diagnostic could not inspect.
fn read_optional(path: &str, label: &str) -> Res<Option<String>> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
            ) =>
        {
            return Ok(None);
        }
        Err(error) => return Err(Finding::io(&error).at(label)),
    };
    String::from_utf8(bytes)
        .map(|text| Some(text.replace("\r\n", "\n").replace('\r', "\n")))
        .map_err(|_| Finding::new("file is not UTF-8; save the file as UTF-8").at(label))
}

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
        Some(dir) => pyenv::expand_home(&dir, env).map_err(|cause| cause.at("CODEX_HOME"))?,
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
        _ => Err(Finding::new(
            "setting must be text, an integer, or a boolean; correct the setting type",
        )),
    }
}

/// Python truthiness of a TOML value of the types [`toml_text`] admits.
fn toml_truthy(value: &toml::Value) -> Res<bool> {
    match value {
        toml::Value::String(text) => Ok(!text.is_empty()),
        toml::Value::Integer(number) => Ok(*number != 0),
        toml::Value::Boolean(flag) => Ok(*flag),
        _ => Err(Finding::new(
            "setting must be text, an integer, or a boolean; correct the setting type",
        )),
    }
}

/// Environment overrides taken from a registration, in insertion order.
pub(super) type Overrides = Vec<(String, String)>;

/// `registration_credentials(os.environ)`: the overrides to apply and the
/// credential source label.
pub(super) fn registration_credentials(
    env: &Env,
    findings: &mut Vec<Finding>,
) -> Res<(Overrides, Option<String>)> {
    if env.var("PSEUDOLIFE_MCP_TOKEN")?.is_some() || env.var("PSEUDOLIFE_MCP_TOKEN_FILE")?.is_some()
    {
        return Ok((Vec::new(), Some("environment".into())));
    }
    // Claude Code first: ~/.claude.json's mcpServers.
    let claude_result = (|| -> Res<Option<(Overrides, String)>> {
        let claude = claude_registration_path(env)?;
        if let Some(text) = read_optional(&claude, "Claude Code registration (~/.claude.json)")?
            && let Some(data) = json_loads(&text)
                .map_err(|cause| cause.at("Claude Code registration (~/.claude.json)"))?
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
                return Ok(Some((
                    found,
                    format!("Claude Code registration ({claude})"),
                )));
            }
        }
        Ok(None)
    })();
    match claude_result {
        Ok(Some((overrides, source))) => return Ok((overrides, Some(source))),
        Ok(None) => {}
        Err(cause) => findings.push(cause),
    }
    // Then Codex: config.toml's mcp_servers.
    let codex = codex_config_path(env)?;
    if let Some(text) = read_optional(&codex, "Codex config.toml")?
        && let Some(data) = toml_loads(&text).map_err(|cause| cause.at("Codex config.toml"))?
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
        Some(_) => {
            return Err(Finding::new(
                "setting must be text, an integer, or a boolean; correct the setting type",
            ));
        }
    })
}

/// `_claude_code_wake`'s file reads.
pub(super) fn claude_code_wake(env: &Env) -> Res<ClaudeWake> {
    let registration = claude_registration_path(env)?;
    let config = claude_config_dir(env)?;
    let installed = join(&join(&config, "plugins"), "installed_plugins.json");
    let mut findings = Vec::new();
    let mut load =
        |path: &str, label: &str| match read_optional(path, label).and_then(|text| match text {
            Some(text) => json_loads(&text).map_err(|cause| cause.at(label)),
            None => Ok(None),
        }) {
            Ok(data) => data,
            Err(cause) => {
                findings.push(cause.to_string());
                None
            }
        };
    let registration = load(&registration, "Claude Code registration (~/.claude.json)");
    let installed = load(&installed, "Claude Code installed_plugins.json");
    let registered = registration
        .as_ref()
        .is_some_and(|data| data.to_string().contains(SERVER));
    let mut plugin_installed = false;
    if let Some(record) = installed {
        let plugins = match &record {
            Value::Object(map) => map.get("plugins").unwrap_or(&record),
            _ => &Value::Null,
        };
        plugin_installed = match plugins {
            Value::Object(map) => map.get(PLUGIN_ID).is_some_and(super::truthy),
            _ => false,
        };
    }
    let registration_unread = !findings.is_empty();
    let settings = join(&config, "settings.json");
    let parsed = match read_optional(&settings, "Claude Code settings.json").and_then(|text| {
        text.map(|text| json_loads(&text).map_err(|cause| cause.at("Claude Code settings.json")))
            .transpose()
    }) {
        Ok(value) => value.flatten(),
        Err(cause) => {
            findings.push(cause.to_string());
            None
        }
    };
    if !findings.is_empty() {
        let registered = if registered || plugin_installed {
            json!(true)
        } else if registration_unread {
            json!("unknown (registration could not be read)")
        } else {
            json!(false)
        };
        return Ok(ClaudeWake::Fixed(json!({"registered": registered,
            "stop_hook": format!("could not read Claude Code wake configuration: {}", findings.join("; "))})));
    }
    if !(registered || plugin_installed) {
        return Ok(ClaudeWake::Fixed(json!({"registered": false})));
    }
    let data = parsed
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default();
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
        match json_effective(block.get(key)).map_err(|cause| cause.at(key))? {
            Some(value) => Ok(value),
            None => Ok(strip(&env.var(key)?.unwrap_or_default()).to_owned()),
        }
    };
    Ok(
        match (
            effective("PSEUDOLIFE_AGENT_COORDINATION"),
            effective("PSEUDOLIFE_AGENT_WAKE_HOOK"),
        ) {
            (Ok(master), Ok(flag)) => ClaudeWake::Hook { master, flag },
            (Err(cause), _) | (_, Err(cause)) => ClaudeWake::Fixed(json!({"registered": true,
            "stop_hook": super::unread("Claude Code settings.json or its environment override", &cause)})),
        },
    )
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
    let Some(text) = read_optional(&config, "Codex config.toml")? else {
        return Ok(CodexWake::Fixed(json!({"registered": false})));
    };
    let Some(data) = toml_loads(&text).map_err(|cause| cause.at("Codex config.toml"))? else {
        return Ok(CodexWake::Fixed(json!({"registered": false})));
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
            return Ok(if toml_truthy(value).map_err(|cause| cause.at(key))? {
                strip(&toml_text(value).map_err(|cause| cause.at(key))?).to_owned()
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
    // Windows environment names are case-insensitive; ambiguous duplicates
    // cannot describe one effective setting.
    let lookup = |key: &str| -> Res<Option<String>> {
        if pyenv::WINDOWS {
            let mut matches = block
                .iter()
                .filter(|(name, _)| name.eq_ignore_ascii_case(key));
            if let Some((_, value)) = matches.next() {
                if matches.next().is_some() {
                    return Err(Finding::new("setting has duplicate case-insensitive names; keep one uppercase environment variable name").at(key));
                }
                return toml_text(value).map(Some).map_err(|cause| cause.at(key));
            }
        }
        if let Some(value) = block.get(key) {
            return toml_text(value).map(Some).map_err(|cause| cause.at(key));
        }
        env.var(key)
    };
    let explicit = lookup("PSEUDOLIFE_CODEX_BIN")?.unwrap_or_default();
    let explicit = strip(&explicit);
    if !explicit.is_empty() {
        let explicit =
            pyenv::expand_home(explicit, env).map_err(|cause| cause.at("PSEUDOLIFE_CODEX_BIN"))?;
        let path = std::path::Path::new(&explicit);
        let absolute = path.is_absolute();
        return Ok(absolute && is_file(&explicit)?);
    }
    let names: Vec<String> = if pyenv::WINDOWS {
        let pathext = lookup("PATHEXT")?
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD".into());
        if pathext.contains(['\\', '/', ':']) {
            return Err(Finding::new(
                "PATHEXT contains a path separator; use semicolon-separated file extensions",
            ));
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
        let absolute = std::path::Path::new(directory).is_absolute();
        if !absolute {
            continue;
        }
        for name in &names {
            let candidate = std::path::Path::new(directory).join(name);
            let candidate = candidate.to_str().ok_or_else(|| {
                Finding::new("PATH candidate is not Unicode; use a Unicode executable path")
            })?;
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
                    let entry = entry
                        .map_err(|error| Finding::io(&error).at("LOCALAPPDATA/OpenAI/Codex/bin"))?;
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
        Some(dir) => pyenv::expand_home(&dir, env).map_err(|cause| cause.at("CODEX_HOME"))?,
        None => join(&pyenv::user_home(env)?, ".codex"),
    };
    let hooks_root = join(&join(&codex_home, "pseudolife"), "hooks");
    if is_dir(&hooks_root)? {
        return Ok(CodexHooks::Line("bundle-present".into()));
    }
    let config_text =
        read_optional(&join(&codex_home, "config.toml"), "Codex config.toml")?.unwrap_or_default();
    if !config_text.contains(&format!("\"{PLUGIN_ID}\"")) {
        return Ok(CodexHooks::Line("not-configured".into()));
    }
    if env
        .truthy("CODEX_HOME")?
        .is_some_and(|home| home.starts_with('~'))
    {
        return Err(Finding::new(
            "CODEX_HOME uses home expansion, which plugin hook verification cannot resolve; set CODEX_HOME to the expanded absolute directory",
        ));
    }
    Ok(CodexHooks::Plugin)
}

/// `git_bash_report(os.environ)` (Windows only).
pub(super) fn git_bash_report(env: &Env, token: Option<&str>) -> Res<Vec<(&'static str, Value)>> {
    // CLAUDE_CODE_GIT_BASH_PATH from the Claude Code settings env block wins.
    let base = match env.truthy("CLAUDE_CONFIG_DIR")? {
        Some(dir) => path_str(&dir)?,
        None => join(&pyenv::home(env)?, ".claude"),
    };
    let settings = read_optional(&join(&base, "settings.json"), "Claude Code settings.json")
        .and_then(|text| {
            text.map(|text| {
                json_loads(&text).map_err(|cause| cause.at("Claude Code settings.json"))
            })
            .transpose()
        });
    let (settings, settings_problem) = match settings {
        Ok(value) => (value.flatten(), None),
        Err(cause) => (
            None,
            Some(super::unread("Claude Code settings.json", &cause)),
        ),
    };
    let configured = settings
        .as_ref()
        .and_then(|settings| settings.get("env"))
        .and_then(|block| block.get("CLAUDE_CODE_GIT_BASH_PATH"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned);
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
    let git_bash = git_bash.map(|path| super::display_path(&path, token));
    let bash_on_path = bash_on_path.map(|path| super::display_path(&path, token));
    let mut report = vec![
        ("git_bash", json!(git_bash)),
        ("bash_on_path", json!(bash_on_path)),
        ("bash_on_path_is_wsl_launcher", json!(launcher)),
    ];
    if let Some(problem) = settings_problem {
        report.push(("git_bash_settings", json!(problem)));
    }
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
pub(super) fn path_resolution(env: &Env, token: Option<&str>) -> Res<Value> {
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
    report.insert(
        "on_path".into(),
        json!(
            found
                .as_deref()
                .map(|path| super::display_path(path, token))
        ),
    );
    report.insert(
        "launcher".into(),
        json!(
            launcher
                .as_deref()
                .map(|path| super::display_path(path, token))
        ),
    );
    if let Some(launcher) = &launcher {
        let same = match &found {
            Some(found) => pyenv::real_normcase(found)? == pyenv::real_normcase(launcher)?,
            None => false,
        };
        if !same {
            let launcher = super::display_path(launcher, token);
            let found = found
                .as_deref()
                .map(|path| super::display_path(path, token));
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

/// Where saved tunnel profiles live (`tunnel_profiles.default_root`).
pub(super) fn tunnel_root(env: &Env) -> Res<String> {
    Ok(join(&join(&pyenv::home(env)?, ".pseudolife-mcp"), "tunnel"))
}

#[cfg(test)]
mod native_tests {
    use super::*;

    #[test]
    fn native_home_tilde_expansion_uses_configured_home() {
        let mut env = Env::default();
        let home = if pyenv::WINDOWS {
            r"C:\absent-w44"
        } else {
            "/absent-w44"
        };
        env.set("HOME", home.into());
        env.set("USERPROFILE", home.into());
        env.set("CODEX_HOME", "~/.codex".into());
        env.set("PSEUDOLIFE_CODEX_BIN", "~/codex".into());
        assert!(codex_hooks(&env).is_ok());
        assert!(!codex_command(&env, &toml::Table::new()).unwrap());
        assert_eq!(
            codex_config_path(&env).unwrap(),
            join(&join(home, ".codex"), "config.toml")
        );
    }

    #[test]
    fn native_launcher_unicode_suffix_is_an_ordinary_path() {
        let mut env = Env::default();
        env.set("PATH", String::new());
        env.set("PSEUDOLIFE_SHIM_RUNTIMES", "fixture".into());
        for path in ["absent-w44.é", "absent-w44.exe", "absent-w44"] {
            env.set("PSEUDOLIFE_SHIM_LAUNCHER", path.into());
            assert!(path_resolution(&env, None).is_ok());
        }
    }

    #[cfg(windows)]
    #[test]
    fn native_windows_lookup_shapes_and_near_misses() {
        let mut env = Env::default();
        env.set("PATH", r"Z:\absent-w44".into());
        env.set("LOCALAPPDATA", String::new());
        let mut block = toml::Table::new();
        block.insert("path".into(), toml::Value::String(r"Z:\absent-w44".into()));
        assert!(!codex_command(&env, &block).unwrap());
        block.insert("PATH".into(), toml::Value::String(r"Z:\absent-w44".into()));
        let text = format!("{:?}", codex_command(&env, &block));
        assert!(
            text.contains("PATH") && text.contains("duplicate"),
            "{text}"
        );
        block.clear();
        env.set("PATHEXT", ".EXE;bad/path".into());
        assert!(format!("{:?}", codex_command(&env, &block)).contains("PATHEXT"));
        env.set("PATHEXT", ".EXE;.é".into());
        assert!(!codex_command(&env, &block).unwrap());
    }

    #[test]
    fn native_codex_home_tilde_finding_names_setting() {
        let mut env = Env::default();
        env.set("CODEX_HOME", "~unknown/codex".into());
        let text = format!("{:?}", codex_hooks(&env).map(|_| ()));
        assert!(
            text.contains("CODEX_HOME") && text.contains("absolute"),
            "{text}"
        );
        env.set("CODEX_HOME", "absent-w44-near-miss".into());
        assert!(codex_hooks(&env).is_ok());
    }

    #[test]
    fn native_setting_types_report_cause_without_values() {
        let mut failed = Vec::new();
        for value in [
            toml::Value::Float(1.5),
            toml::Value::Array(vec![toml::Value::String("w44-secret-value".into())]),
            toml::Value::Table(toml::Table::new()),
            toml::Value::Datetime("1979-05-27T07:32:00Z".parse().unwrap()),
        ] {
            for text in [
                format!("{:?}", toml_text(&value)),
                format!("{:?}", toml_truthy(&value)),
            ] {
                let ok = text.contains("setting") && !text.contains("w44-secret-value");
                eprintln!("TOML setting type {}: {ok}", value.type_str());
                if !ok {
                    failed.push(text);
                }
            }
        }
        for value in [
            json!(1.5),
            json!(["w44-secret-value"]),
            json!({"x":"w44-secret-value"}),
        ] {
            let text = format!("{:?}", json_effective(Some(&value)));
            let ok = text.contains("setting") && !text.contains("w44-secret-value");
            eprintln!("JSON setting type: {ok}");
            if !ok {
                failed.push(text);
            }
        }
        assert!(failed.is_empty(), "{failed:?}");
        assert_eq!(toml_text(&toml::Value::String("on".into())).unwrap(), "on");
        assert_eq!(
            json_effective(Some(&json!("on"))).unwrap(),
            Some("on".into())
        );
    }

    #[test]
    fn native_tilde_setting_names_cause() {
        let mut env = Env::default();
        env.set("PSEUDOLIFE_CODEX_BIN", "~unknown/codex".into());
        let text = format!("{:?}", codex_command(&env, &toml::Table::new()));
        assert!(
            text.contains("PSEUDOLIFE_CODEX_BIN") && text.contains("absolute"),
            "{text}"
        );
        env.set("PSEUDOLIFE_CODEX_BIN", "/absent/codex".into());
        assert!(!codex_command(&env, &toml::Table::new()).unwrap());
    }
}
