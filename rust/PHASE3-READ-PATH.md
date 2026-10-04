# Phase 3 read-path source map

The daemon's read surface is backed mainly by resident Python stores hydrated from Postgres, rather than SQL vector queries. Several GET and MCP read operations also write observational or recovery state. Stored document vectors alone do not determine query rankings.

Source checkout: `56dc806bcd0e8aaa6ada5f5c048bdf51e8b67933`; Python oracle: `f709abb54f7912ae9cd767998d0926ca33df4bcd`. `git diff --name-only <oracle> HEAD -- pseudolife_memory` was empty when inspected. Line references below are repository-relative at these pins. This is a source map for the authorized reading exercise; implementation, acceptance and unresolved scope remain deferred. No application imports, live bank reads, builds, tests, benchmarks, or model execution were used.

## Shared path and initialization

REST entry is `web/api.py:231 build_console_app`, then its ASGI `app`, browser-origin/host gate, principal resolution, request-context binding and threadpool `ConsoleRoutes.dispatch` (`web/routes.py:87`). Query decoding keeps the last value per key (`web/api.py:193 _parse_query`); route coercions in `web/routes.py:21-74` are part of existing behavior. Unknown path and known wrong verb differ (404/405); coordination paths are POST only, so no GET coordination route is implied. Hook GET endpoints bypass the ordinary dispatch table and have distinct always-200 authorization behavior.

MCP registration is `_tool` (`mcp_server.py:282`), with `_async_offload` (`:205`) and `_StringSafeMetadata` (`:261`) before tool wrappers. `_READ_ONLY_TOOLS` (`:233`) is a hint, not proof of side-effect freedom. Request principal/tier handling lives in `mcp_server.py:2645-2807`, `writer_context.py`, and `principals.py`.

For ordinary memory service methods, `_ensure_init` (`service.py:1268`) invokes `_ensure_postgres_storage` (`:1212`) and `_hydrate_resident_stores` (`:1469`), plus pending correction/reinstatement/lesson and slot-curation recovery. Cold initialization and writer ownership are therefore a separate boundary from the steady-state read. `storage/sync.py` hydrates CMS/cortex/world/lessons from `PostgresStorage.load_entries/load_facts/load_world_facts/load_lessons/load_episodes` (`storage/postgres.py:1424/1918/1966/1987/1669`); primary records have `ORDER BY id`, episodes only `ORDER BY started_at`. File mode loads tensor files instead. Stored-vector hydration is not embedding inference, but it imports torch; dimension checks, migrations and recovery must not be silently treated as pure reads. No schema change is implied by this map.

## Ranking and embedding boundaries

* `EmbeddingPipeline.encode_query` (`memory/embedding.py:488`) prefixes `config.query_prefix` and calls `encode_single` -> `encode` (`:484/:406`). `encode` calls `SentenceTransformer.model.encode` on misses, converts output to CPU float32, and returns to CUDA when configured. Query weights, prefix, normalization, device/dtype are inputs beyond stored document vectors; no ONNX agreement or tolerance has been established here.
* `MemoryService.search` (`service.py:1877`) -> `ContinuumMemorySystem.retrieve` (`memory/cms.py:740`) -> `MIRASBand.retrieve` (`memory/miras/band.py:157`): normalized matrix-vector cosine then `torch.topk` (`band.py:188`). There is no explicit secondary tie key at this selection. CMS adds time-dependent recency (`cms.py:1074` and `_recency_weight :2879`), source/supersession modifiers, metadata filters, text de-duplication, optional slot/reference/BM25/timeline pools, score sorting (`:1426`), optional cross-encoder scoring (`:1548`) and timeline ordering (`:1718`). Disabling recency removes that modifier only; it does not establish total-order ties or remove other configured channels.
* `cortex_search` (`service.py:4493`) uses `CortexStore.search` (`memory/cortex.py:1508`): CPU float32 cosine, positive assistant-origin score penalty, threshold, stable Python score sort. Equal scores retain resident record order. Service-level BM25 fusion, constraint pinning, set expansion and annotations apply afterwards. `world_search` (`service.py:4814`, `memory/world_cortex.py:226`) and `lesson_search` (`service.py:5309`, `memory/lessons.py:220`) have CPU float32 cosine and stable score sort with record-order ties. World confidence/staleness depends on clock (`world_cortex.py:72/83`); lessons annotate staleness from current cortex.
* `search_documents` (`service.py:3512`) -> `ReferenceBank.retrieve` (`memory/reference_bank.py:242`) -> Chroma collection query with supplied query embedding. CMS can consult the same reference pool. This is a Chroma distance/index boundary, not a Postgres vector query; backend tie ordering and approximate-search determinism were not established by reading this wrapper.
* `recall` (`service.py:9043`) -> `memory/recall.py:226 run_recall`, repeated public search + graph expansion; vocabulary and degrees load the stored graph. Mechanical mode avoids LLM inference; `driver=llm` uses `LLMController/simple_complete` (`recall.py:466`) through configured dream endpoints. Configured time budget, hop/frontier caps and changing state between separately locked searches also affect the result. It cannot be declared deterministic from stored vectors alone.
* `consolidation_candidates` (`service.py:7234`) embeds only query mode, then CMS retrieval and `memory/consolidation.py:113 cluster_candidates`. Episode mode scans stored vectors. Clustering's seed tie key is original input position (`:146`), cluster sort is cohesion*size (`:188`); determinism is conditional on the candidate sequence and arithmetic, not merely vector set equality.
* Graph paths use `memory/graph_store.py:53 subgraph` -> `PostgresStorage.load_graph/load_relations` -> `graph.py` NetworkX derivation. No new embedding is needed for ordinary neighborhood/path reads. `graph_path` creates the undirected NetworkX graph from SQL edge order and calls `nx.bidirectional_shortest_path` (`graph.py:169`); no additional path tie key is imposed by the wrapper. Explicit SQL ordering covers the graph loads, but a complete traversal/derived-edge tie audit is still unverified.

Telemetry matters: search logs `retrieval_events` (`service.py:5886`), final CMS results increment access counters; fact lookup/search calls `_track_slot_reads` (`service.py:4117`); lesson search logs `lesson_search_events` (`:5942`); `get_entry` (`:6051`) bumps access count and credits retrieval use. `/api/search` and `memory_search` attach served facts to the event. `curation_duplicates` (`:7909`) calls `curation_safety.refresh_auto_dismissals`. These existing effects are mapped, not authorized to execute by this reading task.

## GET /api table

Each row names its registration line, explicit service path, and steady-state Postgres method references reached by statically resolvable same-service calls. Shared initialization above applies separately. `none found` means no explicit `_storage` call in that resolvable slice, not a proof that collaborators issue no SQL. Those collaborator gaps are named below. Query excerpts are in the SQL index.

| Route | Registration and service functions | SQL boundary | Embedding / ranking |
|---|---|---|---|
| `/api/stats` | routes.py:112; service.py:3398 `stats` | `get_meta` (postgres.py:2717), `graduation_report` (postgres.py:2325), `load_communities` (postgres.py:4338), `read_audit` (postgres.py:2481), `retrieval_log_health` (postgres.py:2451) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/overview` | routes.py:113; service.py:5632 `cortex_dump`, service_dream.py:2758 `dream_status`, service.py:7129 `episode_list`, service.py:5326 `lessons_dump`, service.py:2218 `list_sources`, service.py:7461 `list_tags`, service.py:5336 `loop_health`, service.py:3398 `stats`, service.py:4829 `world_dump` | `count_signals_for_episodes` (postgres.py:2023), `entities_above` (postgres.py:3891), `entity_id_map` (postgres.py:2990), `find_entity` (postgres.py:2829), `get_meta` (postgres.py:2717), `graduation_report` (postgres.py:2325), `initialize_dream_tracking` (postgres.py:1459), `load_communities` (postgres.py:4338), `loop_health` (postgres.py:2564), `max_entity_id` (postgres.py:3883), `read_audit` (postgres.py:2481), `retrieval_log_health` (postgres.py:2451), `review_queue_counts` (postgres.py:3897), `trace_invalidations_for_slots` (postgres.py:3987), `traces_for_slot` (postgres.py:3951) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/facts` | routes.py:116; service.py:5632 `cortex_dump` | `entity_id_map` (postgres.py:2990), `trace_invalidations_for_slots` (postgres.py:3987), `traces_for_slot` (postgres.py:3951) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/facts/history` | routes.py:117; service.py:5721 `history` | none found; see collaborator notes | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/world` | routes.py:136; service.py:4829 `world_dump` | none found; see collaborator notes | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/lessons` | routes.py:139; service.py:5326 `lessons_dump` | `find_entity` (postgres.py:2829) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/briefing` | routes.py:144; service.py:8473 `session_briefing` | `find_entity` (postgres.py:2829), `get_meta` (postgres.py:2717), `review_queue_counts` (postgres.py:3897) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/agents` | routes.py:147; service.py:8397 `coordination_awareness` | none found; see collaborator notes | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/episodes` | routes.py:153; service.py:7129 `episode_list` | none found; see collaborator notes | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/episodes/summary` | routes.py:155; service.py:7173 `episode_summary` | none found; see collaborator notes | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/recent` | routes.py:170; service.py:2167 `recent` | none found; see collaborator notes | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/search` | routes.py:173; service.py:5968 `attach_served_facts`, service.py:4493 `cortex_search`, service.py:1877 `search` | `add_retrieval_event` (postgres.py:2148), `attach_served_facts` (postgres.py:2310), `bump_slot_reads` (postgres.py:4253), `chronicle_search` (postgres.py:3333), `trace_invalidations_for_slots` (postgres.py:3987), `traces_for_slot` (postgres.py:3951) | query inference; ranking conditions above |
| `/api/trace` | routes.py:174; service.py:2106 `trace` | none found; see collaborator notes | query inference; ranking conditions above |
| `/api/recall` | routes.py:178; service.py:9043 `recall` | `load_graph` (postgres.py:4292), `trace_invalidations_for_slots` (postgres.py:3987) | query inference; ranking conditions above |
| `/api/chain` | routes.py:180; service.py:5796 `chain` | `canonical_names_among` (postgres.py:2858), `entries_for_entity` (postgres.py:4078), `find_entity` (postgres.py:2829), `load_graph` (postgres.py:4292) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/entry` | routes.py:184; service.py:6051 `get_entry` | `bump_access_count` (postgres.py:4286), `credit_retrieval_use` (postgres.py:2189), `facts_for_entry` (postgres.py:4205), `get_entry` (postgres.py:4217) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/sources` | routes.py:188; service.py:2218 `list_sources` | none found; see collaborator notes | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/graph` | routes.py:191; service.py:8229 `graph_neighborhood` | `entity_sources_map` (postgres.py:4129), `load_communities` (postgres.py:4338), `load_graph` (postgres.py:4292) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/graph/projects` | routes.py:199; service.py:8142 `graph_projects` | `project_source_counts` (postgres.py:4198) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/graph/digest` | routes.py:200; service.py:8364 `graph_digest` | `get_meta` (postgres.py:2717) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/graph/communities` | routes.py:201; service.py:8645 `communities` | `load_communities` (postgres.py:4338), `load_graph` (postgres.py:4292) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/graph/path` | routes.py:203; service.py:8660 `graph_path` -> graph.py:169 `shortest_path` | `find_entity` (postgres.py:2829), `load_graph` (postgres.py:4292), through local `st` alias | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/graph/review` | routes.py:205; service.py:7752 `graph_review` | `dismissed_pairs` (postgres.py:3416), `entity_fact_counts` (postgres.py:4107), `entity_sources_map` (postgres.py:4129), `lesson_entity_ids` (postgres.py:4095), `load_graph` (postgres.py:4292), `merge_decision_stats` (postgres.py:3917), `pending_entity_proposals` (postgres.py:3736), `pending_proposals` (postgres.py:3420), `recent_entity_decisions` (postgres.py:3937), `review_proposal_states` (postgres.py:3705) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/graph/proposal-evidence` | routes.py:206; service_dream.py:3689 `merge_proposal_evidence` | `entity_fact_counts` (postgres.py:4107), `entity_sources_map` (postgres.py:4129), `load_entry_texts` (postgres.py:1436), `load_graph` (postgres.py:4292), `pending_entity_proposals` (postgres.py:3736), `traces_by_entity_norm` (postgres.py:3381) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/wiki` | routes.py:210; service.py:7961 `wiki_page` | `entity_sources_map` (postgres.py:4129), `load_communities` (postgres.py:4338), `load_graph` (postgres.py:4292) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/graph/entity-provenance` | routes.py:211; service.py:7943 `entity_provenance` | `entries_for_entity` (postgres.py:4078), `find_entity` (postgres.py:2829), `sources_for_entity` (postgres.py:4072) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/curation/duplicates` | routes.py:220; service.py:7909 `curation_duplicates` | `dismissed_pairs` (postgres.py:3416) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/curation/retired` | routes.py:224; service.py:5565 `curation_retired` | `retired_slots` (postgres.py:3522) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/dream/status` | routes.py:244; service_dream.py:2758 `dream_status` | `count_signals_for_episodes` (postgres.py:2023), `entities_above` (postgres.py:3891), `get_meta` (postgres.py:2717), `initialize_dream_tracking` (postgres.py:1459), `max_entity_id` (postgres.py:3883), `review_queue_counts` (postgres.py:3897) | no explicit query inference; no score ranking; listing/derivation order not fully audited |
| `/api/consolidation` | routes.py:246; service.py:7234 `consolidation_candidates` | `existing_entry_ids` (postgres.py:4226) | query inference; ranking conditions above |
| `/api/maintainer` | routes.py:266; maintainer.py:451 `maintainer_status` | none found; see collaborator notes | maintainer.py -> storage/maintainer.py; no embedding; see special SQL notes |
| `/api/maintainer/sent` | routes.py:274; maintainer.py:476 `maintainer_sent` | none found; see collaborator notes | maintainer.py -> storage/maintainer.py; no embedding; see special SQL notes |
| `/api/maintainer/inbox` | routes.py:275; maintainer.py:479 `maintainer_inbox` | none found; see collaborator notes | maintainer.py -> storage/maintainer.py; no embedding; see special SQL notes |
| `/api/config` | routes.py:278; config_io.read_config | none found; see collaborator notes | config_io.read_config:791; file/config reads, no ranking |

## MCP read tools and mixed-action read branches

`memory_get` is included as a retrieval-shaped write: it is deliberately absent from `_READ_ONLY_TOOLS` because it reinforces retention. The wrapper references below are exact; service paths reuse the GET table and ranking boundaries above. MCP compaction/projection changes output budgets and field shape (`mcp_server.py:597 _compact_entry`, `:845 _project_search`) after retrieval.

| Tool / action | Wrapper | Service path / SQL / embedding |
|---|---|---|
| `document_search` | mcp_server.py:2632 | service.py:3512 `search_documents`; SQL: no explicit steady-state call found; encoder: encode_query |
| `memory_consolidation_candidates` | mcp_server.py:2130 | service.py:7234 `consolidation_candidates`; SQL: `existing_entry_ids`; encoder: encode_query |
| `memory_episode_summary` | mcp_server.py:2111 | service.py:7173 `episode_summary`; SQL: no explicit steady-state call found; encoder: none explicit; see composed path |
| `memory_fact_get` | mcp_server.py:1220 | service.py:4408 `cortex_candidates`, service.py:4395 `cortex_contenders`, service.py:4161 `cortex_lookup`, service.py:7500 `entity_ref`; SQL: `bump_slot_reads`, `canonical_names_among`, `find_entity`, `trace_invalidations_for_slots`, `traces_for_slot`; encoder: encode_single |
| `memory_get` | mcp_server.py:1182 | service.py:6051 `get_entry`; SQL: `bump_access_count`, `credit_retrieval_use`, `facts_for_entry`, `get_entry`; encoder: none explicit; see composed path |
| `memory_graph` | mcp_server.py:2273 | service.py:8229 `graph_neighborhood`; SQL: `entity_sources_map`, `load_communities`, `load_graph`; encoder: none explicit; see composed path |
| `memory_history` | mcp_server.py:1456 | service.py:5796 `chain`, service.py:5721 `history`; SQL: `canonical_names_among`, `entries_for_entity`, `find_entity`, `load_graph`; encoder: none explicit; see composed path |
| `memory_lesson_search` | mcp_server.py:1710 | service.py:5309 `lesson_search`; SQL: `add_lesson_search_event`, `find_entity`; encoder: encode_query |
| `memory_recall` | mcp_server.py:2498 | service.py:9043 `recall`; SQL: `load_graph`, `trace_invalidations_for_slots`; encoder: none explicit; see composed path |
| `memory_recent` | mcp_server.py:987 | service.py:2167 `recent`; SQL: no explicit steady-state call found; encoder: none explicit; see composed path |
| `memory_search` | mcp_server.py:722 | service.py:5968 `attach_served_facts`, service.py:4493 `cortex_search`, service.py:1877 `search`, service.py:2106 `trace`; SQL: `add_retrieval_event`, `attach_served_facts`, `bump_slot_reads`, `chronicle_search`, `trace_invalidations_for_slots`, `traces_for_slot`; encoder: encode_query |
| `memory_stats` | mcp_server.py:1087 | service.py:3398 `stats`; SQL: `get_meta`, `graduation_report`, `load_communities`, `read_audit`, `retrieval_log_health`; encoder: none explicit; see composed path |
| `memory_world_search` | mcp_server.py:1521 | service.py:4814 `world_search`; SQL: no explicit steady-state call found; encoder: encode_query |

Mixed surfaces: `memory_agents` (`mcp_server.py:298`) list goes through `coordination.agents` (`coordination.py:747`): unbound callers get resident `coordination_awareness`, bound callers dispatch the durable agents action; `memory_message` (`:394`) read/receive/wait actions go through `coordination.dispatch` and can settle/acknowledge mailbox state, so they are not blanket pure reads. `memory_graph_review` (`:1942`) default listing uses `service.graph_review`, but the same tool also mutates proposals/relations. `memory_dream` (`:1805`) status and run-list branches use `service_dream.py`; other actions mutate or call models. `memory_toolset` (`:1129`) changes visibility, not memory retrieval. These action grammars need separate future scope decisions; no generic inclusion of the entire tool follows from a read branch.

## Hook GETs and collaborator SQL notes

* `/api/hook/session-start`: `web/api.py` special branch -> `web/session_hook.py:646 hook_session_start`. Authorized session_id causes episode registration and memory-policy logging, with briefing and release-check notice composition. This GET is not side-effect free. Existing episode/experiment writes are outside a pure-read claim; exact mutation SQL is not fully enumerated by this draft.
* `/api/hook/memory-policy`: `web/session_hook.py:598 hook_memory_policy` -> `memory_policy_variant`; variant selection is configuration plus deterministic `ab_arm_index(session_id, len(arms))` (`web/session_hook.py:445`); no SQL or embedding in this path.
* `/api/hook/memory-changes`: `web/session_hook.py:747 hook_memory_changes` -> `service.py:8554 memory_changes_since`; resident status/lesson scanning and wall-clock cursor. No query inference.
* `/api/hook/coordination-start`: `coordination.unavailable_reason`, gate-only/checkin text; no board storage according to `web/api.py` branch comment and implementation.
* `/api/hook/park-gate`: `coordination.py:262 park_gate` -> `_ensure_tier(full=False)` -> `storage/coordination.py:2791 park_gate`. Authenticated board transaction; SQL is in that method and its helpers. No embedding; clock/board state applies.
* `/api/agents?view=coordination`: `coordination.py:675 console_snapshot` -> `storage/coordination.py:1691 console_snapshot`, reads coordination agents, messages, claims, leases and dependencies, filtered to principal; the wrapper explicitly avoids mailbox read, pruning and lease settlement. Ordinary agents view is resident `coordination_awareness`.
* `/api/maintainer*`: `maintainer.py:451/476/479` -> `_run` and `storage/maintainer.py`. Status `_status` (`maintainer.py:252`) uses passkeys/signing keys, roles and key_changes. `key_changes` (`storage/maintainer.py:233`) reads coordination_events filtered by event, ORDER BY seq DESC LIMIT. `passkeys :308`: SELECT maintainer_passkeys ORDER BY created_at, credential_id; `signing_keys :312`: active-key SELECT with same ordering; `roles :679`: live coordination_leases plus latest audit event ordered by seq, then lease name. `sent :606`: messages+recipient labels, origin/principal filter, ORDER BY created_at DESC,message_id LIMIT. `inbox :628`: messages+sender labels, recipient principal filter with same ordering. No embedding; expiry clock and bootstrap recovery setup remain relevant. Schema54 tables are daemon-side and not automatically authorized for porting.
* Graph `_whole_graph` (`service.py:8164`) and `graph_neighborhood` use `PostgresNetworkxGraphStore.subgraph` (`memory/graph_store.py:53`) and graph helper calls taking storage as an argument; this is why direct `_storage` extraction alone misses load_graph/load_relations/find_entity. `graph.py` traversal ordering and SQL through helper arguments need a dedicated completeness pass.
* `/api/graph/proposal-evidence`: `service_dream.py:3689 merge_proposal_evidence` reads pending entity proposals and calls `_judge_evidence_locked` / `_judge_enrich_from`; reading evidence is not a judge-model call. Queue order is pending_entity_proposals ORDER BY kind,score DESC NULLS LAST,id; pages shift when the queue changes.
* `graph_review` consumes `memory/graph_review.py` computations and stored judgments, not a fresh external judge call. `curation_duplicates` calls auto-dismissal refresh, an actual mutation collaborator to retain in a later parity inventory.
* `ReferenceBank.retrieve` (`memory/reference_bank.py:242`) sends query_embeddings, n_results and includes documents/metadatas/distances in the Chroma request; distance conversion is `cosine_similarity_from_distance :37`. No wrapper-defined SQL or secondary tie rule.

## SQL index (explicit service call sites)

These are static excerpts from pinned `storage/postgres.py`; parameters and conditions remain in the named function. They are not executed queries or a claim that dynamic/helper paths are complete. Read telemetry writes are included because the read operation can reach them. Shared hydration SQL and special stores were listed separately above.

### storage/postgres.py:2400 `add_lesson_search_event`

```text
line 2411: 'INSERT INTO lesson_search_events (query_text, session_id, episode_id, served, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id'
```

### storage/postgres.py:2148 `add_retrieval_event`

```text
line 2164: 'INSERT INTO retrieval_events (query_text, origin, session_id, episode_id, served, params, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id'
```

### storage/postgres.py:2310 `attach_served_facts`

```text
line 2319: 'UPDATE retrieval_events SET served_facts = %s WHERE id = %s'
```

### storage/postgres.py:4286 `bump_access_count`

```text
line 4288: 'UPDATE entries SET access_count = access_count + %s WHERE id = %s'
```

### storage/postgres.py:4253 `bump_slot_reads`

```text
SQL assembled through helpers; inspect this function body.
```

### storage/postgres.py:2858 `canonical_names_among`

```text
line 2869: 'SELECT canonical FROM entities WHERE canonical = ANY(%s)'
```

### storage/postgres.py:3333 `chronicle_search`

```text
line 3350: "SELECT id, to_char(occurred_at, 'YYYY-MM-DD'), occurred_phrase, recorded_at, actor, description, episode, src_entry_id FROM chronicle_events WHERE invalidated_at IS NULL AND to_tsvector('english', description) @@ to_tsquery('simple', %s) ORDER BY occurred_at ASC NULLS LAST, recorded_at ASC LIMIT %s"
line 3345: "SELECT plainto_tsquery('english', %s)::text"
```

### storage/postgres.py:2023 `count_signals_for_episodes`

```text
line 2028: 'SELECT COUNT(*) FROM outcome_signals WHERE episode_id = ANY(%s)'
```

### storage/postgres.py:2189 `credit_retrieval_use`

```text
line 2210: 'INSERT INTO retrieval_uses (event_id, entry_id, used_via, created_at) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING'
line 2200: 'SELECT id FROM retrieval_events WHERE session_id IS NOT DISTINCT FROM %s AND created_at >= %s AND served @> %s ORDER BY created_at DESC, id DESC LIMIT 1'
```

### storage/postgres.py:3416 `dismissed_pairs`

```text
line 3417: 'SELECT a_norm, b_norm FROM dismissed_pairs'
```

### storage/postgres.py:3891 `entities_above`

```text
line 3892: 'SELECT count(*) FROM entities WHERE id > %s'
```

### storage/postgres.py:4107 `entity_fact_counts`

```text
line 4111: "SELECT entity_id, COUNT(*) FROM facts WHERE status = 'current' AND entity_id IS NOT NULL GROUP BY entity_id"
```

### storage/postgres.py:2990 `entity_id_map`

```text
line 2994: 'SELECT alias, entity_id FROM entity_aliases'
line 2998: 'SELECT canonical, id FROM entities'
```

### storage/postgres.py:4129 `entity_sources_map`

```text
line 4131: 'SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source'
```

### storage/postgres.py:4078 `entries_for_entity`

```text
line 4085: "SELECT DISTINCT en.id, en.band, en.source, en.ts, en.text, en.episode_title FROM facts f JOIN memory_traces t ON t.entity_norm = f.entity_norm JOIN entries en ON en.id = t.entry_id WHERE f.entity_id = %s AND f.status = 'current' ORDER BY en.ts DESC LIMIT %s"
```

### storage/postgres.py:4226 `existing_entry_ids`

```text
line 4233: 'SELECT id FROM entries WHERE id = ANY(%s)'
```

### storage/postgres.py:4205 `facts_for_entry`

```text
line 4210: f"SELECT f.{', f.'.join(cols)} FROM facts f JOIN memory_traces t ON f.entity_norm = t.entity_norm AND f.attribute_norm = t.attribute_norm WHERE t.entry_id = %s AND f.status = 'current' ORDER BY f.entity_norm, f.attribute_norm"
```

### storage/postgres.py:2829 `find_entity`

```text
line 2832: 'SELECT id, canonical, display, etype, created_at FROM entities WHERE canonical = %s'
line 2838: '\n                SELECT e.id, e.canonical, e.display, e.etype, e.created_at\n                FROM entity_aliases a JOIN entities e ON e.id = a.entity_id\n                WHERE a.alias = %s\n                '
line 2850: 'SELECT alias FROM entity_aliases WHERE entity_id = %s ORDER BY alias'
```

### storage/postgres.py:4217 `get_entry`

```text
line 4221: f"SELECT {', '.join(cols)} FROM entries WHERE id = %s"
```

### storage/postgres.py:2717 `get_meta`

```text
line 2718: 'SELECT value FROM meta WHERE key = %s'
```

### storage/postgres.py:2325 `graduation_report`

```text
line 2354: "\n            WITH ev AS (\n              SELECT id, session_id, served FROM retrieval_events\n              WHERE created_at >= %s AND session_id IS NOT NULL\n            ),\n            tot AS (SELECT COUNT(DISTINCT session_id) AS n FROM ev),\n            hits AS (\n              SELECT DISTINCT (elem->>'entry_id')::bigint AS entry_id,\n                     ev.session_id\n              FROM ev, LATERAL jsonb_array_elements(ev.served) AS elem\n              WHERE elem ? 'entry_id'\n            )\n            SELECT h.entry_id, COUNT(*) AS sessions_served, t.n,\n                   e.source, e.access_count, e.text\n            FROM hits h\n            CROSS JOIN tot t\n            JOIN entries e ON e.id = h.entry_id\n            WHERE t.n >= %s\n            GROUP BY h.entry_id, t.n, e.source, e.access_count, e.text\n            HAVING COUNT(*) >= %s * t.n\n            ORDER BY COUNT(*) DESC, h.entry_id\n            LIMIT %s\n            "
```

### storage/postgres.py:1459 `initialize_dream_tracking`

```text
line 1473: 'SELECT id FROM entries WHERE dream_state IS NULL ORDER BY id FOR UPDATE'
line 1478: "SELECT value FROM meta WHERE key = 'cortex_dream_cursor'"
line 1489: "SELECT value FROM meta WHERE key = 'dream_ack_secret_v1'"
line 1495: "INSERT INTO meta (key, value) VALUES ('dream_ack_secret_v1', %s::jsonb)"
line 1542: "UPDATE entries SET dream_state = 'pending' WHERE dream_state IS NULL RETURNING id"
line 1527: "UPDATE entries SET dream_state = 'legacy-covered' WHERE dream_state IS NULL AND source = ANY(%s) AND ts <= %s RETURNING id"
line 1534: "UPDATE entries SET dream_state = 'legacy-covered' WHERE dream_state IS NULL AND source <> ALL(%s) AND ts <= %s RETURNING id"
```

### storage/postgres.py:4095 `lesson_entity_ids`

```text
line 4100: 'SELECT entity_id FROM lessons WHERE entity_id IS NOT NULL UNION SELECT object_entity_id FROM lessons WHERE object_entity_id IS NOT NULL'
```

### storage/postgres.py:4338 `load_communities`

```text
line 4340: 'SELECT entity_id, community_id FROM entity_communities'
line 4345: f"SELECT {', '.join(cols)} FROM communities ORDER BY id"
```

### storage/postgres.py:1424 `load_entries`

```text
line 1426: f"SELECT {', '.join(cols)} FROM entries ORDER BY id"
```

### storage/postgres.py:1436 `load_entry_texts`

```text
line 1443: 'SELECT id, text, source FROM entries ORDER BY id'
```

### storage/postgres.py:1669 `load_episodes`

```text
line 1672: f"SELECT {', '.join(cols)} FROM episodes ORDER BY started_at"
```

### storage/postgres.py:1918 `load_facts`

```text
line 1920: f"SELECT {', '.join(cols)} FROM facts ORDER BY id"
```

### storage/postgres.py:4292 `load_graph`

```text
line 4303: 'SELECT alias, entity_id FROM entity_aliases ORDER BY alias'
line 4297: 'SELECT id, canonical, display, etype, created_at FROM entities ORDER BY id'
line 4310: f"SELECT {', '.join(edge_cols)} FROM edges WHERE superseded_at IS NULL ORDER BY id"
```

### storage/postgres.py:1987 `load_lessons`

```text
line 1989: f"SELECT {', '.join(cols)} FROM lessons ORDER BY id"
```

### storage/postgres.py:3006 `load_relations`

```text
line 3010: f"SELECT {', '.join(cols)} FROM relations ORDER BY name"
```

### storage/postgres.py:1966 `load_world_facts`

```text
line 1968: f"SELECT {', '.join(cols)} FROM world_facts ORDER BY id"
```

### storage/postgres.py:2564 `loop_health`

```text
line 2599: 'SELECT outcome, COUNT(*) FROM outcome_signals WHERE created_at >= %s GROUP BY outcome'
line 2602: 'SELECT id, session_key, started_at FROM episodes WHERE started_at >= %s AND parent_id IS NULL ORDER BY started_at, id'
line 2620: "SELECT MAX(asserted_at), COUNT(*) FILTER (WHERE status = 'current') FROM lessons"
line 2590: f'SELECT COUNT(*) FILTER (WHERE {ts_col} >= %s), COUNT(*) FILTER (WHERE {ts_col} >= %s AND {ts_col} < %s) FROM {table}'
line 2615: 'SELECT COUNT(*) FILTER (WHERE created_at >= %s), COUNT(*) FILTER (WHERE created_at < %s) FROM outcome_signals WHERE consumed_at IS NULL'
line 2610: 'SELECT COUNT(*) FROM outcome_signals WHERE consumed_at IS NULL'
```

### storage/postgres.py:3883 `max_entity_id`

```text
line 3887: 'SELECT coalesce(max(id), 0) FROM entities'
```

### storage/postgres.py:3917 `merge_decision_stats`

```text
line 3924: "SELECT status, count(*) FROM merge_decisions WHERE decided_by <> 'dream-auto'   AND into_display IS NOT NULL   AND status IN ('accepted', 'rejected') GROUP BY status"
```

### storage/postgres.py:3736 `pending_entity_proposals`

```text
line 3742: "SELECT p.id, p.kind, p.entity_id, p.into_id, p.score, p.reason, p.status,        p.created_at, p.judge_verdict, p.judge_confidence,        p.judge_note, p.judge_model, p.judged_at,        p.judge2_verdict, p.judge2_confidence, p.judge2_model,        p.judged2_at,        e.display, i.display FROM entity_proposals p JOIN entities e ON e.id = p.entity_id LEFT JOIN entities i ON i.id = p.into_id WHERE p.status = 'pending' ORDER BY p.kind, p.score DESC NULLS LAST, p.id"
```

### storage/postgres.py:3420 `pending_proposals`

```text
line 3425: "SELECT p.id, p.src_id, p.relation, p.dst_id, p.confidence, p.similarity,        p.rationale, p.source, p.created_at, p.status,        p.judge_verdict, p.judge_confidence, p.judge_note,        p.judge_model, p.judged_at, p.judge_relation,        s.display, d.display FROM edge_proposals p JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id WHERE p.status = 'pending' ORDER BY p.confidence DESC, p.id"
```

### storage/postgres.py:4198 `project_source_counts`

```text
line 4200: 'SELECT source, COUNT(DISTINCT entity_id) AS entities FROM entity_sources GROUP BY source ORDER BY entities DESC, source'
```

### storage/postgres.py:2481 `read_audit`

```text
line 2491: 'SELECT count(*), count(*) FILTER (WHERE access_count = 0), COALESCE(sum(access_count), 0), COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY access_count), 0) FROM entries'
line 2530: "SELECT (SELECT count(DISTINCT (entity_norm, attribute_norm)) FROM facts WHERE status = 'current'), (SELECT count(*) FROM slot_reads), (SELECT COALESCE(sum(read_count), 0) FROM slot_reads)"
line 2536: 'SELECT COALESCE(sum(reinforcements), 0), COALESCE(sum(explicit_reinforcements), 0) FROM entries'
line 2510: f"SELECT count(*), count(*) FILTER (WHERE access_count = 0) FROM entries WHERE {' AND '.join(cond)}"
line 2525: 'SELECT COALESCE(sum(access_count), 0) FROM (SELECT access_count FROM entries ORDER BY access_count DESC LIMIT GREATEST(1, (SELECT count(*) / 10 FROM entries))) d'
line 2518: 'SELECT source, count(*) AS n, count(*) FILTER (WHERE access_count = 0) AS never FROM entries GROUP BY source HAVING count(*) >= 5 ORDER BY count(*) FILTER (WHERE access_count = 0)::float / count(*) DESC LIMIT 5'
```

### storage/postgres.py:3937 `recent_entity_decisions`

```text
line 3943: 'SELECT id, proposal_id, entity_display, into_display, status,        score, reason, decided_by, decided_at FROM merge_decisions ORDER BY decided_at DESC, id DESC LIMIT %s'
```

### storage/postgres.py:3522 `retired_slots`

```text
line 3535: sql
```

### storage/postgres.py:2451 `retrieval_log_health`

```text
line 2467: 'SELECT COUNT(*), MAX(created_at) FROM retrieval_events'
line 2470: 'SELECT COUNT(*) FROM retrieval_uses'
line 2472: 'SELECT COUNT(*) FROM lesson_search_events'
```

### storage/postgres.py:3705 `review_proposal_states`

```text
line 3714: 'SELECT kind, entity_id, into_id, status FROM entity_proposals'
line 3709: 'SELECT src_id, relation, dst_id, status FROM edge_proposals'
```

### storage/postgres.py:3897 `review_queue_counts`

```text
line 3902: "SELECT count(*) FILTER (WHERE kind = 'merge'),        count(*) FILTER (WHERE kind = 'junk'),        min(created_at) FILTER (WHERE kind = 'merge'),        min(created_at) FILTER (WHERE kind = 'merge'                                AND judge_verdict IS NULL) FROM entity_proposals WHERE status = 'pending'"
line 3909: "SELECT count(*) FROM edge_proposals WHERE status = 'pending'"
```

### storage/postgres.py:4072 `sources_for_entity`

```text
line 4074: 'SELECT source, count, origin FROM entity_sources WHERE entity_id = %s ORDER BY count DESC, source'
```

### storage/postgres.py:3987 `trace_invalidations_for_slots`

```text
line 3995: 'SELECT i.entity_norm, i.attribute_norm, i.source_entry_id,        i.invalidated_at, i.cause FROM memory_trace_invalidations i JOIN unnest(%s::text[], %s::text[]) AS k(e, a)   ON i.entity_norm = k.e AND i.attribute_norm = k.a ORDER BY i.entity_norm, i.attribute_norm, i.source_entry_id'
```

### storage/postgres.py:3381 `traces_by_entity_norm`

```text
line 3383: 'SELECT entity_norm, entry_id FROM memory_traces ORDER BY entity_norm, entry_id'
```

### storage/postgres.py:3951 `traces_for_slot`

```text
line 3952: 'SELECT entry_id FROM memory_traces WHERE entity_norm = %s AND attribute_norm = %s ORDER BY entry_id'
```


## Special-store SQL excerpts

These close the direct special-store boundaries above; `_roster`, `_all`, `_one` and tier initialization remain referenced helper boundaries.

### storage/coordination.py:1691 `console_snapshot`

```text
line 1694: 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'
line 1695: "SET LOCAL statement_timeout = '5s'"
```

### storage/coordination.py:1698 `_console_snapshot`

```text
line 1723: "WITH visible_mail AS MATERIALIZED (SELECT message_id FROM coordination_events WHERE event='send' AND principal>=%s AND principal<=%s UNION SELECT e.message_id FROM coordination_agents a CROSS JOIN LATERAL (SELECT message_id FROM coordination_events WHERE event='send' AND recipient_agent_id>=a.agent_id AND recipient_agent_id<=a.agent_id ORDER BY recipient_agent_id,seq) e WHERE a.principal=%s), candidates AS ((SELECT e.* FROM coordination_events e WHERE principal>=%s AND principal<=%s AND event IN ('send','read','ack','attempt','served','woke','lease_expire') ORDER BY principal DESC,seq DESC LIMIT 101) UNION (SELECT e.* FROM visible_mail v CROSS JOIN LATERAL (SELECT * FROM coordination_events WHERE message_id>=v.message_id AND message_id<=v.message_id AND event IN ('send','read','ack','attempt','served','woke','lease_expire') ORDER BY message_id,event,created_at OFFSET 0) e ORDER BY e.seq DESC LIMIT 101) UNION (SELECT e.* FROM coordination_events e WHERE e.event='expire' AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(e.payload::jsonb->'message_ids') AS ids(id) JOIN visible_mail v ON v.message_id=ids.id) ORDER BY e.seq DESC LIMIT 101)), timeline AS (SELECT e.seq,e.event,e.created_at,e.agent_id,e.recipient_agent_id,e.message_id,e.project,e.task, CASE e.event WHEN 'send' THEN e.payload::jsonb->>'wake' WHEN 'read' THEN e.payload::jsonb->>'path' END AS detail, CASE WHEN e.event='expire' THEN (SELECT count(*) FROM jsonb_array_elements_text(e.payload::jsonb->'message_ids') AS ids(id) JOIN visible_mail v ON v.message_id=ids.id) ELSE 0 END AS expired_count FROM candidates e) SELECT * FROM timeline ORDER BY seq DESC LIMIT 101"
line 1708: 'SELECT m.recipient_agent_id,count(*) AS n FROM coordination_messages m JOIN coordination_agents a ON a.agent_id=m.recipient_agent_id WHERE a.principal=%s AND m.acknowledged_at IS NULL AND m.expires_at>%s AND m.recipient_agent_id=ANY(%s) GROUP BY m.recipient_agent_id'
```

### storage/coordination.py:2791 `park_gate`

```text
line 2800: 'SELECT * FROM coordination_agents WHERE agent_id=%s AND principal=%s AND credential_hash IS NOT NULL'
line 2824: "SELECT 1 AS hit FROM coordination_events WHERE agent_id=%s AND created_at>%s AND CASE event WHEN 'register' THEN true WHEN 'update' THEN (payload::jsonb->'fields') ?| array['status','park_reason','park_needs','park_clear_by','park_resume','park_expires'] ELSE false END LIMIT 1"
line 2810: "SELECT max(delivered_at) AS created_at FROM (SELECT CASE WHEN m.wake->>'queued'='true' THEN w.served_at ELSE w.created_at END AS delivered_at FROM coordination_wakes w LEFT JOIN coordination_messages m ON m.message_id=w.message_id AND m.recipient_agent_id=w.recipient_agent_id WHERE w.recipient_agent_id=%s AND w.decision='rung') delivered WHERE delivered_at>%s"
```

### storage/maintainer.py:233 `key_changes`

```text
line 237: 'SELECT created_at,principal,payload FROM coordination_events WHERE event=%s ORDER BY seq DESC LIMIT %s'
```

### storage/maintainer.py:308 `passkeys`

```text
line 309: 'SELECT * FROM maintainer_passkeys ORDER BY created_at, credential_id'
```

### storage/maintainer.py:312 `signing_keys`

```text
line 315: "SELECT * FROM maintainer_passkeys WHERE state='active' ORDER BY created_at, credential_id"
```

### storage/maintainer.py:606 `sent`

```text
line 615: 'SELECT m.*,a.label AS recipient_label FROM coordination_messages m LEFT JOIN coordination_agents a ON a.agent_id=m.recipient_agent_id WHERE m.origin=%s AND m.sender_principal=%s ORDER BY m.created_at DESC, m.message_id LIMIT %s'
```

### storage/maintainer.py:628 `inbox`

```text
line 635: 'SELECT m.*,s.label AS sender_label FROM coordination_messages m JOIN coordination_agents r ON r.agent_id=m.recipient_agent_id LEFT JOIN coordination_agents s ON s.agent_id=m.sender_agent_id WHERE r.principal=%s ORDER BY m.created_at DESC, m.message_id LIMIT %s'
```

### storage/maintainer.py:679 `roles`

```text
line 686: "SELECT l.name,l.holder_agent_id,l.expires_at,(SELECT e.actor FROM coordination_events e WHERE e.event='lease_delegate' AND e.agent_id=l.holder_agent_id AND CASE WHEN e.event='lease_delegate' THEN e.payload::jsonb->>'name'=l.name AND (e.payload::jsonb->>'fence')::bigint=l.fence ELSE false END ORDER BY e.seq DESC LIMIT 1) AS granted_by FROM coordination_leases l WHERE l.holder_agent_id IS NOT NULL AND l.expires_at>%s AND (l.name LIKE %s OR l.name LIKE %s) ORDER BY l.name"
```

## Specific unverified questions for the later brief

1. Which mixed GET/MCP effects belong in Phase3 acceptance: access/retrieval/slot telemetry, cold-start recovery, curation refresh, experiment assignment and session registration? This map records present behavior and does not retire it.
2. What fixed inputs will a stored-vector ranking oracle include (query vectors, config, timestamps/clock, record order, device arithmetic and optional reference/rerank/controller outputs)? The source has no universal total-order tie contract; strict deterministic claims require that input boundary to be named.
3. Chroma retrieval ordering, NetworkX traversal/derived-edge ordering, graph helper SQL and coordination/maintainer helper transactions are not proven complete by the static service-call index. Source references above identify the exact follow-on reads; no runtime parity or numerical tolerance was tested.

Helper-path completeness and scope classifications remain unverified and need reconciliation with the later Phase 3 brief.
