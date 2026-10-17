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
by rule.

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
Nonempty-session automatic titles, standalone episode APIs and tensor
autosave/exit persistence remain named follow-ups in `divergences.md`.


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
band one past the bank, so the warning reads "NN% full", then "full", and a
second store evicts a true drop. The event and drop times are compared by
presence and type. The `mcp-stats-no-tier-hits`, `mcp-stats-no-graduation`
and `mcp-stats-no-capacity-warning` mutants must be caught.

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
