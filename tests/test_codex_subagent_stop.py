"""The park gate at a Codex child's stop (SubagentStop).

A Codex native child thread (``collaboration.spawn_agent``) or fork has its
own board address: the shim keys its identity by the MCP ``_meta.threadId``
and writes the child's ``<key>.agent`` record (and digest) under the SHA-256
of that thread id (``pseudolife_memory/codex_coordination.py``). Codex's
SubagentStop payload names the child as ``agent_id``, which equalled the
child's MCP threadId in the 2026-09-29 probe on Codex CLI 0.158.0 and desktop
0.158.0-alpha.2.1, while ``session_id`` stayed the root thread's. So the
child's stop runs the root Stop's park gate keyed by the validated
``agent_id``, never by the root ``session_id``; a block continues the child
once (its next stop carries ``stop_hook_active``). In Claude Code, which
also fires SubagentStop, the entry is a no-op.
"""
import hashlib
import json
import subprocess

import pytest

from tests.test_codex_hook_setup_edges import (
    approving_runtime, options, plugin_hooks, seed_user_files, setup)
from tests.test_codex_hooks import ROOT, bash_exe, pwsh_run
from tests.test_stop_wake_hook import (
    GATE_MESSAGE, HOOK, STARTED, _env, _finish, _gate_daemon, _read_seen)

ROOT_THREAD = "019a4c3e-0000-7000-8000-00000000a0a0"
CHILD_THREAD = "019a4c3e-7b2d-7f10-9c3a-5e6f7a8b9c0d"
ROOT_ADDRESS = "a" * 32
CHILD_ADDRESS = "c" * 32


def _key(thread):
    return hashlib.sha256(thread.encode()).hexdigest()


def _address(tmp_path, thread, address):
    """The shim's ``<key>.agent`` record naming a thread's board address."""
    directory = tmp_path / "digests"
    directory.mkdir(exist_ok=True)
    (directory / f"{_key(thread)}.agent").write_text(f"{address}\n")


def _payload(agent_id=CHILD_THREAD, **extra):
    """SubagentStop stdin as Codex 0.158 sends it (fields from the probe)."""
    body = {"session_id": ROOT_THREAD, "turn_id": "turn-1", "hook_event_name": "SubagentStop",
            "stop_hook_active": False, "last_assistant_message": "done",
            "permission_mode": "default", "agent_type": "default",
            "agent_transcript_path": "/tmp/child.jsonl", "cwd": "/tmp"}
    if agent_id is not None:
        body["agent_id"] = agent_id
    body.update(extra)
    return json.dumps(body)


def _codex_env(tmp_path, server, **extra):
    env = _env(tmp_path, wait=8, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
               PSEUDOLIFE_MCP_TOKEN="fixture-token", **extra)
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        env.pop(name, None)
    env["PSEUDOLIFE_CODEX_HOOK"] = "1"
    return env


def _bash(env, payload):
    process = subprocess.Popen([bash_exe(), str(HOOK), "subagent-stop"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    process.stdin.write(payload)
    process.stdin.close()
    STARTED.append(process)
    return _finish(process)


def _pwsh(env, payload):
    result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "SubagentStop",
                      input=payload, env=env)
    return result.returncode, result.stdout, result.stderr


RUNNERS = {"windows": _pwsh, "bash": _bash}


@pytest.fixture
def daemon():
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    yield server, requests
    server.shutdown()
    server.server_close()


# --- registration -----------------------------------------------------------

def _subagent_stop_hook():
    hooks = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    groups = [g for g in hooks.get("SubagentStop", [])
              if any("stop-wake.sh" in h["command"] for h in g["hooks"])]
    assert len(groups) == 1, "one SubagentStop group of its own runs the child park gate"
    [hook] = groups[0]["hooks"]
    return hook


def test_hooks_json_binds_subagent_stop_to_the_child_park_gate():
    hook = _subagent_stop_hook()
    # Synchronous: Codex reads the block from stdout, and Codex before 0.148
    # skipped async hooks outside SessionEnd.
    assert hook["type"] == "command" and "async" not in hook and "asyncRewake" not in hook
    # A copy that does not parse must not exit 2, which Claude Code reads as
    # a block of its own subagent.
    assert hook["command"] == (
        'bash -n "${CLAUDE_PLUGIN_ROOT}/hooks/stop-wake.sh" 2>/dev/null || exit 0; '
        'exec bash "${CLAUDE_PLUGIN_ROOT}/hooks/stop-wake.sh" subagent-stop')
    assert hook["commandWindows"].endswith('lifecycle.ps1" -Event SubagentStop')
    assert 5 <= hook["timeout"] <= 15


# --- the gate, keyed by the child ------------------------------------------

@pytest.mark.parametrize("runner", sorted(RUNNERS))
def test_a_childs_stop_asks_the_gate_for_the_childs_own_address(tmp_path, daemon, runner):
    """The child's record is consulted, not the root's: the request names the
    child's board address, and the block is Codex's documented decision."""
    server, requests = daemon
    _address(tmp_path, ROOT_THREAD, ROOT_ADDRESS)
    _address(tmp_path, CHILD_THREAD, CHILD_ADDRESS)
    env = _codex_env(tmp_path, server)
    code, out, err = RUNNERS[runner](env, _payload())
    assert (code, err) == (0, "")
    assert json.loads(out) == {"decision": "block", "reason": GATE_MESSAGE}
    # No child turn stamp exists (a child gets no UserPromptSubmit), so the
    # daemon judges the child's standing record.
    assert requests == [("/api/hook/park-gate?agent=" + CHILD_ADDRESS, "Bearer fixture-token")]
    ledger = (tmp_path / "digests" / "ledger.log").read_text().splitlines()
    assert [line.split("\t")[1:3] for line in ledger] == [["gate", _key(CHILD_THREAD)[:8]]]
    # The continuation's stop is not asked again.
    code, out, err = RUNNERS[runner](env, _payload(stop_hook_active=True))
    assert (code, out, err, len(requests)) == (0, "", "", 1)
    # Nothing of the wake hook's: no lease, no seen marker.
    assert not (tmp_path / "digests" / f"{_key(CHILD_THREAD)}.wake").exists()
    assert _read_seen(tmp_path, key=_key(CHILD_THREAD)) is None


@pytest.mark.parametrize("runner", sorted(RUNNERS))
@pytest.mark.parametrize("agent_id", [
    None,                                         # no agent_id at all
    "",
    CHILD_THREAD.upper(),                         # not the shim's canonical form
    CHILD_THREAD.replace("-", ""),
    "{" + CHILD_THREAD + "}",
    CHILD_THREAD + "0",
    CHILD_THREAD[:8] + CHILD_THREAD[9] + "-" + CHILD_THREAD[10:],   # a hyphen out of place
    "../" + CHILD_THREAD[3:],
    ROOT_ADDRESS,                                 # a board address, not a thread id
    12345,
])
def test_without_a_valid_child_id_the_gate_is_not_asked(tmp_path, daemon, runner, agent_id):
    """Only the thread-id shape the shim accepts from ``_meta.threadId`` keys
    the lookup; anything else asks nothing, and never falls back to the root
    ``session_id`` (whose record exists here)."""
    server, requests = daemon
    _address(tmp_path, ROOT_THREAD, ROOT_ADDRESS)
    _address(tmp_path, CHILD_THREAD, CHILD_ADDRESS)
    if isinstance(agent_id, str) and agent_id:
        # A record under the malformed id itself: only the shape check
        # stands between it and a request.
        _address(tmp_path, agent_id, "b" * 32)
    env = _codex_env(tmp_path, server)
    code, out, err = RUNNERS[runner](env, _payload(agent_id=agent_id))
    assert (code, out, err, requests) == (0, "", "", [])


@pytest.mark.parametrize("runner", sorted(RUNNERS))
def test_a_child_without_its_own_record_is_not_asked(tmp_path, daemon, runner):
    """A child the shim never attached (no ``.agent`` under its key) has no
    address to ask about; the root's record is not a stand-in."""
    server, requests = daemon
    _address(tmp_path, ROOT_THREAD, ROOT_ADDRESS)
    env = _codex_env(tmp_path, server)
    code, out, err = RUNNERS[runner](env, _payload())
    assert (code, out, err, requests) == (0, "", "", [])


@pytest.mark.parametrize("runner", sorted(RUNNERS))
@pytest.mark.parametrize("setting", [("PSEUDOLIFE_AGENT_WAKE_HOOK", "0"),
                                     ("PSEUDOLIFE_AGENT_COORDINATION", "off")])
def test_the_child_gate_honours_an_explicit_opt_out(tmp_path, daemon, runner, setting):
    server, requests = daemon
    _address(tmp_path, CHILD_THREAD, CHILD_ADDRESS)
    env = _codex_env(tmp_path, server)
    env[setting[0]] = setting[1]
    code, out, err = RUNNERS[runner](env, _payload())
    assert (code, out, err, requests) == (0, "", "", [])


def test_a_payload_naming_agent_id_twice_is_refused(tmp_path, daemon):
    """The bash hook reads top-level fields by pattern: two ``agent_id``
    keys cannot be told apart, so neither is used."""
    server, requests = daemon
    _address(tmp_path, CHILD_THREAD, CHILD_ADDRESS)
    payload = _payload()[:-1] + ', "agent_id": "' + CHILD_THREAD + '"}'
    code, out, err = _bash(_codex_env(tmp_path, server), payload)
    assert (code, out, err, requests) == (0, "", "", [])


@pytest.mark.parametrize("marker", [None, ("PSEUDOLIFE_CODEX_HOOK", "1"), ("PLUGIN_ROOT", None)])
def test_in_claude_code_the_entry_is_a_no_op(tmp_path, daemon, marker):
    """Claude Code fires SubagentStop too and runs the bash command (it
    ignores commandWindows): its own session's hook does nothing here, even
    with a record under the subagent's id and a leaked Codex marker."""
    server, requests = daemon
    _address(tmp_path, CHILD_THREAD, CHILD_ADDRESS)
    _address(tmp_path, ROOT_THREAD, ROOT_ADDRESS)
    env = _env(tmp_path, wait=8, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
               PSEUDOLIFE_MCP_TOKEN="fixture-token", CLAUDE_CODE_SESSION_ID=ROOT_THREAD)
    if marker:
        env[marker[0]] = marker[1] or env["CLAUDE_PLUGIN_ROOT"]
    code, out, err = _bash(env, _payload())
    assert (code, out, err, requests) == (0, "", "", [])
    assert not list((tmp_path / "digests").glob("*.wake"))


def test_an_unknown_mode_does_nothing(tmp_path, daemon):
    server, requests = daemon
    _address(tmp_path, ROOT_THREAD, ROOT_ADDRESS)
    env = _codex_env(tmp_path, server)
    process = subprocess.Popen([bash_exe(), str(HOOK), "subagent-start"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    process.stdin.write(_payload())
    process.stdin.close()
    STARTED.append(process)
    assert _finish(process) + (requests,) == (0, "", "", [])


def test_the_text_a_child_is_shown_never_suggests_sending():
    """Child send rights are off (maintainer decision 2026-09-30): the gate
    text a child sees asks it to park and names no send."""
    from pseudolife_memory.coordination import PARK_GATE_MESSAGE
    for text in (PARK_GATE_MESSAGE, GATE_MESSAGE):
        lowered = text.lower()
        assert "send" not in lowered and "memory_message" not in lowered and "reply" not in lowered


# --- setup ------------------------------------------------------------------

@pytest.mark.parametrize("listed", ["all", "without-subagent-stop"])
def test_setup_approves_the_child_gate_and_keeps_it_optional(tmp_path, monkeypatch, listed):
    """Codex 0.158 lists the plugin's SubagentStop entry and setup approves
    it with the rest; a Codex that does not list it still reaches ready."""
    seed_user_files(tmp_path)
    hooks = plugin_hooks(tmp_path)
    assert [h["eventName"] for h in hooks].count("subagentStop") == 1
    if listed != "all":
        hooks = [h for h in hooks if h["eventName"] != "subagentStop"]
    writes = approving_runtime(monkeypatch, tmp_path, hooks)
    result = setup.setup(options())
    assert result["status"] == "ready", result
    [write] = writes
    assert sorted(edit["keyPath"] for edit in write["edits"]) == sorted(
        setup.dotted("hooks", "state", h["key"], "trusted_hash") for h in hooks)


def test_a_disabled_subagent_stop_hook_does_not_block_setup(tmp_path, monkeypatch):
    seed_user_files(tmp_path)
    hooks = plugin_hooks(tmp_path)
    [child] = [h for h in hooks if h["eventName"] == "subagentStop"]
    child["enabled"] = False
    writes = approving_runtime(monkeypatch, tmp_path, hooks)
    result = setup.setup(options())
    assert result["status"] == "ready", result
    assert all(child["key"] not in edit["keyPath"] for edit in writes[0]["edits"])
    assert len(writes[0]["edits"]) == len(hooks) - 1
