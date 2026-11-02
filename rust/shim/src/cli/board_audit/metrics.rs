//! Pure report calculation, following `board_audit_stats.py`.
use serde_json::{Value as V, json};
use std::collections::{BTreeMap, HashMap};

const DECISIONS: [&str; 8] = [
    "rung",
    "nudged",
    "hinted",
    "withheld",
    "no_path",
    "capped",
    "not_needed",
    "unknown",
];
pub(super) fn number(v: &V) -> Option<f64> {
    v.as_f64().filter(|v| v.is_finite())
}
fn text<'a>(v: &'a V, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
fn truth(v: &V) -> bool {
    match v {
        V::Null => false,
        V::Bool(b) => *b,
        V::String(s) => !s.is_empty(),
        V::Array(a) => !a.is_empty(),
        V::Object(o) => !o.is_empty(),
        _ => number(v) != Some(0.0),
    }
}
// Formatting uses the exact binary float with decimal half-even rounding,
// as CPython's round(x, ndigits), without an intermediate scaled multiply.
fn rounded(value: f64, digits: usize) -> f64 {
    format!("{value:.digits$}").parse().unwrap_or(value)
}
fn share(part: usize, whole: usize) -> V {
    if whole == 0 {
        V::Null
    } else {
        json!(rounded(part as f64 / whole as f64, 3))
    }
}
fn percentiles(values: Vec<f64>) -> V {
    numeric_percentiles(values.into_iter().map(|v| json!(v)).collect())
}
fn numeric_percentiles(mut values: Vec<V>) -> V {
    values.sort_by(|a, b| number(a).unwrap().partial_cmp(&number(b).unwrap()).unwrap());
    let n = values.len();
    if n == 0 {
        return json!({"n":0,"p50":null,"p95":null});
    }
    let rank = |i: usize| {
        if values[i].as_i64().is_some() || values[i].as_u64().is_some() {
            values[i].clone()
        } else {
            json!(rounded(number(&values[i]).unwrap(), 1))
        }
    };
    json!({"n":n,"p50":rank((n-1)/2),"p95":rank((95*n).div_ceil(100)-1)})
}
fn latency_summary(values: &[Option<V>]) -> V {
    let read: Vec<_> = values.iter().flatten().cloned().collect();
    json!({"sends":values.len(),"read":read.len(),"unread":values.len()-read.len(),"seconds":numeric_percentiles(read)})
}

fn difference(a: &V, b: &V) -> V {
    if let (Some(a), Some(b)) = (a.as_i64(), b.as_i64()) {
        if let Some(difference) = a.checked_sub(b) {
            return json!(difference);
        }
    }
    json!(number(a).unwrap() - number(b).unwrap())
}

pub(super) fn compute(
    mut events: Vec<V>,
    wakes: Vec<V>,
    durations: Vec<V>,
    since: f64,
    until: f64,
    now: f64,
    sources: V,
) -> V {
    events.retain(|e| number(&e["created_at"]).is_some_and(|t| t < until));
    events.sort_by(|a, b| {
        number(&a["created_at"])
            .unwrap()
            .total_cmp(&number(&b["created_at"]).unwrap())
            .then_with(|| {
                a["seq"]
                    .as_i64()
                    .unwrap_or(0)
                    .cmp(&b["seq"].as_i64().unwrap_or(0))
            })
    });
    let mut principals = HashMap::new();
    for event in &events {
        if !text(event, "agent_id").is_empty() && !text(event, "principal").is_empty() {
            principals
                .entry(text(event, "agent_id"))
                .or_insert(text(event, "principal"));
        }
    }
    let window: Vec<_> = events
        .iter()
        .filter(|e| number(&e["created_at"]).unwrap() >= since)
        .collect();
    let mut by_kind = BTreeMap::new();
    for e in &window {
        *by_kind.entry(text(e, "event")).or_insert(0) += 1;
    }
    let mut first_read = HashMap::new();
    for e in &window {
        if text(e, "event") == "read" && truth(&e["message_id"]) {
            first_read
                .entry(text(e, "message_id"))
                .or_insert(&e["created_at"]);
        }
    }
    let mut decisions: BTreeMap<&str, Vec<Option<V>>> =
        DECISIONS.into_iter().map(|d| (d, Vec::new())).collect();
    let mut recipients: BTreeMap<&str, Vec<Option<V>>> = BTreeMap::new();
    for e in &window {
        if text(e, "event") != "send" {
            continue;
        }
        let latency = first_read
            .get(text(e, "message_id"))
            .map(|t| difference(t, &e["created_at"]));
        let decision = text(&e["payload"], "wake");
        let decision = if decisions.contains_key(decision) {
            decision
        } else {
            "unknown"
        };
        decisions.get_mut(decision).unwrap().push(latency.clone());
        recipients
            .entry(
                principals
                    .get(text(e, "recipient_agent_id"))
                    .copied()
                    .unwrap_or("unknown"),
            )
            .or_default()
            .push(latency);
    }
    let mut mail = latency_summary(
        &DECISIONS
            .iter()
            .flat_map(|d| decisions[d].iter().cloned())
            .collect::<Vec<_>>(),
    );
    mail["by_decision"] = DECISIONS
        .into_iter()
        .map(|d| (d.to_owned(), latency_summary(&decisions[d])))
        .collect();
    mail["by_principal"] = recipients
        .iter()
        .map(|(d, v)| ((*d).to_owned(), latency_summary(v)))
        .collect();
    json!({"report":"board-audit stats","shape":1,"version":env!("CARGO_PKG_VERSION"),
        "generated_at":rounded(now,3),"window":{"since":since,"until":until,"hours":rounded((until-since)/3600.0,3)},
        "sources":sources,"events":{"total":window.len(),"by_kind":by_kind},"mail_latency":mail,
        "wake_precision":wake_precision(&wakes,&window,since,until),
        "park_outcomes":parks(&events,since,until),
        "sends_per_session_hour":sessions(&window,&principals,since,until),
        "suite_lock":suite_times(&durations,since,until)})
}

#[derive(Default)]
struct Ring {
    served: Option<f64>,
    woke: Option<f64>,
    acted_served: bool,
    acted_woke: bool,
}
fn ring_summary(rows: &[&Ring]) -> V {
    let served: Vec<_> = rows.iter().filter(|r| r.served.is_some()).collect();
    let woke: Vec<_> = rows.iter().filter(|r| r.woke.is_some()).collect();
    let acted_served = served.iter().filter(|r| r.acted_served).count();
    let acted_woke = woke.iter().filter(|r| r.acted_woke).count();
    json!({"rings":rows.len(),"served":served.len(),"never_served":rows.len()-served.len(),
        "share_never_served":share(rows.len()-served.len(),rows.len()),
        "seconds_to_served":percentiles(served.iter().filter_map(|r| r.served).collect()),
        "acted_after_served":acted_served,"share_acted_after_served":share(acted_served,served.len()),
        "woke":woke.len(),"share_woke":share(woke.len(),served.len()),
        "seconds_served_to_woke":percentiles(woke.iter().filter_map(|r| r.woke).collect()),
        "acted_after_woke":acted_woke,"share_acted_after_woke":share(acted_woke,woke.len())})
}
fn wake_precision(wakes: &[V], events: &[&V], since: f64, until: f64) -> V {
    let mut groups: BTreeMap<&str, Vec<Ring>> = ["rung", "nudged"]
        .into_iter()
        .map(|s| (s, Vec::new()))
        .collect();
    for wake in wakes {
        let Some(created) = number(&wake["created_at"]).filter(|t| since <= *t && *t < until)
        else {
            continue;
        };
        let Some(group) = groups.get_mut(text(wake, "decision")) else {
            continue;
        };
        let served = number(&wake["served_at"]).filter(|t| *t < until);
        let agent = text(wake, "recipient_agent_id");
        let woke = served.and_then(|s| {
            events
                .iter()
                .filter(|e| text(e, "event") == "woke" && text(e, "agent_id") == agent)
                .filter_map(|e| number(&e["created_at"]).filter(|t| s <= *t && *t <= s + 3600.0))
                .min_by(f64::total_cmp)
        });
        let acted = |anchor: Option<f64>| {
            anchor.is_some_and(|a| {
                events.iter().any(|e| {
                    let t = number(&e["created_at"]).unwrap();
                    text(e, "agent_id") == agent
                        && !agent.is_empty()
                        && a < t
                        && t <= a + 120.0
                        && matches!(
                            text(e, "event"),
                            "update" | "read" | "ack" | "send" | "register"
                        )
                        && e.get("actor").and_then(V::as_str).unwrap_or("agent") == "agent"
                        && !(text(e, "event") == "read"
                            && text(&e["payload"], "path") == "delivery")
                })
            })
        };
        group.push(Ring {
            served: served.map(|s| s - created),
            woke: woke.zip(served).map(|(w, s)| w - s),
            acted_served: acted(served),
            acted_woke: acted(woke),
        });
    }
    let mut result = ring_summary(
        &["rung", "nudged"]
            .iter()
            .flat_map(|d| groups[d].iter())
            .collect::<Vec<_>>(),
    );
    result["by_decision"] = ["rung", "nudged"]
        .into_iter()
        .map(|k| {
            (
                k.to_owned(),
                ring_summary(&groups[k].iter().collect::<Vec<_>>()),
            )
        })
        .collect();
    result
}

struct Park {
    agent: String,
    reason: String,
    set: f64,
    expires: Option<f64>,
    ended: Option<f64>,
    set_value: V,
    ended_value: V,
    how: Option<&'static str>,
}
fn parks(events: &[V], since: f64, until: f64) -> V {
    let mut all: Vec<Park> = Vec::new();
    let mut counted = Vec::new();
    let mut standing: HashMap<String, usize> = HashMap::new();
    let mut lapsed: HashMap<String, (String, Option<f64>)> = HashMap::new();
    let mut rings: HashMap<String, Vec<f64>> = HashMap::new();
    for event in events {
        let t = number(&event["created_at"]).unwrap();
        if text(event, "event") == "send" && text(&event["payload"], "wake") == "rung" {
            rings
                .entry(text(event, "recipient_agent_id").into())
                .or_default()
                .push(t);
        }
        if text(event, "event") != "update" {
            continue;
        }
        let fields = &event["payload"]["fields"];
        let before = &event["payload"]["before"];
        if !fields
            .as_object()
            .is_some_and(|f| f.keys().any(|k| k.starts_with("park_")))
        {
            continue;
        }
        let agent = text(event, "agent_id").to_owned();
        if let Some(&i) = standing.get(&agent) {
            let p = &mut all[i];
            if p.expires.is_some_and(|e| e <= t) {
                p.ended = p.expires.map(|e| e.max(p.set));
                p.how = Some("expiry");
                lapsed.insert(agent.clone(), (p.reason.clone(), p.expires));
                standing.remove(&agent);
            }
        }
        if fields.get("park_reason").is_some_and(V::is_null) {
            if let Some(i) = standing.remove(&agent) {
                all[i].ended = Some(t);
                all[i].ended_value = event["created_at"].clone();
            }
            lapsed.remove(&agent);
            continue;
        }
        let reason = fields["park_reason"].as_str();
        let named = number(&fields["park_expires"]);
        if let Some(&i) = standing.get(&agent) {
            if let Some(reason) = reason {
                all[i].reason = reason.into();
            }
            if fields.get("park_expires").is_some() {
                all[i].expires = named;
            }
            continue;
        }
        let (reason, expires, count) = if let Some((old_reason, old_expires)) = lapsed.get(&agent) {
            if let Some(reason) = reason {
                (
                    reason.to_owned(),
                    if fields.get("park_expires").is_some() {
                        named
                    } else {
                        *old_expires
                    },
                    true,
                )
            } else if named.is_some_and(|e| e > t) {
                (old_reason.clone(), named, true)
            } else {
                continue;
            }
        } else {
            let old = number(&before["park_expires"]);
            match reason {
                None => ("unknown".into(), named, false),
                Some(reason) if before["park_reason"].is_null() || old.is_some_and(|e| e <= t) => {
                    (reason.into(), named, true)
                }
                Some(reason) => (reason.into(), named.or(old), false),
            }
        };
        let i = all.len();
        all.push(Park {
            agent: agent.clone(),
            reason,
            set: t,
            expires,
            ended: None,
            set_value: event["created_at"].clone(),
            ended_value: V::Null,
            how: None,
        });
        standing.insert(agent.clone(), i);
        lapsed.remove(&agent);
        if count && since <= t && t < until {
            counted.push(i);
        }
    }
    for &i in &counted {
        let p = &mut all[i];
        if p.ended.is_none() {
            if p.expires.is_some_and(|e| e <= until) {
                p.ended = p.expires.map(|e| e.max(p.set));
                p.how = Some("expiry");
            } else {
                p.how = Some("open");
            }
        } else if p.how.is_none() {
            p.how = Some(
                if rings
                    .get(&p.agent)
                    .is_some_and(|rs| rs.iter().any(|t| p.set < *t && *t <= p.ended.unwrap()))
                {
                    "send"
                } else {
                    "owner_update"
                },
            );
        }
    }
    let summary = |items: &[&Park]| {
        let cleared: serde_json::Map<String, V> = ["send", "owner_update", "expiry", "open"]
            .into_iter()
            .map(|how| {
                (
                    how.into(),
                    json!(items.iter().filter(|p| p.how == Some(how)).count()),
                )
            })
            .collect();
        json!({"parks":items.len(),"cleared":cleared,"seconds_to_clear":numeric_percentiles(items.iter()
            .filter(|p| matches!(p.how,Some("send"|"owner_update"))).map(|p| difference(&p.ended_value,&p.set_value)).collect())})
    };
    let items: Vec<_> = counted.iter().map(|i| &all[*i]).collect();
    let mut reasons: BTreeMap<&str, Vec<&Park>> = BTreeMap::new();
    for p in &items {
        reasons.entry(&p.reason).or_default().push(p);
    }
    let mut result = summary(&items);
    result["by_reason"] = reasons
        .iter()
        .map(|(r, ps)| ((*r).to_owned(), summary(ps)))
        .collect();
    result
}

fn sessions(events: &[&V], principals: &HashMap<&str, &str>, since: f64, until: f64) -> V {
    let mut opened: Vec<(&str, f64)> = Vec::new();
    let mut hours: BTreeMap<String, f64> = BTreeMap::new();
    let mut hour_order: Vec<String> = Vec::new();
    let mut sends: BTreeMap<String, usize> = BTreeMap::new();
    let close = |agent: &str,
                 t: f64,
                 opened: &mut Vec<(&str, f64)>,
                 hours: &mut BTreeMap<String, f64>,
                 order: &mut Vec<String>| {
        if let Some(index) = opened.iter().position(|(a, _)| *a == agent) {
            let (_, start) = opened.remove(index);
            if t > start {
                let principal = principals
                    .get(agent)
                    .copied()
                    .unwrap_or("unknown")
                    .to_owned();
                if !hours.contains_key(&principal) {
                    order.push(principal.clone());
                }
                *hours.entry(principal).or_default() += (t - start) / 3600.0;
            }
        }
    };
    for e in events {
        let t = number(&e["created_at"]).unwrap();
        if t >= until {
            break;
        }
        let agent = text(e, "agent_id");
        match text(e, "event") {
            "attach" if !truth(&e["payload"]["renewed"]) => {
                if !opened.iter().any(|(a, _)| *a == agent) {
                    opened.push((agent, t.max(since)));
                }
            }
            "detach" => close(agent, t, &mut opened, &mut hours, &mut hour_order),
            "prune" => {
                if let Some(gone) = e["payload"]["agent_ids"].as_array() {
                    for a in gone {
                        close(
                            a.as_str().unwrap_or(""),
                            t,
                            &mut opened,
                            &mut hours,
                            &mut hour_order,
                        );
                    }
                }
            }
            _ => {}
        }
        if text(e, "event") == "send" {
            *sends
                .entry(if text(e, "principal").is_empty() {
                    "unknown".into()
                } else {
                    text(e, "principal").to_owned()
                })
                .or_default() += 1;
        }
    }
    for a in opened.iter().map(|(a, _)| *a).collect::<Vec<_>>() {
        close(a, until, &mut opened, &mut hours, &mut hour_order);
    }
    let summary = |count: usize, span: f64| json!({"sends":count,"session_hours":rounded(span,2),"rate":if span != 0.0 { Some(rounded(count as f64/span,2)) } else { None }});
    let mut names = BTreeMap::new();
    for name in sends.keys().chain(hours.keys()) {
        names.insert(
            name.clone(),
            summary(
                *sends.get(name).unwrap_or(&0),
                *hours.get(name).unwrap_or(&0.0),
            ),
        );
    }
    let mut result = summary(
        sends.values().sum(),
        hour_order.iter().map(|name| hours[name]).sum(),
    );
    if hours.is_empty() {
        result["session_hours"] = json!(0);
    }
    result["by_principal"] = json!(names);
    result
}

fn suite_times(records: &[V], since: f64, until: f64) -> V {
    let mut holds = Vec::new();
    let mut waits = Vec::new();
    for record in records {
        let stamp = |key: &str| record[key].as_str().and_then(super::time::stamp);
        if !stamp("ended").is_some_and(|t| since <= t && t < until) {
            continue;
        }
        if let Some(hold) = number(&record["seconds"]).filter(|t| *t >= 0.0) {
            holds.push(hold);
        }
        if let (Some(q), Some(s)) = (stamp("queued_at"), stamp("started_at")) {
            if s >= q {
                waits.push(s - q);
            }
        }
    }
    json!({"runs":holds.len(),"hold":percentiles(holds),"queue_wait":percentiles(waits)})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn complete_reports_match_executed_python_oracle() {
        let cases: Vec<V> = serde_json::from_str(include_str!("testdata/stats.json")).unwrap();
        for case in cases {
            let mut report = compute(
                case["events"].as_array().unwrap().clone(),
                case["wakes"].as_array().unwrap().clone(),
                case["durations"].as_array().unwrap().clone(),
                1000.0,
                5000.0,
                5000.5,
                case["report"]["sources"].clone(),
            );
            assert_eq!(report["version"], env!("CARGO_PKG_VERSION"));
            report["version"] = json!("fixture");
            assert_eq!(report, case["report"], "{}", case["name"]);
            let mut line = String::new();
            super::super::codec::ascii_compact(&report, &mut line).unwrap();
            assert_eq!(line, case["line"].as_str().unwrap(), "{}", case["name"]);
        }
    }
    #[test]
    fn decimal_rounding_and_nearest_rank_follow_python() {
        assert_eq!(rounded(2.675, 2), 2.67);
        assert_eq!(rounded(2.25, 1), 2.2);
        assert_eq!(rounded(2.35, 1), 2.4);
        assert_eq!(
            percentiles(vec![1.0, 2.0, 3.0, 4.0]),
            json!({"n":4,"p50":2.0,"p95":4.0})
        );
    }
}
