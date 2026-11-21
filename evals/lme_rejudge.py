"""Judge the recorded Rust LongMemEval answers with a separate instrument."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import lme_scoring, longmemeval_bench as lme
from evals.retained_artifacts import fingerprint, read_json, record
from evals.retained_daemon import BenchError, add_endpoint_arguments, endpoint_args


def run(source, output, judge):
    context = read_json(source / "context-manifest.json")
    primary = read_json(source / "answer-manifest.json")
    rows = lme.scored_rows(source / "answers", context)
    instruments = judge.provenance()
    if primary.get("judge", {}).get("served_model") == instruments["served_model"]:
        raise BenchError("choose a different judge model from the primary LongMemEval run")
    if source.resolve() == output.resolve():
        raise BenchError("use a separate directory for the re-judged results")
    lme.report(source)
    manifest = {**primary, "judge": instruments, "primary_score_set_sha256": fingerprint(rows)}
    record(output / "answer-manifest.json", manifest)
    checked_endpoint = False
    for index, row in enumerate(rows):
        target = output / "answers" / f"{index:04d}.json"
        if target.exists():
            continue
        if not checked_endpoint:
            judge.verify()
            checked_endpoint = True
        record(target, lme_scoring.rejudge_row(row, judge.chat))
    updated_rows = lme.scored_rows(output / "answers", context)
    changed = {}
    for arm in lme.ARMS:
        key = f"{arm}_correct"
        changed[arm] = sum(a[key] != b[key] for a, b in zip(rows, updated_rows, strict=True))
    record(output / "comparison.json", {"n": len(rows), "disagreements": changed})
    return lme.report(source, answers=output / "answers", instrument_path=output / "answer-manifest.json",
                      output=output / "summary.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    add_endpoint_arguments(parser, "judge")
    args = parser.parse_args(argv)
    try:
        run(args.source, args.out, endpoint_args(args, "judge"))
    except (BenchError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"LongMemEval rejudge incomplete: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
