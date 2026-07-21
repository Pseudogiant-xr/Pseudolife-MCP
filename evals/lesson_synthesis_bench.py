"""Lesson-synthesis benchmark — how well does each model turn outcome SIGNALS
into procedural LESSONS (the ``extract_lessons`` task, schema v10)?

Distinct from ``ladder_sweep.py`` (which benches the *declarative* dream
extraction). This one stresses the procedural path: clustering related signals,
and — the discriminators — getting **polarity** (+ do / - avoid) and
**direction** (don't invert a correction) right. Those are exactly where the
small CPU model fumbled in the v1 live smoke.

PRIME TARGET is the shipped Gemma 2B sidecar (what a real end-user runs); the
27B on the 4090 is the quality CEILING to measure the gap against, not the
optimisation target.

Self-contained (stdlib only) so it runs *inside* the daemon container, where
both endpoints are reachable:
  * gemma     -> http://pseudolife-extractor:8081/v1   (model ``extractor``)
  * qwen-27b  -> http://host.docker.internal:1234/v1   (model ``Qwen3.6-27B-...``)

    docker cp evals/lesson_synthesis_bench.py pseudolife-mcp-daemon:/tmp/lb.py
    docker exec pseudolife-mcp-daemon python /tmp/lb.py --target all

The ``_LESSON_SYSTEM_PROMPT`` / ``_format_signals`` below are kept in sync with
``pseudolife_memory/memory/dream.py`` — this file is where the prompt is *tuned*;
port the winner back to dream.py and re-run the unit + live tests.

A second, independent rung (``--infer``) benches
``OpenAICompatExtractor.infer_outcomes`` (the auto-outcome-inference stage
that runs at episode close) against 8 fixtures. Unlike the lesson-synthesis
prompt above, this rung *consumes* the real prompt/parser straight from
``pseudolife_memory.memory.dream`` (imported lazily, only when the rung
actually runs) rather than keeping a tunable copy — it is smoke-testing the
shipped behaviour, not iterating on it. ``--infer --dry-run`` prints the
fixtures and exits without importing anything or touching the network:

    python evals/lesson_synthesis_bench.py --infer --dry-run
    python evals/lesson_synthesis_bench.py --infer   # probes :8081 / :8082
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# ── prompt under test (sync with dream.py) ─────────────────────────────────
_LESSON_SYSTEM_PROMPT = (
    "You consolidate an agent's work-outcome signals into reusable LESSONS. Each "
    "signal records something that happened while doing a task: a success, a "
    "failure/dead-end, or a user correction. Produce durable, actionable lessons "
    'as JSON: {"lessons":[{"task":..,"aspect":..,"lesson":..,"about":..,'
    '"polarity":"+"|"-","outcome":"success"|"failure"|"correction",'
    '"confidence":0..1}]}.\n'
    "- task = the kind of task, reusing stable wording across signals.\n"
    "- aspect = approach | pitfall | tool-choice | correction.\n"
    "- lesson = the actionable takeaway, phrased as what to DO (or what to avoid).\n"
    "- about = the tool/source/approach the lesson concerns.\n"
    "- outcome = the signal class it came from.\n"
    '- polarity = "+" when the lesson is something to DO — an approach that worked, '
    'or the corrected, now-correct way; "-" ONLY when the lesson is something to '
    'AVOID (a dead-end), phrased as "avoid X". A CORRECTION is almost always "+": '
    "state the new correct behavior to follow, never the mistake.\n"
    "Cluster related signals into one lesson. SKIP trivial or non-durable signals "
    "— generic knowledge any competent agent already has (e.g. basic "
    "language/library usage), one-off chatter, or anything a future run would not "
    'benefit from recalling. Return {"lessons":[]} if nothing qualifies.'
)


def _format_signals(signals: list[dict]) -> str:
    lines = []
    for s in signals or []:
        parts = [f"[{s.get('outcome', '?')}]", f"task={s.get('task', '')!r}"]
        if s.get("about"):
            parts.append(f"about={s['about']!r}")
        if s.get("detail"):
            parts.append(f"detail={s['detail']!r}")
        if s.get("polarity"):
            parts.append(f"polarity={s['polarity']}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def call_model(base_url: str, model: str, signals: list[dict],
               timeout: float = 150.0) -> tuple[list[dict], float]:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _LESSON_SYSTEM_PROMPT},
            {"role": "user", "content": _format_signals(signals)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 600,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=body,
        headers={"content-type": "application/json"}, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    dt = time.time() - t0
    content = data["choices"][0]["message"]["content"] or ""
    s, e = content.find("{"), content.rfind("}")
    if s != -1 and e > s:
        content = content[s:e + 1]
    try:
        parsed = json.loads(content)
        lessons = parsed.get("lessons", []) if isinstance(parsed, dict) else []
    except Exception:
        lessons = []
    return [L for L in lessons if isinstance(L, dict)], dt


# ── fixtures: signals + expected lesson properties ─────────────────────────
# Each `check` is a required lesson, matched to the model's best output lesson by
# `about_has`; then scored on polarity / outcome / direction (value tokens).
FIXTURES = [
    {
        "id": "simple_success",
        "signals": [
            {"outcome": "success", "task": "run a database migration",
             "about": "gh-ost", "detail": "gh-ost ran the migration online with zero downtime"},
        ],
        "min_lessons": 1, "max_lessons": 1,
        "checks": [
            {"about_has": "gh-ost", "polarity": "+", "outcome": "success",
             "value_any": ["online", "zero", "downtime", "gh-ost"], "value_not": []},
        ],
    },
    {
        "id": "simple_deadend",
        "signals": [
            {"outcome": "failure", "task": "debug a flaky CI job",
             "about": "re-running the job", "polarity": "-",
             "detail": "just re-running the failing job never surfaced the cause; it was a race condition"},
        ],
        "min_lessons": 1, "max_lessons": 1,
        "checks": [
            {"about_has": "re-run", "polarity": "-", "outcome": "failure",
             "value_any": ["race", "re-run", "rerun", "never"], "value_not": []},
        ],
    },
    {
        # The discriminator: a fail then a fix for the SAME task. The "worked"
        # lesson must be POSITIVE; the "aborted" one NEGATIVE. Gemma inverted this.
        "id": "merged_fail_then_success",
        "signals": [
            {"outcome": "failure", "task": "deploy the engine to a remote host",
             "about": "tar --same-owner", "polarity": "-",
             "detail": "extract aborted with chown errors because the tar carried Windows uids"},
            {"outcome": "success", "task": "deploy the engine to a remote host",
             "about": "tar --no-same-owner",
             "detail": "extracting with --no-same-owner skipped the chown and succeeded"},
        ],
        "min_lessons": 1, "max_lessons": 2,
        "checks": [
            {"about_has": "no-same-owner", "polarity": "+", "outcome": "success",
             "value_any": ["no-same-owner", "--no-same-owner"],
             "value_not": ["avoid --no-same-owner", "don't use --no-same-owner",
                           "do not use --no-same-owner"]},
        ],
    },
    {
        # Correction must keep DIRECTION: adopt the NEW value, not invert it.
        "id": "correction_direction",
        "signals": [
            {"outcome": "correction", "task": "configure the postgres search_path",
             "about": "search_path config",
             "detail": "user corrected me: use a DATABASE-level default search_path, "
                       "NOT a code-level pin — the code-level pin broke the graph tests"},
        ],
        "min_lessons": 1, "max_lessons": 1,
        "checks": [
            {"about_has": "search_path", "polarity": "+", "outcome": "correction",
             "value_any": ["database-level", "database level", "db-level"],
             "value_not": ["prefer code-level", "code-level pinning over",
                           "use a code-level", "prefer a code-level"]},
        ],
    },
    {
        "id": "cluster_three_success",
        "signals": [
            {"outcome": "success", "task": "speed up the python test suite",
             "about": "pytest-xdist", "detail": "running with -n auto cut wall time ~3x"},
            {"outcome": "success", "task": "speed up the python test suite",
             "about": "a module-scoped embedder fixture", "detail": "loading the embedder once per module not per test saved ~1.5s each"},
            {"outcome": "success", "task": "speed up the python test suite",
             "about": "fast PG truncate", "detail": "truncating tables between tests instead of recreating the DB"},
        ],
        "min_lessons": 1, "max_lessons": 3,
        "checks": [
            {"about_has": "xdist", "polarity": "+", "outcome": "success",
             "value_any": ["xdist", "-n", "parallel", "3x"], "value_not": []},
        ],
    },
    {
        "id": "noise_skip",
        "signals": [
            {"outcome": "success", "task": "print a greeting",
             "detail": "printed 'hello' to stdout once"},
        ],
        "min_lessons": 0, "max_lessons": 0,
        "checks": [],
    },
]


def _norm(s) -> str:
    return str(s or "").lower()


def _match_lesson(lessons: list[dict], about_has: str) -> dict | None:
    """Best lesson whose `about` (or text) mentions the expected subject."""
    key = about_has.lower()
    for L in lessons:
        if key in _norm(L.get("about")):
            return L
    for L in lessons:  # fall back to lesson text
        if key in _norm(L.get("lesson")):
            return L
    return None


def score_scenario(fx: dict, lessons: list[dict]) -> dict:
    n = len(lessons)
    count_ok = fx["min_lessons"] <= n <= fx["max_lessons"]
    checks = []
    for c in fx["checks"]:
        L = _match_lesson(lessons, c["about_has"])
        if L is None:
            checks.append({"about": c["about_has"], "found": False,
                           "polarity_ok": False, "outcome_ok": False,
                           "direction_ok": False})
            continue
        val = _norm(L.get("lesson"))
        polarity_ok = _norm(L.get("polarity")) == c["polarity"]
        outcome_ok = _norm(L.get("outcome")) == c["outcome"]
        dir_ok = (any(t.lower() in val for t in c["value_any"]) if c["value_any"] else True) \
            and not any(t.lower() in val for t in c["value_not"])
        checks.append({"about": c["about_has"], "found": True,
                       "polarity_ok": polarity_ok, "outcome_ok": outcome_ok,
                       "direction_ok": dir_ok})
    # full pass = count_ok AND every check found+polarity+outcome+direction
    full = count_ok and all(
        ch["found"] and ch["polarity_ok"] and ch["outcome_ok"] and ch["direction_ok"]
        for ch in checks
    )
    return {"id": fx["id"], "n_lessons": n, "count_ok": count_ok,
            "checks": checks, "full_pass": full, "lessons": lessons}


def run_target(name: str, base_url: str, model: str) -> dict:
    rows, agg = [], {"full": 0, "count_ok": 0, "found": 0, "polarity": 0,
                     "outcome": 0, "direction": 0, "checks": 0, "secs": 0.0}
    for fx in FIXTURES:
        try:
            lessons, dt = call_model(base_url, model, fx["signals"])
        except Exception as exc:  # noqa: BLE001
            rows.append({"id": fx["id"], "error": str(exc)})
            continue
        agg["secs"] += dt
        r = score_scenario(fx, lessons)
        r["secs"] = round(dt, 1)
        rows.append(r)
        agg["full"] += int(r["full_pass"])
        agg["count_ok"] += int(r["count_ok"])
        for ch in r["checks"]:
            agg["checks"] += 1
            agg["found"] += int(ch["found"])
            agg["polarity"] += int(ch["polarity_ok"])
            agg["outcome"] += int(ch["outcome_ok"])
            agg["direction"] += int(ch["direction_ok"])
    return {"target": name, "model": model, "scenarios": len(FIXTURES),
            "aggregate": agg, "rows": rows}


# ── outcome-inference rung (--infer): fixtures + scoring ───────────────────
# Contexts mirror exactly what MemoryService._episode_inference_context builds
# for a closed session: "Session: <title>" then "- (source)[ [superseded]]
# text" lines (service.py) — including status-source entries, which
# infer_outcomes deliberately does NOT exclude (unlike fact extraction).
#
# `expect` is either a set of (task-ish keyword, outcome) pairs — a claim is a
# match when the keyword is a substring of the claimed `task`+`about` (case-
# insensitive) and `outcome` matches exactly — or the literal string
# "abstain" for fixtures with no real, resolvable outcome in the record.
INFER_FIXTURES = [
    {"name": "deploy-succeeded",
     "context": ("Session: deploy the auth service to staging\n"
                 "- (pseudolife) ran ops/update.ps1 against staging: backup, "
                 "rebuild, health check\n"
                 "- (status) health check green, service responding on :8080"),
     "expect": {("deploy", "success")}},
    {"name": "dead-end-hit",
     "context": ("Session: fix flaky websocket test\n"
                 "- (status) tried raising the timeout to 30s — still flaky\n"
                 "- (pseudolife) Root cause: the test polls before the server "
                 "binds; timeout changes are a dead-end, poll readiness "
                 "instead"),
     # Keyword is title-derived ("websocket"), like every other fixture: the
     # schema routes tool/approach nouns ("timeout") into `about`, not `task`,
     # so a task-field match on "timeout" would punish schema-compliant models
     # (review finding, 2026-07-18).
     "expect": {("websocket", "failure")}},
    {"name": "user-corrected-me",
     "context": ("Session: configure the postgres search_path\n"
                 "- (pseudolife) set a code-level pin for search_path in the "
                 "app config\n"
                 "- (status) user corrected me: use a DATABASE-level default "
                 "search_path instead, not a code-level pin — the pin broke "
                 "the graph tests"),
     "expect": {("search_path", "correction")}},
    {"name": "mixed-session",
     "context": ("Session: speed up the python test suite\n"
                 "- (pseudolife) tried pytest-xdist -n auto\n"
                 "- (status) xdist cut wall time roughly 3x, keeping it\n"
                 "- (pseudolife) also tried sharing one DB connection pool "
                 "across workers\n"
                 "- (status) pool sharing caused cross-test state leaks — "
                 "reverted, dead end"),
     # Second keyword is "pool", not "test suite": the schema asks for a
     # short task-type phrase per outcome, and a model that correctly
     # splits the two sub-attempts words the second around the pool
     # experiment — demanding the session title in both punished correct
     # behavior (2026-07-18 E4B diagnostics).
     "expect": {("test suite", "success"), ("pool", "failure")}},
    {"name": "ambiguous-1",
     "context": ("Session: reading about vector databases\n"
                 "- (notes) pgvector supports HNSW and IVFFlat indexes"),
     "expect": "abstain"},
    {"name": "ambiguous-2",
     "context": ("Session: brainstorming names for the new CLI flag\n"
                 "- (pseudolife) options considered: --dry-run, --preview, "
                 "--no-op\n"
                 "- (pseudolife) no decision made yet, revisit next session"),
     "expect": "abstain"},
    {"name": "status-only-session",
     "context": ("Session: nightly backup job\n"
                 "- (status) backup started 02:00 UTC\n"
                 "- (status) backup completed 02:14 UTC, 12GB written, "
                 "checksum verified"),
     "expect": {("backup", "success")}},
    {"name": "single-entry-thin",
     "context": ("Session: fix the stale version link in the README\n"
                 "- (pseudolife) updated the README badge link from v0.7.0 "
                 "to v0.8.0"),
     "expect": {("readme", "success")}},
]


def score_infer_fixture(fx: dict, claims: list[dict] | None) -> dict:
    """Score one --infer fixture against the extractor's claims.

    ``claims is None`` (malformed reply) always scores 0 with a note.
    Abstain fixtures score 1.0 iff the extractor returned ``[]``. Outcome
    fixtures score the fraction of expected (keyword, outcome) pairs matched
    — keyword checked via substring over task+about, outcome exact — and
    raise an ``extra_flag`` when the extractor claimed more than
    ``len(expect) + 1`` things (over-claiming on a bounded record).
    """
    expect = fx["expect"]
    if claims is None:
        return {"name": fx["name"], "score": 0.0, "n_claims": 0,
                "extra_flag": False, "note": "malformed reply (None)"}
    if expect == "abstain":
        ok = claims == []
        return {"name": fx["name"], "score": 1.0 if ok else 0.0,
                "n_claims": len(claims), "extra_flag": False,
                "note": "" if ok else "expected abstain, got claims"}
    remaining = set(expect)
    for c in claims:
        # Match against task AND about: the schema routes approach nouns
        # into `about`, and a model may legitimately keep the session-level
        # task phrase while naming the sub-approach there (2026-07-18:
        # Sonnet words mixed-session that way; E4B splits task types —
        # both are schema-compliant, the metric must accept either).
        grounding = _norm(f"{c.get('task') or ''} {c.get('about') or ''}")
        outcome = str(c.get("outcome", "")).strip()
        hit = next((pair for pair in remaining
                    if pair[0].lower() in grounding and pair[1] == outcome),
                   None)
        if hit is not None:
            remaining.discard(hit)
    matched = len(expect) - len(remaining)
    score = matched / len(expect) if expect else 0.0
    extra_flag = len(claims) > len(expect) + 1
    return {"name": fx["name"], "score": round(score, 3),
            "n_claims": len(claims), "extra_flag": extra_flag,
            "note": "extra claims beyond expected+1" if extra_flag else ""}


def _probe_infer(base_url: str, timeout: float = 3.0) -> bool:
    """Reachability check — GET <base_url>/models (llama.cpp/OpenAI-compat
    servers serve it); used only to auto-pick an --infer endpoint."""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


# Candidates probed in order when --infer-url isn't given explicitly.
INFER_TARGETS = [
    ("gemma", "http://127.0.0.1:8081/v1", "extractor"),
    ("sonnet-shim", "http://127.0.0.1:8082/v1", "claude-sonnet-5"),
]


def run_infer(base_url: str, model: str) -> dict:
    """Run every INFER_FIXTURES entry through a real
    ``OpenAICompatExtractor.infer_outcomes`` call. Imports
    ``pseudolife_memory`` lazily so plain fixture printing (--dry-run) never
    needs the package or the network."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    extractor = OpenAICompatExtractor(base_url, model, timeout_seconds=150.0)
    rows = []
    for fx in INFER_FIXTURES:
        try:
            claims = extractor.infer_outcomes(fx["context"], cap=3)
        except Exception as exc:  # noqa: BLE001 — ExtractorError or transport
            rows.append({"name": fx["name"], "score": 0.0, "n_claims": 0,
                        "extra_flag": False, "note": f"error: {exc}"})
            continue
        rows.append(score_infer_fixture(fx, claims))
    scores = [r["score"] for r in rows]
    mean_score = round(sum(scores) / len(scores), 3) if scores else 0.0
    return {"base_url": base_url, "model": model, "rows": rows,
            "mean_score": mean_score}


def print_infer_dry_run() -> None:
    print(f"\n=== --infer --dry-run: {len(INFER_FIXTURES)} fixtures, no "
          "endpoint call ===")
    for fx in INFER_FIXTURES:
        print(f"\n[{fx['name']}]")
        print(fx["context"])
        print(f"  expect: {fx['expect']}")


def print_infer_report(result: dict) -> None:
    print(f"\n=== outcome-inference ({result['model']} @ {result['base_url']}) ===")
    for r in result["rows"]:
        print(f"   - {r['name']:24s} score={r['score']:.2f} "
              f"claims={r['n_claims']} extra_flag={r['extra_flag']} {r['note']}")
    print(f"  mean score: {result['mean_score']:.3f}  "
          f"({len(result['rows'])} fixtures)")
    print("\n===JSON===")
    print(json.dumps(result, ensure_ascii=False))


# Endpoints assume execution INSIDE the daemon container; override via CLI.
TARGETS = {
    "gemma": ("http://pseudolife-extractor:8081/v1", "extractor"),
    "qwen-27b": ("http://host.docker.internal:1234/v1", "Qwen3.6-27B-UD-Q4_K_XL.gguf"),
}


def write_result(result: dict, path: Path) -> Path:
    """Persist a bench result so a published number has evidence behind it.

    Both rungs used to print and forget, which is how the `--infer` scores
    reached the CHANGELOG with no artifact anywhere in the repo. Guarded by
    tests/test_eval_evidence.py.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="all", help="gemma | qwen-27b | all")
    ap.add_argument("--gemma-url", default=os.environ.get("GEMMA_URL"))
    ap.add_argument("--qwen-url", default=os.environ.get("QWEN_URL"))
    ap.add_argument("--infer", action="store_true",
                    help="run the outcome-inference rung (8 fixtures) "
                         "instead of lesson synthesis")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --infer: print fixtures and exit, no import/network")
    ap.add_argument("--infer-url", default=os.environ.get("INFER_URL"))
    ap.add_argument("--infer-model", default=os.environ.get("INFER_MODEL"))
    ap.add_argument("--out", type=Path, default=None,
                    help="write the result JSON here (evals/results/...); "
                         "any score you intend to publish needs one")
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.infer:
        if args.dry_run:
            print_infer_dry_run()
            if args.out:
                write_result({"rung": "infer", "dry_run": True,
                              "n_fixtures": len(INFER_FIXTURES),
                              "fixtures": [f["name"] for f in INFER_FIXTURES]},
                             args.out)
            return
        base_url, model = args.infer_url, args.infer_model
        if not base_url:
            for name, url, mdl in INFER_TARGETS:
                if _probe_infer(url):
                    base_url, model = url, mdl
                    print(f"(auto-picked reachable endpoint: {name} @ {url})")
                    break
            else:
                sys.exit(
                    "no --infer-url given and none of "
                    f"{[u for _, u, _ in INFER_TARGETS]} is reachable — "
                    "start an extractor endpoint or pass --dry-run")
        result = run_infer(base_url, model or "extractor")
        print_infer_report(result)
        if args.out:
            write_result(result, args.out)
        return

    if args.gemma_url:
        TARGETS["gemma"] = (args.gemma_url, TARGETS["gemma"][1])
    if args.qwen_url:
        TARGETS["qwen-27b"] = (args.qwen_url, TARGETS["qwen-27b"][1])

    names = list(TARGETS) if args.target == "all" else [args.target]
    out = {}
    for nm in names:
        base, model = TARGETS[nm]
        out[nm] = run_target(nm, base, model)
        a = out[nm]["aggregate"]
        print(f"\n=== {nm} ({model}) ===")
        print(f"  full-pass scenarios : {a['full']}/{len(FIXTURES)}")
        print(f"  count_ok            : {a['count_ok']}/{len(FIXTURES)}")
        print(f"  checks found        : {a['found']}/{a['checks']}")
        print(f"  polarity correct    : {a['polarity']}/{a['checks']}")
        print(f"  outcome correct     : {a['outcome']}/{a['checks']}")
        print(f"  direction correct   : {a['direction']}/{a['checks']}")
        print(f"  total secs          : {a['secs']:.1f}")
        for r in out[nm]["rows"]:
            if "error" in r:
                print(f"   - {r['id']:26s} ERROR {r['error']}")
                continue
            flag = "PASS" if r["full_pass"] else "FAIL"
            print(f"   - {r['id']:26s} {flag}  n={r['n_lessons']} "
                  f"checks={[{k: v for k, v in c.items() if k != 'about'} for c in r['checks']]}")
    print("\n===JSON===")
    print(json.dumps(out, ensure_ascii=False))
    if args.out:
        write_result(out, args.out)


if __name__ == "__main__":
    main()
