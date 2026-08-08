"""Events-pass distillation datagen: Opus-5 teacher labels the LME _s
corpus with the events_pass_v2 candidate prompt, producing student
training rows for the sidecar's separate events pass.

Motivation (evals/results/events-coverage-audit-0806.json): the shipped
e4b-v2 sidecar LoRA is claims-only — if chronicle goes default-on, its
events pass rides on raw Gemma-4B behaviour, and the audit showed the
value lives in quantities kept verbatim, exactly what an untrained 4B
drops. Same corpus, note format, KU-exclusion, and resumable loop as
``distill_datagen.py``; the teacher call sends the v2 events prompt
(the shim only swaps claims-prompt prefixes, so it passes through
verbatim), and stored rows carry that same prompt as the student system
message.

    PYTHONPATH=. python evals/distill_datagen_events.py --limit-rows 900

Output (gitignored): evals/data/distill-events-opus1.jsonl — one
{"id", "messages": [system, user, assistant]} row per session, resumable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pseudolife_memory.memory.dream import events_from_parsed     # noqa: E402
from distill_datagen import _parse_date                           # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
DATASET = DATA_DIR / "longmemeval_s_cleaned.json"
DEFAULT_OUT = DATA_DIR / "distill-events-opus1.jsonl"
EVENTS_PROMPT = (Path(__file__).resolve().parent / "prompts"
                 / "events_pass_v2.txt").read_text(encoding="utf-8")
TEACHER_URL = os.environ.get("PSEUDOLIFE_EVENTS_SHIM_URL",
                             "http://127.0.0.1:8085/v1")


def teacher_events(url: str, notes: list[str]) -> str:
    body = json.dumps({
        "model": "teacher",
        "messages": [
            {"role": "system", "content": EVENTS_PROMPT},
            {"role": "user", "content": "\n\n".join(
                f"[{i + 1}] {t}" for i, t in enumerate(notes))},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/chat/completions", data=body,
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"] or ""


def validate_events(content: str, n_notes: int) -> list[dict] | None:
    """Parse + shape-gate the teacher output. Returns the kept events in
    PROMPT format (1-based source, date_phrase key) — what the student
    must learn to emit — or None on unparseable output."""
    s, e = content.find("{"), content.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        parsed = json.loads(content[s:e + 1])
    except ValueError:
        return None
    kept_norm = events_from_parsed(parsed, n_notes)
    kept_desc = {ev["description"] for ev in kept_norm}
    out = []
    for raw in (parsed.get("events") or []):
        if not isinstance(raw, dict):
            continue
        if (raw.get("description") or "").strip() not in kept_desc:
            continue
        ev = {"description": raw["description"].strip(),
              "actor": (raw.get("actor") or "user").strip(),
              "date": raw.get("date")
              if isinstance(raw.get("date"), str) else None,
              "date_phrase": (raw.get("date_phrase") or "").strip()}
        try:
            src = int(raw.get("source"))
            if 1 <= src <= n_notes:
                ev["source"] = src
        except (TypeError, ValueError):
            pass
        out.append(ev)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--teacher-url", default=TEACHER_URL)
    ap.add_argument("--limit-rows", type=int, default=900)
    ap.add_argument("--max-empty-share", type=float, default=0.25,
                    help="cap on the share of {\"events\":[]} rows kept")
    args = ap.parse_args()

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    ku = [q for q in data if q["question_type"] == "knowledge-update"]
    forbidden = {sid for q in ku for sid in q["haystack_session_ids"]}
    sources = sorted((q for q in data if q["question_type"] !=
                      "knowledge-update"), key=lambda q: q["question_id"])

    done: set[str] = set()
    kept = empty_kept = 0
    if args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                evs = json.loads(row["messages"][-1]["content"])["events"]
            except (ValueError, KeyError, IndexError):
                continue
            done.add(row["id"])
            kept += 1
            if not evs:
                empty_kept += 1
    print(f"{len(sources)} source questions; {len(forbidden)} KU sessions "
          f"excluded; resuming with {len(done)} rows done", flush=True)

    labeled_sessions: set[str] = set()
    dropped = failures = 0
    digit_notes = digit_retained = 0
    with args.out.open("a", encoding="utf-8") as out:
        for q in sources:
            if kept >= args.limit_rows:
                break
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
                if row_id in done:
                    continue
                notes = [f"[{date}] {t['role']}: {t['content'].strip()}"
                         for t in session if (t.get("content") or "").strip()]
                if not notes:
                    continue
                try:
                    content = teacher_events(args.teacher_url, notes)
                except Exception as exc:  # noqa: BLE001 — skip and continue
                    failures += 1
                    print(f"  teacher call failed for {row_id}: {exc}",
                          flush=True)
                    if failures >= 5:
                        print("too many teacher failures — aborting (resume "
                              "later)", flush=True)
                        return 1
                    continue
                events = validate_events(content, len(notes))
                if events is None:
                    dropped += 1
                    continue
                if not events:
                    if empty_kept >= args.max_empty_share * max(kept, 20):
                        continue
                    empty_kept += 1
                # Quantity-retention telemetry (informational, not a gate):
                # user notes carrying digits vs digits surviving into events.
                target = json.dumps({"events": events}, ensure_ascii=False)
                for n in notes:
                    body_txt = n.split(":", 1)[-1]
                    if any(ch.isdigit() for ch in body_txt):
                        digit_notes += 1
                        if any(tok in target for tok in body_txt.split()
                               if any(ch.isdigit() for ch in tok)):
                            digit_retained += 1
                row = {"id": row_id, "messages": [
                    {"role": "system", "content": EVENTS_PROMPT},
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
                          f"{failures} failures, digit-retention "
                          f"{digit_retained}/{max(digit_notes, 1)})",
                          flush=True)
    print(f"done: {kept} rows ({empty_kept} empty, {dropped} dropped, "
          f"{failures} failures, digit-retention "
          f"{digit_retained}/{max(digit_notes, 1)}) -> {args.out}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
