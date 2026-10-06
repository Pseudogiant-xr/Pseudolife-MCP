#![allow(dead_code)]
use pseudolife_stdio::credentials;
use pseudolife_stdio::lifecycle;
mod auth_fixture;
use auth_fixture::DisposableHome;
use serde_json::{Value, json};
use std::{fs, io, path::Path, time::Duration};

#[derive(Default)]
struct Control {
    now: Duration,
    probes: usize,
    spawn_count: usize,
    healthy_at_probe: Option<usize>,
    health: Value,
    probe_cost: bool,
    timeouts: Vec<Duration>,
    notes: Vec<String>,
    exit_code: Option<i32>,
    polled: Vec<Duration>,
}
impl lifecycle::DaemonControl for Control {
    type Child = ();
    fn now(&self) -> Duration {
        self.now
    }
    async fn sleep(&mut self, duration: Duration) {
        self.now += duration;
    }
    async fn probe(&mut self, _: &str, timeout: Duration) -> Option<Value> {
        self.probes += 1;
        self.timeouts.push(timeout);
        if self.probe_cost {
            self.now += timeout;
        }
        self.healthy_at_probe
            .filter(|after| self.probes >= *after)
            .map(|_| self.health.clone())
    }
    fn spawn(&mut self) -> io::Result<()> {
        self.spawn_count += 1;
        Ok(())
    }
    fn exited(&mut self, _: &mut ()) -> Option<i32> {
        self.polled.push(self.now);
        self.exit_code
    }
    fn note(&mut self, message: &str) {
        self.notes.push(message.into());
    }
}
fn options(home: &DisposableHome, no_spawn: bool) -> lifecycle::EnsureOptions {
    lifecycle::EnsureOptions::new("http://127.0.0.1:18765", no_spawn, &home.0)
}

#[tokio::test]
async fn test_shim_surfaces_a_boot_refusal_instead_of_spawning_over_it() {
    let home = DisposableHome::new();
    let mut control = Control {
        healthy_at_probe: Some(1),
        health: json!({"status":"degraded","init_refusal":"Refusing to serve: dimension mismatch"}),
        ..Default::default()
    };
    let error = lifecycle::ensure_daemon(
        "http://127.0.0.1:18765",
        &mut control,
        &options(&home, false),
    )
    .await
    .unwrap_err();
    assert_eq!(error.code, 1);
    assert_eq!(
        error.message,
        "[shim] the daemon at http://127.0.0.1:18765 is up but refusing to serve:\n  Refusing to serve: dimension mismatch"
    );
    assert_eq!(control.spawn_count, 0);
}
#[tokio::test]
async fn test_shim_attaches_to_a_degraded_daemon_and_says_so() {
    let home = DisposableHome::new();
    let mut control = Control {
        healthy_at_probe: Some(1),
        health: json!({"status":"degraded","db":"error: connection refused"}),
        ..Default::default()
    };
    let health = lifecycle::ensure_daemon(
        "http://127.0.0.1:18765",
        &mut control,
        &options(&home, false),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(health["status"], "degraded");
    assert_eq!(
        control.notes,
        [
            "[shim] note: the daemon at http://127.0.0.1:18765 reports status=degraded (db: error: connection refused)"
        ]
    );
    assert_eq!(control.spawn_count, 0);
}
#[tokio::test]
async fn test_no_spawn_env_waits_for_the_daemon_instead_of_spawning() {
    let home = DisposableHome::new();
    let mut control = Control {
        healthy_at_probe: Some(3),
        health: json!({"status":"ok"}),
        ..Default::default()
    };
    assert!(
        lifecycle::ensure_daemon(
            "http://127.0.0.1:18765",
            &mut control,
            &options(&home, true)
        )
        .await
        .unwrap()
        .is_some()
    );
    assert_eq!(control.spawn_count, 0);
    assert_eq!(control.probes, 3);
}
#[tokio::test]
async fn test_no_spawn_env_starts_without_the_daemon_and_names_the_docker_remedy() {
    let home = DisposableHome::new();
    let mut control = Control::default();
    assert!(
        lifecycle::ensure_daemon(
            "http://127.0.0.1:18765",
            &mut control,
            &options(&home, true)
        )
        .await
        .unwrap()
        .is_none()
    );
    let notes = control.notes.join("\n");
    assert!(
        notes.contains("PSEUDOLIFE_MCP_NO_SPAWN")
            && notes.contains("docker compose")
            && notes.contains("retries")
    );
    assert_eq!(control.now, Duration::from_secs(5));
    assert_eq!(control.spawn_count, 0);
}
#[test]
fn test_no_spawn_env_falsy_value_still_spawns() {
    for raw in [
        None,
        Some(""),
        Some("0"),
        Some("false"),
        Some("no"),
        Some("off"),
    ] {
        assert!(!lifecycle::spawn_disabled(raw));
    }
    for raw in [Some("1"), Some(" True "), Some("yes"), Some("ON")] {
        assert!(lifecycle::spawn_disabled(raw));
    }
}
#[tokio::test]
async fn test_non_loopback_daemon_url_never_spawns_a_local_daemon() {
    let home = DisposableHome::new();
    let mut control = Control {
        healthy_at_probe: Some(3),
        health: json!({"status":"ok"}),
        ..Default::default()
    };
    assert!(
        lifecycle::ensure_daemon("http://10.0.0.7:8765", &mut control, &options(&home, false))
            .await
            .unwrap()
            .is_some()
    );
    assert_eq!(control.spawn_count, 0);
    assert_eq!(control.timeouts, [Duration::from_secs(2); 3]);
}
#[tokio::test]
async fn test_remote_startup_wait_is_bounded_from_the_first_probe() {
    let home = DisposableHome::new();
    let mut control = Control {
        probe_cost: true,
        ..Default::default()
    };
    assert!(
        lifecycle::ensure_daemon("http://10.0.0.7:8765", &mut control, &options(&home, false))
            .await
            .unwrap()
            .is_none()
    );
    assert!(control.now <= Duration::from_secs(5));
    assert!(control.notes.join("\n").contains("tailnet up? LAN route?"));
    assert_eq!(control.spawn_count, 0);
}
#[tokio::test]
async fn test_spawn_floor_and_alive_ceiling_preserve_child_liveness_contract() {
    let home = DisposableHome::new();
    let mut control = Control {
        exit_code: Some(9),
        ..Default::default()
    };
    let error = lifecycle::ensure_daemon(
        "http://127.0.0.1:18765",
        &mut control,
        &options(&home, false),
    )
    .await
    .unwrap_err();
    assert_eq!(error.code, 1);
    assert_eq!(control.spawn_count, 1);
    assert_eq!(control.polled, [Duration::from_secs(25)]);
    assert_eq!(control.now, Duration::from_secs(25));
    let mut control = Control::default();
    assert!(
        lifecycle::ensure_daemon(
            "http://127.0.0.1:18765",
            &mut control,
            &options(&home, false)
        )
        .await
        .is_err()
    );
    assert_eq!(control.now, Duration::from_secs(180));
}
#[tokio::test]
async fn test_spawn_lock_is_released_after_the_daemon_is_up() {
    let home = DisposableHome::new();
    let opts = options(&home, false);
    let mut control = Control {
        healthy_at_probe: Some(3),
        health: json!({"status":"ok"}),
        ..Default::default()
    };
    assert!(
        lifecycle::ensure_daemon("http://127.0.0.1:18765", &mut control, &opts)
            .await
            .unwrap()
            .is_some()
    );
    assert_eq!(control.spawn_count, 1);
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&opts.lock_path)
        .unwrap();
    lock.try_lock().unwrap();
    lock.unlock().unwrap();
}
#[tokio::test]
async fn test_concurrent_shims_spawn_exactly_one_daemon() {
    use std::sync::{
        Arc, Barrier, Mutex,
        atomic::{AtomicUsize, Ordering},
    };
    struct Shared {
        started: Mutex<Option<std::time::Instant>>,
        spawns: AtomicUsize,
    }
    struct Concurrent {
        shared: Arc<Shared>,
        epoch: std::time::Instant,
    }
    impl lifecycle::DaemonControl for Concurrent {
        type Child = ();
        fn now(&self) -> Duration {
            self.epoch.elapsed()
        }
        async fn sleep(&mut self, _: Duration) {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        async fn probe(&mut self, _: &str, _: Duration) -> Option<Value> {
            self.shared
                .started
                .lock()
                .unwrap()
                .filter(|started| started.elapsed() >= Duration::from_millis(25))
                .map(|_| json!({"status":"ok"}))
        }
        fn spawn(&mut self) -> io::Result<()> {
            self.shared.spawns.fetch_add(1, Ordering::SeqCst);
            *self.shared.started.lock().unwrap() = Some(std::time::Instant::now());
            Ok(())
        }
        fn exited(&mut self, _: &mut ()) -> Option<i32> {
            None
        }
        fn note(&mut self, _: &str) {}
    }
    let home = DisposableHome::new();
    let opts = options(&home, false);
    let shared = Arc::new(Shared {
        started: Mutex::new(None),
        spawns: AtomicUsize::new(0),
    });
    let barrier = Arc::new(Barrier::new(2));
    let workers: Vec<_> = (0..2)
        .map(|_| {
            let shared = shared.clone();
            let barrier = barrier.clone();
            let opts = opts.clone();
            std::thread::spawn(move || {
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .enable_time()
                    .build()
                    .unwrap();
                barrier.wait();
                runtime.block_on(async {
                    let mut control = Concurrent {
                        shared,
                        epoch: std::time::Instant::now(),
                    };
                    lifecycle::ensure_daemon("http://127.0.0.1:18765", &mut control, &opts)
                        .await
                        .unwrap()
                        .is_some()
                })
            })
        })
        .collect();
    for worker in workers {
        assert!(worker.join().unwrap());
    }
    assert_eq!(shared.spawns.load(Ordering::SeqCst), 1);
}
#[tokio::test]
async fn test_failed_spawn_releases_its_lock_and_open_failure_is_best_effort() {
    let home = DisposableHome::new();
    let mut opts = options(&home, false);
    opts.spawn_floor = Duration::ZERO;
    let mut control = Control {
        exit_code: Some(1),
        ..Default::default()
    };
    assert!(
        lifecycle::ensure_daemon("http://127.0.0.1:18765", &mut control, &opts)
            .await
            .is_err()
    );
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&opts.lock_path)
        .unwrap();
    lock.try_lock().unwrap();
    lock.unlock().unwrap();
    opts.lock_path = home.path("missing-parent/spawn.lock");
    let mut control = Control {
        healthy_at_probe: Some(2),
        health: json!({"status":"ok"}),
        ..Default::default()
    };
    assert!(
        lifecycle::ensure_daemon("http://127.0.0.1:18765", &mut control, &opts)
            .await
            .unwrap()
            .is_some()
    );
    assert_eq!(control.spawn_count, 1);
}

#[test]
fn test_spawn_daemon_never_allocates_a_console_window() {
    assert_eq!(lifecycle::WINDOWS_DETACHED_FLAGS, 0x08000000 | 0x00000200);
    assert_eq!(lifecycle::WINDOWS_DETACHED_FLAGS & 0x00000008, 0);
}

#[test]
fn test_operation_timeout_preserves_cold_start_budget_and_finite_override() {
    for raw in [
        None,
        Some(""),
        Some("bad"),
        Some("0"),
        Some("-1"),
        Some("nan"),
        Some("inf"),
        Some("1_.0"),
        Some("1e_2"),
        Some("²"),
    ] {
        assert_eq!(lifecycle::operation_timeout_seconds(raw), 180.0);
    }
    assert_eq!(lifecycle::operation_timeout_seconds(Some("0.75")), 0.75);
    assert_eq!(
        lifecycle::operation_timeout_seconds(Some(" １２_３.٤e-١ ")),
        12.34
    );
    assert_eq!(lifecycle::operation_timeout_seconds(Some("1e1_0")), 1e10);
    assert_eq!(lifecycle::connect_timeout_seconds(0.001), 0.01);
    assert_eq!(lifecycle::connect_timeout_seconds(0.75), 0.375);
    assert_eq!(lifecycle::connect_timeout_seconds(180.0), 5.0);
}

#[test]
fn test_startup_credential_check_is_silent_when_auth_is_off() {
    let home = DisposableHome::new();
    let provider = credentials::CredentialProvider::new(None, Some(home.path("missing"))).unwrap();
    lifecycle::require_credential_for_auth(
        "http://127.0.0.1:18765",
        &json!({"auth":false}),
        &provider,
    )
    .unwrap();
}
#[test]
fn test_startup_exits_when_the_daemon_needs_a_credential_and_none_is_set() {
    let provider = credentials::CredentialProvider::new(None, None).unwrap();
    let error = lifecycle::require_credential_for_auth(
        "http://127.0.0.1:18765",
        &json!({"auth":true}),
        &provider,
    )
    .unwrap_err();
    assert_eq!(error.code, 1);
    assert!(
        error
            .message
            .contains("every call would be rejected with 401")
            && error
                .message
                .contains("claude_desktop_config.json must carry the setting itself")
    );
}
#[test]
fn test_startup_credential_check_passes_with_a_token_or_a_usable_token_file() {
    let home = DisposableHome::new();
    for provider in [
        credentials::CredentialProvider::new(Some("fixture-token".into()), None).unwrap(),
        credentials::CredentialProvider::new(
            None,
            Some(home.private_token("token", b"fixture-token")),
        )
        .unwrap(),
    ] {
        lifecycle::require_credential_for_auth(
            "http://127.0.0.1:18765",
            &json!({"auth":true}),
            &provider,
        )
        .unwrap();
    }
}
#[test]
fn test_startup_exits_when_the_configured_token_file_is_unusable() {
    let home = DisposableHome::new();
    let path = home.path("missing");
    let provider = credentials::CredentialProvider::new(None, Some(path.clone())).unwrap();
    let error = lifecycle::require_credential_for_auth(
        "http://127.0.0.1:18765",
        &json!({"auth":true}),
        &provider,
    )
    .unwrap_err();
    assert_eq!(error.code, 1);
    assert!(
        error
            .message
            .contains("configured credential file is missing")
    );
    assert!(error.message.contains(&path.display().to_string()));
    assert!(
        lifecycle::credential_file_warning(&provider)
            .unwrap()
            .contains("every call will be refused")
    );
}

#[test]
fn test_the_shim_notice_names_its_own_launcher() {
    let home = DisposableHome::new();
    let launcher = home.path("launcher with spaces.exe");
    fs::write(&launcher, "fixture").unwrap();
    assert_eq!(
        lifecycle::launcher_command_for(&launcher, None),
        format!("\"{}\"", launcher.display())
    );
    assert_eq!(
        lifecycle::launcher_command_for(&launcher, Some(&launcher)),
        "pseudolife-mcp"
    );
    assert_eq!(
        lifecycle::launcher_command_for(Path::new("nonexistent-fixture-launcher"), None),
        "pseudolife-mcp"
    );
}

#[test]
fn test_session_headers_include_writer_and_session() {
    let session = lifecycle::SessionIdentity::new(Some("writer-7"), None);
    let provider =
        credentials::CredentialProvider::new(Some("fixture-token".into()), None).unwrap();
    let snapshot = provider.snapshot().unwrap();
    let headers = session.headers(&snapshot, None).unwrap();
    assert!(headers.get("Authorization").unwrap() == "Bearer fixture-token");
    assert_eq!(headers.get("X-PL-Writer").unwrap(), "writer-7");
    assert_eq!(headers.get("X-PL-Session").unwrap(), session.uid.as_str());
    assert!(headers.get("Authorization").unwrap().is_sensitive());
}

#[test]
fn test_claude_code_shim_joins_the_host_session_and_leaves_its_lifecycle_to_it() {
    let id = "0b9c5f3e-7a1d-4c2e-9f8a-2d4e6b8c0a1f";
    for writer in [None, Some("claude-code"), Some(" CLAUDE-CODE ")] {
        let session = lifecycle::SessionIdentity::new(writer, Some(id));
        assert_eq!(session.uid, id);
        assert!(!session.owns_episode());
    }
}

#[test]
fn test_shim_without_a_host_session_opens_no_root_and_closes_only_its_own() {
    let id = "0b9c5f3e-7a1d-4c2e-9f8a-2d4e6b8c0a1f";
    for (writer, host) in [
        (None, None),
        (None, Some("not-a-session")),
        (None, Some("0B9C5F3E-7A1D-4C2E-9F8A-2D4E6B8C0A1F")),
        (Some("codex"), Some(id)),
        (Some("gemini"), Some(id)),
        (Some("claude-desktop"), Some(id)),
    ] {
        let session = lifecycle::SessionIdentity::new(writer, host);
        assert_ne!(session.uid, id);
        assert_eq!(session.uid.len(), 32);
        assert!(
            session
                .uid
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        );
        assert!(session.owns_episode());
    }
}

#[test]
fn test_a_daemon_whose_health_answers_but_mcp_drops_does_not_flap_the_tool_list() {
    let mut link = lifecycle::RecoveryState::new(true);
    assert_eq!(link.next_delay().as_secs(), 1);
    assert!(link.health_answered());
    assert!(link.daemon_lost());
    assert_eq!(link.next_delay().as_secs(), 2);
    assert!(link.health_answered());
    assert!(link.daemon_lost());
    assert_eq!(link.next_delay().as_secs(), 5);
    link.request_succeeded();
    assert!(link.daemon_lost());
    assert_eq!(link.next_delay().as_secs(), 1);
}
