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
| `/api/hook/session-start`, `memory-policy`, `coordination-start` (any caller) | briefing, policy and board check-in text | W2-D / W2-F |
| Authorized `memory-changes`, `park-gate`, `woke`, `subagent`, and `session-end` after its gates | change note, park gate, woke and subagent writes, episode close | W2-E / W2-F |
| `POST /api/pair` after a valid body on an authenticated install | redemption (principal store write) | W2-F |

## Startup and configuration

| Item | Python | Rust | Why |
|---|---|---|---|
| Storage modes | file mode, or embedded lite Postgres when no DSN | refuses to start (exit 2) without `PSEUDOLIFE_MCP_DATABASE_URL` | the port targets the Postgres tier; lite tier is cutover work (W3-J) |
| TLS to Postgres | libpq `sslmode` (default `prefer`) | plaintext only | no producer DSN asks for TLS; the shim's rustls client can be reused when one does |
| YAML dialect | PyYAML (YAML 1.1: `yes/no/on/off`, `010` octal, `1_000`, `0x10`) | yaml-rust2 (YAML 1.2 core) | canonical config files use `true/false` and plain decimals |
| Wrong-typed values for keys Python does not validate (`memory.top_k: abc`) | starts, fails later at use | refuses to start (exit 1) naming the key | fail early instead of serving a half-broken daemon |
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
| `application_name` fallback | derived from argv | `pseudolife-mcp pid=<pid> pseudolife-daemon serve` | free (not persisted) |

## Request parsing edges (canonical shapes rule)

| Item | Python | Rust |
|---|---|---|
| JSON bodies with `NaN`/`Infinity`, lone surrogate escapes, or nesting past serde's 128 levels | accepted (or a 500 past ~1000 levels) | 400 `invalid_json` |
| Static types for `.md`, `.csv` and other extensions outside the Console build | platform `mimetypes` tables | `application/octet-stream` |
| Unicode decimal digits in `top_k` / `min_score` (`top_k=٣`) | `int()` / `float()` accept them (3) | not a number: the route default (delegate ruling 2026-10-09: non-canonical input) |

## W2-D: GET /api/search

| Item | Python | Rust | Why / owner |
|---|---|---|---|
| Reference pool (Chroma documents from `document_ingest`, `ref_top_k` 3) | queried on every search once `<data_dir>/chromadb` holds a store | never queried: no reference hits, no `reference` components | the store is a private chromadb 1.5.9 on-disk format (HNSW segments); its Rust form is a delegate/maintainer decision (W3-J). A bank with no documents matches. |
| Cross-encoder model | `memory.reranker.model_name` from the Hugging Face cache, lazily | the ONNX export in `PSEUDOLIFE_DAEMON_RERANK_DIR` (same checkpoint; `model_name` is only logged) | model packaging is cutover work (W3-J); parity of the export is proven by `harness/w2d_rerank_check.py` |
| Retrieval-event session and episode | resolved from the resident active-session pointer and episode tree | read from `meta.active_session_pointer` and `episodes` per search (same values) | W2-E owns the resident resolver; the call sites switch when it lands |
| `entries.access_count` persistence | resident counts written on the save cadence | resident counts only | W2-E (autosave) |
| Tie order among equal scores | `torch.topk` (unspecified) and stable sorts | stable sorts, resident order | free (spec) |
