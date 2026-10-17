//! Contender resolution (`memory/cortex.py` `CortexStore.resolve`,
//! 1417-1513) for the inputs `memory_fact_resolve` passes: a support tier
//! (always "user" from the tool), never `dream`, no `expect_value`.
//!
//! Not ported because no MCP call reaches them: the `expect_value` check
//! (`refused_changed`, `cortex.py:1477-1479`) and the dream direct-write
//! guard (`refused_direct`, `:1480-1485`) with the dream tier mark
//! (`_mark_tier(..., dream=True)`). `fact_resolve.rs` never passes those
//! inputs.

use super::cortex::{CortexStore, slot_key};
use super::cortex_write::{
    LogAnchor, LogRow, WriteOutcome, add_unique, mark_tier_direct, norm_support,
};

impl CortexStore {
    /// `resolve(entity, attribute, accept, now, support, hlc, dream=False,
    /// expect_value=None)`: settle the slot's active contender. None when
    /// the slot has no `contested` record. An accept on a slot holding
    /// current members is refused (`"refused"`, nothing mutated). Accept
    /// promotes the contender to current (the old current, if any, is
    /// superseded) and answers `"superseded"` with the contender; reject
    /// retires it and answers `"contested"` with the current record, or the
    /// contender when the slot has no current scalar.
    pub fn resolve(
        &mut self,
        entity: &str,
        attribute: &str,
        accept: bool,
        now: f64,
        support: &str,
        hlc: Option<(i64, i64)>,
    ) -> Option<WriteOutcome> {
        let key = slot_key(entity, attribute);
        let c = self.active_contender(&key)?;
        if accept
            && self.members.get(&key).is_some_and(|m| !m.is_empty())
            && !crate::mutants::active("mcp-fact-resolve-no-set-guard")
        {
            return Some(WriteOutcome {
                action: "refused",
                record: c,
            });
        }
        // `self._current.get(key)`, without a status check, as Python reads it.
        let cur = self.current.get(&key).copied();
        let anchor = LogAnchor::of(&self.records[cur.unwrap_or(c)]);
        let (value, confidence) = (self.records[c].value.clone(), self.records[c].confidence);
        if !accept && crate::mutants::active("mcp-fact-resolve-reject-noop") {
            return Some(WriteOutcome {
                action: "contested",
                record: cur.unwrap_or(c),
            });
        }
        self.dirty_slots.insert(key.clone());
        let row = |reason| LogRow {
            new_value: &value,
            new_conf: confidence,
            t: now,
            decision: "resolved",
            reason,
            writer_id: None,
            session_id: None,
        };
        if accept {
            let cur_value = cur.map(|i| {
                let old = &mut self.records[i];
                old.status = "superseded".into();
                old.superseded_at = Some(now);
                old.superseded_by_value = Some(value.clone());
                old.value.clone()
            });
            // An unrecognised tier fails closed to "agent" (`cortex.py:1497`).
            let sup = norm_support(Some(support)).unwrap_or("agent");
            let rec = &mut self.records[c];
            rec.status = "current".into();
            if !crate::mutants::active("mcp-fact-resolve-no-user-tier") {
                mark_tier_direct(rec, sup);
                add_unique(&mut rec.support, sup);
            }
            rec.last_confirmed = now;
            rec.tx_time = Some(now);
            if let Some((phys, logical)) = hlc {
                rec.hlc_phys = Some(phys);
                rec.hlc_logical = Some(logical as i32);
            }
            if cur_value.is_some() {
                rec.supersedes_value = cur_value;
            }
            self.current.insert(key, c);
            self.log_row(&anchor, &row("accepted"));
            return Some(WriteOutcome {
                action: "superseded",
                record: c,
            });
        }
        let rec = &mut self.records[c];
        rec.status = "retired".into();
        rec.superseded_at = Some(now);
        self.log_row(&anchor, &row("rejected"));
        Some(WriteOutcome {
            action: "contested",
            record: cur.unwrap_or(c),
        })
    }
}

#[cfg(test)]
mod tests {
    use crate::stores::cortex::tests::{rec, store};

    #[test]
    fn a_direct_accept_clears_the_contenders_dream_mark() {
        let cur = rec("db", "host", "a");
        let mut c = rec("db", "host", "b");
        c.status = "contested".into();
        c.support = vec!["agent".into(), "user".into()];
        c.provenance = vec!["dream:user".into(), "note".into()];
        let mut s = store(vec![cur, c]);
        let got = s.resolve("db", "host", true, 50.0, "user", None).unwrap();
        assert_eq!((got.action, got.record), ("superseded", 1));
        let r = &s.records[1];
        assert_eq!(r.provenance, ["note"]);
        assert_eq!(r.support, ["agent", "user"]);
        // No tick: the parked stamp stands.
        assert_eq!(r.hlc_phys, None);
        assert_eq!(r.supersedes_value.as_deref(), Some("a"));
        assert_eq!(s.records[0].status, "superseded");
    }

    #[test]
    fn an_unknown_tier_fails_closed_to_agent() {
        let mut c = rec("db", "host", "b");
        c.status = "contested".into();
        c.support = vec![];
        let mut s = store(vec![c]);
        s.resolve("db", "host", true, 50.0, "admin", Some((7, 2)))
            .unwrap();
        assert_eq!(s.records[0].support, ["agent"]);
        assert_eq!(
            (s.records[0].hlc_phys, s.records[0].hlc_logical),
            (Some(7), Some(2))
        );
        assert!(s.resolve("db", "host", false, 51.0, "user", None).is_none());
    }
}
