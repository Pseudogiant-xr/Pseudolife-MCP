//! Move protocol 2: the operations a move asks of the target's helper.
//!
//! The source runs `pseudolife-mcp move-target 2` over ssh with one JSON
//! [`Request`] line on stdin, followed, for [`Op::Receive`] only, by the
//! file's bytes. The helper answers one line, the frame then a JSON
//! [`Answer`]. Every operation is safe to repeat: after a lost link the
//! source repeats it, or asks [`Op::State`] what is true now. Each operation
//! that changes the target is journalled in the move's folder there before
//! and after it runs, and is refused unless the target's claim names this
//! move.
use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::target::Ask;

/// The protocol this client's source speaks for a move's transaction.
pub(in crate::cli) const PROTOCOL: u32 = 2;

/// One operation on the target.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub(in crate::cli) enum Op {
    /// The preflight's checks (protocol 1's request), unchanged.
    Check {
        ask: Ask,
    },
    /// Claim the target for this move: refused while another move holds it.
    Claim {
        source_bank: String,
        /// A resumed move: lifts the abort its earlier rollback left, which
        /// a late step of the original run never does.
        #[serde(default)]
        resume: bool,
    },
    /// What is true on the target now, with the row count of each table
    /// named (to compare with the dump's).
    State {
        tables: Vec<String>,
    },
    TimerPause,
    TimerResume,
    /// Set the daemon's restart policy to `no`, answering the old one.
    PolicyNo,
    PolicySet {
        policy: String,
    },
    /// A file of the move's: `bytes` bytes follow the request line, and
    /// must hash to `sha256`.
    Receive {
        name: String,
        sha256: String,
        bytes: u64,
    },
    /// Stop the daemon and read back that it is stopped.
    Stop,
    /// Start the daemon (the target's own bank, never a gated clone).
    Start,
    /// Write `/data/move.json`, which refuses every daemon start.
    Gate,
    /// Remove the gate from the target's own bank (never the moved one).
    Ungate,
    /// Remove the gate and start the daemon, in one operation.
    StartGated,
    /// Archive the target's own `/data` once, before it is replaced.
    Snapshot,
    /// Restore the received dump and state archive, leaving the daemon
    /// stopped behind its gate.
    Restore,
    /// Give up the claim: done when this move does not hold it (nothing
    /// to give up).
    Release,
    /// The source is rolling this move back: from now on the target refuses
    /// every forward operation of the move. Taken under the target's update
    /// lock, so an operation still running there finishes first.
    Abort,
}

impl Op {
    /// Whether the operation changes the target (and so needs the claim
    /// and the update lock, and is journalled).
    pub(in crate::cli) fn changes(&self) -> bool {
        !matches!(self, Self::Check { .. } | Self::State { .. })
    }

    pub(in crate::cli) fn name(&self) -> &'static str {
        match self {
            Self::Check { .. } => "check",
            Self::Claim { .. } => "claim",
            Self::State { .. } => "state",
            Self::TimerPause => "timer-pause",
            Self::TimerResume => "timer-resume",
            Self::PolicyNo => "policy-no",
            Self::PolicySet { .. } => "policy-set",
            Self::Receive { .. } => "receive",
            Self::Stop => "stop",
            Self::Start => "start",
            Self::Gate => "gate",
            Self::Ungate => "ungate",
            Self::StartGated => "start-gated",
            Self::Snapshot => "snapshot",
            Self::Restore => "restore",
            Self::Release => "release",
            Self::Abort => "abort",
        }
    }
}

/// The request line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(in crate::cli) struct Request {
    pub protocol: u32,
    pub move_id: String,
    /// The move's folder on the target (`~/...` or absolute; `""` for
    /// `~/.pseudolife-mcp/moves`), as `--target-work-dir` gave it.
    #[serde(default)]
    pub work_dir: String,
    /// Sent by the rollback: allowed after the move's abort.
    #[serde(default)]
    pub rollback: bool,
    #[serde(flatten)]
    pub op: Op,
}

/// What is true on the target, read fresh.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub(in crate::cli) struct State {
    /// Whether the daemon container runs (`None`: it could not be read).
    pub running: Option<bool>,
    /// Whether `/data/move.json` is present (`None`: it could not be read).
    pub gated: Option<bool>,
    pub restart_policy: String,
    /// The fingerprint of `meta.coordination_bank_id`, when the bank has one.
    pub bank: Option<String>,
    /// Whether the bank holds no user data (`None`: it could not be read).
    pub empty: Option<bool>,
    /// The row count of each table asked for (missing: it could not be
    /// counted).
    pub counts: BTreeMap<String, i64>,
    /// The same tables' rows that hold user data ([`super::probe::user_filter`]):
    /// what a served target's writes change, and a plain daemon start does
    /// not.
    pub user_counts: BTreeMap<String, i64>,
    /// Each user table's inserts, updates and deletes since its statistics
    /// began (`pg_stat_user_tables`), but for the two a daemon's own start
    /// writes (`meta`, `retrieval_events`): what an update in place changes
    /// when no row count does. `None`: they could not be read.
    pub mutations: Option<BTreeMap<String, i64>>,
    /// The move that holds the target's claim, if any.
    pub claim: Option<String>,
    /// `systemctl --user is-enabled pseudolife-update.timer`'s answer.
    pub update_timer: String,
    /// The move's folder on the target, and what its files take.
    pub folder: String,
    pub folder_bytes: Option<u64>,
}

/// The helper's answer.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub(in crate::cli) struct Answer {
    pub protocol: u32,
    pub version: String,
    /// Whether the operation did what it says.
    pub ok: bool,
    /// Why not, with the fix.
    pub error: String,
    /// For `state`, and after each operation that changes the daemon.
    pub state: Option<State>,
    /// For `check`: the preflight's reply.
    pub check: Option<super::target::Reply>,
    /// For `policy-no`: the policy it replaced.
    pub policy: String,
    /// For `restore`: whether restore reached its destructive step (its
    /// "Dropping + recreating" line), so a failure before it changed nothing.
    pub restore_dropped: bool,
    /// For `restore`: its last lines, for the report.
    pub detail: String,
    /// Refused only because another operation holds the target's update
    /// lock: worth waiting for.
    pub busy: bool,
}
