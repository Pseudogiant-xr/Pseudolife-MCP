# Declared divergences: Rust daemon vs the Python daemon

What the Rust daemon deliberately does not match yet, and why. The
cutover gate (contract-first plan, method item 5) is this list reaching
zero, or the maintainer accepting what remains. Each row names the slice
expected to close it. The harness checks every "answers 501" row: the Rust
side must answer exactly `501 {"error": "not_implemented", "path": P}`.

## Routes whose handlers belong to later slices (answer 501)

| Surface | Python behaviour | Owner |
|---|---|---|
| Every Console route in `harness/goldens/routes.json` other than `GET /api/search`, the coordination view of `GET /api/agents` and `POST /api/daemon-notice`, after all gates | the handler's answer | W2-D (reads), W2-E (writes), W2-F (board, maintainer) |
| Tokenless MCP Host/Origin rebinding policy | SDK transport policy before tool dispatch | **CUTOVER BLOCKER — W2-G**; tokenless MCP stays unmounted (501) until this gate is accepted. Authenticated bound-identity checks are served (spec.md M18) |
| Credential-bearing extractor/recall redirect refusal | `utils/no_redirect.py`, extractor and recall transports | W3-H / W2-D; outbound transports are not implemented in this slice |
| `/api/hook/session-start`, `memory-policy` (any caller) | briefing and memory-policy text | W2-D |
| Authorized `memory-changes` and `session-end` after its gates | change note and episode close | W2-E |
| `POST /api/pair` after a valid body on an authenticated install | the 2-slot redemption gate (`RedemptionGate`, 429 `rate_limited`), invocation of the implemented `principals::redeem` store transaction plus immediate `PrincipalStore::add`, the failure-budget refund on success, and 503 `pairing_unavailable` on a store error | HTTP-SECURITY / CLI-PAIRING (the gate wraps only `principal_store.redeem`, `web/api.py:378-393`) |
| Stored callers' POST `/api/config` and `/api/daemon-notice` gate | HTTP already returns 403 `operator_principal_required` by stored-name inference; method-aware source policy is implemented for HTTP-SECURITY to consume with `resolve_detailed` | HTTP-SECURITY |

## MCP surface (W2-G)


Bearer-authenticated `/mcp` is served (spec.md, "MCP surface (W2-G)"); `tools/list` is byte-equal
at every tier. Every tool's arguments are bound first (spec.md, "Tool argument
binding"), so binding refusals match Python for every tool. A `tools/call` of a
tool whose body has not landed then answers the
declared stub (delegate ruling 2026-10-09): the normal refusal envelope,
`isError: true`, structured content equal to the JSON text,
`{"error": "not_implemented", "message": "not_implemented: <tool> is not yet served by the Rust daemon"}`,
with no `mutation` key because nothing ran. Nothing installs the Rust daemon
before cutover, so no client sees it. The harness (`harness/mcp_compare.py`,
`UNSERVED`) checks the stub for every row here; the PR that wires a body
deletes its row in both places, and from then on the harness compares that
tool's answers by value.

| Tool | Body owner | Missing service prerequisite |
|---|---|---|
| `document_search` | W2-D | No reference-pool search service. |
| `document_ingest` | W2-E | No parser/chunk/reference ingestion service. |
| `memory_dream` | W3-H | Cursor storage exists, but dream cycle orchestration does not. |
| `memory_consolidate` | W3-H | No consolidation service. |

The integration batch retains W2-G's refusal stubs for every tool except
`memory_toolset` and, since their body registrations, `memory_store`,
`memory_search`, `memory_recent`, `memory_fact_get`, `memory_get`,
`memory_fact_set`, `memory_set_add`, `memory_set_remove`,
`memory_fact_resolve`, `memory_stats`, `memory_episode_start`,
`memory_episode_end`, `memory_session_title`, `memory_episode_summary`,
`memory_outcome`, `memory_reinforce`, `memory_history`, `memory_world_set`,
`memory_world_search`, `memory_lesson_search`, `memory_supersede`,
`memory_reinstate`, `memory_forget`, `memory_consolidation_candidates`,
`memory_graph_relate`, `memory_graph_unrelate`, `memory_alias`,
`memory_relation_define`, `memory_graph`, `memory_recall`,
`memory_graph_review` (all but two actions, below), `memory_agents` and `memory_message`. The two board tools
inherit W2F-CLOCK-RETRY and W2F-SHARED-HLC below (both affect `send`, on
REST and MCP alike).

**MCP-SEARCH-EXPLAIN:** `memory_search(explain=true)` answers the refusal
envelope `{"error": "not_implemented", "message": "not_implemented: explain
(the ranking trace) is not yet served by the Rust daemon"}` before any
search runs, so no retrieval event or access count is written. Python runs
the search, then `service.trace` (a second, instrumented retrieval) and
serves verbose entries with `trace`. The trace port belongs with
`/api/trace` (`w2d-console-projections`). W2-D supplies expanded REST search, W2-E supplies the
harness-gated store body, and W2-F supplies the REST board. Their presence
does not register MCP tool bodies: their projections remain the
prerequisites listed above. Bound identity is enforced on the mount (M18).

**MCP-REFUSAL-CLEANUP:** MCP refusals use the shared counted reader with the
MCP 4 MiB budget, not the REST 256 KiB budget. Fully drained in-budget bodies
preserve the connection. An exhausted budget, failed read, 250 ms cleanup
deadline, or unread `Expect: 100-continue` body closes the connection. The
selected response may be delayed by that deadline, unlike Python's immediate
response and subsequent discard. Over-budget frame overshoot retains the
shared HTTP bound. The strict MCP comparator retains any resulting connection
header or transport differences; they are not normalized away. The live
corpus at `73c4220c5` retains five `body_limit` differences (indices 0, 2,
3, 4 and 5), all the added `Connection: close` header on a 413; response
statuses and bodies match, and all six body-limit probes complete.

**MCP-TOKENLESS-GUARD:** the Host/Origin helper exists inside the transport,
but the production tokenless mount still returns 501 before reaching it.
This slice does not enable tokenless MCP or claim live rebinding parity.
**MCP-BOUND-IDENTITY** is closed: an authenticated `/mcp` request carrying
`X-PL-Bank` / `X-PL-Principal` is checked against the bearer's W2-F board
context before the MCP layer (spec.md M18); the live `bound_identity` case
compares 19 requests with Python and the `mcp-binding-open` mutant is caught.

| Item | Python | Rust | Owner |
|---|---|---|---|
| MCP-LIVE-SESSIONS | Python on mcp 2.2.0 retains a live transport for every session its opening request established, until DELETE (the daemon passes `session_idle_timeout=None, max_sessions=None`, overriding 2.2.0's 30-minute idle close and 10,000-session cap) | the live session map is likewise unbounded, and keeps no history of ended sessions | resource-lifetime parity; future bounded admission needs an explicit policy |
| MCP-HTTP1-ONLY | the current daemon listener serves HTTP/1 | the Rust listener enables Axum's `http1` feature only; SSE uses `Connection: keep-alive`, which must be removed before any HTTP/2 listener is enabled | HTTP/2 transport deferred |
| MCP-SEALED-REQUEST-STATE | `tools/call`, `prompts/get` and `resources/read` unseal a non-null `requestState` before validation/dispatch; a valid server-minted token may resume a multi-round exchange | this slice never mints round-trip state and refuses every non-null token with -32602 `Invalid or expired requestState`, before readiness or tier changes; null remains absent | multi-round state issuance and authenticated unsealing are deferred with the unserved tool bodies; untrusted tokens cannot invoke a body |
| MCP-REQUEST-CANCELLATION | `notifications/cancelled` cancels the matching in-flight response; a legacy response stream settles with -32800 | cancellation notifications receive 202 but are discarded; any matching request still in flight later emits its ordinary response. Currently `memory_toolset` blocked by a backpressured GET notification stream exposes this window | W2-G follow-up: add per-session request tracking and response cancellation separately from disconnect-surviving write bodies; no bank tool body is enabled here |
| The 2026-07-28 stateless era (an `MCP-Protocol-Version` header outside the handshake versions, `_meta` envelopes, `subscriptions/listen`) | served | only the no-envelope refusal (400, -32602) | W2-G (follow-up); the shims and doctor use the handshake era, but README documents direct `--transport http` clients |
| A bearer revoked between the gate and dispatch | JSON-RPC -32004 `unauthorized` (or -32003 `principals_unavailable`) | never: the principal is resolved once, at the gate | free (no race window) |
| `Last-Event-ID` on GET (replay) | the SDK's replay path with no event store | ignored | non-canonical |
| JSON-RPC message validation: batches, non-object messages, ids that are not integers or strings | pydantic union validation | 400 -32602 with a fixed message (the text after `Validation error: ` is free) | non-canonical |
| Duplicate `X-PL-Writer` / `X-PL-Session` headers | the SDK request context's header mapping | the first value | non-canonical |

### MCP argument binding

The binder matches Python on every generated case (spec.md, "Tool argument
binding"). What it cannot or does not reproduce:

| Name | Python | Rust | Why |
|---|---|---|---|
| MCP-BIND-PRE-PARSE | a JSON-text argument holding an unpaired surrogate escape decodes to a str with a lone surrogate; nesting fails at a stack-dependent depth (RecursionError; the text stays a string) | the text stays a string (a Rust `String` cannot hold a lone surrogate); nesting deeper than 900 levels stays a string | no shipped producer sends either; the depth bound keeps the parser's stack finite |
| MCP-BIND-AS-OF-DIGITS | `float()` reads non-ASCII decimal digits in an `as_of` string | refused unless `fromisoformat` accepts it | `as_of` producers send ASCII epoch seconds or ISO text; closable with a decimal-digit table |
| MCP-BIND-AS-OF-PYTHON | `fromisoformat` follows the running interpreter: CPython 3.14 reads `T24:00` and refuses `12:30:45:67`, where 3.11 and 3.12.13 (the production base image, measured) do the opposite. `timestamp()` on a naive instant late on 9999-12-31 fails east of UTC (Tokyo refuses `9999-12-31T23:59`). On Windows a naive instant before 1970 raises OSError, which escapes `_check_as_of` | CPython 3.11/3.12's parser, and `timestamp()` as in UTC on Linux, the container default | the daemon runs in a Linux container; a local time zone east of UTC is not configured there |

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
| Remaining startup integration | CMS and canonical stores, dream tracking (writes `meta.dream_ack_secret_v1`), legacy import, HLC reseed and warmup search | complete entry seating, episodes, canonical loader inputs, resident cortex and identity metadata attach to cached Ready after all required loads; late HLC failure retains those stores with a pending flag and blocks returning Ready until clock-only retry succeeds; W2-D runs the warmup search; typed cortex currently reloads facts rather than adopting StartupState | W2-D/E (store adoption and clock ticking), W3-H (dream tracking), W3-J (legacy/file import) |
| Episode start-time ties | startup loads episodes with `ORDER BY started_at`, without a tie key | uses the same query ordering; ties among equal start times follow the server's row order in both arms | W3 PG-HYDRATE; Python ordering retained |
| Session-pointer or tombstone timestamp parse failures | fail before the embedder and resident-store abandon path; each initialization call retries parsing | fail inside required initialization and arm its retry backoff | W3 PG-HYDRATE; non-canonical metadata, declared divergence |
| Non-canonical startup metadata | Python coercions can accept arbitrary numeric spellings or non-string identities | supports shipped producer shapes; malformed durable HLC pairs refuse, and unbounded integers or Python-only identity/timestamp coercions remain deferred | W2-D/E for producer evolution |
| Reconnect after a lost session | heals on next use, rechecks the lease epoch | none: a lost writer session is not replaced | W2-E (write path) |
| Writer-session probe per call (`verify_writer_session`) | probes at most once a second | none | W2-E |
| Session reaper integration | closes idle sessions, derives generic titles and preserves resumable roots | reaping, cascade close, deferred-empty sweep, tombstone hydration and store/resume integration implemented; generic auto-title pending | W2-E (episodes) / W3-I |
| Autosave and exit persistence | changed-state save through resident writer stores | owned scheduling and exit callback implemented; admitted stores drain before exit, but counters and tensor weights are not saved | W2-E (durability) |
| Canonical-store compaction | removes selected resident records, marks slots dirty and rewrites their rows | selection policy implemented and checked against Python goldens; resident-store mutation and slot sync await the writer | W2-E / W3-I |
| Automatic dream and graph sweep stages | backlog/quiescence gate, extraction, analyzer and graph review ticks | session-end trigger interface is a declared no-op; sweep currently runs maintenance only | W3-H |
| `application_name` fallback | derived from argv | `pseudolife-mcp pid=<pid> pseudolife-daemon serve` | free (not persisted) |

## Request parsing edges (canonical shapes rule)

| Item | Python | Rust |
|---|---|---|
| Noncanonical static drive-relative requests and lexical escapes through linked roots | captured oracle observations | `lexical-outside-root`: 403 before filesystem access, exact refusal headers/body; shipped Console asset paths are unchanged |
| Windows nonempty dot/space-only segments except `.` and `..`, including `. ` and all-space segments | captured path-dependent refusal/fallback observations | `parent-space-refusal`: exact 403 before filesystem access; outside the shipped asset URLs |
| HTTP-BODY-LIMITS: cap enforced at frame granularity | uvicorn sends the selected early response immediately, keeps the connection alive when permitted and discards unread body data after the response | Rust waits for bounded cleanup before replying (up to 250 ms; early Expect refusals skip polling), then sends Connection: close whenever REST/hook cleanup was needed, including complete drains; MCP preserves fully drained in-budget connections as described above; polling stops at the route budget or failure, with one in-flight frame and Hyper's final drop poll able to overshoot; a slow in-cap body can exceed the deliberate deadline |
| Malformed or non-object SessionEnd bodies | becomes an empty object and returns 200 `{ok:true}` with no writes | deliberately returns 400 `invalid_json`: never acknowledge a close that did not happen; non-object SessionEnd bodies also use `invalid_json`, while other REST routes use `body_must_be_object`; empty bodies and valid objects retain their handling. The four HTTP-BODY-LIMITS scenarios now contain 132 live/golden cases, including 16 invalid SessionEnd probes. |
| Noncanonical JSON bodies with `NaN`/`Infinity`, lone surrogate escapes, or nesting past serde's 128 levels | accepted (or a 500 past ~1000 levels); a truthy SessionEnd ID may enter its mutation handler | REST and SessionEnd return 400 `invalid_json`, preserving the native strict JSON domain; the HTTP-BODY-LIMITS corpus includes fixed/chunked `{"session_id": NaN}` probes whose Python replies are 200 `{ok:true}` with unchanged disposable bank state; outside shipped canonical SessionEnd producers |
| Static types for extensions outside the Console build | platform `mimetypes` tables | `application/octet-stream` for unknown extensions; the shipped vendor Markdown notice uses its platform type |
| Unicode decimal digits in `top_k` / `min_score` (`top_k=٣`) | `int()` / `float()` accept them (3) | not a number: the route default (delegate ruling 2026-10-09: non-canonical input) |

## Graph and review

| Item | Python | Rust | Owner |
|---|---|---|---|
| Graph identity Unicode version | runtime Unicode tables (3.11 uses Unicode 14; newer Python images may differ) | pinned Python 3.11 Unicode 14 lowercase and final-sigma context, verified exhaustively | W3-H; revisit when the oracle runtime changes |
| Colliding inverse provenance | base-set iteration can select different source relation names for the same inverse triple depending on hash seed | choose the lexicographically smallest asserted source relation; exact triple and valid provenance membership are checked, with smallest-source choice pinned | W3-H; declared deterministic choice; all other provenance exact |
| Whole graph caps, communities, graph service/MCP wiring, fact projection, proposals, review judges/audit and deep dream graph pass | served graph and review behavior | store and GraphStore read-model helpers; these services are not wired (the four write tools and `memory_graph` are served: see "The graph store's write tools" and "memory_graph" below) | W3-H, subsequent increments |

## W2-D: GET /api/search

| Item | Python | Rust | Why / owner |
|---|---|---|---|
| Reference pool (Chroma documents from `document_ingest`, `ref_top_k` 3) | queried on every search once `<data_dir>/chromadb` holds a store | never queried: no reference hits, no `reference` components | the store is a private chromadb 1.5.9 on-disk format (HNSW segments); delegate ruling 2026-10-09: declared, harness banks hold no documents. `document_search` is declared with it. |
| **Cutover blocker:** reference store | Chroma on-disk format | none | needs v56 document table (Python change in flight; maintainer decision 2026-10-09: documents move to a pgvector table with exact cosine search); W2-D then ports the reference pool and `document_search` against it |
| Cross-encoder model | `memory.reranker.model_name` from the Hugging Face cache, lazily | the ONNX export in `PSEUDOLIFE_DAEMON_RERANK_DIR` (same checkpoint; `model_name` is only logged) | model packaging is cutover work (W3-J); parity of the export is proven by `harness/w2d_rerank_check.py` |
| Retrieval-event session and episode | resolved from the resident active-session pointer and episode tree | read from `meta.active_session_pointer` and `episodes` per search (same values) | W2-E owns the resident resolver; the call sites switch when it lands |
| `entries.access_count` persistence | resident counts written on the save cadence | resident counts only | W2-E (autosave) |
| Tie order among equal scores | `torch.topk` (unspecified) and stable sorts | stable sorts, resident order | free (spec) |
| Casefold in slot keys and pin scope | `str.casefold()` (full Unicode folding) | `to_lowercase` plus `ß` to `ss` and final sigma | rare foldings (ligatures and similar) differ; no producer writes them in slot names. Closable with a casefold crate |
| Pin-scope word characters, non-ASCII | `isalnum()` plus Unicode category M | `char::is_alphanumeric` minus circled letters, plus the main combining-mark blocks | ASCII is exact; closable with `unicode-normalization` |
| Duplicate current facts at hydration | demoted in memory, persisted on the next save | demoted in memory only | W2-E (save path); the bank's unique indexes prevent duplicates from every current producer |
| `memory_stats` reference bank (`reference_bank_size`, `reference_document_count`, and its share of `total_memories`) | the ChromaDB collection's size and distinct sources | `0`, as Python serves when its reference bank failed to open | the reference-bank port (W2-D `document_*`); Rust search serves no reference documents either |
| `memory_stats` counters persisted in `weights.pt` (`interaction_count`, per-band `hit_count`, `retrieval_queries`) | restored at startup, so they count across restarts | count from this process's start | W2-E autosave PR (the same `weights.pt` row below) |
| `memory_stats` `slot_index_shadow_divergences` and `weights_reset` | a sampled shadow check of the slot index; a flag for an unreadable `weights.pt` | `0` and `false`: Rust has no slot-index shadow and reads no `weights.pt` | the values Python serves on a healthy start |
| `memory_stats` `true_drops_total` from a malformed `capacity_true_drops` count | `int()` of any value Python accepts, including non-ASCII digit strings | integers, floats (truncated), booleans and ASCII integer strings; anything else serves both drop keys as null, as Python does for what `int()` rejects | the only writer stores a JSON integer |
| `memory_stats` `GET /api/stats` | the same `service.stats()` | not served | a later PR (the REST route over this body) |
| `memory_stats` band sizes under a custom preset that repeats a band name | positional: a store lands in `bands[0]` even when a later band shares its name | an entry carries its band name, so every entry of that name counts in the last band bearing it (exact at startup, where seating picks that band) | no preset repeats a name; a custom config would have to |
| Text block for a NaN or infinite float in an answer (`memory_history`'s `as_of` echo, a world fact's `retrieved_at`) | the SDK's `pydantic_core.to_json` text spells it `NaN` / `Infinity`, while structured content holds null | null in both (the envelope renders the text from the structured value) | structured content, which the harness compares, matches; only `float()` spellings reach these (`as_of="inf"`, `retrieved_at="inf"`) |
| Lesson `provenance` items that are not strings | `set()` of whatever JSON the row holds, then `sorted` | strings only (others dropped), as the personal cortex's `support`/`provenance` load | the only producer (the dream) writes strings such as `signal:12` |
| `memory_episode_summary` distributions over non-string tags | dict keys (`True == 1`) in `(-count, tag)` order, raising a TypeError when equal counts compare tags of different types | each distinct JSON value its own key; equal counts order by its JSON text (a list or object tag fails with Python's `unhashable type` text) | no tool writes a non-string tag (`normalize_tags`); a hand-written row would have to |
| `memory_outcome` `used_ids` tokens in non-ASCII digits | `int()` takes any Unicode decimal digit (`"١٢"` is 12) | ASCII digits only (with sign, leading zeros, single underscores and the 4,300-digit limit, as `int()`); other digits count as ignored | the same ASCII-only rule as `memory_stats`' drop count; agents type ASCII ids |
| `used_ids` and dereference credits naming the entry the startup warmup search served | the warmup search logs a NULL-session event: a session-less credit may name it as the most recent serving event, and a session's `used_ids` counts it as served elsewhere | Rust's warmup logs no event (the declared warmup row in `harness/run.py`), so such a credit names the previous NULL-session event or none, and the id may read as unmatched | W2-D warmup telemetry; the harness's ops never name the entry the warmup serves |
| `memory_stats` `true_drops` under a multi-band preset | counts evictions out of the last band only (earlier bands relocate) | counts every eviction, as every Rust eviction is a true drop (the multi-band store row below) | the opt-in presets; flat is the default |
| Legacy transport session (`PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1`, header `mcp-session-id`) | the event's session falls back to that header | not read | a one-release rollback switch no shipped client sets |
| Reranker or BM25 settings Python refuses (`reranker.fusion_weight` outside [0, 1], `reranker.top_n < 1`, `bm25.k1 < 0`, `bm25.b` outside [0, 1]) | init fails (500 on every route) or every BM25 search answers 400 | accepted as given | canonical configs use the shipped defaults; W1-A's early-refusal policy can absorb them |
| An integer `memory.reranker.fusion_weight` (`fusion_weight: 1`) | logged in `params.reranker` as written (`1`) | logged as a float (`1.0`) | a canonical config writes the default `0.7` |

## W2-D completion boundary (2026-10-13)

This increment serves `GET /api/search`; it does not close HTTP-READS or
RETRIEVAL as a whole. The following omissions are explicit cutover work,
not empty successful responses. Unwired HTTP handlers retain 501 after the
existing gates. The route inventory is `harness/goldens/routes.json`.

| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `w2d-console-projections` | `/api/stats`, `/api/overview`, `/api/facts`, `/api/facts/history`, `/api/world`, `/api/lessons`, `/api/episodes`, `/api/episodes/summary`, `/api/recent`, `/api/trace`, `/api/chain`, `/api/entry`, `/api/sources`, `/api/consolidation`, `/api/config` | Their service projections, parameter/error contracts and read side effects have no differential coverage in this search increment. W2-D follow-up must port and prove them individually; shared hydration alone is not their implementation. |
| `w2d-standalone-canonical-search` | The `/api` entry points for cortex lookup/candidates, world search and lesson search | Only the cortex block attached to `/api/search` is wired over `/api`. The MCP tools (`memory_fact_get`, `memory_world_search`, `memory_lesson_search` with its telemetry) are served; the `/api` return shapes remain W2-D follow-up work. |
| `w2d-briefing-hooks` | `/api/briefing`, `/api/hook/session-start`, `/api/hook/memory-policy` | Briefing composition and session-start registration/policy logging are not implemented here; depend on W2-E session writes. W2-D owns the projections, W2-E the writes. |
| `w2d-recall-graph` | `/api/recall`; `/api/graph`, `/api/graph/projects`, `/api/graph/digest`, `/api/graph/communities`, `/api/graph/path`, `/api/graph/review`, `/api/graph/proposal-evidence`, `/api/wiki`, `/api/graph/entity-provenance`, `/api/curation/duplicates`, `/api/curation/retired`, `/api/dream/status` | The MCP tools `memory_recall` and `memory_graph` are served (spec "memory_recall over MCP", "memory_graph over MCP"); their REST routes, the review/curation recovery and the dream projections are later W3-H work. |
| `w2d-board-projections` | `/api/agents` outside the coordination view, `/api/maintainer`, `/api/maintainer/sent`, `/api/maintainer/inbox` | Principal-scoped board/maintainer reads and their transaction boundaries belong to W2-F. The coordination view is supplied by W2-F in this batch; the other listed projections remain deferred. |
| `w2d-tokenizer-runtime` | Other SentenceTransformers tokenization semantics | The inherited embedder follows the ST 5.3.0 / transformers 4.57.6 contract in `spec-embedding.md`. ST 5.7.0 changes padded-query cortex scores; supporting that runtime is deferred rather than changing the oracle output or score tolerance. Evidence records the actual runtime versions. |
| `w2d-model-provisioning` | Default automatic embedding and reranker artifact provisioning | Existing local ONNX graphs are required. The embedding override must contain the configured Qwen/MiniLM layout; the reranker override must be the configured cross-encoder checkpoint. W3-J owns installation/provisioning. No claim is made for arbitrary exported checkpoints. |
| `w2d-query-cache-lifetime` | Multiple configured embedders in one process | The inherited outer query cache is process-global and keys raw query text; it is valid only for the daemon's single immutable service/model configuration. Per-embedder caching also remains. Hot model replacement or multiple service instances require cache ownership changes before support. |

`no reference hits` remains the separate v56 dependency above: the Python
Postgres document migration has not landed and no schema change is included.
The typed cortex loader uses the live embedder dimension, while the shared
startup and PostgreSQL schema guards still refuse incompatible banks; this
is not authorization to migrate vectors or change the schema dimension.

Search golden recording stores only Python-oracle responses and bank state.
The inherited replay needs the identical seeded template in the same harness
process: seed clocks and generated identities are not portable fixture data,
and the CLI cleans up the template at exit. `w2d-portable-goldens` remains a
named W2-D follow-up for committed CI fixtures; recording a candidate response,
replacing an expected value or widening normalization is not a remedy. Local
same-template oracle recording/replay is separate evidence, not CI coverage.

## Write path (W2-E)

`memory_store` (spec W1, W2). Each row names the pull request expected to
close it, or why it stays.

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `memory.cortex.auto_promote: true` | slot-shaped facts promoted into the cortex on every store | the client receives successful store admission with `cortex_promoted: 0`, but no cortex rows are written; the configuration is not refused | W2-E cortex PR (needs the cortex write path) |
| Presets with more than one band (`continuum` and aliases, multi-band `custom`) | store lands in `bands[0]`, the promotion walk moves entries down the chain, and eviction relocates to the next band | store lands in `bands[0]`; no promotion walk; every eviction is a true drop | the opt-in presets; flat is the default since 2026-08-15 |
| `custom` band `retention_policy` other than `balanced` | its own eviction score | scored as `balanced` | same |
| Entry insert fails after eviction | the entry stays resident (served until restart) and the error propagates | the entry is not kept; the error propagates | failure path only; keeps bank and table aligned |
| `surprise_history`, `interaction_count` | written to `weights.pt` by autosave | resident only | W2-E autosave PR |
| Per-call `_ensure_init` passes: writer-session probe, rehydrate after a changed lease, correction / reinstatement / curation / lesson-synthesis recovery | run before every store | not run | writer-session probe and reconnect: W2-E; recovery passes belong to the curation and dream slices |
| Unicode newer than 14.0 in the label, meta-filter and slot regexes | Python 3.11 tables (Unicode 14.0) | the `regex` crate's tables (Unicode 16.0) | identical for every code point assigned by Unicode 14.0 (full scan) |
| Non-ASCII decimal digits in a generic session title | `\d` matches them | ASCII digits only | titles are `strftime` output or caller text |
| MCP dispatch of the store body | FastMCP | served on `/mcp` (spec.md W0.4); `w2e.py` compares both daemons' `tools/call` answers | closed; the feature-gated harness route is no longer used by any harness |
| `entries.embedding`, `entries.surprise` | torch fp32 | ONNX Runtime fp32 | compared within measured tolerances (`harness/goldens/w2e-tolerance.json`); an admission decision or reason must never differ |

### W2-E first increment boundaries

Stores and background reaping now use one episode log, touch map, deferred-root
map and tombstone map under `Ready.write`. Startup constructs that owner from
the validated startup candidate; no second episode cache is published. This
closes the preparation's cache-ownership gap without adding another database
connection or changing the leased writer boundary.

Admitted store tasks are tracked independently of disconnected callers and
drained before the background exit callback. This completes their write-through
database operations; it does not implement tensor-state persistence.

The remaining write work is deliberately split into subsequent increments:

| Item | Remaining behavior | Owner / reason |
|---|---|---|
| Canonical writes | `memory_fact_set` (spec FS1-FS12: HLC ticking, contenders, supersession, the per-slot save), `memory_set_add` / `memory_set_remove` (spec SA1-SA11: conversion, dedup, the aggregate guard, the cap, removal) and `memory_fact_resolve` (spec FR1-FR8: contender promotion and retirement) are served, and so are world facts (`memory_world_set`, spec WD1-WD12) and the forget of facts, world facts and lessons (`memory_forget`, spec FG1-FG16); slot extraction here is a pure text operation, not a canonical write | the world and lesson stores' restore and console reads (no served MCP tool reaches them) |
| Lessons | `memory_outcome` (signals, attribution and the `used_ids` label; spec OC1-OC12), `memory_reinforce` and the resident lesson store's read side (`memory_lesson_search`, the `memory_history` chain; spec LS1-LS10, CH1-CH6) are served; `memory_forget` retires lessons (scope `lesson`, and the cascade of scope `memory`; spec FG8, FG12-FG14); the dream's synthesis and restore remain unimplemented | W3-H (dream), the lifecycle increment |
| Episode titles on idle close | `memory_episode_start`, `memory_episode_end`, `memory_session_title` and `memory_episode_summary` are served (spec EP1-EP12) on the state store-driven open/resume and empty-root reaping share; automatic content titles on idle close remain unimplemented | W2-E reaper follow-up; content titles also rewrite entry stamps |
| Autosave and clean-exit flush | Background scheduling exists, but changed-state saving and exit flush remain no-ops; store rows write through, while counters, surprise history and band weights do not survive restart via `weights.pt` | W2-E persistence increment; the Python tensor format and recovery contract need a separate proof |
| Resident cortex refresh | `memory_fact_set`, `memory_set_add`, `memory_set_remove` and `memory_fact_resolve` update the resident cortex in place (readers take snapshots), `memory_world_set` and `memory_forget` the resident world store, and `memory_forget` the resident lesson store (readers take snapshots); auto-promotion stays disabled | W2-E auto-promotion increment |
| Frozen store catalog snapshots | Original store recordings predate the catalog instrument's `column_types`, owner/comment and sequence-bound representations; replay retains and reports initialization differences | Historical recordings are retained unchanged; live Python comparison is the current-source proof, not a waiver of frozen replay differences |
| New reaper recording's startup state | The live fixture inherits Python-seeded relation timestamps, curation metadata and dream acknowledgement state; a fresh Rust-only replay does not reproduce that seed state | W3-H startup/dream integration; the new recording is retained unchanged and its replay differences are reported separately from the matching live reaper behavior |
| Unicode runtime | Label, meta-filter and slot regexes retain the existing Unicode-14 assigned-character contract; newer assigned characters may follow Rust's newer tables | Explicitly retained runtime substitution; no comparator or frozen expectation is weakened |
| Malformed hydrated slot rows | Python may raise while unpacking a malformed persisted slot; Rust ignores the entire entry's structured-slot comparison if any row is not four strings, then retains the text/negation paths | W2-E retained substitution for corrupt or noncanonical persisted data; `malformed_rows_and_superseded_entries` pins it, and canonical producers emit four-string rows |
| Shutdown deadline | Admitted stores drain without a timeout; a store stuck on an unresponsive PostgreSQL connection can block graceful exit indefinitely | W2-E lifecycle follow-up; a deadline and cancellation recovery need proof that eviction and subsequent insert cannot be torn apart |
| Exact-duplicate boundary | The fixed-order Rust dot product and Python BLAS reduction are not proven identical at the exact `1.0` duplicate threshold; a numerically near-boundary value may change the rejection reason | W2-E numeric-boundary follow-up; measured vector tolerances do not waive exact admission/reason equality |
| Hosted write-path execution | Both CI operating systems compile the `harness,mutants` feature combination, but the W2-E live, shutdown, full-docs slot corpus and store replay/control runs remain local evidence | W2-E harness follow-up; hosted parity needs the real-model oracle fixture and explicit handling of the reported frozen startup differences, without suppressing them |

### memory_fact_set (2026-10-17)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `write_fact` with `force_contend=True` (consolidation quarantine), `dream=True` (the dream's claim writes: direct-write guard, echo tier cap, `dream:<tier>` marks on insert and park) or `support="assistant"` (assistant-origin park) | ported in `memory/cortex.py` | not ported; `stores/cortex_write.rs` takes no such inputs | unreachable over MCP (`memory_fact_set` passes user/action/agent, no stance, no provenance); the consolidation and dream slices (W3-H) add them with their callers |
| `stance` and `provenance` on a fact write | written by the dream and consolidation | always none (a confirm still clears a stored stance, as Python's does) | same |
| A read concurrent with a fact write | waits for the service lock, so it sees the write or none of it | reads the snapshot it took: it may predate a write still in progress, or show the written record before that write has persisted or answered its caller | readers never hold a lock across an await; each read is internally consistent, and a read that starts after a write returns sees it |
| `_entity_kind_cache` reset when a changed writer lease rehydrates the service (`service.py:1416`) | the next `"auto"` fact write rereads `entity_kinds` | kept for the process | the rehydrate path is the per-call `_ensure_init` row above (W2-E writer-session work) |
| Fact embeddings | torch fp32 | ONNX Runtime fp32 | compared within the store tolerances per fact id (`harness/w2e.py`); a routing decision never depends on them |
| `memory.cortex.protect_provenance`, `supersede_confidence_margin`, `reinforce_rate`, `memory.lessons.enabled`, `memory.lessons.min_confidence` of the wrong type | `bool()` / `float()` at store build or per call (a string `"false"` is truthy) | refused at config load, like every other typed key (`wrong_types_python_accepts_refuse_here`) | the existing config-type substitution |

### memory_set_add and memory_set_remove (2026-10-17)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `add_member` with `support="assistant"` (the conversion guard and the join park, `cortex.py:891, 995`), a support tier under `protect_provenance` (the conversion tier guard, `:897`) or `dream=True` (the direct-write guard and the `dream:<tier>` mark, `:901, 1029`); provenance on a member write | ported in `memory/cortex.py` | not ported; `stores/cortex_members.rs` takes no such inputs | unreachable over MCP (`service.set_add` passes `origin=None`, no provenance, `dream=False`); the dream and rollback slices (W3-H) add them with their callers |
| `remove_member` refusal (`member_remove_refused`: an assistant, lower-tier or dream retraction, `cortex.py:1121-1128`) | ported | not ported; the note text is served for parity | unreachable over MCP (`service.set_remove` passes `origin=None`, `dream=False`) |
| The member dedup cosine at exactly 0.9 | torch float32 `dot` and `norm` (BLAS / vectorized reduction order) | float32 products summed in f64, rounded once to f32 | within one f32 step of torch (measured on 2,000 unit 1024-vectors: at most 4.5e-8 apart); a cosine that close to 0.9 may decide `member_confirmed` against `member_added` differently, as may the ONNX/torch embedding gap (about 1e-6) itself; the harness members sit at least 0.08 from the line |

### memory_fact_resolve (2026-10-17)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `resolve` with `expect_value` (`refused_changed`, `cortex.py:1477-1479`; the service's `contender_changed` answer, `service.py:4513-4516`) or `dream=True` (`refused_direct` against a direct-backed current, `:1480-1485`, the `direct_write` answer, `:4509-4512`, and the `dream:<tier>` mark on the promoted contender) | ported | not ported; `stores/cortex_resolve.rs` takes no such inputs | unreachable over MCP (`service.cortex_resolve` gets `support="user"`, `dream=False`, `expect_value=None`); the consolidation quarantine (W3-H) adds them with its caller |

The WRITE-CMS, WRITE-CORTEX and CONTRADICTION parity rows remain deferred;
this increment does not claim the complete write boundary or cutover readiness.

### memory_supersede and memory_reinstate (2026-10-19)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A correction or reinstatement whose outcome is unknown (the lock session or connection lost around `COMMIT`) | `PendingCorrectionRecovery` / `PendingEntryReinstatementRecovery` classify the durable state under fresh locks and publish whichever outcome committed; ordinary reads wait behind the recovery | a failed transaction rolls back and the resident bank is unchanged; a commit whose answer was lost leaves the row changed and the resident bank stale until restart, and the call answers `internal_error` | the lifecycle increment, with the store path's own commit-unknown handling (`Entry insert fails after eviction` above); failure path only |
| `entry_mutation_lock` (session advisory locks per target id, held from before the transaction to after publication) | taken | not taken; the per-operation `pg_advisory_xact_lock` of `reinstate_entry` is | the leased single writer already serializes every entry mutation in one process; another writer cannot hold the bank |
| The promotion walk after the replacement (`_consolidate_eligible`) | runs, as after any store | not run | the multi-band row above: the flat default never promotes |
| File-mode corrections (unique exact text without row ids, `id_unavailable`, the file-signature recovery) | served | unreachable: the Rust daemon runs on PostgreSQL only | not ported |

### memory_forget (2026-10-19)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A forget whose commit outcome is unknown | (reconciled by the read/save barriers) | the transaction rolls back, or a lost commit answer is an internal error with the resident bank unchanged | the lifecycle increment, with the corrections' commit-unknown row above; failure path only |
| Where a pending lesson refresh (a failed mirror after a committed forget) is retried | in `_ensure_init`, before every service call, whose failure fails that call | before the next lesson read (`memory_lesson_search`, the history chain) or lesson save; other tools read no lessons | failure path only |
| A lesson read racing a forget | waits on the service lock and the refresh barrier | reads its snapshot without the lock: between the cascade's commit and the mirror it can still serve a lesson retiring | the read slices' lock-free snapshots (as for the cortex) |
| A malformed `meta.session_digest_cursor` (a `ts` that is not a number or numeric string, a `retry` that is not an object, a non-iterable or non-string `regenerate`) | `float()` / `dict()` / `sorted()` raise their own `TypeError` / `ValueError` text | the same refusal kind (`invalid_argument`) with Rust's own wording | corrupt persisted data only; the dream writes the cursor |
| `memory.delete_confirm_threshold` of the wrong type | `int(value or 0)` per call (a float truncates) | refused at config load, like every other typed key | the existing config-type substitution |
| File mode | served | not reachable: the daemon is PostgreSQL-only | W3-H |

### memory_consolidation_candidates (2026-10-19)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The cosine `_cosine` (`consolidation.py:81-92`) | torch float32: `torch.dot` (BLAS order) over float32 norms | each sum accumulated in f64 and rounded to f32, then the same float32 division | measured-tolerance numeric substitution: results agree to about 1e-7 before the 4-place rounding; a cosine within that of `min_cohesion` could join one cluster and not the other |
| The reference pool (ChromaDB documents) in a query anchor's retrieval | queried, appended, then dropped as unaddressable; with documents present and the reranker on, the larger pool can skip the rerank (`len(combined) <= top_n`) | never queried, as for `memory_search` | the search reference-pool row above (delegate ruling 2026-10-09: harness banks hold no documents); with documents and the reranker on, scores and seed order can differ |
| The episode anchor's walk order | `band.entries` per band, which a multi-band preset reorders on relocation | bank order per band | the declared multi-band store divergence (EP11); every episode score is 1.0, so the walk order picks the seeds and the one entry kept per text |

### The graph store's write tools (2026-10-19)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A read racing a relate or an alias | waits on the service lock, so it sees the call's commits all or none | the write tools hold the writer lock, but two reads of the graph do not take it (`memory_fact_get`'s entity resolution, `memory_history`'s graph load): between a relate's node mints and its edge commit, they can see a minted node without its edge (`memory_lesson_search` holds the lock) | the read slices' lock-free reads; Python commits each store call on its own too, so a crash between them leaves the same partial state on both |
| Several current facts owning one slot key (the slot-key fold) | `LIMIT 1` with no order | the same statement | declared free: both read one unordered row |
| `POST /api/graph/relate` and `/api/graph/unrelate` (the Console's graph editor) | served over the same service bodies | not served | the Console REST rows (PARITY.md); the bodies are shared when they are |
| File mode (`graph_requires_postgres`) | answered when no database is configured | not reachable: the daemon is PostgreSQL-only | the PostgreSQL-only rows above |

### memory_graph (2026-10-19)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The order of the inverse-derived edges that mirror asserted edges | `derive_edges` walks its inverse rule over a set of `(src, relation, dst)` tuples, so their order follows the per-process string hash | ordered by their source edge's relation, source and target (the read model's rule) | the "Colliding inverse provenance" row above: the harness compares them as a multiset in their slots. Asserted edges, transitive edges and the mirrors of transitive edges keep their exact order; an inverse edge only mirrors a pair already in the adjacency, so paths do not depend on it |
| `GET /api/graph` (the Console's whole graph with `scope` and the `max_nodes` hub cap) | served over `_whole_graph` | not served; the MCP tool's whole graph (no scope, no cap) is | the Console REST rows (PARITY.md) |

### memory_recall (2026-10-19)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `memory.recall.driver: llm` (or `PSEUDOLIFE_RECALL_DRIVER=llm`): seeds named by the dream's extraction model | served through an outbound model call | refused as `not_implemented` for a non-blank query | the model call joins the dream's extractor (task P); the default mechanical driver is served |
| Locking across the walk | none of its own: each search and each graph read takes the service lock, so writes can land between them; the vocabulary and the degrees are two locked graph loads | each graph read takes the writer lock, but a search takes only the search guard, which does not exclude writes (as for `memory_search`); one graph load serves the vocabulary and the degrees | a write landing mid-walk is visible to the later steps on both; only a write racing one search, or landing between Python's two loads, can tell them apart |
| A wrong-type `memory.recall` setting (`max_searches_per_hop: 2.5`, `hub_gate: "false"`, `driver: null`) | taken untyped (`int()` truncates, a non-empty string is truthy, `None` falls to mechanical) | refused at config load, like every other typed key | the existing config-type substitution |
| `time_budget_seconds` (20 s) | wall clock (`time.monotonic`) | wall clock (`Instant`) | a walk near the budget can stop at a different step; the harness's walks finish in well under a second |
| The order of seed-search and re-query hits | `CMS.retrieve`'s ranking | `search::rank` | the search rows above: equal scores under `torch.topk` are free, and embeddings agree within the measured tolerance, so near-equal hits can swap and with them `texts` and the mention counts behind the re-query order |
| The order of the inverse-derived edges that mirror asserted edges, in `edges` | the graph read's hash order (the memory_graph row above) | the read model's order | inherited from `memory_graph`; recall's edges carry no provenance, so when the 15-edge cap binds, which of them survive can differ too. The harness's recall templates hold no asserted inverse pair |
| `GET /api/recall` (the uncapped walk) | served | not served | the Console REST rows (PARITY.md) |

### memory_graph_review: list and the decision actions (2026-10-19)

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A lesson `restore_slot` that needs the audit-snapshot fallback (a slot whose retired row compaction purged, alone or mixed with retired records) | rewrites it through `lesson_write` | the `not_implemented` refusal, predicted exactly and before any write; a restore Python would also fail in its legacy writes answers the refusal instead of the persistence error | a Rust `lesson_write` (the lesson write path); world facts restore their snapshots |
| A curated restore whose commit outcome is unknown and whose reload cannot be classified | raises, and every later call retries the recovery until it resolves | raises the same internal error; later calls are not gated | the daemon runs no per-call recovery passes (the `_ensure_init` row above) |
| A batch (`proposal_ids`) | takes the service lock per call, so another request can run between items | holds the writer lock for the whole batch | strictly more serialized; a single client sees the same results |
| `list`'s analysis | between two locked blocks, so writes can land while it runs and `recent_merges` can follow a later state than the findings | under the writer lock throughout | writes wait for the analysis (quadratic in the entities); the answer is one consistent state |
| A `proposals` entry whose `rationale` is a list | stored as its array text | refused (`internal_error`, after the entities are minted) | the binder's `list[dict]` admits it; no producer sends one. An object errors on both |
| A `proposals` `similarity` spelled as a hexadecimal float (`"0x1p-3"`) | parsed by PostgreSQL's `strtof` | refused (`internal_error`) | no producer sends one |
| A merge-group tie between keys of different types (merge rows with a NULL target) | `TypeError: '<' not supported between instances of …` | `invalid_argument` with Rust's own wording | merge rows always carry a target |
| A corrupt automatic-decision index (`next_seq` not an integer, `cursors`/`active` not mappings) | `int()` / `dict()` raise | read leniently in `list` | corrupt persisted data only; the judges write the index |
| A NaN in a REAL proposal or decision column | serialized as `NaN` | `null` | NaN is not a stored score |
| The judges (merge two- and three-vote, link, junk, curation, candidates), their freshness and the sweep tick that runs them | served by the background sweep | not run | task O2, with the dream (task P), behind a stub extractor |
| Wrong-type `memory.deep_dream.judges_enabled` / `analyzer_file_duplicates` | taken by truthiness | refused at config load | the existing config-type substitution |

## Coordination board completion (W2-F)

| Name | Disposition and evidence | Remaining gate |
|---|---|---|
| W2F-MAIL-PACING | The original mail golden is retained unchanged in the completion evidence; the supplied recordings are not committed as portable expectations because they omit the elapsed input schedule. It was captured with two interleaved daemon arms and `active_seconds: 1`; standalone replay sends sooner. Both `wake.rs` and Python `CoordinationStore._wake_decision` compare `last_activity > now - active_seconds`. The initial completion baseline reproduces four response differences and 180 downstream state differences: project mail and replies remain `hinted/active` in Rust where the recording says `not_needed/no_park`, affecting delivery reads and audited payloads. This is an unsynchronized input-clock limitation, not permission to change wake semantics or ignore diffs. | Strict golden replay remains nonzero when the boundary differs. A future clock-controlled oracle capture must prove both sides of the activity boundary; no comparator, tolerance or recorded expectation is changed here. |
| W2F-CONSOLE-STARTUP | Both implementations return `available: false, reason: not_initialized` before storage exists. Golden replay previously started after the first HTTP health response while only the live Python arm waited for warmup. The runner now waits for `db: ok` on both arms, and for Python's embedding warmup before the scenario; Console remains read-only and does not initialize the bank. | Runner regression covers both modes; the original Console recording is retained in the completion evidence. Cold-start Console behavior remains unchanged. |
| W2F-PAIRING-HTTP | The authenticated valid-body `/api/pair` route remains a deliberate 501 refusal. The SQL redemption primitive exists, but the HTTP two-slot non-queueing gate, attempt refunds, disconnect ownership and immediate principal-cache publication are not integrated. Wiring only the SQL call would expose an incomplete credential-enrolment path. This slice retains the refusal rather than treating store-level coverage as endpoint coverage. | **Cutover blocker:** HTTP-SECURITY / CLI-PAIRING must cover success, same-token retry, conflicts, expiry/revocation, environment-name exclusion, saturation, failure budgets and cancellation before enabling BK4 at HTTP. |
| W2F-CLOCK-RETRY | After a send detects an epoch change, Python clears the cached session-probe timestamp and calls `_ensure_init()` under the service lock before retrying. Rust only marks the board clock unseeded, reloads its high-water value and retries with the existing storage lease epoch. The database epoch check still refuses the stale write, but the retry cannot heal the writer session. | **Cutover blocker:** integrate W2-E writer-session refresh and prove a changed-epoch send either recovers once or refuses without a partial write. |
| W2F-SHARED-HLC | Python board and memory writes use the service clock. Rust currently owns a separate board clock; the service clock now exists for fact writes (`hlc.rs`, `Ready::tick_hlc`, spec FS6) but the board has not adopted it, so board-only and mixed Python/Rust board audit checks do not establish ordering across Rust board and memory writes. | **Cutover blocker:** move the board onto `Ready::tick_hlc`, then test interleaved board/memory writes, restart reseeding and lease-epoch changes. |

The board transaction boundary is independent of the shared writer adapter:
`Board::conn` serializes the dedicated client, driver transactions roll back
when dropped, and `board::db::commit` probes for an aborted block before
reporting success. A failed connection is replaced only on the next call;
no uncertain write is retried. This does not use the writer's global recovery
marker, which belongs to a different PostgreSQL session.

Once storage is published, board calls bypass the full initialization mutex,
matching Python’s storage-tier fast path. The dedicated connection reuses the
shared connector’s ten-second socket-and-startup deadline without acquiring
the leased writer’s transaction lock.
