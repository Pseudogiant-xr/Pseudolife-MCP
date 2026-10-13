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
| `/mcp`, `/mcp/*` after the bearer gate | the MCP transport | W2-G |
| Tokenless MCP Host/Origin rebinding policy and bound-identity validation | SDK transport policy and durable board binding checks before tool dispatch | **CUTOVER BLOCKER — W2-G**; MCP stays unmounted (501) until these gates are ported |
| Credential-bearing extractor/recall redirect refusal | `utils/no_redirect.py`, extractor and recall transports | W3-H / W2-D; outbound transports are not implemented in this slice |
| `/api/hook/session-start`, `memory-policy` (any caller) | briefing and memory-policy text | W2-D |
| Authorized `memory-changes`, `park-gate`, `woke`, `subagent`, and `session-end` after its gates | change note, park gate, woke and subagent writes, episode close | W2-E / W2-F |
| `POST /api/pair` after a valid body on an authenticated install | the 2-slot redemption gate (`RedemptionGate`, 429 `rate_limited`), invocation of the implemented `principals::redeem` store transaction plus immediate `PrincipalStore::add`, the failure-budget refund on success, and 503 `pairing_unavailable` on a store error | HTTP-SECURITY / CLI-PAIRING (the gate wraps only `principal_store.redeem`, `web/api.py:378-393`) |
| Stored callers' POST `/api/config` and `/api/daemon-notice` gate | HTTP already returns 403 `operator_principal_required` by stored-name inference; method-aware source policy is implemented for HTTP-SECURITY to consume with `resolve_detailed` | HTTP-SECURITY |

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
| Malformed or non-object SessionEnd bodies | becomes an empty object and returns 200 `{ok:true}` with no writes | deliberately returns 400 `invalid_json`: never acknowledge a close that did not happen; non-object SessionEnd bodies also use `invalid_json`, while other REST routes use `body_must_be_object`; empty bodies and valid objects retain their handling. The four HTTP-BODY-LIMITS scenarios now contain 132 live/golden cases, including 16 invalid SessionEnd probes. |
| Noncanonical JSON bodies with `NaN`/`Infinity`, lone surrogate escapes, or nesting past serde's 128 levels | accepted (or a 500 past ~1000 levels); a truthy SessionEnd ID may enter its mutation handler | REST and SessionEnd return 400 `invalid_json`, preserving the native strict JSON domain; the HTTP-BODY-LIMITS corpus includes fixed/chunked `{"session_id": NaN}` probes whose Python replies are 200 `{ok:true}` with unchanged disposable bank state; outside shipped canonical SessionEnd producers |
| Static types for extensions outside the Console build | platform `mimetypes` tables | `application/octet-stream` for unknown extensions; the shipped vendor Markdown notice uses its platform type |
| Unicode decimal digits in `top_k` / `min_score` (`top_k=٣`) | `int()` / `float()` accept them (3) | not a number: the route default (delegate ruling 2026-10-09: non-canonical input) |

## Graph and review

| Item | Python | Rust | Owner |
|---|---|---|---|
| Graph identity Unicode version | runtime Unicode tables (3.11 uses Unicode 14; newer Python images may differ) | pinned Python 3.11 Unicode 14 lowercase and final-sigma context, verified exhaustively | W3-H; revisit when the oracle runtime changes |
| Colliding inverse provenance | base-set iteration can select different source relation names for the same inverse triple depending on hash seed | choose the lexicographically smallest asserted source relation; exact triple and valid provenance membership are checked, with smallest-source choice pinned | W3-H; declared deterministic choice; all other provenance exact |
| Whole graph caps, communities, graph service/MCP wiring, fact projection, proposals, review judges/audit and deep dream graph pass | served graph and review behavior | store and GraphStore read-model helpers; these services are not wired | W3-H, subsequent increments |

## Write path (W2-E)

`memory_store` (spec W1, W2). Each row names the pull request expected to
close it, or why it stays.

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `memory.cortex.auto_promote: true` | slot-shaped facts promoted into the cortex on every store | never promotes; `cortex_promoted` stays 0 | W2-E cortex PR (needs the cortex write path) |
| Presets with more than one band (`continuum` and aliases, multi-band `custom`) | store lands in `bands[0]`, the promotion walk moves entries down the chain, and eviction relocates to the next band | store lands in `bands[0]`; no promotion walk; every eviction is a true drop | the opt-in presets; flat is the default since 2026-08-15 |
| `custom` band `retention_policy` other than `balanced` | its own eviction score | scored as `balanced` | same |
| Entry insert fails after eviction | the entry stays resident (served until restart) and the error propagates | the entry is not kept; the error propagates | failure path only; keeps bank and table aligned |
| `surprise_history`, `interaction_count` | written to `weights.pt` by autosave | resident only | W2-E autosave PR |
| Per-call `_ensure_init` passes: writer-session probe, rehydrate after a changed lease, correction / reinstatement / curation / lesson-synthesis recovery | run before every store | not run | writer-session probe and reconnect: W2-E; recovery passes belong to the curation and dream slices |
| Unicode newer than 14.0 in the label, meta-filter and slot regexes | Python 3.11 tables (Unicode 14.0) | the `regex` crate's tables (Unicode 16.0) | identical for every code point assigned by Unicode 14.0 (full scan) |
| Non-ASCII decimal digits in a generic session title | `\d` matches them | ASCII digits only | titles are `strftime` output or caller text |
| MCP argument binding and the error envelope | FastMCP / pydantic | not in this slice; the harness reaches the body through a feature-gated route | W2-G |
| `entries.embedding`, `entries.surprise` | torch fp32 | ONNX Runtime fp32 | compared within measured tolerances (`harness/goldens/w2e-tolerance.json`); an admission decision or reason must never differ |

### W2-E first increment boundaries

Stores and background reaping now use one episode log, touch map, deferred-root
map and tombstone map under `Ready.write`. Startup constructs that owner from
the validated startup candidate; no second episode cache is published. This
closes the preparation's cache-ownership gap without adding another database
connection or changing the leased writer boundary.

The remaining write work is deliberately split into subsequent increments:

| Item | Remaining behavior | Owner / reason |
|---|---|---|
| Canonical writes | Fact set, HLC ticking, contenders, supersession, set-valued slots and world facts remain unimplemented; slot extraction here is a pure text operation, not a canonical write | W2-E cortex increment; scalar and set history require their own response and post-state proof |
| Outcomes and lessons | Outcome attribution, lesson updates and their persistence remain unimplemented | W2-E outcomes increment |
| Episode APIs and titles | Standalone episode operations, session-title tools and automatic content titles on idle close remain unimplemented; store-driven open/resume and empty-root reaping share state | W2-E episode increment; content titles also rewrite entry stamps |
| Autosave and clean-exit flush | Background scheduling exists, but changed-state saving and exit flush remain no-ops; store rows write through, while counters, surprise history and band weights do not survive restart via `weights.pt` | W2-E persistence increment; the Python tensor format and recovery contract need a separate proof |
| Resident cortex refresh | No canonical write path updates resident canonical stores; auto-promotion stays disabled in this increment | W2-E cortex increment, coordinated with W2-D read-store ownership |
| Frozen store catalog snapshots | Original store recordings predate the catalog instrument's `column_types`, owner/comment and sequence-bound representations; replay retains and reports initialization differences | Historical recordings are retained unchanged; live Python comparison is the current-source proof, not a waiver of frozen replay differences |
| Unicode runtime | Label, meta-filter and slot regexes retain the existing Unicode-14 assigned-character contract; newer assigned characters may follow Rust's newer tables | Explicitly retained runtime substitution; no comparator or frozen expectation is weakened |

The WRITE-CMS, WRITE-CORTEX and CONTRADICTION parity rows remain deferred;
this increment does not claim the complete write boundary or cutover readiness.
