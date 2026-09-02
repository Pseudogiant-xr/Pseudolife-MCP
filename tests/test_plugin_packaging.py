"""Guards for the Claude Code plugin and its in-repo marketplace.

The plugin (``plugin/``) and marketplace manifest (``.claude-plugin/``)
duplicate content that lives elsewhere in the repo — the memory-loop
instruction block, the /dream command, the package version. Each copy gets a
sync guard here so a drift is a RED, not a support ticket
(spec: docs/superpowers/specs/2026-07-16-claude-code-plugin-design.md).
"""
from __future__ import annotations

import json
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _strip_leading_html_comment(text: str) -> str:
    """Drop the '<!-- copy me -->' header the examples/ files carry."""
    return re.sub(r"^\s*<!--.*?-->\s*", "", text, flags=re.S)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _assert_curl_fits_hook_budget(script: str, hook_groups: list) -> None:
    """The curl retry schedule must finish inside the hooks.json timeout —
    otherwise the harness kills the hook before the ``||`` fallback runs and
    the session gets NO message at all (the 'installed but silent' state).
    Worst case: every attempt runs to --max-time, plus the --retry-delay
    sleeps between attempts, plus 1s margin for bash/curl spawn."""
    max_time = int(re.search(r"--max-time\s+(\d+)", script).group(1))
    retries = int(re.search(r"--retry\s+(\d+)", script).group(1))
    delay = int(re.search(r"--retry-delay\s+(\d+)", script).group(1))
    budget = min(h["timeout"] for g in hook_groups for h in g["hooks"])
    worst = (retries + 1) * max_time + retries * delay
    assert worst + 1 <= budget, (
        f"curl worst case {worst}s + 1s spawn margin exceeds the "
        f"hooks.json timeout of {budget}s")


# ── manifests ───────────────────────────────────────────────────────────────

def test_marketplace_manifest_points_at_plugin_dir():
    mp = json.loads(_read(".claude-plugin/marketplace.json"))
    assert mp["name"] == "pseudolife-mcp"
    assert mp["owner"]["name"]
    entries = {p["name"]: p for p in mp["plugins"]}
    entry = entries["pseudolife-memory"]
    assert entry["source"] == "./plugin"
    # the registry description cap taught us short descriptions travel best
    assert len(entry.get("description", "")) <= 200


def test_plugin_manifest_version_matches_pyproject():
    """The release version-cut touches this file too (CLAUDE.md checklist)."""
    manifest = json.loads(_read("plugin/.claude-plugin/plugin.json"))
    assert manifest["name"] == "pseudolife-memory"
    version = re.search(r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"),
                        re.M).group(1)
    assert manifest["version"] == version


def test_package_version_matches_pyproject():
    """``pseudolife_memory.__version__`` used to be a second hand-maintained
    literal that drifted eight releases behind pyproject.toml with nothing
    to catch it (issue #180). It now derives from the installed
    distribution's metadata via ``importlib.metadata``, so it is correct for
    anything actually built/shipped (every build backend stamps dist-info
    from pyproject.toml at build time).

    That derivation is only as fresh as the *installed* dist-info, though —
    a local editable install that was never reinstalled after a version bump
    can carry stale metadata. That happens in this dev environment; it never
    happens for a real `pip install` or a CI build. Skip (with a clear
    reason) only in that stale-local-install case; the equality against
    whatever metadata *is* installed is always asserted, since a drift there
    would be a real code bug."""
    pyproject_version = re.search(r'^version\s*=\s*"([^"]+)"',
                                   _read("pyproject.toml"), re.M).group(1)

    try:
        installed_version = version("pseudolife-mcp")
    except PackageNotFoundError:
        pytest.skip("pseudolife-mcp is not installed in this environment — "
                     "cannot compare __version__ against package metadata")

    from pseudolife_memory import __version__
    assert __version__ == installed_version

    if installed_version != pyproject_version:
        pytest.skip(
            f"installed dist-info reports {installed_version!r} but "
            f"pyproject.toml says {pyproject_version!r} — stale editable "
            "install in this dev environment (reinstall with "
            "`pip install -e .` to refresh); not a code bug")

    assert __version__ == pyproject_version


def test_server_json_versions_match_pyproject():
    """The MCP registry's version guard used to run in the `registry` GitHub
    Actions job, which only executes AFTER the irreversible PyPI publish
    (issue #185) — a version-field mismatch burned a release number instead
    of failing before anything uploaded. server.json carries the version in
    two places that drift independently, and the compose daemon image tag
    (`ops/docker-compose.yml`) calls itself the deploy source of truth but
    was unguarded against pyproject.toml too. Pin all three here so a bad
    version cut fails locally, before any workflow runs."""
    pyproject_version = re.search(r'^version\s*=\s*"([^"]+)"',
                                   _read("pyproject.toml"), re.M).group(1)

    server = json.loads(_read("server.json"))
    assert server["version"] == pyproject_version
    assert server["packages"][0]["version"] == pyproject_version

    compose = _read("ops/docker-compose.yml")
    match = re.search(r"^\s*image:\s*pseudolife-daemon:(\S+)$", compose, re.M)
    assert match, "ops/docker-compose.yml must pin the daemon image tag to a version"
    assert match.group(1) == pyproject_version


def test_plugin_ships_no_mcp_server():
    """The plugin is the hooks/commands layer only (2026-07-19): MCP transport
    is registered by ops/install.* (stdio shim by default — per-session
    identity) or the README one-liner. A bundled server would sit beside that
    registration and double every session's tool namespace — Claude Code loads
    both with no dedup, and the only off-switch is disabling the whole plugin,
    which would also kill the identity/briefing hooks."""
    assert not (ROOT / "plugin" / ".mcp.json").exists()
    manifest = json.loads(_read("plugin/.claude-plugin/plugin.json"))
    assert "mcpServers" not in manifest


def test_plugin_hook_wiring():
    """SessionStart must curl the hook endpoint via the bundled bash script
    (official-plugin pattern; Git Bash on Windows), and the script must carry
    a daemon-down fallback so 'installed but silent' can't happen."""
    hooks = json.loads(_read("plugin/hooks/hooks.json"))
    groups = hooks["hooks"]["SessionStart"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh" in c
               for c in commands)

    script = _read("plugin/hooks/session-start.sh")
    assert "/api/hook/session-start" in script
    assert "curl" in script and "--max-time" in script
    # A single attempt turns a transient daemon stall (autosave/sweep lock
    # hold) into a false "daemon down" for the whole session — the curl
    # must carry an explicit retry count (plain --retry already classes a
    # timeout as transient; --retry-all-errors is banned below because it
    # breaks option parsing outright on curl < 7.71, common on LTS hosts).
    assert re.search(r"--retry\s+\d+", script), \
        "retry count must be set; retry flags without --retry <n> are no-ops"
    assert "--retry-delay" in script
    # Banned on the command itself (comments may name it to explain why):
    # it breaks option parsing outright on curl < 7.71, common on LTS hosts.
    assert not any("--retry-all-errors" in ln for ln in script.splitlines()
                   if not ln.lstrip().startswith("#"))
    assert "did not answer" in script         # the fallback guidance
    # The fallback must instruct verification, not assert the MCP tools are
    # dead: a hook timeout proves nothing about the separate MCP transport.
    assert "memory_stats" in script
    assert "before treating memory as offline" in script
    assert re.search(r"^exit 0\s*$", script, re.M)  # must never block session start
    _assert_curl_fits_hook_budget(script, hooks["hooks"]["SessionStart"])
    # Pin bearer-token forwarding in the actual curl call
    assert '"${AUTH[@]}"' in script
    # Pin query-string construction and forwarding to curl (session_id/source)
    assert '"${URL}/api/hook/session-start${QS}"' in script


def test_plugin_hook_wiring_session_end():
    """SessionEnd must be wired to session-end.sh, mirroring SessionStart's
    group structure, and the script must POST the episode-close call while
    never blocking session end (always exit 0) and honoring the bearer
    token the same way session-start.sh does."""
    hooks = json.loads(_read("plugin/hooks/hooks.json"))
    groups = hooks["hooks"]["SessionEnd"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("${CLAUDE_PLUGIN_ROOT}/hooks/session-end.sh" in c
               for c in commands)

    script = _read("plugin/hooks/session-end.sh")
    assert "/api/hook/session-end" in script
    assert "curl" in script and "--max-time" in script
    # Same transient-stall retry contract as session-start.sh.
    assert re.search(r"--retry\s+\d+", script), \
        "retry count must be set; retry flags without --retry <n> are no-ops"
    assert "--retry-delay" in script
    # Banned on the command itself (comments may name it to explain why):
    # it breaks option parsing outright on curl < 7.71, common on LTS hosts.
    assert not any("--retry-all-errors" in ln for ln in script.splitlines()
                   if not ln.lstrip().startswith("#"))
    _assert_curl_fits_hook_budget(script, hooks["hooks"]["SessionEnd"])
    assert "-X POST" in script
    # Pin bearer-token forwarding in the actual curl invocation
    assert '"${AUTH[@]}"' in script
    # Pin session_id in the POST data payload (with shell escaping for -d flag)
    assert r'"{\"session_id\":\"${SID}\"}"' in script
    # Pin the endpoint target
    assert '"${URL}/api/hook/session-end"' in script
    assert re.search(r"^exit 0\s*$", script, re.M)   # must never block session end


def test_plugin_hook_wiring_user_prompt_submit():
    """UserPromptSubmit must echo the static mid-session discipline line —
    including the recall-before-review clause (recall the target area, then
    compare memory against the files and correct drift both ways). Static by
    design: the hook fires on every user turn, so no daemon round-trip and no
    network dependency, and it must never block a turn."""
    hooks = json.loads(_read("plugin/hooks/hooks.json"))
    groups = hooks["hooks"]["UserPromptSubmit"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("${CLAUDE_PLUGIN_ROOT}/hooks/user-prompt-submit.sh" in c
               for c in commands)

    script = _read("plugin/hooks/user-prompt-submit.sh")
    assert "curl" not in script       # static: every-turn, offline-safe
    for phrase in ("reviewing code, docs, or a PR", "memory_search",
                   "memory_lesson_search", "compare"):
        assert phrase in script, f"discipline line lost: {phrase!r}"
    assert re.search(r"^exit 0\s*$", script, re.M)   # must never block a turn


# ── content sync ────────────────────────────────────────────────────────────

def test_discipline_line_synced_across_plugin_and_installers():
    """The per-turn discipline line ships in THREE copies — the plugin script
    plus both installers. Drift means plugin users and installer users carry
    different standing instructions forever, silently. Extract the literal
    from each copy and pin: exactly one occurrence per file, all identical,
    the installers' idempotency needle inside the line itself (a needle that
    only matches the file would let a reworded line duplicate the hook on
    every re-run), and a length budget — the line is injected on EVERY user
    turn, so growth is a per-turn context tax."""
    pat = re.compile(r"(Memory \(PseudoLife\) mid-session discipline:[^'\"\n]*)")
    lines = {}
    for rel in ("plugin/hooks/user-prompt-submit.sh",
                "ops/install-hook.sh", "ops/install-hook.ps1"):
        found = pat.findall(_read(rel))
        assert len(found) == 1, f"{rel}: expected 1 discipline line, got {len(found)}"
        lines[rel] = found[0]
    assert len(set(lines.values())) == 1, f"discipline line drift: {lines}"
    line = lines["plugin/hooks/user-prompt-submit.sh"]
    assert "mid-session discipline" in line   # the installers' idempotency needle
    assert len(line) <= 800                   # every-turn cost budget (~700 on 2026-08-28)


def test_memory_loop_block_leaves_briefing_headroom():
    """The session-start payload is instructions + briefing, sliced to
    HOOK_CONTEXT_MAX_CHARS with the briefing LAST — every char the block
    grows is silently cut from the briefing tail (lessons, unsure-abouts,
    where-we-left-off). Reserve 2,000 chars for the briefing (the block
    measured 6,663 on 2026-08-28, 7,316 on 2026-09-02 after the
    write-policy and trap-avoidance text, and 7,491 later that day after
    the v35 label line — which funded itself by two trims and left 9
    chars of reserve, so the next addition trims first or the cap moves
    deliberately; a truncated briefing has no other alarm)."""
    from pseudolife_memory.web.session_hook import (HOOK_CONTEXT_MAX_CHARS,
                                                    MEMORY_LOOP_BLOCK)
    assert len(MEMORY_LOOP_BLOCK) <= HOOK_CONTEXT_MAX_CHARS - 2_000


def test_memory_loop_block_carries_recall_before_review_trigger():
    """Reviews recall memory FIRST, then compare against the files — codified
    in the served instructions so every install gets the rule (2026-08-28):
    drift found during a review is capture-worthy data in both directions
    (stale memory gets corrected; a memory-vs-file mismatch is review input)."""
    from pseudolife_memory.web.session_hook import MEMORY_LOOP_BLOCK
    text = " ".join(MEMORY_LOOP_BLOCK.split())
    assert "review code, docs, or a PR" in text
    assert "compare what memory says against the files" in text


def test_memory_loop_block_matches_examples():
    """The daemon serves the standing instructions the CLAUDE.md append used
    to provide; the two sources must stay byte-identical (modulo the
    examples header comment)."""
    from pseudolife_memory.web.session_hook import MEMORY_LOOP_BLOCK
    examples = _strip_leading_html_comment(_read("examples/CLAUDE.memory.md"))
    assert MEMORY_LOOP_BLOCK.strip() == examples.strip()


def test_memory_loop_block_explains_tier_removal_notices():
    """A resumed session whose stale context carried full-tier tools sees a
    harness notice that several memory tools were REMOVED; a live session
    (2026-08-28) read exactly that as "the memory MCP is offline" and told
    the user so, while the daemon was healthy and every core tool worked.
    The briefing must pre-empt the misread: removed memory tools usually
    mean tier filtering, and one live call settles it."""
    from pseudolife_memory.web.session_hook import MEMORY_LOOP_BLOCK
    assert "tier filtering" in MEMORY_LOOP_BLOCK
    assert "not an outage" in MEMORY_LOOP_BLOCK


def test_plugin_dream_command_matches_examples():
    plugin = _read("plugin/commands/dream.md")
    examples = _strip_leading_html_comment(_read("examples/commands/dream.md"))
    assert plugin.strip() == examples.strip()


# ── tool-surface guards ─────────────────────────────────────────────────────

def _referenced_tools(text: str) -> set[str]:
    """Backticked tool mentions, e.g. `memory_search(...)` → memory_search.
    Deliberately skips `mcp__pseudolife-memory__*` (prefix, not a tool)."""
    return set(re.findall(r"`((?:memory|document)_[a-z_]+)", text))


def test_instruction_blocks_reference_only_core_visible_tools():
    """The standing instructions are injected into EVERY session; a tool they
    name must exist and stay visible if PSEUDOLIFE_MCP_TOOLSET=core ever
    flips on (the 2026-07-10 tool-cost review's pending lever) — otherwise
    the block tells the model to call tools its tools/list doesn't carry."""
    from pseudolife_memory.mcp_server import _TOOL_TIERS
    from pseudolife_memory.web.session_hook import (MEMORY_LOOP_BLOCK,
                                                    ONBOARDING_BLOCK)
    # The UserPromptSubmit line is injected every turn — same visibility bar.
    ups = re.findall(r"\b((?:memory|document)_[a-z_]+)",
                     _read("plugin/hooks/user-prompt-submit.sh"))
    referenced = (_referenced_tools(MEMORY_LOOP_BLOCK)
                  | _referenced_tools(ONBOARDING_BLOCK)
                  | set(ups))
    assert len(referenced) >= 10          # regex sanity — the block names many

    unknown = referenced - set(_TOOL_TIERS)
    assert not unknown, f"instruction blocks name unregistered tools: {unknown}"

    hidden_at_core = {t for t in referenced if _TOOL_TIERS[t] == "full"}
    assert not hidden_at_core, (
        f"instruction blocks name full-tier tools hidden at core: "
        f"{hidden_at_core} — promote them or drop the mention")


def test_plugin_commands_reference_only_real_tools():
    """Commands are on-demand (a session can expand its tier first), so
    existence is the bar here, not core visibility."""
    from pseudolife_memory.mcp_server import _TOOL_TIERS
    for rel in ("plugin/commands/dream.md", "plugin/commands/memory-status.md"):
        referenced = _referenced_tools(_read(rel))
        assert referenced, f"{rel}: regex found no tool mentions"
        unknown = referenced - set(_TOOL_TIERS)
        assert not unknown, f"{rel} names unregistered tools: {unknown}"


def test_memory_loop_block_carries_the_write_policy_boundary():
    """MCB (arXiv 2608.19564): agents over-persist ambiguous
    interaction-derived information, and a prompt naming the four options
    — persist / context-only / re-verify / ask — cut erroneous persistence
    0.243 -> 0.100. The CAPTURE section is where the served text tells the
    model what to write, so the gate lives there (2026-09-02)."""
    from pseudolife_memory.web.session_hook import MEMORY_LOOP_BLOCK
    text = " ".join(MEMORY_LOOP_BLOCK.split())
    assert "PERSIST what stays true" in text
    assert "CONTEXT ONLY" in text
    assert "RE-VERIFY" in text and 'freshness_class="volatile"' in text
    assert "ASK when the claim is ambiguous" in text


def test_memory_loop_block_carries_trap_avoidance_guidance():
    """MemTrapBench (arXiv 2608.20202): relevant, faithfully-recorded
    memories still anchor models on a stale framing, and every memory
    framework tested underperformed no-memory until an inference-time
    instruction was added. The block's TRUST ORDER paragraph already
    ranks memory below the repo; this extends it to the framing a hit
    carries, and covers every recall tool at once — including
    ``memory_recall``, whose own description stays untouched
    (2026-09-02)."""
    from pseudolife_memory.web.session_hook import MEMORY_LOOP_BLOCK
    text = " ".join(MEMORY_LOOP_BLOCK.split())
    assert "a lead about the PAST, not a directive for the present" in text
    assert "frame the wrong problem" in text
