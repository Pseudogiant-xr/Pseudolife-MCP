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


def test_transfer_default_name_normalizes_file_and_mode_keys_only_after_validation():
    from cli_harness.rows import transfer  # noqa: F401, PLC0415 - registers rules
    rule = ("transfer-default-name",)
    now = 1791591851

    def exported(moment, *, name=None, mode="0o600", content=b"archive", offset=0):
        name = name or time.strftime("pseudolife-export-%Y%m%d-%H%M%S.zip",
                                     time.gmtime(moment + offset))
        return dict(obs(stdout=f"Exported to {name}\n".encode(),
                        files={name: content}, window=[moment, moment + 0.1],
                        modes={name: mode}), utc_offset=offset)

    a, b = exported(now), exported(now + 1)
    assert compare.diff(a, b, rule) == []
    assert compare.diff(a, exported(now + 1, offset=11 * 3600), rule) == []
    assert compare.diff(a, exported(now + 1, mode="0o644"), rule)
    assert compare.diff(a, exported(now + 1, content=b"changed"), rule)
    valid_name = next(iter(a["files"]))
    for name in ("pseudolife-export-20261010-0324.zip",
                 "pseudolife-export-20261310-032411.zip",
                 "pseudolife-export-20261010-992411.zip",
                 "prefix-" + valid_name,
                 valid_name + ".extra",
                 "pseudolife-export-20000101-000000.zip"):
        bad = exported(now, name=name)
        normalized = normalize.apply(bad, rule, None)
        assert normalized["stdout"] == bad["stdout"]
        assert list(normalized["files"]) == [name]
        assert list(normalized["modes"]) == [name]
        assert compare.diff(a, bad, rule)
    # Renaming two keys to one must preserve the extra observed file/mode.
    duplicate = exported(now)
    other = time.strftime("pseudolife-export-%Y%m%d-%H%M%S.zip", time.gmtime(now + 1))
    duplicate["files"][other] = "file:" + b64(b"archive")
    duplicate["modes"][other] = "0o600"
    assert compare.diff(a, duplicate, rule)


def test_mail_expired_listener_after_renewal(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cli_harness.rows import mail
    token = "a" * 32
    path = tmp_path / f"{mail.KEY}.{token}.wait-armed"
    arm = SimpleNamespace(started=100.0)
    # LF bytes match the listener producer on both platforms.
    for observed, expiry in [(103.0, 101.0), (108.0, 106.0)]:
        path.write_bytes(f"{token}\n{expiry}\n".encode())
        monkeypatch.setattr(mail.time, "time", lambda: observed)
        assert mail.listener_shape(arm, path, 20.0) != "valid"


def test_mail_listener_clock_resolution_and_upper_bound(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cli_harness.rows import mail
    token = "a" * 32
    path = tmp_path / f"{mail.KEY}.{token}.wait-armed"
    arm = SimpleNamespace(started=100.0)
    monkeypatch.setattr(mail.time, "time", lambda: 108.0)
    for expiry, valid in [(107.5, True), (107.0, False), (128.0, True), (130.0, False),
                          (float("inf"), False), (float("nan"), False)]:
        path.write_bytes(f"{token}\n{expiry}\n".encode())
        assert (mail.listener_shape(arm, path, 20.0) == "valid") is valid


def test_mail_renewal_can_rewrite_identical_expiry_bytes(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from cli_harness.rows import mail
    token = "a" * 32
    path = tmp_path / f"{mail.KEY}.{token}.wait-armed"
    path.write_bytes(f"{token}\n128.0\n".encode())
    arm = SimpleNamespace(started=100.0, home=tmp_path, state={})
    ticks = iter([0.0, 1.0, 2.0, 20.0])
    monkeypatch.setattr(mail.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(mail.time, "time", lambda: 108.0)
    monkeypatch.setattr(mail.p, "digest_dir", lambda _home: tmp_path)
    def rewrite(_seconds):
        modified = path.stat().st_mtime_ns + 100_000_000
        os.utime(path, ns=(modified, modified))
    monkeypatch.setattr(mail.time, "sleep", rewrite)
    rings = []
    monkeypatch.setattr(mail.p, "write_ring", lambda *args: rings.append(args))
    mail.renewed_listener(arm, SimpleNamespace(poll=lambda: None))
    assert arm.state["listener"] == "valid"
    assert len(rings) == 1


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


def test_home_removal_waits_out_a_briefly_held_file(tmp_path):
    import threading
    from cli_harness import core
    home = tmp_path / "h"
    (home / "d").mkdir(parents=True)
    held = (home / "d" / "digest.txt").open("w")
    held.write("7\n")
    held.flush()
    threading.Timer(0.5, held.close).start()
    core._remove(home)
    assert not home.exists()


def test_home_removal_retries_injected_sharing_violations(tmp_path, monkeypatch):
    import os
    from cli_harness import core
    home = tmp_path / "h"
    (home / "d").mkdir(parents=True)
    (home / "d" / "digest.txt").write_text("7")
    real, calls = os.unlink, []

    def flaky(path, *args, **kwargs):
        calls.append(path)
        if len(calls) <= 2:
            raise PermissionError(13, "being used by another process")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", flaky)
    core._remove(home)
    assert not home.exists() and len(calls) >= 3


def test_home_removal_gives_up_after_its_deadline(tmp_path, monkeypatch):
    import os
    import pytest
    from cli_harness import core
    home = tmp_path / "h"
    home.mkdir()
    (home / "digest.txt").write_text("7")

    def stuck(path, *args, **kwargs):
        raise PermissionError(13, "being used by another process")
    monkeypatch.setattr(os, "unlink", stuck)
    monkeypatch.setattr(core, "_RETRY_SECONDS", 0.3)
    with pytest.raises(PermissionError):
        core._remove(home)


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


def test_record_scrubs_host_paths_from_oracle_streams(tmp_path):
    from cli_harness import runner  # noqa: PLC0415
    source = tmp_path / "checkout"
    python = Path(sys.executable)
    trace = (f'  File "{source / "pseudolife_memory" / "cli.py"}", line 1\n'
             f'  File "{python.parent / "Lib" / "runpy.py"}", line 2\n').encode()
    normal = {"stdout": base64.b64encode(b"plain").decode(),
              "stderr": base64.b64encode(trace).decode()}
    runner._scrub(normal, runner._host_paths(source, str(python)))
    stderr = base64.b64decode(normal["stderr"])
    assert str(source).encode() not in stderr and str(python.parent).encode() not in stderr
    assert b"{ORACLE}" in stderr and b"{PYTHON}" in stderr
    assert base64.b64decode(normal["stdout"]) == b"plain"


def test_closed_stderr_arm_still_observes(tmp_path):
    case = core.Case("c", [], stderr_closed=True)
    target = core.Target("python", [sys.executable, "-c",
                                    "import sys; print('out'); sys.stderr.write('x')"])
    obs = core.run_arm(case, target, tmp_path / "root" / "h")
    assert core.decode(obs, "stdout").strip() == b"out" and core.decode(obs, "stderr") == b""


_TRAILER = (b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' "
            b"encoding='utf-8'>\r\nOSError: [Errno 22] Invalid argument\r\n")


def test_stdout_closed_trailer_removes_only_the_trailer_and_keeps_120():
    rule = ("python-stdout-closed-trailer",)
    python = obs(exit=120, stderr=b"warning\r\n" + _TRAILER)
    assert compare.diff(python, obs(exit=120, stderr=b"warning\r\n"), rule) == []
    # The candidate's own code where CPython's shutdown flush made it 120.
    assert compare.diff(python, obs(exit=0, stderr=b"warning\r\n"), rule)
    # The trailer with another exit, or anything after it, stays.
    assert compare.diff(obs(exit=1, stderr=_TRAILER), obs(exit=1), rule)
    assert compare.diff(obs(exit=120, stderr=_TRAILER + b"x"), obs(exit=120), rule)


def _near_obs(seed: str, start: float, shown: str | None = None, saved: str | None = None):
    profile = b'{\n  "name": "dot",\n  "runtime_key_expires_at": "%s"\n}' % (saved or seed).encode()
    stdout = b'{"key_expiry": {"known": true, "expires_at": "%s"}}\n' % (shown or seed).encode()
    return obs(stdout=stdout, files={"tunnels/dot.profile.json": profile},
               window=[start, start + 1], seeded_expiry=seed)


def _midnight(days: int, now: float) -> str:
    day = time.strftime("%Y-%m-%d", time.gmtime(now + days * 86_400))
    return f"{day}T00:00:00Z"


def test_tunnel_near_expiry_binds_to_the_exact_seed_and_field():
    from cli_harness.rows import tunnel  # noqa: F401, PLC0415 - registers the rule
    rule = ("tunnel-near-expiry",)
    now = time.time()
    today = _near_obs(_midnight(3, now), now)
    # A golden recorded a day earlier seeded its own date: equal.
    assert compare.diff(_near_obs(_midnight(2, now), now - 86_400), today, rule) == []
    # The seed's date moved by a day, still near expiry: not hidden.
    earlier = _midnight(2, now)
    assert compare.diff(today, _near_obs(_midnight(3, now), now, shown=earlier), rule)
    assert compare.diff(today, _near_obs(_midnight(3, now), now, saved=earlier), rule)
    # A seed outside the near-expiry class is never tokenized.
    far = _near_obs(_midnight(9, now), now)
    assert compare.diff(far, _near_obs(_midnight(8, now), now - 86_400), rule)


def test_redaction_keeps_a_secret_lines_ending_difference(tmp_path):
    """An oracle writing the secret line with LF and a candidate writing it
    with CRLF must still differ once both are redacted."""
    from cli_harness.rows import test_login  # noqa: PLC0415
    field = f"{test_login._LOGIN_SECRET_FIELD}=".encode()
    seen = {"shape": "token_urlsafe32", "authenticates": "ok", "kept": False,
            "verifier": {"stored_key": "s", "server_key": "k"}}
    redacted = {}
    for name, ending in (("lf", b"\n"), ("crlf", b"\r\n")):
        login = tmp_path / f"{name}.env"
        login.write_bytes(b"A=1\n" + field + b"drawn" + ending)
        test_login._redact_validated(login, seen)
        redacted[name] = login.read_bytes()
    assert redacted["lf"] == b"A=1\n" + field + b"<validated>\n"
    assert redacted["crlf"] == b"A=1\n" + field + b"<validated>\r\n"
    assert compare.diff(obs(files={"test-pg.env": redacted["lf"]}),
                        obs(files={"test-pg.env": redacted["crlf"]}), ())


def test_validated_login_secret_is_replaced_only_on_its_single_line(tmp_path):
    from cli_harness.rows import test_login  # noqa: PLC0415
    field = f"{test_login._LOGIN_SECRET_FIELD}=".encode()
    seen = {"shape": "token_urlsafe32", "authenticates": "ok", "kept": False,
            "verifier": {"stored_key": "s", "server_key": "k"}}
    login = tmp_path / "test-pg.env"
    # One line (CRLF here): replaced with its own terminator kept, its
    # neighbours byte for byte as they were.
    login.write_bytes(b"A=1\r\n" + field + b"drawn\r\nB=drawn\n")
    test_login._redact_validated(login, seen)
    assert login.read_bytes() == b"A=1\r\n" + field + b"<validated>\r\nB=drawn\n"
    # LF.
    login.write_bytes(field + b"drawn\n")
    test_login._redact_validated(login, seen)
    assert login.read_bytes() == field + b"<validated>\n"
    # Two such lines: left as they are, so the comparison shows them.
    doubled = field + b"drawn\n" + field + b"other\n"
    login.write_bytes(doubled)
    test_login._redact_validated(login, seen)
    assert login.read_bytes() == doubled
    # A run not validated, or one that kept the old value, changes nothing.
    login.write_bytes(field + b"drawn\n")
    for unvalidated in (dict(seen, authenticates="refused"), dict(seen, kept=True),
                        dict(seen, shape="seeded"), dict(seen, verifier={"stored_key": "s"})):
        test_login._redact_validated(login, unvalidated)
    assert login.read_bytes() == field + b"drawn\n"


_SCRAM = (b"SCRAM-SHA-256$4096:" + base64.b64encode(b"s" * 16) + b"$"
          + base64.b64encode(b"k" * 32) + b":" + base64.b64encode(b"v" * 32))


def test_recorder_writes_no_scram_verifier(tmp_path, monkeypatch):
    import json  # noqa: PLC0415
    from cli_harness import runner  # noqa: PLC0415
    echo = b"test-login: ... PASSWORD '" + _SCRAM + b"'\n"
    monkeypatch.setattr(runner.rows, "ROWS", {"probe": "CLI-PROBE"})
    monkeypatch.setattr(runner, "_golden_path", lambda row: tmp_path / f"{row}.json")
    monkeypatch.setattr(core, "run_arm", lambda case, target, home: obs(
        exit=1, stdout=echo, stderr=echo, files={"login.env": b"V=" + _SCRAM + b"\n"},
        home=str(home)))
    path = runner.record("probe", [core.Case("c", [])], core.Target("python", [sys.executable]),
                         tmp_path, {"oracle_commit": "0" * 40, "oracle_commit_source": "git"})
    golden = json.loads(path.read_text(encoding="utf-8"))
    assert normalize.scram_marks(golden) == []
    case = golden["cases"]["c"]
    for field in ("stdout", "stderr"):
        assert base64.b64decode(case[field]) == b"test-login: ... PASSWORD '" + \
            normalize.SCRAM_TOKEN + b"'\n"
    assert base64.b64decode(case["files"]["login.env"][5:]) == b"V=" + normalize.SCRAM_TOKEN + b"\n"
    assert golden["oracle_commit"] == "0" * 40


def test_golden_writer_refuses_a_scram_verifier_anywhere(tmp_path):
    from cli_harness import runner  # noqa: PLC0415
    for golden in ({"cases": {"c": obs(stdout=b"x " + _SCRAM)}},
                   {"cases": {"c": obs(files={"f": _SCRAM})}},
                   {"cases": {}, "note": "SCRAM-SHA-256$1:a$b:c"}):
        with pytest.raises(SystemExit, match="SCRAM-SHA-256"):
            runner.write_golden(tmp_path / "g.json", golden)
        assert not (tmp_path / "g.json").exists()


def test_no_committed_golden_carries_a_verifier_or_a_drawn_login_secret():
    import json  # noqa: PLC0415
    from cli_harness import rows  # noqa: PLC0415
    goldens = sorted((Path(__file__).resolve().parent / "goldens").glob("*.json"))
    assert goldens
    found = {}
    for path in goldens:
        golden = json.loads(path.read_text(encoding="utf-8"))
        fixtures = rows.fixture_login_secrets(path.name.split(".")[0])
        found[path.name] = (normalize.scram_marks(golden)
                            + normalize.drawn_login_secrets(golden, fixtures))
    assert {name: marks for name, marks in found.items() if marks} == {}


def _drawn_login_line() -> bytes:
    import secrets  # noqa: PLC0415
    from cli_harness.rows import test_login  # noqa: PLC0415
    drawn = secrets.token_urlsafe(32)
    assert len(drawn) == 43
    return f"{test_login._LOGIN_SECRET_FIELD}={drawn}".encode()


def test_golden_writer_refuses_a_drawn_login_secret_in_a_stream(tmp_path):
    from cli_harness import runner  # noqa: PLC0415
    from cli_harness.rows import test_login  # noqa: PLC0415
    path = tmp_path / "g.json"
    golden = {"cases": {"c": obs(stdout=_drawn_login_line() + b"\n",
                                 files={"test-pg.env": b"A=1\n"})}}
    with pytest.raises(SystemExit, match=r"drawn login secret remains at /cases/c/stdout$"):
        runner.write_golden(path, golden, test_login.FIXTURE_LOGIN_SECRETS)
    assert not path.exists()


def test_golden_writer_refuses_a_drawn_login_secret_in_a_file(tmp_path):
    from cli_harness import runner  # noqa: PLC0415
    from cli_harness.rows import test_login  # noqa: PLC0415
    path = tmp_path / "g.json"
    # Only inside the file's base64 content: both streams are clean.
    golden = {"cases": {"c": obs(stdout=b"wrote test-pg.env\n",
                                 files={"test-pg.env": b"A=1\n" + _drawn_login_line() + b"\n"})}}
    stored = golden["cases"]["c"]["files"]["test-pg.env"]
    assert stored.startswith("file:") and b"PASSWORD" not in stored.encode()
    with pytest.raises(SystemExit,
                       match=r"drawn login secret remains at /cases/c/files/test-pg\.env$"):
        runner.write_golden(path, golden, test_login.FIXTURE_LOGIN_SECRETS)
    assert not path.exists()


def test_golden_writer_keeps_fixture_and_validated_login_secrets(tmp_path):
    from cli_harness import runner  # noqa: PLC0415
    from cli_harness.rows import test_login  # noqa: PLC0415
    field = f"{test_login._LOGIN_SECRET_FIELD}=".encode()
    path = tmp_path / "g.json"
    # The row's fixtures and a validated value are written as they are.
    for value in (*test_login.FIXTURE_LOGIN_SECRETS, "<validated>"):
        golden = {"cases": {"c": obs(files={"test-pg.env": field + value.encode() + b"\n"})}}
        runner.write_golden(path, golden, test_login.FIXTURE_LOGIN_SECRETS)
        assert path.exists()
        path.unlink()


def test_verifier_echo_rule_meets_the_recorded_token():
    from cli_harness.rows import test_login  # noqa: F401, PLC0415 - registers the rule
    rule = ("test-login-verifier-echo",)
    native = obs(exit=1, stdout=b"PASSWORD '" + _SCRAM + b"'\n")
    golden = obs(exit=1, stdout=b"PASSWORD '" + normalize.SCRAM_TOKEN + b"'\n")
    assert compare.diff(golden, native, rule) == []
    # A malformed verifier is not mapped, so it still differs from the token.
    assert compare.diff(golden, obs(exit=1, stdout=b"PASSWORD '" + _SCRAM[:-2] + b"'\n"), rule)


def _make_repo(path: Path) -> str:
    import subprocess  # noqa: PLC0415
    (path / "pseudolife_memory").mkdir(parents=True)
    (path / "pseudolife_memory" / "cli.py").write_bytes(b"print('oracle')\n")
    vcs = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com",
           "-c", "core.autocrlf=false"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(vcs + args, check=True, capture_output=True)
    return subprocess.run(vcs + ["rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_record_binds_a_checkout_to_its_clean_head(tmp_path, monkeypatch):
    from cli_harness import runner  # noqa: PLC0415
    monkeypatch.delenv(runner.COMMIT_ENV, raising=False)
    head = _make_repo(tmp_path / "repo")
    binding = runner.oracle_binding(tmp_path / "repo", None)
    assert binding == {"oracle_commit": head, "oracle_commit_source": "git"}
    with pytest.raises(SystemExit, match="is not"):
        runner.oracle_binding(tmp_path / "repo", "0" * 40)
    (tmp_path / "repo" / "pseudolife_memory" / "cli.py").write_bytes(b"changed\n")
    with pytest.raises(SystemExit, match="not HEAD's"):
        runner.oracle_binding(tmp_path / "repo", None)


@pytest.mark.parametrize("stray", ["shadow.py", "sub/shadow.py", "shadow.pyc"])
def test_record_refuses_untracked_or_ignored_oracle_modules(tmp_path, monkeypatch, stray):
    from cli_harness import runner  # noqa: PLC0415
    monkeypatch.delenv(runner.COMMIT_ENV, raising=False)
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("*.pyc\n__pycache__/\n")
    package = repo / "pseudolife_memory"
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "cli.cpython-311.pyc").write_bytes(b"bytecode")
    # Bytecode under __pycache__ alone still binds.
    assert runner.oracle_binding(repo, None)["oracle_commit_source"] == "git"
    (package / stray).parent.mkdir(parents=True, exist_ok=True)
    (package / stray).write_bytes(b"print('shadow')\n")
    with pytest.raises(SystemExit, match="not HEAD's"):
        runner.oracle_binding(repo, None)


def test_record_reads_the_bytes_on_disk_not_the_index(tmp_path, monkeypatch):
    import subprocess  # noqa: PLC0415
    from cli_harness import runner  # noqa: PLC0415
    monkeypatch.delenv(runner.COMMIT_ENV, raising=False)
    repo = tmp_path / "repo"
    _make_repo(repo)
    subprocess.run(["git", "-C", str(repo), "update-index", "--assume-unchanged",
                    "pseudolife_memory/cli.py"], check=True, capture_output=True)
    (repo / "pseudolife_memory" / "cli.py").write_bytes(b"print('edited')\n")
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--ignored",
                             "--untracked-files=all"], capture_output=True, text=True)
    assert status.stdout == ""  # git itself no longer sees the edit
    with pytest.raises(SystemExit, match="changed pseudolife_memory/cli.py"):
        runner.oracle_binding(repo, None)


@pytest.mark.parametrize("broken", ["git-file", "git-dir-env"])
def test_record_refuses_when_git_fails(tmp_path, monkeypatch, broken):
    from cli_harness import runner  # noqa: PLC0415
    repo = tmp_path / "repo"
    head = _make_repo(repo)
    if broken == "git-file":
        (repo / ".git").rename(tmp_path / "moved.git")
        (repo / ".git").write_text("gitdir: nowhere\n")
    else:
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    # Even with the right commit declared, a failing git is never an export.
    monkeypatch.setenv(runner.COMMIT_ENV, head)
    with pytest.raises(SystemExit, match="git exited 128"):
        runner.oracle_binding(repo, None, repo)


def test_record_refuses_bytecode_without_its_source(tmp_path, monkeypatch):
    from cli_harness import runner  # noqa: PLC0415
    monkeypatch.delenv(runner.COMMIT_ENV, raising=False)
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("__pycache__/\n")
    cache = repo / "pseudolife_memory" / "__pycache__"
    cache.mkdir()
    (cache / "cli.cpython-311.pyc").write_bytes(b"bytecode")
    assert runner.oracle_binding(repo, None)["oracle_commit_source"] == "git"
    (cache / "gone.cpython-311.pyc").write_bytes(b"stale shadow")
    with pytest.raises(SystemExit, match="cache without its source"):
        runner.oracle_binding(repo, None)


def test_oracle_arm_never_runs_in_tree_bytecode(tmp_path):
    """A stale cache whose header matches its source (mtime and size) runs
    in place of the source under CPython's default check; the oracle arm's
    environment makes CPython ignore the tree's __pycache__ altogether."""
    import importlib._bootstrap_external as bootstrap  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    package = tmp_path / "pkg"
    package.mkdir()
    module = package / "probe_mod.py"
    module.write_text("print('source')\n")
    stat = module.stat()
    stale = compile("print('shadow')\n", str(module), "exec")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / f"probe_mod.{sys.implementation.cache_tag}.pyc").write_bytes(
        bootstrap._code_to_timestamp_pyc(stale, int(stat.st_mtime), stat.st_size))
    oracle_env = core.python_target(sys.executable, package).env

    def run(env):
        env = {**os.environ, **env, "PYTHONPATH": str(package)}
        return subprocess.run([sys.executable, "-c", "import probe_mod"], env=env, cwd=tmp_path,
                              capture_output=True, text=True, check=True).stdout.strip()

    control = {k: v for k, v in oracle_env.items() if k != "PYTHONPYCACHEPREFIX"}
    assert run(control) == "shadow"  # the stale cache is what a plain launch runs
    assert run(oracle_env) == "source"


_PARENT_DRIVER = '''
import sys
from pathlib import Path
sys.path.insert(0, {rust!r})
from cli_harness import core, rows, runner

runner.GOLDENS = Path(sys.argv[1])


def seed(arm):
    from pseudolife_memory.seeder import VALUE  # a seeder helper, imported here
    (arm.home / "seeded.txt").write_text(VALUE)


rows.ROWS["parent_cache_probe"] = "CLI-PROBE"
rows.load = lambda row: [core.Case("seeded", ["x"], setup=seed)]
relaunch = getattr(runner, "isolated_relaunch", lambda: None)  # as __main__ does
code = relaunch()
if code is None:
    code = runner.main(["--row", "parent_cache_probe", "--record", "--oracle-source", sys.argv[2]])
raise SystemExit(code)
'''


def test_parent_process_records_the_source_not_a_stale_seeder_cache(tmp_path):
    """The harness process imports oracle seeders itself. A stale cache for
    one, headed with its source's mtime and size, is what a plain import
    runs; the recorded bytes must come from the bound source instead."""
    import importlib._bootstrap_external as bootstrap  # noqa: PLC0415
    import json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    repo = tmp_path / "oracle"
    package = repo / "pseudolife_memory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("print('oracle')\n")
    seeder = package / "seeder.py"
    seeder.write_text('VALUE = "source"\n')
    vcs = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
           "-c", "core.autocrlf=false"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(vcs + args, check=True, capture_output=True)
    head = subprocess.run(vcs + ["rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    stat = seeder.stat()
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / f"seeder.{sys.implementation.cache_tag}.pyc").write_bytes(
        bootstrap._code_to_timestamp_pyc(compile('VALUE = "shadow"\n', str(seeder), "exec"),
                                         int(stat.st_mtime), stat.st_size))
    driver = tmp_path / "driver.py"
    driver.write_text(_PARENT_DRIVER.format(rust=str(Path(__file__).resolve().parent.parent)))
    goldens = tmp_path / "goldens"
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE", "CLI_HARNESS_PYCACHE_PREFIX")}
    done = subprocess.run([sys.executable, str(driver), str(goldens), str(repo)], env=env,
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    golden = json.loads(next(goldens.glob("parent_cache_probe.*.json")).read_text("utf-8"))
    assert golden["oracle_commit"] == head
    seeded = golden["cases"]["seeded"]["files"]["seeded.txt"]
    assert base64.b64decode(seeded[5:]) == b"source"


def test_record_refuses_an_oracle_file_edited_between_rows(tmp_path, monkeypatch):
    from cli_harness import runner  # noqa: PLC0415
    from cli_harness.rows import _daemon  # noqa: PLC0415
    monkeypatch.delenv(runner.COMMIT_ENV, raising=False)
    repo = tmp_path / "repo"
    _make_repo(repo)
    empty = tmp_path / "empty"
    empty.mkdir()
    for name in [n for n in sys.modules if n.split(".")[0] == "pseudolife_memory"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "pycache_prefix", str(empty))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(_daemon, "ORACLE", dict(_daemon.ORACLE))
    goldens = tmp_path / "goldens"
    monkeypatch.setattr(runner, "GOLDENS", goldens)
    monkeypatch.setattr(runner.rows, "ROWS", {"first": "CLI-A", "second": "CLI-B"})

    def load(row):
        if row == "second":  # an oracle subprocess of this row would import the edit
            (repo / "pseudolife_memory" / "cli.py").write_bytes(b"print('edited')\n")
        return []

    monkeypatch.setattr(runner.rows, "load", load)
    with pytest.raises(SystemExit, match="changed pseudolife_memory/cli.py"):
        runner.main(["--row", "first", "--row", "second", "--record",
                     "--oracle-source", str(repo)])
    assert [p.name.split(".")[0] for p in goldens.glob("*.json")] == ["first"]


def test_record_refuses_an_oracle_file_edited_after_binding(tmp_path, monkeypatch):
    """The tree is bound, then a tracked file changes before the first row:
    the start-of-run verify refuses, and nothing is recorded."""
    from cli_harness import runner  # noqa: PLC0415
    from cli_harness.rows import _daemon  # noqa: PLC0415
    monkeypatch.delenv(runner.COMMIT_ENV, raising=False)
    repo = tmp_path / "repo"
    head = _make_repo(repo)
    empty = tmp_path / "empty"
    empty.mkdir()
    for name in [n for n in sys.modules if n.split(".")[0] == "pseudolife_memory"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "pycache_prefix", str(empty))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(_daemon, "ORACLE", dict(_daemon.ORACLE))
    goldens = tmp_path / "goldens"
    monkeypatch.setattr(runner, "GOLDENS", goldens)
    monkeypatch.setattr(runner.rows, "ROWS", {"only": "CLI-A"})
    monkeypatch.setattr(runner.rows, "load", lambda row: [])
    bind = runner.oracle_binding
    bound = []

    def bind_then_edit(source, declared, *rest):
        binding = bind(source, declared, *rest)
        bound.append(binding)
        (repo / "pseudolife_memory" / "cli.py").write_bytes(b"print('edited')\n")
        return binding

    monkeypatch.setattr(runner, "oracle_binding", bind_then_edit)
    with pytest.raises(SystemExit) as refused:
        runner.main(["--row", "only", "--record", "--oracle-source", str(repo)])
    assert bound == [{"oracle_commit": head, "oracle_commit_source": "git"}]
    assert str(refused.value) == (f"--record: {repo.resolve()}'s pseudolife_memory/ is no "
                                  "longer the bound commit's: changed pseudolife_memory/cli.py")
    assert not goldens.exists() or not list(goldens.iterdir())


def test_parent_module_check_refuses_an_unisolated_or_foreign_load(tmp_path, monkeypatch):
    from cli_harness import runner  # noqa: PLC0415
    # Other test files may have imported the oracle package into this process.
    for name in [n for n in sys.modules if n.split(".")[0] == "pseudolife_memory"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "pycache_prefix", None)
    assert "cached bytecode" in runner.parent_module_problems(tmp_path, None)[0]
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "pycache_prefix", str(empty))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    assert runner.parent_module_problems(tmp_path, None) == []
    # An oracle module loaded from somewhere other than the bound source.
    foreign = type(sys)("pseudolife_memory")
    foreign.__spec__ = type("Spec", (), {"origin": str(tmp_path / "elsewhere" / "__init__.py"),
                                         "has_location": True, "cached": None})()
    monkeypatch.setitem(sys.modules, "pseudolife_memory", foreign)
    assert "was loaded from" in runner.parent_module_problems(tmp_path, None)[0]


def test_record_refuses_an_unbound_or_mismatched_export(tmp_path, monkeypatch):
    import shutil  # noqa: PLC0415
    from cli_harness import runner  # noqa: PLC0415
    monkeypatch.delenv(runner.COMMIT_ENV, raising=False)
    repo = tmp_path / "repo"
    head = _make_repo(repo)
    export = tmp_path / "export"
    shutil.copytree(repo / "pseudolife_memory", export / "pseudolife_memory")
    with pytest.raises(SystemExit, match="unbound"):
        runner.oracle_binding(export, None, repo)
    with pytest.raises(SystemExit, match="full 40-character"):
        runner.oracle_binding(export, head[:8], repo)
    monkeypatch.setenv(runner.COMMIT_ENV, head)
    assert runner.oracle_binding(export, None, repo) == {
        "oracle_commit": head, "oracle_commit_source": "verified-tree"}
    # A CRLF export of an LF blob is the same file.
    (export / "pseudolife_memory" / "cli.py").write_bytes(b"print('oracle')\r\n")
    assert runner.oracle_binding(export, None, repo)["oracle_commit_source"] == "verified-tree"
    (export / "pseudolife_memory" / "cli.py").write_bytes(b"print('other')\n")
    with pytest.raises(SystemExit, match="changed pseudolife_memory/cli.py"):
        runner.oracle_binding(export, None, repo)
    (export / "pseudolife_memory" / "extra.py").write_bytes(b"")
    with pytest.raises(SystemExit, match="not in the commit"):
        runner.oracle_binding(export, None, repo)
    # With no checkout to ask, the commit is recorded as declared.
    assert runner.oracle_binding(export, None, export) == {
        "oracle_commit": head, "oracle_commit_source": "declared"}


def test_summary_keeps_completed_rows_when_a_later_row_crashes(tmp_path, monkeypatch):
    import json  # noqa: PLC0415
    from cli_harness import runner  # noqa: PLC0415
    monkeypatch.setattr(runner.rows, "ROWS", {"first": "CLI-A", "second": "CLI-B"})
    monkeypatch.setattr(runner.rows, "load", lambda row: [])

    def run_row(row, *a):
        if row == "second":
            raise RuntimeError("row crashed")
        return {"case": {"status": "match"}}

    monkeypatch.setattr(runner, "run_row", run_row)
    out = tmp_path / "summary.json"
    with pytest.raises(RuntimeError, match="row crashed"):
        runner.main(["--row", "first", "--row", "second", "--candidate", sys.executable,
                     "--out", str(out)])
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["complete"] is False and list(summary["rows"]) == ["first"]
    assert summary["rows"]["first"]["results"] == {"case": {"status": "match"}}
    assert [p.name for p in tmp_path.iterdir()] == ["summary.json"]


def test_summary_keeps_rows_that_share_a_parity_id(tmp_path, monkeypatch):
    import json  # noqa: PLC0415
    from cli_harness import runner  # noqa: PLC0415
    monkeypatch.setattr(runner.rows, "ROWS", {"invite": "CLI-PAIRING", "pair": "CLI-PAIRING"})
    monkeypatch.setattr(runner.rows, "load", lambda row: [])
    monkeypatch.setattr(runner, "run_row", lambda row, *a: {f"{row}-case": {"status": "match"}})
    out = tmp_path / "summary.json"
    assert runner.main(["--row", "invite", "--row", "pair", "--candidate", sys.executable,
                        "--out", str(out)]) == 0
    summary = json.loads(out.read_text(encoding="utf-8"))["rows"]
    assert set(summary) == {"invite", "pair"}
    assert summary["invite"]["parity"] == summary["pair"]["parity"] == "CLI-PAIRING"
    assert list(summary["pair"]["results"]) == ["pair-case"]
