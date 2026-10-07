use super::{
    args::Args,
    board::{self, Board, Failure},
    json::Value,
    lock::{self, Lock},
    repr,
    run::{Deadline, RunError, Stops, wait_lock},
    say, view,
};
use std::{path::Path, sync::Arc, time::Duration};
use tokio::{sync::watch, time::Instant};

/// Probe another process without signalling or acquiring ownership of it.
#[cfg_attr(windows, allow(unsafe_code))]
fn pid_alive(pid: u32) -> bool {
    #[cfg(windows)]
    {
        use windows_sys::Win32::{
            Foundation::{CloseHandle, ERROR_ACCESS_DENIED, GetLastError, WAIT_TIMEOUT},
            System::Threading::{
                OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
                WaitForSingleObject,
            },
        };
        // Only query and synchronize rights: opening/closing this handle never
        // terminates the process (unlike a Windows signal-zero emulation).
        unsafe {
            let handle = OpenProcess(
                PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                0,
                pid,
            );
            if handle.is_null() {
                return GetLastError() == ERROR_ACCESS_DENIED;
            }
            let alive = WaitForSingleObject(handle, 0) == WAIT_TIMEOUT;
            CloseHandle(handle);
            alive
        }
    }
    #[cfg(unix)]
    {
        let Some(pid_value) = i32::try_from(pid)
            .ok()
            .and_then(rustix::process::Pid::from_raw)
        else {
            return false;
        };
        match rustix::process::test_kill_process(pid_value) {
            Err(rustix::io::Errno::SRCH) => return false,
            Err(rustix::io::Errno::PERM) => return true,
            _ => (),
        }
        // A Linux zombie has exited even while its parent has not reaped it.
        std::fs::read_to_string(format!("/proc/{pid}/stat"))
            .ok()
            .and_then(|stat| {
                stat.rsplit_once(')')
                    .map(|(_, tail)| tail.trim_start().starts_with('Z'))
            })
            != Some(true)
    }
}
fn park_field<'a>(agent: &'a Value, field: &str) -> &'a Value {
    let flat = &agent[format!("park_{field}")];
    if flat.is_null() {
        &agent["park"][field]
    } else {
        flat
    }
}
fn concerned(agent: &Value, name: &str, project: &str, now: f64) -> bool {
    if agent["lifecycle"] == "revoked"
        || (!project.is_empty()
            && agent["project"].as_str().unwrap_or("").to_lowercase() != project.to_lowercase())
    {
        return false;
    }
    if !park_field(agent, "reason").is_null()
        && !park_field(agent, "expires")
            .as_f64()
            .is_some_and(|expiry| expiry <= now)
    {
        let clear = park_field(agent, "clear_by");
        if clear.as_str() == Some(name)
            || clear
                .as_array()
                .is_some_and(|items| items.iter().any(|item| item.as_str() == Some(name)))
        {
            return true;
        }
    }
    if !agent["lifecycle"].is_null()
        && !matches!(agent["lifecycle"].as_str(), Some("attached" | "registered"))
    {
        return false;
    }
    let status = agent["status"].as_str().unwrap_or("");
    for (start, _) in status
        .char_indices()
        .filter(|(_, c)| *c == 's' || *c == 'g')
    {
        if status[..start]
            .chars()
            .next_back()
            .is_some_and(|c| c.is_alphanumeric() || c == '_')
        {
            continue;
        }
        let tail = &status[start..];
        if tail.starts_with("gpu=") {
            return true;
        }
        for token in ["suite=running", "suite=queued"] {
            if let Some(rest) = tail.strip_prefix(token)
                && !rest
                    .chars()
                    .next()
                    .is_some_and(|c| c.is_alphanumeric() || c == '_')
            {
                return true;
            }
        }
    }
    false
}
struct Notice {
    name: String,
    project: String,
    worktree: String,
    pid: u32,
    expect: Option<u64>,
    held_since: f64,
}
impl Notice {
    fn text(&self, event: &str) -> String {
        let tail = if event == "acquired" {
            self.expect.map_or_else(
                || ", no expected end given".into(),
                |expect| {
                    format!(
                        ", expected end {} (in {})",
                        view::clock(self.held_since + expect as f64),
                        view::span(expect as f64)
                    )
                },
            )
        } else {
            format!(", held {}", view::span(view::now() - self.held_since))
        };
        format!(
            "LEASE {} {event}: pid {}, worktree {}{tail}. The OS lock is the truth; `pseudolife-mcp lease check {}` shows it. Automatic notice, no reply needed.",
            self.name, self.pid, self.worktree, self.name
        )
    }
    async fn announce(
        &self,
        board: &Board,
        event: &str,
        deadline: Instant,
        stopped: &watch::Receiver<Option<Instant>>,
    ) {
        let Some(own) = board.agent_id().await else {
            return;
        };
        if Instant::now() >= deadline {
            return;
        }
        let peers = match tokio::time::timeout_at(deadline, board.agents()).await {
            Ok(Ok(peers)) => peers,
            result => {
                let why = match result {
                    Ok(Err(failure)) => failure.text,
                    _ => "response timed out".into(),
                };
                say(&format!(
                    "lease: could not list the peers to tell about {} ({})",
                    repr(&self.name),
                    view::clean_text(&why, 240)
                ));
                return;
            }
        };
        let now = view::now();
        let text = self.text(event);
        let safe: String = self
            .name
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || "._-".contains(c) {
                    c
                } else {
                    '_'
                }
            })
            .take(40)
            .collect();
        let mut failed = Vec::new();
        for agent in peers
            .iter()
            .filter(|agent| {
                agent["agent_id"] != own && concerned(agent, &self.name, &self.project, now)
            })
            .take(20)
        {
            if Instant::now() >= deadline || (event == "acquired" && stopped.borrow().is_some()) {
                break;
            }
            let to = agent["agent_id"].as_str().expect("admitted peer address");
            let prefix: String = to.chars().take(8).collect();
            let request_id: String = format!(
                "lease-{event}-{safe}-{}-{}-{prefix}",
                self.pid, self.held_since as u64
            )
            .chars()
            .take(120)
            .collect();
            let sent = tokio::time::timeout_at(deadline, board.send(to, &text, &request_id)).await;
            let why = match sent {
                Ok(Ok(())) => continue,
                Ok(Err(failure)) => failure.text,
                Err(_) => "response timed out".into(),
            };
            failed.push(format!(
                "{} ({})",
                view::clean_text(&prefix, 120),
                view::clean_text(&why, 80)
            ));
        }
        if !failed.is_empty() {
            say(&format!(
                "lease: could not tell {} peer(s) about {}: {}",
                failed.len(),
                repr(&self.name),
                failed.join("; ")
            ));
        }
    }
}
struct Mirror {
    stop: watch::Sender<Option<Instant>>,
    task: tokio::task::JoinHandle<()>,
}
impl Mirror {
    fn start(connection: Result<Arc<Board>, Failure>, args: Arc<Args>, directory: &Path) -> Self {
        let label = lock::instance_id(directory).map_or_else(
            || "lease-hold".into(),
            |stamp| format!("lease-hold@{stamp}"),
        );
        let worktree = if args.worktree.is_empty() {
            std::env::current_dir().unwrap_or_default()
        } else {
            args.worktree.clone().into()
        };
        let notice = Notice {
            name: args.name.clone().unwrap_or_default(),
            project: view::printable(
                &std::env::var("PSEUDOLIFE_AGENT_PROJECT").unwrap_or_default(),
            ),
            worktree: match worktree.components().next_back() {
                Some(std::path::Component::Normal(name)) => name.to_string_lossy().into_owned(),
                Some(std::path::Component::ParentDir) => "..".into(),
                _ => args.worktree.clone(),
            },
            pid: args.while_pid.expect("parsed process id"),
            expect: args.expect,
            held_since: view::now(),
        };
        let (stop, mut stopped) = watch::channel(None);
        let task = tokio::spawn(async move {
            let board = match connection {
                Ok(board) => board,
                Err(failure) => {
                    say(&format!(
                        "lease: board skipped: {}; {} is held by the local lock alone",
                        failure.text,
                        repr(&notice.name)
                    ));
                    return;
                }
            };
            let interval = Duration::from_secs_f64(args.ttl as f64 / 3.0);
            let mut failing_since = None;
            let mut warned = false;
            let mut announced = false;
            let mut first = true;
            let mut usable = true;
            loop {
                if stopped.borrow().is_some() {
                    break;
                }
                let call = async {
                    for retry in 0..2 {
                        let task: String = format!("{}: pid {}", notice.name, notice.pid)
                            .chars()
                            .take(120)
                            .collect();
                        let status: String = format!(
                            "lease:{} pid {} {}",
                            notice.name, notice.pid, notice.worktree
                        )
                        .chars()
                        .map(|c| if c.is_control() { '?' } else { c })
                        .take(240)
                        .collect();
                        let reply = async {
                            board
                                .register_as(&task, &notice.project, &label, &status)
                                .await?;
                            board.lease(&args).await
                        }
                        .await;
                        if retry == 0
                            && reply.as_ref().is_err_and(|failure| {
                                matches!(
                                    failure.code.as_deref(),
                                    Some("instance_not_found" | "invalid_credential")
                                )
                            })
                            && board.agent_id().await.is_some()
                        {
                            board.forget().await;
                            continue;
                        }
                        return reply;
                    }
                    unreachable!("two registration attempts")
                };
                let result = tokio::select! { _=stopped.changed()=>break, result=call=>result };
                let mut delay = interval;
                match result {
                    Ok(reply) => {
                        failing_since = None;
                        if reply["state"] != "held" && !warned {
                            warned = true;
                            if first {
                                let now = view::now();
                                let who = if reply["holder"].is_object() {
                                    view::holder(&reply["holder"], now)
                                        + &view::expected(
                                            &reply["holder"]["expected_end"],
                                            now,
                                            false,
                                        )
                                } else {
                                    "nobody the board names".into()
                                };
                                say(&format!(
                                    "lease: the board shows {} held by {who}; this process holds the OS lock, which is the truth, and carries on; the board lease follows when that hold frees or expires",
                                    repr(&notice.name)
                                ));
                            } else {
                                say(&format!(
                                    "lease: warning: the board no longer shows this process holding {}; the local lock, which is what excludes, is still held",
                                    repr(&notice.name)
                                ));
                            }
                        }
                    }
                    Err(failure) if failure.transient => {
                        let since = *failing_since.get_or_insert_with(Instant::now);
                        if since.elapsed() >= Duration::from_secs(120) {
                            say(&format!(
                                "lease: board skipped: the board kept failing for {} ({}); {} is held by the local lock alone",
                                view::span(since.elapsed().as_secs_f64()),
                                failure.text,
                                repr(&notice.name)
                            ));
                            usable = false;
                            break;
                        }
                        if !first {
                            delay = delay.min(Duration::from_secs(5));
                        }
                    }
                    Err(failure) => {
                        say(&format!(
                            "lease: board skipped: {}; {} is held by the local lock alone",
                            failure.text,
                            repr(&notice.name)
                        ));
                        usable = false;
                        break;
                    }
                }
                if first && stopped.borrow().is_none() {
                    notice
                        .announce(
                            &board,
                            "acquired",
                            Instant::now() + Duration::from_secs(30),
                            &stopped,
                        )
                        .await;
                    announced = true;
                }
                first = false;
                tokio::select! { _=stopped.changed()=>break, _=tokio::time::sleep(delay)=>() }
            }
            // The caller has released the local lock before waking this task.
            if !usable {
                return;
            }
            let deadline = stopped
                .borrow()
                .unwrap_or_else(|| Instant::now() + Duration::from_secs(20));
            if Instant::now() < deadline {
                let _ = tokio::time::timeout_at(deadline, board.release(&notice.name)).await;
            }
            if announced {
                notice
                    .announce(&board, "released", deadline, &stopped)
                    .await;
            }
        });
        Self { stop, task }
    }
    async fn stop(mut self) {
        let _ = self
            .stop
            .send(Some(Instant::now() + Duration::from_secs(20)));
        if tokio::time::timeout(Duration::from_secs(21), &mut self.task)
            .await
            .is_err()
        {
            self.task.abort();
            let _ = self.task.await;
        }
    }
}
pub async fn hold(args: Args) -> i32 {
    let pid = args.while_pid.expect("parsed process id");
    let name = args.name.clone().unwrap_or_default();
    if !pid_alive(pid) {
        say(&format!(
            "lease: pid {pid} is already gone; nothing to hold {} for",
            repr(&name)
        ));
        return 0;
    }
    if args.timeout.is_some_and(|seconds| !seconds.is_finite()) {
        say("lease: timeout is out of range");
        return 1;
    }
    let deadline = args.timeout.map(|seconds| Deadline {
        start: Instant::now(),
        seconds,
    });
    let mut stops = match Stops::new() {
        Ok(stops) => stops,
        Err(_) => {
            say("lease: cannot install stop handlers");
            return 71;
        }
    };
    let directory = lock::directory();
    let mut lock = Lock::new(directory.join(lock::file_name(&name)));
    let args = Arc::new(args);
    let mut mirror = None;
    let work = async {
        wait_lock(&mut lock, &name, deadline, false).await?;
        let connection = board::connect_hold(args.no_board);
        mirror = Some(Mirror::start(connection, args.clone(), &directory));
        while pid_alive(pid) {
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        Ok(0)
    };
    let result = tokio::select! { result=work=>result, signal=stops.wait()=>{
        let what=match signal { 2=>"interrupted", 15=>"stopped by SIGTERM", _=>"stopped by SIGHUP" };
        say(&format!("lease: {what}; releasing {} (pid {pid} is left running)", repr(&name)));
        Ok(128+signal)
    } };
    // Never signal PID. Local exclusion ends before bounded board cleanup.
    lock.release();
    if let Some(mirror) = mirror {
        mirror.stop().await;
    }
    match result {
        Ok(code) => code,
        Err(RunError::TimedOut) => {
            say(&format!(
                "lease: gave up waiting for {} after {} (--timeout); nothing is held",
                repr(&name),
                view::span(args.timeout.unwrap_or(0.0))
            ));
            75
        }
        Err(RunError::Lock(message)) => {
            say(&message);
            71
        }
        Err(RunError::RefusedInput(message)) => {
            say(&format!("lease: {message}"));
            1
        }
        Err(RunError::CannotRun(_, _)) => unreachable!("hold never starts a child"),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn peer_selection_keeps_live_status_and_unexpired_clearers_only() {
        use super::super::json::json;
        for status in [
            "suite=running",
            "suite=queued",
            "gpu=idle",
            "x suite=running.",
        ] {
            assert!(concerned(
                &json!({"project":"PROJECT","status":status}),
                "resource",
                "project",
                100.0
            ));
        }
        for status in [
            "xsuite=running",
            "suite=running_extra",
            "suite=done",
            "agpu=busy",
        ] {
            assert!(!concerned(&json!({"status":status}), "resource", "", 100.0));
        }
        let mut parked = json!({"project":"project","lifecycle":"detached","park":{"reason":"needs_resource","clear_by":["resource"],"expires":101}});
        assert!(concerned(&parked, "resource", "project", 100.0));
        parked["park"]["expires"] = json!(100);
        assert!(!concerned(&parked, "resource", "project", 100.0));
        parked["lifecycle"] = json!("revoked");
        parked["park"]["expires"] = json!(101);
        assert!(!concerned(&parked, "resource", "project", 100.0));
    }
    #[test]
    fn probe_current_process_is_read_only() {
        assert!(pid_alive(std::process::id()));
        assert!(pid_alive(std::process::id()));
    }
}
