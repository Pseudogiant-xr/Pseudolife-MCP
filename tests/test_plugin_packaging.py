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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _strip_leading_html_comment(text: str) -> str:
    """Drop the '<!-- copy me -->' header the examples/ files carry."""
    return re.sub(r"^\s*<!--.*?-->\s*", "", text, flags=re.S)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


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


def test_plugin_mcp_json_points_at_daemon():
    """URL and token must honor the same env knobs as the hook script —
    a marketplace-installed plugin lives in a managed cache, so 'edit the
    plugin's .mcp.json' is not a real configuration path."""
    mcp = json.loads(_read("plugin/.mcp.json"))
    server = mcp["mcpServers"]["pseudolife-memory"]
    assert server["type"] == "http"
    assert server["url"] == "${PSEUDOLIFE_MCP_DAEMON_URL:-http://127.0.0.1:8765}/mcp"
    assert server["headers"]["Authorization"] == "Bearer ${PSEUDOLIFE_MCP_TOKEN}"


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
    assert "not reachable" in script          # the fallback guidance
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
    assert "-X POST" in script
    # Pin bearer-token forwarding in the actual curl invocation
    assert '"${AUTH[@]}"' in script
    # Pin session_id in the POST data payload (with shell escaping for -d flag)
    assert r'"{\"session_id\":\"${SID}\"}"' in script
    # Pin the endpoint target
    assert '"${URL}/api/hook/session-end"' in script
    assert re.search(r"^exit 0\s*$", script, re.M)   # must never block session end


# ── content sync ────────────────────────────────────────────────────────────

def test_memory_loop_block_matches_examples():
    """The daemon serves the standing instructions the CLAUDE.md append used
    to provide; the two sources must stay byte-identical (modulo the
    examples header comment)."""
    from pseudolife_memory.web.session_hook import MEMORY_LOOP_BLOCK
    examples = _strip_leading_html_comment(_read("examples/CLAUDE.memory.md"))
    assert MEMORY_LOOP_BLOCK.strip() == examples.strip()


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
    referenced = (_referenced_tools(MEMORY_LOOP_BLOCK)
                  | _referenced_tools(ONBOARDING_BLOCK))
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
