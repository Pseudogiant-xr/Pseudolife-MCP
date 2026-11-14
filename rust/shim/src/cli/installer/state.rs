//! `pseudolife-mcp installer state ...`: the installers' private choices
//! and successful-deployment fingerprints in ops/.env's managed block
//! (formerly `ops/installer_state.py`), with the same actions, arguments,
//! stdout and refusals:
//!
//!   choices --env-file F                 recorded choices as KEY=value lines
//!   write-env --env-file F [--template T]  the managed block from stdin
//!   write-file --path F                  an owner-only file from stdin
//!   plan --repo R --env-file F --override O [--snapshot] [--overlay X]...
//!   started|record ... --expected-stdin  check, then record, the deployment
//!   target --path P                      an absolute path as a JSON string
//!
//! The fingerprints hash the same JSON Python's `json.dumps(sort_keys=True,
//! separators=(",", ":"))` produced, so a host recorded by the Python helper
//! keeps matching. Only named choices or status words reach stdout.
use crate::cli::connect::files;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

const BEGIN: &str =
    "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>";
const END: &str = "# <<< pseudolife-mcp install <<<";
const PREFIX: &str = "PSEUDOLIFE_INSTALL_";
const CHOICES: [&str; 15] = [
    "EXTRACTOR",
    "MODEL",
    "EXTRACTOR_URL",
    "SHIM_PORT",
    "CLIENT",
    "BANK_LOCATION",
    "INSTRUCTIONS",
    "CODEX_HOOKS",
    "CLAUDE_PLUGIN",
    "CLAUDE_LEGACY_HOOKS",
    "TRANSPORT",
    "NO_TOKEN",
    "INSTRUCTIONS_GEMINI",
    "INSTRUCTIONS_GENERIC",
    "GENERIC_TARGET_B64",
];
const FINGERPRINTS: [&str; 4] = ["SOURCE_HASH", "BUILD_HASH", "COMPOSE_HASH", "IMAGES_HASH"];
const INPUT_HASHES: [&str; 3] = ["SOURCE_HASH", "BUILD_HASH", "COMPOSE_HASH"];
const RESOLVED_CHOICES: [&str; 3] = [
    "INSTRUCTIONS_GEMINI",
    "INSTRUCTIONS_GENERIC",
    "GENERIC_TARGET_B64",
];
const MODES: [&str; 7] = [
    "sidecar",
    "claude-only",
    "claude-fallback",
    "openai-only",
    "openai-fallback",
    "endpoint",
    "endpoint-fallback",
];
const READ_FAILED: &str = "Cannot read installer records. Repeat the original installer flags or restore the original ops/.env, then rerun.";
const CHECK_FAILED: &str =
    "Cannot check installer state. Check docker info and git status in this checkout, then rerun.";
pub(super) const USAGE: &str = "usage: pseudolife-mcp installer state choices|write-env|write-file|plan|started|record|target [--env-file F] [--path P] [--template T] [--repo R] [--override O] [--snapshot] [--expected-stdin] [--instruction-gemini append|skip] [--instruction-generic append|skip] [--generic-target P] [--overlay F]...";

struct Refusal(String);
fn refuse<T>(message: &str) -> Result<T, Refusal> {
    Err(Refusal(message.to_owned()))
}

#[derive(Default)]
struct Args {
    env_file: Option<PathBuf>,
    path: Option<PathBuf>,
    template: Option<PathBuf>,
    repo: Option<PathBuf>,
    override_file: Option<PathBuf>,
    snapshot: bool,
    expected_stdin: bool,
    gemini: Option<String>,
    generic: Option<String>,
    generic_target: Option<String>,
    overlays: Vec<PathBuf>,
}

fn parse(args: &[String]) -> Result<Args, String> {
    let mut parsed = Args::default();
    let mut items = args.iter();
    while let Some(item) = items.next() {
        let mut value = || {
            items
                .next()
                .cloned()
                .ok_or_else(|| format!("{item} needs a value"))
        };
        match item.as_str() {
            "--env-file" => parsed.env_file = Some(value()?.into()),
            "--path" => parsed.path = Some(value()?.into()),
            "--template" => parsed.template = Some(value()?.into()),
            "--repo" => parsed.repo = Some(value()?.into()),
            "--override" => parsed.override_file = Some(value()?.into()),
            "--overlay" => parsed.overlays.push(value()?.into()),
            "--generic-target" => parsed.generic_target = Some(value()?),
            "--instruction-gemini" | "--instruction-generic" => {
                let choice = value()?;
                if choice != "append" && choice != "skip" {
                    return Err(format!("{item} takes append or skip"));
                }
                if item == "--instruction-gemini" {
                    parsed.gemini = Some(choice)
                } else {
                    parsed.generic = Some(choice)
                }
            }
            "--snapshot" => parsed.snapshot = true,
            "--expected-stdin" => parsed.expected_stdin = true,
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    Ok(parsed)
}

/// Arguments follow `installer state`.
pub(super) fn run(args: &[String]) -> u8 {
    let Some((action, rest)) = args.split_first() else {
        return usage("expected a state action");
    };
    if ![
        "choices",
        "write-env",
        "write-file",
        "plan",
        "started",
        "record",
        "target",
    ]
    .contains(&action.as_str())
    {
        return usage(&format!("unknown state action {action:?}"));
    }
    let args = match parse(rest) {
        Ok(args) => args,
        Err(message) => return usage(&message),
    };
    match act(action, &args) {
        Ok(text) => {
            let mut stdout = io::stdout().lock();
            u8::from(stdout.write_all(text.as_bytes()).is_err())
        }
        Err(Refusal(message)) => {
            let _ = writeln!(io::stderr().lock(), "{message}");
            1
        }
    }
}

fn usage(message: &str) -> u8 {
    let _ = writeln!(io::stderr().lock(), "installer state: {message}\n{USAGE}");
    2
}

fn need<'a>(value: &'a Option<PathBuf>, name: &str) -> Result<&'a Path, Refusal> {
    value
        .as_deref()
        .ok_or_else(|| Refusal(format!("{name} is required")))
}

fn stdin_utf8() -> Result<String, Refusal> {
    let mut bytes = Vec::new();
    io::stdin()
        .lock()
        .take(16 * 1024 * 1024)
        .read_to_end(&mut bytes)
        .map_err(|_| Refusal(READ_FAILED.into()))?;
    String::from_utf8(bytes).map_err(|_| Refusal(UTF8.into()))
}
const UTF8: &str = "Installer configuration must be UTF-8. Save ops/.env as UTF-8 while retaining its existing values, then rerun; nothing changed.";

fn act(action: &str, args: &Args) -> Result<String, Refusal> {
    Ok(match action {
        "target" => format!(
            "{}\n",
            py_json_string(&absolute(need(&args.path, "--path")?))
        ),
        "choices" => choices(need(&args.env_file, "--env-file")?)?
            .into_iter()
            .map(|(k, v)| format!("{k}={v}\n"))
            .collect(),
        "write-env" => {
            let incoming = stdin_utf8()?;
            format!(
                "{}\n",
                write_env(
                    need(&args.env_file, "--env-file")?,
                    &incoming,
                    args.template.as_deref()
                )?
            )
        }
        "write-file" => {
            let incoming = stdin_utf8()?;
            format!("{}\n", write_file(need(&args.path, "--path")?, &incoming)?)
        }
        "plan" => {
            let (repo, env, over) = deployment_args(args)?;
            let result = plan(repo, env, over, &args.overlays)?;
            if args.snapshot {
                format!("{}\n", compact(&Value::Object(result)))
            } else {
                format!("{}\n", result["action"].as_str().unwrap_or(""))
            }
        }
        _ => {
            if !args.expected_stdin {
                return refuse(
                    "The deployment plan is missing; rerun the installer from startup before recording success.",
                );
            }
            let expected: Value =
                serde_json::from_str(&stdin_utf8()?).map_err(|_| Refusal(READ_FAILED.into()))?;
            let (repo, env, over) = deployment_args(args)?;
            if action == "started" {
                format!(
                    "{}\n",
                    compact(&Value::Object(started(
                        repo,
                        env,
                        over,
                        &expected,
                        &args.overlays
                    )?))
                )
            } else {
                let mut instructions = Vec::new();
                if let Some(v) = &args.gemini {
                    instructions.push(("gemini", v.as_str()))
                }
                if let Some(v) = &args.generic {
                    instructions.push(("generic", v.as_str()))
                }
                format!(
                    "{}\n",
                    record(
                        repo,
                        env,
                        over,
                        &expected,
                        &instructions,
                        args.generic_target.as_deref(),
                        &args.overlays
                    )?
                )
            }
        }
    })
}

fn deployment_args(args: &Args) -> Result<(&Path, &Path, &Path), Refusal> {
    Ok((
        need(&args.repo, "--repo")?,
        need(&args.env_file, "--env-file")?,
        need(&args.override_file, "--override")?,
    ))
}

// -- managed block ------------------------------------------------------------

/// Python `str.splitlines()` over the line breaks a config file holds.
fn split_lines(text: &str) -> Vec<String> {
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut lines: Vec<String> = text.split('\n').map(str::to_owned).collect();
    if lines.last().is_some_and(String::is_empty) {
        lines.pop();
    }
    lines
}

fn fields(lines: &[String]) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    for line in lines {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let mut chars = key.chars();
        let first_ok = chars.next().is_some_and(|c| c.is_ascii_uppercase());
        if !(first_ok
            && key.len() >= 2
            && key
                .chars()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_'))
        {
            continue;
        }
        let mut value = value.trim().to_owned();
        // Python: value[:1] in quotes and value[-1:] == value[:1].
        if let Some(first) = value.chars().next()
            && (first == '\'' || first == '"')
            && value.ends_with(first)
        {
            value = if value.len() >= 2 {
                value[1..value.len() - 1].to_owned()
            } else {
                String::new()
            };
        }
        values.insert(key.to_owned(), value);
    }
    values
}

/// Python `Path.read_text(encoding="utf-8-sig")`: universal newlines, BOM dropped.
fn read_text(path: &Path) -> Result<Option<String>, Refusal> {
    if !path.is_file() {
        return Ok(None);
    }
    let bytes = std::fs::read(path).map_err(|_| Refusal(READ_FAILED.into()))?;
    let text = String::from_utf8(bytes).map_err(|_| Refusal(UTF8.into()))?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    Ok(Some(text.replace("\r\n", "\n").replace('\r', "\n")))
}

struct EnvBlock {
    text: String,
    kept: Vec<String>,
    block: Vec<String>,
    managed: bool,
}

fn read_block(path: &Path) -> Result<EnvBlock, Refusal> {
    let text = read_text(path)?.unwrap_or_default();
    let lines = split_lines(&text);
    let begins = lines.iter().filter(|l| *l == BEGIN).count();
    let ends = lines.iter().filter(|l| *l == END).count();
    if begins > 1 || ends > 1 || begins != ends {
        return refuse(
            "ops/.env has incomplete or duplicate installer blocks. Restore its original single managed block, then rerun; nothing changed.",
        );
    }
    if begins == 0 {
        return Ok(EnvBlock {
            text,
            kept: Vec::new(),
            block: Vec::new(),
            managed: false,
        });
    }
    let start = lines.iter().position(|l| l == BEGIN).unwrap_or(0);
    let finish = lines.iter().position(|l| l == END).unwrap_or(0);
    if finish < start {
        return refuse(
            "ops/.env has reversed installer markers. Restore its original managed block, then rerun; nothing changed.",
        );
    }
    let mut kept = lines[..start].to_vec();
    kept.extend_from_slice(&lines[finish + 1..]);
    Ok(EnvBlock {
        text,
        kept,
        block: lines[start + 1..finish].to_vec(),
        managed: true,
    })
}

/// The parts of `urllib.parse.urlsplit(url)` that `choices` reads, read
/// the same way (WHATWG parsing would drop a default port such as `:80` and
/// refuse some authorities `urlsplit` accepts).
struct Split<'a> {
    scheme: &'a str,
    netloc: &'a str,
    /// The text after the netloc: path, query and fragment.
    rest: &'a str,
}
impl<'a> Split<'a> {
    fn new(url: &'a str) -> Self {
        // A scheme is letters first, then letters, digits, "+", "-" or ".".
        let (scheme, after) = match url.split_once(':') {
            Some((scheme, after))
                if scheme
                    .chars()
                    .next()
                    .is_some_and(|c| c.is_ascii_alphabetic())
                    && scheme
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c)) =>
            {
                (scheme, after)
            }
            _ => ("", url),
        };
        let (netloc, rest) = match after.strip_prefix("//") {
            Some(after) => {
                let end = after.find(['/', '?', '#']).unwrap_or(after.len());
                (&after[..end], &after[end..])
            }
            None => ("", after),
        };
        Self {
            scheme,
            netloc,
            rest,
        }
    }
    /// `(hostname, port text)` as `_hostinfo` splits them.
    fn hostinfo(&self) -> (&'a str, Option<&'a str>) {
        let hostinfo = self.netloc.rsplit_once('@').map_or(self.netloc, |(_, h)| h);
        let (hostname, port) = match hostinfo.split_once('[') {
            Some((_, bracketed)) => {
                let (hostname, after) = bracketed.split_once(']').unwrap_or((bracketed, ""));
                (hostname, after.split_once(':').map_or("", |(_, p)| p))
            }
            None => hostinfo.split_once(':').unwrap_or((hostinfo, "")),
        };
        (hostname, (!port.is_empty()).then_some(port))
    }
    /// `.hostname`: lowercased, `None` when empty.
    fn hostname(&self) -> Option<String> {
        let (host, _) = self.hostinfo();
        (!host.is_empty()).then(|| host.to_lowercase())
    }
    /// `.port`; `Err` where Python raises ValueError (not digits, or out
    /// of range).
    fn port(&self) -> Result<Option<u32>, ()> {
        match self.hostinfo().1 {
            None => Ok(None),
            Some(text) if text.bytes().all(|b| b.is_ascii_digit()) => text
                .parse::<u32>()
                .ok()
                .filter(|p| *p <= 65535)
                .map(Some)
                .ok_or(()),
            Some(_) => Err(()),
        }
    }
    /// `.username is not None or .password is not None`: any "@".
    fn has_userinfo(&self) -> bool {
        self.netloc.contains('@')
    }
}

/// Recorded choices in the order Python's dict kept them: an existing key
/// keeps its place when its value is replaced, a new one goes last.
#[derive(Default)]
struct Ordered(Vec<(String, String)>);
impl Ordered {
    fn get(&self, key: &str) -> Option<&String> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }
    fn contains_key(&self, key: &str) -> bool {
        self.get(key).is_some()
    }
    fn insert(&mut self, key: String, value: String) {
        match self.0.iter_mut().find(|(k, _)| *k == key) {
            Some((_, slot)) => *slot = value,
            None => self.0.push((key, value)),
        }
    }
}
impl<'a> IntoIterator for &'a Ordered {
    type Item = &'a (String, String);
    type IntoIter = std::slice::Iter<'a, (String, String)>;
    fn into_iter(self) -> Self::IntoIter {
        self.0.iter()
    }
}

fn choices(path: &Path) -> Result<Vec<(String, String)>, Refusal> {
    let env = read_block(path)?;
    if !env.managed {
        return Ok(Vec::new());
    }
    let saved = fields(&env.block);
    let all_values = fields(&split_lines(&env.text));
    let mut selected = Ordered(
        CHOICES
            .iter()
            .filter_map(|k| {
                saved
                    .get(&format!("{PREFIX}{k}"))
                    .map(|v| ((*k).to_owned(), v.clone()))
            })
            .collect(),
    );
    if !selected.contains_key("BANK_LOCATION") {
        selected.insert("BANK_LOCATION".into(), "local".into());
    }
    // An older open-loopback install must not acquire a token on a rerun.
    let token_set = |k: &str| all_values.get(k).is_some_and(|v| !v.is_empty());
    if !(selected.contains_key("NO_TOKEN")
        || token_set("PSEUDOLIFE_MCP_TOKEN")
        || token_set("PSEUDOLIFE_MCP_TOKENS"))
    {
        selected.insert("NO_TOKEN".into(), "1".into());
    }
    if selected.get("EXTRACTOR").is_none_or(String::is_empty) {
        let base = saved
            .get("PSEUDOLIFE_DREAM_BASE_URL")
            .cloned()
            .unwrap_or_default();
        let model = saved
            .get("PSEUDOLIFE_DREAM_MODEL")
            .cloned()
            .unwrap_or_default();
        let fallback = saved
            .get("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL")
            .is_some_and(|v| !v.is_empty());
        if base.is_empty() {
            selected.insert("EXTRACTOR".into(), "sidecar".into());
        } else if !model.is_empty() && model != "extractor" {
            let mut shown = base.clone();
            let parts = Split::new(&base);
            if parts.hostname().as_deref() == Some("host.docker.internal") {
                // urlunsplit((scheme, "127.0.0.1" + (f":{port}" if port
                // else ""), path, "", "")): port 0 is dropped, the path is
                // kept as written, the query and fragment are dropped.
                let port = match parts.port() {
                    Ok(Some(port)) if port != 0 => format!(":{port}"),
                    Ok(_) => String::new(),
                    Err(()) => return refuse(READ_FAILED),
                };
                let path = &parts.rest[..parts.rest.find(['?', '#']).unwrap_or(parts.rest.len())];
                let scheme = if parts.scheme.is_empty() {
                    String::new()
                } else {
                    format!("{}:", parts.scheme.to_lowercase())
                };
                shown = format!("{scheme}//127.0.0.1{port}{path}");
            }
            selected.insert(
                "EXTRACTOR".into(),
                if fallback {
                    "endpoint-fallback"
                } else {
                    "endpoint"
                }
                .into(),
            );
            selected.insert("MODEL".into(), model);
            selected.insert("EXTRACTOR_URL".into(), shown);
        } else {
            let port = match Split::new(&base).port() {
                Ok(port) => port,
                Err(()) => return refuse(READ_FAILED),
            };
            let mut matches = Vec::new();
            for (family, mode) in [("CLAUDE", "claude"), ("CODEX", "openai")] {
                let prefix = format!("PSEUDOLIFE_{family}_SHIM_");
                let recorded_port = all_values.get(&format!("{prefix}PORT"));
                let recorded_model = all_values
                    .get(&format!("{prefix}MODEL"))
                    .filter(|m| !m.is_empty());
                if let (Some(port), Some(rp), Some(model)) = (port, recorded_port, recorded_model)
                    && rp == &port.to_string()
                {
                    matches.push((mode, model.clone(), port));
                }
            }
            if let [(mode, model, port)] = matches.as_slice() {
                selected.insert(
                    "EXTRACTOR".into(),
                    format!("{mode}{}", if fallback { "-fallback" } else { "-only" }),
                );
                selected.insert("MODEL".into(), model.clone());
                selected.insert("SHIM_PORT".into(), port.to_string());
            }
        }
    }
    if !selected.contains_key("CLIENT")
        && let Some(writer) = saved.get("PSEUDOLIFE_WRITER_ID")
        && ["claude-code", "claude-desktop", "codex", "gemini"].contains(&writer.as_str())
    {
        selected.insert(
            "CLIENT".into(),
            if writer == "claude-code" {
                "claude".into()
            } else {
                writer.clone()
            },
        );
    }
    let extractor = selected.get("EXTRACTOR").cloned().unwrap_or_default();
    if extractor.starts_with("claude-") || extractor.starts_with("openai-") {
        let family = if extractor.starts_with("claude-") {
            "CLAUDE"
        } else {
            "CODEX"
        };
        for (setting, key) in [("MODEL", "MODEL"), ("PORT", "SHIM_PORT")] {
            if let Some(value) = all_values
                .get(&format!("PSEUDOLIFE_{family}_SHIM_{setting}"))
                .filter(|v| !v.is_empty())
            {
                selected.insert(key.into(), value.clone());
            }
        }
    }
    for (key, value) in &selected {
        if !value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "._:/@+,[]-".contains(c))
        {
            return refuse(
                "Recorded installer choices are invalid. Repeat the original installer flags to replace the managed block; nothing changed.",
            );
        }
        if key == "EXTRACTOR_URL" && !value.is_empty() && Split::new(value).has_userinfo() {
            return refuse(
                "The recorded extractor URL contains credentials. Remove URL credentials from ops/.env and keep API keys outside the managed block; nothing changed.",
            );
        }
    }
    if let Some(mode) = selected.get("EXTRACTOR").filter(|m| !m.is_empty())
        && !MODES.contains(&mode.as_str())
    {
        return refuse(
            "The recorded extractor mode is unknown. Repeat the original --extractor/-Extractor flag; nothing changed.",
        );
    }
    let allowed: [(&str, &[&str]); 9] = [
        ("BANK_LOCATION", &["local", "shared"]),
        ("INSTRUCTIONS", &["", "auto", "append", "skip"]),
        ("CODEX_HOOKS", &["auto", "manual", "plugin", "skip"]),
        ("CLAUDE_PLUGIN", &["auto", "skip"]),
        ("CLAUDE_LEGACY_HOOKS", &["ask", "remove", "keep"]),
        ("TRANSPORT", &["shim", "http"]),
        ("NO_TOKEN", &["0", "1"]),
        ("INSTRUCTIONS_GEMINI", &["append", "skip"]),
        ("INSTRUCTIONS_GENERIC", &["append", "skip"]),
    ];
    if allowed.iter().any(|(key, options)| {
        selected
            .get(key)
            .is_some_and(|v| !options.contains(&v.as_str()))
    }) {
        return refuse(
            "Recorded installer choices are invalid. Restore the original managed block or repeat the original installer flags.",
        );
    }
    if let Some(clients) = selected.get("CLIENT")
        && !clients
            .split(',')
            .all(|c| ["claude", "claude-desktop", "codex", "gemini", "generic"].contains(&c))
    {
        return refuse(
            "The recorded client list is invalid. Repeat the original --client/-Client flag; nothing changed.",
        );
    }
    if let Some(port) = selected.get("SHIM_PORT").filter(|p| !p.is_empty())
        && !(port.chars().all(|c| c.is_ascii_digit())
            && port.parse::<u32>().is_ok_and(|p| (1..65536).contains(&p)))
    {
        return refuse(
            "The recorded shim port is invalid. Repeat the original --shim-port/-ShimPort flag; nothing changed.",
        );
    }
    let mut result: Vec<(String, String)> = CHOICES
        .iter()
        .filter(|k| **k != "GENERIC_TARGET_B64")
        .filter_map(|k| selected.get(k).map(|v| ((*k).to_owned(), v.clone())))
        .collect();
    if let Some(encoded) = selected.get("GENERIC_TARGET_B64").filter(|v| !v.is_empty()) {
        let invalid = || {
            Refusal("The recorded instruction target is invalid; restore the original managed block in ops/.env, then rerun.".into())
        };
        let decoded = base64url_decode(encoded).ok_or_else(invalid)?;
        let target = String::from_utf8(decoded).map_err(|_| invalid())?;
        if target.chars().any(|c| (c as u32) < 32) {
            return Err(invalid());
        }
        result.push(("GENERIC_TARGET_JSON".into(), py_json_string(&target)));
    }
    Ok(result)
}

fn write_file(path: &Path, content: &str) -> Result<&'static str, Refusal> {
    let content = content.replace("\r\n", "\n");
    if read_text(path)?.as_deref() == Some(content.as_str()) {
        return Ok("unchanged");
    }
    files::write_private(path, content.as_bytes()).map_err(|_| {
        Refusal("Cannot secure installer configuration for this user. Use a checkout owned by your user, then rerun.".into())
    })?;
    Ok("changed")
}

fn write_env(
    path: &Path,
    incoming: &str,
    template: Option<&Path>,
) -> Result<&'static str, Refusal> {
    let env = read_block(path)?;
    let mut previous = env.text.clone();
    if !path.is_file()
        && let Some(template) = template
    {
        previous = read_text(template)?.unwrap_or_default();
    }
    let mut new_lines = split_lines(&incoming.replace("\r\n", "\n"));
    if new_lines.first().map(String::as_str) != Some(BEGIN)
        || new_lines.last().map(String::as_str) != Some(END)
    {
        return refuse(
            "Installer managed-block input is incomplete; rerun the installer. Nothing changed.",
        );
    }
    let old_values = fields(&env.block);
    let mut new_values = fields(&new_lines[1..new_lines.len() - 1]);
    for key in FINGERPRINTS.iter().chain(RESOLVED_CHOICES.iter()) {
        let full = format!("{PREFIX}{key}");
        if let Some(old) = old_values.get(&full)
            && !new_values.contains_key(&full)
        {
            let at = new_lines.len() - 1;
            new_lines.insert(at, format!("{full}={old}"));
            new_values.insert(full, old.clone());
        }
    }
    if env.managed && old_values == new_values {
        return Ok("unchanged");
    }
    let outside = if env.managed {
        env.kept.join("\n")
    } else {
        previous.trim_end_matches(['\r', '\n']).to_owned()
    };
    write_file(
        path,
        &format!(
            "{}\n{}\n",
            outside.trim_end_matches('\n'),
            new_lines.join("\n")
        ),
    )
}

// -- deployment fingerprints --------------------------------------------------

/// Python `subprocess.run(..., text=True, errors="replace", timeout=30)`:
/// stdout with universal newlines, or the refusal on any failure.
fn run_command(argv: &[String]) -> Result<String, Refusal> {
    output_within(argv, Duration::from_secs(30)).map_err(|_| Refusal(CHECK_FAILED.into()))
}

/// The program the installer steps run for `program`: one named without a
/// path is found only in an absolute `PATH` directory
/// (`program_lookup::find_program_path`), never in the current directory, the
/// running program's directory or the Windows system and Windows directories
/// that the loader would otherwise also search; a path is run as given.
/// `None` when no absolute `PATH` directory holds it.
pub(in crate::cli) fn program_path(program: &str) -> Option<PathBuf> {
    let has_separator = program.contains('/') || (cfg!(windows) && program.contains('\\'));
    if has_separator || Path::new(program).components().count() != 1 {
        return Some(PathBuf::from(program));
    }
    crate::cli::program_lookup::find_program_path(program)
}

/// Python `subprocess.run(argv, text=True, errors="replace", timeout=...)`
/// for a successful run: stdout with universal newlines, stderr discarded
/// (it may carry resolved credentials). `Err(Unreached)` when the program
/// cannot start or outlives `timeout` (then it is killed), `Err(Exited)`
/// when it exits nonzero.
pub(super) fn output_within(argv: &[String], timeout: Duration) -> Result<String, NotRun> {
    let (program, rest) = argv.split_first().ok_or(NotRun::Unreached)?;
    let program = program_path(program).ok_or(NotRun::Unreached)?;
    let mut child = Command::new(program)
        .args(rest)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| NotRun::Unreached)?;
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        if let Some(mut out) = stdout {
            let _ = out.read_to_end(&mut bytes);
        }
        bytes
    });
    let drain = std::thread::spawn(move || {
        if let Some(mut err) = stderr {
            let _ = io::copy(&mut err, &mut io::sink());
        }
    });
    let started = std::time::Instant::now();
    let status = loop {
        if let Ok(Some(status)) = child.try_wait() {
            break status;
        }
        if started.elapsed() > timeout {
            let _ = child.kill();
            let _ = child.wait();
            return Err(NotRun::Unreached);
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let bytes = reader.join().unwrap_or_default();
    let _ = drain.join();
    if !status.success() {
        return Err(NotRun::Exited);
    }
    Ok(String::from_utf8_lossy(&bytes)
        .replace("\r\n", "\n")
        .replace('\r', "\n"))
}

/// Why `output_within` has no output: Python's OSError or TimeoutExpired
/// (`Unreached`), or a nonzero exit (`Exited`).
pub(super) enum NotRun {
    Unreached,
    Exited,
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

/// `hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")))`.
fn digest(value: &Value) -> String {
    sha256_hex(py_dumps_sorted(value).as_bytes())
}

/// Python `str(Path.resolve())`: no Windows verbatim prefix.
fn resolved(path: &Path) -> String {
    let real = std::fs::canonicalize(path).unwrap_or_else(|_| absolute_path(path));
    let text = real.to_string_lossy().into_owned();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return format!(r"\\{rest}");
    }
    text.strip_prefix(r"\\?\")
        .map(str::to_owned)
        .unwrap_or(text)
}

fn absolute_path(path: &Path) -> PathBuf {
    std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf())
}

/// Python `str(Path(p).expanduser().absolute())`.
fn absolute(path: &Path) -> String {
    let expanded = crate::credentials::expand_user(path).unwrap_or_else(|_| path.to_path_buf());
    absolute_path(&expanded).to_string_lossy().into_owned()
}

fn s(text: impl Into<String>) -> String {
    text.into()
}

struct Deployment {
    hashes: Map<String, Value>,
    complete: bool,
    running: bool,
    applied: bool,
}

fn deployment(
    repo: &Path,
    env_file: &Path,
    override_file: &Path,
    overlays: &[PathBuf],
) -> Result<Deployment, Refusal> {
    let repo_s = repo.to_string_lossy().into_owned();
    let git = |args: &[&str]| -> Result<String, Refusal> {
        let mut argv = vec![s("git"), s("-C"), repo_s.clone()];
        argv.extend(args.iter().map(|a| (*a).to_owned()));
        run_command(&argv)
    };
    let head = git(&["rev-parse", "HEAD"])?.trim().to_owned();
    let delta = git(&["diff", "--binary", "HEAD"])?;
    let untracked = git(&["ls-files", "-z", "--others", "--exclude-standard"])?;
    let mut names: Vec<&str> = untracked.split('\0').filter(|n| !n.is_empty()).collect();
    names.sort_unstable();
    // A file that is there but cannot be read refuses, as Python's open()
    // raising did; one that is gone (or not a file) hashes as empty.
    let extra: Vec<Value> = names
        .into_iter()
        .map(|name| {
            let path = repo.join(name);
            let hash = if path.is_file() {
                sha256_hex(&std::fs::read(&path).map_err(|_| Refusal(READ_FAILED.into()))?)
            } else {
                sha256_hex(b"")
            };
            Ok(json!([name, hash]))
        })
        .collect::<Result<_, Refusal>>()?;
    let source_hash = digest(&json!([resolved(repo), head, delta, extra]));
    let mut compose = vec![
        s("docker"),
        s("compose"),
        s("-p"),
        s("pseudolife-mcp"),
        s("--env-file"),
        env_file.to_string_lossy().into_owned(),
        s("-f"),
        repo.join("ops/docker-compose.yml")
            .to_string_lossy()
            .into_owned(),
    ];
    if override_file.is_file() {
        compose.extend([s("-f"), override_file.to_string_lossy().into_owned()]);
    }
    for overlay in overlays {
        compose.extend([s("-f"), overlay.to_string_lossy().into_owned()]);
    }
    let with = |extra: &[&str]| -> Vec<String> {
        let mut argv = compose.clone();
        argv.extend(extra.iter().map(|a| (*a).to_owned()));
        argv
    };
    let config: Value = serde_json::from_str(&run_command(&with(&["config", "--format", "json"]))?)
        .map_err(|_| Refusal(READ_FAILED.into()))?;
    let mut service_hashes: BTreeMap<String, String> = BTreeMap::new();
    for line in run_command(&with(&["config", "--hash", "*"]))?.lines() {
        let mut parts = line.splitn(2, char::is_whitespace);
        let (Some(name), Some(value)) = (parts.next(), parts.next()) else {
            return refuse(READ_FAILED);
        };
        service_hashes.insert(name.to_owned(), value.trim().to_owned());
    }
    let profiles: Vec<String> = std::env::var("COMPOSE_PROFILES")
        .unwrap_or_default()
        .split(',')
        .filter(|p| !p.is_empty())
        .map(str::to_owned)
        .collect();
    let services = config
        .get("services")
        .and_then(Value::as_object)
        .ok_or_else(|| Refusal(READ_FAILED.into()))?;
    let active: Vec<(&String, &Value)> = services
        .iter()
        .filter(
            |(_, service)| match service.get("profiles").and_then(Value::as_array) {
                None => true,
                Some(list) if list.is_empty() => true,
                Some(list) => list
                    .iter()
                    .filter_map(Value::as_str)
                    .any(|p| profiles.iter().any(|e| e == p)),
            },
        )
        .collect();
    let names: BTreeMap<String, String> = active
        .iter()
        .map(|(name, service)| {
            let container = service
                .get("container_name")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .unwrap_or_else(|| format!("pseudolife-mcp-{name}-1"));
            (container, (*name).clone())
        })
        .collect();
    let ids: Vec<String> =
        run_command(&[s("docker"), s("ps"), s("-a"), s("--format"), s("{{.ID}}")])?
            .split_whitespace()
            .map(str::to_owned)
            .collect();
    let inventory: Vec<Value> = if ids.is_empty() {
        Vec::new()
    } else {
        let mut argv = vec![s("docker"), s("inspect")];
        argv.extend(ids);
        serde_json::from_str(&run_command(&argv)?).map_err(|_| Refusal(READ_FAILED.into()))?
    };
    let mut found: BTreeMap<String, &Value> = BTreeMap::new();
    for container in &inventory {
        let name = container
            .get("Name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim_start_matches('/')
            .to_owned();
        if names.contains_key(&name) {
            found.insert(name, container);
        }
    }
    let label = |c: &Value, key: &str| {
        c.pointer("/Config/Labels")
            .and_then(|l| l.get(key))
            .and_then(Value::as_str)
            .map(str::to_owned)
    };
    if found
        .values()
        .any(|c| label(c, "com.docker.compose.project").as_deref() != Some("pseudolife-mcp"))
    {
        return refuse(
            "The configured containers belong to another Compose project. Use that stack's own compose file; nothing was started or removed.",
        );
    }
    let complete = names.keys().all(|n| found.contains_key(n)) && found.len() == names.len();
    let running = complete
        && found.values().all(|c| {
            c.pointer("/State/Running")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        });
    let applied = complete
        && found.iter().all(|(name, c)| {
            let service = &names[name];
            service_hashes.get(service).is_some_and(|h| {
                !h.is_empty()
                    && label(c, "com.docker.compose.config-hash").as_deref() == Some(h.as_str())
            })
        });
    let build: Map<String, Value> = active
        .iter()
        .filter(|(_, service)| service.get("build").is_some_and(|b| !b.is_null() && b != &Value::Bool(false) && b != &json!("") && b != &json!({})))
        .map(|(name, service)| ((*name).clone(), json!({"build": service["build"], "image": service.get("image").cloned().unwrap_or(Value::Null)})))
        .collect();
    let images: Map<String, Value> = found
        .iter()
        .map(|(name, c)| (name.clone(), c.get("Image").cloned().unwrap_or(Value::Null)))
        .collect();
    let mut hashes = Map::new();
    hashes.insert("SOURCE_HASH".into(), json!(source_hash));
    hashes.insert("COMPOSE_HASH".into(), json!(digest(&config)));
    hashes.insert("BUILD_HASH".into(), json!(digest(&Value::Object(build))));
    hashes.insert("IMAGES_HASH".into(), json!(digest(&Value::Object(images))));
    Ok(Deployment {
        hashes,
        complete,
        running,
        applied,
    })
}

fn hash<'a>(hashes: &'a Map<String, Value>, key: &str) -> &'a str {
    hashes.get(key).and_then(Value::as_str).unwrap_or("")
}

fn plan(
    repo: &Path,
    env_file: &Path,
    override_file: &Path,
    overlays: &[PathBuf],
) -> Result<Map<String, Value>, Refusal> {
    let d = deployment(repo, env_file, override_file, overlays)?;
    let old = fields(&read_block(env_file)?.block);
    let recorded = |key: &str| old.get(&format!("{PREFIX}{key}")).cloned();
    let action = if !d.complete
        || ["SOURCE_HASH", "BUILD_HASH", "IMAGES_HASH"]
            .iter()
            .any(|k| recorded(k).as_deref() != Some(hash(&d.hashes, k)))
    {
        "build"
    } else if d.running
        && d.applied
        && recorded("COMPOSE_HASH").as_deref() == Some(hash(&d.hashes, "COMPOSE_HASH"))
    {
        "unchanged"
    } else {
        "start"
    };
    let mut result = Map::new();
    result.insert("action".into(), json!(action));
    for (k, v) in d.hashes {
        result.insert(k, v);
    }
    Ok(result)
}

fn same_inputs(expected: &Value, hashes: &Map<String, Value>) -> bool {
    expected.is_object()
        && INPUT_HASHES
            .iter()
            .all(|k| expected.get(*k).and_then(Value::as_str) == Some(hash(hashes, k)))
}

fn started(
    repo: &Path,
    env_file: &Path,
    override_file: &Path,
    expected: &Value,
    overlays: &[PathBuf],
) -> Result<Map<String, Value>, Refusal> {
    let d = deployment(repo, env_file, override_file, overlays)?;
    if !same_inputs(expected, &d.hashes) {
        return refuse(
            "Checkout or Compose inputs changed during startup; rerun the installer with a stable checkout, and no success record has been saved.",
        );
    }
    if !d.complete || !d.applied {
        return refuse(
            "The running containers do not match the requested Compose configuration; check docker compose logs and rerun, and no success record has been saved.",
        );
    }
    if expected.get("action").and_then(Value::as_str) == Some("unchanged")
        && expected.get("IMAGES_HASH").and_then(Value::as_str)
            != Some(hash(&d.hashes, "IMAGES_HASH"))
    {
        return refuse(
            "Docker images changed while checking the existing stack; rerun the installer, and no success record has been saved.",
        );
    }
    let mut result = expected.as_object().cloned().unwrap_or_default();
    result.insert(
        "STARTED_IMAGES_HASH".into(),
        json!(hash(&d.hashes, "IMAGES_HASH")),
    );
    Ok(result)
}

fn record(
    repo: &Path,
    env_file: &Path,
    override_file: &Path,
    expected: &Value,
    instructions: &[(&str, &str)],
    generic_target: Option<&str>,
    overlays: &[PathBuf],
) -> Result<&'static str, Refusal> {
    let d = deployment(repo, env_file, override_file, overlays)?;
    if !same_inputs(expected, &d.hashes)
        || expected.get("STARTED_IMAGES_HASH").and_then(Value::as_str)
            != Some(hash(&d.hashes, "IMAGES_HASH"))
    {
        return refuse(
            "Checkout, Compose inputs or images changed after startup; rerun the installer with stable inputs, and no success record has been saved.",
        );
    }
    if !d.complete || !d.running || !d.applied {
        return refuse(
            "The verified Docker stack changed during setup; rerun the installer, and no success record has been saved.",
        );
    }
    let env = read_block(env_file)?;
    if !env.managed {
        return refuse(
            "The installer managed block is missing; restore the original ops/.env and rerun.",
        );
    }
    // Python dict order: the hashes, then instruction choices, then the target.
    let mut updates: Vec<(String, String)> =
        ["SOURCE_HASH", "COMPOSE_HASH", "BUILD_HASH", "IMAGES_HASH"]
            .iter()
            .map(|k| (format!("{PREFIX}{k}"), hash(&d.hashes, k).to_owned()))
            .collect();
    for (client, value) in instructions {
        updates.push((
            format!("{PREFIX}INSTRUCTIONS_{}", client.to_uppercase()),
            (*value).to_owned(),
        ));
    }
    if let Some(target) = generic_target {
        let target = absolute(Path::new(target));
        updates.push((
            format!("{PREFIX}GENERIC_TARGET_B64"),
            base64url_encode(target.as_bytes()),
        ));
    }
    let mut lines: Vec<String> = env
        .block
        .into_iter()
        .filter(|line| {
            !updates
                .iter()
                .any(|(k, _)| line.split_once('=').map_or(line.as_str(), |(key, _)| key) == k)
        })
        .collect();
    lines.extend(updates.iter().map(|(k, v)| format!("{k}={v}")));
    let mut block = vec![BEGIN.to_owned()];
    block.extend(lines);
    block.push(END.to_owned());
    write_env(env_file, &format!("{}\n", block.join("\n")), None)
}

// -- Python-compatible JSON and base64 ----------------------------------------

fn py_escape(text: &str, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `json.dumps(text, ensure_ascii=True)`.
pub(in crate::cli) fn py_json_string(text: &str) -> String {
    let mut out = String::new();
    py_escape(text, &mut out);
    out
}

fn py_number(number: &serde_json::Number) -> String {
    if let Some(i) = number.as_i64() {
        return i.to_string();
    }
    if let Some(u) = number.as_u64() {
        return u.to_string();
    }
    let f = number.as_f64().unwrap_or(0.0);
    crate::cli::connect::pyjson::float_repr(f)
}

/// Python's `json.dumps(value, ensure_ascii=True)` with the given key order
/// and separators (`item`, `key`): `(", ", ": ")` is its default.
fn py_dumps_with(value: &Value, sort: bool, item: &str, key: &str) -> String {
    fn walk(value: &Value, sort: bool, item: &str, key: &str, out: &mut String) {
        match value {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Number(n) => out.push_str(&py_number(n)),
            Value::String(text) => py_escape(text, out),
            Value::Array(items) => {
                out.push('[');
                for (i, entry) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(item);
                    }
                    walk(entry, sort, item, key, out);
                }
                out.push(']');
            }
            Value::Object(map) => {
                let mut keys: Vec<&String> = map.keys().collect();
                if sort {
                    keys.sort();
                }
                out.push('{');
                for (i, name) in keys.iter().enumerate() {
                    if i > 0 {
                        out.push_str(item);
                    }
                    py_escape(name, out);
                    out.push_str(key);
                    walk(&map[*name], sort, item, key, out);
                }
                out.push('}');
            }
        }
    }
    let mut out = String::new();
    walk(value, sort, item, key, &mut out);
    out
}

/// `json.dumps(value, sort_keys=True, separators=(",", ":"))`.
fn py_dumps_sorted(value: &Value) -> String {
    py_dumps_with(value, true, ",", ":")
}

/// `json.dumps(value, separators=(",", ":"))` in insertion order.
fn compact(value: &Value) -> String {
    py_dumps_with(value, false, ",", ":")
}

/// `json.dumps(value)`: insertion order, `", "` and `": "`.
pub(in crate::cli) fn py_dumps(value: &Value) -> String {
    py_dumps_with(value, false, ", ", ": ")
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn base64url_encode(bytes: &[u8]) -> String {
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        for i in 0..=chunk.len() {
            out.push(B64[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}

/// `base64.b64decode(value + padding, altchars=b"-_", validate=True)`.
fn base64url_decode(text: &str) -> Option<Vec<u8>> {
    let text = text.trim_end_matches('=');
    if text.len() % 4 == 1 {
        return None;
    }
    let mut bits = 0u32;
    let mut count = 0;
    let mut out = Vec::new();
    for c in text.bytes() {
        let value = B64.iter().position(|b| *b == c)? as u32;
        bits = (bits << 6) | value;
        count += 6;
        if count >= 8 {
            count -= 8;
            out.push((bits >> count) as u8);
            bits &= (1 << count) - 1;
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sorted_dumps_matches_python_json_dumps() {
        // python3 -c 'import json;print(json.dumps({"b":[1,"é",None],"a":{"z":True,"y":1.5}},sort_keys=True,separators=(",",":")))'
        let value = json!({"b": [1, "é", null], "a": {"z": true, "y": 1.5}});
        assert_eq!(
            py_dumps_sorted(&value),
            r#"{"a":{"y":1.5,"z":true},"b":[1,"\u00e9",null]}"#
        );
        let report = json!({"status": "failed", "recovery": "caf\u{e9}", "n": [1, null]});
        assert_eq!(
            py_dumps(&report),
            r#"{"status": "failed", "recovery": "caf\u00e9", "n": [1, null]}"#
        );
        assert_eq!(
            py_json_string("C:\\x \u{1f600}\t"),
            r#""C:\\x \ud83d\ude00\t""#
        );
    }

    #[test]
    fn fields_reads_assignments_and_unquotes() {
        let lines = split_lines("AB=1\nA=9\nlower=2\nB_2 = 'x y' \nC2=\"q\"\r\n# D=4\n");
        let f = fields(&lines);
        assert_eq!(f.get("AB").map(String::as_str), Some("1"));
        // `[A-Z][A-Z0-9_]+`: a one-letter key is not a field.
        assert!(!f.contains_key("A"));
        assert!(!f.contains_key("lower"));
        assert_eq!(f.get("C2").map(String::as_str), Some("q"));
        assert!(!f.contains_key("B_2 "));
    }

    #[test]
    fn base64url_round_trips_without_padding() {
        for text in ["", "a", "ab", "abc", "/home/u/AGENTS.md"] {
            let encoded = base64url_encode(text.as_bytes());
            assert!(!encoded.contains('='));
            assert_eq!(base64url_decode(&encoded).unwrap(), text.as_bytes());
        }
        assert!(base64url_decode("a").is_none());
        assert!(base64url_decode("ab$c").is_none());
    }
}
