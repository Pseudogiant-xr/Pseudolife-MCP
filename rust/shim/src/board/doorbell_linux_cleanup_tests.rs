use super::*;
use process_wrap::tokio::{CommandWrap, ProcessSession};
use std::{
    path::PathBuf,
    process::{Command, Stdio},
    time::Instant,
};

const FIXTURE: &str = "board::doorbell::posix_process::linux_cleanup::tests::fixture_process";

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("doorbell-fence-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        // Fixture processes also stop on this marker after an assertion failure.
        let _ = fs::write(self.0.join("stop"), b"stop");
    }
}

struct Affinity(libc::cpu_set_t);
impl Affinity {
    #[allow(unsafe_code)]
    fn one_cpu() -> io::Result<Self> {
        // SAFETY: Valid initialized CPU sets are passed with their exact size; PID 0
        // changes only this test thread, and Drop restores its original mask.
        unsafe {
            let mut previous: libc::cpu_set_t = std::mem::zeroed();
            let size = std::mem::size_of::<libc::cpu_set_t>();
            if libc::sched_getaffinity(0, size, &mut previous) != 0 {
                return Err(io::Error::last_os_error());
            }
            let cpu = (0..libc::CPU_SETSIZE as usize)
                .find(|cpu| libc::CPU_ISSET(*cpu, &previous))
                .ok_or_else(|| io::Error::other("no allowed fixture CPU"))?;
            let mut selected: libc::cpu_set_t = std::mem::zeroed();
            libc::CPU_SET(cpu, &mut selected);
            if libc::sched_setaffinity(0, size, &selected) != 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(Self(previous))
        }
    }
}
impl Drop for Affinity {
    #[allow(unsafe_code)]
    fn drop(&mut self) {
        // SAFETY: The saved mask belongs to this thread and remains initialized.
        assert_eq!(
            unsafe { libc::sched_setaffinity(0, std::mem::size_of::<libc::cpu_set_t>(), &self.0) },
            0
        );
    }
}

fn command(root: &Path, role: &str) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", FIXTURE, "--nocapture", "--test-threads=1"])
        .env("DOORBELL_FENCE_ROOT", root)
        .env("DOORBELL_FENCE_ROLE", role)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command
}

#[test]
fn fixture_process() {
    let Some(root) = std::env::var_os("DOORBELL_FENCE_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    match std::env::var("DOORBELL_FENCE_ROLE").unwrap().as_str() {
        "leader" => {
            assert!(
                command(&root, "intermediate")
                    .spawn()
                    .unwrap()
                    .wait()
                    .unwrap()
                    .success()
            );
            fs::write(root.join("leader-ready"), b"ready").unwrap();
        }
        "intermediate" => {
            // This intentional orphan is not waitable by the queue's owner.
            #[allow(clippy::zombie_processes)]
            let worker = command(&root, "worker").spawn().unwrap();
            fs::write(root.join("observed-worker.pid"), worker.id().to_string()).unwrap();
            return;
        }
        "worker" => {
            lower_worker_priority();
            fs::write(root.join("worker.pid"), std::process::id().to_string()).unwrap();
        }
        _ => panic!("invalid fixture role"),
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    while !root.join("stop").exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[allow(unsafe_code)]
fn lower_worker_priority() {
    // SAFETY: PRIO_PROCESS with ID 0 changes only this disposable fixture process.
    assert_eq!(unsafe { libc::setpriority(libc::PRIO_PROCESS, 0, 19) }, 0);
}

#[tokio::test]
async fn cancelled_group_fences_low_priority_orphan_before_reaping_leader() {
    let _affinity = Affinity::one_cpu().unwrap();
    let home = Home::new();
    let mut command = CommandWrap::from(tokio::process::Command::from(command(&home.0, "leader")));
    command.wrap(ProcessSession).wrap(super::super::ExecveOnly);
    let mut child = command.spawn().unwrap();
    let leader = child.id().unwrap();
    let worker = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let pid = fs::read_to_string(home.0.join("worker.pid"))
                .ok()
                .and_then(|value| value.parse::<u32>().ok());
            let observed = fs::read_to_string(home.0.join("observed-worker.pid"))
                .ok()
                .and_then(|value| value.parse::<u32>().ok());
            if home.0.join("leader-ready").exists()
                && let Some(pid) = pid
                && observed == Some(pid)
            {
                break pid;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    let before = read_stat(Path::new("/proc"), worker).unwrap().unwrap();
    assert_eq!(before.group, leader);
    assert_ne!(before.parent, leader);
    assert_ne!(before.state, b'Z');
    tokio::time::timeout(Duration::from_secs(5), kill_cancelled_group(child.as_mut()))
        .await
        .unwrap()
        .unwrap();
    assert!(
        read_stat(Path::new("/proc"), worker)
            .unwrap()
            .is_none_or(|stat| stat.state == b'Z'),
        "orphan still live after cancellation cleanup"
    );
    assert_eq!(
        child.id(),
        None,
        "leader was not reaped after group completion"
    );
    fs::remove_dir_all(&home.0).unwrap();
}

#[tokio::test]
async fn cleanup_refuses_a_child_outside_its_owned_session_group() {
    let home = Home::new();
    let mut command = CommandWrap::from(tokio::process::Command::from(command(&home.0, "worker")));
    let mut child = command.spawn().unwrap();
    let result = tokio::time::timeout(Duration::from_secs(5), kill_cancelled_group(child.as_mut()))
        .await
        .unwrap();
    assert!(result.is_err(), "an unverified group was reported complete");
    assert_eq!(child.id(), None, "owned direct child was not reaped");
    fs::remove_dir_all(&home.0).unwrap();
}

fn stat(pid: u32, state: u8, group: u32, session: u32, started: u64) -> Vec<u8> {
    let mut fields = vec![
        char::from(state).to_string(),
        "100".into(),
        group.to_string(),
        session.to_string(),
    ];
    fields.extend(std::iter::repeat_n("0".to_owned(), 15));
    fields.push(started.to_string());
    let mut bytes = format!("{pid} (").into_bytes();
    bytes.extend_from_slice(b"odd ) (name\xff");
    bytes.extend_from_slice(format!(") {}\n", fields.join(" ")).as_bytes());
    bytes
}

#[test]
fn stat_parser_and_group_scan_fail_closed_on_uncertain_identity() {
    let bytes = stat(400, b'Z', 400, 400, 123);
    let leader = parse_stat(&bytes, 400).unwrap();
    assert!(parse_stat(&bytes, 401).is_err());
    assert!(parse_stat(&stat(400, b'?', 400, 400, 123), 400).is_err());
    assert!(parse_stat(b"400 (name) Z 100 400 400", 400).is_err());
    assert!(number::<u32>(b"-1").is_err());
    assert!(number::<u32>(b"4294967296").is_err());
    assert!(number::<u64>(b"18446744073709551616").is_err());
    let home = Home::new();
    fs::create_dir(home.0.join("400")).unwrap();
    fs::write(home.0.join("400/stat"), &bytes).unwrap();
    fs::create_dir(home.0.join("401")).unwrap();
    fs::write(home.0.join("401/stat"), stat(401, b'S', 400, 400, 124)).unwrap();
    assert!(!group_is_terminal(&home.0, &leader).unwrap());
    fs::write(home.0.join("401/stat"), stat(401, b'Z', 400, 400, 124)).unwrap();
    assert!(group_is_terminal(&home.0, &leader).unwrap());
    fs::write(home.0.join("401/stat"), stat(401, b'Z', 400, 401, 124)).unwrap();
    assert!(group_is_terminal(&home.0, &leader).is_err());
    fs::write(home.0.join("401/stat"), b"malformed").unwrap();
    assert!(group_is_terminal(&home.0, &leader).is_err());
    fs::write(home.0.join("400/stat"), stat(400, b'Z', 400, 400, 999)).unwrap();
    assert!(group_is_terminal(&home.0, &leader).is_err());
    fs::remove_file(home.0.join("400/stat")).unwrap();
    assert!(group_is_terminal(&home.0, &leader).is_err());
    fs::remove_dir_all(&home.0).unwrap();
}
