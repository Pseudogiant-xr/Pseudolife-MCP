//! The ownership order rule for native `backup` and `restore`
//! (`cli::ownership`): probe before anything changes and repair the model
//! cache if it needs it; the state volume is repaired only once a verified
//! archive of it exists (the backup's own pair, restore's safety pair).
//! Both commands archive the state through a uid-0 read-only helper, so a
//! readable archive never needs a repair first.
use std::path::Path;

use serde_json::Value;

use super::{
    bank::{BULK, Bank, Step, Stop},
    docker::Docker,
};
use crate::cli::ownership::{self, Part, Preflight, Repaired};

impl<D: Docker> ownership::Docker for Bank<'_, D> {
    fn docker(&mut self, args: &[String]) -> (i32, String) {
        let ran = self.docker.run(args, BULK);
        let code = if ran.failed {
            1
        } else {
            i32::try_from(ran.code).unwrap_or(1)
        };
        // Docker's own text when the helper printed nothing (its answer is
        // hex, so the lossy decoding never touches a name).
        let out = if ran.stdout.is_empty() {
            &ran.stderr
        } else {
            &ran.stdout
        };
        (code, String::from_utf8_lossy(out).into_owned())
    }
    fn remove(&mut self, name: &str) {
        // Runs to its end even after an interrupt.
        self.quiet(&["rm", "-f", name]);
    }
}

/// The undo lists go beside the other client state.
pub(super) fn log_dir(data_dir: &Path) -> std::path::PathBuf {
    data_dir.join("ownership-repair")
}

fn stop<D: Docker>(bank: &Bank<'_, D>, message: String) -> Stop {
    if bank.docker.interrupted() {
        Stop("interrupted".into(), 130)
    } else {
        Stop(message, 1)
    }
}

/// Probe the daemon's volumes (`container`, its `docker inspect` object as
/// the bank's selection read it) and repair its model cache when it needs
/// it. None when the daemon has no state volume mounted at /data.
pub(super) fn preflight<D: Docker>(
    bank: &mut Bank<'_, D>,
    container: &Value,
    data_dir: &Path,
    stamp: &str,
) -> Step<Option<Preflight>> {
    ownership::preflight(bank, container, &log_dir(data_dir), stamp, |_| Ok(()))
        .map_err(|message| stop(bank, message))
}

/// Repair the state volume after its archive exists, then probe again.
pub(super) fn repair_state<D: Docker>(
    bank: &mut Bank<'_, D>,
    checked: &Preflight,
    data_dir: &Path,
    stamp: &str,
) -> Step<Repaired> {
    ownership::repair(
        bank,
        &checked.layout,
        Part::State,
        &log_dir(data_dir),
        stamp,
    )
    .map_err(|message| stop(bank, message))
}

/// After restore replaced the state: probe it again (the archive may hold
/// entries another owner wrote) and repair it when needed. None when it
/// was usable as restored.
pub(super) fn after_restore<D: Docker>(
    bank: &mut Bank<'_, D>,
    container: &Value,
    data_dir: &Path,
    stamp: &str,
) -> Step<Option<Repaired>> {
    let Some(checked) = preflight(bank, container, data_dir, stamp)? else {
        return Ok(None);
    };
    if checked.state_usable() {
        return Ok(None);
    }
    repair_state(bank, &checked, data_dir, stamp).map(Some)
}
