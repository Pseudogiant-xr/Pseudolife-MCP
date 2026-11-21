"""Measure Rust dream extraction against a frozen no-dream raw control."""
from __future__ import annotations

import argparse
from pathlib import Path
from statistics import fmean
import sys
from time import perf_counter

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.ladder_corpus import DISTRACTORS, NO_MERGE, PAIRS, TOP_K, UNANSWERABLE, approx_tokens, value_present
from evals.ladder_pair_compare import clears_ladder
from evals.retained_artifacts import fingerprint, read_json, record
from evals.retained_daemon import (
    BenchError, add_daemon_arguments, add_endpoint_arguments, bench_config, daemon_setup,
    drain_dream, endpoint_args,
)


CORPUS = [*(p["initial"] for p in PAIRS), *DISTRACTORS, *(p["update"] for p in PAIRS)]
POLICY = "rust-extractor-ladder-v1"
CONTROL_NAME = "naive-rag"


def measure(contexts):
    matched = list(zip(contexts, PAIRS, strict=True))
    return {"gold_recoverable": fmean(any(value_present(text, p["gold"]) for text in values)
                                      for values, p in matched),
            "stale_leak": fmean(any(value_present(text, p["stale"]) for text in values)
                                for values, p in matched),
            "tokens_per_query": fmean(approx_tokens("\n".join(values)) for values, _ in matched)}


def control_measurement(contexts, instrument):
    return {"rung": CONTROL_NAME, "status": "ok", "instrument": instrument, "contexts": contexts,
            "control_sha256": fingerprint(contexts), **measure(contexts)}


def check_control(control, instrument):
    contexts = control.get("contexts")
    if (control.get("instrument") != instrument or control.get("rung") != CONTROL_NAME
            or not isinstance(contexts, list) or len(contexts) != len(PAIRS)
            or any(not isinstance(values, list) or any(not isinstance(text, str) for text in values)
                   for values in contexts)
            or control.get("control_sha256") != fingerprint(contexts)):
        raise BenchError("the ladder raw control is incomplete or from different inputs")
    return measure(contexts)


def ingest(bank, turns):
    for text in turns:
        bank.store(text)


def read_contexts(bank, *, cortex):
    context = []
    elapsed = []
    key, field = ("cortex", "value") if cortex else ("entries", "text")
    for pair in PAIRS:
        start = perf_counter()
        response = bank.search(pair["question"], TOP_K)
        elapsed.append((perf_counter() - start) * 1000)
        context.append([entry[field] for entry in response.get(key, [])])
    return context, round(fmean(elapsed), 3)


def run_rung(open_bank, config, extractor):
    with open_bank(config, extractor) as bank:
        ingest(bank, CORPUS)
        started = perf_counter()
        tally = drain_dream(bank.tool, stored=len(CORPUS))
        elapsed = round(perf_counter() - started, 3)
        contexts, query_ms = read_contexts(bank, cortex=True)
    return {"status": "ok", "contexts": contexts, "consolidation": tally, "extract_seconds": elapsed,
            "query_ms": query_ms, **measure(contexts)}


def abstention(open_bank, extractor, options):
    rows = []
    for guard in (0.0, 0.3, 0.5):
        for floor in (0.0, 0.5, 0.65, 0.70, 0.75, 0.80):
            config = bench_config(**{**options, "floor": guard})
            config["memory"]["search_confidence_floor"] = floor
            with open_bank(config, extractor) as bank:
                ingest(bank, CORPUS)
                drain_dream(bank.tool, stored=len(CORPUS))

                def refuses(question):
                    hit = bank.search(question, TOP_K)
                    return hit.get("low_confidence") is True and not hit.get("cortex")

                rows.append({"guard": guard, "floor": floor, "config": config,
                             "unanswerable_abstained": sum(refuses(q) for _, _, q in UNANSWERABLE),
                             "answerable_abstained": sum(refuses(p["question"]) for p in PAIRS)})
    return rows


def supersession(open_bank, extractor, options):
    rows = []
    extra = [pair[key] for pair in NO_MERGE for key in ("a_text", "b_text")]
    for threshold in (0.0, 0.80, 0.85, 0.90, 0.95):
        config = bench_config(**options, slot_match=threshold)
        with open_bank(config, extractor) as bank:
            ingest(bank, CORPUS + extra)
            tally = drain_dream(bank.tool, stored=len(CORPUS) + len(extra))
            contexts, _ = read_contexts(bank, cortex=True)
            false_merges = [pair[side] for pair in NO_MERGE for side in ("a", "b")
                            if not bank.tool("memory_fact_get", entity=pair[side][0],
                                             attribute=pair[side][1]).get("record")]
        rows.append({"threshold": threshold, "config": config, "missing_distinct_slots": false_merges,
                     "consolidation": tally, **measure(contexts)})
    return rows


def same_quality(cells):
    keys = ("contexts", "consolidation", "gold_recoverable", "stale_leak", "tokens_per_query")
    return all(all(first[key] == second[key] for key in keys) for first, second in zip(cells, cells[1:]))


def report(paths):
    rows = [read_json(path) for path in paths]
    if not rows or rows[0].get("rung") != CONTROL_NAME:
        raise BenchError("pass the raw control artifact first, then the measured rungs")
    control = rows[0]
    instrument = control.get("instrument", {})
    if instrument.get("daemon", {}).get("engine") != "rust" or instrument.get("policy") != POLICY:
        raise BenchError("the report requires a Rust ladder control")
    metrics = check_control(control, instrument)
    failures = {}
    results = []
    for row in rows[1:]:
        if (row.get("instrument") != instrument or row.get("control_sha256") != control["control_sha256"]
                or row.get("status") != "ok"):
            raise BenchError("ladder report requires completed rungs using one identical raw control")
        name = row["rung"]
        if name in failures or name == CONTROL_NAME:
            raise BenchError("record each compared rung with a distinct name")
        failures[name] = clears_ladder(row, metrics)
        results.append({"rung": name, **{key: row[key] for key in metrics}, "failed": failures[name]})
    return {"instrument": instrument, "control_sha256": control["control_sha256"],
            "control": metrics, "results": results,
            "first_viable_rung": next((item["rung"] for item in results if not item["failed"]), None)}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rung", help="naive-rag or an explicit extractor rung label")
    mode.add_argument("--abstain", metavar="RUNG", help="sweep the Rust raw confidence and cortex floors")
    mode.add_argument("--supersede", metavar="RUNG", help="sweep Rust slot-match thresholds")
    mode.add_argument("--report", type=Path, nargs="+", metavar="ARTIFACT")
    parser.add_argument("--control", type=Path, help="the pinned no-dream control for extractor runs")
    parser.add_argument("--out", type=Path, required=True, help="new output artifact")
    parser.add_argument("--replicates", type=int, default=1)
    parser.add_argument("--window", type=int, default=0)
    parser.add_argument("--system-prompt-file", help=argparse.SUPPRESS)
    parser.add_argument("--literal-gate", help=argparse.SUPPRESS)
    add_daemon_arguments(parser)
    add_endpoint_arguments(parser, "extractor")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.report:
            summary = report(args.report)
            record(args.out, summary)
            print(f"First viable rung: {summary['first_viable_rung'] or 'none'}")
            return 0
        name = args.rung or args.abstain or args.supersede
        if name in {"regex", "regex-floor"} or args.system_prompt_file or args.literal_gate:
            raise BenchError("use an explicit Rust extractor endpoint and the daemon's supported prompt policy; "
                             "the Python regex and local prompt or literal-gate overrides are unavailable")
        if args.replicates < 1 or args.window < 0:
            raise BenchError("provide a positive replicate count and a nonnegative facts window")
        if args.replicates > 1 and (not args.rung or args.rung == CONTROL_NAME):
            raise BenchError("replicates apply only to model rungs")
        setup = daemon_setup(args)
        control_config = bench_config(floor=0.3)
        instrument = {"policy": POLICY, "daemon": setup.provenance(),
                      "corpus_sha256": fingerprint(PAIRS + DISTRACTORS), "control_config": control_config}
        if args.rung == CONTROL_NAME:
            with setup.bank(control_config) as bank:
                ingest(bank, CORPUS)
                contexts, query_ms = read_contexts(bank, cortex=False)
            record(args.out, {**control_measurement(contexts, instrument), "query_ms": query_ms})
            return 0
        if args.control is None:
            raise BenchError("provide the frozen no-dream --control artifact")
        control = read_json(args.control)
        metrics = check_control(control, instrument)
        extractor = endpoint_args(args, "extractor")
        extractor.verify()
        operands = {"floor": 0.3, "known_facts_window": args.window}
        output = {"rung": name, "instrument": instrument, "extractor": extractor.provenance(),
                  "control_sha256": control["control_sha256"]}
        if args.abstain:
            output["abstention"] = abstention(setup.bank, extractor, operands)
        elif args.supersede:
            output["supersession"] = supersession(setup.bank, extractor, operands)
        else:
            config = bench_config(**operands)
            cells = [run_rung(setup.bank, config, extractor) for _ in range(args.replicates)]
            if not same_quality(cells):
                raise BenchError("ladder repeats disagreed on the frozen corpus")
            output.update({**cells[0], "config": config, "replicates": cells,
                           "failed_ladder_checks": clears_ladder(cells[0], metrics)})
        record(args.out, output)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"Rust extractor ladder incomplete: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
