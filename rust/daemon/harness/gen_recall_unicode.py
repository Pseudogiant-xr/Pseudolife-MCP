"""Generate the Unicode 14 tables behind the Rust port of recall's _mentions.

``memory/recall.py:_mentions`` is a ``re.IGNORECASE`` str pattern, so its
behavior rests on CPython 3.11's ``_sre`` data, not on the host Rust
compiler's Unicode version:

- ``\\w`` is ``Py_UNICODE_ISALNUM`` or ``_``; ``\\s`` is ``Py_UNICODE_ISSPACE``;
- a cased literal matches a text char whose simple lowercase
  (``_sre.unicode_tolower``) equals the literal's, or falls in the literal's
  ``re._casefix._EXTRA_CASES`` class; an uncased literal (``_sre.unicode_iscased``
  false) matches only itself;
- ``_marks_as_letters`` reads Unicode general category M.

Every table is derived from those oracle functions, and the derived rules are
re-checked against ``re`` itself before anything is written. Each digest
covers the table's behavior over every non-surrogate code point, so the Rust
unit tests recompute it from the committed tables.

usage: python gen_recall_unicode.py [--check]
"""
import argparse
import hashlib
import random
import re
import sys
import unicodedata
from pathlib import Path

import _sre
from re._casefix import _EXTRA_CASES

TARGET = Path(__file__).resolve().parents[1] / "src" / "recall_unicode14.rs"
CODES = [c for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF]
SEPARATORS = "_-/"


def is_word(c):
    return chr(c).isalnum() or c == 0x5F


def is_space(c):
    return chr(c).isspace()


def is_mark(c):
    return unicodedata.category(chr(c))[0] == "M"


def is_sep(c):
    return is_space(c) or chr(c) in SEPARATORS


def accepts(literal, c):
    """Does the IGNORECASE literal ``literal`` match text char ``c``?
    (``re/_compiler.py:_compile``, LITERAL branch.)"""
    if not _sre.unicode_iscased(literal):
        return c == literal
    lo = _sre.unicode_tolower(literal)
    return _sre.unicode_tolower(c) in (lo, *_EXTRA_CASES.get(lo, ()))


def ranges(values):
    result = []
    for value in values:
        if result and result[-1][1] + 1 == value:
            result[-1] = (result[-1][0], value)
        else:
            result.append((value, value))
    return result


def lower_runs(mappings):
    """(start, end, step, delta) runs: start, start+step, ..., end map to
    code + delta. Sorted and non-overlapping, since a step-2 run only
    continues when the code between is unmapped."""
    runs = []
    for code, lowered in mappings:
        delta = lowered - code
        if runs:
            start, end, step, run_delta = runs[-1]
            gap = code - end
            if run_delta == delta and (gap == step or (start == end and gap in (1, 2))):
                runs[-1] = (start, code, gap, delta)
                continue
        runs.append((code, code, 1, delta))
    return runs


def flag_digest(predicate):
    digest = hashlib.sha256()
    digest.update(bytes(1 if predicate(c) else 0 for c in CODES))
    return digest.hexdigest()


def lower_digest():
    digest = hashlib.sha256()
    for c in CODES:
        digest.update(c.to_bytes(4, "little"))
        digest.update(_sre.unicode_tolower(c).to_bytes(4, "little"))
    return digest.hexdigest()


def fixes_digest():
    digest = hashlib.sha256()
    for lo in sorted(_EXTRA_CASES):
        digest.update(lo.to_bytes(4, "little"))
        digest.update(len(_EXTRA_CASES[lo]).to_bytes(4, "little"))
        for k in _EXTRA_CASES[lo]:
            digest.update(k.to_bytes(4, "little"))
    return digest.hexdigest()


def verify_against_re():
    """The derived rules must be what ``re`` does, for every code point the
    classes can see and for every literal's whole case neighborhood."""
    classes = [
        (re.compile(r"\w"), is_word),
        (re.compile(r"\s"), is_space),
        (re.compile(r"[\s_\-/]", re.IGNORECASE), is_sep),
        (re.compile(r"[\w.]", re.IGNORECASE), lambda c: is_word(c) or c == 0x2E),
        (re.compile("['’]", re.IGNORECASE), lambda c: c in (0x27, 0x2019)),
    ]
    for pattern, predicate in classes:
        for c in CODES:
            if bool(pattern.match(chr(c))) != predicate(c):
                raise RuntimeError(f"{pattern.pattern!r} disagrees at U+{c:04X}")
    groups = {}
    for c in CODES:
        groups.setdefault(_sre.unicode_tolower(c), []).append(c)
    rng = random.Random(14)
    for literal in CODES:
        lo = _sre.unicode_tolower(literal)
        if not _sre.unicode_iscased(literal) and len(groups.get(literal, ())) <= 1:
            continue
        neighbors = set(groups.get(lo, ())) | set(groups.get(literal, ()))
        for k in _EXTRA_CASES.get(lo, ()):
            neighbors |= set(groups[k])
        char = chr(literal)
        for variant in char.upper() + char.lower() + char.casefold() + char.title():
            neighbors |= set(groups.get(_sre.unicode_tolower(ord(variant)), ()))
        neighbors |= set(rng.sample(CODES, 4))
        pattern = re.compile(re.escape(char), re.IGNORECASE)
        for c in neighbors:
            if bool(pattern.fullmatch(chr(c))) != accepts(literal, c):
                raise RuntimeError(f"literal U+{literal:04X} disagrees at U+{c:04X}")
    # A greedy separator run never has to give a character back: every run is
    # followed by a literal that is not a separator, and no such literal
    # accepts a separator character.
    seps = [c for c in CODES if is_sep(c)]
    for c in seps:
        if _sre.unicode_tolower(c) != c or _sre.unicode_iscased(c):
            raise RuntimeError(f"separator U+{c:04X} is cased")
    for lo, extra in _EXTRA_CASES.items():
        if any(is_sep(k) for k in (lo, *extra)):
            raise RuntimeError(f"case-fix class of U+{lo:04X} holds a separator")
    for c in CODES:
        if not is_sep(c) and is_sep(_sre.unicode_tolower(c)):
            raise RuntimeError(f"U+{c:04X} lowercases to a separator")


def generated():
    if sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0":
        raise RuntimeError("recall oracle requires CPython 3.11 with Unicode 14.0.0")
    verify_against_re()
    mappings = [(c, _sre.unicode_tolower(c)) for c in CODES if _sre.unicode_tolower(c) != c]
    lines = ["//! Generated by harness/gen_recall_unicode.py with CPython 3.11, Unicode 14.0.0.",
             "//! `_sre` data behind `memory/recall.py:_mentions`; do not edit by hand.", ""]
    digests = [("WORD", flag_digest(is_word)), ("SPACE", flag_digest(is_space)),
               ("MARK", flag_digest(is_mark)), ("CASED", flag_digest(_sre.unicode_iscased)),
               ("LOWER", lower_digest()), ("FIXES", fixes_digest())]
    for name, value in digests:
        lines.extend(["#[cfg(test)]", f'pub const {name}_SHA256: &str = "{value}";', ""])
    tables = [("WORD", "`\\w`: `str.isalnum` or `_`.", is_word),
              ("SPACE", "`\\s`: `str.isspace`.", is_space),
              ("MARK", "General category M.", is_mark),
              ("CASED", "`_sre.unicode_iscased`.", _sre.unicode_iscased)]
    for name, doc, predicate in tables:
        lines.append(f"/// {doc}")
        lines.append(f"pub const {name}: &[(u32, u32)] = &[")
        lines.extend(f"    (0x{lo:x}, 0x{hi:x})," for lo, hi in ranges(c for c in CODES if predicate(c)))
        lines.extend(["];", ""])
    lines.append("/// `_sre.unicode_tolower` as (start, end, step, delta) runs.")
    lines.append("pub const LOWER: &[(u32, u32, u32, i32)] = &[")
    lines.extend(f"    (0x{s:x}, 0x{e:x}, {step}, {delta}),"
                 for s, e, step, delta in lower_runs(mappings))
    lines.extend(["];", ""])
    lines.append("/// `re._casefix._EXTRA_CASES`: lowercase -> its other case-equivalents.")
    lines.append("pub const FIXES: &[(u32, &[u32])] = &[")
    for lo in sorted(_EXTRA_CASES):
        extra = ", ".join(f"0x{k:x}" for k in _EXTRA_CASES[lo])
        lines.append(f"    (0x{lo:x}, &[{extra}]),")
    lines.extend(["];", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = generated()
    if args.check:
        if TARGET.read_text(encoding="utf-8") != expected:
            raise SystemExit("recall Unicode14 data differs from the oracle")
        print("Recall Unicode14 data matches the oracle")
    else:
        TARGET.write_text(expected, encoding="utf-8", newline="\n")
        print(f"Generated {TARGET.name}")


if __name__ == "__main__":
    main()
