//! Codex configuration is written through its versioned app-server API.
//! Never flatten project/managed settings into the user configuration.
mod credentials;
mod outcome;
mod rpc;
pub use credentials::{configure_credential_file, daemon_url, effective_value};
pub use outcome::{SetupFailure, apply_replace_detailed};
use outcome::{WriteDisposition, WriteFailure};
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

/// Diagnostics contain fixed text and locally selected paths/keys only, never
/// credential values or untrusted app-server response bodies.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SetupError {
    message: std::borrow::Cow<'static, str>,
    definite_rejection: bool,
}
/// Preserve the static constructor used by callers of the shared setup seam.
#[allow(non_snake_case)]
pub const fn SetupError(message: &'static str) -> SetupError {
    SetupError {
        message: std::borrow::Cow::Borrowed(message),
        definite_rejection: false,
    }
}
impl SetupError {
    pub(crate) fn detail(message: String) -> Self {
        Self {
            message: std::borrow::Cow::Owned(message),
            definite_rejection: false,
        }
    }

    /// A correlated RPC error response confirms that the request finished.
    /// Do not use for transport errors, timeouts, cancellation, or lost replies.
    pub fn rejected(message: &'static str) -> Self {
        Self {
            message: message.into(),
            definite_rejection: true,
        }
    }
}
const ROLLBACK_UNVERIFIED: SetupError = SetupError(
    "Codex setup failed and rollback could not be verified; the private token copy was retained for the saved configuration. Review the private config backup before retrying.",
);
const CONFIG_CHANGED: SetupError = SetupError(
    "Codex configuration changed while the write outcome was unconfirmed; no rollback was attempted and the private token copy was retained. Review the configuration and retry.",
);
impl fmt::Display for SetupError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
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
#[derive(Clone)]
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
                #[cfg(windows)]
                {
                    real = ordinary_windows_path(real);
                }
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

#[cfg(windows)]
fn ordinary_windows_path(path: PathBuf) -> PathBuf {
    use std::os::windows::ffi::OsStrExt;
    let text = path.to_string_lossy();
    let candidate = if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{rest}"))
    } else if let Some(rest) = text.strip_prefix(r"\\?\") {
        PathBuf::from(rest)
    } else {
        return path;
    };
    // Keep verbatim paths when Win32 length or normalization rules require
    // them. Resolving the ordinary spelling must identify the exact same path.
    if candidate.as_os_str().encode_wide().count() < 260
        && std::fs::canonicalize(&candidate).is_ok_and(|resolved| resolved == path)
    {
        candidate
    } else {
        path
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
        .ok_or_else(|| invalid.clone())?
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
    let layer = layers.next().ok_or_else(|| invalid.clone())?;
    if layers.next().is_some() {
        return Err(invalid);
    }
    let version = layer["version"]
        .as_str()
        .filter(|v| !v.is_empty())
        .ok_or_else(|| invalid.clone())?
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

fn same_env_value(key: &str, left: Option<&Value>, right: Option<&Value>) -> bool {
    if left == right {
        return true;
    }
    // Windows may report the same token through its long and 8.3 names.
    // Open both spellings through the private-file boundary: canonicalizing
    // alone would also accept redirected or otherwise unsafe credential files.
    #[cfg(windows)]
    if key == FILE_KEY
        && let (Some(left), Some(right)) =
            (left.and_then(Value::as_str), right.and_then(Value::as_str))
        && Path::new(left).is_absolute()
        && Path::new(right).is_absolute()
        && let (Ok(left), Ok(right)) = (
            crate::board::state::open(Path::new(left)),
            crate::board::state::open(Path::new(right)),
        )
    {
        return left.identity == right.identity;
    }
    // Planned files that do not exist yet still require the exact spelling.
    let _ = key;
    false
}

fn same_setting(path: &[String], left: &Value, right: &Value) -> bool {
    if path
        .iter()
        .map(String::as_str)
        .eq(["mcp_servers", SERVER, "env", FILE_KEY])
    {
        return same_env_value(FILE_KEY, Some(left), Some(right));
    }
    if let (Some(left), Some(right)) = (left.as_object(), right.as_object()) {
        return left.len() == right.len()
            && left.iter().all(|(key, value)| {
                let mut child = path.to_vec();
                child.push(key.clone());
                right
                    .get(key)
                    .is_some_and(|other| same_setting(&child, value, other))
            });
    }
    left == right
}

fn foreign_authority<'a>(config: &'a Value, key: &str) -> Option<&'a Value> {
    config["layers"].as_array().and_then(|layers| {
        let user = layers.iter().position(|layer| {
            layer["name"]["type"] == "user"
                && layer["name"]
                    .get("profile")
                    .is_none_or(|p| p.is_null() || p == "")
        })?;
        // config/read lists the strongest layer first. Disabled projects are
        // still listed for diagnostics, but do not contribute effective values.
        layers[..user].iter().find(|layer| {
            layer["name"]["type"] != "user"
                && layer.get("disabledReason").is_none_or(Value::is_null)
                && server(&layer["config"]).is_some_and(|s| {
                    s.get("env").is_some_and(|env| env.get(key).is_some())
                        || s.get("env_vars")
                            .and_then(Value::as_array)
                            .is_some_and(|vars| vars.iter().any(|v| v == key))
                })
        })
    })
}

fn authority_error(config: &Value, key: &str) -> SetupError {
    let location = foreign_authority(config, key)
        .and_then(|layer| {
            layer["name"]["file"]
                .as_str()
                .map(PathBuf::from)
                .or_else(|| {
                    layer["name"]["dotCodexFolder"]
                        .as_str()
                        .map(|folder| Path::new(folder).join("config.toml"))
                })
        })
        .map(|file| format!("{file:?}"))
        .unwrap_or_else(|| "the project or managed configuration layer".into());
    SetupError::detail(format!(
        "{key} comes from {location}; remove that key from its env or env_vars and retry. No connection settings were changed."
    ))
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
    let home = config_path(home)?;
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
        !same_env_value(key, effective_env.get(*key), before.get(*key))
            || (forwarded.is_some_and(|f| f.iter().any(|v| v == key)) && !before.contains_key(*key))
    }) {
        return Err(SetupError(
            "Connection settings come from another Codex configuration layer; replace mode did not change them.",
        ));
    }
    // The explicit request is persisted in the user layer. Forwarded values
    // in this installer's environment do not describe Codex's later process.
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
    let mut write = WritePlan::new(config, &home, vec![edit])?;
    write.verify_no_spawn = no_spawn;
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
                .any(|key| !same_env_value(key, current.get(key), effective_env.get(key)))
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
#[derive(Clone)]
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
    home: PathBuf,
    user: UserConfig,
    edits: Vec<Edit>,
    verification: Vec<Edit>,
    server_before: Option<Map<String, Value>>,
    before: Vec<Value>,
    verify_no_spawn: bool,
}

fn matches_write(path: &[String], before: &Value, after: &Value, actual: Option<&Value>) -> bool {
    if let Some(wanted) = after.as_object() {
        let Some(actual) = actual.and_then(Value::as_object) else {
            return false;
        };
        if !wanted.iter().all(|(key, value)| {
            let mut child = path.to_vec();
            child.push(key.clone());
            matches_write(&child, &before[key], value, actual.get(key))
        }) {
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
    actual.is_some_and(|actual| same_setting(path, after, actual))
}
impl WritePlan {
    pub fn new(config: &Value, home: &Path, edits: Vec<Edit>) -> Result<Self, SetupError> {
        let user = user_config(config, home)?;
        let mut changed = Vec::new();
        let mut verification = Vec::new();
        let mut before = Vec::new();
        for edit in edits {
            if edit.path.is_empty() {
                return Err(SetupError("A scoped Codex setting is required."));
            }
            let original = edit.value_at(&user.config).cloned().unwrap_or(Value::Null);
            verification.push(edit.clone());
            if !same_setting(&edit.path, &original, &edit.value) {
                before.push(original);
                changed.push(edit);
            }
        }
        // Reading and validating the backup source is part of preflight.
        files::read_config(&user.path)?;
        Ok(Self {
            home: config_path(home)?,
            user,
            edits: changed,
            verification,
            server_before: server(&config["config"]).cloned(),
            before,
            verify_no_spawn: false,
        })
    }

    pub fn changed(&self) -> bool {
        !self.edits.is_empty()
    }

    fn check_no_spawn_override(&self, config: &Value) -> Result<(), SetupError> {
        if self.verify_no_spawn
            && foreign_authority(config, NO_SPAWN_KEY)
                .and_then(|layer| server(&layer["config"]))
                .and_then(|s| s.get("env"))
                .and_then(|env| env.get(NO_SPAWN_KEY))
                .and_then(Value::as_str)
                .is_some_and(|value| !truthy(value))
        {
            return Err(authority_error(config, NO_SPAWN_KEY));
        }
        Ok(())
    }

    async fn verify_unchanged(&self, client: &mut impl Rpc, cwd: &Path) -> Result<(), SetupError> {
        let current = client
            .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
            .await?;
        let user = user_config(&current, &self.home)?;
        if user.version != self.user.version || !same_setting(&[], &user.config, &self.user.config)
        {
            return Err(CONFIG_CHANGED);
        }
        if self.verify_no_spawn
            && !server(&current["config"])
                .and_then(|s| s.get("env"))
                .and_then(|env| env.get(NO_SPAWN_KEY))
                .and_then(Value::as_str)
                .is_some_and(truthy)
        {
            return Err(authority_error(&current, NO_SPAWN_KEY));
        }
        if self.verification.is_empty()
            && !same_setting(
                &["mcp_servers".into(), SERVER.into()],
                &json!(server(&current["config"])),
                &json!(self.server_before),
            )
        {
            return Err(SetupError(
                "Codex's effective server changed during setup; review project or managed overrides and retry.",
            ));
        }
        self.verify_effective(&current, &user)
    }

    fn verify_effective(&self, current: &Value, user: &UserConfig) -> Result<(), SetupError> {
        let mut effective = current["config"].clone();
        if self.verify_no_spawn {
            let configured = server(&effective).ok_or(SetupError(
                "Codex's effective server disappeared during setup.",
            ))?;
            if !configured
                .get("env")
                .and_then(|env| env.get(NO_SPAWN_KEY))
                .and_then(Value::as_str)
                .is_some_and(truthy)
            {
                return Err(SetupError(
                    "Codex's effective no-spawn setting changed during setup; retry after reviewing the overriding layer.",
                ));
            }
            // Equivalent truthy spellings are allowed while the user value
            // must survive exactly; every other requested field is verified.
            let user_value = server(&user.config)
                .and_then(|s| s.get("env"))
                .and_then(|env| env.get(NO_SPAWN_KEY))
                .cloned();
            if let Some(env) = effective["mcp_servers"][SERVER]["env"].as_object_mut() {
                if let Some(value) = user_value {
                    env.insert(NO_SPAWN_KEY.into(), value);
                } else {
                    env.remove(NO_SPAWN_KEY);
                }
            }
        }
        if self.verification.iter().any(|edit| {
            !matches_write(
                &edit.path,
                edit.value_at(&self.user.config).unwrap_or(&Value::Null),
                &edit.value,
                edit.value_at(&effective),
            )
        }) {
            return Err(SetupError(
                "Codex effective settings differ from the requested configuration; check project or managed overrides before reconnecting.",
            ));
        }
        Ok(())
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
        self.apply_locked_detailed(client, cwd)
            .await
            .map_err(|failure| failure.reason)
    }

    async fn apply_locked_detailed(
        self,
        client: &mut impl Rpc,
        cwd: &Path,
    ) -> Result<AppliedWrite, Box<WriteFailure>> {
        let mut applied = AppliedWrite::unwritten(self);
        let mut disposition = WriteDisposition::Untouched;
        let mut rollback_error = None;
        let result = async {
            if !applied.plan.changed() {
                applied.plan.verify_unchanged(client, cwd).await?;
                return Ok(());
            }
            if let Some(bytes) = files::read_config(&applied.plan.user.path)? {
                applied.backup = Some(files::backup(&applied.plan.user.path, &bytes)?);
            }
            disposition = WriteDisposition::Pending;
            applied.settled = None;
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
                    let user = user_config(&current, &applied.plan.home)
                        .map_err(|_| ROLLBACK_UNVERIFIED)?;
                    if user.version != applied.plan.user.version
                        || !same_setting(&[], &user.config, &applied.plan.user.config)
                    {
                        return Err(CONFIG_CHANGED);
                    }
                    // An unchanged read is not a completion barrier: a timed-out
                    // request may still commit after the snapshot was returned.
                    if !error.definite_rejection {
                        return Err(ROLLBACK_UNVERIFIED);
                    }
                    disposition = WriteDisposition::Untouched;
                    applied.settled = Some(user);
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
                let current = client
                    .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
                    .await?;
                let user = user_config(&current, &applied.plan.home)?;
                if Some(&user.version) != applied.written_version.as_ref() {
                    return Err(ROLLBACK_UNVERIFIED);
                }
                applied.plan.verify_effective(&current, &user)
            }
            .await;
            if let Err(error) = result {
                if let Err(error) = applied.rollback_locked(client, cwd).await {
                    rollback_error = Some(error);
                    return Err(ROLLBACK_UNVERIFIED);
                }
                disposition = WriteDisposition::Restored;
                return Err(error);
            }
            Ok(())
        }
        .await;
        match result {
            Ok(()) => Ok(applied),
            Err(reason) => Err(Box::new(WriteFailure {
                reason,
                write: applied,
                disposition,
                rollback_error,
            })),
        }
    }
}

pub struct AppliedWrite {
    plan: WritePlan,
    pub backup: Option<PathBuf>,
    written_version: Option<String>,
    // An unchanged or acknowledged restored configuration permits private-file
    // cleanup only while this exact snapshot still holds on a later retry.
    settled: Option<UserConfig>,
}
impl AppliedWrite {
    fn unwritten(plan: WritePlan) -> Self {
        Self {
            settled: Some(plan.user.clone()),
            plan,
            backup: None,
            written_version: None,
        }
    }
    pub async fn rollback(&mut self, client: &mut impl Rpc, cwd: &Path) -> Result<(), SetupError> {
        let _lock = files::TransactionLock::acquire(&self.plan.user.path)?;
        self.rollback_locked(client, cwd).await
    }
    async fn rollback_locked(
        &mut self,
        client: &mut impl Rpc,
        cwd: &Path,
    ) -> Result<(), SetupError> {
        let current = client
            .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
            .await?;
        let home = &self.plan.home;
        let user = user_config(&current, home)?;
        let Some(written_version) = self.written_version.as_ref() else {
            if self.settled.as_ref().is_some_and(|settled| {
                settled.version == user.version && same_setting(&[], &settled.config, &user.config)
            }) {
                return Ok(());
            }
            return Err(SetupError(
                "Codex rollback refused configuration changed since compensation; review the remaining private files before retrying.",
            ));
        };
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
            .all(|(edit, before)| {
                same_setting(
                    &edit.path,
                    edit.value_at(&user.config).unwrap_or(&Value::Null),
                    before,
                )
            });
        if old {
            self.written_version = None;
            self.settled = Some(user);
            return Ok(());
        }
        if self.plan.edits.iter().any(|edit| {
            !edit
                .value_at(&user.config)
                .is_some_and(|actual| same_setting(&edit.path, actual, &edit.value))
        }) {
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
        let restored = client
            .rpc(
                "config/batchWrite",
                json!({"edits":edits,"filePath":user.path,"expectedVersion":user.version}),
            )
            .await?;
        let restored_version = restored["version"]
            .as_str()
            .filter(|v| !v.is_empty())
            .ok_or(ROLLBACK_UNVERIFIED)?
            .to_owned();
        // Preserve the acknowledged restoration before a fallible/cancellable
        // readback. A later retry can verify that same version without writing.
        self.written_version = Some(restored_version.clone());
        let current = client
            .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
            .await?;
        let user = user_config(&current, home)?;
        if user.version != restored_version
            || self
                .plan
                .edits
                .iter()
                .zip(&self.plan.before)
                .any(|(edit, before)| {
                    !same_setting(
                        &edit.path,
                        edit.value_at(&user.config).unwrap_or(&Value::Null),
                        before,
                    )
                })
        {
            return Err(SetupError(
                "Codex rollback could not be verified; review the private config backup.",
            ));
        }
        self.written_version = None;
        self.settled = Some(user);
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
        self.rollback_detailed(client, cwd)
            .await
            .map(|_| ())
            .map_err(|failure| failure.reason)
    }
}

pub async fn apply_replace(
    client: &mut impl Rpc,
    cwd: &Path,
    plan: ConnectionPlan,
) -> Result<AppliedConnection, SetupError> {
    apply_replace_detailed(client, cwd, plan)
        .await
        .map_err(|failure| failure.reason)
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
    let bytes = crate::board::state::read(path, 16384).map_err(|_| invalid.clone())?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| invalid.clone())?;
    let url = decode64(
        value["daemon_url"]
            .as_str()
            .ok_or_else(|| invalid.clone())?,
    )
    .ok_or_else(|| invalid.clone())?;
    let token = decode64(
        value["token_file"]
            .as_str()
            .ok_or_else(|| invalid.clone())?,
    )
    .ok_or_else(|| invalid.clone())?;
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
