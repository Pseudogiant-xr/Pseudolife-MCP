"""Admission and rejection for the explicitly named wait-mail policy cases."""
import base64
import copy
import os
import time

import pytest

from evals.rust_port.wait_mail_policy import compare_record


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def traceback_record(case_id="invalid-session-bytes"):
    terminal = b"UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcff' in position 4: surrogates not allowed\n"
    response = {"exit_code": 1, "stdout_b64": "", "stderr_b64": encoded(terminal),
                "post_files_b64": {"seed.dat": encoded(b"seed")}, "post_directories": {"digests": 16832}}
    oracle = copy.deepcopy(response)
    oracle["stderr_b64"] = encoded(b'Traceback (most recent call last):\n  File "oracle.py", line 1, in main\n    encode(session)\n' + terminal)
    return {"id": case_id, "oracle": {"response": oracle}, "candidate": {"response": response}}


@pytest.mark.parametrize("case_id", ["invalid-session-bytes", "inline-session-bytes", "environment-session-bytes",
                                    "invalid-multiple-session-bytes", "inline-unicode-session-bytes"])
def test_named_linux_tracebacks_preserve_terminal_and_state(case_id):
    record = traceback_record(case_id)
    before = copy.deepcopy(record)
    assert compare_record(record, "linux")["passed"]
    assert record == before
    assert not compare_record(record, "windows")["passed"]
    assert not compare_record({**record, "id": "unlisted-session"}, "linux")["passed"]
    for field, value in (("exit_code", 2), ("stdout_b64", encoded(b"extra")),
                         ("stderr_b64", encoded(b"UnicodeEncodeError: different\n")),
                         ("post_files_b64", {}), ("post_directories", {})):
        changed = copy.deepcopy(record)
        changed["candidate"]["response"][field] = value
        assert not compare_record(changed, "linux")["passed"], field


@pytest.mark.parametrize("extra", [b"warning\n", b"\n", b"Traceback (most recent call last):\nwarning\n"])
def test_traceback_policy_cannot_hide_other_stderr(extra):
    record = traceback_record()
    raw = base64.b64decode(record["oracle"]["response"]["stderr_b64"])
    record["oracle"]["response"]["stderr_b64"] = encoded(extra + raw)
    assert not compare_record(record, "linux")["passed"]


def suffix_record(platform):
    epoch = 1791242386
    clock = time.strftime("%H:%M:%S", time.localtime(epoch)).encode("ascii")
    newline = b"\r\r\n" if platform == "windows" else b"\n"
    error = b"[WinError 5] Access is denied" if platform == "windows" else b"[Errno 21] Is a directory"
    response = {"exit_code": 0, "stdout_b64": encoded(b"peer\n"),
                "stderr_b64": encoded(b"wait-mail: the daemon rang for addressed mail at " + clock
                    + b" (rung anyone, watermark 1, 0 s after arming):" + newline
                    + b"wait-mail: could not advance the .seen marker (" + error
                    + b": '/fixture/digests/.tmp-ab12_cd3.seen' -> '/fixture/digests/" + b"a" * 64
                    + b".seen'); the prompt hook or tool-result hint may show this mail again." + newline),
                "post_files_b64": {".pseudolife-mcp/digests/ledger.log": encoded(str(epoch).encode()
                    + b"\twait\taaaaaaaa\t1\t5\trung anyone" + (b"\r\n" if platform == "windows" else b"\n"))},
                "post_directories": {"digests/" + "a" * 64 + ".seen": 16832}}
    candidate = copy.deepcopy(response)
    candidate["stderr_b64"] = encoded(base64.b64decode(response["stderr_b64"]).replace(b"ab12_cd3", b"xy98_zz1"))
    return {"id": "seen-directory", "oracle": {"response": response, "execution": {"wall_window": [epoch, epoch + 0.5]}},
            "candidate": {"response": candidate, "execution": {"wall_window": [epoch, epoch + 0.5]}}}


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_named_seen_source_suffix_is_the_only_free_path_span(platform):
    record = suffix_record(platform)
    original = copy.deepcopy(record)
    checked = compare_record(record, platform)
    assert checked["passed"], checked
    assert record == original
    assert not compare_record({**record, "id": "other-directory"}, platform)["passed"]
    for old, new in [(b"xy98_zz1", b"Xy98_zz1"), (b"xy98_zz1", b"xy98_zz"),
                     (b"/fixture/digests/", b"/other/digests/"), (b".seen' ->", b".bad' ->"),
                     (b"denied", b"allowed"), (b"Is a directory", b"Not a directory"),
                     (b"again.", b"again!"), (b"a" * 64, b"b" * 64)]:
        changed = copy.deepcopy(record)
        raw = base64.b64decode(changed["candidate"]["response"]["stderr_b64"])
        if old not in raw:
            continue
        changed["candidate"]["response"]["stderr_b64"] = encoded(raw.replace(old, new))
        assert not compare_record(changed, platform)["passed"], (old, new)
    for field in ("post_files_b64", "post_directories", "stdout_b64", "exit_code"):
        changed = copy.deepcopy(record)
        changed["candidate"]["response"][field] = {} if field.startswith("post_") else 2 if field == "exit_code" else encoded(b"different")
        assert not compare_record(changed, platform)["passed"], field


def test_clocks_need_own_window_and_unknown_case_ids_remain_exact():
    record = suffix_record("linux")
    record["candidate"]["execution"] = {}
    assert not compare_record(record, "linux")["passed"]
    record = suffix_record("linux")
    record["candidate"]["execution"]["wall_window"] = [1791242388, 1791242388.5]
    assert not compare_record(record, "linux")["passed"]


@pytest.mark.parametrize("traceback", [False, True])
def test_pair_lane_uses_scoped_policy_and_rejects_candidate_mutations(monkeypatch, traceback):
    from evals.rust_port import cli_process
    platform = "windows" if os.name == "nt" else "linux"
    if traceback and platform == "windows":
        monkeypatch.setattr(cli_process.sys, "platform", "linux")
        monkeypatch.setattr(cli_process.os, "name", "posix")
    record = traceback_record() if traceback else suffix_record(platform)
    commands = {arm: [arm] for arm in ("oracle", "candidate")}
    spec = [{"id": record["id"], "mode": "wait-mail"}]
    def captured(case, command, commands, **kwargs):
        return {"request": case, "environment": {"fixture": "same"}, "pre_files_b64": {},
                **copy.deepcopy(record[command[0]])}
    monkeypatch.setattr(cli_process, "observe", captured)
    result = cli_process.paired_cases(spec, commands, root=None, home=None, url=None)
    assert result["passed"], result
    assert result["records"][0]["policy_instances"]
    assert len(result["candidate_output_controls"]) == 4
    assert all(cell["rejected"] for cell in result["candidate_output_controls"])
