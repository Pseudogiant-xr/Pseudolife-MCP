//! The safety backup restore takes of the current bank before replacing it:
//! the database through `pg_dump` in its PostgreSQL container and the
//! daemon's `/data` through `tar` in a read-only helper container, each
//! streamed from the container's stdout into a private `.part` file on the
//! safety directory, verified (the dump's completion marker, the archive's
//! full read and path checks), flushed to disk and only then renamed to its
//! final name. The files are named like `pseudolife-mcp backup`'s Docker
//! artifacts and are never rotated or pruned.
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
}

const DUMP: &str = r#"unset PGHOSTADDR PGSERVICE PGSERVICEFILE; exec pg_dump --no-owner --no-acl -h "$1" -p "$2" -U "$3" -d "$4" -Z9"#;
const ARCHIVE: &str = "tar czf - -C /data .";

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

/// Make a rename in `directory` durable (a no-op where the platform
/// flushes directory entries with the file).
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
pub(super) fn backup<D: Docker>(bank: &mut Bank<'_, D>, plan: &Plan, stamp: &str) -> Step<Pair> {
    let directory = Path::new(&plan.safety_dir);
    create_dir(directory).map_err(|error| {
        Stop(
            format!(
                "cannot create the safety backup directory {} ({error}); make it writable or choose --safety-dir <directory>, then retry",
                directory.display()
            ),
            1,
        )
    })?;
    let dump_path = directory.join(format!("pseudolife_memory-{stamp}.sql.gz"));
    let state_path = directory.join(format!("pseudolife_state-{stamp}.tgz"));
    let (dump_part, state_part) = (part(&dump_path), part(&state_path));
    let result = (|| {
        if plan.exists {
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
            if !ran.ok() {
                let cause = bank.psql_failure(&ran, plan.port);
                if cause.1 == 130 {
                    return Err(cause);
                }
                return Err(Stop(
                    format!(
                        "the safety dump of the current database failed inside {pg} ({detail}); check docker logs {pg} and the free space on {dir}, then retry",
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
                        "the safety dump of the current database is incomplete; check docker logs {} and the free space on {}, then retry",
                        plan.pg,
                        directory.display()
                    ),
                    1,
                ));
            }
        }
        let helper = bank.helper(&plan.image, ARCHIVE, false)?;
        let argv: Vec<String> = vec!["start".into(), "-a".into(), helper.clone()];
        let ran = bank
            .docker
            .run_io(&argv, BULK, Input::Null, Output::File(&state_part));
        bank.quiet(&["rm", "-f", &helper]);
        if ran.interrupted {
            return Err(Stop("interrupted".into(), 130));
        }
        if !ran.ok() {
            return Err(Stop(
                format!(
                    "the safety archive of the state volume failed (tar exited {}); check the daemon's /data volume and the free space on {}, then retry",
                    ran.code,
                    directory.display()
                ),
                1,
            ));
        }
        match state::validate(&state_part) {
            Ok(()) => {}
            Err(state::Refusal::Unsafe) => {
                return Err(Stop(
                    format!(
                        "the safety archive of the state volume is unreadable or holds a path that could escape /data; check the daemon's /data volume and the free space on {}, then retry",
                        directory.display()
                    ),
                    1,
                ));
            }
            Err(state::Refusal::Unsupported(what)) => {
                return Err(Stop(
                    format!(
                        "the safety archive of the state volume contains {what}, which restore cannot check; remove what in the daemon's /data volume produces it, then retry"
                    ),
                    1,
                ));
            }
        }
        let unflushed = |error: std::io::Error| {
            Stop(
                format!(
                    "the safety backup could not be written to disk in {} ({error}); repair that filesystem or choose another --safety-dir, then retry",
                    directory.display()
                ),
                1,
            )
        };
        if plan.exists {
            flush(&dump_part).map_err(unflushed)?;
        }
        flush(&state_part).map_err(unflushed)?;
        if plan.exists {
            fs::rename(&dump_part, &dump_path).map_err(unflushed)?;
        }
        fs::rename(&state_part, &state_path).map_err(unflushed)?;
        sync_dir(directory).map_err(unflushed)?;
        Ok(Pair {
            dump: plan.exists.then(|| dump_path.clone()),
            state: state_path.clone(),
        })
    })();
    // Partial files never outlive the run (a renamed file is complete).
    let _ = fs::remove_file(&dump_part);
    let _ = fs::remove_file(&state_part);
    result
}
