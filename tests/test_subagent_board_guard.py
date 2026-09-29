"""The PreToolUse hook that lets a Claude Code subagent only read the board.

A subagent (Agent tool) runs inside its parent's MCP shim, so every board
call it makes carries the parent's identity (#425, schema v47): its status
update overwrote the parent's, its ack marked the parent's mail read before
the parent saw it, its send went out under the parent's name. The maintainer
decided on 2026-09-30 that subagents only read the board and never send.

Claude Code gives a plugin's PreToolUse hook ``agent_id`` and ``agent_type``
in stdin for a subagent's tool calls and neither for the parent's (measured
on 2.1.283, 2026-09-30), and a ``permissionDecision: deny`` reply blocks
only the call it answers. So the hook denies a subagent's board writes and
lets everything else through: the parent's calls, a subagent's reads, other
tools, and any payload it cannot read (the baseline before it was an
instruction only, so a broken guard must never block the parent).

Codex loads the same hooks.json and lists this entry (Codex 0.158.0,
``hooks/list``: ``pre_tool_use:0:0``), but a Codex child has a board address
of its own, so in Codex context the hook allows everything: the bash script
by the Codex markers the sibling hooks read, and ``lifecycle.ps1 -Event
SubagentBoardGuard`` (Codex's Windows command; Claude Code never runs
``commandWindows``) unconditionally.
"""
import json
import re

import pytest

from tests.test_codex_hooks import ROOT, bash_exe, isolated_env, pwsh_run

import subprocess

HOOK = ROOT / "plugin/hooks/subagent-board-guard.sh"
SESSION = "fixture-session"
CHILD = "a698026ca4ba524e9"
SERVER = "pseudolife-memory"


def _env(tmp_path, **extra):
    env = isolated_env(tmp_path / "codex-home")
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "PSEUDOLIFE_AGENT_COORDINATION"):
        env.pop(name, None)
    env.update({"CLAUDE_PLUGIN_ROOT": str(ROOT / "plugin"), "CLAUDECODE": "1",
                "CLAUDE_CODE_SESSION_ID": SESSION})
    env.update(extra)
    return env


def _payload(tool, tool_input, *, child=True, server=SERVER):
    """A PreToolUse payload in the key order Claude Code 2.1.283 sent it."""
    payload = {"session_id": SESSION, "transcript_path": "/tmp/t.jsonl", "cwd": "/tmp",
               "prompt_id": "p1", "permission_mode": "auto"}
    if child:
        payload.update(agent_id=CHILD, agent_type="general-purpose")
    payload.update(hook_event_name="PreToolUse",
                   tool_name=f"mcp__{server}__{tool}" if server else tool,
                   tool_input=tool_input, tool_use_id="toolu_fixture")
    if server:
        payload["mcp_server"] = {"name": server, "source": "user"}
    return json.dumps(payload)


def _run(env, stdin):
    result = subprocess.run([bash_exe(), str(HOOK)], input=stdin, env=env, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0 and result.stderr == "", result
    return result.stdout


def _denied(stdout):
    decision = json.loads(stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    return decision["permissionDecisionReason"]


WRITES = [("memory_agents", {"action": "update", "status": "reviewing"}),
          ("memory_agents", {"action": "claim", "lease": "claim:src"}),
          ("memory_agents", {"action": "release", "lease": "claim:src"}),
          ("memory_message", {"action": "send", "to": "all", "text": "hi", "request_id": "r1"}),
          ("memory_message", {"action": "ack", "message_id": "m1"})]
READS = [("memory_agents", {"action": "list"}),
         ("memory_agents", {}),                       # action defaults to list
         ("memory_agents", {"action": "list", "project": "x"}),
         ("memory_message", {"action": "receive"}),
         ("memory_message", {"action": "receive", "after": "abc:3"})]


@pytest.mark.parametrize("tool,tool_input", WRITES, ids=lambda v: v if isinstance(v, str) else v.get("action"))
def test_a_subagents_board_write_is_denied_with_a_reason_it_can_act_on(tmp_path, tool, tool_input):
    reason = _denied(_run(_env(tmp_path), _payload(tool, tool_input)))
    assert reason.startswith("Pseudolife board: ")
    assert f"{tool}(action={tool_input['action']})" in reason
    assert "shares its parent session's board address" in reason
    assert "Ask your parent session to update status, ack or send." in reason


@pytest.mark.parametrize("tool,tool_input", WRITES, ids=lambda v: v if isinstance(v, str) else v.get("action"))
def test_the_parents_own_board_write_is_untouched(tmp_path, tool, tool_input):
    assert _run(_env(tmp_path), _payload(tool, tool_input, child=False)) == ""


@pytest.mark.parametrize("tool,tool_input", READS, ids=lambda v: v if isinstance(v, str) else str(v))
def test_a_subagents_board_read_is_allowed(tmp_path, tool, tool_input):
    assert _run(_env(tmp_path), _payload(tool, tool_input)) == ""


@pytest.mark.parametrize("server", ["pseudolife-desktop", "memory", "some_other.name"])
def test_any_server_name_carrying_the_board_tools_is_guarded(tmp_path, server):
    stdin = _payload("memory_message", {"action": "send", "to": "all", "text": "x", "request_id": "r"},
                     server=server)
    assert _denied(_run(_env(tmp_path), stdin))


@pytest.mark.parametrize("tool", ["Bash", "mcp__pseudolife-memory__memory_search",
                                  "mcp__pseudolife-memory__memory_store",
                                  "mcp__pseudolife-memory__memory_agents_extra",
                                  "memory_agents", "mcp____memory_agents"])
def test_other_tools_from_a_subagent_are_untouched(tmp_path, tool):
    stdin = _payload(tool, {"action": "update", "command": "echo hi"}, server=None)
    assert _run(_env(tmp_path), stdin) == ""


def test_an_action_quoted_inside_a_message_is_not_the_calls_action(tmp_path):
    """The text is a JSON string, so the characters of a key inside it are
    escaped; only the real action key counts, wherever it sits."""
    tool_input = {"text": 'please "action":"receive", thanks', "to": "all",
                  "request_id": "r1", "action": "send"}
    assert _denied(_run(_env(tmp_path), _payload("memory_message", tool_input)))


def test_a_parent_message_quoting_agent_id_is_still_the_parents(tmp_path):
    tool_input = {"action": "send", "to": "all", "request_id": "r1",
                  "text": 'the hook reads {"agent_id":"a1"} from stdin'}
    assert _run(_env(tmp_path), _payload("memory_message", tool_input, child=False)) == ""


@pytest.mark.parametrize("stdin", [
    "", "not json", "{", '{"agent_id":"a1"}',
    # No tool_input at all, or one Claude Code did not send as an object.
    '{"agent_id":"a1","tool_name":"mcp__pseudolife-memory__memory_agents"}',
    '{"agent_id":"a1","tool_name":"mcp__pseudolife-memory__memory_agents","tool_input":"update"}',
    # An empty agent id is no subagent.
    '{"agent_id":"","tool_name":"mcp__pseudolife-memory__memory_agents","tool_input":{"action":"update"}}',
], ids=["empty", "text", "brace", "no-tool", "no-input", "string-input", "empty-agent"])
def test_a_payload_it_cannot_read_is_allowed(tmp_path, stdin):
    assert _run(_env(tmp_path), stdin) == ""


@pytest.mark.parametrize("value", ["0", "false", " OFF ", "no", "maybe"])
def test_the_coordination_opt_out_turns_the_guard_off(tmp_path, value):
    env = _env(tmp_path, PSEUDOLIFE_AGENT_COORDINATION=value)
    assert _run(env, _payload("memory_agents", {"action": "update"})) == ""


@pytest.mark.parametrize("value", ["1", "true", " Yes ", "on", ""])
def test_a_coordination_yes_keeps_the_guard_on(tmp_path, value):
    env = _env(tmp_path, PSEUDOLIFE_AGENT_COORDINATION=value)
    assert _denied(_run(env, _payload("memory_agents", {"action": "update"})))


@pytest.mark.parametrize("marker", ["manual", "plugin-root"])
def test_codex_context_allows_a_childs_board_write(tmp_path, marker):
    """A Codex child has a board address of its own: nothing here is the
    parent's to protect, and the daemon owns Codex's child rules."""
    extra = ({"PSEUDOLIFE_CODEX_HOOK": "1"} if marker == "manual"
             else {"PLUGIN_ROOT": str(ROOT / "plugin")})
    env = _env(tmp_path, **extra)
    env.pop("CLAUDECODE")
    env.pop("CLAUDE_CODE_SESSION_ID")
    assert _run(env, _payload("memory_agents", {"action": "update"})) == ""


def test_a_codex_run_nested_in_a_claude_bash_tool_is_still_codex(tmp_path):
    """It inherits CLAUDECODE and the outer session id, which is not the
    payload's (stop-wake.sh reads the same pair)."""
    env = _env(tmp_path, PLUGIN_ROOT=str(ROOT / "plugin"), CLAUDE_CODE_SESSION_ID="outer-claude")
    assert _run(env, _payload("memory_agents", {"action": "update"})) == ""


def test_claude_codes_own_hook_stays_claudes_whatever_codex_marker_it_inherits(tmp_path):
    env = _env(tmp_path, PSEUDOLIFE_CODEX_HOOK="1", PLUGIN_ROOT=str(ROOT / "plugin"))
    assert _denied(_run(env, _payload("memory_agents", {"action": "update"})))


def test_outside_claude_code_and_codex_it_does_nothing(tmp_path):
    env = _env(tmp_path)
    env.pop("CLAUDECODE")
    assert _run(env, _payload("memory_agents", {"action": "update"})) == ""


# ── hooks.json wiring ───────────────────────────────────────────────────────

def _entry():
    hooks = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    [group] = hooks["PreToolUse"]
    [handler] = group["hooks"]
    return group, handler


@pytest.mark.parametrize("name,matches", [
    ("mcp__pseudolife-memory__memory_agents", True),
    ("mcp__pseudolife-memory__memory_message", True),
    ("mcp__pseudolife-desktop__memory_agents", True),
    ("mcp__plugin_x_pseudolife__memory_message", True),
    ("mcp__pseudolife-memory__memory_search", False),
    ("mcp__pseudolife-memory__memory_agents_x", False),
    ("Bash", False),
    ("memory_agents", False),
])
def test_the_matcher_selects_only_the_board_tools(name, matches):
    """Claude Code matches a regex matcher against the tool name; anchored,
    so the guard never even starts for any other tool."""
    group, _ = _entry()
    assert bool(re.search(group["matcher"], name)) is matches


def test_the_entry_runs_the_guard_and_its_codex_windows_no_op():
    _, handler = _entry()
    assert handler["type"] == "command" and handler["timeout"] == 5
    assert handler["command"] == 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/subagent-board-guard.sh"'
    assert handler["commandWindows"].endswith("lifecycle.ps1\" -Event SubagentBoardGuard")
    assert "async" not in handler   # a deny has to arrive before the call runs


# ── the native command: Codex's only, a no-op ───────────────────────────────

@pytest.mark.parametrize("tool,tool_input", WRITES[:1] + READS[:1])
def test_the_native_command_allows_everything(tmp_path, tool, tool_input):
    """Claude Code never runs commandWindows (plugin/README.md, Windows), so
    only Codex on Windows reaches it, where children have their own
    board address. It reads its stdin and answers nothing, without a daemon."""
    env = _env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL="http://127.0.0.1:9")
    result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "SubagentBoardGuard",
                      input=_payload(tool, tool_input), env=env)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
