//! Python is the test oracle only; all provider calls use disposable stubs.
use std::{fs, path::PathBuf, process::Command};

struct Fixtures(PathBuf);

impl Fixtures {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("r6-embedded-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        for (name, bytes) in [
            ("stub", pseudolife_extractor_fixtures::STUB),
            ("launcher", pseudolife_extractor_fixtures::LAUNCHER),
        ] {
            let path = root.join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
            fs::write(&path, bytes).unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            }
        }
        Self(root)
    }

    fn command(&self, script: &str) -> Command {
        let python = std::env::var_os("PSEUDOLIFE_TEST_PYTHON")
            .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into());
        let mut command = Command::new(python);
        command
            .arg("-u")
            .arg(
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("../extractor_harness")
                    .join(script),
            )
            .arg("--binary")
            .arg(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .arg("--stub-exe")
            .arg(self.0.join(format!("stub{}", std::env::consts::EXE_SUFFIX)));
        command
    }
}

impl Drop for Fixtures {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("remove materialized fixtures");
    }
}

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
    let fixtures = Fixtures::new();
    // Inherit output so a nextest timeout retains the last completed step.
    assert!(
        fixtures
            .command("run.py")
            .args(["--suite", "contract"])
            .status()
            .expect("Python test oracle must be installed")
            .success()
    );
}

#[test]
fn extractor_shim_lifecycle() {
    let fixtures = Fixtures::new();
    assert!(
        fixtures
            .command("run.py")
            .args(["--suite", "lifecycle"])
            .status()
            .expect("Python test oracle must be installed")
            .success()
    );
}

fn autostart(provider: &str) {
    let explicit = std::env::var("PSEUDOLIFE_TEST_REAL_AUTOSTART").ok();
    // Hosted Linux runners do not supply a user systemd manager.
    let allowed = explicit.as_deref() == Some("1")
        || (cfg!(windows)
            && real_autostart_allowed(
                explicit.as_deref(),
                std::env::var("GITHUB_ACTIONS").ok().as_deref(),
                std::env::var("RUNNER_ENVIRONMENT").ok().as_deref(),
            ));
    if !allowed {
        eprintln!(
            "real autostart proof skipped; set PSEUDOLIFE_TEST_REAL_AUTOSTART=1 on a disposable dev machine"
        );
        return;
    }
    let fixtures = Fixtures::new();
    assert!(
        fixtures
            .command("autostart.py")
            .arg("--launcher-exe")
            .arg(
                fixtures
                    .0
                    .join(format!("launcher{}", std::env::consts::EXE_SUFFIX))
            )
            .arg("--provider")
            .arg(provider)
            .status()
            .expect("Python test fixture must be installed")
            .success()
    );
}

#[test]
fn extractor_shim_autostart_claude() {
    autostart("claude");
}

#[test]
fn extractor_shim_autostart_codex() {
    autostart("codex");
}
