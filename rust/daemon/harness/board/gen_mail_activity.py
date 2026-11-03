"""Record Python's exact unparked, non-urgent activity boundary."""
import json
from pathlib import Path
from types import SimpleNamespace

from pseudolife_memory.storage.coordination import CoordinationStore


def cases():
    store = object.__new__(CoordinationStore)
    rows = []
    for active_seconds in (0, 1, 600):
        store.wake = SimpleNamespace(active_seconds=active_seconds)
        now = 1000.0
        for delta in (-0.25, 0, 0.25):
            last_activity = now - active_seconds + delta
            wake = store._wake_decision({}, {"last_activity": last_activity}, now,
                                        clears=None, urgent=False, message_id="fixture")
            rows.append(dict(now=now, active_seconds=active_seconds,
                             last_activity=last_activity, wake=wake))
    return rows


if __name__ == "__main__":
    target = Path(__file__).with_name("goldens") / "mail-activity.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(cases(), indent=2) + "\n", encoding="utf-8", newline="\n")
