//! An in-process stand-in for `codex app-server` in the installer's unit
//! tests: `config/read` over one versioned user layer (plus optional
//! stronger layers), `config/batchWrite` with compare-and-swap, and
//! `hooks/list` over a fixed listing whose trust follows `hooks.state`.
use crate::setup::codex::{Rpc, SetupError};
use serde_json::{Map, Value, json};
use std::path::Path;

pub struct FakeCodex {
    pub user: Value,
    /// Settings a stronger (project or managed) layer adds to the effective
    /// configuration, merged over the user layer.
    pub overlay: Value,
    pub version: u64,
    pub file: String,
    pub calls: Vec<(String, Value)>,
    /// What `hooks/list` lists; `trustStatus` is recomputed from
    /// `hooks.state.<key>.trusted_hash` against each entry's `currentHash`.
    pub hooks: Vec<Value>,
    pub hook_errors: Vec<Value>,
    /// Refuse every `config/batchWrite` (a correlated RPC error).
    pub reject_writes: bool,
    /// After a write, the effective value of this dotted path is replaced
    /// (a stronger layer winning over the write).
    pub shadow: Option<(Vec<String>, Value)>,
}

impl FakeCodex {
    pub fn new(home: &Path, user: Value) -> Self {
        Self {
            user,
            overlay: json!({}),
            version: 1,
            file: home.join("config.toml").to_string_lossy().into_owned(),
            calls: vec![],
            hooks: vec![],
            hook_errors: vec![],
            reject_writes: false,
            shadow: None,
        }
    }

    pub fn writes(&self) -> Vec<&Value> {
        self.calls
            .iter()
            .filter(|(method, _)| method == "config/batchWrite")
            .map(|(_, params)| params)
            .collect()
    }

    pub fn effective(&self) -> Value {
        let mut merged = self.user.clone();
        merge(&mut merged, &self.overlay);
        if let Some((path, value)) = &self.shadow {
            set(&mut merged, path, value.clone());
        }
        merged
    }

    fn read(&self) -> Value {
        json!({
            "config": self.effective(),
            "layers": [{
                "name": {"type": "user", "file": self.file, "profile": null},
                "version": format!("version-{}", self.version),
                "config": self.user,
            }],
        })
    }

    fn listed(&self) -> Value {
        let state = &self.user["hooks"]["state"];
        let hooks: Vec<Value> = self
            .hooks
            .iter()
            .map(|hook| {
                let mut hook = hook.clone();
                let key = hook["key"].as_str().unwrap_or_default().to_owned();
                let trusted = state[&key]["trusted_hash"].as_str();
                let status = match trusted {
                    Some(hash) if hook["currentHash"].as_str() == Some(hash) => "trusted",
                    Some(_) => "modified",
                    None => "untrusted",
                };
                if hook["trustStatus"] != "managed" {
                    hook["trustStatus"] = json!(status);
                }
                if state[&key]["enabled"] == false {
                    hook["enabled"] = json!(false);
                }
                hook
            })
            .collect();
        json!({"data": [{"cwd": "fixture", "hooks": hooks, "errors": self.hook_errors}]})
    }
}

fn merge(base: &mut Value, overlay: &Value) {
    match (base, overlay) {
        (Value::Object(base), Value::Object(overlay)) => {
            for (key, value) in overlay {
                merge(base.entry(key.clone()).or_insert(Value::Null), value);
            }
        }
        (base, overlay) => *base = overlay.clone(),
    }
}

fn set(root: &mut Value, path: &[String], value: Value) {
    let mut node = root;
    for key in &path[..path.len() - 1] {
        if !node.is_object() {
            *node = json!({});
        }
        node = node
            .as_object_mut()
            .unwrap()
            .entry(key.clone())
            .or_insert_with(|| Value::Object(Map::new()));
    }
    if !node.is_object() {
        *node = json!({});
    }
    node.as_object_mut()
        .unwrap()
        .insert(path[path.len() - 1].clone(), value);
}

/// `"a"."b.c"` -> `["a", "b.c"]`: the dotted key path Codex's API takes.
pub fn key_path(dotted: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut rest = dotted;
    while !rest.is_empty() {
        assert!(rest.starts_with('"'), "key path {dotted:?}");
        let mut end = 1;
        let bytes = rest.as_bytes();
        while bytes[end] != b'"' {
            end += if bytes[end] == b'\\' { 2 } else { 1 };
        }
        parts.push(serde_json::from_str(&rest[..=end]).unwrap());
        rest = rest[end + 1..].strip_prefix('.').unwrap_or(&rest[end + 1..]);
    }
    parts
}

impl Rpc for FakeCodex {
    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, SetupError> {
        self.calls.push((method.to_owned(), params.clone()));
        match method {
            "config/read" => Ok(self.read()),
            "hooks/list" => Ok(self.listed()),
            "config/batchWrite" => {
                if self.reject_writes {
                    return Err(SetupError::rejected("fixture refusal"));
                }
                assert_eq!(params["filePath"].as_str(), Some(self.file.as_str()));
                if params["expectedVersion"] != json!(format!("version-{}", self.version)) {
                    return Err(SetupError::rejected("fixture version conflict"));
                }
                for edit in params["edits"].as_array().unwrap() {
                    let path = key_path(edit["keyPath"].as_str().unwrap());
                    set(&mut self.user, &path, edit["value"].clone());
                }
                self.version += 1;
                Ok(json!({"status":"ok","version":format!("version-{}", self.version)}))
            }
            other => panic!("unexpected Codex RPC {other}"),
        }
    }
}

#[test]
fn key_paths_round_trip_quoted_segments() {
    let dotted = crate::setup::codex::dotted(&["hooks", "state", "C:\\x\\hooks.json:a.b:0:0"]);
    assert_eq!(
        key_path(&dotted),
        ["hooks", "state", "C:\\x\\hooks.json:a.b:0:0"]
    );
}
