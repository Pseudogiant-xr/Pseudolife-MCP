//! Console sign-in sessions (schema v59; spec 2026-11-02, sections 3 and 4),
//! from `pseudolife_memory/console_sessions.py`.
//!
//! A **Console session** is a bearer the browser minted and holds; the
//! daemon stores only its SHA-256, as with pairing. This module holds:
//!
//! * [`SessionSnapshot`], the daemon's view of `console_sessions`, keyed by
//!   token hash. The principals refresher (`principals::Refresher`) reloads
//!   it on its own connection right after the principals, so resolving a
//!   bearer never touches the database.
//! * [`resolve`], the session resolver. Only the call sites the spec names
//!   use it: the `/api` transport gate (except `/api/coordination/*`,
//!   `/api/daemon-notice`, and `/api/console/link`, where a session is
//!   resolved only to be refused), and through it the board snapshot and
//!   the maintainer routes. Hooks, `/mcp` and the board's mailbox routes
//!   never do, so a session can never register an agent or send mail.
//! * the operations behind the routes (`routes::signin`): [`issue_code`],
//!   [`redeem_code`], [`list_sessions`], [`revoke_session`], and
//!   [`revoke_principal_sessions`], which revoking or re-pairing a principal
//!   runs in its own transaction.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio_postgres::error::SqlState;
use tokio_postgres::{Client, GenericClient};

use crate::auth::{EnvTokens, PrincipalSource, PrincipalStore, presented_bearer};

// Lifetimes from the spec (2026-11-02, section 3). Conventions, not
// measurements: the spec lists them as an open question for review.
pub const SESSION_IDLE_S: f64 = 30.0 * 86400.0;
pub const SESSION_MAX_S: f64 = 90.0 * 86400.0;
pub const CODE_TTL_S: f64 = 5.0 * 60.0;
/// A session's last use is written back at most this often (the
/// refresher's activity cadence), so a busy Console costs one UPDATE a
/// minute.
pub const USE_FLUSH: Duration = Duration::from_secs(60);
/// How long a spent or expired code row, or a dead session row, is kept
/// before [`issue_code`] prunes it.
const CODE_KEEP_S: f64 = 86400.0;
/// The session view counts as available this long after its read began
/// (`SessionSnapshot(max_age=60.0)`), as the principals' view does.
pub const STALE_AFTER: Duration = crate::auth::STALE_AFTER;

pub const METHOD_LINK: &str = "link";
pub const METHOD_PASSKEY: &str = "passkey";
pub const SOURCE_ENVIRONMENT: &str = "environment";
pub const SOURCE_STORE: &str = "store";

const LABEL_MAX: usize = 64;
const LOOPBACK_HOSTS: [&str; 3] = ["127.0.0.1", "::1", "localhost"];
const NOW: &str = "EXTRACT(EPOCH FROM clock_timestamp())::double precision";
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const PAIRING_CODE_ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// `time.time()`.
pub fn wall_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock after epoch")
        .as_secs_f64()
}

// -- origins -------------------------------------------------------------------

/// What [`normalize_origin`] reads of `urllib.parse.urlsplit` (CPython 3.11).
struct Split {
    scheme: String,
    netloc: String,
    path: String,
    query: String,
    fragment: String,
}

/// `urlsplit(url)`, `Err` where it raises `ValueError`. The callers have
/// already refused every character below 33 and DEL, so the leading C0
/// strip and the tab/CR/LF removal have nothing to do.
fn urlsplit(url: &str) -> Result<Split, ()> {
    let mut rest = url;
    let mut scheme = String::new();
    if let Some(i) = rest.find(':')
        && i > 0
    {
        let candidate = &rest[..i];
        let first = candidate.chars().next().unwrap_or(' ');
        if first.is_ascii_alphabetic()
            && candidate
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
        {
            scheme = candidate.to_ascii_lowercase();
            rest = &rest[i + 1..];
        }
    }
    let mut netloc = String::new();
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = after[..end].to_string();
        rest = &after[end..];
        if netloc.contains('[') != netloc.contains(']') {
            return Err(()); // "Invalid IPv6 URL"
        }
        if netloc.contains('[') {
            check_bracketed_netloc(&netloc)?;
        }
    }
    let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    check_netloc(&netloc)?;
    Ok(Split {
        scheme,
        netloc,
        path: path.to_string(),
        query: query.to_string(),
        fragment: fragment.to_string(),
    })
}

/// `_check_bracketed_netloc` (CPython 3.11.14+): nothing before the bracket,
/// nothing but a port after it, and a bracketed host that is an IPv6
/// address or the `v<hex>.<text>` future form.
fn check_bracketed_netloc(netloc: &str) -> Result<(), ()> {
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let host = match hostinfo.split_once('[') {
        Some((before, bracketed)) => {
            if !before.is_empty() {
                return Err(());
            }
            let (host, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            if !port.is_empty() && !port.starts_with(':') {
                return Err(());
            }
            host
        }
        None => hostinfo.split_once(':').map_or(hostinfo, |(h, _)| h),
    };
    let ok = if let Some(rest) = host.strip_prefix('v') {
        rest.split_once('.').is_some_and(|(hex, tail)| {
            !hex.is_empty() && hex.bytes().all(|b| b.is_ascii_hexdigit()) && !tail.is_empty()
        })
    } else {
        let address = host.split_once('%').map_or(host, |(a, _)| a);
        address.parse::<std::net::Ipv6Addr>().is_ok()
    };
    if ok { Ok(()) } else { Err(()) }
}

/// `_checknetloc`: a non-ASCII netloc whose NFKC form gains a `/?#@:`
/// character raises.
fn check_netloc(netloc: &str) -> Result<(), ()> {
    use unicode_normalization::UnicodeNormalization;
    if netloc.is_empty() || netloc.is_ascii() {
        return Ok(());
    }
    let n: String = netloc
        .chars()
        .filter(|c| !matches!(c, '@' | ':' | '#' | '?'))
        .collect();
    let normalized: String = n.nfkc().collect();
    if n == normalized {
        return Ok(());
    }
    if normalized.contains(['/', '?', '#', '@', ':']) {
        return Err(());
    }
    Ok(())
}

/// `SplitResult._hostinfo`: (hostname, port text), each `None` when empty.
fn hostinfo(netloc: &str) -> (Option<&str>, Option<&str>) {
    let info = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let (host, port) = match info.split_once('[') {
        Some((_, bracketed)) => {
            let (host, tail) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            (host, tail.split_once(':').map_or("", |(_, p)| p))
        }
        None => info.split_once(':').unwrap_or((info, "")),
    };
    (
        (!host.is_empty()).then_some(host),
        (!port.is_empty()).then_some(port),
    )
}

/// `SplitResult.hostname`: lower-cased before any `%zone`, which is kept.
fn hostname(netloc: &str) -> Option<String> {
    let host = hostinfo(netloc).0?;
    Some(match host.split_once('%') {
        Some((h, zone)) => format!("{}%{zone}", h.to_lowercase()),
        None => host.to_lowercase(),
    })
}

/// `SplitResult.port`: `Err` where Python raises `ValueError`.
fn port(netloc: &str) -> Result<Option<u16>, ()> {
    match hostinfo(netloc).1 {
        None => Ok(None),
        Some(p) if p.bytes().all(|b| b.is_ascii_digit()) => p
            .parse::<u32>()
            .ok()
            .and_then(|n| u16::try_from(n).ok())
            .map(Some)
            .ok_or(()),
        Some(_) => Err(()),
    }
}

/// `normalize_origin`: the origin a browser would send for `value`, or
/// `None` when it is not a plain `http`/`https` origin. Scheme and host are
/// lower-cased, the scheme's default port and a trailing slash dropped; a
/// path, query, fragment or credentials are refused, so a stored origin
/// always equals what a browser's `Origin` header carries.
pub fn normalize_origin(value: &str) -> Option<String> {
    if value.is_empty() || value.chars().count() > 255 {
        return None;
    }
    if value.chars().any(|c| (c as u32) < 33 || c as u32 == 127) {
        return None;
    }
    let parts = urlsplit(value).ok()?;
    let port = port(&parts.netloc).ok()?;
    let scheme = parts.scheme.as_str();
    if scheme != "http" && scheme != "https" {
        return None;
    }
    if parts.netloc.contains('@') {
        return None; // a username (even an empty one) or a password
    }
    if !matches!(parts.path.as_str(), "" | "/")
        || !parts.query.is_empty()
        || !parts.fragment.is_empty()
        || value.contains('#')
    {
        return None;
    }
    let mut host = hostname(&parts.netloc)?;
    if host.contains(':') {
        host = format!("[{host}]");
    }
    let default = if scheme == "http" { 80 } else { 443 };
    Some(match port.filter(|p| *p != default) {
        Some(p) => format!("{scheme}://{host}:{p}"),
        None => format!("{scheme}://{host}"),
    })
}

/// `origin_key`: the form a code's origin is stored and compared in. A
/// loopback origin's host is `localhost` whichever alias names it
/// (`127.0.0.1`, `[::1]`), so a code made through one redeems in a Console
/// opened at another; scheme and port still have to match.
pub fn origin_key(origin: &str) -> String {
    let Ok(parts) = urlsplit(origin) else {
        return origin.to_string();
    };
    let host = hostname(&parts.netloc).unwrap_or_default();
    if LOOPBACK_HOSTS.contains(&host.as_str()) {
        let port = match port(&parts.netloc) {
            Ok(Some(p)) => format!(":{p}"),
            _ => String::new(),
        };
        return format!("{}://localhost{port}", parts.scheme);
    }
    origin.to_string()
}

/// `is_loopback_origin`.
pub fn is_loopback_origin(origin: &str) -> bool {
    urlsplit(origin)
        .ok()
        .and_then(|p| hostname(&p.netloc))
        .is_some_and(|h| LOOPBACK_HOSTS.contains(&h.as_str()))
}

/// `valid_route_id`: `[a-z][a-z0-9-]{0,31}`, the shape of a Console route
/// id a sign-in link may land on (`&to=`).
pub fn valid_route_id(value: &str) -> bool {
    let b = value.as_bytes();
    !b.is_empty()
        && b.len() <= 32
        && b[0].is_ascii_lowercase()
        && b[1..]
            .iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

/// `clean_label`: whitespace runs collapsed to one space (`str.split()`),
/// at most 64 printable characters, else `None`.
pub fn clean_label(value: &str) -> Option<String> {
    let label = value
        .split(crate::labels::py_isspace)
        .filter(|w| !w.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    if label.chars().count() > LABEL_MAX || !label.chars().all(crate::storage::py_printable) {
        return None;
    }
    Some(label)
}

/// `bearer_hashes`: the SHA-256 hex of the presented bearer as the resolver
/// judges it: its UTF-8 bytes, then its latin-1 bytes when those differ.
/// `header` is latin-1 decoded (one char per byte), as the gate reads it.
pub fn bearer_hashes(header: Option<&str>) -> Vec<String> {
    let Some(token) = presented_bearer(header) else {
        return Vec::new();
    };
    let mut out = vec![hex::encode(Sha256::digest(token.as_bytes()))];
    let latin1: Option<Vec<u8>> = token.chars().map(|c| u8::try_from(c as u32).ok()).collect();
    if let Some(bytes) = latin1 {
        let hash = hex::encode(Sha256::digest(&bytes));
        if !out.contains(&hash) {
            out.push(hash);
        }
    }
    out
}

/// `environment_issuers`: `{sha256(token): principal}` for the daemon's
/// environment tokens. A link session one of them issued resolves only
/// while that same token still names the same principal.
pub fn environment_issuers(env: &EnvTokens) -> HashMap<String, String> {
    let mut out: HashMap<String, String> = env
        .map
        .iter()
        .map(|(tok, p)| (hex::encode(Sha256::digest(tok.as_bytes())), p.clone()))
        .collect();
    if let Some(single) = &env.single {
        out.entry(hex::encode(Sha256::digest(single.as_bytes())))
            .or_insert_with(|| "default".into());
    }
    out
}

/// `principals.environment_principals`: every mapped name, and `default`
/// when the singular token is set.
pub fn environment_principals(env: &EnvTokens) -> HashSet<String> {
    let mut out: HashSet<String> = env.map.iter().map(|(_, p)| p.clone()).collect();
    if env.single.is_some() {
        out.insert("default".into());
    }
    out
}

/// `new_pairing_code`: 12 characters from the 32-symbol alphabet (60 bits).
pub fn new_code() -> String {
    let mut buf = [0u8; 12];
    getrandom::fill(&mut buf).expect("system randomness");
    // 256 is a multiple of 32, so the low five bits are uniform.
    buf.iter()
        .map(|b| PAIRING_CODE_ALPHABET[(b & 31) as usize] as char)
        .collect()
}

/// `format_pairing_code`: `XXXX-XXXX-XXXX`.
pub fn format_code(code: &str) -> String {
    code.as_bytes()
        .chunks(4)
        .map(|c| String::from_utf8_lossy(c).into_owned())
        .collect::<Vec<_>>()
        .join("-")
}

/// `secrets.token_hex(8)`.
fn new_session_id() -> String {
    let mut buf = [0u8; 8];
    getrandom::fill(&mut buf).expect("system randomness");
    hex::encode(buf)
}

// -- the snapshot -----------------------------------------------------------------

/// One row of `console_sessions` (`ConsoleSession`).
#[derive(Clone, Debug, PartialEq)]
pub struct ConsoleSession {
    pub session_id: String,
    pub token_hash: String,
    pub principal: String,
    /// The issuing credential's source: `environment` or `store`.
    pub source: String,
    /// The SHA-256 of the issuing bearer, or a passkey credential id.
    pub issuer: String,
    /// `link` or `passkey`.
    pub method: String,
    pub label: String,
    pub created_at: f64,
    pub last_used_at: Option<f64>,
    pub expires_at: f64,
}

#[derive(Default)]
struct Inner {
    by_hash: HashMap<String, ConsoleSession>,
    added: HashMap<String, (u64, ConsoleSession)>,
    dropped: HashMap<String, u64>,
    used: HashMap<String, f64>,
    unflushed: HashMap<String, f64>,
    seq: u64,
    loaded_at: Option<Instant>,
    active_passkeys: HashSet<String>,
}

/// A refresh's start: taken before the database read, so a sign-in or a
/// revocation that commits during the read survives it.
pub struct SessionRead {
    seq: u64,
    started: Instant,
}

/// `SessionSnapshot`: the live rows of `console_sessions` (not revoked, not
/// past their absolute expiry). An [`Self::add`] (a sign-in that just
/// committed) or a [`Self::drop_ids`] (a revocation that just committed)
/// holds until a refresh that started after it completes. Uses are counted
/// in memory ([`Self::note_use`]) and written back by the refresher
/// ([`Self::take_uses`]).
#[derive(Default)]
pub struct SessionSnapshot {
    inner: Mutex<Inner>,
}

impl SessionSnapshot {
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().expect("console sessions")
    }

    pub fn begin_refresh(&self) -> SessionRead {
        self.begin_refresh_at(Instant::now())
    }

    pub fn begin_refresh_at(&self, started: Instant) -> SessionRead {
        SessionRead {
            seq: self.lock().seq,
            started,
        }
    }

    /// `refresh`: replace the view with one read's rows and active passkey
    /// credential ids, dated from the read's start.
    pub fn finish_refresh(
        &self,
        read: SessionRead,
        rows: Vec<ConsoleSession>,
        passkeys: Vec<String>,
    ) {
        let mut s = self.lock();
        s.added.retain(|_, (seq, _)| *seq > read.seq);
        s.dropped.retain(|_, seq| *seq > read.seq);
        let mut merged: HashMap<String, ConsoleSession> = HashMap::new();
        for row in rows {
            if !s.dropped.contains_key(&row.session_id) {
                merged.insert(row.token_hash.clone(), row);
            }
        }
        for (_, row) in s.added.values() {
            merged.insert(row.token_hash.clone(), row.clone());
        }
        let live: HashSet<String> = merged.values().map(|r| r.session_id.clone()).collect();
        // Uses of sessions that are gone have nothing left to extend.
        s.used.retain(|sid, _| live.contains(sid));
        s.by_hash = merged;
        s.active_passkeys = passkeys.into_iter().collect();
        s.loaded_at = Some(read.started);
    }

    /// A sign-in that just committed: usable at once.
    pub fn add(&self, row: ConsoleSession) {
        let mut s = self.lock();
        s.seq += 1;
        let seq = s.seq;
        s.added.insert(row.session_id.clone(), (seq, row.clone()));
        s.by_hash.insert(row.token_hash.clone(), row);
    }

    /// Revocations that just committed: gone at once (`drop`).
    pub fn drop_ids(&self, ids: &[String]) {
        if ids.is_empty() {
            return;
        }
        let mut s = self.lock();
        s.seq += 1;
        let seq = s.seq;
        for sid in ids {
            s.dropped.insert(sid.clone(), seq);
            s.added.remove(sid);
        }
        s.by_hash.retain(|_, row| !ids.contains(&row.session_id));
    }

    pub fn available(&self) -> bool {
        self.lock()
            .loaded_at
            .is_some_and(|t| t.elapsed() <= STALE_AFTER)
    }

    pub fn lookup(&self, token_hash: &str) -> Option<ConsoleSession> {
        self.lock().by_hash.get(token_hash).cloned()
    }

    pub fn rows(&self) -> Vec<ConsoleSession> {
        self.lock().by_hash.values().cloned().collect()
    }

    pub fn has_passkey(&self, credential_id: &str) -> bool {
        self.lock().active_passkeys.contains(credential_id)
    }

    /// `last_used`: the later of the stored last use (else its creation)
    /// and a use this daemon saw but has not written yet.
    pub fn last_used(&self, row: &ConsoleSession) -> f64 {
        let local = self.lock().used.get(&row.session_id).copied();
        let stored = row.last_used_at.unwrap_or(row.created_at);
        local.map_or(stored, |l| stored.max(l))
    }

    /// The in-memory last use of `session_id`, if any (`with_last_use`).
    pub fn local_use(&self, session_id: &str) -> Option<f64> {
        self.lock().used.get(session_id).copied()
    }

    pub fn note_use(&self, session_id: &str, now: f64) {
        let mut s = self.lock();
        s.used.insert(session_id.into(), now);
        s.unflushed.insert(session_id.into(), now);
    }

    /// The uses not written yet, now handed to the writer (sorted by id).
    pub fn take_uses(&self) -> BTreeMap<String, f64> {
        std::mem::take(&mut self.lock().unflushed)
            .into_iter()
            .collect()
    }

    /// A write that failed: keep its uses for the next one.
    pub fn give_back_uses(&self, uses: BTreeMap<String, f64>) {
        let mut s = self.lock();
        for (sid, stamp) in uses {
            let slot = s.unflushed.entry(sid).or_insert(0.0);
            if *slot < stamp {
                *slot = stamp;
            }
        }
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.lock().by_hash.len()
    }

    #[cfg(test)]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

// -- resolution -----------------------------------------------------------------

/// The answer depends on a view that is stale: a 503, never a 401 (which
/// the Console would read as "sign in again").
#[derive(Debug, PartialEq, Eq)]
pub struct Unavailable;

/// `SessionHit`.
#[derive(Clone, Debug)]
pub struct SessionHit {
    pub principal: String,
    /// The issuing credential's source (environment or store).
    pub issued_by: PrincipalSource,
    pub session: ConsoleSession,
}

/// The views a resolution reads.
pub struct Views<'a> {
    pub sessions: &'a SessionSnapshot,
    pub env_issuers: &'a HashMap<String, String>,
    pub env_principals: &'a HashSet<String>,
    pub store: &'a PrincipalStore,
}

fn source_of(source: &str) -> Option<PrincipalSource> {
    match source {
        SOURCE_ENVIRONMENT => Some(PrincipalSource::Environment),
        SOURCE_STORE => Some(PrincipalSource::Store),
        _ => None,
    }
}

/// `_principal_resolves`: whether `principal` still resolves on its own,
/// from `source`.
fn principal_resolves(principal: &str, source: &str, v: &Views<'_>) -> Result<bool, Unavailable> {
    match source {
        SOURCE_ENVIRONMENT => Ok(v.env_principals.contains(principal)),
        SOURCE_STORE => {
            if !v.store.available() {
                return Err(Unavailable);
            }
            Ok(v.store.resolves(principal))
        }
        _ => Ok(false),
    }
}

/// `still_resolves`: whether `row` still signs a browser in: both
/// lifetimes, its issuer and its principal. `Err` when that depends on a
/// stale stored view.
pub fn still_resolves(row: &ConsoleSession, v: &Views<'_>, now: f64) -> Result<bool, Unavailable> {
    if now >= row.expires_at || now >= row.created_at + SESSION_MAX_S {
        return Ok(false);
    }
    if now - v.sessions.last_used(row) > SESSION_IDLE_S {
        return Ok(false);
    }
    match row.method.as_str() {
        METHOD_LINK => match row.source.as_str() {
            SOURCE_ENVIRONMENT => {
                if v.env_issuers.get(&row.issuer) != Some(&row.principal) {
                    return Ok(false);
                }
            }
            SOURCE_STORE => {
                if !v.store.available() {
                    return Err(Unavailable);
                }
                if v.store.lookup(&row.issuer).as_deref() != Some(row.principal.as_str()) {
                    return Ok(false);
                }
            }
            _ => return Ok(false),
        },
        METHOD_PASSKEY => {
            if !v.sessions.has_passkey(&row.issuer) {
                return Ok(false);
            }
        }
        _ => return Ok(false),
    }
    principal_resolves(&row.principal, &row.source, v)
}

/// `resolve`: the Console session `header` presents, or `None`. A session
/// stops resolving, each alone, when it is revoked (not in the view), 30
/// days past its last use, 90 days past its creation, when its principal
/// no longer resolves on its own, or when its issuer is gone. `Err` when
/// the answer depends on a stale view: the session view itself, or the
/// stored-principal view for a session a stored principal issued.
pub fn resolve(
    header: Option<&str>,
    sessions: Option<&SessionSnapshot>,
    env_issuers: &HashMap<String, String>,
    env_principals: &HashSet<String>,
    store: &PrincipalStore,
    now: f64,
) -> Result<Option<SessionHit>, Unavailable> {
    let Some(sessions) = sessions else {
        return Ok(None);
    };
    let hashes = bearer_hashes(header);
    if hashes.is_empty() {
        return Ok(None);
    }
    if !sessions.available() {
        return Err(Unavailable);
    }
    let Some(row) = hashes.iter().find_map(|h| sessions.lookup(h)) else {
        return Ok(None);
    };
    let views = Views {
        sessions,
        env_issuers,
        env_principals,
        store,
    };
    if !still_resolves(&row, &views, now)? {
        return Ok(None);
    }
    let Some(issued_by) = source_of(&row.source) else {
        return Ok(None);
    };
    sessions.note_use(&row.session_id, now);
    Ok(Some(SessionHit {
        principal: row.principal.clone(),
        issued_by,
        session: row,
    }))
}

// -- the database side ---------------------------------------------------------

const SESSION_COLUMNS: &str = "session_id, token_hash, principal, source, issuer, method, label, \
     created_at, last_used_at, expires_at";

fn session_row(r: &tokio_postgres::Row) -> ConsoleSession {
    ConsoleSession {
        session_id: r.get(0),
        token_hash: r.get(1),
        principal: r.get(2),
        source: r.get(3),
        issuer: r.get(4),
        method: r.get(5),
        label: r.get::<_, Option<String>>(6).unwrap_or_default(),
        created_at: r.get(7),
        last_used_at: r.get(8),
        expires_at: r.get(9),
    }
}

/// `load_sessions`: the live sessions and the active passkey credential
/// ids, on the refresher's autocommit connection. A missing table (a bank
/// before v59) reads as empty. Never runs DDL.
pub async fn load_sessions(
    client: &Client,
) -> Result<(Vec<ConsoleSession>, Vec<String>), tokio_postgres::Error> {
    let rows = match client
        .query(
            &format!(
                "SELECT {SESSION_COLUMNS} FROM public.console_sessions \
                 WHERE revoked_at IS NULL AND expires_at > {NOW}"
            ),
            &[],
        )
        .await
    {
        Ok(rows) => rows.iter().map(session_row).collect(),
        Err(e) if e.code() == Some(&SqlState::UNDEFINED_TABLE) => Vec::new(),
        Err(e) => return Err(e),
    };
    let keys = match client
        .query(
            "SELECT credential_id FROM public.maintainer_passkeys WHERE state = 'active'",
            &[],
        )
        .await
    {
        Ok(rows) => rows.iter().map(|r| r.get::<_, String>(0)).collect(),
        Err(e)
            if e.code() == Some(&SqlState::UNDEFINED_TABLE)
                || e.code() == Some(&SqlState::UNDEFINED_COLUMN) =>
        {
            Vec::new()
        }
        Err(e) => return Err(e),
    };
    Ok((rows, keys))
}

/// `flush_uses`: write sessions' last use, never moving it backwards.
pub async fn flush_uses(
    client: &mut Client,
    uses: &BTreeMap<String, f64>,
) -> Result<(), tokio_postgres::Error> {
    if uses.is_empty() {
        return Ok(());
    }
    let tx = client.transaction().await?;
    for (sid, stamp) in uses {
        tx.execute(
            "UPDATE public.console_sessions SET last_used_at = GREATEST(\
             COALESCE(last_used_at, 0), $1) WHERE session_id = $2",
            &[stamp, sid],
        )
        .await?;
    }
    tx.commit().await
}

async fn connect(dsn: &str) -> Result<(Client, tokio::task::JoinHandle<()>), String> {
    let config = crate::storage::parse_dsn(dsn)?;
    crate::storage::connect(&config, CONNECT_TIMEOUT).await
}

async fn close(client: Client, task: tokio::task::JoinHandle<()>) {
    drop(client);
    let _ = task.await;
}

fn text(e: &tokio_postgres::Error) -> String {
    crate::storage::error_text(e)
}

/// `issue_code`: a one-time sign-in code for `principal` (12 characters,
/// 60 bits), redeemable at `origin` for [`CODE_TTL_S`] seconds. Only its
/// hash is stored; old code and session rows are pruned on the way.
/// Returns the code and its expiry (database clock).
pub async fn issue_code(
    dsn: &str,
    principal: &str,
    source: &str,
    issuer: &str,
    origin: &str,
) -> Result<(String, f64), String> {
    let (client, task) = connect(dsn).await?;
    let out = issue_on(&client, principal, source, issuer, origin).await;
    close(client, task).await;
    out
}

async fn issue_on(
    client: &Client,
    principal: &str,
    source: &str,
    issuer: &str,
    origin: &str,
) -> Result<(String, f64), String> {
    // For the whole connection: the prune commits before the insert.
    client
        .batch_execute("SET statement_timeout = '5s'")
        .await
        .map_err(|e| text(&e))?;
    client
        .execute(
            &format!("DELETE FROM public.console_signin_codes WHERE expires_at < {NOW} - $1"),
            &[&CODE_KEEP_S],
        )
        .await
        .map_err(|e| text(&e))?;
    // Signed-out and expired sessions are kept a day for the record, then
    // go, so the refresher's scan stays small.
    client
        .execute(
            &format!(
                "DELETE FROM public.console_sessions \
                 WHERE revoked_at < {NOW} - $1 OR expires_at < {NOW} - $1"
            ),
            &[&CODE_KEEP_S],
        )
        .await
        .map_err(|e| text(&e))?;
    for _attempt in 0..5 {
        let code = new_code();
        let hash = hex::encode(Sha256::digest(code.as_bytes()));
        match client
            .query_one(
                &format!(
                    "INSERT INTO public.console_signin_codes \
                     (code_hash, principal, source, issuer, origin, expires_at, created_at) \
                     VALUES ($1, $2, $3, $4, $5, {NOW} + $6, {NOW}) RETURNING expires_at"
                ),
                &[&hash, &principal, &source, &issuer, &origin, &CODE_TTL_S],
            )
            .await
        {
            Ok(row) => return Ok((code, row.get(0))),
            Err(e) if e.code() == Some(&SqlState::UNIQUE_VIOLATION) => continue,
            Err(e) => return Err(text(&e)),
        }
    }
    Err("could not draw an unused sign-in code".into())
}

/// What a redemption came to.
#[derive(Debug, PartialEq)]
pub enum Redeemed {
    Session(ConsoleSession),
    /// Any refusal: unknown, expired, spent, another origin, the issuer
    /// gone, the token hash already in use. Nothing was spent.
    Refused,
    /// The issuer could not be checked (a stale stored view): nothing was
    /// spent.
    Unavailable,
}

/// `redeem_code`: redeem a sign-in code at `origin` for the browser's
/// `token_hash`: the code is spent and the session created in one
/// transaction, on the database clock, so two redeemers cannot both win.
/// `still_valid(principal, source, issuer)` re-checks, inside it, that the
/// issuing credential still resolves; a refusal rolls back. `Err` is a
/// database failure (the outcome unknown).
pub async fn redeem_code<F>(
    dsn: &str,
    code_hash: &str,
    token_hash: &str,
    origin: &str,
    label: &str,
    still_valid: F,
) -> Result<Redeemed, String>
where
    F: FnOnce(&str, &str, &str) -> Result<bool, Unavailable>,
{
    let (mut client, task) = connect(dsn).await?;
    let out = redeem_on(
        &mut client,
        code_hash,
        token_hash,
        origin,
        label,
        still_valid,
    )
    .await;
    close(client, task).await;
    out.map_err(|e| text(&e))
}

async fn redeem_on<F>(
    client: &mut Client,
    code_hash: &str,
    token_hash: &str,
    origin: &str,
    label: &str,
    still_valid: F,
) -> Result<Redeemed, tokio_postgres::Error>
where
    F: FnOnce(&str, &str, &str) -> Result<bool, Unavailable>,
{
    let tx = client.transaction().await?;
    tx.batch_execute("SET LOCAL statement_timeout = '5s'")
        .await?;
    let Some(code) = tx
        .query_opt(
            &format!(
                "UPDATE public.console_signin_codes SET used_at = {NOW} \
                 WHERE code_hash = $1 AND used_at IS NULL AND expires_at > {NOW} \
                 AND origin = $2 RETURNING principal, source, issuer"
            ),
            &[&code_hash, &origin],
        )
        .await?
    else {
        tx.rollback().await?;
        return Ok(Redeemed::Refused);
    };
    let (principal, source, issuer): (String, String, String) =
        (code.get(0), code.get(1), code.get(2));
    match still_valid(&principal, &source, &issuer) {
        Ok(true) => {}
        Ok(false) => {
            tx.rollback().await?;
            return Ok(Redeemed::Refused);
        }
        Err(Unavailable) => {
            tx.rollback().await?;
            return Ok(Redeemed::Unavailable);
        }
    }
    let inserted = tx
        .query_one(
            &format!(
                "INSERT INTO public.console_sessions \
                 (session_id, token_hash, principal, source, issuer, method, label, \
                  created_at, last_used_at, expires_at) \
                 VALUES ($1, $2, $3, $4, $5, $6, $7, {NOW}, {NOW}, {NOW} + $8) \
                 RETURNING {SESSION_COLUMNS}"
            ),
            &[
                &new_session_id(),
                &token_hash,
                &principal,
                &source,
                &issuer,
                &METHOD_LINK,
                &label,
                &SESSION_MAX_S,
            ],
        )
        .await;
    match inserted {
        Ok(row) => {
            let session = session_row(&row);
            tx.commit().await?;
            Ok(Redeemed::Session(session))
        }
        Err(e) if e.code() == Some(&SqlState::UNIQUE_VIOLATION) => Ok(Redeemed::Refused),
        Err(e) => Err(e),
    }
}

/// `describe`: a session as the routes show it, never its token hash or
/// issuer.
pub fn describe(row: &ConsoleSession) -> Value {
    json!({"session_id": row.session_id, "principal": row.principal, "method": row.method,
        "label": row.label, "created_at": row.created_at,
        "last_used_at": row.last_used_at, "expires_at": row.expires_at})
}

/// `list_sessions`: the sessions that still resolve by their dates (not
/// revoked, inside both lifetimes), newest first; `principal` limits them
/// to one.
pub async fn list_sessions(dsn: &str, principal: Option<&str>) -> Result<Vec<Value>, String> {
    let (client, task) = connect(dsn).await?;
    let out = async {
        client.batch_execute("SET statement_timeout = '5s'").await?;
        let base = format!(
            "SELECT {SESSION_COLUMNS} FROM public.console_sessions \
             WHERE revoked_at IS NULL AND expires_at > {NOW} \
             AND COALESCE(last_used_at, created_at) > {NOW} - $1"
        );
        let rows = match principal {
            None => {
                client
                    .query(
                        &format!("{base} ORDER BY created_at DESC, session_id"),
                        &[&SESSION_IDLE_S],
                    )
                    .await?
            }
            Some(p) => {
                client
                    .query(
                        &format!("{base} AND principal = $2 ORDER BY created_at DESC, session_id"),
                        &[&SESSION_IDLE_S, &p],
                    )
                    .await?
            }
        };
        Ok::<_, tokio_postgres::Error>(rows.iter().map(|r| describe(&session_row(r))).collect())
    }
    .await;
    close(client, task).await;
    out.map_err(|e| text(&e))
}

/// `revoke_session`: revoke one session (`principal` limits it to that
/// principal's). False when there is no such live session.
pub async fn revoke_session(
    dsn: &str,
    session_id: &str,
    principal: Option<&str>,
) -> Result<bool, String> {
    let (mut client, task) = connect(dsn).await?;
    let out = async {
        let tx = client.transaction().await?;
        tx.batch_execute("SET LOCAL statement_timeout = '5s'")
            .await?;
        let sql = format!(
            "UPDATE public.console_sessions SET revoked_at = {NOW} \
             WHERE session_id = $1 AND revoked_at IS NULL"
        );
        let row = match principal {
            None => {
                tx.query_opt(&format!("{sql} RETURNING session_id"), &[&session_id])
                    .await?
            }
            Some(p) => {
                tx.query_opt(
                    &format!("{sql} AND principal = $2 RETURNING session_id"),
                    &[&session_id, &p],
                )
                .await?
            }
        };
        tx.commit().await?;
        Ok::<_, tokio_postgres::Error>(row.is_some())
    }
    .await;
    close(client, task).await;
    out.map_err(|e| text(&e))
}

/// `revoke_principal_sessions`: revoke `principal`'s live sessions inside
/// the caller's transaction (revoking or re-pairing a principal). `method`
/// limits it to one sign-in method; `keep_issuer` spares the sessions that
/// credential issued. A bank before v59 has no sessions: 0. The table is
/// probed first, since a failed statement would abort the transaction.
pub async fn revoke_principal_sessions<C: GenericClient>(
    conn: &C,
    principal: &str,
    method: Option<&str>,
    keep_issuer: Option<&str>,
) -> Result<u64, tokio_postgres::Error> {
    let present: bool = conn
        .query_one(
            "SELECT to_regclass('public.console_sessions') IS NOT NULL",
            &[],
        )
        .await?
        .get(0);
    if !present {
        return Ok(0);
    }
    let rows = conn
        .query(
            &format!(
                "UPDATE public.console_sessions SET revoked_at = {NOW} \
                 WHERE principal = $1 AND revoked_at IS NULL \
                 AND ($2::text IS NULL OR method = $2) \
                 AND ($3::text IS NULL OR issuer <> $3) RETURNING session_id"
            ),
            &[&principal, &method, &keep_issuer],
        )
        .await?;
    Ok(rows.len() as u64)
}

/// `with_last_use`: `rows` with each last use the daemon has seen but not
/// written yet.
pub fn with_last_use(rows: Vec<Value>, sessions: &SessionSnapshot) -> Vec<Value> {
    rows.into_iter()
        .map(|mut item| {
            let sid = item["session_id"].as_str().unwrap_or("").to_string();
            if let Some(local) = sessions.local_use(&sid) {
                let stored = item["last_used_at"].as_f64().unwrap_or(0.0);
                if stored < local {
                    item["last_used_at"] = json!(local);
                }
            }
            item
        })
        .collect()
}

#[cfg(test)]
#[path = "console_sessions_tests.rs"]
mod tests;
