"""Classify cortex entities as artifact | system | concept (schema v24).

Offline and one-time. Writes a JSON artifact and NEVER touches the database;
`evals/apply_entity_kinds.py` is the human-gated step that commits it.

Scoping is the dominant token lever, not batch size. An entity only matters if
it carries at least one transient-looking attribute -- otherwise every one of
its facts resolves evergreen whatever its kind. On the live bank that is
2423 facts -> 265 scoped -> 33 rule-confident -> 232 needing judgement,
a 10.4x reduction before a single model call (measured 2026-07-27; these
counts drift as the bank grows -- reproduce with `--scope-only`).

Batch 50. Larger batches degrade through lost-in-the-middle attention, label
streaking (the model pattern-matches its own recent outputs), correlated
failure (one malformed response loses the whole batch) and no retry
granularity. Batching also helps, because this is a comparative judgement --
seeing `0-9-0-release` beside `daemon` makes the distinction salient. Fifty
keeps every item in the high-attention zone while preserving that.

Usage:
    python evals/classify_entity_kinds.py --out evals/results/entity-kinds-<tag>.json
    python evals/classify_entity_kinds.py --gold tests/fixtures/entity_kinds_gold.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Iterator

import psycopg
import urllib.request

KINDS = ("artifact", "system", "concept")
DEFAULT_DSN = os.environ.get(
    "PL_DSN", "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory")
SHIM_URL = os.environ.get("PL_SHIM_URL", "http://127.0.0.1:8082/v1/chat/completions")

# The policy lives in exactly ONE place. freshness.py is pure stdlib by
# design ("can be loaded by file path from the gateway venv"), so load it by
# path rather than through the package: `pseudolife_memory.memory.__init__`
# pulls torch, which this offline harness must not require. One copy means no
# drift, and no parity test to keep two copies honest.
def _load_freshness():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pl_freshness",
        Path(__file__).resolve().parents[1]
        / "pseudolife_memory" / "memory" / "freshness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_freshness = _load_freshness()


def _is_transient(attribute_norm: str) -> bool:
    """True when a `system` entity's fact at this attribute would be volatile."""
    return _freshness.resolve_class("system", attribute_norm) == "volatile"

# Names that are frozen in time by construction -- no model needed.
_RULE_ARTIFACT = re.compile(
    r"(^|-)(20\d{2}-\d{2}|release|commit-|pr-\d+|programme)|(^|-)v?\d+-\d+-\d+(-|$)")

SYSTEM_PROMPT = """You classify entities from a personal knowledge base.

For each entity name, answer with exactly one kind:

- "artifact": frozen in time. A specific release, commit, dated run, PR, or a
  completed programme. Facts about it stay true forever, because the thing
  itself never changes again.
- "system": live and mutable. A daemon, server, repository, deployed model,
  or running service. Facts about its current state go out of date.
- "concept": abstract or definitional. A design pattern, policy, lesson, or
  idea. Facts about it are durable.

The distinction that matters: "0-9-0-release" is an artifact (version 0.9.0
shipped with whatever it shipped with, forever), while "daemon" is a system
(its version changes under you).

Reply with ONLY a JSON object mapping each name to its kind. No prose."""


def batched(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def rule_kind(entity_norm: str) -> str | None:
    """Confident lexical classification, or None to defer to the model."""
    return "artifact" if _RULE_ARTIFACT.search(entity_norm or "") else None


def scope_entities(rows: list[tuple[str, str]]) -> list[str]:
    """Entities whose kind can actually change an outcome, in stable order."""
    keep, seen = [], set()
    relevant = {e for e, a in rows if _is_transient(a)}
    for e, _a in rows:
        if e in relevant and e not in seen:
            seen.add(e)
            keep.append(e)
    return keep


def parse_batch(text: str, batch: list[str]) -> dict[str, str]:
    """Labels for the requested entities. Unparseable input yields {} -- the
    caller then writes no kind, and resolve_class falls back to evergreen.
    Never guess a label."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n|\n```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        raw = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    want = set(batch)
    return {k: v for k, v in raw.items()
            if k in want and isinstance(v, str) and v in KINDS}


def _ask(batch: list[str], model: str, timeout: float) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps(batch)}],
    }).encode()
    req = urllib.request.Request(
        SHIM_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def _fetch_rows(dsn: str) -> list[tuple[str, str]]:
    with psycopg.connect(dsn) as conn:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT entity_norm, attribute_norm FROM facts "
            "WHERE status='current' ORDER BY entity_norm, attribute_norm").fetchall()]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dsn", default=DEFAULT_DSN)
    p.add_argument("--model", default="claude-fable-5")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--gold", type=Path, default=None,
                   help="Score against a gold set instead of classifying the bank.")
    p.add_argument("--scope-only", action="store_true",
                   help="Print facts/scoped/rule/model counts for the live "
                        "bank and exit -- no model call, no shim needed. "
                        "Reproduces the backfill's scoping numbers on demand.")
    a = p.parse_args()

    if a.scope_only and a.gold:
        p.error("--scope-only classifies the live bank; it is incompatible with --gold")
    if not a.scope_only and a.out is None:
        p.error("--out is required unless --scope-only is given")

    if a.gold:
        gold = json.loads(a.gold.read_text(encoding="utf-8"))
        names = [g["entity_norm"] for g in gold]
        truth = {g["entity_norm"]: g["kind"] for g in gold}
        fact_count = None
    else:
        rows = _fetch_rows(a.dsn)
        names, truth = scope_entities(rows), {}
        fact_count = len(rows)

    ruled = {n: k for n in names if (k := rule_kind(n))}
    ask = [n for n in names if n not in ruled]

    if a.scope_only:
        print(f"facts={fact_count} scoped={len(names)} rule={len(ruled)} "
              f"model={len(ask)}")
        return

    print(f"scoped={len(names)} rule={len(ruled)} model={len(ask)} "
          f"batch={a.batch_size}")

    labels, failures = dict(ruled), []
    for i, batch in enumerate(batched(ask, a.batch_size), 1):
        try:
            got = parse_batch(_ask(batch, a.model, a.timeout), batch)
        except Exception as exc:                      # noqa: BLE001
            got, _ = {}, failures.append(f"batch {i}: {type(exc).__name__}")
        missing = [n for n in batch if n not in got]
        if missing:
            failures.append(f"batch {i}: {len(missing)} unlabelled")
        labels.update(got)
        print(f"  batch {i}: {len(got)}/{len(batch)} labelled")

    out = {"model": a.model, "batch_size": a.batch_size,
           "scoped": len(names), "rule_labelled": len(ruled),
           "model_labelled": len(labels) - len(ruled),
           "unlabelled": len(names) - len(labels),
           "failures": failures, "labels": labels,
           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    if truth:
        scored = [(n, truth[n], labels.get(n, "(none)")) for n in names]
        correct = sum(1 for _n, t, g in scored if t == g)
        out["accuracy"] = round(correct / len(scored), 4)
        out["errors"] = [{"entity": n, "gold": t, "got": g}
                         for n, t, g in scored if t != g]
        print(f"accuracy {correct}/{len(scored)} = {out['accuracy']:.1%}")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
