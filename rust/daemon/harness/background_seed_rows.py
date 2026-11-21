"""Captured background seeds from the frozen harness at 88787b0e2.

Capture with its offline ONNX fixture on disposable PostgreSQL, pin time.time
to 1700000000.0, UUIDs to successive integers and token_hex to repeating 01.
The shared bank initializer supplies schema, relations and writer epoch.
"""

import json


ROWS = json.loads(r'''{
    "pruning": {
        "dream_runs": [
            {"id": 1, "claims": 0, "pulled": 0, "status": "running", "tallies": {}, "extractor": "fixture", "writer_id": null, "started_at": 100, "finished_at": null, "cursor_after": null, "cursor_before": 0, "rolled_back_at": null},
            {"id": 2, "claims": 0, "pulled": 0, "status": "running", "tallies": {}, "extractor": "fixture", "writer_id": null, "started_at": 101, "finished_at": null, "cursor_after": null, "cursor_before": 0, "rolled_back_at": null},
            {"id": 3, "claims": 0, "pulled": 0, "status": "running", "tallies": {}, "extractor": "fixture", "writer_id": null, "started_at": 102, "finished_at": null, "cursor_after": null, "cursor_before": 0, "rolled_back_at": null},
            {"id": 4, "claims": 0, "pulled": 0, "status": "committed", "tallies": {}, "extractor": "fixture", "writer_id": null, "started_at": 103, "finished_at": 200, "cursor_after": 0, "cursor_before": 0, "rolled_back_at": null}
        ],
        "dream_run_slots": [
            {"at": 100, "id": 1, "op": null, "seq": 1, "kind": "scalar", "action": "noop", "entity": "fixture", "run_id": 1, "attribute": "state", "new_value": "ready", "prev_kind": null, "prev_value": null, "entity_norm": "fixture", "prev_status": null, "prev_support": null, "src_entry_id": null, "attribute_norm": "state", "prev_confidence": null, "chronicle_event_id": null},
            {"at": 100, "id": 2, "op": null, "seq": 1, "kind": "scalar", "action": "noop", "entity": "fixture", "run_id": 2, "attribute": "state", "new_value": "ready", "prev_kind": null, "prev_value": null, "entity_norm": "fixture", "prev_status": null, "prev_support": null, "src_entry_id": null, "attribute_norm": "state", "prev_confidence": null, "chronicle_event_id": null},
            {"at": 100, "id": 3, "op": null, "seq": 1, "kind": "scalar", "action": "noop", "entity": "fixture", "run_id": 3, "attribute": "state", "new_value": "ready", "prev_kind": null, "prev_value": null, "entity_norm": "fixture", "prev_status": null, "prev_support": null, "src_entry_id": null, "attribute_norm": "state", "prev_confidence": null, "chronicle_event_id": null},
            {"at": 100, "id": 4, "op": null, "seq": 1, "kind": "scalar", "action": "noop", "entity": "fixture", "run_id": 4, "attribute": "state", "new_value": "ready", "prev_kind": null, "prev_value": null, "entity_norm": "fixture", "prev_status": null, "prev_support": null, "src_entry_id": null, "attribute_norm": "state", "prev_confidence": null, "chronicle_event_id": null}
        ],
        "retrieval_events": [
            {"id": 1, "origin": "search", "params": null, "served": [], "created_at": 100, "episode_id": null, "query_text": "fixture", "session_id": null, "served_facts": null},
            {"id": 2, "origin": "search", "params": null, "served": [], "created_at": 9000000000000, "episode_id": null, "query_text": "fixture", "session_id": null, "served_facts": null}
        ],
        "lesson_search_events": [
            {"id": 1, "served": [], "created_at": 100, "episode_id": null, "query_text": "fixture", "session_id": null},
            {"id": 2, "served": [], "created_at": 9000000000000, "episode_id": null, "query_text": "fixture", "session_id": null}
        ]
    },
    "sessions": {
        "episodes": [
            {"id": "00000000000000000000000000000008", "hint": null, "title": "empty fixture", "ended_at": null, "parent_id": null, "started_at": 1700000000, "session_key": "empty", "closed_by_new_start": false},
            {"id": "0000000000000000000000000000000a", "hint": null, "title": "work fixture", "ended_at": null, "parent_id": null, "started_at": 1700000000, "session_key": "work", "closed_by_new_start": false},
            {"id": "0000000000000000000000000000000b", "hint": null, "title": "child", "ended_at": null, "parent_id": "0000000000000000000000000000000a", "started_at": 1700000000, "session_key": "work", "closed_by_new_start": false},
            {"id": "0000000000000000000000000000000d", "hint": null, "title": "closed history fixture", "ended_at": 1700000000, "parent_id": null, "started_at": 1700000000, "session_key": "history", "closed_by_new_start": false}
        ],
        "client_sessions": [
            {"ended_at": 1700000000, "principal": null, "end_reason": "end", "started_at": 1700000000, "episode_ids": ["0000000000000000000000000000000d"], "session_key": "history", "start_times": [1700000000.0], "policy_variant": null, "registered_via": "api"},
            {"ended_at": null, "principal": null, "end_reason": null, "started_at": 1700000000, "episode_ids": ["00000000000000000000000000000008"], "session_key": "empty", "start_times": [1700000000.0], "policy_variant": null, "registered_via": "api"},
            {"ended_at": null, "principal": null, "end_reason": null, "started_at": 1700000000, "episode_ids": ["0000000000000000000000000000000a"], "session_key": "work", "start_times": [1700000000.0], "policy_variant": null, "registered_via": "api"}
        ],
        "entries": [
            {"id": 1, "ts": 1700000000, "band": "flat", "tags": [], "text": "The background lifecycle fixture retains a nested session entry.", "slots": [], "source": "fixture", "surprise": 1, "authority": null, "embedding": "[0.8,-0.6,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]", "episode_id": "0000000000000000000000000000000a", "dream_state": "pending", "access_count": 0, "episode_title": "work fixture", "superseded_at": null, "reinforcements": 0, "last_logical_turn": null, "superseded_by_text": null, "distortion_tolerance": null, "explicit_reinforcements": 0}
        ],
        "cms_counters": [
            {"id": 1, "tier_hits": {"flat": 0}, "updated_at": 1700000000, "preset_name": "flat", "tier_queries": 0, "surprise_history": {"flat": [1.0, 0.0]}, "interaction_count": 2, "logical_turn_count": 0, "consolidation_events": []}
        ],
        "meta": [
            {"key": "cortex_dream_cursor", "value": 0.0},
            {"key": "cortex_supersession_log", "value": []},
            {"key": "curation_listing_spelling_v2", "value": {"at": 1700000000.0, "copied": 0}},
            {"key": "dream_ack_secret_v1", "value": "0101010101010101010101010101010101010101010101010101010101010101"},
            {"key": "session_digest_cursor", "value": {"ts": 0.0, "retry": {}, "regenerate": []}}
        ]
    }
}''')

SEQUENCE_CALLS = {
    "pruning": {"dream_run_slots": 4, "dream_runs": 4, "lesson_search_events": 2, "retrieval_events": 2},
    "sessions": {"entries": 2},
}
