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
