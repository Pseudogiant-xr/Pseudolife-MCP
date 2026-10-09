#![forbid(unsafe_code)]
mod common;
use common::cli::{cleared_command, native_text as bytes};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

struct DisposableHome(std::path::PathBuf);
impl DisposableHome {
    fn new() -> Self {
        // Beside the built binary rather than in the system temp directory,
        // so `install_shim` can hard-link it: a link cannot cross filesystems,
        // /tmp is often a separate tmpfs, and Cargo's build-dir (which holds
        // CARGO_TARGET_TMPDIR) can be configured apart from the binaries.
        let path = Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .parent()
            .unwrap()
            .join(format!(
                "pseudolife-version-test-{}",
                uuid::Uuid::new_v4().simple()
            ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn path(&self, name: &str) -> std::path::PathBuf {
        self.0.join(name).components().collect()
    }
}
impl Drop for DisposableHome {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn command(executable: &Path, home: &Path) -> Command {
    let mut command = cleared_command(executable);
    command
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("LOCALAPPDATA", home.join("local"))
        .env("XDG_DATA_HOME", home.join("data"))
        .env("PATH", "")
        .env("CUDA_VISIBLE_DEVICES", "-1")
        .env("PSEUDOLIFE_MCP_PYTHON", "missing-fixture-interpreter");
    command
}

fn native_executable(runtime: &Path) -> std::path::PathBuf {
    let scripts = runtime.join(if cfg!(windows) { "Scripts" } else { "bin" });
    fs::create_dir_all(&scripts).unwrap();
    let executable = scripts.join(if cfg!(windows) {
        "pseudolife-stdio.exe"
    } else {
        "pseudolife-stdio"
    });
    install_shim(&executable);
    executable
}

/// Places the built shim at `executable` by hard link, never by copy.
///
/// A copy holds a writable descriptor on the new file while it fills it, and
/// a child that another test forks in that window keeps a duplicate until its
/// own exec closes it. Linux refuses to exec a file anyone holds open for
/// writing (ETXTBSY, "Text file busy"), so the copy-then-exec tests here
/// failed whenever they raced a concurrent spawn. A link opens nothing for
/// writing, so there is no descriptor to inherit.
fn install_shim(executable: &Path) {
    fs::hard_link(env!("CARGO_BIN_EXE_pseudolife-stdio"), executable).unwrap();
}

fn console_path(runtime: &Path) -> std::path::PathBuf {
    runtime
        .join(if cfg!(windows) { "Scripts" } else { "bin" })
        .join(if cfg!(windows) {
            "pseudolife-mcp.exe"
        } else {
            "pseudolife-mcp"
        })
}

fn installed_executable(runtime: &Path) -> std::path::PathBuf {
    let executable = native_executable(runtime);
    fs::write(console_path(runtime), b"fixture console").unwrap();
    executable
}

fn first_line() -> String {
    format!("pseudolife-mcp {}\n", env!("CARGO_PKG_VERSION"))
}

fn assert_output(output: Output, expected: &str) {
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(output.stdout, bytes(expected));
    assert!(output.stderr.is_empty());
}

#[test]
fn crate_version_matches_the_pinned_python_package() {
    let root = std::env::var_os("PSEUDOLIFE_PORT_ORACLE_ROOT")
        .expect("prepare pinned metadata with evals.rust_port.phase1_oracle --metadata-only");
    let metadata = fs::read_to_string(Path::new(&root).join("pyproject.toml")).unwrap();
    let version = metadata
        .lines()
        .find_map(|line| {
            line.strip_prefix("version = \"")
                .and_then(|value| value.strip_suffix('"'))
        })
        .unwrap();
    assert_eq!(env!("CARGO_PKG_VERSION"), version);
}

#[test]
fn bare_version_aliases_ignore_trailing_arguments_without_an_interpreter() {
    let home = DisposableHome::new();
    for arguments in [
        vec!["version"],
        vec!["--version"],
        vec!["version", "ignored"],
    ] {
        assert_output(
            command(Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")), &home.0)
                .args(arguments)
                .output()
                .unwrap(),
            &first_line(),
        );
    }
}

#[test]
fn installed_identity_reads_its_own_manifest_and_falsey_commit_source() {
    for (marker, origin) in [
        (
            r#"{"version":"stale-marker","source":"ignored","source_commit":"0123456789abcdef"}"#,
            "source commit 0123456789abcdef",
        ),
        (
            r#"{"source_commit":null,"source":"pseudolife-mcp==0.16.1"}"#,
            "source pseudolife-mcp==0.16.1",
        ),
        (r#"{"source_commit":"","source":""}"#, "source unknown"),
        (r#"{"source_commit":false,"source":true}"#, "source True"),
        (r#"{"source_commit":42}"#, "source commit 42"),
    ] {
        let home = DisposableHome::new();
        let runtime = home.path(if cfg!(windows) {
            "local/pseudolife-mcp/runtimes/000001"
        } else {
            "data/pseudolife-mcp/runtimes/000001"
        });
        let executable = installed_executable(&runtime);
        fs::write(runtime.join("runtime.json"), marker).unwrap();
        let expected = format!("{}runtime {} ({origin})\n", first_line(), runtime.display());
        assert_output(
            command(&executable, &home.0)
                .arg("version")
                .output()
                .unwrap(),
            &expected,
        );
    }
}

#[test]
fn missing_malformed_nonobject_and_unmanaged_markers_do_not_claim_runtime_identity() {
    for (name, marker) in [
        ("000001", None),
        ("000001", Some(b"{".as_slice())),
        ("000001", Some(b"[]".as_slice())),
        ("000001", Some(b"\xff".as_slice())),
        ("00001", Some(b"{}".as_slice())),
        ("1234567", Some(b"{}".as_slice())),
        ("manual", Some(b"{}".as_slice())),
    ] {
        let home = DisposableHome::new();
        let runtime = home.path("runtime-root").join(name);
        let executable = installed_executable(&runtime);
        if let Some(marker) = marker {
            fs::write(runtime.join("runtime.json"), marker).unwrap();
        }
        let mut process = command(&executable, &home.0);
        process.env("PSEUDOLIFE_SHIM_RUNTIMES", runtime.parent().unwrap());
        process.env(
            "PSEUDOLIFE_SHIM_LAUNCHER",
            home.path(if cfg!(windows) {
                "launcher.exe"
            } else {
                "launcher"
            }),
        );
        assert_output(process.arg("version").output().unwrap(), &first_line());
    }
}

#[test]
fn overrides_select_the_layout_and_half_overrides_keep_the_default() {
    let home = DisposableHome::new();
    let default = home.path(if cfg!(windows) {
        "local/pseudolife-mcp/runtimes/000001"
    } else {
        "data/pseudolife-mcp/runtimes/000001"
    });
    let custom = home.path("custom/０００００１");
    for runtime in [&default, &custom] {
        let executable = installed_executable(runtime);
        fs::write(
            runtime.join("runtime.json"),
            r#"{"source_commit":"fixture-commit"}"#,
        )
        .unwrap();
        let mut process = command(&executable, &home.0);
        process.env("PSEUDOLIFE_SHIM_RUNTIMES", custom.parent().unwrap());
        if runtime == &custom {
            process.env(
                "PSEUDOLIFE_SHIM_LAUNCHER",
                home.path(if cfg!(windows) {
                    "launcher.exe"
                } else {
                    "launcher"
                }),
            );
        }
        assert_output(
            process.arg("version").output().unwrap(),
            &format!(
                "{}runtime {} (source commit fixture-commit)\n",
                first_line(),
                runtime.display()
            ),
        );
        let mut invalid = command(&executable, &home.0);
        invalid
            .env("PSEUDOLIFE_SHIM_RUNTIMES", runtime.parent().unwrap())
            .env(
                "PSEUDOLIFE_SHIM_LAUNCHER",
                home.path(if cfg!(windows) {
                    "launcher"
                } else {
                    "launcher.exe"
                }),
            );
        assert_output(invalid.arg("--version").output().unwrap(), &first_line());
    }
}

#[test]
fn a_valid_manifest_above_the_installed_executable_is_the_running_identity() {
    let home = DisposableHome::new();
    let runtime = home.path("runtimes/000001");
    let executable = installed_executable(&runtime);
    fs::write(
        runtime.join("runtime.json"),
        r#"{"source_commit":"installed-runtime-commit"}"#,
    )
    .unwrap();

    let mut process = command(&executable, &home.0);
    process
        .env("PSEUDOLIFE_SHIM_RUNTIMES", runtime.parent().unwrap())
        .env(
            "PSEUDOLIFE_SHIM_LAUNCHER",
            home.path(if cfg!(windows) {
                "launcher.exe"
            } else {
                "launcher"
            }),
        );
    assert_output(
        process.arg("version").output().unwrap(),
        &format!(
            "{}runtime {} (source commit installed-runtime-commit)\n",
            first_line(),
            runtime.display()
        ),
    );
}

#[test]
fn a_manifest_and_native_executable_without_a_console_file_do_not_claim_runtime_identity() {
    for console_is_directory in [false, true] {
        let home = DisposableHome::new();
        let runtime = home.path(if cfg!(windows) {
            "local/pseudolife-mcp/runtimes/000001"
        } else {
            "data/pseudolife-mcp/runtimes/000001"
        });
        let executable = native_executable(&runtime);
        fs::write(
            runtime.join("runtime.json"),
            r#"{"source_commit":"incomplete-runtime-commit"}"#,
        )
        .unwrap();
        if console_is_directory {
            fs::create_dir(console_path(&runtime)).unwrap();
        }
        assert_output(
            command(&executable, &home.0)
                .arg("version")
                .output()
                .unwrap(),
            &first_line(),
        );
    }
}

#[test]
fn a_binary_at_the_runtime_root_does_not_claim_installed_identity() {
    let home = DisposableHome::new();
    let runtime = home.path("runtimes/000001");
    fs::create_dir_all(&runtime).unwrap();
    fs::write(
        runtime.join("runtime.json"),
        r#"{"source_commit":"fixture-commit"}"#,
    )
    .unwrap();
    let executable = runtime.join(if cfg!(windows) {
        "pseudolife-stdio.exe"
    } else {
        "pseudolife-stdio"
    });
    install_shim(&executable);
    assert_output(
        command(&executable, &home.0)
            .env("PSEUDOLIFE_SHIM_RUNTIMES", runtime.parent().unwrap())
            .env(
                "PSEUDOLIFE_SHIM_LAUNCHER",
                home.path(if cfg!(windows) {
                    "launcher.exe"
                } else {
                    "launcher"
                }),
            )
            .arg("version")
            .output()
            .unwrap(),
        &first_line(),
    );
}
