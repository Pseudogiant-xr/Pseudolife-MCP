//! Read-only preflight for a move that would carry the source's bearer
//! settings. Only which settings are supplied crosses SSH. Compose is
//! rendered with fixture values on the target; it never receives a source
//! bearer from a dry run.
use std::{collections::BTreeMap, fs, path::Path, time::Duration};

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
    if facts.auth != Some(true) {
        return Err("the source does not report bearer authentication; configure a bearer on the source daemon, then retry move --dry-run".into());
    }
    let container = inspect(docker, &facts.daemon)
        .ok_or("the source daemon's identity settings cannot be read; check Docker access and retry move --dry-run")?;
    let values = source_values(&container);
    let shape = shape(&values)?;
    if !values[0].is_empty() || has_mapped_bearer(&values[1]) {
        return Ok(shape);
    }
    const BEARER: &str = "the stored-principal source needs an active bearer; set PSEUDOLIFE_MCP_TOKEN_FILE to its private file, then retry move --dry-run";
    use sha2::{Digest, Sha256};
    let token = crate::credentials::CredentialProvider::from_environment()
        .and_then(|provider| provider.snapshot())
        .map_err(|_| BEARER)?;
    let digest = format!(
        "{:x}",
        Sha256::digest(token.token().ok_or(BEARER)?.as_bytes())
    );
    let mut bank = super::super::bank::Bank {
        docker,
        pg: facts.pg.clone(),
        daemon: facts.daemon.clone(),
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
        .ok_or_else(|| BEARER.into())
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
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
        crate::private_path::check_ancestors(path).map_err(|_| ENV)?;
        let file = fs::OpenOptions::new()
            .read(true)
            .append(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)
            .map_err(|_| ENV)?;
        let meta = file.metadata().map_err(|_| ENV)?;
        if meta.is_file()
            && meta.mode() & 0o077 == 0
            && meta.nlink() == 1
            && meta.uid() == rustix::process::geteuid().as_raw()
        {
            return Ok(());
        }
    }
    #[cfg(not(unix))]
    let _ = (path, fs::OpenOptions::new());
    Err(ENV.into())
}

pub(super) fn target(docker: &mut docker::Real, shape: [bool; 4]) -> Result<(), String> {
    use super::super::bank::{DAEMON_CONTAINER, PG_CONTAINER};
    let daemon = inspect(docker, DAEMON_CONTAINER).ok_or(STACK)?;
    let pg = inspect(docker, PG_CONTAINER).ok_or(STACK)?;
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
    if !local.ok() || daemon["Image"] != String::from_utf8_lossy(&local.stdout).trim() {
        return Err("the target's installed daemon tag no longer names its running image; restore that image tag, then retry move --dry-run".into());
    }
    let args = [
        docker.program.to_str().ok_or(STACK)?,
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
    let mut env: seams::Environment = std::env::vars()
        .filter(|(key, _)| !key.starts_with("PSEUDOLIFE_") && !key.starts_with("COMPOSE_"))
        .collect();
    env.insert("PSEUDOLIFE_IMAGE_TAG".into(), tag.into());
    let run = |suffix: &[&str], env: &seams::Environment| {
        let argv: Vec<String> = args
            .iter()
            .cloned()
            .chain(suffix.iter().map(|&arg| arg.to_owned()))
            .collect();
        let (code, output) = seams::run_cli(&argv, Some(env), 120, false);
        (code == 0).then_some(output).ok_or(STACK)
    };
    let hashes = run(&["config", "--hash", "*"], &env)?;
    for (service, labels) in [(DAEMON_SERVICE, labels), (PG_SERVICE, pg_labels)] {
        let hash = labels["com.docker.compose.config-hash"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or(STACK)?;
        if !hashes
            .lines()
            .any(|line| line.split_ascii_whitespace().eq([service, hash]))
        {
            return Err(STACK.into());
        }
    }
    let render = |env: &seams::Environment| -> Result<Value, String> {
        let out = run(&["config", "--format", "json"], env)?;
        serde_json::from_str(&out).map_err(|_| STACK.into())
    };
    let mut original = render(&env)?;
    if !mounts_match(&original, DAEMON_SERVICE, &daemon, |path| path == "/data")
        || !mounts_match(&original, PG_SERVICE, &pg, |path| {
            path.starts_with("/var/lib/postgresql")
        })
    {
        return Err(STACK.into());
    }
    let daemon_env = original["services"][DAEMON_SERVICE]["environment"]
        .as_object_mut()
        .ok_or(STACK)?;
    for (index, key) in KEYS.into_iter().enumerate() {
        if index == 3 && !shape[index] {
            continue;
        }
        let sample = if shape[index] { SAMPLE[index] } else { "" };
        daemon_env.insert(key.into(), sample.into());
        env.insert(key.into(), sample.into());
    }
    if render(&env)? != original {
        return Err("the target's Compose files do not apply the carried identity settings without changing other settings; restore its original identity interpolation and retry move --dry-run".into());
    }
    Ok(())
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

    #[cfg(unix)]
    #[test]
    fn target_environment_must_be_private_and_unaliased() {
        use std::os::unix::fs::{PermissionsExt, symlink};
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
        fs::remove_file(&env).unwrap();
        fs::remove_dir(&dir).unwrap();
    }
}
