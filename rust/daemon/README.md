# pseudolife-daemon (contract-first port)

The Rust port of the Pseudolife memory daemon, built contract-first: each
slice writes a short spec of the exact Python behaviour it must match, with
`file:line` sources, then proves it with a live differential harness against
the Python daemon on disposable banks, a mutant control, and two independent
reviews. It is not a release artifact: nothing installs or runs it yet.

| File | What it holds |
|---|---|
| `spec-w1a-foundation.md` | Configuration, route table and gate order, `/health`, the lazy init lifecycle, the storage constructor, stored principals (slice W1-A) |
| `spec.md` | `GET /api/search` (the 2026-10-09 spike) |
| `spec-http-static.md` | Root redirect, every committed Console asset, path containment, fallbacks and response headers |
| `spec-w3h-graph-store.md` | Entity, alias, relation and edge storage (first W3-H increment) |
| `divergences.md` | Everything the Rust daemon deliberately does not match yet, with the slice that owns each row |
| `harness/` | The differential harness, its goldens and helpers |

## Running it

The daemon needs `PSEUDOLIFE_MCP_DATABASE_URL` (it has no file mode) and
reads the Python daemon's environment and `<data dir>/config.yaml`. The
embedder resolves `embedding.model_name` from the existing local model or
Hub cache, or from the complete model root in `PSEUDOLIFE_DAEMON_ONNX_DIR`.
It validates `embedding.onnx_file_name` (default `onnx/model.onnx`) relative
to the Transformer module and reads the model's pooling/tokenizer metadata.
Missing artifacts refuse; it never downloads, exports or falls back to torch.
It uses the CPU provider through the ONNX Runtime library named by
`ORT_DYLIB_PATH`. `PSEUDOLIFE_DAEMON_STATIC_DIR` points at the Console build
and `PSEUDOLIFE_PLUGIN_DIR` at the plugin tree for `hooks_digest`.

## The harness

```bash
python rust/daemon/harness/run.py live --rust-bin <path to pseudolife-daemon> --out results.json
```

It creates `pl_cf_w1a_*` databases on the bench PostgreSQL through the test
login (`~/.pseudolife-mcp/test-pg.env`; any other name is refused), starts
both daemons on disposable homes per scenario, compares every response by
value and both banks' state afterwards, and exits non-zero on any
difference. `run.py mutants` takes a `--features mutants` build and requires
each deliberate break to turn the harness red against a clean control.
`run.py live --record` writes the oracle's normalized answers and bank state
to `harness/goldens/` (no raw bodies or machine paths; vectors, large bodies
and catalogs as digests); `run.py golden` checks the Rust daemon against them
without a Python daemon. Golden mode replays every scenario's responses
except the five timing scenarios (`lease-held`, `reaper`, `null-embedding`,
`unconstrained-dims`, `db-lost`), which ask both daemons the same question at
the same moment and are live-only. It checks bank state only where the bank
starts empty, since a seeded template carries run-specific values. `gen_schema_sql.py --check` and
`record_routes.py --check` keep the embedded schema DDL and route table equal
to the Python source.

`--only static-build static-paths static-missing static-root-link` exercises the static layer
without loading models: both daemons use an unreachable loopback DSN to
isolate asset serving from bank startup. Set `PL_HARNESS_SLICE=http`
for the HTTP slice's isolated database prefix. Static build and path goldens
are platform-specific because Python's MIME database and path resolution
differ between Windows and Linux. The six `static-*` source mutants check
redirects, CSP, containment, component boundaries, content types and exact JSON file bytes.
Golden replay gets WebP/Markdown MIME/cache expectations from the local Python
MIME database; headers and file bytes are still compared exactly. Rust also
refuses a directory index linked outside the static root, a named divergence.

The existing `trust-bind` scenario requires a debug `--features mutants` binary.
Its configured non-loopback host still passes through each daemon's original
bind guard; only the fixture listener is overridden to `127.0.0.1`. The
harness checks the binary capability before creating banks or starting it.
Release builds cannot enable this listener override.

The schema startup boundary has a separate mode in the same harness:

```bash
python rust/daemon/harness/schema_ci.py --out schema-parity.json
```

It builds the daemon's native storage test executable, compares fresh and
historical banks from `schema_history.json` after each open/restart, checks
future/malformed-version refusal without durable writes, and catches five
compiled source mutants. It reuses the harness's disposable database and full
catalog/row helpers, with `pl_cf_pgs_` fixture names. CI supplies its existing
isolated test login. No embedding model is loaded by these storage cells.
`gen_schema_sql.py --check` checks the base DDL, the entire ordered migration
plan, dimension refusal text and generated metadata against `schema.py`.
The same run compares Rust post-state with the committed oracle captures in
`harness/goldens/schema-startup.json`, using the existing golden catalog
digests and full row values. New relation times must first fit their own arm's
captured constructor window; existing row times remain exact.

To regenerate schema data and oracle post-state fixtures after an upstream
change, use one command with the isolated test login configured:

```bash
python rust/daemon/harness/gen_schema_sql.py --record-goldens schema-capture.json
```

The graph-store boundary uses the same disposable-bank and state helpers:

```bash
cargo build --manifest-path rust/Cargo.toml --bin graph-contract --features graph-harness,mutants -j 3
python rust/daemon/harness/graph_store.py live --candidate <graph-contract> --out graph-live.json
python rust/daemon/harness/graph_store.py golden --candidate <graph-contract>
python rust/daemon/harness/graph_store.py mutants --candidate <graph-contract> --out graph-mutants.json
```

It compares every response and all bank state after each operation. Clocks
are checked in each arm's operation window and keep their change identity;
stored REAL responses use PostgreSQL's text-format spelling like Psycopg.
Goldens retain all-row and sequence digests; catalogs must remain unchanged
within each arm, and live mode also compares them across arms. The fixture
binary is available only with `graph-harness`; it has no model or HTTP server.
These are store APIs for subsequent services, not installed graph routes.

`run.py embedding --rust-bin <binary> --model <existing model> --out <file>`
runs Python torch, Python ONNX and Rust sequentially on a canonical text corpus.
`--ranking-banks <dir>` adds all 78 replicated-eval bank dumps (read-only).
Default vector comparison is exact; `--accepted-fp32` applies the declared,
artifact-specific bounds in `spec-embedding.md`, preserving exact tokens, cache
behavior and full/top-eight order. A missing or failing arm stays a failure.
The Qwen Python ONNX `position_ids` failure is recorded, never treated as a
successful ONNX comparison. `--fixture --golden harness/goldens/embedding-fixture.json`
uses locally generated mixed-sign graphs for CI and catches eight compiled
mutants. Mean/right-padding and last-token/left-padding/Normalize profiles include
padded raw batches; the latter consumes attention-mask, token-type and position
inputs. Its Python fixture adapter supplies position IDs explicitly, with that
adjustment recorded; the real Qwen wrapper remains unmodified.
It certifies the process/cache/tokenization seam, not real-model equivalence.
