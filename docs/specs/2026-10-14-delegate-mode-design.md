# Delegate mode: a lean, event-driven orchestrating session

Status: proposed, 2026-10-14, revision 2. Replaces the contributor-only
`ops/delegate_watch.py` of PR #755 before that merges. Revision 1 put the
event wake inside the plugin's Stop hook behind a new daemon endpoint; this
revision sends events as board mail instead, which needs no hook change
and reaches Codex sessions too.

## Problem

An orchestrating session, usually the maintainer's delegate, holds the
plans, dispatches work, orders suites and merges. It is the most
expensive kind of session to run, and its cost is mostly waste.

Measured on 2026-10-14 from Claude Code transcripts, six such sessions,
Sep 23 to Oct 13:

- Context averaged 230k to 500k tokens per request.
- 96% of cost-weighted input was cache reads; 4% was cold turns. Cost
  scales with the number of requests times the context size.
- 2,504 turns began with a background-task notification, 557 with board
  mail, 558 with the maintainer.
- About a third of all turns took no action: no merge, suite launch,
  edit, dispatch or message.
- What filled the context, by characters: Bash output 39%, board results
  25%, task notifications 13%, the maintainer's own words 4%.

The session's value is its long context, so restarting it often is the
wrong fix. What should change is how often it wakes, and how much
throwaway output it reads into that context. Instructions in a 300k
context are a weak lever on their own, so the output rule is repeated
every turn, and an enforcing hook is held in reserve for a session whose
model does not keep to it (revision 3, 2026-10-14; the maintainer runs
the delegate on Opus 5.5 or Fable 5.1).

## Goal and success measure

Keep one orchestrating conversation with its full context. Make it wake
only for decisions, and keep raw output out of it.

Success is measured with the same transcript scripts over the delegate
session for one week after release, against the baseline above:

- the share of turns that take no action, from 34%;
- the Bash and board share of the context, from 64%;
- requests per active hour;
- Bash results over 8k characters per session, the count the per-turn
  reminder is meant to drive down.

## Three pieces

1. **A watcher** that waits for decision events without a model and
   delivers them as board mail. Any session can start one; the mail wakes
   the session through the daemon's existing ring.
2. **A brief and a per-turn reminder**, served only to the maintainer's
   delegate, the session holding an operator-granted `delegate:<project>`
   lease: the brief at session start, one line on every turn.
3. **An escalation ladder** for the output rule, built only when the
   measured week says the reminder is not enough: a PostToolUse nudge
   after a large result, then the refusing guard of the appendix.

Each lands as its own Python PR, then a Rust-only PR ports what has a
Rust counterpart. `rust/PORTING.md` sets that order: a Python change
passes PR parity against the unchanged port; a PR changing both fails
parity until its base holds the Python change.

## 1. The watcher: `pseudolife-mcp delegate-watch`

### What it does

A detached process, one per session, that polls and sends one board
message per batch of events:

- a pull request opened, merged or closed; its checks all green (with
  its merge state) or one failing on that head; newly conflicting; its
  checks all finished with some cancelled, one line however many jobs
  were cancelled (a cancelled check is never a failure; a re-run that
  ends cancelled again is reported again, as a failing re-run is);
  green and cancelled also wait until no workflow run on the head is
  queued or running, since a queued run has no checks yet, and a run
  that failed or was cancelled before creating a check counts as such;
- a master workflow (push or schedule) failing, or passing after a
  failure; cancelled runs are neither;
- a full-suite lease going from held to free;
- a new result in a suite results directory, when one exists.

The events, their once-only reporting, the settle window and the eight
review fixes of #755 carry over unchanged: re-runs, board outages, `gh`
timeouts, lazy `UNKNOWN` mergeability, a pull request falling off the
list, a failed closure lookup, a fresh install's ledger, UTF-8 output.

### Transport: board mail

The watcher registers its own board agent (`register` is instanceless; a
bearer is enough) and sends to the session that started it. The daemon's
wake decision does the rest:

- The owner parks `waiting_peer` with `park_needs="decision events"`
  and `park_clear_by=<watcher agent id>`. The watcher prints its id on
  start, and the brief tells the owner to use it.
- Each event message carries `clears="decision events"`. Either match
  rings: the clearer id, or the word-matched need.
- A session mid-turn gets the mail as context for its next turn
  (`hinted`), never an interruption.
- A session parked `done` is not rung. The daemon enforces that; the
  watcher also skips its quiet messages for a `done` owner, so nothing
  piles up unread.

Every event is then in the mailbox history, so a night can be audited
there, and peers can read the same events. A Codex owner is rung through
its doorbell, with no extra work.

Cost of this choice: a mail wake takes one `memory_message(receive,
ack=true)` call before the owner has the full text, where a hook
injection has none. The wake text shows a five-message preview with
100-character excerpts, so each message leads with its decision.

### Keep-warm

After 55 quiet minutes the watcher sends one line (`quiet 55m: 3 open
PRs, 1 with checks running`). A wake costs one cached read; a cold
rewrite of the same context costs about twenty, so a wake pays for
itself over any quiet stretch shorter than roughly 20 hours. Overnight
while CI runs, that is right; over a weekend away it is not. The park
decides: a `done` owner gets no quiet mail. The maintainer's separate
keep-warm timer can go.

### Lifecycle

- `pseudolife-mcp delegate-watch --detach` starts it from a session and
  prints the watcher's agent id. It detaches the way the shim already
  does (`shim.py` `_spawn_detached`: `CREATE_NO_WINDOW |
  CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session` elsewhere,
  output to a log), so Claude Code's 2-hour cap on background commands
  does not apply.
- The owner is the session's board address, read from the `.agent` file
  beside its coordination digest (`resolve_digest_path`, then
  `.agent`), or `--owner <id>`.
- One per session: a `<key>.dwatch` file beside the digest holds the
  pid; a second start says "already running" and exits 0.
- It stays listed by updating its board status each poll (an ephemeral
  agent is pruned after an hour idle), and re-registers once on
  `instance_not_found` or `invalid_credential`, as `lease` does.
- It exits when the owner's board row is gone, when `--until-lease
  delegate:<project>` names a lease the owner no longer holds, after 14
  days, or on `pseudolife-mcp delegate-watch --stop`.
- `gh` missing or failing on five polls in a row: one message to the
  owner saying so, then exit 2. It never goes silent.

### Python shape, written for the port

- `pseudolife_memory/delegate_watch_cli.py`, dispatched like `wait-mail`
  in `cli.py`; stdlib plus the board client `lease_cli` already has.
  That client gains `update`, and `send` gains `clears` and `urgent`.
- One loop, no threads. Event classification is pure functions of JSON
  in, event lines out (`checks_state`, `pr_events`, `master_events`),
  with exact output strings.
- Those functions' fixtures live in
  `tests/fixtures/delegate_watch_events.json`: snapshot pairs and their
  expected lines. The Python tests read it; the Rust contract tests
  read the same file.
- `cli_help.txt` is byte-pinned to Python's `_USAGE`, so the help line
  and the Rust file change together.

### Rust port, a Rust-only PR after the Python one

- Subcommand: the four edits `lease` shows (module in `cli.rs`, branch
  in `main.rs` with a `tokio` current-thread wrapper, removal from
  `DEFERRED_MODES`, `cli_help.txt`).
- Board client: `cli/lease/board.rs` has `register_as`, `send`,
  `agents` and `leases`; add `update`, and `clears`/`urgent` on `send`.
- Detach: `lifecycle.rs` `spawn_detached` has the same flags; make it
  `pub(crate)`. The crate forbids unsafe code and that module is the
  documented exception.
- Digest path: `cli/wait_mail.rs` already ports `resolve_digest_path`
  with the `.host` record. A `.agent` reader is new (32 lowercase hex,
  regular file only, as the hooks check it).
- Parity: a `CLI-DELEGATE-WATCH` row in `rust/PARITY.md` and the mode
  count in `CLI-MODE-OWNERSHIP`; a `rust/cli_harness/rows/` row with
  mutant controls and both-OS goldens; a shim contract test; the
  fixtures file above. `gh` is faked in the harness, as the Python tests
  fake it.

## 2. The brief and the per-turn reminder

### The brief, served only to the delegate at session start

> You are the maintainer's delegate for <project>. Keep this session's
> context for decisions:
> - Start a watcher once: `pseudolife-mcp delegate-watch --detach`. Park
>   with `park_needs="decision events"` and `park_clear_by=<its id>`
>   before ending a turn that waits. On a wake, read with
>   `memory_message(action="receive", ack=true)`, act, and park again.
> - Hand raw output (CI logs, test runs, long diffs) to a subagent that
>   returns at most ten lines. Put what you already know in its brief
>   (paths, symbols, what is settled) so it reads little. Run it in the
>   foreground while you are working; a background one wakes you when it
>   finishes.
> - Ask workers for decision-ready mail and to keep progress in their
>   board status.
> - Answer a quiet wake in one line, with no tool calls.

### The reminder, one line on every turn

> Delegate: raw output goes to a subagent; a quiet wake gets one line.

It rides the plugin's existing `UserPromptSubmit` hook, which already
appends the "Memory changed since your last turn" note, so there is no
new hook and nothing for Codex users to approve again. About 60 tokens at
the tail of the conversation, where the cached prefix is untouched and a
long context still reads reliably. This is the same mechanism Claude
Code uses for its own per-turn notices. The watcher's wake mail ends
with the same line, at no cost, though as agent-origin text it carries
no authority.

### Mechanics

- `coordination-start.sh` and the prompt hook pass `?agent=<id>` when the
  `.agent` file exists, the shape the park gate already uses. The daemon
  appends the brief or the reminder when that agent belongs to the
  bearer's principal and holds an operator-granted `delegate:<project>`
  lease: the `lease_delegate` audit record for the lease's current fence,
  the same proof `_authority` applies to delegate mail. A held lease alone
  is not proof; the prefix predates its reservation. The daemon caches
  the answer per agent for a few minutes so the per-turn call stays one
  cheap query.
- The check-in text itself does not change. It is pinned to a measured
  eval; the brief and the reminder are separate blocks after it.
- A grant that lands mid-session shows on the next turn's reminder and at
  the next session start.
- Rust: the Rust daemon serves the hook endpoints too, so it gets the
  same `agent` parameter and check, with a board parity scenario for
  delegate, non-delegate, expired, another principal's agent, and a lease
  held without the operator record.

## 3. The escalation ladder for the output rule

Built in this order, each step only when the measured week shows the
previous one did not move the Bash share:

1. **The per-turn reminder** above. Expected to be enough for Opus 5.5
   and Fable 5.1.
2. **A PostToolUse nudge.** After a Bash or PowerShell result over 8k
   characters, the hook appends one line: "that output was 24k
   characters; next time hand it to a subagent." Feedback at the moment
   of the mistake, no refusal, no false positives. It needs a
   `PostToolUse` matcher on `Bash|PowerShell` in `hooks.json`, so it costs
   the one Codex re-approval; the pin in
   `tests/test_codex_hook_launcher.py` moves and the changelog says so.
3. **The refusing guard** of the appendix, for a session whose model does
   not keep to a reminder, which today means a Codex delegate.

## Appendix: the refusing guard, `plugin/hooks/output-guard.sh`

Specified so it can be built without a new design round; not built in
this change.

### Rule

A parent session's command that would read raw output into its context
is refused, with a reason naming the one accepted alternative. A
subagent's is allowed: it is the intended reader. The guard is for every
session with the plugin, not only the delegate; scoping to the lease
would need identity plumbing the hook does not have, and the rule is
good for every orchestrator.

### Patterns, deliberately four

| Refused | Reason names |
|---|---|
| `gh run view` with `--log` or `--log-failed`, no file redirect | a subagent, or redirect to a file and read its tail |
| `gh pr diff`, or `git diff` / `git log -p` with a `..` or `...` range, no `--stat`/`--name-only`/`--name-status`, no redirect | `--stat`, or a subagent |
| `pytest` / `cargo test` with no file redirect | the repo's redirect form, which also keeps the exit code |
| `cat`, `type` or `Get-Content` of a `.log`/`.out` path with no `head`/`tail`/`-Tail`/`-TotalCount`/`Select-Object -First|-Last` | tail it, or a subagent |

A bare `git diff` is allowed: workers check their own small edits with
it constantly, and refusing it would teach the model to route around the
guard. Every added pattern is a false-positive risk; the list grows only
from a measured catch.

### Mechanics

- `hooks.json` gets a second PreToolUse group, matcher `^(Bash|
  PowerShell)$`. Codex users approve the hooks once more, the pin in
  `tests/test_codex_hook_launcher.py` moves, and the changelog says so.
  Tests that unpack exactly one PreToolUse group (`_entry()` in
  `tests/test_subagent_board_guard.py` and its siblings) are updated.
- Same conventions as the board guard: `bash -n` first; top-level JSON
  keys only, matched with the `[{,]\s*"key"` shape so quoted text inside
  a command cannot spoof `agent_id` or `tool_name`; fail open on
  anything unreadable, on the coordination opt-out, and on a script that
  does not parse. A broken guard never blocks a parent.
- Subagent exemption: Claude Code puts `agent_id` in a subagent's hook
  payload and never in the parent's (measured on 2.1.283, 2026-09-30).
- Codex: the same `hooks.json` runs there. Codex's PreToolUse payload
  shape is not yet verified in this repo (the SubagentStop shape is), so
  the first step is a probe, recorded like the 2026-09-30 one. Until a
  subagent marker is confirmed, Codex gets the refusal for every session,
  with the redirect-and-tail alternative, which a Codex worker can follow
  too.
- Windows under Codex runs `commandWindows`, so `lifecycle.ps1` gets an
  `OutputGuard` event that implements the same four patterns. (The board
  guard's PowerShell no-op is specific to that guard: Codex children
  have their own addresses.)
- One ledger line per refusal, `<key>.guard` beside the session's
  digest, for the measurement above.
- Rust: nothing to port. Hooks are bash and PowerShell.

### Tests

The existing guard's harness, which runs the script with a JSON payload:
each pattern refused for a parent and allowed for a subagent; a command
with the alternative applied allowed; the pattern text quoted inside a
message or heredoc not matched; the opt-out; a broken copy blocking
nothing; the PowerShell path through `pwsh_run`.

## Not in this design

- Changing the check-in text to recommend `ack=true` for every session.
  That text is eval-pinned; it needs its own bench run.
- Rewriting commands into a capped form instead of refusing them. It
  would fail quietly on heredocs, background jobs and chained `cd`s.
- A watcher inside the Stop hook (revision 1). It needed a Python start
  every minute inside a bash-only hook, a new endpoint, and excluded
  Codex.

## Order of work

1. Python PR: the watcher, replacing #755. No `hooks.json` change.
2. Python PR: the brief and the reminder, daemon and hook scripts. No
   `hooks.json` change.
3. Rust-only PRs: the watcher port; the brief's daemon parity.
4. One measured week on the delegate, then the numbers above decide
   whether to climb the ladder.

Each PR carries tests seen red first, the dependent-file run, and the
two reviews. The brief PR changes the plugin's scripts, so its deploy
needs the client step (`update --all`); sessions pick them up on
restart.
