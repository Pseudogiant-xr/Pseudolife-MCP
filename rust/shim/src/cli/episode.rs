//! Legacy hook leaves: silent best-effort calls to an already-running daemon.
use super::python_json::{
    self,
    json::{self, Value},
};
use std::{
    ffi::{OsStr, OsString},
    io::Read,
    path::{Component, Path, PathBuf},
    process::ExitCode,
    time::Duration,
};

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_none_or(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn key(input: &Value) -> Option<Value> {
    let value = input.get("session_id").filter(|value| truthy(value))?;
    Some(if value.is_string() {
        value.clone()
    } else {
        Value::from(value.python(false))
    })
}

fn normalized(path: &Path) -> PathBuf {
    let mut result = PathBuf::new();
    for part in path.components() {
        match part {
            Component::ParentDir => {
                if matches!(result.components().next_back(), Some(Component::Normal(_))) {
                    result.pop();
                } else if !result.has_root() {
                    result.push("..");
                }
            }
            Component::CurDir => {}
            other => result.push(other.as_os_str()),
        }
    }
    if result.as_os_str().is_empty() {
        result.push(".");
    }
    result
}

fn absolute(path: &Path) -> Option<PathBuf> {
    if cfg!(windows) {
        return std::path::absolute(path).ok();
    }
    Some(normalized(&if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir().ok()?.join(path)
    }))
}

#[cfg(windows)]
fn os_text(points: &[u32]) -> Option<OsString> {
    use std::os::windows::ffi::OsStringExt;
    let mut units = Vec::new();
    for point in points {
        if *point <= 0xffff {
            units.push(*point as u16);
        } else {
            units.extend(
                char::from_u32(*point)?
                    .encode_utf16(&mut [0; 2])
                    .iter()
                    .copied(),
            );
        }
    }
    Some(OsString::from_wide(&units))
}
#[cfg(unix)]
fn os_text(points: &[u32]) -> Option<OsString> {
    use std::os::unix::ffi::OsStringExt;
    let mut bytes = Vec::new();
    for point in points {
        if (0xdc80..=0xdcff).contains(point) {
            bytes.push((point - 0xdc00) as u8);
        } else {
            bytes.extend(char::from_u32(*point)?.encode_utf8(&mut [0; 4]).as_bytes());
        }
    }
    Some(OsString::from_vec(bytes))
}
#[cfg(windows)]
fn text_os(text: &OsStr) -> Vec<u32> {
    use std::os::windows::ffi::OsStrExt;
    char::decode_utf16(text.encode_wide())
        .map(|c| {
            c.map(u32::from)
                .unwrap_or_else(|e| u32::from(e.unpaired_surrogate()))
        })
        .collect()
}
#[cfg(unix)]
fn text_os(text: &OsStr) -> Vec<u32> {
    use std::os::unix::ffi::OsStrExt;
    let mut bytes = text.as_bytes();
    let mut points = Vec::new();
    while !bytes.is_empty() {
        match std::str::from_utf8(bytes) {
            Ok(valid) => {
                points.extend(valid.chars().map(u32::from));
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
    points
}

fn windows_path_marker(cwd: &[u32]) -> bool {
    cwd.contains(&92)
        || cwd.first().is_some_and(|c| matches!(c, 65..=90 | 97..=122))
            && cwd.get(1) == Some(&58)
            && cwd.get(2).is_some_and(|c| matches!(c, 47 | 92))
}

// ntpath uses character positions for drives, including non-ASCII characters.
fn drive_len(path: &[u32]) -> usize {
    if path.len() >= 2 && path[1] == 58 {
        return 2;
    }
    if path.len() >= 2 && matches!(path[0], 47 | 92) && matches!(path[1], 47 | 92) {
        let start = if matches!(
            path.get(..8),
            Some([92, 92, 63, 92, 85 | 117, 78 | 110, 67 | 99, 92])
        ) {
            8
        } else {
            2
        };
        if let Some(server) = path[start..].iter().position(|c| matches!(c, 47 | 92)) {
            return path[start + server + 1..]
                .iter()
                .position(|c| matches!(c, 47 | 92))
                .map_or(path.len(), |share| start + server + 1 + share);
        }
        return path.len();
    }
    0
}

fn norm_points(path: &[u32]) -> Vec<u32> {
    let path: Vec<_> = path
        .iter()
        .map(|c| if cfg!(windows) && *c == 47 { 92 } else { *c })
        .collect();
    let sep = if cfg!(windows) { 92 } else { 47 };
    let drive = if cfg!(windows) { drive_len(&path) } else { 0 };
    let tail = &path[drive..];
    let rooted = tail.first() == Some(&sep);
    let roots = if !cfg!(windows) && tail.starts_with(&[47, 47]) && tail.get(2) != Some(&47) {
        2
    } else {
        usize::from(rooted)
    };
    let mut parts: Vec<&[u32]> = Vec::new();
    for part in tail.split(|c| *c == sep) {
        if part.is_empty() || part == [46] {
            continue;
        }
        if part == [46, 46] {
            if parts.last().is_some_and(|p| *p != [46, 46]) {
                parts.pop();
            } else if !rooted {
                parts.push(part);
            }
        } else {
            parts.push(part);
        }
    }
    let mut result = path[..drive].to_vec();
    result.extend(std::iter::repeat_n(sep, roots));
    for (i, part) in parts.iter().enumerate() {
        if i != 0 {
            result.push(sep);
        }
        result.extend_from_slice(part);
    }
    if result.is_empty() {
        result.push(46);
    }
    result
}

fn basename(path: &[u32]) -> Vec<u32> {
    let tail = &path[drive_len(path)..];
    tail.rsplit(|c| matches!(c, 47 | 92))
        .next()
        .unwrap_or(&[])
        .to_vec()
}

fn project(cwd: &[u32]) -> Option<Vec<u32>> {
    if cwd.is_empty() || (!cfg!(windows) && windows_path_marker(cwd)) {
        return None;
    }
    if let Some(cwd) = os_text(cwd) {
        let mut path = absolute(Path::new(&cwd))?;
        loop {
            if path.join(".git").is_dir() {
                return path.file_name().map(text_os);
            }
            if !path.pop() {
                return None;
            }
        }
    }
    // POSIX cannot encode every Python surrogate. isdir returns false for that
    // component, but the lexical parent walk can still reach a real repository.
    let mut path = if cwd.first() == Some(&47) {
        cwd.to_vec()
    } else {
        let mut path = text_os(std::env::current_dir().ok()?.as_os_str());
        path.push(47);
        path.extend_from_slice(cwd);
        path
    };
    path = norm_points(&path);
    loop {
        if let Some(candidate) = os_text(&path)
            && Path::new(&candidate).join(".git").is_dir()
        {
            return Some(basename(&path));
        }
        let end = path.iter().rposition(|c| *c == 47)?;
        if end == 0 {
            if path.len() == 1 {
                return None;
            }
            path.truncate(1);
        } else {
            path.truncate(end);
        }
    }
}

fn title(cwd: Option<&Value>) -> Option<Value> {
    let cwd = match cwd {
        Some(value) if truthy(value) => python_json::points(value)?,
        _ => &[],
    };
    let mut name = project(cwd);
    if name.is_none() && !cwd.is_empty() {
        let is_home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
            .and_then(|home| absolute(Path::new(&home)))
            .zip(os_text(cwd).and_then(|cwd| absolute(Path::new(&cwd))))
            .is_some_and(|(home, cwd)| home == cwd);
        if !is_home {
            let base = basename(&norm_points(cwd));
            let lower: String = base
                .iter()
                .map(|c| char::from_u32(*c).unwrap_or('\u{fffd}'))
                .flat_map(char::to_lowercase)
                .collect();
            if !base.is_empty()
                && !matches!(
                    lower.as_str(),
                    "system32" | "syswow64" | "windows" | "system" | "system64"
                )
            {
                name = Some(base);
            }
        }
    }
    let mut name = name.unwrap_or_else(|| "session".chars().map(u32::from).collect());
    name.extend(
        format!(" - {}", chrono::Local::now().format("%Y-%m-%d %H:%M"))
            .chars()
            .map(u32::from),
    );
    Some(python_json::string(&name))
}

// The pinned CPython public CLI has 989 remaining container-recursion slots.
// Count containers, not scalar leaves; json.loads rejects before any request.
fn stdin_json(input: &str) -> Option<Value> {
    let (mut depth, mut quoted, mut escaped) = (0usize, false, false);
    for byte in input.bytes() {
        if quoted {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                quoted = false;
            }
        } else {
            match byte {
                b'"' => quoted = true,
                b'[' | b'{' => {
                    depth += 1;
                    if depth > 989 {
                        return None;
                    }
                }
                b']' | b'}' => depth = depth.saturating_sub(1),
                _ => {}
            }
        }
    }
    json::from_str(input).ok()
}

// Parsing, formatting and dropping accepted Python JSON all recurse. Windows'
// default main-thread stack is too small at the public CLI's accepted depth.
pub(super) async fn run(mode: &str) -> ExitCode {
    std::thread::scope(|scope| {
        std::thread::Builder::new()
            .stack_size(8 * 1024 * 1024)
            .spawn_scoped(scope, || {
                let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                else {
                    return ExitCode::SUCCESS;
                };
                runtime.block_on(run_inner(mode))
            })
            .ok()
            .and_then(|thread| thread.join().ok())
            .unwrap_or(ExitCode::SUCCESS)
    })
}

async fn health(url: &str) -> Option<Value> {
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_millis(250))
        .read_timeout(Duration::from_millis(250))
        .build()
        .ok()?;
    let bytes = client
        .get(format!("{url}/health"))
        .header("User-Agent", "Python-urllib/3.11")
        .header("Accept-Encoding", "identity")
        .header("Connection", "close")
        .send()
        .await
        .ok()?
        .bytes()
        .await
        .ok()?;
    // The production health path decodes UTF-8 text, unlike json.loads(bytes).
    let value = stdin_json(std::str::from_utf8(&bytes).ok()?)?;
    (!value.is_null()).then_some(value)
}

async fn run_inner(mode: &str) -> ExitCode {
    let mut input = String::new();
    if std::io::stdin().read_to_string(&mut input).is_err() {
        return ExitCode::SUCCESS;
    }
    // TextIOWrapper performs universal-newline conversion before json.loads.
    let input = input.replace("\r\n", "\n").replace('\r', "\n");
    let Some(input) = stdin_json(&input) else {
        return ExitCode::SUCCESS;
    };
    let Some(key) = key(&input) else {
        return ExitCode::SUCCESS;
    };
    let url = match crate::daemon_url::from_environment() {
        Ok(url) => url,
        Err(message) => {
            crate::stderrln!("{message}");
            return ExitCode::FAILURE;
        }
    };
    if health(&url).await.is_none() {
        return ExitCode::SUCCESS;
    }
    let (path, body) = if mode == "episode-start" {
        let Some(title) = title(input.get("cwd")) else {
            return ExitCode::SUCCESS;
        };
        (
            "start",
            format!("{{\"session_key\": {key}, \"title\": {title}}}"),
        )
    } else {
        ("end", format!("{{\"session_key\": {key}}}"))
    };
    if let Ok(client) = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .read_timeout(Duration::from_secs(5))
        .build()
    {
        let mut request = client
            .post(format!("{url}/api/episode/{path}"))
            .header("Content-Type", "application/json")
            .header("User-Agent", "Python-urllib/3.11")
            .header("Accept-Encoding", "identity")
            .header("Connection", "close")
            .body(body);
        if let Ok(token) = std::env::var("PSEUDOLIFE_MCP_TOKEN")
            && !token.is_empty()
        {
            // urllib's HTTP/1 header writer encodes values as Latin-1.
            let bytes: Result<Vec<u8>, _> = format!("Bearer {token}")
                .chars()
                .map(|c| u8::try_from(u32::from(c)))
                .collect();
            let Ok(bytes) = bytes else {
                return ExitCode::SUCCESS;
            };
            let Ok(value) = http::HeaderValue::from_bytes(&bytes) else {
                return ExitCode::SUCCESS;
            };
            request = request.header("Authorization", value);
        }
        if let Ok(response) = request.send().await {
            let _ = response.bytes().await;
        }
    }
    ExitCode::SUCCESS
}

#[cfg(test)]
mod tests {
    #[test]
    fn extended_unc_drive_prefix_is_case_insensitive() {
        for value in [
            r"\\?\UNC\server\share\..",
            r"\\?\unc\server\share\..",
            r"\\?\uNc\server\share\..",
        ] {
            let points: Vec<_> = value.chars().map(u32::from).collect();
            assert_eq!(super::drive_len(&points), 20);
        }
    }

    #[test]
    fn foreign_windows_marker_requires_an_ascii_drive_letter() {
        let marker = |value: &str| {
            super::windows_path_marker(&value.chars().map(u32::from).collect::<Vec<_>>())
        };
        assert!(marker("C:/project"));
        assert!(marker(r"relative\project"));
        assert!(!marker("1:/project"));
        assert!(!marker("a:project"));
    }
}
