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
