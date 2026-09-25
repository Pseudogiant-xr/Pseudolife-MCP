"""Plain-text session-start context for the Claude Code plugin hook.

The installer's hook without the plugin (``pseudolife-mcp briefing
--hook-json``) reads the same endpoint, so both paths start with the same
core. ``GET /api/hook/session-start`` serves a short standing memory core and,
when the request is authorized, complete budgeted entries from the session
briefing. The detailed reference remains in ``examples/CLAUDE.memory.md``
and ``MEMORY_LOOP_BLOCK``. Users can add instructions by writing
``<data_dir>/hook-instructions.md``; oversized overrides announce omitted
blocks and their daemon-side source. A briefing must never break a session
start: this module never raises and the endpoint always answers 200.

When the hook passes a ``session_id`` (identity tier 3, spec 2026-07-18),
``hook_session_start`` additionally registers a session episode and the
active-session pointer, and prepends a one-line advertisement of the episode
handle instructing the agent to pass ``episode=`` on every write (identity
tier 2 — the concurrency-correct channel; promoted spec 2026-08-10).
``hook_session_end`` mirrors this on the SessionEnd hook: it closes the
session's episode and clears the pointer (only if still owned). Both are
fail-open — registration/close failures are logged and never surface to the
caller.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from pseudolife_memory import __version__ as DAEMON_VERSION
from pseudolife_memory.utils.config import MEMORY_POLICY_VARIANTS

logger = logging.getLogger("pseudolife-mcp.web")

# Claude Code caps SessionStart hook stdout at 10,000 chars (overflow is
# spilled to a file + preview, which defeats the point). Count UTF-8 bytes
# too, so non-ASCII content cannot exceed the hook's practical limit.
HOOK_CONTEXT_MAX_CHARS = 9_500

# A plugin version arrives on the hook's query string. Only a version-shaped
# value may be echoed into the model's context; anything else is dropped.
_VERSION_SHAPE = re.compile(r"[0-9A-Za-z.+-]{1,32}")  # used with fullmatch: `$` would admit a trailing newline
PLUGIN_UPDATE_COMMANDS = ("/plugin marketplace update pseudolife-mcp, then "
                          "/plugin update pseudolife-memory@pseudolife-mcp")
DAEMON_UPDATE_COMMANDS = "git pull, then ops/update.ps1 or ops/update.sh"
ALL_UPDATE_COMMANDS = "ops/update.ps1 -All (Windows) or ops/update.sh --all, from the checkout"
# A hooks digest is 64 lowercase hex characters (pseudolife_memory.plugin_hooks).
_DIGEST_SHAPE = re.compile(r"[0-9a-f]{64}")


_DAEMON_DIGEST_UNSET = object()


def hooks_notice(plugin_version: str | None, plugin_digest: str | None,
                 daemon_version: str | None = None, daemon_digest=_DAEMON_DIGEST_UNSET) -> str:
    """One line when the plugin is the daemon's version but its hook scripts
    are not the daemon's, else ''.

    The version string cannot move without a release (it is pinned to the
    package version), so a plugin-only change on master reaches a user's
    cache only through a forced refresh; until they run it, the version
    handshake sees two equal strings. The digests tell the difference. A
    version difference is left to :func:`version_notice`, so a session
    never opens with two lines about the same thing. Both digests are
    shape-checked: the plugin's arrives on a query string and is echoed
    into the model's context.
    """
    if daemon_version is None:
        daemon_version = DAEMON_VERSION
    if daemon_digest is _DAEMON_DIGEST_UNSET:
        from pseudolife_memory.plugin_hooks import daemon_hooks_digest
        daemon_digest = daemon_hooks_digest()
    if not plugin_version or plugin_version != daemon_version:
        return ""
    if not isinstance(plugin_digest, str) or not isinstance(daemon_digest, str):
        return ""
    if not _DIGEST_SHAPE.fullmatch(plugin_digest) or not _DIGEST_SHAPE.fullmatch(daemon_digest):
        return ""
    if plugin_digest == daemon_digest:
        return ""
    return (f"Pseudolife-MCP: plugin {plugin_version} matches the daemon's version but "
            f"its hooks differ from the daemon's copy — run {ALL_UPDATE_COMMANDS} to "
            f"refresh the plugin cache and shim, then start a new session.")


def _version_key(value: str) -> tuple[int, ...] | None:
    """The leading dotted-integer part of a version as a sortable tuple
    (``0.15.0rc1`` → ``(0, 15, 0)``); ``None`` when there is none. No
    dependency on ``packaging``, which the daemon image does not declare."""
    match = re.match(r"^(\d+(?:\.\d+)*)", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def version_notice(plugin_version: str | None, daemon_version: str = DAEMON_VERSION) -> str:
    """One line when the plugin release differs from the daemon's, else ''.

    The plugin's hooks run from a cache that moves only on an explicit
    ``/plugin update``; the daemon moves on every deploy. Nothing compared
    the two until 2026-09-21, when a session ran the previous plugin against
    a newer daemon for an hour with no sign of it. The notice names which
    side is behind and the command that moves it. Without ``packaging`` or
    with a non-PEP-440 value, plain inequality still reports the mismatch,
    without saying which side is older.
    """
    if not plugin_version or not _VERSION_SHAPE.fullmatch(plugin_version):
        return ""
    if plugin_version == daemon_version:
        return ""
    head = f"Pseudolife-MCP: plugin {plugin_version} and daemon {daemon_version} differ"
    plugin_key, daemon_key = _version_key(plugin_version), _version_key(daemon_version)
    if plugin_key is not None and daemon_key is not None and plugin_key < daemon_key:
        return (f"{head} — update the plugin ({PLUGIN_UPDATE_COMMANDS}) and start "
                f"a new session; until then its hooks may lack what the daemon serves.")
    if plugin_key is not None and daemon_key is not None and plugin_key > daemon_key:
        return (f"{head} — redeploy the daemon from the matching checkout "
                f"({DAEMON_UPDATE_COMMANDS}); until then the plugin may call what "
                f"the daemon does not serve.")
    return (f"{head} — update the older one: plugin via {PLUGIN_UPDATE_COMMANDS}; "
            f"daemon via {DAEMON_UPDATE_COMMANDS}.")

MEMORY_LOOP_BLOCK = """\
## Memory — your long-term memory; use it every session (tools: `mcp__pseudolife-memory__*`)
One shared memory bank across all sessions. Treat it as a loop with three
beats: RECALL at the start, CAPTURE as you go, REFLECT at the end. Session
episodes are automatic; every memory you store is stamped to the current one.

RECALL — at the start of any task:
- `memory_search(<natural-language task>)` for prior context, decisions, gotchas.
- `memory_lesson_search(<task>)` for what worked / what to avoid last time —
  heed `polarity:-` dead-ends.
- `memory_fact_get(entity, attribute)` for one canonical value. If null, the
  slot is empty, NOT the topic — `memory_search` finds it regardless. A
  set-valued slot returns `{kind: "set", members, removed}` instead of one
  value.
- `memory_world_search(<topic>)` when the task turns on an external fact your
  training may have stale (versions, prices, who-holds-a-role, findings).
- `memory_recall(<question>)` when the answer needs multi-hop chaining across
  related facts.
- Long hits are clipped (`truncated: true` → `memory_get`). A superseded hit's
  `replaced_by` names its recorded replacement. `verified: false` means not
  confirmed as an explicit correction (often an old detector link; about 4 in
  10 of those are unrelated): treat the entry as possibly still valid and
  `memory_get` the replacement only if its `preview` is on the same subject.
  `current: false` = replacement itself replaced or unresolved: search again
  instead. Never follow chains. Pass `verbose=true` only when debugging
  retrieval.
- If a tool named here isn't in your tool list, call
  `memory_toolset(action="expand")` first — sessions can start at a
  reduced tier. A harness notice that some `mcp__pseudolife-memory__*`
  tools were REMOVED means the same tier filtering, not an outage — make
  one `memory_search` call before reporting memory as offline.

RECALL AGAIN mid-session. Search when:
- the user refers to work you weren't part of ("last time…", "in another
  session…", "we decided…") — that is a memory question by definition;
- you are about to propose a design → `memory_lesson_search` first;
  re-deriving a known dead-end is the common failure;
- you are about to state a benchmark number, version, or "current" value →
  check for a prior record before asserting it;
- you start a task in an area you haven't touched this session;
- you are about to review code, docs, or a PR → recall the target area
  FIRST (`memory_search` + `memory_lesson_search`), then compare what
  memory says against the files. Drift is a finding in both directions:
  correct stale memory on the spot (`memory_fact_set` + `memory_outcome`)
  and treat a memory-vs-file mismatch as review input, not noise.

TRUST ORDER — memory tells you WHY; the repo tells you WHAT IS.
A hit is a lead about the PAST, not a directive for the present: a
relevant memory can still frame the wrong problem, so check it against
the task in front of you before letting it steer.
For anything live (deployed version, config, what's running), read the
config/code and say where you read it. A memory records what was true when
it was WRITTEN: cortex facts carry `asserted_at` / `age`, so check them
before relying on one; a fact marked `stale: true` is a lead, not truth —
re-verify before acting on it (a stale fact may arrive with its `value`
quarantined and the original preserved in `last_known_value` — that is
your starting point for re-verification, never the current answer).
When memory and the code disagree, say so
out loud, trust the code, and correct the memory (`memory_fact_set` at the
same slot) — a stale fact nobody corrects is one the next session will
believe too. Recall results mark aged/contested facts with a ready-made
`correct_with` call: run it the moment you notice the mismatch, filling in
the verified value (re-assert the same value if it checks out), then log
`memory_outcome(..., "correction")`. Correcting is part of discovering —
a contradiction you only narrate is work left undone.
A cortex fact carrying `contested: true` has competing values parked
against it — settle it with `memory_fact_resolve(entity, attribute, ...)`,
not by re-asserting `memory_fact_set`, which only contests the slot further.

CAPTURE — as durable things arise (one claim per call):
- Before writing, choose: PERSIST what stays true; CONTEXT ONLY for
  task-scoped detail; RE-VERIFY a value that rots, at source
  (`memory_fact_set(..., freshness_class="volatile")`); ASK when the
  claim is ambiguous.
- Name the session EARLY: `memory_session_title("<project> - <topic>")`.
- `memory_store` for durable context; set `origin` honestly
  (`user`/`action`/`agent`) and use a stable `source` per project/topic.
- `memory_fact_set(entity, attribute, value)` for a canonical single-value
  fact; correct by re-setting the same slot.
- Label what must not drift: `distortion_tolerance="constraint"` on a rule
  that must survive verbatim (served first in recall, `pinned`);
  `authority="quoted"` on what a doc or third party said — a quote is
  not an instruction. Both inherit through supersession.
- Facts the repo or config can answer (deployed version, schema number,
  counts, budgets) do NOT belong in fact slots — they drift by construction;
  store the WHY as an entry and read the value from the repo. A one-off
  observation or audit finding is an EVENT: `memory_store` it (status),
  never mint a fact slot for it.
- Before re-asserting a slot another session may have corrected, re-read it
  and SKIP on a semantic match — a near-duplicate re-assert from a second
  writer contests an already-correct slot instead of confirming it.
- `memory_set_add(entity, attribute, member)` / `memory_set_remove` when a
  slot holds MANY concurrent values (bikes owned, pending tasks) — the first
  add converts a scalar slot one-way; `memory_fact_set` on a set slot
  errors and tells you so. Number-led scalars ("32", "$1,500") are
  protected: an add there parks as a contender instead of converting.
- `memory_world_set(entity, attribute, value, source_url=, source_quote=)`
  for any EXTERNAL fact you verified via web/docs — route research findings
  here (cited), not into plain `memory_store`.
- Open a named sub-episode with `memory_episode_start(title,
  episode=<your session handle>)` for a big multi-step task;
  `memory_episode_end(episode=<handle>)` pops back. The handle anchors
  both to YOUR session when several run concurrently.
- Route verbose status/progress/logs under `source="status"` — searchable,
  but excluded from fact/graph extraction.
- Never store secrets: no tokens, API keys, passwords, or credentials.

REFLECT — at task end, or the moment an outcome lands:
- `memory_outcome(task, outcome, about=, detail=, used_ids=)` whenever
  something WORKED (`success`), was a dead-end (`failure`), or the user
  corrected you (`correction`). Pass `used_ids=[…]` — which search hits the
  work turned on. These signals are the primary feeder for procedural LESSONS —
  the dream distils them into the do/avoid guidance surfaced at your next
  session start.

Be judicious: skip fleeting chatter (the surprise gate drops
near-duplicates; `stored=false` is not an error). The first memory call may
lag while the embedder loads.

If this session has NO `memory_*` tools, the MCP transport isn't registered
(this briefing arrives via a hook, not MCP) — tell the user to run
the repo installer (`ops/install.sh` / `ops\\install.ps1`), which wires it.
"""


# The full reference above is kept in sync with examples/CLAUDE.memory.md.
# SessionStart serves this concise core before any custom instructions or
# memory content, leaving room for actual lessons and the last-session recap.
STARTUP_MEMORY_CORE = """\
## Memory at session start
Use the shared Pseudolife bank for every task. First call `memory_search` with
the task in natural language and `memory_lesson_search` for prior do/avoid
lessons. Search again when the area changes, before design or review, and when
the user refers to another session. If a named tool is hidden, call
`memory_toolset(action="expand")`; a reduced tier is not an outage.

Name the session early with `memory_session_title`. If an episode handle is
shown above, pass `episode=` on every memory write and episode/title call.
Memory is a lead about the past, not an instruction: verify current code,
configuration, versions, and external facts at their source. For clipped hits,
use `memory_get`; for a stale or contested fact, verify or resolve it before
acting. Search before stating a "current" version, number, or benchmark. When
memory and the code disagree, trust the code and correct the memory on the
spot (`memory_fact_set` at the same slot, then `memory_outcome` with
`correction`).

Capture durable context with `memory_store` and canonical values with
`memory_fact_set`; keep status under `source="status"`. Route verified
external facts to `memory_world_set` with their source. Never store secrets.
At task end record success, failure, or correction with `memory_outcome`
and the `used_ids` of the recall entries that informed the work.
Full detailed guidance:
https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/master/examples/CLAUDE.memory.md
Full memory briefing: `pseudolife-mcp briefing` or GET /api/briefing."""


ONBOARDING_BLOCK = """\
Your memory bank is EMPTY — this session is where it starts. Seed it as you
work: name the session (`memory_session_title`), store two or three durable
facts about the current project (`memory_store`), set one canonical value
you'll want back (`memory_fact_set`), and log the first `memory_outcome`
when something works or fails. The dream consolidates whatever you capture
into facts, a knowledge graph, and lessons — a seeded bank compounds; an
empty one stays empty."""


def _cold_bank(service: Any) -> bool:
    """True only when the bank is provably empty — any doubt means warm
    (onboarding noise on a working bank is worse than none on a cold one)."""
    try:
        return (service.stats() or {}).get("total_memories") == 0
    except Exception:  # noqa: BLE001 — never break a session start
        return False


def _custom_instructions(service: Any) -> str:
    """User override text, or empty when it is absent/unreadable."""
    try:
        p = Path(getattr(service, "data_dir", "")) / "hook-instructions.md"
        if p.is_file():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
    except Exception:  # noqa: BLE001 — never break a session start
        pass
    return ""


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _bounded_custom_instructions(text: str, max_bytes: int) -> str:
    """Serve complete override paragraphs; warn when any are omitted.

    The custom file lives with the daemon's bank, which may be inaccessible
    from a remote client. A partial override therefore cannot be treated as
    the user's complete standing instructions.
    """
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not blocks or max_bytes <= 0:
        return ""
    warning = ("Custom instructions are partial. Obtain the complete "
               "daemon-side `<data_dir>/hook-instructions.md` before relying "
               "on this override.")

    def pack(cap: int) -> list[str]:
        chosen: list[str] = []
        for block in blocks:
            candidate = "\n\n".join([*chosen, block])
            if _utf8_len(candidate) <= cap:
                chosen.append(block)
        return chosen

    chosen = pack(max_bytes)
    if len(chosen) == len(blocks):
        return "\n\n".join(chosen)
    chosen = pack(max_bytes - _utf8_len(warning) - 2)
    rendered = "\n\n".join(chosen)
    candidate = rendered + ("\n\n" if rendered else "") + warning
    return candidate if _utf8_len(candidate) <= max_bytes else ""


def ab_arm_index(session_id: str, arms: int) -> int:
    """The online A/B arm of a client session: SHA-256 of its session id
    modulo the arm count. Not ``hash()``, which is salted per process — the
    main hook and the memory-policy hook are separate requests and must pick
    the same arm, across restarts too."""
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % arms


def memory_policy_variant(service: Any, session_id: str | None = None) -> str:
    """The startup memory-policy variant this session gets (config
    ``memory_policy``): its A/B arm when ``ab_arms`` is set and the session
    has an id, else ``variant``. Never raises; falls back to ``compact``."""
    try:
        cfg = service.config.memory_policy
        arms = list(cfg.ab_arms or ())
        if session_id and len(arms) >= 2:
            variant = arms[ab_arm_index(session_id, len(arms))]
        else:
            variant = cfg.variant
    except Exception:  # noqa: BLE001 — never break a session start
        return "compact"
    return variant if variant in MEMORY_POLICY_VARIANTS else "compact"


def _startup_policy(variant: str) -> str:
    """The policy text the main SessionStart output carries for a variant.
    ``full_separate_hook`` carries none here: its block has its own hook."""
    if variant == "compact":
        return STARTUP_MEMORY_CORE
    return ""


def session_start_context(service: Any, authorized: bool, *,
                          session_id: str | None = None,
                          max_bytes: int = HOOK_CONTEXT_MAX_CHARS) -> str:
    """Serve the variant's public policy text; private content requires auth."""
    variant = memory_policy_variant(service, session_id)
    policy = _startup_policy(variant)
    if _utf8_len(policy) > max_bytes:
        short = "Memory: call `memory_search` and `memory_lesson_search` at task start."
        return short if _utf8_len(short) <= max_bytes else ""
    parts = [policy] if policy else []

    def remaining() -> int:
        return max_bytes - _utf8_len("\n\n".join(parts)) - 2

    def add(text: str) -> None:
        if text and _utf8_len(text) <= remaining():
            parts.append(text)

    if authorized:
        custom = _custom_instructions(service)
        if custom:
            # Limit the override's startup share even when the file is huge.
            # The full file remains on the daemon side and omissions are
            # explicit. Reserve space for a useful briefing after it.
            add(_bounded_custom_instructions(custom, min(3_500, remaining() - 2_000)))
        # The onboarding block tells the agent which memory tools to call,
        # so the no-policy variant leaves it out as well.
        if variant != "none" and _cold_bank(service):
            add(ONBOARDING_BLOCK)
        try:
            # Coordination has an independent startup hook. Do not fetch it
            # here: a coordination failure must not suppress memory lessons.
            md = (service.session_briefing(include_coordination=False)
                  or {}).get("markdown", "") or ""
        except Exception:  # noqa: BLE001 — never break a session start
            md = ""
        if md.strip():
            from pseudolife_memory.memory.briefing import format_bounded_briefing
            add(format_bounded_briefing(md, remaining()))
    return "\n\n".join(parts)


def _episode_advertisement(session_id: str, source: str | None, service: Any) -> str:
    """Register the session (idempotent per ``session_id``) and return the
    one-line episode-handle advertisement, or "" on any failure (fail-open —
    a registration hiccup must not break session start). The registration
    also writes the session's durable ``client_sessions`` row (schema v43)
    with the memory-policy variant this hook serves it."""
    try:
        # Generic-shaped title so the auto-titler recognises and replaces it
        # at close (GENERIC_TITLE_RE) — a literal "session" would stick
        # forever (2026-07-19 whole-branch review, finding 2).
        import time as _t
        ep = service.episode_start_session(
            session_id, _t.strftime("session - %Y-%m-%d %H:%M"),
            registered_via="hook",
            policy_variant=memory_policy_variant(service, session_id))
        service.set_active_session(session_id)
        short = (ep.get("id") or "")[:12]
        if not short:
            return ""
        return (f'Session episode: {short} — pass episode="{short}" on every '
                f"memory write AND on memory_episode_start/end and "
                f"memory_session_title (keeps attribution correct even when "
                f"other sessions are open).")
    except Exception:  # noqa: BLE001 — never break a session start
        logger.exception(
            "session-start identity registration failed for session_id=%r "
            "(source=%r)", session_id, source)
        return ""


def _log_memory_policy(service: Any, session_id: str) -> None:
    """Log the policy variant a registered session was served. The durable
    account is the session's ``client_sessions.policy_variant`` (schema
    v43), written by the registration itself and kept when the session's
    root is pruned; this line is its copy in the daemon log."""
    try:
        logger.info("memory-policy variant %s for session %s",
                    memory_policy_variant(service, session_id), session_id[:12])
    except Exception:  # noqa: BLE001 — never break a session start
        pass


def hook_memory_policy(service: Any, session_id: str | None = None) -> str:
    """Text for the plugin's separate memory-policy SessionStart hook: the
    full ``MEMORY_LOOP_BLOCK`` when this session's variant is
    ``full_separate_hook``, else ''. A hook output of its own, so the block
    never competes with the briefing for the main output's budget. Never
    raises."""
    try:
        if memory_policy_variant(service, session_id) != "full_separate_hook":
            return ""
    except Exception:  # noqa: BLE001 — never break a session start
        return ""
    return MEMORY_LOOP_BLOCK if _utf8_len(MEMORY_LOOP_BLOCK) <= HOOK_CONTEXT_MAX_CHARS else ""


def hook_session_start(
    service: Any, session_id: str | None = None, source: str | None = None,
    authorized: bool = True, plugin_version: str | None = None,
    plugin_hooks_digest: str | None = None,
) -> str:
    """``session_start_context`` plus (when ``session_id`` is given) identity
    registration: opens/re-fires the session's episode, sets it as the active
    session (identity tier 3), and prepends the episode-handle advertisement.
    A ``plugin_version`` that differs from the daemon's puts
    :func:`version_notice` first of all; an equal version whose
    ``plugin_hooks_digest`` differs puts :func:`hooks_notice` there instead.
    Without ``session_id`` or a mismatch this is exactly
    ``session_start_context``'s behaviour. Never raises; the endpoint always
    answers 200."""
    prefix_parts = []
    notice = version_notice(plugin_version) or hooks_notice(plugin_version, plugin_hooks_digest)
    if notice:
        prefix_parts.append(notice)
    if session_id:
        ad = _episode_advertisement(session_id, source, service)
        if ad:
            prefix_parts.append(ad)
            _log_memory_policy(service, session_id)
    prefix = "\n\n".join(prefix_parts)
    body_budget = HOOK_CONTEXT_MAX_CHARS - _utf8_len(prefix) - (2 if prefix else 0)
    body = session_start_context(service, authorized, session_id=session_id,
                                 max_bytes=body_budget)
    return prefix + ("\n\n" if prefix and body else "") + body


def hook_session_end(service: Any, session_id: str | None = None) -> dict[str, Any]:
    """Close the session's episode and clear the active-session pointer (only
    if it still names ``session_id`` — the ownership guard means a foreign
    SessionEnd can't clear another session's pointer). Fail-open: errors are
    logged, never raised; always returns ``{"ok": True}``."""
    if session_id:
        try:
            service.episode_end_session(session_id)
        except Exception:  # noqa: BLE001 — never break a session end
            logger.exception(
                "session-end episode close failed for session_id=%r", session_id)
        try:
            service.clear_active_session(session_id)
        except Exception:  # noqa: BLE001 — never break a session end
            logger.exception(
                "session-end pointer clear failed for session_id=%r", session_id)
    return {"ok": True}
