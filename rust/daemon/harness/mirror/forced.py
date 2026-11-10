"""Forced real-data run: every live Python-vs-Rust differential on a copy of
the real bank.

It runs inside the mirror runner (``ops/Dockerfile.mirror-runner``) on the
mirror's internal network. Every disposable database copies the seeded,
credential-rewritten bank (``PL_HARNESS_BASE_DATABASE``, from ``seed.py``),
so each scenario runs its own seed and steps on top of real data. Dumps
hold only what differs from that bank (``dbstate``), and an extractor or
judge prompt that no table answers gets the launcher's default
(``PL_HARNESS_STUB_DEFAULT_CONTENT``). Both daemons then see identical
model output, and their request bytes are still compared.

Each scenario runs in its own harness process, so one crash costs one
scenario. A scenario that differs or fails runs twice more:

* again on the real bank, where a clean rerun means ``transient``;
* on the harness's own synthetic bank, where a difference there too means
  ``preexisting``, and a clean run means ``new`` (it differs on real data
  only).

``summary.json`` holds counts and scenario names only. ``private.json``
holds the differences, which quote bank text, and stays on the host.

    python rust/daemon/harness/mirror/forced.py --rust-bin BIN --out DIR [--suites w2e ...] [--only NAME ...]
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import signal
import secrets
import subprocess
import threading
import uuid
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
ROOT = HARNESS.parents[2]
sys.path.insert(0, str(HERE))
import seed  # noqa: E402

# suite -> (script, directory it imports from, expression naming its scenarios)
SUITES = {
    "w2e": ("w2e.py", HARNESS, "import w2e; names = list(w2e.SCENARIOS)"),
    "w2d_search": ("w2d_search.py", HARNESS, "import w2d_search; names = w2d_search.default_scenarios('live')"),
    "board": ("board/run.py", HARNESS / "board",
              "import run; names = list(run.S.SCENARIOS) + ['mixed']"),
    "routes": ("run.py", HARNESS, "import run; names = list(run.SCENARIOS) + ['refusals']"),
}
# Harnesses this run does not force, and why (reported with the counts).
NOT_FORCED = {
    "dream_cursors": "admits only the bench PostgreSQL at 127.0.0.1:5433 by design; "
                     "the w2e dream, deep-dream and rollback scenarios run instead",
    "cli_harness": "its rows drive a fixture HTTP daemon or build their own synthetic "
                   "databases, not a bank a restore can replace",
    "golden and mutant modes": "they compare against recordings made on synthetic banks",
}
# The summary lines every live harness prints, one per difference: "DIFF
# [scenario]" and "DB DIFF [...]", run.py's refusal rows "DIFF <case>:", and
# the board's "STATE [...]" and "CHAIN [...]". Indented progress lines repeat
# them and are not counted.
DIFF_LINE = re.compile(r"^(?:(?:DB )?DIFF[ \[]|STATE \[|CHAIN \[)")
# Where a difference is: its step or case, and the first state or response path.
LOCATION = re.compile(r": (?:db )?(/[^\s:]+)")
STEP = re.compile(r"\] (?:step |#)?(\d+)")
# What the synthetic arm drops, so it is the harness's own baseline (CI's).
REAL_BANK_ENV = ("PL_HARNESS_BASE_DATABASE", "PL_HARNESS_STUB_DEFAULT_CONTENT",
                 "PL_HARNESS_DREAM_REST_TIMEOUT", "PL_HARNESS_BASE_CACHE")
# The real-bank arms' REST timeout for /api/dream calls, in seconds: Python's
# Console dream run over the real bank's copy outlasted the default 120 s
# (console-dream-run and -gates, forced run of 2026-11-08). Synthetic arms
# keep the default.
DREAM_REST_TIMEOUT = 600
# Scenarios whose real-bank arms do not run, by scenario, with the reason:
# their premise cannot hold on a real bank, so a real-bank result would be
# timing noise. Their synthetic arm still runs and must pass ("excluded");
# CI's synthetic coverage is unchanged. Conductor decision, 2026-11-08.
REAL_BANK_EXCLUDED = {
    "w2e/sweep-analyzer-tick": (
        "the scenario rests on one analyzer batch (50 anchors) covering every entity, so the "
        "settled state does not depend on how many ticks ran; a real bank holds far more entities, "
        "its seeded entities are the newest, and each daemon's analyzer walks 50 per tick from the "
        "copied cursor on its own timer, so the two arms compare at different stages (the settle "
        "also never went quiet: forced run, 2026-11-08)"),
    # Maintainer decision D2, 2026-11-10 (forced run 8eb08c214's findings).
    "w2d_search/hide-superseded": (
        "its hidden and served sets depend on the bank's ranking: on a real bank the seeded "
        "entries compete with the real ones for the top places, so the result reflects the data, "
        "not the daemon (forced run 8eb08c214); excluded by the maintainer, 2026-11-10"),
    "w2d_search/mcp-stats": (
        "its coverage check needs the stats graduation floor reached, which a real bank's "
        "distribution does not reach (coverage stats.graduation below the floor, forced run "
        "8eb08c214); excluded by the maintainer, 2026-11-10"),
    "w2e/candidate-auto": (
        "its expects assume the candidate judge's pairs are its own and are judged first; on a real "
        "graph the judge's top pairs are real ones, and the retry then hits the accepted Python "
        "memory limit (forced run 8eb08c214); excluded by the maintainer, 2026-11-10"),
    # Conductor's ruling A under the maintainer's delegation of 2026-11-11
    # (combined run e4a09212c's findings).
    "w2e/candidate-auto-reopen": (
        "the Python memory limit at the harness's worst-case floors already accepted for its "
        "sibling w2e/candidate-auto (ACCEPTED_OOM): on a real bank's copy neither real attempt "
        "compares. In combined run e4a09212c (--jobs 1, 18 GiB runner) the first attempt was a "
        "Python settle timeout (567 s, no OOM kill) and the retry an OOM kill (184 s), while the "
        "synthetic arm passed; accepted by the conductor under the maintainer's delegation of "
        "2026-11-11"),
    # Maintainer decision, 2026-11-10 (partial run 870fb28fc's findings).
    "routes/stamped": (
        "its template deletes a builtin relation nothing references (no edge, edge proposal or "
        "inverse_of points at it), so a startup that wrongly re-seeds builtins shows it again; a "
        "real bank references every builtin relation, so prepare_template refuses before the "
        "scenario runs (run.py unreferenced_builtin_relation, partial run 870fb28fc); excluded by "
        "the maintainer, 2026-11-10"),
    "w2e/search-after-restart": (
        "its access-count expect, already narrowed to the entries the scenario stores itself, "
        "still fails on both daemons alike on a real bank's copy, so the arm's state and "
        "response diffs cannot be read as Python-vs-Rust differences (partial run 870fb28fc); "
        "excluded by the maintainer, 2026-11-10"),
}
# Differences the maintainer accepted on real-bank arms, by name: a scenario
# whose every difference place (in the first attempt and the retry) is one
# of these is classified "accepted" with the reason, not "new". One place
# anywhere else keeps it new.
ACCEPTED_DIVERGENCES = {
    "community-partition": (
        "the community partition of a real graph differs by Louvain's visiting order (Python "
        "seed.shuffle with random.Random(42), Rust graph order; rust/daemon/divergences/"
        "write-path-w2-e/memory-dream-run-the-graph/louvain-s-visiting-order.md, #1417): on a "
        "real bank's copy Python re-partitions the copied communities after graph writes and Rust "
        "reproduces the base's. Accepted by the maintainer for real-bank arms, 2026-11-10 (D1); "
        "extended the same day to meta's graph_digest row beside a partition difference: the "
        "snapshot _refresh_graph_insight writes with the partition, whose god_nodes can also "
        "differ by betweenness sampling above betweenness_sample nodes (betweenness-above-"
        "betweenness-sample.md; partial run 870fb28fc); and again to the same values in an "
        "observed bank's response-side rows, the digest named by its key (session scenarios)"),
    # The conductor's rulings B, C and D under the maintainer's delegation of
    # 2026-11-11 (combined run e4a09212c's findings): TIE_RULES, each proved
    # from the records the harness writes beside the lines (tie_proofs.py).
    "tie-order": (
        "two daemons serving different entries among equal or near-equal scores "
        "(rust/daemon/divergences/w2-d-get-api-search/tie-order-among-equal-scores.md): Python "
        "picks among exact ties by torch.topk's unspecified order (miras/band.py::retrieve, then "
        "cms.py's seen_texts) and Rust by resident order (search.rs::top_k), and distinct entries "
        "within the 2e-4 score tolerance fall in each engine's own fp32 order. Accepted narrowly, "
        "each rule on its own: a duplicate pick whose two ids have equal text (an md5 the harness "
        "recorded, never the text); an adjacent swap in a retrieval event's served list, neither "
        "entry contiguity or timeline; and an access_count difference of exactly the extra serves "
        "of an entry an accepted duplicate pick served on one side earlier in the scenario. "
        "Accepted by the conductor under the maintainer's delegation of 2026-11-11"),
    # Ruling E, the same delegation: TIE_RULES "padded-query".
    "padded-query": (
        "a w2d_search case whose own query ends in whitespace (tie_proofs.padded: the cause, so "
        "every such case; today default's, mcp-search's, mcp-search-explain's and trace's padded "
        "cases) differing only in its response's cortex block (slots both sides served keep one "
        "value), the served_facts of the event logged with its query (slots both served keep "
        "their fields) and slot_reads, whose full comparison differs only in slots one side alone "
        "served: accepted by the conductor under the maintainer's delegation of "
        "2026-11-11: sentence-transformers 5.4.0 stopped stripping text before tokenising; Rust "
        "follows the 5.3.0 contract (w2d-tokenizer-runtime)"),
}
COMMUNITY_TABLES = ("public.communities", "public.entity_communities")
COMMUNITY_FIELDS = ("community", "communities")
# Meta rows written with the partition, by key: a meta row difference is a
# community one only when its line names one of these keys. A meta place
# names no key (/rows/public.meta/rows, or base_missing), so the line does.
COMMUNITY_META_KEYS = ("graph_digest",)
# The same difference in an observed bank's rows (a routes session
# observation, .json.db.rows.public.<table>...): the partition columns by
# position (pseudolife_memory/storage/schema.py), so a clock or an id
# column stays a difference. Maintainer decision, 2026-11-10.
RESPONSE_ROW = re.compile(r"^\.json\.db\.rows\.public\.(\w+)\.(.+)$")
RESPONSE_PARTITION_COLUMNS = {
    "communities": {0, 1, 2, 3},     # id, label, size, cohesion (not computed_at)
    "entity_communities": {1},       # community_id (not entity_id, computed_at)
}
META_ROW = re.compile(r"/rows/public\.meta/rows: row only in (?:python|rust): \[([\"'])(\w+)\1")
# Known limits the maintainer accepted, by scenario: an error where both real
# attempts each saw the runner's cgroup OOM counter rise is classified
# "accepted" with this reason, not "error". Any other error in these
# scenarios still counts. Both scenarios are now in REAL_BANK_EXCLUDED, so the
# rule is dormant until a real arm runs again.
ACCEPTED_OOM = {
    name: ("a known Python memory limit at the harness's worst-case -1.0 floors: Python peaks at "
           "14.5 GiB in deep apply plus the candidate judge (3.3 GiB at production floors) while "
           "Rust passes at 4.1 GiB, so both daemons together exceed the 18 GiB runner. Accepted by "
           "the maintainer on 2026-11-08 (option (c), Python is being retired); no Python fix")
    for name in ("w2e/candidate-auto", "w2e/candidate-auto-reopen")
}
DISK_FLOOR = 8 * 1024 ** 3
# Scenarios that drive the dream, judge or analyzer queues run on the seed's
# quiet copy (seed.py quiet): the conductor's decision D1, 2026-11-07. On the
# real bank's copy they waited on its whole backlog (3,915 entries pending a
# dream, 276 entity and 376 edge proposals pending a judge), which no stub
# answer settles, so they compared nothing. Why each prefix is here:
QUIET_PREFIXES = {"w2e": (
    "judge-merge-",           # the merge judge: 276 real entity proposals pending (D1)
    "sweep-analyzer-",        # the analyzer files proposals into the same queues (D1)
    "console-dream-run",      # a Console dream pulls the 3,915-entry dream backlog (D1)
    "junk-",                  # the junk judge reached real proposals at a different time per side
    "link-",                  # the link judge: 376 real edge proposals pending (2026-11-08)
    "hook-session-end-dream",  # session-end dreams expect no pending entry (2026-11-08)
    "cli-episode-end-dream",
)}
QUIET_WHY = ("drives the dream, judge or analyzer queue; on the real bank's copy it "
             "waited on the whole real backlog, so it runs on the seed's copy with that backlog "
             "acknowledged, its pending proposals removed and its automatic decisions (which the "
             "judges reconsider) removed")
QUIET_DB = seed.QUIET_DB
# The curation judge's scenarios, on a quiet copy that also holds no current
# lesson or world fact (conductor decision (c), 2026-11-08). The judge takes
# the 8 most similar pairs over every current record, and on the real bank's
# copy (3,748 lessons, 411 world facts) real pairs outranked the scenarios'
# own, which were never judged: curation-shadow and the curation-auto family
# never settled. Checked before QUIET_PREFIXES.
CURATION_PREFIXES = {"w2e": ("curation-",)}
CURATION_WHY = ("drives the curation judge, which ranks pairs over every current lesson and world "
                "fact; on the real bank's copy real pairs outranked the scenario's, so it runs on a "
                "quiet copy whose real lessons and world facts are superseded")
CURATION_DB = seed.CURATION_DB


def scenario_names(suite: str) -> list[str]:
    _, directory, expression = SUITES[suite]
    # The script's own directory first, as when it runs: board/run.py, not run.py.
    code = (f"import json, sys; sys.path.insert(0, {str(HARNESS)!r}); "
            f"sys.path.insert(0, {str(directory)!r}); {expression}; print(json.dumps(names))")
    out = subprocess.run([sys.executable, "-c", code], cwd=directory, capture_output=True,
                         text=True, check=True, timeout=300)
    return json.loads(out.stdout.strip().splitlines()[-1])


def outcome(exit_code: int | None, log: str) -> dict:
    """``pass``, ``diff`` or ``error``, read from the harness's own output:
    every live harness prints one ``DIFF [...]`` line per difference and
    exits non-zero on any."""
    diffs = [line for line in log.splitlines() if DIFF_LINE.match(line)]
    if exit_code == 0 and not diffs:
        return {"status": "pass", "diffs": []}
    status = "diff" if diffs else "error"
    tail = [] if diffs else log.strip().splitlines()[-6:]
    return {"status": status, "diffs": diffs, "error_tail": tail}


# Every harness caps the DIFF lines it prints (w2e 6 per step; routes 4 items
# per case and 20 db lines; w2d 5 and 20; board 4 items per step, 12 state
# and 12 chain lines), so a difference past a cap is in no printed line. Its
# final --out JSON holds them all: harness_lines rebuilds each harness's
# printed lines from it, capped as printed or in full.
def _w2e_lines(data: dict, capped: bool):  # w2e.py main(): DIFF per step diff
    for r in data["scenarios"]:
        for s in r["steps"]:
            for d in s["diffs"][:6 if capped else None]:
                yield f"DIFF [{r['scenario']}] step {s['step']} {json.dumps(s['op'])[:90]}: {d}"


def _routes_lines(data: dict, capped: bool):  # run.py main(): cases, db, refusals
    for r in data["scenarios"]:
        for c in r["cases"]:
            if c["diffs"]:
                yield f"DIFF [{r['scenario']}] {c['case']}: {c['diffs'][:4 if capped else None]}"
        for d in r["db_diffs"][:20 if capped else None]:
            yield f"DB DIFF [{r['scenario']}] {d}"
    for r in data["refusals"]:
        if r["diffs"]:
            yield f"DIFF {r['case']}: {r['diffs']}"


def _w2d_lines(data: dict, capped: bool):  # w2d_search.py main(): cases, db
    for r in data["results"]:
        for c in r["cases"]:
            if c["diffs"]:
                yield f"DIFF [{r['scenario']}] {c['case']}: {c['diffs'][:5 if capped else None]}"
        for d in r["db_diffs"][:20 if capped else None]:
            yield f"DB DIFF [{r['scenario']}] {d}"


def _board_lines(data: dict, capped: bool):  # board/run.py main(): steps, state, chain
    for r in data["results"]:
        for s in r["steps"]:
            if s["diffs"]:
                yield f"DIFF [{r['scenario']}] #{s['step']} {s['name']}: {s['diffs'][:4 if capped else None]}"
        for d in r["state_diffs"][:12 if capped else None]:
            yield f"STATE [{r['scenario']}] {d}"
        for p in r["chain"][:12 if capped else None]:
            yield f"CHAIN [{r['scenario']}] {p}"


# suite -> (is this the harness's final, complete --out write, its lines).
# Routes' progress write is {"partial": [...]}; the others mark theirs
# "complete": false.
HARNESS_OUT = {
    "w2e": (lambda d: d.get("complete") is True and "scenarios" in d, _w2e_lines),
    "routes": (lambda d: "summary" in d and "scenarios" in d and "refusals" in d, _routes_lines),
    "w2d_search": (lambda d: d.get("complete") is True and "results" in d, _w2d_lines),
    "board": (lambda d: d.get("complete") is True and "results" in d, _board_lines),
}


def harness_lines(suite: str, data: dict, capped: bool) -> list[str]:
    """The DIFF lines ``suite``'s harness prints from its --out ``data``:
    as printed when ``capped``, else every difference in the same format."""
    return list(HARNESS_OUT[suite][1](data, capped))


def full_diffs(suite: str, path: Path, printed: list[str]) -> tuple[list[str] | None, str | None]:
    """Every difference line of an attempt, from its --out JSON, or None and
    why not: no file, unparseable, not the final write, or capped lines that
    are not the ones the log printed (another run's file, a changed format)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "the harness wrote no --out JSON"
    except (OSError, ValueError) as e:
        return None, f"its --out JSON is unreadable: {str(e)[:120]}"
    try:
        if not (isinstance(data, dict) and HARNESS_OUT[suite][0](data)):
            return None, "its --out JSON is not the harness's final, complete write"
        shown, full = harness_lines(suite, data, True), harness_lines(suite, data, False)
    except Exception as e:  # any shape the rebuild does not know; never fatal to the run
        return None, f"its --out JSON has an unexpected shape: {type(e).__name__}: {str(e)[:120]}"
    # The log read: a difference holding a newline prints as several lines.
    if [line for line in "\n".join(shown).splitlines() if DIFF_LINE.match(line)] != printed:
        return None, "its --out JSON does not match the printed DIFF lines"
    return full, None


def tie_proofs_of(suite: str, path: Path) -> dict[str, list]:
    """The tie-order proofs (tie_proofs.py) a w2e or w2d_search harness wrote
    beside its lines, by context: "step N", "case <name>" or "db". Read only
    after full_diffs accepted the same file; another suite writes none."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list] = {}
    if suite == "w2e":
        for r in data["scenarios"]:
            for st in r["steps"]:
                if st.get("tie_proofs"):
                    out.setdefault(f"step {st['step']}", []).extend(st["tie_proofs"])
    elif suite == "w2d_search":
        for r in data["results"]:
            for c in r["cases"]:
                if c.get("tie_proofs"):
                    out.setdefault(f"case {c['case']}", []).extend(c["tie_proofs"])
            if r.get("db_tie_proofs"):
                out.setdefault("db", []).extend(r["db_tie_proofs"])
    return out


# The other suites' lines: board STATE (after step N (label): table: ...),
# board CHAIN (side: key: ...), routes DB (a leading /rows/... path), w2d
# fields (public.table[i].field: python ... vs rust ...), w2d coverage
# (coverage name: a < b), scenario errors (scenario error: ['Type: ...
# (case 'x'; at file:N in fn)']) and case lines (case: ['item', ...], board's
# as "#N name: [...]"), where each printed item is a place of its own. Indexes
# are masked, and no value reaches a signature.
HEAD = re.compile(r"^(?:DB )?(?:DIFF|STATE|CHAIN) \[[^\]]*\] ?")
AFTER_STEP = re.compile(r"^after step (\d+) \([^)]*\): (\w+):")
CHAIN_KEY = re.compile(r"^(python|rust): (\w+): value ")
LEADING_PATH = re.compile(r"^(/[^\s:]+)")
FIELD = re.compile(r"^([\w.\[\]\-]+): (?:python |only in |[\d.]+ <)")
COVERAGE = re.compile(r"^coverage ([\w.]+): ")
ERROR = re.compile(r"^scenario error: \[['\"](\w+)")
ERROR_WHERE = re.compile(r"\((case (?:'[^']*'|\"[^\"]*\"); )?at ([\w.]+):\d+ in (\w+)\)")
CASE = re.compile(r"^([^:\[\]'\"]{1,80}): (\[.*\])$", re.S)
ITEM_PATH = re.compile(r"^(\.?[\w.\[\]\-]+): ")
INDEX = re.compile(r"\[\d+\]")
# w2e lines: "DIFF [s] step N <json.dumps(op)[:90]>: <diff>"; _w2e_diff
# finds where the printed op ends.
W2E_STEP = re.compile(r"^(?:DB )?DIFF \[[^\]]*\] step (\d+) ")
OP_CUT = 90  # w2e prints json.dumps(op)[:90]
W2E_EXPECT = re.compile(r"^expect (python|rust): (.*)$", re.S)
W2E_DB_PATH = re.compile(r"^(?:db )?(/[^\s:]+)")
DIFF_PATH = re.compile(r"^(?:db )?[\w.\[\]\-]+$")
# Literals in an expect's SQL: quoted strings and standalone numbers, not the
# digits inside identifiers (e2.id, a _v2 column).
SQL_VALUE = re.compile(r"'(?:[^']|'')*'|(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")


RAW_PATH = re.compile(r"^raw (\S+?): ")
SIDE = re.compile(r"^(python|rust): ")
BARE_VALUE = re.compile(r"'[^']*'|\"[^\"]*\"|\S*\d\S*|\b(?:true|false|null|None)\b")


def _item_place(item) -> str:
    """One printed case item's place: its path or key; for run.py's "raw
    <path>: ..." checks, that path; else its side and its words up to the
    first ": " (after which a value follows), numbers, quoted text and bare
    true/false/null removed."""
    text = str(item)
    m = RAW_PATH.match(text)
    if m:
        return "raw " + INDEX.sub("[]", m.group(1))
    m = ITEM_PATH.match(text)
    if m and m.group(1) not in ("python", "rust"):
        return INDEX.sub("[]", m.group(1))
    side = SIDE.match(text)
    body = text[side.end():] if side else text
    words = BARE_VALUE.sub("", body.split(": ", 1)[0]).split()
    return " ".join(([side.group(1)] if side else []) + words[:12]) or "<item>"


def _w2e_diff(body: str) -> str | None:
    """The diff after a w2e line's printed op (json.dumps(op)[:90]): an op
    that decodes whole ends where the decoder stops; one that does not was
    cut, so the diff starts after its 90 characters. Decoding, not a colon
    search, because the op's own strings may hold ": " (a stored note)."""
    try:
        _, end = json.JSONDecoder().raw_decode(body)
    except ValueError:
        end = OP_CUT
    return body[end + 2:] if body[end:end + 2] == ": " else None


def _w2e_place(diff: str) -> str:
    """A w2e diff's place: an expect's side and query without its literals,
    a db path, or the text before its first ": " (a response or table path
    as printed, else its words with values removed)."""
    m = W2E_EXPECT.match(diff)
    if m:
        return f"expect {m.group(1)} " + " ".join(SQL_VALUE.sub("", m.group(2)).split())[:160]
    m = W2E_DB_PATH.match(diff)
    if m:
        return m.group(1)
    head = diff.split(": ", 1)[0]
    if DIFF_PATH.match(head):
        return INDEX.sub("[]", head)
    return " ".join(BARE_VALUE.sub("", head).split()[:12]) or "<diff>"


def _places(line: str) -> list[tuple[str, str]]:
    rest = HEAD.sub("", line, count=1)
    m = W2E_STEP.match(line)
    if m:
        diff = _w2e_diff(line[m.end():])
        if diff is not None:
            return [(m.group(1), _w2e_place(diff))]
    step, where = STEP.search(line), LOCATION.search(line)
    m = ERROR.match(rest)
    if m:
        # The printed list escapes quotes; decode it so the location reads
        # the same whatever the message's own quoting.
        try:
            printed = ast.literal_eval(rest[len("scenario error: "):])
            text = printed[0] if isinstance(printed, list) and printed else rest
        except (ValueError, SyntaxError):
            text = rest
        at = ERROR_WHERE.search(str(text))
        return [("", f"scenario error {m.group(1)}"
                 + (f" {at.group(1) or ''}{at.group(2)} {at.group(3)}" if at else ""))]
    for pattern, build in (
            (AFTER_STEP, lambda m: (m.group(1), m.group(2))),
            (CHAIN_KEY, lambda m: ("", f"chain {m.group(1)} {m.group(2)}")),
            (LEADING_PATH, lambda m: ("", m.group(1))),
            (COVERAGE, lambda m: ("", "coverage " + m.group(1))),
            (FIELD, lambda m: ("", m.group(1)))):
        m = pattern.match(rest)
        if m:
            s, w = build(m)
            return [(s, INDEX.sub("[]", w))]
    m = CASE.match(rest)
    if m:
        try:
            items = ast.literal_eval(m.group(2))
        except (ValueError, SyntaxError):
            items = None
        if isinstance(items, list) and items:
            return [(m.group(1), _item_place(i)) for i in items]
    return [(step.group(1) if step else "", where.group(1) if where else line[:120])]


def signatures(result: dict) -> set:
    """Each difference's (step or case, place): the state or response path,
    never a value. A case line gives one place per printed item."""
    return {p for line in result["diffs"] for p in _places(line)}


def classify(first: dict, retry: dict | None, synthetic: dict | None) -> str:
    """``preexisting`` only when the synthetic run differs in every place the
    real bank does: an unrelated synthetic failure does not excuse a
    real-bank difference, and neither does one shared place excuse the
    others (conductor decision, 2026-11-10). The first attempt's places must
    be its full list: a place past the harness's printed cap is in no
    printed line."""
    if first["status"] == "pass":
        return "pass"
    if retry is not None and retry["status"] == "pass":
        return "transient"
    if first["status"] == "error" and (retry is None or retry["status"] == "error"):
        return "error"  # no comparison completed: never a pass, never preexisting
    places = signatures(first)
    if synthetic is not None and synthetic["status"] == "diff" and _complete(first) \
            and places and places <= signatures(synthetic):  # an empty set excuses nothing
        return "preexisting"
    return "new"


def base_for(suite: str, name: str) -> tuple[str, str | None]:
    """Which base a scenario's real-bank attempts copy, and why."""
    if any(name.startswith(p) for p in CURATION_PREFIXES.get(suite, ())):
        return "curation", CURATION_WHY
    if any(name.startswith(p) for p in QUIET_PREFIXES.get(suite, ())):
        return "quiet", QUIET_WHY
    return "main", None


def quiet_base_use(report: dict) -> dict:
    """The scenarios that ran on the quiet base, for the summary."""
    used = [f"{suite}/{r['scenario']}" for suite, body in report["suites"].items()
            for r in body["rows"] if r.get("base") == "quiet"]
    return {"database": QUIET_DB, "scenarios": used, "why": QUIET_WHY}


def curation_base_use(report: dict) -> dict:
    """The scenarios that ran on the curation base, for the summary."""
    used = [f"{suite}/{r['scenario']}" for suite, body in report["suites"].items()
            for r in body["rows"] if r.get("base") == "curation"]
    return {"database": CURATION_DB, "scenarios": used, "why": CURATION_WHY}


# A kept daemon log's tail: one log covers one launch (daemons.py opens it
# "wb"), and its last lines say why it died.
KEPT_LOG_BYTES = 4 * 1024 * 1024


def keep_daemon_logs(scratch: Path, kept: Path) -> str:
    """Copy the tail of each daemon.log under ``scratch`` into ``kept``. The
    daemons' own logs say which one died and why; they may quote bank text,
    so they stay beside the private report on the host. A log that cannot be
    kept (a full disk, a log gone mid-copy) is noted, never fatal."""
    notes = ""
    for daemon_log in scratch.rglob("daemon.log") if scratch.is_dir() else ():
        rel = daemon_log.parent.relative_to(scratch)
        try:
            target = kept / rel
            target.mkdir(parents=True, exist_ok=True)
            with daemon_log.open("rb") as src, (target / "daemon.log").open("wb") as dst:
                size = src.seek(0, os.SEEK_END)
                if size > KEPT_LOG_BYTES:
                    dst.write(f"[truncated: last {KEPT_LOG_BYTES} of {size} bytes]\n".encode())
                src.seek(max(0, size - KEPT_LOG_BYTES))
                shutil.copyfileobj(src, dst)
        except OSError as e:
            notes += f"\nkept no daemon log for {rel}: {e}"
    return notes


def read_oom_kills() -> int | None:
    """The OOM kills inside this container's memory cgroup so far, or None
    outside one: an error with kills during its attempt is the runner's
    capacity, not a daemon's verdict."""
    try:
        for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        pass
    return None


class Runner:
    """One harness process per scenario; tests replace ``invoke`` or ``_run``."""

    def __init__(self, rust_bin: Path, out: Path, timeout: float, stop: Path, sweep=None,
                 scratch: Path = Path("/tmp/forced"), oom_kills=read_oom_kills,
                 drop_token: str | None = None, dropper: "Dropper | None" = None,
                 base_cache: Path | None = None):
        self.rust_bin, self.out, self.timeout, self.stop = rust_bin, out, timeout, stop
        # ``sweep(attempt)``: the attempt's token (ATTEMPT_ENV), which a sweep
        # scoped to one worker's harnesses drops by; one at a time, it sweeps all.
        self.sweep = sweep or (lambda attempt: 0)
        self.scratch, self.oom_kills = scratch, oom_kills
        self.base_cache = base_cache or scratch / "base-cache"
        self._proc: subprocess.Popen | None = None
        # An interrupt (``--jobs``: from the main thread) stops this worker's
        # running harness and starts no further attempt: under one lock with
        # ``_proc``, so a harness about to start is never missed.
        self._lock = threading.Lock()
        self.cancelled = False
        # With both, a scenario's drops rename its databases away and the
        # dropper drops them while the next scenario runs.
        self.drop_token, self.dropper = drop_token, dropper

    def invoke(self, suite: str, name: str, label: str, base: bool) -> dict:
        if self.cancelled:  # an interrupted run's worker starts no retry or synthetic arm
            raise Cancelled(f"{suite}/{name} {label}: the run was interrupted")
        script = HARNESS / SUITES[suite][0]
        work = self.out / suite / label
        work.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
        env = dict(os.environ, PYTHONUNBUFFERED="1",
                   PL_HARNESS_SCRATCH=str(self.scratch / suite / safe))
        which = base_for(suite, name)[0] if base else None
        if not base:
            for key in REAL_BANK_ENV:
                env.pop(key, None)
        elif which == "quiet":
            env["PL_HARNESS_BASE_DATABASE"] = QUIET_DB
        elif which == "curation":
            env["PL_HARNESS_BASE_DATABASE"] = CURATION_DB
        if self.drop_token and self.dropper:
            env[DROP_LATER_ENV] = self.drop_token
        if base:
            env.setdefault("PL_HARNESS_DREAM_REST_TIMEOUT", str(DREAM_REST_TIMEOUT))
            # One read of each base's row hashes for the whole run, not one per
            # scenario process (dbstate checks it against the base each time).
            env["PL_HARNESS_BASE_CACHE"] = str(self.base_cache)
        attempt = secrets.token_hex(4)
        env[ATTEMPT_ENV] = attempt
        reaped = self.reap_leftovers()
        before = self.oom_kills()
        started = time.monotonic()
        written = work / f"{safe}.json"
        written.unlink(missing_ok=True)  # an earlier run's file is not this attempt's
        code, log = self._run([sys.executable, str(script), "live", "--rust-bin", str(self.rust_bin),
                               "--only", name, "--out", str(written)], env)
        after = self.oom_kills()
        # Scenarios run one at a time, so no disposable database should
        # outlive its attempt; a harness that raised never reached its drops.
        swept = self.sweep(attempt)
        if swept:
            log += f"\nswept {swept} leftover disposable databases"
        if self.dropper:
            self.dropper.kick()
        if reaped:
            log = f"stopped {len(reaped)} leftover processes before this scenario\n" + log
        kept = work / f"{safe}.daemon-logs"
        shutil.rmtree(kept, ignore_errors=True)  # an earlier attempt's logs are not this one's
        try:
            if outcome(code, log)["status"] != "pass":
                log += keep_daemon_logs(Path(env["PL_HARNESS_SCRATCH"]), kept)
        finally:
            shutil.rmtree(env["PL_HARNESS_SCRATCH"], ignore_errors=True)
        (work / f"{safe}.log").write_text(log, encoding="utf-8")
        kills = after - before if before is not None and after is not None else None
        result = outcome(code, log)
        # The printed lines are capped: a differing attempt takes every
        # difference from its --out JSON, and without that it keeps the
        # printed ones, marked incomplete (no preexisting, no acceptance).
        result["diffs_complete"] = result["status"] == "pass"
        if result["status"] == "diff":
            full, why = full_diffs(suite, written, result["diffs"])
            if full is None:
                result["diffs_note"] = why
            else:
                result["diffs"], result["diffs_complete"] = full, True
                result["tie_proofs"] = tie_proofs_of(suite, written)
        return {**result, "exit": code, "seconds": round(time.monotonic() - started, 1),
                "oom_kills": kills, "base": which}

    def _run(self, argv: list[str], env: dict) -> tuple[int | None, str]:
        # Its own process group, stopped whatever the outcome: a harness that
        # raised may leave a daemon its cleanup lost (2.9 GiB each).
        with self._lock:  # an interrupt either sees this harness or stops it starting
            if self.cancelled:
                raise Cancelled("the run was interrupted before this attempt started")
            proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            self._proc = proc
        try:
            try:
                raw, _ = proc.communicate(timeout=self.timeout)
                code, log = proc.returncode, raw.decode("utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                code, log = None, self._stop(proc) + f"\nTIMEOUT after {self.timeout:g}s"
            except BaseException:
                # Interrupted (Ctrl-C reaches this process only: the harness has
                # a session of its own): stop it as a timeout does, so its
                # cleanup has renamed its databases before the run's drops drain.
                self._stop(proc)
                raise
        finally:
            self._proc = None
            if hasattr(os, "killpg"):  # the runner is Linux
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        return code, log

    def interrupt(self, grace: float = 60.0) -> None:
        """Start no further attempt on this worker, and stop its running
        harness as a timeout does (another thread waits on it): SIGINT, so its
        cleanup stops its daemons and renames its databases, then SIGKILL to
        the group if it has not exited within ``grace``."""
        with self._lock:
            self.cancelled = True
            proc = self._proc
        if proc is None or not hasattr(os, "killpg"):
            return
        for sig, wait in ((signal.SIGINT, grace), (signal.SIGKILL, 30.0)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + wait
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if proc.poll() is not None:
                return

    def reap_leftovers(self) -> list[int]:
        """Stop every process left from an earlier scenario, found by this
        run's own marker (a daemon's data directory under the run's scratch
        root): a daemon in a session of its own escapes its group's stop."""
        marker = str(self.scratch) + os.sep
        found = []
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return found
        for entry in proc_root.iterdir():
            if not entry.name.isdigit() or int(entry.name) in (os.getpid(), os.getppid()):
                continue
            try:
                raw = (entry / "environ").read_bytes()
            except OSError:
                continue
            for item in raw.split(b"\0"):
                if item.startswith(b"PSEUDOLIFE_MCP_DATA_DIR=") \
                        and item.split(b"=", 1)[1].decode(errors="replace").startswith(marker):
                    try:
                        os.kill(int(entry.name), signal.SIGKILL)
                        found.append(int(entry.name))
                    except ProcessLookupError:
                        pass
                    break
        return found

    @staticmethod
    def _stop(proc: subprocess.Popen) -> str:
        """SIGINT first, so the harness's own cleanup stops its daemons and
        drops its databases; SIGKILL to the group if it has not exited."""
        for sig, wait in ((signal.SIGINT, 60), (signal.SIGKILL, 30)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                raw, _ = proc.communicate(timeout=wait)
                return raw.decode("utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                continue
        return ""

    def blocked(self) -> str | None:
        if self.stop.exists():
            return "STOP file present"
        if shutil.disk_usage("/").free < DISK_FLOOR:
            return "free disk below the 8 GiB floor"
        return None


# pgdisposable's: the variable a scenario's drops read the run's token from.
DROP_LATER_ENV = "PL_HARNESS_DROP_LATER"
# run_names': each attempt's token, which starts every database name suffix
# its harness allocates, so a sweep scoped to the attempt drops only those.
ATTEMPT_ENV = "PL_HARNESS_ATTEMPT"


class Cancelled(Exception):
    """An interrupted run's worker was asked to start an attempt."""


class Dropper:
    """Drops the run's renamed-away databases on a thread of its own while
    the next scenario runs. ``kick`` starts a pass after the last one ended,
    so none is missed; ``drain`` waits for it and drops what is left, and
    says why if it could not, for the summary (the run then fails)."""

    def __init__(self, drop):
        self._drop, self._thread = drop, None
        self._kick_lock = threading.Lock()  # workers kick side by side (--jobs)
        self.errors: list[str] = []

    def _pass(self) -> None:
        try:
            self._drop()
        except Exception as e:  # noqa: BLE001 - the drain's own pass retries and raises
            self.errors.append(f"{type(e).__name__}: {e}"[:300])

    def kick(self) -> None:
        with self._kick_lock:
            if self._thread is not None:
                self._thread.join()
            self._thread = threading.Thread(target=self._pass, name="forced-drops", daemon=True)
            self._thread.start()

    def drain(self) -> str | None:
        """None when every tombstone is dropped, else why the last pass failed."""
        with self._kick_lock:  # no worker's late kick starts a pass beside this one
            if self._thread is not None:
                self._thread.join()
                self._thread = None
            try:
                self._drop()
            except Exception as e:  # noqa: BLE001 - reported in the summary, the run fails
                return f"{type(e).__name__}: {e}"[:300]
            return None


def _scenario(runner: Runner, suite: str, name: str) -> tuple[dict, dict | None, str]:
    """One scenario's attempts on ``runner``: its report row, its private
    entry (None when it passed) and its progress line."""
    if f"{suite}/{name}" in REAL_BANK_EXCLUDED:
        synthetic = runner.invoke(suite, name, "synthetic", base=False)
        # Only the real bank is excluded: a failing synthetic arm is an error.
        verdict = "excluded" if synthetic["status"] == "pass" else "error"
        row = {"scenario": name, "class": verdict,
               "differences": len(synthetic["diffs"]), "seconds": synthetic["seconds"],
               "base": None, "oom_kills": _oom_total(synthetic)}
        entry = None if verdict == "excluded" else {"real": None, "real_retry": None,
                                                    "synthetic": synthetic}
        return row, entry, (f"{suite}/{name}: {verdict} (real bank excluded; synthetic "
                            f"{synthetic['status']})")
    first = runner.invoke(suite, name, "real", base=True)
    retry = synthetic = None
    if first["status"] != "pass":
        retry = runner.invoke(suite, name, "real-retry", base=True)
        if retry["status"] != "pass":
            synthetic = runner.invoke(suite, name, "synthetic", base=False)
    verdict = classify(first, retry, synthetic)
    # Each failing real attempt must itself have been OOM-killed: an
    # unrelated failure stays an error, and the small synthetic arm's
    # kills are not the accepted limit.
    accepted_as = None
    if verdict == "error" and f"{suite}/{name}" in ACCEPTED_OOM \
            and all((a.get("oom_kills") or 0) > 0 for a in (first, retry)):
        verdict, accepted_as = "accepted", "oom"
    # Every real attempt that ran must have finished (an exit code,
    # not a timeout, signal or OOM kill) with only community
    # differences, read from its full list: an errored or cut one is
    # never filtered out.
    elif verdict == "new" and all(_finished(a) and _complete(a) and _community_only(a)
                                  for a in (first, retry) if a is not None):
        verdict, accepted_as = "accepted", "community-partition"
    # The tie and padded-query rules, alone or composed with the community
    # one (ruling F): every line of every real attempt covered by one rule
    # holding in full on its own terms; one line covered by none stays new.
    elif verdict == "new" and all(_finished(a) and _complete(a) and _composed(a)
                                  for a in (first, retry) if a is not None):
        used = set().union(*(_composed(a) for a in (first, retry) if a is not None))
        verdict, accepted_as = "accepted", "+".join(sorted(used))
    row = {"scenario": name, "class": verdict, "differences": len(first["diffs"]),
           "seconds": first["seconds"], "base": first.get("base"),
           "oom_kills": _oom_total(first, retry, synthetic),
           **({"accepted_as": accepted_as} if accepted_as else {})}
    entry = None if verdict == "pass" else {"real": first, "real_retry": retry, "synthetic": synthetic}
    return row, entry, f"{suite}/{name}: {verdict} ({len(first['diffs'])} differences, {first['seconds']}s)"


def run(runner: Runner, suites: list[str], only: list[str] | None,
        names=scenario_names, workers: "list[Runner] | None" = None) -> tuple[dict, dict]:
    """Every planned scenario, one at a time on ``runner``, or side by side
    on ``workers`` (``--jobs``), with the rows in plan order either way."""
    if workers and len(workers) > 1:
        return _run_side_by_side(workers, suites, only, names)
    report = {"suites": {}, "not_forced": NOT_FORCED, "complete": False}
    private: dict = {}
    for suite in suites:
        try:
            planned = [n for n in names(suite) if not only or n in only]
        except (subprocess.SubprocessError, ValueError, IndexError) as e:
            report["suites"][suite] = {"planned": 0, "rows": [], "error": str(e)[:300]}
            continue
        rows = []
        report["suites"][suite] = {"planned": len(planned), "rows": rows}
        for name in planned:
            reason = runner.blocked()
            if reason:
                report["stopped"] = f"before {suite}/{name}: {reason}"
                return report, private
            row, entry, line = _scenario(runner, suite, name)
            rows.append(row)
            if entry is not None:
                private[f"{suite}/{name}"] = entry
            print(line, flush=True)
    report["complete"] = True
    return report, private


def _run_side_by_side(workers: "list[Runner]", suites: list[str], only: list[str] | None,
                      names) -> tuple[dict, dict]:
    """``run`` with one thread per worker, each taking the next scenario in
    plan order (a scenario's attempts stay on one worker). Every suite is
    planned first. A stop (the STOP file, the disk floor, an interrupt or a
    worker that raised) starts no further scenario, lets the running ones
    finish and reports the rows of every scenario that ran."""
    report = {"suites": {}, "not_forced": NOT_FORCED, "complete": False}
    private: dict = {}
    plan = []
    for suite in suites:
        try:
            planned = [n for n in names(suite) if not only or n in only]
        except (subprocess.SubprocessError, ValueError, IndexError) as e:
            report["suites"][suite] = {"planned": 0, "rows": [], "error": str(e)[:300]}
            continue
        report["suites"][suite] = {"planned": len(planned), "rows": []}
        plan += [(suite, n) for n in planned]
    lock = threading.Lock()
    state: dict = {"next": 0, "stopped": None, "raised": None}
    done: dict[int, tuple] = {}

    def work(runner: Runner) -> None:
        # Everything a worker does, its admission checks included, reaches
        # the main thread: an exception lost here would read as a full run.
        try:
            while True:
                with lock:
                    if state["stopped"] or state["raised"] or state["next"] >= len(plan):
                        return
                    i = state["next"]
                    reason = runner.blocked()
                    if reason:
                        state["stopped"] = f"before {plan[i][0]}/{plan[i][1]}: {reason}"
                        return
                    state["next"] = i + 1
                done[i] = _scenario(runner, *plan[i])
                print(done[i][2], flush=True)
        except BaseException as e:  # noqa: BLE001 - re-raised by the main thread
            with lock:
                state["raised"] = state["raised"] or e

    threads = [threading.Thread(target=work, args=(w,), name=f"forced-job-{n}", daemon=True)
               for n, w in enumerate(workers)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    except BaseException:
        # Interrupted: start nothing more, stop every running harness as a
        # timeout does (its cleanup renames its databases), then let the
        # interrupt reach main, whose drain drops them.
        with lock:
            state["stopped"] = state["stopped"] or "interrupted"
        # Side by side, so one harness's grace does not delay the others'.
        stoppers = [threading.Thread(target=w.interrupt, daemon=True) for w in workers]
        for s in stoppers:
            s.start()
        for s in stoppers:
            s.join(120)
        for t in threads:
            t.join(120)
        raise
    if state["raised"] is not None:
        raise state["raised"]
    if not state["stopped"] and len(done) != len(plan):
        raise RuntimeError(f"{len(plan) - len(done)} planned scenarios have no result")
    for i in sorted(done):
        suite, name = plan[i]
        row, entry, _ = done[i]
        report["suites"][suite]["rows"].append(row)
        if entry is not None:
            private[f"{suite}/{name}"] = entry
    if state["stopped"]:
        report["stopped"] = state["stopped"]
        return report, private
    report["complete"] = True
    return report, private


def _community_place(place: str) -> bool:
    """A difference place in the community tables (a db path under one of
    them) or a community field (a response path whose last key is one)."""
    for table in COMMUNITY_TABLES:
        if place == f"/rows/{table}/rows" or place.startswith(f"/rows/{table}/"):
            return True
    return place.startswith(".") and place.rsplit(".", 1)[-1].replace("[]", "") in COMMUNITY_FIELDS


def _finished(attempt: dict) -> bool:
    """The harness ran to its end and compared: it printed differences and
    exited with a code (a timeout leaves none, a signal a negative one), and
    the runner's cgroup saw no OOM kill. A harness that raised after printing
    also exits with a code; the attempt alone cannot tell it apart."""
    code = attempt.get("exit")
    return (attempt.get("status") == "diff" and isinstance(code, int) and code >= 0
            and not (attempt.get("oom_kills") or 0))


def _complete(attempt: dict) -> bool:
    """Its diffs are every difference (read from the harness's --out JSON),
    not just the capped printed lines; a missing mark is not complete."""
    return attempt.get("diffs_complete") is True


def _response_community(item) -> str | None:
    """One case item of an observed bank (``.json.db.rows.public.<table>...``,
    a routes session observation): "partition" for a community table's
    partition column or row set, "meta" for the graph_digest value named by
    its key (``meta.by_key.<key>``, background_sessions.keyed_meta), else
    None. A meta value named only by its row index is not one."""
    m = RESPONSE_ROW.match(str(item).split(": ", 1)[0])
    if not m:
        return None
    table, rest = m.groups()
    if table == "meta":
        key = re.match(r"by_key\.(\w+)(?:[.\[]|$)", rest)
        return "meta" if key and key.group(1) in COMMUNITY_META_KEYS else None
    columns = RESPONSE_PARTITION_COLUMNS.get(table)
    if columns is None:
        return None
    if rest == "rows" or rest.startswith("base_missing"):
        return "partition"
    cell = re.fullmatch(r"rows\[\d+\](?:\[(\d+)\])?", rest)
    if cell and (cell.group(1) is None or int(cell.group(1)) in columns):
        return "partition"
    return None


def _community_line(line: str) -> str | None:
    """How one difference line is a community one: "partition" when every
    place is in the community tables or a community field, "meta" when it is
    a meta row whose key COMMUNITY_META_KEYS names, else None. The key is
    read from the line, so a meta row counts only on a line of one place
    (dbstate prints one row per line); a case line listing several does not
    vouch for each. A case line of an observed bank's values is read item
    by item (_response_community), each naming its own table and key."""
    case = CASE.match(HEAD.sub("", line, count=1))
    if case and ".json.db.rows." in case.group(2):
        try:
            items = ast.literal_eval(case.group(2))
        except (ValueError, SyntaxError):
            items = None
        if isinstance(items, list) and items:
            kinds = [_response_community(i) for i in items]
            if None in kinds:
                return None
            return "partition" if "partition" in kinds else "meta"
    places = _places(line)
    if all(_community_place(where) for _, where in places):
        return "partition"
    if len(places) == 1 and places[0][1] == "/rows/public.meta/rows":
        key = META_ROW.search(line)
        if key and key.group(2) in COMMUNITY_META_KEYS:
            return "meta"
    return None


def _community_only(attempt: dict) -> bool:
    """Every difference of an attempt is in the community tables or fields,
    or a meta row written with the partition, and at least one is the
    partition itself: the digest alone also holds god_nodes, which differ
    for another recorded reason."""
    kinds = [_community_line(line) for line in attempt["diffs"]]
    return bool(kinds) and None not in kinds and "partition" in kinds


# The tie-order rules (ACCEPTED_DIVERGENCES "tie-order"), each separate so
# any subset can be applied (the conductor's rulings B, C and D of
# 2026-11-11). A rule reads a difference's position from its line and its
# proof from the harness's record (tie_proofs.py), never a value it guessed.
TIE_RULES = ("duplicate-pick", "adjacent-swap", "access-count-follow-on", "padded-query")
# Ruling E's places: a padded case's cortex block, its event's served facts,
# and slot_reads rows and counts (the row's printed list is read in memory to
# hash its slot, never printed).
PADDED_CORTEX = re.compile(r"^\.cortex(?:$|[.\[])")
PADDED_FACTS = re.compile(r"^public\.retrieval_events\[(-?\d+)\]\.served_facts(?:$|[.\[])")
PADDED_SLOT_ROW = re.compile(r"^/rows/public\.slot_reads/rows: row only in (python|rust): (\[.*\])$", re.S)
PADDED_SLOT_COUNT = re.compile(r"^/rows/public\.slot_reads/base_missing/count: python (-?\d+) vs rust (-?\d+)$")
PADDED_SLOT_DIGEST = re.compile(r"^/rows/public\.slot_reads/base_missing/digest: ")
# What a different entry at one position may change beside its id, as each
# side's own row: a served item's surprise (the scores agree within the
# tolerance, or their lines would show), a response entry's counters and
# stamps. Any other field (text, source, bank, tags, supersession) keeps it new.
TIE_EVENT_FIELDS = frozenset({"entry_id", "components.surprise"})
TIE_RESPONSE_FIELDS = frozenset({"id", "access_count", "surprise_score", "timestamp"})
TIE_ITEM = re.compile(r"^(?P<at>.*\[\d+\])\.(?P<field>[\w.]+): python (?P<py>\S+) vs rust (?P<rs>\S+)$")
TIE_SERVED = re.compile(r"^(?:db )?(?:public\.)?retrieval_events\[(-?\d+)\]\.served\[(\d+)\]$")
TIE_ORDERED_VIA = ("contiguity", "timeline")
# Rules B and D read a response position only in an entries list (w2d's and
# MCP's search entries, w2e's step answers); any other id list keeps it new.
TIE_ENTRIES = re.compile(r"(?:^|\.)entries\[\d+\]$")


def _tie_items(line: str) -> tuple[str, list[str]] | None:
    """A w2e or w2d_search line's context ("step N", "case <name>", "db") and
    its difference items, or None for a line of another shape."""
    m = W2E_STEP.match(line)
    if m:
        diff = _w2e_diff(line[m.end():])
        return (f"step {m.group(1)}", [diff]) if diff is not None else None
    rest = HEAD.sub("", line, count=1)
    if line.startswith("DB DIFF "):
        return "db", [rest]
    case = CASE.match(rest)
    if case:
        try:
            items = ast.literal_eval(case.group(2))
        except (ValueError, SyntaxError):
            return None
        if isinstance(items, list) and items and all(isinstance(i, str) for i in items):
            return f"case {case.group(1)}", items
    return None


def _same_text(proof: dict) -> bool:
    md5 = proof.get("text_md5") or {}
    return md5.get("python") is not None and md5.get("python") == md5.get("rust")


def _found(proof: dict | None) -> bool:
    """Both sides' served item was found by id in its own event."""
    return bool(proof) and all((proof.get("found") or {}).get(s) for s in ("python", "rust"))


def _slot_md5(entity, attribute) -> str:
    """tie_proofs.slot_md5, which a forced run cannot import (the runner puts
    only this directory on its path); test_forced pins the two equal."""
    return hashlib.md5(f"{entity}\x1f{attribute}".encode("utf-8")).hexdigest()


def _shared_equal(maps) -> bool:
    """Two sides' {slot md5: md5} maps agree on every slot both hold."""
    a, b = (maps or {}).get("python"), (maps or {}).get("rust")
    return isinstance(a, dict) and isinstance(b, dict) and all(a[k] == b[k] for k in a.keys() & b.keys())


def _padded_items(attempt: dict, parsed: list) -> set[tuple[int, int]]:
    """Ruling E: the (line, item) indexes a padded case explains, each a
    provable consequence of cortex membership or score.

    * A case's cortex block, when the harness marked the case padded
      (``padded-query``: its own query ends in whitespace) and every cortex
      slot both sides served has the same value hash.
    * The bank's ``served_facts`` of an event logged with a marked case's
      query (``padded-event``, matched by the query's md5), when every slot
      both sides served has the same kind, contested and pinned hash.
    * ``slot_reads`` rows, base_missing count and digest, when the harness's
      full comparison of the two tables (``slot-reads``; the printed diff
      caps its rows) differs only in slots one side alone served there."""
    proofs = attempt.get("tie_proofs") or {}
    cases, queries = set(), set()
    for ctx, ps in proofs.items():
        for p in ps:
            if (isinstance(p, dict) and p.get("kind") == "padded-query" and isinstance(p.get("query_md5"), str)
                    and _shared_equal(p.get("cortex"))):
                cases.add(ctx)
                queries.add(p["query_md5"])
    events, one_sided, reads = set(), set(), None
    for p in proofs.get("db", []):
        if not isinstance(p, dict):
            continue
        if (p.get("kind") == "padded-event" and isinstance(p.get("event"), int)
                and p.get("query_md5") in queries and _shared_equal(p.get("facts"))):
            events.add(p["event"])
            one_sided |= set(p["facts"]["python"]) ^ set(p["facts"]["rust"])
        elif p.get("kind") == "slot-reads" and isinstance(p.get("slots"), list):
            reads = set(p["slots"])
    reads_ok = bool(reads) and reads <= one_sided
    out: set[tuple[int, int]] = set()
    for li, entry in enumerate(parsed):
        if entry is None:
            continue
        ctx, items = entry
        for ii, item in enumerate(items):
            where = item.split(": ", 1)[0]
            if ctx in cases and PADDED_CORTEX.match(where):
                out.add((li, ii))
            elif ctx == "db":
                facts = PADDED_FACTS.match(where)
                if facts and int(facts.group(1)) in events:
                    out.add((li, ii))
                    continue
                if not reads_ok:
                    continue
                row = PADDED_SLOT_ROW.match(item)
                if row:
                    try:
                        values = ast.literal_eval(row.group(2))
                    except (ValueError, SyntaxError):
                        continue
                    if (isinstance(values, list) and len(values) >= 2
                            and _slot_md5(values[0], values[1]) in reads):
                        out.add((li, ii))
                elif PADDED_SLOT_COUNT.match(item) or PADDED_SLOT_DIGEST.match(item):
                    out.add((li, ii))
    return out


_SURPRISE_TOL: list = []


def _surprise_tol() -> float | None:
    """w2e's stored surprise tolerance (goldens/w2e-tolerance.json), the
    one events_diff compares served surprise within; None when unreadable,
    so no swap is accepted."""
    if not _SURPRISE_TOL:
        try:
            _SURPRISE_TOL.append(float(json.loads((HARNESS / "goldens" / "w2e-tolerance.json")
                                                  .read_text(encoding="utf-8"))["surprise_abs"]))
        except (OSError, ValueError, KeyError, TypeError):
            _SURPRISE_TOL.append(None)
    return _SURPRISE_TOL[0]


def _surprise_crosses(a: dict, b: dict) -> bool:
    """Rule C: each entry carries its own surprise to its new position, so
    Python's at p matches Rust's at p+1 and the other way round."""
    tol = _surprise_tol()
    sa, sb = a.get("surprise") or {}, b.get("surprise") or {}
    vals = (sa.get("python"), sa.get("rust"), sb.get("python"), sb.get("rust"))
    if tol is None or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return False
    return abs(sa["python"] - sb["rust"]) <= tol and abs(sa["rust"] - sb["python"]) <= tol


def _tie_only(attempt: dict, rules=None) -> set[str]:
    """The ACCEPTED_DIVERGENCES names ("tie-order", "padded-query") under
    which every difference of an attempt is proved by the enabled ``rules``
    (TIE_RULES when None), or an empty set: no difference, or one no rule
    proves. Lines are grouped by position (context and path); a position is
    covered whole or not at all."""
    rules = TIE_RULES if rules is None else rules
    proofs = {(ctx, p.get("at")): p for ctx, ps in (attempt.get("tie_proofs") or {}).items()
              for p in ps if isinstance(p, dict)}
    groups: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    order: dict[str, int] = {}
    parsed = [_tie_items(line) for line in attempt["diffs"]]
    padded = _padded_items(attempt, parsed) if "padded-query" in rules else set()
    if not attempt["diffs"]:
        return set()
    for li, entry in enumerate(parsed):
        if entry is None:
            return set()
        ctx, items = entry
        order.setdefault(ctx, len(order))
        for ii, item in enumerate(items):
            if (li, ii) in padded:
                continue
            m = TIE_ITEM.match(item)
            if not m:
                return set()
            groups.setdefault((ctx, m["at"]), {})[m["field"]] = (m["py"], m["rs"])
    if not groups:
        return {"padded-query"}  # every item was a padded case's (ruling E)
    covered: set = set()
    picks = []  # response-side duplicate picks: (context order, python id, rust id)
    if "duplicate-pick" in rules:
        for key, fields in groups.items():
            ctx, at = key
            served = TIE_SERVED.match(at)
            ident = "entry_id" if served else "id"
            allowed = TIE_EVENT_FIELDS if served else TIE_RESPONSE_FIELDS
            p = proofs.get(key)
            if (ident in fields and set(fields) <= allowed and (served or TIE_ENTRIES.search(at))
                    and p and p.get("kind") == "pick" and _same_text(p)
                    and (str(p.get("python")), str(p.get("rust"))) == fields[ident]
                    and (not served or _found(p))):
                covered.add(key)
                if not served:
                    picks.append((order[ctx], p["python"], p["rust"]))
    if "adjacent-swap" in rules:
        for key, fields in groups.items():
            ctx, at = key
            served = TIE_SERVED.match(at)
            if key in covered or not served or set(fields) - TIE_EVENT_FIELDS or "entry_id" not in fields:
                continue
            partner = (ctx, f"{at[:at.rindex('[')]}[{int(served.group(2)) + 1}]")
            other = groups.get(partner)
            if (other is None or partner in covered or set(other) - TIE_EVENT_FIELDS
                    or "entry_id" not in other):
                continue
            (a_py, a_rs), (b_py, b_rs) = fields["entry_id"], other["entry_id"]
            pair = [proofs.get(key), proofs.get(partner)]
            if a_py == b_rs and a_rs == b_py and all(
                    _found(q) and q.get("kind") == "pick"
                    and (str(q.get("python")), str(q.get("rust"))) == groups[k]["entry_id"]
                    and not any((q.get("via") or {}).get(s) in TIE_ORDERED_VIA for s in ("python", "rust"))
                    for q, k in zip(pair, (key, partner))) and _surprise_crosses(*pair):
                covered.update((key, partner))
    if "access-count-follow-on" in rules:
        for key, fields in groups.items():
            ctx, at = key
            p = proofs.get(key)
            if (key in covered or set(fields) != {"access_count"} or not TIE_ENTRIES.search(at)
                    or not p or p.get("kind") != "count" or not isinstance(p.get("id"), int)):
                continue
            try:
                py, rs = (int(v) for v in fields["access_count"])
            except ValueError:
                continue
            extra = sum((r == p["id"]) - (q == p["id"]) for when, q, r in picks if when < order[ctx])
            if extra and rs - py == extra:
                covered.add(key)
    if covered != set(groups):
        return set()
    return {"tie-order"} | ({"padded-query"} if padded else set())


def _composed(attempt: dict) -> set[str]:
    """Ruling F (the conductor, under the maintainer's delegation of
    2026-11-11): the ACCEPTED_DIVERGENCES names under which every line of
    an attempt is covered, or an empty set. Its community lines must pass
    the community rule unchanged (at least one partition line among them);
    every other line must be a position the tie or padded-query rules prove.
    It accepts no new kind of difference, only the sum of accepted ones."""
    kinds = [_community_line(line) for line in attempt["diffs"]]
    rest = [line for line, kind in zip(attempt["diffs"], kinds) if kind is None]
    used: set[str] = set()
    if len(rest) < len(kinds):
        if "partition" not in kinds:
            return set()
        used.add("community-partition")
    if rest:
        proved = _tie_only({**attempt, "diffs": rest})
        if not proved:
            return set()
        used |= proved
    return used


def _oom_total(*attempts) -> int | None:
    measured = [a["oom_kills"] for a in attempts if a and a.get("oom_kills") is not None]
    return sum(measured) if measured else None


# A real bank's own rows can stand in for a seed's fixture, which then may not
# be the case the scenario names: reported beside the counts.
FIXTURE_NOTES = {
    "w2e/dream-deep-exclusion": "the real bank already holds an entity named 'release-checklist', "
                                "which stands in for the seed's lesson-only one, so the lesson-only "
                                "exclusion case may not be exercised on the real copy",
}


def _seed_and_copy_comments(copy: str) -> tuple[str, str]:
    """The seed's provenance and ``copy``'s stamp, from the mirror PostgreSQL."""
    sys.path.insert(0, str(HARNESS))
    import pgdisposable
    with pgdisposable._admin() as conn:
        def comment(name):
            row = conn.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                               "WHERE datname = %s", (name,)).fetchone()
            return row[0] if row else None
        return comment(os.environ.get("PL_HARNESS_BASE_DATABASE", "")), comment(copy)


def base_comments() -> tuple[str, str]:
    """The seed's provenance and the quiet copy's."""
    return _seed_and_copy_comments(QUIET_DB)


def curation_comments() -> tuple[str, str]:
    """The seed's provenance and the curation copy's."""
    return _seed_and_copy_comments(CURATION_DB)


def run_scratch() -> Path:
    """This run's own marker: a leftover is reaped only under it, so two runs
    on one host never stop each other's daemons."""
    return Path("/tmp/forced") / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}-" \
        f"{os.urandom(3).hex()}"


def counts(report: dict) -> dict:
    out = {}
    for suite, body in report["suites"].items():
        classes = [r["class"] for r in body["rows"]]
        out[suite] = {"planned": body["planned"], "run": len(classes),
                      **{c: classes.count(c) for c in ("pass", "transient", "preexisting", "new",
                                                        "accepted", "excluded", "error")},
                      "differences": sum(r["differences"] for r in body["rows"]),
                      # Errors whose attempts saw the runner's cgroup kill a process.
                      # Null when no attempt could read the cgroup: not measured is not none.
                      "errors_with_oom": None if all(r.get("oom_kills") is None for r in body["rows"])
                      else sum(1 for r in body["rows"] if r["class"] == "error" and r.get("oom_kills")),
                      "new_scenarios": [r["scenario"] for r in body["rows"] if r["class"] == "new"]}
    return out


def new_drop_token() -> str:
    """A run's own tombstone token (pgdisposable.DROP_TOKEN)."""
    return uuid.uuid4().hex[:12]


def tombstone_prefix(token: str) -> str:
    sys.path.insert(0, str(HARNESS))
    import pgdisposable
    return pgdisposable.tombstone_prefix(token)


def drop_tombstones(token: str) -> int:
    """The run's renamed-away databases, dropped (pgdisposable's)."""
    sys.path.insert(0, str(HARNESS))
    import pgdisposable
    return pgdisposable.drop_tombstones(token)


def sweep_leftovers(spare: str = "", attempt: str = "") -> int:
    """After a killed harness: drop the disposable databases it left. The
    mirror's PostgreSQL holds only this run's copies besides the pl_cf_mir_*
    banks, and scenarios run one at a time. Names starting with ``spare``
    (the run's own tombstones, which its dropper drops) are left alone. With
    ``attempt`` (``--jobs``: other workers' copies are live) only the
    databases that attempt's harness named."""
    sys.path.insert(0, str(HARNESS))
    import pgdisposable
    from psycopg import sql
    dropped = 0
    with pgdisposable._admin() as conn:
        pgdisposable.assert_disposable_database(conn)
        for name in leftover_names(conn, spare, attempt):
            pgdisposable.refuse_production_database(name)
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            dropped += 1
    return dropped


def leftover_names(conn, spare: str = "", attempt: str = "") -> list[str]:
    """The databases ``sweep_leftovers`` drops: every disposable copy but the
    pl_cf_mir_* banks and names starting with ``spare``; with ``attempt``,
    only names whose suffix that attempt's token starts (run_names)."""
    if attempt and not re.fullmatch(r"[0-9a-f]{8}", attempt):
        raise ValueError(f"attempt token {attempt!r}: expected 8 hex digits")
    return [name for (name,) in conn.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE 'pl\\_cf\\_%%' "
        "AND datname NOT LIKE 'pl\\_cf\\_mir\\_%%' "
        "AND (%s = '' OR NOT starts_with(datname, %s)) "
        "AND (%s = '' OR datname ~ ('_' || %s || '[0-9a-f]{8}$'))",
        (spare, spare, attempt, attempt)).fetchall()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rust-bin", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--suites", nargs="*", default=list(SUITES), choices=list(SUITES))
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--stop-file", type=Path, default=Path("/control/STOP"))
    ap.add_argument("--jobs", type=int, default=1,
                    help="scenarios run side by side, each on its own worker (default 1: one "
                         "at a time, as every run before; the maintainer picks N for the box)")
    args = ap.parse_args(argv)
    if args.jobs < 1:
        print(f"forced.py: --jobs {args.jobs}: run at least one scenario at a time", file=sys.stderr)
        return 2
    if args.suites == []:
        # Given but empty plans nothing, and the run would report itself complete.
        print("forced.py: --suites was given no suite names, so nothing was run; pass the suites "
              "to run, or omit --suites to run them all", file=sys.stderr)
        return 2
    if args.only == []:
        # Given but empty (a name list that came out empty) is not "no
        # filter": it would plan every scenario of the chosen suites.
        print("forced.py: --only was given no scenario names, so nothing was run; pass the names "
              "to run, or omit --only to run every scenario", file=sys.stderr)
        return 2
    if args.only:  # a misspelt name must not make an empty, "complete" run
        known = {n for suite in args.suites for n in scenario_names(suite)}
        unknown = sorted(set(args.only) - known)
        if unknown:
            print(f"forced.py: no such scenario in {args.suites}: {unknown}", file=sys.stderr)
            return 2
    base = os.environ.get("PL_HARNESS_BASE_DATABASE", "")
    # The address the harnesses will use: pgdisposable's own default when unset.
    host = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT") or "127.0.0.1:5433"
    try:  # the run's own refusal, before any database is created
        seed.refuse_live_dsn(f"postgresql://runner@{host}/{base}")
    except seed.Refused as e:
        print(f"forced.py: {e}", file=sys.stderr)
        return 3
    planned = {suite: [n for n in scenario_names(suite) if not args.only or n in args.only]
               for suite in args.suites if suite in QUIET_PREFIXES or suite in CURATION_PREFIXES}
    # An excluded scenario's real-bank arms never run, so it needs no quiet copy.
    if any(base_for(suite, n)[0] == "quiet" and f"{suite}/{n}" not in REAL_BANK_EXCLUDED
           for suite, names in planned.items() for n in names):
        try:  # a reseed drops the quiet copy; an old one would compare an older backup
            seed.quiet_preflight(*base_comments())
        except seed.Refused as e:
            print(f"forced.py: {e}", file=sys.stderr)
            return 3
    if any(base_for(suite, n)[0] == "curation" for suite, names in planned.items() for n in names):
        try:  # dropped by a reseed too
            seed.curation_preflight(*curation_comments())
        except seed.Refused as e:
            print(f"forced.py: {e}", file=sys.stderr)
            return 3
    args.out.mkdir(parents=True, exist_ok=True)
    token = new_drop_token()
    tombstones = tombstone_prefix(token)
    dropper = Dropper(lambda: drop_tombstones(token))
    scratch = run_scratch()
    if args.jobs == 1:
        runner = Runner(args.rust_bin, args.out, args.timeout, args.stop_file,
                        sweep=lambda attempt: sweep_leftovers(spare=tombstones), scratch=scratch,
                        drop_token=token, dropper=dropper)
        workers = [runner]
    else:
        # Each worker its own scratch root (its reaping marker), each attempt's
        # sweep only that attempt's databases, one base read for the run, and
        # no per-attempt OOM count: the runner's cgroup counts every worker's.
        workers = [Runner(args.rust_bin, args.out, args.timeout, args.stop_file,
                          sweep=lambda attempt: sweep_leftovers(spare=tombstones, attempt=attempt),
                          scratch=scratch / f"w{n}", oom_kills=lambda: None,
                          drop_token=token, dropper=dropper, base_cache=scratch / "base-cache")
                   for n in range(args.jobs)]
        runner = workers[0]
    try:
        report, private = run(runner, args.suites, args.only, workers=workers)
    finally:
        if args.jobs > 1:
            # Each attempt swept only its own databases; once every worker has
            # stopped nothing else is live, so a run-wide sweep is safe again.
            try:
                swept = sweep_leftovers(spare=tombstones)
                if swept:
                    print(f"forced.py: swept {swept} leftover disposable databases", file=sys.stderr)
            except Exception as e:  # noqa: BLE001 - the drain below still runs
                print(f"forced.py: the closing sweep failed: {type(e).__name__}: {e}", file=sys.stderr)
        # Every drop ends before the summary is written, a stopped run's too.
        drops_failed = dropper.drain()
        for error in dropper.errors:
            print(f"forced.py: a background drop pass failed and the final pass retried it: {error}",
                  file=sys.stderr)
    summary = {"base": base, "complete": report["complete"], "stopped": report.get("stopped"),
               "not_forced": report["not_forced"], "quiet_base": quiet_base_use(report),
               "curation_base": curation_base_use(report),
               "accepted_limits": ACCEPTED_OOM,
               "accepted_divergences": ACCEPTED_DIVERGENCES,
               "real_bank_exclusions": REAL_BANK_EXCLUDED,
               "fixture_notes": FIXTURE_NOTES,
               "counts": counts(report),
               # Above 1, attempts report no OOM count (the cgroup's counts every
               # worker), so an ACCEPTED_OOM scenario reads as an error.
               "jobs": args.jobs,
               # The run's own databases still on the server, and why: the run fails.
               "drops_failed": drops_failed,
               "background_drop_errors": len(dropper.errors)}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (args.out / "private.json").write_text(json.dumps({"report": report, "differences": private},
                                                      indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary["counts"]))
    clean = all(not c["new"] and not c["error"] and not body.get("error")
                for c, body in zip(summary["counts"].values(), report["suites"].values()))
    if drops_failed:
        print(f"forced.py: the run's databases were not all dropped: {drops_failed}", file=sys.stderr)
    return 0 if report["complete"] and clean and not drops_failed else 1


if __name__ == "__main__":
    sys.exit(main())
