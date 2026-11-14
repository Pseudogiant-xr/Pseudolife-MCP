//! Best-effort handshake cache used while a daemon is unreachable.
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub const HANDSHAKE_CACHE_KEEP: usize = 16;

/// The line a failed cache write prints: the folder, the cause (with its
/// repair when an earlier native client's folder protection emptied the
/// folder's access list) and the fix.
pub(crate) fn store_failure_line(folder: &Path, error: &std::io::Error) -> String {
    // An access list an earlier native client's folder protection emptied
    // (#1538): the repair covers this folder only, never an ancestor, whose
    // reset would unprotect the private state files beside it.
    #[cfg(windows)]
    let fix = if error.kind() == std::io::ErrorKind::PermissionDenied
        && crate::credentials::windows_security::empty_dacl_owned(folder)
    {
        format!(
            "Fix: it has an empty access list; give this account access to it and what is in it ({}).",
            crate::board::state::cache_folder_fix(folder)
        )
    } else {
        "Fix: check that this account can write that folder, and that no other program holds its files open.".to_owned()
    };
    #[cfg(not(windows))]
    let fix = "Fix: check that this account can write that folder.".to_owned();
    format!(
        "pseudolife-mcp: the handshake cache could not be saved in {} ({error}); the session works without it, but a start while the daemon is unreachable has no cached instructions. {fix}",
        folder.display()
    )
}
#[derive(Clone, Debug)]
pub struct HandshakeCache {
    url: String,
    path: PathBuf,
}
impl HandshakeCache {
    pub fn new(url: &str, root: impl AsRef<Path>) -> Self {
        let digest = format!("{:x}", Sha256::digest(url.as_bytes()));
        Self {
            url: url.to_owned(),
            path: root
                .as_ref()
                .join("handshake-cache")
                .join(format!("{}.json", &digest[..16])),
        }
    }
    pub fn from_environment(url: &str) -> Option<Self> {
        let state = std::env::var_os("PSEUDOLIFE_AGENT_STATE_DIR").filter(|v| !v.is_empty());
        let home = if state.is_none() {
            crate::credentials::expand_user(Path::new("~")).ok()
        } else {
            None
        };
        Self::from_locations(url, state.as_deref().map(Path::new), home.as_deref())
    }
    pub fn from_locations(
        url: &str,
        state_dir: Option<&Path>,
        home: Option<&Path>,
    ) -> Option<Self> {
        let root = match state_dir.filter(|path| !path.as_os_str().is_empty()) {
            Some(path) => crate::credentials::expand_user(path).ok()?,
            None => home?.join(".pseudolife-mcp"),
        };
        Some(Self::new(url, root))
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn load(&self) -> Map<String, Value> {
        let mut cached = Map::new();
        let Ok(raw) = fs::read_to_string(&self.path) else {
            return cached;
        };
        let Ok(Value::Object(data)) = serde_json::from_str::<Value>(&raw) else {
            return cached;
        };
        if data.get("url").and_then(Value::as_str) != Some(self.url.as_str()) {
            return cached;
        }
        if let Some(value @ Value::String(_)) = data.get("instructions") {
            cached.insert("instructions".into(), value.clone());
        }
        if let Some(value @ Value::Array(_)) = data.get("tools") {
            cached.insert("tools".into(), value.clone());
        }
        cached
    }
    pub fn store(&self, fields: Map<String, Value>) {
        let scratch = self.path.with_file_name(format!(
            "{}.{}.tmp",
            self.path.file_name().unwrap_or_default().to_string_lossy(),
            std::process::id()
        ));
        let parent = self.path.parent().unwrap_or(Path::new("."));
        let stored = (|| -> std::io::Result<()> {
            fs::create_dir_all(parent)?;
            let mut cached = self.load();
            cached.extend(fields);
            // shim.py writes json.dumps({"url": url, **cached}).
            let mut data = Map::from_iter([("url".to_owned(), Value::String(self.url.clone()))]);
            data.extend(cached);
            let mut text = String::new();
            python_dumps(&Value::Object(data), &mut text);
            fs::write(&scratch, text)?;
            fs::rename(&scratch, &self.path)
        })();
        let _ = fs::remove_file(&scratch);
        // Pruning old entries is best effort and never reported as a failed
        // save: the new entry is already in place.
        if stored.is_ok() {
            let _ = (|| -> std::io::Result<()> {
                let mut files = Vec::new();
                for entry in fs::read_dir(parent)? {
                    let path = entry?.path();
                    if path.extension().is_some_and(|e| e == "json") {
                        files.push((fs::metadata(&path)?.modified()?, path));
                    }
                }
                files.sort_by(|a, b| b.0.cmp(&a.0));
                for (_, path) in files.into_iter().skip(HANDSHAKE_CACHE_KEEP) {
                    let _ = fs::remove_file(path);
                }
                Ok(())
            })();
        }
        // Said once per process, not silently dropped: a cache that can never
        // be written leaves every start with the daemon unreachable uncached.
        if let Err(error) = stored {
            static REPORTED: std::sync::atomic::AtomicBool =
                std::sync::atomic::AtomicBool::new(false);
            if !REPORTED.swap(true, std::sync::atomic::Ordering::AcqRel) {
                crate::stderrln!("{}", store_failure_line(parent, &error));
            }
        }
    }
    pub fn remember_instructions(&self, instructions: Option<&str>) {
        self.store(Map::from_iter([(
            "instructions".to_owned(),
            instructions.map_or(Value::Null, |v| Value::String(v.to_owned())),
        )]));
    }
    pub fn remember_tools(&self, tools: Vec<Value>) {
        self.store(Map::from_iter([("tools".to_owned(), Value::Array(tools))]));
    }
}

/// Python's `json.dumps` defaults: `", "` and `": "` separators and ASCII
/// escapes. Numbers keep the lexeme they were read with.
fn python_dumps(value: &Value, out: &mut String) {
    use std::fmt::Write as _;
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(value) => out.push_str(if *value { "true" } else { "false" }),
        Value::Number(number) => out.push_str(&number.to_string()),
        Value::String(text) => {
            out.push('"');
            for c in text.chars() {
                match c {
                    '"' => out.push_str(r#"\""#),
                    '\\' => out.push_str(r"\\"),
                    '\n' => out.push_str(r"\n"),
                    '\r' => out.push_str(r"\r"),
                    '\t' => out.push_str(r"\t"),
                    '\u{8}' => out.push_str(r"\b"),
                    '\u{c}' => out.push_str(r"\f"),
                    ' '..='~' => out.push(c),
                    c => {
                        let mut units = [0u16; 2];
                        for unit in c.encode_utf16(&mut units) {
                            let _ = write!(out, r"\u{unit:04x}");
                        }
                    }
                }
            }
            out.push('"');
        }
        Value::Array(values) => {
            out.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                python_dumps(value, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (index, (key, value)) in map.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                python_dumps(&Value::String(key.clone()), out);
                out.push_str(": ");
                python_dumps(value, out);
            }
            out.push('}');
        }
    }
}

#[cfg(test)]
mod store_failure_tests {
    use super::*;

    #[test]
    fn a_cache_that_cannot_be_saved_says_where_why_and_the_fix() {
        let folder = std::env::temp_dir().join("pseudolife-cache-line");
        let error = std::io::Error::from(std::io::ErrorKind::PermissionDenied);
        let line = store_failure_line(&folder, &error);
        assert!(
            line.starts_with("pseudolife-mcp: the handshake cache could not be saved in "),
            "{line}"
        );
        assert!(line.contains(&folder.display().to_string()), "{line}");
        assert!(line.contains(&error.to_string()), "{line}");
        assert!(
            line.contains("Fix: check that this account can write that folder"),
            "{line}"
        );
    }
}
