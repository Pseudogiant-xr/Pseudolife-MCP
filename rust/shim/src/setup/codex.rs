//! Codex configuration is written through its versioned app-server API.
//! Never flatten project/managed settings into the user configuration.
mod credentials;
mod rpc;
pub use credentials::{configure_credential_file, daemon_url, effective_value};
pub use rpc::{Client, Rpc, resolve_codex};

use super::files::{self, PrivateEdit};
use crate::credentials::CredentialProvider;
use serde_json::{Map, Value, json};
use std::{
    fmt,
    path::{Path, PathBuf},
};

pub const SERVER: &str = "pseudolife-memory";
pub const URL_KEY: &str = "PSEUDOLIFE_MCP_DAEMON_URL";
pub const FILE_KEY: &str = "PSEUDOLIFE_MCP_TOKEN_FILE";
pub const TOKEN_KEY: &str = "PSEUDOLIFE_MCP_TOKEN";
pub const NO_SPAWN_KEY: &str = "PSEUDOLIFE_MCP_NO_SPAWN";

/// Static diagnostics cannot accidentally include credential-bearing RPC data.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SetupError(pub &'static str);
const ROLLBACK_UNVERIFIED: SetupError = SetupError(
    "Codex setup failed and rollback could not be verified; the private token copy was retained for the saved configuration. Review the private config backup before retrying.",
);
impl fmt::Display for SetupError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}
impl std::error::Error for SetupError {}

pub fn connection_path(home: &Path) -> PathBuf {
    home.join("pseudolife").join("connection.json")
}
pub fn token_copy_path(home: &Path) -> PathBuf {
    home.join("pseudolife").join("token")
}

pub fn dotted(parts: &[&str]) -> String {
    parts
        .iter()
        .map(|part| serde_json::to_string(part).expect("string JSON"))
        .collect::<Vec<_>>()
        .join(".")
}

/// The sole writable layer, with the opaque version needed for compare-and-swap.
pub struct UserConfig {
    pub path: PathBuf,
    pub version: String,
    pub config: Value,
}

fn absolute(path: &Path) -> Result<PathBuf, SetupError> {
    std::path::absolute(path).map_err(|_| SetupError("The Codex configuration path is invalid."))
}

fn config_path(path: &Path) -> Result<PathBuf, SetupError> {
    let mut base = absolute(path)?;
    let mut suffix = Vec::new();
    loop {
        match std::fs::canonicalize(&base) {
            Ok(mut real) => {
                for part in suffix.into_iter().rev() {
                    real.push(part);
                }
                return Ok(real);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                if std::fs::symlink_metadata(&base).is_ok() {
                    return Err(SetupError("The Codex configuration path is a broken link."));
                }
                suffix.push(
                    base.file_name()
                        .ok_or(SetupError("The Codex configuration path is invalid."))?
                        .to_owned(),
                );
                if !base.pop() {
                    return Err(SetupError("The Codex configuration path is invalid."));
                }
            }
            Err(_) => return Err(SetupError("The Codex configuration path is unavailable.")),
        }
    }
}

pub fn user_config(config: &Value, home: &Path) -> Result<UserConfig, SetupError> {
    let path = config_path(&home.join("config.toml"))?;
    let invalid = SetupError(
        "Codex did not expose a versioned user configuration; no settings were changed.",
    );
    let mut layers = config
        .get("layers")
        .and_then(Value::as_array)
        .ok_or(invalid)?
        .iter()
        .filter(|layer| {
            layer["name"]["type"] == "user"
                && layer["name"]
                    .get("profile")
                    .is_none_or(|p| p.is_null() || p == "")
                && layer["name"]["file"]
                    .as_str()
                    .and_then(|file| config_path(Path::new(file)).ok())
                    .as_ref()
                    == Some(&path)
        });
    let layer = layers.next().ok_or(invalid)?;
    if layers.next().is_some() {
        return Err(invalid);
    }
    let version = layer["version"]
        .as_str()
        .filter(|v| !v.is_empty())
        .ok_or(invalid)?
        .to_owned();
    let values = layer
        .get("config")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or_else(|| json!({}));
    if !values.is_object() {
        return Err(invalid);
    }
    Ok(UserConfig {
        path,
        version,
        config: values,
    })
}

fn server(config: &Value) -> Option<&Map<String, Value>> {
    config.get("mcp_servers")?.get(SERVER)?.as_object()
}

fn environment(server: &Map<String, Value>) -> Result<Map<String, Value>, SetupError> {
    match server.get("env") {
        None | Some(Value::Null) => Ok(Map::new()),
        Some(Value::Object(env)) if env.values().all(Value::is_string) => Ok(env.clone()),
        _ => Err(SetupError(
            "The Codex server environment is malformed; repair it before retrying.",
        )),
    }
}

pub fn validated_daemon_url(url: &str) -> Result<String, SetupError> {
    crate::daemon_url::validate(url)
        .map_err(|_| SetupError("The configured memory daemon URL is invalid."))
}

pub struct ConnectionPlan {
    home: PathBuf,
    url: String,
    before: Map<String, Value>,
    after: Map<String, Value>,
    literal: Option<String>,
    write: WritePlan,
}
impl ConnectionPlan {
    pub fn env(&self) -> &Map<String, Value> {
        &self.after
    }
    pub fn previous_env(&self) -> &Map<String, Value> {
        &self.before
    }
}

pub fn plan_replace(
    config: &Value,
    home: &Path,
    url: &str,
    token_file: Option<&Path>,
    no_spawn: bool,
) -> Result<ConnectionPlan, SetupError> {
    let url = validated_daemon_url(url)?;
    let home = absolute(home)?;
    let user = user_config(config, &home)?;
    let user_server = server(&user.config)
        .filter(|s| !s.is_empty())
        .ok_or(SetupError(
            "Codex's user configuration has no pseudolife-memory server.",
        ))?;
    let effective = server(&config["config"]).ok_or(SetupError(
        "Codex's effective configuration has no pseudolife-memory server.",
    ))?;
    let before = environment(user_server)?;
    let effective_env = environment(effective)?;
    let forwarded = effective.get("env_vars").and_then(Value::as_array);
    if [URL_KEY, FILE_KEY, TOKEN_KEY].iter().any(|key| {
        effective_env.get(*key) != before.get(*key)
            || (forwarded.is_some_and(|f| f.iter().any(|v| v == key)) && !before.contains_key(*key))
    }) {
        return Err(SetupError(
            "Connection settings come from another Codex configuration layer; replace mode did not change them.",
        ));
    }
    // An effective false/forwarded no-spawn choice must not be silently overridden.
    if no_spawn
        && (effective_env.get(NO_SPAWN_KEY) != before.get(NO_SPAWN_KEY)
            || (forwarded.is_some_and(|f| f.iter().any(|v| v == NO_SPAWN_KEY))
                && !before.contains_key(NO_SPAWN_KEY)
                && !user_server
                    .get("env_vars")
                    .and_then(Value::as_array)
                    .is_some_and(|vars| vars.iter().any(|v| v == NO_SPAWN_KEY))))
    {
        return Err(SetupError(
            "No-spawn settings come from another Codex configuration layer; remove PSEUDOLIFE_MCP_NO_SPAWN from that layer's env or env_vars, then retry replace mode.",
        ));
    }
    let mut after = before.clone();
    after.insert(URL_KEY.to_owned(), json!(url));
    if no_spawn
        && !before
            .get(NO_SPAWN_KEY)
            .and_then(Value::as_str)
            .is_some_and(truthy)
    {
        after.insert(NO_SPAWN_KEY.to_owned(), json!("1"));
    }
    let mut literal = None;
    if let Some(path) = token_file {
        after.insert(FILE_KEY.to_owned(), json!(absolute(path)?));
    } else if !before.contains_key(FILE_KEY)
        && let Some(token) = before
            .get(TOKEN_KEY)
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
    {
        CredentialProvider::new(Some(token.to_owned()), None).map_err(|_| {
            SetupError("The configured credential is malformed; repair it before retrying.")
        })?;
        literal = Some(token.to_owned());
        after.insert(FILE_KEY.to_owned(), json!(token_copy_path(&home)));
    }
    if after
        .get(FILE_KEY)
        .and_then(Value::as_str)
        .is_some_and(|v| !v.is_empty())
    {
        after.remove(TOKEN_KEY);
    }
    let edit = Edit::new(&["mcp_servers", SERVER, "env"], json!(after));
    let write = WritePlan::new(config, &home, vec![edit])?;
    Ok(ConnectionPlan {
        home,
        url,
        before,
        after,
        literal,
        write,
    })
}

fn truthy(value: &str) -> bool {
    matches!(
        value.trim().to_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

/// Register a native launcher in the user layer, retaining its explicit options
/// and unrelated environment keys. A same-name foreign transport is a refusal.
pub fn plan_registration(
    config: &Value,
    home: &Path,
    command: &Path,
    args: &[String],
    env: &Map<String, Value>,
) -> Result<WritePlan, SetupError> {
    if !command.is_absolute() || !env.values().all(Value::is_string) {
        return Err(SetupError(
            "Registration needs an absolute launcher and string environment values.",
        ));
    }
    let user = user_config(config, home)?;
    let existing = server(&user.config);
    let effective = server(&config["config"]);
    if existing.is_none() && effective.is_some_and(|s| !s.is_empty()) {
        return Err(SetupError(
            "The Codex registration comes from another configuration layer; setup did not change it.",
        ));
    }
    let mut updated = existing.cloned().unwrap_or_default();
    if !updated.is_empty() {
        let command = updated.get("command").and_then(Value::as_str).unwrap_or("");
        let name = command
            .rsplit(['/', '\\'])
            .next()
            .unwrap_or("")
            .to_ascii_lowercase();
        let args = updated.get("args").cloned().unwrap_or_else(|| json!([]));
        let own = (matches!(
            name.as_str(),
            "pseudolife-mcp" | "pseudolife-mcp.exe" | "pseudolife-stdio" | "pseudolife-stdio.exe"
        ) && (args == json!([]) || args == json!(["shim"])))
            || (name.starts_with("python")
                && (args == json!(["-m", "pseudolife_memory.cli"])
                    || args == json!(["-m", "pseudolife_memory.cli", "shim"])));
        if !own || updated.get("url").is_some_and(|v| !v.is_null()) {
            return Err(SetupError(
                "A foreign Codex server uses the Pseudolife name; rename it before retrying.",
            ));
        }
        if let Some(effective) = effective {
            let current = environment(&updated)?;
            let effective_env = environment(effective)?;
            if env
                .keys()
                .any(|key| current.get(key) != effective_env.get(key))
            {
                return Err(SetupError(
                    "Registration settings come from another Codex configuration layer; setup did not change them.",
                ));
            }
        }
    }
    let mut merged = environment(&updated)?;
    merged.extend(env.clone());
    if merged.contains_key(FILE_KEY) {
        merged.remove(TOKEN_KEY);
    }
    updated.insert("command".into(), json!(command));
    updated.insert("args".into(), json!(args));
    updated.insert("env".into(), json!(merged));
    // Null is Codex's typed representation of an omitted optional setting.
    for (key, value) in [
        ("required", json!(true)),
        ("startup_timeout_sec", json!(240.0)),
        ("tool_timeout_sec", json!(240.0)),
    ] {
        if effective
            .and_then(|s| s.get(key))
            .is_none_or(Value::is_null)
            && updated.get(key).is_none_or(Value::is_null)
        {
            updated.insert(key.into(), value);
        }
    }
    WritePlan::new(
        config,
        home,
        vec![Edit::new(&["mcp_servers", SERVER], json!(updated))],
    )
}

/// A single scoped replacement, never an entire effective configuration.
pub struct Edit {
    path: Vec<String>,
    value: Value,
}
impl Edit {
    pub fn new(path: &[&str], value: Value) -> Self {
        Self {
            path: path.iter().map(|s| (*s).to_owned()).collect(),
            value,
        }
    }
    fn value_at<'a>(&self, config: &'a Value) -> Option<&'a Value> {
        self.path.iter().try_fold(config, |node, key| node.get(key))
    }
    fn request(&self) -> Value {
        json!({"keyPath":dotted(&self.path.iter().map(String::as_str).collect::<Vec<_>>()),"value":self.value,"mergeStrategy":"replace"})
    }
}

pub struct WritePlan {
    user: UserConfig,
    edits: Vec<Edit>,
    before: Vec<Value>,
}

fn matches_write(before: &Value, after: &Value, actual: Option<&Value>) -> bool {
    if let Some(wanted) = after.as_object() {
        let Some(actual) = actual.and_then(Value::as_object) else {
            return false;
        };
        if !wanted
            .iter()
            .all(|(key, value)| matches_write(&before[key], value, actual.get(key)))
        {
            return false;
        }
        return before.as_object().is_none_or(|old| {
            old.keys()
                .all(|key| wanted.contains_key(key) || !actual.contains_key(key))
        });
    }
    if after.is_null() {
        return actual.is_none_or(Value::is_null);
    }
    actual == Some(after)
}
impl WritePlan {
    pub fn new(config: &Value, home: &Path, edits: Vec<Edit>) -> Result<Self, SetupError> {
        let user = user_config(config, home)?;
        let mut changed = Vec::new();
        let mut before = Vec::new();
        for edit in edits {
            if edit.path.is_empty() {
                return Err(SetupError("A scoped Codex setting is required."));
            }
            let original = edit.value_at(&user.config).cloned().unwrap_or(Value::Null);
            if original != edit.value {
                before.push(original);
                changed.push(edit);
            }
        }
        // Reading and validating the backup source is part of preflight.
        files::read_config(&user.path)?;
        Ok(Self {
            user,
            edits: changed,
            before,
        })
    }

    pub fn changed(&self) -> bool {
        !self.edits.is_empty()
    }

    pub async fn apply(
        self,
        client: &mut impl Rpc,
        cwd: &Path,
    ) -> Result<AppliedWrite, SetupError> {
        let _lock = files::TransactionLock::acquire(&self.user.path)?;
        self.apply_locked(client, cwd).await
    }

    async fn apply_locked(
        self,
        client: &mut impl Rpc,
        cwd: &Path,
    ) -> Result<AppliedWrite, SetupError> {
        let mut applied = AppliedWrite {
            plan: self,
            backup: None,
            written_version: None,
        };
        if !applied.plan.changed() {
            return Ok(applied);
        }
        if let Some(bytes) = files::read_config(&applied.plan.user.path)? {
            applied.backup = Some(files::backup(&applied.plan.user.path, &bytes)?);
        }
        let response = client
            .rpc(
                "config/batchWrite",
                json!({"edits":applied.plan.edits.iter().map(Edit::request).collect::<Vec<_>>(),
            "filePath":applied.plan.user.path,"expectedVersion":applied.plan.user.version}),
            )
            .await;
        let response = match response {
            Ok(response) => response,
            Err(error) => {
                // Matching desired values are not evidence of ownership: another
                // writer may have won this CAS, or our response may have been lost.
                let current = client
                    .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
                    .await
                    .map_err(|_| ROLLBACK_UNVERIFIED)?;
                let user = user_config(
                    &current,
                    applied.plan.user.path.parent().ok_or(ROLLBACK_UNVERIFIED)?,
                )
                .map_err(|_| ROLLBACK_UNVERIFIED)?;
                if user.version != applied.plan.user.version
                    || user.config != applied.plan.user.config
                {
                    return Err(ROLLBACK_UNVERIFIED);
                }
                return Err(error);
            }
        };
        applied.written_version = Some(
            response["version"]
                .as_str()
                .filter(|v| !v.is_empty())
                .ok_or(ROLLBACK_UNVERIFIED)?
                .to_owned(),
        );
        let result = async {
            let current = client.rpc("config/read", json!({"includeLayers":true,"cwd":cwd})).await?;
            let user=user_config(&current,applied.plan.user.path.parent().ok_or(ROLLBACK_UNVERIFIED)?)?;
            if Some(&user.version)!=applied.written_version.as_ref() {return Err(ROLLBACK_UNVERIFIED);}
            if applied.plan.edits.iter().zip(&applied.plan.before).any(|(edit,before)| !matches_write(before,&edit.value,edit.value_at(&current["config"]))) {
                return Err(SetupError("Codex saved settings but its effective configuration differs; check project or managed overrides before reconnecting."));
            }
            Ok(())
        }.await;
        if let Err(error) = result {
            applied
                .rollback_locked(client, cwd)
                .await
                .map_err(|_| ROLLBACK_UNVERIFIED)?;
            return Err(error);
        }
        Ok(applied)
    }
}

pub struct AppliedWrite {
    plan: WritePlan,
    pub backup: Option<PathBuf>,
    written_version: Option<String>,
}
impl AppliedWrite {
    pub async fn rollback(&mut self, client: &mut impl Rpc, cwd: &Path) -> Result<(), SetupError> {
        let _lock = files::TransactionLock::acquire(&self.plan.user.path)?;
        self.rollback_locked(client, cwd).await
    }
    async fn rollback_locked(
        &mut self,
        client: &mut impl Rpc,
        cwd: &Path,
    ) -> Result<(), SetupError> {
        let Some(written_version) = self.written_version.as_ref() else {
            return Ok(());
        };
        let current = client
            .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
            .await?;
        let home = self
            .plan
            .user
            .path
            .parent()
            .ok_or(SetupError("Invalid Codex home."))?;
        let user = user_config(&current, home)?;
        if &user.version != written_version {
            return Err(SetupError(
                "Codex rollback refused a newer configuration version; review the private config backup.",
            ));
        }
        let old = self
            .plan
            .edits
            .iter()
            .zip(&self.plan.before)
            .all(|(edit, before)| edit.value_at(&user.config).unwrap_or(&Value::Null) == before);
        if old {
            self.written_version = None;
            return Ok(());
        }
        if self
            .plan
            .edits
            .iter()
            .any(|edit| edit.value_at(&user.config) != Some(&edit.value))
        {
            return Err(SetupError(
                "Codex rollback refused settings changed by another writer; review the private config backup.",
            ));
        }
        let edits = self
            .plan
            .edits
            .iter()
            .zip(&self.plan.before)
            .map(|(edit, value)| {
                Edit {
                    path: edit.path.clone(),
                    value: value.clone(),
                }
                .request()
            })
            .collect::<Vec<_>>();
        client
            .rpc(
                "config/batchWrite",
                json!({"edits":edits,"filePath":user.path,"expectedVersion":user.version}),
            )
            .await?;
        let current = client
            .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
            .await?;
        let user = user_config(&current, home)?;
        if self
            .plan
            .edits
            .iter()
            .zip(&self.plan.before)
            .any(|(edit, before)| edit.value_at(&user.config).unwrap_or(&Value::Null) != before)
        {
            return Err(SetupError(
                "Codex rollback could not be verified; review the private config backup.",
            ));
        }
        self.written_version = None;
        Ok(())
    }
}

pub struct AppliedConnection {
    write: AppliedWrite,
    token: Option<PrivateEdit>,
    connection: PrivateEdit,
    report: Value,
}
impl AppliedConnection {
    pub fn report(&self) -> &Value {
        &self.report
    }
    pub async fn rollback(&mut self, client: &mut impl Rpc, cwd: &Path) -> Result<(), SetupError> {
        let _lock = files::TransactionLock::acquire(&self.write.plan.user.path)?;
        self.write.rollback_locked(client, cwd).await?;
        self.connection.rollback()?;
        if let Some(token) = &mut self.token {
            token.rollback()?;
        }
        Ok(())
    }
}

pub async fn apply_replace(
    client: &mut impl Rpc,
    cwd: &Path,
    plan: ConnectionPlan,
) -> Result<AppliedConnection, SetupError> {
    let target = plan
        .after
        .get(FILE_KEY)
        .and_then(Value::as_str)
        .filter(|p| !p.is_empty());
    let mut token = if let (Some(literal), Some(path)) = (&plan.literal, target) {
        if std::fs::symlink_metadata(path).is_ok() {
            CredentialProvider::new(None, Some(PathBuf::from(path)))
                .and_then(|p| p.snapshot())
                .map_err(|_| {
                    SetupError(
                        "The Codex token copy is unsafe or malformed; repair it before retrying.",
                    )
                })?;
        }
        Some(PrivateEdit::prepare(
            Path::new(path),
            literal.as_bytes().to_vec(),
        )?)
    } else {
        None
    };
    if token.is_none()
        && let Some(path) = target
    {
        CredentialProvider::new(None, Some(PathBuf::from(path))).and_then(|p|p.snapshot()).map_err(|_| SetupError("The configured credential file is missing, unsafe, or malformed; repair it before retrying."))?;
    }
    let path = connection_path(&plan.home);
    read_connection(&path)?;
    let data = connection_bytes(&plan.url, target.unwrap_or(""));
    let mut connection = PrivateEdit::prepare(&path, data)?;
    let _lock = files::TransactionLock::acquire(&plan.write.user.path)?;
    let current = client
        .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
        .await?;
    if user_config(&current, &plan.home)?.version != plan.write.user.version {
        return Err(SetupError(
            "Codex configuration changed during setup; no connection settings were written. Retry setup.",
        ));
    }
    if let Some(token) = &mut token {
        token.commit()?;
    }
    let mut write = match plan.write.apply_locked(client, cwd).await {
        Ok(write) => write,
        Err(error) => {
            if error != ROLLBACK_UNVERIFIED
                && connection.still_before()
                && let Some(token) = &mut token
            {
                token.rollback()?;
            }
            return Err(error);
        }
    };
    if let Err(error) = connection.commit() {
        write.rollback_locked(client, cwd).await?;
        if let Some(token) = &mut token {
            token.rollback()?;
        }
        return Err(error);
    }
    let mut changed = plan
        .before
        .keys()
        .chain(plan.after.keys())
        .filter(|key| plan.before.get(*key) != plan.after.get(*key))
        .cloned()
        .collect::<Vec<_>>();
    changed.sort();
    changed.dedup();
    let created = token
        .iter()
        .chain(std::iter::once(&connection))
        .filter(|e| e.created())
        .map(|e| e.path().to_owned())
        .collect::<Vec<_>>();
    let report = json!({"daemon_url":plan.url,"credential_file_path":target.unwrap_or(""),"changed":changed,
        "backup":write.backup,"connection_backup":connection.backup,"token_backup":token.as_ref().and_then(|t|t.backup.as_ref()),"created":created});
    Ok(AppliedConnection {
        write,
        token,
        connection,
        report,
    })
}

fn connection_bytes(url: &str, token_file: &str) -> Vec<u8> {
    // The hooks read this exact five-line format without a JSON interpreter.
    format!(
        "{{\n  \"version\": 1,\n  \"daemon_url\": \"{}\",\n  \"token_file\": \"{}\"\n}}\n",
        encode64(url.as_bytes()),
        encode64(token_file.as_bytes())
    )
    .into_bytes()
}

pub fn read_connection(path: &Path) -> Result<Option<(String, String)>, SetupError> {
    let invalid = SetupError(
        "The managed Codex connection file is unsafe or malformed; repair or remove it before retrying.",
    );
    match std::fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(invalid),
        Ok(_) => (),
    }
    let bytes = crate::board::state::read(path, 16384).map_err(|_| invalid)?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| invalid)?;
    let url = decode64(value["daemon_url"].as_str().ok_or(invalid)?).ok_or(invalid)?;
    let token = decode64(value["token_file"].as_str().ok_or(invalid)?).ok_or(invalid)?;
    Ok(Some((url, token)))
}

const BASE64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
fn encode64(bytes: &[u8]) -> String {
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let bits = ((chunk[0] as u32) << 16)
            | ((chunk.get(1).copied().unwrap_or(0) as u32) << 8)
            | chunk.get(2).copied().unwrap_or(0) as u32;
        for shift in [18, 12, 6, 0] {
            out.push(BASE64[((bits >> shift) & 63) as usize] as char);
        }
        for _ in chunk.len()..3 {
            out.pop();
        }
        for _ in chunk.len()..3 {
            out.push('=');
        }
    }
    out
}
fn decode64(text: &str) -> Option<String> {
    if !text.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::new();
    let mut done = false;
    for chunk in text.as_bytes().chunks(4) {
        if done {
            return None;
        }
        let mut bits = 0u32;
        let mut padding = 0;
        for byte in chunk {
            bits <<= 6;
            if *byte == b'=' {
                padding += 1;
                done = true;
            } else {
                if done {
                    return None;
                }
                bits |= BASE64.iter().position(|c| c == byte)? as u32;
            }
        }
        if padding > 2 {
            return None;
        }
        out.push((bits >> 16) as u8);
        if padding < 2 {
            out.push((bits >> 8) as u8);
        }
        if padding < 1 {
            out.push(bits as u8);
        }
    }
    String::from_utf8(out).ok()
}
