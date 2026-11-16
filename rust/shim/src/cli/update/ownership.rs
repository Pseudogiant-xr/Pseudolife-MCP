//! The ownership order rule inside `update` (release and checkout modes):
//! probe first; repair the model cache if it needs it; when the state volume
//! needs it too, back it up first (through the running old container as its
//! configured user, or a stopped one through the uid-0 read-only state
//! helper), then repair it; when only a running container that cannot read
//! it exists, repair it, then back up. A healthy host runs only the probe,
//! which is unprivileged. See `cli::ownership` for the helpers themselves.
use std::{
    process::{Command, Stdio},
    time::{Duration, Instant},
};

use serde_json::Value;

use super::{Result, Update, UpdateError, data_dir, stamp};
use crate::cli::ownership::{self, Part, Preflight, Repaired};

impl ownership::Docker for Update {
    fn docker(&mut self, args: &[String]) -> (i32, String) {
        // A walk reads metadata only, but a large model tree on a slow
        // disk is still bounded generously.
        self.command(args, None, 3600, false)
    }
    fn remove(&mut self, name: &str) {
        // `command` starts nothing once an interrupt arrived, and this
        // helper must still go: a bounded direct call.
        let Ok(mut child) = Command::new(&self.docker)
            .args(["rm", "-f", name])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        else {
            return;
        };
        let started = Instant::now();
        while started.elapsed() < Duration::from_secs(120) {
            if !matches!(child.try_wait(), Ok(None)) {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// A log name no other repair of this run or a retry in the same second takes.
fn log_stamp() -> String {
    let token = uuid::Uuid::new_v4().simple().to_string();
    format!("{}-{}", stamp(), &token[..8])
}

impl Update {
    fn ownership_refusal(&self, message: String) -> UpdateError {
        UpdateError::new(format!("{message}; nothing was deployed"))
    }

    fn ownership_note(&mut self, repaired: &Repaired) {
        self.step(repaired.line());
        if repaired.changed > 0 {
            self.ownership_changes.push(repaired.line());
        }
    }

    /// Steps 1 and 2 of the order: probe the running daemon's volumes and
    /// repair its model cache when it needs it. None when the container has
    /// no state volume at /data to check.
    pub(super) fn ownership_preflight(&mut self, running: &Value) -> Result<Option<Preflight>> {
        let directory = data_dir().join("ownership-repair");
        let no_backup = self.o.no_backup;
        let checked = ownership::preflight(self, running, &directory, &log_stamp(), |state| {
            match state.first() {
                Some(first) if no_backup => Err(format!(
                    "the daemon account (uid 10001) cannot use {} in the daemon's state volume, and its ownership repair runs only after a backup of that volume; rerun without --no-backup",
                    first.shown()
                )),
                _ => Ok(()),
            }
        })
        .map_err(|message| self.ownership_refusal(message))?;
        if let Some(models) = checked.as_ref().and_then(|c| c.models.clone()) {
            self.ownership_note(&models);
        }
        Ok(checked)
    }

    /// Steps 3 to 5: `backup` is the update's own pre-deploy backup. On a
    /// usable state volume it runs as it always did. Otherwise the state is
    /// archived before its repair whenever a container can read it, and the
    /// update goes on only with a verified state archive.
    pub(super) fn backup_around_ownership(
        &mut self,
        checked: Option<&Preflight>,
        running: &Value,
        backup: impl FnOnce(&mut Self) -> Result<Value>,
    ) -> Result<Value> {
        let Some(checked) = checked.filter(|c| !c.state_usable()) else {
            return backup(self);
        };
        let first = checked.state[0].shown();
        let directory = data_dir().join("ownership-repair");
        let up = running["State"]["Running"] == true;
        let archived = |backups: &Value| !backups["state_archive"].is_null();
        if !up
            || ownership::runs_as_root(running)
            || (self.o.allow_auth_change && self.o.checkout.is_some())
        {
            // Step 4: the old container (as its configured user, root on such
            // a host), or for a stopped one the uid-0 read-only state helper,
            // can read everything: archive it first.
            self.step(format!(
                "the daemon account cannot use {first} in the state volume: backing it up before repairing its ownership"
            ));
            self.archive_with_helper = !up;
            let backups = backup(self);
            self.archive_with_helper = false;
            let backups = backups?;
            if !archived(&backups) {
                return Err(UpdateError::new(
                    "the state volume could not be archived, so its ownership was not repaired; fix the backup error above and retry; the bank dump is kept; nothing was deployed",
                ));
            }
            let repaired =
                ownership::repair(self, &checked.layout, Part::State, &directory, &log_stamp())
                    .map_err(|message| self.ownership_refusal(message))?;
            self.ownership_note(&repaired);
            Ok(backups)
        } else {
            // Step 5: the running container is already the daemon account
            // and cannot read root's files: repair, then back up as it.
            self.step(format!(
                "the daemon account cannot use {first} in the state volume and the running container cannot archive it: repairing its ownership before the backup"
            ));
            let repaired =
                ownership::repair(self, &checked.layout, Part::State, &directory, &log_stamp())
                    .map_err(|message| self.ownership_refusal(message))?;
            self.ownership_note(&repaired);
            let backups = backup(self)?;
            if !archived(&backups) {
                return Err(UpdateError::new(
                    "the state volume could not be archived after its ownership repair; fix the backup error above and retry; the bank dump is kept; nothing was deployed",
                ));
            }
            Ok(backups)
        }
    }
}
