# Coordination roles, stuck-lease guards and first-run onboarding

**Status:** design, 2026-11-02, revised 2026-11-03. The maintainer's
decisions of 2026-11-03: F2 and F3 are fixed now, in both daemons, by the
board stream that is changing the same lease and wake code; everything else
is held and built on the Rust daemon only, once it is the default (G2); the
recommendations on Q1 to Q4 below are accepted. Later the same day
(maintainer decision, 2026-11-03) F1 moved out of the hold to ship now in
the same open fix (#1182), so F1, F2 and F3 land together in both
daemons.

A team of agent sessions can stall on the board. A session holds a lease or
the coordinator role, then goes idle, crashes or forgets to release it, and
every session queued behind it waits, polls and messages the holder. A
session whose turn comes while it is between turns loses both the lease and
its place in the queue. This spec makes four changes. The conductor and
coordinator become roles that order the queue, never gates the queue waits
on. Guards catch a holder that is dead, dormant, hung or has left something
running. Role holders get a playbook for using resources and landing work
efficiently. The Console gets a first-run guide that teaches the roles and
helps the user pick the workflow that fits.

## The D10 freeze: held for Rust

`rust/CUTOVER.md` D10 is a soft Python freeze. Fixes continue, but there are
no new Python tools, routes, hooks or CLI modes, and pending schema changes
share one bump. The exception granted for the Console sign-in work (#1144)
covers that work only, and this spec asks for none.

F2 and F3 are fixes under D10 and land now in both daemons, with #1182.
F1 ships in the same PR (maintainer decision, 2026-11-03). Every other
item is built on the Rust daemon only once it is the default (G2, B17),
from the landing map in "Building it on Rust" below. Each item is marked by
kind:

- **fix:** it changes existing behaviour through existing surfaces;
- **surface:** it adds a tool action, route, CLI flag, config key or schema
  column.

| # | Item | Kind | Why |
|---|------|------|-----|
| F1 | Tell a waiter when the lease is granted to it | fix | One mail per grant from the daemon's existing reserved sender, decided `rung` with a new reason `lease_grant` (Q1); lands with #1182 (maintainer decision, 2026-11-03) |
| F2 | A missed grant goes back to the queue once instead of being dropped | fix | Internal to settling |
| F3 | Skip dead waiters at grant time | fix | Internal to settling; the signal is already stored |
| F4 | The coordinator role lasts only while its session can be reached | fix | Same claim call, different expiry rule; every input is already stored (§1.1) |
| F5 | Escalation notices for a stuck holder | fix | Existing daemon notices, existing stale marker |
| F6 | Board and Console flags for a stuck queue or a dormant coordinator | fix | Existing Board view and snapshot fields |
| F7 | Served instructions, role briefs and refusal text, the playbook included | fix | Text only |
| S1 | A role holder reorders a lease queue or names who goes next | surface | New `memory_agents` action and a waiter column (schema) |
| S2 | An ordering rule per lease (for example shortest expected job first) | surface | New stored setting and action |
| S3 | The conductor frees a stuck holder's session lease | surface | New authority; today only the operator can break a lease |
| S4 | `lease run` watchdog: `--max`, `--stall-after`, `--wait-children`, owner link | surface | New CLI flags |
| S5 | Resource probes: check the resource is really free before granting | surface | New config key |
| S6 | Workflow profile per project and the Console's `#/welcome` guide | surface | New stored setting, route and Console page |

The fixes alone remove most of the symptoms: no silent grants, no lost
places, no week-long dead waiters, no coordinator that keeps the role after its
session is gone.

## What a user should experience

You run several agent sessions on one project. Whichever workflow you use,
nothing waits on a session that has stopped:

- A session queued for a resource is told when its turn comes, wherever it
  is. It never polls, and it never loses its place because it was between
  turns.
- A session that holds something and then stops answering is noticed within
  minutes. The board names it and tells the role holder. If its session
  is gone, the daemon frees what it holds, as long as no process of its is
  still running.
- If a coordinator or conductor is active, it puts the most urgent or
  quickest job first, batches landings where that saves time and keeps
  sessions from colliding. If it goes quiet, the queue carries on in
  arrival order.
- The first time you open the Console after installing, it explains the
  roles and asks how your agents land work, then sets the board up for
  that.

## 1. The roles

What they are today (`docs/guide/configuration.md`, "lease conductor"):

| | Conductor (`conductor:<project>`) | Coordinator (`coordinator:<project>`) |
|---|---|---|
| Who gives it | The maintainer: CLI `lease conductor` or the Console with a passkey | Any session claims it for itself |
| How long it lasts | Up to 7 days; the session cannot renew or self-claim it | 1 hour per claim (`SESSION_LEASE_TTL`) |
| Authority | Its urgent mail reopens a `done` park | None ("grants nothing") |
| Help staying awake | Brief, per-prompt reminder, `conductor-watch`, reachability warning at grant | None |

The maintainer's intent (2026-11-02, 2026-11-03): both roles order the
team's work and decide who gets a scarce resource first, by need and overall
speed, and both look for ways to use resources and land work more
efficiently. The coordinator is the lesser role: organised by the agents
themselves, without the maintainer's delegation. A role holder stays
reachable for as long as it holds the role.

What changes:

- **The coordinator gets the conductor's staying-awake help, not its
  authority** (F4, F7): its own brief and reminder while it holds the role
  (the conductor's text says "the maintainer's conductor" and carries its
  authority, so it cannot be reused as is), `conductor-watch` offered to
  it, and a reachability check when it claims.
- **Both roles get the playbook** (§2.1, F7).
- **A role is never a gate** (S1, S2). Queues stay first come, first served
  by default. A role holder only reorders. If it goes quiet, arrival order
  continues.
- **The role is not a mutex for landing.** The served instructions (F7) and
  the warning a claimant without a wake path gets say: to serialise a merge, push or
  build, use `pseudolife-mcp lease run <name> -- <command>`. That holds the
  lease for exactly as long as the command runs.

### 1.1 Coordinator liveness (F4)

Today a coordinator claim lasts an hour whether or not its session is
reachable, and a session that claims again each turn holds it for good.

Two signals, because the stored state cannot tell every case apart. A
Claude Code session running a long foreground command forwards no tool
calls and has no Stop-hook listener until its turn ends. Its shim still
heartbeats, so it looks the same as a session sitting idle with no
listener.

- **Gone:** the session's attachment has lapsed (`lease_until` in the
  past), so its shim process has stopped. This is the only signal that
  frees anything automatically.
- **Quiet:** the attachment is live, but `_listener_path` reports no wake
  path (no armed `ring_armed_until`, no `wake_enabled` channel), and the
  session has forwarded no tool call within the wake policy's
  `active_seconds` (60 s). A quiet holder may be working in the
  foreground, so quiet only raises a flag (F6) and a notice (F5). It never
  frees anything by itself.
- **Rule:** the claim still carries the one-hour TTL. In addition, settling
  expires a coordinator hold whose holder has been gone for
  `COORDINATOR_UNREACHABLE_GRACE` (10 minutes). It is logged as
  `lease_expire` with reason `unreachable`.
- **Claim check:** the claiming call counts as activity, so a claim is
  never refused for liveness. When the claimant has no wake path, the
  answer carries a warning, as a conductor grant already does. The warning
  says how to arm a listener for the host (Stop hook, Codex doorbell,
  `wait-mail`) and names `lease run` for landing.
- **Near-miss tests:** a Claude Code session claims as the first call of a
  turn, before its Stop hook has armed the listener, and succeeds with the
  warning. A session running a 30-minute foreground build keeps the role.
  A session idle with its Stop hook listening keeps the role past the
  grace. A session whose shim process died loses the role after the grace.

## 2. Ordering the queue (S1, S2)

- **`memory_agents(action="order", lease=..., first=<agent id>,
  reason=...)`** moves one waiter to the head of the queue. It is open only
  to the live holder of `coordinator:<project>` or `conductor:<project>`,
  for that project's waiters (Q2). Each use is logged as `lease_order` with
  actor, waiter, old and new positions and the reason, and every waiter
  whose position changed gets a notice (F1's channel).
- **An ordering rule** (`action="order", lease=..., rule=...`):
  `arrival` (default), `shortest_expected` (by the waiter's `expect`), or
  `role` (only explicit moves). The daemon applies the rule at each grant,
  so speed does not depend on a role session waking up. A rule lasts as
  long as the role hold that set it. When that hold ends, the lease goes
  back to `arrival`.
- **What a role holder decides from:** a waiter's purpose and `expect`,
  which a claim already carries, plus an optional `unblocks` note (S1's
  column). The served instructions make purpose and `expect` routine.
- **Limits:** no one can reorder the current holder out. A waiter can be
  moved past at most `ORDER_MAX_SKIPS` times (3), so a waiter is never
  starved. Arrival order stays the only rule while no role is held.
- **Schema:** waiter columns `priority`, `unblocks` (text) and `skips`
  (for `ORDER_MAX_SKIPS`), plus where a lease's ordering rule is stored,
  all in one change to the Rust daemon's schema plan through the
  `schema-bump` skill.

### 2.1 The role playbook (F7)

The playbook tells a role holder how to get the most work landed per hour
with the main branch kept working, and no session waiting on another that
is not working. The rules, in the order a holder applies them:

1. **Keep the line moving.** A lease held by a session that is not using it
   is the first thing to fix: ask the holder, then escalate (§3.1). A role
   holder does not hold a work lease itself for long; the role orders work
   and `lease run` holds the resource.
2. **Shorten every hold.** Workers do everything they can before they queue
   and while they wait (rebase, build, targeted tests), so the landing lease
   covers only the final rebase, check and push, held by `lease run` so it
   ends with the command. A long `expect` on a landing lease gets a
   question.
3. **Order the queue.** First a job other sessions are blocked on
   (`unblocks`), then the shortest `expect`, which cuts total waiting most
   when jobs differ in length, otherwise arrival. Give a reason each time.
   Set the `shortest_expected` rule (S2) when a queue keeps forming, so the
   order holds while you are between turns.
4. **Batch when the check is expensive.** When several changes are ready and
   the check before landing is long (a full test suite, a CI run), land them
   together: combine them, check once, land. On a failure, find the culprit
   by bisection, return it to its session and land the rest. One full check
   then covers several changes.
5. **Prevent conflicts before they cost a landing.** Spread concurrent work
   over separate areas (`claim:<path>`). Land a change others depend on
   first. When two queued changes touch the same files, land one and have
   the other rebase and re-check before its turn.
6. **Use every equivalent resource.** When the same job can run on another
   free resource (another machine's suite lease), send it there instead of
   queueing.
7. **Fail fast.** A landing that fails releases at once and the next waiter
   goes; the failing session fixes off the lease and queues again. A broken
   main branch stops landing until it is fixed or the breaking change is
   reverted, and getting that done is the role holder's first job.
8. **Stay inside your authority.** The conductor acts within what the
   maintainer delegated. The coordinator orders the queue and asks. Neither
   takes a merge click, an approval or a release decision the human keeps.
9. **Stay reachable and cheap.** Keep the listener armed, hand raw output to
   a subagent, and answer a quiet wake in one line.
10. **Measure and adjust.** Check `board-audit stats` (#441) now and then:
    waits, hold times against `expect`, overruns and frees. Report to the
    maintainer what slowed the team.

**Delivery.** A short form (rules 1 to 4 and 7 to 9, about eight lines)
joins each role's brief, served once when the role is held. The per-prompt
reminder stays one line, because the conductor brief's context cost was
measured (2026-10-14, six orchestrating sessions) and an instruction
repeated every turn is paid on every turn. The full playbook is a section
of the coordination guide that the brief links to.

**What it should move, and where that is measured:** median wait from
queueing to grant, hold time against `expect`, the count of overruns and of
unreachable frees, each per lease from `board-audit stats`, compared over
two weeks before and after the playbook ships. The constant or text that a
measurement changes carries a comment naming it.

## 3. Guards: nothing waits on a stopped holder

Every guard decides on one exact signal and is tested against the
legitimate case that looks like the failure (the near-miss). The thresholds
are starting values, not measurements (Q3). `board-audit stats` (#441) is
where their effect is measured once they ship.

| Failure | Today | Guard | Signal | Near-miss that must still work |
|---|---|---|---|---|
| Waiter's turn comes while it is between turns | Grant is silent; the waiter is deleted and must claim again within 300 s or lose its place | **F1** a grant notice rings the waiter; **F2** a missed grant puts it back in the queue once, second in line | Grant event; grant window lapsed without a renewal | A waiter that renews within the window keeps the lease as now. Every other daemon notice stays `hinted` and rings nobody |
| Waiter died while queued | Removed only after 7 days idle (`AGENT_RETENTION`), or 1 hour for a `lease run` address; its turn wastes the grant window (up to 300 s, 120 s for a default `lease run`) | **F3** settling passes over it when granting but keeps its row and place, so a daemon restart that briefly makes live waiters look dead costs them one turn, never their place; the existing retention still removes it | Session waiter: attachment lapsed (`lease_until` past by `ATTACHMENT_LEASE`, 60 s, so at least 120 s without a heartbeat when the shim beats every 20 s) `lease run` waiter: no poll for 60 s (it polls every 5 s) | A live session idle between turns: its shim keeps renewing `lease_until`, so it is kept. A host stalled for under two minutes is kept. After a daemon restart, a head that looks dead is passed over once and is still first in line |
| Session holder dormant or dead (claimed lease) | Held to the TTL: 1 h, or 1 d for `claim:` | **F4** for the coordinator; **F5** escalation for others; **S3** conductor frees it | Holder gone (§1.1) while someone is queued; a quiet holder is only flagged | A quiet holder, such as a session running a long foreground command, is never freed automatically |
| Process holder hung (`lease run` alive but stuck) | Renewed every ttl/3 for as long as it runs; `expect` only marks it stale | **F5** escalation from the expected end; **S4** `--stall-after` notices and `--max` stops | Past the expected end; no output for `--stall-after` | A long build that keeps printing, with an honest `--expect` |
| Process holder crashed | OS lock frees at once; board lease lapses within the ttl (120 s default) | Unchanged | Lock released | |
| Holder left something running after release | Unguarded: a child that outlives the command runs without the lock (`lease_cli` docstring) | **S4** `--wait-children` (opt-in) holds the lease until the process tree exits; by default the release notice names the processes left running | Job object on Windows, process group on POSIX | `lease hold --while-pid` for a detached server, the supported pattern: never killed, never warned about |
| Lease free but resource still busy | Next holder collides | **S5** a per-lease busy probe runs before a grant; when busy, the grant waits and the last holder is told | Probe exit code | A probe that fails to run grants as now, with one warning line |
| Holder keeps renewing past its estimate | Stays stale silently | **F5** renewing a stale lease while others wait logs `lease_overrun` and notifies the role holder | Renewal after the expected end with a non-empty queue | Renewing with a new `expect` is a logged change, not an overrun |
| Sessions polling and messaging the holder | Only way to learn of a grant | **F1** removes the reason; **F7** the queued answer says "you will be told; park `needs_resource` with `park_clear_by` set to the lease; don't poll or message the holder" | | |

### 3.1 Escalation ladder (F5, F6, S3)

For a held lease with someone queued:

1. **Past the expected end:** the board marks it stale (already the case).
2. **Overdue** (twice the expected duration, or 15 minutes past the
   expected end if that comes later; a lease with no expected end starts at
   30 minutes): a notice to the holder, and to its owning session for a
   `lease run` (S4's owner link). It rings the holder if it is parked.
3. **Unanswered for 10 minutes, or the holder is gone:** a notice to
   the live role holder (conductor, otherwise coordinator) naming the lease,
   its holder and the evidence. The Console Board shows a stuck-queue
   banner.
4. **Then:**
   - a **session-held lease** whose holder is gone is freed by the
     daemon after `STUCK_FREE_GRACE` (15 minutes after step 3 is reached,
     whether or not a role holder is live), logged with reason
     `unreachable`. A quiet holder's lease is never freed automatically.
     The conductor can free either sooner (S3);
   - a **process-held lease** whose process is still running is never freed
     automatically, because the OS lock and the running work are real. The
     role holder or the maintainer decides, and `lease break` remains the
     operator's tool.

### 3.2 Owner link for `lease run` (S4)

A `lease run` registers its own short-lived board address, so today nobody
can tell which session started it. It gains an owner: the invoking
session's board address from the session's `.agent` file, the way
`conductor-watch` finds its owner, or `--owner`. Escalation and stall
notices go to the owner, and the Board shows the run under that session.

### 3.3 How F2 and F3 work without a schema change

- **F3** adds one condition to the query that picks the waiter to grant:
  the waiter's attachment is live (`lease_until` later than
  `now - ATTACHMENT_LEASE`) or it was active within `ATTACHMENT_LEASE`. A
  session's shim renews its attachment every 20 seconds. A `lease run` or
  `lease hold` waiter has no attachment, but it asks again every 5 seconds,
  which moves its `last_activity`. A wake listener needs a live
  attachment, so it needs no separate check.
- **The caller is always alive.** `acquire_lease` settles before it
  records the caller's activity, so the caller's own activity is recorded
  first. Otherwise a waiter whose last poll is old (after a daemon restart)
  would be passed over in its own call and then take the free lease
  directly, leaving its waiter row behind. The direct-grant path also
  deletes the caller's waiter row.
- **A newcomer can take a free lease ahead of waiters that only look
  dead.** Those waiters lose that turn but keep their place.
- **F2** reads the audit log instead of a new column. When a hold expires,
  it was an unclaimed grant if both of these hold:
  - a `lease_grant` event exists for that holder, lease and fence
    (`event='lease_grant'`, the holder's `agent_id`, `created_at` from the
    grant time on, with the name and fence read from the TEXT payload);
  - its expiry is still the grant time plus the grant's `window`.

  A later renewal moves the expiry past that point. A renewal made in the
  same call as the grant does not: settling grants the lease, and the
  renewal then uses the same `now`. So a `lease run` that crashes before
  its first renewal reads as a missed grant. The result is harmless: that
  dead waiter is passed over by F3 and removed after an hour.

  Once the next waiter is granted, the missed grantee is queued first
  among the remaining waiters. Its ticket is the lowest remaining ticket
  minus one, which is allowed because the ticket has no unique
  constraint. The requeue is logged as a daemon `lease_queue` event with
  reason `missed_grant`.

  A grant made from such a requeue carries `retry: true`. It is
  recognised by a daemon `missed_grant` event whose `created_at` equals
  the waiter's `enqueued_at`. A second miss is not requeued. If nobody
  else is queued, or F3 passes over every remaining waiter, nothing is
  granted and the lease is simply free.

  Every existing event payload stays byte-identical; the new keys appear
  only on the new paths.
- **F2 needs F1:** without F1 a retry grant is as silent as the first
  one, so an idle session can miss it too and F2 alone buys a sleeping
  waiter one more turn, not a guarantee. This is why F1 ships in the
  same fix (#1182).

## 4. Workflow profiles and onboarding (S6)

**Where:** the Console sign-in work (#1144) is open as stacked PRs: #1164
(`pseudolife-mcp console` with a one-time sign-in link), #1166 (passkey
sign-in) and #1168 (install, update and maintainer setup open the Console
signed in). This guide uses that one browser opening and adds none. Agreed
with that session on 2026-11-02 and 2026-11-03:

- `pseudolife-mcp console --to welcome` appends `&to=welcome` to the
  sign-in link. The Console honours `to` only for a page id in its
  navigation list, so S6 adds `welcome` there;
- install runs `pseudolife-mcp console --unless-signed-in` and passes no
  `--to` today. Once the guide ships, both installers' "console sign-in"
  block passes `--to welcome` until the guide is finished, a one-word
  change;
- whether the owner has finished is stored per bank in the `meta` row
  `console_first_run` = `{completed_at, version}`, so a rerun opens nothing
  and says so in one line. None of the sign-in PRs adds a route that writes
  it, so S6 adds one, gated like the other operator routes.

**The guide (`#/welcome`)** has four short screens:

1. **Sessions and the board:** what a session, the board, mail and a lease
   are, in one diagram.
2. **The roles:** the conductor (given by you, acts for you) and the
   coordinator (taken by the agents, orders the work), that both look for
   faster ways to use resources and land work, and that neither ever blocks
   the queue.
3. **How do your agents land work?** The answer sets the project's profile:
   - **Solo:** one session at a time. Nothing to set up.
   - **Reviewed pull requests with your merge click:** the guide offers to
     grant a conductor once a session is running. Workers open PRs.
   - **Many sessions landing directly on the main branch** (for example a
     dispatcher that starts one session per issue): no role needed. Landing
     goes through `lease run land:<project>`. The guide shows a landing
     script template (fetch, rebase, build and test, push; exit non-zero
     on conflict) and a line to paste into the dispatcher's prompt.
   - **Not sure:** arrival-order defaults with every guard on, which is
     safe for any workflow.
4. **Done:** where to see the board and how to change the profile later
   (Settings).

**What agents see:** agents never open the Console, so the profile is
stored per project on the daemon (`coordination_profile:<project>` in
`meta`) and changes the instructions the daemon serves and the session
check-in text for that project. In the direct-landing profile, a
coordinator claim answers with the `lease run` landing recipe. In the PR
profile, the check-in names the conductor. Every guard in §3 applies in
every profile, including no profile.

## Building it on Rust

The lease, mail and wake logic already exists in the Rust daemon, so each
item has a home. Each change moves the board harness goldens it reaches
(`rust/daemon/harness/board/`), and an item that the Python oracle does not
also get is recorded as a file under `rust/daemon/divergences/` with its parity rows,
per `rust/PORTING.md`.

| Item | Where it lands |
|------|----------------|
| Prerequisite | The conductor's per-prompt reminder (`/api/hook/memory-changes?agent=`) is not ported (`rust/daemon/src/routes/hook.rs`, declared in `divergences.md`). The role briefs and playbook reach a holder through that path and the check-in brief, so they are ported first |
| F1, F2, F3 | Landing with #1182 in both daemons, not held for G2 |
| F4 | `board/lease.rs` (expiry in `settle`) and `board/mcp.rs` (claim check, `SESSION_LEASE_TTL`) |
| F5 | `board/lease.rs` (`settle_due`) and `board/notice.rs` |
| F6 | `board/console.rs` and the Console's Board view |
| F7 | The served instructions and check-in text, the role briefs (`routes/hook.rs`), refusal texts in `board/error.rs` |
| S1, S2 | `board/mcp.rs` (the `order` action), `board/lease.rs` (applying the rule), the waiter column in the schema plan (`schema-bump` skill) |
| S3 | `board/mcp.rs`, with the authority check beside the conductor's in `maintainer_ops/roles.rs` |
| S4 | `rust/shim/src/cli/lease/run.rs` and `args.rs` |
| S5 | `board/lease.rs` and the daemon config |
| S6 | The Console's navigation list and the `welcome` page, the `console_first_run` route, both installers' "console sign-in" block |

Each item ships as its own PR with a watched-RED test and its near-miss
test from §3.

## Phasing

1. **This spec** (docs only).
2. **Now:** F1, F2 and F3 in both daemons, landing with the board
   stream's PR (#1182).
3. **Held until G2, then Rust only:** the reminder port (prerequisite),
   F4, F5 and F6, then F7 with the playbook.
4. **Then surfaces:** S1 and S2, S3, S4, S5.
5. **Onboarding (S6):** once the sign-in PRs (#1164, #1166, #1168) have
   merged.

## Decisions on the open questions

The maintainer accepted each recommendation on 2026-11-03.

- **Q1. Who sends the grant notice?** The first draft said the daemon sends
  no board mail. That was wrong: it has a reserved sender (`daemon`) for
  its notices (dream stalls, `POST /api/daemon-notice`), in both daemons
  (`board/notice.rs` in Rust). Its notices are always decided `hinted`, so
  they never ring. **Decided:** the grant notice is a daemon notice to
  the grantee only, decided `rung` with a new reason `lease_grant`, so it
  rings the grantee's listener whatever its park record says. Both shims
  ring only on `rung` or `attention` (`rust/shim/src/board/adapter.rs`,
  `doorbell.rs`, and their Python counterparts), so a new decision value
  would never ring. No new origin, no new column, and no shim change. The near-miss test is every
  other daemon notice staying `hinted`.
  **Delivery, as built in #1182 (maintainer decision, 2026-11-03, to
  ship F1 now):** the notice is agent-origin mail from the
  reserved daemon sender, so it conveys no user approval. It is sent once
  per grant fence; that deduplication bounds the notices, so a grant ring
  gets no wake cap of its own. The ordinary queue and send limits still
  apply. Its wake is not ordinary peer mail: `lease_grant` joins the
  privileged wake reasons (`PRIVILEGED_WAKE_REASONS` in
  `rust/daemon/src/board/wake.rs`, `_PRIVILEGED_WAKE_REASONS` in Python),
  so while the grant is still current and unaccepted it is served ahead of
  peer mail and passes a park's reopen filter, as maintainer and conductor
  mail does. It lands with #1182 (open), with F2 and F3.
- **Q2. Scope of a project role over machine-wide leases** such as
  `full-suite@<box>`. **Decided: (a)**, a project role reorders only
  its own project's waiters among themselves. Both roles are scoped to a
  project (`<role>:<project>`) and nothing holds a machine-wide role, so
  nobody has the standing to reorder one project's waiters against
  another's. A machine-wide lease therefore stays in arrival order across
  projects, even while a conductor is active.
- **Q3. The thresholds.** **Decided:** keep the starting values, and
  tie each one to the constant it depends on so it moves with that
  constant: the dead-waiter grace is `ATTACHMENT_LEASE` (60 s, three of
  the shim's 20 s heartbeats, so a waiter is passed over only after at least
  two minutes without one); the coordinator's grace after its session is gone is 10
  minutes; overdue is the later of twice `expect` and `expect` plus
  15 minutes, or 30 minutes with no `expect`; the free grace is 15 minutes
  after the role holder was told. Each constant carries a comment saying it
  is a convention, and `board-audit stats` decides any retuning after two
  weeks of use.
- **Q4. Do flags on the existing `lease run` mode count as new surfaces
  under D10?** Moot under the hold: S4 lands in the Rust shim after G2 and
  needs no exception.

## Not in scope

- Preempting a running process to give its lease to a more urgent waiter.
- More than one coordinator or conductor per project.
- Cross-bank coordination.
- A machine-wide role that orders leases across projects.
