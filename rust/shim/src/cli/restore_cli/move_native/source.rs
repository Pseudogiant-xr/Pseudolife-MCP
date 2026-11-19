//! The real hosts for a move's transaction: this host's Docker bank, and
//! the target's helper over ssh ([`super::engine::Hosts`]).
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::Value;
use sha2::{Digest, Sha256};

use super::engine::{Artifact, Dump, Hosts, Marker};
use super::probe::{self, Facts};
use super::protocol::{Answer, Op, Request};
use super::record::{Files, Record, Store};
use super::remote::{self, Remote};
use super::target::FRAME;
use crate::cli::restore_cli::bank::Bank;
use crate::cli::restore_cli::docker::{self, Docker, Input, Output};
use crate::cli::restore_cli::{Verdict, dump, safety};

const SHORT: Duration = Duration::from_secs(120);
const STOP: Duration = Duration::from_secs(300);
const BULK: Duration = Duration::from_secs(4 * 60 * 60);
const TIMER: &str = "pseudolife-update.timer";
const TASK: &str = "Pseudolife Unattended Update";
/// The prefix of the state archive a move takes (never matched by
/// `backup`'s rotation).
const MOVE_PREFIX: &str = "pseudolife_move";

/// This host's unattended update, as move pauses it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct Schedule {
    pub enabled: bool,
    /// The command that re-enables it, for a person.
    pub enable_command: String,
}

/// Whether this host's unattended update is enabled.
pub(super) fn schedule() -> Schedule {
    if cfg!(windows) {
        let Some(schtasks) = crate::cli::program_lookup::find_program_path("schtasks") else {
            return Schedule::default();
        };
        let mut query = std::process::Command::new(schtasks);
        query
            .args(["/Query", "/TN", TASK, "/XML"])
            .stdin(std::process::Stdio::null());
        no_window(&mut query);
        let output = query.output();
        let enabled = output.is_ok_and(|o| {
            o.status.success()
                && !task_text(&o.stdout)
                    .to_ascii_lowercase()
                    .contains("<enabled>false</enabled>")
        });
        return Schedule {
            enabled,
            enable_command: format!("schtasks /Change /TN \"{TASK}\" /ENABLE"),
        };
    }
    let Some(systemctl) = crate::cli::program_lookup::find_program_path("systemctl") else {
        return Schedule::default();
    };
    let enabled = std::process::Command::new(systemctl)
        .args(["--user", "is-enabled", TIMER])
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .is_ok_and(|o| String::from_utf8_lossy(&o.stdout).trim() == "enabled");
    Schedule {
        enabled,
        enable_command: format!("systemctl --user enable --now {TIMER}"),
    }
}

/// schtasks' output: UTF-16 (with or without its byte-order mark) or UTF-8.
fn task_text(bytes: &[u8]) -> String {
    let body = bytes.strip_prefix(&[0xff, 0xfe]).unwrap_or(bytes);
    let utf16 = bytes.starts_with(&[0xff, 0xfe])
        || (body.len() >= 2
            && body.len().is_multiple_of(2)
            && body.iter().skip(1).step_by(2).all(|b| *b == 0));
    if utf16 {
        String::from_utf16_lossy(
            &body
                .chunks_exact(2)
                .map(|b| u16::from_le_bytes([b[0], b[1]]))
                .collect::<Vec<_>>(),
        )
    } else {
        String::from_utf8_lossy(bytes).into_owned()
    }
}

/// A database name the fence's SQL may quote (Python's `fence_sql` rule).
pub(super) fn valid_database(name: &str) -> bool {
    let bytes = name.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 63
        && (bytes[0].is_ascii_lowercase() || bytes[0] == b'_')
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
}

fn sha256_of(path: &Path) -> Result<(String, u64), String> {
    let mut file = std::fs::File::open(path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    let mut digest = Sha256::new();
    let bytes = std::io::copy(&mut file, &mut digest)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    let hex = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Ok((hex, bytes))
}

/// How long one target operation may take.
fn op_time(op: &Op) -> Duration {
    match op {
        Op::Receive { .. } | Op::Snapshot | Op::Restore | Op::GiveBack => BULK,
        _ => Duration::from_secs(10 * 60),
    }
}

fn write_moved_marker(
    docker: &mut impl Docker,
    daemon_id: &str,
    local: &Path,
    body: &str,
) -> Result<(), String> {
    if !probe::container_id(daemon_id) {
        return Err(
            "the source daemon was not pinned to a Docker container ID; rerun move --dry-run"
                .into(),
        );
    }
    let state = docker.run(
        &[
            "inspect".into(),
            "-f".into(),
            "{{.State.Running}}".into(),
            daemon_id.into(),
        ],
        SHORT,
    );
    if !state.ok() || String::from_utf8_lossy(&state.stdout).trim() != "false" {
        return Err("the selected source daemon is not provably stopped; restore its original container and rerun move --rollback".into());
    }
    let _ = std::fs::remove_file(local);
    std::fs::write(local, format!("{body}\n"))
        .map_err(|error| format!("cannot write {}: {error}", local.display()))?;
    let copied = docker.run(
        &[
            "cp".into(),
            local.to_string_lossy().into_owned(),
            format!("{daemon_id}:/data/moved.json"),
        ],
        SHORT,
    );
    let _ = std::fs::remove_file(local);
    if copied.ok() {
        Ok(())
    } else {
        Err(format!(
            "docker cp of moved.json failed ({})",
            probe::why(&copied)
        ))
    }
}

/// The real hosts.
pub(super) struct Live {
    pub docker: docker::Real,
    pub ssh: remote::Ssh,
    pub source: Facts,
    pub to: String,
    pub store: Files,
}

impl Live {
    fn daemon(&self) -> &str {
        self.source.daemon_selector()
    }

    fn bank(&mut self) -> Bank<'_, docker::Real> {
        let daemon = self.daemon().to_owned();
        Bank {
            docker: &mut self.docker,
            pg: self.source.pg.clone(),
            daemon,
        }
    }

    fn run(&mut self, args: &[&str], timeout: Duration) -> docker::Ran {
        let args: Vec<String> = args.iter().map(|a| (*a).to_owned()).collect();
        self.docker.run(&args, timeout)
    }

    /// One SQL statement as the bank's role, against `database`.
    fn sql(&mut self, database: &str, statement: &str) -> Result<String, String> {
        let (user, port) = (self.source.user.clone(), self.source.port);
        let bank = self.bank();
        let argv = bank.psql_argv(
            &user,
            port,
            database,
            &["-v", "ON_ERROR_STOP=1", "-At", "-c", statement],
        );
        let ran = self.docker.run(&argv, SHORT);
        if ran.ok() {
            Ok(String::from_utf8_lossy(&ran.stdout).trim().to_owned())
        } else {
            Err(probe::why(&ran))
        }
    }

    fn terminate_others(&mut self) -> Result<(), String> {
        let db = self.source.db.clone();
        self.sql(
            "postgres",
            &format!(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = '{db}' AND pid <> pg_backend_pid()"
            ),
        )
        .map(|_| ())
    }

    fn sessions(&mut self) -> Result<i64, String> {
        let db = self.source.db.clone();
        self.sql(
            "postgres",
            &format!("SELECT count(*) FROM pg_stat_activity WHERE datname = '{db}' AND pid <> pg_backend_pid()"),
        )?
        .parse()
        .map_err(|_| "pg_stat_activity answered something that is not a count".to_owned())
    }

    /// The source daemon container's image, by ID.
    fn image_id(&mut self) -> Result<String, String> {
        let daemon = self.daemon().to_owned();
        let ran = self.run(&["inspect", "-f", "{{.Image}}", &daemon], SHORT);
        let image = String::from_utf8_lossy(&ran.stdout).trim().to_owned();
        if ran.ok() && !image.is_empty() {
            Ok(image)
        } else {
            Err(format!(
                "docker inspect {daemon} failed ({})",
                probe::why(&ran)
            ))
        }
    }

    fn inspect_running(&mut self) -> Option<bool> {
        let daemon = self.daemon().to_owned();
        let ran = self.run(&["inspect", "-f", "{{.State.Running}}", &daemon], SHORT);
        match String::from_utf8_lossy(&ran.stdout).trim() {
            "true" if ran.ok() => Some(true),
            "false" if ran.ok() => Some(false),
            _ => None,
        }
    }
}

impl Hosts for Live {
    fn source_stop(&mut self) -> Result<(), String> {
        let daemon = self.daemon().to_owned();
        let ran = self.run(&["stop", "-t", "120", &daemon], STOP);
        match self.inspect_running() {
            Some(false) => Ok(()),
            _ => Err(format!(
                "the source daemon did not stop ({})",
                probe::why(&ran)
            )),
        }
    }

    fn source_start(&mut self) -> Result<(), String> {
        let daemon = self.daemon().to_owned();
        let ran = self.run(&["start", &daemon], SHORT);
        if ran.ok() {
            Ok(())
        } else {
            Err(format!(
                "docker start {daemon} failed ({})",
                probe::why(&ran)
            ))
        }
    }

    fn source_running(&mut self) -> Option<bool> {
        self.inspect_running()
    }

    fn source_healthy(&mut self, wait: Duration) -> bool {
        let url = format!("{}/health", self.source.health_url);
        let end = Instant::now() + wait;
        loop {
            if let Some(answer) = crate::cli::restore_cli::http_json(&url, Duration::from_secs(5))
                && answer.get("status").and_then(Value::as_str) == Some("ok")
                && answer.get("ready").and_then(Value::as_bool) != Some(false)
            {
                return true;
            }
            if Instant::now() >= end {
                return false;
            }
            std::thread::sleep(Duration::from_secs(2));
        }
    }

    fn source_bank(&mut self) -> Option<String> {
        let url = format!("{}/health", self.source.health_url);
        crate::cli::restore_cli::http_json(&url, Duration::from_secs(5))?
            .get("bank")
            .and_then(Value::as_str)
            .map(str::to_owned)
    }

    fn source_policy(&mut self, policy: &str) -> Result<(), String> {
        let daemon = self.daemon().to_owned();
        let ran = self.run(&["update", &format!("--restart={policy}"), &daemon], SHORT);
        if ran.ok() {
            Ok(())
        } else {
            Err(format!("docker update failed ({})", probe::why(&ran)))
        }
    }

    fn schedule(&mut self, enable: bool) -> Result<(), String> {
        let (program, args): (&str, Vec<&str>) = if cfg!(windows) {
            (
                "schtasks",
                vec![
                    "/Change",
                    "/TN",
                    TASK,
                    if enable { "/ENABLE" } else { "/DISABLE" },
                ],
            )
        } else {
            (
                "systemctl",
                vec![
                    "--user",
                    if enable { "enable" } else { "disable" },
                    "--now",
                    TIMER,
                ],
            )
        };
        let found = crate::cli::program_lookup::find_program_path(program)
            .ok_or_else(|| format!("no {program} on PATH"))?;
        let mut command = std::process::Command::new(found);
        command.args(&args).stdin(std::process::Stdio::null());
        no_window(&mut command);
        let output = command.output().map_err(|error| error.to_string())?;
        if output.status.success() {
            Ok(())
        } else {
            Err(String::from_utf8_lossy(&output.stderr).trim().to_owned())
        }
    }

    fn dump(&mut self, dir: &Path) -> Result<Dump, String> {
        crate::board::state::private_dir(dir)
            .map_err(|_| format!("cannot create the private folder {}", dir.display()))?;
        // Ended, not just told to end: a session from before the read-only
        // setting could otherwise still commit after the dump's snapshot.
        let ended = end_sessions(self, Self::terminate_others, Self::sessions, || {
            std::thread::sleep(Duration::from_secs(1))
        })?;
        if !ended {
            return Err(format!(
                "sessions to {} remain after they were ended, so the final dump could miss their writes",
                self.source.db
            ));
        }
        let path = dir.join("pseudolife_memory.sql.gz");
        let part = dir.join("pseudolife_memory.sql.gz.part");
        let _ = std::fs::remove_file(&part);
        let port = self.source.port.to_string();
        let target = crate::cli::restore_cli::conn::database_arg(&self.source.db);
        let argv: Vec<String> = [
            "exec",
            &self.source.pg,
            "sh",
            "-c",
            safety::DUMP,
            "_",
            "/var/run/postgresql",
            &port,
            &self.source.user,
            &target,
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        let ran = self
            .docker
            .run_io(&argv, BULK, Input::Null, Output::File(&part));
        if !ran.ok() {
            let _ = std::fs::remove_file(&part);
            return Err(format!("the final dump failed ({})", probe::why(&ran)));
        }
        let (verdict, counts) = dump::complete_with_counts(&part);
        if verdict != Verdict::Pass {
            let _ = std::fs::remove_file(&part);
            return Err(format!(
                "the final dump is incomplete; check docker logs {} and the free space on {}",
                self.source.pg,
                dir.display()
            ));
        }
        // A writable handle: Windows flushes only through one.
        safety::flush(&part)
            .and_then(|()| std::fs::rename(&part, &path))
            .map_err(|error| format!("cannot keep the final dump: {error}"))?;
        let (sha256, bytes) = sha256_of(&path)?;
        Ok(Dump {
            path,
            sha256,
            bytes,
            counts,
        })
    }

    fn fence(&mut self, on: bool) -> Result<(), String> {
        let db = self.source.db.clone();
        if !valid_database(&db) {
            return Err(format!(
                "the database name {db:?} cannot be fenced safely; move needs a plain lower-case name"
            ));
        }
        let allow = if on { "false" } else { "true" };
        self.sql(
            "postgres",
            &format!("ALTER DATABASE \"{db}\" WITH ALLOW_CONNECTIONS {allow}"),
        )
        .map_err(|why| format!("cannot set ALLOW_CONNECTIONS {allow} on {db}: {why}"))?;
        if on {
            // Every session the fence now refuses to replace must end.
            let ended = end_sessions(self, Self::terminate_others, Self::sessions, || {
                std::thread::sleep(Duration::from_secs(1))
            })?;
            if !ended {
                return Err(format!("sessions to {db} remain after the fence"));
            }
        }
        Ok(())
    }

    fn read_only(&mut self, on: bool) -> Result<(), String> {
        let db = self.source.db.clone();
        if !valid_database(&db) {
            return Err(format!("the database name {db:?} cannot be named safely"));
        }
        let statement = if on {
            format!("ALTER DATABASE \"{db}\" SET default_transaction_read_only = on")
        } else {
            format!("ALTER DATABASE \"{db}\" RESET default_transaction_read_only")
        };
        self.sql("postgres", &statement)
            .map(|_| ())
            .map_err(|why| format!("cannot change {db}'s default_transaction_read_only: {why}"))
    }

    fn archive(&mut self, dir: &Path) -> Result<Artifact, String> {
        let plan = safety::Target {
            pg: self.source.pg.clone(),
            port: self.source.port,
            user: self.source.user.clone(),
            db: self.source.db.clone(),
            image: self.image_id()?,
            directory: dir.to_string_lossy().into_owned(),
            include_database: false,
            prefix: MOVE_PREFIX,
        };
        let stamp = chrono::Utc::now().format("%Y%m%d-%H%M%S").to_string();
        let mut bank = self.bank();
        let pair = safety::backup(&mut bank, &plan, &stamp).map_err(|stop| stop.0)?;
        // The source's /data holds neither marker: either one would have
        // refused the daemon's start, and the preflight found it running.
        let (sha256, bytes) = sha256_of(&pair.state)?;
        Ok(Artifact {
            path: pair.state,
            sha256,
            bytes,
        })
    }

    fn write_file(&mut self, path: &Path, bytes: &[u8]) -> Result<Artifact, String> {
        let _ = std::fs::remove_file(path);
        let mut file = docker::private_file(path)
            .map_err(|error| format!("cannot write {}: {error}", path.display()))?;
        file.write_all(bytes)
            .and_then(|()| file.sync_all())
            .map_err(|error| format!("cannot write {}: {error}", path.display()))?;
        let (sha256, size) = sha256_of(path)?;
        Ok(Artifact {
            path: path.to_path_buf(),
            sha256,
            bytes: size,
        })
    }

    fn moved_marker(&mut self) -> Result<Marker, String> {
        let daemon = self.daemon().to_owned();
        let ran = self.run(&["inspect", "-f", "{{.Image}}", &daemon], SHORT);
        let image = String::from_utf8_lossy(&ran.stdout).trim().to_owned();
        if !ran.ok() || image.is_empty() {
            return Err(format!(
                "docker inspect {daemon} failed ({})",
                probe::why(&ran)
            ));
        }
        let ran = self.run(
            &[
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                "--volumes-from",
                &daemon,
                &image,
                "-c",
                "test -e /data/moved.json || exit 3; cat /data/moved.json",
            ],
            SHORT,
        );
        match (ran.fault.is_some(), ran.code) {
            // A file that names no move is no commit point: the move counts
            // its commit only once a whole marker was written.
            (false, 0) => Ok(serde_json::from_slice::<Value>(&ran.stdout)
                .ok()
                .and_then(|doc| {
                    doc.get("move_id")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
                .map_or(Marker::NamesNone, Marker::Names)),
            (false, 3) => Ok(Marker::Absent),
            _ => Err(probe::why(&ran)),
        }
    }

    fn moved(&mut self, body: Option<&str>) -> Result<(), String> {
        match body {
            Some(body) => {
                let local = self.store.moves.join("moved.json.part");
                write_moved_marker(&mut self.docker, &self.source.daemon_id, &local, body)
            }
            None => {
                let daemon = self.daemon().to_owned();
                // By ID: a helper over the daemon's volumes never pulls a tag.
                let image = self.image_id()?;
                let ran = self.run(
                    &[
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--entrypoint",
                        "rm",
                        "--volumes-from",
                        &daemon,
                        &image,
                        "-f",
                        "/data/moved.json",
                    ],
                    SHORT,
                );
                if ran.ok() {
                    Ok(())
                } else {
                    Err(format!(
                        "cannot remove /data/moved.json ({})",
                        probe::why(&ran)
                    ))
                }
            }
        }
    }

    fn target(&mut self, request: &Request, payload: Option<&Path>) -> Result<Answer, String> {
        let mut head = serde_json::to_vec(request).map_err(|e| e.to_string())?;
        head.push(b'\n');
        let command = remote::sh(&super::script(super::protocol::PROTOCOL));
        let to = self.to.clone();
        let ran = self
            .ssh
            .run_stream(&to, &command, &head, payload, op_time(&request.op));
        let stdout = String::from_utf8_lossy(&ran.stdout).into_owned();
        // The helper's answer counts even if ssh then failed.
        if let Some(framed) = stdout.lines().find_map(|line| line.strip_prefix(FRAME)) {
            return serde_json::from_str(framed)
                .map_err(|_| format!("the helper on {to} answered something unreadable"));
        }
        if ran.lost() {
            return Err(remote::lost(&to, &ran));
        }
        Err(format!(
            "the helper on {to} gave no answer (exit {}); its outcome is unknown",
            ran.code
        ))
    }

    fn target_health(&mut self, url: &str) -> Option<Value> {
        super::get_json(url).ok()
    }

    fn repoint(&mut self, url: &str) -> Result<(), String> {
        let program = std::env::current_exe().map_err(|e| e.to_string())?;
        let mut command = std::process::Command::new(program);
        command
            .args(["connect", url, "--yes"])
            .stdin(std::process::Stdio::null());
        no_window(&mut command);
        let output = command
            .output()
            .map_err(|error| format!("connect could not be started: {error}"))?;
        match output.status.code() {
            // 3: no client registration on this host to re-point.
            Some(0 | 3) => Ok(()),
            code => Err(format!(
                "connect {}: {}",
                code.map_or_else(
                    || "was stopped by a signal".to_owned(),
                    |c| format!("exited {c}")
                ),
                String::from_utf8_lossy(&output.stderr).trim()
            )),
        }
    }

    fn save(&mut self, record: &Record) -> Result<(), String> {
        self.store.save(record)
    }

    fn interrupted(&mut self) -> bool {
        crate::cli::restore_cli::interrupt::requested()
    }

    fn now(&mut self) -> String {
        chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
    }

    fn sleep(&mut self, pause: Duration) {
        std::thread::sleep(pause);
    }

    fn say(&mut self, line: &str) {
        let _ = writeln!(std::io::stderr().lock(), "move: {line}");
    }
}

/// This host's folder for move `id`'s files.
pub(super) fn local_dir(store: &Files, id: &str) -> PathBuf {
    store.moves.join(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn moved_marker_only_targets_the_pinned_source_container() {
        struct TwoStacks {
            original: String,
            replacement: String,
            original_exists: bool,
            calls: Vec<Vec<String>>,
        }

        impl Docker for TwoStacks {
            fn run_io(
                &mut self,
                args: &[String],
                _: Duration,
                _: docker::Input<'_>,
                _: docker::Output<'_>,
            ) -> docker::Ran {
                self.calls.push(args.to_vec());
                let args: Vec<_> = args.iter().map(String::as_str).collect();
                match args.as_slice() {
                    ["inspect", "-f", "{{.State.Running}}", id]
                        if *id == self.original && self.original_exists =>
                    {
                        docker::Ran {
                            stdout: b"false\n".to_vec(),
                            ..docker::Ran::default()
                        }
                    }
                    ["inspect", "-f", "{{.State.Running}}", id]
                        if *id == self.replacement
                            || *id == super::super::super::bank::DAEMON_CONTAINER =>
                    {
                        docker::Ran {
                            stdout: b"true\n".to_vec(),
                            ..docker::Ran::default()
                        }
                    }
                    ["cp", _, _] => docker::Ran::default(),
                    _ => docker::Ran {
                        code: 1,
                        ..docker::Ran::default()
                    },
                }
            }
        }

        let mut docker = TwoStacks {
            original: "1".repeat(64),
            replacement: "2".repeat(64),
            original_exists: true,
            calls: vec![],
        };
        let dir = std::env::temp_dir().join(format!("pl-move-marker-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&dir).unwrap();
        let part = dir.join("moved.json.part");
        let original = docker.original.clone();
        write_moved_marker(&mut docker, &original, &part, "{}").unwrap();
        assert_eq!(docker.calls[1][0], "cp");
        assert_eq!(docker.calls[1][2], format!("{original}:/data/moved.json"));
        assert!(!part.exists());

        docker.calls.clear();
        docker.original_exists = false;
        assert!(write_moved_marker(&mut docker, &original, &part, "{}").is_err());
        assert_eq!(docker.calls.len(), 1);
        assert_eq!(docker.calls[0].last().unwrap(), &original);
        assert!(!part.exists());
        std::fs::remove_dir(dir).unwrap();
    }

    #[test]
    fn a_disabled_task_reads_as_disabled_in_utf16_or_utf8() {
        let xml = "<Task><Settings><Enabled>false</Enabled></Settings></Task>";
        let utf16: Vec<u8> = xml.encode_utf16().flat_map(u16::to_le_bytes).collect();
        let with_mark: Vec<u8> = [0xff, 0xfe]
            .into_iter()
            .chain(utf16.iter().copied())
            .collect();
        for bytes in [xml.as_bytes().to_vec(), utf16, with_mark] {
            assert!(
                task_text(&bytes)
                    .to_ascii_lowercase()
                    .contains("<enabled>false</enabled>"),
                "{bytes:?}"
            );
        }
    }

    #[test]
    fn only_plain_database_names_are_fenced() {
        for name in ["pseudolife_memory", "_x", "a1"] {
            assert!(valid_database(name), "{name}");
        }
        for name in ["", "A", "1a", "a-b", "a\"b", "a;b", &"a".repeat(64)] {
            assert!(!valid_database(name), "{name:?}");
        }
    }
}

/// A console child on Windows opens no window of its own when this process
/// has no console (a parent with none would give it one, which takes the
/// user's foreground); at a terminal it shares the terminal's console.
pub(super) fn no_window(command: &mut std::process::Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(super::super::child_window::child_window_flag());
    }
    #[cfg(not(windows))]
    let _ = command;
}

/// Ends the database's other sessions and waits until none is left (up to
/// ten tries, `pause` between them): `Ok(false)` when some remain.
fn end_sessions<T>(
    it: &mut T,
    terminate: fn(&mut T) -> Result<(), String>,
    count: fn(&mut T) -> Result<i64, String>,
    mut pause: impl FnMut(),
) -> Result<bool, String> {
    for _ in 0..10 {
        terminate(it)?;
        if count(it)? == 0 {
            return Ok(true);
        }
        pause();
    }
    Ok(false)
}

#[cfg(test)]
mod session_tests {
    use super::end_sessions;

    struct Db {
        /// Sessions left after each termination, in turn.
        left: Vec<i64>,
        terminated: usize,
    }

    fn terminate(db: &mut Db) -> Result<(), String> {
        db.terminated += 1;
        Ok(())
    }

    fn count(db: &mut Db) -> Result<i64, String> {
        Ok(if db.left.is_empty() {
            0
        } else {
            db.left.remove(0)
        })
    }

    #[test]
    fn the_dump_waits_until_the_ended_sessions_are_gone() {
        // Told to end, two sessions take a moment to go.
        let mut db = Db {
            left: vec![2, 1, 0],
            terminated: 0,
        };
        assert_eq!(end_sessions(&mut db, terminate, count, || {}), Ok(true));
        assert_eq!(db.terminated, 3);
        // One that never goes is reported, not dumped past.
        let mut db = Db {
            left: vec![1; 20],
            terminated: 0,
        };
        assert_eq!(end_sessions(&mut db, terminate, count, || {}), Ok(false));
        assert_eq!(db.terminated, 10);
    }
}

#[cfg(test)]
mod timeout_tests {
    use super::super::protocol::Op;

    #[test]
    fn the_give_back_gets_the_restores_time() {
        assert_eq!(super::op_time(&Op::GiveBack), super::op_time(&Op::Restore));
        assert!(super::op_time(&Op::Restore) > super::op_time(&Op::Stop));
    }
}
