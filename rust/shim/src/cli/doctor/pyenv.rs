//! Read-only environment, path and configuration helpers for doctor.
use serde_json::Value;
use std::{
    io::ErrorKind,
    path::{Path, PathBuf},
};

/// A diagnostic cause and remedy. Never includes input values or parser excerpts.
#[derive(Debug)]
pub(super) struct Finding(pub(super) String);
impl Finding {
    pub(super) fn new(cause: &str) -> Self {
        Self(cause.to_owned())
    }
    pub(super) fn at(self, input: &str) -> Self {
        Self(format!("{input}: {}", self.0))
    }
    pub(super) fn io(error: &std::io::Error) -> Self {
        Self(format!(
            "filesystem operation failed ({:?}); correct the path or its access permissions",
            error.kind()
        ))
    }
}
impl std::fmt::Display for Finding {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.write_str(&self.0)
    }
}
pub(super) type Res<T> = Result<T, Finding>;

pub(super) const WINDOWS: bool = cfg!(windows);
const SEP: char = if WINDOWS { '\\' } else { '/' };

/// `os.environ` after doctor's `os.environ.update(overrides)`.
#[derive(Default)]
pub(super) struct Env {
    overlay: Vec<(String, String)>,
}

impl Env {
    pub(super) fn set(&mut self, key: &str, value: String) {
        self.overlay.retain(|(name, _)| name != key);
        self.overlay.push((key.to_owned(), value));
    }

    pub(super) fn overlay(&self) -> &[(String, String)] {
        &self.overlay
    }

    /// Read text without ever displaying an undecodable value.
    pub(super) fn var(&self, key: &str) -> Res<Option<String>> {
        if let Some((_, value)) = self.overlay.iter().find(|(name, _)| name == key) {
            return Ok(Some(value.clone()));
        }
        match std::env::var_os(key) {
            None => Ok(None),
            Some(value) => value.into_string().map(Some).map_err(|_| {
                Finding::new("value is not valid Unicode; set a Unicode value").at(key)
            }),
        }
    }

    /// `os.environ.get(key)` when truthy.
    pub(super) fn truthy(&self, key: &str) -> Res<Option<String>> {
        Ok(self.var(key)?.filter(|value| !value.is_empty()))
    }
}

/// `str.isspace()`.
pub(super) fn is_space(character: char) -> bool {
    matches!(character, '\t'..='\r' | '\u{1c}'..=' ' | '\u{85}' | '\u{a0}' | '\u{1680}'
        | '\u{2000}'..='\u{200a}' | '\u{2028}' | '\u{2029}' | '\u{202f}' | '\u{205f}' | '\u{3000}')
}

/// `str.strip()`.
pub(super) fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// Normalize separators and dot components without resolving parent components.
pub(super) fn path_str(text: &str) -> Res<String> {
    if WINDOWS {
        let text = text.replace('/', "\\");
        if text.is_empty() {
            return Ok(".".into());
        }
        let normalized = Path::new(&text)
            .components()
            .filter(|part| *part != std::path::Component::CurDir)
            .collect::<PathBuf>()
            .to_string_lossy()
            .into_owned();
        Ok(if normalized.is_empty() {
            ".".into()
        } else {
            normalized
        })
    } else {
        let root = if text.starts_with("//") && !text.starts_with("///") {
            "//"
        } else if text.starts_with('/') {
            "/"
        } else {
            ""
        };
        let parts: Vec<&str> = text
            .split('/')
            .filter(|p| !p.is_empty() && *p != ".")
            .collect();
        if root.is_empty() && parts.is_empty() {
            return Ok(".".into());
        }
        Ok(format!("{root}{}", parts.join("/")))
    }
}

/// `str(Path(base) / name)` for a normalized `base` and one plain `name`.
pub(super) fn join(base: &str, name: &str) -> String {
    if base == "." {
        name.into()
    } else if WINDOWS {
        Path::new(base).join(name).to_string_lossy().into_owned()
    } else if base.ends_with(SEP) {
        format!("{base}{name}")
    } else {
        format!("{base}{SEP}{name}")
    }
}

/// `str(Path(text).parent)` for a normalized path.
pub(super) fn parent(text: &str) -> String {
    match text.rfind(SEP) {
        None => ".".into(),
        Some(index) => {
            let head = &text[..index];
            // The root keeps its separator: C:\ or /.
            if head.is_empty() || (WINDOWS && head.len() == 2 && head.ends_with(':')) {
                text[..=index].to_owned()
            } else {
                head.to_owned()
            }
        }
    }
}

/// `Path(text).name` for a normalized path.
pub(super) fn name(text: &str) -> &str {
    let tail = text.rsplit(SEP).next().unwrap_or(text);
    if WINDOWS && tail.len() == 2 && tail.ends_with(':') {
        ""
    } else {
        tail
    }
}

/// `Path.home()` (CPython 3.11 `os.path.expanduser("~")`) as a normalized string.
pub(super) fn home(env: &Env) -> Res<String> {
    let key = if WINDOWS { "USERPROFILE" } else { "HOME" };
    let raw = env
        .var(key)?
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            Finding::new("home directory is unset or empty; set it to your home directory").at(key)
        })?;
    path_str(&raw)
}

/// Expand this user's home without consulting other users or printing the path.
pub(super) fn expand_home(text: &str, env: &Env) -> Res<String> {
    if text == "~" {
        return home(env);
    }
    if let Some(tail) = text
        .strip_prefix("~/")
        .or_else(|| WINDOWS.then(|| text.strip_prefix("~\\")).flatten())
    {
        // Concatenate before normalization: a repeated slash after '~' is
        // still part of the suffix, not a new absolute path for Path::join.
        return path_str(&format!("{}{SEP}{tail}", home(env)?));
    }
    if text.starts_with('~') {
        return Err(Finding::new(
            "named-user tilde expansion is unsupported; set an absolute path",
        ));
    }
    path_str(text)
}

/// `USERPROFILE or HOME or Path.home()` (runtimes.home, client_updates.home).
pub(super) fn user_home(env: &Env) -> Res<String> {
    match env.truthy("USERPROFILE")?.or(env.truthy("HOME")?) {
        Some(value) => path_str(&value),
        None => home(env),
    }
}

/// `Path.is_file()`: false only where pathlib ignores the stat error.
pub(super) fn is_file(path: &str) -> Res<bool> {
    stat(path).map(|meta| meta.is_some_and(|meta| meta.is_file()))
}

pub(super) fn is_dir(path: &str) -> Res<bool> {
    stat(path).map(|meta| meta.is_some_and(|meta| meta.is_dir()))
}

/// Missing paths are ordinary negative checks; other errors carry their cause.
fn stat(path: &str) -> Res<Option<std::fs::Metadata>> {
    match std::fs::metadata(path) {
        Ok(meta) => Ok(Some(meta)),
        Err(error) if matches!(error.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {
            Ok(None)
        }
        Err(error) => Err(Finding::io(&error)),
    }
}

/// The outcome of `Path.read_text(encoding=...)`.
pub(super) enum Text {
    /// `OSError` (absent, a directory, unreadable).
    Unreadable,
    /// `UnicodeDecodeError`.
    Undecodable,
    Ok(String),
}

/// `Path(path).read_text(encoding="utf-8" | "utf-8-sig")`, universal
/// newlines included.
pub(super) fn read_text(path: &str, sig: bool) -> Text {
    let Ok(bytes) = std::fs::read(path) else {
        return Text::Unreadable;
    };
    let Ok(text) = String::from_utf8(bytes) else {
        return Text::Undecodable;
    };
    let text = if sig {
        text.strip_prefix('\u{feff}')
            .map(str::to_owned)
            .unwrap_or(text)
    } else {
        text
    };
    Text::Ok(text.replace("\r\n", "\n").replace('\r', "\n"))
}

/// CPython 3.11's `int()` refuses decimal strings over 4300 digits
/// (`sys.int_info.default_max_str_digits`): `json.loads` raises ValueError
/// on such an integer literal.
const INT_MAX_STR_DIGITS: usize = 4300;

/// Whether `value` holds an integer literal Python's decoder refuses.
pub(super) fn long_integer(value: &Value) -> bool {
    match value {
        Value::Number(number) => {
            let text = number.to_string();
            super::pyjson::is_int_token(&text)
                && text.trim_start_matches('-').len() > INT_MAX_STR_DIGITS
        }
        Value::Array(items) => items.iter().any(long_integer),
        Value::Object(items) => items.values().any(long_integer),
        _ => false,
    }
}

/// The deepest array/object nesting in a JSON text, strings skipped.
pub(super) fn json_depth(text: &[u8]) -> usize {
    let (mut depth, mut deepest, mut string, mut escaped) = (0usize, 0usize, false, false);
    for byte in text {
        if string {
            match (escaped, byte) {
                (true, _) => escaped = false,
                (false, b'\\') => escaped = true,
                (false, b'"') => string = false,
                _ => {}
            }
            continue;
        }
        match byte {
            b'"' => string = true,
            b'[' | b'{' => {
                depth += 1;
                deepest = deepest.max(depth);
            }
            b']' | b'}' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    deepest
}

/// `json.loads` without this parser's nesting limit: `None` for any JSON
/// error. Callers bound the depth first.
pub(super) fn json_unbounded(text: &str) -> Option<Value> {
    use serde::Deserialize;
    let mut decoder = serde_json::Deserializer::from_str(text);
    decoder.disable_recursion_limit();
    let value = Value::deserialize(serde_stacker::Deserializer::new(&mut decoder)).ok()?;
    decoder.end().ok()?;
    Some(value)
}

/// Standard JSON, with a bounded depth independent of a Python interpreter.
pub(super) fn json_loads(text: &str) -> Res<Option<Value>> {
    if json_depth(text.as_bytes()) > 256 {
        return Err(Finding::new(
            "JSON nesting exceeds 256 levels; reduce the nesting",
        ));
    }
    json_unbounded(text).map(Some).ok_or_else(|| Finding::new(
        "invalid JSON (requires finite numbers and valid Unicode escapes); correct the JSON syntax"))
}

/// Parse TOML without exposing a parser excerpt (which may contain a token).
pub(super) fn toml_loads(text: &str) -> Res<Option<toml::Table>> {
    if text.starts_with('\u{feff}') {
        return Ok(None);
    }
    text.parse::<toml::Table>().map(Some).map_err(|_| {
        Finding::new(
            "invalid TOML or a value outside TOML's supported range; correct the TOML syntax",
        )
    })
}

/// `os.path.join(directory, file)` for a file name with no drive or root.
fn os_join(directory: &str, file: &str) -> Res<String> {
    if WINDOWS {
        let normalized = directory.replace('/', "\\");
        if normalized.starts_with("\\\\") {
            return Ok(Path::new(directory)
                .join(file)
                .to_string_lossy()
                .into_owned());
        }
        let split = if directory.as_bytes().get(1) == Some(&b':') && directory.is_char_boundary(2) {
            2
        } else {
            0
        };
        let (drive, path) = directory.split_at(split);
        let mut result = path.to_owned();
        if !result.is_empty() && !result.ends_with(['\\', '/']) {
            result.push('\\');
        }
        result.push_str(file);
        Ok(format!("{drive}{result}"))
    } else if directory.is_empty() || directory.ends_with('/') {
        Ok(format!("{directory}{file}"))
    } else {
        Ok(format!("{directory}/{file}"))
    }
}

fn access_check(name: &str) -> Res<bool> {
    let Some(meta) = stat(name)? else {
        return Ok(false);
    };
    if meta.is_dir() {
        return Ok(false);
    }
    #[cfg(unix)]
    {
        Ok(rustix::fs::access(name, rustix::fs::Access::EXEC_OK).is_ok())
    }
    #[cfg(not(unix))]
    {
        Ok(true)
    }
}

/// CPython 3.11 `shutil.which(cmd)` for a bare command name.
pub(super) fn which(cmd: &str, env: &Env) -> Res<Option<String>> {
    let path = match env.var("PATH")? {
        Some(path) => path,
        None if WINDOWS => ".;C:\\bin".into(),
        // No PATH: the platform's default search path, as CPython's
        // `os.confstr("CS_PATH")` reports it.
        None if cfg!(target_os = "macos") => "/usr/bin:/bin:/usr/sbin:/sbin".into(),
        None => "/bin:/usr/bin".into(),
    };
    if path.is_empty() {
        return Ok(None);
    }
    let mut directories: Vec<&str> = path.split(if WINDOWS { ';' } else { ':' }).collect();
    let files: Vec<String> = if WINDOWS {
        if !directories.contains(&".") {
            directories.insert(0, ".");
        }
        let source = env
            .truthy("PATHEXT")?
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC".into());
        if source.contains(['\\', '/', ':']) {
            return Err(Finding::new(
                "PATHEXT contains a path separator; use semicolon-separated file extensions",
            ));
        }
        let extensions: Vec<&str> = source.split(';').filter(|ext| !ext.is_empty()).collect();
        let lower = cmd.to_ascii_lowercase();
        if extensions
            .iter()
            .any(|ext| lower.ends_with(&ext.to_ascii_lowercase()))
        {
            vec![cmd.to_owned()]
        } else {
            extensions.iter().map(|ext| format!("{cmd}{ext}")).collect()
        }
    } else {
        vec![cmd.to_owned()]
    };
    for directory in directories {
        for file in &files {
            let name = os_join(directory, file)?;
            if access_check(&name)? {
                return Ok(Some(name));
            }
        }
    }
    Ok(None)
}

/// `os.path.normcase(os.path.realpath(path))` for an existing path.
pub(super) fn real_normcase(path: &str) -> Res<String> {
    let resolved: PathBuf =
        std::fs::canonicalize(Path::new(path)).map_err(|error| Finding::io(&error))?;
    let text = resolved
        .to_str()
        .ok_or_else(|| Finding::new("resolved path is not Unicode; use a Unicode launcher path"))?;
    if WINDOWS {
        let text = text.strip_prefix("\\\\?\\").unwrap_or(text);
        let text = text
            .strip_prefix("UNC\\")
            .map(|tail| format!("\\\\{tail}"))
            .unwrap_or_else(|| text.to_owned());
        Ok(text.to_lowercase())
    } else {
        Ok(text.to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_json_findings_and_near_misses() {
        let mut failed = Vec::new();
        for text in [
            "{\"n\":NaN}",
            "{\"n\":Infinity}",
            "{\"n\":\"\\ud800\"}",
            "{\"n\":\"\\uZZZZ\"}",
        ] {
            let result = json_loads(text);
            let ok = format!("{result:?}").contains("JSON");
            eprintln!("JSON cell {text}: {ok}");
            if !ok {
                failed.push(text);
            }
        }
        assert!(failed.is_empty(), "{failed:?}");
        for text in [
            "{\"n\":\"NaN\"}",
            "{\"n\":\"Infinity\"}",
            "{\"n\":\"\\ud83d\\ude00\"}",
        ] {
            assert!(json_loads(text).unwrap().is_some());
        }
    }

    #[test]
    fn native_deep_json_and_large_integers() {
        let text = format!("{}0{}", "[".repeat(130), "]".repeat(130));
        assert!(json_loads(&text).unwrap().is_some());
        assert!(json_loads(&"1".repeat(4301)).unwrap().is_some());
    }

    #[test]
    fn native_toml_error_names_grammar_without_input() {
        let result = toml_loads("token = 'w44-secret-value'\n[broken");
        let text = format!("{result:?}");
        assert!(text.contains("TOML"), "{text}");
        assert!(!text.contains("w44-secret-value"));
        assert!(toml_loads("token = 'w44-secret-value'").unwrap().is_some());
    }

    #[test]
    fn native_missing_home_names_setting() {
        let mut env = Env::default();
        let key = if WINDOWS { "USERPROFILE" } else { "HOME" };
        env.set(key, String::new());
        let text = format!("{:?}", home(&env));
        assert!(text.contains(key), "{text}");
        env.set(key, "/fixture".into());
        assert!(home(&env).is_ok());
    }

    #[test]
    fn native_filesystem_failures_name_the_operation_without_input() {
        let bad = "fixture\0w44-secret-value";
        for result in [
            is_file(bad).map(|_| ()),
            is_dir(bad).map(|_| ()),
            real_normcase(bad).map(|_| ()),
        ] {
            let text = format!("{result:?}");
            assert!(text.contains("filesystem") && text.contains(';'), "{text}");
            assert!(!text.contains("w44-secret-value"));
        }
        assert!(!is_file("absent-w44-near-miss").unwrap());
        assert!(!is_dir("absent-w44-near-miss").unwrap());
    }

    #[test]
    fn native_json_integer_has_no_python_digit_limit() {
        assert!(json_loads(&"1".repeat(4301)).unwrap().is_some());
    }

    #[cfg(windows)]
    #[test]
    fn native_pathext_errors_and_unicode_extensions() {
        let mut env = Env::default();
        env.set("PATH", r"Z:\absent-w44".into());
        env.set("PATHEXT", ".EXE;bad/path".into());
        let text = format!("{:?}", which("absent-w44", &env));
        assert!(
            text.contains("PATHEXT") && text.contains("separator"),
            "{text}"
        );
        env.set("PATHEXT", ".EXE;.é".into());
        assert!(which("absent-w44", &env).unwrap().is_none());
    }

    #[cfg(windows)]
    #[test]
    fn review_round2_leading_dot_is_normalized() {
        assert_eq!(
            path_str(r".\folder\state.json").unwrap(),
            r"folder\state.json"
        );
        assert_eq!(
            path_str(r"folder\state.json").unwrap(),
            r"folder\state.json"
        );
        assert_eq!(path_str(r".\").unwrap(), ".");
    }

    #[cfg(windows)]
    #[test]
    fn native_windows_path_shapes() {
        let mut failed = Vec::new();
        for path in [
            r"\\server\share\state.json",
            r"C:state.json",
            r"C:\state.json:stream",
            r"\\?\C:\state.json",
        ] {
            let ok = path_str(path).is_ok();
            eprintln!("Windows path {path}: {ok}");
            if !ok {
                failed.push(path);
            }
        }
        assert!(failed.is_empty(), "{failed:?}");
        assert!(os_join(r"\\server\share", "codex.exe").is_ok());
    }

    #[test]
    fn python_whitespace_includes_the_separators() {
        assert_eq!(strip("\u{1c}\u{1f} on \u{3000}"), "on");
        assert_eq!(strip("\u{200b}on"), "\u{200b}on");
    }

    #[test]
    fn pathlib_strings() {
        if WINDOWS {
            assert_eq!(path_str("C:/a//b/./c/").unwrap(), "C:\\a\\b\\c");
            assert_eq!(path_str("C:\\").unwrap(), "C:\\");
            assert_eq!(path_str("").unwrap(), ".");
            assert_eq!(path_str("a/../b").unwrap(), "a\\..\\b");
            assert!(path_str("C:x").is_ok() && path_str("\\\\server\\share").is_ok());
            assert_eq!(parent("C:\\Git\\cmd\\git.EXE"), "C:\\Git\\cmd");
            assert_eq!(parent("C:\\git.EXE"), "C:\\");
            assert_eq!(name("C:\\Windows\\System32\\bash.EXE"), "bash.EXE");
            assert_eq!(join("C:\\", "x"), "C:\\x");
            assert_eq!(os_join("C:", "x").unwrap(), "C:x");
            assert_eq!(os_join("C:\\a\\", "x").unwrap(), "C:\\a\\x");
            assert_eq!(os_join("", "x").unwrap(), "x");
        } else {
            assert_eq!(path_str("//a//b/").unwrap(), "//a/b");
            assert_eq!(path_str("///a").unwrap(), "/a");
            assert_eq!(parent("/git"), "/");
            assert_eq!(os_join("", "x").unwrap(), "x");
        }
        assert_eq!(join(".", "x"), "x");
    }
}
