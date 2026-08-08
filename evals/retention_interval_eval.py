"""Retention-interval eval — behavior as a function of time-since-stored.

Preregistration: docs/superpowers/specs/2026-08-08-retention-interval-eval-design.md

Two studies:

* **Study A** (deterministic, CPU + one extractor bank build): the ladder's
  update-pair bank, queried under simulated clocks offset {0, 7, 30, 90, 365}
  days past the build. H1 preregisters EXACT-ZERO deltas — nothing in the
  serving path reads the clock — certified by per-offset ``gold_recoverable``
  / ``stale_leak`` plus a canonical hash over every served context.

* **Study B** (small-n): synthetic volatile/slow facts seeded through the
  real write paths, read at +90 simulated days (volatile past its 2xTTL
  staleness horizon, slow not), rendered to an answerer in paired arms that
  differ ONLY in whether ``effective_confidence``/``stale`` are present.
  Without ``--answer-url`` the harness writes the rendered contexts and
  stops — the judged half runs on the reproducible bench server later, from
  the same artifact.

Clock injection swaps the freshness module's clock (``freshness._time``) for
a frozen shim — the single seam every default-clock read funnels through
(CortexRecord/WorldRecord delegate to the freshness functions with ``now``
passthrough). Write-time stamps are NOT touched: facts age because the read
clock advances past their real ``asserted_at``. No production knob is added.

Usage:
    PYTHONPATH=. python evals/retention_interval_eval.py --study a --rung e4b-v3 --tag ret-<date>
    PYTHONPATH=. python evals/retention_interval_eval.py --study b --tag ret-<date> [--answer-url http://127.0.0.1:1234/v1]
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/

from pseudolife_memory.memory import freshness                    # noqa: E402

DAY = 86400.0
PREREG = "docs/superpowers/specs/2026-08-08-retention-interval-eval-design.md"
OFFSETS_DAYS = (0, 7, 30, 90, 365)
RESULTS_DIR = Path(__file__).resolve().parent / "results"


# ── clock injection ──────────────────────────────────────────────────────

class _FrozenClock:
    """Stand-in for the ``time`` module inside ``freshness`` only."""

    def __init__(self, now: float) -> None:
        self._now = float(now)

    def time(self) -> float:
        return self._now


@contextlib.contextmanager
def frozen_now(epoch: float):
    """Freeze the freshness layer's default clock at ``epoch``.

    Swaps ``freshness._time`` (the module's imported ``time``) for a frozen
    shim and restores the original on exit, exception included. Scoped to the
    freshness module so write-time stamps (which import ``time`` directly in
    their own modules) keep the real clock.
    """
    real = freshness._time
    freshness._time = _FrozenClock(epoch)
    try:
        yield
    finally:
        freshness._time = real


# ── shared pure helpers ──────────────────────────────────────────────────

def context_hash(payload) -> str:
    """Canonical sha256 over a served context: key-order-insensitive,
    content-sensitive."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def strip_flags(obj):
    """Deep-copy ``obj`` with exactly ``effective_confidence`` and ``stale``
    removed at every depth — the sole difference between Study B's arms."""
    obj = copy.deepcopy(obj)

    def _walk(o):
        if isinstance(o, dict):
            o.pop("effective_confidence", None)
            o.pop("stale", None)
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(obj)
    return obj


# Deterministic-first scoring: an answer that states the stored value with no
# hedge is an unqualified serve. Hedge markers are deliberately few and
# literal — the judge (reproducible server only) arbitrates hedging beyond
# this list; this classifier must never need arbitration itself.
HEDGE_MARKERS = (
    "as of", "may be outdated", "might be outdated", "may have changed",
    "might have changed", "not sure", "re-verify", "stale", "outdated",
    "don't know", "do not know", "no longer certain",
)


def unqualified_stale(answer: str, value: str) -> bool:
    """True when ``value`` appears in ``answer`` with no hedge marker.

    Sentence-terminal punctuation is folded to whitespace before matching:
    ``value_present``'s word boundary rejects dot-adjacent hits (so "4"
    never matches "2.4"), which would otherwise miss a value at sentence
    end ("...port is 8080."). Interior dots (versions, serials) survive —
    only punctuation followed by whitespace/end is folded.
    """
    import re
    from ladder_sweep import value_present
    a = re.sub(r"[.!?,;](?=\s|$)", " ", (answer or "").casefold())
    if not value_present(a, value.casefold()):
        return False
    return not any(m in a for m in HEDGE_MARKERS)


def write_artifact(out: Path, payload: dict) -> None:
    payload = {"preregistration": PREREG, **payload,
               "written_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(f"artifact -> {out}", flush=True)


# ── Study A — KU interval sweep over the ladder bank ─────────────────────

def run_study_a(rung: str, offsets_days=OFFSETS_DAYS) -> dict:
    """One bank build, N simulated query clocks. Requires the bench Postgres
    and the rung's extractor endpoint (like any ladder run)."""
    import tempfile

    import ladder_sweep as ls

    # ignore_cleanup_errors: same ChromaDB file-lock caveat as ladder_sweep —
    # the PersistentClient keeps chroma.sqlite3 open past service teardown on
    # Windows.
    with tempfile.TemporaryDirectory(prefix="plret_a_",
                                     ignore_cleanup_errors=True) as td:
        svc = ls.build_service(Path(td))
        ls.ingest(svc)
        extractor = ls.make_extractor(ls.RUNGS[rung])
        elapsed, tally = ls.consolidate(svc, extractor)
        base_now = time.time()

        per_offset: dict[str, dict] = {}
        for days in offsets_days:
            with frozen_now(base_now + days * DAY):
                metrics = ls.measure_cortex(svc)
                served = []
                for p in ls.PAIRS:
                    res = svc.cortex_search(p["question"], top_k=5,
                                            min_score=0.3)
                    served.append({"question": p["question"],
                                   "entries": res.get("entries", [])})
            # Latency/tokens vary run-to-run and are not part of the identity
            # claim — hash the content-bearing fields only.
            per_offset[str(days)] = {
                "gold_recoverable": metrics["gold_recoverable"],
                "stale_leak": metrics["stale_leak"],
                "context_sha256": context_hash(served),
            }

    hashes = {v["context_sha256"] for v in per_offset.values()}
    golds = {v["gold_recoverable"] for v in per_offset.values()}
    stales = {v["stale_leak"] for v in per_offset.values()}
    return {
        "rung": rung,
        "consolidate_seconds": round(elapsed, 1),
        "tally": tally,
        "offsets": per_offset,
        "h1_time_invariant": len(hashes) == 1 and len(golds) == 1
                             and len(stales) == 1,
    }


# ── Study B — flag-efficacy arms ─────────────────────────────────────────

# Synthetic volatile/slow facts. Entities are systems (state rots); values are
# distinctive tokens so ``value_present`` scoring is unambiguous. At the +90d
# query clock every ``volatile`` fact is past its 2x21d staleness horizon and
# every ``slow`` fact (TTL 270d) is not — the fresh half of the design.
STUDY_B_FACTS = [
    # (entity, attribute, value, freshness_class)
    ("build-farm", "active runner-count", "14", "volatile"),
    ("edge-proxy", "current tls-cert-serial", "5c31f2", "volatile"),
    ("billing-api", "deployed version", "3.8.1", "volatile"),
    ("metrics-db", "current retention-days", "45", "volatile"),
    ("auth-gateway", "live replica-count", "6", "volatile"),
    ("search-cluster", "current shard-count", "24", "volatile"),
    ("cache-tier", "deployed engine", "keydb", "volatile"),
    ("ingest-queue", "current max-lag-seconds", "120", "volatile"),
    ("web-frontend", "live canary-percent", "15", "volatile"),
    ("report-worker", "current batch-size", "500", "volatile"),
    ("archive-store", "compression codec", "zstd-19", "slow"),
    ("license-server", "renewal cadence", "annual-march", "slow"),
    ("backup-vault", "encryption scheme", "aes-256-gcm", "slow"),
    ("dns-zone", "registrar", "gandi", "slow"),
    ("build-image", "base distro", "debian-13", "slow"),
    ("artifact-repo", "storage backend", "minio", "slow"),
    ("vpn-concentrator", "auth protocol", "wireguard-psk", "slow"),
    ("mail-relay", "upstream provider", "postmark", "slow"),
    ("status-page", "hosting vendor", "netlify", "slow"),
    ("cert-authority", "root expiry-year", "2040", "slow"),
]

QUERY_OFFSET_DAYS = 90

_ANSWER_SYSTEM = (
    "You are answering questions from a personal memory store. The MEMORY "
    "block below is everything you know. Answer the question from it in one "
    "or two sentences. If the memory looks unreliable for answering, say so "
    "instead of asserting a value.")


def _seed(svc, facts, force_class: str | None = None) -> float:
    """Write facts through the real path; return the seed wall-clock."""
    for entity, attribute, value, cls in facts:
        svc.world_write(entity, attribute, value,
                        source_url="https://bench.invalid/retention",
                        source_quote=f"{entity} {attribute} is {value}",
                        freshness_class=force_class or cls)
    return time.time()


def _render_contexts(svc, facts, now: float) -> list[dict]:
    """One flags-visible context per fact, via the flag-bearing read surface
    (world search). The stripped arm derives from these — same bytes minus
    the two keys — so the arms cannot drift apart."""
    rows = []
    with frozen_now(now):
        for entity, attribute, value, cls in facts:
            res = svc.world_search(f"{entity} {attribute}", top_k=3)
            rows.append({
                "entity": entity, "attribute": attribute,
                "seeded_value": value, "freshness_class": cls,
                "question": f"What is the {entity} {attribute.replace('-', ' ')}?",
                "context": res.get("entries", []),
            })
    return rows


def _chat(url: str, system: str, user: str, timeout: float = 120.0) -> str:
    body = json.dumps({
        "model": "bench",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/chat/completions", data=body,
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"] or ""


def _score_arm(rows: list[dict], arm: str, answer_url: str) -> list[dict]:
    scored = []
    for row in rows:
        ctx = row["context"] if arm == "flags_visible" else strip_flags(
            row["context"])
        user = ("MEMORY:\n" + json.dumps(ctx, indent=1, ensure_ascii=False)
                + "\n\nQUESTION: " + row["question"])
        answer = _chat(answer_url, _ANSWER_SYSTEM, user)
        scored.append({
            **{k: row[k] for k in ("entity", "attribute", "seeded_value",
                                   "freshness_class", "question")},
            "arm": arm, "answer": answer,
            "unqualified": unqualified_stale(answer, row["seeded_value"]),
        })
    return scored


def run_study_b(answer_url: str | None) -> dict:
    """Seed, render both arms + the evergreen control; score if an answerer
    is given, else persist contexts for a later judged run."""
    import tempfile

    import ladder_sweep as ls

    out: dict = {"query_offset_days": QUERY_OFFSET_DAYS,
                 "n_facts": len(STUDY_B_FACTS)}

    with tempfile.TemporaryDirectory(prefix="plret_b_",
                                     ignore_cleanup_errors=True) as td:
        svc = ls.build_service(Path(td))
        seeded_at = _seed(svc, STUDY_B_FACTS)
        now = seeded_at + QUERY_OFFSET_DAYS * DAY
        rows = _render_contexts(svc, STUDY_B_FACTS, now)

    # Evergreen control: identical values, no decay anywhere — bounds the
    # answerer's base-rate hedging with no staleness signal in play.
    with tempfile.TemporaryDirectory(prefix="plret_bc_",
                                     ignore_cleanup_errors=True) as td:
        svc = ls.build_service(Path(td))
        seeded_at = _seed(svc, STUDY_B_FACTS, force_class="evergreen")
        now = seeded_at + QUERY_OFFSET_DAYS * DAY
        control_rows = _render_contexts(svc, STUDY_B_FACTS, now)

    out["contexts"] = rows
    out["control_contexts"] = control_rows

    if not answer_url:
        out["scored"] = False
        return out

    out["scored"] = True
    out["arms"] = {
        "flags_visible": _score_arm(rows, "flags_visible", answer_url),
        "flags_stripped": _score_arm(rows, "flags_stripped", answer_url),
        "control_evergreen": _score_arm(control_rows, "flags_visible",
                                        answer_url),
    }
    for name, scored in out["arms"].items():
        stale_rows = [r for r in scored if r["freshness_class"] == "volatile"]
        fresh_rows = [r for r in scored if r["freshness_class"] != "volatile"]
        out.setdefault("summary", {})[name] = {
            "stale_answer_rate": round(
                sum(r["unqualified"] for r in stale_rows)
                / max(len(stale_rows), 1), 3),
            "fresh_answer_rate": round(
                sum(r["unqualified"] for r in fresh_rows)
                / max(len(fresh_rows), 1), 3),
        }
    return out


# ── entry point ──────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--study", choices=("a", "b", "both"), default="both")
    ap.add_argument("--rung", default="e4b-v3",
                    help="ladder rung for Study A's bank build")
    ap.add_argument("--offsets", default=",".join(map(str, OFFSETS_DAYS)),
                    help="comma-separated day offsets for Study A")
    ap.add_argument("--answer-url", default=None,
                    help="OpenAI-compatible answerer for Study B scoring "
                         "(reproducible bench server only for judged output)")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    out = args.out or RESULTS_DIR / f"retention-interval-{args.tag}.json"
    payload: dict = {"tag": args.tag}
    if args.study in ("a", "both"):
        offsets = tuple(int(x) for x in args.offsets.split(","))
        payload["study_a"] = run_study_a(args.rung, offsets)
        print(f"study A: h1_time_invariant={payload['study_a']['h1_time_invariant']}",
              flush=True)
    if args.study in ("b", "both"):
        payload["study_b"] = run_study_b(args.answer_url)
        if payload["study_b"].get("summary"):
            print(f"study B: {json.dumps(payload['study_b']['summary'])}",
                  flush=True)
    write_artifact(out, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
