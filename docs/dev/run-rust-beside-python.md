# Run the Rust daemon beside Python

The checkout updater can start a read-only Rust companion while Python keeps
serving the bank. This is an opt-in test: it does not select Rust as the default
engine, replace Python, install clients or publish an image.

## Before starting

Run these commands **on the Docker host**, from a clean checkout of the commit
you want to test. Docker Compose v2 and the installed `pseudolife-mcp` CLI must
be available. The Python writer must already run the matching master or release build.
Its stack must use this checkout's Compose project, named bank and state
volumes, and `ops/.env`; keep those settings. `--env-file` is a release-mode
option and does not redirect checkout mode's host file.
Never copy the host env file into a different checkout. Have at least 8 GB free
and enough additional space for a Rust build and its provisioned models.

Use an updater from the matching checkout, not an older installed package.
For an existing **Python CLI**, select the checkout in a separate terminal
before the commands below (`$env:PYTHONPATH = (Get-Location).Path` in PowerShell):

```sh
export PYTHONPATH="$PWD"
```

A current native CLI needs no `PYTHONPATH`; its installation is described in
[native runtimes](native-runtimes.md). This rehearsal used the Python CLI with
that checkout selection, and did not install or move any client runtime.

Check Python first:

```sh
git rev-parse HEAD
git status --short
curl --fail --silent --show-error http://127.0.0.1:8765/health
```

Healthy Python reports `"status":"ok"`, `"ready":true`, and the same schema
version as `SCHEMA_META_VERSION` in the checkout's
`pseudolife_memory/storage/schema.py`. A read-only shadow cannot migrate an
older bank: arrange the active writer's ordinary update separately before
continuing. A pre-v57 bank also needs its Python counters import completed.
Do not change engines as a way to repair a schema mismatch.

Use the Python daemon's actual loopback port with `--daemon-url` if it differs
from 8765. The companion defaults to loopback port 8766; an occupied port needs
an unused `PSEUDOLIFE_SHADOW_PORT` in the host env file before starting.
Live-bank shadowing requires the maintainer's separate G1 approval; this guide
and its disposable rehearsal do not grant it. Default-engine cutover, releases
and deployment remain separate decisions.

## Start and check the companion

```sh
pseudolife-mcp update --engine rust --shadow --checkout .
curl --fail --silent --show-error http://127.0.0.1:8766/health
curl --fail --silent --show-error http://127.0.0.1:8765/health
```

The update builds `ops/Dockerfile.daemon-rust` from this checkout, applies the
SELECT-only `pseudolife_shadow` role to this project's PostgreSQL, saves its
random password in this checkout's `ops/.env`, and starts only
`pseudolife-rust-shadow`. Its `/data` mount is read-only; models have a separate
persistent cache. It runs as the Python writer's daemon user (uid 10001) with
every capability dropped, so it reads the writer's private `config.yaml`, and
it takes the writer's `TZ`, writer id and toolset settings. First-start model
provisioning may take 30 minutes; let the updater finish its readiness gate.

Successful output includes `read-only Rust shadow answers at
http://127.0.0.1:8766; Python is unchanged`. Rust health must report
`"status":"ok"`, `"ready":true`, `"read_only":true`, the expected schema and
checkout build revision, and no `"counters_import_required":true` indication.
Python must still be healthy. Repeating the start command on the same clean checkout and
configuration reports `already healthy` without recreating the companion.
A failed command is not a successful rehearsal; retain its exit status and
inspect the companion's logs before retrying.

### A model cache created by an older companion

Before #1319 the companion ran as root, so a model cache volume it created
stays root-owned, with a model bundle directory uid 10001 cannot read. On
such a host the companion fails its startup gate although the model files
are there. While the updater waits, the companion's log
(`docker ps -a --filter name=pseudolife-rust-shadow` names its container,
then `docker logs <container>`) reports a missing model artifact, as
`model artifact is missing or unsafe: <path>; …` or `Qwen model artifact is
missing or incomplete in <directory>; …`. Once the wait fails (at the
latest after its 32-minute provisioning budget), the updater removes the
companion and stops with
`shadow startup did not become ready within its provisioning budget;
Python is unchanged; …`. Neither the companion's repair hint (`run
pseudolife-daemon provision models …`) nor the updater's plain retry helps
here: the cache volume itself has to go.

Remove the companion, then that project's model cache volume, then start
again:

```sh
pseudolife-mcp update --engine python --shadow --checkout .
docker volume rm pseudolife-mcp_pseudolife_shadow_models
pseudolife-mcp update --engine rust --shadow --checkout .
```

The first command reports `no Rust shadow is installed` when the failed
start already removed the companion; that is expected. The volume is
`<project>_pseudolife_shadow_models`, and `pseudolife-mcp` is the default
project name (`docker volume ls --filter name=pseudolife_shadow_models`
lists it). The models download again, which can take up to about 30
minutes.

### Memory beside a busy writer

The Python writer also writes on its read path: entry access counts and
reinforcements, the counters row, episodes and session meta. Since #1332
the companion reloads only the stores whose tables changed. An entry change
that touches only `access_count`, `reinforcements` or `last_logical_turn`
is patched in place, and on a bank with one band new entries are appended
in place when every new id is above the highest resident one. These still rebuild the
whole resident bank, briefly holding its old and new copies and raising
memory: deleted entries, any other column change (content, `dream_state`,
so every dream, supersession, tags, episode) and additions out of id
order. A `facts`, `world_facts` or `lessons`
change reloads that store whole. A new `bank_id` or a failed refresh
rebuilds every store, as a cold start does. An entry or fact vector that
does not fit the embedder, or a flat-band migration the loaded config does
not match, also takes that cold-start path, which then refuses the bank
with a cold start's message. The companion's log says which happened:
`read-only shadow: resident state refreshed (<summary>)` for a partial
refresh, `read-only shadow: rebuilding the bank (<reason>)`, `read-only
shadow: rebuilding every store (<reason>)` or `read-only shadow:
incremental refresh failed (<error>); rebuilding every store`; a full
rebuild ends with a bare `read-only shadow: resident state refreshed`.

## Compare reads

The comparator is a **Python test oracle**, run on a machine with Python 3.10+
that can reach both loopback endpoints (normally this Docker host). The Rust
runtime image and its installed CLI contain no Python. Keep the existing CLI
and client registration for this test; client conversion is separate.

Create the ignored `data/` directory if needed, and save approved representative
query strings or explicit read requests as `data/shadow-queries.json`. For example:

```json
[
  "synthetic rehearsal note",
  {"name":"facts", "path":"/api/facts", "params":{"limit":5}},
  {"name":"lessons", "tool":"memory_lesson_search", "args":{"query":"synthetic rehearsal note"}}
]
```

Set `PSEUDOLIFE_MCP_TOKEN` privately to this daemon's bearer; do not print it or
paste it into command arguments. Use `python` instead of `python3` on Windows.
The CLI commands above are the same in PowerShell; use `curl.exe` there.

```sh
python3 rust/daemon/harness/shadow_compare.py --python-url http://127.0.0.1:8765 --rust-url http://127.0.0.1:8766 --queries data/shadow-queries.json --out data/shadow-result.json
```

The JSON and adjacent `.txt` report record both daemon builds and every
difference. Exit 0 means no untriaged differences, 1 means differences, and 2
means invalid input or transport failure. Review the report: do not automatically
accept or discard differences. Python CPU `auto` may use bf16 while Rust uses
the pinned fp32 graph. An explicit bf16 setting instead causes a startup
refusal with the supported configuration choices. Score differences are covered by the
[CPU precision disposition](../../rust/daemon/divergences/startup-and-configuration/cpu-precision.md).
Confirm a difference has that cause before recording an exact case/path in a
`--triage` file; keep the raw report and do not broaden score tolerances.
Python retains its normal read telemetry; Rust must remain read-only. Reports may contain bank data: keep them private.
For read-only sampling of retrieval logs and fact slots, `--database-url-env`
names an environment variable containing a SELECT-only DSN and requires
`psycopg`; a query file needs no database driver.

## Remove it

```sh
pseudolife-mcp update --engine python --shadow --checkout .
curl --fail --silent --show-error http://127.0.0.1:8765/health
```

Expect `removed the Rust shadow; Python is unchanged`. This stops and removes
only the companion, retaining its model cache and password for reuse. It does
not switch the writer, recreate PostgreSQL, delete volumes or remove the
SELECT-only role. Repeating it reports `no Rust shadow is installed`.
Removal does not require a healthy writer or usable shadow credentials.
Always include `--shadow`: omitting it requests a writer engine operation.
Do not use `docker compose down -v`.

## Rehearsal evidence

Rehearsed on 2026-11-04 on the **Windows host Docker Desktop, disposable
project**, at `4078a306b28649ece4cf77a63c65b02b405a645c`, using its own PostgreSQL,
synthetic bank, named volumes and generated env file. The conductor changed the
venue after a ready PostgreSQL on the Linux dev box could not be reached from
a second container (`pg_isready` exited 2); host networking was not changed.
The Python fixture was built from the same commit before the writer baseline
was recorded. The test used loopback ports 18765/18766 (and
`--daemon-url http://127.0.0.1:18765` on each updater command).

| Step | Exit | Observation |
| --- | --- | --- |
| Build Rust image from checkout | 0 | Revision matches; native CLI answers `native-update-v1`; neither `python` nor `python3` is installed |
| Start companion | 0 | Rust `ok`, `ready:true`, `read_only:true`, schema 59, ONNX on CPU |
| Repeat start | 0 | `already healthy`; no recreation |
| Compare with Python CPU `auto` (bf16) | 1 | 16 cases: 11 exact, seven score differences and four telemetry/tolerance differences |
| Repeat same corpus with Python fp32 | 0 | 16 cases: 14 exact, two access-counter differences; no untriaged differences |
| Remove companion | 0 | `removed the Rust shadow; Python is unchanged` |
| Repeat removal | 0 | `no Rust shadow is installed` |

For the control, only the disposable Python daemon's precision was changed to
fp32 before recording a fresh identity baseline; the bank was not reseeded,
and the query corpus was identical. Start, repeat-start, removal and repeat
removal each exited 0 again. No triage file or relaxed tolerance was used.

Python remained `ok`/`ready:true` at schema 59. Its container ID, image ID and
start time, and PostgreSQL's corresponding identity, matched their pre-shadow
baseline after removal. The shadow role had default read-only transactions and
no entries-table write privileges. Only the companion was removed; the writer,
PostgreSQL and named volumes survived. No live bank, default-engine switch,
release or deployment was exercised. The original bf16 comparison remains a
recorded difference; the fp32 control passes this synthetic corpus, not every
bank or read route.
