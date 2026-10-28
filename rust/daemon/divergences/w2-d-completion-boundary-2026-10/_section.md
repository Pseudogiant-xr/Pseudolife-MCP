## W2-D completion boundary (2026-10-13)

This increment serves `GET /api/search`; it does not close HTTP-READS or
RETRIEVAL as a whole. The following omissions are explicit cutover work,
not empty successful responses. Unwired HTTP handlers retain 501 after the
existing gates. The route inventory is `harness/goldens/routes.json`.

Reference hits are served since B7's second Rust PR, as Python serves them
(see "Document store (B7)" below); no schema change is included.
The typed cortex loader uses the live embedder dimension, while the shared
startup and PostgreSQL schema guards still refuse incompatible banks; this
is not authorization to migrate vectors or change the schema dimension.

Search golden recording stores only Python-oracle responses and bank state.
The inherited replay needs the identical seeded template in the same harness
process: seed clocks and generated identities are not portable fixture data,
and the CLI cleans up the template at exit. `w2d-portable-goldens` remains a
named W2-D follow-up for committed CI fixtures; recording a candidate response,
replacing an expected value or widening normalization is not a remedy. Local
same-template oracle recording/replay is separate evidence, not CI coverage.
