"""A completed real-bank exchange may carry only the named readiness notice."""
import base64
import copy
from contextlib import contextmanager
import json
import sys

import pytest

from evals.rust_port.phase1_receipts import receipt_status
from evals.rust_port.stdio_judge import (READINESS_WAIT_RULE, StdioPolicy, judge_with_evidence,
                                        readiness_wait_evidence)


# Exact 172-byte candidate stderr retained from the Windows parity failure.
RETAINED_NOTICE = (
    '[shim] no daemon at http://127.0.0.1:55926 and PSEUDOLIFE_MCP_NO_SPAWN is set '
    '— waiting up to 5s for it instead of spawning a fallback (Docker may still be starting)...\r\n'
).encode('utf-8')


def transcript(stderr=b'', *, arm='oracle'):
    return {'stdout_frames_b64': [base64.b64encode(b'{"id":1}\n').decode()],
            'stderr_b64': base64.b64encode(stderr).decode(), 'exit_code': 0,
            'era': '2025-11-25', 'arm': arm, 'capture_kind': 'process', 'pid': 123,
            'captured_at_utc': '2026-10-06T18:21:09.932179Z'}


def test_retained_notice_is_normalized_only_after_matching_success():
    left, right = transcript(), transcript(RETAINED_NOTICE, arm='candidate')
    before = copy.deepcopy((left, right))
    assert len(RETAINED_NOTICE) == 172
    assert [row['path'] for row in judge_with_evidence(left, right, StdioPolicy())] == ['/stderr']
    assert judge_with_evidence(left, right, StdioPolicy(readiness_wait_notice=True)) == []
    assert (left, right) == before


@pytest.mark.parametrize('stderr', [RETAINED_NOTICE, RETAINED_NOTICE.replace(b'\r\n', b'\n')])
@pytest.mark.parametrize('notice_arm', ['oracle', 'candidate'])
def test_named_rule_retains_exact_raw_bytes_in_both_arms(stderr, notice_arm):
    left = transcript(stderr if notice_arm == 'oracle' else b'')
    right = transcript(stderr if notice_arm == 'candidate' else b'', arm='candidate')
    policy = StdioPolicy(readiness_wait_notice=True)
    events = readiness_wait_evidence(left, right, policy)
    assert judge_with_evidence(left, right, policy) == []
    assert len(events) == 1 and events[0]['rule'] == READINESS_WAIT_RULE
    assert events[0]['path'] == '/stderr'
    for arm, original in [('oracle', left), ('candidate', right)]:
        retained = events[0]['stderr_evidence'][arm]
        assert retained['stderr_b64'] == original['stderr_b64']
        assert retained['pid'] == original['pid']
        assert retained['captured_at_utc'] == original['captured_at_utc']
        assert retained['truncated'] is False
    assert receipt_status({'differences': [], 'normalizations_applied': events,
                           'coverage_complete': True}) == 'passed'


@pytest.mark.parametrize('stderr', [
    RETAINED_NOTICE + b'\n', b'\n' + RETAINED_NOTICE, RETAINED_NOTICE + b'other log\n',
    b'other log\n' + RETAINED_NOTICE, RETAINED_NOTICE * 2, RETAINED_NOTICE.rstrip(b'\r\n'),
    RETAINED_NOTICE.replace(b'5s', b'6s'), RETAINED_NOTICE.replace(b'NO_SPAWN', b'SPAWN'),
    RETAINED_NOTICE.replace(b'http:', b'https:'), RETAINED_NOTICE.replace(b'127.0.0.1', b'localhost'),
    RETAINED_NOTICE.replace(b'55926', b'0'), RETAINED_NOTICE.replace(b'55926', b'65536'),
    RETAINED_NOTICE.replace(b'55926', b'055926'), RETAINED_NOTICE.replace(b'55926', b'123456'),
    RETAINED_NOTICE.replace(b'...', b'....'), b'unrelated diagnostic\n',
])
def test_partial_or_additional_notice_content_remains_a_difference(stderr):
    left, right = transcript(), transcript(stderr, arm='candidate')
    policy = StdioPolicy(readiness_wait_notice=True)
    assert '/stderr' in [row['path'] for row in judge_with_evidence(left, right, policy)]
    assert readiness_wait_evidence(left, right, policy) == []


@pytest.mark.parametrize('mutation', ['spacing', 'payload', 'line-ending', 'extra-frame',
                                     'missing-frame', 'wrong-exit', 'both-fail', 'bool-exit',
                                     'timeout', 'transport', 'malformed-json', 'duplicate-json-key'])
def test_output_exit_and_boundary_failures_cannot_use_notice_normalization(mutation):
    left, right = transcript(), transcript(RETAINED_NOTICE, arm='candidate')
    if mutation in ('spacing', 'payload', 'line-ending', 'malformed-json', 'duplicate-json-key'):
        raw = {'spacing': b'{ "id":1}\n', 'payload': b'{"id":2}\n',
               'line-ending': b'{"id":1}\r\n', 'malformed-json': b'{\n',
               'duplicate-json-key': b'{"id":1,"id":1}\n'}[mutation]
        right['stdout_frames_b64'] = [base64.b64encode(raw).decode()]
    elif mutation == 'extra-frame':
        right['stdout_frames_b64'] *= 2
    elif mutation == 'missing-frame':
        left['stdout_frames_b64'] = right['stdout_frames_b64'] = []
    elif mutation in ('wrong-exit', 'both-fail', 'bool-exit'):
        right['exit_code'] = False if mutation == 'bool-exit' else 1
        if mutation == 'both-fail':
            left['exit_code'] = 1
    else:
        right['boundary_error'] = 'Empty' if mutation == 'timeout' else 'ConnectionError'
    policy = StdioPolicy(readiness_wait_notice=True)
    assert judge_with_evidence(left, right, policy)
    assert readiness_wait_evidence(left, right, policy) == []


def test_existing_source_lf_rule_still_precedes_notice_rule():
    left, right = transcript(), transcript(RETAINED_NOTICE, arm='candidate')
    for side, raw in [(left, b'{"result":{"instructions":"a\\r\\nb"}}\n'),
                      (right, b'{"result":{"instructions":"a\\nb"}}\n')]:
        side['stdout_frames_b64'] = [base64.b64encode(raw).decode()]
    assert judge_with_evidence(left, right, StdioPolicy(readiness_wait_notice=True)) == []


@pytest.mark.parametrize('field', ['pid', 'captured_at_utc', 'capture_kind', 'era'])
def test_missing_provenance_refuses_normalization(field):
    left, right = transcript(), transcript(RETAINED_NOTICE, arm='candidate')
    del right[field]
    policy = StdioPolicy(readiness_wait_notice=True)
    assert judge_with_evidence(left, right, policy)
    assert readiness_wait_evidence(left, right, policy) == []


@pytest.mark.parametrize('value', [None, '!!!'])
@pytest.mark.parametrize('arm', ['oracle', 'candidate'])
def test_missing_or_invalid_stderr_bytes_cannot_be_normalized(value, arm):
    left, right = transcript(), transcript(RETAINED_NOTICE, arm='candidate')
    side = left if arm == 'oracle' else right
    if value is None:
        del side['stderr_b64']
    else:
        side['stderr_b64'] = value
    policy = StdioPolicy(readiness_wait_notice=True)
    assert judge_with_evidence(left, right, policy)
    assert readiness_wait_evidence(left, right, policy) == []


@pytest.mark.parametrize('mutation', ['missing-arm', 'missing-bytes', 'bad-base64', 'wrong-length',
                                     'wrong-clock', 'missing-pid', 'synthetic', 'truncated'])
def test_accepted_notice_evidence_is_checked_at_receipt_status(mutation):
    events = readiness_wait_evidence(transcript(), transcript(RETAINED_NOTICE, arm='candidate'),
                                     StdioPolicy(readiness_wait_notice=True))
    evidence = events[0]['stderr_evidence']
    if mutation == 'missing-arm':
        del evidence['candidate']
    elif mutation == 'missing-bytes':
        del evidence['candidate']['stderr_b64']
    else:
        field, value = {'bad-base64': ('stderr_b64', '!!!'), 'wrong-length': ('byte_count', 173),
                        'wrong-clock': ('captured_at_utc', 'invented'), 'missing-pid': ('pid', None),
                        'synthetic': ('capture_kind', 'judge-sensitivity'),
                        'truncated': ('truncated', True)}[mutation]
        evidence['candidate'][field] = value
    assert receipt_status({'differences': [], 'normalizations_applied': events,
                           'coverage_complete': True}) == 'incomplete'


def test_allowlist_and_order_policies_cannot_broaden_the_named_rule():
    left, right = transcript(), transcript(RETAINED_NOTICE + b'allowed\n', arm='candidate')
    policy = StdioPolicy(readiness_wait_notice=True, stderr_allowlist=(b'allowed\n',))
    assert judge_with_evidence(left, right, policy)
    assert readiness_wait_evidence(left, right, policy) == []
    right = transcript(RETAINED_NOTICE, arm='candidate')
    for policy in (StdioPolicy(readiness_wait_notice=True, eof_orders=((1,),)),
                   StdioPolicy(readiness_wait_notice=True, non_eof_orders=((1,),))):
        assert judge_with_evidence(left, right, policy)
        assert readiness_wait_evidence(left, right, policy) == []


def test_identical_stderr_requires_no_normalization_event():
    for stderr in (b'', RETAINED_NOTICE, b'unrelated diagnostic\n'):
        left, right = transcript(stderr), transcript(stderr, arm='candidate')
        policy = StdioPolicy(readiness_wait_notice=True)
        assert judge_with_evidence(left, right, policy) == []
        assert readiness_wait_evidence(left, right, policy) == []


def test_actual_corpus_and_public_receipt_keep_raw_applied_evidence(tmp_path, monkeypatch):
    from evals.rust_baseline import common, daemon
    from evals.rust_port import full_bank, phase1, stdio_faults, stdio_process_controls, stdio_scenarios

    @contextmanager
    def disposable(*args, **kwargs):
        yield str(tmp_path)

    @contextmanager
    def launched(*args, **kwargs):
        yield None, 'http://127.0.0.1:55926', {}

    def observe(root, home, url, token, era, prefix, *, candidate):
        side = transcript(RETAINED_NOTICE if candidate else b'', arm='candidate' if candidate else 'oracle')
        side.update(era=era, case='real-bank', stdout=[
            {'id': 'list', 'result': {'tools': [{'description': 'contract'}]}},
            {'id': 'invalid', 'result': {'isError': True}}])
        side['stdout_frames_b64'] = [base64.b64encode((json.dumps(frame) + '\n').encode()).decode()
                                    for frame in side['stdout']]
        return side

    monkeypatch.setattr(common, 'lease_gate', lambda **kwargs: {})
    monkeypatch.setattr(daemon, 'disposable_database', disposable)
    monkeypatch.setattr(daemon, 'private_directory', disposable)
    monkeypatch.setattr(daemon, 'launched_daemon', launched)
    monkeypatch.setattr(full_bank, 'private_home_overrides', lambda private: {})
    monkeypatch.setattr(phase1, 'observe', observe)
    monkeypatch.setattr(phase1, 'require_phase1_source', lambda root: {})
    monkeypatch.setattr(phase1, 'runtime_metadata', lambda root: {})
    monkeypatch.setattr(phase1, 'candidate_identity', lambda *args: {})
    monkeypatch.setattr(phase1, 'candidate_bindings', lambda *args: {})
    monkeypatch.setattr(phase1, 'process_tests', lambda *args: {'passed': True})
    monkeypatch.setattr(phase1, 'eof', lambda *args: {'cells': [], 'differences': []})
    monkeypatch.setattr(stdio_faults, 'run', lambda *args: {'cells': [], 'differences': []})
    monkeypatch.setattr(stdio_scenarios, 'run', lambda *args: {
        'startup': {'cells': []}, 'concurrent': {'cells': []}, 'differences': []})
    monkeypatch.setattr(stdio_process_controls, 'run', lambda *args: {'controls': {}})
    monkeypatch.setattr(full_bank, 'full_corpus', lambda: {})
    monkeypatch.setattr(full_bank, 'run', lambda *args, **kwargs: ({}, {
        'status': 'passed', 'cases': [], 'differences': [], 'identity_proxy_validation': {},
        'oracle_source_check': {}, 'graded_controls': {}}))
    raw, public = tmp_path / 'raw.json', tmp_path / 'public.json'
    monkeypatch.setattr(sys, 'argv', ['phase1', '--oracle-root', str(tmp_path),
                                    '--out', str(raw), '--public-out', str(public)])
    assert phase1.main() == 0
    receipt, summary = json.loads(raw.read_text()), json.loads(public.read_text())
    assert receipt['status'] == summary['status'] == 'passed'
    assert READINESS_WAIT_RULE in summary['named_normalizations']
    assert receipt['normalizations_applied'] == summary['normalizations_applied']
    assert len(summary['normalizations_applied']) == 2
    assert 'arms' not in summary
    for left, right, event in zip(receipt['arms'][:2], receipt['arms'][2:], summary['normalizations_applied']):
        assert right['stderr_b64'] == base64.b64encode(RETAINED_NOTICE).decode()
        assert left['stdout_frames_b64'] == right['stdout_frames_b64']
        assert event['stderr_evidence']['oracle']['stderr_b64'] == left['stderr_b64']
        assert event['stderr_evidence']['candidate']['stderr_b64'] == right['stderr_b64']
