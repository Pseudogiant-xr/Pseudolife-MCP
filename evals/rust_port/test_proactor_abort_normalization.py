"""The oracle's closing CPython ProactorEventLoop self-pipe abort is the only stderr this rule drops."""
import base64
import copy

import pytest

from evals.rust_port.phase1_receipts import receipt_status
from evals.rust_port.stdio_judge import (PROACTOR_SELF_PIPE_ABORT_RULE, StdioPolicy, judge_with_evidence,
                                        proactor_abort_evidence, without_proactor_self_pipe_abort)

SHIM_NOTICE = (b"[shim] no answer from the memory daemon at http://127.0.0.1:51323.\r\n"
               b"  (PSEUDOLIFE_MCP_NO_SPAWN is set, so no fallback daemon was spawned.)\r\n")

# The exact block the Python shim printed at teardown on a self-hosted
# Windows worker (judges, down fault, run 41436144681, 2026-11-02).
ABORT = (
    b'Error on reading from the event loop self pipe\r\n'
    b'loop: <ProactorEventLoop running=True closed=False debug=False>\r\n'
    b'Traceback (most recent call last):\r\n'
    b'  File "C:\\actions-runner\\toolcache\\Python\\3.11.18\\x64\\Lib\\asyncio\\windows_events.py", line 471, in finish_recv\r\n'
    b'    return ov.getresult()\r\n'
    b'           ^^^^^^^^^^^^^^\r\n'
    b'OSError: [WinError 995] The I/O operation has been aborted because of either a thread exit or an application request\r\n'
    b'\r\n'
    b'During handling of the above exception, another exception occurred:\r\n'
    b'\r\n'
    b'Traceback (most recent call last):\r\n'
    b'  File "C:\\actions-runner\\toolcache\\Python\\3.11.18\\x64\\Lib\\asyncio\\proactor_events.py", line 792, in _loop_self_reading\r\n'
    b'    f.result()  # may raise\r\n'
    b'    ^^^^^^^^^^\r\n'
    b'  File "C:\\actions-runner\\toolcache\\Python\\3.11.18\\x64\\Lib\\asyncio\\windows_events.py", line 854, in _poll\r\n'
    b'    value = callback(transferred, key, ov)\r\n'
    b'            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\r\n'
    b'  File "C:\\actions-runner\\toolcache\\Python\\3.11.18\\x64\\Lib\\asyncio\\windows_events.py", line 475, in finish_recv\r\n'
    b'    raise ConnectionResetError(*exc.args)\r\n'
    b'ConnectionResetError: [WinError 995] The I/O operation has been aborted because of either a thread exit or an application request\r\n')


def transcript(stderr=b'', *, arm='oracle'):
    return {'stdout_frames_b64': [base64.b64encode(b'{"id":1}\n').decode()],
            'stderr_b64': base64.b64encode(stderr).decode(), 'exit_code': 0,
            'era': '2025-11-25', 'arm': arm, 'capture_kind': 'process', 'pid': 123,
            'captured_at_utc': '2026-11-02T13:46:21.942275Z'}


@pytest.mark.parametrize('block', [ABORT, ABORT.replace(b'\r\n', b'\n'),
                                   # Python 3.10 and earlier print no caret lines.
                                   b''.join(line for line in ABORT.splitlines(True) if not line.strip().startswith(b'^'))])
def test_the_oracles_closing_abort_block_is_dropped_and_retained_as_evidence(block):
    left, right = transcript(SHIM_NOTICE + block), transcript(SHIM_NOTICE, arm='candidate')
    before = copy.deepcopy((left, right))
    policy = StdioPolicy(oracle_proactor_abort=True)
    assert judge_with_evidence(left, right, policy) == []
    events = proactor_abort_evidence(left, right, policy)
    assert len(events) == 1 and events[0]['rule'] == PROACTOR_SELF_PIPE_ABORT_RULE
    for arm, original in (('oracle', left), ('candidate', right)):
        assert events[0]['stderr_evidence'][arm]['stderr_b64'] == original['stderr_b64']
    assert receipt_status({'differences': [], 'normalizations_applied': events,
                           'coverage_complete': True}) == 'passed'
    assert (left, right) == before


def test_the_block_alone_is_dropped():
    assert without_proactor_self_pipe_abort(ABORT) == (b'', True)


def test_the_rule_is_off_unless_a_policy_opts_in():
    left, right = transcript(SHIM_NOTICE + ABORT), transcript(SHIM_NOTICE, arm='candidate')
    policy = StdioPolicy()
    assert [row['path'] for row in judge_with_evidence(left, right, policy)] == ['/stderr']
    assert proactor_abort_evidence(left, right, policy) == []


def test_a_candidate_printing_the_block_still_differs():
    left, right = transcript(SHIM_NOTICE), transcript(SHIM_NOTICE + ABORT, arm='candidate')
    policy = StdioPolicy(oracle_proactor_abort=True)
    assert [row['path'] for row in judge_with_evidence(left, right, policy)] == ['/stderr']
    assert proactor_abort_evidence(left, right, policy) == []


_HEADER = b'Error on reading from the event loop self pipe\r\nloop: <ProactorEventLoop running=True closed=False debug=False>\r\n'
_BODY = ABORT[len(_HEADER):]
assert _HEADER + _BODY == ABORT


# Each candidate is what a looser rule would leave of the oracle's stderr, so
# the case fails only while the named part of the pattern holds.
@pytest.mark.parametrize('stderr, candidate', [
    (ABORT.replace(b'995', b'10054'), b''),                            # another Winsock error
    (ABORT.replace(b'OSError: [WinError 995]', b'OSError: [WinError 64]'), b''),
    (ABORT.replace(b'_loop_self_reading', b'_loop_reading'), b''),     # another proactor reader
    (ABORT.replace(b'in _poll\r\n', b'in _poll\r\n  File "x.py", line 1, in extra\r\n    pass\r\n'), b''),
    (ABORT + b'later output\r\n', b''),                                # not the closing block
    (ABORT + b'later output\r\n', b'later output\r\n'),                # anchored at the end
    (ABORT[:-2], b''),                                                 # truncated last line
    (b'prefix ' + ABORT, b'prefix '),                                  # not at a line start
    # The two header lines are required: another asyncio message or loop state.
    (b'Task exception was never retrieved\r\n' + _BODY, b'Task exception was never retrieved\r\n'),
    (_HEADER.replace(b'running=True closed=False', b'running=False closed=True') + _BODY,
     _HEADER.replace(b'running=True closed=False', b'running=False closed=True')),
    (_BODY, b''),
])
def test_any_other_traceback_is_still_a_difference(stderr, candidate):
    left, right = transcript(stderr), transcript(candidate, arm='candidate')
    policy = StdioPolicy(oracle_proactor_abort=True)
    assert [row['path'] for row in judge_with_evidence(left, right, policy)] == ['/stderr']
    assert proactor_abort_evidence(left, right, policy) == []


def test_the_fault_and_corpus_judges_opt_in_and_name_the_rule():
    import inspect
    from evals.rust_port import phase1, stdio_faults
    assert "StdioPolicy(oracle_proactor_abort=True)" in inspect.getsource(stdio_faults.run)
    corpus = inspect.getsource(phase1.corpus)
    assert "oracle_proactor_abort=True" in corpus and "PROACTOR_SELF_PIPE_ABORT_RULE" in corpus
