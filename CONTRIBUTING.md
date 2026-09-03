# Contributing to Pseudolife-MCP

Thanks for wanting to improve Pseudolife-MCP. This is a small, carefully
tested codebase — the bar for merging is "surgical, tested, and explained",
not "big".

Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md). The
project is solo-maintained and best-effort: issues are read and most get an
answer, but there is no response-time commitment.

## Dev setup

Python 3.10+ and a Postgres with pgvector (the bundled compose stack provides
one). The daemon and tests are CPU-only by contract — no CUDA needed.

```bash
python -m venv .venv
. .venv/bin/activate                # Windows: .venv\Scripts\activate
# CPU torch first so pip never pulls the multi-GB CUDA build:
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -e .[dev]
```

Tests need a Postgres to talk to. Easiest is the bundled stack's instance
(`docker compose -f ops/docker-compose.yml up -d pseudolife-pg`) — the suite
finds it at `127.0.0.1:5433` on its own. Point at a different server with
`PSEUDOLIFE_TEST_DATABASE_URL` (it wins whenever set):

```bash
export PSEUDOLIFE_TEST_DATABASE_URL="postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory_test"
```

Without that override each pytest process provisions its own private
`pseudolife_memory_test_<pid>` database and drops it at interpreter exit, so
concurrent runs never terminate each other — and no live bank is ever touched.

## Running the tests

The documented invocation is offline + deterministic — both embedders must
already be in the HuggingFace cache:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest -q -n 2 --dist loadfile
```

Two models are load-bearing, and a missing one is a hard failure under those
env vars: `Qwen/Qwen3-Embedding-0.6B` (the default since schema v25) and
`all-MiniLM-L6-v2` (still pinned by the tests that guard the symmetric/ONNX
paths). A first run *without* the offline vars downloads both — budget about
1.2 GB. Budget for a slow suite too: real CPU embeds put a warm local run at
~437s, up from 238s on the pre-v25 ONNX path.

All tests must pass. CI's two full-suite lanes run this exact invocation
(`-n 2 --dist loadfile` shards whole files across two workers so
module-scoped fixtures keep their semantics); a third lane
(`test-lite-windows`) runs a narrower fixed file list. If you add
behavior, add a test; if you fix a bug, add the test that would have
caught it.

## If you run a live bank

Some contributors dogfood the server while hacking on it. Two standing rules
from hard experience:

- **Never `docker compose down -v`, `docker volume rm`, or
  `docker system prune --volumes`** — the bank lives in external volumes and
  these delete it.
- **Back up before risky changes**: `ops/backup.ps1` (Windows) or
  `ops/backup.sh` (Linux/macOS). Deploy daemon changes with
  `ops/update.ps1` / `ops/update.sh`, which backs up first and tags a
  rollback image.

## Pull requests

- Branch off `master`; keep each PR to one logical change.
- Commit style is conventional (`feat:`, `fix:`, `docs:`, `test:`,
  `chore:`, scope in parens — see `git log`).
- User-visible changes get a line in `CHANGELOG.md` under `[Unreleased]`.
- Match the surrounding code's style and comment density. Comments explain
  *why*, not *what*.
- Schema changes bump the schema version — see [Schema bumps](#schema-bumps)
  below for the migration rule and the files that move together.

## Schema bumps

`ensure_schema` is additive-only: `CREATE TABLE IF NOT EXISTS` and
`ADD COLUMN IF NOT EXISTS`, never an in-place `ALTER` of an existing column's
type. That isn't a style preference — a daemon that half-migrates a live bank
at boot can neither finish nor undo it.

A change that *can't* be expressed additively doesn't get an exception. It
ships two things instead:

- a **startup refusal** — the daemon detects the mismatch and stops rather
  than write into a bank it can no longer write correctly (see
  `_refuse_on_embedding_dim_mismatch` in
  `pseudolife_memory/storage/schema.py`, added for the v25 embedding-dimension
  change);
- a **human-gated script under `ops/`** that does the real migration offline —
  backup first, daemon stopped (`ops/migrate_embeddings.py` re-embeds every
  row before moving the columns).

Never a silent half-migration at boot.

A bump also touches seven places, and they land in the same change or the
guard tests go red:

- `SCHEMA_META_VERSION` in `pseudolife_memory/storage/schema.py`;
- the capabilities table in `README.md`;
- the DSN row *and* the schema version-history table in
  `docs/guide/configuration.md` (both pinned by `tests/test_release_ux.py`);
- the single `CURRENT_SCHEMA` literal in `tests/test_schema_version.py` — no
  per-version file and no `>=` relaxation pass; the ladder is gap-checked by
  `tests/test_release_ux.py` and `tests/test_atlas_currency.py`. Whatever
  behaviour the bump adds gets a test **beside its consumer**, or a row in
  `tests/test_schema_ddl_shape.py` if it is pure DDL shape — never a new
  `tests/test_schema_vNN.py`;
- a `CHANGELOG.md` entry that names `vNN`;
- `docs/atlas/atlas.json` `meta.schema` (pinned by
  `tests/test_atlas_currency.py`) — re-verify the affected storage cards,
  don't just renumber;
- the two literal meta-version pins in `tests/test_migrate_embeddings.py`,
  then `python ops/gen_llms_txt.py` if any doc changed
  (`tests/test_llms_txt.py` pins the generated file).

## Licensing of contributions

Pseudolife-MCP is Apache-2.0. By contributing you agree to the
[Developer Certificate of Origin](https://developercertificate.org/) —
sign your commits off to say so:

```bash
git commit -s
```

The `Signed-off-by:` line certifies you wrote the code (or have the right to
submit it) under the project license. PRs without sign-off will be asked to
add it.

**New dependencies** must be permissively licensed (Apache-2.0, MIT, BSD or
equivalent). No GPL/AGPL — the project deliberately swapped out its last
copyleft dependency and intends to stay that way. LGPL is acceptable only as
an unmodified, unvendored install-time dependency (like `psycopg`).

## Questions / design discussions

Open a GitHub issue before building anything large. The `docs/specs/`
directory shows the design-first pattern bigger changes follow — a short
issue sketch is enough to find out whether a feature fits before you spend a
weekend on it.
