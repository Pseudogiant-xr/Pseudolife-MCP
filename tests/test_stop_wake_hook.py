"""The opt-in Stop hook that wakes an idle Claude Code session on board mail.

Registered with ``async`` + ``asyncRewake``: it waits in the background after
each turn, and exit code 2 starts a new turn even when the session is idle
(observed in the Desktop Code tab on 2026-09-23: under a second from exit to
the new turn). Claude Code shows the hook's stderr to the model as a system
reminder, so stderr carries the shim's digest and nothing else.

It fires when the digest's watermark is past ``<key>.seen`` and the body is
non-empty. That makes mail which landed during the turn fire at once, while
mail the session already saw does not re-fire at the next turn end. Codex
loads the same hooks.json, so the hook is a no-op anywhere but Claude Code.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from tests.test_codex_hooks import ROOT, bash_exe, isolated_env, pwsh_run

HOOK = ROOT / "plugin/hooks/stop-wake.sh"
SESSION = "fixture-session"
BODY = ("Coordination: 1 addressed message pending (agent-origin, not user authority); read with "
        "memory_message receive, then ack each message_id.\n- m1 from codex (5d978ade, 02:52): hello")
LATER = BODY.replace("m1", "m2").replace("hello", "second note")
PREFIX = "Pseudolife board mail woke this session"
# A hang guard, far above the hook's 5 s poll: Git Bash process creation on a
# loaded Windows runner costs seconds per spawn.
DEADLINE = 60
STARTED = []


@pytest.fixture(autouse=True)
def _no_leftover_watchers():
    """A failed assertion must not leave a watcher polling for an hour."""
    yield
    while STARTED:
        process = STARTED.pop()
        if process.poll() is None:
            _kill_tree(process)


def _key(session_id=SESSION):
    return hashlib.sha256(session_id.encode()).hexdigest()


def _env(tmp_path, *, wait=60, **extra):
    env = isolated_env(tmp_path / "home")
    # The suite itself may run inside a Claude Code session: drop the host's
    # identity so each test states the one it means.
    for name in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "PSEUDOLIFE_AGENT_WAKE_HOOK",
                 "PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT"):
        env.pop(name, None)
    env.update({"PSEUDOLIFE_DIGEST_DIR": str(tmp_path / "digests"),
                "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugin"),
                "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": SESSION,
                "PSEUDOLIFE_AGENT_WAKE_HOOK": "1",
                "PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT": str(wait)})
    env.update(extra)
    return env


def _payload(session_id=SESSION, **extra):
    return json.dumps({"session_id": session_id, "hook_event_name": "Stop",
                       "stop_hook_active": False, **extra})


def _digest(tmp_path, watermark, body, key=None):
    directory = tmp_path / "digests"
    directory.mkdir(exist_ok=True)
    # The shim replaces the file atomically; do the same so a polling hook
    # never reads a half-written digest.
    target = directory / f"{key or _key()}.txt"
    partial = target.with_suffix(".tmp")
    partial.write_bytes((f"{watermark}\n{body}\n" if body else f"{watermark}\n").encode())
    os.replace(partial, target)
    return directory


def _seen(tmp_path, value, key=None):
    (tmp_path / "digests" / f"{key or _key()}.seen").write_text(f"{value}\n")


def _read_seen(tmp_path, key=None):
    path = tmp_path / "digests" / f"{key or _key()}.seen"
    return path.read_text().strip() if path.exists() else None


def _start(env, payload=None):
    process = subprocess.Popen([bash_exe(), str(HOOK)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    process.stdin.write(payload or _payload())
    process.stdin.close()
    STARTED.append(process)
    return process


def _kill_tree(process):
    # Git for Windows' bin/bash.exe is a launcher: killing only it orphans
    # the real bash, which keeps the pipes open and hangs the read.
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    process.kill()
    process.wait()


def _finish(process, timeout=DEADLINE):
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        raise
    return process.returncode, process.stdout.read(), process.stderr.read()


def _run(env, payload=None):
    started = time.monotonic()
    code, out, err = _finish(_start(env, payload))
    return subprocess.CompletedProcess(HOOK, code, out, err), time.monotonic() - started


def _wait_until(predicate, timeout=DEADLINE):
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        try:
            if predicate():
                return True
        except OSError:  # a Windows reader can meet the file mid-replace
            pass
        time.sleep(0.2)
    return False


def _constant(name):
    return int(re.search(rf"^{name}=(\d+)", HOOK.read_text(encoding="utf-8"), re.M)[1])


def _woke(stderr, body=BODY):
    lines = stderr.rstrip("\n").split("\n", 1)
    return lines[0].startswith(PREFIX) and lines[1] == body


# --- registration -----------------------------------------------------------

def _stop_hook():
    hooks = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    [group] = hooks["Stop"]
    [hook] = group["hooks"]
    return hook


def test_hooks_json_binds_stop_as_an_async_rewake_command():
    hook = _stop_hook()
    assert hook["type"] == "command" and hook["async"] is True and hook["asyncRewake"] is True
    assert hook["command"] == ('[ "$PSEUDOLIFE_AGENT_WAKE_HOOK" != 1 ] || '
                               'bash "${CLAUDE_PLUGIN_ROOT}/hooks/stop-wake.sh"')
    # Codex runs commandWindows on Windows; for Stop that is a silent no-op.
    assert hook["commandWindows"].endswith('lifecycle.ps1" -Event Stop')
    # Claude Code enforces the timeout on an asyncRewake hook; the script's own
    # budget stays under it so it always exits before the kill.
    assert hook["timeout"] == 3600 and _constant("MAX_WAIT") < hook["timeout"]


@pytest.mark.parametrize("flag,expected", [("", 0), ("1", 2)])
def test_the_command_itself_holds_the_opt_in(tmp_path, flag, expected):
    """Exit 2 is the wake, and bash also exits 2 on a syntax error: a broken
    copy of the script must not wake every plugin user's session at every
    turn end, so the flag is checked before bash ever reads the script."""
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks/stop-wake.sh").write_bytes(b"if then fi\n")
    env = _env(tmp_path, CLAUDE_PLUGIN_ROOT=plugin.as_posix())
    env["PSEUDOLIFE_AGENT_WAKE_HOOK"] = flag
    result = subprocess.run([bash_exe(), "-c", _stop_hook()["command"]], input=_payload(), env=env,
                            capture_output=True, text=True, timeout=DEADLINE)
    assert result.returncode == expected
    if expected == 0:
        assert (result.stdout, result.stderr) == ("", "")


# --- no-ops -----------------------------------------------------------------

def test_off_unless_the_flag_is_set(tmp_path):
    env = _env(tmp_path)
    env.pop("PSEUDOLIFE_AGENT_WAKE_HOOK")
    _digest(tmp_path, 3, BODY)
    result, _ = _run(env)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert _read_seen(tmp_path) is None


@pytest.mark.parametrize("change", [
    {"CLAUDECODE": None},                                # not Claude Code at all (Codex)
    {"CLAUDE_CODE_SESSION_ID": "the-claude-parent"},     # Codex nested in a Claude Bash tool
])
def test_a_no_op_outside_claude_code(tmp_path, change):
    env = _env(tmp_path)
    for name, value in change.items():
        env.pop(name) if value is None else env.__setitem__(name, value)
    _digest(tmp_path, 3, BODY)
    result, _ = _run(env)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert _read_seen(tmp_path) is None


@pytest.mark.parametrize("session_id", ["fixture session/../x", "x" * 129])
def test_an_odd_session_id_is_refused_at_once(tmp_path, session_id):
    """An id outside the hooks' accepted shape is refused even when a digest
    exists under it; the full budget means a watcher that waited instead
    would trip the hang guard."""
    env = _env(tmp_path, wait=3540, CLAUDE_CODE_SESSION_ID=session_id)
    _digest(tmp_path, 3, BODY, key=_key(session_id))
    result, _ = _run(env, _payload(session_id))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_without_a_digest_directory_it_exits_at_once(tmp_path):
    result, _ = _run(_env(tmp_path, wait=3540))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_a_digest_that_appears_mid_wait_still_wakes(tmp_path):
    """The shim writes the digest on its first heartbeat and rewrites it only
    on change, and the adapter's 24-hour sweep can remove a long-idle
    session's file; the watcher waits for it rather than giving up."""
    (tmp_path / "digests").mkdir()
    process = _start(_env(tmp_path))
    time.sleep(3)
    assert process.poll() is None
    _digest(tmp_path, 1, BODY)
    code, _, err = _finish(process)
    assert code == 2 and _woke(err)


def test_a_digest_that_vanishes_ends_the_watch(tmp_path):
    """The shim removes its digest when it exits: on Windows that is the only
    sign a watcher gets that Claude Code is gone, and a watcher that outlived
    its session must not mark a resumed session's mail as seen."""
    _digest(tmp_path, 3, BODY)
    _seen(tmp_path, 3)
    process = _start(_env(tmp_path, wait=3540))
    assert _wait_until((tmp_path / "digests" / f"{_key()}.wake").exists)
    (tmp_path / "digests" / f"{_key()}.txt").unlink()
    code, out, err = _finish(process, timeout=20)
    assert (code, out, err) == (0, "", "")
    _digest(tmp_path, 1, LATER)
    assert _read_seen(tmp_path) == "3"


def test_without_a_digest_file_it_times_out_quietly(tmp_path):
    (tmp_path / "digests").mkdir()
    result, _ = _run(_env(tmp_path, wait=2))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def _recording_daemon():
    """A fixture daemon that records every request, whatever the method."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _record(self):
            requests.append((self.command, self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = _record

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


def test_lifecycle_ps1_stop_is_a_silent_no_op(tmp_path):
    """Claude never runs commandWindows; Codex does, and must get nothing.
    Without the early exit the Stop event would fall through to the
    SessionEnd request and close the episode at every Codex turn end, which
    prints nothing, so the daemon is what this watches."""
    server, requests = _recording_daemon()
    try:
        env = _env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                   PSEUDOLIFE_MCP_TOKEN="fixture-token")
        _digest(tmp_path, 3, BODY)
        result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "Stop",
                          input=_payload(), env=env)
    finally:
        server.shutdown()
        server.server_close()
    assert (result.stdout, result.stderr, requests) == ("", "", [])
    assert _read_seen(tmp_path) is None


# --- firing -----------------------------------------------------------------

@pytest.mark.parametrize("stop_hook_active", [False, True])
def test_mail_pending_at_arm_time_fires_at_once(tmp_path, stop_hook_active):
    """Mail that landed during the turn wakes the session straight away. The
    Stop after a woken turn carries stop_hook_active=true (observed
    2026-09-23), and must arm the same way."""
    _digest(tmp_path, 3, BODY)
    # The full budget: only an immediate fire gets past the hang guard.
    result, _ = _run(_env(tmp_path, wait=3540), _payload(stop_hook_active=stop_hook_active))
    assert result.returncode == 2 and result.stdout == ""
    assert _woke(result.stderr)
    assert _read_seen(tmp_path) == "3"
    ledger = (tmp_path / "digests" / "ledger.log").read_text().splitlines()
    assert [line.split("\t")[1:4] for line in ledger] == [["wait", _key()[:8], "3"]]


def test_mail_already_seen_waits_for_the_next_change(tmp_path):
    _digest(tmp_path, 3, BODY)
    _seen(tmp_path, 3)
    process = _start(_env(tmp_path))
    time.sleep(3)
    assert process.poll() is None, "fired on mail the session already saw"
    _digest(tmp_path, 4, LATER)
    code, out, err = _finish(process)
    assert (code, out) == (2, "") and _woke(err, LATER)
    assert _read_seen(tmp_path) == "4"


@pytest.mark.parametrize("watermark,body", [(5, ""), ("junk", BODY)])
def test_an_empty_or_malformed_digest_never_fires(tmp_path, watermark, body):
    """Acking every message empties the body and moves the watermark: that
    is news, but not mail."""
    _digest(tmp_path, watermark, body)
    _seen(tmp_path, 3)
    result, _ = _run(_env(tmp_path, wait=3))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert _read_seen(tmp_path) == "3"


def test_times_out_quietly(tmp_path):
    _digest(tmp_path, 3, BODY)
    _seen(tmp_path, 3)
    result, _ = _run(_env(tmp_path, wait=2))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_a_seen_marker_past_the_digest_holds_fire(tmp_path):
    """The prompt hook and the tool-result hint advance the same marker: a
    digest either of them already delivered does not wake the session."""
    _digest(tmp_path, 3, BODY)
    _seen(tmp_path, 9)
    result, _ = _run(_env(tmp_path, wait=2))
    assert (result.returncode, result.stderr) == (0, "")
    assert _read_seen(tmp_path) == "9"


def test_a_marker_that_cannot_advance_never_wakes(tmp_path):
    """A wake that cannot record itself would fire again at every turn end:
    no mark, no wake."""
    _digest(tmp_path, 3, BODY)
    (tmp_path / "digests" / f"{_key()}.seen").mkdir()
    result, _ = _run(_env(tmp_path, wait=2))
    assert (result.returncode, result.stderr) == (0, "")
    assert not list((tmp_path / "digests" / f"{_key()}.seen").iterdir())


def test_wakes_are_capped_per_hour_and_deferred_not_dropped(tmp_path):
    """Two opted-in sessions can keep waking each other; the cap bounds the
    unattended turns that costs. Mail over the cap waits for the window."""
    cap = _constant("MAX_WAKES")
    now = int(time.time())
    wakes = tmp_path / "digests" / f"{_key()}.wakes"
    _digest(tmp_path, 3, BODY)
    wakes.write_bytes("".join(f"{now - 60}\n" for _ in range(cap)).encode())
    result, _ = _run(_env(tmp_path, wait=2))
    assert (result.returncode, result.stderr) == (0, "")
    assert _read_seen(tmp_path) is None, "the capped mail must stay unseen"
    # One of them ages out of the hour: the same mail fires.
    wakes.write_bytes((f"{now - 4000}\n" + "".join(f"{now - 60}\n" for _ in range(cap - 1))).encode())
    result, _ = _run(_env(tmp_path, wait=3540))
    assert result.returncode == 2 and _woke(result.stderr)
    kept = wakes.read_text().split()
    assert len(kept) == cap and str(now - 4000) not in kept


def test_future_dated_wakes_do_not_hold_the_cap_shut(tmp_path):
    """A clock stepped back, or a corrupt entry, must not count forever:
    only a fire rewrites the log, so an entry that never ages would."""
    cap = _constant("MAX_WAKES")
    _digest(tmp_path, 3, BODY)
    future = int(time.time()) + 100_000
    (tmp_path / "digests" / f"{_key()}.wakes").write_bytes(f"{future}\n".encode() * cap)
    result, _ = _run(_env(tmp_path, wait=3540))
    assert result.returncode == 2 and _woke(result.stderr)


def test_a_wake_log_that_is_not_a_file_holds_fire(tmp_path):
    """The cap fails closed, like the marker: something other than a file at
    the log's path could never record a wake."""
    _digest(tmp_path, 3, BODY)
    (tmp_path / "digests" / f"{_key()}.wakes").mkdir()
    result, _ = _run(_env(tmp_path, wait=2))
    assert (result.returncode, result.stderr) == (0, "")
    assert _read_seen(tmp_path) is None


def test_a_corrupt_wake_log_cannot_stop_the_hook(tmp_path):
    _digest(tmp_path, 3, BODY)
    (tmp_path / "digests" / f"{_key()}.wakes").write_bytes(b"089\n0123\r\nnot-a-time\n")
    result, _ = _run(_env(tmp_path, wait=3540))
    assert result.returncode == 2 and _woke(result.stderr)


def test_the_wake_log_is_read_as_decimal(tmp_path):
    """Bash reads a zero-padded number as octal in arithmetic and fails on
    an invalid one ($((N - 089))); a padded entry must count like any other,
    not end the wait."""
    cap = _constant("MAX_WAKES")
    now = int(time.time())
    _digest(tmp_path, 3, BODY)
    lines = [f"{now - 60}"] * (cap - 1) + [f"0{now - 60}"]
    (tmp_path / "digests" / f"{_key()}.wakes").write_bytes(("\n".join(lines) + "\n").encode())
    result, _ = _run(_env(tmp_path, wait=2))
    assert (result.returncode, result.stderr) == (0, "")


# --- one watcher per session ------------------------------------------------

def test_a_newer_firing_retires_the_older_watcher(tmp_path):
    """Every turn end fires the hook again; the newest firing takes the lease
    and the older watcher exits quietly, so mail wakes the session once."""
    _digest(tmp_path, 3, BODY)
    _seen(tmp_path, 3)
    lease = tmp_path / "digests" / f"{_key()}.wake"
    # The full budget, so only retirement (not a timeout) can end it in time.
    older = _start(_env(tmp_path, wait=3540))
    assert _wait_until(lease.exists)
    first_owner = lease.read_text()
    newer = _start(_env(tmp_path))
    assert _wait_until(lambda: lease.read_text() != first_owner)
    code, out, err = _finish(older, timeout=20)
    assert (code, out, err) == (0, "", "")
    _digest(tmp_path, 4, LATER)
    code, out, err = _finish(newer)
    assert code == 2 and _woke(err, LATER)
    ledger = (tmp_path / "digests" / "ledger.log").read_text().splitlines()
    assert len(ledger) == 1


def test_each_session_keeps_its_own_lease(tmp_path):
    other = "another-session"
    _digest(tmp_path, 3, BODY)
    _seen(tmp_path, 3)
    mine = _start(_env(tmp_path))
    assert _wait_until((tmp_path / "digests" / f"{_key()}.wake").exists)
    _digest(tmp_path, 7, LATER, key=_key(other))
    code, _, err = _finish(_start(_env(tmp_path, CLAUDE_CODE_SESSION_ID=other), _payload(other)))
    assert code == 2 and _woke(err, LATER)
    assert mine.poll() is None, "another session's firing retired this one"
    _digest(tmp_path, 4, LATER)
    code, _, err = _finish(mine)
    assert code == 2 and _woke(err, LATER)


# --- keying after /clear ----------------------------------------------------

def _host_record(tmp_path, pid, first_line, confirmed_for="after-clear"):
    """Line 1: the shim's digest key. Line 2: the sha256 of the session id
    SessionStart confirmed that key for."""
    directory = tmp_path / "digests"
    directory.mkdir(exist_ok=True)
    # Bytes, not text: write_text emits CRLF on Windows, which the rule
    # rejects, and the fallback cases below would then pass vacuously.
    second = _key(confirmed_for) if confirmed_for else ""
    (directory / f"claude-{pid}.host").write_bytes(f"{first_line}\n{second}\n".encode())


def test_after_clear_the_host_record_names_the_digest(tmp_path):
    """/clear gives hooks a new session id while the shim keeps writing the
    digest under its spawn-time id; SessionStart records that key per Claude
    Code process in claude-<CLAUDE_PID>.host, confirmed for the new id."""
    spawned = _key("spawn-time-session")
    _host_record(tmp_path, 4242, spawned)
    _digest(tmp_path, 3, BODY, key=spawned)
    env = _env(tmp_path, CLAUDE_PID="4242", CLAUDE_CODE_SESSION_ID="after-clear")
    result, _ = _run(env, _payload("after-clear"))
    assert result.returncode == 2 and _woke(result.stderr)
    assert _read_seen(tmp_path, key=spawned) == "3"


@pytest.mark.parametrize("record", ["not-a-key", "A" * 64, "abc123",
                                    _key("spawn-time-session") + "\r"])  # a CRLF line
def test_a_malformed_host_record_falls_back_to_the_session_id(tmp_path, record):
    _host_record(tmp_path, 4242, record)
    _digest(tmp_path, 3, BODY, key=_key("after-clear"))
    _digest(tmp_path, 5, LATER, key=_key("spawn-time-session"))
    env = _env(tmp_path, wait=10, CLAUDE_PID="4242", CLAUDE_CODE_SESSION_ID="after-clear")
    result, _ = _run(env, _payload("after-clear"))
    assert result.returncode == 2 and _woke(result.stderr)


@pytest.mark.parametrize("confirmed_for", ["an-earlier-session", None])
def test_a_host_record_confirmed_for_another_session_is_not_followed(tmp_path, confirmed_for):
    """A record left by a process whose PID was reused, or one in the older
    one-line form, names another session's digest: following it would wake
    this session for a dead process's mail."""
    _host_record(tmp_path, 4242, _key("spawn-time-session"), confirmed_for=confirmed_for)
    _digest(tmp_path, 3, BODY, key=_key("after-clear"))
    _digest(tmp_path, 5, LATER, key=_key("spawn-time-session"))
    env = _env(tmp_path, wait=10, CLAUDE_PID="4242", CLAUDE_CODE_SESSION_ID="after-clear")
    result, _ = _run(env, _payload("after-clear"))
    assert result.returncode == 2 and _woke(result.stderr)


def test_a_symlinked_host_record_is_refused(tmp_path):
    directory = tmp_path / "digests"
    directory.mkdir()
    target = tmp_path / "elsewhere.host"
    target.write_bytes(f"{_key('spawn-time-session')}\n{_key('after-clear')}\n".encode())
    try:
        (directory / "claude-4242.host").symlink_to(target)
    except OSError:
        pytest.skip("symlinks need privileges on this host")
    _digest(tmp_path, 3, BODY, key=_key("after-clear"))
    _digest(tmp_path, 5, LATER, key=_key("spawn-time-session"))
    env = _env(tmp_path, wait=10, CLAUDE_PID="4242", CLAUDE_CODE_SESSION_ID="after-clear")
    result, _ = _run(env, _payload("after-clear"))
    assert result.returncode == 2 and _woke(result.stderr)


# --- parent liveness (POSIX only: a Windows PID is not visible to kill -0) ---

@pytest.mark.skipif(os.name == "nt", reason="Git Bash cannot probe a Windows PID cheaply")
def test_exits_when_claude_code_is_gone(tmp_path):
    _digest(tmp_path, 3, BODY)
    _seen(tmp_path, 3)
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        # The full budget: only the parent check can end it inside the guard.
        process = _start(_env(tmp_path, wait=3540, CLAUDE_PID=str(parent.pid)))
        time.sleep(3)
        assert process.poll() is None
    finally:
        parent.kill()
        parent.wait()
    code, out, err = _finish(process)
    assert (code, out, err) == (0, "", "")
