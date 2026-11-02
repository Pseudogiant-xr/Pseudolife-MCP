# Console sign-in and owner-scoped board visibility

**Status:** design, 2026-11-02. The maintainer decided: every principal is the
bank owner's unless invited as someone else's, and the Console signs in both
with a one-time link from the CLI and with the maintainer passkey. New Python
routes and a new CLI mode need the D10 exception in `rust/CUTOVER.md`, which the
maintainer grants or refuses on this spec's PR before any implementation PR
opens.

## What a user should experience

You install Pseudolife, and at the end the installer opens the Console in
your browser, already signed in. The Board lists every session you run, on
every machine, and the mail and wake timeline shows their mail. Later, on
another device, you open the Console's HTTPS address and sign in with the
passkey you already use for signed messages. You never see the words "bearer
token" unless you look for them.

Today none of that holds:

1. The Console asks you to paste a bearer token on first open, and again at
   every new address, because browsers keep it per origin. The docs and
   `maintainer setup` tell you where the token file lives.
2. The Board's timeline and unread counts follow the exact principal of that
   token. The installer recommends one principal per machine (pairing codes),
   and a Console may have its own, so one person ends up with several
   principals and their Console sees none of their sessions' mail. The empty
   timeline now says why (#1141), but it cannot fix it.
3. Nothing in the bank records which principals belong to the same person:
   the `principals` table holds a tier, a board flag and a token hash.

## Design

### 1. Ownership of principals

A principal is either the **owner's** or **someone else's**. The bank has one
owner: the person who runs the daemon.

- **Environment principals** (`PSEUDOLIFE_MCP_TOKEN`, every
  `PSEUDOLIFE_MCP_TOKENS` entry) are the owner's: only the person editing the
  daemon's environment can create them.
- **Stored principals** (the `principals` table, made by `invite` and
  redeemed by pairing) gain a column `owned BOOLEAN NOT NULL DEFAULT TRUE`.
  The v59 migration sets it true on every existing row, so existing installs
  become owner-scoped with no step.
- `pseudolife-mcp invite <name> --someone-else` creates a principal with
  `owned = false`. The invite output names the flag in one line ("for a
  machine that is not yours, add --someone-else"). There is no flag for the
  default.
- The reserved `maintainer` principal (passkey-signed mail) counts as the
  owner's for visibility. The reserved `daemon` principal does not.
- `invite --list` shows an "owner" column.

### 2. What ownership changes

**Only the Console's board snapshot widens.** `console_snapshot` resolves the
caller's principal. If that principal is the owner's, the snapshot uses the
set of all owner principals (environment names, owned stored rows that are
not revoked, and `maintainer`) instead of the single name.

- The visible-mail CTE, the event candidates and the unread counts compare
  `principal = ANY(%s)` instead of one name. Unread counts become non-null for
  every owner session.
- A principal that is someone else's keeps today's per-principal scope
  exactly.
- Message bodies stay excluded, as today.
- Mailbox access is unchanged: `_auth` (receive, ack, rebind, park gates)
  and `memory_message history` stay bound to the exact principal. An agent
  cannot read another agent's mailbox because both belong to the owner.

**The trust expansion, stated once:** any bearer of an owner principal (an
agent's token on any of the owner's machines) can read board metadata for
all the owner's sessions through `GET /api/agents?view=coordination`. That
metadata is who mailed whom, when, the wake decision and the delivery path.
Those tokens already read and write the whole bank, so this adds nothing a
bank read does not already reveal.

### 3. Console sessions

A **Console session** is a bearer the browser holds in localStorage
(`pl_token`, as today) that resolves to an existing principal. The Console
keeps sending `Authorization: Bearer …`, so no cookie is introduced and the
CSRF model for `/api/*` (an Authorization header cannot be attached
cross-origin) is unchanged.

**New table** (v59) `console_sessions`:
- `session_id`, primary key;
- `token_hash`, unique, the SHA-256 of the browser-minted token;
- `principal`, the principal it acts as;
- `source`: `environment` or `store`, which decides operator rights;
- `method`: `link` or `passkey`;
- `label`: a short browser description, for the list;
- `created_at`, `last_used_at`, `expires_at`, `revoked_at`.

**Resolution** (`principals.resolve_principal_detailed`, and
`auth::resolve_detailed` in Rust): after environment tokens and stored
principals, look the bearer's hash up in a session snapshot, refreshed like
the stored-principal snapshot (10 s, available for 60 s) and added to
immediately on creation. A hit resolves to the session's principal, with a
new source, `session`, that carries the issuing source.

A session stops resolving when it is revoked or expired, or when its
principal no longer resolves on its own: an environment name dropped from the
token map, or a stored row revoked.

**Rights:**
- A session has exactly the rights of the principal it acts as, including
  board admission and owner scope.
- The operator gate (`_OPERATOR_POST_PATHS`) admits a session only when its
  source is `environment`. A Console signed in from a paired client
  therefore cannot change daemon settings, as a pairing code must not grant
  operator rights (2026-10-02 decision). Its Settings view says so and names
  the way to get them: sign in from the daemon's host, or with the passkey.
- Maintainer routes treat a session like its principal. Every maintainer
  action still needs its own passkey assertion.

**Lifetime:** 30 days from last use, ended early by revocation.
- The Console's Settings view lists signed-in browsers (label, method,
  created, last used, which principal) and revokes them.
- `pseudolife-mcp console --list` and `--revoke <id>` do the same from the
  host.
- Revoking a principal revokes its sessions.

### 4. Sign-in with a one-time link

`pseudolife-mcp console` (new CLI mode) opens the Console signed in.

1. The CLI finds the daemon URL and token as every client command does
   (`PSEUDOLIFE_MCP_DAEMON_URL`, `PSEUDOLIFE_MCP_TOKEN_FILE`, else the client
   registrations `doctor` reads).
2. It calls `POST /api/console/link` with that bearer. The daemon creates a
   one-time sign-in code in `console_signin_codes` (v59: `code_hash`,
   `principal`, `source`, `expires_at`, `used_at`, `failed_attempts`), valid
   for 5 minutes, single use. The code is bound to the calling principal and
   its source, so a link carries exactly the rights of the token that made
   it.
3. The daemon answers with the code and the URL to open:
   - `coordination.maintainer.origin` when passkeys are set up, so the
     session lands on the passkey address;
   - otherwise the origin the CLI reached.

   The URL carries the code in the fragment (`<origin>/ui/#/signin?code=…`),
   which browsers never send to a server, so it stays out of request logs
   and proxies.
4. The CLI opens the URL with the system browser. It always prints the URL
   and the short code, so a headless host works: open the URL on any device
   that reaches the daemon, or type the code into the Console's sign-in
   dialog.
5. The Console reads the code from the fragment and removes it from the
   address bar and history (`history.replaceState`). It then mints a random
   token, stores it, and calls `POST /api/console/session` with
   `{code, token_sha256, label}`.
6. The daemon redeems the code and creates the session. The Console reloads
   signed in.

**`POST /api/console/session` is the second pre-auth route** after
`/api/pair`. It is browser-called, so `/api/pair`'s "no `Origin`" rule cannot
apply. It requires instead:
- an `Origin` header equal to the request's own origin (scheme, host, port),
  and when passkeys are configured, also equal to the configured origin or
  the loopback origin;
- `Content-Type: application/json`, which forces a preflight, and no CORS
  headers are served;
- a body capped at 1 KiB, holding exactly the documented keys;
- one uniform refusal (`signin_refused`) for every bad code;
- the pair route's limiter shape: a global failure budget, two concurrent
  redemptions, and a code burnt after five wrong attempts.

The browser mints the credential and sends only its hash, as `pair` does, so
the token never crosses the wire.

**Where the code is exposed:**
- browser history, for the moment before `replaceState` runs;
- the terminal that printed it;
- the screen.

It is single use and lasts 5 minutes, which bounds all three.

### 5. Sign-in with the passkey

At the configured passkey origin, the sign-in dialog offers "Sign in with your
passkey".

1. `POST /api/console/passkey-challenge` (pre-auth, same Origin and
   content-type rules) returns WebAuthn request options for a new challenge
   purpose, `sign-in`. These are the existing MAC'd payload, nonce and
   120-second expiry.
   - Passkeys are enrolled with `residentKey: "preferred"`, so the options
     list the active credential ids (`allowCredentials`). Those ids are not
     secrets, and the route is reachable only where the daemon is.
2. The browser signs. The Console mints a token and posts
   `{payload, mac, assertion, token_sha256, label}` to
   `/api/console/session`.
3. The daemon verifies the assertion through the existing `complete()` path:
   quarantine passed, sign count advances, nonce spent once. It then creates
   a session that acts as the passkey's **sign-in principal**.

**The sign-in principal** is a new column on `maintainer_passkeys` (v59),
`signs_in_as`. It holds the bearer principal that enrolled the key.
- New enrolments record the principal of the request that completed the
  enrolment.
- The migration backfills it from the key's `key_change` audit event, which
  already records that principal.
- A key enrolled from the host (`path = host`, empty principal) gets the
  singular `default` principal when one exists. Otherwise it is left empty.

A passkey with no sign-in principal, or one whose principal no longer
resolves, is refused with `passkey_signin_unavailable`. The refusal says to
sign in once with `pseudolife-mcp console`, which sets the key's sign-in
principal from that session at its next use.

The maintainer routes still refuse the `maintainer` principal as a caller. A
passkey session acts as an ordinary owner principal, never as `maintainer`,
so no reserved name changes meaning.

### 6. No new reserved principal names

Sessions act as existing principals. Nothing is added to
`RESERVED_PRINCIPALS`, so an existing environment principal of any name,
including one called `console`, keeps working. A Console token pasted the old
way still works, and the dialog keeps "Paste a bearer token" under an
"Advanced" heading.

## Install, update and the surfaces that change

- **Install (daemon host):** after the health check, run
  `pseudolife-mcp console`.
  - An interactive install opens the browser, since that is not a persistent
    change.
  - An unattended install prints the command in one line.
  - The passkey offer is unchanged.
- **Install (client only):** the end-of-install summary names
  `pseudolife-mcp console`.
- **Update:** the v59 migration marks every stored principal the owner's,
  and update prints that once ("Console: every paired machine counts as
  yours; …").
  - When no Console session exists yet, it prints the sign-in command once.
  - Nothing is asked.
- **`maintainer setup`:** after moving the daemon to its HTTPS address, it
  prints and opens a sign-in link for that address (host access issues the
  code directly, as `enrol-code` does). This replaces "paste the token your
  Console uses at its usual address", the circularity that started this.
- **Console:**
  - The token dialog becomes a sign-in dialog: passkey (when available),
    sign-in code, and "Advanced: paste a bearer token".
  - A 401 opens it with "Sign in again".
  - Settings gains "Signed-in browsers".
  - The empty-timeline text from #1141 points at sign-in instead of the
    token.
- **Docs:** the README, `docs/guide/configuration.md` and `llms-full.txt`
  replace every "paste the token" instruction with sign-in.

## Phasing

1. **Spec** (this PR, docs-only). Its review carries the D10 decision.
2. **Schema v59 and owner scope** (Python):
   - one bump, covering `principals.owned`, `console_sessions`,
     `console_signin_codes` and `maintainer_passkeys.signs_in_as` with its
     backfill;
   - owner-scoped `console_snapshot`;
   - `invite --someone-else` and the owner column.

   It follows the `schema-bump` skill end to end, including the Rust
   schema-plan regeneration, the startup golden, the shim transfer literals
   and the goldens on both OSes. The Board starts showing your mail at this
   step, for any Console holding an owner token.
3. **Link sign-in** (Python and Console):
   - session resolution and the operator rule;
   - `/api/console/link` and `/api/console/session`;
   - `pseudolife-mcp console`, list and revoke;
   - the sign-in dialog and Signed-in browsers.
4. **Passkey sign-in:** the `sign-in` challenge purpose, the pre-auth
   challenge route, and the dialog option.
5. **Install, update, `maintainer setup` and docs**, so that no step is
   manual.

Rust follows each Python step in its own PR (the oracle leads, per
`rust/PORTING.md`). Until each lands:
- new routes answer the declared 501 with a `daemon/divergences/` entry;
- the shadow refuses them (POSTs already; no new GET is added to
  `SHADOW_SAFE_GETS` without audit);
- `/api/console/*` sits outside `ConsoleRoutes`, as `/api/pair` does, so its
  parity rows record that `routes.json` does not list it;
- the board harness's `console` scenario gains an owner-scope case with two
  owner principals and a guest;
- `rust/shim` gains `console` as a deferred mode until ported.

## Not in scope

- More than one owner per bank, and any sharing model beyond "mine / not
  mine".
- Showing message bodies in the Console.
- Owner-wide mailbox access for agents.

## Open questions for review

- The 30-day sliding session lifetime and the 5-minute code lifetime are
  conventions, not measured values. Should either change?
- Should `pseudolife-mcp console` on a paired client (store source) warn up
  front that the Console it opens cannot change settings, or only say so on
  the Settings view?
