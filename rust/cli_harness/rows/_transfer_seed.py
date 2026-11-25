"""The seed bank of tests/test_transfer_cli.py: one row (or a small chain) in
every exported table. The transfer and backup rows seed their source banks
with it; that test module imports the frozen package at load, so the rows
keep this copy instead of importing it.

Copied verbatim, by syntax tree, from tests/test_transfer_cli.py at 6ca817d70:
_DIM :47-47, _VEC :51-53, _seed_bank :97-335.
Below them, values computed by calling the frozen code once, named where they are defined.
The rows own this copy from now on: a later change to the test module's
seed does not reach them, and a change here is a deliberate fixture change
that says so in its pull request.
"""
from __future__ import annotations

import hashlib
import json


_DIM = 1024


_VEC = "[" + ",".join(
    ["0.1234567", "1e-08", "-3.4028235e+38", "42.42424242"]
    + ["0"] * (_DIM - 5) + ["1"]) + "]"


def _seed_bank(conn) -> None:
    """One row (or a small chain) in every exported table, exercising the
    tricky column types: vector, JSONB, timestamptz, HLC stamps, NULLs."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO episodes (id, title, hint, started_at, ended_at, "
        "closed_by_new_start, session_key, parent_id) "
        "VALUES ('ep-1', 'seed episode', NULL, 100.0, 200.0, FALSE, "
        "'sess-key-1', NULL)"
    )
    cur.execute(
        "INSERT INTO entries (band, text, embedding, surprise, ts, "
        "access_count, source, episode_id, episode_title, tags, slots, "
        "reinforcements, explicit_reinforcements) "
        "VALUES ('flat', 'seed entry one', %s::vector, 0.5, 150.0, 3, "
        "'seed-src', 'ep-1', 'seed episode', '[\"tag-a\"]'::jsonb, "
        "'[]'::jsonb, 2, 1)",
        (_VEC,),
    )
    # U+2028/U+0085/U+2029 are line breaks to str.split("\n") but NOT to
    # JSON or to a "\n"-delimited JSONL reader — text pasted from web pages
    # carries them routinely, and one such entry must not shatter a record.
    cur.execute(
        "INSERT INTO entries (band, text, embedding, ts, superseded_at, "
        "superseded_by_text) "
        "VALUES ('flat', %s, %s::vector, 160.0, 170.0, "
        "'replaced by entry one')",
        ("seed entry two pasted\u2028line\u0085sep\u2029end", _VEC),
    )
    reinstate_request = {
        "entry_id": 2,
        "operation_id": "11111111-2222-4333-8444-555555555555",
        "expected_text_sha256": hashlib.sha256(
            "seed entry two pasted\u2028line\u0085sep\u2029end".encode()
        ).hexdigest(),
        "expected_source_sha256": hashlib.sha256(b"").hexdigest(),
        "expected_superseded_at": 170.0,
        "expected_superseded_by_text_sha256": hashlib.sha256(
            b"replaced by entry one"
        ).hexdigest(),
        "evidence_packet_sha256": hashlib.sha256(
            b"synthetic transfer evidence"
        ).hexdigest(),
        "reviewer_ids": ["reviewer-a", "reviewer-b"],
        "reason": "preserve synthetic replay evidence",
        "decided_by": "synthetic-principal",
    }
    operation_id, request_sha256, request = (
        PostgresStorage._reinstatement_request(**reinstate_request)
    )
    cur.execute(
        "INSERT INTO entry_reinstatement_decisions "
        "(operation_id, entry_id, request_sha256, entry_text_sha256, "
        "entry_source_sha256, prior_superseded_at, "
        "prior_superseded_by_text, prior_superseded_by_text_sha256, "
        "evidence_packet_sha256, reviewer_ids, reason, decided_by, "
        "decided_at) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s::jsonb, %s, %s, 171.0)",
        (operation_id, 2, request_sha256,
         request["expected_text_sha256"],
         request["expected_source_sha256"], 170.0,
         "replaced by entry one",
         request["expected_superseded_by_text_sha256"],
         request["evidence_packet_sha256"],
         json.dumps(request["reviewer_ids"]), request["reason"],
         request["decided_by"]),
    )
    cur.execute(
        "INSERT INTO entities (canonical, display, etype, created_at) "
        "VALUES ('widget', 'Widget', 'artifact', 100.0), "
        "('gadget', 'Gadget', NULL, 101.0)"
    )
    cur.execute(
        "INSERT INTO entity_aliases (alias, entity_id) VALUES ('the widget', 1)"
    )
    # An inverse pair exercises the self-referential inverse_of FK, which
    # import must satisfy regardless of the order rows appear in the file.
    cur.execute(
        "INSERT INTO relations (name, description, transitive, builtin, "
        "created_at) VALUES "
        "('uses', 'src makes use of dst', FALSE, TRUE, 90.0), "
        "('hosts', 'src is the host/platform for dst', FALSE, TRUE, 90.0)"
    )
    cur.execute(
        "INSERT INTO relations (name, description, transitive, inverse_of, "
        "builtin, created_at) VALUES "
        "('runs-on', 'src executes on host/platform dst', FALSE, 'hosts', "
        "TRUE, 90.0)"
    )
    cur.execute(
        "INSERT INTO edges (src_id, relation, dst_id, confidence, origin, "
        "asserted_at, tx_time, valid_time, hlc_phys, hlc_logical, writer_id, "
        "session_id, version) "
        "VALUES (1, 'uses', 2, 0.9, 'agent', 110.0, 110.0, 110.0, "
        "1750000000000, 4, 'seed-writer', 'sess-1', 1)"
    )
    cur.execute("INSERT INTO edge_evidence (edge_id, entry_id) VALUES (1, 1)")
    cur.execute(
        "INSERT INTO edge_proposals (src_id, relation, dst_id, confidence, "
        "similarity, rationale, source, created_at, status) "
        "VALUES (2, 'uses', 1, 0.6, 0.8, 'looks related', 'deep-dream', "
        "120.0, 'pending')"
    )
    cur.execute(
        "INSERT INTO entity_proposals (kind, entity_id, into_id, score, "
        "reason, status, created_at, judge_verdict, judge_confidence) "
        "VALUES ('merge', 2, 1, 0.7, 'same thing?', 'pending', 121.0, "
        "'reject', 0.55)"
    )
    cur.execute(
        "INSERT INTO entity_kinds (entity_norm, kind, origin, confidence, "
        "decided_at) VALUES ('widget', 'artifact', 'dream', 0.8, 122.0)"
    )
    cur.execute(
        "INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at) "
        "VALUES ('gadget', 'widget', 123.0)"
    )
    # A superseded->current scalar chain, one member row, one contested row.
    cur.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, origin, support, provenance, asserted_at, "
        "last_confirmed, superseded_by_value, superseded_at, embedding, "
        "freshness_class, kind, tx_time, valid_time, hlc_phys, hlc_logical, "
        "writer_id, version) "
        "VALUES ('widget', 'color', 'widget', 'color', 'red', 'superseded', "
        "0.9, 'user', '[]'::jsonb, '[1]'::jsonb, 100.0, 100.0, 'blue', "
        "130.0, %s::vector, 'evergreen', 'scalar', 100.0, 100.0, "
        "1750000000001, 0, 'seed-writer', 1)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, origin, support, provenance, asserted_at, "
        "last_confirmed, supersedes_value, embedding, freshness_class, kind, "
        "stance, tx_time, valid_time, hlc_phys, hlc_logical, writer_id, "
        "version) "
        "VALUES ('widget', 'color', 'widget', 'color', 'blue', 'current', "
        "0.95, 'user', '[]'::jsonb, '[2]'::jsonb, 130.0, 130.0, 'red', "
        "%s::vector, 'evergreen', 'scalar', 'probably', 130.0, 130.0, "
        "1750000000002, 0, 'seed-writer', 2)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, support, provenance, asserted_at, "
        "last_confirmed, kind, value_norm, tx_time, valid_time, writer_id, "
        "version) "
        "VALUES ('widget', 'owners', 'widget', 'owners', 'alice', 'current', "
        "0.9, '[]'::jsonb, '[]'::jsonb, 131.0, 131.0, 'member', 'alice', "
        "131.0, 131.0, 'seed-writer', 1)"
    )
    cur.execute(
        "INSERT INTO world_facts (entity, attribute, entity_norm, "
        "attribute_norm, value, status, confidence, origin, support, "
        "provenance, asserted_at, last_confirmed, embedding, source_url, "
        "source_quote, retrieved_at, freshness_class, content_hash, "
        "tx_time, valid_time, writer_id, version) "
        "VALUES ('pgvector', 'latest-version', 'pgvector', 'latest-version', "
        "'0.8.6', 'current', 0.9, 'source', '[]'::jsonb, '[]'::jsonb, 140.0, "
        "140.0, %s::vector, 'https://example.com/rel', 'v0.8.6 released', "
        "140.0, 'volatile', 'abc123', 140.0, 140.0, 'seed-writer', 1)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, "
        "value, about, polarity, outcome, status, confidence, support, "
        "provenance, asserted_at, last_confirmed, embedding, tx_time, "
        "valid_time, writer_id, version) "
        "VALUES ('deploys', 'approach', 'deploys', 'approach', "
        "'verify live after deploy', 'ops/update.ps1', '+', 'success', "
        "'current', 0.8, '[]'::jsonb, '[]'::jsonb, 145.0, 145.0, %s::vector, "
        "145.0, 145.0, 'seed-writer', 1)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO outcome_signals (task, outcome, about, detail, "
        "episode_id, created_at, consumed_at) "
        "VALUES ('seed task', 'success', 'seeding', 'went fine', 'ep-1', "
        "150.0, NULL)"
    )
    cur.execute(
        "INSERT INTO communities (id, label, size, cohesion, computed_at) "
        "VALUES (7, 'widgets', 2, 0.5, 151.0)"
    )
    cur.execute(
        "INSERT INTO entity_communities (entity_id, community_id, "
        "computed_at) VALUES (1, 7, 151.0)"
    )
    cur.execute(
        "INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, "
        "created_at) VALUES ('widget', 'color', 1, 152.0), "
        "('widget', 'color', 2, 152.5)"
    )
    cur.execute(
        "INSERT INTO memory_trace_invalidations "
        "(entity_norm, attribute_norm, source_entry_id, invalidated_at, cause) "
        "VALUES ('widget', 'color', 2, 170.0, 'source_superseded')"
    )
    cur.execute(
        "INSERT INTO entity_sources (entity_id, source, count, origin, "
        "updated_at) VALUES (1, 'seed-src', 3, 'derived', 153.0)"
    )
    cur.execute(
        "INSERT INTO merge_decisions (proposal_id, entity_display, "
        "into_display, status, score, reason, decided_by, decided_at) "
        "VALUES (1, 'Gadget', 'Widget', 'rejected', 0.7, 'distinct', "
        "'user', 154.0)"
    )
    cur.execute(
        "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
        "recorded_at, actor, actor_norm, description, description_norm, "
        "episode, src_entry_id, hlc_phys, hlc_logical, writer_id) "
        "VALUES ('2026-08-30T12:00:00+00:00'::timestamptz, NULL, 155.0, "
        "'Widget', 'widget', 'shipped', 'shipped', 'ep-1', 1, "
        "1750000000003, 0, 'seed-writer')"
    )
    cur.execute(
        "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
        "recorded_at, actor, actor_norm, description, description_norm) "
        "VALUES (NULL, 'last spring', 156.0, 'Gadget', 'gadget', "
        "'was painted', 'was painted')"
    )
    # v57: an ingested document chunk, metadata and vector included.
    cur.execute(
        "INSERT INTO reference_chunks (id, source, chunk_index, text, "
        "embedding, metadata, ingested_at, created_at) "
        "VALUES ('0123456789abcdef0123456789abcdef', 'manual.md', 0, "
        "'Widgets ship in blue.', %s::vector, "
        "'{\"source\": \"manual.md\", \"chunk_index\": 0, "
        "\"timestamp\": 157.0}'::jsonb, 157.0, 150.0)",
        (_VEC,),
    )
    cur.execute(
        "INSERT INTO meta (key, value) VALUES "
        "('cortex_dream_cursor', '160.5'::jsonb), "
        "('active_session_pointer', '{\"session\": \"sess-1\"}'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    conn.commit()


# The one PostgresStorage._reinstatement_request call _seed_bank makes, recorded
# from the frozen storage at 6ca817d70: its arguments and its answer.
_REINSTATEMENT_REQUEST = {'entry_id': 2, 'operation_id': '11111111-2222-4333-8444-555555555555', 'expected_text_sha256': 'a5766a3d79d0359e3c6e9f8a7a9b565682e67354a47a980c9f03e3986143ea73', 'expected_source_sha256': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'expected_superseded_at': 170.0, 'expected_superseded_by_text_sha256': '590ae42df37a43a2eb3ee294cbb2e2f04ac8fe71442d35212901b704113dec9d', 'evidence_packet_sha256': '3d8aa706340315ad49e9f2b2af1d140b71f001a6f98410972d088f2d0fbaf2b7', 'reviewer_ids': ['reviewer-a', 'reviewer-b'], 'reason': 'preserve synthetic replay evidence', 'decided_by': 'synthetic-principal'}
_REINSTATEMENT_ANSWER = ('11111111-2222-4333-8444-555555555555', '71645ced50f1b1f76dbc529d31e144534ba45c0fc39592a5a5dd2e964fa1c52e', {'version': 1, 'entry_id': 2, 'operation_id': '11111111-2222-4333-8444-555555555555', 'expected_text_sha256': 'a5766a3d79d0359e3c6e9f8a7a9b565682e67354a47a980c9f03e3986143ea73', 'expected_source_sha256': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'expected_superseded_by_text_sha256': '590ae42df37a43a2eb3ee294cbb2e2f04ac8fe71442d35212901b704113dec9d', 'evidence_packet_sha256': '3d8aa706340315ad49e9f2b2af1d140b71f001a6f98410972d088f2d0fbaf2b7', 'expected_superseded_at': 170.0, 'reviewer_ids': ['reviewer-a', 'reviewer-b'], 'reason': 'preserve synthetic replay evidence', 'decided_by': 'synthetic-principal'})


class PostgresStorage:
    """The recorded answer of the frozen storage, for exactly that request."""

    @staticmethod
    def _reinstatement_request(**request):
        if request != _REINSTATEMENT_REQUEST:
            raise AssertionError("_seed_bank changed its reinstatement request")
        return _REINSTATEMENT_ANSWER
