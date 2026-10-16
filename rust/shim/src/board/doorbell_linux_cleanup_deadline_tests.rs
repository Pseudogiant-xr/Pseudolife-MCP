use super::*;
use process_wrap::tokio::{CommandWrap, ProcessSession};
use std::{
    ffi::CString,
    io::Write,
    os::unix::{ffi::OsStrExt, fs::OpenOptionsExt},
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
};

const FIXTURE: &str =
    "board::doorbell::posix_process::linux_cleanup::deadline_tests::deadline_fixture_process";

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("doorbell-deadline-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::write(self.0.join("stop"), b"stop");
    }
}

#[test]
fn deadline_fixture_process() {
    let Some(root) = std::env::var_os("DOORBELL_DEADLINE_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    fs::write(root.join("started"), b"started").unwrap();
    let end = Instant::now() + Duration::from_secs(10);
    while Instant::now() < end && !root.join("stop").exists() {
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[allow(unsafe_code)]
fn fifo(path: &Path) {
    let path = CString::new(path.as_os_str().as_bytes()).unwrap();
    // SAFETY: The NUL-terminated path belongs to this disposable fixture directory.
    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
}

#[tokio::test]
async fn stalled_metadata_cutoff_retains_owned_leader_and_allows_ordinary_retry() {
    let home = Home::new();
    let mut command = CommandWrap::with_new(std::env::current_exe().unwrap(), |command| {
        command
            .args(["--exact", FIXTURE, "--nocapture", "--test-threads=1"])
            .env("DOORBELL_DEADLINE_ROOT", &home.0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
    });
    command.wrap(ProcessSession).wrap(super::super::ExecveOnly);
    let mut child = command.spawn().unwrap();
    let leader = child.id().unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        while !home.0.join("started").exists() {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();

    // The slow metadata cell uses the production scanner and a real owned session.
    // Only proc data is substituted; signaling and reaping still operate on that child.
    let root = home.0.join("proc");
    fs::create_dir(&root).unwrap();
    fs::create_dir(root.join(leader.to_string())).unwrap();
    let mut metadata = fs::read(format!("/proc/{leader}/stat")).unwrap();
    let state = metadata.windows(2).rposition(|part| part == b") ").unwrap() + 2;
    metadata[state] = b'Z';
    fs::write(root.join(leader.to_string()).join("stat"), metadata).unwrap();
    fs::create_dir(root.join("99999999")).unwrap();
    let slow = root.join("99999999/stat");
    fifo(&slow);

    let opened = Arc::new(AtomicBool::new(false));
    let writer_opened = opened.clone();
    let (release, receive) = mpsc::channel();
    let writer = std::thread::spawn(move || {
        let end = Instant::now() + Duration::from_secs(2);
        let mut output = loop {
            match fs::OpenOptions::new()
                .write(true)
                .custom_flags(libc::O_NONBLOCK)
                .open(&slow)
            {
                Ok(output) => break output,
                Err(error) if error.raw_os_error() == Some(libc::ENXIO) && Instant::now() < end => {
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(error) => panic!("metadata reader did not open fixture: {error}"),
            }
        };
        writer_opened.store(true, Ordering::Release);
        // The fallback also makes the synchronous counterfactual terminate.
        let _ = receive.recv_timeout(Duration::from_millis(350));
        let fields = std::iter::repeat_n("0", 15).collect::<Vec<_>>().join(" ");
        output
            .write_all(format!("99999999 (slow metadata) Z 0 0 0 {fields} 1\n").as_bytes())
            .unwrap();
    });
    let timer = Arc::new(AtomicBool::new(false));
    let progress = timer.clone();
    let ticker = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(20)).await;
        progress.store(true, Ordering::Release);
    });
    let started = Instant::now();
    let result =
        kill_cancelled_group_until(child.as_mut(), root, started + Duration::from_millis(100))
            .await;
    let elapsed = started.elapsed();
    let runtime_progress = timer.load(Ordering::Acquire);
    let reader_opened = opened.load(Ordering::Acquire);
    let identity_retained = child.id() == Some(leader);
    let _ = release.send(());
    writer.join().unwrap();
    let retry = if identity_retained {
        kill_cancelled_group(child.as_mut()).await
    } else {
        Err(io::Error::other("counterfactual released the owned leader"))
    };
    ticker.await.unwrap();
    fs::remove_dir_all(&home.0).unwrap();
    eprintln!(
        "metadata deadline: elapsed_ms={} timer_progress={runtime_progress} reader_opened={reader_opened} identity_retained={identity_retained} ordinary_retry_ok={}",
        elapsed.as_millis(),
        retry.is_ok()
    );
    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::TimedOut);
    assert!(
        elapsed < Duration::from_millis(250),
        "cleanup overran its bounded deadline: {elapsed:?}"
    );
    assert!(
        runtime_progress,
        "metadata scan blocked the current-thread runtime"
    );
    assert!(
        reader_opened,
        "slow production metadata path was not exercised"
    );
    assert!(
        identity_retained,
        "cutoff reaped the leader before completion was confirmed"
    );
    retry.unwrap();
    assert_eq!(
        child.id(),
        None,
        "ordinary retry did not reap the owned leader"
    );
}

#[tokio::test]
async fn metadata_success_after_deadline_is_rejected() {
    let deadline = Instant::now() + Duration::from_millis(10);
    let result = read_before_deadline(deadline, || {
        std::thread::sleep(Duration::from_millis(40));
        Ok(true)
    })
    .await;
    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::TimedOut);
}
