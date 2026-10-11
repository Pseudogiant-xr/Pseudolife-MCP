//! `verify_audit_chain` (storage/coordination.py) over archive or bank rows.
//!
//! Rows arrive one at a time, so a break is reported before a later
//! unreadable archive line is reached, as the Python generator walk does.
//! `Err(Deferred)` means the row is outside the reproduced domain.
use super::codec;
use crate::sent_json::Json;
use codec::J;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};

const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";

#[derive(Debug)]
pub(super) enum Failure {
    Deferred,
    Python,
}
pub(super) use Failure::Deferred;

pub(super) struct Row {
    /// The exported row: columns by name, `payload` parsed (archive) or the
    /// stored text (bank), `body`/`body_salt` absent only in a pre-v46 export.
    pub value: Value,
    pub seq: i64,
    pub created_at: f64,
}

struct Cut {
    seq: i64,
    created_at: f64,
    cutoff: J,
    retention_days: i64,
    through_seq: i64,
    actor: String,
}

struct Edge {
    seq: i64,
    prev_hash: String,
    hash: String,
    created_at: f64,
}

pub(super) struct Walker {
    expect: Option<super::args::Head>,
    first: Option<Edge>,
    prev: Option<Edge>,
    count: u64,
    cuts: HashMap<(i64, String), Cut>,
    expected: Option<String>,
    /// seq -> (message_id, whether the row had a `body` field at all)
    absent: BTreeMap<i64, (Value, bool)>,
    kept: HashSet<i64>,
}

fn integer(value: impl ToString) -> Json {
    Json::Integer(value.to_string())
}

pub(super) fn broken(seq: impl ToString, reason: &str) -> Json {
    Json::Object(vec![
        ("ok".into(), Json::Bool(false)),
        ("seq".into(), integer(seq)),
        ("reason".into(), Json::String(reason.into())),
    ])
}

/// `type(value) is int`; large Python ints can name no i64 chain row.
fn python_int(value: &J) -> Option<Option<i64>> {
    if let J::Int(token) = value {
        Some(token.parse().ok())
    } else {
        None
    }
}

fn parsed_payload(row: &Value) -> Result<Option<codec::Payload>, Failure> {
    codec::parsed_payload(row).map_err(|_| Failure::Python)
}

/// `body_commitment(salt, body) == payload["text_commitment"]`.
fn body_matches(body: &str, salt: &Value, payload: Option<&codec::Payload>) -> bool {
    let Some(commitment) = payload.and_then(|p| p.get("text_commitment")?.as_str()) else {
        return false;
    };
    let Some(salt) = salt.as_str() else {
        return false;
    };
    if salt.len() != 32 || !salt.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f')) {
        return false;
    }
    let mut digest = Sha256::new();
    for pair in salt.as_bytes().chunks(2) {
        let text = std::str::from_utf8(pair).expect("ASCII hex");
        digest.update([u8::from_str_radix(text, 16).expect("hex pair")]);
    }
    digest.update(body.as_bytes());
    format!("{:x}", digest.finalize()) == commitment
}

/// `_cut`: an `audit_prune` row's fields when shaped as prune writes them.
fn cut(row: &Row) -> Result<Option<(i64, String, Cut)>, Failure> {
    let Some(map) = codec::parsed_payload(&row.value).map_err(|_| Failure::Python)? else {
        return Ok(None);
    };
    let field = |name: &str| map.get(name).unwrap_or(&J::Null);
    let (Some(through_seq), Some(through_hash)) = (
        python_int(field("through_seq")),
        field("through_hash").as_str(),
    ) else {
        return Ok(None);
    };
    let cutoff = field("cutoff");
    let Some(days) = python_int(field("retention_days")) else {
        return Ok(None);
    };
    if !matches!(cutoff, J::Int(_) | J::Float(_)) || !days.is_some_and(|d| (0..=36500).contains(&d))
    {
        return Ok(None);
    }
    // A through_seq beyond i64 can never be first_seq - 1 of a read chain.
    let Some(through_seq) = through_seq else {
        return Ok(None);
    };
    Ok(Some((
        through_seq,
        through_hash.to_owned(),
        Cut {
            seq: row.seq,
            created_at: row.created_at,
            cutoff: cutoff.clone(),
            retention_days: days.expect("checked"),
            through_seq,
            actor: row.value["actor"].as_str().unwrap_or_default().to_owned(),
        },
    )))
}

/// `audit_cutoff(now, days)`, exactly (Python's floor is an unbounded int).
fn audit_cutoff(now: f64, days: i64) -> Result<i128, Failure> {
    let quotient = ((now - (days * 86400) as f64) / 86400.0).floor();
    if !quotient.is_finite() || quotient.abs() > 1e30 {
        return Err(Deferred);
    }
    Ok(quotient as i128 * 86400)
}

/// Python `number == integer` for an int or float JSON number.
fn equals_integer(value: &J, integer: i128) -> Result<bool, Failure> {
    match value {
        J::Int(token) => Ok(token.parse::<i128>().is_ok_and(|v| v == integer)),
        J::Float(number) => Ok(number.is_finite()
            && number.fract() == 0.0
            && number.abs() < 1e30
            && *number as i128 == integer),
        _ => Err(Deferred),
    }
}

fn cutoff_json(value: &J) -> Result<Json, Failure> {
    match value {
        J::Int(token) => Ok(Json::Integer(token.clone())),
        J::Float(number) => Ok(Json::Float(*number)),
        _ => Err(Deferred),
    }
}

impl Walker {
    pub(super) fn new(expect: Option<super::args::Head>) -> Self {
        Self {
            expect,
            first: None,
            prev: None,
            count: 0,
            cuts: HashMap::new(),
            expected: None,
            absent: BTreeMap::new(),
            kept: HashSet::new(),
        }
    }

    /// One row in chain order; `Ok(Some(report))` is the first break.
    pub(super) fn push(&mut self, row: Row) -> Result<Option<Json>, Failure> {
        let value = &row.value;
        let text = |key: &str| value[key].as_str().unwrap_or_default().to_owned();
        let seq = row.seq;
        let prev_hash = text("prev_hash");
        match &self.prev {
            None => {
                if seq == 1 && prev_hash != GENESIS {
                    return Ok(Some(broken(seq, "broken_link")));
                }
            }
            Some(prev) => {
                if prev.seq.checked_add(1) != Some(seq) {
                    return Ok(Some(broken(seq, "sequence_gap")));
                }
                if prev_hash != prev.hash {
                    return Ok(Some(broken(seq, "broken_link")));
                }
            }
        }
        let hash = text("hash");
        if codec::hash(value, row.created_at).map_err(|_| Deferred)? != hash {
            return Ok(Some(broken(seq, "hash_mismatch")));
        }
        let event = text("event");
        let body = value.get("body").unwrap_or(&Value::Null);
        let salt = value.get("body_salt").unwrap_or(&Value::Null);
        let payload = if !body.is_null() || event == "send" || event == "redact" {
            parsed_payload(value)?
        } else {
            None
        };
        if body.is_null() && !salt.is_null() {
            return Ok(Some(broken(seq, "body_mismatch")));
        }
        if let Some(body) = body.as_str() {
            let committed = if event == "send" {
                payload.as_ref()
            } else {
                None
            };
            if !body_matches(body, salt, committed) {
                return Ok(Some(broken(seq, "body_mismatch")));
            }
            self.kept.insert(seq);
        } else if event == "send"
            && payload
                .as_ref()
                .is_some_and(|p| p.contains_key("text_commitment"))
        {
            self.absent.insert(
                seq,
                (
                    value.get("message_id").cloned().unwrap_or(Value::Null),
                    value.get("body").is_some(),
                ),
            );
        }
        if event == "redact"
            && value["actor"] == "operator"
            && let Some(payload) = &payload
            // An int beyond i64 names no row this walker read: nothing to do.
            && let Some(Some(named)) = python_int(payload.get("seq").unwrap_or(&J::Null))
        {
            if self.kept.contains(&named) {
                return Ok(Some(broken(named, "body_mismatch")));
            }
            let message_id = payload.get("message_id").and_then(J::as_str);
            if payload.get("audit_copy").and_then(J::as_str) == Some("removed")
                && let Some(message_id) = message_id
                && self
                    .absent
                    .get(&named)
                    .is_some_and(|(id, _)| id.as_str() == Some(message_id))
            {
                self.absent.remove(&named);
            }
        }
        if event == "audit_prune"
            && let Some((through_seq, through_hash, found)) = cut(&row)?
        {
            self.cuts.insert((through_seq, through_hash), found);
        }
        if self
            .expect
            .as_ref()
            .is_some_and(|(want, _)| want == &seq.to_string())
        {
            self.expected = Some(hash.clone());
        }
        let edge = Edge {
            seq,
            prev_hash,
            hash,
            created_at: row.created_at,
        };
        if self.first.is_none() {
            self.first = Some(Edge {
                seq: edge.seq,
                prev_hash: edge.prev_hash.clone(),
                hash: edge.hash.clone(),
                created_at: edge.created_at,
            });
        }
        self.prev = Some(edge);
        self.count += 1;
        Ok(None)
    }

    /// The report once every row was read, and whether the chain is intact.
    pub(super) fn finish(self) -> Result<(Json, bool), Failure> {
        let ok = self.ok_report()?;
        let intact = matches!(&ok, Json::Object(fields)
            if matches!(fields.first(), Some((_, Json::Bool(true)))));
        Ok((ok, intact))
    }

    fn ok_report(self) -> Result<Json, Failure> {
        let mut start_cut = Json::Null;
        if let Some(first) = self.first.as_ref().filter(|first| first.seq != 1) {
            let found = first
                .seq
                .checked_sub(1)
                .and_then(|through| self.cuts.get(&(through, first.prev_hash.clone())));
            let anchored = match found {
                None => false,
                Some(cut) if cut.actor != "daemon" || cut.retention_days < 1 => false,
                Some(cut) => {
                    let cutoff = audit_cutoff(cut.created_at, cut.retention_days)?;
                    if !equals_integer(&cut.cutoff, cutoff)? {
                        false
                    } else {
                        // float(first created_at) < cutoff, exactly.
                        let floor = first.created_at.floor();
                        if floor.abs() > 1e30 {
                            return Err(Deferred);
                        }
                        (floor as i128) >= cutoff
                    }
                }
            };
            if !anchored {
                return Ok(broken(first.seq, "unanchored_start"));
            }
            let cut = found.expect("anchored cut");
            start_cut = Json::Object(vec![
                ("seq".into(), integer(cut.seq)),
                ("created_at".into(), Json::Float(cut.created_at)),
                ("cutoff".into(), cutoff_json(&cut.cutoff)?),
                ("retention_days".into(), integer(cut.retention_days)),
                ("through_seq".into(), integer(cut.through_seq)),
            ]);
        }
        if let Some((seq, (_, had_field))) = self.absent.iter().next() {
            let reason = if *had_field {
                "body_missing"
            } else {
                "body_not_exported"
            };
            return Ok(broken(*seq, reason));
        }
        if let Some((seq, digest)) = &self.expect {
            match &self.expected {
                None => {
                    if self
                        .first
                        .as_ref()
                        .is_some_and(|first| seq.parse::<i64>().is_ok_and(|seq| seq < first.seq))
                    {
                        let Json::Object(mut fields) = broken(seq, "head_pruned") else {
                            unreachable!("broken is an object")
                        };
                        fields.push(("start_cut".into(), start_cut));
                        return Ok(Json::Object(fields));
                    }
                    return Ok(broken(seq, "head_missing"));
                }
                Some(expected) if expected != digest => {
                    return Ok(broken(seq, "head_mismatch"));
                }
                Some(_) => {}
            }
        }
        let head = |pick: fn(&Edge) -> Json| self.prev.as_ref().map_or(Json::Null, pick);
        Ok(Json::Object(vec![
            ("ok".into(), Json::Bool(true)),
            ("events".into(), integer(self.count)),
            (
                "first_seq".into(),
                self.first
                    .as_ref()
                    .map_or(Json::Null, |first| integer(first.seq)),
            ),
            ("head_seq".into(), head(|edge| integer(edge.seq))),
            (
                "head_hash".into(),
                head(|edge| Json::String(edge.hash.clone())),
            ),
            (
                "head_created_at".into(),
                head(|edge| Json::Float(edge.created_at)),
            ),
            ("start_cut".into(), start_cut),
        ]))
    }
}
