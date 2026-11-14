//! Starting a watcher: in the foreground (the lock, the board, gh, the
//! loop) or `--detach`ed (the same binary started on its own, then waited
//! for until its pid record names it).
use super::{
    args::Args,
    sources::GhProgram,
    watch::{self, EXIT_OK, EXIT_SETUP, Files, Host, Settings},
};
use serde_json::Value;
use std::{
    fs,
    io::Write,
    path::Path,
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

const DEFAULT_QUIET_AFTER: f64 = 3300.0;
const DEFAULT_MAX_AGE: f64 = 14.0 * 86400.0;
const DEFAULT_INTERVAL: f64 = 60.0;
const DEFAULT_SETTLE: f64 = 45.0;
const DEFAULT_LEASES: [&str; 2] = ["full-suite", "full-suite@box"];
/// How long `--detach` waits for the watcher to register and finish its
/// first poll (two or three gh calls, a few seconds normally) before it
/// says "still starting" and leaves it running.
const DETACH_WAIT: Duration = Duration::from_secs(45);
const DETACH_POLL: Duration = Duration::from_millis(200);

/// What the start needs beyond the parsed arguments.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct Start {
    pub(super) owner: String,
    pub(super) files: Files,
    pub(super) args: Args,
    pub(super) argv: Vec<String>,
    pub(super) home: std::path::PathBuf,
}

/// One line on stderr, with the line ending the rest of this CLI writes.
fn say(text: &str) {
    let line = super::super::text_bytes(&format!(
        "conductor-watch: {text}
"
    ));
    let _ = std::io::stderr().write_all(&line);
}

/// One line on stdout, flushed: a detached watcher's stdout is its log.
fn print(text: &str) {
    let mut out = std::io::stdout();
    let _ = out.write_all(&super::super::text_bytes(&format!(
        "{text}
"
    )));
    let _ = out.flush();
}

pub(super) fn settings(start: &Start) -> Settings {
    let args = &start.args;
    let leases = args.all("--lease");
    Settings {
        owner: start.owner.clone(),
        files: start.files.clone(),
        until_lease: args.last("--until-lease").map(str::to_owned),
        repo: args.last("--repo").map(str::to_owned),
        author: args.last("--author").map(str::to_owned),
        quiet_after: args.seconds("--quiet-after", DEFAULT_QUIET_AFTER),
        max_age: args.max_age(DEFAULT_MAX_AGE),
        interval: args.seconds("--interval", DEFAULT_INTERVAL),
        settle: args.seconds("--settle", DEFAULT_SETTLE),
        suite_results: args.last("--suite-results").map_or_else(
            || start.home.join(".pseudolife-mcp").join("suite-results"),
            std::path::PathBuf::from,
        ),
        leases: if leases.is_empty() {
            DEFAULT_LEASES
                .iter()
                .map(|name| (*name).to_owned())
                .collect()
        } else {
            leases
        },
        nonce: args.last("--nonce").map(str::to_owned),
        project: std::env::var("PSEUDOLIFE_AGENT_PROJECT").ok(),
    }
}

/// The real host: wall clocks, tokio's sleep, the signal flag, this
/// machine's lease locks and this process's start token.
struct RealHost {
    start: Instant,
    interrupted: Arc<AtomicBool>,
}

impl Host for RealHost {
    fn now(&self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }
    fn stamp(&self) -> String {
        chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
    }
    fn clock(&self) -> String {
        chrono::Local::now().format("%H:%M").to_string()
    }
    async fn sleep(&mut self, seconds: f64) {
        tokio::time::sleep(Duration::from_secs_f64(seconds.max(0.0))).await;
    }
    fn interrupted(&self) -> bool {
        self.interrupted.load(Ordering::SeqCst)
    }
    fn lease_held(&self, name: &str, leases: &[Value]) -> bool {
        crate::cli::lease::view::held_with(name, leases)
    }
    fn process(&self) -> (u32, Option<String>) {
        let pid = std::process::id();
        (pid, super::process_start(pid).ok().flatten())
    }
    fn print(&mut self, line: &str) {
        print(line);
    }
    fn say(&mut self, line: &str) {
        say(line);
    }
}

/// Set `flag` on Ctrl-C, SIGTERM or SIGHUP (POSIX) or Ctrl-C and
/// Ctrl-Break (Windows); the loop sees it within a second and exits
/// cleanly, releasing what it holds. A second one exits at once (130).
fn listen_for_interrupts(flag: Arc<AtomicBool>) {
    tokio::spawn(async move {
        #[cfg(unix)]
        {
            use tokio::signal::unix::{SignalKind, signal};
            let (Ok(mut interrupt), Ok(mut term), Ok(mut hangup)) = (
                signal(SignalKind::interrupt()),
                signal(SignalKind::terminate()),
                signal(SignalKind::hangup()),
            ) else {
                return;
            };
            for _ in 0..2 {
                tokio::select! {
                    _ = interrupt.recv() => {}
                    _ = term.recv() => {}
                    _ = hangup.recv() => {}
                }
                if flag.swap(true, Ordering::SeqCst) {
                    std::process::exit(130);
                }
            }
        }
        #[cfg(windows)]
        {
            use tokio::signal::windows::{ctrl_break, ctrl_c};
            let (Ok(mut interrupt), Ok(mut brk)) = (ctrl_c(), ctrl_break()) else {
                return;
            };
            for _ in 0..2 {
                tokio::select! {
                    _ = interrupt.recv() => {}
                    _ = brk.recv() => {}
                }
                if flag.swap(true, Ordering::SeqCst) {
                    std::process::exit(130);
                }
            }
        }
    });
}

/// The folder holding `path`, `.` for a bare name.
fn folder_of(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."))
}

/// `folder` made (or checked) owner-only, as the rest of the client's
/// state is: the watcher's files hold private pull request titles and
/// board ids.
fn private_folder(folder: &Path) -> Result<(), String> {
    crate::board::state::private_dir(folder).map_err(|code| {
        format!(
            "the folder {} cannot be made private ({code}); pass --state <path to a file in a \
             folder only you can write>",
            folder.display()
        )
    })
}

/// The watch in this process: one per state and one per owner, each held
/// by an OS lock for the process's life.
pub(super) fn foreground(start: &Start) -> u8 {
    let settings = settings(start);
    let (owner_lock_path, owner_note) = super::owner_files(&start.home, &start.owner);
    let folders = private_folder(folder_of(&start.files.state))
        .and_then(|()| private_folder(folder_of(&owner_lock_path)));
    if let Err(why) = folders {
        say(&why);
        return EXIT_SETUP;
    }
    let mut lock = crate::cli::lease::lock::Lock::new(start.files.lock.clone());
    match lock.acquire() {
        Ok(true) => {}
        Ok(false) => {
            say(&format!(
                "another watcher holds {}; this one exits",
                start.files.state.display()
            ));
            return EXIT_OK;
        }
        Err(error) => {
            say(&format!(
                "cannot lock {} ({error}); pass --state <path to a file in a writable directory>",
                start.files.lock.display()
            ));
            return EXIT_SETUP;
        }
    }
    // One watcher per owner: a second one for the same owner, on another
    // state file, would deliver every batch twice.
    let mut owner_lock = crate::cli::lease::lock::Lock::new(owner_lock_path.clone());
    match owner_lock.acquire() {
        Ok(true) => {
            let note = serde_json::json!({"state": start.files.state.display().to_string()});
            if let Err(error) = watch::save(&owner_note, &note) {
                say(&format!(
                    "cannot write {} ({error}); pass --state <path to a file in a writable directory>",
                    owner_note.display()
                ));
                lock.release();
                return EXIT_SETUP;
            }
        }
        Ok(false) => {
            lock.release();
            let other = super::owner_watcher(&start.home, &start.owner);
            match other.as_deref().map(super::running) {
                Some(Ok(Some(record))) => {
                    print(super::already(&record).stdout.trim_end());
                }
                _ => say(&format!(
                    "another watcher for {} holds {}; this one exits",
                    start.owner,
                    owner_lock_path.display()
                )),
            }
            return EXIT_OK;
        }
        Err(error) => {
            lock.release();
            say(&format!(
                "cannot lock {} ({error}); pass --state <path to a file in a writable directory>",
                owner_lock_path.display()
            ));
            return EXIT_SETUP;
        }
    }
    let code = watch_here(&settings);
    owner_lock.release();
    lock.release();
    code
}

fn watch_here(settings: &Settings) -> u8 {
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        say("could not start the async runtime");
        return EXIT_SETUP;
    };
    runtime.block_on(async {
        let board = match crate::cli::lease::board::connect(false, &Default::default()) {
            Ok(board) => board,
            Err(failure) => {
                say(&format!("no board: {}", failure.text));
                append_ledger(
                    &settings.files.ledger,
                    "setup",
                    &format!("no board: {}", failure.text),
                );
                return EXIT_SETUP;
            }
        };
        let interrupted = Arc::new(AtomicBool::new(false));
        listen_for_interrupts(interrupted.clone());
        let mut host = RealHost {
            start: Instant::now(),
            interrupted,
        };
        let mut gh = GhProgram {
            // Absolute `PATH` directories only: never the current directory.
            program: crate::cli::program_lookup::find_program_path("gh"),
            repo: settings.repo.clone(),
        };
        watch::Watcher::new(settings, board.as_ref(), &mut gh, &mut host)
            .run()
            .await
    })
}

/// One ledger line before the loop exists (a setup failure).
fn append_ledger(path: &Path, what: &str, detail: &str) {
    let line = format!(
        "{} {what} {detail}\n",
        chrono::Local::now().format("%Y-%m-%d %H:%M:%S")
    );
    let written = path
        .parent()
        .map_or(Ok(()), fs::create_dir_all)
        .and_then(|()| fs::OpenOptions::new().create(true).append(true).open(path))
        .and_then(|mut file| file.write_all(line.as_bytes()));
    if let Err(error) = written {
        say(&format!("ledger not written ({error})"));
    }
}

/// Keep this process's own standard handles out of the child: Rust's
/// `Command` passes every inheritable handle on, so a detached watcher
/// would otherwise hold the caller's output pipe open until it exits, and
/// whoever ran `--detach` would wait for it.
#[cfg(windows)]
#[allow(unsafe_code)]
fn keep_standard_handles() {
    use windows_sys::Win32::{
        Foundation::{HANDLE_FLAG_INHERIT, SetHandleInformation},
        System::Console::{GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE},
    };
    for which in [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
        // SAFETY: GetStdHandle returns this process's handle (or null or
        // INVALID_HANDLE_VALUE, which SetHandleInformation refuses); the
        // call only clears the inherit flag on it.
        unsafe {
            let handle = GetStdHandle(which);
            if !handle.is_null() {
                SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0);
            }
        }
    }
}

/// `OWNER/NAME` as gh names a repository: two parts of letters, digits,
/// `.`, `_` and `-`.
fn repo_name_shape(text: &str) -> bool {
    let part = |p: &str| {
        !p.is_empty()
            && p.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    };
    text.split_once('/')
        .is_some_and(|(owner, name)| part(owner) && part(name))
}

/// The repository gh finds from this folder, for a detached watcher that
/// runs from its own folder. `Ok(None)` when gh is not installed (the
/// watcher then says so to its owner); `Err` with the reason otherwise.
fn caller_repository() -> Result<Option<String>, String> {
    let Some(program) = crate::cli::program_lookup::find_program_path("gh") else {
        return Ok(None);
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| format!("could not start the async runtime ({error})"))?;
    let mut gh = GhProgram {
        program: Some(program),
        repo: None,
    };
    let out = runtime
        .block_on(super::sources::Gh::call(
            &mut gh,
            &["repo", "view", "--json", "nameWithOwner"],
        ))
        .map_err(|error| match error {
            super::sources::GhError::Missing => "gh is not installed".to_owned(),
            super::sources::GhError::Failed(why) => why,
        })?;
    let value: Value = serde_json::from_str(&out).map_err(|_| "gh printed no JSON".to_owned())?;
    match value["nameWithOwner"].as_str() {
        Some(name) if repo_name_shape(name) => Ok(Some(name.to_owned())),
        _ => Err("gh named no repository".to_owned()),
    }
}

/// What the child wrote to its log since `offset`: this start's lines only.
fn log_since(log: &Path, offset: u64) -> String {
    let Ok(bytes) = fs::read(log) else {
        return String::new();
    };
    let start = usize::try_from(offset)
        .unwrap_or(usize::MAX)
        .min(bytes.len());
    String::from_utf8_lossy(&bytes[start..]).into_owned()
}

/// The watcher for `start`, started on its own from its state's folder;
/// then its proof that it registered and finished its first poll (the pid
/// record carrying its nonce and `ready`), or its reason for stopping.
pub(super) fn detach(start: &Start) -> u8 {
    let nonce = uuid::Uuid::new_v4().simple().to_string();
    let folder = folder_of(&start.files.state).to_path_buf();
    if let Err(why) = private_folder(&folder) {
        say(&why);
        return EXIT_SETUP;
    }
    let mut child_args = vec!["conductor-watch".to_owned()];
    child_args.extend(start.argv.iter().filter(|a| *a != "--detach").cloned());
    child_args.extend(["--nonce".to_owned(), nonce.clone()]);
    if start.args.owner.is_none() {
        child_args.extend(["--owner".to_owned(), start.owner.clone()]);
    }
    // Absolute: the child does not run from the caller's folder.
    let state =
        std::path::absolute(&start.files.state).unwrap_or_else(|_| start.files.state.clone());
    if start.args.state.is_none() || state != start.files.state {
        child_args.extend(["--state".to_owned(), state.display().to_string()]);
    }
    // The child runs from its own folder, so it does not keep the caller's
    // worktree in use for days; gh then needs the repository named.
    if start.args.last("--repo").is_none() {
        match caller_repository() {
            Ok(Some(repo)) => child_args.extend(["--repo".to_owned(), repo]),
            Ok(None) => {}
            Err(why) => {
                say(&format!(
                    "--detach needs this folder's GitHub repository and gh could not name it \
                     ({why}); pass --repo OWNER/NAME"
                ));
                return EXIT_SETUP;
            }
        }
    }
    let log = &start.files.log;
    let offset = fs::metadata(log).map_or(0, |info| info.len());
    let spawned = (|| {
        let binary = std::env::current_exe()?;
        let handle = fs::OpenOptions::new().create(true).append(true).open(log)?;
        let mut command = Command::new(binary);
        command
            .args(&child_args)
            .current_dir(&folder)
            .stdin(Stdio::null())
            .stdout(Stdio::from(handle.try_clone()?))
            .stderr(Stdio::from(handle));
        crate::lifecycle::detached(&mut command);
        #[cfg(windows)]
        keep_standard_handles();
        command.spawn()
    })();
    let mut child = match spawned {
        Ok(child) => child,
        // A job above this one forbids breakaway: CreateProcess refuses.
        Err(error) if cfg!(windows) && error.raw_os_error() == Some(5) => {
            say(&format!(
                "the watcher could not be started outside this session's Windows job ({error}); \
                 run --detach from a terminal outside this session"
            ));
            return EXIT_SETUP;
        }
        Err(error) => {
            say(&format!(
                "the watcher did not start: its log {} or this program could not be opened ({error}); \
                 pass --state <path to a file in a writable directory>",
                log.display()
            ));
            return EXIT_SETUP;
        }
    };
    let deadline = Instant::now() + DETACH_WAIT;
    loop {
        let record = fs::read(&start.files.pid)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        if let Some(record) = record.filter(|r| r["nonce"] == nonce.as_str() && r["ready"] == true)
        {
            let agent = record["agent_id"].as_str().unwrap_or("?").to_owned();
            print(&format!(
                "conductor-watch {agent} watching for {}; {}",
                start.owner,
                super::park_hint(&agent)
            ));
            if crate::lifecycle::job_ends_detached_children() {
                say(
                    "note: this session runs in a Windows job that ends its processes when it \
                     closes and lets none break away, so the watcher stops when this session \
                     ends; run --detach from a terminal outside the session to keep it",
                );
            }
            return EXIT_OK;
        }
        // Another start won: say whose watcher runs. Our own child's
        // record is not another.
        let other = super::running(&start.files.state)
            .ok()
            .flatten()
            .or_else(|| {
                super::owner_watcher(&start.home, &start.owner)
                    .and_then(|other| super::running(&other).ok().flatten())
            });
        if let Some(other) = other
            && super::pyjson::get(&other, "nonce") != Some(&super::pyjson::J::Str(nonce.clone()))
        {
            let owner_matches = super::pyjson::get(&other, "owner")
                == Some(&super::pyjson::J::Str(start.owner.clone()));
            let out = if owner_matches {
                super::already(&other)
            } else {
                match super::foreign(&other, &start.owner, &start.files.state) {
                    Ok(out) => out,
                    Err(_) => return EXIT_SETUP,
                }
            };
            let _ = std::io::stdout().write_all(&super::super::text_bytes(&out.stdout));
            let _ = std::io::stderr().write_all(&super::super::text_bytes(&out.stderr));
            return out.code;
        }
        if let Ok(Some(status)) = child.try_wait() {
            let said = log_since(log, offset);
            // It found this owner's watcher already running: relay that.
            if let Some(line) = said
                .lines()
                .find(|line| line.contains(" already running for "))
            {
                print(line.trim_end());
                return EXIT_OK;
            }
            // It lost the state's lock to a start racing this one: the
            // winner's record follows, so wait for it.
            if !(status.success() && said.contains("another watcher holds")) {
                let tail = said
                    .trim()
                    .lines()
                    .last()
                    .map_or_else(|| "it wrote nothing".to_owned(), str::to_owned);
                say(&format!(
                    "the watcher did not start ({tail}); its log is {}",
                    log.display()
                ));
                return EXIT_SETUP;
            }
        }
        if Instant::now() >= deadline {
            print(&format!(
                "conductor-watch: still starting after {} s (its first poll has not finished); see {}",
                DETACH_WAIT.as_secs(),
                log.display()
            ));
            return EXIT_OK;
        }
        std::thread::sleep(DETACH_POLL);
    }
}
