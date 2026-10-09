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

## Free, and therefore not compared byte for byte

- Exact score floats, and tie order among equal scores.
- Error text other than the listed bodies.
- JSON key order and whitespace.
- The `version` value and the contents of the `memory` object.
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
| S8c | Session identity of the event: the request's `X-PL-Session` header (last value), else the resident active-session pointer while younger than `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` (21600), else null. `episode_id` is that session's open leaf episode, else null. | `service.py:970-1000, 4915-4930`; `writer_context.py:130-142, 194-211` |
| S8d | When the cortex block (S16) serves facts and the event row exists: `UPDATE retrieval_events SET served_facts = <facts> WHERE id = <event>`. The event id never reaches the response. | `web/routes.py:362-378`; `storage/postgres.py:2310-2322` |
| S9 | Access accrual: every entry of the final merged result (dense, slot, BM25, timeline and reference positions; not contiguity neighbours) gains 1 on its resident `access_count` before serialisation, so the response shows the incremented count. The read path does not write `entries.access_count`; the save cadence does (W2-E). The warmup probe (S17) does not count. | `cms.py:1748-1752`; `service.py:3855-3867` |
| S10 | Chronicle events, when the query has a temporal cue (S11's regex), an aggregation cue (`how many`, `how much`, `how often`, `what percentage`, `in total`, `total` + one of `number amount distance cost sum time money`, `altogether`, `each time`, `every time`, `average`, `the most`; word-bounded, case-insensitive) or a year-first date (`\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b`): `plainto_tsquery('english', q)::text`, its ` & ` rebuilt as ` \| `, matched with `to_tsquery('simple', ...)` against `to_tsvector('english', description)` over live rows, `ORDER BY occurred_at ASC NULLS LAST, recorded_at ASC LIMIT 30` (aggregation cue) or 6. Hits give `events: [{description, actor, date, phrase}]` (`date` is `to_char(occurred_at, 'YYYY-MM-DD')`), plus `events_total` under an aggregation cue. No hits, or an empty tsquery: no key. | `service.py:2023-2066`; `cms.py:62-121`; `storage/postgres.py:3333-3361` |
| S11 | Timeline channel, when `memory.search.timeline_channel` (default false) and the query has a temporal cue (`first last when earliest latest before after since until ago`, `how many times`, `how long`, `what order`, `in order`, `order in which`, `sequence`, `chronolog\w*`, `timeline`, and the month names except `may`; word-bounded, case-insensitive): BM25 (the configured k1, b) over the same filtered candidate pool, top 6, min-max normalised; each unseen hit with norm > 0 enters at `0.3 x norm` (dropped under an explicit `min_score` above it), marked `via: "timeline"`. After the cut, the memory part of the result is ordered by `(timestamp, seq)` (seq = resident order), reference documents trailing. | `cms.py:62-82, 1185-1196, 1315-1370, 1719-1726` |
| S12 | Contiguity, when `memory.search.contiguity_neighbors` n > 0 (default 0): around each direct hit in order, up to n stream neighbours per side (same `episode_id` when the hit has one, else the same `source` among episode-less entries; superseded digests and, under `hide_superseded`, superseded entries excluded; texts already served skipped), ordered by `(timestamp, seq)`, served with score 0.0 and `via: "contiguity"`. `low_confidence` judges only the direct hits. | `service.py:1980-2004`; `cms.py:620-664` |
| S13 | Cross-encoder rerank, when `rerank` (route tribool) or `memory.reranker.enabled` (default false) is on: only when the combined pool (memories + reference) is non-empty and fits `reranker.top_n` (20); a margin gate (`skip_margin`, default 0 = off) on the sorted original scores may skip it. Scores are `sigmoid(logit)` of `cross-encoder/ms-marco-MiniLM-L-6-v2` per (query, text), fused `w x ce + (1 - w) x original` with `fusion_weight` 0.7, then a stable sort. Over budget: `skip_reason: "candidate_budget_exceeded"`, order unchanged. The rerank log (`enabled, fired, skip_reason, top_n, candidate_count, scored_candidates, scoring_policy`, and when the pool qualified `skip_margin, fusion_weight, model`, `margin`) is `params.reranker`. Model: an ONNX export of the same checkpoint (rows R1-Rn, `read/rerank.rs`). | `cms.py:1466-1600`; `memory/reranker.py` |
| S14 | Reference pool (Chroma documents, `ref_top_k` 3): see the divergence list. | `cms.py:1439-1465` |
| S15 | Query-embedding LRU cache: keyed on the prefixed query text, `embedding.cache_size` entries (default 1024), least recently used evicted. Observable only as latency (free). | `memory/embedding.py:406-484` |
| S16 | Cortex facts block, when `memory.cortex.enabled` and `search_first` (both default true): `cortex_search(q, top_k=5, min_score=memory.cortex.guard_min_score (0.2))`; non-empty results are served as `cortex` and attached to the event (S8d). Contract rows C1-Cn below. | `web/routes.py:371-378`; `service.py:4499-4610` |
| S17 | Warmup: once init succeeds, the daemon runs `search("warmup probe", top_k=1, count_access=False)`: one retrieval event, no access accrual, no cortex block. | `service.py:6219-6224` |
| S18 | Pool shape: `candidate_pool_multiplier` m >= 1 widens each band's dense top-k to `k x m`; `fusion: rrf` replaces every pre-fusion score with the sum of `1/(60 + rank)` over the dense (by relevance), slot, BM25 and timeline rank lists, times the source and supersession multipliers; with m > 1 and rerank on, the cut to `k` happens after the rerank, reference positions reserved. Recency (multi-band presets with `recency_boost_enabled`, unless `disable_recency_boost`): band depth d of n gets boost `0.4 x (1 - d/(n-1))` and half-life `recency_base_half_life_s x 2^d`; relevance = `cos x (1 + boost x 2^(-age/half_life))`. | `cms.py:858-905, 940-1000, 1059-1072, 1372-1430, 2878-2881` |
| S19 | `low_confidence`: true when the direct result is empty or, with `memory.search_confidence_floor` > 0, its best score is below the floor. | `memory/abstain.py`; `service.py:2058-2063` |
