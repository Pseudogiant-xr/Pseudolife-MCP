"""LongMemEval against a disposable Rust daemon, with separate extract and answer phases."""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import fmean
import sys
from time import perf_counter

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import context_format, leak_check, lme_scoring, replicate
from evals.retained_artifacts import fingerprint, read_json, record, rows_in
from evals.retained_daemon import (
    BenchError, DREAM_TALLIES, add_daemon_arguments, add_endpoint_arguments, bench_config,
    daemon_setup, drain_dream, endpoint_args,
)


ALL_TYPES = ("knowledge-update", "multi-session", "temporal-reasoning",
             "single-session-user", "single-session-assistant", "single-session-preference")
ARMS = ("rag", "cortex", "hybrid", "rag_control")
ENTRY_WIDTH = 6
CONTEXT_POLICY = "rust-search-5-facts-with-history-v1"
SCORING_POLICY = "longmemeval-temperature-zero-v1"


def chronological_turns(question):
    def dated(pair):
        date, _ = pair
        cleaned = re.sub(r"\s*\(\w+\)\s*", " ", date).strip()
        return datetime.fromisoformat(cleaned.replace("/", "-"))

    try:
        sessions = zip(question["haystack_dates"], question["haystack_sessions"], strict=True)
        return [[f"[{date}] {turn['role']}: {content}" for turn in session
                 if (content := (turn.get("content") or "").strip())]
                for date, session in sorted(sessions, key=dated)]
    except (AttributeError, TypeError, ValueError, KeyError):
        raise BenchError("question has invalid or unpaired dated sessions") from None


def raw_texts(result):
    return [entry["text"] for entry in result["entries"]]


def fact_lines(bank, result):
    lines = []
    for entry in result.get("cortex", []):
        entity, attribute, current = (entry[key] for key in ("entity", "attribute", "value"))
        history = bank.tool("memory_history", entity=entity, attribute=attribute)
        if not isinstance(history.get("versions"), list):
            raise BenchError("Rust history returned no versions list")
        earlier = list(dict.fromkeys(version["value"] for version in history["versions"]
                                    if version["value"] != current))
        suffix = f" (previous values: {'; '.join(earlier)})" if earlier else ""
        lines.append(f"{entity} / {attribute}: {current}{suffix}")
    return lines


def extract_question(question, open_bank, *, frozen_control=None, diagnose=False):
    sessions = chronological_turns(question)
    query = question["question"]
    tally = dict.fromkeys(DREAM_TALLIES, 0)
    turns = 0
    elapsed = 0.0
    diagnostics = None
    with open_bank(True) as bank:
        for session in sessions:
            for turn in session:
                bank.store(turn)
            turns += len(session)
            started = perf_counter()
            for key, value in drain_dream(bank.tool, stored=len(session)).items():
                tally[key] += value
            elapsed += perf_counter() - started
        hits = bank.search(query, ENTRY_WIDTH)
        raw = raw_texts(hits)
        facts = fact_lines(bank, hits)
        if diagnose:
            snapshot = bank.get("/api/facts", limit=500)
            entries = snapshot.get("entries")
            if not isinstance(entries, list) or type(snapshot.get("total")) is not int:
                raise BenchError("Rust facts response is missing its entries or total")
            diagnostics = {"current_facts_partial": len(entries) != snapshot["total"],
                           "current_facts_total": snapshot["total"],
                           "gold_in_current_fact": any(leak_check.answer_present(
                               entry["value"], question["answer"]) for entry in entries)}

    if frozen_control is None:
        with open_bank(False) as control:
            for session in sessions:
                for turn in session:
                    control.store(turn)
            frozen_control = "\n\n".join(raw_texts(control.search(query, ENTRY_WIDTH)))
    row = {key: question[key] for key in ("question_id", "question_type", "question_date",
                                          "question", "answer")}
    row["abstention"] = row["question_id"].endswith("_abs")
    row["consolidation"] = {"turns": turns, "extract_seconds": round(elapsed, 3), **tally}
    row["contexts"] = {"rag": "\n\n".join(raw), "cortex": "\n".join(facts),
                       "hybrid": context_format.hybrid_context(facts, raw),
                       "rag_control": frozen_control}
    row["control_sha256"] = fingerprint(frozen_control)
    row["gold_in_question"] = leak_check.answer_present(row["question"], row["answer"])
    if diagnostics is not None:
        row["diagnostics"] = diagnostics
    return row


def load_questions(path, types, limit):
    data = path.read_bytes()
    questions = [row for row in json.loads(data) if row["question_type"] in types]
    if limit is not None:
        questions = questions[:limit]
    ids = [row["question_id"] for row in questions]
    if not ids or any(not isinstance(qid, str) or not qid for qid in ids) or len(ids) != len(set(ids)):
        raise BenchError("selected question IDs must be nonempty and unique")
    return sha256(data).hexdigest(), questions


def validate_context(row, question_id):
    contexts = row.get("contexts")
    if (row.get("question_id") != question_id or not isinstance(contexts, dict)
            or set(contexts) != set(ARMS) or any(not isinstance(text, str) for text in contexts.values())
            or row.get("control_sha256") != fingerprint(contexts["rag_control"])):
        raise BenchError("the context row is incomplete or does not match the manifest")


def run_extract(root, manifest, questions, open_bank, controls=None):
    record(root / "context-manifest.json", manifest)
    for index, question in enumerate(questions):
        target = root / "contexts" / f"{index:04d}.json"
        qid = question["question_id"]
        frozen = None if controls is None else controls[qid]
        if target.exists():
            row = read_json(target)
            validate_context(row, qid)
            if frozen is not None and row["contexts"]["rag_control"] != frozen:
                raise BenchError("existing context does not agree with the pinned raw control")
            continue
        row = extract_question(question, open_bank, frozen_control=frozen,
                               diagnose=manifest.get("diagnose", False))
        record(target, row)
        print(f"Rust contexts {index + 1}/{len(questions)}", flush=True)


def pinned_controls(source, manifest):
    previous = read_json(source / "context-manifest.json")
    keys = ("dataset_sha256", "question_ids", "control_policy", "daemon")
    if any(previous.get(key) != manifest[key] for key in keys):
        raise BenchError("raw control was made with different daemon, corpus, question IDs or policy")
    rows = rows_in(source / "contexts", previous)
    for row in rows:
        validate_context(row, row["question_id"])
    return {row["question_id"]: row["contexts"]["rag_control"] for row in rows}


def scored_rows(root, manifest):
    rows = rows_in(root, manifest)
    for row in rows:
        validate_context(row, row["question_id"])
        for arm in ARMS:
            if (type(row.get(f"{arm}_correct")) is not bool
                    or not isinstance(row.get(f"{arm}_response"), str)
                    or type(row.get(f"{arm}_context_tokens")) is not int):
                raise BenchError("missing or invalid answer or verdict in the scored rows")
    return rows


def run_answer(root, answerer, judge):
    manifest = read_json(root / "context-manifest.json")
    contexts = rows_in(root / "contexts", manifest)
    if manifest.get("daemon", {}).get("engine") != "rust":
        raise BenchError("answer phase requires a Rust context manifest")
    for row in contexts:
        validate_context(row, row["question_id"])
    record(root / "answer-manifest.json", {"scoring_policy": SCORING_POLICY,
                                          "context_set_sha256": fingerprint(contexts),
                                          "context_manifest_sha256": fingerprint(manifest),
                                          "answerer": answerer.provenance(), "judge": judge.provenance()})
    checked_endpoints = False
    for index, row in enumerate(contexts):
        target = root / "answers" / f"{index:04d}.json"
        if target.exists():
            continue
        if not checked_endpoints:
            answerer.verify()
            judge.verify()
            checked_endpoints = True
        record(target, lme_scoring.score_row(row, answerer.chat, judge.chat))
        print(f"Rust answers {index + 1}/{len(contexts)}", flush=True)
    return report(root)


def report(root, *, answers=None, instrument_path=None, output=None):
    manifest = read_json(root / "context-manifest.json")
    contexts = rows_in(root / "contexts", manifest)
    scoring = read_json(instrument_path or root / "answer-manifest.json")
    rows = scored_rows(answers or root / "answers", manifest)
    if (manifest.get("daemon", {}).get("engine") != "rust"
            or scoring.get("context_manifest_sha256") != fingerprint(manifest)
            or scoring.get("context_set_sha256") != fingerprint(contexts)
            or [replicate.strip_judged([row])[0] for row in rows] != contexts):
        raise BenchError("answered rows and scoring manifest do not match the Rust context set")

    def scores(subset):
        arms = {arm: {"accuracy": fmean(row[f"{arm}_correct"] for row in subset),
                      "context_tokens": fmean(row[f"{arm}_context_tokens"] for row in subset)}
                for arm in ARMS}
        arms["cascade"] = {"accuracy": fmean(replicate.cascade_correct(row) for row in subset),
                           "context_tokens": fmean(replicate.cascade_context_tokens(row) for row in subset)}
        return arms

    from evals import answerability_probe

    summary = {"n": len(rows), "context_manifest": manifest, "scoring": scoring,
               "arms": scores(rows), "by_question_type": {},
               "leaks": leak_check.check_rows(rows), "answerability": answerability_probe.probe_rows(rows)}
    for kind in ALL_TYPES:
        subset = [row for row in rows if row["question_type"] == kind]
        if subset:
            summary["by_question_type"][kind] = {"n": len(subset), "arms": scores(subset)}
    record(output or root / "summary.json", summary)
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("full", "extract", "answer", "report"), default="full")
    parser.add_argument("--dataset", choices=("oracle", "s"), default="oracle")
    parser.add_argument("--data", type=Path, help="explicit LongMemEval JSON file for the selected dataset")
    parser.add_argument("--types", default="all", help="all or a comma-separated list of question types")
    parser.add_argument("--limit", type=int, help="limit the selected slice in source order")
    parser.add_argument("--out", required=True, type=Path, help="a dedicated run directory")
    parser.add_argument("--control-from", type=Path, help="previous run with a pinned Rust raw control")
    parser.add_argument("--contiguity", type=int, default=0)
    parser.add_argument("--timeline", action="store_true")
    parser.add_argument("--known-facts-window", type=int, default=0)
    parser.add_argument("--diagnose", action="store_true", help="record bounded current-fact diagnostics")
    add_daemon_arguments(parser)
    for role in ("extractor", "answerer", "judge"):
        add_endpoint_arguments(parser, role)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.phase in {"full", "extract"}:
            types = ALL_TYPES if args.types == "all" else tuple(part.strip() for part in args.types.split(","))
            if set(types) - set(ALL_TYPES) or args.limit is not None and args.limit < 1:
                raise BenchError("select known LongMemEval types and a positive limit")
            if args.data is None:
                raise BenchError("provide the local dataset path with --data")
            dataset, questions = load_questions(args.data, types, args.limit)
            setup = daemon_setup(args)
            extractor = endpoint_args(args, "extractor")
            config = bench_config(contiguity=args.contiguity, timeline=args.timeline,
                                  known_facts_window=args.known_facts_window)
            control = bench_config()
            full = (args.dataset == "oracle" and args.limit is None and set(types) == set(ALL_TYPES)
                    and len(questions) == 500 and {row["question_type"] for row in questions} == set(ALL_TYPES))
            manifest = {"dataset": args.dataset, "dataset_sha256": dataset,
                        "scope": "oracle-500" if full else "slice", "types": list(types),
                        "question_ids": [row["question_id"] for row in questions],
                        "context_policy": CONTEXT_POLICY, "daemon": setup.provenance(),
                        "extractor": extractor.provenance(), "config": config, "diagnose": args.diagnose,
                        "control_policy": {"entry_width": ENTRY_WIDTH, "config": control}}
            frozen = pinned_controls(args.control_from, manifest) if args.control_from else None
            if frozen is not None:
                manifest["control_set_sha256"] = fingerprint(frozen)
            extractor.verify()
            run_extract(args.out, manifest, questions, lambda active: setup.bank(
                config if active else control, extractor if active else None), frozen)
        if args.phase in {"full", "answer"}:
            run_answer(args.out, endpoint_args(args, "answerer"), endpoint_args(args, "judge"))
        if args.phase == "report":
            report(args.out)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"LongMemEval incomplete: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
