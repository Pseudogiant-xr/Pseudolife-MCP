//! The unattended-update schedule rows. A schedule `update --schedule`
//! installed here, the daemon's or this client machine's, names its daemon
//! URL in its command, so moving this machine leaves it stale. `connect`
//! re-creates one it finds through `--schedule` itself, keeping its kind and
//! daily time, when it is exactly what `--schedule` writes and the kind is
//! the one `--schedule` writes here; anything else is a manual row naming
//! the command. It never creates a schedule and never removes one.
use super::discover::{self, Row};
use super::{Ctx, Defer, url};
use std::cell::RefCell;
use std::rc::Rc;

/// One installed schedule (`crate::cli::update::schedules::installed`).
pub(super) struct Found {
    pub client: bool,
    pub place: String,
    pub time: Option<String>,
    pub url: Option<String>,
    pub shaped: bool,
    pub why: Option<String>,
    /// Why connect cannot vouch for it beyond the base files (a drop-in, a
    /// unit file systemd loads from elsewhere, an unreadable unit), with the
    /// step that resolves it.
    pub overridden: Option<crate::cli::update::schedules::Blocker>,
    /// A task setting `--schedule` never writes, named.
    pub differs: Option<String>,
    /// The task's command is the `-EncodedCommand` layout wrapper.
    pub encoded: bool,
    /// What differs from what `--schedule` writes from this shell, when it
    /// can be shown.
    pub mismatch: Option<String>,
    pub remove: String,
}

/// What a schedule change row re-creates.
#[derive(Clone, Debug, PartialEq)]
pub(super) struct Plan {
    pub client: bool,
    pub time: String,
    /// The daemon URL its command named when it was read, as written.
    pub from: String,
}

/// Why a re-creation stopped.
pub(super) enum Stop {
    /// The re-read immediately before the write refused: nothing written.
    Changed(String),
    /// `--schedule` refused or failed.
    Failed(String),
}

/// Why `--schedule` here would not re-create a schedule.
pub(super) struct Refusal {
    /// It would write the other kind of schedule instead.
    pub other_kind: bool,
    pub reason: String,
}

pub(super) trait Scheduler {
    /// The daemon and client schedules installed on this machine.
    fn found(&self) -> Vec<Found>;
    /// The schedule of this kind, read again: `None` when it is not
    /// installed, and why when it could not be read.
    fn reread(&self, client: bool) -> Result<Option<Found>, String>;
    /// Why `--schedule` here would not re-create this kind for `url`.
    fn preflight(&self, client: bool, url: &str) -> Result<(), Refusal>;
    /// Re-create this kind daily at `when` against `url`, running `check`
    /// immediately before the write; why it stopped, if it did.
    fn recreate(
        &self,
        client: bool,
        when: &str,
        url: &str,
        check: &dyn Fn() -> Result<(), String>,
    ) -> Result<(), Stop>;
    /// A scheduler command this run started was stopped by Ctrl-C or
    /// another cancelling signal: the run stops too.
    fn cancelled(&self) -> bool;
}

/// The native `update --schedule` code.
struct Native;

fn found(installed: crate::cli::update::schedules::Installed) -> Found {
    Found {
        client: installed.client,
        place: installed.place,
        time: installed.time,
        url: installed.url,
        shaped: installed.shaped,
        why: installed.why,
        overridden: installed.overridden,
        differs: installed.differs,
        encoded: installed.encoded,
        mismatch: installed.mismatch,
        remove: installed.remove,
    }
}

impl Scheduler for Native {
    fn found(&self) -> Vec<Found> {
        use crate::cli::update::schedules;
        schedules::installed(&schedules::NAMES)
            .into_iter()
            .map(found)
            .collect()
    }

    fn reread(&self, client: bool) -> Result<Option<Found>, String> {
        use crate::cli::update::schedules;
        let read = schedules::installed_kind(&schedules::NAMES, client);
        if schedules::cancelled() {
            return Err("interrupted".to_owned());
        }
        read.map(|installed| installed.map(found))
    }

    fn preflight(&self, client: bool, url: &str) -> Result<(), Refusal> {
        crate::cli::update::schedules::preflight(client, url).map_err(|refusal| Refusal {
            other_kind: refusal.other_kind,
            reason: refusal.reason,
        })
    }

    fn recreate(
        &self,
        client: bool,
        when: &str,
        url: &str,
        check: &dyn Fn() -> Result<(), String>,
    ) -> Result<(), Stop> {
        use crate::cli::update::schedules;
        schedules::reschedule(&schedules::NAMES, client, when, url, check)
            .map(|_| ())
            .map_err(|stop| match stop {
                schedules::Stop::Changed(cause) => Stop::Changed(cause),
                schedules::Stop::Failed(cause) => Stop::Failed(cause),
            })
    }

    fn cancelled(&self) -> bool {
        crate::cli::update::schedules::cancelled()
    }
}

thread_local! {
    static OVERRIDE: RefCell<Option<Rc<dyn Scheduler>>> = const { RefCell::new(None) };
}

/// The scheduler in use: the native one, or a test's fake.
pub(super) fn backend() -> Rc<dyn Scheduler> {
    OVERRIDE
        .with(|slot| slot.borrow().clone())
        .unwrap_or_else(|| Rc::new(Native))
}

#[cfg(test)]
pub(super) fn with_backend<T>(backend: Rc<dyn Scheduler>, run: impl FnOnce() -> T) -> T {
    OVERRIDE.with(|slot| *slot.borrow_mut() = Some(backend));
    let result = run();
    OVERRIDE.with(|slot| *slot.borrow_mut() = None);
    result
}

/// The command that schedules this machine's run against `url` at `when`.
pub(super) fn command(when: &str, url: &str) -> String {
    format!("`pseudolife-mcp update --schedule {when} --daemon-url {url}`")
}

/// The schedule rows (only when every client is selected, as the oracle's).
pub(super) fn rows(ctx: &Ctx) -> Result<Vec<Row>, Defer> {
    let backend = backend();
    let mut rows = Vec::new();
    for found in backend.found() {
        let place = if found.client {
            "client-schedule"
        } else {
            "schedule"
        };
        if let Some(blocker) = &found.overridden {
            // What systemd reads beyond the base files, not the base unit
            // `--schedule` writes, decides what runs, so neither "current"
            // nor `--schedule` is true. The daemon's own schedule after the
            // daemon moved away is the exception: its next step is
            // `--unschedule`, so the row names that, and the cause found
            // without the fix (which would promise a re-creation connect will
            // not make there); some causes (a directory at the unit path, an
            // unreachable user manager) can also make `--unschedule` fail.
            let detail = if !found.client && ctx.remote {
                format!(
                    "{}: the daemon now runs on another machine, and an unattended update only updates a daemon on this one, so connect does not re-run the schedule step: remove it here with `pseudolife-mcp update --unschedule`, and schedule it on the daemon's host. Also found here: {}; if `--unschedule` reports an error, deal with that first",
                    found.place, blocker.cause
                )
            } else {
                format!(
                    "{}: {}",
                    found.place,
                    blocker
                        .sentence()
                        .replace(crate::cli::update::schedules::TARGET, &ctx.url)
                )
            };
            rows.push(discover::manual(
                "unattended-update",
                place,
                None,
                None,
                detail,
            )?);
            continue;
        }
        // A URL the plan cannot hold (hand-edited) is a manual row, never a
        // deferral of the whole run.
        let target = named(found.url.as_deref());
        // "current" needs the schedule to be exactly what `--schedule`
        // writes: one that names the new URL first can still differ (a second
        // `--daemon-url`, which the run takes, an `ExecStartPost=`, a second
        // task action, or an environment written from another shell). The
        // daemon's own schedule after the daemon moved away is never current:
        // its next step is `--unschedule`.
        let current = target.as_deref() == Some(ctx.url.as_str());
        let moved_away = !found.client && ctx.remote;
        if current && found.shaped && !moved_away {
            let mut row = discover::row("unattended-update", place, None, None, "current")?;
            row.detail = found.place;
            rows.push(row);
            continue;
        }
        let before = match &found.url {
            Some(value) => {
                url::shown(value).unwrap_or_else(|_| "a daemon URL connect cannot show".to_owned())
            }
            None => "a daemon URL connect cannot read".to_owned(),
        };
        let when = found.time.clone().unwrap_or_else(|| "HH:MM".to_owned());
        let fix = command(&when, &ctx.url);
        let manual = |detail: String| {
            discover::manual(
                "unattended-update",
                place,
                None,
                None,
                format!("{} runs against {before}; {detail}", found.place),
            )
        };
        let leave = |why: &str| manual(format!("{why}: {fix}"));
        let row = if moved_away {
            manual(
                "connect does not re-run the schedule step: the daemon now runs on another machine, and an unattended update only updates a daemon on this one: remove it here with `pseudolife-mcp update --unschedule`, and schedule it on the daemon's host".to_owned(),
            )?
        } else if let Some(why) = &found.why {
            leave(&format!(
                "connect cannot tell whether it is the one `update --schedule` writes ({why}), so it leaves it as it is; re-schedule it"
            ))?
        } else if current && found.differs.is_some() && found.mismatch.is_none() {
            // Only the named setting differs: the rest is what `--schedule`
            // writes, so nothing needs changing for the move.
            discover::manual(
                "unattended-update",
                place,
                None,
                None,
                format!(
                    "{} names {} and differs from what `update --schedule` writes only in {}, so it needs no change for this move; connect leaves it as it is, and re-scheduling it would drop that: {fix}",
                    found.place,
                    ctx.url,
                    found.differs.as_deref().unwrap_or_default()
                ),
            )?
        } else if current && !found.shaped {
            // It names the new daemon first, but is not exactly what
            // `--schedule` writes from this shell: a hand edit, or a schedule
            // written from a shell whose environment differs from this one.
            let shown = found
                .mismatch
                .as_deref()
                .map(|differs| format!(" ({differs})"))
                .unwrap_or_default();
            discover::manual(
                "unattended-update",
                place,
                None,
                None,
                format!(
                    "{} names {} first, but is not exactly the schedule `update --schedule` writes from this shell{shown}, so connect cannot vouch that it runs only against that daemon and leaves it as it is; re-creating it with `update --schedule` from this shell makes it one connect manages: {fix}",
                    found.place, ctx.url
                ),
            )?
        } else if let Some(differs) = &found.differs {
            // A task registered through the `-EncodedCommand` layout wrapper
            // shows its daemon URL only as base64, so it cannot be edited
            // in Task Scheduler's argument field.
            let keep = if found.encoded {
                "to keep it, re-register the task by hand with the new daemon URL (its URL is inside the task's `-EncodedCommand` script, which Task Scheduler shows only as base64)".to_owned()
            } else {
                format!(
                    "to keep it, change the task's `--daemon-url` argument to {} in Task Scheduler",
                    ctx.url
                )
            };
            manual(format!(
                "it has {differs}, which `update --schedule` does not write, so re-scheduling it would drop that, and connect leaves it as it is: {keep}; to drop it, re-schedule it: {fix}"
            ))?
        } else if found.url.is_none() {
            leave(
                "its command names no `--daemon-url` connect can read (an older `update --schedule` wrote it that way), so connect leaves it as it is; re-schedule it",
            )?
        } else if !found.shaped {
            leave(&format!(
                "it is not exactly what `update --schedule` writes from this shell ({}), so connect leaves it as it is; re-schedule it",
                found
                    .mismatch
                    .as_deref()
                    .unwrap_or("its command, environment, trigger or state differ")
            ))?
        } else if found.time.is_none() {
            leave(
                "its daily time could not be read, so connect cannot keep it; re-schedule it at your time",
            )?
        } else if let Err(refusal) = backend.preflight(found.client, &ctx.url) {
            if refusal.other_kind {
                // `--schedule` would add the other kind beside it: name the
                // command that removes exactly this one first.
                manual(format!(
                    "connect cannot re-create it here ({}): remove this one with `{}`, then, if this machine should update itself, schedule that with {fix}",
                    refusal.reason, found.remove
                ))?
            } else {
                leave(&format!(
                    "connect cannot re-create it here ({}); fix that, then re-schedule it",
                    refusal.reason
                ))?
            }
        } else {
            let time = found.time.clone().unwrap_or_default();
            let mut row = discover::row("unattended-update", place, None, None, "change")?;
            row.detail = found.place;
            row.changes = vec![(
                "--daemon-url".to_owned(),
                Some(before.clone()),
                Some(ctx.url.clone()),
            )];
            row.notes.push(format!(
                "re-created through `pseudolife-mcp update --schedule {time}`, keeping its daily time"
            ));
            row.schedule = Some(Plan {
                client: found.client,
                time,
                from: found.url.clone().unwrap_or_default(),
            });
            row
        };
        rows.push(row);
    }
    Ok(rows)
}

/// Re-create every schedule a change row names, after the registrations
/// (`moved`: some client file was written) moved: a note for each one
/// re-created, and for each refusal the line that says what changed and
/// the one command that finishes it.
pub(super) fn apply(
    rows: &[Row],
    url: &str,
    moved: bool,
    backend: &dyn Scheduler,
) -> (Vec<String>, Vec<String>) {
    let mut notes = Vec::new();
    let mut failures = Vec::new();
    for row in rows.iter().filter(|row| row.state == "change") {
        let Some(plan) = &row.schedule else {
            continue;
        };
        let prefix = if moved {
            format!("the client registrations now name {url} (backups above), but ")
        } else {
            String::new()
        };
        // Read again immediately before the write: a schedule removed,
        // disabled or edited since the plan was shown is left as it now is,
        // never revived or overwritten.
        let check = || unchanged(plan, backend.reread(plan.client));
        match backend.recreate(plan.client, &plan.time, url, &check) {
            // Read back what was written before saying it runs against the
            // new daemon: the write's own `daemon-reload` can load a unit file
            // from a higher-priority directory connect never read.
            Ok(()) => match written(plan, url, backend.reread(plan.client)) {
                Ok(()) => notes.push(format!(
                    "{} now runs against {url}, daily at {}, as `update --schedule` writes it",
                    row.detail, plan.time
                )),
                Err(cause) => failures.push(format!(
                    "{prefix}{} was re-created for {url}, but {}",
                    row.detail,
                    cause.replace(crate::cli::update::schedules::TARGET, url)
                )),
            },
            Err(Stop::Changed(cause)) => failures.push(format!(
                "{prefix}{} was not re-created: {}, so connect leaves it as it is; re-run connect to plan it again",
                row.detail,
                cause.replace(crate::cli::update::schedules::TARGET, url)
            )),
            Err(Stop::Failed(cause)) => failures.push(format!(
                "{prefix}{} was not re-created ({cause}); finish it with {}",
                row.detail,
                command(&plan.time, url)
            )),
        }
    }
    (notes, failures)
}

/// The daemon URL a schedule names, as the plan compares it.
fn named(value: Option<&str>) -> Option<String> {
    value.and_then(|value| match url::validated(value) {
        Ok(Ok(valid)) => Some(valid),
        Ok(Err(())) => Some(value.to_owned()),
        Err(Defer) => None,
    })
}

/// Whether the schedule read back after the write (`read`) is what was
/// written for `url` at the planned time, with nothing systemd loads beyond
/// it; why not, as the rest of the failure line ("… was re-created for
/// URL, but …").
pub(super) fn written(
    plan: &Plan,
    url: &str,
    read: Result<Option<Found>, String>,
) -> Result<(), String> {
    match read {
        Err(cause) => Err(format!(
            "connect could not read it back ({cause}), so it cannot confirm the run uses the new daemon; re-run connect to check it"
        )),
        Ok(None) => Err(
            "it is not installed when connect reads it back, so it cannot confirm the run uses the new daemon; re-run connect to check it".to_owned(),
        ),
        Ok(Some(found)) => match &found.overridden {
            Some(blocker) => Err(format!(
                "when connect reads it back, {}, so connect cannot confirm the run uses the new daemon: {}",
                blocker.cause, blocker.fix
            )),
            None if found.shaped
                && found.time.as_deref() == Some(plan.time.as_str())
                && named(found.url.as_deref()).as_deref() == Some(url) =>
            {
                Ok(())
            }
            None => Err(
                "it does not read back as what `update --schedule` writes for this daemon at the planned time, so connect cannot confirm the run uses the new daemon; re-run connect to check it".to_owned(),
            ),
        },
    }
}

/// Whether the schedule read again (`read`) is still the one `plan` was
/// made from; why not, as the cause a failure line states.
pub(super) fn unchanged(plan: &Plan, read: Result<Option<Found>, String>) -> Result<(), String> {
    match read {
        Err(cause) => Err(format!(
            "connect could not read it again immediately before writing ({cause})"
        )),
        Ok(None) => Err("it was removed after connect read it".to_owned()),
        // Something beyond the base files is new since the plan (a drop-in,
        // a reload pending, an unreachable user manager): that is the cause.
        Ok(Some(Found {
            overridden: Some(blocker),
            ..
        })) => Err(format!(
            "{} (found when connect read it again immediately before writing)",
            blocker.cause
        )),
        Ok(Some(found))
            if found.client == plan.client
                && found.shaped
                && found.why.is_none()
                && found.overridden.is_none()
                && found.differs.is_none()
                && found.time.as_deref() == Some(plan.time.as_str())
                && found.url.as_deref() == Some(plan.from.as_str()) =>
        {
            Ok(())
        }
        Ok(Some(_)) => Err(
            "it changed after connect read it (its daemon URL, time, state or shape)".to_owned(),
        ),
    }
}
