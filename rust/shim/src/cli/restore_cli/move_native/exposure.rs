//! Scope of the target's persistent tailnet forward during a native move.
use std::{fs, net::Ipv4Addr, path::Path};

use serde::{Deserialize, Serialize};

use super::ops::Site;

const RECEIPT: &str = "exposure.json";
const UNKNOWN: &str = "the target forward's ownership could not be established; inspect its exposure receipt and run pseudolife-mcp expose status on the target before removing any forward";

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
}

pub(super) fn parse(code: i32, output: &[u8], port: u16) -> Result<Report, String> {
    let refused = "the target's native expose command could not verify its tailnet forward; configure Tailscale there or pass --target-url";
    if code != 0 {
        return Err(refused.into());
    }
    let response: serde_json::Value = serde_json::from_slice(output).map_err(|_| refused)?;
    match response["state"].as_str() {
        Some("not_exposed") => Ok(Report::Absent),
        Some("removed") => Ok(Report::Removed),
        Some(state @ ("exposed" | "already_exposed")) => {
            let url = response["url"]
                .as_str()
                .filter(|url| valid_url(url, port))
                .ok_or(refused)?;
            if state == "exposed" {
                Ok(Report::Created(url.to_owned()))
            } else {
                Ok(Report::Existing(url.to_owned()))
            }
        }
        _ => Err(refused.into()),
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
    if super::carry::loopback(health_url)
        && let Ok(url) = reqwest::Url::parse(health_url)
        && url.scheme() == "http"
        && let Some(port) = url.port_or_known_default()
    {
        return Ok(port);
    }
    Err("the target's daemon has no usable loopback HTTP binding for Tailscale Serve; configure that binding or pass --target-url".into())
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
        Report::Removed => return Err(UNKNOWN.into()),
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
    match fs::read(folder.join(RECEIPT)) {
        Ok(bytes) => {
            let receipt: Receipt = serde_json::from_slice(&bytes).map_err(|_| UNKNOWN)?;
            if receipt.port != port {
                return Err(UNKNOWN.into());
            }
            Ok(Some(receipt))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(UNKNOWN.into()),
    }
}

fn save(folder: &Path, port: u16, ownership: Ownership) -> Result<(), String> {
    let data = serde_json::to_vec(&Receipt { port, ownership }).map_err(|_| UNKNOWN)?;
    crate::board::state::atomic_write(&folder.join(RECEIPT), &data, None)
        .map_err(|_| UNKNOWN.into())
}

pub(super) fn create(site: &mut impl Site, folder: &Path, port: u16) -> Result<String, String> {
    let observed = status(site.expose(Action::Status, port)?, port)?.url;
    if let Some(receipt) = read(folder, port)? {
        match receipt.ownership {
            Ownership::Pending => return Err(UNKNOWN.into()),
            Ownership::Owned { url } | Ownership::Borrowed { url } => {
                return (observed.as_ref() == Some(&url))
                    .then_some(url)
                    .ok_or_else(|| UNKNOWN.into());
            }
            Ownership::Removed => {}
        }
    }
    if let Some(url) = observed {
        save(folder, port, Ownership::Borrowed { url: url.clone() })?;
        return Ok(url);
    }
    save(folder, port, Ownership::Pending)?;
    let (url, ownership) = match site.expose(Action::Create, port)? {
        Report::Created(url) => (url.clone(), Ownership::Owned { url }),
        Report::Existing(url) => (url.clone(), Ownership::Borrowed { url }),
        _ => return Err(UNKNOWN.into()),
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
        return Err(UNKNOWN.into());
    };
    let observed = status(site.expose(Action::Status, port)?, port)?.url;
    if let Some(observed) = observed
        && (observed != url
            || !matches!(
                site.expose(Action::Remove, port)?,
                Report::Removed | Report::Absent
            ))
    {
        return Err(UNKNOWN.into());
    }
    save(folder, port, Ownership::Removed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    const PORT: u16 = 8765;
    const URL: &str = "http://100.64.0.7:8765";

    #[derive(Default)]
    struct Fixture {
        url: Option<String>,
        lose_create_result: bool,
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

    struct Folder(PathBuf);

    impl Folder {
        fn new() -> Self {
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
                .contains("ownership")
        );
        assert!(
            undo(&mut lost, &folder.0, PORT)
                .unwrap_err()
                .contains("ownership")
        );
        assert_eq!((lost.creates, lost.removes), (1, 0));
        assert!(lost.url.is_some());
        lost.url = None;
        assert!(
            undo(&mut lost, &folder.0, PORT)
                .unwrap_err()
                .contains("ownership")
        );
        assert_eq!(lost.removes, 0);
    }
}
