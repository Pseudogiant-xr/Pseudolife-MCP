"""Guards for the ``pl_taubench`` tau2-bench plugin (evals/taubench_pseudolife).

The plugin lives in its own distribution and runs under a separate Python 3.12
venv (``.venv-taubench``) where tau2 is installed. These tests run under the REPO
venv (3.11, no tau2), so they import the plugin by path and skip the two modules
that genuinely need tau2 (``agent``, ``memory_env``).
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "evals" / "taubench_pseudolife"
PLUGIN_SRC = PLUGIN_ROOT / "src"
PACKAGE_DIR = PLUGIN_SRC / "pl_taubench"
PATCH_PATH = PLUGIN_ROOT / "patches" / "tau2_pseudolife.patch"
TAU2_ROOT = REPO_ROOT / "reference" / "tau2-bench"

if str(PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SRC))


def _has_tau2() -> bool:
    try:
        importlib.import_module("tau2")
    except Exception:  # noqa: BLE001
        return False
    return True


needs_tau2 = pytest.mark.skipif(not _has_tau2(), reason="tau2 not installed in this venv")


# --------------------------------------------------------------------------- #
# packaging / import hygiene
# --------------------------------------------------------------------------- #


def _package_modules() -> list[Path]:
    return sorted(PACKAGE_DIR.glob("*.py"))


def test_package_has_the_expected_modules():
    names = {p.stem for p in _package_modules()}
    assert {
        "context",
        "client",
        "session",
        "telemetry",
        "prompts",
        "reflection",
        "reflection_diff",
        "dream",
        "memory_env",
        "agent",
    } <= names


def test_package_never_imports_pseudolife_memory_or_torch():
    """The plugin talks to the daemon over MCP; it must never import the library."""
    forbidden = ("pseudolife_memory", "torch")
    offenders: list[str] = []
    for path in _package_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                head = mod.split(".")[0]
                if head in forbidden:
                    offenders.append(f"{path.name}:{node.lineno} imports {mod}")
    assert offenders == [], offenders


def test_pyproject_declares_no_tau2_dependency():
    """tau2 is imported from the venv at runtime, never resolved by the installer."""
    text = (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "pseudolife-taubench"' in text
    assert "pl_taubench" in text
    assert 'requires-python = ">=3.12,<3.14"' in text

    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, flags=re.M | re.S)
    assert block is not None, "no dependencies list"
    deps = re.findall(r'"([^"]+)"', block.group(1))
    assert any(d.startswith("mcp") for d in deps), deps
    assert any(d.startswith("httpx") for d in deps), deps
    assert not any(d.lower().startswith("tau2") for d in deps), deps


def _agent_helper(name: str):
    """Compile one top-level helper out of ``agent.py`` without importing it.

    ``agent.py`` imports tau2, which the repo venv does not have — but its env
    parsers are plain functions over ``os.environ``, and they decide whether a
    frozen store stays frozen. Extracting them keeps that contract testable in
    the venv the suite actually runs in, rather than only in the tau2 venv.
    """
    tree = ast.parse((PACKAGE_DIR / "agent.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            namespace: dict = {"os": os}
            exec(  # noqa: S102 - our own source, one function, no imports
                compile(ast.Module(body=[node], type_ignores=[]),
                        str(PACKAGE_DIR / "agent.py"), "exec"),
                namespace,
            )
            return namespace[name]
    raise AssertionError(f"{name} is not a top-level function of agent.py")


def test_pl_read_only_is_parsed_as_a_boolean_flag(monkeypatch):
    """The other half of the adapter's contract: ``taubench_adapter`` emits
    ``PL_READ_ONLY=1`` and the plugin must read that as True.

    It is a boolean parser, so anything that is not a recognised true word —
    a bank name, a DSN, the string the adapter used to pass — reads as FALSE
    and the transfer arm writes into the store it was told to freeze.
    ``tests/test_taubench_adapter.py`` pins the emitted value.
    """
    flag = _agent_helper("_flag")
    monkeypatch.setenv("PL_READ_ONLY", "1")          # what the adapter emits
    assert flag("PL_READ_ONLY") is True
    for truthy in ("true", "YES", "on"):
        monkeypatch.setenv("PL_READ_ONLY", truthy)
        assert flag("PL_READ_ONLY") is True
    for falsy in ("0", "", "pl_bench_ro", "postgresql://host/bench"):
        monkeypatch.setenv("PL_READ_ONLY", falsy)
        assert flag("PL_READ_ONLY") is False
    monkeypatch.delenv("PL_READ_ONLY")
    assert flag("PL_READ_ONLY") is False


def test_vendored_reflection_diff_carries_attribution():
    head = (PACKAGE_DIR / "reflection_diff.py").read_text(encoding="utf-8")[:2000]
    assert "memcoai/spark-continual-learning-paper-data" in head
    assert "MIT" in head


# --------------------------------------------------------------------------- #
# session / episode
# --------------------------------------------------------------------------- #


def test_episode_window_ok_boundary():
    from pl_taubench.session import Episode

    ep = Episode(session_uid="a" * 32, task_id="t1", trial=0, run="r", window_seconds=100.0)
    # No search yet -> nothing to credit, window trivially open.
    assert ep.window_ok(now=1_000.0) is True

    ep.note_search([1, 2], ts=1_000.0)
    assert ep.window_ok(now=1_000.0) is True
    assert ep.window_ok(now=1_099.999) is True
    # Strictly-less-than: at exactly the window the credit is refused.
    assert ep.window_ok(now=1_100.0) is False
    assert ep.window_ok(now=1_100.001) is False


def test_episode_first_search_ts_is_stamped_once():
    from pl_taubench.session import Episode

    ep = Episode(session_uid="b" * 32, task_id="t", trial=0, run="r")
    ep.note_search([1], ts=10.0)
    ep.note_search([2], ts=99.0)
    assert ep.first_search_ts == 10.0


def test_served_ids_dedup_preserves_first_seen_order():
    from pl_taubench.session import Episode

    ep = Episode(session_uid="c" * 32, task_id="t", trial=0, run="r")
    ep.note_search([5, 3, 5, 9], ts=1.0)
    ep.note_search([9, 1, 3], ts=2.0)
    assert ep.served_ids == [5, 3, 9, 1]


def test_extract_served_ids_from_search_payloads():
    from pl_taubench.session import extract_served_ids

    payload = {
        "query": "q",
        "count": 2,
        "entries": [{"id": 7, "text": "x"}, {"id": 11, "text": "y"}, {"text": "no id"}],
    }
    assert extract_served_ids(payload) == [7, 11]
    # memory_lesson_search returns no ids at all.
    assert extract_served_ids({"count": 1, "entries": [{"task": "t", "lesson": "l"}]}) == []
    assert extract_served_ids({"error": "boom"}) == []


def test_set_context_applies_the_trial_offset():
    """``--distill trial`` runs tau2 once per trial with ``--num-trials 1``,
    so the harness hands every simulation trial 0. The orchestrator passes the
    real column in ``PL_TRIAL_OFFSET`` and the context adds it — without that
    every record in the run is stamped trial 0."""
    from pl_taubench.context import clear_context, get_context, set_context

    try:
        os.environ["PL_TRIAL_OFFSET"] = "3"
        set_context(task_id="t1", trial=0)
        assert get_context()["trial"] == 3
        set_context(task_id="t1", trial=1)
        assert get_context()["trial"] == 4

        os.environ["PL_TRIAL_OFFSET"] = "not a number"
        set_context(task_id="t1", trial=2)
        assert get_context()["trial"] == 2, "a junk offset is ignored, not fatal"

        os.environ.pop("PL_TRIAL_OFFSET")
        set_context(task_id="t1", trial=2)
        assert get_context()["trial"] == 2
    finally:
        os.environ.pop("PL_TRIAL_OFFSET", None)
        clear_context()


def test_episode_binding_is_thread_local():
    from pl_taubench.session import Episode, bind_episode, clear_episode, current_episode

    clear_episode()
    assert current_episode() is None
    ep = Episode(session_uid="d" * 32, task_id="t", trial=0, run="r")
    bind_episode(ep)
    assert current_episode() is ep
    clear_episode()
    assert current_episode() is None


# --------------------------------------------------------------------------- #
# prompts — the prompt-lift guard
# --------------------------------------------------------------------------- #

FORBIDDEN_WORDS = ("bank", "banking", "card", "customer", "kb_search", "tau", "τ")


def _forbidden_hits(text: str) -> list[str]:
    low = (text or "").lower()
    return [w for w in FORBIDDEN_WORDS if w.lower() in low]


def test_prompt_constants_carry_no_benchmark_words():
    from pl_taubench import prompts

    offenders = []
    for name in dir(prompts):
        if name.startswith("_"):
            continue
        value = getattr(prompts, name)
        if isinstance(value, str):
            hits = _forbidden_hits(value)
            if hits:
                offenders.append((name, hits))
    assert offenders == [], offenders


def test_prompts_module_source_carries_no_benchmark_words():
    """Comments and docstrings in the prompt module too — nothing lifted."""
    text = (PACKAGE_DIR / "prompts.py").read_text(encoding="utf-8")
    assert _forbidden_hits(text) == []


@pytest.mark.parametrize("route", ["entries", "lessons"])
@pytest.mark.parametrize("supervision", ["experience", "instruction"])
@pytest.mark.parametrize("success", [True, False])
def test_built_reflection_prompt_carries_no_benchmark_words(route, supervision, success):
    from pl_taubench.reflection import build_reflection_prompt

    prompt = build_reflection_prompt(
        route=route,
        supervision=supervision,
        verdict={"success": success, "reward": 1.0 if success else 0.0, "reason": "r"},
        transcript_text="",
        diff_text="",
        gold_text="",
    )
    assert _forbidden_hits(prompt) == []
    assert "must_include" in prompt
    assert "WHEN" in prompt


def test_instruction_failure_prompt_reveals_the_verified_sequence():
    from pl_taubench.reflection import build_reflection_prompt

    prompt = build_reflection_prompt(
        route="entries",
        supervision="instruction",
        verdict={"success": False, "reward": 0.0, "reason": "r"},
        transcript_text="",
        diff_text="DIFF-BLOCK-MARKER",
        gold_text="GOLD-BLOCK-MARKER",
    )
    assert "DIFF-BLOCK-MARKER" in prompt
    assert "GOLD-BLOCK-MARKER" in prompt


def test_experience_failure_prompt_forbids_inventing_the_answer_and_hides_gold():
    from pl_taubench.reflection import build_reflection_prompt

    prompt = build_reflection_prompt(
        route="entries",
        supervision="experience",
        verdict={"success": False, "reward": 0.0, "reason": "r"},
        transcript_text="",
        diff_text="DIFF-BLOCK-MARKER",
        gold_text="GOLD-BLOCK-MARKER",
    )
    assert "GOLD-BLOCK-MARKER" not in prompt
    assert "DIFF-BLOCK-MARKER" not in prompt
    assert "do NOT" in prompt


def test_the_diff_labels_live_in_the_prompt_module():
    """``format_action_diff`` composes text that goes into the reflection
    prompt, so its labels are prompt surface and belong in ``prompts.py`` —
    that is the module the lift guard (``tests/test_prompt_example_lifts.py``)
    and the vocabulary guard above scan. A label written inline in
    ``reflection.py`` is prompt text neither of them can see."""
    from pl_taubench import prompts
    from pl_taubench.reflection import format_action_diff

    for name in ("DIFF_MISSING", "DIFF_WRONG_VALUE", "DIFF_EXTRA",
                 "DIFF_NOTHING_TO_FIX"):
        assert isinstance(getattr(prompts, name, None), str), name

    source = (PACKAGE_DIR / "reflection.py").read_text(encoding="utf-8")
    for literal in ("NEVER DONE", "WRONG VALUE", "NOT PART OF THE SOLUTION",
                    "Every needed step"):
        assert literal not in source, (
            f"{literal!r} is prompt text written inline in reflection.py")

    rendered = format_action_diff({
        "missing": [{"action": {"name": "do_thing", "arguments": {"a": 1}},
                     "required": 2, "executed": 1}],
        "wrong_args": [{"action": {"name": "do_thing"},
                        "mismatches": [("args.a", "right", "wrong")]}],
        "extra": [{"action": {"name": "other", "arguments": {}}, "count": 2}],
    })
    assert prompts.DIFF_MISSING.split("{")[0] in rendered
    assert prompts.DIFF_EXTRA.split("{")[0] in rendered
    assert "'wrong'" in rendered and "'right'" in rendered
    assert "[needed 2x, done 1x]" in rendered and "[2x]" in rendered
    assert format_action_diff({}) == prompts.DIFF_NOTHING_TO_FIX


# --------------------------------------------------------------------------- #
# reflection: verdict, diff, JSON parsing, detail block
# --------------------------------------------------------------------------- #


class _Reward:
    def __init__(self, reward):
        self.reward = reward


class _Sim:
    def __init__(self, reward, messages=None):
        self.reward_info = _Reward(reward) if reward is not None else None
        self.messages = messages or []
        self.termination_reason = "user_stop"


def test_verdict_from_reward():
    from pl_taubench.reflection import verdict_from

    assert verdict_from(_Sim(1.0))["success"] is True
    assert verdict_from(_Sim(0.0))["success"] is False
    v = verdict_from(_Sim(None))
    assert v["success"] is False and v["reward"] is None


def test_executed_actions_excludes_memory_and_gold_actions_read():
    from pl_taubench.reflection import executed_actions, gold_actions

    messages = [
        {"role": "assistant", "tool_calls": [{"name": "memory_search", "arguments": {}}]},
        {"role": "assistant", "tool_calls": [{"name": "do_thing", "arguments": {"a": 1}}]},
    ]
    names = [a["name"] for a in executed_actions(messages)]
    assert names == ["do_thing"]

    task = {"evaluation_criteria": {"actions": [{"name": "do_thing", "arguments": {"a": 2}}]}}
    assert [a["name"] for a in gold_actions(task)] == ["do_thing"]


def test_reflection_json_parser_handles_fences_and_bare_text():
    from pl_taubench.reflection import parse_reflection_json

    assert parse_reflection_json('```json\n{"rules": []}\n```') == {"rules": []}
    assert parse_reflection_json('noise {"rules": [1]} noise') == {"rules": [1]}
    assert parse_reflection_json("not json at all") is None
    assert parse_reflection_json("") is None


def test_run_reflection_retries_json_once_then_falls_back_to_text():
    from pl_taubench.reflection import run_reflection
    from pl_taubench.session import Episode

    calls: list[str] = []

    def bad_llm(prompt: str) -> str:
        calls.append(prompt)
        return "I cannot produce JSON."

    client = _FakeClient()
    ep = Episode(session_uid="e" * 32, task_id="T1", trial=0, run="run-x")
    out = run_reflection(
        client,
        ep,
        _Sim(0.0),
        task=None,
        route="entries",
        supervision="experience",
        llm_call=bad_llm,
        run_tag="run-x",
    )
    assert len(calls) == 2, "one parse retry, then the text fallback"
    assert out["parsed"] is False
    # The fallback still banks exactly one rule and one signal.
    assert [c[0] for c in client.calls] == ["memory_store", "memory_outcome"]


def test_run_reflection_second_attempt_can_succeed():
    from pl_taubench.reflection import run_reflection
    from pl_taubench.session import Episode

    replies = ["garbage", json.dumps({"rules": [_rule()]})]

    def flaky_llm(prompt: str) -> str:
        return replies.pop(0)

    client = _FakeClient()
    ep = Episode(session_uid="f" * 32, task_id="T1", trial=0, run="r")
    out = run_reflection(
        client, ep, _Sim(1.0), route="entries", supervision="experience",
        llm_call=flaky_llm, run_tag="r",
    )
    assert out["parsed"] is True
    assert replies == []


def test_detail_block_format():
    from pl_taubench.reflection import build_detail

    detail = build_detail(
        situation="the request repeats",
        verdict_text="the desired outcome was NOT achieved",
        actions_text="agent: do_thing(a=1)",
        gold_text="1. do_thing(a=2)",
        diff_text="- MISSING: do_thing(a=2)",
        must_include=["a", "b"],
    )
    lines = detail.split("\n")
    assert lines[0] == "SITUATION: the request repeats"
    assert lines[1] == "VERDICT: the desired outcome was NOT achieved"
    assert "ACTIONS TAKEN: agent: do_thing(a=1)" in detail
    assert "CORRECT SOLUTION: 1. do_thing(a=2)" in detail
    assert "ACTION DIFF: - MISSING: do_thing(a=2)" in detail
    assert detail.rstrip().endswith("MUST INCLUDE: a; b")


def test_detail_block_omits_empty_sections():
    from pl_taubench.reflection import build_detail

    detail = build_detail(
        situation="s", verdict_text="v", actions_text="", gold_text="",
        diff_text="", must_include=[],
    )
    assert detail == "SITUATION: s\nVERDICT: v"


# --------------------------------------------------------------------------- #
# reflection: route decisions via a fake client
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self, results=None):
        self.calls: list[tuple[str, dict]] = []
        self._results = results or {}

    def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        return self._results.get(name, {"ok": True})


def _rule(**over):
    rule = {
        "situation": "the request repeats",
        "rule": "WHEN the request repeats THEN take the recorded action",
        "polarity": "+",
        "about": "repeat requests",
        "must_include": ["do_thing"],
    }
    rule.update(over)
    return rule


def _reflect(client, *, route, supervision, read_only=False, success=False, rules=None,
             episode=None, run_tag="run-1", now=None):
    from pl_taubench.reflection import run_reflection
    from pl_taubench.session import Episode

    ep = episode or Episode(session_uid="0" * 32, task_id="T7", trial=2, run=run_tag)
    payload = json.dumps({"rules": rules if rules is not None else [_rule()]})
    return run_reflection(
        client,
        ep,
        _Sim(1.0 if success else 0.0),
        task=None,
        route=route,
        supervision=supervision,
        read_only=read_only,
        llm_call=lambda prompt: payload,
        run_tag=run_tag,
        now=now,
    )


def test_entries_route_stores_each_rule_then_one_outcome():
    client = _FakeClient()
    _reflect(client, route="entries", supervision="experience",
             rules=[_rule(), _rule(situation="another", about="another")])
    names = [c[0] for c in client.calls]
    assert names == ["memory_store", "memory_store", "memory_outcome"]

    _, store_args = client.calls[0]
    assert store_args["source"] == "taubench"
    assert store_args["distortion_tolerance"] == "constraint"
    assert store_args["episode"] == "0" * 32
    assert store_args["text"].startswith("RULE: ")
    assert set(store_args["tags"]) >= {"taubench", "rule", "run:run-1", "task:T7"}

    _, outcome_args = client.calls[-1]
    assert outcome_args["episode"] == "0" * 32
    assert outcome_args["about"] == "repeat requests"
    assert outcome_args["task"] == "the request repeats"
    assert outcome_args["detail"].startswith("SITUATION: ")


def test_lessons_route_never_stores_and_prefixes_about_with_rule():
    client = _FakeClient()
    _reflect(client, route="lessons", supervision="experience")
    names = [c[0] for c in client.calls]
    assert names == ["memory_outcome"]
    assert client.calls[0][1]["about"] == "rule: repeat requests"


def test_outcome_label_success_failure_and_correction():
    c1 = _FakeClient()
    _reflect(c1, route="lessons", supervision="experience", success=True)
    assert c1.calls[-1][1]["outcome"] == "success"

    c2 = _FakeClient()
    _reflect(c2, route="lessons", supervision="experience", success=False)
    assert c2.calls[-1][1]["outcome"] == "failure"

    c3 = _FakeClient()
    _reflect(c3, route="lessons", supervision="instruction", success=False)
    assert c3.calls[-1][1]["outcome"] == "correction"

    c4 = _FakeClient()
    _reflect(c4, route="lessons", supervision="instruction", success=True)
    assert c4.calls[-1][1]["outcome"] == "success"


def test_read_only_mode_makes_zero_write_calls():
    """The reflection half of the frozen-store contract. The other two write
    paths are covered beside it: ``test_read_only_client_opens_no_episode_rows``
    (the episode lifecycle) and ``test_read_only_skips_the_dream``
    (consolidation)."""
    client = _FakeClient()
    for route in ("entries", "lessons"):
        out = _reflect(client, route=route, supervision="instruction", read_only=True)
        assert out["read_only"] is True
    assert client.calls == []


def test_read_only_client_opens_no_episode_rows():
    """A frozen store gets no episode rows either. ``episode_start`` /
    ``episode_end`` are REST writes on the daemon, and the agent calls them
    around every simulation — reflection being skipped does not stop them,
    so the guard belongs at the client, which is the one write boundary both
    call sites cross."""
    from pl_taubench.client import PLMemoryClient

    posts: list[str] = []

    class _Recording(PLMemoryClient):
        def _post(self, path, payload):
            posts.append(path)
            return True

    frozen = _Recording(url="http://127.0.0.1:1", session_uid="a" * 32,
                        read_only=True)
    assert frozen.episode_start("title") is False
    assert frozen.episode_end() is False
    assert posts == [], "a read-only client must reach the daemon for neither"

    writing = _Recording(url="http://127.0.0.1:1", session_uid="b" * 32)
    assert writing.episode_start("title") is True
    assert writing.episode_end() is True
    assert posts == ["/api/episode/start", "/api/episode/end"]


def test_agent_hands_read_only_through_to_the_client():
    """The wiring the repo venv cannot execute (agent.py imports tau2), pinned
    at the source: without this keyword the client guard above is unreachable
    in a real run."""
    tree = ast.parse((PACKAGE_DIR / "agent.py").read_text(encoding="utf-8"))
    constructions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PLMemoryClient"
    ]
    assert constructions, "agent.py no longer builds a PLMemoryClient"
    for call in constructions:
        assert "read_only" in [kw.arg for kw in call.keywords], (
            "agent.py builds its client without read_only=, so a frozen "
            "store would still get episode rows")


def test_dream_warns_when_the_daemon_returns_an_error(caplog):
    """``client.call_tool`` swallows failures into ``{"error": ...}``, so the
    harness hook's own try/except never fires and a failed consolidation is
    invisible. The trigger logs it and hands the error back to its caller."""
    import logging

    import pl_taubench.dream as dream_mod

    class _Failing:
        def __init__(self, **kwargs):
            pass

        def call_tool(self, name, args):
            return {"error": "daemon said no"}

    original = dream_mod.PLMemoryClient
    dream_mod.PLMemoryClient = _Failing
    try:
        with caplog.at_level(logging.WARNING, logger="pl_taubench.dream"):
            out = dream_mod.dream_run("http://127.0.0.1:1")
    finally:
        dream_mod.PLMemoryClient = original
    assert out == {"error": "daemon said no"}
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert warnings and "daemon said no" in warnings[0], warnings


def test_read_only_skips_the_dream():
    """Consolidation is a write. The batch hook fires ``dream_run`` from
    ``PL_DREAM`` alone, which knows nothing about the frozen arm — so the
    trigger checks the same flag rather than trusting its caller."""
    import pl_taubench.dream as dream_mod

    built: list[dict] = []

    class _Recorder:
        def __init__(self, **kwargs):
            built.append(kwargs)

        def call_tool(self, name, args):
            built.append({"tool": name})
            return {"ok": True}

    original = dream_mod.PLMemoryClient
    dream_mod.PLMemoryClient = _Recorder
    try:
        os.environ["PL_READ_ONLY"] = "1"
        out = dream_mod.dream_run("http://127.0.0.1:1")
        assert built == [], "a frozen store must not be consolidated"
        assert out.get("skipped") == "read_only"
        os.environ.pop("PL_READ_ONLY")
        dream_mod.dream_run("http://127.0.0.1:1")
        assert [b.get("tool") for b in built if "tool" in b] == ["memory_dream"]
    finally:
        dream_mod.PLMemoryClient = original
        os.environ.pop("PL_READ_ONLY", None)


def test_used_ids_are_capped_and_ordered():
    from pl_taubench.session import Episode

    ep = Episode(session_uid="9" * 32, task_id="T", trial=0, run="r")
    ep.note_search(list(range(100)), ts=time.time())
    client = _FakeClient()
    _reflect(client, route="lessons", supervision="experience", episode=ep)
    used = client.calls[-1][1]["used_ids"]
    assert used == list(range(50))


def test_window_violation_refuses_the_label_but_still_sends_the_signal():
    from pl_taubench.session import Episode

    ep = Episode(session_uid="8" * 32, task_id="T", trial=0, run="r", window_seconds=10.0)
    ep.note_search([1, 2], ts=1_000.0)
    client = _FakeClient()
    out = _reflect(client, route="lessons", supervision="experience",
                   episode=ep, now=1_500.0)
    assert out["window_ok"] is False
    assert [c[0] for c in client.calls] == ["memory_outcome"]


# --------------------------------------------------------------------------- #
# telemetry
# --------------------------------------------------------------------------- #


def test_telemetry_records_carry_context_and_every_used_ids_key(tmp_path, monkeypatch):
    from pl_taubench import telemetry
    from pl_taubench.context import clear_context, set_context
    from pl_taubench.session import Episode

    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("PL_TAUBENCH_LOG", str(log))
    telemetry.reset_log_path()
    set_context(task_id="T3", trial=1, seed=42, agent="pl_memory", run="run-9")
    try:
        telemetry.record_call(
            tool="memory_search", args_digest={"query": "q"}, ok=True,
            latency_ms=12.3, result_len=400, served_ids=[4, 5],
        )
        ep = Episode(session_uid="7" * 32, task_id="T3", trial=1, run="run-9")
        ep.note_search([4, 5], ts=time.time())
        telemetry.record_episode(
            ep,
            verdict={"success": True, "reward": 1.0, "reason": "ok"},
            window_ok=True,
            outcome_result={
                "recorded": True,
                "signal_id": 5,
                "used_ids_recorded": [4],
                "used_ids_unmatched": [5],
                "used_ids_served_elsewhere": [],
                "used_ids_errors": [],
                "used_ids_reason": "ok",
            },
        )
    finally:
        clear_context()
        telemetry.reset_log_path()

    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    for rec in records:
        assert rec["task_id"] == "T3"
        assert rec["trial"] == 1
        assert rec["seed"] == 42
        assert rec["agent"] == "pl_memory"
        assert rec["run"] == "run-9"
        assert "ts" in rec

    call, episode = records
    assert call["kind"] == "call"
    assert call["tool"] == "memory_search"
    assert call["ok"] is True
    assert call["served_ids"] == [4, 5]

    assert episode["kind"] == "episode"
    assert episode["session_uid"] == "7" * 32
    assert episode["window_ok"] is True
    for key in (
        "used_ids_recorded",
        "used_ids_unmatched",
        "used_ids_served_elsewhere",
        "used_ids_errors",
        "used_ids_reason",
    ):
        assert key in episode, key


def test_telemetry_never_raises(tmp_path, monkeypatch):
    from pl_taubench import telemetry

    monkeypatch.setenv("PL_TAUBENCH_LOG", str(tmp_path / "nope" / "x.jsonl"))
    telemetry.reset_log_path()
    try:
        telemetry.record_call(tool="t", args_digest={"x": object()}, ok=True,
                              latency_ms=1.0, result_len=0)
    finally:
        telemetry.reset_log_path()


# --------------------------------------------------------------------------- #
# the tau2 patch
# --------------------------------------------------------------------------- #

_PATCHED_FILES = {
    "src/tau2/registry.py",
    "src/tau2/runner/build.py",
    "src/tau2/runner/simulation.py",
    "src/tau2/runner/batch.py",
}


def _patch_text() -> str:
    return PATCH_PATH.read_text(encoding="utf-8")


def test_patch_is_a_unified_diff_touching_exactly_four_files():
    text = _patch_text()
    minus = re.findall(r"^--- a/(\S+)", text, flags=re.M)
    plus = re.findall(r"^\+\+\+ b/(\S+)", text, flags=re.M)
    assert set(minus) == _PATCHED_FILES
    assert set(plus) == _PATCHED_FILES
    assert len(minus) == 4 and len(plus) == 4
    assert text.count("@@") >= 8  # at least one hunk header per file


def test_patch_registers_pl_memory_and_wires_the_hooks():
    text = _patch_text()
    assert "pl_memory" in text
    assert "pl_taubench.agent" in text
    assert "pl_taubench.memory_env" in text
    assert "attach_memory_tools" in text
    assert "on_simulation_end" in text
    assert "PL_DREAM" in text
    assert "set_context" in text


def _find_patch_exe() -> str | None:
    """GNU patch, from PATH or from the Git-for-Windows toolchain beside git."""
    found = shutil.which("patch")
    if found:
        return found
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parents[1] / "usr" / "bin" / "patch.exe"
        if candidate.exists():
            return str(candidate)
    return None


@pytest.mark.skipif(not TAU2_ROOT.exists(), reason="reference/tau2-bench not present")
def test_patch_targets_exist_and_it_applies_to_a_temp_copy(tmp_path):
    for rel in sorted(_PATCHED_FILES):
        assert (TAU2_ROOT / rel).exists(), rel

    work = tmp_path / "tau2"
    for rel in sorted(_PATCHED_FILES):
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TAU2_ROOT / rel, dst)

    # The reference checkout is pristine on a fresh clone and PATCHED once
    # the operator has run apply_patch (as it is during a smoke). Either
    # state proves the patch matches the tree: it applies forward, or it
    # reverses cleanly. What must never pass is a patch that fits neither.
    patch_exe = _find_patch_exe()
    if patch_exe is not None:
        forward = [patch_exe, "-p1", "--dry-run", "-f", "-i", str(PATCH_PATH)]
        reverse = [patch_exe, "-p1", "--dry-run", "-f", "-R", "-i", str(PATCH_PATH)]
    else:
        git = shutil.which("git")
        if git is None:
            pytest.skip("neither GNU patch nor git available")
        forward = [git, "apply", "--check", "-p1", str(PATCH_PATH)]
        reverse = [git, "apply", "--check", "-R", "-p1", str(PATCH_PATH)]
    outcomes = {}
    for label, cmd in (("forward", forward), ("reverse", reverse)):
        proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
        outcomes[label] = (proc.returncode, " ".join(cmd) + "\n"
                           + proc.stdout + proc.stderr)
    assert any(rc == 0 for rc, _ in outcomes.values()), (
        "the patch neither applies to nor reverses from the checkout:\n"
        + "\n".join(out for _, out in outcomes.values()))


def test_apply_scripts_exist_for_both_shells():
    assert (PLUGIN_ROOT / "patches" / "apply_patch.ps1").exists()
    assert (PLUGIN_ROOT / "patches" / "apply_patch.sh").exists()
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "patch -p1" in readme
    assert "git apply" in readme
    assert "pip install -e evals/taubench_pseudolife" in readme


# --------------------------------------------------------------------------- #
# tau2-dependent modules (skipped under the repo venv)
# --------------------------------------------------------------------------- #


@needs_tau2
def test_memory_env_and_agent_import_and_expose_only_read_tools():
    from pl_taubench.memory_env import MEMORY_TOOL_NAMES, WRITE_TOOL_NAMES, build_memory_tools

    assert MEMORY_TOOL_NAMES == ("memory_search", "memory_lesson_search")
    assert "memory_store" in WRITE_TOOL_NAMES and "memory_outcome" in WRITE_TOOL_NAMES
    schemas = [t.openai_schema for t in build_memory_tools()]
    assert {s["function"]["name"] for s in schemas} == set(MEMORY_TOOL_NAMES)
    for schema in schemas:
        assert schema["function"]["description"]
        assert "query" in schema["function"]["parameters"]["properties"]


@needs_tau2
def test_agent_module_exposes_the_factory():
    from pl_taubench.agent import PLMemoryAgent, create_pl_agent

    assert callable(create_pl_agent)
    assert hasattr(PLMemoryAgent, "on_simulation_end")


def test_inject_mode_folds_recalled_memory_into_the_first_turn():
    """Retrieval mode ``inject`` (the paper's second mode): the harness runs
    the two memory searches itself on the first customer turn and folds the
    results into the agent's copy of that turn, so retrieval no longer
    depends on the model choosing to call the tool — the first two smokes
    (2026-09-08) saw one search in seven episodes under ``nudge``. The block
    is built by a pure function in prompts.py; the searches run through the
    same dispatch as the tools, so served ids and the window clock are
    recorded for ``used_ids`` exactly as for an agent-driven search."""
    prompts_text = (PACKAGE_DIR / "prompts.py").read_text(encoding="utf-8")
    ns: dict = {}
    exec(compile(prompts_text, "prompts.py", "exec"), ns)  # noqa: S102
    block = ns["build_inject_block"]("- [taubench] WHEN x THEN y", "- (+) do z")
    assert "WHEN x THEN y" in block and "do z" in block
    assert "memory_search" in block          # the model may search again
    empty = ns["build_inject_block"]("", "")
    assert "nothing" in empty.lower() or "no " in empty.lower()
    agent_src = (PACKAGE_DIR / "agent.py").read_text(encoding="utf-8")
    assert "PL_RETRIEVAL_MODE" in agent_src
    assert "build_inject_block" in agent_src
    assert '"inject"' in agent_src and '"nudge"' in agent_src


def test_the_policy_carries_a_memory_addendum_not_only_the_nudge():
    """The 2026-09-08 smoke: three episodes, six rules written, zero memory
    searches — the one-time nudge lost to the pull of the environment's
    own search tool. The paper injects a memory addendum into the policy
    itself as well; so does the plugin, from ``prompts.POLICY_ADDENDUM``,
    appended to ``domain_policy`` in ``create_pl_agent`` (pinned on the
    source because agent.py imports the harness)."""
    prompts_text = (PACKAGE_DIR / "prompts.py").read_text(encoding="utf-8")
    assert "POLICY_ADDENDUM" in prompts_text
    ns: dict = {}
    exec(compile(prompts_text, "prompts.py", "exec"), ns)  # noqa: S102
    addendum = ns["POLICY_ADDENDUM"]
    assert "memory_search" in addendum and "memory_lesson_search" in addendum
    agent_src = (PACKAGE_DIR / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(agent_src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "create_pl_agent")
    body = ast.get_source_segment(agent_src, fn)
    assert "POLICY_ADDENDUM" in body and "domain_policy" in body
