//! `pseudolife-mcp move-target <protocol>`: the target half of a move's
//! preflight, run by the source over ssh.
//!
//! Hidden: users never run it. It reads one JSON request on stdin (what the
//! source is: its schema, PostgreSQL version and bank fingerprint, and the
//! work directory the move would use), checks this host's bank without
//! changing anything, and prints one line, [`FRAME`] then a JSON reply.
//! The frame lets the source find the reply after anything a login script
//! prints. The reply carries verdicts and facts, never a secret. It exits 0
//! whatever it found (the verdicts say), 3 for a protocol it does not
//! speak, and never 255, which the source reads as ssh's own failure.
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::probe::{self, Facts};
use crate::cli::restore_cli::docker::{self, Docker};

/// The protocol this client speaks.
pub(in crate::cli) const PROTOCOL: u32 = 1;
/// The marker before the reply's JSON on its one line.
pub(in crate::cli) const FRAME: &str = "PSEUDOLIFE-MOVE-TARGET ";

/// What the source sends.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(in crate::cli) struct Ask {
    pub protocol: u32,
    pub source_schema: i64,
    pub source_pg_version: i64,
    pub source_bank: String,
    /// The work directory's parent (`~/...` or absolute); `""` for the
    /// default, `~/.pseudolife-mcp/moves`.
    pub work_dir: String,
    /// What the artifacts may take at most: the database and `/data`.
    pub need_bytes: u64,
}

/// What the target answers.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(in crate::cli) struct Reply {
    pub protocol: u32,
    pub version: String,
    /// Every reason this host cannot take the move; empty when it can.
    pub refusals: Vec<String>,
    pub facts: Option<Facts>,
    /// The work directory, resolved, and the free space there.
    pub work_dir: String,
    pub free_bytes: Option<u64>,
    /// `systemctl --user is-enabled pseudolife-update.timer`'s answer.
    pub update_timer: String,
}

/// What the target host offers the helper: its docker, a health GET, the
/// home directory, a path check and the update timer's state.
pub(super) trait Place {
    type D: Docker;
    fn docker(&mut self) -> Result<&mut Self::D, String>;
    fn health(&mut self, url: &str) -> Option<serde_json::Value>;
    fn home(&self) -> Option<PathBuf>;
    /// The nearest existing ancestor of `path`, canonical; the rest of
    /// `path` below it (still to be created); the free bytes there.
    fn nearest(&self, path: &Path) -> Option<(PathBuf, PathBuf, Option<u64>)>;
    /// `path` with its links resolved, or as given when it does not resolve.
    fn canonical(&self, path: &Path) -> PathBuf;
    /// Whether `path` or any ancestor holds a `.git` entry.
    fn in_checkout(&self, path: &Path) -> bool;
    fn update_timer(&mut self) -> String;
}

fn under(path: &Path, root: &str) -> bool {
    !root.is_empty() && (path == Path::new(root) || path.starts_with(root))
}

/// The target's verdict on `ask`.
pub(super) fn answer<P: Place>(place: &mut P, ask: &Ask) -> Reply {
    let mut reply = Reply {
        protocol: PROTOCOL,
        version: env!("CARGO_PKG_VERSION").to_owned(),
        ..Reply::default()
    };
    let home = place.home();
    let work = if ask.work_dir.is_empty() {
        home.as_ref()
            .map(|h| h.join(".pseudolife-mcp").join("moves"))
    } else if let Some(rest) = ask.work_dir.strip_prefix("~/") {
        home.as_ref().map(|h| h.join(rest))
    } else {
        Some(PathBuf::from(&ask.work_dir))
    };
    reply.update_timer = place.update_timer();
    let docker = match place.docker() {
        Ok(docker) => docker,
        Err(why) => {
            reply.refusals.push(why);
            return reply;
        }
    };
    let mut facts = match probe::gather(docker) {
        Ok(facts) => facts,
        Err(why) => {
            reply.refusals.push(format!("the target's bank: {why}"));
            return reply;
        }
    };
    if facts.running
        && !facts.health_url.is_empty()
        && let Some(answer) = place.health(&format!("{}/health", facts.health_url))
    {
        probe::apply_health(&mut facts, &answer);
    }
    let refuse = &mut reply.refusals;
    if !facts.running {
        refuse.push(format!(
            "the target daemon {} is not running; start it (docker compose start in its install directory) so move can check it, then retry",
            facts.daemon
        ));
    }
    if facts.compose_project.is_empty() || facts.compose_project != facts.pg_compose_project {
        refuse.push(format!(
            "the target's {} and {} are not one Docker Compose project; move goes only to a bank the installer set up",
            facts.daemon, facts.pg
        ));
    } else if !facts.compose_dir.is_empty() && place.in_checkout(Path::new(&facts.compose_dir)) {
        refuse.push(format!(
            "the target's bank runs from a source checkout ({}); move goes only to an installed (release) Docker bank, so install one with the installer and retry",
            facts.compose_dir
        ));
    }
    if facts.running {
        if facts.health_url.is_empty() {
            refuse.push(format!(
                "the target daemon {} publishes no health port, so move cannot check it; publish its port 8765 as the installer's compose does (or run it on the host network), then retry",
                facts.daemon
            ));
        } else if facts.status != "ok" || facts.ready == Some(false) {
            refuse.push(format!(
                "the target daemon is not healthy and ready at {}/health (status {:?}, ready {:?}); wait for it or check docker logs {}, then retry",
                facts.health_url,
                facts.status,
                facts.ready,
                facts.daemon
            ));
        } else {
            match facts.schema {
                Some(schema) if schema >= ask.source_schema => {}
                schema => refuse.push(format!(
                    "the target's schema ({}) is older than the source's ({}); update the target with pseudolife-mcp update, then retry",
                    schema.map_or_else(|| "unknown".to_owned(), |s| s.to_string()),
                    ask.source_schema
                )),
            }
            // A fresh bank has no identity until a client's first session
            // writes one: no fingerprint in /health or in meta is a fresh
            // target, as Python's move accepts it. One without the other is
            // a daemon that has not read its bank yet, or another database.
            match (&facts.bank, &facts.meta_fingerprint) {
                (Some(bank), _) if *bank == ask.source_bank => refuse.push(
                    "the target is this same bank (its fingerprint equals the source's); name another host with --to".to_owned(),
                ),
                (None, Some(_)) => refuse.push(format!(
                    "the target daemon has not read its bank's identity yet (its /health shows no fingerprint while {} holds one); wait a minute or check docker logs {}, then retry",
                    facts.pg, facts.daemon
                )),
                (health, meta) if health != meta => refuse.push(format!(
                    "the target daemon's fingerprint does not match the bank in {}; check that {} serves that database, then retry",
                    facts.pg, facts.daemon
                )),
                _ => {}
            }
        }
    }
    // A dump restores into the same or a newer major version (Python's
    // `_pg_major`); a minor release apart does not matter.
    match facts.pg_version {
        Some(version) if version / 10000 >= ask.source_pg_version / 10000 => {}
        version => refuse.push(format!(
            "the target's PostgreSQL ({}) is an older major version than the source's ({}); a dump restores only into the same or a newer major version, so update the target's PostgreSQL, then retry",
            version.map_or_else(|| "unknown".to_owned(), |v| (v / 10000).to_string()),
            ask.source_pg_version / 10000
        )),
    }
    if let Ok(docker) = place.docker() {
        match probe::used_tables(docker, &facts) {
            Ok(used) if used.is_empty() => {}
            Ok(used) => refuse.push(format!(
                "the target bank is not empty ({}): move never merges into or overwrites a used bank; back up and remove that bank, or install a fresh target, and use pseudolife-mcp export / import to merge knowledge instead",
                used.iter()
                    .map(|(t, n)| format!("{t} {n}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            )),
            Err(why) => refuse.push(why),
        }
    }
    match work.as_deref().and_then(|w| place.nearest(w).map(|n| (w.to_path_buf(), n))) {
        None => refuse.push(
            "the target's move work directory cannot be resolved (no home directory, or no existing parent); name one with --target-work-dir <absolute path>".to_owned(),
        ),
        Some((_, (existing, rest, free))) => {
            // The canonical ancestor plus the part still to be created: a
            // symlinked ancestor keeps the requested directory below it.
            let resolved = existing.join(rest);
            reply.work_dir = resolved.to_string_lossy().into_owned();
            reply.free_bytes = free;
            // Docker reports a mount source as it was given; the work
            // directory is canonical, so each root is compared both ways.
            let inside = facts
                .state_sources
                .iter()
                .chain(&facts.pg_sources)
                .flat_map(|root| {
                    let canonical = place.canonical(Path::new(root));
                    [root.clone(), canonical.to_string_lossy().into_owned()]
                })
                .any(|root| under(&existing, &root) || under(&resolved, &root));
            if inside {
                refuse.push(format!(
                    "the move's work directory {} is inside a volume of the target's bank, which the restore replaces; name another with --target-work-dir <absolute path>",
                    reply.work_dir
                ));
            } else if place.in_checkout(&existing) {
                refuse.push(format!(
                    "the move's work directory {} is inside a git checkout; name another with --target-work-dir <absolute path>",
                    reply.work_dir
                ));
            }
            match free {
                Some(free) if free >= ask.need_bytes => {}
                Some(free) => refuse.push(format!(
                    "the target has {} MiB free at {}, and the move's files may take up to {} MiB; free space there or name another --target-work-dir, then retry",
                    free / (1024 * 1024),
                    reply.work_dir,
                    ask.need_bytes.div_ceil(1024 * 1024)
                )),
                None => refuse.push(format!(
                    "cannot read the free space at {}; check that directory, then retry",
                    reply.work_dir
                )),
            }
        }
    }
    reply.facts = Some(facts);
    reply
}

/// The real target host.
struct Here {
    docker: Option<docker::Real>,
    missing: Option<String>,
}

impl Place for Here {
    type D = docker::Real;
    fn docker(&mut self) -> Result<&mut docker::Real, String> {
        match (&mut self.docker, &self.missing) {
            (Some(docker), _) => Ok(docker),
            (None, Some(why)) => Err(why.clone()),
            (None, None) => Err("no docker program".to_owned()),
        }
    }
    fn health(&mut self, url: &str) -> Option<serde_json::Value> {
        super::super::http_json(url, std::time::Duration::from_secs(5))
    }
    fn home(&self) -> Option<PathBuf> {
        std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .filter(|h| !h.is_empty())
            .map(PathBuf::from)
    }
    fn nearest(&self, path: &Path) -> Option<(PathBuf, PathBuf, Option<u64>)> {
        path.ancestors().find_map(|probe| {
            let canonical = std::fs::canonicalize(probe).ok()?;
            let rest = path.strip_prefix(probe).ok()?.to_path_buf();
            let free = super::super::bank::free_space(&canonical.to_string_lossy())
                .map(|bytes| u64::try_from(bytes).unwrap_or(u64::MAX));
            Some((canonical, rest, free))
        })
    }
    fn canonical(&self, path: &Path) -> PathBuf {
        std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
    }
    fn in_checkout(&self, path: &Path) -> bool {
        path.ancestors()
            .any(|a| std::fs::symlink_metadata(a.join(".git")).is_ok())
    }
    fn update_timer(&mut self) -> String {
        let Some(systemctl) = crate::cli::program_lookup::find_program_path("systemctl") else {
            return "unknown".to_owned();
        };
        std::process::Command::new(systemctl)
            .args(["--user", "is-enabled", "pseudolife-update.timer"])
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .output()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned())
            .ok()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "unknown".to_owned())
    }
}

/// `pseudolife-mcp move-target <protocol>`.
pub(in crate::cli) fn run(arguments: &[std::ffi::OsString]) -> u8 {
    let asked = arguments
        .first()
        .and_then(|a| a.to_str())
        .unwrap_or_default()
        .to_owned();
    let print = |reply: &Reply| {
        let line = format!(
            "{FRAME}{}\n",
            serde_json::to_string(reply).unwrap_or_else(|_| "{}".to_owned())
        );
        let mut out = std::io::stdout().lock();
        let _ = out.write_all(line.as_bytes());
        let _ = out.flush();
    };
    if asked != PROTOCOL.to_string() || arguments.len() != 1 {
        print(&Reply {
            protocol: PROTOCOL,
            version: env!("CARGO_PKG_VERSION").to_owned(),
            refusals: vec![format!(
                "the target's pseudolife-mcp speaks move protocol {PROTOCOL}, and the source asked for {asked:?}; update the older client (pseudolife-mcp update) so both match, then retry"
            )],
            ..Reply::default()
        });
        return 3;
    }
    let mut input = Vec::new();
    let _ = std::io::stdin().take(1 << 20).read_to_end(&mut input);
    let Ok(ask) = serde_json::from_slice::<Ask>(&input) else {
        print(&Reply {
            protocol: PROTOCOL,
            version: env!("CARGO_PKG_VERSION").to_owned(),
            refusals: vec!["the source's request could not be read; update both clients so they match, then retry".to_owned()],
            ..Reply::default()
        });
        return 3;
    };
    let mut here = match super::docker_program() {
        Ok(program) => Here {
            docker: Some(docker::Real {
                program,
                interruptible: false,
            }),
            missing: None,
        },
        Err(why) => Here {
            docker: None,
            missing: Some(format!("the target: {why}")),
        },
    };
    print(&answer(&mut here, &ask));
    0
}
