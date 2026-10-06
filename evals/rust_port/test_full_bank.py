import json

import pytest

from evals.rust_port.full_bank import Normalizer, full_corpus, normalize_payload, private_home_overrides


def test_bindings_are_bijective_and_credentials_never_recorded():
    normal = Normalizer()
    rules = {"captures": {"/body/agent_id": {"name": "sender.agent_id", "kind": "uuid"},
                           "/body/credential": {"name": "sender.credential", "kind": "credential"}}}
    raw = {"body": {"agent_id": "a" * 32, "credential": "s" * 43}}
    safe = normal.apply(raw, rules)
    assert "s" * 43 not in json.dumps(safe)
    assert safe["body"]["agent_id"] == "<sender.agent_id>"
    assert normal.resolve({"$ref": "sender.agent_id"}) == "a" * 32
    with pytest.raises(ValueError):
        normal.apply({"body": {"agent_id": "a" * 32}}, {"captures": {
            "/body/agent_id": {"name": "recipient.agent_id", "kind": "uuid"}}})


def test_reference_mismatch_is_not_normalized_away():
    normal = Normalizer()
    normal.apply({"id": "a" * 32}, {"captures": {"/id": {"name": "agent", "kind": "uuid"}}})
    with pytest.raises(ValueError):
        normal.apply({"id": "b" * 32}, {"references": {"/id": "agent"}})


def test_cursor_keeps_sequence_and_checks_mailbox():
    normal = Normalizer()
    normal.apply({"id": "a" * 32}, {"captures": {"/id": {"name": "agent", "kind": "uuid"}}})
    assert normal.apply({"after": "a" * 32 + ":2"}, {"cursors": {"/after": "agent"}}) == {
        "after": "<agent>:2"}
    with pytest.raises(ValueError):
        normal.apply({"after": "b" * 32 + ":2"}, {"cursors": {"/after": "agent"}})


def test_clock_types_presence_and_relative_constraints_are_load_bearing():
    normal = Normalizer()
    rules = {"times": {"/created": "epoch", "/expires": "epoch"},
             "constraints": [{"left": "/created", "op": "lt", "right": "/expires"}]}
    assert normal.apply({"created": 10.0, "expires": 20.0}, rules) == {
        "created": "<epoch:float:epoch-unit=other:magnitude=1>", "expires": "<epoch:float:epoch-unit=other:magnitude=1>"}
    for bad in ({"created": 30.0, "expires": 20.0}, {"created": "x", "expires": 20.0},
                {"created": 10.0}, {"created": float("nan"), "expires": 20.0}):
        with pytest.raises(ValueError):
            normal.apply(bad, rules)


def test_mcp_text_and_structured_content_share_captured_identity():
    normal = Normalizer()
    raw = {"result": {"content": [{"text": '{"id":"' + "a" * 32 + '"}'}],
                      "structuredContent": {"id": "a" * 32}}}
    rules = {"json_text_paths": ["/result/content/*/text"], "captures": {
        "/result/content/*/text/id": {"name": "id", "kind": "uuid"},
        "/result/structuredContent/id": {"name": "id", "kind": "uuid"}}}
    safe = normal.apply(raw, rules)
    assert json.loads(safe["result"]["content"][0]["text"])["id"] == "<id>"
    assert safe["result"]["structuredContent"]["id"] == "<id>"


def test_corpus_has_auth_errors_mutations_state_reads_and_ordered_mail():
    payload = full_corpus()
    ids = {c["id"] for c in payload["cases"]}
    assert {"search-unauthorized", "search-wrong-bearer", "store-empty", "fact-set", "fact-update",
            "fact-get", "fact-history", "send-first", "send-second", "receive-ordered", "ack-first",
            "receive-after-ack", "receive-wrong-identity"} <= ids
    assert not any("Authorization" in c["request"].get("headers", {}) for c in payload["cases"])


def fact(value, stamp, **extra):
    return {"value": value, "asserted_at": stamp, "last_confirmed": stamp,
            "tx_time": stamp, "valid_time": stamp, "superseded_at": None, **extra}


def test_fact_mutation_read_history_keep_same_clocks_and_strict_version_order():
    normal = Normalizer()
    amber = fact("amber", 10.0, action="inserted")
    violet = fact("violet", 20.0, action="superseded")
    normalize_payload(normal, amber, "fact-set")
    normalize_payload(normal, violet, "fact-update")
    normalize_payload(normal, {"record": fact("violet", 20.0)}, "fact-get")
    versions = [fact("amber", 10.0, superseded_at=20.0), fact("violet", 20.0)]
    safe = normalize_payload(normal, {"versions": versions}, "fact-history")
    assert safe["versions"][1]["asserted_at"] == "<fact.violet.asserted_at:float:epoch-unit=other:magnitude=1>"
    with pytest.raises(ValueError):
        normalize_payload(normal, {"record": fact("violet", 21.0)}, "fact-get")
    with pytest.raises(ValueError):
        normalize_payload(normal, {"versions": versions[::-1]}, "fact-history")


def test_mail_receipt_lifetime_and_acknowledgment_are_validated():
    normal = Normalizer()
    normal.capture("recipient.agent_id", "uuid", "a" * 32)
    sent = {"message_id": "b" * 32, "recipient_agent_id": "a" * 32, "state": "queued",
            "created_at": 10.0, "expires_at": 86410.0, "acknowledged_at": None}
    normalize_payload(normal, sent, "send-first")
    ack = {k: v for k, v in sent.items() if k != "recipient_agent_id"}
    normalize_payload(normal, {**ack, "state": "acknowledged", "acknowledged_at": 20.0}, "ack-first")
    with pytest.raises(ValueError):
        normalize_payload(normal, {**ack, "state": "acknowledged", "acknowledged_at": 9.0}, "ack-first")
    with pytest.raises(ValueError):
        normalize_payload(normal, {**sent, "expires_at": 11.0}, "send-second")


def test_home_is_private_and_only_offline_model_artifacts_are_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "model-cache"))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    overrides = private_home_overrides(tmp_path / "private")
    assert overrides["HOME"] == str(tmp_path / "private" / "home")
    assert overrides["HF_HOME"] != str(tmp_path / "model-cache")
    assert overrides["HF_HUB_CACHE"] == str(tmp_path / "model-cache" / "hub")
    assert set(overrides) == {"HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "HF_HOME", "HF_HUB_CACHE"}


@pytest.mark.parametrize("rules", [{"times": {"/value/*": "epoch"}},
                                   {"captures": {"/value": {"name": "id", "kind": "unknown"}}}])
def test_normalization_rejects_invalid_shapes_and_unknown_binding_kinds(rules):
    with pytest.raises(ValueError):
        Normalizer().apply({"value": "scalar"}, rules)
