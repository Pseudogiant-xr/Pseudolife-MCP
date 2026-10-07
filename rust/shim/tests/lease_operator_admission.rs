#![forbid(unsafe_code)]
mod common;
use common::{LeaseHome as Home, cli::native_text};
use std::path::PathBuf;

const NO_BANK: &str = "no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, or run where the lite tier's data dir holds one (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp lease ...` on its host)\n";
const DEPRECATED: &str =
    "pseudolife-mcp lease designate is deprecated: use pseudolife-mcp lease delegate\n";

fn docker_fixture(home: &Home) -> PathBuf {
    #[cfg(windows)]
    let (name, script) = (
        "owned-docker.cmd",
        "@echo off\r\necho called>>\"%LEASE_DOCKER_CALLS%\"\r\nif \"%~1\"==\"inspect\" (echo true& exit /b 0)\r\n:args\r\nif \"%~1\"==\"\" exit /b 3\r\n>>\"%LEASE_DOCKER_ARGUMENTS%\" echo %~1\r\nshift\r\ngoto args\r\n",
    );
    #[cfg(unix)]
    let (name, script) = (
        "owned-docker",
        "#!/bin/sh\nprintf 'called\\n' >> \"$LEASE_DOCKER_CALLS\"\nif [ \"$1\" = inspect ]; then printf 'true\\n'; exit 0; fi\nprintf '%s\\n' \"$@\" > \"$LEASE_DOCKER_ARGUMENTS\"\nexit 3\n",
    );
    let path = home.0.join(name);
    std::fs::write(&path, script).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    path
}

#[test]
fn lease_home_blocks_container_even_when_an_executable_is_available() {
    let home = Home::new();
    let fake = docker_fixture(&home);
    let calls = home.0.join("calls");
    let arguments = home.0.join("arguments");
    let proof = common::cli::cleared_command(&fake)
        .env("LEASE_DOCKER_CALLS", &calls)
        .env("LEASE_DOCKER_ARGUMENTS", &arguments)
        .arg("inspect")
        .output()
        .unwrap();
    assert!(proof.status.success());
    let before = std::fs::read(&calls).unwrap();
    for board in [false, true] {
        for action in ["break", "delegate", "designate"] {
            let mut command = if board {
                home.board_command("http://127.0.0.1:1")
            } else {
                home.command()
            };
            assert_eq!(
                command
                    .get_envs()
                    .find(|(name, _)| *name == "PSEUDOLIFE_DAEMON_EXEC")
                    .unwrap()
                    .1,
                Some(std::ffi::OsStr::new("1"))
            );
            let invocation = if action == "break" {
                vec!["lease", action, "resource"]
            } else {
                vec!["lease", action, "project", "fixture-agent"]
            };
            let output = command
                .current_dir(&home.0)
                .env("PSEUDOLIFE_DOCKER", &fake)
                .env("LEASE_DOCKER_CALLS", &calls)
                .env("LEASE_DOCKER_ARGUMENTS", &arguments)
                .args(&invocation)
                .output()
                .unwrap();
            assert_eq!(output.status.code(), Some(1));
            assert!(output.stdout.is_empty());
            assert_eq!(
                output.stderr,
                native_text(&format!(
                    "{}{NO_BANK}",
                    if action == "designate" {
                        DEPRECATED
                    } else {
                        ""
                    }
                ))
            );
            assert_eq!(std::fs::read(&calls).unwrap(), before);
            assert!(!arguments.exists());
        }
    }
}

#[test]
fn designate_container_receives_delegate_and_emits_one_deprecation() {
    let home = Home::new();
    let fake = docker_fixture(&home);
    let arguments = home.0.join("arguments");
    let output = home
        .command()
        .current_dir(&home.0)
        .env_remove("PSEUDOLIFE_DAEMON_EXEC")
        .env("PSEUDOLIFE_DOCKER", &fake)
        .env("LEASE_DOCKER_CALLS", home.0.join("calls"))
        .env("LEASE_DOCKER_ARGUMENTS", &arguments)
        .args([
            "lease",
            "designate",
            "project",
            "fixture-agent",
            "--for",
            "60",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(3));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        native_text(&format!(
            "{DEPRECATED}pseudolife-mcp lease: no bank in this shell; running it inside the pseudolife-mcp-daemon container\n"
        ))
    );
    let forwarded = std::fs::read_to_string(arguments).unwrap();
    assert_eq!(
        forwarded.lines().collect::<Vec<_>>(),
        vec![
            "exec",
            "-i",
            "-e",
            "PSEUDOLIFE_DAEMON_EXEC=1",
            "pseudolife-mcp-daemon",
            "python",
            "-m",
            "pseudolife_memory.cli",
            "lease",
            "delegate",
            "project",
            "fixture-agent",
            "--for",
            "60",
        ]
    );
}

#[test]
fn malformed_dsn_fragments_are_never_operator_diagnostics() {
    for dsn in [
        "host=db password=correct horse",
        "fixture_password_fragment=synthetic",
        "postgresql://fixture@localhost/db?fixture_password_fragment=synthetic",
    ] {
        for action in ["break", "designate"] {
            let home = Home::new();
            let mut command = home.command();
            command
                .env("PSEUDOLIFE_MCP_DATABASE_URL", dsn)
                .args(["lease", action, "resource"]);
            if action == "designate" {
                command.arg("fixture-agent");
            }
            let output = command.output().unwrap();
            assert_eq!(output.status.code(), Some(1));
            assert!(output.stdout.is_empty());
            assert_eq!(
                output.stderr,
                native_text(&format!(
                    "{}cannot open the bank (unsupported PostgreSQL DSN option)\n",
                    if action == "designate" {
                        DEPRECATED
                    } else {
                        ""
                    }
                ))
            );
            assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
        }
    }
}
