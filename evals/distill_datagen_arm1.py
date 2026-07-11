"""Arm-1 registry-datagen: Sonnet-5 + v1-prompt teacher via the live shim.

Direct extension of `distill_datagen.py` (the original Qwen HTTP pipeline),
pointed at `evals/sonnet_shim.py` instead of a local Qwen server, with one
addition: a per-chain **key registry** shown to the teacher only, forcing
reuse of established `entity.attribute` keys and re-statement of
carried-forward facts instead of the model rewording the same fact into a
fresh key each time (Stage 1.5 arm B' measured 6.8% raw key reuse for
Sonnet vs Qwen's 24.1% — this attacks that at generation time instead of
patching it post-hoc).

Asymmetric prompt split (same principle as Stage 1.5 Arm B): the teacher
call gets `_SYSTEM_PROMPT + _vocab_hint(...) + _registry_hint(...)`; the
shim (evals/sonnet_shim.py) swaps the `_SYSTEM_PROMPT` prefix for the v1
prompt and forwards the rest untouched. The STORED training row keeps
`_SYSTEM_PROMPT + _vocab_hint(...)` only — no registry block — so the
student learns key-reuse as a trained-in prior and never sees a registry
at inference (the known-facts-window intervention that failed at inference
time, 2026-07-11, is not repeated here).

    PYTHONPATH=. python evals/distill_datagen_arm1.py --questions 10

Output (gitignored): evals/data/distill-extract-arm1.jsonl — one
{"id", "messages": [system, user, assistant]} row per session, resumable.

Design doc: docs/superpowers/specs/2026-07-12-arm1-registry-datagen-design.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pseudolife_memory.memory.cortex import _norm_key             # noqa: E402
from pseudolife_memory.memory.dream import _SYSTEM_PROMPT, _vocab_hint  # noqa: E402
from distill_datagen import (                                     # noqa: E402
    VOCAB_MAX, _parse_date, teacher_extract, validate_claims,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATASET = DATA_DIR / "longmemeval_s_cleaned.json"
DEFAULT_OUT = DATA_DIR / "distill-extract-arm1.jsonl"
TEACHER_URL = os.environ.get("PSEUDOLIFE_ARM1_SHIM_URL", "http://127.0.0.1:8082/v1")


def _registry_hint(registry: dict[tuple[str, str], str]) -> str:
    if not registry:
        return ""
    items = list(registry.items())[:VOCAB_MAX]
    lines = "\n".join(f"- {e} | {a}: {v}" for (e, a), v in items)
    return (
        "\n\nCHAIN REGISTRY (facts already established earlier in this "
        "conversation history — if this session's notes still evidence one "
        "of these, reuse the EXACT SAME entity/attribute key; restate the "
        "same value if unchanged, or the new value if this session updates "
        "it. Do not invent a new key for a fact you already have a key for. "
        "Never emit a claim this session's notes don't evidence.):\n" + lines
    )


def _teacher_system(vocab_list: list[str],
                    registry: dict[tuple[str, str], str]) -> str:
    return _SYSTEM_PROMPT + _vocab_hint(vocab_list) + _registry_hint(registry)


def _stored_system(vocab_list: list[str]) -> str:
    return _SYSTEM_PROMPT + _vocab_hint(vocab_list)


def _update_registry(registry: dict[tuple[str, str], str],
                     claims: list[dict]) -> None:
    for c in claims:
        key = (_norm_key(str(c["entity"])), _norm_key(str(c["attribute"])))
        registry[key] = c["value"]


def teacher_extract_arm1(url: str, notes: list[str], vocab: list[str],
                         registry: dict[tuple[str, str], str],
                         timeout: float = 600.0) -> str:
    """Same HTTP contract as distill_datagen.teacher_extract, with the
    registry hint appended to the teacher-side system message only."""
    import urllib.request
    body = json.dumps({
        "model": "teacher",
        "messages": [
            {"role": "system", "content": _teacher_system(vocab, registry)},
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--teacher-url", default=TEACHER_URL)
    ap.add_argument("--questions", type=int, default=0,
                    help="cap the number of source questions processed "
                         "(pilot control; 0 = no cap)")
    ap.add_argument("--limit-rows", type=int, default=2000)
    ap.add_argument("--max-empty-share", type=float, default=0.2,
                    help="cap on the share of {\"claims\":[]} rows kept")
    args = ap.parse_args()

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    ku = [q for q in data if q["question_type"] == "knowledge-update"]
    forbidden = {sid for q in ku for sid in q["haystack_session_ids"]}
    sources = sorted((q for q in data if q["question_type"] !=
                      "knowledge-update"), key=lambda q: q["question_id"])
    if args.questions:
        sources = sources[:args.questions]

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
            registry: dict[tuple[str, str], str] = {}
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
                    # Resumed row: replay its claims into vocab AND registry
                    # so later sessions see the same state the original run did.
                    _update_registry(registry, done_rows[row_id])
                    for c in done_rows[row_id]:
                        vocab.add(f"{_norm_key(c['entity'])}."
                                  f"{_norm_key(c['attribute'])}")
                    continue
                try:
                    content = teacher_extract_arm1(args.teacher_url, notes,
                                                   vocab_list, registry)
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
                _update_registry(registry, claims)
                for c in claims:
                    vocab.add(f"{_norm_key(c['entity'])}."
                              f"{_norm_key(c['attribute'])}")
                target = json.dumps({"claims": claims}, ensure_ascii=False)
                row = {"id": row_id, "messages": [
                    {"role": "system", "content": _stored_system(vocab_list)},
                    {"role": "user", "content": "\n\n".join(
                        f"[{i + 1}] {t}" for i, t in enumerate(notes))},
                    {"role": "assistant", "content": target},
                ]}
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                kept += 1
                if kept % 10 == 0:
                    print(f"[{kept}/{args.limit_rows}] rows "
                          f"({empty_kept} empty, {dropped} dropped, "
                          f"{failures} call failures)", flush=True)
    print(f"done: {kept} rows ({empty_kept} empty, {dropped} dropped, "
          f"{failures} call failures) -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
