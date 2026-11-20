//! A foreground supervisor owns the vendor process. Stop requests name a
//! single launch and are acted on by that supervisor, never by a recorded PID.
use std::{
    fs::{self, File},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::process::Command;
use uuid::Uuid;

use super::{Fail, KEY_RENEWAL, Output, Profile, record, setup, store, vendor};
use crate::board::state;

mod diagnostics;
pub(super) mod identity;
mod key;
mod process;
#[cfg(test)]
mod tests;

const STARTUP: Duration = Duration::from_secs(30);
const STOP: Duration = Duration::from_secs(10);
const POLL: Duration = Duration::from_millis(100);
const UNSAFE_STORE: Fail = Fail::Tunnel("check that the tunnel store is an owner-only directory");
const RECORD_LIMIT: usize = 64 * 1024;
const NOT_READY: Fail = Fail::Tunnel(
    "the tunnel did not become ready. Check the daemon, tunnel ID and runtime key before retrying",
);
const READINESS_TIMEOUT: Fail = Fail::Tunnel(
    "the tunnel readiness deadline elapsed. Check the daemon, tunnel ID and runtime key before retrying",
);
const CHILD_ERROR: Fail = Fail::Tunnel(
    "the tunnel runtime could not be started or reaped. Check tunnel status before retrying",
);
const CREDENTIAL: Fail = Fail::Tunnel(
    "cannot read the saved daemon credential. Restore its owner-only token file before running the tunnel",
);

pub(super) fn run(root: &Path, name: &str) -> Output {
    let prepared = prepare(root, name)?;
    let executable = std::env::current_exe().map_err(|_| CHILD_ERROR)?;
    let executable = crate::cli::update::seams::command_path(executable);
    #[cfg(unix)]
    if rustix::process::geteuid().is_root() {
        trusted_root_paths([
            root,
            executable.as_path(),
            Path::new(&prepared.profile.token_file),
        ])?;
    }
    let installed = vendor::ensure(root).map_err(Fail::Detail)?;
    #[cfg(unix)]
    if rustix::process::geteuid().is_root() {
        trusted_root_paths([installed.binary.as_path()])?;
    }
    launch(prepared, &executable, STARTUP, |config| {
        let mut command = Command::new(installed.binary);
        command
            .args(["run", "--profile-file"])
            .arg(config)
            .env_clear()
            .envs(vendor::probe_environment(std::env::vars_os()));
        command
    })
}

#[cfg(unix)]
fn trusted_root_paths<'a>(paths: impl IntoIterator<Item = &'a Path>) -> Result<(), Fail> {
    use std::os::unix::fs::MetadataExt;

    for path in paths {
        store::checked_path(path).map_err(|_| UNSAFE_STORE)?;
        for entry in path.ancestors() {
            if !fs::symlink_metadata(entry).is_ok_and(|info| {
                !info.file_type().is_symlink() && info.uid() == 0 && info.mode() & 0o022 == 0
            }) {
                return Err(Fail::Detail(format!(
                    "cannot launch a tunnel as root through {}. Run as the account that owns the saved profile or secure that path",
                    entry.display()
                )));
            }
        }
    }
    Ok(())
}

struct Prepared<'a> {
    root: &'a Path,
    profile: Profile,
    profile_bytes: Vec<u8>,
    key: key::Key,
    _locks: (File, File),
}

fn prepare<'a>(root: &'a Path, name: &str) -> Result<Prepared<'a>, Fail> {
    let path = store::profile_path(root, name)?;
    setup::check_directory(root).map_err(|_| UNSAFE_STORE)?;
    let record = record::path(root, name);
    // Recover interrupted files even if the saved profile or key will
    // later refuse for an unrelated reason, such as an expired key.
    let locks = if store::occupied(&record).map_err(|_| conflict(root, name))? {
        Some(prepare_locks(root, name)?)
    } else {
        None
    };
    let unavailable = || {
        Fail::Detail(format!(
            "cannot read the tunnel profile at {}. Finish tunnel setup before running it",
            path.display()
        ))
    };
    let bytes = store::private_read(&path, RECORD_LIMIT).map_err(|_| unavailable())?;
    let (_, profile) = store::decode_profile(&bytes, name).map_err(|_| unavailable())?;
    if profile.state != "ready" {
        return Err(Fail::Tunnel(
            "finish tunnel setup and save a tunnel ID before running it",
        ));
    }
    if profile
        .expires_at
        .as_ref()
        .is_some_and(|(_, deadline)| *deadline <= chrono::Utc::now())
    {
        return Err(Fail::Tunnel(KEY_RENEWAL));
    }
    if crate::daemon_url::validate(&profile.daemon_url).is_err() {
        return Err(Fail::Tunnel(
            "the saved daemon URL needs an HTTP origin. Repoint it with tunnel setup before running",
        ));
    }
    let key = key::Key::load(root, name)?;
    let locks = locks.map_or_else(|| prepare_locks(root, name), Ok)?;
    if !store::private_read(&path, RECORD_LIMIT).is_ok_and(|current| current == bytes) {
        return Err(Fail::Tunnel(
            "the tunnel profile changed during launch. Check tunnel status before retrying",
        ));
    }
    verify_credential(&profile, &key)?;
    Ok(Prepared {
        root,
        profile,
        profile_bytes: bytes,
        key,
        _locks: locks,
    })
}

fn prepare_locks(root: &Path, name: &str) -> Result<(File, File), Fail> {
    let locks = launch_locks(root, name)?;
    let record = record::path(root, name);
    if store::occupied(&record).map_err(|_| conflict(root, name))? {
        recover_launch(root, name)?;
    }
    Ok(locks)
}

fn verify_credential(profile: &Profile, key: &key::Key) -> Result<(), Fail> {
    let provider =
        crate::credentials::CredentialProvider::new(None, Some((&profile.token_file).into()))
            .map_err(|_| CREDENTIAL)?;
    let snapshot = provider.snapshot().map_err(|_| CREDENTIAL)?;
    let token = snapshot.token().ok_or(CREDENTIAL)?;
    if key.matches(token.as_bytes()) {
        return Err(Fail::Tunnel(
            "the runtime key matches the daemon credential. Import the separate tunnel runtime key before launching",
        ));
    }
    Ok(())
}

fn conflict(root: &Path, name: &str) -> Fail {
    Fail::Detail(format!(
        "tunnel launch ownership is already present or cannot be verified for {name} ({}). Check tunnel status before retrying",
        record::path(root, name).display()
    ))
}

fn lock_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.native-run.lock"))
}

fn legacy_lock_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.lock"))
}

fn open_lock(root: &Path, name: &str, path: &Path) -> Result<File, Fail> {
    let path = store::checked_path(path).map_err(|_| conflict(root, name))?;
    if !store::occupied(&path).map_err(|_| conflict(root, name))? {
        drop(state::create(&path));
    }
    state::open_with_write(&path, true)
        .map(|opened| opened.file)
        .map_err(|_| conflict(root, name))
}

fn launch_locks(root: &Path, name: &str) -> Result<(File, File), Fail> {
    let native = open_lock(root, name, &lock_path(root, name))?;
    native.try_lock().map_err(|_| conflict(root, name))?;
    // The retained Python client flocks this file on Unix and locks its
    // first byte on Windows. Rust's Windows whole-file lock overlaps it.
    let legacy = open_lock(root, name, &legacy_lock_path(root, name))?;
    legacy.try_lock().map_err(|_| conflict(root, name))?;
    Ok((native, legacy))
}

#[derive(Serialize, Deserialize)]
struct Identity {
    pid: u32,
    exe: PathBuf,
}

#[derive(Serialize, Deserialize)]
struct LaunchRecord {
    native_launch: String,
    #[serde(default)]
    launch_identity: Option<String>,
    spawn_started: Option<bool>,
    supervisor: Identity,
    target: Option<Identity>,
    ready: bool,
}

const OWNER_LOCK: &str = "supervisor.lock";

fn stage_path(root: &Path, id: Uuid) -> PathBuf {
    root.join(format!(".native-launch-{}", id.simple()))
}

fn recover_launch(root: &Path, name: &str) -> Result<(), Fail> {
    // The caller owns the native and Python lifetime locks. A dead
    // supervisor alone does not establish that its vendor child is gone.
    let path = record::path(root, name);
    let launch = read_record(&path)?;
    let id = launch_id(&launch.native_launch).ok_or_else(|| conflict(root, name))?;
    let directory = stage_path(root, id);
    let table = record::table();
    #[cfg(target_os = "linux")]
    let group_alive = launch
        .target
        .as_ref()
        .is_some_and(|target| !process::group_is_empty(target.pid));
    #[cfg(not(target_os = "linux"))]
    let group_alive = cfg!(target_os = "macos");
    if (launch.target.is_none() && launch.spawn_started != Some(false))
        || group_alive
        || !record::read(path.clone(), table.as_deref())
            .is_some_and(|record| record.liveness == record::Liveness::Gone)
    {
        return Err(Fail::Detail(format!(
            "cannot confirm the old runtime stopped. Check tunnel status before removing {}",
            directory.display()
        )));
    }
    remove_launch_files(&directory, &format!("{name}.profile.json"))?;
    store::sharing_retry(|| fs::remove_file(&path)).map_err(|_| conflict(root, name))
}

fn remove_launch_files(directory: &Path, profile: &str) -> Result<(), Fail> {
    let blocked = || {
        Fail::Detail(format!(
            "cannot clear the tunnel launch directory {}. Inspect and remove it after confirming the runtime is stopped",
            directory.display()
        ))
    };
    setup::check_directory(directory).map_err(|_| blocked())?;
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err(blocked()),
    };
    let known = [
        profile,
        "profile.yaml",
        "key",
        "health.url",
        "stop",
        OWNER_LOCK,
    ];
    for entry in entries {
        let entry = entry.map_err(|_| blocked())?;
        if !entry
            .file_name()
            .to_str()
            .is_some_and(|name| known.contains(&name))
            || !entry.file_type().is_ok_and(|kind| kind.is_file())
            || store::checked_path(&entry.path()).is_err()
        {
            return Err(blocked());
        }
    }
    for name in known {
        if let Err(error) = store::sharing_retry(|| fs::remove_file(directory.join(name)))
            && error.kind() != std::io::ErrorKind::NotFound
        {
            return Err(blocked());
        }
    }
    store::sharing_retry(|| fs::remove_dir(directory)).map_err(|_| blocked())
}

struct Stage {
    directory: PathBuf,
    record: PathBuf,
    owns_record: bool,
    owner: Option<File>,
    retain: bool,
    launch: String,
    profile: String,
}

impl Stage {
    fn new(root: &Path, name: &str, launch: Uuid) -> Result<Self, Fail> {
        let directory = stage_path(root, launch);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            fs::DirBuilder::new()
                .mode(0o700)
                .create(&directory)
                .map_err(|_| UNSAFE_STORE)?;
        }
        #[cfg(windows)]
        crate::credentials::windows_security::create_dir_owner_only(&directory)
            .map_err(|_| UNSAFE_STORE)?;
        let mut stage = Self {
            directory,
            record: record::path(root, name),
            owns_record: false,
            owner: None,
            retain: false,
            launch: launch.simple().to_string(),
            profile: format!("{name}.profile.json"),
        };
        state::private_dir(&stage.directory).map_err(|_| UNSAFE_STORE)?;
        // This lock belongs to this UUID for the supervisor's entire launch.
        let owner = state::create(&stage.directory.join(OWNER_LOCK))
            .map_err(|_| UNSAFE_STORE)?
            .file;
        owner.try_lock().map_err(|_| UNSAFE_STORE)?;
        stage.owner = Some(owner);
        Ok(stage)
    }

    fn config(&self) -> PathBuf {
        self.directory.join("profile.yaml")
    }

    fn announce(&mut self, record: &LaunchRecord) -> Result<(), Fail> {
        let bytes = serde_json::to_vec(record).map_err(|_| UNSAFE_STORE)?;
        if self.owns_record {
            let identity = state::open(&self.record)
                .map_err(|_| UNSAFE_STORE)?
                .identity;
            let existing = read_record(&self.record)?;
            if existing.native_launch != record.native_launch {
                return Err(UNSAFE_STORE);
            }
            state::atomic_write(&self.record, &bytes, Some(&identity)).map_err(|_| UNSAFE_STORE)
        } else {
            setup::publish_new(&self.record, |file| file.write_all(&bytes))?;
            self.owns_record = true;
            Ok(())
        }
    }
}

impl Drop for Stage {
    fn drop(&mut self) {
        drop(self.owner.take());
        if self.retain {
            crate::stderrln!(
                "Tunnel ownership remains at {}. Check tunnel status.",
                self.record.display()
            );
            return;
        }
        if let Err(Fail::Detail(message)) = remove_launch_files(&self.directory, &self.profile) {
            crate::stderrln!("{message}");
            return;
        }
        if self.owns_record
            && read_record(&self.record).is_ok_and(|record| record.native_launch == self.launch)
            && store::sharing_retry(|| fs::remove_file(&self.record)).is_err()
        {
            crate::stderrln!(
                "Tunnel process record remains at {}. Check tunnel status.",
                self.record.display()
            );
        }
    }
}

fn bridge_command(
    executable: &Path,
    directory: &Path,
    name: &str,
    launch: Uuid,
) -> Result<String, Fail> {
    let quote = |path: &Path| -> Result<String, Fail> {
        let text = path
            .to_str()
            .filter(|value| !value.contains(['\n', '\r', '\0']))
            .ok_or(Fail::Tunnel(
                "the tunnel launch paths must be Unicode and contain no line breaks",
            ))?;
        // The vendor parses this with Go shellwords on Windows too.
        Ok(format!("'{}'", text.replace('\'', "'\\''")))
    };
    Ok(format!(
        "{} tunnel shim --profile {name} --profile-dir {} --launch-id {}",
        quote(executable)?,
        quote(directory)?,
        launch.simple(),
    ))
}

fn launch(
    prepared: Prepared<'_>,
    executable: &Path,
    deadline: Duration,
    command: impl FnOnce(&Path) -> Command,
) -> Output {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| CHILD_ERROR)?;
    let result = runtime.block_on(supervise(prepared, executable, deadline, command));
    runtime.shutdown_background();
    result
}

async fn supervise(
    prepared: Prepared<'_>,
    executable: &Path,
    deadline: Duration,
    command: impl FnOnce(&Path) -> Command,
) -> Output {
    let mut signals = process::Signals::new().map_err(|_| CHILD_ERROR)?;
    let id = Uuid::new_v4();
    let name = &prepared.profile.name;
    let launch_identity = identity::compute(&prepared.profile, &prepared.key, executable)?;
    let mut stage = Stage::new(prepared.root, name, id)?;
    let mut record = LaunchRecord {
        native_launch: id.simple().to_string(),
        launch_identity: Some(launch_identity),
        spawn_started: Some(false),
        supervisor: Identity {
            pid: std::process::id(),
            exe: executable.to_owned(),
        },
        target: None,
        ready: false,
    };
    // Record ownership before a Unix launch writes its plaintext key.
    stage.announce(&record)?;
    let profile_path = stage.directory.join(&stage.profile);
    state::atomic_create(&profile_path, &prepared.profile_bytes).map_err(|_| UNSAFE_STORE)?;
    let key = prepared.key.stage(&stage.directory, id)?;
    let health = stage.directory.join("health.url");
    let mut control = json!({
        "base_url": "https://api.openai.com", "tunnel_id": prepared.profile.tunnel_id,
        "api_key": key.reference
    });
    if let Some(org) = &prepared.profile.organization_id {
        control["organization_id"] = json!(org);
    }
    let config = json!({
        "config_version": 1, "control_plane": control,
        "health": {"listen_addr": "127.0.0.1:0", "url_file": health},
        "admin_ui": {"open_browser": false},
        "log": {"level": "info", "format": "json"},
        "mcp": {"commands": [{"channel": "main", "command": bridge_command(executable, &stage.directory, name, id)?}]}
    });
    let config_path = stage.config();
    state::atomic_create(
        &config_path,
        &serde_json::to_vec(&config).map_err(|_| UNSAFE_STORE)?,
    )
    .map_err(|_| UNSAFE_STORE)?;
    if stop_marker(&stage.directory, id)? {
        return Ok((format!("Tunnel {name} stopped.\n"), 0));
    }
    // A crash between spawn and publication of the target is ambiguous.
    // Durably record that spawning can occur before taking that step.
    record.spawn_started = Some(true);
    stage.announce(&record)?;
    let mut command = command(&config_path);
    let target = PathBuf::from(command.as_std().get_program());
    command
        .current_dir(&stage.directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    let mut child = process::Child::spawn(command).map_err(|_| CHILD_ERROR)?;
    let mut stderr = child
        .take_stderr()
        .map(|pipe| tokio::spawn(diagnostics::drain(pipe)));
    let result = async {
        let pid = child.id().ok_or(CHILD_ERROR)?;
        record.target = Some(Identity { pid, exe: target });
        stage.announce(&record)?;
        let client = reqwest::Client::builder().no_proxy().retry(reqwest::retry::never())
            .redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(1))
            .build().map_err(|_| NOT_READY)?;
        let limit = tokio::time::Instant::now() + deadline;
        loop {
            if stop_marker(&stage.directory, id)? {
                return Ok((format!("Tunnel {name} stopped.\n"), 0));
            }
            if let Some(code) = child.exit_code().map_err(|_| CHILD_ERROR)? {
                return if record.ready {
                    Ok((format!("Tunnel {name} exited.\n"), code))
                } else {
                    Err(NOT_READY)
                };
            }
            if !record.ready {
                if tokio::time::Instant::now() >= limit {
                    return Err(READINESS_TIMEOUT);
                }
                if let Some(url) = health_url(&health) {
                    let probe = client.get(url).send();
                    let ready = tokio::select! {
                        biased;
                        _ = signals.wait() => return Ok((format!("Tunnel {name} stopped.\n"), 0)),
                        response = tokio::time::timeout_at(limit, probe) =>
                            matches!(response, Ok(Ok(response)) if response.status() == reqwest::StatusCode::OK),
                    };
                    if ready {
                        record.ready = true;
                        stage.announce(&record)?;
                        crate::stderrln!("Tunnel {name} is ready.");
                    }
                }
            }
            tokio::select! {
                _ = signals.wait() => return Ok((format!("Tunnel {name} stopped.\n"), 0)),
                () = tokio::time::sleep(POLL) => {},
            }
        }
    }.await;
    if child.close().await.is_err() {
        stage.retain = true;
        if let Some(task) = &stderr {
            task.abort();
        }
        return Err(CHILD_ERROR);
    }
    let mut notes = 0;
    if let Some(task) = stderr.as_mut() {
        match tokio::time::timeout(Duration::from_millis(250), &mut *task).await {
            Ok(Ok(read)) => notes = read,
            _ => task.abort(),
        }
    }
    if notes != 0
        && (result == Err(NOT_READY)
            || result == Err(READINESS_TIMEOUT)
            || result.as_ref().is_ok_and(|(_, code)| *code != 0))
        && let Some(log) = diagnostics::save(prepared.root, name, notes)
    {
        return match result {
            Err(error) => {
                let cause = if error == READINESS_TIMEOUT {
                    "the tunnel readiness deadline elapsed"
                } else {
                    "the tunnel did not become ready"
                };
                Err(Fail::Detail(format!(
                    "{cause}. Check the runtime diagnostics at {}",
                    log.display()
                )))
            }
            Ok((output, code)) => Ok((
                format!("{output}Runtime diagnostics: {}\n", log.display()),
                code,
            )),
        };
    }
    result
}

fn health_url(path: &Path) -> Option<String> {
    let path = store::checked_path(path).ok()?;
    if !fs::symlink_metadata(&path).ok()?.is_file() {
        return None;
    }
    let mut text = String::new();
    File::open(path)
        .ok()?
        .take(257)
        .read_to_string(&mut text)
        .ok()?;
    if text.len() > 256 {
        return None;
    }
    let address = text.trim().strip_prefix("http://127.0.0.1:")?;
    let port: u16 = address.strip_suffix('/').unwrap_or(address).parse().ok()?;
    (port != 0).then(|| format!("http://127.0.0.1:{port}/readyz"))
}

fn read_record(path: &Path) -> Result<LaunchRecord, Fail> {
    store::private_read(path, RECORD_LIMIT).ok()
        .and_then(|bytes| serde_json::from_slice::<LaunchRecord>(&bytes).ok())
        .filter(|record| launch_id(&record.native_launch).is_some())
        .ok_or_else(|| Fail::Detail(format!(
            "cannot prove native ownership of {}. Check tunnel status and stop it with the client that started it",
            path.display()
        )))
}

pub(super) fn launch_id(text: &str) -> Option<Uuid> {
    Uuid::try_parse(text)
        .ok()
        .filter(|id| !id.is_nil() && id.simple().to_string() == text)
}

fn stop_marker(directory: &Path, id: Uuid) -> Result<bool, Fail> {
    let path = directory.join("stop");
    if !store::occupied(&path).map_err(|_| UNSAFE_STORE)? {
        return Ok(false);
    }
    let value = store::private_read(&path, 64).map_err(|_| UNSAFE_STORE)?;
    Ok(value == id.simple().to_string().as_bytes())
}

pub(super) fn stop(root: &Path, name: &str) -> Output {
    store::profile_path(root, name)?;
    let record_path = record::path(root, name);
    let stopped = || Ok((format!("Tunnel {name} is stopped.\n"), 0));
    if !store::occupied(&record_path).map_err(|_| conflict(root, name))? {
        for path in [lock_path(root, name), legacy_lock_path(root, name)] {
            if store::occupied(&path).map_err(|_| conflict(root, name))? {
                let lock = state::open_with_write(&path, true).map_err(|_| conflict(root, name))?;
                lock.file.try_lock().map_err(|_| conflict(root, name))?;
            }
        }
        return stopped();
    }
    setup::check_directory(root).map_err(|_| UNSAFE_STORE)?;
    let record = read_record(&record_path)?;
    let lock =
        state::open_with_write(&lock_path(root, name), true).map_err(|_| conflict(root, name))?;
    match lock.file.try_lock() {
        Err(fs::TryLockError::WouldBlock) => {}
        Ok(()) => {
            return Err(Fail::Detail(format!(
                "the native supervisor is no longer holding its launch lock ({}). Check tunnel status, then use tunnel run to check and recover the old launch",
                record_path.display()
            )));
        }
        Err(_) => return Err(conflict(root, name)),
    }
    let id = launch_id(&record.native_launch).ok_or_else(|| conflict(root, name))?;
    let directory = stage_path(root, id);
    setup::check_directory(&directory).map_err(|_| conflict(root, name))?;
    let marker = directory.join("stop");
    let requested = stop_marker(&directory, id)?
        || setup::publish_new(&marker, |file| {
            file.write_all(record.native_launch.as_bytes())
        })
        .is_ok()
        || stop_marker(&directory, id)?;
    let limit = std::time::Instant::now() + STOP;
    loop {
        if !store::occupied(&record_path).map_err(|_| conflict(root, name))? {
            match lock.file.try_lock() {
                Ok(()) => return stopped(),
                Err(fs::TryLockError::WouldBlock) if requested => {}
                Err(_) => return Err(conflict(root, name)),
            }
        }
        if read_record(&record_path)
            .is_ok_and(|observed| observed.native_launch != record.native_launch)
        {
            return Err(conflict(root, name));
        }
        if !requested {
            return Err(conflict(root, name));
        }
        if std::time::Instant::now() >= limit {
            return Err(Fail::Detail(format!(
                "tunnel {name} did not acknowledge its stop request in time. Check tunnel status before retrying"
            )));
        }
        std::thread::sleep(POLL);
    }
}
