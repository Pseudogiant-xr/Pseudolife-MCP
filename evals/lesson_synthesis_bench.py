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
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

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


# Endpoints assume execution INSIDE the daemon container; override via CLI.
TARGETS = {
    "gemma": ("http://pseudolife-extractor:8081/v1", "extractor"),
    "qwen-27b": ("http://host.docker.internal:1234/v1", "Qwen3.6-27B-UD-Q4_K_XL.gguf"),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="all", help="gemma | qwen-27b | all")
    ap.add_argument("--gemma-url", default=os.environ.get("GEMMA_URL"))
    ap.add_argument("--qwen-url", default=os.environ.get("QWEN_URL"))
    args = ap.parse_args()
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


if __name__ == "__main__":
    main()
