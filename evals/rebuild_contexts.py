"""Project the local historical gate dumps into public Rust write inputs."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import re
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.retained_artifacts import fingerprint, record
from evals.retained_daemon import BenchError


PINNED_ROWS = Path(__file__).resolve().parent / "results/longmemeval-ku-oracle-e4b-ft-arm1.jsonl"
SCHEMA = "rust-gate-fact-fixture-v1"
QUESTION_FIELDS = ("question_id", "question", "answer", "question_date")


def question_stamp(row):
    return fingerprint({key: row[key] for key in QUESTION_FIELDS})


def pinned_rows(path=PINNED_ROWS):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row["question_id"] for row in rows]
    if len(ids) != 78 or len(set(ids)) != 78:
        raise BenchError("the pinned gate reference must contain its 78 distinct questions")
    return rows


def slot_writes(facts):
    slots = {}
    for fact in facts:
        # The public writes assign Rust provenance. A dump's Python origin
        # is neither a Rust writer identity nor an input to slot replay.
        entity, attribute, value = (fact[key] for key in ("entity", "attribute", "value"))
        if any(not isinstance(text, str) or not text.strip() for text in (entity, attribute, value)):
            raise BenchError("the fixture contains a blank or invalid fact")
        key = (entity.strip().casefold(), attribute.strip().casefold())
        kind = fact.get("kind", "scalar")
        if kind == "member":
            slot = slots.setdefault(key, {"entity": entity, "attribute": attribute,
                                          "kind": "set", "values": []})
            if slot["kind"] != "set" or value in slot["values"]:
                raise BenchError("a set fixture contains a mixed or duplicate slot")
            slot["values"].append(value)
            continue
        if kind != "scalar" or key in slots:
            raise BenchError("the fixture has an unsupported fact kind or duplicate scalar slot")
        history = fact.get("history") or []
        if not isinstance(history, list) or any(not isinstance(item, str) and item is not None for item in history):
            raise BenchError("the fixture contains invalid scalar history")
        values = [item for item in history if item and item.strip()]
        if not values or values[-1] != value:
            values.append(value)
        slots[key] = {"entity": entity, "attribute": attribute, "kind": "scalar", "values": values}
    return list(slots.values())


def project(directory, rows):
    cells = []
    for expected in rows:
        qid = expected["question_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", qid):
            raise BenchError("invalid question ID for a local dump filename")
        with gzip.open(directory / f"{qid}.json.gz", "rt", encoding="utf-8") as stream:
            dump = json.load(stream)
        if question_stamp(dump) != question_stamp(expected):
            raise BenchError("a local dump does not match the pinned gate question")
        cells.append({"question_id": qid, "question_sha256": question_stamp(expected),
                      "source_sha256": fingerprint(dump), "slots": slot_writes(dump["facts"])})
    return {"schema": SCHEMA, "cells": cells}


def fixture_cells(fixture, rows):
    if fixture.get("schema") != SCHEMA or not isinstance(fixture.get("cells"), list):
        raise BenchError("invalid Rust gate fixture schema")
    cells = fixture["cells"]
    if len(cells) != len(rows):
        raise BenchError("fixture does not cover the pinned gate question set")
    result = {}
    for cell, expected in zip(cells, rows, strict=True):
        qid = expected["question_id"]
        if (cell.get("question_id") != qid or cell.get("question_sha256") != question_stamp(expected)
                or not re.fullmatch(r"[a-f0-9]{64}", cell.get("source_sha256", ""))):
            raise BenchError("fixture question order or identity does not match the gate reference")
        if not isinstance(cell.get("slots"), list):
            raise BenchError("fixture question has no slot list")
        keys = set()
        for slot in cell["slots"]:
            entity, attribute, values = (slot.get(key) for key in ("entity", "attribute", "values"))
            if (not isinstance(entity, str) or not entity.strip() or not isinstance(attribute, str)
                    or not attribute.strip() or slot.get("kind") not in {"scalar", "set"}
                    or not isinstance(values, list) or not values
                    or any(not isinstance(value, str) or not value.strip() for value in values)
                    or slot["kind"] == "set" and len(set(values)) != len(values)):
                raise BenchError("fixture question contains an invalid public write input")
            key = (entity.strip().casefold(), attribute.strip().casefold())
            if key in keys:
                raise BenchError("fixture question contains the same slot twice")
            keys.add(key)
        result[qid] = cell["slots"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new local fixture JSON path")
    args = parser.parse_args(argv)
    try:
        rows = pinned_rows()
        fixture = project(args.dump_dir, rows)
        fixture_cells(fixture, rows)
        record(args.out, fixture)
    except (BenchError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Projection incomplete: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
