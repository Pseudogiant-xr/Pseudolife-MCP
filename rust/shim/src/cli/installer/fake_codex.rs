//! An in-process stand-in for `codex app-server` in the installer's unit
//! tests: `config/read` over one versioned user layer (plus optional
//! stronger layers), `config/batchWrite` with compare-and-swap, `hooks/list`
//! over a fixed listing plus the manual hooks of `<home>/hooks.json` (trust
//! following `hooks.state`), and a thread whose first turn reports hook runs.
use crate::setup::codex::{Rpc, SetupError};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

pub struct FakeCodex {
    pub user: Value,
    /// Settings a stronger (project or managed) layer adds to the effective
    /// configuration, merged over the user layer.
    pub overlay: Value,
    pub version: u64,
    pub file: String,
    pub calls: Vec<(String, Value)>,
    /// What `hooks/list` lists besides the manual hooks; `trustStatus` is
    /// recomputed from `hooks.state.<key>.trusted_hash` against each
    /// entry's `currentHash`.
    pub hooks: Vec<Value>,
    pub hook_errors: Vec<Value>,
    /// List the handlers of this `hooks.json` too, as Codex does.
    pub manual_from: Option<PathBuf>,
    /// Omit `layers` (an unversioned or unknown configuration API).
    pub layers: Option<Value>,
    /// Refuse every `config/batchWrite` (a correlated RPC error).
    pub reject_writes: bool,
    /// After a write, the effective value of this dotted path is replaced
    /// (a stronger layer winning over the write).
    pub shadow: Option<(Vec<String>, Value)>,
    /// What a turn's hooks report (`hook/completed` runs), and the file the
    /// prompt hook's cursor would create.
    pub turn_runs: Vec<Value>,
    pub turn_mark: Option<PathBuf>,
    /// Notifications not yet received by the session.
    pub pending: Vec<Value>,
    /// The `-c key=value` switches the current session was started with,
    /// applied over the effective configuration as Codex applies them.
    pub session_overrides: Option<Value>,
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
            manual_from: None,
            layers: None,
            reject_writes: false,
            shadow: None,
            turn_runs: vec![],
            turn_mark: None,
            pending: vec![],
            session_overrides: None,
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
        for (key, value) in self.session_overrides.iter().flat_map(|o| o.as_object()).flatten() {
            let path: Vec<String> = key.split('.').map(str::to_owned).collect();
            let mut node = &mut merged;
            for part in &path {
                if !node.is_object() {
                    *node = json!({});
                }
                node = node.as_object_mut().unwrap().entry(part.clone()).or_insert(Value::Null);
            }
            merge(node, value);
        }
        merged
    }

    fn read(&self) -> Value {
        let layers = self.layers.clone().unwrap_or_else(|| {
            json!([{
                "name": {"type": "user", "file": self.file, "profile": null},
                "version": format!("version-{}", self.version),
                "config": self.user,
            }])
        });
        json!({"config": self.effective(), "layers": layers})
    }

    /// The handlers of a user `hooks.json`, keyed as Codex keys them.
    fn manual(&self) -> Vec<Value> {
        let Some(path) = &self.manual_from else {
            return vec![];
        };
        let Ok(bytes) = std::fs::read(path) else {
            return vec![];
        };
        let document: Value = serde_json::from_slice(&bytes).unwrap_or(json!({}));
        let mut listed = Vec::new();
        for (section, groups) in document["hooks"].as_object().into_iter().flatten() {
            let event = format!("{}{}", section[..1].to_lowercase(), &section[1..]);
            let snake: String = section
                .chars()
                .enumerate()
                .flat_map(|(i, c)| {
                    let lower = c.to_ascii_lowercase();
                    if i > 0 && c.is_ascii_uppercase() {
                        vec!['_', lower]
                    } else {
                        vec![lower]
                    }
                })
                .collect();
            for (g, group) in groups.as_array().into_iter().flatten().enumerate() {
                for (h, handler) in group["hooks"].as_array().into_iter().flatten().enumerate() {
                    let field = if cfg!(windows) && handler.get("commandWindows").is_some() {
                        "commandWindows"
                    } else {
                        "command"
                    };
                    let command = handler[field].as_str().unwrap_or_default();
                    let definition = format!("{command}|{}", handler["timeout"]);
                    listed.push(json!({
                        "key": format!("{}:{snake}:{g}:{h}", path.display()),
                        "eventName": event, "handlerType": "command",
                        "command": command, "sourcePath": path, "source": "user",
                        "pluginId": null, "enabled": true, "isManaged": false,
                        "currentHash": format!("sha256:{:x}", Sha256::digest(definition.as_bytes())),
                        "trustStatus": "untrusted",
                    }));
                }
            }
        }
        listed
    }

    fn listed(&self) -> Value {
        let state = &self.effective()["hooks"]["state"];
        let hooks: Vec<Value> = self
            .hooks
            .iter()
            .cloned()
            .chain(self.manual())
            .map(|mut hook| {
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
    let mut stream = serde_json::Deserializer::from_str(dotted).into_iter::<String>();
    let Some(Ok(first)) = stream.next() else {
        return vec![];
    };
    let rest = &dotted[stream.byte_offset()..];
    let mut parts = vec![first];
    if let Some(rest) = rest.strip_prefix('.') {
        parts.extend(key_path(rest));
    }
    parts
}

pub const THREAD: &str = "fixture-thread";

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
            "thread/start" => Ok(json!({"thread": {"id": THREAD}})),
            "turn/start" => {
                for run in &self.turn_runs {
                    self.pending
                        .push(json!({"method": "hook/completed", "params": {"run": run}}));
                }
                if let Some(mark) = &self.turn_mark {
                    std::fs::create_dir_all(mark.parent().unwrap()).unwrap();
                    std::fs::write(mark, b"cursor").unwrap();
                }
                Ok(json!({"turn": {"id": "fixture-turn"}}))
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
