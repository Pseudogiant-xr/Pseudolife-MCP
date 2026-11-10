# Rust mirror: a private copy of the bank

The mirror is a Rust writer on its own copy of the bank, on the daemon host.
Nothing in the live stack changes, and no session talks to it. The design is
`docs/specs/2026-11-06-full-rust-shadow.md` (§4.2); the Compose project is
`ops/docker-compose.rust-mirror.yml`.

Run everything from a clone that is **not** the live stack's checkout, as
root on the Docker host. Keep these files private (mode 600), outside any
tracked tree:

- `mirror.env` holds `PSEUDOLIFE_MIRROR_PG_PASSWORD` (a new random value),
  `PSEUDOLIFE_MIRROR_BACKUPS` (the live `data/backups` directory, mounted
  read-only) and `PSEUDOLIFE_MIRROR_DATA` (a directory owned by uid 10001).
  It also carries the live writer's `TZ` and `PSEUDOLIFE_WRITER_ID` values.
- `secret` holds at least 32 random characters. Every replay-only
  credential derives from it.

Never copy the live `ops/.env`, and never pass a live token to the mirror.

The mirror builds nothing. It runs the images that are already present:
the live stack's `pseudolife-pg:18`, and the read-only companion's Rust
image and provisioned model volume. Start the companion once first (see
`docs/dev/run-rust-beside-python.md`).

```sh
C="docker compose -p pseudolife-mirror --env-file mirror.env -f ops/docker-compose.rust-mirror.yml"
$C up -d mirror-pg
python3 rust/daemon/harness/mirror/seed.py seed --backups <live>/data/backups --secret-file secret --record seed.json
python3 rust/daemon/harness/mirror/seed.py tokens --secret-file secret --out tokens.env
python3 rust/daemon/harness/mirror/seed.py config --backups <live>/data/backups \
    --backup <the seeded backup> --out <PSEUDOLIFE_MIRROR_DATA>/config.yaml   # needs PyYAML
python3 rust/daemon/harness/mirror/seed.py quiet --record quiet.json  # the forced run's quiet base
python3 rust/daemon/harness/mirror/seed.py curation --record curation.json  # and its curation base
$C --env-file tokens.env up -d mirror-daemon
python3 rust/daemon/harness/mirror/seed.py health --schema <schema_version from seed.json>
```

- **Seeding:** `seed` restores into `pl_cf_mir_seed` and rewrites its
  credentials. It then clones the result as `pl_cf_mir_bank`, the daemon's
  database. Harnesses may clone `pl_cf_mir_seed` while it is idle.
- **Reseeding:** stop `mirror-daemon` first; `seed` refuses while it runs.
  `seed` drops the quiet and curation copies and stamps the new seed with
  its backup, so run `seed.py quiet` and `seed.py curation` again after it;
  the forced run refuses a copy made from another seed.
- **Copies for queue-driven scenarios:** `forced.py` documents why each
  scenario prefix runs where it does (`QUIET_PREFIXES`,
  `CURATION_PREFIXES`).
  - `pl_cf_mir_seed_quiet` has the real dream backlog acknowledged and the
    pending proposals removed, for the dream, judge and analyzer scenarios.
  - `pl_cf_mir_seed_curation` is the same, plus every current lesson and
    world fact superseded, for the curation judge's scenarios. That judge
    ranks pairs over all current records, so the real ones crowded out the
    scenario's own.
- **Disk:** the 8 GiB floor is measured inside the mirror PostgreSQL's
  container, on its data directory, which is the storage a restore fills.
- **Refusals:** every refusal exits 3 and says what to fix. Nothing has
  changed when it does.
- **Stopping:** `$C stop` stops the mirror. Never run `down -v` here or on
  the live project.

## Forced real-data run

`forced.py` runs every live Python-vs-Rust differential on a copy of the
seeded bank: w2e, w2d_search, board and the route harness (`run.py`). Both
daemons come from one release. `ops/Dockerfile.mirror-runner` combines the
release's Python image, run from the mounted checkout, with the release's
Rust binary.

- **Base bank:** every disposable database a harness creates copies
  `pl_cf_mir_seed` (`PL_HARNESS_BASE_DATABASE`). Each scenario then seeds
  and runs on top of real data.
- **Dumps:** a dump holds only the rows that differ from the base
  (`dbstate`). A whole 284 MB bank per step took the harness past 9.6 GiB.
- **Model output:** any prompt no stub table answers gets
  `PL_HARNESS_STUB_DEFAULT_CONTENT`, so both daemons see the same model
  output while their request bytes are still compared.

A scenario that differs runs again on the real bank, and then on the
harness's synthetic bank. Its class follows from those runs:

| Class | What the reruns showed |
|---|---|
| `transient` | The second real-bank run was clean |
| `preexisting` | It also differs on the synthetic bank |
| `new` | It differs on real data only |
| `accepted` | A difference accepted with its reason (`accepted_as` on the row): the OOM limit and the community partition by the maintainer, the tie-order, padded-query and composition rules by the conductor under the maintainer's delegation of 2026-11-11; each reason in `forced.py` says which. both real-bank attempts saw the runner's OOM counter rise in a scenario listed in `ACCEPTED_OOM` (`"oom"`), or every difference place of both real attempts is in the community tables or a community field, with meta's `graph_digest` row (on a line of its own, as dbstate prints it) allowed beside them (`"community-partition"`, `ACCEPTED_DIVERGENCES`, `COMMUNITY_META_KEYS`; the digest alone, or one place anywhere else, another meta row included, keeps it `new`). The same holds for an observed bank's response-side rows (`.json.db.rows.public.…`): `communities`' id, label, size and cohesion, `entity_communities`' community_id (`RESPONSE_PARTITION_COLUMNS`), either table's row-count, whole-row (`rows[i]`) or `base_missing` line (as on the bank side, a whole-table or whole-row place, so it covers every column when the row counts differ), and the `graph_digest` value named by its key (`meta.by_key`). With equal row counts their `computed_at` and `entity_id` cells stay a difference, as do another meta key or one that only starts like it, a meta value named only by its index, and any other table's rows or `base_missing`. Or every difference of both real attempts is a tie the harness proved beside its lines (`tie_proofs.py`; `"tie-order"`, `TIE_RULES`, each rule on its own): a different entry at one position whose two texts have equal recorded md5s, changing only its id and a served item's surprise or a response entry's access_count, surprise_score and timestamp; two adjacent positions of a retrieval event's served list exchanging two ids, neither contiguity nor timeline, each entry's surprise crossing with it, and the scores near (the two entries within twice `EVENT_SCORE_TOL` on each side and each entry within it across the engines, for the total and each recorded component); or an access_count difference of exactly the extra serves of an entry an accepted pick served on one side earlier in the scenario. Ruling E (`"padded-query"`, its own rule in `TIE_RULES`): a w2d_search case whose own query ends in whitespace (`tie_proofs.padded`; the marker is the cause, so it marks every such case: today `default`'s "padded query (cortex sees the raw q)", `mcp-search`'s "padded query", `mcp-search-explain`'s "trace padded query" and `trace`'s "trace padded q") may differ in its response's cortex block when the harness's maps cover both blocks and every slot both sides served hashes equal (the whole item less `score` and the clock-derived `age`); in the `served_facts` of the event logged with its query when the maps cover both lists and every slot both sides served has the same kind, contested and pinned hash; and in `slot_reads` (rows, base_missing count and digest) when the harness's full comparison of both tables, not the capped printout, differs only in slots one side alone served, each by exactly its one-sided serves. That holds because sentence-transformers 5.4.0 stopped stripping text before tokenising and Rust keeps the 5.3.0 strip. Ruling F: the rules compose; an attempt is accepted when every line is covered by one of them, the community lines passing the community rule unchanged (at least one partition line) and every other line a position a tie or padded-query rule proves. `accepted_as` names every divergence used (`community-partition`, `tie-order`, `padded-query`), joined by `+`. Any line no rule covers, or an errored, cut or incomplete real attempt, keeps it `new`. The summary lists both as `accepted_limits` and `accepted_divergences` |
| `excluded` | Its real-bank arms do not run, because its premise cannot hold on a real bank (`REAL_BANK_EXCLUDED` in `forced.py`, with each reason; the summary lists them as `real_bank_exclusions`); its synthetic arm ran and passed |

Every harness caps the DIFF lines it prints, so the places come from each
attempt's `--out` JSON, rebuilt in the printed format with no cap. An
attempt without a complete one, or whose JSON does not match its printed
lines, keeps its printed lines with `diffs_complete: false` (and
`diffs_note` saying why). A scenario's real attempts are then never
`preexisting` or community-`accepted`.

`summary.json` holds counts and names only. `private.json` quotes bank text
and stays on the host.

Run it on the mirror's network with a cap. Stop the mirror daemon first, to
free its memory for the run:

```sh
docker build -t pseudolife-mirror-runner -f ops/Dockerfile.mirror-runner ops
docker run -d --network pseudolife-mirror_mirror --memory 10g --memory-swap 10g \
  --oom-score-adj 1000 --cpus 4 --cpu-shares 256 \
  -v "$PWD":/repo -v <out>:/out -v <control>:/control:ro \
  -v <test-pg.env>:/run/mirror/test-pg.env:ro \
  -v pseudolife-mcp_pseudolife_shadow_models:/models:ro \
  -e PSEUDOLIFE_TEST_PG_HOST_PORT=mirror-pg:5432 \
  -e PSEUDOLIFE_TEST_PG_LOGIN_FILE=/run/mirror/test-pg.env \
  -e PL_HARNESS_BASE_DATABASE=pl_cf_mir_seed -e PL_HARNESS_STUB_DEFAULT_CONTENT='[]' \
  -e PSEUDOLIFE_DAEMON_ONNX_DIR=/models/<bundle>/embedding \
  -e PSEUDOLIFE_DAEMON_RERANK_DIR=/models/<bundle>/reranker \
  pseudolife-mirror-runner \
  python rust/daemon/harness/mirror/forced.py --rust-bin /usr/local/bin/pseudolife-daemon --out /out/forced
```

- **`test-pg.env`:** the mirror's own login, as `PSEUDOLIFE_TEST_PG_USER`
  and `PSEUDOLIFE_TEST_PG_PASSWORD`.
- **Stopping:** `touch <control>/STOP` stops the run before its next
  scenario. Below 8 GiB free disk it stops by itself.
- **Limit:** a checkout whose schema is newer than the backup migrates each
  template, which changes every migrated table's row text, so those tables
  dump whole again. Compare same-schema pairs.
- **Not forced:** `dream_cursors.py` admits only the bench PostgreSQL;
  the CLI harness drives a fixture daemon or synthetic databases; golden
  and mutant modes replay recordings of synthetic banks.
