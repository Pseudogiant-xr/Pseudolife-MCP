# Contract spec: daemon read-path slice (spike)

> Carried over unchanged from the 2026-10-09 spike. Later slices amend it
> in place and record why.

This page lists what the Rust spike must reproduce exactly from the Python
daemon. It covers three routes and the bank read behind them. Everything not
listed (module layout, data structures, caching, error wording beyond the
listed JSON bodies) is free. References are to `pseudolife_memory/` at master
`35b8f5d2`.

## Transport (all routes)

| # | Exact item | Source |
|---|---|---|
| T1 | JSON bodies are `application/json; charset=utf-8`, with `cache-control: no-store` and `x-content-type-options: nosniff`. | `web/api.py:102 _send_json` |
| T2 | Query string: decoded as UTF-8 (replacement on error), `parse_qsl(keep_blank_values=True)`, last value per key wins, `+` is a space. | `web/api.py:193 _parse_query` |

## GET /health (open, never gated)

| # | Exact item | Source |
|---|---|---|
| H1 | Routed before every auth gate; any method; no bearer read. | `web/api.py:418` |
| H2 | Always: `status` str, `version` str, `schema` int (55), `storage` str (`"postgres"`), `auth` bool (a singular or mapped env token is configured), `bank` str or null (first 16 hex of sha256 of `meta.coordination_bank_id`, else null), `persist_errors` int, `memory` object with `source`. | `daemon.py:232 _build_health_payload` |
| H3 | `db`: `"ok"` after a `SELECT 1` on a fresh connection, else `"error: <text>"` and `status` becomes `"degraded"`. | `daemon.py:~380`, `storage/postgres.py:702` |
| H4 | 200 when `status == "ok"`, else 503 with the same body; 500 `{"status":"error","error":str}` if building the payload fails. | `web/api.py:418-427` |
| H5 | `embedder` `{backend, device, dtype}` is present once the embedder is built; `dtype` is null for the ONNX backend. | `daemon.py` health payload |

Conditional keys owned by subsystems outside this slice (`coordination`,
`updates`, `extractor`, `stall`, `hooks_digest`, `build`, `init_refusal`,
`not_ready`, `migration_partial`, `last_backup` and so on) may be omitted,
because Python omits each one when its subsystem is absent. Each omission is
listed as a divergence.

## Bearer gate (`/api/*`)

| # | Exact item | Source |
|---|---|---|
| B1 | Env `PSEUDOLIFE_MCP_TOKEN` maps to principal `default`. `PSEUDOLIFE_MCP_TOKENS` is `token:principal,...`, split on the last `:`, principal lowercased; entries naming `default` or `maintainer` are skipped, and a duplicate token keeps its first entry. Auth is configured iff either yields a token. A map that is set but yields no entry, with no singular token, refuses to start (exit 2). | `principals.py:103 parse_token_map`, `daemon.py:487-550` |
| B2 | Open mode (auth not configured): every request resolves to `default`, except the browser gate. A non-loopback `Origin` gives 403 `{"error":"forbidden_origin","hint":H}`; a non-loopback `Host` gives 403 `{"error":"forbidden_host","hint":H}`. Loopback is `127.0.0.1`, `::1`, `localhost`; the host part strips scheme, port and IPv6 brackets. | `web/api.py:292-320, 707-713` |
| B3 | Header lookup is case-insensitive and decoded as latin-1; `partition(" ")`; the scheme is compared case-insensitively to `bearer`; the token is the rest, stripped of spaces and tabs; an empty token means no principal. | `principals.py:245-306` |
| B4 | Candidate bytes are UTF-8, plus latin-1 when encodable. Constant-time compare against the env map tokens, then the singular token. | `principals.py:245-306` |
| B5 | Stored principals: `SELECT principal, token_hash, tier, board, revoked_at IS NOT NULL FROM public.principals`, refreshed every 10 s. A row authenticates iff `token_hash` is non-empty, it is not revoked, the name (stripped and lowercased) matches `[a-z0-9][a-z0-9._-]{0,63}`, is not reserved (`default`, `daemon`, `maintainer`) and is not shadowed by an env principal. The match is lowercase hex `sha256(candidate)`. | `principal_store.py:156, 236`; `principals.py:92` |
| B6 | A bearer that matches no env token, while the store is not loaded or its snapshot (timed from the start of its read; reads time out at 5 s) is older than 60 s: 503 `{"error":"principals_unavailable"}`. Env tokens still authenticate, and a missing or malformed header is still 401. | `principal_store.py:46`, `web/api.py:283` |
| B7 | No principal: 401 `{"error":"unauthorized","hint":"Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"}`, identical for missing, malformed, unknown and revoked. | `web/api.py:719-723` |
| B8 | After auth: methods other than GET and POST give 405 `{"error":"method_not_allowed"}`; an unknown path gives 404 `{"error":"not_found","path":P}`; a known path with the wrong verb gives 405 `{"error":"method_not_allowed","path":P}`. | `web/api.py:724, 838-841` |
| B9 | Gate order: browser gate, then principals-unavailable, then 401, then method, then route. | `web/api.py:705-731` |

## GET /api/search

| # | Exact item | Source |
|---|---|---|
| S1 | `q` (blank means `""`); `top_k` int, blank or non-numeric gives 12, `0` gives the config default 8; `source`, `band`, `tag` are comma lists, stripped, empties dropped; `min_score` float or null; `disable_recency_boost` true only for `1/true/yes/on`; `bm25`, `rerank` tribool (`""`, `null`, `auto` follow config). | `web/routes.py:21-74, 355-364`; `cms.py:~800` |
| S2 | Query blank after strip: 200 `{"entries":[],"query":"","count":0,"low_confidence":true}`. | `service.py:1963` |
| S3 | Unknown band name (checked before the blank-query answer): 400 `{"error":"unknown band name(s) [...] — this preset has [...]"}` (wording free; status and key exact). | `service.py:2095-2104` |
| S4 | Response: `query` (stripped), `count` int, `low_confidence` bool (true iff no hits, at the default floor 0.0), `entries` list. `cortex` only when facts match; `events` only for temporal cues with chronicle hits. | `service.py:2053-2068`; `web/routes.py:371-379` |
| S5 | Entry: `id` int, `text`, `source`, `bank` str, `timestamp` float, `access_count` int, `surprise_score` float (4 dp), `superseded` bool, `superseded_at` float or null, `superseded_by_text` str or null, `episode_id` and `episode_title` str or null, `tags` list, `score` float (4 dp). `authority` and `distortion_tolerance` only when set; `slots` only when non-empty; superseded entries add `superseded_by_id`, `supersession_verified`, `superseded_by_current`. | `service.py:147-190, 213` |
| S6 | Ranking: query = config prefix + `q`, embedded and L2-normalized; stored rows re-normalized; cosine over eligible entries (source and tag filters apply before top-k); top `k` by cosine; drop exact-duplicate texts (first wins); keep iff relevance >= floor (0.25, or an explicit `min_score`); relevance = cosine (the recency boost is 0 on the flat preset and off by default); sort key = relevance x 0.85 for source `assistant` x 0.55 if superseded; stable sort, cut to `k`. Superseded digest entries never surface. | `cms.py:740-1100, 1426`; `band.py:157-193` |
| S6a | Slot pool, after the dense pool: query content tokens (`[a-z']{3,}` on the lowercased text, minus the stop list) against each eligible, unseen entry's slot entity and value tokens; score `min(0.95, 0.55 + 0.35 x overlap / slot_tokens)`, x 0.55 if superseded; top `k` by score; dropped under an explicit `min_score` only. | `cms.py:168-195, 1135-1160, 1851-1951` |
| S6b | BM25 (on unless `bm25` is false): Okapi k1 1.5, b 0.75 over every eligible entry, query tokens by `bm25.tokenize`; top 20, min-max normalized (one hit or a flat set is 1.0), kept at >= 0.1. Pool hits whose text BM25 found gain `0.3 x norm`; unseen BM25 hits enter at `0.3 x norm`, dropped under an explicit `min_score` only. Then the stable sort and the cut to `k`. | `memory/bm25.py`; `cms.py:1175-1260`; `utils/config.py:~189` |
| S7 | Reads come from the resident state hydrated from schema 55 (`entries ORDER BY id`, facts, chronicle). A row whose band is not in the preset is served under the first band's name. The only writes are the read-path telemetry of S8-S9 (amended by W2-D: the spike was read-only). | `storage/postgres.py:104-114, 1424`; `service.py:2069-2075` |

## Query embedding

The model is Qwen3-Embedding-0.6B. The query prefix
`"Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"`
(no space after the colon) is prepended to the query only. Inputs are capped
at 512 tokens, pooling takes the last token, the output is L2-normalized, and
it has 1024 dims (`utils/config.py:16-70`, `memory/embedding.py:378, 488`).

The spike runs the ONNX export that the prerequisite receipt verified
(`evals/results/rust-onnx-cpu-prerequisite-20d0e75d.json`). Its inputs are
`input_ids`, `attention_mask` and `position_ids = arange(T)`; its output is
`last_hidden_state`.

## Free, and therefore not compared byte for byte (read path)

- Exact score floats, and tie order among equal scores.
- Error text other than the listed bodies.
- JSON key order and whitespace.
- The `version` value and numeric/boolean readings in the `memory` object.
  Memory keys and scalar kinds are checked before values are normalized:
  `unavailable` has only `source`; `process` has `source`, null `near_limit`
  and at least one integer RSS field; `cgroup` has integer current/working-set
  bytes, integer-or-null limit bytes, float-or-null fraction, boolean-or-null
  `near_limit`, and an object of integer event counters. RSS and anon/file
  byte fields and the individual event counters are conditional; their
  presence must agree between arms. A zero or null limit has null fraction
  and flag.
- Dense and fused score floats beyond 4 dp in the served entry, and the raw floats inside
  `retrieval_events.served[].components` (compared at 2e-4: torch and ONNX differ in the last bits).

## W2-D: the rest of GET /api/search

Added by slice W2-D (2026-10-09) at master `28c1f23c`. Everything above
still holds; these rows complete the route. Config keys are read through
`config.rs` with Python's dataclass defaults.

| # | Exact item | Source |
|---|---|---|
| S8 | Retrieval event, when `memory.retrieval_log.enabled` (default true) and the query is non-blank: one `INSERT INTO retrieval_events (query_text, origin, session_id, episode_id, served, params, created_at)` with the stripped query, origin `search`, `created_at` wall clock. `served` is the response's entry list in order, skipping entries without an id: `{entry_id, score, rank, via, bank}` plus `components` when the entry has them. `rank` counts every served position, contiguity neighbours included. A zero-hit search still writes its row. A failed write never fails the search. | `service.py:2069-2075, 5892-5946`; `storage/postgres.py:2148-2170` |
| S8a | `components` per channel: dense `{channel, dense, recency, recency_boost, source_mult, supersession_mult, surprise, band, band_depth}`; slot `{channel, slot, surprise, band}`; BM25-only `{channel, bm25, surprise: 0.0, band}`; timeline `{channel: "timeline", bm25, surprise: 0.0, band}`; contiguity `{channel: "contiguity"}`. Dense and slot hits gain `bm25` (the normalised lexical score, 0.0 when unmatched) whenever BM25 ran over a non-empty pool; every entry the reranker considered gains `ce` (a float, or null when the margin gate skipped it). | `cms.py:1090-1105, 1150-1158, 1271-1312, 1360-1366, 1557-1561`; `service.py:1998-2018` |
| S8b | `params`: `top_k`, `min_score`, `min_score_explicit`, `band_count`, `recency_boost`, `recency_base_half_life_s`, `hide_superseded`, `bm25` (`{enabled}` plus `weight, min_score, k1, b, top_n` when on), `candidate_pool` `{multiplier, pool_size, fusion, rerank_position}`, `reranker` (the rerank log of S13), `timeline` `{enabled, fired}`, `filters` `{bands, sources, episodes, tags, min_logical_turn}`, then `contiguity_neighbors`. | `cms.py:1636-1688`; `service.py:2072-2073` |
| S8c | Session identity of the event: the request's `X-PL-Session` header (last value), else the resident active-session pointer while younger than `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` (21600), else, with `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` set, the request's `mcp-session-id` as given (an empty value included): the MCP call's, or the REST request's last value (Python binds REST headers too), else null. `episode_id` is that session's open leaf episode, else null. `w2d_search.py --only mcp-transport-session` compares it, with mutant `search-no-transport-session`. | `service.py:974-1010, 4946-4959`; `writer_context.py:107-143, 194-211`; `web/api.py:866-885` |
| S8d | When the cortex block (S16) serves facts and the event row exists: `UPDATE retrieval_events SET served_facts = <facts> WHERE id = <event>`. The event id never reaches the response. | `web/routes.py:362-378`; `storage/postgres.py:2310-2322` |
| S9 | Access accrual: every entry of the final merged result (dense, slot, BM25, timeline and reference positions; not contiguity neighbours) gains 1 on its resident `access_count` before serialisation, so the response shows the incremented count. The read path does not write `entries.access_count`; the save cadence does (W2-E). The warmup probe (S17) does not count. | `cms.py:1748-1752`; `service.py:3855-3867` |
| S10 | Chronicle events, when the query has a temporal cue (S11's regex), an aggregation cue (`how many`, `how much`, `how often`, `what percentage`, `in total`, `total` + one of `number amount distance cost sum time money`, `altogether`, `each time`, `every time`, `average`, `the most`; word-bounded, case-insensitive) or a year-first date (`\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b`): `plainto_tsquery('english', q)::text`, its ` & ` rebuilt as ` \| `, matched with `to_tsquery('simple', ...)` against `to_tsvector('english', description)` over live rows, `ORDER BY occurred_at ASC NULLS LAST, recorded_at ASC LIMIT 30` (aggregation cue) or 6. Hits give `events: [{description, actor, date, phrase}]` (`date` is `to_char(occurred_at, 'YYYY-MM-DD')`), plus `events_total` under an aggregation cue. No hits, or an empty tsquery: no key. | `service.py:2023-2066`; `cms.py:62-121`; `storage/postgres.py:3333-3361` |
| S11 | Timeline channel, when `memory.search.timeline_channel` (default false) and the query has a temporal cue (`first last when earliest latest before after since until ago`, `how many times`, `how long`, `what order`, `in order`, `order in which`, `sequence`, `chronolog\w*`, `timeline`, and the month names except `may`; word-bounded, case-insensitive): BM25 (the configured k1, b) over the same filtered candidate pool, top 6, min-max normalised; each unseen hit with norm > 0 enters at `0.3 x norm` (dropped under an explicit `min_score` above it), marked `via: "timeline"`. After the cut, the memory part of the result is ordered by `(timestamp, seq)` (seq preserves insertion-id order before band seating), reference documents trailing. This ordering also applies when every served hit came from another channel. | `cms.py:62-82, 1185-1196, 1315-1370, 1719-1726` |
| S12 | Contiguity, when `memory.search.contiguity_neighbors` n > 0 (default 0): around each direct hit in order, up to n stream neighbours per side (same `episode_id` when the hit has one, else the same `source` among episode-less entries; superseded digests and, under `hide_superseded`, superseded entries excluded; texts already served skipped), ordered by `(timestamp, seq)`, served with score 0.0 and `via: "contiguity"`. `low_confidence` judges only the direct hits. | `service.py:1980-2004`; `cms.py:620-664` |
| S13 | Cross-encoder rerank, when `rerank` (route tribool) or `memory.reranker.enabled` (default false) is on: only when the combined pool (memories + reference) is non-empty and fits `reranker.top_n` (20); a margin gate (`skip_margin`, default 0 = off) on the sorted original scores may skip it. Scores are `sigmoid(logit)` of `cross-encoder/ms-marco-MiniLM-L-6-v2` per (query, text), fused `w x ce + (1 - w) x original` with `fusion_weight` 0.7, then a stable sort. Over budget: `skip_reason: "candidate_budget_exceeded"`, order unchanged. The rerank log (`enabled, fired, skip_reason, top_n, candidate_count, scored_candidates, scoring_policy`, and when the pool qualified `skip_margin, fusion_weight, model`, `margin`) is `params.reranker`. Model: an ONNX export of the same checkpoint (rows R1-R6). | `cms.py:1466-1600`; `memory/reranker.py` |
| S14 | Reference pool (Chroma documents, `ref_top_k` 3): see the divergence list. | `cms.py:1439-1465` |
| S15 | Query-embedding LRU cache: keyed on the prefixed query text, `embedding.cache_size` entries (default 1024), least recently used evicted. Observable only as latency (free). | `memory/embedding.py:406-484` |
| S16 | Cortex facts block, when `memory.cortex.enabled` and `search_first` (both default true): `cortex_search(q, top_k=5, min_score=memory.cortex.guard_min_score (0.2))`; non-empty results are served as `cortex` and attached to the event (S8d). Contract rows C1-C18 below. | `web/routes.py:371-378`; `service.py:4499-4610` |
| S17 | Warmup: once init succeeds, the daemon runs `search("warmup probe", top_k=1, count_access=False)`: one retrieval event, no access accrual, no cortex block. | `service.py:6219-6224` |
| S18 | Pool shape: `candidate_pool_multiplier` m >= 1 widens each band's dense top-k to `k x m`; `fusion: rrf` replaces every pre-fusion score with the sum of `1/(60 + rank)` over the dense (by relevance), slot, BM25 and timeline rank lists, times the source and supersession multipliers; with m > 1 and rerank on, the cut to `k` happens after the rerank, reference positions reserved. Recency (multi-band presets with `recency_boost_enabled`, unless `disable_recency_boost`): band depth d of n gets boost `0.4 x (1 - d/(n-1))` and half-life `recency_base_half_life_s x 2^d`; relevance = `cos x (1 + boost x 2^(-age/half_life))`. | `cms.py:858-905, 940-1000, 1059-1072, 1372-1430, 2878-2881` |
| S19 | `low_confidence`: true when the direct result is empty or, with `memory.search_confidence_floor` > 0, its best score is below the floor. | `memory/abstain.py`; `service.py:2058-2063` |

### Cross-encoder (S13 detail, `read/rerank.rs`)

Sources are `memory/reranker.py` and sentence-transformers 5.5.1 (`ST`).

| # | Exact item | Source |
|---|---|---|
| R1 | A pair is `(query, candidate)` in the BERT pair template `[CLS] q [SEP] c [SEP]`, token types 0 for q and 1 for c. | `reranker.py:153`; ST `base/modules/transformer.py:1296` |
| R2 | Truncation `longest_first` at 512 tokens, from the right. | ST `base/modules/transformer.py:667-677, 917` |
| R3 | Pairs sorted by descending `len(q) + len(c)` (code points) into batches of 32, right-padded (`[PAD]` 0, type 0, attention 0); results restored to input order. | ST `cross_encoder/model.py:545, 677, 700` |
| R4 | Activation Identity (the raw logit, `[B,1]` squeezed), then the stable sigmoid `x >= 0: 1/(1+e^-x)`, else `e^x/(1+e^x)`. | ST `cross_encoder/model.py:402-424, 689-696`; `reranker.py:44-51, 154-156` |
| R5 | No candidates, or a query blank after `strip()`: no scores. | `reranker.py:146-149` |
| R6 | Fusion `w x ce + (1 - w) x original`; no scores leaves the originals. | `reranker.py:177-185` |

### Cortex facts block (S16 detail, `stores/cortex.rs`, `read/cortex_search.rs`)

| # | Exact item | Source |
|---|---|---|
| C1 | Hydration: every `facts` row `ORDER BY id`; `freshness_class` defaults to `evergreen`, `kind` to `scalar`, a version of 0 or null to 1; the embedding read as `real[]`, a vector whose width differs from the live embedder refuses (W2-D completion correction, 2026-10-13). | `storage/sync.py:43, 273`; `storage/postgres.py:122, 1918`; `service.py:1170` |
| C2 | Slot key: strip, casefold, runs of `[\s._\-/]+` become `-`, strip `-`; recomputed from `entity` and `attribute`, never read from the `*_norm` columns. | `memory/cortex.py:54, 311` |
| C3 | Index: current members in record order; a duplicate current or contested scalar keeps the most recently confirmed row (the loser is demoted in memory). | `memory/cortex.py:1797` |
| C4 | Served `origin`: the strongest tier in `support` (user > action > agent > assistant), else `""`; the `origin` column is not read. | `memory/cortex.py:229` |
| C5 | Dense search: f32 cosine over `x/(norm + 1e-12)`; positive cosines of assistant-origin records x 0.85; `min_score` after the penalty; stable sort, cut to `top_k`. | `memory/cortex.py:1508-1541` |
| C6 | Lexical fusion only when `memory.bm25.cortex_enabled` (default false): BM25 over `"{entity} — {attribute}: {value}"` of current records, per record; boost `+ weight x norm`, lexical-only at `weight x norm`; stable sort, cut. | `service.py:4527-4538, 4724-4760` |
| C7 | Set slots: one entry at the first member's place: `kind: "set"`, `entity`, `attribute`, `value` (`compose_set_value`: `"a; b (N members)"`), `source_entries`, `members`, `score` (the best member, 4 dp), `contested: false`, `last_confirmed`, `asserted_at` (the newest member activity) and `age`. | `service.py:4547-4623`; `memory/cortex.py:165` |
| C8 | Scalar entries: the record dict, then `score` (4 dp), `contested` (with `contender_value`, `contender_origin` when a contender exists) and `source_entries`. | `service.py:347, 4637-4650` |
| C9 | Record dict: every key in order; `confidence` and `effective_confidence` at 4 dp, retaining psycopg's decimal-text REAL decoding; `stale`; `stance`, `authority`, `distortion_tolerance` only when set; `age` from `tx_time` or `asserted_at`. | `service.py:297-346` |
| C10 | Freshness: per-class TTLs and floors, linear decay, stale past twice the TTL; an unknown class is volatile; evergreen never decays. | `memory/freshness.py` |
| C11 | Relative ages: `_relative_time` buckets with Python's float floor division. | `memory/context_builder.py:14` |
| C12 | Constraint pins (`pin_constraints`, default true): current scalar constraints whose entity the query names; scored by their ranked score, else their cosine, else 0; under the floor dropped; at most `max(1, k // 2)` by best score, then record order; first, with `pinned: true`; then the rest; cut to `k`. | `service.py:4652-4722` |
| C13 | "Names the entity": a word-bounded match on normalised strings; an apostrophe inside a word belongs to it except a possessive `'s`; combining marks continue a word. | `service.py:613-680` |
| C14 | `memory.search.stale_policy`: `annotate` (flags), `demote` (`warning`, stable stale-last order), `quarantine` (`last_known_value` and the wrapper); set members too. | `service.py:272-296` |
| C15 | Evidence annotation: one invalidation read; an entry whose slot has a `source_superseded` invalidation after `last_confirmed or asserted_at` gains `re_verify` and `re_verify_reason`. | `service.py:4282-4330`; `storage/postgres.py:3987` |
| C16 | `source_entries`: `traces_for_slot` (`ORDER BY entry_id`), one read per served slot. | `storage/postgres.py:3951` |
| C17 | Slot reads (`read_tracking`, default true): the sorted unique served slots, one upsert each in one transaction, `last_read_at` wall clock; a failure is logged, never raised. | `service.py:4117-4135`; `storage/postgres.py:4253` |
| C18 | Served facts attached to the event: `[{entity_norm, attribute_norm, rank, score, kind, contested, pinned}]` as JSONB; never fails the search. | `service.py:5968-5990`; `storage/postgres.py:2310` |


# Write path (slice W2-E)

Added 2026-10-09 by slice W2-E. This part lists what the Rust daemon must
reproduce exactly from the Python daemon when it writes: `memory_store` first,
then the cortex, outcomes, episodes and autosave as their pull requests land.
References are to `pseudolife_memory/` at master `35b8f5d2`. The contract is
the response by value plus the full database state after every write; the
harness (`harness/w2e.py`) diffs both. Declared divergences are in
`divergences.md` under "Write path (W2-E)".

## W0. Tool bodies and their callers

| # | Exact item | Source |
|---|---|---|
| W0.1 | Each MCP tool body returns the dict the Python `def` returns after binding, including its MCP-layer post-processing (`_store_noted`, `_noted`). Argument binding, the error envelope and the success envelope belong to the MCP surface (W2-G). A service `ValueError` is returned as `ToolError::Invalid(text)`; any other failure is internal. | `mcp_server.py:243-302, 664-695` |
| W0.2 | Until the MCP surface lands, the harness reaches a tool body through `POST /api/_harness/tool/<name>`, compiled only under the cargo feature `harness`. A default build's route table equals Python's (unit test). | delegate ruling 2026-10-09 |
| W0.4 | `memory_store` is served on `/mcp`: after binding (spec "Tool argument binding"), the body receives the bound arguments and a `Caller` built from the request's `X-PL-Session` (first value, as M17) and the legacy transport session; it runs in its own tracked task, so a disconnecting caller's store still lands and is drained at shutdown. A body `ValueError` is `invalid_argument` prose and any other failure, initialization included, `internal_error` with `mutation: "unknown"` (M15). `harness/w2e.py` and `w2e_shutdown.py` drive both daemons through `tools/call`. | `mcp_server.py:664-750`, `_async_offload` |
| W0.3 | Caller identity: the session is the `X-PL-Session` header when non-empty, else the active-session pointer (`meta.active_session_pointer`, ignored when older than `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS`, default 21600; `<= 0` never stale), else `mcp-session-id` only when `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` is `1/true/yes/on`, else none. Header values are not trimmed; an empty value falls through. | `service.py:970-1011`, `writer_context.py:107-211` |

## W1. memory_store

| # | Exact item | Source |
|---|---|---|
| W1.1 | Labels resolve before anything else (so an invalid explicit label wins over empty text). `"auto"` (exact, case-sensitive) runs the heuristic: text stripped, `None` when empty or over 400 code points; distortion: `None` on a first-person habit, `"constraint"` on `_STRONG`, `_FRAMING` or an imperative opener with a bare verb; authority: `"quoted"` on `_REPORTED`, else `"directive"` on `_ADDRESSED`, `_FRAMING` or the opener. An unresolved auto stores NULL. Any other value is `str(v).strip().casefold()` checked against the vocabulary, else `ValueError("<name> must be one of [...] (or None), got <repr>")`. | `memory/labels.py:60-251`, `service.py:1712-1715` |
| W1.2 | Text is `str.strip()`ped (Unicode whitespace and U+001C..U+001F). Empty: `{"stored": false, "surprise": 0.0, "reason": "empty", "cortex_promoted": 0}` plus the `empty` note; nothing else happens (no episode, no label keys). | `service.py:1719-1722` |
| W1.3 | Order of effects: embed the stripped text (document side, no prefix); resolve the session (W0.3); resolve the `episode` handle (W2.3); if no handle resolved, ensure the session's root episode (W2.2). These episode writes happen even when the store is later dropped. | `service.py:1723-1737` |
| W1.4 | Meta filter (only when `memory.meta_filter.enabled`; the daemon overlay sets it false unless the YAML leaf is set): `source != "user"` and any of the 16 case-insensitive patterns searches the stripped text: `(False, 0.0)` before any other CMS effect. | `memory/cms.py:498`, `memory/meta_filter.py:17-72`, `service.py:1108` |
| W1.5 | Surprise per band: `1.0` for an empty band, else `max(0, min(1, 1 - max_j cos))` over every resident entry of the band (superseded and digest entries included), each row and the query L2-normalized. Overall surprise is the minimum over bands. | `memory/miras/band.py:92-109`, `cms.py:502-503` |
| W1.6 | Slots: `extract_slots(text, last_entity_context)` runs before the contradiction scan and is stamped on the stored entry as `[[entity, attribute, value, "+"], ...]`. The coreference anchor (`_last_entity_seen`) starts empty, is resident only, and after a real store becomes the entity of the first slot whose attribute is `name` or `type`. | `memory/slots.py`, `cms.py:519-525, 578, 589-594` |
| W1.7 | Contradiction scan over every band's entries, skipping superseded ones; the first path that hits per entry wins: (0) slot identity, normalized as the cortex normalizes keys and values; (1) cosine >= 0.70 and negation asymmetry; (2) cosine >= 0.80 and `_looks_like_replacement`; (3) gain/loss asymmetry with an anchor tier (slot 0.15, content 0.35, pronoun 0.35). No NLI (never constructed). No side effect on any entry. | `memory/contradiction.py:84-601`, `service.py:1502` |
| W1.8 | Gate: drop with `(False, surprise)` when no contradiction was found and `surprise < memory.surprise_threshold` (default 0.0, so the gate is inert and the scan unobservable at defaults). | `cms.py:540-542` |
| W1.9 | A stored entry goes to `bands[0]` (eviction first, W1.10), stamped with: the session's open leaf episode (or, with no session, the global current episode if open); a resolved handle overrides `episode_id` and `episode_title` with its root's; tags normalized (strip, lowercase, drop empties, dedupe in order); slots; labels. Then one `INSERT INTO entries` with `band, text, embedding, surprise (float4), ts (time.time()), access_count 0, source (raw), superseded_at/superseded_by_text/last_logical_turn NULL, episode_id, episode_title, tags, slots, authority, distortion_tolerance, dream_state 'pending'`, `RETURNING id`. | `cms.py:545-601`, `memory/episodes.py:41-61, 281-299`, `storage/sync.py:57-79`, `storage/postgres.py:916-933` |
| W1.10 | Capacity: when `bands[0]` already holds `max_entries` (flat 5250), evict before appending the entry with the lowest `((balanced + 1) * source_weight + retention_boost * log1p(reinforcements)) * (0.05 if superseded)`, `balanced = access_count / max(now - ts, 1) + 0.1 * surprise`, first index on ties; source weights `user_msg`/`user` 1.5, `system` 10.0, `tool_call` 0.5, `llm_thinking` 0.2, others 1.0; `retention_boost` 1.0 under the daemon overlay. On the last band a true drop: one transaction deleting the row and upserting `meta.capacity_true_drops` (`count` incremented, `last_at`, `last_entry_id`, `last_source`, `last_superseded`). | `memory/miras/band.py:130-151`, `protocols.py:84-103`, `retention.py:37-47`, `cms.py:2709-2772`, `storage/postgres.py:1380-1422` |
| W1.11 | Response keys: `stored`, `surprise` (`round(x, 4)`), `reason` (`null` when stored; `"filtered_meta"` when dropped at surprise exactly 0.0; `"below_surprise_threshold"` when below the threshold; `"rejected"` otherwise), `cortex_promoted` (0 unless auto-promotion is on), then `authority` and `distortion_tolerance` when set, `episode_hint` when the landing root's title matches `^session - \d{4}-\d{2}-\d{2} \d{2}:\d{2}$` (text: `session episode is untitled — call memory_session_title('<project> - <topic>'` + `, episode='<root id[:12]>'` when a handle resolved + `)`), `episode_warning: "unknown or closed episode handle"` when a handle was given and did not resolve, and `note` per `_STORE_NOTES` (a `filtered_meta` drop that is not a meta statement, with the threshold above 0, gets the exact-duplicate note). | `service.py:1749-1797`, `mcp_server.py:671-695`, `session_title.py:23` |

## W2. Episodes on the store path

| # | Exact item | Source |
|---|---|---|
| W2.1 | The episode log hydrates from `episodes ORDER BY started_at`; the current episode is the last open one. `meta.active_session_pointer` (`{session_id, ts}`), `meta.episode_tombstones` (`{id: {session_key, ended_at, title}}`, keyed rows only) and `meta.deferred_empty_roots` hydrate at init. | `storage/sync.py:146-155`, `service.py:1308-1330` |
| W2.2 | Ensure a session episode (key non-empty): an open leaf for the key wins (no write); else the newest closed root for the key within `PSEUDOLIFE_SESSION_RESUME_SECONDS` (default 21600; `<= 0` disables) reopens (`ended_at` null, `closed_by_new_start` false, current = it, then the full upsert sweep and `UPDATE client_sessions SET ended_at = NULL, end_reason = NULL WHERE session_key = ? AND ended_at IS NOT NULL`); else a new root: id `uuid4().hex`, title `session - %Y-%m-%d %H:%M` in local time, `started_at` now, current = it, then the sweep. | `service.py:6850-6917` |
| W2.3 | Handle (non-empty, at least 8 chars): exactly one open keyed root whose id starts with it resolves (a keyless or ambiguous match does not); else, within `PSEUDOLIFE_HANDLE_RESUME_SECONDS` (default 2592000; `<= 0` disables), exactly one closed keyed root reopens (not made current), or exactly one tombstone recreates its root under the same id (`started_at` now, its title or the generic one), deleting the tombstone (`meta` write). Each is followed by the sweep and the `client_sessions` reopen. | `service.py:6768-6848` |
| W2.4 | The sweep upserts every episode in log order, one transaction each (`INSERT ... ON CONFLICT (id) DO UPDATE` over all eight columns); a failure is logged and stops the sweep, never the call. | `service.py:6735-6745`, `storage/postgres.py:1648-1667` |
| W2.5 | With a resolved handle and no `X-PL-Session` header, the effective session becomes the handle root's key; with a header, the header stays the identity while the entry still attributes to the handle's root. | `service.py:1727-1737` |

Free for W1 and W2: log lines; the exact float bits of `entries.embedding`
and `entries.surprise` within the measured tolerance (but never a different
admission decision or reason); wall-clock values (`ts`, `started_at`,
`capacity_true_drops.last_at`) and episode ids, which the harness normalizes
by rule; and a search `score` in an answer within 1.5e-4 (the two
embedders agree to the measured tolerance, and scores are rounded to four
places).

## W3. Shared episode ownership

Store-driven open/resume, handle touches, idle close, deferred-empty sweeping
and tombstones operate on one resident `EpisodeLog` under `Ready.write`.
Startup hydrates it once from the complete validated startup candidate.
Lock order is `Service.inner`, then `Ready.write`, then the leased storage
writer; a later-lock holder must not acquire an earlier lock. Closing a
session clears the global current leaf, matching `EpisodeManager.end_session`.
The store's full episode upsert sweep must observe prior reaper closes and
must never reopen unrelated sessions from an obsolete snapshot.

The live `store-reaper` case creates an empty session through a filtered
store, waits for idle close and compares the full episode and deferred-root
state with Python. Native coverage also checks that handle touches postpone
reaping and that a sweep publishes its tombstone to the same owner.
The standalone episode tools share this state ("Episode tools over MCP").
Nonempty-session automatic titles and tensor autosave/exit persistence
remain named follow-ups in `divergences.md`.


# Contract spec: coordination board (slice W2-F)

What the Rust daemon reproduces exactly from the Python board: the
`/api/coordination/<action>` surface, the store behind it, the audit chain,
wake decisions, the async receive hub and the four board hooks. The pairing
write below is the target contract; its HTTP integration remains the named
W2F-PAIRING-HTTP cutover blocker in `divergences.md`. References are to `pseudolife_memory/` at master `28c1f23c`
(`co` = `coordination.py`, `sc` = `storage/coordination.py`, `api` =
`web/api.py`, `hub` = `web/coordination.py`). W1-A's gate
(`spec-w1a-foundation.md`, section R) runs first; this slice owns
everything after the parsed body. "Exact" covers response values,
statuses and every row the board writes; the free list is at the end.

## BT. Transport (after W1-A's gate)

| # | Exact item | Source |
|---|---|---|
| BT1 | `POST /api/coordination/<action>`: `authenticated_principal` first. No env token and no map gives 401 `authentication_required`; a store that cannot vouch gives 503 `principals_unavailable`; no principal gives 401 `unauthorized`. | `api:774-779`, `co:407-426` |
| BT2 | A refusal is `{"error": code}`, plus `"detail"` only for a public code whose detail is a string. Status: 401 for `unauthorized`, `authentication_required`, `instance_authentication_required`; 403 for `principal_not_allowed`, `invalid_credential`, `child_send_refused`; 404 `instance_not_found`; 409 `bank_identity_mismatch`; 429 for `wait_capacity_exceeded`, `rate_limited`, `queue_full`, `lease_queue_full`; 500 for `coordination_unavailable`, `invalid_bank_identity`; 503 `principals_unavailable`; else 400. A code outside `PUBLIC_ERROR_CODES` becomes `invalid_request` (a ValueError or TypeError) or `coordination_unavailable` (anything else, such as a database error). | `api:170-189`, `co:67-114, 380-404` |
| BT3 | Success is 200 with the action's result object. | `api:787` |

## BD. Dispatch (`_dispatch`)

| # | Exact item | Source |
|---|---|---|
| BD1 | Order: the hub's `wait_seconds` check (receive only; any other action keeps the key, and it is refused as unexpected), then `coordination.enabled` false gives 200 `{"enabled": false}`, then `principal_admitted` (listed in `allowed_principals`, or a stored principal with `board` true; never `daemon` or `maintainer`), then an unknown action (`unknown_coordination_action`, detail `use one of <sorted actions>`), then the `x-pl-bank`/`x-pl-principal` binding parse (not for `context`), then unexpected parameters, missing parameters, the `context` shape rules, `generation` (an int >= 1, not a bool), the receive attachment pair, and then the instance headers `x-pl-agent` and `x-pl-agent-key` for every action but `context`, `register` and `leases`. | `co:556-612`, `hub:44-49` |
| BD2 | Parameter whitelists and required sets as `_PARAMETERS` and `_REQUIRED`. The `unexpected_parameter` detail is `<action> does not take <sorted identifier-shaped names, or "these parameters">; it takes <accepted, comma-joined>` (or `; it takes no parameters`). The `missing_parameter` detail is `<action> needs <sorted missing>`. | `co:31-64, 536-583` |
| BD3 | A binding (both headers; the principal percent-encoded exactly as `quote(safe="")` gives it; 1-256 characters, no C0) must equal the store's `context` `(bank_id, principal)`, else 409 `bank_identity_mismatch`. | `co:434-454, 621-626` |
| BD4 | `register`, `send` and `heartbeat` run `prune(audit_retention_days)` first, at most once a minute per process. | `co:632-636` |
| BD5 | `receive` with an attachment passes `check_attachment` and reads `for_delivery`. Its result gains `note` = `RECEIVE_NOTE`, plus `maintainer_note` when a message has origin `maintainer` and `verified`. `history` gains its fixed `note`. | `co:641-666, 138-148` |
| BD6 | After `send`, `attach` and `detach`, the hub's waiters for each recipient (a send's receipts) are woken. | `co:667-673` |

## BH. Async receive hub

| # | Exact item | Source |
|---|---|---|
| BH1 | `wait_seconds` must be a finite int or float in [0, 30] (a bool is refused), else `invalid_wait_seconds`. At most 64 calls are in flight, else `wait_capacity_exceeded`. | `hub:14-15, 43-52` |
| BH2 | A wait needs `x-pl-agent` (`instance_authentication_required`), a truthy `attachment_id` and an int `generation` (`attachment_required`). One wait per (agent, attachment, generation) (`attachment_already_waiting`), and at most 64 waiters in all. | `hub:82-90` |
| BH3 | Loop: subscribe, read; return when there are messages or `enabled` is false; otherwise wait until notified or the deadline, then read once more. A client disconnect cancels the wait without a response. The capacity stays held until the synchronous read ends. | `hub:91-117`, `api:117-140, 782-786` |

## BS. Store

| # | Exact item | Source |
|---|---|---|
| BS1 | Validation: `_string` (type, length in characters, no C0 or surrogate, optional nonblank), `_fields` (label, project and task 120; status 240; episode 120; capabilities at most 8 names of up to 40 with bool values; children; the park fields and their exact refusal details), `_check_label`, `_check_name`, `_lease_args`, `_limit` (int 1..50, not a bool). Every hashed text runs the credential-shape guard `secret_kind` (`secret_like_body`, detail `<field> looks like a credential, which the board never keeps; remove it and retry`). | `sc:615-620, 792-946, 1379-1406, 1541-1616, 1947-1966, 3490-3492` |
| BS2 | `reserved_name`: NFKD minus combining marks, confusables folded, casefold, alphanumerics only; `maintainer` refused anywhere in the label and within one OSA edit, `daemon` within one edit of the whole label, `passkey` and `verified` exactly. | `sc:396-422, 529-569` |
| BS3 | `context`: insert the bank id (`str(uuid4())`) when it is absent and not `read_only`, logging `bank_identity` once; the proof is the HMAC-SHA256, keyed by the hex-decoded credential hash, of `["pseudolife-context-v1",bank,principal,agent,nonce]` (ASCII, compact). | `sc:1292-1337` |
| BS4 | `register`, `update` (park record, expect, children, names, status history), `authenticate`/`_public` (every key; `children` without hook entries past 3 h; `adapter_available`; `name` = the name, else `label short8`, else `short8`), `attach` (the generation rule, `last_activity` only for a new attachment, `ring`, `ring_armed_until` capped at now + 60), `heartbeat` (`active`, the harness name), `detach` (drops hook children), the subagent hooks, `park_gate`, `woke`. | `sc:1355-1789, 2503-2632, 2999-3088` |
| BS5 | Roster (`agents`): peers active within 1 h, or 3 h while attached; reachable first, then `last_activity` descending, then id; `idle_omitted`, `truncated`, `status_expires_at`, `status_overdue`, the status age (`_relative_time` buckets, `more than ...` from the log's oldest row, `unknown`), `status_stale` (2 h, non-blank status), and the settled lease page. | `sc:1791-1829, 1901-1934`, `memory/context_builder.py:14-33` |
| BS6 | Mail: `send` (every refusal in order, prefix resolution, routed replies, bursts with `FANOUT_MAX` 50, request replay and `request_conflict`, the 60-per-minute rate, the 256 pending cap, the writer-epoch check, the HLC high-water upsert), `receive` (the cursor, the `for_delivery` filter, continuity, first-read events), `ack` (batches and prefixes), `attempt`, `history`, `prune`. | `sc:3090-3925` |
| BS7 | Wake: `_wake_decision` and its maintainer branch (every decision, reason, cap, stagger and receipt field), `_serve_wake`, `_listener_path`, `_live_park`, `_authority`, `_lease_clearer`, `_need_matches`. | `sc:500-513, 2551-2579, 2662-2997` |
| BS8 | Leases: `acquire_lease`, `release_lease`, `list_leases`, settle and grant (one fence sequence, the 300 s grant window, departed and reserved waiters), `reserved_lease`, `already_delegate`, `lease_queue_full`. | `sc:1947-2268, 2285-2298` |
| BS9 | Every mutation takes its row locks in Python's order, ending with the audit-chain advisory lock. Every board row equals Python's after the same operation sequence, apart from the free fields. | `sc:1-21, 441-447` |

## BA. Audit chain (byte-exact)

| # | Exact item | Source |
|---|---|---|
| BA1 | `payload` is `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`, stored as TEXT: keys in code-point order, non-ASCII raw, `\uXXXX` in lowercase for C0, Python `repr` for floats (`1e+16`, `1e-05`, `60.0`). | `sc:953-954` |
| BA2 | `hash = sha256(prev_hash + json.dumps(["pseudolife-coordination-audit-v1", seq, event, actor, principal, agent_id, recipient_agent_id, project, task, message_id, float(created_at), hlc, payload], ensure_ascii=False, separators=(",", ":")))`; genesis `"0"*64`; `seq` dense under `pg_advisory_xact_lock(hashtextextended('coordination-audit-chain', 0))`, taken last in the mutation's own transaction. | `sc:439-447, 957-971, 1235-1272` |
| BA3 | A send event keeps `body` and a 16-byte hex `body_salt` outside the hash; its payload holds `text_commitment = sha256(bytes.fromhex(salt) + utf8(body))`. | `sc:448-472, 516-525, 3461-3475` |
| BA4 | Which events each mutation logs, with which actor, columns and payload. Heartbeats and plain renewals log nothing, apart from a heartbeat's name change. `audit_prune` cuts on UTC day boundaries. | `sc:1-21, 978-985, 3816-3925` |
| BA5 | Python's `verify_audit_chain` accepts a chain written by any mix of Python and Rust appends. | `sc:1035-1150` |

## BK. Hooks and pairing

| # | Exact item | Source |
|---|---|---|
| BK1 | `GET /api/hook/coordination-start`: 200 `text/plain; charset=utf-8`, `cache-control: no-store`, body `CHECKIN_TEXT + "\n"` or empty, and `x-pl-board: on` or `off; reason=<unavailable_reason>` (`disabled`, `authentication_required`, `unauthorized`, `principals_unavailable`, `principal_not_allowed`). | `api:598-621`, `co:192-212, 360-377` |
| BK2 | `GET /api/hook/park-gate?agent&since`: `allow\n`, or `block\n<PARK_GATE_MESSAGE>\n`; `allow` on any failure or a malformed agent; `since` counts only as a float in [0, 1e12). | `co:259-303`, `sc:2999-3042` |
| BK3 | `POST /api/hook/woke?agent` and `POST /api/hook/subagent?agent&event&child&type`: `ok\n` when recorded, else empty; every failure is empty. | `co:306-357`, `sc:1741-1789, 3044-3064` |
| BK4 | Pairing write: one conditional UPDATE on the database clock (`token_hash`, `paired_at`, `paired_code_hash`; the code cleared), a same-token retry inside the window answering the same row, the spent-code cleanup, and `store.add` of the result. | `principal_store.py:407-454`, `api:394-408` |

## Free, and therefore not compared byte for byte (board)

- Random values: agent and message ids, credentials, bank ids, body salts.
  The harness maps each one to an ordinal by first appearance and checks
  its shape (32 hex, a 43-character URL-safe token, a canonical UUID, 32
  hex), and that each credential hashes to its stored `credential_hash`.
- Wall-clock times and HLC physical parts: compared as (step, offset), the
  offset being a declared constant (0, 25, 60, 300, a ttl, a TTL), inside
  each arm's own captured window.
- Audit `hash` and `prev_hash`, which depend on the above: checked by
  recomputing them with Python's `audit_hash` and by `verify_audit_chain`
  (BA2, BA5), not by cross-arm equality.
- JSON key order and whitespace in responses.
- Log lines.

## Declared divergences (W2-F)

| Item | Python | Rust | Why |
|---|---|---|---|
| Unicode tables for names, labels, lease names and excerpts (NFKD, combining marks, casefold, category C, alphanumerics) | the production oracle is python:3.12-slim (Unicode 15.0); the local oracle is CPython 3.11 (Unicode 14.0) | the crates' Unicode tables | the canonical shapes are ASCII plus the listed confusables; only code points whose properties differ between versions behave differently (ruling d, 2026-10-09) |
| `send` readiness | waits for the full service (embedder and hydration), so an embedder failure refuses sends | needs the storage tier and the board's HLC reseed only | the full init exists to reseed the HLC, which the board reseeds from SQL itself (ruling a) |
| Lone surrogates in a JSON body | `json.loads` accepts `"\ud800"`, then a field check refuses it with that field's code | `400 invalid_json` | Rust strings cannot hold one; no producer sends one |
| #639: a `no_path` receipt can still ring a Codex doorbell | rings | rings (ported as is) | deferred until after cutover by decision; ported, not fixed |
| `lease` with an explicit `"ttl": null` | depends on state: `reserved_lease`, an auth refusal, 200 for a caller already queued, a 500 from the NOT NULL column, or `invalid_request` from `now + None` | `400 invalid_request` before any check | no producer sends a null ttl; the shim and the MCP tool always send an int (Opus review, finding 2) |
| A cursor or Console `limit` integer beyond i64, and non-ASCII digits in a cursor | Python's unbounded `int()` (a receive cursor answers `cursor_ahead`; a history cursor with no retained sends answers 200) | a receive or history cursor is `invalid_cursor` without detail; a Console limit beyond i64 is `invalid_limit` as in Python | cursors are produced by the daemon itself (Opus review, finding 6) |
| A 400-digit integer `park_expires` | `math.isfinite` overflows: 500 | `400 invalid_park` | no producer sends one (Opus review, finding 11) |
| `NaN`, `Infinity` and nesting deeper than 128 in a JSON body | `json.loads` accepts them, so the board's own type checks refuse them | `400 invalid_json` from the gate's parser | body parsing is W1-A's gate; no producer sends them (Opus review, finding 10) |
| Receive-wait key for `attachment_id` `true` vs `1` | equal in Python's tuple key (`attachment_already_waiting`) | distinct | no producer sends a non-string attachment id (Opus review, finding 5) |


## MCP surface (W2-G)

Added 2026-10-09 for slice W2-G. Python references are to
`pseudolife_memory/` at master `28c1f23c`; SDK references are to `mcp` 2.1.1,
the pin when this section was written; M3 follows the current pin, 2.2.0
(`mcp/server/streamable_http.py`, `streamable_http_manager.py`,
`transport_security.py`). The canonical producers are the Python and Rust
shims and `doctor`, which all speak the legacy handshake: POST `initialize`
(protocol `2025-11-25`), POST `notifications/initialized`, POST requests with
`Mcp-Session-Id` and `MCP-Protocol-Version`, then DELETE (recorded from the
SDK client the Python shim uses). Code: `src/mcp/`. Unlike the spike's
routes, the JSON-RPC bodies below are compared byte for byte, key order
included; the "Free" list at the end of this section replaces the one above
for the MCP surface.

### Mount and transport

| # | Exact item | Source |
|---|---|---|
| M1 | `/mcp` and `/mcp/*` are served only on bearer-configured installs after the bearer gate (503/401 as for the catch-all). `/mcp/` answers 307 with `Location: http://<Host>/mcp[?query]`, no body; any other `/mcp/...` path answers 404 `text/plain; charset=utf-8` `Not Found`. | `web/api.py:858-886`; Starlette `redirect_slashes` |
| M2 | An `MCP-Protocol-Version` header outside `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25` routes to the 2026-07-28 era (declared divergence: only its no-envelope refusal is served). | `streamable_http_manager.py:_handle_request` |
| M3 | Session lookup first: a known id goes to its transport; any other id answers 404 `{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Session not found"}}`, no id header, before the security check. That includes a DELETEd id and a discarded one: mcp 2.2.0 keeps no history (`_discard_session`). No id: a new transport with a fresh `uuid4().hex` id, reported in `mcp-session-id` even on refusals, and kept only if that opening request answers below 400; refused, failed or cancelled first, it is discarded (`_serve_opening_request`). A request whose session a racing DELETE forgot after its lookup answers 404 `Not Found: Session has been terminated` with the id header, after the security check and before its method is handled (`streamable_http.py:506-516`). Mutants `mcp-keep-failed-opening` and `mcp-delete-keeps-session`. | `streamable_http_manager.py:_handle_stateful_request`, `_serve_opening_request`, `_discard_session` |
| M4 | Security, per request: POST needs a Content-Type whose lowercase starts with `application/json`, else 400 plain `Invalid Content-Type header` (no content type). Tokenless MCP remains unserved (501) under the forward-port ruling; the deferred transport policy requires Host to start with `127.0.0.1:`, `localhost:` or `[::1]:` (421 `Invalid Host header`), and a non-empty Origin must start with `http://127.0.0.1:`, `http://localhost:` or `http://[::1]:` (403 `Invalid Origin header`). With a token, no Host or Origin check. | `mcp_server.py:125-160`; `transport_security.py` |
| M5 | POST order: Accept must cover both `application/json` and `text/event-stream` (`*/*`, `application/*`, `text/*` count; 406), then a Content-Type part equal to `application/json` (415), then JSON (400, -32700 `Parse error: ...`), then a JSON-RPC message (400, -32602 `Validation error: ...`), then, for anything but `initialize`, a session id (400 `Bad Request: Missing session ID`). Errors are `{"jsonrpc":"2.0","id":null,"error":{"code":C,"message":M}}`, `application/json`, with the id header. | `streamable_http.py:_handle_post_request`, `_create_error_response` |
| M6 | Notifications and client responses answer 202, empty body, `application/json`, with the id header. Requests answer 200 SSE with `cache-control: no-cache, no-transform`, `connection: keep-alive`, `content-type: text/event-stream`, `mcp-session-id`, `x-accel-buffering: no`, and one `event: message\r\ndata: <json>\r\n\r\n` event; then the stream ends. | same; sse-starlette |
| M7 | GET needs Accept covering `text/event-stream` (406 `Not Acceptable: Client must accept text/event-stream`), a session id (400), and no open standalone stream on the session (409 `Conflict: Only one SSE stream is allowed per session`); then an open SSE stream carrying server notifications. DELETE needs a session id (400), terminates the session, and answers 200, empty, `application/json`, with the id header. Other methods: 405 `Method Not Allowed` with `allow: GET, POST, DELETE`. | `streamable_http.py:_handle_get_request`, `_handle_delete_request`, `_handle_unsupported_request` |
| M8 | `initialize` needs `protocolVersion` (string), `capabilities` (object) and `clientInfo` with string `name` and `version`, else -32602 `Invalid request parameters` with `data: ""`. It echoes a handshake version and answers `2025-11-25` for any other; re-initializing a live session is allowed. The result is exactly `{"capabilities":{"experimental":{},"prompts":{"listChanged":false},"resources":{"listChanged":false,"subscribe":false},"tools":{"listChanged":true}},"instructions":I,"protocolVersion":V,"serverInfo":{"name":"Pseudolife Memory","version":""}}`, `I` being `_MCP_INSTRUCTIONS`. `ping` gives `{}`; `prompts/list`, `resources/list`, `resources/templates/list` give empty lists; unknown methods (including `subscriptions/listen` in this era) give -32601 `Method not found` with `data` set to the method. Requests need no prior `notifications/initialized`. | `mcp_server.py:122, 163-166, 3147-3159`; SDK lowlevel server |

### Tools, tiers and identity

| # | Exact item | Source |
|---|---|---|
| M9 | The catalogue is 38 tools in registration order; each tool object (`annotations`, `description`, `inputSchema`, `name`, `outputSchema`) is byte-equal to Python's serialization. `src/mcp/tools.jsonl` holds them as recorded by `harness/mcp_catalogue.py`, one object per line. | `mcp_server.py:_tool`, `_bind_arguments`, `_annotations` |
| M10 | Tiers are cumulative: minimal 10, core 24, full 38 (`src/mcp/tiers.json`). `tools/list`, with any params, returns `{"tools":[...]}` for the caller's tier, with no `nextCursor`. | `mcp_server.py:208-213, 3133-3137` |
| M11 | The default tier is `PSEUDOLIFE_MCP_TOOLSET`, stripped and lowercased; unset or unknown means `full`. `PSEUDOLIFE_MCP_TIER_MAP` is `writer:tier,...`: each part stripped, split at the first `:`, both sides stripped and lowercased; malformed parts are skipped and a later part wins. | `toolset_tiers.py:36-65` |
| M12 | The tier key is the bearer's principal when it is not `default`; for `default`, the `X-PL-Writer` header (first value, latin-1, empty means unset), else `PSEUDOLIFE_WRITER_ID`, else none (the shared bucket). Resolution: the key's override (stripped, lowercased) → the tier map → the stored principal's tier (only when the key is the request's own named principal) → the default. | `mcp_server.py:3001-3037`; `toolset_tiers.py:115-130` |
| M13 | `memory_toolset(action)`: `status` gives `{"current","default","ladder","adds"}` (`adds` text exact). `expand` steps up one rung (floor minimal); `collapse` steps down to the key's floor (tier map → stored tier → default). No move gives `{"changed":false,"current":C,"reason":R}` with `R` `already at full` or `already at your floor (F)`. A move stores the override for 12 h, answers `{"changed":true,"current","previous","visible_tools_added","visible_tools_removed","list_changed_sent":true}` (names sorted), and pushes `{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}` to the session's open GET stream. | `mcp_server.py:1419-1497`; `toolset_tiers.py:67-112` |
| M14 | `tools/call` with non-object params, no string `name`, or non-object `arguments` gives -32602 `Invalid request parameters`, `data: ""`; missing or null `arguments` is `{}`. A name not in the catalogue gives `{"content":[{"text":"Unknown tool: N","type":"text"}],"isError":true}`. Hidden tools stay callable. | SDK `call_tool`; `toolset_tiers.py` docstring |
| M15 | A dict result is `{"content":[{"text":T,"type":"text"}],"isError":false,"structuredContent":D}`, `T` being the dict indented by 2. A refusal has the same shape with `isError: true` and the `_error_payload` object: the code from a `code` attribute or a `code` / `code: detail` message, prose as `invalid_argument`, a missing file as `file_not_found`, anything else as `internal_error` (plus `mutation: "unknown"` off the read-only tools); `coordination_unavailable` adds `mutation: "unknown"` off the read-only tools. | `mcp_server.py:240-302, 309-314` |
| M16 | memory_toolset's argument binding: an unknown name gives `unknown_parameter`, one sentence per name (`unknown parameter 'K' for memory_toolset; did you mean 'G'?` from difflib at cutoff 0.6, else ending `.`), the last closed by ` Accepted: action`, with `param` the first name and `accepted: ["action"]`. A missing `action` gives `action: Field required`; any other value gives `action: Input should be 'expand', 'collapse' or 'status'` (both `invalid_argument`, `param: "action"`). | `mcp_server.py:358-432` |
| M18 | Bound identity, after the bearer gate and before the MCP layer: with neither `X-PL-Bank` nor `X-PL-Principal`, nothing happens. Otherwise `bound_identity` parses them (both non-empty; the principal is the canonical percent-encoding of 1 to 256 characters with none below U+0020), the bearer's principal is resolved as the board resolves it, the board's `context` action runs for it (dispatched directly, outside the coordination hub's admission limit, and finishing if the caller disconnects), and `(bank_id, principal)` must equal the binding. Any refusal answers the coordination error (`bank_identity_mismatch` 409, `principal_not_allowed` 403, and so on: `_send_coordination_error`) in place of MCP, on every request of a session. Tokenless installs stay 501 before this check. | `web/api.py:912-939`, `coordination.py:475-496` |
| M17 | The writer is the named principal, else `X-PL-Writer` (non-empty), else `PSEUDOLIFE_WRITER_ID`, else `unknown`; the session is `X-PL-Session` as sent. Tool bodies receive both through `CallIdentity` (`src/mcp/dispatch.rs`). | `writer_context.py:130-205`; `service.py:893` |

## W2-D: memory_search over MCP (2026-10-16)

`read/mcp_search.rs`, registered in `mcp::dispatch::registry`. Python:
`mcp_server.py:1047-1310` (`memory_search`, `_project_search`,
`_compact_entry`, `_replaced_by`, `_truncate`, `_restates_fact`,
`_cortex_correct_with`, `CORRECTION_NOTE`), `service.py:1881-2112`.

| # | Exact item | Source |
|---|---|---|
| MS1 | After binding: `explain=true` is declared (MCP-SEARCH-EXPLAIN). Unknown band names refuse as `invalid_argument` prose with Python's `repr` lists (the REST route's 400 text now uses the same lists); empty lists are no filter, as `list(x) if x else None`; episodes expand to their subtrees (`_episode_subtree`) and log sorted; a blank query answers `{"entries": [], "query": "", "count": 0, "low_confidence": true, "cortex": []}` with no event. | `service.py:1958-1970, 2091-2108, 6261-6280` |
| MS2 | Otherwise `service.search` with `top_k`, the filters, `min_score`, recency, rerank and BM25 overrides and the caller's `X-PL-Session`, then the cortex-first block on the raw query with `fact_k = min(5, max(1, top_k))` under `memory.mcp.compact_payloads` (else 5) and `attach_served_facts`, all under the search lock (`search_route::search_with_facts`, shared with the REST route). The search result keeps Python's key order: `query, count, low_confidence, entries, [events, events_total]`. | `mcp_server.py:1122-1158` |
| MS3 | Projection: `cortex` always present; each served fact re-selected in `_project_search`'s key order, dates through `_iso_seconds` (local time, to the second, after microsecond rounding), freshness fields off `evergreen`, contender fields when contested, `warning`, `last_known_value`, `re_verify`, labels, `pinned`, and `correct_with` (the resolve path when contested, else `memory_fact_set` when stale or older than TTL/3, slots in Python `repr`); `correction_note` when any fact has one; entries restating a fact value dropped and `count` recomputed; `low_confidence` only with no cortex fact. | `mcp_server.py:993-1029, 1171-1310`, `freshness.py:66` |
| MS4 | Compact entries (not `verbose`): `id, text, source, tags, score`, `date` (the local write date), `superseded`, `replaced_by` (`id, at, preview` of 120 code points, `verified, current`), labels, then the text capped at `memory.mcp.entry_text_chars` code points with `"…"` and `truncated: true` (no cap when `compact_payloads` is false). Verbose entries are the service's dicts. | `mcp_server.py:858-948` |

`harness/w2d_search.py --only mcp-search` compares 20 calls against the
Python oracle on a derived template that adds an aged volatile fact, a
contender and a restating entry, with coverage requirements for each
projection branch; the `mcp-search-no-dedup` and
`mcp-search-no-correct-with` mutants must be caught.

### memory_recent over MCP (2026-10-17)

`read/mcp_reads.rs`, registered in `mcp::dispatch::registry`. Python:
`mcp_server.py:1320-1343`, `service.py:2171-2216`.

| # | Exact item | Source |
|---|---|---|
| MR1 | Every resident entry that passes the filters: `sources` and `episodes` match exactly (an episode filter does not reach child episodes, unlike search); `tags` are normalised (stripped, lower-cased, deduplicated) and need a non-empty intersection. Each filter is OR within itself and the filters AND across; an empty list, or tags that normalise to nothing, is no filter. Superseded entries are listed. With a tag filter, an admitted entry that stores a list or object tag refuses the call `invalid_argument` with Python's `set()` TypeError text, `unhashable type: 'list'` (or `'dict'`) for the first such tag, met band by band; the text is Python 3.10-3.13's (3.14 rewords it), and the pinned oracles run 3.11 and 3.12. No side effects: no access counts, no telemetry. | `service.py:2171-2205`; `mcp_server.py:247-275` |
| MR2 | Newest `(timestamp, seq)` first (`seq` is assigned in id order at hydration, then per store), at most `n`. Each entry is `_entry_to_dict` without `score`, then `_annotate_supersession` over the resident bank; the answer is `{count, entries}`. | `service.py:2205-2216` |
| MR3 | Not `verbose`: each entry through `_compact_entry` with no text budget (never `truncated`); `count` is unchanged. `verbose`: the service's dicts. | `mcp_server.py:1343, 957-961` |

`harness/w2d_search.py --only mcp-recent` compares 10 calls on the derived
template, with coverage for the compact `replaced_by`, the verbose
supersession keys and an exact episode filter that leaves the child
episode's entry out; the `mcp-recent-oldest-first` mutant must be caught.

### memory_fact_get over MCP (2026-10-17)

`read/fact_get.rs`, registered in `mcp::dispatch::registry`. Python:
`mcp_server.py:1561-1689`; `service.py` `cortex_lookup` (4157-4287),
`_annotate_set_slot_evidence` (4346), `cortex_contenders` (4419),
`cortex_candidates` (4432), `entity_ref` (7556); `memory/cortex.py`
`lookup`, `slot_kind`, `members`, `candidates_for`.

| # | Exact item | Source |
|---|---|---|
| FG1 | The record: the slot's current scalar (keys normalised by `_norm_key`). On a miss, the graph node `find_entity(norm_name(entity))` gives retry names: its canonical, then its aliases in `find_entity`'s order, minus any alias that is another entity's canonical, deduplicated on the cortex key; the first name with a scalar wins, and the record keeps its own slot. Still nothing: the first of those names whose `slot_kind` is "set" serves `{kind: "set", entity: <that name>, attribute, members, removed}` (current members, then removed member rows, in record order). Else null. | `service.py:4157-4268`; `cortex.py:1182-1216, 1519` |
| FG2 | A scalar is `_cortex_record_to_dict` with `time.relative_age` (no `age` when false) and the stale policy, then `source_entries` (the slot's `memory_traces` entry ids) and the trace-invalidation `re_verify`; a set's members and removed rows are record dicts without those, and a set with members carries the slot-wide `re_verify`, seen at the latest member's `last_confirmed or asserted_at`. Serving a scalar, or a set with members, writes one `slot_reads` upsert when `memory.cortex.read_tracking` is on; a failure is logged and swallowed. | `service.py:4141, 4237-4287, 4300-4357` |
| FG3 | `contenders`: the slot's contested records as full record dicts (always with `age`), with no alias handling and no projection. | `service.py:4419-4430` |
| FG4 | Only when the record is null or a set without members and there are no contenders: `candidates`, under the queried name and, when its graph node's canonical differs by `norm_name`, that canonical. Per name, same-entity current records at other attributes (most recently asserted or confirmed first, `score` null, `why` "same_entity") then embedding-similar slots (the query `"{entity} {attribute}"` embedded without the query prefix; cosine at least 0.35, assistant facts penalised; `score` to four places, `why` "similar_slot"), five each; merged, deduplicated on the lower-cased slot, same-entity first then by score (a null or zero score sorting as 1.0), five in all. | `service.py:4432-4463`; `cortex.py:1701-1737` |
| FG5 | A non-empty record gets `correct_with` from `_cortex_correct_with` with `contested` = whether there are contenders, and then the answer carries `correction_note`. `entity_ref` (`entity_id, canonical, etype, aliases`) when the queried name has a graph node. Keys in order: `record, contenders, candidates, correction_note, entity_ref`. | `mcp_server.py:1588-1617` |
| FG6 | Not `verbose` and `memory.mcp.compact_payloads`: the record through `_lean_fact_record` (the allow-list in its order, then `effective_confidence` when the class decays; set members and removed rows projected the same way). | `mcp_server.py:1618-1689` |

`harness/w2d_search.py --only mcp-fact-get` compares 17 calls on a template
derived with `w2d_seed.py --mcp-extra --fact-extra` (an aliased entity with
a shadowed alias, a set with a removed member, an emptied set, a stale
fact, trace rows), with the `slot_reads` writes in the compared bank state
and coverage for each branch; the `mcp-fact-get-no-alias-retry`,
`mcp-fact-get-no-candidates` and `mcp-fact-get-no-lean` mutants must be
caught.

### memory_get over MCP (2026-10-17)

`read/mcp_reads.rs`. Python: `mcp_server.py:1518-1539, 718, 910`;
`service.py` `get_entry` (6088-6138), `_record_retrieval_use` (6043);
`storage/postgres.py` `get_entry` (4217), `facts_for_entry` (4205),
`bump_access_count` (4286), `credit_retrieval_use` (2189).

| # | Exact item | Source |
|---|---|---|
| MG1 | The `entries` row by id; none (an unknown id, or one past BIGINT, which Python's query compares as numeric and matches nothing) answers `{found: false, faded: true, note: _FADED_NOTE}`. | `service.py:6100-6106`; `mcp_server.py:1529-1530` |
| MG2 | A found row: `access_count + 1` in the database (Python reads, then bumps in a transaction of its own, both under the service lock, so concurrent gets each answer the count after their own bump; Rust gets the same by reading and bumping in one transaction with the row locked), and on the resident entry if held; then `consolidated_into`, the current facts traced to the entry (`entity, attribute, value`, by slot key); then, with `memory.retrieval_log.enabled`, one `retrieval_uses` row (`used_via` "get") for the most recent event within `use_window_seconds` (default 3600) that served the entry under the caller's session (header, active pointer, legacy transport); a failure there is logged and swallowed. | `service.py:6043-6070, 6107-6111`; `postgres.py:2189-2215, 4205-4215, 4286-4290` |
| MG3 | The answer: `found, entry_id, text, source, reinforcements, explicit_reinforcements, access_count` (the row's plus one), `consolidated_into`; a superseded row adds `superseded` and the successor annotation over the resident entries (the row excluded by id). | `service.py:6112-6138` |
| MG4 | The MCP layer: a superseded answer with a successor text gains `replaced_by` (as a compact search hit), and `superseded_at`, `superseded_by_text`, `superseded_by_id`, `supersession_verified`, `superseded_by_current` are removed. An internal failure carries `mutation: "unknown"` (not a read-only tool). | `mcp_server.py:1531-1539, 910` |

`harness/w2d_search.py --only mcp-get` compares 8 calls on its own template
derived with the fact-get seed flags: a search in one session, then gets of the entry it served (the
credit, then the access count showing the first bump), a superseded and a
traced entry, a get from another session, and two faded ids, with
`entries.access_count` and `retrieval_uses` in the compared bank state; the
`mcp-get-no-db-bump` and `mcp-get-no-use-credit` mutants must be caught.

### memory_stats over MCP (2026-10-17)

`read/stats.rs`. Python: `mcp_server.py:1423-1428`; `service.py` `stats`
(3402-3480); `memory/cms.py` `stats` (2811-2875), `capacity_warning`
(2774-2809), `retrieve` instrumentation (1086-1437), `_on_band_evict`
(2748); `storage/postgres.py` `retrieval_log_health` (2451),
`read_audit` (2481), `graduation_report` (2325).

| # | Exact item | Source |
|---|---|---|
| MS1 | The CMS block, in order: `bands` (per band `name, size, capacity, update_interval, retention_policy, hit_rate` = `round(hits / max(1, queries), 4)`, `hit_count`), `preset`, `total_memories`, `interaction_count`, `logical_turn_count` (0), `retrieval_queries`, `true_drops`, `capacity_warning`, `weights_reset`, `slot_index_shadow_divergences`, the `instant`/`short_term`/`long_term` `_bank_size` and `_bank_capacity` aliases (by name, else `bands[0]`, `bands[1]` or `bands[0]`, `bands[-1]`), `reference_bank_size`, `reference_document_count`. Band `update_interval`: flat 1,000,000,000; continuum 1, 1, 2, 5, 15, 50, 200, 1000; custom as configured (default 1). | `cms.py:253-258, 2811-2875`; `miras/presets.py:38-100` |
| MS2 | The process counters. `retrieval_queries` counts each ranking with `count_access` (`memory_search`, `/api/search`; not the warmup probe); a band's `hit_count` counts the rankings whose candidate pool held one of its entries: a dense candidate at or above the floor, or a slot, BM25 or timeline injection. `true_drops` counts capacity evictions out of the last band. `retrieval_log.write_errors` counts swallowed failures of the search event write, the served-facts attach (including a zero row count) and the `memory_get` use credit; `read_audit.slot_read_write_errors` counts swallowed slot-read bumps. | `cms.py:1086-1437, 2748`; `service.py:4154, 5975, 6035-6040, 6068` |
| MS3 | `capacity_warning`: null unless the last band's fill (`size / capacity`, capacity above 0) is at least 0.8; else `{band, size, capacity, fill: round(fill, 3), message}`, the message naming the band by `repr`, sizes with thousands separators, and "is full: ..." at capacity or "is NN% full: ..." (`{fill:.0%}`) below it. | `cms.py:162, 2774-2809` |
| MS4 | `communities` (the `communities` row count) and `graph_digest_at` (`computed_at` of the `graph_digest` meta value, null when absent or falsy; a truthy non-object fails the call, as Python's `.get` raises); then `true_drops_total` = `int(count)` of the `capacity_true_drops` record (0 without one) and `last_true_drop` = its `last_at, last_entry_id, last_source, last_superseded` as `at, entry_id, source, superseded` (null without a record); any failure in that pair serves both as null. | `service.py:3409-3433`; `postgres.py:49, 2717` |
| MS5 | `retrieval_log`: `events, last_event_at, uses, lesson_searches` (on failure `events, uses, lesson_searches, last_event_at` null and `unavailable: true`), then `enabled` and `write_errors`. `read_audit`: `entries` (`total, never_read, never_read_pct, reads_total, reads_median, top_decile_read_share, by_age` with 14- and 45-day buckets), `worst_sources` (at least 5 entries, five worst never-read shares), `slots`, `reinforcements` (on failure `{unavailable: true}`); then `graduation_candidates` (30-day window, at least 8 sessions, share at least 0.6, ten rows, text cut to 200 characters; `[]` on its own failure), `slot_tracking_enabled`, `slot_read_write_errors`. | `service.py:3434-3478`; `postgres.py:2325-2381, 2451-2575` |
| MS6 | The MCP layer serves the dict as is; `memory_stats` is read-only (no `mutation` on failure). | `mcp_server.py:315-320, 1423-1428` |

`harness/w2d_search.py --only mcp-stats mcp-stats-continuum
mcp-stats-capacity` compares three scenarios. `mcp-stats` (7 calls on the
fact-get template plus `--stats-extra`: retrieval events from eight sessions,
a community, a graph digest) reads the stats before and after a dense hit, a
slot query, a miss, a get and a store. `mcp-stats-continuum` reads them
around two searches with eight bands. `mcp-stats-capacity` sizes one custom
band one past the bank, so the warning reads "is NN% full" (the percentage
rounds, so a large bank reads 100), then "full", and a second store evicts a
true drop. The event and drop times are compared by presence and type, the
answers key order and all, and `worst_sources` rows tied on their share in
either order. The `mcp-stats-no-tier-hits`, `mcp-stats-no-graduation`
and `mcp-stats-no-capacity-warning` mutants must be caught.

### memory_fact_set over MCP (2026-10-17)

`fact_set.rs` (the tool and `service.cortex_write`), `stores/cortex_write.rs`
(the engine), `stores/cortex_sync.rs` (persistence), `hlc.rs`,
`junk_names.rs`, `freshness.rs`; registered in `mcp::dispatch::registry`.
Python: `mcp_server.py:1692-1747`; `service.py` `cortex_write`
(3916-4040), `_save_cortex` (3746), `_ensure_subject_entity` (1859),
`_emit_correction_signal` (4961); `memory/cortex.py` `write_fact` (438-619)
and helpers; `storage/sync.py` `sync_cortex_slots` (225).

| # | Exact item | Source |
|---|---|---|
| FS1 | Bound arguments: `entity`, `attribute`, `value` (non-blank, passed unstripped), `origin` (user/action/agent; omitted is `"agent"`), `confidence` (0..1, default 0.8; an integer binds as a float), `episode`, `freshness_class` (default `"auto"`), `authority` and `distortion_tolerance` (default `"auto"`). No projection. Not read-only: an internal failure carries `mutation: "unknown"`. | `mcp_server.py:249-275, 763, 1692-1747` |
| FS2 | `resolve_authority` / `resolve_distortion` on the raw `value`, before the lock: `"auto"` is the inferred label or inherit-unless-restated, an explicit value restates it. | `service.py:3970-3971`; `labels.py:235-251` |
| FS3 | Under the service lock (Rust: `Ready.write`, held to the answer). `"auto"` freshness is `resolve_class(kind, _norm_key(attribute))`, `kind` from `entity_kinds` keyed by `_norm_key(entity)`, read once per process (`SELECT entity_norm, kind FROM entity_kinds`) and cached; only a `system` entity's state attribute (not an event) is `volatile`. The stored class goes through `_norm_freshness` (unknown is `evergreen`). | `service.py:3903-3914, 3972-3977`; `freshness.py:114-127`; `postgres.py:1932-1936`; `cortex.py:62` |
| FS4 | Two document-side encodes (no query prefix), in order: the claim `f"{entity} {attribute} {value}".strip()` and the slot `f"{entity} {attribute}".strip()`. The slot embedding is resident only (never persisted). | `service.py:3979-3981`; `cortex.py:302` |
| FS5 | Writer: a named principal, else `X-PL-Writer`, else `PSEUDOLIFE_WRITER_ID`, else `unknown`. Session: `X-PL-Session`, else the live active-session pointer, else the legacy transport id. An episode handle (8+ characters; an open root, a reaped root reopened inside the resume window, or a tombstone recreated) sets the session to its root's key unless a header session is present; a non-empty handle that resolves nothing adds `episode_warning`. | `service.py:974-985, 3982-3988, 6799` |
| FS6 | The service HLC ticks after identity and before `write_fact` (so a refused write still advances it). It is seeded from the highest stamp among `facts`, `world_facts` and `lessons` and `meta.coordination_hlc_highwater`; Rust observes the startup high-water mark before each tick, which equals Python's observe at reseed. | `service.py:1642-1669, 3998`; `hlc.py:40-56` |
| FS7 | `write_fact`: a slot with current set members raises; the tool answers `invalid_argument` with `slot holds a set; use memory_set_add / memory_set_remove` and nothing is written. Else the slot is marked dirty. No current scalar: insert (then settle a parked contender holding the same value). Same `_norm_value` as the current: confirm. A compression echo (strictly shorter, 3+ ASCII tokens, 75% contained, no negator, no novel digit token): log `confirm/compression_echo`, then confirm. Confirm: stance cleared, labels inherit-unless-restated, `last_confirmed` and `tx_time` = now, the tier unioned into `support` (a direct write clears its `dream:<tier>` mark), confidence `min(1, max(c + (1 - c) * reinforce_rate, confidence))`, slot embedding backfilled when absent, HLC, writer and session when non-empty; `valid_time` unchanged. | `cortex.py:97-122, 438-560, 621-656, 658-710` |
| FS8 | A conflict: `tier_ok` is `not protect_provenance` or the write's tier rank (user 3, action 2, agent 1) at least the current origin's. Supersede when `tier_ok` and `_should_supersede` (the write's HLC not below the current's, the wall clock breaking an HLC tie, and `confidence >= current - margin`): the old record superseded (`superseded_at`, `superseded_by_value`), log `supersede/newer_wins`, insert with `supersedes_value` and labels inherited from the old, settle a matching contender (its non-assistant tiers carried, log `resolved/contender_value_now_current`). Otherwise the reason is `tier_downgrade` or `below_confidence_margin`; with `protect_provenance` off the value is dropped (log `contested/<reason>`, the answer names the current); on, `_contend`: a contender with the same value is refreshed like a confirm (log `contested/contender_confirmed`), else the prior contender is superseded and a new `contested` record parks (labels from the current, `supersedes_value` = the prior contender's value), log `contested/<reason>`. | `cortex.py:597-619, 715-729, 1222-1361` |
| FS9 | An audit row is `{entity, attribute, old_value, new_value, decision, reason, confidence_delta: round(new - anchor, 4), timestamp, writer_id, session_id}` anchored on the current (the contender for a settle); the resident log is seeded from `meta.cortex_supersession_log`, capped at 200 in place, and marks the meta dirty. Config read at store build: `memory.cortex.protect_provenance` (true), `supersede_confidence_margin` (0.15), `reinforce_rate` (0.34). | `cortex.py:148, 731-751`; `service.py:1568-1573`; `sync.py:312-313` |
| FS10 | After the write, the subject's graph node: skipped when `junk_name_reason(entity)` (empty, concat artifact, bare number, status word, bare date, count prefix, dump file, image tag, command string, hash status, action prefix, status shard, metric reading, list artifact, 7+ tokens); else `norm_name(entity)`, and when `find_entity` misses, `ensure_entity(n, display=entity.strip())`, whose mint relinks orphaned fact and lesson rows. A failure is an internal error after the resident write. | `service.py:1859-1874`; `graph_consolidation.py:439-552`; `postgres.py:2732-2756` |
| FS11 | `_save_cortex`: every record of every dirty slot in record order as `_record_to_row` (keys re-normalised, `origin` or NULL, sorted `support` and `provenance`, `value_norm` only on member rows), `entity_id` / `object_entity_id` by `find_entity(norm_name(entity / value))`, then one transaction: `DELETE` the slots, one `INSERT` per row (new ids). Those slots are then clean. With the meta dirty: `meta_set` of the newest 200 log rows, then `advance_dream_cursor` (the stored cursor locked, the larger kept), a transaction each. A failure counts in `/health` `persist_errors` and answers `internal_error` with `mutation: "unknown"`; unpersisted marks are kept. Hydration keeps the load-time healing's dirty marks for the next save. | `service.py:3746-3761`; `sync.py:170-245, 314`; `postgres.py:122-138, 1807-1854, 2665-2709`; `cortex.py:1969-1975` |
| FS12 | A `superseded` write whose `origin` is `user`, with `memory.lessons.enabled` (default true): one `outcome_signals` row `(entity, 'correction', entity, "{attribute}: {old} → {new}", NULL, 'action', episode, now)` in its own transaction, the episode being the handle's root, else the open leaf of the session; a failure is logged and swallowed. The answer is `{"action", ...record}` (`_cortex_record_to_dict` with `age` and the stale policy), plus `current` (the slot's current record, or null) when `contested`, then `episode_warning`. | `service.py:297-344, 4024-4040, 4946-4974`; `postgres.py:2001-2012` |

Readers (`memory_fact_get`, the cortex block of `memory_search` and
`/api/search`) take a snapshot of the resident cortex and never hold its
lock across an await; the writer mutates it in place under `Ready.write`.
Not reachable over MCP, so not ported: the quarantine park
(`force_contend`), the dream guards and tier marks (`dream=True`) and the
assistant-origin park (`support="assistant"`), see `divergences.md`.

`harness/w2e.py live --only fact-set-branches fact-set-newer-wins
fact-set-tuned fact-set-freshness-auto fact-set-set-slot` compares every
answer and the full bank after each call, on templates seeded through the
Python daemon (plus one seed-only SQL insert into `entity_kinds`): insert,
confirm, compression echo, user supersession with its correction signal,
tier-downgrade and below-margin contenders (refresh and replacement), the
settle, entity minting with an object relink, junk subjects, episode handles
(valid, unknown, empty, short, with a header), every freshness and label
option, newer-wins with `protect_provenance: false`, a tuned margin and
reinforce rate with lessons off, `"auto"` freshness from `entity_kinds`, and
the set-slot refusal beside a slot holding only a removed member. The
`mcp-fact-set-no-supersede`, `mcp-fact-set-no-confirm-reinforce`,
`mcp-fact-set-no-correction-signal` and `mcp-fact-set-no-entity-mint`
mutants must be caught. The engine is also checked offline against write
sequences recorded from `memory/cortex.py`
(`harness/gen_w2e_cortex_write_goldens.py`), and `junk_name_reason` against
`harness/gen_w2e_junk_names_goldens.py`.

### memory_set_add and memory_set_remove over MCP (2026-10-17)

`set_members.rs` (the tools, `service.set_add` and `service.set_remove`),
`stores/cortex_members.rs` (the engine), reusing `fact_set.rs`'s
embedding, entity mint and save, and `stores/cortex_write.rs`'s contend,
settle and audit log; registered in `mcp::dispatch::registry`. Python:
`mcp_server.py:679-684, 721-733, 1750-1782`; `service.py` `set_add`
(4042-4098) and `set_remove` (4100-4133); `memory/cortex.py` `add_member`
(754-1032), `_insert_member` (1034-1081), `remove_member` (1083-1135).

| # | Exact item | Source |
|---|---|---|
| SA1 | Bound arguments: `entity`, `attribute`, `member`, each a required plain string with no minimum length, passed raw (blank included). No projection. Neither tool is read-only: an internal failure carries `mutation: "unknown"`. Errors map as for `memory_fact_set`. | `mcp_server.py:1750-1782` |
| SA2 | The answer is `{action, entity, attribute, member, members_count}` echoing the raw arguments, `members_count` the slot's current members after the call; `_noted(..., key="action")` then appends `note` only for `member_invalid`, `member_capped` (the text names `MAX_CURRENT_MEMBERS`, 100), `member_not_found` and `member_remove_refused` (unreachable here, SA11). | `service.py:4092-4098, 4127-4133`; `mcp_server.py:679-684, 721-733` |
| SA3 | `set_add` under the service lock (Rust: `Ready.write`, held to the answer): one document-side encode of `f"{entity} {attribute} {member}".strip()`, made even for a blank member; writer and session as FS5 (no episode handle); the service HLC ticks before `add_member`, so an invalid or capped add still advances it. `add_member` gets `confidence=0.7`, no provenance, `support=None`, `dream=False`. | `service.py:4042-4091` |
| SA4 | A member whose `_norm_value` (strip, casefold) is empty answers `member_invalid`: nothing dirty, nothing logged. | `cortex.py:846-852` |
| SA5 | A current scalar whose stripped value is number-led (`^[$€£]?[+-]?\d`, `\d` the Unicode decimal digits) is protected: the slot is dirty; the same `_norm_value` confirms the scalar (`last_confirmed` = now, confidence `min(1, max(c + (1 - c) * reinforce_rate, 0.7))`; `tx_time`, HLC, writer, session, labels untouched) and answers `confirmed`; any other member is parked by `_contend` with reason `member_add_blocked_aggregate` (FS8: no tier, `evergreen`, labels inherited from the scalar, the scalar's resident slot embedding, the new HLC, `tx_time` = `valid_time` = now; a contender of the same value is refreshed with `contested/contender_confirmed`) and answers `contested`. | `cortex.py:118-127, 862-888, 1238-1329` |
| SA6 | Any other current scalar converts, one way: the slot is dirty, the scalar `superseded` (`superseded_at` = now, `superseded_by_value` = `"(converted to set)"`) and out of the current index; the log row `convert_to_set/member_add_to_scalar` is anchored on it (new value the member, `confidence_delta` = round(0.7 - c, 4)) and gains `dropped_freshness_class` (a class other than `evergreen`), `dropped_authority` and `dropped_distortion_tolerance` (non-empty labels). The old value becomes the first member: the scalar's entity, attribute, value, polarity, embedding, confidence, provenance, support, writer and session; `asserted_at` = `last_confirmed` = `tx_time` = `valid_time` = the scalar's `asserted_at`; the new HLC; `evergreen`; no labels, no stance, no slot embedding. | `cortex.py:936-970, 1034-1081` |
| SA7 | Dedup over the current members in insertion order (the converted one included): the same `_norm_value`, or a float32 cosine `m @ e / (m.norm() * e.norm() + 1e-12)` of at least `MEMBER_DEDUP_COSINE` (0.9; 0.0 for a member with no embedding). The first match is refreshed: slot dirty, `last_confirmed` = now, confidence `min(1, max(c, 0.7))`, nothing else; answers `member_confirmed`. | `cortex.py:150-157, 972-994` |
| SA8 | With 100 or more current members: one log row `member_capped/max_current_members` anchored on the rejected value (`old_value` = `new_value` = the member, `confidence_delta` 0.0, the writer and session), nothing dirty; answers `member_capped`. | `cortex.py:158-162, 1010-1023` |
| SA9 | Otherwise the slot is dirty and a member row is appended: kind `member`, `current`, the entity, attribute and member as passed, polarity `+`, confidence 0.7, empty support and provenance, `asserted_at` = `last_confirmed` = `tx_time` = `valid_time` = now, the HLC, writer and session, `evergreen`, the claim embedding; then a parked contender of the same value is settled (FS8, its non-assistant tiers carried to the member); answers `member_added`. | `cortex.py:1024-1032, 1034-1081, 1331-1361` |
| SA10 | After `add_member`, whatever its action: `_ensure_subject_entity(entity)` (FS10; a blank, invalid or capped add can still mint the subject), then `_save_cortex` (FS11: member rows carry `value_norm` = `_norm_value(value)`, so the rewrite satisfies the one-current-member-per-value unique index). | `service.py:4087-4091`; `sync.py:170-209`; `schema.py:1565-1578` |
| SA11 | `set_remove` under the service lock: no encode, no HLC tick, no writer resolution, no entity mint. `remove_member` takes the first current member (insertion order) whose `_norm_value` matches; none answers `member_not_found` and changes nothing. A match: slot dirty, `status` = `removed`, `superseded_at` = now, out of the member index (the row is kept), log `member_removed/user_removed` anchored on it with the raw `member` as `new_value`, `confidence_delta` 0.0, writer and session null; answers `member_removed`. Then `_save_cortex` and the SA2 answer. The removal refusal needs a support tier or `dream`, which the tool never passes. | `service.py:4100-4133`; `cortex.py:1083-1135` |

Not reachable over MCP, so not ported: the assistant-origin guards, the
conversion tier guard, the dream direct-write guard and tier mark, and the
removal refusal (`divergences.md`).

`harness/w2e.py live --only set-add-branches set-add-aggregate set-cap`
compares every answer and the full bank after each call, on templates
seeded through the Python daemon (the cap's hundred members are one
seed-only SQL insert with NULL embeddings, run after the seeding daemon
stops, since its exit flush rewrites `facts` from its resident cortex):
the conversion of a labelled
volatile scalar with its dropped-label log row, the member it adds and the
contender it settles; re-adding the old value (by value) and a near
synonym (by cosine); a plain add; blank members (the subject minted all
the same); real and junk-shaped subjects; removal by normalised value, a
repeat, an empty slot, and a re-add beside the removed row; the aggregate
guard's confirm, park, refresh and replacement, with a currency-led total;
the cap, dedup before it, room made by a removal and the cap again. The
`mcp-set-add-no-convert`, `mcp-set-add-no-cosine-dedup` and
`mcp-set-remove-noop` mutants must be caught. The engine is also checked
offline against `add_member` / `remove_member` sequences recorded from
`memory/cortex.py` (`harness/gen_w2e_cortex_write_goldens.py`: members,
aggregate, aggregate_newer_wins, cap), including Unicode-digit and
superscript aggregates and exact slot indexes.

### memory_fact_resolve over MCP (2026-10-17)

`fact_resolve.rs` (the tool and `service.cortex_resolve`),
`stores/cortex_resolve.rs` (the engine), reusing `fact_set.rs`'s save and
`stores/cortex_write.rs`'s contender index, tier mark and audit log;
registered in `mcp::dispatch::registry`. Python: `mcp_server.py:1785-1801`;
`service.py` `cortex_resolve` (4465-4528); `memory/cortex.py` `resolve`
(1417-1513).

| # | Exact item | Source |
|---|---|---|
| FR1 | Bound arguments: `entity` and `attribute` (required plain strings, passed raw) and `accept` (a required lax boolean: `true`/`false`, 0/1 and the pydantic string spellings). No projection, no `_noted`. Not read-only: an internal failure carries `mutation: "unknown"`. The service gets `support="user"`, `dream=False`, `expect_value=None`. | `mcp_server.py:1785-1801`; `service.py:4465-4467` |
| FR2 | Under the service lock (Rust: `Ready.write`, held to the answer). The service HLC ticks only when `accept` is true, before the store is consulted, so an accept that finds no contender or a set slot still advances it; a reject never ticks. No embedding, no writer or session, no entity mint, no correction signal. | `service.py:4492-4502` |
| FR3 | The contender is the first record at the slot (`_norm_key` of both parts) whose status is `contested`, in record order. None: the answer is `{"resolved": false, "reason": "no_contender", "entity", "attribute"}` with the raw arguments; nothing changes. | `cortex.py:1463-1471`; `service.py:4503-4505` |
| FR4 | An accept at a slot with current members (the scalar the contender was parked against has since converted to a set) is refused before anything is marked: `{"resolved": false, "reason": "slot_holds_set", "entity", "attribute"}`, nothing dirty, nothing saved. A reject there is allowed (FR6). | `cortex.py:1472-1473`; `service.py:4506-4508` |
| FR5 | Accept: the slot is dirty. The current scalar (the slot's entry in the current index, if any) becomes `superseded` with `superseded_at` = now and `superseded_by_value` = the contender's value. The contender becomes `current`; `_norm_support("user") or "agent"` is marked (a direct tier clears its `dream:<tier>` provenance mark) and unioned into `support`; `last_confirmed` = `tx_time` = now; the fresh HLC stamp replaces its own; `supersedes_value` = the old current's value when there was one, else its own is kept; `asserted_at`, `valid_time`, confidence, labels, writer and session are unchanged. The current index names the contender. Log `resolved/accepted` anchored on the old current, or the contender when there is none (`new_value` the contender's value, `confidence_delta` = round(contender - anchor, 4), writer and session null). The action is `superseded`, naming the contender. | `cortex.py:1486-1508, 227-234, 731-751` |
| FR6 | Reject: the slot is dirty; the contender becomes `retired` with `superseded_at` = now (`superseded_by_value` untouched); log `resolved/rejected` anchored as FR5. The action is `contested` (as Python names it), naming the current scalar, or the contender when the slot has none (a set slot, or one whose members were all removed). | `cortex.py:1509-1513` |
| FR7 | After FR5 or FR6: `_save_cortex` (FS11), then the answer `{"resolved": true, "accepted": accept, "action", "current", "record"}`: `current` is `lookup(entity, attribute)` (the slot's current scalar, null for a set slot or an empty one) and `record` the named record, both as `_cortex_record_to_dict` with `age` and the stale policy (FS12). | `service.py:297-344, 4517-4528` |
| FR8 | `refused_direct` (`dream=True` only) and `refused_changed` (`expect_value` only), with their `direct_write` and `contender_changed` answers, are unreachable over MCP and not ported (`divergences.md`). | `cortex.py:1477-1485`; `service.py:4509-4516` |

`harness/w2e.py live --only fact-resolve-scalar fact-resolve-set-slot`
compares every answer and the full bank after each call, on templates whose
contenders are parked through the Python daemon's own tools: no contender
(an accept, which ticks); an accept over a labelled user scalar of a
replacement agent contender (supersedes_value the old value), the repeat
answering no contender either way, and a restatement that confirms the
promoted record; an accept of a user contender parked below the margin; a
reject under an action scalar, then a lax `accept: 0`; at a converted set
slot, the accept refused with `slot_holds_set` and the reject allowed;
after every member is removed, an accept with no current (the contender
keeps its own supersedes_value) and a reject with no current; and an
accept of a member parked by the aggregate guard over the protected total.
The `mcp-fact-resolve-no-user-tier`, `mcp-fact-resolve-reject-noop` and
`mcp-fact-resolve-no-set-guard` mutants must be caught. The engine is also
checked offline against `resolve` sequences recorded from
`memory/cortex.py` (`harness/gen_w2e_cortex_write_goldens.py`: resolve),
which each of the three mutants fails, and which checks the HLC stamp an
accept writes. A tick that writes no stamp (a reject, had it ticked, or an
accept with nothing to resolve) shows only in the logical counter of a
stamp written in the same millisecond, which the harness's calls, seconds
apart, never produce; so no mutant targets FR2's reject side. The lax
`accept` binding is checked where it decides the outcome: `"yes"` promotes
a contender and `0` retires one.

### Episode tools over MCP (2026-10-18)

`episode_tools.rs` (the four tools and the service bodies), on the episode
log and handle resolver of `episodes.rs` (W2, W3), plus the board naming in
`board/store.rs` (`Store::title_name`, `Store::title_names`) and
`board/mod.rs` (`Board::title_names`); registered in
`mcp::dispatch::registry`. Python: `mcp_server.py:2431-2494`; `service.py`
`_episode_to_dict` (6283-6294), `episode_start` (6296-6336), `episode_end`
(6338-6394), `_resolve_episode_handle` (6799-6879), `set_session_title`
(6950-6979), `_retitle_session` (6981-7016), `_retitle_locked` (7029-7048),
`episode_summary` (7229-7285); `memory/episodes.py` `start_nested`,
`end_episode`, `end_leaf`, `open_subtree_leaf` (135-216);
`coordination.py` `title_board_names` (1040-1050); `storage/coordination.py`
`_title_name` (1432-1441) and `title_names` (2657-2685);
`harness_names.py` `clean_name` (78-86).

| # | Exact item | Source |
|---|---|---|
| EP1 | Bound arguments: `memory_episode_start(title, hint=None, episode=None)`, `memory_episode_end(episode=None)`, `memory_session_title(title, episode=None)`, `memory_episode_summary(id)`; strings passed raw, and an empty `episode` means no handle. No projection and no `_noted`, except that `memory_episode_summary` compacts `recent_entries` (EP11). The three writers are not read-only (an internal failure carries `mutation: "unknown"`) and run to completion detached from the request, as `memory_fact_set` does; the summary is read-only. Each runs under the service lock (Rust: `Ready.write`). | `mcp_server.py:2431-2494` |
| EP2 | An episode answers as `_episode_to_dict`: `id`, `title`, `started_at`, `ended_at`, `hint`, `closed_by_new_start`, `session_key`, `parent_id`, in that order. | `service.py:6283-6294` |
| EP3 | Start with a handle: the handle resolves as W2.3 (`resume=True`: a closed or swept root reopens). The parent is the root's open leaf strictly below it (`open_subtree_leaf`: the key's open leaf when its parent chain reaches the root and it is not the root), else the root itself; the key is the root's. Without a resolved handle: the session is the caller's (`X-PL-Session`, then the live active-session pointer, then the legacy transport id; an invalid TTL setting is the `invalid_argument` prose), its root opens lazily as W2.2, and there is no explicit parent. | `service.py:6311-6331`; `episodes.py:204-216` |
| EP4 | `start_nested`: with no explicit parent, the key's open leaf (with a key) or the global current leaf (without one) is the parent; the new episode (`uuid4().hex`, `started_at` now, the hint, `closed_by_new_start` false) takes the key, or else the parent's, and becomes current; a parentless start is a root. Then the W2.4 sweep. An unknown handle adds `episode_warning: "unknown or closed episode handle"`. | `episodes.py:135-159`; `service.py:6332-6336` |
| EP5 | End with a handle: it resolves with `resume=False` (an open keyed root only, still touched; a closed or swept root is never reopened); a miss answers `{"closed": null, "reason": "unknown or closed episode handle"}`. The subtree's open leaf (EP3) is closed exactly (`end_episode`: `ended_at` now; when it was current, current pops to its parent if that is open, else none), then the sweep, and the episode answers. No leaf below the root answers `{}` with no write. | `service.py:6362-6378`; `episodes.py:161-170` |
| EP6 | End without a handle: the session as EP3. The candidate is the key's open leaf, or the global current leaf without a key. None answers `{}`. A candidate whose key differs from the session (including a keyed leaf when there is no session) answers `{"closed": null, "reason": "no owned open session"}`. A keyed root (only the session root is open) answers `{}`: the session root is never closed here. Otherwise `end_leaf` closes it, the sweep runs, and the closed episode answers. | `service.py:6379-6394`; `episodes.py:172-181` |
| EP7 | Title: the title is `str.strip()`ped; empty answers `{"ok": false, "reason": "empty title"}` before the lock or initialization. A resolved handle (W2.3, `resume=True`) retitles its root (EP9). A handle that does not resolve answers `{"ok": false, "reason": "unknown or closed episode handle"}`. Without one, no session answers `{"ok": false, "reason": "no session id on this request"}`; else the first open root with the session key is retitled; failing that, a root the idle reaper closed within the session-resume window reopens (W2.2) and is retitled; failing that, `start_session(title, key)` opens a root under the title (an open non-root leaf for the key is returned untitled, as Python's). Then the sweep, and `{"ok": true, "id", "title"}` with the stripped title. | `service.py:6981-7016` |
| EP8 | After the write lock is released (still inside the detached task), a successful title names the board rows registered under the root's session key and root id (`title_board_names`): only when the board is enabled, under the coordination lock once the storage tier is up, as the request's bearer principal (none names nothing; a failure is logged and never fails the retitle). | `service.py:6950-6979`; `coordination.py:1040-1050` |
| EP9 | Retitle (`_retitle_locked`): the episode's title, and every resident entry attributed to it whose `episode_title` differs takes the title in memory and in its row (`UPDATE entries SET episode_title`, one transaction each; a failure is logged and skips that row). | `service.py:7029-7048`; `storage/postgres.py:946-960` |
| EP10 | Board naming (`title_names`): the name is `clean_name(title)` (every Unicode category C character a space, whitespace runs collapsed, cut to 120 characters; blank is none), refused when it names a reserved sender or is shaped like a credential. With a name, a principal and at least one non-empty key: one transaction updates `name`, `name_source='title'` and `name_set_at` (the board clock) on rows with `episode` among the sorted distinct keys, non-empty episode, a credential, `name_source` `''` or `'title'`, a different name and the principal, then appends one `update` event per changed row in agent-id order (actor `daemon`, the row's principal, agent and scope; payload `{"fields": {name, name_source}, "before": {name, name_source}}`). | `storage/coordination.py:1432-1441, 1462-1467, 2657-2685`; `harness_names.py:78-86` |
| EP11 | Summary: an unknown id answers `{"found": false, "id"}`. Else the resident entries attributed to the id (exactly; no subtree), walked band by band and stably sorted newest `timestamp` first (under a multi-band preset Python's within-band order can differ after a relocation, which Rust does not perform: the declared multi-band store divergence); `entry_count`; `tag_distribution` and `source_distribution` as `{tag or source, count}` in `(-count, key)` order; and the newest twenty as `_entry_to_dict` with supersession annotated over every resident entry, then compacted by `_compact_entry`. `{"found": true, <EP2 keys>, "entry_count", "tag_distribution", "source_distribution", "recent_entries"}`. A list or object tag fails with Python's `unhashable type` text as `invalid_argument`. No side effects. | `service.py:7229-7285`; `mcp_server.py:2477-2494` |
| EP12 | Not reproduced: distributions over non-string tags (declared in `divergences.md`); automatic content titles on idle close (the reaper follow-up). | `service.py:7050` |

`harness/w2e.py live --only episode-lifecycle session-title` compares every
answer and the full bank after each call, on templates seeded through the
Python daemon (stores opening session roots, a root closed through the
session-end hook, three board rows registered under the sessions, and the
active-session pointer from the session-start hook). Lifecycle: nesting
twice under a session and popping back to the root (the third end answers
`{}`), a handle that anchors to its own root while another session's
sub-episode is the global current leaf, its handle-scoped ends, an unknown
handle that degrades to a header session whose root opens lazily, ends
refusing unknown and short handles, an end with no session against another
session's current leaf, an empty title, an end with nothing open, a title
with no session, three summaries (a root, a nested sub-episode, an unknown
id), and a closed root whose handle an end refuses and a start reopens,
then its summary.
Titles: a retitle that rewrites the root's entry stamps and names the
unnamed board row (not the agent-named one, nor another session's), its
idempotent repeat, an empty title, a handle retitle, unknown and short
handles, the pointer's root, a new session's root, a closed root resumed by
its key, a reserved name that retitles but names no row, a start and end
through the pointer, and a summary whose tag counts disagree with tag
order. The `mcp-episode-start-no-anchor`, `mcp-episode-end-pops-root`,
`mcp-session-title-no-entry-stamp`, `mcp-session-title-no-board` and
`mcp-episode-summary-count-order` mutants must be caught. Native tests cover
the episode primitives and the title name's cleaning against Python's.

### Outcome and reinforcement tools over MCP (2026-10-18)

`outcome_tools.rs` (both tools and their service bodies), reusing the
episode log and handle resolver of `episodes.rs` and the dereference credit
of `read/mcp_reads.rs` (`credit_retrieval_use`, now taking its `used_via`);
registered in `mcp::dispatch::registry`. Python: `mcp_server.py`
`USED_IDS_CAP` and `_parse_used_ids` (1933-2003), `memory_outcome`
(2006-2059), `memory_reinforce` (1542-1554); `service.py`
`_caller_episode_id` (4946-4959), `record_outcome` (4976-5044),
`_record_signal_used_ids` (5046-5055), `_label_used_entries` (5057-5129),
`_record_retrieval_use` (6043-6070), `reinforce` (6140-6155);
`storage/postgres.py` `add_signal` (2012-2023), `set_signal_used_ids`
(2025-2032), `credit_retrieval_use` (2200-2227), `credit_retrieval_uses`
(2229-2319), `bump_reinforcements` (4315-4329).

| # | Exact item | Source |
|---|---|---|
| OC1 | Bound arguments: `memory_outcome(task, outcome, about=None, detail=None, polarity=None, episode=None, used_ids=None)` (`outcome` one of `success`/`failure`/`correction` and `polarity` `+`/`-`, both refused by the binder otherwise; `used_ids` a list of lax ints or a string) and `memory_reinforce(entry_id)` (a lax int). Neither is read-only (an internal failure carries `mutation: "unknown"`); both run to completion detached from the request, under the service lock (Rust: `Ready.write`). | `mcp_server.py:1542-1554, 2006-2039` |
| OC2 | `_parse_used_ids` on the bound value: none or a boolean is no ids; a list is its items; a string is stripped (empty is no ids), then JSON-decoded: a list is its items, an integer is one item, and anything else (or invalid JSON) is split as text (`[]() ` stripped from the ends, commas as spaces, split on whitespace). Per item: a boolean is ignored; an integer is itself; a string is stripped of whitespace and then of double and single quotes, skipped when empty, else `int()` of it (sign, leading zeros, single underscores between digits, at most 4,300 digits; ASCII digits only, declared) or ignored; anything else is ignored. The JSON decode is Python's (`mcp/py_loads.rs`: its extensions and the same digit limit); over MCP a string is never a JSON array, since the SDK pre-parse decodes one before binding. Ids deduplicate in first-seen order and cap at fifty; the answer is `(ids, ignored, truncated)`. Integers keep their full size. | `mcp_server.py:1933-2003` |
| OC3 | `record_outcome`: an unknown outcome answers `{"recorded": false, "reason": "unknown_outcome", "outcomes": [...]}` before the lock (unreachable over MCP: the binder refuses it). Under the lock, lessons disabled answers `{"recorded": false, "reason": "lessons disabled"}`. Else the handle resolves as W2.3 (`resume=True`; an empty handle is none), then the session as `_resolve_writer` (an invalid TTL setting is the `invalid_argument` prose). The episode is the resolved root, else the session's open leaf (`open_leaf_for`), else none: without a session there is none, never the global current leaf. | `service.py:4946-4959, 5016-5031` |
| OC4 | The signal: one `INSERT INTO outcome_signals (task, outcome, about, detail, polarity, origin, episode_id, created_at)` in its own transaction, origin `action`, `created_at` now, `RETURNING id`; a failure is an internal error. The answer is `{"recorded": true, "signal_id", "task", "outcome"}` plus `episode_warning: "unknown or closed episode handle"` for an unresolved non-empty handle. | `service.py:5032-5038`; `storage/postgres.py:2012-2023` |
| OC5 | With ids: the retrieval log disabled adds `{"used_ids_recorded": 0, "used_ids_reason": "retrieval log disabled"}` and records nothing. Else one statement (`credit_retrieval_uses`, `used_via="outcome"`, the window `memory.retrieval_log.use_window_seconds` back from now): over the sorted distinct ids, every event in the window whose `served` holds an id is a hit; with a session every same-session hit gains a `retrieval_uses` row (`ON CONFLICT DO NOTHING`), without one only the most recent NULL-session hit per id (by `created_at`, then id); per hit id it reports whether an eligible same-session event served it and whether another session's did. An id outside BIGINT fails the statement. | `service.py:5101-5110`; `storage/postgres.py:2229-2319` |
| OC6 | The report, in the caller's order: credited (an eligible event served it), else served elsewhere (only another session's event did), else unmatched. `used_ids_recorded` (the credited count), then `used_ids_unmatched` and `used_ids_served_elsewhere` when non-empty. The record `{"credited", "unmatched", "served_elsewhere"}` (id lists) is written to the signal row's `used_ids` (`UPDATE outcome_signals SET used_ids`, its own transaction; a failure is counted in `retrieval_log_errors` and logged). A failed statement instead counts one retrieval-log error and reports `{"used_ids_recorded": 0, "used_ids_errors": <ids>}`, recording `{"unchecked": ids, "reason": "label write failed"}`. | `service.py:5046-5055, 5110-5129` |
| OC7 | The MCP layer after the service: a `reason` of `lessons disabled` adds its `_OUTCOME_NOTES` note; `used_ids_ignored` and `used_ids_truncated` when non-zero; when `used_ids` was given but no id survived, `used_ids_reason` is `no usable entry ids` (something was ignored) or `empty: no label written (used_ids is positive-only)`. | `mcp_server.py:2040-2059, 712-717` |
| OC8 | `reinforce`: no `entries` row with the id (an id past BIGINT included) answers `{"reinforced": false, "faded": true}`, to which the MCP layer adds the faded note; nothing is written. | `service.py:6143-6145`; `mcp_server.py:1551-1554, 718-720` |
| OC9 | Else `UPDATE entries SET reinforcements = reinforcements + 1, explicit_reinforcements = explicit_reinforcements + 1` in one statement; the resident entry's `reinforcements` follows. | `service.py:6146-6153`; `storage/postgres.py:4315-4329` |
| OC10 | Then, with the retrieval log enabled, the dereference credit (`used_via="reinforce"`): the most recent event in the window that served the id under the caller's session (`IS NOT DISTINCT FROM`, so no session matches NULL-session events) gains one `retrieval_uses` row (`ON CONFLICT DO NOTHING`); a failure is counted and logged, never failing the call. The answer is `{"reinforced": true, "entry_id"}`. | `service.py:6043-6070, 6154-6155`; `storage/postgres.py:2200-2227` |
| OC11 | The session for OC10 is read per call from `meta.active_session_pointer`, as `memory_get`'s credit reads it (the declared retrieval-event session row: the same values). | `divergences.md` |
| OC12 | Not reproduced: non-ASCII decimal digits in `used_ids` tokens; credits naming the entry Python's startup warmup event served (both declared in `divergences.md`). | `mcp_server.py:1986-1991` |

`harness/w2e.py live --only outcome-labels outcome-lessons-off outcome-log-off`
compares every answer and the full bank after each call, on templates whose
entries and logged searches (top 1 each) the Python daemon wrote: an
outcome crediting an id two same-session events served, with unmatched,
served-elsewhere and unknown ids; a text list with junk tokens and a
repeated id, whose credit writes no new row; handle attribution without a header with every optional
field; an unknown handle; a session-less outcome that credits only the most
recent NULL-session event; an empty list; junk only; an id past BIGINT that
fails the label statement; fifty-three ids (none the warmup's entry); a reinforce with its credit, an
unknown id, and a session-less reinforce with nothing to credit; lessons
disabled (the note, with junk and with ids); and the retrieval log disabled
(the reason, and a reinforce without a credit). The
`mcp-outcome-no-used-ids-record`, `mcp-outcome-credit-latest-only`,
`mcp-outcome-ignore-handle`, `mcp-reinforce-no-explicit` and
`mcp-reinforce-no-use-credit` mutants must be caught. The `used_ids` parser
is checked natively against cases whose Python answers were recorded.

### memory_history over MCP, slot mode (2026-10-18)

`read/history.rs` (the tool and `service.history`), on the resident cortex
snapshot and `read/cortex_search.rs`'s `record_to_dict_age`, with the
`as_of` epoch from `mcp/as_of.rs` (`epoch`, beside the binder's
`_check_as_of`); registered in `mcp::dispatch::registry`. Python:
`mcp_server.py:1804-1828`; `service.py` `_parse_as_of` (5746-5756) and
`history` (5758-5831); `memory/cortex.py` `members` (1182-1191),
`slot_kind` (1193-1216), `records_for` (1526-1528).

| # | Exact item | Source |
|---|---|---|
| HI1 | Bound arguments: `entity` (a plain string), `attribute` (a nullable string; omitted or null is chain mode) and `as_of` (a string, a float or none, refused at binding unless `_parse_as_of` can read it). Read-only: an internal failure carries no `mutation`. | `mcp_server.py:1804-1828, 769-782` |
| HI2 | `_parse_as_of`, before the lock: none passes; else `float(value)` (Python's spelling: whitespace, single underscores, `inf`/`nan`), else `datetime.fromisoformat(value).timestamp()` (CPython `datetime_timestamp`): an aware value (an offset with zero whole seconds is UTC, its fraction dropped) as its microseconds since the epoch over 10**6, divided exactly; a naive one as its local seconds since the epoch plus `microsecond / 1e6`, the earlier instant at a fold and the pre-transition offset in a gap (`fold=0`). | `service.py:5746-5756` |
| HI3 | A set slot (`slot_kind` is `set`: no current scalar, and current members or any member row): every member row in record order adds `{"value", "event": "added", "at": asserted_at}` and, when its status is `removed` with a `superseded_at`, `{"value", "event": "removed", "at": superseded_at}`; with a cutoff only events at or before it remain; they sort by `at`, then adds before removes, then member row order. The answer is `{"kind": "set", "entity", "attribute", "count", "versions"}` with the raw arguments. | `service.py:5779-5812`; `cortex.py:1182-1216` |
| HI4 | Otherwise every record at the slot key (`_norm_key` of both parts), in record order, filtered to `tx_time` (or `asserted_at` when it is unset or zero) at or before a cutoff, stably sorted by that time. The answer is `{"entity", "attribute", "count", "versions"}`, each version `_cortex_record_to_dict` with `config.time.relative_age` and the default `annotate` stale policy (never policy-rendered: the audit surface shows what was stored). | `service.py:5813-5831`; `cortex.py:1526-1528` |
| HI5 | With a cutoff, `as_of` is appended (the epoch seconds; a NaN or infinite cutoff is null in structured content, as Python's is; Python's text block spells it `NaN`/`Infinity`, declared). | `service.py:5810-5811, 5829-5830` |
| HI6 | Chain mode (`attribute` omitted) is CH1-CH6 below. | `mcp_server.py:1826-1827` |
| HI7 | Not reproduced: the text block's `NaN`/`Infinity` for a non-finite echo (declared). A naive `as_of` reads in each daemon's own local zone, so the harness compares two daemons in one zone. | `divergences.md` |

`harness/w2e.py live --only history-slot` compares every answer and the full
bank after each call, on a template whose scalar chain (three user writes)
and set slot (two adds, a removal, a later add) the Python daemon's own fact
tools wrote: the chain, under a differently spelled key, a chain whose restated current
value orders after its parked contender by transaction time, with a cutoff
between the first two versions (read from the bank's `tx_time`), with an
offset ISO time, a naive date, epoch zero, NaN and infinity; the set
slot's events, with far-future and early cutoffs; an empty slot; and a
relative word refused at binding. The `mcp-history-ignore-as-of`,
`mcp-history-set-unsorted`, `mcp-history-set-no-removed` and
`mcp-history-asserted-order` mutants must be caught. The epoch is checked natively against `_parse_as_of` answers
recorded from CPython 3.11.

### Lessons and the memory_history chain over MCP (2026-10-18)

`stores/lessons.rs` (the resident lesson store's read side, hydrated at init
as `Ready.lessons`), `lesson_tools.rs` (`memory_lesson_search`) and
`read/history.rs` (`chain`); registered in `mcp::dispatch::registry`.
Python: `memory/lessons.py` (`LessonRecord`, `current_records`, `search`
220-238); `storage/sync.py` `hydrate_lessons` (474-505);
`storage/postgres.py` `load_lessons` (1998-2008), `add_lesson_search_event`
(2411-2428), `entries_for_entity` (4156-4171), `load_graph` (4370-4393),
`find_entity` (2840-2867); `service.py` `_lesson_record_to_dict` (411-430),
`_alias_retry_names` (4157-4183), `lesson_search` (5346-5361),
`_cortex_change_index` (5399-5412), `_annotate_lesson_staleness`
(5414-5443), `chain` (5833-5927), `_log_lesson_search` (5979-6003);
`mcp_server.py` `memory_lesson_search` (2062-2093).

| # | Exact item | Source |
|---|---|---|
| LS1 | Hydration at init: every `lessons` row in id order (`confidence` as its REAL text, `support`/`provenance` as their string items). No Rust path writes lessons, so the store is fixed after init. | `sync.py:474-505`; `postgres.py:1998-2008` |
| LS2 | `memory_lesson_search(query, top_k=5, verbose=False)`; read-only (no `mutation` on an internal failure). Under the service lock (Rust: `Ready.write`, which the log's session resolution needs). | `mcp_server.py:2062-2093` |
| LS3 | The width is `top_k` (the binder always passes one, so the config's `memory.lessons.top_k` is never read over MCP); the floor is `max(0.0, memory.lessons.min_confidence)`. | `service.py:5353-5354` |
| LS4 | The query embedding; current lessons with an embedding scored by f32 cosine against the normalised query, at or above the floor, stably sorted by descending score, cut to the width; a stored vector of another length fails (torch raises). | `lessons.py:220-238`; `service.py:5355-5356` |
| LS5 | Each hit as `_lesson_record_to_dict` (`task, aspect, lesson, about, polarity, outcome, status, confidence` (4 places), `origin`, sorted `provenance`, `asserted_at, last_confirmed, supersedes_value, superseded_by_value, superseded_at`) plus `score` (4 places). | `service.py:411-430, 5357-5358` |
| LS6 | `re_verify`: with any personal-cortex record (the change index: per normalised entity, the latest `max(asserted_at, superseded_at or 0)` over every record), each row's name (`about`, else `task`, stripped) keys the index directly, else through its graph entity (`find_entity(norm_name(name))`, keyed by its canonical); a change after `max(asserted_at, last_confirmed)` adds `re_verify: true` and `re_verify_reason: "facts about <name> changed since this lesson"`. No index, no graph lookup. | `service.py:5399-5443` |
| LS7 | With the retrieval log enabled, one `lesson_search_events` row (`query_text`, the session from `_resolve_writer`, its open leaf as `episode_id`, `served` = `[{entity_norm, attribute_norm, rank, score}]` in hit order, `created_at` now), its own transaction; any failure counts a retrieval-log error and never fails the search. A search with no hits logs an empty list. | `service.py:5979-6003`; `postgres.py:2411-2428` |
| LS8 | The answer `{"count", "entries"}`; without `verbose` each entry keeps only `task, aspect, lesson, about, polarity, outcome, confidence, score, re_verify, re_verify_reason` (those present). | `mcp_server.py:2083-2093` |
| LS9 | Not served: lesson writes (the dream's synthesis, `lesson_forget`, `lesson_restore`) and `lessons_dump` (no MCP tool reaches them yet). | `service.py:5446-5480, 5363-5371` |
| LS10 | Not reproduced: non-string `provenance` items (declared). | `divergences.md` |
| CH1 | `chain(entity, limit=20)`: a name blank after `strip()` answers `{"found": false, "entity", "count": 0, "events": []}` with the raw argument. | `service.py:5845-5849` |
| CH2 | The keys are the name's `_norm_key`, plus, when `find_entity(norm_name(name))` resolves a node, the `_norm_key` of each `_alias_retry_names` name (its canonical, then its aliases not shadowed by another entity's canonical); the node's `display` replaces the name in the answer. | `service.py:5850-5866, 4157-4183` |
| CH3 | Facts: every personal-cortex record (all statuses, record order) whose entity's key is in the keys adds `{"t": asserted_at, "kind": "fact_set", "summary": "<attribute> = <value>", "refs": {"attribute"}}` and, when `superseded_at` is set, `{"t": superseded_at, "kind": "superseded", "summary": "<attribute>: <value> superseded[ by <superseded_by_value>]", ...}`. | `service.py:5868-5883` |
| CH4 | With a node: its source entries (`entries_for_entity`: distinct entries traced to its current facts, newest first, at most `limit`) add `{"t": ts, "kind": "entry", "summary": text[:160], "refs": {"entry_id"[, "episode_title"]}}`; each live edge touching it with a truthy `asserted_at` adds `{"t", "kind": "edge", "summary": "<src display> —<relation>→ <dst display>", "refs": {"relation"}}`. | `service.py:5884-5906`; `postgres.py:4156-4171, 4370-4393` |
| CH5 | Lessons: every current lesson whose task's key, or whose `about`'s, is in the keys adds `{"t": asserted_at, "kind": "lesson", "summary": the lesson, "refs": {"task", "aspect"}}`. | `service.py:5907-5917` |
| CH6 | No events answers CH1's not-found shape; else the events stably sorted by `t`, the last `max(1, limit)` kept: `{"found": true, "entity": the display name, "count", "events"}`. Read-only. | `service.py:5918-5927` |

`harness/w2e.py live --only lessons` compares every answer and the full bank
after each call, on a template whose facts, graph alias (`bearer tokens` ->
`auth tokens`) and edge the Python daemon's own tools wrote, and whose four
lessons (one superseded) are SQL seeds with embeddings copied from entries
the Python daemon stored: compact, verbose and `top_k=1` searches, with
`re_verify` direct and through the alias; a session's search (its
`lesson_search_events` row, whose scores compare within one rounding unit);
chains for an entity with facts (one superseded), an edge and an
about-named lesson, for one whose alias names widen the keys, for a lesson
matched by its task, for an unknown entity and for a blank one; and, in
`lessons-config`, the `min_confidence` floor with the retrieval log off (no
event row). Not exercised: chain entry events (the seeded facts trace no
entries) and shadowed aliases. The `mcp-lesson-search-no-graph-resolve`,
`mcp-lesson-search-no-log` and `mcp-history-chain-no-lessons` mutants must be
caught.

### World cortex tools over MCP (2026-10-18)

`stores/world.rs` (the resident world store: hydration, `write_fact`,
`search`, the per-slot save) and `world_tools.rs` (both tools); `Ready.world`
is hydrated at init beside the personal cortex; registered in
`mcp::dispatch::registry`. Python: `memory/world_cortex.py` (`WorldRecord`,
`write_fact` 98-191, `_insert` 193-213, `lookup` 216-222, `search`
226-239); `storage/sync.py` `_world_record_to_row` (320-348),
`sync_world_slots` (351-359), `hydrate_world_cortex` (372-404);
`storage/postgres.py` `load_world_facts` (1977-1987), `_replace_slot_rows`
and `_insert_canonical_rows` (1818-1866); `service.py` stale policy
(268-294), `_is_safe_source_url` (362-382), `_world_record_to_dict`
(385-408), `_save_world` (3763-3772), `world_write` (4816-4842),
`world_search` (4851-4864); `mcp_server.py` `_WORLD_NOTES` (747-750),
`_world_correct_with` (1038-1049), `memory_world_set` (1834-1868),
`memory_world_search` (1871-1903), `_compact_world` (1907-1921).

| # | Exact item | Source |
|---|---|---|
| WD1 | Hydration at init: every `world_facts` row in id order (`confidence` as its REAL text, `source_url`/`source_quote` or empty, `freshness_class` or `volatile`, `retrieved_at` or 0.0, `version` or 1), then the current index (`{key: i}` over current records, the last winning). No dimension check, as Python checks none for world rows. | `sync.py:372-404`; `postgres.py:1977-1987` |
| WD2 | `memory_world_set(entity, attribute, value, source_url="", source_quote="", freshness_class="volatile", confidence=0.85, retrieved_at=None, content_hash=None)`: the class is a binder Literal, `confidence` in [0, 1]. Not read-only (an internal failure carries `mutation: "unknown"`); the write runs detached, as `memory_fact_set` does. | `mcp_server.py:1834-1868` |
| WD3 | An unsafe citation is refused before initialization: with every U+0000..U+0020 removed and the rest lowercased, a non-empty URL that is not `http://`/`https://` and starts with a scheme (`[a-z][a-z0-9+.\-]*:`) answers `{"action": "rejected", "reason": "unsafe_source_url", "source_url", "note"}` (the `_WORLD_NOTES` note); nothing runs. | `service.py:362-382, 4823-4828` |
| WD4 | Under the write lock: the embedding of `f"{entity} {attribute} {value}".strip()` (a document embedding), the writer (`X-PL-Writer` identity) and session (`_resolve_writer`; an invalid TTL is the `invalid_argument` prose), one HLC tick, then `write_fact` at `now`, then `_save_world`. | `service.py:4829-4842` |
| WD5 | `write_fact`: the slot is marked dirty first. An empty slot inserts a current record (`polarity "+"`, the confidence, citation, normalised class, `retrieved_at` or now, the hash, `asserted_at` = `last_confirmed` = `tx_time` = `valid_time` = now, the HLC stamp, writer and session, version 1). | `world_cortex.py:98-139, 193-213` |
| WD6 | A value equal under `_norm_value` confirms: `last_confirmed` = `tx_time` = now, `retrieved_at` (or now), the class, `confidence = min(1, max(old, new))`, the URL, quote and hash only when non-empty, the HLC stamp, writer and session only when non-empty; `asserted_at` and `valid_time` stay. Action `confirmed`. | `world_cortex.py:141-166` |
| WD7 | A different value: a candidate HLC older than the current record's (or equal with an earlier `now`) answers `stale` with the current record, unchanged; otherwise the current record becomes `superseded` (`superseded_at` = now, `superseded_by_value`) and a new current record is inserted with `supersedes_value` = the old value. Action `superseded`. | `world_cortex.py:168-191` |
| WD8 | `_save_world` (`sync_world_slots`): the dirty slots' records, in record order, rewritten in one transaction: DELETE the slots, then one INSERT per row (`origin 'source'`, `support ["source"]`, `provenance []`, every citation and stamp column); the slots are cleared after. A failure counts a persistence error and raises (`internal_error`). | `sync.py:320-359`; `postgres.py:1818-1866`; `service.py:3763-3772` |
| WD9 | The answer: `{"action", <record>}` with `_world_record_to_dict`: `entity, attribute, value, polarity, status, confidence` (4 places), `effective_confidence` (4 places: confidence times the class decay since `retrieved_at`), `stale` (past twice the TTL since `retrieved_at`), `origin "source"`, `freshness_class, source_url, source_quote, retrieved_at, asserted_at, last_confirmed, supersedes_value, superseded_by_value, superseded_at`, then the stale policy (`demote` adds `warning`; `quarantine` moves `value` to `last_known_value`). | `service.py:268-286, 385-408` |
| WD10 | `memory_world_search(query, top_k=5, verbose=False)`: the query embedding; current records with an embedding scored by f32 cosine against the normalised query, at or above 0.0, stably sorted by descending score, cut to `top_k`; a stored vector of another length fails (torch raises). Entries are WD9 records plus `score` (4 places); under `demote` stale entries sort last, stably. Read-only. | `world_cortex.py:226-239`; `service.py:4851-4864` |
| WD11 | The MCP layer: an entry stale, or past a third of its class TTL since `last_confirmed or retrieved_at or asserted_at`, gains `correct_with` (the `memory_world_set(entity=..., attribute=..., value=<the verified current value>, source_url=<citation>)` call with Python `repr` arguments); any such entry adds `correction_note`. Without `verbose` each entry is compacted (`entity, attribute, value, effective_confidence, stale, score, correct_with, warning` when present, then `last_known_value`, then `source_url` and `source_quote` when non-empty). | `mcp_server.py:1891-1921, 1038-1049` |
| WD12 | Not served: `world_lookup`, `world_dump`, `retire`, `restore` and `forget` (no MCP tool reaches them yet); `source_doc_id` is never set over MCP. | `world_cortex.py:241-326`; `service.py:4844-4900` |

`harness/w2e.py live --only world-facts world-facts-demote world-facts-quarantine`
compares every answer and the full bank after each call, on templates whose
three world facts (a slow one, a stale one with `retrieved_at` 1.0, an
evergreen one) the Python daemon's own tool wrote: compact, verbose and
`top_k=1` searches (the stale entry's `correct_with` and note); a
restatement that confirms with a lifted confidence and a new hash; a
supersession with a session header and an explicit `retrieved_at`; two
unsafe schemes (one behind a tab and mixed case); a scheme-less citation;
a wide search; and the personal slot history of the world slot (empty: the
stores are apart). Under `demote` the stale entry sorts last with its
warning; under `quarantine` its value moves to `last_known_value` in both
projections. World embeddings compare within the measured tolerance and a
response `score` within one rounding unit; the world clocks keep only
null-or-not, except that `retrieved_at` does so only within a day of now (a
write without one takes the clock) and an explicit past value compares
exactly. The `mcp-world-set-no-confirm`,
`mcp-world-set-unsafe-allowed`, `mcp-world-set-no-save` and
`mcp-world-search-no-correct-with` mutants must be caught.

### memory_supersede and memory_reinstate over MCP (2026-10-19)

`correction_tools.rs` (both tools and their service bodies), on
`store.rs`'s admission split (`admit`, `record_surprise`,
`insert_entry_row`, `delete_evicted_rows`: `memory_store` runs the same
pieces in its old order) and the resident cortex snapshot; registered in
`mcp::dispatch::registry`. Python: `mcp_server.py` `_correction_noted`
(753-760), `memory_supersede` (1346-1376), `memory_reinstate`
(1379-1418); `service.py` `DERIVED_FLAGGED_CAP` (453), `_inherited_labels`
(682-692), `_correction_noop` (2250-2263), `reinstate` (2681-2807),
`_stage_correction_locked` (2809-2850), `_apply_correction_locked`
(2852-2884), `_apply_correction_under_mutation_lock_locked` (2927-3102),
`_resolve_correction_targets_locked` (3104-3199), `supersede` (3201-3229),
`_retire_entries_locked` (3231-3262), `_derived_from_entries_locked`
(4359-4407); `storage/postgres.py` `supersede_entries` (962-1029),
`_reinstatement_request` (1031-1083), `entry_reinstatement_decision`
(1085-1105), `reinstate_entry` (1107-1204), `slots_for_entries`
(4169-4187), `existing_entry_ids` (4359-4368); `memory/cms.py` `store` and
`_store` (411-611); `memory/labels.py` `_strictest` and its ranks
(254-278).

| # | Exact item | Source |
|---|---|---|
| CO1 | Bound arguments: `memory_supersede(old_text=None, *, new_text, entry_id=None)` (`entry_id` a strict int of any size). Not read-only (an internal failure carries `mutation: "unknown"`); the body runs detached from the request, under the service lock (Rust: `Ready.write`). The answer goes through `_correction_noted`: a refusal's sentence moves from `error` to `note`. | `mcp_server.py:753-760, 1346-1376` |
| CO2 | `new_text` stripped empty answers the no-op `empty_input` ("Replacement text must be non-empty.") before any selector check. A no-op is `{superseded_count: 0, superseded_texts: [], superseded_ids: [], new_memory_stored: false, derived_flagged: [], derived_flagged_total: 0, derived_flagged_truncated: false, reason, note, target_errors}`; nothing is written. | `service.py:2250-2263, 3214-3216` |
| CO3 | Selector checks, in order: both or neither of `old_text` / `entry_id` is `invalid_selector`; an id not positive is `invalid_selector` ("Entry IDs must be positive integers, not coerced values."); a text stripped empty is `empty_input` ("Exact-text targets must be non-empty."). | `service.py:3114-3135` |
| CO4 | Resolution over every resident entry, bands in configured order: an id matches `db_id`, a text matches the stored text exactly (unstripped). No match is `target_not_found`; an id with other than one match, or a text with more than one LIVE match, is `ambiguous_target` with `candidates` (`id`, `source`, `episode_id`, `superseded`) over every match; matches all retired is `target_superseded` with the first match's `superseded_by_text`. A retired twin never makes a text ambiguous. Each error is the selector (`entry_id` as bound, or `text`) plus `reason`; the no-op takes the first error's reason and the sentence "No entries changed. Resolve the reported targets and resubmit exact IDs or unique texts." A selected id held by more than one resident entry is then `ambiguous_target` ("A selected row ID has multiple resident entries; retry after recovery."). | `service.py:3137-3186` |
| CO5 | Then `SELECT id FROM entries WHERE id = ANY(...)`: a failed read is `target_unavailable` ("Could not verify selected rows; retry when storage is available."); a missing row is `target_unavailable` ("Selected rows are no longer persisted; reselect after recovery.") with `[{entry_id, reason: "target_unavailable"}]`. | `service.py:3175-3198`; `postgres.py:4359-4368` |
| CO6 | Labels (`_inherited_labels`): `infer_authority(new_text)` or the strictest target authority (`quoted` 3 > `directive` 2 > `observation` 1); `infer_distortion_tolerance(new_text)` or the strictest target tolerance (`constraint` 5 > `procedural` 4 > `belief` 3 > `preference` 2 > `episodic` 1); unknown labels rank nothing. Then the embedding of the stripped text, and the writer session (`_resolve_writer`; an invalid TTL setting is the `invalid_argument` prose). | `service.py:682-692, 2938-2939, 2839`; `labels.py:254-278` |
| CO7 | One transaction, at one clock reading `stamp`: `supersede_entries` locks the sorted distinct target rows (`FOR UPDATE`, id order); a missing or already retired row raises `ValueError("supersede_entries: missing=[..], already_retired=[..]")` (an `invalid_argument` answer, nothing written); else `UPDATE entries SET superseded_at = stamp, superseded_by_text = new_text`, then one `memory_trace_invalidations` row (`cause 'source_superseded'`, `invalidated_at` = stamp) per `memory_traces` row of the targets, `ON CONFLICT DO NOTHING`. Lessons are untouched. | `postgres.py:962-1029`; `service.py:3231-3262` |
| CO8 | Still in the transaction, with traces enabled, the derived slots: the targets' `memory_traces` rows grouped by `(entity_norm, attribute_norm)`; named by the first current cortex record with that normalized key, else the first record of any status, else the key itself; `has_current_value` when a current record has the key; `source_entry_ids` sorted and distinct; ordered live first, then by normalized key. Traces disabled reads none. | `service.py:4359-4407`; `postgres.py:4169-4187` |
| CO9 | Then the replacement through `ContinuumMemorySystem.store` with `source="correction"`, no tags, the writer session, the CO6 labels, `bypass_surprise_gate` and strict storage: the meta filter refusing it is `_ReplacementRejected`, which rolls the transaction back and answers `replacement_rejected` ("Replacement was rejected; no source entries were retired."). Otherwise W1.5-W1.8 (surprise, slots, episode stamp; the gate bypassed), with the first band's capacity victim chosen as if the targets were already retired (so a full band can evict a target), its row deleted and `meta.capacity_true_drops` counted in the same transaction (`last_superseded` as staged), then the insert. Any failure rolls back; a database error is an internal error. | `service.py:2809-2850, 2969-3009`; `cms.py:411-611` |
| CO10 | After commit, the resident bank takes exactly the staged changes: the victim removed, the targets retired at `stamp` with `superseded_by_text`, the replacement appended under its row id; the surprise history, coreference anchor and interaction count advance. The answer: `{superseded_count, superseded_texts, superseded_ids, new_memory_stored: true, new_memory_surprise (4 places), derived_flagged (the first 50), derived_flagged_total, derived_flagged_truncated}`. | `service.py:3043-3102` |
| RI1 | Bound arguments: `memory_reinstate(entry_id, *, operation_id, expected_text_sha256, expected_source_sha256, expected_superseded_at, expected_superseded_by_text_sha256, evidence_packet_sha256, reviewer_ids, reason)` (`entry_id` a strict int, `expected_superseded_at` a lax float that may be non-finite). Idempotent, not read-only; detached and under the service lock. | `mcp_server.py:330, 1379-1418` |
| RI2 | The caller's principal is the default principal (the singular token, an open install): `ValueError("named_principal_required")` before the service runs, initialization included. The decision's `decided_by` is the named principal. | `mcp_server.py:1404-1409`; `service.py:2694-2696` |
| RI3 | `_reinstatement_request`, in order, each an `invalid_request: <detail>` refusal: `entry_id` positive; `operation_id` a UUID that `uuid.UUID` parses (`urn:` / `uuid:` removed, braces stripped from the ends, hyphens removed, 32 characters, `int(…, 16)`: whitespace, a sign, `0x`, single underscores, Unicode decimal digits) else "must be a UUID", and exactly its canonical lowercase hyphenated form else "must be canonical"; the four hashes 64 lowercase hex characters; `expected_superseded_at` finite; `reviewer_ids` non-empty, each stripped non-empty; `reason` and `decided_by` stripped non-empty. | `postgres.py:1031-1072` |
| RI4 | The request fingerprint: SHA-256 of `json.dumps({version: 1, entry_id, operation_id, the four hashes, expected_superseded_at, reviewer_ids, reason, decided_by}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` (Python float `repr`, the integer's digits). | `postgres.py:1073-1083` |
| RI5 | Then the target's row is mirrored into the resident bank, on every call, replays and refusals included (`_reinstatement_target_mirrored`): no row drops every resident entry with the id; a row without one is loaded (`row_to_entry`, seated in its band or the first, a zero `ts` replaced by now); otherwise the resident entries take the row's `superseded_at` and `superseded_by_text`. An id past BIGINT has neither. A failed read is an internal error. | `service.py:2604-2640, 2728-2734` |
| RI6 | A decision already stored under the UUID: another fingerprint is `operation_conflict`; the same one replays through RI9 before any resident check. | `service.py:2738-2746` |
| RI7 | Otherwise the resident entry with the id: none is `target_not_found`, more than one `resident_durable_mismatch`; not retired is `target_not_retired`; no `superseded_by_text` is `retirement_text_missing`; the SHA-256 of its text, source and `superseded_by_text`, or its exact `superseded_at`, differing from the request is `preimage_mismatch`. Nothing is written. | `service.py:2748-2768` |
| RI8 | `reinstate_entry` in one transaction: `pg_advisory_xact_lock(hashtextextended(operation_id, 41))`; a decision under the UUID now replays (RI9); else the row `FOR UPDATE` and the RI7 checks against the row; any `memory_trace_invalidations` row for the entry is `trace_invalidations_present`; then the append-only `entry_reinstatement_decisions` row (the request's hashes, the prior `superseded_at` and `superseded_by_text`, `reviewer_ids` as JSON, `reason`, `decided_by`, `decided_at` now) and `UPDATE entries SET superseded_at = NULL, superseded_by_text = NULL` guarded on the prior values (not one row is an internal error, rolled back). The resident entry then goes live. | `postgres.py:1107-1204`; `service.py:2778-2807` |
| RI9 | Answers: committed `{action: "reinstated", decision: "committed", operation_id, entry_id, current_state: "live", changed_by_this_call: true, idempotent_replay: false}`; a replay `{action: "already_reinstated", …, current_state, changed_by_this_call: false, idempotent_replay: true}` with `current_state` `missing`, `live` or `retired_again` from the row now; both plus `cortex_changed: false` and `trace_invalidations_changed: 0`. | `postgres.py:1141-1152, 1198-1204`; `service.py:2743-2746, 2804-2807` |

`harness/w2e.py live --only supersede-branches supersede-capacity supersede-gate supersede-meta-filter supersede-traces-off reinstate`
compares every answer and the full bank after each call, on templates whose
entries, retirements, cortex records and set history the Python daemon's
own tools wrote (an exact-duplicate row and the `memory_traces` rows are
SQL seeds: no tool writes either). Supersede: by id with a session header;
the retired source and its replacement as the resident bank now holds
them; by exact text; label inheritance (quoted carried over, a directive
text winning its axis); a retired twin that leaves a text unambiguous;
derived slots that are live, named only by a removed member's records, and
named by their key; every selector refusal (both, neither, zero, negative,
past BIGINT, blank text, blank replacement, unknown id and text, retired by
text and by id, ambiguous with candidates); a replacement the meta filter
refuses when enabled (the whole correction rolls back) and stores when off
(the default); a row deleted and a row retired behind the daemon's back; a full
band evicting the target itself and then a cheaper entry; a surprise
threshold that refuses a near-duplicate as a store and admits it as the
correction of an unrelated entry (the gate bypassed); traces disabled
(no derived slots, the invalidations still written); fifty-two derived
slots (the first fifty, the total, truncated). Reinstate: the default
principal; every request refusal (uppercase, braced, `urn:uuid:`, unhyphenated
and `0x` UUID forms included; the other `int()` forms are unit-checked only);
every target refusal (unknown, past BIGINT, live, retired before the
replacement text was recorded, a mismatched hash, trace invalidations
present); a commit followed at once by a correction of the entry (the
resident entry is live), the replay after that correction
(`retired_again`), a conflicting request under the same UUID, the replay of
a live commit, and the replay after the row was deleted (`missing`); an integral retirement time in the
fingerprint; and the RI5 mirror for a row retired, a row inserted and a row
deleted behind the daemons' backs (the deleted row then cannot be corrected
either). The
correction clocks keep only null-or-not; the replacement's embedding and
surprise compare within the measured tolerance. The
`mcp-supersede-text-first-live`, `mcp-supersede-derived-current-only`,
`mcp-supersede-surprise-gate`, `mcp-supersede-no-trace-invalidation`,
`mcp-supersede-resident-stale`, `mcp-reinstate-default-principal`,
`mcp-reinstate-ignore-invalidations`, `mcp-reinstate-resident-stale` and
`mcp-reinstate-no-mirror` mutants must be caught.

### memory_forget over MCP (2026-10-19)

`forget_tools.rs` (the tool and the four service bodies), on the resident
bank, cortex (`CortexStore::forget`, `retire_unsupported` in
`stores/cortex_write.rs`), world store (`WorldStore::retire`,
`WorldRecord::audit_snapshot`) and lesson store (`stores/lessons.rs`:
writable as `SharedLessons`, `retire`, `audit_snapshot`, the forget refresh,
the per-slot sync with entity linking); `memory.delete_confirm_threshold`
in `config.rs`; registered in `mcp::dispatch::registry`. Python:
`mcp_server.py` `memory_forget` (2098-2156); `service.py`
`_lesson_record_to_dict` (411-430), `_world_record_snapshot` (506-519),
`_forget_entries_locked` (3264-3323), `delete` (3325-3396),
`_save_lessons` (3774-3785), `world_forget` (4876-4897),
`_refresh_source_retired_lessons_locked` (5173-5203), `lesson_forget`
(5449-5474), `_record_store_decision` (5539-5551), `cortex_forget`
(6161-6183); `service_dream.py` `_load_digest_cursor` (3197-3210);
`memory/cms.py` `delete_entries` (2618-2707); `memory/cortex.py` `_log`
(731-748), `retire_unsupported` (1162-1181), `forget` (1610-1636);
`memory/world_cortex.py` `retire` (259-282); `memory/lessons.py` `retire`
(258-282); `storage/sync.py` `_link_entity_ids` (211-222),
`_lesson_record_to_row` and `sync_lesson_slots` (409-450);
`storage/postgres.py` `forget_entry_ids` (1225-1314),
`retire_lessons_for_entries` (1332-1389), `set_meta` (2733-2739),
`record_store_decision` (3527-3543), `slots_for_entries` (4169-4187),
`delete_slot_reads` (4403-4417).

| # | Exact item | Source |
|---|---|---|
| FG1 | Bound arguments: `memory_forget(scope, entity=None, attribute=None, text=None, substring=None, source=None, episode=None, tag=None, confirm_bulk=False)` (`scope` a binder Literal of `memory`, `fact`, `world`, `lesson`). Not read-only; the body runs detached, under the service lock. | `mcp_server.py:2098-2121` |
| FG2 | Scope `memory` with no truthy filter (an empty string is none) answers `{"error": "filter_required", "filters": [text, substring, source, episode, tag]}`; the other scopes without a truthy `entity` answer `{"error": "entity_required", "scope"}`. Nothing runs. | `mcp_server.py:2137-2146` |
| FG3 | After the body: a `deleted_count` (memory) or `removed` (other scopes) of 0 with no `error` gains the note "Nothing deleted: no entry matched every filter (filters AND-combine)." or "Nothing removed: no slot matches this entity/attribute.". | `mcp_server.py:2142-2155` |
| FG4 | `delete` matches every resident entry, bands in configured order, on every given filter (AND, an empty string included): exact text, substring, source, episode id, and the tag stripped and lowercased against the stored tags. The threshold is `memory.delete_confirm_threshold` (20; 0 or less disables it, as does `confirm_bulk`); more matches than it answer `{deleted_count: 0, error: "bulk_confirm_required", would_delete, threshold, sample_texts (the first 20), hint: "pass confirm_bulk=true to remove them"}` and nothing changes. | `service.py:3364-3385`; `cms.py:2647-2682` |
| FG5 | The cascade (`_forget_entries_locked`), before the resident delete: each match's root episode (the parent walk over the episode log); with any non-digest match, matched digests that are retired or whose episode is such a root are retained as history, the rest are deleted. Then the deleted ids' trace slots (the largest source id names each), the surviving roots of unmatched non-digest residents, and the live resident digests of the roots (unmatched or retained). | `service.py:3264-3308` |
| FG6 | The digest cursor (`meta.session_digest_cursor`, `_load_digest_cursor`): `ts` as a float (0.0 when absent), `retry` as an object, `regenerate` / `context_deferred` kept when present, then `regenerate` = the sorted union with the roots that survive. | `service.py:3309-3311`; `service_dream.py:3197-3210` |
| FG7 | `forget_entry_ids`, one transaction, at one clock reading: lock the sorted deleted and digest rows (`FOR UPDATE`); FG8's lesson cascade; the trace slots of the deleted ids that no surviving live entry supports; the agent-origin live edges whose evidence no surviving live entry supports; each such slot's current facts `retired` (`superseded_by_value` names its smallest deleted source); the edges' `superseded_at`; the digests retired (`superseded_by_text` names the smallest deleted id; not exactly the expected set is an internal error); the cursor written (always); the rows deleted (not every one is an internal error). | `postgres.py:1225-1314` |
| FG8 | `retire_lessons_for_entries`: current lessons whose provenance names a deleted id (`entry:<id>`, `?|`), locked in id order; each retires when its synthesis lineage audits (`store_decisions`: `lesson`, `lineage`, `lesson_synthesis`, `verified_dependencies`, the same lesson text and `asserted_at`, integer `source_entry_ids`) name a deleted id and no other named entry still exists; each gets a `retire` audit (`source_cascade`, `source_forgotten`, a snapshot of the row's fields plus `task`, `aspect`, `lesson`, `status: retired`, `superseded_at`, `superseded_by_value`); then one UPDATE. | `postgres.py:1332-1389` |
| FG9 | After commit: the resident cortex retires each FG7 slot (`retire_unsupported`: every current record `retired` at the clock, `superseded_by_value` naming the FG5 source id, one audit row each with no new value and a zero delta; the slot leaves both indexes and is marked dirty, saved with the next cortex save); the resident digests are retired (`superseded_by_text` naming the first deleted id in match order); the deleted matches leave the resident bank; then, when lessons retired, they are mirrored onto the resident store by `(entity_norm, attribute_norm, value, asserted_at)`: a failure is logged, never the forget's, and the mirror stays pending, retried before the next lesson read (`memory_lesson_search`, the history chain) or lesson save, whose failure is then an internal error. The answer: `{deleted_count, deleted_texts (the first 20)}`. | `service.py:3316-3323, 3386-3396, 5173-5203`; `cortex.py:731-748, 1162-1181`; `cms.py:2684-2707` |
| FG10 | Scope `fact` (`cortex_forget`): every record at the entity, or the one slot (normalized keys), is removed (the indexes rebuilt without healing, the slots marked dirty); if any, the cortex save (the per-slot rewrite and the supersession log; a failure counts a persistence error and is an internal error), then the slot-read counters deleted (`slot_reads`, the entity or the slot; a failure is counted, never the call's). Answer `{removed, entity, attribute}`. | `service.py:6161-6183`; `cortex.py:1610-1636`; `postgres.py:4403-4417` |
| FG11 | Scope `world` (`world_forget`, `decided_by="agent"`, no reason): every current record at the entity or slot `retired` at the clock (kept, slot dirty, the current index rebuilt); per record an audit row in its own transaction (`world`, `retire`, the verbatim snapshot: entity, attribute, value, polarity, status, confidence to 4 places, origin `source`, citation, class, `retrieved_at`, hash, `source_doc_id`, `asserted_at`, `last_confirmed`); then the world save. Answer `{removed, retired, entity, attribute}`. | `service.py:506-519, 4876-4897, 5539-5551`; `world_cortex.py:259-282`; `postgres.py:3527-3543` |
| FG12 | Scope `lesson` (`lesson_forget`): the same over lessons (`task` / `aspect`); the snapshot is `_lesson_record_to_dict` (confidence to 4 places, provenance sorted) plus the sorted `support`; then `_save_lessons`. Answer `{removed, retired, task, aspect}`. | `service.py:411-430, 5449-5474`; `lessons.py:258-282` |
| FG13 | The lesson store holds every `lessons` column it rewrites (the vector, the HLC stamps, writer, session, version) and is shared like the cortex: readers take snapshots, writers mutate under the service lock. | `sync.py:474-505` |
| FG14 | `sync_lesson_slots`: the dirty slots' records in record order, rewritten in one transaction (DELETE the slots, one INSERT each: support and provenance sorted, an empty origin as NULL, `version` at least 1), each row's subject and `about` linked to their entities (`find_entity(norm_name(...))`, looked up before the transaction); the slots are cleared after. A failure counts a persistence error and is an internal error. | `sync.py:211-222, 409-450`; `postgres.py:1846-1875` |
| FG15 | The cortex, world and lesson saves are the existing per-slot paths, so a forget's dirty state persists exactly as Python's does: at its own save (fact, world, lesson scopes), or the next one (the cascade's cortex retirements). | `service.py:3746-3785` |
| FG16 | Not reachable: the restore path (`memory_graph_review(action="restore_slot")`, W3-H) and file mode. | `divergences.md` |

`harness/w2e.py live --only forget-memory forget-slots forget-unbounded` compares every
answer and the full bank after each call, on templates whose entries,
facts, world facts, graph edge and slot reads the Python daemon's own
tools wrote (the digest, the trace rows, the lineage lessons with their
audits, and the edge's agent origin and evidence are SQL seeds: only the
dream writes those). Memory scope: no filter, an empty-string filter,
nothing matched (an empty text still filters), a source and its session's
digest together (the digest kept as retired history, the root queued for
regeneration), the bulk refusal and its confirmation, the tag filter
normalized, the trace cascade (one slot retired, one kept by a surviving
source; both read back through `memory_fact_get`), the lesson cascade (one
lesson retired with its audit, one kept; read back through
`memory_lesson_search`), the dream edge retired, an episode filter, a source
with its session's already retired digest (kept), two sources of one slot
forgotten together and then a cortex save (the retired slot rewritten with
the larger source named, and its audit log row), an existing digest cursor
(kept keys, merged `regenerate`), a digest-only selection, and the bulk
guard disabled (`forget-unbounded`). Other scopes: a missing and an empty entity, nothing
removed, one fact slot with every version and its read counter, a whole
entity, a world slot and then the entity (read back through
`memory_world_search`, then nothing left), and lessons by slot and by task
(the rewritten rows linked to their entities, a subject entity included,
read back through `memory_lesson_search`). The forget clocks keep only null-or-not. The
`mcp-forget-no-bulk-guard`, `mcp-forget-delete-digests`,
`mcp-forget-cortex-stale`, `mcp-forget-no-lesson-cascade`,
`mcp-forget-lessons-stale`, `mcp-forget-facts-kept`,
`mcp-forget-edges-kept`, `mcp-forget-slot-reads-kept`,
`mcp-forget-no-world-audit` and `mcp-forget-no-lesson-audit` mutants must be
caught.

### memory_consolidation_candidates over MCP (2026-10-19)

`consolidation_tools.rs` (the tool, the candidate pool and the greedy
clustering), on `search::rank` for the query anchor (the reranker shared
through `read/search_route.rs`'s `LazyReranker`) and `search::entry_json`
for members; registered in `mcp::dispatch::registry`. Python:
`mcp_server.py` `memory_consolidation_candidates` (2499-2545);
`service.py` `_entry_to_dict` (147-196), `consolidation_candidates`
(7294-7460); `memory/consolidation.py` `_cosine` (81-92),
`_cluster_cohesion` (95-110), `cluster_candidates` (113-189);
`memory/cms.py` `retrieve` (740-).

| # | Exact item | Source |
|---|---|---|
| CC1 | Bound arguments: `memory_consolidation_candidates(query=None, episode=None, sources=None, tags=None, top_k=20, min_cohesion=0.6, min_cluster_size=2, max_clusters=10)` (the counts lax integers of at least 1, `min_cohesion` a lax float that may be non-finite). Read-only; under the service lock. | `mcp_server.py:2499-2545` |
| CC2 | The addressable residents: live, and their row id held by exactly one resident entry (the correction path's uniqueness rule). | `service.py:7352-7370` |
| CC3 | A truthy `query` anchors retrieval: the query embedding through `CMS.retrieve` with `top_k`, the sources, `[episode]` when truthy, the tags (normalized), the query text, superseded entries hidden before the caps, `min_score=0.0`, and the configured BM25, recency, fusion and rerank; served entries accrue access as any retrieval does. The candidates are the addressable hits, with their scores, in served order. | `service.py:7372-7390`; `cms.py:740-` |
| CC4 | Else a truthy `episode`: every addressable resident entry, bands in configured order (bank order within a band: the multi-band divergence), whose episode id equals it, one per text (the first), passing a non-empty `sources` (its source listed) and a non-empty `tags` (a stored tag listed, unnormalized), each scored 1.0. | `service.py:7391-7410` |
| CC5 | Neither: `{"query": null, "episode": null, "count": 0, "clusters": []}`. | `service.py:7411-7420` |
| CC6 | With candidates, `SELECT id FROM entries WHERE id = ANY(...)` keeps those with a row (a failed read keeps them all, logged); then the pool is cut to `top_k`. | `service.py:7422-7439` |
| CC7 | `cluster_candidates`: candidates in descending score, ties in pool order; each unclustered one in turn (until `max_clusters` are kept) seeds a cluster of every later unclustered candidate whose cosine to it is at least `min_cohesion`; a cluster smaller than `min_cluster_size` is released, else kept with its cohesion (the mean of every distinct pairwise cosine) and seed score; kept clusters sort stably by `cohesion × size`, largest first. The cosine is float32 over the stored vectors (0.0 for a zero norm; vectors of different lengths fail). | `consolidation.py:81-189` |
| CC8 | The answer: `{query, episode, count, clusters: [{cohesion (4 places), seed_score (4 places), size, members: [_entry_to_dict(member) without score]}]}`, the query and episode echoed as given. | `service.py:7441-7459`; `service.py:147-196` |
| CC9 | What the differential claims: the two daemons' embeddings agree within the measured tolerance (`goldens/w2e-tolerance.json`), so scores, cohesions and seed scores compare within 1.5e-4 and cluster membership can differ for a cosine within that tolerance of `min_cohesion`, or, under the query anchor, where two candidates' scores lie within it and swap seed order (the episode anchor scores every candidate 1.0); every corpus answer agrees. A cohesion rests only on the stored vectors, which both banks hold identically, so it compares within one rounding unit. Exact agreement with the ONNX graph is a cutover question, as for the world cortex, not a porting one. | `harness/w2e.py` |

`harness/w2e.py live --only consolidation-candidates` compares every answer
and the full bank after each call, on a template whose entries
(near-duplicates across three sessions, a retired one and its correction,
a tagged one) the Python daemon's own tools wrote: no anchor and an empty
query; the query anchor with the defaults, a high cohesion floor, a source
filter with a small `top_k`, a tag filter needing normalization, singleton
clusters under a cluster cap, a NaN floor, an episode filter, and empty
lists with an empty episode (no filters); the episode anchor with a low
floor (an exact twin offered once), a source filter that excludes
everything, a tag filter, and an unnormalized tag that matches nothing; and
a row deleted behind the daemons' backs, then the episode anchor's check
and cut. Inside a cluster, `seed_score` compares within 1.5e-4 and
`cohesion` within one rounding unit. The
`mcp-consolidation-unsorted` and `mcp-consolidation-unverified` mutants
must be caught.

`memory_consolidate` needs nothing from the W3-H graph/dream engine:
`service.consolidate` (`service.py:7462-7516`) resolves its targets with
`_resolve_correction_targets_locked` over lists and stores the replacement
through `_apply_correction_locked` (source `consolidation` by default, the
caller's tags, no derived-slot report), the correction machinery that
`memory_supersede` ports. What remains for it is the list selectors (every
target's error reported, the first error's reason), several targets in one
retirement, label inheritance and capacity victim over them, and the tags
path.

### The graph store's write tools over MCP (2026-10-19)

`graph_write_tools.rs` (the four tools, entity resolution with the slot-key
fold), on the ported store layer (`storage/graph.rs`: `ensure_entity` with
its mint-only relink, `find_entity`, `add_alias`, `load_relations`,
`upsert_relation`, `upsert_edge`, `supersede_edge`) and
`graph_read::resolve_relation`; registered in `mcp::dispatch::registry`.
Python: `mcp_server.py` `memory_graph_relate` (2583-2618),
`memory_graph_unrelate` (2621-2631), `memory_alias` (2634-2644),
`memory_relation_define` (2955-2981); `service.py`
`_resolve_or_create_entity` (7570-7599), `graph_relate` (7659-7713),
`graph_unrelate` (7729-7750), `graph_alias` (8146-8163), `relation_define`
(8165-8200); `graph.py` `norm_name` (34-39), `resolve_relation` (76-88);
`storage/postgres.py` 2743-2904, 3017-3148.

| # | Exact item | Source |
|---|---|---|
| GW1 | Bound arguments: `memory_graph_relate(src, relation, dst, origin=None, confidence=0.8, src_type=None, dst_type=None)` (`confidence` a float in [0, 1]); `memory_graph_unrelate(src, relation, dst)`; `memory_alias(entity, alias)`; `memory_relation_define(name, description, transitive=False, inverse_of=None, src_type=None, dst_type=None)`. Each body runs under the service lock (the writer lock), detached from the request. There is no outer transaction: each store call commits on its own. | `mcp_server.py:2583-2644,2955-2981`; `service.py:7659-8200` |
| GW2 | Entity resolution (`_resolve_or_create_entity`, without the dream's write-dedup proposals): `find_entity(norm_name(name))`, canonical first, then alias. A found node with a truthy requested type and a falsy stored one (`None` or `""`) is backfilled through `ensure_entity` (`COALESCE`, so a stored `""` stays), and the answer carries the requested type either way. | `service.py:7570-7584` |
| GW3 | On a miss, the slot-key fold: when a current fact's `entity_norm || '-' || attribute_norm` equals the normalized name (`LIMIT 1`, no order), and that fact's entity normalizes differently, resolution restarts on the fact's entity with the same type. Otherwise `ensure_entity(n, display=name.strip(), etype=type)` mints the node (an empty display stores the canonical; an empty type stores `""`), relinking orphaned fact and lesson rows in the same commit; the answer's display is the stripped name. No junk-name guard applies on this explicit path. | `service.py:7585-7599`; `storage/postgres.py:2743-2838,2885-2894` |
| GW4 | `memory_graph_relate`: the registry (`ORDER BY name`) and `resolve_relation`; an unknown relation answers `{"error": "unknown_relation", "relation": <as given>, "suggestions": <difflib's top 3 at cutoff 0.5>, "hint": "Define it with memory_relation_define, or use 'related-to' as the lawful fallback."}` before any node is touched. | `service.py:7679-7688`; `graph.py:76-88` |
| GW5 | Then `src` and `dst` resolve in that order (GW2-GW3), and for each side whose relation declares a type, a node with a truthy type that differs warns `"{side} '{display}' has type '{etype}' but relation '{relation}' expects '{want}' — edge stored anyway"`. | `service.py:7689-7702` |
| GW6 | The edge: `upsert_edge(src, relation, dst, confidence, origin)` with revival and no evidence (insert, or bump to `LEAST(0.99, GREATEST(new, old + 0.05))`, keep the higher-ranked origin of user > action > agent > other, clear `superseded_at`, restamp `asserted_at`). The answer: `{src, relation, dst, confidence: round(stored REAL, 4), warnings}` with the nodes' displays. | `service.py:7703-7713`; `storage/postgres.py:3051-3116` |
| GW7 | `memory_graph_unrelate`: an unknown relation answers `{"error": "unknown_relation", "relation", "suggestions"}` (no hint). Both names resolve without minting; a missing one answers `{"removed": false, "reason": "unknown_entity", "entity": <src as given if src is missing, else dst as given>}`. | `service.py:7729-7747` |
| GW8 | Else `supersede_edge` stamps a live edge's `superseded_at` (kept for audit, revived by a later relate): `{"removed": <a live edge was stamped>, "src", "relation", "dst"}` with the displays. | `service.py:7748-7750`; `storage/postgres.py:3138-3148` |
| GW9 | `memory_alias`: an alias that normalizes to `""` answers `{"error": "empty_alias"}` first. The owner then resolves (and may mint, GW2-GW3); an alias equal to its canonical answers `{"error": "alias_is_canonical", "entity": <display>}`, with a node minted on the way kept. | `service.py:8146-8159` |
| GW10 | Else `add_alias` binds it (`ON CONFLICT (alias)` reassigns it from another node; an alias equal to another node's canonical is stored and stays dead for resolution) and the owner is re-read: `{"entity": display, "canonical", "aliases": <ORDER BY alias>}`. | `service.py:8160-8163`; `storage/postgres.py:2896-2904` |
| GW11 | `memory_relation_define`: a name that normalizes to `""` or a blank description answers `{"error": "name_and_description_required"}`; a builtin name `{"error": "builtin_relation", "hint": "'<name>' is a builtin and cannot be redefined."}`. A non-empty `inverse_of` normalizes; one neither registered nor equal to the name answers `{"error": "unknown_inverse", "inverse_of": <normalized>, "known": <registry names, sorted by code point>}` (an empty one is no inverse; a blank one normalizes to `""` and is refused). | `service.py:8181-8193` |
| GW12 | Else `upsert_relation` writes the normalized name, the stripped description, the types as given, the inverse and `bool(transitive)` (a custom redefinition overwrites; `builtin` and `created_at` stay): `{"defined", "transitive", "inverse_of", "src_type", "dst_type"}`. Python keeps no resident graph, so no tool publishes anything. | `service.py:8194-8200`; `storage/postgres.py:3026-3047` |

`harness/w2e.py live --only graph-writes graph-relations` compares every
answer and the full bank (entities, aliases, edges, relations, facts) after
each call, on templates whose facts the Python daemon's own `fact_set`
wrote. `graph-writes`: unknown relations with and without suggestions; a
fresh source onto an existing target; the slot-key fold; a mint that
relinks a fact's object, then a fact save that must keep the link;
re-assertion through the bump, the cap and the origin ranks; a 6-digit
confidence, 1.0 and 0; a fresh typed node, a backfill, and a typed node
keeping its type; an empty name, a self-loop and a final sigma; unrelate
through each refusal, a live edge twice, and a revival; aliases empty,
chained, refused as canonical (keeping a minted node), reassigned, through
a slot key, and dead behind another canonical; relate and unrelate through
aliases. `graph-relations`: each define refusal; a typed definition, an
inverse, a self-inverse, an empty inverse and an overwriting redefinition;
typed warnings on both sides; a stored empty type that a later hint answers
but does not backfill; the new names in relate and unrelate; an inverse
refusal whose known list sorts differently by code point and by collation.
`graph-writes` also folds a typed slot key onto a node it backfills, and
onto an owner whose node is gone (the fold mints it, relinking the fact).
The stamp columns (`edges.asserted_at`, `relations.created_at`) compare as
present or absent, so a restamp or a kept creation time is the store
differential's to show (`harness/graph_store.py`). The
`mcp-graph-no-etype-backfill`, `mcp-graph-no-slot-fold`,
`mcp-graph-no-warnings`, `mcp-graph-unrelate-missing-dst`,
`mcp-graph-alias-canonical`, `mcp-graph-redefine-builtin` and
`mcp-graph-inverse-unchecked` mutants must be caught. The store layer's
own differential (`harness/graph_store.py`) keeps its eight mutants.

### memory_graph over MCP (2026-10-19)

`graph_read_tools.rs` (the tool, the neighborhood and whole-graph
projections, `classify_edge`), on the ported GraphStore read model
(`storage::graph::subgraph` over `graph_read::build_subgraph` and
`derive_edges`, `spec-w3h-graph-read.md`) and `graph_read::alias_canonical_map`;
registered in `mcp::dispatch::registry`. Python: `mcp_server.py`
`memory_graph` (2647-2677); `service.py` `_whole_graph` (8224-8287),
`graph_neighborhood` (8289-8405); `memory/graph_store.py` `subgraph`
(56-70); `graph.py` `alias_canonical_map` (42-), `derive_edges` (91-140),
`build_subgraph` (190-231); `memory/graph_review.py` `classify_edge`
(100-113); `storage/postgres.py` `entity_sources_map` (4262-4268),
`load_graph` (4425-4448), `load_communities` (4471-4481).

| # | Exact item | Source |
|---|---|---|
| GR1 | Bound arguments: `memory_graph(entity, depth=1, include_facts=True, to=None, relation_filter=None)`. Read-only; under the service lock (the writer lock), so an answer never shows half of a write. | `mcp_server.py:2647-2671` |
| GR2 | An empty `entity` is the whole graph (`_whole_graph(scope=None, max_nodes=None)`: no scope filter, no cap). | `service.py:8308-8310` |
| GR3 | Else the community assignment (`SELECT entity_id, community_id FROM entity_communities`), then the root through `find_entity(norm_name(entity))`; a miss answers `{"found": false, "entity": <as given>}` (a blank name normalizes to `""` and misses). | `service.py:8318-8322` |
| GR4 | A truthy `to` resolves the same way; a miss records `to_not_found: <as given>` and asks for no path. Then the subgraph of the root at `depth` (clamped to 1..3), with the path to the target: asserted edges in id order, then the derived ones (transitive closures per relation in registry order, then inverses), the nodes within `depth` undirected hops, NetworkX's bidirectional shortest path folded in, and the edges with both ends kept, in order. | `service.py:8324-8335`; `graph.py:91-231` |
| GR5 | With `include_facts`, the resident cortex's current records in record order, each `{attribute, value, origin, confidence (4 places)}` plus `authority` and `distortion_tolerance` when set, filed under `norm_name(entity)` resolved through the alias table (an alias that is another node's canonical is dropped). | `service.py:8338-8361`; `graph.py:42-` |
| GR6 | Nodes by id: `{entity: display, canonical, etype, aliases: <ORDER BY alias>, facts (with include_facts), community: <assignment or null>}`. | `service.py:8363-8377` |
| GR7 | Edges in subgraph order: `{src, relation, dst, derived}` with display names; a derived edge adds `via` (its rule provenance); an asserted one adds `confidence` (4 places), `origin` when truthy, and `tag`: `EXTRACTED` for a user or action origin, else `AMBIGUOUS` below confidence 0.5, else `INFERRED`. | `service.py:8379-8393`; `graph_review.py:100-113` |
| GR8 | The answer: `{found: true, entity: <root display>, depth: <clamped>, nodes, edges, paths: <display names>}` and `to_not_found` when recorded. | `service.py:8395-8405` |
| GR9 | The whole graph: the graph load (entities by id, aliases, live edges by id), the communities, then the source map (`ORDER BY entity_id, source`). Nodes in id order `{entity, canonical, etype, aliases, community, sources, created_at}` plus `facts` (without the v35 labels) with `include_facts`; every live edge `{src, relation, dst, derived: false, confidence, origin (null allowed), asserted_at, tag}`; the answer `{found: true, entity: null, scope: "all", nodes, edges, paths: [], truncated: false, total_nodes, total_edges}`. | `service.py:8224-8287`; `storage/postgres.py:4262-4268,4425-4448` |
| GR10 | `relation_filter`, when truthy and the answer has a non-empty edge list, keeps the edges whose relation, lowercased, contains the filter lowercased (Python's `str.lower`, under the pinned Unicode 14 tables); nodes and paths are untouched. | `mcp_server.py:2672-2677` |

`harness/w2e.py live --only graph-read` compares every answer and the full
bank after each call, on a template the Python daemon's own tools wrote
(facts with labels and one under an alias, user, action, agent and
low-confidence edges, a transitive chain, both inverse directions, a second
component), plus what only the dream writes (community assignments, source
attribution) as SQL seeds: the default neighborhood, depths 2, 3, 0 and 9,
no facts, an alias root, a leaf; paths found, to the root, to an unknown
target, to an empty one and into another component; relation filters
case-folded, empty, emptying and on a miss; unknown and blank roots; the
whole graph with and without facts and filtered; then answers after an
unrelate, a new alias and a revival. Inverse-derived edges compare as a
multiset in their slots (the hash-order row in "Graph and review"). The
`mcp-graph-no-relation-filter`, `mcp-graph-tag-no-ambiguous`,
`mcp-graph-facts-no-alias` and `mcp-graph-no-labels` mutants must be
caught.

### Board tools over MCP (2026-10-16)

`board/mcp.rs`, registered in `mcp::dispatch::registry`. Python:
`mcp_server.py:479-663`, `coordination.py:821-937` (`dispatch`, `agents`),
`repository_claims.py:54-61`, `service.py:8453-8527`
(`coordination_awareness`).

| # | Exact item | Source |
|---|---|---|
| BT1 | Both tools run the board's own dispatch (the REST refusal boundary, gates and post-processing) with the request's lower-cased headers (last duplicate wins) and the bearer's principal, outside the REST hub's admission limit, in a spawned task a disconnect cannot cancel. A board refusal becomes the MCP refusal with its public `code`, `detail`, `param`, `accepted` and `action`; an unexpected failure is `coordination_unavailable` with `mutation: "unknown"`. | `coordination.py:821-839`, `mcp_server.py:243-287` |
| BT2 | `memory_message`: the board action is the tool's `action`; the parameters are `to, text, request_id, reply_to, after, message_id, clears, urgent, limit, peer, ack` in that order without nulls, `urgent` and `ack` only when true. | `mcp_server.py:658-663` |
| BT3 | `memory_agents`, in `agents()` order: `worktree` refuses `file_claim_requires_local_client`; a file selector outside claim/release is an unexpected parameter (with the per-action homes hint), beside `lease` refuses "takes one selector", else becomes `claim:file:<sha256>` (`file_claim_name`, the shim's shared `claim_name.rs`) and adds `file_claim` to the result; park fields in order, an empty `park_reason` sent as null; children, name or park outside update refuse; list refuses status, lease and expect, and without an `X-PL-Agent` (first value) answers coordination awareness, else dispatches `agents {project, task}`; update refuses lease and dispatches the present fields then the park; claim and release need a lease (`missing_parameter`) and refuse project and task; release refuses status and expect and dispatches `release {name}`; claim dispatches `lease {name, ttl, expect, purpose=status}` with ttl 86400 for `claim:` names, else 3600. | `coordination.py:841-937` |
| BT4 | Awareness: `{enabled, available, peers, truncated}`; board disabled answers as is, an unadmitted principal adds `reason: principal_not_allowed`, an uninitialized bank `reason: not_initialized` (never initializing it); otherwise open root episodes with a session key other than the caller's `X-PL-Session`, newest `(started_at, id)` first, capped at `min(awareness_limit, 20)` with `truncated`, each with its whitespace-collapsed title (160 code points), null principal/project/task, `scope` and `capability` "unknown", and the latest touch or entry time under its subtree. | `service.py:8453-8527` |

`harness/board/run.py --only mcp-tools` compares 31 MCP tool calls (both
tools, every refusal above, file claims, park clearing, and awareness over
three open sessions against a cap of two, with and without the caller's own
session) and the board tables after each write against the Python oracle;
the sessions are opened by uncompared `memory_store` calls, and peer times and
minute-stamped titles are placeholders. Both daemons warm up first, so it
runs where the Rust embedder model is present. The tools resolve their
principal as `authenticated_principal` does (a configured bearer only). The
`mcp-agents-no-file-claim`, `mcp-agents-no-awareness`,
`mcp-agents-awareness-self` and `mcp-agents-awareness-order` mutants must be
caught.

### Tool argument binding (2026-10-16)

Every `tools/call` of a catalogue tool binds its arguments before anything
else runs, so a refused argument answers the same whether or not the tool's
body has landed; a body receives the bound map (`dispatch::ToolCall::args`).
Python references are to `mcp_server.py` (`_StringSafeMetadata`,
`_bind_arguments`, `_non_blank`, `_check_as_of`) and the pinned SDK's
`FuncMetadata` (`mcp/server/mcpserver/utilities/func_metadata.py`).
Code: `src/mcp/binding.rs`, `py_loads.rs`, `as_of.rs`.

| # | Exact item | Source |
|---|---|---|
| B1 | The per-tool parameter table, `src/mcp/binding.json`, is generated from each registered argument model's pydantic-core schema by `harness/mcp_binding_table.py`, reduced to a closed vocabulary (str, int, float, bool with lax/strict, literal, list, dict, any, nullable, smart union, the two after-validators, defaults). A schema outside it refuses generation. Literal and bound sentences are recorded from pydantic. `--check` keeps it current. | `_bind_arguments`; pydantic-core schemas |
| B2 | Unknown names first: one sentence per unknown name in argument order, `unknown parameter 'K' for T` then `; did you mean 'G'?` (the `_PARAM_ALIASES` target when it is a parameter of the tool, else difflib at cutoff 0.6) or `.`; the last sentence closes with ` Accepted: <parameters, comma-separated>`. Payload `{"error":"unknown_parameter","message","param":<first unknown>,"accepted":[...]}`. Nothing else is validated. | `_bind`, `_unknown_message`, `_binding_payload` |
| B3 | Pre-parse: a string value of a parameter whose annotation is not exactly `str` or `str \| None` is replaced by its `json.loads` value when that is null, an array or an object (scalars, including bools, stay the string). `json.loads` is Python's: `NaN`, `Infinity`, `-Infinity`, whitespace `[ \t\n\r]`, last duplicate key wins in first position, more than 4,300 int digits refused. | `pre_parse_json` (SDK and override) |
| B4 | Each parameter in declaration order: present values are validated, absent ones take the recorded default, else `Field required`. Lax coercions: int from a bool, a whole float below 2**63 (else `Unable to parse input string as an integer, exceeded maximum size`), or an integer string (first read as a JSON number, so an integer part past 4,300 characters is `Unable to parse input string as an integer, exceeded maximum size`, then SDK-cleaned); float from a bool, an int within the float range (a longer JSON int literal is the wrong type) or an SDK-parsed string, infinity and NaN allowed; bool from 0/1, 0.0/1.0 and the twelve spellings in any case (an i64-valued number or other string fails to parse; any other number is the wrong type); str only from a string, its length in code points; list only from an array, too-long lists refused before their items. Strict fields take only their own JSON type. Bounds are checked in the order le, lt, ge, gt. A smart union takes the first choice that validates strictly, else the first in lax mode, else every choice's errors under its tag. | pydantic-core 2.50.0 lax mode |
| B5 | `_non_blank` refuses a value that `str.strip()` empties (`Value error, must not be blank`). `_check_as_of` refuses a string that neither `float()` nor `datetime.fromisoformat(...).timestamp()` accepts, with the `_check_as_of` sentence: CPython's C parser (3.11 and 3.12 agree: separator any one character, week dates, `,` fractions, unchecked offset parts under 24 h), and `timestamp()`, which refuses a naive instant on 0001-01-01 in every time zone. | `mcp_server.py:757-780`, `service.py:_parse_as_of` |
| B6 | A refusal is `{"error":"invalid_argument","message","param"}`: the message joins `<loc joined by '.'>: <sentence>` with `; `, `param` is the first error's parameter. It is answered through the ordinary refusal envelope (M15). | `_binding_payload` |
| B7 | The bound map holds every parameter in declaration order. A float binds as its shortest round-trip number, an infinity as an overflowing literal and a NaN as the number `NaN`; bodies read floats through `binding::float_arg`. | `model_dump_one_level` |

`harness/mcp_binding_sweep.py` generates 36,880 argument sets (every
parameter of every tool against numeric spellings, strings, JSON texts,
length edges, ISO forms, unknown and missing names) and compares the two
binders in process: Python's own `validate_arguments` on the registered tool
against the Rust binder's probe mode, type-strictly. It needs no database or
model and runs in seconds. `--mutants` requires 14 source mutants to be caught;
the smart union's strict pass, which today's two unions cannot distinguish, is
covered by a unit test. On the wire, the MCP harness's `tool_binding` case
compares 70 binding refusals and an oversized-integer parse error live, and
the Rust unit tests replay the 70 refusals from `harness/goldens/mcp.json`. A body that panics answers `internal_error`.

### SDK parameter validation

`src/mcp/validation-schema.json` records the MCP and MCP-types 2.2.0
parameter schemas, including the Pydantic versions used to generate them.
Regenerate it with `python rust/daemon/harness/mcp_validation_schema.py`.
The table contains all 13 handshake request methods, four client notification
methods and the nine registered request handlers. It includes nested metadata,
task, client implementation, capability and input-response fields. Unknown
extension keys follow each model's extra-field policy.

Non-null round-trip `requestState` is refused before dispatch until sealed-state
issuance and verification are implemented (MCP-SEALED-REQUEST-STATE).
Spec parameter validation precedes handler lookup; registered handler validation
precedes readiness or tier mutation. Unknown methods reach lookup without spec
validation. Notifications validate before changing readiness. The Rust evaluator
implements the table's scalar coercions, unions, required fields, nested objects,
arrays, literals, URL fields and numeric constraints; it does not dispatch Python
or load schemas from the network.

`mcp_run.py <binary> <output> --validation-sweep` generates field variants from
both validation layers and compares HTTP/JSON-RPC codes, readiness and tier on
both live daemons, before and after initialization. The pinned corpus has 18,515
cases and requires all 37,030 paired observations with zero differences. It
retains the separate strict transport corpus and its declared differences. The
CI driver also runs `--validation-coercions`: 2,137 cases and 4,274 observations
covering integer, float and URL spellings, including raw overflowing JSON number
literals. CI installs Pydantic 2.14.0/core 2.50.0; generation and live sweeps
refuse an unreviewed core version. Completed sweep differences are parity
failures; incomplete runs and cleanup failures remain infrastructure failures.
The sweep retains one-shot HTTP exchanges and a fresh MCP session and writer
key per observation. Its sockets use SO_LINGER(1,0) on close so completed
exchanges do not hold client ephemeral ports in TIME_WAIT on Windows. This
setting applies only to the generated validation sweep, not the strict transport
corpus. Responses are read completely before normal closure; failures still
abort the run and release clients. No request is retried or removed.
Oracle recording and golden distillation reject case exceptions, missing cases,
incomplete tool coverage and incorrect per-case or corpus totals before writing outputs.

### Free (MCP surface)

- The text after `Parse error: ` and `Validation error: `.
- SSE keep-alive comments (`: ping - <time>` every 15 s) and their timestamps.
- The value of `mcp-session-id` (a fresh uuid4 hex); only its presence is compared.
- Header order, `date`, `server`, `content-length` and `transfer-encoding`.
- Key order and float text inside a result's `content[0].text`, which repeats `structuredContent`.

### MCP refusal cleanup (2026-10-13)

MCP admission and cleanup share the SDK's 4 MiB body limit. A complete,
in-budget refusal drain keeps the connection reusable; incomplete cleanup,
read failure, an exhausted budget or an unread Expect body closes it. The
shared 250 ms deadline bounds cleanup, including headers-only peers. This
resolves the accidental use of the REST 256 KiB budget without weakening
body admission or changing REST cleanup. `MCP-REFUSAL-CLEANUP` in
`divergences.md` records the remaining Python connection-lifetime difference.
The strict live comparator and committed goldens are unchanged.

Initialization validates known client-capability object fields and the SDK's
boolean spellings for `roots.listChanged`; unknown capability fields remain
open. Standalone SSE notifications have a 16-item delivery queue and the
existing 8-item HTTP output queue. A slow consumer backpressures delivery;
DELETE and disconnect cancel blocked delivery without holding the sessions
lock. Mutant runs require `--baseline <compare.json>` and must add or change
strict differences beyond that ordinary run.
