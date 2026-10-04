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
finds it at `127.0.0.1:5433` on its own. It logs in with the first of:
`PSEUDOLIFE_TEST_PG_PASSWORD` (as `PSEUDOLIFE_TEST_PG_USER`, else the bank
owner); the **test login file** `~/.pseudolife-mcp/test-pg.env`
(`PSEUDOLIFE_TEST_PG_LOGIN_FILE` moves it); `POSTGRES_PASSWORD` from
`ops/.env`, as the bank owner; the compose default. Create the test login
once with `pseudolife-mcp test-login create` on the daemon host (the
installer does it on a fresh install): it is a role that creates and drops
its own databases and cannot connect to the bank, so no checkout needs
`ops/.env`, which holds the bank owner's password. A run that still logs in
through `ops/.env` prints one line saying so. See
[agent isolation](docs/guide/agent-isolation.md).
A server that answers but rejects the credentials makes the PG-backed tests
**error**, not skip — only an absent server skips them — so a rotated
password can never produce a green run by accident. A full run checks this
before it queues for the suite lock and refuses to start, naming where the
rejected password came from and the fix: create the test login (or re-run
`test-login create`, which re-applies the file's password), or export
`PSEUDOLIFE_TEST_PG_USER` and `PSEUDOLIFE_TEST_PG_PASSWORD` (or correct the
latter, since it overrides the file). A targeted run prints one warning line
and starts. The check is skipped when
`PSEUDOLIFE_TEST_DATABASE_URL` is set, in xdist workers, and with
`PSEUDOLIFE_SUITE_LOCK=off`.

That instance is also the server holding your real bank (`pseudolife_memory`),
so for it **set nothing**: the suite provisions its own per-run database
there (see below). `PSEUDOLIFE_TEST_DATABASE_URL` exists to point the suite
at a *different*, disposable server — CI's service container, or a throwaway
Postgres of your own — and wins whenever set:

```bash
export PSEUDOLIFE_TEST_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:55432/pseudolife_memory_test"
```

An override is used verbatim: its database is reset at the start of every
PG-backed test (other connections to it terminated, then every table
truncated) and is not dropped afterwards. The test and bench fixtures refuse
a production bank — `pseudolife_memory`, or whichever database
`PSEUDOLIFE_MCP_DATABASE_URL` names — before connecting, and ask the server
which database they reached before resetting it. The suite also removes
`PSEUDOLIFE_MCP_DATABASE_URL` from its own environment, so an exported daemon
DSN never binds a test fixture to your bank. If that DSN leaves its database
implicit (no `dbname`, so libpq would use the user name or a service file),
the suite refuses to start, and the eval harnesses refuse to reset or
replay anything: unset it — tests never need it — or name the database.

URI query options and keyword connection strings are preserved when selecting
isolated test databases. Eval-backed tests use the same server unless
`PSEUDOLIFE_BENCH_ADMIN_URL` explicitly selects another one. Reachable
permission or setup failures also error instead of skipping.

The local password reader accepts quoted literals and inline comments.
Compose variable expansion in `POSTGRES_PASSWORD` is refused with a safe
diagnosis; use a single-quoted literal or `PSEUDOLIFE_TEST_PG_PASSWORD` for
that case. Credential-bearing parser frames are omitted from error reports,
including reports with local variables enabled.

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
1.2 GB.

Most tests do not depend on what the embedding model thinks is similar, so
by default they embed through `tests/fake_embedder.py`: a deterministic,
weight-free stand-in (hashed words and character trigrams, 1024-d like the
real model) that keeps identical text identical and unrelated text apart.
Real CPU forward passes were the suite's largest single cost. A test whose
assertions depend on the real model's geometry (paraphrase similarity,
dense ranking, cosine thresholds between differently worded texts,
published retrieval floors, the model path itself) carries
`@pytest.mark.real_model` and gets the real weights. Mark a new test the
same way when it needs them; `tests/conftest.py` fails a `real_model` test
that ends up embedding through a fake built by an earlier test in its
module. `PSEUDOLIFE_TEST_EMBEDDER=real` gives every test the real weights;
use it locally for changes to retrieval, ranking or embedding, since the
default run exercises those only in the marked tests (CI's `test` lane
always runs them on the real weights).

Ordinary code changes run touched and dependent test files locally; they
do not require a local full suite. Select dependent tests through reverse
imports, fixture dependencies and references to changed paths, commands
and configuration keys, including relevant guards and platform tests.
Record the tested head, selection, dependency reasoning and result in the
PR. A missing PostgreSQL service or an unexpected platform/database skip
is not a pass for the affected behavior.

A local full suite remains required for schema/DDL/migration changes;
shared test infrastructure, conftest, fixtures, suite lock or imported test
helpers; daemon process creation, ownership, shutdown or recovery changes;
and dependent coverage that cannot be bounded confidently. Finish review
fixes and targeted validation before queueing a required full run. Keep
the suite lock, one local slot, CPU-only execution, PostgreSQL preflight
and fingerprint guard, and avoid overlapping saturating work. Follow
[the project validation rules](CLAUDE.md#running-tests-exit-code-discipline)
for docs-only and test-only changes and merges from master.

`tests/conftest.py` enforces the lock for full runs (all of `tests/`, half
or more of its files, or a `-k`/`-m` that only excludes): it takes
`~/.pseudolife-mcp/locks/full-suite.lock` and queues runs in arrival order.
`PSEUDOLIFE_SUITE_LOCK=fail` exits instead of queueing and `=off` skips the
lock, which is the default on GitHub Actions. `PSEUDOLIFE_SUITE_SLOTS` (a
whole number from 1 to 8, default 1) sets how many full runs may hold it at
once. The suite hides the GPU unless `PSEUDOLIFE_TEST_CUDA=1`. A run whose
imported files change on disk while it waits refuses to start and asks for
a rerun. On a Windows host, `pwsh ops/wsl-suite.ps1` runs the committed
HEAD's suite in WSL, and `pwsh ops/remote-suite.ps1` runs it on the first
free machine (local WSL, or a second Linux machine configured outside the
repository); both refuse a checkout with uncommitted changes (the WSL
launcher's `PSEUDOLIFE_WSL_SUITE_SOURCE=worktree` tests the working tree
instead). Both run `ops/wsl-suite.sh`, which tags the run with
`PSEUDOLIFE_SUITE_RUN_ID` and, once pytest exits (also on Ctrl+C or a
hangup), stops every process still carrying that marker and names each on
stderr; it exits 130, 129 or 143 for an interrupt, hangup or TERM, else
with pytest's code. A run dispatched to the second machine refuses a test
server that holds a production bank, and checks again once it holds the
suite lock, since a server that refused the first connection may be up by
then.

All tests must pass. Every PR requires fresh CI for its current head
integrated with current master, including the full PostgreSQL, lite Linux,
Windows, macOS and required analysis checks. If master moves, update the
branch and wait for fresh CI; rerunning an old job tests its old merge
commit. Independent review remains required, and the maintainer owns the
merge.

Trial from 2026-09-28; the maintainer reassesses on 2026-10-12 against the
measures in the maintainer's private suite-gate memo of 2026-09-28.

CI's two full-suite lanes run this exact invocation
(`-n 2 --dist loadfile` shards whole files across two workers so
module-scoped fixtures keep their semantics): the `test` lane with
`PSEUDOLIFE_TEST_EMBEDDER=real`, so every test also runs on the real
weights for each PR, and `test-lite-linux` with the default. Two more
lanes run narrower fixed file lists: `test-lite-windows`, and
`test-lite-macos` for the plugin hooks and installers under macOS's
bash 3.2 and BSD tools. If you add
behavior, add a test; if you fix a bug, add the test that would have
caught it.

A docs-only change can skip the full run locally. That means Markdown
anywhere, `llms.txt` / `llms-full.txt`, and non-code files under `docs/`
(such as `docs/atlas/atlas.json`), with nothing under `tests/`, `ops/`,
plugin hooks, workflows or packaging, and no code or `.json` outside
`docs/`. Run the doc guards instead (`tests/test_release_ux.py`,
`tests/test_llms_txt.py`, `tests/test_atlas_currency.py`,
`tests/test_eval_evidence.py`, `tests/test_i18n_readme.py`), plus every
test file that names a file you touched (`git grep -l <file basename>
tests/`). CI's full lanes still have to pass before merge.

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
