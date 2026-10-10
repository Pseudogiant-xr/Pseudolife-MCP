//! Bearer gate: env tokens plus stored principals (spec B1-B9).

use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::RwLock;
use std::time::{Duration, Instant};

const RESERVED_ENV: [&str; 2] = ["default", "maintainer"];
const RESERVED_STORED: [&str; 3] = ["default", "daemon", "maintainer"];
/// A stored-principal snapshot older than this refuses requests (principal_store.py:46).
pub const STALE_AFTER: Duration = Duration::from_secs(60);
pub const REFRESH_EVERY: Duration = Duration::from_secs(10);

pub struct EnvTokens {
    /// (token, principal), in configured order.
    pub map: Vec<(String, String)>,
    pub single: Option<String>,
}

impl EnvTokens {
    pub fn from_env() -> Self {
        let single = std::env::var("PSEUDOLIFE_MCP_TOKEN")
            .ok()
            .filter(|t| !t.is_empty());
        let map = std::env::var("PSEUDOLIFE_MCP_TOKENS")
            .map(|raw| parse_token_map(&raw))
            .unwrap_or_default();
        EnvTokens { map, single }
    }

    pub fn configured(&self) -> bool {
        self.single.is_some() || !self.map.is_empty()
    }

    // Used by `load` (tests); main passes the principals to the refresher.
    #[cfg_attr(not(test), allow(dead_code))]
    fn principals(&self) -> impl Iterator<Item = &str> {
        self.map.iter().map(|(_, p)| p.as_str())
    }
}

/// `token:principal,...`, split on the last colon; entries naming a reserved
/// principal are skipped and a duplicate token keeps its first entry (principals.py:103).
pub fn parse_token_map(raw: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for part in raw.split(',') {
        let part = crate::storage::py_strip(part);
        let Some((tok, principal)) = part.rsplit_once(':') else {
            continue;
        };
        let (tok, principal) = (
            crate::storage::py_strip(tok),
            crate::storage::py_strip(principal).to_lowercase(),
        );
        if tok.is_empty() || principal.is_empty() || RESERVED_ENV.contains(&principal.as_str()) {
            continue;
        }
        if out.iter().any(|(t, _)| t == tok) {
            continue;
        }
        out.push((tok.to_string(), principal));
    }
    out
}

/// One row of `public.principals` (principal_store.py `StoredPrincipal`).
#[derive(Clone, Debug, PartialEq)]
pub struct StoredPrincipal {
    pub principal: String,
    pub token_hash: Option<String>,
    pub tier: Option<String>,
    pub board: bool,
    pub revoked: bool,
}

/// The tiers a stored principal may carry (principal_store.py `_TIERS`).
const TIERS: [&str; 3] = ["minimal", "core", "full"];

/// A tier on the ladder, or `None` (principal_store.py `_normal`): exact
/// match, no case folding.
pub fn normalize_tier(tier: Option<String>) -> Option<String> {
    tier.filter(|t| TIERS.contains(&t.as_str()))
}

/// Python's `str.strip()` with no argument: Unicode whitespace plus
/// U+001C-U+001F, which `str.isspace` also counts.
fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// principal_store.py `_normal`: the name stripped and lowercased and the
/// tier on the ladder, or `None` when the name is not a principal name.
pub fn normalize_stored(row: StoredPrincipal) -> Option<StoredPrincipal> {
    let name = py_strip(&row.principal).to_lowercase();
    if !valid_name(&name) {
        return None;
    }
    Some(StoredPrincipal {
        principal: name,
        tier: normalize_tier(row.tier),
        ..row
    })
}

#[derive(Default)]
struct Snapshot {
    /// Normalized rows collapsed by name, in first-appearance order with the
    /// later row's values (a Python dict's update order).
    rows: Vec<StoredPrincipal>,
    by_hash: HashMap<String, String>,
    loaded_at: Option<Instant>,
    /// The bank fingerprint read with the rows (`PrincipalSnapshot.bank`).
    bank: Option<String>,
    shadowed_rows: Vec<String>,
    invalid_rows: Vec<String>,
}

pub struct PrincipalStore {
    snap: RwLock<Snapshot>,
}

fn valid_name(name: &str) -> bool {
    let b = name.as_bytes();
    !b.is_empty()
        && b.len() <= 64
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b[1..].iter().all(|c| {
            c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, b'.' | b'_' | b'-')
        })
}

impl PrincipalStore {
    pub fn new() -> Self {
        PrincipalStore {
            snap: RwLock::new(Snapshot::default()),
        }
    }

    /// Replace the snapshot from `(principal, token_hash, revoked)` rows (no
    /// tier, board admitted): the spike's entry point, now used by the unit
    /// tests only. Same rules as [`Self::refresh`].
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn load(
        &self,
        rows: Vec<(String, Option<String>, bool)>,
        env: &EnvTokens,
        started: Instant,
    ) {
        let rows = rows
            .into_iter()
            .map(|(principal, token_hash, revoked)| StoredPrincipal {
                principal,
                token_hash,
                tier: None,
                board: true,
                revoked,
            })
            .collect();
        let env_principals: Vec<String> = env.principals().map(String::from).collect();
        self.refresh(rows, None, &env_principals, started);
    }

    /// `PrincipalSnapshot.refresh` (principal_store.py:118-141): replace the
    /// view with one read's rows. A row whose name is not a principal name is
    /// skipped first; a name the environment maps, or a reserved one
    /// (`default`, `daemon`, `maintainer`), is shadowed; rows collapsing to
    /// one name keep the later row (in the earlier one's position, as a
    /// Python dict update does). Only rows with a non-empty token hash that
    /// are not revoked authenticate. The snapshot is as old as the read's
    /// start, so a slow read cannot renew a stale snapshot.
    pub fn refresh(
        &self,
        rows: Vec<StoredPrincipal>,
        bank: Option<String>,
        env_principals: &[String],
        started: Instant,
    ) {
        let mut merged: Vec<StoredPrincipal> = Vec::new();
        let mut at: HashMap<String, usize> = HashMap::new();
        let (mut shadowed, mut invalid) = (Vec::new(), Vec::new());
        for row in rows {
            let raw = row.principal.clone();
            let Some(row) = normalize_stored(row) else {
                invalid.push(raw);
                continue;
            };
            if RESERVED_STORED.contains(&row.principal.as_str())
                || env_principals.contains(&row.principal)
            {
                shadowed.push(row.principal);
                continue;
            }
            match at.get(&row.principal) {
                Some(&i) => merged[i] = row,
                None => {
                    at.insert(row.principal.clone(), merged.len());
                    merged.push(row);
                }
            }
        }
        // A hash two names share goes to the later name (dict comprehension).
        let mut by_hash = HashMap::new();
        for row in &merged {
            if let Some(hash) = row.token_hash.as_deref().filter(|h| !h.is_empty())
                && !row.revoked
            {
                by_hash.insert(hash.to_string(), row.principal.clone());
            }
        }
        for list in [&mut shadowed, &mut invalid] {
            list.sort();
            list.dedup();
        }
        *self.snap.write().unwrap() = Snapshot {
            rows: merged,
            by_hash,
            loaded_at: Some(started),
            bank,
            shadowed_rows: shadowed,
            invalid_rows: invalid,
        };
    }
}

// Tier, board and bank reads: wired by later slices (MCP tiers, the board,
// /health), so not all are called yet.
#[allow(dead_code)]
impl PrincipalStore {
    fn row(&self, principal: &str) -> Option<StoredPrincipal> {
        let snap = self.snap.read().unwrap();
        snap.rows.iter().find(|r| r.principal == principal).cloned()
    }

    /// `tier_of`: the stored tier, `None` for an unknown or revoked row.
    pub fn tier_of(&self, principal: &str) -> Option<String> {
        self.row(principal)
            .filter(|r| !r.revoked)
            .and_then(|r| r.tier)
    }

    /// `admitted`: a stored row with board admission that is not revoked.
    pub fn admitted(&self, principal: &str) -> bool {
        self.row(principal).is_some_and(|r| r.board && !r.revoked)
    }

    /// `has`: the view holds a row of that name, revoked or not.
    pub fn has(&self, principal: &str) -> bool {
        self.row(principal).is_some()
    }

    /// The bank fingerprint the last successful read saw (`bank`).
    pub fn bank(&self) -> Option<String> {
        self.snap.read().unwrap().bank.clone()
    }

    /// Names the last read skipped: (shadowed, invalid), each sorted.
    pub fn skipped_rows(&self) -> (Vec<String>, Vec<String>) {
        let snap = self.snap.read().unwrap();
        (snap.shadowed_rows.clone(), snap.invalid_rows.clone())
    }

    /// Rows in the view (`len`).
    pub fn len(&self) -> usize {
        self.snap.read().unwrap().rows.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    fn available(&self) -> bool {
        matches!(self.snap.read().unwrap().loaded_at, Some(t) if t.elapsed() <= STALE_AFTER)
    }

    fn lookup(&self, hash: &str) -> Option<String> {
        self.snap.read().unwrap().by_hash.get(hash).cloned()
    }
}

pub enum Resolved {
    // Read once callers map principals to tiers (the MCP surface).
    #[allow(dead_code)]
    Principal(String),
    None,
    Unavailable,
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// `header` is the raw Authorization value decoded as latin-1 (one char per byte).
pub fn resolve(header: Option<&str>, env: &EnvTokens, store: &PrincipalStore) -> Resolved {
    if !env.configured() {
        return Resolved::Principal("default".into());
    }
    let Some(header) = header else {
        return Resolved::None;
    };
    let (scheme, rest) = header.split_once(' ').unwrap_or((header, ""));
    let token = rest.trim_matches([' ', '\t']);
    if !scheme.eq_ignore_ascii_case("bearer") || token.is_empty() {
        return Resolved::None;
    }
    // The header string is latin-1 decoded, so its chars are bytes. Python
    // re-encodes it as UTF-8 and, when possible, as latin-1.
    let utf8 = token.as_bytes().to_vec();
    let latin1: Option<Vec<u8>> = token.chars().map(|c| u8::try_from(c as u32).ok()).collect();
    let candidates: Vec<Vec<u8>> = if crate::mutants::active("security-drop-latin1") {
        vec![utf8]
    } else {
        std::iter::once(utf8).chain(latin1).collect()
    };
    // principals.py:290-300: every map token against both candidates first,
    // then the singular token.
    let matches = |token: &str| candidates.iter().any(|c| ct_eq(c, token.as_bytes()));
    for (tok, principal) in &env.map {
        if matches(tok) && !crate::mutants::active("auth-candidate-order") {
            return Resolved::Principal(principal.clone());
        }
    }
    if let Some(single) = &env.single
        && matches(single)
    {
        return Resolved::Principal("default".into());
    }
    if crate::mutants::active("auth-candidate-order") {
        for (tok, principal) in &env.map {
            if matches(tok) {
                return Resolved::Principal(principal.clone());
            }
        }
    }
    if !store.available() {
        return Resolved::Unavailable;
    }
    for cand in &candidates {
        if let Some(p) = store.lookup(&hex::encode(Sha256::digest(cand))) {
            return Resolved::Principal(p);
        }
    }
    Resolved::None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_map_token_is_tried_on_both_encodings_before_the_singular_token() {
        // principals.py:290-300. The client sends UTF-8 "café"; the header
        // arrives latin-1 decoded as "cafÃ©". Its UTF-8 candidate equals the
        // singular token, its latin-1 candidate the mapped one: the map wins.
        let e = EnvTokens {
            single: Some("caf\u{c3}\u{a9}".into()),
            map: parse_token_map("caf\u{e9}:alice"),
        };
        let s = PrincipalStore::new();
        let header = "Bearer caf\u{c3}\u{a9}";
        assert!(matches!(resolve(Some(header), &e, &s), Resolved::Principal(p) if p == "alice"));
    }

    fn env(single: Option<&str>, map: &str) -> EnvTokens {
        EnvTokens {
            single: single.map(String::from),
            map: parse_token_map(map),
        }
    }

    #[test]
    fn token_map_splits_on_last_colon_and_skips_reserved() {
        let m =
            parse_token_map("a:b:Alice, x:default ,y:maintainer,z:bob,z:carol,w:alice,,nocolon");
        assert_eq!(
            m,
            vec![
                ("a:b".into(), "alice".into()),
                ("z".into(), "bob".into()),
                ("w".into(), "alice".into())
            ]
        );
    }

    #[test]
    fn open_mode_is_default() {
        let s = PrincipalStore::new();
        assert!(
            matches!(resolve(None, &env(None, ""), &s), Resolved::Principal(p) if p == "default")
        );
    }

    #[test]
    fn env_and_store_resolution() {
        let e = env(Some("tok1"), "tok2:alice");
        let s = PrincipalStore::new();
        assert!(
            matches!(resolve(Some("Bearer tok1"), &e, &s), Resolved::Principal(p) if p == "default")
        );
        assert!(
            matches!(resolve(Some("bearer \ttok2 "), &e, &s), Resolved::Principal(p) if p == "alice")
        );
        assert!(matches!(
            resolve(Some("Basic tok1"), &e, &s),
            Resolved::None
        ));
        assert!(matches!(resolve(Some("Bearer"), &e, &s), Resolved::None));
        assert!(matches!(resolve(None, &e, &s), Resolved::None));
        // Unknown token with no snapshot loaded: unavailable.
        assert!(matches!(
            resolve(Some("Bearer nope"), &e, &s),
            Resolved::Unavailable
        ));
        let h = hex::encode(Sha256::digest(b"stored-tok"));
        s.load(
            vec![
                ("carol".into(), Some(h.clone()), false),
                (
                    "revoked".into(),
                    Some(hex::encode(Sha256::digest(b"r"))),
                    true,
                ),
                (
                    "alice".into(),
                    Some(hex::encode(Sha256::digest(b"shadow"))),
                    false,
                ),
                (
                    "Bad Name".into(),
                    Some(hex::encode(Sha256::digest(b"bad"))),
                    false,
                ),
                (
                    " Erin ".into(),
                    Some(hex::encode(Sha256::digest(b"erin-tok"))),
                    false,
                ),
            ],
            &e,
            Instant::now(),
        );
        assert!(
            matches!(resolve(Some("Bearer stored-tok"), &e, &s), Resolved::Principal(p) if p == "carol")
        );
        assert!(
            matches!(resolve(Some("Bearer erin-tok"), &e, &s), Resolved::Principal(p) if p == "erin")
        );
        for t in ["r", "shadow", "bad", "nope"] {
            assert!(
                matches!(
                    resolve(Some(&format!("Bearer {t}")), &e, &s),
                    Resolved::None
                ),
                "{t}"
            );
        }
    }

    fn sp(
        name: &str,
        hash: Option<&str>,
        tier: Option<&str>,
        board: bool,
        revoked: bool,
    ) -> StoredPrincipal {
        StoredPrincipal {
            principal: name.into(),
            token_hash: hash.map(String::from),
            tier: tier.map(String::from),
            board,
            revoked,
        }
    }

    #[test]
    fn tier_and_name_normalization() {
        assert_eq!(normalize_tier(Some("core".into())), Some("core".into()));
        assert_eq!(normalize_tier(Some("Core".into())), None);
        assert_eq!(normalize_tier(Some("admin".into())), None);
        assert_eq!(normalize_tier(None), None);
        let n = normalize_stored(sp("\u{1f} Erin\u{a0}", None, Some("full"), true, false)).unwrap();
        assert_eq!(n.principal, "erin");
        assert_eq!(n.tier.as_deref(), Some("full"));
        assert!(normalize_stored(sp("bad name", None, None, true, false)).is_none());
        assert!(normalize_stored(sp(".dot", None, None, true, false)).is_none());
        assert!(normalize_stored(sp(&"a".repeat(65), None, None, true, false)).is_none());
        assert!(normalize_stored(sp(&"a".repeat(64), None, None, true, false)).is_some());
    }

    #[test]
    fn refresh_collapses_by_name_later_wins() {
        let s = PrincipalStore::new();
        let e = env(Some("tok1"), "tok2:alice");
        let h = |t: &[u8]| hex::encode(Sha256::digest(t));
        s.refresh(
            vec![
                sp("bob", Some(&h(b"old")), Some("core"), true, false),
                sp("alice", Some(&h(b"shadow")), None, true, false),
                sp("Daemon", Some(&h(b"d")), None, true, false),
                sp("no way", Some(&h(b"x")), None, true, false),
                sp("carol", Some(&h(b"c")), Some("full"), false, false),
                sp(" BOB ", Some(&h(b"new")), Some("bogus"), true, false),
                sp("dave", Some(&h(b"dv")), Some("minimal"), true, true),
                sp("erin", Some(""), Some("minimal"), true, false),
            ],
            Some("fp".into()),
            &["alice".to_string()],
            Instant::now(),
        );
        // bob: the later row wins (new hash, tier normalized away).
        assert!(
            matches!(resolve(Some("Bearer new"), &e, &s), Resolved::Principal(p) if p == "bob")
        );
        assert!(matches!(
            resolve(Some("Bearer old"), &e, &s),
            Resolved::None
        ));
        assert_eq!(s.tier_of("bob"), None);
        // Revoked rows stay in the view but neither authenticate nor admit.
        assert!(matches!(resolve(Some("Bearer dv"), &e, &s), Resolved::None));
        assert!(s.has("dave") && !s.admitted("dave") && s.tier_of("dave").is_none());
        // board = false: authenticates, not admitted; tier kept.
        assert!(
            matches!(resolve(Some("Bearer c"), &e, &s), Resolved::Principal(p) if p == "carol")
        );
        assert!(!s.admitted("carol"));
        assert_eq!(s.tier_of("carol").as_deref(), Some("full"));
        // Empty hash: in the view, never authenticates.
        assert!(s.has("erin") && s.admitted("erin"));
        assert_eq!(s.tier_of("erin").as_deref(), Some("minimal"));
        // Shadowed and invalid rows are left out.
        assert!(!s.has("alice") && !s.has("daemon"));
        assert!(matches!(resolve(Some("Bearer d"), &e, &s), Resolved::None));
        assert_eq!(
            s.skipped_rows(),
            (
                vec!["alice".to_string(), "daemon".to_string()],
                vec!["no way".to_string()]
            )
        );
        assert_eq!(s.len(), 4);
        assert_eq!(s.bank().as_deref(), Some("fp"));
    }

    #[test]
    fn shared_hash_goes_to_the_later_name() {
        let s = PrincipalStore::new();
        let e = env(Some("tok1"), "");
        let h = hex::encode(Sha256::digest(b"same"));
        s.refresh(
            vec![
                sp("x", Some(&h), None, true, false),
                sp("y", Some(&h), None, true, false),
                // A later row for x keeps x's (first) position: y still wins.
                sp("x", Some(&h), None, true, false),
            ],
            None,
            &[],
            Instant::now(),
        );
        assert!(matches!(resolve(Some("Bearer same"), &e, &s), Resolved::Principal(p) if p == "y"));
    }

    #[test]
    fn snapshot_age_is_measured_from_the_read_start() {
        let s = PrincipalStore::new();
        let e = env(Some("tok1"), "");
        let old = Instant::now()
            .checked_sub(STALE_AFTER + Duration::from_secs(1))
            .unwrap();
        s.refresh(vec![], None, &[], old);
        assert!(matches!(
            resolve(Some("Bearer nope"), &e, &s),
            Resolved::Unavailable
        ));
        s.refresh(vec![], None, &[], Instant::now());
        assert!(matches!(
            resolve(Some("Bearer nope"), &e, &s),
            Resolved::None
        ));
    }
}
