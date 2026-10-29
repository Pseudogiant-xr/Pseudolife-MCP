"""Write the golden vectors for the Rust port of PyYAML's ``safe_dump``
(``src/config_io/yaml_dump.rs``), which ``POST /api/config`` writes
config.yaml with.

Each vector stores a tagged-JSON tree and the exact string produced by
``yaml.safe_dump(value, default_flow_style=False, sort_keys=False)``. The
Python value is decoded from the tagged tree, so input and dumped value agree
by construction and no object is shared (which would emit anchors).

Mappings that a Python dict cannot hold (unhashable or colliding keys such as
True/1/1.0) are dumped through a SafeDumper subclass whose only addition is a
representer that hands an ordered pair list to ``represent_mapping``; those
vectors carry ``"dumper": "pairs"``.

Output: ``goldens/yaml-dump.json``. The fuzz sections are a fixed-seed
sample; a larger sample (``--fuzz-scale 10``) is a one-off check, not the
committed file.

usage: python gen_yaml_dump_goldens.py [--check] [--fuzz-scale N]
"""

import json
import math
import random
import struct
import sys

import yaml
from pathlib import Path

assert yaml.__version__ == "6.0.3", yaml.__version__

TARGET = Path(__file__).resolve().parent / "goldens" / "yaml-dump.json"
FUZZ_SCALE = (int(sys.argv[sys.argv.index("--fuzz-scale") + 1])
              if "--fuzz-scale" in sys.argv else 1)


class Pairs(list):
    """An ordered mapping as a list of (key, value) pairs."""


class PairsDumper(yaml.SafeDumper):
    pass


PairsDumper.add_representer(
    Pairs, lambda d, data: d.represent_mapping("tag:yaml.org,2002:map", list(data))
)


class T(dict):
    """A node already in tagged form."""


# Tagged-tree constructors.
def N():
    return T(t="null")


def B(v):
    return T(t="bool", v=v)


def I(v):
    return T(t="int", v=v)


def F(v):
    return T(t="float", bits="%016x" % struct.unpack("<Q", struct.pack("<d", v))[0])


def S(v):
    return T(t="str", v=v)


def L(*items):
    return T(t="seq", v=list(items))


def M(*pairs):
    return T(t="map", v=[list(p) for p in pairs])


def MERGE():
    return T(t="merge")


def auto(v):
    """Python literal -> tagged tree (dict keys keep insertion order)."""
    if isinstance(v, T):
        return v
    if v is None:
        return N()
    if isinstance(v, bool):
        return B(v)
    if isinstance(v, int):
        return I(v)
    if isinstance(v, float):
        return F(v)
    if isinstance(v, str):
        return S(v)
    if isinstance(v, list):
        return L(*[auto(x) for x in v])
    if isinstance(v, dict):
        return M(*[(auto(k), auto(x)) for k, x in v.items()])
    raise TypeError(v)


def decode(t):
    """Tagged tree -> (python value, needs_pairs_dumper)."""
    kind = t["t"]
    if kind == "null":
        return None, False
    if kind == "bool":
        return t["v"], False
    if kind == "int":
        return t["v"], False
    if kind == "float":
        return struct.unpack("<d", struct.pack("<Q", int(t["bits"], 16)))[0], False
    if kind == "str":
        return t["v"], False
    if kind == "merge":
        return "<<", False
    if kind == "seq":
        out, pairs = [], False
        for item in t["v"]:
            v, p = decode(item)
            out.append(v)
            pairs |= p
        return out, pairs
    if kind == "map":
        items, pairs = [], False
        for k, v in t["v"]:
            kv, kp = decode(k)
            vv, vp = decode(v)
            items.append((kv, vv))
            pairs |= kp or vp
        hashable = all(not isinstance(k, (list, dict, Pairs)) for k, _ in items)
        if hashable:
            d = dict(items)
            if len(d) == len(items) and all(
                type(k) is type(dk) and (k == dk or k != k)
                for (k, _), dk in zip(items, d.keys())
            ):
                return d, pairs
        return Pairs(items), True
    raise ValueError(kind)


def dump(tree):
    value, pairs = decode(tree)
    if pairs:
        out = yaml.dump(value, Dumper=PairsDumper, default_flow_style=False, sort_keys=False)
        return out, "pairs"
    out = yaml.safe_dump(value, default_flow_style=False, sort_keys=False)
    return out, "safe"


cases = []


def add(name, tree):
    cases.append((name, auto(tree)))


# --- Scalar strings -------------------------------------------------------
resolving = [
    "true", "True", "TRUE", "false", "yes", "Yes", "YES", "no", "No", "NO", "on", "On",
    "ON", "off", "Off", "OFF", "y", "n", "Y", "N", "tRue", "yEs",
    "1", "-1", "+1", "0", "00", "007", "08", "0o17", "0b101", "-0b1", "0b", "0b2",
    "0x1F", "0X1F", "-0x1f", "0x", "0xg", "1_000", "_1000", "1__0", "1:30", "-1:30:00",
    "1:60", "190:20:30", "1:3", "0:30",
    "1.5", "-1.5", "+1.5", ".5", "-.5", "1.", "1e5", "1e+5", "1.0e+5", "1.0e5", "1.0E-5",
    "1_0.5_", ".inf", "-.inf", "+.inf", ".Inf", ".INF", ".iNf", ".nan", ".NaN", ".NAN",
    "nan", "inf", "-inf", "1:30.5", "190:20:30.15", "1.2.3", "1e", ".", "..", "+", "-",
    "null", "Null", "NULL", "nULL", "~", "", "none", "None", "nil",
    "2026-10-27", "2026-1-27", "2026-10-27T10:00:00", "2026-10-27t10:00:00Z",
    "2001-12-14t21:59:43.10-05:00", "2001-12-14 21:59:43.10 -5", "2001-12-14 21:59:43.10 ",
    "2001-12-14  21:59:43", "2001-12-14\t21:59:43.10Z", "2001-12-14 21:59:43.10 +05:3",
    "2026-10-27 ", "20261027", "1-2-3",
    "<<", "<", "<<<", "=", "==", "!", "&", "*",
    "true\n", "1\n", "null\n", "\n",
]
for s in resolving:
    add("resolve:" + s, s)

plain_or_not = [
    "hello", "hello world", "a b  c", "foo:bar", "foo: bar", "foo :bar", "foo:", "foo :",
    "a#b", "a #b", "a# b", "#a", "a,b", "a[b]", "a{b}", "a?b", "a ?b", "x - y",
    "-", "-a", "- a", "--", "---", "--- a", "---a", "...", "... a", "....", "?", "?a",
    "? a", ":", ":a", ": a", "::", ",", ",a", "[", "[a", "]", "{", "}", "#", "&a", "*a",
    "!a", "|", "|a", ">", ">a", "'", "'a", "a'b", "it's", "''", "\"", "\"a", "a\"b",
    "%a", "@a", "`a", "a%", "a@", "a`", "a!", "a&", "a*", "a|", "a>", " a", "a ", " ",
    "  ", " a ", "a  ", "\ta", "a\t", "a\tb", "a\nb", "a\n\nb", "a\n", "\na", "a\n\n",
    "\n\na", "a \nb", "a\n b", "a\r\nb", "a\rb", "\r", "caf\u00e9", "\u00e9", "\u00a0",
    "a\u00a0b", "\U0001f600", "emoji \U0001f600 end", "\u0085", "a\u0085b", "\u2028",
    "a\u2028b", "a\u2029b", "\ufeff", "\ufeffa", "\u2603", "\u00ff\u0100", "\x00",
    "\x01", "\x07\x08\x0b\x0c\x1b", "\x7f", "\x1f", "back\\slash", "\\", "quote\"d",
    "http://127.0.0.1:8080/v1", "https://example.com/a?b=c#frag", "claude-sonnet-4-5",
    "gpt-4o-mini", "a: b: c", "key=value", "C:\\data\\x", "/abs/path", "~/home",
    "~a", "a~", "1a", "1 2", "1.5 x", "-1a", "0x1Fz", "123abc", "e5", "+a", ".a", "a.",
    "a-", "a?", "a:", "a,", "a[", "a]", "a{", "a}", "-\n", "x\n\ny\n", " \n", "\n ",
    "trailing  \n", "line1\nline2\nline3", "a\n\n\nb",
]
for s in plain_or_not:
    add("str:" + repr(s), s)

# Long strings for folding.
words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon".split()
rng = random.Random(1234)
for n in (79, 80, 81, 82, 100, 120, 150, 200):
    add("long-nospace:%d" % n, "x" * n)
    add("long-nospace-quoted:%d" % n, "'" + "x" * (n - 1))
    s = ""
    while len(s) < n:
        s += rng.choice(words) + " "
    s = s[:n].rstrip() or "x"
    add("long-words:%d" % n, s)
    add("long-words-in-map:%d" % n, {"description": s})
    add("long-words-in-seq:%d" % n, [s])
    add("long-words-nested:%d" % n, {"a": {"b": [{"c": s}]}})
    add("long-words-quoted:%d" % n, "- " + s)
    add("long-words-double:%d" % n, s.replace(" ", "\u00e9 ", 3))
    add("long-words-tab:%d" % n, s + "\t")
    add("long-words-nl:%d" % n, s.replace(" ", "\n", 2))
    add("long-words-doublespace:%d" % n, s.replace(" ", "  "))
    add("long-words-trailing:%d" % n, s + " ")
    add("long-words-leading:%d" % n, " " + s)
    add("long-escape-run:%d" % n, "\u00e9" * n)
    add("long-escape-space:%d" % n, ("\u00e9 " * n)[:n])
    add("long-key:%d" % n, {s: 1})
    add("long-key-nospace:%d" % n, {"k" * n: "v"})

# --- Keys -----------------------------------------------------------------
for n in (120, 121, 122, 123, 124, 125, 127, 128, 129):
    add("key-len-str:%d" % n, {"k" * n: 1})
add("key-int", {1: "a", -5: "b", 0: "c"})
add("key-bool", {True: "t", False: "f"})
add("key-none", {None: "n"})
add("key-float", {1.5: "x", float("inf"): "y", float("-inf"): "z", 1e20: "w"})
add("key-nan", M((F(float("nan")), S("v"))))
add("key-empty-str", {"": 1})
add("key-multiline", {"a\nb": 1})
add("key-trailing-nl", {"a\n": [1, 2]})
add("key-quoted", {"true": 1, "1": 2, "null": 3, "a: b": 4, "- x": 5, " sp": 6})
add("key-unicode", {"caf\u00e9": 1, "\U0001f600": {"x": 1}})
add("key-long-unicode", {"\u00e9" * 125: 1})
add("key-long-multiline", {("word " * 30) + "\n" + ("more " * 30): "v"})
add("key-merge", M((MERGE(), M((S("a"), I(1)))), (S("b"), I(2))))
add("key-merge-value", M((S("x"), MERGE())))
add("key-merge-in-seq", L(MERGE()))
add("key-merge-root", MERGE())
add("key-collide-bool-int", M((B(True), S("a")), (I(1), S("b")), (F(1.0), S("c"))))
add("key-dup", M((S("a"), I(1)), (S("a"), I(2))))
add("key-empty-seq", M((L(), I(1))))
add("key-empty-map", M((M(), I(1))))
add("key-seq", M((L(S("a"), S("b")), S("v"))))
add("key-map", M((M((S("a"), I(1))), S("v"))))
add("key-seq-value-seq", M((L(S("a")), L(S("b"), S("c")))))
add("key-map-value-map", M((M((S("a"), I(1)), (S("b"), I(2))), M((S("c"), I(3))))))
add("key-nested-complex", L(M((L(L(S("a"))), M((M((S("x"), N())), L()))))))
add("key-long-then-seq", {"k" * 130: [1, {"a": 2}]})
add("key-long-then-map", {"k" * 130: {"a": 1}})
add("key-long-then-empty", {"k" * 130: []})
add("key-multiline-then-seq", {"a\nb": ["x"]})

# --- Numbers --------------------------------------------------------------
floats = [0.0, -0.0, 1e-05, 1e16, 1.5, 0.1, 3.0, 1e300, float("nan"), float("inf"),
          float("-inf"), 123456789.123, 0.0001, 0.00012, 1234567890123456.0,
          12345678901234567.0, 1e15, 9999999999999998.0, 1e-4, 1e-7, 1.5e-10, 2.5e+20,
          -1e-05, 5e-324, 2.2250738585072014e-308, 1.7976931348623157e308, 0.3, 2.0 / 3,
          1 / 3, 100.0, 1e22, 1e21, -123.456, 4.35, 0.1 + 0.2, 1.0000000000000002,
          1000000000000000.2]
for f in floats:
    add("float:" + repr(f), F(f))
add("float-map", {"x": 1.5, "nan": float("nan"), "neg": -0.0})
add("float-seq", [1e16, 1e-5, float("inf")])
for i in (0, -1, 1, 42, -42, 2**63 - 1, -(2**63), 10**18, 7):
    add("int:%d" % i, i)
add("int-map", {"a": 0, "b": -(2**63), "c": 2**63 - 1})
add("bools", [True, False])
add("bool-root", True)
add("none-root", None)
add("none-seq", [None, None])
add("none-map", {"a": None, "b": {"c": None}})

# --- Containers -----------------------------------------------------------
add("empty-map-root", {})
add("empty-seq-root", [])
add("empty-str-root", "")
add("empty-in-map", {"a": {}, "b": [], "c": ""})
add("empty-in-seq", [{}, [], ""])
add("empty-nested", {"a": [{}], "b": [[]], "c": {"d": {}}, "e": [[], {}]})
add("seq-of-seq", [[1, 2], [3, [4, [5]]]])
add("seq-of-map", [{"a": 1, "b": 2}, {"c": 3}])
add("seq-of-map-of-seq", [{"a": [1, 2], "b": [{"c": [3]}]}])
add("map-of-seq", {"a": [1, 2], "b": [[3]], "c": [{"d": 4}]})
add("deep", {"l1": {"l2": {"l3": {"l4": {"l5": [{"l6": [[{"l7": "deep"}]]}]}}}}})
add("seq-mixed", [1, "two", 3.0, None, True, [], {}, {"k": "v"}, ["x"]])
add("map-in-seq-first-empty", [{"a": {}, "b": []}])
add("seq-seq-map", [[{"a": 1, "b": [2]}]])
add("seq-multiline-items", ["a\nb", "c", " d", "e\n"])
add("map-multiline-values", {"a": "x\ny", "b": "line\n", "c": "\n", "d": " lead"})
add("seq-root-scalars", ["plain", "with space", "1", "true", "a: b"])

# --- Realistic config ----------------------------------------------------
add("config", {
    "daemon": {"host": "127.0.0.1", "port": 8765, "log_level": "info", "token": None},
    "memory": {
        "preset": "flat",
        "storage": {"backend": "postgres", "dsn": "postgresql://pl@127.0.0.1:5433/bank",
                    "pool_size": 10},
        "bands": [
            {"name": "working", "capacity": 512, "decay": 0.5, "ttl_hours": 24},
            {"name": "forever", "capacity": None, "decay": 0.0, "ttl_hours": None},
        ],
        "dream": {
            "enabled": True,
            "extractor": "sidecar",
            "model": "claude-sonnet-4-5",
            "endpoint": "http://127.0.0.1:8080/v1",
            "batch_size": 32,
            "interval_minutes": 30,
            "min_confidence": 0.65,
            "digest_enabled": False,
            "schedule": "0 3 * * *",
            "stop_words": ["yes", "no", "null", "~", ""],
        },
        "deep_dream": {"enabled": False, "max_candidates": 200, "review": {"auto_accept": None}},
        "reranker": {"enabled": True, "model": "BAAI/bge-reranker-v2-m3", "top_k": 20,
                     "device": "cpu", "threshold": 1e-05},
        "recency_boost_enabled": True,
        "freshness": {"evergreen": None, "slow": "30d", "volatile": "1:30"},
        "version": "1.0",
        "date": "2026-10-27",
        "empty_list": [],
        "empty_map": {},
    },
    "clients": {"codex": {"hooks": True}, "claude": {"plugin": "pseudolife-memory@1.2.3"}},
})

# --- Fuzz -------------------------------------------------------------------
alphabet = list("ab xyz019:#-?,[]{}&*!|>'\"%@`.~=<\\/_") + ["\n", "\t", "\r", "\u00e9",
            "\U0001f600", "\u0085", "\u2028", "\ufeff", "\x00", "  ", "e", "E", "+"]
frng = random.Random(20261027)


def rand_str():
    n = frng.choice([0, 1, 2, 3, 5, 8, 13, 40, 85, 130, 170])
    return "".join(frng.choice(alphabet) for _ in range(n))


def rand_scalar():
    r = frng.random()
    if r < 0.55:
        return S(rand_str())
    if r < 0.65:
        return I(frng.randint(-(2**63), 2**63 - 1))
    if r < 0.75:
        return F(struct.unpack("<d", struct.pack("<Q", frng.getrandbits(64)))[0])
    if r < 0.85:
        return B(frng.random() < 0.5)
    if r < 0.9:
        return MERGE()
    return N()


def rand_node(depth):
    r = frng.random()
    if depth <= 0 or r < 0.4:
        return rand_scalar()
    width = frng.choice([0, 1, 2, 3, 4])
    if r < 0.7:
        return L(*[rand_node(depth - 1) for _ in range(width)])
    pairs = []
    for _ in range(width):
        key = rand_node(depth - 2) if frng.random() < 0.1 else rand_scalar()
        pairs.append((key, rand_node(depth - 1)))
    return M(*pairs)


for i in range(150 * FUZZ_SCALE):
    add("fuzz-str:%d" % i, rand_str())
for i in range(150 * FUZZ_SCALE):
    add("fuzz-tree:%d" % i, rand_node(4))
for i in range(300 * FUZZ_SCALE):
    bits = frng.getrandbits(64)
    add("fuzz-float:%d" % i, F(struct.unpack("<d", struct.pack("<Q", bits))[0]))
for i in range(100 * FUZZ_SCALE):
    # Floats with few significant digits stress the exponent thresholds.
    mant = frng.randint(1, 99999)
    exp = frng.randint(-30, 30)
    add("fuzz-decimal:%d" % i, F(float("%de%d" % (mant, exp))))


# config.yaml texts as ``yaml.safe_load`` reads them (``config_io/yaml_load.rs``):
# YAML 1.1 scalars, merges, explicit tags, and values the rewrite refuses.
LOADS = [
    "a: yes\nb: No\nc: on\nd: OFF\ne: True\nf: y\n",
    "n: 017\nh: 0x1F\nb: 0b101\nu: 1_000\ns: 1:30\nneg: -0x10\nz: 0\nzz: 00\np: +12\n",
    "f: 1e5\ng: 1.5e+3\nh: 1.\ni: .5\nj: 1:30.5\nk: .inf\nl: -.Inf\nm: 1_0.5\nn: 1.e+5\no: -1.5\n",
    "x: ~\ny: null\nz:\nw: Null\n",
    "q: 'yes'\nr: \"017\"\ns: plain text here\nt: 'a: b'\n",
    "base: &b\n  x: 1\n  y: old\nder:\n  <<: *b\n  y: 2\n",
    "k: !!str 017\nl: !!int '017'\nm: !!float '1'\nn: !!bool 'yes'\no: !!null ''\n",
    "memory:\n  # a comment\n  top_k: 8\n  dream:\n    enabled: false\n    extractor_base_url: http://127.0.0.1:8080/v1\n"
    "  miras:\n    preset: custom\n    bands:\n    - name: flat\n      max_entries: 5\n",
    "",
    "# only a comment\n",
    "- a\n- b\n",
    "t: 2026-10-27\n",
    "t: 2001-12-14 21:59:43.10 -5\n",
    "list:\n- yes\n- 2026-01-01\n",
    "b: !!binary aGVsbG8=\n",
    "t: !!timestamp '2026-10-27'\n",
    "big: 99999999999999999999\n",
    "e: =\n",
    "bad: [unclosed\n",
]


def load_case(text):
    import datetime
    try:
        value = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError):
        return {"text": text, "error": True}

    def unwritable(v):
        if isinstance(v, (datetime.date, bytes)):
            return True
        if isinstance(v, list):
            return any(unwritable(x) for x in v)
        if isinstance(v, dict):
            return any(unwritable(k) or unwritable(x) for k, x in v.items())
        return False

    def too_big(v):
        if isinstance(v, bool):
            return False
        if isinstance(v, int):
            return not -2 ** 63 <= v < 2 ** 63
        if isinstance(v, list):
            return any(too_big(x) for x in v)
        if isinstance(v, dict):
            return any(too_big(x) for x in v.values())
        return False

    if unwritable(value):
        return {"text": text, "unwritable": True}
    if too_big(value):
        return {"text": text, "big_int": True}
    return {"text": text, "tree": None if value is None else auto(value)}


def render() -> str:
    vectors = []
    for name, tree in cases:
        expected, dumper = dump(tree)
        vectors.append({"name": name, "dumper": dumper, "input": tree, "expected": expected})
    doc = {
        "generator": Path(__file__).name,
        "pyyaml": yaml.__version__,
        "call": "yaml.safe_dump(v, default_flow_style=False, sort_keys=False)",
        "vectors": vectors,
        "loads": [load_case(text) for text in LOADS],
    }
    return json.dumps(doc, ensure_ascii=True, indent=0) + "\n"


def main():
    text = render()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET.name} is stale: run python {Path(__file__).name}", file=sys.stderr)
            return 1
        print(f"{TARGET.name} is current")
        return 0
    if FUZZ_SCALE != 1:
        out = TARGET.with_name(f"yaml-dump-x{FUZZ_SCALE}.json")
    else:
        out = TARGET
    out.write_text(text, encoding="utf-8", newline="\n")
    print("wrote %d vectors to %s" % (text.count('"expected"'), out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
