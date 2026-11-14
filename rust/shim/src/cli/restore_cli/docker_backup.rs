//! `pseudolife-mcp backup` of a Docker bank: the same verified backup
//! restore takes before it replaces a bank (`safety::backup`), written under
//! the backup names and rotated. The daemon keeps serving: nothing is
//! stopped. The pair a run reports exists, is complete and was read back
//! (the dump's completion marker, the archive's full read and path checks)
//! before its rename; a failed or interrupted run leaves no file under a
//! backup name.
use std::{
    collections::HashSet,
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

/// A refusal text from the shared checks, with this command's ending.
fn ours(message: &str) -> String {
    let message = message
        .replace("Nothing was restored", NOTHING)
        .replace("Nothing was changed", NOTHING);
    if message.ends_with(NOTHING) {
        message
    } else {
        format!("{message}. {NOTHING}")
    }
}

/// Whether a Docker bank is configured on this host: `docker` resolves and
/// the daemon or PostgreSQL container answers `docker inspect`. An installed
/// but stopped engine, or no such container, is no Docker bank (the file and
/// DSN tiers then answer).
pub(in crate::cli) fn configured() -> bool {
    let Ok(program) = docker::program() else {
        return false;
    };
    let mut runner = docker::Real {
        program,
        interruptible: false,
    };
    [bank::DAEMON_CONTAINER, bank::PG_CONTAINER]
        .iter()
        .any(|name| {
            let args: Vec<String> = ["inspect", "-f", "{{.Id}}", name]
                .iter()
                .map(|s| (*s).to_owned())
                .collect();
            let ran = runner.run(&args, Duration::from_secs(10));
            ran.ok() && !String::from_utf8_lossy(&ran.stdout).trim().is_empty()
        })
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
                    "another pseudolife-mcp update or restore is running (pid {pid}); nothing was changed. Run this again when it has finished"
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
        if code == 130 || interrupt::requested() {
            failed(
                host,
                "the backup was interrupted. No backup was written (its partial files were removed)",
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
    // A Ctrl-C after the pair was published still cancels the backup: its
    // files go and nothing is rotated.
    if bank.docker.interrupted() {
        let _ = fs::remove_file(&dump);
        let _ = fs::remove_file(&pair.state);
        return refusal(host, Stop("interrupted".into(), 130));
    }
    // The files reported are the files on disk.
    for path in [&dump, &pair.state] {
        if !fs::symlink_metadata(path).is_ok_and(|meta| meta.is_file() && meta.len() > 0) {
            let _ = fs::remove_file(&dump);
            let _ = fs::remove_file(&pair.state);
            failed(
                host,
                &format!(
                    "{} is missing after it was written; check the backup directory's filesystem, then retry. No backup was written",
                    path.display()
                ),
            );
            return 1;
        }
    }
    host.info(&format!("bank dump:     {}", dump.display()));
    host.info(&format!("state archive: {}", pair.state.display()));
    let keep: HashSet<PathBuf> = [dump, pair.state.clone()].into_iter().collect();
    let Some(protected) = journal_files(data_dir) else {
        host.error(&format!(
            "warning: a restore journal in {} cannot be read, so the files it names are unknown; nothing was rotated (the new backup is complete)",
            data_dir.display()
        ));
        return 0;
    };
    let rotated = rotate(
        Path::new(&resolved),
        request.keep_days,
        &keep,
        &protected,
        SystemTime::now(),
    );
    for (path, outcome) in rotated {
        match outcome {
            Ok(()) => host.info(&format!("rotated out:   {}", path.display())),
            Err(error) => host.error(&format!(
                "warning: could not rotate out {} ({error}); the new backup is complete",
                path.display()
            )),
        }
    }
    0
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

/// The stamp of one of this command's names, which differs from a restore
/// note's stamp only in its prefix and extension.
fn rotation_stamp(name: &str) -> Option<&str> {
    name.strip_prefix("pseudolife_memory-")
        .and_then(|rest| rest.strip_suffix(".sql.gz"))
        .or_else(|| {
            name.strip_prefix("pseudolife_state-")
                .and_then(|rest| rest.strip_suffix(".tgz"))
        })
}

/// `pseudolife_memory-YYYYmmdd-HHMMSS-<8 hex>.sql.gz` or
/// `pseudolife_state-...tgz`: this command's own names, nothing else.
fn rotation_kind(name: &str) -> Option<&'static str> {
    let (kind, rest) = if let Some(rest) = name.strip_prefix("pseudolife_memory-") {
        ("dump", rest.strip_suffix(".sql.gz")?)
    } else if let Some(rest) = name.strip_prefix("pseudolife_state-") {
        ("state", rest.strip_suffix(".tgz")?)
    } else {
        return None;
    };
    let bytes = rest.as_bytes();
    let digits = |range: std::ops::Range<usize>| bytes[range].iter().all(u8::is_ascii_digit);
    let hex = |range: std::ops::Range<usize>| {
        bytes[range]
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
    };
    (bytes.len() == 24
        && digits(0..8)
        && bytes[8] == b'-'
        && digits(9..15)
        && bytes[15] == b'-'
        && hex(16..24))
    .then_some(kind)
}

/// The files every restore journal in `data_dir` names: never rotated.
/// `None` when a journal cannot be read: its files are then unknown. A
/// journal from before `state_archive` was recorded protects the state
/// archive named like its backup file too.
fn journal_files(data_dir: &Path) -> Option<HashSet<PathBuf>> {
    let mut files = HashSet::new();
    let Ok(entries) = fs::read_dir(data_dir) else {
        return Some(files);
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with("restore-in-progress") || !name.ends_with(".json") {
            continue;
        }
        let bytes = fs::read(entry.path()).ok()?;
        let value = serde_json::from_slice::<serde_json::Value>(&bytes).ok()?;
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
            if let Some(stamp) = backup
                .file_name()
                .and_then(|name| name.to_str())
                .and_then(rotation_stamp)
            {
                files.insert(backup.with_file_name(format!("pseudolife_state-{stamp}.tgz")));
            }
        }
    }
    Some(files)
}

fn same_file(a: &Path, b: &Path) -> bool {
    a == b
        || match (fs::canonicalize(a), fs::canonicalize(b)) {
            (Ok(a), Ok(b)) => a == b,
            _ => false,
        }
}

/// Remove this command's own backups older than `keep_days`, always keeping
/// the newest three of each kind, this run's files and every file a restore
/// journal names. Runs only after this run's pair was written. A NaN or
/// `+inf` `keep_days` removes nothing; a negative one (`-inf` too) keeps
/// only the newest three.
pub(super) fn rotate(
    directory: &Path,
    keep_days: f64,
    keep: &HashSet<PathBuf>,
    protected: &HashSet<PathBuf>,
    now: SystemTime,
) -> Vec<(PathBuf, std::io::Result<()>)> {
    let mut out = Vec::new();
    let Ok(entries) = fs::read_dir(directory) else {
        return out;
    };
    let mut kinds: [(&str, Vec<(String, PathBuf)>); 2] =
        [("dump", Vec::new()), ("state", Vec::new())];
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
    for entry in entries {
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        // A restore's safety pair under the backup names, kept by its note.
        if rotation_stamp(&name).is_some_and(|stamp| noted.contains(stamp)) {
            continue;
        }
        let Some(kind) = rotation_kind(&name) else {
            continue;
        };
        let path = entry.path();
        // Regular files only: never a link, a directory or a `.part`.
        if !fs::symlink_metadata(&path).is_ok_and(|meta| meta.is_file()) {
            continue;
        }
        for (k, list) in kinds.iter_mut() {
            if *k == kind {
                list.push((name.clone(), path.clone()));
            }
        }
    }
    let now = now
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64());
    let cutoff = now - keep_days * 86400.0;
    for (_, list) in kinds.iter_mut() {
        list.sort_by(|a, b| b.0.cmp(&a.0));
        for (_, path) in list.iter().skip(3) {
            if keep.iter().any(|k| same_file(k, path))
                || protected.iter().any(|p| same_file(p, path))
            {
                continue;
            }
            let Ok(modified) = fs::metadata(path).and_then(|m| m.modified()) else {
                continue;
            };
            let modified = modified
                .duration_since(SystemTime::UNIX_EPOCH)
                .map_or(0.0, |d| d.as_secs_f64());
            // NaN compares false: nothing is old enough.
            if modified < cutoff {
                out.push((path.clone(), fs::remove_file(path)));
            }
        }
    }
    out
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
        let dump = out.join(format!("pseudolife_memory-{STAMP}.sql.gz"));
        let state = out.join(format!("pseudolife_state-{STAMP}.tgz"));
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
                assert!(
                    text.starts_with("backup failed: the archive of the state volume failed (tar reports a file in /data changed while it was read, or Docker failed to run the helper, code 1);"),
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

    #[test]
    fn rotation_removes_only_its_own_old_files_and_keeps_the_newest_three() {
        let dir = Dir::new();
        let d = &dir.0;
        let dumps: Vec<PathBuf> = (1..=6)
            .map(|day| {
                d.join(format!(
                    "pseudolife_memory-2026110{day}-000000-0000000{day}.sql.gz"
                ))
            })
            .collect();
        for path in &dumps {
            aged(path, 30);
        }
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
        let keep: HashSet<PathBuf> = [dumps[5].clone()].into_iter().collect();
        // A restore journal names the oldest one.
        let protected: HashSet<PathBuf> = [dumps[0].clone()].into_iter().collect();
        let now = SystemTime::now();
        for keep_days in [f64::NAN, f64::INFINITY, 365.0] {
            assert!(
                rotate(d, keep_days, &keep, &protected, now).is_empty(),
                "{keep_days}"
            );
        }
        let mut removed: Vec<PathBuf> = rotate(d, 7.0, &keep, &protected, now)
            .into_iter()
            .map(|(path, outcome)| {
                outcome.unwrap();
                path
            })
            .collect();
        // Newest three kept (6, 5, 4); 1 is protected by the journal and 3
        // by its restore note; only 2 goes.
        removed.sort();
        assert_eq!(removed, [dumps[1].clone()]);
        for name in others {
            assert!(d.join(name).exists(), "{name}");
        }
        // A negative keep has nothing further to remove.
        assert!(rotate(d, -1.0, &keep, &protected, now).is_empty());
    }

    #[test]
    fn rotation_treats_state_archives_alike_and_keeps_this_runs_pair_whatever_its_name() {
        let dir = Dir::new();
        let d = &dir.0;
        let states: Vec<PathBuf> = (1..=5)
            .map(|day| {
                d.join(format!(
                    "pseudolife_state-2026110{day}-000000-0000000{day}.tgz"
                ))
            })
            .collect();
        for path in &states {
            aged(path, 30);
        }
        // This run's pair carries the oldest stamp (a clock set back): still
        // kept, though outside the newest three by name.
        let keep: HashSet<PathBuf> = [states[0].clone()].into_iter().collect();
        let mut removed: Vec<PathBuf> = rotate(d, 7.0, &keep, &HashSet::new(), SystemTime::now())
            .into_iter()
            .map(|(path, outcome)| {
                outcome.unwrap();
                path
            })
            .collect();
        removed.sort();
        assert_eq!(removed, [states[1].clone()]);
        assert!(states[0].exists());
    }

    #[cfg(unix)]
    #[test]
    fn a_link_with_a_backup_name_is_never_rotated() {
        let dir = Dir::new();
        let d = &dir.0;
        let target = d.join("elsewhere.sql.gz");
        aged(&target, 30);
        for day in 1..=4 {
            aged(
                &d.join(format!(
                    "pseudolife_memory-2026110{day}-000000-0000000{day}.sql.gz"
                )),
                30,
            );
        }
        // The oldest name is a symlink: never this command's to remove.
        let link = d.join("pseudolife_memory-20261001-000000-00000001.sql.gz");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let removed: Vec<PathBuf> =
            rotate(d, 7.0, &HashSet::new(), &HashSet::new(), SystemTime::now())
                .into_iter()
                .map(|(path, _)| path)
                .collect();
        assert_eq!(
            removed,
            [d.join("pseudolife_memory-20261101-000000-00000001.sql.gz")]
        );
        assert!(fs::symlink_metadata(&link).is_ok());
        assert!(target.exists());
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
        // An unreadable journal: its files are unknown, so nothing is known
        // to be safe to rotate.
        fs::write(
            dir.0.join("restore-in-progress-0000000000000000.json"),
            b"{not json",
        )
        .unwrap();
        assert_eq!(journal_files(&dir.0), None);
    }

    #[test]
    fn rotation_names_match_exactly() {
        assert_eq!(
            rotation_kind("pseudolife_memory-20261114-010203-0a1b2c3d.sql.gz"),
            Some("dump")
        );
        assert_eq!(
            rotation_kind("pseudolife_state-20261114-010203-0a1b2c3d.tgz"),
            Some("state")
        );
        for name in [
            "pseudolife_memory-20261114-010203-0A1B2C3D.sql.gz",
            "pseudolife_memory-20261114-010203.sql.gz",
            "pseudolife_memory-20261114-010203-0a1b2c3d.sql.gz.part",
            "pseudolife_safety_memory-20261114-010203-0a1b2c3d.sql.gz",
            "pseudolife_state-20261114-010203-0a1b2c3d.tar.gz",
            "xpseudolife_memory-20261114-010203-0a1b2c3d.sql.gz",
        ] {
            assert_eq!(rotation_kind(name), None, "{name}");
        }
    }
}
