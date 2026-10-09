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
        let single = std::env::var("PSEUDOLIFE_MCP_TOKEN").ok().filter(|t| !t.is_empty());
        let map = std::env::var("PSEUDOLIFE_MCP_TOKENS")
            .map(|raw| parse_token_map(&raw))
            .unwrap_or_default();
        EnvTokens { map, single }
    }

    pub fn configured(&self) -> bool {
        self.single.is_some() || !self.map.is_empty()
    }

    fn principals(&self) -> impl Iterator<Item = &str> {
        self.map.iter().map(|(_, p)| p.as_str())
    }
}

/// `token:principal,...`, split on the last colon; reserved and duplicate
/// principals skipped, duplicate tokens keep the first entry (principals.py:103).
pub fn parse_token_map(raw: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for part in raw.split(',') {
        let part = part.trim();
        let Some((tok, principal)) = part.rsplit_once(':') else { continue };
        let (tok, principal) = (tok.trim(), principal.trim().to_lowercase());
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

#[derive(Default)]
struct Snapshot {
    by_hash: HashMap<String, String>,
    loaded_at: Option<Instant>,
}

pub struct PrincipalStore {
    snap: RwLock<Snapshot>,
}

fn valid_name(name: &str) -> bool {
    let b = name.as_bytes();
    !b.is_empty()
        && b.len() <= 64
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b[1..]
            .iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, b'.' | b'_' | b'-'))
}

impl PrincipalStore {
    pub fn new() -> Self {
        PrincipalStore { snap: RwLock::new(Snapshot::default()) }
    }

    /// Replace the snapshot from `(principal, token_hash, revoked)` rows.
    pub fn load(&self, rows: Vec<(String, Option<String>, bool)>, env: &EnvTokens) {
        let mut by_hash = HashMap::new();
        for (principal, hash, revoked) in rows {
            let Some(hash) = hash.filter(|h| !h.is_empty()) else { continue };
            if revoked
                || !valid_name(&principal)
                || RESERVED_STORED.contains(&principal.as_str())
                || env.principals().any(|p| p == principal)
            {
                continue;
            }
            by_hash.insert(hash, principal);
        }
        *self.snap.write().unwrap() = Snapshot { by_hash, loaded_at: Some(Instant::now()) };
    }

    fn available(&self) -> bool {
        matches!(self.snap.read().unwrap().loaded_at, Some(t) if t.elapsed() <= STALE_AFTER)
    }

    fn lookup(&self, hash: &str) -> Option<String> {
        self.snap.read().unwrap().by_hash.get(hash).cloned()
    }
}

pub enum Resolved {
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
    let Some(header) = header else { return Resolved::None };
    let (scheme, rest) = header.split_once(' ').unwrap_or((header, ""));
    let token = rest.trim_matches([' ', '\t']);
    if !scheme.eq_ignore_ascii_case("bearer") || token.is_empty() {
        return Resolved::None;
    }
    // The header string is latin-1 decoded, so its chars are bytes. Python
    // re-encodes it as UTF-8 and, when possible, as latin-1.
    let utf8 = token.as_bytes().to_vec();
    let latin1: Option<Vec<u8>> = token.chars().map(|c| u8::try_from(c as u32).ok()).collect();
    let candidates: Vec<Vec<u8>> = std::iter::once(utf8).chain(latin1).collect();
    for cand in &candidates {
        for (tok, principal) in &env.map {
            if ct_eq(cand, tok.as_bytes()) {
                return Resolved::Principal(principal.clone());
            }
        }
        if let Some(single) = &env.single {
            if ct_eq(cand, single.as_bytes()) {
                return Resolved::Principal("default".into());
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

    fn env(single: Option<&str>, map: &str) -> EnvTokens {
        EnvTokens { single: single.map(String::from), map: parse_token_map(map) }
    }

    #[test]
    fn token_map_splits_on_last_colon_and_skips_reserved() {
        let m = parse_token_map("a:b:Alice, x:default ,y:maintainer,z:bob,z:carol,w:alice,,nocolon");
        assert_eq!(
            m,
            vec![("a:b".into(), "alice".into()), ("z".into(), "bob".into()), ("w".into(), "alice".into())]
        );
    }

    #[test]
    fn open_mode_is_default() {
        let s = PrincipalStore::new();
        assert!(matches!(resolve(None, &env(None, ""), &s), Resolved::Principal(p) if p == "default"));
    }

    #[test]
    fn env_and_store_resolution() {
        let e = env(Some("tok1"), "tok2:alice");
        let s = PrincipalStore::new();
        assert!(matches!(resolve(Some("Bearer tok1"), &e, &s), Resolved::Principal(p) if p == "default"));
        assert!(matches!(resolve(Some("bearer \ttok2 "), &e, &s), Resolved::Principal(p) if p == "alice"));
        assert!(matches!(resolve(Some("Basic tok1"), &e, &s), Resolved::None));
        assert!(matches!(resolve(Some("Bearer"), &e, &s), Resolved::None));
        assert!(matches!(resolve(None, &e, &s), Resolved::None));
        // Unknown token with no snapshot loaded: unavailable.
        assert!(matches!(resolve(Some("Bearer nope"), &e, &s), Resolved::Unavailable));
        let h = hex::encode(Sha256::digest(b"stored-tok"));
        s.load(
            vec![
                ("carol".into(), Some(h.clone()), false),
                ("revoked".into(), Some(hex::encode(Sha256::digest(b"r"))), true),
                ("alice".into(), Some(hex::encode(Sha256::digest(b"shadow"))), false),
                ("Bad Name".into(), Some(hex::encode(Sha256::digest(b"bad"))), false),
            ],
            &e,
        );
        assert!(matches!(resolve(Some("Bearer stored-tok"), &e, &s), Resolved::Principal(p) if p == "carol"));
        for t in ["r", "shadow", "bad", "nope"] {
            assert!(matches!(resolve(Some(&format!("Bearer {t}")), &e, &s), Resolved::None), "{t}");
        }
    }
}
