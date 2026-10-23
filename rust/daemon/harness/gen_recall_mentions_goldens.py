"""Write the golden cases for the Rust port of ``memory/recall._mentions``
(``src/recall_mentions.rs``).

Every expected value comes from calling the real Python function in this
checkout, so the Rust unit test asserts against the oracle. The corpus covers
the pattern's parts one by one (separator folding, the dot and apostrophe
boundaries, text edges, overlapping candidates), the ``re.IGNORECASE`` case
fixes (every character of every ``_EXTRA_CASES`` class, as a name character
and as a text character), combining marks around and inside a match,
non-ASCII word and space characters, and a seeded fuzz over an alphabet that
mixes those hazards.

Output: ``goldens/recall-mentions.json``, a list of ``[text, name, bool]``.

usage: python gen_recall_mentions_goldens.py [--check]
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from pathlib import Path

import _sre
from re._casefix import _EXTRA_CASES

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "recall-mentions.json"

HAND = [
    ("", "x"), ("x", ""), ("", ""), ("payments db", "payments db"),
    ("the payments-db is down", "payments db"), ("payments_db", "payments db"),
    ("payments / db", "payments-db"), ("payments 　db", "payments/db"),
    ("paymentsdb", "payments db"), ("payments db", "payments  db"),
    ("payments.db", "payments db"), ("payments db", "payments.db"),
    ("node.js", "node"), ("use node.js", "node.js"), ("node.", "node"), ("node..", "node"),
    (".env file", "env"), ("see .claude/settings.json", "claude"), ("a.env", "env"),
    ("the bench server.", "bench server"), ("bench server.x", "bench server"),
    ("v1.2", "v1"), ("v1.2 shipped", "v1.2"), ("v1. shipped", "v1"),
    ("don't", "don"), ("don’t", "don"), ("server's config", "server"),
    ("server’s config", "server"), ("server'S config", "server"),
    ("server'ſ config", "server"), ("server'sa", "server"), ("server's_", "server"),
    ("server'", "server"), ("server' x", "server"), ("x'server", "server"),
    (" 'server", "server"), ("o'server", "server"), ("server''s", "server"),
    ("k8s", "k8s"), ("k8s-prod cluster", "k8s"), ("prod-k8s", "k8s"), ("k8s_prod", "k8s"),
    ("aaa", "aa"), ("aa aa", "aa"), ("aaa aa", "aa"), ("a-a-a", "a-a"), ("xa-a", "a-a"),
    ("abab ab", "ab"), ("ab.ab ab", "ab"), ("ab'ab", "ab"),
    ("c++ rocks", "c++"), ("c++", "c++"), ("(x)", "(x)"), ("a.b", "a.b"), ("axb", "a.b"),
    ("x[y]z", "[y]"), ("$HOME", "$HOME"), ("1+1=2", "1+1"), ("a*b", "a*"), ("a|b", "a|b"),
    ("a b", "a|b"), ("^x$", "^x$"), ("\\d", "\\d"), ("9", "\\d"), ("#tag", "#tag"),
    ("-flag", "-flag"), ("x-flag", "-flag"), ("flag-", "flag-"), ("flag- x", "flag-"),
    ("flag-x", "flag-"), (" pad ", " pad"), ("pad", " pad"), ("_x_", "_x_"), ("a_x_", "_x_"),
    ("--", "--"), ("a -- b", "--"), ("a - b", "--"), ("/", "/"), ("a/b", "/"), (" ", " "),
    ("x  y", "  "), ("__", "_"), ("-", "-"), ("a - b", "a - b"), ("a-b", "a - b"),
    ("a - - b", "a - b"), ("a -_/ b", "a/b"), ("a\tb", "a b"), ("a\nb", "a b"),
    ("a\x1cb", "a b"), ("a\x1fb", "a b"), ("a b", "a b"), ("a\x85b", "a b"),
    ("a​b", "a b"), ("a᠎b", "a b"), ("a﻿b", "a b"), ("a b", "a\x1cb"),
    ("٣ apples", "٣"), ("x٣", "x"), ("٣x", "x"), ("²x", "x"),
    ("xⅧ", "x"), ("中文", "中"), ("中 文", "中"),
    ("\U0001f600x", "x"), ("x\U0001f600", "x"), ("\U0001f600", "\U0001f600"),
    ("café", "cafe"), ("café", "café"), ("café", "café"),
    ("́cafe", "cafe"), ("x́ cafe", "cafe"), ("cafe ́", "cafe"),
    ("का", "क"), ("क्", "क"), ("क्ष", "क"),
    ("ाक", "क"), ("á's", "a"), ("x'ś", "x"), ("x'́s", "x"),
    ("x.́", "x"), ("x'́", "x"), ("́'x", "x"), ("á'x", "x"),
    ("á́́́x", "x"), ("x́́́́", "x"),
    ("server'ś", "server"), ("server'́s", "server"),
    ("STRASSE", "straße"), ("straße", "STRAẞE"), ("STRAẞE", "straße"),
    ("οδος", "ΟΔΟΣ"), ("ΟΔΟΣ", "οδος"),
    ("K", "k"), ("k", "K"), ("Å", "å"), ("Ω", "ω"),
    ("İstanbul", "istanbul"), ("istanbul", "İstanbul"), ("ıstanbul", "Istanbul"),
    ("İstanbul", "İstanbul"), ("ſtop", "stop"), ("ﬅ", "ﬆ"),
    ("ẛ", "Ṡ"), ("ι", "Ι"), ("ͅ", "ι"), ("ǅ", "ǆ"),
    ("Ǆ", "ǅ"), ("ǆ", "Ǆ"),
]

NAMES = [
    "payments db", "payments_db", "payments-db", "payments/db", "Payments DB", "a b c",
    "node", "node.js", "env", ".env", "v1", "v1.2", "bench server", "server", "don", "k8s",
    "c++", "a.b", "-x", "x-", "_x", "x_", " x", "x ", "x", "s", "is", "i", "ſ",
    "İ", "ı", "Kelvin", "straße", "σοφια",
    "café", "café", "क", "٣", "中文", "\U0001f600", "-", "_",
    "/", " ", "--", "_-/", "a b", "a　b", "a\x1cb", "a b", "(x)", "[x]",
    "x*", "x+", "x?", "x|y", "^x", "x$", "x{2}", "\\x", "#x", "x&y", "x~y",
]

TEMPLATES = [
    "{}", "the {} here", "{}.", "{}.x", ".{}", "x{}", "{}x", "{}'s", "{}'t", "{}’s",
    "{}'S", "{}'sx", "x'{}", "{} {}", "{}{}", "{}́", "́{}", "á{}",
    "{}-ok", "ok-{}", "{}_ok", "ok_{}", "{}/ok", "({})", "\"{}\"", "{}٣", "٣{}",
    "{}中", "\U0001f600{}\U0001f600", " {}　", "{}..", "{}.́",
]


def variants(name):
    """Spellings of ``name`` a text might use: as written, swapped case,
    each separator run respelled."""
    yield name
    yield name.swapcase()
    yield name.upper()
    for sep in (" ", "_", "-", "/", " - ", " ", "\t\x1c"):
        yield "".join(sep if c in " _-/" else c for c in name)


def fix_cases():
    """Every character of every ``_EXTRA_CASES`` class and every character
    lowercasing into one, against each other, as name and as text."""
    groups = {}
    for code in range(0x110000):
        if not 0xD800 <= code <= 0xDFFF:
            groups.setdefault(_sre.unicode_tolower(code), []).append(chr(code))
    classes = []
    for lo, extra in sorted(_EXTRA_CASES.items()):
        members = sorted({c for k in (lo, *extra) for c in groups[k]})
        if members not in classes:
            classes.append(members)
    for extra in ("kK", "åÅ", "ωΩ", "ßẞ", "Ǆǅǆ"):
        classes.append(sorted({c for k in extra for c in groups[_sre.unicode_tolower(ord(k))]}))
    cases = []
    for members in classes:
        for a, b in itertools.product(members, repeat=2):
            cases.append((f"see {b}z now", f"{a}z"))
            cases.append((b, a))
            cases.append((f"x{b}", a))
    return cases


ALPHABET = list("abs.'_ -/xi") + [
    "S", "ſ", "’", "́", "é", "ı", "İ", "I", "K", "k",
    "K", "٣", " ", "　", "\x1c", " ", "ß", "ẞ",
    "σ", "ς", "Σ", "中", "\U0001f600", "ा", "्", "क",
    "#", "+", "\t", "ͅ", "ι", "ι",
]


def fuzz_cases(count, seed=20261019):
    rng = random.Random(seed)
    cases = []
    for _ in range(count):
        name = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 4)))
        noise = lambda: "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 5)))
        if rng.random() < 0.6:
            spelled = rng.choice(list(variants(name)))
            text = noise() + spelled + noise()
        else:
            text = noise() + noise()
        cases.append((text, name))
    return cases


def corpus():
    cases = list(HAND)
    for name in NAMES:
        for template in TEMPLATES:
            for v in variants(name):
                spelled = template.format(*[v] * template.count("{}"))
                cases.append((spelled, name))
    cases += fix_cases()
    cases += fuzz_cases(3000)
    seen, unique = set(), []
    for case in cases:
        if case not in seen:
            seen.add(case)
            unique.append(case)
    return unique


def render():
    sys.path.insert(0, str(REPO))
    from pseudolife_memory.memory.recall import _mentions

    rows = [json.dumps([text, name, _mentions(text, name)]) for text, name in corpus()]
    return "[\n" + ",\n".join(rows) + "\n]\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        if TARGET.read_text(encoding="utf-8") != expected:
            raise SystemExit("recall-mentions golden differs from the oracle")
        print("recall-mentions golden matches the oracle")
        return 0
    TARGET.write_text(expected, encoding="utf-8", newline="\n")
    rows = json.loads(expected)
    print(f"wrote {len(rows)} cases ({sum(r[2] for r in rows)} true) to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
