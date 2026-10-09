//! Sweep maintenance policy (`memory/compaction.py`, `storage/postgres.py`).

use std::collections::{HashMap, HashSet};
use tokio_postgres::Client;

pub struct CompactRecord<'a> {
    pub entity_norm: &'a str,
    pub attribute_norm: &'a str,
    pub status: &'a str,
    pub superseded_at: Option<f64>,
    pub asserted_at: f64,
}

/// Returns resident ordinals to remove. The canonical-store owner rebuilds
/// its current index, marks affected slots dirty and persists through its
/// existing slot-rewrite API; direct DELETE would lose Python's row-ID and
/// sequence behavior.
#[allow(dead_code)] // W2-E's mutable canonical stores consume this policy.
pub fn compaction_victims(
    records: &[CompactRecord<'_>],
    keep: i64,
    min_age_days: f64,
    now: f64,
) -> Vec<usize> {
    let cutoff = now - min_age_days.max(0.0) * 86400.0;
    type Slot<'a> = (&'a str, &'a str);
    type SortKey = (f64, f64, usize);
    let mut pools: HashMap<Slot<'_>, Vec<SortKey>> = HashMap::new();
    for (i, record) in records.iter().enumerate() {
        if crate::mutants::active("compact-skip-removed") && record.status == "removed" {
            continue;
        }
        if matches!(record.status, "superseded" | "retired" | "removed") {
            pools
                .entry((record.entity_norm, record.attribute_norm))
                .or_default()
                .push((record.superseded_at.unwrap_or(0.0), record.asserted_at, i));
        }
    }
    let mut victims = HashSet::new();
    for pool in pools.values_mut() {
        pool.sort_by(|a, b| {
            b.0.total_cmp(&a.0)
                .then_with(|| b.1.total_cmp(&a.1))
                .then_with(|| {
                    if crate::mutants::active("compact-reverse-ordinal") {
                        a.2.cmp(&b.2)
                    } else {
                        b.2.cmp(&a.2)
                    }
                })
        });
        for (sup, _, i) in pool.iter().skip(keep.max(0) as usize) {
            if *sup < cutoff {
                victims.insert(*i);
            }
        }
    }
    (0..records.len()).filter(|i| victims.contains(i)).collect()
}

/// Caller owns the shared writer's service lock and completes this future
/// before shutdown; no request cancellation may drop an open transaction.
pub async fn prune_runs(client: &Client, keep: i64, now: f64) -> Result<u64, String> {
    client
        .batch_execute("BEGIN")
        .await
        .map_err(|e| e.to_string())?;
    let result = async {
        let cutoff = if crate::mutants::active("runs-no-stale-failure") { f64::MIN } else { now - 86400.0 };
        client.execute("UPDATE dream_runs SET status='failed' WHERE status='running' AND started_at < $1", &[&cutoff]).await?;
        client.execute("DELETE FROM dream_runs WHERE id NOT IN (SELECT id FROM dream_runs ORDER BY id DESC LIMIT $1)", &[&keep.max(0)]).await
    }.await;
    match result {
        Ok(count) => {
            client
                .batch_execute("COMMIT")
                .await
                .map_err(|e| e.to_string())?;
            Ok(count)
        }
        Err(e) => {
            let _ = client.batch_execute("ROLLBACK").await;
            Err(e.to_string())
        }
    }
}

/// Each DELETE is a separate PostgreSQL transaction, like the oracle.
pub async fn prune_retrieval(client: &Client, cutoff: f64) -> Result<u64, String> {
    let cutoff = if crate::mutants::active("retrieval-no-prune") {
        f64::MIN
    } else {
        cutoff
    };
    let events = client
        .execute(
            "DELETE FROM retrieval_events WHERE created_at < $1",
            &[&cutoff],
        )
        .await
        .map_err(|e| e.to_string())?;
    let lessons = client
        .execute(
            "DELETE FROM lesson_search_events WHERE created_at < $1",
            &[&cutoff],
        )
        .await
        .map_err(|e| e.to_string())?;
    Ok(events + lessons)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn live_python_compaction_goldens() {
        let golden: serde_json::Value =
            serde_json::from_str(include_str!("../harness/goldens/maintenance-policy.json"))
                .unwrap();
        for case in golden["compaction"].as_array().unwrap() {
            let rows: Vec<_> = case["rows"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| CompactRecord {
                    entity_norm: r[0].as_str().unwrap(),
                    attribute_norm: r[1].as_str().unwrap(),
                    status: r[2].as_str().unwrap(),
                    superseded_at: r[3].as_f64(),
                    asserted_at: r[4].as_f64().unwrap(),
                })
                .collect();
            let victims = compaction_victims(
                &rows,
                case["keep"].as_i64().unwrap(),
                case["min_age_days"].as_f64().unwrap(),
                case["now"].as_f64().unwrap(),
            );
            assert_eq!(serde_json::json!(victims), case["victims"]);
        }
    }
    #[test]
    fn compaction_keeps_live_rows_newest_and_exact_cutoff() {
        let rows: Vec<_> = [
            ("current", Some(1.0), 1.0),
            ("contested", Some(1.0), 1.0),
            ("superseded", Some(10.0), 1.0),
            ("retired", Some(10.0), 2.0),
            ("removed", Some(10.0), 2.0),
            ("superseded", Some(20.0), 0.0),
        ]
        .iter()
        .map(|(status, sup, asserted)| CompactRecord {
            entity_norm: "e",
            attribute_norm: "a",
            status,
            superseded_at: *sup,
            asserted_at: *asserted,
        })
        .collect();
        assert_eq!(compaction_victims(&rows, 2, 0.0, 15.0), vec![2, 3]);
        assert_eq!(compaction_victims(&rows, 0, 0.0, 10.0), Vec::<usize>::new());
        assert_eq!(compaction_victims(&rows, -1, -1.0, 15.0), vec![2, 3, 4]);
    }
}
