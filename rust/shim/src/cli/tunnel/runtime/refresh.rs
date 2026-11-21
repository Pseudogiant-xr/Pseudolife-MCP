//! The updater requests a reconnect from the supervisor that owns the launch.
//! The request carries a digest claim. Only the supervisor selects an image.
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};

use hmac::Mac;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use super::{Fail, LaunchRecord, identity, launch_id, setup, stage_path, state, store};

pub(super) const REQUEST: &str = "refresh.request";
pub(super) const CLAIM: &str = "refresh.claim";
pub(super) const PUBLISHER: &str = "refresh.publisher.lock";
pub(super) const ISSUE: &str = "refresh.issue";
const SENDER_PREFIX: &str = "refresh-sender-";
const DOMAIN: &[u8] = b"pseudolife/tunnel-refresh/request/1\0";
const LIMIT: usize = 4096;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Image {
    pub(super) path: PathBuf,
    pub(super) digest: String,
    pub(super) version: String,
    pub(super) vendor_version: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Capability {
    pub(super) epoch: String,
    pub(super) image: Image,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Request {
    pub(super) id: String,
    pub(super) generation: String,
    epoch: String,
    pub(super) identity: String,
    pub(super) candidate: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(super) enum ChannelIssue {
    NonFile,
    ClaimBlocked,
    ClaimMove,
    Unreadable,
    InvalidRequest,
    ForeignGeneration,
    ResultUnavailable,
}

impl ChannelIssue {
    pub(super) fn message(self) -> &'static str {
        match self {
            Self::NonFile => "the refresh request path is not a plain file",
            Self::ClaimBlocked => {
                "a leftover refresh claim could not be cleared. Stop the tunnel and check its private launch directory"
            }
            Self::ClaimMove => "the refresh request could not be consumed atomically",
            Self::Unreadable => "the refresh request is not an owner-only readable file",
            Self::InvalidRequest => "the refresh request was malformed or failed authentication",
            Self::ForeignGeneration => "the refresh request is for a different launch generation",
            Self::ResultUnavailable => "the supervisor could not save the refresh outcome",
        }
    }
}

pub(super) fn note_issue(directory: &Path, issue: ChannelIssue) -> Result<(), Fail> {
    let path = directory.join(ISSUE);
    let bytes = serde_json::to_vec(&issue).map_err(|_| super::UNSAFE_STORE)?;
    if store::occupied(&path)? {
        setup::replace_private(&path, &bytes)
    } else {
        setup::publish_new(&path, |file| file.write_all(&bytes))
    }
}

fn read_issue(directory: &Path) -> Option<ChannelIssue> {
    let bytes = store::private_read(&directory.join(ISSUE), LIMIT).ok()?;
    serde_json::from_slice(&bytes).ok()
}

pub(super) fn clear_issue(directory: &Path) {
    let path = directory.join(ISSUE);
    if state::open(&path).is_ok() {
        let _ = store::sharing_retry(|| fs::remove_file(&path));
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Signed {
    request: Request,
    tag: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(super) enum Phase {
    Pending,
    Refreshed,
    RolledBack,
    Failed,
    Refused,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(super) enum Cause {
    ProfileChanged,
    DaemonHandshake,
    CandidateChanged,
    VendorUnavailable,
    BridgeProtocol,
    BridgeProtocolTimeout,
    StateUnavailable,
    VerificationBusy,
    CandidateStartup(CandidateFailure),
    CandidateAlreadyRolledBack(CandidateFailure),
    OldChildUnproven,
    CandidateChildUnproven,
    RollbackChildUnproven,
    RollbackStartup,
    Stopped,
    OldChildExited,
    Reply(Refusal),
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(super) enum CandidateFailure {
    Startup,
    ReadinessDeadline,
    VendorDiagnostics,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(super) enum Refusal {
    Replay,
    Stale,
    OtherIdentity,
    OtherGeneration,
    SenderGone,
}

impl Refusal {
    pub(super) fn message(self) -> &'static str {
        match self {
            Self::Replay => "the refresh request was already consumed",
            Self::Stale => "the refresh request is stale",
            Self::OtherIdentity => "the refresh request is for a different launch identity",
            Self::OtherGeneration => "the refresh request is for a different launch generation",
            Self::SenderGone => "the refresh request is stale because its sender has exited",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Outcome {
    id: String,
    generation: String,
    request_epoch: String,
    candidate: String,
    pub(super) phase: Phase,
    previous_identity: String,
    pub(super) active_identity: Option<String>,
    active_epoch: Option<String>,
    pub(super) cause: Option<Cause>,
}

impl Outcome {
    pub(super) fn new(
        request: &Request,
        phase: Phase,
        active_identity: Option<String>,
        cause: Option<Cause>,
    ) -> Self {
        Self {
            id: request.id.clone(),
            generation: request.generation.clone(),
            request_epoch: request.epoch.clone(),
            candidate: request.candidate.clone(),
            phase,
            previous_identity: request.identity.clone(),
            active_identity,
            active_epoch: None,
            cause,
        }
    }

    pub(super) fn bind(mut self, record: &LaunchRecord) -> Self {
        self.active_epoch = record.refresh.as_ref().map(|refresh| refresh.epoch.clone());
        self
    }

    pub(super) fn for_generation(&self, record: &LaunchRecord) -> bool {
        self.generation == record.native_launch
    }

    pub(super) fn for_record(&self, record: &LaunchRecord) -> bool {
        if !self.for_generation(record) {
            return false;
        }
        let Some(capability) = &record.refresh else {
            return false;
        };
        if self.phase == Phase::Pending {
            return true;
        }
        if self.active_epoch.as_deref() != Some(capability.epoch.as_str()) {
            return false;
        }
        match self.phase {
            Phase::Failed => {
                self.active_identity.is_none()
                    && (!record.ready
                        // The old child can stop before the epoch changes. The
                        // result still binds to that exact launch if rewriting
                        // its process record was blocked.
                        || (self.request_epoch == capability.epoch
                            && record.launch_identity.as_deref()
                                == Some(self.previous_identity.as_str())
                            && matches!(
                                self.cause,
                                Some(Cause::Stopped | Cause::OldChildExited | Cause::OldChildUnproven)
                            )))
            }
            Phase::Refreshed => {
                record.ready
                    && self.active_identity.is_some()
                    && self.active_identity == record.launch_identity
                    && capability.image.digest == self.candidate
            }
            Phase::RolledBack => {
                record.ready
                    && self.active_identity == record.launch_identity
                    && self.active_identity.as_deref() == Some(self.previous_identity.as_str())
            }
            Phase::Refused => {
                record.ready
                    && self.active_identity.is_some()
                    && self.active_identity == record.launch_identity
            }
            Phase::Pending => unreachable!(),
        }
    }

    pub(super) fn for_ticket(&self, ticket: &Ticket) -> bool {
        self.generation == ticket.request.generation
            && self.id == ticket.request.id
            && self.previous_identity == ticket.request.identity
            && self.request_epoch == ticket.request.epoch
            && self.candidate == ticket.request.candidate
    }

    pub(super) fn same_request(&self, other: &Self) -> bool {
        self.id == other.id
            && self.generation == other.generation
            && self.request_epoch == other.request_epoch
            && self.candidate == other.candidate
            && self.previous_identity == other.previous_identity
    }

    pub(super) fn state(&self) -> &'static str {
        match self.phase {
            Phase::Pending => "pending",
            Phase::Refreshed => "refreshed",
            Phase::RolledBack => "rolled-back",
            Phase::Failed => "failed",
            Phase::Refused => "refused",
        }
    }
}

fn result_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.native-refresh.json"))
}

/// Called under both launch locks before the new generation starts a child.
pub(super) fn retire_outcome(root: &Path, name: &str) -> Result<(), Fail> {
    let path = result_path(root, name);
    let unavailable = || {
        Fail::Detail(format!(
            "cannot retire the previous tunnel refresh result at {}. Check that file before running the tunnel again",
            path.display()
        ))
    };
    if store::occupied(&path).map_err(|_| unavailable())? {
        state::open(&path).map_err(|_| unavailable())?;
        store::sharing_retry(|| fs::remove_file(&path)).map_err(|_| unavailable())?;
        setup::sync_directory(root)?;
    }
    Ok(())
}

pub(super) fn saved_outcome(root: &Path, name: &str) -> Option<Outcome> {
    store::private_read(&result_path(root, name), LIMIT)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Outcome>(&bytes).ok())
        .filter(valid_outcome)
}

pub(super) fn read_outcome(root: &Path, name: &str) -> Option<Outcome> {
    let saved = saved_outcome(root, name);
    let fallback = super::read_record(&super::record::path(root, name))
        .ok()
        .and_then(|record| {
            record
                .refresh_outcome
                .as_ref()
                .filter(|outcome| valid_outcome(outcome) && outcome.for_record(&record))
                .cloned()
        });
    // A terminal state can be committed to the supervisor's process record
    // if the standalone journal is held open. Use that matching commit.
    if let Some(fallback) = fallback
        && saved
            .as_ref()
            .is_none_or(|saved| saved.phase == Phase::Pending && saved.same_request(&fallback))
    {
        return Some(fallback);
    }
    saved
}

pub(super) fn repair_outcome(root: &Path, name: &str, record: &LaunchRecord) -> Result<(), Fail> {
    if let Some(outcome) = record.refresh_outcome.as_ref()
        && saved_outcome(root, name)
            .is_none_or(|saved| saved.phase == Phase::Pending && saved.same_request(outcome))
    {
        if !valid_outcome(outcome) || !outcome.for_record(record) {
            return Err(super::UNSAFE_STORE);
        }
        write_outcome(root, name, outcome)?;
    }
    Ok(())
}

fn valid_outcome(result: &Outcome) -> bool {
    let identifiers = [&result.id, &result.generation, &result.request_epoch]
        .into_iter()
        .all(|id| launch_id(id).is_some());
    let digests = [&result.previous_identity, &result.candidate]
        .into_iter()
        .all(|digest| crate::runtime::valid_digest(digest));
    let active = result
        .active_identity
        .as_deref()
        .is_some_and(crate::runtime::valid_digest);
    let epoch = result.active_epoch.as_deref().and_then(launch_id).is_some();
    identifiers
        && digests
        && match result.phase {
            Phase::Pending => {
                result.active_epoch.is_none()
                    && result.active_identity.is_none()
                    && result.cause.is_none()
            }
            Phase::Refreshed => epoch && active && result.cause.is_none(),
            Phase::RolledBack => {
                epoch
                    && result.active_identity.as_deref() == Some(result.previous_identity.as_str())
                    && result.cause.is_some()
            }
            Phase::Failed => epoch && result.active_identity.is_none() && result.cause.is_some(),
            Phase::Refused => epoch && active && result.cause.is_some(),
        }
}

pub(super) fn write_outcome(root: &Path, name: &str, outcome: &Outcome) -> Result<(), Fail> {
    let path = result_path(root, name);
    let bytes = serde_json::to_vec(outcome).map_err(|_| super::UNSAFE_STORE)?;
    if store::occupied(&path)? {
        setup::replace_private(&path, &bytes)
    } else {
        setup::publish_new(&path, |file| file.write_all(&bytes))
    }
}

fn signer(root: &Path, request: &Request) -> Result<hmac::Hmac<sha2::Sha256>, &'static str> {
    let mut signer = identity::mac(root).map_err(|_| "the refresh signing key is unavailable")?;
    signer.update(DOMAIN);
    signer.update(&serde_json::to_vec(request).map_err(|_| "the refresh request is malformed")?);
    Ok(signer)
}

fn encode(root: &Path, request: Request) -> Result<Vec<u8>, &'static str> {
    let tag = format!("{:x}", signer(root, &request)?.finalize().into_bytes());
    serde_json::to_vec(&Signed { request, tag }).map_err(|_| "the refresh request is malformed")
}

fn decode(root: &Path, bytes: &[u8]) -> Result<Request, &'static str> {
    let Signed { request, tag } =
        serde_json::from_slice(bytes).map_err(|_| "the refresh request is malformed")?;
    if [&request.id, &request.generation, &request.epoch]
        .into_iter()
        .any(|s| launch_id(s).is_none())
        || [&request.identity, &request.candidate, &tag]
            .into_iter()
            .any(|s| !crate::runtime::valid_digest(s))
    {
        return Err("the refresh request has an invalid identifier or digest");
    }
    let mut mac = [0u8; 32];
    for (out, pair) in mac.iter_mut().zip(tag.as_bytes().chunks_exact(2)) {
        let hex = |byte| match byte {
            b'0'..=b'9' => byte - b'0',
            b'a'..=b'f' => byte - b'a' + 10,
            _ => 0xff,
        };
        let hi = hex(pair[0]);
        let lo = hex(pair[1]);
        if hi > 15 || lo > 15 {
            return Err("the refresh request has an invalid authentication tag");
        }
        *out = hi << 4 | lo;
    }
    signer(root, &request)?
        .verify_slice(&mac)
        .map_err(|_| "the refresh request failed authentication")?;
    Ok(request)
}

pub(super) fn check_binding(
    request: &Request,
    record: &LaunchRecord,
    last: Option<&Outcome>,
) -> Result<(), Refusal> {
    let capability = record.refresh.as_ref().ok_or(Refusal::Stale)?;
    if request.generation != record.native_launch {
        return Err(Refusal::OtherGeneration);
    }
    if last.is_some_and(|last| last.generation == request.generation && last.id == request.id) {
        return Err(Refusal::Replay);
    }
    if !record.ready || request.epoch != capability.epoch {
        return Err(Refusal::Stale);
    }
    if record.launch_identity.as_deref() != Some(&request.identity) {
        return Err(Refusal::OtherIdentity);
    }
    Ok(())
}

fn sender_path(directory: &Path, id: &str) -> PathBuf {
    directory.join(format!("{SENDER_PREFIX}{id}.lock"))
}

pub(super) fn sender_file(name: &str) -> bool {
    name.strip_prefix(SENDER_PREFIX)
        .and_then(|id| id.strip_suffix(".lock"))
        .and_then(launch_id)
        .is_some()
}

pub(super) struct Ticket {
    request: Request,
    path: PathBuf,
    lock: Option<fs::File>,
    _publisher: fs::File,
}

impl Ticket {
    #[cfg(test)]
    pub(super) fn request(&self) -> Request {
        self.request.clone()
    }

    pub(super) fn queued(&self) -> bool {
        let directory = self.path.parent().expect("sender lock in launch directory");
        // An atomic claim cannot disappear between these observations.
        [REQUEST, CLAIM]
            .into_iter()
            .any(|name| store::occupied(&directory.join(name)).unwrap_or(true))
    }

    pub(super) fn issue(&self) -> Option<ChannelIssue> {
        self.path.parent().and_then(read_issue)
    }
}

impl Drop for Ticket {
    fn drop(&mut self) {
        drop(self.lock.take());
        let _ = store::sharing_retry(|| fs::remove_file(&self.path));
    }
}

pub(super) fn submit(
    root: &Path,
    name: &str,
    record: &LaunchRecord,
    candidate: &Image,
) -> Result<Ticket, &'static str> {
    let request = Request::for_launch(record, candidate)?;
    submit_request(root, name, record, request)
}

impl Request {
    pub(super) fn for_launch(
        record: &LaunchRecord,
        candidate: &Image,
    ) -> Result<Self, &'static str> {
        let capability = record
            .refresh
            .as_ref()
            .ok_or("the supervisor does not support refresh")?;
        Ok(Self {
            id: Uuid::new_v4().simple().to_string(),
            generation: record.native_launch.clone(),
            epoch: capability.epoch.clone(),
            identity: record
                .launch_identity
                .clone()
                .ok_or("the launch identity is unavailable")?,
            candidate: candidate.digest.clone(),
        })
    }
}

pub(super) fn submit_request(
    root: &Path,
    name: &str,
    record: &LaunchRecord,
    request: Request,
) -> Result<Ticket, &'static str> {
    let directory = launch_id(&record.native_launch)
        .map(|id| stage_path(root, id))
        .ok_or("the tunnel launch record is invalid")?;
    if !record.ready || !identity::owns_generation(root, name, &directory) {
        return Err("the tunnel launch changed before the refresh request");
    }
    let publisher = directory.join(PUBLISHER);
    let publisher = state::open_with_write(&publisher, true)
        .or_else(|_| state::create(&publisher))
        .map_err(|_| "the launch refresh publisher lock is unavailable")?
        .file;
    publisher
        .try_lock()
        .map_err(|_| "another refresh caller is already waiting for this launch")?;
    let bytes = encode(root, request.clone())?;
    let path = sender_path(&directory, &request.id);
    let lock = state::create(&path)
        .map_err(|_| "the refresh sender lock could not be created")?
        .file;
    let ticket = Ticket {
        request,
        path,
        lock: Some(lock),
        _publisher: publisher,
    };
    ticket
        .lock
        .as_ref()
        .unwrap()
        .try_lock()
        .map_err(|_| "the refresh sender lock could not be held")?;
    setup::publish_new(&directory.join(REQUEST), |file| file.write_all(&bytes)).map_err(|_| {
        read_issue(&directory).map_or(
            "a refresh is already queued or its request file could not be published",
            ChannelIssue::message,
        )
    })?;
    Ok(ticket)
}

pub(super) struct Claim {
    path: PathBuf,
    sender: Option<PathBuf>,
}

impl Claim {
    pub(super) fn clear_abandoned(directory: &Path) -> Result<(), ChannelIssue> {
        let path = directory.join(CLAIM);
        // This supervisor processes claims serially. At entry there is no
        // live claimant, so an owner-only file here is an abandoned claim.
        if store::occupied(&path).map_err(|_| ChannelIssue::ClaimBlocked)? {
            state::open(&path).map_err(|_| ChannelIssue::ClaimBlocked)?;
            store::sharing_retry(|| fs::remove_file(&path))
                .map_err(|_| ChannelIssue::ClaimBlocked)?;
        }
        Ok(())
    }

    pub(super) fn take(directory: &Path) -> Result<Option<Self>, ChannelIssue> {
        let source = directory.join(REQUEST);
        let path = directory.join(CLAIM);
        match fs::symlink_metadata(&source) {
            Ok(info) if info.is_file() && !crate::runtime::redirect(&info) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            _ => return Err(ChannelIssue::NonFile),
        }
        match setup::rename_new(&source, &path) {
            Ok(()) => Ok(Some(Self { path, sender: None })),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err(ChannelIssue::ClaimMove),
        }
    }

    pub(super) fn authenticate(&mut self, root: &Path) -> Result<Request, ChannelIssue> {
        let bytes = store::private_read(&self.path, LIMIT).map_err(|_| ChannelIssue::Unreadable)?;
        let request = decode(root, &bytes).map_err(|_| ChannelIssue::InvalidRequest)?;
        self.sender = self.path.parent().map(|dir| sender_path(dir, &request.id));
        Ok(request)
    }

    #[cfg(test)]
    fn read(
        &mut self,
        root: &Path,
        name: &str,
        record: &LaunchRecord,
    ) -> Result<Request, &'static str> {
        let request = self.authenticate(root).map_err(ChannelIssue::message)?;
        check_binding(&request, record, read_outcome(root, name).as_ref())
            .map_err(Refusal::message)?;
        if !self.sender_live() {
            return Err("the refresh request is stale because its sender has exited");
        }
        Ok(request)
    }

    /// The final check is the acceptance point. Only an authenticated
    /// request with a live sender reaches it, after local preflight.
    pub(super) fn sender_live(&self) -> bool {
        self.sender
            .as_ref()
            .and_then(|path| state::open_with_write(path, true).ok())
            .is_some_and(|lock| {
                matches!(
                    lock.file.try_lock_shared(),
                    Err(fs::TryLockError::WouldBlock)
                )
            })
    }
}

impl Drop for Claim {
    fn drop(&mut self) {
        let _ = store::sharing_retry(|| fs::remove_file(&self.path));
        // The caller owns the sender handle. On Windows its handle can
        // prevent removal until it observes the outcome and exits.
        if let Some(sender) = &self.sender {
            let _ = fs::remove_file(sender);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::{Identity, Stage, launch_locks};
    use super::*;

    struct Home(PathBuf);

    impl Home {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("tunnel-refresh-{}", Uuid::new_v4()));
            state::private_dir(&root).unwrap();
            identity::ensure_signing_key(&root).unwrap();
            Self(root)
        }
    }

    impl Drop for Home {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn setup() -> (Home, LaunchRecord) {
        let home = Home::new();
        let exe = std::env::current_exe().unwrap();
        let record = LaunchRecord {
            native_launch: Uuid::new_v4().simple().to_string(),
            launch_identity: Some("a".repeat(64)),
            spawn_started: Some(true),
            supervisor: Identity {
                pid: std::process::id(),
                exe: exe.clone(),
            },
            target: None,
            ready: true,
            refresh_outcome: None,
            bridge_attempt: None,
            refresh: Some(Capability {
                epoch: Uuid::new_v4().simple().to_string(),
                image: Image {
                    path: exe,
                    digest: "b".repeat(64),
                    version: "0.24.0".to_owned(),
                    vendor_version: "0.0.15".to_owned(),
                },
            }),
        };
        (home, record)
    }

    fn fixture_request(record: &LaunchRecord) -> Request {
        Request {
            id: Uuid::new_v4().simple().to_string(),
            generation: record.native_launch.clone(),
            epoch: record.refresh.as_ref().unwrap().epoch.clone(),
            identity: record.launch_identity.clone().unwrap(),
            candidate: "c".repeat(64),
        }
    }

    #[test]
    fn request_parsing_authenticates_every_typed_field_in_its_own_domain() {
        let (home, record) = setup();
        let other = Home::new();
        let request = fixture_request(&record);
        let encoded = encode(&home.0, request.clone()).unwrap();
        assert_eq!(decode(&home.0, &encoded), Ok(request.clone()));
        assert_eq!(
            decode(&other.0, &encoded),
            Err("the refresh request failed authentication")
        );
        let mut envelope: Signed = serde_json::from_slice(&encoded).unwrap();
        let mut raw = identity::mac(&home.0).unwrap();
        raw.update(&serde_json::to_vec(&request).unwrap());
        envelope.tag = format!("{:x}", raw.finalize().into_bytes());
        assert_eq!(
            decode(&home.0, &serde_json::to_vec(&envelope).unwrap()),
            Err("the refresh request failed authentication")
        );
        let encoded = String::from_utf8(encoded).unwrap();
        for modified in [
            encoded.replace(
                &format!("\"{}\"", request.id),
                "100000000000000000000000000000000000",
            ),
            encoded.replacen("\"id\":", &format!("\"id\":\"{}\",\"id\":", request.id), 1),
            encoded.replacen("\"request\":{", "\"request\":{\"path\":\"foreign\",", 1),
            encoded.replace(
                &format!("\"{}\"", request.id),
                r#"{"$serde_json::private::Number":"123"}"#,
            ),
        ] {
            assert_eq!(
                decode(&home.0, modified.as_bytes()),
                Err("the refresh request is malformed")
            );
        }
        for replacement in [
            &request.id,
            &request.generation,
            &request.epoch,
            &request.identity,
            &request.candidate,
        ] {
            let changed = encoded.replacen(replacement, &"d".repeat(replacement.len()), 1);
            assert_eq!(
                decode(&home.0, changed.as_bytes()),
                Err("the refresh request failed authentication")
            );
        }
    }

    #[test]
    fn another_generation_an_old_epoch_and_another_identity_cannot_refresh() {
        let (_home, mut record) = setup();
        let request = fixture_request(&record);
        assert_eq!(check_binding(&request, &record, None), Ok(()));
        let accepted = Outcome::new(&request, Phase::Pending, None, None);
        assert_eq!(
            check_binding(&request, &record, Some(&accepted)),
            Err(Refusal::Replay)
        );
        record.launch_identity = Some("b".repeat(64));
        assert_eq!(
            check_binding(&request, &record, None),
            Err(Refusal::OtherIdentity)
        );
        record.refresh.as_mut().unwrap().epoch = Uuid::new_v4().simple().to_string();
        assert_eq!(check_binding(&request, &record, None), Err(Refusal::Stale));
        record.native_launch = Uuid::new_v4().simple().to_string();
        assert_eq!(
            check_binding(&request, &record, None),
            Err(Refusal::OtherGeneration)
        );
    }

    #[test]
    fn refresh_request_is_private_consumed_once_and_its_sender_can_withdraw() {
        let (home, record) = setup();
        let _locks = launch_locks(&home.0, "dot").unwrap();
        let stage = Stage::new(&home.0, "dot", launch_id(&record.native_launch).unwrap()).unwrap();
        let image = &record.refresh.as_ref().unwrap().image;
        let ticket = submit(&home.0, "dot", &record, image).unwrap();
        assert!(state::open(&stage.directory.join(REQUEST)).is_ok());
        assert!(submit(&home.0, "dot", &record, image).is_err());
        let mut claim = Claim::take(&stage.directory).unwrap().unwrap();
        assert!(!stage.directory.join(REQUEST).exists());
        assert!(Claim::take(&stage.directory).unwrap().is_none());
        assert!(matches!(
            submit(&home.0, "dot", &record, image),
            Err("another refresh caller is already waiting for this launch")
        ));
        assert_eq!(
            claim.read(&home.0, "dot", &record),
            Ok(ticket.request.clone())
        );
        drop(ticket);
        assert!(!claim.sender_live());
        assert_eq!(
            claim.read(&home.0, "dot", &record),
            Err("the refresh request is stale because its sender has exited")
        );
    }

    #[test]
    fn abandoned_refresh_publication_is_cleaned_only_without_an_active_publisher() {
        let (home, record) = setup();
        let stage = Stage::new(&home.0, "dot", launch_id(&record.native_launch).unwrap()).unwrap();
        let directory = stage.directory.clone();
        let abandoned = directory.join(format!(".tunnel-setup-{}", Uuid::new_v4().simple()));
        state::atomic_create(&abandoned, b"unpublished request fixture").unwrap();
        let publisher = state::create(&directory.join(PUBLISHER)).unwrap().file;
        publisher.try_lock().unwrap();
        assert!(super::super::remove_launch_files(&directory, &stage.profile).is_err());
        assert!(abandoned.is_file());
        drop(publisher);
        drop(stage);
        assert!(!directory.exists());
    }

    #[test]
    fn a_matching_process_record_recovers_the_terminal_outcome() {
        let (home, mut record) = setup();
        let request = fixture_request(&record);
        let mut stage =
            Stage::new(&home.0, "dot", launch_id(&record.native_launch).unwrap()).unwrap();
        write_outcome(
            &home.0,
            "dot",
            &Outcome::new(&request, Phase::Pending, None, None),
        )
        .unwrap();
        let capability = record.refresh.as_mut().unwrap();
        capability.image.digest.clone_from(&request.candidate);
        capability.epoch = Uuid::new_v4().simple().to_string();
        record.launch_identity = Some("e".repeat(64));
        let done = Outcome::new(
            &request,
            Phase::Refreshed,
            record.launch_identity.clone(),
            None,
        )
        .bind(&record);
        assert!(done.for_record(&record));
        record.refresh_outcome = Some(done.clone());
        stage.announce(&record).unwrap();
        assert_eq!(
            read_outcome(&home.0, "dot").unwrap().phase,
            Phase::Refreshed
        );
        assert_eq!(saved_outcome(&home.0, "dot").unwrap().phase, Phase::Pending);

        record.refresh.as_mut().unwrap().epoch = Uuid::new_v4().simple().to_string();
        stage.announce(&record).unwrap();
        assert!(!done.for_record(&record));
        assert_eq!(read_outcome(&home.0, "dot").unwrap().phase, Phase::Pending);
        record.refresh.as_mut().unwrap().epoch = done.active_epoch.clone().unwrap();
        record.refresh.as_mut().unwrap().image.digest = "d".repeat(64);
        assert!(!done.for_record(&record));
        record
            .refresh
            .as_mut()
            .unwrap()
            .image
            .digest
            .clone_from(&request.candidate);
        assert!(
            !Outcome {
                active_identity: None,
                ..done.clone()
            }
            .for_record(&record)
        );
        stage.announce(&record).unwrap();
        repair_outcome(&home.0, "dot", &record).unwrap();
        assert_eq!(
            saved_outcome(&home.0, "dot").unwrap().phase,
            Phase::Refreshed
        );
    }

    #[test]
    fn preswitch_failure_requires_the_exact_previous_identity_if_the_record_is_still_ready() {
        let (_home, mut record) = setup();
        let request = fixture_request(&record);
        let failure =
            Outcome::new(&request, Phase::Failed, None, Some(Cause::OldChildExited)).bind(&record);
        assert!(failure.for_record(&record));
        record.launch_identity = Some("d".repeat(64));
        assert!(!failure.for_record(&record));
        record.ready = false;
        assert!(failure.for_record(&record));
    }

    #[test]
    fn status_separates_an_older_pending_generation_and_ignores_stopped_success() {
        use super::super::restart::status;
        let (home, mut record) = setup();
        let request = fixture_request(&record);
        write_outcome(
            &home.0,
            "dot",
            &Outcome::new(&request, Phase::Pending, None, None),
        )
        .unwrap();
        record.native_launch = Uuid::new_v4().simple().to_string();
        let mut stage =
            Stage::new(&home.0, "dot", launch_id(&record.native_launch).unwrap()).unwrap();
        stage.announce(&record).unwrap();
        assert!(status(&home.0, "dot").is_none());
        let mut current = fixture_request(&record);
        current.candidate = record.refresh.as_ref().unwrap().image.digest.clone();
        let success = Outcome::new(
            &current,
            Phase::Refreshed,
            record.launch_identity.clone(),
            None,
        )
        .bind(&record);
        write_outcome(&home.0, "dot", &success).unwrap();
        assert_eq!(status(&home.0, "dot").unwrap().0, "refreshed");
        drop(stage);
        assert!(status(&home.0, "dot").is_none());
    }

    #[test]
    fn pending_status_requires_the_owning_supervisor_and_its_claim() {
        use super::super::restart::status;
        let (home, record) = setup();
        let _locks = launch_locks(&home.0, "dot").unwrap();
        let mut stage =
            Stage::new(&home.0, "dot", launch_id(&record.native_launch).unwrap()).unwrap();
        stage.announce(&record).unwrap();
        let request = fixture_request(&record);
        write_outcome(
            &home.0,
            "dot",
            &Outcome::new(&request, Phase::Pending, None, None),
        )
        .unwrap();
        let claim = stage.directory.join(CLAIM);
        state::atomic_create(&claim, b"owned claim fixture").unwrap();
        assert_eq!(status(&home.0, "dot").unwrap().0, "pending");
        fs::remove_file(claim).unwrap();
        let (state, detail) = status(&home.0, "dot").unwrap();
        assert_eq!(state, "failed");
        assert!(detail.contains("no durable refresh result"));
        drop(stage.owner.take());
        let (state, detail) = status(&home.0, "dot").unwrap();
        assert_eq!(state, "failed");
        assert!(detail.contains("supervisor exited before reporting the refresh outcome"));
    }

    #[test]
    fn an_owner_only_abandoned_claim_does_not_block_the_next_request() {
        let (home, record) = setup();
        let _locks = launch_locks(&home.0, "dot").unwrap();
        let stage = Stage::new(&home.0, "dot", launch_id(&record.native_launch).unwrap()).unwrap();
        state::atomic_create(&stage.directory.join(CLAIM), b"abandoned owner claim").unwrap();
        let image = &record.refresh.as_ref().unwrap().image;
        let ticket = submit(&home.0, "dot", &record, image).unwrap();
        Claim::clear_abandoned(&stage.directory).unwrap();
        let mut claim = Claim::take(&stage.directory).unwrap().unwrap();
        assert_eq!(
            claim.read(&home.0, "dot", &record),
            Ok(ticket.request.clone())
        );
    }

    #[cfg(windows)]
    #[test]
    fn refresh_outcome_retries_sharing_and_leaves_committed_state_when_still_held() {
        use std::{
            thread,
            time::{Duration, Instant},
        };
        let (home, record) = setup();
        let request = fixture_request(&record);
        let path = result_path(&home.0, "dot");
        let pending = Outcome::new(&request, Phase::Pending, None, None);
        let done =
            Outcome::new(&request, Phase::Refreshed, Some("c".repeat(64)), None).bind(&record);
        write_outcome(&home.0, "dot", &pending).unwrap();

        let blocking_reader = state::open(&path).unwrap();
        thread::scope(|scope| {
            let writer = scope.spawn(|| write_outcome(&home.0, "dot", &done));
            let deadline = Instant::now() + Duration::from_secs(5);
            while !fs::read_dir(&home.0).unwrap().flatten().any(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".tunnel-setup-")
            }) {
                assert!(Instant::now() < deadline && !writer.is_finished());
                thread::sleep(Duration::from_millis(2));
            }
            thread::sleep(Duration::from_millis(35));
            drop(blocking_reader);
            writer.join().unwrap().unwrap();
        });
        assert_eq!(
            read_outcome(&home.0, "dot").unwrap().phase,
            Phase::Refreshed
        );

        let blocking_reader = state::open(&path).unwrap();
        thread::scope(|scope| {
            let writer = scope.spawn(|| write_outcome(&home.0, "dot", &pending));
            // Even an unbounded retry in a mutation must eventually finish.
            let deadline = Instant::now() + Duration::from_secs(3);
            while !writer.is_finished() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
            drop(blocking_reader);
            assert!(writer.join().unwrap().is_err());
        });
        assert_eq!(
            read_outcome(&home.0, "dot").unwrap().phase,
            Phase::Refreshed
        );
    }
}
