"""Controls for the caller's narrow process and target associations."""

import base64
import copy
import json
from datetime import datetime, timezone

import pytest

from . import compare, core, normalize
from .rows import lease_transport


def observation(first=100, stamp=1_700_000_000):
    ids = [first, first + 1, first + 2]
    clock = datetime.fromtimestamp(stamp, timezone.utc).strftime("%H:%M:%S")
    text = (f"{clock} gpu lease hold started (pid {ids[1]}) for server pid {ids[0]}\n"
            f"{clock} could not take the gpu lease for server pid {ids[2]}: another process "
            "holds the gpu lock (exit 75); the server runs unleased\n"
            "RESULT=True,True,True,True\nOWNED_CLEANUP=True\n")
    binding = {"File": "<bound-cli>", "Lead": ["<bound-prefix>"]}
    owned = [{"role": role, "pid": pid,
              "started": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
              "absent": True, "command": (["<bound-cli>", "<bound-prefix>", "lease", "hold",
                                           "gpu", "--while-pid", str(ids[0])]
                                          if role == "hold" else ["<owned-sleeper>"])}
             for role, pid in zip(("server", "hold", "second"), ids)]
    return {"exit": 0, "stdout": base64.b64encode(text.encode()).decode(), "stderr": "",
            "window": [stamp, stamp + 5], "utc_offset": 0, "launcher_binding": binding,
            "files": {name: "file:" + base64.b64encode(raw).decode() for name, raw in {
                "routes.jsonl": ((json.dumps(binding) + "\n") * 5).encode(),
                "owned.json": json.dumps(owned).encode(), "untouched": b"keep\x00\xff",
            }.items()}}


def test_valid_owned_processes_compare_in_each_arms_window():
    assert not compare.diff(observation(), observation(200, 1_700_000_100), ("lease-launcher-owned",))


@pytest.mark.parametrize("change", ["route", "pid", "clock", "launch", "server"])
def test_unbound_values_cannot_be_associated(change):
    obs = observation()
    if change == "route":
        raw = normalize._file(obs, "routes.jsonl").replace(b"<bound-cli>", b"other-cli")
        normalize._set_file(obs, "routes.jsonl", raw)
    elif change in ("pid", "clock"):
        raw = core.decode(obs, "stdout")
        raw = raw.replace(b"pid 101", b"pid 999") if change == "pid" else raw.replace(b"22:13:20", b"01:02:03")
        normalize._put(obs, "stdout", raw)
    else:
        records = json.loads(normalize._file(obs, "owned.json"))
        if change == "launch":
            records[0]["started"] = "2000-01-01T00:00:00+00:00"
        else:
            records[1]["command"][-1] = "999"
        normalize._set_file(obs, "owned.json", json.dumps(records).encode())
    with pytest.raises(ValueError):
        normalize.apply(obs, ("lease-launcher-owned",), None)


def test_association_preserves_unrelated_stream_bytes_and_files():
    before = observation()
    after = copy.deepcopy(before)
    normalize._put(after, "stderr", b"extra\x00\xfe")
    normalize._set_file(after, "untouched", b"changed\x00\xff")
    differences = compare.diff(before, after, ("lease-launcher-owned",))
    assert any(line.startswith("stderr:") for line in differences)
    assert any(line.startswith("file untouched:") for line in differences)


def test_modified_fixture_input_is_preserved_and_refused(tmp_path):
    arm = core.Arm("python", tmp_path, tmp_path)
    path = lease_transport._input(arm, "input", "original")
    path.write_bytes(b"changed")
    with pytest.raises(AssertionError, match="fixture input changed"):
        lease_transport._remove_inputs(arm)
    assert path.read_bytes() == b"changed"
