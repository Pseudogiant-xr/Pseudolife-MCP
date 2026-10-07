//! Best-effort GET hooks and their private, non-transactional cursor file.
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    ffi::OsStr,
    fs::{self, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::ExitCode,
    time::{Duration, SystemTime},
};

const USAGE: &str = "usage: pseudolife-mcp briefing [-h] [--max-unsure MAX_UNSURE]\n                               [--max-lessons MAX_LESSONS]\n                               [--max-world MAX_WORLD] [--hook-json]\n                               [--coordination]\n";
const HELP: &str = include_str!("briefing_help.txt");
const OPTIONS: &[&str] = &[
    "--help",
    "--max-unsure",
    "--max-lessons",
    "--max-world",
    "--hook-json",
    "--coordination",
];

#[cfg(unix)]
struct DescriptorWriter<'a>(rustix::fd::BorrowedFd<'a>);
#[cfg(unix)]
impl Write for DescriptorWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        rustix::io::write(self.0, bytes).map_err(Into::into)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn print(text: &str, stderr: bool) -> io::Result<()> {
    let bytes = super::text_bytes(text);
    #[cfg(unix)]
    let out = io::stdout();
    #[cfg(unix)]
    let error = io::stderr();
    #[cfg(unix)]
    let mut stream = DescriptorWriter(if stderr { error.as_fd() } else { out.as_fd() });
    #[cfg(windows)]
    let mut stream: Box<dyn Write> = if stderr {
        Box::new(io::stderr().lock())
    } else {
        Box::new(io::stdout().lock())
    };
    stream.write_all(&bytes).and_then(|_| stream.flush())
}

fn stdout(text: &str) -> io::Result<()> {
    print(text, false)
}

fn output_failed() -> u8 {
    let _ = print("pseudolife-mcp briefing: output failed\n", true);
    1
}

fn reply_not_understood() -> u8 {
    let _ = print(
        "pseudolife-mcp briefing: daemon reply not understood\n",
        true,
    );
    1
}

#[cfg(unix)]
use std::os::fd::AsFd;

fn terminal_width() -> usize {
    std::env::var("COLUMNS")
        .ok()
        .and_then(|v| columns_width(&v))
        .unwrap_or(80)
        .saturating_sub(2)
}

fn columns_width(raw: &str) -> Option<usize> {
    let value = integer(raw)?;
    if value == "0" || value.starts_with('-') {
        return None;
    }
    // Large positive widths still put this finite help text on one line.
    Some(value.parse().unwrap_or(usize::MAX))
}

fn wrapped_parts(parts: &[&str], width: usize, indent: usize, first: usize) -> String {
    let mut output = String::new();
    let mut used = first;
    for (i, part) in parts.iter().enumerate() {
        if i > 0 && used + 1 + part.len() > width {
            output.push('\n');
            output.push_str(&" ".repeat(indent));
            used = indent;
        } else if i > 0 {
            output.push(' ');
            used += 1;
        }
        output.push_str(part);
        used += part.len();
    }
    output
}

fn usage(width: usize) -> String {
    if width == 78 {
        return USAGE.into();
    }
    let prog = "pseudolife-mcp briefing";
    let options = [
        "[-h]",
        "[--max-unsure MAX_UNSURE]",
        "[--max-lessons MAX_LESSONS]",
        "[--max-world MAX_WORLD]",
        "[--hook-json]",
        "[--coordination]",
    ];
    if 7 + prog.len() + 1 + options.join(" ").len() <= width {
        format!("usage: {prog} {}\n", options.join(" "))
    } else if ((7 + prog.len()) as u128) * 4 <= (width as u128) * 3 {
        let mut parts = vec![prog];
        parts.extend(options);
        format!(
            "usage: {}\n",
            wrapped_parts(&parts, width, 7 + prog.len() + 1, 7)
        )
    } else {
        format!(
            "usage: {prog}\n       {}\n",
            wrapped_parts(&options, width, 7, 7)
        )
    }
}

fn help_lines(text: &str, width: usize) -> Vec<String> {
    let mut chunks = Vec::new();
    for word in text.split_whitespace() {
        if matches!(word, "agent-board" | "check-in") {
            let (left, right) = word.split_once('-').unwrap();
            chunks.push(format!("{left}-"));
            chunks.push(right.into());
        } else {
            chunks.push(word.into());
        }
        chunks.push(" ".into());
    }
    chunks.pop();
    let mut lines = Vec::new();
    let mut line = String::new();
    for mut chunk in chunks {
        if chunk == " " && line.is_empty() {
            continue;
        }
        while line.len() + chunk.len() > width {
            let preserve_space = chunk.len() > width && line.len() == width;
            if chunk.len() > width {
                let count = width.saturating_sub(line.len());
                if count > 0 {
                    line.push_str(&chunk[..count]);
                    chunk = chunk[count..].into();
                }
            }
            if !line.is_empty() {
                // textwrap drops the empty long-word fragment in this case,
                // leaving the preceding whitespace chunk on the line.
                lines.push(if preserve_space {
                    line.clone()
                } else {
                    line.trim_end().into()
                });
                line.clear();
            }
            if chunk == " " {
                chunk.clear();
                break;
            }
        }
        line.push_str(&chunk);
    }
    if !line.is_empty() {
        lines.push(line.trim_end().into());
    }
    lines
}

fn help(width: usize) -> String {
    if width == 78 {
        return HELP.into();
    }
    let descriptions = [
        ("-h, --help", "show this help message and exit"),
        (
            "--max-unsure MAX_UNSURE",
            "cap surprises AND questions at this many EACH (default 3 of each)",
        ),
        ("--max-lessons MAX_LESSONS", ""),
        ("--max-world MAX_WORLD", ""),
        (
            "--hook-json",
            "emit a Claude Code/Codex SessionStart hook payload (hookSpecificOutput.additionalContext) carrying the memory core and the bounded briefing the plugin hook serves; the --max-* caps do not apply",
        ),
        (
            "--coordination",
            "print the agent-board check-in instead, only where this bearer can use the board",
        ),
    ];
    let position = 24.min(width.saturating_sub(20).max(4));
    let action_width = position.saturating_sub(4);
    let help_width = width.saturating_sub(position).max(11);
    let mut output = format!("{}\noptions:\n", usage(width));
    for (action, description) in descriptions {
        if description.is_empty() {
            output.push_str(&format!("  {action}\n"));
            continue;
        }
        let lines = help_lines(description, help_width);
        if action.len() <= action_width {
            output.push_str(&format!("  {action:action_width$}  {}\n", lines[0]));
        } else {
            output.push_str(&format!(
                "  {action}\n{}{}\n",
                " ".repeat(position),
                lines[0]
            ));
        }
        for line in &lines[1..] {
            output.push_str(&format!("{}{line}\n", " ".repeat(position)));
        }
    }
    output
}

fn whitespace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

fn integer(raw: &str) -> Option<String> {
    let text = raw.trim_matches([' ', '\t', '\n', '\r', '\u{b}', '\u{c}']);
    let (negative, text) = if let Some(text) = text.strip_prefix('-') {
        (true, text)
    } else {
        (false, text.strip_prefix('+').unwrap_or(text))
    };
    if text.is_empty() || !text.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    // Caps are signed, unbounded decimal query values, not machine integers.
    let digits = text.trim_start_matches('0');
    Some(if digits.is_empty() {
        "0".into()
    } else if negative {
        format!("-{digits}")
    } else {
        digits.into()
    })
}

struct Arguments {
    caps: [String; 3],
    hook: bool,
    coordination: bool,
}
enum Parsed {
    Arguments(Arguments),
    Help,
    Error(String),
}

struct Argument {
    text: String,
    points: Vec<u32>,
}
impl Argument {
    fn from_points(points: Vec<u32>) -> Self {
        let text = points
            .iter()
            .map(|point| {
                char::from_u32(*point)
                    .map(|c| c.to_string())
                    .unwrap_or_else(|| format!("\\u{point:04x}"))
            })
            .collect();
        Self { text, points }
    }
    fn from_os(value: &OsStr) -> Self {
        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStrExt;
            Self::from_points(
                char::decode_utf16(value.encode_wide())
                    .map(|c| {
                        c.map(u32::from)
                            .unwrap_or_else(|e| u32::from(e.unpaired_surrogate()))
                    })
                    .collect(),
            )
        }
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStrExt;
            let mut bytes = value.as_bytes();
            let mut points = Vec::new();
            while !bytes.is_empty() {
                match std::str::from_utf8(bytes) {
                    Ok(text) => {
                        points.extend(text.chars().map(u32::from));
                        break;
                    }
                    Err(error) => {
                        let valid = error.valid_up_to();
                        points.extend(
                            std::str::from_utf8(&bytes[..valid])
                                .unwrap()
                                .chars()
                                .map(u32::from),
                        );
                        points.push(0xdc00 + u32::from(bytes[valid]));
                        bytes = &bytes[valid + 1..];
                    }
                }
            }
            Self::from_points(points)
        }
    }
    fn repr(&self, inline: bool) -> String {
        let points = if inline {
            self.points
                .iter()
                .position(|p| *p == u32::from(b'='))
                .map_or(&self.points[..], |index| &self.points[index + 1..])
        } else {
            &self.points
        };
        let quote = if points.contains(&39) && !points.contains(&34) {
            '"'
        } else {
            '\''
        };
        let mut output = quote.to_string();
        for point in points {
            match char::from_u32(*point) {
                None => {
                    use std::fmt::Write;
                    let _ = write!(output, "\\u{point:04x}");
                }
                Some(c) if c == quote => {
                    output.push('\\');
                    output.push(c);
                }
                Some('\'') => output.push('\''),
                Some('"') => output.push('"'),
                Some(c) => {
                    let one = super::mode_repr(&c.to_string());
                    output.push_str(&one[1..one.len() - 1]);
                }
            }
        }
        output.push(quote);
        output
    }
}
impl From<&str> for Argument {
    fn from(value: &str) -> Self {
        Self::from_points(value.chars().map(u32::from).collect())
    }
}
impl std::ops::Deref for Argument {
    type Target = str;
    fn deref(&self) -> &str {
        &self.text
    }
}
impl std::fmt::Display for Argument {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.text.fmt(f)
    }
}

fn negative_number(text: &str) -> bool {
    let Some(text) = text.strip_prefix('-') else {
        return false;
    };
    // The narrowed numeric grammar retains negative decimal option values.
    let mut dots = 0;
    let mut count = 0;
    for c in text.chars() {
        if c == '.' && dots == 0 {
            dots += 1;
            count = 0;
        } else if c.is_ascii_digit() {
            count += 1;
        } else {
            return false;
        }
    }
    count > 0
}

fn parse(argv: &[Argument]) -> Parsed {
    // argparse classifies the complete sequence before help or value conversion.
    let mut classified = Vec::new();
    for raw in argv {
        if raw.text == "--" {
            break;
        }
        let name = raw.split_once('=').map_or(&raw.text[..], |(name, _)| name);
        let matched: Vec<_> = if name.starts_with("-h") && !name.starts_with("--") {
            vec!["--help"]
        } else if name.starts_with("--") {
            if OPTIONS.contains(&name) {
                vec![name]
            } else {
                OPTIONS
                    .iter()
                    .copied()
                    .filter(|opt| opt.starts_with(name))
                    .collect()
            }
        } else {
            vec![]
        };
        if matched.len() > 1 {
            return Parsed::Error(format!(
                "ambiguous option: {raw} could match {}",
                matched.join(", ")
            ));
        }
        let optional = !matched.is_empty()
            || raw.starts_with('-')
                && raw.text != "-"
                && !negative_number(raw)
                && !raw.contains(' ');
        classified.push(optional);
    }
    let mut result = Arguments {
        caps: ["3".into(), "3".into(), "3".into()],
        hook: false,
        coordination: false,
    };
    let mut i = 0;
    while i < argv.len() {
        let raw = &argv[i];
        if raw.text == "--" {
            break;
        }
        let (name, inline) = raw
            .split_once('=')
            .map_or((&raw.text[..], None), |(a, b)| (a, Some(b)));
        let matched: Vec<_> = if name == "-h" || name.starts_with("-h") && !name.starts_with("--") {
            vec!["--help"]
        } else if name.starts_with("--") {
            if OPTIONS.contains(&name) {
                vec![name]
            } else {
                OPTIONS
                    .iter()
                    .copied()
                    .filter(|opt| opt.starts_with(name))
                    .collect()
            }
        } else {
            vec![]
        };
        if matched.len() > 1 {
            return Parsed::Error(format!(
                "ambiguous option: {raw} could match {}",
                matched.join(", ")
            ));
        }
        let Some(option) = matched.first().copied() else {
            i += 1;
            continue;
        };
        if matches!(option, "--help" | "--hook-json" | "--coordination") {
            let short_tail = if name.starts_with("-h") && name != "-h" && !name.starts_with("--") {
                Some(&raw[2..])
            } else {
                None
            };
            if short_tail.is_some() {
                return Parsed::Help;
            }
            if inline.or(short_tail).is_some() {
                let label = if option == "--help" {
                    "-h/--help"
                } else {
                    option
                };
                return Parsed::Error(format!(
                    "argument {label}: ignored explicit argument {}",
                    raw.repr(true)
                ));
            }
            if option == "--help" {
                return Parsed::Help;
            }
            if option == "--hook-json" {
                result.hook = true;
            } else {
                result.coordination = true;
            }
        } else {
            let value: &str = if let Some(value) = inline {
                value
            } else {
                i += 1;
                let Some(value) = argv.get(i) else {
                    return Parsed::Error(format!("argument {option}: expected one argument"));
                };
                if value.text == "--" || classified.get(i).copied().unwrap_or(false) {
                    return Parsed::Error(format!("argument {option}: expected one argument"));
                }
                value
            };
            let Some(value) = integer(value) else {
                return Parsed::Error(format!(
                    "argument {option}: invalid int value: {}",
                    argv[i].repr(inline.is_some())
                ));
            };
            let index = OPTIONS.iter().position(|v| *v == option).unwrap() - 1;
            result.caps[index] = value;
        }
        i += 1;
    }
    Parsed::Arguments(result)
}

fn quote(text: &str) -> String {
    use std::fmt::Write;
    let mut result = String::new();
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.' | b'~') {
            result.push(byte as char);
        } else {
            let _ = write!(result, "%{byte:02X}");
        }
    }
    result
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
}

fn launcher_query() -> String {
    let launcher = if let (Some(_), Some(launcher)) = (
        env_path("PSEUDOLIFE_SHIM_RUNTIMES"),
        env_path("PSEUDOLIFE_SHIM_LAUNCHER"),
    ) {
        if launcher
            .extension()
            .is_some_and(|s| s.eq_ignore_ascii_case("exe"))
            != cfg!(windows)
        {
            return String::new();
        }
        launcher
    } else {
        let Some(home) = env_path("USERPROFILE")
            .or_else(|| env_path("HOME"))
            .or_else(dirs::home_dir)
        else {
            return String::new();
        };
        let data = if cfg!(windows) {
            env_path("LOCALAPPDATA").unwrap_or_else(|| home.join("AppData/Local"))
        } else {
            env_path("XDG_DATA_HOME").unwrap_or_else(|| home.join(".local/share"))
        };
        data.join("pseudolife-mcp/bin").join(if cfg!(windows) {
            "pseudolife-mcp.exe"
        } else {
            "pseudolife-mcp"
        })
    };
    if !launcher.is_file() {
        return String::new();
    }
    if crate::lifecycle::find_executable("pseudolife-mcp")
        .ok()
        .is_some_and(|found| same_path(&found, &launcher))
    {
        return String::new();
    }
    // PathBuf removes redundant separators and '.' exactly as pathlib does.
    let launcher: PathBuf = launcher.components().collect();
    format!("?launcher={}", quote(&launcher.to_string_lossy()))
}

fn same_path(left: &Path, right: &Path) -> bool {
    let form = |path: &Path| {
        let path = fs::canonicalize(path)
            .unwrap_or_else(|_| std::path::absolute(path).unwrap_or_else(|_| path.to_owned()));
        let text = path.to_string_lossy().into_owned();
        if cfg!(windows) {
            text.trim_start_matches("\\\\?\\").to_lowercase()
        } else {
            text
        }
    };
    form(left) == form(right)
}

async fn get(
    origin: &str,
    target: &str,
    token: Option<&str>,
    timeout: Duration,
    health: bool,
) -> Option<String> {
    let client = reqwest::Client::builder()
        .redirect(if health {
            reqwest::redirect::Policy::limited(10)
        } else {
            reqwest::redirect::Policy::none()
        })
        .referer(false)
        .connect_timeout(timeout)
        .read_timeout(timeout)
        .user_agent("Python-urllib/3.11")
        .build()
        .ok()?;
    let mut request = client.get(format!("{origin}{target}"));
    if let Some(token) = token.filter(|s| !s.is_empty()) {
        // Python's http.client encodes header values as Latin-1.
        // http-forbidden-input-refused rejects C0/DEL and folded bearer values.
        let bytes: Option<Vec<u8>> = format!("Bearer {token}")
            .chars()
            .map(|c| u8::try_from(c as u32).ok())
            .collect();
        if bytes
            .as_ref()?
            .iter()
            .any(|byte| *byte < 0x20 || *byte == 0x7f)
        {
            return None;
        }
        request = request.header(
            reqwest::header::AUTHORIZATION,
            reqwest::header::HeaderValue::from_bytes(&bytes?).ok()?,
        );
    }
    let response = request.send().await.ok()?;
    if !health && !(200..300).contains(&response.status().as_u16()) {
        return None;
    }
    String::from_utf8(response.bytes().await.ok()?.to_vec()).ok()
}

fn origin(prompt: bool) -> Result<String, u8> {
    crate::daemon_url::from_environment().map_err(|message| {
        let _ = print(&format!("{message}\n"), true);
        if prompt { 0 } else { 1 }
    })
}

fn payload(event: &str, context: &str) -> String {
    format!(
        "{{\"hookSpecificOutput\": {{\"hookEventName\": \"{event}\", \"additionalContext\": {}}}}}\n",
        super::hook_json::quoted(context)
    )
}

async fn briefing(argv: &[Argument]) -> u8 {
    let args = match parse(argv) {
        Parsed::Help => {
            return if stdout(&help(terminal_width())).is_ok() {
                0
            } else {
                output_failed()
            };
        }
        Parsed::Error(error) => {
            let _ = print(
                &format!(
                    "{}pseudolife-mcp briefing: error: {error}\n",
                    usage(terminal_width())
                ),
                true,
            );
            return 2;
        }
        Parsed::Arguments(args) => args,
    };
    if args.coordination {
        let setting = std::env::var("PSEUDOLIFE_AGENT_COORDINATION").unwrap_or_default();
        let setting = setting.trim_matches(whitespace).to_lowercase();
        if !setting.is_empty() && !matches!(setting.as_str(), "1" | "true" | "yes" | "on") {
            return 0;
        }
    }
    let url = match origin(false) {
        Ok(v) => v,
        Err(code) => return code,
    };
    let Some(health) = get(&url, "/health", None, Duration::from_millis(250), true).await else {
        return 0;
    };
    match super::hook_json::input(&health) {
        Ok(Value::Null) => return 0,
        Ok(_) => {}
        Err(()) => return 0,
    }
    let token = std::env::var("PSEUDOLIFE_MCP_TOKEN").ok();
    let target = if args.coordination {
        "/api/hook/coordination-start".into()
    } else if args.hook {
        format!("/api/hook/session-start{}", launcher_query())
    } else {
        format!(
            "/api/briefing?max_unsure={}&max_lessons={}&max_world={}",
            args.caps[0], args.caps[1], args.caps[2]
        )
    };
    let Some(body) = get(
        &url,
        &target,
        token.as_deref(),
        Duration::from_secs(if args.coordination { 2 } else { 5 }),
        false,
    )
    .await
    else {
        return 0;
    };
    let markdown = if args.hook || args.coordination {
        body
    } else {
        let Ok(data) = super::hook_json::input(&body) else {
            return reply_not_understood();
        };
        match data.get("markdown") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(text)) => text.clone(),
            Some(_) => return reply_not_understood(),
        }
    };
    let context = markdown.trim_matches(whitespace);
    if context.is_empty() {
        return 0;
    }
    let output = if args.hook {
        payload("SessionStart", context)
    } else {
        format!("{context}\n")
    };
    if stdout(&output).is_ok() {
        0
    } else {
        output_failed()
    }
}

fn cursor(text: &str) -> bool {
    !text.is_empty() && text.len() <= 22 && text.bytes().all(|b| b.is_ascii_digit() || b == b'.')
}

fn mark_directory() -> Option<PathBuf> {
    env_path("PSEUDOLIFE_DIGEST_DIR").or_else(|| {
        crate::credentials::expand_user(Path::new("~"))
            .ok()
            .map(|home| home.join(".pseudolife-mcp/digests"))
    })
}

fn first_line(path: &Path) -> String {
    if !path.is_file() || path.is_symlink() {
        return String::new();
    }
    let Ok(bytes) = fs::read(path) else {
        return String::new();
    };
    // Python text streams translate all newline forms before readline().
    let first = bytes
        .split(|b| matches!(b, b'\r' | b'\n'))
        .next()
        .unwrap_or_default();
    if !first.is_ascii() {
        return String::new();
    }
    let text = std::str::from_utf8(first).unwrap().trim_matches(whitespace);
    if cursor(text) {
        text.into()
    } else {
        String::new()
    }
}

fn directory(path: &Path) -> io::Result<()> {
    if path.is_dir() {
        return Ok(());
    }
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(parent)?;
    }
    #[cfg(unix)]
    let mut builder = fs::DirBuilder::new();
    #[cfg(not(unix))]
    let builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)
}

fn clean_old(directory: &Path) -> io::Result<()> {
    let cutoff = SystemTime::now() - Duration::from_secs(30 * 86400);
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        if entry.file_name().to_string_lossy().ends_with(".mark")
            && path.is_file()
            && !path.is_symlink()
            && fs::metadata(&path)
                .and_then(|m| m.modified())
                .is_ok_and(|mtime| mtime < cutoff)
        {
            let _ = fs::remove_file(path);
        }
    }
    Ok(())
}

async fn prompt_hook() -> Option<()> {
    let mut input = Vec::new();
    io::stdin().read_to_end(&mut input).ok()?;
    let data = super::hook_json::input(&String::from_utf8_lossy(&input)).ok()?;
    let session = data.get("session_id")?.as_str()?;
    if session.is_empty()
        || session.len() > 128
        || !session
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        return None;
    }
    let directory_path = mark_directory()?;
    let mark = directory_path.join(format!("{:x}.mark", Sha256::digest(session.as_bytes())));
    let since = first_line(&mark);
    let credentials = crate::credentials::CredentialProvider::from_environment()
        .ok()?
        .snapshot()
        .ok()?;
    let url = origin(true).ok()?;
    let mut target = format!("/api/hook/memory-changes?session_id={session}");
    if !since.is_empty() {
        target.push_str(&format!("&since={since}"));
    }
    let body = get(
        &url,
        &target,
        credentials.token(),
        Duration::from_secs(2),
        false,
    )
    .await?;
    let (next, note) = body.split_once('\n').unwrap_or((&body, ""));
    let next = next.trim_end_matches('\r');
    if !cursor(next) {
        return None;
    }
    let note = note.trim_end_matches(['\r', '\n']);
    directory(&directory_path).ok()?;
    if mark.is_symlink() {
        return None;
    }
    if !mark.exists() {
        clean_old(&directory_path).ok()?;
    }
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let mut file = options.open(&mark).ok()?;
    if !note.is_empty() {
        stdout(&payload("UserPromptSubmit", note)).ok()?;
    }
    file.set_len(0).ok()?;
    file.write_all(format!("{next}\n").as_bytes()).ok()?;
    Some(())
}

pub(super) fn run(mode: &str) -> ExitCode {
    let prompt = mode == "prompt-hook";
    let argv: Vec<_> = if prompt {
        Vec::new()
    } else {
        std::env::args_os()
            .skip(2)
            .map(|value| Argument::from_os(&value))
            .collect()
    };
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        return ExitCode::from(if prompt { 0 } else { 1 });
    };
    ExitCode::from(runtime.block_on(async {
        if prompt {
            let _ = prompt_hook().await;
            0
        } else {
            briefing(&argv).await
        }
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn first_line_cursor_ignores_trailing_bytes_but_refuses_malformed_first_line() {
        let path =
            std::env::temp_dir().join(format!("pseudolife-hook-mark-{}", uuid::Uuid::new_v4()));
        for (bytes, expected) in [
            (b"100.0\n\xff".as_slice(), "100.0"),
            (b"100.0\r\xff".as_slice(), "100.0"),
            (b"junk\n100.0".as_slice(), ""),
            (b"100.0\xff\n".as_slice(), ""),
        ] {
            fs::write(&path, bytes).unwrap();
            assert_eq!(first_line(&path), expected);
        }
        fs::remove_file(path).unwrap();
    }
    #[test]
    fn columns_use_positive_ascii_decimal() {
        for value in ["40", "+40", " 40\t", "00040"] {
            assert_eq!(columns_width(value), Some(40));
        }
        for value in ["0", "-40", "٤٠", "4_0", "\u{a0}40", "4.0", "\u{1c}40"] {
            assert_eq!(columns_width(value), None);
        }
        assert_eq!(columns_width(&"9".repeat(100)), Some(usize::MAX));
    }
    #[test]
    fn help_long_word_keeps_preceding_space_when_line_is_full() {
        assert_eq!(
            help_lines("Code/Codex SessionStart", 11),
            ["Code/Codex ", "SessionStar", "t"]
        );
        assert_eq!(help_lines("short ordinary", 11), ["short", "ordinary"]);
    }
    #[test]
    fn cursor_and_int_domains() {
        for value in [".", "00..5", "1234567890123456789012"] {
            assert!(cursor(value));
        }
        for value in ["", "-1", "1e3", "1\n", "12345678901234567890123"] {
            assert!(!cursor(value));
        }
        assert!(integer("\u{a0}-００_３\u{a0}").is_none());
        assert_eq!(integer(" -003\t\u{b}\u{c}"), Some("-3".into()));
        assert_eq!(integer(&"9".repeat(5000)), Some("9".repeat(5000)));
        assert!(integer("\u{1c}3").is_none());
        assert_eq!(integer("-0"), Some("0".into()));
        for value in ["_1", "1__2", "1_", "1.0"] {
            assert!(integer(value).is_none());
        }
    }
    #[test]
    fn grammar_abbreviations_and_known_tail() {
        let args = vec![
            "--max-w=-04".into(),
            "--coor".into(),
            "unknown".into(),
            "--hook-j".into(),
        ];
        let Parsed::Arguments(parsed) = parse(&args) else {
            panic!()
        };
        assert_eq!(parsed.caps, ["3", "3", "-4"]);
        assert!(parsed.hook && parsed.coordination);
        assert!(matches!(parse(&["--max".into()]), Parsed::Error(_)));
        assert!(matches!(
            parse(&["--max-world".into(), "--help".into()]),
            Parsed::Error(_)
        ));
    }
    #[test]
    fn classification_precedes_help_and_value_conversion() {
        for first in ["--help", "--max-world=bogus", "--max-world"] {
            let Parsed::Error(error) = parse(&[first.into(), "--max".into()]) else {
                panic!()
            };
            assert_eq!(
                error,
                "ambiguous option: --max could match --max-unsure, --max-lessons, --max-world"
            );
        }
        for value in ["-foo bar", "--unknown value"] {
            let Parsed::Error(error) = parse(&["--max-world".into(), value.into()]) else {
                panic!()
            };
            assert_eq!(
                error,
                format!("argument --max-world: invalid int value: '{value}'")
            );
        }
        assert!(matches!(
            parse(&["--help".into(), "--".into(), "--max".into()]),
            Parsed::Help
        ));
    }
}
