use super::*;
use refresh::{Capability, Cause, Image, Outcome, Phase, Request};

const PREFLIGHT: Duration = Duration::from_secs(20);
const RESPONSE: Duration = Duration::from_secs(110);

struct Candidate {
    image: Image,
    pin: File,
}

fn select(vendor_version: &str) -> Result<Candidate, Cause> {
    let refused = Cause::CandidateChanged;
    let layout = crate::runtime::default_layout().map_err(|_| refused)?;
    setup::check_directory(&layout.root).map_err(|_| refused)?;
    #[cfg(unix)]
    if rustix::process::geteuid().is_root() {
        trusted_root_paths([layout.root.as_path()]).map_err(|_| refused)?;
    }
    let (runtime, pin) = crate::runtime::select_pinned_runtime(&layout).map_err(|_| refused)?;
    setup::check_directory(&runtime.path).map_err(|_| refused)?;
    let path = crate::cli::update::seams::command_path(runtime.executable());
    check_executable_path(&path).map_err(|_| refused)?;
    Ok(Candidate {
        image: Image {
            path,
            digest: runtime.marker.binary_sha256,
            version: runtime.version,
            vendor_version: vendor_version.to_owned(),
        },
        pin,
    })
}

fn verify_image(image: &Image) -> Result<(), Cause> {
    check_executable_path(&image.path).map_err(|_| Cause::CandidateChanged)?;
    if !crate::runtime::hash_file(&image.path).is_ok_and(|digest| digest == image.digest) {
        return Err(Cause::CandidateChanged);
    }
    Ok(())
}

struct Preflight {
    candidate: Candidate,
    identity: String,
    key: key::Key,
}

fn saved_key(session: &Session<'_>) -> Result<key::Key, Cause> {
    let name = &session.profile.name;
    let root = session.root;
    let changed = Cause::ProfileChanged;
    setup::check_pending(root, name).map_err(|_| changed)?;
    if setup::validate_previous_key(root, name).map_err(|_| changed)? {
        return Err(changed);
    }
    let path = store::profile_path(root, name).map_err(|_| changed)?;
    if !store::private_read(&path, RECORD_LIMIT).is_ok_and(|bytes| bytes == session.profile_bytes)
        || session
            .profile
            .expires_at
            .as_ref()
            .is_some_and(|(_, expiry)| *expiry <= chrono::Utc::now())
    {
        return Err(changed);
    }
    key::Key::load(root, name).map_err(|_| changed)
}

async fn preflight(
    session: &Session<'_>,
    previous: &Image,
    request: &Request,
) -> Result<Preflight, Cause> {
    let candidate = select(&previous.vendor_version)?;
    if candidate.image.digest != request.candidate || candidate.image.digest == previous.digest {
        return Err(Cause::CandidateChanged);
    }
    // The fixture-only driver uses tiny images for the repeated state-machine
    // tests. The production-bridge fixture takes the same probe as users.
    #[cfg(test)]
    let mock_bridge = std::env::var(super::tests::restart_cases::FIXTURE_ENV)
        .is_ok_and(|mode| mode == "supervisor");
    #[cfg(not(test))]
    let mock_bridge = false;
    if !mock_bridge && !readiness::query(&candidate.image.path).await {
        return Err(Cause::BridgeProtocol);
    }
    verify_image(previous)?;
    let key = saved_key(session)?;
    let profile = session.profile;
    #[cfg(unix)]
    if rustix::process::geteuid().is_root() {
        trusted_root_paths([session.root, Path::new(&profile.token_file)])
            .map_err(|_| Cause::ProfileChanged)?;
    }
    let provider =
        crate::credentials::CredentialProvider::new(None, Some((&profile.token_file).into()))
            .map_err(|_| Cause::DaemonHandshake)?;
    let snapshot = provider.snapshot().map_err(|_| Cause::DaemonHandshake)?;
    let token = snapshot.token().ok_or(Cause::DaemonHandshake)?;
    if key.matches(token.as_bytes()) {
        return Err(Cause::ProfileChanged);
    }
    // While the old child serves, only the saved daemon is contacted. No
    // vendor process or control-plane poll is part of this preflight.
    if !tokio::time::timeout(PREFLIGHT, setup::handshake(&profile.daemon_url, token))
        .await
        .unwrap_or(false)
    {
        return Err(Cause::DaemonHandshake);
    }
    let saved = saved_key(session)?;
    if saved.digest() != key.digest() {
        return Err(Cause::ProfileChanged);
    }
    let sign = |image| {
        identity::sign_image(session.root, profile, &key, image).map_err(|_| Cause::ProfileChanged)
    };
    if sign(previous)? != request.identity {
        return Err(Cause::ProfileChanged);
    }
    let identity = sign(&candidate.image)?;
    // A second install may have changed selection even though this image
    // still exists. Compare the supervisor's current verified selection.
    let selected = select(&previous.vendor_version)?;
    if selected.image != candidate.image {
        return Err(Cause::CandidateChanged);
    }
    provider
        .require_current(&snapshot)
        .map_err(|_| Cause::DaemonHandshake)?;
    Ok(Preflight {
        candidate,
        identity,
        key,
    })
}

fn publish(session: &Session<'_>, outcome: &Outcome) -> Result<(), Fail> {
    refresh::write_outcome(session.root, &session.profile.name, outcome)
}

fn report(
    session: &Session<'_>,
    stage: &mut Stage,
    record: &mut LaunchRecord,
    request: &Request,
    phase: Phase,
    cause: Option<Cause>,
) {
    let name = &session.profile.name;
    let active = (phase != Phase::Failed)
        .then(|| record.launch_identity.clone())
        .flatten();
    if phase == Phase::Failed {
        record.ready = false;
    }
    let outcome = Outcome::new(request, phase, active, cause).bind(record);
    if publish(session, &outcome).is_err() {
        let accepted = phase != Phase::Refused
            || refresh::saved_outcome(session.root, name)
                .is_some_and(|saved| saved.phase == Phase::Pending && saved.same_request(&outcome));
        if accepted {
            record.refresh_outcome = Some(outcome);
            if stage.announce(record).is_ok() {
                return;
            }
        }
        crate::stderrln!(
            "Tunnel refresh outcome could not be saved. Check tunnel status for the current launch."
        );
    } else {
        record.refresh_outcome = None;
    }
}

pub(super) fn flush_outcome(session: &Session<'_>, record: &mut LaunchRecord) {
    if record.refresh_outcome.is_some()
        && refresh::repair_outcome(session.root, &session.profile.name, record).is_ok()
    {
        record.refresh_outcome = None;
    }
}

pub(super) async fn handle(
    session: &Session<'_>,
    stage: &mut Stage,
    record: &mut LaunchRecord,
    signals: &mut process::Signals,
    pin: &mut Option<File>,
    mut running: RunningChild,
) -> Result<Option<RunningChild>, Fail> {
    let current = record.refresh.as_ref().ok_or(CHILD_ERROR)?;
    let claim = refresh::Claim::take(&stage.directory);
    let mut claim = match claim {
        Ok(Some(claim)) => claim,
        Ok(None) => return Ok(Some(running)),
        Err(reason) => {
            crate::stderrln!("Tunnel refresh refused: {reason}.");
            return Ok(Some(running));
        }
    };
    let name = &session.profile.name;
    let request = match claim.read(session.root, name, record) {
        Ok(request) => request,
        Err(reason) => {
            crate::stderrln!("Tunnel refresh refused: {reason}.");
            return Ok(Some(running));
        }
    };
    let prepared = tokio::select! {
        biased;
        _ = async {
            tokio::select! {
                _ = signals.wait() => {},
                _ = async {
                    while !stop_marker(&stage.directory, session.id).unwrap_or(true) {
                        tokio::time::sleep(POLL).await;
                    }
                } => {},
            }
        } => {
            running.finish(session.root, name, stage, stopped(name), false).await?;
            return Ok(None);
        }
        prepared = preflight(session, &current.image, &request) => prepared,
    };
    let prepared = match prepared {
        Ok(prepared) => prepared,
        Err(cause) => {
            report(
                session,
                stage,
                record,
                &request,
                Phase::Refused,
                Some(cause),
            );
            return Ok(Some(running));
        }
    };
    if stop_marker(&stage.directory, session.id).unwrap_or(true) {
        report(
            session,
            stage,
            record,
            &request,
            Phase::Refused,
            Some(Cause::Stopped),
        );
        return Ok(Some(running));
    }
    if !matches!(running.child.exit_code(), Ok(None)) {
        return Ok(Some(running));
    }
    let verification = match identity::verification_fence(session.root, name) {
        Ok(fence) => fence,
        Err(_) => {
            report(
                session,
                stage,
                record,
                &request,
                Phase::Refused,
                Some(Cause::VerificationBusy),
            );
            return Ok(Some(running));
        }
    };
    if !claim.sender_live() {
        crate::stderrln!(
            "Tunnel refresh refused: the refresh request is stale because its sender exited before acceptance."
        );
        return Ok(Some(running));
    }
    // Acceptance is irreversible for the sender. Journal it before the
    // first action that can interrupt the live tunnel.
    if publish(session, &Outcome::new(&request, Phase::Pending, None, None)).is_err() {
        crate::stderrln!(
            "Tunnel refresh refused: its pending outcome could not be saved. The old tunnel was left running."
        );
        return Ok(Some(running));
    }
    let old = current.image.clone();
    let epoch = Uuid::new_v4().simple().to_string();
    record.ready = false;
    record.refresh.as_mut().unwrap().epoch.clone_from(&epoch);
    if let Err(error) = stage.announce(record) {
        record.ready = true;
        let _ = stage.announce(record);
        report(
            session,
            stage,
            record,
            &request,
            Phase::Refused,
            Some(Cause::StateUnavailable),
        );
        if let Fail::Detail(detail) = error {
            crate::stderrln!("{detail}");
        }
        return Ok(Some(running));
    }
    drop(verification);
    if running
        .finish(session.root, name, stage, stopped(name), false)
        .await
        .is_err()
    {
        return unproven(session, stage, record, &request, Cause::OldChildUnproven);
    }
    // Even after the old vendor stops, keep its image pinned until the
    // candidate has proved readiness or the rollback has settled.
    change_image(
        record,
        prepared.candidate.image.clone(),
        prepared.identity.clone(),
        &epoch,
    );
    let attempted = match verify_image(&prepared.candidate.image) {
        Ok(()) => {
            session
                .start(stage, record, &prepared.key, STARTUP, false, signals)
                .await
        }
        Err(_) => Err(CHILD_ERROR),
    };
    match attempted {
        Ok(Some(candidate)) => {
            *pin = Some(prepared.candidate.pin);
            report(session, stage, record, &request, Phase::Refreshed, None);
            return Ok(Some(candidate));
        }
        Ok(None) => {
            report(
                session,
                stage,
                record,
                &request,
                Phase::Failed,
                Some(Cause::Stopped),
            );
            return Ok(None);
        }
        Err(_) if stage.retain => {
            return unproven(
                session,
                stage,
                record,
                &request,
                Cause::CandidateChildUnproven,
            );
        }
        Err(_) => {}
    }
    change_image(record, old.clone(), request.identity.clone(), &epoch);
    let restored = match verify_image(&old) {
        Ok(()) => {
            session
                .start(stage, record, &prepared.key, STARTUP, false, signals)
                .await
        }
        Err(_) => Err(CHILD_ERROR),
    };
    match restored {
        Ok(Some(restored)) => {
            report(
                session,
                stage,
                record,
                &request,
                Phase::RolledBack,
                Some(Cause::CandidateStartup),
            );
            Ok(Some(restored))
        }
        Ok(None) => {
            report(
                session,
                stage,
                record,
                &request,
                Phase::Failed,
                Some(Cause::Stopped),
            );
            Ok(None)
        }
        Err(_) if stage.retain => unproven(
            session,
            stage,
            record,
            &request,
            Cause::RollbackChildUnproven,
        ),
        Err(_) => {
            report(
                session,
                stage,
                record,
                &request,
                Phase::Failed,
                Some(Cause::RollbackStartup),
            );
            Err(Fail::Detail(detail(
                session.root,
                name,
                Phase::Failed,
                Some(Cause::RollbackStartup),
            )))
        }
    }
}

fn change_image(record: &mut LaunchRecord, image: Image, identity: String, epoch: &str) {
    record.refresh = Some(Capability {
        image,
        epoch: epoch.to_owned(),
    });
    record.launch_identity = Some(identity);
    record.ready = false;
    record.target = None;
    record.spawn_started = Some(false);
}

fn unproven(
    session: &Session<'_>,
    stage: &mut Stage,
    record: &mut LaunchRecord,
    request: &Request,
    cause: Cause,
) -> Result<Option<RunningChild>, Fail> {
    report(session, stage, record, request, Phase::Failed, Some(cause));
    Err(Fail::Detail(detail(
        session.root,
        &session.profile.name,
        Phase::Failed,
        Some(cause),
    )))
}

fn detail(root: &Path, name: &str, phase: Phase, cause: Option<Cause>) -> String {
    let run = || super::super::tunnel_command("run", name, root.to_str());
    let status = || super::super::bridge::status_command(name, root.to_str());
    match (phase, cause) {
        (Phase::Pending, _) => format!("tunnel refresh is pending; `{}` shows the outcome", status()),
        (Phase::Refreshed, _) => "the tunnel reconnected on the installed runtime".to_owned(),
        (Phase::RolledBack, _) => "the installed runtime did not become ready. The tunnel reconnected on its previous runtime and saved verification for that identity still applies".to_owned(),
        (Phase::Failed, Some(Cause::OldChildUnproven | Cause::CandidateChildUnproven | Cause::RollbackChildUnproven)) => {
            let child = match cause {
                Some(Cause::OldChildUnproven) => "previous",
                Some(Cause::CandidateChildUnproven) => "replacement",
                _ => "rollback",
            };
            format!("could not confirm the {child} tunnel process exited. Recovery is blocked. Check with `{}`", status())
        }
        (Phase::Failed, Some(Cause::Stopped)) => "a stop was requested during refresh and the tunnel stopped".to_owned(),
        (Phase::Failed, _) => format!("the installed and previous runtimes failed to become ready. The tunnel is stopped. Run `{}` to start it again", run()),
        (Phase::Refused, _) => {
            let reason = match cause {
                Some(Cause::ProfileChanged) => "its saved profile or credential no longer matches the launch",
                Some(Cause::DaemonHandshake) => "the authenticated daemon handshake failed",
                Some(Cause::CandidateChanged) => "the installed runtime could not be verified or changed during preflight",
                Some(Cause::BridgeProtocol) => "the installed runtime did not confirm readiness protocol v1",
                Some(Cause::Stopped) => "a stop was requested",
                Some(Cause::VerificationBusy) => "verification holds the profile",
                _ => "the launch state could not be saved",
            };
            if cause == Some(Cause::BridgeProtocol) {
                format!("refresh was refused because {reason}. The old tunnel is still running. Install a runtime that supports readiness protocol v1 and retry update")
            } else {
                format!("refresh was refused because {reason}. This refresh left the running child alone. Check with `{}`", status())
            }
        }
    }
}

pub(in crate::cli::tunnel) struct Report {
    pub(in crate::cli::tunnel) state: &'static str,
    pub(in crate::cli::tunnel) running: &'static str,
    pub(in crate::cli::tunnel) detail: String,
}

pub(in crate::cli::tunnel) fn predates_refresh(root: &Path, name: &str) -> bool {
    read_record(&record::path(root, name)).is_ok_and(|record| record.refresh.is_none())
}

pub(in crate::cli::tunnel) fn attempt(
    root: &Path,
    name: &str,
    executable: &Path,
) -> Option<Report> {
    let record = read_record(&record::path(root, name)).ok()?;
    let capability = record.refresh.as_ref()?;
    let refuse = |reason| Report {
        state: "stale",
        running: "true",
        detail: format!(
            "{reason}. No restart was requested. Check with `{}`",
            super::super::bridge::status_command(name, root.to_str())
        ),
    };
    let candidate = match select(&capability.image.vendor_version) {
        Ok(candidate)
            if candidate.image.digest != capability.image.digest
                && crate::runtime::same_path(&candidate.image.path, executable) =>
        {
            candidate
        }
        // A checkout build should report the mismatch without requesting a
        // restart on a different installed runtime.
        _ => return None,
    };
    let ticket = match refresh::submit(root, name, &record, &candidate.image) {
        Ok(ticket) => ticket,
        Err(reason) => return Some(refuse(reason)),
    };
    let limit = std::time::Instant::now() + RESPONSE;
    loop {
        if let Some(outcome) =
            refresh::read_outcome(root, name).filter(|outcome| outcome.for_ticket(&ticket))
            && outcome.phase != Phase::Pending
        {
            let live = read_record(&record::path(root, name)).ok();
            let verified = live.as_ref().is_some_and(|record| {
                outcome.for_record(record)
                    && launch_id(&record.native_launch).is_some_and(|id| {
                        identity::owns_generation(root, name, &stage_path(root, id))
                    })
            });
            let identity_matches = !matches!(outcome.phase, Phase::Refreshed | Phase::RolledBack)
                || identity::current(root, name).ok() == outcome.active_identity;
            if outcome.phase != Phase::Failed && !(verified && identity_matches)
                || outcome.phase == Phase::Failed
                    && live
                        .as_ref()
                        .is_some_and(|record| !outcome.for_record(record))
            {
                return Some(Report {
                    state: "stale",
                    running: "\"unknown\"",
                    detail: format!(
                        "the refresh result could not be matched to the current launch. Check with `{}`",
                        super::super::bridge::status_command(name, root.to_str())
                    ),
                });
            }
            let (state, running) = match outcome.phase {
                Phase::Refreshed => ("refreshed", "true"),
                Phase::RolledBack | Phase::Refused => ("stale", "true"),
                Phase::Failed
                    if matches!(
                        outcome.cause,
                        Some(
                            Cause::OldChildUnproven
                                | Cause::CandidateChildUnproven
                                | Cause::RollbackChildUnproven
                        )
                    ) =>
                {
                    ("failed", "\"unknown\"")
                }
                Phase::Failed => ("failed", "false"),
                Phase::Pending => unreachable!(),
            };
            return Some(Report {
                state,
                running,
                detail: detail(root, name, outcome.phase, outcome.cause),
            });
        }
        if std::time::Instant::now() >= limit {
            return Some(Report {
                state: "stale",
                running: "\"unknown\"",
                detail: format!(
                    "the supervisor did not report a final refresh outcome in time. Check with `{}`",
                    super::super::bridge::status_command(name, root.to_str())
                ),
            });
        }
        std::thread::sleep(POLL);
    }
}

pub(in crate::cli::tunnel) fn status(root: &Path, name: &str) -> Option<(&'static str, String)> {
    let outcome = refresh::read_outcome(root, name)?;
    let record = read_record(&record::path(root, name)).ok();
    if outcome.phase == Phase::Pending {
        let live = record.as_ref().is_some_and(|record| {
            outcome.for_record(record)
                && launch_id(&record.native_launch)
                    .is_some_and(|id| identity::owns_generation(root, name, &stage_path(root, id)))
        });
        if !live {
            return Some(("failed", "the supervisor exited before reporting the refresh outcome. Confirm the old runtime is stopped before restarting".to_owned()));
        }
        if let Some(id) = record
            .as_ref()
            .and_then(|record| launch_id(&record.native_launch))
            && !store::occupied(&stage_path(root, id).join(refresh::CLAIM)).unwrap_or(true)
        {
            return Some((
                "failed",
                "no durable refresh result was saved. Check the current launch before retrying"
                    .to_owned(),
            ));
        }
    } else if let Some(record) = &record {
        if !outcome.for_generation(record) {
            return None;
        }
        if !outcome.for_record(record) {
            return Some((
                "failed",
                "the saved refresh result does not match the current launch".to_owned(),
            ));
        }
    }
    Some((
        outcome.state(),
        detail(root, name, outcome.phase, outcome.cause),
    ))
}

pub(in crate::cli::tunnel) fn verify(root: &Path, name: &str) -> Output {
    let record = read_record(&record::path(root, name)).ok();
    let outcome = refresh::read_outcome(root, name).filter(|outcome| {
        record
            .as_ref()
            .is_some_and(|record| outcome.for_record(record))
    });
    match outcome.as_ref().map(|outcome| outcome.phase) {
        Some(Phase::Pending) if status(root, name).is_some_and(|(state, _)| state == "pending") => {
            Err(Fail::Detail(detail(root, name, Phase::Pending, None)))
        }
        Some(Phase::RolledBack) => super::super::bridge::continue_verification(root, name)
            .map_or_else(|| super::super::bridge::begin(root, name), Ok),
        Some(Phase::Refreshed) => {
            if let Some(current) = super::super::bridge::continue_verification(root, name) {
                return Ok(current);
            }
            let (challenge, code) = super::super::bridge::begin(root, name)?;
            Ok((
                format!(
                    "The refreshed launch uses a new identity and needs a new challenge.\n{challenge}"
                ),
                code,
            ))
        }
        _ => super::super::bridge::begin(root, name),
    }
}
