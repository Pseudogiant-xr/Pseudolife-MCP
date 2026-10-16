use process_wrap::tokio::ChildWrapper;
use rustix::process::{Pid, WaitId, WaitIdOptions, waitid};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

fn check_deadline(deadline: Instant) -> io::Result<()> {
    if Instant::now() >= deadline {
        Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "queue cleanup completion deadline expired",
        ))
    } else {
        Ok(())
    }
}

// Only filesystem work crosses threads. No task owns a child or can signal/reap it.
async fn read_before_deadline<T: Send + 'static>(
    deadline: Instant,
    read: impl FnOnce() -> io::Result<T> + Send + 'static,
) -> io::Result<T> {
    check_deadline(deadline)?;
    let result = tokio::task::spawn_blocking(move || {
        check_deadline(deadline)?;
        let result = read();
        check_deadline(deadline)?;
        result
    })
    .await
    .map_err(|_| io::Error::other("queue cleanup metadata reader failed"))?;
    check_deadline(deadline)?;
    result
}

#[derive(Clone, Copy, Debug)]
struct ProcessStat {
    pid: u32,
    state: u8,
    parent: u32,
    group: u32,
    session: u32,
    started: u64,
}

fn invalid_stat() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid process stat")
}

fn number<T: std::str::FromStr>(bytes: &[u8]) -> io::Result<T> {
    if bytes.is_empty() || !bytes.iter().all(u8::is_ascii_digit) {
        return Err(invalid_stat());
    }
    std::str::from_utf8(bytes)
        .map_err(|_| invalid_stat())?
        .parse()
        .map_err(|_| invalid_stat())
}

fn parse_stat(bytes: &[u8], expected_pid: u32) -> io::Result<ProcessStat> {
    let open = bytes
        .windows(2)
        .position(|part| part == b" (")
        .ok_or_else(invalid_stat)?;
    // comm may contain spaces, parentheses and non-UTF-8 bytes.
    let close = bytes
        .windows(2)
        .rposition(|part| part == b") ")
        .ok_or_else(invalid_stat)?;
    if close < open + 2 || number::<u32>(&bytes[..open])? != expected_pid {
        return Err(invalid_stat());
    }
    let fields = bytes[close + 2..]
        .split(u8::is_ascii_whitespace)
        .filter(|field| !field.is_empty())
        .collect::<Vec<_>>();
    if fields.len() < 20 || fields[0].len() != 1 || !b"RSDZTtXxKWPI".contains(&fields[0][0]) {
        return Err(invalid_stat());
    }
    Ok(ProcessStat {
        pid: expected_pid,
        state: fields[0][0],
        parent: number(fields[1])?,
        group: number(fields[2])?,
        session: number(fields[3])?,
        started: number(fields[19])?,
    })
}

fn read_stat(root: &Path, pid: u32) -> io::Result<Option<ProcessStat>> {
    match fs::read(root.join(pid.to_string()).join("stat")) {
        Ok(bytes) => parse_stat(&bytes, pid).map(Some),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn group_is_terminal_until(
    root: &Path,
    leader: &ProcessStat,
    deadline: Instant,
) -> io::Result<bool> {
    check_deadline(deadline)?;
    let current = read_stat(root, leader.pid)?.ok_or_else(invalid_stat)?;
    check_deadline(deadline)?;
    if current.parent != leader.parent
        || current.group != leader.pid
        || current.session != leader.pid
        || current.started != leader.started
    {
        return Err(io::Error::other("owned queue leader identity changed"));
    }
    check_deadline(deadline)?;
    let mut entries = fs::read_dir(root)?;
    loop {
        check_deadline(deadline)?;
        let entry = entries.next();
        check_deadline(deadline)?;
        let Some(entry) = entry else {
            break;
        };
        let entry = entry?;
        let name = entry.file_name();
        let bytes = std::os::unix::ffi::OsStrExt::as_bytes(name.as_os_str());
        if !bytes.is_empty() && bytes.iter().all(u8::is_ascii_digit) {
            let pid = number(bytes)?;
            check_deadline(deadline)?;
            let stat = read_stat(root, pid)?;
            check_deadline(deadline)?;
            if let Some(stat) = stat
                && stat.group == leader.pid
            {
                if stat.session != leader.pid {
                    return Err(io::Error::other("owned queue group session changed"));
                }
                // Match the lifecycle contract: /proc absence or Z confirms death.
                if stat.state != b'Z' {
                    return Ok(false);
                }
            }
        }
    }
    check_deadline(deadline)?;
    Ok(true)
}

/// Signal only the verified owned session group, fence its death, then reap its leader.
/// Capture the five-second deadline before first polling; reject success after it.
pub(in crate::board::doorbell) fn kill_cancelled_group(
    child: &mut dyn ChildWrapper,
) -> impl std::future::Future<Output = io::Result<()>> {
    kill_cancelled_group_until(
        child,
        PathBuf::from("/proc"),
        Instant::now() + Duration::from_secs(5),
    )
}

async fn kill_cancelled_group_until(
    child: &mut dyn ChildWrapper,
    root: PathBuf,
    deadline: Instant,
) -> io::Result<()> {
    let result = tokio::time::timeout_at(
        tokio::time::Instant::from_std(deadline),
        cleanup_until(child, root, deadline),
    )
    .await
    .map_err(|_| {
        io::Error::new(
            io::ErrorKind::TimedOut,
            "queue cleanup completion deadline expired",
        )
    })?;
    check_deadline(deadline)?;
    result
}

async fn cleanup_until(
    child: &mut dyn ChildWrapper,
    root: PathBuf,
    deadline: Instant,
) -> io::Result<()> {
    check_deadline(deadline)?;
    let raw_pid = child
        .id()
        .ok_or_else(|| io::Error::other("queue leader already reaped"))?;
    let pid = Pid::from_raw(i32::try_from(raw_pid).map_err(|_| invalid_stat())?)
        .ok_or_else(invalid_stat)?;
    // NOWAIT proves ownership without releasing the PID, including an already-dead leader.
    waitid(
        WaitId::Pid(pid),
        WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
    )?;
    let fence = async {
        let metadata_root = root.clone();
        let leader = read_before_deadline(deadline, move || read_stat(&metadata_root, raw_pid))
            .await?
            .ok_or_else(invalid_stat)?;
        if leader.parent != std::process::id()
            || leader.group != raw_pid
            || leader.session != raw_pid
        {
            return Err(io::Error::other(
                "queue leader is not the owned session group",
            ));
        }
        check_deadline(deadline)?;
        child.start_kill()?;
        // Do not poll any wait/try_wait method here: the unreaped leader pins the PGID.
        // process-wrap's waitpid(-pgid) cannot wait for a reparented grandchild.
        loop {
            let metadata_root = root.clone();
            if read_before_deadline(deadline, move || {
                group_is_terminal_until(&metadata_root, &leader, deadline)
            })
            .await?
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        Ok(())
    }
    .await;
    // On an uncertain /proc result, kill only our direct child; never claim group completion.
    check_deadline(deadline)?;
    if fence.is_err() {
        let _ = child.inner_mut().start_kill();
    }
    let reaped = wait_child_before_deadline(child.inner_mut(), deadline).await;
    check_deadline(deadline)?;
    fence.and(reaped)
}

async fn wait_child_before_deadline(
    child: &mut dyn ChildWrapper,
    deadline: Instant,
) -> io::Result<()> {
    check_deadline(deadline)?;
    let mut wait = child.wait();
    std::future::poll_fn(|cx| {
        // Timeout polls its inner future first, so every resumed reap poll needs
        // its own cutoff check before it can release the owned leader identity.
        if let Err(error) = check_deadline(deadline) {
            return std::task::Poll::Ready(Err(error));
        }
        wait.as_mut().poll(cx).map(|status| status.map(|_| ()))
    })
    .await
}

#[cfg(test)]
fn group_is_terminal(root: &Path, leader: &ProcessStat) -> io::Result<bool> {
    group_is_terminal_until(root, leader, Instant::now() + Duration::from_secs(5))
}

#[cfg(test)]
#[path = "doorbell_linux_cleanup_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "doorbell_linux_cleanup_deadline_tests.rs"]
mod deadline_tests;

#[cfg(test)]
#[path = "doorbell_linux_cleanup_wait_deadline_tests.rs"]
mod wait_deadline_tests;
