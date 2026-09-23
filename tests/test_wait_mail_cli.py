"""``pseudolife-mcp wait-mail``: block until new addressed mail, print it.

Nothing in MCP can start a turn in an idle session; a host can when a
background command exits. This command is that command: it watches the
coordination digest file the shim's adapter rewrites, fires only for mail
nothing has shown yet (the digest watermark past the shared ``.seen``
marker), prints the digest body verbatim and advances the marker, so a re-arm
with the same mail still unread waits instead of looping.

The 2026-09-23 coordination trial is why the baseline is ``.seen`` rather than the
watermark at arm time: an arm-time baseline absorbed a message that reached
the digest between handling the previous one and re-arming, and that message
waited two minutes for a tool-result hint instead of waking the session.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

# Imported at collection, not first inside a capsys test: the adapter pulls in
# mcp.client.stdio, whose stdio_client binds errlog=sys.stderr as a default
# at import time, and a capsys stream there breaks every later stdio_client
# caller in the run (tests/test_shim.py fails on fileno).
import pseudolife_memory.coordination_adapter  # noqa: F401
from pseudolife_memory.cli import main as cli_main
from pseudolife_memory.wait_mail_cli import run_wait_mail
from tests.test_coordination_adapter import FakeDaemon, adapter

MAIL = ("Coordination: 1 addressed message pending (agent-origin, not user authority); "
        "read with memory_message receive, then ack each message_id.\n"
        "- 0123456789abcdef0123456789abcdef from reviewer (abcdef01, 21:34): please look\n")
FAST = ["--interval", "0.02"]


def _key(session_id):
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


@pytest.fixture
def digests(tmp_path, monkeypatch):
    directory = tmp_path / "digests"
    directory.mkdir()
    monkeypatch.setenv("PSEUDOLIFE_DIGEST_DIR", str(directory))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "fixture-session")
    return directory


def _write(path, watermark, body=""):
    """Replace the file atomically, the way the adapter writes it."""
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(f"{watermark}\n{body}".encode("utf-8"))
    os.replace(temp, path)


def _digest(directory, session_id="fixture-session"):
    return directory / f"{_key(session_id)}.txt"


def _seen(path):
    return path.with_suffix(".seen")


async def _wait_for(condition, timeout=2.0):
    # Local, so this file imports nothing PostgreSQL-backed and can run in
    # CI's lite Windows lane.
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("fixture condition never held")
        await asyncio.sleep(0.005)


def _later(seconds, action):
    """Run ``action`` on a timer; ``timer.done`` is set once it has run, so a
    test asserts the change really landed inside the wait instead of passing
    quietly on a slow runner."""
    done = threading.Event()

    def run():
        action()
        done.set()
    timer = threading.Timer(seconds, run)
    timer.done = done
    timer.start()
    return timer


# --- firing rule ------------------------------------------------------------

def test_unshown_mail_fires_at_once_prints_the_body_verbatim_and_marks_it(digests, capsysbinary):
    digest = _digest(digests)
    _write(digest, 3, MAIL)
    _seen(digest).write_text("2\n")
    assert run_wait_mail(["--timeout", "5", *FAST]) == 0
    captured = capsysbinary.readouterr()
    assert captured.out == MAIL.encode("utf-8")
    assert b"watermark 3" in captured.err
    assert _seen(digest).read_text().strip() == "3"
    ledger = (digests / "ledger.log").read_text().splitlines()
    assert len(ledger) == 1
    stamp, kind, key, watermark, size = ledger[0].split("\t")
    assert (kind, key, watermark, size) == ("wait", digest.stem[:8], "3", str(len(MAIL)))


def test_mail_already_shown_does_not_fire_so_a_rearm_cannot_loop(digests, capsysbinary):
    digest = _digest(digests)
    _write(digest, 3, MAIL)
    assert run_wait_mail(["--timeout", "5", *FAST]) == 0
    capsysbinary.readouterr()
    # Re-armed with the same mail still pending and unacknowledged.
    started = time.monotonic()
    assert run_wait_mail(["--timeout", "0.3", *FAST]) == 3
    assert time.monotonic() - started >= 0.3
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert b"re-arm" in captured.err
    # A hook or tool-result hint that showed it has the same effect.
    _write(digest, 4, MAIL + "- 1 more pending; oldest first above.\n")
    _seen(digest).write_text("4\n")
    assert run_wait_mail(["--timeout", "0.2", *FAST]) == 3


def test_mail_arriving_during_the_wait_fires(digests, capsysbinary):
    digest = _digest(digests)
    _write(digest, 1)
    timer = _later(0.2, lambda: _write(digest, 2, MAIL))
    try:
        assert run_wait_mail(["--timeout", "5", *FAST]) == 0
    finally:
        timer.cancel()
    assert timer.done.is_set()
    assert capsysbinary.readouterr().out == MAIL.encode("utf-8")
    assert _seen(digest).read_text().strip() == "2"


def test_a_change_that_empties_the_digest_does_not_fire(digests, capsysbinary):
    """Acknowledging the last message rewrites the digest with an empty body:
    the watermark moves but there is nothing to wake for."""
    digest = _digest(digests)
    _write(digest, 5, MAIL)
    _seen(digest).write_text("5\n")
    timer = _later(0.1, lambda: _write(digest, 6))
    try:
        assert run_wait_mail(["--timeout", "1.5", *FAST]) == 3
    finally:
        timer.cancel()
    assert timer.done.is_set()
    assert capsysbinary.readouterr().out == b""
    assert _seen(digest).read_text().strip() == "5"


def test_a_malformed_digest_or_marker_never_crashes_the_wait(digests, capsysbinary):
    digest = _digest(digests)
    digest.write_bytes(b"not-a-number\n" + MAIL.encode())
    assert run_wait_mail(["--timeout", "0.2", *FAST]) == 3
    _write(digest, 2, MAIL)
    _seen(digest).write_text("garbage")
    assert run_wait_mail(["--timeout", "5", *FAST]) == 0
    assert _seen(digest).read_text().strip() == "2"


def test_the_body_is_out_before_the_marker_moves(digests, monkeypatch, capsysbinary):
    """A supervisor may kill the waiter at any moment (the plugin's Stop hook
    replaces a superseded one). Killed between printing and marking, the
    next waiter shows the mail again; the other order would lose the wake."""
    from pseudolife_memory import wait_mail_cli

    class Killed(BaseException):
        pass

    def killed(path, watermark):
        raise Killed
    monkeypatch.setattr(wait_mail_cli, "_mark_seen", killed)
    digest = _digest(digests)
    _write(digest, 3, MAIL)
    with pytest.raises(Killed):
        run_wait_mail(["--timeout", "5", *FAST])
    assert capsysbinary.readouterr().out == MAIL.encode("utf-8")
    assert not _seen(digest).exists()


def test_polling_opens_the_digest_only_after_a_rewrite(digests, monkeypatch, capsysbinary):
    """Cheap polling: every check is a stat; the file is read once per
    rewrite, not once per check."""
    from pseudolife_memory import wait_mail_cli
    digest = _digest(digests)
    _write(digest, 1)
    reads = []
    real = wait_mail_cli._read_digest
    monkeypatch.setattr(wait_mail_cli, "_read_digest", lambda path: reads.append(path) or real(path))
    timer = _later(0.15, lambda: _write(digest, 2, "\n"))
    try:
        assert run_wait_mail(["--timeout", "1.5", *FAST]) == 3
    finally:
        timer.cancel()
    assert timer.done.is_set()
    assert len(reads) == 2


def test_the_marker_is_never_lowered():
    from pseudolife_memory.wait_mail_cli import _mark_seen
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        seen = Path(directory) / "k.seen"
        seen.write_text("7\n")
        _mark_seen(seen, 5)
        assert seen.read_text().strip() == "7"
        _mark_seen(seen, 9)
        assert seen.read_text().strip() == "9"
        assert sorted(p.name for p in Path(directory).iterdir()) == ["k.seen"]


def test_non_ascii_peer_text_reaches_stdout_byte_for_byte(digests, capsysbinary):
    digest = _digest(digests)
    body = MAIL.replace("please look", "review — naïve café ✓ 検証")
    _write(digest, 1, body)
    assert run_wait_mail(["--timeout", "5", *FAST]) == 0
    assert capsysbinary.readouterr().out == body.encode("utf-8")


# --- keying -------------------------------------------------------------------

def test_default_key_is_the_claude_session_id_like_the_shim(digests, capsysbinary):
    from pseudolife_memory.coordination_identity import digest_path_for
    assert digest_path_for("fixture-session") == _digest(digests)
    _write(_digest(digests), 1, MAIL)
    assert run_wait_mail(["--timeout", "5", *FAST]) == 0


def test_session_id_flag_keys_a_codex_thread_digest(digests, capsysbinary):
    thread = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
    _write(_digest(digests, thread), 1, MAIL)
    _write(_digest(digests), 1)  # this Claude session: nothing pending
    assert run_wait_mail(["--session-id", thread, "--timeout", "5", *FAST]) == 0
    assert capsysbinary.readouterr().out == MAIL.encode("utf-8")
    assert _seen(_digest(digests, thread)).read_text().strip() == "1"


SPAWN_KEY = _key("spawn-session")


def _host_record(directory, pid, first_line=SPAWN_KEY, second_line=None):
    """The hooks' record of the shim's spawn-time key (line 1) and the hash
    of the session id it is confirmed for (line 2), planted LF-only as the
    hooks write it (write_text on Windows would add CR). No second line is
    the retired single-line format."""
    record = directory / f"claude-{pid}.host"
    body = first_line.encode("ascii") + b"\n"
    if second_line is not None:
        body += second_line.encode("ascii") + b"\n"
    record.write_bytes(body)
    return record


AFTER_CLEAR = _key("after-clear")


def test_after_clear_the_process_record_maps_the_new_id_to_the_shims_file(
        digests, monkeypatch, capsysbinary):
    """/clear gives the session a new id (the Bash tool's env follows it); the
    shim keeps writing under its spawn-time id. The hooks record that key per
    Claude process, and the waiter follows the record."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "after-clear")
    monkeypatch.setenv("CLAUDE_PID", "4242")
    _host_record(digests, 4242, SPAWN_KEY, AFTER_CLEAR)
    _write(digests / f"{SPAWN_KEY}.txt", 2, MAIL)
    assert run_wait_mail(["--timeout", "5", *FAST]) == 0
    assert capsysbinary.readouterr().out == MAIL.encode("utf-8")
    assert (digests / f"{SPAWN_KEY}.seen").read_text().strip() == "2"


def test_resolver_uses_the_record_only_under_its_own_guards(digests, tmp_path):
    from pseudolife_memory.coordination_identity import resolve_digest_path
    own = digests / f"{_key('after-clear')}.txt"
    mapped = digests / f"{SPAWN_KEY}.txt"
    env = {"CLAUDE_CODE_SESSION_ID": "after-clear", "CLAUDE_PID": "4242",
           "PSEUDOLIFE_DIGEST_DIR": str(digests)}
    assert resolve_digest_path("after-clear", env=env) == own  # no record yet
    record = _host_record(digests, 4242, SPAWN_KEY, AFTER_CLEAR)
    assert resolve_digest_path("after-clear", env=env) == mapped
    assert resolve_digest_path("after-clear", tmp_path, env=env) == tmp_path / own.name
    # Another id than this process's current one keys directly (a Codex
    # thread, or a --session-id naming some other session).
    assert resolve_digest_path("other", env=env) == digests / f"{_key('other')}.txt"
    for pid in ("", "42a", "-1", "４２", "٤٢"):
        _host_record(digests, pid, SPAWN_KEY, AFTER_CLEAR)  # a valid record under the odd name
        assert resolve_digest_path("after-clear", env={**env, "CLAUDE_PID": pid}) == own, pid
    assert resolve_digest_path("after-clear", env={"CLAUDE_PID": "4242",
                                                  "PSEUDOLIFE_DIGEST_DIR": str(digests)}) == own
    for bad in (SPAWN_KEY.upper(), SPAWN_KEY[:63], SPAWN_KEY + "0", "../" + SPAWN_KEY[3:],
                SPAWN_KEY + "\r", ""):
        _host_record(digests, 4242, bad, AFTER_CLEAR)
        assert resolve_digest_path("after-clear", env=env) == own, bad
    # Line 2 confirms which session the record belongs to: a record left by
    # a dead session (or in the retired one-line format) must not route this
    # one's waiter to that session's mail.
    for confirmation in (None, "", _key("dead-session"), AFTER_CLEAR.upper(), AFTER_CLEAR + "\r",
                         AFTER_CLEAR[:63], AFTER_CLEAR + "0", " " + AFTER_CLEAR):
        _host_record(digests, 4242, SPAWN_KEY, confirmation)
        assert resolve_digest_path("after-clear", env=env) == own, confirmation
    record.write_bytes(SPAWN_KEY.encode())  # one line, not even a newline
    assert resolve_digest_path("after-clear", env=env) == own
    # Like `IFS= read -r`: line 2 needs no trailing newline; later lines are ignored.
    record.write_bytes(SPAWN_KEY.encode() + b"\n" + AFTER_CLEAR.encode())
    assert resolve_digest_path("after-clear", env=env) == mapped
    record.write_bytes(SPAWN_KEY.encode() + b"\n" + AFTER_CLEAR.encode() + b"\nanything\n")
    assert resolve_digest_path("after-clear", env=env) == mapped
    record.unlink()
    record.mkdir()
    assert resolve_digest_path("after-clear", env=env) == own
    assert resolve_digest_path("", env=env) is None


def test_resolver_ignores_a_symlinked_record(digests, tmp_path):
    from pseudolife_memory.coordination_identity import resolve_digest_path
    target = tmp_path / "planted.host"
    target.write_bytes(SPAWN_KEY.encode() + b"\n" + AFTER_CLEAR.encode() + b"\n")
    try:
        (digests / "claude-4242.host").symlink_to(target)
    except OSError:
        pytest.skip("symlinks need privileges on this platform")
    env = {"CLAUDE_CODE_SESSION_ID": "after-clear", "CLAUDE_PID": "4242",
           "PSEUDOLIFE_DIGEST_DIR": str(digests)}
    assert resolve_digest_path("after-clear", env=env) == digests / f"{_key('after-clear')}.txt"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_resolver_never_opens_a_fifo_record(digests):
    """Opening a FIFO blocks until a writer appears, before the waiter's
    timeout is even running; only a regular file is read. Run on a daemon
    thread so a regression fails here instead of hanging the suite."""
    from pseudolife_memory.coordination_identity import resolve_digest_path
    os.mkfifo(digests / "claude-4242.host")
    env = {"CLAUDE_CODE_SESSION_ID": "after-clear", "CLAUDE_PID": "4242",
           "PSEUDOLIFE_DIGEST_DIR": str(digests)}
    result = []
    worker = threading.Thread(target=lambda: result.append(resolve_digest_path("after-clear", env=env)),
                              daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "resolve_digest_path blocked on a FIFO record"
    assert result == [digests / f"{_key('after-clear')}.txt"]


def test_resolver_takes_the_digest_directory_from_the_env_it_is_given(tmp_path):
    from pseudolife_memory.coordination_identity import resolve_digest_path
    elsewhere = tmp_path / "given"
    assert (resolve_digest_path("x", env={"PSEUDOLIFE_DIGEST_DIR": str(elsewhere)})
            == elsewhere / f"{_key('x')}.txt")


def test_digest_flag_overrides_the_keying(tmp_path, digests, capsysbinary):
    elsewhere = tmp_path / "elsewhere" / f"{_key('other')}.txt"
    elsewhere.parent.mkdir()
    _write(elsewhere, 4, MAIL)
    assert run_wait_mail(["--digest", str(elsewhere), "--timeout", "5", *FAST]) == 0
    assert _seen(elsewhere).read_text().strip() == "4"


def test_digest_flag_only_names_a_coordination_digest(tmp_path, digests, capsysbinary):
    """A narrow allow rule approves every argument: --digest must not turn an
    auto-approved waiter into a writer of .seen and ledger.log files beside
    arbitrary files that happen to start with a number."""
    notes = tmp_path / "notes" / "notes.txt"
    notes.parent.mkdir()
    _write(notes, 4, MAIL)
    assert run_wait_mail(["--digest", str(notes), "--timeout", "5", *FAST]) == 2
    captured = capsysbinary.readouterr()
    assert captured.out == b"" and b"--digest" in captured.err
    assert sorted(p.name for p in notes.parent.iterdir()) == ["notes.txt"]


# --- setup problems exit 2 with a clear message ------------------------------

def test_missing_digest_file_exits_2_and_says_what_to_do(digests, capsysbinary):
    assert run_wait_mail(["--timeout", "5", *FAST]) == 2
    err = capsysbinary.readouterr().err.decode()
    assert "no digest file" in err
    assert "shim start" in err and "re-arm" in err


def test_no_session_id_exits_2(digests, monkeypatch, capsysbinary):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert run_wait_mail(["--timeout", "5"]) == 2
    err = capsysbinary.readouterr().err.decode()
    assert "--session-id" in err and "CLAUDE_CODE_SESSION_ID" in err


def test_a_digest_that_disappears_mid_wait_exits_2(digests, capsysbinary):
    digest = _digest(digests)
    _write(digest, 1)
    timer = _later(0.1, digest.unlink)
    try:
        assert run_wait_mail(["--timeout", "5", *FAST]) == 2
    finally:
        timer.cancel()
    assert b"disappeared" in capsysbinary.readouterr().err


@pytest.mark.parametrize("args", [
    ["--timeout", "0"], ["--timeout", "-1"], ["--timeout", "86401"], ["--timeout", "nan"],
    ["--timeout", "inf"], ["--interval", "0"], ["--interval", "0.001"], ["--interval", "61"],
    ["--interval", "nan"], ["--session-id", "x", "--digest", "y.txt"], ["--bogus"],
])
def test_bad_arguments_exit_2(args, digests, capsysbinary):
    # Unshown mail is pending, so any of these that slipped through would
    # exit 0 (or, for a NaN timeout, never exit).
    _write(_digest(digests), 1, MAIL)
    assert run_wait_mail(args) == 2
    captured = capsysbinary.readouterr()
    assert captured.out == b"" and b"error:" in captured.err
    assert not _seen(_digest(digests)).exists()


def test_a_stat_error_is_retried_mid_wait_but_stops_the_arming(digests, monkeypatch, capsysbinary):
    """A sharing violation mid-replace (Windows) is transient: the wait looks
    again. At arm time an unreadable digest is a setup problem, exit 2, not
    a traceback."""
    from pseudolife_memory import wait_mail_cli
    digest = _digest(digests)
    _write(digest, 1)
    real_stat = os.stat
    refusals = {"left": 0}

    def flaky(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == digest and refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(13, "sharing violation", str(path))
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(wait_mail_cli.os, "stat", flaky)
    refusals["left"] = 10**9
    assert run_wait_mail(["--timeout", "5", *FAST]) == 2
    assert b"cannot read" in capsysbinary.readouterr().err
    refusals["left"] = 0

    def arrive():
        refusals["left"] = 3
        _write(digest, 2, MAIL)
    timer = _later(0.1, arrive)
    try:
        assert run_wait_mail(["--timeout", "5", *FAST]) == 0
    finally:
        timer.cancel()
    assert capsysbinary.readouterr().out == MAIL.encode("utf-8")


def test_mail_that_cannot_be_written_to_stdout_stays_unshown(digests, monkeypatch):
    # No capsys fixture here: monkeypatch would save capsys's stream as the
    # "original" sys.stdout and restore it after capsys tore it down, leaving
    # a dead stream for every later test (subprocess then fails on fileno).
    import io

    class Closed(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError(32, "Broken pipe")

    class Stdout:
        buffer = Closed()

        def flush(self):
            pass
    digest = _digest(digests)
    _write(digest, 3, MAIL)
    monkeypatch.setattr("sys.stdout", Stdout())
    assert run_wait_mail(["--timeout", "5", *FAST]) == 2
    assert not _seen(digest).exists()
    assert not (digests / "ledger.log").exists()


# --- end to end with the real writer -----------------------------------------

def _one_pending(action, body):
    if action not in {"attach", "heartbeat"}:
        return None
    return httpx.Response(200, json={
        "generation": 3, "lease_until": "later", "pending_count": 1,
        "pending_preview": [{"message_id": "m0", "sender_agent_id": "0" * 32,
                             "sender_label": "reviewer", "created_at": 1789900000.0,
                             "excerpt": "please look"}]})


def test_waits_on_the_file_the_adapter_writes_and_its_hint_then_stays_quiet(tmp_path, capsysbinary):
    """The contract between writer and reader: the adapter's digest makes the
    wait fire, and once the wait has shown the mail, the adapter's
    tool-result hint does not repeat it."""
    async def drive():
        daemon = FakeDaemon()
        daemon.hook = _one_pending
        digest = tmp_path / "d" / f"{_key('e2e')}.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            code = await asyncio.to_thread(
                run_wait_mail, ["--digest", str(digest), "--timeout", "5", *FAST])
            assert code == 0
            assert instance.deliver_hint() is None
            return instance.unread_hint

    expected = asyncio.run(asyncio.wait_for(drive(), 10))
    assert capsysbinary.readouterr().out == (expected + "\n").encode("utf-8")


def test_a_daemon_outage_and_recovery_do_not_wake_a_waiter(monkeypatch, tmp_path, capsysbinary):
    """An outage clears the adapter's cached count but leaves the digest file
    alone, and recovery with the same mailbox renders the same text: the
    watermark stays put, so a waiter armed over mail already shown sleeps
    through a daemon restart instead of waking the session for nothing."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        daemon = FakeDaemon()
        down = {"now": False}

        def hook(action, body):
            if down["now"]:
                raise httpx.ConnectError("daemon restarting")
            return _one_pending(action, body)
        daemon.hook = hook
        digest = tmp_path / "d" / f"{_key('e2e')}.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            assert instance.deliver_hint() is not None  # shown in a tool result
            waiter = asyncio.create_task(asyncio.to_thread(
                run_wait_mail, ["--digest", str(digest), "--timeout", "1.5", *FAST]))
            await asyncio.sleep(0.1)
            down["now"] = True
            await _wait_for(lambda: instance._failure is not None)
            beats = len(daemon.calls)
            down["now"] = False
            await _wait_for(lambda: instance._failure is None and len(daemon.calls) > beats + 3)
            assert await waiter == 3
            assert instance.digest_watermark == 1

    asyncio.run(asyncio.wait_for(drive(), 10))
    assert capsysbinary.readouterr().out == b""


# --- dispatch and cost ---------------------------------------------------------

def test_cli_dispatches_wait_mail_and_passes_its_exit_code(digests, monkeypatch, capsysbinary):
    _write(_digest(digests), 1, MAIL)
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "wait-mail", "--timeout", "5", *FAST])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0
    assert capsysbinary.readouterr().out == MAIL.encode("utf-8")
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "wait-mail", "--timeout", "0.1", *FAST])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 3


def test_import_stays_light():
    """A waiter is re-armed for every wake; it must not load the ML stack."""
    probe = ("import sys, pseudolife_memory.wait_mail_cli; "
             "heavy = [m for m in ('torch', 'sentence_transformers', 'numpy', 'psycopg') "
             "if m in sys.modules]; print(heavy)")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            timeout=60, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
