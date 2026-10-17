"""Write the golden write sequences for the Rust port of
``CortexStore.write_fact`` (``src/stores/cortex_write.rs``).

Each sequence runs the real ``memory/cortex.py`` store in this checkout with
the inputs ``memory_fact_set`` can pass to ``write_fact`` (support
user/action/agent, no stance, no provenance, no ``force_contend``, no
``dream``), and those ``memory_set_add`` / ``memory_set_remove`` pass to
``add_member`` / ``remove_member`` (``support=None``, no provenance, no
``dream``; ops ``add`` and ``remove``, ``src/stores/cortex_members.rs``),
with fixed clocks and embeddings, and records every write's action and
record, then the final records, the slot indexes and the supersession log. The Rust unit test replays the same writes and must
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


def add(e, a, m, emb, t, logical=0, conf=0.7, writer="writer-a", session=None):
    """One ``add_member`` call as ``service.set_add`` makes it."""
    return {"op": "add", "entity": e, "attribute": a, "value": m, "embedding": emb,
            "confidence": conf, "now": t, "hlc": [int(t * 1000), logical],
            "writer_id": writer, "session_id": session}


def remove(e, a, m, t):
    """One ``remove_member`` call as ``service.set_remove`` makes it."""
    return {"op": "remove", "entity": e, "attribute": a, "value": m, "now": t}


DEFAULT_CONFIG = {"protect_provenance": True, "supersede_confidence_margin": 0.15,
                  "reinforce_rate": 0.34}


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
    "members": {
        "config": DEFAULT_CONFIG,
        "writes": [
            # a labelled, volatile user scalar and an agent contender beside it
            {**w("Home Lab", "services", "Jellyfin", "user", 0.9, 400.0, freshness="volatile",
                 authority={"set": "directive"}, distortion={"set": "constraint"},
                 session="s0"), "embedding": [1.0, 0.0, 0.0]},
            {**w("Home Lab", "services", "Pi-hole", "agent", 0.8, 401.0),
             "embedding": [0.0, 1.0, 0.0]},
            # converts the scalar (drops stamped), adds a member, settles the contender
            add("home lab", "Services", "Pi-hole", [0.0, 1.0, 0.0], 402.0, session="s1"),
            # the old scalar's value is now a member: confirmed by value
            add("Home Lab", "services", " JELLYFIN ", [0.0, 0.0, 1.0], 403.0),
            # confirmed by cosine 0.95 against the converted member
            add("Home Lab", "services", "Jellyfin streaming", [0.95, 0.3122499, 0.0], 404.0),
            add("Home Lab", "services", "Nextcloud", [0.6, 0.0, 0.8], 405.0, writer=None),
            # blank: nothing changes
            add("Home Lab", "services", "   ", [0.0, 0.0, 1.0], 406.0),
            add("Home Lab", "services", "", [0.0, 0.0, 1.0], 406.5),
            remove("HOME LAB", "services", "nextcloud ", 407.0),
            remove("Home Lab", "services", "Nextcloud", 408.0),
            remove("nobody", "things", "x", 409.0),
            # re-added after removal: a new row beside the removed one
            add("Home Lab", "services", "Nextcloud", [0.6, 0.0, 0.8], 410.0),
            # a zero vector is never near anything
            add("Home Lab", "services", "Zero", [0.0, 0.0, 0.0], 411.0),
            # a scalar at an unlabelled evergreen slot converts with no drops
            {**w("garage", "tools", "drill", "agent", 0.6, 412.0), "embedding": [1.0, 0.0, 0.0]},
            add("Garage", "Tools", "Drill", [1.0, 0.0, 0.0], 413.0),
            # the converted member is removed, then a scalar write lands again
            remove("garage", "tools", "drill", 414.0),
            w("garage", "tools", "saw", "agent", 0.6, 415.0),
        ],
    },
    "aggregate": {
        "config": DEFAULT_CONFIG,
        "writes": [
            w("Aquarium", "fish count", "32", "user", 0.8, 500.0),
            # the same total confirms the scalar (confidence and last_confirmed only)
            add("aquarium", "Fish Count", " 32 ", [1.0, 0.0], 501.0),
            # anything else parks a contender; the same value refreshes it
            add("Aquarium", "fish count", "neon tetra", [0.0, 1.0], 502.0, session="s1"),
            add("Aquarium", "fish count", "Neon Tetra ", [0.0, 1.0], 503.0),
            add("Aquarium", "fish count", "guppy", [0.6, 0.8], 504.0),
            w("Aquarium", "budget", "$1,500", "agent", 0.7, 505.0),
            add("Aquarium", "budget", "new filter", [0.0, 1.0], 506.0),
            w("Aquarium", "offset", "-2 degrees", "agent", 0.7, 507.0),
            add("Aquarium", "offset", "heater", [0.0, 1.0], 508.0),
            # Unicode decimal digits lead an aggregate; a superscript does not
            w("Aquarium", "arabic", "\u0663\u0662", "agent", 0.7, 509.0),
            add("Aquarium", "arabic", "x", [0.0, 1.0], 510.0),
            w("Aquarium", "superscript", "\u00b25", "agent", 0.7, 511.0),
            add("Aquarium", "superscript", "x", [0.0, 1.0], 512.0),
            # letter-led: converts
            w("Aquarium", "name", "Reef 32", "agent", 0.7, 513.0),
            add("Aquarium", "name", "Reef 33", [0.0, 1.0], 514.0),
        ],
    },
    "aggregate_newer_wins": {
        "config": {"protect_provenance": False, "supersede_confidence_margin": 0.15,
                   "reinforce_rate": 0.5},
        "writes": [
            w("tank", "volume", "120 litres", "agent", 0.6, 600.0),
            # the park does not depend on protect_provenance
            add("tank", "volume", "big", [0.0, 1.0], 601.0),
            add("Tank", "Volume", "120 LITRES", [1.0, 0.0], 602.0),
        ],
    },
    "cap": {
        "config": DEFAULT_CONFIG,
        "writes": [add("Cap Box", "tags", f"tag-{i:03d}", [0.0, 0.0], 700.0 + i)
                   for i in range(100)] + [
            # capped: logged against the rejected value, nothing dirty for it
            add("cap box", "tags", "tag-100", [0.0, 0.0], 800.0),
            # dedup runs before the cap
            add("cap box", "tags", "TAG-007", [0.0, 0.0], 801.0),
            remove("cap box", "tags", "tag-000", 802.0),
            add("cap box", "tags", "tag-100", [0.0, 0.0], 803.0),
            add("cap box", "tags", "tag-101", [0.0, 0.0], 804.0),
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
    out["embedding"] = r.embedding.tolist() if r.embedding is not None else None
    return out


def index_of(store, rec):
    """The record's position in ``store.records``, None for one never stored."""
    return next((i for i, r in enumerate(store.records) if r is rec), None)


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
        op = x.get("op", "fact")
        if op == "add":
            res = store.add_member(
                Slot(x["entity"], x["attribute"], x["value"]), torch.tensor(x["embedding"]),
                confidence=x["confidence"], provenance=(), support=None, now=x["now"],
                hlc=tuple(x["hlc"]), writer_id=x["writer_id"], session_id=x["session_id"],
                dream=False)
        elif op == "remove":
            res = store.remove_member(x["entity"], x["attribute"], x["value"], now=x["now"],
                                      support=None, dream=False)
        else:
            res = store.write_fact(
                Slot(x["entity"], x["attribute"], x["value"]),
                torch.tensor(x.get("embedding", [1.0, 0.0])),
                slot_embedding=torch.tensor([0.0, 1.0]), confidence=x["confidence"],
                support=x["support"], now=x["now"], hlc=tuple(x["hlc"]),
                writer_id=x["writer_id"], session_id=x["session_id"],
                freshness_class=x["freshness_class"], authority=label(x["authority"]),
                distortion_tolerance=label(x["distortion_tolerance"]))
        result = {"action": res.action, "record": index_of(store, res.record)}
        if op != "fact":
            result["members_count"] = len(store.members(x["entity"], x["attribute"]))
        results.append(result)
    return {**seq, "results": results, "records": [record(r) for r in store.records],
            "current": sorted([list(k), i] for k, i in store._current.items()),
            "members": sorted([list(k), v] for k, v in store._members.items()),
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
