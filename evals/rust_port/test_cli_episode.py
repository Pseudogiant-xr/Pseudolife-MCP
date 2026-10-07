"""Additive episode corpus checks; no binary routing of in-process Python tests."""
import base64
from .episode_corpus import cases


def test_episode_corpus_preserves_tail_arguments_and_raw_json_domain():
    rows = cases()
    assert len({row['id'] for row in rows}) == len(rows)
    assert {row['mode'] for row in rows} == {'episode-start', 'episode-end'}
    assert all(row['argv'] == [row['mode'], '--help', 'ignored'] for row in rows)
    assert all(row['normalizations'] == [] for row in rows)
    raw = [base64.b64decode(row['stdin_b64'], validate=True) for row in rows]
    assert b'{"session_id":"\\ud800"}' in raw
    assert b'{"session_id":NaN}' in raw
    assert b'{"session_id":"old","session_id":"new"}' in raw
