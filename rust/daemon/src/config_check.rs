//! `pseudolife-daemon check-config`: does this bank's configuration run on
//! the native daemon? An updater asks the target image before it replaces a
//! Python daemon, so an unsupported setting keeps Python instead of failing
//! the new daemon's startup. It opens no bank and needs no network.

use crate::{config, embed, onnx_artifacts};
use anyhow::{Result, anyhow};
use std::ffi::OsString;
use std::path::PathBuf;

/// The configuration is valid but the native daemon refuses it. Distinct
/// from 2, which an image without this command returns when it falls
/// through to startup and refuses the missing database URL, and from 1, a
/// file it could not read (no verdict on the configuration).
pub const UNSUPPORTED: i32 = 3;

/// The configuration refusals startup makes before it serves. A new one
/// belongs here, so `check-config` reports it too.
pub fn startup_refusals(config: &config::Config) -> Result<()> {
    onnx_artifacts::validate_reranker_model(&config.memory.reranker.model_name)
}

/// Startup refuses this later, after its environment checks, so it keeps
/// its own place there; `check-config` reports it with the rest.
pub fn sweep_refusal(config: &config::Config) -> Option<&'static str> {
    ((config.dream.enabled || config.memory.retrieval_log_enabled)
        && !config.dream.sweep_interval_ok)
        .then_some("memory.dream.sweep_interval_seconds is not a number")
}

fn check(config_file: &std::path::Path) -> Result<()> {
    // An unreadable file is an I/O failure, not a refusal: `config::load`
    // would report it as one, so read it here first.
    if let Err(error) = std::fs::read(config_file)
        && !matches!(
            error.kind(),
            std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
        )
    {
        return Err(
            anyhow::Error::new(error).context(format!("cannot read {}", config_file.display()))
        );
    }
    let config = config::load(config_file).map_err(|e| anyhow!("{e}"))?;
    startup_refusals(&config)?;
    if let Some(refusal) = sweep_refusal(&config) {
        anyhow::bail!(refusal);
    }
    // Startup checks the embedding model when it builds the embedder.
    let override_root = std::env::var_os("PSEUDOLIFE_DAEMON_ONNX_DIR").map(PathBuf::from);
    embed::Embedder::check(&config.embedding, override_root.as_deref())?;
    Ok(())
}

pub fn command(args: &[OsString]) -> i32 {
    if !args.is_empty() {
        eprintln!("usage: pseudolife-daemon check-config (reads the configuration startup would)");
        return 2;
    }
    let env_lookup = |k: &str| std::env::var(k).ok();
    let cwd = match std::env::current_dir() {
        Ok(cwd) => cwd,
        Err(error) => {
            eprintln!("check-config: cwd: {error}");
            return 1;
        }
    };
    let (_, config_file) = config::config_path(&env_lookup, &cwd);
    match check(&config_file) {
        Ok(()) => {
            println!(
                "the native daemon supports the configuration in {}",
                config_file.display()
            );
            0
        }
        // A file that could not be read gives no verdict: only a refusal is 3.
        Err(error) if is_io(&error) => {
            eprintln!("check-config could not read what it checks: {error:#}");
            1
        }
        Err(error) => {
            eprintln!("{error:#}");
            UNSUPPORTED
        }
    }
}

fn is_io(error: &anyhow::Error) -> bool {
    error
        .chain()
        .any(|cause| cause.downcast_ref::<std::io::Error>().is_some())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_refusals_name_the_reranker_setting() {
        let mut config = config::Config::default();
        assert!(startup_refusals(&config).is_ok());
        config.memory.reranker.model_name = "BAAI/bge-reranker-base".into();
        let error = startup_refusals(&config).unwrap_err().to_string();
        assert!(error.contains("memory.reranker.model_name"), "{error}");
        assert!(error.contains("Python engine"), "{error}");
    }

    #[test]
    fn extra_arguments_are_a_usage_error_not_unsupported() {
        assert_eq!(command(&["--now".into()]), 2);
    }
}
