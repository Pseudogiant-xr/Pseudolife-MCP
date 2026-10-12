# Contract spec: daemon foundation (slice W1-A)

What the Rust daemon reproduces exactly from the Python daemon for its
configuration, its HTTP route table and gate order, `GET /health`, the lazy
initialization lifecycle, the `PostgresStorage` constructor's database writes
and the stored-principal read. References are to `pseudolife_memory/` at
master `35b8f5d2`. This page supersedes the spike spec (`spec.md`) for health
(H1-H5) and the bearer gate (B1-B9); `spec.md` still governs search.

"Free" items are not compared byte for byte: listed per section and in the
last section. Every declared divergence is in `divergences.md`.

## C. Configuration

| # | Exact item | Source |
|---|---|---|
| C1 | Data dir: `PSEUDOLIFE_MCP_DATA_DIR` if non-empty, else `<cwd>/data`; created at start. Config file: `PSEUDOLIFE_MCP_CONFIG` if set (even empty; Python then fails on `Path("")`), else `<data_dir>/config.yaml`. | `mcp_server.py:109-111`, `service.py:844-853` |
| C2 | Missing file, empty file, comment-only file, or a top-level list or string: all defaults. Unknown keys and sections at any level are ignored. Duplicate keys: last wins. A YAML syntax error, `memory:` or `miras:` that is null or not a mapping, or a top-level scalar number: refuse to start (exit 1). | `utils/config.py:1665-1782` |
| C3 | Keys read (others are ignored until a slice needs them): `memory.{top_k, hide_superseded, search_confidence_floor, recency_boost_enabled, recency_base_half_life_s}`, `memory.miras.{preset, bands}`, `memory.search.{min_score, fusion, candidate_pool_multiplier, contiguity_neighbors, timeline_channel}`, `memory.bm25.{enabled, k1, b, weight, top_n, min_score}`, `memory.reranker.enabled`, `memory.dream.{enabled, extractor_source, primary/fallback url and model keys}`, `embedding.{model_name, device, backend, query_prefix, max_seq_length}`, `coordination.{enabled, wake.*}`, `updates.{check_releases, unattended_clients, unattended_daemon, check_interval_seconds}`. Defaults are the dataclass defaults. | `utils/config.py` (per field) |
| C4 | MCP overlay, applied only where the YAML leaf is absent: `memory.recency_base_half_life_s` 86400.0. (The overlay's other keys are not read by this slice.) | `service.py:1071-1140, 578-600` |
| C5 | Refuse to start (exit 1) on: unknown `miras.preset` or `custom` without bands; `search.fusion` not `weighted_sum`/`rrf`; `search.min_score` not a non-bool number in [0, 1]; a `wake` cap that is not an exact int >= 0, or `active_seconds` < 1; `coordination.enabled` not a bool; an `updates` flag not a bool, or `check_interval_seconds` not an int >= 60. A retired `wake.nudge_interval_seconds` is dropped. | `utils/config.py:116-127, 1142-1154, 1426-1433, 1552-1592, 1628-1633` |
| C6 | Presets: `flat` is one band `flat`; `continuum` and its aliases `titans`, `moneta`, `yaad`, `memora` are the eight bands `working, micro, instant, fast, medium, slow, archival, forever`; `custom` uses `bands[].name`. | `memory/miras/presets.py` |
| C7 | Environment: `PSEUDOLIFE_MCP_HOST` (default `127.0.0.1`), `PSEUDOLIFE_MCP_PORT` (default 8765; non-integer exits 1), `PSEUDOLIFE_MCP_TOKEN` (empty is unset), `PSEUDOLIFE_MCP_TOKENS`, `PSEUDOLIFE_MCP_TRUST_BIND` (`1/true/yes/on` after lowercasing, not stripped), `PSEUDOLIFE_MCP_DATABASE_URL`, `PSEUDOLIFE_RELEASE_CHECK` (`0` after strip disables), `PSEUDOLIFE_BUILD_*`, `PSEUDOLIFE_PLUGIN_DIR`, `PSEUDOLIFE_DREAM_{BASE_URL, MODEL, FALLBACK_BASE_URL, FALLBACK_MODEL}` (only when `dream.extractor_source` is `env`, the default). | `daemon.py:525-559`, `release_check.py:41`, `memory/dream.py:1984-2038` |
| C8 | Startup refusals, exit 2: a `move.json` or `moved.json` in the data dir (`pseudolife-mcp serve: ...` on stderr); a token map that is set and non-blank, parses to nothing, with no singular token; a non-loopback host with no token and no trust-bind. | `daemon.py:416-481, 533-584` |

Free: log lines and the wording of refusal messages; YAML 1.1-only scalar
forms (see divergences).

## R. Route table and gate order

Paths are percent-decoded before routing (uvicorn); an empty path is `/`.
Every JSON answer is `application/json; charset=utf-8` with
`cache-control: no-store` and `x-content-type-options: nosniff`.

| # | Exact item | Source |
|---|---|---|
| R1 | `/health`, any method: section H. Never gated. | `web/api.py:418-426` |
| R2 | `/`: 307, `location: /ui/`, the four Console security headers (CSP, `x-frame-options: DENY`, `referrer-policy: no-referrer`, nosniff), empty body. | `web/api.py:60-71, 429-433` |
| R3 | `/ui` and `/ui/*`: static files from the Console build, open. Traversal outside the root: 403 `forbidden` text/plain. A directory serves its `index.html`. A missing file serves `index.html` (SPA fallback), or 404 `not found` without one. Types: `text/*`, JS, SVG and JSON get `; charset=utf-8`; fonts and images `cache-control: max-age=86400`, everything else `no-store`; the security headers on every answer. | `web/api.py:204-228, 436-450` |
| R4 | Hook routes, each before the bearer gate, in this order: browser gate (403 `{"error": denied}`, no hint), then the method (405 `{"error":"method_not_allowed"}`): GET for `session-start`, `memory-policy`, `memory-changes`, `coordination-start`, `park-gate`; POST for `session-end`, `woke`, `subagent`. Unauthorized `memory-changes`, `park-gate`, `woke`, `subagent`: 200, empty `text/plain; charset=utf-8`, `no-store`. `session-end`: principals-unavailable 503, then 401 with the `/api` hint, then a body over 16 KiB 413 `request_too_large`. | `web/api.py:464-697` |
| R5 | `/api/pair`, before the gate: non-POST 405; any `Origin` 403 `forbidden_origin`; media type (before `;`, stripped, lowercased) not `application/json` 415; then one failure is reserved from a budget of 20 per 60 s (spent: 429 `rate_limited`); a body over 1 KiB 413; a body that is not a UTF-8 JSON object with exactly `code` and `token_sha256`, a code that does not normalize, or a hash that is not 64 lowercase hex: 400 `pairing_refused`; tokenless, no stored-principal store, or no DSN: 400 `pairing_refused`. The budget is refunded only on success or a full gate. | `web/api.py:322-402`, `principal_store.py:52, 344-405`, `principals.py:75-101` |
| R6 | `/api` and `/api/*`: browser gate (403 with the hint), principals unavailable (503), no principal (401 with the hint), method not GET/POST (405 without `path`), stored principal POSTing `/api/config` or `/api/daemon-notice` (403 `operator_principal_required`), `/api/coordination/*` not POST (405 without `path`). | `web/api.py:707-742` |
| R7 | POST bodies, after R6: over the limit 413 `request_too_large` (32 KiB under `/api/coordination/`, 4 MiB for `/api/facts/set`, `/api/consolidate`, `/api/supersede`, else 256 KiB). A non-empty body: media type not `application/json` 415 `content_type_must_be_application_json`; invalid JSON 400 `invalid_json`; not an object 400 `body_must_be_object`. Invalid UTF-8 on a non-coordination path escapes the handler: 500 `Internal Server Error` text/plain (uvicorn). | `web/api.py:91-94, 743-772` |
| R8 | Dispatch: tokenless requests to `/api/maintainer[/...]` or `GET /api/agents?view=coordination` are refused 401 `{"error":"authentication_required"}` before the route lookup. Then the 72-route table (`ConsoleRoutes`, golden `harness/goldens/routes.json`): an unknown path 404 `{"error":"not_found","path":P}`; a known path with the other verb 405 `{"error":"method_not_allowed","path":P}`, except a known maintainer path, which answers 400 `{"error":"invalid_request"}`. | `web/api.py:790-828`, `web/routes.py:87-95`, `coordination.py:407-426` |
| R9 | Everything else: principals unavailable 503, no principal 401 (hint), then the MCP app, whose only route is `/mcp`; any other path is 404 `Not Found` `text/plain; charset=utf-8`. | `web/api.py:847-886` |
| R10 | Header reads are latin-1. Origin and Host take the first value; Authorization the last. The browser gate applies only when tokenless: a non-loopback Origin host is `forbidden_origin`, then a non-loopback Host is `forbidden_host`. | `web/api.py:256-320` |

Free: `server` and `date` headers; the `error` text of 400 and 500 answers
other than the codes listed.

## H. GET /health

| # | Exact item | Source |
|---|---|---|
| H1 | Keys always present: `status`, `version`, `schema` (55), `storage` (`"postgres"` when a DSN is configured), `auth`, `bank`, `persist_errors`, `coordination` (`enabled`, `wake` with the six caps), `updates` (`check_releases`, `latest_release`, `checked_at`, `unattended_clients`, `unattended_daemon`), `memory`. | `daemon.py:231-409` |
| H2 | `build` only when `PSEUDOLIFE_BUILD_GIT_SHA` is non-empty: `git_sha`, `dirty` (true/false for `true`/`false` after strip+lower, else null), `built_at` (default `unknown`), `source` (empty means `unknown`). | `daemon.py:75-96` |
| H3 | `extractor`: `disabled` when `dream.enabled` is false; else `none` unless a primary or fallback (url and model) resolves; else `configured` (no stall tracker in this slice). | `daemon.py:99-142` |
| H4 | `hooks_digest` when the plugin dir (`PSEUDOLIFE_PLUGIN_DIR`) holds `hooks/lifecycle.ps1` and all nine scripts: SHA-256 over `name NUL body(CRLF->LF) NUL` in the fixed order. | `plugin_hooks.py:30-65` |
| H5 | `init_refusal` and `not_ready` (section L) each set `status` to `degraded`. | `daemon.py:306-318` |
| H6 | `memory`: `{"source":"unavailable"}` where `/proc` and cgroup v2 are absent; on Linux the cgroup or process shape from `utils/memory_headroom.py`. | `utils/memory_headroom.py` |
| H7 | `embedder` `{backend, device, dtype}` once the embedder is built (after storage opened; it survives a later hydration failure). | `daemon.py:382-384` |
| H8 | `db` only once storage exists: a fresh connection (2 s connect timeout) runs `SELECT 1`; while a reconnect was refused for the lease and a holder still exists, it fails. Success: `"ok"`, then `bank`. Failure: `"error: <text>"`, `status` `degraded`, `bank` null. | `daemon.py:391-401`, `storage/postgres.py:702-719` |
| H9 | `bank`: after a successful ping, the cached coordination bank id, read on a fresh connection (`statement_timeout 2s`, `SELECT value FROM public.meta WHERE key = 'coordination_bank_id'`) at most once per 60 s until found, then never again; a non-empty string id gives the first 16 hex of its SHA-256, else null. | `daemon.py:193-211`, `storage/postgres.py:721-752` |
| H10 | `last_backup` when `<data_dir>/last-backup.json` (utf-8-sig) has `created_at` in `%Y-%m-%dT%H:%M:%SZ`: `{at, age_hours (1 dp), rotation (default "unknown")}`. | `daemon.py:45-72` |
| H11 | 200 when `status` is `ok`, else 503 with the same body; 500 `{"status":"error","error":E}` if the build raises. | `web/api.py:418-426` |

Free: `version`; `updates.checked_at` and `latest_release` (a network
read); the `db` error text; the `memory` byte counts; key order.

## L. Lazy initialization lifecycle

| # | Exact item | Source |
|---|---|---|
| L1 | Nothing touches the bank before the HTTP server answers. A warmup task starts `ensure_init` at startup in the background; `/health` never waits for it. | `mcp_server.py:3188-3219`, `daemon.py:589` |
| L2 | `ensure_init` order: storage constructor (section P), then the `search_path` check, then the embedder, then hydration. Storage opens before any model loads; the embedder is kept across retries. | `service.py:1223-1350` |
| L3 | A connection failure records nothing: no `not_ready`, no `init_refusal`, no backoff; `/health` stays 200 with no `db` key. | `service.py:1234-1249, 1422-1427` |
| L4 | Writer lease held by another session: `not_ready` = the lease refusal, backoff armed (5, 10, 20, 40, then 60 s). A `RuntimeError` from the constructor (the embedding-dimension refusal): `init_refusal`, no backoff. A hydration failure: stores dropped, `not_ready` = the reason, backoff armed. Success clears both and the backoff. | `service.py:1236-1251, 1344-1359, 1435-1467` |
| L5 | While backing off, a call that needs init fails `memory bank not ready: <reason> (next retry in <n>s)`; a Console route answers 500 `{"error": that text}`. | `service.py:1422-1433`, `web/api.py:836-844` |
| L6 | Warmup retries only while `not_ready` is set and the backoff is under its 60 s cap; otherwise it stops and the next request retries. | `service.py:6192-6224` |
| L7 | Concurrent requests during init wait for it (one init at a time); `/health` does not. | `service.py:805, 6206-6208` |

## P. PostgresStorage constructor (database writes)

| # | Exact item | Source |
|---|---|---|
| P1 | Session: autocommit, `connect_timeout` 10, `SET lock_timeout = '5s'`, `SET search_path TO public`. | `storage/postgres.py:603-624` |
| P2 | Writer lease on that session: `SET tcp_keepalives_idle = 60`, `_interval = 10`, `_count = 3`; `SHOW lock_timeout`; `set_config('lock_timeout', '2s', false)`; `pg_advisory_lock(hashtextextended('pseudolife-bank-writer', 0))`; restore the previous `lock_timeout`. A lock timeout is the `WriterLeaseHeld` refusal (text free, but it names the bank and the holder's pid). | `storage/postgres.py:447-479, 584-601` |
| P3 | `ensure_schema`, one transaction: the embedding-dimension probe first (a non-1024 `entries.embedding` refuses before any DDL); `SET LOCAL lock_timeout = '5s'; SET LOCAL statement_timeout = '30s';`; `CREATE EXTENSION IF NOT EXISTS vector`; `to_regclass('public.memory_trace_invalidations')`; `SCHEMA_SQL` as one batch (byte-identical copy `src/storage/schema.sql`, checked by `harness/gen_schema_sql.py --check`); the trace-invalidation backfill when that table was absent; then the additive tail statement for statement, including the healing updates and the `schema_version` upsert. | `storage/schema.py:1145-1664` |
| P4 | Seed the 11 builtin relations in one transaction, `ON CONFLICT (name) DO NOTHING`, `created_at` = wall clock. | `storage/postgres.py:191-211, 754-766` |
| P5 | Bump `meta.writer_lease_epoch` with the COALESCE upsert (non-numbers restart at 1). | `storage/postgres.py:482-498` |
| P6 | No newer-schema guard: a bank stamped above 55 is rewritten to 55 (matched, not fixed). | `storage/schema.py:1657-1662` |
| P7 | After the constructor, `SHOW search_path` must contain `public` and not list `$user` before it; otherwise the session is closed and the call fails, recording nothing (amended: the first draft said `init_refusal`; `_ensure_postgres_storage` catches only the lease and constructor refusals). | `service.py:1250-1265, 1047-1065` |

Proven by database-state equality (`harness/dbstate.py`): catalog and every
row, with `relations.created_at` normalized (wall clock).

## Q. Stored principals (read)

| # | Exact item | Source |
|---|---|---|
| Q1 | Started at daemon start when a DSN is configured; refreshes immediately, then every 10 s, on its own autocommit connection (`connect_timeout` 5, TCP keepalives) with `SET statement_timeout = '5s'`. | `daemon.py:163-178`, `principal_store.py:45-63, 221-325` |
| Q2 | `SELECT principal, token_hash, tier, board, revoked_at IS NOT NULL FROM public.principals`; a missing table reads as empty. A failed refresh keeps the previous view. | `principal_store.py:226-248, 283-295` |
| Q3 | Rows: name stripped and lowercased, must match `[a-z0-9][a-z0-9._-]{0,63}`; names in the env map or `default`, `daemon`, `maintainer` are dropped; a tier outside `minimal`/`core`/`full` becomes null; rows collapsing to one name keep the later. Authentication uses rows with a token hash that are not revoked. | `principal_store.py:141-216`, `principals.py:54-72` |
| Q4 | Available only when the last successful refresh started within 60 s. An unknown bearer while unavailable: 503 `principals_unavailable`; env tokens still authenticate. | `principal_store.py:46, 162-165`, `principals.py:245-306` |

The bearer resolution itself (env map, singular token, latin-1 and UTF-8
candidates, constant-time compare, last `Authorization` header) is the
spike's B1-B9, carried over unchanged.
