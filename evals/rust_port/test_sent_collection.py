"""Sent boundary collection admits owned controls and refuses other files."""
from types import SimpleNamespace

import pytest

from evals.rust_port.pytest_plugin import pytest_collection_finish


def session(*nodes):
    config = SimpleNamespace(_port_sent_prefix=["candidate"], getoption=lambda _: False)
    return SimpleNamespace(config=config,
                           items=[SimpleNamespace(nodeid=node) for node in nodes])


def test_application_key_nodes_have_a_process_adapter():
    pytest_collection_finish(session(*[
        "evals/rust_port/test_sent_jsonb_keys.py::test_canonical_send_application_keys[" + label + "]"
        for label in ("numeric-string", "text-string", "numeric-value", "nested", "bare-proof")
    ]))


@pytest.mark.parametrize("node", [
    "evals/rust_port/test_sent_jsonb_keys.py.extra::test_canonical_send_application_keys[numeric-string]",
    "evals/rust_port/test_sent_collection.py::test_application_key_nodes_have_a_process_adapter",
    "tests/test_maintainer_roles.py::test_role",
    "tests/test_fixture_contract.py::test_fixture",
])
def test_other_files_have_no_sent_process_adapter(node):
    with pytest.raises(pytest.UsageError, match="selected tests have no process adapter:"):
        pytest_collection_finish(session(node))


@pytest.mark.parametrize("fail_native", [False, True])
def test_sent_fixture_owns_contexts_until_finalizer(monkeypatch, tmp_path, fail_native):
    from contextlib import contextmanager
    from evals.rust_port import pytest_plugin, sent_http
    from evals.rust_baseline import daemon
    events, finalizers = [], []

    @contextmanager
    def database():
        events.append("bank-enter")
        try:
            yield "host=127.0.0.1 dbname=plbench_fixture"
        finally:
            events.append("bank-exit")

    @contextmanager
    def native(*args):
        events.append("native-enter")
        if fail_native:
            raise RuntimeError("synthetic setup failure")
        try:
            yield object(), object()
        finally:
            events.append("native-exit")

    monkeypatch.setattr(pytest_plugin, "sent_eligible", lambda node: True)
    monkeypatch.setattr(daemon, "disposable_database", database)
    monkeypatch.setattr(sent_http, "native_sent", native)
    monkeypatch.setattr(sent_http, "asgi_http_adapter", lambda client: object())
    module = SimpleNamespace(_app=None)
    fixtures = {"tmp_path": tmp_path, "monkeypatch": monkeypatch}
    request = SimpleNamespace(config=SimpleNamespace(_port_sent_prefix=["candidate"]),
                              node=SimpleNamespace(module=module),
                              getfixturevalue=fixtures.__getitem__, addfinalizer=finalizers.append)
    if fail_native:
        with pytest.raises(RuntimeError, match="synthetic setup failure"):
            pytest_plugin._port_selected_boundary.__wrapped__(request)
    else:
        assert pytest_plugin._port_selected_boundary.__wrapped__(request) is None
        assert module._app(None) is not None
    assert events == ["bank-enter", "native-enter"]
    assert len(finalizers) == 1
    finalizers[0]()
    assert events == ["bank-enter", "native-enter", *([] if fail_native else ["native-exit"]), "bank-exit"]
