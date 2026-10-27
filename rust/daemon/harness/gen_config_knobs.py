"""Write the Console knob table the Rust daemon serves at ``/api/config``.

``pseudolife_memory/web/config_io.py::KNOBS`` is the oracle. Each knob is
written as the item ``read_config`` builds for it, minus the running
``value``: the metadata keys in ``read_config``'s order with ``None``
dropped, plus the knob's ``group``. ``defaults`` holds the value
``read_config`` serves for each knob when no config file exists
(``AppConfig()`` plus the daemon's MCP overlay), which the Rust unit tests
compare against the Rust daemon's own defaults.

Output: ``goldens/config-knobs.json``.

usage: python gen_config_knobs.py [--check]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "config-knobs.json"
sys.path.insert(0, str(REPO))

from pseudolife_memory.service import MemoryService  # noqa: E402
from pseudolife_memory.utils.config import AppConfig  # noqa: E402
from pseudolife_memory.web import config_io  # noqa: E402

# read_config's item keys, in its order.
ITEM_KEYS = ("path", "label", "type", "default", "min", "max", "step",
             "options", "restart", "help", "format", "suggestions")


INF = float("inf")

# (knob path, inputs) for `_coerce`: every type, bound and refusal class.
COERCE = [
    ("memory.top_k", [5, 0, 51, 3.7, -0.5, "7", " 8 ", "1_0", "3.7", "abc", "",
                      None, True, False, [1], {}, 10 ** 30, INF, -INF]),
    ("memory.retrieval_log.retention_days", [365, 365.0, 0.5, 3651, "١٢"]),
    ("memory.search_confidence_floor", [0.5, 1, 2, -0.1, "0.3", " 1e-1 ", "nan",
                                        "inf", "-Infinity", "1_0.5", "x", None,
                                        False, [0.1], INF]),
    ("memory.dream.idle_seconds", [600, 1e-05, 0.0]),
    ("memory.dream.literal_gate", ["enforce", "Enforce", "bogus", None, 1, True,
                                   ["enforce"], "it's"]),
    ("memory.dream.extractor_base_url", ["http://x", "  ", "", None, "ftp://x",
                                         "HTTPS://Y", " http://pad ", 5]),
    ("memory.dream.extractor_model", [" m ", 1.5, True, [1, "a"], {"a": 1}, 0]),
    ("memory.hide_superseded", [True, False, "yes", " On ", "no", "TRUE", 0, 2,
                                0.0, [], [0], {}, None, ""]),
]


def tagged(v):
    """JSON cannot carry an infinity; the Rust test reads this tag."""
    if isinstance(v, float) and v in (INF, -INF):
        return {"$float": "inf" if v > 0 else "-inf"}
    return v


def coerce_cases() -> list:
    out = []
    for path, inputs in COERCE:
        knob = config_io._KNOB_BY_PATH[path]
        for value in inputs:
            try:
                result = {"ok": config_io._coerce(knob, value)}
            except (ValueError, TypeError, OverflowError) as exc:
                result = {"error": type(exc).__name__, "text": str(exc)}
            out.append({"path": path, "input": tagged(value), **result})
    return out


# read_config over a daemon booted on ``boot`` whose config.yaml now holds
# ``saved``: the boot values served uncoerced, defaults where unset, and the
# restart knobs' ``saved`` (absent when equal or not coercible).
BOOT = """\
memory:
  top_k: 9
  reranker:
    top_n: 10
  retrieval_log:
    enabled: false
    retention_days: 365.0
  deep_dream:
    judge_url: null
    judge_mode: auto
  dream:
    enabled: false
    sweep_interval_seconds: 300
    extractor_model: some-model
time:
  relative_age: 1
"""
READ = [
    ("unchanged", BOOT),
    ("saved-differs", BOOT
     .replace("top_k: 9", "top_k: 12")
     .replace("top_n: 10", "top_n: 30")
     .replace("    enabled: false\n    sweep", "    enabled: true\n    sweep")
     .replace("sweep_interval_seconds: 300", "sweep_interval_seconds: 300.0")),
    ("saved-not-coercible", BOOT
     .replace("top_n: 10", "top_n: abc")
     .replace("retrieval_log:\n    enabled: false", "retrieval_log:\n    enabled: null")),
    ("saved-not-a-mapping", "- a\n- b\n"),
    ("saved-overflow", BOOT.replace("top_n: 10", "top_n: .inf")),
]


def read_cases() -> list:
    import tempfile
    from types import SimpleNamespace

    from pseudolife_memory.service import _user_yaml_leaves
    from pseudolife_memory.utils.config import load_config

    out = []
    with tempfile.TemporaryDirectory() as tmp:
        boot = Path(tmp) / "boot.yaml"
        boot.write_text(BOOT, encoding="utf-8")
        cfg = load_config(boot)
        MemoryService._apply_mcp_defaults(cfg, user_keys=_user_yaml_leaves(boot))
        for name, saved in READ:
            data = Path(tmp) / name
            data.mkdir()
            (data / "config.yaml").write_text(saved, encoding="utf-8")
            svc = SimpleNamespace(config=cfg, data_dir=data)
            try:
                got = config_io.read_config(svc)
                got.pop("config_path")
                result = {"ok": got}
            except OverflowError as exc:
                result = {"error": type(exc).__name__, "text": str(exc)}
            out.append({"name": name, "saved": saved, **result})
    return out


def build() -> dict:
    if os.environ.get("PSEUDOLIFE_MCP_CONFIG"):
        raise SystemExit("unset PSEUDOLIFE_MCP_CONFIG first: read_config reads it")
    cfg = AppConfig()
    MemoryService._apply_mcp_defaults(cfg)
    knobs, defaults = [], {}
    for knob in config_io.KNOBS:
        item = {k: knob.get(k) for k in ITEM_KEYS if knob.get(k) is not None}
        knobs.append({"group": knob["group"], "item": item})
        defaults[knob["path"]] = config_io._get_by_path(cfg, knob["path"])
    return {"knobs": knobs, "defaults": defaults, "coerce": coerce_cases(),
            "boot": BOOT, "read": read_cases()}


def render() -> str:
    return json.dumps(build(), indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET.name} is stale: run python {Path(__file__).name}",
                  file=sys.stderr)
            return 1
        print(f"{TARGET.name} is current ({len(build()['knobs'])} knobs)")
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
