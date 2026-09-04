"""What an AGENT pays, in context, to use this memory — measured.

Every "fewer tokens" figure this project has published measures *served
benchmark context*: the passage an answerer model reads to answer a
LongMemEval question (``ladder_sweep.py``'s ``tokens_per_query``,
``longmemeval_bench.py``'s ``context_tokens``). None of them measures the
other side of the wire — what a real MCP client reads back from a tool
call, and pays for on every single call, forever.

This ledger measures that side, in four parts:

1. **manifest** — the tool descriptions and inputSchema param descriptions
   a non-deferring client eats once per session, per toolset tier. Measured
   through the same ``mcp.list_tools()`` + ``_visible_tool_names`` path
   ``tests/test_tool_consolidation.py::test_descriptions_fit_tier_budgets``
   meters, so the two can never disagree.
2. **session_start** — ``web/session_hook.MEMORY_LOOP_BLOCK``, injected once
   per session by the hook.
3. **search / fact_get / recall** — the per-call response payloads, for real
   queries against a live bank.

Method (the honest caveats, because the numbers are bank-specific):

* The bank is whatever daemon ``--base`` points at; totals scale with its
  entry sizes. The artifact records ``bank`` (entry count) so a rerun on a
  different bank is not mistaken for a regression.
* Queries are a fixed, committed list of 15 dev-session questions rather
  than a sample of the ``retrieval_events`` table. The table would be more
  representative and is deliberately not used: this is a public repo, real
  queries carry paths and names, and the ledger has to be re-runnable by
  anyone.
* Payloads are fetched ONCE, raw, from the daemon's GET-only REST
  (``/api/search``, ``/api/recall``, ``/api/facts``). GET-only is not the
  same as side-effect-free: ``/api/search`` runs the real retrieval path,
  which appends ``retrieval_events`` rows and touches per-entry access
  counters. It does not change bank CONTENT — no entry, fact, edge or
  episode is written, moved or reinforced by this script. The payloads are
  then projected offline through the MCP layer's own pure helpers
  (``mcp_server._project_search``, ``_lean_fact_record``, the
  ``_cap_recall_*`` family). Before/after is
  therefore exactly paired — same bytes in, two projections out — and one
  read of the bank serves both arms.
* Sizes are chars of the compact JSON encoding an MCP client receives
  (``separators=(",", ":")``, ``ensure_ascii=False``) and approx tokens are
  ``chars // 4``, the convention in ``ladder_sweep.approx_tokens``.
* The ``fact_get`` arm prices the RECORD, not the call: its "before" is the
  ``/api/facts`` dump row rather than the served ``cortex_lookup`` record
  (the dump carries an ``entity_id`` the served record never has), and
  neither arm includes the tool envelope, ``contenders`` or
  ``correct_with``. See ``measure_fact_get``.
* The cuts are read from ``utils.config.McpConfig`` rather than restated
  here, so a change to ``entry_text_chars`` re-prices the run instead of
  silently desynchronising it from the published numbers. The values used
  are written into the artifact's ``config`` block.

Usage::

    python evals/agent_token_ledger.py \
        --daemon http://127.0.0.1:8765 \
        --out evals/results/agent-token-ledger-20260904-r2.json

The output path is never overwritten: a rerun that would land on an existing
artifact refuses and tells you to tag the new run (``--out ...-r2.json``) or
pass ``--force``. Tag and promote deliberately — a canonical result file
silently rewritten by a rerun is the 2026-07-21 lesson in CLAUDE.md.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import socket
import statistics
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import date
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ── size conventions ──────────────────────────────────────────────────────

def wire(obj: Any) -> str:
    """The JSON an MCP client actually receives for this object."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def approx_tokens(text: str) -> int:
    """chars // 4 — the same rough conversion ``ladder_sweep`` publishes."""
    return max(1, len(text or "") // 4)


def sized(obj: Any) -> dict[str, int]:
    s = wire(obj)
    return {"chars": len(s), "approx_tokens": approx_tokens(s)}


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "median": 0.0, "p90": 0.0, "max": 0.0}
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
    return {"mean": round(statistics.fmean(values), 1),
            "median": round(statistics.median(values), 1),
            "p90": round(float(ordered[idx]), 1),
            "max": round(float(ordered[-1]), 1)}


# ── the fixed query set ───────────────────────────────────────────────────
# Fifteen questions in the shape a coding session actually asks a memory:
# a current value, a past decision, a procedure, a failure, a constraint.
QUERIES: list[str] = [
    "what port does the bench postgres listen on",
    "how do I deploy the daemon",
    "why is the recency boost disabled",
    "what did the flat band ablation conclude",
    "which judge model do the evals use",
    "how does the dream cursor advance",
    "what breaks when two dreams run concurrently",
    "what is the current schema version and what did it add",
    "how are constraint facts pinned in retrieval",
    "what happened with the retrieval event log",
    "which extractor is deployed for consolidation",
    "how do I run the full test suite",
    "what is the release procedure for pypi",
    "why did the cascade number get retired",
    "how does supersession order writes",
]

# Relational questions — the shape ``memory_recall`` exists for.
RECALL_QUESTIONS: list[str] = [
    "what does the daemon ultimately run on",
    "how does the console reach the bank",
    "what depends on the embedding model",
    "how does a memory become a canonical fact",
    "what does the shim connect to",
]

# ── PII hygiene (public repo) ─────────────────────────────────────────────
# The artifact records slot NAMES from a live bank. Anything that looks like
# a home path, an address or a credential is replaced rather than committed.
_UNSAFE = re.compile(
    r"(?:[A-Za-z]:\\\\?Users|/home/|/Users/|@[\w.-]+\.\w+|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|\bsk-[A-Za-z0-9]|\bghp_|\bBearer\b)", re.IGNORECASE)


def host_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """A matcher for MACHINE NAMES, or None when there is nothing to hide.

    A bank keeps facts ABOUT the machine it runs on, so a slot label can
    be a bare hostname with no path, address or credential in it for
    ``_UNSAFE`` to catch: it passed ``<host> | crash-root-cause`` straight
    into a committed artifact (2026-09-04 review finding), and CLAUDE.md
    forbids hostnames in a public tree as firmly as it forbids emails.

    Separators are optional in the match, so the DNS form, the shouty
    NetBIOS form and the spaced-out prose form of one name all hit:
    ``box-two`` matches ``BOX-TWO``, ``box two`` and ``boxtwo``. Any
    domain suffix is dropped (only the label is matched), and names under
    three characters are skipped because they would redact real words.
    """
    alts = []
    for n in names:
        n = (n or "").strip().split(".")[0]
        if len(n) < 3:
            continue
        parts = [re.escape(p) for p in re.split(r"[-_.\s]+", n) if p]
        if parts:
            alts.append(r"[-_.\s]?".join(parts))
    if not alts:
        return None
    return re.compile(r"\b(?:" + "|".join(sorted(set(alts))) + r")\b",
                      re.IGNORECASE)


def local_hostnames() -> set[str]:
    """Every name this machine answers to — the OS name plus the two env
    vars Windows and POSIX respectively set."""
    return {n for n in (socket.gethostname(),
                        os.environ.get("COMPUTERNAME", ""),
                        os.environ.get("HOSTNAME", "")) if n}


_HOST = host_pattern(local_hostnames())


def safe_label(s: str) -> str:
    s = " ".join((s or "").split())
    if _UNSAFE.search(s) or (_HOST is not None and _HOST.search(s)):
        return "<redacted>"
    return s


# ── daemon REST (read-only) ───────────────────────────────────────────────

class Daemon:
    def __init__(self, base: str, token: str | None) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def get(self, path: str, **params: Any) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))


def read_token() -> str | None:
    """The daemon bearer, from the environment or ops/.env. Never printed,
    never written to the artifact."""
    tok = os.environ.get("PSEUDOLIFE_MCP_TOKEN")
    if tok:
        return tok.strip() or None
    env = os.path.join(REPO, "ops", ".env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8", errors="ignore"):
            k, _, v = line.strip().partition("=")
            if k.strip() == "PSEUDOLIFE_MCP_TOKEN":
                return v.strip().strip('"').strip("'") or None
    return None


# ── the MCP projection layer, loaded without touching a bank ──────────────

def mcp_defaults() -> Any:
    """The shipped ``memory.mcp`` defaults, read from the dataclass.

    The two cuts this ledger prices are parameterised in
    ``utils.config.McpConfig`` (``entry_text_chars``, and the
    ``min(5, top_k)`` cortex width the tool computes from it being on).
    Reading them here rather than restating the numbers means a future
    change to a default cannot silently desynchronise the published
    figures from the behaviour they describe — the run would price the
    new default and the pinned claims would go red (2026-09-04 review
    finding).
    """
    from pseudolife_memory.utils.config import AppConfig
    return AppConfig().memory.mcp


# The cortex block's width under the cut, as ``memory_search`` computes it
# (``mcp_server.memory_search``: ``min(5, max(1, top_k))``). Kept as one
# named helper so the ledger and the tool cannot drift apart in two
# places.
CORTEX_WIDTH_UNCUT = 5


def cortex_width(top_k: int) -> int:
    return min(CORTEX_WIDTH_UNCUT, max(1, top_k))


def load_mcp():
    """Import ``mcp_server`` bound to a throwaway file-mode data dir. Only
    the pure projection helpers and the tool manifest are used; the service
    singleton is never initialised, so nothing here can reach the live
    bank."""
    os.environ["PSEUDOLIFE_MCP_DATA_DIR"] = tempfile.mkdtemp(
        prefix="agent-token-ledger-")
    os.environ.pop("PSEUDOLIFE_MCP_DATABASE_URL", None)
    os.environ.setdefault("PSEUDOLIFE_MCP_TOOLSET", "full")
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


# ── 1. the tool manifest ──────────────────────────────────────────────────

def measure_manifest(mod) -> dict[str, Any]:
    tools = asyncio.run(mod.mcp.list_tools())
    desc = {t.name: len(t.description or "") for t in tools}
    params: dict[str, int] = {}
    for t in tools:
        props = (t.input_schema or {}).get("properties", {}) or {}
        params[t.name] = sum(len(p.get("description") or "")
                             for p in props.values())
    out: dict[str, Any] = {}
    for tier in ("minimal", "core", "full"):
        names = mod._visible_tool_names(tier)
        d = sum(desc[n] for n in names)
        p = sum(params[n] for n in names)
        out[tier] = {
            "tools": len(names),
            "description_chars": d,
            "param_description_chars": p,
            "chars": d + p,
            "approx_tokens": approx_tokens("x" * (d + p)),
        }
    return out


# ── 2. the served session-start block ─────────────────────────────────────

def measure_session_block() -> dict[str, Any]:
    """Both sizes, because they measure different things and only one is
    the cost. The hook writes the block to Claude's context as PLAIN TEXT,
    so ``raw_chars`` is what a session actually pays — that is the number
    the docs publish. ``chars`` is the same block JSON-encoded (escaped
    newlines and quotes), kept for comparability with every other cell in
    this ledger, which really is a JSON payload (2026-09-04 review
    finding: the README published the JSON size for a non-JSON surface).
    """
    from pseudolife_memory.web.session_hook import MEMORY_LOOP_BLOCK
    return sized(MEMORY_LOOP_BLOCK) | {
        "raw_chars": len(MEMORY_LOOP_BLOCK),
        "raw_approx_tokens": approx_tokens(MEMORY_LOOP_BLOCK),
    }


# ── 3. memory_search ──────────────────────────────────────────────────────

def _split_search(payload: dict) -> dict[str, int]:
    """Where a served ``memory_search`` payload's bytes actually go."""
    entries = payload.get("entries", []) or []
    text_chars = sum(len(wire(e.get("text", ""))) for e in entries)
    sup_chars = sum(len(wire(e.get("superseded_by_text", "")))
                    for e in entries if e.get("superseded_by_text"))
    entries_chars = len(wire(entries))
    cortex_chars = len(wire(payload.get("cortex", []) or []))
    events_chars = len(wire(payload["events"])) if payload.get("events") else 0
    total = len(wire(payload))
    return {
        "total_chars": total,
        "entries_chars": entries_chars,
        "entries_text_chars": text_chars,
        "entries_superseded_text_chars": sup_chars,
        "entries_other_chars": entries_chars - text_chars - sup_chars,
        "cortex_chars": cortex_chars,
        "events_chars": events_chars,
        "envelope_chars": total - entries_chars - cortex_chars - events_chars,
        "count": len(entries),
        "cortex_count": len(payload.get("cortex", []) or []),
        "approx_tokens": approx_tokens("x" * total),
    }


def project_search(mod, raw: dict, *, compact: bool, top_k: int,
                   text_chars: int) -> dict:
    """Reproduce the MCP payload for one raw ``/api/search`` response.

    ``/api/search`` returns exactly the two inputs the tool's projection
    consumes: the raw service result and, under ``cortex``, the unprojected
    cortex-search entries. The projection itself is the tool's own
    ``_project_search``, so the entries side is exact.

    The cortex narrowing (cut b) is the one APPROXIMATION here, and it is
    only exact on an unlabelled bank. ``/api/search`` fetches its facts at
    a hardcoded ``top_k=5`` (``web/routes.py``), so the narrow arm slices
    that list to ``min(5, top_k)`` rather than re-running
    ``cortex_search`` at the narrower width the way ``memory_search``
    does. Those differ when constraint pinning is in play:
    ``_pin_constraint_facts`` budgets pins at ``max(1, top_k // 2)``, so a
    real ``top_k=3`` call would allow one pin where a width-5 fetch
    allowed two, and the slice would keep the extra pin instead of a
    ranked fact. They cannot differ while no current fact carries a
    ``distortion_tolerance`` label, because ``_pin_constraint_facts`` then
    returns its input untouched and the slice is the same set in the same
    order. That validity condition is counted per run rather than asserted:
    ``pick_slots`` writes ``facts_labelled`` / ``facts_current`` into the
    artifact's ``bank`` block. Read the narrow arm only when
    ``facts_labelled`` is 0.
    """
    payload = json.loads(json.dumps(raw, default=str))
    facts = payload.pop("cortex", []) or []
    if compact:
        facts = facts[:cortex_width(top_k)]
    return mod._project_search(
        payload, facts, compact=True,
        text_chars=(text_chars if compact else None))


def measure_search(mod, dm: Daemon, top_k: int,
                   text_chars: int) -> dict[str, Any]:
    rows = []
    entry_text_lengths: list[float] = []
    for q in QUERIES:
        raw = dm.get("/api/search", q=q, top_k=top_k)
        before = _split_search(project_search(
            mod, raw, compact=False, top_k=top_k, text_chars=text_chars))
        after = _split_search(project_search(
            mod, raw, compact=True, top_k=top_k, text_chars=text_chars))
        # Raw per-entry text length — what the ``entry_text_chars`` cap is
        # chosen against. Taken from the untruncated arm, in characters of
        # the text itself rather than of its JSON encoding.
        entry_text_lengths += [
            float(len(e.get("text") or ""))
            for e in project_search(
                mod, raw, compact=False, top_k=top_k,
                text_chars=text_chars).get("entries", [])]
        rows.append({"query": q, "before": before, "after": after})
    agg: dict[str, Any] = {}
    for arm in ("before", "after"):
        agg[arm] = {
            k: _stats([r[arm][k] for r in rows])
            # ``entries_superseded_text_chars`` is aggregated, not just
            # recorded per query: it is a third of the entries block on
            # this bank and the README's breakdown did not name it, which
            # left ~2,400 chars unlabelled between the block total and
            # text + metadata (2026-09-04 review finding).
            for k in ("total_chars", "entries_chars", "entries_text_chars",
                      "entries_superseded_text_chars",
                      "entries_other_chars", "cortex_chars", "events_chars")
        }
        agg[arm]["total_approx_tokens"] = _stats(
            [r[arm]["approx_tokens"] for r in rows])
    cap = text_chars
    return {"top_k": top_k, "queries": len(rows), "per_query": rows,
            "aggregate": agg,
            "entry_text": {
                "entries": len(entry_text_lengths),
                "entry_text_chars": cap,
                "raw_chars": _stats(entry_text_lengths),
                "over_cap": sum(1 for n in entry_text_lengths if n > cap),
                "share_over_cap": round(
                    sum(1 for n in entry_text_lengths if n > cap)
                    / max(1, len(entry_text_lengths)), 3),
            }}


# ── 4. memory_fact_get ────────────────────────────────────────────────────

def measure_fact_get(mod, dm: Daemon, slots: list[tuple[str, str]],
                     records: dict[tuple[str, str], dict]) -> dict[str, Any]:
    """Price the lean fact projection over five real slots — with two
    disclosed gaps, both in the direction of over-stating the "before".

    1. The "before" record is the ``/api/facts`` dump row
       (``service.cortex_dump``), not the record ``memory_fact_get``
       serves (``service.cortex_lookup``). The two agree on the
       bookkeeping keys the cut moves, which is what makes the arm
       meaningful, but the dump row carries an ``entity_id`` the served
       record never has, and the served record can carry keys the dump
       does not (``correct_with``, ``stale``/currency flags recomputed at
       lookup time). So the "before" is a few tens of chars wide of a real
       call and the "after" is the projection applied to that same row.
    2. Neither arm includes the tool ENVELOPE — ``{"record": ...,
       "contenders": [...]}`` plus, on an aged fact, ``correct_with`` and
       the ``correction_note`` — so both numbers are the record alone.
       The percentage cut is the honest figure; the absolute chars are a
       floor on what a call costs, not the call.

    Fixing either would need a live service bound to the bank, which this
    script deliberately does not have (it loads ``mcp_server`` against a
    throwaway file-mode dir so it can never write). Disclosed rather than
    silently approximated (2026-09-04 review finding).
    """
    rows = []
    for ent, attr in slots:
        rec = records[(ent, attr)]
        before = len(wire(rec))
        after = len(wire(mod._lean_fact_record(rec)))
        rows.append({
            "slot": safe_label(f"{ent} | {attr}"),
            "before_chars": before, "after_chars": after,
            "before_approx_tokens": approx_tokens("x" * before),
            "after_approx_tokens": approx_tokens("x" * after),
            "keys_before": len(rec), "keys_after": len(
                mod._lean_fact_record(rec)),
        })
    return {"slots": len(rows), "per_slot": rows,
            "aggregate": {
                "before": _stats([r["before_chars"] for r in rows]),
                "after": _stats([r["after_chars"] for r in rows])}}


def pick_slots(dm: Daemon, n: int = 5, limit: int = 20_000) -> tuple[
        list[tuple[str, str]], dict[tuple[str, str], dict], dict[str, Any]]:
    """Five real slots, chosen deterministically: the widest current facts
    in ``(entity, attribute)`` order, so a rerun on the same bank picks the
    same five and the record projection is exercised at its real width.

    The same dump is the census the narrow search arm's validity rests on
    (see ``project_search``): the cortex slice only equals a real
    ``top_k=3`` call while ``_pin_constraint_facts`` is a no-op, which holds
    exactly while no current fact carries a ``distortion_tolerance`` label.
    That condition used to be a hand-checked sentence in the docstring and
    the README with nothing behind it (2026-09-04 review finding), so it is
    counted here, from the dump already being fetched, and written into the
    artifact's ``bank`` block where a Claim row can pin it.

    ``truncated`` is recorded rather than assumed: the endpoint caps at
    ``limit``, and a truncated dump would make both the slot pick and the
    label census read a prefix of the bank.
    """
    dump = dm.get("/api/facts", limit=limit)
    entries = dump.get("entries", []) or []
    rows = [r for r in entries if r.get("kind") != "member"]
    labelled = [r for r in rows if r.get("distortion_tolerance")]
    census = {
        "facts_total": len(entries),
        "facts_current": len(rows),
        "facts_labelled": len(labelled),
        "facts_dump_truncated": bool(dump.get("truncated")),
        "facts_dump_limit": limit,
    }
    rows.sort(key=lambda r: (-len(wire(r)), r.get("entity", ""),
                             r.get("attribute", "")))
    picked = rows[:n]
    slots = [(r["entity"], r["attribute"]) for r in picked]
    return (slots, {(r["entity"], r["attribute"]): r for r in picked},
            census)


# ── 5. memory_recall ──────────────────────────────────────────────────────

def project_recall(mod, raw: dict, *, verbose: bool = False) -> dict:
    """The MCP-side capping/compaction of one raw ``service.recall``
    result, lifted from ``memory_recall``'s body (which is not itself
    callable without a service)."""
    out = json.loads(json.dumps(raw, default=str))
    entity_hop = out.get("entity_hop") or {}
    seed_text_count = out.get("seed_text_count")
    top_k = 5
    if seed_text_count is None:
        seed_text_count = min(top_k, len(out.get("texts", [])))
    capped = mod._cap_recall_entities(out.get("entities", []), entity_hop)
    capped = [{**e, "facts": (e.get("facts") or [])[
        :mod._RECALL_MAX_FACTS_PER_ENTITY]} for e in capped]
    surviving = {e.get("entity") for e in capped}
    out["entities"] = capped
    out["edges"] = mod._cap_recall_edges(
        out.get("edges", []), out.get("edge_hop", []), surviving)
    out["texts"] = mod._cap_recall_texts(
        out.get("texts", []), seed_text_count, top_k)
    for k in ("entity_hop", "edge_hop", "seed_text_count"):
        out.pop(k, None)
    if not verbose:
        out["entities"] = [
            {"entity": n.get("entity"),
             "facts": [mod._compact_recall_fact(f) for f in n.get("facts", [])]}
            for n in out["entities"]]
        out["edges"] = [{"src": e.get("src"), "relation": e.get("relation"),
                         "dst": e.get("dst")} for e in out["edges"]]
        out["texts"] = [mod._compact_recall_text(t) for t in out["texts"]]
    return out


def measure_recall(mod, dm: Daemon, hops: int = 3) -> dict[str, Any]:
    """Response size plus the number of ``service.search`` calls one recall
    issues.

    The call count is derived, not instrumented: ``recall.run_recall`` does
    exactly one seed search, then one search per entity newly discovered on
    each hop (``MechanicalController.next_queries`` emits one query per
    newly-seen entity, and every emitted query is searched). So
    ``searches = 1 + |{e : entity_hop[e] >= 1}|`` — read off the raw
    response, which still carries ``entity_hop`` before the MCP layer pops
    it. Graph calls are one per expanded frontier entity and are not
    counted here.
    """
    rows = []
    for q in RECALL_QUESTIONS:
        raw = dm.get("/api/recall", q=q, hops=hops, top_k=5)
        hop = raw.get("entity_hop") or {}
        searches = 1 + sum(1 for h in hop.values() if (h or 0) >= 1)
        payload = project_recall(mod, raw)
        rows.append({
            "question": q,
            "hops": hops,
            "iterations": raw.get("iterations", 0),
            "entities": len(payload.get("entities", [])),
            "service_search_calls": searches,
            **{k: v for k, v in sized(payload).items()},
            "verbose_chars": len(wire(project_recall(mod, raw, verbose=True))),
        })
    return {"questions": len(rows), "per_question": rows,
            "aggregate": {
                "service_search_calls": _stats(
                    [r["service_search_calls"] for r in rows]),
                "chars": _stats([r["chars"] for r in rows])}}


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--daemon", default="http://127.0.0.1:8765")
    ap.add_argument("--top-k", type=int, default=8,
                    help="memory_search top_k (the tool default).")
    ap.add_argument("--narrow-top-k", type=int, default=3,
                    help="Second search pass, to price the cortex-block "
                         "narrowing (cut b), which is inert at top_k >= 5.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--force", action="store_true",
                    help="Overwrite --out if it already exists. Off by "
                         "default: a rerun tags a new file and is promoted "
                         "deliberately, so a canonical artifact is never "
                         "silently rewritten.")
    args = ap.parse_args()

    out_path = args.out or os.path.join(
        REPO, "evals", "results",
        f"agent-token-ledger-{date.today():%Y%m%d}.json")

    # Refuse before spending the run, not after: the daemon fetch is the
    # slow part and there is nothing to salvage once the file is gone.
    if os.path.exists(out_path) and not args.force:
        print(f"refusing to overwrite {out_path}\n"
              f"  tag this run instead (--out ...-r2.json) and promote it "
              f"deliberately, or pass --force.", file=sys.stderr)
        return 2

    mod = load_mcp()
    cfg = mcp_defaults()
    dm = Daemon(args.daemon, read_token())
    stats = dm.get("/api/stats")

    slots, records, census = pick_slots(dm)
    ledger = {
        "generated": date.today().isoformat(),
        "bank": {"entries": stats.get("total_memories"),
                 "preset": stats.get("preset"), **census},
        "convention": {
            "chars": "compact JSON as an MCP client receives it "
                     "(separators=(',',':'), ensure_ascii=False)",
            "approx_tokens": "chars // 4 (evals/ladder_sweep.approx_tokens)",
        },
        "config": {
            "compact_payloads": cfg.compact_payloads,
            "entry_text_chars": cfg.entry_text_chars,
            "cortex_width_uncut": CORTEX_WIDTH_UNCUT,
        },
        "manifest": measure_manifest(mod),
        "session_start_block": measure_session_block(),
        "search": measure_search(mod, dm, args.top_k, cfg.entry_text_chars),
        "search_narrow": measure_search(mod, dm, args.narrow_top_k,
                                        cfg.entry_text_chars),
        "fact_get": measure_fact_get(mod, dm, slots, records),
        "recall": measure_recall(mod, dm),
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
        f.write("\n")

    s = ledger["search"]["aggregate"]
    print(f"wrote {out_path}")
    print(f"manifest full: {ledger['manifest']['full']['chars']} chars "
          f"(~{ledger['manifest']['full']['approx_tokens']} tok)")
    print(f"session block: {ledger['session_start_block']['raw_chars']} "
          f"raw chars ({ledger['session_start_block']['chars']} JSON)")
    print(f"labelled facts: {census['facts_labelled']} of "
          f"{census['facts_current']} current"
          + ("  [DUMP TRUNCATED]" if census["facts_dump_truncated"] else ""))
    print(f"search top_k={args.top_k} mean total: "
          f"{s['before']['total_chars']['mean']} -> "
          f"{s['after']['total_chars']['mean']} chars")
    print(f"fact_get mean: "
          f"{ledger['fact_get']['aggregate']['before']['mean']} -> "
          f"{ledger['fact_get']['aggregate']['after']['mean']} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
