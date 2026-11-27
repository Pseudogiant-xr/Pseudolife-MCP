//! `pseudolife-mcp installer engine detect|prepare|accept|choose` (formerly
//! `ops/installer_engine.py`): the Docker-tier daemon engine for the
//! installers, keeping the engine labels and the accepted-selection record in
//! the exact format `pseudolife-mcp update` verifies.
//!
//! - `detect` prints `none`, `python`, `rust` (an accepted release-mode Rust
//!   selection, also when its container was removed), `rust-unaccepted` (a
//!   Rust container whose selection was never accepted) or `rust-checkout`
//!   (one made by checkout-mode update).
//! - `prepare --repo R --engine python|rust|rust-checkout [--default]` prints
//!   the Compose overlay files the stack is brought up with, one per line.
//!   For Rust it writes the installer's daemon overlay: the accepted
//!   selection on a rerun, or a new activation of the checkout's release,
//!   whose image it pulls and whose pinned models it provisions into the
//!   project's model volume before any container starts.
//! - `accept` records the installer's Rust activation once the daemon is
//!   ready at its release with its counters imported; a rerun on an accepted
//!   host changes nothing.
//! - `choose --engine python` records an explicit Python choice.
//! - `foreign --repo R` exits 1 with `prepare`'s refusal when the container
//!   is another release's unfinished Rust daemon, and otherwise prints
//!   nothing; it pulls and writes nothing.
//!
//! Progress goes to stderr; stdout carries only the machine-readable answer.
use crate::cli::update::engine::{
    ACTIVATION_LABEL, CHECKOUT_LABEL, ENGINE_LABEL, PYTHON_ACCEPTED, RELEASE_LABEL, SELECTION,
    record_choice, release_accepted, selection,
};
use crate::cli::update::{
    DAEMON_CONTAINER, DAEMON_SERVICE, Options, PG_CONTAINER, Update, data_dir, truthy,
};
use serde_json::{Map, Value, json};
use std::{
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant, UNIX_EPOCH},
};

const OVERLAY: &str = "docker-compose.engine-installer.yml";
const PROJECT: &str = "pseudolife-mcp";
const MODELS_VOLUME: &str = "pseudolife_rust_models";
const MODELS_DIR: &str = "/models";
const DEFAULT_DAEMON_URL: &str = "http://127.0.0.1:8765";
const NO_DOCKER: &str =
    "Cannot reach Docker; start Docker and rerun the installer. Nothing was started.";
const BAD_OVERLAY: &str =
    "Cannot read the installer's engine overlay; rerun the installer. Nothing was started.";
const USAGE: &str = "usage: pseudolife-mcp installer engine {detect,prepare,accept,choose,foreign} [--repo REPO] [--engine {python,rust,rust-checkout}] [--default]";

/// A refusal; `unavailable` marks Rust unavailable for this checkout (no
/// release version, image or models), on which alone a defaulted engine
/// falls back to Python.
#[derive(Debug)]
struct Refusal {
    message: String,
    unavailable: bool,
}
fn refusal(message: impl Into<String>) -> Refusal {
    Refusal {
        message: message.into(),
        unavailable: false,
    }
}
fn unavailable(message: impl Into<String>) -> Refusal {
    Refusal {
        message: message.into(),
        unavailable: true,
    }
}
type Result<T> = std::result::Result<T, Refusal>;

/// `PSEUDOLIFE_DOCKER`, else `docker`, found as every installer step finds a
/// program (`state::program_path`): a name only in an absolute
/// `PATH` directory, a path as given.
fn docker_program() -> Option<std::path::PathBuf> {
    let named = std::env::var("PSEUDOLIFE_DOCKER")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "docker".into());
    super::state::program_path(&named)
}

struct Ran {
    code: i32,
    stdout: String,
    stderr: String,
}

/// One Docker call. Captured output is decoded with replacement and
/// universal newlines; uncaptured output is streamed to stderr, since stdout
/// carries only the answer. A call that cannot start or outlives `timeout`
/// is a refusal, as Python's `subprocess.run` raised there.
fn docker(args: &[&str], capture: bool, timeout: u64) -> Result<Ran> {
    let mut command = Command::new(docker_program().ok_or_else(|| refusal(NO_DOCKER))?);
    command.args(args).stdin(Stdio::null());
    if capture {
        command.stdout(Stdio::piped()).stderr(Stdio::piped());
    } else {
        command
            .stdout(Stdio::from(io::stderr()))
            .stderr(Stdio::inherit());
    }
    let mut child = command.spawn().map_err(|_| refusal(NO_DOCKER))?;
    let read = |pipe: Option<Box<dyn Read + Send>>| {
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            if let Some(mut pipe) = pipe {
                let _ = pipe.read_to_end(&mut bytes);
            }
            String::from_utf8_lossy(&bytes)
                .replace("\r\n", "\n")
                .replace('\r', "\n")
        })
    };
    let out = read(
        child
            .stdout
            .take()
            .map(|p| Box::new(p) as Box<dyn Read + Send>),
    );
    let err = read(
        child
            .stderr
            .take()
            .map(|p| Box::new(p) as Box<dyn Read + Send>),
    );
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() <= Duration::from_secs(timeout) => {
                std::thread::sleep(Duration::from_millis(20));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(refusal(NO_DOCKER));
            }
        }
    };
    Ok(Ran {
        code: status.code().unwrap_or(1),
        stdout: out.join().unwrap_or_default(),
        stderr: err.join().unwrap_or_default(),
    })
}

/// The daemon container's labels, or None when there is no container.
fn labels() -> Result<Option<Map<String, Value>>> {
    let ran = docker(
        &["inspect", "-f", "{{json .Config.Labels}}", DAEMON_CONTAINER],
        true,
        60,
    )?;
    if ran.code != 0 {
        if format!("{}{}", ran.stderr, ran.stdout)
            .to_lowercase()
            .contains("no such")
        {
            return Ok(None);
        }
        return Err(refusal(
            "Cannot inspect the existing daemon container; check docker info and rerun. Nothing was started.",
        ));
    }
    let text = ran.stdout.trim();
    let value: Value =
        serde_json::from_str(if text.is_empty() { "null" } else { text }).unwrap_or(Value::Null);
    Ok(Some(match value {
        Value::Object(map) => map,
        _ => Map::new(),
    }))
}

fn label<'a>(labels: &'a Map<String, Value>, key: &str) -> &'a Value {
    labels.get(key).unwrap_or(&Value::Null)
}

/// The release-accepted selection, `None` when missing or unreadable.
fn recorded() -> Option<Value> {
    selection(&release_accepted()).ok().flatten()
}

/// The accepted release selection matching the running container.
fn accepted(found: &Map<String, Value>) -> Option<Value> {
    if !truthy(label(found, RELEASE_LABEL)) {
        return None;
    }
    let selected = recorded()?;
    let theirs = &selected["labels"];
    (theirs[ENGINE_LABEL] == *label(found, ENGINE_LABEL)
        && truthy(label(found, ACTIVATION_LABEL))
        && theirs[ACTIVATION_LABEL] == *label(found, ACTIVATION_LABEL)
        && theirs[RELEASE_LABEL] == *label(found, RELEASE_LABEL))
    .then_some(selected)
}

/// The accepted release-mode Rust selection, read without a container.
fn accepted_release() -> Option<Value> {
    recorded().filter(|selected| {
        selected["labels"][ENGINE_LABEL] == "rust"
            && truthy(&selected["labels"][RELEASE_LABEL])
            && truthy(&selected["image"])
    })
}

fn mtime_ns(path: &Path) -> Option<i128> {
    let modified = fs::metadata(path).ok()?.modified().ok()?;
    Some(match modified.duration_since(UNIX_EPOCH) {
        Ok(after) => after.as_nanos() as i128,
        Err(before) => -(before.duration().as_nanos() as i128),
    })
}

fn detect(repo: Option<&Path>) -> Result<&'static str> {
    let Some(found) = labels()? else {
        // `docker compose down` removes the container, not the selection. A
        // checkout-mode selection made after it supersedes it.
        let release = accepted_release();
        // A release-mode switch back to Python leaves a python record: it counts too.
        let record = recorded();
        let mut newest = if record.is_some() {
            mtime_ns(&release_accepted()).unwrap_or(-1)
        } else {
            -1
        };
        let mut choice = if release.is_some() {
            "rust"
        } else if record.is_some() {
            "python"
        } else {
            "none"
        };
        for (name, engine) in [(PYTHON_ACCEPTED, "python"), (SELECTION, "rust-checkout")] {
            if let Some(path) = repo.map(|r| r.join(name))
                && path.is_file()
                && let Some(stamp) = mtime_ns(&path)
                && stamp > newest
            {
                (newest, choice) = (stamp, engine);
            }
        }
        return Ok(choice);
    };
    if *label(&found, ENGINE_LABEL) != "rust" {
        return Ok("python");
    }
    if truthy(label(&found, CHECKOUT_LABEL)) && !truthy(label(&found, RELEASE_LABEL)) {
        return Ok("rust-checkout");
    }
    Ok(if accepted(&found).is_some() {
        "rust"
    } else {
        "rust-unaccepted"
    })
}

fn checkout_version(repo: &Path) -> Result<String> {
    use crate::cli::update::{RELEASE_MANIFEST, plain_release, release_manifest_unreadable};
    // An unreadable release manifest is a refusal, not unavailability: it is
    // the checkout that is broken, so the default does not fall back. The
    // message keeps the cause and names the native fix.
    let version = crate::cli::update::checkout_release_version(repo).map_err(|error| {
        let why = release_manifest_unreadable(repo, &error);
        let mut text = why[..1].to_uppercase();
        text.push_str(&why[1..]);
        refusal(format!("{text}. Nothing was started."))
    })?;
    match version {
        Some(version) if plain_release(&version) => Ok(version),
        // A defaulted install falls back to Python on either alone, and the
        // fallback line keeps the text before the first ';' (cause()).
        Some(version) => Err(unavailable(format!(
            "Cannot read a release version from this checkout's {RELEASE_MANIFEST} (its [package] version is {version}, not a plain release); check out a release tag (git checkout v<version>) and rerun. Nothing was started."
        ))),
        // No plain version line at all: the file needs repair, which a
        // checkout of a release tag would not do (it refuses the local
        // change, or keeps it where the tag has the same file).
        None => Err(unavailable(format!(
            "Cannot read a release version from this checkout's {RELEASE_MANIFEST} (its [package] table names no plain version); {}. Nothing was started.",
            crate::cli::update::RELEASE_MANIFEST_NO_VERSION_FIX
        ))),
    }
}

fn overlay_path() -> PathBuf {
    data_dir().join("update-engine").join(OVERLAY)
}

fn quoted(text: &str) -> String {
    super::state::py_json_string(text)
}

/// A YAML daemon overlay: JSON strings are YAML scalars; `!reset` drops the
/// build. The models volume matches `docker-compose.rust-ghcr.yml`, which
/// later release updates apply, so both keep the models outside /data.
fn write_overlay(image: &str, labels: &Map<String, Value>) -> io::Result<PathBuf> {
    let mut lines = vec![
        "# Written by the installer from the accepted engine selection; rerunning rewrites it."
            .to_owned(),
        "services:".to_owned(),
        format!("  {DAEMON_SERVICE}:"),
        format!("    image: {}", quoted(image)),
        "    build: !reset null".to_owned(),
        "    environment:".to_owned(),
        format!("      PSEUDOLIFE_DAEMON_MODELS_DIR: {}", quoted(MODELS_DIR)),
        "    volumes:".to_owned(),
        format!(
            "      - {}",
            quoted(&format!("{MODELS_VOLUME}:{MODELS_DIR}"))
        ),
        "    labels:".to_owned(),
    ];
    let mut sorted: Vec<_> = labels.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(b.0));
    for (key, value) in sorted {
        let value = value
            .as_str()
            .map(quoted)
            .unwrap_or_else(|| value.to_string());
        lines.push(format!("      {}: {value}", quoted(key)));
    }
    lines.extend(["volumes:".to_owned(), format!("  {MODELS_VOLUME}: {{}}")]);
    let body = lines.join("\n") + "\n";
    let path = overlay_path();
    fs::create_dir_all(path.parent().expect("overlay directory"))?;
    // Python's read_text/write_text: compared as text (universal newlines),
    // written with the platform's line ending.
    let current = fs::read_to_string(&path)
        .ok()
        .map(|t| t.replace("\r\n", "\n"));
    if current.as_deref() != Some(body.as_str()) {
        let temporary = path.with_file_name(format!("{OVERLAY}.tmp"));
        let bytes = if cfg!(windows) {
            body.replace('\n', "\r\n")
        } else {
            body
        };
        fs::write(&temporary, bytes)?;
        fs::rename(&temporary, &path)?;
    }
    Ok(path)
}

/// The service the installer overlay names (image and labels only).
fn read_overlay() -> Option<Value> {
    let text = fs::read_to_string(overlay_path()).ok()?;
    let (mut image, mut found, mut section) = (None, Map::new(), String::new());
    for line in text.lines() {
        let (key, value) = line.trim().split_once(": ").unwrap_or((line.trim(), ""));
        if line.starts_with("    ") && !line.starts_with("      ") {
            section = line.trim().trim_end_matches(':').to_owned();
        }
        if let Some(rest) = line.strip_prefix("    image: ") {
            image = Some(serde_json::from_str::<Value>(rest).ok()?);
        } else if section == "labels" && line.starts_with("      ") && key.starts_with('"') {
            let key: String = serde_json::from_str(key).ok()?;
            found.insert(key, serde_json::from_str(value).ok()?);
        }
    }
    let mut service = json!({"labels": found});
    if let Some(image) = image {
        service["image"] = image;
    }
    Some(service)
}

/// The updater's 8 GiB floor, before a multi-GB pull and model download.
fn preflight_disk() -> Result<()> {
    crate::cli::update::disk::host(&data_dir())
        .map_err(|e| refusal(format!("{e} Nothing was started.")))?;
    let mut update = Update::new(Options::default());
    update
        .disk_storage(DAEMON_CONTAINER, Some(PG_CONTAINER), None)
        .map_err(|e| refusal(format!("{e} Nothing was started.")))
}

fn provision(image: &str) -> Result<()> {
    preflight_disk()?;
    let _ = writeln!(
        io::stderr().lock(),
        "Pulling the Rust daemon image {image}..."
    );
    // A pull that timed out: Rust is unavailable here, not Docker.
    let pulled = docker(&["pull", image], false, 7200).is_ok_and(|ran| ran.code == 0);
    if !pulled {
        return Err(unavailable(format!(
            "Cannot pull {image}: check the network and registry access, or check out a release that publishes its Rust daemon image, then rerun. Nothing was started."
        )));
    }
    let volume = format!("{PROJECT}_{MODELS_VOLUME}");
    if docker(&["volume", "inspect", &volume], true, 60)?.code != 0 {
        // Created the way Compose would, so `up` adopts it quietly.
        let project = format!("com.docker.compose.project={PROJECT}");
        let name = format!("com.docker.compose.volume={MODELS_VOLUME}");
        if docker(
            &[
                "volume", "create", "--label", &project, "--label", &name, &volume,
            ],
            true,
            60,
        )?
        .code
            != 0
        {
            return Err(refusal(format!(
                "Cannot create the model volume {volume}; check docker info and rerun. Nothing was started."
            )));
        }
    }
    let _ = writeln!(
        io::stderr().lock(),
        "Provisioning the Rust daemon's pinned models (the first install downloads them)..."
    );
    let models = format!("PSEUDOLIFE_DAEMON_MODELS_DIR={MODELS_DIR}");
    let mount = format!("{volume}:{MODELS_DIR}");
    let args = [
        "run",
        "--rm",
        "-e",
        &models,
        "-v",
        &mount,
        "--entrypoint",
        "pseudolife-daemon",
        image,
        "provision",
        "models",
        "--if-missing",
    ];
    if docker(&args, false, 3600)?.code != 0 {
        return Err(unavailable(
            "Cannot provision the Rust daemon's models; check network access and disk space, then rerun. Nothing was started.",
        ));
    }
    Ok(())
}

/// Only this checkout's own unfinished Rust install may be redone here:
/// anything else would replace a daemon without update's safeguards.
fn refuse_foreign_unaccepted(found: Option<&Map<String, Value>>, version: &str) -> Result<()> {
    if let Some(found) = found
        && *label(found, ENGINE_LABEL) == "rust"
        && accepted(found).is_none()
        && *label(found, RELEASE_LABEL) != version
    {
        let release = match label(found, RELEASE_LABEL) {
            Value::String(s) if !s.is_empty() => s.clone(),
            _ => "of unknown release".to_owned(),
        };
        return Err(refusal(format!(
            "The running Rust daemon {release} has no accepted selection, and this checkout installs {version}; the installer will not replace it. Remove the container (docker rm -f pseudolife-mcp-daemon; the bank volumes stay) and rerun. Nothing was started."
        )));
    }
    Ok(())
}

/// The refusal `prepare` gives another release's unfinished Rust container,
/// whichever engine is asked for, read without the ownership repair, a pull
/// or a write: stage 4b states it before a bearer refusal whose fixes would
/// only lead here.
fn foreign(repo: &Path) -> Result<()> {
    if let Some(found) = labels()?
        && *label(&found, ENGINE_LABEL) == "rust"
    {
        refuse_foreign_unaccepted(Some(&found), &checkout_version(repo)?)?;
    }
    Ok(())
}

/// The part of an unavailability refusal before its advice, for the
/// fallback line.
fn cause(message: &str) -> &str {
    let text = message.split(';').next().unwrap_or("");
    let text = text.split(". Rerun").next().unwrap_or("");
    text.split(". Nothing")
        .next()
        .unwrap_or("")
        .trim_end_matches('.')
}

fn activation() -> String {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).expect("operating system randomness");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn prepare(repo: &Path, engine: &str, default: bool) -> Result<Vec<PathBuf>> {
    let written = |r: io::Result<PathBuf>| r.map(|p| vec![p]).map_err(|_| refusal(BAD_OVERLAY));
    // Checkout mode manages a checkout's Rust daemon, and its update
    // (`update::engine::deploy`) applies the ownership order rule itself;
    // every other rerun applies it here first.
    if engine != "rust-checkout" {
        ownership()?;
    }
    if engine == "python" {
        foreign(repo)?;
        return Ok(vec![]);
    }
    if engine == "rust-checkout" {
        // A rerun would rebuild it from the checkout with none of update's
        // backup, rollback or health gate: checkout mode manages this daemon.
        // As Python's str(Path(...)) prints it: Windows separators there.
        let repo = if cfg!(windows) {
            repo.display().to_string().replace('/', "\\")
        } else {
            repo.display().to_string()
        };
        return Err(refusal(format!(
            "This host runs a Rust daemon selected by checkout-mode update, which the installer does not rebuild. Update it with pseudolife-mcp update --checkout {repo}, or switch it back with pseudolife-mcp update --engine python --checkout {repo} and rerun the installer. Nothing was started."
        )));
    }
    let found = labels()?;
    let kept = match &found {
        None => accepted_release(),
        Some(found) => accepted(found),
    };
    if let Some(kept) = kept {
        let image = kept["image"].as_str().unwrap_or_default().to_owned();
        let labels = kept["labels"].as_object().cloned().unwrap_or_default();
        return written(write_overlay(&image, &labels));
    }
    let attempt = (|| -> Result<(String, String)> {
        let version = checkout_version(repo)?;
        refuse_foreign_unaccepted(found.as_ref(), &version)?;
        let image = format!("{}:{version}", crate::cli::update::release::RUST_IMAGE);
        provision(&image)?;
        Ok((image, version))
    })();
    let (image, version) = match attempt {
        Ok(found) => found,
        Err(refused) if refused.unavailable && default => {
            // The default engine falls back to Python; an explicit --engine
            // rust refuses. Low disk or an unreachable Docker refuse either way.
            let _ = writeln!(
                io::stderr().lock(),
                "{}. The default Rust daemon is unavailable, so this install uses the Python daemon.",
                cause(&refused.message)
            );
            return Ok(vec![]);
        }
        Err(refused) => return Err(refused),
    };
    let mut labels = Map::new();
    labels.insert(ENGINE_LABEL.into(), json!("rust"));
    labels.insert(ACTIVATION_LABEL.into(), json!(activation()));
    labels.insert(RELEASE_LABEL.into(), json!(version));
    written(write_overlay(&image, &labels))
}

impl crate::cli::ownership::Docker for Installer {
    fn docker(&mut self, args: &[String]) -> (i32, String) {
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        match docker(&args, true, 3600) {
            // Docker's own text when the helper printed nothing, so "fix what
            // Docker reports" has a report (answers are hex, untouched by the
            // newline handling).
            Ok(ran) if ran.stdout.trim().is_empty() => (ran.code, ran.stderr),
            Ok(ran) => (ran.code, ran.stdout),
            Err(_) => (1, String::new()),
        }
    }
}

/// The ownership helpers' Docker: this module's own calls.
struct Installer;

/// A rerun over an existing daemon applies update's ownership order rule
/// before Compose recreates it (`cli::ownership`): probe; repair the model
/// cache when it needs it; and when the state volume needs it, back it up
/// first with the native backup (into the data directory's backups), then
/// repair it. A healthy host runs only the unprivileged probe; a first
/// install has no daemon to probe.
fn ownership() -> Result<()> {
    let ran = docker(&["inspect", DAEMON_CONTAINER], true, 60)?;
    if ran.code != 0 {
        if format!("{}{}", ran.stderr, ran.stdout)
            .to_lowercase()
            .contains("no such")
        {
            return Ok(());
        }
        return Err(refusal(
            "Cannot inspect the existing daemon container; check docker info and rerun. Nothing was started.",
        ));
    }
    let container = serde_json::from_str::<Value>(&ran.stdout)
        .ok()
        .and_then(|list| list.get(0).cloned())
        .ok_or_else(|| {
            refusal("Cannot inspect the existing daemon container; check docker info and rerun. Nothing was started.")
        })?;
    let directory = crate::cli::update::data_dir().join("ownership-repair");
    let stamp = crate::cli::update::seams::stamp();
    let request = crate::cli::restore_cli::docker_backup::Request {
        keep_days: 7.0,
        ..Default::default()
    };
    ownership_order(
        &mut Installer,
        &container,
        &directory,
        &stamp,
        || crate::cli::restore_cli::docker_backup::run_on_stderr(&request) == 0,
        &mut |line| {
            let _ = writeln!(io::stderr().lock(), "{line}");
        },
    )
}

/// [`ownership`]'s order over one daemon: `backup` takes the native backup
/// (true when it wrote a verified pair), `say` prints a line for the user.
fn ownership_order(
    docker: &mut impl crate::cli::ownership::Docker,
    container: &Value,
    directory: &Path,
    stamp: &str,
    backup: impl FnOnce() -> bool,
    say: &mut dyn FnMut(&str),
) -> Result<()> {
    use crate::cli::ownership;
    let checked = ownership::preflight(docker, container, directory, stamp, |_| Ok(()))
        .map_err(|message| refusal(format!("{message}. Nothing was started.")))?;
    let Some(checked) = checked else {
        return Ok(());
    };
    if let Some(models) = &checked.models {
        say(&models.line());
    }
    if checked.state_usable() {
        return Ok(());
    }
    say(&format!(
        "The daemon account cannot use {} in the state volume: backing it up, after which the backup repairs its ownership.",
        checked.state[0].shown()
    ));
    // The native backup takes its verified pair and only then repairs the
    // state (`restore_cli::docker_backup`): one repair, after one backup.
    if !backup() {
        return Err(refusal(
            "The backup above, or the ownership repair that follows it, did not complete; fix the cause it names and rerun. Nothing was started.",
        ));
    }
    let after = ownership::probe(docker, &checked.layout)
        .map_err(|message| refusal(format!("{message}. Nothing was started.")))?;
    if let Some(left) = after.of(&checked.layout, ownership::Part::State).first() {
        return Err(refusal(format!(
            "The daemon account still cannot use {} in the state volume after the backup's ownership repair (see above); fix the cause it names and rerun. Nothing was started.",
            left.shown()
        )));
    }
    Ok(())
}

fn image_schema(image: &str) -> Option<i64> {
    let args = [
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/usr/local/bin/pseudolife-mcp",
        image,
        "update",
        "--schema-version",
    ];
    let ran = docker(&args, true, 60).ok()?;
    let text = ran.stdout.trim();
    (ran.code == 0 && !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit()))
        .then(|| text.parse().ok())
        .flatten()
}

/// The bearer the running daemon was started with: its container
/// environment is what Compose resolved. Never printed.
fn bearer() -> Result<String> {
    let ran = docker(
        &["inspect", "-f", "{{json .Config.Env}}", DAEMON_CONTAINER],
        true,
        60,
    )?;
    let entries: Vec<Value> = if ran.code == 0 {
        serde_json::from_str(ran.stdout.trim()).unwrap_or_default()
    } else {
        vec![]
    };
    let value = |key: &str| {
        entries.iter().filter_map(Value::as_str).find_map(|item| {
            let (name, value) = item.split_once('=')?;
            (name == key).then(|| value.to_owned())
        })
    };
    if let Some(token) = value("PSEUDOLIFE_MCP_TOKEN").filter(|t| !t.is_empty()) {
        return Ok(token);
    }
    // The first principal `parse_token_map` accepts.
    let map = value("PSEUDOLIFE_MCP_TOKENS").unwrap_or_default();
    Ok(super::credentials::parse_token_map(&map)
        .into_iter()
        .next()
        .map(|(token, _)| token)
        .unwrap_or_default())
}

fn shown(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

fn accept(url: &str) -> Result<&'static str> {
    let found = labels()?.unwrap_or_default();
    if !overlay_path().is_file()
        || *label(&found, ENGINE_LABEL) != "rust"
        || !truthy(label(&found, RELEASE_LABEL))
    {
        return Ok("not-applicable");
    }
    if accepted(&found).is_some() {
        return Ok("unchanged");
    }
    let service = read_overlay().ok_or_else(|| refusal(BAD_OVERLAY))?;
    if service["labels"][ACTIVATION_LABEL] != *label(&found, ACTIVATION_LABEL) {
        return Err(refusal(
            "The running daemon is not the one this install started; rerun the installer. The Rust selection was not recorded.",
        ));
    }
    let unrecorded = " The Rust selection was not recorded.";
    let state = crate::cli::update::seams::fetch_health_json(
        &format!("{}/health", url.trim_end_matches('/')),
        3,
    )
    .unwrap_or_else(|| json!({}));
    let release = &service["labels"][RELEASE_LABEL];
    if state["status"] != "ok" || state["ready"] != Value::Bool(true) {
        return Err(refusal(format!(
            "The Rust daemon is not ready yet; check docker logs pseudolife-mcp-daemon and rerun.{unrecorded}"
        )));
    }
    if state["version"] != *release {
        return Err(refusal(format!(
            "The daemon reports version {}, not the {} this install started; check docker compose ps for the daemon's image and rerun.{unrecorded}",
            shown(&state["version"]),
            shown(release)
        )));
    }
    if truthy(&state["counters_import_required"]) {
        return Err(refusal(format!(
            "The Rust daemon reports counters still to import from memory_state/weights.pt, which only pseudolife-mcp update imports: remove this unfinished daemon (docker rm -f pseudolife-mcp-daemon; the bank's volumes stay) and rerun the installer, which brings the bank up as it is and moves it to Rust through that update.{unrecorded}"
        )));
    }
    let schema = image_schema(service["image"].as_str().unwrap_or_default());
    if schema.is_none() || state["schema"].as_f64() != schema.map(|s| s as f64) {
        return Err(refusal(format!(
            "The daemon reports schema {}, and the image's own schema could not be read or differs ({}); check docker logs pseudolife-mcp-daemon and rerun.{unrecorded}",
            shown(&state["schema"]),
            schema.map_or_else(|| "None".to_owned(), |s| s.to_string())
        )));
    }
    let token = bearer()?;
    if token.is_empty() || token.contains(['\r', '\n']) {
        // update verifies a Rust daemon with an authenticated read: without a
        // bearer the host could never be updated.
        return Err(refusal(format!(
            "The Rust daemon runs without a bearer token, so pseudolife-mcp update could not verify it; rerun the installer without --no-token (its token step names any failure).{unrecorded}"
        )));
    }
    if !crate::cli::update::seams::authenticated_read(url, &token) {
        return Err(refusal(format!(
            "The Rust daemon refused an authenticated bank read with its own bearer; check docker logs pseudolife-mcp-daemon and rerun.{unrecorded}"
        )));
    }
    let path = release_accepted();
    fs::create_dir_all(path.parent().expect("selection directory"))
        .map_err(|_| refusal(BAD_OVERLAY))?;
    crate::cli::update::engine::write_overlay(&path, &service)
        .map_err(|e| refusal(format!("{e}.{unrecorded}")))?;
    Ok("accepted")
}

struct Arguments {
    action: String,
    repo: Option<PathBuf>,
    engine: Option<String>,
    default: bool,
}

fn parse(args: &[String]) -> std::result::Result<Arguments, String> {
    let mut parsed = Arguments {
        action: String::new(),
        repo: None,
        engine: None,
        default: false,
    };
    let mut items = args.iter();
    while let Some(item) = items.next() {
        match item.as_str() {
            "--repo" => {
                parsed.repo = Some(PathBuf::from(items.next().ok_or("--repo needs a path")?))
            }
            "--engine" => {
                let engine = items.next().ok_or("--engine needs a value")?;
                if !["python", "rust", "rust-checkout"].contains(&engine.as_str()) {
                    return Err(format!("--engine: invalid choice '{engine}'"));
                }
                parsed.engine = Some(engine.clone());
            }
            "--default" => parsed.default = true,
            action
                if parsed.action.is_empty()
                    && ["detect", "prepare", "accept", "choose", "foreign"].contains(&action) =>
            {
                parsed.action = action.to_owned();
            }
            other => return Err(format!("unrecognized argument '{other}'")),
        }
    }
    if parsed.action.is_empty() {
        return Err("an action is required: detect, prepare, accept, choose or foreign".into());
    }
    Ok(parsed)
}

fn answer(lines: &[String]) -> u8 {
    let mut stdout = io::stdout().lock();
    u8::from(lines.iter().any(|line| writeln!(stdout, "{line}").is_err()))
}

/// Arguments follow `installer engine`. Exit 0 with the answer on stdout, 1
/// with a refusal on stderr, 2 for a usage error.
pub(crate) fn run(args: &[String]) -> u8 {
    if matches!(args, [flag] if flag == "--help" || flag == "-h") {
        return answer(&[USAGE.to_owned()]);
    }
    let arguments = match parse(args) {
        Ok(arguments) => arguments,
        Err(message) => {
            let _ = writeln!(
                io::stderr().lock(),
                "{USAGE}\ninstaller engine: error: {message}"
            );
            return 2;
        }
    };
    let result = match arguments.action.as_str() {
        "detect" => detect(arguments.repo.as_deref()).map(|choice| vec![choice.to_owned()]),
        "prepare" => match (&arguments.repo, &arguments.engine) {
            (Some(repo), Some(engine)) => prepare(repo, engine, arguments.default)
                .map(|paths| paths.iter().map(|p| p.display().to_string()).collect()),
            _ => Err(refusal("prepare needs --repo and --engine.")),
        },
        "foreign" => match &arguments.repo {
            Some(repo) => foreign(repo).map(|()| vec![]),
            None => Err(refusal("foreign needs --repo.")),
        },
        "choose" if arguments.engine.as_deref() != Some("python") => {
            Err(refusal("choose records only --engine python."))
        }
        // An explicit engine choice that leaves no activation, recorded as
        // update reads it, so update's default keeps it.
        "choose" => Ok(vec![match record_choice("python") {
            Ok(()) => "recorded".to_owned(),
            Err(error) => {
                // The install itself succeeded; only a later update's default
                // could still move the host.
                let _ = writeln!(
                    io::stderr().lock(),
                    "WARNING: could not record the Python engine choice ({error}); a later pseudolife-mcp update may move this host to the Rust daemon unless it runs with --engine python."
                );
                "unrecorded".to_owned()
            }
        }]),
        _ => accept(DEFAULT_DAEMON_URL).map(|state| vec![state.to_owned()]),
    };
    match result {
        Ok(lines) => answer(&lines),
        Err(refused) => {
            let _ = writeln!(io::stderr().lock(), "{}", refused.message);
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The helpers' world: entries the probe reports until a repair (the
    /// installer's for models, the backup's for the state) changes them, and
    /// one order of every helper and the backup.
    #[derive(Default)]
    struct World {
        unusable: Vec<(&'static str, &'static str)>,
        repaired: Vec<String>,
        order: Vec<String>,
    }

    type Shared = std::rc::Rc<std::cell::RefCell<World>>;

    struct Helpers(Shared);

    fn hex(fields: &[&str]) -> String {
        let mut bytes = Vec::new();
        for field in fields {
            bytes.extend_from_slice(field.as_bytes());
            bytes.push(0);
        }
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    fn left(world: &World, root: &str) -> Vec<(&'static str, &'static str)> {
        world
            .unusable
            .iter()
            .filter(|(_, path)| path.starts_with(&format!("{root}/")) || root.is_empty())
            .filter(|(_, path)| !world.repaired.iter().any(|r| r == path))
            .cloned()
            .collect()
    }

    impl crate::cli::ownership::Docker for Helpers {
        fn docker(&mut self, args: &[String]) -> (i32, String) {
            let mut world = self.0.borrow_mut();
            let root = args.last().unwrap().clone();
            if args.iter().any(|a| a == "--probe-access") {
                world.order.push("probe".into());
                let mut fields = vec!["probe-access", "1"];
                let found = left(&world, "");
                for (owner, path) in &found {
                    fields.extend(["U", *owner, *path]);
                }
                fields.push("end");
                return (0, hex(&fields));
            }
            let entries: Vec<_> = left(&world, &root)
                .into_iter()
                .filter(|(o, _)| *o == "0")
                .collect();
            if args.iter().any(|a| a == "--list-root-owned") {
                world.order.push(format!("list {root}"));
                let mut fields = vec!["list-root-owned", "1"];
                for (_, path) in &entries {
                    fields.extend(["L", "0", *path]);
                }
                fields.push("end");
                return (0, hex(&fields));
            }
            world.order.push(format!("repair {root}"));
            let mut fields = vec!["repair-ownership", "1"];
            for (_, path) in &entries {
                fields.extend(["C", "0", *path]);
            }
            fields.push("end");
            let answer = hex(&fields);
            world
                .repaired
                .extend(entries.iter().map(|(_, p)| (*p).to_owned()));
            (0, answer)
        }
    }

    fn rerun(
        unusable: Vec<(&'static str, &'static str)>,
        backed_up: bool,
    ) -> (Result<()>, Vec<String>, Vec<String>) {
        let world: Shared = Shared::default();
        world.borrow_mut().unusable = unusable;
        let mut docker = Helpers(world.clone());
        let container = json!({
            "Image": "sha256:old",
            "State": {"Running": true},
            "Config": {"User": "", "Env": ["PSEUDOLIFE_DAEMON_MODELS_DIR=/models"]},
            "Mounts": [
                {"Type": "volume", "Name": "s", "Destination": "/data", "RW": true},
                {"Type": "volume", "Name": "m", "Destination": "/models", "RW": true},
            ],
        });
        let dir = std::env::temp_dir().join(format!(
            "pl-installer-own-{}",
            uuid::Uuid::new_v4().simple()
        ));
        let mut said = Vec::new();
        let result = ownership_order(
            &mut docker,
            &container,
            &dir,
            "s",
            || {
                // The native backup: its pair, then its own state repair.
                let mut world = world.borrow_mut();
                world.order.push("backup".into());
                if backed_up {
                    let fixed: Vec<String> = left(&world, "/data")
                        .into_iter()
                        .filter(|(o, _)| *o == "0")
                        .map(|(_, p)| p.to_owned())
                        .collect();
                    world.repaired.extend(fixed);
                }
                backed_up
            },
            &mut |line| said.push(line.to_owned()),
        );
        let _ = std::fs::remove_dir_all(&dir);
        let seen = world.borrow().order.clone();
        (result, seen, said)
    }

    #[test]
    fn a_rerun_on_a_healthy_host_only_probes() {
        let (result, seen, said) = rerun(vec![], true);
        assert!(result.is_ok());
        assert_eq!(seen, ["probe"]);
        assert!(said.is_empty(), "{said:?}");
    }

    #[test]
    fn a_rerun_repairs_models_then_leaves_the_state_to_the_backup_it_takes_first() {
        let (result, seen, said) = rerun(vec![("0", "/models/m"), ("0", "/data/old")], true);
        assert!(result.is_ok(), "{:?}", result.err().map(|r| r.message));
        // One state repair only: the backup's, after its pair; then a probe
        // confirms it.
        assert_eq!(
            seen,
            [
                "probe",
                "list /models",
                "repair /models",
                "probe",
                "backup",
                "probe"
            ]
        );
        assert!(said.iter().any(|l| l.contains("model volume")), "{said:?}");
        assert!(said.iter().any(|l| l.contains("/data/old")), "{said:?}");
    }

    #[test]
    fn a_rerun_whose_backup_fails_never_repairs_the_state() {
        let (result, seen, _) = rerun(vec![("0", "/data/old")], false);
        let refused = result.unwrap_err();
        assert!(
            refused.message.contains("did not complete"),
            "{}",
            refused.message
        );
        assert!(
            refused.message.ends_with("Nothing was started."),
            "{}",
            refused.message
        );
        assert_eq!(seen, ["probe", "backup"]);
    }

    #[test]
    fn a_rerun_whose_state_is_still_unusable_after_the_backup_refuses() {
        // uid 999's entry: the backup's repair (uid 0 only) cannot help.
        let (result, seen, _) = rerun(vec![("999", "/data/theirs")], true);
        let refused = result.unwrap_err();
        assert!(
            refused.message.contains("/data/theirs"),
            "{}",
            refused.message
        );
        assert_eq!(seen, ["probe", "backup", "probe"]);
    }

    #[test]
    fn the_checkout_version_is_the_native_crate_not_pyproject() {
        let repo = std::env::temp_dir().join(format!(
            "pl-installer-checkout-version-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let manifest = repo.join("rust/shim/Cargo.toml");
        fs::create_dir_all(manifest.parent().unwrap()).unwrap();
        // pyproject.toml leaves with the Python package; it is not read.
        fs::write(
            repo.join("pyproject.toml"),
            "[project]\nversion = \"0.0.9\"\n",
        )
        .unwrap();
        fs::write(
            &manifest,
            "[package]\nname = \"pseudolife-stdio\"\nversion = \"0.26.0\"\n",
        )
        .unwrap();
        assert_eq!(checkout_version(&repo).unwrap(), "0.26.0");
        // A version that is no plain release: Rust is unavailable here.
        fs::write(&manifest, "[package]\nversion = \"0.26.0.dev1\"\n").unwrap();
        let refused = checkout_version(&repo).unwrap_err();
        assert!(refused.unavailable, "{}", refused.message);
        assert_eq!(
            cause(&refused.message),
            "Cannot read a release version from this checkout's rust/shim/Cargo.toml (its [package] version is 0.26.0.dev1, not a plain release)"
        );
        assert!(
            refused
                .message
                .contains("check out a release tag (git checkout v<version>) and rerun. Nothing was started.")
        );
        // No plain version at all (a deleted line, version.workspace): the
        // message names the line it needs and the repair, not a release tag
        // (a checkout of a tag would refuse the local change or keep it).
        fs::write(&manifest, "[package]\nname = \"pseudolife-stdio\"\n").unwrap();
        let refused = checkout_version(&repo).unwrap_err();
        assert!(refused.unavailable, "{}", refused.message);
        assert!(
            refused.message.contains(
                "its [package] table needs a plain `version = \"<x.y.z>\"` line: restore it (git checkout -- rust/shim/Cargo.toml) or update the checkout"
            ),
            "{}",
            refused.message
        );
        assert!(
            !refused.message.contains("git checkout v<version>"),
            "{}",
            refused.message
        );
        // Invalid TOML is a broken checkout too: refused without the
        // fallback, with the parse error and the repair.
        fs::write(&manifest, "[package]\nversion = \"0.26.0\"\nnot toml [\n").unwrap();
        let refused = checkout_version(&repo).unwrap_err();
        assert!(!refused.unavailable, "{}", refused.message);
        assert!(
            refused
                .message
                .starts_with("Cannot read rust/shim/Cargo.toml in this checkout (not valid TOML:"),
            "{}",
            refused.message
        );
        assert!(
            refused.message.ends_with(
                "restore it (git checkout -- rust/shim/Cargo.toml) and retry. Nothing was started."
            ),
            "{}",
            refused.message
        );
        // The manifest deleted from a current checkout (rust/shim/ is still
        // there): restore it; pulling would not bring it back.
        fs::remove_file(&manifest).unwrap();
        let refused = checkout_version(&repo).unwrap_err();
        assert!(!refused.unavailable, "{}", refused.message);
        assert!(
            refused.message.ends_with(
                "restore it (git checkout -- rust/shim/Cargo.toml) and retry. Nothing was started."
            ),
            "{}",
            refused.message
        );
        // No rust/shim/ at all: a checkout from before the native client
        // crate, told to update rather than to restore.
        fs::remove_dir_all(manifest.parent().unwrap()).unwrap();
        let refused = checkout_version(&repo).unwrap_err();
        assert!(!refused.unavailable, "{}", refused.message);
        assert_eq!(
            refused.message,
            "This checkout has no rust/shim/Cargo.toml, so it predates the native release version; update the checkout (git pull, or check out a newer release tag) and retry. Nothing was started."
        );
        let _ = fs::remove_dir_all(&repo);
    }

    #[test]
    fn the_fallback_line_keeps_only_the_cause() {
        assert_eq!(
            cause(
                "Cannot pull x:1: this release may not publish a Rust daemon image. Rerun with --engine python. Nothing was started."
            ),
            "Cannot pull x:1: this release may not publish a Rust daemon image"
        );
        assert_eq!(
            cause("Cannot provision the Rust daemon's models; check network access"),
            "Cannot provision the Rust daemon's models"
        );
        assert_eq!(
            cause(
                "Cannot read a release version from this checkout's rust/shim/Cargo.toml (its [package] table names no plain version); check out a release tag (git checkout v<version>) and rerun, or rerun with --engine python. Nothing was started."
            ),
            "Cannot read a release version from this checkout's rust/shim/Cargo.toml (its [package] table names no plain version)"
        );
    }

    #[test]
    fn arguments_follow_the_python_choices() {
        let args = |list: &[&str]| list.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let parsed = parse(&args(&[
            "prepare",
            "--repo",
            "r",
            "--engine",
            "rust",
            "--default",
        ]))
        .unwrap();
        assert_eq!(
            (
                parsed.action.as_str(),
                parsed.engine.as_deref(),
                parsed.default
            ),
            ("prepare", Some("rust"), true)
        );
        assert!(parse(&args(&["prepare", "--engine", "lite"])).is_err());
        let parsed = parse(&args(&["foreign", "--repo", "r"])).unwrap();
        assert_eq!(
            (parsed.action.as_str(), parsed.repo.as_deref()),
            ("foreign", Some(Path::new("r")))
        );
        assert!(parse(&args(&["install"])).is_err());
        assert!(parse(&args(&[])).is_err());
    }

    #[test]
    fn activations_are_32_hex_digits() {
        let one = activation();
        assert_eq!(one.len(), 32);
        assert!(one.bytes().all(|b| b.is_ascii_hexdigit()));
        assert_ne!(one, activation());
    }
}
