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
            let mut data = self.load();
            data.extend(fields);
            data.insert("url".into(), Value::String(self.url.clone()));
            fs::write(&scratch, serde_json::to_vec(&data)?)?;
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
