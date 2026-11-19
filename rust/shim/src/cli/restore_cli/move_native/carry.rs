//! Read-only preflight for a move that would carry the source's bearer
//! settings. Only which settings are supplied crosses SSH. Compose is
//! rendered with fixture values on the target; it never receives a source
//! bearer from a dry run.
use std::{collections::BTreeMap, path::Path, time::Duration};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::super::docker::{self, Docker};
use super::probe::Facts;
use crate::cli::update::seams;

const KEYS: [&str; 4] = [
    "PSEUDOLIFE_MCP_TOKEN",
    "PSEUDOLIFE_MCP_TOKENS",
    "PSEUDOLIFE_MCP_TIER_MAP",
    "PSEUDOLIFE_MCP_TOOLSET",
];
const SAMPLE: [&str; 4] = [
    "move-dry-run-placeholder",
    "move-dry-run-mapped-placeholder:example",
    "example:core",
    "full",
];
const STACK: &str = "the target's running Docker stack cannot be reproduced from its installed Compose files; restore the original project and environment, then retry move --dry-run";
const ENV: &str = "the target's Compose environment is not a private writable file owned by the SSH account; restore the original file and make it owner-only, then retry move --dry-run";
const DAEMON_SERVICE: &str = "pseudolife-daemon";
const PG_SERVICE: &str = "pseudolife-pg";
const INTERPOLATION: &str = "the target's Compose files do not apply the carried identity settings without changing other settings; restore its original identity interpolation and retry move --dry-run";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(in crate::cli) struct TargetPins {
    pub(super) daemon: String,
    pub(super) postgres: String,
}

impl TargetPins {
    pub(super) fn from_facts(facts: &Facts) -> Option<Self> {
        (super::probe::container_id(&facts.daemon_id) && super::probe::container_id(&facts.pg_id))
            .then(|| Self {
                daemon: facts.daemon_id.clone(),
                postgres: facts.pg_id.clone(),
            })
    }

    pub(super) fn inspect(docker: &mut impl Docker, id: &str) -> Result<Value, String> {
        if !super::probe::container_id(id) {
            return Err(TARGET_PIN.into());
        }
        inspect(docker, id)
            .filter(|container| container["Id"] == id)
            .ok_or_else(|| TARGET_PIN.into())
    }
}

const TARGET_PIN: &str = "the target's selected Docker containers could not be verified by immutable ID; restore the original Compose stack and retry move --dry-run";

fn docker_run(docker: &mut impl Docker, args: &[&str]) -> docker::Ran {
    let args: Vec<String> = args.iter().map(|&arg| arg.to_owned()).collect();
    docker.run(&args, Duration::from_secs(30))
}

fn inspect(docker: &mut impl Docker, name: &str) -> Option<Value> {
    let result = docker_run(docker, &["inspect", "--type=container", name]);
    if !result.ok() {
        return None;
    }
    let mut entries = serde_json::from_slice::<Vec<Value>>(&result.stdout).ok()?;
    (entries.len() == 1).then(|| entries.remove(0))
}

fn source_values(container: &Value) -> [String; 4] {
    let env: BTreeMap<&str, &str> = container["Config"]["Env"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter_map(|entry| entry.split_once('='))
        .collect();
    KEYS.map(|key| env.get(key).copied().unwrap_or_default().to_owned())
}

pub(super) fn source(docker: &mut impl Docker, facts: &Facts) -> Result<[bool; 4], String> {
    source_with_token(docker, facts, || {
        crate::credentials::CredentialProvider::from_environment()
            .and_then(|provider| provider.snapshot())
            .map(|snapshot| snapshot.token().map(str::to_owned))
    })
}

pub(super) fn source_with_token(
    docker: &mut impl Docker,
    facts: &Facts,
    credential: impl FnOnce() -> Result<Option<String>, crate::credentials::CredentialError>,
) -> Result<[bool; 4], String> {
    if facts.auth != Some(true) {
        return Err("the source does not report bearer authentication; configure a bearer on the source daemon, then retry move --dry-run".into());
    }
    let container = inspect(docker, facts.daemon_selector())
        .filter(|container| facts.daemon_id.is_empty() || container["Id"] == facts.daemon_id)
        .ok_or("the source daemon's identity settings cannot be read; check Docker access and retry move --dry-run")?;
    let values = source_values(&container);
    let shape = shape(&values)?;
    if !values[0].is_empty() || has_mapped_bearer(&values[1]) {
        return Ok(shape);
    }
    const BEARER: &str = "the stored-principal source needs an active bearer; set PSEUDOLIFE_MCP_TOKEN_FILE to its private file, then retry move --dry-run";
    use sha2::{Digest, Sha256};
    let token = credential()
        .map_err(|_| "the source client could not read its configured bearer; repair the credential settings and retry move --dry-run")?
        .ok_or(BEARER)?;
    let digest = format!("{:x}", Sha256::digest(token.as_bytes()));
    let mut bank = super::super::bank::Bank {
        docker,
        pg: facts.pg_selector().to_owned(),
        daemon: facts.daemon_selector().to_owned(),
    };
    // An authenticated HTTP read can update a principal's last-seen time.
    // Compare against the active hashes with a read-only SQL session instead.
    let hashes = super::probe::query(&mut bank, &facts.user, facts.port, &facts.db,
        "SELECT token_hash FROM principals WHERE revoked_at IS NULL AND token_hash IS NOT NULL")
        .map_err(|_| "the active source principal hashes could not be read; restore the source PostgreSQL role's local access, then retry move --dry-run")?;
    hashes
        .lines()
        .any(|hash| hash == digest)
        .then_some(shape)
        .ok_or_else(|| "the configured bearer does not match an active source principal; select a bearer for an active principal and retry move --dry-run".into())
}

fn has_mapped_bearer(raw: &str) -> bool {
    raw.split(',')
        .filter_map(|entry| entry.rsplit_once(':'))
        .any(|(token, principal)| {
            let principal = principal.trim();
            !token.trim().is_empty()
                && !principal.is_empty()
                && !principal.eq_ignore_ascii_case("default")
                && !principal.eq_ignore_ascii_case("maintainer")
        })
}

fn shape(values: &[String; 4]) -> Result<[bool; 4], String> {
    for (key, value) in KEYS.into_iter().zip(values) {
        if value.chars().any(char::is_control) || value.contains('\'') || value.ends_with('\\') {
            return Err(format!(
                "the source's {key} cannot be written as a literal Compose environment value; pass --no-keep-tokens to plan a move using the target's environment"
            ));
        }
    }
    Ok(std::array::from_fn(|index| !values[index].is_empty()))
}

fn private_env(path: &Path) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        use rustix::fs::{Mode, OFlags};
        use std::os::unix::fs::MetadataExt;
        crate::private_path::check_ancestors(path).map_err(|_| ENV)?;
        // O_PATH obtains an identity for fstat without opening the file for
        // I/O, even if the path unexpectedly names a FIFO or device.
        let fd = rustix::fs::open(
            path,
            OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|_| ENV)?;
        let meta = std::fs::File::from(fd).metadata().map_err(|_| ENV)?;
        if meta.is_file()
            && meta.mode() & 0o677 == 0o600
            && meta.nlink() == 1
            && meta.uid() == rustix::process::geteuid().as_raw()
        {
            return Ok(());
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = path;
    Err(ENV.into())
}

pub(super) fn target(
    docker: &mut docker::Real,
    shape: [bool; 4],
    pins: Option<&TargetPins>,
) -> Result<(), String> {
    let program = docker.program.clone();
    target_with(docker, shape, pins, |deployment, args, env| {
        deployment.compose(&program, args, env)
    })
}

struct Deployment {
    args: Vec<String>,
    tag: String,
    daemon_hash: String,
    pg_hash: String,
}

impl Deployment {
    fn compose(
        &self,
        program: &Path,
        suffix: &[&str],
        overrides: &seams::Environment,
    ) -> Result<String, String> {
        let argv = std::iter::once(program.to_str().ok_or(STACK)?.to_owned())
            .chain(self.args.iter().cloned())
            .chain(suffix.iter().map(|&arg| arg.to_owned()))
            .collect::<Vec<_>>();
        let mut env: seams::Environment = std::env::vars_os()
            .filter_map(|(key, value)| Some((key.into_string().ok()?, value.into_string().ok()?)))
            .filter(|(key, _)| !key.starts_with("PSEUDOLIFE_") && !key.starts_with("COMPOSE_"))
            .collect();
        env.insert("PSEUDOLIFE_IMAGE_TAG".into(), self.tag.clone());
        env.extend(overrides.clone());
        let (code, output) = seams::run_cli(&argv, Some(&env), 120, false);
        // Compose can print environment values even on failure.
        (code == 0).then_some(output).ok_or_else(|| STACK.into())
    }
}

fn target_with(
    docker: &mut impl Docker,
    shape: [bool; 4],
    pins: Option<&TargetPins>,
    mut run: impl FnMut(&Deployment, &[&str], &seams::Environment) -> Result<String, String>,
) -> Result<(), String> {
    use super::super::bank::{DAEMON_CONTAINER, PG_CONTAINER};
    let (daemon, pg) = if let Some(pins) = pins {
        (
            TargetPins::inspect(docker, &pins.daemon)?,
            TargetPins::inspect(docker, &pins.postgres)?,
        )
    } else {
        (
            inspect(docker, DAEMON_CONTAINER).ok_or(STACK)?,
            inspect(docker, PG_CONTAINER).ok_or(STACK)?,
        )
    };
    let labels = &daemon["Config"]["Labels"];
    let pg_labels = &pg["Config"]["Labels"];
    let label = |key: &str| labels[key].as_str().filter(|s| !s.is_empty()).ok_or(STACK);
    let project = label("com.docker.compose.project")?;
    let dir = label("com.docker.compose.project.working_dir")?;
    let files = label("com.docker.compose.project.config_files")?;
    if !Path::new(dir).is_absolute()
        || pg_labels["com.docker.compose.project"] != project
        || labels["com.docker.compose.service"] != DAEMON_SERVICE
        || pg_labels["com.docker.compose.service"] != PG_SERVICE
    {
        return Err(STACK.into());
    }
    let reference = daemon["Config"]["Image"].as_str().ok_or(STACK)?;
    let tag = reference.strip_prefix("ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:")
        .or_else(|| reference.strip_prefix("ghcr.io/pseudogiant-xr/pseudolife-daemon:"))
        .filter(|tag| crate::cli::update::plain_release(tag))
        .ok_or("carrying identities needs the target's pinned release daemon image; pass --no-keep-tokens to retain the existing container")?;
    let env_file = label("com.docker.compose.project.environment_file")
        .map(str::to_owned)
        .unwrap_or_else(|_| format!("{dir}/.env"));
    if env_file.contains(',') || !Path::new(&env_file).is_absolute() {
        return Err(ENV.into());
    }
    private_env(Path::new(&env_file))?;
    let local = docker_run(
        docker,
        &["image", "inspect", "--format", "{{.Id}}", reference],
    );
    let image_id = daemon["Image"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or(STACK)?;
    if !local.ok() || image_id != String::from_utf8_lossy(&local.stdout).trim() {
        return Err("the target's installed daemon tag no longer names its running image; restore that image tag, then retry move --dry-run".into());
    }
    let args = [
        "compose",
        "--project-name",
        project,
        "--project-directory",
        dir,
        "--env-file",
        &env_file,
    ];
    let mut args: Vec<String> = args.into_iter().map(str::to_owned).collect();
    for file in files.split(',') {
        if !Path::new(file).is_absolute() || !Path::new(file).is_file() {
            return Err(STACK.into());
        }
        args.extend(["-f".into(), file.into()]);
    }
    let deployment = Deployment {
        args,
        tag: tag.into(),
        daemon_hash: label("com.docker.compose.config-hash")?.into(),
        pg_hash: pg_labels["com.docker.compose.config-hash"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or(STACK)?
            .into(),
    };
    let mut env = seams::Environment::new();
    deployment.check_hashes(&run(&deployment, &["config", "--hash", "*"], &env)?)?;
    let mut render = |env: &seams::Environment| -> Result<Value, String> {
        let out = run(&deployment, &["config", "--format", "json"], env)?;
        serde_json::from_str(&out).map_err(|_| STACK.into())
    };
    let original = render(&env)?;
    if !mounts_match(&original, DAEMON_SERVICE, &daemon, |path| path == "/data")
        || !mounts_match(&original, PG_SERVICE, &pg, |path| {
            path.starts_with("/var/lib/postgresql")
        })
    {
        return Err(STACK.into());
    }
    for (index, key) in KEYS.into_iter().enumerate() {
        if index == 3 && !shape[index] {
            continue;
        }
        let sample = if shape[index] { SAMPLE[index] } else { "" };
        env.insert(key.into(), sample.into());
    }
    if render(&env)?
        != with_identities(
            original.clone(),
            env.iter()
                .map(|(key, value)| (key.as_str(), value.as_str())),
        )?
    {
        return Err(INTERPOLATION.into());
    }
    Ok(())
}

impl Deployment {
    fn check_hashes(&self, hashes: &str) -> Result<(), String> {
        for (service, hash) in [
            (DAEMON_SERVICE, &self.daemon_hash),
            (PG_SERVICE, &self.pg_hash),
        ] {
            if !hashes
                .lines()
                .any(|line| line.split_ascii_whitespace().eq([service, hash.as_str()]))
            {
                return Err(STACK.into());
            }
        }
        Ok(())
    }
}

fn with_identities<'key, 'value>(
    mut config: Value,
    settings: impl IntoIterator<Item = (&'key str, &'value str)>,
) -> Result<Value, String> {
    let env = config["services"][DAEMON_SERVICE]["environment"]
        .as_object_mut()
        .ok_or(STACK)?;
    env.extend(
        settings
            .into_iter()
            .map(|(key, value)| (key.into(), value.into())),
    );
    Ok(config)
}

fn mounts_match(config: &Value, service: &str, container: &Value, data: fn(&str) -> bool) -> bool {
    let Some(actual) = container["Mounts"].as_array() else {
        return false;
    };
    let mut data_mounts = actual
        .iter()
        .filter(|value| value["Destination"].as_str().is_some_and(data))
        .peekable();
    data_mounts.peek().is_some()
        && data_mounts.all(|actual| {
            config["services"][service]["volumes"]
                .as_array()
                .into_iter()
                .flatten()
                .any(|wanted| {
                    let source = wanted["source"].as_str().unwrap_or_default();
                    wanted["target"] == actual["Destination"]
                        && wanted["type"] == actual["Type"]
                        && match actual["Type"].as_str() {
                            Some("volume") => config["volumes"][source]["name"] == actual["Name"],
                            Some("bind") => source == actual["Source"],
                            _ => false,
                        }
                })
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn carried_settings_reject_a_substitute_at_the_pinned_target_id() {
        struct Replaced {
            selector: String,
            returned: String,
        }
        impl Docker for Replaced {
            fn run_io(
                &mut self,
                argv: &[String],
                _: Duration,
                _: docker::Input<'_>,
                _: docker::Output<'_>,
            ) -> docker::Ran {
                assert_eq!(argv, ["inspect", "--type=container", &self.selector]);
                docker::Ran {
                    stdout: serde_json::to_vec(&serde_json::json!([{"Id": self.returned}]))
                        .unwrap(),
                    ..docker::Ran::default()
                }
            }
        }

        let id = "1".repeat(64);
        let mut docker = Replaced {
            selector: id.clone(),
            returned: "2".repeat(64),
        };
        assert!(TargetPins::inspect(&mut docker, &id).is_err());
        docker.returned = id.clone();
        assert!(TargetPins::inspect(&mut docker, &id).is_ok());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn target_refuses_inconsistent_installed_compose_identity() {
        use crate::cli::restore_cli::bank::{DAEMON_CONTAINER, PG_CONTAINER};
        use serde_json::json;
        use std::fs;

        struct Scratch(std::path::PathBuf);
        impl Drop for Scratch {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        struct Inspect {
            daemon: Value,
            pg: Value,
            image: &'static str,
        }
        impl Docker for Inspect {
            fn run_io(
                &mut self,
                args: &[String],
                _: Duration,
                _: docker::Input<'_>,
                _: docker::Output<'_>,
            ) -> docker::Ran {
                let argv: Vec<_> = args.iter().map(String::as_str).collect();
                let output = match argv.as_slice() {
                    ["inspect", "--type=container", DAEMON_CONTAINER] => {
                        json!([self.daemon]).to_string()
                    }
                    ["inspect", "--type=container", PG_CONTAINER] => json!([self.pg]).to_string(),
                    ["image", "inspect", "--format", "{{.Id}}", _] => self.image.into(),
                    _ => panic!("unexpected Docker command"),
                };
                docker::Ran {
                    stdout: output.into_bytes(),
                    ..docker::Ran::default()
                }
            }
        }
        let path = std::env::temp_dir().join(format!("pl-carry-target-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&path).unwrap();
        let scratch = Scratch(path);
        docker::private_file(&scratch.0.join(".env")).unwrap();
        let compose = scratch.0.join("compose.yml");
        fs::write(&compose, "").unwrap();
        let daemon = json!({
            "Image": "sha256:fixture",
            "Config": {
                "Image":"ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.25.0",
                "Labels": {
                    "com.docker.compose.project": "fixture",
                    "com.docker.compose.project.working_dir": scratch.0,
                    "com.docker.compose.project.config_files": compose,
                    "com.docker.compose.service": DAEMON_SERVICE,
                    "com.docker.compose.config-hash":"daemon-hash",
                },
            },
            "Mounts":[{"Destination":"/data","Type":"volume","Name":"fixture-data"}],
        });
        let pg = json!({
            "Config":{"Labels":{
                "com.docker.compose.project":"fixture",
                "com.docker.compose.service":PG_SERVICE,
                "com.docker.compose.config-hash":"pg-hash",
            }},
            "Mounts":[{"Destination":"/var/lib/postgresql","Type":"volume","Name":"fixture-bank"}],
        });
        let original = json!({
            "services": {
                DAEMON_SERVICE: {"environment":{},"volumes":[{"target":"/data","type":"volume","source":"data"}]},
                PG_SERVICE: {"volumes":[{"target":"/var/lib/postgresql","type":"volume","source":"bank"}]},
            },
            "volumes":{"data":{"name":"fixture-data"},"bank":{"name":"fixture-bank"}},
        });
        for case in [
            "valid",
            "project",
            "daemon service",
            "pg service",
            "tag",
            "image",
            "daemon hash",
            "pg hash",
            "interpolation",
        ] {
            let mut docker = Inspect {
                daemon: daemon.clone(),
                pg: pg.clone(),
                image: "sha256:fixture",
            };
            match case {
                "project" => {
                    docker.pg["Config"]["Labels"]["com.docker.compose.project"] = json!("other")
                }
                "daemon service" => {
                    docker.daemon["Config"]["Labels"]["com.docker.compose.service"] = json!("other")
                }
                "pg service" => {
                    docker.pg["Config"]["Labels"]["com.docker.compose.service"] = json!("other")
                }
                "tag" => {
                    docker.daemon["Config"]["Image"] =
                        json!("ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:latest")
                }
                "image" => docker.image = "sha256:other",
                _ => {}
            }
            let result = target_with(
                &mut docker,
                [true, false, false, false],
                None,
                |_, args, env| {
                    if args == ["config", "--hash", "*"] {
                        let daemon = if case == "daemon hash" {
                            "other"
                        } else {
                            "daemon-hash"
                        };
                        let pg = if case == "pg hash" {
                            "other"
                        } else {
                            "pg-hash"
                        };
                        return Ok(format!("{DAEMON_SERVICE} {daemon}\n{PG_SERVICE} {pg}\n"));
                    }
                    assert_eq!(args, ["config", "--format", "json"]);
                    let mut rendered = original.clone();
                    if case != "interpolation" {
                        for (key, value) in env {
                            rendered["services"][DAEMON_SERVICE]["environment"][key] = json!(value);
                        }
                    }
                    Ok(rendered.to_string())
                },
            );
            assert_eq!(result.is_ok(), case == "valid", "{case}");
        }
    }

    #[test]
    fn target_data_mount_must_name_the_installed_volume() {
        let config = serde_json::json!({
            "services": {"daemon": {"volumes": [{"type":"volume","target":"/data","source":"data"}]}},
            "volumes": {"data": {"name":"fixture-data"}}
        });
        let matches = |name: &str| {
            mounts_match(
                &config,
                "daemon",
                &serde_json::json!({
                    "Mounts": [{"Type":"volume","Destination":"/data","Name":name}]
                }),
                |path| path == "/data",
            )
        };
        assert!(matches("fixture-data"));
        assert!(!matches("other-data"));
    }

    #[test]
    fn a_mapped_bearer_requires_a_token_and_a_non_reserved_principal() {
        assert!(has_mapped_bearer("left:right:fixture"));
        assert!(!has_mapped_bearer(
            "bad, :fixture, some: , other: DEFAULT, last: maintainer"
        ));
    }

    #[test]
    fn only_the_shape_of_the_source_credentials_is_needed_for_preflight() {
        let mut values = [
            "fixture-bearer".into(),
            "".into(),
            "team:$literal".into(),
            "".into(),
        ];
        assert_eq!(shape(&values), Ok([true, false, true, false]));
        values[0] = "fixture'unsafe".into();
        assert!(shape(&values).is_err_and(|why| !why.contains("fixture'unsafe")));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn target_environment_must_be_private_and_unaliased() {
        use std::{
            fs,
            os::unix::fs::{PermissionsExt, symlink},
        };
        let dir = std::env::temp_dir().join(format!("pl-carry-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&dir).unwrap();
        let env = dir.join("env");
        let alias = dir.join("alias");
        docker::private_file(&env).unwrap();
        assert!(private_env(&env).is_ok());
        symlink(&env, &alias).unwrap();
        assert!(private_env(&alias).is_err());
        fs::remove_file(&alias).unwrap();
        fs::hard_link(&env, &alias).unwrap();
        assert!(private_env(&env).is_err());
        fs::remove_file(&alias).unwrap();
        fs::set_permissions(&env, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(private_env(&env).is_err());
        fs::set_permissions(&env, fs::Permissions::from_mode(0o400)).unwrap();
        assert!(private_env(&env).is_err());
        fs::remove_file(&env).unwrap();
        fs::remove_dir(&dir).unwrap();
    }
}
