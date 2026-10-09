"""Record slot-extraction and contradiction goldens from the Python oracle.

The Rust ports in ``src/slots.rs`` and ``src/contradiction.rs`` embed
``goldens/w2e-slots.json`` and must reproduce every case byte for byte:

* ``classes`` -- Python's ``\\w``, ``\\s``, ``\\d``, ``str.isspace``,
  ``str.splitlines`` breaks, ``re.IGNORECASE`` letter classes,
  ``str.lower`` and ``str.casefold`` over every code point, plus the code
  points this Python's Unicode database leaves unassigned (a Rust table
  built on a newer Unicode may differ only there);
* ``slots`` -- ``extract_slots`` (with and without ``last_entity_context``)
  and ``extract_dev_slots`` over crafted cases and the repo's docs;
* ``contradiction`` -- ``bool(detect_contradictions(...))`` for one entry at
  float32 cosines on both sides of every threshold;
* ``norm`` -- ``cortex._norm_key`` / ``_norm_value``.

The docs corpus (every line and paragraph of ``docs/**/*.md`` and
``README.md``) is too large to embed: the golden keeps every text that
yields a slot plus a strided sample of near-misses. ``--full PATH`` writes
the whole corpus to PATH for the ignored Rust test ``full_docs_corpus``
(``W2E_FULL_GOLDEN=PATH cargo test -- --ignored full_docs_corpus``).

usage: PYTHONPATH=<repo> python gen_w2e_slots_goldens.py [--full PATH]
"""
from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TARGET = HERE / "goldens" / "w2e-slots.json"
sys.path.insert(0, str(REPO))

from pseudolife_memory.memory import contradiction as C  # noqa: E402
from pseudolife_memory.memory.cortex import _norm_key, _norm_value  # noqa: E402
from pseudolife_memory.memory.slots import extract_dev_slots, extract_slots  # noqa: E402
from pseudolife_memory.memory.titans_memory import MemoryEntry  # noqa: E402

# Budget for the sampled docs near-misses (characters of text).
DOCS_NEGATIVE_BUDGET = 450_000

# Cases the Rust port cannot match, keyed by exact input, with the reason.
# The Rust tests skip these but count and print them.
DECLARED: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Unicode classes
# ---------------------------------------------------------------------------

def _all_chars():
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue  # a Rust char cannot be a surrogate
        yield cp, chr(cp)


def _ranges(pred) -> list[list[int]]:
    out: list[list[int]] = []
    for cp, ch in _all_chars():
        if pred(ch):
            if out and out[-1][1] == cp - 1:
                out[-1][1] = cp
            else:
                out.append([cp, cp])
    return out


def classes() -> dict:
    w, s, d = re.compile(r"\w"), re.compile(r"\s"), re.compile(r"\d")
    ci = {k: re.compile(p, re.IGNORECASE) for k, p in
          (("i", "i"), ("s", "s"), ("k", "k"), ("letters", "[A-Za-z]"))}
    letters = [cp for cp, ch in _all_chars() if ci["letters"].fullmatch(ch)]
    return {
        "python": sys.version.split()[0],
        "unicode": unicodedata.unidata_version,
        "word": _ranges(lambda ch: w.fullmatch(ch) is not None),
        "space": _ranges(lambda ch: s.fullmatch(ch) is not None),
        "digit": _ranges(lambda ch: d.fullmatch(ch) is not None),
        "isspace": _ranges(str.isspace),
        "unassigned": _ranges(lambda ch: unicodedata.category(ch) == "Cn"),
        "splitlines": [cp for cp, ch in _all_chars()
                       if len(f"a{ch}b".splitlines()) == 2],
        "ignorecase": {k: [cp for cp, ch in _all_chars() if p.fullmatch(ch)]
                       for k, p in ci.items()},
        "isupper": [cp for cp in letters if chr(cp).isupper()],
        "lower": [[cp, ch.lower()] for cp, ch in _all_chars() if ch.lower() != ch],
        "casefold": [[cp, ch.casefold()] for cp, ch in _all_chars()
                     if ch.casefold() != ch],
    }


# ---------------------------------------------------------------------------
# Slot extraction
# ---------------------------------------------------------------------------

def _rows(slots) -> list[list[str]]:
    return [[s.entity, s.attribute, s.value, s.polarity] for s in slots]


def slot_case(text: str, ctx: str | None, origin: str) -> dict:
    case = {"origin": origin, "text": text, "ctx": ctx,
            "slots": _rows(extract_slots(text, last_entity_context=ctx)),
            "dev": _rows(extract_dev_slots(text))}
    if text in DECLARED:
        case["declared"] = DECLARED[text]
    return case


LOSS_FORMS = [
    "give him away", "gives her away", "giving it away", "given away", "gave him away",
    "sell it", "sells", "selling", "sold him", "lose her", "loses", "losing it", "lost him",
    "rehome", "rehomes", "rehoming", "rehomed", "donate", "donates", "donating",
    "donated", "no longer have", "no  longer\nhave", "used to have", "used to own",
    "got rid of", "throw away", "throws out", "throwing away", "thrown out",
    "threw away", "threw out", "thrw away", "GAVE HIM AWAY", "Sold", "closet",
    "lostling", "selling-out", "giveaway", "used to\thave", "got  rid  of",
    "throwaway", "thrown\u00a0out", "\u0130 lost", "s\u0131ngle lost",
]

CRAFTED = [
    # named / called
    "I have a cat named Jacque", "My dog is called Rex Junior and he is great",
    "a project named nebula", "called R2D2", "named Jacque Smith Jones",
    "named\nJacque", "renamed Foo", "named A", "called \u00c9lodie", "NAMED Foo",
    "named Jacque, called Bob", "named  Foo1 Bar2", "named Foo-Bar",
    "my cat named Jacque. She is cute", "named Rex. I have a dog",
    "called x", "named Rex\u2028Max", "named\u00a0Rex", "callednamed Rex",
    "named R", "named Ab Cd Ef", "named O'Brien", "Named Rex",
    # I have / own / got / drive a [attr] <type>
    "I have a Ragdoll cat", "I own a blue car", "I got a new laptop", "I drive a car",
    "I HAVE A CAT", "i have a cat", "I have a graphics card",
    "I have a Nvidia graphics card", "I have a gpu", "I have a GPU",
    "I have a dog named Rex", "We have a cat", "I have an apple", "I have a catalog",
    "I have a  big   dog", "I have a Big dog", "I have a BIG dog", "I have a x dog",
    "I have a X dog", "I have a cat and I own a dog", "I have a phone named Pixel",
    "I have a dog\nI drive a car", "I have a cat. She is lovely.",
    "I have a computer", "I own a bike", "I got a phone", "I have a carpet",
    "I have a red graphics card", "I have a dogged cat", "Ihave a cat",
    "I have a\ncat", "I have a cat named Jacque and a dog named Rex",
    "\u0130 have a cat", "\u0131 have a cat", "I have a \u212aat dog",
    "I have a \u017fmall cat", "I have a B\u0130KE", "I HAVE A BIKE",
    "I have a b\u0131ke", "I have a graphi\u017fs card", "I have a \u0130rish dog",
    "I have a \u0131rish dog", "I have a caf\u00e9 cat", "I have a Caf\u00e9 cat",
    "I have a Ragdoll cat named Jacque. I gave him away.",
    # pronouns
    "He likes his toys", "It is great", "Their house is big", "The dog-him thing",
    "himself", "This is it", "Th\u00e9y run", "HIM", "she", "Hers",
    "him\u0301", "x\u203fhim", "\u200dhim", "\u00b2him", "himx", "_him",
    "him_", "1him", "her2", "\u0130t is", "it\u2028is", "they them their",
    # loss with and without referent
    *[f"I {form}" for form in LOSS_FORMS],
    # fences
    "```\nhost: x\n```\nfoo port: 8080", "```python\nfoo port = 1\n```",
    "``` foo port: 1 ``` bar port: 2", "```\nfoo port: 1", "a ``` b ``` c ``` d\nx port: 9",
    # key: value / key = value
    "server host: example.com", "GND-Share host: 10.0.0.5", "the daemon port = 8765",
    "daemon db host: localhost", "Foo: bar", "a b c d e port: 80", "a b c d port: 80",
    "The port: 80", "foo port: that is", "foo port: not set", "foo port: Never",
    "foo port: " + "x" * 79, "foo port: " + "x" * 80, "foo port: " + "x" * 85,
    "foo port: 8080.", "foo port: 8080.,;!", "db_1 host: x", "foo port:: 80",
    "Foo host = bar # comment", "foo version: 3; bar version: 4",
    "k" * 59 + " port: 1", "k" * 60 + " port: 1", "a" + " " * 58 + "port: 1",
    "foo port :  80", "foo port=80", "foo port:80", "foo port :", "foo port: ",
    "1foo port: 80", "_foo port: 80", "foo.bar port: 80", "foo-bar host: x",
    "foo  port: 1", "foo port: \u00a0x", "  foo port: 1  ", "foo port: why",
    "foo port: if so", "foo port: n't", "foo port: isn't it", "foo port: no",
    "foo port: Because", "foo port: WHAT", "foo port: 1 port", "foo PORT: 1",
    "Foo Port Host: x", "my port: 8080", "the foo port: 1", "an a the foo port: 1",
    "our this that its port: 1", "foo port: 'quoted'", "foo url: http://x/y?z=1",
    "foo path: C:\\Users\\x", "foo bar baz qux port: 1",
    "foo bar baz qux quux port: 1", "Foo's port: 1", "FOO'S port: 1",
    "foo port host: 1", "foo port = 1 = 2", "foo port: 1: 2",
    # copula
    "my port is 8080", "My default port is 8080", "my point is that",
    "my port number is 5", "my default port name is x", "my is 5", "my port is",
    "the version of foo is 3", "The default version of the foo daemon is 3.1",
    "version of a b c d e is 3", "the version of is 3", "the point of foo is 3",
    "the the version of foo is 3", "a b of foo is 3", "of foo is 3",
    "GND-Share's host is nas.local", "GND-SHARE'S host is x", "Bob' host is x",
    "Is the port 80?", "foo port is not 80", "foo port is that",
    "foo port is because", "The server is down", "Postgres db port is 5433; Redis port is 6379.",
    "Postgres port is 5433. Redis port is 6379! foo host is x",
    "Claude Code is the client", "this is fine", "x port isn't 5", "This port IS 80",
    "foo port is " + "y" * 90, "foo port is 8080 and the bar host is y",
    "foo port is 1. foo port is 1.", "foo port is 1;bar host is 2",
    "foo port is 1.bar host is 2", "foo port is 1?  bar host is 2",
    "foo port is 1!\tbar host is 2", "foo port is: 1", "foo port is = 1",
    "foo.bar port is 1", "C:\\path dir is x", "/usr/bin path is x",
    "foo port is never", "foo port is No", "foo port is aren't",
    "foo port is when", "foo port is it", "Foo Bar Baz Qux host is x",
    "Foo Bar Baz Qux Quux host is x", "foo this port is 1", "this port is 1",
    "foo port\u00a0is 1", "foo port is\u00a01", "isfoo port is 1", "foo port issue is 1",
    "foo port is 1 is 2", "foo is port is 1", "the foo's port is 1",
    "my db host is x", "my host port is 1", "my foo port is 1", "MY PORT IS 1",
    "my port is 1, and more.", "my port is ...", "my port is .",
    # combinations and dedupe
    "I have a dog named Rex. My port is 8080", "a dog named Rex. Rex name is Rex",
    "named Rex. rex name is Rex", "I have a cat. my cat type is cat",
    "foo port: 1\nfoo port: 1", "foo port: 1\nFOO port: 1", "foo port: 1\nfoo port is 1",
    "foo port: 1\nfoo port: 2",
    # line breaks
    "foo port: 1\r\nbar host: 2", "foo port: 1\rbar host: 2", "foo port: 1\u2028bar host: 2",
    "foo port: 1\u2029bar host: 2", "foo port: 1\x85bar host: 2", "foo port: 1\x1cbar host: 2",
    "foo port: 1\x1dbar host: 2", "foo port: 1\x1ebar host: 2", "foo port: 1\x1fbar host: 2",
    "foo port: 1\vbar host: 2", "foo port: 1\fbar host: 2", "\x1cfoo port: 1\x1f",
    "foo port is 1\u2028bar host is 2", "foo port is 1\x1f\x1c",
    # Unicode
    "caf\u00e9 host is x", "na\u00efve port: 80", "Stra\u00dfe version is 2",
    "Cafe\u0301 host is x", "x\u00b2 port is 5", "\u216b host is x", "\u24b6 host is z",
    "\u03a3\u039f\u03a6\u039f\u03a3 host is x", "\u0130stanbul host is x",
    "the HOST of \u017ferver is x", "\u212a host is x", "foo\u00a0port is 8080",
    "foo\u3000port: 1", "\u0663 port is 1", "\u0130STANBUL'S host is x",
    "foo \u0130s 1", "foo port \u0131s 1", "foo port \u0130S 1", "foo port i\u017f 1",
    "foo \u0130P is 1", "foo \u017fcope is 1", "Jos\u00e9's port is 1",
    "\u65e5\u672c host is \u6771\u4eac", "foo port is \u00e9t\u00e9", "\u00c9cole host: x",
    "foo\u203fbar host is 1", "foo\u200dbar host is 1", "foo\u0301 host is 1",
    "foo port is \u202f1", "foo port: \u20281", "foo\u1680port is 1",
    "foo port is " + "\u00e9" * 80, "foo port is " + "\u00e9" * 81,
    "foo port: " + "\U0001f600" * 80, "\U0001d400\U0001d401 host is 1",
    "\u2160 port: 1", "\u00bd port is 1", "foo port is \u00bd",
]

CTX_VALUES = [None, "Jacque", ""]


def slot_cases() -> list[dict]:
    cases = []
    for text in dict.fromkeys(CRAFTED):
        for ctx in CTX_VALUES:
            cases.append(slot_case(text, ctx, "crafted"))
    return cases


def docs_texts() -> list[str]:
    files = sorted(REPO.glob("docs/**/*.md")) + [REPO / "README.md"]
    seen: dict[str, None] = {}
    for f in files:
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip():
                seen.setdefault(line, None)
        for para in re.split(r"\n\s*\n", text):
            if para.strip():
                seen.setdefault(para, None)
    return list(seen)


_NEAR_MISS = re.compile(r"\bis\b|[:=]|named|called|\bI\s+(?:have|own|got|drive)\b",
                        re.IGNORECASE)


def docs_cases(texts: list[str]) -> list[dict]:
    pos, neg = [], []
    for t in texts:
        case = slot_case(t, None, "docs")
        (pos if case["slots"] or case["dev"] else neg).append(case)
    near = [c for c in neg if _NEAR_MISS.search(c["text"]) and len(c["text"]) <= 400]
    total = sum(len(c["text"]) for c in near)
    stride = max(1, math.ceil(total / DOCS_NEGATIVE_BUDGET))
    return pos + near[::stride]


# ---------------------------------------------------------------------------
# Contradiction
# ---------------------------------------------------------------------------

_QUERY = torch.tensor([1.0, 0.0], dtype=torch.float32)


def _cosine(emb: torch.Tensor) -> float:
    """detect_contradictions' own similarity computation, verbatim."""
    embeddings = F.normalize(torch.stack([emb]), p=2, dim=1)
    query = F.normalize(_QUERY.unsqueeze(0), p=2, dim=1).squeeze(0)
    return (embeddings @ query).tolist()[0]


def embedding_for(target: np.float32) -> tuple[torch.Tensor, float]:
    """A 2-dim float32 embedding whose cosine with [1, 0] is ``target``
    exactly when one exists among nearby second components; the cosine
    torch actually computes is returned either way."""
    t = float(target)
    y0 = np.float32(math.sqrt(max(0.0, 1.0 - t * t)))
    best = None
    ys = [y0]
    for _ in range(64):
        ys.append(np.nextafter(ys[-1], np.float32(np.inf), dtype=np.float32))
    y = y0
    for _ in range(64):
        y = np.nextafter(y, np.float32(-np.inf), dtype=np.float32)
        ys.append(y)
    for y in ys:
        emb = torch.tensor([target, y], dtype=torch.float32)
        sim = _cosine(emb)
        if sim == t:
            return emb, sim
        if best is None or abs(sim - t) < abs(best[1] - t):
            best = (emb, sim)
    return best


def _f32_neighbours(x: float) -> list[np.float32]:
    c = np.float32(x)
    return [np.nextafter(c, np.float32(-np.inf), dtype=np.float32), c,
            np.nextafter(c, np.float32(np.inf), dtype=np.float32)]


SIM_TARGETS = sorted({float(v) for v in (
    [np.float32(v) for v in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)]
    + _f32_neighbours(0.15) + _f32_neighbours(0.35)
    + _f32_neighbours(0.70) + _f32_neighbours(0.80))})
EMBEDDINGS = {t: embedding_for(np.float32(t)) for t in SIM_TARGETS}


def _tuples(rows):
    return [tuple(r) for r in rows]


def contradiction_case(new: str, entry: str, *, new_slots=None, entry_slots=None,
                       superseded: bool = False, origin: str) -> dict:
    if new_slots is None:
        new_slots = _rows(extract_slots(new))
    if entry_slots is None:
        entry_slots = _rows(extract_slots(entry))
    hits = ""
    for t in SIM_TARGETS:
        emb, _sim = EMBEDDINGS[t]
        e = MemoryEntry(text=entry, embedding=emb, slots=_tuples(entry_slots),
                        superseded_at=1.0 if superseded else None)
        hit = bool(C.detect_contradictions(new, _QUERY, [e], new_slots=_tuples(new_slots)))
        hits += "1" if hit else "0"
    # ``hits[i]`` is the verdict at ``contradiction_sims[i]``.
    case = {"origin": origin, "new": new, "entry": entry, "new_slots": new_slots,
            "entry_slots": entry_slots, "superseded": superseded, "hits": hits}
    key = f"{new}\u0000{entry}"
    if key in DECLARED:
        case["declared"] = DECLARED[key]
    return case


NEGATION_CUES = [
    "not", "no", "never", "don't", "dont", "doesn't", "doesnt", "won't", "wont",
    "can't", "cant", "isn't", "isnt", "aren't", "arent", "wasn't", "wasnt",
    "weren't", "werent", "neither", "nor", "actually", "instead", "rather",
    "contrary", "incorrect", "wrong", "false", "mistaken", "correct", "correction",
    "corrected", "correctly", "update", "updated", "updates", "updatesd", "change",
    "changed", "changes", "no longer", "used to", "NOT", "No", "nothing", "notable",
    "knot", "updating", "changing", "correctness", "\u0130sn't", "\u0131sn't",
    "can\u2019t", "no\nlonger", "used\tto", "\u017fhould not", "do n't", "NEVER",
    "Actually,", "-not-", "not\u0301", "x\u203fnot", "\u00b2not", "_not", "n0t",
]

GAINS = [
    "I have a cat named Jacque", "I own a Tesla", "I got a puppy", "I've got a bike",
    "Ive got a bike", "I've had this laptop for years", "my dog loves walks", "My Car",
    "a dog named Rex", "a project called Nova", "\u0130 have a cat", "I\nhave a cat",
    "\u0131 own a cat", "my 2 cats", "my\tcat", "renamed Jacque", "I\u00a0have a cat",
]

LOSS_PHRASES = [
    "gave him away", "gave the cat to my neighbour, she took it away",
    "gave\n\nit away", "gave " + "x" * 35 + " away", "gave " + "x" * 36 + " away",
    "gave " + "x" * 34 + " away", "gave up", "giving it away", "give it away",
    "given away", "gives it away", "got rid of it", "sold it", "lost it", "rehomed",
    "donated it", "passed away", "died", "no longer have it", "no longer own",
    "no longer with", "no longer around", "no longer together", "no longer here",
    "is no longer with us", "is not with me", "is gone", "isn't here", "isnt around",
    "isn't with", "used to have", "used to own", "got divorced", "broke up",
    "moved out", "moved away", "quit", "threw it away", "threw out", "discard it",
    "discarded", "discarding", "abandon", "abandoned", "abandoning", "upgraded to",
    "upgrading from", "switched to", "switching from", "replaced it with",
    "replacing it with", "traded it for", "trading it for", "GAVE IT AWAY",
    "Sold", "dyed", "gaveaway", "resold", "replaced", "traded", "switch to",
    "upgrade to", "giving\naway", "\u0130s gone", "\u0131s gone", "d\u0130ed",
    "d\u0131ed", "qu\u0131t", "di\u0301ed", "lost\u203f", "gave " + "\u00e9" * 35 + " away",
    "gave " + "\u00e9" * 36 + " away",
]


def contradiction_cases() -> list[dict]:
    out = []
    base = "the cache layer is enabled on the box"
    for cue in NEGATION_CUES:
        neg = f"the cache layer is {cue} enabled on the box"
        out.append(contradiction_case(neg, base, origin="negation"))
        out.append(contradiction_case(base, neg, origin="negation"))
        out.append(contradiction_case(neg, neg + " today", origin="negation-both"))
    gain = "I have a cat named Jacque"
    for phrase in LOSS_PHRASES:
        for loss in (f"Jacque {phrase}", f"the cat {phrase}", f"she {phrase}",
                     f"Bob {phrase}", f"we {phrase}"):
            out.append(contradiction_case(loss, gain, new_slots=[], origin="state"))
            out.append(contradiction_case(gain, loss, new_slots=[], origin="state"))
    for g in GAINS:
        for loss in ("I gave him away", "it died", "we sold the Tesla",
                     "Jacque passed away", "the bike was stolen", "I sold 2 cats"):
            out.append(contradiction_case(g, loss, new_slots=[], origin="gain"))
            out.append(contradiction_case(loss, g, new_slots=[], origin="gain"))
    replacement = [
        ("my dog's name is Rex", "my dog's name is Max"),
        ("I have an RTX 4090", "I have an RTX 5090"),
        ('the password hint is "blue"', 'the password hint is "green"'),
        ("the password hint is 'blue'", "the password hint is 'green'"),
        ('hint is "blue"', "hint is 'blue'"),
        ("version 3.5 of the tool", "version 3.6 of the tool"),
        ("version 3.5 of the tool", "version 3.5 of the tool"),
        ("red green Blue", "red green Pink"),          # Jaccard exactly 0.5
        ("red green Blue", "red green Pink white"),    # 0.4
        ("red green Blue", "red green blue Pink"),     # shared slot "blue"
        ("red Blue", "green Pink"),
        ("the box runs Debian", "the box runs Ubuntu"),
        ("the box runs Debian", "the box runs Debian"),
        ("the box runs Debian-Linux", "the box runs Debian-Linux now"),
        ("Foo-", "Foo- 1"), ("Foo-1", "Foo-2"), ("a \"\" b", "a \"x\" b"),
        ('"unterminated', '"unterminated too'), ("'it's here'", "'it's there'"),
        ("port 8080", "port 8081"), ("port 8080.5", "port 8080"),
        ("x \u00c9cole y", "x Ecole y"), ("the \u0663 value", "the 3 value"),
        ("\"\u0130STANBUL\"", "\"i\u0307stanbul\""), ("we use Rust", "we use rust"),
        ("I'll go", "I'll stay"), ("ab-cd ef", "ab-cd gh"), ("A-b", "A-c"),
        ("x 1.2.3 y", "x 1.2.4 y"), ("x 12abc y", "x 12 y"),
        ("tokens: alpha beta gamma Delta", "tokens: alpha beta gamma Epsilon"),
        ("\"\u03a3\u039f\u03a6\u039f\u03a3\" wins", "\"\u03c3\u03bf\u03c6\u03bf\u03c2\" wins"),
    ]
    for a, b in replacement:
        out.append(contradiction_case(a, b, new_slots=[], origin="replacement"))
        out.append(contradiction_case(b, a, new_slots=[], origin="replacement"))
    slot_pairs = [
        ([["Jacque", "type", "cat", "+"]], [["jacque", "TYPE", "Cat", "+"]]),
        ([["Jacque", "type", "cat", "+"]], [["Jacque", "type", "dog", "+"]]),
        ([["Jacque", "type", "cat", "+"]], [["Jacque", "type", "cat", "-"]]),
        ([["NEBULA-SERPENT", "port", "1", "+"]], [["nebula serpent", "port", "2", "+"]]),
        ([["nebula_serpent", "port", "1", "+"]], [["nebula.serpent", "port", "1", "+"]]),
        ([[" nebula/serpent ", "Port", "1", "+"]], [["--nebula--serpent--", "port", "2", "+"]]),
        ([["a", "b", " Cat ", "+"]], [["a", "b", "cat", "+"]]),
        ([["a", "b", "Stra\u00dfe", "+"]], [["a", "b", "STRASSE", "+"]]),
        ([["a", "b", "x", "+"]], [["a", "b", "y", "+"], ["a", "b", "x", "+"]]),
        ([["a", "b", "y", "+"]], [["a", "b", "y", "+"], ["a", "b", "x", "+"]]),
        ([["a", "b", "x", "+"]], [["a", "c", "y", "+"]]),
        ([["a", "b", "x", "+"]], []),
        ([], [["a", "b", "x", "+"]]),
        ([["\u03a3\u0399\u03a3\u03a5\u03a6\u039f\u03a3", "b", "1", "+"]],
         [["\u03c3\u03b9\u03c3\u03c5\u03c6\u03bf\u03c2", "b", "2", "+"]]),
        ([["\u0130", "b", "1", "+"]], [["i\u0307", "b", "2", "+"]]),
        ([["\u212a", "b", "1", "+"]], [["k", "b", "2", "+"]]),
        ([["\ufb01le", "b", "1", "+"]], [["file", "b", "2", "+"]]),
        ([["\u1c90\u1c91", "b", "1", "+"]], [["\u10d0\u10d1", "b", "2", "+"]]),
        ([["a\u00a0b", "c", "1", "+"]], [["a-b", "c", "2", "+"]]),
        ([["a\u3000b", "c", "1", "+"]], [["a b", "c", "2", "+"]]),
        ([["a\x1cb", "c", "1", "+"]], [["a-b", "c", "2", "+"]]),
        ([["\x1ca", "c", "1", "+"]], [["a", "c", "2", "+"]]),
        ([["a", "c", "\x1c1\x1f", "+"]], [["a", "c", "1", "+"]]),
        ([["-a-", "c", "1", "+"]], [["a", "c", "2", "+"]]),
        ([["a", "c", "1", "+"], ["d", "e", "1", "+"]], [["D", "E", "2", "+"]]),
        ([["user", "port", "8080", "+"]], [["user", "port", "8081", "+"]]),
        ([["\u13a0", "b", "1", "+"]], [["\uab70", "b", "2", "+"]]),
        ([["\u24b6", "b", "1", "+"]], [["\u24d0", "b", "2", "+"]]),
    ]
    for new_slots, entry_slots in slot_pairs:
        out.append(contradiction_case("alpha", "omega", new_slots=new_slots,
                                      entry_slots=entry_slots, origin="slots"))
    for i, c in enumerate(list(out)):
        if i % 7 == 0:
            out.append(contradiction_case(c["new"], c["entry"], new_slots=c["new_slots"],
                                          entry_slots=c["entry_slots"], superseded=True,
                                          origin="superseded"))
    out.append(contradiction_case("Jacque gave him away", "I have a cat named Jacque",
                                  origin="superseded", superseded=True))
    return out


NORM_STRINGS = [
    "", " ", "Jacque", "JACQUE", "NEBULA-SERPENT", "nebula serpent", "nebula_serpent",
    "nebula.serpent", "nebula/serpent", " --a--b-- ", "a . _ - / b", "Stra\u00dfe",
    "STRASSE", "\u03a3\u0399\u03a3\u03a5\u03a6\u039f\u03a3", "\u03c3\u03b9\u03c3\u03c5\u03c6\u03bf\u03c2",
    "\u0130stanbul", "\u0131", "\u212a", "\u017f", "\ufb01le", "\u1c90\u1c91", "\u13a0\u13a1",
    "\uab70", "\u24b6", "\u0345", "a\u00a0b", "a\u3000b", "a\x1cb", "\x1ca\x1f", "a\u2028b",
    "a\x85b", "a\tb\nc", "-", "--", "a-", "-a", "\u00df", "\u1e9e", "\u0149", "\u01c4",
    "\u01c5", "\u1f88", "Caf\u00e9", "Cafe\u0301", "\U00010400", "\U0001e900", "\U00016e40",
    "\u2c2f", "\ua7c0", "\ua7d0", "\U00010570", "\u10a0", "\u2d00", "user", "Port Number",
]


def norm_cases() -> list[dict]:
    return [{"s": s, "key": _norm_key(s), "value": _norm_value(s)}
            for s in dict.fromkeys(NORM_STRINGS)]


def main() -> int:
    texts = docs_texts()
    if "--full" in sys.argv:
        path = Path(sys.argv[sys.argv.index("--full") + 1])
        full = {"slots": [slot_case(t, None, "docs") for t in texts]}
        path.write_text(json.dumps(full, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
        print(f"wrote {len(full['slots'])} docs cases to {path}")
        return 0
    golden = {
        "source": "pseudolife_memory.memory.slots, .contradiction, .cortex",
        "classes": classes(),
        "slots": slot_cases() + docs_cases(texts),
        # The float32 cosine torch computes for each case's entry, in order.
        "contradiction_sims": [EMBEDDINGS[t][1] for t in SIM_TARGETS],
        "contradiction": contradiction_cases(),
        "norm": norm_cases(),
    }
    TARGET.write_text(json.dumps(golden, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    n_pairs = len(golden["contradiction"])
    print(f"wrote {TARGET.name}: {len(golden['slots'])} slot cases, "
          f"{n_pairs} contradiction pairs x {len(SIM_TARGETS)} sims, "
          f"{len(golden['norm'])} norm cases, {TARGET.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
