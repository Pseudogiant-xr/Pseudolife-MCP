# Board roles from the Console: delegate and coordinator, proven by a passkey

Status: design, 2026-10-04. An addendum to
`2026-10-02-maintainer-wake-design.md` (the maintainer-messages spec), which
this change implements in full, plus role changes and the Console UI.
Maintainer decisions (2026-10-04): the Board gets a pinned "Roles" band per
project with a delegate slot and a coordinator slot (canvas artboard "Board
roles: pinned band"); every session has Make/Revoke buttons for both roles;
the maintainer can message the delegate and the coordinator from the Board;
the proof is a passkey, as in the maintainer-messages spec. Schema v54
(master is v53; the earlier spec's v55 numbering assumed two bumps that did
not land in that order).

## What changes beyond the maintainer-messages spec

### 1. Role changes are signed purposes

Four new challenge purposes, each completed at one route, `POST
/api/maintainer/role {payload, mac, assertion}`. Every input that decides the
outcome is in the signed payload:

| purpose | payload fields | effect |
|---|---|---|
| `grant-delegate` | `project`, `agent_id` (full id), `hold` (seconds, 60..604800) | `grant_delegate(project, agent_id, hold=hold, actor="maintainer")`: replaces any current delegate; also used to extend (same agent, new hold from now) |
| `revoke-delegate` | `project` | `break_lease("delegate:" + project, actor="maintainer")` |
| `assign-coordinator` | `project`, `agent_id`, `hold` | new `assign_coordinator(project, agent_id, hold=hold, actor="maintainer")`: vacates `coordinator:<project>` (logged), grants it to the agent; an ordinary lease afterwards (the holder renews or releases it; others queue) |
| `revoke-coordinator` | `project` | `break_lease("coordinator:" + project, actor="maintainer")`: the next queued waiter, if any, takes it |

- Why coordinator changes are signed too, although the role carries no
  authority: any bearer-callable route that frees `coordinator:<project>`
  would let one session evict another's coordinator. Claiming a free
  coordinator lease stays an ordinary agent action, unchanged.
- A session holds at most one of the two roles at a time: granting the
  delegate to the current coordinator also breaks its coordinator lease in
  the same transaction, and assigning the coordinator to the current delegate
  also breaks its delegate lease. Both are logged.
- `_authority` accepts the `lease_delegate` record when its actor is
  `operator` **or** `maintainer`; nothing else changes there (the fence match
  still proves the daemon wrote it). Only the passkey route writes actor
  `maintainer`.
- The recipient must exist, be registered, not revoked, not a reserved row
  and not a subagent row (as for a send). `project` must equal the
  recipient's project exactly: the Console offers roles per project and a
  role on another project's session would never be read.
- Answers: grant/assign `{name, agent_id, fence, expires_at, replaced,
  also_broken?}`; revoke `{name, broken, was_held_by}`.
- The preview in the challenge answer (as for a send) names the recipient,
  the role, the project and who is replaced, so the Console can show it
  before the prompt.

### 2. HTTPS, with one exception for local use

The maintainer-messages spec requires HTTPS at a fixed name. Browsers also
treat `http://localhost` as a secure context, so `coordination.maintainer.origin`
may be `http://localhost:<port>` with `rp_id` `localhost` (exact host
`localhost`, nothing else over plain HTTP). Any other non-https origin is a
configuration error and the routes answer `409 maintainer_https_required`.

### 3. Content-Security-Policy on every `/ui/` response

`default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:; font-src 'self'; connect-src 'self';
object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`,
plus `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.

- `style-src 'unsafe-inline'` is needed: Svelte and the vendored 3D engine set
  style attributes. Script is the attack the policy blocks.
- The Console must therefore carry no inline `<script>`: the theme bootstrap
  in `frontend/index.html` moves to a file.

### 4. Service methods (the route contract; the fixture service implements the same names)

Routes in `web/routes.py` call these on the service, one each:

| route | service method |
|---|---|
| `GET /api/maintainer` | `maintainer_status()` |
| `POST /api/maintainer/challenge` | `maintainer_challenge(body)` |
| `POST /api/maintainer/enrol` | `maintainer_enrol(body)` |
| `POST /api/maintainer/send` | `maintainer_send(body)` |
| `POST /api/maintainer/role` | `maintainer_role(body)` |
| `POST /api/maintainer/cancel` | `maintainer_cancel(body)` |
| `POST /api/maintainer/revoke` | `maintainer_revoke(body)` |
| `POST /api/maintainer/repudiate` | `maintainer_repudiate(body)` |
| `GET /api/maintainer/sent` | `maintainer_sent(limit)` |
| `GET /api/maintainer/inbox` | `maintainer_inbox(limit)` |

Shapes are the maintainer-messages spec's "Console contract" table, plus:

- `GET /api/maintainer` adds `roles: {<project>: {delegate: {agent_id,
  expires_at, granted_by: "operator"|"maintainer"} | null, coordinator:
  {agent_id, expires_at} | null}}` for every project with a live role lease,
  so the band does not need to infer roles from the lease list.
- `POST /api/maintainer/challenge` accepts the four role purposes with the
  payload fields above, and answers `{payload, mac, publicKey, preview}`.
- `publicKey` is the `PublicKeyCredentialRequestOptions` (or, for enrolment,
  `CreationOptions`) as JSON with base64url strings for every byte field; the
  Console decodes them.

### 5. The Console

- The Board gains the Roles band (artboard "Board roles: pinned band"): a
  project picker; the delegate slot (holder, status, time left, Message,
  Extend, Revoke); the coordinator slot (holder, status, Message, Revoke);
  empty states; Make/Revoke delegate and Make/Revoke coordinator on every
  session card.
- Every role change and every message runs: challenge (shows the preview in
  the confirm dialog), `navigator.credentials.get` (the tap), complete.
  Grant and extend ask for a duration (1 h, 8 h, 24 h, 3 days, 7 days).
- Messages to the delegate or coordinator are maintainer sends to that
  agent; the slot shows the recent thread from `sent` and `inbox`.
- Settings gains "Your passkeys": the list, the bootstrap flow (enter the
  one-time code from `pseudolife-mcp maintainer enrol-code`), adding another
  key, cancelling a quarantined key, revoking the current one.
- Unavailable states say exactly why: HTTPS not configured (with the
  `tailscale serve --https` line), no passkey enrolled (with the enrol-code
  command), the browser has no WebAuthn.

## Tests (in addition to the maintainer-messages spec's)

- Each role purpose: happy path; wrong purpose at each route; payload edited
  under a kept MAC; recipient on another project; subagent or reserved
  recipient; hold out of range.
- A bearer-only path cannot change a role (MCP `memory_agents claim` on
  `delegate:<project>` stays `reserved_lease`; no REST route without an
  assertion reaches `grant_delegate`, `assign_coordinator` or `break_lease`).
- `_authority` honours a maintainer-granted delegate and an operator-granted
  one, and still refuses a plain hold without the audit record.
- One-role rule: granting delegate to the coordinator breaks the coordinator
  lease, and the reverse.
- The CSP and frame headers on `/ui/` responses; the built `index.html`
  carries no inline script.
