"""No worked example in an extraction prompt may be lifted from the corpus
the prompt is measured on.

The 2026-09-05 audit found the count-exclusion example in the shipped
``dream._SYSTEM_PROMPT`` — "[5] saw a Northern Flicker today, that makes 32
species at the park now" -> value "32" — is a paraphrase of LongMemEval
question ``affe2881``'s own answer turn, and "32" is that question's gold.
The rule it illustrates was written to recover that exact question (it is
one of seven ``frozen-total`` losses in ``c2op-count-census.json``) and was
gated on the 78-question slice containing it. Two more lifts surfaced in the
same sweep: the collection-membership example's value "road bike" is the
gold of ``89941a94`` and ``gpt4_e414231f``, and ``events_pass_v2``'s jam
example is question ``2b8f3739``'s answer turn with one word removed.

Two predicates, because each catches what the other cannot:

* **values vs gold** — every ``"value"`` / ``"description"`` string a worked
  example emits, checked case-insensitively against every gold answer. Finds
  "32" and "road bike"; blind to the jam example, whose value ($225) is a
  summand of the gold ($495), not the gold.
* **shingles vs corpus** — every word 4-gram of every bracketed example note,
  checked against both dataset files. Finds the Flicker and jam sentences;
  blind to "road bike", a phrase the corpus writes 625 different ways.

Both are asserted as EQUALITY against a dated allowlist keyed by (carrier,
example), so recorded debt can neither grow (a new lift fails) nor rot (an
entry that stops hitting must be deleted). The debt is recorded rather than
paid here: the shipped prompt is byte-pinned to a measured artifact
(``test_op_prompt_artifact.py``), so re-cutting it is a prompt change with
its own ladder gate, and the archived ``ku_op_prompt_v*`` variants are the
committed arms of past gates and cannot be edited at all.

Carriers are the live ``dream.py`` constants plus every file under
``evals/prompts``; the Titlecase-only name scan on the shim-prompt branch
misses both lowercase values and paraphrased prose, which is why this guard
exists as well as that one.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROMPTS = _REPO / "evals" / "prompts"
_DATA = _REPO / "evals" / "data"


# ── carriers ─────────────────────────────────────────────────────────────

def _carriers() -> dict[str, str]:
    """Every prompt text an extractor can be handed, by a stable name.

    ``.md`` files are split the way ``claude_shim.main`` splits them: the
    text above ``\\n---\\n`` is provenance documentation the model never
    sees, and scanning it would report this repo's own prose."""
    from pseudolife_memory.memory import dream

    out = {
        "dream._SYSTEM_PROMPT": dream._SYSTEM_PROMPT,
        "dream._EVENTS_SYSTEM_PROMPT": dream._EVENTS_SYSTEM_PROMPT,
    }
    for p in sorted(_PROMPTS.glob("*.md")):
        out[p.name] = p.read_text(encoding="utf-8").split("\n---\n", 1)[-1]
    for p in sorted(_PROMPTS.glob("*.txt")):
        out[p.name] = p.read_text(encoding="utf-8")
    return out


# ── extraction ───────────────────────────────────────────────────────────

# A worked-example note: "[N] text", optionally prefixed by a corpus-style
# "[date] user:" stamp, running to the next note, the Output block, the
# em-dash / "yields" that introduces the expected claim, or end of text.
_NOTE = re.compile(
    r"\[\d+\]\s*(?:\[[^\]]*\]\s*(?:user|assistant):\s*)?(.+?)"
    r"(?=\s*\[\d+\]|\s*Output|\s*—|\s*-\s+yields|\s*yields|$)")
_VALUE = re.compile(r'"(?:value|description)"\s*:\s*"([^"]+)"')

# Tokens that carry no lift signal on their own. A shingle needs two tokens
# NOT in this set (and either 3+ letters or a digit) before it is checked,
# so ordinary connective English — "i think we'll" hit 31 times in the
# corpus on the first cut — cannot fail the guard.
_STOP = frozenset("""
a an the and or of to in on at for we i it is are was my our you your that
this he she they ll re ve s not no now too so by with from as be been do did
has have had will its us me them their then than but if up out
""".split())
_K = 4


def _norm(text: str) -> list[str]:
    text = text.lower().replace("’", "'").replace("'", "")
    return re.sub(r"[^a-z0-9$ ]+", " ", text).split()


def _notes(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    return [m.group(1).strip() for m in _NOTE.finditer(flat)]


def _note_key(note: str) -> str:
    return " ".join(_norm(note)[:6])


def _content(tok: str) -> bool:
    return tok not in _STOP and (len(tok) >= 3 or any(c.isdigit() or c == "$" for c in tok))


def _shingles(note: str) -> set[str]:
    w = _norm(note)
    out = set()
    for i in range(len(w) - _K + 1):
        gram = w[i:i + _K]
        if sum(map(_content, gram)) >= 2:
            out.add(" ".join(gram))
    return out


def _example_values(text: str) -> set[str]:
    return {v.strip() for v in _VALUE.findall(text) if v.strip()}


# ── the recorded debt ────────────────────────────────────────────────────
#
# Keyed (carrier, example). Every entry below was verified against both
# dataset files on 2026-09-05; the guards hold these as equalities.

# The count-exclusion block shipped 2026-08-01 (a4686df6) into the live
# prompt and, in the same commit, into the v1/v2 shim files; v3 and the
# v5..v10 op-prompt artifacts were cut from it.
_COUNT_RULE_CARRIERS = (
    "dream._SYSTEM_PROMPT",
    "sonnet_extractor_v1.md", "sonnet_extractor_v2.md", "sonnet_extractor_v3.md",
    "ku_op_prompt_v5.txt", "ku_op_prompt_v6.txt", "ku_op_prompt_v7_events.txt",
    "ku_op_prompt_v8_stance.txt", "ku_op_prompt_v9_stance_quote.txt",
    "ku_op_prompt_v10_stance_update.txt",
)
# The op block (collection membership) predates the count rule by a day
# (e4776729, 2026-07-31) and is also in the v0 artifact.
_OP_BLOCK_CARRIERS = _COUNT_RULE_CARRIERS + ("ku_op_prompt_v0.txt",)

_FLICKER = "saw a northern flicker today that"
_JAM = "i sold 15 jars of jam"

KNOWN_GOLD_COLLISIONS: dict[tuple[str, str], str] = {
    **{(c, "32"): "LongMemEval affe2881 (knowledge-update): gold '32'; the "
                  "example states the same number. Recorded 2026-09-05."
       for c in _COUNT_RULE_CARRIERS},
    **{(c, "road bike"): "LongMemEval 89941a94 (knowledge-update, gold 'Yes. "
                         "(You have a road bike too.)') and gpt4_e414231f "
                         "(temporal-reasoning, gold 'road bike'). Recorded "
                         "2026-09-05."
       for c in _OP_BLOCK_CARRIERS},
}

KNOWN_CORPUS_LIFTS: dict[tuple[str, str], str] = {
    **{(c, _FLICKER): "paraphrases affe2881's answer turn ('I just saw a "
                      "Northern Flicker in my local park last weekend, which "
                      "brings my total species count to 32'). Recorded "
                      "2026-09-05."
       for c in _COUNT_RULE_CARRIERS},
    ("events_pass_v2.txt", _JAM):
        "paraphrases 2b8f3739's answer turn ('I just sold 15 jars of my "
        "homemade jam at the Homemade and Handmade Market on May 29th, "
        "earning $225'); gold is the multi-session total $495. The v2 events "
        "prompt is unshipped but was the teacher prompt for "
        "distill-events-opus1.jsonl (e4b-v3). Recorded 2026-09-05.",
}


# ── structural half: runs with or without the datasets ───────────────────

def test_the_scan_finds_examples_in_every_prompt_family():
    """Non-vacuity. A regex drift that stopped seeing notes or values would
    make both dataset tests pass on anything."""
    c = _carriers()
    for name in _OP_BLOCK_CARRIERS + ("dream._EVENTS_SYSTEM_PROMPT",
                                      "events_pass_v1.txt", "events_pass_v2.txt"):
        assert _notes(c[name]), f"no worked-example note found in {name}"
        assert _example_values(c[name]), f"no example value found in {name}"
    assert any(_shingles(n) for n in _notes(c["dream._SYSTEM_PROMPT"]))


def test_the_deployed_shim_default_is_a_scanned_carrier():
    """The maintainer's install extracts through the CLI shim, whose
    ``--system-prompt-file`` REPLACES the shipped prompt. Whatever the
    launcher defaults to must be in scope or the guard misses production."""
    launcher = (_REPO / "ops" / "install-shim-autostart.ps1").read_text(encoding="utf-8")
    named = set(re.findall(r"sonnet_extractor_v\d+\.md", launcher))
    assert named, "launcher names no shim prompt file"
    missing = sorted(named - set(_carriers()))
    assert missing == [], f"shim default(s) not under evals/prompts: {missing}"


def test_every_allowlisted_example_is_really_in_its_carrier():
    """The allowlists must describe the carriers, not park inconvenient
    strings. Each (carrier, example) has to be present as stated."""
    c = _carriers()
    for (name, value) in KNOWN_GOLD_COLLISIONS:
        assert value in _example_values(c[name]), (
            f"allowlisted value {value!r} is not an example value in {name}")
    for (name, key) in KNOWN_CORPUS_LIFTS:
        assert key in {_note_key(n) for n in _notes(c[name])}, (
            f"allowlisted note {key!r} is not a worked example in {name}")


def test_the_stop_list_cannot_hide_a_lift():
    """``_STOP`` is the one list that removes tokens from the shingle scan,
    so it is where a name could be parked to silence a hit ("may", "bee").
    Every entry must look like a function word, and none may appear
    capitalised inside a worked-example note after its first word — that is
    how the notes write names."""
    for tok in _STOP:
        assert len(tok) <= 5 and tok.isalpha(), f"{tok!r} is not a function word"
    for name, text in _carriers().items():
        for note in _notes(text):
            for word in note.split()[1:]:
                bare = re.sub(r"[^A-Za-z]", "", word)
                assert not (bare[:1].isupper() and bare.lower() in _STOP), (
                    f"{bare!r} is capitalised mid-note in {name} yet listed in "
                    "_STOP — a name would be dropped from the scan")


# ── evidence half: needs the datasets (evals/data is gitignored) ─────────

def _golds():
    path = _DATA / "longmemeval_oracle.json"
    if not path.exists():
        pytest.skip("longmemeval_oracle.json not present (evals/data is gitignored)")
    rows = json.loads(path.read_text(encoding="utf-8"))
    # Both dataset files carry the same 500 questions and identical golds
    # (verified 2026-09-05), so the 15 MB file is the whole gold set.
    return [(r["question_id"], r["question_type"], str(r.get("answer", ""))) for r in rows]


def test_no_example_value_is_a_gold_answer():
    golds = _golds()
    found: dict[tuple[str, str], list[str]] = {}
    for name, text in _carriers().items():
        for v in _example_values(text):
            vl = v.lower()
            qids = [q for q, _, g in golds
                    if g and (vl == g.lower().strip() or (len(vl) >= 4 and vl in g.lower()))]
            if qids:
                found[(name, v)] = sorted(qids)
    assert sorted(found) == sorted(KNOWN_GOLD_COLLISIONS), (
        "worked-example values that are LongMemEval gold answers:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in sorted(found.items()))
        + "\nknown and allowed: " + str(sorted(KNOWN_GOLD_COLLISIONS))
        + "\nA new entry means an example states a benchmark answer — re-cut "
          "it on an invented value. A missing entry means the debt is paid: "
          "delete it.")


@pytest.mark.parametrize("dataset", ["longmemeval_oracle.json",
                                     "longmemeval_s_cleaned.json"])
def test_no_example_note_is_lifted_from_the_corpus(dataset):
    """Streamed with an overlap — the ``_s`` file is ~277 MB. Shingles are
    matched space-padded against the normalised stream so a gram cannot
    match inside a longer word."""
    path = _DATA / dataset
    if not path.exists():
        pytest.skip(f"{dataset} not present (evals/data is gitignored)")

    owners: dict[str, set[tuple[str, str]]] = {}
    for name, text in _carriers().items():
        for note in _notes(text):
            for s in _shingles(note):
                owners.setdefault(s, set()).add((name, _note_key(note)))
    assert owners, "no shingle produced from any carrier — the scan has degenerated"

    hit: set[str] = set()
    probes = [f" {s} " for s in owners]
    prev = ""
    with path.open(encoding="utf-8") as fh:
        while True:
            chunk = fh.read(8 << 20)
            if not chunk:
                break
            buf = " " + " ".join(_norm(prev + chunk)) + " "
            hit.update(s.strip() for s in probes if s in buf)
            prev = (prev + chunk)[-400:]

    found = sorted({owner for s in hit for owner in owners[s]})
    assert found == sorted(KNOWN_CORPUS_LIFTS), (
        f"worked-example notes with 4-gram overlap in {dataset}:\n  "
        + "\n  ".join(f"{o}  <- {sorted(s for s in hit if o in owners[s])}"
                      for o in found)
        + "\nknown and allowed: " + str(sorted(KNOWN_CORPUS_LIFTS))
        + "\nA new entry means an example was written from the corpus — "
          "re-cut it on invented content. A missing entry means the debt is "
          "paid: delete it.")
