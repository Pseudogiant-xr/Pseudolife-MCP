# Shadow write-path audit

The audit starts at state mutations, including the writes hidden in reads.
The inventory command is:

```sh
rg -n 'INSERT INTO|UPDATE [a-z_]+ SET|DELETE FROM|TRUNCATE|\.execute\(|\.write\(|fetch_add|std::fs::write|fs::write|File::create|create_dir_all' rust/daemon/src
```

Test-only occurrences and process-local locks/caches are distinguished from
bank mutations below. This is an inventory of the current implementation;
the MCP and HTTP GET allowlists refuse newly added tools and routes until
audited. A route-inventory test requires every GET to be explicitly classified.

| State / mutation sources | Shadow boundary |
|---|---|
| Writer advisory lease, schema creation/migration/repair, relation seed, `meta.lease_epoch`: `storage/mod.rs`, `storage/schema.rs` | Separate read-only open and reconnect; schema checked using SELECT; neither lease nor epoch touched |
| Dream status initializes acknowledgement state: `dream/service.rs`, `dream/cursors.rs` | Console overview returns `read_only: true, enabled: false`; GET `/api/dream/status` refuses instead of initializing tracking |
| GET duplicates expires `curation_auto_dismissals_v1` and deletes `dismissed_pairs`: `routes/graph.rs`, `dream/curation_judge.rs` | GET `/api/curation/duplicates` refuses before refresh, including with stale markers present |
| `entries.band` reconciliation during `bank.rs` hydration | Seating still computed; session read-only flag suppresses all three write-through paths |
| Entry access counts and tier queries/hits: `search.rs`, `read/search_route.rs`, `read/trace.rs`, `consolidation_tools.rs` | `count_access=false` for shadow, including trace and consolidation ranking |
| Entry dereference, `entries.access_count`, `retrieval_uses`: `read/mcp_reads.rs` | Ordinary SELECT, no `FOR UPDATE`, durable or resident bump, or use credit |
| `retrieval_events`, `served_facts`, `lesson_search_events`: `read/search_route.rs`, `read/cortex_search.rs`, `lesson_tools.rs` | Effective retrieval-log config disabled; includes searches inside recall |
| `slot_reads`: `read/cortex_search.rs`, `read/fact_get.rs` | Effective cortex read tracking disabled |
| `cms_counters`, bulk entry access counts: `service.rs`, `counters.rs` | Autosave and final shutdown save return before initialization or persistence |
| Episode/session log, active pointer, tombstones, idle titles and client registration: `episodes.rs`, `episode_tools.rs`, `session_close.rs`, `session_register.rs`, `background_sessions.rs`, `routes/episodes.rs` | Write tools and POST routes refused; session/coordination hooks refused (including GET session-start); reaper not scheduled and its duty is guarded |
| Entry store/correction/retirement, facts, set members, contenders, world facts, lessons and outcomes: `store.rs`, `correction_tools.rs`, `forget_tools.rs`, `fact_set.rs`, `fact_resolve.rs`, `set_members.rs`, `world_tools.rs`, `lesson_write.rs`, `outcome_tools.rs`, `stores/{cortex,cortex_sync,world,lessons}.rs` | MCP writes refused before binding; Console POSTs refused before parsing or dispatch |
| Reference/document ingestion: `documents.rs` | `document_ingest` refused; search only reads vectors/rows |
| Graph entities, edges, aliases, relations, proposals and judgments: `storage/graph.rs`, `graph_review_actions.rs`, `graph_review_slots.rs`, `graph_insight.rs`, `routes/graph.rs` | Write tools and POSTs refused; dream/sweep producers disabled |
| Dream cursors, runs, stalls, extraction, lessons, graph and review sweeps: `dream/{curation_judge,cursors,deep,judge,lessons,queue_judges,relations,review_judgments,run,service,stages}.rs`, `maintenance.rs` | Dream config disabled, sweep duty returns immediately, session-dream trigger returns immediately, explicit dream tool and route refused |
| Coordination agents, mail, leases, audits, wake state: `board/{dispatch,mail,lease,store,wake}.rs` | Board MCP tools and coordination POSTs refused; Console snapshot uses a read-only transaction; awareness reads resident episodes |
| Passkeys, maintainer roles, signed mail and pairing: `maintainer_ops.rs`, `maintainer_ops/{actions,mail,roles}.rs`, `principals.rs` | POSTs refused, including `/api/pair`; principal refresher only reads |
| `principal_activity`, auth rejection counters: `main.rs`, `http.rs`, `principal_activity.rs` | No recorder constructed; HTTP accounting also guarded |
| Bank config and data directory: `config_io.rs`, `main.rs` | Config POST refused; startup does not create the bank directory |

`startup.rs` reads and publishes the clock high-water mark; it does not
persist it. `auth.rs`, `release_check.rs`, `read/embed_cache.rs`, reranker
loading and MCP transport maintain process-local snapshots or protocol
sessions. Model provisioning is separate from the bank and should use a
prepared model cache. Native administrative subcommands are not daemon
request paths.

Storage, board and principal-refresher sessions default to read-only in
addition to the SELECT-only role. Startup refuses a DSN with INSERT, UPDATE,
DELETE or TRUNCATE rights on any hydrated table, including column grants,
and names the shadow DSN as the fix. The role script checks every bank table and asserts that no
memberships survived revocation. This is defense in depth, not the
absence-of-writes proof: swallowed
permission errors do not count as success. `test_shadow_mode.py` snapshots
every non-system table and sequence before Rust startup and after a burst
of reads, refused writes and clean shutdown while Python holds the lease.
During that interval only, the owned Python process is paused so queued
auth telemetry cannot pollute the snapshot; it resumes for the comparator.
No columns or tables are normalized away. Required MCP reads must succeed;
the comparator separately records Console refusals and response differences.

Every shadow request checks whether the last successful probe is less than
one second old; a burst reuses that probe. After expiry, the next request
probes row counts and two independently seeded hashes
of visible tuple versions (`ctid` and `xmin`) for the resident stores' inputs.
This scans tuple headers without loading text or embedding payloads; it is
still linear in row count, not a constant-time probe. The signatures detect
inserts, updates and deletes, including older transactions committing late;
hash collisions are theoretically possible. Unrelated telemetry does not
invalidate the cache. Only a changed signature triggers full hydration, and
the service lock coalesces concurrent refreshes. A multi-query hydration can
still observe an active writer across more than one committed state; retaining
the pre-hydration marker makes the next request detect a concurrent commit.
The Linux proof asserts one initial hydration for an unchanged read burst and
one probe and one refresh for 16 concurrent readers following a writer change.
A disposable bank containing 100,000 resident rows measured 45.6 ms per probe
(median), or 721.6 ms for 16 sequential probes; the one-second reuse window
amortizes that cost. Python board traffic that updates
`coordination_hlc_highwater`, and autosave access-count syncs, can still trigger
periodic rehydration. That cost and up to one second of cache reuse are accepted
for shadow comparisons. Bound MCP initialization and read requests look up the
existing coordination identity with `read_only: true`, without inserting it.

Python has no daemon-wide shadow equivalent. Its coordination `context`
has a narrower `read_only` argument, and read tools still record telemetry.
Comparator replays tag both daemons with `X-PL-Session: shadow-compare:<uuid>`;
retrieval-log corpus selection excludes that prefix before grouping queries.
Python's ordinary telemetry remains in the bank, but these tagged replays
cannot feed subsequent corpora. Older, untagged runs cannot be distinguished.
The shadow refusal therefore explicitly names this mode and directs writes
to the active writer daemon.

## Running a comparison

Start the Rust daemon with `PSEUDOLIFE_READ_ONLY=1` and the
`pseudolife_shadow` DSN provisioned by `ops/sql/shadow_reader_role.sql`
(`shadow_password` is its required psql variable). Supply the two base URLs
and bearer environment-variable names to `shadow_compare.py --help`.
Use `--queries` for a JSON read corpus or `--database-url-env` for a
SELECT-only retrieval-log snapshot. Replays still produce Python telemetry;
the reserved session prefix described above keeps it out of later corpora.
Keep the JSON and digest together: tolerated differences are reported, not
discarded. `test_shadow_mode.py` is a Linux-only disposable-bank proof;
the storage reconnect unit test runs natively on Windows as well.
