"""Write the golden cases for the Rust ports of ``memory/labels.py`` and
``memory/meta_filter.py``.

Every expected value comes from calling the real Python functions in this
checkout, so the Rust unit tests (``src/labels.rs``, ``src/meta_filter.rs``)
assert against the oracle, not against a hand-written expectation. The corpus
is the repo's own prose (``README.md`` and ``docs/**/*.md`` paragraphs, cut at
lengths around the 400-code-point gate) plus hand-written positives and
near-misses for every regex branch and the Unicode edges where Python's
``re`` and Rust's regex engines disagree by default.

Output: ``goldens/w2e-labels.json``, a sorted list of
``{"fn": name, "args": [...], "out": value}``. ``resolve_*`` outputs are
``{"inherit": true}``, ``{"ok": value-or-null}`` or ``{"err": message}``.

usage: python gen_w2e_labels_goldens.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "w2e-labels.json"

# Every code-point cut is applied to some paragraphs, cycling, so the corpus
# straddles the 400-code-point gate.
CUTS = (60, 150, 280, 399, 400, 401, 520, None)
DOC_TEXTS = 420


def doc_paragraphs() -> list[str]:
    files = [REPO / "README.md"] + sorted((REPO / "docs").rglob("*.md"))
    paras: list[str] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        paras.extend(p for p in re.split(r"\n\s*\n", text) if p.strip())
    return paras


def doc_corpus() -> list[str]:
    paras = doc_paragraphs()
    step = max(1, len(paras) // DOC_TEXTS)
    out = []
    for i, p in enumerate(paras[::step][:DOC_TEXTS]):
        cut = CUTS[i % len(CUTS)]
        out.append(p if cut is None else p[:cut])
    return out


OPENERS = ("Never", "Always", "Do not", "Don't", "Must", "You must", "never",
           "ALWAYS", "do not", "[2026-09-02] Never", "  - Never", "** Always",
           "[stamp]  always", "> you must")
BARE = ("run", "put", "read", "set", "found", "push", "commit", "pass", "use",
        "Run-time", "re-run", "x", "PASS", "kill")
SUFFIXED = ("pushed", "taken", "running", "quickly", "runs", "passes", "miss",
            "class", "boss", "Calls", "instantiated", "falls")


def label_corpus(labels) -> list[str]:
    out: list[str] = []
    words = sorted(labels._NOT_BARE | labels._IRREGULAR_PAST)
    for i, w in enumerate(words):
        opener = OPENERS[i % len(OPENERS)]
        out.append(f"{opener} {w} the daemon")
    for i, w in enumerate(BARE + SUFFIXED):
        for opener in (OPENERS[i % len(OPENERS)], OPENERS[(i + 5) % len(OPENERS)]):
            out.append(f"{opener} {w} the suite")
    out += [
        # opener shapes
        "Never  run it", "Never\trun it", "Never\nrun it", "Never\u00a0run it",
        "Never\u2028run it", "Never\x1crun it", "never-run it", "Nevermind run it",
        "Never 3 times", "Never", "never ", "[2026-09-02]Never run", "[a] [b] never run",
        "[unclosed never run", "x Never run it", "(Never run it)", "\u0301Never run",
        "\u24d0 Never run", "_Never run", "Never r\u00e9sum\u00e9", "Never \u00e9at",
        "Never run\u00e9", "Never \u212aill it", "Never \u0130s it", "Never \u0131s it",
        "Never \u017fet it", "Never pa\u017fs it", "Never \u0130t", "Never \u0131t",
        "Never It", "Never IT", "Do  not run", "Dont run", "Don\u2019t run",
        "You  must run", "you must-run", "MUST restart", "must-restart the daemon",
        "Always-on run",
        # _STRONG
        "The daemon must be restarted.", "a must", "A must-read series",
        "materials are a must", "an must go", "the must go", "xa must go",
        "a  must go", "a\u00a0must go", "a\u2028must go", "a\x1cmust go",
        "the\tmust go", "themust go", "mustn't", "must not", "must  not",
        "must\nnot", "must nothing", "must-have", "must\u2014go", "mustard",
        "MUST", "\u00e9must go", "must\u00e9", "must_", "_must", "\u00b2must",
        "must\u00b2", "\u0301must", "must\u0301", "\u24d0 must", "must\u200d",
        "an absolute must", "agent-must-invoke", "the daemon\u00a0must restart",
        "\u0130 must go", "a must must", "the must, then must",
        "shall", "shall not", "shallow water", "Shall\u00a0not", "forbidden",
        "forbiddenness", "prohibited", "Non-negotiable", "nonnegotiable",
        "No Exceptions", "no  exceptions", "under no circumstances",
        "Under No\u00a0circumstances", "hard rule", "Hard Rules", "standing rule",
        "standing instruction", "standing\u00a0rule", "standing rules",
        "Mandatory", "mandatory-ish", "mandatoryish",
        # _FRAMING
        "Rule: keep it", "rule - keep it", "Policy \u2014 keep it",
        "invariant:keep", "intro\nRule: keep", "intro\n  ** Constraint : keep",
        "intro\r\nRule: keep", "Rules: keep", "hard rule: keep",
        "standing rule - keep", "Non-negotiable: keep", "Mandatory\u2014 keep",
        "intro\u2028Rule: keep", "intro Rule: keep", "Rule\u00a0: keep",
        "Rule\x1c: keep", "Rule\n: keep", "Policy; keep", "RULE:",
        "\n\nPolicy-keep", "Invariants: none",
        # _FIRST_PERSON_HABIT
        "I always run tests", "I never push on Fridays", "I usually must",
        "I prefer tabs", "I like it", "I tend to", "I tend  to", "i always must",
        "\u0130 always must", "\u0131 never run", "Hi always must",
        "I always-on must", "I alwaysmust", "Me, I always", "I\u00a0always must",
        "I  always must",
        # _REPORTED
        "according to the docs, you must restart", "as per the spec",
        "per the spec, never run", "per the usual", "per the plan, x",
        "per the schedule", "per the above", "per the below",
        "per the usualness", "per the  spec", "per the planner says",
        "the paper says x", "docs say x", "the docs state x", "the docs stated x",
        "README says x", "he said x", "She told me", "they recommend x",
        "someone warned x", "someone warns x", "colleague mentioned x",
        "friend asked x", "upstream notes x", "authors argue x",
        "author argues x", "vendor advises x", "the guide suggests x",
        "Thread wrote x", "post writes x", "the  paper says x",
        "paper\u00a0says x", "paper\x1csays x", "papers say x", "the spec saysx",
        "the manual prescribes x", "the page claims x", "the runbook reports x",
        "documentation states x", "doc says x", "the article notes x",
        "according  to x", "As Per x", "\u00e9he said x",
        # _ADDRESSED
        "Please run", "  please, x", "Pleased to meet", "You must restart",
        "you should x", "you need to x", "you may not x",
        "you are not allowed to x", "Thank you should x", "you  should x",
        "you mustn't x", "...you must x", "you must", "Please", "\u0301please x",
        "Per the docs, you must restart.", "you shouldn't", "you need x",
        # empties and stripping
        "", " ", "\x1c\x1d\x1e\x1f", "\u00a0\u2028\u0085", "\x1cmust restart\x1f",
        "\u00a0Never run\u2028", "\u180eNever run", "\u200bNever run",
        "\u3000must go\u3000", "\x0b\x0cmust\r\n",
    ]
    # the 400-code-point gate, with multibyte padding
    for head in ("Never run ", "must ", "Rule: ", "per the docs ", "you should "):
        for n in (399, 400, 401):
            out.append(head + "\u00e9" * (n - len(head)))
            out.append(head + "\U0001f600" * (n - len(head)))
        out.append(" \x1c" + head + "\u00e9" * (400 - len(head)) + "\u2028\x1f ")
        out.append(" " + head + "\u00e9" * (401 - len(head)) + " ")
    return out


META_CORPUS = [
    "I don't have any of that saved.", "I dont have the information",
    "I don't have\nsaved", "I don\u2019t have it saved", "I donot have saved",
    "xI don't have saved", "I don't have it savedx", "\u0130 don't have it saved",
    "i DON'T HAVE ANY MEMORY", "I don't have materials",
    "not stored", "it is not stored-ish", "notstored", "not  stored",
    "not in my memory", "not in my knowledge", "not in my records",
    "not in my record", "not in my recordings",
    "no material saved", "no records about it", "no record stored",
    "no data about", "no data", "no informations saved",
    "I don't recall any", "I don't remember specific details",
    "I dont recall anything", "I don't recall\nany",
    "I have no memory of it", "I had no records regarding",
    "I have no memories of", "I have no memory", "I have  no memory of",
    "my memory doesn't", "my memory banks don't", "my neural memory do not",
    "my memory bank dont", "my memories doesnt",
    "nothing stored about", "nothings stored about", "nothing recorded on",
    "no relevant memory", "no relevant memories", "no relevant memoriesx",
    "I can't find any in my memory", "I cannot find that in my memory",
    "I cant find any in my memory", "\u0131 can't find any in my memory",
    "my knowledge does not contain", "my memory doesnt include",
    "according to my memory", "according to my memory\u00e9",
    "based on the retrieved", "based on my recalled", "based on our memory",
    "from the memory bank", "from my neural memory",
    "from the retrieved memories", "from the memory banks",
    "my TITANS memory", "my titans system", "MY TITANS SYSTEM",
    "my titanssystem", "my short-term memory", "my long-term bank",
    "my instant bank", "my reference memory", "my short term memory",
    "no relevant memory\u0301", "\u0301not stored", "not stored\u00b2",
    "\u24d0not stored", "\u200dnot stored", "\u203fnot stored",
    "not stored\u200d", "\u00b2not stored", "_not stored",
    "The deploy succeeded at 10:00.", "x", " \t ", "\x1c", "\u2028",
    "\u180e", "\u200b",
    "Line one\nI have no record of that\nline three",
]


def main() -> int:
    sys.path.insert(0, str(REPO))
    from pseudolife_memory.memory import labels, meta_filter

    docs = doc_corpus()
    crafted = label_corpus(labels)
    texts = docs + crafted + META_CORPUS
    cases: dict[str, dict] = {}

    def add(fn: str, args: list, out) -> None:
        key = json.dumps([fn, args], ensure_ascii=False)
        cases[key] = {"fn": fn, "args": args, "out": out}

    def resolved(fn, requested, text):
        try:
            v = fn(requested, text)
        except ValueError as e:
            return {"err": str(e)}
        if v is labels.INHERIT:
            return {"inherit": True}
        return {"ok": v}

    for t in texts:
        add("infer_authority", [t], labels.infer_authority(t))
        add("infer_distortion_tolerance", [t], labels.infer_distortion_tolerance(t))
        add("is_meta_statement", [t, "assistant"], meta_filter.is_meta_statement(t, "assistant"))
    for t in crafted + META_CORPUS:
        for name, fn in (("resolve_authority", labels.resolve_authority),
                         ("resolve_distortion", labels.resolve_distortion)):
            add(name, ["auto", t], resolved(fn, "auto", t))
    for t in META_CORPUS + crafted[:40]:
        for role in ("user", "User", "", "system", "user "):
            add("is_meta_statement", [t, role], meta_filter.is_meta_statement(t, role))

    requested = [
        None, "auto", "AUTO", "Auto", " auto", "auto ",
        "directive", " Directive ", "DIRECTIVE", "observation", "quoted",
        "Quoted\n", "constraint", "procedural", "belief", "preference",
        "episodic", "Episodic\t", "con\ufb06raint", "CON\ufb05RAINT",
        "ob\u017fervation", "\u212aquoted", "D\u0130RECTIVE", "direct\u0131ve",
        "\uff24\uff49\uff52\uff45\uff43\uff54\uff49\uff56\uff45",
        "\u00a0directive\u2028", "\x1cdirective\x1f", "\u180edirective",
        "\u200bdirective", "\u0085quoted\u3000", "observation.",
        "junk", "", "   ", "it's", 'say "hi"', "both ' and \"", "back\\slash",
        "new\nline", "tab\t", "cr\r", "nul\x00", "del\x7f", "fs\x1c",
        "c1\x85", "nbsp\u00a0x", "\u00e9", "\u65e5\u672c", "zw\u200bsp",
        "ls\u2028", "emoji\U0001f600", "tag\U000e0001", "nonchar\uffff",
        "max\U0010ffff", "pua\ue000", "soft\u00adhyphen", "\u0301mark",
        "quote'\\'mix\"", "'", '"', "\\", "a\x1bb", "unassigned\u0378",
    ]
    for req in requested:
        for text in ("", "You must restart the daemon.", "per the docs, x"):
            for name, fn in (("resolve_authority", labels.resolve_authority),
                             ("resolve_distortion", labels.resolve_distortion)):
                add(name, [req, text], resolved(fn, req, text))

    ordered = [cases[k] for k in sorted(cases)]
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(ordered, ensure_ascii=False, indent=1) + "\n"
    TARGET.write_text(body, encoding="utf-8", newline="\n")
    counts: dict[str, int] = {}
    for c in ordered:
        counts[c["fn"]] = counts.get(c["fn"], 0) + 1
    print(f"wrote {TARGET} ({len(body.encode('utf-8'))} bytes, {len(texts)} texts)")
    for fn in sorted(counts):
        print(f"  {fn}: {counts[fn]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
