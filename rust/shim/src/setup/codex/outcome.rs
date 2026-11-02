//! Transaction outcomes carry only owned paths and actual compensation events.
use super::*;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum WriteDisposition {
    Untouched,
    Restored,
    Pending,
}
pub(super) struct WriteFailure {
    pub reason: SetupError,
    pub write: AppliedWrite,
    pub disposition: WriteDisposition,
    pub rollback_error: Option<SetupError>,
}

/// A failed connection attempt and the effects observed while compensating it.
/// `recovery_pending` never grants ownership. Only `recovery`, when present,
/// can retry rollback, retaining the same version and file-identity fences.
pub struct SetupFailure {
    pub reason: SetupError,
    pub report: Value,
    pub rollback: Vec<Value>,
    pub recovery_pending: bool,
    pub recovery: Option<Box<AppliedConnection>>,
}
impl From<SetupError> for SetupFailure {
    fn from(reason: SetupError) -> Self {
        Self {
            reason,
            report: json!({"backup":null,"connection_backup":null,"token_backup":null,"created":[]}),
            rollback: vec![],
            recovery_pending: false,
            recovery: None,
        }
    }
}
impl fmt::Display for SetupFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.reason, f)
    }
}
impl fmt::Debug for SetupFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SetupFailure")
            .field("reason", &self.reason)
            .field("recovery_pending", &self.recovery_pending)
            .finish_non_exhaustive()
    }
}
impl std::error::Error for SetupFailure {}

fn report(
    write: Option<&AppliedWrite>,
    token: Option<&PrivateEdit>,
    connection: &PrivateEdit,
) -> Value {
    let created: Vec<_> = token
        .into_iter()
        .chain(std::iter::once(connection))
        .filter(|edit| edit.created())
        .map(|edit| edit.path())
        .collect();
    json!({"backup":write.and_then(|w|w.backup.as_ref()),
        "connection_backup":connection.backup,"token_backup":token.and_then(|t|t.backup.as_ref()),"created":created})
}

fn compensate_file(edit: &mut PrivateEdit, events: &mut Vec<Value>) -> bool {
    rollback_file(edit, events).is_ok()
}

fn rollback_file(edit: &mut PrivateEdit, events: &mut Vec<Value>) -> Result<(), SetupError> {
    if !edit.committed() {
        return Ok(());
    }
    let state = if edit.created() {
        "removed"
    } else {
        "restored"
    };
    match edit.rollback() {
        Ok(()) => {
            events.push(json!({"file":edit.path(),"state":state,"backup":edit.backup}));
            Ok(())
        }
        Err(error) => {
            // A directory sync can fail after the owned removal/restoration.
            // Report the actual operation, retaining its durability diagnostic.
            events.push(json!({"file":edit.path(),"state":if edit.committed() {"left"} else {state},"backup":edit.backup,"detail":error.to_string()}));
            Err(error)
        }
    }
}

impl AppliedConnection {
    /// Compensate only writes still owned by this connection transaction.
    /// On failure the caller retains this typed capability; the returned
    /// metadata never grants ownership or describes inferred file operations.
    pub async fn rollback_detailed(
        &mut self,
        client: &mut impl Rpc,
        cwd: &Path,
    ) -> Result<Vec<Value>, SetupFailure> {
        let mut events = Vec::new();
        let result = async {
            let _lock = files::TransactionLock::acquire(&self.write.plan.user.path)?;
            if self.write.written_version.is_none() && !self.connection.committed()
                && !self.token.as_ref().is_some_and(PrivateEdit::committed) {
                return Ok(());
            }
            if !self.connection.committed() && !self.connection.still_before() {
                let error = SetupError("Codex rollback refused a connection file that appeared or changed during setup; review it before retrying recovery.");
                events.push(json!({"file":self.connection.path(),"state":"left","backup":self.connection.backup,"detail":error.to_string()}));
                return Err(error);
            }
            let config_was_written = self.write.written_version.is_some();
            match self.write.rollback_locked(client, cwd).await {
                Ok(()) if config_was_written => events.push(json!({"file":self.write.plan.user.path,"state":"restored","backup":self.write.backup})),
                Ok(()) => (),
                Err(error) => {
                    events.push(json!({"file":self.write.plan.user.path,"state":"left","backup":self.write.backup,"detail":error.to_string()}));
                    return Err(error);
                }
            }
            rollback_file(&mut self.connection, &mut events)?;
            if let Some(edit) = &mut self.token {
                rollback_file(edit, &mut events)?;
            }
            Ok(())
        }.await;
        match result {
            Ok(()) => Ok(events),
            Err(reason) => Err(SetupFailure {
                reason,
                report: report(Some(&self.write), self.token.as_ref(), &self.connection),
                rollback: events,
                recovery_pending: self.write.written_version.is_some()
                    || self.connection.committed()
                    || self.token.as_ref().is_some_and(PrivateEdit::committed),
                recovery: None,
            }),
        }
    }
}

fn failed(
    reason: SetupError,
    write: AppliedWrite,
    token: Option<PrivateEdit>,
    connection: PrivateEdit,
    rollback: Vec<Value>,
    recovery_pending: bool,
    configuration_owned_or_restored: bool,
) -> SetupFailure {
    let report = report(Some(&write), token.as_ref(), &connection);
    let recoverable = configuration_owned_or_restored
        && (write.written_version.is_some()
            || connection.committed()
            || (connection.still_before() && token.as_ref().is_some_and(PrivateEdit::committed)));
    let recovery = (recovery_pending && recoverable).then(|| {
        Box::new(AppliedConnection {
            write,
            token,
            connection,
            report: report.clone(),
        })
    });
    SetupFailure {
        reason,
        report,
        rollback,
        recovery_pending,
        recovery,
    }
}

/// Apply a planned replacement, retaining recovery metadata on every failure.
/// Backup paths come from this attempt, never from scanning backup filenames.
pub async fn apply_replace_detailed(
    client: &mut impl Rpc,
    cwd: &Path,
    plan: ConnectionPlan,
) -> Result<AppliedConnection, SetupFailure> {
    let target = plan
        .after
        .get(FILE_KEY)
        .and_then(Value::as_str)
        .filter(|p| !p.is_empty())
        .map(str::to_owned);
    let mut token = if let (Some(literal), Some(path)) = (&plan.literal, &target) {
        if std::fs::symlink_metadata(path).is_ok() {
            CredentialProvider::new(None, Some(PathBuf::from(path)))
                .and_then(|p| p.snapshot())
                .map_err(|_| {
                    SetupError(
                        "The Codex token copy is unsafe or malformed; repair it before retrying.",
                    )
                })?;
        }
        Some(PrivateEdit::prepare(
            Path::new(path),
            literal.as_bytes().to_vec(),
        )?)
    } else {
        None
    };
    if token.is_none()
        && let Some(path) = &target
    {
        CredentialProvider::new(None, Some(PathBuf::from(path))).and_then(|p|p.snapshot())
            .map_err(|_| SetupError("The configured credential file is missing, unsafe, or malformed; repair it before retrying."))?;
    }
    let path = connection_path(&plan.home);
    read_connection(&path)?;
    let mut connection = PrivateEdit::prepare(
        &path,
        connection_bytes(&plan.url, target.as_deref().unwrap_or("")),
    )?;
    let _lock = files::TransactionLock::acquire(&plan.write.user.path)?;
    let current = client
        .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
        .await?;
    plan.write.check_no_spawn_override(&current)?;
    if user_config(&current, &plan.home)?.version != plan.write.user.version {
        return Err(SetupError("Codex configuration changed during setup; no connection settings were written. Retry setup.").into());
    }
    if let Some(edit) = &mut token
        && let Err(reason) = edit.commit()
    {
        if edit.committed() {
            // Publication can succeed before its durability check fails. The
            // receipt still owns that inode; cleanup also needs unchanged config.
            let mut write = AppliedWrite::unwritten(plan.write);
            let mut events = Vec::new();
            let safe =
                connection.still_before() && write.rollback_locked(client, cwd).await.is_ok();
            let pending = if safe {
                !compensate_file(edit, &mut events)
            } else {
                events.push(json!({"file":edit.path(),"state":"left","backup":edit.backup,
                    "detail":"Retained while configuration or connection ownership is unconfirmed."}));
                true
            };
            return Err(failed(
                reason, write, token, connection, events, pending, true,
            ));
        }
        let pending = !edit.still_before();
        let event = json!({"file":edit.path(),"state":"left","backup":edit.backup,"detail":reason.to_string()});
        return Err(SetupFailure {
            reason,
            report: report(None, token.as_ref(), &connection),
            rollback: vec![event],
            recovery_pending: pending,
            recovery: None,
        });
    }
    let mut write = match plan.write.apply_locked_detailed(client, cwd).await {
        Ok(write) => write,
        Err(failure) => {
            let WriteFailure {
                reason,
                mut write,
                disposition,
                rollback_error,
            } = *failure;
            let mut events = Vec::new();
            if disposition != WriteDisposition::Untouched {
                events.push(json!({"file":write.plan.user.path,
                    "state":if disposition==WriteDisposition::Restored {"restored"} else {"left"},
                    "backup":write.backup,"detail":rollback_error.map(|error|error.to_string())}));
            }
            let mut pending = disposition == WriteDisposition::Pending;
            if !pending && let Err(error) = write.rollback_locked(client, cwd).await {
                pending = true;
                events.push(json!({"file":write.plan.user.path,"state":"left","backup":write.backup,"detail":error.to_string()}));
            }
            if !pending && connection.still_before() {
                if let Some(edit) = &mut token {
                    pending = !compensate_file(edit, &mut events);
                }
            } else if let Some(edit) = &token {
                pending = true;
                events.push(json!({"file":edit.path(),"state":"left","backup":edit.backup,
                    "detail":"Retained while configuration or connection ownership is unconfirmed."}));
            }
            let configuration_owned_or_restored =
                disposition != WriteDisposition::Pending || write.written_version.is_some();
            return Err(failed(
                reason,
                write,
                token,
                connection,
                events,
                pending,
                configuration_owned_or_restored,
            ));
        }
    };
    if let Err(mut reason) = connection.commit() {
        let mut events = Vec::new();
        let mut pending = false;
        let config_was_written = write.written_version.is_some();
        match write.rollback_locked(client, cwd).await {
            Ok(()) if config_was_written => events.push(
                json!({"file":write.plan.user.path,"state":"restored","backup":write.backup}),
            ),
            Ok(()) => (),
            Err(error) => {
                pending = true;
                events.push(json!({"file":write.plan.user.path,"state":"left","backup":write.backup,"detail":error.to_string()}));
            }
        }
        if connection.committed() {
            if !pending {
                pending = !compensate_file(&mut connection, &mut events);
            } else {
                events.push(
                    json!({"file":connection.path(),"state":"left","backup":connection.backup,
                    "detail":"Retained while configuration rollback is pending."}),
                );
            }
        } else if !connection.still_before() {
            if let Err(error) = read_connection(connection.path()) {
                reason = error;
            }
            pending = true;
            events.push(json!({"file":connection.path(),"state":"left","backup":connection.backup,
                "detail":"A connection file appeared or changed during setup; it was not removed."}));
        }
        if let Some(edit) = &mut token {
            if !pending {
                pending = !compensate_file(edit, &mut events);
            } else {
                events.push(
                    json!({"file":edit.path(),"state":"left","backup":edit.backup,
                "detail":"Retained for the connection that remains on disk."}),
                );
            }
        }
        return Err(failed(
            reason, write, token, connection, events, pending, true,
        ));
    }
    let mut changed: Vec<_> = plan
        .before
        .keys()
        .chain(plan.after.keys())
        .filter(|key| plan.before.get(*key) != plan.after.get(*key))
        .cloned()
        .collect();
    changed.sort();
    changed.dedup();
    let mut report = report(Some(&write), token.as_ref(), &connection);
    report["daemon_url"] = json!(plan.url);
    report["credential_file_path"] = json!(target.unwrap_or_default());
    report["changed"] = json!(changed);
    Ok(AppliedConnection {
        write,
        token,
        connection,
        report,
    })
}
