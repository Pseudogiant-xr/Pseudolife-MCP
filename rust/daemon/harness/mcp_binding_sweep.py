"""Compare the Rust MCP argument binder with the Python one, in process.

usage: python mcp_binding_sweep.py --rust-bin <pseudolife-daemon> [--out FILE]
                                   [--tool NAME] [--show N] [--mutants]

Generates argument variants for every parameter of every tool from the
committed binding table (`src/mcp/binding.json`), plus unknown-name and
missing-parameter cases. Each case is one JSON object text: the Python side
parses it as the SDK transport does (`pydantic_core.from_json`) and binds it
with the registered tool's own `_StringSafeMetadata.validate_arguments`; the
Rust side parses it with the daemon's `serde_json` and binds it in the probe
mode (`PSEUDOLIFE_MCP_BIND_PROBE`) of a `--features harness` or `mutants`
binary.
Bound values compare type-strictly (a bool is not an int, an int is not a
float, floats by `repr`); refusals compare as whole payloads. No database,
model or daemon is started. Exits 1 on any difference, 2 on an
infrastructure failure.

`--mutants` needs a `--features mutants` binary: the clean run must
match, then every binder mutant in MUTANTS must produce a difference. The
strict first pass of a smart union is not in MUTANTS: neither union in
today's table can tell it apart (lax `str` never takes a non-string, lax
`list` never takes a string), so a unit test in `binding.rs` covers it.

The corpus holds no naive `as_of` instant before 1970: on Windows, Python's
`timestamp()` raises OSError for one, where the Linux daemon converts it
(or, on 0001-01-01, refuses it: a unit test in `as_of.rs` covers that).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pydantic_core

TABLE = Path(__file__).resolve().parents[1] / "src/mcp/binding.json"

# Raw JSON number spellings: both parsers must read these bytes themselves.
NUMBERS = [
    "0", "1", "-1", "2", "3", "5", "7", "8", "9", "20", "50", "51", "120",
    "604800", "604801", "-0", "1.0", "1.5", "-0.0", "0.0", "0.5", "0.85",
    "1E2", "1e2", "0.1e1", "1e400", "-1e400", "1e-400", "1e19", "9.3e18",
    "-9.3e18", "9223372036854775807", "9223372036854775808",
    "123456789012345678901234567890", "1.0000000000000002", "4.5e15",
    "1" * 4300, "1" * 4301, "-" + "1" * 4299, "-" + "1" * 4300,
    "1" * 4300 + ".5", "1" * 4301 + ".5", "1" * 4300 + "e1", "[" + "1" * 4301 + "]",
]
STRINGS = [
    "", " ", "x", "probe", "5", "5.0", " 7 ", "+7", "-7", "007", "0_7", "1_000",
    "1__0", "0x10", "1e3", "5.5", "true", "True", "yes", "Y", "off", "on", "f",
    "2", "0", "1", "1.0", "0.0", "nan", "NaN", "inf", "-Infinity", "infinity",
    "1e400", "null", "Null", "[]", "[1,2]", '["a"]', '["a","b"]', "{}",
    '{"a":1}', '"q"', "[NaN]", "[Infinity]", "[1,]", " [1] ", "[[1]]",
    '[{"a":1}]', '{"a":1,"a":2}', "[1.5]", '["1"]', "[true]", "[null]",
    "　", " x", "\x1c", "\t\n", " probe ", " ",
    "expand", "collapse", "status", "EXPAND", "list", "update", "send",
    "user", "action", "agent", "+", "-", "auto", "directive", "constraint",
    "success", "memory", "evergreen", "volatile", "pull", "propose",
    "2026-10-01", "2026-10-01T12:00:00+00:00", "20261001", "2026-W40-4",
    "2026W404", "2026-W40", "2026W40", "2026-10-01 12:00", "2026-10-01T12",
    "2026-10-01T1230", "2026-10-01T12:30:45.5Z", "2026-10-01T12:30:45,1234567",
    "2026-10-01T12:30-05:30", "2026-10-01T12:30+0530", "2026-10-01x12:30",
    "yesterday", "2026-1001", "2026-13-01", "2026-02-30", "2024-02-29",
    "0000-01-01", "2026-10-01T", "2026-10-01T24:00", "2026-10-01T12:30Zx",
    "2026-10-01T12:30+24:00", "2026-10-01T12:30+23:59:59.999999",
    "2026-10-01T12:30+05:99", "2026-W54", "2020-W53-7", "2026-W53",
    "2026-10-01T12:3045", "2026-10-01T12:30:45.", "2026-10-01T12:30:45:67",
    "2026-10-01T123045678", "2026-10-01é12:00", "2026-10-01T12:00:00Z",
    "2026-10-01T12:00:00.000+00:00", "1696118400", " 1696118400.5 ",
    "2026-10-01T12:00:00 +00:00", "2026-10-1", "2026-W40-", "2026-W40-8",
    "2026-W40-0", "+2026-10-01", "2026-10-01T12:00:00+00", "2026-10-01T12:00:00-0000",
    "\U0001f600", "a\U0001f600b",
    "1" * 4301, "-" + "1" * 4300, " " + "1" * 4301, "1" * 4301 + ".0", "0" * 4301 + "1",
    "1" * 4300 + ".5", "+" + "1" * 4300, "1" * 4300, "-" + "1" * 4299 + "x",
    "[-" + "1" * 4300 + "]", "[" + "1" * 4301 + "]", "[" + "1" * 400 + "]",
    "0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59+00:00", "0001-01-01T00:00:00+05:00",
]
COMPOUND = [
    "true", "false", "null", "[]", "{}", "[1]", "[1.5]", '["a"]', '["a","b"]',
    "[null]", "[true]", '["1"]', "[[1]]", "[{}]", '[{"a":1}]', '{"a":1}',
    '["a","a","a","a","a","a","a","a"]', '["a","a","a","a","a","a","a","a","a"]',
    "[1,2,3]", '[1,"x",null]', "[1.0]", '["2"]', "[9223372036854775808]",
    '{"b":1,"a":[1,2],"c":{"z":null,"y":1.5}}', '[{"b":1,"a":2},{"d":"x","c":true}]',
    "1" + "0" * 400, "-1" + "0" * 400,
]
MUTANTS = (
    "mcp-no-hint", "mcp-bind-no-preparse", "mcp-bind-text-preparse",
    "mcp-bind-strict-bool", "mcp-bind-length-bytes",
    "mcp-bind-no-default", "mcp-bind-loc-join", "mcp-bind-no-alias",
    "mcp-bind-int-no-string", "mcp-bind-float-int", "mcp-bind-ascii-strip",
    "mcp-bind-asof-lenient", "mcp-bind-asof-offset-range", "mcp-bind-no-infinity",
)
UNKNOWN_SETS = [
    {"limit": 3}, {"content": "x"}, {"note": "x"}, {"notes": "x"},
    {"text": "x"}, {"zzz": 1}, {"zzz": 1, "yyy": 2}, {"Query": "x"},
]


def baseline(node):
    kind = node["kind"]
    if kind == "str":
        return "probe" * max(1, node.get("min_length", 1))
    if kind == "literal":
        return node["expected"][0]
    if kind == "int":
        return max(node.get("ge", 1), node.get("gt", 0) + 1)
    if kind == "float":
        return 0.5
    if kind == "bool":
        return False
    if kind == "list":
        return []
    if kind == "dict":
        return {}
    if kind in ("nullable",):
        return None
    if kind == "union":
        return baseline(node["choices"][0]["node"])
    if kind == "after":
        return "probe" if node["validator"] == "_non_blank" else None
    return None


def lengths(node):
    """String lengths at each `max_length` / `min_length` edge in the node."""
    out = set()
    kind = node["kind"]
    if kind == "str":
        for key in ("max_length", "min_length"):
            if key in node:
                out |= {node[key] - 1, node[key], node[key] + 1}
    for child in ("inner", "items"):
        if child in node:
            out |= lengths(node[child])
    for choice in node.get("choices", []):
        out |= lengths(choice["node"])
    return {n for n in out if n >= 0}


def param_variants(param):
    raws = list(NUMBERS) + list(COMPOUND)
    raws += [json.dumps(s, ensure_ascii=False) for s in STRINGS]
    for n in sorted(lengths(param["schema"])):
        raws += [json.dumps("a" * n), json.dumps("\U0001f600" * n, ensure_ascii=False),
                 json.dumps(" " * n), json.dumps(["a" * n])]
    return raws


def cases_for(name, table):
    params = table["params"]
    required = {p["name"]: json.dumps(baseline(p["schema"]))
                for p in params if "default" not in p}

    def obj(fields):
        return "{" + ",".join(f"{json.dumps(k)}:{v}" for k, v in fields.items()) + "}"

    yield obj(required)
    yield "{}"
    for missing in required:
        yield obj({k: v for k, v in required.items() if k != missing})
    for p in params:
        for raw in param_variants(p):
            yield obj({**required, p["name"]: raw})
    accepted = [p["name"] for p in params]
    for extra in UNKNOWN_SETS:
        yield obj({**required, **{k: json.dumps(v) for k, v in extra.items()}})
        yield obj({k: json.dumps(v) for k, v in extra.items()})
    for a in accepted:
        if len(a) > 2:
            yield obj({**required, a[:-1]: '"x"'})
            yield obj({**required, a.upper(): '"x"'})


def python_bind(meta, text):
    from pseudolife_memory.mcp_server import _BindingRefusal
    try:
        raw = pydantic_core.from_json(text)
    except ValueError:
        # The transport answers a parse error before any binding.
        return {"parse_error": True}
    try:
        result = meta.validate_arguments(raw)
    except Exception as exc:  # noqa: BLE001 - recorded, compared as a difference
        return {"exception": type(exc).__name__}
    if isinstance(result, _BindingRefusal):
        return {"refused": result.result.structured_content}
    return {"bound": result}


def same(a, b):
    """Type-strict equality: bool, int and float are distinct; floats by repr."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return repr(a) == repr(b) or (math.isnan(a) and math.isnan(b))
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def probe(rust_bin, lines, mutant=None):
    env = {**os.environ, "PSEUDOLIFE_MCP_BIND_PROBE": "1"}
    env.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
    if mutant:
        env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    proc = subprocess.run([rust_bin], input=lines.encode("utf-8"), env=env,
                          capture_output=True, timeout=600)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace")[-4000:])
        return None
    # Only a newline ends a probe line; str.splitlines would also split U+2028.
    return proc.stdout.decode("utf-8").split(chr(10))[:-1]


def compare(corpus, expected, rust_lines):
    diffs = []
    for (name, text), py, rust_line in zip(corpus, expected, rust_lines):
        rust = json.loads(rust_line)
        if not same(py, rust):
            diffs.append({"tool": name, "args": text, "python": py, "rust": rust})
    return diffs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust-bin", required=True)
    ap.add_argument("--out")
    ap.add_argument("--tool")
    ap.add_argument("--show", type=int, default=20)
    ap.add_argument("--mutants", action="store_true")
    args = ap.parse_args(argv)

    os.environ.setdefault("PSEUDOLIFE_MCP_TOOLSET", "full")
    from pseudolife_memory import mcp_server
    tools = mcp_server.mcp._tool_manager._tools
    table = json.loads(TABLE.read_text(encoding="utf-8"))["tools"]
    if set(table) != set(tools):
        print("binding table and registered tools differ; regenerate it", file=sys.stderr)
        return 2
    corpus = []
    for name in table:
        if args.tool and name != args.tool:
            continue
        seen = set()
        for text in cases_for(name, table[name]):
            if text not in seen:
                seen.add(text)
                corpus.append((name, text))
    lines = "".join(f'{{"tool":{json.dumps(n)},"args":{t}}}' + chr(10) for n, t in corpus)
    expected = [python_bind(tools[name].fn_metadata, text) for name, text in corpus]
    bound = sum("bound" in py for py in expected)
    refused = sum("refused" in py for py in expected)
    rust_lines = probe(args.rust_bin, lines)
    if rust_lines is None or len(rust_lines) != len(corpus):
        print(f"probe failed or answered {len(rust_lines or [])} of {len(corpus)} cases",
              file=sys.stderr)
        return 2
    diffs = compare(corpus, expected, rust_lines)
    report = {"cases": len(corpus), "python_bound": bound, "python_refused": refused,
              "differences": len(diffs), "diffs": diffs}
    status = 1 if diffs else 0
    if args.mutants and not diffs:
        killed = {}
        for mutant in MUTANTS:
            out = probe(args.rust_bin, lines, mutant)
            if out is None or len(out) != len(corpus):
                print(f"mutant {mutant}: probe failed", file=sys.stderr)
                return 2
            killed[mutant] = len(compare(corpus, expected, out))
            print(f"mutant {mutant}: {killed[mutant]} differences")
        report["mutants"] = killed
        survivors = [m for m, n in killed.items() if n == 0]
        if survivors:
            print(f"surviving mutants: {', '.join(survivors)}")
            status = 1
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, default=repr,
                                             ensure_ascii=False), encoding="utf-8")
    for d in diffs[:args.show]:
        print(json.dumps(d, default=repr, ensure_ascii=False)[:900])
    print(f"{len(corpus)} cases ({bound} bound, {refused} refused by Python), "
          f"{len(diffs)} differences")
    return status


if __name__ == "__main__":
    sys.exit(main())
