//! Python is the test oracle only; all provider calls use disposable stubs.
use std::{path::PathBuf, process::Command};

fn real_autostart_allowed(
    explicit: Option<&str>,
    actions: Option<&str>,
    runner: Option<&str>,
) -> bool {
    explicit == Some("1") || (actions == Some("true") && runner == Some("github-hosted"))
}

#[test]
fn real_autostart_gate_excludes_local_and_self_hosted() {
    assert!(real_autostart_allowed(
        None,
        Some("true"),
        Some("github-hosted")
    ));
    assert!(!real_autostart_allowed(
        None,
        Some("true"),
        Some("self-hosted")
    ));
    assert!(!real_autostart_allowed(None, None, None));
    assert!(!real_autostart_allowed(None, None, Some("github-hosted")));
    assert!(real_autostart_allowed(Some("1"), None, None));
}

#[test]
fn extractor_shim_stub_differential() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
    let python = std::env::var_os("PSEUDOLIFE_TEST_PYTHON")
        .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into());
    let output = Command::new(python)
        .arg(root.join("extractor_harness/run.py"))
        .arg("--binary")
        .arg(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .output()
        .expect("Python test oracle must be installed");
    assert!(
        output.status.success(),
        "differential failed:\n{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    println!("{}", String::from_utf8_lossy(&output.stdout));
}

#[cfg(windows)]
#[test]
fn extractor_shim_autostart() {
    if !real_autostart_allowed(
        std::env::var("PSEUDOLIFE_TEST_REAL_AUTOSTART")
            .ok()
            .as_deref(),
        std::env::var("GITHUB_ACTIONS").ok().as_deref(),
        std::env::var("RUNNER_ENVIRONMENT").ok().as_deref(),
    ) {
        eprintln!(
            "real autostart proof skipped; set PSEUDOLIFE_TEST_REAL_AUTOSTART=1 on a disposable dev machine"
        );
        return;
    }
    let python = std::env::var_os("PSEUDOLIFE_TEST_PYTHON").unwrap_or_else(|| "python".into());
    let output = Command::new(python)
        .arg(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../extractor_harness/autostart.py"))
        .arg("--binary")
        .arg(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .output()
        .expect("Python test fixture must be installed");
    assert!(
        output.status.success(),
        "autostart failed:\n{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    println!("{}", String::from_utf8_lossy(&output.stdout));
}
