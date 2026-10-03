#![allow(dead_code)]
pub fn stderr(name: &str, expected: &[&str], absent: &[&str]) -> bool {
    if std::env::var("PSEUDOLIFE_BOARD_TEST_CHILD").ok().as_deref() == Some(name) {
        return false;
    }
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", name, "--nocapture"])
        .env("PSEUDOLIFE_BOARD_TEST_CHILD", name)
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        stderr
    );
    for text in expected {
        assert!(stderr.contains(text), "missing {text}: {stderr}");
    }
    for text in absent {
        assert!(!stderr.contains(text), "unexpected {text}: {stderr}");
    }
    true
}
