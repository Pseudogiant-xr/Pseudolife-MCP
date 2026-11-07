//! Native, opt-in host update transactions. Python remains the differential oracle.
mod backup;
pub(super) mod clients;
mod disk;
mod engine;
mod options;
mod release;
mod seams;
mod shadow;
#[cfg(unix)]
mod signals;
mod unattended;
#[cfg(windows)]
mod windows_job;

use super::lease::lock as os_lock;
pub(crate) use options::Options;
use seams::{Environment, compose_args, compose_environment, display, stamp, tree_state};
pub(crate) use seams::{data_dir, fetch_json, home, run_cli};
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
pub(crate) const PYPI_JSON: &str = "https://pypi.org/pypi/pseudolife-mcp/json";
pub(crate) type Result<T> = std::result::Result<T, UpdateError>;

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
}
impl UpdateError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            exit_code: 1,
        }
    }
    pub fn usage(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            exit_code: 2,
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
    pub maintainer_context: Option<(Vec<String>, Environment)>,
    pub rollback_scope: Option<String>,
    pub stopped_handover: bool,
    pub handover_port: u16,
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

pub(crate) struct UpdateLock {
    lock: os_lock::Lock,
    pid: PathBuf,
}
impl UpdateLock {
    pub fn acquire() -> Result<Self> {
        let mut lock = os_lock::Lock::new(data_dir().join("update.lock"));
        let pid = data_dir().join("update.lock.pid");
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
            return Err(UpdateError::usage(format!(
                "another pseudolife-mcp update is running (pid {holder}); nothing was changed. Run this again when it has finished"
            )));
        }
        let _ = fs::write(&pid, format!("{}\n", std::process::id()));
        Ok(Self { lock, pid })
    }
}
impl Drop for UpdateLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.pid);
        self.lock.release();
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
            maintainer_context: None,
            rollback_scope: None,
            stopped_handover: false,
            handover_port: 5432,
        }
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
        seams::fetch_health_json(
            &format!("{}/health", self.o.daemon_url.trim_end_matches('/')),
            3,
        )
    }
    pub fn daemon_present(&self) -> Result<bool> {
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
    pub fn latest_release(&self) -> Option<String> {
        fetch_json(PYPI_JSON, 10).and_then(|v| {
            v["info"]["version"]
                .as_str()
                .filter(|s| plain_release(s))
                .map(str::to_owned)
        })
    }
    pub fn target_version(&self) -> Result<String> {
        self.o.tag.clone().or_else(||self.latest_release()).ok_or_else(||UpdateError::usage("could not read the newest release from PyPI; name one with --tag <version>. Nothing was changed"))
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
        let latest = self.target_version()?;
        self.report.current = current.clone();
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
        let _lock = UpdateLock::acquire()?;
        self.update()
    }
    pub fn update(&mut self) -> Result<()> {
        if let Some(repo) = engine::selected_checkout(self)? {
            self.report.mode = "checkout".into();
            return engine::deploy(self, &repo);
        }
        if let Some(repo) = self.o.checkout.clone() {
            self.report.mode = "checkout".into();
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
        if self.o.engine.is_some() && tier != "docker" {
            return Err(UpdateError::usage(
                "--engine requires a Docker host; run update --engine <engine> --checkout <repo> there; nothing was changed",
            ));
        }
        if self.o.check {
            return self.check(&tier);
        }
        if tier == "client" {
            let _lock = UpdateLock::acquire()?;
            self.update_client_machine()
        } else {
            self.run_locked()?;
            if !self.o.clients_only && !self.o.shadow {
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
        self.step("updating the client side (shim, plugin cache, Codex hooks)...");
        let report = clients::run_steps(
            &["shim", "plugin", "codex"],
            repo,
            source,
            health.and_then(|h| h["hooks_digest"].as_str()),
            self.o.reinstall,
        );
        if !self.o.as_json {
            for name in ["shim", "plugin", "codex", "autostart", "tunnel"] {
                if let Some(step) = report.get(name) {
                    let _ = output(
                        &format!(
                            "  {name}: {} — {}\n",
                            step["state"].as_str().unwrap_or("unknown"),
                            step["detail"].as_str().unwrap_or("")
                        ),
                        false,
                    );
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
        Ok(o) => Update::new(o).run(),
        Err(e) => {
            let _ = output(&format!("{}\n", e.message), true);
            e.exit_code
        }
    }
}

#[cfg(test)]
mod tests;
