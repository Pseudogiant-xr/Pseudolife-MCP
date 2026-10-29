"""When each principal last authenticated, and which bearers the daemon
refused (schema v58): the in-memory recorder the gate feeds.

Two client machines once had no memory for eleven days after a token
rotation, and nothing could tell "this machine's token is dead" from "this
machine is switched off". The gate now notes every successful
authentication and every refused bearer in memory (a dict write, no I/O),
and the principals refresher writes the batch at most once a minute.
"""
from __future__ import annotations

import hashlib

import pytest

from pseudolife_memory import principal_activity as activity
from pseudolife_memory.principals import (
    DEFAULT_PRINCIPAL, SOURCE_ENVIRONMENT, SOURCE_OPEN, SOURCE_STORE,
)


class Clocks:
    def __init__(self):
        self.wall = 1_800_000_000.0
        self.mono = 1000.0

    def tick(self, seconds):
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def clocks():
    return Clocks()


@pytest.fixture
def recorder(clocks):
    return activity.ActivityRecorder(wall=lambda: clocks.wall, clock=lambda: clocks.mono)


def test_the_prefix_is_the_first_eight_hex_of_the_tokens_sha256():
    token = "fixture-refused-token"
    assert activity.bearer_hash_prefix(token) == hashlib.sha256(token.encode()).hexdigest()[:8]
    assert activity.HASH_PREFIX_LEN == 8


def test_the_presented_bearer_is_parsed_like_the_gate():
    assert activity.presented_bearer("Bearer abc") == "abc"
    assert activity.presented_bearer("bearer \tabc \t") == "abc"
    assert activity.presented_bearer("Basic abc") is None
    assert activity.presented_bearer("Bearer ") is None
    assert activity.presented_bearer(None) is None
    assert activity.presented_bearer("") is None


def test_a_header_decoded_as_latin1_hashes_the_bytes_the_client_sent():
    # The gate reads headers as latin-1, so a UTF-8 token arrives as its
    # latin-1 text; the prefix must match the client's own (doctor's) hash.
    token = "fixture-tökén"
    header = "Bearer " + token.encode("utf-8").decode("latin-1")
    assert activity.bearer_hash_prefix(activity.presented_bearer(header)) == \
        hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def test_nothing_pending_is_never_due(recorder):
    assert not recorder.due()
    assert recorder.take() is None


def test_the_first_activity_is_due_at_the_next_tick(recorder, clocks):
    recorder.seen("laptop")
    assert recorder.due()
    batch = recorder.take()
    assert batch.seen == {"laptop": clocks.wall}
    assert batch.rejected == {}


def test_writes_are_at_most_once_a_minute(recorder, clocks):
    recorder.seen("laptop")
    recorder.take()
    clocks.tick(10)
    recorder.seen("laptop")
    assert not recorder.due()
    clocks.tick(49.9)
    assert not recorder.due()
    clocks.tick(0.1)
    assert recorder.due()
    assert recorder.take().seen == {"laptop": clocks.wall - 50}


def test_repeat_successes_keep_only_the_latest_time(recorder, clocks):
    recorder.seen("laptop")
    clocks.tick(5)
    recorder.seen("laptop")
    recorder.seen("desk")
    assert recorder.take().seen == {"laptop": clocks.wall, "desk": clocks.wall}


def test_refusals_count_and_keep_first_and_last_times(recorder, clocks):
    first = clocks.wall
    recorder.rejected("ab12cd34")
    clocks.tick(3)
    recorder.rejected("ab12cd34")
    recorder.rejected("00ff00ff")
    batch = recorder.take()
    assert batch.rejected == {"ab12cd34": (2, first, clocks.wall),
                              "00ff00ff": (1, clocks.wall, clocks.wall)}


def test_a_failed_write_gives_the_batch_back_merged_with_newer_activity(recorder, clocks):
    first = clocks.wall
    recorder.seen("laptop")
    recorder.rejected("ab12cd34")
    batch = recorder.take()
    clocks.tick(2)
    recorder.seen("laptop")
    recorder.rejected("ab12cd34")
    recorder.give_back(batch)
    clocks.tick(60)
    again = recorder.take()
    assert again.seen == {"laptop": first + 2}
    assert again.rejected == {"ab12cd34": (2, first, first + 2)}


def test_distinct_refused_prefixes_are_capped_per_batch(clocks):
    recorder = activity.ActivityRecorder(wall=lambda: clocks.wall, clock=lambda: clocks.mono,
                                         max_rejected=3)
    for prefix in ("00000001", "00000002", "00000003", "00000004", "00000005"):
        recorder.rejected(prefix)
    recorder.rejected("00000001")    # a known prefix still counts
    batch = recorder.take()
    assert sorted(batch.rejected) == ["00000001", "00000002", "00000003"]
    assert batch.rejected["00000001"][0] == 2
    assert batch.dropped == 2


def test_the_gate_notes_environment_and_stored_successes_only(recorder, clocks):
    activity.note(recorder, "Bearer x", "laptop", SOURCE_STORE)
    activity.note(recorder, "Bearer y", "desk", SOURCE_ENVIRONMENT)
    activity.note(recorder, None, DEFAULT_PRINCIPAL, SOURCE_OPEN)
    batch = recorder.take()
    assert batch.seen == {"laptop": clocks.wall, "desk": clocks.wall}


def test_the_gate_notes_a_refused_bearer_by_prefix_never_by_value(recorder):
    activity.note(recorder, "Bearer fixture-refused-token", None, None)
    activity.note(recorder, None, None, None)              # no bearer: nothing to match
    activity.note(recorder, "Basic Zm9vOmJhcg==", None, None)
    batch = recorder.take()
    assert list(batch.rejected) == [activity.bearer_hash_prefix("fixture-refused-token")]
    assert "fixture-refused-token" not in repr(batch)


def test_the_log_line_names_prefixes_most_refused_first_and_never_a_token(clocks):
    batch = activity.Batch(rejected={f"0000000{i}": (i, 0.0, 0.0) for i in range(1, 8)}, dropped=4)
    line = activity.refusal_log_line(batch)
    assert line.startswith("refused 32 bearer token(s)")
    assert "00000007 x7, 00000006 x6, 00000005 x5, 00000004 x4, 00000003 x3 and 2 more" in line
    assert "plus 4 past the" in line
    assert activity.refusal_log_line(activity.Batch(seen={"laptop": 1.0})) is None


def test_no_recorder_is_a_no_op():
    activity.note(None, "Bearer x", None, None)
