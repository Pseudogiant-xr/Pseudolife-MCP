"""Sonnet-5 recall-tuned teacher labeling via Max-plan subagents (Stage 1.5).

Asymmetric prompt split (docs/superpowers/specs/2026-07-07): Sonnet labels
sessions under evals/prompts/sonnet_recall_system.md, but the stored
training rows keep the UNCHANGED production `_SYSTEM_PROMPT` + `_vocab_hint`
— the student learns "production prompt -> high-recall claims".

Three modes:
  --emit-briefs   write one self-contained brief per source question to
                  evals/data/sonnet_briefs/<qid>.md; a subagent answers each
                  brief by writing evals/data/sonnet_out/<qid>.jsonl
                  (one line per session: {"session_id", "claims": [...]}).
  --ingest        strictly validate sonnet_out/, recompute the vocab chain
                  deterministically, rewrite prompts to production form, and
                  append rows to evals/data/distill-extract-sonnet.jsonl.
                  A question is all-or-nothing: any bad row rejects it.
  --compare       recall proxies vs the Qwen labels on shared sessions.

Vocab evolution is sequential WITHIN a question and independent ACROSS
questions (matching distill_datagen.py), so dispatch is one subagent per
question, fully parallel. KU contamination guard reused verbatim.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/

from pseudolife_memory.memory.cortex import _norm_key             # noqa: E402
from pseudolife_memory.memory.dream import (                      # noqa: E402
    _SYSTEM_PROMPT, _vocab_hint,
)
from distill_datagen import (                                     # noqa: E402
    VOCAB_MAX, _parse_date, validate_claims,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATASET = DATA_DIR / "longmemeval_s_cleaned.json"
BRIEFS_DIR = DATA_DIR / "sonnet_briefs"
OUT_DIR = DATA_DIR / "sonnet_out"
MERGED = DATA_DIR / "distill-extract-sonnet.jsonl"
QWEN_SET = DATA_DIR / "distill-extract.jsonl"
RECALL_PROMPT = (Path(__file__).resolve().parent / "prompts"
                 / "sonnet_recall_system.md")


def _notes(session: list[dict], date: str) -> list[str]:
    return [f"[{date}] {t['role']}: {t['content'].strip()}"
            for t in session if (t.get("content") or "").strip()]


# Slot-key canonicalization (arm B'): the Sonnet labels re-word the same
# property's key instead of reusing it (6.8% exact reuse vs qwen 24.1%),
# which suppresses supersession downstream. Merging is per (entity,
# signature) within one question chain; the first-seen attribute wins so
# the recomputed vocab hint and the labels stay coherent.
_GENERIC_TOKENS = {"a", "an", "and", "at", "for", "in", "of", "on", "or",
                   "the", "to", "up"}
_STEM_SUFFIXES = ("ation", "ment", "ness", "ing", "ed", "es", "al", "s")


def _stem(tok: str) -> str:
    for suf in _STEM_SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def _key_sig(attribute: str) -> frozenset[str]:
    toks = {_stem(t) for t in _norm_key(attribute).split("-") if t}
    return frozenset(toks - _GENERIC_TOKENS) or frozenset(toks)


def canonicalize_claims(claims: list[dict], canon: dict) -> list[dict]:
    out = []
    for c in claims:
        key = (_norm_key(str(c.get("entity", ""))),
               _key_sig(str(c.get("attribute", ""))))
        first = canon.setdefault(key, c["attribute"])
        if first != c["attribute"]:
            c = {**c, "attribute": first}
        out.append(c)
    return out


def apply_key_map(claims: list[dict], qmap: dict) -> list[dict]:
    """Rewrite claim attributes per a controller-supplied per-question map
    (LLM-judged granularity merges, e.g. office-location -> location)."""
    out = []
    for c in claims:
        canon = qmap.get(_norm_key(str(c.get("entity", ""))), {}).get(
            _norm_key(str(c.get("attribute", ""))))
        if canon and canon != c["attribute"]:
            c = {**c, "attribute": canon}
        out.append(c)
    return out


def plan_questions(data: list[dict]) -> list[dict]:
    """Chrono-ordered per-question session plans; KU-forbidden sessions and
    cross-question duplicates removed (first question in sorted order wins,
    matching distill_datagen's global dedup)."""
    ku = [q for q in data if q["question_type"] == "knowledge-update"]
    forbidden = {sid for q in ku for sid in q["haystack_session_ids"]}
    sources = sorted((q for q in data
                      if q["question_type"] != "knowledge-update"),
                     key=lambda q: q["question_id"])
    claimed: set[str] = set()
    plans = []
    for q in sources:
        sessions = []
        ordered = sorted(zip(q["haystack_dates"], q["haystack_session_ids"],
                             q["haystack_sessions"]),
                         key=lambda tpl: _parse_date(tpl[0]))
        for date, sid, session in ordered:
            if sid in forbidden or sid in claimed:
                continue
            notes = _notes(session, date)
            if not notes:
                continue
            claimed.add(sid)
            sessions.append({"session_id": sid, "date": date, "notes": notes})
        if sessions:
            plans.append({"question_id": q["question_id"],
                          "sessions": sessions})
    return plans


def render_brief(qplan: dict, recall_prompt: str) -> str:
    qid = qplan["question_id"]
    parts = [
        f"# Extraction brief — question {qid}",
        "",
        "You are labeling chat sessions for extractor training. Apply the",
        "extraction prompt below to EACH session independently, in order.",
        "Maintain a growing slot-key list: after each session, add each",
        "claim's key as `entity.attribute` normalized (lowercase, every run",
        "of non-alphanumeric characters collapsed to a single hyphen). When",
        "a later session updates a fact you already keyed, REUSE that key.",
        "",
        "## Extraction prompt",
        "", recall_prompt, "",
        "## Output contract",
        "",
        f"Write EXACTLY one file: evals/data/sonnet_out/{qid}.jsonl — one",
        "line per session, in the order given, each line:",
        '{"session_id": "<id>", "claims": [{"entity":..,"attribute":..,'
        '"value":..,"confidence":0..1,"source":<note number>}]}',
        'Every session MUST appear, with "claims": [] when nothing',
        "qualifies. No prose, no markdown fences, JSONL only.",
        "",
        "## Sessions (chronological)",
    ]
    for s in qplan["sessions"]:
        parts += ["", f"### session_id: {s['session_id']}   date: {s['date']}",
                  ""]
        parts += [f"[{i + 1}] {n}" for i, n in enumerate(s["notes"])]
    return "\n".join(parts) + "\n"


def ingest_question(qplan: dict, answers: dict[str, list],
                    canonical: bool = False,
                    keymap: dict | None = None) -> list[dict] | None:
    """Rebuild production-shaped training rows from a subagent's answers.

    all-or-nothing: every session must be answered and every claim must pass
    validate_claims; the vocab hint is recomputed here from the accepted
    claims in chrono order — the subagent's own bookkeeping is never trusted.
    """
    rows = []
    vocab: set[str] = set()
    canon: dict = {}
    for s in qplan["sessions"]:
        claims_in = answers.get(s["session_id"])
        if claims_in is None:
            return None                                # unanswered session
        content = json.dumps({"claims": claims_in}, ensure_ascii=False)
        claims = validate_claims(content, len(s["notes"]))
        if claims is None:
            return None                                # schema violation
        if keymap:
            qmap = keymap.get(qplan["question_id"])
            if qmap:
                claims = apply_key_map(claims, qmap)
        if canonical:
            claims = canonicalize_claims(claims, canon)
        vocab_list = sorted(vocab)[:VOCAB_MAX]
        target = json.dumps({"claims": claims}, ensure_ascii=False)
        rows.append({
            "id": f"{qplan['question_id']}:{s['session_id']}",
            "messages": [
                {"role": "system",
                 "content": _SYSTEM_PROMPT + _vocab_hint(vocab_list)},
                {"role": "user", "content": "\n\n".join(
                    f"[{i + 1}] {n}" for i, n in enumerate(s["notes"]))},
                {"role": "assistant", "content": target},
            ]})
        for c in claims:
            vocab.add(f"{_norm_key(c['entity'])}.{_norm_key(c['attribute'])}")
    return rows


def _cmd_emit(args) -> int:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    plans = plan_questions(data)
    if args.questions:
        plans = plans[:args.questions]
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    recall = RECALL_PROMPT.read_text(encoding="utf-8")
    done = {p.stem for p in OUT_DIR.glob("*.jsonl")}
    n = 0
    for p in plans:
        if p["question_id"] in done:
            continue
        (BRIEFS_DIR / f"{p['question_id']}.md").write_text(
            render_brief(p, recall), encoding="utf-8")
        n += 1
    print(f"{n} briefs -> {BRIEFS_DIR} ({len(done)} questions already "
          f"answered in {OUT_DIR})")
    return 0


def _cmd_ingest(args) -> int:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    plans = {p["question_id"]: p for p in plan_questions(data)}
    out_path = args.out or MERGED
    keymap = (json.loads(args.key_map.read_text(encoding="utf-8"))
              if args.key_map else None)
    done_ids = set()
    kept = empty_kept = 0
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            done_ids.add(row["id"].split(":")[0])
            kept += 1
            if not json.loads(row["messages"][-1]["content"])["claims"]:
                empty_kept += 1
    rejected = []
    with out_path.open("a", encoding="utf-8") as out:
        for f in sorted(OUT_DIR.glob("*.jsonl")):
            qid = f.stem
            if qid in done_ids or qid not in plans:
                continue
            try:
                answers = {}
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        d = json.loads(line)
                        answers[d["session_id"]] = d["claims"]
            except (ValueError, KeyError):
                rejected.append(qid)
                continue
            rows = ingest_question(plans[qid], answers,
                                   canonical=args.canonical_keys,
                                   keymap=keymap)
            if rows is None:
                rejected.append(qid)
                continue
            for r in rows:
                is_empty = not json.loads(
                    r["messages"][-1]["content"])["claims"]
                if is_empty and empty_kept >= args.max_empty_share * max(kept, 20):
                    continue
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
                kept += 1
                empty_kept += is_empty
    print(f"kept {kept} rows ({empty_kept} empty); rejected questions "
          f"(delete their sonnet_out file and re-dispatch): {rejected}")
    return 0


def _cmd_compare(args) -> int:
    qwen = {}
    for line in QWEN_SET.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        qwen[row["id"]] = json.loads(row["messages"][-1]["content"])["claims"]
    per_id, keys = {}, defaultdict(lambda: [set(), set()])
    for line in MERGED.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rid = row["id"]
        if rid not in qwen:
            continue
        claims = json.loads(row["messages"][-1]["content"])["claims"]
        per_id[rid] = (len(claims), len(qwen[rid]))
        for c in claims:
            keys[rid][0].add(f"{_norm_key(c['entity'])}."
                             f"{_norm_key(c['attribute'])}")
        for c in qwen[rid]:
            keys[rid][1].add(f"{_norm_key(c['entity'])}."
                             f"{_norm_key(c['attribute'])}")
    if not per_id:
        print("no shared sessions between the two sets yet")
        return 1
    s_mean = sum(a for a, _ in per_id.values()) / len(per_id)
    q_mean = sum(b for _, b in per_id.values()) / len(per_id)
    jac = [len(a & b) / len(a | b) for a, b in keys.values() if a | b]
    print(f"shared sessions: {len(per_id)}")
    print(f"claims/session — sonnet {s_mean:.2f} vs qwen {q_mean:.2f} "
          f"(ratio {s_mean / max(q_mean, 1e-9):.2f})")
    print(f"sonnet>qwen on {sum(1 for a, b in per_id.values() if a > b)} "
          f"sessions; slot-key jaccard mean "
          f"{sum(jac) / max(len(jac), 1):.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-briefs", action="store_true")
    mode.add_argument("--ingest", action="store_true")
    mode.add_argument("--compare", action="store_true")
    ap.add_argument("--questions", type=int, default=0,
                    help="emit only the first N question briefs (pilot)")
    ap.add_argument("--max-empty-share", type=float, default=0.2)
    ap.add_argument("--canonical-keys", action="store_true",
                    help="merge near-miss slot keys to the first-seen key "
                         "per question chain (arm B')")
    ap.add_argument("--out", type=Path, default=None,
                    help="ingest output file (default: distill-extract-sonnet.jsonl)")
    ap.add_argument("--key-map", type=Path, default=None,
                    help="JSON file of controller-supplied per-question "
                         "attribute rewrites (LLM-judged granularity merges)")
    args = ap.parse_args()
    if args.emit_briefs:
        return _cmd_emit(args)
    if args.ingest:
        return _cmd_ingest(args)
    return _cmd_compare(args)


if __name__ == "__main__":
    raise SystemExit(main())
