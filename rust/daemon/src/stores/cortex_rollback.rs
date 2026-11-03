//! The cortex store's dream-rollback helpers (`memory/cortex.py`
//! `records_for` 1526, `retire_current` 1137-1160,
//! `restore_settled_contender` 1363-1415): the slot's full history, the
//! reverse of an `inserted` scalar write, and the re-parking of a contender
//! a reverted write had settled.

use super::cortex::{CortexStore, norm_key, slot_key};
use super::cortex_write::{LogAnchor, LogRow, norm_value};

impl CortexStore {
    /// `records_for(entity, attribute)`: every record at the slot, any
    /// status, in record order.
    pub fn records_for(&self, entity: &str, attribute: &str) -> Vec<usize> {
        let key = slot_key(entity, attribute);
        (0..self.records.len())
            .filter(|&i| self.records[i].key == key)
            .collect()
    }

    /// `retire_current(entity, attribute, now=now)`: the slot's current
    /// scalar becomes `retired` (its row kept) so the slot reads absent
    /// again; None when it has no current scalar. Members are never touched.
    pub fn retire_current(&mut self, entity: &str, attribute: &str, now: f64) -> Option<usize> {
        let key = (norm_key(entity), norm_key(attribute));
        let i = *self.current.get(&key)?;
        if self.records[i].status != "current" || self.records[i].kind != "scalar" {
            return None;
        }
        let r = &mut self.records[i];
        r.status = "retired".into();
        r.superseded_at = Some(now);
        self.current.remove(&key);
        self.dirty_slots.insert(key);
        let anchor = LogAnchor::of(&self.records[i]);
        let row = LogRow {
            new_value: anchor.value.clone(),
            new_conf: anchor.confidence,
            t: now,
            decision: "retired",
            reason: "rollback",
            writer_id: None,
            session_id: None,
        };
        self.log_row(&anchor, &row);
        Some(i)
    }

    /// `restore_settled_contender(entity, attribute, value, since, until,
    /// now)`: re-park the contender a write of `value` inside `[since,
    /// until]` settled; None when there is nothing to restore (no such
    /// record, a contender already parked, or `value` current again at the
    /// slot, scalar or member).
    pub fn restore_settled_contender(
        &mut self,
        entity: &str,
        attribute: &str,
        value: &str,
        since: f64,
        until: Option<f64>,
        now: f64,
    ) -> Option<usize> {
        let key = (norm_key(entity), norm_key(attribute));
        let want = norm_value(value);
        if self.active_contender(&key).is_some() {
            return None;
        }
        let cur = self
            .current
            .get(&key)
            .copied()
            .filter(|&i| self.records[i].status == "current");
        if cur.is_some_and(|i| norm_value(&self.records[i].value) == want) {
            return None;
        }
        if self
            .members(entity, attribute)
            .iter()
            .any(|m| norm_value(&m.value) == want)
        {
            return None;
        }
        let at_slot: Vec<usize> = (0..self.records.len())
            .filter(|&i| self.records[i].key == key && norm_value(&self.records[i].value) == want)
            .collect();
        let mut best: Option<usize> = None;
        for &i in &at_slot {
            let r = &self.records[i];
            let Some(at) = r.superseded_at else {
                continue;
            };
            let settled = r.status == "superseded"
                && norm_value(r.superseded_by_value.as_deref().unwrap_or("")) == want
                && at >= since
                && until.is_none_or(|u| at <= u)
                && at_slot
                    .iter()
                    .any(|&o| o != i && self.records[o].asserted_at == at);
            // `max(..., key=superseded_at)`: the first of equal maxima.
            if settled && best.is_none_or(|b| at > self.records[b].superseded_at.unwrap_or(0.0)) {
                best = Some(i);
            }
        }
        let i = best?;
        let r = &mut self.records[i];
        r.status = "contested".into();
        r.superseded_at = None;
        r.superseded_by_value = None;
        r.raw.remove("superseded_by_value");
        let list = self.contested.entry(key.clone()).or_default();
        if !list.contains(&i) {
            list.push(i);
            list.sort_unstable();
        }
        self.dirty_slots.insert(key);
        let (value, confidence) = (self.records[i].value.clone(), self.records[i].confidence);
        let anchor = LogAnchor::of(&self.records[cur.unwrap_or(i)]);
        let row = LogRow {
            new_value: value.into(),
            new_conf: confidence,
            t: now,
            decision: "contested",
            reason: "rollback_restored_contender",
            writer_id: None,
            session_id: None,
        };
        self.log_row(&anchor, &row);
        Some(i)
    }
}
