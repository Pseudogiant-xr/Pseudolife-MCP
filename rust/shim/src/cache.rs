//! Best-effort handshake cache used while a daemon is unreachable.
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub const HANDSHAKE_CACHE_KEEP: usize = 16;
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
        let _ = (|| -> std::io::Result<()> {
            let parent = self.path.parent().unwrap_or(Path::new("."));
            fs::create_dir_all(parent)?;
            let mut cached = self.load();
            cached.extend(fields);
            // shim.py writes json.dumps({"url": url, **cached}).
            let mut data = Map::from_iter([("url".to_owned(), Value::String(self.url.clone()))]);
            data.extend(cached);
            let mut text = String::new();
            python_dumps(&Value::Object(data), &mut text);
            fs::write(&scratch, text)?;
            fs::rename(&scratch, &self.path)?;
            let mut files = Vec::new();
            for entry in fs::read_dir(parent)? {
                let path = entry?.path();
                if path.extension().is_some_and(|e| e == "json") {
                    files.push((fs::metadata(&path)?.modified()?, path));
                }
            }
            files.sort_by(|a, b| b.0.cmp(&a.0));
            for (_, path) in files.into_iter().skip(HANDSHAKE_CACHE_KEEP) {
                match fs::remove_file(path) {
                    Ok(()) => {}
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    Err(e) => return Err(e),
                }
            }
            Ok(())
        })();
        let _ = fs::remove_file(scratch);
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
