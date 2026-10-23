//! `pseudolife_memory/memory/judge_agreement.py`: how often each recorded
//! judge vote agreed with the decision a human or agent later took on the
//! same row, plus its storage read (`PostgresStorage.judge_vote_decisions`)
//! and the `ServiceDream.judge_agreement` wrapper.
//!
//! The rows are the decoded column dicts Python sees. A failure Python would
//! raise inside `compute_judge_agreement` (a malformed `votes` snapshot) is a
//! `"<TypeName>: <message>"` string here, which the wrapper reports as its
//! `error` field exactly as Python's `except Exception` does.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};
use tokio_postgres::Client;

use crate::graph_review::{is_decimal, merge_veto, round3};

/// Decisions the judges took themselves: votes applied, not votes judged.
const JUDGE_DECIDERS: [&str; 2] = ["dream-judge", "dream-auto"];
const MERGE_OPINIONS: &[&str] = &["judge", "judge2", "judge3"];
const LINK_OPINIONS: &[&str] = &["judge", "judge2"];
const JUNK_OPINIONS: &[&str] = &["judge"];
/// `ServiceDream._JUDGE_AGREEMENT_WINDOW_DAYS`.
pub(crate) const JUDGE_AGREEMENT_WINDOW_DAYS: f64 = 90.0;

/// A Python exception as `f"{type(exc).__name__}: {exc}"`.
type PyResult<T> = Result<T, String>;

static NULL: Value = Value::Null;

// ── Python object model over decoded JSON ─────────────────────────────────

fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if is_int_literal(n) => "int",
        Value::Number(_) => "float",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn is_int_literal(n: &serde_json::Number) -> bool {
    !n.to_string().contains(['.', 'e', 'E'])
}

fn num_f64(n: &serde_json::Number) -> f64 {
    n.to_string().parse::<f64>().unwrap_or(f64::NAN)
}

/// Python truthiness.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => num_f64(n) != 0.0,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `x.get(key)` on a decoded value: a non-dict raises `AttributeError`.
fn get<'a>(v: &'a Value, key: &str) -> PyResult<Option<&'a Value>> {
    match v {
        Value::Object(o) => Ok(o.get(key)),
        other => Err(format!(
            "AttributeError: '{}' object has no attribute 'get'",
            py_type(other)
        )),
    }
}

/// `row.get(key)` with a missing key as None.
fn field<'a>(row: &'a Value, key: &str) -> &'a Value {
    row.get(key).unwrap_or(&NULL)
}

/// Hashing a value (set membership, dict lookup): lists and dicts raise.
fn hashable(v: &Value) -> PyResult<()> {
    match v {
        Value::Array(_) | Value::Object(_) => {
            Err(format!("TypeError: unhashable type: '{}'", py_type(v)))
        }
        _ => Ok(()),
    }
}

/// A number or bool as Python adds it: its value and whether it is an int.
fn py_num(v: &Value) -> Option<(f64, bool)> {
    match v {
        Value::Bool(b) => Some((f64::from(u8::from(*b)), true)),
        Value::Number(n) => Some((num_f64(n), is_int_literal(n))),
        _ => None,
    }
}

/// Python `==` between decoded values (`1 == 1.0 == True`).
fn py_eq(a: &Value, b: &Value) -> bool {
    match (py_num(a), py_num(b)) {
        (Some((x, _)), Some((y, _))) => return x == y,
        (Some(_), None) | (None, Some(_)) => return false,
        (None, None) => {}
    }
    match (a, b) {
        (Value::Null, Value::Null) => true,
        (Value::String(x), Value::String(y)) => x == y,
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq(p, q))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, p)| y.get(k).is_some_and(|q| py_eq(p, q)))
        }
        _ => false,
    }
}

/// `x == "literal"`.
fn is_str(v: &Value, s: &str) -> bool {
    v.as_str() == Some(s)
}

/// `_conf`: `float(value or 0.0)`, a `TypeError` / `ValueError` as 0.0.
fn conf(v: &Value) -> f64 {
    if !truthy(v) {
        return 0.0;
    }
    match v {
        Value::Bool(_) | Value::Number(_) => py_num(v).map_or(0.0, |(x, _)| x),
        Value::String(s) => super::extractor::py_float_str(s).unwrap_or(0.0),
        _ => 0.0,
    }
}

// ── variant tokens (graph_consolidation.py) ───────────────────────────────

/// `[A-Za-z0-9]` under `re.I` (CPython 3.11): ASCII alphanumerics plus the
/// four code points that case-fold onto ASCII letters.
fn vw(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '\u{130}' | '\u{131}' | '\u{17f}' | '\u{212a}')
}

/// An ASCII letter in a pattern under `re.I` (only `k` and `s` gain
/// non-ASCII partners among the letters these patterns use).
fn ci(c: char, upper: char) -> bool {
    c.to_ascii_uppercase() == upper
        || (upper == 'K' && c == '\u{212a}')
        || (upper == 'S' && c == '\u{17f}')
}

fn at(s: &[char], i: usize) -> Option<char> {
    s.get(i).copied()
}

fn sep(c: Option<char>) -> bool {
    matches!(c, Some('_' | '-'))
}

fn digit(c: Option<char>) -> bool {
    c.is_some_and(is_decimal)
}

fn digits(s: &[char], mut j: usize) -> usize {
    while digit(at(s, j)) {
        j += 1;
    }
    j
}

fn letter(s: &[char], i: usize, upper: char) -> bool {
    at(s, i).is_some_and(|c| ci(c, upper))
}

/// `(?<![A-Za-z0-9])`.
fn vb(s: &[char], i: usize) -> bool {
    i == 0 || !vw(s[i - 1])
}

/// `(?![A-Za-z0-9])`.
fn ve(s: &[char], j: usize) -> bool {
    at(s, j).is_none_or(|c| !vw(c))
}

/// `E\d+B`.
fn pat_e(s: &[char], i: usize) -> Option<usize> {
    if !vb(s, i) || !letter(s, i, 'E') || !digit(at(s, i + 1)) {
        return None;
    }
    let j = digits(s, i + 1);
    (letter(s, j, 'B') && ve(s, j + 1)).then_some(j + 1)
}

/// `\d+(?:\.\d+)?[MK]?B`.
fn pat_size(s: &[char], i: usize) -> Option<usize> {
    if !vb(s, i) || !digit(at(s, i)) {
        return None;
    }
    let mut j = digits(s, i);
    if at(s, j) == Some('.') && digit(at(s, j + 1)) {
        j = digits(s, j + 1);
    }
    if letter(s, j, 'M') || letter(s, j, 'K') {
        j += 1;
    }
    (letter(s, j, 'B') && ve(s, j + 1)).then_some(j + 1)
}

/// `Q\d[_-]K(?:[_-](?:XS|S|M|L|XL))?`, backtracking through the
/// alternatives and then the empty group when the end boundary fails.
fn pat_quant(s: &[char], i: usize) -> Option<usize> {
    if !vb(s, i)
        || !letter(s, i, 'Q')
        || !digit(at(s, i + 1))
        || !sep(at(s, i + 2))
        || !letter(s, i + 3, 'K')
    {
        return None;
    }
    let j = i + 4;
    if sep(at(s, j)) {
        let alts: [&[char]; 5] = [&['X', 'S'], &['S'], &['M'], &['L'], &['X', 'L']];
        for alt in alts {
            let k = j + 1;
            if alt.iter().enumerate().all(|(n, u)| letter(s, k + n, *u)) && ve(s, k + alt.len()) {
                return Some(k + alt.len());
            }
        }
    }
    ve(s, j).then_some(j)
}

/// `q\d[_-]\d`.
fn pat_q0(s: &[char], i: usize) -> Option<usize> {
    (vb(s, i)
        && letter(s, i, 'Q')
        && digit(at(s, i + 1))
        && sep(at(s, i + 2))
        && digit(at(s, i + 3))
        && ve(s, i + 4))
    .then_some(i + 4)
}

/// `UD[_-]Q[A-Za-z0-9_-]*`: the greedy tail always ends on a boundary.
fn pat_ud(s: &[char], i: usize) -> Option<usize> {
    if !vb(s, i)
        || !letter(s, i, 'U')
        || !letter(s, i + 1, 'D')
        || !sep(at(s, i + 2))
        || !letter(s, i + 3, 'Q')
    {
        return None;
    }
    let mut j = i + 4;
    while at(s, j).is_some_and(|c| vw(c) || c == '_' || c == '-') {
        j += 1;
    }
    Some(j)
}

/// `\d+\.\d+(?:\.\d+)*` (case-sensitive, no boundaries).
fn pat_dotted(s: &[char], i: usize) -> Option<usize> {
    if !digit(at(s, i)) {
        return None;
    }
    let j = digits(s, i);
    if at(s, j) != Some('.') || !digit(at(s, j + 1)) {
        return None;
    }
    let mut j = digits(s, j + 1);
    while at(s, j) == Some('.') && digit(at(s, j + 1)) {
        j = digits(s, j + 1);
    }
    Some(j)
}

/// `variant_tokens`: size / quant / dotted-version markers in `name`,
/// casefolded with `_` folded to `-`.
pub(crate) fn variant_tokens(name: &str) -> BTreeSet<String> {
    type Pat = fn(&[char], usize) -> Option<usize>;
    const PATTERNS: [Pat; 6] = [pat_e, pat_size, pat_quant, pat_q0, pat_ud, pat_dotted];
    let s: Vec<char> = name.chars().collect();
    let mut out = BTreeSet::new();
    for pat in PATTERNS {
        let mut i = 0;
        while i < s.len() {
            if let Some(end) = pat(&s, i) {
                let text: String = s[i..end].iter().collect();
                out.insert(crate::board::text::casefold(&text).replace('_', "-"));
                i = end;
            } else {
                i += 1;
            }
        }
    }
    out
}

/// `variant_conflict`: both names carry variant tokens and the sets differ.
pub(crate) fn variant_conflict(a: &str, b: &str) -> bool {
    let (ta, tb) = (variant_tokens(a), variant_tokens(b));
    !ta.is_empty() && !tb.is_empty() && ta != tb
}

// ── vote classes ───────────────────────────────────────────────────────────

type Classify = fn(&Value) -> PyResult<Option<&'static str>>;

/// `_merge_class`: `accept` / `reject` (relate included), None otherwise.
fn merge_class(verdict: &Value) -> PyResult<Option<&'static str>> {
    Ok(match verdict.as_str() {
        Some("accept") => Some("accept"),
        Some("reject" | "relate") => Some("reject"),
        _ => None,
    })
}

/// `_junk_class`: a dict lookup, so an unhashable verdict raises.
fn junk_class(verdict: &Value) -> PyResult<Option<&'static str>> {
    hashable(verdict)?;
    Ok(match verdict.as_str() {
        Some("delete" | "junk") => Some("delete"),
        Some("keep" | "reject") => Some("keep"),
        _ => None,
    })
}

fn link_class(verdict: &Value) -> PyResult<Option<&'static str>> {
    Ok(match verdict.as_str() {
        Some("accept") => Some("accept"),
        Some("reject") => Some("reject"),
        Some("retype") => Some("retype"),
        _ => None,
    })
}

/// A vote's confidence as the gates read it: `vote["confidence"]` (a
/// missing key raises `KeyError`), or the `_conf` float a link vote holds.
#[derive(Clone, Copy)]
enum Conf<'a> {
    Json(&'a Value),
    Float(f64),
}

/// One opinion's vote after `_classes`: always a dict by then.
#[derive(Clone, Copy)]
struct Vote<'a> {
    cls: Option<&'static str>,
    conf: Option<Conf<'a>>,
    model: &'a Value,
    served: &'a Value,
}

fn conf_value<'a>(v: &Vote<'a>) -> PyResult<Conf<'a>> {
    v.conf.ok_or_else(|| "KeyError: 'confidence'".to_string())
}

/// `first["confidence"] + second["confidence"]`, then `/ 2.0`.
fn mean_two<'a>(a: Conf<'a>, b: Conf<'a>) -> PyResult<f64> {
    fn operand(c: Conf<'_>) -> Result<(f64, bool), &Value> {
        match c {
            Conf::Float(x) => Ok((x, false)),
            Conf::Json(v) => py_num(v).ok_or(v),
        }
    }
    let type_of = |c: Conf<'a>| match c {
        Conf::Float(_) => "float",
        Conf::Json(v) => py_type(v),
    };
    match (operand(a), operand(b)) {
        (Ok((x, _)), Ok((y, _))) => Ok((x + y) / 2.0),
        (Ok(_), Err(vb)) => Err(format!(
            "TypeError: unsupported operand type(s) for +: '{}' and '{}'",
            type_of(a),
            py_type(vb)
        )),
        (Err(va), right) => {
            let tb = match right {
                Ok(_) => type_of(b),
                Err(vb) => py_type(vb),
            };
            Err(match (va, tb) {
                (Value::String(_), "str") | (Value::Array(_), "list") => format!(
                    "TypeError: unsupported operand type(s) for /: '{}' and 'float'",
                    py_type(va)
                ),
                (Value::String(_), t) => {
                    format!("TypeError: can only concatenate str (not \"{t}\") to str")
                }
                (Value::Array(_), t) => {
                    format!("TypeError: can only concatenate list (not \"{t}\") to list")
                }
                (_, t) => format!(
                    "TypeError: unsupported operand type(s) for +: '{}' and '{t}'",
                    py_type(va)
                ),
            })
        }
    }
}

/// `sum(v["confidence"] for v in side) / len(side)` under CPython 3.11
/// (plain sequential addition from the int 0).
fn mean_side(side: &[Vote<'_>]) -> PyResult<f64> {
    let (mut acc, mut acc_int) = (0.0_f64, true);
    for v in side {
        let (x, is_int) = match conf_value(v)? {
            Conf::Float(x) => (x, false),
            Conf::Json(j) => py_num(j).ok_or_else(|| {
                format!(
                    "TypeError: unsupported operand type(s) for +: '{}' and '{}'",
                    if acc_int { "int" } else { "float" },
                    py_type(j)
                )
            })?,
        };
        acc += x;
        acc_int &= is_int;
    }
    Ok(acc / side.len() as f64)
}

/// `_distinct`: both served names known and different, and the configured
/// stamps different too.
fn distinct(first: &Vote<'_>, second: &Vote<'_>) -> bool {
    let empty = Value::String(String::new());
    let m1 = if truthy(first.model) {
        first.model
    } else {
        &empty
    };
    let m2 = if truthy(second.model) {
        second.model
    } else {
        &empty
    };
    truthy(first.served)
        && truthy(second.served)
        && !py_eq(first.served, second.served)
        && !py_eq(m1, m2)
}

/// `_third_distinct`: the third vote's served name known and, like its
/// stamp, other than every name the first two votes carry.
fn third_distinct(third: &Vote<'_>, first: &Vote<'_>, second: &Vote<'_>) -> PyResult<bool> {
    let mut earlier: Vec<&Value> = Vec::new();
    for v in [first, second] {
        for m in [v.served, v.model] {
            if truthy(m) {
                hashable(m)?;
                earlier.push(m);
            }
        }
    }
    let served3 = third.served;
    if !truthy(served3) {
        return Ok(false);
    }
    hashable(served3)?;
    if earlier.iter().any(|e| py_eq(e, served3)) {
        return Ok(false);
    }
    hashable(third.model)?;
    Ok(!earlier.iter().any(|e| py_eq(e, third.model)))
}

/// The gates by class, in Python's dict order.
type Gates = [(&'static str, f64); 2];

fn gate(gates: &Gates, cls: Option<&str>) -> Option<f64> {
    gates.iter().find(|(c, _)| Some(*c) == cls).map(|(_, g)| *g)
}

/// `_agreed_pair`: the class when the first two opinions agree on an accept
/// or reject class, come from distinct models and clear the class's gate.
fn agreed_pair(votes: &[(&str, Vote<'_>)], gates: &Gates) -> PyResult<Option<&'static str>> {
    let find = |op: &str| votes.iter().find(|(o, _)| *o == op).map(|(_, v)| *v);
    let (Some(first), Some(second)) = (find("judge"), find("judge2")) else {
        return Ok(None);
    };
    let cls = first.cls;
    let Some(g) = gate(gates, cls) else {
        return Ok(None);
    };
    if second.cls != cls || !distinct(&first, &second) {
        return Ok(None);
    }
    let (a, b) = (conf_value(&first)?, conf_value(&second)?);
    if mean_two(a, b)? < g {
        return Ok(None);
    }
    Ok(cls)
}

/// `_majority`: the class when all three opinions voted, the served-name
/// rule holds and two votes share a class at a mean clearing its gate.
fn majority(votes: &[(&str, Vote<'_>)], gates: &Gates) -> PyResult<Option<&'static str>> {
    let find = |op: &str| votes.iter().find(|(o, _)| *o == op).map(|(_, v)| *v);
    let (Some(first), Some(second), Some(third)) = (find("judge"), find("judge2"), find("judge3"))
    else {
        return Ok(None);
    };
    if !(truthy(first.served) && truthy(second.served) && third_distinct(&third, &first, &second)?)
    {
        return Ok(None);
    }
    for (cls, g) in gates {
        let side: Vec<Vote<'_>> = [first, second, third]
            .into_iter()
            .filter(|v| v.cls == Some(*cls))
            .collect();
        if side.len() >= 2 {
            return Ok((mean_side(&side)? >= *g).then_some(*cls));
        }
    }
    Ok(None)
}

/// `_classes` over a merge `votes` dict, then each present opinion as a
/// `Vote` (a non-dict opinion raises `AttributeError` here, as in Python).
fn merge_votes<'a>(
    votes: &'a Value,
    classify: Classify,
) -> PyResult<Vec<(&'static str, Vote<'a>)>> {
    let mut out = Vec::new();
    for op in MERGE_OPINIONS {
        let Some(vote) = get(votes, op)? else {
            continue;
        };
        let cls = classify(get(vote, "verdict")?.unwrap_or(&NULL))?;
        let map = vote.as_object().expect("checked by get");
        out.push((
            *op,
            Vote {
                cls,
                conf: map.get("confidence").map(Conf::Json),
                model: map.get("model").unwrap_or(&NULL),
                served: map.get("served_model").unwrap_or(&NULL),
            },
        ));
    }
    Ok(out)
}

// ── tallies ────────────────────────────────────────────────────────────────

struct Opinions {
    ops: &'static [&'static str],
    voted: Vec<u64>,
    agree: Vec<u64>,
    abstain: Vec<u64>,
    by_verdict: Vec<BTreeMap<&'static str, (u64, u64)>>,
}

impl Opinions {
    fn new(ops: &'static [&'static str]) -> Self {
        Self {
            ops,
            voted: vec![0; ops.len()],
            agree: vec![0; ops.len()],
            abstain: vec![0; ops.len()],
            by_verdict: vec![BTreeMap::new(); ops.len()],
        }
    }

    fn score(&mut self, votes: &Value, label: &str, classify: Classify) -> PyResult<()> {
        for (i, op) in self.ops.iter().enumerate() {
            let vote = get(votes, op)?.unwrap_or(&NULL);
            if !truthy(vote) {
                continue;
            }
            let Some(cls) = classify(get(vote, "verdict")?.unwrap_or(&NULL))? else {
                self.abstain[i] += 1;
                continue;
            };
            self.voted[i] += 1;
            let slot = self.by_verdict[i].entry(cls).or_insert((0, 0));
            slot.0 += 1;
            if cls == label {
                self.agree[i] += 1;
                slot.1 += 1;
            }
        }
        Ok(())
    }

    fn report(&self) -> Map<String, Value> {
        let mut out = Map::new();
        for (i, op) in self.ops.iter().enumerate() {
            let mut by = Map::new();
            for (cls, (n, agree)) in &self.by_verdict[i] {
                by.insert((*cls).into(), json!({"n": n, "agree": agree}));
            }
            out.insert(
                (*op).into(),
                json!({
                    "voted": self.voted[i],
                    "agree": self.agree[i],
                    "abstain": self.abstain[i],
                    "precision": ratio(self.agree[i], self.voted[i]),
                    "by_verdict": by,
                }),
            );
        }
        out
    }
}

/// `_Pairs`: agreeing votes by class, how often the gate's choice matched.
#[derive(Default)]
struct Pairs {
    n: [u64; 2],
    agree: [u64; 2],
}

impl Pairs {
    fn score(&mut self, cls: &str, label: &str) {
        let i = usize::from(cls != "accept");
        self.n[i] += 1;
        if cls == label {
            self.agree[i] += 1;
        }
    }

    fn report(&self) -> Value {
        let mut out = Map::new();
        for (i, cls) in ["accept", "reject"].into_iter().enumerate() {
            out.insert(
                cls.into(),
                json!({"n": self.n[i], "agree": self.agree[i],
                       "precision": ratio(self.agree[i], self.n[i])}),
            );
        }
        Value::Object(out)
    }
}

/// `_ratio`: `round(num / den, 3)`, None for an empty denominator.
fn ratio(num: u64, den: u64) -> Value {
    if den == 0 {
        Value::Null
    } else {
        json!(round3(num as f64 / den as f64))
    }
}

/// A `decided_by` value as a label key: its hash is taken (`in
/// JUDGE_DECIDERS`), and `json.dumps` writes a non-str key as its JSON form.
fn decider_key(v: &Value) -> PyResult<String> {
    hashable(v)?;
    Ok(match v {
        Value::String(s) => s.clone(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => crate::pyjson::number_text(n),
        _ => "null".into(),
    })
}

fn is_judge_decider(v: &Value) -> bool {
    v.as_str().is_some_and(|s| JUDGE_DECIDERS.contains(&s))
}

fn bump(labels: &mut Map<String, Value>, key: String) {
    let n = labels.get(&key).and_then(Value::as_u64).unwrap_or(0);
    labels.insert(key, json!(n + 1));
}

/// `str(x or "")`.
fn str_or_empty(v: &Value) -> String {
    if truthy(v) {
        crate::pyjson::py_str(v)
    } else {
        String::new()
    }
}

/// `_accept_refused`: an analyzer-filed proposal, a name veto or a variant
/// conflict between the two names.
fn accept_refused(row: &Value) -> bool {
    if str_or_empty(field(row, "reason")).starts_with("analyzer-duplicate") {
        return true;
    }
    let a = str_or_empty(field(row, "entity_display"));
    let b = str_or_empty(field(row, "into_display"));
    merge_veto(&a, &b).is_some() || variant_conflict(&a, &b)
}

/// `compute_judge_agreement`, or the exception Python would raise as
/// `"<TypeName>: <message>"`.
pub(crate) fn try_compute_judge_agreement(
    merge_rows: &[Value],
    link_rows: &[Value],
    accept_gate: f64,
    reject_gate: f64,
    link_gate: f64,
    window_days: f64,
) -> PyResult<Value> {
    let mut labels = Map::new();
    let (mut scored_merge, mut scored_junk, mut scored_link) = (0u64, 0u64, 0u64);
    let mut unattributed_links = 0u64;
    let mut merge = Opinions::new(MERGE_OPINIONS);
    let mut junk = Opinions::new(JUNK_OPINIONS);
    let mut link = Opinions::new(LINK_OPINIONS);
    let (mut merge_two, mut merge_three, mut link_two) =
        (Pairs::default(), Pairs::default(), Pairs::default());
    let merge_gates: Gates = [("accept", accept_gate), ("reject", reject_gate)];
    let link_gates: Gates = [("accept", link_gate), ("reject", link_gate)];

    for row in merge_rows {
        let decider = field(row, "decided_by");
        let votes = field(row, "votes");
        if !truthy(votes) || !truthy(decider) {
            continue;
        }
        let key = decider_key(decider)?;
        if is_judge_decider(decider) {
            continue;
        }
        let is_junk = field(row, "into_display").is_null();
        let status = field(row, "status");
        hashable(status)?;
        let label = match (is_junk, status.as_str()) {
            (true, Some("accepted")) => "delete",
            (true, Some("rejected")) => "keep",
            (false, Some("accepted")) => "accept",
            (false, Some("rejected")) => "reject",
            _ => continue,
        };
        bump(&mut labels, key);
        if is_junk {
            scored_junk += 1;
            junk.score(votes, label, junk_class)?;
            continue;
        }
        scored_merge += 1;
        merge.score(votes, label, merge_class)?;
        let classed = merge_votes(votes, merge_class)?;
        let refused = accept_refused(row);
        if let Some(cls) = agreed_pair(&classed, &merge_gates)?
            && !(refused && cls == "accept")
        {
            merge_two.score(cls, label);
        }
        if let Some(cls) = majority(&classed, &merge_gates)?
            && !(refused && cls == "accept")
        {
            merge_three.score(cls, label);
        }
    }

    for row in link_rows {
        let decider = field(row, "decided_by");
        let status = field(row, "status");
        hashable(status)?;
        let label = match status.as_str() {
            Some("accepted") => "accept",
            Some("rejected") => "reject",
            Some("retyped") => "retype",
            _ => continue,
        };
        hashable(decider)?;
        if is_judge_decider(decider) {
            continue;
        }
        if !truthy(decider) {
            unattributed_links += 1;
            continue;
        }
        let mut shown = Map::new();
        let mut classed = Vec::new();
        for op in LINK_OPINIONS {
            let verdict = field(row, &format!("{op}_verdict"));
            if !truthy(verdict) {
                continue;
            }
            shown.insert((*op).into(), json!({"verdict": verdict}));
            classed.push((
                *op,
                Vote {
                    cls: link_class(verdict)?,
                    conf: Some(Conf::Float(conf(field(row, &format!("{op}_confidence"))))),
                    model: field(row, &format!("{op}_model")),
                    served: field(row, &format!("{op}_served_model")),
                },
            ));
        }
        if classed.is_empty() {
            continue;
        }
        bump(&mut labels, decider_key(decider)?);
        scored_link += 1;
        link.score(&Value::Object(shown), label, link_class)?;
        if let Some(cls) = agreed_pair(&classed, &link_gates)? {
            link_two.score(cls, label);
        }
    }

    let mut merge_out = merge.report();
    merge_out.insert("agreed_two".into(), merge_two.report());
    merge_out.insert("majority_three".into(), merge_three.report());
    let mut link_out = link.report();
    link_out.insert("agreed_two".into(), link_two.report());
    Ok(json!({
        "window_days": window_days,
        "scored": {"merge": scored_merge, "junk": scored_junk, "link": scored_link},
        "labels": labels,
        "unattributed_links": unattributed_links,
        "merge": merge_out,
        "junk": junk.report(),
        "link": link_out,
    }))
}

/// `compute_judge_agreement`: every decided row's votes scored against its
/// decision. Where Python would raise, this answers the wrapper's
/// `{"error": "<TypeName>: <message>"}`.
pub(crate) fn compute_judge_agreement(
    merge_rows: &[Value],
    link_rows: &[Value],
    accept_gate: f64,
    reject_gate: f64,
    link_gate: f64,
    window_days: f64,
) -> Value {
    try_compute_judge_agreement(
        merge_rows,
        link_rows,
        accept_gate,
        reject_gate,
        link_gate,
        window_days,
    )
    .unwrap_or_else(|e| json!({"error": e}))
}

// ── storage and service ────────────────────────────────────────────────────

/// `PostgresStorage.judge_vote_decisions` (`postgres.py:3976-4003`): the
/// decided merge / junk rows carrying a vote snapshot and the decided link
/// proposals, decided at or after `since`, as `(merge, link)` column dicts.
pub(crate) async fn judge_vote_decisions(
    client: &Client,
    since: f64,
) -> Result<(Vec<Value>, Vec<Value>), anyhow::Error> {
    let (merges, links) = crate::txn::with_client(client, |c| async move {
        let merges = c
            .query(
                "SELECT status, decided_by, decided_at, entity_display, \
                        into_display, reason, votes \
                 FROM merge_decisions \
                 WHERE votes IS NOT NULL AND decided_at >= $1 \
                   AND status IN ('accepted', 'rejected')",
                &[&since],
            )
            .await?;
        let links = c
            .query(
                "SELECT id, status, decided_by, decided_at, \
                        judge_verdict, judge_confidence::text::double precision, \
                        judge_model, judge_served_model, judge2_verdict, \
                        judge2_confidence::text::double precision, judge2_model, \
                        judge2_served_model \
                 FROM edge_proposals \
                 WHERE judge_verdict IS NOT NULL AND decided_at >= $1 \
                   AND status IN ('accepted', 'rejected', 'retyped')",
                &[&since],
            )
            .await?;
        Ok::<_, anyhow::Error>((merges, links))
    })
    .await?;
    let merges = merges
        .iter()
        .map(|r| {
            json!({
                "status": r.get::<_, Option<String>>(0),
                "decided_by": r.get::<_, Option<String>>(1),
                "decided_at": r.get::<_, Option<f64>>(2),
                "entity_display": r.get::<_, Option<String>>(3),
                "into_display": r.get::<_, Option<String>>(4),
                "reason": r.get::<_, Option<String>>(5),
                "votes": r.get::<_, Option<Value>>(6),
            })
        })
        .collect();
    let links = links
        .iter()
        .map(|r| {
            json!({
                "id": r.get::<_, Option<i64>>(0),
                "status": r.get::<_, Option<String>>(1),
                "decided_by": r.get::<_, Option<String>>(2),
                "decided_at": r.get::<_, Option<f64>>(3),
                "judge_verdict": r.get::<_, Option<String>>(4),
                "judge_confidence": r.get::<_, Option<f64>>(5),
                "judge_model": r.get::<_, Option<String>>(6),
                "judge_served_model": r.get::<_, Option<String>>(7),
                "judge2_verdict": r.get::<_, Option<String>>(8),
                "judge2_confidence": r.get::<_, Option<f64>>(9),
                "judge2_model": r.get::<_, Option<String>>(10),
                "judge2_served_model": r.get::<_, Option<String>>(11),
            })
        })
        .collect();
    Ok((merges, links))
}

/// `ServiceDream.judge_agreement` (`service_dream.py:3795-3819`) over the
/// default 90-day window: never fails, a failure is an `error` field. The
/// gates are `judge_accept_min_confidence`, `judge_reject_min_confidence_2`
/// and `link_two_vote_min_confidence`.
pub(crate) async fn judge_agreement(
    client: &Client,
    now: f64,
    accept_gate: f64,
    reject_gate: f64,
    link_gate: f64,
) -> Value {
    let window = JUDGE_AGREEMENT_WINDOW_DAYS;
    let result = match judge_vote_decisions(client, now - window * 86400.0).await {
        Ok((merges, links)) => try_compute_judge_agreement(
            &merges,
            &links,
            accept_gate,
            reject_gate,
            link_gate,
            window,
        ),
        Err(e) => Err(db_error_text(&e)),
    };
    result.unwrap_or_else(|e| {
        eprintln!("judge-agreement report failed: {e}");
        json!({"error": e})
    })
}

/// Best effort at psycopg's `f"{type(exc).__name__}: {exc}"` for a failed
/// read: a server error as its primary message.
fn db_error_text(e: &anyhow::Error) -> String {
    match e
        .downcast_ref::<tokio_postgres::Error>()
        .and_then(tokio_postgres::Error::as_db_error)
    {
        Some(db) => format!("DatabaseError: {}", db.message()),
        None => format!("OperationalError: {e}"),
    }
}

#[cfg(test)]
mod tests {
    //! Expected outputs are CPython 3.11 `json.dumps` of the Python
    //! oracle (`compute_judge_agreement`, `variant_tokens`) on the same
    //! inputs, gates accept 0.6 / reject 0.7 / link 0.7.
    use super::*;

    /// `(name, merge rows, link rows, window_days, expected)`.
    const CASES: &[(&str, &str, &str, f64, &str)] = &[
        (
            "empty",
            r#"[]"#,
            r#"[]"#,
            90.0,
            r#"{"window_days": 90.0, "scored": {"merge": 0, "junk": 0, "link": 0}, "labels": {}, "unattributed_links": 0, "merge": {"judge": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "judge2": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "judge3": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "agreed_two": {"accept": {"n": 0, "agree": 0, "precision": null}, "reject": {"n": 0, "agree": 0, "precision": null}}, "majority_three": {"accept": {"n": 0, "agree": 0, "precision": null}, "reject": {"n": 0, "agree": 0, "precision": null}}}, "junk": {"judge": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}}, "link": {"judge": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "judge2": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "agreed_two": {"accept": {"n": 0, "agree": 0, "precision": null}, "reject": {"n": 0, "agree": 0, "precision": null}}}}"#,
        ),
        (
            "merge",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "accepted", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "A"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "relate", "confidence": 0.6, "model": "a", "served_model": "A", "relation": "uses"}, "judge2": {"verdict": "reject", "confidence": 0.7, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "relate", "confidence": 0.8, "model": "a", "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.75, "model": "b", "served_model": "B"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.7, "model": "a", "served_model": "A"}, "judge2": {"verdict": "leave", "confidence": 0.5, "model": "b", "served_model": "B"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": "c", "served_model": "C"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.5, "model": "b", "served_model": "B"}, "judge3": {"verdict": "reject", "confidence": 0.9, "model": "c", "served_model": "a"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.8, "model": "a", "served_model": "A"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": "c", "served_model": "C"}}}, {"status": "accepted", "decided_by": "dream-judge", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.9, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "dream-auto", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a", "served_model": "A"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": null}, {"status": "accepted", "decided_by": null, "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": "analyzer-duplicate: x", "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Gemma 4 E4B", "into_display": "Gemma 4 E2B", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "qwen 3.6", "into_display": "qwen 3.5", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": "c", "served_model": "C"}}}, {"status": "rejected", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "qwen 3.6", "into_display": "qwen 3.5", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "CT200 host", "into_display": "CT400 host", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "delete", "confidence": 0.9, "model": "a"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "junk", "confidence": 0.9, "model": "a"}}}, {"status": "rejected", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "keep", "confidence": 0.9, "model": "a"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "delete", "confidence": 0.9, "model": "a"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "maybe", "confidence": 0.9, "model": "a"}}}, {"status": "pending", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 1, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": true, "model": "b", "served_model": "B"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": null, "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.9, "model": "", "served_model": "B"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.6, "model": null, "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.9, "model": null, "served_model": "B"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": null, "served_model": "C"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.6, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.6, "model": "b", "served_model": "B"}, "judge3": {}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"window_days": 90.0, "scored": {"merge": 17, "junk": 6, "link": 0}, "labels": {"human": 18, "agent": 5}, "unattributed_links": 0, "merge": {"judge": {"voted": 17, "agree": 12, "abstain": 0, "precision": 0.706, "by_verdict": {"accept": {"n": 11, "agree": 7}, "reject": {"n": 6, "agree": 5}}}, "judge2": {"voted": 16, "agree": 12, "abstain": 1, "precision": 0.75, "by_verdict": {"accept": {"n": 11, "agree": 7}, "reject": {"n": 5, "agree": 5}}}, "judge3": {"voted": 5, "agree": 1, "abstain": 0, "precision": 0.2, "by_verdict": {"accept": {"n": 4, "agree": 1}, "reject": {"n": 1, "agree": 0}}}, "agreed_two": {"accept": {"n": 3, "agree": 3, "precision": 1.0}, "reject": {"n": 2, "agree": 2, "precision": 1.0}}, "majority_three": {"accept": {"n": 1, "agree": 1, "precision": 1.0}, "reject": {"n": 2, "agree": 2, "precision": 1.0}}}, "junk": {"judge": {"voted": 5, "agree": 5, "abstain": 1, "precision": 1.0, "by_verdict": {"delete": {"n": 3, "agree": 3}, "keep": {"n": 2, "agree": 2}}}}, "link": {"judge": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "judge2": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "agreed_two": {"accept": {"n": 0, "agree": 0, "precision": null}, "reject": {"n": 0, "agree": 0, "precision": null}}}}"#,
        ),
        (
            "link",
            r#"[]"#,
            r#"[{"id": 1, "status": "accepted", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": 0.7, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "rejected", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": 0.7, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "retyped", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "retype", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "retype", "judge2_confidence": 0.9, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "retyped", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": 0.9, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": null, "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "accepted", "decided_by": "", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "accepted", "decided_by": "dream-judge", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "pending", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "accepted", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": null, "judge_confidence": null, "judge_model": null, "judge_served_model": null, "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "rejected", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "reject", "judge_confidence": "0.9", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": " 0_6 ", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "rejected", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "reject", "judge_confidence": null, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": "0.9", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "rejected", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "reject", "judge_confidence": "inf", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": "garbage", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": "nan", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": 0.1, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": true, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": "\u0661.\u0660", "judge2_model": "b", "judge2_served_model": null}, {"id": 1, "status": "accepted", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": "\u20000.75", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": [1], "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "weird", "judge_confidence": 0.9, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": "1.e0", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": "1_000", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": "-inFinity", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "rejected", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "reject", "judge_confidence": "1__0", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": "1e1_0", "judge2_model": "b", "judge2_served_model": "B"}]"#,
            30.5,
            r#"{"window_days": 30.5, "scored": {"merge": 0, "junk": 0, "link": 13}, "labels": {"human": 6, "agent": 7}, "unattributed_links": 2, "merge": {"judge": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "judge2": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "judge3": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}, "agreed_two": {"accept": {"n": 0, "agree": 0, "precision": null}, "reject": {"n": 0, "agree": 0, "precision": null}}, "majority_three": {"accept": {"n": 0, "agree": 0, "precision": null}, "reject": {"n": 0, "agree": 0, "precision": null}}}, "junk": {"judge": {"voted": 0, "agree": 0, "abstain": 0, "precision": null, "by_verdict": {}}}, "link": {"judge": {"voted": 12, "agree": 10, "abstain": 1, "precision": 0.833, "by_verdict": {"accept": {"n": 7, "agree": 5}, "reject": {"n": 4, "agree": 4}, "retype": {"n": 1, "agree": 1}}}, "judge2": {"voted": 13, "agree": 11, "abstain": 0, "precision": 0.846, "by_verdict": {"accept": {"n": 7, "agree": 6}, "reject": {"n": 5, "agree": 4}, "retype": {"n": 1, "agree": 1}}}, "agreed_two": {"accept": {"n": 3, "agree": 2, "precision": 0.667}, "reject": {"n": 3, "agree": 3, "precision": 1.0}}}}"#,
        ),
        (
            "both",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "accepted", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "A"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "relate", "confidence": 0.6, "model": "a", "served_model": "A", "relation": "uses"}, "judge2": {"verdict": "reject", "confidence": 0.7, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "relate", "confidence": 0.8, "model": "a", "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.75, "model": "b", "served_model": "B"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.7, "model": "a", "served_model": "A"}, "judge2": {"verdict": "leave", "confidence": 0.5, "model": "b", "served_model": "B"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": "c", "served_model": "C"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.5, "model": "b", "served_model": "B"}, "judge3": {"verdict": "reject", "confidence": 0.9, "model": "c", "served_model": "a"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.8, "model": "a", "served_model": "A"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": "c", "served_model": "C"}}}, {"status": "accepted", "decided_by": "dream-judge", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.9, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "dream-auto", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a", "served_model": "A"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": null}, {"status": "accepted", "decided_by": null, "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": "analyzer-duplicate: x", "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Gemma 4 E4B", "into_display": "Gemma 4 E2B", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "qwen 3.6", "into_display": "qwen 3.5", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": "c", "served_model": "C"}}}, {"status": "rejected", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "qwen 3.6", "into_display": "qwen 3.5", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "CT200 host", "into_display": "CT400 host", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.8, "model": "b", "served_model": "B"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "delete", "confidence": 0.9, "model": "a"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "junk", "confidence": 0.9, "model": "a"}}}, {"status": "rejected", "decided_by": "agent", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "keep", "confidence": 0.9, "model": "a"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.9, "model": "a"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "delete", "confidence": 0.9, "model": "a"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": "maybe", "confidence": 0.9, "model": "a"}}}, {"status": "pending", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 1, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": true, "model": "b", "served_model": "B"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": null, "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.9, "model": "", "served_model": "B"}}}, {"status": "rejected", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "reject", "confidence": 0.6, "model": null, "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.9, "model": null, "served_model": "B"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": null, "served_model": "C"}}}, {"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.6, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.6, "model": "b", "served_model": "B"}, "judge3": {}}}]"#,
            r#"[{"id": 1, "status": "accepted", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": 0.7, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "rejected", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": 0.7, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "retyped", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "retype", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "retype", "judge2_confidence": 0.9, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "retyped", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": 0.9, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": null, "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "accepted", "decided_by": "", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "accepted", "decided_by": "dream-judge", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "pending", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": 0.8, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "accepted", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": null, "judge_confidence": null, "judge_model": null, "judge_served_model": null, "judge2_verdict": null, "judge2_confidence": null, "judge2_model": null, "judge2_served_model": null}, {"id": 1, "status": "rejected", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "reject", "judge_confidence": "0.9", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": " 0_6 ", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "rejected", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "reject", "judge_confidence": null, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": "0.9", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "rejected", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "reject", "judge_confidence": "inf", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": "garbage", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": "nan", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": 0.1, "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": true, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": "\u0661.\u0660", "judge2_model": "b", "judge2_served_model": null}, {"id": 1, "status": "accepted", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": "\u20000.75", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": [1], "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": "agent", "decided_at": 1600000000.0, "judge_verdict": "weird", "judge_confidence": 0.9, "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": "1.e0", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "accepted", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "accept", "judge_confidence": "1_000", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "accept", "judge2_confidence": "-inFinity", "judge2_model": "b", "judge2_served_model": "B"}, {"id": 1, "status": "rejected", "decided_by": "human", "decided_at": 1600000000.0, "judge_verdict": "reject", "judge_confidence": "1__0", "judge_model": "a", "judge_served_model": "A", "judge2_verdict": "reject", "judge2_confidence": "1e1_0", "judge2_model": "b", "judge2_served_model": "B"}]"#,
            90.0,
            r#"{"window_days": 90.0, "scored": {"merge": 17, "junk": 6, "link": 13}, "labels": {"human": 24, "agent": 12}, "unattributed_links": 2, "merge": {"judge": {"voted": 17, "agree": 12, "abstain": 0, "precision": 0.706, "by_verdict": {"accept": {"n": 11, "agree": 7}, "reject": {"n": 6, "agree": 5}}}, "judge2": {"voted": 16, "agree": 12, "abstain": 1, "precision": 0.75, "by_verdict": {"accept": {"n": 11, "agree": 7}, "reject": {"n": 5, "agree": 5}}}, "judge3": {"voted": 5, "agree": 1, "abstain": 0, "precision": 0.2, "by_verdict": {"accept": {"n": 4, "agree": 1}, "reject": {"n": 1, "agree": 0}}}, "agreed_two": {"accept": {"n": 3, "agree": 3, "precision": 1.0}, "reject": {"n": 2, "agree": 2, "precision": 1.0}}, "majority_three": {"accept": {"n": 1, "agree": 1, "precision": 1.0}, "reject": {"n": 2, "agree": 2, "precision": 1.0}}}, "junk": {"judge": {"voted": 5, "agree": 5, "abstain": 1, "precision": 1.0, "by_verdict": {"delete": {"n": 3, "agree": 3}, "keep": {"n": 2, "agree": 2}}}}, "link": {"judge": {"voted": 12, "agree": 10, "abstain": 1, "precision": 0.833, "by_verdict": {"accept": {"n": 7, "agree": 5}, "reject": {"n": 4, "agree": 4}, "retype": {"n": 1, "agree": 1}}}, "judge2": {"voted": 13, "agree": 11, "abstain": 0, "precision": 0.846, "by_verdict": {"accept": {"n": 7, "agree": 6}, "reject": {"n": 5, "agree": 4}, "retype": {"n": 1, "agree": 1}}}, "agreed_two": {"accept": {"n": 3, "agree": 2, "precision": 0.667}, "reject": {"n": 3, "agree": 3, "precision": 1.0}}}}"#,
        ),
        (
            "err_votes_list",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": [1, 2]}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "AttributeError: 'list' object has no attribute 'get'"}"#,
        ),
        (
            "err_vote_null",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": null}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "AttributeError: 'NoneType' object has no attribute 'get'"}"#,
        ),
        (
            "err_conf_missing",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.9, "model": "b", "served_model": "B"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "KeyError: 'confidence'"}"#,
        ),
        (
            "err_conf_str_str",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": "0.9", "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": "0.9", "model": "b", "served_model": "B"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "TypeError: unsupported operand type(s) for /: 'str' and 'float'"}"#,
        ),
        (
            "err_conf_str_num",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": "0.9", "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 0.9, "model": "b", "served_model": "B"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "TypeError: can only concatenate str (not \"float\") to str"}"#,
        ),
        (
            "err_conf_num_null",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": null, "model": "b", "served_model": "B"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "TypeError: unsupported operand type(s) for +: 'float' and 'NoneType'"}"#,
        ),
        (
            "err_conf_list_int",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": [0.9], "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": 1, "model": "b", "served_model": "B"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "TypeError: can only concatenate list (not \"int\") to list"}"#,
        ),
        (
            "err_majority_str",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "reject", "confidence": 0.9, "model": "b", "served_model": "B"}, "judge3": {"verdict": "accept", "confidence": "x", "model": "c", "served_model": "C"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "TypeError: unsupported operand type(s) for +: 'float' and 'str'"}"#,
        ),
        (
            "err_majority_float_str",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": "A"}, "judge2": {"verdict": "accept", "confidence": "x", "model": "b", "served_model": "A"}, "judge3": {"verdict": "reject", "confidence": 0.4, "model": "c", "served_model": "C"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "TypeError: unsupported operand type(s) for +: 'float' and 'str'"}"#,
        ),
        (
            "err_junk_list_verdict",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": null, "reason": null, "votes": {"judge": {"verdict": ["delete"], "confidence": 0.9, "model": "a"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "TypeError: unhashable type: 'list'"}"#,
        ),
        (
            "err_unhashable_served",
            r#"[{"status": "accepted", "decided_by": "human", "decided_at": 1500000000.0, "entity_display": "Alpha", "into_display": "Alpha Beta", "reason": null, "votes": {"judge": {"verdict": "accept", "confidence": 0.9, "model": "a", "served_model": ["A"]}, "judge2": {"verdict": "reject", "confidence": 0.9, "model": "b", "served_model": "B"}, "judge3": {"verdict": "accept", "confidence": 0.9, "model": "c", "served_model": "C"}}}]"#,
            r#"[]"#,
            90.0,
            r#"{"error": "TypeError: unhashable type: 'list'"}"#,
        ),
    ];

    #[test]
    fn compute_judge_agreement_matches_python() {
        for (name, merge, link, window, expected) in CASES {
            let merge: Vec<Value> = serde_json::from_str(merge).unwrap();
            let link: Vec<Value> = serde_json::from_str(link).unwrap();
            let got = compute_judge_agreement(&merge, &link, 0.6, 0.7, 0.7, *window);
            assert_eq!(crate::pyjson::dumps(&got), *expected, "case {name}");
        }
    }

    /// `(name, sorted(variant_tokens(name)) as json.dumps)`.
    const VARIANTS: &[(&str, &str)] = &[
        (r#""Gemma 4 E4B""#, r#"["e4b"]"#),
        (r#""gemma-4-e2b-it-Q4_K_M""#, r#"["e2b", "q4-k-m"]"#),
        (r#""qwen 3.6""#, r#"["3.6"]"#),
        (
            r#""Qwen3.6-35B-A3B UD-Q4_K_XL""#,
            r#"["3.6", "35b", "q4-k-xl", "ud-q4-k-xl"]"#,
        ),
        (r#""llama q4_0""#, r#"["q4-0"]"#),
        (r#""Q4_K_XLZ""#, r#"["q4-k"]"#),
        (r#""Q4_K_XS""#, r#"["q4-k-xs"]"#),
        (r#""Q4-k-s model""#, r#"["q4-k-s"]"#),
        (r#""v0.2.0 release""#, r#"["0.2.0"]"#),
        (r#""x\u0663.\u0664 \u212a""#, r#"["\u0663.\u0664"]"#),
        (r#""E\u0664B 7b""#, r#"["7b", "e\u0664b"]"#),
        (r#""Q5_\u212a_\u017f""#, r#"["q5-k-s"]"#),
        (r#""a1.2.3b 4.5B""#, r#"["1.2.3", "2.3b", "4.5", "4.5b"]"#),
        (r#""plain name""#, r#"[]"#),
        (r#""\u0130\u0661.\u0662""#, r#"["\u0661.\u0662"]"#),
        (r#""26B-A4B 1.5mb""#, r#"["1.5", "1.5mb", "26b"]"#),
        (r#""x\u06634B""#, r#"["4b"]"#),
        (r#""ud-q4_k_xl.gguf""#, r#"["q4-k-xl", "ud-q4-k-xl"]"#),
        (r#""1.5KB2""#, r#"["1.5"]"#),
    ];

    #[test]
    fn variant_tokens_match_python() {
        for (name, expected) in VARIANTS {
            let name: String = serde_json::from_str(name).unwrap();
            let got: Vec<String> = variant_tokens(&name).into_iter().collect();
            assert_eq!(crate::pyjson::dumps(&json!(got)), *expected, "{name:?}");
        }
        assert!(variant_conflict("Gemma 4 E4B", "Gemma 4 E2B"));
        assert!(variant_conflict("qwen 3.6", "qwen 3.5"));
        assert!(!variant_conflict("qwen 3.6", "qwen"));
        assert!(!variant_conflict("Qwen 3.6", "qwen-3.6"));
    }
}
