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
        let sums = seams::download(&format!("{base}/SHA256SUMS"))?;
        let archive = seams::download(&format!("{base}/pseudolife-compose-{target}.tar.gz"))?;
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
        // Release Compose is always taken from the checksum-verified archive.
        // Keep project-specific overrides, but never stale engine/candidate
        // overlays from a prior activation, which could override the new tag.
        let directory = self.release_compose(target)?;
        let mut files = vec![directory.join("docker-compose.yml")];
        for path in existing.into_iter().filter(|p| p.is_file()) {
            if !path.file_name().and_then(|n| n.to_str()).is_some_and(|n| {
                n.starts_with("docker-compose.engine")
                    || n.starts_with("docker-compose.rollback")
                    || COMPOSE_FILES.contains(&n)
            }) {
                files.push(path);
            }
        }
        files.push(directory.join("docker-compose.ghcr.yml"));
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
        self.schema_before = before.as_ref().and_then(|v| v["schema"].as_i64());
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
        if choice == "rust" {
            return Err(UpdateError::usage(
                "Rust images are not published; use --engine rust --checkout <repo> (checkout mode); nothing was changed",
            ));
        }
        let activating = self.o.engine.is_some()
            || (choice != "python" && truthy(&labels[engine::ACTIVATION_LABEL]));
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
        engine::verify_bank_identity(&config, &context.running)?;
        let token = if activating {
            engine::verify_identity(&config, &context.running, &self.o.daemon_url)?;
            engine::daemon_token(&config)?
        } else {
            String::new()
        };
        if activating
            && (before.as_ref().is_none_or(|v| v["status"] != "ok")
                || !seams::authenticated_read(&self.o.daemon_url, &token))
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
            engine::require_imported_with(
                self,
                &pg,
                user,
                db,
                context.running["Id"].as_str().unwrap_or(DAEMON_CONTAINER),
            )?;
        }
        let repository = if choice == "rust" {
            RUST_IMAGE
        } else {
            PYTHON_IMAGE
        };
        let image = format!("{repository}:{target}");
        self.step(format!("pulling {image}..."));
        if self
            .command(&["pull".into(), image.clone()], None, 7200, true)
            .0
            != 0
        {
            return Err(UpdateError::new(format!(
                "docker pull {image} failed; nothing was changed. Is {target} a published release?"
            )));
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
        let pg = engine::container(self, &context.args, Some(&env), "pseudolife-pg")?;
        let pg_env = &config["services"]["pseudolife-pg"]["environment"];
        let user = pg_env["POSTGRES_USER"]
            .as_str()
            .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL user"))?;
        let db = pg_env["POSTGRES_DB"]
            .as_str()
            .ok_or_else(|| UpdateError::usage("cannot resolve PostgreSQL database"))?;
        let daemon = context.running["Id"].as_str().unwrap_or(DAEMON_CONTAINER);
        let backups = if self.o.no_backup {
            self.warn("skipping backup (--no-backup)");
            json!({})
        } else if let Some(repo) = context
            .checkout
            .as_deref()
            .filter(|repo| self.checkout_script(repo, "backup").is_some())
        {
            self.project_backup(repo, &pg, daemon, user, db)?
        } else {
            self.builtin_backup(&pg, daemon, user, db, &data_dir().join("backups"))?
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
        let suffix = if self.o.rollback_suffix.is_empty() {
            format!("pre-update-{}", stamp())
        } else {
            self.o.rollback_suffix.clone()
        };
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
        self.rollback_lines = engine::rollback_commands(
            &context.args,
            &context.env_files,
            &rollback_file,
            &engine::release_accepted(),
            None,
            Some(&target),
        );
        self.rollback_lines.extend(self.schema_rollback_note());
        self.report.rollback =
            json!({"tag":rollback,"state":"tagged","overlay":rollback_file,"backup":backups});
        self.prune_rollbacks(None, PYTHON_IMAGE);
        self.prune_rollbacks(None, RUST_IMAGE);
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
            let health = fetch_json(
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
            if attempt + 1 < self.o.health_retries {
                std::thread::sleep(std::time::Duration::from_millis(self.o.health_delay_ms));
            }
        }
        Err(UpdateError::new(if token.is_some() {
            "the daemon failed its expected version, schema or authenticated-read health gate; the client side was not moved"
        } else {
            "the daemon did not report healthy at the expected version; inspect docker logs and use the rollback commands below; the client side was not moved"
        }))
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
        let env_file = repo.join("ops/.env");
        if !env_file.is_file() && repo.join("ops/.env.example").is_file() {
            seams::write_atomic(&env_file, &fs::read(repo.join("ops/.env.example"))?)?;
            self.step("scaffolded ops/.env from ops/.env.example (all values commented)");
        }
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
        engine::verify_checkout_identity(&config, &running, &daemon)?;
        if self.o.no_backup {
            self.backup(None)?;
        } else {
            let pg = engine::container(self, &compose, Some(&env), "pseudolife-pg")?;
            let pg_env = &config["services"]["pseudolife-pg"]["environment"];
            let user = pg_env["POSTGRES_USER"].as_str().ok_or_else(|| {
                UpdateError::usage("cannot resolve the project's PostgreSQL user; set POSTGRES_USER in its Compose environment and retry; nothing was deployed")
            })?;
            let db = pg_env["POSTGRES_DB"].as_str().ok_or_else(|| {
                UpdateError::usage("cannot resolve the project's PostgreSQL database; set POSTGRES_DB in its Compose environment and retry; nothing was deployed")
            })?;
            if self.checkout_script(&repo, "backup").is_some() {
                self.project_backup(&repo, &pg, &daemon, user, db)?;
            } else {
                self.builtin_backup(&pg, &daemon, user, db, &data_dir().join("backups"))?;
            }
        }
        let text = fs::read_to_string(&file)?;
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
        let suffix = if self.o.rollback_suffix.is_empty() {
            format!("pre-update-{}", stamp())
        } else {
            self.o.rollback_suffix.clone()
        };
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
        self.prune_rollbacks(
            Some(&repo),
            image.split(':').next().unwrap_or("pseudolife-daemon"),
        );
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
        self.checkout_post_deploy(
            &repo,
            before.as_ref().and_then(|v| v["hooks_digest"].as_str()),
            &health,
        )?;
        self.prune_cache(Some(&repo));
        Ok(())
    }
}
