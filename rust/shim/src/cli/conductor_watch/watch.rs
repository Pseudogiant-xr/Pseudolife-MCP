//! The watch: poll the sources, classify what changed, deliver it to the
//! owner as board mail, and keep this watcher's own board row honest.
//!
//! One loop, no threads. Every exit, 0 or 2, goes through `finish`, which
//! sets the row's status to say it stopped and why, ends its attachment so
//! the board no longer shows it live, and removes this watcher's pid
//! record. A watcher killed outright cannot do that; its row then shows
//! the lapse itself: the attachment's lease (60 s on the board) runs out,
//! so `adapter_available` turns false within a minute, and the status set
//! with `expect` turns `status_overdue` within three polls.
use super::{
    events::{self, Event},
    sources::{self, Gh, GhError, GitHub},
};
use crate::cli::lease::board::Failure;
use serde_json::{Map, Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub(super) const LABEL: &str = "conductor-watch";
pub(super) const CLEARS: &str = "decision events";
pub(super) const EXIT_OK: u8 = 0;
pub(super) const EXIT_SETUP: u8 = 2;
/// gh failing this many polls in a row stops the watcher.
const MAX_GH_FAILURES: u32 = 5;
/// The longest run `--max-age` gives: 100 years.
const MAX_DEADLINE: f64 = 100.0 * 365.0 * 86400.0;
/// How often the attachment is renewed; the board's lease on it is 60 s.
const RENEW_EVERY: f64 = 20.0;
/// The longest sleep between checks of the stop request and an interrupt.
const SLICE: f64 = 1.0;
/// The board forgot this address (an ephemeral agent is pruned after it
/// goes quiet): register again, once.
const FORGOTTEN: [&str; 2] = ["instance_not_found", "invalid_credential"];
const STATE_FIX: &str = "pass --state <path to a file in a writable directory>";

/// The files beside the state, as `Path.with_suffix` names them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Files {
    pub(super) state: PathBuf,
    pub(super) pid: PathBuf,
    pub(super) lock: PathBuf,
    pub(super) stop: PathBuf,
    pub(super) log: PathBuf,
    pub(super) ledger: PathBuf,
}

/// What one watcher was asked to do.
#[derive(Clone, Debug)]
pub(super) struct Settings {
    pub(super) owner: String,
    pub(super) files: Files,
    pub(super) until_lease: Option<String>,
    pub(super) repo: Option<String>,
    pub(super) author: Option<String>,
    pub(super) quiet_after: f64,
    pub(super) max_age: f64,
    pub(super) interval: f64,
    pub(super) settle: f64,
    pub(super) suite_results: PathBuf,
    pub(super) leases: Vec<String>,
    pub(super) nonce: Option<String>,
    /// `PSEUDOLIFE_AGENT_PROJECT`, for the board row.
    pub(super) project: Option<String>,
}

/// The board calls a watcher makes.
#[allow(async_fn_in_trait)]
pub(super) trait BoardApi {
    async fn register(&self, task: &str, project: &str) -> Result<String, Failure>;
    async fn forget(&self);
    async fn attach(&self, attachment: &str) -> Result<i64, Failure>;
    async fn heartbeat(&self, attachment: &str, generation: i64) -> Result<(), Failure>;
    async fn detach(&self, attachment: &str, generation: i64) -> Result<(), Failure>;
    async fn agents_page(&self) -> Result<(Vec<Value>, bool), Failure>;
    async fn leases(&self, name: &str) -> Result<Vec<Value>, Failure>;
    async fn send(&self, to: &str, text: &str, request_id: &str) -> Result<(), Failure>;
    async fn update(&self, status: &str, expect: Option<u64>) -> Result<(), Failure>;
}

impl BoardApi for crate::cli::lease::board::Board {
    async fn register(&self, task: &str, project: &str) -> Result<String, Failure> {
        self.register_as(task, project, LABEL, "starting").await?;
        self.agent_id().await.ok_or_else(|| Failure {
            text: "the daemon's registration reply carried no address".into(),
            transient: false,
            fatal_input: false,
            code: None,
        })
    }
    async fn forget(&self) {
        crate::cli::lease::board::Board::forget(self).await;
    }
    async fn attach(&self, attachment: &str) -> Result<i64, Failure> {
        crate::cli::lease::board::Board::attach(self, attachment).await
    }
    async fn heartbeat(&self, attachment: &str, generation: i64) -> Result<(), Failure> {
        crate::cli::lease::board::Board::heartbeat(self, attachment, generation).await
    }
    async fn detach(&self, attachment: &str, generation: i64) -> Result<(), Failure> {
        crate::cli::lease::board::Board::detach(self, attachment, generation).await
    }
    async fn agents_page(&self) -> Result<(Vec<Value>, bool), Failure> {
        crate::cli::lease::board::Board::agents_page(self).await
    }
    async fn leases(&self, name: &str) -> Result<Vec<Value>, Failure> {
        crate::cli::lease::board::Board::leases(self, Some(name))
            .await
            .map(|(leases, _)| leases)
    }
    async fn send(&self, to: &str, text: &str, request_id: &str) -> Result<(), Failure> {
        self.send_clearing(to, text, request_id, CLEARS).await
    }
    async fn update(&self, status: &str, expect: Option<u64>) -> Result<(), Failure> {
        self.update_status(status, expect).await
    }
}

/// Time, output and the host facts the loop reads.
#[allow(async_fn_in_trait)]
pub(super) trait Host {
    /// Seconds on a monotonic clock.
    fn now(&self) -> f64;
    /// Local wall time for the ledger (`%Y-%m-%d %H:%M:%S`).
    fn stamp(&self) -> String;
    /// Local wall time for the status (`%H:%M`).
    fn clock(&self) -> String;
    async fn sleep(&mut self, seconds: f64);
    /// Ctrl-C, SIGTERM, SIGHUP or a console break arrived.
    fn interrupted(&self) -> bool;
    /// Whether `name` is held, from this machine's locks and the board's
    /// page for it (`lease check`'s rule).
    fn lease_held(&self, name: &str, leases: &[Value]) -> bool;
    /// This process's id and its start token, for the pid record.
    fn process(&self) -> (u32, Option<String>);
    fn print(&mut self, line: &str);
    fn say(&mut self, line: &str);
}

/// Why a board step stopped.
enum Halt {
    Transient(String),
    Refused(String),
    Unwritable(String),
}

impl From<Failure> for Halt {
    fn from(failure: Failure) -> Self {
        if failure.transient {
            Self::Transient(failure.text)
        } else {
            Self::Refused(failure.text)
        }
    }
}

struct Pending {
    text: String,
    request_id: String,
    lines: Vec<String>,
}

pub(super) struct Watcher<'a, B: BoardApi, H: Host, G: Gh> {
    settings: &'a Settings,
    board: &'a B,
    gh: &'a mut G,
    host: &'a mut H,
    agent: Option<String>,
    attachment: String,
    generation: Option<i64>,
    renewed_at: f64,
    project: Option<String>,
    last_event: Option<String>,
    last_row: Option<Value>,
    skipped: Vec<String>,
}

fn uuid() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// `os.replace` of a sibling `.tmp`: the file is whole or untouched.
fn save(path: &Path, value: &Value) -> std::io::Result<()> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(parent)?;
    }
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    fs::write(&tmp, serde_json::to_vec_pretty(value).unwrap_or_default())?;
    fs::rename(&tmp, path).inspect_err(|_| {
        let _ = fs::remove_file(&tmp);
    })
}

fn reason(error: &std::io::Error, path: &Path) -> String {
    if path.is_dir() {
        "is a directory".into()
    } else {
        format!("cannot be written ({error})")
    }
}

/// Refuse unless `save` can write `path`: its directory takes a new file,
/// and an existing file can be replaced (rewritten with its own bytes).
fn writable(path: &Path, label: &str) -> Result<(), String> {
    let refused = |why: String| format!("{label} {why}; {STATE_FIX}");
    if path.is_dir() {
        return Err(refused("is a directory".into()));
    }
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let attempt = (|| {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent)?;
        }
        match fs::read(path) {
            Ok(body) => {
                fs::write(&tmp, body)?;
                fs::rename(&tmp, path)
            }
            Err(_) => {
                fs::write(&tmp, b"")?;
                fs::remove_file(&tmp)
            }
        }
    })();
    attempt.map_err(|error| {
        let _ = fs::remove_file(&tmp);
        refused(format!("cannot be written ({error})"))
    })
}

/// Why a state file is not one this watcher wrote, or `None`.
fn state_problem(state: &Value) -> Option<String> {
    let Value::Object(state) = state else {
        return Some(format!("holds {}, not an object", kind(state)));
    };
    for key in ["prs", "master", "suites", "leases"] {
        if let Some(value) = state.get(key)
            && !(value.is_object() || value.is_null())
        {
            return Some(format!("holds {key} as {}, not an object", kind(value)));
        }
    }
    if let Some(Value::Object(prs)) = state.get("prs") {
        for (number, pr) in prs {
            let digits = number.strip_prefix('-').unwrap_or(number);
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) || !pr.is_object() {
                return Some(format!("holds a pull request entry {number:?} it cannot read"));
            }
            let texts = ["title", "head", "checks", "mergeable", "merge_state"]
                .iter()
                .all(|key| pr[*key].is_string());
            let failed = pr["failed"]
                .as_array()
                .is_some_and(|names| names.iter().all(Value::is_string));
            if !(texts && failed) {
                return Some(format!("holds pull request {number} in a shape it cannot read"));
            }
        }
    }
    if let Some(Value::Object(master)) = state.get("master") {
        for (name, run) in master {
            let ok = run["id"].is_i64() && run["conclusion"].is_string() && run["sha"].is_string();
            if !ok {
                return Some(format!("holds master workflow {name:?} in a shape it cannot read"));
            }
        }
    }
    None
}

fn kind(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "a boolean",
        Value::Object(_) => "a JSON object",
        Value::Array(_) => "a list",
        Value::String(_) => "a string",
        Value::Number(_) => "a number",
    }
}

fn object(value: Option<&Value>) -> Option<&Map<String, Value>> {
    value.and_then(Value::as_object)
}

/// A pid record whose `pid` is this process, removed; anything else kept.
pub(super) fn remove_own_pid(path: &Path, pid: u32) {
    let ours = fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|record| record["pid"].as_u64() == Some(u64::from(pid)));
    if ours {
        let _ = fs::remove_file(path);
    }
}

/// Keep the attachment's lease running, at most every `RENEW_EVERY`
/// seconds; re-attach once it lapsed. A failure waits a full period too, so
/// a board that is down is not asked (or logged) every second.
async fn keep_attached<B: BoardApi, H: Host>(
    board: &B,
    host: &mut H,
    attachment: &str,
    generation: &mut Option<i64>,
    renewed_at: &mut f64,
) {
    let Some(current) = *generation else {
        return;
    };
    if host.now() - *renewed_at < RENEW_EVERY {
        return;
    }
    let renewed = match board.heartbeat(attachment, current).await {
        Err(failure) if failure.code.as_deref() == Some("stale_attachment") => board
            .attach(attachment)
            .await
            .map(|fresh| *generation = Some(fresh)),
        other => other,
    };
    *renewed_at = host.now();
    if let Err(failure) = renewed {
        host.say(&format!(
            "the board did not renew this watcher's attachment ({}); trying again in {RENEW_EVERY:.0} s",
            failure.text
        ));
    }
}

/// Why a poll's snapshot ended early.
enum Cut {
    /// gh is not installed or not on an absolute `PATH` directory.
    Missing,
    /// A stop request or an interrupt arrived while gh ran.
    Stopped,
}

impl<'a, B: BoardApi, H: Host, G: Gh> Watcher<'a, B, H, G> {
    pub(super) fn new(settings: &'a Settings, board: &'a B, gh: &'a mut G, host: &'a mut H) -> Self {
        Self {
            settings,
            board,
            gh,
            host,
            agent: None,
            attachment: uuid(),
            generation: None,
            renewed_at: 0.0,
            project: None,
            last_event: None,
            last_row: None,
            skipped: Vec::new(),
        }
    }

    fn ledger(&mut self, what: &str, detail: &str) {
        let path = &self.settings.files.ledger;
        let line = format!("{} {what} {detail}\n", self.host.stamp());
        let written = (|| {
            if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
                fs::create_dir_all(parent)?;
            }
            use std::io::Write;
            fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(path)?
                .write_all(line.as_bytes())
        })();
        if let Err(error) = written {
            self.host.say(&format!("ledger not written ({error})"));
        }
    }

    async fn project(&mut self) -> String {
        if let Some(project) = &self.project {
            return project.clone();
        }
        let printable = |text: &str| text.chars().filter(|c| !c.is_control()).take(120).collect::<String>();
        let project = match (&self.settings.project, &self.settings.repo) {
            (Some(project), _) if !project.is_empty() => printable(project),
            (_, Some(repo)) => printable(repo.rsplit('/').next().unwrap_or_default()),
            _ => GitHub { gh: &mut *self.gh, author: None }
                .repo_name()
                .await
                .map(|name| printable(&name))
                .unwrap_or_default(),
        };
        self.project = Some(project.clone());
        project
    }

    /// Register (unless this address is still known), attach, and write the
    /// pid record that names this watcher. A step that failed is retried by
    /// the next board call, so a registration whose attach failed is not
    /// left unattached for good.
    async fn register(&mut self) -> Result<(), Halt> {
        if self.agent.is_none() {
            let task = format!("decision events for {}", &self.settings.owner[..8]);
            let project = self.project().await;
            self.agent = Some(self.board.register(&task, &project).await?);
            self.generation = None;
        }
        if self.generation.is_some() {
            return Ok(());
        }
        self.generation = Some(self.board.attach(&self.attachment).await?);
        self.renewed_at = self.host.now();
        let agent = self.agent.clone().unwrap_or_default();
        let (pid, start) = self.host.process();
        let record = json!({
            "pid": pid, "process_start": start, "agent_id": agent,
            "owner": self.settings.owner, "started": chrono::Utc::now().timestamp_millis() as f64 / 1000.0,
            "nonce": self.settings.nonce,
        });
        let path = &self.settings.files.pid;
        save(path, &record).map_err(|error| {
            Halt::Unwritable(format!("{} {}; {STATE_FIX}", path.display(), reason(&error, path)))
        })
    }

    /// A board call, registering first when there is no address and again
    /// once when the board forgot this one.
    async fn ensure(&mut self) -> Result<(), Halt> {
        if self.agent.is_none() || self.generation.is_none() {
            self.register().await?;
        }
        Ok(())
    }

    async fn forgotten(&mut self, failure: &Failure) -> Result<bool, Halt> {
        if !failure.code.as_deref().is_some_and(|code| FORGOTTEN.contains(&code)) {
            return Ok(false);
        }
        self.host.say(&format!(
            "the board forgot this watcher ({}); registering again",
            failure.code.as_deref().unwrap_or("")
        ));
        self.board.forget().await;
        self.agent = None;
        self.generation = None;
        self.register().await?;
        Ok(true)
    }

    async fn agents(&mut self) -> Result<(Vec<Value>, bool), Halt> {
        self.ensure().await?;
        match self.board.agents_page().await {
            Err(failure) => {
                if !self.forgotten(&failure).await? {
                    return Err(failure.into());
                }
                Ok(self.board.agents_page().await?)
            }
            Ok(page) => Ok(page),
        }
    }

    async fn send(&mut self, text: &str, request_id: &str) -> Result<(), Halt> {
        self.ensure().await?;
        let settings = self.settings;
        match self.board.send(&settings.owner, text, request_id).await {
            Err(failure) => {
                if !self.forgotten(&failure).await? {
                    return Err(failure.into());
                }
                Ok(self.board.send(&settings.owner, text, request_id).await?)
            }
            Ok(()) => Ok(()),
        }
    }

    async fn update(&mut self, status: &str, expect: Option<u64>) -> Result<(), Halt> {
        self.ensure().await?;
        match self.board.update(status, expect).await {
            Err(failure) => {
                if !self.forgotten(&failure).await? {
                    return Err(failure.into());
                }
                Ok(self.board.update(status, expect).await?)
            }
            Ok(()) => Ok(()),
        }
    }

    async fn renew(&mut self) {
        let Self { board, host, attachment, generation, renewed_at, .. } = self;
        keep_attached(*board, &mut **host, attachment, generation, renewed_at).await;
    }

    /// The owner's row; `None` when the board no longer lists it. A cut
    /// page without it proves nothing, so that reads as the last row seen,
    /// or an empty row before any was seen.
    async fn owner_row(&mut self) -> Result<Option<Value>, Halt> {
        let (agents, truncated) = self.agents().await?;
        if let Some(row) = agents.into_iter().find(|agent| agent["agent_id"] == self.settings.owner.as_str()) {
            self.last_row = Some(row.clone());
            return Ok(Some(row));
        }
        Ok(truncated.then(|| self.last_row.clone().unwrap_or_else(|| json!({}))))
    }

    async fn owner_holds(&mut self, name: &str) -> Result<bool, Halt> {
        let leases = self.board.leases(name).await?;
        for lease in leases {
            if lease["name"] == name {
                let holder = &lease["holder"];
                if !(holder.is_null() || holder.is_object()) {
                    return Err(Halt::Refused("the daemon's leases reply was not understood".into()));
                }
                return Ok(holder["agent_id"] == self.settings.owner.as_str());
            }
        }
        Ok(false)
    }

    async fn lease_states(&mut self, old: Option<&Map<String, Value>>) -> Map<String, Value> {
        let mut snap = old.cloned().unwrap_or_default();
        for name in &self.settings.leases {
            match self.board.leases(name).await {
                Ok(leases) => {
                    let held = self.host.lease_held(name, &leases);
                    snap.insert(name.clone(), json!(if held { "held" } else { "free" }));
                }
                // Free counts only when the board answered; a held local
                // lock is held whatever the board says.
                Err(_) if self.host.lease_held(name, &[]) => {
                    snap.insert(name.clone(), json!("held"));
                }
                Err(_) => {}
            }
        }
        snap
    }

    /// Every source, keeping a failed source's old value. While gh runs the
    /// attachment is renewed, and a stop request or an interrupt ends the
    /// poll at once (`Cut::Stopped`; the loop then exits).
    async fn snapshot(&mut self, old: Option<&Value>) -> Result<(Value, Option<String>), Cut> {
        let old_prs = object(old.and_then(|o| o.get("prs"))).cloned();
        let old_master = object(old.and_then(|o| o.get("master"))).cloned();
        let settings = self.settings;
        let fetched = {
            let Self { gh, board, host, attachment, generation, renewed_at, .. } = self;
            let github = async {
                let mut github = GitHub { gh: &mut **gh, author: settings.author.clone() };
                let prs = github.prs(old_prs.as_ref()).await;
                let master = github.master(old_master.as_ref()).await;
                (prs, master)
            };
            let alive = async {
                loop {
                    keep_attached(*board, &mut **host, attachment, generation, renewed_at).await;
                    if host.interrupted() || settings.files.stop.exists() {
                        return;
                    }
                    host.sleep(SLICE).await;
                }
            };
            tokio::select! {
                biased;
                fetched = github => Some(fetched),
                () = alive => None,
            }
        };
        let Some((prs, master)) = fetched else {
            return Err(Cut::Stopped);
        };
        let mut error = None;
        let mut keep = |result: Result<Map<String, Value>, GhError>, key: &str| match result {
            Ok(snap) => Ok(Value::Object(snap)),
            Err(GhError::Missing) => Err(Cut::Missing),
            Err(GhError::Failed(why)) => {
                error = Some(why);
                Ok(old.and_then(|o| o.get(key)).cloned().unwrap_or(Value::Null))
            }
        };
        let prs = keep(prs, "prs")?;
        let master = keep(master, "master")?;
        let mut notes = Vec::new();
        let suites = sources::suites(
            &self.settings.suite_results,
            object(old.and_then(|o| o.get("suites"))),
            &mut self.skipped,
            &mut |note| notes.push(note),
        );
        for note in notes {
            self.host.say(&note);
            self.ledger("skipped", &note);
        }
        let leases = self.lease_states(object(old.and_then(|o| o.get("leases")))).await;
        Ok((
            json!({"prs": prs, "master": master, "suites": suites, "leases": leases}),
            error,
        ))
    }

    /// The events from `old` to `new`. A pull request missing from the new
    /// list is looked up; while its end is unknown it stays in `new`.
    async fn events(&mut self, old: &Value, new: &mut Value) -> Vec<Event> {
        let mut out = Vec::new();
        if let (Some(old_prs), Some(new_prs)) = (old["prs"].as_object(), new["prs"].as_object_mut()) {
            let mut gone: Vec<String> = old_prs.keys().filter(|k| !new_prs.contains_key(*k)).cloned().collect();
            gone.sort_by_key(|n| n.parse::<i64>().unwrap_or(i64::MAX));
            let mut closed = Vec::new();
            let mut github = GitHub { gh: &mut *self.gh, author: None };
            for number in gone {
                match github.closed_as(&number).await {
                    Some(how) => closed.push((number, how)),
                    None => {
                        new_prs.insert(number.clone(), old_prs[&number].clone());
                    }
                }
            }
            out.extend(events::pr_events(old_prs, new_prs, &closed));
        }
        let empty = Map::new();
        let pick = |value: &Value, key: &str| value[key].as_object().cloned().unwrap_or_else(|| empty.clone());
        out.extend(events::master_events(&pick(old, "master"), &pick(new, "master")));
        out.extend(events::suite_events(&pick(old, "suites"), &pick(new, "suites")));
        out.extend(events::lease_events(&pick(old, "leases"), &pick(new, "leases")));
        out
    }

    /// The state an earlier run saved, or `None` for a fresh baseline.
    fn load(&mut self) -> Option<Value> {
        let path = self.settings.files.state.clone();
        let why = match fs::read(&path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return None,
            Err(error) => format!("could not be read ({error})"),
            Ok(bytes) => match String::from_utf8(bytes) {
                Err(_) => "is not UTF-8 text".into(),
                Ok(text) => match serde_json::from_str::<Value>(&text) {
                    Err(_) => "is not JSON".into(),
                    Ok(state) => match state_problem(&state) {
                        None => return Some(state),
                        Some(why) => why,
                    },
                },
            },
        };
        let detail = format!("state {} {why}; recording a fresh baseline", path.display());
        self.host.say(&detail);
        self.ledger("reset", &detail);
        None
    }

    fn persist(&self, state: &Value) -> Result<(), Halt> {
        let path = &self.settings.files.state;
        save(path, state).map_err(|error| {
            Halt::Unwritable(format!("state {} {}; {STATE_FIX}", path.display(), reason(&error, path)))
        })
    }

    fn status_line(&self, state: Option<&Value>) -> String {
        let prs = state.and_then(|s| s["prs"].as_object()).map_or(0, Map::len);
        let plural = if prs == 1 { "" } else { "s" };
        let last = self
            .last_event
            .as_ref()
            .map_or_else(|| "no events yet".to_owned(), |at| format!("last event {at}"));
        format!("watching {prs} open PR{plural}; {last}")
    }

    /// How long the row's status stays current: three polls, at least
    /// three minutes, so a watcher killed outright shows overdue soon.
    fn expect(&self) -> u64 {
        let seconds = (self.settings.interval * 3.0).clamp(180.0, 7.0 * 86400.0);
        seconds.ceil() as u64
    }

    /// Every exit: say stopped on the row, end the attachment, remove the
    /// pid record. Board failures here never change the exit code.
    async fn finish(&mut self, code: u8, why: &str) -> u8 {
        if self.agent.is_some() {
            let _ = self.board.update(&format!("stopped: {why}"), None).await;
            if let Some(generation) = self.generation {
                let _ = self.board.detach(&self.attachment, generation).await;
            }
        }
        let (pid, _) = self.host.process();
        remove_own_pid(&self.settings.files.pid, pid);
        code
    }

    async fn leave(&mut self, why: &str) -> u8 {
        self.ledger("exit", why);
        self.host.say(&format!("exiting: {why}"));
        self.finish(EXIT_OK, why).await
    }

    /// Tell the owner the watcher stopped, best effort, then exit 2: it
    /// never goes silent.
    async fn give_up(&mut self, why: &str) -> u8 {
        self.host.say(why);
        self.ledger("setup", why);
        let text = format!(
            "conductor-watch stopped: {}. Restart it with `pseudolife-mcp conductor-watch --detach` once that is fixed.",
            events::quote(why)
        );
        if self.send(&text, &uuid()).await.is_err() {
            self.host.say("could not tell the owner");
        }
        self.finish(EXIT_SETUP, "gave up").await
    }

    /// The whole run, from the stop request left by an earlier watcher to
    /// the exit code.
    pub(super) async fn run(&mut self) -> u8 {
        let _ = fs::remove_file(&self.settings.files.stop);
        let files = self.settings.files.clone();
        let checks = writable(&files.state, &format!("state {}", files.state.display()))
            .and_then(|()| writable(&files.pid, &files.pid.display().to_string()));
        if let Err(why) = checks {
            self.host.say(&why);
            self.ledger("setup", &why);
            return EXIT_SETUP;
        }
        match self.register().await {
            Ok(()) => {}
            Err(Halt::Unwritable(why)) => {
                self.host.say(&why);
                self.ledger("setup", &why);
                return self.finish(EXIT_SETUP, "setup failed").await;
            }
            Err(Halt::Transient(why) | Halt::Refused(why)) => {
                let detail = format!("could not register on the board: {why}");
                self.host.say(&detail);
                self.ledger("setup", &detail);
                return self.finish(EXIT_SETUP, "could not register").await;
            }
        }
        let agent = self.agent.clone().unwrap_or_default();
        self.host.print(&format!(
            "conductor-watch {agent} watching for {}; {}",
            self.settings.owner,
            super::park_hint(&agent)
        ));
        let state = self.load();
        self.watch(state).await
    }

    async fn watch(&mut self, mut state: Option<Value>) -> u8 {
        let settings = self.settings;
        let start = self.host.now();
        let deadline = start + settings.max_age.min(MAX_DEADLINE);
        let mut last_sent = start;
        let mut found: Vec<Event> = Vec::new();
        let mut settle_until: Option<f64> = None;
        let mut failures = 0;
        // The batch being delivered: its text and request id stay fixed
        // across retries, so a send the daemon committed but whose reply
        // was lost is deduplicated, not delivered twice.
        let mut pending: Option<Pending> = None;
        loop {
            if self.host.interrupted() {
                return self.leave("interrupted").await;
            }
            if settings.files.stop.exists() {
                let _ = fs::remove_file(&settings.files.stop);
                return self.leave("stopped").await;
            }
            let (mut snap, error) = match self.snapshot(state.as_ref()).await {
                Ok(snapshot) => snapshot,
                Err(Cut::Missing) => return self.give_up("gh is not installed or not on PATH").await,
                // The loop's top says which.
                Err(Cut::Stopped) => continue,
            };
            match &error {
                Some(why) => {
                    failures += 1;
                    self.host.say(&format!("gh failed ({failures}/{MAX_GH_FAILURES}): {why}"));
                    if failures >= MAX_GH_FAILURES {
                        return self
                            .give_up(&format!("gh failed on {failures} polls in a row; last: {why}"))
                            .await;
                    }
                }
                None => failures = 0,
            }
            match &state {
                None => {
                    if error.is_none() {
                        self.host.say("baseline recorded; reporting changes from now");
                        if let Err(Halt::Unwritable(why)) = self.persist(&snap) {
                            return self.give_up(&why).await;
                        }
                        state = Some(snap);
                    }
                }
                Some(old) => {
                    let new = self.events(old, &mut snap).await;
                    state = Some(snap);
                    if !new.is_empty() {
                        found.extend(new);
                        settle_until.get_or_insert(self.host.now() + settings.settle);
                    }
                }
            }
            let now = self.host.now();
            match self.board_turn(&mut found, &mut pending, &mut settle_until, &mut last_sent, state.as_ref(), now, deadline).await {
                Ok(None) => {}
                Ok(Some(why)) => return self.leave(&why).await,
                Err(Halt::Transient(why)) => {
                    self.host.say(&format!("the board failed this poll ({why}); trying again next poll"));
                }
                Err(Halt::Refused(why)) => {
                    self.host.say(&format!("the board refused this watcher: {why}"));
                    self.ledger("setup", &format!("board refused: {why}"));
                    return self.finish(EXIT_SETUP, "board refused").await;
                }
                Err(Halt::Unwritable(why)) => return self.give_up(&why).await,
            }
            if let Some(current) = &state
                && found.is_empty()
                && pending.is_none()
                && let Err(Halt::Unwritable(why)) = self.persist(current)
            {
                return self.give_up(&why).await;
            }
            if now >= deadline {
                let mut unsent: Vec<String> = pending.as_ref().map(|p| p.lines.clone()).unwrap_or_default();
                unsent.extend(found.iter().map(Event::quoted));
                if !unsent.is_empty() {
                    self.ledger("lost", &format!("{} unsent at exit: {}", unsent.len(), unsent.join("; ")));
                }
                return self.leave("max age").await;
            }
            let mut wait = settings.interval;
            let quiet_due = (found.is_empty() && pending.is_none()).then_some(last_sent + settings.quiet_after);
            for due in [if found.is_empty() { None } else { settle_until }, Some(deadline), quiet_due]
                .into_iter()
                .flatten()
            {
                if due > now {
                    wait = wait.min(due - now);
                }
            }
            self.pause(wait).await;
        }
    }

    /// Sleep `seconds` in slices, renewing the attachment and returning
    /// early for a stop request or an interrupt.
    async fn pause(&mut self, seconds: f64) {
        let until = self.host.now() + seconds;
        loop {
            self.renew().await;
            let left = until - self.host.now();
            if left <= 0.0 || self.host.interrupted() || self.settings.files.stop.exists() {
                return;
            }
            self.host.sleep(left.min(SLICE)).await;
        }
    }

    /// The board's part of one poll: the owner still there, the lease it
    /// must hold, the batch or quiet line, the status. `Ok(Some(why))` is
    /// a clean exit.
    #[allow(clippy::too_many_arguments)]
    async fn board_turn(
        &mut self,
        found: &mut Vec<Event>,
        pending: &mut Option<Pending>,
        settle_until: &mut Option<f64>,
        last_sent: &mut f64,
        state: Option<&Value>,
        now: f64,
        deadline: f64,
    ) -> Result<Option<String>, Halt> {
        let Some(row) = self.owner_row().await? else {
            return Ok(Some("owner gone".into()));
        };
        if let Some(lease) = self.settings.until_lease.clone()
            && !self.owner_holds(&lease).await?
        {
            return Ok(Some(format!("lease {lease} no longer held by the owner")));
        }
        if pending.is_none() && !found.is_empty() && settle_until.is_some_and(|until| now >= until.min(deadline)) {
            let (text, kept) = events::next_batch(found);
            // The ledger keeps the quoted lines too: a title cannot forge one.
            let lines = found.drain(..kept).map(|event| event.quoted()).collect();
            *pending = Some(Pending { text, request_id: uuid(), lines });
            *settle_until = if found.is_empty() { None } else { Some(now) };
        }
        if let Some(batch) = pending.as_ref() {
            let (text, request_id) = (batch.text.clone(), batch.request_id.clone());
            self.send(&text, &request_id).await?;
            let lines = pending.take().map(|p| p.lines).unwrap_or_default();
            self.ledger("sent", &format!("{}: {}", lines.len(), lines.join("; ")));
            self.last_event = Some(self.host.clock());
            *last_sent = now;
        } else if found.is_empty()
            && now - *last_sent >= self.settings.quiet_after
            && row["park_reason"] != "done"
        {
            let minutes = ((now - *last_sent) / 60.0).round() as i64;
            let prs = state.and_then(|s| s["prs"].as_object()).cloned().unwrap_or_default();
            self.send(&events::quiet_line(minutes, &prs), &uuid()).await?;
            self.ledger("quiet", &format!("{minutes}m"));
            *last_sent = now;
        }
        let status = self.status_line(state);
        let expect = self.expect();
        self.update(&status, Some(expect)).await?;
        Ok(None)
    }
}

#[cfg(test)]
#[path = "watch_tests.rs"]
mod tests;
