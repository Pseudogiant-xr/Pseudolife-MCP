//! Tailscale HTTPS ownership checks and rollback of this command's serve.
use super::{Args, Error, Result, SETUP, system};
use serde_json::{Value, json};
use std::{ffi::OsString, path::PathBuf};

pub(super) struct Tailnet {
    pub binary: PathBuf,
    pub dns: String,
    pub certs: Vec<String>,
}
async fn call(binary: &std::path::Path, args: &[&str]) -> system::Output {
    let mut command = vec![binary.as_os_str().to_owned()];
    command.extend(args.iter().map(OsString::from));
    system::run(&command, 30).await
}
pub(super) async fn detect() -> Result<Option<Tailnet>> {
    let Some(binary) = system::tailscale()? else {
        return Ok(None);
    };
    let output = call(&binary, &["status", "--json"]).await;
    let value: Value = serde_json::from_str(&output.out).unwrap_or(Value::Null);
    if !value.is_object() {
        return Err(Error::refused(format!(
            "tailscale is not running ({}): start Tailscale and run `tailscale up`; or pass --local to use http://localhost on this machine only",
            output.detail()
        )));
    }
    if value["BackendState"] != "Running" {
        return Err(Error::refused(format!(
            "tailscale reports BackendState {}, not 'Running': run `tailscale up` (and log in) first; or pass --local to use http://localhost on this machine only",
            value["BackendState"]
                .as_str()
                .map(super::python_repr)
                .unwrap_or_else(|| "None".into())
        )));
    }
    let dns = value["Self"]["DNSName"]
        .as_str()
        .unwrap_or("")
        .trim_end_matches('.')
        .to_lowercase();
    let certs = value["CertDomains"]
        .as_array()
        .map(|v| {
            v.iter()
                .filter_map(|v| v.as_str())
                .map(|v| v.trim_end_matches('.').to_lowercase())
                .collect()
        })
        .unwrap_or_default();
    Ok(Some(Tailnet { binary, dns, certs }))
}
pub(super) fn choose(args: &Args, ts: Option<&Tailnet>) -> Result<(String, String)> {
    if args.local || ts.is_none() {
        return Ok((
            "localhost".into(),
            format!(
                "http://localhost{}",
                if args.port == 80 {
                    String::new()
                } else {
                    format!(":{}", args.port)
                }
            ),
        ));
    }
    let ts = ts.unwrap();
    if ts.dns.is_empty() {
        return Err(Error::refused(
            "tailscale reports no DNS name for this host; pass --local to use http://localhost on this machine only",
        ));
    }
    if !ts.certs.contains(&ts.dns) {
        return Err(Error::refused(format!(
            "HTTPS certificates are off for this tailnet, so the Console cannot be served over HTTPS here: turn them on in the Tailscale admin console (DNS, HTTPS Certificates) and run this again, or pass --local to use http://localhost:{} on this machine only",
            args.port
        )));
    }
    Ok((
        ts.dns.clone(),
        format!(
            "https://{}{}",
            ts.dns,
            if args.https_port == 443 {
                String::new()
            } else {
                format!(":{}", args.https_port)
            }
        ),
    ))
}
pub(super) async fn configuration(ts: &Tailnet) -> Result<Value> {
    let output = call(&ts.binary, &["serve", "status", "--json"]).await;
    if output.code != 0 {
        return Err(Error::refused(format!(
            "could not read `tailscale serve status --json` ({})",
            output.detail()
        )));
    }
    if output.out.trim().is_empty() || output.out.trim() == "null" {
        return Ok(json!({}));
    }
    let value: Value = serde_json::from_str(&output.out).map_err(|_| {
        Error::refused("`tailscale serve status --json` printed something that is not JSON")
    })?;
    if !value.is_object() {
        return Err(Error::refused(
            "`tailscale serve status --json` printed an unexpected shape",
        ));
    }
    Ok(value)
}
pub(super) fn state(
    config: &Value,
    https: u16,
    dns: &str,
    port: u16,
) -> (&'static str, Option<String>) {
    let key = https.to_string();
    if config["AllowFunnel"].as_object().is_some_and(|m| {
        m.iter()
            .any(|(k, v)| k.ends_with(&format!(":{https}")) && truthy(v))
    }) {
        return (
            "foreign",
            Some(format!("Funnel (public internet) on port {https}")),
        );
    }
    let tcp = &config["TCP"][&key];
    let site = &config["Web"][format!("{dns}:{https}")];
    if tcp.is_null() && site.is_null() {
        if config["Foreground"]
            .as_object()
            .is_some_and(|m| m.values().any(|v| !v["TCP"][&key].is_null()))
        {
            return ("foreign", Some("a foreground serve".into()));
        }
        return ("none", None);
    }
    let handlers = &site["Handlers"];
    let proxy = handlers["/"]["Proxy"].as_str();
    let wanted = [
        format!("http://127.0.0.1:{port}"),
        format!("http://localhost:{port}"),
        format!("127.0.0.1:{port}"),
    ];
    if tcp.is_object()
        && truthy(&tcp["HTTPS"])
        && proxy.is_some_and(|v| wanted.iter().any(|w| w == v))
        && handlers
            .as_object()
            .is_some_and(|m| m.len() == 1 && m.contains_key("/"))
    {
        return ("ours", None);
    }
    if let Some(proxy) = proxy.filter(|v| !v.is_empty()) {
        return ("foreign", Some(format!("HTTPS proxying to {proxy}")));
    }
    let mut desc = Vec::new();
    if truthy(&tcp["TCPForward"]) {
        desc.push(format!(
            "a TCP forward to {}",
            tcp["TCPForward"].as_str().unwrap_or("")
        ));
    }
    if truthy(&tcp["TerminateTLS"]) {
        desc.push(format!(
            "TLS terminated for {}",
            tcp["TerminateTLS"].as_str().unwrap_or("")
        ));
    }
    if truthy(&tcp["HTTPS"]) {
        desc.push("HTTPS".into());
    }
    if truthy(&tcp["HTTP"]) {
        desc.push("HTTP".into());
    }
    if truthy(&tcp["ProxyProtocol"]) {
        desc.push(format!("PROXY protocol v{}", tcp["ProxyProtocol"]));
    }
    let detail = if tcp.is_null() {
        "another web serve".into()
    } else if !tcp.is_object() {
        "an unreadable handler".into()
    } else if desc.is_empty() {
        "a handler of an unknown kind".into()
    } else {
        desc.join(", ")
    };
    ("foreign", Some(detail))
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
        Value::Number(v) => v.as_f64() != Some(0.0),
    }
}
pub(super) async fn off(ts: &Tailnet, https: u16) -> bool {
    call(&ts.binary, &["serve", &format!("--https={https}"), "off"])
        .await
        .code
        == 0
}
pub(super) async fn serve(ts: &Tailnet, https: u16, port: u16) -> Result<()> {
    let output = call(
        &ts.binary,
        &[
            "serve",
            "--bg",
            &format!("--https={https}"),
            &format!("http://127.0.0.1:{port}"),
        ],
    )
    .await;
    let permission = regex::Regex::new(
        r"(?i)access denied|permission denied|not permitted|--operator\b|unauthori[sz]ed|must be (?:root|admin)",
    )
    .unwrap();
    if output.code != 0 && permission.is_match(&output.err) {
        let fix = if cfg!(windows) {
            "run it as the Windows user logged in to Tailscale, or from an elevated (administrator) shell"
        } else if cfg!(target_os = "macos") {
            "run it as the macOS user logged in to the Tailscale app"
        } else {
            "grant your user operator rights once with `sudo tailscale set --operator=$USER`, or run this as root"
        };
        return Err(Error::refused(format!(
            "tailscale refused the change for this user ({}): {fix}",
            output.detail()
        )));
    }
    let after=configuration(ts).await.map_err(|e|Error::new(format!("the serve command ran but its result could not be read ({e}): check `tailscale serve status`"),5))?;
    let (after, _) = state(&after, https, &ts.dns, port);
    if output.code == 0 && after == "ours" {
        println!(
            "served: https://{}{} -> http://127.0.0.1:{port}",
            ts.dns,
            if https == 443 {
                String::new()
            } else {
                format!(":{https}")
            }
        );
        return Ok(());
    }
    let undone = if after == "none" {
        "nothing to undo".into()
    } else if off(ts, https).await {
        "undone".into()
    } else {
        format!("the undo failed: run `tailscale serve --https={https} off` yourself")
    };
    let mut reason = if output.err.trim().is_empty() && output.code == 0 {
        "the serve status does not show it".into()
    } else {
        output.detail()
    };
    if let Some(link) = regex::Regex::new(r"https://login\.tailscale\.com/\S+")
        .unwrap()
        .find(&output.out)
    {
        reason.push_str(&format!(
            "; Serve may be off for this tailnet: turn it on at {}, then run `{SETUP}` again",
            link.as_str()
        ));
    }
    Err(Error::new(
        format!("the HTTPS serve did not take ({reason}); {undone}"),
        5,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn foreign_serves_and_public_funnel_are_never_owned() {
        let ours = json!({"TCP":{"8443":{"HTTPS":true}},"Web":{"node.invalid:8443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8765"}}}}});
        assert_eq!(state(&ours, 8443, "node.invalid", 8765).0, "ours");
        let mut foreign = ours.clone();
        foreign["AllowFunnel"] = json!({"node.invalid:8443":true});
        assert_eq!(state(&foreign, 8443, "node.invalid", 8765).0, "foreign");
        let mut foreign = ours;
        foreign["Web"]["node.invalid:8443"]["Handlers"]["/other"] = json!({"Text":"unrelated"});
        assert_eq!(state(&foreign, 8443, "node.invalid", 8765).0, "foreign");
        assert_eq!(
            state(
                &json!({"Foreground":{"session":{"TCP":{"8443":{"HTTPS":true}}}}}),
                8443,
                "node.invalid",
                8765
            )
            .0,
            "foreign"
        );
        assert_eq!(state(&json!({}), 8443, "node.invalid", 8765).0, "none");
    }
}
