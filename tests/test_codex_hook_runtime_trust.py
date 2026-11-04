"""Matching scripts do not prove that Codex will execute their handlers."""
from contextlib import contextmanager

import pytest

from pseudolife_memory import client_updates as clients, codex_connection, doctor_cli
from tests.test_codex_reapproval import ROOT, _clone
from tests.test_update_clients import _plugin_handler_keys, cli  # noqa: F401


@pytest.mark.parametrize("trust,enabled,state", [
    ("modified", True, "needs-approval"), ("untrusted", True, "needs-approval"),
    ("trusted", True, "current"), ("trusted", False, "disabled"),
    ("future-state", True, "unknown"),
])
def test_doctor_names_the_fix_for_matching_but_unapproved_handlers(cli, monkeypatch, trust, enabled, state):
    _clone(cli)
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
    assert line == ("current" if state == "current" else
                    f"{state}: Run python ops/setup-codex-hooks.py "
                    "--source plugin --trust ask, then start a new session.")
    assert calls == ["hooks/list"]
