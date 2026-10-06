use process_wrap::tokio::ChildWrapper;
use rustix::process::{Pid, WaitId, WaitIdOptions, waitid};
use std::{fs, io, path::Path, time::Duration};

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

fn group_is_terminal(root: &Path, leader: &ProcessStat) -> io::Result<bool> {
    let current = read_stat(root, leader.pid)?.ok_or_else(invalid_stat)?;
    if current.parent != leader.parent
        || current.group != leader.pid
        || current.session != leader.pid
        || current.started != leader.started
    {
        return Err(io::Error::other("owned queue leader identity changed"));
    }
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry.file_name();
        let bytes = std::os::unix::ffi::OsStrExt::as_bytes(name.as_os_str());
        if !bytes.is_empty() && bytes.iter().all(u8::is_ascii_digit) {
            let pid = number(bytes)?;
            if let Some(stat) = read_stat(root, pid)?
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
    Ok(true)
}

/// Signal only the verified owned session group, fence its death, then reap its leader.
/// The caller supplies the existing five-second deadline for the whole operation.
pub(in crate::board::doorbell) async fn kill_cancelled_group(
    child: &mut dyn ChildWrapper,
) -> io::Result<()> {
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
    let root = Path::new("/proc");
    let fence = async {
        let leader = read_stat(root, raw_pid)?.ok_or_else(invalid_stat)?;
        if leader.parent != std::process::id()
            || leader.group != raw_pid
            || leader.session != raw_pid
        {
            return Err(io::Error::other(
                "queue leader is not the owned session group",
            ));
        }
        child.start_kill()?;
        // Do not poll any wait/try_wait method here: the unreaped leader pins the PGID.
        // process-wrap's waitpid(-pgid) cannot wait for a reparented grandchild.
        while !group_is_terminal(root, &leader)? {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        Ok(())
    }
    .await;
    // On an uncertain /proc result, kill only our direct child; never claim group completion.
    if fence.is_err() {
        let _ = child.inner_mut().start_kill();
    }
    let reaped = child.inner_mut().wait().await.map(|_| ());
    fence.and(reaped)
}

#[cfg(test)]
#[path = "doorbell_linux_cleanup_tests.rs"]
mod tests;
