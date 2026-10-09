//! The Python runtime pieces doctor's answers rest on: `os.environ` with the
//! registration overrides applied, `pathlib` string forms, `Path.home()`,
//! `read_text`, `json.loads` and CPython 3.11's `shutil.which`. Anything
//! outside the reproduced domain is a [`Defer`].
use serde_json::Value;
use std::{
    io::ErrorKind,
    path::{Path, PathBuf},
};

/// The native doctor cannot answer this environment exactly; defer.
#[derive(Debug)]
pub(super) struct Defer;
pub(super) type Res<T> = Result<T, Defer>;

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

    /// `os.environ.get(key)`; a value Python would hold as a non-UTF-8
    /// string defers.
    pub(super) fn var(&self, key: &str) -> Res<Option<String>> {
        if let Some((_, value)) = self.overlay.iter().find(|(name, _)| name == key) {
            return Ok(Some(value.clone()));
        }
        match std::env::var_os(key) {
            None => Ok(None),
            Some(value) => value.into_string().map(Some).map_err(|_| Defer),
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

/// `str(Path(text))`; drive-relative, UNC and device paths defer.
pub(super) fn path_str(text: &str) -> Res<String> {
    if WINDOWS {
        let text = text.replace('/', "\\");
        if text.starts_with("\\\\") {
            return Err(Defer);
        }
        let bytes = text.as_bytes();
        let (drive, rest) =
            if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
                text.split_at(2)
            } else {
                ("", text.as_str())
            };
        let root = if rest.starts_with('\\') { "\\" } else { "" };
        if !drive.is_empty() && root.is_empty() && !rest.is_empty() {
            return Err(Defer);
        }
        let parts: Vec<&str> = rest
            .split('\\')
            .filter(|p| !p.is_empty() && *p != ".")
            .collect();
        if parts.iter().any(|part| part.contains(':')) {
            return Err(Defer);
        }
        if drive.is_empty() && root.is_empty() && parts.is_empty() {
            return Ok(".".into());
        }
        Ok(format!("{drive}{root}{}", parts.join("\\")))
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
    let raw = env
        .var(if WINDOWS { "USERPROFILE" } else { "HOME" })?
        .ok_or(Defer)?;
    if raw.is_empty() {
        return Err(Defer);
    }
    path_str(&raw)
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

/// `os.path.exists`-family stat: `None` for an absent path, defer for
/// any error pathlib would raise.
fn stat(path: &str) -> Res<Option<std::fs::Metadata>> {
    match std::fs::metadata(path) {
        Ok(meta) => Ok(Some(meta)),
        Err(error) if matches!(error.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {
            Ok(None)
        }
        Err(_) => Err(Defer),
    }
}

/// `os.path.lexists`.
pub(super) fn lexists(path: &str) -> Res<bool> {
    match std::fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if matches!(error.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {
            Ok(false)
        }
        Err(_) => Err(Defer),
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

/// `json.loads(text)`: `Ok(None)` where Python raises `ValueError` too; a
/// failure that may be a Python success (NaN, Infinity, escapes that may be
/// lone surrogates, nesting deeper than this parser's limit) defers.
pub(super) fn json_loads(text: &str) -> Res<Option<Value>> {
    match serde_json::from_str::<Value>(text) {
        Ok(value) if long_integer(&value) => Ok(None),
        Ok(value) => Ok(Some(value)),
        Err(error) => {
            if text.contains("NaN")
                || text.contains("Infinity")
                || text.contains("\\u")
                || error.to_string().contains("recursion limit")
            {
                Err(Defer)
            } else {
                Ok(None)
            }
        }
    }
}

/// `tomllib.loads(text)`: `Ok(None)` for a leading U+FEFF, which tomllib
/// refuses (TOMLDecodeError, a ValueError) and this parser would skip; any
/// other parse failure defers (TOML 1.0 parsers can still disagree at the
/// edges, and Python's integers are unbounded).
pub(super) fn toml_loads(text: &str) -> Res<Option<toml::Table>> {
    if text.starts_with('\u{feff}') {
        return Ok(None);
    }
    text.parse::<toml::Table>().map(Some).map_err(|_| Defer)
}

/// `os.path.join(directory, file)` for a file name with no drive or root.
fn os_join(directory: &str, file: &str) -> Res<String> {
    if WINDOWS {
        let normalized = directory.replace('/', "\\");
        if normalized.starts_with("\\\\") {
            return Err(Defer);
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
        None => return Err(Defer),
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
        if !source.is_ascii() || source.contains(['\\', '/', ':']) {
            return Err(Defer);
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
    let resolved: PathBuf = std::fs::canonicalize(Path::new(path)).map_err(|_| Defer)?;
    let text = resolved.to_str().ok_or(Defer)?;
    if WINDOWS {
        let text = text.strip_prefix("\\\\?\\").ok_or(Defer)?;
        if text.starts_with("UNC\\") {
            return Err(Defer);
        }
        Ok(text.to_lowercase())
    } else {
        Ok(text.to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
            assert!(path_str("C:x").is_err() && path_str("\\\\server\\share").is_err());
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
