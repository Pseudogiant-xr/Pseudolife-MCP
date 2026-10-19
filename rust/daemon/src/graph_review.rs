//! The pure graph-hygiene analyzer behind `memory_graph_review(action="list")`;
//! oracle: `memory/graph_review.py` (`review`, 539-554, and every analyzer it
//! calls). DB-free like the Python: the tool module loads the rows, this
//! returns the findings. Read-only.
//!
//! Python semantics carried over exactly:
//! * `str.lower()` is `storage::graph::py_lower` (Unicode 14 tables).
//! * `re.I` on the ASCII literal patterns matches what CPython 3.11's `_sre`
//!   matches: ASCII case plus `İ`/`ı` for `i`, `K` (Kelvin) for `k` and `ſ`
//!   for `s` ([`ci_eq`]).
//! * `\d` in `_ISO_DATE_RE` is Unicode `Nd` under Unicode 14 ([`is_decimal`]).
//! * `$` also matches before one trailing newline (`_CODE_FILE_RE`).
//! * Sorts are stable; `sorted()` over `str` is code-point order, which is the
//!   byte order of UTF-8 (`String`'s `Ord`).
//! * `round(x, 3)` rounds the exact binary value half-to-even ([`round3`]).

use std::collections::{BTreeSet, HashMap, HashSet};

use serde_json::{Map, Value, json};

/// `duplicate_candidates(min_jaccard=0.6)`.
const DUPLICATE_MIN_JACCARD: f64 = 0.6;
/// `_DUBIOUS_CONF`.
const DUBIOUS_CONF: f64 = 0.6;
/// `orphans(max_degree=1)`.
const ORPHAN_MAX_DEGREE: usize = 1;

/// An `entities` row as `load_graph` returns it (the keys the analyzer reads).
#[derive(Debug, Clone)]
pub(crate) struct Entity {
    pub id: i64,
    pub canonical: String,
    pub display: String,
}

/// A live `edges` row (`load_graph`), REAL confidence decoded from its text.
#[derive(Debug, Clone)]
pub(crate) struct Edge {
    pub src_id: i64,
    pub relation: String,
    pub dst_id: i64,
    pub confidence: f64,
    pub origin: Option<String>,
}

/// A `pending_proposals` row: the keys `proposed_links` reads.
#[derive(Debug, Clone, Default)]
pub(crate) struct LinkProposal {
    pub id: i64,
    pub src: String,
    pub relation: String,
    pub dst: String,
    pub confidence: f64,
    pub similarity: Option<f64>,
    pub rationale: Option<String>,
    pub source: String,
    pub judge_verdict: Option<String>,
    pub judge_confidence: Option<f64>,
    pub judge_note: Option<String>,
    pub judge_model: Option<String>,
    pub judge_relation: Option<String>,
    pub judge2_verdict: Option<String>,
    pub judge2_confidence: Option<f64>,
    pub judge2_model: Option<String>,
    pub judge2_relation: Option<String>,
}

/// A `pending_entity_proposals` row: the keys `merge_candidates` and
/// `junk_candidates` read. `entity_id` is optional because the service's
/// fold-direction swap can move a NULL `into_id` into it.
#[derive(Debug, Clone, Default)]
pub(crate) struct EntityProposal {
    pub id: i64,
    pub kind: String,
    pub entity_id: Option<i64>,
    pub into_id: Option<i64>,
    pub score: Option<f64>,
    pub reason: Option<String>,
    pub judge_verdict: Option<String>,
    pub judge_confidence: Option<f64>,
    pub judge_note: Option<String>,
    pub judge_model: Option<String>,
    pub judge2_verdict: Option<String>,
    pub judge2_confidence: Option<f64>,
    pub judge2_model: Option<String>,
    pub judge3_verdict: Option<String>,
    pub judge3_confidence: Option<f64>,
    pub judge3_model: Option<String>,
    pub entity: Option<String>,
    pub into: Option<String>,
}

/// `round(x, 3)`: correctly rounded on the exact binary value.
pub(crate) fn round3(x: f64) -> f64 {
    format!("{x:.3}").parse().unwrap_or(x)
}

/// Python truthiness of an optional string (`if p.get("judge_verdict")`).
fn truthy(s: &Option<String>) -> bool {
    s.as_deref().is_some_and(|v| !v.is_empty())
}

/// `re.I` equality of a subject char with a pattern char that is an ASCII
/// lowercase letter, digit or punctuation, as CPython 3.11's `_sre` decides
/// it (enumerated over every code point: only `i`, `k` and `s` gain non-ASCII
/// partners).
fn ci_eq(ch: char, pat: char) -> bool {
    (ch.is_ascii() && ch.to_ascii_lowercase() == pat)
        || match pat {
            'i' => ch == '\u{130}' || ch == '\u{131}',
            'k' => ch == '\u{212a}',
            's' => ch == '\u{17f}',
            _ => false,
        }
}

/// `str.isdecimal()` / `re`'s `\d` on `str` under CPython 3.11 (Unicode 14):
/// general category `Nd`.
fn is_decimal(c: char) -> bool {
    const ND: &[(u32, u32)] = &[
        (0x30, 0x39),
        (0x660, 0x669),
        (0x6f0, 0x6f9),
        (0x7c0, 0x7c9),
        (0x966, 0x96f),
        (0x9e6, 0x9ef),
        (0xa66, 0xa6f),
        (0xae6, 0xaef),
        (0xb66, 0xb6f),
        (0xbe6, 0xbef),
        (0xc66, 0xc6f),
        (0xce6, 0xcef),
        (0xd66, 0xd6f),
        (0xde6, 0xdef),
        (0xe50, 0xe59),
        (0xed0, 0xed9),
        (0xf20, 0xf29),
        (0x1040, 0x1049),
        (0x1090, 0x1099),
        (0x17e0, 0x17e9),
        (0x1810, 0x1819),
        (0x1946, 0x194f),
        (0x19d0, 0x19d9),
        (0x1a80, 0x1a89),
        (0x1a90, 0x1a99),
        (0x1b50, 0x1b59),
        (0x1bb0, 0x1bb9),
        (0x1c40, 0x1c49),
        (0x1c50, 0x1c59),
        (0xa620, 0xa629),
        (0xa8d0, 0xa8d9),
        (0xa900, 0xa909),
        (0xa9d0, 0xa9d9),
        (0xa9f0, 0xa9f9),
        (0xaa50, 0xaa59),
        (0xabf0, 0xabf9),
        (0xff10, 0xff19),
        (0x104a0, 0x104a9),
        (0x10d30, 0x10d39),
        (0x11066, 0x1106f),
        (0x110f0, 0x110f9),
        (0x11136, 0x1113f),
        (0x111d0, 0x111d9),
        (0x112f0, 0x112f9),
        (0x11450, 0x11459),
        (0x114d0, 0x114d9),
        (0x11650, 0x11659),
        (0x116c0, 0x116c9),
        (0x11730, 0x11739),
        (0x118e0, 0x118e9),
        (0x11950, 0x11959),
        (0x11c50, 0x11c59),
        (0x11d50, 0x11d59),
        (0x11da0, 0x11da9),
        (0x16a60, 0x16a69),
        (0x16ac0, 0x16ac9),
        (0x16b50, 0x16b59),
        (0x1d7ce, 0x1d7ff),
        (0x1e140, 0x1e149),
        (0x1e2f0, 0x1e2f9),
        (0x1e950, 0x1e959),
        (0x1fbf0, 0x1fbf9),
    ];
    let c = c as u32;
    ND.iter().any(|&(lo, hi)| (lo..=hi).contains(&c))
}

/// `re.split(r"[^a-z0-9]+", s)` minus the empty pieces.
fn alnum_tokens(s: &str) -> impl Iterator<Item = &str> {
    s.split(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit()))
        .filter(|t| !t.is_empty())
}

/// `_token_set`: lowercased `[a-z0-9]` runs longer than two characters or
/// carrying a digit.
pub(crate) fn token_set(name: &str) -> BTreeSet<String> {
    alnum_tokens(&crate::storage::graph::py_lower(name))
        .filter(|t| t.len() > 2 || t.bytes().any(|b| b.is_ascii_digit()))
        .map(str::to_string)
        .collect()
}

// ── merge vetoes ─────────────────────────────────────────────────────────

/// `_EVENT_TOKEN_RE.match` on an `[a-z0-9]` token: letters, then exactly a
/// `20YYMMDD` date or an `MMDD` run tag.
fn event_token(t: &str) -> bool {
    let digits = t.trim_start_matches(|c: char| c.is_ascii_lowercase());
    let d = digits.as_bytes();
    if !d.iter().all(u8::is_ascii_digit) {
        return false;
    }
    match d.len() {
        8 => d[0] == b'2' && d[1] == b'0' && matches!(d[4], b'0' | b'1') && d[6] <= b'3',
        4 => {
            let month = (d[0] == b'0' && d[1] != b'0') || (d[0] == b'1' && d[1] <= b'2');
            let day = d[2] <= b'2' || (d[2] == b'3' && d[3] <= b'1');
            month && day
        }
        _ => false,
    }
}

/// `_ISO_DATE_RE` (`20\d{2}[-_.][01]\d[-_.][0-3]\d`) at the start of `c`.
fn iso_date_at(c: &[char]) -> bool {
    let sep = |x: char| matches!(x, '-' | '_' | '.');
    c.len() >= 10
        && c[0] == '2'
        && c[1] == '0'
        && is_decimal(c[2])
        && is_decimal(c[3])
        && sep(c[4])
        && matches!(c[5], '0' | '1')
        && is_decimal(c[6])
        && sep(c[7])
        && matches!(c[8], '0'..='3')
        && is_decimal(c[9])
}

/// `_veto_tokens`: `(tokens, sluggy)` with ISO dates stripped whole first.
fn veto_tokens(name: &str) -> (BTreeSet<String>, bool) {
    let chars: Vec<char> = crate::storage::graph::py_lower(name).chars().collect();
    let mut stripped = String::new();
    let mut sluggy = false;
    let mut i = 0;
    while i < chars.len() {
        if iso_date_at(&chars[i..]) {
            sluggy = true;
            stripped.push(' ');
            i += 10;
        } else {
            stripped.push(chars[i]);
            i += 1;
        }
    }
    let toks: BTreeSet<String> = alnum_tokens(&stripped).map(str::to_string).collect();
    let dated = sluggy || toks.iter().any(|t| event_token(t));
    (toks, dated)
}

/// `merge_veto`: `"event-slug"`, `"numeric-substitution"` or `None`.
pub(crate) fn merge_veto(name_a: &str, name_b: &str) -> Option<&'static str> {
    let ((ta, ea), (tb, eb)) = (veto_tokens(name_a), veto_tokens(name_b));
    if ta.is_empty() || tb.is_empty() || ta == tb {
        return None;
    }
    if ea != eb {
        let (dated, plain) = if ea { (&ta, &tb) } else { (&tb, &ta) };
        let kept: BTreeSet<String> = dated.iter().filter(|t| !event_token(t)).cloned().collect();
        return (kept != *plain).then_some("event-slug");
    }
    let a_only: BTreeSet<&String> = ta.difference(&tb).collect();
    let b_only: BTreeSet<&String> = tb.difference(&ta).collect();
    if a_only.is_empty() || b_only.is_empty() {
        return None;
    }
    let single = |s: &BTreeSet<&String>, w: &str| s.len() == 1 && s.iter().all(|t| *t == w);
    if (single(&a_only, "pre") && single(&b_only, "post"))
        || (single(&a_only, "post") && single(&b_only, "pre"))
    {
        return Some("numeric-substitution");
    }
    let numeric = |s: &BTreeSet<&String>| -> Vec<String> {
        // `stem`: sorted, digits removed, duplicates kept.
        let mut out: Vec<String> = s
            .iter()
            .filter(|t| t.bytes().any(|b| b.is_ascii_digit()))
            .map(|t| t.chars().filter(|c| !c.is_ascii_digit()).collect())
            .collect();
        out.sort();
        out
    };
    let (na, nb) = (numeric(&a_only), numeric(&b_only));
    (!na.is_empty() && !nb.is_empty() && na == nb).then_some("numeric-substitution")
}

// ── file / concept pairs ─────────────────────────────────────────────────

/// `_CODE_FILE_RE` extensions.
const CODE_EXTS: &[&str] = &[
    "py", "js", "mjs", "ts", "tsx", "jsx", "ps1", "sh", "bat", "rb", "go", "rs", "java", "c", "cc",
    "cpp", "h", "hpp",
];

/// `_CODE_FILE_RE.search(s)`: the byte span of `.ext` when `s` ends with a
/// code extension (case-insensitively), or with one followed by a single
/// trailing newline (`$`). At most one extension can match.
fn code_file_span(s: &str) -> Option<(usize, usize)> {
    let body = s.strip_suffix('\n').unwrap_or(s);
    for ext in CODE_EXTS {
        let mut rev = body.char_indices().rev();
        let mut ok = true;
        for p in ext.chars().rev() {
            match rev.next() {
                Some((_, ch)) if ci_eq(ch, p) => {}
                _ => {
                    ok = false;
                    break;
                }
            }
        }
        if ok && let Some((dot, '.')) = rev.next() {
            return Some((dot, body.len()));
        }
    }
    None
}

/// `_stem_key`: the last path segment, lowercased, `[a-z0-9]` only.
fn stem_key(name: &str) -> String {
    let last = name.rsplit('/').next().unwrap_or(name);
    crate::storage::graph::py_lower(last)
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        .collect()
}

/// Shell-command prefixes `relation_quality.infer_type` types as concepts.
const CMD_PREFIXES: &[&str] = &[
    "docker compose",
    "docker ",
    "git ",
    "pip ",
    "npm ",
    "curl ",
    "kubectl ",
    "psql ",
    "pg_dump",
    "wsl ",
];
/// `relation_quality._FILE_EXT`.
const FILE_EXT: &[&str] = &[
    ".py", ".yaml", ".yml", ".json", ".md", ".txt", ".sql", ".ps1", ".sh", ".gguf", ".toml",
    ".ini", ".cfg", ".fx",
];
/// `relation_quality._BRANCH_PREFIX` alternatives.
const BRANCH_PREFIXES: &[&str] = &[
    "feat", "feature", "fix", "hotfix", "bugfix", "chore", "ci", "build", "perf", "refactor",
    "style", "release", "revert", "eval", "spike", "exp",
];

/// `infer_type(name) == "concept"` for a name that contains `/`, the only
/// case `file_concept_split` asks. The bare-version rule
/// (`v?\d+(?:\.\d+)*`) and the `branch`/`master`/`main` names cannot hold
/// for such a name, so the reachable concept rules are the command prefixes,
/// `schema*`, and a git-flow branch prefix that no file suffix shadows.
fn slash_name_is_concept(name: &str) -> bool {
    let n = crate::storage::graph::py_lower(crate::storage::py_strip(name));
    if n.is_empty() {
        return false;
    }
    if CMD_PREFIXES.iter().any(|p| n.starts_with(p)) || n.starts_with("schema") {
        return true;
    }
    if FILE_EXT.iter().any(|e| n.ends_with(e)) {
        return false;
    }
    // `^(feat|...)/.+`: `.` is anything but a newline.
    BRANCH_PREFIXES.iter().any(|p| {
        n.strip_prefix(p)
            .and_then(|r| r.strip_prefix('/'))
            .and_then(|r| r.chars().next())
            .is_some_and(|c| c != '\n')
    })
}

/// `file_concept_split`: `(file, concept)` when exactly one name is a source
/// file and the other its own bare stem.
pub(crate) fn file_concept_split<'a>(a: &'a str, b: &'a str) -> Option<(&'a str, &'a str)> {
    for (f, c) in [(a, b), (b, a)] {
        let Some((start, end)) = code_file_span(f) else {
            continue;
        };
        if code_file_span(c).is_some() {
            continue;
        }
        if c.contains('/') && slash_name_is_concept(c) {
            continue;
        }
        let bare = format!("{}{}", &f[..start], &f[end..]);
        if stem_key(&bare) == stem_key(c) {
            return Some((f, c));
        }
    }
    None
}

// ── analyzers ────────────────────────────────────────────────────────────

/// `duplicate_candidates(entities, dismissed=..., lesson_ids=...)` with the
/// default threshold over every pair. `dismissed` holds the stored
/// `(a_norm, b_norm)` tuples; a pair matches only in sorted order.
pub(crate) fn duplicate_candidates(
    entities: &[Entity],
    dismissed: &HashSet<(String, String)>,
    lesson_ids: &HashSet<i64>,
) -> Vec<Map<String, Value>> {
    // Tokens interned to ids so the all-pairs Jaccard is a sorted merge.
    let mut intern: HashMap<String, u32> = HashMap::new();
    let toks: Vec<(&Entity, Vec<u32>)> = entities
        .iter()
        .filter(|e| !lesson_ids.contains(&e.id))
        .map(|e| {
            let mut ids: Vec<u32> = token_set(&e.display)
                .into_iter()
                .map(|t| {
                    let next = intern.len() as u32;
                    *intern.entry(t).or_insert(next)
                })
                .collect();
            ids.sort_unstable();
            (e, ids)
        })
        .collect();
    let min_jaccard = if crate::mutants::active("mcp-review-dup-threshold") {
        0.5
    } else {
        DUPLICATE_MIN_JACCARD
    };
    let mut out: Vec<(f64, Map<String, Value>)> = Vec::new();
    for i in 0..toks.len() {
        let (ei, a) = (&toks[i].0, &toks[i].1);
        if a.is_empty() {
            continue;
        }
        for (ej, b) in &toks[i + 1..] {
            if b.is_empty() {
                continue;
            }
            let inter = sorted_intersection(a, b);
            let union = a.len() + b.len() - inter;
            // `len(a & b) / len(a | b)`: both exact, one correctly rounded divide.
            let jac = inter as f64 / union as f64;
            if jac < min_jaccard {
                continue;
            }
            let (ca, cb) = (ei.canonical.clone(), ej.canonical.clone());
            let key = if ca <= cb { (ca, cb) } else { (cb, ca) };
            if dismissed.contains(&key) {
                continue;
            }
            let pair = file_concept_split(&ei.display, &ej.display);
            if pair.is_none()
                && merge_veto(&ei.display, &ej.display).is_some()
                && !crate::mutants::active("mcp-review-no-merge-veto")
            {
                continue;
            }
            let (n0, n1) = pair.unwrap_or((&ei.display, &ej.display));
            let score = round3(jac);
            let mut found = Map::new();
            found.insert("type".into(), json!("duplicate"));
            found.insert("severity".into(), json!("warn"));
            found.insert("label".into(), json!(format!("{n0} \u{2194} {n1}")));
            found.insert("entities".into(), json!([n0, n1]));
            found.insert("score".into(), json!(score));
            found.insert(
                "action".into(),
                json!(if pair.is_some() { "relate" } else { "merge" }),
            );
            if pair.is_some() {
                found.insert("suggested_relation".into(), json!("implements"));
            }
            out.push((score, found));
        }
    }
    // `out.sort(key=lambda f: -f["score"])`: stable, highest first.
    out.sort_by(|x, y| y.0.total_cmp(&x.0));
    out.into_iter().map(|(_, f)| f).collect()
}

fn sorted_intersection(a: &[u32], b: &[u32]) -> usize {
    let (mut i, mut j, mut n) = (0, 0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                n += 1;
                i += 1;
                j += 1;
            }
        }
    }
    n
}

/// `graph.degree_counts` over the given edges.
pub(crate) fn degree_counts(edges: &[Edge]) -> std::collections::BTreeMap<i64, usize> {
    let stored: Vec<crate::graph_read::StoredEdge> = edges
        .iter()
        .map(|e| crate::graph_read::StoredEdge {
            src_id: e.src_id,
            dst_id: e.dst_id,
        })
        .collect();
    crate::graph_read::degree_counts(&stored)
}

fn names_finding(
    kind: &str,
    severity: &str,
    label: String,
    names: Vec<String>,
    action: &str,
) -> Map<String, Value> {
    let mut f = Map::new();
    f.insert("type".into(), json!(kind));
    f.insert("severity".into(), json!(severity));
    f.insert("label".into(), json!(label));
    f.insert("entities".into(), json!(names));
    f.insert("action".into(), json!(action));
    f
}

/// `orphans(edges, entities, lesson_ids=...)`.
pub(crate) fn orphans(
    edges: &[Edge],
    entities: &[Entity],
    lesson_ids: &HashSet<i64>,
) -> Vec<Map<String, Value>> {
    let deg = degree_counts(edges);
    let mut names: Vec<String> = entities
        .iter()
        .filter(|e| {
            deg.get(&e.id).copied().unwrap_or(0) <= ORPHAN_MAX_DEGREE && !lesson_ids.contains(&e.id)
        })
        .map(|e| e.display.clone())
        .collect();
    if names.is_empty() {
        return vec![];
    }
    names.sort();
    let label = format!("{} weakly-connected entities", names.len());
    vec![names_finding("orphan", "info", label, names, "review")]
}

/// `dubious_edges(edges, entities)`: agent edges at or below 0.6, a zero
/// confidence reading as 1.0 (`e.get("confidence") or 1.0`).
pub(crate) fn dubious_edges(edges: &[Edge], entities: &[Entity]) -> Vec<Map<String, Value>> {
    let disp: HashMap<i64, &str> = entities
        .iter()
        .map(|e| (e.id, e.display.as_str()))
        .collect();
    let name = |id: i64| {
        disp.get(&id)
            .map_or_else(|| id.to_string(), |d| (*d).to_string())
    };
    let rows: Vec<Value> = edges
        .iter()
        .filter(|e| {
            let conf = if e.confidence == 0.0 {
                1.0
            } else {
                e.confidence
            };
            e.origin.as_deref() == Some("agent") && conf <= DUBIOUS_CONF
        })
        .map(|e| {
            json!({"src": name(e.src_id), "relation": e.relation, "dst": name(e.dst_id),
                   "confidence": e.confidence, "tag": "AMBIGUOUS"})
        })
        .collect();
    if rows.is_empty() {
        return vec![];
    }
    let mut f = Map::new();
    f.insert("type".into(), json!("dubious_edge"));
    f.insert("severity".into(), json!("warn"));
    f.insert(
        "label".into(),
        json!(format!(
            "{} low-confidence / type-suspect edges",
            rows.len()
        )),
    );
    f.insert("edges".into(), json!(rows));
    f.insert("action".into(), json!("prune"));
    vec![f]
}

/// `_TEST_PATTERNS.search(s)`:
/// `(payments?[-/]|pl-healthcheck|deploy-smoke|smoke[-_]?test|noise[ _-]?agent)`, `re.I`.
fn test_pattern(s: &str) -> bool {
    // Each element: the chars it accepts, and whether it is optional.
    type Pat = &'static [(&'static str, bool)];
    const ALTS: &[Pat] = &[
        &[
            ("p", false),
            ("a", false),
            ("y", false),
            ("m", false),
            ("e", false),
            ("n", false),
            ("t", false),
            ("s", true),
            ("-/", false),
        ],
        &[
            ("p", false),
            ("l", false),
            ("-", false),
            ("h", false),
            ("e", false),
            ("a", false),
            ("l", false),
            ("t", false),
            ("h", false),
            ("c", false),
            ("h", false),
            ("e", false),
            ("c", false),
            ("k", false),
        ],
        &[
            ("d", false),
            ("e", false),
            ("p", false),
            ("l", false),
            ("o", false),
            ("y", false),
            ("-", false),
            ("s", false),
            ("m", false),
            ("o", false),
            ("k", false),
            ("e", false),
        ],
        &[
            ("s", false),
            ("m", false),
            ("o", false),
            ("k", false),
            ("e", false),
            ("-_", true),
            ("t", false),
            ("e", false),
            ("s", false),
            ("t", false),
        ],
        &[
            ("n", false),
            ("o", false),
            ("i", false),
            ("s", false),
            ("e", false),
            (" _-", true),
            ("a", false),
            ("g", false),
            ("e", false),
            ("n", false),
            ("t", false),
        ],
    ];
    fn at(c: &[char], pat: Pat) -> bool {
        let Some(((accept, optional), rest)) = pat.split_first() else {
            return true;
        };
        (c.first()
            .is_some_and(|&ch| accept.chars().any(|p| ci_eq(ch, p)))
            && at(&c[1..], rest))
            || (*optional && at(c, rest))
    }
    let chars: Vec<char> = s.chars().collect();
    (0..chars.len()).any(|i| ALTS.iter().any(|alt| at(&chars[i..], alt)))
}

/// `test_artifacts(entities)`.
pub(crate) fn test_artifacts(entities: &[Entity]) -> Vec<Map<String, Value>> {
    let mut names: Vec<String> = entities
        .iter()
        .filter(|e| test_pattern(&e.display))
        .map(|e| e.display.clone())
        .collect();
    if names.is_empty() {
        return vec![];
    }
    names.sort();
    let label = format!("{} test/smoke artifacts", names.len());
    vec![names_finding(
        "test_artifact",
        "warn",
        label,
        names,
        "delete",
    )]
}

/// `lesson_only_ids(edges, lesson_entity_ids)`: endpoints whose every edge is
/// `prefers`/`avoids`, plus the lesson-referenced ids.
pub(crate) fn lesson_only_ids(edges: &[Edge], lesson_entity_ids: &HashSet<i64>) -> HashSet<i64> {
    let mut rels: HashMap<i64, bool> = HashMap::new();
    for e in edges {
        let lesson = e.relation == "prefers" || e.relation == "avoids";
        for id in [e.src_id, e.dst_id] {
            let all = rels.entry(id).or_insert(true);
            *all = *all && lesson;
        }
    }
    let mut out: HashSet<i64> = rels
        .into_iter()
        .filter(|&(_, all)| all)
        .map(|(id, _)| id)
        .collect();
    out.extend(lesson_entity_ids.iter().copied());
    out
}

/// `unattributed(entities, entity_sources_map, edges, lesson_entity_ids)`.
pub(crate) fn unattributed(
    entities: &[Entity],
    attributed: &HashSet<i64>,
    edges: &[Edge],
    lesson_entity_ids: &HashSet<i64>,
) -> Vec<Map<String, Value>> {
    let lesson_only = lesson_only_ids(edges, lesson_entity_ids);
    let mut names: Vec<String> = entities
        .iter()
        .filter(|e| !attributed.contains(&e.id) && !lesson_only.contains(&e.id))
        .map(|e| e.display.clone())
        .collect();
    if names.is_empty() {
        return vec![];
    }
    names.sort();
    let label = format!("{} entities with no project", names.len());
    vec![names_finding(
        "unattributed",
        "info",
        label,
        names,
        "assign",
    )]
}

/// `proposed_links(proposals)`. Every row is tagged AMBIGUOUS: the rows carry
/// no `origin`, and `classify_edge(p, proposed=True)` short-circuits there.
pub(crate) fn proposed_links(proposals: &[LinkProposal]) -> Vec<Map<String, Value>> {
    if proposals.is_empty() {
        return vec![];
    }
    let links: Vec<Value> = proposals
        .iter()
        .map(|p| {
            let mut row = Map::new();
            row.insert("id".into(), json!(p.id));
            row.insert("src".into(), json!(p.src));
            row.insert("relation".into(), json!(p.relation));
            row.insert("dst".into(), json!(p.dst));
            row.insert("confidence".into(), json!(p.confidence));
            row.insert("similarity".into(), json!(p.similarity));
            row.insert("rationale".into(), json!(p.rationale));
            row.insert("source".into(), json!(p.source));
            row.insert("tag".into(), json!("AMBIGUOUS"));
            if truthy(&p.judge_verdict) {
                row.insert(
                    "judge".into(),
                    json!({"verdict": p.judge_verdict, "confidence": p.judge_confidence,
                           "note": p.judge_note, "model": p.judge_model,
                           "relation": p.judge_relation}),
                );
            }
            if truthy(&p.judge2_verdict) {
                row.insert(
                    "judge2".into(),
                    json!({"verdict": p.judge2_verdict, "confidence": p.judge2_confidence,
                           "model": p.judge2_model, "relation": p.judge2_relation}),
                );
            }
            Value::Object(row)
        })
        .collect();
    let mut f = Map::new();
    f.insert("type".into(), json!("proposed_link"));
    f.insert("severity".into(), json!("info"));
    f.insert("action".into(), json!("review"));
    f.insert(
        "label".into(),
        json!(format!("{} proposed cross-session links", links.len())),
    );
    f.insert("links".into(), json!(links));
    vec![f]
}

/// The maximal `[a-z0-9-]` prefix of `s`.
fn relation_run(s: &str) -> &str {
    let end = s
        .find(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-'))
        .unwrap_or(s.len());
    &s[..end]
}

/// `re.search(r"\): relate:([a-z0-9-]+) ", seg)`.
fn relate_tail(seg: &str) -> Option<String> {
    const MARK: &str = "): relate:";
    seg.match_indices(MARK).find_map(|(i, _)| {
        let rest = &seg[i + MARK.len()..];
        let run = relation_run(rest);
        (!run.is_empty() && rest[run.len()..].starts_with(' ')).then(|| run.to_string())
    })
}

/// `relate_relations(note)`: the first and second opinions' relations.
pub(crate) fn relate_relations(note: Option<&str>) -> (Option<String>, Option<String>) {
    let note = note.unwrap_or("");
    let head = note
        .strip_prefix("relate:")
        .map(relation_run)
        .filter(|r| !r.is_empty())
        .map(str::to_string);
    let tail = note.rfind("| 2nd (").and_then(|cut| {
        let rest = &note[cut..];
        let seg = rest.find("| 3rd (").map_or(rest, |end| &rest[..end]);
        relate_tail(seg)
    });
    (head, tail)
}

/// `third_relate_relation(note)`.
pub(crate) fn third_relate_relation(note: Option<&str>) -> Option<String> {
    let note = note.unwrap_or("");
    note.rfind("| 3rd (")
        .and_then(|cut| relate_tail(&note[cut..]))
}

/// A merge row's grouping key: its id, or (id-less) its display name.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum Key {
    Id(i64),
    Name(Option<String>),
}

/// `(-counts[a], a) > (-counts[b], b)`, or the TypeError Python raises when
/// the tie reaches keys it cannot order.
fn key_greater(ca: usize, a: &Key, cb: usize, b: &Key) -> Result<bool, String> {
    if ca != cb {
        return Ok(ca < cb);
    }
    if a == b {
        return Ok(false);
    }
    match (a, b) {
        (Key::Id(x), Key::Id(y)) => Ok(x > y),
        (Key::Name(Some(x)), Key::Name(Some(y))) => Ok(x > y),
        _ => Err("'<' not supported between merge group keys of different types".into()),
    }
}

/// `shared_pair_groups(pairs)`.
fn shared_pair_groups(pairs: &[(Key, Key)]) -> Result<Vec<Option<Key>>, String> {
    let mut counts: HashMap<&Key, usize> = HashMap::new();
    for (a, b) in pairs {
        *counts.entry(a).or_default() += 1;
        *counts.entry(b).or_default() += 1;
    }
    let mut out = Vec::new();
    for (a, b) in pairs {
        let shared: Vec<&Key> = [a, b].into_iter().filter(|k| counts[k] > 1).collect();
        // `min(shared, key=lambda i: (-counts[i], i))`: the first unless the
        // second is strictly smaller.
        let pick = match shared.as_slice() {
            [] => None,
            [only] => Some((*only).clone()),
            [x, y] => Some(if key_greater(counts[x], x, counts[y], y)? {
                (*y).clone()
            } else {
                (*x).clone()
            }),
            _ => unreachable!(),
        };
        out.push(pick);
    }
    Ok(out)
}

/// `merge_candidates(entity_proposals)`. `Err` carries the TypeError a
/// mixed-type group tie raises in Python.
pub(crate) fn merge_candidates(rows: &[EntityProposal]) -> Result<Vec<Map<String, Value>>, String> {
    let rows: Vec<&EntityProposal> = rows.iter().filter(|p| p.kind == "merge").collect();
    if rows.is_empty() {
        return Ok(vec![]);
    }
    let key = |id: Option<i64>, name: &Option<String>| match id {
        Some(id) => Key::Id(id),
        None => Key::Name(name.clone()),
    };
    let mut disp: HashMap<Key, Option<String>> = HashMap::new();
    for p in &rows {
        disp.insert(key(p.entity_id, &p.entity), p.entity.clone());
    }
    for p in &rows {
        disp.insert(key(p.into_id, &p.into), p.into.clone());
    }
    let pairs: Vec<(Key, Key)> = rows
        .iter()
        .map(|p| (key(p.entity_id, &p.entity), key(p.into_id, &p.into)))
        .collect();
    let groups = shared_pair_groups(&pairs)?;
    let mut merges = Vec::new();
    for (p, g) in rows.iter().zip(groups) {
        let mut m = Map::new();
        m.insert("from".into(), json!(p.entity));
        m.insert("into".into(), json!(p.into));
        m.insert("similarity".into(), json!(p.score));
        m.insert("reason".into(), json!(p.reason));
        m.insert("id".into(), json!(p.id));
        m.insert(
            "group".into(),
            json!(g.and_then(|g| disp.get(&g).cloned().flatten())),
        );
        let (first, second) = relate_relations(p.judge_note.as_deref());
        let third = third_relate_relation(p.judge_note.as_deref());
        let block =
            |verdict: &Option<String>, fields: Vec<(&str, Value)>, relation: Option<String>| {
                let mut b = Map::new();
                b.insert("verdict".into(), json!(verdict));
                for (k, v) in fields {
                    b.insert(k.into(), v);
                }
                // `add_relate_relations`: stamped last, on a relate verdict only.
                if verdict.as_deref() == Some("relate")
                    && let Some(r) = relation
                {
                    b.insert("relation".into(), json!(r));
                }
                Value::Object(b)
            };
        if truthy(&p.judge_verdict) {
            m.insert(
                "judge".into(),
                block(
                    &p.judge_verdict,
                    vec![
                        ("confidence", json!(p.judge_confidence)),
                        ("note", json!(p.judge_note)),
                        ("model", json!(p.judge_model)),
                    ],
                    first,
                ),
            );
        }
        if truthy(&p.judge2_verdict) {
            m.insert(
                "judge2".into(),
                block(
                    &p.judge2_verdict,
                    vec![
                        ("confidence", json!(p.judge2_confidence)),
                        ("model", json!(p.judge2_model)),
                    ],
                    second,
                ),
            );
        }
        if truthy(&p.judge3_verdict) {
            m.insert(
                "judge3".into(),
                block(
                    &p.judge3_verdict,
                    vec![
                        ("confidence", json!(p.judge3_confidence)),
                        ("model", json!(p.judge3_model)),
                    ],
                    third,
                ),
            );
        }
        merges.push(Value::Object(m));
    }
    let mut f = Map::new();
    f.insert("type".into(), json!("merge_candidate"));
    f.insert("severity".into(), json!("warn"));
    f.insert("action".into(), json!("merge"));
    f.insert(
        "label".into(),
        json!(format!("{} near-duplicate entity merges", merges.len())),
    );
    f.insert("merges".into(), json!(merges));
    Ok(vec![f])
}

/// `junk_candidates(entity_proposals)`.
pub(crate) fn junk_candidates(rows: &[EntityProposal]) -> Vec<Map<String, Value>> {
    let items: Vec<Value> = rows
        .iter()
        .filter(|p| p.kind == "junk")
        .map(|p| json!({"entity": p.entity, "reason": p.reason, "id": p.id}))
        .collect();
    if items.is_empty() {
        return vec![];
    }
    let mut f = Map::new();
    f.insert("type".into(), json!("junk_candidate"));
    f.insert("severity".into(), json!("warn"));
    f.insert("action".into(), json!("delete"));
    f.insert(
        "label".into(),
        json!(format!("{} junk entities to prune", items.len())),
    );
    f.insert("entities".into(), json!(items));
    vec![f]
}

/// The inputs `review` takes.
pub(crate) struct ReviewInput<'a> {
    pub edges: &'a [Edge],
    pub entities: &'a [Entity],
    /// The ids `entity_sources_map` carries (the analyzer tests membership).
    pub attributed: &'a HashSet<i64>,
    pub proposals: &'a [LinkProposal],
    pub entity_proposals: &'a [EntityProposal],
    pub dismissed: &'a HashSet<(String, String)>,
    pub lesson_entity_ids: &'a HashSet<i64>,
}

/// `review(...)`: `{"findings": [...], "counts": {"total": n}}`.
pub(crate) fn review(input: &ReviewInput<'_>) -> Result<Map<String, Value>, String> {
    let lesson_ids = lesson_only_ids(input.edges, input.lesson_entity_ids);
    let mut findings = duplicate_candidates(input.entities, input.dismissed, &lesson_ids);
    findings.extend(test_artifacts(input.entities));
    findings.extend(dubious_edges(input.edges, input.entities));
    findings.extend(orphans(input.edges, input.entities, &lesson_ids));
    findings.extend(unattributed(
        input.entities,
        input.attributed,
        input.edges,
        input.lesson_entity_ids,
    ));
    findings.extend(proposed_links(input.proposals));
    findings.extend(merge_candidates(input.entity_proposals)?);
    findings.extend(junk_candidates(input.entity_proposals));
    let total = findings.len();
    let mut out = Map::new();
    out.insert(
        "findings".into(),
        Value::Array(findings.into_iter().map(Value::Object).collect()),
    );
    out.insert("counts".into(), json!({"total": total}));
    Ok(out)
}

#[cfg(test)]
mod tests {
    //! Expected values are the oracle's own output: each fixture was run
    //! through `pseudolife_memory.memory.graph_review` (CPython 3.11.9) with
    //! the same inputs and the result pasted here as compact JSON.
    use super::*;

    fn compact(v: &impl serde::Serialize) -> String {
        serde_json::to_string(v).unwrap()
    }

    fn ent(id: i64, canonical: &str, display: &str) -> Entity {
        Entity {
            id,
            canonical: canonical.into(),
            display: display.into(),
        }
    }

    fn edge(
        src_id: i64,
        dst_id: i64,
        relation: &str,
        confidence: f64,
        origin: Option<&str>,
    ) -> Edge {
        Edge {
            src_id,
            relation: relation.into(),
            dst_id,
            confidence,
            origin: origin.map(str::to_string),
        }
    }

    fn s(v: &str) -> Option<String> {
        Some(v.to_string())
    }

    #[test]
    fn merge_veto_matches_python() {
        let pairs = [
            ("CT200 host", "CT400 host"),
            ("alpha beta gamma ct200", "alpha beta gamma ct400"),
            ("notes 2026-08-05", "notes"),
            ("notes-20260805", "notes-20260807"),
            ("project x", "project x 0806"),
            ("big project", "event 0806"),
            ("pre-deploy check", "post-deploy check"),
            ("v2.0.0", "v2"),
            ("CT300 Local-Models", "CT200"),
            ("pgvector 0.8.5", "pgvector 0.8.6"),
            ("same", "SAME"),
            ("1024 port", "1230 port"),
            // Arabic-Indic digits are `\d` (Nd) for the ISO-date strip.
            ("x 20\u{662}\u{666}-08-05 y", "x y"),
            ("x 20\u{662}\u{666}-08-05 y", "x z"),
            ("smoke0726 run", "run"),
            ("", "a"),
            ("a1b2 q", "a3b4 q"),
        ];
        let got: Vec<Option<&str>> = pairs.iter().map(|(a, b)| merge_veto(a, b)).collect();
        assert_eq!(compact(&got), VETO);
    }

    #[test]
    fn file_concept_split_matches_python() {
        let pairs = [
            ("band.py", "band"),
            ("band", "band.py"),
            ("evals/dg_shim.py", "dg_shim"),
            ("test_shim.py", "tests/test_shim.py"),
            ("README.md", "README"),
            ("terra_shim.py", "feat/terra-shim"),
            ("Band.PY", "band"),
            // `$` also matches before a trailing newline.
            ("band.py\n", "band"),
            ("x.cpp", "X"),
            // `re.I` matches the long s for `s`.
            ("tool.\u{17f}h", "tool"),
            ("a.tsx", "a.ts"),
            ("ops/backup.ps1", "Backup"),
            ("lib.rs", "docs/lib"),
            ("lib.rs", "git x/lib"),
            ("lib.rs", "feat/\nlib"),
            ("lib.rs", "schema/lib"),
            ("lib.rs", "fix/lib.md"),
        ];
        let got: Vec<Option<(&str, &str)>> = pairs
            .iter()
            .map(|(a, b)| file_concept_split(a, b))
            .collect();
        assert_eq!(compact(&got), FCS);
    }

    #[test]
    fn token_set_matches_python() {
        let names = [
            "Hello World-12 ab a1",
            "\u{130}stanbul \u{212a}ELVIN",
            "x_y.z 2026-01-01",
            "\u{c0}\u{c9} t\u{f4}kens",
            "",
        ];
        let got: Vec<Vec<String>> = names
            .iter()
            .map(|n| token_set(n).into_iter().collect())
            .collect();
        assert_eq!(compact(&got), TOKS);
    }

    #[test]
    fn test_artifacts_match_python_ignorecase() {
        let names = [
            "payments-api",
            "PAYMENT/x",
            "pl-healthcheck",
            "noiseagent",
            "Noise Agent",
            "smoke_test",
            "smoke__test",
            "deploy-smoke",
            "\u{17f}moke-test",
            "no\u{130}se agent",
            "pl-healthchec\u{212a}",
            "pl-healthchecK",
            "payment",
            "pay ments-",
        ];
        let ents: Vec<Entity> = names
            .iter()
            .enumerate()
            .map(|(i, n)| ent(i as i64, "", n))
            .collect();
        assert_eq!(compact(&test_artifacts(&ents)), TEST);
    }

    #[test]
    fn relate_relations_match_python() {
        let notes: [Option<&str>; 10] = [
            None,
            Some(""),
            Some("relate:uses | because"),
            Some("relate:depends-on"),
            Some("relate: x"),
            Some("foo | 2nd (m): relate:uses 0.9"),
            Some("relate:a | n | 2nd (m1): reject 0.5 | 3rd (m2): relate:part-of 0.7"),
            Some("| 2nd (m): relate:uses 0.9 | 3rd (x): relate:b 0.1"),
            Some("| 2nd (m): relate:x| 2nd (n): relate:y 0.2"),
            Some("| 2nd (m): relate:x0.1 then ): relate:z 1"),
        ];
        let got: Vec<Value> = notes
            .iter()
            .map(|n| json!([relate_relations(*n), third_relate_relation(*n)]))
            .collect();
        assert_eq!(compact(&got), REL);
    }

    #[test]
    fn round3_matches_python_round() {
        let xs = [
            11.0 / 16.0,
            13.0 / 16.0,
            2.0 / 3.0,
            0.6,
            5.0 / 8.0,
            0.0005,
            0.0015,
            1.0 / 3.0,
        ];
        let got: Vec<f64> = xs.iter().map(|&x| round3(x)).collect();
        assert_eq!(compact(&got), ROUND);
    }

    fn tie_rows(pairs: &[(Option<i64>, Option<i64>, &str, &str)]) -> Vec<EntityProposal> {
        pairs
            .iter()
            .enumerate()
            .map(|(i, &(a, b, an, bn))| EntityProposal {
                id: i as i64,
                kind: "merge".into(),
                entity_id: a,
                into_id: b,
                entity: s(an),
                into: s(bn),
                ..Default::default()
            })
            .collect()
    }

    #[test]
    fn merge_groups_match_python() {
        // Three rows sharing endpoints pairwise: every count ties at 2, so
        // the lower id wins; the fourth row shares nothing.
        let rows = tie_rows(&[
            (Some(3), Some(2), "n3", "n2"),
            (Some(2), Some(1), "n2", "n1"),
            (Some(1), Some(3), "n1", "n3"),
            (Some(7), Some(8), "n7", "n8"),
        ]);
        assert_eq!(compact(&merge_candidates(&rows).unwrap()), TIE);
        // Id-less rows group on display names (str order on the tie).
        let mut rows = tie_rows(&[
            (None, None, "b", "a"),
            (None, None, "a", "c"),
            (None, None, "c", "b"),
        ]);
        for (p, id) in rows.iter_mut().zip(1..) {
            p.id = id;
        }
        assert_eq!(compact(&merge_candidates(&rows).unwrap()), NAMES);
        // Python cannot order an id against a NULL-named key on a tie.
        let mut rows = tie_rows(&[(Some(1), None, "n1", ""), (None, Some(1), "", "n1")]);
        rows[0].into = None;
        rows[1].entity = None;
        assert!(merge_candidates(&rows).is_err());
    }

    #[test]
    fn review_matches_python() {
        let common: Vec<String> = (1..12).map(|i| format!("c{i:02}")).collect();
        let twelve = format!("{} aa1 aa2", common.join(" "));
        let thirteen = format!("{} bb1 bb2 bb3", common.join(" "));
        let entities = vec![
            ent(1, "band-py", "band.py"),
            ent(2, "band", "band"),
            ent(3, "abg-ct200", "alpha beta gamma ct200"),
            ent(4, "abg-ct400", "alpha beta gamma ct400"),
            ent(5, "abg-delta", "alpha beta gamma delta"),
            ent(6, "memory-daemon-service", "Memory Daemon Service"),
            ent(7, "memory-daemon-service-2", "memory daemon service"),
            ent(8, "lesson-task-approach", "lesson task approach"),
            ent(9, "lesson-task-approach-x", "lesson task approach x"),
            ent(10, "payments-test", "payments-test"),
            ent(11, "a-b", "a b"),
            ent(12, "twelve", &twelve),
            ent(13, "thirteen", &thirteen),
            ent(14, "zeta-one", "zeta one 0.8.5"),
            ent(15, "zeta-two", "zeta one 0.8.6"),
        ];
        let edges = vec![
            edge(1, 2, "related-to", 0.8, Some("user")),
            edge(3, 5, "uses", 0.5, Some("agent")),
            edge(8, 9, "prefers", 0.9, Some("agent")),
            edge(9, 10, "uses", 0.0, Some("agent")),
            edge(12, 13, "related-to", 0.6, Some("agent")),
            edge(6, 6, "related-to", 0.7, None),
            edge(99, 3, "uses", 0.2, Some("agent")),
        ];
        let attributed: HashSet<i64> = [1, 2, 3, 12].into();
        let proposals = vec![
            LinkProposal {
                id: 7,
                src: "band".into(),
                relation: "uses".into(),
                dst: "alpha beta gamma delta".into(),
                confidence: 0.9,
                similarity: Some(0.71),
                rationale: s("r"),
                source: "deep-dream".into(),
                judge_verdict: s("accept"),
                judge_confidence: Some(0.8),
                judge_note: s("ok"),
                judge_model: s("m1"),
                judge_relation: None,
                judge2_verdict: s("retype"),
                judge2_confidence: Some(0.6),
                judge2_model: s("m2"),
                judge2_relation: s("part-of"),
            },
            LinkProposal {
                id: 3,
                src: "x".into(),
                relation: "related-to".into(),
                dst: "y".into(),
                confidence: 0.4,
                source: "analyzer".into(),
                judge_verdict: s(""),
                ..Default::default()
            },
        ];
        let entity_proposals = vec![
            EntityProposal {
                id: 1,
                kind: "junk".into(),
                entity_id: Some(10),
                reason: s("test"),
                entity: s("payments-test"),
                ..Default::default()
            },
            EntityProposal {
                id: 2,
                kind: "merge".into(),
                entity_id: Some(3),
                into_id: Some(5),
                score: Some(0.6),
                reason: s("dup"),
                entity: s("alpha beta gamma ct200"),
                into: s("alpha beta gamma delta"),
                judge_verdict: s("relate"),
                judge_confidence: Some(0.7),
                judge_note: s("relate:uses | close | 2nd (m2): relate:part-of 0.6 \
                     | 3rd (m3): relate:depends-on 0.5"),
                judge_model: s("m1"),
                judge2_verdict: s("relate"),
                judge2_confidence: Some(0.6),
                judge2_model: s("m2"),
                judge3_verdict: s("relate"),
                judge3_confidence: Some(0.5),
                judge3_model: s("m3"),
            },
            EntityProposal {
                id: 5,
                kind: "merge".into(),
                entity_id: Some(5),
                into_id: Some(4),
                score: Some(0.5),
                entity: s("alpha beta gamma delta"),
                into: s("alpha beta gamma ct400"),
                judge_verdict: s("reject"),
                judge_confidence: Some(0.9),
                judge_note: s("no"),
                judge_model: s("m1"),
                judge2_verdict: s("relate"),
                judge2_confidence: Some(0.4),
                judge2_model: s("m2"),
                ..Default::default()
            },
            EntityProposal {
                id: 6,
                kind: "merge".into(),
                entity_id: Some(12),
                into_id: Some(13),
                reason: s("x"),
                entity: s("e12"),
                into: s("e13"),
                judge_verdict: s(""),
                ..Default::default()
            },
        ];
        let dismissed: HashSet<(String, String)> = [(
            "memory-daemon-service".to_string(),
            "memory-daemon-service-2".to_string(),
        )]
        .into();
        let lesson_entity_ids: HashSet<i64> = [11].into();
        let out = review(&ReviewInput {
            edges: &edges,
            entities: &entities,
            attributed: &attributed,
            proposals: &proposals,
            entity_proposals: &entity_proposals,
            dismissed: &dismissed,
            lesson_entity_ids: &lesson_entity_ids,
        })
        .unwrap();
        assert_eq!(compact(&out), REVIEW);
    }

    const VETO: &str = r#"["numeric-substitution","numeric-substitution",null,"numeric-substitution",null,"event-slug","numeric-substitution",null,"numeric-substitution","numeric-substitution",null,"numeric-substitution",null,"event-slug",null,null,"numeric-substitution"]"#;
    const FCS: &str = r#"[["band.py","band"],["band.py","band"],["evals/dg_shim.py","dg_shim"],null,null,null,["Band.PY","band"],["band.py\n","band"],["x.cpp","X"],["tool.ſh","tool"],null,["ops/backup.ps1","Backup"],["lib.rs","docs/lib"],null,["lib.rs","feat/\nlib"],null,null]"#;
    const TOKS: &str =
        r#"[["12","a1","hello","world"],["kelvin","stanbul"],["01","2026"],["kens"],[]]"#;
    const TEST: &str = r#"[{"type":"test_artifact","severity":"warn","label":"11 test/smoke artifacts","entities":["Noise Agent","PAYMENT/x","deploy-smoke","noiseagent","noİse agent","payments-api","pl-healthchecK","pl-healthcheck","pl-healthchecK","smoke_test","ſmoke-test"],"action":"delete"}]"#;
    const REL: &str = r#"[[[null,null],null],[[null,null],null],[["uses",null],null],[["depends-on",null],null],[[null,null],null],[[null,"uses"],null],[["a",null],"part-of"],[[null,"uses"],"b"],[[null,"y"],null],[[null,"z"],null]]"#;
    const ROUND: &str = r#"[0.688,0.812,0.667,0.6,0.625,0.001,0.002,0.333]"#;
    const TIE: &str = r#"[{"type":"merge_candidate","severity":"warn","action":"merge","label":"4 near-duplicate entity merges","merges":[{"from":"n3","into":"n2","similarity":null,"reason":null,"id":0,"group":"n2"},{"from":"n2","into":"n1","similarity":null,"reason":null,"id":1,"group":"n1"},{"from":"n1","into":"n3","similarity":null,"reason":null,"id":2,"group":"n1"},{"from":"n7","into":"n8","similarity":null,"reason":null,"id":3,"group":null}]}]"#;
    const NAMES: &str = r#"[{"type":"merge_candidate","severity":"warn","action":"merge","label":"3 near-duplicate entity merges","merges":[{"from":"b","into":"a","similarity":null,"reason":null,"id":1,"group":"a"},{"from":"a","into":"c","similarity":null,"reason":null,"id":2,"group":"a"},{"from":"c","into":"b","similarity":null,"reason":null,"id":3,"group":"b"}]}]"#;
    const REVIEW: &str = r#"{"findings":[{"type":"duplicate","severity":"warn","label":"band.py ↔ band","entities":["band.py","band"],"score":1.0,"action":"relate","suggested_relation":"implements"},{"type":"duplicate","severity":"warn","label":"c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 aa1 aa2 ↔ c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 bb1 bb2 bb3","entities":["c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 aa1 aa2","c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 bb1 bb2 bb3"],"score":0.688,"action":"merge"},{"type":"duplicate","severity":"warn","label":"alpha beta gamma ct200 ↔ alpha beta gamma delta","entities":["alpha beta gamma ct200","alpha beta gamma delta"],"score":0.6,"action":"merge"},{"type":"duplicate","severity":"warn","label":"alpha beta gamma ct400 ↔ alpha beta gamma delta","entities":["alpha beta gamma ct400","alpha beta gamma delta"],"score":0.6,"action":"merge"},{"type":"test_artifact","severity":"warn","label":"1 test/smoke artifacts","entities":["payments-test"],"action":"delete"},{"type":"dubious_edge","severity":"warn","label":"3 low-confidence / type-suspect edges","edges":[{"src":"alpha beta gamma ct200","relation":"uses","dst":"alpha beta gamma delta","confidence":0.5,"tag":"AMBIGUOUS"},{"src":"c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 aa1 aa2","relation":"related-to","dst":"c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 bb1 bb2 bb3","confidence":0.6,"tag":"AMBIGUOUS"},{"src":"99","relation":"uses","dst":"alpha beta gamma ct200","confidence":0.2,"tag":"AMBIGUOUS"}],"action":"prune"},{"type":"orphan","severity":"info","label":"10 weakly-connected entities","entities":["alpha beta gamma ct400","alpha beta gamma delta","band","band.py","c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 aa1 aa2","c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 bb1 bb2 bb3","memory daemon service","payments-test","zeta one 0.8.5","zeta one 0.8.6"],"action":"review"},{"type":"unattributed","severity":"info","label":"9 entities with no project","entities":["Memory Daemon Service","alpha beta gamma ct400","alpha beta gamma delta","c01 c02 c03 c04 c05 c06 c07 c08 c09 c10 c11 bb1 bb2 bb3","lesson task approach x","memory daemon service","payments-test","zeta one 0.8.5","zeta one 0.8.6"],"action":"assign"},{"type":"proposed_link","severity":"info","action":"review","label":"2 proposed cross-session links","links":[{"id":7,"src":"band","relation":"uses","dst":"alpha beta gamma delta","confidence":0.9,"similarity":0.71,"rationale":"r","source":"deep-dream","tag":"AMBIGUOUS","judge":{"verdict":"accept","confidence":0.8,"note":"ok","model":"m1","relation":null},"judge2":{"verdict":"retype","confidence":0.6,"model":"m2","relation":"part-of"}},{"id":3,"src":"x","relation":"related-to","dst":"y","confidence":0.4,"similarity":null,"rationale":null,"source":"analyzer","tag":"AMBIGUOUS"}]},{"type":"merge_candidate","severity":"warn","action":"merge","label":"3 near-duplicate entity merges","merges":[{"from":"alpha beta gamma ct200","into":"alpha beta gamma delta","similarity":0.6,"reason":"dup","id":2,"group":"alpha beta gamma delta","judge":{"verdict":"relate","confidence":0.7,"note":"relate:uses | close | 2nd (m2): relate:part-of 0.6 | 3rd (m3): relate:depends-on 0.5","model":"m1","relation":"uses"},"judge2":{"verdict":"relate","confidence":0.6,"model":"m2","relation":"part-of"},"judge3":{"verdict":"relate","confidence":0.5,"model":"m3","relation":"depends-on"}},{"from":"alpha beta gamma delta","into":"alpha beta gamma ct400","similarity":0.5,"reason":null,"id":5,"group":"alpha beta gamma delta","judge":{"verdict":"reject","confidence":0.9,"note":"no","model":"m1"},"judge2":{"verdict":"relate","confidence":0.4,"model":"m2"}},{"from":"e12","into":"e13","similarity":null,"reason":"x","id":6,"group":null}]},{"type":"junk_candidate","severity":"warn","action":"delete","label":"1 junk entities to prune","entities":[{"entity":"payments-test","reason":"test","id":1}]}],"counts":{"total":11}}"#;
}
