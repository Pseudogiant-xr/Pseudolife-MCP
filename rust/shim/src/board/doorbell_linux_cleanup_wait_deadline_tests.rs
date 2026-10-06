use super::*;
use process_wrap::tokio::{CommandWrap, ProcessSession};
use std::{
    future::Future,
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::Poll,
};

const FIXTURE: &str =
    "board::doorbell::posix_process::linux_cleanup::deadline_tests::deadline_fixture_process";

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let root =
            std::env::temp_dir().join(format!("doorbell-wait-deadline-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::write(self.0.join("stop"), b"stop");
    }
}

async fn wait_cutoff_control(resume_pending: bool) {
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

    let deadline = Instant::now() + Duration::from_millis(100);
    // Use the actual production reap helper under the same inner-first timeout.
    let mut wait = Box::pin(tokio::time::timeout_at(
        tokio::time::Instant::from_std(deadline),
        wait_child_before_deadline(child.inner_mut(), deadline),
    ));
    let pending_observed = if resume_pending {
        std::future::poll_fn(|cx| Poll::Ready(matches!(wait.as_mut().poll(cx), Poll::Pending)))
            .await
    } else {
        false
    };
    // The owned child exits while its wait future is suspended. /proc observes
    // its zombie without releasing the PID; only a resumed wait can reap it.
    fs::write(home.0.join("stop"), b"stop").unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        while read_stat(Path::new("/proc"), leader)
            .unwrap()
            .is_none_or(|stat| stat.state != b'Z')
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    let timer = Arc::new(AtomicBool::new(false));
    let progress = timer.clone();
    let ticker = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(10)).await;
        progress.store(true, Ordering::Release);
    });
    tokio::time::sleep_until(tokio::time::Instant::from_std(
        deadline + Duration::from_millis(40),
    ))
    .await;
    let resumed = Instant::now();
    let result = wait.as_mut().await;
    drop(wait);
    let resumed_elapsed = resumed.elapsed();
    let identity_retained = child.id() == Some(leader);
    let timed_out = match result {
        Ok(Err(error)) => error.kind() == io::ErrorKind::TimedOut,
        Err(_) => true,
        Ok(Ok(())) => false,
    };
    let runtime_progress = timer.load(Ordering::Acquire);
    ticker.await.unwrap();
    // Test-owned cleanup runs only after the production future has been dropped.
    let _ = child.inner_mut().start_kill();
    child.inner_mut().wait().await.unwrap();
    fs::remove_dir_all(&home.0).unwrap();
    eprintln!(
        "wait cutoff: resume_pending={resume_pending} pending_observed={pending_observed} expired_ms={} resumed_ms={} timed_out={timed_out} identity_retained={identity_retained} timer_progress={runtime_progress} cleanup_ok=true",
        resumed.duration_since(deadline).as_millis(),
        resumed_elapsed.as_millis()
    );
    assert_eq!(pending_observed, resume_pending);
    assert!(timed_out, "expired child wait reported successful reaping");
    assert!(identity_retained, "expired wait released the owned leader");
    assert!(runtime_progress, "suspended wait blocked runtime progress");
    assert!(resumed_elapsed < Duration::from_millis(50));
}

#[tokio::test]
async fn pending_child_wait_resumed_after_deadline_retains_owned_leader() {
    wait_cutoff_control(true).await;
}

#[tokio::test]
async fn ready_child_wait_first_polled_after_deadline_retains_owned_leader() {
    wait_cutoff_control(false).await;
}
