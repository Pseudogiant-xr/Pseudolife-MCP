"""No bench endpoint may default to a production shim port without an override.

:8082 is the deployed Claude CLI shim (``ops/install-shim-autostart.ps1``
defaults ``-Port 8082``; the daemon routes dream extraction through it) and
:8086 the deployed Codex shim (``ops/install.ps1`` picks it for the codex
modes). A bench endpoint pinned to one of those measures whatever model and
system prompt the live shim was launched with — a configuration the run does
not choose. The failure is quiet: the incumbent answers ``/v1/models``, so the
harness's reachability probe passes and the artifact looks clean. Verified
live 2026-09-05 — :8082 returned 200 and advertised the model id ``extractor``,
exactly the id these entries request.

Defaults deliberately stay on those ports (that is where the shims really run,
and changing them would invalidate committed results). What this pins is that
each one is REDIRECTABLE, and that the set of such entries is exactly the
declared set — so a new rung added on :8082 with no override fails here rather
than silently benchmarking production.

Both harnesses reuse the SAME variable names, so one export covers a run that
crosses them (``evals/bench_diffusiongemma.ps1`` drives both).
"""
import contextlib
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import ladder_sweep as ladder            # noqa: E402
import longmemeval_bench as lme          # noqa: E402

PRODUCTION_PORTS = {
    ":8082": "deployed Claude shim (ops/install-shim-autostart.ps1)",
    ":8086": "deployed Codex shim (ops/install.ps1)",
}

# entry name -> the env var that must redirect it. terra/luna share one var
# because they share one shim launch; sonnet-5 and diffusiongemma get separate
# vars because they are different shims (claude_shim.py vs dg_shim.py) that
# merely collide on a port, so redirecting one must not move the other.
LADDER_EXPECTED = {
    "sonnet-5": "PSEUDOLIFE_BENCH_SONNET_URL",
    "diffusiongemma": "PSEUDOLIFE_BENCH_DG_URL",
    "terra": "PSEUDOLIFE_BENCH_CODEX_URL",
    "luna": "PSEUDOLIFE_BENCH_CODEX_URL",
}
LME_EXPECTED = {
    "sonnet-5": "PSEUDOLIFE_BENCH_SONNET_URL",
    "diffusiongemma": "PSEUDOLIFE_BENCH_DG_URL",
}
ALL_VARS = sorted(set(LADDER_EXPECTED.values()) | set(LME_EXPECTED.values()))

SENTINEL = "http://127.0.0.1:9099/v1"


def _on_production_port(url: str) -> bool:
    return any(p in url for p in PRODUCTION_PORTS)


@contextlib.contextmanager
def _reloaded(module, **env):
    """Reimport ``module`` under a controlled endpoint env.

    Both harnesses resolve their overrides at import time (matching the
    long-standing ``PSEUDOLIFE_BENCH_QWEN_URL`` convention), so exercising an
    override means reimporting. Restores both the env and the module.
    """
    names = set(ALL_VARS) | set(env)
    saved = {k: os.environ.get(k) for k in names}
    try:
        for k in names:
            os.environ.pop(k, None)
        os.environ.update(env)
        yield importlib.reload(module)
    finally:
        for k in names:
            os.environ.pop(k, None)
            if saved.get(k) is not None:
                os.environ[k] = saved[k]
        importlib.reload(module)


def _ladder_endpoints(mod):
    return {n: r["base_url"] for n, r in mod.RUNGS.items() if "base_url" in r}


def _lme_endpoints(mod):
    return dict(mod.EXTRACTORS)


HARNESSES = [
    pytest.param(ladder, _ladder_endpoints, LADDER_EXPECTED,
                 id="ladder_sweep.RUNGS"),
    pytest.param(lme, _lme_endpoints, LME_EXPECTED,
                 id="longmemeval_bench.EXTRACTORS"),
]


@pytest.mark.parametrize("module,endpoints,expected", HARNESSES)
def test_production_port_entries_are_exactly_the_declared_set(
        module, endpoints, expected):
    """A new entry added on :8082/:8086 fails here until it declares a var."""
    with _reloaded(module) as mod:
        found = {n for n, url in endpoints(mod).items()
                 if _on_production_port(url)}
    assert found == set(expected), (
        f"entries on a production shim port changed: {found ^ set(expected)}. "
        "Give each one an env override and declare it here, or move it off "
        f"the port. Production ports: {PRODUCTION_PORTS}")


@pytest.mark.parametrize("module,endpoints,expected", HARNESSES)
def test_every_production_port_entry_is_redirectable(
        module, endpoints, expected):
    for name, var in expected.items():
        with _reloaded(module, **{var: SENTINEL}) as mod:
            assert endpoints(mod)[name] == SENTINEL, (
                f"{name} ignored {var} — it would still hit "
                f"{endpoints(mod)[name]}")


@pytest.mark.parametrize("module,endpoints,expected", HARNESSES)
def test_defaults_are_unchanged_when_no_var_is_set(
        module, endpoints, expected):
    """The fix must not move any default — committed results depend on them."""
    with _reloaded(module) as mod:
        for name in expected:
            assert _on_production_port(endpoints(mod)[name])


def test_the_two_8082_shims_move_independently():
    """claude_shim.py and dg_shim.py merely collide on :8082."""
    for module, endpoints in ((ladder, _ladder_endpoints),
                              (lme, _lme_endpoints)):
        with _reloaded(module, PSEUDOLIFE_BENCH_SONNET_URL=SENTINEL) as mod:
            eps = endpoints(mod)
            assert eps["sonnet-5"] == SENTINEL
            assert ":8082" in eps["diffusiongemma"]


def test_one_codex_var_moves_both_gpt_rungs():
    """terra/luna share a shim launch, so one var must move both — redirecting
    only one would leave the other pointed at production."""
    with _reloaded(ladder, PSEUDOLIFE_BENCH_CODEX_URL=SENTINEL) as mod:
        assert mod.RUNGS["terra"]["base_url"] == SENTINEL
        assert mod.RUNGS["luna"]["base_url"] == SENTINEL


def test_both_harnesses_share_variable_names():
    """evals/bench_diffusiongemma.ps1 drives ladder AND lme in one run, so a
    single export must redirect both or the run is half-fixed."""
    for name, var in LME_EXPECTED.items():
        assert LADDER_EXPECTED[name] == var
