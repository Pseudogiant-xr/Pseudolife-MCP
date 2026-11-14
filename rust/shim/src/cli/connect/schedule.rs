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
    pub remove: String,
}

/// What a schedule change row re-creates.
#[derive(Clone, Debug, PartialEq)]
pub(super) struct Plan {
    pub client: bool,
    pub time: String,
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
    /// Why `--schedule` here would not re-create this kind for `url`.
    fn preflight(&self, client: bool, url: &str) -> Result<(), Refusal>;
    /// Re-create this kind daily at `when` against `url`; why not, if it
    /// refused.
    fn recreate(&self, client: bool, when: &str, url: &str) -> Result<(), String>;
    /// A scheduler command this run started was stopped by Ctrl-C or
    /// another cancelling signal: the run stops too.
    fn cancelled(&self) -> bool;
}

/// The native `update --schedule` code.
struct Native;

impl Scheduler for Native {
    fn found(&self) -> Vec<Found> {
        use crate::cli::update::schedules;
        schedules::installed(&schedules::NAMES)
            .into_iter()
            .map(|installed| Found {
                client: installed.client,
                place: installed.place,
                time: installed.time,
                url: installed.url,
                shaped: installed.shaped,
                why: installed.why,
                remove: installed.remove,
            })
            .collect()
    }

    fn preflight(&self, client: bool, url: &str) -> Result<(), Refusal> {
        crate::cli::update::schedules::preflight(client, url).map_err(|refusal| Refusal {
            other_kind: refusal.other_kind,
            reason: refusal.reason,
        })
    }

    fn recreate(&self, client: bool, when: &str, url: &str) -> Result<(), String> {
        use crate::cli::update::schedules;
        schedules::reschedule(&schedules::NAMES, client, when, url).map(|_| ())
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
        // A URL the plan cannot hold (hand-edited) is a manual row, never a
        // deferral of the whole run.
        let target = found
            .url
            .as_deref()
            .and_then(|value| match url::validated(value) {
                Ok(Ok(valid)) => Some(valid),
                Ok(Err(())) => Some(value.to_owned()),
                Err(Defer) => None,
            });
        if target.as_deref() == Some(ctx.url.as_str()) {
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
        let row = if !found.client && ctx.remote {
            manual(
                "connect does not re-run the schedule step: the daemon now runs on another machine, and an unattended update only updates a daemon on this one: remove it here with `pseudolife-mcp update --unschedule`, and schedule it on the daemon's host".to_owned(),
            )?
        } else if let Some(why) = &found.why {
            leave(&format!(
                "connect cannot tell whether it is the one `update --schedule` writes ({why}), so it leaves it as it is; re-schedule it"
            ))?
        } else if found.url.is_none() {
            leave(
                "its command names no `--daemon-url` connect can read (an older `update --schedule` wrote it that way), so connect leaves it as it is; re-schedule it",
            )?
        } else if !found.shaped {
            leave(
                "it is not exactly what `update --schedule` writes (its command, environment, trigger or state differ), so connect leaves it as it is; re-schedule it",
            )?
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
        match backend.recreate(plan.client, &plan.time, url) {
            Ok(()) => notes.push(format!(
                "{} now runs against {url}, daily at {}, as `update --schedule` writes it",
                row.detail, plan.time
            )),
            Err(cause) => failures.push(format!(
                "{}{} was not re-created ({cause}); finish it with {}",
                if moved {
                    format!("the client registrations now name {url} (backups above), but ")
                } else {
                    String::new()
                },
                row.detail,
                command(&plan.time, url)
            )),
        }
    }
    (notes, failures)
}
