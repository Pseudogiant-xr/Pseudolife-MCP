# Session-start briefing — auto-inject "unsure-about" + lessons — design

**Date:** 2026-06-26 · **Status:** approved (design), pending plan
**Background:** full-review item **P1.7**; the graph-insight deferred experiment
("inject a *## What your memory is unsure about* block, mirroring the
world-knowledge block", `docs/specs/2026-06-24-graph-insight-design.md:240`) and
the lessons deferred follow-on ("auto-injected *lessons from past work* prompt
block", `docs/specs/2026-06-20-procedural-outcome-memory-design.md`).

## Goal

At session start the agent automatically sees (1) what its memory is *unsure /
curious about* and (2) *lessons from past work* — without manual tool calls or
relying on the agent to remember. The raw material already exists (the dream
computes the graph digest's surprising connections + suggested questions; the
lessons store holds procedural lessons); this assembles it into one injected
block.

## Why a CLI + hook (not server-side injection)

There is **no server-side context injection** in the MCP path —
`context_builder.py` is desktop-app Pseudolife legacy (it builds a "You are
Pseudolife" *system prompt*), unused by the MCP server. The MCP server exposes
tools; the *client* decides what to inject. In Claude Code the injection point is
a **SessionStart hook** (runs a command, injects its stdout). So the briefing is
delivered as: a server-side assembler, a thin CLI that prints it, and a hook that
runs the CLI.

## Components

### 1. `MemoryService.session_briefing(max_unsure=3, max_lessons=3) -> dict`

The assembler — read-only, no LLM, fast. Pulls from data the dream already
computed:

- **Unsure-about** — from `graph_digest()` (`service.py:2530`): the digest's
  `surprises` (surprising cross-community connections) and `questions` (suggested
  questions). Take up to `max_unsure` of each.
- **Lessons** — from the existing lessons-list service method (the one backing
  `memory_lessons`). **Prioritize `avoid` / `failure` / `correction` lessons**
  (polarity `-` / outcome `failure`|`correction`) — the "don't repeat this"
  signal is the highest-value to auto-surface — then fill to `max_lessons` with
  the most recent remaining.

Returns:
```python
{
  "available": bool,                 # False when the bank is cold (no digest, no lessons)
  "markdown": str,                   # the formatted block (empty string when not available)
  "unsure": {"surprises": [...], "questions": [...]},
  "lessons": [ {task, aspect, lesson, polarity, outcome, ...}, ... ],
}
```

The `markdown` field is the canonical injected block:
```markdown
## What your memory is unsure about
- Surprising link: `web-portal` ↔ `edge-cluster` (bridges 2 communities)
- Open question: what does `checkout-svc` ultimately run on?

## Lessons from past work
- ⚠ avoid: running `docker compose down -v` (wiped the bank once)
- ✓ prefer: HF_HUB_OFFLINE=1 for deterministic test runs
```

### 2. `memory_briefing()` MCP tool (full-tier)

A thin wrapper returning the `session_briefing()` dict, so the agent can also pull
the briefing on demand mid-session. Not core-tier.

### 3. `pseudolife-mcp briefing` CLI mode

A new `cli.py` branch — **torch-free**, mirroring the shim's client-only import
discipline (session-start must stay ~instant). It:

- Connects to an **already-running** daemon (reuse the shim's connect path).
  **It must NOT auto-start the daemon** — session-start runs this every time, and
  spawning the daemon there would block startup. If no daemon is reachable, print
  nothing and `exit 0`.
- Calls `memory_briefing`, prints the `markdown` field to stdout.
- Prints **nothing** (and `exit 0`) when `available` is False or the block is
  empty — so the hook never injects noise or fails session start.
- `--max-unsure N` / `--max-lessons N` flags (default 3 / 3) to tune the budget;
  it costs context every session, so the default is lean.

### 4. SessionStart hook (documented, user's global config — applied with a nod)

A `settings.json` snippet that runs `pseudolife-mcp briefing` on `SessionStart`
and injects its stdout, replacing/augmenting the existing static reminder hook.
This lives in the user's Claude config, **not** the repo — the repo ships the CLI
+ the documented snippet (README). Applied to the user's machine only with
explicit confirmation.

## Error handling / graceful degradation

- Daemon down / unreachable → CLI prints nothing, `exit 0` (never block startup).
- Cold bank (no digest yet, no lessons) → `available: False`, empty markdown,
  CLI prints nothing.
- Any assembler exception is caught at the CLI boundary → print nothing, `exit 0`
  (a memory briefing must never break a session).

## Testing

- `session_briefing()` unit tests: cold bank → `available False` + empty markdown;
  populated digest+lessons → correct selection (caps respected) and well-formed
  markdown; **lesson prioritization** — `avoid`/`failure`/`correction` lessons
  appear before plain-recent ones.
- `memory_briefing` tool: registration + dispatch returns the dict shape.
- CLI: a small smoke test that `briefing` against **no daemon** prints nothing and
  exits 0 (the must-not-break-startup guarantee). The assembly itself is covered
  at the service level.
- Full suite green.

## Out of scope (YAGNI)

- World-knowledge facts in the briefing (a separate follow-on — the original
  "world block" idea).
- Any LLM call (pure assembly from already-computed data).
- Auto-starting the daemon from the briefing path.
- A Cortex Console "briefing" panel (the data is already in the Insight tab).

## Success criteria (verifiable)

1. `session_briefing()` returns the documented shape; cold bank → `available
   False`; lessons prioritize avoid/failure/correction; caps respected.
2. `memory_briefing` tool registered (full-tier) and dispatches.
3. `pseudolife-mcp briefing` prints the markdown when a daemon + content exist,
   and prints nothing + exits 0 when the daemon is down or the bank is cold.
4. README documents the SessionStart hook snippet. Full suite green.
