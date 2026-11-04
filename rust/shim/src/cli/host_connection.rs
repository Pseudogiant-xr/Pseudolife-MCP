//! Read the install-managed connection for shell commands inside Codex.
use crate::credentials::CredentialProvider;
use crate::setup::codex::{self, FILE_KEY, SetupError, TOKEN_KEY, URL_KEY};
use std::{ffi::OsString, path::PathBuf};

/// Only additions are retained; ambient credentials are never copied here.
#[derive(Debug, Default)]
pub(super) struct Environment(Vec<(String, String)>);

impl Environment {
    pub(super) fn additions(&self) -> &[(String, String)] {
        &self.0
    }

    pub(super) fn lookup(&self, key: &str) -> Option<OsString> {
        self.0
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| OsString::from(value))
            .or_else(|| std::env::var_os(key))
    }

    pub(super) fn daemon_url(&self) -> Result<String, &'static str> {
        crate::daemon_url::validate(
            &self
                .lookup(URL_KEY)
                .and_then(|value| value.into_string().ok())
                .unwrap_or_else(|| crate::daemon_url::DEFAULT_URL.to_owned()),
        )
    }

    pub(super) fn discover(mode: &str, arguments: &[String]) -> Result<Self, SetupError> {
        discover_from(mode, arguments, |key| std::env::var_os(key))
    }
}

fn discover_from(
    mode: &str,
    arguments: &[String],
    lookup: impl Fn(&str) -> Option<OsString>,
) -> Result<Environment, SetupError> {
    if !selected(mode, arguments) {
        return Ok(Environment::default());
    }
    from_lookup(lookup)
}

fn selected(mode: &str, arguments: &[String]) -> bool {
    let flags = || arguments.iter().take_while(|value| value.as_str() != "--");
    if flags().any(|value| matches!(value.as_str(), "--help" | "-h")) {
        return false;
    }
    match mode {
        "lease"
            if arguments.first().is_some_and(|action| {
                matches!(
                    action.as_str(),
                    "break" | "conductor" | "delegate" | "designate"
                )
            }) =>
        {
            false
        }
        "lease" => !flags().any(|value| value == "--no-board"),
        "doctor" => !flags().any(|value| value == "--disposable-proof"),
        _ => false,
    }
}

fn from_lookup(lookup: impl Fn(&str) -> Option<OsString>) -> Result<Environment, SetupError> {
    let truthy = |key| lookup(key).is_some_and(|value| !value.is_empty());
    if lookup(URL_KEY).is_some() || !(truthy("CODEX_SESSION_ID") || truthy("CODEX_THREAD_ID")) {
        return Ok(Environment::default());
    }
    let home = lookup("CODEX_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("~/.codex"));
    let home = crate::credentials::expand_user(&home)
        .map_err(|_| SetupError("The Codex configuration path is invalid."))?;
    let Some((url, token_file)) = codex::read_connection(&codex::connection_path(&home))? else {
        return Ok(Environment::default());
    };
    let url = codex::validated_daemon_url(&url)?;
    let mut additions = vec![(URL_KEY.to_owned(), url)];
    if lookup(FILE_KEY).is_some() || truthy(TOKEN_KEY) {
        const UNMATCHED: SetupError = SetupError(
            "The Codex command's credential could not be matched to its saved daemon. Set PSEUDOLIFE_MCP_DAEMON_URL to the intended daemon and retry.",
        );
        let ambient = CredentialProvider::from_lookup(&lookup)
            .and_then(|provider| provider.snapshot())
            .map_err(|_| UNMATCHED)?;
        let saved = if token_file.is_empty() {
            None
        } else {
            CredentialProvider::new(None, Some(PathBuf::from(&token_file)))
                .and_then(|provider| provider.snapshot())
                .map_err(|_| UNMATCHED)?
                .token()
                .map(str::to_owned)
        };
        if !ambient.token().zip(saved.as_deref()).is_some_and(|(a, b)| {
            crate::board::identity::constant_time_equal(a.as_bytes(), b.as_bytes())
        }) {
            return Err(SetupError(
                "The Codex command's credential differs from its saved daemon connection. Set PSEUDOLIFE_MCP_DAEMON_URL to the intended daemon and retry.",
            ));
        }
    } else if !token_file.is_empty() {
        additions.push((FILE_KEY.to_owned(), token_file));
    } else {
        additions.push((TOKEN_KEY.to_owned(), String::new()));
    }
    Ok(Environment(additions))
}

#[cfg(test)]
#[path = "host_connection_tests.rs"]
mod tests;
