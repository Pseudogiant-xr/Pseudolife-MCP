"""Generated-name controls preserve source, prefix, identity relation and wire bytes."""
import copy
from contextlib import contextmanager
import json
import tempfile

import pytest

from . import full_bank
from .harness import Policy, compare
from .registration_policy import POLICY_NAME, generated_name
from .wire import normalize_content_length, replacements


def registration(actor="sender", digit="a", **changes):
    label = "synthetic-" + actor
    return {"agent_id": digit * 32, "credential": "s" * 43,
            "label": label, "name": label + " " + digit * 8, "name_source": "",
            "created_at": 10.0, "last_activity": 10.0, **changes}


@pytest.mark.parametrize("actor", ["sender", "recipient"])
def test_only_eight_generated_characters_are_free(actor):
    first, second = registration(actor, "a"), registration(actor, "b")
    before = copy.deepcopy(first)
    left = full_bank.normalize_payload(full_bank.Normalizer(), first, "register-" + actor)
    right = full_bank.normalize_payload(full_bank.Normalizer(), second, "register-" + actor)
    assert not compare(left, right, Policy())
    assert left["name"] == first["label"] + " " + f"<{actor}.agent_id-prefix8>"
    assert first == before
    assert generated_name(first, actor)["raw_name"] == before["name"]


@pytest.mark.parametrize("changes", [
    {"name": "synthetic-sender bbbbbbbb"}, {"name": "synthetic-sender aaaaaaa"},
    {"name": "synthetic-sender aaaaaaaaa"}, {"name": "synthetic-sender  aaaaaaaa"},
    {"name": "wrong-label aaaaaaaa"}, {"name": "synthetic-sender AAAAAAAA"},
    {"name": "synthetic-sender aaaaaaaa\n"}, {"name": None},
    {"agent_id": "a" * 31}, {"agent_id": "A" * 32}, {"agent_id": "g" * 32},
    {"label": None}, {"label": ""}, {"name_source": None},
])
def test_invalid_relation_is_rejected_before_tokenization(changes):
    with pytest.raises(ValueError):
        full_bank.normalize_payload(full_bank.Normalizer(), registration(**changes), "register-sender")


@pytest.mark.parametrize("field", ["name", "name_source"])
def test_partial_naming_shape_is_rejected(field):
    payload = registration()
    del payload[field]
    with pytest.raises(ValueError):
        full_bank.normalize_payload(full_bank.Normalizer(), payload, "register-sender")


def test_explicit_name_source_and_other_operations_remain_exact():
    first, second = registration(name_source="agent"), registration("sender", "b", name_source="agent")
    left = full_bank.normalize_payload(full_bank.Normalizer(), first, "register-sender")
    right = full_bank.normalize_payload(full_bank.Normalizer(), second, "register-sender")
    assert {difference["path"] for difference in compare(left, right, Policy())} == {"/name"}
    assert full_bank.normalize_payload(full_bank.Normalizer(), first, "unrelated") == first


def test_changed_label_and_other_fields_still_fail_parity():
    first = registration()
    second = registration("sender", "b", label="changed", name="changed bbbbbbbb", task="changed")
    left = full_bank.normalize_payload(full_bank.Normalizer(), first, "register-sender")
    right = full_bank.normalize_payload(full_bank.Normalizer(), second, "register-sender")
    assert {difference["path"] for difference in compare(left, right, Policy())} == {"/label", "/name", "/task"}


def test_wire_adjustment_preserves_prefix_escaping_and_all_surrounding_bytes():
    payload = registration(label='synthetic "\u03bb"', name='synthetic "\u03bb" aaaaaaaa')
    raw = json.dumps(payload, ensure_ascii=True, indent=2).replace(' aaaaaaaa"', ' ' + "\\u0061" * 8 + '"').encode()
    original = json.loads(raw)
    normalized = full_bank.normalize_payload(full_bank.Normalizer(), original, "register-sender")
    changes = replacements(raw.decode(), original, normalized)
    suffix_changes = [(start, end, text) for start, end, text in changes if "prefix8" in text]
    assert len(suffix_changes) == 1
    start, end, text = suffix_changes[0]
    assert raw.decode()[start:end] == "\\u0061" * 8
    assert text == "<sender.agent_id-prefix8>"
    assert '\\u03bb' in raw.decode()[:start]
    response = {"headers": {"content-type": "application/json", "content-length": str(len(raw))},
                "body": normalized}
    normalize_content_length(response, raw, original)
    assert response["content_length"]["compared"] == len(raw) + sum(
        len(text.encode()) - len(raw.decode()[start:end].encode()) for start, end, text in changes)
    with pytest.raises(ValueError):
        replacements(raw.decode(), original, {**normalized, "name": "wrong <sender.agent_id-prefix8>"})


def test_raw_policy_instances_survive_all_three_generic_arms(tmp_path, monkeypatch):
    from evals.rust_baseline import common, daemon
    from . import controls
    observed = []

    @contextmanager
    def database():
        yield "synthetic-disposable-bank"

    @contextmanager
    def launcher(dsn, private, *, env_extra, source_root):
        yield None, "http://127.0.0.1:1", {}

    @contextmanager
    def proxy(url, mutation):
        assert mutation == "identity"
        yield url, {"mutations": 0}

    class Client:
        def __init__(self, *args, **kwargs):
            self.digit = "abc"[len(observed)]
            observed.append(self.digit)

        def execute(self, request, surface, runtime_headers):
            payload = registration(digit=self.digit)
            raw = json.dumps(payload).encode()
            return {"status": 200, "headers": {"content-type": "application/json",
                    "content-length": str(len(raw))}, "body": payload, "_wire_body": raw}

    monkeypatch.setattr(daemon, "disposable_database", database)
    monkeypatch.setattr(daemon, "private_directory", tempfile.TemporaryDirectory)
    monkeypatch.setattr(daemon, "launched_daemon", launcher)
    monkeypatch.setattr(common, "lease_gate", lambda stamp: {})
    monkeypatch.setattr(controls, "mutation_proxy", proxy)
    monkeypatch.setattr(controls, "capture_controls", lambda *args, **kwargs: {})
    monkeypatch.setattr(full_bank, "require_historical_source", lambda root: None)
    monkeypatch.setattr(full_bank, "environment_metadata", lambda root: {})
    monkeypatch.setattr(full_bank, "private_home_overrides", lambda private: {})
    monkeypatch.setattr(full_bank, "HttpClient", Client)
    corpus = full_bank.full_corpus()
    corpus["cases"] = [case for case in corpus["cases"] if case["id"] == "register-sender"]
    _, receipt = full_bank.run(corpus, "verified", validate_controls=True)
    assert receipt["status"] == "passed" and receipt["differences"] == []
    instances = receipt["policy_instances"]
    assert [instance["arm"] for instance in instances] == [0, 1, 2]
    assert [instance["raw_name"] for instance in instances] == ["synthetic-sender " + digit * 8 for digit in "abc"]
    assert all(instance["policy"] == POLICY_NAME and instance["case"] == "register-sender" for instance in instances)
    assert "s" * 43 not in json.dumps(instances)
