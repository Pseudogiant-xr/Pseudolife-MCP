//! The move record: what a move has done so far, kept where a rollback,
//! a resumed run or a person can read it.
//!
//! One `record.json` per move under `~/.pseudolife-mcp/moves/<id>/`, in a
//! folder only this user can use. Every flag is written before the effect
//! it names, so a set flag means "may have happened" and the rollback undoes
//! it (each undo is safe to repeat). A write that fails stops the move before
//! its next effect: without the record, neither a rollback nor a person
//! could tell what was done. The record never holds a secret.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// The record format this client writes and reads.
pub(super) const RECORD_VERSION: u32 = 1;

/// Where the move stands.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum Phase {
    /// The transaction has begun; nothing irreversible is known yet.
    #[default]
    Started,
    /// The source has been or may have been stopped.
    Moving,
    /// The commit point passed: the bank lives on the target.
    Moved,
    /// Moved, and every follow-up step finished.
    Complete,
    /// Moved, with follow-up steps left for a person.
    MovedFollowUps,
    /// Failed before the commit point, and every undo succeeded.
    RolledBack,
    /// Failed before the commit point, and some undo did not succeed.
    RollbackIncomplete,
}

/// The effects a move may have taken, each set (and written) before the
/// effect it names.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub(super) struct Flags {
    /// The target's claim was asked for (it names this move once taken).
    pub claimed: bool,
    pub target_timer_paused: bool,
    pub target_policy_paused: bool,
    pub schedule_paused: bool,
    pub source_stopped: bool,
    pub fenced: bool,
    /// The source database refuses writes from new sessions
    /// (`default_transaction_read_only`), set before the final dump.
    pub read_only: bool,
    pub target_quiesced: bool,
    pub marker_written: bool,
    pub gate_written: bool,
    pub target_restored: bool,
    /// Set before the gate is lifted and the target started: from here the
    /// target may be running, and the rollback stops and re-gates it.
    pub gate_lifting: bool,
    pub target_started: bool,
    pub source_policy_changed: bool,
    pub moved_written: bool,
}

/// One undo the rollback attempted.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct Undo {
    pub step: String,
    pub ok: bool,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub detail: String,
}

/// What a move has done, as written to `record.json`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub(super) struct Record {
    pub version: u32,
    pub move_id: String,
    /// The ssh target, as given to `--to`.
    pub target: String,
    pub target_url: String,
    /// The source bank's fingerprint, which the target must end up serving.
    pub source_bank: String,
    pub phase: Phase,
    /// Set inside the commit window, right after `moved.json` reached the
    /// stopped source.
    pub committed: bool,
    pub flags: Flags,
    /// The target daemon's restart policy before the move paused it.
    pub target_policy: String,
    /// The source daemon's restart policy before the move changed it.
    pub source_policy: String,
    /// `--target-work-dir` as given (`""`: `~/.pseudolife-mcp/moves`).
    pub work_dir: String,
    /// This host's folder for the move's files.
    pub local_dir: String,
    /// Whether the target daemon ran when the move began.
    pub target_was_running: bool,
    /// The command that re-enables this host's unattended update.
    pub schedule_enable: String,
    /// Each table's row count in the final dump.
    pub dump_counts: std::collections::BTreeMap<String, i64>,
    /// The restored target's user-data rows, read before it first started:
    /// what a rollback compares with, to find writes the target took.
    pub restored_counts: std::collections::BTreeMap<String, i64>,
    /// The restored target's mutation counters before it first started.
    pub restored_mutations: Option<std::collections::BTreeMap<String, i64>>,
    /// The source bank's containers, database, role, port and image as the
    /// preflight found them: a rollback works from these, since a fenced
    /// database refuses the queries that would find them again.
    pub source: super::probe::Facts,
    /// The target daemon's image, for the manual rollback's gate command.
    pub target_image: String,
    /// The move's folder on the target, and what its files took at the last
    /// reading.
    pub remote_folder: String,
    pub remote_bytes: Option<u64>,
    /// Tables whose rows on the target differ from the dump's at rollback:
    /// writes the target took while it served, kept in its gated bank.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parked_writes: Option<String>,
    pub updated_at: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub rollback: Vec<Undo>,
    /// Commands that undo what was done, in order, for a person.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub manual_rollback: Vec<String>,
    /// Commands left to run after the commit point.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub follow_ups: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// The ID of a new move: its UTC start time and eight random hex digits,
/// as Python's move names them (`YYYYMMDD-HHMMSS-xxxxxxxx`).
pub(super) fn new_id(now: chrono::DateTime<chrono::Utc>) -> String {
    let random = uuid::Uuid::new_v4().simple().to_string();
    format!("{}-{}", now.format("%Y%m%d-%H%M%S"), &random[..8])
}

/// Whether `id` is a move ID this client could have made; anything else
/// is never used in a path.
pub(super) fn valid_id(id: &str) -> bool {
    let bytes = id.as_bytes();
    bytes.len() == 24
        && bytes.iter().enumerate().all(|(at, byte)| match at {
            8 | 15 => *byte == b'-',
            0..=7 | 9..=14 => byte.is_ascii_digit(),
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(byte),
        })
}

/// Where records are kept.
pub(super) trait Store {
    /// Writes `record` whole, replacing the last one, or says why not.
    fn save(&mut self, record: &Record) -> Result<(), String>;
    /// The record of move `id`, if one was kept.
    fn load(&mut self, id: &str) -> Result<Option<Record>, String>;
}

/// Records under `<home>/.pseudolife-mcp/moves/<id>/record.json`.
pub(super) struct Files {
    pub moves: PathBuf,
}

impl Files {
    pub(super) fn under_home(home: &Path) -> Self {
        Self {
            moves: home.join(".pseudolife-mcp").join("moves"),
        }
    }

    fn path(&self, id: &str) -> Result<PathBuf, String> {
        if !valid_id(id) {
            return Err(format!("{id:?} is not a move ID"));
        }
        Ok(self.moves.join(id).join("record.json"))
    }
}

impl Store for Files {
    fn save(&mut self, record: &Record) -> Result<(), String> {
        let path = self.path(&record.move_id)?;
        let folder = path.parent().expect("a record path has a folder");
        let failed = |what: &str| {
            format!(
                "cannot {what} the move record {}; nothing more was changed",
                path.display()
            )
        };
        crate::board::state::private_dir(folder).map_err(|_| failed("create the folder of"))?;
        let mut bytes = serde_json::to_vec_pretty(record).map_err(|_| failed("encode"))?;
        bytes.push(b'\n');
        crate::board::state::atomic_write(&path, &bytes, None).map_err(|_| failed("write"))
    }

    fn load(&mut self, id: &str) -> Result<Option<Record>, String> {
        let path = self.path(id)?;
        let bytes = match std::fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(format!("cannot read {}: {error}", path.display())),
        };
        let record: Record = serde_json::from_slice(&bytes)
            .map_err(|error| format!("{} is not a move record ({error})", path.display()))?;
        if record.version != RECORD_VERSION || record.move_id != id {
            return Err(format!(
                "{} is a record this client does not read (version {}, move {:?})",
                path.display(),
                record.version,
                record.move_id
            ));
        }
        Ok(Some(record))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_new_id_is_valid_and_names_its_start() {
        let at = chrono::DateTime::parse_from_rfc3339("2026-11-14T07:30:05Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let id = new_id(at);
        assert!(id.starts_with("20261114-073005-"), "{id}");
        assert!(valid_id(&id), "{id}");
    }

    #[test]
    fn an_id_that_could_name_another_path_is_refused() {
        for id in [
            "",
            "20261114-073005-abcdef0",
            "20261114-073005-abcdef012",
            "20261114-073005-ABCDEF01",
            "20261114-073005-../../x",
            "2026111x-073005-abcdef01",
            "20261114_073005-abcdef01",
            "../20261114-073005-abcd",
        ] {
            assert!(!valid_id(id), "{id:?}");
        }
        assert!(valid_id("20261114-073005-abcdef01"));
    }

    #[test]
    fn a_record_round_trips_and_its_flags_default_to_unset() {
        let root =
            std::env::temp_dir().join(format!("move-record-{}", uuid::Uuid::new_v4().simple()));
        let mut store = Files::under_home(&root);
        let mut record = Record {
            version: RECORD_VERSION,
            move_id: "20261114-073005-abcdef01".into(),
            target: "dev@box".into(),
            ..Record::default()
        };
        record.flags.fenced = true;
        store.save(&record).unwrap();
        assert_eq!(store.load(&record.move_id).unwrap(), Some(record.clone()));
        // A record written before a flag existed reads it as unset.
        let path = root.join(".pseudolife-mcp/moves/20261114-073005-abcdef01/record.json");
        std::fs::write(
            &path,
            br#"{"version":1,"move_id":"20261114-073005-abcdef01","flags":{"fenced":true}}"#,
        )
        .unwrap();
        let read = store.load(&record.move_id).unwrap().unwrap();
        assert!(read.flags.fenced && !read.flags.gate_lifting && !read.committed);
        assert_eq!(store.load("20261114-073005-abcdef02").unwrap(), None);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_record_for_another_move_or_version_is_refused() {
        let root =
            std::env::temp_dir().join(format!("move-record-{}", uuid::Uuid::new_v4().simple()));
        let mut store = Files::under_home(&root);
        let id = "20261114-073005-abcdef01";
        let folder = root.join(".pseudolife-mcp/moves").join(id);
        std::fs::create_dir_all(&folder).unwrap();
        for body in [
            r#"{"version":1,"move_id":"20261114-073005-abcdef02"}"#,
            r#"{"version":2,"move_id":"20261114-073005-abcdef01"}"#,
            "not json",
        ] {
            std::fs::write(folder.join("record.json"), body).unwrap();
            assert!(store.load(id).is_err(), "{body}");
        }
        let _ = std::fs::remove_dir_all(&root);
    }
}
