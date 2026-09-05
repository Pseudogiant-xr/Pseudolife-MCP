"""Tests for evals/ladder_sweep.py — canonical-result overwrite guard.

A rerun once silently rewrote ``evals/results/sonnet-5.json`` in place
(2026-07-21), erasing the canonical run's timing fields. The rule (CLAUDE.md,
"Publishing a benchmark number") is: never overwrite a canonical result file
on a rerun — tag the run and promote deliberately. These tests pin the code
that enforces it.

Pure-function + CLI-wiring tests only: the run_* functions are monkeypatched,
so no endpoints, no Postgres, no GPU.
"""
import contextlib
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import ladder_sweep as ladder  # noqa: E402


class TestResolveOutPath:
    def test_tag_names_a_sibling_and_never_touches_canonical(self, tmp_path):
        base = tmp_path / "sonnet-5.json"
        base.write_text("{}", encoding="utf-8")
        assert ladder.resolve_out_path(base, "retrv1") == (
            tmp_path / "sonnet-5-retrv1.json")

    def test_untagged_first_run_uses_the_canonical_path(self, tmp_path):
        base = tmp_path / "e2b.json"
        assert ladder.resolve_out_path(base, None) == base

    def test_untagged_rerun_refuses_to_clobber(self, tmp_path):
        base = tmp_path / "sonnet-5.json"
        base.write_text("{}", encoding="utf-8")
        with pytest.raises(SystemExit, match="--out-tag"):
            ladder.resolve_out_path(base, None)


def test_cli_accepts_out_tag():
    parser = ladder.build_parser()
    assert parser.parse_args(["--rung", "naive-rag",
                              "--out-tag", "r2"]).out_tag == "r2"
    assert parser.parse_args(["--rung", "naive-rag"]).out_tag is None


@pytest.mark.parametrize("flag,canonical", [
    ("--rung", "naive-rag.json"),
    ("--abstain", "abstain.json"),
    ("--supersede", "supersede.json"),
])
def test_guard_fires_before_the_run_starts(tmp_path, monkeypatch,
                                           flag, canonical):
    """Refusing AFTER an overnight run would discard it — the guard must
    resolve the output path before any run function is entered."""
    (tmp_path / canonical).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ladder, "RESULTS_DIR", tmp_path)

    def _must_not_run(*a, **k):
        raise AssertionError("run started despite existing canonical file")

    monkeypatch.setattr(ladder, "run_rung", _must_not_run)
    monkeypatch.setattr(ladder, "run_abstain", _must_not_run)
    monkeypatch.setattr(ladder, "run_supersede", _must_not_run)
    with pytest.raises(SystemExit, match="--out-tag"):
        ladder.main([flag, "naive-rag"])


def test_tagged_rerun_writes_the_tagged_file_only(tmp_path, monkeypatch,
                                                  capsys):
    (tmp_path / "naive-rag.json").write_text('{"canonical": true}',
                                             encoding="utf-8")
    monkeypatch.setattr(ladder, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ladder, "run_rung", lambda rung: {"ok": 1})
    assert ladder.main(["--rung", "naive-rag", "--out-tag", "r2"]) == 0
    capsys.readouterr()
    assert json.loads((tmp_path / "naive-rag-r2.json").read_text(
        encoding="utf-8")) == {"ok": 1}
    assert json.loads((tmp_path / "naive-rag.json").read_text(
        encoding="utf-8")) == {"canonical": True}


# ---------------------------------------------------------------------------
# Rung endpoints: never silently benchmark a production shim
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _reloaded_rungs(**env):
    """Reimport ladder_sweep under a controlled endpoint env.

    ``RUNGS`` resolves its env overrides at import time (matching the
    long-standing ``PSEUDOLIFE_BENCH_QWEN_URL`` rungs), so exercising the
    override means reimporting. Restores both the env and the module.
    """
    keys = ("PSEUDOLIFE_BENCH_SONNET_URL", "PSEUDOLIFE_BENCH_DG_URL",
            "PSEUDOLIFE_BENCH_CODEX_URL")
    saved = {k: os.environ.get(k) for k in set(keys) | set(env)}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(env)
        yield importlib.reload(ladder).RUNGS
    finally:
        for k in set(keys) | set(env):
            os.environ.pop(k, None)
            if saved.get(k) is not None:
                os.environ[k] = saved[k]
        importlib.reload(ladder)


class TestProductionShimPortsAreOverridable:
    """A rung whose default endpoint is a PRODUCTION shim must be redirectable.

    ``:8082`` is the deployed Claude shim (``ops/.env``
    ``PSEUDOLIFE_DREAM_BASE_URL``, ``ops/install-shim-autostart.ps1 -Port``)
    and ``:8086`` the deployed Codex shim (``ops/install.ps1``). A rung that
    hardcodes one of those benchmarks whatever the live shim happens to be
    serving — a model and system prompt the run neither chooses nor records —
    and the incumbent answers ``/models``, so ``probe()`` passes and the
    result file looks clean. ``diffusiongemma`` is the sharp edge: it sits in
    ``LADDER_ORDER``, so a bare sweep hits ``:8082`` with no ``--rung`` at all.
    """

    CASES = [
        ("sonnet-5", "PSEUDOLIFE_BENCH_SONNET_URL", "http://127.0.0.1:8082/v1"),
        ("diffusiongemma", "PSEUDOLIFE_BENCH_DG_URL", "http://127.0.0.1:8082/v1"),
        ("terra", "PSEUDOLIFE_BENCH_CODEX_URL", "http://127.0.0.1:8086/v1"),
        ("luna", "PSEUDOLIFE_BENCH_CODEX_URL", "http://127.0.0.1:8086/v1"),
    ]

    @pytest.mark.parametrize("rung,var,default", CASES)
    def test_default_is_the_documented_port(self, rung, var, default):
        with _reloaded_rungs() as rungs:
            assert rungs[rung]["base_url"] == default

    @pytest.mark.parametrize("rung,var,default", CASES)
    def test_env_var_redirects_the_rung(self, rung, var, default):
        with _reloaded_rungs(**{var: "http://127.0.0.1:9099/v1"}) as rungs:
            assert rungs[rung]["base_url"] == "http://127.0.0.1:9099/v1"

    def test_the_two_8082_rungs_move_independently(self):
        """Separate shims (claude_shim vs dg_shim) that merely collide on a
        port — redirecting one must not drag the other along."""
        with _reloaded_rungs(
                PSEUDOLIFE_BENCH_SONNET_URL="http://127.0.0.1:9099/v1") as rungs:
            assert rungs["sonnet-5"]["base_url"] == "http://127.0.0.1:9099/v1"
            assert rungs["diffusiongemma"]["base_url"] == "http://127.0.0.1:8082/v1"

    def test_one_codex_var_moves_both_gpt_rungs(self):
        """terra/luna share a shim launch by design (luna overrides the model
        per request), so one var moves both — redirecting only one would leave
        the other on the production port."""
        with _reloaded_rungs(
                PSEUDOLIFE_BENCH_CODEX_URL="http://127.0.0.1:9099/v1") as rungs:
            assert rungs["terra"]["base_url"] == "http://127.0.0.1:9099/v1"
            assert rungs["luna"]["base_url"] == "http://127.0.0.1:9099/v1"


class TestResultRecordsItsEndpoint:
    """Which endpoint served a rung belongs IN the artifact.

    Before this, a successful run recorded neither ``base_url`` nor ``model``
    (only the unreachable branch did), so a run against the wrong shim was
    indistinguishable afterwards from a run against the right one.
    """

    def test_successful_llm_run_stamps_base_url_and_model(self, monkeypatch):
        monkeypatch.setattr(ladder, "probe", lambda *a, **k: True)
        monkeypatch.setattr(ladder, "build_service", lambda *a, **k: object())
        monkeypatch.setattr(ladder, "ingest", lambda *a, **k: None)
        monkeypatch.setattr(ladder, "make_extractor", lambda *a, **k: object())
        monkeypatch.setattr(ladder, "consolidate", lambda *a, **k: (1.0, {}))
        monkeypatch.setattr(ladder, "measure_cortex", lambda *a, **k: {})
        out = ladder.run_rung("sonnet-5")
        assert out["status"] == "ok"
        assert out["base_url"] == ladder.RUNGS["sonnet-5"]["base_url"]
        assert out["model"] == ladder.RUNGS["sonnet-5"]["model"]

    def test_unreachable_llm_run_still_stamps_the_endpoint(self, monkeypatch):
        monkeypatch.setattr(ladder, "probe", lambda *a, **k: False)
        out = ladder.run_rung("sonnet-5")
        assert out["status"] == "unreachable"
        assert out["base_url"] == ladder.RUNGS["sonnet-5"]["base_url"]
        assert out["model"] == ladder.RUNGS["sonnet-5"]["model"]

    @pytest.mark.parametrize("runner,flag", [("run_abstain", "--abstain"),
                                             ("run_supersede", "--supersede")])
    def test_sub_sweeps_stamp_the_endpoint_too(self, monkeypatch, runner, flag):
        """The abstain/supersede sub-sweeps pick a rung the same way and write
        their own artifacts, so they carry the same provenance."""
        monkeypatch.setattr(ladder, "probe", lambda *a, **k: False)
        out = getattr(ladder, runner)("sonnet-5")
        assert out["status"] == "unreachable"
        assert out["base_url"] == ladder.RUNGS["sonnet-5"]["base_url"]
        assert out["model"] == ladder.RUNGS["sonnet-5"]["model"]

    def test_stamp_helper_is_empty_for_non_llm_rungs(self):
        assert ladder.endpoint_stamp(ladder.RUNGS["naive-rag"]) == {}
        assert ladder.endpoint_stamp(ladder.RUNGS["floor"]) == {}

    def test_sub_sweep_ok_result_stamps_the_endpoint(self, monkeypatch):
        """The unreachable branch is the easy one; pin the ok branch too."""
        class _Cfg:
            class memory:
                class search:
                    pass
                class cortex:
                    dream_slot_match_threshold = 0.0
            search_confidence_floor = 0.0

        class _Svc:
            config = _Cfg()
            def search(self, *a, **k):
                return {"low_confidence": False}
            def cortex_search(self, *a, **k):
                return {"entries": []}
            def store(self, *a, **k):
                return None
        monkeypatch.setattr(ladder, "probe", lambda *a, **k: True)
        monkeypatch.setattr(ladder, "build_service", lambda *a, **k: _Svc())
        monkeypatch.setattr(ladder, "ingest", lambda *a, **k: None)
        monkeypatch.setattr(ladder, "make_extractor", lambda *a, **k: object())
        monkeypatch.setattr(ladder, "consolidate", lambda *a, **k: (1.0, {}))
        out = ladder.run_abstain("sonnet-5", floors=(0.0,), guards=(0.3,))
        assert out["status"] == "ok"
        assert out["base_url"] == ladder.RUNGS["sonnet-5"]["base_url"]
        assert out["model"] == ladder.RUNGS["sonnet-5"]["model"]


class TestCliDiscoverability:
    """A bare invocation must print help, and --list must show every rung.

    Both matter to the production-port fix: three of the four rungs that
    default to a production port sit OUTSIDE ``LADDER_ORDER``, so a --list
    that iterates only that order hides exactly the endpoints a reader
    needs to check. And ``main()`` fell through to a name that only exists
    inside ``build_parser()``, so a bare run died with
    ``NameError: name 'ap' is not defined`` instead of printing help.
    """

    def test_bare_invocation_prints_help_and_does_not_crash(self, capsys):
        assert ladder.main([]) == 1
        assert "usage:" in capsys.readouterr().out

    @pytest.mark.parametrize("rung", ["sonnet-5", "terra", "luna",
                                      "opus-5", "fable-5", "diffusiongemma"])
    def test_list_shows_every_registered_rung_and_endpoint(self, capsys, rung):
        assert ladder.main(["--list"]) == 0
        out = capsys.readouterr().out
        assert rung in out
        assert ladder.RUNGS[rung]["base_url"] in out


    def test_naive_rung_has_no_endpoint_to_stamp(self, monkeypatch):
        monkeypatch.setattr(ladder, "build_service", lambda *a, **k: object())
        monkeypatch.setattr(ladder, "ingest", lambda *a, **k: None)
        monkeypatch.setattr(ladder, "measure_naive", lambda *a, **k: {})
        out = ladder.run_rung("naive-rag")
        assert "base_url" not in out
        assert "model" not in out
