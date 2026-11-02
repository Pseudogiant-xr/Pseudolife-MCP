//! Offline lossless migration; the daemon supplies the shared YAML plan.

use crate::pg::{Client, Dsn, Session};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

const MARKER: &str = "flat_band_migration_v1";
const FINGERPRINT: &str =
    "SELECT id, md5((to_jsonb(e)-'band')::text) FROM public.entries e ORDER BY id";
const RETRY: &str = "bank migration committed but config finalization failed; keep the daemon stopped and retry migrate-flat --apply; restore bank and config together if needed: ";

fn emit(stdout: bool, text: &str) {
    let text = if cfg!(windows) {
        text.replace("\n", "\r\n")
    } else {
        text.into()
    };
    if stdout {
        let _ = std::io::stdout().lock().write_all(text.as_bytes());
    } else {
        let _ = std::io::stderr().lock().write_all(text.as_bytes());
    }
}

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn quoted(value: &str) -> String {
    if !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
    {
        value.into()
    } else {
        format!("'{}'", value.replace('\'', "'\"'\"'"))
    }
}
fn restore(pair: &Value) -> String {
    format!(
        "use your rehearsed external recovery tooling for this same bank AND config; bank dump {}; state archive {}; no automatic restore is available",
        quoted(pair["dump"].as_str().unwrap_or("<dump>")),
        quoted(pair["state"].as_str().unwrap_or("<archive>"))
    )
}

fn daemon() -> Result<PathBuf, String> {
    let name = if cfg!(windows) {
        "pseudolife-daemon.exe"
    } else {
        "pseudolife-daemon"
    };
    let sibling = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.join(name)));
    if let Some(path) = sibling.filter(|p| p.is_file()) {
        return Ok(path);
    }
    for dir in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let path = dir.join(name);
        if path.is_file() {
            return Ok(path);
        }
    }
    Err("pseudolife-daemon is unavailable; run migrate-flat --apply on the bank host with both native binaries installed; nothing changed".into())
}

fn plan(daemon: &Path, raw: &[u8], entries: i64) -> Result<Value, String> {
    let mut child = Command::new(daemon).args(["migrate-flat-config", &entries.to_string()])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()
        .map_err(|_| "the native config planner could not start; install the matching daemon binary on the bank host; nothing changed")?;
    let mut input = child.stdin.take().expect("piped stdin");
    let bytes = raw.to_vec();
    let writer = std::thread::spawn(move || input.write_all(&bytes));
    let output = child
        .wait_with_output()
        .map_err(|_| "the native config planner did not finish; retry migrate-flat --apply")?;
    writer
        .join()
        .map_err(|_| "config planner input failed")?
        .map_err(|_| "config planner input failed")?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr)
            .trim()
            .trim_start_matches("migrate-flat refused: ")
            .into());
    }
    let result: Value = serde_json::from_slice(&output.stdout).map_err(
        |_| "the native config planner returned invalid JSON; install matching native binaries",
    )?;
    if result["capacity"].as_i64().is_none()
        || result["yaml"].as_str().is_none()
        || result["flat"].as_bool().is_none()
    {
        return Err("the native config planner returned an incomplete plan; install matching native binaries".into());
    }
    Ok(result)
}

fn read_config(path: &Path) -> Result<Vec<u8>, String> {
    let metadata = fs::symlink_metadata(path).map_err(
        |_| "config.yaml must be an existing regular file in --data-dir; nothing changed",
    )?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(
            "config.yaml must be an existing regular file in --data-dir; nothing changed".into(),
        );
    }
    fs::read(path).map_err(|_| {
        "config.yaml could not be read; check --data-dir and retry migrate-flat --apply".into()
    })
}

fn replace_config(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let temporary = path.with_file_name(format!(
        ".migrate-flat.config.{}.part",
        uuid::Uuid::new_v4().simple()
    ));
    let result = (|| {
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, path)?;
        #[cfg(unix)]
        fs::File::open(path.parent().unwrap())?.sync_all()?;
        Ok::<(), std::io::Error>(())
    })();
    let _ = fs::remove_file(&temporary);
    result.map_err(|_| "config replacement failed".into())
}

async fn fingerprint(client: &Client) -> Result<Vec<(i64, String)>, String> {
    let rows = client
        .query(FINGERPRINT, &[])
        .await
        .map_err(|_| "entry verification query failed; nothing committed")?;
    Ok(rows.iter().map(|row| (row.get(0), row.get(1))).collect())
}

async fn finish(
    client: &Client,
    daemon: &Path,
    path: &Path,
    marker: &mut Value,
) -> Result<(), String> {
    let raw = read_config(path)?;
    if hash(&raw) == marker["original_sha256"].as_str().unwrap_or("") {
        let planned = plan(
            daemon,
            &raw,
            marker["capacity"]
                .as_i64()
                .ok_or("migration marker capacity is invalid")?,
        )?;
        let rendered = planned["yaml"].as_str().unwrap().as_bytes();
        if hash(rendered) != marker["target_sha256"].as_str().unwrap_or("")
            || planned["capacity"] != marker["capacity"]
        {
            return Err(format!(
                "migration config no longer matches its committed plan; restore bank and config together: {}",
                restore(&marker["backup"])
            ));
        }
        replace_config(path, rendered)?;
    } else if hash(&raw) != marker["target_sha256"].as_str().unwrap_or("") {
        return Err(format!(
            "config.yaml changed after the migration committed; restore bank and config together: {}",
            restore(&marker["backup"])
        ));
    }
    marker["complete"] = Value::Bool(true);
    client
        .execute(
            "UPDATE public.meta SET value=$1::text::jsonb WHERE key=$2",
            &[&marker.to_string(), &MARKER],
        )
        .await
        .map_err(|_| "migration marker finalization failed")?;
    Ok(())
}

async fn locked(
    client: &Client,
    dsn: &str,
    daemon: &Path,
    data_dir: &Path,
    raw: &[u8],
) -> Result<Value, String> {
    let path = data_dir.join("config.yaml");
    if let Some(row) = client
        .query_opt(
            "SELECT value::text FROM public.meta WHERE key=$1",
            &[&MARKER],
        )
        .await
        .map_err(|_| "migration marker could not be read")?
    {
        let mut marker: Value = serde_json::from_str(&row.get::<_, String>(0))
            .map_err(|_| "migration marker is invalid; restore the backup pair before retrying")?;
        if marker["complete"] == true {
            let planned = plan(
                daemon,
                raw,
                marker["capacity"]
                    .as_i64()
                    .ok_or("migration marker capacity is invalid")?,
            )?;
            if planned["flat"] != true {
                return Err(format!(
                    "this bank was migrated but its config is not the matching flat layout; restore bank and config together: {}",
                    restore(&marker["backup"])
                ));
            }
            marker["status"] = "unchanged".into();
        } else {
            if let Err(reason) = finish(client, daemon, &path, &mut marker).await {
                return Err(format!("{RETRY}{} ({reason})", restore(&marker["backup"])));
            }
            marker["status"] = "resumed".into();
        }
        return Ok(marker);
    }
    client.batch_execute("BEGIN; SET LOCAL lock_timeout='5s'; LOCK TABLE public.entries IN SHARE ROW EXCLUSIVE MODE").await.map_err(|_| "entry lock unavailable; stop all bank writers and retry migrate-flat --apply")?;
    let rows = fingerprint(client).await?;
    let planned = plan(daemon, raw, rows.len() as i64)?;
    let nonflat: i64 = client
        .query_one(
            "SELECT count(*) FROM public.entries WHERE band <> 'flat'",
            &[],
        )
        .await
        .map_err(|_| "entry count query failed")?
        .get(0);
    if nonflat == 0 && planned["flat"] == true {
        client
            .batch_execute("ROLLBACK")
            .await
            .map_err(|_| "database connection failed")?;
        return Ok(
            json!({"status": "unchanged", "entries": rows.len(), "capacity": planned["capacity"]}),
        );
    }
    let backup_dir = data_dir.to_owned();
    let backup_dsn = dsn.to_owned();
    let backup = tokio::task::spawn_blocking(move || {
        super::backup::migration_backup(&backup_dir, &backup_dsn)
    })
    .await
    .map_err(|_| "backup worker did not finish; nothing changed")??;
    if read_config(&path)? != raw {
        return Err(
            "config.yaml changed during backup; nothing changed; retry migrate-flat --apply".into(),
        );
    }
    let mut marker = json!({"version": 1, "complete": false, "entries": rows.len(),
        "capacity": planned["capacity"], "original_sha256": hash(raw),
        "target_sha256": hash(planned["yaml"].as_str().unwrap().as_bytes()), "backup": backup});
    client
        .execute(
            "UPDATE public.entries SET band='flat' WHERE band <> 'flat'",
            &[],
        )
        .await
        .map_err(|_| "band update failed; migration rolled back")?;
    if fingerprint(client).await? != rows {
        return Err(format!(
            "entry verification failed; migration rolled back; restore backup if needed: {}",
            restore(&marker["backup"])
        ));
    }
    client
        .execute(
            "INSERT INTO public.meta (key,value) VALUES ($1,$2::text::jsonb)",
            &[&MARKER, &marker.to_string()],
        )
        .await
        .map_err(|_| "migration marker could not be saved; migration rolled back")?;
    client.batch_execute("INSERT INTO public.meta (key,value) VALUES ('writer_lease_epoch','1'::jsonb) ON CONFLICT (key) DO UPDATE SET value=to_jsonb(COALESCE(CASE WHEN jsonb_typeof(meta.value)='number' THEN (meta.value #>> '{}')::numeric::bigint END,0)+1)").await.map_err(|_| "writer epoch could not be advanced; migration rolled back")?;
    client.batch_execute("COMMIT").await.map_err(|_| format!("migration COMMIT answer was lost; keep the daemon stopped and retry migrate-flat --apply to reconcile; restore bank and config together if needed: {}", restore(&marker["backup"])))?;
    if let Err(reason) = finish(client, daemon, &path, &mut marker).await {
        return Err(format!("{RETRY}{} ({reason})", restore(&marker["backup"])));
    }
    marker["status"] = "migrated".into();
    Ok(marker)
}

async fn migrate(
    dsn_text: &str,
    daemon: &Path,
    data_dir: &Path,
    raw: &[u8],
) -> Result<Value, String> {
    let dsn = Dsn::parse(dsn_text).map_err(|e| e.to_string())?;
    let session = Session::open(&dsn).await.map_err(|e| e.to_string())?;
    let client = session.client();
    let held: bool = client
        .query_one(
            "SELECT pg_try_advisory_lock(hashtextextended('pseudolife-bank-writer',0))",
            &[],
        )
        .await
        .map_err(|_| "bank writer lock query failed")?
        .get(0);
    if !held {
        return Err("the bank writer is running; stop the daemon and retry migrate-flat --apply; nothing changed".into());
    }
    let result = locked(client, dsn_text, daemon, data_dir, raw).await;
    let _ = client.batch_execute("ROLLBACK").await;
    let _ = session.close().await;
    result
}

pub(super) fn run(arguments: Vec<OsString>) -> u8 {
    let mut apply = false;
    let mut external_recovery = false;
    let mut data_dir = None;
    let mut args = arguments.iter();
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--apply") => apply = true,
            Some("--external-recovery-verified") => external_recovery = true,
            Some("--data-dir") => data_dir = args.next().map(PathBuf::from),
            Some("--help" | "-h") => {
                emit(
                    true,
                    "usage: pseudolife-mcp migrate-flat --apply --data-dir PATH --external-recovery-verified\n\nBack up and flatten a stopped bank. Set PSEUDOLIFE_MCP_DATABASE_URL.\nNative execution also requires pseudolife-daemon on the bank host.\n--external-recovery-verified attests that you have rehearsed restoring\nthis exact bank AND config with your own tooling; no automatic restore exists.\n",
                );
                return 0;
            }
            _ => {
                emit(false, "migrate-flat refused: use --apply --data-dir PATH\n");
                return 2;
            }
        }
    }
    let result: Result<Value, String> = (|| {
        if !apply {
            return Err("no changes made; stop the daemon and pass --apply".into());
        }
        let data_dir = data_dir.ok_or("--data-dir is required")?;
        if !external_recovery {
            return Err("a paired restore for an explicit DSN is not available yet; nothing changed; rehearse restoring this exact bank AND config with your own tooling, then pass --external-recovery-verified".into());
        }
        let dsn = std::env::var("PSEUDOLIFE_MCP_DATABASE_URL")
            .ok()
            .filter(|s| !s.is_empty())
            .ok_or("set PSEUDOLIFE_MCP_DATABASE_URL to the stopped bank and retry")?;
        emit(
            false,
            "WARNING: external recovery acknowledged for this bank AND config; use your rehearsed tooling to restore the backup pair; no automatic restore is available.\n",
        );
        let data_dir =
            fs::canonicalize(data_dir).map_err(|_| "--data-dir is unavailable; nothing changed")?;
        #[cfg(windows)]
        let data_dir = {
            let text = data_dir.to_string_lossy();
            PathBuf::from(text.strip_prefix(r"\\?\").unwrap_or(&text))
        };
        let raw = read_config(&data_dir.join("config.yaml"))?;
        let daemon = daemon()?;
        plan(&daemon, &raw, 0)?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| "migration runtime could not start")?;
        runtime.block_on(migrate(&dsn, &daemon, &data_dir, &raw))
    })();
    match result {
        Ok(result) => {
            emit(
                true,
                &format!(
                    "flat migration: {}; entries={}; capacity={}\n",
                    result["status"].as_str().unwrap(),
                    result["entries"],
                    result["capacity"]
                ),
            );
            if result.get("backup").is_some() {
                emit(
                    true,
                    &format!(
                        "bank dump: {}\n",
                        result["backup"]["dump"].as_str().unwrap()
                    ),
                );
                emit(
                    true,
                    &format!(
                        "state archive: {}\n",
                        result["backup"]["state"].as_str().unwrap()
                    ),
                );
                emit(
                    true,
                    &format!(
                        "restore bank and config together: {}\n",
                        restore(&result["backup"])
                    ),
                );
            }
            0
        }
        Err(reason) => {
            emit(false, &format!("migrate-flat refused: {reason}\n"));
            1
        }
    }
}
