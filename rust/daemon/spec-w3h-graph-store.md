# Graph store contract (W3-H, first increment)

Python `storage/postgres.py` is the oracle for this increment. This adds
the store called by later graph services; it does not register Console GET
routes or close the entire GRAPH-REVIEW row.

| Exact behavior | Python source |
|---|---|
| Entity upsert preserves display and the first non-null type; a fresh mint repairs orphaned fact/lesson subject and object links using graph normalization, atomically | `storage/postgres.py:2732–2827` |
| Canonical names resolve before aliases; aliases are sorted and reassignable; the ID map gives canonicals precedence | `storage/postgres.py:2829–2892`, `:2990–3002` |
| Entity deletion unlinks facts and lessons and cascades graph references; merge deduplicates edges before repointing, removes self-loops, carries aliases/sources and preserves fact/lesson text | `storage/postgres.py:2894–2988` |
| Relation upsert changes metadata while preserving builtin and creation time; registry rows sort by name | `storage/postgres.py:3006–3036` |
| Reassertion uses max(new confidence, old + 0.05), capped at 0.99; origin priority is user > action > agent > other, equal priority chooses new | `storage/postgres.py:3040–3105` |
| Explicit reassertion revives; dream reassertion does not; source evidence is locked, filtered to current entries and inserted in the same transaction, with no edge write when all support is stale | `storage/postgres.py:3058–3104` |
| Bless updates live edges only, never creates/revives; removal updates live edges only | `storage/postgres.py:3107–3139` |
| Graph loads entities and live edges by ID, aliases by name | `storage/postgres.py:4292–4317` |

Inputs are normalized strings, integer database IDs, finite confidence values,
nullable type/origin strings and integer source-entry lists from shipped writers.
Malformed interpreter inputs and file/embedded storage defer by name.
Stored REAL confidence and all IDs, sequence advancement, FK links and durable
state compare exactly. REAL response values use PostgreSQL text-format decoding,
matching Psycopg rather than widening binary float32. SQLSTATE and rollback
state are exact on FK refusal; interpreter traceback/error wording is free.
Wall-clock columns compare within each operation's
captured window; unchanged clock values remain exact. JSON object key order
is free. No score tolerance or per-row timing claim is introduced.

The differential driver reuses `harness/pgdisposable.py`, `dbstate.py` and
`daemons.base_env`. It compares responses and all bank rows/catalog after every
operation and replays recorded Python results in CI. Golden state digests keep
all rows and sequence values; catalog immutability is exact within each arm,
and live runs compare catalogs across arms. Source mutants exercise
confidence, origin, revival, blessing, aliases, orphan repair and merging.
Traversal/order, communities, service/MCP wiring, proposals, review and deep
dream remain later increments. No NetworkX ordering substitution is exercised
by this storage increment.
