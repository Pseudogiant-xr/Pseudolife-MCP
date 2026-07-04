"""Teacher-labeling pipeline for the bespoke extraction model (Stage 1 SFT).

Generates chat-format training rows for a small extraction student by
running the local teacher (Qwen3.6-27B) over LongMemEval haystack sessions
with the EXACT production extraction prompt — `_SYSTEM_PROMPT` and
`_vocab_hint` are imported from `pseudolife_memory/memory/dream.py`, so the
training distribution cannot drift from what ships.

Contamination guard: only sessions from questions OUTSIDE the
knowledge-update subset are used, and any session id appearing in ANY
knowledge-update question's haystack is excluded — the LongMemEval-KU eval
stays fully held out.

Vocab evolution: each source question's sessions are processed in
chronological order against one simulated bank, with the vocab hint grown
from the teacher's own prior claims — teaching the slot-key-reuse behaviour
that makes supersession fire (the specific capability the floor model
lacks). Sessions shared across haystacks are labeled once (global dedup).

    PYTHONPATH=. python evals/distill_datagen.py --limit-rows 2000

Output (gitignored): evals/data/distill-extract.jsonl — one
{"id", "messages": [system, user, assistant]} row per session, resumable.

Design doc: docs/specs/2026-07-04-bespoke-extractor-design.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pseudolife_memory.memory.cortex import _norm_key            # noqa: E402
from pseudolife_memory.memory.dream import (                     # noqa: E402
    _SYSTEM_PROMPT, _vocab_hint,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATASET = DATA_DIR / "longmemeval_s_cleaned.json"
DEFAULT_OUT = DATA_DIR / "distill-extract.jsonl"
TEACHER_URL = os.environ.get("PSEUDOLIFE_BENCH_QWEN_URL",
                             "http://127.0.0.1:1234/v1")
VOCAB_MAX = 120        # mirrors service.cortex_vocab(limit=120)


def _parse_date(raw: str) -> datetime:
    cleaned = re.sub(r"\s*\(\w+\)\s*", " ", raw or "").strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return datetime.min


def teacher_extract(url: str, notes: list[str], vocab: list[str],
                    timeout: float = 600.0) -> str:
    """One production-shaped extraction call; returns raw message content."""
    body = json.dumps({
        "model": "teacher",
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT + _vocab_hint(vocab)},
            {"role": "user", "content": "\n\n".join(
                f"[{i + 1}] {t}" for i, t in enumerate(notes))},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(f"{url}/chat/completions", data=body,
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return (data["choices"][0]["message"]["content"] or "").strip()


def validate_claims(content: str, n_notes: int) -> list[dict] | None:
    """Quality-gate the teacher output; return cleaned claims or None."""
    s, e = content.find("{"), content.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        parsed = json.loads(content[s:e + 1])
    except ValueError:
        return None
    raw = parsed.get("claims") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return None
    cleaned = []
    for c in raw:
        if not isinstance(c, dict):
            return None
        entity = str(c.get("entity", "")).strip()
        attribute = str(c.get("attribute", "")).strip()
        value = str(c.get("value", "")).strip()
        if not (entity and attribute and value):
            return None
        try:
            conf = float(c.get("confidence", 0.7))
        except (TypeError, ValueError):
            return None
        if not 0.0 <= conf <= 1.0:
            return None
        out = {"entity": entity, "attribute": attribute, "value": value,
               "confidence": round(conf, 2)}
        if "source" in c:
            try:
                src = int(c["source"])
            except (TypeError, ValueError):
                return None
            if not 1 <= src <= n_notes:
                return None                       # hallucinated citation
            out["source"] = src
        cleaned.append(out)
    return cleaned


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--teacher-url", default=TEACHER_URL)
    ap.add_argument("--limit-rows", type=int, default=2000)
    ap.add_argument("--max-empty-share", type=float, default=0.2,
                    help="cap on the share of {\"claims\":[]} rows kept")
    args = ap.parse_args()

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    ku = [q for q in data if q["question_type"] == "knowledge-update"]
    forbidden = {sid for q in ku for sid in q["haystack_session_ids"]}
    sources = sorted((q for q in data if q["question_type"] !=
                      "knowledge-update"), key=lambda q: q["question_id"])

    done_rows: dict[str, list[dict]] = {}
    kept = empty_kept = 0
    if args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                claims = json.loads(row["messages"][-1]["content"])["claims"]
            except (ValueError, KeyError, IndexError):
                continue
            done_rows[row["id"]] = claims
            kept += 1
            if not claims:
                empty_kept += 1
    print(f"{len(sources)} source questions; {len(forbidden)} KU sessions "
          f"excluded; resuming with {len(done_rows)} rows done", flush=True)

    labeled_sessions: set[str] = set()
    dropped = failures = 0
    with args.out.open("a", encoding="utf-8") as out:
        for q in sources:
            if kept >= args.limit_rows:
                break
            vocab: set[str] = set()
            sessions = sorted(
                zip(q["haystack_dates"], q["haystack_session_ids"],
                    q["haystack_sessions"]),
                key=lambda tpl: _parse_date(tpl[0]))
            for date, sid, session in sessions:
                if kept >= args.limit_rows:
                    break
                if sid in forbidden or sid in labeled_sessions:
                    continue
                labeled_sessions.add(sid)
                row_id = f"{q['question_id']}:{sid}"
                notes = [f"[{date}] {t['role']}: {t['content'].strip()}"
                         for t in session if (t.get("content") or "").strip()]
                if not notes:
                    continue
                vocab_list = sorted(vocab)[:VOCAB_MAX]
                if row_id in done_rows:
                    # Resumed row: replay its claims into the vocab stream so
                    # later sessions see the same hint the original run did.
                    for c in done_rows[row_id]:
                        vocab.add(f"{_norm_key(c['entity'])}."
                                  f"{_norm_key(c['attribute'])}")
                    continue
                try:
                    content = teacher_extract(args.teacher_url, notes,
                                              vocab_list)
                except Exception as exc:  # noqa: BLE001 — skip and continue
                    failures += 1
                    print(f"  teacher call failed for {row_id}: {exc}",
                          flush=True)
                    if failures >= 5:
                        print("too many teacher failures — aborting (resume "
                              "later)", flush=True)
                        return 1
                    continue
                claims = validate_claims(content, len(notes))
                if claims is None:
                    dropped += 1
                    continue
                if not claims:
                    if empty_kept >= args.max_empty_share * max(kept, 20):
                        continue                  # enough empty examples
                    empty_kept += 1
                for c in claims:
                    vocab.add(f"{_norm_key(c['entity'])}."
                              f"{_norm_key(c['attribute'])}")
                target = json.dumps({"claims": claims}, ensure_ascii=False)
                row = {"id": row_id, "messages": [
                    {"role": "system",
                     "content": _SYSTEM_PROMPT + _vocab_hint(vocab_list)},
                    {"role": "user", "content": "\n\n".join(
                        f"[{i + 1}] {t}" for i, t in enumerate(notes))},
                    {"role": "assistant", "content": target},
                ]}
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                kept += 1
                if kept % 25 == 0:
                    print(f"[{kept}/{args.limit_rows}] rows "
                          f"({empty_kept} empty, {dropped} dropped, "
                          f"{failures} call failures)", flush=True)
    print(f"done: {kept} rows ({empty_kept} empty, {dropped} dropped, "
          f"{failures} call failures) -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
