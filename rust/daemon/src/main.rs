//! The Pseudolife memory daemon, ported contract-first. Startup order and
//! refusals follow `daemon.py:run_daemon` (spec section C8): the moved-bank
//! gate, storage resolution, config, tokens, the bind guard, then the
//! background warmup and the HTTP server. See `spec-w1a-foundation.md`.

mod auth;
mod background;
mod background_sessions;
mod bank;
mod config;
mod dream;
mod embed;
mod embedding_math;
mod graph_read;
mod health;
mod heap_trim;
mod http;
mod maintenance;
mod mutants;
mod onnx_artifacts;
mod onnx_runtime;
mod principals;
mod pyjson;
mod release_check;
mod routes;
mod search;
mod service;
mod startup;
mod static_files;
mod storage;
mod txn;

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
    if cfg!(feature = "mutants")
        && std::env::var_os("PSEUDOLIFE_DAEMON_HARNESS_CAPABILITIES").is_some()
    {
        println!("{}", mutants::supports_loopback_bind_fixture());
        return;
    }
    // The embedding harness exercises the same loader and encoder without a bank.
    if std::env::var_os("PSEUDOLIFE_DAEMON_EMBED_PROBE").is_some() {
        match embed::probe() {
            Ok(()) => return,
            Err(e) => fail(1, &e.to_string()),
        }
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
            Ok(raw) => crate::background::seconds(Some(&raw), default)
                .unwrap_or_else(|_| fail(1, &format!("{name}={raw:?} is not a number"))),
        }
    };
    let autosave_every = seconds("PSEUDOLIFE_MCP_AUTOSAVE_SECONDS", 30.0);
    if (config.dream.enabled || config.memory.retrieval_log_enabled)
        && !config.dream.sweep_interval_ok
    {
        fail(1, "memory.dream.sweep_interval_seconds is not a number");
    }
    let idle_seconds = seconds("PSEUDOLIFE_SESSION_IDLE_SECONDS", 1800.0);
    let reap_every = seconds("PSEUDOLIFE_SESSION_REAP_SECONDS", 300.0);
    let trim_every = heap_trim::interval(
        std::env::var("PSEUDOLIFE_MALLOC_TRIM_SECONDS")
            .ok()
            .as_deref(),
    )
    .unwrap_or_else(|e| fail(1, &e));
    let static_dir = std::env::var_os("PSEUDOLIFE_DAEMON_STATIC_DIR").map(PathBuf::from);
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap_or_else(|e| fail(1, &format!("runtime: {e}")));
    let code = runtime.block_on(async move {
        // asyncio binds every interface for an empty host (IPv4 here: the
        // IPv6 wildcard is a declared divergence).
        let host = if mutants::loopback_bind_fixture() {
            "127.0.0.1".to_string()
        } else if env.host.is_empty() {
            "0.0.0.0".to_string()
        } else {
            env.host.clone()
        };
        let Ok(port) = u16::try_from(env.port) else {
            eprintln!("bind {host}:{}: port out of range", env.port);
            return 1;
        };
        let store = Arc::new(auth::PrincipalStore::new());
        let env_principals = tokens.map.iter().map(|(_, p)| p.clone()).collect();
        principals::spawn_refresher(dsn.clone(), store.clone(), env_principals);
        let service = Arc::new(service::Service::new(
            config,
            env,
            data_dir,
            dsn,
            auth_configured,
        ));
        let background = background::Background::default();
        background.start_background_durability(service.clone(), autosave_every);
        background.start_session_reaper(service.clone(), reap_every, idle_seconds);
        background.start_dream_sweep(
            service.clone(),
            service.config.dream.enabled,
            service.config.memory.retrieval_log_enabled,
            service.config.dream.sweep_interval_seconds,
        );
        background.start_release_check(service.release_check.clone(), &service.config.updates);
        background.start_heap_trim(trim_every);
        let app = Arc::new(http::App::new(service.clone(), tokens, store, static_dir));
        let router = axum::Router::new().fallback(http::handle).with_state(app);
        let listener = match tokio::net::TcpListener::bind((host.as_str(), port)).await {
            Ok(l) => l,
            Err(e) => {
                eprintln!("bind {host}:{port}: {e}");
                background.shutdown(service.as_ref()).await;
                return 1;
            }
        };
        eprintln!("daemon: listening on {host}:{port} (auth={auth_configured})");
        let code = match axum::serve(listener, router)
            .with_graceful_shutdown(shutdown_signal())
            .await
        {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("serve: {e}");
                1
            }
        };
        background.shutdown(service.as_ref()).await;
        code
    });
    std::process::exit(code);
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("SIGTERM handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    }
    #[cfg(windows)]
    {
        let mut break_signal = tokio::signal::windows::ctrl_break().expect("CTRL_BREAK handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = break_signal.recv() => {} }
    }
}
