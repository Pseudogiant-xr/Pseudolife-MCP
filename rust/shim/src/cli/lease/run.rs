use super::{
    args::Args,
    board::{self, Board, Renewer},
    lock::{self, Lock},
    repr, say, view,
};
use std::{io, sync::Arc, time::Duration};
use tokio::{
    process::{Child, Command},
    time::Instant,
};

#[cfg(unix)]
#[path = "sigint.rs"]
mod sigint;

enum RunError {
    RefusedInput(String),
    TimedOut,
    Lock(String),
    CannotRun(i32, String),
}
enum BoardWaitError {
    Failure(board::Failure),
    TimedOut,
}
struct Stops {
    #[cfg(unix)]
    interrupt: Option<tokio::signal::unix::Signal>,
    #[cfg(unix)]
    terminate: tokio::signal::unix::Signal,
    #[cfg(unix)]
    hangup: tokio::signal::unix::Signal,
}
impl Stops {
    fn new() -> io::Result<Self> {
        Ok(Self {
            #[cfg(unix)]
            interrupt: if sigint::ignored()? {
                None
            } else {
                Some(tokio::signal::unix::signal(
                    tokio::signal::unix::SignalKind::interrupt(),
                )?)
            },
            #[cfg(unix)]
            terminate: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?,
            #[cfg(unix)]
            hangup: tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?,
        })
    }
    async fn wait(&mut self) -> i32 {
        #[cfg(unix)]
        {
            let interrupt = async {
                match &mut self.interrupt {
                    Some(signal) => {
                        let _ = signal.recv().await;
                    }
                    None => std::future::pending::<()>().await,
                }
            };
            tokio::select! {_=interrupt=>2,_=self.terminate.recv()=>15,_=self.hangup.recv()=>1}
        }
        #[cfg(windows)]
        {
            let _ = tokio::signal::ctrl_c().await;
            2
        }
    }
}
#[derive(Clone, Copy)]
struct Deadline {
    start: Instant,
    seconds: f64,
}
async fn pause(poll: u64, deadline: Option<Deadline>) -> Result<(), RunError> {
    let delay = if let Some(end) = deadline {
        let remaining = end.seconds - end.start.elapsed().as_secs_f64();
        if remaining <= 0.0 {
            return Err(RunError::TimedOut);
        }
        Duration::from_secs_f64((poll as f64).min(remaining))
    } else {
        Duration::from_secs(poll)
    };
    tokio::time::sleep(delay).await;
    Ok(())
}
fn task(args: &Args) -> String {
    let program = args.command[0]
        .rsplit(if cfg!(windows) {
            &['/', '\\'][..]
        } else {
            &['/'][..]
        })
        .next()
        .filter(|v| !v.is_empty())
        .unwrap_or(&args.command[0]);
    view::printable(&format!(
        "{}: {program}",
        args.name.as_deref().unwrap_or("")
    ))
}
async fn wait_board(
    board: &Board,
    args: &Args,
    deadline: Option<Deadline>,
) -> Result<(), BoardWaitError> {
    let project = view::printable(&std::env::var("PSEUDOLIFE_AGENT_PROJECT").unwrap_or_default());
    let mut failing = None;
    let mut notice = Instant::now();
    loop {
        let result = async {
            board.register(&task(args), &project).await?;
            board.lease(args).await
        }
        .await;
        match result {
            Ok(reply) => {
                failing = None;
                if reply["state"] == "held" {
                    return Ok(());
                }
                if Instant::now() >= notice {
                    say(&view::queued_notice(
                        args.name.as_deref().unwrap_or(""),
                        &reply,
                    ));
                    notice = Instant::now() + Duration::from_secs(60);
                }
            }
            Err(e) if !e.transient => return Err(BoardWaitError::Failure(e)),
            Err(e) => {
                let start = *failing.get_or_insert_with(Instant::now);
                if start.elapsed() >= Duration::from_secs(120) {
                    return Err(BoardWaitError::Failure(board::Failure {
                        text: format!(
                            "the board kept failing for {} ({})",
                            view::span(start.elapsed().as_secs_f64()),
                            e.text
                        ),
                        transient: false,
                        fatal_input: false,
                    }));
                }
            }
        }
        if pause(5, deadline).await.is_err() {
            return Err(BoardWaitError::TimedOut);
        }
    }
}
fn try_lock(lock: &mut Lock) -> Result<bool, RunError> {
    lock.acquire().map_err(|e| {
        RunError::Lock(format!(
            "lease: cannot use the lock file {}: {e}",
            lock.path.display()
        ))
    })
}
async fn wait_lock(
    lock: &mut Lock,
    name: &str,
    deadline: Option<Deadline>,
    board_holds: bool,
) -> Result<(), RunError> {
    if try_lock(lock)? {
        return Ok(());
    }
    let mut notice = Instant::now();
    loop {
        if Instant::now() >= notice {
            say(&if board_holds {
                format!(
                    "lease: the board granted {}, but its local lock ({}) is held by a process the board does not show (a --no-board run, or a holder whose board lease lapsed); waiting for it",
                    repr(name),
                    lock.path.display()
                )
            } else {
                format!(
                    "lease: waiting for the local lock on {} ({}), which another process holds",
                    repr(name),
                    lock.path.display()
                )
            });
            notice = Instant::now() + Duration::from_secs(60);
        }
        pause(2, deadline).await?;
        if try_lock(lock)? {
            return Ok(());
        }
    }
}
fn start(args: &Args) -> Result<Child, RunError> {
    let original = &args.command[0];
    #[cfg(windows)]
    let program = crate::lifecycle::find_executable(original)
        .ok()
        .map(|p| p.into_os_string())
        .unwrap_or_else(|| original.into());
    #[cfg(unix)]
    let program = std::ffi::OsString::from(original);
    let held = std::env::var("PSEUDOLIFE_LEASES_HELD").unwrap_or_default();
    let name = args.name.as_deref().unwrap_or("");
    let child = Command::new(program)
        .args(&args.command[1..])
        .env(
            "PSEUDOLIFE_LEASES_HELD",
            if held.is_empty() {
                name.into()
            } else {
                format!("{held},{name}")
            },
        )
        .spawn();
    child.map_err(|e| {
        if e.kind() == io::ErrorKind::NotFound {
            RunError::CannotRun(127, format!("lease: command not found: {original}"))
        } else {
            let text = e.to_string();
            let suffix = format!(" (os error {})", e.raw_os_error().unwrap_or(0));
            RunError::CannotRun(
                126,
                format!(
                    "lease: cannot run {original}: {}",
                    text.strip_suffix(&suffix)
                        .unwrap_or(&text)
                        .trim_end_matches('.')
                ),
            )
        }
    })
}
fn exit_status(status: std::process::ExitStatus) -> i32 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        status
            .code()
            .unwrap_or_else(|| 128 + status.signal().unwrap_or(1))
    }
    #[cfg(windows)]
    {
        status.code().unwrap_or(1)
    }
}
#[cfg(unix)]
fn send_signal(child: &mut Child, signal: i32) {
    use rustix::process::{Pid, Signal, kill_process};
    let signal = match signal {
        1 => Signal::HUP,
        2 => Signal::INT,
        15 => Signal::TERM,
        _ => return,
    };
    if let Some(pid) = child
        .id()
        .and_then(|pid| i32::try_from(pid).ok())
        .and_then(Pid::from_raw)
    {
        let _ = kill_process(pid, signal);
    }
}
#[cfg(windows)]
fn send_signal(child: &mut Child, _signal: i32) {
    let _ = child.start_kill();
}
async fn stop_child(child: &mut Child, signal: i32, stops: &mut Stops) {
    let mut steps = vec![];
    if signal == 2 {
        steps.push(None);
        #[cfg(unix)]
        steps.push(Some(2));
    } else {
        steps.push(Some(signal));
    }
    if !steps.contains(&Some(15)) {
        steps.push(Some(15));
    }
    for step in steps {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        if let Some(s) = step {
            send_signal(child, s);
        }
        tokio::select! {_ = child.wait()=>return,_ = tokio::time::sleep(Duration::from_secs(10))=>(),_ = stops.wait()=>break}
    }
    let _ = child.kill().await;
    let _ = child.wait().await;
}
async fn work(
    args: Arc<Args>,
    lock: &mut Lock,
    board: &mut Option<Arc<Board>>,
    renewer: &mut Option<Renewer>,
    child: &mut Option<Child>,
) -> Result<i32, RunError> {
    // An elapsed-seconds deadline retains large timeout bounds without trying
    // to construct an Instant outside the platform's representable range.
    let deadline = args.timeout.map(|seconds| Deadline {
        start: Instant::now(),
        seconds,
    });
    let mut skipped = None;
    match board::connect(args.no_board) {
        Err(e) if e.fatal_input => return Err(RunError::RefusedInput(e.text)),
        Err(reason) => skipped = Some(reason.text),
        Ok(client) => {
            *board = Some(client.clone());
            match wait_board(&client, &args, deadline).await {
                Ok(()) => *renewer = Some(Renewer::start(client, args.clone())),
                Err(BoardWaitError::Failure(e)) if e.fatal_input => {
                    return Err(RunError::RefusedInput(e.text));
                }
                Err(BoardWaitError::TimedOut) => {
                    return Err(RunError::TimedOut);
                }
                Err(BoardWaitError::Failure(e)) => skipped = Some(e.text),
            }
        }
    }
    let name = args.name.as_deref().unwrap_or("");
    if renewer.is_none() {
        if let Some(board) = board {
            board.release(name).await;
        }
        say(&format!(
            "lease: board skipped: {}; waiting on the local lock for {} alone (not FIFO)",
            skipped.unwrap_or_else(|| "None".into()),
            repr(name)
        ));
    }
    wait_lock(lock, name, deadline, renewer.is_some()).await?;
    *child = Some(start(&args)?);
    let status = child
        .as_mut()
        .expect("started child")
        .wait()
        .await
        .map_err(|e| {
            RunError::CannotRun(126, format!("lease: cannot run {}: {e}", args.command[0]))
        })?;
    Ok(exit_status(status))
}
pub async fn run(args: Args) -> i32 {
    let name = args.name.as_deref().unwrap_or("").to_owned();
    let mut lock = Lock::new(lock::directory().join(lock::file_name(&name)));
    let held = std::env::var("PSEUDOLIFE_LEASES_HELD").unwrap_or_default();
    if format!(",{held},").contains(&format!(",{name},"))
        && lock::probe(&lock.path).ok().flatten() == Some(true)
    {
        say(&format!(
            "lease: {} is held by an enclosing lease run (PSEUDOLIFE_LEASES_HELD={}); a nested run of the same lease would wait for itself forever. Run the command directly, or use another name. If this process was not started by that run, unset PSEUDOLIFE_LEASES_HELD.",
            repr(&name),
            view::clean_text(&held, 240)
        ));
        return 64;
    }
    // Invalid timeout bounds fail after the enclosing-run guard and before
    // connecting, acquiring a lock or starting a child.
    if args.timeout.is_some_and(|seconds| !seconds.is_finite()) {
        say("lease: timeout is out of range");
        return 1;
    }
    let mut stops = match Stops::new() {
        Ok(s) => s,
        Err(_) => {
            say("lease: cannot install stop handlers");
            return 71;
        }
    };
    let args = Arc::new(args);
    let mut board = None;
    let mut renewer = None;
    let mut child = None;
    let result = tokio::select! {
        result=work(args.clone(),&mut lock,&mut board,&mut renewer,&mut child)=>result,
        signal=stops.wait()=>{
            if let Some(child)=&mut child{stop_child(child,signal,&mut stops).await;}
            let what=match signal{2=>"interrupted",15=>"stopped by SIGTERM",_=>"stopped by SIGHUP"};say(&format!("lease: {what}; releasing {}",repr(&name)));Ok(128+signal)
        }
    };
    let code = match result {
        Err(RunError::RefusedInput(message)) => {
            say(&format!("lease: {message}"));
            1
        }
        Ok(code) => code,
        Err(RunError::TimedOut) => {
            say(&format!(
                "lease: gave up waiting for {} after {} (--timeout); the command did not run",
                repr(&name),
                view::span(args.timeout.unwrap_or(0.0))
            ));
            75
        }
        Err(RunError::Lock(message)) => {
            say(&message);
            71
        }
        Err(RunError::CannotRun(code, message)) => {
            say(&message);
            code
        }
    };
    lock.release();
    if let Some(renewer) = renewer {
        renewer.stop().await;
    }
    if let Some(board) = board {
        board.release(&name).await;
    }
    code
}
