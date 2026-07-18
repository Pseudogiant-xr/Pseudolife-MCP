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
   omitted, so a cold bank injects nothing.
2. **Episode lifecycle is owned by the daemon, keyed by a resolved session
   identity — hooks make that identity precise, but nothing about opening
   or closing an episode requires them.** Five tiers, strict precedence
   (full table + rationale:
   [Configuration — session identity](configuration.md#session-identity)):
   a stdio shim's per-process `X-PL-Session` header outranks an explicit
   `episode` handle passed on a write, which outranks the SessionStart-hook-
   registered active session, which outranks the legacy transport
   `mcp-session-id` (per-**connection**, not per-session, and removed
   entirely by the MCP 2026-07-28 revision — SEP-2567, "Sessionless" — so
   treat it as a dying fallback, not something to depend on), which falls
   back to writer id + idle-gap sessionization when nothing above resolved.
   - **Hook-registered identity.** The plugin's SessionStart hook forwards
     Claude Code's own `session_id` to the daemon, which opens (or
     resumes) that session's root episode immediately — no longer lazily
     on first store — and sets it as the machine-scoped active-session
     pointer. The returned briefing text carries a one-line **handle
     advertisement**: the episode id (truncated) plus the instruction to
     pass `episode="<id>"` on writes when running concurrent sessions. A
     SessionEnd hook closes that session's episode and clears the pointer
     when the session ends.
   - **Ownership guard.** `memory_episode_end`, the direct
     `POST /api/episode/end` with no `session_key` in the body, and the
     idle reaper can only close a root episode whose `session_key` matches
     the caller's own resolved identity — a session can no longer pop
     another, still-open session's root by accident. No match is a no-op:
     `{"closed": null, "reason": "no owned open session"}`.
   - **Direct-HTTP / sessionless clients** (no shim, no hook, no explicit
     handle) still get episodes: the daemon **lazily opens** one on the
     first store of a new session (so empty sessions never leave a husk)
     and the **idle reaper** closes it once inactive — firing the
     end-of-session dream, or pruning it if empty
     (`PSEUDOLIFE_SESSION_IDLE_SECONDS`, default 30 min). One open episode
     is tracked *per resolved identity*, so concurrent sessions (e.g.
     different projects) don't clobber each other, subject to tier 3's
     last-start-wins limitation (see Configuration).

   A store arriving after the reaper closed the episode **resumes** it —
   same identity, same episode — rather than opening a new husk
   (`PSEUDOLIFE_SESSION_RESUME_SECONDS`, default 6 h; `0` disables).
   Direct-HTTP titles start generic
   (`session - YYYY-MM-DD HH:MM`, since the daemon has no project `cwd`) —
   name the session with `memory_session_title` (store responses carry an
   `episode_hint` until you do); a session closing still-generic gets an
   auto-derived `"{dominant source} - {stamp}: {first-entry snippet}"`
   title. Fragmented history is repairable over REST:
   `POST /api/episodes/rename` and `POST /api/episodes/merge`. Set `TZ` in
   `ops/.env` for local time.

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

One command installs the briefing hook:

```powershell
.\ops\install-hook.ps1     # Windows (PowerShell 7)
```
```bash
./ops/install-hook.sh      # Linux / macOS
```

It backs up your `settings.json`, then adds the hook **alongside** any
existing ones (idempotent — safe to re-run; it installs only what's
missing). Requires `pseudolife-mcp` on PATH — `pip install -e .` in the
repo puts it there.

Prefer to wire it by hand? The briefing's `--hook-json` flag emits the
`hookSpecificOutput.additionalContext` payload Claude Code injects:

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
start. Tune the briefing budget with `--max-unsure N` / `--max-lessons N` /
`--max-world N` (default 3 each). The briefing content is also available on
demand via the CLI or the Console's `/api/briefing` route.

## Episodes + tags

An *episode* is a bracketed working session. While an episode is open,
every memory stored carries the episode's id + title automatically, so
later queries can scope by session. **Session episodes open and close for
you**, daemon-owned and keyed by a resolved session identity (five tiers —
shim header, `episode` handle, hook registration, legacy transport id, or
idle-gap sessionization; see
[Configuration — session identity](configuration.md#session-identity)) so
concurrent sessions don't collide; absent a hook or shim, the daemon lazily
opens one on first store and an idle reaper closes it. For a substantial
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
