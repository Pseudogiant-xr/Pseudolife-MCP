//! Approving the selected hooks (`hooks.state.<key>.trusted_hash` set to
//! the hash Codex itself computed) and board mail's `memory_message` tool,
//! through Codex's versioned configuration API.
use super::{Fail, Hook, Report};
use crate::setup::codex::{Edit, Rpc, SERVER, WritePlan, user_config};
use serde_json::{Value, json};
use std::path::Path;

pub const MAILBOX_TOOL: &str = "memory_message";

fn hash_ok(hash: &str) -> bool {
    hash.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

pub async fn trust_hooks(
    client: &mut impl Rpc,
    config: &Value,
    hooks: &[&Hook],
    home: &Path,
    cwd: &Path,
    report: &mut Report,
) -> Result<(), Fail> {
    let mut edits = Vec::new();
    for hook in hooks {
        if hook.trust_status == "trusted" {
            continue;
        }
        if hook.is_managed || !matches!(hook.trust_status.as_str(), "untrusted" | "modified") {
            return Err(Fail::new(
                "Codex policy or an unsupported trust state prevents automatic approval; approve the hooks in Codex /hooks.",
            ));
        }
        if !hash_ok(&hook.current_hash) {
            return Err(Fail::new(
                "Unsupported Codex hook hash format; approve the hooks in Codex /hooks.",
            ));
        }
        edits.push(Edit::new(
            &["hooks", "state", &hook.key, "trusted_hash"],
            json!(hook.current_hash),
        ));
    }
    if edits.is_empty() {
        return Ok(());
    }
    user_config(config, home).map_err(|_| {
        Fail::new(
            "Codex did not expose a versioned user configuration; approve the hooks in Codex /hooks.",
        )
    })?;
    let applied = WritePlan::new(config, home, edits)?
        .apply(client, cwd)
        .await?;
    if let Some(backup) = applied.backup {
        report.backups.push(backup);
    }
    Ok(())
}

fn tool_mode(server: &Value) -> Option<Value> {
    server
        .get("tools")
        .and_then(|t| t.get(MAILBOX_TOOL))
        .and_then(|t| t.get("approval_mode"))
        .filter(|v| !v.is_null())
        .cloned()
}

/// The approval mode Codex holds for `memory_message` on Pseudolife's
/// server (the effective value first, so another layer's choice is kept;
/// the user layer's where Codex does not report the tool table), and that
/// server's user-layer table.
fn mailbox_mode(config: &Value, home: &Path) -> (Option<Value>, Option<Value>) {
    let effective = &config["config"]["mcp_servers"][SERVER];
    let user = user_config(config, home)
        .ok()
        .map(|u| u.config["mcp_servers"][SERVER].clone())
        .filter(Value::is_object);
    let current = tool_mode(effective).or_else(|| user.as_ref().and_then(tool_mode));
    (current, user)
}

/// Record in `report` whether `memory_message` is approved, approving it
/// when consent was given (`client`) and nothing is set: "set", "already",
/// "kept-explicit" (another value stays), "declined" (no consent) or
/// "unavailable" (why, in `mailbox_approval_detail`). Never fails setup.
pub async fn mailbox_approval<R: Rpc>(
    config: &Value,
    home: &Path,
    report: &mut Report,
    client: Option<(&mut R, &Path)>,
) -> String {
    let result: Result<String, Fail> = async {
        let (config, client) = match client {
            Some((client, cwd)) => {
                // The trust write just before moved the version.
                let fresh = client
                    .rpc("config/read", json!({"includeLayers":true,"cwd":cwd}))
                    .await?;
                (fresh, Some((client, cwd)))
            }
            None => (config.clone(), None),
        };
        let (current, server) = mailbox_mode(&config, home);
        if current.as_ref().and_then(Value::as_str) == Some("approve") {
            return Ok("already".into());
        }
        if let Some(current) = current {
            report.mailbox_approval_detail = Some(format!("approval_mode = {current}"));
            return Ok("kept-explicit".into());
        }
        let Some((client, cwd)) = client else {
            return Ok("declined".into());
        };
        // Writing the key without the server's own table would leave Codex
        // a server with no command.
        if server
            .as_ref()
            .and_then(Value::as_object)
            .is_none_or(|s| s.is_empty())
        {
            return Err(Fail::new(
                "Codex's user configuration has no pseudolife-memory server.",
            ));
        }
        let plan = WritePlan::new(
            &config,
            home,
            vec![Edit::new(
                &[
                    "mcp_servers",
                    SERVER,
                    "tools",
                    MAILBOX_TOOL,
                    "approval_mode",
                ],
                json!("approve"),
            )],
        )?;
        if let Some(backup) = plan.apply(client, cwd).await?.backup {
            report.backups.push(backup);
        }
        Ok("set".into())
    }
    .await;
    let state = result.unwrap_or_else(|error| {
        report.mailbox_approval_detail = Some(error.to_string());
        "unavailable".into()
    });
    report.mailbox_approval = Some(state.clone());
    state
}

/// The end-of-setup line for ready hooks: what happened to the
/// `memory_message` approval, and the choice still open where none was set.
pub fn mailbox_notice(state: &str, detail: Option<&str>) -> String {
    let table = format!("[mcp_servers.{SERVER}.tools.{MAILBOX_TOOL}]");
    let see = "\nSee docs/guide/configuration.md (Experimental agent coordination).";
    match state {
        "set" => format!(
            "Hooks ready; setup approved memory_message (approval_mode = \"approve\" under {table} in Codex config.toml), so a thread woken by board mail can receive, ack and send without an approval prompt. No other tool was approved.{see}"
        ),
        "already" => format!(
            "Hooks ready; memory_message is already approved (approval_mode = \"approve\"), so a thread woken by board mail can receive, ack and send without an approval prompt.{see}"
        ),
        _ => {
            let lead = match state {
                "kept-explicit" => format!(
                    "Hooks ready; setup kept your memory_message {}. ",
                    detail.unwrap_or_default()
                ),
                "unavailable" => format!(
                    "Hooks ready; setup could not approve memory_message: {} ",
                    detail.unwrap_or_default()
                ),
                _ => "Hooks ready; setup leaves tool approvals unchanged. ".into(),
            };
            format!(
                "{lead}For unattended receive, ack and send, choose approval_mode = \"approve\" under {table} in Codex config.toml. Without that approval, a woken thread can stall on an approval prompt.{see}"
            )
        }
    }
}
