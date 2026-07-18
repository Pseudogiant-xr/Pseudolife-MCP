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
import json
import os
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


def trajectory_to_turns(traj: dict, *, include_observations: bool = False,
                        observation_chars: int = 2000) -> list[str]:
    """Flatten one LME-V2 trajectory into a list of text turns for ``svc.store``.

    Turn 0 frames the task (goal + environment + outcome). Each subsequent turn
    is one state's agent-visible experience: the URL it was on, its ``thought``,
    and the ``action`` it took. States are already ordered by ``state_index``.

    Text-only. Two fields are intentionally dropped or gated:
      * ``screenshot`` — a PNG path (multimodal); always skipped.
      * ``accessibility_tree`` — the text page observation. Skipped by default:
        it is huge (single trees run to tens of KB) and is the main driver of
        the 115M-token haystacks. ``include_observations=True`` folds it back in,
        truncated to ``observation_chars`` per state. Whether the memory system
        should ingest observations at all is OPEN QUESTION 1 for the smoke.
    """
    env = traj.get("environment", "?")
    domain = traj.get("domain", "?")
    goal = (traj.get("goal") or "").strip()
    outcome = traj.get("outcome", "?")
    turns: list[str] = [
        f"[task | {domain}/{env} | outcome={outcome}] {goal}"
    ]
    for st in traj.get("states", []):
        parts = [f"[step {st.get('state_index')}] url: {st.get('url', '')}"]
        thought = (st.get("thought") or "").strip()
        if thought:
            parts.append(f"thought: {thought}")
        action = st.get("action")          # None on the initial state
        if action:
            parts.append(f"action: {action}")
        if include_observations:
            tree = (st.get("accessibility_tree") or "").strip()
            if tree:
                if observation_chars and len(tree) > observation_chars:
                    tree = tree[:observation_chars] + " ...[truncated]"
                parts.append(f"observation:\n{tree}")
        turns.append("\n".join(parts))
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
