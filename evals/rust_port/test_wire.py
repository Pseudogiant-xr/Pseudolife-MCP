"""Content-Length retains all bytes outside exact authorized replacement spans."""
import copy
import json

import pytest

from .full_bank import Normalizer
from .harness import DuplicateJSONKey, Policy, compare, strict_json_loads
from .wire import normalize_content_length, replacements, validate_disjoint


def observed(raw, path='/timestamp', *, declared=None):
    original = strict_json_loads(raw)
    body = Normalizer().apply(original, {'times': {path: 'epoch'}})
    response = {'headers': {'content-type': 'application/json', 'content-length': str(len(raw) if declared is None else declared)},
                'body': body, 'framing': 'fixed'}
    normalize_content_length(response, raw, original)
    return response


def test_timestamp_width_only_changes_raw_length_and_adjustment():
    left = observed(b'{"timestamp":1790000000.1,"stable":"same"}')
    right = observed(b'{"timestamp":1790000000.123456,"stable":"same"}')
    assert left['content_length']['raw'] != right['content_length']['raw']
    assert left['content_length']['compared'] == right['content_length']['compared']
    assert compare(left, right, Policy()) == []


@pytest.mark.parametrize('raw,reason', [(b'{"timestamp":1790000000000.1}', 'epoch_unit'),
                                      (b'{"timestamp":1790000000}', 'type')])
def test_epoch_units_and_types_still_fail(raw, reason):
    diffs = compare(observed(b'{"timestamp":1790000000.1}'), observed(raw), Policy())
    assert reason in {d['reason'] for d in diffs}


def test_whitespace_outside_replaced_value_remains_in_compared_length():
    assert compare(observed(b'{"timestamp":1790000000.1}'), observed(b'{ "timestamp":1790000000.123}'), Policy())


@pytest.mark.parametrize('declared', [1, 999])
def test_wrong_or_truncated_declared_length_is_refused(declared):
    with pytest.raises(ValueError, match='entity bytes'):
        observed(b'{"timestamp":1790000000.1}', declared=declared)


def test_missing_header_does_not_receive_synthetic_length():
    left = observed(b'{"timestamp":1790000000.1}')
    right = copy.deepcopy(left)
    del right['headers']['content-length']
    del right['content_length']
    assert compare(left, right, Policy())


def test_unicode_and_repeated_timestamp_outside_declared_path_are_preserved():
    left = observed('{"label":"星🌟","timestamp":1790000000.1,"same":1790000000.1}'.encode())
    right = observed('{"label":"星🌟","timestamp":1790000000.123,"same":1790000000.1}'.encode())
    assert compare(left, right, Policy()) == []
    assert compare(left, observed('{"label":"星🌟","timestamp":1790000000.123,"same":1790000000.123}'.encode()), Policy())


def test_nested_json_escaping_adjusts_only_inner_tokens():
    results = []
    for timestamp in (1790000000.1, 1790000000.123456):
        inner = '{ "label":"星🌟", "timestamp":' + str(timestamp) + ' }'
        raw = json.dumps({'result': {'content': [{'type': 'text', 'text': inner}]}}, ensure_ascii=True).encode()
        original = strict_json_loads(raw)
        normalized = copy.deepcopy(original)
        normalized['result']['content'][0]['text'] = json.dumps(Normalizer().apply(strict_json_loads(inner),
            {'times': {'/timestamp': 'epoch'}}), ensure_ascii=False, separators=(',', ':'))
        response = {'headers': {'content-type': 'application/json', 'content-length': str(len(raw))}, 'body': normalized}
        normalize_content_length(response, raw, original)
        results.append(response)
    assert compare(*results, Policy()) == []


def test_duplicate_keys_and_overlapping_or_duplicate_spans_are_refused():
    with pytest.raises(DuplicateJSONKey): observed(b'{"timestamp":1790000000.1,"timestamp":1790000000.1}')
    for changes in ([(1, 3, 'a'), (1, 3, 'b')], [(1, 4, 'a'), (3, 6, 'b')]):
        with pytest.raises(ValueError, match='overlapping'): validate_disjoint(changes)


def test_non_authorized_value_replacement_cannot_adjust_length():
    with pytest.raises(ValueError, match='authorized'):
        replacements('{"stable":3}', {'stable': 3}, {'stable': 4})
