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
const FIXTURE_IMAGE: &str = "PSEUDOLIFE_TEST_SUPERVISED_IMAGE";
const FIXTURE_ACK: &str = "PSEUDOLIFE_TEST_SUPERVISED_ACK";
const FIXTURE_VENDOR: &str = "PSEUDOLIFE_TEST_SUPERVISED_VENDOR";
const VENDOR_BYTES: &[u8] = b"installed vendor fixture";

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
        "supervisor" | "production-supervisor" => {
            let plain_fixture = !store::key_path(&path, "dot").is_file();
            let prepared = if plain_fixture {
                fixture_prepared(&path)
            } else {
                prepare(&path, "dot").unwrap()
            };
            let image = PathBuf::from(std::env::var_os(FIXTURE_IMAGE).unwrap());
            let vendor = std::env::var_os(FIXTURE_VENDOR).map(PathBuf::from);
            let output =
                launch_with_vendor(prepared, &image, STARTUP, vendor.as_deref(), |config| {
                    Command::from(command(&executable, "vendor", config))
                });
            assert!(
                output.is_ok()
                    || matches!(output, Err(Fail::Detail(ref message)) if message.contains("The tunnel is stopped")),
                "{output:?}"
            );
        }
        "bridge" => {
            let owner = state::create(&path.with_extension("lock")).unwrap().file;
            owner.try_lock().unwrap();
            let ack = PathBuf::from(std::env::var_os(FIXTURE_ACK).unwrap());
            let _ack = readiness::hold(&ack).unwrap();
            state::atomic_create(&path, b"bridge attached").unwrap();
            std::io::copy(&mut std::io::stdin().lock(), &mut std::io::sink()).unwrap();
        }
        "vendor" => eager_vendor(&path),
        "updater" | "invalid-updater" | "failing-updater" | "stopped-updater"
        | "exited-updater" => {
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
            if matches!(
                mode.as_str(),
                "failing-updater" | "stopped-updater" | "exited-updater"
            ) {
                let report: serde_json::Value = serde_json::from_str(&text).unwrap();
                assert_eq!(code, 2, "{text}");
                assert_eq!(report["state"], "failed", "{text}");
                assert_eq!(report["profiles"][0]["running"], false, "{text}");
                if mode == "stopped-updater" {
                    assert!(
                        text.contains("a stop was requested during refresh"),
                        "{text}"
                    );
                }
                if mode == "exited-updater" {
                    assert!(
                        text.contains("the previous tunnel exited during refresh preparation"),
                        "{text}"
                    );
                }
                return;
            }
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
    let stage = config.parent().unwrap();
    let root = stage.parent().unwrap();
    let poller = state::open_with_write(&root.join("fixture-poller.lock"), true).unwrap();
    if poller.file.try_lock().is_err() {
        state::atomic_write(&root.join("overlap"), b"more than one vendor poller", None).unwrap();
        panic!("overlapping vendor pollers");
    }
    for entry in fs::read_dir(root).unwrap().flatten().filter(|entry| {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        name.starts_with("bridge-attached-") && name.ends_with(".lock")
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
    let mode = || fs::read_to_string(root.join("scenario")).unwrap_or_default();
    let (mut bridge, candidate, mut rejected) = if expression.contains(" --selected-image ") {
        mock_bridge(root, stage, expression)
    } else {
        production_bridge(root, stage, expression, &mode())
    };
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
        if !candidate && mode() == "exit-old" {
            drop(bridge.stdin.take());
            assert!(bridge.wait().unwrap().success());
            return;
        }
        if !rejected && bridge.try_wait().unwrap().is_some() {
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
                let (_, attempt) = expression.rsplit_once(" --bridge-attempt ").unwrap();
                let settling = root.join(format!("settling-{attempt}"));
                wait_until(|| settling.is_file());
                eprintln!("controlplane client: unexpected status 401");
                drop(bridge.stdin.take());
                assert!(bridge.wait().unwrap().success());
                return;
            }
            if candidate && !rejected && mode() == "lose-bridge" && status == "200 OK" {
                let (_, attempt) = expression.rsplit_once(" --bridge-attempt ").unwrap();
                wait_until(|| root.join(format!("settling-{attempt}")).is_file());
                drop(bridge.stdin.take());
                assert!(bridge.wait().unwrap().success());
                rejected = true;
            }
        }
        thread::sleep(Duration::from_millis(10));
    }
    drop(bridge.stdin.take());
    let _ = bridge.wait();
}

fn mock_bridge(root: &Path, stage: &Path, expression: &str) -> (std::process::Child, bool, bool) {
    let (invocation, attempt) = expression.rsplit_once(" --bridge-attempt ").unwrap();
    let separator = format!("' --exact {FIXTURE} --nocapture --selected-image '");
    let (actual, selected) = invocation
        .strip_prefix('\'')
        .unwrap()
        .split_once(&separator)
        .unwrap();
    let unquote = |value: &str| PathBuf::from(value.replace("'\\''", "'"));
    let selected = unquote(selected.strip_suffix('\'').unwrap());
    let attached = root.join(format!("bridge-attached-{}", Uuid::new_v4()));
    let bridge = command(&unquote(actual), "bridge", &attached)
        .env(FIXTURE_ACK, readiness::lock_path(stage, attempt))
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    wait_until(|| {
        store::private_read(&attached, 32).is_ok_and(|bytes| bytes == b"bridge attached")
    });
    (bridge, selected.starts_with(root.join("runtimes")), false)
}

fn production_bridge(
    root: &Path,
    stage: &Path,
    expression: &str,
    mode: &str,
) -> (std::process::Child, bool, bool) {
    let (invocation, attempt) = expression.rsplit_once(" --bridge-attempt ").unwrap();
    let (exe, directory) = invocation
        .strip_prefix('\'')
        .unwrap()
        .split_once("' tunnel shim --profile dot --profile-dir '")
        .unwrap();
    let (directory, generation) = directory.split_once("' --launch-id ").unwrap();
    let exe = PathBuf::from(exe.replace("'\\''", "'"));
    let directory = PathBuf::from(directory.replace("'\\''", "'"));
    assert!(crate::runtime::same_path(stage, &directory));
    let candidate = exe.starts_with(root.join("runtimes"));
    let rejected = candidate && mode == "bridge-refused";
    let supplied = if rejected {
        Uuid::new_v4().simple().to_string()
    } else {
        attempt.to_owned()
    };
    let mut bridge = std::process::Command::new(&exe);
    bridge
        .args(["tunnel", "shim", "--profile", "dot", "--profile-dir"])
        .arg(directory)
        .args(["--launch-id", generation, "--bridge-attempt", &supplied])
        .env_clear()
        .envs(vendor::probe_environment(std::env::vars_os()))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut bridge = bridge.spawn().unwrap();
    if rejected {
        assert!(!bridge.wait().unwrap().success());
        // Simulate the pinned vendor returning 200 while its asynchronous
        // bridge-exit shutdown has yet to finish.
        state::atomic_create(&root.join(format!("bridge-refusal-{attempt}")), b"").unwrap();
    } else {
        let mut stdout = std::io::BufReader::new(bridge.stdout.take().unwrap());
        let input = bridge.stdin.as_mut().unwrap();
        let mut call = |value: serde_json::Value| {
            writeln!(input, "{value}").unwrap();
            input.flush().unwrap();
            let mut line = String::new();
            assert!(stdout.read_line(&mut line).unwrap() != 0);
            serde_json::from_str::<serde_json::Value>(&line).unwrap()
        };
        let reply = call(
            json!({"jsonrpc":"2.0","id":1,"method":"initialize", "params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"fixture","version":"1"}}}),
        );
        assert!(reply["result"].is_object() && reply["id"] == 1);
        writeln!(
            input,
            "{{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}}"
        )
        .unwrap();
        let mut line = String::new();
        writeln!(
            input,
            "{{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}}"
        )
        .unwrap();
        input.flush().unwrap();
        assert!(stdout.read_line(&mut line).unwrap() != 0);
        let reply: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert!(
            reply["id"] == 2
                && reply["result"]["tools"]
                    .as_array()
                    .is_some_and(|tools| tools.iter().any(|tool| tool["name"] == "fixture"))
        );
        // Leave stdout connected so the production bridge can forward.
        bridge.stdout = Some(stdout.into_inner());
        state::atomic_create(&root.join(format!("bridge-rpc-{attempt}")), b"").unwrap();
    }
    (bridge, candidate, rejected)
}

pub(in crate::cli::tunnel::runtime) async fn before_settling_readiness(
    root: &Path,
    directory: &Path,
    record: &LaunchRecord,
    child: &mut process::Child,
) {
    let mode = fs::read_to_string(root.join("scenario")).unwrap_or_default();
    if !record
        .refresh
        .as_ref()
        .is_some_and(|capability| capability.image.path.starts_with(root.join("runtimes")))
        || !matches!(mode.as_str(), "rollback" | "lose-bridge")
    {
        return;
    }
    let attempt = record.bridge_attempt.as_ref().unwrap();
    state::atomic_create(&root.join(format!("settling-{}", attempt.id)), b"").unwrap();
    tokio::time::timeout(Duration::from_secs(10), async {
        if mode == "rollback" {
            while child.exit_code().unwrap().is_none() {
                tokio::time::sleep(POLL).await;
            }
        } else {
            while attempt.held(directory) {
                tokio::time::sleep(POLL).await;
            }
            assert!(child.exit_code().unwrap().is_none());
        }
    })
    .await
    .expect("fixture should release its bridge after sending readiness");
}

pub(in crate::cli::tunnel::runtime) async fn after_failure_before_drop(root: &Path) {
    let hold = root.join("hold-terminal");
    if hold.is_file() {
        tokio::time::timeout(Duration::from_secs(10), async {
            while hold.is_file() {
                tokio::time::sleep(POLL).await;
            }
        })
        .await
        .expect("fixture must release the terminal record after checking the caller");
    }
}

pub(in crate::cli::tunnel::runtime) async fn before_candidate_start(root: &Path) {
    let hold = root.join("hold-candidate-spawn");
    if hold.is_file() {
        state::atomic_create(&root.join("candidate-stopped"), b"").unwrap();
        tokio::time::timeout(Duration::from_secs(10), async {
            while hold.is_file() {
                tokio::time::sleep(POLL).await;
            }
        })
        .await
        .expect("fixture must release the candidate spawn");
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
    if request.uri().path() == "/health" && request.method() == "GET" {
        let body = json!({"status":"ok", "auth_required":true, "version":crate::lifecycle::PACKAGE_VERSION});
        return Ok(Response::builder()
            .header("content-type", "application/json")
            .body(Full::new(Bytes::from(body.to_string())))
            .unwrap());
    }
    let auth = request
        .headers()
        .get("authorization")
        .is_some_and(|header| header == "Bearer fixture-daemon-token");
    assert!(
        auth && request.uri().path() == "/mcp",
        "fixture daemon received an unexpected route or missing authentication"
    );
    if request.method() == "DELETE" {
        return Ok(Response::new(Full::new(Bytes::new())));
    }
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

#[track_caller]
fn wait_until(mut done: impl FnMut() -> bool) {
    wait_until_for(Duration::from_secs(40), &mut done);
}

#[track_caller]
fn wait_until_for(duration: Duration, mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + duration;
    while !done() {
        assert!(
            Instant::now() < deadline,
            "supervised fixture did not reach its next state"
        );
        thread::sleep(Duration::from_millis(20));
    }
}

fn production_executable() -> Option<PathBuf> {
    let expected = std::env::var_os("GITHUB_ACTIONS").is_some()
        || std::env::var_os("W62_REQUIRE_PRODUCTION_BRIDGE").is_some();
    let executable = std::env::current_exe().unwrap();
    let binary = executable
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join(if cfg!(windows) {
            "pseudolife-stdio.exe"
        } else {
            "pseudolife-stdio"
        });
    if !binary.is_file() {
        assert!(
            !expected,
            "the production bridge test needs the built CLI binary beside the unit test artifacts"
        );
        eprintln!("production bridge fixture needs a built pseudolife-stdio binary");
        return None;
    }
    if fixture_key_bytes(b"fixture-runtime-key").is_none() {
        assert!(
            std::env::var_os("GITHUB_ACTIONS").is_none(),
            "hosted Windows must run the production bridge with a sealed key"
        );
        return None;
    }
    Some(binary)
}

fn install_candidate(root: &Path, source: Option<&Path>, sequence: u32) -> refresh::Image {
    let layout = root.join("runtimes");
    let dir = layout.join(format!("{sequence:06}"));
    state::private_dir(&layout).unwrap();
    state::private_dir(&dir).unwrap();
    let path = dir.join(crate::runtime::BINARY);
    let mut output = state::create(&path).unwrap().file;
    if let Some(source) = source {
        std::io::copy(&mut File::open(source).unwrap(), &mut output).unwrap();
    }
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
    vendor: Option<PathBuf>,
}

impl Scenario {
    fn start() -> Self {
        Self::start_with_bridge(None)
    }

    fn start_with_bridge(production: Option<&Path>) -> Self {
        Self::start_with_images(production, production)
    }

    fn start_with_images(production: Option<&Path>, candidate_source: Option<&Path>) -> Self {
        Self::start_configured(production, candidate_source, false)
    }

    fn start_configured(
        production: Option<&Path>,
        candidate_source: Option<&Path>,
        verify_vendor: bool,
    ) -> Self {
        let home = Home::new();
        let root = &home.0;
        let executable = std::env::current_exe().unwrap();
        // The Linux debug CLI can be hundreds of megabytes. Exercise the
        // same production command and digest checks with a disposable image
        // that omits debug symbols. CI can run several such witnesses at once.
        #[cfg(target_os = "linux")]
        let compact = production.map(|binary| compact_fixture(root, binary));
        #[cfg(target_os = "linux")]
        let production = compact.as_deref().or(production);
        #[cfg(target_os = "linux")]
        let candidate_source = candidate_source.and(production);
        let candidate = install_candidate(root, candidate_source, 1);
        let mock_image = root.join("original-image");
        if production.is_none() {
            state::atomic_create(&mock_image, b"original image fixture").unwrap();
        }
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
        let mode = if production.is_some() {
            "production-supervisor"
        } else {
            "supervisor"
        };
        let mut child = Self::child(&executable, root, mode);
        child.env(FIXTURE_IMAGE, production.unwrap_or(&mock_image));
        let vendor = verify_vendor.then(|| vendor::installed_fixture(root, VENDOR_BYTES));
        if let Some(vendor) = &vendor {
            child.env(FIXTURE_VENDOR, vendor);
        }
        let supervisor = child.spawn().unwrap();
        let mut scenario = Self {
            supervisor,
            home,
            candidate,
            daemon,
            sealed_key,
            vendor,
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
        wait_until(|| {
            read().is_some_and(|outcome| {
                assert!(
                    outcome.phase == phase || outcome.phase == refresh::Phase::Pending,
                    "expected {phase:?}, got {:?} ({:?})",
                    outcome.phase,
                    outcome.cause
                );
                outcome.phase == phase
            })
        });
        read().unwrap()
    }
}

#[cfg(target_os = "linux")]
fn compact_fixture(root: &Path, binary: &Path) -> PathBuf {
    let destination = root.join("original-bridge");
    match std::process::Command::new("/usr/bin/strip")
        .args(["--strip-debug", "-o"])
        .arg(&destination)
        .arg(binary)
        .status()
    {
        Ok(status) => assert!(
            status.success(),
            "could not prepare the compact production bridge fixture"
        ),
        Err(error)
            if error.kind() == std::io::ErrorKind::NotFound
                && std::env::var_os("GITHUB_ACTIONS").is_none() =>
        {
            return binary.to_owned();
        }
        Err(error) => panic!("could not prepare the compact production bridge fixture: {error}"),
    }
    destination
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
    let first_request = ticket.request();
    assert_eq!(
        scenario.outcome(&ticket, Phase::Refused).cause,
        Some(Cause::CandidateChanged)
    );
    drop(ticket);
    assert_eq!(scenario.record().target.unwrap().pid, old_pid);
    // Replace the first result so this checks consumed-ID history, not the
    // last result's replay guard or the first result read by outcome().
    let another = scenario.submit(&wrong);
    assert_eq!(
        scenario.outcome(&another, Phase::Refused).cause,
        Some(Cause::CandidateChanged)
    );
    drop(another);
    let replay =
        refresh::submit_request(scenario.root(), "dot", &scenario.record(), first_request).unwrap();
    assert_eq!(
        scenario.outcome(&replay, Phase::Refused).cause,
        Some(Cause::Reply(refresh::Refusal::Replay))
    );
    drop(replay);
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
    assert_eq!(
        rolled_back.cause,
        Some(Cause::CandidateStartup(
            refresh::CandidateFailure::VendorDiagnostics
        ))
    );
    assert!(
        restart::status(scenario.root(), "dot")
            .unwrap()
            .1
            .contains("dot.native-runtime.log")
    );
    assert!(scenario.root().join("dot.native-runtime.log").is_file());
    drop(ticket);
    let restored_pid = scenario.record().target.unwrap().pid;
    let again = scenario.submit(&scenario.candidate);
    assert_eq!(
        scenario.outcome(&again, Phase::Refused).cause,
        Some(Cause::CandidateAlreadyRolledBack(
            refresh::CandidateFailure::VendorDiagnostics
        ))
    );
    assert_eq!(scenario.record().target.unwrap().pid, restored_pid);
    let (state, detail) = restart::status(scenario.root(), "dot").unwrap();
    assert_eq!(state, "refused");
    assert!(detail.contains("tunnel stop --profile dot"));
    assert!(detail.contains("tunnel run --profile dot"));
    assert!(detail.contains("to reconnect"));
    assert!(!detail.contains("Install a corrected runtime"));
    drop(again);
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
fn stopping_during_preflight_reports_a_terminal_stopped_result() {
    before_switch_failure("stopped-updater", refresh::Cause::Stopped);
}

#[test]
fn old_child_exit_during_preflight_reports_failure_through_update() {
    before_switch_failure("exited-updater", refresh::Cause::OldChildExited);
}

fn before_switch_failure(mode: &str, cause: refresh::Cause) {
    let scenario = Scenario::start();
    if !scenario.sealed_key {
        return;
    }
    let hold = scenario.root().join("hold-terminal");
    state::atomic_create(&hold, b"").unwrap();
    scenario.daemon.mode.store(2, Ordering::SeqCst);
    let executable = std::env::current_exe().unwrap();
    thread::scope(|scope| {
        let caller = scope.spawn(|| {
            Scenario::child(&executable, scenario.root(), mode)
                .status()
                .unwrap()
        });
        wait_until(|| scenario.daemon.observed.load(Ordering::SeqCst) != 0);
        let stop = if cause == refresh::Cause::Stopped {
            Some(scope.spawn(|| super::stop(scenario.root(), "dot")))
        } else {
            scenario.mode("exit-old");
            None
        };
        assert!(caller.join().unwrap().success());
        let outcome = refresh::read_outcome(scenario.root(), "dot").unwrap();
        assert_eq!(outcome.phase, refresh::Phase::Failed);
        assert_eq!(outcome.cause, Some(cause));
        assert!(!scenario.record().ready);
        fs::remove_file(hold).unwrap();
        if let Some(stop) = stop {
            assert!(stop.join().unwrap().is_ok());
        }
    });
    assert!(!scenario.root().join("overlap").exists());
}

#[test]
fn new_launch_retires_pending_and_failed_outcomes_after_it_stops() {
    for phase in [refresh::Phase::Pending, refresh::Phase::Failed] {
        let mut scenario = Scenario::start();
        let root = &scenario.home.0;
        let old = scenario.record();
        let capability = old.refresh.as_ref().unwrap();
        let request = refresh::Request::for_launch(&old, &scenario.candidate).unwrap();
        let mut outcome = refresh::Outcome::new(&request, phase, None, None);
        if phase == refresh::Phase::Failed {
            outcome = refresh::Outcome::new(&request, phase, None, Some(refresh::Cause::Stopped))
                .bind(&old);
        }
        refresh::write_outcome(root, "dot", &outcome).unwrap();
        assert!(stop(root, "dot").is_ok());
        assert!(scenario.supervisor.wait().unwrap().success());
        assert_eq!(restart::status(root, "dot").unwrap().0, "failed");

        scenario.supervisor =
            Scenario::child(&std::env::current_exe().unwrap(), root, "supervisor")
                .env(FIXTURE_IMAGE, &capability.image.path)
                .spawn()
                .unwrap();
        let new = wait_for_ready(&record::path(root, "dot"), || {
            scenario.supervisor.try_wait().unwrap().is_some()
        })
        .unwrap();
        assert_ne!(old.native_launch, new.native_launch);
        assert!(stop(root, "dot").is_ok());
        assert!(scenario.supervisor.wait().unwrap().success());
        assert!(
            restart::status(root, "dot").is_none(),
            "old {phase:?} leaked"
        );
    }
}

#[test]
fn healed_claim_failure_clears_its_issue_and_can_recur() {
    let scenario = Scenario::start();
    let record = scenario.record();
    let stage = stage_path(scenario.root(), launch_id(&record.native_launch).unwrap());
    let claim = stage.join(refresh::CLAIM);
    let issue = stage.join(refresh::ISSUE);
    for _ in 0..2 {
        state::private_dir(&claim).unwrap();
        wait_until(|| {
            store::private_read(&issue, 256).is_ok_and(|bytes| bytes == b"\"claim-blocked\"")
        });
        fs::remove_dir(&claim).unwrap();
        wait_until(|| !issue.exists());
    }
    let ticket = scenario.submit(&scenario.candidate);
    let expected = if scenario.sealed_key {
        refresh::Phase::Refreshed
    } else {
        refresh::Phase::Refused
    };
    assert_eq!(scenario.outcome(&ticket, expected).phase, expected);
}

#[test]
fn vendor_integrity_is_checked_in_preflight_and_again_before_spawn() {
    use refresh::{Cause, Phase};
    let scenario = Scenario::start_configured(None, None, true);
    if !scenario.sealed_key {
        return;
    }
    let vendor = scenario.vendor.as_ref().unwrap();
    let old = scenario.record();
    let old_pid = old.target.unwrap().pid;
    let before = scenario.daemon.observed.load(Ordering::SeqCst);
    setup::replace_private(vendor, b"tampered vendor fixture").unwrap();
    let ticket = scenario.submit(&scenario.candidate);
    assert_eq!(
        scenario.outcome(&ticket, Phase::Refused).cause,
        Some(Cause::VendorUnavailable)
    );
    assert_eq!(scenario.record().target.unwrap().pid, old_pid);
    assert!(scenario.daemon.observed.load(Ordering::SeqCst) > before);
    let poller =
        state::open_with_write(&scenario.root().join("fixture-poller.lock"), true).unwrap();
    assert!(matches!(
        poller.file.try_lock_shared(),
        Err(fs::TryLockError::WouldBlock)
    ));
    drop(ticket);

    setup::replace_private(vendor, VENDOR_BYTES).unwrap();
    let hold = scenario.root().join("hold-candidate-spawn");
    state::atomic_create(&hold, b"").unwrap();
    let ticket = scenario.submit(&scenario.candidate);
    wait_until(|| scenario.root().join("candidate-stopped").is_file());
    setup::replace_private(vendor, b"tampered after preflight").unwrap();
    fs::remove_file(hold).unwrap();
    assert_eq!(
        scenario.outcome(&ticket, Phase::Failed).cause,
        Some(Cause::RollbackStartup)
    );
    assert!(
        poller.file.try_lock_shared().is_ok(),
        "no replacement or rollback vendor should have started"
    );
}

#[test]
fn readiness_requires_the_acknowledgment_to_still_be_held_after_the_probe() {
    let scenario = Scenario::start();
    if !scenario.sealed_key {
        return;
    }
    let original = scenario.record().launch_identity;
    scenario.mode("lose-bridge");
    let ticket = scenario.submit(&scenario.candidate);
    scenario.outcome(&ticket, refresh::Phase::Pending);
    let result = scenario.outcome(&ticket, refresh::Phase::RolledBack);
    assert_eq!(result.active_identity, original);
    assert_eq!(
        result.cause,
        Some(refresh::Cause::CandidateStartup(
            refresh::CandidateFailure::ReadinessDeadline
        ))
    );
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
    let status = |args: &[&str]| {
        let args: Vec<_> = args.iter().copied().map(std::ffi::OsString::from).collect();
        let options = super::super::super::parse(&args).unwrap();
        super::super::super::status(&options, scenario.root())
            .unwrap()
            .0
    };
    let report: serde_json::Value = serde_json::from_str(&status(&["status", "--json"])).unwrap();
    assert_eq!(report["native_refresh"]["state"], "refreshed");
    assert_eq!(report["native_refresh"]["new_verification_required"], true);
    assert!(status(&["status"]).contains("The refreshed launch needs a new challenge"));
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
    let executable = std::env::current_exe().unwrap();
    assert!(
        Scenario::child(&executable, scenario.root(), "failing-updater")
            .status()
            .unwrap()
            .success()
    );
    let failure = refresh::read_outcome(scenario.root(), "dot").unwrap();
    assert_eq!(failure.phase, Phase::Failed);
    assert_eq!(failure.active_identity, None);
    assert_eq!(failure.cause, Some(Cause::RollbackStartup));
    wait_until(|| scenario.supervisor.try_wait().unwrap().is_some());
    let (state, detail) = restart::status(scenario.root(), "dot").unwrap();
    assert_eq!(state, "failed");
    assert!(
        detail.contains("The tunnel is stopped") && detail.contains("tunnel run --profile dot"),
        "{detail}"
    );
    assert!(!scenario.root().join("overlap").exists());
}

#[test]
fn production_bridge_serves_initialize_and_tools_list_after_refresh() {
    let Some(binary) = production_executable() else {
        return;
    };
    let scenario = Scenario::start_with_bridge(Some(&binary));
    let old = scenario.record().bridge_attempt.unwrap();
    assert!(
        scenario
            .root()
            .join(format!("bridge-rpc-{}", old.id))
            .is_file()
    );
    let ticket = scenario.submit(&scenario.candidate);
    scenario.outcome(&ticket, refresh::Phase::Refreshed);
    let record = scenario.record();
    let new = record.bridge_attempt.unwrap();
    let stage = stage_path(scenario.root(), launch_id(&record.native_launch).unwrap());
    assert_ne!(old.id, new.id);
    assert!(!old.held(&stage) && new.held(&stage));
    assert!(
        scenario
            .root()
            .join(format!("bridge-rpc-{}", new.id))
            .is_file()
    );
}

#[test]
fn vendor_ready_without_production_bridge_ack_rolls_back() {
    let Some(binary) = production_executable() else {
        return;
    };
    let scenario = Scenario::start_with_bridge(Some(&binary));
    let old = scenario.record();
    scenario.mode("bridge-refused");
    let ticket = scenario.submit(&scenario.candidate);
    // After acceptance this case includes the complete vendor startup
    // deadline. Hashing before acceptance does not count against it.
    scenario.outcome(&ticket, refresh::Phase::Pending);
    let terminal = || {
        refresh::read_outcome(scenario.root(), "dot").filter(|outcome| outcome.for_ticket(&ticket))
    };
    wait_until_for(Duration::from_secs(75), || {
        terminal().is_some_and(|outcome| outcome.phase == refresh::Phase::RolledBack)
    });
    let result = terminal().unwrap();
    assert_eq!(result.active_identity, old.launch_identity);
    assert!(
        fs::read_dir(scenario.root())
            .unwrap()
            .flatten()
            .any(|entry| entry
                .file_name()
                .to_string_lossy()
                .starts_with("bridge-refusal-"))
    );
    let rolled_back = scenario.record().bridge_attempt.unwrap();
    assert_ne!(rolled_back.id, old.bridge_attempt.unwrap().id);
    assert!(
        scenario
            .root()
            .join(format!("bridge-rpc-{}", rolled_back.id))
            .is_file()
    );
}

#[test]
fn an_incompatible_bridge_is_refused_before_interrupting_the_old_tunnel() {
    let Some(binary) = production_executable() else {
        return;
    };
    let scenario = Scenario::start_with_images(Some(&binary), None);
    let old = scenario.record();
    let before = scenario.daemon.observed.load(Ordering::SeqCst);
    let ticket = scenario.submit(&scenario.candidate);
    let outcome = scenario.outcome(&ticket, refresh::Phase::Refused);
    assert_eq!(outcome.cause, Some(refresh::Cause::BridgeProtocol));
    assert_eq!(
        scenario.record().target.unwrap().pid,
        old.target.unwrap().pid
    );
    assert_eq!(scenario.daemon.observed.load(Ordering::SeqCst), before);
}
