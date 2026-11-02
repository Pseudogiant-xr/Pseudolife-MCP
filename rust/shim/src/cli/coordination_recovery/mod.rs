//! Offline recovery of restored mailbox credentials. Never starts or migrates a bank.
use super::board_audit::codec;

use crate::{board::state, pg, sent_config};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
};
use tokio_postgres::{GenericClient, Transaction};

const FAILED: &str = "coordination recovery failed; keep coordination disabled and inspect private state before retrying. Credentials were not printed.";
const UNAUDITED: &str = "The restored bank predates the board audit log (schema v42), so this operation is not recorded in it; the log starts empty at the next daemon start.";

#[derive(Default, Debug)]
struct Args {
    action: String,
    config: PathBuf,
    stopped: bool,
    restore: bool,
    agent: Option<String>,
    principal: Option<String>,
    bank_url: Option<String>,
    state: Option<PathBuf>,
}

#[derive(Debug)]
enum Error {
    Safe(String),
    Private,
    Output,
}
impl From<tokio_postgres::Error> for Error {
    fn from(_: tokio_postgres::Error) -> Self {
        Self::Private
    }
}
impl From<pg::Error> for Error {
    fn from(_: pg::Error) -> Self {
        Self::Private
    }
}
type Result<T> = std::result::Result<T, Error>;
fn safe(text: &str) -> Error {
    Error::Safe(text.into())
}

fn parse(values: Vec<OsString>) -> std::result::Result<Args, String> {
    let mut args = Args::default();
    let mut values = values.into_iter();
    while let Some(value) = values.next() {
        let value = value
            .into_string()
            .map_err(|_| "arguments must be Unicode")?;
        let (flag, inline) = value
            .split_once('=')
            .map_or((value.as_str(), None), |(k, v)| (k, Some(v)));
        if matches!(flag, "--confirm-daemon-stopped" | "--confirm-restore") && inline.is_none() {
            if flag == "--confirm-daemon-stopped" {
                args.stopped = true;
            } else {
                args.restore = true;
            }
        } else if matches!(
            flag,
            "--config" | "--agent" | "--principal" | "--bank-url" | "--state"
        ) {
            let value = inline
                .map(str::to_owned)
                .or_else(|| values.next().and_then(|v| v.into_string().ok()))
                .ok_or_else(|| format!("argument {flag}: expected one argument"))?;
            match flag {
                "--config" => args.config = value.into(),
                "--agent" => args.agent = Some(value),
                "--principal" => args.principal = Some(value),
                "--bank-url" => args.bank_url = Some(value),
                _ => args.state = Some(value.into()),
            }
        } else if args.action.is_empty() && matches!(flag, "recover" | "rebind") {
            args.action = value;
        } else {
            return Err(format!("unrecognized arguments: {value}"));
        }
    }
    if args.action.is_empty() || args.config.as_os_str().is_empty() {
        return Err("the following arguments are required: action, --config".into());
    }
    Ok(args)
}

/// Entry point after `coordination-recovery`; always native, including refusals.
pub fn run(values: Vec<OsString>) -> u8 {
    std::thread::spawn(move || {
        if values.iter().any(|v| v == "-h" || v == "--help") {
            use std::io::Write;
            return if std::io::stdout()
                .lock()
                .write_all(&super::text_bytes(
                    &include_str!("help.txt").replace("\r\n", "\n"),
                ))
                .is_ok()
            {
                0
            } else {
                120
            };
        }
        let args = match parse(values) {
            Ok(args) => args,
            Err(message) => {
                crate::stderrln!("pseudolife-mcp coordination-recovery: error: {message}");
                return 2;
            }
        };
        let result = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| Error::Private)
            .and_then(|rt| rt.block_on(perform(args)));
        match result {
            Ok(()) => 0,
            Err(Error::Safe(message)) => {
                crate::stderrln!("coordination recovery: {message}");
                1
            }
            Err(Error::Private) => {
                crate::stderrln!("{FAILED}");
                1
            }
            Err(Error::Output) => 120,
        }
    })
    .join()
    .unwrap_or(1)
}

fn configuration(args: &Args) -> Result<sent_config::SentConfig> {
    if !args.config.is_file() {
        return Err(safe("an existing daemon configuration file is required"));
    }
    let text = fs::read_to_string(&args.config).map_err(|_| Error::Private)?;
    let config = sent_config::parse(&text).map_err(|_| Error::Private)?;
    if config.enabled {
        return Err(safe(
            "coordination must be disabled in the daemon configuration: set coordination.enabled: false",
        ));
    }
    if !args.stopped {
        return Err(safe(
            "stop the daemon and adapters, then pass --confirm-daemon-stopped",
        ));
    }
    Ok(config)
}

async fn connect() -> Result<pg::Session> {
    let value = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")
        .ok()
        .filter(|v| !v.is_empty())
        .ok_or_else(|| {
            safe("PSEUDOLIFE_MCP_DATABASE_URL is required; no database is started automatically")
        })?;
    // Recovery's connect deadline is five seconds, independent of ordinary clients.
    let dsn = pg::Dsn::parse(&value)?;
    tokio::time::timeout(std::time::Duration::from_secs(5), pg::Session::open(&dsn))
        .await
        .map_err(|_| Error::Private)?
        .map_err(Into::into)
}

async fn admitted(config: &sent_config::SentConfig, principal: &str) -> Result<bool> {
    if matches!(principal, "daemon" | "maintainer") {
        return Ok(false);
    }
    if config.allowed.iter().any(|v| v == principal) {
        return Ok(true);
    }
    // Environment principals shadow stored invites, as PrincipalSnapshot does.
    if std::env::var("PSEUDOLIFE_MCP_TOKENS")
        .ok()
        .is_some_and(|v| environment_principals(&v).iter().any(|v| v == principal))
    {
        return Ok(false);
    }
    let session = connect().await?;
    let present = present(session.client(), "principals").await?;
    let answer = if present {
        session
            .client()
            .query_opt(
                "SELECT board AND revoked_at IS NULL FROM public.principals WHERE principal=$1",
                &[&principal],
            )
            .await?
            .map(|r| r.try_get::<_, bool>(0))
            .transpose()?
            .unwrap_or(false)
    } else {
        false
    };
    let _ = session.close().await;
    Ok(answer)
}

fn state_path(path: &Path) -> Result<PathBuf> {
    // Resolve existing ancestors, including a symlinked checkout parent.
    let absolute = std::path::absolute(path).map_err(|_| Error::Private)?;
    let mut parent = absolute.as_path();
    let mut suffix = Vec::new();
    while !parent.exists() {
        suffix.push(parent.file_name().ok_or(Error::Private)?.to_owned());
        parent = parent.parent().ok_or(Error::Private)?;
    }
    let mut resolved = fs::canonicalize(parent).map_err(|_| Error::Private)?;
    for part in suffix.iter().rev() {
        resolved.push(part);
    }
    if resolved
        .ancestors()
        .skip(1)
        .any(|p| p.join(".git").exists())
    {
        return Err(safe("private state must be outside a Git repository"));
    }
    Ok(absolute)
}

fn valid_url(value: &str) -> bool {
    reqwest::Url::parse(value).is_ok_and(|u| {
        matches!(u.scheme(), "http" | "https")
            && u.host_str().is_some()
            && u.username().is_empty()
            && u.password().is_none()
            && u.query().is_none()
            && u.fragment().is_none()
    })
}

async fn perform(args: Args) -> Result<()> {
    let config = configuration(&args)?;
    if args.action == "recover" {
        if !args.restore {
            return Err(safe(
                "restore recovery revokes every instance credential; --confirm-restore is required",
            ));
        }
        if args.agent.as_ref().is_some_and(|v| !v.is_empty())
            || args.principal.as_ref().is_some_and(|v| !v.is_empty())
            || args.bank_url.as_ref().is_some_and(|v| !v.is_empty())
            || args
                .state
                .as_ref()
                .is_some_and(|v| !v.as_os_str().is_empty())
        {
            return Err(safe("rebind parameters cannot be used for recover"));
        }
        let mut session = connect().await?;
        let tx = session.client_mut().transaction().await?;
        let (revoked, audited) = recover(&tx).await?;
        commit(tx).await?;
        line(&format!(
            "Revoked {revoked} mailbox credentials; pending mail retained and wake disabled."
        ))?;
        if !audited {
            line(UNAUDITED)?;
        }
        let _ = session.close().await;
        return Ok(());
    }
    let (Some(agent), Some(principal), Some(url), Some(path)) =
        (&args.agent, &args.principal, &args.bank_url, &args.state)
    else {
        return Err(safe(
            "rebind requires --agent, --principal, --bank-url and --state",
        ));
    };
    if agent.is_empty() || principal.is_empty() || url.is_empty() || path.as_os_str().is_empty() {
        return Err(safe(
            "rebind requires --agent, --principal, --bank-url and --state",
        ));
    }
    if !admitted(&config, principal).await? {
        return Err(safe(
            "the requested owner must be in coordination.allowed_principals, or a principal invited with board access",
        ));
    }
    let path = state_path(path)?;
    if !valid_url(url) {
        return Err(Error::Private);
    }
    if state::exists(&path) {
        return Err(safe(
            "state output must be a new private file; existing files are never replaced",
        ));
    }
    // Keep the reservation, even on failure: no automatic retry may replace it.
    let reserved = state::create(&path).map_err(|_| Error::Private)?;
    let identity = reserved.identity;
    drop(reserved.file);
    let mut session = connect().await?;
    let tx = session.client_mut().transaction().await?;
    let (agent, credential, audited) = rebind(&tx, agent, principal).await?;
    let bytes = serde_json::to_vec(&json!({"bank_url": url.trim_end_matches('/'), "agent_id": agent, "credential": credential})).map_err(|_| Error::Private)?;
    state::atomic_write(&path, &bytes, Some(&identity)).map_err(|_| Error::Private)?;
    // A file-write error rolls back. An unconfirmed COMMIT keeps the private file.
    commit(tx).await?;
    line(
        "Mailbox rebound; private state written. Wake remains disabled until explicit adapter opt-in.",
    )?;
    if !audited {
        line(UNAUDITED)?;
    }
    let _ = session.close().await;
    Ok(())
}

fn line(value: &str) -> Result<()> {
    use std::io::Write;
    let mut stdout = std::io::stdout().lock();
    stdout
        .write_all(&super::text_bytes(&format!("{value}\n")))
        .and_then(|()| stdout.flush())
        .map_err(|_| Error::Output)
}

async fn commit(tx: Transaction<'_>) -> Result<()> {
    // Every preceding statement is awaited with `?`; no failed statement is
    // swallowed and no aborted transaction can reach COMMIT as a success.
    tx.commit()
        .await
        .map_err(|_| safe("database commit was not confirmed; keep coordination disabled"))
}

fn environment_principals(raw: &str) -> Vec<String> {
    let mut tokens = std::collections::BTreeMap::new();
    for entry in raw.split(',') {
        if let Some((token, principal)) = entry.rsplit_once(':') {
            let (token, principal) = (token.trim(), principal.trim().to_lowercase());
            if !token.is_empty()
                && !principal.is_empty()
                && !matches!(principal.as_str(), "default" | "maintainer")
            {
                tokens.entry(token).or_insert(principal);
            }
        }
    }
    tokens.into_values().collect()
}

async fn present(client: &impl GenericClient, name: &str) -> Result<bool> {
    Ok(client
        .query_one("SELECT to_regclass($1) IS NOT NULL", &[&name])
        .await?
        .try_get(0)?)
}
fn clock() -> Result<f64> {
    Ok(std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| Error::Private)?
        .as_secs_f64())
}

async fn recover(tx: &Transaction<'_>) -> Result<(usize, bool)> {
    let mut ids = tx.query("UPDATE coordination_agents SET credential_hash=NULL,wake_enabled=FALSE,attachment_id=NULL,lease_until=NULL,generation=generation+1,lifecycle='revoked' RETURNING agent_id", &[])
        .await?.iter().map(|r| r.try_get::<_, String>(0)).collect::<std::result::Result<Vec<_>,_>>()?;
    ids.sort();
    let mut freed: Vec<String> = Vec::new();
    let mut waiters = 0;
    if present(tx, "coordination_leases").await? {
        freed = tx.query("UPDATE coordination_leases SET holder_agent_id=NULL,holder_principal='',purpose='',acquired_at=NULL,expires_at=NULL,expect=NULL,expected_end=NULL,freed_at=$1 WHERE holder_agent_id IS NOT NULL RETURNING name", &[&clock()?])
            .await?.iter().map(|r| r.try_get(0)).collect::<std::result::Result<_,_>>()?;
        freed.sort();
        waiters = tx
            .execute("DELETE FROM coordination_lease_waiters", &[])
            .await?;
    }
    let audited = present(tx, "coordination_events").await?;
    if audited {
        let mut payload = json!({"agent_ids": ids});
        if !freed.is_empty() || waiters != 0 {
            payload["leases_freed"] = json!(freed);
            payload["waiters_removed"] = json!(waiters);
        }
        append(tx, "recover", &payload, "", "", "").await?;
    }
    Ok((ids.len(), audited))
}

async fn rebind(
    tx: &Transaction<'_>,
    supplied: &str,
    principal: &str,
) -> Result<(String, String, bool)> {
    let agent = if (8..32).contains(&supplied.len())
        && supplied
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        let pattern = format!("{supplied}%");
        let rows = tx.query("SELECT agent_id FROM coordination_agents WHERE agent_id LIKE $1 ORDER BY agent_id LIMIT 9", &[&pattern]).await?;
        let ids: Vec<String> = rows
            .iter()
            .map(|r| r.try_get(0))
            .collect::<std::result::Result<_, _>>()?;
        match ids.as_slice() {
            [id] => id.clone(),
            [] => return Err(Error::Private),
            _ => {
                let cut = 8.max(
                    ids[..ids.len().min(8)]
                        .windows(2)
                        .map(|v| {
                            v[0].bytes()
                                .zip(v[1].bytes())
                                .take_while(|(a, b)| a == b)
                                .count()
                                + 1
                        })
                        .max()
                        .unwrap_or(8),
                );
                let detail = ids
                    .iter()
                    .take(8)
                    .map(|id| &id[..id.len().min(cut)])
                    .collect::<Vec<_>>()
                    .join(", ");
                return Err(safe(&format!(
                    "--agent {supplied} matches several ids: {detail}; give a longer prefix"
                )));
            }
        }
    } else {
        supplied.into()
    };
    let row = tx.query_opt("SELECT principal,credential_hash,project,task FROM coordination_agents WHERE agent_id=$1 FOR UPDATE", &[&agent]).await?.ok_or(Error::Private)?;
    if row.try_get::<_, String>("principal")? != principal
        || row
            .try_get::<_, Option<String>>("credential_hash")?
            .is_some()
    {
        return Err(Error::Private);
    }
    let credential = random_credential()?;
    let hash = format!("{:x}", Sha256::digest(credential.as_bytes()));
    tx.execute("UPDATE coordination_agents SET credential_hash=$1,lifecycle='registered' WHERE agent_id=$2", &[&hash, &agent]).await?;
    let audited = present(tx, "coordination_events").await?;
    if audited {
        append(
            tx,
            "rebind",
            &json!({"principal": principal}),
            &agent,
            &row.try_get::<_, String>("project")?,
            &row.try_get::<_, String>("task")?,
        )
        .await?;
    }
    Ok((agent, credential, audited))
}

fn random_credential() -> Result<String> {
    // secrets.token_urlsafe(32), without introducing another base64 dependency.
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| Error::Private)?;
    let mut output = String::new();
    let mut bits = 0u32;
    let mut count = 0;
    for b in bytes {
        bits = (bits << 8) | u32::from(b);
        count += 8;
        while count >= 6 {
            count -= 6;
            output.push(ALPHABET[((bits >> count) & 63) as usize] as char);
        }
    }
    if count > 0 {
        output.push(ALPHABET[((bits << (6 - count)) & 63) as usize] as char);
    }
    Ok(output)
}

async fn append(
    tx: &Transaction<'_>,
    event: &str,
    payload: &Value,
    agent: &str,
    project: &str,
    task: &str,
) -> Result<()> {
    tx.query_one(
        "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
        &[&"coordination-audit-chain"],
    )
    .await?;
    let (seq, prev) = match tx
        .query_opt(
            "SELECT seq,hash FROM coordination_events ORDER BY seq DESC LIMIT 1",
            &[],
        )
        .await?
    {
        Some(r) => (
            r.try_get::<_, i64>(0)?
                .checked_add(1)
                .ok_or(Error::Private)?,
            r.try_get::<_, String>(1)?,
        ),
        None => (1, "0".repeat(64)),
    };
    let now = clock()?;
    let payload = codec::canonical(payload).map_err(|_| Error::Private)?;
    let row = json!({"seq": seq,"event":event,"actor":"operator","principal":"","agent_id":agent,"recipient_agent_id":null,"project":project,"task":task,"message_id":null,"payload":payload,"hlc":"","prev_hash":prev});
    let hash = codec::hash(&row, now).map_err(|_| Error::Private)?;
    tx.execute("INSERT INTO coordination_events (seq,event,actor,principal,agent_id,recipient_agent_id,project,task,message_id,payload,created_at,hlc,prev_hash,hash) VALUES ($1,$2,'operator','',$3,NULL,$4,$5,NULL,$6,$7,'',$8,$9)", &[&seq,&event,&agent,&project,&task,&payload,&now,&prev,&hash]).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recovery_urls_never_admit_credentials_or_suffix_metadata() {
        for value in ["http://localhost:8765", "https://bank.invalid/base/"] {
            assert!(valid_url(value));
        }
        for value in [
            "file:///tmp/a",
            "https://u:p@bank.invalid",
            "https://bank.invalid?q=1",
            "https://bank.invalid/#x",
        ] {
            assert!(!valid_url(value));
        }
    }
    #[test]
    fn credentials_have_the_oracles_entropy_and_canonical_padding_bits() {
        let a = random_credential().unwrap();
        let b = random_credential().unwrap();
        assert_ne!(a, b);
        assert_eq!(a.len(), 43);
        assert!(
            a.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
        );
        assert!(b"AEIMQUYcgkosw048".contains(a.as_bytes().last().unwrap()));
    }

    #[test]
    fn environment_shadowing_uses_first_token_and_the_last_colon() {
        assert_eq!(
            environment_principals(
                "x:alice,x:bob,token:with:colon:Paired,bad,ignored:default,ignored:maintainer"
            ),
            vec!["paired".to_owned(), "alice".to_owned()]
        );
    }
}
