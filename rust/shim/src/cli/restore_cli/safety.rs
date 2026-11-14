//! The safety backup restore takes of the current bank before replacing it:
//! the database through `pg_dump` in its PostgreSQL container and the
//! daemon's `/data` through `tar` in a read-only helper container, each
//! streamed from the container's stdout into a private `.part` file on the
//! safety directory, verified (the dump's completion marker, the archive's
//! full read and path checks), flushed to disk and only then renamed to its
//! final name. Restore names its pair `pseudolife_safety_*`, which backup
//! rotation never matches; `pseudolife-mcp backup` writes the same verified
//! pair under its own names (`Target::prefix`).
use std::{
    fs,
    path::{Path, PathBuf},
};

use super::{
    Verdict,
    bank::{BULK, Bank, Plan, Step, Stop},
    conn,
    docker::{Docker, Input, Output},
    dump, state,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Pair {
    /// `None` when the destination database did not exist.
    pub dump: Option<PathBuf>,
    pub state: PathBuf,
    /// tar's reason for each archive attempt retried before the one kept
    /// (a file in /data changed while it was read); empty when the first
    /// attempt was kept.
    pub archive_retries: Vec<String>,
}

/// How many times the state archive is taken when tar reports a file in
/// /data changed or vanished while it was read, and the pause between
/// attempts. A serving daemon writes /data in bursts (a deep dream writes
/// and prunes graph snapshots in place), so a few seconds apart three
/// attempts outlast one burst; a writer that never pauses still fails the
/// backup, with tar's text.
const ARCHIVE_ATTEMPTS: usize = 3;
const ARCHIVE_PAUSE: std::time::Duration = if cfg!(test) {
    std::time::Duration::from_millis(20)
} else {
    std::time::Duration::from_secs(10)
};

/// tar's own last line of complaint, or `None`.
fn last_line(stderr: &[u8]) -> Option<String> {
    String::from_utf8_lossy(stderr)
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(str::to_owned)
}

/// GNU tar's words for a file that changed or was removed while it read
/// it: the archive is then not a consistent copy, but a later attempt can be.
fn changed_while_read(stderr: &[u8]) -> bool {
    let text = String::from_utf8_lossy(stderr);
    text.contains("changed as we read it") || text.contains("removed before we read it")
}

/// Removes files this run published; the ones that could not be removed,
/// named for the message (they stand under their final names).
pub(super) fn unpublish(paths: &[&Path]) -> Vec<String> {
    paths
        .iter()
        .filter_map(|path| match fs::remove_file(path) {
            Ok(()) => None,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => Some(format!("{} ({error})", path.display())),
        })
        .collect()
}

/// The words that say a published file could not be removed again; the
/// caller then never says nothing was written.
pub(super) const LEFT_BEHIND: &str = "could not be removed and still stands under its final name";

/// `message`, telling what still stands when a removal failed: this run
/// reports no backup, so the user deletes those files.
pub(super) fn with_leftovers(message: String, left: &[String]) -> String {
    if left.is_empty() {
        return message;
    }
    let each: Vec<String> = left
        .iter()
        .map(|file| format!("{file} {LEFT_BEHIND}"))
        .collect();
    let them = if left.len() == 1 { "it" } else { "them" };
    format!(
        "{message}; {}. This run reports no backup, so delete {them}",
        each.join(", and ")
    )
}

const DUMP: &str = r#"unset PGHOSTADDR PGSERVICE PGSERVICEFILE; exec pg_dump --no-owner --no-acl -h "$1" -p "$2" -U "$3" -d "$4" -Z9"#;
/// `/data` without a staging directory a killed restore left behind (the
/// exact name `execute::STAGE_GLOB` matches).
const ARCHIVE: &str = "tar czf - --anchored '--exclude=./.pseudolife-restore-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]' -C /data .";

fn part(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(".part");
    PathBuf::from(name)
}

/// Flush a written file's data and metadata to disk.
fn flush(path: &Path) -> std::io::Result<()> {
    // FlushFileBuffers on Windows needs a writable handle.
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)?
        .sync_all()
}

/// Make a rename in `directory` durable on Unix (fsync of the directory).
/// On Windows there is no directory handle to flush: the file's data is
/// flushed before its rename, and the new name relies on NTFS's metadata
/// journal, so a power loss can lose the name but not tear the file.
pub(super) fn sync_dir(directory: &Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        fs::File::open(directory)?.sync_all()?;
    }
    let _ = directory;
    Ok(())
}

/// The safety directory, private to this user when it is created here.
fn create_dir(directory: &Path) -> std::io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(directory)
}

/// Back up the bank as it is now. `stamp` is unique to this run.
/// What one backup copies and where: the bank's database (when
/// `include_database`), the daemon's `/data`, into `directory`, as
/// `<prefix>_memory-<stamp>.sql.gz` and `<prefix>_state-<stamp>.tgz`.
#[derive(Debug, Clone)]
pub(super) struct Target {
    pub pg: String,
    pub port: u16,
    pub user: String,
    pub db: String,
    pub image: String,
    pub directory: String,
    pub include_database: bool,
    pub prefix: &'static str,
}

/// The names `pseudolife-mcp backup` writes (and rotates).
pub(super) const BACKUP_PREFIX: &str = "pseudolife";
/// The names restore's safety backup writes: never matched by rotation.
pub(super) const SAFETY_PREFIX: &str = "pseudolife_safety";

impl Target {
    /// Restore's safety backup of the bank `plan` names.
    pub fn safety(plan: &Plan) -> Self {
        Self {
            pg: plan.pg.clone(),
            port: plan.port,
            user: plan.user.clone(),
            db: plan.db.clone(),
            image: plan.image.clone(),
            directory: plan.safety_dir.clone(),
            include_database: plan.exists,
            prefix: SAFETY_PREFIX,
        }
    }
}

/// The words a message uses for this backup: restore's safety backup, or
/// `pseudolife-mcp backup`'s own.
fn words(plan: &Target) -> (&'static str, &'static str) {
    if plan.prefix == SAFETY_PREFIX {
        ("safety ", "--safety-dir")
    } else {
        ("", "--out")
    }
}

pub(super) fn backup<D: Docker>(bank: &mut Bank<'_, D>, plan: &Target, stamp: &str) -> Step<Pair> {
    let directory = Path::new(&plan.directory);
    let (kind, flag) = words(plan);
    create_dir(directory).map_err(|error| {
        Stop(
            format!(
                "cannot create the {kind}backup directory {} ({error}); make it writable or choose {flag} <directory>, then retry",
                directory.display()
            ),
            1,
        )
    })?;
    let prefix = plan.prefix;
    let dump_path = directory.join(format!("{prefix}_memory-{stamp}.sql.gz"));
    let state_path = directory.join(format!("{prefix}_state-{stamp}.tgz"));
    let (dump_part, state_part) = (part(&dump_path), part(&state_path));
    let result = (|| {
        if plan.include_database {
            let port = plan.port.to_string();
            let target = conn::database_arg(&plan.db);
            let argv: Vec<String> = [
                "exec",
                &plan.pg,
                "sh",
                "-c",
                DUMP,
                "_",
                "/var/run/postgresql",
                &port,
                &plan.user,
                &target,
            ]
            .iter()
            .map(|s| (*s).to_owned())
            .collect();
            let ran = bank
                .docker
                .run_io(&argv, BULK, Input::Null, Output::File(&dump_part));
            if ran.failed && !ran.interrupted {
                let fault = ran.fault.clone().unwrap_or_else(|| "it failed".to_owned());
                return Err(Stop(
                    format!(
                        "the {kind}dump of the current database could not be taken: {fault}; fix that, then retry"
                    ),
                    1,
                ));
            }
            if !ran.ok() {
                let cause = bank.psql_failure(&ran, plan.port);
                if cause.1 == 130 {
                    return Err(cause);
                }
                return Err(Stop(
                    format!(
                        "the {kind}dump of the current database failed inside {pg} ({detail}); check docker logs {pg} and the free space on {dir}, then retry",
                        pg = plan.pg,
                        detail = cause.0,
                        dir = directory.display()
                    ),
                    1,
                ));
            }
            if dump::complete(&dump_part) != Verdict::Pass {
                return Err(Stop(
                    format!(
                        "the {kind}dump of the current database is incomplete; check docker logs {} and the free space on {}, then retry",
                        plan.pg,
                        directory.display()
                    ),
                    1,
                ));
            }
        }
        let mut retries = Vec::new();
        loop {
            let _ = fs::remove_file(&state_part);
            let helper = bank.helper(&plan.image, ARCHIVE, false)?;
            let argv: Vec<String> = vec!["start".into(), "-a".into(), helper.clone()];
            let ran = bank
                .docker
                .run_io(&argv, BULK, Input::Null, Output::File(&state_part));
            bank.quiet(&["rm", "-f", &helper]);
            if ran.interrupted {
                return Err(Stop("interrupted".into(), 130));
            }
            if ran.ok() {
                break;
            }
            if ran.failed {
                let fault = ran.fault.clone().unwrap_or_else(|| "it failed".to_owned());
                return Err(Stop(
                    format!(
                        "the {kind}archive of the state volume could not be taken: {fault}; fix that, then retry"
                    ),
                    1,
                ));
            }
            let said = last_line(&ran.stderr).map_or_else(
                || "it printed nothing".to_owned(),
                |line| format!("it said: {line}"),
            );
            // GNU tar exits 1 when a file changed or vanished while it read
            // it (a serving daemon writing /data); that archive is not a
            // consistent copy and is not kept, but a later attempt can be.
            if changed_while_read(&ran.stderr) && retries.len() + 1 < ARCHIVE_ATTEMPTS {
                retries.push(said);
                let end = std::time::Instant::now() + ARCHIVE_PAUSE;
                while std::time::Instant::now() < end {
                    if bank.docker.interrupted() {
                        return Err(Stop("interrupted".into(), 130));
                    }
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                continue;
            }
            let attempts = if retries.is_empty() {
                String::new()
            } else {
                format!(" on attempt {} of {ARCHIVE_ATTEMPTS}", retries.len() + 1)
            };
            return Err(Stop(
                format!(
                    "the {kind}archive of the state volume failed{attempts}: tar or Docker exited {} and {said}; check the daemon's /data volume and the free space on {}, then retry",
                    ran.code,
                    directory.display()
                ),
                1,
            ));
        }
        match state::validate(&state_part) {
            Ok(_) => {}
            Err(state::Refusal::Unsafe) => {
                return Err(Stop(
                    format!(
                        "the {kind}archive of the state volume is unreadable or holds a path that could escape /data; check the daemon's /data volume and the free space on {}, then retry",
                        directory.display()
                    ),
                    1,
                ));
            }
            Err(state::Refusal::Unsupported(what)) => {
                return Err(Stop(
                    format!(
                        "the {kind}archive of the state volume contains {what}, which restore cannot check; remove what in the daemon's /data volume produces it, then retry"
                    ),
                    1,
                ));
            }
        }
        let unflushed = |error: std::io::Error| {
            Stop(
                format!(
                    "the {kind}backup could not be written to disk in {} ({error}); repair that filesystem or choose another {flag}, then retry",
                    directory.display()
                ),
                1,
            )
        };
        // A Ctrl-C during the read-back only raised the flag: nothing is
        // published once it is up.
        if bank.docker.interrupted() {
            return Err(Stop("interrupted".into(), 130));
        }
        if plan.include_database {
            flush(&dump_part).map_err(unflushed)?;
        }
        flush(&state_part).map_err(unflushed)?;
        if plan.include_database {
            fs::rename(&dump_part, &dump_path).map_err(unflushed)?;
        }
        // The pair is published whole or not at all: a dump renamed before
        // the archive's rename or the directory sync failed is removed, and
        // the archive only when this run renamed it into place.
        let renamed = fs::rename(&state_part, &state_path);
        let state_renamed = renamed.is_ok();
        let published = renamed.and_then(|()| sync_dir(directory));
        if let Err(error) = published {
            let mut mine: Vec<&Path> = Vec::new();
            if plan.include_database {
                mine.push(&dump_path);
            }
            if state_renamed {
                mine.push(&state_path);
            }
            let left = unpublish(&mine);
            let Stop(message, code) = unflushed(error);
            return Err(Stop(with_leftovers(message, &left), code));
        }
        Ok(Pair {
            dump: plan.include_database.then(|| dump_path.clone()),
            state: state_path.clone(),
            archive_retries: retries,
        })
    })();
    // Partial files never outlive the run (a renamed file is complete).
    let _ = fs::remove_file(&dump_part);
    let _ = fs::remove_file(&state_part);
    result
}
