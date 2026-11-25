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
                "gpt-6.1-sol",
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
    pub receipt_dir: Option<PathBuf>,
}

impl Config {
    // Serialize only values the shim actually uses, after defaults, overrides
    // and path resolution. Unrelated ops/.env values must never enter this hash.
    pub fn fingerprint(&self) -> String {
        let values = serde_json::json!([
            self.kind.name(),
            self.cli,
            self.cwd,
            self.model,
            self.host,
            self.port,
            self.timeout.as_nanos().to_string(),
            self.ttl.as_nanos().to_string(),
            self.effort,
            self.system,
            self.emulate,
            self.managed,
            self.log
        ]);
        format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&values).expect("settings JSON"))
        )
    }
}

pub(super) fn default_receipt_dir() -> Result<PathBuf, String> {
    absolute_path(
        &dirs::cache_dir()
            .or_else(|| dirs::home_dir().map(|home| home.join(".cache")))
            .ok_or("user cache directory unavailable")?
            .join("pseudolife-mcp/extractor-settings"),
    )
}

// Expand existing Windows ancestors so a short temp/home path and its long
// spelling produce the same registration, including not-yet-created files.
pub(super) fn absolute_path(path: &Path) -> Result<PathBuf, String> {
    let absolute = std::path::absolute(path).map_err(|e| e.to_string())?;
    #[cfg(windows)]
    {
        let mut head = absolute.as_path();
        let mut tail = vec![];
        loop {
            if let Ok(mut resolved) = fs::canonicalize(head) {
                for part in tail.iter().rev() {
                    resolved.push(part);
                }
                if let Some(text) = resolved.to_str() {
                    resolved = PathBuf::from(windows_path_without_verbatim_prefix(text));
                }
                return Ok(resolved);
            }
            let Some(name) = head.file_name() else { break };
            tail.push(name);
            let Some(parent) = head.parent() else { break };
            head = parent;
        }
    }
    Ok(absolute)
}

#[cfg(any(windows, test))]
fn windows_path_without_verbatim_prefix(path: &str) -> String {
    let Some(verbatim) = path.strip_prefix(r"\\?\") else {
        return path.to_owned();
    };
    let bytes = verbatim.as_bytes();
    let (plain, components) = if let Some(unc) = verbatim.strip_prefix(r"UNC\") {
        if !unc
            .split_once('\\')
            .is_some_and(|(server, share)| !server.is_empty() && !share.is_empty())
        {
            return path.to_owned();
        }
        (format!(r"\\{unc}"), unc)
    } else if bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && &bytes[1..3] == b":\\" {
        (verbatim.to_owned(), &verbatim[3..])
    } else {
        // Volume GUIDs and other namespaces have no equivalent DOS spelling.
        return path.to_owned();
    };
    // MAX_PATH includes the NUL terminator; check the complete path, not just
    // the ancestor that canonicalize found. Windows counts UTF-16 code units.
    let safe = components.is_empty()
        || components
            .strip_suffix('\\')
            .unwrap_or(components)
            .split('\\')
            .all(|part| {
                let stem = part.split('.').next().unwrap_or("").trim_end_matches(' ');
                let stem = stem.to_ascii_uppercase();
                !part.is_empty()
                    && !part.ends_with(['.', ' '])
                    && !part.chars().any(|c| c < ' ' || r#"<>:"/|?*"#.contains(c))
                    && !matches!(
                        stem.as_str(),
                        "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
                    )
                    && !["COM", "LPT"].iter().any(|prefix| {
                        stem.strip_prefix(*prefix)
                            .is_some_and(|n| n.chars().count() == 1 && "123456789¹²³".contains(n))
                    })
            });
    if plain.encode_utf16().count() < 260 && safe {
        plain
    } else {
        path.to_owned()
    }
}

fn receipt_path(config: &Config, pid: u32) -> Result<PathBuf, String> {
    Ok(config
        .receipt_dir
        .as_ref()
        .ok_or("managed settings required")?
        .join(format!("{pid}.sha256")))
}

pub(super) struct SettingsReceipt(PathBuf);
impl SettingsReceipt {
    pub fn create(config: &Config) -> Result<Self, String> {
        let path = receipt_path(config, std::process::id())?;
        let parent = path.parent().expect("receipt parent");
        let mut builder = fs::DirBuilder::new();
        builder.recursive(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        builder.create(parent).map_err(|e| e.to_string())?;
        // The directory is in the user's private cache (LocalAppData on
        // Windows). Reject links and keep the Unix directory owner-only.
        if fs::symlink_metadata(parent)
            .map_err(|e| e.to_string())?
            .file_type()
            .is_symlink()
        {
            return Err("settings receipt directory must not be a symlink".into());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
                .map_err(|e| e.to_string())?;
        }
        // A killed process can leave its receipt behind; replace only this
        // process's PID entry, without following a stale symlink.
        match fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.to_string()),
        }
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&path).map_err(|e| e.to_string())?;
        use std::io::Write;
        file.write_all(config.fingerprint().as_bytes())
            .map_err(|e| e.to_string())?;
        Ok(Self(path))
    }
}
impl Drop for SettingsReceipt {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

pub(super) fn verify_settings(config: &Config, pid: u32) -> Result<(), String> {
    let loaded = fs::read_to_string(receipt_path(config, pid)?).map_err(|e| e.to_string())?;
    if loaded == config.fingerprint() {
        Ok(())
    } else {
        Err("running shim settings differ from requested settings".into())
    }
}

// The block's first line tells whoever edits the file how to apply the edit.
// The shim reads this file at every start, so on Linux a restart of its unit
// is enough: the step autostart.sh prints as its reload, for the default unit
// names (autostart.rs) or the one given with --name. macOS has no native
// autostart. A shipped text is never edited: files written with it must
// still match.
const BEGIN_LINUX: &str = "# >>> pseudolife shim autostart (edit values here, then: systemctl --user restart pseudolife-sonnet-shim.service for Claude, pseudolife-codex-shim.service for Codex, or the unit named at install with --name) >>>";
const BEGIN_OTHER: &str = "# >>> pseudolife shim autostart (edit values here, then stop the extractor shim you started and start it again) >>>";
// On Windows stopping and starting the task stops only the PowerShell
// wrapper it runs: the shim that wrapper started keeps its port and its old
// settings (Windows dev VM, 26 November 2026, real-autostart harness). The
// registration's own rerun captures and stops that shim, so the line names
// it with this install's values, built at write time, and the task it is
// for, so the line stays true beside a sibling or after a removal.
const BEGIN_WINDOWS_PREFIX: &str =
    "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task ";
const BEGIN_WINDOWS_INFIX: &str = " is registered, run in PowerShell: ";
const BEGIN_WINDOWS_SUFFIX: &str = ") >>>";
// A Windows write that does not know the registration (a bare `config` on a
// file no install-autostart wrote) never guesses its name or logon type.
const BEGIN_WINDOWS_UNKNOWN: &str = "# >>> pseudolife shim autostart (edit values here, then run again, in PowerShell, the pseudolife-mcp extractor-shim install-autostart command this shim was registered with) >>>";
// Written by ops/shim_autostart.py and by earlier native releases; its
// instruction needs Python, so it is read and replaced but never written.
const BEGIN_PYTHON: &str = "# >>> pseudolife shim autostart (managed by ops/shim_autostart.py; edit values here, then: python ops/shim_autostart.py restart claude|codex) >>>";
const END: &str = "# <<< pseudolife shim autostart <<<";

/// The characters PowerShell accepts as a single-quote delimiter.
fn powershell_quote_char(c: char) -> bool {
    matches!(c, '\'' | '\u{2018}' | '\u{2019}' | '\u{201A}' | '\u{201B}')
}

/// `value` as a PowerShell single-quoted string: each quote character is
/// doubled, as PowerShell's own escaping does.
pub(super) fn powershell_quote(value: &str) -> String {
    let mut quoted = String::from("'");
    for c in value.chars() {
        if powershell_quote_char(c) {
            quoted.push(c);
        }
        quoted.push(c);
    }
    quoted.push('\'');
    quoted
}

/// What follows a PowerShell single-quoted string at the start of `text`.
fn after_powershell_quoted(text: &str) -> Option<&str> {
    let rest = text.strip_prefix('\'')?;
    let mut chars = rest.char_indices().peekable();
    while let Some((at, c)) = chars.next() {
        if powershell_quote_char(c) {
            if chars
                .peek()
                .is_some_and(|&(_, next)| powershell_quote_char(next))
            {
                chars.next();
                continue;
            }
            return Some(&rest[at + c.len_utf8()..]);
        }
    }
    None
}

/// A command `rerun_command` builds, parsed whole: `& '<native>'
/// extractor-shim install-autostart <kind>`, then `--repo '<path>'` and/or
/// `--env-file '<path>'`, `--native-exe '<path>'`, and optionally `--name
/// '<name>'`, `--task-logon-type Interactive|S4U`, `--startup-timeout
/// <seconds>` and `--receipt-dir '<path>'`, each at most once and nothing else.
fn generated_rerun(command: &str) -> bool {
    let Some(mut rest) = command.strip_prefix("& ").and_then(after_powershell_quoted) else {
        return false;
    };
    let Some(kind_rest) = rest.strip_prefix(" extractor-shim install-autostart ") else {
        return false;
    };
    let Some(after_kind) = ["claude", "codex"]
        .iter()
        .find_map(|kind| kind_rest.strip_prefix(kind))
    else {
        return false;
    };
    rest = after_kind;
    let mut seen: Vec<&str> = vec![];
    while !rest.is_empty() {
        let Some(flagged) = rest.strip_prefix(" --") else {
            return false;
        };
        let Some((flag, value)) = flagged.split_once(' ') else {
            return false;
        };
        if seen.contains(&flag) {
            return false;
        }
        rest = match flag {
            "repo" | "env-file" | "native-exe" | "name" | "receipt-dir" => {
                match after_powershell_quoted(value) {
                    Some(after) => after,
                    None => return false,
                }
            }
            "task-logon-type" => match ["Interactive", "S4U"]
                .iter()
                .find_map(|logon| value.strip_prefix(logon))
            {
                Some(after) => after,
                None => return false,
            },
            "startup-timeout" => {
                let digits = value.bytes().take_while(u8::is_ascii_digit).count();
                if digits == 0 {
                    return false;
                }
                &value[digits..]
            }
            _ => return false,
        };
        seen.push(flag);
    }
    seen.contains(&"native-exe") && (seen.contains(&"repo") || seen.contains(&"env-file"))
}

/// A Windows first line naming a registration's task and rerun.
fn windows_line(line: &str) -> bool {
    line.strip_prefix(BEGIN_WINDOWS_PREFIX)
        .and_then(after_powershell_quoted)
        .and_then(|rest| rest.strip_prefix(BEGIN_WINDOWS_INFIX))
        .and_then(|rest| rest.strip_suffix(BEGIN_WINDOWS_SUFFIX))
        .is_some_and(generated_rerun)
}

fn windows_begin(task: &str, command: &str) -> String {
    format!(
        "{BEGIN_WINDOWS_PREFIX}{}{BEGIN_WINDOWS_INFIX}{command}{BEGIN_WINDOWS_SUFFIX}",
        powershell_quote(task)
    )
}

/// The Windows first line for a write: the registration's, when
/// install-autostart names it; else the file's own (a bare `config` keeps
/// the registration's task, name and logon type); else the fixed text.
fn windows_chosen(rerun: Option<(&str, &str)>, raw: &str) -> String {
    match rerun {
        Some((task, command)) => windows_begin(task, command),
        None => raw
            .lines()
            .map(str::trim)
            .find(|line| windows_line(line))
            .map_or_else(|| BEGIN_WINDOWS_UNKNOWN.into(), str::to_owned),
    }
}

/// This platform's first line for a write.
fn chosen_begin(rerun: Option<(&str, &str)>, raw: &str) -> String {
    if cfg!(windows) {
        windows_chosen(rerun, raw)
    } else if cfg!(target_os = "linux") {
        BEGIN_LINUX.into()
    } else {
        BEGIN_OTHER.into()
    }
}

/// Every first line a managed block has been written with, matched exactly
/// (the Windows one by its whole generated structure) so a block from any
/// platform or release is replaced, never duplicated.
fn is_begin(line: &str) -> bool {
    let line = line.trim();
    [
        BEGIN_LINUX,
        BEGIN_OTHER,
        BEGIN_PYTHON,
        BEGIN_WINDOWS_UNKNOWN,
    ]
    .contains(&line)
        || windows_line(line)
}

/// What a Windows registration is run again with to apply an edit of its
/// settings file: everything install-autostart was given that the file does
/// not keep.
pub(super) struct Rerun<'a> {
    pub native: &'a Path,
    pub kind: &'a str,
    pub repo: Option<&'a Path>,
    pub env_file: &'a Path,
    pub name: Option<&'a str>,
    pub logon: Option<&'a str>,
    pub startup_timeout: Option<&'a str>,
    pub receipt_dir: Option<&'a Path>,
}

/// The PowerShell command that registers this shim again with the settings
/// in its file: the way an edit applies on Windows.
pub(super) fn rerun_command(rerun: &Rerun) -> String {
    let path = |p: &Path| powershell_quote(&p.display().to_string());
    let native = path(rerun.native);
    let mut words = vec![
        format!("& {native}"),
        "extractor-shim".into(),
        "install-autostart".into(),
        rerun.kind.into(),
    ];
    match rerun.repo {
        Some(repo) => {
            words.extend(["--repo".into(), path(repo)]);
            if rerun.env_file != repo.join("ops/.env") {
                words.extend(["--env-file".into(), path(rerun.env_file)]);
            }
        }
        None => words.extend(["--env-file".into(), path(rerun.env_file)]),
    }
    words.extend(["--native-exe".into(), native]);
    if let Some(name) = rerun.name {
        words.extend(["--name".into(), powershell_quote(name)]);
    }
    if let Some(logon) = rerun.logon {
        words.extend(["--task-logon-type".into(), logon.into()]);
    }
    if let Some(timeout) = rerun.startup_timeout {
        words.extend(["--startup-timeout".into(), timeout.into()]);
    }
    if let Some(receipt_dir) = rerun.receipt_dir {
        words.extend(["--receipt-dir".into(), path(receipt_dir)]);
    }
    words.join(" ")
}

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

pub(super) fn env_file(path: &Path) -> BTreeMap<String, String> {
    let raw = fs::read(path).unwrap_or_default();
    String::from_utf8_lossy(&raw)
        .lines()
        .filter_map(env_line)
        .collect()
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
    rerun: Option<(&str, &str)>,
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
    let mut outside = |line: &str| {
        if let Some((k, v)) = env_line(line).filter(|(k, _)| k.starts_with(&kind.prefix())) {
            moved.insert(k, v);
        } else {
            kept.push(line.to_owned());
        }
    };
    // A block is a first line, then lines, then the last line. A first line
    // with no last line before the next first line or the end of the file
    // owns nothing: ops/shim_autostart.py keeps a first line it does not
    // know as an ordinary line, drops the shared last line and appends its
    // own block, so the lines after that stray first line are the user's.
    let mut open: Option<Vec<&str>> = None;
    for line in raw.lines() {
        if is_begin(line) {
            for stray in open.replace(vec![]).unwrap_or_default() {
                outside(stray);
            }
            continue;
        }
        if line.trim() == END {
            for inner in open.take().unwrap_or_default() {
                if let Some((k, v)) = env_line(inner) {
                    block.insert(k, v);
                }
            }
            continue;
        }
        match open.as_mut() {
            Some(lines) => lines.push(line),
            None => outside(line),
        }
    }
    for stray in open.unwrap_or_default() {
        outside(stray);
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
    kept.push(chosen_begin(rerun, &raw));
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
            "receipt-dir",
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
            "rerun-task",
            "rerun-command",
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
    let receipt_dir = opts
        .remove("receipt-dir")
        .map(|p| absolute_path(Path::new(&p)))
        .transpose()?;
    if writing {
        for key in ["model", "reasoning-effort"] {
            if let Some(value) = opts.get(key).filter(|s| !s.is_empty()) {
                validate_cli_token(key, value)?;
            }
        }
        let settings_path = settings_path
            .as_deref()
            .ok_or("config needs --repo or --env-file")?;
        // install-autostart names its registration's task and rerun; a bare
        // config keeps the file's.
        let task = opts.remove("rerun-task");
        let command = opts.remove("rerun-command");
        write_config(
            repo.as_deref().unwrap_or(Path::new(".")),
            settings_path,
            kind,
            &opts,
            task.as_deref().zip(command.as_deref()),
        )?;
        return Ok(None);
    }
    opts.remove("rerun-task");
    opts.remove("rerun-command");
    let managed = repo.is_some() || settings_path.is_some();
    let receipt_dir = if managed {
        Some(receipt_dir.map_or_else(default_receipt_dir, Ok)?)
    } else {
        receipt_dir
    };
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
    if let Some(path) = &settings_path {
        for (k, v) in env_file(path) {
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
        receipt_dir,
    }))
}

#[cfg(test)]
mod block_tests {
    use super::{
        BEGIN_WINDOWS_UNKNOWN, Kind, Rerun, chosen_begin, is_begin, rerun_command, windows_begin,
        windows_chosen, write_config,
    };
    use std::{
        collections::BTreeMap,
        fs,
        path::{Path, PathBuf},
    };

    const PYTHON_MARKER: &str = "# >>> pseudolife shim autostart (managed by ops/shim_autostart.py; edit values here, then: python ops/shim_autostart.py restart claude|codex) >>>";
    const END: &str = "# <<< pseudolife shim autostart <<<";
    // What install-autostart passes as --rerun-task and --rerun-command.
    const TASK: &str = "Pseudolife Claude Shim";
    const RERUN: &str = r"& 'C:\fixture\bin\pseudolife-mcp.exe' extractor-shim install-autostart claude --repo 'C:\fixture\it''s repo' --native-exe 'C:\fixture\bin\pseudolife-mcp.exe'";
    // The installer's own block (cli/installer/state.rs), which shares the file.
    const INSTALL_BEGIN: &str =
        "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>";
    const INSTALL_END: &str = "# <<< pseudolife-mcp install <<<";

    /// The first line an install-autostart write gives this platform.
    fn this_begin() -> String {
        chosen_begin(Some((TASK, RERUN)), "")
    }

    struct Repo(PathBuf);
    impl Drop for Repo {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn write_with(
        seed: Option<&str>,
        kind: Kind,
        values: &[(&str, &str)],
        rerun: Option<(&str, &str)>,
    ) -> String {
        let repo = Repo(std::env::temp_dir().join(format!("shim-block-{}", uuid::Uuid::new_v4())));
        fs::create_dir_all(repo.0.join("ops")).unwrap();
        let env = repo.0.join("ops/.env");
        if let Some(seed) = seed {
            fs::write(&env, seed).unwrap();
        }
        let values: BTreeMap<String, String> = values
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        write_config(&repo.0, &env, kind, &values, rerun).unwrap();
        fs::read_to_string(&env).unwrap()
    }

    fn write(seed: Option<&str>, kind: Kind, values: &[(&str, &str)]) -> String {
        write_with(seed, kind, values, Some((TASK, RERUN)))
    }

    fn begin_lines(text: &str) -> Vec<&str> {
        text.lines()
            .filter(|l| l.starts_with("# >>> pseudolife shim autostart"))
            .collect()
    }

    #[test]
    fn the_block_names_this_platforms_step_not_python() {
        let text = write(None, Kind::Claude, &[("port", "8082")]);
        let begins = begin_lines(&text);
        assert_eq!(begins.len(), 1, "{text}");
        let begin = begins[0];
        assert!(!begin.contains("shim_autostart.py"), "{begin}");
        if cfg!(windows) {
            // Stopping and starting the task leaves the old shim on its port.
            assert_eq!(
                begin,
                format!(
                    "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'Pseudolife Claude Shim' is registered, run in PowerShell: {RERUN}) >>>"
                )
            );
        } else if cfg!(target_os = "linux") {
            assert!(
                begin.contains("systemctl --user restart pseudolife-sonnet-shim.service")
                    && begin.contains("pseudolife-codex-shim.service"),
                "{begin}"
            );
        }
    }

    #[test]
    fn the_windows_line_names_its_task_and_rerun_on_any_host() {
        assert_eq!(
            windows_chosen(Some(("Team’s Shim", RERUN)), ""),
            format!(
                "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'Team’’s Shim' is registered, run in PowerShell: {RERUN}) >>>"
            )
        );
        assert!(is_begin(&windows_begin("Team’s Shim", RERUN)));
    }

    #[test]
    fn a_python_marker_block_is_replaced_not_duplicated() {
        let seed = format!(
            "UNRELATED=kept\n# a comment that stays\n\n{PYTHON_MARKER}\nPSEUDOLIFE_CLAUDE_SHIM_MODEL=claude-opus-5\nPSEUDOLIFE_CODEX_SHIM_PORT=8086\n{END}\n"
        );
        let text = write(Some(&seed), Kind::Claude, &[("port", "9001")]);
        assert!(!text.contains(PYTHON_MARKER), "{text}");
        assert_eq!(begin_lines(&text).len(), 1, "{text}");
        assert_eq!(text.matches(END).count(), 1, "{text}");
        for line in [
            "UNRELATED=kept",
            "# a comment that stays",
            "PSEUDOLIFE_CLAUDE_SHIM_MODEL=claude-opus-5",
            "PSEUDOLIFE_CODEX_SHIM_PORT=8086",
            "PSEUDOLIFE_CLAUDE_SHIM_PORT=9001",
        ] {
            assert_eq!(
                text.lines().filter(|l| *l == line).count(),
                1,
                "{line}\n{text}"
            );
        }
    }

    #[test]
    fn a_block_this_writer_wrote_is_rewritten_in_one_piece() {
        let first = write(None, Kind::Codex, &[("port", "8086")]);
        let again = write(Some(&first), Kind::Codex, &[("model", "gpt-5.6-terra")]);
        assert_eq!(begin_lines(&again).len(), 1, "{again}");
        assert_eq!(again.matches(END).count(), 1, "{again}");
        assert!(
            again.contains("PSEUDOLIFE_CODEX_SHIM_PORT=8086\n"),
            "{again}"
        );
        // Unchanged settings leave the file byte-identical, a bare config
        // (no rerun named) included.
        assert_eq!(write(Some(&again), Kind::Codex, &[]), again);
        assert_eq!(write_with(Some(&again), Kind::Codex, &[], None), again);
    }

    #[test]
    fn every_shipped_marker_is_recognised() {
        // A file carried between hosts, or written by another platform's
        // build or an earlier release. Literal copies: a shipped first line
        // must keep matching even if the constants change.
        for begin in [
            "# >>> pseudolife shim autostart (edit values here, then: systemctl --user restart pseudolife-sonnet-shim.service for Claude, pseudolife-codex-shim.service for Codex, or the unit named at install with --name) >>>",
            r"# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'My Shim' is registered, run in PowerShell: & 'C:\Users\<user>\AppData\Local\pseudolife-mcp\bin\pseudolife-mcp.exe' extractor-shim install-autostart codex --repo 'C:\Users\<user>\repo' --native-exe 'C:\Users\<user>\AppData\Local\pseudolife-mcp\bin\pseudolife-mcp.exe' --name 'My Shim' --task-logon-type S4U) >>>",
            "# >>> pseudolife shim autostart (edit values here, then run again, in PowerShell, the pseudolife-mcp extractor-shim install-autostart command this shim was registered with) >>>",
            "# >>> pseudolife shim autostart (edit values here, then stop the extractor shim you started and start it again) >>>",
            PYTHON_MARKER,
        ] {
            assert!(is_begin(begin), "{begin}");
            let seed = format!("{begin}\nPSEUDOLIFE_CLAUDE_SHIM_PORT=1234\n{END}\n");
            let text = write(Some(&seed), Kind::Claude, &[]);
            assert_eq!(begin_lines(&text), [this_begin()], "{begin}\n{text}");
            assert!(
                text.contains("PSEUDOLIFE_CLAUDE_SHIM_PORT=1234\n"),
                "{text}"
            );
        }
    }

    #[test]
    fn a_python_rewrite_of_a_native_block_keeps_every_other_line() {
        // What ops/shim_autostart.py write_config made of a native block with
        // user lines and the installer's block after it (run 26 November
        // 2026): it keeps the native first line it does not know as an
        // ordinary line, drops the shared last line and appends its own
        // block. Nothing between that stray first line and the Python block
        // belongs to the shim block.
        let python_rewrite = format!(
            "UNRELATED=kept\n\n{}\nPSEUDOLIFE_CODEX_SHIM_PORT=8086\n# a user comment after the block\nPSEUDOLIFE_API_TOKEN=${{TOKEN_FROM_SHELL}}\nexport OTHER=\"x y\"\n{INSTALL_BEGIN}\nPSEUDOLIFE_INSTALL_MODE=docker\n{INSTALL_END}\n\n{PYTHON_MARKER}\nPSEUDOLIFE_CLAUDE_SHIM_MODEL=claude-opus-5-5\nPSEUDOLIFE_CLAUDE_SHIM_PORT=8082\n{END}\n",
            this_begin()
        );
        let text = write(Some(&python_rewrite), Kind::Claude, &[]);
        assert_eq!(
            text,
            format!(
                "UNRELATED=kept\n\nPSEUDOLIFE_CODEX_SHIM_PORT=8086\n# a user comment after the block\nPSEUDOLIFE_API_TOKEN=${{TOKEN_FROM_SHELL}}\nexport OTHER=\"x y\"\n{INSTALL_BEGIN}\nPSEUDOLIFE_INSTALL_MODE=docker\n{INSTALL_END}\n\n{}\nPSEUDOLIFE_CLAUDE_SHIM_MODEL=claude-opus-5-5\nPSEUDOLIFE_CLAUDE_SHIM_PORT=8082\n{END}\n",
                this_begin()
            )
        );
    }

    #[test]
    fn a_first_line_with_no_last_line_owns_nothing() {
        let seed = format!(
            "{}\n# kept comment\nOTHER=kept\nPSEUDOLIFE_CLAUDE_SHIM_PORT=1234\n",
            this_begin()
        );
        let text = write(Some(&seed), Kind::Claude, &[]);
        assert_eq!(
            text,
            format!(
                "# kept comment\nOTHER=kept\n\n{}\nPSEUDOLIFE_CLAUDE_SHIM_PORT=1234\n{END}\n",
                this_begin()
            )
        );
    }

    #[test]
    fn a_comment_that_only_resembles_the_marker_is_left_alone() {
        let near = [
            "# >>> pseudolife shim autostart (my own notes) >>>",
            "# >>> pseudolife shim autostart >>>",
            "## >>> pseudolife shim autostart (managed by ops/shim_autostart.py; edit values here, then: python ops/shim_autostart.py restart claude|codex) >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: ) >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: python ops/shim_autostart.py restart claude) >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: & 'pseudolife-mcp.exe' extractor-shim install-autostart --help) >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: & 'x' extractor-shim install-autostart claude --repo 'r') >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task X is registered, run in PowerShell: & 'x' extractor-shim install-autostart claude --repo 'r' --native-exe 'x') >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: & 'x' extractor-shim install-autostart claude --repo ' --native-exe ') >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: & 'x' extractor-shim install-autostart claude --repo 'r' --native-exe 'x'; Remove-Item 'y') >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: & 'x' extractor-shim install-autostart claude --repo 'r' --native-exe 'x' --native-exe 'z') >>>",
            "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: & 'x' extractor-shim install-autostart claude --repo 'r' --native-exe 'x' --task-logon-type Admin) >>>",
        ];
        let seed = format!("{}\nNOT_A_SHIM_KEY=stays\n", near.join("\n"));
        let text = write(Some(&seed), Kind::Claude, &[("port", "8082")]);
        for line in near {
            assert!(!is_begin(line), "{line}");
            assert_eq!(
                text.lines().filter(|l| *l == line).count(),
                1,
                "{line}\n{text}"
            );
        }
        assert!(text.contains("NOT_A_SHIM_KEY=stays\n"), "{text}");
        assert_eq!(text.matches(this_begin().as_str()).count(), 1, "{text}");
        // The near misses come before the managed block, which is still the
        // file's only block and ends it.
        assert!(
            text.ends_with(&format!(
                "{}\nPSEUDOLIFE_CLAUDE_SHIM_PORT=8082\n{END}\n",
                this_begin()
            )),
            "{text}"
        );
    }

    #[test]
    fn a_write_that_names_no_rerun_keeps_the_registrations_and_never_guesses() {
        // `extractor-shim config` after install-autostart: the registration's
        // task and rerun (its --name, its logon type) stay the file's.
        let theirs = windows_begin(
            "Mine",
            r"& 'C:\x\pseudolife-mcp.exe' extractor-shim install-autostart claude --repo 'C:\x' --native-exe 'C:\x\pseudolife-mcp.exe' --name 'Mine' --task-logon-type S4U",
        );
        let raw = format!("OTHER=1\n{theirs}\nPSEUDOLIFE_CLAUDE_SHIM_PORT=1\n{END}\n");
        assert_eq!(windows_chosen(None, &raw), theirs);
        assert_eq!(
            windows_chosen(Some(("Given", RERUN)), &raw),
            windows_begin("Given", RERUN)
        );
        // Nothing to keep (no file, an earlier release's Python marker, a
        // look-alike comment): the fixed text, never a guessed registration.
        let near = "# >>> pseudolife shim autostart (edit values here, then, while the scheduled task 'X' is registered, run in PowerShell: & 'x' extractor-shim config claude) >>>\n";
        for raw in [
            "OTHER=1\n".to_owned(),
            format!("{PYTHON_MARKER}\n{END}\n"),
            near.into(),
        ] {
            assert_eq!(windows_chosen(None, &raw), BEGIN_WINDOWS_UNKNOWN, "{raw}");
        }
    }

    #[test]
    fn the_windows_rerun_names_every_value_its_registration_needs() {
        let native = Path::new(r"C:\Users\<user>\bin\pseudolife-mcp.exe");
        let repo = Path::new(r"C:\Users\<user>\Team’s repo");
        let env = repo.join("ops/.env");
        let plain = Rerun {
            native,
            kind: "claude",
            repo: Some(repo),
            env_file: &env,
            name: None,
            logon: None,
            startup_timeout: None,
            receipt_dir: None,
        };
        let repo_rerun = rerun_command(&plain);
        assert_eq!(
            repo_rerun,
            r"& 'C:\Users\<user>\bin\pseudolife-mcp.exe' extractor-shim install-autostart claude --repo 'C:\Users\<user>\Team’’s repo' --native-exe 'C:\Users\<user>\bin\pseudolife-mcp.exe'"
        );
        let other_env = Path::new(r"C:\Users\<user>\shims.env");
        let both = rerun_command(&Rerun {
            env_file: other_env,
            ..plain
        });
        assert!(
            both.contains(
                r" --repo 'C:\Users\<user>\Team’’s repo' --env-file 'C:\Users\<user>\shims.env' "
            ),
            "{both}"
        );
        let receipts = Path::new(r"C:\Users\<user>\receipts");
        let named_rerun = rerun_command(&Rerun {
            kind: "codex",
            repo: None,
            env_file: other_env,
            name: Some("Shim 'beta'"),
            logon: Some("S4U"),
            startup_timeout: Some("240"),
            receipt_dir: Some(receipts),
            ..plain
        });
        assert_eq!(
            named_rerun,
            r"& 'C:\Users\<user>\bin\pseudolife-mcp.exe' extractor-shim install-autostart codex --env-file 'C:\Users\<user>\shims.env' --native-exe 'C:\Users\<user>\bin\pseudolife-mcp.exe' --name 'Shim ''beta''' --task-logon-type S4U --startup-timeout 240 --receipt-dir 'C:\Users\<user>\receipts'"
        );
        // Every rerun it builds is recognised in the line that names it.
        for rerun in [RERUN, &repo_rerun, &both, &named_rerun] {
            assert!(is_begin(&windows_begin("Shim ’x’", rerun)), "{rerun}");
        }
    }
}

#[cfg(test)]
mod path_tests {
    use super::windows_path_without_verbatim_prefix;

    #[test]
    fn windows_verbatim_short_drive_and_unc_paths() {
        for (verbatim, plain) in [
            (r"\\?\C:\", r"C:\"),
            (
                r"\\?\C:\cache\.config\COM0.txt",
                r"C:\cache\.config\COM0.txt",
            ),
            (r"\\?\UNC\server\share", r"\\server\share"),
            (r"\\?\UNC\server\share\cache\", r"\\server\share\cache\"),
            (r"C:\cache\file", r"C:\cache\file"),
        ] {
            assert_eq!(windows_path_without_verbatim_prefix(verbatim), plain);
        }
    }

    #[test]
    fn windows_verbatim_max_path_boundary() {
        for root in [r"C:\", r"\\server\share\"] {
            let prefix = if let Some(unc) = root.strip_prefix(r"\\") {
                format!(r"\\?\UNC\{unc}")
            } else {
                format!(r"\\?\{root}")
            };
            // The final component can take a short existing ancestor over
            // MAX_PATH, so the decision must include every appended component.
            let middle = format!("{}\\", "a".repeat(200));
            let short_tail = "b".repeat(259 - root.encode_utf16().count() - middle.len());
            let short = format!("{prefix}{middle}{short_tail}");
            let plain = format!("{root}{middle}{short_tail}");
            assert_eq!(plain.encode_utf16().count(), 259);
            assert_eq!(windows_path_without_verbatim_prefix(&short), plain);
            let long = format!("{short}b");
            assert_eq!(windows_path_without_verbatim_prefix(&long), long);
        }
    }

    #[test]
    fn windows_verbatim_length_counts_utf16_units() {
        let plain = format!("C:\\{}\\{}", "😀".repeat(100), "b".repeat(55));
        let short = format!(r"\\?\{plain}");
        assert_eq!(plain.encode_utf16().count(), 259);
        assert_eq!(windows_path_without_verbatim_prefix(&short), plain);
        let long = format!("{short}b");
        assert!(long.chars().count() < 260);
        assert_eq!(windows_path_without_verbatim_prefix(&long), long);
    }

    #[test]
    fn windows_verbatim_namespaces_and_normalization_are_preserved() {
        for path in [
            r"\\?\Volume{00000000-0000-0000-0000-000000000000}\cache",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1\cache",
            r"\\?\C:relative",
            r"\\?\UNC\server",
            r"\\?\C:\cache\name.",
            r"\\?\C:\cache\name ",
            r"\\?\C:\cache\..\file",
            r"\\?\C:\cache/file",
            r"\\?\C:\\",
            r"\\?\C:\cache\\file",
            r"\\?\C:\cache\NUL.txt",
            r"\\?\C:\cache\com¹.txt",
        ] {
            assert_eq!(windows_path_without_verbatim_prefix(path), path);
        }
    }
}
