"""Compare pinned Rust public retrieval against a measured Rust baseline."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import sys
import threading

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
# Context cells built at once. Each cell's daemon embeds on one ORT thread, so
# eight cells keep eight cores busy. Measured 24 November 2026 on the
# maintainer's Windows PC: one cell's daemon (pinned fp32 Qwen3 embedding, ORT
# 1.27.0) peaked at 1.88 GiB working set while storing a 24-turn question, so
# 2.5 GiB per cell leaves room for its driver thread; the reserve keeps the
# desktop responsive. Serial cells took about 40 s each (78 cells, ~45 min).
MAX_CONTEXT_WORKERS = 8
CELL_MEMORY_BYTES = 5 * 1024 ** 3 // 2
MEMORY_RESERVE_BYTES = 4 * 1024 ** 3


def instrument_digests():
    launcher = Path(__file__).parent
    paths = [Path(__file__), launcher / "regression_gate.ps1", launcher / "qwen_server.ps1",
             *(Path(module.__file__) for module in (
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


def replay_slots(bank, slots, question_id):
    """Write the fixture's public inputs and return the writes the daemon kept as echoes.

    A write the daemon answers with action "confirmed" while returning the slot's
    previous value unchanged kept the standing value: by its compression-echo rule
    (a shorter rewrite repeating most of its words, issue #1924) or because the
    two are equal after its case and space normalisation. The response does not
    say which, so the gate records every such write and does not count it as a
    version; any other difference from the fixture's history is still an
    incomplete cell.
    """
    echoes = []
    for slot in slots:
        key = {"entity": slot["entity"], "attribute": slot["attribute"]}
        if slot["kind"] == "set":
            for member in slot["values"]:
                bank.tool("memory_set_add", **key, member=member)
            current = bank.tool("memory_fact_get", **key, verbose=True).get("record") or {}
            if set(slot["values"]) != {item["value"] for item in current.get("members", [])}:
                raise BenchError("the Rust bank did not admit every fixture set member")
            continue
        expected = []
        for value in slot["values"]:
            result = bank.tool("memory_fact_set", **key, value=value)
            if (expected and value != expected[-1] and result.get("action") == "confirmed"
                    and result.get("value") == expected[-1]):
                echoes.append({"question_id": question_id, **key, "written": value,
                               "kept": expected[-1]})
                continue
            if not expected or value != expected[-1]:
                expected.append(value)
        current = bank.tool("memory_fact_get", **key, verbose=True).get("record") or {}
        history = bank.tool("memory_history", **key)
        if (current.get("value") != expected[-1]
                or [entry["value"] for entry in history.get("versions", [])] != expected):
            raise BenchError("the Rust bank did not reproduce the fixture's public scalar history")
    return echoes


def contexts_for(question, frozen_context, slots, open_bank, stop=None):
    with open_bank() as bank:
        for session in lme.chronological_turns(question):
            for turn in session:
                if stop is not None and stop.is_set():
                    raise BenchError("gate context build stopped after another cell failed")
                bank.store(turn)
        echoes = replay_slots(bank, slots, question["question_id"])
        hits = bank.search(question["question"], lme.ENTRY_WIDTH)
        raw = lme.raw_texts(hits)
        facts = lme.fact_lines(bank, hits)
    row = {key: question[key] for key in (*rebuild_contexts.QUESTION_FIELDS, "question_type")}
    row["quarantined"] = 0  # the gate replays projected slots without running extraction
    row["echo_confirmed"] = echoes
    row["contexts"] = {"rag": "\n\n".join(raw), "cortex": "\n".join(facts),
                       "hybrid": context_format.hybrid_context(facts, raw), "rag_control": frozen_context}
    return row


def available_memory():
    """Bytes a new process can use now, or None where it cannot be read.

    On Windows that is the smaller of available physical memory and the
    remaining commit budget, since a daemon's model allocation needs both.
    """
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended", ctypes.c_ulonglong)]

        status = Status(length=ctypes.sizeof(Status))
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return min(status.avail_phys, status.avail_page)
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def usable_cpus():
    """Cores this process may run on (its affinity where the platform reports one)."""
    if hasattr(os, "process_cpu_count"):
        return os.process_cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


def context_workers(requested, available=None, cpus=None):
    """How many context cells run at once: the request, else what memory and cores allow.

    Unreadable memory means one cell at a time, never the maximum.
    """
    if requested < 0:
        raise BenchError("--context-workers must be 0 (size to this machine) or a positive count")
    if requested:
        return requested
    cpus = cpus if cpus is not None else usable_cpus()
    available = available if available is not None else available_memory()
    if available is None:
        return 1
    by_memory = (available - MEMORY_RESERVE_BYTES) // CELL_MEMORY_BYTES
    return max(1, min(MAX_CONTEXT_WORKERS, cpus // 2, by_memory))


def build_contexts(questions, controls, slots, open_bank, workers):
    """Build every cell, each in its own bank and daemon, returned in question order.

    Cells share nothing: a fresh test database and daemon each, one ORT thread per
    daemon. Running several at once changes only the wall time, never a context.
    The first failure, or an interrupt, stops queued cells and tells running ones
    to stop at their next turn, so the run ends within one stored turn.
    """
    if workers == 1:
        return [contexts_for(q, controls[q["question_id"]], slots[q["question_id"]], open_bank)
                for q in questions]
    stop = threading.Event()
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gate-cell")
    futures = {pool.submit(contexts_for, q, controls[q["question_id"]], slots[q["question_id"]],
                           open_bank, stop): index for index, q in enumerate(questions)}
    rows = [None] * len(questions)
    try:
        for future in as_completed(futures):
            rows[futures[future]] = future.result()
    except BaseException:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return rows


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
        *, repetitions, baseline, workers=1, contexts_only=False):
    if baseline is not None and baseline["manifest"] != manifest:
        raise BenchError("the Rust baseline inputs changed")
    record(root / "inputs.json", {"manifest": manifest, "daemon": setup_id, "replicates": repetitions})
    context_file = root / "contexts.json"
    if context_file.exists():
        contexts = read_json(context_file)
    else:
        print(f"Building {len(questions)} gate contexts, {workers} at a time", flush=True)
        contexts = build_contexts(questions, controls, slots, open_bank, workers)
        record(context_file, contexts)
    if ([row.get("question_id") for row in contexts] != manifest["question_ids"]
            or any(row["contexts"].get("rag_control") != controls[row["question_id"]]
                   or set(row["contexts"]) != set(lme.ARMS)
                   or not isinstance(row.get("echo_confirmed"), list) for row in contexts)):
        raise BenchError("gate context checkpoint is incomplete or uses a different raw control")
    echoes = [echo for row in contexts for echo in row["echo_confirmed"]]
    if contexts_only:
        print(f"Rust gate contexts recorded at {context_file}; {len(echoes)} fixture writes "
              "were kept by the daemon as compression echoes.", flush=True)
        return 0
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
                "echo_confirmed": echoes,
                "control_score_sha256": score_control(answers[0])}
    if baseline is None:
        candidate = root / "baseline-candidate.json"
        record(candidate, measured)
        print(f"Rust baseline candidate recorded at {candidate}. The maintainer reviews it and "
              "commits it as evals/results/rust-quality/regression_gate.baseline.json. "
              f"It lists {len(echoes)} fixture writes the daemon kept as compression echoes.")
        return 0
    if measured["control_score_sha256"] != baseline["control_score_sha256"]:
        raise BenchError("answerer or judge drifted on the pinned raw control")
    failures = replicate.gate_verdict(measured, baseline)
    warnings = []
    if measured["quarantined"] != baseline.get("quarantined", 0):
        warnings.append(f"quarantined entries changed from {baseline.get('quarantined', 0)} "
                        f"to {measured['quarantined']}")
    if echoes != baseline.get("echo_confirmed", []):
        warnings.append(f"fixture writes kept as compression echoes changed from "
                        f"{len(baseline.get('echo_confirmed', []))} to {len(echoes)}")
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
    parser.add_argument("--contexts-only", action="store_true",
                        help="build and record the contexts, then stop before any model call")
    parser.add_argument("--context-workers", type=int, default=0,
                        help="context cells built at once; 0 sizes the pool to free memory and cores")
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
        workers = context_workers(args.context_workers)
        return run(args.out, manifest, setup_id, questions, controls, slots, lambda: setup.bank(config),
                   answerer, judge, repetitions=args.replicates, baseline=baseline,
                   workers=workers, contexts_only=args.contexts_only)
    except (Exception, SystemExit) as exc:
        return report_incomplete("Rust regression gate", exc)


if __name__ == "__main__":
    raise SystemExit(main())
