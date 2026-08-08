"""Events-capacity spot check against the committed Opus ceiling probe.

Gate T3 of the evlora preregistration
(docs/superpowers/specs/2026-08-07-evlora-antisuppression-design.md): a
candidate extractor is worth the 500-question run only if it captures at
least HALF the gold instances the Opus-5 teacher captured on the same
sessions. The session list and per-session instance sentences come from
the committed probe artifact (evals/results/evq-opus-probe-0807.json),
so the spot is exactly reproducible: same sessions, same v2 events
prompt, same digit-coverage measure.

    PYTHONPATH=. python evals/evlora_capacity_spot.py \
        --extractor-url http://127.0.0.1:8081/v1 --tag e4b-v3

Writes evals/results/evlora-capacity-spot-<tag>.json; exit 0 only when
the >= 1/2 bar is met.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pseudolife_memory.memory.dream import events_from_parsed  # noqa: E402

EVALS = Path(__file__).resolve().parent
PROBE = EVALS / "results" / "evq-opus-probe-0807.json"
DATASET = EVALS / "data" / "longmemeval_s_cleaned.json"
EVENTS_PROMPT = (EVALS / "prompts" / "events_pass_v2.txt").read_text(
    encoding="utf-8")


def extract(url: str, notes: list[str], timeout: int) -> list[dict]:
    body = json.dumps({
        "model": "extractor",
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = json.loads(resp.read().decode())[
            "choices"][0]["message"]["content"] or ""
    s, e = content.find("{"), content.rfind("}")
    if s == -1 or e <= s:
        return []
    try:
        return events_from_parsed(json.loads(content[s:e + 1]), len(notes))
    except (ValueError, KeyError, TypeError):
        return []


def digit_covered(sentence: str, events: list[dict]) -> bool:
    ev_text = " | ".join(ev["description"] for ev in events)
    for tok in re.findall(r"\S+", sentence):
        if any(c.isdigit() for c in tok):
            bare = tok.strip(".,;:!?()$")
            if bare and bare in ev_text:
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extractor-url", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--timeout", type=int, default=1800,
                    help="per-call timeout; CPU sidecars are slow")
    args = ap.parse_args()

    probe = json.loads(PROBE.read_text(encoding="utf-8"))
    data = {q["question_id"]: q for q in json.loads(
        DATASET.read_text(encoding="utf-8"))}

    rows = []
    student_total = opus_total = 0
    for prow in probe:
        q = data[prow["question_id"]]
        sess_by_id = dict(zip(q["haystack_session_ids"],
                              zip(q["haystack_dates"],
                                  q["haystack_sessions"])))
        for s in prow["sessions_probed"]:
            if "error" in s:
                continue
            date, sess = sess_by_id[s["session_id"]]
            notes = [f"[{date}] {t['role']}: {t['content'].strip()}"
                     for t in sess if (t.get("content") or "").strip()]
            try:
                evs = extract(args.extractor_url, notes, args.timeout)
            except Exception as exc:  # noqa: BLE001
                print(f"  extract failed {prow['question_id']}"
                      f"/{s['session_id'][:12]}: {exc}", flush=True)
                evs = []
            covered = sum(digit_covered(sent, evs)
                          for sent in s["instance_sentences"])
            student_total += covered
            opus_total += s["instances_with_digit_in_event"]
            rows.append({
                "question_id": prow["question_id"],
                "session_id": s["session_id"],
                "student_events_n": len(evs),
                "student_covered": covered,
                "opus_covered": s["instances_with_digit_in_event"],
                "instances_total": s["instances_total"],
                "student_events": [
                    {"description": ev["description"][:140],
                     "date": ev.get("date")} for ev in evs],
            })
            print(f"  {prow['question_id']}/{s['session_id'][:12]}: student "
                  f"{covered} vs opus {s['instances_with_digit_in_event']} "
                  f"({len(evs)} events)", flush=True)

    passed = (opus_total == 0) or (student_total * 2 >= opus_total)
    out = EVALS / "results" / f"evlora-capacity-spot-{args.tag}.json"
    out.write_text(json.dumps({
        "tag": args.tag,
        "prereg": ("gate T3, docs/superpowers/specs/"
                   "2026-08-07-evlora-antisuppression-design.md — student "
                   "must capture >= half the Opus-covered instances"),
        "probe_artifact": "evals/results/evq-opus-probe-0807.json",
        "student_covered_total": student_total,
        "opus_covered_total": opus_total,
        "pass": passed,
        "rows": rows,
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"capacity spot: student {student_total} vs opus {opus_total} "
          f"-> {'PASS' if passed else 'FAIL'} -> {out}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
