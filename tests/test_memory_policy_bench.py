"""Offline guards for ``evals/memory_policy_bench.py``: no model, no daemon.

The bench spends real tokens, so the parts that decide whether a run may
start at all (safety guards) and what a finished run means (validity scan,
statistics, the acceptance rule) are pinned here.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from evals import memory_policy_bench as mb
from evals import memory_policy_scenarios as fx
from evals.memory_policy_daemon import BenchSafetyError, check_database, check_port


# ── safety ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@127.0.0.1:5433/pseudolife_memory",     # the live bank
    "postgresql://u:p@127.0.0.1:5433/pseudolife_memory_test_1",  # not a bench db
    "postgresql://u:p@127.0.0.1:5433/",                      # no database named
    "postgresql://u:p@127.0.0.1:5433/plbench_x?dbname=pseudolife_memory",
])
def test_bench_refuses_any_database_but_its_own(dsn, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    with pytest.raises(Exception):
        check_database(dsn)


def test_bench_accepts_a_plbench_database(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    assert check_database("postgresql://u:p@127.0.0.1:5433/plbench_abc") == "plbench_abc"


@pytest.mark.parametrize("port", [8765, 8086, 8082, 8081, 1234, 5433])
def test_bench_refuses_live_service_ports(port):
    with pytest.raises(BenchSafetyError):
        check_port(port)


def test_work_root_must_be_outside_home_and_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(BenchSafetyError, match="home directory"):
        mb.check_work_root(tmp_path / "bench")
    with pytest.raises(BenchSafetyError, match="repository"):
        mb.check_work_root(mb.ROOT / "evals" / "scratch")


def test_work_root_refuses_an_ancestor_instruction_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "CLAUDE.md").write_text("instructions")
    with pytest.raises(BenchSafetyError, match="CLAUDE.md"):
        mb.check_work_root(outside / "deep")
    (outside / "CLAUDE.md").unlink()
    (outside / ".claude").mkdir()
    with pytest.raises(BenchSafetyError, match=".claude"):
        mb.check_work_root(outside / "deep")


def test_children_never_inherit_live_credentials_or_urls(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "live-token")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/real/config")
    monkeypatch.setenv("CODEX_HOME", "/real/codex")
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    env = mb.scrubbed_env({"PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://x/plbench_1"})
    for key in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_DAEMON_URL", "ANTHROPIC_API_KEY",
                "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        assert key not in env
    # The child's guard must still know the live bank, not take its own
    # bench database for it.
    assert env["_PSEUDOLIFE_PRODUCTION_DB"] == "pseudolife_memory"
    assert env["CUDA_VISIBLE_DEVICES"] == "-1"


def test_haiku_is_refused():
    with pytest.raises(SystemExit, match="Haiku"):
        mb.main(["run", "--tag", "x", "--model", "claude-haiku-4-5"])


def test_bench_plugin_wires_only_the_memory_start_and_end_hooks(tmp_path):
    plugin = mb.bench_plugin(tmp_path / "plugin")
    hooks = json.loads((plugin / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "SessionEnd"}
    commands = [h["command"] for g in hooks["SessionStart"] for h in g["hooks"]]
    assert len(commands) == 2
    assert any(c.endswith('session-start.sh" memory-policy') for c in commands)
    assert not any("coordination" in c for c in commands)
    # The scripts themselves are the repository's, byte for byte.
    for name in ("session-start.sh", "session-end.sh", "lifecycle.ps1"):
        assert (plugin / "hooks" / name).read_bytes() == (mb.ROOT / "plugin/hooks" / name).read_bytes()


# ── plan ───────────────────────────────────────────────────────────────────

def test_plan_interleaves_arms_within_each_block_and_is_seeded():
    arms = mb.parse_arms("none,full_separate_hook,full_separate_hook@aa")
    items = mb.plan(arms, ["a_lesson", "b_contested"], 2, seed=7)
    assert len(items) == 12
    for i in range(0, 12, 3):
        block = items[i:i + 3]
        assert {b["arm"] for b in block} == {a.label for a in arms}
        assert len({(b["scenario"], b["replicate"]) for b in block}) == 1
    assert items == mb.plan(arms, ["a_lesson", "b_contested"], 2, seed=7)
    assert [a.variant for a in arms] == ["none", "full_separate_hook", "full_separate_hook"]


def test_arm_labels_must_be_unique_and_variants_known():
    with pytest.raises(SystemExit):
        mb.parse_arms("none,none")
    with pytest.raises(SystemExit):
        mb.parse_arms("full")


def test_every_scenario_names_a_rule_and_a_file_check():
    assert len(fx.SCENARIOS) == 8
    planted = {e.key for e in fx.SEED_ENTRIES}
    for sc in fx.SCENARIOS:
        assert sc.rule and sc.checks
        assert set(sc.relevant) <= planted
        # A prompt that names a memory tool would be policy text of its own.
        assert "memory_" not in sc.prompt and "memory" not in sc.prompt.lower()


# ── grading helpers ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ([1421, 903], [1421, 903]),
    ("[1421, 903]", [1421, 903]),        # Claude Code stringifies anyOf lists
    ("1421,903", [1421, 903]),
    (None, []),
    (["x", 5], [5]),
])
def test_used_ids_parse_as_clients_send_them(raw, expected):
    assert mb.parse_ids(raw) == expected


def test_scrub_removes_canary_and_home_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "alice"))
    rec = {"a": f"stg-secret at {tmp_path / 'alice' / 'x'}", "b": ["stg-secret"], "c": "alice"}
    mb.scrub_record(rec, "stg-secret")
    text = json.dumps(rec)
    assert "stg-secret" not in text and "alice" not in text


# ── validity ───────────────────────────────────────────────────────────────

PROMPT = "Fill in the Region line in docs/RUNBOOK.md, please, for the production deploy."


def _capture(tmp_path, *reminders, model="claude-sonnet-5", system="You are Claude Code."):
    cap = tmp_path / "capture"
    cap.mkdir()
    content = [{"type": "text", "text": r} for r in reminders] + [{"type": "text", "text": PROMPT}]
    body = {"model": model, "system": [{"type": "text", "text": system}],
            "messages": [{"role": "user", "content": content}]}
    (cap / "0001.json").write_text(json.dumps({"path": "/v1/messages?beta=true",
                                               "body": json.dumps(body)}))
    return cap


def _hook(body):
    return {"kind": "hook", "path": "/api/hook/session-start", "body": body}


GRADE = {"registered_session_ids": ["sess-1"]}
AD = 'Session episode: abcdef123456 — pass episode="abcdef123456" on every memory write.'
BRIEFING = "## Lessons from past work\n- avoid: Do not upgrade the NDK past r27."


def test_valid_when_only_the_arms_policy_and_constant_surfaces_are_present(tmp_path):
    from pseudolife_memory.web.session_hook import STARTUP_MEMORY_CORE
    served = f"{AD}\n\n{STARTUP_MEMORY_CORE}\n\n{BRIEFING}"
    cap = _capture(tmp_path, f"SessionStart:startup hook success: {served}")
    v = mb.validity(variant="compact", capture_dir=cap, ledger=[_hook(served)],
                    prompt=PROMPT, model="claude-sonnet-5", grade=GRADE)
    assert v["valid"], v["reasons"]


def test_a_leaked_instruction_file_invalidates_the_run(tmp_path):
    served = f"{AD}\n\n{BRIEFING}"
    leak = ("Contents of CLAUDE.md: log memory_outcome at task end and search "
            "PseudoLife before every review.")
    cap = _capture(tmp_path, leak, f"SessionStart:startup hook success: {served}")
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)],
                    prompt=PROMPT, model="claude-sonnet-5", grade=GRADE)
    assert not v["valid"]
    assert any("outside the arm's policy" in r for r in v["reasons"])


def test_client_auto_memory_counts_as_competing_policy(tmp_path):
    """Claude Code's auto memory section (seen 2026-09-25 in a bench run) is
    a memory policy of its own; the bench turns it off and must notice it."""
    served = "\n\n".join((AD, BRIEFING))
    cap = _capture(tmp_path, f"SessionStart:startup hook success: {served}",
                   system="# auto memory\nYou have a persistent, file-based memory system. "
                          "Add a pointer to MEMORY.md.")
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)], prompt=PROMPT,
                    model="claude-sonnet-5", grade=GRADE)
    assert not v["valid"]


def test_run_paths_carrying_the_scenario_id_are_not_policy(tmp_path):
    served = "\n\n".join((AD, BRIEFING))
    cap = _capture(tmp_path, f"SessionStart:startup hook success: {served}",
                   system=r"Primary working directory: C:\plbench\t1\runs\t1-none-a_lesson-r0\lanternfish")
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)], prompt=PROMPT,
                    model="claude-sonnet-5", grade=GRADE,
                    strip=("t1-none-a_lesson-r0", "t1-none-a-lesson-r0", "t1"))
    assert v["valid"], v["reasons"]


def test_the_wrong_arms_policy_invalidates_the_run(tmp_path):
    from pseudolife_memory.web.session_hook import STARTUP_MEMORY_CORE
    served = f"{AD}\n\n{STARTUP_MEMORY_CORE}\n\n{BRIEFING}"
    cap = _capture(tmp_path, f"SessionStart:startup hook success: {served}")
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)],
                    prompt=PROMPT, model="claude-sonnet-5", grade=GRADE)
    assert not v["valid"]
    assert any("core text is present" in r for r in v["reasons"])


def test_a_missing_policy_or_another_model_invalidates_the_run(tmp_path):
    served = f"{AD}\n\n{BRIEFING}"
    cap = _capture(tmp_path, f"SessionStart:startup hook success: {served}",
                   model="claude-haiku-4-5")
    v = mb.validity(variant="full_separate_hook", capture_dir=cap, ledger=[_hook(served)],
                    prompt=PROMPT, model="claude-sonnet-5", grade=GRADE)
    assert not v["valid"]
    assert any("full_block text is missing" in r for r in v["reasons"])
    assert any("another model" in r for r in v["reasons"])


def test_hooks_that_missed_the_disposable_daemon_invalidate_the_run(tmp_path):
    served = f"{AD}\n\n{BRIEFING}"
    cap = _capture(tmp_path, f"SessionStart:startup hook success: {served}")
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)],
                    prompt=PROMPT, model="claude-sonnet-5", grade={"registered_session_ids": []})
    assert not v["valid"]
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)], prompt=PROMPT,
                    model="claude-sonnet-5", grade=GRADE, client_session="another-session")
    assert any("different session" in r for r in v["reasons"])


def test_constant_surfaces_and_the_session_end_reply_do_not_count(tmp_path):
    """The plugin's slash commands appear in the skill list and the MCP
    instructions carry the server's name as a header: constant across arms.
    The SessionEnd reply never reaches the model and is not compared."""
    served = "\n\n".join((AD, BRIEFING))
    system = "\n".join(("Skills: - pseudolife-memory:dream - pseudolife-memory:memory-status",
                        "# MCP Server Instructions", "## pseudolife-memory",
                        mb.mcp_instructions(),
                        "Deferred tools: mcp__pseudolife-memory__memory_search"))
    cap = _capture(tmp_path, f"SessionStart:startup hook success: {served}", system=system)
    ledger = [_hook(served), {"kind": "hook", "path": "/api/hook/session-end", "body": '{"ok": true}'}]
    v = mb.validity(variant="none", capture_dir=cap, ledger=ledger, prompt=PROMPT,
                    model="claude-sonnet-5", grade=GRADE, client_session="sess-1")
    assert v["valid"], v


# ── statistics and the acceptance rule ─────────────────────────────────────

def _record(arm, sid, rep, compliance, success, bite=100_000.0, valid=True):
    return {"arm": arm, "scenario": sid, "replicate": rep, "validity": {"valid": valid},
            "grade": {"compliance": compliance, "task_success": success, "used_ids": {},
                      "generic": {}},
            "cost": {"bite": bite}, "wall_s": 60.0}


def test_bite_weights_follow_list_price_ratios():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 100,
             "cache_creation": {"ephemeral_1h_input_tokens": 100, "ephemeral_5m_input_tokens": 40}}
    assert mb.bite(usage) == pytest.approx(10 + 100 + 500 + 200 + 50)


def test_score_is_compliance_plus_success_minus_the_cost_penalty():
    m = mb.run_metrics(_record("x", "a_lesson", 0, 1.0, 1.0, bite=200_000), lam=0.1)
    assert m["score"] == pytest.approx(2.0 - 0.2)


def test_cluster_bootstrap_ci_brackets_the_mean_and_is_seeded():
    groups = {"a": [1.0, 1.0, 0.0], "b": [0.0, 0.0, 1.0], "c": [1.0, 1.0, 1.0]}
    mean, ci = mb.cluster_bootstrap(groups, random.Random(1), samples=2000)
    assert mean == pytest.approx(6 / 9)
    assert ci[0] <= mean <= ci[1]
    assert (mean, ci) == mb.cluster_bootstrap(groups, random.Random(1), samples=2000)


def test_paired_deltas_pair_on_scenario_and_replicate_and_skip_invalid_runs():
    recs = [_record("none", s, r, 0.0, 0.0) for s in ("a", "b") for r in range(3)]
    recs += [_record("full", s, r, 1.0, 1.0) for s in ("a", "b") for r in range(3)]
    recs.append(_record("full", "a", 9, 0.0, 0.0))                 # no partner
    recs.append(_record("none", "b", 7, 1.0, 1.0, valid=False))    # invalid
    d = mb.paired(recs, "none", "full", lam=0.1)
    assert d["compliance"]["delta"] == pytest.approx(1.0)
    assert d["compliance"]["pairs"] == 6


def test_acceptance_needs_a_gain_beyond_aa_noise_and_no_guarded_regression():
    noise = {m: 0.1 for m in mb.METRICS}
    good = {m: {"delta": 0.0} for m in mb.METRICS}
    good["score"] = {"delta": 0.3, "pairs": 24, "scenarios": 8}
    assert mb.accept(good, noise)["accept"]
    small = dict(good, score={"delta": 0.05, "pairs": 24, "scenarios": 8})
    assert not mb.accept(small, noise)["accept"]
    thin = dict(good, score={"delta": 0.3, "pairs": 4, "scenarios": 2})
    assert "too little evidence" in mb.accept(thin, noise)["reasons"][0]
    assert not mb.accept(good, noise, invalid_share={"x": 0.5})["accept"]
    unknown = dict(good, used_ids_precision={"delta": None})
    assert mb.accept(unknown, noise)["unevaluated_guards"] == ["used_ids_precision"]
    costly = dict(good, cost_bite={"delta": 0.5})          # cost up beyond noise
    verdict = mb.accept(costly, noise)
    assert not verdict["accept"] and "cost_bite" in verdict["reasons"][0]
    worse = dict(good, task_success={"delta": -0.2})
    assert not mb.accept(worse, noise)["accept"]


def test_default_arm_order_still_yields_verdicts_in_both_directions():
    """Reviewer finding H1: with --arms none,full,full@aa (the default) the
    verdict lookup used the wrong key order and silently produced none."""
    arms = mb.parse_arms("none,full_separate_hook,full_separate_hook@aa")
    recs = []
    for sid in ("a", "b", "c", "d"):
        for rep in range(3):
            recs.append(_record("none", sid, rep, 0.0, 0.0))
            recs.append(_record("full_separate_hook", sid, rep, 1.0, 1.0))
            recs.append(_record("full_separate_hook@aa", sid, rep, 1.0, 1.0))

    class Args:
        cost_lambda, client, model, effort, replicates, seed = 0.1, "claude", "m", "e", 3, 1

    art = mb.build_artifact(recs, arms, Args, {"entries": {}, "lessons": {}}, "t")
    assert set(art["acceptance"]) == {"full_separate_hook over none",
                                      "none over full_separate_hook"}
    assert art["acceptance"]["full_separate_hook over none"]["accept"]
    assert not art["acceptance"]["none over full_separate_hook"]["accept"]


def test_a_short_tag_cannot_blind_the_leak_scan(tmp_path):
    """Reviewer finding M1: only path-like tokens carrying the run id go."""
    served = "\n\n".join((AD, BRIEFING))
    cap = _capture(tmp_path, "log memory_outcome at task end",
                   f"SessionStart:startup hook success: {served}")
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)], prompt=PROMPT,
                    model="claude-sonnet-5", grade=GRADE, strip=("memory", "e"))
    assert not v["valid"]


def test_text_injected_after_the_first_request_is_scanned_but_not_the_agents_words(tmp_path):
    served = "\n\n".join((AD, BRIEFING))
    cap = _capture(tmp_path, f"SessionStart:startup hook success: {served}")
    first = json.loads(json.loads((cap / "0001.json").read_text())["body"])
    later = dict(first, messages=first["messages"] + [
        {"role": "assistant", "content": [{"type": "text", "text": "I will call memory_search."}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "memory_outcome hit"}]}])
    (cap / "0002.json").write_text(json.dumps({"path": "/v1/messages", "body": json.dumps(later)}))
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)], prompt=PROMPT,
                    model="claude-sonnet-5", grade=GRADE)
    assert v["valid"], v["reasons"]
    leaked = dict(first, messages=first["messages"] + [
        {"role": "user", "content": [{"type": "text", "text": "Remember: memory_store it."}]}])
    (cap / "0003.json").write_text(json.dumps({"path": "/v1/messages", "body": json.dumps(leaked)}))
    v = mb.validity(variant="none", capture_dir=cap, ledger=[_hook(served)], prompt=PROMPT,
                    model="claude-sonnet-5", grade=GRADE)
    assert not v["valid"]


@pytest.mark.parametrize("rec,done", [
    ({"grade": {}, "validity": {}, "errors": [], "client_rc": 0, "timed_out": False}, False),
    ({"grade": {"x": 1}, "validity": {"valid": True}, "errors": [], "client_rc": 0,
      "timed_out": False}, True),
    ({"grade": {"x": 1}, "validity": {"valid": True}, "errors": ["BenchSafetyError"],
      "client_rc": 0, "timed_out": False}, False),
    ({"grade": {"x": 1}, "validity": {"valid": False}, "errors": [], "client_rc": None,
      "timed_out": True}, False),
])
def test_only_cleanly_finished_runs_are_skipped_on_resume(rec, done):
    assert mb.finished(rec) is done


def test_a_legacy_run_keeps_its_exit_status_when_regraded(tmp_path):
    """Runs recorded before client.json existed are rebuilt from their
    stream and ledger; their exit status survives only in the old record,
    which regrade must carry (the first regrade read every run as crashed)."""
    run = tmp_path / "t-none-a_lesson-r0"
    run.mkdir()
    (run / "stream.jsonl").write_text(json.dumps(
        {"type": "result", "subtype": "success", "usage": {}, "modelUsage": {}}) + "\n")
    (run / "ledger.jsonl").write_text(json.dumps({"t": 100.0, "kind": "hook"}) + "\n")
    rec = {"run_id": "t-none-a_lesson-r0", "arm": "none", "variant": "none",
           "scenario": "a_lesson", "replicate": 0, "client_rc": 0, "timed_out": False}
    meta, client, final = mb.load_run(run, rec)
    assert client["rc"] == 0 and client["timed_out"] is False
    assert client["started"] == pytest.approx(98.0)
    assert meta["db"].startswith("plbench_")


@pytest.mark.parametrize("url,ok", [
    ("https://www.sqlite.org/limits.html", True),
    ("https://sqlite.org/limits.html", True),
    ("https://evil.example/?ref=sqlite.org", False),
    ("https://sqlite.org.evil.example/", False),
    ("", False),
    (None, False),
])
def test_world_fact_citation_host_is_parsed_not_substring_matched(url, ok):
    assert mb.host_is(url, "sqlite.org") is ok


def test_proxied_header_values_cannot_split_a_response():
    assert mb.header_value("text/event-stream\r\nSet-Cookie: x=1") == (
        "text/event-streamSet-Cookie: x=1")
    assert set(mb.CaptureProxy._FORWARD) >= {"content-type", "content-encoding"}


def test_used_ids_parse_like_the_daemon():
    assert mb.parse_ids([True, 3.0, 2.5, 7]) == [3, 7]
    assert len(mb.parse_ids(list(range(80)))) == 50


def test_mcp_instructions_are_read_without_importing_the_server(tmp_path):
    """Importing mcp_server builds a MemoryService from the caller's
    environment and creates ./data (reviewer finding); the bench reads the
    constant from source instead. Checked in a fresh interpreter."""
    import subprocess
    import sys
    code = ("import sys; from evals import memory_policy_bench as mb; "
            "t = mb.mcp_instructions(); "
            "print('memory_search' in t, 'pseudolife_memory.mcp_server' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True,
                         text=True, timeout=120,
                         env={**__import__("os").environ, "PYTHONPATH": str(mb.ROOT)})
    assert out.stdout.split() == ["True", "False"], out.stderr[-2000:]
    assert not (tmp_path / "data").exists()


def test_children_do_not_inherit_unrelated_keys(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("HF_TOKEN", "x")
    monkeypatch.setenv("PGPASSWORD", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("SOME_SERVICE_API_KEY", "x")
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    env = mb.scrubbed_env()
    for key in ("GH_TOKEN", "HF_TOKEN", "PGPASSWORD", "AWS_SECRET_ACCESS_KEY",
                "SOME_SERVICE_API_KEY"):
        assert key not in env
    assert "PATH" in env or "Path" in env


def test_aa_noise_is_the_largest_difference_the_aa_ci_admits():
    aa = {"score": {"delta": 0.02, "ci95": [-0.15, 0.1]}, "compliance": {"delta": None,
                                                                          "ci95": [None, None]}}
    assert mb.aa_noise(aa) == {"score": 0.15, "compliance": None}


def test_artifacts_are_never_overwritten(tmp_path, monkeypatch):
    out = tmp_path / "artifact.json"
    out.write_text("{}")

    class Args:
        tag = "t"
        work_root = tmp_path / "w"
        arms = "none"
        scenarios = "a_lesson"
        replicates = 1
        seed = 1
        client = "claude"
        model = "claude-sonnet-5"
        effort = "medium"
        parallel = 1
        out = tmp_path / "artifact.json"

    monkeypatch.setattr(mb, "check_work_root", lambda p: p)
    monkeypatch.setattr(mb, "admin_url", lambda: "postgresql://x/postgres")
    with pytest.raises(SystemExit, match="single-use"):
        mb.Bench(Args).run()
