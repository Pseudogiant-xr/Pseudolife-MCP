"""Proof records for the forced mirror run's tie-order acceptance
(mirror/forced.py ``TIE_RULES``), written beside a step's or case's diffs.

A harness that compares two daemons' served entries prints a difference
wherever the two sides served a different entry at one position. Whether
that is a tie the spec leaves free cannot be read from the line: it names
two ids, never their texts. The harness can prove it while both disposable
banks still exist, so it records, for each such line only:

* ``pick``: the line's two ids and an md5 of each entry's text (from the
  response's own entries, or ``md5(text)`` computed in SQL in both banks),
  never the text itself; and each side's ``via`` for a served list.
* ``count``: for an ``access_count`` difference at a position where both
  sides served the same entry, that entry's id.

Positions that do not differ get no record, so a passing step writes none,
and nothing here changes what is compared.
"""
from __future__ import annotations

import hashlib
import re

# "<at>.id: python 69 vs rust 65" (a response's entries) and
# "<at>.entry_id: python 69 vs rust 65" (a retrieval event's served list),
# "<at>" ending in the position's index.
PICK_LINE = re.compile(r"^(?P<at>.*\[\d+\])\.(?:id|entry_id): python (?P<py>-?\d+) vs rust (?P<rs>-?\d+)$")
COUNT_LINE = re.compile(r"^(?P<at>.*\[\d+\])\.access_count: python -?\d+ vs rust -?\d+$")
TOKEN = re.compile(r"\.(\w+)|\[(\d+)\]")
# A retrieval event's served position, as w2e (events_diff, "db " prefixed)
# and w2d_search (bank_diff) print it.
SERVED_AT = re.compile(r"^(?:db )?(?:public\.)?retrieval_events\[(-?\d+)\]\.served\[(\d+)\]$")


def _md5(text) -> str | None:
    return hashlib.md5(text.encode("utf-8")).hexdigest() if isinstance(text, str) else None


def _walk(value, path: str):
    """The value at a printed path (".value.entries[2]"), or None."""
    pos = 0
    for m in TOKEN.finditer(path):
        if m.start() != pos:
            return None
        pos = m.end()
        key, index = m.groups()
        if key is not None:
            value = value.get(key) if isinstance(value, dict) else None
        else:
            i = int(index)
            value = value[i] if isinstance(value, list) and i < len(value) else None
        if value is None:
            return None
    return value if pos == len(path) else None


def _parent(at: str) -> tuple[str, int]:
    head, _, index = at.rpartition("[")
    return head, int(index.rstrip("]"))


def _by_id(items, key: str, wanted: int):
    """The dict in ``items`` whose ``key`` is ``wanted`` (the tie alignment
    may have moved it, so a position alone does not find it)."""
    for item in items if isinstance(items, list) else ():
        if isinstance(item, dict) and item.get(key) == wanted:
            return item
    return None


def response_proofs(lines: list[str], python, rust) -> list[dict]:
    """Proofs for a response's lines: ``python``/``rust`` are the compared
    values the lines' paths start from (w2e's step answer, w2d's JSON body)."""
    out = []
    for line in lines:
        if line.startswith("db "):  # w2e's bank lines: event_proofs
            continue
        m = PICK_LINE.match(line)
        if m and not SERVED_AT.match(m["at"]):
            head, _ = _parent(m["at"])
            a = _by_id(_walk(python, head), "id", int(m["py"]))
            b = _by_id(_walk(rust, head), "id", int(m["rs"]))
            out.append({"at": m["at"], "kind": "pick", "python": int(m["py"]), "rust": int(m["rs"]),
                        "text_md5": {"python": _md5((a or {}).get("text")),
                                     "rust": _md5((b or {}).get("text"))}})
            continue
        m = COUNT_LINE.match(line)
        if m:
            item = _walk(python, m["at"])
            other = _walk(rust, m["at"])
            ident = item.get("id") if isinstance(item, dict) else None
            out.append({"at": m["at"], "kind": "count",
                        "id": ident if isinstance(other, dict) and other.get("id") == ident else None})
    return out


def _served(state: dict | None, event: int) -> list:
    table = (state or {}).get("rows", {}).get("public.retrieval_events") or {}
    cols = table.get("columns") or []
    if "id" not in cols or "served" not in cols:
        return []
    i, s = cols.index("id"), cols.index("served")
    for row in table.get("rows") or []:
        if row[i] == event:
            return row[s] if isinstance(row[s], list) else []
    return []


def event_proofs(lines: list[str], python_events: dict | None, rust_events: dict | None,
                 text_md5) -> list[dict]:
    """Proofs for retrieval-event lines. ``*_events`` are each side's dump
    holding ``public.retrieval_events`` (raw, before any alignment);
    ``text_md5(ids)`` returns ``{id: md5}`` read in both banks, an id whose
    two banks disagree or that either lacks mapped to None."""
    picks = []
    for line in lines:
        prefix = "db " if line.startswith("db ") else ""
        m = PICK_LINE.match(line[len(prefix):])
        if m and SERVED_AT.match(m["at"]):
            picks.append((prefix, m))
    if not picks:
        return []
    hashes = text_md5({int(m[k]) for _, m in picks for k in ("py", "rs")})
    out = []
    for prefix, m in picks:
        event = int(SERVED_AT.match(m["at"])[1])
        a = _by_id(_served(python_events, event), "entry_id", int(m["py"]))
        b = _by_id(_served(rust_events, event), "entry_id", int(m["rs"]))
        out.append({"at": prefix + m["at"], "kind": "pick", "python": int(m["py"]), "rust": int(m["rs"]),
                    "text_md5": {"python": hashes.get(int(m["py"])), "rust": hashes.get(int(m["rs"]))},
                    "via": {"python": (a or {}).get("via"), "rust": (b or {}).get("via")},
                    "found": {"python": a is not None, "rust": b is not None}})
    return out


def padded(query) -> bool:
    """A query with trailing whitespace around real words: the case shape
    ruling E covers (sentence-transformers 5.4.0 stopped stripping text
    before tokenising; Rust keeps the 5.3.0 strip). A blank query is not."""
    return isinstance(query, str) and query.strip() != "" and query != query.rstrip()


def slot_md5(entity, attribute) -> str:
    return hashlib.md5(f"{entity}\x1f{attribute}".encode("utf-8")).hexdigest()


def padded_event_proofs(queries: list[str], python_events: dict | None,
                        rust_events: dict | None) -> list[dict]:
    """For each retrieval event both sides logged with one of the padded
    ``queries`` (exactly as given; the event logs the query unstripped), the
    md5 of every cortex slot (entity_norm, attribute_norm) its served facts
    hold on one side only. An event whose query differs between the sides,
    or that one side lacks, gets no record."""
    def table(state):
        t = (state or {}).get("rows", {}).get("public.retrieval_events") or {}
        cols = t.get("columns") or []
        if not {"id", "query", "served_facts"} <= set(cols):
            return {}
        i, q, f = cols.index("id"), cols.index("query"), cols.index("served_facts")
        return {row[i]: (row[q], row[f]) for row in t.get("rows") or []}
    a, b = table(python_events), table(rust_events)
    out = []
    for event, (query, facts) in sorted(a.items()):
        if query not in queries or event not in b or b[event][0] != query:
            continue

        def slots(served):
            return {slot_md5(x.get("entity_norm"), x.get("attribute_norm"))
                    for x in (served if isinstance(served, list) else []) if isinstance(x, dict)}
        out.append({"at": f"public.retrieval_events[{event}]", "kind": "padded-event", "event": event,
                    "slots": sorted(slots(facts) ^ slots(b[event][1]))})
    return out


def bank_text_md5(dsns: dict, connect=None) -> "callable":
    """``text_md5`` over both banks: ``connect(dsn)`` opens a connection
    (psycopg's by default). Only the hash leaves the database."""
    if connect is None:
        import psycopg
        connect = psycopg.connect

    def read(ids: set[int]) -> dict:
        if not ids:
            return {}
        seen = []
        for side in ("python", "rust"):
            with connect(dsns[side]) as conn:
                rows = conn.execute("SELECT id, md5(text) FROM entries WHERE id = ANY(%s)",
                                    (sorted(ids),)).fetchall()
            seen.append({int(i): h for i, h in rows})
        return {i: (seen[0].get(i) if seen[0].get(i) == seen[1].get(i) else None) for i in ids}
    return read
