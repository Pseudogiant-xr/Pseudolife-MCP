//! The installer steps run `docker` only from an absolute `PATH` directory.
//!
//! A program named without a path is otherwise also looked up where the
//! loader searches by itself: the current directory, the running program's
//! own directory and (on Windows) the system and Windows directories, where
//! the hosted `windows-latest` image keeps `docker.exe`. Each case plants a
//! `docker` that answers differently from a missing one, in one of those
//! places, with `PATH` holding none, and checks that the step reports Docker
//! as unreachable rather than running the plant. The control puts the same
//! plant on an absolute `PATH` directory and checks that it is run.
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
};

/// `volume-names` when `docker` cannot be started at all.
const UNREACHED: &str = "FAIL Cannot inspect Docker safely.";
/// `volume-names` when a `docker` ran and exited nonzero.
const RAN: &str = "FAIL Cannot resolve ops/.env with Docker Compose.";
/// `engine detect` when `docker` cannot be started at all.
const NO_DOCKER: &str = "Cannot reach Docker; start Docker and rerun the installer.";
/// `preflight-check` when `docker` cannot be started or fails.
const PREFLIGHT_UNSAFE: &str = "Cannot inspect Docker safely.";
/// `engine detect` when a `docker` ran and exited nonzero.
const ENGINE_RAN: &str = "Cannot inspect the existing daemon container";

struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn scratch(tag: &str) -> Scratch {
    let root = std::env::temp_dir().join(format!(
        "installer-docker-lookup-{tag}-{}",
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(root.join("home/repo/ops")).unwrap();
    fs::create_dir_all(root.join("empty-path")).unwrap();
    fs::write(root.join("home/repo/ops/.env.example"), "").unwrap();
    Scratch(root)
}

/// The file the shell-script plant creates when it is run.
const MARKER: &str = "INSTALLER_LOOKUP_MARKER";

/// A `docker` that runs and fails whatever it is asked: a copy of this
/// package's binary on Windows, a shell script elsewhere that also leaves
/// the `MARKER` file (the preflight check answers alike for a docker that
/// could not start and one that failed, so only the file tells them apart).
fn plant(dir: &Path) {
    fs::create_dir_all(dir).unwrap();
    if cfg!(windows) {
        fs::copy(
            env!("CARGO_BIN_EXE_pseudolife-stdio"),
            dir.join("docker.exe"),
        )
        .unwrap();
    } else {
        let path = dir.join("docker");
        fs::write(&path, format!("#!/bin/sh\n: > \"${MARKER}\"\nexit 3\n")).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        }
    }
}

fn run(program: &Path, root: &Path, cwd: &Path, path: &std::ffi::OsStr, args: &[&str]) -> Output {
    let home = root.join("home");
    let mut command = Command::new(program);
    command.env_clear();
    for key in ["SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    command
        .env("HOME", &home)
        .env("USERPROFILE", &home)
        .env("LOCALAPPDATA", home.join("Local"))
        .env("APPDATA", home.join("Roaming"))
        .env("PATH", path)
        .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1")
        .env(MARKER, root.join("ran"))
        .arg("installer")
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

/// What the steps answered, and whether the shell-script plant was run.
struct Answers {
    volumes: String,
    engine: String,
    preflight: String,
    ran: bool,
}

fn answers(program: &Path, root: &Path, cwd: &Path, path: &std::ffi::OsStr) -> Answers {
    let repo = root.join("home/repo");
    let repo = repo.to_str().unwrap();
    let env_file = root.join("home/repo/ops/.env");
    let volumes = run(
        program,
        root,
        cwd,
        path,
        &[
            "volume-names",
            "--repo",
            repo,
            "--env-file",
            env_file.to_str().unwrap(),
        ],
    );
    let engine = run(
        program,
        root,
        cwd,
        path,
        &["engine", "detect", "--repo", repo],
    );
    let preflight = run(
        program,
        root,
        cwd,
        path,
        &[
            "preflight-check",
            "--repo",
            repo,
            "--env-file",
            env_file.to_str().unwrap(),
        ],
    );
    Answers {
        volumes: String::from_utf8_lossy(&volumes.stderr).into_owned(),
        engine: String::from_utf8_lossy(&engine.stderr).into_owned(),
        preflight: String::from_utf8_lossy(&preflight.stderr).into_owned(),
        ran: root.join("ran").exists(),
    }
}

fn assert_not_run(where_: &str, answers: Answers) {
    let Answers {
        volumes,
        engine,
        preflight,
        ran,
    } = answers;
    assert!(
        volumes.starts_with(UNREACHED),
        "{where_}: volume-names ran the planted docker: {volumes}"
    );
    assert!(
        engine.starts_with(NO_DOCKER),
        "{where_}: engine detect ran the planted docker: {engine}"
    );
    assert!(
        preflight.contains(PREFLIGHT_UNSAFE),
        "{where_}: preflight-check answered {preflight}"
    );
    assert!(!ran, "{where_}: a step ran the planted docker");
}

#[test]
fn a_docker_in_the_current_directory_is_never_run() {
    let root = scratch("cwd");
    let home = root.0.join("home");
    plant(&home);
    let answers = answers(
        Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")),
        &root.0,
        &home,
        root.0.join("empty-path").as_os_str(),
    );
    assert_not_run("current directory", answers);
}

#[test]
fn a_docker_found_only_outside_path_beside_the_program_is_never_run() {
    // The loader also searches the running program's own directory, as it
    // searches the Windows system directories: a docker reachable only there
    // is not on PATH, so it is not run.
    let root = scratch("app");
    let app = root.0.join("app");
    fs::create_dir_all(&app).unwrap();
    let program = app.join(if cfg!(windows) {
        "pseudolife-mcp.exe"
    } else {
        "pseudolife-mcp"
    });
    fs::copy(env!("CARGO_BIN_EXE_pseudolife-stdio"), &program).unwrap();
    plant(&app);
    let answers = answers(
        &program,
        &root.0,
        &root.0.join("home"),
        root.0.join("empty-path").as_os_str(),
    );
    assert_not_run("beside the program", answers);
}

#[test]
fn a_docker_on_a_relative_path_entry_is_never_run() {
    let root = scratch("relative");
    let home = root.0.join("home");
    plant(&home.join("tools"));
    let answers = answers(
        Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")),
        &root.0,
        &home,
        std::ffi::OsStr::new("tools"),
    );
    assert_not_run("relative PATH entry", answers);
}

#[test]
fn a_docker_on_an_absolute_path_directory_is_run() {
    // The control: the same plant where the lookup does search is run, so
    // the cases above would see it.
    let root = scratch("control");
    let tools = root.0.join("tools");
    plant(&tools);
    let Answers {
        volumes,
        engine,
        ran,
        ..
    } = answers(
        Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")),
        &root.0,
        &root.0.join("home"),
        tools.as_os_str(),
    );
    // Only the shell-script plant leaves the file.
    assert_eq!(ran, !cfg!(windows), "the planted docker's marker");
    assert!(
        volumes.starts_with(RAN),
        "volume-names did not run docker from PATH: {volumes}"
    );
    assert!(
        engine.contains(ENGINE_RAN),
        "engine detect did not run docker from PATH: {engine}"
    );
}
