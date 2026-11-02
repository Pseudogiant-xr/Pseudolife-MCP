//! Docker operator rerun and the stopped-daemon psql fallback.
use super::*;
use crate::pg::resolution;
use std::{ffi::OsString, io::Write, process::Stdio, time::Duration};
use tokio::io::AsyncWriteExt;

const POSTGRES: &str = "pseudolife-mcp-postgres";
const PSQL: &str = r#"psql -X -q -tA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB""#;
const LIST: &str = include_str!("list_sql.sql");
const OLD_LIST: &str = include_str!("list_sql_v53.sql");

fn program(name: &str) -> Option<std::path::PathBuf> {
    let mut names = vec![name.to_owned()];
    if cfg!(windows) {
        names.extend(
            std::env::var("PATHEXT")
                .unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into())
                .split(';')
                .filter(|ext| !ext.is_empty())
                .map(|ext| format!("{name}{ext}")),
        );
    }
    for dir in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        for name in &names {
            let path = dir.join(name);
            if path.is_file() {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    if path.metadata().ok()?.permissions().mode() & 0o111 == 0 {
                        continue;
                    }
                }
                return Some(path);
            }
        }
    }
    None
}

pub(super) fn docker_available() -> bool {
    std::env::var_os("PSEUDOLIFE_DOCKER").is_some_and(|v| !v.is_empty())
        || program("docker").is_some()
}

fn docker() -> OsString {
    std::env::var_os("PSEUDOLIFE_DOCKER")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "docker".into())
}

pub(super) async fn exposed_url(port: u32) -> Option<String> {
    let binary = program("tailscale").or_else(|| {
        let mut paths = Vec::new();
        if cfg!(windows) {
            let base = std::env::var_os("ProgramW6432")
                .or_else(|| std::env::var_os("ProgramFiles"))
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(r"C:\Program Files"));
            paths.push(base.join("Tailscale/tailscale.exe"));
        } else if cfg!(target_os = "macos") {
            paths.push(PathBuf::from(
                "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
            ));
        }
        paths.into_iter().find(|p| p.is_file())
    })?;
    let query = |args: &[&str]| {
        let mut command = vec![binary.clone().into_os_string()];
        command.extend(args.iter().map(|arg| OsString::from(*arg)));
        command
    };
    let status = capture(&query(&["status", "--json"]), None).await;
    let status: serde_json::Value = serde_json::from_str(&status.stdout).ok()?;
    if status["BackendState"] != "Running" {
        return None;
    }
    let serve = capture(&query(&["serve", "status", "--json"]), None).await;
    if serve.code != 0 {
        return None;
    }
    let serve: serde_json::Value = serde_json::from_str(&serve.stdout).ok()?;
    if !ours(&serve, port) {
        return None;
    }
    let ip = capture(&query(&["ip", "-4"]), None).await;
    let mut ips: Vec<&str> = if ip.code == 0 {
        ip.stdout.split_whitespace().collect()
    } else {
        Vec::new()
    };
    if let Some(own) = status["Self"]["TailscaleIPs"].as_array() {
        ips.extend(own.iter().filter_map(|ip| ip.as_str()));
    }
    let ip = ips.into_iter().find(|ip| {
        let parts: Vec<_> = ip.split('.').collect();
        parts.len() == 4
            && parts
                .iter()
                .all(|p| (1..=3).contains(&p.len()) && p.bytes().all(|b| b.is_ascii_digit()))
    })?;
    Some(format!("http://{ip}:{port}"))
}

fn ours(config: &serde_json::Value, port: u32) -> bool {
    let funnel = |v: &serde_json::Value| {
        v["AllowFunnel"].as_object().is_some_and(|m| {
            m.iter()
                .any(|(k, v)| k.ends_with(&format!(":{port}")) && truthy(v))
        })
    };
    if funnel(config)
        || config["Foreground"]
            .as_object()
            .is_some_and(|m| m.values().any(funnel))
    {
        return false;
    }
    let handler = &config["TCP"][port.to_string()];
    handler["TCPForward"] == format!("127.0.0.1:{port}")
        && !["TerminateTLS", "HTTPS", "HTTP", "ProxyProtocol"]
            .iter()
            .any(|key| truthy(&handler[*key]))
}

fn truthy(value: &serde_json::Value) -> bool {
    match value {
        serde_json::Value::Null => false,
        serde_json::Value::Bool(v) => *v,
        serde_json::Value::String(v) => !v.is_empty(),
        serde_json::Value::Number(v) => v.as_f64() != Some(0.0),
        serde_json::Value::Array(v) => !v.is_empty(),
        serde_json::Value::Object(v) => !v.is_empty(),
    }
}

struct Output {
    code: i32,
    stdout: String,
    stderr: String,
}

async fn capture(command: &[OsString], input: Option<&str>) -> Output {
    let mut process = tokio::process::Command::new(&command[0]);
    process
        .args(&command[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .kill_on_drop(true);
    let result = async {
        let mut child = process.spawn()?;
        let stdin = child.stdin.take();
        let feed = async {
            if let Some(mut stdin) = stdin {
                let _ = stdin
                    .write_all(&super::super::super::text_bytes(input.unwrap_or("")))
                    .await;
            }
        };
        let (_, output) = tokio::join!(feed, child.wait_with_output());
        output
    };
    match tokio::time::timeout(Duration::from_secs(60), result).await {
        Ok(Ok(output)) => Output {
            code: resolution::exit_code(output.status),
            stdout: text(&output.stdout),
            stderr: text(&output.stderr),
        },
        other => Output {
            code: 127,
            stdout: String::new(),
            stderr: match other {
                Err(_) => "TimeoutExpired",
                Ok(Err(error)) if error.kind() == std::io::ErrorKind::NotFound => {
                    "FileNotFoundError"
                }
                Ok(Err(error)) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                    "PermissionError"
                }
                _ => "OSError",
            }
            .into(),
        },
    }
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .replace("\r\n", "\n")
        .replace('\r', "\n")
}

async fn psql(sql: &str) -> Output {
    let mut command: Vec<OsString> = ["docker", "exec", "-i", POSTGRES, "sh", "-c", PSQL]
        .into_iter()
        .map(Into::into)
        .collect();
    command[0] = docker();
    capture(&command, Some(&format!("{sql}\n"))).await
}

fn forward(args: &Args, url: Option<String>) -> Vec<OsString> {
    let mut out: Vec<String> = Vec::new();
    if args.list {
        out.push("--list".into());
    } else if let Some(name) = &args.revoke {
        out.extend(["--revoke".into(), name.clone(), "--yes".into()]);
    } else {
        out.push(args.name.clone().unwrap_or_default());
        if let Some(tier) = &args.tier {
            out.extend(["--tier".into(), tier.clone()]);
        }
        if let Some(board) = args.board {
            out.push(if board { "--board" } else { "--no-board" }.into());
        }
        out.extend([
            "--expires".into(),
            format!("{}s", args.expires.unwrap_or(900)),
        ]);
        // v59: whose machine, in `_forward`'s place, so the container's
        // invite records it (never dropped: an omission would make someone
        // else's machine the owner's).
        if let Some(owned) = args.owned {
            out.push(if owned { "--mine" } else { "--someone-else" }.into());
        }
        if args.replace {
            out.push("--replace".into());
        }
        out.extend([
            "--url".into(),
            url.unwrap_or_else(|| URL_PLACEHOLDER.into()),
        ]);
    }
    if let Some(bank) = &args.bank
        && !bank.is_empty()
    {
        out.extend(["--bank".into(), bank.clone()]);
    }
    if args.json {
        out.push("--json".into());
    }
    out.into_iter().map(Into::into).collect()
}

pub(super) async fn run(args: &Args, report: &mut Report, port: u32) -> Result<u8, Defer> {
    if std::env::var_os("PSEUDOLIFE_DAEMON_EXEC").is_some_and(|v| !v.is_empty()) {
        return Ok(report.fail(
            EXIT_REFUSED,
            "no database found inside the daemon container: set PSEUDOLIFE_MCP_DATABASE_URL",
        ));
    }
    let docker = docker();
    let running = resolution::inspect(&resolution::container_inspect(&docker)).await;
    if !running.is_some_and(|o| o.status.success() && text(&o.stdout).trim() == "true") {
        if args.list || args.revoke.is_some() {
            return through_postgres(args, report).await;
        }
        return Ok(report.fail(EXIT_REFUSED, "the pseudolife-mcp-daemon container is not running: start the daemon (an invite needs it to report its bank), then re-run"));
    }
    let rust = resolution::inspect(&resolution::container_engine_inspect(&docker))
        .await
        .is_some_and(|o| o.status.success() && text(&o.stdout).trim() == "rust");
    let mut check = resolution::container_exec_for_engine(
        &docker,
        false,
        "invite",
        &["--version-check".into()],
        rust,
    );
    check.remove(2); // A version probe has no stdin or TTY.
    let checked = capture(&check, None).await;
    // `_image_version`: the highest N over lines that are exactly
    // `pseudolife-mcp invite <N>`; 2 (v59) takes --someone-else and --mine.
    let version = (checked.code == 0)
        .then(|| image_version(&checked.stdout))
        .flatten();
    if version.is_none() {
        let message = if checked.code == 127 || checked.stderr.contains("No such container") {
            "no database here (PSEUDOLIFE_MCP_DATABASE_URL is unset and no lite bank was found) and no pseudolife-mcp-daemon container: run this on the daemon's host"
        } else {
            "the daemon's image predates `invite`: update the daemon first (pseudolife-mcp update), then re-run"
        };
        return Ok(report.fail(EXIT_REFUSED, message));
    }
    if args.owned.is_some() && version.is_some_and(|v| v < 2) {
        return Ok(report.fail(EXIT_REFUSED, "the daemon's image predates --someone-else and --mine: update the daemon to schema 59 (pseudolife-mcp update), then re-run. Nothing was changed"));
    }
    let url = match args.url.clone().filter(|u| !u.is_empty()) {
        Some(url) => Some(url),
        None if args.name.is_some() => exposed_url(port).await,
        None => None,
    };
    let command =
        resolution::container_exec_for_engine(&docker, false, "invite", &forward(args, url), rust);
    let output = capture(&command, None).await;
    let _ = std::io::stdout().write_all(&super::super::super::text_bytes(&output.stdout));
    let _ = std::io::stdout().flush();
    let _ = std::io::stderr().write_all(&super::super::super::text_bytes(&output.stderr));
    let _ = std::io::stderr().flush();
    Ok(match output.code {
        0 | 1 | 2 | 4 => output.code as u8,
        _ => EXIT_FAILED,
    })
}

fn revoke_sql(name: &str) -> String {
    // The caller has checked the principal-name grammar; no quote can occur.
    format!(
        "UPDATE public.principals SET revoked_at = COALESCE(revoked_at, EXTRACT(EPOCH FROM clock_timestamp())::double precision), code_hash = NULL, code_expires_at = NULL, paired_code_hash = NULL WHERE principal = '{name}' RETURNING principal;"
    )
}

fn by_hand(report: &mut Report, why: &str, sql: &str) -> u8 {
    report.fail(EXIT_FAILED, &format!("{why}. With the Postgres container running, the same change by hand is: docker exec -i {POSTGRES} psql -U pseudolife -d pseudolife_memory -c \"{sql}\""))
}

async fn through_postgres(args: &Args, report: &mut Report) -> Result<u8, Defer> {
    let mut sql = if args.list {
        LIST.into()
    } else {
        revoke_sql(args.revoke.as_deref().unwrap_or(""))
    };
    if let Some(expected) = args.bank.as_ref().filter(|v| !v.is_empty()) {
        let output = psql(include_str!("bank_sql.sql")).await;
        if output.code != 0 {
            return Ok(by_hand(
                report,
                &format!("psql in {POSTGRES} failed (exit {})", output.code),
                &sql,
            ));
        }
        let value = output.stdout.trim();
        let bank = if value.is_empty() {
            None
        } else {
            Some(super::super::sha256_hex(value)[..16].to_owned())
        };
        if let Some(refusal) = bank_refusal(bank.as_deref(), &J::Str(expected.clone()), "--bank") {
            return Ok(report.fail_units(EXIT_REFUSED, refusal));
        }
    }
    if args.list {
        let probe = psql(include_str!("activity_sql.sql")).await;
        if probe.code != 0 {
            return Ok(by_hand(
                report,
                &format!("psql in {POSTGRES} failed (exit {})", probe.code),
                &sql,
            ));
        }
        if probe.stdout.trim().lines().last() != Some("t") {
            sql = OLD_LIST.into();
        }
    }
    let output = psql(&sql).await;
    if output.code != 0 {
        return Ok(by_hand(
            report,
            &format!("psql in {POSTGRES} failed (exit {})", output.code),
            &sql,
        ));
    }
    if args.list {
        let listed = output
            .stdout
            .trim()
            .lines()
            .last()
            .and_then(|s| decode_list(s).ok());
        let Some((rows, rejected)) = listed else {
            return Ok(by_hand(
                report,
                "psql answered something that is not the list",
                &sql,
            ));
        };
        return list_report(report, &rows, &rejected);
    }
    let name = args.revoke.as_deref().unwrap_or("");
    if !output.stdout.trim().lines().any(|line| line == name) {
        return Ok(report.fail(
            EXIT_REFUSED,
            &format!("no stored principal is named {name}"),
        ));
    }
    report.set("principal", J::Str(name.into()));
    report.set("state", J::Str("revoked".into()));
    report.say(&format!(
        "{name}: revoked; the daemon stops accepting its token within 10 seconds of starting again"
    ));
    Ok(report.finish(EXIT_OK))
}

/// `_image_version`: the highest `N` over the lines of `invite
/// --version-check` that are exactly `pseudolife-mcp invite <N>`. ASCII
/// digits only, where Python's `\d` also takes other decimal digits: the
/// probe reads our own image's answer, which prints ASCII.
fn image_version(stdout: &str) -> Option<u64> {
    stdout
        .lines()
        .filter_map(|line| line.strip_prefix("pseudolife-mcp invite "))
        .filter(|rest| !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()))
        .filter_map(|digits| digits.parse::<u64>().ok())
        .max()
}

/// `describe_rows(..., not_owner=None)` over psql's JSON: a row is 12
/// columns, or 13 with v59's `owned` (null for an environment row). The
/// stopped daemon's `coordination.not_owner_principals` is in its volume,
/// not this host's config, so an environment row's owner is unknown.
fn decode_list(text: &str) -> Result<(Vec<invite_db::Listed>, Vec<invite_db::Rejected>), Defer> {
    let value: serde_json::Value = serde_json::from_str(text).map_err(|_| Defer)?;
    let mut rows = Vec::new();
    for row in value["principals"].as_array().ok_or(Defer)? {
        let row = row
            .as_array()
            .filter(|r| r.len() == 12 || r.len() == 13)
            .ok_or(Defer)?;
        let time = |i: usize| -> Result<Option<f64>, Defer> {
            if row[i].is_null() {
                Ok(None)
            } else {
                row[i].as_f64().map(Some).ok_or(Defer)
            }
        };
        let stored = row[11].as_bool().ok_or(Defer)?;
        let paired = stored && row[3].as_bool().ok_or(Defer)?;
        let pending = stored && row[4].as_bool().ok_or(Defer)?;
        let expires = time(5)?;
        let revoked = time(8)?;
        let state = if !stored {
            "environment"
        } else if revoked.is_some() {
            "revoked"
        } else if pending && expires.is_some_and(|e| e > row[9].as_f64().unwrap_or(f64::INFINITY)) {
            "pending"
        } else if pending {
            "expired"
        } else if paired {
            "paired"
        } else {
            "unpaired"
        };
        rows.push(invite_db::Listed {
            principal: row[0].as_str().ok_or(Defer)?.into(),
            state,
            paired,
            pending,
            tier: if stored {
                row[1].as_str().map(Into::into)
            } else {
                None
            },
            board: if stored {
                Some(row[2].as_bool().ok_or(Defer)?)
            } else {
                None
            },
            code_expires_at: if stored { expires } else { None },
            created_at: if stored { time(6)? } else { None },
            paired_at: if stored { time(7)? } else { None },
            revoked_at: if stored { revoked } else { None },
            last_seen_at: time(10)?,
            owner: stored.then(|| {
                row.get(12)
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(true)
            }),
        });
    }
    rows.sort_by(|a, b| a.principal.cmp(&b.principal));
    let mut rejected = Vec::new();
    for row in value["rejected"].as_array().ok_or(Defer)? {
        let row = row.as_array().filter(|r| r.len() == 4).ok_or(Defer)?;
        rejected.push(invite_db::Rejected {
            hash_prefix: row[0].as_str().ok_or(Defer)?.into(),
            rejections: row[1].as_i64().ok_or(Defer)?,
            first_seen_at: row[2].as_f64().ok_or(Defer)?,
            last_seen_at: row[3].as_f64().ok_or(Defer)?,
        });
    }
    Ok((rows, rejected))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stopped_daemon_rows_keep_state_and_environment_semantics() {
        let (rows, rejected) = decode_list(r#"{"principals":[["z",null,null,false,false,null,null,null,null,100,99,false],["a","core",true,false,true,101,50,null,null,100,null,true]],"rejected":[["01234567",3,90,99]]}"#).unwrap();
        assert_eq!(rows[0].principal, "a");
        assert_eq!(rows[0].state, "pending");
        assert_eq!(rows[1].state, "environment");
        assert_eq!(rows[1].board, None);
        assert_eq!(rejected[0].rejections, 3);
        assert!(decode_list(r#"{"principals":[[]],"rejected":[]}"#).is_err());
        // A stored row from before v59 is the owner's; an environment
        // row's owner is the stopped daemon's config, unknown here.
        assert_eq!(rows[0].owner, Some(true));
        assert_eq!(rows[1].owner, None);
    }

    #[test]
    fn the_image_version_is_the_highest_exact_version_line() {
        for (stdout, version) in [
            ("pseudolife-mcp invite 1\npseudolife-mcp invite 2\n", Some(2)),
            ("pseudolife-mcp invite 1\r\npseudolife-mcp invite 2\r\n", Some(2)),
            ("pseudolife-mcp invite 2\npseudolife-mcp invite 1\n", Some(2)),
            ("a banner\npseudolife-mcp invite 3\n", Some(3)),
            ("pseudolife-mcp invite 1\n", Some(1)),
            ("pseudolife-mcp invite 1\npseudolife-mcp invite 2x\n", Some(1)),
            ("pseudolife-mcp invite 1\n pseudolife-mcp invite 2\n", Some(1)),
            ("pseudolife-mcp invite \n", None),
            ("pseudolife-mcp invite 2 extra\n", None),
            ("", None),
        ] {
            assert_eq!(image_version(stdout), version, "{stdout:?}");
        }
    }

    #[test]
    fn the_container_invite_carries_whose_machine_it_is() {
        let base = |owned| Args {
            name: Some("laptop".into()),
            owned,
            ..Args::default()
        };
        let argv = |args: &Args| -> Vec<String> {
            forward(args, Some("https://example.com".into()))
                .into_iter()
                .map(|a| a.to_string_lossy().into_owned())
                .collect()
        };
        let mine = argv(&base(Some(true)));
        let theirs = argv(&base(Some(false)));
        assert!(
            mine.contains(&"--mine".to_owned()) && !mine.contains(&"--someone-else".to_owned())
        );
        assert!(
            theirs.contains(&"--someone-else".to_owned()) && !theirs.contains(&"--mine".to_owned())
        );
        // Python's order: after --expires, before --replace and --url.
        let at = |v: &[String], s: &str| v.iter().position(|a| a == s).unwrap();
        assert!(at(&theirs, "--expires") < at(&theirs, "--someone-else"));
        assert!(at(&theirs, "--someone-else") < at(&theirs, "--url"));
        let neither = argv(&base(None));
        assert!(
            !neither
                .iter()
                .any(|a| a == "--mine" || a == "--someone-else")
        );
    }

    #[test]
    fn stopped_daemon_rows_carry_v59_ownership() {
        let (rows, _) = decode_list(r#"{"principals":[["guest",null,true,true,false,null,1,2,null,100,null,true,false],["env",null,null,false,false,null,null,null,null,100,9,false,null],["mine",null,true,true,false,null,1,2,null,100,null,true,true]],"rejected":[]}"#).unwrap();
        let owner: Vec<(&str, Option<bool>)> = rows
            .iter()
            .map(|r| (r.principal.as_str(), r.owner))
            .collect();
        assert_eq!(
            owner,
            [("env", None), ("guest", Some(false)), ("mine", Some(true))]
        );
    }

    #[test]
    fn exposed_url_requires_our_private_background_forward() {
        use serde_json::json;
        let mut config = json!({"TCP": {"8765": {"TCPForward": "127.0.0.1:8765", "HTTPS": false}}});
        assert!(ours(&config, 8765));
        config["AllowFunnel"] = json!({"example.com:8765": true});
        assert!(!ours(&config, 8765));
        config["AllowFunnel"] = json!({});
        config["Foreground"] = json!({"other": {"AllowFunnel": {"example.com:8765": true}}});
        assert!(!ours(&config, 8765));
        config["Foreground"] = json!({});
        config["TCP"]["8765"]["HTTPS"] = json!(true);
        assert!(!ours(&config, 8765));
    }
}
