# Full Rust shadow on real data

**Status:** design, 2026-11-06. Research only: nothing here is built,
deployed or authorized. It answers the maintainer's question of 2026-11-06:
"I want to run the rust daemon in shadow mode as well if possible. How can
we work out the best way to test the complete rust version using real data
while the actual tool you use is the known working python version?"

The read-only Rust companion (G1, approved 2026-11-06) compares **reads** on
the live bank. It cannot test writes, dreams, sweeps, the board or the
Console, because a read-only daemon refuses all of them. This spec designs a
**mirror writer**: a Rust daemon that writes to its own copy of the bank and
replays the traffic the Python daemon really served. Python stays the only
daemon any session talks to, and nothing it does waits on Rust.

**Recommendation in one line:** a Rust mirror writer on a private bank clone
on the daemon box. It is fed first by write calls derived from consecutive
nightly backups (no Python change), then by a default-off Python request
journal with recorded extractor responses. It is checked per request against
Python's recorded answers, and nightly against the next backup, re-seeding
from that backup each night.

## Contents

1. [What the straw-man got right and wrong](#1-what-the-straw-man-got-right-and-wrong)
2. [Verified facts](#2-verified-facts)
3. [Candidate architectures](#3-candidate-architectures)
4. [Recommended design](#4-recommended-design)
5. [Phased plan](#5-phased-plan)
6. [Decisions the maintainer needs to make](#6-decisions-the-maintainer-needs-to-make)
7. [Risks and what this does not test](#7-risks-and-what-this-does-not-test)

## 1. What the straw-man got right and wrong

The conductor's straw-man (seven points), checked against master `3f328d44`:

| # | Straw-man | Verdict |
|---|---|---|
| 1 | Keep the read-only companion for continuous read comparison. | **Holds.** It is the only arm that compares reads at the same instant on the same bank; the mirror's reads run on a copy, at replay time, so the two arms are complementary. |
| 2 | A Rust writer on a cloned bank, fed by a private Python journal of accepted mutating requests, replayed in order, with a periodic state diff. | **Holds, with five corrections.** (a) Python has no single choke point: four seams are needed (§2.2). (b) Reads also write (access counts, retrieval log, slot reads), so the journal must carry reads too or the clone drifts. (c) Random ids (episodes, board agents, messages) and credentials differ per bank, so the replayer needs an id-translation table and replay-only credentials in the clone. (d) Python's background writers (session reaper, dream sweep, session-end dreams) make no request, so they need journal markers. (e) The journal must not land in `/data`, which the nightly state archive tars. |
| 3 | Re-seed nightly from the backup, bounding drift to one day. | **Holds, and doubles as the state diff:** compare the clone with the *next* backup, then re-seed from it. Aligning the journal with the dump's snapshot instant is the hard part; §4.5 does it by checking which journalled writes are present in the restored dump. |
| 4 | Record Python's extractor responses and replay them to Rust. | **Holds.** Python persists no raw extractor output today, but the Rust extractor takes any OpenAI-compatible URL and the harness already has a stub server to build on. **A gap:** both engines fire a dream at every session close whatever `memory.dream.enabled` says, so a mirror cannot hold its dreams back for the replayer to drive (§2.3, filed as an issue). |
| 5 | Disable Rust's outbound side effects on the mirror. | **Mostly already true.** The Rust daemon pushes nothing to sessions (wakes, mail and doorbells are rows that clients poll). Its only outbound calls are the extractor and judges, the release check, and model provisioning in the container entry point. Each has a switch, and an internal-only Docker network blocks all three (§2.3). |
| 6 | Test native clients and hooks separately, on one opt-in machine pointed at Python. | **Holds.** The mirror replays hook **routes** (the daemon side). The client side (the native shim, plugin hooks, launchers) is the existing CLI harness's and B11's job. |
| 7 | Exit on N consecutive clean days, or every difference classified. | **Holds** once "clean" is defined per surface (§4.8). N is the maintainer's (§6). |

## 2. Verified facts

Paths are relative to the repository root; `rust/daemon/src/` is shortened
to `rs/` and `pseudolife_memory/` to `py/`. Each line number was read at
master `3f328d44`.

### 2.1 What Python already records that a replay could use

Almost every table stores **results, not requests**. No table can replay
the day on its own, but the backups can reconstruct most of the day's
writes (Phase 1).

| Source | Holds | Usable for |
|---|---|---|
| `entries` (`py/storage/schema.py:40`) | text, source, tags, band, `ts`, `episode_id`, `dream_state`; **no writer, session or HLC column** | Re-deriving each day's `memory_store` calls from the backup delta. Deduplicated or refused stores leave no row. |
| `facts`, `world_facts`, `lessons`, `edges` (`schema.py:186`; versioning DO block `schema.py:362-376`) | the supersession chain with `tx_time`, `valid_time`, `hlc_phys`, `hlc_logical`, `writer_id`, `session_id`, `version`, `provenance` | Re-deriving `fact_set`, set add/remove and supersession in HLC order. `writer_id` separates dream writes from client writes. Compaction keeps 3 versions per slot after 30 days (`py/utils/config.py:893`), so one day's versions survive. **`facts.id` is reassigned on every per-slot write** (`py/storage/postgres.py:1858-1887`): compare by slot, never by id. |
| `outcome_signals` (`schema.py:315`) | task, outcome, about, detail, polarity, origin, episode, `used_ids` | `memory_outcome` calls, nearly verbatim. |
| `coordination_events` (`schema.py:547-563`) | a hash-chained append-only board log: event, actor, principal, agent, recipient, message id, canonical payload, HLC, and the message body in the event's body column after the live copy is pruned (`py/storage/coordination.py:3834-3846`) | Board registrations, status, sends, acks, leases and parks, in order. It is the closest thing to a request journal in the bank. |
| `retrieval_events` (`schema.py:1658`), `retrieval_uses` (`:1688`), `slot_reads` (`:1708`), `lesson_search_events` (`:342`) | the **queries** (text, session, params, served ids with score parts), not the response bodies; 365-day retention (`py/utils/config.py:875`) | The read corpus, which `shadow_compare.py` already samples (`rust/daemon/harness/shadow_compare.py:103-107`). Also the reads to replay so access counts and telemetry match. |
| `dream_runs` (`schema.py:1538`), `dream_run_slots` (`:1559`), `merge_decisions`, `curation_judgments`, `store_decisions` | cursors, tallies, which extractor side served, each claim's pre-image and new value, parsed judge verdicts | The dream-comparison oracle (what Python's dream concluded). **No raw LLM output anywhere:** no table, file or cache keeps extractor or judge responses. |
| `episodes` (`schema.py:29`), `client_sessions` (`:472`), `principals` (`:816`), `cms_counters` (`:969`) | episode tree, session registrations and closes, principals by SHA-256 token hash (`schema.py:800-812`) | Session lifecycle markers; credential rewrite targets in the clone. |
| Nightly backup (`ops/backup.sh:84`) | plain-format `pg_dump -Z9` → `data/backups/pseudolife_memory-<stamp>.sql.gz`, end-of-dump marker and per-table row-count checks, a row-count manifest, and a `/data` state tar `pseudolife_state-<stamp>.tgz`; 7-day rotation | The seed. Restorable with `gunzip -c | psql`, as `ops/restore.sh`'s rehearsal does (into `pseudolife_restore_rehearsal`, dropped afterwards). |

### 2.2 Which routes and tools mutate

The transport is a raw ASGI app (`py/web/api.py:986`), not FastAPI.

- **MCP:** 37 `@_tool` functions plus `memory_toolset` (`py/mcp_server.py:471`, `:1515`). The ones that write:
  - memory and cortex: `memory_store`, `memory_supersede`, `memory_reinstate`, `memory_reinforce`, `memory_fact_set`, `memory_set_add`, `memory_set_remove`, `memory_fact_resolve`, `memory_world_set`, `memory_forget`, `memory_consolidate`;
  - outcomes and sessions: `memory_outcome`, `memory_episode_start`, `memory_episode_end`, `memory_session_title`;
  - graph: `memory_graph_review` (its write actions), `memory_graph_relate`, `memory_graph_unrelate`, `memory_alias`, `memory_relation_define`;
  - documents and dreams: `document_ingest`, `memory_dream` (pull, commit, run, deep with apply, rollback);
  - board: `memory_agents` (update, claim, release) and `memory_message` (send, ack, receive with ack).
- **Reads that write:**
  - `memory_search` bumps access and band counters, appends `retrieval_events` and fills `served_facts`, then upserts `slot_reads` (`py/service.py:2009`, `:2194`, `:6138`, `:4792`).
  - `memory_get` bumps `access_count` and writes `retrieval_uses` (`service.py:6240`, `:6244`).
  - `memory_fact_get` bumps slot reads (`service.py:4383`), `memory_lesson_search` writes `lesson_search_events` (`service.py:5493`), and `memory_recall` writes one retrieval event per sub-search.
- **REST:** the Console table (`py/web/routes.py:75`, filled in `_register` from `:114`) has 38 POST rows, every one a mutator (facts, episodes, memory edits, dream run, 13 graph actions, curation, restores, daemon notice, config, 7 maintainer actions). `GET /api/search` and `GET /api/entry` write telemetry. Outside the table:
  - `POST /api/coordination/<action>` (15 actions) goes to `coordination.dispatch` (`api.py:1412-1424`).
  - Of the hooks, `GET /api/hook/session-start` registers and opens a session (`api.py:1049`), `POST /api/hook/session-end` closes it and may fire a dream (`:1156`), and `woke` and `subagent` write board rows (`:1259`, `:1284`).
  - `POST /api/pair` and `/api/console/*` handle credentials.
- **Choke points for a journal** (no single one, and no existing audit decorator):

  | Seam | Sees | Covers |
  |---|---|---|
  | `_async_offload._run` (`py/mcp_server.py:237-243`) | the tool name, validated kwargs and result | every MCP tool body, in the worker thread |
  | `ConsoleRoutes.dispatch` (`py/web/routes.py:87`), called from `api.py:1425-1447` | method, path, params, body and result | the Console table routes |
  | `coordination.dispatch` (`py/coordination.py:1145`) | the action, parameters and result | MCP board tools and the REST coordination routes. Journal board calls here only, never also at the tool seam. |
  | the hook handlers in `api.py` | the hook request and its result | the hook endpoints |

  Identity rides on the `Authorization`, `X-PL-Writer`, `X-PL-Session`, `X-PL-Agent` and `X-PL-Agent-Key` headers (`py/coordination.py:904-905`). The bearer and the agent key are credentials and are never journalled.
- **Writes that bypass the daemon:** the operator CLIs `invite`, `maintainer`, `import`, `test-login`, `migrate-flat` and `board-audit` connect to PostgreSQL directly (`py/invite_cli.py:366`, `py/maintainer_cli.py:66`, `py/transfer_cli.py:196`). No daemon journal sees them; the nightly re-seed absorbs them.
- **Python's autonomous writers:**
  - The dream sweep `pl-dream` (`py/mcp_server.py:3272-3321`) runs every `memory.dream.sweep_interval_seconds` (600). In order it compacts, prunes dream runs and the retrieval log, then runs the analyzer and deep-dream ticks, the five judges, the stall tick and the backlog dream (`py/memory/dream.py:2704`).
  - The session reaper `pl-session-reaper` (`py/mcp_server.py:3324-3351`) closes sessions idle for 1,800 s and fires their dream.
  - Autosave runs every 30 s (`py/mcp_server.py:3223`). The principal refresher flushes `principal_activity` about once a minute (`py/daemon.py:163-190`).
  - Board pruning and lease grants run lazily, inside calls.

### 2.3 Rust outbound side effects, and how to switch each off

The Rust daemon makes **no outbound connection to a session**. Board mail,
wakes and parks are rows plus in-process notifications that clients collect
by long-polling the daemon, capped at 30 s (`rs/board/hub.rs:1-17`). A
search of non-test source for raw sockets, `Command::new`, SMTP, webhooks,
Tailscale and telemetry found no hits. A mirror can only reach a real session if
that session connects to the mirror's port, which a loopback-only port on
an internal network prevents.

| Side effect | Where | Switch | Mirror setting |
|---|---|---|---|
| Extractor and judge HTTP calls | `rs/dream/extractor.rs:1129` (client), `POST {base}/chat/completions` (`:1533`), probes (`:2268-2285`); judges fall back to the extractor URL (`rs/dream/judge.rs:149-228`) | `PSEUDOLIFE_DREAM_BASE_URL`, `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL`, `memory.deep_dream.judge_url` / `judge_second_url` / `judge_third_url`, `memory.deep_dream.judges_enabled` | every URL points at the loopback replay server (§4.6) |
| **Session-end dream** | `Service::fire_session_dream` checks only `read_only` (`rs/service.rs:268-271`) and runs `run_auto` (`rs/dream/ticks.rs:261-285`), which has no `enabled` check (`rs/dream/run.rs:1874-1895`). It is called from the reaper, explicit close, the session-end hook and the CLI route (`rs/service.rs:1095`, `:1185`, `rs/session_close.rs:259`, `rs/routes/cli.rs:270`). | **None** short of read-only. Python is the same, deliberately (`py/memory/dream.py:2738-2752`, `py/service_dream.py:3166-3178`). With no extractor configured, the no-op extractor returns no claims (`rs/dream/extractor.rs:1812`) and the run still commits the cursor (`rs/dream/run.rs:2205-2212`), so an unconfigured mirror quietly acknowledges the backlog. | Phase 2's mirror mode stops it; until then the extractor URL is a dead loopback port, so the dream fails and the cursor stays put (`rs/dream/run.rs:2079`). |
| Automatic backlog dream, deep-dream and judge ticks | the sweep, `rs/background.rs:74-94`; the gate is `cfg.enabled` in `would_fire` (`rs/dream/service.rs:852-857`) | `memory.dream.enabled`, `memory.deep_dream.auto_tick`, `memory.deep_dream.judges_enabled` | off; the replayer drives dreams from Python's markers (§4.6) |
| Compaction and pruning | the sweep, before the `enabled` gate | `memory.compaction.enabled`; the sweep starts only if dreams or the retrieval log are on (`rs/background.rs:81`) | on, timed by Python's sweep markers once Phase 2 exists |
| Session reaper | `rs/main.rs:282`, `:344` | `PSEUDOLIFE_SESSION_REAP_SECONDS` / `PSEUDOLIFE_SESSION_IDLE_SECONDS`; not started in read-only mode | effectively off (a huge interval); Python's reaper closes are replayed |
| Daemon board notices (dream stall) | `rs/board/notice.rs:198-201` | `coordination.enabled: false` (also turns off the board), or `memory.dream.stall_notice: false` (`rs/config.rs:649`) | `stall_notice: false`; notices are rows only, so they reach no one anyway |
| Release check (GET to PyPI) | `rs/release_check.rs:11`, `:158-166` | `updates.check_releases: false` or `PSEUDOLIFE_RELEASE_CHECK=0` (`rs/config.rs:1225`) | off |
| Model download | the container entry point runs `provision models --if-missing` at every start (`ops/provision-daemon-models.sh:40-43`); the serving daemon never downloads (`rs/model_provision.rs:1`) | pre-seed the models volume, or set `PSEUDOLIFE_DAEMON_ONNX_DIR` | pre-seeded volume |
| Writer advisory lease, `writer_lease_epoch`, board lease tables | `rs/storage/mod.rs:33`, `:493-494`, `:573-587`; lease holders are filtered by `current_database()` (`:412`) | per database | harmless on a clone in its own database; dangerous only if the DSN named the live database (§4.2) |
| Files outside the data dir | none found; `config.yaml` and `graph_snapshots/` live under the data dir | none needed | the mirror's own volume |

A network declared `internal: true` blocks all three outbound HTTP paths at
once. The existing `offline` network in `ops/docker-compose.rust.yml:43-44`
is a plain bridge, which blocks nothing. There is no mirror or no-egress
mode today. Read-only mode (`PSEUDOLIFE_READ_ONLY`, `rs/config.rs:1240-1243`;
the forced settings at `rs/service.rs:184-196`) refuses every write, so it
cannot be the mirror.

### 2.4 Can the existing differential harnesses run on a restored real bank?

**Only `shadow_compare.py` can, and it only reads.** It takes any two base
URLs and checks that the Rust side reports `read_only: true` (`rust/daemon/harness/shadow_compare.py:271-272`).
Every other harness creates, seeds and drops its own disposable databases:

- `pgdisposable.py` admits only names matching `pl_cf_<slice>_...` and refuses the production name (`rust/daemon/harness/pgdisposable.py:132-138`). It clones with `CREATE DATABASE ... TEMPLATE`, which needs a source with no connections. Its credentials come from `~/.pseudolife-mcp/test-pg.env` against `127.0.0.1:5433`, the bench port. On the daemon box, `127.0.0.1:5433` is the **live** bank's published port (`ops/docker-compose.yml:70`).
- `w2e.py` seeds each scenario's template by driving a Python daemon (`rust/daemon/harness/w2e.py:6247-6275`), clones `_py` and `_rs`, and diffs both banks after every step. `w2d_search.py` and `board/run.py` seed scripted banks the same way.

The **parts carry over unchanged**:

- `dbstate.dump` reads every table in one read-only REPEATABLE READ snapshot, with every column, and catalog sets (`rust/daemon/harness/dbstate.py:141-199`).
- `dbstate.normalize` provides the null-or-not clock rule and the rename-to-token rule (`:202-236`).
- `w2e.py`'s rule tables (`CLOCK`, `ORDERED_CLOCK`, `RECENT_CLOCK`, `META_CLOCK`, response clocks and `SCORE_TOLERANCE`, `w2e.py:233-327`) are reusable.
- So are the episode-id renaming (`w2e.py:5385`), the board harness's ordinal renaming of tokens, hex ids, UUIDs and HLCs (`rust/daemon/harness/board/norm.py:14-40`), and the stub extractor (`rust/daemon/harness/stub_extractor.py:150`), which answers `/v1`, `/fallback/v1`, `/second/v1`, `/third/v1` and `/health` from a declared table keyed by request-body SHA-256 and returns 500 when nothing matches.

**What blocks reuse as written:**

- The fixed seed and template steps.
- The admitted-name pattern.
- The bench DSN.
- `dbstate.UNPORTED_ROWS` (`dbstate.py:33-46`), whose rows are deleted from clones.

None needs a rewrite: Phase 1 adds a seed step that restores a backup under
an admitted mirror name on the mirror's own PostgreSQL.

The **nightly body-evidence job** (`.github/workflows/rust.yml:10-11`, `:1756-1827`; `rust/ci/body_evidence.py:21`) runs `w2e.py`, `w2d_search.py` and `board/run.py` on synthetic banks on a self-hosted Linux runner. It publishes counts, not data. It is the model for the mirror's report, and it must never see real data.

### 2.5 Nondeterminism a state diff must normalize

| Source | Python | Rust | Normalization |
|---|---|---|---|
| Entry, lesson and world ids | `BIGSERIAL` | `INSERT ... RETURNING id` (`rs/store.rs:474-495`) | Equal when the clone's sequences match and inserts arrive in the same order. Concurrent Python requests can commit out of journal order, so map ids through the responses (§4.4). |
| Fact ids | reassigned on every per-slot write (`py/storage/postgres.py:1858`) | the same | compare by slot `(entity, attribute)` and version |
| Episode and session ids | `uuid4().hex` (`py/memory/episodes.py:112`, `:149`) | uuid v4 (`rs/episodes.rs:174`, `:246`) | a translation table from paired responses; `w2e.py`'s `<ep#i>` renaming for the diff |
| Board agent ids, credentials, message ids, body salts | `uuid4`, `token_urlsafe(32)` (`py/storage/coordination.py:1643`, `:3806`, `:3838`) | uuid v4, `getrandom` (`rs/board/store.rs:309-322`) | translation table; ordinal renaming (`board/norm.py`). The hash chain is checked per side, never compared across sides. |
| Dream entry tokens, the commit-signing secret, Console sessions, maintainer nonces, pairing codes | `secrets` (`py/memory/cms.py:2425`, `py/storage/postgres.py:1513-1520`) | `getrandom` (`rs/dream/cursors.rs:235`, `rs/console_sessions.rs:351`, `rs/maintainer_ops.rs:400`) | compare the decoded payload without its signature (as `w2e.py` does for `commit_token`); exclude the secrets |
| Wall-clock times (`ts`, `tx_time`, `created_at`, ...) | `time.time()`; no public path takes a time (`py/service.py:4045` has `now=`, which no caller passes) | `SystemTime::now()` in `rs/episodes.rs:29-34` and some ten other modules; no seam outside unit tests | `w2e.CLOCK`'s null-or-not rule for new rows; ordered tokens where order matters. A continuous replay keeps Rust's clock within seconds of Python's; a batch replay does not (§3). |
| HLC | seeded from the largest stored stamp at start (`py/service.py:1755-1782`) | `Ready::tick_hlc`, wall-clock based | compare the **order** of stamps within a slot, never their values |
| Embedding precision | `embedding.cpu_dtype: auto` uses **bf16** on CPUs with native bf16, else fp32 (`py/memory/embedding.py:43-91`); measured bf16 against fp32: top-8 overlap 0.994, max score delta 0.0058 (`py/utils/config.py:55-68`, figures at `:65-66`) | fp32 only; bf16 is refused at start (`rs/embed.rs:100-115`); against Python fp32: max abs 5.886e-7, min cosine 0.99999999999 (`rust/CUTOVER.md:60`) | With Python on fp32, `w2e.py`'s tolerance file (max abs 1e-5, min cosine 0.9999999) holds. With Python on bf16, vectors differ about 10⁴ times more, and so can the store-time decisions that depend on them (deduplication, surprise, band routing). Then a declared tolerance is not enough: the diff must class those decisions as precision-caused (decision Q6). |
| LLM output | temperature 0, but not guaranteed identical between calls (`py/memory/dream.py:1258`) | the same client contract | replaying recorded responses removes it (§4.6) |
| Tables written on each engine's own timer | `cms_counters`, `principal_activity`, `rejected_bearers` | the same | already excluded from row diffs (`dbstate.UNPORTED_ROWS`); compared at fixed points |

### 2.6 Can the daemon box host it?

The maintainer's brief gives the box as 8 cores, 12 GB RAM and about 30 GB
free disk. This design did not log in to measure it.

- **Live Python daemon:** capped at 6 GB with no swap (`ops/docker-compose.yml:143-144`). Measured at 3.1-3.3 GiB anon at rest and 4.48 GB later on an fp32 embedder in 2026-09; bf16 lowers the steady state by about 1.4 GB (`ops/docker-compose.yml:117-133`). PostgreSQL has no cap.
- **Rust daemon:** no RSS figure is documented for the serving daemon. Its floor is the fp32 Qwen3 export: about 2.38 GB of external weights (`rs/model_provision.rs:61-80`), plus the 91 MB reranker loaded at start whatever the config says, plus ONNX Runtime. The shadow overlay caps it at 6 GB (`ops/docker-compose.rust-shadow.yml:18-19`). The companion's `/health` memory block will give the first real number this week.
- **Arithmetic, as an estimate:** live Python about 4.5 GB, plus PostgreSQL and the OS about 1.5 GB, leaves about 6 GB. That holds one Rust daemon of about 3 GB with margin. It does not reliably hold two (companion and mirror) plus a second PostgreSQL, and it **cannot** hold a second Python daemon as a control arm. Containers' caps are per container; if their sum exceeds RAM, a host-level OOM can still kill the live daemon.
- **Disk:** the mirror needs the clone, a scratch restore of the next backup for the diff, the journal and a models volume (about 2.5 GB, or shared with the companion's). The bank's dump size was not measured here; Phase 1 records it.

So: **one Rust writer fits on the box; the companion and the mirror together
fit only if measured RSS allows, and a Python control arm does not fit.**

## 3. Candidate architectures

| | **A. Journal replay to a continuous mirror** | **B. Tee proxy** | **C. Nightly record-and-replay on paired clones** |
|---|---|---|---|
| How | Python appends accepted requests (with results) to a private journal, off its request path. A replayer tails it into a Rust writer on a clone, seconds behind. Nightly: diff the clone with the next backup, then re-seed from it. | A reverse proxy in front of Python's port forwards each request to Python and sends a copy to the Rust mirror, recording both answers. | Nightly: restore backup N twice, start a Python daemon and a Rust daemon on the two copies, replay the day's recorded traffic into both, and diff them with each other. |
| Risk to the working Python tool | Low. A default-off journal; when on, one boolean check plus a non-blocking enqueue per call, and a bounded queue that drops (and marks a gap) rather than blocks. Python never connects to Rust. | **High.** The proxy sits in every session's path: MCP streaming sessions, 30-second board long-polls, hook timeouts and Host/Origin checks. Its outage is Python's outage, which breaks "Python never depends on Rust". | None at request time (it uses the same journal), but a second Python daemon on the box competes for memory with the live one. |
| Fidelity | High for writes, reads and the board. Rust's clock stays within seconds of Python's, so idle timers, lease TTLs, message expiry and recency behave as they did live. It also exercises Rust as a long-running process (memory growth, reconnects). The weak points are concurrency reordering and the backup alignment window (§4.5). | Highest arrival fidelity (real concurrency), but the mirror's state still drifts after the first divergence unless it is re-seeded, and the proxy must not forward credentials Rust cannot validate. | The best **differential**, because both engines see identical input and identical compressed time, which gives a true control arm. The worst **behavioural** fidelity: time is compressed, so leases expire, sessions reap and recency ranks differently than they did live. |
| Build cost | About 8 PRs over Phases 1-5 (§5): roughly 2,500-4,000 lines, mostly harness. | About 3-4 PRs plus ops changes on the live path; the Python change is zero, but the live compose and port mapping change. | About 6-7 PRs (shares the journal and replay server with A). |
| Privacy | Everything stays on the daemon box. | On the box. | Needs a second Python daemon, so in practice a dev box: real bank data leaves the daemon box (decision Q1). |
| Resources | One Rust daemon, one small PostgreSQL, 2-3× the bank size on disk plus the journal. | One Rust daemon plus the proxy. | A Python and a Rust daemon for about an hour a night, plus three restored copies. |

**Recommendation: A**, with C's control arm run occasionally to calibrate
the replay itself (§4.8). B is rejected on risk: it is the only design that
puts new code in front of every session. A gives the whole-daemon coverage
the maintainer asked for, keeps every byte of real data on the box, and fits
the box's memory. Phase 1 below delivers part of A's value with **no Python
change at all**, by deriving the day's writes from consecutive backups.

## 4. Recommended design

### 4.1 Shape

```
  sessions ──► Python daemon (8765, live writer) ──► live bank
                    │  (default-off, async)              │ nightly pg_dump
                    ▼                                    ▼
             journal volume                       data/backups/*.sql.gz
                    │                                    │
                    ▼                          seed ┌────┴────┐ diff target
              replayer ──── loopback ───► Rust mirror (writer)   │
                    │                     on shadow PG, clone ◄──┘
                    ▼                           │
           response diffs            replay server (recorded
           + nightly state diff      extractor/judge answers)
           (private report)
  Read-only companion (8766) compares reads on the live bank, separately.
```

### 4.2 Isolation

- **Containers:** the mirror runs in its own Compose project with two containers: a PostgreSQL 18 with its own volume, and the Rust daemon. Both join a network with `internal: true`, so there is no route out. Memory caps are chosen so that the live cap, the companion's cap and the mirror's caps sum to less than RAM minus 1 GB.
- **DSN:** the mirror's DSN names only its own PostgreSQL. The mirror's PostgreSQL holds no role on the live cluster, and the live cluster holds no role for the mirror. A startup check refuses a DSN whose host is the live PostgreSQL service or the live port.
- **Ports:** the mirror publishes only loopback ports, on a third port (proposed 8767), and the replayer is its only client.
- **Credentials:** after restore, the seed step replaces every `principals.token_hash` and `coordination_agents.credential_hash` in the clone with the SHA-256 of a replay-only token derived from a mirror secret. Both are SHA-256 hex schemes (`py/storage/schema.py:800-805`, `:493`). The replayer can then act as each original principal and agent, and no live credential works on the mirror. These columns are excluded from the diff.
- **Kill switch:** stopping the mirror's project stops everything. Python's journal has its own switch (§4.3), and nothing in Python reads anything the mirror writes.

### 4.3 The Python journal (Phase 3)

- **Off by default.** It is enabled by one environment variable (proposed `PSEUDOLIFE_SHADOW_JOURNAL=1`). When unset there is no queue and no thread, only one boolean check per call.
- **When on:**
  - each seam (§2.2) enqueues one record after the call returns, with `put_nowait` on a bounded queue;
  - a daemon thread serializes records to newline-delimited JSON files on a **separate volume** (`/shadow-journal`, not under `/data`, so backups do not carry it);
  - a full queue, a write error or free disk below a floor drops the record and writes one `gap` marker;
  - the journal never raises into the request path and never retries.
- **A record holds:**
  - a monotonic sequence number and the enqueue time;
  - the seam, the tool, route or action, and the validated arguments or body;
  - Python's result, so the replay can compare answers;
  - the principal name, `X-PL-Writer`, `X-PL-Session` and `X-PL-Agent` (an agent **id**, not its key).
- **Never recorded:** the `Authorization` header, `X-PL-Agent-Key`, pairing codes, and the bodies of `/api/pair`, `/api/console/*`, maintainer signed actions and `POST /api/config`. Each of those leaves a marker naming the route and time, with no body; the nightly re-seed brings their effects in.
- **Background markers:** the dream sweep tick (and which stages ran), each `dream_run_auto` start and end with its trigger, cursor and pulled entry ids, each reaper close, and each extractor or judge exchange (request body and response content, §4.6). These are the only changes in Python's background code, behind the same switch.
- **Reads are journalled too:** search, get, fact_get, lesson_search and recall all write state (§2.2). The record carries the query and the served ids, not full response bodies, which keeps the journal small and holds less copied data.
- **Under D10:** this adds no tool, route, hook or CLI mode, but it is new behaviour in the live daemon. Decision Q3 asks the maintainer to confirm that D10 allows it.

### 4.4 The replayer

- **Order:** it replays records in sequence order, one at a time, as fast as the mirror answers, with an optional floor that keeps the original spacing for timing-sensitive families (leases, parks, idle closes).
- **Translation table:** Python and Rust mint different random ids, so the replayer maps each Python id in a response (entry id, episode id, agent id, message id, lease token) to the Rust id in the mirror's response. Later calls are rewritten through the map before they are sent.
- **Per-request comparison:** it compares Rust's response with Python's recorded one using `w2e.py`'s response normalizers. Each difference is recorded with the record's sequence number, and the replay carries on, because the next nightly re-seed resets the drift.
- **Autonomous events:** it drives these from Python's markers. A reaper close becomes the same close on the mirror, a dream start becomes an explicit dream run at the same point, and a sweep tick becomes a mirror sweep (Phase 2's mirror-only control). Rust runs none of them on its own timers.
- **Gap markers:** a gap marker stops the replay until the next re-seed and is reported as such, never as a pass.

### 4.5 Seeding, aligning and the nightly state diff

1. Restore backup N into the mirror PostgreSQL under an admitted name (a new `pgdisposable` slice). Rewrite the credentials. Start the mirror.
2. **Align:** every journalled write whose response carries a durable key is checked against the restored seed. The keys are an entry id, a coordination event sequence, a slot version with its HLC, or an outcome signal id. Replay starts after the longest journal prefix whose keyed writes are all present, provided none after it are. Unkeyed records inside the bracket (reads, telemetry) are replayed anyway and their differences are labelled `seed-window`. This needs no change to `ops/backup.sh`.
3. Replay continuously until backup N+1 lands. Pause at the aligned offset for N+1, using the same presence test.
4. **Diff:**
   - restore N+1 into a scratch database;
   - dump both with `dbstate.dump`;
   - normalize with the §2.5 rules;
   - write a family-by-family report: entries, cortex slots and history, set members, contenders, episodes and sessions, outcomes and lessons, world facts, graph, dream state, board;
   - drop the scratch database.
5. Re-seed from N+1 and continue. Drift is bounded to one day, and every day starts from Python's real state.

### 4.6 Dreams and deep dreams

- **Recording:** Python records each extractor and judge exchange at the HTTP layer of `OpenAICompatExtractor` (`py/memory/dream.py:1231`; the claims call at `py/service_dream.py:1704-1706`, the judge calls through `_judge_request`, `py/memory/dream.py:1752`). It records the request body and the response content into the journal.
- **Replay server:** a loopback server extends `stub_extractor.py`. It answers chat completions from the recordings: by exact body hash first, then by (prompt kind, user content). It also answers the `/health` and `/models` probes. Every URL in the mirror's extractor and judge settings points at it.
- **Outcomes:**
  - An exact-hash hit means Rust built the same prompt from the same state. That is the strongest dream-parity signal available, and it is already tested on synthetic banks: the harness compares both daemons' request bytes.
  - A hit found only by content is a prompt-presentation difference.
  - A miss is answered with HTTP 500. The Rust dream then fails without advancing its cursor (`rs/dream/run.rs:2079`), and the report shows the nearest recorded prompt beside Rust's.
- **Comparison:** for each Python dream marker the report compares the pulled entry ids, the claims, the `dream_run_slots` outcomes, the contenders and the cursor advance. A mismatch in pulled ids is classed as a race when a client store arrived during Python's dream; otherwise it is a difference.
- **No second LLM bill and no live second extractor.** That choice is decision Q5.
- **Deep dreams:** the same path covers the manual graph pass and the judges, since their calls are recorded the same way. Option B's deterministic graph-insight algorithm (CUTOVER 2026-10-19) is compared structurally, as its declared divergence requires, never row by row.

### 4.7 Console, hooks and board

- **Console:** the Console's writes go through the table routes and are journalled and replayed like MCP calls. Console sign-in (CUTOVER blocker 8) and credential routes are markers only, so the mirror cannot test sign-in. That stays with B5's own differential.
- **Hooks:** the session-start, session-end, woke and subagent hooks are replayed as routes; the client side of hooks is B11's.
- **Board:** registrations, status, mail, acks, leases, parks and wake decisions are replayed through `coordination.dispatch`'s records. The mirror's wake decisions are compared as rows (`coordination_wakes`), and they ring nothing, because nothing listens on the mirror. Each side's hash chain is verified with Python's verifier, as `board/run.py` does, and never compared across sides.

### 4.8 What "clean" means, and calibration

- **A clean day:**
  - no untriaged per-request difference;
  - no untriaged state-diff row;
  - no gap marker;
  - an exact-hash hit rate on recorded prompts at or above a stated threshold;
  - no mirror restart, OOM or `not_ready` episode.
- **Classifying a difference:** each one is classed the way `shadow_compare.py` already does: `known_declared_divergence` (it must name a file under `rust/daemon/divergences/`), `new_bug` (a fix PR or issue) or `comparator_noise`. Two classes are added: `seed-window` and `race`.
- **Calibrating with a control arm:** once, and after any change to the replayer, replay one day's journal into a **Python** mirror on a clone of the same seed, in a window when the Rust mirror is stopped and the box has room (or on a dev box, if Q1 allows). Python-against-Python differences are the replay's own noise floor. A class of difference that shows up there cannot count against Rust.
- **Privacy of reports:** reports hold bank text. They stay in the mirror's private directory on the box, like the companion's (`docs/dev/run-rust-beside-python.md:111`). Only counts and classes are published, never row content.

## 5. Phased plan

Each phase is small PRs, each with its own review, and none changes what a
session talks to. Phase 1 needs no Python change; Python's hot path is first
touched in Phase 3, behind a default-off switch.

| Phase | PRs (rough size) | Delivers | Touches Python? |
|---|---|---|---|
| **1. Backup-delta replay (week 1)** | **1a** (about 400 lines): mirror Compose overlay (own PostgreSQL, Rust writer, `internal: true` network, caps, pre-seeded models, port 8767) and `harness/mirror/seed.py` (restore a backup under an admitted slice name, rewrite credentials, health gate). **1b** (about 600 lines): `harness/mirror/derive.py`, which turns the difference between backups N and N+1 into ordered calls (stores from new `entries`; fact, set and supersession writes in HLC order from `facts`/`world_facts` history with client `writer_id`; outcomes from `outcome_signals`; board actions from `coordination_events`; episode starts and ends from `episodes`/`client_sessions`), plus `replay.py` and `diff.py`, which run the calls into the mirror and compare it with N+1 for those families only (dream-written rows and telemetry excluded). | The first Rust writes on real data, with a family-by-family state diff on the box, and measured numbers: Rust RSS under load, dump and restore size and time, daily write counts per family. Deduplicated and refused stores, reads and dreams are not covered yet. | **No.** It reads backups only, and the mirror's extractor URL is a dead loopback port. |
| **2. Rust mirror mode** | **2a** (about 300 lines, Rust): `PSEUDOLIFE_MIRROR=1`, a Rust-only mode like `PSEUDOLIFE_READ_ONLY`. It is a writer that starts no reaper, dream, deep-dream or judge tick, fires no session-end dream, sends no stall notice and runs no release check. It refuses any non-loopback extractor or judge URL, reports `mirror: true` in `/health`, and serves a loopback-only, mirror-only control route that runs one sweep or one dream on request. It closes the filed session-end-dream issue for the mirror case. **2b** (about 150 lines): the guard that refuses a mirror DSN pointing at the live cluster. | A mirror whose every autonomous action is driven by the replayer, so its dream cursor never moves on its own. | No |
| **3. Python journal** | **3a** (about 350 lines with tests): the four seams, the bounded queue, the writer thread, scrubbing, gap markers and the background markers, behind `PSEUDOLIFE_SHADOW_JOURNAL`; a separate journal volume in the compose file; an overhead test (switch off: one branch; switch on: one enqueue) and a test that a full queue or a failing disk never fails a request. | A complete record of what Python served, ready to replay, while switched off by default. Switching it on in the live deploy is the maintainer's click (Q3). | **Yes**, behind a switch, off by default. |
| **4. Continuous replayer** | **4a** (about 700 lines): tailing, the translation table, per-request comparison with `w2e.py`'s normalizers, marker-driven reaps and sweeps, prefix alignment and nightly re-seed with diff. **4b** (about 200 lines): the daily report and a private ledger (one line per day, totals by class), following the body-evidence report's shape. | Whole-daemon coverage of writes, reads, the board, the Console and the hook routes on live traffic, per request and per day. | No |
| **5. Dream replay** | **5a** (about 300 lines): extractor and judge recording in Python (part of the journal, same switch). **5b** (about 400 lines): the replay-server mode of `stub_extractor.py` and the per-dream comparison. | Dreams, deep dreams and judges compared on real data with no second LLM bill. | Yes: recording only, behind the same switch. |
| **6. Calibration and exit** | One control-arm run (Python mirror, §4.8) and the exit ledger. | The replay's noise floor, and the evidence the maintainer reads before deciding on the G2+ soak. | No |

Native clients and hooks on an opt-in machine pointed at Python (straw-man
6) stay with B11 and the CLI harness and are not part of this plan.

**First week's success criteria:**

- Phase 1 has run for at least two nights with exit status recorded.
- Each report names the two backups, the Rust build and the diff counts per family.
- The measured Rust RSS and bank size are recorded.
- Python's container identity and `/health` are unchanged, checked the same way the companion's rehearsal checks them.

## 6. Decisions the maintainer needs to make

1. **Q1. Where the mirror and its clones live.** On the daemon box (recommended: the backups are already there and nothing leaves it), or on a Linux dev box? A dev box means copying a backup of the real bank, and in later phases the journal, off the daemon box. That is a privacy decision. It also breaks the rule that dev machines test only against their own test PostgreSQL, which would need an explicit exception. The control arm (§4.8) needs about 4 GB that the box may not have: allow it on a dev box, or run it on the box in a window with the companion and mirror stopped?
2. **Q2. The box's memory budget.** Run the companion and the mirror at the same time (only if their measured RSS, plus the live cap, PostgreSQL and a 1 GB margin, fits in 12 GB), or alternate them, with the mirror taking the companion's slot once read comparisons have converged? What caps should each get?
3. **Q3. The Python journal.** Does D10's soft freeze allow a default-off internal journal in the live daemon (no new tool, route, hook or CLI mode)? If so, approve switching it on in the live deploy after Phase 3 merges.
4. **Q4. Journal retention.** It holds the same text as the bank. Proposed: 8 days (one more than the backup rotation), on its own volume, excluded from backups and their off-disk mirror, deleted by the replayer after the nightly diff that covers it. A different window, or included in backups?
5. **Q5. Dreams.** Replay Python's recorded extractor and judge responses (recommended: deterministic and no extra cost), or run a live second extractor for the mirror (a second bill, and its non-identical outputs make the comparison noisy)?
6. **Q6. Precision.** If the live Python daemon runs bf16 (`embedding.cpu_dtype: auto` on a bf16 CPU), then store-time decisions can differ for precision reasons alone. Pin live Python to fp32 (about 1.4 GB more memory, which affects Q2), or accept a declared bf16 class in the mirror's diff, as the companion does for scores?
7. **Q7. Rust mirror mode.** Accept `PSEUDOLIFE_MIRROR=1` and its mirror-only control route as a declared Rust-only surface, like `PSEUDOLIFE_READ_ONLY`, refused outside mirror mode?
8. **Q8. Exit criteria.** How many consecutive clean days (proposed: 14, with every class of difference closed by a fix or a divergence file), and what minimum prompt hit rate, before you consider the G2+ soak with Rust as the live writer?

## 7. Risks and what this does not test

- **Live load.**
  - The nightly restores and diffs run on the box. They are timed after the backup, CPU-capped (ONNX threads and PostgreSQL workers), and checked against the full-suite lease where the box also runs suites.
  - If the box runs out of memory, the shadow's containers are the ones to go, because the live daemon's cap is kept and the shadow's caps are sized below the remainder.
  - Free disk is checked before each restore against the 8 GB floor.
- **Concurrency.** The replay is sequential, while Python serves requests concurrently. Order-dependent differences (two writes to one slot) are checked against HLC order before they are called bugs.
- **Writes that bypass the daemon** (operator CLIs) reach the mirror only through the nightly re-seed.
- **What the mirror cannot test:**
  - Console sign-in, pairing and maintainer signatures (credential markers only);
  - the native shim, plugin hooks and launchers (B11);
  - installed execution after a restart (B11's third proof);
  - real concurrency, which only the G2+ soak exercises.
- **The read-only companion's limits stay its own.** The mirror's reads happen on a copy at replay time, so they do not replace the companion's same-instant comparison.
