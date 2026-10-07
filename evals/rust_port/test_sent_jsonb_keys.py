"""Application JSONB keys through canonical send and the whole sent HTTP read."""
import base64
import json
from contextlib import closing

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest

from evals.rust_baseline.daemon import disposable_database
from evals.rust_port.test_sent_http_process import CONFIG, TOKEN, oracle, prefix, seed  # noqa: F401
from evals.rust_port.test_sent_review_controls import raw_bank_snapshot
from evals.rust_port.sent_http import native_sent
from evals.rust_port.maintainer_sent import compare_response, observe_http


NUMBER_KEY = "$serde_json::private::Number"
KEY_CASES = [
    ("numeric-string", {"label": {NUMBER_KEY: "1"}}, {NUMBER_KEY: "1"}),
    ("text-string", {"label": {NUMBER_KEY: "text"}}, {NUMBER_KEY: "text"}),
    ("numeric-value", {"label": {NUMBER_KEY: 1}}, {NUMBER_KEY: 1}),
    ("nested", {"label": {"outer": [{NUMBER_KEY: "1"}, {NUMBER_KEY: "text"}, {NUMBER_KEY: 1}]}},
     {"outer": [{NUMBER_KEY: "1"}, {NUMBER_KEY: "text"}, {NUMBER_KEY: 1}]}),
    ("bare-proof", {NUMBER_KEY: "1"}, {NUMBER_KEY: {"child": [NUMBER_KEY]}}),
]


def canonical_key_proof(dsn, proof):
    """Actual pinned CoordinationStore.send permits and preserves ordinary keys."""
    from pseudolife_memory.storage.coordination import CoordinationConnection, CoordinationStore
    seed(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value JSONB)")
    with closing(CoordinationConnection(dsn)) as connection:
        store = CoordinationStore(connection, clock=lambda: 1000.0)
        sender = store.register("maintainer", label="maintainer")
        recipient = store.register("default", label="synthetic key fixture")
        receipt = store.send("maintainer", sender["agent_id"], sender["credential"],
                             to=recipient["agent_id"], text="synthetic JSONB key control",
                             request_id="synthetic-jsonb-key", maintainer_proof=proof)
    message = receipt["message_id"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        stored = conn.execute("SELECT maintainer_proof FROM coordination_messages WHERE message_id=%s", (message,)).fetchone()[0]
        assert stored == proof  # Canonical write path permits these ordinary keys.
    return stored


def paired_key_message(prefix, private, monkeypatch, proof, wake, *, strict=True):
    from pseudolife_memory.utils.config import load_config
    private.mkdir(parents=True, exist_ok=True)
    config = private / "oracle.yaml"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    request = {"headers": {"Authorization": "Bearer " + TOKEN}}
    captures = []
    for native in (False, True):
        with disposable_database() as generated:
            dsn = make_conninfo(generated, sslmode="disable")
            # Independent banks receive identical deterministic SQL fixtures;
            # canonical producer acceptance is proved in a separate owned bank.
            seed(dsn, proof=proof)
            message = "message-00"
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("UPDATE coordination_messages SET wake=%s WHERE message_id=%s", (Jsonb(wake), message))
            before = raw_bank_snapshot(dsn)
            if native:
                with native_sent(prefix, private / "native", dsn, configuration=json.dumps(CONFIG), token=TOKEN) as (client, process):
                    capture = observe_http(client, **request)
                assert process.owned_cleanup == {"process_stopped": True, "subtree_stopped": True}
            else:
                capture = oracle(dsn, [request], load_config(config), TOKEN, {})[0]
            assert raw_bank_snapshot(dsn) == before
            if strict or not native:
                assert capture["status"] == 200
                item = next(row for row in json.loads(base64.b64decode(capture["body_b64"]))["messages"] if row["message_id"] == message)
                assert item["label"] == proof.get("label") and item["wake"] == wake
            captures.append(capture)
    differences = compare_response(*captures)
    if strict:
        assert differences == []
    return {"expected": captures[0], "actual": captures[1], "differences": differences,
            "readiness": process.sent_readiness, "owned_cleanup": process.owned_cleanup,
            "read_banks_unchanged": True, "disposable_banks_dropped": True}


@pytest.mark.parametrize("label,proof,wake", KEY_CASES, ids=[item[0] for item in KEY_CASES])
def test_canonical_send_application_keys(prefix, tmp_path, monkeypatch, label, proof, wake):
    with disposable_database() as generated:
        dsn = make_conninfo(generated, sslmode="disable")
        assert canonical_key_proof(dsn, proof) == proof
    paired_key_message(prefix, tmp_path / label, monkeypatch, proof, wake)
