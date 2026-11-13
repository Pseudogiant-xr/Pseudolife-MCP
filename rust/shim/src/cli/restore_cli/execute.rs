//! The restore itself, after every check passed, as one ordered list of
//! steps. Each step runs only after the one before it succeeded; the first
//! failure or interrupt ends the run at that step, and the message then
//! names the bank's state (from the phase reached) and the command that
//! recovers it.
//!
//! | Phase | Steps | The bank after a failure or interrupt there |
//! |---|---|---|
//! | `Unchanged` | clear stale staging files | unchanged; the daemon untouched |
//! | `Stopped` | stop the daemon, safety backup, recovery note, copy and decompress the dump, stage the state archive, re-check space | unchanged; the daemon started again (left stopped when an earlier restore left the bank incomplete) |
//! | `Replacing` | journal, drop, create, replay | database missing or partial, state unchanged; the daemon left stopped |
//! | `StateReplacing` | replace the state volume | database restored, state partial; the daemon left stopped |
//! | `DataRestored` | clear the journal, start the daemon, wait for readiness | restored; the daemon may still be starting |
//!
//! Before the drop, the bank's journal (`restore-in-progress-<bank>.json`
//! in the data directory) records the safety backup, so a later run after an
//! interruption still names the backup taken before the bank changed, not
//! a safety backup of the half-restored bank. A recovery note beside the
//! safety backup holds the same command for a run killed outright.
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

use serde_json::{Value, json};

use super::{
    args::Args,
    bank::{BULK, Bank, CLEANUP, MINUTE, Plan, Step, Stop, sh_quote},
    docker::{Docker, Input, Output},
    safety::{self, Pair},
};

/// What `execute` needs from the host besides Docker: output, the health
/// probe, waiting, and unique names.
pub(super) trait Host {
    fn info(&mut self, line: &str);
    fn error(&mut self, line: &str);
    /// The daemon's `/health` answer, when it answered with a JSON object.
    fn health(&mut self, url: &str) -> Option<Value>;
    fn sleep(&mut self, duration: Duration);
    /// `<YYYYmmdd-HHMMSS>-<8 hex>` for the safety backup's names.
    fn stamp(&mut self) -> String;
    /// A random hex token for staging names.
    fn nonce(&mut self) -> String;
    /// An ISO 8601 time for the journal and the note.
    fn now(&mut self) -> String;
    /// At least 8 GiB free where the safety backup is written.
    fn host_space(&mut self, directory: &str) -> Step<()>;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum Phase {
    Unchanged,
    Stopped,
    Replacing,
    StateReplacing,
    DataRestored,
}

/// The inputs `execute` acts on.
pub(super) struct Context<'a> {
    pub args: &'a Args,
    pub plan: &'a Plan,
    /// The backup and archive as absolute paths.
    pub backup_file: PathBuf,
    pub state_archive: Option<PathBuf>,
    /// Where the journal lives (the update lock's directory).
    pub data_dir: PathBuf,
}

/// `restore-in-progress-<bank>.json`: one journal per bank (container and
/// database), so a restore of another bank never replaces or clears it.
const JOURNAL: &str = "restore-in-progress";
/// Streams stdin into a file owned by the container's own user.
const STREAM_INTO: &str = r#"umask 077 && cat > "$1""#;
const GUNZIP: &str = r#"gunzip -c "$1" > "$2""#;
/// Keep an active move gate (`move.json`) through the replay.
const EXTRACT: &str = "find /data -mindepth 1 -maxdepth 1 ! -name move.json -exec rm -rf -- {} + && tar --exclude=move.json --exclude=./move.json -xzf /tmp/pl_state.tgz -C /data";
const STALE: &str = "rm -f /tmp/pl_restore-*.sql.gz /tmp/pl_restore-*.sql.gz.sql";
/// How long the restarted daemon may take to report ready: the Rust image's
/// first start can provision models before it is ready.
const READY_WAIT: Duration = Duration::from_secs(10 * 60);
const READY_POLL: Duration = Duration::from_secs(2);

/// A word of the printed command for this platform's shell.
pub(super) fn shell_word(text: &str) -> String {
    if cfg!(windows) {
        if !text.is_empty()
            && text
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_./:\\-".contains(&b))
        {
            text.to_owned()
        } else {
            // PowerShell reads all four of these as a single quote.
            let mut quoted = String::from("'");
            for character in text.chars() {
                if matches!(
                    character,
                    '\'' | '\u{2018}' | '\u{2019}' | '\u{201a}' | '\u{201b}'
                ) {
                    quoted.push(character);
                }
                quoted.push(character);
            }
            quoted.push('\'');
            quoted
        }
    } else {
        sh_quote(text)
    }
}

/// A restore command for `backup` (and `state`) naming this bank exactly.
fn command(cx: &Context, backup: &Path, state: Option<&Path>) -> String {
    let plan = cx.plan;
    let mut words: Vec<String> = vec!["pseudolife-mcp".into(), "restore".into()];
    let mut push = |option: &str, value: &str| {
        words.push(option.into());
        words.push(shell_word(value));
    };
    push("--backup-file", &backup.to_string_lossy());
    if let Some(state) = state {
        push("--state-archive", &state.to_string_lossy());
    }
    push("--container", &plan.pg);
    push("--daemon-container", &plan.daemon);
    push("--db", &plan.db);
    push("--user", &plan.user);
    push("--db-port", &plan.port.to_string());
    push("--safety-dir", &plan.safety_dir);
    if !plan.daemon_url.is_empty() {
        push("--daemon-url", &plan.daemon_url);
    }
    if cx.args.leave_stopped {
        words.push("--leave-stopped".into());
    }
    words.push("--yes".into());
    words.join(" ")
}

/// The journal of an earlier restore of this bank that stopped after the
/// drop began.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Earlier {
    pub pair: Pair,
    pub started: String,
}

pub(super) fn journal_file(data_dir: &Path, plan: &Plan) -> PathBuf {
    use sha2::{Digest, Sha256};
    let mut digest = Sha256::new();
    digest.update(plan.pg.as_bytes());
    digest.update([0]);
    digest.update(plan.db.as_bytes());
    let hex: String = digest
        .finalize()
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect();
    data_dir.join(format!("{JOURNAL}-{hex}.json"))
}

fn journal_path(cx: &Context) -> PathBuf {
    journal_file(&cx.data_dir, cx.plan)
}

/// This bank's journal: `Ok(None)` when there is none, `Err` with the
/// refusal when one is there but cannot be read.
pub(super) fn read_journal(data_dir: &Path, plan: &Plan) -> Result<Option<Earlier>, String> {
    let path = journal_file(data_dir, plan);
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(unreadable_journal(&path, &error.to_string())),
    };
    let parsed = (|| {
        let value: Value = serde_json::from_slice(&bytes).ok()?;
        if value["container"] != json!(plan.pg) || value["db"] != json!(plan.db) {
            return None;
        }
        let state = PathBuf::from(value["safety_state"].as_str()?);
        let dump = value["safety_dump"].as_str().map(PathBuf::from);
        Some(Earlier {
            pair: Pair { dump, state },
            started: value["started"]
                .as_str()
                .unwrap_or("an earlier run")
                .to_owned(),
        })
    })();
    parsed
        .map(Some)
        .ok_or_else(|| unreadable_journal(&path, "not a journal of this bank"))
}

fn unreadable_journal(path: &Path, why: &str) -> String {
    format!(
        "the restore journal {} cannot be read ({why}); it records the safety backup an earlier interrupted restore of this bank took before it began replacing the bank. Keep the backup it names (pseudolife_memory-*.sql.gz and pseudolife_state-*.tgz in the safety directory), then delete the file and retry. Nothing was changed",
        path.display()
    )
}

/// Write `bytes` to `path` through a flushed temporary file and a rename,
/// so a reader sees the old file or the new one, never a torn one.
fn replace_file(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let mut temporary = path.as_os_str().to_os_string();
    temporary.push(".tmp");
    let temporary = PathBuf::from(temporary);
    let _ = fs::remove_file(&temporary);
    {
        use std::io::Write;
        let mut file = super::docker::private_file(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
    }
    let renamed = fs::rename(&temporary, path);
    if renamed.is_err() {
        let _ = fs::remove_file(&temporary);
        return renamed;
    }
    match path.parent() {
        Some(parent) => super::safety::sync_dir(parent),
        None => Ok(()),
    }
}

/// A new file, flushed to disk with its directory entry.
fn write_durably(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let mut file = super::docker::private_file(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    match path.parent() {
        Some(parent) => super::safety::sync_dir(parent),
        None => Ok(()),
    }
}

struct Run<'a, 'd, D: Docker, H: Host> {
    bank: Bank<'d, D>,
    host: &'a mut H,
    cx: &'a Context<'a>,
    phase: Phase,
    /// This run stopped the daemon.
    stopped: bool,
    safety: Option<Pair>,
    earlier: Option<Earlier>,
    helper: Option<String>,
    remote: String,
    sql: String,
    staged: bool,
    /// The DROP succeeded (the database is gone, not maybe gone).
    dropped: bool,
}

/// The run ends here: what failed (the [`Stop`]'s text) at the phase
/// reached.
struct Failure(Stop);

fn step<T>(result: Step<T>) -> Result<T, Failure> {
    result.map_err(Failure)
}

impl<'a, 'd, D: Docker, H: Host> Run<'a, 'd, D, H> {
    fn interrupted(&self) -> bool {
        self.bank.docker.interrupted()
    }

    /// Stop between steps when the user asked.
    fn check(&self) -> Result<(), Failure> {
        if self.interrupted() {
            Err(Failure(Stop("interrupted".into(), 130)))
        } else {
            Ok(())
        }
    }

    /// A Docker command that must succeed; `what` names it in the message.
    fn docker(
        &mut self,
        argv: &[&str],
        timeout: Duration,
        input: Input<'_>,
        what: &str,
    ) -> Result<(), Failure> {
        let argv: Vec<String> = argv.iter().map(|s| (*s).to_owned()).collect();
        let ran = self
            .bank
            .docker
            .run_io(&argv, timeout, input, Output::Capture);
        if ran.interrupted {
            return Err(Failure(Stop("interrupted".into(), 130)));
        }
        if !ran.ok() {
            return Err(Failure(Stop(
                format!(
                    "{what} failed; check docker logs {} and docker logs {}",
                    self.plan().pg,
                    self.plan().daemon
                ),
                1,
            )));
        }
        Ok(())
    }

    fn plan(&self) -> &'a Plan {
        self.cx.plan
    }

    fn psql(&mut self, database: &str, args: &[&str]) -> Result<(), Failure> {
        let plan = self.plan();
        self.bank
            .psql(&plan.user, plan.port, database, args)
            .map(|_| ())
            .map_err(Failure)
    }

    fn steps(&mut self) -> Result<(), Failure> {
        let plan = self.plan();
        let cx = self.cx;
        // Unchanged: staging files an interrupted run left in PostgreSQL's
        // /tmp (this host holds the update lock for the whole run).
        self.check()?;
        let pg = plan.pg.clone();
        self.bank.run(&["exec", &pg, "sh", "-c", STALE], MINUTE);
        for stale in &plan.stale_helpers {
            // A killed run's helper may still be extracting into /data.
            self.host.info(&format!(
                "Removing helper container {stale} an earlier restore left behind..."
            ));
            let removed = self.bank.quiet(&["rm", "-f", stale]);
            if !removed.ok() {
                return Err(Failure(Stop(
                    format!(
                        "cannot remove helper container {stale} an earlier restore left behind; run `docker rm -f {stale}`, then retry"
                    ),
                    1,
                )));
            }
        }

        self.check()?;
        self.phase = Phase::Stopped;
        if plan.was_running {
            self.host.info(&format!("==> Stopping {}...", plan.daemon));
            self.stopped = true;
            let daemon = plan.daemon.clone();
            self.docker(
                &["stop", &daemon],
                Duration::from_secs(300),
                Input::Null,
                "stopping the daemon",
            )?;
        }

        self.check()?;
        self.host
            .info("==> Safety-dumping the current database and state volume...");
        let stamp = self.host.stamp();
        let pair = step(safety::backup(&mut self.bank, plan, &stamp))?;
        match &pair.dump {
            Some(dump) => self.host.info(&format!(
                "Safety backup: {} + {}",
                dump.display(),
                pair.state.display()
            )),
            None => self.host.info(&format!(
                "Safety state backup: {} (destination database was absent)",
                pair.state.display()
            )),
        }
        self.safety = Some(pair.clone());
        self.check()?;
        let recovery = self.recovery_command();
        let note = Path::new(&plan.safety_dir).join(format!("pseudolife_restore-{stamp}.txt"));
        let now = self.host.now();
        let text = format!(
            "pseudolife-mcp restore started {now}: replacing database {db} in {pg} (and its daemon {daemon}'s state) with {backup}.\nIf it did not finish, recover the bank as it was before with:\n  {recovery}\n",
            db = plan.db,
            pg = plan.pg,
            daemon = plan.daemon,
            backup = cx.backup_file.display(),
            recovery = recovery.clone().unwrap_or_else(|| self.retry_command()),
        );
        write_durably(&note, text.as_bytes()).map_err(|error| {
            Failure(Stop(
                format!(
                    "cannot write the recovery note {} ({error}); make the safety directory writable, then retry",
                    note.display()
                ),
                1,
            ))
        })?;
        if let Some(recovery) = &recovery {
            self.host.info(&format!(
                "If this restore fails or is interrupted from here on, recover the bank as it was with:\n  {recovery}\n(also written to {})",
                note.display()
            ));
        }

        self.check()?;
        step(self.host.host_space(&plan.safety_dir))?;
        self.host
            .info(&format!("==> Copying the backup into {}...", plan.pg));
        self.staged = true;
        let remote = self.remote.clone();
        let sql = self.sql.clone();
        let backup = cx.backup_file.clone();
        self.docker(
            &["exec", "-i", &pg, "sh", "-c", STREAM_INTO, "_", &remote],
            BULK,
            Input::File(&backup),
            &format!("copying the backup into {pg}"),
        )?;
        self.check()?;
        // The whole SQL is staged before anything is dropped: decompression
        // can use the last free space even when the compressed dump fits.
        self.docker(
            &["exec", &pg, "sh", "-c", GUNZIP, "_", &remote, &sql],
            BULK,
            Input::Null,
            &format!("decompressing the backup inside {pg}"),
        )?;
        if let Some(archive) = &cx.state_archive {
            self.check()?;
            let helper = step(self.bank.helper(&plan.image, EXTRACT, true))?;
            self.helper = Some(helper.clone());
            let target = format!("{helper}:/tmp/pl_state.tgz");
            let archive = archive.to_string_lossy().into_owned();
            self.docker(
                &["cp", &archive, &target],
                BULK,
                Input::Null,
                "staging the state archive",
            )?;
        }
        self.check()?;
        step(self.host.host_space(&plan.safety_dir))?;
        step(self.bank.pg_space(&plan.pg_paths))?;
        if cx.state_archive.is_some() {
            step(self.bank.state_space(&plan.image, &plan.state_mounts))?;
        }

        self.check()?;
        self.write_journal()?;
        self.phase = Phase::Replacing;
        self.host
            .info(&format!("==> Dropping + recreating {}...", plan.db));
        let database = format!("\"{}\"", plan.db.replace('"', "\"\""));
        self.psql(
            "postgres",
            &[
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                &format!("DROP DATABASE IF EXISTS {database} WITH (FORCE)"),
            ],
        )?;
        self.dropped = true;
        self.check()?;
        self.psql(
            "postgres",
            &[
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                &format!("CREATE DATABASE {database}"),
            ],
        )?;
        self.psql(
            "postgres",
            &[
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                &format!("REVOKE CONNECT ON DATABASE {database} FROM PUBLIC"),
            ],
        )?;
        self.check()?;
        self.host.info(&format!(
            "==> Replaying {} into {}...",
            cx.backup_file.display(),
            plan.db
        ));
        let db = plan.db.clone();
        self.psql(&db, &["-q", "-v", "ON_ERROR_STOP=1", "-f", &sql])?;

        if let Some(helper) = self.helper.clone() {
            self.check()?;
            self.phase = Phase::StateReplacing;
            self.host.info("==> Replacing the state volume...");
            self.docker(
                &["start", "-a", &helper],
                BULK,
                Input::Null,
                "replacing the state volume",
            )?;
        }

        self.phase = Phase::DataRestored;
        let _ = fs::remove_file(journal_path(cx));
        if cx.args.leave_stopped {
            self.host
                .info("Restore complete; daemon left stopped (--leave-stopped).");
            return Ok(());
        }
        self.check()?;
        let daemon = plan.daemon.clone();
        self.host.info(&format!("==> Starting {daemon}..."));
        self.docker(
            &["start", &daemon],
            MINUTE * 5,
            Input::Null,
            "starting the daemon",
        )?;
        self.wait_ready()?;
        self.host.info("Restore complete; daemon started.");
        Ok(())
    }

    fn write_journal(&mut self) -> Result<(), Failure> {
        if self.earlier.is_some() {
            // The earliest pre-restore backup stays the recovery point.
            return Ok(());
        }
        let pair = self.safety.as_ref().expect("taken before the journal");
        let now = self.host.now();
        let record = json!({
            "version": 1,
            "container": self.plan().pg,
            "daemon": self.plan().daemon,
            "db": self.plan().db,
            "safety_dump": pair.dump.as_ref().map(|p| p.to_string_lossy().into_owned()),
            "safety_state": pair.state.to_string_lossy(),
            "backup_file": self.cx.backup_file.to_string_lossy(),
            "started": now,
        });
        let path = journal_path(self.cx);
        replace_file(&path, record.to_string().as_bytes()).map_err(|error| {
            Failure(Stop(
                format!(
                    "cannot record the restore journal {} ({error}); make the data directory writable, then retry",
                    path.display()
                ),
                1,
            ))
        })
    }

    fn wait_ready(&mut self) -> Result<(), Failure> {
        let url = format!("{}/health", self.plan().daemon_url.trim_end_matches('/'));
        let attempts = (READY_WAIT.as_secs() / READY_POLL.as_secs()).max(1);
        for attempt in 0..attempts {
            if let Some(health) = self.host.health(&url)
                && ready(&health)
            {
                return Ok(());
            }
            self.check()?;
            if attempt > 0 && attempt % 15 == 0 {
                self.host.info(&format!(
                    "Waiting for {} to report ready at {url}...",
                    self.plan().daemon
                ));
            }
            self.host.sleep(READY_POLL);
        }
        Err(Failure(Stop(
            format!(
                "the restored daemon did not report ready at {url} within {} minutes",
                READY_WAIT.as_secs() / 60
            ),
            1,
        )))
    }

    /// The command that puts back the bank as it was before (the earliest
    /// interrupted restore's backup when there is one); `None` when the
    /// database did not exist then.
    fn recovery_command(&self) -> Option<String> {
        let pair = match &self.earlier {
            Some(earlier) => &earlier.pair,
            None => self.safety.as_ref()?,
        };
        let dump = pair.dump.as_ref()?;
        Some(command(self.cx, dump, Some(&pair.state)))
    }

    fn retry_command(&self) -> String {
        command(
            self.cx,
            &self.cx.backup_file,
            self.cx.state_archive.as_deref(),
        )
    }

    /// The bank's state at a failure, after the daemon was put right for
    /// the phase, and what to run next.
    fn report(&mut self, Failure(stop): Failure) -> u8 {
        let interrupted = stop.1 == 130 || self.interrupted();
        // Cleanup and recovery run to their end.
        self.bank.docker.set_interruptible(false);
        let plan = self.plan();
        let daemon = plan.daemon.clone();
        let cause = if interrupted {
            "restore was interrupted".to_owned()
        } else {
            stop.0.clone()
        };
        let retry = self.retry_command();
        let recovery = self.recovery_command();
        let earlier_note = self.earlier.as_ref().map(|earlier| {
            let missing: Vec<String> = earlier
                .pair
                .dump
                .iter()
                .chain([&earlier.pair.state])
                .filter(|path| !path.is_file())
                .map(|path| path.display().to_string())
                .collect();
            let missing = if missing.is_empty() {
                String::new()
            } else {
                format!(
                    "\n(missing on this host: {}; copy it back from where you keep backups first)",
                    missing.join(", ")
                )
            };
            format!(
                "An earlier restore of this bank ({}) stopped after it began replacing it, so the bank is still incomplete; recover the bank as it was before that restore with:\n  {}{missing}",
                earlier.started,
                recovery.clone().unwrap_or_else(|| retry.clone())
            )
        });
        let mut lines = vec![format!("restore failed: {cause}.")];
        match self.phase {
            Phase::Unchanged | Phase::Stopped => {
                let restarted = if !self.stopped {
                    if plan.was_running {
                        "The bank was not changed and the daemon was not stopped.".to_owned()
                    } else {
                        "The bank was not changed; the daemon was already stopped and stays stopped.".to_owned()
                    }
                } else if self.earlier.is_some() {
                    format!(
                        "The bank was not changed by this run; the daemon {daemon} is left stopped because the bank is incomplete."
                    )
                } else {
                    let ran = self.bank.run(&["start", &daemon], MINUTE * 5);
                    if ran.ok() {
                        format!(
                            "The bank was not changed and the daemon {daemon} was started again."
                        )
                    } else {
                        format!(
                            "The bank was not changed, but the daemon {daemon} is stopped and could not be started again: run docker start {daemon}."
                        )
                    }
                };
                lines.push(format!("{restarted} Nothing was restored."));
                if let Some(note) = earlier_note {
                    lines.push(note);
                } else {
                    lines.push(format!("Fix the cause, then retry with:\n  {retry}"));
                }
            }
            Phase::Replacing | Phase::StateReplacing => {
                if self.phase == Phase::Replacing {
                    // A replay the docker client no longer waits on still
                    // runs in the container: end it, so nothing writes to
                    // the database after this message.
                    let literal = format!("'{}'", plan.db.replace('\'', "''"));
                    let query = format!(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = {literal} AND pid <> pg_backend_pid()"
                    );
                    let _ = self
                        .bank
                        .psql(&plan.user, plan.port, "postgres", &["-Atc", &query]);
                    lines.push(if self.dropped {
                        format!(
                            "The database {} was dropped and is missing or only partly restored; the state volume is unchanged. The daemon {daemon} is left stopped so it cannot serve a half-restored bank.",
                            plan.db
                        )
                    } else {
                        format!(
                            "The DROP of the database {} did not report success, so it may or may not still exist; the state volume is unchanged. The daemon {daemon} is left stopped so it cannot serve a half-restored bank.",
                            plan.db
                        )
                    });
                } else {
                    lines.push(format!(
                        "The database {} holds the backup, but the state volume is only partly replaced. The daemon {daemon} is left stopped so it cannot serve a half-restored bank.",
                        plan.db
                    ));
                }
                match &recovery {
                    Some(recovery) => lines.push(format!(
                        "Recover the bank as it was before this restore with:\n  {recovery}\nor, once the cause is fixed, finish this restore with:\n  {retry}"
                    )),
                    None => lines.push(format!(
                        "The database did not exist before this restore; finish the restore, once the cause is fixed, with:\n  {retry}\n(the state volume as it was is in {})",
                        self.safety
                            .as_ref()
                            .map_or_else(String::new, |pair| pair.state.display().to_string())
                    )),
                }
            }
            Phase::DataRestored => {
                lines.push(format!(
                    "The database and state were restored completely. Check docker logs {daemon}; start it with docker start {daemon} if it is stopped."
                ));
                if let Some(recovery) = &recovery {
                    lines.push(format!(
                        "If the restored bank does not start, recover the bank as it was before with:\n  {recovery}"
                    ));
                }
            }
        }
        for line in &lines {
            self.host.error(line);
        }
        if interrupted { 130 } else { stop.1.max(1) }
    }

    fn cleanup(&mut self) {
        self.bank.docker.set_interruptible(false);
        if self.staged {
            let pg = self.plan().pg.clone();
            let (remote, sql) = (self.remote.clone(), self.sql.clone());
            self.bank
                .run(&["exec", &pg, "rm", "-f", &remote, &sql], CLEANUP);
        }
        if let Some(helper) = self.helper.take() {
            self.bank.run(&["rm", "-f", &helper], CLEANUP);
        }
    }
}

/// `/health` says ready (a daemon from before 0.20.0 has no `ready` field
/// and can attest only liveness).
pub(super) fn ready(health: &Value) -> bool {
    if health["status"] != json!("ok") {
        return false;
    }
    match health.get("ready") {
        Some(value) => value == &json!(true),
        None => health["version"]
            .as_str()
            .and_then(|version| {
                let mut parts = version.split('.').map(|part| part.parse::<u64>().ok());
                Some((parts.next()??, parts.next()??))
            })
            .is_some_and(|(major, minor)| (major, minor) < (0, 20)),
    }
}

/// Run the restore; the exit code (0 restored).
pub(super) fn execute<D: Docker, H: Host>(docker: &mut D, host: &mut H, cx: &Context) -> u8 {
    let nonce = host.nonce();
    let remote = format!("/tmp/pl_restore-{nonce}.sql.gz");
    let earlier = match read_journal(&cx.data_dir, cx.plan) {
        Ok(earlier) => earlier,
        Err(message) => {
            host.error(&format!("restore failed: {message}"));
            return 2;
        }
    };
    let mut run = Run {
        bank: Bank {
            docker,
            pg: cx.plan.pg.clone(),
            daemon: cx.plan.daemon.clone(),
        },
        host,
        cx,
        phase: Phase::Unchanged,
        stopped: false,
        safety: None,
        earlier,
        helper: None,
        sql: format!("{remote}.sql"),
        remote,
        staged: false,
        dropped: false,
    };
    if let Some(earlier) = &run.earlier {
        let note = format!(
            "Note: an earlier restore of {} in {} ({}) stopped after it began replacing the bank; its safety backup stays the recovery point.",
            cx.plan.db, cx.plan.pg, earlier.started
        );
        run.host.info(&note);
    }
    run.bank.docker.set_interruptible(true);
    let code = match run.steps() {
        Ok(()) => 0,
        Err(failure) => run.report(failure),
    };
    run.cleanup();
    code
}
