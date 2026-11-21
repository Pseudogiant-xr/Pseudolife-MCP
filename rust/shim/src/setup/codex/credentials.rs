use super::*;
use reqwest::StatusCode;
use std::time::Duration;
use tokio::time::{Instant, sleep_until, timeout_at};

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

// The 21 November 2026 fresh-install probe took 3.431 seconds to get HTTP 200
// after /health was already answering. Use the native installers' Rust
// readiness wait (120 retries spaced 1.5 seconds apart) as a hard ceiling.
const STARTUP_BUDGET: Duration = Duration::from_secs(180);
// A single attempt must have time to finish that 3.431-second read.
const ATTEMPT_TIMEOUT: Duration = Duration::from_secs(10);
const RETRY_DELAY: Duration = Duration::from_millis(1500);

pub(crate) enum CredentialCheck {
    Accepted,
    Refused,
    NotReady,
    UnexpectedStatus(StatusCode),
}

impl CredentialCheck {
    pub(crate) fn not_ready_reason(url: &str) -> String {
        format!(
            "The daemon at {url} did not answer the authenticated check in {} seconds and may still be starting.",
            STARTUP_BUDGET.as_secs()
        )
    }
}

pub(crate) async fn wait_for_credential(
    mut probe: impl AsyncFnMut(Duration) -> Option<StatusCode>,
) -> CredentialCheck {
    let deadline = Instant::now() + STARTUP_BUDGET;
    loop {
        let now = Instant::now();
        let remaining = deadline.saturating_duration_since(now);
        if remaining.is_zero() {
            return CredentialCheck::NotReady;
        }
        let attempt = remaining.min(ATTEMPT_TIMEOUT);
        match timeout_at(now + attempt, probe(attempt)).await {
            Ok(Some(StatusCode::OK)) => return CredentialCheck::Accepted,
            Ok(Some(StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN)) => {
                return CredentialCheck::Refused;
            }
            Ok(Some(status)) if !status.is_server_error() => {
                return CredentialCheck::UnexpectedStatus(status);
            }
            // A 5xx, a transport failure or an expired attempt can occur
            // while the daemon starts. Bound both each attempt and the sleep.
            _ => sleep_until(deadline.min(Instant::now() + RETRY_DELAY)).await,
        }
    }
}

async fn verify_installer_credential(url: &str, token: &str) -> Result<(), SetupError> {
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| SetupError("The installer could not start the credential check. Check the HTTPS and proxy settings, then rerun the installer."))?;
    let request = client
        .get(format!("{url}/api/episodes?limit=1"))
        .bearer_auth(token)
        .build()
        .map_err(|_| SetupError("The installer could not prepare the authenticated request. Check the daemon URL and credential settings, then rerun the installer."))?;
    installer_check_result(url, async |_| {
        let request = request.try_clone()?;
        client
            .execute(request)
            .await
            .ok()
            .map(|response| response.status())
    })
    .await
}

async fn installer_check_result(
    url: &str,
    probe: impl AsyncFnMut(Duration) -> Option<StatusCode>,
) -> Result<(), SetupError> {
    let (mut error, failure) = match wait_for_credential(probe).await {
        CredentialCheck::Accepted => return Ok(()),
        CredentialCheck::Refused => (
            SetupError("The installer credential was not accepted by the configured daemon."),
            CredentialCheckFailure::Refused,
        ),
        CredentialCheck::NotReady => (
            SetupError::detail(format!(
                "{} When it is ready, rerun the installer.",
                CredentialCheck::not_ready_reason(url)
            )),
            CredentialCheckFailure::NotReady,
        ),
        CredentialCheck::UnexpectedStatus(status) => (
            SetupError::detail(format!(
                "The daemon at {url} returned HTTP {} to the authenticated check. Check that the address serves the Pseudolife daemon, then rerun the installer.",
                status.as_u16()
            )),
            CredentialCheckFailure::UnexpectedStatus,
        ),
    };
    error.credential_check = Some(failure);
    Err(error)
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
    configure_credential_file_with_check(
        client,
        home,
        cwd,
        config,
        installer_connection,
        verify_installer_credential,
    )
    .await
}

async fn configure_credential_file_with_check(
    client: &mut impl Rpc,
    home: &Path,
    cwd: &Path,
    config: Option<Value>,
    installer_connection: Option<(&str, Option<&str>)>,
    check: impl AsyncFnOnce(&str, &str) -> Result<(), SetupError>,
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
            if before.contains_key(*key)
                && same_env_value(key, before.get(*key), effective_env.get(*key))
            {
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
            check(&url, token).await?;
            literal = Some(token.to_owned());
        }
        selected_url = Some(url);
    }
    // Validate authority before staging a token copy. Foreign environment keys
    // remain untouched; only the connection fields must agree with the user.
    if let Some(key) = [FILE_KEY, TOKEN_KEY, URL_KEY].iter().find(|key| {
        !same_env_value(key, effective_env.get(**key), before.get(**key))
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
    let checked = selected_url.is_some();
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
    let mut report = json!({"credential_file_configured":true,"credential_file_path":target,"daemon_url":url,
        "connection_configured":true,"migrated_literal":migrated,"backup":applied.report()["backup"],"connection_backup":applied.report()["connection_backup"]});
    if checked {
        report["credential_check"] = json!("accepted");
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::time::Duration;
    use tokio::time::{Instant, sleep};

    const URL: &str = "https://daemon.example.com";
    const TOKEN: &str = "fixture-installer-bearer";
    const BUDGET: Duration = Duration::from_secs(180);

    struct Home(PathBuf);

    impl Home {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("codex-credential-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&path).unwrap();
            std::fs::write(path.join("config.toml"), "# fixture\n").unwrap();
            Self(resolved_path(&path).unwrap())
        }

        fn assert_untouched(&self) {
            assert!(!self.0.join("pseudolife").exists());
            assert_eq!(
                std::fs::read_to_string(self.0.join("config.toml")).unwrap(),
                "# fixture\n"
            );
        }

        async fn configure(
            &self,
            mut probe: impl AsyncFnMut(&str, &str) -> Option<StatusCode>,
        ) -> Result<Value, SetupError> {
            let mut rpc = ReadOnlyCodex(json!({
                "config": {},
                "layers": [{
                    "name": {"type": "user", "file": self.0.join("config.toml")},
                    "version": "fixture-version",
                    "config": {}
                }]
            }));
            configure_credential_file_with_check(
                &mut rpc,
                &self.0,
                &self.0,
                None,
                Some((URL, Some(TOKEN))),
                async |url, token| {
                    installer_check_result(url, async |_| probe(url, token).await).await
                },
            )
            .await
        }
    }

    impl Drop for Home {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    struct ReadOnlyCodex(Value);

    impl Rpc for ReadOnlyCodex {
        async fn rpc(&mut self, method: &str, _params: Value) -> Result<Value, SetupError> {
            assert_eq!(method, "config/read");
            Ok(self.0.clone())
        }
    }

    #[tokio::test(start_paused = true)]
    async fn installer_retries_after_a_timeout_and_writes_only_after_http_200() {
        let home = Home::new();
        let attempts = Cell::new(0);
        let started = Instant::now();
        let report = home
            .configure(async |url, token| {
                assert_eq!(url, URL);
                assert!(token == TOKEN);
                home.assert_untouched();
                attempts.set(attempts.get() + 1);
                if attempts.get() == 1 {
                    // A request that cannot complete until its attempt times out.
                    sleep(BUDGET * 2).await;
                    return None;
                }
                sleep(Duration::from_millis(3431)).await;
                home.assert_untouched();
                Some(StatusCode::OK)
            })
            .await
            .unwrap();
        assert_eq!(attempts.get(), 2);
        assert!(started.elapsed() < BUDGET);
        assert_eq!(report["credential_check"], "accepted");
        assert!(
            std::fs::read_to_string(token_copy_path(&home.0))
                .unwrap()
                .trim()
                == TOKEN
        );
        assert_eq!(
            read_connection(&connection_path(&home.0))
                .unwrap()
                .unwrap()
                .0,
            URL
        );
        assert!(!report.to_string().contains(TOKEN));
    }

    #[tokio::test(start_paused = true)]
    async fn installer_refuses_http_401_or_403_immediately_without_writes() {
        for status in [StatusCode::UNAUTHORIZED, StatusCode::FORBIDDEN] {
            let home = Home::new();
            let attempts = Cell::new(0);
            let started = Instant::now();
            let error = home
                .configure(async |_, _| {
                    attempts.set(attempts.get() + 1);
                    Some(status)
                })
                .await
                .unwrap_err();
            assert_eq!(attempts.get(), 1);
            assert_eq!(started.elapsed(), Duration::ZERO);
            home.assert_untouched();
            assert_eq!(
                error.to_string(),
                "The installer credential was not accepted by the configured daemon."
            );
            assert!(!format!("{error:?}").contains(TOKEN));
            assert_eq!(
                error.credential_check,
                Some(CredentialCheckFailure::Refused)
            );
        }
    }

    #[tokio::test(start_paused = true)]
    async fn installer_stops_at_the_budget_when_the_daemon_never_becomes_ready() {
        let home = Home::new();
        let attempts = Cell::new(0);
        let started = Instant::now();
        let error = home
            .configure(async |_, _| {
                home.assert_untouched();
                attempts.set(attempts.get() + 1);
                match attempts.get() {
                    1 => Some(StatusCode::SERVICE_UNAVAILABLE),
                    2 => None,
                    _ => {
                        sleep(BUDGET * 2).await;
                        None
                    }
                }
            })
            .await
            .unwrap_err();
        assert_eq!(started.elapsed(), BUDGET);
        assert!(attempts.get() > 2);
        home.assert_untouched();
        assert!(
            error
                .to_string()
                .contains("did not answer the authenticated check in 180 seconds")
        );
        assert!(error.to_string().contains("rerun the installer"));
        assert!(!format!("{error:?}").contains(TOKEN));
        assert_eq!(
            error.credential_check,
            Some(CredentialCheckFailure::NotReady)
        );
    }
}
