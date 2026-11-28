//! Principal-scoped toolset tiers (`toolset_tiers.py`): minimal ⊂ core ⊂ full.
//! Resolution is principal override (memory_toolset, 12 h TTL) → tier map
//! (`PSEUDOLIFE_MCP_TIER_MAP`) → the stored principal's tier → the default
//! (`PSEUDOLIFE_MCP_TOOLSET`). Visibility only: hidden tools stay callable.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Tier {
    Minimal,
    Core,
    Full,
}

pub const LADDER: [Tier; 3] = [Tier::Minimal, Tier::Core, Tier::Full];

/// Overrides are working-day-scoped (`SESSION_TTL_S`).
pub const OVERRIDE_TTL: Duration = Duration::from_secs(12 * 3600);
/// At most this many overrides are kept; past it the oldest goes first.
/// Not measured: an entry is a short key and a timestamp, so the cap only
/// bounds memory against keys that change on every request.
pub const OVERRIDE_CAP: usize = 10_000;

impl Tier {
    pub fn parse(s: &str) -> Option<Tier> {
        match s {
            "minimal" => Some(Tier::Minimal),
            "core" => Some(Tier::Core),
            "full" => Some(Tier::Full),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Tier::Minimal => "minimal",
            Tier::Core => "core",
            Tier::Full => "full",
        }
    }

    /// One rung up or down, clamped to `[floor, full]` (`step`).
    pub fn step(self, delta: i32, floor: Tier) -> Tier {
        let i = (self as i32 + delta).clamp(floor as i32, 2);
        LADDER[i as usize]
    }
}

/// Python `str.strip().lower()` for the ASCII names real configs use.
pub fn norm(s: &str) -> String {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        .to_lowercase()
}

/// `normalize_tier`: unset or blank is `full`; an unknown value warns and is `full`.
pub fn normalize_tier(value: Option<&str>, context: &str) -> Tier {
    let v = norm(value.unwrap_or(""));
    if let Some(t) = Tier::parse(&v) {
        return t;
    }
    if !v.is_empty() {
        eprintln!("unknown toolset tier {value:?} ({context}) — falling back to 'full'");
    }
    Tier::Full
}

/// `parse_tier_map`: `writer:tier,...`; malformed entries are skipped, and a
/// later entry for the same writer wins.
pub fn parse_tier_map(raw: Option<&str>) -> HashMap<String, Tier> {
    let mut out = HashMap::new();
    for part in raw.unwrap_or("").split(',') {
        let part = norm_keep_case(part);
        if part.is_empty() {
            continue;
        }
        let (writer, tier) = match part.split_once(':') {
            Some((w, t)) => (norm(w), norm(t)),
            None => {
                eprintln!("tier-map entry {part:?} malformed (want writer:tier) — skipped");
                continue;
            }
        };
        match Tier::parse(&tier) {
            Some(t) if !writer.is_empty() => {
                out.insert(writer, t);
            }
            _ => eprintln!("tier-map entry {part:?} malformed (want writer:tier) — skipped"),
        }
    }
    out
}

fn norm_keep_case(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// TTL'd principal-tier overrides (`PrincipalTierState`). Keys are the
/// lowercased writer-id namespace; an empty key is the shared bucket.
pub struct Overrides {
    ttl: Duration,
    cap: usize,
    map: Mutex<HashMap<String, (Tier, Instant)>>,
}

/// Expired overrides go once the map is large, then the oldest past `cap`.
fn bound(map: &mut HashMap<String, (Tier, Instant)>, now: Instant, ttl: Duration, cap: usize) {
    if map.len() > 256 {
        map.retain(|_, (_, ts)| now.duration_since(*ts) <= ttl);
    }
    while map.len() > cap {
        let Some(oldest) = map
            .iter()
            .min_by_key(|(_, (_, ts))| *ts)
            .map(|(key, _)| key.clone())
        else {
            break;
        };
        map.remove(&oldest);
    }
}

const GLOBAL: &str = "__global__";

impl Overrides {
    pub fn new(ttl: Duration) -> Self {
        Self::with_cap(ttl, OVERRIDE_CAP)
    }

    fn with_cap(ttl: Duration, cap: usize) -> Self {
        Self {
            ttl,
            cap,
            map: Mutex::new(HashMap::new()),
        }
    }

    fn key(key: Option<&str>) -> String {
        let k = norm(key.unwrap_or(""));
        if k.is_empty() { GLOBAL.to_string() } else { k }
    }

    pub fn get(&self, key: Option<&str>) -> Option<Tier> {
        let k = Self::key(key);
        let mut map = self.map.lock().expect("tier overrides lock");
        let (tier, ts) = *map.get(&k)?;
        if ts.elapsed() >= self.ttl {
            map.remove(&k);
            return None;
        }
        Some(tier)
    }

    /// Read, step and write an override in one critical section.
    pub fn transition(
        &self,
        key: Option<&str>,
        default: Tier,
        step: impl FnOnce(Tier) -> Tier,
    ) -> (Tier, Tier) {
        let k = Self::key(key);
        let mut map = self.map.lock().expect("tier overrides lock");
        let now = Instant::now();
        if map
            .get(&k)
            .is_some_and(|(_, ts)| now.duration_since(*ts) >= self.ttl)
        {
            map.remove(&k);
        }
        let current = map.get(&k).map_or(default, |(tier, _)| *tier);
        let next = step(current);
        if next != current {
            map.insert(k, (next, now));
            bound(&mut map, now, self.ttl, self.cap);
        }
        (current, next)
    }

    #[cfg(test)]
    pub fn set(&self, key: Option<&str>, tier: Tier) {
        let k = Self::key(key);
        let now = Instant::now();
        let mut map = self.map.lock().expect("tier overrides lock");
        map.insert(k, (tier, now));
        bound(&mut map, now, self.ttl, self.cap);
    }
}

/// `resolve_tier`: override → tier map → stored tier → default. `stored`
/// answers for the request's own bearer only (`_stored_tier_of`).
pub fn resolve(
    principal: Option<&str>,
    overrides: &Overrides,
    map: &HashMap<String, Tier>,
    default: Tier,
    stored: impl Fn(&str) -> Option<Tier>,
) -> Tier {
    if let Some(t) = overrides.get(principal) {
        return t;
    }
    if let Some(p) = principal.filter(|p| !p.is_empty()) {
        let key = norm(p);
        if let Some(t) = map.get(&key).copied().or_else(|| stored(&key)) {
            return t;
        }
    }
    default
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn past_the_cap_the_oldest_override_goes() {
        let o = Overrides::with_cap(OVERRIDE_TTL, 3);
        let now = Instant::now();
        for (key, seconds) in [("a", 30), ("b", 20), ("c", 10)] {
            o.set(Some(key), Tier::Full);
            o.map.lock().unwrap().get_mut(key).unwrap().1 = now - Duration::from_secs(seconds);
        }
        o.set(Some("d"), Tier::Full);
        assert_eq!(o.map.lock().unwrap().len(), 3);
        assert_eq!(o.get(Some("a")), None);
        for key in ["b", "c", "d"] {
            assert_eq!(o.get(Some(key)), Some(Tier::Full), "{key}");
        }
        // A transition that changes a tier is bounded the same way.
        o.transition(Some("e"), Tier::Core, |_| Tier::Minimal);
        assert_eq!(o.map.lock().unwrap().len(), 3);
        assert_eq!(o.get(Some("b")), None);
    }

    #[test]
    fn map_parsing_matches_python() {
        let m = parse_tier_map(Some(
            " Alice : Minimal ,bob:core,bad,:core,x:huge,bob:full,c:d:e",
        ));
        assert_eq!(m.get("alice"), Some(&Tier::Minimal));
        assert_eq!(m.get("bob"), Some(&Tier::Full));
        assert_eq!(m.len(), 2);
    }

    #[test]
    fn default_tier_is_lenient() {
        assert_eq!(normalize_tier(None, "t"), Tier::Full);
        assert_eq!(normalize_tier(Some(" CORE "), "t"), Tier::Core);
        assert_eq!(normalize_tier(Some("bogus"), "t"), Tier::Full);
    }

    #[test]
    fn step_clamps_to_floor() {
        assert_eq!(Tier::Core.step(1, Tier::Minimal), Tier::Full);
        assert_eq!(Tier::Full.step(1, Tier::Minimal), Tier::Full);
        assert_eq!(Tier::Core.step(-1, Tier::Core), Tier::Core);
        assert_eq!(Tier::Full.step(-1, Tier::Minimal), Tier::Core);
    }

    #[test]
    fn overrides_expire_and_share_the_blank_bucket() {
        let o = Overrides::new(Duration::from_millis(0));
        o.set(Some("A"), Tier::Full);
        assert_eq!(o.get(Some("a")), None);
        let o = Overrides::new(OVERRIDE_TTL);
        o.set(None, Tier::Core);
        assert_eq!(o.get(Some("  ")), Some(Tier::Core));
        o.set(Some(" Bob "), Tier::Full);
        assert_eq!(o.get(Some("bob")), Some(Tier::Full));
    }
}
