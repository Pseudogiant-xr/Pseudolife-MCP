//! Windows queue containment and fallback; all public entry points are safe.

use std::{
    io,
    os::windows::io::{AsRawHandle, BorrowedHandle, FromRawHandle, OwnedHandle},
    path::PathBuf,
    process::{ExitStatus, Stdio},
    time::Duration,
};
use tokio::process::{Child, Command};
use windows_sys::Win32::{
    Foundation::{ERROR_NO_MORE_FILES, INVALID_HANDLE_VALUE},
    System::{
        Diagnostics::ToolHelp::{
            CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
        },
        JobObjects::{AssignProcessToJobObject, CreateJobObjectW, TerminateJobObject},
        Threading::{
            CREATE_NO_WINDOW, CREATE_SUSPENDED, GetProcessIdOfThread, OpenThread, ResumeThread,
            THREAD_QUERY_LIMITED_INFORMATION, THREAD_SUSPEND_RESUME,
        },
    },
};

/// A spawn failure distinguishes definite preexecution failure from uncertainty.
#[derive(Debug)]
pub struct SpawnFailure {
    pub error: io::Error,
    pub unstarted: bool,
    #[cfg(test)]
    pid: Option<u32>,
}

#[derive(Clone, Copy)]
enum Execution {
    Unstarted,
    MayHaveExecuted,
}
struct AdoptionFailure {
    error: io::Error,
    execution: Execution,
}
impl From<io::Error> for AdoptionFailure {
    fn from(error: io::Error) -> Self {
        Self {
            error,
            execution: Execution::Unstarted,
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Phase {
    Create,
    Assign,
    Handle,
    Snapshot,
    ThreadOpen,
    Resume,
    #[cfg(test)]
    ResumeAfterFirst,
    #[cfg(test)]
    AssignAndResumeAfterFirst,
    Terminate,
}

#[derive(Default)]
struct Phases {
    #[cfg(test)]
    refuse: Option<Phase>,
}
impl Phases {
    fn refuses(&self, _phase: Phase) -> bool {
        #[cfg(test)]
        {
            self.refuse == Some(_phase)
                || (self.refuse == Some(Phase::AssignAndResumeAfterFirst)
                    && matches!(_phase, Phase::Assign | Phase::ResumeAfterFirst))
        }
        #[cfg(not(test))]
        {
            false
        }
    }
}

/// Owns the leader and its optional job without killing successful workers on drop.
pub struct QueueProcess {
    child: Child,
    job: Option<OwnedHandle>,
    // A separate handle pins the PID even after Tokio observes leader exit.
    process_pin: Option<OwnedHandle>,
    // Failed or cancelled adoption still owns cleanup, even after partial resume.
    needs_cleanup: bool,
    phases: Phases,
    #[cfg(test)]
    fixture_root: Option<PathBuf>,
}
impl QueueProcess {
    pub async fn spawn(command: &mut Command) -> Result<Self, SpawnFailure> {
        Self::spawn_phases(command, Phases::default()).await
    }

    #[allow(unsafe_code)]
    async fn spawn_phases(command: &mut Command, phases: Phases) -> Result<Self, SpawnFailure> {
        #[cfg(test)]
        let fixture_root = command.as_std().get_envs().find_map(|(key, value)| {
            (key == "DOORBELL_WIN32_ROOT")
                .then(|| value.map(PathBuf::from))
                .flatten()
        });
        let job = if phases.refuses(Phase::Create) {
            None
        } else {
            create_job()
        };
        command.creation_flags(CREATE_NO_WINDOW | if job.is_some() { CREATE_SUSPENDED } else { 0 });
        let child = command.spawn().map_err(|error| SpawnFailure {
            unstarted: super::definitely_unstarted(&error),
            error,
            #[cfg(test)]
            pid: None,
        })?;
        let mut process = Self {
            child,
            needs_cleanup: job.is_some(),
            job,
            process_pin: None,
            phases,
            #[cfg(test)]
            fixture_root,
        };
        // Tokio owns this live process handle. Clone it while the child borrow
        // is live; never take ownership of the borrowed raw handle itself.
        let pin = process
            .child
            .raw_handle()
            .ok_or_else(|| io::Error::other("no process handle"))
            .and_then(|raw| unsafe { BorrowedHandle::borrow_raw(raw) }.try_clone_to_owned());
        match pin {
            Ok(pin) => process.process_pin = Some(pin),
            Err(error) => {
                let unstarted = process.job.is_some();
                #[cfg(test)]
                let pid = process.child.id();
                process.kill().await;
                return Err(SpawnFailure {
                    error,
                    unstarted,
                    #[cfg(test)]
                    pid,
                });
            }
        }
        if process.job.is_some() {
            let adopted = process.adopt();
            match adopted {
                Ok(true) => process.needs_cleanup = false,
                // Assignment refusal resumes the CLI and closes the unused job.
                Ok(false) => {
                    process.needs_cleanup = false;
                    process.job = None;
                }
                Err(failure) => {
                    #[cfg(test)]
                    let pid = process.child.id();
                    process.kill().await;
                    return Err(SpawnFailure {
                        error: failure.error,
                        unstarted: matches!(failure.execution, Execution::Unstarted),
                        #[cfg(test)]
                        pid,
                    });
                }
            }
        }
        Ok(process)
    }

    #[allow(unsafe_code)]
    fn adopt(&mut self) -> Result<bool, AdoptionFailure> {
        if self.phases.refuses(Phase::Handle) {
            return Err(io::Error::other("no process handle").into());
        }
        let job = self
            .job
            .as_ref()
            .ok_or_else(|| io::Error::other("no job handle"))?;
        let process = self
            .process_pin
            .as_ref()
            .ok_or_else(|| io::Error::other("no process handle"))?;
        // Both handles remain owned through the call. A refusal is a fallback,
        // not a spawn failure, matching the Python adoption contract.
        let assigned = !self.phases.refuses(Phase::Assign)
            && unsafe { AssignProcessToJobObject(job.as_raw_handle(), process.as_raw_handle()) }
                != 0;
        // An empty refused job cannot contain descendants created by partial
        // resume. Close it now so any ensuing failure uses taskkill fallback.
        if !assigned {
            self.job = None;
        }
        if self.phases.refuses(Phase::Resume) {
            return Err(io::Error::other("resume failed").into());
        }
        resume_threads(
            self.child
                .id()
                .ok_or_else(|| io::Error::other("no process id"))?,
            &self.phases,
            #[cfg(test)]
            self.fixture_root.as_deref(),
        )?;
        Ok(assigned)
    }

    pub async fn wait(&mut self) -> io::Result<ExitStatus> {
        self.child.wait().await
    }

    /// Terminate the job, else taskkill the still-live leader tree; always reap.
    #[allow(unsafe_code)]
    pub async fn kill(&mut self) {
        let killed = !self.phases.refuses(Phase::Terminate)
            && self.job.as_ref().is_some_and(|job| {
                // OwnedHandle remains live; the job was created with terminate access.
                unsafe { TerminateJobObject(job.as_raw_handle(), 1) != 0 }
            });
        if !killed
            && !matches!(self.child.try_wait(), Ok(Some(_)))
            && let Some(pid) = self.child.id()
        {
            let root = std::env::var_os("SystemRoot").unwrap_or_else(|| r"C:\Windows".into());
            let mut command = Command::new(PathBuf::from(root).join("System32/taskkill.exe"));
            command
                .args(["/T", "/F", "/PID", &pid.to_string()])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(CREATE_NO_WINDOW);
            if let Ok(mut killer) = command.spawn() {
                let _ = tokio::time::timeout(Duration::from_secs(5), killer.wait()).await;
            }
        }
        // Both the Tokio child and process_pin stay owned throughout taskkill.
        let _ = self.child.start_kill();
        let _ = tokio::time::timeout(Duration::from_secs(5), self.child.wait()).await;
    }
}

impl Drop for QueueProcess {
    #[allow(unsafe_code)]
    fn drop(&mut self) {
        // A cancelled spawn future may be awaiting failed-adoption cleanup.
        // Partial resume can create descendants before failed-adoption cleanup.
        // The job stays owned until cleanup; successful workers survive drop.
        if self.needs_cleanup {
            if let Some(job) = &self.job {
                let _ = unsafe { TerminateJobObject(job.as_raw_handle(), 1) };
            }
            let _ = self.child.start_kill();
        }
    }
}

#[allow(unsafe_code)]
fn create_job() -> Option<OwnedHandle> {
    // Null attributes/name create a fresh, non-inheritable job; no kill-on-close
    // or breakaway limits are enabled. Transfer each successful handle once.
    let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
    (!handle.is_null()).then(|| unsafe { OwnedHandle::from_raw_handle(handle) })
}

#[allow(unsafe_code)]
fn resume_threads(
    pid: u32,
    phases: &Phases,
    #[cfg(test)] fixture_root: Option<&std::path::Path>,
) -> Result<(), AdoptionFailure> {
    if phases.refuses(Phase::Snapshot) {
        return Err(io::Error::other("thread snapshot failed").into());
    }
    // Snapshot is non-inheritable; INVALID_HANDLE_VALUE must not be owned.
    let raw = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if raw == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error().into());
    }
    let snapshot = unsafe { OwnedHandle::from_raw_handle(raw) };
    let mut entry = THREADENTRY32 {
        dwSize: std::mem::size_of::<THREADENTRY32>() as u32,
        ..Default::default()
    };
    // The writable initialized entry remains alive through enumeration. Open
    // every matching thread before resuming any, while the child is suspended.
    if unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) } == 0 {
        return Err(io::Error::last_os_error().into());
    }
    let mut threads = Vec::new();
    loop {
        if entry.dwSize < std::mem::size_of::<THREADENTRY32>() as u32 {
            return Err(io::Error::other("short thread snapshot entry").into());
        }
        if entry.th32OwnerProcessID == pid {
            if phases.refuses(Phase::ThreadOpen) {
                return Err(io::Error::other("open thread failed").into());
            }
            let raw = unsafe {
                OpenThread(
                    THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION,
                    0,
                    entry.th32ThreadID,
                )
            };
            if raw.is_null() {
                return Err(io::Error::last_os_error().into());
            }
            let thread = unsafe { OwnedHandle::from_raw_handle(raw) };
            // A snapshot's TID can be recycled before OpenThread. The owned
            // handle pins its thread; verify its process before resuming it.
            let owner = unsafe { GetProcessIdOfThread(thread.as_raw_handle()) };
            if owner == 0 {
                return Err(io::Error::last_os_error().into());
            }
            if owner != pid {
                return Err(io::Error::other("thread owner changed").into());
            }
            threads.push(thread);
        }
        entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
        if unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) } == 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(ERROR_NO_MORE_FILES as i32) {
                return Err(error.into());
            }
            break;
        }
    }
    if threads.is_empty() {
        return Err(io::Error::other("no suspended process threads").into());
    }
    let mut execution = Execution::Unstarted;
    for thread in threads {
        // Only owned thread handles from this child are passed. DWORD_MAX is
        // failure; a successful ResumeThread decrements the initial suspension.
        let previous = unsafe { ResumeThread(thread.as_raw_handle()) };
        if previous == u32::MAX {
            return Err(AdoptionFailure {
                error: io::Error::last_os_error(),
                execution,
            });
        }
        // Resume is sequential, not atomic. Any successful call loses the
        // definitely-unstarted guarantee, regardless of its previous count.
        execution = Execution::MayHaveExecuted;
        #[cfg(test)]
        if phases.refuses(Phase::ResumeAfterFirst) {
            let root = fixture_root.expect("partial-resume fixture root");
            std::fs::write(root.join("native-resume"), previous.to_string()).unwrap();
            let until = std::time::Instant::now() + Duration::from_secs(5);
            while !root.join("ticks").exists() && std::time::Instant::now() < until {
                std::thread::sleep(Duration::from_millis(15));
            }
            return Err(AdoptionFailure {
                error: io::Error::other("injected failure after native resume"),
                execution,
            });
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::board::{notice::PendingNotice, state};
    use serde_json::json;
    use std::{
        io::{Read, Write},
        os::windows::process::CommandExt,
    };

    const THREAD: &str = "12345678-1234-1234-1234-123456789abc";
    const FIXTURE: &str = "board::doorbell::windows_process::tests::fixture_child";

    #[test]
    fn partial_resume_fixture_child() {
        let Some(root) = std::env::var_os("DOORBELL_WIN32_ROOT") else {
            return;
        };
        let root = PathBuf::from(root);
        let role = std::env::var("DOORBELL_WIN32_ROLE").unwrap();
        std::fs::write(
            root.join(format!("{role}.pid")),
            std::process::id().to_string(),
        )
        .unwrap();
        if role == "worker" {
            let until = std::time::Instant::now() + Duration::from_secs(10);
            while !root.join("stop").exists() && std::time::Instant::now() < until {
                std::fs::write(root.join("ticks"), "alive").unwrap();
                std::thread::sleep(Duration::from_millis(30));
            }
        } else {
            if role == "leader" {
                std::fs::write(root.join("executed"), "executed").unwrap();
            }
            let mut child = partial_command(
                &root,
                if role == "leader" {
                    "intermediate"
                } else {
                    "worker"
                },
            )
            .as_std_mut()
            .spawn()
            .unwrap();
            let _ = child.wait();
        }
    }

    fn partial_command(root: &std::path::Path, role: &str) -> Command {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "board::doorbell::windows_process::tests::partial_resume_fixture_child",
                "--nocapture",
            ])
            .env("DOORBELL_WIN32_ROOT", root)
            .env("DOORBELL_WIN32_ROLE", role)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW);
        for (key, _) in std::env::vars_os() {
            if key
                .to_string_lossy()
                .to_uppercase()
                .starts_with("PSEUDOLIFE_")
            {
                command.env_remove(key);
            }
        }
        command
    }

    #[tokio::test]
    async fn partial_native_resume_kills_tree_and_preserves_reservation() {
        for phase in [Phase::ResumeAfterFirst, Phase::AssignAndResumeAfterFirst] {
            partial_resume(phase).await;
        }
    }

    async fn partial_resume(phase: Phase) {
        let home = Home::new();
        let pending = PendingNotice::new(&home.0.join("notices"), THREAD).unwrap();
        let notice = pending
            .reserve(
                1,
                Some(super::super::now() + 3600.0),
                super::super::now(),
                false,
            )
            .unwrap();
        let before = state::read(&pending.path, 8192).unwrap();
        let failure = match QueueProcess::spawn_phases(
            &mut partial_command(&home.0, "leader"),
            Phases {
                refuse: Some(phase),
            },
        )
        .await
        {
            Ok(_) => panic!("injected partial resume accepted"),
            Err(failure) => failure,
        };
        let leader = failure.pid.unwrap();
        assert_eq!(
            std::fs::read_to_string(home.0.join("native-resume")).unwrap(),
            "1"
        );
        assert!(
            home.0.join("executed").exists(),
            "resumed leader did not execute"
        );
        assert!(
            home.0.join("ticks").exists(),
            "resumed leader did not launch grandchild"
        );
        let worker: u32 = std::fs::read_to_string(home.0.join("worker.pid"))
            .unwrap()
            .parse()
            .unwrap();
        let intermediate: u32 = std::fs::read_to_string(home.0.join("intermediate.pid"))
            .unwrap()
            .parse()
            .unwrap();
        assert!(terminated(leader), "partially resumed leader survived");
        assert!(
            terminated(intermediate),
            "partially resumed intermediate survived"
        );
        assert!(terminated(worker), "partially resumed grandchild survived");
        // Same classification gate used by Doorbell::_ring's spawn-failure path.
        let rolled_back =
            failure.unstarted && pending.rollback_unstarted(&notice, super::super::now());
        assert!(!rolled_back, "executed child was falsely rolled back");
        assert!(!failure.unstarted);
        assert_eq!(state::read(&pending.path, 8192).unwrap(), before);
        assert!(!pending.path.with_extension("bell-accepted").exists());
        evidence(
            json!({"scenario":"injected_partial_resume","platform":"windows","leader_pid":leader,"intermediate_pid":intermediate,"worker_pid":worker,"real_resume_previous_count":1,"leader_executed":true,"grandchild_ticked":true,"leader_terminated":true,"intermediate_terminated":true,"worker_terminated":true,"reservation_preserved":true,"assignment_refused":phase == Phase::AssignAndResumeAfterFirst,"fault_injected":true}),
        );
    }

    #[test]
    fn windows_spawn_error_rollback_allowlist_is_exact() {
        for code in [
            2, 3, 5, 11, 15, 16, 53, 65, 67, 82, 83, 108, 132, 158, 161, 167, 206, 267, 10013,
        ]
        .into_iter()
        .chain(18..=36)
        .chain(188..=202)
        {
            assert!(super::super::definitely_unstarted(
                &io::Error::from_raw_os_error(code)
            ));
        }
        for code in [
            0, 1, 4, 6, 17, 37, 52, 54, 64, 66, 68, 81, 84, 107, 109, 131, 133, 157, 159, 160, 162,
            166, 168, 187, 203, 205, 207, 266, 268, 10012, 10014, 65535,
        ] {
            assert!(!super::super::definitely_unstarted(
                &io::Error::from_raw_os_error(code)
            ));
        }
    }
    struct Home(PathBuf);
    impl Home {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("doorbell-win32-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Home {
        fn drop(&mut self) {
            let _ = std::fs::write(self.0.join("stop"), "stop");
            // Only the unique fixture directory under the verified temp root.
            if self.0.parent() == Some(std::env::temp_dir().as_path()) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }
    fn command(home: &Home) -> Command {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args(["--exact", FIXTURE, "--nocapture"])
            .env("DOORBELL_WIN32_ROOT", &home.0)
            .env("DOORBELL_WIN32_ROLE", "leader")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        for (key, _) in std::env::vars_os() {
            if key
                .to_string_lossy()
                .to_uppercase()
                .starts_with("PSEUDOLIFE_")
            {
                command.env_remove(key);
            }
        }
        command
    }
    fn terminated(pid: u32) -> bool {
        std::process::Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &format!("$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($null -eq $p) {{ exit 0 }}; if ($p.WaitForExit(0)) {{ exit 0 }}; exit 1")])
            .creation_flags(CREATE_NO_WINDOW).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .status().unwrap().success()
    }
    async fn waiting(home: &Home) -> io::Result<u32> {
        tokio::time::timeout(Duration::from_secs(5), async {
            while !home.0.join("ticks").exists() {
                tokio::time::sleep(Duration::from_millis(15)).await;
            }
        })
        .await
        .map_err(|error| io::Error::new(io::ErrorKind::TimedOut, error))?;
        std::fs::read_to_string(home.0.join("worker.pid"))?
            .parse()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
    }
    fn evidence(value: serde_json::Value) {
        if let Some(path) = std::env::var_os("DOORBELL_CLEANUP_EVIDENCE") {
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .read(true)
                .open(path)
                .unwrap();
            std::fs::File::lock(&file).unwrap();
            file.write_all(format!("{value}\n").as_bytes()).unwrap();
            std::fs::File::unlock(&file).unwrap();
        }
    }
    // This executable supplies a real, connected tree for taskkill fallback.
    // Normal orphan containment is separately asserted by the integration fixture.
    #[test]
    fn fixture_child() {
        let Some(root) = std::env::var_os("DOORBELL_WIN32_ROOT") else {
            return;
        };
        let root = PathBuf::from(root);
        let role = std::env::var("DOORBELL_WIN32_ROLE").unwrap();
        if matches!(role.as_str(), "leader" | "intermediate") {
            if role == "leader" {
                std::fs::write(root.join("executed"), "executed").unwrap();
            }
            let mut worker = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", FIXTURE, "--nocapture"])
                .env(
                    "DOORBELL_WIN32_ROLE",
                    if role == "leader" {
                        "intermediate"
                    } else {
                        "worker"
                    },
                )
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::inherit())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            let status = worker.wait().unwrap();
            if !status.success() {
                std::process::exit(status.code().unwrap_or(1));
            }
        } else {
            if std::env::var_os("DOORBELL_FIXTURE_WORKER_FAILURE").is_some() {
                std::process::exit(2);
            }
            std::fs::write(root.join("worker.pid"), std::process::id().to_string()).unwrap();
            std::fs::write(root.join("ticks"), "alive").unwrap();
            // The test owns the sole writer through cleanup assertions. EOF is
            // an unwind signal, never a timer that can fake successful cleanup.
            std::io::stdin().read_to_end(&mut Vec::new()).unwrap();
        }
    }

    #[tokio::test]
    async fn fixture_propagates_unsuccessful_descendant() {
        let home = Home::new();
        let status = command(&home)
            .env("DOORBELL_FIXTURE_WORKER_FAILURE", "1")
            .status()
            .await
            .unwrap();
        assert_eq!(status.code(), Some(2));
    }

    async fn fallback(phase: Phase, name: &str) {
        let home = Home::new();
        let mut process = QueueProcess::spawn_phases(
            &mut command(&home),
            Phases {
                refuse: Some(phase),
            },
        )
        .await
        .unwrap();
        let contained = process.job.is_some();
        let leader = process.child.id().unwrap();
        // Child::wait closes its stdin. Keep the writer separately so a failed
        // tree kill cannot pass by releasing the fixture worker through EOF.
        let keepalive = process.child.stdin.take().unwrap();
        let worker = waiting(&home).await;
        let alive = worker
            .as_ref()
            .ok()
            .map(|worker| std::panic::catch_unwind(|| !terminated(*worker)));
        process.kill().await;
        let status = process.wait().await;
        // Readiness errors and probe panics reach owned cleanup before failing.
        assert_eq!(contained, phase == Phase::Terminate);
        let worker = worker.expect("fallback worker readiness failed after cleanup");
        let alive = alive
            .unwrap()
            .unwrap_or_else(|panic| std::panic::resume_unwind(panic));
        assert!(alive, "fallback worker was not alive before cleanup");
        assert!(terminated(leader), "fallback leader remained alive");
        assert!(terminated(worker), "fallback descendant remained alive");
        assert!(
            !status
                .expect("fallback leader wait failed after cleanup")
                .success()
        );
        drop(keepalive);
        evidence(
            json!({"scenario":name,"platform":"windows","leader_pid":leader,"worker_pid":worker,"worker_alive_before_cleanup":true,"connected_grandchild":true,"leader_terminated":true,"worker_terminated":true,"native_taskkill_fallback":true}),
        );
    }
    #[tokio::test]
    async fn create_job_refusal_runs_cli_and_taskkill_kills_descendants() {
        fallback(Phase::Create, "create_job_refused").await;
    }
    #[tokio::test]
    async fn assignment_refusal_resumes_cli_and_taskkill_kills_descendants() {
        fallback(Phase::Assign, "assignment_refused").await;
    }
    #[tokio::test]
    async fn terminate_job_refusal_falls_back_to_taskkill_tree() {
        fallback(Phase::Terminate, "terminate_job_refused").await;
    }

    #[tokio::test]
    async fn fixture_pipe_release_on_unwind_reaps_the_connected_tree() {
        let home = Home::new();
        let mut child = command(&home).spawn().unwrap();
        let worker = waiting(&home).await.unwrap();
        assert!(!terminated(worker), "fixture worker exited before signal");
        let keepalive = child.stdin.take().unwrap();
        let panic = std::panic::catch_unwind(move || {
            let _keepalive = keepalive;
            panic!("injected fixture assertion failure");
        })
        .unwrap_err();
        assert_eq!(
            panic.downcast_ref::<&str>(),
            Some(&"injected fixture assertion failure")
        );
        assert!(
            tokio::time::timeout(Duration::from_secs(5), child.wait())
                .await
                .unwrap()
                .unwrap()
                .success(),
            "fixture tree did not finish after the owner unwound"
        );
        assert!(terminated(worker), "fixture worker survived pipe closure");
    }

    #[tokio::test]
    async fn adoption_and_resume_failures_kill_suspended_child_and_fence_rollback() {
        for (phase, name) in [
            (Phase::Handle, "adoption_handle_missing"),
            (Phase::Snapshot, "thread_snapshot_failed"),
            (Phase::ThreadOpen, "thread_open_failed"),
            (Phase::Resume, "resume_failed"),
        ] {
            let home = Home::new();
            let pending = PendingNotice::new(&home.0.join("notices"), THREAD).unwrap();
            let notice = pending
                .reserve(
                    1,
                    Some(super::super::now() + 3600.0),
                    super::super::now(),
                    false,
                )
                .unwrap();
            let error = match QueueProcess::spawn_phases(
                &mut command(&home),
                Phases {
                    refuse: Some(phase),
                },
            )
            .await
            {
                Ok(_) => panic!("injected adoption failure accepted"),
                Err(error) => error,
            };
            assert!(error.unstarted);
            let pid = error.pid.unwrap();
            assert!(terminated(pid), "suspended child survived adoption failure");
            assert!(
                !home.0.join("executed").exists(),
                "suspended child executed before failure"
            );
            assert!(pending.rollback_unstarted(&notice, super::super::now()));
            assert!(!state::exists(&pending.path));
            // A failed spawn may not roll back a subsequent nonce or acceptance.
            let next = pending
                .reserve(
                    1,
                    Some(super::super::now() + 3600.0),
                    super::super::now(),
                    false,
                )
                .unwrap();
            assert_ne!(next["nonce"], notice["nonce"]);
            assert!(!pending.rollback_unstarted(&notice, super::super::now()));
            let before = state::read(&pending.path, 8192).unwrap();
            pending.accept(&next, super::super::now());
            assert!(!pending.rollback_unstarted(&next, super::super::now()));
            assert_eq!(state::read(&pending.path, 8192).unwrap(), before);
            evidence(
                json!({"scenario":name,"platform":"windows","pid":pid,"suspended_child_terminated":true,"child_never_executed":true,"matching_reservation_rolled_back":true,"newer_nonce_and_accepted_notice_preserved":true}),
            );
        }
    }
}
