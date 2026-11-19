//! Own the launched child and its process group or Windows job.
use std::{io, time::Duration};

use process_wrap::tokio::{ChildWrapper, CommandWrap, KillOnDrop};
use tokio::process::Command;

pub(super) struct Child {
    child: Box<dyn ChildWrapper>,
    signalled: bool,
}

impl Child {
    pub(super) fn spawn(command: Command) -> io::Result<Self> {
        #[cfg(target_os = "linux")]
        let command = {
            let mut command = command;
            parent_death(&mut command);
            command
        };
        let mut command = CommandWrap::from(command);
        command.wrap(KillOnDrop);
        #[cfg(unix)]
        command.wrap(process_wrap::tokio::ProcessGroup::leader());
        #[cfg(windows)]
        command.wrap(process_wrap::tokio::JobObject);
        Ok(Self {
            child: command.spawn()?,
            signalled: false,
        })
    }

    pub(super) fn id(&self) -> Option<u32> {
        self.child.id()
    }

    pub(super) fn exit_code(&mut self) -> io::Result<Option<u8>> {
        // Observe a Unix exit without reaping the leader. Keep its PGID
        // reserved until we have stopped descendants in the owned group.
        #[cfg(unix)]
        {
            use rustix::process::{Pid, WaitId, WaitIdOptions, waitid};
            let pid = self
                .id()
                .and_then(|id| i32::try_from(id).ok())
                .and_then(Pid::from_raw)
                .ok_or_else(|| io::Error::from_raw_os_error(libc::ECHILD))?;
            let info = waitid(
                WaitId::Pid(pid),
                WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
            )?;
            Ok(info.map(|info| {
                exit_byte(
                    info.exit_status()
                        .or_else(|| info.terminating_signal().map(|n| 128 + n)),
                )
            }))
        }
        #[cfg(windows)]
        Ok(self
            .child
            .try_wait()?
            .map(|status| exit_byte(status.code())))
    }

    fn kill(&mut self) -> io::Result<()> {
        if self.signalled {
            return Ok(());
        }
        match self.child.start_kill() {
            Ok(()) => {}
            #[cfg(unix)]
            Err(error) if error.raw_os_error() == Some(libc::ESRCH) => {}
            Err(error) => return Err(error),
        }
        self.signalled = true;
        Ok(())
    }

    pub(super) async fn close(mut self) -> io::Result<()> {
        self.kill()?;
        tokio::time::timeout(Duration::from_secs(5), self.child.wait()).await??;
        Ok(())
    }
}

impl Drop for Child {
    fn drop(&mut self) {
        let _ = self.kill();
    }
}

fn exit_byte(status: Option<i32>) -> u8 {
    status.and_then(|code| u8::try_from(code).ok()).unwrap_or(1)
}

#[cfg(target_os = "linux")]
#[allow(unsafe_code)]
fn parent_death(command: &mut Command) {
    let parent = rustix::process::getpid();
    // SAFETY: this hook only issues process syscalls after fork. Checking
    // the parent closes the race between fork and setting the death signal.
    unsafe {
        command.pre_exec(move || {
            rustix::process::set_parent_process_death_signal(Some(rustix::process::Signal::KILL))?;
            if rustix::process::getppid() != Some(parent) {
                return Err(io::Error::from_raw_os_error(libc::ESRCH));
            }
            Ok(())
        });
    }
}

pub(super) struct Signals {
    #[cfg(unix)]
    interrupt: tokio::signal::unix::Signal,
    #[cfg(unix)]
    terminate: tokio::signal::unix::Signal,
    #[cfg(unix)]
    hangup: tokio::signal::unix::Signal,
    #[cfg(windows)]
    interrupt: tokio::signal::windows::CtrlC,
    #[cfg(windows)]
    terminate: tokio::signal::windows::CtrlBreak,
}

impl Signals {
    pub(super) fn new() -> io::Result<Self> {
        #[cfg(unix)]
        {
            use tokio::signal::unix::{SignalKind, signal};
            Ok(Self {
                interrupt: signal(SignalKind::interrupt())?,
                terminate: signal(SignalKind::terminate())?,
                hangup: signal(SignalKind::hangup())?,
            })
        }
        #[cfg(windows)]
        Ok(Self {
            interrupt: tokio::signal::windows::ctrl_c()?,
            terminate: tokio::signal::windows::ctrl_break()?,
        })
    }

    pub(super) async fn wait(&mut self) {
        #[cfg(unix)]
        tokio::select! {
            _ = self.interrupt.recv() => {}
            _ = self.terminate.recv() => {}
            _ = self.hangup.recv() => {}
        }
        #[cfg(windows)]
        tokio::select! {
            _ = self.interrupt.recv() => {}
            _ = self.terminate.recv() => {}
        }
    }
}
