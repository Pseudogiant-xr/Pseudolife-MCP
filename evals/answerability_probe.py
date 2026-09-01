"""Memory-only answerability probe + pathway evidence over a judged artifact.

AWM (arXiv 2608.25618) removed the source context and asked whether each
question could be answered from the agent's terminal memory ALONE — and
found 42.5% of CORRECT answers could not be reproduced from memory alone:
the agent was right while its notes were too thin to support the answer
later. That failure is invisible to end-to-end QA, and this stack is
structurally exposed to it — dream claims and digests are written while
the full session is still in context.

PAST-Bench (arXiv 2608.04003) asks the per-row sibling: does a correct
answer's gain actually follow the save → retrieve → use pathway? Here
that becomes: WHICH served context entries contain the gold?

This checker reads a committed artifact (BEAM ``*_score`` rows or
LongMemEval ``*_correct`` rows) and reports, per arm:

* **answerability** — is the gold answer contained in the arm's persisted
  context? Not leak_check's whole-gold verbatim containment: that is a
  leak test, and as an answerability test it marks a context that plainly
  says ``25:50`` unanswerable for the gold ``25 minutes and 50 seconds
  (or 25:50)`` — inflating the red-flag cell with surface mismatches
  instead of missing memory. Instead a two-step ladder, the method
  recorded per row: ``span`` (some gold variant — the full gold, or a
  parenthetical alternative — appears as a contiguous normalized token
  sequence, with number words and plural-s folded), then ``tokens``
  (every content token of the gold appears somewhere in the context —
  the coverage reading a sentence-shaped BEAM gold needs, since the
  information can be present while the sentence never is). Crossed with the
  arm's verdict this gives four cells: ``answerable_correct``,
  ``answerable_wrong`` (an answering failure), ``unanswerable_wrong`` (a
  storage/retrieval failure) and ``unanswerable_correct`` — the AWM red
  flag, a right answer with no memory support. One way that cell fills is
  the question naming its own gold (the SR-TTT leak), so the recorded
  ``gold_in_question`` flag is reconciled within it
  (``red_flag_leak_explained``).
* **pathway evidence** — for each correct answer with a testable gold,
  the served context is split back into the entries it was composed from
  and the gold-bearing entry indices are recorded, with a verdict:
  ``supported`` (at least one served entry carries the gold),
  ``unsupported`` (none does — overlapping the red-flag cell), or
  ``spanning`` (the full context contains the gold but no single entry
  does: the gold straddles a block boundary, or the splitter over-split —
  reported apart, never miscounted as either).

Rows the probe cannot examine are classified with reasons, never skipped:
``no_gold`` / ``trivial_gold`` (leak_check's gold taxonomy — gold reasons
first, so a row that could never be tested is not blamed on its
artifact), then ``no_context`` for rows that predate context persistence
(the committed 2026-08-21 BEAM artifact is entirely such rows).

Containment is a floor, not a judge: it proves the gold string is served,
not that the question is answerable from the context. The judge-based
level ("can this question be answered from this context alone?") is
wired behind ``--judge`` — it probes the judge server up front and fails
fast (the chip-1 --refind discipline), writes a per-row, per-arm
``{arm}_answerable_judge`` verdict (registered in
``replicate.is_judge_field`` so every stripper clears it), and is
resumable. It is deliberately not run by any committed artifact here.

    python evals/answerability_probe.py --in evals/results/<artifact>.jsonl

Always writes its report (default ``<artifact>.answerability.json``); a
diagnostic, not a gate — the exit code is 0 unless the run itself fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from pathlib import Path

import leak_check
from context_format import FACTS_HEADER, MEMS_HEADER
from leak_check import (answer_present, gold_answer, load_rows, normalize,
                        row_id, untestable_reason)

# BEAM rubric scores are means over items in [0, 1]. The cross-tab needs a
# binary "correct"; full credit is the only reading under which the AWM
# red-flag cell means what it claims (a partially-credited answer is not
# "right with no memory support"). Partial rows land on the wrong side and
# are counted in ``n_partial`` so the binarization loses nothing silently.
BEAM_CORRECT_THRESHOLD = 1.0
# The per-arm judge verdict key suffix; replicate._JUDGE_SUFFIXES carries
# it so strip_judged / rebuild_contexts clear it like any other verdict.
JUDGE_SUFFIX = "answerable_judge"
CELLS = ("answerable_correct", "answerable_wrong",
         "unanswerable_correct", "unanswerable_wrong")

_JUDGE_ANSWERABLE_SYSTEM = (
    "You judge whether a question could be answered from a memory context "
    "ALONE, with no other source. Reply with exactly one word: yes or no.\n"
    "- yes if the context contains the information the question asks for "
    "(equivalence counts; exact wording is not required).\n"
    "- no if the context does not contain it, or only hints at it."
)

# ── the containment ladder ──────────────────────────────────────────────
# Spelled numbers fold to digits so "seven" matches a stored "7". MIRRORS
# pseudolife_memory.memory.dream._SPELLED_NUMBERS, which cannot be
# imported here: the package __init__ pulls torch, and this module must
# stay importable on CPU-only machines. A re-typed subset stopping at
# twenty is what shipped, so "thirty minutes" scored unanswerable against
# a context saying "30 minutes"; tests/test_answerability_probe.py
# asserts equality with dream.py's table (2026-09-01 review of PR #236).
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100", "thousand": "1000",
}


def _deplural(token: str) -> str:
    """The plural-s strip, on its own so the stopword test can reuse it."""
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _fold(token: str) -> str:
    """One normalized token, folded for comparison: plural-s stripped
    FIRST, then the spelled number mapped. The other order leaves
    "sevens" at "seven" while a bare "seven" becomes "7", so the two
    spellings of one number stop matching (2026-09-01 review)."""
    token = _deplural(token)
    return _NUMBER_WORDS.get(token, token)


def _tokens(text) -> list[str]:
    """Comparison tokens: leak_check's normalization, then the fold above
    (both sides get the same fold, so the comparison stays symmetric)."""
    return [_fold(t) for t in normalize(text).split()]


# Function words dropped for the token-coverage step. Frozen and small on
# purpose: every word here loosens what "the gold's information is
# present" requires, so additions belong in review, not in tuning.
# Stored RAW — membership is tested on the raw token and on its
# depluralized stem, never on the fully folded token (see
# _content_tokens). Folding the set first collides function words with
# real ones: "does" folds to "doe", which then excluded the genuine noun
# "doe" from the required-coverage set (2026-09-01 review of PR #236).
_STOPWORDS = frozenset(normalize("""
a about an and approximately are around as at be been by did do does
exactly for from had has have i in into is it its just my of on only or
our roughly that the their there they this to was were will with you
your
""").split())


def _content_tokens(text) -> list[str]:
    """The folded tokens of ``text`` minus its function words.

    A token is dropped when the RAW word is a function word, or when its
    depluralized STEM is — the second half matters because the folded set
    this replaced was also absorbing inflected function words ("theirs"
    folded onto "their"), and requiring those instead pushes rows into
    ``unanswerable`` and inflates the red-flag cell with the same surface
    mismatches the ladder exists to keep out of it. Neither test can
    swallow a content word, because a word is only dropped when the raw
    form or the stem is ITSELF a listed function word: "doe" and "thi"
    are their own stems and stay required (2026-09-01 reviews of PR #236
    and of this fix-pass)."""
    return [_fold(t) for t in normalize(text).split()
            if t not in _STOPWORDS and _deplural(t) not in _STOPWORDS]


def gold_variants(gold) -> list[str]:
    """The gold and its parenthetical alternatives: LongMemEval golds
    read like ``25 minutes and 50 seconds (or 25:50)`` — the paren text
    (minus a leading or/i.e./e.g.) and the gold with parens removed are
    each a full statement of the answer."""
    s = str(gold or "")
    variants = [s]
    parens = re.findall(r"\(([^)]*)\)", s)
    if parens:
        variants.append(re.sub(r"\([^)]*\)", " ", s))
        for p in parens:
            # Raw text, so the lead-in forms carry their dots: "i.e.",
            # "e.g." (with or without them) must both strip.
            variants.append(re.sub(
                r"^\s*(?:or|i\.?\s*e\.?|e\.?\s*g\.?)[.,:]?\s+", "",
                p.strip(), flags=re.IGNORECASE))
    seen: dict[tuple, str] = {}
    for v in variants:
        # Each variant re-passes the triviality screen: "Yes. (reason)"
        # is a testable gold whose parens-stripped variant is the bare
        # "yes" TRIVIAL_ANSWERS exists to exclude — unscreened, any
        # context containing that token scores answerable.
        if untestable_reason(v):
            continue
        toks = tuple(_tokens(v))
        if toks and toks not in seen:
            seen[toks] = v
    return list(seen.values())


def _seq_contained(needle: list[str], hay: list[str]) -> bool:
    n = len(needle)
    return any(hay[i:i + n] == needle
               for i in range(len(hay) - n + 1))


def _contained(hay: list[str], variants: list[list[str]],
               content: list[str]) -> str | None:
    """The ladder itself, over pre-tokenized inputs, so a caller testing
    many contexts against ONE gold folds that gold once."""
    if not hay:
        return None
    if any(_seq_contained(v, hay) for v in variants):
        return "span"
    hay_set = set(hay)
    if content and all(t in hay_set for t in content):
        return "tokens"
    return None


def _block_answers(block: str, variants: list[list[str]],
                   content: list[str]) -> bool:
    return _contained(_tokens(block), variants, content) is not None


def answerable_in(ctx, gold) -> str | None:
    """How the gold is contained in the context: ``span`` (some variant
    is a contiguous token sequence), ``tokens`` (every content token
    appears somewhere — the coverage reading for sentence-shaped golds),
    or None. The caller must have ruled the gold testable first
    (leak_check.untestable_reason)."""
    return _contained(_tokens(ctx),
                      [_tokens(v) for v in gold_variants(gold)],
                      _content_tokens(gold))


def _group_lines(text: str) -> list[str]:
    """Blocks of lines: a block starts at a non-indented line; indented
    lines (the enum fact render's two-space continuations) stay with the
    block above."""
    blocks: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0].isspace() and blocks:
            blocks[-1] += "\n" + line
        else:
            blocks.append(line)
    return blocks


def context_blocks(arm: str, ctx: str) -> list[str]:
    """The served context split back into the entries it was composed
    from, mirroring how each arm joins them (build_contexts /
    refind_search). An entry whose own text contains a joiner over-splits
    here; the ``spanning`` pathway verdict is where that imprecision
    surfaces instead of corrupting a count."""
    if not ctx:
        return []
    if arm.startswith("cortex"):
        # "\n".join(fact_lines) — one fact per non-indented line. Prefix
        # match for symmetry with the hybrid branch below: under the
        # exact "cortex" test this used to carry, any future cortex
        # VARIANT arm would fall through to the blank-line splitter and
        # collapse to a single block, silently gutting its pathway
        # attribution. No such arm exists today — the ``cortex_orig`` key
        # in the committed rejudge summary is a display key
        # (beam_rejudge.py builds ``f"{arm}_orig"``), not an arm, and
        # nothing reaches context_blocks with it — so this guards the
        # next variant rather than fixing a live miscount
        # (2026-09-01 review of PR #236, corrected in its fix-pass).
        return _group_lines(ctx)
    if arm.startswith("hybrid"):
        # The canonical hybrid join (context_format.hybrid_context), read
        # backwards (+ any appended section, e.g. hybrid_ev's events
        # block, which stays in the double-newline split).
        body = ctx.removeprefix(FACTS_HEADER)
        facts_part, sep, rest = body.partition(MEMS_HEADER)
        blocks = _group_lines(facts_part)
        if sep:
            blocks.extend(b for b in rest.split("\n\n") if b.strip())
        return blocks
    # rag, refind, and unknown arms: "\n\n".join(entry_texts).
    return [b for b in ctx.split("\n\n") if b.strip()]


def row_correct(row: dict, arm: str) -> bool | None:
    """The arm's binary verdict; None when the row is unjudged (an
    extract-phase artifact)."""
    if f"{arm}_correct" in row:
        return bool(row[f"{arm}_correct"])
    if f"{arm}_score" in row:
        return float(row[f"{arm}_score"]) >= BEAM_CORRECT_THRESHOLD
    return None


def classify(row: dict, arm: str, *, position: int = 0) -> dict:
    """One row-arm's answerability verdict. Gold reasons come first: a
    row with no testable gold is untestable however its contexts were
    persisted, and the reason split is what tells a reader how much of an
    artifact the probe could ever cover."""
    gold = gold_answer(row)
    verdict: dict = {"id": row_id(row, position), "arm": arm,
                     "testable": False, "reason": None,
                     "answerable": None, "answerable_method": None,
                     "correct": row_correct(row, arm), "cell": None}
    if arm in leak_check.CONTEXT_FREE_ARMS:
        # The no-memory arm is served nothing BY CONSTRUCTION, so every
        # correct answer of its is "unanswerable_correct" — that cell
        # would publish the memory-off arm's accuracy as an AWM red flag
        # (2026-09-01 review). leak_check owns the complementary check
        # that its context really was empty.
        verdict["reason"] = "context_free_arm"
        return verdict
    if row.get("abstention"):
        # The gold names an ABSENCE ("the information provided is not
        # enough") and correct means abstaining — a right abstention with
        # no memory support is the designed outcome, not the AWM red
        # flag, and containment cannot test an absence. (BEAM's
        # abstention rows carry no gold and already fall out as no_gold.)
        verdict["reason"] = "abstention"
        return verdict
    reason = untestable_reason(gold)
    if reason:
        verdict["reason"] = reason
        return verdict
    contexts = row.get("contexts") or {}
    if arm not in contexts:
        # Absent key = the run never persisted this arm's context (rows
        # predating persistence, or an arm this row never served). An
        # EMPTY string is different: the arm was served nothing (the
        # no-memory arm's contract) and that is testable.
        verdict["reason"] = "no_context"
        return verdict
    verdict["testable"] = True
    verdict["answerable_method"] = answerable_in(contexts[arm], gold)
    verdict["answerable"] = verdict["answerable_method"] is not None
    if verdict["correct"] is not None:
        verdict["cell"] = (
            ("answerable_" if verdict["answerable"] else "unanswerable_")
            + ("correct" if verdict["correct"] else "wrong"))
    return verdict


def _question_leak(row: dict) -> bool:
    """Did the question name its own gold? The recorded answer-time flag
    wins over a re-derivation (leak_check's rule)."""
    if leak_check.FLAG_KEY in row:
        return bool(row[leak_check.FLAG_KEY])
    return bool(answer_present(row.get("question", ""), gold_answer(row)))


def pathway(row: dict, arm: str, *, position: int = 0,
            verdict: dict | None = None) -> dict | None:
    """PAST-Bench per-row pathway evidence: which served entries carry
    the gold. Only computed for correct answers — the pathway question is
    whether a WIN is memory-supported. Returns None for wrong/unjudged
    rows and for rows whose gold or context is untestable (those are
    counted, with reasons, on the answerability side). ``verdict`` passes
    an already-computed ``classify`` result back in; probe_rows has one
    per row-arm already."""
    v = verdict if verdict is not None else classify(row, arm,
                                                     position=position)
    # A verdict for a DIFFERENT arm would be read as this arm's and then
    # index this arm's context on the strength of it — silently, so the
    # invariant is asserted rather than trusted.
    assert v["arm"] == arm, f"verdict for {v['arm']!r} passed as {arm!r}"
    if v["correct"] is not True or not v["testable"]:
        return None
    blocks = context_blocks(arm, (row.get("contexts") or {})[arm])
    gold = gold_answer(row)
    # Hoisted: gold_variants(gold) is the same list for every block.
    variants = [_tokens(x) for x in gold_variants(gold)]
    content = _content_tokens(gold)
    gold_entries = [i for i, b in enumerate(blocks)
                    if _block_answers(b, variants, content)]
    if gold_entries:
        outcome = "supported"
    elif v["answerable"]:
        # In the context as a whole but in no single entry: the answer
        # is assembled across entries, or the splitter over-split one.
        outcome = "spanning"
    else:
        outcome = "unsupported"
    return {"id": v["id"], "arm": arm, "n_context_entries": len(blocks),
            "gold_entries": gold_entries, "verdict": outcome}


def _arms(rows: list[dict]) -> list[str]:
    """Every arm the rows carry — judged fields (leak_check's derivation)
    plus persisted context keys, so an extract-phase artifact still
    probes."""
    found = set(leak_check._arms(rows))
    for row in rows:
        found.update((row.get("contexts") or {}).keys())
    return sorted(found)


def probe_rows(rows: list[dict], source: str | None = None) -> dict:
    summary: dict = {
        "check": ("memory-only answerability + pathway evidence "
                  "(AWM arXiv 2608.25618, PAST-Bench arXiv 2608.04003)"),
        "n_rows": len(rows),
        "arms": {},
        "pathway_evidence": [],
    }
    if source:
        summary["source"] = source
    arms = _arms(rows)
    if any(f"{arm}_score" in row for row in rows for arm in arms):
        summary["beam_correct_threshold"] = BEAM_CORRECT_THRESHOLD
    for arm in arms:
        verdicts = [classify(r, arm, position=i)
                    for i, r in enumerate(rows)]
        testable = [v for v in verdicts if v["testable"]]
        reasons: dict[str, int] = {}
        for v in verdicts:
            if not v["testable"]:
                reasons[v["reason"]] = reasons.get(v["reason"], 0) + 1
        cells = {c: sum(1 for v in testable if v["cell"] == c)
                 for c in CELLS}
        answerable = sum(1 for v in testable if v["answerable"])
        red_rows = [(r, v) for r, v in zip(rows, verdicts)
                    if v["cell"] == "unanswerable_correct"]
        # Over ALL score rows, testable or not — pathway.n_correct is
        # unconditioned too, and a binarization count scoped narrower
        # than the binarized count it audits would report 0 while
        # partial credit is being floored (2026-09-01 review).
        n_partial = sum(
            1 for r in rows if f"{arm}_score" in r
            and 0.0 < float(r[f"{arm}_score"]) < BEAM_CORRECT_THRESHOLD)
        evidence = [e for e in (pathway(r, arm, position=i, verdict=v)
                                for i, (r, v) in enumerate(zip(rows,
                                                               verdicts)))
                    if e is not None]
        n_correct = sum(1 for v in verdicts if v["correct"] is True)
        supported = sum(1 for e in evidence if e["verdict"] == "supported")
        arm_summary = {
            "n": len(rows),
            "n_testable": len(testable),
            "untestable_reasons": reasons,
            "n_unjudged": sum(1 for v in testable if v["cell"] is None),
            "n_partial": n_partial,
            "cells": cells,
            "answerable_share": (round(answerable / len(testable), 4)
                                 if testable else None),
            "answerable_by": {
                m: sum(1 for v in testable if v["answerable_method"] == m)
                for m in ("span", "tokens")},
            "unanswerable_correct_ids": [v["id"] for _, v in red_rows],
            "red_flag_leak_explained": sum(1 for r, _ in red_rows
                                           if _question_leak(r)),
            "pathway": {
                # correct answers the pathway question could not examine
                # (no gold / no context) — reported, never dropped.
                "n_correct": n_correct,
                "untestable": n_correct - len(evidence),
                "supported": supported,
                "unsupported": sum(1 for e in evidence
                                   if e["verdict"] == "unsupported"),
                "spanning": sum(1 for e in evidence
                                if e["verdict"] == "spanning"),
                "supported_share": (round(supported / len(evidence), 4)
                                    if evidence else None),
                "unsupported_ids": [e["id"] for e in evidence
                                    if e["verdict"] == "unsupported"],
                "spanning_ids": [e["id"] for e in evidence
                                 if e["verdict"] == "spanning"],
            },
        }
        judged = [(r, v) for r, v in zip(rows, verdicts)
                  if f"{arm}_{JUDGE_SUFFIX}" in r and v["correct"] is not None]
        if judged:
            jcells = {c: 0 for c in CELLS}
            for r, v in judged:
                jcells[("answerable_" if r[f"{arm}_{JUDGE_SUFFIX}"]
                        else "unanswerable_")
                       + ("correct" if v["correct"] else "wrong")] += 1
            arm_summary["judge"] = {"n_judged": len(judged),
                                    "cells": jcells}
        summary["arms"][arm] = arm_summary
        summary["pathway_evidence"].extend(evidence)
    return summary


def report_block(rows: list[dict]) -> dict | None:
    """The summary-sized block both harnesses hang off ``--report`` —
    the cross-tab without the per-row evidence list. None when no row
    persisted contexts, so legacy summaries stay unchanged."""
    if not any(r.get("contexts") for r in rows):
        return None
    # This block is printed BESIDE an accuracy table whose arms both
    # harnesses derive from rows[0], while _arms unions over every row. A
    # file resumed with different arm flags would publish an
    # answerability block covering arms the table omits, and say nothing
    # about it. longmemeval_bench.report() already exits on that
    # disagreement; the block must not be quieter (2026-09-01 review of
    # PR #236). probe_rows itself stays tolerant — a per-row diagnostic
    # is exactly where a mixed file should still be readable.
    arms, first = _arms(rows), _arms(rows[:1])
    if arms != first:
        raise SystemExit(
            "answerability block: the rows disagree about their arms — "
            f"{', '.join(arms)} across the file against "
            f"{', '.join(first) or '(none)'} on the first row. The file "
            "mixes runs with different arm flags, so the block would "
            "cover arms the accuracy table beside it omits. Re-run the "
            "answer phase over the whole file, or report the runs "
            "separately.")
    return {k: v for k, v in probe_rows(rows).items()
            if k != "pathway_evidence"}


# ── the judge-based level (wired; never run by the committed artifacts) ──

def _judge_url() -> str:
    return os.environ.get("PSEUDOLIFE_BENCH_QWEN_URL",
                          "http://127.0.0.1:1234/v1")


def _server_alive(url: str, timeout: float = 4.0) -> bool:
    # ladder_sweep.probe's contract, restated locally: importing
    # ladder_sweep would run its module-level CUDA_VISIBLE_DEVICES=-1
    # (ladder_sweep.py:38), a process-wide side effect this CPU-only
    # diagnostic has no business imposing on whatever imports it. (Its
    # heavy imports are lazy, so weight is NOT the reason — the previous
    # comment claimed it was; 2026-09-01 review of PR #236.)
    try:
        req = urllib.request.Request(url.rstrip("/") + "/models",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — unreachable is the answer
        return False


def _judge_chat(system: str, user: str, url: str | None = None,
                timeout: float = 600.0) -> str:
    body = json.dumps({
        "model": "bench",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": 8,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"{(url or _judge_url()).rstrip('/')}/chat/completions", data=body,
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return (data["choices"][0]["message"]["content"] or "").strip()


def annotate_judge(rows: list[dict], chat=None,
                   on_row=None) -> int:
    """Fill ``{arm}_answerable_judge`` on every testable row-arm that
    does not carry it yet (resumable — a rerun skips judged pairs).
    ``on_row`` is called after each row that gained a verdict, so the
    caller can persist incrementally. Returns how many verdicts were
    written."""
    chat = chat or _judge_chat
    written = 0
    for i, row in enumerate(rows):
        row_written = 0
        for arm in _arms([row]):
            key = f"{arm}_{JUDGE_SUFFIX}"
            if key in row:
                continue
            if not classify(row, arm, position=i)["testable"]:
                continue
            verdict = chat(_JUDGE_ANSWERABLE_SYSTEM, (
                f"Question: {row['question']}\n\n"
                f"Memory context:\n{(row.get('contexts') or {})[arm] or '(empty)'}"))
            row[key] = verdict.strip().lower().startswith("yes")
            row_written += 1
        if row_written:
            written += row_written
            if on_row:
                on_row()
    return written


def rewrite_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def default_out(path: Path) -> Path:
    return path.with_name(
        path.name.removesuffix(".jsonl").removesuffix(".json")
        + ".answerability.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="in_path", required=True,
                    help="judged artifact (.jsonl rows or a .json list)")
    ap.add_argument("--out", default=None,
                    help="report path (default: "
                         "<artifact>.answerability.json)")
    ap.add_argument("--judge", action="store_true",
                    help="also run the judge-based answerability level "
                         "('answerable from this context alone?') — "
                         "needs the judge server; annotates the rows "
                         "in place, resumably")
    ap.add_argument("--judge-url", default=None,
                    help="judge endpoint (default: "
                         "PSEUDOLIFE_BENCH_QWEN_URL or :1234)")
    args = ap.parse_args(argv)
    src = Path(args.in_path)
    rows = load_rows(src)
    if args.judge:
        if src.suffix != ".jsonl":
            # annotate_judge persists via rewrite_rows, which writes
            # JSONL — run against a .json LIST artifact it would rewrite
            # a canonical file in a different format.
            raise SystemExit("--judge annotates the input in place and "
                             "writes JSONL; give it a .jsonl artifact")
        url = args.judge_url or _judge_url()
        # Fail BEFORE any judge call: a pass that dies mid-artifact would
        # leave a half-annotated file that reads as fully judged.
        if not _server_alive(url):
            raise SystemExit(f"no judge server at {url} — start it first "
                             "(the --judge level asks it per row per arm)")
        chat = ((lambda s, u: _judge_chat(s, u, url))
                if args.judge_url else None)
        written = annotate_judge(rows, chat=chat,
                                 on_row=lambda: rewrite_rows(src, rows))
        print(f"judge level: {written} verdicts written", flush=True)
    summary = probe_rows(rows, source=src.name)
    out = Path(args.out) if args.out else default_out(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items()
                      if k != "pathway_evidence"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
