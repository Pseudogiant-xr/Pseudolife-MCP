"""The write-time label pair ``{authority, distortion_tolerance}`` (schema v35).

Two papers name the same failure from two sides. Authority collapse
(arXiv 2608.01679): consolidation preserves a CLAIM while erasing the
source constraints governing its authorized use, so a third party's
offhand remark reads back as a standing user instruction; the remedy the
paper validates is an extra persisted label carried through consolidation
(unauthorized actions 16.9% -> 0.0%), not an architecture change. The
compaction cliff (arXiv 2608.22752): context management is type-blind — a
safety rule and an episodic log are summarised at the same rate, but only
the rule needs exact wording — and the fix is to classify each item by
DISTORTION TOLERANCE and route each class through its own policy.

Both labels are set at write time and carried through supersession:

``authority`` — a SPEECH-ACT axis, deliberately orthogonal to ``origin``.
  ``origin`` (user / action / agent) says WHO wrote and is a *tier* that
  drives supersession arithmetic across the engine (the provenance guard,
  the two-man rule, contender promotion); adding values to it would change
  that arithmetic, and directive-vs-observation is not a tier ordering at
  all. Entries do not even persist ``origin`` — the two-man rule derives an
  entry's tier from ``source``. So ``authority`` records HOW the text
  speaks: ``directive`` (an instruction to the agent), ``observation`` (a
  plain statement — the default, stored as NULL), ``quoted`` (reported
  speech: a document, a paper, a third person). The composite reading the
  paper calls authority is the pair ``(origin, authority)``.

``distortion_tolerance`` — the paper's five classes: ``constraint`` (zero
  tolerance — must survive verbatim; pinned ahead of cosine in recall),
  ``procedural`` (semantic equivalence iff execution preserved),
  ``belief`` (bounded semantic distance), ``preference`` (mergeable),
  ``episodic`` (discardable). NULL = unlabelled, which every consumer
  treats as today's behaviour.

The ``auto`` default is a DETERMINISTIC form heuristic, never a model
call — the store path stays fast, and a label derived from the writer's
own text is not steerable by anything but that text. It is conservative
by construction: measured against the live bank on 2026-09-03 (869
current entries, 5,435 current facts; fact hits scored against the
2026-09-02 hand verdicts, so a hit added since carries no verdict and
counts as not genuine — the precision is a floor), the shipped rule
fires on 86 facts (1.6%) of which 73 read as a genuine rule (0.85) — the
strong-deontic part (must / shall / forbidden / mandatory / rule:
framing) 62 of 75, the imperative-opener increment (Never / Always / Do
not + a bare verb) 11 of 11 — and on 1 of 869 entries. On chat-style
text (the chip-5 BEAM bank, 1,099 facts, every hit hand-judged) it fires
on 8 values, all 8 standing instructions to the assistant. The same
words used descriptively mid-sentence ("never instantiated", "always
falls back") scored 0.53 and are excluded, as is the bare "no X" opener,
any attribute-name signal, ``must`` as a noun or adjective ("a must-read
series", "materials are a must" — the 2026-09-03 fix; both chip-5 BEAM
false positives were this form) and an irregular past form after an
opener ("never paid off"). Under ``auto`` only ``constraint`` is ever
asserted — it is the one value with a consumer in this change; the other
four are accepted explicitly and carried. The numbers live in
evals/results/label-heuristic-audit-20260903.json and
label-heuristic-audit-20260903-beam-chip5.json; the 2026-09-02 pre-fix
audit (label-heuristic-audit-20260902.json) stays beside them.
"""
from __future__ import annotations

import re
from typing import Final, Iterable

AUTHORITY_VALUES: Final = ("directive", "observation", "quoted")
DISTORTION_VALUES: Final = ("constraint", "procedural", "belief",
                            "preference", "episodic")
AUTO: Final = "auto"

# Rule-sized. The live bank's entries are narratives (median well above
# this), and 36% of them carry a deontic word somewhere; only 0.2% clear
# this length gate. A reader pinned verbatim on a 600-char status note is
# the compaction cliff inverted, so the gate is load-bearing.
AUTO_MAX_CHARS: Final = 400


class _Inherit:
    """Sentinel: "carry whatever the record I land on already has". The
    dream passes it when its source entry is unlabelled; ``None`` means an
    explicit clear (the rollback needs that), so the two cannot share a
    value."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "INHERIT"


INHERIT: Final = _Inherit()

# "must" only as the deontic verb. Preceded by an article ("a must",
# "the must") it is a noun, and hyphenated ("must-read", "must-have") an
# adjective; neither is a rule. Measured 2026-09-03: both constraint fires
# on the chip-5 BEAM bank were this form ("a must-read series", "materials
# are a must"), and the one live-bank hit of the form ("agent-must-invoke")
# had been hand-judged not a rule, so the exclusion removes only false
# positives on both corpora (evals/results/label-heuristic-audit-20260903*.json).
# Known limit, deliberately not chased: the article check is fixed-width,
# so "an absolute must" (an adjective between) still reads as the verb;
# neither corpus carries that form, and a wider check would have to
# special-case "the daemon must be restarted", the canonical positive.
_STRONG = re.compile(
    r"\b((?<!\ba\s)(?<!\ban\s)(?<!\bthe\s)must(?!-)(?:\s+not|n't)?|"
    r"shall(?:\s+not)?|forbidden|prohibited|"
    r"non-negotiable|no exceptions|under no circumstances|hard rule|"
    r"standing (?:rule|instruction)|mandatory)\b", re.I)
_FRAMING = re.compile(
    r"(?:^|\n)\W*(?:rule|constraint|policy|invariant|hard rule|"
    r"standing rule|non-negotiable|mandatory)\s*[:\-—]", re.I)
# The imperative openers, followed by a bare-infinitive verb. A participle
# ("never instantiated"), a third-person form ("never calls") or an
# auxiliary ("never been") is description, not instruction.
# A leading bracketed stamp ("[2026-09-02] Never run …") is tolerated;
# any other prefix makes it a sentence about a rule, not a rule.
_IMPERATIVE_OPENER = re.compile(
    r"^\W*(?:\[[^\]]*\]\s*)?(?:never|always|do not|don't|must|you must)\s+"
    r"([A-Za-z][A-Za-z\-]*)", re.I)
_NOT_BARE = frozenset({
    "be", "been", "being", "was", "were", "is", "are", "am", "had", "has",
    "have", "did", "does", "a", "an", "the", "to", "of", "in", "on",
    "at", "for", "with", "before", "after", "again", "available", "this",
    "that", "these", "those", "it", "its", "my", "your", "our", "their",
    "any", "all", "one", "once", "ever", "yet", "quite", "fully", "really",
    "actually", "just", "only", "even", "also", "not", "resident",
    "visible", "reached", "gone", "true", "false", "up", "out",
})
# Irregular past forms the -ed test cannot see ("never paid off", "always
# ran the suite"): a report of what happened, not an instruction. Only
# forms that are never a bare imperative are listed; put / read / run /
# set / cut / hit / let stay bare because "Never put ..." is a rule, and
# so does found ("Never found a company without a co-founder").
# Measured 2026-09-03: one live-bank opener hit ("ran", hand-judged not a
# rule) and one chip-5 BEAM value ("never paid off any personal loans").
_IRREGULAR_PAST = frozenset({
    "ate", "arose", "awoke", "became", "began", "bought", "broke",
    "brought", "built", "came", "caught", "chose", "dealt", "did", "done",
    "drank", "drew", "drove", "fed", "fell", "felt", "flew", "forgave",
    "forgot", "fought", "froze", "gave", "got", "gotten", "grew",
    "held", "kept", "knew", "laid", "led", "left", "lost", "made", "meant",
    "met", "mistook", "overcame", "paid", "ran", "rode", "rose", "said",
    "sang", "sank", "sat", "shook", "shrank", "slept", "slid", "sold",
    "sought", "spent", "spoke", "stole", "stood", "struck", "stuck",
    "swam", "taught", "threw", "thought", "told", "took", "tore",
    "understood", "undertook", "went", "withdrew", "woke", "won", "wore",
    "wrote",
})
_FIRST_PERSON_HABIT = re.compile(
    r"\bi (?:always|never|usually|prefer|like|tend to)\b", re.I)
# Reported speech: an explicit reporting construction whose subject is a
# document or a third person. Bare quotation marks are NOT a trigger — on
# the live bank they wrap error strings, session titles and log lines, none
# of which is someone else speaking.
_REPORTED = re.compile(
    r"\b(?:according to|as per|per the (?!usual\b|plan\b|schedule\b|above\b|below\b)|"
    r"(?:the\s+)?(?:paper|docs?|documentation|spec|runbook|readme|article|"
    r"guide|manual|page|post|thread|vendor|upstream|authors?|he|she|they|"
    r"someone|colleague|friend)\s+"
    r"(?:said|says|state[sd]|prescribes?|recommends?|reports?|claims?|"
    r"argues?|writes?|wrote|told|asked|mentioned|notes?|suggests?|"
    r"advises?|warns?))\b", re.I)
_ADDRESSED = re.compile(
    r"^\W*(?:please|you must)\b|"
    r"\byou (?:must|should|need to|may not|are not allowed to)\b", re.I)


def _bare_verb(word: str) -> bool:
    w = word.lower()
    if w in _NOT_BARE or w in _IRREGULAR_PAST:
        return False
    if w.endswith(("ed", "en", "ing", "ly")):
        return False
    if w.endswith("s") and not w.endswith("ss"):
        return False
    return True


def _opener_imperative(text: str) -> bool:
    m = _IMPERATIVE_OPENER.match(text)
    return bool(m) and _bare_verb(m.group(1))


def infer_distortion_tolerance(text: str | None, *,
                               max_chars: int = AUTO_MAX_CHARS) -> str | None:
    """``"constraint"`` when ``text`` is a rule-sized deontic or imperative
    statement; ``None`` otherwise. Never asserts any other class."""
    t = (text or "").strip()
    if not t or len(t) > max_chars:
        return None
    if _FIRST_PERSON_HABIT.search(t):
        return None          # a habit is a preference, not a rule
    if _STRONG.search(t) or _FRAMING.search(t) or _opener_imperative(t):
        return "constraint"
    return None


def infer_authority(text: str | None, *,
                    max_chars: int = AUTO_MAX_CHARS) -> str | None:
    """``"quoted"`` on an explicit reporting construction (it wins: a quoted
    rule is still someone else's rule), ``"directive"`` on an instruction
    addressed to the reader, else ``None`` (observation).

    Same rule-sized gate as the distortion heuristic, for the same
    reason with a sharper edge: ``quoted`` demotes under the two-man rule
    and is inherited by later corrections, so a 2,000-char status note
    that mentions "per the docs" once must not become reported speech as
    a whole. Ungated it fired on 26/836 live entries; gated, see the audit
    artifact (evals/results/label-heuristic-audit-20260902.json)."""
    t = (text or "").strip()
    if not t or len(t) > max_chars:
        return None
    if _REPORTED.search(t):
        return "quoted"
    if _ADDRESSED.search(t) or _FRAMING.search(t) or _opener_imperative(t):
        return "directive"
    return None


def _normalize(value, vocabulary, name: str) -> str | None:
    if value is None:
        return None
    v = str(value).strip().casefold()
    if v not in vocabulary:
        raise ValueError(
            f"{name} must be one of {list(vocabulary)} (or None), got {value!r}")
    return v


def normalize_authority(value) -> str | None:
    return _normalize(value, AUTHORITY_VALUES, "authority")


def normalize_distortion(value) -> str | None:
    return _normalize(value, DISTORTION_VALUES, "distortion_tolerance")


def resolve_authority(requested, text: str | None):
    """Turn a caller's ``authority`` argument into what the store receives:
    ``INHERIT`` passes through; ``"auto"`` infers from ``text`` and falls
    back to ``INHERIT``; anything else is validated (``None`` = clear)."""
    if requested is INHERIT:
        return INHERIT
    if requested == AUTO:
        return infer_authority(text) or INHERIT
    return normalize_authority(requested)


def resolve_distortion(requested, text: str | None):
    if requested is INHERIT:
        return INHERIT
    if requested == AUTO:
        return infer_distortion_tolerance(text) or INHERIT
    return normalize_distortion(requested)


_AUTHORITY_RANK = {"quoted": 3, "directive": 2, "observation": 1}
_DISTORTION_RANK = {"constraint": 5, "procedural": 4, "belief": 3,
                    "preference": 2, "episodic": 1}


def _strictest(values: Iterable[str | None], rank: dict) -> str | None:
    best, best_rank = None, 0
    for v in values:
        r = rank.get(v or "", 0)
        if r > best_rank:
            best, best_rank = v, r
    return best


def strictest_authority(values: Iterable[str | None]) -> str | None:
    """For a consolidation over several parents: never UPGRADE — reported
    speech beats a directive beats an observation."""
    return _strictest(values, _AUTHORITY_RANK)


def strictest_distortion(values: Iterable[str | None]) -> str | None:
    """The paper's fidelity ladder; a constraint anywhere in the cluster
    makes the consolidated entry a constraint (TypeDecompose replicates
    in-scope rules into every partition rather than losing them)."""
    return _strictest(values, _DISTORTION_RANK)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_TOKEN_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into",
    "are", "was", "were", "has", "have", "not", "any", "all",
    "you", "your", "its", "our", "their", "than", "then", "when",
    "never", "always", "must", "should", "never", "do", "don", "t",
})


def content_tokens(text: str | None) -> frozenset[str]:
    """Casefolded alphanumeric tokens of >= 3 chars minus a small stop
    list (deontic markers included — every rule has them, so they carry
    no information about WHICH claim restates the rule). The carrier's
    overlap measure; deterministic, no model."""
    return frozenset(t for t in (m.lower() for m in _TOKEN_RE.findall(text or ""))
                     if len(t) >= 3 and t not in _TOKEN_STOP)


def _collapse(s: str | None) -> str:
    return " ".join((s or "").split())


def contains_verbatim(haystack: str | None, needle: str | None) -> bool:
    """Whitespace-collapsed, case-preserving containment: the only
    normalisation "verbatim" tolerates is line-wrapping."""
    n = _collapse(needle)
    return bool(n) and n in _collapse(haystack)
