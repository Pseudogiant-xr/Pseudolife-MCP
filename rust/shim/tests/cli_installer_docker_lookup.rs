//! The installer steps run `docker` only from an absolute `PATH` directory.
//!
//! A program named without a path is otherwise also looked up where the
//! loader searches by itself: the running program's own directory, a
//! relative `PATH` entry and (on Windows) the system and Windows directories,
//! where the hosted `windows-latest` image keeps `docker.exe`; Python's
//! `CreateProcess` also searched the current directory. Each case plants a
//! `docker` in one of those places, with `PATH` holding none, and checks that
//! every step reports Docker as unreachable rather than running the plant.
//! The plant succeeds and prints text, so a step that ran it answers
//! differently on every platform, and on Unix it also leaves a marker file
//! per step. The control puts the same plant on an absolute `PATH` directory
//! and checks that every step runs it.
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
};

/// `volume-names` when `docker` cannot be started at all.
const UNREACHED: &str = "FAIL Cannot inspect Docker safely.";
/// `engine detect` when `docker` cannot be started at all.
const NO_DOCKER: &str = "Cannot reach Docker; start Docker and rerun the installer.";
/// `preflight-check` when `docker` cannot be started at all.
const PREFLIGHT_UNSAFE: &str = "Cannot inspect Docker safely.";
/// The variable naming the file the Unix plant creates when it is run.
const MARKER: &str = "INSTALLER_LOOKUP_MARKER";
const STEPS: [&str; 3] = ["volume-names", "engine", "preflight-check"];

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

/// A `docker` that succeeds and prints text whatever it is asked: on Windows
/// a renamed copy of `cmd.exe` (with no `/c` it prints its banner and exits 0
/// at the end of its empty input), elsewhere a shell script that also creates
/// the `MARKER` file.
fn plant(dir: &Path) {
    fs::create_dir_all(dir).unwrap();
    if cfg!(windows) {
        let root = std::env::var_os("SystemRoot").expect("SystemRoot");
        fs::copy(
            Path::new(&root).join("System32").join("cmd.exe"),
            dir.join("docker.exe"),
        )
        .unwrap();
    } else {
        let path = dir.join("docker");
        fs::write(
            &path,
            format!("#!/bin/sh\n: > \"${MARKER}\"\necho planted\nexit 0\n"),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        }
    }
}

fn run(
    program: &Path,
    root: &Path,
    cwd: &Path,
    path: &std::ffi::OsStr,
    step: &str,
    args: &[&str],
) -> Output {
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
        .env(MARKER, root.join(format!("ran-{step}")))
        .arg("installer")
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

/// Each step's stderr, and which steps ran the Unix plant.
struct Answers {
    volumes: String,
    engine: String,
    preflight: String,
    ran: Vec<&'static str>,
}

fn answers(program: &Path, root: &Path, cwd: &Path, path: &std::ffi::OsStr) -> Answers {
    let repo = root.join("home/repo");
    let repo = repo.to_str().unwrap();
    let env_file = root.join("home/repo/ops/.env");
    let env_file = env_file.to_str().unwrap();
    let stderr = |output: Output| String::from_utf8_lossy(&output.stderr).into_owned();
    let volumes = stderr(run(
        program,
        root,
        cwd,
        path,
        STEPS[0],
        &["volume-names", "--repo", repo, "--env-file", env_file],
    ));
    let engine = stderr(run(
        program,
        root,
        cwd,
        path,
        STEPS[1],
        &["engine", "detect", "--repo", repo],
    ));
    let preflight = stderr(run(
        program,
        root,
        cwd,
        path,
        STEPS[2],
        &["preflight-check", "--repo", repo, "--env-file", env_file],
    ));
    Answers {
        volumes,
        engine,
        preflight,
        ran: STEPS
            .into_iter()
            .filter(|step| root.join(format!("ran-{step}")).exists())
            .collect(),
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
        "{where_}: preflight-check ran the planted docker: {preflight}"
    );
    assert!(
        ran.is_empty(),
        "{where_}: these steps ran the planted docker: {ran:?}"
    );
}

#[test]
fn a_docker_in_the_current_directory_is_never_run() {
    // A guard: Rust's own lookup already skips the current directory, which
    // Python's CreateProcess searched, so this case is not red against the
    // bare-name start; it keeps the native steps from regaining that search.
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
    // Windows' loader also searches the running program's own directory, as
    // it searches the system directories: a docker reachable only there is
    // not on PATH, so it is not run. (Unix never searches there.)
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
    // The control: the same plant where the lookup does search is run by
    // every step, so the cases above would see it.
    let root = scratch("control");
    let tools = root.0.join("tools");
    plant(&tools);
    let Answers {
        volumes,
        engine,
        preflight,
        ran,
    } = answers(
        Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")),
        &root.0,
        &root.0.join("home"),
        tools.as_os_str(),
    );
    assert!(
        !volumes.starts_with(UNREACHED),
        "volume-names did not run docker from PATH: {volumes}"
    );
    assert!(
        !engine.starts_with(NO_DOCKER),
        "engine detect did not run docker from PATH: {engine}"
    );
    assert!(
        !preflight.contains(PREFLIGHT_UNSAFE),
        "preflight-check did not run docker from PATH: {preflight}"
    );
    // Only the shell-script plant leaves the files.
    let expected: Vec<&str> = if cfg!(windows) {
        Vec::new()
    } else {
        STEPS.to_vec()
    };
    assert_eq!(ran, expected, "the steps whose plant marker exists");
}
