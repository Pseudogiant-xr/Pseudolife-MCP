# Contract: schema creation and startup hydration

Python sources are `storage/schema.py:1145-1664`,
`storage/postgres.py:383-430`, `storage/sync.py`, and
`service.py:1298-1663`. The generated plan records the full schema source
hash; the historical matrix records each older source commit explicitly.

| Boundary | Required behavior |
|---|---|
| Schema ownership | Generate base DDL, every additive/conditional migration, duplicate healing, trace backfill, dimension probe/refusal text, and metadata from Python. Unknown generator constructs fail closed. |
| Execution | One transaction, original statement order, parameterized values, local timeouts; rollback on any error. The writer lease precedes DDL. |
| Fresh/restart | Identical catalogs and rows to Python after each constructor call; only the writer lease epoch advances on a restart. |
| Older banks | v25 onward follows Python's additive ladder. PostgreSQL v8-v24 has 384-dimensional vectors and refuses before DDL; deliberate embedding migration is required. v1-v7 predates PostgreSQL and is file-mode scope. |
| Future/malformed version | Refuse a future version or non-positive/non-integer version before DDL; preserve all durable state. Python currently overwrites the version, so this is an intentional divergence required by the slice brief. |
| Missing pieces | Preserve Python's additive repair of missing tables/columns/indexes; malformed existing types or failed constraints must abort, never publish a partially hydrated store. |
| Disposable safety | Harness target selection delegates to Python's exact production-name refusal, including unresolved identity, Unicode casefolding and trailing slash removal. Server identity is checked before destructive SQL. The additional slice prefix restricts owned fixture targets. |

SQL is committed as a structured ordered plan rather than a flattened script:
the trace backfill and constraint repairs depend on query results, and healing
values must remain bound parameters. No Python interpreter is called by the
Rust schema executor. Regenerate with
`python rust/daemon/harness/gen_schema_sql.py`; `--check` and the generator
tests reject source changes without regeneration. New literal schema additions
(including the pending v56 document store) and constant-loop migrations are
picked up by that command; new control flow requires an explicit compiler
extension and review. Check current master again before opening the PR.
The `--record-goldens <result.json>` option also builds and records oracle
post-states for every historical cell, after the live control and five source
mutants pass. CI compares the candidate to these committed snapshots as well
as to the live Python constructor.

## Seam with W2-D/E

W3 owns the pre-serve startup barrier, entry seating/band reconciliation and
loading the durable inputs that Python loads before serving. W2-D owns read
semantics; W2-E owns mutations, HLC ticking and reconnect/flush semantics.
W3 will publish episodes, canonical-store rows and metadata together as a
`StartupState` attached to `Ready`, only after every load succeeds. W2-D/E
can adopt those inputs into their stores without a second independent bank
snapshot. W3 does not change scoring or write semantics. Those sessions are
paused; the delegate will relay this seam when they resume.

The schema/migration PR advances PG-HYDRATE; the row remains deferred until
the startup hydration seam has executable differential coverage.

Error wording is free except the dimension refusal text. Catalog shape, row
values, transaction outcomes and refusal/no-write behavior are exact. Only
`relations.created_at` is normalized between independent constructors.
Every newly seeded timestamp must fit its own arm's captured invocation
window before normalization; the same arm's restart timestamps stay exact.
