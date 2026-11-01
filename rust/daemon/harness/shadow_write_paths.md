# Shadow write-path audit

The audit starts at state mutations, including the writes hidden in reads.
The inventory command is:

```sh
rg -n 'INSERT INTO|UPDATE [a-z_]+ SET|DELETE FROM|TRUNCATE|\.execute\(|\.write\(|fetch_add|std::fs::write|fs::write|File::create|create_dir_all' rust/daemon/src
```

Test-only occurrences and process-local locks/caches are distinguished from
bank mutations below. This is an inventory of the current implementation;
the MCP allowlist refuses newly added tools until audited.

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

The database session defaults to read-only in addition to the SELECT-only
role. This is defense in depth, not the absence-of-writes proof: swallowed
permission errors do not count as success. `test_shadow_mode.py` snapshots
every non-system table and sequence before Rust startup and after a burst
of reads, refused writes and clean shutdown while Python holds the lease.
During that interval only, the owned Python process is paused so queued
auth telemetry cannot pollute the snapshot; it resumes for the comparator.
No columns or tables are normalized away. Required MCP reads must succeed;
the comparator separately records Console refusals and response differences.

Every shadow request rehydrates resident stores, retaining the loaded
embedder, so later Python writes are visible on the next read. This is a
diagnostic mode, not a performance claim; a multi-query hydration can still
observe an active writer across more than one committed state.

Python has no daemon-wide shadow equivalent. Its coordination `context`
has a narrower `read_only` argument, and read tools still record telemetry.
The shadow refusal therefore explicitly names this mode and directs writes
to the active writer daemon.
