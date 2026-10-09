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
| `spec-principals.md` | Durable principal service operations, snapshot races and identity refusal policy |
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

The principal-store extension reuses the disposable database guard, catalog
dumper and type-strict comparison. It runs service operations in the native
process, using synthetic credentials and `pl_cf_prn_*` banks, without loading
an embedder or exposing a diagnostic HTTP endpoint:

```bash
cargo build --manifest-path rust/Cargo.toml -p pseudolife-daemon --features principal-harness,mutants -j 3
python rust/daemon/harness/principals.py live --rust-bin <binary> --out principals-live.json
python rust/daemon/harness/principals.py golden --rust-bin <binary> --out principals-golden.json
python rust/daemon/harness/principals.py mutants --rust-bin <binary> --out principals-mutants.json
```

Every database mutation compares both catalogs and all durable rows. New
timestamps must fall inside the arm's database-clock write window, including
the invite TTL offset; unchanged timestamps retain their event identity.
The concurrent-redemption case verifies two blocked callers and exactly one
durable winner; only that case's validated winning hash becomes a symbol.
Eight rejecting clock and ownership controls guard the normalizer. Goldens contain outputs
and post-state, never bearer tokens, pairing codes or connection strings.
The `principal-harness` feature is absent from the serving release build.
