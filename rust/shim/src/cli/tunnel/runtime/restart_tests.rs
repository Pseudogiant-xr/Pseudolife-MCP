use super::*;
use std::sync::{
    Arc,
    atomic::{AtomicU8, AtomicUsize, Ordering},
};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response, body::Incoming, server::conn::http1, service::service_fn};
use hyper_util::rt::TokioIo;

pub(in crate::cli::tunnel::runtime) const FIXTURE_ENV: &str = "PSEUDOLIFE_TEST_SUPERVISED_FIXTURE";
pub(in crate::cli::tunnel::runtime) const FIXTURE: &str =
    "cli::tunnel::runtime::tests::restart_cases::supervised_fixture";
const FIXTURE_PATH: &str = "PSEUDOLIFE_TEST_SUPERVISED_FIXTURE_PATH";

fn command(executable: &Path, mode: &str, path: &Path) -> std::process::Command {
    let mut child = std::process::Command::new(executable);
    child
        .args(["--exact", FIXTURE, "--nocapture"])
        .env_clear()
        .envs(vendor::probe_environment(std::env::vars_os()))
        .env(FIXTURE_ENV, mode)
        .env(FIXTURE_PATH, path);
    child
}

#[test]
fn supervised_fixture() {
    let Ok(mode) = std::env::var(FIXTURE_ENV) else {
        return;
    };
    let path = PathBuf::from(std::env::var_os(FIXTURE_PATH).unwrap());
    let executable = std::env::current_exe().unwrap();
    match mode.as_str() {
        "supervisor" => {
            let plain_fixture = !store::key_path(&path, "dot").is_file();
            let prepared = if plain_fixture {
                fixture_prepared(&path)
            } else {
                prepare(&path, "dot").unwrap()
            };
            let output = launch(prepared, &executable, STARTUP, |config| {
                Command::from(command(&executable, "vendor", config))
            });
            assert!(
                output.is_ok()
                    || matches!(output, Err(Fail::Detail(ref message)) if message.contains("The tunnel is stopped")),
                "{output:?}"
            );
        }
        "bridge" => {
            let mut owner = state::create(&path).unwrap().file;
            owner.try_lock().unwrap();
            owner.write_all(b"bridge attached").unwrap();
            std::io::copy(&mut std::io::stdin().lock(), &mut std::io::sink()).unwrap();
        }
        "vendor" => eager_vendor(&path),
        "updater" | "invalid-updater" => {
            let candidate =
                crate::runtime::current_runtime(&crate::runtime::default_layout().unwrap())
                    .unwrap();
            let result =
                super::super::super::update_with_runtime(&path, Some(&candidate.executable()));
            if mode == "invalid-updater" {
                assert!(
                    matches!(result, Err(ref message) if message.contains("z.profile.json")),
                    "{result:?}"
                );
                return;
            }
            let (text, code) = result.unwrap();
            assert_eq!(code, 0, "{text}");
            assert_eq!(
                serde_json::from_str::<serde_json::Value>(&text).unwrap()["state"],
                "refreshed",
                "{text}"
            );
        }
        _ => panic!("unknown supervised fixture mode"),
    }
}

fn fixture_prepared(root: &Path) -> Prepared<'_> {
    let profile_bytes =
        store::private_read(&store::profile_path(root, "dot").unwrap(), RECORD_LIMIT).unwrap();
    let (_, profile) = store::decode_profile(&profile_bytes, "dot").unwrap();
    Prepared {
        root,
        profile,
        profile_bytes,
        key: key::Key::fixture(),
        previous_key: false,
        _locks: launch_locks(root, "dot").unwrap(),
    }
}

fn eager_vendor(config: &Path) {
    let root = config.parent().unwrap().parent().unwrap();
    let poller = state::open_with_write(&root.join("fixture-poller.lock"), true).unwrap();
    if poller.file.try_lock().is_err() {
        state::atomic_write(&root.join("overlap"), b"more than one vendor poller", None).unwrap();
        panic!("overlapping vendor pollers");
    }
    for entry in fs::read_dir(root).unwrap().flatten().filter(|entry| {
        entry
            .file_name()
            .to_string_lossy()
            .starts_with("bridge-attached-")
    }) {
        let previous_bridge = state::open_with_write(&entry.path(), true).unwrap();
        if previous_bridge.file.try_lock().is_err() {
            state::atomic_write(
                &root.join("overlap"),
                b"previous bridge still running",
                None,
            )
            .unwrap();
            panic!("a previous bridge was still alive when the next vendor started");
        }
    }
    let config: serde_json::Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
    let reference = config["control_plane"]["api_key"].as_str().unwrap();
    let mut key = Vec::new();
    File::open(reference.strip_prefix("file:").unwrap())
        .unwrap()
        .read_to_end(&mut key)
        .unwrap();
    assert!(key == b"fixture-runtime-key");
    let expression = config["mcp"]["commands"][0]["command"].as_str().unwrap();
    let suffix = format!("' --exact {FIXTURE} --nocapture");
    let path = PathBuf::from(
        expression
            .strip_prefix('\'')
            .unwrap()
            .strip_suffix(&suffix)
            .unwrap()
            .replace("'\\''", "'"),
    );
    let candidate = path.starts_with(root.join("runtimes"));
    let signal = |name: &str| root.join(name);
    let attached = signal(&format!("bridge-attached-{}", Uuid::new_v4()));
    let mut bridge = command(&path, "bridge", &attached)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    wait_until(|| {
        store::private_read(&attached, 32).is_ok_and(|bytes| bytes == b"bridge attached")
    });
    let mode = || fs::read_to_string(signal("scenario")).unwrap_or_default();
    if mode() == "both-fail" {
        // The eager fixture's vendor exits when its configured bridge ends.
        drop(bridge.stdin.take());
        assert!(bridge.wait().unwrap().success());
        return;
    }
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    fs::write(
        config["health"]["url_file"].as_str().unwrap(),
        format!("http://{}", listener.local_addr().unwrap()),
    )
    .unwrap();
    let deadline = Instant::now() + FIXTURE_WATCHDOG;
    while Instant::now() < deadline {
        if bridge.try_wait().unwrap().is_some() {
            return;
        }
        if let Ok((mut stream, _)) = listener.accept() {
            stream.set_nonblocking(false).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            let mut line = String::new();
            std::io::BufReader::new(&mut stream)
                .read_line(&mut line)
                .unwrap_or(0);
            let ready = !candidate || mode() != "warming";
            let status = if ready && line.starts_with("GET /readyz ") {
                "200 OK"
            } else {
                "503 Starting"
            };
            let _ = write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            );
            if candidate && mode() == "rollback" && status == "200 OK" {
                // Prove that a 200 racing the vendor's exit cannot settle.
                drop(stream);
                drop(bridge.stdin.take());
                assert!(bridge.wait().unwrap().success());
                return;
            }
        }
        thread::sleep(Duration::from_millis(10));
    }
    drop(bridge.stdin.take());
    let _ = bridge.wait();
}

pub(in crate::cli::tunnel::runtime) async fn before_settling_readiness(
    root: &Path,
    record: &LaunchRecord,
    child: &mut process::Child,
) {
    if record
        .refresh
        .as_ref()
        .is_some_and(|capability| capability.image.path.starts_with(root.join("runtimes")))
        && fs::read_to_string(root.join("scenario")).is_ok_and(|mode| mode == "rollback")
    {
        tokio::time::timeout(Duration::from_secs(10), async {
            while child.exit_code().unwrap().is_none() {
                tokio::time::sleep(POLL).await;
            }
        })
        .await
        .expect("fixture should exit after sending readiness");
    }
}

struct Daemon {
    url: String,
    mode: Arc<AtomicU8>,
    observed: Arc<AtomicUsize>,
    stop: Option<tokio::sync::oneshot::Sender<()>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Daemon {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let mode = Arc::new(AtomicU8::new(0));
        let observed = Arc::new(AtomicUsize::new(0));
        let (stop, mut ending) = tokio::sync::oneshot::channel();
        let (gate, count) = (mode.clone(), observed.clone());
        let thread = thread::spawn(move || {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap()
                .block_on(async move {
                    let listener = tokio::net::TcpListener::from_std(listener).unwrap();
                    loop {
                        let (stream, _) = tokio::select! {
                            _ = &mut ending => return,
                            socket = listener.accept() => socket.unwrap(),
                        };
                        let (gate, count) = (gate.clone(), count.clone());
                        tokio::spawn(async move {
                            let service = service_fn(move |request| {
                                daemon_reply(request, gate.clone(), count.clone())
                            });
                            let _ = http1::Builder::new()
                                .serve_connection(TokioIo::new(stream), service)
                                .await;
                        });
                    }
                })
        });
        Self {
            url,
            mode,
            observed,
            stop: Some(stop),
            thread: Some(thread),
        }
    }
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.stop.take().unwrap().send(());
        self.thread.take().unwrap().join().unwrap();
    }
}

async fn daemon_reply(
    request: Request<Incoming>,
    mode: Arc<AtomicU8>,
    observed: Arc<AtomicUsize>,
) -> Result<Response<Full<Bytes>>, std::convert::Infallible> {
    let auth = request
        .headers()
        .get("authorization")
        .is_some_and(|header| header == "Bearer fixture-daemon-token");
    assert!(auth && request.uri().path() == "/mcp");
    let bytes = request.into_body().collect().await.unwrap().to_bytes();
    let message: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_default();
    let reply = match message["method"].as_str() {
        Some("initialize") => {
            observed.fetch_add(1, Ordering::SeqCst);
            while mode.load(Ordering::SeqCst) == 2 {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            if mode.load(Ordering::SeqCst) == 1 {
                return Ok(Response::builder()
                    .status(401)
                    .body(Full::new(Bytes::new()))
                    .unwrap());
            }
            json!({"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"1"},"instructions":"fixture"})
        }
        Some("tools/list") => json!({"tools":[{"name":"fixture","inputSchema":{"type":"object"}}]}),
        _ => {
            return Ok(Response::builder()
                .status(202)
                .body(Full::new(Bytes::new()))
                .unwrap());
        }
    };
    let bytes = json!({"jsonrpc":"2.0", "id":message["id"], "result":reply}).to_string();
    Ok(Response::builder()
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(bytes)))
        .unwrap())
}

fn wait_until(mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(40);
    while !done() {
        assert!(
            Instant::now() < deadline,
            "supervised fixture did not reach its next state"
        );
        thread::sleep(Duration::from_millis(20));
    }
}

fn install_candidate(root: &Path, source: &Path, sequence: u32) -> refresh::Image {
    let layout = root.join("runtimes");
    let dir = layout.join(format!("{sequence:06}"));
    state::private_dir(&layout).unwrap();
    state::private_dir(&dir).unwrap();
    let path = dir.join(crate::runtime::BINARY);
    let mut output = state::create(&path).unwrap().file;
    std::io::copy(&mut File::open(source).unwrap(), &mut output).unwrap();
    output.write_all(&sequence.to_le_bytes()).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        output
            .set_permissions(fs::Permissions::from_mode(0o700))
            .unwrap();
    }
    drop(output);
    let digest = crate::runtime::hash_file(&path).unwrap();
    let version = "0.24.1".to_owned();
    let marker = crate::runtime::Marker {
        format: 1,
        kind: "native".into(),
        version: version.clone(),
        installed_at: chrono::Utc::now().to_rfc3339(),
        source: "fixture".into(),
        source_commit: None,
        target: crate::runtime::target_triple().unwrap().into(),
        binary: crate::runtime::BINARY.into(),
        binary_sha256: digest.clone(),
        archive_sha256: None,
    };
    state::atomic_create(
        &dir.join(crate::runtime::MARKER),
        &serde_json::to_vec(&marker).unwrap(),
    )
    .unwrap();
    refresh::Image {
        path,
        digest,
        version,
        vendor_version: vendor::VERSION.to_owned(),
    }
}

struct Scenario {
    supervisor: std::process::Child,
    home: Home,
    candidate: refresh::Image,
    daemon: Daemon,
    sealed_key: bool,
}

impl Scenario {
    fn start() -> Self {
        let home = Home::new();
        let root = &home.0;
        let executable = std::env::current_exe().unwrap();
        let candidate = install_candidate(root, &executable, 1);
        let daemon = Daemon::start();
        let token = root.join("daemon-token");
        state::atomic_create(&token, b"fixture-daemon-token").unwrap();
        let key = fixture_key_bytes(b"fixture-runtime-key");
        let sealed_key = key.is_some();
        #[cfg(windows)]
        assert!(
            sealed_key || std::env::var_os("GITHUB_ACTIONS").is_none(),
            "hosted Windows must exercise the sealed-key refresh paths"
        );
        if let Some(key) = key {
            state::atomic_create(&store::key_path(root, "dot"), &key).unwrap();
        }
        let profile = json!({"name":"dot", "daemon_url":daemon.url, "token_file":token,
            "state":"ready", "consent":true, "tunnel_id":"tunnel_abcdef"});
        state::atomic_create(
            &store::profile_path(root, "dot").unwrap(),
            &serde_json::to_vec(&profile).unwrap(),
        )
        .unwrap();
        state::atomic_create(&root.join("fixture-poller.lock"), b"").unwrap();
        state::atomic_create(&root.join("scenario"), b"ready").unwrap();
        let supervisor = Self::child(&executable, root, "supervisor")
            .spawn()
            .unwrap();
        let mut scenario = Self {
            supervisor,
            home,
            candidate,
            daemon,
            sealed_key,
        };
        assert!(
            wait_for_ready(&record::path(&scenario.home.0, "dot"), || scenario
                .supervisor
                .try_wait()
                .unwrap()
                .is_some())
            .is_some()
        );
        scenario
    }

    fn child(executable: &Path, root: &Path, mode: &str) -> std::process::Command {
        let mut fixture = command(executable, mode, root);
        let base = root.join("launcher");
        let launcher = if cfg!(windows) {
            base.with_extension("exe")
        } else {
            base
        };
        fixture
            .env("PSEUDOLIFE_SHIM_RUNTIMES", root.join("runtimes"))
            .env("PSEUDOLIFE_SHIM_LAUNCHER", launcher);
        fixture
    }

    fn root(&self) -> &Path {
        &self.home.0
    }
    fn record(&self) -> LaunchRecord {
        read_record(&record::path(self.root(), "dot")).unwrap()
    }
    fn mode(&self, name: &str) {
        setup::replace_private(&self.root().join("scenario"), name.as_bytes()).unwrap();
    }

    fn submit(&self, image: &refresh::Image) -> refresh::Ticket {
        refresh::submit(self.root(), "dot", &self.record(), image).unwrap()
    }

    fn outcome(&self, ticket: &refresh::Ticket, phase: refresh::Phase) -> refresh::Outcome {
        let read = || {
            refresh::read_outcome(self.root(), "dot").filter(|outcome| outcome.for_ticket(ticket))
        };
        wait_until(|| read().is_some_and(|outcome| outcome.phase == phase));
        read().unwrap()
    }
}

impl Drop for Scenario {
    fn drop(&mut self) {
        let root = &self.home.0;
        if self.supervisor.try_wait().ok().flatten().is_none() {
            if stop(root, "dot").is_err() && self.supervisor.try_wait().ok().flatten().is_none() {
                let _ = self.supervisor.kill();
            }
            let _ = self.supervisor.wait();
        }
    }
}

fn proof(root: &Path, identity: &str) -> Vec<u8> {
    let directory = root.join("dot-verification");
    state::private_dir(&directory).unwrap();
    let nonce = Uuid::new_v4().simple().to_string();
    let challenge = json!({"nonce":nonce,"identity":identity,"created":10.0})
        .to_string()
        .into_bytes();
    state::atomic_create(&directory.join("challenge.json"), &challenge).unwrap();
    for tool in ["memory_search", "memory_agents"] {
        let receipt =
            json!({"nonce":nonce,"identity":identity,"tool":tool,"success":true,"observed":11.0});
        state::atomic_create(
            &directory.join(format!("{nonce}-{tool}.json")),
            receipt.to_string().as_bytes(),
        )
        .unwrap();
    }
    challenge
}

#[test]
fn supervised_restart_fences_preflight_and_keeps_old_proof_on_rollback() {
    use refresh::{Cause, Phase};
    let scenario = Scenario::start();
    let old = scenario.record();
    let old_pid = old.target.as_ref().unwrap().pid;
    let mut wrong = scenario.candidate.clone();
    wrong.digest = "f".repeat(64);
    let ticket = scenario.submit(&wrong);
    assert_eq!(
        scenario.outcome(&ticket, Phase::Refused).cause,
        Some(Cause::CandidateChanged)
    );
    drop(ticket);
    assert_eq!(scenario.record().target.unwrap().pid, old_pid);
    if !scenario.sealed_key {
        let ticket = scenario.submit(&scenario.candidate);
        assert_eq!(
            scenario.outcome(&ticket, Phase::Refused).cause,
            Some(Cause::ProfileChanged)
        );
        assert_eq!(scenario.record().target.unwrap().pid, old_pid);
        return;
    }
    scenario.daemon.mode.store(1, Ordering::SeqCst);
    let ticket = scenario.submit(&scenario.candidate);
    assert_eq!(
        scenario.outcome(&ticket, Phase::Refused).cause,
        Some(Cause::DaemonHandshake)
    );
    drop(ticket);
    assert_eq!(scenario.record().target.unwrap().pid, old_pid);
    scenario.daemon.mode.store(0, Ordering::SeqCst);
    let fence = identity::verification_fence(scenario.root(), "dot").unwrap();
    let ticket = scenario.submit(&scenario.candidate);
    assert_eq!(
        scenario.outcome(&ticket, Phase::Refused).cause,
        Some(Cause::VerificationBusy)
    );
    drop(ticket);
    drop(fence);
    assert_eq!(scenario.record().target.unwrap().pid, old_pid);
    scenario.daemon.mode.store(2, Ordering::SeqCst);
    let before = scenario.daemon.observed.load(Ordering::SeqCst);
    let ticket = scenario.submit(&scenario.candidate);
    wait_until(|| scenario.daemon.observed.load(Ordering::SeqCst) > before);
    assert_eq!(scenario.record().target.unwrap().pid, old_pid);
    let poller =
        state::open_with_write(&scenario.root().join("fixture-poller.lock"), true).unwrap();
    assert!(matches!(
        poller.file.try_lock_shared(),
        Err(fs::TryLockError::WouldBlock)
    ));
    drop(ticket);
    scenario.daemon.mode.store(0, Ordering::SeqCst);
    let stage = stage_path(scenario.root(), launch_id(&old.native_launch).unwrap());
    wait_until(|| !stage.join(refresh::CLAIM).exists());
    assert_eq!(scenario.record().target.unwrap().pid, old_pid);

    let challenge = proof(scenario.root(), old.launch_identity.as_ref().unwrap());
    assert_eq!(
        super::super::super::bridge::cloud_status(scenario.root(), "dot").successful,
        2
    );
    scenario.mode("rollback");
    let ticket = scenario.submit(&scenario.candidate);
    let rolled_back = scenario.outcome(&ticket, Phase::RolledBack);
    assert_eq!(rolled_back.active_identity, old.launch_identity);
    assert_ne!(scenario.record().target.unwrap().pid, old_pid);
    assert!(
        restart::verify(scenario.root(), "dot")
            .unwrap()
            .0
            .contains("already verified")
    );
    assert_eq!(
        fs::read(scenario.root().join("dot-verification/challenge.json")).unwrap(),
        challenge
    );
    let parsed: serde_json::Value = serde_json::from_slice(&challenge).unwrap();
    let nonce = parsed["nonce"].as_str().unwrap();
    fs::remove_file(
        scenario
            .root()
            .join(format!("dot-verification/{nonce}-memory_agents.json")),
    )
    .unwrap();
    let text = restart::verify(scenario.root(), "dot").unwrap().0;
    assert!(text.contains("The saved challenge still applies") && text.contains(nonce));
    assert_eq!(
        fs::read(scenario.root().join("dot-verification/challenge.json")).unwrap(),
        challenge
    );
    assert!(!scenario.root().join("overlap").exists());
}

#[test]
fn accepted_restart_finishes_after_sender_leaves_and_requires_new_verification() {
    use refresh::Phase;
    let scenario = Scenario::start();
    if !scenario.sealed_key {
        return;
    }
    let old = scenario.record();
    proof(scenario.root(), old.launch_identity.as_ref().unwrap());
    let invalid = scenario.root().join("z.profile.json");
    state::atomic_create(&invalid, &vec![b'x'; RECORD_LIMIT + 1]).unwrap();
    let executable = std::env::current_exe().unwrap();
    assert!(
        Scenario::child(&executable, scenario.root(), "invalid-updater")
            .status()
            .unwrap()
            .success()
    );
    assert_eq!(
        scenario.record().target.unwrap().pid,
        old.target.as_ref().unwrap().pid
    );
    assert!(refresh::read_outcome(scenario.root(), "dot").is_none());
    fs::remove_file(invalid).unwrap();
    scenario.mode("warming");
    struct Caller(std::process::Child);
    impl Drop for Caller {
        fn drop(&mut self) {
            if self.0.try_wait().ok().flatten().is_none() {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }
    }
    let mut caller = Caller(
        Scenario::child(&executable, scenario.root(), "updater")
            .spawn()
            .unwrap(),
    );
    wait_until(|| {
        refresh::read_outcome(scenario.root(), "dot")
            .is_some_and(|outcome| outcome.phase == Phase::Pending)
    });
    caller.0.kill().unwrap();
    caller.0.wait().unwrap();
    scenario.mode("ready");
    wait_until(|| {
        refresh::read_outcome(scenario.root(), "dot")
            .is_some_and(|outcome| outcome.phase == Phase::Refreshed)
    });
    let new = scenario.record();
    assert_ne!(new.launch_identity, old.launch_identity);
    assert_eq!(new.refresh.unwrap().image, scenario.candidate);
    assert!(!super::super::super::bridge::cloud_status(scenario.root(), "dot").current);
    assert!(
        restart::verify(scenario.root(), "dot")
            .unwrap()
            .0
            .contains("needs a new challenge")
    );
    let challenge_path = scenario.root().join("dot-verification/challenge.json");
    let new_challenge = fs::read(&challenge_path).unwrap();
    assert!(
        restart::verify(scenario.root(), "dot")
            .unwrap()
            .0
            .contains("saved challenge still applies")
    );
    assert!(fs::read(challenge_path).unwrap() == new_challenge);
    assert!(!scenario.root().join("overlap").exists());
}

#[test]
fn unsuccessful_rollback_leaves_durable_stopped_outcome_with_run_command() {
    use refresh::{Cause, Phase};
    let mut scenario = Scenario::start();
    if !scenario.sealed_key {
        return;
    }
    scenario.mode("both-fail");
    let ticket = scenario.submit(&scenario.candidate);
    let failure = scenario.outcome(&ticket, Phase::Failed);
    assert_eq!(failure.active_identity, None);
    assert_eq!(failure.cause, Some(Cause::RollbackStartup));
    drop(ticket);
    wait_until(|| scenario.supervisor.try_wait().unwrap().is_some());
    let (state, detail) = restart::status(scenario.root(), "dot").unwrap();
    assert_eq!(state, "failed");
    assert!(
        detail.contains("The tunnel is stopped") && detail.contains("tunnel run --profile dot"),
        "{detail}"
    );
    assert!(!scenario.root().join("overlap").exists());
}
