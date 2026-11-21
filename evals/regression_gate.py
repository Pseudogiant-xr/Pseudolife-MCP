"""Compare pinned Rust public retrieval against a measured Rust baseline."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import context_format, lme_scoring, longmemeval_bench as lme, rebuild_contexts, replicate
from evals import retained_daemon
from evals.retained_artifacts import fingerprint, read_json, record
from evals.retained_daemon import (
    BenchError, add_daemon_arguments, add_endpoint_arguments, bench_config, daemon_setup,
    endpoint_args, report_incomplete,
)


POLICY = "rust-public-search-ku78-v1"
MEASURED_ARMS = ("rag", "cortex", "hybrid", "cascade")
BASELINE = Path(__file__).resolve().parent / "results/rust-quality/regression_gate.baseline.json"


def instrument_digests():
    paths = [Path(__file__), *(Path(module.__file__) for module in (
        lme, context_format, replicate, lme_scoring, rebuild_contexts, retained_daemon,
    ))]
    # Normalize checkout newline conventions without hiding changes to source text.
    return {path.name: fingerprint(path.read_text(encoding="utf-8")) for path in paths}


def baseline_for(path, manifest):
    try:
        baseline = read_json(path)
    except BenchError as exc:
        raise BenchError(f"{exc}. Supply the measured Rust baseline or use an authorised "
                         "--establish run to create a candidate.") from None
    if (baseline.get("manifest") != manifest
            or not re.fullmatch(r"[0-9a-f]{40}", baseline.get("revision", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", baseline.get("control_score_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", baseline.get("binary_sha256", ""))
            or type(baseline.get("replicates")) is not int or baseline["replicates"] < 2
            or set(baseline.get("arms", {})) != set(MEASURED_ARMS)):
        raise BenchError("Rust baseline is incomplete or does not match this gate's inputs. "
                         "Use matching inputs or an authorised --establish run.")
    for arm in baseline["arms"].values():
        mean = arm.get("mean")
        if type(mean) not in (int, float) or not 0 <= mean <= 1 or arm.get("margin") != replicate.BASELINE_FLOOR:
            raise BenchError("Rust baseline has invalid scores or tolerance")
    return baseline


def replay_slots(bank, slots):
    for slot in slots:
        key = {"entity": slot["entity"], "attribute": slot["attribute"]}
        if slot["kind"] == "set":
            for member in slot["values"]:
                bank.tool("memory_set_add", **key, member=member)
            current = bank.tool("memory_fact_get", **key, verbose=True).get("record") or {}
            if set(slot["values"]) != {item["value"] for item in current.get("members", [])}:
                raise BenchError("the Rust bank did not admit every fixture set member")
            continue
        for value in slot["values"]:
            bank.tool("memory_fact_set", **key, value=value)
        current = bank.tool("memory_fact_get", **key, verbose=True).get("record") or {}
        history = bank.tool("memory_history", **key)
        expected = [val for index, val in enumerate(slot["values"])
                    if index == 0 or val != slot["values"][index - 1]]
        if (current.get("value") != expected[-1]
                or [entry["value"] for entry in history.get("versions", [])] != expected):
            raise BenchError("the Rust bank did not reproduce the fixture's public scalar history")


def contexts_for(question, frozen_context, slots, open_bank):
    with open_bank() as bank:
        for session in lme.chronological_turns(question):
            for turn in session:
                bank.store(turn)
        replay_slots(bank, slots)
        hits = bank.search(question["question"], lme.ENTRY_WIDTH)
        raw = lme.raw_texts(hits)
        facts = lme.fact_lines(bank, hits)
    row = {key: question[key] for key in (*rebuild_contexts.QUESTION_FIELDS, "question_type")}
    row["quarantined"] = 0  # the gate replays projected slots without running extraction
    row["contexts"] = {"rag": "\n\n".join(raw), "cortex": "\n".join(facts),
                       "hybrid": context_format.hybrid_context(facts, raw), "rag_control": frozen_context}
    return row


def aggregate(runs):
    first = runs[0]
    if len(runs) < 2 or not first or any(other != first for other in runs[1:]):
        raise BenchError("gate replicates disagree on identical input, or fewer than two completed")
    combined = replicate.aggregate({str(index): rows for index, rows in enumerate(runs)})
    if not set(MEASURED_ARMS) <= combined["arms"].keys():
        raise BenchError("missing required Rust measurement arms")
    return {arm: {"mean": combined["arms"][arm]["mean"], "margin": replicate.BASELINE_FLOOR}
            for arm in MEASURED_ARMS}


def score_control(rows):
    return fingerprint([{key: row[key] for key in ("question_id", "rag_control_response",
                                                  "rag_control_correct")} for row in rows])


def run(root, manifest, setup_id, questions, controls, slots, open_bank, answerer, judge,
        *, repetitions, baseline):
    if baseline is not None and baseline["manifest"] != manifest:
        raise BenchError("the Rust baseline inputs changed")
    record(root / "inputs.json", {"manifest": manifest, "daemon": setup_id, "replicates": repetitions})
    context_file = root / "contexts.json"
    if context_file.exists():
        contexts = read_json(context_file)
    else:
        contexts = [contexts_for(q, controls[q["question_id"]], slots[q["question_id"]], open_bank)
                    for q in questions]
        record(context_file, contexts)
    if ([row.get("question_id") for row in contexts] != manifest["question_ids"]
            or any(row["contexts"].get("rag_control") != controls[row["question_id"]]
                   or set(row["contexts"]) != set(lme.ARMS) for row in contexts)):
        raise BenchError("gate context checkpoint is incomplete or uses a different raw control")
    contexts_sha = fingerprint(contexts)
    answers = []
    answerer.verify()
    judge.verify()
    for index in range(repetitions):
        target = root / f"answers-{index + 1}.json"
        if target.exists():
            scored = read_json(target)
            if scored.get("contexts_sha256") != contexts_sha:
                raise BenchError("gate answer checkpoint refers to other contexts")
            rows = scored["rows"]
        else:
            rows = [lme_scoring.score_row(row, answerer.chat, judge.chat) for row in contexts]
            record(target, {"contexts_sha256": contexts_sha, "rows": rows})
        if replicate.strip_judged(rows) != contexts:
            raise BenchError("gate answer checkpoint differs from its context set")
        if any(type(row.get(f"{arm}_correct")) is not bool or not row.get(f"{arm}_response")
               for row in rows for arm in lme.ARMS):
            raise BenchError("gate answer checkpoint is not fully judged")
        answers.append(rows)
    arms = aggregate(answers)
    measured = {"manifest": manifest, "revision": setup_id["revision"], "arms": arms,
                "replicates": repetitions, "binary_sha256": setup_id["binary_sha256"],
                "quarantined": sum(row.get("quarantined", 0) for row in contexts),
                "control_score_sha256": score_control(answers[0])}
    if baseline is None:
        candidate = root / "baseline-candidate.json"
        record(candidate, measured)
        print(f"Rust baseline candidate recorded at {candidate}. The maintainer reviews it and "
              "commits it as evals/results/rust-quality/regression_gate.baseline.json.")
        return 0
    if measured["control_score_sha256"] != baseline["control_score_sha256"]:
        raise BenchError("answerer or judge drifted on the pinned raw control")
    failures = replicate.gate_verdict(measured, baseline)
    warnings = []
    if measured["quarantined"] != baseline.get("quarantined", 0):
        warnings.append(f"quarantined entries changed from {baseline.get('quarantined', 0)} "
                        f"to {measured['quarantined']}")
    record(root / "verdict.json", {"measured": measured, "baseline_sha256": fingerprint(baseline),
                                   "regressions": failures, "warnings": warnings})
    for warning in warnings:
        print(f"WARNING: {warning}")
    for failure in failures:
        print(failure)
    print("Rust retrieval gate regressed" if failures else "Rust retrieval gate passed")
    return int(bool(failures))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replicates", type=int, default=2)
    parser.add_argument("--establish", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    add_daemon_arguments(parser)
    add_endpoint_arguments(parser, "answerer")
    add_endpoint_arguments(parser, "judge")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.replicates < 2:
            raise BenchError("the Rust gate needs two or more replicates")
        fixture = read_json(args.fixture)
        pinned = rebuild_contexts.pinned_rows()
        slots = rebuild_contexts.fixture_cells(fixture, pinned)
        dataset_hash, data = lme.load_questions(args.data, ("knowledge-update",), None)
        by_id = {row["question_id"]: row for row in data}
        questions = [by_id[row["question_id"]] for row in pinned]
        if any(rebuild_contexts.question_stamp(left) != rebuild_contexts.question_stamp(right)
               for left, right in zip(pinned, questions, strict=True)):
            raise BenchError("the oracle data does not match the pinned gate questions")
        controls = {row["question_id"]: row["contexts"]["rag"] for row in pinned}
        config = bench_config()
        setup = daemon_setup(args)
        setup_id = setup.provenance()
        answerer, judge = endpoint_args(args, "answerer"), endpoint_args(args, "judge")
        manifest = {"engine": "rust", "policy": POLICY, "config": config, "dataset_sha256": dataset_hash,
                    "fixture_sha256": fingerprint(fixture), "question_ids": list(controls),
                    "control_sha256": fingerprint(controls), "control_origin": "pinned-frozen-python-rag",
                    "embedding_files": setup_id["embedding_files"], "ort_sha256": setup_id["ort_sha256"],
                    "answerer": answerer.provenance(), "judge": judge.provenance(),
                    "instrument_sha256": instrument_digests()}
        baseline = None if args.establish else baseline_for(args.baseline, manifest)
        if args.preflight:
            setup.preflight_database()
            return 0
        return run(args.out, manifest, setup_id, questions, controls, slots, lambda: setup.bank(config),
                   answerer, judge, repetitions=args.replicates, baseline=baseline)
    except (Exception, SystemExit) as exc:
        return report_incomplete("Rust regression gate", exc)


if __name__ == "__main__":
    raise SystemExit(main())
