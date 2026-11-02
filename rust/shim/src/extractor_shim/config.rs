use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) enum Kind {
    Claude,
    Codex,
}
impl Kind {
    pub fn name(self) -> &'static str {
        if self == Self::Claude {
            "claude"
        } else {
            "codex"
        }
    }
    pub fn prefix(self) -> String {
        format!("PSEUDOLIFE_{}_SHIM_", self.name().to_uppercase())
    }
    pub fn models(self) -> &'static [&'static str] {
        match self {
            Self::Claude => &[
                "claude-opus-5",
                "claude-opus-5-5",
                "claude-sonnet-5-5",
                "claude-sonnet-5",
                "claude-haiku-5-5",
                "claude-haiku-4-5",
                "claude-fable-5-1",
                "claude-fable-5",
            ],
            Self::Codex => &[
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-6-sol",
                "gpt-6-luna",
            ],
        }
    }
    pub fn resolve_model(self, requested: Option<&str>, default: &str) -> String {
        let model = requested
            .filter(|s| match self {
                Self::Claude => s.starts_with("claude-"),
                Self::Codex => s.starts_with("gpt-") || s.starts_with("codex-"),
            })
            .unwrap_or(default);
        model.into()
    }
}

pub(super) fn validate_cli_token(label: &str, value: &str) -> Result<(), String> {
    if value.is_empty()
        || !value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(&c))
    {
        return Err(format!(
            "{label} must contain only ASCII letters, digits, '.', '_', ':', or '-'"
        ));
    }
    Ok(())
}

pub(super) struct Config {
    pub kind: Kind,
    pub cli: PathBuf,
    pub cwd: Option<PathBuf>,
    pub model: String,
    pub host: String,
    pub port: u16,
    pub timeout: Duration,
    pub ttl: Duration,
    pub effort: Option<String>,
    pub system: Option<String>,
    pub emulate: bool,
    pub managed: bool,
    pub log: Option<PathBuf>,
    pub config_hash: Option<String>,
}

const BEGIN: &str = "# >>> pseudolife shim autostart (managed by ops/shim_autostart.py; edit values here, then: python ops/shim_autostart.py restart claude|codex) >>>";
const END: &str = "# <<< pseudolife shim autostart <<<";

fn env_line(line: &str) -> Option<(String, String)> {
    let line = line
        .trim()
        .strip_prefix("export ")
        .unwrap_or(line.trim())
        .trim();
    let (key, value) = line.split_once('=')?;
    let key = key.trim();
    if key.is_empty()
        || !key
            .chars()
            .enumerate()
            .all(|(i, c)| c == '_' || c.is_ascii_alphabetic() || i > 0 && c.is_ascii_digit())
    {
        return None;
    }
    let value = value.trim();
    let v = if value.len() >= 2
        && ((value.starts_with('"') && value.ends_with('"'))
            || (value.starts_with('\'') && value.ends_with('\'')))
    {
        let inner = &value[1..value.len() - 1];
        if value.starts_with('"') {
            inner.replace("\\\"", "\"").replace("\\\\", "\\")
        } else {
            inner.into()
        }
    } else {
        value.split(" #").next().unwrap_or("").trim().into()
    };
    Some((key.into(), v))
}

fn env_file(path: &Path) -> (BTreeMap<String, String>, String) {
    let raw = fs::read(path).unwrap_or_default();
    let values = String::from_utf8_lossy(&raw)
        .lines()
        .filter_map(env_line)
        .collect();
    (values, format!("{:x}", Sha256::digest(&raw)))
}

fn default_host(managed: bool) -> String {
    if managed
        && cfg!(target_os = "linux")
        && let Ok(out) = std::process::Command::new("ip")
            .args(["-4", "addr", "show", "docker0"])
            .output()
    {
        let text = String::from_utf8_lossy(&out.stdout);
        if let Some(ip) = regex::Regex::new(r"inet (\d+\.\d+\.\d+\.\d+)")
            .expect("bridge address")
            .captures(&text)
        {
            return ip[1].into();
        }
    }
    "127.0.0.1".into()
}

fn quote(v: &str) -> String {
    if v.chars()
        .all(|c| c.is_ascii_alphanumeric() || "_./:@%+=~\\-".contains(c))
    {
        v.into()
    } else if !v.contains('\'') {
        format!("'{v}'")
    } else {
        format!("\"{}\"", v.replace('\\', "\\\\").replace('"', "\\\""))
    }
}

fn write_config(
    repo: &Path,
    path: &Path,
    kind: Kind,
    values: &BTreeMap<String, String>,
) -> Result<(), String> {
    let raw = match fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            fs::read_to_string(repo.join("ops/.env.example")).unwrap_or_default()
        }
        Err(e) => return Err(e.to_string()),
    };
    let mut kept = vec![];
    let mut block = BTreeMap::new();
    let mut moved = BTreeMap::new();
    let mut inside = false;
    for line in raw.lines() {
        if line.trim() == BEGIN {
            inside = true;
            continue;
        }
        if line.trim() == END {
            inside = false;
            continue;
        }
        let parsed = env_line(line);
        if inside {
            if let Some((k, v)) = parsed {
                block.insert(k, v);
            }
        } else if let Some((k, v)) = parsed.filter(|(k, _)| k.starts_with(&kind.prefix())) {
            moved.insert(k, v);
        } else {
            kept.push(line.to_owned());
        }
    }
    block.extend(moved);
    for (k, v) in values {
        if !v.is_empty() {
            block.insert(
                format!("{}{}", kind.prefix(), k.to_uppercase().replace('-', "_")),
                v.clone(),
            );
        }
    }
    while kept.last().is_some_and(|s| s.trim().is_empty()) {
        kept.pop();
    }
    kept.push(String::new());
    kept.push(BEGIN.into());
    kept.extend(block.into_iter().map(|(k, v)| format!("{k}={}", quote(&v))));
    kept.push(END.into());
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let rendered = kept.join("\n") + "\n";
    if rendered != raw {
        fs::write(path, rendered).map_err(|e| e.to_string())?;
    }
    println!(
        "wrote settings for the {} shim into {}",
        kind.name(),
        path.display()
    );
    Ok(())
}

pub(super) fn resolve_cli(name: &str, kind: Kind) -> Option<PathBuf> {
    let direct = PathBuf::from(name);
    let runnable = |path: PathBuf| -> Option<PathBuf> {
        #[cfg(windows)]
        {
            let extensions =
                std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
            let extensions: Vec<_> = extensions
                .split(';')
                .filter(|s| s.starts_with('.') && s.len() > 1)
                .collect();
            if path.extension().is_some_and(|e| {
                extensions
                    .iter()
                    .any(|x| e.to_string_lossy().eq_ignore_ascii_case(&x[1..]))
            }) && path.is_file()
            {
                return Some(path);
            }
            for ext in extensions {
                let candidate = PathBuf::from(format!("{}{ext}", path.display()));
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
            None
        }
        #[cfg(not(windows))]
        {
            path.is_file().then_some(path)
        }
    };
    if let Some(path) = runnable(direct) {
        return Some(path);
    }
    for dir in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        if let Some(path) = runnable(dir.join(name)) {
            return Some(path);
        }
    }
    if cfg!(windows) && kind == Kind::Codex && matches!(name, "codex" | "codex.exe") {
        let root = PathBuf::from(std::env::var_os("LOCALAPPDATA")?).join("OpenAI/Codex/bin");
        return fs::read_dir(root)
            .ok()?
            .filter_map(Result::ok)
            .map(|d| d.path().join("codex.exe"))
            .filter(|p| p.is_file())
            .max_by_key(|p| p.metadata().and_then(|m| m.modified()).ok());
    }
    None
}

pub(super) fn parse(args: Vec<OsString>) -> Result<Option<Config>, String> {
    let args = args
        .into_iter()
        .map(|a| {
            a.into_string()
                .map_err(|_| "arguments must be valid Unicode".to_owned())
        })
        .collect::<Result<Vec<_>, _>>()?;
    if args.is_empty() || args.iter().any(|s| matches!(s.as_str(), "--help" | "-h")) {
        println!(
            "usage: pseudolife-mcp extractor-shim [config] claude|codex [--repo PATH] [--cli PATH] [--model ID] [--host HOST] [--port PORT] [--call-timeout SECONDS] [--health-ttl SECONDS] [--system-prompt-file PATH] [--reasoning-effort LEVEL] [--emulate-tools]\nWith --repo, read ops/.env at every start; config writes only shim settings. Without --repo, match the eval shims' defaults."
        );
        return Ok(None);
    }
    let writing = args[0] == "config";
    let pos = usize::from(writing);
    let kind = match args.get(pos).map(String::as_str) {
        Some("claude") => Kind::Claude,
        Some("codex") => Kind::Codex,
        _ => return Err("expected claude or codex".into()),
    };
    let mut opts = BTreeMap::new();
    let mut emulate = false;
    let mut at = pos + 1;
    while at < args.len() {
        let arg = &args[at];
        at += 1;
        if arg == "--emulate-tools" {
            emulate = true;
            continue;
        }
        let key = arg
            .strip_prefix("--")
            .ok_or_else(|| format!("unknown argument: {arg}"))?;
        if ![
            "repo",
            "env-file",
            "cli",
            "model",
            "host",
            "port",
            "call-timeout",
            "health-ttl",
            "system-prompt-file",
            "prompt-file",
            "reasoning-effort",
            "log",
        ]
        .contains(&key)
        {
            return Err(format!("unknown argument: {arg}"));
        }
        let v = args
            .get(at)
            .ok_or_else(|| format!("{arg} needs a value"))?
            .clone();
        at += 1;
        opts.insert(
            if key == "system-prompt-file" {
                "prompt-file"
            } else {
                key
            }
            .to_owned(),
            v,
        );
    }
    let repo = opts.remove("repo").map(PathBuf::from);
    let settings_path = opts
        .remove("env-file")
        .map(PathBuf::from)
        .or_else(|| repo.as_ref().map(|r| r.join("ops/.env")));
    if writing {
        for key in ["model", "reasoning-effort"] {
            if let Some(value) = opts.get(key).filter(|s| !s.is_empty()) {
                validate_cli_token(key, value)?;
            }
        }
        write_config(
            repo.as_deref().unwrap_or(Path::new(".")),
            settings_path
                .as_deref()
                .ok_or("config needs --repo or --env-file")?,
            kind,
            &opts,
        )?;
        return Ok(None);
    }
    let managed = repo.is_some() || settings_path.is_some();
    let mut vals = BTreeMap::from([
        (
            "model".into(),
            match kind {
                Kind::Claude if managed => "claude-opus-5-5",
                Kind::Claude => "claude-sonnet-5",
                Kind::Codex => "gpt-5.6-terra",
            }
            .into(),
        ),
        (
            "port".into(),
            if kind == Kind::Claude { "8082" } else { "8086" }.into(),
        ),
        ("host".into(), default_host(managed)),
        ("call-timeout".into(), "300".into()),
        (
            "health-ttl".into(),
            if managed && kind == Kind::Codex {
                "1800"
            } else {
                "300"
            }
            .into(),
        ),
    ]);
    if managed {
        vals.insert(
            "log".into(),
            format!("~/.pseudolife-mcp/{}-shim.log", kind.name()),
        );
    }
    if repo.is_some() && kind == Kind::Claude {
        vals.insert(
            "prompt-file".into(),
            "evals/prompts/sonnet_extractor_v5.md".into(),
        );
    }
    let mut config_hash = None;
    if let Some(path) = &settings_path {
        let (values, hash) = env_file(path);
        config_hash = Some(hash);
        for (k, v) in values {
            if let Some(k) = k.strip_prefix(&kind.prefix())
                && !v.is_empty()
            {
                vals.insert(k.to_lowercase().replace('_', "-"), v);
            }
        }
    }
    vals.extend(opts.into_iter().filter(|(_, v)| !v.is_empty()));
    let port = vals["port"]
        .parse::<u16>()
        .map_err(|_| "port must be between 0 and 65535")?;
    if emulate && kind == Kind::Codex {
        return Err("--emulate-tools is supported only by the Claude shim".into());
    }
    if emulate && [8082, 8086].contains(&port) {
        return Err(format!(
            "--emulate-tools is eval-only; :{port} is a deployed shim port whose callers expect the plain-completion wire format. Pick another port (the tau2 adapter's default is :8092)."
        ));
    }
    let seconds = |key: &str| -> Result<Duration, String> {
        let n = vals[key]
            .parse::<f64>()
            .map_err(|_| format!("{key} must be seconds"))?;
        Duration::try_from_secs_f64(n)
            .map_err(|_| format!("{key} must be finite, non-negative seconds"))
    };
    let timeout = seconds("call-timeout")?;
    let ttl = seconds("health-ttl")?;
    let cli_name = vals
        .get("cli")
        .cloned()
        .or_else(|| {
            std::env::var(format!(
                "PSEUDOLIFE_SHIM_{}_CLI",
                kind.name().to_uppercase()
            ))
            .ok()
        })
        .unwrap_or_else(|| kind.name().into());
    let cwd = repo.clone().or_else(|| {
        settings_path
            .as_ref()
            .and_then(|p| p.parent().map(Path::to_path_buf))
    });
    validate_cli_token("model", &vals["model"])?;
    if let Some(effort) = vals.get("reasoning-effort").filter(|s| !s.is_empty()) {
        validate_cli_token("reasoning-effort", effort)?;
    }
    let local_cli = cwd
        .as_ref()
        .map(|p| p.join(&cli_name))
        .and_then(|p| resolve_cli(&p.to_string_lossy(), kind));
    let cli = local_cli
        .or_else(|| resolve_cli(&cli_name, kind))
        .ok_or_else(|| format!("{} CLI not found at {cli_name}", kind.name()))?;
    let cli = if cwd.is_some() {
        std::path::absolute(cli).map_err(|e| e.to_string())?
    } else {
        cli
    };
    let system = vals
        .get("prompt-file")
        .map(|p| {
            let p = PathBuf::from(p);
            let p = if p.is_absolute() {
                p
            } else {
                cwd.as_deref().unwrap_or(Path::new(".")).join(p)
            };
            fs::read_to_string(&p)
                .map(|s| {
                    let s = s.replace("\r\n", "\n");
                    s.split_once("\n---\n")
                        .map(|(_, b)| b)
                        .unwrap_or(&s)
                        .trim()
                        .to_owned()
                })
                .map_err(|e| format!("{}: {e}", p.display()))
        })
        .transpose()?
        .or_else(|| {
            (managed && kind == Kind::Claude).then(|| {
                let s = include_str!("../../../../evals/prompts/sonnet_extractor_v5.md")
                    .replace("\r\n", "\n");
                s.split_once("\n---\n")
                    .map(|(_, body)| body)
                    .unwrap_or(&s)
                    .trim()
                    .to_owned()
            })
        });
    let log = vals.get("log").map(|s| {
        if let Some(tail) = s.strip_prefix("~/") {
            dirs::home_dir().unwrap_or_default().join(tail)
        } else {
            PathBuf::from(s)
        }
    });
    Ok(Some(Config {
        kind,
        cli,
        cwd,
        model: vals["model"].clone(),
        host: vals["host"].clone(),
        port,
        timeout,
        ttl,
        effort: vals.get("reasoning-effort").cloned(),
        system,
        emulate,
        managed,
        log,
        config_hash,
    }))
}
