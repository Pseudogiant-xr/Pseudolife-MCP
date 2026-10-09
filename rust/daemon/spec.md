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
| S7 | Reads are from schema 55 `entries` as stored (`ORDER BY id`); no DDL, no writes. A row whose band is not in the preset is served under the first band's name. | `storage/postgres.py:104-114, 1424` |

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

## MCP surface (W2-G)

Added 2026-10-09 for slice W2-G. Python references are to
`pseudolife_memory/` at master `28c1f23c`; SDK references are to the pinned
`mcp` 2.1.1 (`mcp/server/streamable_http.py`, `streamable_http_manager.py`,
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
| M1 | `/mcp` and `/mcp/*` are served after the bearer gate (503/401 as for the catch-all). `/mcp/` answers 307 with `Location: http://<Host>/mcp[?query]`, no body; any other `/mcp/...` path answers 404 `text/plain; charset=utf-8` `Not Found`. | `web/api.py:858-886`; Starlette `redirect_slashes` |
| M2 | An `MCP-Protocol-Version` header outside `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25` routes to the 2026-07-28 era (declared divergence: only its no-envelope refusal is served). | `streamable_http_manager.py:_handle_request` |
| M3 | Session lookup first: a known id goes to its transport; an id the process terminated answers 404 `Not Found: Session has been terminated` (with the id header) after the security check; an unknown id answers 404 `{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Session not found"}}`, no id header. No id: a new transport with a fresh `uuid4().hex` id, reported in `mcp-session-id` even on refusals. | `streamable_http_manager.py:_handle_stateful_request` |
| M4 | Security, per request: POST needs a Content-Type whose lowercase starts with `application/json`, else 400 plain `Invalid Content-Type header` (no content type). With no token configured, Host must start with `127.0.0.1:`, `localhost:` or `[::1]:` (421 `Invalid Host header`), and a non-empty Origin must start with `http://127.0.0.1:`, `http://localhost:` or `http://[::1]:` (403 `Invalid Origin header`). With a token, no Host or Origin check. | `mcp_server.py:125-160`; `transport_security.py` |
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
| M17 | The writer is the named principal, else `X-PL-Writer` (non-empty), else `PSEUDOLIFE_WRITER_ID`, else `unknown`; the session is `X-PL-Session` as sent. Tool bodies receive both through `CallIdentity` (`src/mcp/dispatch.rs`). | `writer_context.py:130-205`; `service.py:893` |

### Free (MCP surface)

- The text after `Parse error: ` and `Validation error: `.
- SSE keep-alive comments (`: ping - <time>` every 15 s) and their timestamps.
- The value of `mcp-session-id` (a fresh uuid4 hex); only its presence is compared.
- Header order, `date`, `server`, `content-length` and `transfer-encoding`.
- Key order and float text inside a result's `content[0].text`, which repeats `structuredContent`.
