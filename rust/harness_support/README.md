# Shared Rust harness support

This package holds small test-only helpers shared by the Python drivers of
the Rust binaries. Disposable-bank guards and HTTP readiness interpretation
belong here; product implementations, scenario expectations, schema ownership
and process lifecycle belong with their respective owners.

`database_guard.py` owns disposable-bank refusal, `readiness.py` interprets
HTTP readiness, and `schema.py` executes the daemon's committed schema plan.
`bank_fixture.py` uses that plan and seeds the built-in relations and writer
epoch for the dream, graph and principal harnesses.
The schema executor contains no DDL and imports no product code. It runs in
one transaction, or a savepoint inside a caller-owned transaction, and refuses
unknown operations. On Linux the `cli-startup` schema step compares its complete
catalog and rows with a schema-only Rust probe; it does not seed relations or
advance the writer epoch as a full storage open does. Other OSes skip this
comparison because the plan and PostgreSQL semantics are client-OS independent.
The existing schema-test discovery runs a non-spawning parsed-plan/SQL hash
check on both OSes, including CRLF, UTF-8 and paths containing spaces. A schema
artifact change updates that hash alongside the Linux equivalence proof.

Import helpers by their repository-qualified name:

```python
from rust.harness_support import readiness
from rust.harness_support.database_guard import assert_disposable_database
```

A directly executed harness must put the repository root on `sys.path`
before that import, as `daemon/harness/daemons.py` does. Do not add an installed
checkout or an oracle source to find these helpers.

Nothing in this package may import `pseudolife_memory`, directly or
transitively. The single temporary exception is the explicitly named guard
decision comparison test in `test_database_guard.py`; delete that test when
the frozen package is removed. All other tests and helpers must work without
the package. The blocked-import test evicts cached modules before importing
the shared harness modules and exercises the lazy database-state guard too.

Call the production-name guard before opening a target, then
`assert_disposable_database(conn)` on the same connection before DDL, reaping
connections or resetting rows. Keep the server check and the harness's own
name/ownership restrictions: neither substitutes for the other. Unknown
production-bank identity fails closed; never log a DSN or credentials.

The readiness helper reads only HTTP responses. Preserve the distinction
between ready, selected stages completed, and merely answering HTTP; callers
choose which one their scenario requires.

Run `python -m pytest rust/harness_support -q` and the affected harness steps.
The security golden subset can replay with product imports blocked; other
scenario seeders and manual Python-oracle modes are separate migration work.
