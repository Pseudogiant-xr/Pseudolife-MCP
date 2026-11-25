//! The extractor shim autostart step of the client ladder.
//!
//! An install with the `claude-*` or `openai-*` extractor modes registered a
//! shim autostart: a systemd user unit on Linux, a scheduled task on Windows
//! (no other platform has one). A Python-client install registered one that
//! runs `ops/shim_autostart.py run <kind>` under Python, which an update that
//! moves the machine to the native client leaves in place. This step reads
//! the registrations and changes nothing: for that runner it names the one
//! native command that moves it (`extractor-shim install-autostart`, which
//! stops the Python shim, registers the native one under the same name with
//! the settings both runners read from `<checkout>/ops/.env`, checks that it
//! serves and puts everything back when it does not). Moving it inside the
//! update is left out on purpose: an update without a person never changes
//! the engine (maintainer ruling, 20 November 2026), a handed-off update
//! cannot tell which it is, and the move has effects nobody chose (it enables
//! a disabled unit, replaces the pre-rename Sonnet task).
use super::{Output, registration_text, result};
use serde_json::Value;
use std::path::{Path, PathBuf};

/// The default registration names, as `extractor-shim install-autostart`
/// and the Python-era installers write them.
const REGISTRATIONS: [(&str, &str, &str); 2] = [
    (
        "claude",
        "Pseudolife Claude Shim",
        "pseudolife-sonnet-shim.service",
    ),
    (
        "codex",
        "Pseudolife Codex Shim",
        "pseudolife-codex-shim.service",
    ),
];

/// What a registration's definition says it runs.
#[derive(Debug, PartialEq)]
pub enum Registered {
    Disabled,
    /// Exactly the Python-era runner, `<python> <repo>/ops/shim_autostart.py
    /// run <kind>`: its settings live in `<repo>/ops/.env`.
    PythonRunner {
        repo: PathBuf,
    },
    /// Another Python-era form (the runner in a shape not recognised
    /// exactly, or a shim script with its values on its command line).
    PythonOther,
    ModelFlags,
    Other,
}

/// Classifies a `systemctl --user cat` unit or a `schtasks /XML` task.
pub fn classify(text: &str, kind: &str) -> Registered {
    if text
        .to_ascii_lowercase()
        .contains("<enabled>false</enabled>")
    {
        return Registered::Disabled;
    }
    if let Some(repo) = unit_runner_repo(text, kind).or_else(|| task_runner_repo(text, kind)) {
        return Registered::PythonRunner { repo };
    }
    let action = action_text(text);
    if action.contains("shim_autostart.py")
        || ["claude", "codex", "sonnet"]
            .iter()
            .any(|k| action.contains(&format!("{k}_shim.py")))
    {
        return Registered::PythonOther;
    }
    if action.contains("--model") || action.contains("-Model") {
        return Registered::ModelFlags;
    }
    Registered::Other
}

fn python_interpreter(path: &str) -> bool {
    let name = path.rsplit(['/', '\\']).next().unwrap_or("");
    let stem = name.strip_suffix(".exe").unwrap_or(name);
    // `python`, `pythonw`, `python3`, `python3.13`: autostart.ps1's own rule.
    stem.strip_prefix("python")
        .is_some_and(|rest| rest == "w" || rest.bytes().all(|b| b.is_ascii_digit() || b == b'.'))
}

/// `<repo>/ops/shim_autostart.py` (or with backslashes, as the Windows
/// installer wrote it) for an absolute `<repo>`, on any host.
fn runner_repo(script: &str) -> Option<PathBuf> {
    let repo = script
        .strip_suffix("/ops/shim_autostart.py")
        .or_else(|| script.strip_suffix("\\ops\\shim_autostart.py"))?;
    let bytes = repo.as_bytes();
    let absolute = repo.starts_with('/')
        || repo.starts_with("\\\\")
        || (bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && &bytes[1..3] == b":\\");
    absolute.then(|| PathBuf::from(repo))
}

/// What a registration runs, without its description: a task's XML minus
/// `<Description>`, or a unit's `ExecStart=` lines.
fn action_text(text: &str) -> String {
    if let Some(start) = text.find("<Task") {
        let mut rest = text[start..].to_owned();
        while let (Some(a), Some(b)) = (rest.find("<Description>"), rest.find("</Description>")) {
            if b < a {
                break;
            }
            rest.replace_range(a..b + "</Description>".len(), "");
        }
        format!("{}{rest}", &text[..start])
    } else {
        text.lines()
            .filter(|l| l.trim().starts_with("ExecStart="))
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// The unit `ops/install-*shim-autostart.sh` wrote:
/// `ExecStart="<python>" "<repo>/ops/shim_autostart.py" run <kind> --foreground`.
fn unit_runner_repo(text: &str, kind: &str) -> Option<PathBuf> {
    let starts: Vec<_> = text
        .lines()
        .filter_map(|l| l.trim().strip_prefix("ExecStart="))
        .collect();
    let [start] = starts.as_slice() else {
        return None;
    };
    let words = systemd_words(start)?;
    match words.as_slice() {
        [python, script, run, provider, foreground]
            if python_interpreter(python)
                && run == "run"
                && provider == kind
                && foreground == "--foreground" =>
        {
            runner_repo(script)
        }
        _ => None,
    }
}

/// systemd's word splitting for the forms the installer wrote: words
/// separated by spaces, each optionally double-quoted with backslash
/// escapes. Anything else (an unterminated quote) is not split.
fn systemd_words(line: &str) -> Option<Vec<String>> {
    let mut words = vec![];
    let mut chars = line.trim().chars().peekable();
    while let Some(&c) = chars.peek() {
        if c == ' ' || c == '\t' {
            chars.next();
            continue;
        }
        let mut word = String::new();
        if c == '"' {
            chars.next();
            loop {
                match chars.next()? {
                    '"' => break,
                    '\\' => word.push(chars.next()?),
                    other => word.push(other),
                }
            }
        } else {
            while let Some(&c) = chars.peek() {
                if c == ' ' || c == '\t' {
                    break;
                }
                word.push(c);
                chars.next();
            }
        }
        words.push(word);
    }
    Some(words)
}

/// The task `ops/install-*shim-autostart.ps1` registered: `powershell.exe`
/// running an encoded command whose start info is
/// `FileName = '<python>'` and `Arguments = '"<repo>\ops\shim_autostart.py" run <kind>'`.
/// `registration_text` appends the decoded command to the task XML.
fn task_runner_repo(text: &str, kind: &str) -> Option<PathBuf> {
    let quoted = |prefix: &str| -> Option<String> {
        let found: Vec<_> = text
            .lines()
            .filter_map(|l| l.trim().strip_prefix(prefix))
            .collect();
        let [value] = found.as_slice() else {
            return None;
        };
        let inner = value.strip_prefix('\'')?.strip_suffix('\'')?;
        // PowerShell doubles a single quote inside a single-quoted string.
        if inner.replace("''", "").contains('\'') {
            return None;
        }
        Some(inner.replace("''", "'"))
    };
    let python = quoted("$psi.FileName = ")?;
    let arguments = quoted("$psi.Arguments = ")?;
    if !python_interpreter(&python) || !text.contains("System.Diagnostics.ProcessStartInfo") {
        return None;
    }
    let rest = arguments.strip_prefix('"')?;
    let (script, tail) = rest.split_once('"')?;
    (tail == format!(" run {kind}"))
        .then(|| runner_repo(script))
        .flatten()
}

/// The characters PowerShell accepts as a single-quote delimiter.
fn powershell_quote_char(c: char) -> bool {
    matches!(c, '\'' | '\u{2018}' | '\u{2019}' | '\u{201A}' | '\u{201B}')
}

fn quoted_command(words: &[String]) -> String {
    let quote = |w: &String| {
        if cfg!(windows) {
            let mut quoted = String::from("'");
            for c in w.chars() {
                if powershell_quote_char(c) {
                    quoted.push(c);
                }
                quoted.push(c);
            }
            quoted.push('\'');
            quoted
        } else if w
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/._-+=:@%".contains(&b))
        {
            w.clone()
        } else {
            format!("'{}'", w.replace('\'', "'\\''"))
        }
    };
    let program = quote(&words[0]);
    let rest: Vec<_> = words[1..].iter().map(quote).collect();
    let line = format!("{program} {}", rest.join(" "));
    // PowerShell runs a quoted path only after its call operator.
    if cfg!(windows) {
        format!("& {line}")
    } else {
        line
    }
}

/// The native extractor setup that moves `kind`'s Python runner registration
/// with the settings in `<repo>/ops/.env`.
pub fn move_command(launcher: &Path, kind: &str, repo: &Path) -> String {
    let mut words: Vec<String> = vec![
        launcher.display().to_string(),
        "extractor-shim".into(),
        "install-autostart".into(),
        kind.into(),
        "--repo".into(),
        repo.display().to_string(),
        "--native-exe".into(),
        launcher.display().to_string(),
    ];
    // Without --log the native setup writes its default log path into the
    // settings and so replaces the one this install chose. The Python runner
    // ran in the checkout, so a relative path is the checkout's.
    if let Some(log) =
        crate::extractor_shim::configured_setting(&repo.join("ops/.env"), kind, "LOG")
    {
        let log = if log.starts_with("~/") || Path::new(&log).is_absolute() {
            log
        } else {
            repo.join(&log).display().to_string()
        };
        words.extend(["--log".into(), log]);
    }
    quoted_command(&words)
}

/// The step: reports each default registration. `launcher` is the stable
/// launcher when it is the native client's; the command a Python runner
/// note names runs it.
pub fn check_autostart(
    query: &dyn Fn(&str, &str, &str) -> Option<Output>,
    launcher: Option<&Path>,
    launcher_path: &Path,
) -> Value {
    let mut notes = Vec::new();
    let mut registered = 0;
    for (kind, task, unit) in REGISTRATIONS {
        let Some(output) = query(kind, task, unit) else {
            continue;
        };
        if output.code != 0 {
            continue;
        } // Informational, as in the Python oracle.
        registered += 1;
        let text = registration_text(&output.bytes);
        match classify(&text, kind) {
            Registered::Disabled => notes.push(format!("{kind}: the extractor autostart task is disabled; rerun native extractor setup if it should start at logon")),
            Registered::PythonRunner { repo } => {
                let runner = repo.join("ops/shim_autostart.py");
                notes.push(match launcher {
                    Some(native) => format!(
                        "{kind}: extractor autostart still runs the legacy Python runner ({}); to move it to the native shim with the same settings, run {}",
                        runner.display(),
                        move_command(native, kind, &repo)
                    ),
                    None => format!(
                        "{kind}: extractor autostart still runs the legacy Python runner ({}); once this machine runs the native client (pseudolife-mcp update --clients-only), move it to the native shim with {}",
                        runner.display(),
                        move_command(launcher_path, kind, &repo)
                    ),
                });
            }
            Registered::PythonOther => notes.push(format!("{kind}: extractor autostart still names the legacy Python runner; rerun native extractor setup to migrate it")),
            Registered::ModelFlags => notes.push(format!("{kind}: extractor autostart carries model values on its command line; rerun native extractor setup to read current configuration at logon")),
            Registered::Other => {}
        }
    }
    if !notes.is_empty() {
        return serde_json::json!({"state":"stale","detail":notes.join("; "),"notes":notes});
    }
    result(
        if registered > 0 { "current" } else { "none" },
        if registered > 0 {
            "registered extractor autostart has no legacy runner or baked model override"
        } else {
            "no extractor autostart task or unit was found"
        },
    )
}
