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
| `divergences.md` | Everything the Rust daemon deliberately does not match yet, with the slice that owns each row |
| `harness/` | The differential harness, its goldens and helpers |

## Running it

The daemon needs `PSEUDOLIFE_MCP_DATABASE_URL` (it has no file mode) and
reads the Python daemon's environment and `<data dir>/config.yaml`. The
embedder loads lazily from `PSEUDOLIFE_DAEMON_ONNX_DIR` (a directory with
`model.onnx`, `model.onnx_data` and `tokenizer.json`, the verified fp32
Qwen3-Embedding-0.6B export) through the ONNX Runtime library named by
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
