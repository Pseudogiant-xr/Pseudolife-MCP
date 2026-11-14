//! Starting a watcher: in the foreground (the lock, the board, gh, the
//! loop) or `--detach`ed (the same binary started on its own, then waited
//! for until its pid record names it).
use super::{
    args::Args,
    sources::{self, GhProgram},
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
/// How long `--detach` waits for the watcher to register and write its
/// pid record.
const DETACH_WAIT: Duration = Duration::from_secs(15);
const DETACH_POLL: Duration = Duration::from_millis(200);
/// How long a child that exited 0 is given for the winning watcher's record
/// to appear, when two starts raced for the state's lock.
const LOST_RACE_GRACE: Duration = Duration::from_secs(2);

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
    let line = super::super::text_bytes(&format!("conductor-watch: {text}
"));
    let _ = std::io::stderr().write_all(&line);
}

/// One line on stdout, flushed: a detached watcher's stdout is its log.
fn print(text: &str) {
    let mut out = std::io::stdout();
    let _ = out.write_all(&super::super::text_bytes(&format!("{text}
")));
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
            DEFAULT_LEASES.iter().map(|name| (*name).to_owned()).collect()
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

/// The watch in this process: one per state, held by its lock for life.
pub(super) fn foreground(start: &Start) -> u8 {
    let settings = settings(start);
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
    let code = watch_here(&settings);
    lock.release();
    code
}

fn watch_here(settings: &Settings) -> u8 {
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread().enable_all().build() else {
        say("could not start the async runtime");
        return EXIT_SETUP;
    };
    runtime.block_on(async {
        let board = match crate::cli::lease::board::connect(false, &Default::default()) {
            Ok(board) => board,
            Err(failure) => {
                say(&format!("no board: {}", failure.text));
                append_ledger(&settings.files.ledger, "setup", &format!("no board: {}", failure.text));
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
            program: sources::find_program(
                "gh",
                std::env::var_os("PATH"),
                std::env::var_os("PATHEXT"),
            ),
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

/// The watcher for `start`, started on its own; then the pid record that
/// names it (by the nonce it was given).
pub(super) fn detach(start: &Start) -> u8 {
    let nonce = uuid::Uuid::new_v4().simple().to_string();
    let mut child_args = vec!["conductor-watch".to_owned()];
    child_args.extend(start.argv.iter().filter(|a| *a != "--detach").cloned());
    child_args.extend(["--nonce".to_owned(), nonce.clone()]);
    if start.args.owner.is_none() {
        child_args.extend(["--owner".to_owned(), start.owner.clone()]);
    }
    if start.args.state.is_none() {
        child_args.extend(["--state".to_owned(), start.files.state.display().to_string()]);
    }
    let log = &start.files.log;
    let spawned = (|| {
        let binary = std::env::current_exe()?;
        if let Some(parent) = log.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent)?;
        }
        let handle = fs::OpenOptions::new().create(true).append(true).open(log)?;
        let mut command = Command::new(binary);
        command
            .args(&child_args)
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
    let mut exited: Option<Instant> = None;
    loop {
        let record = fs::read(&start.files.pid)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        if let Some(record) = record.filter(|r| r["nonce"] == nonce.as_str()) {
            let agent = record["agent_id"].as_str().unwrap_or("?").to_owned();
            print(&format!(
                "conductor-watch {agent} watching for {}; {}",
                start.owner,
                super::park_hint(&agent)
            ));
            return EXIT_OK;
        }
        // Another start won the state's lock: say whose watcher runs. Our
        // own child's record, written since the read above, is not another.
        if let Ok(Some(other)) = super::running(&start.files.state)
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
        match child.try_wait() {
            // A child that lost the state's lock exits 0 ("another watcher
            // holds it"): the winner's record follows, so wait a little.
            Ok(Some(status)) if status.success() => {
                let since = *exited.get_or_insert_with(Instant::now);
                if since.elapsed() >= LOST_RACE_GRACE {
                    break;
                }
            }
            Ok(Some(_)) => break,
            _ => {}
        }
        if Instant::now() >= deadline {
            print(&format!("conductor-watch: still starting; see {}", log.display()));
            return EXIT_OK;
        }
        std::thread::sleep(DETACH_POLL);
    }
    let tail = fs::read_to_string(log)
        .ok()
        .and_then(|text| text.trim().lines().last().map(str::to_owned))
        .unwrap_or_else(|| "no output".into());
    say(&format!(
        "the watcher did not start ({tail}); its log is {}",
        log.display()
    ));
    EXIT_SETUP
}
