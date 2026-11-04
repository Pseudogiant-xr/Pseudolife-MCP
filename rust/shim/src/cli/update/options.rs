use super::{Result, UpdateError};
use std::{ffi::OsString, path::PathBuf};

#[derive(Clone, Debug)]
pub(crate) struct Options {
    pub engine: Option<String>,
    pub shadow: bool,
    pub checkout: Option<PathBuf>,
    pub tag: Option<String>,
    pub rollback_suffix: String,
    pub no_backup: bool,
    pub keep_rollbacks: usize,
    pub keep_cache_hours: u64,
    pub no_cache_prune: bool,
    pub force_rollback_tag: bool,
    pub allow_dirty: bool,
    pub allow_auth_change: bool,
    pub health_retries: usize,
    pub health_delay_ms: u64,
    pub all_clients: bool,
    pub no_test_login: bool,
    pub clients_only: bool,
    pub daemon_only: bool,
    pub reinstall: bool,
    pub allow_downgrade: bool,
    pub env_file: Option<PathBuf>,
    pub check: bool,
    pub result_file: Option<PathBuf>,
    pub unattended: bool,
    pub schedule: Option<String>,
    pub allow_no_bearer: bool,
    pub unschedule: bool,
    pub as_json: bool,
    pub daemon_url: String,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            engine: None,
            shadow: false,
            checkout: None,
            tag: None,
            rollback_suffix: String::new(),
            no_backup: false,
            keep_rollbacks: 2,
            keep_cache_hours: 168,
            no_cache_prune: false,
            force_rollback_tag: false,
            allow_dirty: false,
            allow_auth_change: false,
            health_retries: 30,
            health_delay_ms: 1500,
            all_clients: false,
            no_test_login: false,
            clients_only: false,
            daemon_only: false,
            reinstall: false,
            allow_downgrade: false,
            env_file: None,
            check: false,
            result_file: None,
            unattended: false,
            schedule: None,
            allow_no_bearer: false,
            unschedule: false,
            as_json: false,
            daemon_url: std::env::var("PSEUDOLIFE_MCP_DAEMON_URL")
                .ok()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "http://127.0.0.1:8765".into()),
        }
    }
}

impl Options {
    pub fn parse(arguments: Vec<OsString>) -> Result<Self> {
        let mut o = Self::default();
        let mut args = arguments.into_iter();
        while let Some(arg) = args.next() {
            let arg = arg
                .into_string()
                .map_err(|_| UpdateError::usage("arguments must be UTF-8"))?;
            let (flag, inline) = arg
                .split_once('=')
                .map_or((arg.as_str(), None), |(a, b)| (a, Some(b)));
            let mut value = || -> Result<String> {
                inline
                    .map(str::to_owned)
                    .or_else(|| args.next().and_then(|v| v.into_string().ok()))
                    .ok_or_else(|| UpdateError::usage(format!("{flag} needs a value")))
            };
            match flag {
                "--engine" => o.engine = Some(value()?),
                "--checkout" => o.checkout = Some(value()?.into()),
                "--tag" => o.tag = Some(value()?),
                "--rollback-tag" => o.rollback_suffix = value()?,
                "--env-file" => o.env_file = Some(value()?.into()),
                "--result-file" => o.result_file = Some(value()?.into()),
                "--schedule" => o.schedule = Some(value()?),
                "--daemon-url" => o.daemon_url = value()?,
                "--keep-rollbacks" => o.keep_rollbacks = number(flag, &value()?)?,
                "--keep-cache-hours" => o.keep_cache_hours = number(flag, &value()?)?,
                "--health-retries" => o.health_retries = number(flag, &value()?)?,
                "--health-delay-ms" => o.health_delay_ms = number(flag, &value()?)?,
                _ if inline.is_some() => {
                    return Err(UpdateError::usage(format!("unrecognized argument: {arg}")));
                }
                "--shadow" => o.shadow = true,
                "--no-backup" => o.no_backup = true,
                "--no-cache-prune" => o.no_cache_prune = true,
                "--force-rollback-tag" => o.force_rollback_tag = true,
                "--allow-dirty" => o.allow_dirty = true,
                "--allow-auth-change" => o.allow_auth_change = true,
                "--all" => o.all_clients = true,
                "--no-test-login" => o.no_test_login = true,
                "--clients-only" => o.clients_only = true,
                "--daemon-only" => o.daemon_only = true,
                "--reinstall" => o.reinstall = true,
                "--allow-downgrade" => o.allow_downgrade = true,
                "--check" => o.check = true,
                "--unattended" => o.unattended = true,
                "--allow-no-bearer" => o.allow_no_bearer = true,
                "--unschedule" => o.unschedule = true,
                "--json" => o.as_json = true,
                _ => return Err(UpdateError::usage(format!("unrecognized argument: {arg}"))),
            }
        }
        o.validate()?;
        Ok(o)
    }

    pub fn validate(&self) -> Result<()> {
        if self.allow_auth_change
            && (self.checkout.is_some()
                || self.engine.is_some()
                || self.shadow
                || self.clients_only
                || self.all_clients
                || self.unattended
                || self.schedule.is_some()
                || self.unschedule
                || self.check
                || self.tag.is_none()
                || !self.reinstall
                || !self.daemon_only
                || !self.env_file.as_ref().is_some_and(|path| {
                    super::seams::env_file_sets_credentials(std::slice::from_ref(path))
                }))
        {
            return Err(UpdateError::usage(
                "--allow-auth-change requires an attended release recreation with --env-file <bearer file>, --tag, --reinstall and --daemon-only. Nothing was changed",
            ));
        }
        if self.engine.is_some()
            && self.no_backup
            && !(self.shadow && self.engine.as_deref() == Some("python"))
        {
            return Err(UpdateError::usage(
                "engine changes require a backup; remove --no-backup and retry; nothing was changed",
            ));
        }
        if self.clients_only && self.daemon_only {
            return Err(UpdateError::usage(
                "--clients-only and --daemon-only exclude each other",
            ));
        }
        if self
            .engine
            .as_deref()
            .is_some_and(|s| !matches!(s, "rust" | "python"))
        {
            return Err(UpdateError::usage("--engine must be python or rust"));
        }
        if self.shadow && (self.engine.is_none() || self.checkout.is_none()) {
            return Err(UpdateError::usage(
                "--shadow requires --engine rust --checkout <repo> to start, or --engine python --checkout <repo> to remove; nothing was changed",
            ));
        }
        if self.engine.as_deref() == Some("rust") && self.checkout.is_none() {
            return Err(UpdateError::usage(
                "Rust images are not published; use --engine rust --checkout <repo> (checkout mode); nothing was changed",
            ));
        }
        if (self.engine.is_some() || self.shadow)
            && (self.clients_only || self.all_clients || self.unattended || self.schedule.is_some())
        {
            return Err(UpdateError::usage(
                "engine selection updates only the Docker daemon; remove client/schedule options and retry",
            ));
        }
        if self.checkout.is_some() {
            for (flag, set) in [
                ("--clients-only", self.clients_only),
                ("--tag", self.tag.is_some()),
                ("--reinstall", self.reinstall),
                ("--allow-downgrade", self.allow_downgrade),
                ("--env-file", self.env_file.is_some()),
            ] {
                if set {
                    return Err(UpdateError::usage(format!(
                        "{flag} is a release-mode option; a checkout deploy builds what the tree holds (for the clients alone: pseudolife-mcp update --clients-only)"
                    )));
                }
            }
        } else if self.no_test_login {
            return Err(UpdateError::usage(
                "--no-test-login is a checkout-mode option: a release update never creates the test suite's login",
            ));
        }
        if self.unattended {
            for (flag, set) in [
                ("--no-backup", self.no_backup),
                ("--clients-only", self.clients_only),
                ("--daemon-only", self.daemon_only),
                ("--force-rollback-tag", self.force_rollback_tag),
                ("--allow-downgrade", self.allow_downgrade),
                ("--checkout", self.checkout.is_some()),
                ("--reinstall", self.reinstall),
                ("--check", self.check),
            ] {
                if set {
                    return Err(UpdateError::usage(format!(
                        "{flag} cannot be combined with --unattended: the unattended run always backs up, tags a rollback and moves the daemon and the clients together"
                    )));
                }
            }
        }
        if self
            .tag
            .as_deref()
            .is_some_and(|v| !super::plain_release(v))
        {
            return Err(UpdateError::usage(
                "--tag must be a plain release number (N.N.N); nothing was changed",
            ));
        }
        if !self.rollback_suffix.is_empty() && !super::safe_tag(&self.rollback_suffix) {
            return Err(UpdateError::usage(
                "--rollback-tag must contain only letters, digits, dots, underscores or hyphens; nothing was deployed",
            ));
        }
        Ok(())
    }
}
fn number<T: std::str::FromStr>(flag: &str, value: &str) -> Result<T> {
    value
        .parse()
        .map_err(|_| UpdateError::usage(format!("{flag} needs a nonnegative integer")))
}

#[cfg(test)]
mod auth_transfer_tests {
    use super::*;

    #[test]
    fn auth_transfer_requires_a_declared_source_and_an_explicit_attended_recreation() {
        let source = std::env::temp_dir().join(format!(
            "pseudolife-auth-transfer-{}.env",
            uuid::Uuid::new_v4()
        ));
        std::fs::write(&source, "PSEUDOLIFE_MCP_TOKEN=fixture\n").unwrap();
        let valid = Options::parse(vec![
            "--allow-auth-change".into(),
            "--env-file".into(),
            source.clone().into_os_string(),
            "--tag".into(),
            "0.21.0".into(),
            "--reinstall".into(),
            "--daemon-only".into(),
        ])
        .unwrap();
        for case in 0..11 {
            let mut invalid = valid.clone();
            match case {
                0 => invalid.env_file = None,
                1 => invalid.tag = None,
                2 => invalid.reinstall = false,
                3 => invalid.daemon_only = false,
                4 => invalid.unattended = true,
                5 => invalid.clients_only = true,
                6 => invalid.check = true,
                7 => invalid.schedule = Some("07:05".into()),
                8 => invalid.checkout = Some(std::env::temp_dir()),
                9 => invalid.engine = Some("python".into()),
                _ => invalid.unschedule = true,
            }
            assert!(
                invalid
                    .validate()
                    .unwrap_err()
                    .message
                    .contains("--allow-auth-change requires")
            );
        }
        std::fs::write(&source, "# no bearer settings\n").unwrap();
        assert!(valid.validate().is_err());
        std::fs::remove_file(source).unwrap();
    }
}
