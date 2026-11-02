//! Python is the test oracle only; all provider calls use disposable stubs.
use std::{path::PathBuf, process::Command};

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
