# Design: the lite storage tier in the Rust daemon (B9)

The Rust daemon refuses to start without `PSEUDOLIFE_MCP_DATABASE_URL`, but
the README Quickstart installs the lite tier, where Python starts an embedded
PostgreSQL itself. Maintainer decision D1 (2026-10-23) ports the lite tier and
retires the in-process `embedded` mode and `.pt` file mode. This note
re-verifies the two upstreams D1 names, compares them with how Python lite
works today, picks one, and lists what the maintainer still has to decide.

Upstream state was read on 2026-10-25 through the GitHub API and the
installed `pg0-embedded` 0.15.1 wheel; repository paths refer to master
`b36d4460`, and pg0 sources to its `main` unless a tag is named.

## How Python lite works today

`pseudolife_memory/storage/embedded_pg.py` drives the `pg0` CLI binary that
ships inside the `pg0-embedded` wheel (`pyproject.toml` `[lite]`:
`pg0-embedded>=0.15.1,<0.16`).

| Concern | Python lite | Source |
|---|---|---|
| Resolution | Nonempty DSN wins; `PSEUDOLIFE_MCP_STORAGE=files` opts out; any other value but `auto` is an error; with `pg0` importable, start or attach; otherwise file mode. Exports the DSN and data dir into the environment. | `embedded_pg.py` `resolve_daemon_storage` |
| Data dir | `PSEUDOLIFE_MCP_DATA_DIR`, else the per-user default: `%LOCALAPPDATA%\pseudolife-mcp`, `~/Library/Application Support/pseudolife-mcp`, `$XDG_DATA_HOME/pseudolife-mcp` or `~/.local/share/pseudolife-mcp`. pgdata is `<data_dir>/embedded_pg`. | `default_lite_data_dir`, `attach_or_start` |
| Guards | Windows non-ASCII pgdata refused with the remedy (pg0's runtime fails on initdb's ANSI-codepage output); `PG_VERSION` major other than 18 refused, never re-initialized. | `_preflight_path`, `_guard_pg_major` |
| First boot | A blocking OS lock on `<data_dir>/embedded_pg.lock` (byte lock on Windows, `flock` elsewhere; 120 s Windows timeout) around the start attempt. | `_start_lock` |
| Start or attach | `pg0 start --name pseudolife-<sha1(lower(resolved pgdata))[:10]>`; on `AlreadyRunning`, `pg0 info` supplies the URI. Only an instance this process started is stopped at exit. | `attach_or_start`, `_instance_name` |
| Credentials | Role `postgres`, password `postgres` (the wrapper defaults; `embedded_pg.py` passes neither), database `pseudolife_memory`, loopback TCP. | `pg0/__init__.py` `Pg0.__init__` defaults (`username`, `password`) |
| Binaries | pg0 embeds the theseus-rs PostgreSQL 18.1.0 archive and a pgvector 0.8.5 build at build time and extracts both to `~/.pg0/installation/18.1.0` on first start; runtime is offline. Its Linux builds also bundle `libxml2.so.2` and ICU 70 from Ubuntu 22.04 packages (sha256-pinned in `versions.env`) and prepend them to `LD_LIBRARY_PATH`; on macOS the theseus binaries need a host OpenSSL (pg0 issue #42, open). Instance state is `~/.pg0/instances/<name>/instance.json` (pid, port, data dir, installation dir, user, password, database, version). | pg0 `build.rs`, `versions.env`, `src/main.rs` (`extract_bundled_postgresql`, `install_pgvector`, `ensure_runtime_libs`, `save_instance`) |
| Server settings | Every start passes, as `-c` options not saved in `postgresql.conf`: `shared_buffers=256MB`, `maintenance_work_mem=512MB`, `effective_cache_size=1GB`, `max_parallel_maintenance_workers=4`, `work_mem=64MB`, `logging_collector=on` into `<pgdata>/log/postgresql-%Y-%m-%d.log` (rotated daily or at 100MB), `timezone=UTC`, `log_timezone=UTC`, plus `-F` (fsync off). Port 5432 when free on 127.0.0.1, else the next free one up. | pg0 `src/main.rs` `start`, `find_available_port`; `postgresql_embedded` v0.20.4 `postgresql.rs` `start` |
| New clusters | Windows: `initdb -U postgres --auth=password --encoding=UTF8 --locale=C` (`--locale=C` since pg0 0.15.2; 0.15.1 banks carry the system locale). Unix: `postgresql_embedded` 0.20's `initdb -U postgres --auth=password --encoding=UTF8` (system locale). | pg0 `init_data_dir`; `postgresql_embedded` v0.20.4 `postgresql.rs` |

## The constraint that decides it: both sides open one bank

A user's existing lite bank must start under the Rust daemon, and after a
rollback (CUTOVER "Recovery") the Python daemon must open a bank the Rust
daemon touched. So whatever the Rust side runs must agree with Python on:

1. pgdata location and layout: `<data_dir>/embedded_pg`, PostgreSQL major 18,
   and a pgvector whose control file installs 0.8.5 (Python's `ensure_schema`
   and the Rust `schema_plan.json` both run `CREATE EXTENSION IF NOT EXISTS
   vector`, so the installed control file sets the version).
2. The role, password and database above; changing any of them strands the
   other side.
3. Attach: a server the other side started must be found and used, never
   started twice. `postmaster.pid` in the pgdata carries the pid (line 1),
   data dir (2), start time (3) and port (4) whichever program started the
   server, so the Rust side can attach from it with the pid checked to be a
   live postgres process. That also avoids pg0's open issue #41 (on Windows
   `pg0 info` reports a live instance as stopped, which Python surfaces as
   "embedded postgres reported no connection URI").
4. The first-boot lock: the same file, with an OS lock both sides honour.
5. pg0's registry, in the other direction. pg0 decides whether a server
   runs from its `instance.json` record, not from `postmaster.pid`. When the
   Rust daemon started the server and Python later runs against it (a Python
   CLI mode or a rolled-back daemon):
   - with no record, pg0 goes on to start PostgreSQL, which fails on the
     live lock file, and Python gets a generic `Pg0Error`;
   - with a stale record from an earlier Python run (the usual upgrade
     case), pg0 0.15.2 checks that record against `postmaster.pid`, refuses
     `start`, then `info` reports the instance stopped and Python fails with
     "no connection URI"; pg0 0.15.1 checks only the saved pid, drops the
     record when that pid is gone, and the start fails as above.

   Either way Python fails. Decision 4 below; `pyproject.toml` admits both
   pg0 versions, so the proof runs against both.
6. Server settings and port: the `-c` settings and port policy above.
   Without them a Rust-started server takes initdb's detected time zone and
   logs somewhere other than where Python's errors point. The recommendation
   below matches pg0 exactly on these, except `-F`, which is decision 7.

## Candidates

| | A. Run the `pg0` binary, as Python does | B. `postgresql_embedded` crate in process | C. Native module (recommended) |
|---|---|---|---|
| What it is | Spawn `pg0 start/info/stop` from Rust. | theseus-rs `postgresql_embedded` 0.22.0 plus `postgresql_extensions` for pgvector. | Our own small module: provision the pinned PostgreSQL and pgvector archives, then spawn `initdb`, `pg_ctl` and `postgres` and read `postmaster.pid`. |
| Licence | Cargo and wheel metadata say MIT, but the repository has no LICENSE file (GitHub's licence API answers 404). | Apache-2.0. | Ours (Apache-2.0); artifacts: PostgreSQL licence (theseus-rs binaries), MIT (pgvector build repo). |
| Maintenance | Active (v0.15.2 2026-09-14, push 2026-10-24), 6 open issues, among them #41 above and #44, 24 advisories in its own lockfile. | Active (v0.22.0 2026-10-21); pg0 itself is still on 0.20. | We maintain what pg0's `start` path holds today: lock, guards, archive extraction, pgvector file install, Linux runtime libraries, initdb, the server settings, start, attach, database creation, health and stop. |
| Platforms | Windows, Linux and macOS; the #41 attach bug is open. | Windows, Linux and macOS. | The same theseus-rs binaries as A, plus, as in A, the Linux runtime libraries and a host OpenSSL on macOS. Needs the proof cases below. |
| Binary provenance | One opaque 60 MB `pg0-windows-x86_64.exe` (v0.15.2 release digest `sha256:88945756…`), built from unpinned downloads: its `build.rs` checks a sha256 only for the Linux runtime libraries, not the PostgreSQL archive or the pgvector build. | Runtime download from theseus-rs GitHub releases, checked against the `.sha256` file published beside it (self-attested); pgvector from the extension registries. Or `bundled`: the same download at build time, about +55 MB in the daemon binary. | The theseus-rs PostgreSQL archive and the pgvector 0.8.5 build that pg0 bundles, and on Linux the two Ubuntu runtime-library packages pg0 already pins, each pinned by sha256 in the tree (the D4 pattern). No source has a GitHub build attestation (all three answer 404). |
| Offline | Offline after install (bundled). | `theseus`: needs the network at first start, with unauthenticated GitHub rate limits. `bundled`: offline. | Offline once provisioned. Where provisioning happens is decision 2. |
| Dependencies | None new; ships a second executable. | A scratch crate resolving both crates with the workspace's TLS choice pulls 190 crates, 69 name@version pairs not in `rust/Cargo.lock` and 30 new crate names (`sqlx`, `sqlx-postgres`, `reqwest-middleware`, `reqwest-retry`, `h2`, `tempfile`, ...), beside a workspace that exact-pins most of its dependencies and talks to PostgreSQL through `tokio-postgres`. | None new: `reqwest`, `sha2`, `zip`, `tar`, `flate2`, `rustix` and `windows-sys` are already workspace dependencies. |
| Bank compatibility | Exact: the same program Python runs. | Same initdb on Unix; on Windows differs from pg0's `--locale=C` unless we override; keeps no pg0 registry. | Exact by construction (pgdata, credentials and initdb arguments above), plus whatever decision 4 says about the registry. |

### Recommendation: C

A gives exact behaviour by shipping the very binary whose build we cannot
verify and whose attach bug Python already hits; B adds a second PostgreSQL
client stack and an HTTP retry stack for what is, in this daemon, about five
process spawns and a file read. C keeps the dependency surface flat, pins
both artifacts by checksum the way D4 decided to pin the model export, uses the binaries
Python lite runs today (so the same banks open on both sides), and puts the
lifecycle code where the CUTOVER plan says B9 owns it. Its cost is owning the
whole start path listed in the table, not only the spawns; pg0's history
(#16, #17, #24, #35, #36, #37, #41) shows what that path has to get right,
and the proof cases below test exactly those.

The first-boot lock needs no new unsafe code: the standard library's
`File::lock` is `flock` on Unix (what Python's `fcntl.flock` takes; `fcntl`
record locks would not see it) and, on Windows, a `LockFileEx` over the
whole file, which overlaps the byte-0 range Python's `msvcrt.locking` takes,
so each side excludes the other. Checking that a recorded pid is a live
postgres process needs `OpenProcess` and `QueryFullProcessImageNameW` on
Windows, a narrow unsafe exception under PORTING.md's rules (pg0 does the
same).

The pick is provisional until the proof cases pass on the Windows dev VM
(PORTING.md, "Unsafe code and dependencies": a dependency choice stays
provisional until its platform behaviour is demonstrated).

## Decisions for the maintainer

None of these is decided here.

1. **Artifact chain.** The pgvector build pg0 bundles comes from a
   single-maintainer repository (`nicoloboschi/pgvector_compiled`, release
   `v0.18.320`); the PostgreSQL archive from theseus-rs. Python lite already
   trusts both through pg0. Accept them with in-tree sha256 pins, or build
   pgvector (and possibly PostgreSQL) in this repository's CI and publish our
   own release assets.
2. **Where the binaries come from at install.** Pinned download at install
   with a first-start fallback (the D4 pattern; the installers and
   release-mode `update` do it, per CLAUDE.md "Install and update
   experience", and an offline first start fails with the one fixing
   command), or bundled into the daemon executable (+about 55 MB on Windows,
   fully offline). Either way a new downloaded or bundled binary ships, so
   the installer half lands in the same PR as the provisioning or before the
   release (B11).
3. **Version.** Exactly PostgreSQL 18.1.0 (what every Python lite install runs
   today) or the newest 18.x (18.6.1 at this reading). A minor upgrade opens
   the same pgdata, but a rolled-back Python then runs 18.1.0 on a cluster a
   newer minor last ran, which PostgreSQL allows within a major. theseus-rs
   republished every 18.x as `18.x.1` between 2026-10-18 and 10-20; for
   Windows x64 the `.1` builds are compiled from PostgreSQL source with MSVC
   and vcpkg, where the `.0` builds (and so pg0's 18.1.0) repackage
   EnterpriseDB binaries. So "18.1.0" also means choosing between the
   EnterpriseDB-derived 18.1.0 and theseus's own 18.1.1 build.
4. **pg0 registry during the transition.** The Rust daemon writes a current
   `~/.pg0/instances/<name>/instance.json` when it starts the server, so
   Python tools and a rolled-back Python daemon can attach; or that gap is a
   declared divergence (Python CLI modes then fail while the Rust daemon
   owns the server). Writing it couples us to a third-party file format for
   as long as Python lite is supported, and its `installation_dir` field is
   where a later `pg0 stop`, `psql` or `info` runs binaries from, which ties
   this to decision 5.
5. **Install location.** A Pseudolife-owned runtime dir (two copies on disk
   during the transition), or reuse `~/.pg0/installation/<version>` when its
   files match the pins (couples to pg0's layout, saves about 160 MB on Windows).
6. **The one-time `.pt` import.** D1 keeps it, and D2 rules out a Rust port of
   the torch `.pt` format for `weights.pt`. Today Python imports
   `cms_state.pt` and `cortex_state.pt` into a PostgreSQL bank on daemon start
   (`storage/migrate.py` `migrate_legacy`). Either the import stays in Python
   (the Rust daemon refuses a data dir holding un-imported `.pt` state and
   names the command that imports it), or the Rust daemon gets a safe tensor
   reader for those two files. The first needs no new format code; the
   second keeps a single entry point.
7. **fsync.** pg0 starts every server with `-F`, so Python lite banks run
   with fsync off today: a power loss or OS crash can corrupt the bank, not
   just lose the last writes. Keep that (identical behaviour, faster
   writes) or start with fsync on (durable; either side still opens the
   bank, since fsync is a runtime setting, not a pgdata property). Until
   this is decided, step 2 follows pg0.

## Proof plan (step 2's acceptance cases, run before that PR leaves draft)

On the Windows dev VM (its own test PostgreSQL only; `lease check
full-suite@<vm>`, 8 GB free, the warm shared `CARGO_TARGET_DIR`), and on a
Linux dev box, with a disposable data dir. macOS has only hosted CI, so its
cases run there or stay recorded as unrun:

1. Python lite creates a bank; the Rust module attaches while Python's server
   runs, then starts the same pgdata after Python stops it, and reads the
   bank.
2. The Rust module creates a bank; Python lite (pg0) attaches to it while it
   runs and starts it after it stops, with and without decision 4's record,
   under both pg0 0.15.1 and 0.15.2.
3. The pg0 failure cases: a stale `postmaster.pid` after a hard kill, a
   reused pid after a reboot-like reassignment, a start against an existing
   instance, a stop that returns before the postmaster exits, a localized
   Windows locale, and two concurrent first boots on one empty data dir.
4. A path with spaces passes; a non-ASCII path is refused by the guard
   (whether a native spawn lifts pg0's limit is measured, not assumed; the
   guard stays until it is).

## How the work splits into PRs

1. This note.
2. The embedded-PostgreSQL module: resolve the per-user data dir, both
   guards, the first-boot lock, start or attach through `postmaster.pid`,
   pg0's server settings and port policy, database creation, and stop only
   what this process started. It takes the runtime dir as an input, so it
   does not wait on decisions 2, 3 and 5; provisioning and the Linux
   runtime libraries come with decision 2.
3. The daemon's startup resolution: DSN, then lite, then refuse.
   `PSEUDOLIFE_MCP_STORAGE=files` and the in-process mode are refused with a
   message naming their replacement; an unrecognized value is refused as
   Python refuses it. When lite engages, the data-dir handoff Python does by
   exporting `PSEUDOLIFE_MCP_DATA_DIR` applies too: `config.rs`
   `config_path` otherwise falls back to `<cwd>/data`, which would put
   `config.yaml` and the moved-bank markers somewhere other than the bank.
4. The lite halves of CLI-BACKUP-LITE, CLI-TRANSFER-LITE,
   CLI-AUDIT-TRANSPORT, CLI-PAIRING-CONTAINER and CLI-MAINTAINER-TRANSPORT
   and the lease's `phase4-embedded-pg-deferred` refusal, replacing the
   shim's "needs native embedded_pg" deferrals (`rust/shim/src/pg/resolution.rs`
   and the per-mode modules).
5. The deprecation notes for `embedded` mode and `.pt` file mode (release
   note and guides; D10 allows no new Python surface, so no Python warning
   is added).
