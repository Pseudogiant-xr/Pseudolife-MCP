# pseudolife_memory/memory/graph_review.py
"""Pure graph-hygiene analyzer (Atlas Stage 3). DB-free + unit-testable like
graph_insight.py: the service supplies edges/entities/entity_sources_map; this
returns review findings the Atlas workbench surfaces. READ-ONLY — no mutation."""
from __future__ import annotations

import re

from pseudolife_memory.graph import degree_counts

_DUBIOUS_CONF = 0.6
_TAG_AMBIGUOUS_CONF = 0.5
_TEST_PATTERNS = re.compile(
    r"(payments?[-/]|pl-healthcheck|deploy-smoke|smoke[-_]?test|noise[ _-]?agent)",
    re.I)


def classify_edge(edge: dict, *, proposed: bool = False) -> str:
    """Three-way provenance tag for an edge (graphify-style).

    EXTRACTED — asserted by a human or a confirming action (origin
    user/action); never demoted by low confidence. AMBIGUOUS — a proposal
    awaiting review, or confidence below 0.5. INFERRED — everything else
    (agent/dream extraction at working confidence)."""
    origin = (edge.get("origin") or "").lower()
    if origin in ("user", "action"):
        return "EXTRACTED"
    conf = edge.get("confidence")
    if proposed or (conf is not None and float(conf) < _TAG_AMBIGUOUS_CONF):
        return "AMBIGUOUS"
    return "INFERRED"


def near_duplicate_names(name, existing, *, min_jaccard=0.6,
                         dismissed=frozenset()):
    """Score a candidate name against existing entities (write-time dedup).

    ``existing`` rows are ``{"id", "canonical", "display", "aliases"}``;
    the best token-Jaccard across canonical/display/aliases wins per entity.
    ``dismissed`` holds sorted (canonical_a, canonical_b) pairs — human-
    settled distinct pairs never match. ``min_jaccard <= 0`` disables.
    Returns ``[{"entity_id", "display", "score"}]`` sorted by score desc."""
    if min_jaccard is None or min_jaccard <= 0:
        return []
    from pseudolife_memory.graph import norm_name
    from pseudolife_memory.memory.graph_consolidation import variant_conflict
    cand_tokens = _token_set(name)
    if not cand_tokens:
        return []
    cand_canon = norm_name(name)
    out = []
    for e in existing:
        if tuple(sorted((cand_canon, e.get("canonical") or ""))) in dismissed:
            continue
        if (variant_conflict(name, e.get("display") or "")
                or variant_conflict(name, e.get("canonical") or "")):
            continue          # size/quant/version mismatch: never a merge
        best = 0.0
        for variant in [e.get("canonical"), e.get("display"),
                        *(e.get("aliases") or [])]:
            toks = _token_set(variant or "")
            if not toks:
                continue
            jac = len(cand_tokens & toks) / len(cand_tokens | toks)
            if jac > best:
                best = jac
        if best >= min_jaccard:
            out.append({"entity_id": e["id"], "display": e.get("display"),
                        "score": round(best, 3)})
    out.sort(key=lambda m: -m["score"])
    return out


def _disp(entities):
    return {e["id"]: e["display"] for e in entities}


def _token_set(name):
    return {t for t in re.split(r"[^a-z0-9]+", str(name).lower())
            if len(t) > 2 or any(c.isdigit() for c in t)}


_CODE_FILE_RE = re.compile(
    r"\.(py|js|mjs|ts|tsx|jsx|ps1|sh|bat|rb|go|rs|java|c|cc|cpp|h|hpp)$", re.I)


# ── merge-proposal vetoes (2026-08-12, measured on the 2026-08-11 triage) ──
#
# Name-shape classes that produced most of the merge queue's false positives
# (101/153 proposals rejected). Both rules are gated by the replay fixture
# (tests/fixtures/merge_triage_replay_20260811.json): a rule change that
# suppresses even one ground-truth ACCEPTED merge goes red. A scope-disjoint
# veto was measured and deliberately NOT shipped: naming drift correlates
# with scope drift (two accepted host merges — CT-id vs hostname variants of
# one box — had fully disjoint scopes), so for MERGE pairs disjoint scopes
# are weak evidence. The rule stays where it is safe, in candidate_pairs'
# link filtering.

# A token is an event slug when it is a full date (20YYMMDD / 20YY-MM-DD)
# or a bare/suffixed MMDD run tag (0806, smoke0726). Version atoms (v2, 0),
# PR numbers (104) and most ports (8082) fall outside the window — but any
# 4-digit token whose halves parse as MM/DD does match (1024, 1230), so a
# dimension- or port-shaped token can flag a name as dated. Safe in the
# one-sided case (the strip-and-compare equality still applies); the replay
# gate bounds the rest.
_EVENT_TOKEN_RE = re.compile(
    r"^[a-z]*(20\d{2}[01]\d[0-3]\d|(0[1-9]|1[0-2])([0-2]\d|3[01]))$")
# Separator-form dates (2026-08-05) must be recognized BEFORE tokenizing —
# the tokenizer would split them into innocuous fragments (2026 / 08 / 05).
_ISO_DATE_RE = re.compile(r"20\d{2}[-_.][01]\d[-_.][0-3]\d")
_ANTONYM_DIFFS = ({"pre"}, {"post"})


def _veto_tokens(name):
    """``(tokens, sluggy)`` — lowercased alphanumeric tokens with short
    tokens KEPT (sibling ids, version atoms and antonyms are load-bearing
    here, unlike in the Jaccard matcher's _token_set), plus whether the name
    carries a date/run-tag. ISO dates are stripped whole so their fragments
    never pollute the token set."""
    s = str(name).lower()
    sluggy = bool(_ISO_DATE_RE.search(s))
    s = _ISO_DATE_RE.sub(" ", s)
    toks = {t for t in re.split(r"[^a-z0-9]+", s) if t}
    return toks, sluggy or any(_EVENT_TOKEN_RE.match(t) for t in toks)


def merge_veto(name_a, name_b):
    """Reason string when a merge proposal for ``(name_a, name_b)`` should
    not be filed, else ``None``. Purely name-shaped — callers with stronger
    evidence (exact duplicates, human proposals) should not consult it.

    * ``"event-slug"``: exactly one side carries a date/run-tag token, and
      stripping those tokens does NOT make the token sets equal — a broader
      name (project, programme) paired with one dated event. When stripping
      makes them equal, the pair is one event under naming drift and stays
      proposable. Two dated names are left to evidence when the dates are
      separator-form (stripped whole before tokenizing); COMPACT dates
      surviving as tokens can still veto as numeric-substitution
      (notes-20260805 / notes-20260807) — sibling events, correctly so.
    * ``"numeric-substitution"``: both sides carry numeric-bearing tokens
      the other lacks, with identical alpha stems (CT200/CT400,
      0-11-0/0-13-0) — or the diff is exactly the pre/post antonym pair.
      Sibling artifacts, not duplicates. One-sided numeric EXTENSIONS
      (v2.0.0 vs v2, "CT100 host" vs "host") do not veto, and extra
      alpha-only tokens beside a numeric substitution do not rescue the
      pair ("CT300 Local-Models" vs "CT200" stays vetoed).
    """
    (ta, ea), (tb, eb) = _veto_tokens(name_a), _veto_tokens(name_b)
    if not ta or not tb or ta == tb:
        return None
    if ea != eb:
        dated, plain = (ta, tb) if ea else (tb, ta)
        if {t for t in dated if not _EVENT_TOKEN_RE.match(t)} != plain:
            return "event-slug"
        return None
    a_only, b_only = ta - tb, tb - ta
    if not a_only or not b_only:
        return None
    if (a_only, b_only) in (_ANTONYM_DIFFS, _ANTONYM_DIFFS[::-1]):
        return "numeric-substitution"
    stem = lambda toks: sorted(re.sub(r"\d+", "", t) for t in toks)  # noqa: E731
    na = {t for t in a_only if any(c.isdigit() for c in t)}
    nb = {t for t in b_only if any(c.isdigit() for c in t)}
    if na and nb and stem(na) == stem(nb):
        return "numeric-substitution"
    return None


def _stem_key(name):
    """Fold a bare name for stem comparison: case, separators and the
    directory prefix all ignored."""
    return re.sub(r"[^a-z0-9]+", "", str(name).rsplit("/", 1)[-1].lower())


def file_concept_split(a, b):
    """``(file, concept)`` when one name is a SOURCE FILE and the other is its
    own bare stem — ``("band.py", "band")``, ``("evals/dg_shim.py", "dg_shim")``.

    Such a pair is neither a duplicate nor unrelated. The concept usually has
    identity the file does not: an independent runtime ("dream" runs-on the
    host shim, "band" stores-data-in postgres — both FALSE of the module), or
    several implementing files (`backup.sh` AND `ops/backup.ps1` realize
    "Backup", so no single merge is even well-defined). Merging asserts false
    things about the file; dismissing throws away a real relationship. Callers
    offer ``relate`` instead and let the reviewer pick the edge — the
    suggestion is only a default (2026-07-26 curation review).

    Returns ``None`` unless exactly one side is code and the stems match, so
    two source files (``test_shim.py`` / ``tests/test_shim.py``), non-code
    pairs (``README.md`` / ``README``) and git branches (``terra_shim.py`` /
    ``feat/terra-shim``) keep the ordinary merge action.
    """
    from pseudolife_memory.memory.relation_quality import infer_type

    for f, c in ((a, b), (b, a)):
        if not _CODE_FILE_RE.search(str(f)) or _CODE_FILE_RE.search(str(c)):
            continue
        # A git branch is a VCS artifact, not a role a file realizes — and
        # _stem_key drops the "feat/" prefix, so terra_shim.py would otherwise
        # pair with feat/terra-shim (live verification, 2026-07-26).
        if "/" in str(c) and infer_type(str(c)) == "concept":
            continue
        if _stem_key(_CODE_FILE_RE.sub("", str(f))) == _stem_key(c):
            return f, c
    return None


def duplicate_candidates(entities, *, min_jaccard=0.6, dismissed=frozenset(),
                         lesson_ids=frozenset()):
    """``dismissed`` holds human-settled false positives as ordered
    (canonical_a, canonical_b) tuples — those pairs never re-flag.

    ``lesson_ids`` (see :func:`lesson_only_ids`) are skipped entirely: a
    lesson node is named ``<artifact> <aspect>`` so it shares almost every
    token with the artifact it merely mentions, and dismissing the pair does
    not stop the next lesson from minting another one (2026-07-26).

    A file/concept pair (see :func:`file_concept_split`) carries ``action:
    "relate"`` and a ``suggested_relation`` instead of ``"merge"``, with the
    file listed first so the edge reads ``<file> implements <concept>``."""
    toks = [(e["id"], e["display"], _token_set(e["display"]), e.get("canonical"))
            for e in entities if e["id"] not in lesson_ids]
    out = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a, b = toks[i][2], toks[j][2]
            if not a or not b:
                continue
            if tuple(sorted((toks[i][3] or "", toks[j][3] or ""))) in dismissed:
                continue
            jac = len(a & b) / len(a | b)
            if jac < min_jaccard:
                continue
            pair = file_concept_split(toks[i][1], toks[j][1])
            # Same name-shape vetoes the proposal-filing paths apply (the
            # replay gate that admitted them covers this listing too — same
            # predicate): a numeric-substitution / event-slug pair clutters
            # the Console with never-merge siblings ("pgvector 0.8.5" ↔
            # "0.8.6", two dated snapshot files) that filing already
            # refuses. Relate-action pairs (file/concept) are not merges
            # and keep listing.
            if not pair and merge_veto(toks[i][1], toks[j][1]) is not None:
                continue
            names = list(pair) if pair else [toks[i][1], toks[j][1]]
            found = {"type": "duplicate", "severity": "warn",
                     "label": f"{names[0]} ↔ {names[1]}", "entities": names,
                     "score": round(jac, 3),
                     "action": "relate" if pair else "merge"}
            if pair:
                found["suggested_relation"] = "implements"
            out.append(found)
    out.sort(key=lambda f: -f["score"])
    return out


def orphans(edges, entities, *, max_degree=1, lesson_ids=frozenset()):
    """``lesson_ids`` (see :func:`lesson_only_ids`) are weakly connected BY
    DESIGN — one prefers/avoids edge each — so counting them inflates this
    informational finding without ever being actionable."""
    deg = degree_counts(edges)
    names = sorted(e["display"] for e in entities
                   if deg.get(e["id"], 0) <= max_degree
                   and e["id"] not in lesson_ids)
    if not names:
        return []
    return [{"type": "orphan", "severity": "info",
             "label": f"{len(names)} weakly-connected entities",
             "entities": names, "action": "review"}]


def dubious_edges(edges, entities, *, conf=_DUBIOUS_CONF):
    disp = _disp(entities)
    rows = [{"src": disp.get(e["src_id"], str(e["src_id"])),
             "relation": e.get("relation", ""),
             "dst": disp.get(e["dst_id"], str(e["dst_id"])),
             "confidence": e.get("confidence"),
             "tag": "AMBIGUOUS"}
            for e in edges
            if e.get("origin") == "agent" and (e.get("confidence") or 1.0) <= conf]
    if not rows:
        return []
    return [{"type": "dubious_edge", "severity": "warn",
             "label": f"{len(rows)} low-confidence / type-suspect edges",
             "edges": rows, "action": "prune"}]


def test_artifacts(entities):
    names = sorted(e["display"] for e in entities if _TEST_PATTERNS.search(e["display"]))
    if not names:
        return []
    return [{"type": "test_artifact", "severity": "warn",
             "label": f"{len(names)} test/smoke artifacts",
             "entities": names, "action": "delete"}]


_LESSON_RELATIONS = frozenset({"prefers", "avoids"})


def lesson_only_ids(edges, lesson_entity_ids=frozenset()):
    """Entity ids that are lesson-minted task/approach nodes: every edge they
    carry is a lesson relation (prefers/avoids), i.e. they exist only because
    ``memory_outcome`` recorded a task. They are not graph entities — the
    mention-scan can never attribute them, they are weakly connected by
    construction, and their ``<artifact> <aspect>`` names shadow the real
    artifact in duplicate findings.

    ``lesson_entity_ids`` covers the residual tail: entities still referenced
    by lessons.entity_id/object_entity_id whose lesson edges were pruned carry
    ZERO edges, so the edge signal alone cannot identify them."""
    rels: dict = {}
    for ed in edges:
        for eid in (ed["src_id"], ed["dst_id"]):
            rels.setdefault(eid, set()).add(ed.get("relation", ""))
    out = {eid for eid, rs in rels.items() if rs <= _LESSON_RELATIONS}
    out.update(lesson_entity_ids)
    return out


def unattributed(entities, entity_sources_map, edges=(),
                 lesson_entity_ids=frozenset()):
    """Entities with no project, excluding lesson-minted nodes (see
    :func:`lesson_only_ids`) which can never be attributed."""
    lesson_only = lesson_only_ids(edges, lesson_entity_ids)
    names = sorted(e["display"] for e in entities
                   if e["id"] not in entity_sources_map
                   and e["id"] not in lesson_only)
    if not names:
        return []
    return [{"type": "unattributed", "severity": "info",
             "label": f"{len(names)} entities with no project",
             "entities": names, "action": "assign"}]


def proposed_links(proposals):
    if not proposals:
        return []
    links = []
    for p in proposals:
        row = {"id": p.get("id"), "src": p["src"], "relation": p["relation"],
               "dst": p["dst"], "confidence": p.get("confidence"),
               "similarity": p.get("similarity"), "rationale": p.get("rationale"),
               "source": p.get("source"),
               "tag": classify_edge(p, proposed=True)}
        # The link judge's opinion (schema v35), shown beside the row like
        # the merge judge's; a verdict-less row shows nothing.
        if p.get("judge_verdict"):
            row["judge"] = {"verdict": p["judge_verdict"],
                            "confidence": p.get("judge_confidence"),
                            "note": p.get("judge_note"),
                            "model": p.get("judge_model"),
                            "relation": p.get("judge_relation")}
        links.append(row)
    return [{"type": "proposed_link", "severity": "info", "action": "review",
             "label": f"{len(links)} proposed cross-session links",
             "links": links}]


def shared_pair_groups(pairs):
    """Per-pair group id for merge pairs ``[(a_id, b_id), ...]``: the entity
    appearing in MORE THAN ONE pair (highest count wins; ties break to the
    lower id), else ``None``. The write-dedup detector files up to three
    matches per minted entity, so rows sharing an endpoint are really one
    where-does-this-entity-belong decision — 22 of the 153 proposals in the
    2026-08-11 triage shared a side, each group presented as independent
    rows (and accepting two of them would race, since the first accept
    deletes the shared entity)."""
    from collections import Counter
    counts = Counter()
    for a, b in pairs:
        counts[a] += 1
        counts[b] += 1
    out = []
    for a, b in pairs:
        shared = [i for i in (a, b) if counts[i] > 1]
        out.append(min(shared, key=lambda i: (-counts[i], i))
                   if shared else None)
    return out


def merge_candidates(entity_proposals):
    rows = [p for p in (entity_proposals or []) if p.get("kind") == "merge"]
    if not rows:
        return []
    # Group on ids when the rows carry them (PG pending_entity_proposals);
    # fall back to display names for id-less rows (older callers, tests).
    def key(p, side, name):
        return p[side] if p.get(side) is not None else p[name]

    disp = {key(p, "entity_id", "entity"): p["entity"] for p in rows}
    disp.update({key(p, "into_id", "into"): p["into"] for p in rows})
    groups = shared_pair_groups(
        [(key(p, "entity_id", "entity"), key(p, "into_id", "into"))
         for p in rows])
    merges = []
    for p, g in zip(rows, groups):
        m = {"from": p["entity"], "into": p["into"], "similarity": p.get("score"),
             "reason": p.get("reason"), "id": p["id"],
             "group": disp.get(g) if g is not None else None}
        # Shadow pre-judgment (schema v30): the reviewer sees the model's
        # opinion beside the pair; a verdict-less row shows nothing.
        if p.get("judge_verdict"):
            m["judge"] = {"verdict": p["judge_verdict"],
                          "confidence": p.get("judge_confidence"),
                          "note": p.get("judge_note"),
                          "model": p.get("judge_model")}
        # The second opinion (schema v35) beside the first.
        if p.get("judge2_verdict"):
            m["judge2"] = {"verdict": p["judge2_verdict"],
                           "confidence": p.get("judge2_confidence"),
                           "model": p.get("judge2_model")}
        merges.append(m)
    return [{"type": "merge_candidate", "severity": "warn", "action": "merge",
             "label": f"{len(merges)} near-duplicate entity merges", "merges": merges}]


def junk_candidates(entity_proposals):
    rows = [p for p in (entity_proposals or []) if p.get("kind") == "junk"]
    if not rows:
        return []
    items = [{"entity": p["entity"], "reason": p.get("reason"), "id": p["id"]} for p in rows]
    return [{"type": "junk_candidate", "severity": "warn", "action": "delete",
             "label": f"{len(items)} junk entities to prune", "entities": items}]


def review(edges, entities, entity_sources_map, proposals=None, entity_proposals=None,
           dismissed_pairs=None, lesson_entity_ids=None):
    # One lesson-node computation shared by every finding that must ignore
    # them — duplicates, orphans and unattributed alike (2026-07-26).
    lesson_ids = lesson_only_ids(edges, lesson_entity_ids or frozenset())
    findings = (duplicate_candidates(entities, dismissed=dismissed_pairs or frozenset(),
                                     lesson_ids=lesson_ids)
                + test_artifacts(entities)
                + dubious_edges(edges, entities)
                + orphans(edges, entities, lesson_ids=lesson_ids)
                + unattributed(entities, entity_sources_map, edges,
                               lesson_entity_ids or frozenset())
                + proposed_links(proposals or [])
                + merge_candidates(entity_proposals or [])
                + junk_candidates(entity_proposals or []))
    return {"findings": findings, "counts": {"total": len(findings)}}
