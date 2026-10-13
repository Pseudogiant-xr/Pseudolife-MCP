//! The board's hybrid logical clock (`memory/hlc.py`), seeded as
//! `MemoryService._reseed_hlc` seeds the service clock (ruling a,
//! 2026-10-09): the highest stamp among canonical facts, world facts and
//! lessons, and the board's own high-water mark. Replaced by W2-E's shared
//! clock when that lands.

use super::error::{BoardError, Result};
use super::mail::HLC_META_KEY;
use serde_json::Value;

#[derive(Default)]
pub struct Hlc {
    phys: i64,
    logical: i64,
    pub seeded: bool,
}

fn wall_ms() -> i64 {
    (super::store::now() * 1000.0) as i64
}

impl Hlc {
    /// `tick`: monotonic whatever the wall clock does.
    pub fn tick(&mut self) -> (i64, i64) {
        let now = wall_ms();
        if now > self.phys {
            self.phys = now;
            self.logical = 0;
        } else {
            self.logical += 1;
        }
        (self.phys, self.logical)
    }

    /// `observe`: advance past a stamp seen on read.
    pub fn observe(&mut self, phys: i64, logical: i64) {
        if phys > self.phys {
            self.phys = phys;
            self.logical = logical;
        } else if phys == self.phys {
            self.logical = self.logical.max(logical);
        }
    }
}

/// The best stored stamp, as `_reseed_hlc` finds it.
pub async fn stored_high_water(c: &tokio_postgres::Client) -> Result<(i64, i64)> {
    crate::txn::with_client(c, |c| async move { stored_high_water_inner(c).await }).await
}

async fn stored_high_water_inner(c: &tokio_postgres::Client) -> Result<(i64, i64)> {
    let mut best = (0i64, 0i64);
    for table in ["facts", "world_facts", "lessons"] {
        let sql = format!(
            "SELECT hlc_phys, coalesce(hlc_logical,0)::bigint FROM {table} \
             WHERE hlc_phys IS NOT NULL AND hlc_phys <> 0 \
             ORDER BY hlc_phys DESC, hlc_logical DESC NULLS LAST LIMIT 1"
        );
        if let Some(row) = c.query_opt(&sql, &[]).await? {
            let cand: (i64, i64) = (row.get(0), row.get(1));
            if cand > best {
                best = cand;
            }
        }
    }
    let stamp = c
        .query_opt("SELECT value FROM meta WHERE key=$1", &[&HLC_META_KEY])
        .await?;
    if let Some(row) = stamp {
        let v: Value = row.get(0);
        let parts = v.as_array().filter(|a| a.len() == 2).and_then(|a| {
            let p = super::canon::py_int_i64(&a[0])?;
            let l = super::canon::py_int_i64(&a[1])?;
            (p >= 0 && l >= 0).then_some((p, l))
        });
        let Some(stamp) = parts else {
            return Err(BoardError::Internal(
                "invalid coordination clock high-water mark".into(),
            ));
        };
        best = best.max(stamp);
    }
    Ok(best)
}
