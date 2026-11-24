//! `installer codex-token-copy --daemon-url URL --token-file PATH`: whether a
//! client-only rerun may treat a `connect` plan that changes only Codex's rows
//! as current, because Codex's registration already names Codex's own copy of
//! PATH's token and those rows would change nothing else (issue #1908;
//! `setup::codex::token_copy::token_copy_state`). Reads Codex's configuration
//! through its app-server and writes nothing.
//!
//! Prints one JSON report: `state` ("same", "rotated", "not-copy",
//! "other-changes", "unregistered" or "unknown") and `detail`, never a token.
//! Exit 0 with a state, 1 when it could not tell ("unknown"), 2 for a usage
//! error. The installers rely on it only on exit 0 with "same".
use super::codex_config::{Neutral, codex_home, emit_report, runtime, usage};
use crate::setup::codex::{self, Client, Rpc, SetupError, token_copy};
use serde_json::{Value, json};
use std::{
    io::{self, Write},
    path::PathBuf,
};

pub(super) const TOKEN_COPY_USAGE: &str =
    "usage: pseudolife-mcp installer codex-token-copy --daemon-url URL --token-file PATH";

#[derive(Debug, PartialEq, Eq)]
struct Options {
    daemon_url: String,
    token_file: PathBuf,
}

fn parse(arguments: &[String]) -> Result<Option<Options>, String> {
    let (mut url, mut file) = (None, None);
    let mut items = arguments.iter();
    while let Some(item) = items.next() {
        match item.as_str() {
            "--help" | "-h" => return Ok(None),
            "--daemon-url" if url.is_none() => {
                url = Some(
                    items
                        .next()
                        .ok_or("--daemon-url needs a daemon URL")?
                        .clone(),
                );
            }
            "--token-file" if file.is_none() => {
                file = Some(PathBuf::from(
                    items.next().ok_or("--token-file needs a path")?,
                ));
            }
            other => return Err(format!("unknown or repeated option {other:?}")),
        }
    }
    match (url, file) {
        (Some(daemon_url), Some(token_file)) => Ok(Some(Options {
            daemon_url,
            token_file,
        })),
        _ => Err("codex-token-copy needs --daemon-url and --token-file".into()),
    }
}

pub(super) fn run(arguments: &[String]) -> u8 {
    let options = match parse(arguments) {
        Ok(Some(options)) => options,
        Ok(None) => {
            let _ = writeln!(io::stdout().lock(), "{TOKEN_COPY_USAGE}");
            return 0;
        }
        Err(message) => return usage(&message, TOKEN_COPY_USAGE),
    };
    let report = (|| -> Result<Value, SetupError> {
        let home = codex_home()?;
        // Opening the app-server would create a missing Codex home, and a home
        // without config.toml holds no registration.
        if !home.join("config.toml").is_file() {
            return Ok(json!({
                "state": token_copy::UNREGISTERED,
                "detail": "Codex has no user configuration yet",
            }));
        }
        let neutral = Neutral::new("pseudolife-codex-config-")?;
        let executable = codex::resolve_codex()?;
        let runtime = runtime().ok_or(SetupError("The setup runtime could not start."))?;
        runtime.block_on(async {
            let mut client = Client::open(&executable, &home, &neutral.0).await?;
            let result = state(&mut client, &home, &neutral.0, &options).await;
            client.close().await;
            result
        })
    })()
    .unwrap_or_else(|error| json!({"state": token_copy::UNKNOWN, "detail": error.to_string()}));
    let decided = report["state"] != token_copy::UNKNOWN;
    emit_report(&report, decided)
}

async fn state(
    client: &mut impl Rpc,
    home: &std::path::Path,
    cwd: &std::path::Path,
    options: &Options,
) -> Result<Value, SetupError> {
    let config = client
        .rpc("config/read", json!({"includeLayers": true, "cwd": cwd}))
        .await?;
    Ok(token_copy::token_copy_state(
        &config,
        home,
        &options.daemon_url,
        &options.token_file,
    ))
}

#[cfg(test)]
#[path = "codex_token_copy_tests.rs"]
mod tests;
