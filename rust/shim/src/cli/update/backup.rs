use super::*;
use flate2::read::MultiGzDecoder;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{BufRead, BufReader},
    time::{Duration, SystemTime},
};

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
        self.o.engine.is_some() || self.selected_engine == "rust"
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
        self.builtin_backup(
            PG_CONTAINER,
            DAEMON_CONTAINER,
            "pseudolife",
            "pseudolife_memory",
            &data_dir().join("backups"),
        )
    }
    pub fn builtin_backup(
        &mut self,
        pg: &str,
        daemon: &str,
        user: &str,
        db: &str,
        directory: &Path,
    ) -> Result<Value> {
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
            "pg_dump -U \"$1\" -d \"$2\" -Z9 > {}",
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
                user,
                db,
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
        if code != 0 || !dump_complete(&part) {
            return Err(UpdateError::new(
                "the backup is INCOMPLETE ('PostgreSQL database dump complete' missing or gzip damaged); fix the dump or use --no-backup for a plain update; nothing was deployed",
            ));
        }
        fs::rename(&part, &dump)?;
        let remote = format!("/tmp/pl_state-{stamp}.tgz");
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
            "backup: {}{}; restore with ops/restore.* -BackupFile / --backup-file {}",
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
        let removed = rotate(directory, &keep);
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
            s.starts_with(&format!("{repository}:"))
                && s.split_once(':')
                    .is_some_and(|(_, tag)| tag.contains(&marker))
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
