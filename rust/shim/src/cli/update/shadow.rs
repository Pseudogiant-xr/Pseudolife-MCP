//! Read-only companion lifecycle; recovery never requires a healthy writer.
use super::*;
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
};
const SERVICE: &str = "pseudolife-rust-shadow";
const PASSWORD: &str = "PSEUDOLIFE_SHADOW_DB_PASSWORD";
const CONFIG_LABEL: &str = "org.pseudolife.shadow.configuration";
const REMOVE: &str = "update --engine python --shadow --checkout <repo>";
const RETRY: &str = "update --engine rust --shadow --checkout <repo>";

fn compose_call(
    u: &Update,
    base: &[String],
    env: &Environment,
    tail: &[&str],
    timeout: u64,
) -> (i32, String) {
    let mut args = vec!["compose".into()];
    args.extend_from_slice(base);
    args.extend(tail.iter().map(|s| s.to_string()));
    u.command(&args, Some(env), timeout, false)
}
fn companion(u: &Update, base: &[String], env: &Environment) -> Result<Option<String>> {
    let (code, out) = compose_call(u, base, env, &["ps", "-a", "-q", SERVICE], 60);
    let ids: Vec<_> = out.split_whitespace().collect();
    if code != 0 || ids.len() > 1 {
        return Err(UpdateError::usage(
            "cannot identify the project's shadow; restore its Compose configuration and retry; nothing was changed",
        ));
    }
    Ok(ids.first().map(|s| s.to_string()))
}
pub(super) fn remove(
    u: &mut Update,
    ops: &Path,
    base: &[String],
    environment: &Environment,
) -> Result<()> {
    let overlay = ops.join("docker-compose.rust-shadow.yml");
    if !overlay.is_file() {
        return Err(UpdateError::usage(
            "shadow overlay is missing; restore ops/docker-compose.rust-shadow.yml and retry; nothing was changed",
        ));
    }
    let mut base = base.to_vec();
    base.extend(["-f".into(), overlay.to_string_lossy().into_owned()]);
    let mut env = environment.clone();
    env.insert(PASSWORD.into(), "unused-for-removal".into());
    if companion(u, &base, &env)?.is_none() {
        u.step("no Rust shadow is installed; Python is unchanged");
        return Ok(());
    }
    if compose_call(u, &base, &env, &["rm", "--stop", "--force", SERVICE], 120).0 != 0 {
        return Err(UpdateError::new(format!(
            "could not remove the Rust shadow; retry {REMOVE}; Python is unchanged"
        )));
    }
    u.step("removed the Rust shadow; Python is unchanged (model cache and password retained for reuse)");
    Ok(())
}

pub(super) trait AppendFile: Read + Write + Seek {
    fn truncate(&mut self, len: u64) -> std::io::Result<()>;
    fn sync(&mut self) -> std::io::Result<()>;
}
impl AppendFile for File {
    fn truncate(&mut self, len: u64) -> std::io::Result<()> {
        self.set_len(len)
    }
    fn sync(&mut self) -> std::io::Result<()> {
        self.sync_all()
    }
}
pub(super) fn append_checked(
    file: &mut impl AppendFile,
    original: &[u8],
    addition: &[u8],
) -> std::io::Result<()> {
    file.seek(SeekFrom::Start(0))?;
    let mut current = vec![];
    file.read_to_end(&mut current)?;
    if current != original {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "host env changed during shadow build",
        ));
    }
    let result = (|| {
        file.write_all(addition)?;
        file.sync()
    })();
    if result.is_err() {
        // File is unbuffered: truncate the descriptor directly. Never flush or
        // retry pending output after ENOSPC, including a short first write.
        file.truncate(original.len() as u64)?;
        file.sync()?;
    }
    result
}
fn save_password(path: &Path, original: Option<&[u8]>, password: &str) -> Result<()> {
    let addition = format!("\n{PASSWORD}={password}\n");
    if let Some(original) = original {
        let mut file = OpenOptions::new().read(true).write(true).open(path)?;
        append_checked(&mut file,original,addition.as_bytes()).map_err(|e|if e.kind()==std::io::ErrorKind::InvalidData{UpdateError::new("the host env file changed during the shadow build; its edits were preserved; retry after the configuration is settled")}else{UpdateError::new("cannot save the shadow setup files")})
    } else {
        // atomic_create refuses an intervening file instead of overwriting it.
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        crate::board::state::atomic_create(path, addition.as_bytes()).map_err(|_| {
            UpdateError::new(
                "cannot save the shadow setup files; an intervening host env file was preserved",
            )
        })
    }
}
fn dsn_matches(value: &Value, db: &str, user: Option<&str>, password: Option<&str>) -> bool {
    let Some(value) = value.as_str() else {
        return false;
    };
    // Reuse the native libpq grammar rather than accepting a URL by substring.
    let Ok(dsn) = crate::pg::Dsn::parse(value) else {
        return false;
    };
    // tokio-postgres's parser handles both libpq keyword strings and URLs.
    let Ok(config) = value.parse::<tokio_postgres::Config>() else {
        return false;
    };
    let _ = dsn;
    let host = config.get_hosts();
    let ports = config.get_ports();
    matches!(host, [tokio_postgres::config::Host::Tcp(h)] if h=="pseudolife-pg")
        && (ports.is_empty() || ports == [5432])
        && config.get_dbname() == Some(db)
        && user.is_none_or(|u| config.get_user() == Some(u))
        && password.is_none_or(|p| config.get_password() == Some(p.as_bytes()))
}
fn config(u: &Update, base: &[String], env: &Environment) -> Result<Value> {
    let (code, out) = compose_call(u, base, env, &["config", "--format", "json"], 60);
    serde_json::from_str(&out).ok().filter(|_:&Value|code==0).ok_or_else(||UpdateError::usage("cannot resolve the Compose project; check its files and env file, then retry; nothing was changed"))
}
fn gate(
    u: &mut Update,
    url: &str,
    token: &str,
    version: &str,
    schema: i64,
    retries: usize,
) -> Result<Value> {
    let old_url = std::mem::replace(&mut u.o.daemon_url, url.into());
    let old_retries = std::mem::replace(&mut u.o.health_retries, retries);
    let result = u.wait_health(Some(version), Some(schema), token, true);
    u.o.daemon_url = old_url;
    u.o.health_retries = old_retries;
    result
}
fn stable(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            let mut result = serde_json::Map::new();
            for key in keys {
                result.insert(key.clone(), stable(&map[key]));
            }
            Value::Object(result)
        }
        Value::Array(a) => Value::Array(a.iter().map(stable).collect()),
        _ => value.clone(),
    }
}
struct Cleanup {
    args: Vec<String>,
    env: Environment,
    armed: bool,
}
impl Drop for Cleanup {
    fn drop(&mut self) {
        if self.armed {
            let _ = run_cli(&self.args, Some(&self.env), 120, false);
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn start(
    u: &mut Update,
    repo: &Path,
    ops: &Path,
    base: &[String],
    environment: &Environment,
    tree: &seams::Tree,
    writer_config: &Value,
    running: &Value,
    pg: &str,
    user: &str,
    db: &str,
    token: &str,
    version: &str,
    schema: i64,
) -> Result<()> {
    for path in [
        "ops/docker-compose.rust-shadow.yml",
        "ops/Dockerfile.daemon-rust",
        "ops/sql/shadow_reader_role.sql",
        "rust/daemon/src/config.rs",
    ] {
        if !repo.join(path).is_file() {
            return Err(UpdateError::usage(
                "shadow support is missing (overlay, Rust image, SELECT-only role or PSEUDOLIFE_READ_ONLY); update the checkout to current master and retry; nothing was changed",
            ));
        }
    }
    if !fs::read_to_string(repo.join("rust/daemon/src/config.rs"))?.contains("PSEUDOLIFE_READ_ONLY")
    {
        return Err(UpdateError::usage(
            "shadow support is missing PSEUDOLIFE_READ_ONLY; update the checkout and retry; nothing was changed",
        ));
    }
    if running["Config"]["Labels"][engine::ENGINE_LABEL] == "rust" {
        return Err(UpdateError::usage(
            "shadow mode needs the Python daemon; run update --engine python --checkout <repo> first",
        ));
    }
    let path = ops.join(".env");
    let original = match fs::read(&path) {
        Ok(bytes) => Some(bytes),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => return Err(e.into()),
    };
    let text = original
        .as_deref()
        .map(std::str::from_utf8)
        .transpose()
        .map_err(|_| UpdateError::usage("the host env file must be UTF-8; nothing was changed"))?
        .unwrap_or("");
    let assignments: Vec<_> = text
        .lines()
        .filter_map(|line| {
            line.trim()
                .strip_prefix("export ")
                .unwrap_or(line.trim())
                .split_once('=')
        })
        .filter(|(k, _)| k.trim() == PASSWORD)
        .map(|(_, v)| v.trim())
        .collect();
    if assignments.len() > 1
        || assignments.first().is_some_and(|v| {
            v.is_empty()
                || !v
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        })
    {
        return Err(UpdateError::usage(
            "PSEUDOLIFE_SHADOW_DB_PASSWORD must be one URL-safe value in the host env file; fix it and retry; nothing was changed",
        ));
    }
    let save = assignments.is_empty();
    let password = assignments
        .first()
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            )
        });
    let mut env = environment.clone();
    env.insert(PASSWORD.into(), password.clone());
    env.insert(
        "PSEUDOLIFE_SHADOW_DB_NAME".into(),
        percent_encoding::utf8_percent_encode(db, percent_encoding::NON_ALPHANUMERIC).to_string(),
    );
    let mut compose = base.to_vec();
    compose.extend([
        "-f".into(),
        ops.join("docker-compose.rust-shadow.yml")
            .to_string_lossy()
            .into_owned(),
    ]);
    let resolved = config(u, &compose, &env)?;
    let main = &writer_config["services"][DAEMON_SERVICE];
    let service = &resolved["services"][SERVICE];
    let settings = &service["environment"];
    if !dsn_matches(
        &main["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"],
        db,
        None,
        None,
    ) || !dsn_matches(
        &settings["PSEUDOLIFE_MCP_DATABASE_URL"],
        db,
        Some("pseudolife_shadow"),
        Some(&password),
    ) {
        return Err(UpdateError::usage(
            "shadow must use pseudolife_shadow on the writer's project PostgreSQL bank; restore its Compose bank settings and retry; nothing was changed",
        ));
    }
    if settings["PSEUDOLIFE_READ_ONLY"] != "1"
        || ["PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS"]
            .iter()
            .any(|k| {
                settings.get(*k).unwrap_or(&Value::Null)
                    != main["environment"].get(*k).unwrap_or(&Value::Null)
            })
    {
        return Err(UpdateError::usage(
            "shadow needs read-only mode and the writer's bearer configuration; restore the shadow overlay and retry; nothing was changed",
        ));
    }
    let data: Vec<_> = service["volumes"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|v| v["target"] == "/data")
        .collect();
    let actual = running["Mounts"]
        .as_array()
        .and_then(|a| a.iter().find(|v| v["Destination"] == "/data"))
        .and_then(|v| v["Name"].as_str());
    if data.len() != 1
        || data[0]["read_only"] != true
        || data[0]["type"] != "volume"
        || data[0]["source"]
            .as_str()
            .and_then(|s| resolved["volumes"][s]["name"].as_str())
            != actual
        || resolved["services"][DAEMON_SERVICE] != *main
    {
        return Err(UpdateError::usage(
            "shadow must mount the writer's /data read-only and leave Python unchanged; restore the shadow overlay and retry; nothing was changed",
        ));
    }
    let ports = service["ports"].as_array().ok_or_else(|| {
        UpdateError::usage(
            "shadow needs its own loopback port; restore the shadow overlay; nothing was changed",
        )
    })?;
    let port = ports
        .first()
        .map(|p| {
            p["published"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| p["published"].to_string())
        })
        .unwrap_or_default();
    if ports.len() != 1
        || ports[0]["host_ip"] != "127.0.0.1"
        || ports[0]["target"] != 8765
        || port.parse::<u16>().is_err()
        || main["ports"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|p| p["published"] == ports[0]["published"])
    {
        return Err(UpdateError::usage(
            "shadow needs its own loopback port; restore PSEUDOLIFE_SHADOW_PORT=8766 and the shadow overlay, then retry; nothing was changed",
        ));
    }
    let url = format!("http://127.0.0.1:{port}");
    let mut service = service.clone();
    if let Some(labels) = service["labels"].as_object_mut() {
        labels.remove(CONFIG_LABEL);
    }
    if let Some(args) = service["build"]["args"].as_object_mut() {
        args.remove("BUILD_TIME");
    }
    let fingerprint = format!(
        "{:x}",
        Sha256::digest(
            serde_json::to_vec(&stable(
                &json!({"service":service,"volumes":resolved["volumes"],"revision":tree.sha})
            ))
            .expect("fingerprint")
        )
    );
    env.insert(
        "PSEUDOLIFE_SHADOW_CONFIGURATION".into(),
        fingerprint.clone(),
    );
    if let Some(id) = companion(u, &compose, &env)?
        && tree.dirty == "false"
    {
        let current = engine::inspect(u, &id)?;
        if current["State"]["Running"] == true
            && current["Config"]["Labels"][CONFIG_LABEL] == fingerprint
            && gate(u, &url, token, version, schema, 1).is_ok()
        {
            u.step(format!(
                "read-only Rust shadow is already healthy at {url}; nothing changed"
            ));
            return Ok(());
        }
    }
    u.step("building the read-only Rust shadow (Python remains running)...");
    if compose_call(u, &compose, &env, &["build", SERVICE], 7200).0 != 0 {
        return Err(UpdateError::new(format!(
            "shadow build failed; Python is unchanged; retry {RETRY}"
        )));
    }
    if tree_state(repo) != *tree {
        return Err(UpdateError::new(format!(
            "the checkout moved during the shadow build; Python is unchanged; retry {RETRY}"
        )));
    }
    if fs::read(&path).ok() != original {
        return Err(UpdateError::new(
            "the host env file changed during the shadow build; its edits were preserved; retry after the configuration is settled",
        ));
    }
    let setup = (|| -> Result<()> {
        if save {
            save_password(&path, original.as_deref(), &password)?;
        }
        let temporary = std::env::temp_dir().join(format!(
            "pseudolife-shadow-{}.sql",
            uuid::Uuid::new_v4().simple()
        ));
        let remote = format!(
            "/tmp/pseudolife-shadow-{}.sql",
            uuid::Uuid::new_v4().simple()
        );
        let mut script = format!("\\set shadow_password '{password}'\n").into_bytes();
        script.extend(fs::read(ops.join("sql/shadow_reader_role.sql"))?);
        seams::write_atomic(&temporary, &script)?;
        let copied = u
            .docker_call(&[
                "cp",
                &temporary.to_string_lossy(),
                &format!("{pg}:{remote}"),
            ])
            .0;
        let applied = if copied == 0 {
            u.docker_call(&[
                "exec",
                pg,
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                user,
                "-d",
                db,
                "-f",
                &remote,
            ])
            .0
        } else {
            1
        };
        let cleaned = u.docker_call(&["exec", pg, "rm", "-f", &remote]).0;
        let local_cleaned = fs::remove_file(&temporary).is_ok();
        if cleaned != 0 || !local_cleaned {
            return Err(UpdateError::new(
                "could not remove the private shadow role script; remove the owned temporary script before retrying",
            ));
        }
        if applied != 0 {
            return Err(UpdateError::new(
                "could not apply the SELECT-only shadow role; check PostgreSQL access and the role script's prerequisites, then retry",
            ));
        }
        Ok(())
    })();
    setup.map_err(|e| {
        UpdateError::new(format!(
            "{}; Python is unchanged; retry {RETRY} (the saved password is retained)",
            e.message
        ))
    })?;
    // Own exactly this Compose service from the first startup attempt. Drop
    // also cleans it during unwinding; the writer is never targeted.
    let mut args = vec![u.docker.clone(), "compose".into()];
    args.extend(compose.clone());
    args.extend([
        "rm".into(),
        "--stop".into(),
        "--force".into(),
        SERVICE.into(),
    ]);
    let mut cleanup = Cleanup {
        args,
        env: env.clone(),
        armed: true,
    };
    if compose_call(
        u,
        &compose,
        &env,
        &[
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "1920",
            SERVICE,
        ],
        1980,
    )
    .0 != 0
    {
        return Err(UpdateError::new(format!(
            "shadow startup did not become ready within its provisioning budget; Python is unchanged; remove it with {REMOVE}, then retry {RETRY} (the saved password is retained)"
        )));
    }
    let retries = u.o.health_retries;
    match gate(u, &url, token, version, schema, retries) {
        Ok(_) => {
            cleanup.armed = false;
            u.step(format!("read-only Rust shadow answers at {url}; Python is unchanged; remove it with {REMOVE}"));
            Ok(())
        }
        Err(e) => {
            let failed = compose_call(
                u,
                &compose,
                &env,
                &["rm", "--stop", "--force", SERVICE],
                120,
            )
            .0 != 0;
            cleanup.armed = false;
            Err(UpdateError::new(format!(
                "{}; Python is unchanged; {}retry {RETRY} (the saved password is retained)",
                e.message,
                if failed {
                    format!("remove it with {REMOVE}, then ")
                } else {
                    String::new()
                }
            )))
        }
    }
}
