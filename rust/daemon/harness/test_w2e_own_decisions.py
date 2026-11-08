"""A scenario's automatic-decision checks read its own decisions only.

On the real bank's copy the meta table already holds the bank's automatic
decisions (2,851 records on the forced run's seed, 2026-11-08). A check that
matched any decision with the right queue and state could then hold, or fail,
on those alone: a false pass or a false difference. Each check now names the
scenario's own pair, entity or rationale, so foreign decisions change nothing.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import w2e  # noqa: E402
import w2e_queue_judges  # noqa: E402

KEY = "automatic_review_decision_v1"


def exists_parts(query: str) -> list[str]:
    """Each ``EXISTS (...)`` in ``query`` that reads decisions, as its own
    SELECT: a conjunction is checked part by part, so a scoped part cannot
    hide an unscoped one by keeping the whole false."""
    parts, at = [], 0
    while (start := query.find("EXISTS (", at)) != -1:
        depth, i = 0, start + len("EXISTS ")
        for i in range(i, len(query)):
            depth += {"(": 1, ")": -1}.get(query[i], 0)
            if depth == 0:
                break
        inner = query[start:i + 1]
        if KEY in inner:
            parts.append("SELECT " + inner)
        at = start + 1
    return parts


def decision_checks() -> list[tuple[str, str]]:
    """Every SELECT a w2e scenario runs that reads automatic decisions, each
    EXISTS part on its own too, and the candidate fingerprint check's query."""
    found = set()

    def walk(name, node):
        if isinstance(node, str):
            if KEY in node and node.lstrip().upper().startswith("SELECT"):
                found.add((name, node))
                found.update((name, part) for part in exists_parts(node))
        elif isinstance(node, dict):
            for value in node.values():
                walk(name, value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(name, value)

    for name, scenario in w2e.SCENARIOS.items():
        walk(name, scenario)  # ops, settles, seeds: wherever a check may sit
    # Run-time: the records Python's candidate fingerprint check compares.
    found.add(("fingerprint_diffs",
               f"SELECT count(*) FROM ({w2e_queue_judges.candidate_records_sql()}) own"))
    return sorted(found)


def test_conjunction_parts_are_checked_on_their_own():
    parts = exists_parts(w2e_queue_judges.CANDIDATE_REOPENED)
    assert len(parts) == 2 and all("value->'pair' IN" in p for p in parts)


def test_the_decision_checks_are_found():
    names = {name for name, _ in decision_checks()}
    assert {"candidate-auto", "candidate-auto-reopen", "junk-auto", "junk-auto-reopen",
            "link-auto", "link-auto-reopen", "fingerprint_diffs"} <= names


# Decisions no scenario made: every queue, state and action, naming pairs,
# entities and rationales that are not the scenarios' own (a real bank's).
FOREIGN = [
    {"queue": queue, "state": state, "action": action, "target": f"{queue}:foreign",
     "pair": ["aaa-foreign", "zzz-foreign"],
     "candidate": {"src": "aaa-foreign", "dst": "zzz-foreign"},
     "proposal": {"id": 990000 + n, "entity_id": 990000 + n, "kind": "junk", "reason": "compound",
                  "rationale": "detector: foreign"}}
    for n, (queue, action) in enumerate((("candidate", "dismiss"), ("junk", "keep"),
                                          ("junk", "delete"), ("link", "reject"),
                                          ("merge", "reject")))
    for state in ("active", "reopened")
]


@pytest.fixture
def bank():
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    import psycopg
    from pseudolife_memory.storage.schema import ensure_schema
    pg = pgdisposable.Slice("w2e")
    name = pg.name("pl_cf_w2e_own_decisions")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            ensure_schema(conn)
            yield conn
    finally:
        pg.drop(name)


def test_foreign_decisions_change_no_scenario_check(bank):
    checks = decision_checks()

    def answers():
        return [bank.execute(query).fetchone()[0] for _, query in checks]

    before = answers()
    for n, record in enumerate(FOREIGN):
        bank.execute("INSERT INTO meta (key, value) VALUES (%s, %s::jsonb)",
                     (f"automatic_review_decision_v1:foreign-{n}", json.dumps(record)))
    after = answers()
    moved = [name for (name, _), b, a in zip(checks, before, after) if b != a]
    assert moved == [], f"checks that read decisions they did not make: {sorted(set(moved))}"
