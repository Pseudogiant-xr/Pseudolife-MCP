# Session-start Briefing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-inject a session-start briefing — "what your memory is unsure about" (graph surprises + questions) + "lessons from past work" — via a server-side assembler, a `memory_briefing` tool, a torch-free `pseudolife-mcp briefing` CLI, and a documented SessionStart hook.

**Architecture:** A pure formatter/selector module (`memory/briefing.py`) turns already-fetched digest+lessons data into the markdown block. `MemoryService.session_briefing()` orchestrates (`graph_digest()` + `lessons_dump()` → formatter) and is exposed as the full-tier `memory_briefing` tool. A separate torch-free `briefing_cli.py` connects to an already-running daemon, calls the tool, and prints the markdown (nothing + exit 0 if the daemon's down or the bank is cold).

**Tech Stack:** Python 3.10+, the `mcp` client SDK (`streamablehttp_client` + `ClientSession`), pytest.

## Global Constraints

- Run tests offline: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`. Interpreter: `.venv/Scripts/python.exe`.
- The briefing CLI is **torch-free** (mirrors `shim.py`) and **must never auto-start the daemon** — if the daemon is unreachable, print nothing and `exit 0`. A briefing must never break session start.
- **ASCII-only** markdown markers (`avoid:` / `prefer:`), no emoji — the hook prints to a possibly-cp1252 Windows console; an encoding error must be impossible.
- `memory_briefing` is **full-tier** (`@_tool()`, not core) — core stays at 15.
- Data shapes (verified): `graph_digest()` → `{"available": bool, "digest": {"surprises": [{src,dst,relation,why,...}], "questions": [{type,question,why}], ...}}`; `service.lessons_dump(limit)` → `{"count", "entries": [{task,aspect,lesson,polarity,outcome,about,confidence,...}]}`.
- Spec: `docs/specs/2026-06-26-session-briefing-design.md`. Commit style: conventional commits, `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Out of scope: world facts in the briefing; any LLM call; auto-starting the daemon; a Console panel.

---

### Task 1: Pure formatter + lesson selector (`memory/briefing.py`)

**Files:**
- Create: `pseudolife_memory/memory/briefing.py`
- Test: `tests/test_briefing.py`

**Interfaces:**
- Produces:
  - `select_lessons(entries: list[dict], max_lessons: int) -> list[dict]` — avoid-first (polarity `-` or outcome `failure`/`correction`), then the rest in order, capped.
  - `format_briefing(surprises: list[dict], questions: list[dict], lessons: list[dict]) -> str` — the markdown block; `""` when there's nothing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_briefing.py`:

```python
from pseudolife_memory.memory.briefing import select_lessons, format_briefing


def test_select_lessons_prioritizes_avoid_then_recent():
    entries = [
        {"lesson": "use offline env", "polarity": "+", "outcome": "success"},
        {"lesson": "do not down -v", "polarity": "-", "outcome": "failure"},
        {"lesson": "correction here", "polarity": "+", "outcome": "correction"},
        {"lesson": "prefer X", "polarity": "+", "outcome": "success"},
    ]
    picked = select_lessons(entries, max_lessons=3)
    # the two avoid/correction lessons come first
    assert picked[0]["lesson"] == "do not down -v"
    assert picked[1]["lesson"] == "correction here"
    assert len(picked) == 3


def test_format_briefing_renders_both_sections_ascii():
    md = format_briefing(
        surprises=[{"src": "a", "dst": "b", "relation": "uses", "why": "bridge"}],
        questions=[{"question": "what runs where?"}],
        lessons=[{"lesson": "do not down -v", "polarity": "-", "outcome": "failure"},
                 {"lesson": "prefer offline", "polarity": "+", "outcome": "success"}],
    )
    assert "## What your memory is unsure about" in md
    assert "`a` uses `b`" in md and "what runs where?" in md
    assert "## Lessons from past work" in md
    assert "avoid: do not down -v" in md and "prefer: prefer offline" in md
    assert md.isascii()


def test_format_briefing_empty_when_nothing():
    assert format_briefing([], [], []) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_briefing.py -v`
Expected: FAIL — `pseudolife_memory.memory.briefing` does not exist.

- [ ] **Step 3: Implement the module**

Create `pseudolife_memory/memory/briefing.py`:

```python
"""Session-start briefing assembly — pure selection + markdown formatting.

No torch, no daemon: takes already-fetched digest/lessons data and produces the
injected markdown block. Kept out of the service so it's unit-testable. ASCII
only — the block is printed to a possibly-cp1252 console by the hook.
"""
from __future__ import annotations

_AVOID_OUTCOMES = {"failure", "correction"}


def _is_avoid(e: dict) -> bool:
    return e.get("polarity") == "-" or e.get("outcome") in _AVOID_OUTCOMES


def select_lessons(entries: list[dict], max_lessons: int) -> list[dict]:
    """Avoid-first (the 'do not repeat this' signal), then the rest in input
    order, capped at ``max_lessons``."""
    avoid, rest = [], []
    for e in entries:
        (avoid if _is_avoid(e) else rest).append(e)
    return (avoid + rest)[:max_lessons]


def _fmt_surprise(s: dict) -> str:
    src, dst = s.get("src", "?"), s.get("dst", "?")
    rel = s.get("relation") or "related-to"
    why = (s.get("why") or "").strip()
    tail = f" -- {why}" if why else ""
    return f"- `{src}` {rel} `{dst}`{tail}"


def _fmt_question(q: dict) -> str:
    text = (q.get("question") or "").strip()
    return f"- {text}" if text else ""


def _fmt_lesson(e: dict) -> str:
    marker = "avoid" if _is_avoid(e) else "prefer"
    text = (e.get("lesson") or "").strip()
    return f"- {marker}: {text}" if text else ""


def format_briefing(surprises: list[dict], questions: list[dict],
                    lessons: list[dict]) -> str:
    """Render the markdown block; empty string when there is nothing to say."""
    parts: list[str] = []
    unsure = [_fmt_surprise(s) for s in surprises]
    unsure += [_fmt_question(q) for q in questions]
    unsure = [ln for ln in unsure if ln]
    if unsure:
        parts.append("## What your memory is unsure about\n" + "\n".join(unsure))
    lesson_lines = [ln for ln in (_fmt_lesson(e) for e in lessons) if ln]
    if lesson_lines:
        parts.append("## Lessons from past work\n" + "\n".join(lesson_lines))
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_briefing.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/briefing.py tests/test_briefing.py
git commit -m "feat(briefing): pure formatter + avoid-first lesson selector (P1.7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `session_briefing()` service method + `memory_briefing` tool

**Files:**
- Modify: `pseudolife_memory/service.py` (new method, near `graph_digest` ~line 2530)
- Modify: `pseudolife_memory/mcp_server.py` (new `memory_briefing` tool)
- Test: `tests/test_briefing.py` (add), `tests/test_mcp_server.py` (tripwire)

**Interfaces:**
- Consumes: `select_lessons`, `format_briefing` (Task 1); `self.graph_digest()`, `self.lessons_dump(limit)`.
- Produces: `MemoryService.session_briefing(max_unsure=3, max_lessons=3) -> dict` returning `{"available": bool, "markdown": str, "unsure": {"surprises": [...], "questions": [...]}, "lessons": [...]}`; `memory_briefing(max_unsure=3, max_lessons=3)` MCP tool (full-tier).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_briefing.py`:

```python
def test_session_briefing_cold_bank_is_unavailable(tmp_path):
    from pseudolife_memory.service import MemoryService
    svc = MemoryService(data_dir=str(tmp_path))   # file mode, no graph/lessons
    out = svc.session_briefing()
    assert out["available"] is False
    assert out["markdown"] == ""
    assert out["unsure"] == {"surprises": [], "questions": []}
    assert out["lessons"] == []
```

(File mode has no graph digest and no lessons, so this exercises the cold-bank
path without Postgres.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_briefing.py::test_session_briefing_cold_bank_is_unavailable -v`
Expected: FAIL — `MemoryService` has no `session_briefing`.

- [ ] **Step 3: Implement the service method**

In `pseudolife_memory/service.py`, add this method to `MemoryService` immediately
after `graph_digest` (which ends ~line 2539). Do **not** wrap it in `self._lock` —
`graph_digest()` and `lessons_dump()` each take the (non-reentrant) lock
themselves:

```python
    def session_briefing(self, max_unsure: int = 3, max_lessons: int = 3) -> dict[str, Any]:
        """Assemble the session-start briefing: graph 'unsure-about' (surprising
        links + open questions) + avoid-first lessons. Read-only; no LLM. Each
        sub-call takes the lock itself, so this orchestrator must not hold it."""
        from pseudolife_memory.memory.briefing import format_briefing, select_lessons
        dg = self.graph_digest()
        surprises: list[dict] = []
        questions: list[dict] = []
        if dg.get("available"):
            d = dg.get("digest") or {}
            surprises = (d.get("surprises") or [])[:max_unsure]
            questions = (d.get("questions") or [])[:max_unsure]
        lessons_all = (self.lessons_dump(limit=120) or {}).get("entries", [])
        lessons = select_lessons(lessons_all, max_lessons)
        markdown = format_briefing(surprises, questions, lessons)
        return {
            "available": bool(markdown),
            "markdown": markdown,
            "unsure": {"surprises": surprises, "questions": questions},
            "lessons": lessons,
        }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_briefing.py::test_session_briefing_cold_bank_is_unavailable -v`
Expected: PASS.

- [ ] **Step 5: Add the `memory_briefing` tool**

In `pseudolife_memory/mcp_server.py`, add after the `memory_communities` tool
(`def memory_communities` block, ~line 1104):

```python
@_tool()
def memory_briefing(max_unsure: int = 3, max_lessons: int = 3) -> dict[str, Any]:
    """Session-start briefing: what your memory is unsure about (surprising graph
    links + open questions) plus lessons from past work (avoid / prefer). Pull this
    at the start of a task. Read-only; `available: false` + empty `markdown` on a
    cold bank (no dream digest, no lessons yet)."""
    return service.session_briefing(max_unsure=max_unsure, max_lessons=max_lessons)
```

- [ ] **Step 6: Update the registration tripwire**

In `tests/test_mcp_server.py`, add `"memory_briefing",` to the expected list in
`test_all_tools_registered` (e.g. right after `"memory_communities",`).

- [ ] **Step 7: Run the affected tests**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_briefing.py tests/test_mcp_server.py -q`
Expected: PASS — `test_all_tools_registered` now lists 47; the core-membership test is unchanged (still 15; `memory_briefing` is full-tier).

- [ ] **Step 8: Commit**

```bash
git add pseudolife_memory/service.py pseudolife_memory/mcp_server.py tests/test_briefing.py tests/test_mcp_server.py
git commit -m "feat(briefing): session_briefing() service method + memory_briefing tool (P1.7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `pseudolife-mcp briefing` CLI

**Files:**
- Create: `pseudolife_memory/briefing_cli.py`
- Modify: `pseudolife_memory/cli.py` (new `briefing` branch + docstring line)
- Test: `tests/test_briefing.py` (add)

**Interfaces:**
- Consumes: `pseudolife_memory.shim._daemon_url`, `pseudolife_memory.shim.probe_health`; the `memory_briefing` tool (Task 2).
- Produces: `pseudolife_memory.briefing_cli.run_briefing()` and `_extract_markdown(result)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_briefing.py`:

```python
def test_extract_markdown_prefers_structured():
    import types
    from pseudolife_memory import briefing_cli as bc
    r = types.SimpleNamespace(structuredContent={"markdown": "## hi\n- x"}, content=[])
    assert bc._extract_markdown(r) == "## hi\n- x"
    r2 = types.SimpleNamespace(structuredContent={"available": False, "markdown": ""}, content=[])
    assert bc._extract_markdown(r2) == ""


def test_briefing_no_daemon_prints_nothing(monkeypatch, capsys):
    import sys
    from pseudolife_memory import briefing_cli as bc
    monkeypatch.setattr("pseudolife_memory.shim.probe_health",
                        lambda url, timeout=0.25: None)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "briefing"])
    bc.run_briefing()                       # must not raise, must not print
    assert capsys.readouterr().out == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_briefing.py::test_extract_markdown_prefers_structured tests/test_briefing.py::test_briefing_no_daemon_prints_nothing -v`
Expected: FAIL — `pseudolife_memory.briefing_cli` does not exist.

- [ ] **Step 3: Implement the CLI client**

Create `pseudolife_memory/briefing_cli.py`:

```python
"""`pseudolife-mcp briefing` — print the session-start briefing markdown.

Torch-free client. Connects to an ALREADY-RUNNING daemon (never auto-starts;
session-start must stay fast) and prints the briefing markdown. Prints nothing +
exit 0 when the daemon is down, the bank is cold, or anything goes wrong — a
memory briefing must never break a session.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _extract_markdown(result) -> str:
    """Pull the ``markdown`` field from an mcp ClientSession.call_tool result."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("markdown", "") or ""
    content = getattr(result, "content", None) or []
    texts = [getattr(c, "text", "") for c in content]
    try:
        return (json.loads("".join(texts)) or {}).get("markdown", "") or ""
    except Exception:
        return ""


async def _fetch(url: str, token: str | None, max_unsure: int, max_lessons: int) -> str:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(url + "/mcp", headers=headers) as (read, write, _sid):
        async with ClientSession(read, write) as remote:
            await remote.initialize()
            result = await remote.call_tool(
                "memory_briefing",
                {"max_unsure": max_unsure, "max_lessons": max_lessons},
            )
    return _extract_markdown(result)


def run_briefing() -> None:
    import asyncio

    from pseudolife_memory.shim import _daemon_url, probe_health  # torch-free helpers

    ap = argparse.ArgumentParser(prog="pseudolife-mcp briefing")
    ap.add_argument("--max-unsure", type=int, default=3)
    ap.add_argument("--max-lessons", type=int, default=3)
    args, _ = ap.parse_known_args(sys.argv[2:])  # argv[1] == "briefing"

    url = _daemon_url()
    if probe_health(url) is None:
        return  # daemon down -> inject nothing
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None
    try:
        md = asyncio.run(_fetch(url, token, args.max_unsure, args.max_lessons))
    except Exception:
        return  # never break session start
    md = (md or "").strip()
    if md:
        print(md)
```

- [ ] **Step 4: Wire the CLI mode**

In `pseudolife_memory/cli.py`, add a branch before the `else:` in `main()`:

```python
    elif mode == "briefing":
        from pseudolife_memory.briefing_cli import run_briefing
        run_briefing()
```

And add one line to the module docstring's mode list (after the `embedded` line):

```
* ``pseudolife-mcp briefing``  — print the session-start briefing (for a hook).
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_briefing.py -q`
Expected: PASS (all briefing tests).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/briefing_cli.py pseudolife_memory/cli.py tests/test_briefing.py
git commit -m "feat(briefing): pseudolife-mcp briefing CLI (torch-free, never auto-starts) (P1.7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Document the SessionStart hook

**Files:**
- Modify: `README.md` (add a "Session-start briefing" subsection under "Recommended agent setup (CLAUDE.md)" / "Wire into Claude Code")
- Modify: `CHANGELOG.md` (`[Unreleased]` bullet)

**Interfaces:** docs only.

- [ ] **Step 1: Add the README subsection**

In `README.md`, after the "Recommended agent setup (CLAUDE.md)" section, add:

```markdown
### Session-start briefing (optional hook)

`pseudolife-mcp briefing` prints a compact markdown block — **what your memory is
unsure about** (surprising graph links + open questions) and **lessons from past
work** (avoid / prefer). Wire it to a SessionStart hook so every session opens
with it. In your Claude Code `settings.json`:

​```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "pseudolife-mcp briefing" } ] }
    ]
  }
}
​```

It connects to the *already-running* daemon (never starts one), and prints
nothing if the daemon is down or the bank is still cold — it can't slow or break
session start. Tune the budget with `--max-unsure N` / `--max-lessons N` (default
3 / 3). The same content is available on demand via the `memory_briefing` tool.
```

(Use real triple backticks for the JSON fence; the `​` above only marks where they
go in this plan.)

- [ ] **Step 2: Add the CHANGELOG bullet**

In `CHANGELOG.md`, under `## [Unreleased]` → `### Changed` (or a new `### Added`),
add:

```markdown
- **Session-start briefing (P1.7).** New `memory_briefing` tool + `pseudolife-mcp
  briefing` CLI assemble a "what your memory is unsure about" (graph surprises +
  questions) + "lessons from past work" (avoid/prefer) block. Wire the CLI to a
  SessionStart hook (README) to auto-inject it; it never auto-starts the daemon
  and prints nothing on a cold bank.
```

- [ ] **Step 3: Verify the docs**

Run: `grep -n "pseudolife-mcp briefing" README.md CHANGELOG.md`
Expected: matches in both files.

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs(briefing): document the SessionStart hook + memory_briefing (P1.7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Component 1 (`session_briefing()`) → Task 2. ✓
- Component 2 (`memory_briefing` tool, full-tier) → Task 2 Steps 5-6. ✓
- Component 3 (`pseudolife-mcp briefing` CLI, torch-free, no auto-start, empty→nothing) → Task 3. ✓
- Component 4 (SessionStart hook doc) → Task 4. ✓
- Content selection (avoid-first lessons; caps 3/3) → Task 1 `select_lessons` + Task 2 caps. ✓
- Graceful degradation (daemon down / cold / exception → nothing, exit 0) → Task 3 `run_briefing` guards + Task 2 cold-bank test. ✓
- Testing (assembler selection + markdown, tool dispatch, no-daemon smoke) → Tasks 1-3 tests. ✓
- Success criteria 1-4 → Tasks 1-4 respectively. ✓

**Placeholder scan:** none — every code step is complete. The README JSON fence uses a sentinel char with an explicit note (a documentation-of-documentation detail, not a code placeholder).

**Type consistency:** `select_lessons(entries, max_lessons)` and `format_briefing(surprises, questions, lessons)` are used identically in Task 1 (def + tests) and Task 2 (caller). `session_briefing(max_unsure, max_lessons) -> {available, markdown, unsure, lessons}` matches between Task 2's def, the tool wrapper, and Task 3's `_extract_markdown` (reads `markdown`). `_extract_markdown(result)` reads `.structuredContent` / `.content` — the mcp client `CallToolResult` shape used by `shim.py`.
