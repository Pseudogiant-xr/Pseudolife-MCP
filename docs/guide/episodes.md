# Episodes & session lifecycle

How session episodes open and close (daemon-owned, no hooks required), the
SessionStart briefing hook, nested sub-episodes, and tags. Part of the
[user guide](../../README.md#documentation).

## Session lifecycle — daemon-owned episodes

Two things wire to Claude Code's session lifecycle so the memory loop runs
reliably — without the agent having to remember:

1. **SessionStart briefing.** `pseudolife-mcp briefing` prints a compact
   block: **what your memory is unsure about** (surprising graph links +
   open questions), **lessons from past work** (avoid / prefer),
   **verified world facts** (fresh, cited, age-ranked), and **where we left
   off** (a one-line recap of your last closed session). Empty sections are
   omitted, so a cold bank prints nothing. As a hook (`--hook-json`, or the
   plugin) it injects a short memory core first and then this block without
   the unsure section (the Console's Insight view keeps it), fitted to the
   hook's size budget, so even a cold bank's session starts with the core.
   A resumed or compacted session is not served the block again: it gets
   the episode-handle line and a pointer to the full briefing (after a
   compaction, also a daemon-side `hook-instructions.md`).
2. **Episode lifecycle is owned by the daemon, keyed by a resolved session
   identity — hooks make that identity precise, but nothing about opening
   or closing an episode requires them.** Five tiers, strict precedence
   (full table + rationale:
   [Configuration — session identity](configuration.md#session-identity)):
   a stdio shim's `X-PL-Session` header outranks an explicit
   `episode` handle passed on a write (on the lifecycle tools —
   `memory_episode_start`/`_end`, `memory_session_title` — a resolved
   handle wins outright), which outranks the SessionStart-hook-registered
   active session. The legacy transport `mcp-session-id` tier is
   **retired** (per-**connection**, not per-session, and removed entirely
   by the MCP 2026-07-28 revision — SEP-2567, "Sessionless";
   `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1` is a one-release rollback
   hatch), leaving writer id + idle-gap sessionization as the floor when
   nothing above resolved.
   - **Hook-registered identity.** The plugin's SessionStart hook forwards
     Claude Code's own `session_id` to the daemon, which opens (or
     resumes) that session's root episode immediately — no longer lazily
     on first store — and sets it as the machine-scoped active-session
     pointer. The returned briefing text carries a one-line **handle
     advertisement**: the episode id (truncated) plus the instruction to
     pass `episode="<id>"` on every memory write — the concurrency-correct
     channel, so attribution stays right even when other sessions are open. A
     SessionEnd hook closes that session's episode and clears the pointer
     when the session ends. If a client crashes without firing SessionEnd,
     the pointer expires after `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS`
     (default 6 h, the resume window; `0` disables) so a dead session stops
     attracting later tier-3 writes — SessionStart re-stamps it, so an active
     session (Claude Code re-fires the hook on resume/compact) stays live.
   - **Ownership guard.** `memory_episode_end` pops only the caller's own
     sub-episodes and never closes a session root, with or without a
     handle. The direct `POST /api/episode/end` with no `session_key` in
     the body can only close a root episode whose `session_key` matches the
     caller's own resolved identity — a session can no longer pop another,
     still-open session's root by accident. No match is a no-op:
     `{"closed": null, "reason": "no owned open session"}`. The idle
     reaper is separate: it closes any root idle past the threshold,
     using each root's own key — that's its job, not a guard bypass.
   - **The stdio shim** (the installer default) opens no episode of its
     own. Under Claude Code its `X-PL-Session` header is the session id
     Claude Code launched it with, which is the id the plugin's
     SessionStart hook registers. A write without an `episode` handle, or a
     `memory_session_title`, therefore lands on the hook's root, and that
     root's lifecycle stays with the hooks and the idle reaper: the shim's
     exit does not close it, because a reconnect restarts the shim
     mid-session. Without the plugin's hooks the daemon opens that root on
     the first write, and the idle reaper closes it. Other hosts get one
     session per shim process (Codex keys each call by its own thread
     instead). The daemon opens that session's episode on the first write
     that needs one, as for a direct-HTTP client, and the shim closes it at
     exit when the host lets it exit; otherwise the idle reaper does. A
     shim that is idle or only searches leaves no episode behind. Until
     2026-09-25 the shim opened a working-directory-titled episode at
     connect, which gave each Claude Code session a second root and left an
     empty root for every shim a host killed. One gap remains: `/clear` and
     an in-session `/resume` give the session a new id but keep the shim's.
     Afterwards a `memory_store` or `memory_episode_start` without a handle
     reopens the root under the old id (or opens one) and lands there, and
     a `memory_session_title` without one renames that root. A call that
     passes the handle SessionStart advertised lands on the new root and
     opens nothing under the old id. `--continue`, or `--resume` without an
     id, can likewise launch the shim with an id no hook registers.
   - **Direct-HTTP / sessionless clients** (no shim, no hook, no explicit
     handle) still get episodes: the daemon **lazily opens** one on the
     first store of a new session (so empty sessions never leave a husk)
     and the **idle reaper** closes it once inactive — firing the
     end-of-session dream for non-empty sessions
     (`PSEUDOLIFE_SESSION_IDLE_SECONDS`, default 30 min). An episode that
     is *empty* at reap time is closed but kept until it is also past the
     resume window (the session may only be on a break), then deleted with
     a **tombstone** left behind. One open episode
     is tracked *per resolved identity*, so concurrent sessions (e.g.
     different projects) don't clobber each other, subject to tier 3's
     last-start-wins limitation (see Configuration).

   A store arriving after the reaper closed the episode **resumes** it —
   same identity, same episode — rather than opening a new husk
   (`PSEUDOLIFE_SESSION_RESUME_SECONDS`, default 6 h; `0` disables). The
   briefing's `episode="<id>"` handle resumes under its **own, far longer
   window** (`PSEUDOLIFE_HANDLE_RESUME_SECONDS`, default 30 days; `0`
   disables): a handle is a daemon-minted id only that session's briefing
   carried — an explicit identity claim, not an inference — so a session
   parked for days (a deferred benchmark, a long weekend) still attributes
   correctly on return. A write carrying a handle whose root the reaper
   closed reopens that episode (without moving the current-episode
   pointer — the writer may be a different session); one whose empty root
   the sweep already deleted **recreates it from the tombstone under the
   original id** (keeping an agent-set title), so the always-pass handle
   stays valid across long pauses either way. Only roots the *reaper*
   closed empty are ever swept — an old episode whose entries were later
   evicted or forgotten is history, not a husk, and is never touched. The
   one deliberate gap in the promise is the manual prune
   (`POST /api/episodes/prune`): an explicit operator action that deletes
   empty closed episodes without a tombstone. Past the handle window, or
   on an ambiguous prefix, the write proceeds under normal identity with
   an `episode_warning`.
   Session titles start generic
   (`session - YYYY-MM-DD HH:MM`, since the daemon has no project `cwd`) —
   name the session with `memory_session_title` (store responses carry an
   `episode_hint` until you do, naming the handle when the store passed
   one); a session closing still-generic gets an
   auto-derived `"{dominant source} - {stamp}: {first-entry snippet}"`
   title. Fragmented history is repairable over REST:
   `POST /api/episodes/rename` and `POST /api/episodes/merge`. Set `TZ` in
   `ops/.env` for local time.

### Session record

An episode is not a durable record that a session happened: a root that
ends holding no stored entry is deleted. So every registration (the
SessionStart hook, or `POST /api/episode/start` from the stdio shim and the
CLI episode hooks) also writes one `client_sessions` row per session key
(schema v43), which no prune, sweep or tombstone expiry deletes. The row
holds how the session first registered (`hook` or `api`), the bearer's
principal, its first start and every registration time since (a resumed
client registers again), its most recent close and why (`end` for
SessionEnd or shim exit, `idle` for the reaper; cleared when the session
registers again or a store or handle reopens its root), the startup memory-policy variant the hook assigned
(for `full_separate_hook`, a plugin without the separate memory-policy hook
never delivers it), and every root episode id the session was given. A
session that only searched or logged outcomes loses its root at the end but
keeps this row, so its searches (by session key) and outcomes (by episode
id) still have a session to count against; `evals/capture_metrics.py`
reads it. A root the daemon opened lazily for a key that never registered
gets no row, and an operator's manual prune of an open root
(`include_open`) leaves that session open on record. Postgres only;
operational data, left out of `pseudolife-mcp export` like the retrieval
log.

## Inferred outcomes at session close

Most sessions never call `memory_outcome` — the agent stores facts and
moves on without logging how the work went. When a session episode closes
with stored entries but zero outcome signals, the end-of-session dream
runs an extra stage that infers up to 3 signals from the episode's own
record (`origin="inferred"`) before the usual lesson synthesis — the
context deliberately includes status-source entries too, since a
session's own status chatter is still evidence of how it went, just
weaker evidence than an explicit `memory_outcome` call. Lessons synthesised
from a batch that is *entirely* inferred signals are written at a
discounted confidence (0.4 vs the usual 0.6); a lesson that already
exists at a higher confidence isn't dragged down — the write path keeps
the higher value, as it always has. Kill switch:
`memory.lessons.infer_outcomes: false` (see
[Configuration](configuration.md)); the signal cap per episode
(`infer_outcomes_max_signals`, default 3) is tunable alongside it.

## Installing the briefing hook

One command installs the briefing, per-turn memory guidance, and a separate
startup instruction for coordination check-in:

```powershell
.\ops\install-hook.ps1     # Windows (PowerShell 7)
```
```bash
./ops/install-hook.sh      # Linux / macOS
```

It backs up your `settings.json`, then adds the hooks **alongside** any
existing ones (idempotent — safe to re-run; it installs only what's
missing). Requires `pseudolife-mcp` on PATH — `pip install -e .` in the
repo puts it there.

Prefer to wire it by hand? The briefing's `--hook-json` flag emits the
`hookSpecificOutput.additionalContext` payload Claude Code injects — the
same memory core and bounded briefing the plugin hook serves:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [
        { "type": "command", "command": "pseudolife-mcp briefing --hook-json" }
      ] }
    ]
  }
}
```

The briefing connects to the *already-running* daemon (never starts one)
and does nothing if the daemon is down — it can't slow or break session
start. Tune the printed briefing with `--max-unsure N` / `--max-lessons N` /
`--max-world N` (default 3 each); with `--hook-json` these are ignored,
because the daemon fits the hook context to the hook's size budget. The
briefing content is also available on demand via the CLI or the Console's
`/api/briefing` route.

The plugin's daemon-served memory hook uses a short operating guide rather
than repeating the full standing memory policy. Its bounded briefing retains
complete items, prioritizes lessons and recap, and reports omitted content. Full standing guidance is in
[`examples/CLAUDE.memory.md`](../../examples/CLAUDE.memory.md). A custom
`hook-instructions.md` in the daemon's data directory is also bounded: an
omission notice means the complete custom instructions must be obtained before
relying on the partial copy. That path belongs to the daemon host and may not
be readable from a remote client; `/api/briefing` returns the briefing, not
the custom instruction file.

The plugin additionally supplies independent startup and per-turn coordination
handlers for check-in and local inbox previews. The startup handler asks the agent to set its project, task
and status, discover peers, and receive pending mail. Full messages are read
with `memory_message`, then acknowledged after reading. The existing adapter
owns the mailbox; this handler does not create another identity or grant
permissions. The memory and coordination hooks have independent output budgets
and do not depend on execution order.

The lightweight `install-hook` scripts install the briefing, the coordination
check-in (`pseudolife-mcp briefing --coordination`, which prints it only where
the daemon serves it: a board that is on and usable by that bearer), and the
per-turn memory-change note (`pseudolife-mcp prompt-hook`). Re-running them
replaces the unconditional check-in echo and the static discipline echo
older versions wrote. They do not register an
agent identity or install the plugin's local inbox-preview handler. `pseudolife-mcp briefing --hook-json` reads
`/api/hook/session-start` (the plugin hook's memory core and briefing) but
forwards no session id, and no SessionEnd hook is written, so an install
wired this way has no hook-registered identity (tier 3) and no hook-driven
episode close: the idle reaper closes the episode instead, and the
briefing's `episode="<id>"` handle is the concurrency-correct attribution
channel. For the full lifecycle, use the plugin.

## Episodes + tags

An *episode* is a bracketed working session. While an episode is open,
every memory stored carries the episode's id + title automatically, so
later queries can scope by session. **Session episodes open and close for
you**, daemon-owned and keyed by a resolved session identity (five tiers —
shim header, `episode` handle, hook registration, legacy transport id, or
idle-gap sessionization; see
[Configuration — session identity](configuration.md#session-identity)) so
concurrent sessions don't collide; absent a hook, the daemon lazily opens
one on first store and an idle reaper closes it. For a substantial
multi-step task you open a **nested sub-episode** under the session:

```
memory_episode_start("auth refactor")            # nests under the open session
memory_store("Decided to keep tags orthogonal to source instead of merging them")
memory_episode_end()                             # pops back to the session
memory_search("design choices", episodes=[session_id])  # expands to the subtree
memory_episode_summary(session_id)               # stats + tag distribution + recent entries
```

Episodes **nest** (schema v15): `memory_episode_start` opens a child under
the current open episode — the parent stays open — `memory_episode_end`
pops back to it, and closing the session cascade-closes any still-open
children. A session-scoped `memory_search(episodes=[root_id])` expands to
the whole subtree, so a sub-episode's entries surface under their parent
session too. (Calling `memory_episode_start` with nothing open simply opens
a root.) In Postgres mode episodes live in the `episodes` table
(`session_key` + `parent_id` columns); in file mode they ride
`cms_state.pt` under the `episodes` key.

Tags are a parallel multi-valued axis to `source`: pass
`tags=["decision", "blocker"]` on store, filter with
`memory_search(..., tags=[...])`. Normalised at store time (lowercased,
stripped, deduped). Set intersection non-empty for the filter to pass
(OR within the filter list, AND with the other filters).
