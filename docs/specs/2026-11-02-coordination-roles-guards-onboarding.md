# Coordination roles, stuck-lease guards and first-run onboarding

**Status:** design, 2026-11-02. Not reviewed yet. Nothing here is built.

A team of agent sessions can stall on the board. A session holds a lease or
the coordinator role, then goes idle, crashes or forgets to release it, and
every session queued behind it waits, polls and messages the holder. A
session whose turn comes while it is between turns loses both the lease and
its place in the queue. This spec makes three changes. The conductor and
coordinator become roles that order the queue, never gates the queue waits
on. Guards catch a holder that is dead, dormant, hung or has left something
running. The Console gets a first-run guide that teaches the roles and helps
the user pick the workflow that fits.

## Decision needed: the D10 freeze

`rust/CUTOVER.md` D10 is a soft Python freeze. Fixes continue, but there are
no new Python tools, routes, hooks or CLI modes, and pending schema changes
share one bump. The exception granted for the Console sign-in work (#1144)
covers that work only. Each item below is marked as one of:

- **fix:** it changes existing behaviour through existing surfaces, so it
  lands under D10;
- **surface:** it adds a tool action, route, CLI flag, config key or schema
  column, so it needs the maintainer's exception or waits for the Rust
  daemon.

| # | Item | Kind | Why |
|---|------|------|-----|
| F1 | Tell a waiter when the lease is granted to it | fix, see Q1 | It uses existing board mail. The daemon sends no mail today, though (Q1) |
| F2 | A missed grant goes back to the queue once instead of being dropped | fix | Internal to `_settle` |
| F3 | Skip dead waiters at grant time | fix | Internal to `_settle`; the signal is already stored |
| F4 | The coordinator role lasts only while its session can be reached | fix | Same claim call, different expiry rule |
| F5 | Escalation notices for a stuck holder | fix | Existing mail, existing stale marker |
| F6 | Board and Console flags for a stuck queue or a dormant coordinator | fix | Existing Board view and snapshot fields |
| F7 | Served instructions and refusal text for roles and landing | fix | Text only |
| S1 | A role holder reorders a lease queue or names who goes next | surface | New `memory_agents` action and a waiter column (schema) |
| S2 | An ordering rule per lease (for example shortest expected job first) | surface | New stored setting and action |
| S3 | The conductor frees an unreachable holder's session lease | surface | New authority; today only the operator can break a lease |
| S4 | `lease run` watchdog: `--max`, `--stall-after`, `--wait-children`, owner link | surface | New CLI flags (the maintainer rules whether flags on an existing mode count) |
| S5 | Resource probes: check the resource is really free before granting | surface | New config key |
| S6 | Workflow profile per project and the Console's `#/welcome` guide | surface | New stored setting and route; needs #1144 steps 3 and 5 |

**Phase 1 is F1 to F7 and needs no exception.** It removes most of the
symptoms on its own: no silent grants, no lost places, no week-long dead
waiters, no coordinator that holds the role while unreachable.

Rust follows every step, as #1144 does: the oracle leads per
`rust/PORTING.md`. The lease logic lives in `rust/daemon/src/board/lease.rs`
(grant window) and `rust/daemon/src/board/mcp.rs` (claim TTLs). Any change to
`_settle` moves the board harness goldens, so each Python PR either ships its
Rust twin or records a `daemon/divergences/` entry and parity rows.

## What a user should experience

You run several agent sessions on one project. Whichever workflow you use,
nothing waits on a session that has stopped:

- A session queued for a resource is told when its turn comes, wherever it
  is. It never polls, and it never loses its place because it was between
  turns.
- A session that holds something and then stops answering is noticed within
  minutes. The board names it and tells the role holder. If nobody can
  reach it, the daemon frees what it holds, as long as no process of its is
  still running.
- If a coordinator or conductor is active, it can put the most urgent or
  quickest job first. If it goes quiet, the queue carries on in arrival
  order.
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

The maintainer's intent (2026-11-02): both roles order the team's work and
decide who gets a scarce resource first, by need and overall speed. The
coordinator is the lesser role: organised by the agents themselves, without
the maintainer's delegation. A role holder stays reachable for as long as it
holds the role.

What changes:

- **The coordinator gets the conductor's staying-awake help, not its
  authority** (F4, F7): the same brief and reminder when it holds the role,
  `conductor-watch` offered to it, and a reachability check when it claims.
- **A role is never a gate** (S1, S2). Queues stay first come, first served
  by default. A role holder only reorders. If it goes quiet, arrival order
  continues.
- **The role is not a mutex for landing.** The served instructions (F7) and
  the refusal for an unreachable claimant say: to serialise a merge, push or
  build, use `pseudolife-mcp lease run <name> -- <command>`. That holds the
  lease for exactly as long as the command runs.

### 1.1 Coordinator liveness (F4)

Today a coordinator claim lasts an hour whether or not its session is
reachable, and a session that claims again each turn holds it for good.

- **Signal: reachable.** The session has a live attachment (`lease_until`
  in the future), and either it is mid-turn (a tool call forwarded since its
  last heartbeat) or its wake listener is armed (`ring_armed_until` in the
  future). `_listener_path` already computes the listener half, and the
  mid-turn half is new. This is not the heartbeat's `active` flag: a session waiting on a
  background build makes no tool calls but is reachable, and must keep the
  role.
- **Rule:** the claim still carries the one-hour TTL. In addition, settling
  expires a coordinator hold whose holder has been unreachable for
  `COORDINATOR_UNREACHABLE_GRACE` (starting value 10 minutes, not a
  measurement). It is logged as `lease_expire` with reason `unreachable`.
- **Claim check:** a coordinator claim from an unreachable session is
  refused with `coordinator_unreachable`. The message gives the cause, says
  how to arm a listener for the host (Stop hook, Codex doorbell,
  `wait-mail`), and names `lease run` for landing.
- **Near-miss tests:** a Claude Code session mid-turn, before its Stop hook
  has armed the listener, claims successfully. A session idle with its Stop
  hook listening keeps the role past the grace. A session whose shim
  process died loses the role after the grace.

## 2. Ordering the queue (S1, S2)

- **`memory_agents(action="order", lease=..., first=<agent id>,
  reason=...)`** moves one waiter to the head of the queue. It is open only
  to the live holder of `coordinator:<project>` or `conductor:<project>`,
  for leases in its scope (Q2). Each use is logged as `lease_order` with
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
  moved past at most `ORDER_MAX_SKIPS` times (starting value 3), so a
  waiter is never starved. Arrival order stays the only rule while no role
  is held.
- **Schema:** one waiter column (`priority`, plus `unblocks` text). Under
  D10 this rides the next bump. If the exception is granted while v59
  (#1154) is still open, it rides v59 with that session's agreement;
  otherwise the next bump.

## 3. Guards: nothing waits on a stopped holder

Every guard decides on one exact signal and is tested against the
legitimate case that looks like the failure (the near-miss). The thresholds
are starting values, not measurements. `board-audit stats` (#441) is where
their effect is measured once they ship.

| Failure | Today | Guard | Signal | Near-miss that must still work |
|---|---|---|---|---|
| Waiter's turn comes while it is between turns | Grant is silent; the waiter is deleted and must claim again within 300 s or lose its place | **F1** a grant notice rings the waiter; **F2** a missed grant puts it back in the queue once, second in line | Grant event; grant window lapsed without a renewal | A waiter that renews within the window keeps the lease as now |
| Waiter died while queued | Removed only after 7 days idle (`AGENT_RETENTION`); its turn wastes 300 s | **F3** settling skips it and dequeues it with reason `departed` | Session waiter: attachment lapsed (`lease_until` past by 60 s) and no listener. `lease run` waiter: no poll for 60 s (it polls every 5 s) | A live session idle between turns: its shim keeps renewing `lease_until`, so it is kept |
| Session holder dormant or dead (claimed lease) | Held to the TTL: 1 h, or 1 d for `claim:` | **F4** for the coordinator; **F5** escalation for others; **S3** conductor frees it | Holder unreachable (§1.1) while someone is queued | A reachable holder waiting on a long build is never freed automatically |
| Process holder hung (`lease run` alive but stuck) | Renewed every ttl/3 for as long as it runs; `expect` only marks it stale | **F5** escalation from the expected end; **S4** `--stall-after` notices and `--max` stops | Past the expected end; no output for `--stall-after` | A long build that keeps printing, with an honest `--expect` |
| Process holder crashed | OS lock frees at once; board lease lapses within the ttl (120 s default) | Unchanged | Lock released | |
| Holder left something running after release | Unguarded: a child that outlives the command runs without the lock (`lease_cli` docstring) | **S4** `--wait-children` (opt-in) holds the lease until the process tree exits; by default the release notice names the processes left running | Job object on Windows, process group on POSIX | `lease hold --while-pid` for a detached server, the supported pattern: never killed, never warned about |
| Lease free but resource still busy | Next holder collides | **S5** a per-lease busy probe runs before a grant; when busy, the grant waits and the last holder is told | Probe exit code | A probe that fails to run grants as now, with one warning line |
| Holder keeps renewing past its estimate | Stays stale silently | **F5** renewing a stale lease while others wait logs `lease_overrun` and notifies the role holder | Renewal after the expected end with a non-empty queue | Renewing with a new `expect` is a logged change, not an overrun |
| Sessions polling and messaging the holder | Only way to learn of a grant | **F1** removes the reason; **F7** the queued answer says "you will be told; don't poll or message the holder" | | |

### 3.1 Escalation ladder (F5, F6, S3)

For a held lease with someone queued:

1. **Past the expected end:** the board marks it stale (already the case).
2. **Overdue** (twice the expected duration, or 15 minutes past the
   expected end if that comes later; a lease with no expected end starts at
   30 minutes): a notice to the holder, and to its owning session for a
   `lease run` (S4's owner link). It rings the holder if it is parked.
3. **Unanswered for 10 minutes, or the holder is unreachable:** a notice to
   the live role holder (conductor, otherwise coordinator) naming the lease,
   its holder and the evidence. The Console Board shows a stuck-queue
   banner.
4. **Then:**
   - a **session-held lease** whose holder is unreachable is freed by the
     daemon after `STUCK_FREE_GRACE` (starting value 15 minutes past step 3),
     logged with reason `unreachable`. The conductor can free it sooner
     (S3);
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

## 4. Workflow profiles and onboarding (S6)

**Where:** the Console sign-in work (#1144) adds `pseudolife-mcp console`,
a one-time sign-in link, and makes install and update open the Console
already signed in (its steps 3 and 5). This guide uses that one browser
opening and adds none. Agreed with that session on 2026-11-02:

- the link lands on the guide with `#/signin?code=…&to=welcome`, set by
  `pseudolife-mcp console --to welcome`;
- install and update pass `--to welcome` until the guide is finished;
- whether the owner has finished is stored per bank in the `meta` row
  `console_first_run` = `{completed_at, version}`, written through the
  operator-gated route #1144 defines, so a rerun opens nothing and says so
  in one line.

**The guide (`#/welcome`)** has four short screens:

1. **Sessions and the board:** what a session, the board, mail and a lease
   are, in one diagram.
2. **The roles:** the conductor (given by you, acts for you) and the
   coordinator (taken by the agents, orders the work), and that neither
   ever blocks the queue.
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

## Phasing

1. **This spec** (docs only). Its review carries the D10 decision.
2. **Phase 1, fixes (F1 to F7):** one PR per guard, each with a watched-RED
   test and its near-miss test, and Rust twins or divergence entries.
   Suggested order: F3 and F2 (smallest, no new message origin), F1 (after
   Q1), F4, F5 and F6, then F7.
3. **Phase 2, surfaces (S1 to S5):** only after the exception is granted, or
   Rust-first after the switch (G2).
4. **Phase 3, onboarding (S6):** after #1144 steps 3 and 5.

## Open questions for review

- **Q1. Who sends the grant notice?** The daemon sends no board mail today:
  every message has an agent or the maintainer as its origin. The options
  are a `daemon` origin on ordinary mail (it carries no authority and is
  shown as a system notice), or a wake-only ring plus a `granted` field in
  the heartbeat answer the shim already reads. The first is simpler for
  every harness. The second adds no new message kind.
- **Q2. Scope of a project role over machine-wide leases.** A lease such
  as `full-suite@<box>` is shared by every project on that machine. The
  options are: (a) a project role reorders only waiters from its own
  project, among themselves; (b) only the conductor reorders machine-wide
  leases; (c) machine-wide leases stay in arrival order. (a) is the safest
  default.
- **Q3. The thresholds.** The 10-minute unreachable grace, the 15- and
  30-minute overdue points, the 15-minute free grace and the 3-skip limit
  are conventions. Should any start elsewhere?
- **Q4. Do flags on the existing `lease run` mode count as new surfaces
  under D10?** This spec treats them as surfaces (S4).

## Not in scope

- Preempting a running process to give its lease to a more urgent waiter.
- More than one coordinator or conductor per project.
- Cross-bank coordination.
