# Porting dossiers

These dossiers map unfinished, unowned parity rows to their Python contracts,
shipped inputs and oracle tests. They are preparation for a port, not evidence
that Rust implements the row. Proposed harness cases and mutants have not run.

## Reading and format

Each dossier names the source commit used for its repository-relative
`path:line` references. Resolve those references with `git show <commit>:<path>`;
later changes do not move the dossier's source identity. A `path::test` names
an original oracle test, not an assertion that the test can execute Rust.

Every dossier has nine sections:

1. What the row promises: the observable contract in two or three sentences.
2. Entry points: Python functions/routes and their call graph to storage.
3. State touched: persistent and resident state, SQL locations and transactions.
4. Shipped producers: exact input shapes and the source that sends them.
5. Python incidentals to defer: producer-unreached behavior to leave out by name.
6. Oracle tests: behavioral pins separated from Python implementation pins.
7. Proposed harness cases and mutants: implementable cases and four to eight breaks.
8. Dependencies and risks: ordering, concurrency, clocks, platforms and other rows.
9. Effort estimate: small, medium or large, with its reason.

`Unverified` marks an open source or execution question; it is never a contract
assertion. A dossier may explicitly leave part of an inventory incomplete.
The Python oracle and its immutable tests take precedence over this source map.
Use [SEMANTICS-CHECKLIST.md](../SEMANTICS-CHECKLIST.md) and the shared
[daemon harness](../daemon/harness/run.py), rather than inventing a comparator.
Compare durable state after writes; a read-shaped endpoint can write state.

Producer attribution currently says **dossier author's source census**.
`rust/producer-census.json` was not present at the source commit. Once that
artifact lands, reconcile its cited producer source revision and unresolved
dynamic calls before replacing these local shape inventories. Arbitrary model
tool arguments are not a closed corpus merely because the schema is known.

## Ownership and priority

Selection uses `rust/PARITY.md:231` at
`3b4de2c515bee07e97cd35b6e1473ea48511ec77`, the contract-first plan's slice
assignments and the live/paused coordination board checked on 2026-10-10.
Rows remain deferred; this directory changes no parity status or ownership.

The first two rows cover bank opening and configuration: all later service
slices depend on their effective state. Subsequent batches cover compatibility,
HTTP write/hook wrappers and remaining operator workflows. During preparation,
new embedding and HTTP-layer authors registered; their completed draft source
maps were removed from this public batch and handed to those owners.

| Priority | Row | Dossier | Preparation state |
|---|---|---|---|
| 1 | PG-HYDRATE | [PG-HYDRATE.md](PG-HYDRATE.md) | Source map; current schema/catalog/recovery |
| 1 | CONFIG-LOAD | [CONFIG-LOAD.md](CONFIG-LOAD.md) | Source map; peripheral reader census incomplete |
| 2 | DISPOSABLE-GUARDS | [DISPOSABLE-GUARDS.md](DISPOSABLE-GUARDS.md) | Production-name refusal and reset ordering |
| 2 | STORAGE-COMPAT | [STORAGE-COMPAT.md](STORAGE-COMPAT.md) | Legacy import/checkpoints; embedded/Chroma deferrals |
| 3 | HTTP-WRITES | [HTTP-WRITES.md](HTTP-WRITES.md) | Full POST registry; transitive owned storage gaps named |
| 3 | HOOKS | [HOOKS.md](HOOKS.md) | Eight wrappers and producer wiring; launcher gaps named |
| 4 | PLUGIN-COMMANDS | [PLUGIN-COMMANDS.md](PLUGIN-COMMANDS.md) | Command/guard workflows; backend dependencies named |
| 5 | OPS-MAINTENANCE | [OPS-MAINTENANCE.md](OPS-MAINTENANCE.md) | Six distinct operator contracts; no live execution |

Excluded owned scopes: Wave 2 retrieval/read service, writes (including
CONTRADICTION), board/delivery/principals/mail-ring and MCP projections
(including SEARCH-COMPACTION); embedding/ONNX prerequisite; HTTP limits,
static/security/health; W3-H dream/graph; W3-I background and its
curation/recovery stretch; W1-C CLI leaves; the lease remainder; and W3-J's
image, compose, YAML repair and cutover checklist. The latter covers
OPS-INSTALL-RESTORE and SHIM-CLIENT-UPDATES preparation. HTTP wrapper dossiers
do not take ownership of their service implementations. Recheck the board
before implementing a row: this is a dated selection, not a reservation.
