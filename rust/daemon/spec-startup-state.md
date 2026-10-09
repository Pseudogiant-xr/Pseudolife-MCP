# Contract: complete startup inputs before serving

Sources: `storage/sync.py:107-155,273-316,372-412,474-511` and
`service.py:1308-1350,1435-1467,1638-1664`. This closes the pre-serve seam
specified in `spec-pg-hydrate.md`; read/write semantics remain with W2-D/E.

| Item | Contract |
|---|---|
| Publication | Build the bank and every required snapshot in locals; attach both to the cached `Ready` only after every required load succeeds. A required-loader failure abandons all stores and keeps the existing backoff. Keep storage/embedder across retry. Returning `Ready` to a caller also requires successful clock reseeding. |
| Entries | Load IDs and original float32 vectors with every producer field. Seat in the matching band, or the first band after a preset rename. Overflow moves the lowest retention-scoring entries shallow-to-deep, keeping insertion-order ties and every deepest-band overflow row. |
| Stamp writes | Follow each relocation in source order, then reconcile remaining stale stamps. These cosmetic writes are tolerant; failed writes log and do not erase an entry. IDs, history and unrelated row values never change. |
| Writer statement ownership | Bank hydration owns the shared txn::with_client lock across its read/seating/stamp group; metadata, episode/canonical reads and late reseeding use the same adapter. Borrowed-client bodies never nest adapters. An abandoned transaction must roll back before stamp writes, so a returned repair is committed. |
| Episodes | Preserve `ORDER BY started_at`, every nullable producer field, and the last open episode as the current pointer. Add no invented tie key. |
| Canonical inputs | Cortex, world and lesson arrays retain the complete storage-loader row shapes, IDs, member/scalar kinds, historical rows, nullable vectors, labels and temporal stamps. Binary vectors promote float32 components to float64 JSON; scalar REAL fields follow psycopg's server-text-to-double decoding. SQL DOUBLE PRECISION fields remain JSON floats even when integral. |
| Hydrated vector checks | After entries, episodes and Cortex load, count every non-null wrong-size entry/Cortex vector and report the sorted dimensions together, before loading world/lessons. This follows service.py's check; its scope covers entries and Cortex. |
| Identity metadata | Reload the active-session pointer, filtered episode tombstones, deferred empty roots, cortex supersession log and dream cursor. No principal or unrelated secret metadata enters this snapshot. |
| HLC | Compute the greatest persisted `(physical, logical)` across every cortex/world/lesson record and the latest coordination high-water row. Read coordination after store loads because its independent writers can advance it during initialization. Missing metadata means no additional stamp. A malformed pair or late read failure retains the fully loaded stores with `hlc_reseed_pending = true`; the next ensure-init retries only the clock, without backoff or reinitialization. Healthy init calls skip the clock read. |
| Refusal | A missing required loader column abandons the candidate. A malformed late HLC retains fully loaded stores with a pending flag and blocks returning Ready until reseeding succeeds. The source may already have repaired cosmetic band stamps; cross-arm DB state still compares exactly, and all other histories/values remain unchanged apart from the constructor epoch. |

`StartupState` carries `episodes: Vec<Value>`, `current_episode: Option<String>`,
`cortex/world/lessons: Vec<Value>` and normalized `metadata: Value`. Its
clock snapshot contains `hlc_highwater: (i64, i64)` and
`hlc_reseed_pending: bool`, read together under a lock. W2-D/E adopt these
complete loader inputs and seed their clock from the maximum before their
first tick; a pending reseed must block ticking.
No startup code changes their scoring, slot mutations, reconnect or flush
semantics. The HLC domain is the signed SQL BIGINT producer domain; synthetic
unbounded integers and Python-only value coercions remain deferred by name.

The differential harness uses fixed CPU vectors and real Python storage/sync
producers on independently cloned disposable banks. It compares complete
snapshots and whole catalogs/rows after initial hydration and restart,
including scalar/member history, null vectors, presets, capacity overflow,
identity metadata, HLC maximum selection and required-loader failure.
Compiled mutants remove a world load, the HLC seed, tombstones, seating,
stamp persistence or the Cortex dimension check. A separate actual-service
cell corrects a malformed clock, drops a required loader column, and verifies
clock-only retry with every store/storage/embedder identity retained. It then
malforms the durable clock again and proves healthy calls skip the read;
six full DB states compare after each write and init. A seventh mutant
reinitializes on a pending clock and must fail that recovery. Committed oracle
snapshots permit the same CI replay.

A canceled-writer PostgreSQL case verifies that seating commits its stamp and
the preceding uncommitted marker rolls back. The eighth mutant bypasses the
shared adapter; an independent reader then observes the stale band while the
writer sees its uncommitted marker, making the missing guard load-bearing.

Optional Chroma/reference initialization, legacy file migration and dream
tracking remain their separately named integration scopes; this input seam
does not port Chroma-backed behavior or introduce a new schema.
