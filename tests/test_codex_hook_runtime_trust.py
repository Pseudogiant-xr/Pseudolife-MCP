"""Matching scripts do not prove that Codex will execute their handlers."""
from contextlib import contextmanager

import pytest

from pseudolife_memory import client_updates as clients, codex_connection, doctor_cli
from tests.test_codex_reapproval import ROOT, _clone
from tests.test_update_clients import _codex_plugin_config, _plugin_handler_keys, cli  # noqa: F401


@pytest.mark.parametrize("trust,enabled,state", [
    ("modified", True, "needs-approval"), ("untrusted", True, "needs-approval"),
    ("trusted", True, "current"), ("trusted", False, "disabled"),
    ("future-state", True, "unknown"),
])
@pytest.mark.parametrize("global_hashes", [True, False])
def test_doctor_names_the_fix_for_matching_but_unapproved_handlers(cli, monkeypatch, trust, enabled, state, global_hashes):
    _clone(cli)
    if not global_hashes:
        _codex_plugin_config(cli, approved=[])
    calls = []

    class Runtime:
        def rpc(self, method, params):
            calls.append(method)
            return {"data": [{"errors": [], "hooks": [{
                "key": key, "enabled": enabled, "trustStatus": trust}
                for key in _plugin_handler_keys()] + [{
                    "key": "another-plugin:hooks/hooks.json:session_start:0:0",
                    "enabled": True, "trustStatus": "modified"}]}]}

    @contextmanager
    def runtime(*args, **kwargs):
        yield Runtime()

    monkeypatch.setattr(codex_connection, "codex", runtime)
    monkeypatch.setattr(clients, "codex_executable", lambda home: "fixture-codex")
    original = clients.check_codex_hooks
    monkeypatch.setattr(clients, "check_codex_hooks", lambda repo, **kwargs:
                        original(ROOT, **kwargs))
    line = doctor_cli._codex_hooks_line({})
    expected = {
        "current": "current",
        "needs-approval": ("needs-approval: Run python ops/setup-codex-hooks.py "
                           "--source plugin --trust ask, then start a new session."),
        "disabled": ("disabled: If you want these hooks enabled, enable the Pseudolife "
                     "handlers in Codex /hooks, then start a new session."),
        "unknown": "current" if global_hashes else "needs-approval",
    }
    assert line == expected[state]
    assert calls == ["hooks/list"]


@pytest.mark.parametrize("mutation", ["duplicate", "extra"])
def test_same_plugin_runtime_inventory_must_be_exact_and_duplicate_free(cli, monkeypatch, mutation):
    directory = _clone(cli) / "hooks"
    hooks = [{"key": key, "enabled": True, "trustStatus": "trusted"}
             for key in _plugin_handler_keys()]
    extra = dict(hooks[0])
    if mutation == "extra":
        extra["key"] = clients.PLUGIN_ID + ":hooks/hooks.json:session_start:99:0"
        extra["trustStatus"] = "modified"
    hooks.append(extra)

    class Runtime:
        def rpc(self, method, params):
            assert method == "hooks/list"
            return {"data": [{"errors": [], "hooks": hooks}]}

    @contextmanager
    def runtime(*args, **kwargs):
        yield Runtime()

    monkeypatch.setattr(codex_connection, "codex", runtime)
    monkeypatch.setattr(clients, "codex_executable", lambda home: "fixture-codex")
    assert clients._codex_runtime_hook_status(cli.home / "codex", directory) == "unknown"


@pytest.mark.parametrize("failure", ["missing-executable", "rpc-error", "not-loaded"])
def test_unavailable_or_unloaded_runtime_does_not_offer_approval(cli, monkeypatch, failure):
    _clone(cli)

    class Runtime:
        def rpc(self, method, params):
            if failure == "rpc-error":
                raise RuntimeError("fixture runtime unavailable")
            return {"data": [{"errors": [], "hooks": []}]}

    @contextmanager
    def runtime(*args, **kwargs):
        yield Runtime()

    monkeypatch.setattr(codex_connection, "codex", runtime)
    monkeypatch.setattr(clients, "codex_executable", lambda home:
                        None if failure == "missing-executable" else "fixture-codex")
    result = clients.check_codex_hooks(ROOT, verify_runtime=True)
    if failure == "not-loaded":
        assert result["state"] == "not-loaded"
        assert result["detail"] == "Codex did not load the complete Pseudolife plugin hook set."
        assert result["recovery"] == (
            "Check that the Pseudolife plugin is installed and enabled in Codex's plugin "
            "manager, then start a new session.")
    else:
        assert result["state"] == "current" and "recovery" not in result
