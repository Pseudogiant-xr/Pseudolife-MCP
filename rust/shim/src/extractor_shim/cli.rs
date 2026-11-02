use super::{
    config::{Config, Kind},
    json,
};
use process_wrap::tokio::*;
use serde_json::Value;
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::PathBuf,
    process::Stdio,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

#[derive(Debug)]
pub(super) struct Failure {
    pub message: String,
    pub sign_in: bool,
}
impl From<String> for Failure {
    fn from(message: String) -> Self {
        Self {
            message,
            sign_in: false,
        }
    }
}
impl From<std::io::Error> for Failure {
    fn from(e: std::io::Error) -> Self {
        e.to_string().into()
    }
}

pub(super) fn sign_in(kind: Kind, text: &str) -> bool {
    let pattern = if kind == Kind::Claude {
        r"(?i)failed to authenticate|authentication_error|authentication_failed|oauth (?:token|session)|not logged in|please run /login|please log ?in again|invalid api key|\bunauthori[sz]ed\b|api error: 401\b"
    } else {
        r"(?i)not logged in|codex login|\bunauthori[sz]ed\b|token_expired|token (?:has )?expired|could not be refreshed|incorrect api key|invalid_api_key"
    };
    regex::Regex::new(pattern)
        .expect("static regex")
        .is_match(text)
}

pub(super) fn strip_fence(s: &str) -> String {
    let re = regex::Regex::new(r"(?s)^\s*```(?:json)?\s*(.*?)\s*```\s*$").expect("static regex");
    re.captures(s)
        .map(|m| m[1].trim().to_owned())
        .unwrap_or_else(|| s.trim().to_owned())
}

fn bounded(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        return s.into();
    }
    format!(
        "{} ... {}",
        json::head(s, n / 3),
        s.chars()
            .skip(s.chars().count() - (n - n / 3 - 5))
            .collect::<String>()
    )
}

fn cli_output(kind: Kind, out: &str, err: &str) -> (String, String) {
    let mut streams = vec![];
    let mut events = 0;
    for (name, raw) in [("stdout", out), ("stderr", err)] {
        let mut text = raw.trim().to_owned();
        if name == "stdout" {
            if kind == Kind::Claude {
                if let Ok(v) = serde_json::from_str::<Value>(&text) {
                    let v = if json::truth(&v["result"]) {
                        &v["result"]
                    } else {
                        &v["error"]
                    };
                    if json::truth(v) {
                        text = json::text(v);
                    }
                }
            } else {
                text = text
                    .lines()
                    .filter(|l| {
                        if l.trim_start().starts_with('{') {
                            events += 1;
                            false
                        } else {
                            true
                        }
                    })
                    .collect::<Vec<_>>()
                    .join(" ");
            }
        }
        let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        if !text.is_empty() {
            streams.push((name, text));
        }
    }
    let full = streams
        .iter()
        .map(|(_, s)| s.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    let limit = 480 / streams.len().max(1);
    let mut detail = streams
        .iter()
        .map(|(name, s)| format!("{name}: {}", bounded(s, limit)))
        .collect::<Vec<_>>()
        .join("; ");
    if detail.is_empty() {
        detail = if events > 0 {
            format!(
                "(no message: stdout had {events} JSON events and none an error; stderr was empty)"
            )
        } else {
            "(no output on stdout or stderr)".into()
        };
    }
    (detail, full)
}

fn codex_reply(out: &str) -> Result<String, (String, bool)> {
    let mut reply = None;
    for line in out.lines() {
        let Ok(ev) = serde_json::from_str::<Value>(line.trim()) else {
            continue;
        };
        match ev["type"].as_str() {
            Some("turn.failed" | "error") => {
                let msg = if json::truth(&ev["error"]["message"]) {
                    json::text(&ev["error"]["message"])
                } else if json::truth(&ev["message"]) {
                    json::text(&ev["message"])
                } else {
                    json::head(&json::dumps(&ev, true), 200)
                };
                return Err((format!("codex exec failed: {msg}"), true));
            }
            Some("item.completed") if ev["item"]["type"] == "agent_message" => {
                reply = Some(ev["item"]["text"].as_str().unwrap_or("").to_owned())
            }
            _ => {}
        }
    }
    reply.map(|s| strip_fence(&s)).ok_or_else(|| {
        (
            format!(
                "codex exec produced no agent_message (stream: {})",
                json::repr(&json::head(out, 200))
            ),
            false,
        )
    })
}

pub(super) struct Instructions(PathBuf);
impl Instructions {
    pub fn new() -> Result<Self, std::io::Error> {
        let path =
            std::env::temp_dir().join(format!("codex-shim-instr-{}.md", uuid::Uuid::new_v4()));
        let mut opts = OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        opts.open(&path)?;
        Ok(Self(path))
    }
}
impl Drop for Instructions {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

pub(super) async fn call(
    c: &Config,
    instructions: &mut Option<Instructions>,
    system: &str,
    user: &str,
    model: &str,
    effort: Option<&str>,
) -> Result<String, Failure> {
    let shipped = include_str!("../../../../evals/prompts/assistant_facts_provenance.txt")
        .replace("\r\n", "\n");
    let system = match c.system.as_deref().filter(|s| !s.is_empty()) {
        Some(override_) if system.starts_with(&shipped) => {
            format!("{override_}{}", &system[shipped.len()..])
        }
        _ => system.into(),
    };
    let mut payload = user.to_owned();
    let mut args: Vec<String> = if c.kind == Kind::Claude {
        [
            "-p",
            "--model",
            model,
            "--output-format",
            "json",
            "--strict-mcp-config",
            "--mcp-config",
            "{\"mcpServers\":{}}",
            "--tools",
            "",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect()
    } else {
        [
            "exec",
            "--model",
            model,
            "--json",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "-c",
            "features.hooks=false",
            "-c",
            "web_search=disabled",
            "-c",
            "features.shell_tool=false",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect()
    };
    if let Some(effort) = effort.or(c.effort.as_deref()).filter(|s| !s.is_empty()) {
        if c.kind == Kind::Claude {
            args.extend(["--effort".into(), effort.into()]);
        } else {
            args.extend(["-c".into(), format!("model_reasoning_effort={effort}")]);
        }
    }
    let mut claude_prompt = None;
    if c.kind == Kind::Claude {
        let batch = cfg!(windows)
            && c.cli.extension().is_some_and(|s| {
                matches!(
                    s.to_string_lossy().to_ascii_lowercase().as_str(),
                    "cmd" | "bat"
                )
            });
        if !system.is_empty() && batch {
            let file = Instructions::new()?;
            fs::write(&file.0, system.as_bytes())?;
            args.extend([
                "--system-prompt-file".into(),
                file.0.to_string_lossy().into_owned(),
            ]);
            claude_prompt = Some(file);
        } else if !system.is_empty() && system.chars().count() <= 24000 {
            args.extend(["--system-prompt".into(), system]);
        } else if !system.is_empty() {
            payload = format!("{system}\n\n{payload}");
        }
    } else {
        if !system.is_empty() {
            if instructions.is_none() {
                *instructions = Some(Instructions::new()?);
            }
            let path = &instructions.as_ref().expect("instructions").0;
            // Match Path.write_text's native newline translation.
            fs::write(
                path,
                if cfg!(windows) {
                    system.replace('\n', "\r\n")
                } else {
                    system
                },
            )?;
            args.extend([
                "-c".into(),
                format!("model_instructions_file={}", path.display()),
            ]);
        }
        args.push("-".into());
    }
    let mut command = CommandWrap::with_new(&c.cli, |cmd| {
        if let Some(cwd) = &c.cwd {
            cmd.current_dir(cwd);
        }
        cmd.args(&args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if c.managed {
            for (k, _) in std::env::vars_os() {
                let name = k.to_string_lossy();
                if ["CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"].contains(&name.as_ref())
                    || name.starts_with("CLAUDE_CODE_")
                    || name.starts_with("CLAUDE_AGENT_SDK_")
                {
                    cmd.env_remove(k);
                }
            }
        }
    });
    command.wrap(KillOnDrop);
    #[cfg(unix)]
    command.wrap(ProcessSession);
    #[cfg(windows)]
    {
        let mut flags = CreationFlags(Default::default());
        flags.0.0 = 0x08000000; // CREATE_NO_WINDOW, also preserved by JobObject.
        command.wrap(flags);
        command.wrap(JobObject);
    }
    let mut child = command.spawn()?;
    let mut stdin = child.stdin().take().expect("piped stdin");
    let mut stdout = child.stdout().take().expect("piped stdout");
    let mut stderr = child.stderr().take().expect("piped stderr");
    let mut out = Vec::new();
    let mut err = Vec::new();
    let result = tokio::time::timeout(c.timeout, async {
        let input = async {
            let result = stdin.write_all(payload.as_bytes()).await;
            drop(stdin);
            match result {
                Err(e) if e.kind() == std::io::ErrorKind::BrokenPipe => Ok(()),
                other => other,
            }
        };
        let (_, _, _, status) = tokio::try_join!(
            input,
            stdout.read_to_end(&mut out),
            stderr.read_to_end(&mut err),
            child.wait()
        )?;
        Ok::<_, std::io::Error>(status)
    })
    .await;
    let status = match result {
        Ok(Ok(s)) => s,
        result => {
            // The group/job remains owned until it is killed and reaped; the
            // request mutex cannot be released while descendants still run.
            let _ = Box::into_pin(child.kill()).await;
            if let Ok(Err(e)) = result {
                return Err(e.into());
            }
            let argv = std::iter::once(c.cli.to_string_lossy().into_owned())
                .chain(args)
                .map(|s| json::repr(&s))
                .collect::<Vec<_>>()
                .join(", ");
            return Err(format!(
                "Command '[{argv}]' timed out after {} seconds",
                crate::float_repr::finite_float(c.timeout.as_secs_f64()).expect("finite timeout")
            )
            .into());
        }
    };
    let out = String::from_utf8_lossy(&out);
    drop(claude_prompt);
    let err = String::from_utf8_lossy(&err);
    if !status.success() {
        let (mut detail, mut full) = cli_output(c.kind, &out, &err);
        if c.kind == Kind::Codex
            && let Err((message, true)) = codex_reply(&out)
        {
            detail = message.clone();
            full = message;
        }
        let prefix = if c.kind == Kind::Claude {
            "claude -p"
        } else {
            "codex exec"
        };
        #[cfg(unix)]
        let rc = {
            use std::os::unix::process::ExitStatusExt;
            status
                .code()
                .unwrap_or_else(|| -status.signal().unwrap_or(1))
        };
        #[cfg(windows)]
        let rc = status.code().unwrap_or(-1);
        return Err(Failure {
            message: format!("{prefix} rc={rc}: {detail}"),
            sign_in: sign_in(c.kind, &full),
        });
    }
    if c.kind == Kind::Codex {
        return codex_reply(&out).map_err(|(message, _)| Failure {
            sign_in: sign_in(c.kind, &message),
            message,
        });
    }
    let value = serde_json::from_str::<Value>(&out)
        .map_err(|e| Failure::from(super::decode_error(&out, e)))?;
    if !value.is_object() {
        return Err(format!("'{}' object has no attribute 'get'", json::typename(&value)).into());
    }
    if json::truth(&value["is_error"]) {
        let result = json::text(&value["result"]);
        return Err(Failure {
            message: format!("claude -p error result: {}", json::head(&result, 400)),
            sign_in: sign_in(c.kind, &result),
        });
    }
    let result = &value["result"];
    if json::truth(result) && !result.is_string() {
        return Err(format!(
            "'{}' object has no attribute 'strip'",
            json::typename(result)
        )
        .into());
    }
    Ok(strip_fence(result.as_str().unwrap_or("")))
}

pub(super) fn log(c: &Config, line: &str) {
    println!("{line}");
    if let Some(path) = &c.log {
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
            let _ = writeln!(f, "{line}");
        }
    }
}
