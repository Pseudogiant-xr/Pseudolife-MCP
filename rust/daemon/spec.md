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
- The `version` value and numeric/boolean readings in the `memory` object.
  Memory keys and scalar kinds are checked before values are normalized:
  `unavailable` has only `source`; `process` has `source`, null `near_limit`
  and at least one integer RSS field; `cgroup` has integer current/working-set
  bytes, integer-or-null limit bytes, float-or-null fraction, boolean-or-null
  `near_limit`, and an object of integer event counters. RSS and anon/file
  byte fields and the individual event counters are conditional; their
  presence must agree between arms. A zero or null limit has null fraction
  and flag.
- `access_count` values: Python increments them on every served hit, while the spike is read-only.

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

## Free, and therefore not compared byte for byte

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
