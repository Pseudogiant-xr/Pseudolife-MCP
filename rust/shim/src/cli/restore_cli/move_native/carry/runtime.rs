//! Target-side execution. Bearers arrive on SSH stdin and go into the
//! installed private environment. The journal contains sizes and digests.
use super::*;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
};

const JOURNAL: &str = "carry.json";
const MAX_ENV: u64 = 1 << 20;

#[derive(Serialize, Deserialize)]
struct Journal {
    deployment: Deployment,
    pins: TargetPins,
    running_shape: [u8; 32],
    original_bytes: usize,
    original_digest: [u8; 32],
    appended_bytes: usize,
    appended_digest: [u8; 32],
    #[serde(default)]
    restored: bool,
}

pub(in crate::cli::restore_cli::move_native) fn recorded_pins(
    folder: &Path,
) -> Result<Option<TargetPins>, String> {
    Ok(read_journal(&folder.join(JOURNAL))?.map(|journal| journal.pins))
}

fn read_journal(path: &Path) -> Result<Option<Journal>, String> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map(Some)
            .map_err(|_| ENV.into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(ENV.into()),
    }
}

fn digest(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

fn running_shape(container: &Value) -> [u8; 32] {
    let mut environment: BTreeMap<_, _> = container["Config"]["Env"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter_map(|entry| entry.split_once('='))
        .collect();
    for key in KEYS {
        environment.remove(key);
    }
    let mut mounts = container["Mounts"].as_array().cloned().unwrap_or_default();
    mounts.sort_by(|a, b| a["Destination"].as_str().cmp(&b["Destination"].as_str()));
    digest(
        &serde_json::to_vec(&(
            environment,
            mounts,
            &container["HostConfig"]["NetworkMode"],
            &container["HostConfig"]["PortBindings"],
        ))
        .expect("Docker inspection JSON"),
    )
}

fn open_env(path: &Path) -> Result<fs::File, String> {
    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd;
        // Resolve the checked regular file by descriptor, so a pathname
        // replacement cannot redirect the write to a device or another file.
        let descriptor = env_descriptor(path)?;
        fs::OpenOptions::new()
            .read(true)
            .append(true)
            .open(format!("/proc/self/fd/{}", descriptor.as_raw_fd()))
            .map_err(|_| ENV.into())
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = path;
        Err(ENV.into())
    }
}

fn contents(file: &mut fs::File) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    file.take(MAX_ENV + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ENV)?;
    if bytes.len() as u64 > MAX_ENV {
        return Err(ENV.into());
    }
    Ok(bytes)
}

fn marker(id: &str) -> String {
    format!("\n# Pseudolife move {id}\n")
}

fn owns_env_suffix(
    suffix: &[u8],
    header: &[u8],
    appended_bytes: usize,
    appended_digest: [u8; 32],
) -> bool {
    header.starts_with(suffix)
        || (suffix.len() == appended_bytes && digest(suffix) == appended_digest)
}

impl Journal {
    fn matches(&self, container: &Value) -> bool {
        let labels = &container["Config"]["Labels"];
        labels["com.docker.compose.project"] == self.deployment.project
            && labels["com.docker.compose.service"] == DAEMON_SERVICE
            && container["Image"] == self.deployment.image_id
            && running_shape(container) == self.running_shape
    }

    fn postgres(&self, docker: &mut impl Docker) -> Result<(), String> {
        let pg = TargetPins::inspect(docker, &self.pins.postgres)?;
        let labels = &pg["Config"]["Labels"];
        (labels["com.docker.compose.project"] == self.deployment.project
            && labels["com.docker.compose.service"] == PG_SERVICE)
            .then_some(())
            .ok_or_else(|| TARGET_PIN.into())
    }

    /// Compose is allowed to replace the daemon. Resolve its own project
    /// service, then verify the returned ID and the original deployment.
    fn compose_daemon(&self, docker: &mut docker::Real) -> Result<Option<Value>, String> {
        let output = self.deployment.compose(
            &docker.program,
            &["ps", "--all", "--quiet", DAEMON_SERVICE],
            &seams::Environment::new(),
        )?;
        let id = output.trim();
        if id.is_empty() {
            return Ok(None);
        }
        let daemon = TargetPins::inspect(docker, id)?;
        if !self.matches(&daemon) {
            return Err(TARGET_PIN.into());
        }
        Ok(Some(daemon))
    }

    fn save(&self, path: &Path) -> Result<(), String> {
        let data = serde_json::to_vec(self).map_err(|_| ENV)?;
        crate::board::state::atomic_write(path, &data, None).map_err(|_| ENV.into())
    }

    fn recreate(
        &mut self,
        docker: &mut docker::Real,
        carried: Option<&Identities>,
    ) -> Result<(), String> {
        self.postgres(docker)?;
        self.deployment.compose(&docker.program, &[
            "up", "--no-start", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", DAEMON_SERVICE,
        ], &seams::Environment::new())
            .map_err(|_| "the target's daemon could not be recreated stopped; repair its Compose deployment and rerun move --rollback")?;
        let container = self.compose_daemon(docker)?.ok_or(STACK)?;
        if container["State"]["Running"] != false {
            return Err(STACK.into());
        }
        if carried.is_some_and(|settings| !container_matches(&container, settings)) {
            return Err(INTERPOLATION.into());
        }
        let id = container["Id"].as_str().ok_or(STACK)?;
        let restart = docker_run(docker, &["update", "--restart=no", id]);
        if !restart.ok() {
            return Err(STACK.into());
        }
        self.pins.daemon = id.to_owned();
        Ok(())
    }
}

fn config(deployment: &Deployment, docker: &docker::Real) -> Result<Value, String> {
    let output = deployment.compose(
        &docker.program,
        &["config", "--format", "json"],
        &seams::Environment::new(),
    )?;
    serde_json::from_str(&output).map_err(|_| STACK.into())
}

pub(in crate::cli::restore_cli::move_native) fn apply(
    docker: &mut docker::Real,
    folder: &Path,
    id: &str,
    identities: &Identities,
    pins: &TargetPins,
) -> Result<(), String> {
    let (deployment, original) = checked_target(docker, shape(&identities.values)?, Some(pins))?;
    let container = TargetPins::inspect(docker, &pins.daemon)?;
    if container["State"]["Running"] != false || container["Image"] != deployment.image_id {
        return Err("the target daemon changed after preflight; rerun move --rollback".into());
    }
    // Compose escapes dollars in config output so the rendered file can
    // be fed back to Compose. Docker inspect must still show literal values.
    let rendered: Vec<_> = identities
        .entries()
        .map(|(key, value)| (key, compose_value(value)))
        .collect();
    let expected = with_identities(
        original,
        rendered.iter().map(|(key, value)| (*key, value.as_str())),
    )?;
    let mut file = open_env(Path::new(&deployment.env_file))?;
    let previous = contents(&mut file)?;
    let addition = format!("{}{}", marker(id), identities.assignments()?);
    let path = folder.join(JOURNAL);
    if previous.len() + addition.len() > MAX_ENV as usize
        || read_journal(&path)?.is_some_and(|journal| !journal.restored)
    {
        return Err("the target environment is too large or already has an unfinished identity change; rerun move --rollback".into());
    }
    let mut journal = Journal {
        deployment,
        pins: pins.clone(),
        running_shape: running_shape(&container),
        original_bytes: previous.len(),
        original_digest: digest(&previous),
        appended_bytes: addition.len(),
        appended_digest: digest(addition.as_bytes()),
        restored: false,
    };
    journal.save(&path)?;
    file.write_all(addition.as_bytes())
        .and_then(|()| file.sync_all())
        .map_err(|_| ENV)?;
    if config(&journal.deployment, docker)? != expected {
        return Err(INTERPOLATION.into());
    }
    journal.recreate(docker, Some(identities))?;
    journal.save(&path)
}

pub(in crate::cli::restore_cli::move_native) fn undo(
    docker: &mut docker::Real,
    folder: &Path,
    id: &str,
) -> Result<(), String> {
    let path = folder.join(JOURNAL);
    let Some(mut journal) = read_journal(&path)? else {
        return Ok(());
    };
    journal.postgres(docker)?;
    if journal
        .compose_daemon(docker)?
        .is_some_and(|value| value["State"]["Running"] != false)
    {
        return Err("the target daemon must be stopped to restore its identity settings; rerun move --rollback".into());
    }
    if journal.restored {
        return Ok(());
    }
    let mut file = open_env(Path::new(&journal.deployment.env_file))?;
    let current = contents(&mut file)?;
    let (prefix, suffix) = current
        .split_at_checked(journal.original_bytes)
        .ok_or(ENV)?;
    let header = marker(id);
    if digest(prefix) != journal.original_digest
        || !owns_env_suffix(
            suffix,
            header.as_bytes(),
            journal.appended_bytes,
            journal.appended_digest,
        )
    {
        return Err("the target's environment changed outside this move; restore its original configuration, then rerun move --rollback".into());
    }
    file.set_len(journal.original_bytes as u64)
        .and_then(|()| file.sync_all())
        .map_err(|_| ENV)?;
    let hashes = journal.deployment.compose(
        &docker.program,
        &["config", "--hash", "*"],
        &seams::Environment::new(),
    )?;
    journal.deployment.check_hashes(&hashes)?;
    journal.recreate(docker, None)?;
    journal.restored = true;
    journal.save(&path)
}

pub(in crate::cli::restore_cli::move_native) fn loopback(url: &str) -> bool {
    reqwest::Url::parse(url)
        .ok()
        .and_then(|url| {
            url.host_str()?
                .trim_matches(['[', ']'])
                .parse::<std::net::IpAddr>()
                .ok()
        })
        .is_some_and(|ip| ip.is_loopback())
}

pub(in crate::cli::restore_cli::move_native) fn verify(
    docker: &mut impl Docker,
    folder: &Path,
    identities: &Identities,
) -> Result<(), String> {
    let journal = read_journal(&folder.join(JOURNAL))?
        .filter(|journal| !journal.restored)
        .ok_or(ENV)?;
    journal.postgres(docker)?;
    let container = TargetPins::inspect(docker, &journal.pins.daemon)?;
    if !journal.matches(&container) || container["State"]["Running"] != true {
        return Err(TARGET_PIN.into());
    }
    if !container_matches(&container, identities) {
        return Err(INTERPOLATION.into());
    }
    let url = crate::cli::restore_cli::bank::daemon_url(&container, None, &journal.pins.daemon)
        .map_err(|_| STACK.to_owned())?;
    let url = if url.is_empty() {
        super::super::probe::host_network_url(&container)
    } else {
        url
    };
    if !loopback(&url) {
        return Err("a loopback daemon binding is required to verify carried identities; repair the target binding and rerun move --rollback".into());
    }
    let deadline = std::time::Instant::now() + Duration::from_secs(120);
    while std::time::Instant::now() < deadline {
        if super::super::get_json(&format!("{url}/health"))
            .is_ok_and(|health| health["auth"] == true)
            && seams::authenticated_read(&url, &identities.probe)
        {
            return Ok(());
        }
        std::thread::sleep(Duration::from_secs(2));
    }
    Err("the target's local daemon did not accept the carried bearer; check its authentication and rerun move --rollback".into())
}

pub(super) fn compose_value(value: &str) -> String {
    value.replace('$', "$$")
}

fn container_matches(container: &Value, identities: &Identities) -> bool {
    let env: BTreeMap<_, _> = container["Config"]["Env"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter_map(|entry| entry.split_once('='))
        .collect();
    identities
        .entries()
        .all(|(key, value)| env.get(key).copied().unwrap_or_default() == value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rollback_refuses_unverified_edits_after_the_move_header() {
        let header = marker("test-move");
        let expected = format!("{header}PSEUDOLIFE_MCP_TOKEN='long-test-fixture-bearer'\n");
        let changed = format!("{header}OTHER_SETTING='keep'\n");
        assert!(changed.len() < expected.len());
        let owns = |suffix: &[u8]| {
            owns_env_suffix(
                suffix,
                header.as_bytes(),
                expected.len(),
                digest(expected.as_bytes()),
            )
        };
        assert!(owns(b""));
        assert!(owns(&header.as_bytes()[..3]));
        assert!(owns(header.as_bytes()));
        assert!(owns(expected.as_bytes()));
        assert!(!owns(&expected.as_bytes()[..header.len() + 5]));
        assert!(!owns(changed.as_bytes()));
    }
}
