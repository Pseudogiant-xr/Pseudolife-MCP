//! Native, opt-in host update transactions. Python remains the differential oracle.
mod backup;
pub(super) mod clients;
pub(crate) mod disk;
pub(crate) mod engine;
mod options;
mod ownership;
pub(crate) mod release;
pub(crate) mod seams;
mod shadow;
#[cfg(unix)]
mod signals;
pub(crate) mod tunnel_guard;
mod unattended;
#[cfg(windows)]
mod windows_job;

use super::lease::lock as os_lock;
pub(crate) use options::Options;
use seams::{Environment, compose_args, compose_environment, display, stamp, tree_state};
pub(crate) use seams::{data_dir, home, run_cli};

/// The unattended-update schedules as `connect` reads and re-creates them.
pub(crate) mod schedules {
    pub(crate) use super::unattended::{
        Blocker, Installed, NAMES, Stop, TARGET, installed, installed_kind, preflight, reschedule,
    };

    /// A command this process ran for them was cancelled by a signal.
    pub(crate) fn cancelled() -> bool {
        #[cfg(unix)]
        return super::signals::cancelled();
        #[cfg(not(unix))]
        false
    }
}
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    fs,
    io::Write,
    path::{Path, PathBuf},
};

pub(crate) const DAEMON_CONTAINER: &str = "pseudolife-mcp-daemon";
pub(crate) const PG_CONTAINER: &str = "pseudolife-mcp-postgres";
pub(crate) const DAEMON_SERVICE: &str = "pseudolife-daemon";
const GITHUB_RELEASE: &str =
    "https://api.github.com/repos/Pseudogiant-xr/Pseudolife-MCP/releases/latest";
const RELEASE_METADATA_LIMIT: usize = 1024 * 1024;
const NO_PUBLISHED_RELEASE: &str = "no newer release with all native assets is published yet. Name a published release with --tag <version> to install it now.";
pub(crate) type Result<T> = std::result::Result<T, UpdateError>;

fn discovery_error(detail: &str) -> UpdateError {
    UpdateError::usage(format!("{detail}. Nothing was changed"))
}

fn invalid_release_metadata() -> UpdateError {
    discovery_error("GitHub returned invalid release metadata; retry later")
}

fn discovery_transport_error(error: reqwest::Error) -> UpdateError {
    if error.is_timeout() {
        discovery_error(
            "the GitHub release check timed out; check access to api.github.com and retry",
        )
    } else {
        discovery_error(
            "could not reach GitHub or read its reply; check access to api.github.com and retry",
        )
    }
}

fn discovery_client() -> reqwest::ClientBuilder {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
}

async fn discovery_body(mut response: reqwest::Response) -> Result<Vec<u8>> {
    let too_large =
        || discovery_error("GitHub release metadata exceeds the size limit. Retry later");
    if response
        .headers()
        .get(reqwest::header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok()?.parse::<u64>().ok())
        .is_some_and(|size| size > RELEASE_METADATA_LIMIT as u64)
    {
        return Err(too_large());
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(discovery_transport_error)? {
        if chunk.len() > RELEASE_METADATA_LIMIT - bytes.len() {
            return Err(too_large());
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

fn latest_release() -> Result<Option<String>> {
    seams::block_on(async {
        let client = discovery_client().build().map_err(|_| {
            discovery_error(
                "could not initialize the GitHub release check; check HTTPS and proxy settings and retry",
            )
        })?;
        fetch_latest_with(&client, |request| client.execute(request)).await
    })
}

async fn fetch_latest_with<F, Fut>(client: &reqwest::Client, send: F) -> Result<Option<String>>
where
    F: FnOnce(reqwest::Request) -> Fut,
    Fut: std::future::Future<Output = std::result::Result<reqwest::Response, reqwest::Error>>,
{
    let request = client
        .get(GITHUB_RELEASE)
        .header(reqwest::header::ACCEPT, "application/vnd.github+json")
        .header(reqwest::header::USER_AGENT, "pseudolife-mcp")
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|_| {
            discovery_error(
                "could not build the GitHub release request; retry with an updated client",
            )
        })?;
    let response = send(request).await.map_err(discovery_transport_error)?;
    match response.status().as_u16() {
        200 => {}
        404 => return Ok(None),
        status @ 300..=399 => {
            return Err(discovery_error(&format!(
                "GitHub returned a redirection status for release discovery (HTTP {status}). Install the current client or retry with --tag <published version>"
            )));
        }
        status => {
            let headers = response.headers();
            let rate_limited = status == 429
                || (status == 403
                    && (headers.contains_key(reqwest::header::RETRY_AFTER)
                        || headers
                            .get("x-ratelimit-remaining")
                            .is_some_and(|value| value == "0")
                        // Secondary rate limits can omit both headers.
                        || serde_json::from_slice::<Value>(&discovery_body(response).await?)
                            .is_ok_and(|data| {
                                data["message"].as_str().is_some_and(|message| {
                                    message.to_ascii_lowercase().contains("rate limit")
                                })
                            })));
            let detail = if rate_limited {
                "GitHub API rate limit reached; retry after the limit resets".to_owned()
            } else {
                format!("GitHub release check failed (HTTP {status}); retry later")
            };
            return Err(discovery_error(&detail));
        }
    }
    let data: Value = serde_json::from_slice(&discovery_body(response).await?)
        .map_err(|_| invalid_release_metadata())?;
    match (data["draft"].as_bool(), data["prerelease"].as_bool()) {
        (Some(false), Some(false)) => {}
        (Some(_), Some(_)) => return Ok(None),
        _ => return Err(invalid_release_metadata()),
    }
    let version = release_version(&data).ok_or_else(invalid_release_metadata)?;
    if data.get("assets").is_some_and(|assets| !assets.is_array()) {
        return Err(invalid_release_metadata());
    }
    Ok(complete_assets(&data, version).then(|| version.to_owned()))
}

// Keep the positive version range and component rules aligned with the daemon.
fn release_version(data: &Value) -> Option<&str> {
    let version = data["tag_name"]
        .as_str()
        .and_then(|s| s.strip_prefix('v'))?;
    let parts: Vec<_> = version.split('.').collect();
    if !(1..=32).contains(&version.len())
        || !(2..=4).contains(&parts.len())
        || parts.iter().any(|s| {
            s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) || s.parse::<u64>().is_err()
        })
    {
        return None;
    }
    Some(version)
}

// Keep this in step with the daemon while the two discovery paths are separate.
fn complete_assets(data: &Value, version: &str) -> bool {
    let Some(assets) = data["assets"].as_array() else {
        return false;
    };
    // SHA256SUMS uploads first, not last. All six archives must also be present.
    // Keep these names aligned with docs/dev/release-assets.md and its publisher.
    let mut names = vec![
        "RELEASE-READY.json".to_owned(),
        "SHA256SUMS".to_owned(),
        format!("pseudolife-compose-{version}.tar.gz"),
    ];
    for (target, extension) in [
        ("x86_64-unknown-linux-gnu", "tar.gz"),
        ("aarch64-unknown-linux-gnu", "tar.gz"),
        ("x86_64-apple-darwin", "tar.gz"),
        ("aarch64-apple-darwin", "tar.gz"),
        ("x86_64-pc-windows-msvc", "zip"),
    ] {
        names.push(format!("pseudolife-mcp-{version}-{target}.{extension}"));
    }
    names.iter().all(|name| {
        let mut matches = assets.iter().filter(|asset| asset["name"] == *name);
        matches.next().is_some_and(|asset| {
            asset["state"] == "uploaded" && asset["size"].as_u64().is_some_and(|n| n > 0)
        }) && matches.next().is_none()
    })
}

fn client_release_target(health: Option<&Value>, tag: Option<&str>) -> Result<String> {
    let current = health
        .and_then(|h| h["version"].as_str())
        .filter(|version| !version.is_empty())
        .ok_or_else(|| UpdateError::usage("the configured daemon did not report its release on /health; retry with --daemon-url http(s)://<host>:<port> for the running daemon. Nothing was changed"))?;
    if !plain_release(current) && tag.is_none() {
        return Err(UpdateError::usage(
            "the daemon reports a version which is not a plain release number (N.N.N); name the release with --tag <version>. Nothing was changed",
        ));
    }
    Ok(tag.unwrap_or(current).to_owned())
}

#[derive(Debug)]
pub(crate) struct UpdateError {
    pub message: String,
    pub exit_code: u8,
    pub(super) attended_required: bool,
}
impl UpdateError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            exit_code: 1,
            attended_required: false,
        }
    }
    pub fn usage(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            exit_code: 2,
            attended_required: false,
        }
    }
    pub(super) fn attended(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            exit_code: 4,
            attended_required: true,
        }
    }
}
impl std::fmt::Display for UpdateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for UpdateError {}
impl From<std::io::Error> for UpdateError {
    fn from(_: std::io::Error) -> Self {
        Self::new(
            "cannot read or save update files; check file access and free disk space, then retry",
        )
    }
}

#[derive(Default, Debug, Serialize, Deserialize)]
pub(crate) struct Report {
    pub tier: String,
    pub mode: String,
    pub target: Option<String>,
    pub current: Option<String>,
    pub steps: Vec<String>,
    pub rollback: Value,
    pub clients: Option<Value>,
    pub codex_reapproval: String,
    pub ok: bool,
    pub exit_code: u8,
}
#[cfg(test)]
type ReleaseLookup = Box<dyn Fn() -> Result<Option<String>> + Send + Sync>;
#[cfg(test)]
type CommandProbe = fn(&[String]) -> (i32, String);

pub(crate) struct Update {
    pub o: Options,
    pub report: Report,
    pub docker: String,
    pub rollback_lines: Vec<String>,
    pub schema_before: Option<i64>,
    pub selected_engine: String,
    /// The G2 default moved an unchosen host to another engine this run.
    pub defaulted_engine: bool,
    /// The release deploy began its backup: the host changes from here.
    pub release_started: bool,
    /// What a defaulted attempt pulled or created, removed if it falls back.
    pub default_pulled: Option<String>,
    pub default_volumes: Vec<String>,
    /// G2-K: the Python leg that imports weights.pt already ran this update.
    pub legacy_attempt: bool,
    pub maintainer_context: Option<(Vec<String>, Environment)>,
    pub rollback_scope: Option<String>,
    pub stopped_handover: bool,
    pub handover_port: u16,
    /// What this run's ownership repairs changed, repeated after a later
    /// refusal so "nothing was deployed" is never read as "nothing changed".
    pub ownership_changes: Vec<String>,
    /// The next built-in backup archives a stopped daemon's state through the
    /// uid-0 read-only state helper, before that state's ownership repair.
    pub archive_with_helper: bool,
    /// Decides whether moving this machine's launcher to the native runtime
    /// must be refused because a tunnel is registered to start by itself
    /// (`tunnel_guard`); a seam so tests never ask a real service manager.
    pub tunnel_probe: fn(tunnel_guard::Mover, Option<&Path>) -> tunnel_guard::Decision,
    /// The probe passed once this run; later client steps do not ask again.
    pub tunnel_checked: bool,
    /// An unattended run the guard turned daemon-only: its refusal, which
    /// keeps the client steps out even if a later step reached them.
    pub tunnel_refused: Option<String>,
    #[cfg(test)]
    release_lookup: Option<ReleaseLookup>,
    #[cfg(test)]
    health_probe: Option<fn() -> Option<Value>>,
    #[cfg(test)]
    command_probe: Option<CommandProbe>,
}

/// The checkout as the deploy later names it to the shim step.
fn canonical(repo: &Path) -> PathBuf {
    fs::canonicalize(repo)
        .map(seams::command_path)
        .unwrap_or_else(|_| repo.to_owned())
}

fn output(text: &str, stderr: bool) -> std::io::Result<()> {
    let bytes = super::text_bytes(text);
    if stderr {
        let mut stream = std::io::stderr().lock();
        stream.write_all(&bytes).and_then(|_| stream.flush())
    } else {
        let mut stream = std::io::stdout().lock();
        stream.write_all(&bytes).and_then(|_| stream.flush())
    }
}
pub(crate) fn truthy(v: &Value) -> bool {
    !v.is_null()
        && v != &Value::Bool(false)
        && v != &json!(0)
        && v != &json!("")
        && v != &json!([])
        && v != &json!({})
}
pub(crate) fn plain_release(v: &str) -> bool {
    let p: Vec<_> = v.split('.').collect();
    (2..=4).contains(&p.len())
        && p.iter()
            .all(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
}
/// The release version's home in a checkout: the native client crate's
/// manifest (the field the release tooling's reader, `rust/ci/release_version.py`
/// from #1922, reads too).
pub(crate) const RELEASE_MANIFEST: &str = "rust/shim/Cargo.toml";

/// What to do when [`RELEASE_MANIFEST`] names no usable version.
pub(crate) const RELEASE_MANIFEST_NO_VERSION_FIX: &str = "its [package] table needs a plain `version = \"<x.y.z>\"` line: restore it (git checkout -- rust/shim/Cargo.toml) or update the checkout, then retry";

/// Why a checkout's release manifest could not be read, and its fix: a
/// checkout from before the native client crate (no `rust/shim/` at all;
/// the crate dates from 4 October 2026) is updated, any other, a current
/// checkout whose manifest was deleted included, is repaired.
pub(crate) fn release_manifest_unreadable(repo: &Path, error: &std::io::Error) -> String {
    let crate_dir = Path::new(RELEASE_MANIFEST)
        .parent()
        .unwrap_or(Path::new("rust/shim"));
    if error.kind() == std::io::ErrorKind::NotFound && !repo.join(crate_dir).is_dir() {
        format!(
            "this checkout has no {RELEASE_MANIFEST}, so it predates the native release version; update the checkout (git pull, or check out a newer release tag) and retry"
        )
    } else {
        format!(
            "cannot read {RELEASE_MANIFEST} in this checkout ({error}); restore it (git checkout -- {RELEASE_MANIFEST}) and retry"
        )
    }
}

/// The native client crate's package name, which identifies the release
/// manifest as this repository's.
const RELEASE_PACKAGE: &str = "pseudolife-stdio";

fn release_manifest(dir: &Path) -> std::io::Result<toml::Table> {
    let text = fs::read_to_string(dir.join(RELEASE_MANIFEST))?;
    text.parse::<toml::Table>().map_err(|error| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("not valid TOML: {}", error.message()),
        )
    })
}

/// The release version a checkout carries: `[package] version` in
/// [`RELEASE_MANIFEST`]. `Err` when the file cannot be read or is not
/// valid TOML (a broken checkout); `Ok(None)` when it names no string
/// version (`version.workspace = true`, none at all).
pub(crate) fn checkout_release_version(repo: &Path) -> std::io::Result<Option<String>> {
    Ok(release_manifest(repo)?
        .get("package")
        .and_then(toml::Value::as_table)
        .and_then(|package| package.get("version"))
        .and_then(toml::Value::as_str)
        .filter(|version| !version.is_empty())
        .map(str::to_owned))
}

/// Whether `dir` is a checkout of this repository: its release manifest
/// names this repository's crate, or, for a checkout from before the
/// native release version, it carries `pyproject.toml`.
pub(crate) fn is_checkout_root(dir: &Path) -> bool {
    let ours = release_manifest(dir).is_ok_and(|manifest| {
        manifest
            .get("package")
            .and_then(toml::Value::as_table)
            .and_then(|package| package.get("name"))
            .and_then(toml::Value::as_str)
            == Some(RELEASE_PACKAGE)
    });
    ours || dir.join("pyproject.toml").is_file()
}

pub(crate) fn safe_tag(v: &str) -> bool {
    !v.is_empty()
        && v.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}
pub(crate) fn version_key(v: &str) -> Option<Vec<u64>> {
    let s = v
        .trim_start_matches('v')
        .split(|c: char| !c.is_ascii_digit() && c != '.')
        .next()?;
    s.split('.').map(|p| p.parse().ok()).collect()
}

/// The exclusive OS lock on `<data dir>/update.lock`, shared with the Python
/// updater, held from before a run reads what to change until it exits; the
/// pid beside it names the holder. A handed-off client run borrows it: when
/// the holder is the updater that started it, it runs under that hold.
pub(crate) struct UpdateLock {
    held: Option<(os_lock::Lock, PathBuf)>,
}
impl UpdateLock {
    pub fn acquire() -> Result<Self> {
        Self::acquire_in(&data_dir(), None)
    }
    pub fn acquire_borrowing(from: Option<&str>) -> Result<Self> {
        Self::acquire_in(&data_dir(), from)
    }
    pub(crate) fn acquire_in(directory: &Path, borrow_from: Option<&str>) -> Result<Self> {
        let mut lock = os_lock::Lock::new(directory.join("update.lock"));
        let pid = directory.join("update.lock.pid");
        if !lock.acquire().map_err(|_| {
            UpdateError::usage(
                "cannot take the update lock; check the data directory's access and retry",
            )
        })? {
            let holder = fs::read_to_string(&pid).unwrap_or_default();
            let holder =
                if !holder.trim().is_empty() && holder.trim().bytes().all(|b| b.is_ascii_digit()) {
                    holder.trim()
                } else {
                    "unknown"
                };
            if holder != "unknown" && borrow_from == Some(holder) {
                // The holder waits for this run and releases the lock itself;
                // its pid file stays as it is.
                return Ok(Self { held: None });
            }
            return Err(UpdateError::usage(format!(
                "another pseudolife-mcp update is running (pid {holder}); nothing was changed. Run this again when it has finished"
            )));
        }
        let _ = fs::write(&pid, format!("{}\n", std::process::id()));
        Ok(Self {
            held: Some((lock, pid)),
        })
    }
    #[cfg(test)]
    pub(crate) fn borrowed(&self) -> bool {
        self.held.is_none()
    }
}
impl Drop for UpdateLock {
    fn drop(&mut self) {
        if let Some((mut lock, pid)) = self.held.take() {
            let _ = fs::remove_file(&pid);
            lock.release();
        }
    }
}

impl Update {
    pub fn new(o: Options) -> Self {
        Self {
            o,
            report: Report {
                ok: true,
                rollback: json!({}),
                ..Report::default()
            },
            docker: std::env::var("PSEUDOLIFE_DOCKER")
                .ok()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "docker".into()),
            rollback_lines: vec![],
            schema_before: None,
            selected_engine: "python".into(),
            defaulted_engine: false,
            release_started: false,
            default_pulled: None,
            default_volumes: vec![],
            legacy_attempt: false,
            maintainer_context: None,
            rollback_scope: None,
            stopped_handover: false,
            handover_port: 5432,
            ownership_changes: vec![],
            archive_with_helper: false,
            tunnel_probe: tunnel_guard::probe,
            tunnel_checked: false,
            tunnel_refused: None,
            #[cfg(test)]
            release_lookup: None,
            #[cfg(test)]
            health_probe: None,
            #[cfg(test)]
            command_probe: None,
        }
    }
    /// Refuses, before anything is changed, a run whose client steps would
    /// move this machine's launcher to the native runtime while a tunnel is
    /// registered to start by itself (issue #1632). Called at the start of
    /// every path that runs the client steps, and again by `clients` as a
    /// backstop.
    pub fn refuse_tunnel_move(&mut self, repo: Option<&Path>) -> Result<()> {
        self.tunnel_check(repo, false)
    }
    /// The same check where a release deploy is about to update the daemon:
    /// an unattended run does not leave the daemon behind every night for a
    /// client-side service, so it updates the daemon alone (as with
    /// `--daemon-only`), skips the client steps and warns instead.
    pub fn refuse_tunnel_move_before_daemon(&mut self) -> Result<()> {
        self.tunnel_check(None, true)
    }
    fn tunnel_check(&mut self, repo: Option<&Path>, before_daemon: bool) -> Result<()> {
        if self.tunnel_checked {
            return Ok(());
        }
        let mover = if self.o.unattended && before_daemon {
            tunnel_guard::Mover::UnattendedDaemon
        } else if self.o.unattended {
            tunnel_guard::Mover::Unattended
        } else if self.report.mode == "checkout" {
            tunnel_guard::Mover::Checkout
        } else {
            tunnel_guard::Mover::Update
        };
        match (self.tunnel_probe)(mover, repo) {
            tunnel_guard::Decision::Refuse(line)
                if mover == tunnel_guard::Mover::UnattendedDaemon =>
            {
                self.o.daemon_only = true;
                self.tunnel_checked = true;
                self.tunnel_refused = Some(line.clone());
                self.warn(line);
                Ok(())
            }
            tunnel_guard::Decision::Refuse(line) => Err(UpdateError::usage(line)),
            tunnel_guard::Decision::Proceed(notes) => {
                self.tunnel_checked = true;
                for note in notes {
                    self.warn(note);
                }
                Ok(())
            }
        }
    }
    /// The backstop in `clients`: true when the client steps must not run,
    /// with the refusal recorded as the run's result.
    pub(crate) fn refused_clients(&mut self, repo: Option<&Path>) -> bool {
        let refused = match self.tunnel_refused.clone() {
            Some(line) => Err(UpdateError::usage(line)),
            None => self.refuse_tunnel_move(repo),
        };
        let Err(error) = refused else {
            return false;
        };
        self.warn(error.message.clone());
        self.report.ok = false;
        self.report.exit_code = error.exit_code;
        self.report.clients = Some(json!({
            "shim": {"state": "refused", "detail": error.message},
            "ok": false,
        }));
        true
    }
    pub fn step(&mut self, text: impl Into<String>) {
        let text = text.into();
        unattended::log_step(&text);
        if !self.o.as_json {
            let _ = output(&format!("==> {text}\n"), false);
            let _ = std::io::stdout().flush();
        }
        self.report.steps.push(text);
    }
    pub fn warn(&mut self, text: impl Into<String>) {
        let text = text.into();
        unattended::log_step(&format!("WARNING: {text}"));
        if !self.o.as_json {
            let _ = output(&format!("WARNING: {text}\n"), true);
            let _ = std::io::stderr().flush();
        }
        self.report.steps.push(format!("WARNING: {text}"));
    }
    pub fn command(
        &self,
        args: &[String],
        env: Option<&Environment>,
        timeout: u64,
        stream: bool,
    ) -> (i32, String) {
        #[cfg(test)]
        if let Some(probe) = self.command_probe {
            return probe(args);
        }
        let mut cmd = vec![self.docker.clone()];
        cmd.extend_from_slice(args);
        run_cli(&cmd, env, timeout, stream && !self.o.as_json)
    }
    pub fn docker_call(&self, args: &[&str]) -> (i32, String) {
        self.command(
            &args.iter().map(|s| s.to_string()).collect::<Vec<_>>(),
            None,
            60,
            false,
        )
    }
    pub fn daemon_health(&self) -> Option<Value> {
        #[cfg(test)]
        if let Some(probe) = self.health_probe {
            return probe();
        }
        seams::fetch_health_json(
            &format!("{}/health", self.o.daemon_url.trim_end_matches('/')),
            3,
        )
    }
    pub fn daemon_present(&self) -> Result<bool> {
        // `update_cli`: `if not which(self.docker) and self.docker == "docker"`
        // is a client install; `which` finds only an absolute PATH entry's
        // docker, never one in the current directory.
        if self.docker == "docker"
            && crate::cli::program_lookup::find_program_path("docker").is_none()
        {
            return Ok(false);
        }
        let (code, out) = self.docker_call(&["inspect", "-f", "{{.Id}}", DAEMON_CONTAINER]);
        if code == 0 {
            return Ok(true);
        }
        // Missing Docker and a confirmed absent container both mean a native
        // client install. An unavailable Docker engine never means absence.
        if out.contains("missing executable")
            || out.to_ascii_lowercase().contains("no such object")
            || out.to_ascii_lowercase().contains("no such container")
        {
            return Ok(false);
        }
        if reqwest::Url::parse(&self.o.daemon_url)
            .ok()
            .and_then(|u| u.host_str().map(str::to_owned))
            .is_some_and(|h| !matches!(h.as_str(), "localhost" | "127.0.0.1" | "::1"))
            && self.daemon_health().is_some()
        {
            return Ok(false);
        }
        Err(UpdateError::usage(
            "docker is installed but did not answer: start Docker and retry. Nothing was changed",
        ))
    }
    pub fn detect_tier(&self) -> Result<String> {
        Ok(if self.o.checkout.is_some() || self.daemon_present()? {
            "docker"
        } else {
            "client"
        }
        .into())
    }
    pub fn target_version(&self) -> Result<Option<String>> {
        #[cfg(test)]
        if let Some(discover) = &self.release_lookup {
            return self.target_version_with(discover);
        }
        self.target_version_with(latest_release)
    }
    fn target_version_with(
        &self,
        discover: impl FnOnce() -> Result<Option<String>>,
    ) -> Result<Option<String>> {
        match &self.o.tag {
            Some(tag) => Ok(Some(tag.clone())),
            None => discover(),
        }
    }
    pub fn refuse_downgrade(&self, current: Option<&str>, target: &str) -> Result<()> {
        if let Some(current) = current
            && let (Some(a), Some(b)) = (version_key(current), version_key(target))
            && b < a
            && !self.o.allow_downgrade
        {
            return Err(UpdateError::usage(format!(
                "{target} is older than the running {current}; a downgrade needs --tag {target} --allow-downgrade (the bank's schema may be newer than that release knows). Nothing was changed"
            )));
        }
        Ok(())
    }
    pub fn check(&mut self, tier: &str) -> Result<()> {
        let current = if tier == "docker" {
            self.daemon_health()
                .and_then(|v| v["version"].as_str().map(str::to_owned))
        } else {
            Some(env!("CARGO_PKG_VERSION").into())
        };
        self.report.current = current.clone();
        let Some(latest) = self.target_version()? else {
            self.step(NO_PUBLISHED_RELEASE);
            self.report.exit_code = 3;
            return Ok(());
        };
        self.report.target = Some(latest.clone());
        let current = current.ok_or_else(|| {
            UpdateError::usage("could not read the current version (daemon /health did not answer)")
        })?;
        if version_key(&latest) > version_key(&current) {
            self.step(format!(
                "update available: {current} -> {latest} (run: pseudolife-mcp update)"
            ));
        } else {
            self.step(format!("current: {current} is the newest release"));
            self.report.exit_code = 3;
        }
        Ok(())
    }
    pub fn run_locked(&mut self) -> Result<()> {
        let _lock = UpdateLock::acquire_borrowing(self.o.handoff.from.as_deref())?;
        self.update()
    }
    pub fn update(&mut self) -> Result<()> {
        if self.o.checkout.is_none()
            && !self.o.clients_only
            && let Some(recovery) = self.active_import()?
        {
            self.report.mode = "release".into();
            return self.resume_import(recovery, None, None);
        }
        if let Some(repo) = engine::selected_checkout(self)? {
            self.report.mode = "checkout".into();
            if self.o.all_clients {
                self.refuse_tunnel_move(Some(&canonical(&repo)))?;
            }
            return engine::deploy(self, &repo);
        }
        if let Some(repo) = self.o.checkout.clone() {
            self.report.mode = "checkout".into();
            if self.o.all_clients {
                self.refuse_tunnel_move(Some(&canonical(&repo)))?;
            }
            self.deploy_checkout(&repo)
        } else {
            self.report.mode = "release".into();
            self.deploy_release(None, None, None)
        }
    }
    pub fn execute(&mut self) -> Result<()> {
        self.o.validate()?;
        if self.o.engine.is_some() {
            self.o.daemon_only = true;
        }
        if self.o.unschedule {
            return unattended::unschedule(self);
        }
        if let Some(time) = self.o.schedule.clone() {
            return unattended::schedule(self, &time);
        }
        if self.o.unattended {
            self.report.mode = "unattended".into();
            self.report.exit_code = unattended::run_unattended(self)?;
            return Ok(());
        }
        let tier = self.detect_tier()?;
        self.report.tier = tier.clone();
        if tier == "client" && !self.o.clients_only && !self.o.check {
            self.refuse_missing_import_daemon()?;
        }
        if self.o.engine.is_some() && tier != "docker" {
            return Err(UpdateError::usage(
                "--engine requires a Docker host; run update --engine <engine> --checkout <repo> there; nothing was changed",
            ));
        }
        if self.o.check {
            return self.check(&tier);
        }
        if tier == "client" {
            let _lock = UpdateLock::acquire_borrowing(self.o.handoff.from.as_deref())?;
            self.update_client_machine()
        } else {
            self.run_locked()?;
            if !self.o.clients_only
                && !self.o.shadow
                && (self.report.mode != "release" || self.report.target.is_some())
            {
                self.maintainer_passkeys();
            }
            Ok(())
        }
    }
    pub fn run(&mut self) -> u8 {
        self.run_with(|update| update.execute())
    }
    fn run_with(&mut self, action: impl FnOnce(&mut Self) -> Result<()>) -> u8 {
        let mut panic = None;
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| action(self))) {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                self.warn(error.message);
                for change in self.ownership_changes.clone() {
                    self.warn(format!(
                        "before that, this update {change}; only owners changed, and the running daemon is unaffected"
                    ));
                }
                self.report.ok = false;
                self.report.exit_code = error.exit_code;
            }
            Err(payload) => {
                self.report.ok = false;
                self.report.exit_code = 1;
                panic = Some(payload);
            }
        }
        if let Some(path) = self.o.result_file.clone() {
            let result = (|| -> Result<()> {
                seams::write_atomic(&path, format!("{}\n", self.report.exit_code).as_bytes())?;
                let codex = path.with_extension("codex");
                if self.report.codex_reapproval.is_empty() {
                    if codex.exists() {
                        fs::remove_file(codex)?;
                    }
                } else {
                    seams::write_atomic(
                        &codex,
                        format!("{}\n", self.report.codex_reapproval).as_bytes(),
                    )?;
                }
                Ok(())
            })();
            if result.is_err() {
                self.warn(
                    "could not write the update result file; check its access and free disk space",
                );
            }
        }
        if let Some(path) = self.o.handoff.report.clone() {
            // What the updater that handed this run its client steps reads
            // back as its own result (`clients::handoff::hand_off`).
            let written = serde_json::to_vec(&self.report)
                .ok()
                .is_some_and(|bytes| fs::write(&path, bytes).is_ok());
            if !written {
                self.warn(format!(
                    "could not write the hand-off report {}",
                    path.display()
                ));
            }
        }
        if let Some(payload) = panic {
            std::panic::resume_unwind(payload);
        }
        if self.o.as_json
            && output(
                &format!(
                    "{}\n",
                    serde_json::to_string_pretty(&self.report).expect("report")
                ),
                false,
            )
            .is_err()
        {
            return 120;
        }
        self.report.exit_code
    }
    fn maintainer_passkeys(&mut self) {
        if self.daemon_health().is_none_or(|h| h["auth"] != true) {
            return;
        }
        let Ok(exe) = std::env::current_exe() else {
            return;
        };
        // Recreation changes container IDs. Resolve the selected Compose
        // project again; never fall back to another project's global daemon.
        let container = if let Some((args, env)) = &self.maintainer_context {
            match engine::container(self, args, Some(env), DAEMON_SERVICE) {
                Ok(container) => container,
                Err(_) => {
                    self.warn("could not resolve the updated daemon for maintainer setup; use docker ps to find it, then run pseudolife-mcp maintainer setup --container <name>");
                    return;
                }
            }
        } else {
            DAEMON_CONTAINER.into()
        };
        let args = self.maintainer_command(&exe, &container, true);
        let (code, detail) = run_cli(&args, None, 120, false);
        if code != 1 || detail.trim() != "maintainer passkeys: not set up" {
            return;
        }
        let mut remedy = self.maintainer_command(Path::new("pseudolife-mcp"), &container, false);
        remedy.retain(|arg| arg != "--yes");
        self.step(format!("maintainer passkeys are not set up yet (they sign your Console messages and role changes): run {}", display(&remedy)));
        if self.o.as_json
            || !ask(
                "Set them up now? When Tailscale runs, this serves the daemon over HTTPS with tailscale serve (devices on your tailnet can reach it at https://<this machine>:8443), sets its config and restarts it [y/N] ",
            )
        {
            return;
        }
        let args = self.maintainer_command(&exe, &container, false);
        let mut command = std::process::Command::new(&args[0]);
        command.args(&args[1..]);
        if !command.status().is_ok_and(|status| status.success()) {
            self.warn("pseudolife-mcp maintainer setup failed; the update itself succeeded");
        }
    }
    fn maintainer_command(&self, exe: &Path, container: &str, check: bool) -> Vec<String> {
        let mut args = vec![
            exe.to_string_lossy().into_owned(),
            "maintainer".into(),
            "setup".into(),
            if check {
                "--check".into()
            } else {
                "--yes".into()
            },
            "--container".into(),
            container.into(),
        ];
        if let Ok(url) = reqwest::Url::parse(&self.o.daemon_url)
            && matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "::1"))
            && let Some(port) = url.port()
            && port != 8765
        {
            args.extend(["--port".into(), port.to_string()]);
        }
        args
    }
    pub fn clients(&mut self, repo: Option<&Path>, source: &str, health: Option<&Value>) {
        use clients::handoff::{self, Note};
        // Every caller refuses earlier; this only keeps a new caller from
        // moving the launcher under a registered tunnel service.
        if self.refused_clients(repo) {
            return;
        }
        let own = env!("CARGO_PKG_VERSION");
        // A handed-off run never hands off again; a checkout deploy already
        // runs the checkout's own code.
        let handed = self.o.handoff.from.is_some();
        if handed {
            self.step(format!(
                "continuing the client side with {own}'s own code (shim check, plugin cache, Codex hooks)..."
            ));
        } else {
            self.step("updating the client side (shim, plugin cache, Codex hooks)...");
        }
        let digest = health.and_then(|h| h["hooks_digest"].as_str());
        let reinstall = self.o.reinstall;
        let mut outcome = None;
        let mut shim_printed = false;
        let report = if handoff::hands_off(repo, &self.o.handoff) {
            let daemon_url = self.o.daemon_url.clone();
            // PowerShell runs a quoted path only after its call operator.
            let retry_program = std::env::current_exe()
                .map(|exe| {
                    let quoted = seams::quote(&exe.to_string_lossy());
                    if cfg!(windows) {
                        format!("& {quoted}")
                    } else {
                        quoted
                    }
                })
                .unwrap_or_else(|_| "pseudolife-mcp".into());
            let runtime_dir = crate::runtime::default_layout()
                .ok()
                .and_then(|layout| crate::runtime::running_runtime(&layout))
                .map(|runtime| runtime.path);
            let parent = handoff::Parent {
                own_version: own,
                daemon_url: &daemon_url,
                allow_downgrade: self.o.allow_downgrade,
                quiet: self.o.as_json,
                retry_program,
                pid: std::process::id(),
                runtime_dir,
            };
            let mut after_shim = |shim: &Value| -> Option<Value> {
                let (ladder, child) = handoff::hand_off(
                    shim,
                    &parent,
                    &clients::NativeRunner,
                    &handoff::NativeSpawner,
                    &mut |note| match note {
                        Note::Step(text) => self.step(text),
                        Note::Shim(shim) => {
                            if !self.o.as_json {
                                print_row("shim", shim);
                            }
                            shim_printed = true;
                        }
                    },
                )?;
                outcome = child;
                Some(ladder)
            };
            clients::run_steps(
                &["shim", "plugin", "codex"],
                repo,
                source,
                digest,
                reinstall,
                Some(&mut after_shim),
            )
        } else {
            clients::run_steps(
                &["shim", "plugin", "codex"],
                repo,
                source,
                digest,
                reinstall,
                None,
            )
        };
        if let Some(outcome) = outcome {
            self.fold_handoff(report, outcome);
            return;
        }
        if !self.o.as_json {
            for name in ["shim", "handoff", "plugin", "codex", "autostart", "tunnel"] {
                if name == "shim" && shim_printed {
                    continue;
                }
                if let Some(step) = report.get(name) {
                    print_row(name, step);
                }
            }
        }
        if report["ok"] != true {
            self.warn("a client-side step failed (see the ladder above); the daemon update itself succeeded");
            self.report.ok = false;
            self.report.exit_code = 5;
        }
        let text = clients::codex_reapproval_text(report.get("codex"));
        if !text.is_empty() {
            self.report.codex_reapproval = text.clone();
            self.step(text);
        }
        self.report.clients = Some(report);
    }
    /// The child ran the steps after the shim: its ladder, steps, Codex
    /// steps and result are this run's.
    pub(crate) fn fold_handoff(&mut self, report: Value, outcome: clients::handoff::Outcome) {
        // The child printed its own ladder and Codex steps, and its result
        // is this run's.
        for step in &outcome.steps {
            unattended::log_step(step);
        }
        self.report.steps.extend(outcome.steps.iter().cloned());
        if !outcome.codex.is_empty() {
            self.report.codex_reapproval = outcome.codex.clone();
        }
        if report["ok"] != true {
            self.report.ok = false;
            // After a daemon deploy the run's code says the client half
            // failed (5); a clients-only run carries the child's own code.
            let alone = self.o.clients_only || self.report.tier == "client";
            self.report.exit_code = if alone && outcome.exit_code != 0 {
                outcome.exit_code
            } else {
                clients::handoff::CLIENT_STEP_FAILED
            };
            self.warn(format!(
                "the client steps in runtime {} ({}) ended with exit {} (see above){}",
                outcome.runtime,
                outcome.version,
                outcome.exit_code,
                if alone {
                    ""
                } else {
                    "; the daemon update itself succeeded"
                }
            ));
        }
        self.report.clients = Some(report);
    }
    pub fn update_client_machine(&mut self) -> Result<()> {
        if self.o.daemon_only {
            return Err(UpdateError::usage(format!(
                "the daemon is not on this machine (it is configured at {}): run pseudolife-mcp update where it runs. Nothing was changed",
                self.o.daemon_url
            )));
        }
        let health = self.daemon_health();
        let target = client_release_target(health.as_ref(), self.o.tag.as_deref())?;
        let current = health.as_ref().and_then(|h| h["version"].as_str());
        self.refuse_downgrade(current, &target)?;
        self.refuse_checkout_shadow(&target)?;
        self.refuse_tunnel_move(None)?;
        self.report.mode = "clients".into();
        self.report.current = current.map(str::to_owned);
        self.report.target = Some(target.clone());
        self.clients(None, &format!("pseudolife-mcp=={target}"), health.as_ref());
        Ok(())
    }
    pub fn running_image(&self) -> (Option<String>, Option<String>) {
        self.running_image_for(DAEMON_CONTAINER)
    }
    pub fn running_image_for(&self, container: &str) -> (Option<String>, Option<String>) {
        let (code, out) =
            self.docker_call(&["inspect", "-f", "{{.Image}}|{{.Config.Image}}", container]);
        if code == 0
            && let Some((a, b)) = out.trim().split_once('|')
        {
            return (Some(a.into()), Some(b.into()));
        }
        (None, None)
    }
    pub fn refuse_checkout_shadow(&self, target: &str) -> Result<()> {
        if !self.o.reinstall
            && let Some(runtime) = crate::runtime::default_layout()
                .ok()
                .and_then(|layout| crate::runtime::current_runtime(&layout))
            && runtime.version == target
            && (runtime.source_commit.is_some() || Path::new(&runtime.source).is_absolute())
        {
            return Err(UpdateError::usage(
                "the current shim runtime was built from a checkout at this version; a release would replace it for new sessions. Update from that checkout instead, or pass --reinstall to install the release anyway. Nothing was changed",
            ));
        }
        Ok(())
    }
    pub fn rollback_error(&self, message: impl AsRef<str>) -> UpdateError {
        if self.rollback_lines.is_empty() {
            UpdateError::new(message.as_ref())
        } else {
            UpdateError::new(format!(
                "{}\nto roll back:\n{}",
                message.as_ref(),
                self.rollback_lines.join("\n")
            ))
        }
    }
    pub fn schema_rollback_note(&self) -> Vec<String> {
        vec!["      If the new daemon upgraded the bank's schema before failing, the older daemon refuses it,".into(),format!("      so after rolling back the image, restore {} with",if self.o.no_backup {"a backup taken before this deploy (this run took none: --no-backup)"}else{"the backup taken above"}),"      pseudolife-mcp restore --backup-file <dump> --state-archive <state.tgz> --yes. (Omit --state-archive only if no state archive was taken.)".into()]
    }
    pub fn hooks_changed_note(&mut self, previous: Option<&str>, health: Option<&Value>) {
        if let (Some(a), Some(b)) = (previous, health.and_then(|h| h["hooks_digest"].as_str()))
            && a != b
        {
            self.step("the daemon's hook scripts changed with this update and the client side was not moved: run pseudolife-mcp update --clients-only --tag <this version> (from a checkout: ops/update.ps1 -All or ops/update.sh --all) for the shim, plugin cache and Codex re-approval steps");
        }
    }
    pub fn checkout_post_deploy(
        &mut self,
        repo: &Path,
        previous: Option<&str>,
        health: &Value,
    ) -> Result<()> {
        if self.o.all_clients {
            self.clients(Some(repo), &repo.to_string_lossy(), Some(health));
        } else {
            self.hooks_changed_note(previous, Some(health));
        }
        if !self.o.no_test_login {
            self.ensure_test_login();
        }
        Ok(())
    }
    fn ensure_test_login(&mut self) {
        let path = std::env::var_os("PSEUDOLIFE_TEST_PG_LOGIN_FILE")
            .map(PathBuf::from)
            .unwrap_or_else(|| data_dir().join("test-pg.env"));
        if path.is_file() {
            return;
        }
        let Some((compose, env)) = &self.maintainer_context else {
            self.warn("test login not created: no selected Compose project; run pseudolife-mcp test-login create --container <project-postgres>");
            return;
        };
        let Ok(pg) = engine::container(self, compose, Some(env), "pseudolife-pg") else {
            self.warn("test login not created: cannot identify this project's PostgreSQL container; restore that service, then run pseudolife-mcp test-login create --container <project-postgres>");
            return;
        };
        let (code, running) = self.docker_call(&["inspect", "-f", "{{.State.Running}}", &pg]);
        if code != 0 || running.trim() != "true" {
            return;
        }
        let suite = std::env::var("PSEUDOLIFE_TEST_PG_HOST_PORT")
            .ok()
            .or_else(|| {
                fs::read_to_string(home().join(".config/pseudolife-suite/env"))
                    .ok()
                    .and_then(|s| {
                        s.lines().find_map(|l| {
                            l.trim()
                                .strip_prefix("export ")
                                .unwrap_or(l.trim())
                                .strip_prefix("PSEUDOLIFE_TEST_PG_HOST_PORT=")
                                .map(|v| v.trim_matches(['\'', '"']).to_owned())
                        })
                    })
            });
        let confirmed = suite.is_some();
        let suite = suite.unwrap_or_else(|| "127.0.0.1:5433".into());
        let (code, ports) = self.docker_call(&["port", &pg, "5432/tcp"]);
        let Some((host, port)) = suite.rsplit_once(':') else {
            return;
        };
        if code != 0
            || !matches!(
                host.trim_matches(['[', ']']),
                "127.0.0.1" | "localhost" | "::1" | "0.0.0.0" | "::"
            )
            || !ports
                .lines()
                .any(|p| p.rsplit_once(':').is_some_and(|(_, p)| p == port))
        {
            self.step("test login not created: this account's test suite uses a different PostgreSQL server");
            return;
        }
        if !confirmed
            && (self.o.as_json
                || !ask(&format!(
                    "This account's test suite would use the bundled Postgres ({suite}), which also holds the bank. Give it its own login there, one that cannot open the bank? [y/N] "
                )))
        {
            self.step("test login not created: nothing on this account says its test suite uses the bundled Postgres (no PSEUDOLIFE_TEST_PG_HOST_PORT); if it does, run pseudolife-mcp test-login create");
            return;
        }
        if let Ok(exe) = std::env::current_exe() {
            let (code, _) = run_cli(
                &[
                    exe.to_string_lossy().into_owned(),
                    "test-login".into(),
                    "create".into(),
                    "--container".into(),
                    pg.clone(),
                ],
                None,
                120,
                !self.o.as_json,
            );
            if code != 0 {
                self.warn(format!("the test suite's login was not created; run pseudolife-mcp test-login create --container {}", seams::quote(&pg)));
            }
        }
    }
}

fn print_row(name: &str, step: &Value) {
    let _ = output(
        &format!(
            "  {name}: {} — {}\n",
            step["state"].as_str().unwrap_or("unknown"),
            step["detail"].as_str().unwrap_or("")
        ),
        false,
    );
}

fn ask(question: &str) -> bool {
    use std::io::IsTerminal;
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        return false;
    }
    print!("{question}");
    let _ = std::io::stdout().flush();
    let mut answer = String::new();
    std::io::stdin().read_line(&mut answer).is_ok()
        && matches!(answer.trim().to_lowercase().as_str(), "y" | "yes")
}

pub(crate) fn run(arguments: Vec<OsString>) -> u8 {
    // Read once and removed, so nothing this run starts inherits them.
    let handoff = clients::handoff::take_environment();
    if arguments == [OsString::from(clients::handoff::PROBE)] {
        return if output(&clients::handoff::probe_text(), false).is_ok() {
            0
        } else {
            120
        };
    }
    if arguments == [OsString::from("--checkout-handover-capability")]
        || (arguments.len() == 3
            && arguments[0] == "--checkout"
            && arguments[2] == "--checkout-handover-capability")
    {
        return if output("pseudolife-mcp checkout-handover-v1\n", false).is_ok() {
            0
        } else {
            120
        };
    }
    // Wrapper capability probe: exact, read-only and absent from deferred CLIs.
    if arguments == [OsString::from("--native-capability")] {
        return if output("pseudolife-mcp native-update-v1\n", false).is_ok() {
            0
        } else {
            120
        };
    }
    // Image metadata probe: no daemon, configuration, network or bank access.
    // R3 ships this same CLI beside the daemon in its runtime image.
    if arguments == [OsString::from("--schema-version")] {
        let plan: Value = serde_json::from_str(include_str!(
            "../../../../daemon/src/storage/schema_plan.json"
        ))
        .expect("embedded daemon schema plan");
        if let Some(version) = plan["schema_version"].as_u64() {
            return if output(&format!("{version}\n"), false).is_ok() {
                0
            } else {
                120
            };
        }
        return 2;
    }
    // The lite/legacy Python updater is available only by an explicit
    // interpreter selection; native operation never searches for Python.
    if let Some(index) = arguments.iter().position(|s| s == "--python-runtime") {
        let Some(interpreter) = arguments.get(index + 1) else {
            let _ = output("--python-runtime needs an interpreter path\n", true);
            return 2;
        };
        let mut argv = vec![
            interpreter.to_string_lossy().into_owned(),
            "-m".into(),
            "pseudolife_memory.cli".into(),
            "update".into(),
        ];
        argv.extend(
            arguments
                .iter()
                .enumerate()
                .filter(|(i, _)| *i != index && *i != index + 1)
                .map(|(_, s)| s.to_string_lossy().into_owned()),
        );
        return run_cli(&argv, None, 14400, true).0.clamp(0, 255) as u8;
    }
    if arguments.iter().any(|s| s == "--help" || s == "-h") {
        return if output("usage: pseudolife-mcp update [--checkout REPO] [--tag VERSION] [--engine python|rust] [--shadow]\n       [--all | --daemon-only | --clients-only] [--check] [--reinstall] [--allow-downgrade]\n       [--no-backup] [--rollback-tag SUFFIX] [--force-rollback-tag] [--allow-dirty]\n       [--keep-rollbacks N] [--keep-cache-hours N] [--no-cache-prune] [--no-test-login]\n       [--env-file PATH] [--allow-auth-change] [--daemon-url URL] [--health-retries N] [--health-delay-ms N]\n       [--unattended | --schedule HH:MM | --unschedule] [--allow-no-bearer] [--result-file PATH] [--json]\n       [--python-runtime INTERPRETER] (explicit legacy/lite updater)\n",false).is_ok(){0}else{120};
    }
    match Options::parse(arguments) {
        Ok(mut o) => {
            o.handoff = handoff;
            Update::new(o).run()
        }
        Err(e) => {
            let _ = output(&format!("{}\n", e.message), true);
            e.exit_code
        }
    }
}

#[cfg(test)]
mod tests;
