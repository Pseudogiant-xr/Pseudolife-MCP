"""Offline self-tests: the comparator reports every kind of difference,
each named rule rewrites only the span it validates, and the external-program
preflight refuses a lookup that leaves the home. No CLI runs here (the
preflight's child is the interpreter itself)."""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_harness import compare, core, normalize  # noqa: E402


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def obs(exit=0, stdout=b"", stderr=b"", files=None, window=None, **extra) -> dict:
    now = time.time()
    return {"home": "/tmp/h", "utc_offset": time.localtime(now).tm_gmtoff,
            "exit": exit, "stdout": b64(stdout), "stderr": b64(stderr),
            "files": {k: "file:" + b64(v) for k, v in (files or {}).items()},
            "window": window or [now - 1, now + 1], **extra}


def test_equal_observations_have_no_diff():
    assert compare.diff(obs(stdout=b"x"), obs(stdout=b"x"), ()) == []


def test_each_field_difference_is_reported():
    base = obs(stdout=b"a", stderr=b"e", files={"f": b"1"})
    assert compare.diff(base, obs(exit=1, stdout=b"a", stderr=b"e", files={"f": b"1"}), ())
    assert compare.diff(base, obs(stdout=b"b", stderr=b"e", files={"f": b"1"}), ())
    assert compare.diff(base, obs(stdout=b"a", stderr=b"E", files={"f": b"1"}), ())
    assert compare.diff(base, obs(stdout=b"a", stderr=b"e", files={"f": b"2"}), ())
    assert compare.diff(base, obs(stdout=b"a", stderr=b"e", files={}), ())
    assert compare.diff(base, obs(stdout=b"a", stderr=b"e", files={"f": b"1", "g": b""}), ())
    def req(target):
        return [{"method": "GET", "target": target, "headers": [], "body": ""}]
    assert compare.diff(dict(base, requests=req("/a")), dict(base, requests=req("/b")), ())
    assert compare.diff(dict(base, listener="valid"), dict(base, listener="never-armed"), ())
    assert compare.diff(dict(base, modes={"f": "0o600"}), dict(base, modes={"f": "0o644"}), ())
    assert compare.diff(dict(base, db={"t": [[1]]}), dict(base, db={"t": [[2]]}), ())


def test_home_path_is_tokenized_in_streams_and_files():
    a = obs(stderr=b"no file at /tmp/h/x", files={"log": b"/tmp/h/y"})
    b = dict(obs(stderr=b"no file at /other/x", files={"log": b"/other/y"}), home="/other")
    assert compare.diff(a, b, ()) == []


def _rang(clock: str, elapsed: int) -> bytes:
    return (f"wait-mail: the daemon rang for addressed mail at {clock} (rung urgent, "
            f"watermark 7, {elapsed} s after arming):\n").encode()


def test_mail_clock_accepts_each_arms_own_window_only():
    now = time.time()
    window = [now, now + 1]
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    stale = time.strftime("%H:%M:%S", time.localtime(now - 3600))
    a = obs(stderr=_rang(clock, 0), window=window,
            files={"d/ledger.log": f"{int(now)}\twait\tabcd\t7\t3\trung urgent\n".encode()})
    b = obs(stderr=_rang(clock, 1), window=window,
            files={"d/ledger.log": f"{int(now) + 1}\twait\tabcd\t7\t3\trung urgent\n".encode()})
    assert compare.diff(a, b, ("mail-clock",)) == []
    out_of_window = obs(stderr=_rang(stale, 0), window=window,
                        files={"d/ledger.log": b"12\twait\tabcd\t7\t3\trung urgent\n"})
    assert compare.diff(a, out_of_window, ("mail-clock",))
    # Elapsed longer than the window is not normalized.
    assert compare.diff(a, obs(stderr=_rang(clock, 99), window=window, files={
        "d/ledger.log": f"{int(now)}\twait\tabcd\t7\t3\trung urgent\n".encode()}),
        ("mail-clock",))
    # Other ledger columns stay exact.
    assert compare.diff(a, obs(stderr=_rang(clock, 0), window=window, files={
        "d/ledger.log": f"{int(now)}\twake\tabcd\t7\t3\trung urgent\n".encode()}),
        ("mail-clock",))


def test_mail_clock_uses_the_recording_hosts_offset():
    # A golden recorded at UTC+11 replays on a UTC host: its clock is judged
    # on the recording host's clock, not the replaying host's.
    now = time.time()
    window = [now, now + 1]
    eleven = 11 * 3600
    clock = time.strftime("%H:%M:%S", time.gmtime(now + eleven))
    golden = dict(obs(stderr=_rang(clock, 0), window=window), utc_offset=eleven)
    utc_clock = time.strftime("%H:%M:%S", time.gmtime(now))
    candidate = dict(obs(stderr=_rang(utc_clock, 0), window=window), utc_offset=0)
    assert compare.diff(golden, candidate, ("mail-clock",)) == []
    wrong = dict(golden, utc_offset=0)
    if clock != utc_clock:
        assert compare.diff(wrong, candidate, ("mail-clock",))


def test_requests_compare_every_field_by_case_insensitive_name_and_flag_repeats():
    def req(headers, target="/a?x=1"):
        return {"method": "GET", "target": target, "headers": headers, "body": ""}
    base = obs(requests=[req([["Host", "h"], ["User-Agent", "Python-urllib/3.11"],
                              ["Accept-Encoding", "identity"]])])
    same = obs(requests=[req([["user-agent", "Python-urllib/3.11"], ["host", "h"],
                              ["accept-encoding", "identity"]])])
    assert compare.diff(base, same, ()) == []
    added = obs(requests=[req([["Host", "h"], ["User-Agent", "Python-urllib/3.11"],
                               ["Accept-Encoding", "identity"], ["Accept", "*/*"]])])
    assert compare.diff(base, added, ())
    assert compare.diff(base, obs(requests=[req([["Host", "h"],
                                                 ["User-Agent", "Python-urllib/3.11"]])]), ())
    assert compare.diff(base, obs(requests=[req([["Host", "h"]], "/a?x=2")]), ())
    repeated = obs(requests=[req([["Host", "h"], ["User-Agent", "Python-urllib/3.11"],
                                  ["Authorization", "a"], ["authorization", "a"]])])
    assert compare.diff(base, repeated, ())


def test_shutdown_flush_maps_only_the_exact_trailer():
    line = b"wait-mail: could not write the mail to stdout (<x>); left it unshown.\r\n"
    trailer = (b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' "
               b"encoding='utf-8'>\r\nOSError: [Errno 22] Invalid argument\r\n")
    python = obs(exit=120, stderr=line + trailer)
    rust = obs(exit=2, stderr=line)
    assert compare.diff(python, rust, ("python-shutdown-flush",)) == []
    assert compare.diff(python, rust, ())
    # Exit 120 without the trailer, or the trailer with another exit, stays.
    assert compare.diff(obs(exit=120, stderr=line), rust, ("python-shutdown-flush",))
    assert compare.diff(obs(exit=1, stderr=line + trailer), rust, ("python-shutdown-flush",))
    assert compare.diff(obs(exit=120, stderr=line + trailer + b"more\n"), rust,
                        ("python-shutdown-flush",))


def test_rules_apply_to_copies_not_the_raw_observation():
    raw = obs(exit=120, stderr=b"x")
    normalize.apply(raw, ("python-shutdown-flush",), raw["home"])
    assert raw["exit"] == 120


def test_episode_title_minute_rewrites_only_an_in_window_minute():
    now = time.time()
    offset = time.localtime(now).tm_gmtoff

    def body(stamp):
        return {"method": "POST", "target": "/api/episode/start", "headers": [],
                "body": '{"session_key": "k", "title": "proj - %s"}' % stamp}
    inside = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + offset))
    later = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + offset + 7200))
    a = dict(obs(requests=[body(inside)]), utc_offset=offset)
    b = dict(obs(requests=[body(inside)]), utc_offset=offset)
    out = normalize.apply(a, ("episode-title-minute",), None)
    assert '"proj - <minute>"' in out["requests"][0]["body"]
    assert compare.diff(a, b, ("episode-title-minute",)) == []
    assert compare.diff(a, dict(obs(requests=[body(later)]), utc_offset=offset),
                        ("episode-title-minute",))
    renamed = {**body(inside), "body": body(inside)["body"].replace("proj", "other")}
    assert compare.diff(a, dict(obs(requests=[renamed]), utc_offset=offset),
                        ("episode-title-minute",))


def test_a_vacuous_arm_is_a_difference_even_when_both_match():
    quiet = dict(obs(), vacuous="no stdout where the case requires output")
    assert compare.diff(quiet, dict(quiet), ())
    assert compare.diff(obs(stdout=b"x"), dict(obs(stdout=b"x"), vacuous="v"), ())


# --- external programs: the preflight before any arm -------------------------

def _install(directory: Path, name: str) -> Path:
    """A fake program file (never run): what ``shutil.which`` would find."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".exe" if core.WINDOWS else name)
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


def _preflight(tmp_path: Path, programs=(), real_programs=(), env=None):
    home = tmp_path / "home"
    (home / "cwd").mkdir(parents=True, exist_ok=True)
    case = core.Case("c", [], env=env or {}, programs=programs, real_programs=real_programs)
    arm = core.Arm("python", home, home / "cwd")
    environment = core._environment(case, arm, core.Target("python", []))
    return core.check_programs(case, environment, arm.cwd, home, sys.executable)


def test_a_program_on_a_path_outside_the_home_is_refused(tmp_path):
    _install(tmp_path / "outside", "plcfake")
    with pytest.raises(core.ProgramLeak, match="plcfake"):
        _preflight(tmp_path, ("plcfake",), env={"PATH": str(tmp_path / "outside")})


@pytest.mark.skipif(not core.WINDOWS, reason="Program Files variables are Windows-only")
def test_an_empty_programw6432_is_refused(tmp_path):
    # 64-bit Windows derives a child's ProgramFiles from ProgramW6432: empty
    # both, and a default-location lookup falls back to the real C:\Program Files.
    with pytest.raises(core.ProgramLeak, match="ProgramW6432"):
        _preflight(tmp_path, ("plcfake",), env={"PATH": "{HOME}\\bin", "ProgramW6432": ""})


@pytest.mark.skipif(not core.WINDOWS, reason="Program Files variables are Windows-only")
def test_program_files_outside_the_home_is_refused(tmp_path):
    with pytest.raises(core.ProgramLeak, match="ProgramFiles"):
        _preflight(tmp_path, ("plcfake",), env={"PATH": "{HOME}\\bin",
                                                 "ProgramW6432": str(tmp_path / "pf")})


def test_a_program_inside_the_home_passes(tmp_path):
    _install(tmp_path / "home" / "bin", "plcfake")
    seen = _preflight(tmp_path, ("plcfake",), env={"PATH": "{HOME}" + os.sep + "bin"})
    assert Path(seen["which"]["plcfake"]).parent == tmp_path / "home" / "bin"


def test_real_programs_allows_the_named_program_only(tmp_path):
    outside = tmp_path / "outside"
    _install(outside, "plcfake-a")
    _install(outside, "plcfake-b")
    env = {"PATH": str(outside)}
    seen = _preflight(tmp_path, ("plcfake-a",), ("plcfake-a",), env=env)
    assert Path(seen["which"]["plcfake-a"]).parent == outside
    with pytest.raises(core.ProgramLeak, match="plcfake-b"):
        _preflight(tmp_path, ("plcfake-a", "plcfake-b"), ("plcfake-a",), env=env)


def test_one_child_per_environment_shape(tmp_path, monkeypatch):
    env = {"PATH": "{HOME}" + os.sep + "bin"}
    first = _preflight(tmp_path, ("plcfake",), env=env)

    def no_child(*_args, **_kwargs):
        raise AssertionError("a second child for the same environment")
    monkeypatch.setattr(core.subprocess, "run", no_child)
    assert _preflight(tmp_path, ("plcfake",), env=env) == first
    with pytest.raises(AssertionError, match="second child"):
        _preflight(tmp_path, ("plcfake",), env={"PATH": "{HOME}" + os.sep + "other"})


def test_run_arm_refuses_before_setup_and_before_the_arm(tmp_path):
    _install(tmp_path / "outside", "plcfake")
    ran = []
    case = core.Case("c", [], env={"PATH": str(tmp_path / "outside")}, programs=("plcfake",),
                     setup=lambda arm: ran.append("setup"))
    marker = tmp_path / "arm-ran"
    target = core.Target("python", [sys.executable, "-c", f"open({str(marker)!r}, 'w')"])
    with pytest.raises(core.ProgramLeak):
        core.run_arm(case, target, tmp_path / "root" / "h")
    assert ran == [] and not marker.exists()
    # The same case without the declaration runs (the target is the interpreter).
    case.programs = ()
    core.run_arm(case, target, tmp_path / "root" / "h")
    assert ran == ["setup"] and marker.exists()
