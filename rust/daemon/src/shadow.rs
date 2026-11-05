//! The read-only shadow's incremental refresh (issue #1322).
//!
//! A shadow serves reads from a bank another process writes, and that writer
//! writes on its read path too (access counts, reinforcements, the counters
//! row, episodes and session meta). Rebuilding every resident store on each
//! such change held the old and the new copy of everything at once. This
//! refresh keeps the cheap whole-table marker as its gate, then reloads only
//! the stores whose input tables changed, and keeps the bank in place when
//! the changed `entries` rows differ only in telemetry or are new rows that
//! a rebuild would append at the end anyway.
//!
//! Every mutation path of the resident state this patches in place (grep
//! for the state, not the API), and why none races it in a shadow:
//! - `Bank.entries` / `Bank.matrix`: `Bank::push` (store, dream stages),
//!   `Bank::remove` (store), `entries.push` in `dream/cursors.rs`, and
//!   `bank.write()` in correction, forget, outcome, episode, routes/episodes,
//!   dream run and dream tracking initialization. All are write tools,
//!   refused by the read-only boundary, or dream actions, refused too.
//! - `Entry.access_count`: the search bump, masked by `count_access &&
//!   !read_only` (`read/search_route.rs`), and the writer's access-count
//!   sync (`Service::save_counters`, which returns early when read-only).
//! - `Entry.reinforcements`: outcome and dream run (write paths only).
//! - `Entry.last_logical_turn`: hydration only.
//!
//! So in a shadow this refresh, under the service lock, is the only writer
//! of the resident bank.

use crate::bank::{Bank, Entry, Hydrated};
use crate::config::MemoryConfig;
use crate::storage::{SHADOW_META_KEYS, SHADOW_TABLES};
use anyhow::{Context, Result};
use serde_json::Value;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::RwLock;
use std::sync::atomic::Ordering;
use tokio_postgres::Client;

/// One `(count, hash, hash)` row per [`SHADOW_TABLES`] entry.
pub(crate) type Marker = Vec<(i64, String, String)>;

const ENTRIES: usize = 0;
const FACTS: usize = 1;
const WORLD: usize = 2;
const LESSONS: usize = 3;
const EPISODES: usize = 4;
const COUNTERS: usize = 5;
const META: usize = 6;

/// The meta keys `startup::metadata` reads (the identity metadata).
const IDENTITY_KEYS: [&str; 5] = [
    "active_session_pointer",
    "episode_tombstones",
    "deferred_empty_roots",
    "cortex_supersession_log",
    "cortex_dream_cursor",
];

/// Changed rows are fetched and compared this many at a time, so a change
/// to many rows holds one chunk's text and vectors, not the table's.
const FETCH_CHUNK: usize = 256;

/// The version recorded for a resident row whose loaded version is unknown
/// (it was added between the version read and the hydration): never a real
/// `xmin`, so the next refresh fetches and compares the row.
const UNKNOWN_VERSION: i64 = -1;

/// What the resident stores were built from: the marker probed before the
/// last refresh, each resident entry's row version (`xmin`), and the meta
/// values the stores read.
pub(crate) struct ShadowState {
    /// The marker probed before the refresh that built the resident state,
    /// so a commit during it leaves the next probe different.
    pub marker: Marker,
    /// Exactly the resident bank's ids.
    versions: HashMap<i64, i64>,
    meta: BTreeMap<String, Value>,
}

/// The inputs a full hydration records, read before it.
pub(crate) struct Inputs {
    versions: Vec<(i64, i64)>,
    meta: BTreeMap<String, Value>,
}

/// Read the row versions and meta values before a full hydration. Reading
/// them first errs only toward refetching: a row changed meanwhile carries
/// an older version here than the one hydrated, and the next refresh
/// fetches and compares it.
pub(crate) async fn preread(client: &Client) -> Result<Inputs> {
    Ok(Inputs {
        versions: entry_versions(client).await?,
        meta: meta_values(client).await?,
    })
}

impl ShadowState {
    pub(crate) fn new(marker: Marker, inputs: Inputs, bank: &Bank) -> ShadowState {
        ShadowState {
            marker,
            versions: reconcile(inputs.versions.into_iter().collect(), bank),
            meta: inputs.meta,
        }
    }
}

/// The versions restricted to the bank's ids; a resident id without a
/// version gets [`UNKNOWN_VERSION`].
fn reconcile(mut versions: HashMap<i64, i64>, bank: &Bank) -> HashMap<i64, i64> {
    bank.entries
        .iter()
        .map(|e| (e.id, versions.remove(&e.id).unwrap_or(UNKNOWN_VERSION)))
        .collect()
}

async fn entry_versions(client: &Client) -> Result<Vec<(i64, i64)>> {
    // Tuple headers only: no text or vector is detoasted.
    let rows = crate::txn::with_client(client, |c| {
        c.query(
            "SELECT id, xmin::text::bigint FROM entries ORDER BY id",
            &[],
        )
    })
    .await
    .context("entry version read failed")?;
    Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
}

async fn meta_values(client: &Client) -> Result<BTreeMap<String, Value>> {
    let keys: Vec<String> = SHADOW_META_KEYS.iter().map(|k| k.to_string()).collect();
    let rows = crate::txn::with_client(client, |c| async move {
        c.query("SELECT key, value FROM meta WHERE key = ANY($1)", &[&keys])
            .await
    })
    .await
    .context("meta read failed")?;
    let mut values = BTreeMap::new();
    for row in rows {
        values.insert(row.try_get(0)?, row.try_get(1)?);
    }
    Ok(values)
}

/// How the current row versions differ from the resident ones.
#[derive(Debug, Default, PartialEq)]
struct Diff {
    /// Ids not resident, ascending.
    added: Vec<i64>,
    /// Resident ids whose version changed, ascending.
    changed: Vec<i64>,
    deleted: usize,
}

fn diff(resident: &HashMap<i64, i64>, current: &[(i64, i64)]) -> Diff {
    let mut out = Diff::default();
    let mut present = 0;
    for &(id, version) in current {
        match resident.get(&id) {
            None => out.added.push(id),
            Some(&old) => {
                present += 1;
                if old != version {
                    out.changed.push(id);
                }
            }
        }
    }
    out.deleted = resident.len() - present;
    out.added.sort_unstable();
    out.changed.sort_unstable();
    out
}

/// Every column hydration reads other than the telemetry the writer bumps
/// on its read path (`access_count`, `reinforcements`, `last_logical_turn`).
/// A stored zero `ts` is replaced by the clock at hydration, so the
/// resident value cannot be compared with it: that counts as a change.
fn same_content(resident: &Entry, fresh: &Hydrated) -> bool {
    let f = &fresh.entry;
    fresh.stored_ts != 0.0
        && resident.ts == fresh.stored_ts
        && resident.id == f.id
        && resident.stored_band == f.stored_band
        && resident.text == f.text
        && resident.embedding == f.embedding
        && resident.surprise_value == f.surprise_value
        && resident.source == f.source
        && resident.superseded_at == f.superseded_at
        && resident.superseded_by_text == f.superseded_by_text
        && resident.episode_id == f.episode_id
        && resident.episode_title == f.episode_title
        && resident.tags == f.tags
        && resident.slots == f.slots
        && resident.authority == f.authority
        && resident.distortion_tolerance == f.distortion_tolerance
        && resident.dream_state == f.dream_state
}

/// The telemetry of one resident entry, as the bank now stores it.
#[derive(Debug, PartialEq)]
struct Patch {
    id: i64,
    access_count: i64,
    reinforcements: i32,
    last_logical_turn: Option<i32>,
}

impl Patch {
    fn of(entry: &Entry) -> Patch {
        Patch {
            id: entry.id,
            access_count: entry.access_count.load(Ordering::Relaxed),
            reinforcements: entry.reinforcements,
            last_logical_turn: entry.last_logical_turn,
        }
    }
}

enum EntriesPlan {
    InPlace {
        patches: Vec<Patch>,
        appends: Vec<(Entry, Vec<f32>)>,
        versions: HashMap<i64, i64>,
    },
    Rebuild(&'static str, Vec<(i64, i64)>),
}

/// The result of one refresh attempt.
pub(crate) enum Outcome {
    /// Rebuild every store as a cold start does, for this reason.
    Full(String),
    Partial(Box<Refresh>),
}

/// What changed, as loaded, for the caller to install. Nothing resident has
/// been modified yet: [`Refresh::apply_bank`] patches the bank in place.
pub(crate) struct Refresh {
    /// The state to record once the refresh is installed.
    pub state: ShadowState,
    /// A rebuilt bank (its rows changed beyond telemetry, or were deleted).
    pub bank: Option<Bank>,
    /// The cortex store (not yet configured) and its startup rows.
    pub cortex: Option<(crate::stores::cortex::CortexStore, Vec<Value>)>,
    pub world: Option<(crate::stores::world::WorldStore, Vec<Value>)>,
    pub lessons: Option<(crate::stores::lessons::LessonStore, Vec<Value>)>,
    /// The episode rows and the open episode.
    pub episodes: Option<(Vec<Value>, Option<String>)>,
    /// The identity metadata, re-derived because one of its keys changed.
    pub metadata: Option<Value>,
    /// The cortex's own meta (supersession log, dream cursor) changed.
    pub cortex_meta: bool,
    /// The durable clock high-water mark changed.
    pub clock: bool,
    /// The episode write state is rebuilt over this counters row.
    pub counters: Option<Option<crate::counters::Row>>,
    pub stats: Stats,
    patches: Vec<Patch>,
    appends: Vec<(Entry, Vec<f32>)>,
}

/// What a refresh did, for its log line and the tests.
#[derive(Debug, Default, Clone, PartialEq)]
pub(crate) struct Stats {
    /// Resident entries whose telemetry was patched.
    pub patched: usize,
    /// New entries appended to the resident bank.
    pub appended: usize,
    /// The tables whose stores were reloaded, in [`SHADOW_TABLES`] order.
    pub reloaded: Vec<&'static str>,
}

impl Refresh {
    /// The startup inputs change, so the startup state is rebuilt (sharing
    /// the unchanged parts) and its clock reseeded.
    pub(crate) fn startup_changed(&self) -> bool {
        self.cortex.is_some()
            || self.world.is_some()
            || self.lessons.is_some()
            || self.episodes.is_some()
            || self.metadata.is_some()
            || self.clock
    }

    /// Patch the telemetry and append the new entries in place. Indices of
    /// existing entries never move, so a request holding hit indices from
    /// an earlier read still addresses the same entries.
    pub(crate) fn apply_bank(&mut self, bank: &RwLock<Bank>) {
        if self.patches.is_empty() && self.appends.is_empty() {
            return;
        }
        let mut bank = bank.write().expect("bank lock");
        if !self.patches.is_empty() {
            let index: HashMap<i64, usize> = bank
                .entries
                .iter()
                .enumerate()
                .map(|(i, e)| (e.id, i))
                .collect();
            for patch in self.patches.drain(..) {
                if let Some(&i) = index.get(&patch.id) {
                    let entry = &mut bank.entries[i];
                    entry
                        .access_count
                        .store(patch.access_count, Ordering::Relaxed);
                    entry.reinforcements = patch.reinforcements;
                    entry.last_logical_turn = patch.last_logical_turn;
                }
            }
        }
        let dim = bank.dim;
        bank.entries.reserve(self.appends.len());
        bank.matrix.reserve(self.appends.len() * dim);
        for (entry, row) in self.appends.drain(..) {
            bank.push(entry, &row);
        }
    }

    /// The suffix of the refresh log line.
    pub(crate) fn summary(&self) -> String {
        let mut parts = Vec::new();
        if self.stats.patched > 0 || self.stats.appended > 0 {
            parts.push(format!(
                "entries: {} patched, {} appended",
                self.stats.patched, self.stats.appended
            ));
        }
        if !self.stats.reloaded.is_empty() {
            parts.push(format!("reloaded: {}", self.stats.reloaded.join(", ")));
        }
        if parts.is_empty() {
            "no resident change".into()
        } else {
            parts.join("; ")
        }
    }
}

/// Bring the resident stores up to the bank whose marker was just probed,
/// reading only what changed. `bank` is the resident bank the state
/// describes; nothing resident is modified here.
pub(crate) async fn refresh(
    client: &Client,
    memory: &MemoryConfig,
    dim: usize,
    state: &ShadowState,
    marker: &Marker,
    bank: &RwLock<Bank>,
) -> Result<Outcome> {
    if marker.len() != SHADOW_TABLES.len() || state.marker.len() != SHADOW_TABLES.len() {
        return Ok(Outcome::Full("marker shape changed".into()));
    }
    let changed = |table: usize| marker[table] != state.marker[table];
    let mut out = Refresh {
        state: ShadowState {
            marker: marker.clone(),
            versions: HashMap::new(),
            meta: BTreeMap::new(),
        },
        bank: None,
        cortex: None,
        world: None,
        lessons: None,
        episodes: None,
        metadata: None,
        cortex_meta: false,
        clock: false,
        counters: None,
        stats: Stats::default(),
        patches: Vec::new(),
        appends: Vec::new(),
    };
    // Meta first: a new bank identity rebuilds everything.
    out.state.meta = if changed(META) {
        let fresh = meta_values(client).await?;
        let differs = |key: &str| fresh.get(key) != state.meta.get(key);
        if differs("bank_id") {
            return Ok(Outcome::Full("bank identity changed".into()));
        }
        if IDENTITY_KEYS.iter().any(|k| differs(k)) {
            let raw: serde_json::Map<String, Value> = IDENTITY_KEYS
                .iter()
                .filter_map(|k| fresh.get(*k).map(|v| (k.to_string(), v.clone())))
                .collect();
            out.metadata = Some(crate::startup::identity_metadata(&Value::Object(raw))?);
            out.cortex_meta = differs("cortex_supersession_log") || differs("cortex_dream_cursor");
        }
        out.clock = differs("coordination_hlc_highwater");
        if out.metadata.is_some() || out.clock {
            out.stats.reloaded.push(SHADOW_TABLES[META]);
        }
        fresh
    } else {
        state.meta.clone()
    };
    out.state.versions = if changed(ENTRIES) {
        match plan_entries(client, memory, dim, state, bank).await? {
            EntriesPlan::InPlace {
                patches,
                appends,
                versions,
            } => {
                out.stats.patched = patches.len();
                out.stats.appended = appends.len();
                out.patches = patches;
                out.appends = appends;
                versions
            }
            EntriesPlan::Rebuild(reason, current) => {
                eprintln!("read-only shadow: rebuilding the bank ({reason})");
                let (rebuilt, stale) = crate::bank::hydrate_config(client, memory, dim).await?;
                if !stale.is_empty() {
                    return Ok(Outcome::Full(
                        "entry vectors do not fit the embedder".into(),
                    ));
                }
                let versions = reconcile(current.into_iter().collect(), &rebuilt);
                out.bank = Some(rebuilt);
                out.stats.reloaded.push(SHADOW_TABLES[ENTRIES]);
                versions
            }
        }
    } else {
        state.versions.clone()
    };
    if changed(FACTS) {
        let store = crate::stores::cortex::hydrate(client, dim).await?;
        let rows = crate::startup::cortex_rows(client).await?;
        if !crate::startup::stale_cortex_dims(&rows, dim).is_empty() {
            return Ok(Outcome::Full(
                "cortex vectors do not fit the embedder".into(),
            ));
        }
        out.cortex = Some((store, rows));
        out.stats.reloaded.push(SHADOW_TABLES[FACTS]);
    }
    if changed(WORLD) {
        let store = crate::stores::world::hydrate(client).await?;
        out.world = Some((store, crate::startup::world_rows(client).await?));
        out.stats.reloaded.push(SHADOW_TABLES[WORLD]);
    }
    if changed(LESSONS) {
        let store = crate::stores::lessons::hydrate(client).await?;
        out.lessons = Some((store, crate::startup::lesson_rows(client).await?));
        out.stats.reloaded.push(SHADOW_TABLES[LESSONS]);
    }
    if changed(EPISODES) {
        out.episodes = Some(crate::startup::episode_rows(client).await?);
        out.stats.reloaded.push(SHADOW_TABLES[EPISODES]);
    }
    if changed(COUNTERS) {
        out.stats.reloaded.push(SHADOW_TABLES[COUNTERS]);
    }
    // The episode write state derives from the episodes, the identity
    // metadata and the counters row; it is rebuilt whole, as hydration
    // builds it, when any of them changed.
    if changed(COUNTERS) || out.episodes.is_some() || out.metadata.is_some() {
        let row = crate::counters::get(client)
            .await
            .map_err(|e| anyhow::anyhow!("counters row read failed: {e}"))?;
        out.counters = Some(row);
    }
    Ok(Outcome::Partial(Box::new(out)))
}

/// Diff the entries by row version and classify the changed rows.
async fn plan_entries(
    client: &Client,
    memory: &MemoryConfig,
    dim: usize,
    state: &ShadowState,
    bank: &RwLock<Bank>,
) -> Result<EntriesPlan> {
    let current = entry_versions(client).await?;
    let diff = diff(&state.versions, &current);
    if diff.deleted > 0 {
        return Ok(EntriesPlan::Rebuild("entries were deleted", current));
    }
    let (index, max_id, moves) = {
        let bank = bank.read().expect("bank lock");
        let index: HashMap<i64, usize> = bank
            .entries
            .iter()
            .enumerate()
            .map(|(i, e)| (e.id, i))
            .collect();
        let max_id = bank.entries.iter().map(|e| e.id).max();
        (
            index,
            max_id,
            crate::bank::seating_moves(&bank.entries, &memory.band_specs),
        )
    };
    if !diff.changed.is_empty() && moves {
        // Over capacity, the seating spills by retention score, which the
        // telemetry feeds: only a rebuild reproduces it.
        return Ok(EntriesPlan::Rebuild("a band is over capacity", current));
    }
    if !diff.added.is_empty() {
        // One band seats by id alone, so a new highest id lands last, where
        // a rebuild would put it. Several bands seat by stored band first.
        if memory.band_specs.len() != 1 {
            return Ok(EntriesPlan::Rebuild("entries were added", current));
        }
        if max_id.is_some_and(|max| diff.added[0] <= max) {
            return Ok(EntriesPlan::Rebuild(
                "entries were added out of id order",
                current,
            ));
        }
    }
    let mut versions = state.versions.clone();
    let mut patches = Vec::new();
    let mut appends = Vec::new();
    let wanted: Vec<i64> = diff.changed.iter().chain(&diff.added).copied().collect();
    let resident_wanted: HashSet<i64> = diff.changed.iter().copied().collect();
    let sql = format!(
        "SELECT {}, xmin::text::bigint FROM entries WHERE id = ANY($1) ORDER BY id",
        crate::bank::ENTRY_COLUMNS
    );
    let mut seen = HashSet::new();
    let mut fetched = Vec::new();
    for chunk in wanted.chunks(FETCH_CHUNK) {
        let ids = chunk.to_vec();
        let sql = sql.as_str();
        let rows = crate::txn::with_client(client, |c| async move { c.query(sql, &[&ids]).await })
            .await
            .context("changed entry read failed")?;
        // Hydration's own construction, so a new row is built exactly as a
        // rebuild would build it.
        fetched.clear();
        for r in &rows {
            let version: i64 = r.get(19);
            fetched.push((entry_from(r, memory, dim)?, version));
        }
        let resident_bank = bank.read().expect("bank lock");
        for (fresh, version) in fetched.drain(..) {
            let id = fresh.entry.id;
            seen.insert(id);
            if let Some(&i) = index.get(&id) {
                let resident = &resident_bank.entries[i];
                if !same_content(resident, &fresh) {
                    return Ok(EntriesPlan::Rebuild("entry content changed", current));
                }
                let patch = Patch::of(&fresh.entry);
                if patch != Patch::of(resident) {
                    patches.push(patch);
                }
            } else {
                let Ok(row) = fresh.row else {
                    return Ok(EntriesPlan::Rebuild(
                        "an entry vector does not fit",
                        current,
                    ));
                };
                let mut entry = fresh.entry;
                // The single band's seat (see the check above).
                entry.band.clone_from(&memory.band_specs[0].name);
                appends.push((entry, row));
            }
            versions.insert(id, version);
        }
    }
    if resident_wanted.iter().any(|id| !seen.contains(id)) {
        return Ok(EntriesPlan::Rebuild("entries were deleted", current));
    }
    Ok(EntriesPlan::InPlace {
        patches,
        appends,
        versions,
    })
}

fn entry_from(r: &tokio_postgres::Row, memory: &MemoryConfig, dim: usize) -> Result<Hydrated> {
    crate::bank::entry_from_row(r, &memory.bands, true, dim)
}

#[cfg(test)]
#[path = "shadow_tests.rs"]
mod tests;
