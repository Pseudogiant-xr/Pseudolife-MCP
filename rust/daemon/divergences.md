# Declared divergences: Rust daemon vs the Python daemon

What the Rust daemon deliberately does not match yet, and why. The
cutover gate (contract-first plan, method item 5) is this list reaching
zero, or the maintainer accepting what remains. Each row names the slice
expected to close it. The harness checks every "answers 501" row: the Rust
side must answer exactly `501 {"error": "not_implemented", "path": P}`.

## Routes whose handlers belong to later slices (answer 501)

| Surface | Python behaviour | Owner |
|---|---|---|
| Every Console route in `harness/goldens/routes.json` other than `GET /api/search`, after all gates | the handler's answer | W2-D (reads), W2-E (writes), W2-F (board, maintainer) |
| `POST /api/coordination/*` after the body checks | the board hub | W2-F |
| `/api/hook/session-start`, `memory-policy`, `coordination-start` (any caller) | briefing, policy and board check-in text | W2-D / W2-F |
| Authorized `memory-changes`, `park-gate`, `woke`, `subagent`, and `session-end` after its gates | change note, park gate, woke and subagent writes, episode close | W2-E / W2-F |
| `POST /api/pair` after a valid body on an authenticated install | redemption (principal store write) | W2-F |

## MCP surface (W2-G)

`/mcp` is served (spec.md, "MCP surface (W2-G)"); `tools/list` is byte-equal
at every tier. A `tools/call` of a tool whose body has not landed answers the
declared stub (delegate ruling 2026-10-09): the normal refusal envelope,
`isError: true`, structured content equal to the JSON text,
`{"error": "not_implemented", "message": "not_implemented: <tool> is not yet served by the Rust daemon"}`,
with no `mutation` key because nothing ran. Nothing installs the Rust daemon
before cutover, so no client sees it. The harness (`harness/mcp_compare.py`,
`UNSERVED`) checks the stub for every row here; the PR that wires a body
deletes its row in both places, and from then on the harness compares that
tool's answers by value.

| Tool | Body owner |
|---|---|
| `memory_agents` | W2-F |
| `memory_message` | W2-F |
| `memory_search` | W2-D |
| `memory_recall` | W2-D |
| `memory_world_search` | W2-D |
| `memory_lesson_search` | W2-D |
| `memory_get` | W2-D |
| `memory_recent` | W2-D |
| `memory_history` | W2-D |
| `memory_stats` | W2-D |
| `memory_graph` | W2-D |
| `memory_fact_get` | W2-D |
| `document_search` | W2-D |
| `memory_episode_summary` | W2-D |
| `memory_consolidation_candidates` | W2-D |
| `memory_store` | W2-E |
| `memory_fact_set` | W2-E |
| `memory_set_add` | W2-E |
| `memory_set_remove` | W2-E |
| `memory_fact_resolve` | W2-E |
| `memory_world_set` | W2-E |
| `memory_outcome` | W2-E |
| `memory_episode_start` | W2-E |
| `memory_episode_end` | W2-E |
| `memory_session_title` | W2-E |
| `memory_supersede` | W2-E |
| `memory_reinstate` | W2-E |
| `memory_reinforce` | W2-E |
| `memory_forget` | W2-E |
| `document_ingest` | W2-E |
| `memory_dream` | W3-H |
| `memory_graph_review` | W3-H |
| `memory_consolidate` | W3-H |
| `memory_graph_relate` | W3-H |
| `memory_graph_unrelate` | W3-H |
| `memory_alias` | W3-H |
| `memory_relation_define` | W3-H |

| Item | Python | Rust | Owner |
|---|---|---|---|
| Argument binding for tools other than `memory_toolset` (`unknown_parameter`, pydantic type, enum, length and range refusals, the string-to-list pre-parse, `_non_blank`, `_check_as_of`) | refused before the body runs | not yet: every unserved tool answers the stub whatever its arguments | W2-G (next PR) |
| The 2026-07-28 stateless era (an `MCP-Protocol-Version` header outside the handshake versions, `_meta` envelopes, `subscriptions/listen`) | served | only the no-envelope refusal (400, -32602) | W2-G (follow-up); the shims and doctor use the handshake era, but README documents direct `--transport http` clients |
| `X-PL-Bank` / `X-PL-Principal` binding on `/mcp` (`bound_identity`, `enforce_bound_identity` against the board context) | checked before forwarding; a mismatch is a coordination error | forwarded unchecked; no served tool reads the binding yet | W2-F supplies the check, W2-G calls it |
| A bearer revoked between the gate and dispatch | JSON-RPC -32004 `unauthorized` (or -32003 `principals_unavailable`) | never: the principal is resolved once, at the gate | free (no race window) |
| Terminated session ids | kept for the process lifetime (404 "has been terminated") | the latest 10,000; older ones answer 404 "Session not found" | bounded memory |
| A refused non-`initialize` request with no session id | registers a live, uninitialized session under the fresh id it reports | registers nothing; that id is later unknown | non-canonical (no client reuses a refusal's id) |
| Request body size on `/mcp` | unbounded | 64 MiB | bounded memory |
| `logging/setLevel`, `completion/complete` | -32602 for these params (capabilities do not advertise them) | -32601 `Method not found` | non-canonical |
| `Last-Event-ID` on GET (replay) | the SDK's replay path with no event store | ignored | non-canonical |
| JSON-RPC message validation: batches, non-object messages, ids that are not integers or strings | pydantic union validation | 400 -32602 with a fixed message (the text after `Validation error: ` is free) | non-canonical |
| Duplicate `X-PL-Writer` / `X-PL-Session` headers | the SDK request context's header mapping | the first value | non-canonical |

## Startup and configuration

| Item | Python | Rust | Why |
|---|---|---|---|
| Storage modes | file mode, or embedded lite Postgres when no DSN | refuses to start (exit 2) without `PSEUDOLIFE_MCP_DATABASE_URL` | the port targets the Postgres tier; lite tier is cutover work (W3-J) |
| TLS and libpq conventions | libpq: `sslmode` (default `prefer`), every DSN keyword (`sslrootcert`, `gssencmode`, `client_encoding`, ...), `PGPASSWORD`, `~/.pgpass`, `PGHOST`, `PGSSLMODE` | tokio-postgres: plaintext only, its own keyword set (an unknown keyword is a connection failure: nothing recorded, `/health` keeps no `db` key, stored principals never load) | producer DSNs are plain `postgresql://user:pass@host:port/db`; the shim's rustls/libpq-compatible client is the fix when one is not |
| YAML dialect | PyYAML (YAML 1.1: `yes/no/on/off`, `010` octal, `1_000`, `0x10`, `Null`/`NULL` as null, `1e-1` as a string, unhashable keys refused, ints past 2^63 accepted) | yaml-rust2 (YAML 1.2 core) loaded PyYAML-style (last duplicate wins, merge keys) | canonical config files use `true/false`, plain decimals and string keys |
| Wrong-typed values for keys Python does not validate (`memory.top_k: abc`), and sections that are null or not mappings (`updates:` with every child commented out) | starts, fails later at use, or omits that `/health` key | refuses to start (exit 1) naming the key | fail early instead of serving a half-broken daemon |
| A token variable that is not valid Unicode (Linux) | starts with auth on; every bearer answers 500 | refuses to start (exit 2) | fail closed: read as unset it would open the bank |
| `PSEUDOLIFE_MCP_HOST=""` | asyncio binds every IPv4 and IPv6 interface | binds `0.0.0.0` only | dual-stack wildcard is packaging work (W3-J) |
| `PSEUDOLIFE_MCP_PORT` with Unicode digits, or `PSEUDOLIFE_MCP_*_SECONDS` with Python-only float spellings | `int()`/`float()` accept them | refuses to start (exit 1) | non-canonical input |
| `PSEUDOLIFE_MALLOC_TRIM_SECONDS` | parsed at start on Linux/glibc (exit 1 when not a number) | not read | heap trimming is a W3-I duty |
| Embedding model | `embedding.model_name` from the Hugging Face cache, torch or ONNX | the ONNX export in `PSEUDOLIFE_DAEMON_ONNX_DIR`; ORT library from `ORT_DYLIB_PATH` | model resolution and packaging are cutover work (W3-J) |
| Plugin dir for `hooks_digest` | `PSEUDOLIFE_PLUGIN_DIR`, else `plugin/` beside the package | `PSEUDOLIFE_PLUGIN_DIR` only | the image sets the env var; a checkout run sets it too |
| Console static dir | the package's `web/static` | `PSEUDOLIFE_DAEMON_STATIC_DIR` | packaging (W3-J) |
| Refusal and log wording | as written | same meaning, free wording | not read by any consumer |

## /health

| Key | Python | Rust | Owner |
|---|---|---|---|
| `version` | the package version | the crate version | free until cutover (W3-J) |
| `updates.check_releases`, `latest_release`, `checked_at` | live PyPI check when enabled | always off (`false`, `null`, `0.0`) | W3-I (release check) |
| `stall`, `migration_partial`, `dream_tracking_error`, `capacity_warning`, `lesson_reconciliation_required`, `persist_errors > 0` | subsystem state | never emitted | W2-E, W3-H, W3-I |
| `embedder.backend`/`dtype` | `torch` with a dtype, or `onnx` with null | `onnx`, null | backend choice, not a contract |
| `memory` byte counts | live | live (same reader); compared by `source` only | values are process-specific |

## Lifecycle and storage

| Item | Python | Rust | Owner |
|---|---|---|---|
| Hydration | CMS with capacity rebalancing, cortex, world, lessons, dream tracking (writes `meta.dream_ack_secret_v1`), legacy import, HLC reseed, the warmup search | entries and band-stamp write-back only | W2-D, W2-E, W3-H |
| Reconnect after a lost session | heals on next use, rechecks the lease epoch | none: a lost writer session is not replaced | W2-E (write path) |
| Writer-session probe per call (`verify_writer_session`) | probes at most once a second | none | W2-E |
| Session reaper | closes idle session episodes every `PSEUDOLIFE_SESSION_REAP_SECONDS`, after `_ensure_init` | only the `_ensure_init` retry runs on that cadence | W2-E (episodes) |
| Search knobs `memory.search.{fusion: rrf, candidate_pool_multiplier > 1, contiguity_neighbors, timeline_channel}`, `memory.reranker.enabled`, `?rerank=`, the recency boost on multi-band presets | change `/api/search` | parsed and validated, ignored by search | W2-D (search) |
| `application_name` fallback | derived from argv | `pseudolife-mcp pid=<pid> pseudolife-daemon serve` | free (not persisted) |

## Request parsing edges (canonical shapes rule)

| Item | Python | Rust |
|---|---|---|
| JSON bodies with `NaN`/`Infinity`, lone surrogate escapes, or nesting past serde's 128 levels | accepted (or a 500 past ~1000 levels) | 400 `invalid_json` |
| Static types for `.md`, `.csv` and other extensions outside the Console build | platform `mimetypes` tables | `application/octet-stream` |
| Unicode decimal digits in `top_k` / `min_score` (`top_k=٣`) | `int()` / `float()` accept them (3) | not a number: the route default (delegate ruling 2026-10-09: non-canonical input) |
