//! The real hosts for a move's transaction: this host's Docker bank, and
//! the target's helper over ssh ([`super::engine::Hosts`]).
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::Value;
use sha2::{Digest, Sha256};

use super::engine::{Artifact, Dump, Hosts};
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
        let output = std::process::Command::new(schtasks)
            .args(["/Query", "/TN", TASK, "/XML"])
            .stdin(std::process::Stdio::null())
            .output();
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
        Op::Receive { .. } | Op::Snapshot | Op::Restore => BULK,
        _ => Duration::from_secs(10 * 60),
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
    fn bank(&mut self) -> Bank<'_, docker::Real> {
        Bank {
            docker: &mut self.docker,
            pg: self.source.pg.clone(),
            daemon: self.source.daemon.clone(),
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
        let daemon = self.source.daemon.clone();
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
        let daemon = self.source.daemon.clone();
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
        let daemon = self.source.daemon.clone();
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
        let daemon = self.source.daemon.clone();
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

    fn source_policy(&mut self, policy: &str) -> Result<(), String> {
        let daemon = self.source.daemon.clone();
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
        let output = std::process::Command::new(found)
            .args(&args)
            .stdin(std::process::Stdio::null())
            .output()
            .map_err(|error| error.to_string())?;
        if output.status.success() {
            Ok(())
        } else {
            Err(String::from_utf8_lossy(&output.stderr).trim().to_owned())
        }
    }

    fn dump(&mut self, dir: &Path) -> Result<Dump, String> {
        crate::board::state::private_dir(dir)
            .map_err(|_| format!("cannot create the private folder {}", dir.display()))?;
        self.terminate_others()?;
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
            for _ in 0..10 {
                self.terminate_others()?;
                if self.sessions()? == 0 {
                    return Ok(());
                }
                std::thread::sleep(Duration::from_secs(1));
            }
            return Err(format!("sessions to {db} remain after the fence"));
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

    fn moved_marker(&mut self) -> Result<Option<String>, String> {
        let daemon = self.source.daemon.clone();
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
            (false, 0) => serde_json::from_slice::<Value>(&ran.stdout)
                .ok()
                .and_then(|doc| {
                    doc.get("move_id")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
                .map(Some)
                .ok_or_else(|| "/data/moved.json does not name a move".to_owned()),
            (false, 3) => Ok(None),
            _ => Err(probe::why(&ran)),
        }
    }

    fn moved(&mut self, body: Option<&str>) -> Result<(), String> {
        let daemon = self.source.daemon.clone();
        // By ID: a helper over the daemon's volumes never pulls a tag.
        let image = self.image_id()?;
        match body {
            Some(body) => {
                let local = self.store.moves.join("moved.json.part");
                let _ = std::fs::remove_file(&local);
                std::fs::write(&local, format!("{body}\n"))
                    .map_err(|error| format!("cannot write {}: {error}", local.display()))?;
                let local_text = local.to_string_lossy().into_owned();
                let ran = self.run(
                    &["cp", &local_text, &format!("{daemon}:/data/moved.json")],
                    SHORT,
                );
                let _ = std::fs::remove_file(&local);
                if ran.ok() {
                    Ok(())
                } else {
                    Err(format!(
                        "docker cp of moved.json failed ({})",
                        probe::why(&ran)
                    ))
                }
            }
            None => {
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
        let output = std::process::Command::new(program)
            .args(["connect", url, "--yes"])
            .stdin(std::process::Stdio::null())
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
