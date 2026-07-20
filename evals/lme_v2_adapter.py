"""LongMemEval-V2 trajectory -> turns adapter (pilot prep, text-only).

LME-V2 (arXiv 2605.12493, Wu et al.) replaces the V1 user-chat QA with
web/enterprise *agent-experience* memory: 451 questions over 1,870 task
trajectories, five abilities (static state recall, dynamic state tracking,
workflow knowledge, environment gotchas, premise awareness). See the scoping
note ``docs/superpowers/specs/2026-07-18-lme-v2-scoping-note.md``.

This module is the ingest-side adapter only. Pseudolife's pipeline consumes
*chat turns* (``svc.store(text)``); LME-V2 gives *trajectories* — ordered
web-agent state/action logs. The two pure functions here bridge that gap:

  * ``load_questions(category_filter=None)`` — read ``questions.jsonl``,
    optionally filtered to one or more ability categories (raw
    ``question_type`` labels or the friendly aliases in ``CATEGORY_ALIASES``).
  * ``trajectory_to_turns(traj)`` — flatten one trajectory into a list of
    text turns suitable for ``svc.store``. Text-only: the per-state
    ``screenshot`` path is skipped (multimodal), and by default the bulky
    ``accessibility_tree`` observation is skipped too (it is what makes the
    haystacks reach 115M tokens — see OPEN QUESTION 1 in the smoke block
    below and in ``data/lme_v2/FORMAT-NOTES.md``).

Deliberately does NOT ingest, build a service, or import anything heavy
(torch/transformers/ladder_sweep) at import time — the ``--dry-run`` CLI must
stay a fast, GPU-free format sanity check. The GPU ingest harness is left
unwritten on purpose; the READY-TO-RUN smoke block at the bottom of this file
records exactly what it will do once GPU time is authorized, plus the open
questions the format raised.

Data lives under ``evals/data/lme_v2/`` (gitignored, downloaded separately):
``questions.jsonl``, ``haystacks/lme_v2_small.json`` (question_id -> 100
trajectory_ids), and ``trajectories_small.jsonl`` (the 200 unique trajectories
the small tier references, filtered out of the 1.19 GB ``trajectories.jsonl``).

Usage (repo root):

  PYTHONPATH=. python evals/lme_v2_adapter.py --dry-run
  PYTHONPATH=. python evals/lme_v2_adapter.py --dry-run --category workflow-knowledge
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
# Offline guards, matching the other benches. Cheap to set; no heavy import
# follows, so --dry-run stays fast and GPU-free.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

DATA_DIR = Path(__file__).resolve().parent / "data" / "lme_v2"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
QUESTIONS_FILE = DATA_DIR / "questions.jsonl"
SMALL_HAYSTACK_FILE = DATA_DIR / "haystacks" / "lme_v2_small.json"
# The small tier's 200 trajectories, filtered out of the full trajectories.jsonl.
TRAJECTORIES_SMALL_FILE = DATA_DIR / "trajectories_small.jsonl"

# question_type -> the five README ability names. The three ``*-abs`` variants
# (static/dynamic/procedure) are the abstention/premise-awareness questions:
# the answer is "this premise is wrong here" rather than a value.
ABILITIES = {
    "static-environment": "static state recall",
    "dynamic-environment": "dynamic state tracking",
    "procedure": "workflow knowledge",
    "errors-gotchas": "environment gotchas",
    "static-environment-abs": "premise awareness (abstention)",
    "dynamic-environment-abs": "premise awareness (abstention)",
    "procedure-abs": "premise awareness (abstention)",
}
# Friendly names -> raw question_type label(s). ``premise-awareness`` /
# ``abstention`` expands to all three ``*-abs`` types.
CATEGORY_ALIASES = {
    "static-state-recall": ["static-environment"],
    "static": ["static-environment"],
    "dynamic-state-tracking": ["dynamic-environment"],
    "dynamic": ["dynamic-environment"],
    "workflow-knowledge": ["procedure"],
    "workflow": ["procedure"],
    "environment-gotchas": ["errors-gotchas"],
    "gotchas": ["errors-gotchas"],
    "premise-awareness": ["static-environment-abs", "dynamic-environment-abs",
                          "procedure-abs"],
    "abstention": ["static-environment-abs", "dynamic-environment-abs",
                   "procedure-abs"],
}


def _resolve_categories(category_filter) -> set[str] | None:
    """Normalise a str / iterable of category names to raw question_type labels."""
    if category_filter is None:
        return None
    names = [category_filter] if isinstance(category_filter, str) else list(category_filter)
    resolved: set[str] = set()
    for name in names:
        resolved.update(CATEGORY_ALIASES.get(name, [name]))
    return resolved


def load_questions(category_filter=None) -> list[dict]:
    """Return question dicts from ``questions.jsonl``.

    ``category_filter``: ``None`` for all; else a raw ``question_type`` label
    (or friendly alias), or an iterable of them. Matching is exact against the
    resolved raw labels — asking for ``procedure`` does NOT also return
    ``procedure-abs`` (request ``premise-awareness`` for those).
    """
    wanted = _resolve_categories(category_filter)
    questions = []
    with QUESTIONS_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            q = json.loads(line)
            if wanted is None or q.get("question_type") in wanted:
                questions.append(q)
    return questions


# --------------------------------------------------------------------------- #
# accessibility-tree resolution (Fix A) — turn opaque action bids into the
# human-readable module/page labels the raw trees hide.
#
# The web-agent acts by bid (``click('1269')``); the human label ("Reports")
# lives only in the ``accessibility_tree`` node that bid names. Ingesting whole
# trees reintroduces the multi-MB haystack scale (and was what starved the
# corpus of gold labels — the gold module names appear 0× in a bid-only ingest).
# So instead of dumping trees we distil two compact, high-signal lines per
# state: the resolved ACTION label and a capped PAGE context (title + headers).
# --------------------------------------------------------------------------- #
# A tree line: optional ``[bid]`` prefix, then ``role 'name'``, then flags.
_TREE_LINE_RE = re.compile(r"^\s*(?:\[(?P<bid>\w+)\]\s*)?"
                           r"(?P<role>[A-Za-z]+)\s+'(?P<name>[^']*)'(?P<rest>.*)$")
# An action string like ``click('1269')`` / ``fill('113', 'x', True)`` /
# ``select_option('a60', 'assigned_to')`` — the first quoted arg is the bid.
_ACTION_BID_RE = re.compile(r"^[A-Za-z_]+\(\s*'(?P<bid>[^']+)'")
# Action function name -> a past-tense verb for the resolved line.
_ACTION_VERB = {
    "click": "clicked", "dblclick": "double-clicked", "fill": "filled",
    "press": "pressed", "select_option": "selected", "hover": "hovered",
    "clear": "cleared", "check": "checked", "uncheck": "unchecked",
    "focus": "focused", "type": "typed",
}
# Roles worth surfacing as page landmarks/headers (the "breadcrumbs/headers"
# fallback): where module names like "Problems"/"Reports" actually live.
_LANDMARK_ROLES = ("heading", "main")


def _parse_bid_map(tree: str) -> dict[str, str]:
    """Map ``bid -> "role 'name'...rest"`` for every bidded line in one tree."""
    out: dict[str, str] = {}
    for line in (tree or "").splitlines():
        m = _TREE_LINE_RE.match(line)
        if m and m.group("bid"):
            out[m.group("bid")] = (
                f"{m.group('role')} '{m.group('name')}'{m.group('rest')}").strip()
    return out


def _resolve_bid(bid: str, tree: str) -> tuple[str, str] | None:
    """Resolve one ``bid`` against a tree to ``(role, name)`` or ``None``."""
    if not bid:
        return None
    for line in (tree or "").splitlines():
        m = _TREE_LINE_RE.match(line)
        if m and m.group("bid") == bid:
            return m.group("role"), m.group("name")
    return None


def _resolve_action(action: str, prev_tree: str,
                    same_tree: str) -> str | None:
    """A compact resolved-action line, or ``None`` if the bid won't resolve.

    ``state[i].action`` is the action decided while observing ``state[i-1]`` (its
    element lives in the PRE-action tree — a navigation ``click`` targets a link
    that is gone once the page changes), so ``prev_tree`` is authoritative;
    ``same_tree`` is a fallback for the initial state / rare reorderings.
    """
    m = _ACTION_BID_RE.match((action or "").strip())
    if not m:
        return None
    bid = m.group("bid")
    hit = _resolve_bid(bid, prev_tree) or _resolve_bid(bid, same_tree)
    if not hit:
        return None
    role, name = hit
    if not name:
        return None
    fn = (action or "").split("(", 1)[0].strip()
    verb = _ACTION_VERB.get(fn, fn or "acted on")
    return f'{verb}: {role} "{name}"'


def _page_context(tree: str, max_labels: int, char_cap: int) -> str:
    """A compact ``page: <title>`` line plus a few landmark/header labels.

    Distils the module/page the state is ON (the ``RootWebArea`` title) and its
    headers — the high-signal, human-readable content the raw tree buries. Capped
    by ``max_labels`` distinct labels and a hard ``char_cap`` so a pathological
    tree can never reintroduce the multi-MB scale.
    """
    parts: list[str] = []
    seen: set[tuple[str, str]] = set()
    for line in (tree or "").splitlines():
        m = _TREE_LINE_RE.match(line)
        if not m:
            continue
        role, name, rest = m.group("role"), m.group("name"), m.group("rest")
        if role == "RootWebArea" and not any(p.startswith("page:") for p in parts):
            if name:
                parts.insert(0, f"page: {name}")
            continue
        take = (role in _LANDMARK_ROLES
                or (role == "button" and "expanded=True" in rest)
                or (role == "tab" and "selected=True" in rest))
        if take and name and (role, name) not in seen:
            seen.add((role, name))
            if len(seen) <= max_labels:
                parts.append(f'{role}: "{name}"')
    block = "\n".join(parts)
    if char_cap and len(block) > char_cap:
        block = block[:char_cap] + " ...[capped]"
    return block


# --------------------------------------------------------------------------- #
# Knowledge-article body capture (Fix D) — get the PROTOCOL prescription into
# the corpus.
#
# Fix A distils each state to title + landmark labels + resolved action, and
# deliberately drops body StaticText (that cap kept the corpus ~1.4x baseline
# instead of ~47x). But some `procedure` gold answers are grounded in the BODY
# of a ServiceNow KB article ("Company Protocols - Agent Workload Balancing"):
# the article's numbered steps name the modules ("...access the list of
# reports... Re-assign the ... problem...") the answer needs. With body text
# never captured, no extractor can emit that procedure. Fix D detects article
# pages and emits their body ONCE per trajectory as a framed `[article]` turn.
#
# Detector (verified against all 200 small-tier trajectories, 2026-07-20): a
# `RootWebArea` whose title contains "Knowledge Portal" AND a tree that carries
# an `article` role node. Across the haystack that pair matches exactly the five
# "Company Protocols - <topic>" article pages and excludes every "... Knowledge
# Search - Knowledge Portal" / "Knowledge Home - Knowledge Portal" chrome page
# (those render no `article` node). The `article` node also bounds the body: the
# KB number, "Authored by", view count, timestamp and "Copy Permalink" are
# siblings OUTSIDE the article subtree, so anchoring on it drops them for free.
# --------------------------------------------------------------------------- #
_KNOWLEDGE_PORTAL = "Knowledge Portal"
_PORTAL_TITLE_SUFFIX = " - Knowledge Portal"
# A tree line whose accessible name is a Python-repr string literal — single OR
# double quoted, with escapes (`\'`). Article BODY text needs this (apostrophes
# and quotes are common); the simpler ``_TREE_LINE_RE`` above only handles plain
# single-quoted names and would truncate at the first inner apostrophe.
_ARTICLE_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:\[(?P<bid>\w+)\]\s*)?"
    r"(?P<role>[A-Za-z]+)\s+"
    r"(?P<name>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
    r"(?P<rest>.*)$")
# Body roles that carry human text; ListMarker / Section / paragraph (empty
# name) / button etc. are dropped.
_ARTICLE_TEXT_ROLES = ("StaticText", "heading", "link")
# Metadata lines to skip even if they slip inside the article subtree.
_ARTICLE_BOILERPLATE_RE = re.compile(
    r"(?i)^(authored by|article metadata|this article (was|has)|"
    r"copy permalink|attach to private task|kb\d+$)")


def _literal_name(raw: str) -> str:
    """Unescape a repr-quoted accessible name (``ast.literal_eval`` with a plain
    strip fallback)."""
    try:
        return ast.literal_eval(raw)
    except Exception:  # noqa: BLE001 — malformed literal: best-effort strip
        return raw.strip("'\"")


def _root_title(tree: str) -> str:
    """The ``RootWebArea`` title of a tree (first line), or ``''``."""
    first = (tree or "").splitlines()[:1]
    if not first:
        return ""
    m = _ARTICLE_LINE_RE.match(first[0])
    if m and m.group("role") == "RootWebArea":
        return _literal_name(m.group("name"))
    return ""


def _article_page(tree: str, char_cap: int) -> tuple[str, str] | None:
    """If ``tree`` is a KB article page, return ``(title, body)`` else ``None``.

    ``title`` is the RootWebArea title minus the ``" - Knowledge Portal"``
    suffix (the stable dedup key and turn label). ``body`` is the article
    subtree's StaticText/heading/link names joined in document order (links stay
    interleaved so sentences read coherently), boilerplate stripped, capped at
    ``char_cap``.
    """
    title = _root_title(tree)
    if _KNOWLEDGE_PORTAL not in title:
        return None
    lines = (tree or "").splitlines()
    # Locate the `article` role node and remember its indent (the body is its
    # subtree — every deeper-indented line until indent returns to its level).
    art_i = art_indent = None
    for i, line in enumerate(lines):
        m = _ARTICLE_LINE_RE.match(line)
        if m and m.group("role") == "article":
            art_i, art_indent = i, len(m.group("indent"))
            break
    if art_i is None:               # a search/home portal page — not an article
        return None

    parts: list[str] = []
    for line in lines[art_i + 1:]:
        m = _ARTICLE_LINE_RE.match(line)
        if not m:
            # a nameless/blank line that is back at the article's level ends it
            if line.strip() and len(line) - len(line.lstrip()) <= art_indent:
                break
            continue
        if len(m.group("indent")) <= art_indent:
            break                   # left the article subtree
        if m.group("role") not in _ARTICLE_TEXT_ROLES:
            continue
        name = _literal_name(m.group("name")).strip()
        # skip icon-glyph names (private-use area) and metadata boilerplate
        if (not name or ord(name[0]) >= 0xE000
                or _ARTICLE_BOILERPLATE_RE.match(name)):
            continue
        parts.append(name)

    body = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if not body:
        return None
    label = title[:-len(_PORTAL_TITLE_SUFFIX)] if title.endswith(
        _PORTAL_TITLE_SUFFIX) else title
    if char_cap and len(body) > char_cap:
        body = body[:char_cap].rstrip() + " ...[capped]"
    return label, body


def trajectory_to_turns(traj: dict, *, include_observations: bool = False,
                        observation_chars: int = 2000,
                        max_page_labels: int = 8,
                        include_article_body: bool = True,
                        article_chars: int = 1500) -> list[str]:
    """Flatten one LME-V2 trajectory into a list of text turns for ``svc.store``.

    Turn 0 frames the task (goal + environment + outcome). Each subsequent turn
    is one state's agent-visible experience: the URL it was on, its ``thought``,
    and the ``action`` it took. States are already ordered by ``state_index``.

    Text-only. Two fields are intentionally dropped or gated:
      * ``screenshot`` — a PNG path (multimodal); always skipped.
      * ``accessibility_tree`` — the text page observation. Skipped by default
        (default off keeps the plain thought+action baseline). With
        ``include_observations=True`` the tree is NOT dumped; it is distilled
        (Fix A) into two compact lines per state, each bounded by
        ``observation_chars``:
          - a resolved ACTION line (``clicked: link "Reports"``) — the action's
            bid mapped to its node's role+name (against the pre-action tree);
          - a capped PAGE context (``page: <title>`` + up to ``max_page_labels``
            landmark/header labels) — where the gold module/page names live.
        This recovers the human-readable labels the bid-only baseline drops
        (gold module names go from 0 occurrences to present) WITHOUT the
        multi-MB scale a raw-tree dump reintroduces.

    Fix D — knowledge-article body. When ``include_observations`` and
    ``include_article_body`` (default on), a state that is a KB article page
    (see ``_article_page``) also contributes a separate, framed turn
    ``[article] <title>: <body>`` right after its step turn — the article's
    prescription (protocol steps, module names) that the page-context cap above
    deliberately omits. Emitted ONCE per distinct article title per trajectory
    (articles get revisited), body capped at ``article_chars``.
    """
    env = traj.get("environment", "?")
    domain = traj.get("domain", "?")
    goal = (traj.get("goal") or "").strip()
    outcome = traj.get("outcome", "?")
    turns: list[str] = [
        f"[task | {domain}/{env} | outcome={outcome}] {goal}"
    ]
    states = traj.get("states", [])
    prev_tree = ""
    emitted_articles: set[str] = set()
    for st in states:
        tree = st.get("accessibility_tree") or ""
        parts = [f"[step {st.get('state_index')}] url: {st.get('url', '')}"]
        thought = (st.get("thought") or "").strip()
        if thought:
            parts.append(f"thought: {thought}")
        action = st.get("action")          # None on the initial state
        if action:
            parts.append(f"action: {action}")
        if include_observations:
            obs: list[str] = []
            if action:
                resolved = _resolve_action(action, prev_tree, tree)
                if resolved:
                    obs.append(resolved)
            ctx = _page_context(tree, max_page_labels, observation_chars)
            if ctx:
                obs.append(ctx)
            block = "\n".join(obs)
            if observation_chars and len(block) > observation_chars:
                block = block[:observation_chars] + " ...[capped]"
            if block:
                parts.append(block)
        turns.append("\n".join(parts))
        if include_observations and include_article_body:
            art = _article_page(tree, article_chars)
            if art and art[0] not in emitted_articles:
                emitted_articles.add(art[0])
                turns.append(f"[article] {art[0]}: {art[1]}")
        prev_tree = tree
    return turns


def load_small_haystack() -> dict[str, list[str]]:
    """question_id -> ordered list of 100 trajectory_ids (small tier)."""
    return json.loads(SMALL_HAYSTACK_FILE.read_text(encoding="utf-8"))


def load_trajectory(traj_id: str) -> dict | None:
    """Scan ``trajectories_small.jsonl`` for one trajectory by id (or None)."""
    if not TRAJECTORIES_SMALL_FILE.exists():
        return None
    with TRAJECTORIES_SMALL_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("id") == traj_id:
                return obj
    return None


def _dry_run(category: str | None, include_observations: bool) -> int:
    """Print one adapted trajectory + the question it belongs to, then exit."""
    questions = load_questions(category)
    if not questions:
        print(f"no questions for category={category!r}", file=sys.stderr)
        return 1
    q = questions[0]
    print("=" * 72)
    print(f"QUESTION  id={q['id']}  type={q['question_type']} "
          f"({ABILITIES.get(q['question_type'], '?')})")
    print(f"  domain={q['domain']}  environment={q['environment']}  "
          f"image={q.get('image')}")
    print(f"  Q: {q['question']}")
    print(f"  A: {q['answer']}")
    print(f"  eval_function: {q['eval_function']}")
    print("=" * 72)

    haystack = load_small_haystack()
    traj_ids = haystack.get(q["id"], [])
    print(f"small-tier haystack for this question: {len(traj_ids)} trajectories")

    # Prefer a trajectory actually in this question's haystack; fall back to the
    # first trajectory on disk so the dry-run still demonstrates the adapter even
    # if trajectories_small.jsonl is a partial download.
    traj = None
    for tid in traj_ids:
        traj = load_trajectory(tid)
        if traj is not None:
            break
    if traj is None and TRAJECTORIES_SMALL_FILE.exists():
        with TRAJECTORIES_SMALL_FILE.open(encoding="utf-8") as fh:
            first = next((ln for ln in fh if ln.strip()), None)
        traj = json.loads(first) if first else None
    if traj is None:
        print(f"\n(no trajectory content available at {TRAJECTORIES_SMALL_FILE};"
              " download the small tier to see an adapted trajectory)")
        return 0

    turns = trajectory_to_turns(traj, include_observations=include_observations)
    print(f"\nADAPTED TRAJECTORY  id={traj['id']}  "
          f"{traj['domain']}/{traj['environment']}  outcome={traj['outcome']}  "
          f"states={len(traj.get('states', []))} -> {len(turns)} turns "
          f"(include_observations={include_observations})")
    print("-" * 72)
    for i, turn in enumerate(turns):
        preview = turn if len(turn) <= 600 else turn[:600] + " ...[turn truncated]"
        print(f"[turn {i}]\n{preview}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print one adapted trajectory + its question, then exit")
    ap.add_argument("--category", default=None,
                    help="ability category or alias (e.g. workflow-knowledge, "
                         "procedure, environment-gotchas)")
    ap.add_argument("--observations", action="store_true",
                    help="fold the accessibility_tree observation into each turn "
                         "(truncated) — off by default, see OPEN QUESTION 1")
    args = ap.parse_args()
    if args.dry_run:
        return _dry_run(args.category, args.observations)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ============================================================================
# READY-TO-RUN GPU SMOKE (do NOT run until GPU time is authorized — CPU-only
# tonight; the 27B extractor and answer/judge endpoints both need the 4090).
# ============================================================================
#
# Goal: run ONE small-tier haystack end-to-end through the Pseudolife pipeline
# with the qwen-27b extractor and the three standard arms (rag / cortex /
# hybrid), mirroring longmemeval_bench.py's ingest -> dream -> answer -> judge
# structure, and decide whether a full category run earns the GPU time.
#
# Category: use `procedure` (workflow-knowledge, 74 questions, ALL text-only).
#   NOT `errors-gotchas`: all 29 gotcha questions are multimodal — the question
#   stem references a `question_screenshots/*.png` the text-only path cannot
#   see (FINDING, see FORMAT-NOTES.md). Gotchas need the multimodal answerer.
#
# Prereqs the smoke assumes are already on disk (tonight's deliverables):
#   evals/data/lme_v2/questions.jsonl
#   evals/data/lme_v2/haystacks/lme_v2_small.json
#   evals/data/lme_v2/trajectories_small.jsonl   (the 200 small-tier trajectories)
#
# Endpoints to bring up first (GPU):
#   qwen-27b extractor + answerer/judge at http://127.0.0.1:1234/v1
#   (EXTRACTORS["qwen-27b"] and QWEN_URL in longmemeval_bench.py already point here)
#
# The ingest harness is NOT written yet — the format raised open questions that
# should be answered before committing to a store policy. When it is written it
# will, per question, mirror longmemeval_bench.ingest_and_dream:
#
#   from ladder_sweep import build_service
#   from pseudolife_memory.memory.dream import OpenAICompatExtractor
#   import evals.lme_v2_adapter as A
#
#   svc = build_service(tempfile.mkdtemp(prefix="lme2_"))   # fresh bench DB
#   svc.config.memory.dream.extract_relations = False       # facts only
#   extractor = OpenAICompatExtractor("http://127.0.0.1:1234/v1", "bench",
#                                     max_tokens=4096, timeout_seconds=600.0)
#   haystack = A.load_small_haystack()
#   for q in A.load_questions("workflow-knowledge")[:1]:     # ONE question
#       for tid in haystack[q["id"]]:                        # its 100 trajectories
#           traj = A.load_trajectory(tid)
#           for turn in A.trajectory_to_turns(traj):         # text-only turns
#               svc.store(turn, source="bench")
#           # dream cadence: one trajectory ~= one "session" boundary
#           <run svc.dream_run(...) to consolidation, as ingest_and_dream does>
#       # then build_contexts + answer_and_judge over rag/cortex/hybrid arms,
#       # reusing longmemeval_bench.py's build_contexts / answer_and_judge.
#
# OPEN QUESTIONS to resolve before/with the smoke (details in FORMAT-NOTES.md):
#   1. Observations: ingest accessibility_tree per state or not? Off keeps a
#      100-trajectory haystack tractable but starves static/dynamic-state recall
#      of page content; on reintroduces the 115M-token scale. Try a small
#      observation_chars cap first. (Only `procedure` is in the smoke, and
#      workflow knowledge lives in thought+action, so default-off is the safe
#      smoke start.)
#   2. Store policy: store every turn (as above) vs agent-voluntary capture
#      simulation (ties into the capture experiment) — the scoping note flags
#      this as an undecided design axis.
#   3. Dream cadence: what is a "session" boundary over a trajectory? Per
#      trajectory (used above) vs per state vs per haystack. Affects how much
#      supersession the cortex arm can exercise.
#   4. Latency: LME-V2 also scores query latency (scoping note cost 3); the
#      smoke should record per-arm wall time, not just accuracy.
