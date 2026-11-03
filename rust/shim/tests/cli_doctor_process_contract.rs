#![deny(unsafe_code)]
//! Selectable lifecycle contracts using only disposable synthetic executables.

#[path = "../src/cli/doctor/proof_process.rs"]
mod process;

use std::{
    fs,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::process::Command;

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let root =
            std::env::temp_dir().join(format!("pseudolife-proof-process-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn helper(mode: &str, root: &Path) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args([
            "--exact",
            "process_fixture_child",
            "--nocapture",
            "--test-threads=1",
        ])
        .env_clear()
        .env("PROOF_PROCESS_MODE", mode)
        .env("PROOF_PROCESS_ROOT", root)
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    for name in ["SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    #[cfg(windows)]
    command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    command
}

async fn ready(root: &Path, name: &str) -> String {
    tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            if let Ok(value) = fs::read_to_string(root.join(name))
                && !value.is_empty()
            {
                break value;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("synthetic process did not create {name}"))
}

#[test]
fn process_fixture_child() {
    let Ok(mode) = std::env::var("PROOF_PROCESS_MODE") else {
        return;
    };
    let root = PathBuf::from(std::env::var_os("PROOF_PROCESS_ROOT").unwrap());
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(async {
        match mode.as_str() {
            "leaf" | "descendant" => {
                fs::write(root.join(&mode), std::process::id().to_string()).unwrap();
            }
            "tree" | "exited-tree" => {
                // This ordinary descendant inherits the group/job, without
                // receiving its own process wrapper or parent-death hook.
                let mut command = helper("descendant", &root);
                if mode == "exited-tree" {
                    command.kill_on_drop(false);
                }
                let mut descendant = command.spawn().unwrap();
                ready(&root, "descendant").await;
                fs::write(root.join("tree"), std::process::id().to_string()).unwrap();
                if mode == "exited-tree" {
                    return;
                }
                tokio::time::sleep(Duration::from_secs(60)).await;
                let _ = descendant.kill().await;
                return;
            }
            "parent" => {
                let mode = if cfg!(windows) { "tree" } else { "leaf" };
                let _owned = process::spawn(helper(mode, &root)).unwrap();
                ready(&root, mode).await;
                fs::write(root.join("parent-ready"), b"ready").unwrap();
                tokio::time::sleep(Duration::from_secs(60)).await;
                return;
            }
            #[cfg(target_os = "linux")]
            "signals" => {
                let mut signals = process::Signals::new().unwrap();
                // A cancelled wait must leave the same receivers available.
                assert!(
                    tokio::time::timeout(Duration::from_millis(1), signals.wait())
                        .await
                        .is_err()
                );
                fs::write(root.join("signals-ready"), b"ready").unwrap();
                signals.wait().await;
                fs::write(root.join("first-signal"), b"ready").unwrap();
                signals.wait().await;
                fs::write(root.join("second-signal"), b"ready").unwrap();
                return;
            }
            #[cfg(windows)]
            "windows-signals" => {
                hidden_console::signals(&root).await;
                return;
            }
            _ => panic!("unknown synthetic process mode"),
        }
        tokio::time::sleep(Duration::from_secs(60)).await;
    });
}

#[cfg(windows)]
struct Observed(std::os::windows::io::OwnedHandle);

#[cfg(windows)]
impl Observed {
    #[allow(unsafe_code)]
    fn new(pid: u32) -> Self {
        use std::os::windows::io::FromRawHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_SYNCHRONIZE};
        // SAFETY: OpenProcess yields an owned handle for this recorded fixture
        // PID; no control event is sent and OwnedHandle closes it exactly once.
        let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
        assert!(!handle.is_null(), "cannot observe synthetic child");
        Self(unsafe { std::os::windows::io::OwnedHandle::from_raw_handle(handle) })
    }

    #[allow(unsafe_code)]
    fn stopped(&self) -> bool {
        use std::os::windows::io::AsRawHandle;
        // SAFETY: this read-only zero-time wait borrows our live process handle.
        unsafe {
            windows_sys::Win32::System::Threading::WaitForSingleObject(self.0.as_raw_handle(), 0)
                == windows_sys::Win32::Foundation::WAIT_OBJECT_0
        }
    }
}

#[cfg(target_os = "linux")]
struct Observed {
    pid: u32,
    started: String,
}

#[cfg(target_os = "linux")]
fn process_stat(pid: u32) -> Option<(String, String)> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // comm can contain spaces and ')'; all fields after its last ')' are plain.
    let (_, fields) = stat.rsplit_once(')')?;
    let fields: Vec<_> = fields.split_whitespace().collect();
    Some((fields.first()?.to_string(), fields.get(19)?.to_string()))
}

#[cfg(target_os = "linux")]
impl Observed {
    fn new(pid: u32) -> Self {
        let (_, started) = process_stat(pid).expect("synthetic child already exited");
        Self { pid, started }
    }
    fn stopped(&self) -> bool {
        // An orphan zombie has terminated even if the host's PID 1 has not
        // reaped it yet; a reused PID no longer represents our fixture.
        process_stat(self.pid)
            .is_none_or(|(state, started)| state == "Z" || started != self.started)
    }
}

#[cfg(any(windows, target_os = "linux"))]
async fn observe(root: &Path, name: &str) -> Observed {
    Observed::new(ready(root, name).await.parse().unwrap())
}

#[cfg(any(windows, target_os = "linux"))]
async fn stopped(processes: &[Observed]) {
    tokio::time::timeout(Duration::from_secs(10), async {
        while processes.iter().any(|process| !process.stopped()) {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("owned synthetic process survived cleanup");
}

#[cfg(any(windows, target_os = "linux"))]
#[tokio::test]
async fn explicit_cleanup_kills_owned_descendants() {
    let home = Home::new();
    let mut child = process::spawn(helper("tree", &home.0)).unwrap();
    let processes = [
        observe(&home.0, "tree").await,
        observe(&home.0, "descendant").await,
    ];
    child.start_kill().unwrap();
    tokio::time::timeout(Duration::from_secs(10), child.wait())
        .await
        .unwrap()
        .unwrap();
    stopped(&processes).await;
}

#[cfg(any(windows, target_os = "linux"))]
#[tokio::test]
async fn dropped_owner_kills_owned_descendants() {
    let home = Home::new();
    let child = process::spawn(helper("tree", &home.0)).unwrap();
    let processes = [
        observe(&home.0, "tree").await,
        observe(&home.0, "descendant").await,
    ];
    drop(child);
    stopped(&processes).await;
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn early_exit_readiness_keeps_the_group_owned_until_cleanup() {
    use rustix::process::{Pid, WaitId, WaitIdOptions, waitid};
    let home = Home::new();
    let mut child = process::spawn(helper("exited-tree", &home.0)).unwrap();
    let pid = Pid::from_raw(child.id().unwrap() as i32).unwrap();
    let descendant = observe(&home.0, "descendant").await;
    ready(&home.0, "tree").await;
    tokio::time::timeout(Duration::from_secs(10), async {
        while child.try_wait().unwrap().is_none() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    // A second non-reaping observation must still find our direct child. A
    // reaping readiness poll would release its PID while descendants remain.
    assert!(
        waitid(
            WaitId::Pid(pid),
            WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
        )
        .unwrap()
        .is_some()
    );
    tokio::time::timeout(Duration::from_secs(10), child.wait())
        .await
        .unwrap()
        .unwrap();
    child.start_kill().unwrap(); // A repeated cleanup must not signal a recycled PGID.
    stopped(&[descendant]).await;
}

#[cfg(any(windows, target_os = "linux"))]
#[tokio::test]
async fn abrupt_parent_termination_kills_owned_daemon() {
    let home = Home::new();
    let mut parent = helper("parent", &home.0).spawn().unwrap();
    ready(&home.0, "parent-ready").await;
    #[cfg(windows)]
    let processes = [
        observe(&home.0, "tree").await,
        observe(&home.0, "descendant").await,
    ];
    #[cfg(target_os = "linux")]
    let processes = [observe(&home.0, "leaf").await];
    // Tokio kills this exact child handle (SIGKILL / TerminateProcess), so the
    // nested owner cannot run Drop or ordinary asynchronous fixture cleanup.
    parent.kill().await.unwrap();
    stopped(&processes).await;
}

#[tokio::test]
async fn cancelled_signal_wait_can_be_reused() {
    let mut signals = process::Signals::new().unwrap();
    for _ in 0..2 {
        assert!(
            tokio::time::timeout(Duration::from_millis(1), signals.wait())
                .await
                .is_err()
        );
    }
    // Keep the guard in scope through cleanup, including when its wait future
    // was cancelled by a successful proof branch.
    tokio::task::yield_now().await;
    drop(signals);
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn repeated_interrupts_use_the_same_signal_guard() {
    use rustix::process::{Pid, Signal, kill_process};
    let home = Home::new();
    let mut child = helper("signals", &home.0).spawn().unwrap();
    let pid = Pid::from_raw(child.id().unwrap() as i32).unwrap();
    ready(&home.0, "signals-ready").await;
    kill_process(pid, Signal::INT).unwrap();
    ready(&home.0, "first-signal").await;
    kill_process(pid, Signal::INT).unwrap();
    ready(&home.0, "second-signal").await;
    let status = tokio::time::timeout(Duration::from_secs(10), child.wait())
        .await
        .unwrap()
        .unwrap();
    assert!(status.success());
}

#[cfg(windows)]
#[tokio::test]
async fn repeated_ctrl_break_is_retained_during_cleanup_in_a_hidden_private_console() {
    let home = Home::new();
    let child = hidden_console::Child::spawn(&home.0);
    ready(&home.0, "first-signal").await;
    ready(&home.0, "cleanup-second-signal-sent").await;
    fs::write(home.0.join("release-cleanup"), b"ready").unwrap();
    ready(&home.0, "second-signal").await;
    child.wait_success().await;
}

// These native calls are confined to a disposable subprocess with its own
// initially hidden console; the test runner never attaches to or signals it.
#[cfg(windows)]
#[allow(unsafe_code)]
mod hidden_console {
    use super::*;
    use std::{
        ffi::OsString,
        os::windows::{
            ffi::OsStrExt,
            io::{AsRawHandle, FromRawHandle, OwnedHandle},
        },
        ptr,
    };
    use windows_sys::Win32::{
        Foundation::WAIT_OBJECT_0,
        System::{
            Console::{CTRL_BREAK_EVENT, GenerateConsoleCtrlEvent, GetConsoleProcessList},
            Threading::{
                CREATE_NEW_CONSOLE, CREATE_UNICODE_ENVIRONMENT, CreateProcessW, GetExitCodeProcess,
                GetStartupInfoW, PROCESS_INFORMATION, STARTF_USESHOWWINDOW, STARTUPINFOW,
                TerminateProcess, WaitForSingleObject,
            },
        },
    };

    pub(super) struct Child(OwnedHandle);

    impl Child {
        pub(super) fn spawn(root: &Path) -> Self {
            let command = helper("windows-signals", root);
            let command = command.as_std();
            let executable: Vec<_> = command.get_program().encode_wide().chain([0]).collect();
            // A Windows executable filename cannot contain a quote; preserve
            // its native UTF-16 spelling, including non-Unicode path elements.
            let mut arguments = OsString::from("\"");
            arguments.push(command.get_program());
            arguments.push("\" --exact process_fixture_child --nocapture --test-threads=1");
            let mut arguments: Vec<_> = arguments.encode_wide().chain([0]).collect();
            let directory: Vec<_> = root.as_os_str().encode_wide().chain([0]).collect();
            let mut variables: Vec<_> = command
                .get_envs()
                .filter_map(|(key, value)| value.map(|value| (key, value)))
                .collect();
            variables.sort_by_key(|(key, _)| key.to_string_lossy().to_ascii_uppercase());
            let mut environment = Vec::new();
            for (key, value) in variables {
                let mut entry = key.to_os_string();
                entry.push("=");
                entry.push(value);
                environment.extend(entry.encode_wide().chain([0]));
            }
            environment.push(0);
            // STARTF_USESHOWWINDOW applies to the console at creation time;
            // wShowWindow=0 is SW_HIDE. CREATE_NO_WINDOW would remove the console.
            // https://learn.microsoft.com/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfow
            let startup = STARTUPINFOW {
                cb: std::mem::size_of::<STARTUPINFOW>() as u32,
                dwFlags: STARTF_USESHOWWINDOW,
                wShowWindow: 0,
                ..Default::default()
            };
            let mut information = PROCESS_INFORMATION::default();
            // SAFETY: all buffers remain live for this synchronous call; the
            // command line is writable and environment is double-NUL terminated.
            // No parent handles or console are inherited by the new console.
            let created = unsafe {
                CreateProcessW(
                    executable.as_ptr(),
                    arguments.as_mut_ptr(),
                    ptr::null(),
                    ptr::null(),
                    0,
                    CREATE_NEW_CONSOLE | CREATE_UNICODE_ENVIRONMENT,
                    environment.as_ptr().cast(),
                    directory.as_ptr(),
                    &startup,
                    &mut information,
                )
            };
            assert_ne!(
                created,
                0,
                "hidden console fixture spawn failed: {}",
                std::io::Error::last_os_error()
            );
            // SAFETY: successful CreateProcessW transfers both distinct handles
            // to us; OwnedHandle closes each exactly once.
            let process = unsafe { OwnedHandle::from_raw_handle(information.hProcess) };
            let thread = unsafe { OwnedHandle::from_raw_handle(information.hThread) };
            drop(thread);
            Self(process)
        }

        pub(super) async fn wait_success(&self) {
            tokio::time::timeout(Duration::from_secs(10), async {
                // SAFETY: zero-time waits borrow our owned, live process handle.
                while unsafe { WaitForSingleObject(self.0.as_raw_handle(), 0) } != WAIT_OBJECT_0 {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .expect("hidden console fixture did not exit");
            let mut code = 0;
            // SAFETY: the process handle and writable exit-code pointer are valid.
            assert_ne!(
                unsafe { GetExitCodeProcess(self.0.as_raw_handle(), &mut code) },
                0
            );
            assert_eq!(code, 0, "hidden console fixture failed");
        }
    }

    impl Drop for Child {
        fn drop(&mut self) {
            // SAFETY: this handle identifies only the synthetic child we created,
            // including if the numeric PID has since been reused elsewhere.
            unsafe {
                if WaitForSingleObject(self.0.as_raw_handle(), 0) != WAIT_OBJECT_0 {
                    let _ = TerminateProcess(self.0.as_raw_handle(), 1);
                    let _ = WaitForSingleObject(self.0.as_raw_handle(), 10_000);
                }
            }
        }
    }

    fn send_private_break() {
        let mut startup = STARTUPINFOW::default();
        // SAFETY: GetStartupInfoW writes only to this valid output structure.
        unsafe { GetStartupInfoW(&mut startup) };
        assert_ne!(startup.dwFlags & STARTF_USESHOWWINDOW, 0);
        assert_eq!(
            startup.wShowWindow, 0,
            "fixture console was not created hidden"
        );
        let mut members = [0_u32; 2];
        // SAFETY: members is a writable buffer of the specified length.
        let count = unsafe { GetConsoleProcessList(members.as_mut_ptr(), members.len() as u32) };
        assert_eq!(
            count, 1,
            "control event requires a private one-process console"
        );
        assert_eq!(members[0], std::process::id());
        // SAFETY: CREATE_NEW_CONSOLE plus the membership check above confines
        // this event to our sole synthetic process. No AttachConsole is used.
        // https://learn.microsoft.com/windows/console/generateconsolectrlevent
        assert_ne!(unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0) }, 0);
    }

    pub(super) async fn signals(root: &Path) {
        let mut signals = process::Signals::new().unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(1), signals.wait())
                .await
                .is_err()
        );
        send_private_break();
        signals.wait().await;
        fs::write(root.join("first-signal"), b"ready").unwrap();
        let cleanup_file = root.join("synthetic-cleanup");
        fs::write(&cleanup_file, b"owned fixture file").unwrap();
        send_private_break();
        fs::write(root.join("cleanup-second-signal-sent"), b"ready").unwrap();
        // No signal future is polled while this cleanup barrier is pending;
        // the retained guard must keep Tokio's default-handler suppression alive.
        ready(root, "release-cleanup").await;
        fs::remove_file(cleanup_file).unwrap();
        signals.wait().await;
        fs::write(root.join("second-signal"), b"ready").unwrap();
        drop(signals);
    }
}
