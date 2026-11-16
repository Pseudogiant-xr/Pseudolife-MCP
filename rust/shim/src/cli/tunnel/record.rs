//! Tunnel process records (`<name>.process.json`): whether the process a
//! record names still runs. Read-only: nothing here stops, signals or starts
//! a process.
//!
//! A record names the tunnel process and its supervisor by pid, image and
//! argv (`runtime::record_processes`, shared with runtime pruning). A named
//! process runs when its pid is in the process table with the recorded
//! image. A pid the table does not hold, or holds under another image, is
//! gone: the table lists every process of this account (on Linux any whose
//! image or absolute argv[0] can be read), and a tunnel runs as the account
//! that started it. On Windows a pid that exists but cannot be opened is
//! unknown, never gone. Process creation times are not compared, so a pid
//! reused by the same program reads as running; that only ever errs towards
//! "running".
use std::path::{Path, PathBuf};

use serde_json::Value;

use super::Fail;
use super::store::{self, occupied};
use super::syntax::{Admit, json_loads};

const RECORD: &str = ".process.json";

/// One row of the process table: pid, parent pid, image.
pub(super) type Row = (u32, u32, PathBuf);

#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) enum Liveness {
    /// A process the record names runs under its recorded image.
    Running,
    /// No process the record names runs.
    Gone,
    /// Whether a tunnel runs is unknown; `Record::why` says which cause.
    Unknown,
}

/// The one cause, known to the code, of an `Unknown` verdict.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) enum Why {
    /// The file is not an owner-only JSON object this client can read.
    Unreadable,
    /// The record names no usable process id (none, or not a whole number).
    NoProcessId,
    /// This machine's process table could not be read.
    NoTable,
    /// A named process exists but this account cannot inspect its image.
    Uninspectable,
}

#[derive(Debug)]
pub(super) struct Record {
    pub path: PathBuf,
    pub liveness: Liveness,
    /// The record's own readiness flag, meaningful only while it runs.
    pub ready: bool,
    /// Why the verdict is `Unknown`; `None` otherwise.
    pub why: Option<Why>,
}

/// This machine's process table, or `None` where it cannot be read. On
/// Windows a pid the Toolhelp snapshot holds but `OpenProcess` refuses (an
/// elevated or another account's process) is added with an empty image, as
/// runtime pruning adds it: such a pid is not proof that a tunnel stopped.
pub(super) fn table() -> Option<Vec<Row>> {
    #[allow(unused_mut, reason = "only Windows adds unopenable pids")]
    let mut rows = crate::cli::process_table().ok().flatten()?;
    #[cfg(windows)]
    {
        let (pids, completed) = crate::runtime::windows_snapshot().ok()?;
        if !completed {
            return None;
        }
        for (pid, ppid) in pids {
            if !rows.iter().any(|row| row.0 == pid) {
                rows.push((pid, ppid, PathBuf::new()));
            }
        }
    }
    Some(rows)
}

pub(super) fn path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}{RECORD}"))
}

/// The record at `path`, or `None` when there is none. A record this client
/// cannot read is reported as such, never a reason to stop.
pub(super) fn read(path: PathBuf, table: Option<&[Row]>) -> Option<Record> {
    // A path that cannot even be inspected still names a record: unknown.
    if !occupied(&path).unwrap_or(true) {
        return None;
    }
    let parsed = match store::private_read(&path, 1024 * 1024) {
        Ok(data) => match json_loads(&data) {
            Admit::Yes(value @ Value::Object(_)) => Some(value),
            _ => None,
        },
        Err(_) => None,
    };
    let Some(record) = parsed else {
        return Some(Record {
            path,
            liveness: Liveness::Unknown,
            ready: false,
            why: Some(Why::Unreadable),
        });
    };
    let (liveness, why) = match table {
        None if crate::runtime::record_processes(&record).is_empty() => {
            (Liveness::Unknown, Some(Why::NoProcessId))
        }
        None => (Liveness::Unknown, Some(Why::NoTable)),
        Some(rows) => assess(&record, rows),
    };
    Some(Record {
        path,
        liveness,
        ready: record.get("ready") == Some(&Value::Bool(true)),
        why,
    })
}

/// Whether any process `record` names runs under its recorded image.
#[cfg(test)]
pub(super) fn liveness(record: &Value, rows: &[Row]) -> Liveness {
    assess(record, rows).0
}

/// The verdict and, for `Unknown`, its cause. "Gone" needs an observation:
/// at least one named pid, none of them running under its image. A record
/// naming no usable pid, or a pid whose image cannot be read, is unknown.
fn assess(record: &Value, rows: &[Row]) -> (Liveness, Option<Why>) {
    let processes = crate::runtime::record_processes(record);
    if processes.is_empty() {
        return (Liveness::Unknown, Some(Why::NoProcessId));
    }
    let mut verdict = (Liveness::Gone, None);
    for process in processes {
        for row in rows.iter().filter(|row| u64::from(row.0) == process.pid) {
            let matched = match (process.exe, process.argv0) {
                _ if row.2.as_os_str().is_empty() => {
                    verdict = (Liveness::Unknown, Some(Why::Uninspectable));
                    continue;
                }
                // A record that kept no image is matched by pid alone.
                (None, None) => true,
                (exe, argv0) => [exe, argv0]
                    .into_iter()
                    .flatten()
                    .any(|recorded| same_image(&row.2, Path::new(recorded))),
            };
            if matched {
                return (Liveness::Running, None);
            }
        }
    }
    verdict
}

/// `os.path.normcase` equality on Windows; on POSIX the two paths resolve to
/// the same file (a table image may be a symlinked argv[0], while the record
/// kept the resolved executable).
fn same_image(row: &Path, recorded: &Path) -> bool {
    if row.as_os_str().is_empty() {
        return false;
    }
    if cfg!(windows) {
        let fold = |path: &Path| path.to_string_lossy().replace('/', "\\").to_lowercase();
        fold(row) == fold(recorded)
    } else {
        let resolve = |path: &Path| std::fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
        row == recorded || resolve(row) == resolve(recorded) || cut_at_space(row, recorded)
    }
}

/// The macOS table reads `ps -o args=` up to its first whitespace, so a
/// recorded image whose path holds a space shows only its head. Accepting
/// that head errs only towards "running".
fn cut_at_space(row: &Path, recorded: &Path) -> bool {
    match (row.to_str(), recorded.to_str()) {
        (Some(row), Some(recorded)) => recorded
            .strip_prefix(row)
            .is_some_and(|rest| rest.starts_with(char::is_whitespace)),
        _ => false,
    }
}

/// Record files in `root` with no saved profile beside them, by file name,
/// sorted. `profiles` are the store's listed profile names.
pub(super) fn orphans(root: &Path, profiles: &[String]) -> Result<Vec<String>, Fail> {
    let mut found = Vec::new();
    let entries = std::fs::read_dir(root).map_err(|_| Fail::Defer)?;
    for entry in entries {
        let entry = entry.map_err(|_| Fail::Defer)?;
        let Some(file_name) = entry.file_name().to_str().map(str::to_owned) else {
            return Err(Fail::Defer);
        };
        // Windows globbing folds case, as the profile listing does.
        let folded = if cfg!(windows) {
            file_name.to_ascii_lowercase()
        } else {
            file_name.clone()
        };
        let Some(stem) = folded.strip_suffix(RECORD) else {
            continue;
        };
        let owned = profiles.iter().any(|name| {
            if cfg!(windows) {
                name.eq_ignore_ascii_case(stem)
            } else {
                name == stem
            }
        });
        if !owned {
            found.push(file_name);
        }
    }
    found.sort();
    Ok(found)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn own() -> Row {
        (std::process::id(), 0, std::env::current_exe().unwrap())
    }

    #[test]
    fn a_record_runs_only_while_its_pid_holds_its_image() {
        let (pid, _, exe) = own();
        let rows = vec![own()];
        let alive = json!({"pid": pid, "created": 0.0, "exe": exe, "argv": [exe]});
        assert_eq!(liveness(&alive, &rows), Liveness::Running);
        // The same pid under another image: the recorded process is gone.
        let reused = json!({"pid": pid, "exe": "/nonexistent/tunnel-client", "argv": ["/nonexistent/tunnel-client"]});
        assert_eq!(liveness(&reused, &rows), Liveness::Gone);
        // A pid no table holds.
        let dead = json!({"pid": u64::from(pid) + 1, "exe": exe});
        assert_eq!(liveness(&dead, &rows), Liveness::Gone);
        // No process identity at all.
        // No usable process id: nothing was observed, so unknown.
        for record in [
            json!({}),
            json!({"pid": "12"}),
            json!({"pid": -3}),
            json!({"pid": 1.5}),
        ] {
            assert_eq!(liveness(&record, &rows), Liveness::Unknown, "{record}");
        }
        // A live supervisor keeps the record running though its child died.
        let supervised = json!({"pid": u64::from(pid) + 1, "exe": "/nonexistent/tunnel-client",
            "supervisor": {"pid": pid, "exe": exe}});
        assert_eq!(liveness(&supervised, &rows), Liveness::Running);
        // A record that kept no image is matched by pid alone.
        assert_eq!(liveness(&json!({"pid": pid}), &rows), Liveness::Running);
        // A pid that exists but whose image cannot be read (Windows
        // `OpenProcess` refused): unknown, so its record is never called
        // deletable.
        let unopenable = vec![(4242, 4, PathBuf::new())];
        let record = json!({"pid": 4242, "exe": exe});
        assert_eq!(liveness(&record, &unopenable), Liveness::Unknown);
        assert!(crate::runtime::record_keeps_runtime(&record, &unopenable));
        // ...unless another process it names is running.
        let mixed = vec![(4242, 4, PathBuf::new()), own()];
        let record = json!({"pid": 4242, "exe": exe, "supervisor": {"pid": pid, "exe": exe}});
        assert_eq!(liveness(&record, &mixed), Liveness::Running);
    }

    #[test]
    fn a_running_record_always_keeps_its_runtime_from_pruning() {
        // Invariant: running here implies kept by runtime pruning, which
        // reads the same pids (`record_processes`) without the image match.
        let (pid, _, exe) = own();
        let rows = vec![own(), (pid + 4, pid, PathBuf::from("/other/image"))];
        let other = u64::from(pid) + 4;
        let records = [
            json!({"pid": pid, "exe": exe}),
            json!({"pid": pid, "exe": "/nonexistent"}),
            json!({"pid": other, "exe": exe}),
            json!({"pid": other}),
            json!({"pid": 1, "supervisor": {"pid": pid, "exe": exe}}),
            json!({"pid": 1, "target": {"pid": other}}),
            json!({"supervisor": {"pid": "not a pid"}}),
            json!({}),
        ];
        let mut running = 0;
        for record in &records {
            if liveness(record, &rows) == Liveness::Running {
                running += 1;
                assert!(
                    crate::runtime::record_keeps_runtime(record, &rows),
                    "{record}"
                );
            }
        }
        assert!(running >= 4, "the cases exercise running records");
    }

    #[test]
    fn the_real_table_holds_this_test_process() {
        let (pid, _, exe) = own();
        let rows = table().expect("this platform's process table");
        let record = json!({"pid": pid, "created": 0.0, "exe": exe, "argv": [exe]});
        assert_eq!(liveness(&record, &rows), Liveness::Running);
    }

    #[cfg(windows)]
    #[test]
    fn windows_images_compare_as_normcase_does() {
        assert!(same_image(
            Path::new(r"C:\Tools\Tunnel-Client.EXE"),
            Path::new("c:/tools/tunnel-client.exe")
        ));
        assert!(!same_image(Path::new(""), Path::new("")));
    }

    #[cfg(unix)]
    #[test]
    fn a_symlinked_argv0_is_the_recorded_executable() {
        let base = std::env::temp_dir().join(format!("tunnel-record-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&base).unwrap();
        let real = base.join("python3.12");
        std::fs::write(&real, b"").unwrap();
        let link = base.join("python3");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert!(same_image(&link, &real));
        assert!(!same_image(&link, &base.join("other")));
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(unix)]
    #[test]
    fn an_image_path_with_a_space_matches_the_head_the_macos_table_shows() {
        let recorded = Path::new("/Users/someone/Application Support/tunnel-client");
        // What `ps -o args=` split at whitespace yields for that process.
        assert!(same_image(
            Path::new("/Users/someone/Application"),
            recorded
        ));
        let rows = vec![(7, 1, PathBuf::from("/Users/someone/Application"))];
        let record = json!({"pid": 7, "exe": recorded, "argv": [recorded]});
        assert_eq!(liveness(&record, &rows), Liveness::Running);
        // A head that is not followed by whitespace is another image.
        assert!(!same_image(Path::new("/Users/someone/App"), recorded));
        assert!(!same_image(Path::new("/opt/other"), recorded));
    }
}
