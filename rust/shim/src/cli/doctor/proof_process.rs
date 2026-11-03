//! Process and signal ownership for one disposable proof invocation.
use process_wrap::tokio::{ChildWrapper, CommandWrap, KillOnDrop};
use std::io;
use tokio::process::Command;

/// Keep these receivers alive until all fixture cleanup has finished: Tokio's
/// Windows handler defers to the default handler when the last receiver drops.
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
    #[cfg(windows)]
    close: tokio::signal::windows::CtrlClose,
    #[cfg(windows)]
    logoff: tokio::signal::windows::CtrlLogoff,
    #[cfg(windows)]
    shutdown: tokio::signal::windows::CtrlShutdown,
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
        {
            use tokio::signal::windows;
            Ok(Self {
                interrupt: windows::ctrl_c()?,
                terminate: windows::ctrl_break()?,
                close: windows::ctrl_close()?,
                logoff: windows::ctrl_logoff()?,
                shutdown: windows::ctrl_shutdown()?,
            })
        }
    }

    pub(super) async fn wait(&mut self) {
        #[cfg(unix)]
        tokio::select! {
            _ = self.interrupt.recv() => {},
            _ = self.terminate.recv() => {},
            _ = self.hangup.recv() => {},
        }
        #[cfg(windows)]
        tokio::select! {
            _ = self.interrupt.recv() => {},
            _ = self.terminate.recv() => {},
            _ = self.close.recv() => {},
            _ = self.logoff.recv() => {},
            _ = self.shutdown.recv() => {},
        }
    }
}

pub(super) fn spawn(command: Command) -> io::Result<Box<dyn ChildWrapper>> {
    #[cfg(target_os = "linux")]
    let command = {
        let mut command = command;
        parent_death(&mut command);
        command
    };
    let mut command = CommandWrap::from(command);
    command.wrap(KillOnDrop);
    #[cfg(windows)]
    {
        use process_wrap::tokio::{CreationFlags, JobObject};
        let mut flags = CreationFlags(Default::default());
        flags.0.0 = windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
        // JobObject suspends the child until assignment, and KillOnDrop makes
        // the job kill its entire tree when the owning process closes its handle.
        command.wrap(flags).wrap(JobObject);
    }
    #[cfg(unix)]
    command.wrap(process_wrap::tokio::ProcessGroup::leader());
    let child = command.spawn()?;
    #[cfg(unix)]
    let child: Box<dyn ChildWrapper> = Box::new(OwnedGroup {
        child: Some(child),
        killed: false,
        reaped: false,
    });
    Ok(child)
}

#[cfg(target_os = "linux")]
#[allow(unsafe_code)]
fn parent_death(command: &mut Command) {
    let parent = rustix::process::getpid();
    // SAFETY: this child-only hook performs only prctl/getppid system calls;
    // their rustix wrappers allocate nothing and acquire no userspace locks.
    // A parent that dies before prctl is covered by the subsequent PID check.
    unsafe {
        command.pre_exec(move || {
            rustix::process::set_parent_process_death_signal(Some(rustix::process::Signal::KILL))?;
            if rustix::process::getppid() != Some(parent) {
                return Err(io::Error::from_raw_os_error(libc::ESRCH));
            }
            Ok(())
        });
    }
    // PDEATHSIG covers the direct daemon if this proof is SIGKILLed; the
    // process group additionally covers descendants during ordinary cleanup.
}

/// KillOnDrop alone kills only the direct POSIX child, so retain the group
/// wrapper through cancellation as well as explicit kill-and-wait cleanup.
#[cfg(unix)]
#[derive(Debug)]
struct OwnedGroup {
    child: Option<Box<dyn ChildWrapper>>,
    killed: bool,
    reaped: bool,
}

#[cfg(unix)]
impl ChildWrapper for OwnedGroup {
    fn inner(&self) -> &dyn ChildWrapper {
        self.child.as_ref().unwrap().as_ref()
    }
    fn inner_mut(&mut self) -> &mut dyn ChildWrapper {
        self.child.as_mut().unwrap().as_mut()
    }
    fn into_inner(mut self: Box<Self>) -> Box<dyn ChildWrapper> {
        self.child.take().unwrap()
    }
    fn start_kill(&mut self) -> io::Result<()> {
        if self.killed || self.reaped {
            return Ok(());
        }
        self.inner_mut().start_kill()?;
        self.killed = true;
        Ok(())
    }
    fn try_wait(&mut self) -> io::Result<Option<std::process::ExitStatus>> {
        use rustix::process::{Pid, WaitId, WaitIdOptions, waitid};
        use std::os::unix::process::ExitStatusExt;
        if self.reaped {
            return self.inner_mut().try_wait();
        }
        let pid = self.inner().id().and_then(|pid| Pid::from_raw(pid as i32));
        let pid = pid.ok_or_else(|| io::Error::from_raw_os_error(libc::ECHILD))?;
        // Keep the leader waitable so its PID/PGID cannot be reused before
        // cleanup kills the owned group, even when readiness sees an early exit.
        let status = waitid(
            WaitId::Pid(pid),
            WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
        )?;
        Ok(status.map(|status| {
            let raw = if let Some(code) = status.exit_status() {
                code << 8
            } else {
                status.terminating_signal().unwrap_or(0) | if status.dumped() { 0x80 } else { 0 }
            };
            std::process::ExitStatus::from_raw(raw)
        }))
    }
    fn wait(
        &mut self,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = io::Result<std::process::ExitStatus>> + Send + '_>,
    > {
        Box::pin(async {
            // This private wrapper's wait is fixture cleanup: terminate the
            // group before reaping its leader and releasing the PGID reservation.
            if !self.killed {
                self.start_kill()?;
            }
            let status = self.inner_mut().wait().await?;
            self.reaped = true;
            Ok(status)
        })
    }
}

#[cfg(unix)]
impl Drop for OwnedGroup {
    fn drop(&mut self) {
        // Once SIGKILL reached the group, do not signal its numeric PGID again:
        // cancellation may occur after the inner wait has reaped the leader.
        if !self.killed
            && !self.reaped
            && let Some(child) = &mut self.child
        {
            let _ = child.start_kill();
        }
    }
}
