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
    original_pins: TargetPins,
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
    supplied: Option<&TargetPins>,
) -> Result<Option<TargetPins>, String> {
    checked_journal(folder, supplied).map(|journal| journal.map(|journal| journal.pins))
}

fn checked_journal(
    folder: &Path,
    supplied: Option<&TargetPins>,
) -> Result<Option<Journal>, String> {
    let journal = read_journal(&folder.join(JOURNAL))?;
    if let Some(journal) = &journal {
        let original = supplied.ok_or(TARGET_PIN)?;
        // A resumed move uses the verified daemon left by the previous
        // rollback. Its new preflight seals the same deployment with that ID.
        if journal.restored
            && journal.original_pins.daemon != original.daemon
            && journal.pins.daemon == original.daemon
            && journal.pins.postgres == original.postgres
            && journal.pins.project == original.project
        {
            journal.deployment.check_binding(original)?;
            return Ok(None);
        }
        if &journal.original_pins != original
            || journal.pins.postgres != original.postgres
            || journal.pins.project != original.project
            || journal.pins.deployment_digest != original.deployment_digest
            || !super::super::probe::container_id(&journal.pins.daemon)
        {
            return Err(TARGET_PIN.into());
        }
        journal.deployment.check_binding(original)?;
    }
    Ok(journal)
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
    if suffix.len() >= appended_bytes {
        return suffix.len() == appended_bytes && digest(suffix) == appended_digest;
    }
    if header.starts_with(suffix) {
        return true;
    }
    // A torn append must fit this move's marker and assignment format.
    // A complete append still has to match the recorded digest.
    let Some(mut tail) = suffix.strip_prefix(header) else {
        return false;
    };
    for key in KEYS {
        let start = format!("{key}='");
        if start.as_bytes().starts_with(tail) {
            return true;
        }
        let Some(value) = tail.strip_prefix(start.as_bytes()) else {
            return false;
        };
        let end = value.iter().position(|byte| *byte == b'\'');
        if value[..end.unwrap_or(value.len())]
            .iter()
            .any(u8::is_ascii_control)
        {
            return false;
        }
        let Some(end) = end else {
            return true;
        };
        let rest = &value[end + 1..];
        if rest.is_empty() {
            return true;
        }
        let Some(next) = rest.strip_prefix(b"\n") else {
            return false;
        };
        tail = next;
    }
    tail.is_empty()
}

type Compose<'a> =
    dyn FnMut(&Deployment, &[&str], &seams::Environment) -> Result<String, String> + 'a;

impl Journal {
    fn matches(&self, container: &Value) -> bool {
        let labels = &container["Config"]["Labels"];
        labels["com.docker.compose.project"] == self.deployment.project
            && labels["com.docker.compose.service"] == DAEMON_SERVICE
            && container["Image"] == self.deployment.image_id
            && running_shape(container) == self.running_shape
    }

    fn postgres(&self, docker: &mut impl Docker) -> Result<(), String> {
        self.pins.inspect_postgres(docker).map(drop)
    }

    /// Compose is allowed to replace the daemon. Resolve its own project
    /// service, then verify the returned ID and the original deployment.
    fn compose_daemon(
        &self,
        docker: &mut impl Docker,
        compose: &mut Compose<'_>,
    ) -> Result<Option<Value>, String> {
        let output = compose(
            &self.deployment,
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
        docker: &mut impl Docker,
        compose: &mut Compose<'_>,
        carried: Option<&Identities>,
    ) -> Result<(), String> {
        self.postgres(docker)?;
        compose(&self.deployment, &[
            "up", "--no-start", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", DAEMON_SERVICE,
        ], &seams::Environment::new())
            .map_err(|_| "the target's daemon could not be recreated stopped; repair its Compose deployment and rerun move --rollback")?;
        let container = self.compose_daemon(docker, compose)?.ok_or(STACK)?;
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

fn config(deployment: &Deployment, compose: &mut Compose<'_>) -> Result<Value, String> {
    let output = compose(
        deployment,
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
    files: &TargetFiles,
    home: Option<&Path>,
) -> Result<(), String> {
    let program = docker.program.clone();
    apply_with(
        docker,
        folder,
        Application {
            id,
            identities,
            pins,
            files: &files.resolve(home)?,
        },
        &mut |deployment, args, env| deployment.compose(&program, args, env),
        |file, bytes| file.write_all(bytes),
    )
}

#[derive(Clone, Copy)]
struct Application<'a> {
    id: &'a str,
    identities: &'a Identities,
    pins: &'a TargetPins,
    files: &'a TargetFiles,
}

fn apply_with(
    docker: &mut impl Docker,
    folder: &Path,
    application: Application<'_>,
    compose: &mut Compose<'_>,
    append: impl FnOnce(&mut fs::File, &[u8]) -> std::io::Result<()>,
) -> Result<(), String> {
    let Application {
        id,
        identities,
        pins,
        files,
    } = application;
    let (deployment, original) = target_with(
        docker,
        shape(&identities.values)?,
        Some(pins),
        files,
        &mut *compose,
    )?;
    deployment.check_binding(pins)?;
    let container = pins.inspect_daemon(docker)?;
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
        original_pins: pins.clone(),
        pins: pins.clone(),
        running_shape: running_shape(&container),
        original_bytes: previous.len(),
        original_digest: digest(&previous),
        appended_bytes: addition.len(),
        appended_digest: digest(addition.as_bytes()),
        restored: false,
    };
    journal.save(&path)?;
    if append(&mut file, addition.as_bytes())
        .and_then(|()| file.sync_all())
        .is_err()
    {
        file.set_len(journal.original_bytes as u64)
            .and_then(|()| file.sync_all())
            .map_err(|_| "the target environment write was incomplete and could not be restored; rerun move --rollback")?;
        return Err(ENV.into());
    }
    if config(&journal.deployment, compose)? != expected {
        return Err(INTERPOLATION.into());
    }
    journal.recreate(docker, compose, Some(identities))?;
    journal.save(&path)
}

pub(in crate::cli::restore_cli::move_native) fn undo(
    docker: &mut docker::Real,
    folder: &Path,
    id: &str,
    supplied: Option<&TargetPins>,
) -> Result<(), String> {
    let program = docker.program.clone();
    undo_with(
        docker,
        folder,
        id,
        supplied,
        &mut |deployment, args, env| deployment.compose(&program, args, env),
    )
}

fn undo_with(
    docker: &mut impl Docker,
    folder: &Path,
    id: &str,
    supplied: Option<&TargetPins>,
    compose: &mut Compose<'_>,
) -> Result<(), String> {
    let path = folder.join(JOURNAL);
    let Some(mut journal) = checked_journal(folder, supplied)? else {
        return Ok(());
    };
    journal.postgres(docker)?;
    if journal
        .compose_daemon(docker, compose)?
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
    let hashes = compose(
        &journal.deployment,
        &["config", "--hash", "*"],
        &seams::Environment::new(),
    )?;
    journal.deployment.check_hashes(&hashes)?;
    journal.recreate(docker, compose, None)?;
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
    supplied: Option<&TargetPins>,
) -> Result<(), String> {
    verify_with(docker, folder, identities, supplied, verify_local)
}

fn verify_with(
    docker: &mut impl Docker,
    folder: &Path,
    identities: &Identities,
    supplied: Option<&TargetPins>,
    local: impl FnOnce(&str, &str) -> Result<(), String>,
) -> Result<(), String> {
    let journal = checked_journal(folder, supplied)?
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
    local(&url, &identities.probe)
}

fn verify_local(url: &str, probe: &str) -> Result<(), String> {
    let deadline = std::time::Instant::now() + Duration::from_secs(120);
    while std::time::Instant::now() < deadline {
        if super::super::get_json(&format!("{url}/health"))
            .is_ok_and(|health| health["auth"] == true)
            && seams::authenticated_read(url, probe)
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

    #[cfg(target_os = "linux")]
    mod transaction {
        use super::*;
        use crate::cli::restore_cli::docker::{
            Input, Output, Ran,
            script::{fail, ok},
        };
        use serde_json::json;
        use std::{
            cell::{Cell, RefCell},
            os::unix::fs::OpenOptionsExt,
            path::PathBuf,
            time::Duration,
        };

        const ID: &str = "20261120-000000-aabbccdd";
        const ORIGINAL: &[u8] = b"# preserve this configuration\nFIXTURE=one\n";
        const IMAGE: &str = "sha256:fixture";
        const HASHES: &str = "pseudolife-daemon daemon-hash\npseudolife-pg pg-hash";

        struct Fixture {
            root: PathBuf,
            recreated: Cell<bool>,
            rendering_changed: Cell<bool>,
            running_changed: Cell<bool>,
            running: Cell<bool>,
            active_env: RefCell<Option<PathBuf>>,
            exported_only: Cell<bool>,
        }

        impl Fixture {
            fn new() -> Self {
                let root = std::env::temp_dir()
                    .join(format!("move-carry-{}", uuid::Uuid::new_v4().simple()));
                fs::create_dir(&root).unwrap();
                fs::write(root.join("compose.yaml"), b"fixture").unwrap();
                fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(root.join(".env"))
                    .unwrap()
                    .write_all(ORIGINAL)
                    .unwrap();
                Self {
                    root,
                    recreated: Cell::new(false),
                    rendering_changed: Cell::new(false),
                    running_changed: Cell::new(false),
                    running: Cell::new(false),
                    active_env: RefCell::new(None),
                    exported_only: Cell::new(false),
                }
            }

            fn pins(&self) -> TargetPins {
                self.pins_for(&TargetFiles::default())
            }

            fn pins_for(&self, files: &TargetFiles) -> TargetPins {
                let mut pins = TargetPins {
                    daemon: "1".repeat(64),
                    postgres: "2".repeat(64),
                    project: "fixture".into(),
                    ..Default::default()
                };
                let (deployment, _) = target_with(
                    &mut Engine(self),
                    [true, false, false, true],
                    Some(&pins),
                    files,
                    |deployment, args, env| self.compose(deployment, args, env),
                )
                .unwrap();
                pins.deployment_digest = Some(deployment.digest(&pins).unwrap());
                pins
            }

            fn env(path: &Path) -> BTreeMap<String, String> {
                fs::read_to_string(path)
                    .unwrap()
                    .lines()
                    .filter_map(|line| line.split_once('='))
                    .map(|(key, value)| (key.to_owned(), value.trim_matches('\'').to_owned()))
                    .collect()
            }

            fn compose(
                &self,
                deployment: &Deployment,
                args: &[&str],
                overrides: &seams::Environment,
            ) -> Result<String, String> {
                match args {
                    ["config", "--hash", "*"] => Ok(HASHES.into()),
                    ["config", "--format", "json"] => {
                        let mut env = Self::env(Path::new(&deployment.env_file));
                        if self.rendering_changed.get() && env.contains_key(KEYS[0]) {
                            env.insert("FIXTURE".into(), "unexpected".into());
                        }
                        env.extend(overrides.clone());
                        Ok(json!({"services": {
                            "pseudolife-daemon": {"environment":env,
                                "volumes":[{"type":"volume", "source":"state", "target":"/data"}]},
                            "pseudolife-pg": {"volumes":[{"type":"volume", "source":"pg", "target":"/var/lib/postgresql"}]}
                        }, "volumes": {"state":{"name":"fixture-state"}, "pg":{"name":"fixture-pg"}}}).to_string())
                    }
                    ["ps", "--all", "--quiet", DAEMON_SERVICE] => {
                        Ok(if self.recreated.get() { "3" } else { "1" }.repeat(64))
                    }
                    ["up", ..] => {
                        *self.active_env.borrow_mut() = Some(PathBuf::from(&deployment.env_file));
                        self.recreated.set(true);
                        Ok(String::new())
                    }
                    _ => panic!("unexpected fixture Compose shape"),
                }
            }
        }

        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.root);
            }
        }

        struct Engine<'a>(&'a Fixture);

        impl Docker for Engine<'_> {
            fn run_io(&mut self, args: &[String], _: Duration, _: Input<'_>, _: Output<'_>) -> Ran {
                let f = self.0;
                let words: Vec<_> = args.iter().map(String::as_str).collect();
                match words.as_slice() {
                    ["image", "inspect", ..] => ok(IMAGE),
                    ["update", "--restart=no", ..] => ok(""),
                    ["inspect", "--type=container", id] if *id == "2".repeat(64) => {
                        ok(&json!([{"Id":id,"Config":{"Labels":{
                            "com.docker.compose.project":"fixture","com.docker.compose.service":PG_SERVICE,
                            "com.docker.compose.config-hash":"pg-hash"
                        }},"Mounts":[{"Destination":"/var/lib/postgresql","Type":"volume","Name":"fixture-pg"}]}]).to_string())
                    }
                    ["inspect", "--type=container", id] => {
                        let mut env = if f.recreated.get() {
                            Fixture::env(f.active_env.borrow().as_deref().unwrap())
                        } else {
                            BTreeMap::from([("FIXTURE".into(),"one".into())])
                        };
                        if f.running_changed.get() {
                            env.insert("FIXTURE".into(), "unexpected".into());
                        }
                        let env: Vec<_> = env.iter().map(|(key,value)| format!("{key}={value}")).collect();
                        let labelled_dir = if f.exported_only.get() { f.root.join("not-visible") } else { f.root.clone() };
                        ok(&json!([{"Id":id,"Image":IMAGE,"State":{"Running":f.running.get()},
                            "Config":{"Env":env,"Image":"ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.25.0","Labels":{
                                "com.docker.compose.project":"fixture","com.docker.compose.service":DAEMON_SERVICE,
                                "com.docker.compose.config-hash":"daemon-hash",
                                "com.docker.compose.project.working_dir":labelled_dir,
                                "com.docker.compose.project.config_files":labelled_dir.join("compose.yaml")
                            }},"Mounts":[{"Destination":"/data","Type":"volume","Name":"fixture-state"}],
                            "HostConfig":{"PortBindings":{"8765/tcp":[{"HostIp":"127.0.0.1","HostPort":"8765"}]}}
                        }]).to_string())
                    }
                    _ => fail(1, "unexpected fixture command"),
                }
            }
        }

        #[test]
        fn explicit_target_files_drive_apply_and_undo_in_order() {
            let f = Fixture::new();
            let export = f.root.join("export");
            fs::create_dir(&export).unwrap();
            for file in ["base.yaml", "override.yaml"] {
                fs::write(export.join(file), "fixture").unwrap();
            }
            for file in ["target env", "other env"] {
                fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(export.join(file))
                    .unwrap()
                    .write_all(ORIGINAL)
                    .unwrap();
            }
            f.exported_only.set(true);
            let files = TargetFiles {
                env: Some("~/export/target env".into()),
                compose: vec!["~/export/base.yaml".into(), "~/export/override.yaml".into()],
            }
            .resolve(Some(&f.root))
            .unwrap();
            let pins = f.pins_for(&files);
            let identities = fixture();
            let result = target_with(
                &mut Engine(&f),
                [true, false, false, true],
                Some(&pins),
                &TargetFiles::default(),
                |deployment, args, env| f.compose(deployment, args, env),
            );
            assert!(result.err().unwrap().contains("--target-compose-file"));
            let application = Application {
                id: ID,
                identities: &identities,
                pins: &pins,
                files: &files,
            };
            let alternate = TargetFiles {
                env: Some(export.join("other env").to_str().unwrap().into()),
                ..files.clone()
            };
            let mut compose = |deployment: &Deployment, args: &[&str], env: &seams::Environment| {
                f.compose(deployment, args, env)
            };
            assert!(
                apply_with(
                    &mut Engine(&f),
                    &f.root,
                    Application {
                        files: &alternate,
                        ..application
                    },
                    &mut compose,
                    |_, _| panic!("an unverified path reached the append")
                )
                .is_err()
            );
            apply_with(
                &mut Engine(&f),
                &f.root,
                application,
                &mut compose,
                |file, bytes| file.write_all(bytes),
            )
            .unwrap();
            let journal = read_journal(&f.root.join(JOURNAL)).unwrap().unwrap();
            assert_eq!(
                journal.deployment.args,
                [
                    "compose",
                    "--project-name",
                    "fixture",
                    "--project-directory",
                    export.to_str().unwrap(),
                    "--env-file",
                    files.env.as_deref().unwrap(),
                    "-f",
                    &files.compose[0],
                    "-f",
                    &files.compose[1],
                ]
            );
            assert!(fs::read(export.join("target env")).unwrap().len() > ORIGINAL.len());
            assert!(fs::read(f.root.join(".env")).unwrap() == ORIGINAL);
            undo_with(&mut Engine(&f), &f.root, ID, Some(&pins), &mut compose).unwrap();
            for file in ["target env", "other env"] {
                assert!(fs::read(export.join(file)).unwrap() == ORIGINAL);
            }
        }

        #[test]
        fn torn_carry_write_restores_original_and_undo_recovers_a_marked_partial_suffix() {
            let f = Fixture::new();
            let pins = f.pins();
            let identities = fixture();
            let files = TargetFiles::default();
            let mut partial = Vec::new();
            let result = apply_with(
                &mut Engine(&f),
                &f.root,
                Application {
                    id: ID,
                    identities: &identities,
                    pins: &pins,
                    files: &files,
                },
                &mut |deployment, args, env| f.compose(deployment, args, env),
                |file, bytes| {
                    assert!(read_journal(&f.root.join(JOURNAL)).unwrap().is_some());
                    partial.extend_from_slice(&bytes[..marker(ID).len() + KEYS[0].len() + 5]);
                    file.write_all(&partial)?;
                    Err(std::io::ErrorKind::StorageFull.into())
                },
            );
            assert!(result.is_err());
            assert!(fs::read(f.root.join(".env")).unwrap() == ORIGINAL);
            // Simulate a process that stopped before it could truncate.
            fs::OpenOptions::new()
                .append(true)
                .open(f.root.join(".env"))
                .unwrap()
                .write_all(&partial)
                .unwrap();
            undo_with(
                &mut Engine(&f),
                &f.root,
                ID,
                Some(&pins),
                &mut |deployment, args, env| f.compose(deployment, args, env),
            )
            .unwrap();
            assert!(fs::read(f.root.join(".env")).unwrap() == ORIGINAL);
            assert!(
                read_journal(&f.root.join(JOURNAL))
                    .unwrap()
                    .unwrap()
                    .restored
            );
        }

        #[test]
        fn carry_runtime_checks_render_and_live_shape_and_seals_deployment_before_undo() {
            let f = Fixture::new();
            let pins = f.pins();
            let identities = fixture();
            let files = TargetFiles::default();
            let application = Application {
                id: ID,
                identities: &identities,
                pins: &pins,
                files: &files,
            };
            let mut compose = |deployment: &Deployment, args: &[&str], env: &seams::Environment| {
                f.compose(deployment, args, env)
            };
            f.rendering_changed.set(true);
            assert_eq!(
                apply_with(
                    &mut Engine(&f),
                    &f.root,
                    application,
                    &mut compose,
                    |file, bytes| file.write_all(bytes)
                )
                .unwrap_err(),
                INTERPOLATION
            );
            assert!(!f.recreated.get());
            f.rendering_changed.set(false);
            undo_with(&mut Engine(&f), &f.root, ID, Some(&pins), &mut compose).unwrap();
            f.recreated.set(false);
            apply_with(
                &mut Engine(&f),
                &f.root,
                application,
                &mut compose,
                |file, bytes| file.write_all(bytes),
            )
            .unwrap();
            f.running.set(true);
            f.running_changed.set(true);
            assert!(
                verify_with(
                    &mut Engine(&f),
                    &f.root,
                    &identities,
                    Some(&pins),
                    |_, _| panic!("changed shape reached verification")
                )
                .is_err()
            );
            f.running_changed.set(false);
            verify_with(
                &mut Engine(&f),
                &f.root,
                &identities,
                Some(&pins),
                |url, probe| {
                    assert!(loopback(url));
                    assert!(probe == "test-fixture-bearer");
                    Ok(())
                },
            )
            .unwrap();
            f.running.set(false);
            let path = f.root.join(JOURNAL);
            let mut journal = read_journal(&path).unwrap().unwrap();
            let expected = journal.deployment.env_file.clone();
            journal.deployment.env_file = f.root.join("another.env").to_string_lossy().into_owned();
            journal.save(&path).unwrap();
            assert!(
                undo_with(
                    &mut Engine(&f),
                    &f.root,
                    ID,
                    Some(&pins),
                    &mut |_, _, _| panic!("unverified deployment reached Compose")
                )
                .is_err()
            );
            journal.deployment.env_file = expected;
            journal.save(&path).unwrap();
            undo_with(&mut Engine(&f), &f.root, ID, Some(&pins), &mut compose).unwrap();
            assert!(fs::read(f.root.join(".env")).unwrap() == ORIGINAL);
            let mut resumed = pins.clone();
            resumed.daemon = "3".repeat(64);
            resumed.deployment_digest = Some(journal.deployment.digest(&resumed).unwrap());
            assert!(checked_journal(&f.root, Some(&resumed)).unwrap().is_none());
        }
    }

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
        for end in 0..=expected.len() {
            assert!(owns(&expected.as_bytes()[..end]));
        }
        assert!(!owns(changed.as_bytes()));
    }
}
