"""The startup memory-policy knob (``memory_policy`` in config.yaml).

``evals/memory_policy_bench.py`` compares these variants, so each one must
serve exactly its own policy text and nothing else: the bench's validity
check depends on it. ``full_separate_hook`` moves the full block to its own
hook output (``/api/hook/memory-policy``) so the block and the briefing do
not compete for one output's budget.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pseudolife_memory.utils.config import (
    MEMORY_POLICY_VARIANTS, AppConfig, MemoryPolicyConfig, load_config)
from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from pseudolife_memory.web.session_hook import (
    HOOK_CONTEXT_MAX_CHARS, MEMORY_LOOP_BLOCK, STARTUP_MEMORY_CORE,
    STARTUP_MEMORY_GAPS, ab_arm_index, hook_memory_policy, hook_session_start,
    memory_policy_variant, session_start_context)
from tests.asgi_helpers import call, call_with_headers, stub_mcp

ROOT = Path(__file__).resolve().parents[1]
# Memory tool names any policy text mentions; the fixture briefing names none.
_TOOL = re.compile(r"memory_[a-z_]+")


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    s = FixtureService()
    s.data_dir = tmp_path
    return s


def _set(svc, variant="compact", arms=()):
    svc.config.memory_policy = MemoryPolicyConfig(variant=variant, ab_arms=list(arms))


def _app(svc, token=None):
    return build_console_app(stub_mcp, token, lambda: {"status": "ok"}, svc)


# ── config ──────────────────────────────────────────────────────────────────

def test_default_variant_is_todays_compact_core():
    assert AppConfig().memory_policy.variant == "compact"
    assert AppConfig().memory_policy.ab_arms == []
    assert MEMORY_POLICY_VARIANTS == ("none", "compact", "compact_gaps",
                                      "full_separate_hook")


def test_yaml_selects_variant_and_arms(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("memory_policy:\n  variant: none\n"
                    "  ab_arms: [compact, full_separate_hook]\n", encoding="utf-8")
    cfg = load_config(path).memory_policy
    assert cfg.variant == "none"
    assert cfg.ab_arms == ["compact", "full_separate_hook"]


@pytest.mark.parametrize("kwargs", [
    {"variant": "full"},                      # not a variant name
    {"ab_arms": ["compact", "verbose"]},      # unknown arm
    {"ab_arms": ["compact"]},                 # one arm is not a test
    {"ab_arms": "compact,none"},              # not a list
])
def test_invalid_policy_config_is_refused(kwargs):
    with pytest.raises(ValueError, match="memory_policy"):
        MemoryPolicyConfig(**kwargs)


# ── what each variant serves ───────────────────────────────────────────────

def test_compact_serves_the_core_and_no_separate_block(svc):
    _set(svc, "compact")
    out = session_start_context(svc, True)
    assert out.startswith(STARTUP_MEMORY_CORE)
    assert STARTUP_MEMORY_GAPS not in out
    assert "(fixture)" in out                      # briefing still follows
    assert hook_memory_policy(svc) == ""


def test_none_serves_no_policy_text_but_keeps_the_briefing(svc):
    _set(svc, "none")
    out = session_start_context(svc, True)
    assert "(fixture)" in out
    assert not _TOOL.search(out), out              # no memory-tool instruction at all
    assert hook_memory_policy(svc) == ""


def test_none_drops_the_cold_bank_onboarding_too(svc):
    """The onboarding block is policy text (it tells the agent which memory
    tools to call), so ``none`` must not serve it either."""
    _set(svc, "none")
    svc.stats = lambda: {"total_memories": 0}
    out = session_start_context(svc, True)
    assert "memory bank is EMPTY" not in out
    assert not _TOOL.search(out)


def test_compact_gaps_is_core_plus_three_rules_under_two_thousand_chars(svc):
    _set(svc, "compact_gaps")
    out = session_start_context(svc, True)
    policy = STARTUP_MEMORY_CORE + "\n\n" + STARTUP_MEMORY_GAPS
    assert out.startswith(policy)
    assert len(policy) < 2_000
    gaps = " ".join(STARTUP_MEMORY_GAPS.split())
    assert "version" in gaps and "benchmark" in gaps      # recall before stating
    assert "memory_world_set" in gaps                     # route external facts
    assert "memory_fact_set" in gaps                      # correct drift on the spot
    assert hook_memory_policy(svc) == ""


def test_full_separate_hook_moves_the_full_block_out_of_the_briefing_output(svc):
    _set(svc, "full_separate_hook")
    out = session_start_context(svc, True)
    assert "(fixture)" in out
    assert not _TOOL.search(out)                   # no core beside the briefing
    block = hook_memory_policy(svc)
    assert block == MEMORY_LOOP_BLOCK
    assert len(block.encode("utf-8")) <= HOOK_CONTEXT_MAX_CHARS


def test_briefing_budget_is_the_same_for_none_and_full(svc):
    """The point of the separate hook: the full arm's briefing output is
    byte-identical to the no-policy arm's, so the budget cannot confound
    the comparison."""
    svc.session_briefing = lambda **kw: {"markdown": "## Lessons\n" + "- lesson\n" * 2000}
    _set(svc, "none")
    none_out = session_start_context(svc, True)
    _set(svc, "full_separate_hook")
    assert session_start_context(svc, True) == none_out


# ── online A/B ─────────────────────────────────────────────────────────────

def test_ab_arm_is_a_stable_hash_of_the_session_id():
    assert ab_arm_index("session-abc", 2) == ab_arm_index("session-abc", 2)
    # Independent of PYTHONHASHSEED: sha256, not hash().
    import hashlib
    expected = int.from_bytes(hashlib.sha256(b"session-abc").digest()[:8], "big") % 3
    assert ab_arm_index("session-abc", 3) == expected
    spread = {ab_arm_index(f"s{i}", 2) for i in range(64)}
    assert spread == {0, 1}


def test_ab_arms_override_variant_per_session_and_both_hooks_agree(svc):
    _set(svc, "compact", arms=["none", "full_separate_hook"])
    sid_none = next(f"s{i}" for i in range(100) if ab_arm_index(f"s{i}", 2) == 0)
    sid_full = next(f"s{i}" for i in range(100) if ab_arm_index(f"s{i}", 2) == 1)
    assert memory_policy_variant(svc, sid_none) == "none"
    assert memory_policy_variant(svc, sid_full) == "full_separate_hook"
    assert hook_memory_policy(svc, sid_full) == MEMORY_LOOP_BLOCK
    assert hook_memory_policy(svc, sid_none) == ""
    assert not _TOOL.search(session_start_context(svc, True, session_id=sid_none))
    # No session id: the configured variant.
    assert memory_policy_variant(svc, None) == "compact"
    assert session_start_context(svc, True).startswith(STARTUP_MEMORY_CORE)


def test_registered_session_logs_its_arm(svc, caplog):
    """The arm is a pure function of the session key the bank keeps; the
    daemon log names it too, for audit without a schema change."""
    _set(svc, "compact", arms=["compact", "compact_gaps"])
    svc.episode_start_session = lambda sid, title: {"id": "episode-123456789"}
    svc.set_active_session = lambda sid: None
    with caplog.at_level("INFO", logger="pseudolife-mcp.web"):
        hook_session_start(svc, "session-xyz")
    arm = memory_policy_variant(svc, "session-xyz")
    assert f"memory-policy variant {arm} for session session-xyz" in caplog.text


def test_unregistered_session_logs_no_arm(svc, caplog):
    """No session id (or a failed registration): nothing to attribute."""
    _set(svc, "compact", arms=["compact", "compact_gaps"])

    def boom(*a):
        raise RuntimeError("db down")
    svc.episode_start_session = boom
    with caplog.at_level("INFO", logger="pseudolife-mcp.web"):
        out = hook_session_start(svc, "session-xyz")
    assert "memory-policy variant" not in caplog.text
    assert "Session episode:" not in out


# ── endpoint ───────────────────────────────────────────────────────────────

def test_memory_policy_endpoint_serves_the_block_only_for_the_full_variant(svc):
    _set(svc, "full_separate_hook")
    st, headers, body = call_with_headers(_app(svc), "GET", "/api/hook/memory-policy")
    assert st == 200 and headers[b"content-type"].startswith(b"text/plain")
    assert body.decode("utf-8") == MEMORY_LOOP_BLOCK
    _set(svc, "compact")
    st, body = call(_app(svc), "GET", "/api/hook/memory-policy")
    assert st == 200 and body == b""


def test_memory_policy_endpoint_uses_session_arm_only_when_authorized(svc):
    _set(svc, "compact", arms=["none", "full_separate_hook"])
    sid = next(f"s{i}" for i in range(100) if ab_arm_index(f"s{i}", 2) == 1)
    app = _app(svc, token="secret")
    st, body = call(app, "GET", "/api/hook/memory-policy", query=f"session_id={sid}",
                    headers=[(b"authorization", b"Bearer secret")])
    assert st == 200 and body.decode("utf-8") == MEMORY_LOOP_BLOCK
    # Unauthorized: session_id is dropped, as on session-start.
    st, body = call(app, "GET", "/api/hook/memory-policy", query=f"session_id={sid}")
    assert st == 200 and body == b""


def test_memory_policy_endpoint_rejects_post(svc):
    st, _ = call(_app(svc), "POST", "/api/hook/memory-policy")
    assert st == 405


# ── plugin wiring ──────────────────────────────────────────────────────────

def test_plugin_runs_the_memory_policy_hook_as_its_own_session_start_output():
    hooks = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    handlers = [h for group in hooks["SessionStart"] for h in group["hooks"]]
    policy = [h for h in handlers if h["command"].endswith("session-start.sh\" memory-policy")]
    assert len(policy) == 1
    assert "-Event MemoryPolicy" in policy[0]["commandWindows"]
    script = (ROOT / "plugin/hooks/session-start.sh").read_text(encoding="utf-8")
    assert "/api/hook/memory-policy" in script
    ps = (ROOT / "plugin/hooks/lifecycle.ps1").read_text(encoding="utf-8")
    assert "'MemoryPolicy'" in ps and "/api/hook/memory-policy" in ps


# ── the hook scripts themselves ────────────────────────────────────────────

def _policy_daemon(body: bytes):
    """A fixture daemon recording each GET path; answers ``body``."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker, paths


def _run_policy_hook(hook, port, tmp_path):
    import subprocess
    from tests.test_codex_hooks import HOOK_PROCESS_TIMEOUT, bash_exe, isolated_env, pwsh_run
    env = isolated_env(tmp_path / "codex-home")
    env.update({"PSEUDOLIFE_MCP_DAEMON_URL": f"http://127.0.0.1:{port}",
                "PSEUDOLIFE_MCP_TOKEN": "fixture-token"})
    stdin = '{"session_id":"fixture","source":"startup"}'
    if hook == "bash":
        return subprocess.run(
            [bash_exe(), str(ROOT / "plugin/hooks/session-start.sh"), "memory-policy"],
            input=stdin, env=env, capture_output=True, text=True,
            timeout=HOOK_PROCESS_TIMEOUT, check=True).stdout
    return pwsh_run("-Command",
                    f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event MemoryPolicy",
                    input=stdin, env=env).stdout


@pytest.mark.parametrize("hook", ["bash", "native"])
def test_memory_policy_hook_prints_the_daemon_body_as_its_own_output(tmp_path, hook):
    server, worker, paths = _policy_daemon(b"FULL POLICY BLOCK")
    try:
        out = _run_policy_hook(hook, server.server_port, tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert paths == ["/api/hook/memory-policy?session_id=fixture"]
    if hook == "bash":
        assert out == "FULL POLICY BLOCK"
    else:
        context = json.loads(out)["hookSpecificOutput"]
        assert context == {"hookEventName": "SessionStart",
                           "additionalContext": "FULL POLICY BLOCK"}


@pytest.mark.parametrize("hook", ["bash", "native"])
@pytest.mark.parametrize("daemon", ["empty-body", "down"])
def test_memory_policy_hook_adds_nothing_for_other_variants_or_a_down_daemon(
        tmp_path, hook, daemon):
    """Every other variant answers an empty body; a down daemon is the main
    hook's to report. Either way this output must stay empty."""
    server, worker, paths = _policy_daemon(b"")
    port = server.server_port
    if daemon == "down":
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    try:
        out = _run_policy_hook(hook, port, tmp_path)
    finally:
        if daemon != "down":
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
    assert out == ""
