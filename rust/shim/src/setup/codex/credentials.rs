use super::*;

pub fn effective_value(server: &Map<String, Value>, key: &str) -> Option<String> {
    if let Some(value) = server.get("env").and_then(|env| env.get(key)) {
        return value.as_str().map(str::to_owned);
    }
    if server
        .get("env_vars")
        .and_then(Value::as_array)
        .is_some_and(|vars| vars.iter().any(|value| value == key))
    {
        return std::env::var(key).ok();
    }
    None
}

pub fn daemon_url(server: &Map<String, Value>) -> Result<String, SetupError> {
    let url = effective_value(server, URL_KEY)
        .or_else(|| {
            if server.is_empty() {
                std::env::var(URL_KEY).ok()
            } else {
                None
            }
        })
        .unwrap_or_else(|| crate::daemon_url::DEFAULT_URL.to_owned());
    validated_daemon_url(&url)
}

async fn accepted(url: &str, token: &str) -> bool {
    let Ok(client) = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(3))
        .build()
    else {
        return false;
    };
    client
        .get(format!("{url}/api/episodes?limit=1"))
        .bearer_auth(token)
        .send()
        .await
        .is_ok_and(|response| response.status() == 200)
}

/// Installer credential bootstrap; configured credentials outrank installer or
/// inherited process values. No RPC/file mutation occurs until preflight ends.
pub async fn configure_credential_file(
    client: &mut impl Rpc,
    home: &Path,
    cwd: &Path,
    config: Option<Value>,
    installer_connection: Option<(&str, Option<&str>)>,
) -> Result<Value, SetupError> {
    let config = match config {
        Some(value) => value,
        None => {
            client
                .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
                .await?
        }
    };
    let home = config_path(home)?;
    let user = user_config(&config, &home)?;
    let empty = Map::new();
    let server = server(&user.config).unwrap_or(&empty);
    let effective = super::server(&config["config"]).unwrap_or(&empty);
    let before = environment(server)?;
    let effective_env = environment(effective)?;
    let mut source = effective_value(server, FILE_KEY);
    let mut literal = if source.is_none() {
        effective_value(server, TOKEN_KEY).filter(|v| !v.is_empty())
    } else {
        None
    };
    let mut selected_url = None;
    if source.is_none()
        && literal.is_none()
        && let Some((url, token)) = installer_connection
    {
        let forwarded = effective.get("env_vars").and_then(Value::as_array);
        if let Some(key) = [FILE_KEY, TOKEN_KEY].iter().find(|key| {
            effective_env.contains_key(**key)
                || forwarded.is_some_and(|vars| vars.iter().any(|v| v == *key))
        }) {
            if foreign_authority(&config, key).is_some() {
                return Err(authority_error(&config, key));
            }
            if before.contains_key(*key) && before.get(*key) == effective_env.get(*key) {
                return Err(SetupError::detail(format!(
                    "{key} is empty in Codex's user configuration {:?}; remove that empty key or configure a usable credential, then retry. No connection settings were changed.",
                    user.path
                )));
            }
            return Err(authority_error(&config, key));
        }
        let url = validated_daemon_url(url)?;
        if !effective.is_empty() && (daemon_url(server)? != url || daemon_url(effective)? != url) {
            return Err(SetupError(
                "The installer daemon URL does not match the configured Codex server.",
            ));
        }
        if let Some(token) = token {
            CredentialProvider::new(Some(token.to_owned()), None)
                .map_err(|_| SetupError("The installer-provided daemon credential is invalid."))?;
            if !accepted(&url, token).await {
                return Err(SetupError(
                    "The installer credential was not accepted by the configured daemon.",
                ));
            }
            literal = Some(token.to_owned());
        }
        selected_url = Some(url);
    }
    // Validate authority before staging a token copy. Foreign environment keys
    // remain untouched; only the connection fields must agree with the user.
    if let Some(key) = [FILE_KEY, TOKEN_KEY, URL_KEY].iter().find(|key| {
        effective_env.get(**key) != before.get(**key)
            || (!before.contains_key(**key)
                && effective
                    .get("env_vars")
                    .and_then(Value::as_array)
                    .is_some_and(|v| v.iter().any(|v| v == *key))
                && !server
                    .get("env_vars")
                    .and_then(Value::as_array)
                    .is_some_and(|v| v.iter().any(|v| v == *key)))
    }) {
        return Err(authority_error(&config, key));
    }
    let migrated = before.contains_key(TOKEN_KEY) || selected_url.is_some();
    if let Some(path) = &source {
        let path = crate::credentials::absolute_expanded(Path::new(path))
            .map_err(|_| SetupError("The configured credential file path is invalid."))?;
        CredentialProvider::new(None,Some(path.clone())).and_then(|p|p.snapshot()).map_err(|_|SetupError("The configured credential file is missing, unsafe, or malformed; repair it before retrying."))?;
        source = Some(path.to_string_lossy().into_owned());
    } else if let Some(token) = &literal {
        CredentialProvider::new(Some(token.clone()), None).map_err(|_| {
            SetupError("The configured credential is malformed; repair it before retrying.")
        })?;
        source = Some(token_copy_path(&home).to_string_lossy().into_owned());
    }
    let Some(target) = source else {
        let mut backup = None;
        if let Some(url) = &selected_url {
            let path = connection_path(&home);
            read_connection(&path)?;
            let mut write = PrivateEdit::prepare(&path, connection_bytes(url, ""))?;
            let _lock = super::files::TransactionLock::acquire(&user.path)?;
            let current = client
                .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
                .await?;
            if user_config(&current, &home)?.version != user.version {
                return Err(SetupError(
                    "Codex configuration changed during setup; retry credential setup.",
                ));
            }
            write.commit()?;
            backup = write.backup;
        }
        return Ok(
            json!({"credential_file_configured":false,"credential_file_path":"","daemon_url":selected_url,
            "connection_configured":selected_url.is_some(),"migrated_literal":false,"backup":null,"connection_backup":backup}),
        );
    };
    let url = match selected_url {
        Some(url) => url,
        None => daemon_url(if effective.is_empty() { &empty } else { server })?,
    };
    let mut after = before.clone();
    if !effective.is_empty() {
        after.insert(FILE_KEY.to_owned(), json!(target));
        after.remove(TOKEN_KEY);
    }
    // Even before MCP registration, the hooks need the selected connection and
    // private token copy. An empty edit plan deliberately leaves config alone.
    let edits = if effective.is_empty() {
        vec![]
    } else {
        vec![Edit::new(&["mcp_servers", SERVER, "env"], json!(after))]
    };
    let write = WritePlan::new(&config, &home, edits)?;
    let mut private_after = after.clone();
    private_after.insert(FILE_KEY.to_owned(), json!(target));
    let plan = ConnectionPlan {
        home,
        url: url.clone(),
        before,
        after: private_after,
        literal,
        write,
    };
    let applied = apply_replace(client, cwd, plan).await?;
    Ok(
        json!({"credential_file_configured":true,"credential_file_path":target,"daemon_url":url,
        "connection_configured":true,"migrated_literal":migrated,"backup":applied.report()["backup"],"connection_backup":applied.report()["connection_backup"]}),
    )
}
