//! `pseudolife-mcp backup` of a Docker bank: the same verified backup
//! restore takes before it replaces a bank (`safety::backup`), written under
//! the backup names and rotated. The daemon keeps serving: nothing is
//! stopped. The pair a run reports exists, is complete and was read back
//! (the dump's completion marker, the archive's full read and path checks)
//! before its rename; a failed or interrupted run leaves no file under a
//! backup name.
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

use super::{
    bank::{self, Bank, Selectors, Stop},
    docker::{self, Docker},
    execute::Host,
    interrupt, pathlib,
    safety::{self, BACKUP_PREFIX, Target},
};

/// What `backup` was asked for on a Docker bank.
#[derive(Debug, Clone, Default)]
pub(in crate::cli) struct Request {
    pub container: Option<String>,
    pub daemon_container: Option<String>,
    pub db: Option<String>,
    pub user: Option<String>,
    pub db_port: Option<i128>,
    /// `--out`; else `<data dir>/backups`.
    pub out: Option<PathBuf>,
    /// `--keep-days`: rotate this tool's files older than this.
    pub keep_days: f64,
}

impl Request {
    fn selectors(&self) -> Selectors {
        Selectors {
            container: self.container.clone(),
            daemon_container: self.daemon_container.clone(),
            db: self.db.clone(),
            user: self.user.clone(),
            db_port: self.db_port,
        }
    }
}

const NOTHING: &str = "No backup was written";

/// Output for this command: `backup failed: ...` on stderr.
fn failed(host: &mut impl Host, message: &str) {
    host.error(&format!("backup failed: {message}"));
}

/// A refusal text from the shared checks, with this command's ending; a
/// message naming a file left under its final name keeps its own ending.
fn ours(message: &str) -> String {
    let message = message
        .replace("Nothing was restored", NOTHING)
        .replace("Nothing was changed", NOTHING);
    if message.ends_with(NOTHING) || message.contains(safety::LEFT_BEHIND) {
        message
    } else {
        format!("{message}. {NOTHING}")
    }
}

/// Whether this host runs a Docker bank, as `configured` found it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(in crate::cli) enum Configured {
    /// The daemon or PostgreSQL container exists.
    Yes,
    /// No `docker` program, or Docker answered and neither container exists.
    No,
    /// `docker` resolves but could not be asked (an engine that is stopped
    /// or starting, a listing over 10 seconds): the reason.
    Unknown(String),
}

/// Whether a Docker bank is configured on this host: `docker` resolves and
/// `docker ps -a` lists the daemon or PostgreSQL container. The listing's
/// exit is the signal: 0 with neither name is no Docker bank, and a listing
/// that fails or never finishes leaves it unknown.
pub(in crate::cli) fn configured() -> Configured {
    let Ok(program) = docker::program() else {
        return Configured::No;
    };
    let mut runner = docker::Real {
        program,
        interruptible: false,
    };
    let args: Vec<String> = ["ps", "-a", "--format", "{{.Names}}"]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
    let ran = runner.run(&args, Duration::from_secs(10));
    listed(&ran)
}

/// `configured`'s reading of `docker ps -a --format {{.Names}}`.
fn listed(ran: &docker::Ran) -> Configured {
    if ran.failed {
        return Configured::Unknown(format!(
            "docker ps -a failed: {}",
            ran.fault.as_deref().unwrap_or("it did not run")
        ));
    }
    if ran.code != 0 {
        let said = String::from_utf8_lossy(&ran.stderr)
            .lines()
            .rev()
            .map(str::trim)
            .find(|line| !line.is_empty())
            .map_or_else(String::new, |line| format!(": {line}"));
        return Configured::Unknown(format!("docker ps -a exited {}{said}", ran.code));
    }
    let names = String::from_utf8_lossy(&ran.stdout).into_owned();
    if names
        .lines()
        .map(str::trim)
        .any(|name| name == bank::DAEMON_CONTAINER || name == bank::PG_CONTAINER)
    {
        Configured::Yes
    } else {
        Configured::No
    }
}

/// Run the backup under the update lock; the exit code.
pub(in crate::cli) fn run(request: &Request) -> u8 {
    let mut host = super::RealHost;
    let Some(directory) = super::data_dir() else {
        failed(
            &mut host,
            "cannot find your home directory for the update lock and the default backup directory; set HOME (USERPROFILE on Windows) or PSEUDOLIFE_MCP_DATA_DIR, then retry. No backup was written",
        );
        return 2;
    };
    let result = super::with_update_lock(&directory, || match docker::program() {
        Err(message) => {
            failed(&mut host, &ours(message));
            2
        }
        Ok(program) => {
            let _guard = interrupt::Guard::install();
            let mut runner = docker::Real {
                program,
                interruptible: true,
            };
            let code = backup(&mut runner, &mut host, request, &directory);
            if code != 0 && interrupt::requested() && code != 130 {
                130
            } else {
                code
            }
        }
    });
    match result {
        Ok(code) => code,
        Err(super::LockRefusal::Busy(pid)) => {
            failed(
                &mut super::RealHost,
                &format!(
                    "another pseudolife-mcp update, restore or backup is running (pid {pid}); nothing was changed. Run this again when it has finished"
                ),
            );
            2
        }
        Err(super::LockRefusal::Unavailable(error)) => {
            failed(
                &mut super::RealHost,
                &format!(
                    "cannot take the update lock in {} ({error}); make that directory writable, then retry. No backup was written",
                    directory.display()
                ),
            );
            2
        }
    }
}

/// The backup itself, once the lock is held: select, check, write, rotate.
pub(super) fn backup<D: Docker, H: Host>(
    docker: &mut D,
    host: &mut H,
    request: &Request,
    data_dir: &Path,
) -> u8 {
    let refusal = |host: &mut H, Stop(message, code): Stop| {
        if (code == 130 || interrupt::requested()) && !message.contains(safety::LEFT_BEHIND) {
            failed(
                host,
                "the backup was interrupted. No backup was written (a partial file it could not remove is removed by a later backup)",
            );
        } else {
            failed(host, &ours(&message));
        }
        code
    };
    let selectors = request.selectors();
    let (pg, daemon) = bank::containers(&selectors);
    let mut bank = Bank { docker, pg, daemon };
    let chosen = match bank::select(&mut bank, &selectors) {
        Ok(chosen) => chosen,
        Err(stop) => return refusal(host, stop),
    };
    match bank::database_exists(&mut bank, &chosen) {
        Ok(true) => {}
        Ok(false) => {
            failed(
                host,
                &format!(
                    "the database {} does not exist in {}; name the bank's database with --db <database>, then retry. No backup was written",
                    chosen.db, chosen.pg
                ),
            );
            return 2;
        }
        Err(stop) => return refusal(host, stop),
    }
    let out = match &request.out {
        Some(out) => out.clone(),
        None => data_dir.join("backups"),
    };
    let out = std::path::absolute(&out).unwrap_or(out);
    let shown = out.to_string_lossy().into_owned();
    let Some(resolved) = pathlib::resolve(&out) else {
        failed(
            host,
            &format!(
                "cannot resolve the backup directory {shown}; make it and its parents readable (no symlink loop), or choose another --out, then retry. No backup was written"
            ),
        );
        return 2;
    };
    if let Ok(meta) = fs::metadata(&resolved)
        && !meta.is_dir()
    {
        failed(
            host,
            &format!(
                "--out names {resolved}, which is not a directory; name a directory with --out <directory>, then retry. No backup was written"
            ),
        );
        return 2;
    }
    match bank::inside_state_mount(&chosen, &resolved) {
        Ok(false) => {}
        Ok(true) => {
            failed(
                host,
                &format!(
                    "the backup directory {resolved} is inside the daemon's /data volume, which the backup archives; choose --out <directory outside it>, then retry. No backup was written"
                ),
            );
            return 2;
        }
        Err(stop) => return refusal(host, stop),
    }
    for (path, outcome) in sweep_parts(Path::new(&resolved), SystemTime::now()) {
        match outcome {
            Ok(()) => host.info(&format!(
                "removed partial file an interrupted backup left: {}",
                path.display()
            )),
            Err(error) => host.error(&format!(
                "warning: could not remove {}, a partial file an interrupted backup left ({error}); delete it yourself",
                path.display()
            )),
        }
    }
    let run_tag = host.run_tag();
    let checks = host
        .host_space(&resolved)
        .and_then(|()| bank.storage_space(&chosen.image, &run_tag));
    if let Err(stop) = checks {
        return refusal(host, stop);
    }
    let stamp = host.stamp();
    let target = Target {
        pg: chosen.pg.clone(),
        port: chosen.port,
        user: chosen.user.clone(),
        db: chosen.db.clone(),
        image: chosen.image.clone(),
        directory: resolved.clone(),
        include_database: true,
        prefix: BACKUP_PREFIX,
    };
    let pair = match safety::backup(&mut bank, &target, &stamp) {
        Ok(pair) => pair,
        Err(stop) => {
            if bank.docker.interrupted() {
                return refusal(host, Stop("interrupted".into(), 130));
            }
            return refusal(host, stop);
        }
    };
    let dump = pair.dump.clone().expect("the database is included");
    let published = [dump.as_path(), pair.state.as_path()];
    // A Ctrl-C after the pair was published still cancels the backup: its
    // files go and nothing is rotated.
    if bank.docker.interrupted() {
        let left = safety::unpublish(&published);
        if left.is_empty() {
            return refusal(host, Stop("interrupted".into(), 130));
        }
        failed(
            host,
            &safety::with_leftovers(
                "the backup was interrupted after its pair was written".into(),
                &left,
            ),
        );
        return 130;
    }
    // The files reported are the files on disk.
    for path in published {
        if !fs::symlink_metadata(path).is_ok_and(|meta| meta.is_file() && meta.len() > 0) {
            let left = safety::unpublish(&published);
            let message = format!(
                "{} is missing after it was written; check the backup directory's filesystem, then retry",
                path.display()
            );
            if left.is_empty() {
                failed(host, &format!("{message}. {NOTHING}"));
            } else {
                failed(host, &safety::with_leftovers(message, &left));
            }
            return 1;
        }
    }
    host.info(&format!("bank dump:     {}", dump.display()));
    host.info(&format!("state archive: {}", pair.state.display()));
    if !pair.archive_retries.is_empty() {
        host.info(&format!(
            "state archive taken on attempt {}: tar reported a file in /data changing while it read the earlier attempts ({})",
            pair.archive_retries.len() + 1,
            pair.archive_retries.join("; ")
        ));
    }
    let keep: HashSet<PathBuf> = [dump, pair.state.clone()].into_iter().collect();
    let protected = match journal_files(data_dir) {
        Ok(protected) => protected,
        Err(reason) => {
            host.error(&format!(
                "warning: rotation was skipped: {reason}, so the files a restore journal names are unknown and nothing was rotated (the new backup is complete)"
            ));
            return 0;
        }
    };
    let rotation = rotate(
        Path::new(&resolved),
        request.keep_days,
        &keep,
        &protected,
        SystemTime::now(),
    );
    for path in &rotation.removed {
        host.info(&format!("rotated out:   {}", path.display()));
    }
    for (path, error, partner_removed) in &rotation.failed {
        if *partner_removed {
            host.error(&format!(
                "warning: could not rotate out {} ({error}) after the other half of its pair was removed; it is left on its own, so delete it yourself (the new backup is complete)",
                path.display()
            ));
        } else {
            host.error(&format!(
                "warning: could not rotate out {} ({error}); its pair was kept whole (the new backup is complete)",
                path.display()
            ));
        }
    }
    for path in &rotation.unpaired {
        host.info(&format!(
            "kept unpaired: {} (the other half of its pair is missing; rotation removes only whole pairs, so delete it yourself if you do not need it)",
            path.display()
        ));
    }
    0
}

/// A killed backup's partial files stop changing; one changed this recently
/// may still be written by a backup that takes no update lock (a 0.23.x
/// client), so it is left alone.
const PART_IDLE: Duration = Duration::from_secs(600);

/// Remove the partial files killed backups left in `directory`: this
/// command's exact names plus `.part`, regular files only, unchanged for
/// [`PART_IDLE`]. Runs under the update lock, so no native backup owns one.
fn sweep_parts(directory: &Path, now: SystemTime) -> Vec<(PathBuf, std::io::Result<()>)> {
    let Ok(entries) = fs::read_dir(directory) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name();
            let name = name.to_str()?.strip_suffix(".part")?;
            rotation_name(name)?;
            let path = entry.path();
            let meta = fs::symlink_metadata(&path).ok()?;
            if !meta.is_file() {
                return None;
            }
            let idle = now.duration_since(meta.modified().ok()?).ok()?;
            (idle >= PART_IDLE).then(|| {
                let outcome = fs::remove_file(&path);
                (path, outcome)
            })
        })
        .collect()
}

/// The stamp of a restore's recovery note, `pseudolife_restore-<stamp>.txt`:
/// a native restore before the safety prefix wrote its safety pair under
/// the backup names with this stamp, so that pair is never rotated.
fn note_stamp(name: &str) -> Option<&str> {
    let stamp = name
        .strip_prefix("pseudolife_restore-")?
        .strip_suffix(".txt")?;
    stamp_shape(stamp).then_some(stamp)
}

/// `YYYYmmdd-HHMMSS-<8 lowercase hex>`.
fn stamp_shape(stamp: &str) -> bool {
    let bytes = stamp.as_bytes();
    let digits = |range: std::ops::Range<usize>| bytes[range].iter().all(u8::is_ascii_digit);
    let hex = |range: std::ops::Range<usize>| {
        bytes[range]
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
    };
    bytes.len() == 24
        && digits(0..8)
        && bytes[8] == b'-'
        && digits(9..15)
        && bytes[15] == b'-'
        && hex(16..24)
}

/// Which half of a pair one of this command's names is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Half {
    Dump = 0,
    State = 1,
}

/// `pseudolife_memory-YYYYmmdd-HHMMSS-<8 hex>.sql.gz` or
/// `pseudolife_state-...tgz`: this command's own names, nothing else; the
/// half and the stamp.
fn rotation_name(name: &str) -> Option<(Half, &str)> {
    let (half, stamp) = if let Some(stamp) = name
        .strip_prefix("pseudolife_memory-")
        .and_then(|rest| rest.strip_suffix(".sql.gz"))
    {
        (Half::Dump, stamp)
    } else {
        let stamp = name
            .strip_prefix("pseudolife_state-")
            .and_then(|rest| rest.strip_suffix(".tgz"))?;
        (Half::State, stamp)
    };
    stamp_shape(stamp).then_some((half, stamp))
}

/// The files every restore journal in `data_dir` names: never rotated. An
/// error when the directory cannot be listed or a journal cannot be read:
/// its files are then unknown. A journal from before `state_archive` was
/// recorded protects the state archive named like its backup file too.
pub(super) fn journal_files(data_dir: &Path) -> Result<HashSet<PathBuf>, String> {
    let unlisted = |error: std::io::Error| {
        format!(
            "the data directory {} cannot be listed ({error})",
            data_dir.display()
        )
    };
    let mut files = HashSet::new();
    let entries = match fs::read_dir(data_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(files),
        Err(error) => return Err(unlisted(error)),
    };
    for entry in entries {
        let entry = entry.map_err(unlisted)?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with("restore-in-progress") || !name.ends_with(".json") {
            continue;
        }
        let path = entry.path();
        let bytes = fs::read(&path).map_err(|error| {
            format!(
                "the restore journal {} cannot be read ({error})",
                path.display()
            )
        })?;
        let value = serde_json::from_slice::<serde_json::Value>(&bytes).map_err(|error| {
            format!(
                "the restore journal {} is not valid JSON ({error})",
                path.display()
            )
        })?;
        for key in [
            "safety_dump",
            "safety_state",
            "backup_file",
            "state_archive",
        ] {
            if let Some(path) = value[key].as_str() {
                files.insert(PathBuf::from(path));
            }
        }
        // The state archive a restore would finish with, named like its
        // backup file (a journal without `state_archive`).
        if let Some(backup) = value["backup_file"].as_str() {
            let backup = Path::new(backup);
            if let Some((_, stamp)) = backup
                .file_name()
                .and_then(|name| name.to_str())
                .and_then(rotation_name)
            {
                files.insert(backup.with_file_name(format!("pseudolife_state-{stamp}.tgz")));
            }
        }
    }
    Ok(files)
}

fn same_file(a: &Path, b: &Path) -> bool {
    a == b
        || match (fs::canonicalize(a), fs::canonicalize(b)) {
            (Ok(a), Ok(b)) => a == b,
            _ => false,
        }
}

/// What one rotation did.
#[derive(Debug, Default)]
pub(super) struct Rotation {
    /// Removed files, each pair's archive before its dump.
    pub removed: Vec<PathBuf>,
    /// A file whose removal failed, why, and whether the other half of its
    /// pair was already removed.
    pub failed: Vec<(PathBuf, std::io::Error, bool)>,
    /// Halves whose partner is missing: never removed, listed instead.
    pub unpaired: Vec<PathBuf>,
}

/// Rotate by pair: this command's files are grouped by stamp, and a pair
/// (a dump and an archive of one stamp) is kept or removed whole. The newest
/// three complete pairs, this run's files and every file a restore journal
/// names are kept; an older pair goes once both halves are older than
/// `keep_days`. A half without its partner (a state-only pair of a 0.23.x
/// restore, a hand-deleted half, a run killed between its renames) never
/// counts as a pair and is never removed, only listed. Runs only after this
/// run's pair was written. A NaN or `+inf` `keep_days` removes nothing; a
/// negative one (`-inf` too) keeps only the newest three pairs.
pub(super) fn rotate(
    directory: &Path,
    keep_days: f64,
    keep: &HashSet<PathBuf>,
    protected: &HashSet<PathBuf>,
    now: SystemTime,
) -> Rotation {
    let mut rotation = Rotation::default();
    let Ok(entries) = fs::read_dir(directory) else {
        return rotation;
    };
    let entries: Vec<_> = entries.flatten().collect();
    let noted: HashSet<String> = entries
        .iter()
        .filter_map(|entry| {
            entry
                .file_name()
                .to_str()
                .and_then(note_stamp)
                .map(str::to_owned)
        })
        .collect();
    let mut pairs: BTreeMap<String, [Option<PathBuf>; 2]> = BTreeMap::new();
    for entry in entries {
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        let Some((half, stamp)) = rotation_name(&name) else {
            continue;
        };
        // A restore's safety pair under the backup names, kept by its note.
        if noted.contains(stamp) {
            continue;
        }
        let path = entry.path();
        // Regular files only: never a link or a directory.
        if !fs::symlink_metadata(&path).is_ok_and(|meta| meta.is_file()) {
            continue;
        }
        pairs.entry(stamp.to_owned()).or_default()[half as usize] = Some(path);
    }
    let seconds = |time: SystemTime| {
        time.duration_since(SystemTime::UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    };
    let cutoff = seconds(now) - keep_days * 86400.0;
    // NaN compares false: nothing is old enough.
    let old = |path: &Path| {
        fs::metadata(path)
            .and_then(|meta| meta.modified())
            .is_ok_and(|modified| seconds(modified) < cutoff)
    };
    let held = |path: &Path| {
        keep.iter().any(|k| same_file(k, path)) || protected.iter().any(|p| same_file(p, path))
    };
    let mut complete = 0;
    // Newest stamp first.
    for (_, [dump, state]) in pairs.into_iter().rev() {
        let (Some(dump), Some(state)) = (dump.clone(), state.clone()) else {
            rotation
                .unpaired
                .extend(dump.or(state).filter(|path| !held(path)));
            continue;
        };
        complete += 1;
        if complete <= 3 || held(&dump) || held(&state) || !(old(&dump) && old(&state)) {
            continue;
        }
        // The archive goes first; when it cannot, the pair stays whole.
        match fs::remove_file(&state) {
            Err(error) => rotation.failed.push((state, error, false)),
            Ok(()) => {
                rotation.removed.push(state);
                match fs::remove_file(&dump) {
                    Ok(()) => rotation.removed.push(dump),
                    Err(error) => rotation.failed.push((dump, error, true)),
                }
            }
        }
    }
    rotation
}

#[cfg(test)]
mod tests {
    use super::super::execute_tests::{Dir, Fake, FakeHost, STAMP, label, ready_host};
    use super::*;
    use serde_json::json;

    fn request(out: &Path) -> Request {
        Request {
            container: Some("p".into()),
            daemon_container: Some("d".into()),
            db: Some("db".into()),
            user: Some("u".into()),
            db_port: Some(5432),
            out: Some(out.to_path_buf()),
            keep_days: 7.0,
        }
    }

    fn fake(mount_source: &str) -> Fake {
        let daemon = json!([{
            "Image": "img",
            "State": {"Running": true},
            "Mounts": [{"Destination": "/data", "Source": mount_source}],
        }])
        .to_string();
        let pg = json!([{"State": {"Running": true}}]).to_string();
        Fake {
            docs: [("d".to_owned(), daemon), ("p".to_owned(), pg)]
                .into_iter()
                .collect(),
            ..Fake::default()
        }
    }

    /// The backup directory as the product resolves it before it prints a
    /// path (a Windows short name such as the hosted runner's `RUNNER~1`
    /// reads as its long name there).
    fn shown(out: &Path) -> PathBuf {
        PathBuf::from(pathlib::resolve(out).expect("the backup directory resolves"))
    }

    fn names(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(dir)
            .map(|entries| {
                entries
                    .flatten()
                    .map(|e| e.file_name().to_string_lossy().into_owned())
                    .collect()
            })
            .unwrap_or_default();
        names.sort();
        names
    }

    #[test]
    fn a_backup_writes_a_verified_pair_and_never_stops_the_daemon() {
        let dir = Dir::new();
        let out = dir.0.join("backups");
        let mut docker = fake("");
        let mut host = ready_host();
        let code = backup(&mut docker, &mut host, &request(&out), &dir.0);
        assert_eq!(code, 0, "{:?}", host.errors);
        let dump = shown(&out).join(format!("pseudolife_memory-{STAMP}.sql.gz"));
        let state = shown(&out).join(format!("pseudolife_state-{STAMP}.tgz"));
        assert_eq!(
            names(&out),
            [
                format!("pseudolife_memory-{STAMP}.sql.gz"),
                format!("pseudolife_state-{STAMP}.tgz")
            ]
        );
        assert_eq!(
            host.info[..2],
            [
                format!("bank dump:     {}", dump.display()),
                format!("state archive: {}", state.display()),
            ]
        );
        let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
        assert!(
            !labels
                .iter()
                .any(|l| l.starts_with("stop") || l == "start d"),
            "{labels:?}"
        );
        // Only a read-only helper over the daemon's volumes, never logged.
        let create = docker
            .calls
            .iter()
            .find(|c| c[0] == "create" && c.iter().any(|a| a == "--volumes-from"))
            .unwrap();
        assert!(create.iter().any(|a| a == "d:ro"), "{create:?}");
        assert!(
            create
                .windows(2)
                .any(|w| w[0] == "--log-driver" && w[1] == "none"),
            "{create:?}"
        );
    }

    #[test]
    fn a_failed_or_interrupted_backup_leaves_no_file_under_a_backup_name() {
        for (step, interrupt, code) in [
            ("safety-dump", false, 1),
            ("start-a helper1", false, 1),
            ("safety-dump", true, 130),
        ] {
            let dir = Dir::new();
            let out = dir.0.join("backups");
            let mut docker = fake("");
            if interrupt {
                // inspect, inspect, the exists probe, Docker storage's df,
                // then the dump.
                docker.interrupt_at = Some(4);
            } else {
                docker.fail_on = Some(step);
            }
            let mut host = ready_host();
            assert_eq!(
                backup(&mut docker, &mut host, &request(&out), &dir.0),
                code,
                "{step} {:?}",
                host.errors
            );
            assert!(names(&out).is_empty(), "{step}: {:?}", names(&out));
            let text = host.errors.join("\n");
            assert!(text.starts_with("backup failed: "), "{text}");
            assert!(text.contains("No backup was written"), "{text}");
            // Backup's own words, never restore's safety backup's.
            assert!(!text.contains("safety"), "{text}");
            if step == "start-a helper1" && !interrupt {
                // tar's own words, and no retry for an error that is not a
                // changed file.
                assert!(
                    text.starts_with("backup failed: the archive of the state volume failed: tar or Docker exited 1 and it said: boom;"),
                    "{text}"
                );
            }
        }
    }

    #[test]
    fn refusals_name_the_cause_and_write_nothing() {
        let dir = Dir::new();
        let out = dir.0.join("backups");
        // An absent database.
        let mut docker = Fake {
            absent_db: true,
            ..fake("")
        };
        let mut host = ready_host();
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 2);
        assert!(
            host.errors[0].contains("the database db does not exist in p; name the bank"),
            "{:?}",
            host.errors
        );
        // Too little space where the backup goes.
        let mut docker = fake("");
        let mut host = FakeHost {
            no_space: true,
            ..ready_host()
        };
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 2);
        assert!(
            host.errors[0].contains("less than 8 GiB is free on"),
            "{:?}",
            host.errors
        );
        assert!(
            host.errors[0].ends_with("No backup was written"),
            "{:?}",
            host.errors
        );
        // The backup directory inside the volume it archives.
        let mut docker = fake(&dir.0.to_string_lossy());
        let mut host = ready_host();
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 2);
        assert!(
            host.errors[0].contains("is inside the daemon's /data volume"),
            "{:?}",
            host.errors
        );
        // --out naming a file.
        let file = dir.0.join("a-file");
        fs::write(&file, b"x").unwrap();
        let mut docker = fake("");
        let mut host = ready_host();
        assert_eq!(backup(&mut docker, &mut host, &request(&file), &dir.0), 2);
        assert!(
            host.errors[0].contains("which is not a directory"),
            "{:?}",
            host.errors
        );
        assert!(!out.exists());
    }

    #[test]
    fn a_ctrl_c_after_the_archive_was_read_publishes_nothing() {
        let dir = Dir::new();
        // Where the archive helper's start falls in a clean run.
        let mut clean = fake("");
        let mut host = ready_host();
        assert_eq!(
            backup(
                &mut clean,
                &mut host,
                &request(&dir.0.join("first")),
                &dir.0
            ),
            0
        );
        let at = clean
            .calls
            .iter()
            .position(|c| label(c) == "start-a helper1")
            .unwrap();
        // The interrupt lands as that call ends: the read-back that follows
        // runs no Docker call, so only the check before publishing sees it.
        let out = dir.0.join("backups");
        let mut docker = Fake {
            interrupt_after: Some(at),
            ..fake("")
        };
        let mut host = ready_host();
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 130);
        assert!(names(&out).is_empty(), "{:?}", names(&out));
        let text = host.errors.join(
            "
",
        );
        assert!(
            text.contains("the backup was interrupted. No backup was written"),
            "{text}"
        );
    }

    #[test]
    fn a_pair_whose_archive_cannot_be_renamed_leaves_no_dump() {
        let dir = Dir::new();
        let out = dir.0.join("backups");
        // A directory stands where the archive is renamed to.
        fs::create_dir_all(out.join(format!("pseudolife_state-{STAMP}.tgz"))).unwrap();
        let mut docker = fake("");
        let mut host = ready_host();
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 1);
        assert!(
            !out.join(format!("pseudolife_memory-{STAMP}.sql.gz"))
                .exists(),
            "{:?}",
            names(&out)
        );
        assert_eq!(names(&out), [format!("pseudolife_state-{STAMP}.tgz")]);
        let text = host.errors.join(
            "
",
        );
        assert!(
            text.contains("the backup could not be written to disk in"),
            "{text}"
        );
        assert!(text.contains("No backup was written"), "{text}");
    }

    fn aged(path: &Path, days: u64) {
        fs::write(path, b"x").unwrap();
        let when = SystemTime::now() - Duration::from_secs(days * 86400);
        fs::File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_modified(when)
            .unwrap();
    }

    /// `pseudolife_{memory,state}-2026110<day>-<hh>0000-0000000<day>`, aged.
    fn pair(d: &Path, day: u32, hour: u32, days_old: u64) -> [PathBuf; 2] {
        let stamp = format!("2026110{day}-{hour:02}0000-0000000{day}");
        let dump = d.join(format!("pseudolife_memory-{stamp}.sql.gz"));
        let state = d.join(format!("pseudolife_state-{stamp}.tgz"));
        aged(&dump, days_old);
        aged(&state, days_old);
        [dump, state]
    }

    fn sorted(mut paths: Vec<PathBuf>) -> Vec<PathBuf> {
        paths.sort();
        paths
    }

    #[test]
    fn rotation_removes_only_whole_old_pairs_and_keeps_the_newest_three() {
        let dir = Dir::new();
        let d = &dir.0;
        let pairs: Vec<[PathBuf; 2]> = (1..=6).map(|day| pair(d, day, 0, 30)).collect();
        // Never this command's to rotate: restore's safety pairs, the
        // updater's names (no nonce), partial files, other spellings.
        let others = [
            "pseudolife_safety_memory-20261101-000000-00000001.sql.gz",
            "pseudolife_memory-20261101-000000.sql.gz",
            "pseudolife_memory-20261101-000000-00000001.sql.gz.part",
            "pseudolife_memory-20261101-000000-0000000G.sql.gz",
            "pseudolife_lite_memory-20261101-000000.sql.gz",
        ];
        for name in others {
            aged(&d.join(name), 30);
        }
        // A native restore before the safety prefix wrote its pair under the
        // backup names, beside a note of the same stamp (day 3): kept. A
        // file that only looks like a note (day 2) protects nothing.
        fs::write(
            d.join("pseudolife_restore-20261103-000000-00000003.txt"),
            b"note",
        )
        .unwrap();
        fs::write(
            d.join("pseudolife_restore-20261102-000000-00000002.txt.bak"),
            b"x",
        )
        .unwrap();
        let keep: HashSet<PathBuf> = pairs[5].iter().cloned().collect();
        // A restore journal names the oldest pair's dump only: the pair is
        // kept whole.
        let protected: HashSet<PathBuf> = [pairs[0][0].clone()].into_iter().collect();
        let now = SystemTime::now();
        for keep_days in [f64::NAN, f64::INFINITY, 365.0] {
            let rotation = rotate(d, keep_days, &keep, &protected, now);
            assert!(rotation.removed.is_empty(), "{keep_days}: {rotation:?}");
        }
        let rotation = rotate(d, 7.0, &keep, &protected, now);
        // Newest three pairs kept (6, 5, 4); 1 is protected by the journal
        // and 3 by its restore note; only pair 2 goes, archive first.
        assert_eq!(
            rotation.removed,
            [pairs[1][1].clone(), pairs[1][0].clone()],
            "{rotation:?}"
        );
        assert!(rotation.failed.is_empty() && rotation.unpaired.is_empty());
        for name in others {
            assert!(d.join(name).exists(), "{name}");
        }
        for (index, [dump, state]) in pairs.iter().enumerate() {
            assert_eq!(dump.exists(), index != 1, "{}", dump.display());
            assert_eq!(state.exists(), index != 1, "{}", state.display());
        }
        // A negative keep has nothing further to remove.
        assert!(rotate(d, -1.0, &keep, &protected, now).removed.is_empty());
    }

    #[test]
    fn rotation_never_splits_a_pair_and_keeps_every_unpaired_half() {
        // The review's case: five backups and a 0.23.x restore's state-only
        // safety archive stamped between the second and third; then an
        // orphan dump (a run killed between its renames) and a pair whose
        // archive a user deleted.
        let dir = Dir::new();
        let d = &dir.0;
        let pairs: Vec<[PathBuf; 2]> = (1..=5).map(|day| pair(d, day, 0, 30)).collect();
        let lone_state = d.join("pseudolife_state-20261102-120000-0000000a.tgz");
        aged(&lone_state, 30);
        let lone_dump = d.join("pseudolife_memory-20261101-120000-0000000b.sql.gz");
        aged(&lone_dump, 30);
        let [bare_dump, gone_state] = pair(d, 4, 12, 30);
        fs::remove_file(&gone_state).unwrap();
        let rotation = rotate(d, 7.0, &HashSet::new(), &HashSet::new(), SystemTime::now());
        // The newest three complete pairs (5, 4, 3) stay; 2 and 1 go whole.
        assert_eq!(
            sorted(rotation.removed.clone()),
            sorted(pairs[..2].iter().flatten().cloned().collect()),
            "{rotation:?}"
        );
        for [dump, state] in &pairs[2..] {
            assert!(dump.exists() && state.exists(), "{}", dump.display());
        }
        // No half is removed and every one is listed, newest first.
        assert_eq!(
            rotation.unpaired,
            [bare_dump.clone(), lone_state.clone(), lone_dump.clone()]
        );
        for path in [&bare_dump, &lone_state, &lone_dump] {
            assert!(path.exists(), "{}", path.display());
        }
        // Every remaining dump has its archive, except the listed halves.
        for name in names(d) {
            if let Some((Half::Dump, stamp)) = rotation_name(&name) {
                let state = d.join(format!("pseudolife_state-{stamp}.tgz"));
                assert!(
                    state.exists() || rotation.unpaired.contains(&d.join(&name)),
                    "{name}"
                );
            }
        }
    }

    #[test]
    fn rotation_waits_for_both_halves_to_age_and_keeps_this_runs_pair_whatever_its_name() {
        let dir = Dir::new();
        let d = &dir.0;
        let pairs: Vec<[PathBuf; 2]> = (1..=5).map(|day| pair(d, day, 0, 30)).collect();
        // Pair 2's archive was written recently: the pair waits whole.
        aged(&pairs[1][1], 0);
        // This run's pair carries the oldest stamp (a clock set back): still
        // kept, though outside the newest three by name.
        let keep: HashSet<PathBuf> = pairs[0].iter().cloned().collect();
        let rotation = rotate(d, 7.0, &keep, &HashSet::new(), SystemTime::now());
        assert!(rotation.removed.is_empty(), "{rotation:?}");
        aged(&pairs[1][1], 30);
        let rotation = rotate(d, 7.0, &keep, &HashSet::new(), SystemTime::now());
        assert_eq!(rotation.removed, [pairs[1][1].clone(), pairs[1][0].clone()]);
        assert!(pairs[0].iter().all(|path| path.exists()));
    }

    #[cfg(unix)]
    #[test]
    fn a_link_with_a_backup_name_is_never_rotated() {
        let dir = Dir::new();
        let d = &dir.0;
        let target = d.join("elsewhere.sql.gz");
        aged(&target, 30);
        for day in 1..=4 {
            pair(d, day, 0, 30);
        }
        // The oldest stamp's dump is a symlink: no file of this command's,
        // so its archive is a half, and neither is removed.
        let link = d.join("pseudolife_memory-20261001-000000-00000001.sql.gz");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let state = d.join("pseudolife_state-20261001-000000-00000001.tgz");
        aged(&state, 30);
        let rotation = rotate(d, 7.0, &HashSet::new(), &HashSet::new(), SystemTime::now());
        assert_eq!(
            rotation.removed,
            [
                d.join("pseudolife_state-20261101-000000-00000001.tgz"),
                d.join("pseudolife_memory-20261101-000000-00000001.sql.gz"),
            ]
        );
        assert_eq!(rotation.unpaired, [state.clone()]);
        assert!(fs::symlink_metadata(&link).is_ok());
        assert!(target.exists() && state.exists());
    }

    #[test]
    fn journals_protect_their_files() {
        let dir = Dir::new();
        // A journal before `state_archive` was recorded: its backup file's
        // stamp names the state archive a finishing restore would read.
        fs::write(
            dir.0.join("restore-in-progress-0011223344556677.json"),
            json!({
                "safety_dump": "/x/a.sql.gz",
                "safety_state": "/x/a.tgz",
                "backup_file": "/x/pseudolife_memory-20261101-000000-0a1b2c3d.sql.gz",
            })
            .to_string(),
        )
        .unwrap();
        // One that records it.
        fs::write(
            dir.0.join("restore-in-progress-8899aabbccddeeff.json"),
            json!({"safety_state": "/y/b.tgz", "backup_file": "/y/b.sql.gz", "state_archive": "/y/state.tgz"})
                .to_string(),
        )
        .unwrap();
        let files = journal_files(&dir.0).unwrap();
        for path in [
            "/x/a.sql.gz",
            "/x/a.tgz",
            "/x/pseudolife_memory-20261101-000000-0a1b2c3d.sql.gz",
            "/x/pseudolife_state-20261101-000000-0a1b2c3d.tgz",
            "/y/b.sql.gz",
            "/y/state.tgz",
        ] {
            assert!(files.contains(Path::new(path)), "{path}: {files:?}");
        }
        // No data directory yet: no journal.
        assert_eq!(journal_files(&dir.0.join("absent")), Ok(HashSet::new()));
        // A data directory that cannot be listed (here a file): unknown.
        let file = dir.0.join("a-file");
        fs::write(&file, b"x").unwrap();
        let reason = journal_files(&file).unwrap_err();
        assert!(reason.contains("cannot be listed"), "{reason}");
        // An unreadable journal: its files are unknown, so nothing is known
        // to be safe to rotate.
        fs::write(
            dir.0.join("restore-in-progress-0000000000000000.json"),
            b"{not json",
        )
        .unwrap();
        let reason = journal_files(&dir.0).unwrap_err();
        assert!(
            reason.contains("restore-in-progress-0000000000000000.json is not valid JSON"),
            "{reason}"
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_data_directory_that_cannot_be_listed_protects_nothing_known() {
        use std::os::unix::fs::PermissionsExt;
        let dir = Dir::new();
        let data = dir.0.join("data");
        fs::create_dir(&data).unwrap();
        // Enterable, not listable.
        fs::set_permissions(&data, fs::Permissions::from_mode(0o300)).unwrap();
        let listable = fs::read_dir(&data).is_ok();
        let result = journal_files(&data);
        fs::set_permissions(&data, fs::Permissions::from_mode(0o700)).unwrap();
        if listable {
            eprintln!("running as root: a 0300 directory stays listable");
            return;
        }
        assert!(result.unwrap_err().contains("cannot be listed"));
    }

    #[test]
    fn rotation_names_match_exactly() {
        assert_eq!(
            rotation_name("pseudolife_memory-20261114-010203-0a1b2c3d.sql.gz"),
            Some((Half::Dump, "20261114-010203-0a1b2c3d"))
        );
        assert_eq!(
            rotation_name("pseudolife_state-20261114-010203-0a1b2c3d.tgz"),
            Some((Half::State, "20261114-010203-0a1b2c3d"))
        );
        for name in [
            "pseudolife_memory-20261114-010203-0A1B2C3D.sql.gz",
            "pseudolife_memory-20261114-010203.sql.gz",
            "pseudolife_memory-20261114-010203-0a1b2c3d.sql.gz.part",
            "pseudolife_safety_memory-20261114-010203-0a1b2c3d.sql.gz",
            "pseudolife_state-20261114-010203-0a1b2c3d.tar.gz",
            "xpseudolife_memory-20261114-010203-0a1b2c3d.sql.gz",
        ] {
            assert_eq!(rotation_name(name), None, "{name}");
        }
    }

    /// Five complete pairs a month old in `out`.
    fn old_backups(out: &Path) -> Vec<PathBuf> {
        fs::create_dir_all(out).unwrap();
        (1..=5).flat_map(|day| pair(out, day, 0, 30)).collect()
    }

    #[test]
    fn a_failed_backup_never_rotates_old_backups() {
        for step in ["refused", "safety-dump", "start-a helper1"] {
            let dir = Dir::new();
            let out = dir.0.join("backups");
            let old = old_backups(&out);
            let mut docker = if step == "refused" {
                Fake {
                    absent_db: true,
                    ..fake("")
                }
            } else {
                Fake {
                    fail_on: Some(step),
                    ..fake("")
                }
            };
            let mut host = ready_host();
            assert_ne!(backup(&mut docker, &mut host, &request(&out), &dir.0), 0);
            assert!(
                old.iter().all(|path| path.exists()),
                "{step}: {:?}",
                names(&out)
            );
            assert!(
                !host.info.iter().any(|l| l.starts_with("rotated out")),
                "{step}"
            );
        }
    }

    #[test]
    fn a_backup_rotates_around_what_a_restore_journal_names() {
        let dir = Dir::new();
        let out = dir.0.join("backups");
        let old = old_backups(&out);
        // A journal in the data directory names day 1's dump.
        fs::write(
            dir.0.join("restore-in-progress-0011223344556677.json"),
            json!({"backup_file": old[0].to_string_lossy()}).to_string(),
        )
        .unwrap();
        let mut docker = fake("");
        let mut host = ready_host();
        assert_eq!(
            backup(&mut docker, &mut host, &request(&out), &dir.0),
            0,
            "{:?}",
            host.errors
        );
        // Kept: this run's pair, days 5 and 4, and day 1 (the journal's,
        // with the archive named like it). Days 3 and 2 go.
        let rotated: Vec<&String> = host
            .info
            .iter()
            .filter(|l| l.starts_with("rotated out:   "))
            .collect();
        assert_eq!(rotated.len(), 4, "{:?}", host.info);
        for (index, path) in old.iter().enumerate() {
            assert_eq!(
                path.exists(),
                !(2..6).contains(&index),
                "{}",
                path.display()
            );
        }
    }

    #[test]
    fn a_journal_that_cannot_be_read_skips_rotation_and_the_backup_succeeds() {
        let dir = Dir::new();
        let out = dir.0.join("backups");
        let old = old_backups(&out);
        fs::write(
            dir.0.join("restore-in-progress-0000000000000000.json"),
            b"{not json",
        )
        .unwrap();
        let mut docker = fake("");
        let mut host = ready_host();
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 0);
        assert!(old.iter().all(|path| path.exists()));
        assert!(out.join(format!("pseudolife_state-{STAMP}.tgz")).exists());
        let text = host.errors.join("\n");
        assert!(
            text.starts_with("warning: rotation was skipped: the restore journal ")
                && text.contains("is not valid JSON")
                && text.ends_with("nothing was rotated (the new backup is complete)"),
            "{text}"
        );
        // A data directory that cannot be listed (here a file) alike.
        let file = dir.0.join("a-file");
        fs::write(&file, b"x").unwrap();
        let mut docker = fake("");
        let mut host = FakeHost {
            run: 1,
            ..ready_host()
        };
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &file), 0);
        assert!(old.iter().all(|path| path.exists()));
        assert!(
            host.errors.join("\n").contains("the data directory")
                && host.errors.join("\n").contains("cannot be listed"),
            "{:?}",
            host.errors
        );
    }

    #[test]
    fn a_changed_file_is_retried_and_the_kept_attempt_is_reported() {
        let dir = Dir::new();
        let out = dir.0.join("backups");
        let mut docker = Fake {
            archive_failures: vec!["tar: ./graph_snapshots/g.json: file changed as we read it"],
            ..fake("")
        };
        let mut host = ready_host();
        assert_eq!(
            backup(&mut docker, &mut host, &request(&out), &dir.0),
            0,
            "{:?}",
            host.errors
        );
        assert_eq!(
            names(&out),
            [
                format!("pseudolife_memory-{STAMP}.sql.gz"),
                format!("pseudolife_state-{STAMP}.tgz")
            ]
        );
        // One dump, two archive helpers: the verified dump is kept.
        let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
        assert_eq!(
            labels.iter().filter(|l| *l == "safety-dump").count(),
            1,
            "{labels:?}"
        );
        assert_eq!(
            labels
                .iter()
                .filter(|l| *l == "create safety-helper")
                .count(),
            2,
            "{labels:?}"
        );
        assert!(
            host.info.contains(&"state archive taken on attempt 2: tar reported a file in /data changing while it read the earlier attempts (it said: tar: ./graph_snapshots/g.json: file changed as we read it)".to_owned()),
            "{:?}",
            host.info
        );
    }

    #[test]
    fn a_file_that_keeps_changing_fails_with_tars_text_after_three_attempts() {
        let dir = Dir::new();
        let out = dir.0.join("backups");
        let changed = "tar: ./graph_snapshots/g.json: file changed as we read it";
        let mut docker = Fake {
            archive_failures: vec![changed, "tar: ./x: file removed before we read it", changed],
            ..fake("")
        };
        let mut host = ready_host();
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 1);
        assert!(names(&out).is_empty(), "{:?}", names(&out));
        let text = host.errors.join("\n");
        assert!(
            text.starts_with(&format!("backup failed: the archive of the state volume failed on attempt 3 of 3: tar or Docker exited 1 and it said: {changed};")),
            "{text}"
        );
        assert!(text.ends_with("No backup was written"), "{text}");
        // Another tar error is not retried.
        let mut docker = Fake {
            archive_failures: vec!["tar: ./x: Cannot open: Permission denied"],
            ..fake("")
        };
        let mut host = ready_host();
        assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 1);
        let text = host.errors.join("\n");
        assert!(
            text.starts_with("backup failed: the archive of the state volume failed: tar or Docker exited 1 and it said: tar: ./x: Cannot open: Permission denied;"),
            "{text}"
        );
        let labels: Vec<String> = docker.calls.iter().map(|c| label(c)).collect();
        assert_eq!(
            labels
                .iter()
                .filter(|l| *l == "create safety-helper")
                .count(),
            1
        );
    }

    #[test]
    fn a_step_that_never_ran_names_its_true_cause() {
        for (step, fault, words) in [
            (
                "safety-dump",
                "/out/x.part cannot be created (Permission denied)",
                "the dump of the current database could not be taken: /out/x.part cannot be created (Permission denied); fix that, then retry. No backup was written",
            ),
            (
                "start-a helper1",
                "it did not finish within 360 minutes",
                "the archive of the state volume could not be taken: it did not finish within 360 minutes; fix that, then retry. No backup was written",
            ),
        ] {
            let dir = Dir::new();
            let out = dir.0.join("backups");
            let mut docker = Fake {
                fault_on: Some((step, fault)),
                ..fake("")
            };
            let mut host = ready_host();
            assert_eq!(backup(&mut docker, &mut host, &request(&out), &dir.0), 1);
            assert_eq!(host.errors, [format!("backup failed: {words}")], "{step}");
            assert!(names(&out).is_empty(), "{step}");
        }
    }

    #[test]
    fn leftover_partial_files_of_killed_runs_are_removed_and_near_misses_kept() {
        let dir = Dir::new();
        let out = dir.0.join("backups");
        fs::create_dir_all(&out).unwrap();
        let killed = [
            out.join("pseudolife_memory-20261101-000000-00000001.sql.gz.part"),
            out.join("pseudolife_state-20261101-000000-00000001.tgz.part"),
        ];
        for path in &killed {
            aged(path, 1);
        }
        let kept = [
            "pseudolife_memory-20261101-000000-00000001.sql.gz.part.bak",
            "pseudolife_safety_memory-20261101-000000-00000001.sql.gz.part",
            "pseudolife_memory-20261101-000000.sql.gz.part",
            "pseudolife_state-20261101-000000-00000001.tar.gz.part",
            "pseudolife_state-20261101-000000-0000000G.tgz.part",
        ];
        for name in kept {
            aged(&out.join(name), 1);
        }
        // One still being written by a backup that takes no lock.
        let fresh = out.join("pseudolife_memory-20261102-000000-00000002.sql.gz.part");
        fs::write(&fresh, b"x").unwrap();
        let mut docker = fake("");
        let mut host = ready_host();
        assert_eq!(
            backup(&mut docker, &mut host, &request(&out), &dir.0),
            0,
            "{:?}",
            host.errors
        );
        for path in &killed {
            assert!(!path.exists(), "{}", path.display());
            assert!(
                host.info.contains(&format!(
                    "removed partial file an interrupted backup left: {}",
                    shown(&out).join(path.file_name().unwrap()).display()
                )),
                "{:?}",
                host.info
            );
        }
        for name in kept {
            assert!(out.join(name).exists(), "{name}");
        }
        assert!(fresh.exists());
    }

    #[test]
    fn a_published_file_that_cannot_be_removed_is_named_never_denied() {
        let dir = Dir::new();
        // A directory with content stands where a published file was: its
        // removal fails, as a file held open on Windows would.
        let held = dir
            .0
            .join("pseudolife_memory-20261114-000000-abcdef01.sql.gz");
        fs::create_dir_all(held.join("inside")).unwrap();
        let gone = dir.0.join("pseudolife_state-20261114-000000-abcdef01.tgz");
        fs::write(&gone, b"x").unwrap();
        let left = safety::unpublish(&[held.as_path(), gone.as_path()]);
        assert!(!gone.exists());
        assert_eq!(left.len(), 1, "{left:?}");
        assert!(left[0].starts_with(&held.display().to_string()), "{left:?}");
        let message = ours(&safety::with_leftovers(
            "the backup could not be written to disk".into(),
            &left,
        ));
        assert!(message.contains(safety::LEFT_BEHIND), "{message}");
        assert!(
            message.ends_with("This run reports no backup, so delete it"),
            "{message}"
        );
        assert!(!message.contains(NOTHING), "{message}");
        // Nothing left: the ordinary ending.
        assert!(ours(&safety::with_leftovers("x".into(), &[])).ends_with(NOTHING));
    }

    #[test]
    fn docker_that_cannot_be_asked_is_unknown_and_an_empty_listing_is_no_bank() {
        use docker::Ran;
        let listing = |names: &str| Ran {
            code: 0,
            stdout: names.as_bytes().to_vec(),
            ..Ran::default()
        };
        assert_eq!(
            listed(&listing(&format!("other\n{}\n", bank::DAEMON_CONTAINER))),
            Configured::Yes
        );
        assert_eq!(
            listed(&listing(&format!("{}\r\n", bank::PG_CONTAINER))),
            Configured::Yes
        );
        // Near misses name no bank.
        assert_eq!(
            listed(&listing(&format!(
                "{}-old\nx{}\n",
                bank::DAEMON_CONTAINER,
                bank::PG_CONTAINER
            ))),
            Configured::No
        );
        assert_eq!(listed(&listing("")), Configured::No);
        let down = Ran {
            code: 1,
            stderr: b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n".to_vec(),
            ..Ran::default()
        };
        assert_eq!(
            listed(&down),
            Configured::Unknown("docker ps -a exited 1: Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?".into())
        );
        let slow = Ran {
            code: 1,
            failed: true,
            fault: Some("it did not finish within 10 seconds".into()),
            ..Ran::default()
        };
        assert_eq!(
            listed(&slow),
            Configured::Unknown("docker ps -a failed: it did not finish within 10 seconds".into())
        );
    }
}
