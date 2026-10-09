//! The Pseudolife memory daemon, ported contract-first. Startup order and
//! refusals follow `daemon.py:run_daemon` (spec section C8): the moved-bank
//! gate, storage resolution, config, tokens, the bind guard, then the
//! background warmup and the HTTP server. See `spec-w1a-foundation.md`.

mod auth;
mod bank;
mod config;
mod embed;
mod health;
mod http;
mod mutants;
#[cfg(feature = "principal-harness")]
mod principal_probe;
mod principals;
mod pyjson;
mod routes;
mod search;
mod service;
mod static_files;
mod storage;

use std::path::PathBuf;
use std::sync::Arc;

const LOOPBACK: [&str; 3] = ["127.0.0.1", "::1", "localhost"];

/// `daemon.moved_refusal`: presence of either marker refuses.
fn moved_refusal(data_dir: Option<&str>) -> Option<String> {
    let dir = PathBuf::from(data_dir?);
    let incoming = dir.join("move.json");
    if incoming.exists() {
        return Some(format!(
            "an unfinished pseudolife-mcp move is restoring into this bank ({}); refusing to start.",
            incoming.display()
        ));
    }
    let marker = dir.join("moved.json");
    marker.exists().then(|| {
        format!(
            "this bank moved to another host ({}); refusing to start. Point clients at the new daemon.",
            marker.display()
        )
    })
}

fn fail(code: i32, message: &str) -> ! {
    eprintln!("{message}");
    std::process::exit(code)
}

fn main() {
    #[cfg(feature = "principal-harness")]
    if std::env::var_os("PSEUDOLIFE_PRINCIPAL_HARNESS").is_some() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        if runtime.block_on(principal_probe::run()).is_err() {
            fail(1, "principal harness refused");
        }
        return;
    }
    let env_lookup = |k: &str| std::env::var(k).ok();
    if let Some(msg) = moved_refusal(
        std::env::var("PSEUDOLIFE_MCP_DATA_DIR")
            .ok()
            .filter(|s| !s.is_empty())
            .as_deref(),
    ) {
        fail(2, &format!("pseudolife-mcp serve: {msg}"));
    }
    // Storage resolution: an explicit DSN is the only mode this port has
    // (no file mode, no embedded lite Postgres: declared divergence).
    let Some(dsn) = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")
        .ok()
        .filter(|s| !s.is_empty())
    else {
        fail(
            2,
            "storage resolution refused: the Rust daemon requires PSEUDOLIFE_MCP_DATABASE_URL",
        );
    };
    let cwd = std::env::current_dir().unwrap_or_else(|e| fail(1, &format!("cwd: {e}")));
    let (data_dir, config_file) = config::config_path(&env_lookup, &cwd);
    if let Err(e) = std::fs::create_dir_all(&data_dir) {
        fail(1, &format!("data dir {}: {e}", data_dir.display()));
    }
    let config = config::load(&config_file).unwrap_or_else(|e| fail(1, &e.to_string()));
    // Harness hook: print the parsed configuration and exit, so the config
    // differential can compare it with Python's AppConfig (harness/run.py).
    if std::env::var_os("PSEUDOLIFE_DAEMON_DUMP_CONFIG").is_some() {
        println!("{}", pyjson::dumps(&config.dump()));
        std::process::exit(0);
    }
    let env = config::DaemonEnv::from_env(&env_lookup).unwrap_or_else(|e| fail(1, &e.to_string()));

    // A token that is not valid Unicode would read as unset and open the
    // bank; refuse instead (declared divergence: Python starts and answers
    // 500 to every bearer).
    for name in ["PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS"] {
        if std::env::var_os(name).is_some_and(|v| v.to_str().is_none()) {
            fail(
                2,
                &format!("{name} is not valid Unicode; refusing to start"),
            );
        }
    }
    let tokens = auth::EnvTokens::from_env();
    // `misconfigured_tokens_env`: the map is set and non-blank, yet nothing parsed.
    if env
        .tokens_raw
        .as_deref()
        .is_some_and(|r| !crate::storage::py_strip(r).is_empty())
        && tokens.map.is_empty()
    {
        if tokens.single.is_none() {
            fail(
                2,
                "PSEUDOLIFE_MCP_TOKENS is set but no entry parsed (want \"token:principal,...\") and \
                 PSEUDOLIFE_MCP_TOKEN is unset — refusing to start in open mode. Fix the map or unset it.",
            );
        }
        eprintln!(
            "PSEUDOLIFE_MCP_TOKENS is set but no entry parsed — continuing with the singular token only"
        );
    }
    let auth_configured = tokens.configured();
    if !LOOPBACK.contains(&env.host.as_str()) && !auth_configured {
        if env.trust_bind {
            eprintln!(
                "Binding {} without a token because PSEUDOLIFE_MCP_TRUST_BIND is set",
                env.host
            );
        } else {
            fail(
                2,
                &format!(
                    "Refusing to bind {} without PSEUDOLIFE_MCP_TOKEN (or a PSEUDOLIFE_MCP_TOKENS map)",
                    env.host
                ),
            );
        }
    }
    // The background threads' settings, parsed where Python parses them
    // (after the bind guard): a bad value is an uncaught ValueError, exit 1.
    let seconds = |name: &str, default: f64| -> f64 {
        match std::env::var(name) {
            Err(_) => default,
            Ok(raw) => crate::storage::py_strip(&raw)
                .replace('_', "")
                .parse::<f64>()
                .unwrap_or_else(|_| fail(1, &format!("{name}={raw:?} is not a number"))),
        }
    };
    seconds("PSEUDOLIFE_MCP_AUTOSAVE_SECONDS", 30.0);
    if (config.dream.enabled || config.memory.retrieval_log_enabled)
        && !config.dream.sweep_interval_ok
    {
        fail(1, "memory.dream.sweep_interval_seconds is not a number");
    }
    seconds("PSEUDOLIFE_SESSION_IDLE_SECONDS", 1800.0);
    let reap_every = seconds("PSEUDOLIFE_SESSION_REAP_SECONDS", 300.0);
    let static_dir = std::env::var_os("PSEUDOLIFE_DAEMON_STATIC_DIR").map(PathBuf::from);
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap_or_else(|e| fail(1, &format!("runtime: {e}")));
    let code = runtime.block_on(async move {
        // asyncio binds every interface for an empty host (IPv4 here: the
        // IPv6 wildcard is a declared divergence).
        let host = if env.host.is_empty() {
            "0.0.0.0".to_string()
        } else {
            env.host.clone()
        };
        let Ok(port) = u16::try_from(env.port) else {
            eprintln!("bind {host}:{}: port out of range", env.port);
            return 1;
        };
        let env_principals: Vec<String> = tokens.map.iter().map(|(_, p)| p.clone()).collect();
        let store = Arc::new(auth::PrincipalStore::new_with_shadowed(
            env_principals.clone(),
        ));
        principals::spawn_refresher(dsn.clone(), store.clone(), env_principals);
        let service = Arc::new(service::Service::new(
            config,
            env,
            data_dir,
            dsn,
            auth_configured,
        ));
        tokio::spawn(service.clone().warmup());
        tokio::spawn(service.clone().reaper(reap_every));
        let app = Arc::new(http::App::new(service, tokens, store, static_dir));
        let router = axum::Router::new().fallback(http::handle).with_state(app);
        let listener = match tokio::net::TcpListener::bind((host.as_str(), port)).await {
            Ok(l) => l,
            Err(e) => {
                eprintln!("bind {host}:{port}: {e}");
                return 1;
            }
        };
        eprintln!("daemon: listening on {host}:{port} (auth={auth_configured})");
        match axum::serve(listener, router).await {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("serve: {e}");
                1
            }
        }
    });
    std::process::exit(code);
}
