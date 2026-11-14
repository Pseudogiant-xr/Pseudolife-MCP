//! `pseudolife-mcp move --dry-run`, natively: check both hosts without
//! changing anything and print the plan.
//!
//! The source is this host's Docker bank (the installer's container names);
//! the target is another host's, reached over key-based ssh, where the
//! native client's hidden `move-target` helper checks its bank with the
//! same code ([`probe`]). The move itself (stop, fenced final backup, copy,
//! restore, start, commit, rollback) is not native yet: without
//! `--dry-run` the command refuses, naming it. Nothing here takes the
//! update lock, creates a move record or writes a file on either host.
mod argv;
pub(super) mod probe;
pub(super) mod remote;
pub(in crate::cli) mod target;
#[cfg(test)]
mod tests;

use std::{
    ffi::OsString,
    io::{self, Write},
    time::Duration,
};

use serde_json::{Value, json};

use self::{
    argv::Request,
    probe::Facts,
    remote::Remote,
    target::{Ask, FRAME, PROTOCOL, Reply},
};
use super::docker::{self, Docker};

/// Printed by the remote script when no native client is installed.
const NO_CLIENT: &str = "PSEUDOLIFE-MOVE-NO-CLIENT";
const NOTHING: &str = "Nothing was changed";
const PROBE_TIME: Duration = Duration::from_secs(300);

/// The remote script: the native client's stable launcher, its user-bin
/// link, then PATH; the first that is executable runs the helper.
fn script() -> String {
    format!(
        r#"for c in "${{XDG_DATA_HOME:-$HOME/.local/share}}/pseudolife-mcp/bin/pseudolife-mcp" "$HOME/.local/bin/pseudolife-mcp" "$(command -v pseudolife-mcp 2>/dev/null)"; do if [ -n "$c" ] && [ -x "$c" ]; then exec "$c" move-target {PROTOCOL}; fi; done; echo {NO_CLIENT}; exit 3"#
    )
}

/// The docker program: `PSEUDOLIFE_DOCKER` as restore reads it, else
/// `docker` on an absolute PATH entry (`program_lookup`), never the current
/// directory or a relative entry.
pub(super) fn docker_program() -> Result<std::path::PathBuf, String> {
    let named = std::env::var_os("PSEUDOLIFE_DOCKER").is_some_and(|v| !v.is_empty());
    if named {
        return docker::program()
            .map_err(|why| why.trim_end_matches(". Nothing was restored").to_owned());
    }
    crate::cli::program_lookup::find_program_path("docker").ok_or_else(|| {
        "Docker was not found: no docker program is on PATH (only absolute PATH entries are searched); install Docker, or set PSEUDOLIFE_DOCKER to the docker program's full path, then retry".to_owned()
    })
}

/// The hosts a dry run reads.
trait World {
    type D: Docker;
    type R: Remote;
    fn docker(&mut self) -> Result<&mut Self::D, String>;
    fn remote(&mut self) -> Result<&mut Self::R, String>;
    /// `GET <url>` of this host's own daemon (plain HTTP on its binding).
    fn local_health(&mut self, url: &str) -> Option<Value>;
    /// `GET <url>` from this host, http or https, no redirects.
    fn reach(&mut self, url: &str) -> Result<Value, String>;
}

/// What a dry run found.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Plan {
    pub to: String,
    pub target_url: String,
    pub source: Facts,
    pub target: Reply,
    /// Whether --target-url answered with the target's own fingerprint. A
    /// fresh target has none, and its URL is matched by release and schema
    /// only.
    pub url_tied: bool,
}

fn mib(bytes: u64) -> String {
    format!("{} MiB", bytes.div_ceil(1024 * 1024))
}

fn sized(bytes: Option<u64>) -> String {
    bytes.map_or_else(|| "size unknown".to_owned(), mib)
}

/// The source's own checks.
fn source_refusal(facts: &Facts) -> Option<String> {
    if !facts.running {
        return Some(format!(
            "the source daemon {} is not running; move moves a running bank, so start it (docker compose start in its install directory), then retry",
            facts.daemon
        ));
    }
    if facts.health_url.is_empty() {
        return Some(format!(
            "the source daemon {} publishes no health port, so move cannot read its bank's identity; publish its port 8765 as the installer's compose does (or run it on the host network), then retry",
            facts.daemon
        ));
    }
    if facts.status != "ok" || facts.ready == Some(false) {
        return Some(format!(
            "the source daemon is not healthy and ready at {}/health (status {:?}, ready {:?}); wait for it or check docker logs {}, then retry",
            facts.health_url, facts.status, facts.ready, facts.daemon
        ));
    }
    let Some(bank) = &facts.bank else {
        return Some(format!(
            "the source daemon reports no bank fingerprint yet; wait until its /health shows one (or check docker logs {}), then retry",
            facts.daemon
        ));
    };
    if facts.schema.is_none_or(|s| s <= 0) {
        return Some(
            "the source daemon reports no schema version; check its /health, then retry".to_owned(),
        );
    }
    if facts.meta_fingerprint.as_deref() != Some(bank.as_str()) {
        return Some(format!(
            "the source daemon's fingerprint does not match the bank in {}; check that {} serves that database, then retry",
            facts.pg, facts.daemon
        ));
    }
    if facts.db_bytes.is_none() || facts.data_kb.is_none() {
        return Some(format!(
            "cannot measure the source bank's size (pg_database_size in {} or du of /data in {}), so the room the move needs on the target is unknown; check docker logs {} and {}, then retry",
            facts.pg, facts.daemon, facts.pg, facts.daemon
        ));
    }
    if facts.pg_version.is_none() {
        return Some(format!(
            "cannot read the source PostgreSQL's version in {}; check docker logs {}, then retry",
            facts.pg, facts.pg
        ));
    }
    None
}

/// The last non-empty line of `bytes`, for a message.
fn last_line(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .lines()
        .rev()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .unwrap_or("nothing")
        .to_owned()
}

/// Check both hosts; the plan, or the first reason a move cannot go ahead.
fn preflight<W: World>(world: &mut W, request: &Request) -> Result<Plan, String> {
    let target_url = request.target_url.clone().unwrap_or_default();
    let docker = world.docker()?;
    let mut source = probe::gather(docker).map_err(|why| format!("the source: {why}"))?;
    if source.running
        && !source.health_url.is_empty()
        && let Some(answer) = world.local_health(&format!("{}/health", source.health_url))
    {
        probe::apply_health(&mut source, &answer);
    }
    if let Some(why) = source_refusal(&source) {
        return Err(why);
    }
    // Both are known: an unmeasured size refuses above. The restore that
    // will use these files keeps 8 GiB free beyond what it stages
    // (`bank::MIN_FREE`), so the target needs that much more here.
    const RESTORE_FLOOR: u64 = 8 << 30;
    let need = source
        .db_bytes
        .unwrap_or_default()
        .saturating_add(source.data_kb.unwrap_or_default().saturating_mul(1024))
        .saturating_add(RESTORE_FLOOR);
    let ask = Ask {
        protocol: PROTOCOL,
        source_schema: source.schema.unwrap_or_default(),
        source_pg_version: source.pg_version.unwrap_or_default(),
        source_bank: source.bank.clone().unwrap_or_default(),
        work_dir: request.target_work_dir.clone().unwrap_or_default(),
        need_bytes: need,
    };
    let body = serde_json::to_vec(&ask).map_err(|e| e.to_string())?;
    let to = request.to.clone();
    let ran = world
        .remote()?
        .run(&to, &remote::sh(&script()), &body, PROBE_TIME);
    // A reply the helper printed is its answer even if ssh then failed (a
    // helper killed after printing): the frame is read first.
    let stdout = String::from_utf8_lossy(&ran.stdout).into_owned();
    let framed = stdout.lines().find_map(|line| line.strip_prefix(FRAME));
    if framed.is_none() && ran.lost() {
        return Err(remote::lost(&to, &ran));
    }
    let Some(framed) = framed else {
        if stdout.lines().any(|line| line.trim() == NO_CLIENT) {
            return Err(format!(
                "{to} has no native pseudolife-mcp client (move looks for its stable launcher, ~/.local/bin and PATH); install it there with the installer (ops/install.sh from a clone of the repository), then retry"
            ));
        }
        return Err(format!(
            "the pseudolife-mcp on {to} has no native move helper (it answered: {}); update it there with pseudolife-mcp update (or install the native client with the installer), then retry",
            // An error goes to stderr; stdout may hold only a login banner.
            last_line(
                if !ran.stderr.iter().all(u8::is_ascii_whitespace) || ran.stdout.is_empty() {
                    &ran.stderr
                } else {
                    &ran.stdout
                },
            )
        ));
    };
    let reply: Reply = serde_json::from_str(framed).map_err(|_| {
        format!("the native move helper on {to} answered something this client cannot read; update both clients (pseudolife-mcp update) so they match, then retry")
    })?;
    if reply.protocol != PROTOCOL {
        return Err(format!(
            "the client on {to} ({}) speaks move protocol {}, and this one ({}) speaks {PROTOCOL}; update the older one (pseudolife-mcp update) so both match, then retry",
            reply.version,
            reply.protocol,
            env!("CARGO_PKG_VERSION")
        ));
    }
    if !reply.refusals.is_empty() {
        return Err(reply.refusals.join("; "));
    }
    let Some(target) = &reply.facts else {
        return Err(format!(
            "the native move helper on {to} sent no facts about its bank; update both clients so they match, then retry"
        ));
    };
    let health = format!("{}/health", argv::base(&target_url));
    let answer = world.reach(&health).map_err(|why| {
        format!(
            "--target-url {target_url} does not answer from this host ({why}); name the URL clients will use to reach the target's daemon, check that it is exposed there, then retry"
        )
    })?;
    let reached = answer.get("bank").and_then(Value::as_str);
    if reached != target.bank.as_deref() {
        return Err(format!(
            "--target-url {target_url} reaches a daemon whose bank ({}) is not the target's ({}); name the URL of {to}'s daemon, then retry",
            reached.unwrap_or("none"),
            target.bank.as_deref().unwrap_or("none")
        ));
    }
    // A fresh target has no fingerprint to match: the daemon reached must
    // at least answer as the target's does (its version and schema).
    if target.bank.is_none() {
        let version = answer
            .get("version")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let schema = answer.get("schema").and_then(Value::as_i64);
        if version != target.version || schema != target.schema {
            return Err(format!(
                "--target-url {target_url} reaches a daemon (version {version:?}, schema {schema:?}) that does not answer as {to}'s does (version {:?}, schema {:?}); name the URL of {to}'s daemon, then retry",
                target.version, target.schema
            ));
        }
    }
    let url_tied = target.bank.is_some();
    Ok(Plan {
        to,
        target_url,
        source,
        target: reply,
        url_tied,
    })
}

/// The plan as text.
fn render(plan: &Plan) -> String {
    let s = &plan.source;
    let t = plan.target.facts.clone().unwrap_or_default();
    let unknown = "unknown".to_owned();
    let mut out = String::new();
    out.push_str("Move plan (dry run): both hosts checked; nothing was changed on either.\n");
    out.push_str(&format!(
        "  source: this host, {} (image {}), bank {}, schema {}, PostgreSQL {}; database {} ({}), /data ({})\n",
        s.daemon,
        s.image,
        s.bank.as_ref().unwrap_or(&unknown),
        s.schema.map_or_else(|| unknown.clone(), |v| v.to_string()),
        s.pg_version.map_or_else(|| unknown.clone(), |v| v.to_string()),
        s.db,
        sized(s.db_bytes),
        sized(s.data_kb.map(|kb| kb.saturating_mul(1024))),
    ));
    out.push_str(&format!(
        "  target: {}, {} (image {}), an empty bank {}, schema {}, PostgreSQL {}; native client {}\n",
        plan.to,
        t.daemon,
        t.image,
        t.bank
            .as_ref()
            .map_or_else(|| "(no identity yet: a fresh install)".to_owned(), Clone::clone),
        t.schema.map_or_else(|| unknown.clone(), |v| v.to_string()),
        t.pg_version.map_or_else(|| unknown.clone(), |v| v.to_string()),
        plan.target.version,
    ));
    if plan.url_tied {
        out.push_str(&format!("  clients will use: {}\n", plan.target_url));
    } else {
        out.push_str(&format!(
            "  clients will use: {} (it answers as the target's daemon does, by release and schema; the target's fresh bank has no identity yet, so this URL is not tied to {}: check it reaches that host)\n",
            plan.target_url, plan.to
        ));
    }
    out.push_str(&format!(
        "  files on the target: under {} ({} free)\n",
        plan.target.work_dir,
        sized(plan.target.free_bytes)
    ));
    out.push_str("The move would, in this order:\n");
    let steps = steps(plan);
    for (n, step) in steps.iter().enumerate() {
        out.push_str(&format!("  {}. {step}\n", n + 1));
    }
    out.push_str("A failure before the commit point rolls back: the target is stopped first, then the source's database fence is lifted and the source started again; the target never runs beside a running source.\n");
    out.push_str("The native client does not perform the move yet; this run only checked it.\n");
    out
}

/// The steps a move would take, as the plan prints them.
fn steps(plan: &Plan) -> Vec<String> {
    let t = plan.target.facts.clone().unwrap_or_default();
    let timer = match plan.target.update_timer.as_str() {
        "enabled" => "pause the target's unattended update (enabled now) and restore it at the end"
            .to_owned(),
        "not-found" => "leave the target's unattended update alone (it has none)".to_owned(),
        other => format!("leave the target's unattended update as it is ({other})"),
    };
    let policy = if t.restart_policy.is_empty() || t.restart_policy == "no" {
        format!(
            "leave the target daemon's restart policy as it is ({:?})",
            t.restart_policy
        )
    } else {
        format!(
            "set the target daemon's restart policy to no for the move (it is {}) and restore it at the end",
            t.restart_policy
        )
    };
    vec![
        timer,
        policy,
        "pause this host's unattended update, if enabled".to_owned(),
        format!("stop the source daemon {} (docker stop -t 120; never removed)", plan.source.daemon),
        format!(
            "take a final backup of {} from the stopped source, then fence that database (ALLOW_CONNECTIONS false)",
            plan.source.db
        ),
        format!(
            "copy the dump, the state archive and a manifest to {} on the target and check their SHA-256",
            plan.target.work_dir
        ),
        "stop the target daemon, recheck its bank, and restore it from those files, left stopped behind /data/move.json".to_owned(),
        "keep the target's own tokens (--no-keep-tokens)".to_owned(),
        "start the target once and wait until its bank fingerprint equals the source's".to_owned(),
        "write /data/moved.json into the stopped source: the commit point, after which the move is not rolled back".to_owned(),
        format!("re-point this machine's clients to {}", plan.target_url),
    ]
}

/// The plan as one JSON document.
fn document(plan: &Plan) -> Value {
    json!({
        "dry_run": true,
        "changed": false,
        "to": plan.to,
        "target_url": plan.target_url,
        "target_url_tied": plan.url_tied,
        "source": plan.source,
        "target": plan.target,
        "steps": steps(plan),
    })
}

/// The real hosts.
struct Real {
    docker: Result<docker::Real, String>,
    ssh: Result<remote::Ssh, String>,
}

impl World for Real {
    type D = docker::Real;
    type R = remote::Ssh;
    fn docker(&mut self) -> Result<&mut docker::Real, String> {
        self.docker.as_mut().map_err(|why| why.clone())
    }
    fn remote(&mut self) -> Result<&mut remote::Ssh, String> {
        self.ssh.as_mut().map_err(|why| why.clone())
    }
    fn local_health(&mut self, url: &str) -> Option<Value> {
        super::http_json(url, Duration::from_secs(5))
    }
    fn reach(&mut self, url: &str) -> Result<Value, String> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        runtime.block_on(async {
            let client = reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(10))
                .build()
                .map_err(|e| e.to_string())?;
            let response = client.get(url).send().await.map_err(|e| e.to_string())?;
            let status = response.status();
            if !status.is_success() {
                return Err(format!("it answered HTTP {}", status.as_u16()));
            }
            response
                .json::<Value>()
                .await
                .map_err(|_| "its answer is not JSON".to_owned())
        })
    }
}

fn say(stdout: bool, text: &str) -> bool {
    let bytes = crate::cli::text_bytes(text);
    if stdout {
        let mut out = io::stdout().lock();
        out.write_all(&bytes).and_then(|()| out.flush()).is_ok()
    } else {
        io::stderr().lock().write_all(&bytes).is_ok()
    }
}

/// `move` for the argv `move_cli` passed on: `None` keeps the deferral for
/// shapes this module does not parse.
pub(in crate::cli) fn run(arguments: &[OsString]) -> Option<u8> {
    let request = argv::parse(arguments)?;
    if let Some(why) = argv::refusal(&request) {
        say(false, &format!("move: {why}\n"));
        return Some(2);
    }
    // ssh first: a missing one is named before Docker is asked anything.
    let ssh = match remote::Ssh::find() {
        Ok(ssh) => ssh,
        Err(why) => return Some(finish(&request, Err(why))),
    };
    let mut world = Real {
        docker: docker_program().map(|program| docker::Real {
            program,
            interruptible: false,
        }),
        ssh: Ok(ssh),
    };
    Some(finish(&request, preflight(&mut world, &request)))
}

/// Print a preflight's outcome; the exit code.
fn finish(request: &Request, outcome: Result<Plan, String>) -> u8 {
    match outcome {
        // Under --json the outcome is one JSON document on stdout either way.
        Err(why) if request.json => {
            let report = json!({
                "dry_run": true,
                "changed": false,
                "error": format!("{why}. {NOTHING}"),
            });
            say(true, &format!("{report}\n"));
            4
        }
        Err(why) => {
            say(false, &format!("move: {why}. {NOTHING}\n"));
            4
        }
        Ok(plan) => {
            let text = if request.json {
                format!("{}\n", document(&plan))
            } else {
                render(&plan)
            };
            if say(true, &text) { 0 } else { 120 }
        }
    }
}
