"""Self-tests for the board harness normalizers: each rule replaces only
what it is declared to, and a malformed or unmatched value stays visible."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from board.norm import Normalizer, Windows  # noqa: E402

A = "a" * 32
B = "b" * 32
M1 = "1" * 32
M2 = "2" * 32


def norm_with(steps):
    w = Windows()
    w.steps = steps
    return Normalizer(w)


def test_ids_are_ordinals_by_first_appearance_regardless_of_key_order():
    n1, n2 = norm_with([]), norm_with([])
    n1.learn({"z": B, "a": A})
    n2.learn({"a": A, "z": B})
    assert n1.value({"x": A, "y": B}) == n2.value({"y": B, "x": A}) == {"x": "<id0>", "y": "<id1>"}


def test_prefixes_and_cursors_map_to_their_id():
    n = norm_with([])
    n.learn(A)
    assert n.value(A[:10]) == "<id0:10>"
    assert n.value(f"{A}:7") == "<id0>:7"
    assert n.value("c" * 32) == "<id?>"


def test_times_need_their_declared_offset_inside_the_window():
    t = 1_790_000_000.0
    n = norm_with([(t, t + 0.5), (t + 1000, t + 1000.2)])
    assert n.value(t + 1000.1, "created_at") == "T1+0"
    assert n.value(t + 60.2, "lease_until") == "T0+60"
    assert n.value(t + 61.0, "lease_until").startswith("<unmatched")
    assert n.value(t + 0.2, "lease_until").startswith("<unmatched")


def test_digests_salts_credentials_are_shape_checked():
    n = norm_with([])
    assert n.value("f" * 64, "hash") == "<sha256>"
    assert n.value("f" * 63, "hash").startswith("<bad digest")
    assert n.value("ab" * 16, "body_salt") == "<salt>"
    assert n.value("x" * 43, "credential") == "<credential>"
    assert n.value("x" * 42, "credential").startswith("<bad credential")


def test_sorted_id_lists_compare_as_sets_but_must_be_sorted_raw():
    n = norm_with([])
    n.learn([B, A])
    assert n.value([A, B], "agent_ids") == n.value([A, B], "agent_ids")
    assert n.value([B, A], "agent_ids").startswith("<agent_ids not sorted")


def test_receipts_pair_new_ids_with_their_recipient():
    # Two arms whose recipients sort in opposite raw order still pair the
    # burst's message ids with the same recipient ordinals.
    arm1, arm2 = norm_with([]), norm_with([])
    arm1.learn([A, B])
    arm2.learn(["d" * 32, "c" * 32])
    r1 = {"receipts": [{"message_id": M1, "recipient_agent_id": A},
                       {"message_id": M2, "recipient_agent_id": B}]}
    r2 = {"receipts": [{"message_id": M2, "recipient_agent_id": "c" * 32},
                       {"message_id": M1, "recipient_agent_id": "d" * 32}]}
    arm1.learn(r1)
    arm2.learn(r2)
    assert arm1.value(r1) == arm2.value(r2)


def test_burst_seqs_relabel_by_recipient_ordinal():
    n = norm_with([])
    n.learn([B, A])  # B is <id0>, A is <id1>
    sends = [{"seq": 5, "agent_id": "s", "recipient_agent_id": A, "created_at": 1.0, "request_id": "r"},
             {"seq": 6, "agent_id": "s", "recipient_agent_id": B, "created_at": 1.0, "request_id": "r"}]
    n.refresh_seqs(sends)
    assert n.seq_map == {5: 6, 6: 5}
    assert n.value(5, "seq") == 6
    assert n.value(f"{A}:history:6") == "<id1>:history:5"
    assert not n.problems
    swapped = [dict(sends[0], recipient_agent_id=B), dict(sends[1], recipient_agent_id=A)]
    n.refresh_seqs(swapped)
    assert n.problems and "raw recipient order" in n.problems[0]


def test_hlc_maps_to_its_step():
    t = 1_790_000_000.0
    n = norm_with([(t, t + 0.5)])
    assert n.value("1790000000250:3", "hlc") == "H0:3"
    assert n.value("9999999999999:0", "hlc") == "<unmatched hlc>"
    assert n.value("abc", "hlc").startswith("<bad hlc")
