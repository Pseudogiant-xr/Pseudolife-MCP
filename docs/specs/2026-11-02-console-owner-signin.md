# Console sign-in and owner-scoped board visibility

**Status:** design, 2026-11-02. The maintainer decided two things. Every
principal belongs to the bank owner unless it was invited as someone else's.
The Console signs in both with a one-time link from the CLI and with the
maintainer passkey.

This work adds new Python routes, a new CLI mode and a schema bump (v59).
D10 in `rust/CUTOVER.md` freezes all three, so the maintainer grants or
refuses an exception on this spec's PR before any implementation PR opens.

## What a user should experience

You install Pseudolife, and at the end the installer opens the Console in
your browser, already signed in. The Board lists every session you run, on
every machine, and the mail and wake timeline shows their mail. Later, on
another device, you open the Console's HTTPS address and sign in with the
passkey you already use for signed messages. You never see the words "bearer
token" unless you look for them.

Today none of that holds:

1. **The Console asks for a pasted token.** It asks on first open, and again
   at every new address, because browsers keep the token per origin. The
   docs and `maintainer setup` tell you where the token file lives.
2. **The Board's mail follows one exact principal.** The timeline and unread
   counts use the principal of that token. The installer recommends one
   principal per machine (pairing codes), and a Console may have its own, so
   one person ends up with several principals. Their Console then sees none
   of their sessions' mail. The empty timeline now says why (#1141), but
   cannot fix it.
3. **Nothing records which principals are the same person's.** The
   `principals` table holds a tier, a board flag and a token hash.

## 1. Ownership of principals

A principal is either the **owner's** or **someone else's**. The bank has one
owner: the person who runs the daemon.

- **Stored principals** (the `principals` table, made by `invite` and
  redeemed by pairing) gain `owned BOOLEAN NOT NULL DEFAULT TRUE`.
  - The v59 migration sets it true on every existing row, so existing
    installs become owner-scoped with no step.
  - `pseudolife-mcp invite <name> --someone-else` creates a row with
    `owned = false`. The invite output names the flag in one line. There is
    no flag for the default.
- **Environment principals** (`PSEUDOLIFE_MCP_TOKEN` and every
  `PSEUDOLIFE_MCP_TOKENS` entry) are the owner's, except names listed in the
  new file-only config key `coordination.not_owner_principals`. Before
  invites existed, the token map was the way to give someone a token, and
  that list keeps such a token someone else's. `POST /api/config` refuses
  the key (`config_protected`), as it refuses `coordination.maintainer*`.
- **Reserved principals.** `maintainer` (passkey-signed mail) counts as the
  owner's for visibility. `daemon` does not.
- `invite --list` gains an "owner" column.

## 2. What ownership changes

**Only the Console's board snapshot widens.** `console_snapshot` resolves the
caller's principal. If it is the owner's, the snapshot covers the **owner
set**: environment names not listed as someone else's, owned stored rows not
revoked, and `maintainer`. Any other principal keeps today's per-principal
scope exactly. Message bodies stay excluded.

**The query is rewritten, not edited.** Two branches of today's query
(`storage/coordination.py` `_console_snapshot`) only work for one principal:

- The caller-events branch orders by `principal DESC, seq DESC LIMIT 101`.
  With one principal that order is harmless. With `principal = ANY(set)` it
  would return the 101 newest events of the alphabetically last owner
  principal only.
- The `visible_mail` CTE is `MATERIALIZED` with no limit. Under owner scope
  it becomes nearly every send in the retention window, all inside the 5 s
  statement timeout.

The rewrite takes the newest 101 events per principal through a `LATERAL`
over the owner set (each one an index range on `(principal, seq)`), merges
them by `seq`, and bounds the recipient branch the same way.

It is a perf and index change, so it gets the independent review before
commit and a measured run on a bank of at least the live board's size.

**Mailbox access is unchanged.** `_auth` (receive, ack, rebind, park gates)
and `memory_message history` stay bound to the exact principal plus the
agent's credential.

**The trust expansion, stated plainly:**
- Any bearer of an owner principal can read board metadata for all the
  owner's sessions through `GET /api/agents?view=coordination`. That means
  any agent token on any of the owner's machines.
- The metadata is who mailed whom, when, the wake decision and the delivery
  path. Today that cross-principal mail graph is readable only one principal
  at a time.
- Those tokens already read and write the whole bank, and tiers are not
  access control (2026-10-02 spec), so this widens metadata and not content.
  It is still a widening, and the changelog says so.

## 3. Console sessions

A **Console session** is a bearer the browser holds in localStorage
(`pl_token`, as today) that resolves to an existing principal. The Console
keeps sending `Authorization: Bearer …`, so no cookie is introduced and the
CSRF model for bearer-gated `/api/*` (an Authorization header cannot be
attached cross-origin) is unchanged.

**New table** (v59) `console_sessions`:
- `session_id`;
- `token_hash`, unique, the SHA-256 of the browser-minted token;
- `principal`;
- `source`: `environment` or `store`, the source of the credential that
  issued it;
- `issuer`: what issued it. That is the SHA-256 of the issuing bearer for a
  link, or the passkey `credential_id` for a passkey sign-in;
- `method`: `link` or `passkey`;
- `label`;
- `created_at`, `last_used_at`, `expires_at`, `revoked_at`.

The table is credentials of this deployment. It stays out of logical exports
and is listed in `BENCH_RESET_TABLES`, like v54's and v58's tables.

**Resolution is an explicit opt-in, never global.** Hooks, `/mcp`,
`writer_context` and the board's `/api/coordination/*` routes all resolve
through `authenticated_principal` and `resolve_principal_detailed`. A
session found there would become an agent credential that could register a
mailbox and send mail.

So sessions get their own resolver, `resolve_console_session`, and only
these call sites use it:
- the `/api` transport gate, for every `/api/*` path except
  `/api/coordination/*`, `/api/hook/*`, `/api/console/link` and
  `/api/daemon-notice`;
- the `authenticated_principal` calls of the board snapshot and the
  maintainer routes.

Everything else, `/mcp` included, keeps today's resolver and answers 401 to
a session bearer. The Console calls none of the excluded routes, so this
costs it nothing.

The session snapshot refreshes every 10 s, counts as available for 60 s, and
gains a new session immediately. Rust mirrors the same split
(`auth::resolve_detailed` unchanged, a separate session lookup at the same
call sites).

The resolver works as follows:
- **What a hit gives.** The session's principal, with source `session`
  carrying the issuing source.
- **When a session stops resolving.** Each of these alone ends it:
  - it is revoked;
  - it is 30 days past its last use;
  - it is 90 days past its creation, an absolute limit;
  - its principal no longer resolves on its own (an environment name dropped
    from the token map, or a stored row revoked);
  - its issuer is gone:
    - a link session's issuing token no longer resolves to the same
      principal (the environment token changed, or the stored row was
      re-paired with a new token hash);
    - a passkey session's key is no longer `active`.
- **Revocation is eager as well.** Revoking or re-pairing a principal, and
  `revoke-self`, `cancel`, host `maintainer revoke` and `reset` on a
  passkey, mark the matching sessions revoked in the same transaction.
  Re-inviting a revoked name never brings its old sessions back.
- **Sessions cannot issue sessions.** `POST /api/console/link` refuses a
  session bearer, so revocation never has to follow a chain.
- `principal_activity` records session use under the session's principal,
  so `invite --list` keeps a true last-seen.

**Rights:**
- **Board.** A session has its principal's board admission and owner scope.
- **Operator routes** (`POST /api/config`, `/api/daemon-notice`). The gate
  becomes an allowlist: environment or open source, or a session whose
  source is `environment`.
  - Today it refuses only `store` (`web/api.py`, the `_OPERATOR_POST_PATHS`
    check; `rust/daemon/src/auth.rs`, the `PrincipalSource::Store` check).
    A new source would pass both unless both are rewritten.
  - The tests include a session issued from a paired client being refused.
    This keeps the 2026-10-02 rule that a pairing code never grants operator
    rights.
  - That Console's Settings view says why, and names the way in: sign in
    from the daemon's host.
- **Maintainer authority.** A session never carries it.
  - `_authority` (`storage/coordination.py`) gives maintainer authority to
    mail from an agent registered under a principal in
    `coordination.maintainer_principals`.
  - A session can neither register an agent nor send board mail, because
    `/api/coordination/*` refuses it. So no session mail exists for
    `_authority` to judge, and that function needs no change.
  - Every `/api/maintainer/*` action still needs its own passkey assertion.
- **Environment principals listed in `coordination.not_owner_principals`**
  keep the operator rights they have today, and so do sessions they issue.
  This is not a regression. The list changes board visibility only.

**Revoking sessions.** The Console's Settings view lists signed-in browsers
(label, method, principal, created, last used) and revokes them.
`pseudolife-mcp console --list` and `--revoke <id>` do the same.

**Open mode.** A daemon with no token configured has no principals, so
sign-in is refused there, as `/api/pair` is. The Console needs no sign-in on
such a daemon anyway.

## 4. Sign-in with a one-time link

`pseudolife-mcp console` (new CLI mode) opens the Console signed in.

1. **The CLI finds its credentials** the way every client command does
   (`PSEUDOLIFE_MCP_DAEMON_URL`, `PSEUDOLIFE_MCP_TOKEN_FILE`, else the client
   registrations `doctor` reads).
2. **It asks for a code.** It calls `POST /api/console/link` with that bearer
   and `{origin}`, the daemon origin it reached. The daemon validates and
   normalises that origin before storing it. It must be `http` or `https`,
   and either loopback or the configured passkey origin. The host is
   lower-cased, the scheme's default port and any trailing slash are
   dropped, and a path, query or credentials are refused. That way a stored
   origin always equals what a browser sends. The daemon creates a
   one-time code in `console_signin_codes`. That table is v59, excluded from
   exports and holds:
   - `code_hash`;
   - `principal`, `source` and `issuer` from the calling bearer;
   - `origin`: the origin the Console may redeem the code from. That is
     `coordination.maintainer.origin` when passkeys are set up, otherwise the
     origin the CLI reached;
   - `expires_at`, 5 minutes after issue;
   - `used_at`.

   The code uses the pairing-code generator: 12 characters from a 32-symbol
   alphabet, 60 bits. It is single use.
3. **The daemon answers** with the code and
   `<origin>/ui/#/signin?code=…`. The code sits in the fragment, which
   browsers never send to a server.
4. **The CLI opens the URL** in the system browser and also prints it with
   the short code.
   - On a headless host you open the URL on any device that reaches that
     origin, or type the code into the Console's sign-in dialog there.
   - `--no-open` only prints. On a shared multi-user host the browser's
     command line is visible to other users, so use it there.
5. **The Console redeems it.**
   - It reads the code and clears the fragment (`history.replaceState`).
   - If the browser already holds a working token, it asks before replacing
     it, so a link cannot silently sign the owner's browser into someone
     else's principal.
   - It mints a random token, stores it, and posts
     `{code, token_sha256, label}` to `POST /api/console/session`.
6. **The daemon creates the session.** The Console reloads, signed in.

**`POST /api/console/session` is a browser-called pre-auth route**, the
second after `/api/pair`. The rule "no `Origin` header" cannot apply to it,
and the origin cannot be derived from the request: the daemon sits behind
`tailscale serve` and Docker, forwarded headers are forgeable from loopback,
and a rebinding attacker controls both Host and Origin. Instead:

- **Origin.** It must equal the code's stored `origin`. For a passkey
  sign-in it must equal `coordination.maintainer.origin`.
- **Content type.** `application/json` only, which forces a preflight, and
  no CORS headers are served.
- **Body.** It must hold exactly the documented keys. It is capped at 1 KiB
  for a code. A passkey sign-in gets 8 KiB, since an RS256 assertion alone
  is over 1 KiB.
- **Refusals.** Every bad code or assertion gets one uniform
  `signin_refused`.
- **Limits.** `/api/pair`'s shape: a global failure budget (`FailureLimiter`)
  and two concurrent redemptions (`RedemptionGate`). There is no per-code
  attempt counter: a wrong code matches no row, and 60 bits under the global
  budget is the bound.

The browser mints the credential and sends only its hash, as `pair` does.

**Where the code is exposed:**
- the terminal that printed it;
- the screen;
- the browser's history, including synced history, which `replaceState`
  does not purge;
- on a shared host, the browser's command line.

It is single use, lasts 5 minutes and is redeemable only at its stored
origin, which bounds all four.

## 5. Sign-in with the passkey

At `coordination.maintainer.origin`, the sign-in dialog offers "Sign in with
your passkey".

1. **The browser mints the token first.**
2. **It asks for a challenge.** `POST /api/console/passkey-challenge` takes
   `{token_sha256, label}`. Both go into the MAC'd challenge payload under a
   new purpose, `sign-in`, so the assertion signs exactly which token the
   session will accept. A captured `{payload, mac, assertion}` is useless to
   anyone holding a different token.
   - The route is pre-auth, with the same Origin and content-type rules.
   - It has its own failure budget and concurrency gate.
   - It does not go through the maintainer `_run` path. It reads the signing
     keys and the secret on its own short-lived database connection, as
     pairing does (`principal_store.redeem(dsn, …)`), never on the board's
     mailbox connection, which only the coordination lock may use. The
     `complete()` call on `/session` does take the lock, as every maintainer
     completion does, bounded by the two-slot redemption gate.
   - It refuses when the challenge secret does not exist yet, rather than
     inserting it, so an unauthenticated caller can never write.
   - Passkeys are enrolled with `residentKey: "preferred"`, so the options
     list the active credential ids. Those ids are not secrets.
3. **The browser signs.** The Console posts `{payload, mac, assertion}` to
   `/api/console/session`.
4. **The daemon verifies and creates the session.** Verification runs
   through the existing `complete()` path: the key is past quarantine, the
   sign count advances, and the nonce is spent once. The session then acts
   as the key's **sign-in principal**, with that principal's source.

**The sign-in principal** is a new column, `maintainer_passkeys.signs_in_as`
(v59).
- **How it is set.**
  - New enrolments record the bearer principal of the request that
    completed the enrolment.
  - The migration backfills it only from the key's own enrolment event: the
    `maintainer_key` audit event for that `credential_id` whose change is
    `enrol` (the bootstrap key) or `add` (a key approved by another key). It never uses a later `confirm`, `revoke` or `reset`
    event, which the host logs with an empty principal.
  - Where retention has pruned the enrolment event, the column stays empty.
  - No value is guessed from the daemon's environment, which neither a
    migration nor Rust's static schema plan can see.
- **How it changes.** Only through a signed action, **Use this passkey to
  sign in**, under a new challenge purpose, `bind-sign-in`.
  - It binds the key to the current Console session's principal, and only to
    an owner principal.
  - It is logged as a `maintainer_key` change.
  - Rebinding ends every session the key issued under its old principal, in
    the same transaction.
  - The recovery path runs through it. A key enrolled from a paired machine,
    or one whose enrolment event was pruned, is fixed by signing in once on
    the daemon's host with `pseudolife-mcp console` (an environment
    principal), then binding the key. From then on the passkey signs in with
    operator rights.
- **Refusal.** A key with no sign-in principal, or one whose principal no
  longer resolves, is refused with `passkey_signin_unavailable`, whose text
  names that recovery.
- **The maintainer principal.** The maintainer routes still refuse
  `maintainer` as a caller. A passkey session acts as an ordinary owner
  principal, never as `maintainer`.

**What this adds, stated plainly:** a passkey becomes a way to obtain a bank
bearer for up to 90 days. A synced passkey can do that from every device in
its sync group. The changelog and `docs/guide/security-posture.md` say so.

## 6. No new reserved principal names

Sessions act as existing principals, and nothing is added to
`RESERVED_PRINCIPALS`, so an environment principal of any name keeps working,
including one called `console`. A token pasted the old way still works, and
the dialog keeps "Paste a bearer token" under "Advanced".

## Install, update and the surfaces that change

- **Install on the daemon host:** after the health check, run
  `pseudolife-mcp console`.
  - Interactive installs open the browser, which is not a persistent
    change.
  - Unattended installs print the command in one line.
  - The passkey offer is unchanged.
- **Client-only install:** the end-of-install summary names
  `pseudolife-mcp console`.
- **Update:**
  - The v59 migration marks every stored principal as the owner's. Update
    says so in one line, which names `invite --someone-else` and
    `coordination.not_owner_principals` for a principal that is not.
  - When no Console session exists yet, it prints the sign-in command once.
  - Nothing is asked.
- **`maintainer setup`:** after moving the daemon to its HTTPS address, it
  runs the same link flow with the bearer it already reads
  (`maintainer_setup.bearer()`), so the Console opens signed in at the new
  address. This replaces "paste the token your Console uses at its usual
  address". With no bearer, it prints the command.
- **Console:**
  - The token dialog becomes a sign-in dialog, with three choices:
    - passkey, at the passkey origin;
    - sign-in code;
    - "Advanced: paste a bearer token".
  - A new `#/signin` route.
  - A 401 from a bearer-gated route, `/api/console/*` list and revoke
    included, opens it with "Sign in again". Only the two pre-auth routes,
    `/session` and `/passkey-challenge`, read 401, 404 or 501 as "this daemon
    cannot sign in yet", and keep the paste option. The Python daemon never
    answers 401 on those two, and a daemon without them never loops.
  - The bearer wording at `frontend/src/lib/board.ts` (the
    `usesConsoleToken` comment and `pendingText`'s "Hidden: uses another
    token") and the demo board in `web/fixtures.py` are updated with it.
  - Settings gains Signed-in browsers and "Use this passkey to sign in".
  - The empty-timeline text from #1141 points at sign-in.
- **Docs:** the README, `docs/guide/configuration.md`,
  `docs/guide/security-posture.md` and `llms-full.txt` replace every "paste
  the token" instruction with sign-in.

## Phasing

1. **Spec** (this PR, docs only). Its review carries the D10 exception for
   the routes, the CLI mode and the v59 bump.
2. **Schema v59 and owner scope** (Python).
   - **Schema.** One bump: `principals.owned`, `console_sessions`,
     `console_signin_codes` and `maintainer_passkeys.signs_in_as` with its
     backfill. The two new tables are excluded from exports and listed in
     `BENCH_RESET_TABLES`.
   - **Owner scope.** `coordination.not_owner_principals`, the rewritten
     owner-scoped snapshot with its perf review and measurement,
     `invite --someone-else`, and the owner column.
   - **Process.** It follows the `schema-bump` skill end to end: the Rust
     schema plan, the startup golden, the shim transfer literals, and the
     goldens on both OSes.
   - **Rust.** The owner-scope body change lands on a route the Rust daemon
     already serves (`GET /api/agents?view=coordination`). The same PR
     records a `daemon/divergences/` entry for it, and the board harness's
     `console` scenario gains an owner-scope case (two owner principals and
     one that is someone else's), marked as that divergence until Rust
     matches.
   - **Result.** From this step the Board shows your mail for any Console
     holding an owner token.
3. **Link sign-in** (Python and Console):
   - session resolution, revocation and the operator allowlist;
   - the `session` exclusion in `_authority`;
   - `/api/console/link` and `/api/console/session`;
   - `pseudolife-mcp console`, with `--list`, `--revoke` and `--no-open`;
   - the sign-in dialog, `#/signin` and Signed-in browsers.
4. **Passkey sign-in:** the `sign-in` and `bind-sign-in` purposes, the
   pre-auth challenge route, and the dialog and Settings options.
5. **Install, update, `maintainer setup` and docs**, so that no step is
   manual.

Rust follows each Python step in its own PR (the oracle leads, per
`rust/PORTING.md`). These rules apply until each lands:

- **Unported routes don't answer 501 in Rust.** Routes outside `routes.json`
  answer 401 from the bearer gate, or 404 once authenticated. Each step
  therefore records `daemon/divergences/` entries and parity rows naming the
  real answer. The Console treats 401, 404 and 501 from the two pre-auth
  routes as "this daemon cannot sign in yet".
- **`/session` and `/passkey-challenge` are pre-auth routes**, dispatched
  beside `/api/pair` in `http.rs`, outside `ConsoleRoutes`.
- **`/link`, list and revoke are bearer-gated routes.** If they join
  `ConsoleRoutes`, the route-count pin in `rust/daemon/src/routes/console.rs`
  moves and `routes.json` is re-recorded.
- **The shadow refuses every new POST.** No new GET joins `SHADOW_SAFE_GETS`
  without its audit.
- **`rust/shim` gains `console` as a deferred mode.**
- **G2 (the default switch) waits.** Before it, these must be ported:
  - session resolution, with its opt-in call sites;
  - the operator allowlist;
  - both sign-in routes.

  Otherwise switching daemons signs every browser out, or gives a session
  more than Python does.

## Not in scope

- More than one owner per bank, and any sharing model beyond "mine / not
  mine".
- Showing message bodies in the Console.
- Owner-wide mailbox access for agents.
- Console sessions as MCP credentials.

## Open questions for review

- **The lifetimes are conventions, not measured.** Should the 30-day sliding
  session lifetime, the 90-day absolute cap or the 5-minute code change?
- **When should a paired client hear about settings?** Should
  `pseudolife-mcp console` on a paired client warn up front that the Console
  it opens cannot change settings, or only say so in the Settings view?

## Review record

The first draft (f0ba16b7) had two reviews. Their findings are folded in
above.

- **Opus reviewer:**
  - sessions outliving their issuing credential;
  - the refuse-only operator gate;
  - an unsigned token in the passkey hand-off;
  - the owner-scope query returning one principal's events;
  - the origin check;
  - the per-code burn;
  - the Rust 501 claim;
  - the backfill event name and source;
  - maintainer-authority mail through sessions;
  - D10 covering the bump;
  - export exclusion;
  - the minor points.
- **Codex:**
  - the body cap rejecting RS256 assertions;
  - no path to operator rights for a passkey enrolled from a paired machine.

The Opus reviewer's re-check of 920f7029 closed those findings except one,
which it marked partly closed: excluding sessions inside `_authority` could
not be built. It also found new defects, all folded in:
- a global session lookup would have made sessions agent credentials on
  hooks, `/mcp` and `/api/coordination/*`;
- the backfill missed keys logged as `add`;
- 401 meant two things;
- the challenge route would have needed the coordination lock;
- an unnormalised stored origin;
- what a rebind does to existing sessions.
