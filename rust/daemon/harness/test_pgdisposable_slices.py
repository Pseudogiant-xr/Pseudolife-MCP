"""Each harness admits its own slice's disposable names whatever imported
the shared helper first, and no other slice's."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
# Harness modules a pytest process can import together, with the slice each
# admits when the launcher sets none.
SLICES = {"run": "w1a", "board.run": "w2f", "graph_store": "w3h", "w2e": "w2e",
          "w2d_search": "w2d", "w2d_cortex_check": "w2d", "mcp_run": "w2g",
          "principals": "prn", "dream_cursors": "w3d", "schema_cases": "pgs",
          "clock_retry_cases": "pgs", "startup_cases": "pgs", "writer_session_cases": "pgs"}
PROBE = '''
import importlib, json, os, sys
order = json.loads(sys.argv[1])
modules = {m: importlib.import_module(m) for m in order}
import pgdisposable
seen = {}
for m, mod in modules.items():
    pg = mod.pg
    admitted = {}
    for other in sorted(set(json.loads(sys.argv[2]).values())):
        try:
            pg.name(f"pl_cf_{other}_probe")
            admitted[other] = True
        except ValueError:
            admitted[other] = False
    seen[m] = {"slice": pg.SLICE, "prefix": pg.PREFIX, "admitted": admitted}
print(json.dumps({"seen": seen, "env": os.environ.get("PL_HARNESS_SLICE"),
                  "default": [pgdisposable.SLICE, pgdisposable.PREFIX]}))
'''


def probe(order: list[str]) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PL_HARNESS_SLICE"}
    result = subprocess.run([sys.executable, "-c", PROBE, json.dumps(order), json.dumps(SLICES)],
                            cwd=HERE, env=env, capture_output=True, text=True, encoding="utf-8",
                            timeout=300)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_every_harness_keeps_its_slice_in_any_import_order(order):
    names = list(SLICES) if order == "forward" else list(reversed(SLICES))
    out = probe(names)
    for module, own in SLICES.items():
        seen = out["seen"][module]
        assert (seen["slice"], seen["prefix"]) == (own, f"pl_cf_{own}_"), (module, seen)
        assert seen["admitted"] == {s: s == own for s in seen["admitted"]}, (module, seen)
    # Importing a harness configures only that harness: the launcher's slice
    # (none here) is what the shared helper's own functions still follow.
    assert out["env"] is None
    assert out["default"] == ["w1a", "pl_cf_w1a_"]


def test_the_launcher_slice_is_read_when_used():
    probe_code = '''
import os
import pgdisposable as pg
os.environ["PL_HARNESS_SLICE"] = "w2f"
assert (pg.SLICE, pg.PREFIX) == ("w2f", "pl_cf_w2f_"), pg.PREFIX
assert pg.name("pl_cf_w2f_probe").startswith("pl_cf_w2f_probe")
for name in ("pl_cf_w1a_probe", "pl_cf_w2f_", "pl_cf_w2f_A", "pl_cf_w2fx_probe"):
    try:
        pg.name(name)
    except ValueError:
        continue
    raise AssertionError(f"admitted {name!r}")
os.environ["PL_HARNESS_SLICE"] = "nope"
try:
    pg.name("pl_cf_nope_probe")
except SystemExit as exc:
    assert "PL_HARNESS_SLICE='nope'" in str(exc), exc
else:
    raise AssertionError("an unknown slice was admitted")
'''
    env = {k: v for k, v in os.environ.items() if k != "PL_HARNESS_SLICE"}
    result = subprocess.run([sys.executable, "-c", probe_code], cwd=HERE, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
