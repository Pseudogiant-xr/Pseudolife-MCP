"""Record the live Python maintenance policies as executable test goldens."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from pseudolife_memory.memory.compaction import compact_store
from pseudolife_memory.web.session_hook import update_notice


def main():
    rows = [
        ["e", "a", "current", 1.0, 1.0], ["e", "a", "contested", 1.0, 1.0],
        ["e", "a", "superseded", 10.0, 1.0], ["e", "a", "retired", 10.0, 2.0],
        ["e", "a", "removed", 10.0, 2.0], ["e", "a", "superseded", 20.0, 0.0],
        ["other", "a", "removed", None, 0.0],
    ]
    compaction = []
    for keep, age, now in [(2, 0.0, 15.0), (0, 0.0, 10.0), (-1, -1.0, 15.0), (3, 30.0, 86400.0 * 100)]:
        records = [SimpleNamespace(key=(r[0], r[1]), status=r[2], superseded_at=r[3], asserted_at=r[4], ordinal=i)
                   for i, r in enumerate(rows)]
        store = SimpleNamespace(records=records, _current={}, dirty_slots=set())
        count = compact_store(store, keep_per_slot=keep, min_age_days=age, now=now)
        remaining = {r.ordinal for r in store.records}
        victims = [i for i in range(len(rows)) if i not in remaining]
        if count != len(victims): raise RuntimeError("oracle compaction count disagrees with survivors")
        compaction.append(dict(rows=rows, keep=keep, min_age_days=age, now=now, victims=victims,
                               dirty_slots=sorted([list(key) for key in store.dirty_slots])))
    notices = []
    for daemon, latest, plugin in [
        ("0.15.0", "0.16.0", None), ("0.15.0", "0.16.0", "0.14.0"),
        ("0.15.0", "0.16.0", "0.15.0"), ("0.15.0", "0.15.0", None),
        ("0.16.0", "0.15.0", None), ("0.15.0", None, None),
        ("0.15.0", "0.16.0\nEXTRA", None), ("0.15.0", "x" * 33, None),
        ("1.0", "1.0.0", None), ("1.2.0", "1.2.0rc1", None),
        ("0009", "00010", None), ("9", "9" * 32, None),
    ]:
        command = "pseudolife-mcp"
        notices.append(dict(daemon=daemon, latest=latest, plugin=plugin, command=command,
                            expected=update_notice(daemon, latest, plugin, command)))
    data = dict(normalizers=[], source="live Python compact_store and update_notice", compaction=compaction, notices=notices)
    path = Path(__file__).with_name("goldens") / "maintenance-policy.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {len(compaction)} compaction and {len(notices)} notice cases")


if __name__ == "__main__": main()
