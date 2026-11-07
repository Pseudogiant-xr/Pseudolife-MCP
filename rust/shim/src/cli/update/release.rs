use super::*;
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, io::Read};

pub(super) const PYTHON_IMAGE: &str = "ghcr.io/pseudogiant-xr/pseudolife-daemon";
pub(super) const RUST_IMAGE: &str = "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust";
const COMPOSE_FILES: [&str; 6] = [
    "docker-compose.yml",
    "docker-compose.ghcr.yml",
    "docker-compose.rust-daemon.yml",
    "docker-compose.rust-shadow.yml",
    "docker-compose.rust-ghcr.yml",
    "Dockerfile.extractor",
];

pub(super) fn health_endpoint_hint(ports: &str) -> String {
    let mut urls = std::collections::BTreeSet::new();
    for line in ports.lines() {
        let Some((host, port)) = line.trim().rsplit_once(':') else {
            continue;
        };
        if port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let Ok(port) = port.parse::<u16>() else {
            continue;
        };
        if port == 0 {
            continue;
        }
        match host {
            "127.0.0.1" | "0.0.0.0" => {
                urls.insert(format!("http://127.0.0.1:{port}"));
            }
            "[::1]" | "[::]" => {
                urls.insert(format!("http://[::1]:{port}"));
            }
            _ => {}
        }
    }
    if let Some(url) = urls.first() {
        format!(
            "Docker reports a daemon publication at {url} on the Docker host. If Docker runs on this machine and this is the intended daemon, retry with --daemon-url {url}; otherwise check your proxy/custom endpoint."
        )
    } else {
        "Check --daemon-url http(s)://<host>:<port> for the intended daemon or proxy endpoint."
            .into()
    }
}

pub(super) fn verified_compose(
    version: &str,
    sums: &[u8],
    archive: &[u8],
) -> Result<BTreeMap<String, Vec<u8>>> {
    let name = format!("pseudolife-compose-{version}.tar.gz");
    let sums =
        std::str::from_utf8(sums).map_err(|_| UpdateError::new("invalid release checksums"))?;
    let matches: Vec<_> = sums
        .lines()
        .filter_map(|line| line.split_once("  "))
        .filter(|(_, file)| *file == name)
        .collect();
    if matches.is_empty() {
        return Err(UpdateError::usage(
            "this release does not publish checksums for its native Compose bundle; use the Python updater, or --tag a release that ships pseudolife-compose-<version>.tar.gz; nothing was deployed",
        ));
    }
    if matches.len() != 1
        || matches[0].0.len() != 64
        || !matches[0]
            .0
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || matches[0].0 != format!("{:x}", Sha256::digest(archive))
    {
        return Err(UpdateError::new(
            "the Compose release archive checksum is missing or does not match; nothing was deployed",
        ));
    }
    let mut decoder = flate2::read::MultiGzDecoder::new(archive);
    let mut result = BTreeMap::new();
    {
        let mut tar = tar::Archive::new(&mut decoder);
        for entry in tar
            .entries()
            .map_err(|_| UpdateError::new("invalid Compose release archive"))?
        {
            let mut entry =
                entry.map_err(|_| UpdateError::new("invalid Compose release archive"))?;
            let path = entry
                .path()
                .map_err(|_| UpdateError::new("invalid Compose archive path"))?;
            let name = path
                .to_str()
                .ok_or_else(|| UpdateError::new("invalid Compose archive path"))?
                .to_owned();
            if !COMPOSE_FILES.contains(&name.as_str())
                || !entry.header().entry_type().is_file()
                || entry.size() > 1024 * 1024
                || result.contains_key(&name)
            {
                return Err(UpdateError::new(
                    "unexpected or duplicate file in Compose release archive; nothing was deployed",
                ));
            }
            let mut bytes = Vec::new();
            entry.read_to_end(&mut bytes)?;
            result.insert(name, bytes);
        }
    }
    std::io::copy(&mut decoder, &mut std::io::sink())?;
    if COMPOSE_FILES.iter().any(|n| !result.contains_key(*n)) {
        return Err(UpdateError::new(
            "the release Compose archive is incomplete; nothing was deployed",
        ));
    }
    Ok(result)
}
struct Context {
    args: Vec<String>,
    env_files: Vec<PathBuf>,
    checkout: Option<PathBuf>,
    running: Value,
    /// The Rust overlay the running daemon was created with, if still present.
    running_rust: Option<PathBuf>,
    /// Compose arguments the mount identity check resolves when a Rust
    /// daemon leaves Rust: the target project plus the Rust overlay, whose
    /// models volume the switch drops on purpose.
    identity_args: Option<Vec<String>>,
}

/// A copy of the shipped Compose files the release archive replaces: one
/// of this updater's release archives, the Python updater's bundled copies,
/// or a checkout's `ops/` (the installers' layout).
/// Recognised by shape as well as by today's roots, so an archive or copy
/// written under another home or data directory still counts.
fn packaged_compose(path: &Path) -> bool {
    let Some(parent) = path.parent() else {
        return false;
    };
    let name = |p: Option<&Path>| {
        p.and_then(Path::file_name)
            .and_then(|n| n.to_str())
            .map(str::to_owned)
    };
    let grandparent = parent.parent();
    // <...>/pseudolife-mcp/compose/<x.y.z>/: a release archive.
    let archive = grandparent == Some(compose_root().as_path())
        || (name(Some(parent)).is_some_and(|v| plain_release(&v))
            && name(grandparent).as_deref() == Some("compose")
            && name(grandparent.and_then(Path::parent)).as_deref() == Some("pseudolife-mcp"));
    // <data dir>/compose/: the Python updater's bundled copies.
    let bundled = parent == data_dir().join("compose")
        || (name(Some(parent)).as_deref() == Some("compose")
            && name(grandparent).as_deref() == Some(".pseudolife-mcp"));
    // <checkout>/ops/: the installers' layout.
    let checkout = name(Some(parent)).as_deref() == Some("ops")
        && grandparent.is_some_and(|c| c.join("pyproject.toml").is_file());
    archive || bundled || checkout
}

/// The updater's own overlays and the release's engine overlays: never a
/// project's settings, so never kept from the labels.
fn engine_overlay(name: &str) -> bool {
    name.starts_with("docker-compose.engine")
        || name.starts_with("docker-compose.rollback")
        || matches!(
            name,
            "docker-compose.rust-ghcr.yml"
                | "docker-compose.rust-daemon.yml"
                | "docker-compose.rust-shadow.yml"
        )
}

pub(super) fn compose_root() -> PathBuf {
    if cfg!(windows) {
        std::env::var_os("LOCALAPPDATA")
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join("AppData/Local"))
            .join("pseudolife-mcp/compose")
    } else {
        home().join(".local/share/pseudolife-mcp/compose")
    }
}
pub(super) fn publish_compose(
    root: &Path,
    target: &str,
    files: &BTreeMap<String, Vec<u8>>,
) -> Result<PathBuf> {
    if !plain_release(target) {
        return Err(UpdateError::usage("invalid Compose release version"));
    }
    let directory = root.join(target);
    crate::credentials::reject_ancestor_redirects(&directory).map_err(|_| {
        UpdateError::usage(
            "the Compose bundle path contains a redirect; restore its original directory and retry",
        )
    })?;
    let agrees = || -> bool {
        files.iter().all(|(name, bytes)| {
            fs::symlink_metadata(directory.join(name)).is_ok_and(|m| m.is_file())
                && fs::read(directory.join(name)).ok().as_ref() == Some(bytes)
        }) && fs::read_dir(&directory).is_ok_and(|d| d.count() == files.len())
    };
    if directory.exists() {
        return if agrees() {
            Ok(directory)
        } else {
            Err(UpdateError::usage(
                "the immutable Compose bundle differs from its verified release; preserve it for rollback and restore the original bundle before retrying",
            ))
        };
    }
    fs::create_dir_all(root)?;
    let staging = root.join(format!(
        ".compose-{target}-{}",
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir(&staging)?;
    let result = (|| -> Result<()> {
        for (name, bytes) in files {
            seams::write_atomic(&staging.join(name), bytes)?;
        }
        fs::rename(&staging, &directory)?;
        Ok(())
    })();
    if staging.exists() {
        let _ = fs::remove_dir_all(&staging);
    }
    if result.is_err() && directory.exists() && agrees() {
        return Ok(directory);
    }
    result?;
    Ok(directory)
}

impl Update {
    fn missing_release_asset(&self, target: &str) -> UpdateError {
        // A missing asset can mean either an older release or a nonexistent
        // tag. Only a confirmed published release justifies the Python remedy.
        let verify = || -> Result<()> {
            let base = std::env::var("PSEUDOLIFE_RELEASE_API_BASE_URL").unwrap_or_else(|_| {
                "https://api.github.com/repos/Pseudogiant-xr/Pseudolife-MCP/releases/tags".into()
            });
            let parsed = reqwest::Url::parse(&base)
                .map_err(|_| UpdateError::usage("invalid release metadata URL"))?;
            if parsed.scheme() != "https"
                && !(parsed.scheme() == "http"
                    && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "::1")))
            {
                return Err(UpdateError::usage(
                    "release metadata requires HTTPS (or a loopback fixture endpoint)",
                ));
            }
            let tag = format!("v{target}");
            let Some(bytes) = seams::download(&format!("{}/{tag}", base.trim_end_matches('/')))?
            else {
                return Err(UpdateError::usage(format!(
                    "release {tag} is not published; choose a published version with --tag <version>; nothing was deployed"
                )));
            };
            let release: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
            if release["tag_name"] != tag || release["draft"] != false {
                return Err(UpdateError::usage(
                    "cannot verify the published release; check --tag and retry; nothing was deployed",
                ));
            }
            Ok(())
        };
        verify().err().unwrap_or_else(|| UpdateError::usage(
            "this release does not publish the native Compose bundle and checksums; use the Python updater, or --tag a release that ships pseudolife-compose-<version>.tar.gz; nothing was deployed",
        ))
    }
    fn release_compose(&self, target: &str) -> Result<PathBuf> {
        let base = std::env::var("PSEUDOLIFE_RELEASE_BASE_URL").unwrap_or_else(|_| {
            "https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/download".into()
        });
        let parsed = reqwest::Url::parse(&base)
            .map_err(|_| UpdateError::usage("invalid release asset URL"))?;
        if parsed.scheme() != "https"
            && !(parsed.scheme() == "http"
                && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "::1")))
        {
            return Err(UpdateError::usage(
                "release assets require HTTPS (or a loopback fixture endpoint)",
            ));
        }
        let base = format!("{}/v{target}", base.trim_end_matches('/'));
        let sums = seams::download(&format!("{base}/SHA256SUMS"))?
            .ok_or_else(|| self.missing_release_asset(target))?;
        let archive = seams::download(&format!("{base}/pseudolife-compose-{target}.tar.gz"))?
            .ok_or_else(|| self.missing_release_asset(target))?;
        let files = verified_compose(target, &sums, &archive)?;
        publish_compose(&compose_root(), target, &files)
    }
    fn release_context(&self, target: &str, engine: &str) -> Result<Context> {
        let (code, out) = self.docker_call(&["inspect", DAEMON_CONTAINER]);
        let running=serde_json::from_str::<Value>(&out).ok().and_then(|v|v.as_array().and_then(|a|a.first()).cloned()).filter(|_|code==0).ok_or_else(||UpdateError::usage("cannot inspect the daemon's Compose project; start it and retry; nothing was changed"))?;
        let labels = &running["Config"]["Labels"];
        let list = |key: &str| -> Vec<PathBuf> {
            labels[key]
                .as_str()
                .unwrap_or("")
                .split(',')
                .filter(|s| !s.is_empty())
                .map(PathBuf::from)
                .collect()
        };
        let labelled = list("com.docker.compose.project.environment_file");
        let working = labels["com.docker.compose.project.working_dir"]
            .as_str()
            .map(PathBuf::from);
        let env_files = if let Some(file) = &self.o.env_file {
            vec![file.clone()]
        } else if !labelled.is_empty() {
            labelled.clone()
        } else {
            working
                .as_ref()
                .map(|d| d.join(".env"))
                .filter(|p| p.is_file())
                .into_iter()
                .collect()
        };
        if env_files.iter().any(|p| !p.is_file()) {
            return Err(UpdateError::usage(
                "the daemon's env file no longer exists; recreating without it would reset credentials and volume names. Name the original file with --env-file. Nothing was changed",
            ));
        }
        let existing = list("com.docker.compose.project.config_files");
        let running_rust = existing
            .iter()
            .find(|p| {
                p.file_name().and_then(|n| n.to_str()) == Some("docker-compose.rust-ghcr.yml")
                    && p.is_file()
            })
            .cloned();
        let project_dir = working.or_else(|| {
            existing.first().and_then(|p| p.parent()).map(Path::to_owned)
        }).ok_or_else(|| UpdateError::usage(
            "cannot resolve the original Compose project directory; restore its project labels before updating; nothing was changed"
        ))?;
        if existing.iter().any(|p| !p.is_file()) && env_files.is_empty() {
            return Err(UpdateError::usage(
                "the daemon's compose files are gone and no env file was found; name the original file with --env-file. Nothing was changed",
            ));
        }
        // A project file that is neither packaged nor one of the updater's
        // own overlays holds this stack's settings: if it is missing here (a
        // stack manager's path inside its own container), the release's files
        // would replace that configuration, so refuse, as the Python updater does.
        let unavailable: Vec<String> = existing
            .iter()
            .filter(|p| !p.is_file())
            .filter(|p| {
                !p.file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(engine_overlay)
                    && !packaged_compose(p)
            })
            .map(|p| p.display().to_string())
            .collect();
        if !unavailable.is_empty() {
            let project = labels["com.docker.compose.project"]
                .as_str()
                .unwrap_or("pseudolife-mcp");
            return Err(UpdateError::usage(format!(
                "compose project {project:?} has files unavailable on this host: {}. Their settings cannot be recovered from the env file, and the release's Compose files would replace this stack's configuration. Restore those files at those paths (Portainer: Stacks > {project} > Editor), then retry. Nothing was changed",
                unavailable.join(", ")
            )));
        }
        // Release Compose is always taken from the checksum-verified archive.
        // Keep project-specific overrides, but never stale engine/candidate
        // overlays from a prior activation, which could override the new tag.
        let directory = self.release_compose(target)?;
        // Only a packaged copy of a shipped file is replaced by the archive's.
        // A stack manager's own file that shares a shipped name (a Portainer
        // stack's docker-compose.yml) holds that stack's settings and stays.
        let mut kept_base = false;
        let mut kept_ghcr = false;
        let mut kept = Vec::new();
        for path in existing.into_iter().filter(|p| p.is_file()) {
            let Some(name) = path.file_name().and_then(|n| n.to_str()).map(str::to_owned) else {
                continue;
            };
            if engine_overlay(&name) {
                continue;
            }
            if COMPOSE_FILES.contains(&name.as_str()) {
                if packaged_compose(&path) {
                    continue;
                }
                kept_base |= name == "docker-compose.yml";
                kept_ghcr |= name == "docker-compose.ghcr.yml";
            }
            kept.push(path);
        }
        // Kept files stay in label order; the archive supplies a base and
        // the ghcr overlay only where the project kept none of its own.
        let mut files = Vec::new();
        if !kept_base {
            files.push(directory.join("docker-compose.yml"));
        }
        files.extend(kept);
        if !kept_ghcr {
            files.push(directory.join("docker-compose.ghcr.yml"));
        }
        let leaving_rust = engine != "rust" && labels[engine::ENGINE_LABEL] == "rust";
        let identity_overlay = leaving_rust.then(|| {
            running_rust
                .clone()
                .unwrap_or_else(|| directory.join("docker-compose.rust-ghcr.yml"))
        });
        if engine == "rust" {
            files.push(directory.join("docker-compose.rust-ghcr.yml"));
        }
        let mut args = vec![
            "-p".into(),
            labels["com.docker.compose.project"]
                .as_str()
                .unwrap_or("pseudolife-mcp")
                .into(),
        ];
        args.extend([
            "--project-directory".into(),
            project_dir.to_string_lossy().into_owned(),
        ]);
        args.extend(compose_args(&files, &env_files));
        let identity_args = identity_overlay.map(|overlay| {
            let mut identity = args.clone();
            identity.extend(compose_args(&[overlay], &[]));
            identity
        });
        let checkout = [
            project_dir.clone(),
            project_dir.parent().unwrap_or(&project_dir).to_owned(),
        ]
        .into_iter()
        .find(|p| p.join("pyproject.toml").is_file() && p.join("ops").is_dir());
        Ok(Context {
            args,
            env_files,
            checkout,
            running,
            running_rust,
            identity_args,
        })
    }
    pub fn deploy_release(
        &mut self,
        current: Option<String>,
        mut before_recreate: Option<&mut dyn FnMut() -> Result<()>>,
        previous_digest: Option<String>,
    ) -> Result<()> {
        if !self.o.clients_only && engine::selected_checkout(self)?.is_some() {
            return Err(UpdateError::usage(
                "this host selected a checkout engine; run update --checkout <repo>; release updates leave it unchanged",
            ));
        }
        let target = self.target_version()?;
        self.report.target = Some(target.clone());
        let before = self.daemon_health();
        let current = current
            .or_else(|| {
                before
                    .as_ref()
                    .and_then(|v| v["version"].as_str().map(str::to_owned))
            })
            .or_else(|| {
                self.running_image()
                    .1
                    .and_then(|v| v.rsplit_once(':').map(|(_, v)| v.to_owned()))
                    .filter(|v| plain_release(v))
            });
        let previous = previous_digest.or_else(|| {
            before
                .as_ref()
                .and_then(|v| v["hooks_digest"].as_str().map(str::to_owned))
        });
        if !self.after_import {
            // The import legs report against the schema the update started from.
            self.schema_before = before.as_ref().and_then(|v| v["schema"].as_i64());
        }
        self.report.current = current.clone();
        self.refuse_downgrade(current.as_deref(), &target)?;
        if current.as_deref().is_some_and(|v| !safe_tag(v)) {
            return Err(UpdateError::usage(
                "the running daemon reported an invalid version; restore its previous image before retrying; nothing was deployed",
            ));
        }
        if self.o.clients_only {
            self.refuse_checkout_shadow(&target)?;
            self.clients(None, &format!("pseudolife-mcp=={target}"), before.as_ref());
            return Ok(());
        }
        let labels = engine::host_labels(self, DAEMON_CONTAINER);
        let selected = labels[engine::ENGINE_LABEL].as_str().unwrap_or("python");
        let choice = self.o.engine.clone().unwrap_or_else(|| selected.to_owned());
        // Engine selection, and every update of a Rust host, records an
        // accepted activation so the next update can verify it.
        let activating = self.o.engine.is_some() || choice == "rust";
        if current.as_deref() == Some(&target) && !self.o.reinstall && choice == selected {
            self.step(format!("the daemon already runs {target}; nothing to do (--reinstall to recreate it anyway, --clients-only for the shim and plugin)"));
            return Ok(());
        }
        let context = self.release_context(&target, &choice)?;
        let env = compose_environment(
            &context.env_files,
            &[("PSEUDOLIFE_IMAGE_TAG".into(), target.clone())]
                .into_iter()
                .collect(),
        );
        // Resolve before any mutation, retaining the running bank and state.
        let mut config_args = vec!["compose".into()];
        config_args.extend(context.args.clone());
        config_args.extend(["config".into(), "--format".into(), "json".into()]);
        let (code, out) = self.command(&config_args, Some(&env), 60, false);
        let config:Value=serde_json::from_str(&out).ok().filter(|_:&Value|code==0).ok_or_else(||UpdateError::usage("cannot resolve the Compose project; check its files and env file, then retry; nothing was changed"))?;
        // Leaving Rust drops the overlay's models volume on purpose: check the
        // running mounts against the project with that overlay, so every other
        // mount is still held to the running daemon's.
        let identity = match &context.identity_args {
            Some(args) => {
                let mut identity_args = vec!["compose".into()];
                identity_args.extend(args.clone());
                identity_args.extend(["config".into(), "--format".into(), "json".into()]);
                let (code, out) = self.command(&identity_args, Some(&env), 60, false);
                serde_json::from_str(&out).ok().filter(|_: &Value| code == 0).ok_or_else(|| {
                    UpdateError::usage("cannot resolve the Compose project with the running Rust overlay; check its files and env file, then retry; nothing was changed")
                })?
            }
            None => config.clone(),
        };
        engine::verify_bank_identity(self, &identity, &context.running)?;
        if self.o.allow_auth_change {
            self.step("using the explicitly requested daemon bearer settings from --env-file");
        }
        self.set_rollback_scope(&config, &context.running)?;
        self.maintainer_context = Some((context.args.clone(), env.clone()));
        let (token, running_token) = if activating {
            engine::verify_identity(self, &identity, &context.running, &self.o.daemon_url)?;
            let token = engine::daemon_token(&config)?;
            // A daemon running without a bearer (--allow-auth-change adding
            // one) answers any bearer: read it with the configured one.
            let running = engine::daemon_token(&running_config(&context.running))
                .unwrap_or_else(|_| token.clone());
            (token, running)
        } else {
            (String::new(), String::new())
        };
        // The running daemon still holds its own bearer (--allow-auth-change
        // replaces it); the configured one is checked after recreation.
        if activating
            && (before.as_ref().is_none_or(|v| v["status"] != "ok")
                || !seams::authenticated_read(&self.o.daemon_url, &running_token))
        {
            return Err(UpdateError::usage(
                "the running daemon did not pass an authenticated bank read; use the previous update's rollback commands before retrying; no rollback tag was moved",
            ));
        }
        if choice == "rust" {
            let pg = engine::container(self, &context.args, Some(&env), "pseudolife-pg")?;
            let pg_env = &config["services"]["pseudolife-pg"]["environment"];
            let user = pg_env["POSTGRES_USER"]
                .as_str()
                .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL user"))?;
            let db = pg_env["POSTGRES_DB"]
                .as_str()
                .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL database"))?;
            let pending = engine::import_pending(
                self,
                &pg,
                user,
                db,
                context.running["Id"].as_str().unwrap_or(DAEMON_CONTAINER),
            )?;
            let host_rust = context.running["Config"]["Labels"][engine::ENGINE_LABEL] == "rust";
            if let Some(schema) = pending {
                if self.after_import {
                    // The Python leg already changed the host: a failure, not a refusal.
                    return Err(UpdateError::new(
                        "the Python daemon did not import memory_state/weights.pt into cms_counters, so the host stays on Python at the target. If its log (docker logs pseudolife-mcp-daemon) says the counters cannot be read, the file is unreadable: move memory_state/weights.pt aside (the counters then restart from zero), then retry update --engine rust",
                    ));
                }
                // Only a readable schema below v57, or a pending import, takes the
                // import leg: an unreadable one would fail the Python daemon too.
                if host_rust || schema < 0 {
                    return Err(UpdateError::usage(
                        "Rust activation requires schema v57 or later and imported counters: run update --engine python, let the Python daemon import memory_state/weights.pt into cms_counters, then retry; nothing was changed",
                    ));
                }
                // G2-K: the Rust daemon never reads weights.pt. The Python
                // daemon at the target imports it at startup, so update to that
                // first (its own backup, rollback tag and gate), then move on.
                model_volumes(&config)?;
                let shown = if schema >= 0 {
                    format!("v{schema}")
                } else {
                    "an unreadable schema".into()
                };
                self.step(format!(
                    "this bank ({shown}) still keeps its CMS counters in memory_state/weights.pt: moving to the Python daemon {target} first, which imports them, then to the Rust daemon"
                ));
                // The Rust image must exist before the Python leg changes anything.
                let rust_image = format!("{RUST_IMAGE}:{target}");
                self.step(format!("pulling {rust_image}..."));
                if self
                    .command(&["pull".into(), rust_image.clone()], None, 7200, true)
                    .0
                    != 0
                {
                    return Err(UpdateError::new(format!(
                        "docker pull {rust_image} failed; nothing was changed. Is a Rust daemon image published for {target}? Use --engine python to stay on the Python daemon"
                    )));
                }
                self.after_import = true;
                let requested = self.o.engine.replace("python".into());
                // A Python daemon already at the target must still restart to import.
                let reinstall = std::mem::replace(&mut self.o.reinstall, true);
                // Both legs tag a Python image of the same version: keep them apart.
                let suffix = self.o.rollback_suffix.clone();
                if !suffix.is_empty() {
                    self.o.rollback_suffix = format!("{suffix}-pre-import");
                }
                let first = self.deploy_release(current.clone(), before_recreate, previous.clone());
                self.o.reinstall = reinstall;
                self.o.rollback_suffix = suffix;
                if let Err(error) = first {
                    self.o.engine = requested;
                    return Err(error);
                }
                // Leg 2 is Rust at this target, whatever chose it, without asking
                // the release index again.
                let before_import = self.report.rollback.clone();
                self.o.engine = Some("rust".into());
                let tag = self.o.tag.replace(target.clone());
                let second = self.deploy_release(Some(target.clone()), None, previous);
                self.o.engine = requested;
                self.o.tag = tag;
                if self.report.rollback.is_object() {
                    self.report.rollback["before_import"] = before_import;
                }
                return second;
            }
            // Refuse a misplaced model directory before the pull.
            model_volumes(&config)?;
        }
        let repository = if choice == "rust" {
            RUST_IMAGE
        } else {
            PYTHON_IMAGE
        };
        let image = format!("{repository}:{target}");
        disk::host(&data_dir())?;
        let pg = storage_pg(self.o.no_backup, || {
            engine::container(self, &context.args, Some(&env), "pseudolife-pg")
        })?;
        self.disk_storage(
            context.running["Id"].as_str().unwrap_or(DAEMON_CONTAINER),
            pg.as_deref(),
            context.running["Image"].as_str(),
        )?;
        self.step(format!("pulling {image}..."));
        if self
            .command(&["pull".into(), image.clone()], None, 7200, true)
            .0
            != 0
        {
            return Err(UpdateError::new(
                if choice == "rust" && self.o.engine.is_none() {
                    format!(
                        "docker pull {image} failed; nothing was changed: this host keeps its current Rust daemon. Retry once {target} publishes its Rust daemon image, or move to it on Python with --engine python"
                    )
                } else if choice == "rust" {
                    format!(
                        "docker pull {image} failed; nothing was changed. Is a Rust daemon image published for {target}? Use --engine python to stay on the Python daemon"
                    )
                } else {
                    format!(
                        "docker pull {image} failed; nothing was changed. Is {target} a published release?"
                    )
                },
            ));
        }
        let probe = if choice == "rust" {
            vec![
                "run".into(),
                "--rm".into(),
                "--network".into(),
                "none".into(),
                "--entrypoint".into(),
                "/usr/local/bin/pseudolife-mcp".into(),
                image.clone(),
                "update".into(),
                "--schema-version".into(),
            ]
        } else {
            vec![
                "run".into(),
                "--rm".into(),
                "--network".into(),
                "none".into(),
                "--entrypoint".into(),
                "sh".into(),
                image.clone(),
                "-c".into(),
                "cat /app/pseudolife_memory/storage/schema.py".into(),
            ]
        };
        let schema = if activating {
            let (code, metadata) = self.command(&probe, None, 60, false);
            Some(if choice=="rust"{metadata.trim().parse::<i64>().ok()}else{regex::Regex::new(r"(?m)^SCHEMA_META_VERSION\s*=\s*([0-9]+)").expect("schema").captures(&metadata).and_then(|c|c[1].parse::<i64>().ok())}.filter(|_|code==0)
                .ok_or_else(||UpdateError::usage("cannot read the pulled image's schema version; verify its published CLI/image contract and retry; nothing was deployed"))?)
        } else {
            None
        };
        if let (Some(image_schema), Some(bank)) = (schema, self.schema_before)
            && bank > image_schema
        {
            return Err(UpdateError::usage(format!(
                "the bank is at schema v{bank}, newer than {image}'s v{image_schema}, which would refuse it; update to a release at schema v{bank} or later; nothing was changed"
            )));
        }
        let daemon = context.running["Id"].as_str().unwrap_or(DAEMON_CONTAINER);
        if choice == "rust" {
            // A first start would otherwise download the models inside the
            // health gate. The running daemon keeps serving meanwhile.
            self.provision_models(&image, &config)?;
        }
        let backups = if self.o.no_backup {
            self.warn("skipping backup (--no-backup)");
            json!({})
        } else if let Some(repo) = context
            .checkout
            .as_deref()
            .filter(|repo| self.checkout_script(repo, "backup").is_some())
        {
            let pg_env = &config["services"]["pseudolife-pg"]["environment"];
            let user = pg_env["POSTGRES_USER"]
                .as_str()
                .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL user"))?;
            let db = pg_env["POSTGRES_DB"]
                .as_str()
                .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL database"))?;
            self.project_backup(
                repo,
                pg.as_deref().expect("backup discovery returns PostgreSQL"),
                daemon,
                user,
                db,
            )?
        } else {
            self.builtin_backup(
                pg.as_deref().expect("backup discovery returns PostgreSQL"),
                daemon,
                &data_dir().join("backups"),
            )?
        };
        if let Some(callback) = before_recreate.as_mut() {
            callback()?;
        }
        let (running_id, _) = self.running_image();
        let running_id = running_id.ok_or_else(|| {
            UpdateError::new(
                "the daemon container vanished during the backup; nothing was deployed",
            )
        })?;
        if context.running["Image"].as_str() != Some(&running_id) {
            return Err(UpdateError::new(
                "the running daemon changed during backup; retry; nothing was deployed",
            ));
        }
        let old_engine = context.running["Config"]["Labels"][engine::ENGINE_LABEL]
            .as_str()
            .unwrap_or("python");
        let suffix = self.rollback_suffix();
        let rollback = format!(
            "{}:{}-{suffix}",
            if old_engine == "rust" {
                RUST_IMAGE
            } else {
                PYTHON_IMAGE
            },
            current.as_deref().unwrap_or("previous")
        );
        engine::tag_validated(self, &running_id, &rollback)?;
        let old_service = engine::rollback_service(&context.running, &rollback, None);
        let rollback_file = data_dir().join("update-engine").join(format!(
            "docker-compose.rollback-{}-{}.json",
            stamp(),
            uuid::Uuid::new_v4().simple()
        ));
        engine::write_overlay(&rollback_file, &old_service)?;
        let mut restore_args = context.args.clone();
        if old_engine == "rust" && choice != "rust" {
            // The restored Rust daemon needs its overlay's model volume again.
            // Prefer the overlay the running daemon was created with: an older
            // target release's overlay may lack the model volume.
            let overlay = context.running_rust.clone().unwrap_or_else(|| {
                compose_root()
                    .join(&target)
                    .join("docker-compose.rust-ghcr.yml")
            });
            restore_args.extend(["-f".into(), overlay.to_string_lossy().into_owned()]);
        }
        self.rollback_lines = engine::rollback_commands(
            &restore_args,
            &context.env_files,
            &rollback_file,
            &engine::release_accepted(),
            None,
            Some(&target),
        );
        self.rollback_lines.extend(self.schema_rollback_note());
        self.report.rollback =
            json!({"tag":rollback,"state":"tagged","overlay":rollback_file,"backup":backups});
        self.prune_rollbacks(PYTHON_IMAGE);
        self.prune_rollbacks(RUST_IMAGE);
        let activation = uuid::Uuid::new_v4().simple().to_string();
        let mut service = json!({"image":image});
        if activating {
            service["labels"] = json!({(engine::ENGINE_LABEL):choice,(engine::ACTIVATION_LABEL):activation,(engine::RELEASE_LABEL):target});
        }
        let candidate = data_dir()
            .join("update-engine")
            .join(format!("docker-compose.engine-candidate-{activation}.json"));
        engine::write_overlay(&candidate, &service).map_err(|e| self.rollback_error(e.message))?;
        let result = (|| -> Result<()> {
            let mut args = vec!["compose".into()];
            args.extend(context.args.clone());
            args.extend([
                "-f".into(),
                candidate.to_string_lossy().into_owned(),
                "up".into(),
                "-d".into(),
                "--no-deps".into(),
                "--no-build".into(),
                DAEMON_SERVICE.into(),
            ]);
            self.step(format!(
                "recreating the daemon container on {image} (Postgres + extractor untouched)..."
            ));
            if self.command(&args, Some(&env), 3600, true).0 != 0 {
                return Err(UpdateError::new("daemon recreate failed"));
            }
            let health = if activating {
                self.wait_health(Some(&target), schema, &token, false)?
            } else {
                self.wait_plain_health(Some(&target))?
            };
            if activating {
                engine::write_overlay(&engine::release_accepted(), &service)?;
            }
            if !self.o.daemon_only {
                self.clients(None, &format!("pseudolife-mcp=={target}"), Some(&health));
            } else {
                self.hooks_changed_note(previous.as_deref(), Some(&health));
            }
            Ok(())
        })();
        if fs::remove_file(&candidate).is_err() {
            self.warn("could not remove the temporary engine overlay; the rollback instructions remain usable");
        }
        result.map_err(|e| self.rollback_error(e.message))
    }
    pub fn wait_health(
        &mut self,
        version: Option<&str>,
        schema: Option<i64>,
        token: &str,
        shadow: bool,
    ) -> Result<Value> {
        self.wait_health_with(version, schema, Some(token), shadow)
    }
    fn wait_plain_health(&mut self, version: Option<&str>) -> Result<Value> {
        self.wait_health_with(version, None, None, false)
    }
    fn wait_health_with(
        &mut self,
        version: Option<&str>,
        schema: Option<i64>,
        token: Option<&str>,
        shadow: bool,
    ) -> Result<Value> {
        self.step("waiting for the daemon to report healthy...");
        for attempt in 0..self.o.health_retries {
            #[cfg(unix)]
            if super::signals::checkpoint() != 0 {
                return Err(UpdateError::new("health check interrupted"));
            }
            let health = seams::fetch_health_json(
                &format!("{}/health", self.o.daemon_url.trim_end_matches('/')),
                3,
            );
            if let Some(health) = health
                && health["status"] == "ok"
                && version.is_none_or(|v| health["version"] == v)
                && schema.is_none_or(|v| health["schema"] == v)
                && (token.is_none() || !truthy(&health["counters_import_required"]))
                && (!shadow
                    || (health["read_only"] == true
                        && health["ready"] == true
                        && health["db"] == "ok"))
                && token.is_none_or(|token| seams::authenticated_read(&self.o.daemon_url, token))
            {
                if self
                    .schema_before
                    .zip(health["schema"].as_i64())
                    .is_some_and(|(a, b)| b > a)
                {
                    self.report.rollback["restore_required"] = json!(true);
                }
                self.step(format!(
                    "healthy. version={} schema={} persist_errors={}",
                    health["version"].as_str().unwrap_or("unknown"),
                    health["schema"],
                    health["persist_errors"]
                ));
                if !self.o.as_json && !self.rollback_lines.is_empty() {
                    let _ = output("    Rolled-back deploy if ever needed:\n", false);
                    for line in &self.rollback_lines {
                        let _ = output(&format!("{line}\n"), false);
                    }
                    let _ = std::io::stdout().flush();
                }
                return Ok(health);
            }
            #[cfg(unix)]
            if super::signals::checkpoint() != 0 {
                return Err(UpdateError::new("health check interrupted"));
            }
            if attempt + 1 < self.o.health_retries {
                let delay = std::time::Duration::from_millis(self.o.health_delay_ms);
                #[cfg(unix)]
                super::signals::sleep(delay);
                #[cfg(not(unix))]
                std::thread::sleep(delay);
            }
        }
        let failure = if token.is_some() {
            "the daemon failed its expected version, schema or authenticated-read health gate at the configured endpoint; the client side was not moved"
        } else {
            "the daemon did not report healthy at the expected version at the configured endpoint; inspect docker logs and use the rollback commands below; the client side was not moved"
        };
        // Shadow verification uses its temporary endpoint, not the main daemon's port.
        let ports = if !shadow {
            self.maintainer_context.as_ref().and_then(|(compose, env)| {
                let daemon = engine::container(self, compose, Some(env), DAEMON_SERVICE).ok()?;
                let (code, out) = self.docker_call(&["port", &daemon, "8765/tcp"]);
                (code == 0).then_some(out)
            })
        } else {
            None
        };
        Err(UpdateError::new(format!(
            "{failure}\n{}",
            health_endpoint_hint(ports.as_deref().unwrap_or(""))
        )))
    }
    pub fn deploy_checkout(&mut self, repo: &Path) -> Result<()> {
        let repo = fs::canonicalize(repo)
            .map(seams::command_path)
            .map_err(|_| UpdateError::usage("--checkout must name a Pseudolife-MCP checkout"))?;
        let file = repo.join("ops/docker-compose.yml");
        if !file.is_file() {
            return Err(UpdateError::usage(
                "docker-compose.yml is missing: --checkout must name a Pseudolife-MCP checkout",
            ));
        }
        let tree = tree_state(&repo);
        if tree.dirty != "false" && !self.o.allow_dirty {
            for line in tree.lines.iter().take(20) {
                self.warn(line);
            }
            return Err(UpdateError::new(
                "commit, stash or remove the listed paths, or re-run with --allow-dirty to deploy this tree as it is (stamped dirty, or unknown when git cannot describe it)",
            ));
        }
        disk::host(&repo)?;
        let env_file = repo.join("ops/.env");
        let compose = compose_args(
            &[file.clone(), repo.join("ops/docker-compose.override.yml")],
            std::slice::from_ref(&env_file),
        );
        let env = compose_environment(&[env_file], &engine::build_environment(&tree));
        let before = self.daemon_health();
        self.schema_before = before.as_ref().and_then(|v| v["schema"].as_i64());
        // Resolve this project's container and bank before backup. Plain
        // deployments may reach health through a proxy or a non-loopback bind.
        let mut discovery = vec!["compose".into()];
        discovery.extend(compose.clone());
        discovery.extend(["config".into(), "--format".into(), "json".into()]);
        let (code, out) = self.command(&discovery, Some(&env), 60, false);
        let config: Value = serde_json::from_str(&out)
            .ok()
            .filter(|_: &Value| code == 0)
            .ok_or_else(|| {
                UpdateError::usage(
                    "cannot resolve the project's Compose configuration; restore its files and retry; nothing was deployed",
                )
            })?;
        // A checkout can name a different Compose project. Never read a global
        // daemon or back up a different bank while recreating this project.
        let daemon = engine::container(self, &compose, Some(&env), DAEMON_SERVICE)?;
        let running = engine::inspect(self, &daemon)?;
        engine::verify_checkout_identity(self, &config, &running, &daemon)?;
        let pg = storage_pg(self.o.no_backup, || {
            engine::container(self, &compose, Some(&env), "pseudolife-pg")
        })?;
        self.disk_storage(&daemon, pg.as_deref(), running["Image"].as_str())?;
        self.set_rollback_scope(&config, &running)?;
        self.maintainer_context = Some((compose.clone(), env.clone()));
        if self.o.no_backup {
            self.backup(None)?;
        } else {
            let pg = pg.as_deref().expect("backup discovery returns PostgreSQL");
            if self.checkout_script(&repo, "backup").is_some() {
                let pg_env = &config["services"]["pseudolife-pg"]["environment"];
                let user = pg_env["POSTGRES_USER"].as_str().ok_or_else(|| {
                    UpdateError::usage("cannot resolve the project's PostgreSQL user; set POSTGRES_USER in its Compose environment and retry; nothing was deployed")
                })?;
                let db = pg_env["POSTGRES_DB"].as_str().ok_or_else(|| {
                    UpdateError::usage("cannot resolve the project's PostgreSQL database; set POSTGRES_DB in its Compose environment and retry; nothing was deployed")
                })?;
                self.project_backup(&repo, pg, &daemon, user, db)?;
            } else {
                self.builtin_backup(pg, &daemon, &data_dir().join("backups"))?;
            }
        }
        let text = fs::read_to_string(&file)?;
        disk::host(&repo)?;
        self.disk_storage(&daemon, pg.as_deref(), running["Image"].as_str())?;
        let image = text
            .lines()
            .find_map(|l| {
                l.trim()
                    .strip_prefix("image:")
                    .map(str::trim)
                    .filter(|s| s.starts_with("pseudolife-daemon:"))
                    .map(str::to_owned)
            })
            .ok_or_else(|| {
                UpdateError::new("could not find the daemon image tag in docker-compose.yml")
            })?;
        let (code, out) = self.docker_call(&["image", "inspect", "-f", "{{.Id}}", &image]);
        let tag_id = (code == 0).then(|| out.trim().to_owned());
        let (running, reference) = self.running_image_for(&daemon);
        let foreign = reference
            .as_deref()
            .is_some_and(|r| r.split(':').next() != image.split(':').next());
        let suffix = self.rollback_suffix();
        let rollback = format!("{image}-{suffix}");
        if let Some(source) = if foreign {
            running.as_ref()
        } else {
            tag_id.as_ref()
        } {
            if !foreign
                && running.as_ref().is_some_and(|id| id != source)
                && !self.o.force_rollback_tag
            {
                self.warn("REFUSING to move the rollback tag: the version tag is NOT the image the running daemon deployed; existing rollback tags are untouched");
                self.report.rollback = json!({"tag":rollback,"state":"kept","image_tag":image});
                self.rollback_lines = vec![
                    "(the rollback tag was NOT moved this run - see the warning above)".into(),
                    "Pick the newest surviving rollback tag and redeploy it:".into(),
                    format!(
                        "docker image ls {}",
                        seams::quote(image.split(':').next().unwrap_or("pseudolife-daemon"))
                    ),
                    format!(
                        "docker tag <surviving-rollback-tag> {}",
                        seams::quote(&image)
                    ),
                    format!(
                        "docker compose {} up -d --no-deps --no-build {DAEMON_SERVICE}",
                        display(&compose)
                    ),
                ];
            } else {
                engine::tag_validated(self, source, &rollback)?;
                self.report.rollback = json!({"tag":rollback,"state":"tagged","image_tag":image});
                self.rollback_lines = vec![
                    format!(
                        "docker tag {} {}",
                        seams::quote(&rollback),
                        seams::quote(&image)
                    ),
                    format!(
                        "docker compose {} up -d --no-deps --no-build {DAEMON_SERVICE}",
                        display(&compose)
                    ),
                ];
            }
        } else {
            self.warn("this deploy has NO rollback image. Rolling back means rebuilding the previous code");
            self.report.rollback = json!({"tag":rollback,"state":"none","image_tag":image});
            self.rollback_lines = vec![
                "(no rollback image exists for this deploy - nothing was tagged)".into(),
                "Restore the previous checkout and rebuild the daemon to roll back.".into(),
                format!(
                    "docker compose {} up -d --no-deps --build {DAEMON_SERVICE}",
                    display(&compose)
                ),
            ];
        }
        if !self.rollback_lines.is_empty() {
            self.rollback_lines.extend(self.schema_rollback_note());
        }
        self.prune_rollbacks(image.split(':').next().unwrap_or("pseudolife-daemon"));
        if tree_state(&repo) != tree {
            return Err(UpdateError::new(
                "the checkout changed during this deploy; nothing was built. Re-run the deploy",
            ));
        }
        let mut args = vec!["compose".into()];
        args.extend(compose.clone());
        args.extend([
            "up".into(),
            "-d".into(),
            "--no-deps".into(),
            "--build".into(),
            DAEMON_SERVICE.into(),
        ]);
        self.step("rebuilding the daemon only (Postgres + extractor untouched)...");
        self.step(format!(
            "build stamp: commit {}, dirty={}",
            tree.sha, tree.dirty
        ));
        if self.command(&args, Some(&env), 7200, true).0 != 0 {
            return Err(self.rollback_error("daemon rebuild failed"));
        }
        let health = self
            .wait_plain_health(None)
            .map_err(|e| self.rollback_error(e.message))?;
        // A forwarded health URL may name another healthy daemon. Verify the
        // recreated service itself before reporting this project as healthy.
        let recreated = engine::container(self, &compose, Some(&env), DAEMON_SERVICE)
            .map_err(|_| self.rollback_error("cannot identify exactly one recreated checkout daemon; inspect this project's Compose services and use the rollback commands below before retrying"))?;
        let (code, state) = self.docker_call(&["inspect", "-f", "{{.State.Status}}", &recreated]);
        if code != 0 || state.trim() != "running" {
            return Err(self.rollback_error("the recreated checkout daemon is not running; inspect this project's container logs before retrying"));
        }
        self.checkout_post_deploy(
            &repo,
            before.as_ref().and_then(|v| v["hooks_digest"].as_str()),
            &health,
        )?;
        self.prune_cache(Some(&repo));
        Ok(())
    }
}

/// The running daemon's environment shaped as a Compose config, so its own
/// bearer is read the same way as the configured one.
fn running_config(running: &Value) -> Value {
    let mut env = serde_json::Map::new();
    for item in running["Config"]["Env"].as_array().into_iter().flatten() {
        if let Some((key, value)) = item.as_str().and_then(|s| s.split_once('=')) {
            env.insert(key.to_owned(), json!(value));
        }
    }
    json!({"services": {DAEMON_SERVICE: {"environment": env}}})
}

/// `(docker run flag, value, Compose volume key)`: one mount holding the
/// models. A named volume uses `--mount` (it keeps a subpath); a bind uses
/// `-v`, which creates a missing host path as Compose does.
type ModelMount = (String, String, Option<String>);

/// A `docker run --mount` value. Docker parses it as CSV, so a field holding
/// a comma or quote is quoted, with its quotes doubled.
fn mount_spec(fields: &[(&str, &str)]) -> String {
    fields
        .iter()
        .map(|(key, value)| {
            let field = format!("{key}={value}");
            if field.contains([',', '"', '\n', '\r']) {
                format!("\"{}\"", field.replace('"', "\"\""))
            } else {
                field
            }
        })
        .collect::<Vec<_>>()
        .join(",")
}

/// The models directory and the mounts covering it, or None when the daemon
/// names its own ONNX model directory.
fn model_volumes(config: &Value) -> Result<Option<(String, Vec<ModelMount>)>> {
    let service = &config["services"][DAEMON_SERVICE];
    let env = &service["environment"];
    if env["PSEUDOLIFE_DAEMON_ONNX_DIR"]
        .as_str()
        .is_some_and(|s| !s.is_empty())
    {
        return Ok(None);
    }
    let models = env["PSEUDOLIFE_DAEMON_MODELS_DIR"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or("/data/models")
        .trim_end_matches('/')
        .to_owned();
    let mut mounts = Vec::new();
    for volume in service["volumes"].as_array().into_iter().flatten() {
        let target = volume["target"]
            .as_str()
            .unwrap_or("")
            .trim_end_matches('/');
        if target.is_empty() || !(models == target || models.starts_with(&format!("{target}/"))) {
            continue;
        }
        let Some(source) = volume["source"].as_str().filter(|s| !s.is_empty()) else {
            continue;
        };
        match volume["type"].as_str() {
            Some("volume") => {
                let name = config["volumes"][source]["name"].as_str().unwrap_or(source);
                let mut fields = vec![("type", "volume"), ("src", name), ("dst", target)];
                if let Some(subpath) = volume["volume"]["subpath"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                {
                    fields.push(("volume-subpath", subpath));
                }
                mounts.push((
                    "--mount".to_owned(),
                    mount_spec(&fields),
                    Some(source.to_owned()),
                ));
            }
            // `-v` creates a missing host path, as Compose does for a bind.
            Some("bind") => mounts.push(("-v".to_owned(), format!("{source}:{target}"), None)),
            _ => {}
        }
    }
    if mounts.is_empty() {
        return Err(UpdateError::usage(format!(
            "the Rust daemon's model directory {models} is on no persistent volume of this project; restore the release Compose files and retry; nothing was changed"
        )));
    }
    Ok(Some((models, mounts)))
}

/// `docker volume create` arguments matching the project's definition, or
/// an error for a missing external volume.
fn volume_create_args(config: &Value, key: &str) -> Result<Vec<String>> {
    let definition = &config["volumes"][key];
    let name = definition["name"].as_str().unwrap_or(key);
    if !definition["external"].is_null() && definition["external"] != false {
        return Err(UpdateError::usage(format!(
            "the external model volume {name} does not exist; create it as the project expects, then retry; nothing was deployed"
        )));
    }
    let project = config["name"].as_str().unwrap_or("pseudolife-mcp");
    let mut args: Vec<String> = vec![
        "volume".into(),
        "create".into(),
        "--label".into(),
        format!("com.docker.compose.project={project}"),
        "--label".into(),
        format!("com.docker.compose.volume={key}"),
    ];
    if let Some(driver) = definition["driver"].as_str() {
        args.extend(["--driver".into(), driver.to_owned()]);
    }
    for (flag, field) in [("--opt", "driver_opts"), ("--label", "labels")] {
        if let Some(map) = definition[field].as_object() {
            for (option, value) in map {
                let value = value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string());
                args.extend([flag.into(), format!("{option}={value}")]);
            }
        }
    }
    args.push(name.to_owned());
    Ok(args)
}

impl Update {
    /// Install the pinned models into the daemon's model volume before backup.
    fn provision_models(&mut self, image: &str, config: &Value) -> Result<()> {
        let Some((models, mounts)) = model_volumes(config)? else {
            self.step(
                "the daemon names its own ONNX model directory; leaving model provisioning to it",
            );
            return Ok(());
        };
        for (_, _, key) in &mounts {
            let Some(key) = key else { continue };
            let name = config["volumes"][key.as_str()]["name"]
                .as_str()
                .unwrap_or(key)
                .to_owned();
            let (code, out) = self.docker_call(&["volume", "inspect", &name]);
            let exists = code == 0
                && serde_json::from_str::<Value>(&out)
                    .ok()
                    .and_then(|v| v.as_array().map(|a| !a.is_empty()))
                    .unwrap_or(false);
            if !exists {
                // Created the way Compose would, so `up` adopts it quietly.
                let args = volume_create_args(config, key)?;
                let borrowed: Vec<&str> = args.iter().map(String::as_str).collect();
                if self.docker_call(&borrowed).0 != 0 {
                    return Err(UpdateError::new(format!(
                        "could not create the model volume {name}; check Docker and retry; nothing was deployed"
                    )));
                }
            }
        }
        self.step(format!(
            "provisioning the Rust daemon's pinned models into {models} (a first activation downloads them)..."
        ));
        let mut args: Vec<String> = vec![
            "run".into(),
            "--rm".into(),
            "-e".into(),
            format!("PSEUDOLIFE_DAEMON_MODELS_DIR={models}"),
        ];
        for (flag, value, _) in &mounts {
            args.extend([flag.clone(), value.clone()]);
        }
        args.extend(
            [
                "--entrypoint",
                "pseudolife-daemon",
                image,
                "provision",
                "models",
                "--if-missing",
            ]
            .into_iter()
            .map(str::to_owned),
        );
        if self.command(&args, None, 3600, true).0 != 0 {
            return Err(UpdateError::new(
                "could not provision the Rust daemon's models; check this host's network access and disk space, then retry; nothing was deployed",
            ));
        }
        Ok(())
    }
}

fn storage_pg(no_backup: bool, resolve: impl FnOnce() -> Result<String>) -> Result<Option<String>> {
    if no_backup {
        Ok(None)
    } else {
        resolve().map(Some)
    }
}

#[cfg(test)]
mod disk_review_tests {
    use super::*;
    fn daemon(env: Value, volumes: Value) -> Value {
        json!({"name": "fixture", "services": {DAEMON_SERVICE: {"environment": env, "volumes": volumes}},
               "volumes": {"pseudolife_data": {"name": "fixture-state"},
                           "pseudolife_rust_models": {"name": "fixture_pseudolife_rust_models"}}})
    }
    #[test]
    fn models_are_provisioned_into_the_volume_the_service_maps_there() {
        let data = json!({"type": "volume", "source": "pseudolife_data", "target": "/data"});
        let models =
            json!({"type": "volume", "source": "pseudolife_rust_models", "target": "/models"});
        let (dir, mounts) = model_volumes(&daemon(
            json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
            json!([data.clone(), models]),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(dir, "/models");
        assert_eq!(
            mounts,
            vec![(
                "--mount".to_owned(),
                "type=volume,src=fixture_pseudolife_rust_models,dst=/models".to_owned(),
                Some("pseudolife_rust_models".to_owned())
            )]
        );
        let (dir, mounts) = model_volumes(&daemon(json!({}), json!([data.clone()])))
            .unwrap()
            .unwrap();
        assert_eq!(
            (dir.as_str(), mounts[0].1.as_str()),
            ("/data/models", "type=volume,src=fixture-state,dst=/data")
        );
        let nested = json!({"type": "volume", "source": "pseudolife_rust_models", "target": "/models",
                            "volume": {"subpath": "cache"}});
        let (_, mounts) = model_volumes(&daemon(
            json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
            json!([nested]),
        ))
        .unwrap()
        .unwrap();
        assert!(mounts[0].1.ends_with(",volume-subpath=cache"));
        assert!(
            model_volumes(&daemon(
                json!({"PSEUDOLIFE_DAEMON_ONNX_DIR": "/opt/models"}),
                json!([data])
            ))
            .unwrap()
            .is_none()
        );
        assert!(
            model_volumes(&daemon(
                json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
                json!([])
            ))
            .is_err()
        );
    }
    #[test]
    fn a_comma_in_a_model_subpath_is_quoted_and_a_bind_keeps_v() {
        let nested = json!({"type": "volume", "source": "pseudolife_rust_models", "target": "/models",
                            "volume": {"subpath": "a,\"b\""}});
        let (_, mounts) = model_volumes(&daemon(
            json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
            json!([nested]),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(
            mounts[0].1,
            "type=volume,src=fixture_pseudolife_rust_models,dst=/models,\"volume-subpath=a,\"\"b\"\"\""
        );
        let bind = json!({"type": "bind", "source": "/srv/models,cache", "target": "/models"});
        let (_, mounts) = model_volumes(&daemon(
            json!({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}),
            json!([bind]),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(
            mounts,
            vec![(
                "-v".to_owned(),
                "/srv/models,cache:/models".to_owned(),
                None
            )]
        );
    }
    #[test]
    fn a_model_volume_is_created_with_its_project_driver_and_options() {
        let mut config = daemon(json!({}), json!([]));
        config["volumes"]["pseudolife_rust_models"] = json!({"name": "fixture_models", "driver": "local",
            "driver_opts": {"type": "none", "o": "bind", "device": "/srv/models"}});
        let args = volume_create_args(&config, "pseudolife_rust_models").unwrap();
        assert_eq!(args.last().map(String::as_str), Some("fixture_models"));
        for expected in [
            "com.docker.compose.project=fixture",
            "com.docker.compose.volume=pseudolife_rust_models",
            "local",
            "device=/srv/models",
            "o=bind",
            "type=none",
        ] {
            assert!(
                args.iter().any(|a| a == expected),
                "{expected} missing from {args:?}"
            );
        }
        config["volumes"]["pseudolife_rust_models"] =
            json!({"name": "fixture_models", "external": true});
        assert!(volume_create_args(&config, "pseudolife_rust_models").is_err());
    }
    #[test]
    fn the_running_bearer_is_read_from_the_container_environment() {
        let running = json!({"Config": {"Env": ["PATH=/bin", "PSEUDOLIFE_MCP_TOKEN=old=bearer"]}});
        assert_eq!(
            engine::daemon_token(&running_config(&running)).unwrap(),
            "old=bearer"
        );
    }
    #[test]
    fn no_backup_storage_probe_does_not_require_project_postgres() {
        let absent = storage_pg(true, || Err(UpdateError::usage("no project PostgreSQL")));
        assert!(matches!(absent, Ok(None)));
        assert!(storage_pg(false, || Err(UpdateError::usage("no project PostgreSQL"))).is_err());
        assert_eq!(
            storage_pg(false, || Ok("selected-pg".into())).unwrap(),
            Some("selected-pg".into())
        );
    }
}
