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
| Tokenless MCP Host/Origin rebinding policy and bound-identity validation | SDK transport policy and durable board binding checks before tool dispatch | **CUTOVER BLOCKER — W2-G**; tokenless MCP stays unmounted (501) until these gates are accepted; authenticated bound-identity checks remain deferred as listed below |
| Credential-bearing extractor/recall redirect refusal | `utils/no_redirect.py`, extractor and recall transports | W3-H / W2-D; outbound transports are not implemented in this slice |
| `/api/hook/session-start`, `memory-policy` (any caller) | briefing and memory-policy text | W2-D |
| Authorized `memory-changes`, `park-gate`, `woke`, `subagent`, and `session-end` after its gates | change note, park gate, woke and subagent writes, episode close | W2-E / W2-F |
| `POST /api/pair` after a valid body on an authenticated install | the 2-slot redemption gate (`RedemptionGate`, 429 `rate_limited`), invocation of the implemented `principals::redeem` store transaction plus immediate `PrincipalStore::add`, the failure-budget refund on success, and 503 `pairing_unavailable` on a store error | HTTP-SECURITY / CLI-PAIRING (the gate wraps only `principal_store.redeem`, `web/api.py:378-393`) |
| Stored callers' POST `/api/config` and `/api/daemon-notice` gate | HTTP already returns 403 `operator_principal_required` by stored-name inference; method-aware source policy is implemented for HTTP-SECURITY to consume with `resolve_detailed` | HTTP-SECURITY |

## MCP surface (W2-G)


Bearer-authenticated `/mcp` is served (spec.md, "MCP surface (W2-G)"); `tools/list` is byte-equal
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
| `PSEUDOLIFE_MALLOC_TRIM_SECONDS` | parsed before platform availability is checked; glibc trim runs only on Linux | parsed at start; glibc trim on Linux, no trim elsewhere | canonical finite seconds are implemented; Python-only numeric spellings remain deferred |
| Embedding model | `embedding.model_name` from the Hugging Face cache, torch or ONNX with fallback | existing local/cached ONNX only; complete model-root override in `PSEUDOLIFE_DAEMON_ONNX_DIR`; missing graph refuses; canonical Qwen/MiniLM pools only | verified default Qwen artifact provisioning remains a W3-J cutover blocker; other heads are deferred |
| CPU precision | `auto` uses bf16 torch on bf16-native CPUs; explicit bf16 is honored by torch | `auto` selects the graph's fp32 policy; effective explicit bf16 refuses with `deferred: bf16 ONNX` | parity evidence pins fp32; automatic and explicit bf16 ONNX remain deferred |
| Hub cache override interpolation | Python expands `~` and environment variables in cache paths | HF_HOME, HF_HUB_CACHE and XDG_CACHE_HOME must already contain expanded paths | interpolation spellings are deferred; ordinary absolute/relative paths and override precedence are supported |
| Nonpositive embedding limits / empty tokenization | backend-specific Python errors | batch_size and max_seq_length <= 0 refuse; a zero-token batch reports `empty tokenization` | unsupported input/limit shapes are explicitly refused before invalid inference |
| Plugin dir for `hooks_digest` | `PSEUDOLIFE_PLUGIN_DIR`, else `plugin/` beside the package | `PSEUDOLIFE_PLUGIN_DIR` only | the image sets the env var; a checkout run sets it too |
| Console static dir | the package's `web/static` | `PSEUDOLIFE_DAEMON_STATIC_DIR` | packaging (W3-J) |
| Refusal and log wording | as written | same meaning, free wording | not read by any consumer |

## /health

| Key | Python | Rust | Owner |
|---|---|---|---|
| `version` | the package version | the crate version | free until cutover (W3-J) |
| Release update offer in session-start briefing | newest-release offer prepended to the hook response | checker and offer formatter implemented; hook consumption awaits its handler | W2-D (hook) / W3-I (release check) |
| `stall`, `migration_partial`, `dream_tracking_error`, `capacity_warning`, `lesson_reconciliation_required`, `persist_errors > 0` | subsystem state | never emitted | W2-E, W3-H, W3-I |
| `embedder.backend`/`dtype` | `torch` with a dtype, or `onnx` with null | `onnx`, null | backend choice, not a contract |
| `memory` byte counts | live | live (same reader); compared by `source` only | values are process-specific |

## Lifecycle and storage

| Item | Python | Rust | Owner |
|---|---|---|---|
| Future or malformed schema version | `storage/schema.py:1657-1662` overwrites the stored version with the build's version | refuses future versions and non-positive/non-integer version metadata before DDL, preserving all durable state | W3 PG-HYDRATE; required by the slice brief, retained for the maintainer's cutover decision |
| Remaining startup integration | CMS and canonical stores, dream tracking (writes `meta.dream_ack_secret_v1`), legacy import, HLC reseed and warmup search | complete entry seating, episodes, canonical loader inputs and identity metadata attach to cached Ready after all required loads; late HLC failure retains those stores with a pending flag and blocks returning Ready until clock-only retry succeeds; read/write stores adopt StartupState and seed their clock before ticking | W2-D/E (store adoption, clock ticking and warmup search), W3-H (dream tracking), W3-J (legacy/file import) |
| Episode start-time ties | startup loads episodes with `ORDER BY started_at`, without a tie key | uses the same query ordering; ties among equal start times follow the server's row order in both arms | W3 PG-HYDRATE; Python ordering retained |
| Session-pointer or tombstone timestamp parse failures | fail before the embedder and resident-store abandon path; each initialization call retries parsing | fail inside required initialization and arm its retry backoff | W3 PG-HYDRATE; non-canonical metadata, declared divergence |
| Non-canonical startup metadata | Python coercions can accept arbitrary numeric spellings or non-string identities | supports shipped producer shapes; malformed durable HLC pairs refuse, and unbounded integers or Python-only identity/timestamp coercions remain deferred | W2-D/E for producer evolution |
| Reconnect after a lost session | heals on next use, rechecks the lease epoch | none: a lost writer session is not replaced | W2-E (write path) |
| Writer-session probe per call (`verify_writer_session`) | probes at most once a second | none | W2-E |
| Session reaper integration | closes idle sessions, derives generic titles and preserves resumable roots | reaping, cascade close, deferred-empty sweep and tombstone hydration implemented; generic auto-title and write/resume integration pending | W2-E (episodes) / W3-I |
| Autosave and exit persistence | changed-state save through resident writer stores | owned scheduling and exit callback implemented; callback has no mutable state to save in the current reader | W2-E (durability) |
| Canonical-store compaction | removes selected resident records, marks slots dirty and rewrites their rows | selection policy implemented and checked against Python goldens; resident-store mutation and slot sync await the writer | W2-E / W3-I |
| Automatic dream and graph sweep stages | backlog/quiescence gate, extraction, analyzer and graph review ticks | session-end trigger interface is a declared no-op; sweep currently runs maintenance only | W3-H |
| Search knobs `memory.search.{fusion: rrf, candidate_pool_multiplier > 1, contiguity_neighbors, timeline_channel}`, `memory.reranker.enabled`, `?rerank=`, the recency boost on multi-band presets | change `/api/search` | parsed and validated, ignored by search | W2-D (search) |
| `application_name` fallback | derived from argv | `pseudolife-mcp pid=<pid> pseudolife-daemon serve` | free (not persisted) |

## Request parsing edges (canonical shapes rule)

| Item | Python | Rust |
|---|---|---|
| Noncanonical static drive-relative requests and lexical escapes through linked roots | captured oracle observations | `lexical-outside-root`: 403 before filesystem access, exact refusal headers/body; shipped Console asset paths are unchanged |
| Windows nonempty dot/space-only segments except `.` and `..`, including `. ` and all-space segments | captured path-dependent refusal/fallback observations | `parent-space-refusal`: exact 403 before filesystem access; outside the shipped asset URLs |
| HTTP-BODY-LIMITS: cap enforced at frame granularity | uvicorn sends the selected early response immediately, keeps the connection alive when permitted and discards unread body data after the response | Rust waits for bounded cleanup before replying (up to 250 ms; early Expect refusals skip polling), then sends Connection: close whenever cleanup was needed, including complete drains; polling stops at the route budget or failure, with one in-flight frame and Hyper's final drop poll able to overshoot; a slow in-cap body can exceed the deliberate deadline |
| Noncanonical JSON bodies with `NaN`/`Infinity`, lone surrogate escapes, or nesting past serde's 128 levels | accepted (or a 500 past ~1000 levels); a truthy SessionEnd ID may enter its mutation handler | REST returns 400 `invalid_json`; SessionEnd parse failures become an empty object and return 200 `{ok:true}` without mutation, including nonempty inputs Python could parse into a truthy ID; outside shipped canonical SessionEnd producers |
| Static types for extensions outside the Console build | platform `mimetypes` tables | `application/octet-stream` for unknown extensions; the shipped vendor Markdown notice uses its platform type |
| Unicode decimal digits in `top_k` / `min_score` (`top_k=٣`) | `int()` / `float()` accept them (3) | not a number: the route default (delegate ruling 2026-10-09: non-canonical input) |

## Graph and review

| Item | Python | Rust | Owner |
|---|---|---|---|
| Graph identity Unicode version | runtime Unicode tables (3.11 uses Unicode 14; newer Python images may differ) | pinned Python 3.11 Unicode 14 lowercase and final-sigma context, verified exhaustively | W3-H; revisit when the oracle runtime changes |
| Colliding inverse provenance | base-set iteration can select different source relation names for the same inverse triple depending on hash seed | choose the lexicographically smallest asserted source relation; exact triple and valid provenance membership are checked, with smallest-source choice pinned | W3-H; declared deterministic choice; all other provenance exact |
| Whole graph caps, communities, graph service/MCP wiring, fact projection, proposals, review judges/audit and deep dream graph pass | served graph and review behavior | store and GraphStore read-model helpers; these services are not wired | W3-H, subsequent increments |
