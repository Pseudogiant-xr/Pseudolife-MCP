//! The Pseudolife memory daemon, ported contract-first. Startup order and
//! refusals follow `daemon.py:run_daemon` (spec section C8): the moved-bank
//! gate, storage resolution, config, tokens, the bind guard, then the
//! background warmup and the HTTP server. See `spec-w1a-foundation.md`.

#![deny(unsafe_code)]

mod auth;
mod background;
mod background_sessions;
mod bank;
mod board;
mod briefing;
mod config;
mod consolidation_tools;
mod contradiction;
mod correction_tools;
mod counters;
mod documents;
mod dream;
mod embed;
mod embedding_math;
mod episode_tools;
#[cfg_attr(not(feature = "harness"), allow(dead_code))]
mod episodes;
mod fact_resolve;
mod fact_set;
mod forget_tools;
mod freshness;
mod graph_consolidation;
mod graph_insight;
mod graph_read;
mod graph_read_tools;
mod graph_review;
mod graph_review_tools;
mod graph_write_tools;
#[cfg(feature = "harness")]
mod harness_route;
mod health;
mod heap_trim;
mod hlc;
mod http;
mod junk_names;
mod labels;
mod lesson_tools;
mod lesson_write;
mod linger;
mod maintenance;
mod mcp;
mod memory_changes;
mod meta_filter;
mod mutants;
mod native;
mod onnx_artifacts;
mod onnx_runtime;
mod openblas_sdot;
mod outcome_tools;
#[cfg(feature = "principal-harness")]
mod principal_probe;
mod principals;
mod py_repr_unicode14;
mod pyjson;
mod read;
mod recall;
mod recall_mentions;
mod recall_unicode14;
mod release_check;
mod routes;
mod search;
mod service;
mod session_close;
mod session_hook;
mod session_register;
mod session_title;
mod set_members;
mod slots;
mod startup;
mod static_files;
mod storage;
#[cfg_attr(not(feature = "harness"), allow(dead_code))]
mod store;
mod stores;
mod txn;
mod world_tools;

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

/// The daemon's accepted connections, as `axum::serve` sees them: no Nagle
/// delay, and a lingering close (`linger.rs`).
fn serving_listener(
    listener: tokio::net::TcpListener,
) -> impl axum::serve::Listener<
    Io = linger::LingerStream<tokio::net::TcpStream>,
    Addr = std::net::SocketAddr,
> {
    linger::LingerListener::new(nodelay_listener(listener))
}

fn nodelay_listener(
    listener: tokio::net::TcpListener,
) -> impl axum::serve::Listener<Io = tokio::net::TcpStream, Addr = std::net::SocketAddr> {
    use axum::serve::ListenerExt;

    listener.tap_io(|stream| {
        if let Err(error) = stream.set_nodelay(true) {
            eprintln!("set TCP_NODELAY: {error}");
        }
    })
}

fn main() {
    // `provision <runtime>` installs a pinned native runtime and exits,
    // before anything that needs a bank or configuration.
    let args: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    if args.first().is_some_and(|a| a == "provision") {
        std::process::exit(native::command(&args[1..]));
    }
    #[cfg(feature = "principal-harness")]
    if std::env::var_os("PSEUDOLIFE_PRINCIPAL_HARNESS").is_some() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        if runtime.block_on(principal_probe::run()).is_err() {
            fail(1, "principal harness refused");
        }
        return;
    }
    if cfg!(feature = "mutants")
        && std::env::var_os("PSEUDOLIFE_DAEMON_HARNESS_CAPABILITIES").is_some()
    {
        println!("{}", mutants::supports_loopback_bind_fixture());
        return;
    }
    // The binding sweep (harness/mcp_binding_sweep.py) drives the MCP
    // argument binder directly: one {"tool", "args"} JSON object per line.
    #[cfg(any(feature = "harness", feature = "mutants"))]
    if std::env::var_os("PSEUDOLIFE_MCP_BIND_PROBE").is_some() {
        mcp::binding::probe();
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
        let env_principals: Vec<String> = tokens.map.iter().map(|(_, p)| p.clone()).collect();
        let store = Arc::new(auth::PrincipalStore::new_with_shadowed(
            env_principals.clone(),
        ));
        principals::spawn_refresher(dsn.clone(), store.clone(), env_principals);
        let service_dsn = dsn.clone();
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
        let board = Arc::new(board::Board::new(
            service.config.coordination.clone(),
            service_dsn,
            service.clone(),
            store.clone(),
        ));
        service.install(&board);
        let app = Arc::new(http::App::new(
            service.clone(),
            tokens,
            store,
            static_dir,
            board,
        ));
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
        // Graceful: stop accepting, wait for open requests. One budget is
        // taken at the signal (`background::HTTP_DRAIN` etc. are its marks):
        // a request still open at the first mark (a parked board wait, a
        // client that never reads) is no longer waited for. It keeps running
        // on its own connection task until exit, but a write it submits from
        // then on is refused, since the exit could cut it part way.
        let (signalled, mut on_signal) = tokio::sync::watch::channel(None);
        let serve = std::future::IntoFuture::into_future(
            axum::serve(serving_listener(listener), router).with_graceful_shutdown(async move {
                shutdown_signal().await;
                let _ = signalled.send(Some(tokio::time::Instant::now()));
            }),
        );
        let mut deadline_watch = on_signal.clone();
        let http_deadline = async move {
            let signalled_at = match deadline_watch.wait_for(Option::is_some).await {
                Ok(at) => at.expect("waited for Some"),
                Err(_) => std::future::pending().await,
            };
            tokio::time::sleep_until(signalled_at + crate::background::HTTP_DRAIN).await;
        };
        let code = tokio::select! {
            served = serve => match served {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("serve: {e}");
                    1
                }
            },
            () = http_deadline => {
                eprintln!(
                    "shutdown: open requests still running after {:.1}s; no longer waiting for them",
                    crate::background::HTTP_DRAIN.as_secs_f64()
                );
                0
            }
        };
        let start = (*on_signal.borrow_and_update()).unwrap_or_else(tokio::time::Instant::now);
        let writes_until = start + crate::background::HTTP_DRAIN + crate::background::WRITES_DRAIN;
        let join_until = writes_until + crate::background::JOIN_DEADLINE;
        let save_until = join_until + crate::background::SAVE_DEADLINE;
        // HTTP admission has stopped. Admitted writes finish before the
        // final save, until their mark; later ones are refused.
        service.writes.close_admission();
        if tokio::time::timeout_at(writes_until, service.writes.drain())
            .await
            .is_err()
        {
            eprintln!("shutdown: admitted writes still running; the final save runs beside them");
        }
        background
            .shutdown_until(service.as_ref(), join_until, save_until)
            .await;
        // Writes still running get the rest of the budget before the exit.
        if tokio::time::timeout_at(save_until, service.writes.wait())
            .await
            .is_err()
        {
            eprintln!(
                "shutdown: admitted writes still running at the end of the exit budget; leaving them to the exit"
            );
        }
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

#[cfg(test)]
mod listener_tests {
    use super::*;
    use axum::serve::Listener;

    #[tokio::test]
    async fn accepted_tcp_socket_disables_nagle() {
        let tcp = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = tcp.local_addr().unwrap();
        let mut listener = nodelay_listener(tcp);
        assert_eq!(listener.local_addr().unwrap(), address);
        let _client = tokio::net::TcpStream::connect(address).await.unwrap();
        let (accepted, _) = listener.accept().await;
        assert!(accepted.nodelay().unwrap());
    }
}

#[cfg(test)]
mod manifest_tests {
    /// `cargo run -p pseudolife-daemon` (the PDF tests' provisioning hint)
    /// needs `default-run`: cargo refuses a package with several binaries,
    /// feature-gated ones included, when none is named.
    #[test]
    fn cargo_run_picks_the_daemon() {
        let manifest = include_str!("../Cargo.toml");
        assert!(manifest.matches("[[bin]]").count() > 1);
        assert!(
            manifest
                .lines()
                .any(|line| line.trim() == r#"default-run = "pseudolife-daemon""#),
            "rust/daemon/Cargo.toml must set default-run = \"pseudolife-daemon\""
        );
    }
}
