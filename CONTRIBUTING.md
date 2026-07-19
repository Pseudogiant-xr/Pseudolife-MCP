# Contributing to Pseudolife-MCP

Thanks for wanting to improve Pseudolife-MCP. This is a small, carefully
tested codebase — the bar for merging is "surgical, tested, and explained",
not "big".

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

The documented invocation is offline + deterministic (the embedder must
already be cached; first run without the env vars will download it):

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest -q
```

All tests must pass. CI runs exactly this on every PR. If you add behavior,
add a test; if you fix a bug, add the test that would have caught it.

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
- Schema changes bump the schema version and must migrate additively
  (`ADD COLUMN IF NOT EXISTS` on daemon start — see existing migrations).

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
