"""Write the golden write sequences for the Rust port of
``CortexStore.write_fact`` (``src/stores/cortex_write.rs``).

Each sequence runs the real ``memory/cortex.py`` store in this checkout with
the inputs ``memory_fact_set`` can pass (support user/action/agent, no stance,
no provenance, no ``force_contend``, no ``dream``) and fixed clocks, and
records every write's action and record, then the final records and the
supersession log. The Rust unit test replays the same writes and must
reproduce all of it, so every engine branch is checked against the oracle,
not against a hand-written expectation.

Output: ``goldens/w2e-cortex-write.json``.

usage: python gen_w2e_cortex_write_goldens.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "w2e-cortex-write.json"

INHERIT = "inherit"


def w(e, a, v, sup, conf, t, logical=0, authority=INHERIT, distortion=INHERIT,
      freshness="evergreen", writer="writer-a", session=None):
    return {"entity": e, "attribute": a, "value": v, "support": sup, "confidence": conf,
            "now": t, "hlc": [int(t * 1000), logical], "authority": authority,
            "distortion_tolerance": distortion, "freshness_class": freshness,
            "writer_id": writer, "session_id": session}


SEQUENCES = {
    "branches": {
        "config": {"protect_provenance": True, "supersede_confidence_margin": 0.15,
                   "reinforce_rate": 0.34},
        "writes": [
            # insert, then confirm (case and separators fold), tier union
            w("Dev Box", "OS", "Debian 12", "agent", 0.7, 100.0, authority={"set": "directive"}),
            w("dev_box", "os", " debian 12 ", "user", 0.5, 101.0, session="s1"),
            # compression echo confirms the richer value
            w("pipeline", "schedule", "the deploy pipeline runs nightly on the bench box",
              "action", 0.6, 102.0, freshness="volatile"),
            w("Pipeline", "Schedule", "deploy pipeline runs nightly", "agent", 0.9, 103.0),
            # a number change is not an echo: below the margin, then supersede
            w("pipeline", "schedule", "the deploy pipeline runs nightly on bench box 2",
              "action", 0.3, 104.0),
            w("pipeline", "schedule", "the deploy pipeline runs hourly", "user", 0.9, 105.0,
              distortion={"set": "constraint"}),
            # tier downgrade parks, the same value refreshes, a new value replaces
            w("db", "host", "10.0.0.5", "user", 0.9, 106.0, freshness="slow"),
            w("db", "host", "10.0.0.6", "agent", 0.95, 107.0, session="s2"),
            w("DB", "Host", "10.0.0.6 ", "action", 0.4, 108.0, authority={"set": "quoted"}),
            w("db", "host", "10.0.0.7", "agent", 0.5, 109.0, authority={"set": None}),
            # the contender's value with the current's tier settles it
            w("db", "host", "10.0.0.7", "user", 0.9, 110.0, writer="writer-b"),
            # an older HLC loses on order, not on confidence
            w("db", "host", "10.0.0.8", "user", 0.99, 111.0, logical=0),
            w("db", "port", "5432", "action", 0.8, 112.0),
            {**w("db", "port", "5433", "action", 0.8, 112.5), "hlc": [111000, 9]},
            # an equal HLC falls back to the wall clock
            {**w("db", "port", "5434", "action", 0.8, 111.9), "hlc": [112000, 0]},
            {**w("db", "port", "5435", "action", 0.8, 112.1), "hlc": [112000, 0]},
            # negated values never echo
            w("svc", "state", "not deployed to the bench box yet", "agent", 0.7, 113.0),
            w("svc", "state", "deployed to the bench box", "agent", 0.7, 114.0),
            # explicit label clears on confirm
            w("dev box", "os", "Debian 12", "agent", 0.2, 115.0, authority={"set": None},
              distortion={"set": "belief"}),
            # unknown freshness class normalises to evergreen
            w("printer", "toner", "low", "agent", 0.6, 116.0, freshness="weekly"),
        ],
    },
    "newer_wins": {
        "config": {"protect_provenance": False, "supersede_confidence_margin": 0.15,
                   "reinforce_rate": 0.34},
        "writes": [
            w("db", "host", "a", "user", 0.9, 200.0),
            w("db", "host", "b", "agent", 0.9, 201.0),
            w("db", "host", "c", "agent", 0.1, 202.0),
            w("db", "host", "c", "agent", 0.1, 203.0),
        ],
    },
    "tuned": {
        "config": {"protect_provenance": True, "supersede_confidence_margin": 0.5,
                   "reinforce_rate": 0.5},
        "writes": [
            w("db", "host", "a", "agent", 0.9, 300.0),
            w("db", "host", "b", "agent", 0.45, 301.0),
            w("db", "host", "c", "agent", 0.3, 302.0),
            w("db", "host", "c", "agent", 0.3, 303.0),
            w("db", "host", "b", "agent", 0.9, 304.0),
        ],
    },
}

FIELDS = ("entity", "attribute", "value", "polarity", "status", "kind", "confidence",
          "asserted_at", "last_confirmed", "supersedes_value", "superseded_by_value",
          "superseded_at", "tx_time", "valid_time", "hlc_phys", "hlc_logical", "writer_id",
          "session_id", "freshness_class", "stance", "authority", "distortion_tolerance")


def record(r) -> dict:
    out = {f: getattr(r, f) for f in FIELDS}
    out["support"] = sorted(r.support)
    out["provenance"] = sorted(r.provenance)
    out["slot_embedding"] = r.slot_embedding is not None
    return out


def run(seq: dict) -> dict:
    import torch
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.labels import INHERIT as PY_INHERIT
    from pseudolife_memory.memory.slots import Slot

    store = CortexStore(**seq["config"])
    results = []
    for x in seq["writes"]:
        def label(v):
            return PY_INHERIT if v == INHERIT else v["set"]
        res = store.write_fact(
            Slot(x["entity"], x["attribute"], x["value"]), torch.tensor([1.0, 0.0]),
            slot_embedding=torch.tensor([0.0, 1.0]), confidence=x["confidence"],
            support=x["support"], now=x["now"], hlc=tuple(x["hlc"]),
            writer_id=x["writer_id"], session_id=x["session_id"],
            freshness_class=x["freshness_class"], authority=label(x["authority"]),
            distortion_tolerance=label(x["distortion_tolerance"]))
        results.append({"action": res.action,
                        "record": next(i for i, r in enumerate(store.records) if r is res.record)})
    return {**seq, "results": results, "records": [record(r) for r in store.records],
            "log": store.supersession_log,
            "dirty_slots": sorted(list(k) for k in store.dirty_slots),
            "meta_dirty": store.meta_dirty}


def main() -> int:
    sys.path.insert(0, str(REPO))
    out = {name: run(seq) for name, seq in SEQUENCES.items()}
    TARGET.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {sum(len(s['writes']) for s in out.values())} writes to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
