# Contract spec: daemon read-path slice (spike)

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
| H5 | `embedder` `{backend, device, dtype}` is present once the embedder is built. | `daemon.py` health payload |

Conditional keys owned by subsystems outside this slice (`coordination`,
`updates`, `extractor`, `stall`, `hooks_digest`, `build`, `init_refusal`,
`not_ready`, `migration_partial`, `last_backup` and so on) may be omitted,
because Python omits each one when its subsystem is absent. Each omission is
listed as a divergence.

## Bearer gate (`/api/*`)

| # | Exact item | Source |
|---|---|---|
| B1 | Env `PSEUDOLIFE_MCP_TOKEN` maps to principal `default`. `PSEUDOLIFE_MCP_TOKENS` is `token:principal,...`, split on the last `:`, principal lowercased; `default`, `maintainer` and duplicate principals are skipped. Auth is configured iff either yields a token. | `principals.py:103 parse_token_map`, `daemon.py:487-550` |
| B2 | Open mode (auth not configured): every request resolves to `default`, except the browser gate. A non-loopback `Origin` gives 403 `{"error":"forbidden_origin","hint":H}`; a non-loopback `Host` gives 403 `{"error":"forbidden_host","hint":H}`. Loopback is `127.0.0.1`, `::1`, `localhost`; the host part strips scheme, port and IPv6 brackets. | `web/api.py:292-320, 707-713` |
| B3 | Header lookup is case-insensitive and decoded as latin-1; `partition(" ")`; the scheme is compared case-insensitively to `bearer`; the token is the rest, stripped of spaces and tabs; an empty token means no principal. | `principals.py:245-306` |
| B4 | Candidate bytes are UTF-8, plus latin-1 when encodable. Constant-time compare against the env map tokens, then the singular token. | `principals.py:245-306` |
| B5 | Stored principals: `SELECT principal, token_hash, tier, board, revoked_at IS NOT NULL FROM public.principals`, refreshed every 10 s. A row authenticates iff `token_hash` is non-empty, it is not revoked, the name matches `[a-z0-9][a-z0-9._-]{0,63}`, is not reserved (`default`, `daemon`, `maintainer`) and is not shadowed by an env principal. The match is lowercase hex `sha256(candidate)`. | `principal_store.py:156, 236`; `principals.py:92` |
| B6 | Store not loaded, or the last load is older than 60 s: 503 `{"error":"principals_unavailable"}`. | `principal_store.py:46`, `web/api.py:283` |
| B7 | No principal: 401 `{"error":"unauthorized","hint":"Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"}`, identical for missing, malformed, unknown and revoked. | `web/api.py:719-723` |
| B8 | After auth: methods other than GET and POST give 405 `{"error":"method_not_allowed"}`; an unknown path gives 404 `{"error":"not_found","path":P}`; a known path with the wrong verb gives 405 `{"error":"method_not_allowed","path":P}`. | `web/api.py:724, 838-841` |
| B9 | Gate order: browser gate, then principals-unavailable, then 401, then method, then route. | `web/api.py:705-731` |

## GET /api/search

| # | Exact item | Source |
|---|---|---|
| S1 | `q` (blank means `""`); `top_k` int, blank or non-numeric gives 12, `0` gives the config default 8; `source`, `band`, `tag` are comma lists, stripped, empties dropped; `min_score` float or null; `disable_recency_boost` true only for `1/true/yes/on`; `bm25`, `rerank` tribool (`""`, `null`, `auto` follow config). | `web/routes.py:21-74, 355-364`; `cms.py:~800` |
| S2 | Query blank after strip: 200 `{"entries":[],"query":"","count":0,"low_confidence":true}`. | `service.py:1963` |
| S3 | Unknown band name: 400 `{"error":"unknown band name(s) [...] — this preset has [...]"}` (wording free; status and key exact). | `service.py:2095-2104` |
| S4 | Response: `query` (stripped), `count` int, `low_confidence` bool (true iff no hits, at the default floor 0.0), `entries` list. `cortex` only when facts match; `events` only for temporal cues with chronicle hits. | `service.py:2053-2068`; `web/routes.py:371-379` |
| S5 | Entry: `id` int, `text`, `source`, `bank` str, `timestamp` float, `access_count` int, `surprise_score` float (4 dp), `superseded` bool, `superseded_at` float or null, `superseded_by_text` str or null, `episode_id` and `episode_title` str or null, `tags` list, `score` float (4 dp). `authority` and `distortion_tolerance` only when set; `slots` only when non-empty; superseded entries add `superseded_by_id`, `supersession_verified`, `superseded_by_current`. | `service.py:147-190, 213` |
| S6 | Ranking: query = config prefix + `q`, embedded and L2-normalized; stored rows re-normalized; cosine over eligible entries (source and tag filters apply before top-k); top `k` by cosine; drop exact-duplicate texts (first wins); keep iff relevance >= floor (0.25, or an explicit `min_score`); relevance = cosine (the recency boost is 0 on the flat preset and off by default); sort key = relevance x 0.85 for source `assistant` x 0.55 if superseded; stable sort, cut to `k`. Superseded digest entries never surface. | `cms.py:740-1100, 1426`; `band.py:157-193` |
| S7 | Reads are from schema 55 `entries` as stored (`ORDER BY id`); no DDL, no writes. | `storage/postgres.py:104-114, 1424` |

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
- `access_count` values: Python increments them on every served hit, while the spike is read-only.
