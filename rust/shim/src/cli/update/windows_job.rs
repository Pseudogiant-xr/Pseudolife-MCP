//! Own descendants until command completion; preserve successful background
//! services (for example Git fsmonitor) without waiting for their lifetime.
#![allow(unsafe_code)]
use std::{
    io,
    os::windows::io::{AsRawHandle, OwnedHandle},
};
use windows_sys::Win32::System::JobObjects::{
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectExtendedLimitInformation, SetInformationJobObject,
};

pub(super) struct Job(OwnedHandle);
impl Job {
    pub(super) fn new() -> io::Result<Self> {
        let job = Self(crate::runtime::windows_job::create()?);
        // Unlike detached runtimes, an in-progress update must keep all its
        // descendants contained, including explicitly detached children.
        job.limits(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)?;
        Ok(job)
    }

    pub(super) fn assign(&self, child: &tokio::process::Child) -> io::Result<()> {
        crate::runtime::windows_job::assign_and_resume(&self.0, child)
    }

    pub(super) fn complete(&self) -> io::Result<()> {
        // Only after the primary process AND captured pipes finish. Closing
        // this unnamed, non-inheritable handle then leaves background services
        // alive; timeout/error/panic retains kill-on-close instead.
        self.limits(0)
    }

    fn limits(&self, flags: u32) -> io::Result<()> {
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = flags;
        // SAFETY: this newly owned job has no other limits or external handle;
        // the correctly sized input buffer and job remain live for the call.
        if unsafe {
            SetInformationJobObject(
                self.0.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                (&info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of_val(&info) as u32,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}
