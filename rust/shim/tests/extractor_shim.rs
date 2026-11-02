//! Python is the test oracle only; all provider calls use disposable stubs.
use std::{ffi::OsStr, fs, io::Write, path::PathBuf, process::Command};

#[path = "../../extractor_harness/fixtures/build_support.rs"]
mod fixture_build;

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
        if let Err(error) = fs::remove_dir_all(&self.0) {
            if std::thread::panicking() {
                // A second panic would abort the test process and hide its failure.
                let _ = writeln!(
                    std::io::stderr().lock(),
                    "remove materialized fixtures at {}: {error}",
                    self.0.display()
                );
            } else {
                panic!(
                    "remove materialized fixtures at {}: {error}",
                    self.0.display()
                );
            }
        }
    }
}

#[test]
fn fixture_cleanup_failure_is_visible() {
    let path = std::env::temp_dir().join(format!("r6-cleanup-{}", uuid::Uuid::new_v4()));
    fs::write(&path, b"a file cannot be removed as a directory").unwrap();
    let result = std::panic::catch_unwind(|| drop(Fixtures(path.clone())));
    fs::remove_file(&path).unwrap();
    let panic = result.expect_err("normal cleanup failures must fail the test");
    assert!(
        panic
            .downcast_ref::<String>()
            .expect("cleanup panic message")
            .contains("remove materialized fixtures")
    );
}

#[test]
fn fixture_cleanup_failure_during_unwind_preserves_original_panic() {
    const CHILD: &str = "PSEUDOLIFE_FIXTURE_UNWIND_CHILD";
    if let Some(path) = std::env::var_os(CHILD) {
        let result = std::panic::catch_unwind(|| {
            let _fixtures = Fixtures(path.into());
            panic!("original fixture failure");
        });
        assert_eq!(
            result.unwrap_err().downcast_ref::<&str>(),
            Some(&"original fixture failure")
        );
        return;
    }
    // Keep the regression's former double-panic abort inside a child process.
    // The parent owns the file so it can clean up even if that child aborts.
    let path = std::env::temp_dir().join(format!("r6-cleanup-{}", uuid::Uuid::new_v4()));
    fs::write(&path, b"a file cannot be removed as a directory").unwrap();
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "fixture_cleanup_failure_during_unwind_preserves_original_panic",
            "--nocapture",
        ])
        .env(CHILD, &path)
        .output()
        .expect("run cleanup unwind regression");
    fs::remove_file(&path).unwrap();
    assert!(
        output.status.success(),
        "cleanup unwind child failed: {}\n{}\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stderr).contains("remove materialized fixtures"));
}

#[test]
fn fixture_build_forwards_encoded_rustflags_without_splitting_spaces() {
    let command = fixture_build::fixture_command(
        OsStr::new("compiler path/rustc"),
        "target-triple",
        std::path::Path::new("source path/stub.rs"),
        std::path::Path::new("output path/stub.bin"),
        Some(OsStr::new("linker path/linker")),
        Some("--cfg\x1ffeature=\"fixture feature\"\x1f-C\x1flink-arg=argument with spaces"),
    );
    assert_eq!(command.get_program(), OsStr::new("compiler path/rustc"));
    let expected = [
        "--edition=2024",
        "--target",
        "target-triple",
        "source path/stub.rs",
        "-o",
        "output path/stub.bin",
        "-C",
        "linker=linker path/linker",
        "--cfg",
        "feature=\"fixture feature\"",
        "-C",
        "link-arg=argument with spaces",
    ];
    assert_eq!(
        command.get_args().collect::<Vec<_>>(),
        expected.iter().map(OsStr::new).collect::<Vec<_>>()
    );
}

#[test]
fn fixture_build_accepts_absent_or_empty_rustflags_and_linker() {
    for encoded in [None, Some("")] {
        let command = fixture_build::fixture_command(
            OsStr::new("compiler"),
            "target-triple",
            std::path::Path::new("stub.rs"),
            std::path::Path::new("stub.bin"),
            None,
            encoded,
        );
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            [
                "--edition=2024",
                "--target",
                "target-triple",
                "stub.rs",
                "-o",
                "stub.bin"
            ]
            .iter()
            .map(OsStr::new)
            .collect::<Vec<_>>()
        );
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

#[cfg(windows)]
#[test]
fn extractor_shim_autostart_names_are_literal() {
    let fixtures = Fixtures::new();
    let missing_launcher = fixtures.0.join("missing-launcher.exe");
    assert!(!missing_launcher.exists());
    for provider in ["claude", "codex"] {
        for mode in ["install-autostart", "uninstall-autostart"] {
            for name in [
                "Pseudolife*",
                "Shim?",
                "Shim[beta",
                "Shim]beta",
                "Shim`beta",
            ] {
                // A missing explicit launcher also stops the old implementation
                // before it can invoke PowerShell or touch any real task.
                let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
                    .args(["extractor-shim", mode, provider, "--name", name])
                    .arg("--native-exe")
                    .arg(&missing_launcher)
                    .arg("--env-file")
                    .arg(fixtures.0.join("settings.env"))
                    .arg("--cli")
                    .arg(fixtures.0.join("stub.exe"))
                    .args(["--port", "12345", "--host", "127.0.0.1"])
                    .output()
                    .unwrap();
                assert!(!output.status.success());
                let error = String::from_utf8_lossy(&output.stderr);
                assert!(
                    error.contains("invalid registration name"),
                    "{mode} {provider} {name}: {error}"
                );
                assert!(!fixtures.0.join("settings.env").exists());
            }
            // Spaces and punctuation without wildcard meaning remain valid.
            let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
                .args(["extractor-shim", mode, provider, "--name", "Shim (beta)-2"])
                .arg("--native-exe")
                .arg(&missing_launcher)
                .arg("--env-file")
                .arg(fixtures.0.join("settings.env"))
                .arg("--cli")
                .arg(fixtures.0.join("stub.exe"))
                .args(["--port", "12345", "--host", "127.0.0.1"])
                .output()
                .unwrap();
            assert!(!output.status.success());
            assert!(String::from_utf8_lossy(&output.stderr).contains("native launcher not found"));
        }
    }
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

fn autostart(provider: &str, scenario: &str) {
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
            .arg("--scenario")
            .arg(scenario)
            .status()
            .expect("Python test fixture must be installed")
            .success()
    );
}

#[test]
fn extractor_shim_autostart_claude() {
    autostart("claude", "lifecycle");
}

#[test]
fn extractor_shim_autostart_codex() {
    autostart("codex", "lifecycle");
}

// The Windows batch on 2026-11-03 spent 108-110 seconds in completed scheduler
// steps before its final replacement hit the 120-second test deadline. Give
// preflight and the installed lifecycle separate budgets, fixtures and cleanup.
#[test]
fn extractor_shim_autostart_claude_preflight() {
    autostart("claude", "preflight");
}

#[test]
fn extractor_shim_autostart_codex_preflight() {
    autostart("codex", "preflight");
}
