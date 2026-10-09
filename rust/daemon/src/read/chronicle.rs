//! Chronicle events served beside search hits (spec S10):
//! `service.py:2023-2066`, `cms.py:84-121`, `storage/postgres.py:3333-3361`.

use crate::search::has_temporal_cue;
use serde_json::{Value, json};
use std::sync::OnceLock;
use tokio_postgres::Client;

/// `_AGGREGATION_CUE_RE` (cms.py:94-100).
pub fn has_aggregation_cue(text: &str) -> bool {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    RE.get_or_init(|| {
        regex::Regex::new(
            r"(?i)\b(how many|how much|how often|what percentage|in total|total (?:number|amount|distance|cost|sum|time|money)|altogether|each time|every time|average|the most)\b",
        )
        .unwrap()
    })
    .is_match(text)
}

/// `_DATE_CUE_RE` (cms.py:115): year-first full dates only.
pub fn has_date_cue(text: &str) -> bool {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b").unwrap())
        .is_match(text)
}

/// The `events` block and, under an aggregation cue, `events_total`.
pub struct Events {
    pub events: Vec<Value>,
    pub total: Option<usize>,
}

/// `PostgresStorage.chronicle_search` plus the service's cue gate and
/// projection. `None` when no cue fires or nothing matches.
pub async fn events_for(db: &Client, query: &str) -> anyhow::Result<Option<Events>> {
    let agg = has_aggregation_cue(query);
    if !(agg || has_temporal_cue(query) || has_date_cue(query)) {
        return Ok(None);
    }
    let limit: i64 = if agg { 30 } else { 6 };
    let lex: Option<String> = db
        .query_one("SELECT plainto_tsquery('english', $1)::text", &[&query])
        .await?
        .get(0);
    let Some(lex) = lex.filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    let rows = db
        .query(
            "SELECT id, to_char(occurred_at, 'YYYY-MM-DD'), occurred_phrase, \
             recorded_at, actor, description, episode, src_entry_id \
             FROM chronicle_events \
             WHERE invalidated_at IS NULL AND \
             to_tsvector('english', description) @@ to_tsquery('simple', $1) \
             ORDER BY occurred_at ASC NULLS LAST, recorded_at ASC LIMIT $2",
            &[&lex.replace(" & ", " | "), &limit],
        )
        .await?;
    if rows.is_empty() {
        return Ok(None);
    }
    let events: Vec<Value> = rows
        .iter()
        .map(|r| {
            json!({
                "description": r.get::<_, Option<String>>(5),
                "actor": r.get::<_, Option<String>>(4),
                "date": r.get::<_, Option<String>>(1),
                "phrase": r.get::<_, Option<String>>(2),
            })
        })
        .collect();
    let total = agg.then_some(events.len());
    Ok(Some(Events { events, total }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cues_match_python() {
        assert!(has_aggregation_cue("How many times did it fail"));
        assert!(has_aggregation_cue("the total cost of it"));
        assert!(!has_aggregation_cue("the total"));
        assert!(!has_aggregation_cue("somehow manyfold"));
        assert!(has_date_cue("what happened on 2026-08-08?"));
        assert!(has_date_cue("on 2026/8/8"));
        assert!(!has_date_cue("issue 08-08"));
        assert!(!has_date_cue("0412-345-678"));
    }
}
