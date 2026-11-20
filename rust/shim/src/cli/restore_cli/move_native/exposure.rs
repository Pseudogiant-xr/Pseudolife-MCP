//! Scope of the target's persistent tailnet forward during a native move.
use std::{fs, net::Ipv4Addr, path::Path};

use serde::{Deserialize, Serialize};

use super::ops::Site;

const RECEIPT: &str = "exposure.json";

fn inspect(port: u16) -> String {
    format!("run pseudolife-mcp expose status --port {port} --json on the target")
}

fn settle_steps(folder: &Path, port: u16) -> String {
    let folder = super::super::bank::sh_quote(&folder.to_string_lossy());
    format!(
        "On the target, verify in the process list that the earlier native expose and Tailscale client processes have exited (on Linux, ps -eo pid,args shows their command lines). Check pseudolife-mcp expose status --port {port} --json and remove only this move's verified forward with pseudolife-mcp expose off --port {port}. Once the port's forward is absent, run pseudolife-mcp move-target settle-exposure {folder} {port} --confirmed-idle on the target"
    )
}

fn pending(folder: &Path, port: u16) -> String {
    format!(
        "the target exposure outcome is unknown. {}",
        settle_steps(folder, port)
    )
}

#[derive(Clone, Copy)]
pub(super) enum Action {
    Status,
    Create,
    Remove,
}

impl Action {
    pub(super) fn arguments(self, port: u16) -> Vec<String> {
        let subcommand = match self {
            Self::Status => "status",
            Self::Create => "tailscale",
            Self::Remove => "off",
        };
        let mut args = vec![
            "expose".into(),
            subcommand.into(),
            "--port".into(),
            port.to_string(),
            "--json".into(),
        ];
        if !matches!(self, Self::Status) {
            args.push("--yes".into());
        }
        args
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Report {
    Absent,
    Existing(String),
    Created(String),
    Removed,
    NotCreated(String),
}

pub(super) fn parse(action: Action, code: i32, output: &[u8], port: u16) -> Result<Report, String> {
    let refused = || {
        format!(
            "the target's native expose command could not verify the tailnet forward; {} before retrying",
            inspect(port)
        )
    };
    let response: serde_json::Value = serde_json::from_slice(output).map_err(|_| refused())?;
    if code != 0 {
        if matches!(action, Action::Create)
            && response["action"] == "tailscale"
            && response["port"].as_u64() == Some(u64::from(port))
            && response["exit"].as_i64() == Some(i64::from(code))
        {
            let detail = response["move_failure_detail"].as_str().filter(|detail| {
                !detail.is_empty() && detail.len() <= 512 && !detail.chars().any(char::is_control)
            });
            let why = match response["move_failure"].as_str() {
                Some("permission_denied") => format!(
                    "Tailscale refused to create the forward for this user; {}",
                    crate::cli::maintainer::setup::tailscale_permission_fix(std::env::consts::OS)
                ),
                Some("serve_not_enabled") => "Tailscale Serve is not enabled for the target's tailnet; have a tailnet administrator enable Serve before retrying".into(),
                Some("not_attempted") => detail.map_or_else(|| format!("the native expose checks refused to begin; {} to check the target setup", inspect(port)), str::to_owned),
                Some("not_created") => detail.map_or_else(|| format!("Tailscale did not leave a forward; {} before retrying", inspect(port)), str::to_owned),
                _ => return Err(refused()),
            };
            return Ok(Report::NotCreated(why));
        }
        return Err(refused());
    }
    match response["state"].as_str() {
        Some("not_exposed") => Ok(Report::Absent),
        Some("removed") => Ok(Report::Removed),
        Some(state @ ("exposed" | "already_exposed")) => {
            let url = response["url"]
                .as_str()
                .filter(|url| valid_url(url, port))
                .ok_or_else(refused)?;
            if state == "exposed" {
                Ok(Report::Created(url.to_owned()))
            } else {
                Ok(Report::Existing(url.to_owned()))
            }
        }
        _ => Err(refused()),
    }
}

pub(super) fn valid_url(url: &str, port: u16) -> bool {
    super::argv::origin(url).is_ok()
        && reqwest::Url::parse(url).is_ok_and(|url| {
            url.scheme() == "http"
                && url.port_or_known_default() == Some(port)
                && url
                    .host_str()
                    .is_some_and(|host| host.parse::<Ipv4Addr>().is_ok())
        })
}

pub(super) fn port(health_url: &str) -> Result<u16, String> {
    if let Ok(url) = reqwest::Url::parse(health_url)
        && url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && let Some(port) = url.port_or_known_default()
    {
        return Ok(port);
    }
    Err("the selected daemon needs a 127.0.0.1 HTTP binding for Tailscale Serve; configure that binding or pass --target-url".into())
}

pub(super) fn wait_ready(
    bank: &str,
    mut get: impl FnMut() -> Option<serde_json::Value>,
    mut delay: impl FnMut(),
) -> Result<(), String> {
    for _ in 0..90 {
        if let Some(health) = get()
            && health["status"] == "ok"
            && health["ready"] != false
        {
            return if health["bank"] == bank && health["auth"] == true {
                Ok(())
            } else {
                Err("the target loopback endpoint did not identify the authenticated moved bank; verify the selected daemon and binding".into())
            };
        }
        delay();
    }
    Err("the selected target daemon did not become ready for exposure; check its local health endpoint before retrying".into())
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(in crate::cli) struct Status {
    pub(super) port: u16,
    pub(super) url: Option<String>,
}

pub(super) fn status(report: Report, port: u16) -> Result<Status, String> {
    let url = match report {
        Report::Absent => None,
        Report::Existing(url) | Report::Created(url) => Some(url),
        Report::Removed | Report::NotCreated(_) => {
            return Err(format!(
                "the target returned an unexpected exposure status; {}",
                inspect(port)
            ));
        }
    };
    Ok(Status { port, url })
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "ownership")]
enum Ownership {
    Pending,
    Borrowed { url: String },
    Owned { url: String },
    Removed,
}

#[derive(Serialize, Deserialize)]
struct Receipt {
    port: u16,
    #[serde(flatten)]
    ownership: Ownership,
}

fn read(folder: &Path, port: u16) -> Result<Option<Receipt>, String> {
    let path = folder.join(RECEIPT);
    match fs::read(&path) {
        Ok(bytes) => {
            let receipt: Receipt = serde_json::from_slice(&bytes).map_err(|_| format!("the exposure receipt at {path:?} is unreadable JSON; recover the original receipt before retrying"))?;
            if receipt.port != port {
                return Err(format!(
                    "the exposure receipt at {path:?} names a different port; verify the original move and its exposure receipt before retrying"
                ));
            }
            Ok(Some(receipt))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(format!(
            "the exposure receipt at {path:?} could not be read; restore read access before retrying"
        )),
    }
}

fn save(folder: &Path, port: u16, ownership: Ownership) -> Result<(), String> {
    let data = serde_json::to_vec(&Receipt { port, ownership }).map_err(
        |_| "the exposure receipt could not be encoded; verify the target client before retrying",
    )?;
    crate::board::state::atomic_write(&folder.join(RECEIPT), &data, None)
        .map_err(|_| "the exposure receipt could not be saved; verify the move folder is writable before retrying".into())
}

pub(super) fn create(site: &mut impl Site, folder: &Path, port: u16) -> Result<String, String> {
    let observed = status(site.expose(Action::Status, port)?, port)?.url;
    if let Some(receipt) = read(folder, port)? {
        match receipt.ownership {
            Ownership::Pending => return Err(pending(folder, port)),
            Ownership::Owned { .. } if observed.is_none() => {
                return Err("the move's recorded forward is absent; let rollback settle the exposure receipt before resuming".into());
            }
            Ownership::Owned { url } | Ownership::Borrowed { url } => {
                return (observed.as_ref() == Some(&url))
                    .then_some(url)
                    .ok_or_else(|| format!("the observed forward differs from the exposure receipt; {} before retrying", inspect(port)));
            }
            Ownership::Removed => {}
        }
    }
    if let Some(url) = observed {
        save(folder, port, Ownership::Borrowed { url: url.clone() })?;
        return Ok(url);
    }
    save(folder, port, Ownership::Pending)?;
    let (url, ownership) = match site
        .expose(Action::Create, port)
        .map_err(|_| pending(folder, port))?
    {
        Report::Created(url) => (url.clone(), Ownership::Owned { url }),
        Report::Existing(url) => (url.clone(), Ownership::Borrowed { url }),
        Report::NotCreated(why) => {
            save(folder, port, Ownership::Removed)?;
            return Err(why);
        }
        _ => return Err(pending(folder, port)),
    };
    save(folder, port, ownership)?;
    Ok(url)
}

pub(super) fn undo(site: &mut impl Site, folder: &Path, port: u16) -> Result<(), String> {
    let Some(receipt) = read(folder, port)? else {
        return Ok(());
    };
    if matches!(
        receipt.ownership,
        Ownership::Borrowed { .. } | Ownership::Removed
    ) {
        return Ok(());
    }
    // The expose child can outlive the target helper. An absent snapshot
    // cannot settle an attempt whose result never reached the journal.
    let Ownership::Owned { url } = receipt.ownership else {
        return Err(pending(folder, port));
    };
    let observed = status(site.expose(Action::Status, port)?, port)?.url;
    if let Some(observed) = observed
        && (observed != url
            || !matches!(
                site.expose(Action::Remove, port)?,
                Report::Removed | Report::Absent
            ))
    {
        return Err(format!(
            "the receipt's forward could not be verified and removed; {} before retrying",
            inspect(port)
        ));
    }
    save(folder, port, Ownership::Removed)
}

/// A detached release may close its source record after settlement, without
/// taking a claim or changing the target.
pub(super) fn check_release_receipt(
    folder: &Path,
    port: u16,
    unclaimed: bool,
) -> Result<(), String> {
    let cause = match read(folder, port)?.map(|receipt| receipt.ownership) {
        None | Some(Ownership::Removed | Ownership::Borrowed { .. }) => return Ok(()),
        Some(Ownership::Pending) => "the target exposure outcome is unknown",
        Some(Ownership::Owned { .. }) => "the receipt still records a forward owned by this move",
    };
    let next = if unclaimed {
        format!(
            "Before making any exposure changes, confirm that no other move holds the target claim. {}",
            settle_steps(folder, port)
        )
    } else {
        "Leave the forward in place while the target is claimed or its claim cannot be read. Verify that the target is unclaimed before retrying release".into()
    };
    Err(format!("{cause}. {next}"))
}

/// Called only after the operator confirms the earlier operations exited.
pub(super) fn settle_idle(site: &mut impl Site, folder: &Path, port: u16) -> Result<(), String> {
    let Some(receipt) = read(folder, port)? else {
        return Err(
            "the move folder has no exposure receipt; verify the move folder before retrying"
                .into(),
        );
    };
    match receipt.ownership {
        Ownership::Removed => return Ok(()),
        Ownership::Pending | Ownership::Owned { .. } => {}
        Ownership::Borrowed { .. } => {
            return Err(
                "the receipt records a borrowed forward; preserve it and use the usual move rollback or release"
                    .into(),
            );
        }
    }
    if status(site.expose(Action::Status, port)?, port)?
        .url
        .is_some()
    {
        return Err(if matches!(receipt.ownership, Ownership::Owned { .. }) {
            format!(
                "the receipt records this move's forward and port {port} still has a forward; {} and verify its ownership before retrying settlement",
                inspect(port)
            )
        } else {
            pending(folder, port)
        });
    }
    save(folder, port, Ownership::Removed)
}

#[cfg(test)]
pub(super) mod tests {
    use super::*;
    use std::path::PathBuf;

    const PORT: u16 = 8765;
    const URL: &str = "http://100.64.0.7:8765";

    #[test]
    fn exposure_requires_the_exact_ipv4_forward_destination() {
        assert_eq!(port("http://127.0.0.1:8765").unwrap(), PORT);
        assert!(port("http://127.0.0.2:8765").is_err());
        assert!(port("http://[::1]:8765").is_err());
    }

    #[test]
    fn exposure_waits_for_the_authenticated_moved_bank() {
        let health = |bank, ready| serde_json::json!({"status":"ok", "ready":ready, "auth":true, "bank":bank});
        let mut replies = [
            None,
            Some(health("moved", false)),
            Some(health("moved", true)),
        ]
        .into_iter();
        let mut waits = 0;
        wait_ready("moved", || replies.next().flatten(), || waits += 1).unwrap();
        assert_eq!(waits, 2);
        assert!(
            wait_ready(
                "moved",
                || Some(health("other", true)),
                || panic!("wrong bank was retried")
            )
            .is_err()
        );
    }

    #[derive(Default)]
    pub(in crate::cli::restore_cli::move_native) struct Fixture {
        pub(in crate::cli::restore_cli::move_native) url: Option<String>,
        pub(in crate::cli::restore_cli::move_native) lose_create_result: bool,
        clean_failure: Option<String>,
        creates: usize,
        removes: usize,
    }

    impl Site for Fixture {
        type D = super::super::super::docker::Real;
        fn docker(&mut self) -> Result<&mut Self::D, String> {
            Err("unused".into())
        }
        fn home(&self) -> Option<PathBuf> {
            None
        }
        fn now(&self) -> String {
            String::new()
        }
        fn systemctl(&mut self, _: &[&str]) -> Result<String, String> {
            Err("unused".into())
        }
        fn restore(&mut self, _: &[String], _: Option<&Path>) -> (i32, String) {
            (1, "unused".into())
        }
        fn expose(&mut self, action: Action, port: u16) -> Result<Report, String> {
            assert_eq!(port, PORT);
            match action {
                Action::Status => Ok(self.url.clone().map_or(Report::Absent, Report::Existing)),
                Action::Create => {
                    self.creates += 1;
                    if let Some(why) = self.clean_failure.take() {
                        return Ok(Report::NotCreated(why));
                    }
                    self.url = Some(URL.into());
                    if self.lose_create_result {
                        Err("creation result lost".into())
                    } else {
                        Ok(Report::Created(URL.into()))
                    }
                }
                Action::Remove => {
                    self.removes += 1;
                    self.url = None;
                    Ok(Report::Removed)
                }
            }
        }
    }

    impl Fixture {
        pub(in crate::cli::restore_cli::move_native) fn with_clean_refusal() -> Self {
            Self {
                clean_failure: Some("fixture exposure refusal".into()),
                ..Self::default()
            }
        }
    }

    pub(in crate::cli::restore_cli::move_native) struct Folder(
        pub(in crate::cli::restore_cli::move_native) PathBuf,
    );

    impl Folder {
        pub(in crate::cli::restore_cli::move_native) fn new() -> Self {
            let path = std::env::temp_dir()
                .join(format!("move-exposure-{}", uuid::Uuid::new_v4().simple()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for Folder {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn only_a_verified_owned_forward_is_removed_and_retries_are_idempotent() {
        let folder = Folder::new();
        let mut fixture = Fixture::default();
        assert_eq!(create(&mut fixture, &folder.0, PORT).unwrap(), URL);
        assert_eq!(create(&mut fixture, &folder.0, PORT).unwrap(), URL);
        assert_eq!(fixture.creates, 1);
        assert!(undo(&mut fixture, &folder.0, PORT + 1).is_err());
        fixture.url = Some("http://100.64.0.8:8765".into());
        assert!(undo(&mut fixture, &folder.0, PORT).is_err());
        assert_eq!(fixture.removes, 0);
        fixture.url = Some(URL.into());
        undo(&mut fixture, &folder.0, PORT).unwrap();
        undo(&mut fixture, &folder.0, PORT).unwrap();
        assert!(fixture.url.is_none());
        assert_eq!(fixture.removes, 1);
    }

    #[test]
    fn borrowed_and_unattributed_forwards_stay_in_place() {
        let folder = Folder::new();
        let mut borrowed = Fixture {
            url: Some(URL.into()),
            ..Default::default()
        };
        assert_eq!(create(&mut borrowed, &folder.0, PORT).unwrap(), URL);
        undo(&mut borrowed, &folder.0, PORT).unwrap();
        assert_eq!((borrowed.creates, borrowed.removes), (0, 0));
        assert!(borrowed.url.is_some());

        let folder = Folder::new();
        let mut lost = Fixture {
            lose_create_result: true,
            ..Default::default()
        };
        assert!(create(&mut lost, &folder.0, PORT).is_err());
        assert!(
            create(&mut lost, &folder.0, PORT)
                .unwrap_err()
                .contains("settle-exposure")
        );
        assert!(
            undo(&mut lost, &folder.0, PORT)
                .unwrap_err()
                .contains("settle-exposure")
        );
        assert_eq!((lost.creates, lost.removes), (1, 0));
        assert!(lost.url.is_some());
        assert!(settle_idle(&mut lost, &folder.0, PORT).is_err());
        lost.url = None;
        assert!(
            undo(&mut lost, &folder.0, PORT)
                .unwrap_err()
                .contains("settle-exposure")
        );
        settle_idle(&mut lost, &folder.0, PORT).unwrap();
        undo(&mut lost, &folder.0, PORT).unwrap();
        assert_eq!(lost.removes, 0);
    }

    #[test]
    fn clean_exposure_refusals_preserve_the_recovery_state_and_report_the_cause() {
        let report = |failure| {
            serde_json::to_vec(&serde_json::json!({
                "action":"tailscale", "port":PORT, "exit":4, "move_failure":failure,
                "state":null, "error":"fixture diagnostic must stay local",
            }))
            .unwrap()
        };
        assert!(parse(Action::Create, 4, &report("unknown"), PORT).is_err());
        assert!(parse(Action::Create, 4, &report("permission_denied"), PORT + 1).is_err());
        for (kind, expected) in [
            ("permission_denied", "refused to create"),
            ("serve_not_enabled", "enable Serve"),
        ] {
            let Report::NotCreated(why) = parse(Action::Create, 4, &report(kind), PORT).unwrap()
            else {
                panic!("expected a clean refusal")
            };
            assert!(why.contains(expected));
            assert!(!why.contains("fixture diagnostic"));
            let folder = Folder::new();
            let mut fixture = Fixture {
                clean_failure: Some(why.clone()),
                ..Default::default()
            };
            assert_eq!(create(&mut fixture, &folder.0, PORT), Err(why));
            undo(&mut fixture, &folder.0, PORT).unwrap();
            assert_eq!(create(&mut fixture, &folder.0, PORT).unwrap(), URL);
        }
    }

    #[test]
    fn clean_report_keeps_the_native_diagnostic() {
        for failure in ["not_attempted", "not_created"] {
            let detail = "No daemon answers on the selected port. Start it before retrying.";
            let bytes = serde_json::to_vec(&serde_json::json!({
                "action":"tailscale", "port":PORT, "exit":4, "move_failure":failure,
                "move_failure_detail":detail, "error":"raw fixture output",
            }))
            .unwrap();
            assert_eq!(
                parse(Action::Create, 4, &bytes, PORT),
                Ok(Report::NotCreated(detail.into()))
            );
        }
    }

    #[test]
    fn clean_report_discards_invalid_native_diagnostics() {
        for detail in [
            String::new(),
            "é".repeat(257),
            "unexpected\nline".into(),
            "unexpected\u{007f}".into(),
        ] {
            for (failure, expected) in [
                (
                    "not_attempted",
                    "the native expose checks refused to begin; run pseudolife-mcp expose status --port 8765 --json on the target to check the target setup",
                ),
                (
                    "not_created",
                    "Tailscale did not leave a forward; run pseudolife-mcp expose status --port 8765 --json on the target before retrying",
                ),
            ] {
                let bytes = serde_json::to_vec(&serde_json::json!({
                    "action":"tailscale", "port":PORT, "exit":4, "move_failure":failure,
                    "move_failure_detail":detail,
                }))
                .unwrap();
                assert_eq!(
                    parse(Action::Create, 4, &bytes, PORT),
                    Ok(Report::NotCreated(expected.into())),
                    "{failure}: {detail:?}"
                );
            }
        }
    }

    #[test]
    fn an_absent_owned_forward_can_be_settled_after_operator_confirmation() {
        let folder = Folder::new();
        let mut fixture = Fixture::default();
        create(&mut fixture, &folder.0, PORT).unwrap();
        assert!(
            settle_idle(&mut fixture, &folder.0, PORT)
                .unwrap_err()
                .contains("still has a forward")
        );
        fixture.url = None;
        assert!(
            create(&mut fixture, &folder.0, PORT)
                .unwrap_err()
                .contains("rollback")
        );
        assert!(check_release_receipt(&folder.0, PORT, true).is_err());
        settle_idle(&mut fixture, &folder.0, PORT).unwrap();
        assert!(check_release_receipt(&folder.0, PORT, true).is_ok());
    }
}
