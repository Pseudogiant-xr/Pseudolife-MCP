# Sharing and moving a bank — `expose`, `invite`/`pair` and `move`

**Date:** 2026-10-02 · **Status:** revised after two review passes · **Parts 2
and 3 of 3** (part 1, `connect` and the installer's first question:
`2026-09-30-connect-and-bank-location-design.md`)

## Problem

Part 1 made re-pointing a machine's clients one command. Two journeys are
still made of hand edits.

**A second machine joins a bank.** `docs/guide/remote-bank.md` and the
installer's option 3 (`ops/install.sh` `show_shared_bank_notes`) list the
steps:

1. run `tailscale serve` by hand, after checking `/health` reports
   `"auth": true`;
2. mint a token and add it to `PSEUDOLIFE_MCP_TOKENS` (and the tier to
   `PSEUDOLIFE_MCP_TIER_MAP`) in `ops/.env`, then recreate the daemon;
3. list the new principal in `coordination.allowed_principals` in the
   daemon's `/data/config.yaml`, then restart the daemon again;
4. carry the token to the other machine yourself, through a chat window, a
   shell or a file copy.

Every identity is read from the environment once at startup
(`daemon.py` 415-441, `mcp_server.py` 189), so each new machine costs two
daemon restarts, and the secret travels by whatever channel the user
improvises.

**A bank moves to another host.** The 2026-09-29 move from a Windows host to
a Linux container took about a dozen manual steps. The bank's cutover lesson
records the order as "expose the target, take a final backup, stop the
source, restore, re-point clients". That order has a gap: writes that land
between the final backup and the stop are lost. Nothing scripts the move,
and the restore tooling (`ops/restore.sh`) assumes a local checkout.

## Goals

1. `pseudolife-mcp expose tailscale` makes a daemon reachable from the
   tailnet in one command, refusing a daemon without authentication.
2. `pseudolife-mcp invite <name>` on the daemon host creates an identity,
   admits it to the board and prints a short-lived single-use pairing code,
   with no daemon restart.
3. `pseudolife-mcp pair <url> <code>` on the new machine mints the token
   locally, writes it to an owner-only file, and registers only its hash
   with the daemon: no bearer token ever appears in a terminal, a chat,
   shell history or a network response. The installer's option 2 accepts a
   pairing code wherever it accepts a token.
4. `pseudolife-mcp move --to <ssh-target>` moves a Docker-tier bank to
   another Docker-tier checkout host with no lost writes, the source
   stopped and fenced (never deleted), and a rollback that never leaves two
   live daemons on one bank.

Out of scope: LAN exposure as a command (it needs the compose publish to
change, and the installer owns `ops/docker-compose.override.yml`; the guide's
manual section stays), Tailscale HTTPS mode (manual, as documented), moving a
lite/pip-tier bank, a release-install (no checkout) move target, and any
change to the one-bank-one-daemon model.

## What an invited principal can do

Tiers control which tools a client sees, not what it may call
(`toolset_tiers.py` 11-14), and today every bearer can call every `/api`
route, including `POST /api/config` (`web/routes.py` 263-265). A pairing
code would therefore hand out operator rights. This design narrows that for
stored principals only: they get full memory and board access, and are
refused `POST /api/config` and `POST /api/daemon-notice` with `403
{"error": "operator_principal_required"}`. Environment principals keep
every right they have today. The guide says plainly that an invited machine
can read and write the whole bank.

## A bank fingerprint on `/health`

`/health` gains `"bank": "<first 16 hex of sha256(coordination_bank_id)>"`.
It is not secret and identifies the bank, unlike `version` and `schema`.
The health payload never touches or starts storage (`daemon.py` 195-198),
so `bank` is null until storage has started and the bank id has been read
once by normal use; it is then cached (it never changes for a bank). Null
means "unknown": `invite` refuses on null, and `move` treats a null target
as different from the source. It is used by `expose`'s check, by
`invite` (direct-database mode must write to the daemon's own database; the
bench Postgres on 5433 also holds test databases), by `move`'s preflight
(target is not the source) and after `move`'s restore (target is now the
source's bank).

## Part 2a — `pseudolife-mcp expose`

```
pseudolife-mcp expose tailscale [--port 8765] [--yes] [--json]
pseudolife-mcp expose off [--port 8765] [--yes] [--json]
pseudolife-mcp expose status [--port 8765] [--json]
```

Runs on the daemon host. Standard library only, and added to the test's
`_CLIENT_ENTRY_MODULES` tuple, so it runs from a shim runtime.

`tailscale`:

1. `GET http://127.0.0.1:<port>/health` must answer `status: ok` and
   `"auth": true`. Otherwise exit 4: an unauthenticated bank is never
   exposed (the guide's warning, enforced).
2. Find the `tailscale` CLI (PATH, then the Windows and macOS default
   install locations). `tailscale status --json` must report
   `BackendState: Running`; otherwise exit 4, naming the fix (install,
   `tailscale up`).
3. Read `tailscale serve status --json`. If the TCP port already forwards to
   `127.0.0.1:<port>`, report "already exposed" and exit 0. If it serves
   anything else, refuse (exit 4): `expose` never replaces someone else's
   serve.
4. Show the plan (the exact command and the URL clients will use,
   `http://<tailscale ip -4>:<port>`), confirm (or `--yes`; no TTY without
   `--yes` exits 2), then run
   `tailscale serve --bg --tcp=<port> tcp://127.0.0.1:<port>`. A permission
   refusal (Linux without root or an operator grant) exits 4 with the fix:
   `sudo tailscale set --operator=$USER`.
5. Check: `tailscale serve status --json` must now show the forward; if not,
   undo and exit 5. Then probe `<url>/health` and compare the `bank`
   fingerprint. The probe is advisory: a host cannot always reach its own
   tailnet address through serve, so a failed probe is reported ("check
   from another machine: curl <url>/health") and does not roll back.
6. Print the URL and the next step (`pseudolife-mcp invite <machine>`).

`off` removes only a serve that forwards to `127.0.0.1:<port>`; anything
else on the port is left alone and reported. `status` prints the current
URL or "not exposed", and is what `invite` and `move` read.

Exit codes: 0 done or already in that state; 2 usage, declined, or no TTY
without `--yes`; 4 refused before any change; 5 changed but the serve did
not take (undone).

## Part 2b — principals stored in the bank

### Storage (schema v53)

One new table in its own DDL constant appended to `SCHEMA_SQL` (it is not
coordination state), added to `BENCH_RESET_TABLES` and to `transfer_cli`'s
`EXCLUDED_TABLES` (credentials, like `coordination_agents`; a logical
export never carries them, a physical backup does):

```sql
CREATE TABLE IF NOT EXISTS principals (
    principal       TEXT PRIMARY KEY,
    token_hash      TEXT UNIQUE,          -- sha256 hex of the bearer; NULL until paired
    tier            TEXT,                 -- NULL = the daemon's default tier
    board           BOOLEAN NOT NULL DEFAULT TRUE,
    code_hash       TEXT UNIQUE,          -- sha256 hex of the pending pairing code
    code_expires_at DOUBLE PRECISION,
    paired_code_hash TEXT,                -- the redeemed code, kept 10 min for idempotent retry
    created_at      DOUBLE PRECISION NOT NULL,
    paired_at       DOUBLE PRECISION,
    revoked_at      DOUBLE PRECISION
);
```

Neither a bearer token nor a pairing code is stored in plaintext. Both are
high-entropy random values, so an unsalted SHA-256 is enough to make a
stolen table useless; the board's instance credentials use the same scheme
(`storage/coordination.py` `_hash`). Because the table lives in the bank, a
backup carries it, and `move` carries every invited machine with no token
handling.

### The daemon's view: an in-memory snapshot

The table holds tens of rows. The daemon keeps all of it in memory, keyed by
`token_hash`, and never queries the database while resolving a request:

- A background thread refreshes the snapshot every 10 s on its own database
  connection, opened from the daemon's DSN. It never takes the service lock
  or the coordination lock, so it cannot queue behind a dream, and it never
  runs on the event loop.
- Resolution is `sha256(token)` plus a dict lookup. An unknown or random
  bearer costs no I/O and cannot evict anything.
- The redemption endpoint runs in the daemon, so on success it adds the new
  entry to the snapshot immediately, and evicts the principal's previous hash
  on a `--replace`: a freshly paired machine works at once. Immediate adds
  are kept until a refresh that *started* after them completes, so a refresh
  whose read began before the redemption committed cannot drop them.
  `invite --revoke` runs out of process and takes effect at the next refresh:
  within 10 s normally, and within the 60 s staleness limit below while the
  database is in trouble.
- The refresh thread also loads the `bank` fingerprint from `meta`. It never
  runs DDL. A missing table (`undefined_table`: a daemon upgraded to v53
  whose storage has not started yet, since the schema is applied when
  storage starts) counts as loaded and empty.
- If the snapshot has never loaded, or its last successful refresh is older
  than 60 s, a bearer that matches nothing in the environment gets `503
  {"error": "principals_unavailable"}` instead of 401, so `connect` and
  `doctor` do not report a valid token as bad. The always-200 hooks treat it
  as unauthorized and return their empty body. A daemon without Postgres has
  no store, and this rule does not apply there.
- When the table has rows but the environment configures no authentication,
  the daemon warns at startup: stored principals do not turn authentication
  on, and on an open daemon they mean nothing.

### One resolver, every site

`principals.resolve_principal(authorization, token_map, token, store)`
checks the environment map, then the singular token (both as today, with
`hmac.compare_digest`), then the snapshot. The environment always wins, so
an existing install behaves exactly as before. A stored principal whose name
later appears in the environment map is shadowed by it; `invite` refuses
such names up front (below), and the daemon warns at startup if one exists.

Today the bearer is resolved, or the admission list read, in more places
than the gate. Each of these moves to the shared resolver and to one
admission helper, and the implementation greps for `allowed_principals`,
`resolve_principal`, `authenticated_principal`, `current_principal` and
`_TIER_MAP` to find any the list misses:

- the HTTP gate (`web/api.py` `_principal`, `_authorized`);
- `/mcp` tool calls: `mcp_server._bound` re-binds every `tools/call` and
  `tools/list` from `ctx.headers` (`mcp_server.py` 2586), and older-protocol
  requests run on a task group created at startup, so a principal bound in
  the gate never reaches them. Resolution therefore happens inside `_bound`,
  from `ctx.headers`, with the shared resolver (a snapshot hit), binding
  `principal=<resolved>`;
- the board: `coordination.authenticated_principal` and the hook paths that
  use it (`park_gate`, `woke`, `subagent`, `unavailable_reason`), the `/mcp`
  bound-identity check and `/api/coordination`. A bound principal is
  honoured only when authentication is configured: on an open install the
  board keeps refusing with `authentication_required`;
- tiers: `resolve_tier` and the `memory_toolset` floor, which reads
  `_TIER_MAP` directly (`mcp_server.py` 1025), fall back to the stored tier
  after the environment map;
- admission: `principal_admitted(config, principal)` replaces every inline
  `principal not in cfg.allowed_principals` check (`coordination.py` 329,
  479, 637; `service.py` 8425; `coordination_recovery.py` 77). Admitted if
  listed, or stored with `board = true` and not revoked; `daemon` stays
  reserved. `unavailable_reason` keeps its "no storage I/O" promise because
  the snapshot is in memory.

A daemon without Postgres has no store: resolution is unchanged, and
`invite` refuses (`principals_require_postgres`).

### `pseudolife-mcp invite`

```
pseudolife-mcp invite <name> [--tier TIER] [--no-board] [--expires 15m]
                             [--replace] [--url URL] [--json]
pseudolife-mcp invite --list [--json]
pseudolife-mcp invite --revoke <name> [--yes]
```

An operator command on the daemon host. Like `board-audit`, `lease break`
and `export`, it talks to Postgres directly, and access to the database is
the privilege.

The host-side entry is standard library only until it has chosen a path,
because on a Docker host `pseudolife-mcp` is a shim runtime without the
storage layer:

1. `PSEUDOLIFE_MCP_DATABASE_URL` set (or the lite tier's embedded Postgres
   found, as `transfer_cli` finds it): run locally. Before writing, compare
   the database's bank identity with the daemon's `/health` `bank`
   fingerprint and refuse a mismatch.
2. Otherwise, on a Docker install: check the daemon container's image
   carries `invite` (`docker exec pseudolife-mcp-daemon python -m
   pseudolife_memory.cli invite --version-check`), then re-run inside it
   (`docker exec -i ...`), where the daemon's own DSN and environment are
   set. The host never handles the database password, and the environment
   checks below see the daemon's real environment. An older image is
   refused with "update the daemon first".

Rules:

- `<name>` becomes the principal: lowercase, `[a-z0-9][a-z0-9._-]{0,63}`.
  Refused: `default`, `daemon`, and any name already in the daemon's
  `PSEUDOLIFE_MCP_TOKENS` or `PSEUDOLIFE_MCP_TIER_MAP`. A name already in
  `coordination.allowed_principals` without a token is accepted with a note.
- An existing paired name is refused unless `--replace`. With `--replace`,
  redeeming the new code replaces the token; the old one keeps working until
  then. A revoked name may be invited again: the row is reset (token and
  revocation cleared, new code).
- The daemon must report `"auth": true`; on an open daemon `invite` refuses.
- The code is 12 Crockford base32 characters (60 bits), printed as
  `XXXX-XXXX-XXXX`; input is normalised (case, dashes, `O`→`0`, `I`/`L`→`1`).
  `--expires` defaults to 15 minutes, maximum 24 hours.
- Output: the code, its expiry, and the line to run on the new machine
  (`pseudolife-mcp pair <url> <code>`, or the installer's option 2). The URL
  is `--url`, else `expose status`, else a `<daemon-url>` placeholder.
- `--list` prints name, paired or pending (with code expiry), tier, board,
  created and paired times, revoked. Never a token or a code.
- `--revoke` sets `revoked_at` and clears any pending code.

### Redemption endpoint

`POST /api/pair`, routed before the authenticated `/api/` branch. Body
`{"code": "<code>", "token_sha256": "<64 hex>"}`.

- Only `POST`, only `Content-Type: application/json` (the existing 415
  logic), 1 KiB body limit, and any request carrying an `Origin` header is
  refused, so no browser page can reach it.
- Redemption is one conditional `UPDATE principals SET token_hash = $2,
  paired_at = now, code_hash = NULL, code_expires_at = NULL WHERE code_hash =
  $1 AND code_expires_at > now AND revoked_at IS NULL RETURNING principal,
  tier`, with "now" from the database clock. It is single-use under
  concurrency. It runs in a worker thread, never on the event loop.
- Success: `200 {"principal", "tier", "bank"}` with `Cache-Control:
  no-store`. No token is in the response: the client minted it and sent only
  its hash.
- Idempotent retry: a repeat POST whose code matches a row that was paired
  from that code in the last 10 minutes *and* whose `token_sha256` equals the
  stored hash returns the same 200. Only the client that minted the token
  knows that hash, so a lost response is recoverable and nothing else is.
  (The code's hash is kept in a `paired_code_hash` column for this window
  instead of being cleared outright.)
- Every failure (unknown, expired, used, revoked, malformed, hash already in
  use) is the same `400 {"error": "pairing_refused"}`: no oracle for which
  codes exist.
- Rate limit: at most 20 failed redemptions per minute for the daemon as a
  whole, then `429 {"error": "rate_limited"}` without consulting the store.
  Behind `tailscale serve --tcp` or Docker's bridge every client arrives from
  the same address, so a per-address limit would be the same bucket; the
  global limit is the real one, and any peer can hold pairing at 429 while it
  floods. With 60-bit codes that live 15 minutes, guessing is out of reach
  even at the limit.
- Refused (`400 pairing_refused`) when the daemon has no authentication.
- The handler never logs the body or exception text (uvicorn's access log is
  off at `log_level="warning"`, `daemon.py` 509; the requirement is on the
  handler).

### `pseudolife-mcp pair`

```
pseudolife-mcp pair <daemon-url> [<code> | --read-code] [--token-file PATH] [--json]
```

Standard library only, in the shim-runtime module tuple.

1. Validate the URL the way `connect` does; `GET /health` must report
   `"auth": true`.
2. Mint the token (`secrets.token_urlsafe(32)`) and write it to
   `--token-file`, or to a new owner-only file
   `~/.pseudolife-mcp/pairing-<8 hex>.token`, through `client_config`'s
   token-file helpers. An existing file is never overwritten.
3. `POST /api/pair` with the code and the token's SHA-256.
4. On success, validate the returned principal name against the same regex
   (a server or anyone on a plain-HTTP path cannot steer the path), and, if
   `--token-file` was not given, move the file to
   `~/.pseudolife-mcp/<principal>.token` without ever replacing an existing
   file: `os.link` then `unlink` on POSIX, `os.rename` on Windows (which
   refuses an existing target); on `FileExistsError`, keep the pairing name.
   Then verify the token with `connect`'s authenticated check.
5. On a lost response (timeout, connection reset), retry the same POST a few
   times: the endpoint's idempotent retry returns the original 200. On a
   refused redemption, remove the file. If the outcome is still unknown, or
   verification fails, keep the file and say so: the code may already be
   spent, and the file is the only copy of a token the daemon may now
   accept.

It prints the token file path and the principal, never the token. A 429 is
reported as "pairing is rate-limited on the daemon, try again in a minute".
`--read-code` reads the code from stdin, for callers that keep it out of
argv.

### `connect --code`

`pseudolife-mcp connect <url> --code <code>` (or `--read-code`) runs `pair`,
then `connect` with the new token file. The code is redeemed after
confirmation, in the phase where `connect` already sends its first
credential, so `--dry-run` never consumes it. The plan names the token file
as `~/.pseudolife-mcp/<principal>.token (name from the daemon)`. A plan with
nothing to re-point (exit 3) stops before redeeming and says to run the
installer with the code instead. When more than one client would be
re-pointed, the plan warns that they will share one principal, and that
inviting one name per client keeps their writes apart.

### Installer

- Option 2's token prompt also accepts a pairing code (recognised by
  shape), and both installers gain `--pairing-code CODE` /
  `-PairingCode CODE`. After the shim is installed, the installer runs
  `<shim> pair <url> --read-code --token-file <default> --json` with the
  code on stdin, then continues exactly as with a token file.
- Option 3's printed steps become two commands, with the hand steps kept
  as a pointer to the guide: `pseudolife-mcp expose tailscale`, then
  `pseudolife-mcp invite <machine>` per joining machine. The installer
  offers to run `expose tailscale` at the end of the install (default no),
  since it changes the host's tailnet serve.
- The hints at `connect_cli.py` 706 and `board_status.py` 24 that tell the
  user to edit `allowed_principals` also mention `invite`.

## Part 3 — `pseudolife-mcp move`

```
pseudolife-mcp move --to <ssh-target> [--target-checkout PATH]
                    [--target-url URL] [--no-keep-tokens] [--resume]
                    [--dry-run] [--yes] [--json]
```

Run from the source host. Standard library only (subprocess for `ssh` and
`docker`). `<ssh-target>` is anything `ssh` accepts (`root@box`, or a
`~/.ssh/config` alias); key-based auth is required (`ssh -o BatchMode=yes`),
and nothing ever types a password. Files are copied as `ssh <target> 'cat >
<path>' < <file>` with a SHA-256 check on the far side, which avoids
Windows `scp` path quirks.

Scope: a Docker-tier source and a Docker-tier checkout target where the
installer has already run (daemon and Postgres containers exist). A target
that is not ready is refused with the install line to run there.

### Order

1. **Preflight, no changes.** Source `/health` ok and Docker tier; `ssh
   <target> true` works; the target checkout exists (default
   `~/src/Pseudolife-MCP`, or `--target-checkout`); the target daemon is
   healthy, its schema is at least the source's, its `bank` fingerprint
   differs from the source's, and its bank is empty (no entries, facts or
   principals). `move` never merges into or overwrites a used bank, except
   with `--resume` over a bank whose move marker names this move. Target
   exposure: `--target-url`, else the target's `expose status`; if it is not
   exposed, the plan includes `expose tailscale` on the target.
2. **Plan and confirm.** Every step below, with sizes and the target URL.
3. **Expose the target** if needed (remotely, `expose tailscale --yes`; the
   Linux operator-permission refusal stops here with its fix), and check this
   host reaches `<target-url>/health`.
4. **Stop the source daemon** (`docker stop pseudolife-mcp-daemon`, never
   `rm` or `down`), after pausing the source's unattended update if one is
   scheduled. Postgres stays up. The daemon is the bank's only routine
   writer, so the dump that follows is final; step 5 closes the remaining
   direct-database writers. From here on, any failure before step 10 runs
   the rollback.
5. **Final backup on the source, then fence its database.** `pg_dump`
   through the Postgres container; the daemon's `/data` through `docker run
   --rm --volumes-from pseudolife-mcp-daemon` (it works on a stopped
   container; `moved.json` and `move.json` are excluded); and a manifest
   with per-table row counts. This is new package code: `update_cli`'s
   release-mode backup tars `/data` with `docker exec` into a running
   daemon, only warns when the state archive fails and writes no manifest,
   so it cannot serve here. A failed state archive is a failure.
   Immediately after the dump, fence the source database with `ALTER
   DATABASE <db> WITH ALLOW_CONNECTIONS false` (run as the Postgres
   superuser against the `postgres` database). That stops every writer of
   any version, including an older daemon image that knows nothing of
   `moved.json`, and direct-database commands such as `lease break` and
   `board-audit`. Rollback undoes it with `ALLOW_CONNECTIONS true`.
6. **Copy** the three files to the target and check their SHA-256 there.
7. **Restore on the target, without starting it.** `ops/restore.sh` gains
   `--no-start` (and `move` always names the dump with `--backup-file`, so
   it never picks the newest one in the target's `data/backups`). The
   target's restore takes its own safety dump first, as it already does. Then:
   restore the database; compare row counts with the manifest (the daemon is
   not running, so nothing races the comparison); restore the state archive;
   write a move marker (`/data/move.json`: source fingerprint, time, move id).
8. **Carry the environment identities** (default; `--no-keep-tokens` skips
   it). The source's `PSEUDOLIFE_MCP_TOKEN`, `PSEUDOLIFE_MCP_TOKENS`,
   `PSEUDOLIFE_MCP_TIER_MAP` and `PSEUDOLIFE_MCP_TOOLSET` are read from the
   container that actually ran (`docker inspect pseudolife-mcp-daemon`
   `Config.Env`), not from `ops/.env`, because Compose prefers shell
   variables and on a Windows host the token can be a User variable. They are
   written into the target's `ops/.env` over ssh stdin (never argv), with LF
   line endings and the target's file backed up first. The target's own
   installer-minted token is replaced, and the report names any registration
   on the target that used it. Invited principals need nothing: they are in
   the bank. With `--no-keep-tokens`, the report lists every environment
   principal that must be re-invited.
9. **Start the target once** with `ops/update.sh` (which recreates it with
   the carried environment), wait for health, and check its `bank`
   fingerprint now equals the source's. `move` then removes the target's
   `move.json` itself (after the check, so a failed check leaves `--resume`
   its marker), and writes `/data/moved.json` into the stopped source
   container (`docker cp`). The database fence from step 5 is what stops the
   old host; `moved.json` adds the readable reason: a daemon new enough to
   know it refuses to start and names the new location, instead of failing
   on a refused database connection.
10. **Re-point this machine's clients**: `pseudolife-mcp connect
    <target-url> --yes`. A failure here does not roll back the bank (the
    target is correct and the source is fenced); it is reported with the
    command to rerun.
11. **Report**: the source is stopped and fenced, not deleted, with the
    manual rollback (`docker stop` the target daemon; `ALTER DATABASE <db>
    WITH ALLOW_CONNECTIONS true` on the source Postgres; remove
    `/data/moved.json` with `docker run --rm --volumes-from
    pseudolife-mcp-daemon <image> rm /data/moved.json`, since `docker cp`
    cannot delete; `docker start` the source daemon; `connect` back). Then every other machine's principal with the `connect
    <target-url>` line to run there; board leases still held by the source's
    sessions, each with its `lease break` line (claim leases last 24 h); and
    the source leftovers to retire by hand, each with its command: backup
    task or cron, extractor shims (the moved daemon's extractor settings may
    name `host.docker.internal` on the old host), saved tunnel profiles, the
    daemon autostart task, the tailnet serve, and the unattended update. It
    also notes what the state archive replaced on the target: `config.yaml`
    (the source's extractor and update settings) and `last-backup.json`
    (`/health`'s `last_backup` names the source's last backup until the
    target's first).

### Rollback

Any failure in steps 5-9 runs, in order: stop the target daemon if it was
started; restore the target's `ops/.env` from its backup if step 8 wrote it;
lift the source database fence if set; remove the source's `moved.json` if
written; then restart the source's paused schedule and `docker start` the
source daemon. The target is never left running beside a running source. The
target keeps its move marker, so `move --resume` may overwrite that
half-restored bank on the next attempt, and nothing else may.

### Board state

The documented restore procedure (`docs/guide/coordination-recovery.md`)
revokes every restored instance credential, because a restored bank could be
a second copy of a live one. A `move` is not a clone: the source database is
fenced right after the final dump, so there is only ever one live authority
for the bank's mailboxes, and the revocation protects against nothing here.
`move` therefore does not run `coordination-recovery`.

That does not mean sessions keep their board addresses. A shim discards its
saved adapter state when the bank URL changes (`coordination_adapter.py`
731), and `connect` already tells the user that re-pointed sessions start
with new addresses. Mail addressed to an old address stays in the bank,
undelivered. The report counts undelivered mail per old address and points
to `coordination-recovery rebind` for anyone who needs an address back. The
guide records this exemption and its limit next to the procedure.

Exit codes: 0 moved; 1 failed and rolled back (source running again);
2 usage or declined; 4 preflight refused, nothing changed; 5 the bank moved
but a later step (re-pointing, final checks) failed.

## Where the code lives

- `pseudolife_memory/expose_cli.py`, `pair_cli.py`, `move_cli.py`, and the
  host-side entry of `invite_cli.py`: standard library only, in the
  shim-runtime module tuple.
- `pseudolife_memory/principal_store.py`: the table, the snapshot and the
  redemption; used by the daemon and by `invite` in its database path.
- `principals.py`, `web/api.py`, `web/routes.py`, `mcp_server.py`,
  `writer_context.py`, `coordination.py`, `service.py`,
  `coordination_recovery.py`, `daemon.py`: the one resolver, admission, the
  operator-route refusal, the `/health` fingerprint, the `moved.json` fence
  and the startup warnings.
- `ops/restore.sh`, `ops/restore.ps1`: `--no-start`.
- `ops/install.sh`, `ops/install.ps1`: option 2's code, option 3's steps.
- `docs/guide/remote-bank.md`: "Adding a machine" (expose, invite, pair)
  ahead of the hand steps, with what an invited machine can do; "Moving a
  bank" (move). `docs/guide/coordination-recovery.md`: the move exemption.

## Testing

- `expose`: a fake `tailscale` on PATH for every branch (not installed, not
  running, foreign serve, already exposed, permission refused, serve not
  taking, advisory probe failing without rollback).
- Store and resolver: PG-backed redemption tests (single use under two
  concurrent redeemers, expiry, revoke, replace, re-invite after revoke,
  database clock); the snapshot (refresh, immediate add on redemption,
  revocation within one interval, 503 when stale, no I/O on an unknown
  bearer); environment precedence; and that `/mcp` tool calls on both
  protocol paths, `/api`, `/api/coordination` and the board hooks all see a
  stored principal and its tier. An open-install test that the board still
  refuses. A test that every admission site goes through the helper.
- Operator routes: a stored principal gets 403 on `/api/config` and
  `/api/daemon-notice`; an environment principal does not.
- Endpoint: identical failure bodies, `Origin` refusal, content type,
  global rate limit, no-store header, nothing from the body in captured
  logs.
- `pair` and `connect --code`: a fake daemon; the token never on stdout or
  in any request body; dry-run never redeems; the exit-3 refusal; a rogue
  principal name cannot change the file path; the file kept on a lost
  response.
- `move`: a fake runner for `ssh` and `docker` that records every command,
  covering each step's failure and its rollback (the target is stopped
  before the source starts); stop precedes dump; tokens never in any
  recorded argv; `--resume` accepted only over this move's marker.
- `move`'s fence: the recorded commands set and lift `ALLOW_CONNECTIONS`;
  rollback lifts it before the source starts; with PG available, a test
  that a fenced database refuses a new connection and an unfenced one
  accepts it.
- Daemon: refuses to start with `moved.json`; `/health` fingerprint,
  including null on a cold daemon with storage left unstarted.
- Installer: option 2's code path and option 3's text, in the existing
  installer test files.
- Schema v53 touches the seven places in CLAUDE.md and needs a local full
  suite.

## Decisions (to confirm)

1. Stored principals live in a new bank table; environment identities keep
   precedence; nothing migrates from the environment automatically.
2. Stored principals are refused the two operator routes (`/api/config`,
   `/api/daemon-notice`); otherwise they have full memory and board access.
3. One principal per invite; `connect --code` warns when several clients
   would share it.
4. `pair` mints the token on the client and sends only its hash.
5. Exposure as a command covers Tailscale TCP only; LAN and HTTPS stay
   documented manual steps.
6. `move` reaches the target over key-based ssh, supports Docker-tier
   source and checkout target only, stops the source before its final dump,
   fences the source database (`ALLOW_CONNECTIONS false`, plus `moved.json`
   for the message), and skips `coordination-recovery` for the reason above.
7. `move` carries environment tokens by default, so no client anywhere needs
   a new secret.
8. Three stacked PRs: `expose` (with the `/health` fingerprint); then
   principals, `invite`, `pair`, `connect --code` and the installer; then
   `move` (with `restore.sh --no-start` and the fence).
