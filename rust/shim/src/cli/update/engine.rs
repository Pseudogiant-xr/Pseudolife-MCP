use super::*;

pub(super) const SELECTION: &str = "data/update-engine/docker-compose.engine.json";
pub(super) const PYTHON_ACCEPTED: &str = "data/update-engine/docker-compose.python-accepted.json";
pub(super) const ENGINE_LABEL: &str = "org.pseudolife.engine";
pub(super) const CHECKOUT_LABEL: &str = "org.pseudolife.checkout";
pub(super) const ACTIVATION_LABEL: &str = "org.pseudolife.activation";
pub(super) const RELEASE_LABEL: &str = "org.pseudolife.release";
pub(super) fn release_accepted() -> PathBuf {
    data_dir().join("update-engine/docker-compose.release-accepted.json")
}
pub(super) fn selection(path: &Path) -> Result<Option<Value>> {
    match fs::read(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
            .ok()
            .and_then(|v| v["services"].get(DAEMON_SERVICE).cloned())
            .filter(|s| matches!(s["labels"][ENGINE_LABEL].as_str(), Some("rust" | "python")))
            .map(Some)
            .ok_or_else(|| {
                UpdateError::usage(format!(
                    "invalid engine selection at {}; restore its rollback copy before updating",
                    path.display()
                ))
            }),
        Err(_) => Err(UpdateError::usage(
            "cannot read engine selection; restore its rollback copy before updating",
        )),
    }
}
pub(super) fn validated(labels: &Value) -> Result<Value> {
    let engine = labels[ENGINE_LABEL].as_str().unwrap_or("python");
    let path = if labels[RELEASE_LABEL].as_str().is_some() {
        Some(release_accepted())
    } else {
        labels[CHECKOUT_LABEL].as_str().map(|p| {
            Path::new(p).join(if engine == "rust" {
                SELECTION
            } else {
                PYTHON_ACCEPTED
            })
        })
    };
    if let Some(path) = path
        && let Some(selected) = selection(&path)?
        && selected["labels"][ENGINE_LABEL] == engine
        && selected["labels"][ACTIVATION_LABEL].as_str().is_some()
        && selected["labels"][ACTIVATION_LABEL] == labels[ACTIVATION_LABEL]
        && (labels[RELEASE_LABEL].is_null()
            || selected["labels"][RELEASE_LABEL] == labels[RELEASE_LABEL])
    {
        return Ok(selected);
    }
    Err(UpdateError::usage(format!(
        "the running {engine} container has no valid accepted activation; use the previous update's rollback commands (the Python rollback for a first activation) before retrying; nothing was changed"
    )))
}
pub(super) fn host_labels(u: &Update, container: &str) -> Value {
    let (code, out) = u.docker_call(&["inspect", "-f", "{{json .Config.Labels}}", container]);
    if code == 0 {
        serde_json::from_str::<Value>(out.trim())
            .ok()
            .filter(Value::is_object)
            .unwrap_or_else(|| json!({}))
    } else {
        json!({})
    }
}
pub(super) fn selected_checkout(u: &mut Update) -> Result<Option<PathBuf>> {
    let mut container = DAEMON_CONTAINER.to_owned();
    let mut selected = None;
    let repo =
        u.o.checkout
            .as_ref()
            .map(|p| seams::command_path(fs::canonicalize(p).unwrap_or_else(|_| p.clone())));
    if let Some(repo) = &repo {
        selected = selection(&repo.join(SELECTION))?;
        if u.o.engine.is_some() {
            return Ok(Some(repo.clone()));
        }
        if !u.o.allow_dirty && tree_state(repo).dirty != "false" {
            return Ok(None);
        }
        let env_file = repo.join("ops/.env");
        let compose = compose_args(
            &[
                repo.join("ops/docker-compose.yml"),
                repo.join("ops/docker-compose.override.yml"),
            ],
            std::slice::from_ref(&env_file),
        );
        let env = compose_environment(&[env_file], &Environment::new());
        let mut args = vec!["compose".into()];
        args.extend(compose);
        args.extend(["ps".into(), "-a".into(), "-q".into(), DAEMON_SERVICE.into()]);
        let (code, out) = u.command(&args, Some(&env), 60, false);
        let ids: Vec<_> = out.split_whitespace().collect();
        if code == 0 && ids.len() == 1 {
            container = ids[0].into();
        } else {
            return Err(UpdateError::usage(
                "cannot find this checkout's daemon; restore its Compose project and retry; nothing was changed",
            ));
        }
    }
    let labels = host_labels(u, &container);
    let current = labels[ENGINE_LABEL].as_str().unwrap_or("python");
    if current == "rust" || truthy(&labels[ACTIVATION_LABEL]) {
        validated(&labels)?;
    }
    if let Some(selected) = selected
        && selected["labels"][ENGINE_LABEL] != current
    {
        return Err(UpdateError::usage(
            "this checkout's engine selection disagrees with the host; use --engine python --checkout <repo> to restore the default, or --engine rust --checkout <repo> to explicitly activate Rust; nothing was changed",
        ));
    }
    if current != "rust" || u.o.clients_only {
        if u.o.allow_auth_change
            && let Some(repo) = repo
        {
            u.selected_engine = "python".into();
            return Ok(Some(repo));
        }
        return Ok(None);
    }
    u.selected_engine = current.into();
    if let Some(repo) = repo {
        return Ok(Some(repo));
    }
    if labels[RELEASE_LABEL].as_str().is_some() {
        return Ok(None);
    }
    if u.o.tag.is_some() || u.o.reinstall || u.o.allow_downgrade || u.o.env_file.is_some() {
        return Err(UpdateError::usage(
            "this host selected a checkout engine; use update --checkout <repo> to keep it, or --engine python --checkout <repo> to switch back; nothing was changed",
        ));
    }
    Ok(labels[CHECKOUT_LABEL].as_str().map(PathBuf::from))
}
pub(super) fn write_overlay(path: &Path, service: &Value) -> Result<()> {
    let body =
        serde_json::to_vec_pretty(&json!({"services":{DAEMON_SERVICE:service}})).expect("overlay");
    // Python-created overlays can be readable by other users. Replace their
    // directory entry with a private staged file rather than opening them as
    // private board state or changing the old file's permissions in place.
    if let Ok(metadata) = fs::symlink_metadata(path)
        && !metadata.is_file()
    {
        return Err(UpdateError::usage(
            "the engine overlay is not a regular file; restore its original file before retrying",
        ));
    }
    let staged = path.with_file_name(format!(".engine-overlay-{}", uuid::Uuid::new_v4().simple()));
    seams::write_atomic(&staged, &body)?;
    let result = (|| -> std::io::Result<()> {
        fs::rename(&staged, path)?;
        #[cfg(unix)]
        std::fs::File::open(path.parent().expect("overlay directory"))?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(&staged);
    result.map_err(|_| {
        UpdateError::new(format!(
            "cannot replace the engine overlay; restore write access to {} and retry",
            path.parent().expect("overlay directory").display()
        ))
    })
}
pub(super) fn build_environment(tree: &seams::Tree) -> Environment {
    [
        ("PSEUDOLIFE_BUILD_GIT_SHA".into(), tree.sha.clone()),
        ("PSEUDOLIFE_BUILD_DIRTY".into(), tree.dirty.clone()),
        (
            "PSEUDOLIFE_BUILD_TIME".into(),
            chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        ),
    ]
    .into_iter()
    .collect()
}
pub(super) fn checkout_version_schema(repo: &Path) -> Result<(String, i64)> {
    let project = fs::read_to_string(repo.join("pyproject.toml"))?;
    let schema = fs::read_to_string(repo.join("pseudolife_memory/storage/schema.py"))?;
    let version = regex::Regex::new(r#"(?m)^version\s*=\s*"([^"]+)""#)
        .expect("version")
        .captures(&project)
        .map(|c| c[1].to_owned())
        .ok_or_else(|| {
            UpdateError::usage("cannot read the checkout version from pyproject.toml")
        })?;
    let schema = regex::Regex::new(r"(?m)^SCHEMA_META_VERSION\s*=\s*([0-9]+)")
        .expect("schema")
        .captures(&schema)
        .and_then(|c| c[1].parse::<i64>().ok())
        .ok_or_else(|| UpdateError::usage("cannot read the checkout schema version"))?;
    Ok((version, schema))
}
pub(super) fn daemon_token(config: &Value) -> Result<String> {
    let env = &config["services"][DAEMON_SERVICE]["environment"];
    let token = env["PSEUDOLIFE_MCP_TOKEN"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .or_else(|| {
            env["PSEUDOLIFE_MCP_TOKENS"].as_str().and_then(|raw| {
                raw.split(',').find_map(|entry| {
                    let (token, principal) = entry.rsplit_once(':')?;
                    let (token, principal) = (token.trim(), principal.trim().to_lowercase());
                    (!token.is_empty()
                        && !principal.is_empty()
                        && !matches!(principal.as_str(), "default" | "maintainer"))
                    .then(|| token.to_owned())
                })
            })
        });
    token.filter(|s|!s.contains(['\r','\n'])).ok_or_else(||UpdateError::usage("the engine health gate needs bearer auth; set PSEUDOLIFE_MCP_TOKEN in the host env file and retry; nothing was changed"))
}
pub(super) fn verify_identity(
    u: &Update,
    config: &Value,
    running: &Value,
    url: &str,
) -> Result<()> {
    verify_bank_identity(u, config, running)?;
    verify_endpoint(config, url)
}
pub(super) fn verify_checkout_identity(
    u: &Update,
    config: &Value,
    running: &Value,
    daemon: &str,
) -> Result<()> {
    let project = config["name"].as_str().filter(|name| !name.is_empty());
    let labels = &running["Config"]["Labels"];
    if running["Id"] != daemon
        || project.is_none()
        || project != labels["com.docker.compose.project"].as_str()
        || labels["com.docker.compose.service"] != DAEMON_SERVICE
    {
        return Err(UpdateError::usage(
            "the daemon does not belong to this checkout's Compose project; restore its project files and container before updating; nothing was deployed",
        ));
    }
    verify_bank_identity(u, config, running)
}
fn bind_source(source: &str) -> String {
    let slashes = source.replace('\\', "/");
    let path = slashes
        .strip_prefix("//./")
        .or_else(|| slashes.strip_prefix("//?/"))
        .unwrap_or(&slashes);
    if let Some(host) = path.strip_prefix("/run/desktop/mnt/host/")
        && host.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
        && host.as_bytes().get(1) == Some(&b'/')
    {
        return format!("{}:{}", &host[..1], &host[1..]).to_ascii_lowercase();
    }
    if path.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
        && path.as_bytes().get(1..3) == Some(b":/")
    {
        return path.to_ascii_lowercase();
    }
    if let Some(unc) = path
        .strip_prefix("UNC/")
        .or_else(|| path.strip_prefix("unc/"))
    {
        return format!("//{unc}").to_ascii_lowercase();
    }
    if slashes.starts_with("//") {
        return slashes.to_ascii_lowercase();
    }
    // Preserve POSIX case and backslashes: they can name distinct files.
    source.to_owned()
}
fn same_bind_source(actual: &str, expected: &str) -> bool {
    let windows = |s: &str| {
        s.starts_with(r"\\")
            || s.starts_with("/run/desktop/mnt/host/")
            || (s.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
                && s.as_bytes().get(1) == Some(&b':'))
            || (cfg!(windows) && s.starts_with("//"))
    };
    if windows(actual) || windows(expected) {
        bind_source(actual) == bind_source(expected)
    } else {
        actual == expected
    }
}
fn ephemeral_mount(
    config: &Value,
    running: &Value,
    mount: &Value,
    volume_inspect: &mut impl FnMut(&str) -> Option<Value>,
) -> Result<bool> {
    if mount["Type"] == "tmpfs" {
        return Ok(true);
    }
    if mount["Type"] != "volume" {
        return Ok(false);
    }
    let refusal = |cause: &str| {
        UpdateError::usage(format!(
            "cannot verify anonymous volume provenance at {}: {cause}; declare the volume as a named Compose volume to restore its persistent mount, or recreate the anonymous mount with a current Docker engine; nothing was changed",
            mount["Destination"]
                .as_str()
                .unwrap_or("an unreadable destination"),
        ))
    };
    let name = mount["Name"]
        .as_str()
        .filter(|name| !name.is_empty())
        .ok_or_else(|| refusal("the running mount has no volume Name"))?;
    let declared = config["volumes"]
        .as_object()
        .filter(|volumes| {
            volumes
                .values()
                .all(|v| v["name"].as_str().is_some_and(|name| !name.is_empty()))
        })
        .ok_or_else(|| refusal("the project's named volumes could not be resolved"))?;
    if declared.values().any(|v| v["name"] == name) {
        return Ok(false);
    }
    let configured = running["HostConfig"]["Mounts"]
        .as_array()
        .is_some_and(|mounts| {
            mounts.iter().any(|m| {
                m["Target"] == mount["Destination"]
                    && m["Type"] == "volume"
                    && (m["Source"].is_null() || m["Source"] == "" || m["Source"] == name)
            })
        });
    if !configured {
        return Err(refusal(
            "the running mount configuration is missing or disagrees with its volume Name",
        ));
    }
    // Generated names and absence from the new Compose configuration cannot
    // distinguish an anonymous volume from an external persistent volume.
    let inspected = volume_inspect(name)
        .ok_or_else(|| refusal("docker volume inspect failed or returned unreadable metadata"))?;
    let record = inspected
        .as_array()
        .filter(|a| a.len() == 1)
        .and_then(|a| a.first())
        .filter(|v| v["Name"] == name)
        .ok_or_else(|| refusal("docker volume inspect did not return the matching volume"))?;
    if !record["Labels"]["com.docker.volume.anonymous"].is_string() {
        return Err(refusal(
            "docker volume inspect did not report the com.docker.volume.anonymous label",
        ));
    }
    Ok(true)
}

fn verify_compose_bearers(configured: &Value, running: &Value, allow_change: bool) -> Result<()> {
    let unreadable = || {
        UpdateError::usage(
            "cannot verify the daemon's Compose bearer settings; restore the original compose and env files, then retry. Nothing was changed",
        )
    };
    let configured = configured.as_object().ok_or_else(unreadable)?;
    let entries = running.as_array().ok_or_else(unreadable)?;
    let mut current = serde_json::Map::new();
    for entry in entries {
        let (name, value) = entry
            .as_str()
            .and_then(|s| s.split_once('='))
            .ok_or_else(unreadable)?;
        current.insert(name.into(), json!(value));
    }
    let identity = |env: &serde_json::Map<String, Value>| -> Result<_> {
        let read = |name| match env.get(name) {
            None | Some(Value::Null) => Ok(""),
            Some(Value::String(value)) => Ok(value.as_str()),
            _ => Err(unreadable()),
        };
        let token = read("PSEUDOLIFE_MCP_TOKEN")?;
        let raw = read("PSEUDOLIFE_MCP_TOKENS")?;
        let mut mapped = std::collections::BTreeMap::new();
        for part in raw.split(',') {
            if let Some((token, principal)) = part.rsplit_once(':') {
                let (token, principal) = (token.trim(), principal.trim().to_lowercase());
                if !token.is_empty()
                    && !principal.is_empty()
                    && !matches!(principal.as_str(), "default" | "maintainer")
                {
                    mapped.entry(token.to_owned()).or_insert(principal);
                }
            }
        }
        let invalid = if !raw.trim().is_empty() && mapped.is_empty() {
            raw.to_owned()
        } else {
            String::new()
        };
        Ok((token.to_owned(), mapped, invalid))
    };
    let expected = identity(configured)?;
    if expected != identity(&current)? && (!allow_change || !expected.2.is_empty()) {
        return Err(UpdateError::usage(
            "the Compose configuration would change the running daemon's bearer settings. Restore its original bearer settings in the project's env file (release mode: --env-file <original-file>), then retry. Nothing was changed",
        ));
    }
    Ok(())
}

pub(super) fn verify_bank_identity(u: &Update, config: &Value, running: &Value) -> Result<()> {
    if u.o.allow_auth_change && u.o.checkout.is_some() && running["State"]["Running"] != false {
        return Err(UpdateError::usage(
            "checkout --allow-auth-change requires a stopped daemon; stop the target daemon after restoring it, then retry; nothing was changed",
        ));
    }
    verify_compose_bearers(
        &config["services"][DAEMON_SERVICE]["environment"],
        &running["Config"]["Env"],
        u.o.allow_auth_change,
    )?;
    verify_bank_identity_with(config, running, |name| {
        let (code, out) = u.docker_call(&["volume", "inspect", name]);
        if code != 0 {
            return None;
        }
        serde_json::from_str(&out).ok()
    })
}

pub(super) fn verify_bank_identity_with(
    config: &Value,
    running: &Value,
    mut volume_inspect: impl FnMut(&str) -> Option<Value>,
) -> Result<()> {
    let service = &config["services"][DAEMON_SERVICE];
    let env = &service["environment"];
    let expected = env["PSEUDOLIFE_MCP_DATABASE_URL"].as_str();
    let actual = running["Config"]["Env"].as_array().and_then(|a| {
        a.iter()
            .filter_map(Value::as_str)
            .find_map(|s| s.strip_prefix("PSEUDOLIFE_MCP_DATABASE_URL="))
    });
    if expected.is_none() || expected != actual {
        return Err(UpdateError::usage(
            "the host Compose configuration names a different bank from the running daemon; restore its original env file before switching; nothing was changed",
        ));
    }
    let data = service["volumes"].as_array().and_then(|a| {
        a.iter()
            .find(|v| v["target"] == "/data" && v["type"] == "volume")
    });
    let expected = data
        .and_then(|v| v["source"].as_str())
        .and_then(|source| config["volumes"][source]["name"].as_str());
    let actual = running["Mounts"]
        .as_array()
        .and_then(|a| a.iter().find(|v| v["Destination"] == "/data"))
        .and_then(|v| v["Name"].as_str());
    if expected.is_none() || expected != actual {
        return Err(UpdateError::usage(
            "the host Compose configuration must keep the running daemon's named /data volume; restore its volume setting and retry; nothing was changed",
        ));
    }
    // Retained overrides may include relative bind mounts. Compare the
    // resolved sources, destinations and write access, not just /data.
    let mounts = running["Mounts"].as_array().ok_or_else(|| UpdateError::usage(
        "cannot read the daemon's mounts; restore its Compose project and retry; nothing was changed"
    ))?;
    let mut volumes = service["volumes"].as_array().cloned().unwrap_or_default();
    // Compose retains service-level tmpfs separately from long-form volumes.
    // Include both forms in the same persistent-mount checks.
    if !service["tmpfs"].is_null() {
        let unreadable = || {
            UpdateError::usage(
                "cannot read the daemon's Compose tmpfs destinations; restore its tmpfs configuration before updating; nothing was changed",
            )
        };
        for entry in service["tmpfs"].as_array().ok_or_else(unreadable)? {
            let target = entry
                .as_str()
                .and_then(|value| value.split(':').next())
                .filter(|target| target.starts_with('/'))
                .ok_or_else(unreadable)?;
            volumes.push(json!({"type": "tmpfs", "target": target}));
        }
    }
    for volume in &volumes {
        let target = volume["target"].as_str();
        let actual = mounts.iter().find(|m| m["Destination"].as_str() == target);
        // A new destination must not hide data below an existing mount, or
        // obscure that mount by introducing a new ancestor. Compare path
        // components so /data-other remains a legitimate new destination.
        if target.is_some_and(|target| !target.is_empty()) && actual.is_none() {
            let new = mount_components(target.unwrap());
            for mount in mounts.iter().filter(|m| m["Type"] != "tmpfs") {
                let old = mount["Destination"].as_str().ok_or_else(|| UpdateError::usage(
                    "cannot read an existing mount destination; restore its Compose mount metadata before updating; nothing was changed"
                ))?;
                let old = mount_components(old);
                if new.starts_with(&old) || old.starts_with(&new) {
                    return Err(UpdateError::usage(
                        "a new Compose mount overlaps an existing persistent destination; remove the new overlapping mount before updating; nothing was changed",
                    ));
                }
            }
            continue;
        }
        if volume["type"] == "tmpfs" || volume["source"].as_str().is_none_or(str::is_empty) {
            if let Some(mount) = actual
                && !ephemeral_mount(config, running, mount, &mut volume_inspect)?
            {
                return Err(UpdateError::usage(format!(
                    "the Compose configuration replaces the persistent mount at {} with an ephemeral mount; restore its original mount type, source and access before updating; nothing was changed",
                    target.unwrap_or("an unreadable destination"),
                )));
            }
            continue;
        }
        let expected_source = if volume["type"] == "volume" {
            volume["source"]
                .as_str()
                .and_then(|s| config["volumes"][s]["name"].as_str())
        } else {
            volume["source"].as_str()
        };
        let same = actual.is_some_and(|m| {
            let source = if volume["type"] == "volume" {
                m["Name"].as_str()
            } else {
                m["Source"].as_str()
            };
            let same_source = match (source, expected_source) {
                (Some(actual), Some(expected)) if volume["type"] == "bind" => {
                    same_bind_source(actual, expected)
                }
                (Some(actual), Some(expected)) => actual == expected,
                _ => false,
            };
            same_source
                && m["Type"] == volume["type"]
                && m["RW"].as_bool() == Some(volume["read_only"] != true)
        });
        if !same {
            return Err(UpdateError::usage(
                "the Compose configuration changes a daemon mount; restore its original project directory, mount source and access before updating; nothing was changed",
            ));
        }
    }
    for mount in mounts {
        if mount["Type"] == "tmpfs"
            || (mount["Source"].as_str().is_none_or(str::is_empty)
                && mount["Name"].as_str().is_none_or(str::is_empty))
        {
            continue;
        }
        if !volumes.iter().any(|v| v["target"] == mount["Destination"]) {
            return Err(UpdateError::usage(
                "the Compose configuration drops a daemon mount; restore its original mounts before updating; nothing was changed",
            ));
        }
    }
    Ok(())
}
fn mount_components(path: &str) -> Vec<&str> {
    let mut parts = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => (),
            ".." => {
                parts.pop();
            }
            _ => parts.push(part),
        }
    }
    parts
}
fn verify_endpoint(config: &Value, url: &str) -> Result<()> {
    let service = &config["services"][DAEMON_SERVICE];
    let port = service["ports"]
        .as_array()
        .and_then(|a| a.iter().find(|p| p["target"] == 8765));
    let endpoint = reqwest::Url::parse(url).ok();
    let published = port.map(|p| {
        p["published"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| p["published"].to_string())
    });
    if port.is_none_or(|p| p["host_ip"] != "127.0.0.1")
        || endpoint.as_ref().is_none_or(|u| {
            u.scheme() != "http"
                || !matches!(u.host_str(), Some("localhost" | "127.0.0.1"))
                || u.port_or_known_default().map(|p| p.to_string()) != published
        })
    {
        return Err(UpdateError::usage(format!(
            "engine verification must reach this project's loopback port; use --daemon-url http://127.0.0.1:{}; nothing was changed",
            published.unwrap_or_else(|| "8765".into())
        )));
    }
    Ok(())
}
pub(super) fn require_imported_with(
    u: &Update,
    pg: &str,
    user: &str,
    db: &str,
    daemon: &str,
) -> Result<()> {
    let schema = sql(
        u,
        pg,
        user,
        db,
        "SELECT value::text FROM meta WHERE key = 'schema_version'",
    )?
    .trim_matches('"')
    .parse::<i64>()
    .ok();
    let pending = if schema.is_some_and(|v| v >= 57) {
        let row = sql(
            u,
            pg,
            user,
            db,
            "SELECT EXISTS (SELECT 1 FROM cms_counters WHERE id = 1)",
        )?;
        row!="t"&&u.docker_call(&["exec",daemon,"sh","-c","test ! -e /data/memory_state/weights.pt && test ! -e /data/memory_state/weights.pt.bak"]).0!=0
    } else {
        true
    };
    if schema.is_none_or(|s| s < 57) || pending {
        return Err(UpdateError::usage(
            "Rust activation requires schema v57 or later and imported counters: run update --engine python --checkout <repo>, let the Python daemon import memory_state/weights.pt into cms_counters, then retry; nothing was changed",
        ));
    }
    Ok(())
}
pub(super) fn sql(u: &Update, pg: &str, user: &str, db: &str, query: &str) -> Result<String> {
    let args = sql_args(u, pg, user, db, query);
    let borrowed: Vec<_> = args.iter().map(String::as_str).collect();
    let (code, out) = u.docker_call(&borrowed);
    if code == 0 {
        Ok(out.trim().into())
    } else {
        Err(UpdateError::usage(
            "cannot check the bank schema; start the project's PostgreSQL and retry; nothing was changed",
        ))
    }
}
fn sql_args(u: &Update, pg: &str, user: &str, db: &str, query: &str) -> Vec<String> {
    let database = if u.stopped_handover {
        backup::database_arg(db)
    } else {
        db.to_owned()
    };
    let mut args = vec!["exec", pg];
    let port = u.handover_port.to_string();
    if u.stopped_handover {
        args.extend([
            "env",
            "-u",
            "PGHOSTADDR",
            "-u",
            "PGSERVICE",
            "-u",
            "PGSERVICEFILE",
        ]);
    }
    args.extend(["psql", "-X", "-v", "ON_ERROR_STOP=1"]);
    if u.stopped_handover {
        args.extend(["-h", "/var/run/postgresql", "-p", &port]);
    }
    args.extend(["-U", user, "-d", &database, "-Atc", query]);
    args.into_iter().map(str::to_owned).collect()
}
pub(super) fn container(
    u: &Update,
    base: &[String],
    env: Option<&Environment>,
    service: &str,
) -> Result<String> {
    let mut args = vec!["compose".into()];
    args.extend_from_slice(base);
    args.extend(["ps".into(), "-a".into(), "-q".into(), service.into()]);
    let (code, out) = u.command(&args, env, 60, false);
    let ids: Vec<_> = out.split_whitespace().collect();
    if code == 0 && ids.len() == 1 {
        Ok(ids[0].into())
    } else {
        Err(UpdateError::usage(format!(
            "the project needs one existing {service} container; restore that service in this Compose project before updating; nothing was changed"
        )))
    }
}
pub(super) fn inspect(u: &Update, container: &str) -> Result<Value> {
    let (code, out) = u.docker_call(&["inspect", container]);
    serde_json::from_str::<Value>(&out)
        .ok()
        .and_then(|v| v.as_array().and_then(|a| a.first()).cloned())
        .filter(|_| code == 0)
        .ok_or_else(|| {
            UpdateError::usage("cannot inspect the project's running daemon; start it and retry")
        })
}
pub(super) fn tag_validated(u: &mut Update, id: &str, rollback: &str) -> Result<()> {
    let (code, out) = u.docker_call(&["image", "inspect", "-f", "{{.Id}}", rollback]);
    if code == 0 && !out.trim().is_empty() && out.trim() != id {
        if !u.o.force_rollback_tag {
            return Err(UpdateError::usage(
                "that rollback tag already names another image; choose a new --rollback-tag suffix or use --force-rollback-tag to replace it with the validated running image; nothing was deployed",
            ));
        }
        u.warn("--force-rollback-tag: replacing the existing rollback tag with the validated running image");
    }
    if u.docker_call(&["tag", id, rollback]).0 != 0 {
        return Err(UpdateError::new(
            "could not tag the running image for rollback; nothing was deployed",
        ));
    }
    Ok(())
}
pub(super) fn rollback_service(running: &Value, rollback: &str, repo: Option<&Path>) -> Value {
    let labels = &running["Config"]["Labels"];
    let old = labels[ENGINE_LABEL].as_str().unwrap_or("python");
    let mut service = json!({"image":rollback,"labels":{ENGINE_LABEL:old}});
    for key in [CHECKOUT_LABEL, ACTIVATION_LABEL, RELEASE_LABEL] {
        if let Some(value) = labels[key].as_str() {
            service["labels"][key] = json!(value);
        }
    }
    if let Some(repo) = repo {
        service["labels"][CHECKOUT_LABEL] = json!(repo);
        service["build"] = json!({"dockerfile":format!("ops/Dockerfile.daemon{}",if old=="rust"{"-rust"}else{""})});
    }
    service
}
pub(super) fn rollback_commands(
    base: &[String],
    env_files: &[PathBuf],
    overlay: &Path,
    accepted: &Path,
    clear_selection: Option<&Path>,
    image_tag: Option<&str>,
) -> Vec<String> {
    let mut args = base.to_vec();
    args.extend(["-f".into(), overlay.to_string_lossy().into_owned()]);
    let mut restore = format!(
        "docker compose {} up -d --no-deps --no-build --pull never {DAEMON_SERVICE}",
        display(&args)
    );
    // Compose interpolates every base file before merging the rollback overlay.
    // Release Rust compose requires a tag even though the overlay selects the
    // locally retained image and recreation explicitly forbids pulling.
    if let Some(tag) = image_tag {
        restore = if cfg!(windows) {
            format!("$env:PSEUDOLIFE_IMAGE_TAG={}; {restore}", seams::quote(tag))
        } else {
            format!("PSEUDOLIFE_IMAGE_TAG={} {restore}", seams::quote(tag))
        };
    }
    let copy = if cfg!(windows) {
        format!(
            "Copy-Item -LiteralPath {} -Destination {} -ErrorAction Stop",
            seams::quote(&overlay.to_string_lossy()),
            seams::quote(&accepted.to_string_lossy())
        )
    } else {
        format!(
            "cp -- {} {}",
            seams::quote(&overlay.to_string_lossy()),
            seams::quote(&accepted.to_string_lossy())
        )
    };
    let mut commit = copy;
    if let Some(path) = clear_selection {
        commit.push_str(&if cfg!(windows) {
            format!(
                "; Remove-Item -LiteralPath {} -ErrorAction SilentlyContinue",
                seams::quote(&path.to_string_lossy())
            )
        } else {
            format!(" && rm -f -- {}", seams::quote(&path.to_string_lossy()))
        });
    }
    restore = if cfg!(windows) {
        format!(
            "{restore}; if ($LASTEXITCODE -eq 0) {{ {commit} }} else {{ throw 'Rollback recreation failed; accepted engine selection was not changed' }}"
        )
    } else {
        format!("{restore} && {commit}")
    };
    if seams::env_file_sets_credentials(env_files) {
        vec![if cfg!(windows){"Remove-Item Env:PSEUDOLIFE_MCP_TOKEN,Env:PSEUDOLIFE_MCP_TOKENS -ErrorAction SilentlyContinue"}else{"unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS"}.into(),restore]
    } else {
        vec![if cfg!(windows) {
            format!(
                "if ($env:PSEUDOLIFE_MCP_TOKEN -or $env:PSEUDOLIFE_MCP_TOKENS) {{ {restore} }} else {{ throw 'Restore PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKENS in this shell before rollback' }}"
            )
        } else {
            format!(
                "if [ -n \"${{PSEUDOLIFE_MCP_TOKEN:-}}${{PSEUDOLIFE_MCP_TOKENS:-}}\" ]; then {restore}; else echo 'Restore PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKENS in this shell before rollback' >&2; false; fi"
            )
        }]
    }
}

fn verify_stopped_database(
    config: &Value,
    running: &Value,
    pg: &Value,
    user: &str,
    db: &str,
    port: u16,
) -> Result<()> {
    let refusal = || {
        UpdateError::usage(
            "the restored bank cannot be matched to this project's PostgreSQL; restore its original database configuration before retrying; nothing was deployed",
        )
    };
    let declared = config["services"][DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"]
        .as_str()
        .ok_or_else(refusal)?;
    let actual = running["Config"]["Env"].as_array().and_then(|env| {
        env.iter()
            .filter_map(Value::as_str)
            .find_map(|item| item.strip_prefix("PSEUDOLIFE_MCP_DATABASE_URL="))
    });
    if pg["State"]["Running"] != true
        || actual != Some(declared)
        || backup::connection_details(running, pg) != Some((user.to_owned(), db.to_owned(), port))
    {
        return Err(refusal());
    }
    Ok(())
}

fn stored_probe_token(u: &Update, pg: &str, user: &str, db: &str) -> Result<String> {
    use sha2::{Digest, Sha256};
    let token = crate::credentials::CredentialProvider::from_environment()
        .and_then(|provider| provider.snapshot())
        .ok()
        .and_then(|snapshot| snapshot.token().map(str::to_owned));
    let hashes = sql(
        u,
        pg,
        user,
        db,
        "SELECT token_hash FROM principals WHERE revoked_at IS NULL AND token_hash IS NOT NULL",
    )?;
    if let Some(token) = token
        && hashes
            .lines()
            .any(|hash| hash == format!("{:x}", Sha256::digest(token.as_bytes())))
    {
        return Ok(token);
    }
    Err(UpdateError::usage(
        "stored-principal verification needs a carried bearer; set PSEUDOLIFE_MCP_TOKEN_FILE to its existing private token file and retry; nothing was deployed",
    ))
}

fn stopped_preflight(
    u: &Update,
    running: &Value,
    daemon: &str,
    connection: (&str, &str, &str, u16),
    choice: &str,
    config: &Value,
) -> Result<Value> {
    let (pg, user, db, port) = connection;
    verify_stopped_database(config, running, &inspect(u, pg)?, user, db, port)?;
    let id = running["Image"].as_str().filter(|id| !id.is_empty())
        .ok_or_else(|| UpdateError::usage("the stopped daemon's existing image is unavailable; restore that local image before retrying; nothing was deployed"))?;
    let (code, out) = u.docker_call(&["image", "inspect", "-f", "{{.Id}}", id]);
    if code != 0 || out.trim() != id {
        return Err(UpdateError::usage(
            "the stopped daemon's existing image is unavailable; restore that local image before retrying; nothing was deployed",
        ));
    }
    let schema = sql(u, pg, user, db, "SELECT value::text FROM meta WHERE key = 'schema_version'")?
        .trim_matches('"').parse::<i64>()
        .map_err(|_| UpdateError::usage("cannot verify the restored bank schema; restore a complete bank before retrying; nothing was deployed"))?;
    let pending = choice == "rust"
        && (schema < 57
            || sql(
                u,
                pg,
                user,
                db,
                "SELECT EXISTS (SELECT 1 FROM cms_counters WHERE id = 1)",
            )? != "t");
    let script = if pending {
        "test -d /data && test -r /data && test ! -e /data/memory_state/weights.pt && test ! -e /data/memory_state/weights.pt.bak"
    } else {
        "test -d /data && test -r /data && test -x /bin/sh"
    };
    if choice == "rust" && schema < 57 {
        return Err(UpdateError::usage(
            "Rust activation requires schema v57 or later and imported counters; run update --engine python --checkout <repo> first; nothing was changed",
        ));
    }
    let old_rust = running["Config"]["Labels"][ENGINE_LABEL] == "rust";
    let metadata = if old_rust {
        "/usr/local/bin/pseudolife-mcp update --schema-version"
    } else {
        "cat /app/pseudolife_memory/storage/schema.py"
    };
    u.with_state_helper(daemon, id, &format!("{script} && {metadata}"), |helper| {
        let (code, output) = u.command(&["start".into(), "-a".into(), helper.into()], None, 60, false);
        let image_schema = if old_rust { output.trim().parse::<i64>().ok() } else {
            regex::Regex::new(r"(?m)^SCHEMA_META_VERSION\s*=\s*([0-9]+)").expect("schema")
                .captures(&output).and_then(|c| c[1].parse::<i64>().ok())
        };
        if code != 0 || image_schema.is_none_or(|image_schema| image_schema < schema) {
            return Err(UpdateError::usage("the stopped image cannot verify its state or support the restored schema; restore an accepted image compatible with this bank before retrying; nothing was deployed"));
        }
        Ok(())
    })?;
    Ok(json!({"schema":schema,"version":"previous"}))
}

pub(super) fn deploy(u: &mut Update, repo: &Path) -> Result<()> {
    let repo = seams::command_path(fs::canonicalize(repo)?);
    let ops = repo.join("ops");
    let env_file = ops.join(".env");
    let base = compose_args(
        &[
            ops.join("docker-compose.yml"),
            ops.join("docker-compose.override.yml"),
        ],
        std::slice::from_ref(&env_file),
    );
    let choice =
        u.o.engine
            .clone()
            .or_else(|| {
                selection(&repo.join(SELECTION))
                    .ok()
                    .flatten()
                    .and_then(|s| s["labels"][ENGINE_LABEL].as_str().map(str::to_owned))
            })
            .unwrap_or_else(|| u.selected_engine.clone());
    let tree = tree_state(&repo);
    let env = compose_environment(std::slice::from_ref(&env_file), &build_environment(&tree));
    if u.o.shadow && choice == "python" {
        return shadow::remove(u, &ops, &base, &env);
    }
    if u.o.no_backup {
        return Err(UpdateError::usage(
            "engine changes require a backup; remove --no-backup and retry; nothing was changed",
        ));
    }
    if tree.dirty != "false" && !u.o.allow_dirty {
        return Err(UpdateError::usage(
            "commit or stash the checkout before switching engines (or pass --allow-dirty); nothing was changed",
        ));
    }
    let mut args = vec!["compose".into()];
    args.extend(base.clone());
    args.extend(["config".into(), "--format".into(), "json".into()]);
    let (code, out) = u.command(&args, Some(&env), 60, false);
    let config:Value=serde_json::from_str(&out).ok().filter(|_:&Value|code==0).ok_or_else(||UpdateError::usage("cannot resolve the Compose project; check its files and env file, then retry; nothing was changed"))?;
    let daemon = container(u, &base, Some(&env), DAEMON_SERVICE)?;
    let pg = container(u, &base, Some(&env), "pseudolife-pg")?;
    let running = inspect(u, &daemon)?;
    let labels = &running["Config"]["Labels"];
    if labels[ENGINE_LABEL] == "rust" || truthy(&labels[ACTIVATION_LABEL]) {
        validated(labels)?;
    }
    let stopped_handover =
        u.o.allow_auth_change && u.o.checkout.is_some() && running["State"]["Running"] == false;
    u.stopped_handover = stopped_handover;
    if running["State"]["Running"] != true && !stopped_handover {
        return Err(UpdateError::usage(
            "the current daemon must be running for the backup and rollback tag; start it and retry",
        ));
    }
    verify_identity(u, &config, &running, &u.o.daemon_url)?;
    u.set_rollback_scope(&config, &running)?;
    u.maintainer_context = Some((base.clone(), env.clone()));
    let pg_env = &config["services"]["pseudolife-pg"]["environment"];
    let runtime = if stopped_handover {
        backup::connection_details(&running, &inspect(u, &pg)?).ok_or_else(|| UpdateError::usage(
            "cannot verify the handover bank on the project's shared PostgreSQL network; restore the daemon's original direct database connection and retry; nothing was deployed"
        ))?
    } else {
        (String::new(), String::new(), 5432)
    };
    u.handover_port = runtime.2;
    let user = if stopped_handover {
        runtime.0.as_str()
    } else {
        pg_env["POSTGRES_USER"]
            .as_str()
            .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL user"))?
    };
    let db = if stopped_handover {
        runtime.1.as_str()
    } else {
        pg_env["POSTGRES_DB"]
            .as_str()
            .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL database"))?
    };
    let configured_env = &config["services"][DAEMON_SERVICE]["environment"];
    let raw_map = configured_env["PSEUDOLIFE_MCP_TOKENS"]
        .as_str()
        .unwrap_or("");
    if u.o.allow_auth_change && !raw_map.trim().is_empty() {
        let mut map_only = config.clone();
        map_only["services"][DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_TOKEN"] = json!("");
        daemon_token(&map_only)?;
    }
    let stored_only = configured_env["PSEUDOLIFE_MCP_TOKEN"]
        .as_str()
        .unwrap_or("")
        .is_empty()
        && raw_map.trim().is_empty();
    let token = if stopped_handover && stored_only {
        stored_probe_token(u, &pg, user, db)?
    } else {
        daemon_token(&config)?
    };
    if choice == "rust" && !stopped_handover {
        require_imported_with(u, &pg, user, db, &daemon)?;
    }
    let before = if stopped_handover {
        stopped_preflight(
            u,
            &running,
            &daemon,
            (&pg, user, db, runtime.2),
            &choice,
            &config,
        )?
    } else {
        u.daemon_health().ok_or_else(||UpdateError::usage("the running daemon did not answer /health; use the previous update's rollback commands before retrying; no rollback tag was moved"))?
    };
    if !stopped_handover
        && (before["status"] != "ok" || !seams::authenticated_read(&u.o.daemon_url, &token))
    {
        return Err(UpdateError::usage(
            "the running daemon did not pass an authenticated bank read; use the previous update's rollback commands before retrying; no rollback tag was moved",
        ));
    }
    u.schema_before = before["schema"].as_i64();
    let running_version = before["version"].as_str().unwrap_or("unknown");
    if !safe_tag(running_version) {
        return Err(UpdateError::usage(
            "the running daemon reported an invalid version; restore its previous image before retrying",
        ));
    }
    let (version, schema) = checkout_version_schema(&repo)?;
    if stopped_handover && u.schema_before.is_some_and(|before| before > schema) {
        return Err(UpdateError::usage(
            "the restored bank is newer than this checkout; update the checkout before retrying; nothing was deployed",
        ));
    }
    if u.o.shadow {
        return shadow::start(
            u, &repo, &ops, &base, &env, &tree, &config, &running, &pg, user, db, &token, &version,
            schema,
        );
    }
    let backups = if stopped_handover {
        u.builtin_backup(&pg, &daemon, &repo.join("data/backups"))?
    } else {
        u.project_backup(&repo, &pg, &daemon, user, db)?
    };
    if stopped_handover {
        let current = inspect(u, &daemon)?;
        if current["Image"] != running["Image"]
            || current["State"]["Running"] != false
            || current["Config"] != running["Config"]
        {
            return Err(UpdateError::usage(
                "the stopped daemon changed during backup; stop other updates and retry; nothing was deployed",
            ));
        }
    }
    let old = labels[ENGINE_LABEL].as_str().unwrap_or("python");
    let suffix = u.rollback_suffix();
    let rollback = format!("pseudolife-daemon-{old}:{running_version}-{suffix}");
    let id = running["Image"]
        .as_str()
        .ok_or_else(|| UpdateError::usage("cannot identify the running image"))?;
    tag_validated(u, id, &rollback)?;
    let old_service = rollback_service(&running, &rollback, Some(&repo));
    let rollback_file = repo.join("data/update-engine").join(format!(
        "docker-compose.rollback-{}-{}.json",
        stamp(),
        uuid::Uuid::new_v4().simple()
    ));
    write_overlay(&rollback_file, &old_service)?;
    let selected = repo.join(SELECTION);
    let accepted = if old == "rust" {
        selected.clone()
    } else {
        repo.join(PYTHON_ACCEPTED)
    };
    u.rollback_lines = rollback_commands(
        &base,
        &[env_file],
        &rollback_file,
        &accepted,
        (old == "python").then_some(selected.as_path()),
        None,
    );
    u.rollback_lines.extend(u.schema_rollback_note());
    u.report.rollback =
        json!({"tag":rollback,"state":"tagged","overlay":rollback_file,"backup":backups});
    u.prune_rollbacks("pseudolife-daemon-python");
    u.prune_rollbacks("pseudolife-daemon-rust");
    let activation = uuid::Uuid::new_v4().simple().to_string();
    let service = json!({"image":format!("pseudolife-daemon-{choice}:checkout"),"build":{"dockerfile":format!("ops/Dockerfile.daemon{}",if choice=="rust"{"-rust"}else{""})},"labels":{ENGINE_LABEL:choice,CHECKOUT_LABEL:repo,ACTIVATION_LABEL:activation}});
    let candidate = repo.join("data/update-engine/docker-compose.engine-candidate.json");
    write_overlay(&candidate, &service).map_err(|e| u.rollback_error(e.message))?;
    let mut compose = base;
    if choice == "rust" {
        compose.extend([
            "-f".into(),
            ops.join("docker-compose.rust-daemon.yml")
                .to_string_lossy()
                .into_owned(),
        ]);
    }
    compose.extend(["-f".into(), candidate.to_string_lossy().into_owned()]);
    let result = (|| -> Result<()> {
        if tree_state(&repo) != tree {
            return Err(UpdateError::new(
                "the checkout moved during backup; retry from a stable checkout",
            ));
        }
        u.step(format!(
            "building the {choice} daemon; rollback image {rollback}"
        ));
        let mut args = vec!["compose".into()];
        args.extend(compose.clone());
        args.extend(["build".into(), DAEMON_SERVICE.into()]);
        if u.command(&args, Some(&env), 7200, true).0 != 0 {
            return Err(UpdateError::new(
                "engine build failed; the running daemon is unchanged",
            ));
        }
        let mut args = vec!["compose".into()];
        args.extend(compose);
        args.extend([
            "up".into(),
            "-d".into(),
            "--no-deps".into(),
            "--no-build".into(),
            DAEMON_SERVICE.into(),
        ]);
        if u.command(&args, Some(&env), 120, true).0 != 0 {
            return Err(UpdateError::new("engine recreate failed"));
        }
        let health = u.wait_health(Some(&version), Some(schema), &token, false)?;
        if choice == "rust" {
            write_overlay(&selected, &service)?;
        } else {
            write_overlay(&repo.join(PYTHON_ACCEPTED), &service)?;
            if selected.exists() {
                fs::remove_file(&selected)?;
            }
        }
        u.checkout_post_deploy(&repo, before["hooks_digest"].as_str(), &health)?;
        u.prune_cache(Some(&repo));
        Ok(())
    })();
    if fs::remove_file(&candidate).is_err() {
        u.warn("could not remove the temporary engine overlay; the rollback instructions remain usable");
    }
    result.map_err(|e| u.rollback_error(e.message))
}

#[cfg(test)]
mod bearer_source_tests {
    use super::*;

    #[test]
    fn explicit_auth_transfer_accepts_carried_or_stored_credentials_but_not_malformed_maps() {
        let running = json!(["PSEUDOLIFE_MCP_TOKEN=fixture-original"]);
        for configured in [json!({"PSEUDOLIFE_MCP_TOKEN":"fixture-carried"}), json!({})] {
            verify_compose_bearers(&configured, &running, true).unwrap();
            assert!(verify_compose_bearers(&configured, &running, false).is_err());
        }
        assert!(
            verify_compose_bearers(
                &json!({"PSEUDOLIFE_MCP_TOKENS":"invalid-map"}),
                &running,
                true
            )
            .is_err()
        );
    }

    #[test]
    fn compose_bearers_preserve_credentials_without_a_client_environment() {
        let running = json!(["PSEUDOLIFE_MCP_TOKEN=fixture-daemon"]);
        for config in [json!({}), json!({"PSEUDOLIFE_MCP_TOKEN":"fixture-client"})] {
            let refused = verify_compose_bearers(&config, &running, false).unwrap_err();
            assert_eq!(refused.exit_code, 2);
            assert!(refused.message.contains("--env-file"));
            assert!(!refused.message.contains("fixture-daemon"));
            assert!(!refused.message.contains("fixture-client"));
        }
        verify_compose_bearers(&json!({}), &json!([]), false).unwrap();
        verify_compose_bearers(
            &json!({"PSEUDOLIFE_MCP_TOKEN":"fixture-daemon"}),
            &running,
            false,
        )
        .unwrap();
        verify_compose_bearers(
            &json!({"PSEUDOLIFE_MCP_TOKENS":"fixture-b:second, fixture-a:FIRST"}),
            &json!(["PSEUDOLIFE_MCP_TOKENS=fixture-a:first,fixture-b:second"]),
            false,
        )
        .unwrap();
    }
}

#[cfg(test)]
mod stopped_handover_tests {
    use super::*;

    #[test]
    fn review_running_checkout_cannot_drop_a_bearer_subset() {
        let config = json!({"services":{DAEMON_SERVICE:{"environment":{
            "PSEUDOLIFE_MCP_DATABASE_URL":"fixture", "PSEUDOLIFE_MCP_TOKEN":"fixture-a",
            "PSEUDOLIFE_MCP_TOKENS":"fixture-a:owner"},
            "volumes":[{"type":"volume","source":"state","target":"/data"}]}},
            "volumes":{"state":{"name":"fixture-state"}}});
        let mut running = json!({"State":{"Running":true},"Config":{"Env":[
            "PSEUDOLIFE_MCP_DATABASE_URL=fixture", "PSEUDOLIFE_MCP_TOKEN=fixture-a",
            "PSEUDOLIFE_MCP_TOKENS=fixture-a:owner,fixture-b:peer"]},
            "Mounts":[{"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true}]});
        let update = Update::new(Options {
            allow_auth_change: true,
            checkout: Some(PathBuf::from("fixture")),
            ..Options::default()
        });
        let refused = verify_bank_identity(&update, &config, &running).unwrap_err();
        assert!(refused.message.contains("stopped"));
        running["State"]["Running"] = json!(false);
        assert!(verify_bank_identity(&update, &config, &running).is_ok());
    }

    #[test]
    fn review_sql_socket_scope_requires_the_validated_stopped_handover() {
        let mut update = Update::new(Options {
            allow_auth_change: true,
            checkout: Some(PathBuf::from("fixture")),
            ..Options::default()
        });
        let ordinary = sql_args(&update, "pg", "role", "bank", "SELECT 1");
        update.stopped_handover = true;
        update.handover_port = 6543;
        let stopped = sql_args(&update, "pg", "role", "bank", "SELECT 1");
        assert!(!ordinary.iter().any(|arg| arg == "env" || arg == "-h"));
        assert!(
            stopped
                .windows(2)
                .any(|pair| pair == ["-h", "/var/run/postgresql"])
        );
        assert!(stopped.windows(2).any(|pair| pair == ["-p", "6543"]));
    }

    fn running(config: &Value) -> Value {
        let environment = config["services"][DAEMON_SERVICE]["environment"]
            .as_object()
            .unwrap();
        let entries: Vec<_> = environment
            .iter()
            .map(|(key, value)| format!("{key}={}", value.as_str().unwrap()))
            .collect();
        json!({"Config":{"Env":entries},"NetworkSettings":{"Networks":{"fixture":{}}}})
    }

    #[test]
    fn stopped_bank_verification_accepts_libpq_database_defaults_and_tls_options() {
        let pg = json!({"State":{"Running":true},"Name":"/custom-project-pg",
            "NetworkSettings":{"Networks":{"fixture":{"Aliases":["legacy-pg"]}}}});
        for url in [
            "postgresql://fixture:fixture@legacy-pg",
            "postgresql://fixture:fixture@legacy-pg/fixture?sslmode=require&sslrootcert=/run/ca.pem",
        ] {
            let config = json!({"services":{DAEMON_SERVICE:{"environment":{
                "PSEUDOLIFE_MCP_DATABASE_URL":url
            }}}});
            assert!(
                verify_stopped_database(
                    &config,
                    &running(&config),
                    &pg,
                    "fixture",
                    "fixture",
                    5432
                )
                .is_ok()
            );
        }
    }

    #[test]
    fn stopped_bank_verification_preserves_nondefault_port_and_literal_database() {
        let config = json!({"services":{DAEMON_SERVICE:{"environment":{
            "PSEUDOLIFE_MCP_DATABASE_URL":"postgresql://fixture@legacy-pg:6543/host%3Delsewhere"
        }}}});
        let pg = json!({"State":{"Running":true},"Name":"/custom-project-pg",
            "NetworkSettings":{"Networks":{"fixture":{"Aliases":["legacy-pg"]}}}});
        assert!(
            verify_stopped_database(
                &config,
                &running(&config),
                &pg,
                "fixture",
                "host=elsewhere",
                6543
            )
            .is_ok()
        );
        assert!(
            verify_stopped_database(
                &config,
                &running(&config),
                &pg,
                "fixture",
                "host=elsewhere",
                5432
            )
            .is_err()
        );
    }

    #[cfg(unix)]
    #[test]
    fn overlay_replacement_accepts_python_written_file_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("pseudolife-overlay-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&dir).unwrap();
        let path = dir.join("accepted.json");
        fs::write(&path, b"previous Python overlay").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        let service = json!({"image":"fixture:accepted","labels":{ENGINE_LABEL:"rust"}});
        let result = write_overlay(&path, &service);
        let bytes = fs::read(&path).unwrap();
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        fs::remove_dir_all(&dir).unwrap();
        result.unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&bytes).unwrap()["services"][DAEMON_SERVICE],
            service
        );
        assert_eq!(mode, 0o600);
    }

    #[cfg(unix)]
    #[test]
    fn overlay_replacement_rejects_redirects_without_changing_the_target() {
        let dir = std::env::temp_dir().join(format!("pseudolife-overlay-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&dir).unwrap();
        let target = dir.join("original.json");
        let link = dir.join("accepted.json");
        fs::write(&target, b"original").unwrap();
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let result = write_overlay(&link, &json!({"image":"fixture:accepted"}));
        let bytes = fs::read(&target).unwrap();
        let entries = fs::read_dir(&dir).unwrap().count();
        fs::remove_dir_all(&dir).unwrap();
        assert!(result.is_err());
        assert_eq!(bytes, b"original");
        assert_eq!(entries, 2);
    }

    #[test]
    fn stopped_bank_verification_is_scoped_to_the_declared_project_database() {
        let mut config = json!({"services":{DAEMON_SERVICE:{"environment":{
            "PSEUDOLIFE_MCP_DATABASE_URL":"postgresql://fixture:fixture@legacy-pg:5432/fixture"
        }}}});
        let mut pg = json!({"State":{"Running":true},"Name":"/custom-project-pg",
            "NetworkSettings":{"Networks":{"fixture":{"Aliases":["legacy-pg"]}}}});
        assert!(
            verify_stopped_database(&config, &running(&config), &pg, "fixture", "fixture", 5432)
                .is_ok()
        );
        for url in [
            "postgresql://fixture:fixture@external-pg:5432/fixture",
            "postgresql://fixture:fixture@legacy-pg:5432/other",
            "postgresql://other:fixture@legacy-pg:5432/fixture",
            "postgresql://fixture:fixture@legacy-pg:5433/fixture",
        ] {
            config["services"][DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] =
                json!(url);
            assert!(
                verify_stopped_database(
                    &config,
                    &running(&config),
                    &pg,
                    "fixture",
                    "fixture",
                    5432
                )
                .is_err()
            );
        }
        config["services"][DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] =
            json!("postgresql://fixture:fixture@legacy-pg:5432/fixture");
        pg["State"]["Running"] = json!(false);
        assert!(
            verify_stopped_database(&config, &running(&config), &pg, "fixture", "fixture", 5432)
                .is_err()
        );
    }
}
