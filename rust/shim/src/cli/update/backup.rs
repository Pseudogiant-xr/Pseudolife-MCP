use super::*;
use flate2::read::MultiGzDecoder;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{BufRead, BufReader},
    time::{Duration, SystemTime},
};

const CONNECTION_FIX: &str = "cannot resolve the daemon's backup connection; set PSEUDOLIFE_MCP_DATABASE_URL in its container environment to an explicit PostgreSQL host, user and database on this project's PostgreSQL container (without a service indirection), then retry; nothing was deployed";

fn connection_fix(daemon: &Value) -> String {
    for key in ["PGSERVICE", "PGHOSTADDR"] {
        if daemon["Config"]["Env"].as_array().is_some_and(|env| {
            env.iter().any(|item| {
                item.as_str()
                    .and_then(|s| s.strip_prefix(&format!("{key}=")))
                    .is_some_and(|value| !value.is_empty())
            })
        }) {
            return format!(
                "cannot resolve the daemon's backup connection; unset {key} in its container environment and retry; nothing was deployed"
            );
        }
    }
    CONNECTION_FIX.into()
}

pub(super) fn connection_parts(daemon: &Value, postgres: &Value) -> Option<(String, String)> {
    let mut env = BTreeMap::new();
    for item in daemon["Config"]["Env"].as_array()? {
        let (key, value) = item.as_str()?.split_once('=')?;
        if env.insert(key, value).is_some() {
            return None;
        }
    }
    let get = |key| env.get(key).copied().filter(|v| !v.is_empty());
    if get("PGSERVICE").is_some() || get("PGHOSTADDR").is_some() {
        return None;
    }
    let raw = get("PSEUDOLIFE_MCP_DATABASE_URL")?;
    let quote = |value: &str| format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'"));
    // Resolve ambient defaults before the URI parser inserts its own 5432.
    // The shared parser understands the daemon's TLS options and rejects
    // opaque service/hostaddr indirections without printing the DSN.
    let normalized = if raw.starts_with("postgresql://") || raw.starts_with("postgres://") {
        let (base, query) = raw.split_once('?').unwrap_or((raw, ""));
        let authority = base
            .split_once("://")?
            .1
            .split('/')
            .next()?
            .rsplit('@')
            .next()?;
        let explicit_port = if authority.starts_with('[') {
            authority.split_once(']')?.1.starts_with(':')
        } else {
            authority.contains(':')
        };
        let origin = base.parse::<tokio_postgres::Config>().ok()?;
        let encode = |value: &str| {
            percent_encoding::utf8_percent_encode(value, percent_encoding::NON_ALPHANUMERIC)
                .to_string()
        };
        let mut defaults = vec![];
        if !explicit_port {
            defaults.push(format!("port={}", encode(get("PGPORT").unwrap_or("5432"))));
        }
        if origin.get_hosts().is_empty()
            && let Some(host) = get("PGHOST")
        {
            defaults.push(format!("host={}", encode(host)));
        }
        defaults.push(query.into());
        format!("{base}?{}", defaults.join("&"))
    } else {
        format!(
            "host={} port={} {raw}",
            quote(get("PGHOST").unwrap_or("")),
            quote(get("PGPORT").unwrap_or("5432"))
        )
    };
    let parsed = crate::pg::Dsn::parse_with_connection_options(&normalized).ok()?;
    let config = parsed.config();
    let host = match config.get_hosts() {
        [] => get("PGHOST")?,
        [tokio_postgres::config::Host::Tcp(host)] => host,
        _ => return None,
    };
    let port = match config.get_ports() {
        [] => get("PGPORT").unwrap_or("5432"),
        [5432] => "5432",
        _ => return None,
    };
    let user = config
        .get_user()
        .filter(|v| !v.is_empty())
        .or_else(|| get("PGUSER"))?;
    // POSTGRES_DB/USER initialize a cluster; they are not libpq defaults.
    let db = config
        .get_dbname()
        .filter(|v| !v.is_empty())
        .or_else(|| get("PGDATABASE"))
        .unwrap_or(user);
    if port != "5432"
        || db.contains('=')
        || db.contains("://")
        || user.chars().chain(db.chars()).any(|c| c < ' ')
    {
        return None;
    }
    let daemon_networks = daemon["NetworkSettings"]["Networks"].as_object()?;
    let pg_networks = postgres["NetworkSettings"]["Networks"].as_object()?;
    let found = pg_networks
        .iter()
        .filter(|(name, _)| daemon_networks.contains_key(*name))
        .any(|(_, network)| {
            postgres["Name"]
                .as_str()
                .is_some_and(|n| n.trim_start_matches('/') == host)
                || network["IPAddress"].as_str() == Some(host)
                || ["Aliases", "DNSNames"].iter().any(|key| {
                    network[key]
                        .as_array()
                        .is_some_and(|names| names.iter().any(|name| name.as_str() == Some(host)))
                })
        });
    found.then(|| (user.to_owned(), db.to_owned()))
}

pub(super) fn dump_complete(path: &Path) -> bool {
    fn check(path: &Path) -> std::io::Result<bool> {
        let mut stream = BufReader::new(MultiGzDecoder::new(File::open(path)?));
        let (mut complete, mut copy) = (false, false);
        let mut line = String::new();
        loop {
            line.clear();
            if stream.read_line(&mut line)? == 0 {
                break;
            }
            let line = line
                .trim_end_matches('\n')
                .strip_suffix('\r')
                .unwrap_or(line.trim_end_matches('\n'));
            if copy {
                if line == "\\." {
                    copy = false;
                }
            } else if line.starts_with("COPY ") && line.ends_with("FROM stdin;") {
                copy = true;
            } else if line == "-- PostgreSQL database dump complete" {
                complete = true;
            }
        }
        Ok(complete)
    }
    check(path).unwrap_or(false)
}
fn own_backup_file(path: &Path) -> Result<()> {
    own_backup_file_with(path, |source, target| fs::rename(source, target))
}
fn own_backup_file_with(
    path: &Path,
    replace: impl FnOnce(&Path, &Path) -> std::io::Result<()>,
) -> Result<()> {
    // A privileged Docker wrapper may own its copy. Stream it into a new
    // operator-owned file before accepting the backup or moving a rollback tag.
    let owned = path.with_extension(format!("owned-{}", uuid::Uuid::new_v4().simple()));
    let mut complete = false;
    let result = (|| -> std::io::Result<()> {
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut output = options.open(&owned)?;
        std::io::copy(&mut File::open(path)?, &mut output)?;
        output.sync_all()?;
        drop(output);
        complete = true;
        replace(&owned, path)
    })();
    if result.is_err() {
        if complete {
            return Err(UpdateError::usage(format!(
                "cannot replace the operator-owned backup; the complete copy is retained at {}; repair access to the backup file and directory and retry; nothing was deployed",
                owned.display()
            )));
        }
        let _ = fs::remove_file(&owned);
        return Err(UpdateError::usage(
            "cannot save an operator-owned backup; repair access to the backup file and directory and retry; nothing was deployed",
        ));
    }
    Ok(())
}
fn state_complete(path: &Path) -> bool {
    fn check(path: &Path) -> std::io::Result<()> {
        let mut decoder = MultiGzDecoder::new(File::open(path)?);
        {
            let mut archive = tar::Archive::new(&mut decoder);
            for entry in archive.entries()? {
                let mut entry = entry?;
                std::io::copy(&mut entry, &mut std::io::sink())?;
            }
        }
        // tar stops at its end marker; consume the gzip trailer too.
        std::io::copy(&mut decoder, &mut std::io::sink())?;
        Ok(())
    }
    check(path).is_ok()
}
fn backup_name(name: &str, state: bool) -> bool {
    let pattern = if state {
        r"^pseudolife_state-[0-9]{8}-[0-9]{6}\.tgz$"
    } else {
        r"^pseudolife_memory-[0-9]{8}-[0-9]{6}\.sql\.gz$"
    };
    regex::Regex::new(pattern)
        .expect("backup pattern")
        .is_match(name)
}
pub(super) fn snapshot(directory: &Path) -> BTreeMap<String, (SystemTime, u64)> {
    fs::read_dir(directory)
        .into_iter()
        .flatten()
        .filter_map(|e| {
            let e = e.ok()?;
            let m = e.metadata().ok()?;
            let name = e.file_name().into_string().ok()?;
            (m.is_file() && name.starts_with("pseudolife_"))
                .then_some((name, (m.modified().ok()?, m.len())))
        })
        .collect()
}
pub(super) fn verify(
    directory: &Path,
    before: &BTreeMap<String, (SystemTime, u64)>,
) -> Result<Value> {
    verify_with_state(directory, before, true)
}
fn verify_with_state(
    directory: &Path,
    before: &BTreeMap<String, (SystemTime, u64)>,
    require_state: bool,
) -> Result<Value> {
    let after = snapshot(directory);
    let dumps: Vec<_> = after
        .iter()
        .filter(|(n, m)| backup_name(n, false) && before.get(*n) != Some(*m))
        .map(|(n, _)| n)
        .collect();
    if dumps.len() != 1 {
        return Err(UpdateError::new(
            "cannot identify this run's bank backup; check the backup directory and retry, or use --no-backup for a plain update; nothing was deployed",
        ));
    }
    let dump = directory.join(dumps[0]);
    let state_name = dumps[0]
        .replace("pseudolife_memory-", "pseudolife_state-")
        .replace(".sql.gz", ".tgz");
    let state = directory.join(&state_name);
    let state_ok = after.get(&state_name) != before.get(&state_name) && state_complete(&state);
    if !dump_complete(&dump) || (require_state && !state_ok) {
        return Err(UpdateError::new(
            "the bank or required state backup is missing or incomplete; fix the backup error and retry, or use --no-backup for a plain update; nothing was deployed",
        ));
    }
    Ok(json!({"dump":dump,"state_archive":if state_ok {Some(state)} else {None}}))
}
pub(super) fn rotate(directory: &Path, keep: &BTreeSet<String>) -> Vec<PathBuf> {
    let mut removed = vec![];
    for state in [false, true] {
        let mut paths: Vec<_> = fs::read_dir(directory)
            .into_iter()
            .flatten()
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| backup_name(n, state))
            })
            .collect();
        paths.sort_by(|a, b| b.cmp(a));
        for path in paths.into_iter().skip(3) {
            if path
                .file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| keep.contains(n))
            {
                continue;
            }
            if fs::symlink_metadata(&path).is_ok_and(|m| {
                m.is_file()
                    && m.modified()
                        .ok()
                        .and_then(|t| t.elapsed().ok())
                        .is_some_and(|age| age > Duration::from_secs(7 * 86400))
            }) && fs::remove_file(&path).is_ok()
            {
                removed.push(path);
            }
        }
    }
    removed
}
impl Update {
    fn rotate_after_backup(&self, directory: &Path, keep: &BTreeSet<String>) -> Vec<PathBuf> {
        if self.stopped_handover {
            vec![]
        } else {
            rotate(directory, keep)
        }
    }
    pub fn set_rollback_scope(&mut self, config: &Value, running: &Value) -> Result<()> {
        use sha2::{Digest, Sha256};
        let project = config["name"].as_str().filter(|s| !s.is_empty());
        let volume = running["Mounts"]
            .as_array()
            .and_then(|mounts| mounts.iter().find(|m| m["Destination"] == "/data"))
            .and_then(|m| m["Name"].as_str())
            .filter(|s| !s.is_empty());
        let (Some(project), Some(volume)) = (project, volume) else {
            return Err(UpdateError::usage(
                "cannot identify this Compose project and its state volume for rollback retention; restore the project's named /data volume and retry; nothing was deployed",
            ));
        };
        self.rollback_scope = Some(format!(
            "{:x}",
            Sha256::digest(json!([project, volume]).to_string())
        ));
        Ok(())
    }
    pub fn rollback_suffix(&self) -> String {
        if !self.o.rollback_suffix.is_empty() {
            return self.o.rollback_suffix.clone();
        }
        format!(
            "pre-update-pl-{}-{}",
            self.rollback_scope
                .as_deref()
                .expect("validated rollback scope"),
            stamp()
        )
    }
    fn requires_state_backup(&self) -> bool {
        self.o.engine.is_some() || self.selected_engine == "rust" || self.o.allow_auth_change
    }
    pub(super) fn with_state_helper<T>(
        &self,
        daemon: &str,
        image: &str,
        script: &str,
        action: impl FnOnce(&str) -> Result<T>,
    ) -> Result<T> {
        // A unique owned name permits cleanup even if create times out before
        // returning its ID. Inherited mounts remain read-only and offline.
        let helper = format!("pseudolife-update-state-{}", uuid::Uuid::new_v4().simple());
        let args = [
            "create",
            "--name",
            &helper,
            "--network",
            "none",
            "--user",
            "0",
            "--volumes-from",
            &format!("{daemon}:ro"),
            "--entrypoint",
            "sh",
            image,
            "-c",
            &format!(
                "timeout --kill-after=5s 3500s sh -c {}",
                seams::quote(script)
            ),
        ]
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
        let (code, _) = self.command(&args, None, 60, false);
        let result = if code == 0 {
            action(&helper)
        } else {
            Err(UpdateError::usage(
                "cannot create the stopped daemon's state helper; restore its local image and retry; nothing was deployed",
            ))
        };
        let cleanup = self
            .command(&["rm".into(), "-f".into(), helper], None, 120, false)
            .0;
        if code == 0 && cleanup != 0 {
            return Err(UpdateError::usage(
                "cannot remove the owned state helper; remove the pseudolife-update-state helper container before retrying; nothing was deployed",
            ));
        }
        result
    }
    fn verify_backup(
        &mut self,
        directory: &Path,
        before: &BTreeMap<String, (SystemTime, u64)>,
    ) -> Result<Value> {
        let backup = if self.requires_state_backup() {
            verify(directory, before)?
        } else {
            verify_with_state(directory, before, false)?
        };
        if backup["state_archive"].is_null() {
            self.warn("state archive could not be made; continuing with the complete SQL dump (the daemon may be stopped)");
        }
        Ok(backup)
    }
    pub fn backup(&mut self, repo: Option<&Path>) -> Result<Value> {
        if self.o.no_backup {
            self.warn("skipping backup (--no-backup)");
            return Ok(json!({}));
        }
        if let Some(repo) = repo
            && let Some(script) = self.checkout_script(repo, "backup")
        {
            let directory = repo.join("data/backups");
            let before = snapshot(&directory);
            self.step(format!(
                "backing up the bank ({})...",
                script.file_name().unwrap_or_default().to_string_lossy()
            ));
            let mut args = Self::script_argv(&script);
            args.extend([
                if script.extension().is_some_and(|s| s == "ps1") {
                    "-OutDir"
                } else {
                    "--out-dir"
                }
                .into(),
                directory.to_string_lossy().into_owned(),
            ]);
            if run_cli(&args, None, 3600, !self.o.as_json).0 != 0 {
                return Err(UpdateError::new(
                    "the backup failed; fix the backup error or use --no-backup for a plain update; nothing was deployed",
                ));
            }
            return self.verify_backup(&directory, &before);
        }
        self.builtin_backup(PG_CONTAINER, DAEMON_CONTAINER, &data_dir().join("backups"))
    }
    pub fn builtin_backup(&mut self, pg: &str, daemon: &str, directory: &Path) -> Result<Value> {
        // Runtime container settings remain available when the daemon is stopped.
        // The caller's Compose initialization settings cannot identify its bank.
        let running =
            engine::inspect(self, daemon).map_err(|_| UpdateError::usage(CONNECTION_FIX))?;
        let postgres = engine::inspect(self, pg).map_err(|_| UpdateError::usage(CONNECTION_FIX))?;
        let (user, db) = connection_parts(&running, &postgres)
            .ok_or_else(|| UpdateError::usage(connection_fix(&running)))?;
        fs::create_dir_all(directory)?;
        let stamp = stamp();
        let dump = directory.join(format!("pseudolife_memory-{stamp}.sql.gz"));
        let state = directory.join(format!("pseudolife_state-{stamp}.tgz"));
        // Never overwrite a previous backup, even when a retry starts in the
        // same second. A collision is a pre-deploy refusal.
        let part = dump.with_file_name(format!(
            "{}.part",
            dump.file_name().unwrap().to_string_lossy()
        ));
        if dump.exists() || state.exists() || part.exists() {
            return Err(UpdateError::new(
                "a backup for this timestamp already exists; retry after one second; nothing was deployed",
            ));
        }
        self.step("backing up the bank (pg_dump in the container + state volume tar)...");
        let remote = format!("/tmp/pl_backup-{stamp}.sql.gz");
        let sql = format!(
            "unset PGHOSTADDR PGSERVICE PGSERVICEFILE; pg_dump -h /var/run/postgresql -p 5432 -U \"$1\" -d \"$2\" -Z9 > {}",
            seams::quote(&remote)
        );
        let (code, _) = self.command(
            // Docker exec children run in the container, outside the host's
            // process group/job. Their own deadline expires before the host
            // CLI's 3600-second timeout, with a bounded forced-kill grace.
            &[
                "exec",
                pg,
                "timeout",
                "--kill-after=5s",
                "3500s",
                "sh",
                "-c",
                &sql,
                "sh",
                &user,
                &db,
            ]
            .iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>(),
            None,
            3600,
            false,
        );
        if code != 0 {
            self.docker_call(&["exec", pg, "rm", "-f", &remote]);
            return Err(UpdateError::new(
                "pg_dump failed or timed out inside the PostgreSQL container; ensure GNU timeout is installed and fix the backup error, or use --no-backup for a plain update; nothing was deployed",
            ));
        }
        let (code, _) = self.command(
            &[
                "cp".into(),
                format!("{pg}:{remote}"),
                part.to_string_lossy().into_owned(),
            ],
            None,
            3600,
            false,
        );
        self.docker_call(&["exec", pg, "rm", "-f", &remote]);
        if code == 0 && self.o.allow_auth_change && self.o.checkout.is_some() {
            own_backup_file(&part)?;
        }
        if code != 0 || !dump_complete(&part) {
            return Err(UpdateError::new(
                "the backup is INCOMPLETE ('PostgreSQL database dump complete' missing or gzip damaged); fix the dump or use --no-backup for a plain update; nothing was deployed",
            ));
        }
        fs::rename(&part, &dump)?;
        let remote = format!("/tmp/pl_state-{stamp}.tgz");
        let copied = if self.o.allow_auth_change && self.o.checkout.is_some() {
            let running = engine::inspect(self, daemon)?;
            let image = running["Image"].as_str().filter(|image| !image.is_empty())
                .ok_or_else(|| UpdateError::usage("cannot identify the stopped image; restore its local image before retrying; nothing was deployed"))?;
            self.with_state_helper(
                daemon,
                image,
                &format!("tar czf {remote} -C /data ."),
                |helper| {
                    if self
                        .command(
                            &["start".into(), "-a".into(), helper.into()],
                            None,
                            3600,
                            false,
                        )
                        .0
                        != 0
                    {
                        return Ok(false);
                    }
                    Ok(self
                        .command(
                            &[
                                "cp".into(),
                                format!("{helper}:{remote}"),
                                state.to_string_lossy().into_owned(),
                            ],
                            None,
                            3600,
                            false,
                        )
                        .0
                        == 0)
                },
            )?
        } else {
            let (code, _) = self.command(
                &[
                    "exec".into(),
                    daemon.into(),
                    "timeout".into(),
                    "--kill-after=5s".into(),
                    "3500s".into(),
                    "sh".into(),
                    "-c".into(),
                    format!("tar czf {remote} -C /data ."),
                ],
                None,
                3600,
                false,
            );
            let copied = code == 0
                && self
                    .command(
                        &[
                            "cp".into(),
                            format!("{daemon}:{remote}"),
                            state.to_string_lossy().into_owned(),
                        ],
                        None,
                        3600,
                        false,
                    )
                    .0
                    == 0;
            self.docker_call(&["exec", daemon, "rm", "-f", &remote]);
            copied
        };
        if copied && self.o.allow_auth_change && self.o.checkout.is_some() {
            own_backup_file(&state)?;
        }
        let state_ok = copied && state_complete(&state);
        if !state_ok {
            if self.requires_state_backup() {
                return Err(UpdateError::new(
                    "the state volume backup is missing or incomplete; the bank dump is retained; ensure GNU timeout is installed in the daemon image, fix the backup error and retry; engine changes cannot use --no-backup; nothing was deployed",
                ));
            }
            self.warn("state archive could not be made; continuing with the complete SQL dump (the daemon may be stopped)");
        }
        self.step(format!(
            "backup: {}{}; restore with pseudolife-mcp restore --backup-file {}",
            dump.display(),
            if state_ok {
                format!(" + {}", state.file_name().unwrap().to_string_lossy())
            } else {
                String::new()
            },
            dump.display()
        ));
        let keep = [
            dump.file_name().unwrap().to_string_lossy().into_owned(),
            state.file_name().unwrap().to_string_lossy().into_owned(),
        ]
        .into_iter()
        .collect();
        let removed = self.rotate_after_backup(directory, &keep);
        if !removed.is_empty() {
            self.step(format!(
                "rotated {} backup file(s) older than 7 days",
                removed.len()
            ));
        }
        Ok(
            json!({"dump":dump,"state_archive":if state_ok {Some(state)} else {None},"rotated":removed}),
        )
    }
    pub fn project_backup(
        &mut self,
        repo: &Path,
        pg: &str,
        daemon: &str,
        user: &str,
        db: &str,
    ) -> Result<Value> {
        let Some(script) = self.checkout_script(repo, "backup") else {
            return Err(UpdateError::usage(
                "the checkout backup script is missing; restore ops/backup.sh or ops/backup.ps1 and retry",
            ));
        };
        let directory = repo.join("data/backups");
        let before = snapshot(&directory);
        let flags = if script.extension().is_some_and(|s| s == "ps1") {
            ["-Container", "-DaemonContainer", "-User", "-Db", "-OutDir"]
        } else {
            [
                "--container",
                "--daemon-container",
                "--user",
                "--db",
                "--out-dir",
            ]
        };
        let mut args = Self::script_argv(&script);
        for (flag, value) in flags.into_iter().zip([
            pg.to_owned(),
            daemon.to_owned(),
            user.to_owned(),
            db.to_owned(),
            directory.to_string_lossy().into_owned(),
        ]) {
            args.extend([flag.into(), value]);
        }
        self.step("backing up the bank and /data before deploying...");
        if run_cli(&args, None, 3600, !self.o.as_json).0 != 0 {
            return Err(UpdateError::new(
                "the backup failed; fix the backup error or use --no-backup for a plain update; nothing was deployed",
            ));
        }
        self.verify_backup(&directory, &before)
    }
    pub fn checkout_script(&self, repo: &Path, stem: &str) -> Option<PathBuf> {
        let suffix = if cfg!(windows) { "ps1" } else { "sh" };
        let path = repo.join("ops").join(format!("{stem}.{suffix}"));
        path.is_file().then_some(path)
    }
    pub fn script_argv(script: &Path) -> Vec<String> {
        if script.extension().is_some_and(|s| s == "ps1") {
            vec![
                "pwsh".into(),
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-File".into(),
                script.to_string_lossy().into_owned(),
            ]
        } else {
            vec!["bash".into(), script.to_string_lossy().into_owned()]
        }
    }
    pub fn prune_rollbacks(&mut self, repository: &str) {
        let Some(scope) = &self.rollback_scope else {
            self.warn("rollback-tag retention skipped: no verified project scope");
            return;
        };
        let marker = format!("-pre-update-pl-{scope}-");
        let (code, out) = self.docker_call(&["ps", "-q"]);
        if code != 0 {
            self.warn("rollback-tag retention skipped: cannot inspect running containers");
            return;
        }
        let mut in_use = BTreeSet::new();
        let ids: Vec<_> = out.split_whitespace().collect();
        if !ids.is_empty() {
            let mut args = vec!["inspect", "--format", "{{.Image}}"];
            args.extend(ids);
            let (code, out) = self.docker_call(&args);
            if code != 0 {
                self.warn("rollback-tag retention skipped: cannot inspect running images");
                return;
            }
            in_use.extend(out.lines().map(str::to_owned));
        }
        let (code, out) = self.docker_call(&[
            "image",
            "ls",
            repository,
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ]);
        if code != 0 {
            self.warn("rollback-tag retention failed (deploy continues)");
            return;
        }
        let mut candidates = vec![];
        for reference in out.lines().map(str::trim).filter(|s| {
            s.strip_prefix(&format!("{repository}:"))
                .and_then(|tag| tag.split_once(&marker))
                .is_some_and(|(_, stamp)| {
                    // Only our complete automatic timestamp grants retention
                    // authority; operator suffixes such as -keep stay intact.
                    stamp.len() == 15
                        && stamp.bytes().enumerate().all(|(i, byte)| {
                            if i == 8 {
                                byte == b'-'
                            } else {
                                byte.is_ascii_digit()
                            }
                        })
                })
        }) {
            let (code, out) = self.docker_call(&[
                "image",
                "inspect",
                "--format",
                "{{.Created}}|{{.Id}}",
                reference,
            ]);
            if code == 0
                && let Some((time, id)) = out.trim().split_once('|')
            {
                candidates.push((time.to_owned(), reference.to_owned(), id.to_owned()));
            }
        }
        candidates.sort_by(|a, b| b.cmp(a));
        for (_, reference, id) in candidates.into_iter().skip(self.o.keep_rollbacks) {
            if self.report.rollback["tag"] == reference {
                continue;
            }
            if in_use.contains(&id) {
                self.step(format!(
                    "rollback retention: keeping {reference} (image in use by a running container)"
                ));
                continue;
            }
            if self.docker_call(&["rmi", &reference]).0 != 0 {
                self.warn(format!(
                    "rollback retention: docker rmi {reference} failed; leaving it"
                ));
            }
        }
    }
    pub fn prune_cache(&mut self, repo: Option<&Path>) {
        if self.o.no_cache_prune {
            return;
        }
        let code = if let Some(repo) = repo
            && let Some(script) = self.checkout_script(repo, "prune-build-cache")
        {
            let mut args = Self::script_argv(&script);
            args.extend([
                if script.extension().is_some_and(|s| s == "ps1") {
                    "-MaxAgeHours"
                } else {
                    "--max-age-hours"
                }
                .into(),
                self.o.keep_cache_hours.to_string(),
            ]);
            run_cli(&args, None, 1800, !self.o.as_json).0
        } else {
            self.command(
                &[
                    "builder".into(),
                    "prune".into(),
                    "-f".into(),
                    "--filter".into(),
                    format!("until={}h", self.o.keep_cache_hours),
                ],
                None,
                1800,
                false,
            )
            .0
        };
        if code != 0 {
            self.warn("build-cache retention failed (deploy already succeeded)");
        }
    }
}

#[cfg(all(test, unix))]
mod stopped_backup_ownership_tests {
    use super::*;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    #[test]
    fn stopped_backup_copy_is_private_and_owned_by_the_operator() {
        let directory =
            std::env::temp_dir().join(format!("pseudolife-backup-owner-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("fixture.sql.gz.part");
        fs::write(&path, b"fixture backup").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        let original = fs::metadata(&path).unwrap().ino();
        own_backup_file(&path).unwrap();
        let accepted = fs::metadata(&path).unwrap();
        assert_ne!(accepted.ino(), original);
        assert_eq!(accepted.uid(), fs::metadata(&directory).unwrap().uid());
        assert_eq!(accepted.permissions().mode() & 0o777, 0o600);
        assert_eq!(fs::read(&path).unwrap(), b"fixture backup");
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 1);
        fs::remove_dir_all(directory).unwrap();
    }
}

#[cfg(test)]
mod connection_tests {
    use super::*;

    fn fixture(dsn: &str, extra: &[&str]) -> (Value, Value) {
        let mut env = vec![format!("PSEUDOLIFE_MCP_DATABASE_URL={dsn}")];
        env.extend(extra.iter().map(|v| v.to_string()));
        (
            json!({"Config":{"Env":env}, "State":{"Running":false},
            "NetworkSettings":{"Networks":{"bank":{}}}}),
            json!({"Name":"/fixture-pg", "Config":{"Env":["POSTGRES_USER=wrong","POSTGRES_DB=wrong"]},
            "NetworkSettings":{"Networks":{"bank":{"Aliases":["pseudolife-pg"]}}}}),
        )
    }

    #[test]
    fn backup_connection_uses_runtime_authority_and_libpq_defaults() {
        for (dsn, extra, user, db) in [
            (
                "postgresql://custom@pseudolife-pg/bank?port=5432",
                vec!["PGPORT=9999"],
                "custom",
                "bank",
            ),
            (
                "user=custom dbname=bank",
                vec!["PGHOST=pseudolife-pg"],
                "custom",
                "bank",
            ),
            (
                "postgresql://custom@/bank",
                vec!["PGHOST=pseudolife-pg"],
                "custom",
                "bank",
            ),
            (
                "postgresql://custom@pseudolife-pg:5432/bank",
                vec!["PGPORT=9999"],
                "custom",
                "bank",
            ),
            (
                "postgresql://custom@pseudolife-pg/bank?sslmode=verify-full&sslrootcert=/fixture/root.pem",
                vec![],
                "custom",
                "bank",
            ),
            (
                "host=pseudolife-pg user=custom dbname=bank sslmode=verify-full sslrootcert=/fixture/root.pem",
                vec![],
                "custom",
                "bank",
            ),
            (
                "postgresql://custom:fixture@pseudolife-pg/custom_bank",
                vec![],
                "custom",
                "custom_bank",
            ),
            (
                "postgresql://pseudolife:fixture@pseudolife-pg/pseudolife_memory",
                vec![],
                "pseudolife",
                "pseudolife_memory",
            ),
            (
                "postgresql://custom:fixture@pseudolife-pg",
                vec![],
                "custom",
                "custom",
            ),
            (
                "postgresql://custom:fixture@pseudolife-pg",
                vec!["PGDATABASE=runtime_bank"],
                "custom",
                "runtime_bank",
            ),
            (
                "host=pseudolife-pg user=custom dbname='bank with spaces' password=fixture",
                vec![],
                "custom",
                "bank with spaces",
            ),
            (
                "postgresql://pseudolife-pg",
                vec!["PGUSER=runtime_user"],
                "runtime_user",
                "runtime_user",
            ),
            (
                "postgresql://custom:fixture@pseudolife-pg/wrong?dbname=actual",
                vec![],
                "custom",
                "actual",
            ),
        ] {
            let (daemon, pg) = fixture(dsn, &extra);
            assert_eq!(
                connection_parts(&daemon, &pg),
                Some((user.into(), db.into()))
            );
        }
    }

    #[test]
    fn backup_connection_refuses_opaque_or_foreign_targets() {
        for (dsn, extra) in [
            ("", vec![]),
            ("service=opaque", vec![]),
            (
                "host=pseudolife-pg hostaddr=10.0.0.2 user=custom dbname=bank",
                vec![],
            ),
            (
                "postgresql://custom@pseudolife-pg/bank?hostaddr=10.0.0.2",
                vec![],
            ),
            ("invalid fixture", vec![]),
            ("postgresql://custom@elsewhere/bank", vec![]),
            (
                "postgresql://custom@pseudolife-pg/bank",
                vec!["PGSERVICE=opaque"],
            ),
            (
                "postgresql://custom@pseudolife-pg/bank",
                vec!["PGHOSTADDR=10.0.0.1"],
            ),
            ("postgresql://pseudolife-pg/bank", vec![]),
            (
                "host=pseudolife-pg user=custom dbname='host=elsewhere'",
                vec![],
            ),
            ("postgresql://custom@pseudolife-pg:9999/bank", vec![]),
            (
                "postgresql://custom@pseudolife-pg/bank",
                vec!["PGPORT=9999"],
            ),
        ] {
            let (daemon, pg) = fixture(dsn, &extra);
            assert!(connection_parts(&daemon, &pg).is_none());
        }
    }
}

#[cfg(test)]
mod handover_review_tests {
    use super::*;
    #[test]
    fn review_failed_backup_replace_retains_both_complete_copies() {
        let directory =
            std::env::temp_dir().join(format!("pseudolife-backup-copy-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("fixture.sql.gz.part");
        fs::write(&path, b"complete fixture backup").unwrap();
        let mut replacement = None;
        let result = own_backup_file_with(&path, |source, _| {
            replacement = Some(source.to_owned());
            Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied))
        });
        let original = fs::read(&path).ok();
        let copied = fs::read(replacement.unwrap()).ok();
        fs::remove_dir_all(&directory).unwrap();
        assert!(result.is_err());
        assert_eq!(
            original.as_deref(),
            Some(b"complete fixture backup".as_slice())
        );
        assert_eq!(copied, original);
    }

    #[test]
    fn review_stopped_handover_never_rotates_older_backups() {
        let directory =
            std::env::temp_dir().join(format!("pseudolife-backup-retain-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        for stamp in 1..=4 {
            for (prefix, suffix) in [("pseudolife_memory", "sql.gz"), ("pseudolife_state", "tgz")] {
                let path = directory.join(format!("{prefix}-20000101-00000{stamp}.{suffix}"));
                let file = File::create(path).unwrap();
                file.set_times(fs::FileTimes::new().set_modified(SystemTime::UNIX_EPOCH))
                    .unwrap();
            }
        }
        let mut update = Update::new(Options::default());
        update.stopped_handover = true;
        let removed = update.rotate_after_backup(&directory, &BTreeSet::new());
        let remaining = fs::read_dir(&directory).unwrap().count();
        update.stopped_handover = false;
        let ordinary_removed = update
            .rotate_after_backup(&directory, &BTreeSet::new())
            .len();
        fs::remove_dir_all(&directory).unwrap();
        assert!(removed.is_empty());
        assert_eq!(remaining, 8);
        assert_eq!(ordinary_removed, 2);
    }
}
