//! When each principal last authenticated, and which bearers were refused
//! (schema v58): `pseudolife_memory/principal_activity.py`.
//!
//! The bearer gate calls [`Recorder::note`] once per request with what it
//! resolved: a success for an environment or stored principal updates that
//! name's last-seen time, and a refused bearer counts against the first
//! [`HASH_PREFIX_LEN`] hex characters of its SHA-256. Neither touches the
//! database: the recorder keeps them under a short lock, and the principals
//! refresher (`principals::Refresher`, its own task and connection) writes
//! the batch at most once every [`FLUSH_INTERVAL_S`]. A token is never
//! stored, logged or kept past the hash. Open daemons record nothing.

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use tokio_postgres::Client;

pub use crate::auth::presented_bearer;
use crate::auth::{PrincipalSource, Resolved};

// Design bounds (2026-10-30), not measurements; principal_activity.py.
pub const FLUSH_INTERVAL_S: f64 = 60.0;
pub const HASH_PREFIX_LEN: usize = 8;
pub const MAX_PENDING_REJECTED: usize = 1000;
pub const MAX_REJECTED_ROWS: i64 = 1000;
pub const REJECTED_RETENTION_S: f64 = 90.0 * 86400.0;
pub const LOG_PREFIXES: usize = 5;

fn prefix_of(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))[..HASH_PREFIX_LEN].to_string()
}

/// `bearer_hash_prefix`: the header text is latin-1 decoded, so its latin-1
/// encoding is the bytes the client sent (UTF-8 for text no header makes).
pub fn bearer_hash_prefix(token: &str) -> String {
    let latin1: Option<Vec<u8>> = token.chars().map(|c| u8::try_from(c as u32).ok()).collect();
    match latin1 {
        Some(bytes) => prefix_of(&bytes),
        None => prefix_of(token.as_bytes()),
    }
}

/// `token_hash_prefix`: a client's own token text, hashed as UTF-8.
#[cfg_attr(not(test), allow(dead_code))]
pub fn token_hash_prefix(token: &str) -> String {
    prefix_of(token.as_bytes())
}

/// One write (`Batch`): the latest success per principal, and per refused
/// prefix `(count, first_seen, last_seen)`; `dropped` counts refusals past
/// the prefix cap.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Batch {
    pub seen: BTreeMap<String, f64>,
    pub rejected: BTreeMap<String, (i64, f64, f64)>,
    pub dropped: i64,
}

impl Batch {
    fn has_pending(&self) -> bool {
        !self.seen.is_empty() || !self.rejected.is_empty() || self.dropped != 0
    }
}

struct State {
    pending: Batch,
    last_write: Option<f64>,
}

type Clock = Box<dyn Fn() -> f64 + Send + Sync>;

/// `ActivityRecorder`: thread-safe; `seen` and `rejected` are a map write
/// under a short lock. Due when something is pending and the last write
/// began at least the interval ago (or never).
pub struct Recorder {
    state: Mutex<State>,
    wall: Clock,
    clock: Clock,
    interval: f64,
    max_rejected: usize,
}

fn wall_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

impl Recorder {
    pub fn new() -> Self {
        let started = Instant::now();
        Self::with_clocks(
            Box::new(wall_now),
            Box::new(move || started.elapsed().as_secs_f64()),
            MAX_PENDING_REJECTED,
        )
    }

    pub fn with_clocks(wall: Clock, clock: Clock, max_rejected: usize) -> Self {
        Recorder {
            state: Mutex::new(State {
                pending: Batch::default(),
                last_write: None,
            }),
            wall,
            clock,
            interval: FLUSH_INTERVAL_S,
            max_rejected,
        }
    }

    pub fn seen(&self, principal: &str) {
        let now = (self.wall)();
        let mut state = self.state.lock().unwrap();
        state.pending.seen.insert(principal.to_string(), now);
    }

    pub fn rejected(&self, prefix: &str) {
        let now = (self.wall)();
        let mut state = self.state.lock().unwrap();
        let max = self.max_rejected;
        let pending = &mut state.pending;
        if let Some(entry) = pending.rejected.get_mut(prefix) {
            *entry = (entry.0 + 1, entry.1, now);
        } else if pending.rejected.len() < max {
            pending.rejected.insert(prefix.to_string(), (1, now, now));
        } else {
            pending.dropped += 1;
        }
    }

    /// `note`: the gate's one call per request, after a resolution that was
    /// not `Unavailable`. Environment and stored successes are seen; an
    /// unauthorized `Bearer` is refused by prefix; open mode and a request
    /// with no bearer record nothing.
    pub fn note_outcome(
        &self,
        header: Option<&str>,
        principal: Option<&str>,
        source: Option<PrincipalSource>,
    ) {
        if let Some(name) = principal {
            if matches!(
                source,
                Some(PrincipalSource::Environment | PrincipalSource::Store)
            ) {
                self.seen(name);
            }
            return;
        }
        if let Some(token) = presented_bearer(header) {
            self.rejected(&bearer_hash_prefix(token));
        }
    }

    /// The gate's form of [`Self::note_outcome`]: `Unavailable` notes nothing.
    pub fn note(&self, header: Option<&str>, resolved: &Resolved, source: Option<PrincipalSource>) {
        match resolved {
            Resolved::Unavailable => {}
            Resolved::Principal(name) => self.note_outcome(header, Some(name), source),
            Resolved::None => self.note_outcome(header, None, source),
        }
    }

    pub fn due(&self) -> bool {
        let state = self.state.lock().unwrap();
        state.pending.has_pending()
            && state
                .last_write
                .is_none_or(|last| (self.clock)() - last >= self.interval)
    }

    /// The pending batch, now empty here, or `None`; starts the interval.
    pub fn take(&self) -> Option<Batch> {
        let mut state = self.state.lock().unwrap();
        if !state.pending.has_pending() {
            return None;
        }
        state.last_write = Some((self.clock)());
        Some(std::mem::take(&mut state.pending))
    }

    /// A batch whose write failed, merged with what arrived meanwhile.
    pub fn give_back(&self, batch: Batch) {
        let mut state = self.state.lock().unwrap();
        let pending = &mut state.pending;
        for (name, at) in batch.seen {
            let slot = pending.seen.entry(name).or_insert(at);
            if *slot <= at {
                *slot = at;
            }
        }
        for (prefix, (count, first, last)) in batch.rejected {
            match pending.rejected.get_mut(&prefix) {
                None => {
                    pending.rejected.insert(prefix, (count, first, last));
                }
                Some(newer) => {
                    *newer = (count + newer.0, first.min(newer.1), last.max(newer.2));
                }
            }
        }
        pending.dropped += batch.dropped;
    }

    /// `flush`: write the pending batch when due, in one transaction. `Ok(false)`
    /// when nothing was due; a failed write gives the batch back.
    pub async fn flush(&self, client: &mut Client) -> Result<bool, tokio_postgres::Error> {
        if !self.due() {
            return Ok(false);
        }
        let Some(batch) = self.take() else {
            return Ok(false);
        };
        if let Err(e) = write_batch(client, &batch, (self.wall)()).await {
            self.give_back(batch);
            return Err(e);
        }
        if let Some(line) = refusal_log_line(&batch) {
            eprintln!("{line}");
        }
        Ok(true)
    }
}

/// `refusal_log_line`: one line per write naming the refused prefixes, most
/// refused first, or `None` when nothing was refused.
pub fn refusal_log_line(batch: &Batch) -> Option<String> {
    if batch.rejected.is_empty() && batch.dropped == 0 {
        return None;
    }
    let mut ranked: Vec<(&String, &(i64, f64, f64))> = batch.rejected.iter().collect();
    ranked.sort_by(|a, b| b.1.0.cmp(&a.1.0).then_with(|| a.0.cmp(b.0)));
    let total: i64 = ranked.iter().map(|(_, r)| r.0).sum::<i64>() + batch.dropped;
    let named = ranked
        .iter()
        .take(LOG_PREFIXES)
        .map(|(p, r)| format!("{p} x{}", r.0))
        .collect::<Vec<_>>()
        .join(", ");
    let mut tail = String::new();
    if ranked.len() > LOG_PREFIXES {
        tail.push_str(&format!(
            " and {} more prefix(es)",
            ranked.len() - LOG_PREFIXES
        ));
    }
    if batch.dropped != 0 {
        tail.push_str(&format!(
            ", plus {} past the {MAX_PENDING_REJECTED}-prefix cap",
            batch.dropped
        ));
    }
    let named = if named.is_empty() {
        "(none recorded)".to_string()
    } else {
        named
    };
    Some(format!(
        "refused {total} bearer token(s) since the last report, by SHA-256 prefix: \
         {named}{tail}. `pseudolife-mcp doctor` on a client prints its own token's prefix; \
         `pseudolife-mcp invite --list` shows them all"
    ))
}

const UPSERT_SEEN_SQL: &str = "INSERT INTO public.principal_activity (principal, last_seen_at) \
     SELECT * FROM unnest($1::text[], $2::double precision[]) \
     ON CONFLICT (principal) DO UPDATE \
     SET last_seen_at = GREATEST(principal_activity.last_seen_at, EXCLUDED.last_seen_at)";
const UPSERT_REJECTED_SQL: &str = "INSERT INTO public.rejected_bearers \
     (hash_prefix, rejections, first_seen_at, last_seen_at) \
     SELECT * FROM unnest($1::text[], $2::bigint[], $3::double precision[], \
     $4::double precision[]) \
     ON CONFLICT (hash_prefix) DO UPDATE \
     SET rejections = rejected_bearers.rejections + EXCLUDED.rejections, \
     first_seen_at = LEAST(rejected_bearers.first_seen_at, EXCLUDED.first_seen_at), \
     last_seen_at = GREATEST(rejected_bearers.last_seen_at, EXCLUDED.last_seen_at)";
const PRUNE_REJECTED_SQL: &str = "DELETE FROM public.rejected_bearers \
     WHERE last_seen_at < $1 \
     OR hash_prefix IN (SELECT hash_prefix FROM public.rejected_bearers \
     ORDER BY last_seen_at DESC, hash_prefix OFFSET $2)";

/// `write_batch`: the upserts (and, with refusals, the prune) in one
/// transaction; times only move forward, counts add.
pub async fn write_batch(
    client: &mut Client,
    batch: &Batch,
    now: f64,
) -> Result<(), tokio_postgres::Error> {
    let tx = client.transaction().await?;
    if !batch.seen.is_empty() {
        let names: Vec<String> = batch.seen.keys().cloned().collect();
        let times: Vec<f64> = batch.seen.values().copied().collect();
        tx.execute(UPSERT_SEEN_SQL, &[&names, &times]).await?;
    }
    if !batch.rejected.is_empty() {
        let prefixes: Vec<String> = batch.rejected.keys().cloned().collect();
        let counts: Vec<i64> = batch.rejected.values().map(|r| r.0).collect();
        let firsts: Vec<f64> = batch.rejected.values().map(|r| r.1).collect();
        let lasts: Vec<f64> = batch.rejected.values().map(|r| r.2).collect();
        tx.execute(UPSERT_REJECTED_SQL, &[&prefixes, &counts, &firsts, &lasts])
            .await?;
        tx.execute(
            PRUNE_REJECTED_SQL,
            &[&(now - REJECTED_RETENTION_S), &MAX_REJECTED_ROWS],
        )
        .await?;
    }
    tx.commit().await
}

#[cfg(test)]
mod tests {
    //! Golden sequences from the Python recorder
    //! (`harness/gen_principal_activity_goldens.py`).
    use super::*;
    use serde_json::Value;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicU64, Ordering};

    const GOLDEN: &str = include_str!("../harness/goldens/principal-activity.json");

    fn batch_json(batch: &Option<Batch>) -> Value {
        match batch {
            None => Value::Null,
            Some(b) => serde_json::json!({
                "seen": b.seen,
                "rejected": b.rejected.iter()
                    .map(|(k, v)| (k.clone(), serde_json::json!([v.0, v.1, v.2])))
                    .collect::<serde_json::Map<_, _>>(),
                "dropped": b.dropped,
            }),
        }
    }

    fn source(name: &Value) -> Option<PrincipalSource> {
        match name.as_str() {
            Some("store") => Some(PrincipalSource::Store),
            Some("environment") => Some(PrincipalSource::Environment),
            Some("open") => Some(PrincipalSource::Open),
            _ => None,
        }
    }

    #[test]
    fn constants_match_python() {
        let g: Value = serde_json::from_str(GOLDEN).unwrap();
        let c = &g["constants"];
        assert_eq!(c["flush_interval_s"].as_f64(), Some(FLUSH_INTERVAL_S));
        assert_eq!(c["hash_prefix_len"].as_u64(), Some(HASH_PREFIX_LEN as u64));
        assert_eq!(
            c["max_pending_rejected"].as_u64(),
            Some(MAX_PENDING_REJECTED as u64)
        );
        assert_eq!(c["max_rejected_rows"].as_i64(), Some(MAX_REJECTED_ROWS));
        assert_eq!(
            c["rejected_retention_s"].as_f64(),
            Some(REJECTED_RETENTION_S)
        );
        assert_eq!(c["log_prefixes"].as_u64(), Some(LOG_PREFIXES as u64));
    }

    #[test]
    fn prefixes_match_python() {
        let g: Value = serde_json::from_str(GOLDEN).unwrap();
        for case in g["prefixes"].as_array().unwrap() {
            let header = case["header"].as_str();
            let presented = presented_bearer(header);
            assert_eq!(presented, case["presented"].as_str(), "{case}");
            assert_eq!(
                presented.map(bearer_hash_prefix).as_deref(),
                case["prefix"].as_str(),
                "{case}"
            );
        }
        for case in g["tokens"].as_array().unwrap() {
            assert_eq!(
                token_hash_prefix(case["token"].as_str().unwrap()),
                case["prefix"].as_str().unwrap()
            );
        }
    }

    #[test]
    fn sequences_match_python() {
        let g: Value = serde_json::from_str(GOLDEN).unwrap();
        for seq in g["sequences"].as_array().unwrap() {
            let name = seq["name"].as_str().unwrap();
            let wall = Arc::new(AtomicU64::new(
                g["start"]["wall"].as_f64().unwrap().to_bits(),
            ));
            let mono = Arc::new(AtomicU64::new(
                g["start"]["mono"].as_f64().unwrap().to_bits(),
            ));
            let (w, m) = (wall.clone(), mono.clone());
            let rec = Recorder::with_clocks(
                Box::new(move || f64::from_bits(w.load(Ordering::SeqCst))),
                Box::new(move || f64::from_bits(m.load(Ordering::SeqCst))),
                seq["max_rejected"].as_u64().unwrap() as usize,
            );
            let mut last: Option<Batch> = None;
            let steps = seq["steps"].as_array().unwrap();
            let expect = seq["expect"].as_array().unwrap();
            assert_eq!(steps.len(), expect.len(), "{name}");
            for (i, (step, want)) in steps.iter().zip(expect).enumerate() {
                let step = step.as_array().unwrap();
                let got = match step[0].as_str().unwrap() {
                    "tick" => {
                        let s = step[1].as_f64().unwrap();
                        for clock in [&wall, &mono] {
                            let now = f64::from_bits(clock.load(Ordering::SeqCst));
                            clock.store((now + s).to_bits(), Ordering::SeqCst);
                        }
                        Value::Null
                    }
                    "seen" => {
                        rec.seen(step[1].as_str().unwrap());
                        Value::Null
                    }
                    "rejected" => {
                        rec.rejected(step[1].as_str().unwrap());
                        Value::Null
                    }
                    "note" => {
                        rec.note_outcome(step[1].as_str(), step[2].as_str(), source(&step[3]));
                        Value::Null
                    }
                    "due" => Value::Bool(rec.due()),
                    "take" => {
                        let batch = rec.take();
                        if batch.is_some() {
                            last = batch.clone();
                        }
                        batch_json(&batch)
                    }
                    "give_back" => {
                        rec.give_back(last.clone().unwrap());
                        Value::Null
                    }
                    "log" => refusal_log_line(last.as_ref().unwrap())
                        .map(Value::String)
                        .unwrap_or(Value::Null),
                    other => panic!("unknown op {other}"),
                };
                assert_eq!(&got, want, "{name} step {i}: {step:?}");
            }
        }
    }

    #[test]
    fn the_gate_form_notes_nothing_for_unavailable() {
        let rec = Recorder::new();
        rec.note(Some("Bearer x"), &Resolved::Unavailable, None);
        assert!(rec.take().is_none());
        rec.note(Some("Bearer x"), &Resolved::None, None);
        assert_eq!(
            rec.take().unwrap().rejected.keys().collect::<Vec<_>>(),
            vec![&bearer_hash_prefix("x")]
        );
    }

    fn test_dsn() -> Option<String> {
        std::env::var("PL_W1A_TEST_DSN")
            .ok()
            .filter(|d| !d.is_empty())
    }

    /// Against a bank at v58 (PL_W1A_TEST_DSN): upserts move times forward
    /// and add counts, and the prune keeps the newest rows within retention.
    #[tokio::test]
    async fn db_write_batch_upserts_and_prunes() {
        let Some(dsn) = test_dsn() else {
            eprintln!("PL_W1A_TEST_DSN unset: skipping database test");
            return;
        };
        let (mut c, t) = crate::storage::connect(
            &crate::storage::parse_dsn(&dsn).unwrap(),
            std::time::Duration::from_secs(10),
        )
        .await
        .unwrap();
        let has = c
            .query_opt("SELECT to_regclass('public.rejected_bearers')::text", &[])
            .await
            .unwrap()
            .and_then(|r| r.get::<_, Option<String>>(0))
            .is_some();
        if !has {
            eprintln!("rejected_bearers absent (bank below v58): skipping");
            return;
        }
        c.batch_execute(
            "DELETE FROM principal_activity WHERE principal LIKE 'w58-%'; \
             DELETE FROM rejected_bearers WHERE hash_prefix LIKE 'w58%'",
        )
        .await
        .unwrap();
        let mut first = Batch::default();
        first.seen.insert("w58-laptop".into(), 100.0);
        first.rejected.insert("w58aaaaa".into(), (2, 10.0, 20.0));
        write_batch(&mut c, &first, 1000.0).await.unwrap();
        let mut second = Batch::default();
        second.seen.insert("w58-laptop".into(), 90.0);
        second.rejected.insert("w58aaaaa".into(), (3, 5.0, 15.0));
        write_batch(&mut c, &second, 1000.0).await.unwrap();
        let seen: f64 = c
            .query_one(
                "SELECT last_seen_at FROM principal_activity WHERE principal = 'w58-laptop'",
                &[],
            )
            .await
            .unwrap()
            .get(0);
        assert_eq!(seen, 100.0);
        let row = c
            .query_one(
                "SELECT rejections, first_seen_at, last_seen_at FROM rejected_bearers \
                 WHERE hash_prefix = 'w58aaaaa'",
                &[],
            )
            .await
            .unwrap();
        assert_eq!(
            (
                row.get::<_, i64>(0),
                row.get::<_, f64>(1),
                row.get::<_, f64>(2)
            ),
            (5, 5.0, 20.0)
        );
        // Past retention: the next write with refusals prunes it.
        let mut later = Batch::default();
        later.rejected.insert("w58bbbbb".into(), (1, 1.0e9, 1.0e9));
        write_batch(&mut c, &later, 1.0e9).await.unwrap();
        let left: i64 = c
            .query_one(
                "SELECT count(*) FROM rejected_bearers WHERE hash_prefix = 'w58aaaaa'",
                &[],
            )
            .await
            .unwrap()
            .get(0);
        assert_eq!(left, 0);
        c.batch_execute(
            "DELETE FROM principal_activity WHERE principal LIKE 'w58-%'; \
             DELETE FROM rejected_bearers WHERE hash_prefix LIKE 'w58%'",
        )
        .await
        .unwrap();
        drop(c);
        let _ = t.await;
    }
}
