"""The Stop hook that wakes an idle Claude Code session on board mail (on by
default since 2026-09-28; ``PSEUDOLIFE_AGENT_WAKE_HOOK=0`` opts out).

Registered with ``async`` + ``asyncRewake``: it waits in the background after
each turn, and exit code 2 starts a new turn even when the session is idle
(observed in the Desktop Code tab on 2026-09-23: under a second from exit to
the new turn). Claude Code shows the hook's stderr to the model as a system
reminder, so stderr carries the shim's digest and nothing else.

It fires when the digest's watermark is past ``<key>.seen`` and the body is
non-empty. That makes mail which landed during the turn fire at once, while
mail the session already saw does not re-fire at the next turn end. Codex
loads the same hooks.json: in Codex context (Windows runs lifecycle.ps1,
macOS and Linux this script) only the park gate runs, answered the way Codex
documents for Stop; anywhere else the hook is a no-op.
"""
import hashlib
import json
import os
import re
import shutil
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


def test_stop_wait_has_a_short_listener_lease_and_disarms_at_exit(tmp_path):
    _digest(tmp_path, 1, "")
    lease = tmp_path / "digests" / f"{_key()}.wake"
    process = _start(_env(tmp_path, wait=5))
    assert _wait_until(lambda: lease.exists())
    token = lease.read_text().strip()
    armed = lease.with_name(f"{_key()}.{token}.wake-armed")
    assert _wait_until(lambda: armed.exists() and len(armed.read_text().splitlines()) == 2)
    token, expiry = armed.read_text().splitlines()
    assert token == lease.read_text().strip()
    assert token and 0 < float(expiry) - time.time() <= 61
    code, out, err = _finish(process)
    assert (code, out, err) == (0, "", "")
    assert lease.read_text().strip() == token
    assert not armed.exists()


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
                 "PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT", "PSEUDOLIFE_AGENT_COORDINATION"):
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


def _ring(tmp_path, watermark, reason="rung anyone", key=None):
    """The marker the shim writes for a ring the daemon decided (v49)."""
    directory = tmp_path / "digests"
    directory.mkdir(exist_ok=True)
    target = directory / f"{key or _key()}.ring"
    partial = target.with_suffix(".tmp")
    partial.write_bytes(f"{watermark}\n{reason}\n".encode())
    os.replace(partial, target)


def _digest(tmp_path, watermark, body, key=None, ring=True):
    """A digest as the shim writes it, with (by default) a ring marker for
    the same watermark: since v49 the hook fires only on a decided ring."""
    directory = tmp_path / "digests"
    directory.mkdir(exist_ok=True)
    # The shim replaces the file atomically; do the same so a polling hook
    # never reads a half-written digest.
    target = directory / f"{key or _key()}.txt"
    partial = target.with_suffix(".tmp")
    partial.write_bytes((f"{watermark}\n{body}\n" if body else f"{watermark}\n").encode())
    os.replace(partial, target)
    if ring:
        _ring(tmp_path, watermark, ring if isinstance(ring, str) else "rung anyone", key)
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
    # On by default (2026-09-28): the command checks the opt-outs and refuses
    # a script that does not parse before bash reads it.
    # Both settings are trimmed and lower-cased as the shim and doctor read
    # them, with a spawn only for a non-empty value.
    assert hook["command"] == (
        'w=$PSEUDOLIFE_AGENT_WAKE_HOOK; [ -z "$w" ] || '
        "w=$(printf %s \"$w\" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'); "
        'case "$w" in 0|false|no|off) exit 0 ;; esac; '
        'c=$PSEUDOLIFE_AGENT_COORDINATION; [ -z "$c" ] || '
        "c=$(printf %s \"$c\" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'); "
        "case \"$c\" in ''|1|true|yes|on) ;; *) exit 0 ;; esac; "
        'bash -n "${CLAUDE_PLUGIN_ROOT}/hooks/stop-wake.sh" 2>/dev/null || exit 0; '
        'exec bash "${CLAUDE_PLUGIN_ROOT}/hooks/stop-wake.sh"')
    # Codex runs commandWindows on Windows; for Stop that is a silent no-op.
    assert hook["commandWindows"].endswith('lifecycle.ps1" -Event Stop')
    # Claude Code enforces the timeout on an asyncRewake hook; the script's own
    # budget stays under it so it always exits before the kill.
    assert hook["timeout"] == 3600 and _constant("MAX_WAIT") < hook["timeout"]


@pytest.mark.parametrize("flag", ["", "1", "0"])
def test_the_command_refuses_a_script_that_does_not_parse(tmp_path, flag):
    """Exit 2 is the wake, and bash also exits 2 on a syntax error. With the
    hook on by default the opt-in flag no longer stands between a broken copy
    of the script and every plugin user's session waking at every turn end,
    so the command syntax-checks the script before bash runs it."""
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks/stop-wake.sh").write_bytes(b"if then fi\n")
    env = _env(tmp_path, CLAUDE_PLUGIN_ROOT=plugin.as_posix())
    env["PSEUDOLIFE_AGENT_WAKE_HOOK"] = flag
    result = subprocess.run([bash_exe(), "-c", _stop_hook()["command"]], input=_payload(), env=env,
                            capture_output=True, text=True, timeout=DEADLINE)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("change,expected", [
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "0 "}, 0),             # trimmed, as the shim and doctor read it
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "\tOff"}, 0),
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "  "}, 2),              # blank is unset: on
    ({"PSEUDOLIFE_AGENT_COORDINATION": " 1 "}, 2),
    ({"PSEUDOLIFE_AGENT_COORDINATION": "NO"}, 0),
    ({"PSEUDOLIFE_AGENT_COORDINATION": " "}, 2),
])
def test_the_command_and_script_trim_and_fold_case(tmp_path, change, expected):
    """doctor and the shim strip and lower-case these settings; the hook
    must read the same value the same way, in its command and again in the
    script."""
    # One digest directory each: a run that fires marks the mail seen.
    for where, via_command in ((tmp_path / "command", True), (tmp_path / "script", False)):
        where.mkdir()
        env = _env(where, wait=3)
        env.update(change)
        _digest(where, 3, BODY)
        if via_command:
            result = subprocess.run([bash_exe(), "-c", _stop_hook()["command"]], input=_payload(),
                                    env=env, capture_output=True, text=True, timeout=DEADLINE)
        else:
            result, _ = _run(env)
        assert result.returncode == expected, (via_command, result.stderr)


@pytest.mark.parametrize("change,expected", [
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": None}, 2),              # unset: on by default
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "1"}, 2),               # the old opt-in still works
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "0"}, 0),
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "Off"}, 0),
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "false"}, 0),
    ({"PSEUDOLIFE_AGENT_COORDINATION": "0"}, 0),            # the master off switch
    ({"PSEUDOLIFE_AGENT_COORDINATION": "no"}, 0),
    ({"PSEUDOLIFE_AGENT_COORDINATION": "1"}, 2),
])
def test_the_command_holds_the_opt_outs(tmp_path, change, expected):
    """The hooks.json command decides before bash reads the script, and the
    script checks the same two settings again (test_on_unless_told_off)."""
    env = _env(tmp_path)
    for name, value in change.items():
        env.pop(name, None) if value is None else env.__setitem__(name, value)
    _digest(tmp_path, 3, BODY)
    result = subprocess.run([bash_exe(), "-c", _stop_hook()["command"]], input=_payload(), env=env,
                            capture_output=True, text=True, timeout=DEADLINE)
    assert result.returncode == expected, result.stderr
    assert (result.returncode == 2) == _woke(result.stderr)


@pytest.mark.skipif(os.name == "nt", reason="Claude Code runs hook commands in Git Bash on Windows")
@pytest.mark.parametrize("flag,expected", [("", 2), ("0", 0)])
def test_the_command_runs_under_posix_sh(tmp_path, flag, expected):
    """Claude Code runs a hook command with ``sh -c`` on Linux and macOS,
    which is dash on Debian and Ubuntu: the command must be POSIX sh, not
    bash, as well as honour the opt-out there."""
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX sh on this host")
    env = _env(tmp_path, PSEUDOLIFE_AGENT_WAKE_HOOK=flag)
    _digest(tmp_path, 3, BODY)
    result = subprocess.run([shell, "-c", _stop_hook()["command"]], input=_payload(), env=env,
                            capture_output=True, text=True, timeout=DEADLINE)
    assert result.returncode == expected, result.stderr


# --- no-ops -----------------------------------------------------------------

@pytest.mark.parametrize("change,expected", [
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": None}, 2),
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "0"}, 0),
    ({"PSEUDOLIFE_AGENT_WAKE_HOOK": "FALSE"}, 0),
    ({"PSEUDOLIFE_AGENT_COORDINATION": "0"}, 0),
    ({"PSEUDOLIFE_AGENT_COORDINATION": "yes"}, 2),
])
def test_on_unless_told_off(tmp_path, change, expected):
    env = _env(tmp_path)
    for name, value in change.items():
        env.pop(name, None) if value is None else env.__setitem__(name, value)
    _digest(tmp_path, 3, BODY)
    result, _ = _run(env)
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert (result.stdout, result.stderr) == ("", "")
        assert _read_seen(tmp_path) is None
    else:
        assert _woke(result.stderr) and _read_seen(tmp_path) == "3"


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
    new_owner = lease.read_text().strip()
    new_listener = lease.with_name(f"{_key()}.{new_owner}.wake-armed")
    assert _wait_until(new_listener.exists)
    code, out, err = _finish(older, timeout=20)
    assert (code, out, err) == (0, "", "")
    assert lease.read_text().strip() == new_owner
    assert new_listener.exists()
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


# --- parent liveness -------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="kill -0 cannot see a Windows PID; see the test below")
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



# --- the daemon decides, the hook rings (v49) ------------------------------

def test_a_digest_change_without_a_ring_does_not_wake(tmp_path):
    """New mail alone no longer wakes the session: the shim writes
    ``<key>.ring`` only for a ring the daemon decided (chatter to a parked
    session is withheld), and the hook fires only on a ring past ``.seen``."""
    _digest(tmp_path, 3, BODY, ring=False)
    result, _ = _run(_env(tmp_path, wait=8))
    assert (result.returncode, result.stderr) == (0, "")
    assert _read_seen(tmp_path) is None


def test_a_ring_names_its_reason_in_the_ledger(tmp_path):
    _digest(tmp_path, 3, BODY, ring="rung clears")
    result, _ = _run(_env(tmp_path, wait=3540))
    assert result.returncode == 2 and _woke(result.stderr)
    ledger = (tmp_path / "digests" / "ledger.log").read_text().splitlines()
    assert [line.split("\t")[1:4] + line.split("\t")[5:] for line in ledger] == [
        ["wait", _key()[:8], "3", "rung clears"]]


def test_a_ring_for_a_digest_already_seen_holds_fire(tmp_path):
    """The session read the mail while active (the prompt hook or a hint
    moved ``.seen``): the ring is stale and the hook waits for the next."""
    _digest(tmp_path, 3, BODY, ring="rung anyone")
    _seen(tmp_path, 3)
    result, _ = _run(_env(tmp_path, wait=8))
    assert (result.returncode, result.stderr) == (0, "")


def test_a_ring_behind_a_newer_digest_still_wakes(tmp_path):
    """Chatter arriving after the ring changes the digest (a new watermark)
    but the rung mail is still unseen, so the wake prints the current digest."""
    _digest(tmp_path, 5, LATER, ring=False)
    _ring(tmp_path, 4, "rung clearer")
    result, _ = _run(_env(tmp_path, wait=3540))
    assert result.returncode == 2 and _woke(result.stderr, LATER)
    assert _read_seen(tmp_path) == "5"


def test_a_nudged_ring_never_wakes(tmp_path):
    """Regular mail never wakes (maintainer decision 2026-10-02): a
    ``nudged`` marker (an idle session that never parked), left by a shim
    from before the change, is no ring. Only ``rung`` fires."""
    _digest(tmp_path, 3, BODY, ring="nudged no_park")
    result, _ = _run(_env(tmp_path, wait=8))
    assert (result.returncode, result.stderr) == (0, "")
    assert _read_seen(tmp_path) != "3"


@pytest.mark.parametrize("ring", ["", "x\nrung anyone\n", "3\n", "3\n\n"])
def test_a_malformed_ring_never_wakes(tmp_path, ring):
    _digest(tmp_path, 3, BODY, ring=False)
    (tmp_path / "digests" / f"{_key()}.ring").write_text(ring)
    result, _ = _run(_env(tmp_path, wait=8))
    assert (result.returncode, result.stderr) == (0, "")


# --- the park gate -----------------------------------------------------------

GATE_MESSAGE = ("Before ending: update your board status with why you stopped and what you need "
                "(memory_agents update park_reason=... park_needs=... park_clear_by=... "
                "park_resume=...). Use done only when no follow-up is expected: nothing will ring "
                "you. Waiting on a merge click or a review that may still bring fixes? Park "
                "needs_approval with park_clear_by set to the reviewer's agent id or maintainer, "
                "or waiting_peer. A park records intent; automatic wake requires a live listener. "
                "Check the sender's wake receipt; no_path means mail is queued for receive on a later turn. "
                "For waits over 59 minutes, especially needs_approval waiting on maintainer, arm wait-mail "
                "in the background or keep the Codex doorbell active; otherwise record that you are "
                "reachable on your next turn.")


def test_the_served_park_gate_prompt_names_the_followup_distinction():
    from pseudolife_memory.coordination import PARK_GATE_MESSAGE
    assert PARK_GATE_MESSAGE == GATE_MESSAGE


@pytest.mark.parametrize("surface", ["checkin_sentence", "configuration", "stop_hook_quote"])
def test_the_park_guidance_keeps_done_distinct_from_followup(surface):
    from pseudolife_memory.coordination import PARK_CHECKIN_SENTENCE
    if surface == "checkin_sentence":
        text = PARK_CHECKIN_SENTENCE
    else:
        text = (ROOT / "docs/guide/configuration.md").read_text(encoding="utf-8")
        if surface == "configuration":
            text = text.split("### Park records and the wake decision", 1)[1]
            text = text.split("An omitted field stays", 1)[0]
        else:
            text = text.split("### Waking an idle Claude Code session: the Stop hook", 1)[1]
            text = text.split("park_resume=...)", 1)[1].split('" as the wake text', 1)[0]
    guidance = GATE_MESSAGE.split(")", 1)[1].split(" A park records intent;", 1)[0].strip(". ")
    assert guidance in " ".join(text.replace("`", "").split())


def _gate_daemon(answer, status=200, stall=0):
    """A fixture daemon answering the park gate with ``answer`` (and
    ``status``) and recording each request's path and bearer. ``stall``
    promises more body than it sends and holds the connection that many
    seconds, so the client's time limit cuts the answer off."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            body = answer.encode("utf-8")
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body) + (100 if stall else 0)))
            self.end_headers()
            self.wfile.write(body)
            if stall:
                self.wfile.flush()
                time.sleep(stall)

        def do_POST(self):
            # The woke marker; recorded with its method so a test can tell
            # it from the gate's GET.
            requests.append(("POST " + self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


def _agent(tmp_path, agent_id="a" * 32, key=None):
    directory = tmp_path / "digests"
    directory.mkdir(exist_ok=True)
    (directory / f"{key or _key()}.agent").write_text(f"{agent_id}\n")


def _turn(tmp_path, stamp, key=None):
    (tmp_path / "digests" / f"{key or _key()}.turn").write_text(f"{stamp}\n")


def _gate_env(tmp_path, server, **extra):
    return _env(tmp_path, wait=8, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                PSEUDOLIFE_MCP_TOKEN="fixture-token", **extra)


@pytest.mark.parametrize("daemon_message", [GATE_MESSAGE, ""], ids=["served", "fallback"])
def test_an_unparked_session_is_asked_once_to_park(tmp_path, daemon_message):
    """At turn end the hook asks the daemon whether this address parked; a
    block ends the turn with the request as the wake text, at once, and
    the turn that follows (stop_hook_active) is not asked again."""
    server, requests = _gate_daemon("block\n" + daemon_message + "\n")
    try:
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        _turn(tmp_path, 1700000000)
        result, elapsed = _run(_gate_env(tmp_path, server))
        assert result.returncode == 2 and result.stdout == ""
        assert result.stderr == GATE_MESSAGE + "\n"
        assert elapsed < 8
        assert requests == [("/api/hook/park-gate?agent=" + "a" * 32 + "&since=1700000000",
                             "Bearer fixture-token")]
        ledger = (tmp_path / "digests" / "ledger.log").read_text().splitlines()
        assert [line.split("\t")[1:2] + line.split("\t")[5:] for line in ledger] == [
            ["gate", "block"]]
        result, _ = _run(_gate_env(tmp_path, server), _payload(stop_hook_active=True))
        assert (result.returncode, result.stderr) == (0, "")
        assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_a_firing_hook_posts_a_woke_marker_for_its_board_address(tmp_path):
    """After a wake fires, and before it exits, the hook tells the daemon
    that this address's turn is starting (``POST /api/hook/woke?agent=<id>``,
    the same bearer and URL as the gate), so wake precision can be measured
    from the turn rather than from the ring being served. Nothing else goes
    with it, and it runs in the background, so a daemon that hangs cannot
    hold the wake back."""
    server, requests = _gate_daemon("allow\n")
    try:
        _digest(tmp_path, 3, BODY)
        _agent(tmp_path)
        result, _ = _run(_gate_env(tmp_path, server), _payload(stop_hook_active=True))
        assert result.returncode == 2 and _woke(result.stderr)
        # In the background, so it may land just after the hook exits. A
        # continuation's Stop is not gated, so the marker is the only call.
        assert _wait_until(lambda: requests)
        assert requests == [("POST /api/hook/woke?agent=" + "a" * 32, "Bearer fixture-token")]
        assert _read_seen(tmp_path) == "3"
    finally:
        server.shutdown()
        server.server_close()
    # Without a board address there is nothing to mark; the wake still fires.
    server, requests = _gate_daemon("allow\n")
    try:
        (tmp_path / "digests" / f"{_key()}.agent").unlink()
        (tmp_path / "digests" / f"{_key()}.seen").unlink()
        result, _ = _run(_gate_env(tmp_path, server), _payload(stop_hook_active=True))
        assert result.returncode == 2 and _woke(result.stderr)
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()


def test_a_woke_marker_the_daemon_does_not_take_still_wakes(tmp_path):
    server, _ = _gate_daemon("allow\n")
    port = server.server_port
    server.shutdown()
    server.server_close()
    _digest(tmp_path, 3, BODY)
    _agent(tmp_path)
    env = _env(tmp_path, wait=8, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{port}",
               PSEUDOLIFE_MCP_TOKEN="fixture-token")
    result, _ = _run(env, _payload(stop_hook_active=True))
    assert result.returncode == 2 and _woke(result.stderr)


def test_a_parked_session_ends_its_turn_quietly(tmp_path):
    server, requests = _gate_daemon("allow\n")
    try:
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        result, _ = _run(_gate_env(tmp_path, server))
        assert (result.returncode, result.stderr) == (0, "")
        assert requests == [("/api/hook/park-gate?agent=" + "a" * 32, "Bearer fixture-token")]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("agent", ["", "not an id\n", "A" * 32 + "\n", "a" * 31 + "\n"])
def test_without_a_board_address_the_gate_is_not_asked(tmp_path, agent):
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        _digest(tmp_path, 3, "", ring=False)
        if agent:
            (tmp_path / "digests" / f"{_key()}.agent").write_text(agent)
        result, _ = _run(_gate_env(tmp_path, server))
        assert (result.returncode, result.stderr, requests) == (0, "", [])
    finally:
        server.shutdown()
        server.server_close()


def test_a_gate_the_daemon_does_not_answer_is_open(tmp_path):
    server, _ = _gate_daemon("allow\n")
    port = server.server_port
    server.shutdown()
    server.server_close()
    _digest(tmp_path, 3, "", ring=False)
    _agent(tmp_path)
    env = _env(tmp_path, wait=8, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{port}",
               PSEUDOLIFE_MCP_TOKEN="fixture-token")
    result, _ = _run(env)
    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes; Windows ACLs tested separately")
def test_the_gate_reads_a_private_token_file(tmp_path):
    server, requests = _gate_daemon("allow\n")
    try:
        token_file = tmp_path / "token"
        token_file.write_text("file-token\n")
        token_file.chmod(0o600)
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        env = _env(tmp_path, wait=8, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                   PSEUDOLIFE_MCP_TOKEN_FILE=str(token_file))
        env.pop("PSEUDOLIFE_MCP_TOKEN", None)
        result, _ = _run(env)
        assert (result.returncode, result.stderr) == (0, "")
        assert [bearer for _, bearer in requests] == ["Bearer file-token"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
@pytest.mark.parametrize("private", [True, False])
def test_windows_gate_validates_the_installer_token_file(tmp_path, private):
    from pseudolife_memory.credentials import _write_token_file
    token_file = tmp_path / "token ' with spaces"
    if private:
        _write_token_file(token_file, "file-token")
    else:
        token_file.write_bytes(b"file-token\n")
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        env = _env(tmp_path, wait=0,
                   PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                   PSEUDOLIFE_MCP_TOKEN_FILE=str(token_file))
        env.pop("PSEUDOLIFE_MCP_TOKEN", None)
        result, _ = _run(env)
        assert result.returncode == (2 if private else 0)
        assert [bearer for _, bearer in requests] == (["Bearer file-token"] if private else [])
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
@pytest.mark.parametrize("guard", ["owner", "protected", "shared", "links", "size", "empty"])
def test_windows_private_file_guards_are_load_bearing(tmp_path, guard):
    from pseudolife_memory.credentials import _secure_windows_file, _write_token_file
    directory = tmp_path / "private"
    directory.mkdir()
    _secure_windows_file(directory)
    token_file = directory / "token"
    _write_token_file(token_file, "fixture-token")
    function = re.search(r"(?ms)^private_regular\(\) \{.*?^\}",
                         HOOK.read_text(encoding="utf-8"))[0]
    edits = {
        "owner": ("$owner -ne $current", "$false"),
        "protected": ("-not $acl.AreAccessRulesProtected", "$false"),
        "shared": ('$sid -notin $owner, "S-1-3-4"', "$false"),
        "links": ('[ "${3:-0}" = 1 ] || return 1', ":"),
        "size": ('[ "$(wc -c < "$path" 2>/dev/null || echo $((maximum + 1)))" -le "$maximum" ] || return 1', ":"),
        "empty": ("$item.Length -lt 1", "$false"),
    }
    if guard == "owner":
        # A different caller identity, without requiring host privileges to
        # transfer fixture ownership to another account.
        function = function.replace(
            "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value", '"S-1-5-18"')
    elif guard in ("protected", "shared"):
        import ctypes
        from ctypes import wintypes
        # Apply only the DACL. Set-Acl can also try to write the SACL, which
        # requires SeSecurityPrivilege even for a file the caller owns.
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        apply = advapi.SetFileSecurityW
        apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        descriptor = ctypes.c_void_p()
        sddl = "D:(A;;FA;;;OW)" if guard == "protected" else "D:P(A;;FA;;;OW)(A;;FR;;;WD)"
        assert convert(sddl, 1, ctypes.byref(descriptor), None)
        try:
            flags = 0x20000004 if guard == "protected" else 0x80000004
            assert apply(str(token_file), flags, descriptor)
        finally:
            kernel = ctypes.WinDLL("kernel32")
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(descriptor)
    elif guard == "links":
        os.link(token_file, directory / "alias")
    else:
        token_file.write_bytes(b"" if guard == "empty" else b"x" * 4097)
    env = _env(tmp_path, PSEUDOLIFE_TEST_FILE=str(token_file))
    try:
        for mutated, expected in ((False, 1), (True, 0)):
            candidate = function.replace(*edits[guard]) if mutated else function
            result = subprocess.run(
                [bash_exe(), "-c", candidate + '\nprivate_regular "$PSEUDOLIFE_TEST_FILE" 4096'],
                env=env, capture_output=True, text=True, timeout=DEADLINE)
            assert result.returncode == expected, (guard, mutated, result.stderr)
    finally:
        _secure_windows_file(token_file)


def test_windows_acl_blocks_are_byte_identical():
    pattern = rb"(?ms)^        PSEUDOLIFE_PRIVATE_FILE=.*?^        return \$\?"
    expected = re.search(pattern, HOOK.read_bytes())[0]
    for name in ("coordination-start.sh", "session-start.sh", "session-end.sh"):
        assert re.search(pattern, (HOOK.parent / name).read_bytes())[0] == expected, name


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_gate_records_a_rejected_junction_token_without_a_request(tmp_path):
    from pseudolife_memory.credentials import _write_token_file
    directory = tmp_path / "private"
    directory.mkdir()
    _write_token_file(directory / "token", "fixture-token")
    junction = tmp_path / "junction"
    server, requests = _gate_daemon("allow\n")
    env = _gate_env(tmp_path, server, PSEUDOLIFE_MCP_TOKEN_FILE=str(junction / "token"))
    env.update(PSEUDOLIFE_TEST_JUNCTION=str(junction), PSEUDOLIFE_TEST_TARGET=str(directory),
               PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT="0")
    try:
        pwsh_run("-Command", 'New-Item -ItemType Junction -Path $env:PSEUDOLIFE_TEST_JUNCTION '
                 '-Target $env:PSEUDOLIFE_TEST_TARGET -ErrorAction Stop | Out-Null', env=env)
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr, requests) == (0, "", "", [])
        lines = (tmp_path / "digests" / "ledger.log").read_text().splitlines()
        assert len(lines) == 1
        stamp, *fields = lines[0].split("\t")
        assert stamp.isdigit() and fields == ["token", _key()[:8], "0", "0", "rejected"]
    finally:
        if junction.exists():
            os.rmdir(junction)  # Remove only the junction, preserving its target.
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
@pytest.mark.parametrize("unsafe", ["junction", "missing-validator"])
def test_windows_private_file_fails_closed(tmp_path, unsafe):
    from pseudolife_memory.credentials import _write_token_file
    directory = tmp_path / "private"
    directory.mkdir()
    token_file = directory / "token"
    _write_token_file(token_file, "fixture-token")
    env = _env(tmp_path, PSEUDOLIFE_TEST_FILE=str(token_file))
    function = re.search(r"(?ms)^private_regular\(\) \{.*?^\}",
                         HOOK.read_text(encoding="utf-8"))[0]
    junction = tmp_path / "junction"
    if unsafe == "junction":
        env.update(PSEUDOLIFE_TEST_JUNCTION=str(junction), PSEUDOLIFE_TEST_TARGET=str(directory))
        pwsh_run("-Command", 'New-Item -ItemType Junction -Path $env:PSEUDOLIFE_TEST_JUNCTION '
                 '-Target $env:PSEUDOLIFE_TEST_TARGET -ErrorAction Stop | Out-Null', env=env)
        env["PSEUDOLIFE_TEST_FILE"] = str(junction / "token")
    else:
        function = 'powershell.exe() { return 1; }\n' + function
    try:
        result = subprocess.run(
            [bash_exe(), "-c", function + '\nprivate_regular "$PSEUDOLIFE_TEST_FILE" 4096'],
            env=env, capture_output=True, text=True, timeout=DEADLINE)
        assert result.returncode == 1 and result.stdout == "" and result.stderr == ""
    finally:
        if junction.exists():
            os.rmdir(junction)  # Remove the junction itself, never its target.


def test_the_prompt_hook_stamps_the_turn_start(tmp_path):
    """The gate's ``since`` is when the turn began, which only the prompt
    hook knows; it leaves the stamp beside the digest."""
    from tests.test_codex_hooks import bash_run
    _digest(tmp_path, 3, "", ring=False)
    env = _env(tmp_path)
    before = int(time.time())
    result = bash_run(ROOT / "plugin/hooks/coordination-prompt.sh",
                      input=json.dumps({"session_id": SESSION, "prompt": "hi"}), env=env)
    assert result.returncode == 0
    stamp = (tmp_path / "digests" / f"{_key()}.turn").read_text().strip()
    assert stamp.isdigit() and before <= int(stamp) <= int(time.time())
    result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "CoordinationPrompt",
                      input=json.dumps({"session_id": SESSION, "prompt": "hi"}), env=env)
    assert result.returncode == 0
    stamp = (tmp_path / "digests" / f"{_key()}.turn").read_text().strip()
    assert stamp.isdigit() and before <= int(stamp) <= int(time.time())


def test_lifecycle_ps1_stop_asks_an_unparked_codex_thread_to_park(tmp_path):
    """Codex runs the native command synchronously: the gate answers with
    the Stop decision Codex documents (JSON on stdout, exit 0), and stays
    silent when the daemon allows or the turn was already continued."""
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        env = _env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                   PSEUDOLIFE_MCP_TOKEN="fixture-token")
        _digest(tmp_path, 3, BODY, ring=False)
        _agent(tmp_path)
        _turn(tmp_path, 1700000000)
        result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "Stop",
                          input=_payload(), env=env)
        assert result.returncode == 0 and result.stderr == ""
        assert json.loads(result.stdout) == {"decision": "block", "reason": GATE_MESSAGE}
        assert requests == [("/api/hook/park-gate?agent=" + "a" * 32 + "&since=1700000000",
                             "Bearer fixture-token")]
        ledger = (tmp_path / "digests" / "ledger.log").read_text().splitlines()
        assert [line.split("\t")[1:2] + line.split("\t")[5:] for line in ledger] == [
            ["gate", "block"]]
        result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "Stop",
                          input=_payload(stop_hook_active=True), env=env)
        assert (result.returncode, result.stdout, len(requests)) == (0, "", 1)
    finally:
        server.shutdown()
        server.server_close()
    server, requests = _gate_daemon("allow\n")
    try:
        env = _env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                   PSEUDOLIFE_MCP_TOKEN="fixture-token")
        result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "Stop",
                          input=_payload(), env=env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()
    # The .seen marker is the wake hook's, and Codex has none: untouched.
    assert _read_seen(tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_token_file_others_can_read_is_refused(tmp_path):
    """The gate reads a bearer file with the checks the sibling hooks
    apply (owner-only, one link, bounded): a readable one is not used."""
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        token_file = tmp_path / "token"
        token_file.write_text("file-token\n")
        token_file.chmod(0o644)
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        env = _env(tmp_path, wait=8, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                   PSEUDOLIFE_MCP_TOKEN_FILE=str(token_file))
        env.pop("PSEUDOLIFE_MCP_TOKEN", None)
        result, _ = _run(env)
        assert (result.returncode, result.stderr, requests) == (0, "", [])
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("setting", [("PSEUDOLIFE_AGENT_WAKE_HOOK", "0"),
                                     ("PSEUDOLIFE_AGENT_WAKE_HOOK", "off"),
                                     ("PSEUDOLIFE_AGENT_COORDINATION", "0")])
def test_the_codex_park_gate_honours_an_explicit_opt_out(tmp_path, setting):
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        env = _env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                   PSEUDOLIFE_MCP_TOKEN="fixture-token")
        env[setting[0]] = setting[1]
        _digest(tmp_path, 3, BODY, ring=False)
        _agent(tmp_path)
        result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "Stop",
                          input=_payload(), env=env)
        assert (result.returncode, result.stdout, result.stderr, requests) == (0, "", "", [])
    finally:
        server.shutdown()
        server.server_close()


# --- the Codex gate in the bash hook (macOS and Linux) ----------------------

def _codex_env(tmp_path, **extra):
    """The environment Codex gives the bash Stop command on macOS and
    Linux: the plugin root, no Claude Code identity, and the marker the
    sibling bash hooks read for Codex context."""
    env = _env(tmp_path, wait=8, **extra)
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        env.pop(name, None)
    env["PSEUDOLIFE_CODEX_HOOK"] = "1"
    return env


def test_stop_wake_sh_asks_an_unparked_codex_thread_to_park(tmp_path):
    """Codex on macOS and Linux runs the bash command, not lifecycle.ps1.
    In Codex context the script runs the same park gate the native command
    runs on Windows: one request, Codex's documented Stop decision on
    stdout (exit 0) for a block, nothing otherwise, and never Claude's
    wake wait (no lease, no marker, an exit well inside the wait)."""
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        _digest(tmp_path, 3, BODY, ring=False)
        _agent(tmp_path)
        _turn(tmp_path, 1700000000)
        env = _codex_env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                         PSEUDOLIFE_MCP_TOKEN="fixture-token")
        result, elapsed = _run(env)
        assert (result.returncode, result.stderr) == (0, "")
        assert json.loads(result.stdout) == {"decision": "block", "reason": GATE_MESSAGE}
        assert elapsed < 8
        assert requests == [("/api/hook/park-gate?agent=" + "a" * 32 + "&since=1700000000",
                             "Bearer fixture-token")]
        assert not (tmp_path / "digests" / f"{_key()}.wake").exists()
        assert _read_seen(tmp_path) is None
        ledger = (tmp_path / "digests" / "ledger.log").read_text().splitlines()
        assert [line.split("\t")[1:2] + line.split("\t")[5:] for line in ledger] == [
            ["gate", "block"]]
        result, _ = _run(env, _payload(stop_hook_active=True))
        assert (result.returncode, result.stdout, result.stderr, len(requests)) == (0, "", "", 1)
    finally:
        server.shutdown()
        server.server_close()
    server, requests = _gate_daemon("allow\n")
    try:
        env = _codex_env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                         PSEUDOLIFE_MCP_TOKEN="fixture-token")
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert requests == [("/api/hook/park-gate?agent=" + "a" * 32 + "&since=1700000000",
                             "Bearer fixture-token")]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("message, reason", [
    ('say "park" \\ now', 'say "park" \\ now'),
    ("line one\nline two\n\n", "line one\nline two"),
    ("", GATE_MESSAGE),
    ("tab\there \x01 control", GATE_MESSAGE),
])
def test_the_codex_bash_gate_quotes_the_daemons_message_as_json(tmp_path, message, reason):
    """The reason is the daemon's text as a JSON string: quotes and
    backslashes escaped, newlines kept; an empty message, or one carrying
    a control character the script does not escape, gets the fixed text."""
    server, _ = _gate_daemon("block\n" + message + "\n")
    try:
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        env = _codex_env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                         PSEUDOLIFE_MCP_TOKEN="fixture-token")
        result, _ = _run(env)
        assert (result.returncode, result.stderr) == (0, "")
        assert json.loads(result.stdout) == {"decision": "block", "reason": reason}
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("setting", [("PSEUDOLIFE_AGENT_WAKE_HOOK", "0"),
                                     ("PSEUDOLIFE_AGENT_WAKE_HOOK", "off"),
                                     ("PSEUDOLIFE_AGENT_COORDINATION", "0")])
def test_the_codex_bash_gate_honours_an_explicit_opt_out(tmp_path, setting):
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        _digest(tmp_path, 3, BODY, ring=False)
        _agent(tmp_path)
        env = _codex_env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                         PSEUDOLIFE_MCP_TOKEN="fixture-token")
        env[setting[0]] = setting[1]
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr, requests) == (0, "", "", [])
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("agent", ["", "not an id\n", "A" * 32 + "\n"])
def test_the_codex_bash_gate_needs_a_board_address(tmp_path, agent):
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        _digest(tmp_path, 3, "", ring=False)
        if agent:
            (tmp_path / "digests" / f"{_key()}.agent").write_text(agent)
        env = _codex_env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                         PSEUDOLIFE_MCP_TOKEN="fixture-token")
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr, requests) == (0, "", "", [])
    finally:
        server.shutdown()
        server.server_close()


def test_the_codex_bash_gate_is_open_without_an_answer(tmp_path):
    server, _ = _gate_daemon("allow\n")
    port = server.server_port
    server.shutdown()
    server.server_close()
    _digest(tmp_path, 3, "", ring=False)
    _agent(tmp_path)
    env = _codex_env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{port}",
                     PSEUDOLIFE_MCP_TOKEN="fixture-token")
    result, elapsed = _run(env)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert elapsed < 8


def test_codex_context_is_read_from_the_plugin_root_codex_sets(tmp_path):
    """The plugin's Stop entry carries no marker: Codex is recognised, as
    in the sibling hooks, by PLUGIN_ROOT equal to CLAUDE_PLUGIN_ROOT."""
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n")
    try:
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        env = _codex_env(tmp_path, PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                         PSEUDOLIFE_MCP_TOKEN="fixture-token")
        del env["PSEUDOLIFE_CODEX_HOOK"]
        env["PLUGIN_ROOT"] = env["CLAUDE_PLUGIN_ROOT"]
        result, _ = _run(env)
        assert (result.returncode, result.stderr) == (0, "")
        assert json.loads(result.stdout) == {"decision": "block", "reason": GATE_MESSAGE}
        assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("marker", [("PLUGIN_ROOT", None), ("PSEUDOLIFE_CODEX_HOOK", "1")])
def test_claude_codes_own_session_is_never_taken_for_codex(tmp_path, marker):
    """A hook Claude Code started for this session (CLAUDECODE=1 and its
    session id in the environment) stays Claude's, whatever Codex marker
    leaks into it: the wake still fires, on stderr with exit 2."""
    _digest(tmp_path, 3, BODY)
    env = _env(tmp_path)
    env[marker[0]] = marker[1] or env["CLAUDE_PLUGIN_ROOT"]
    result, _ = _run(env)
    assert result.returncode == 2 and result.stdout == ""
    assert _woke(result.stderr)


@pytest.mark.parametrize("codex", [False, True])
def test_a_gate_answer_that_is_not_a_plain_success_is_open(tmp_path, codex):
    """A redirect is not the daemon's answer: its body must not block, on
    either client (curl -f passes a 3xx through unless -L is set)."""
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n", status=302)
    try:
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        extra = dict(PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                     PSEUDOLIFE_MCP_TOKEN="fixture-token")
        env = _codex_env(tmp_path, **extra) if codex else _gate_env(tmp_path, server)
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("codex", [False, True])
def test_an_answer_cut_off_at_the_time_limit_is_open(tmp_path, codex):
    """curl prints what arrived before its 2 s limit and then fails: a
    truncated "block" is not the daemon's answer and must not block."""
    server, requests = _gate_daemon("block\n" + GATE_MESSAGE + "\n", stall=4)
    try:
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        extra = dict(PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                     PSEUDOLIFE_MCP_TOKEN="fixture-token")
        env = _codex_env(tmp_path, **extra) if codex else _gate_env(tmp_path, server)
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("client", ["claude", "codex", "codex-windows"])
def test_the_gate_ledger_counts_the_message_in_utf8_bytes(tmp_path, client):
    """Every client's gate line records the same unit: the message's UTF-8
    length plus one (the newline), not characters or UTF-16 code units."""
    message = "Park now: café ☃ \U0001f600 please"
    server, _ = _gate_daemon("block\n" + message + "\n")
    try:
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        extra = dict(PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_port}",
                     PSEUDOLIFE_MCP_TOKEN="fixture-token")
        # A UTF-8 locale, as on macOS and most Linux desktops: there bash's
        # ${#var} counts characters, not bytes.
        utf8 = {"LC_ALL": "C.UTF-8"}
        if client == "claude":
            result, _ = _run(_gate_env(tmp_path, server, **utf8))
            assert result.returncode == 2
        elif client == "codex":
            result, _ = _run(_codex_env(tmp_path, **extra, **utf8))
            assert json.loads(result.stdout) == {"decision": "block", "reason": message}
        else:
            result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", "Stop",
                              input=_payload(), env=_env(tmp_path, **extra))
            assert json.loads(result.stdout) == {"decision": "block", "reason": message}
        [line] = (tmp_path / "digests" / "ledger.log").read_text(encoding="utf-8").splitlines()
        fields = line.split("\t")
        assert (fields[1], fields[5]) == ("gate", "block")
        assert int(fields[4]) == len(message.encode("utf-8")) + 1
    finally:
        server.shutdown()
        server.server_close()


def _managed_connection(tmp_path, url, token_file):
    from tests.test_codex_hooks import connection_payload
    connection = tmp_path / "home" / "pseudolife" / "connection.json"
    connection.parent.mkdir(parents=True, exist_ok=True)
    connection.write_text(json.dumps(connection_payload(url, token_file), indent=2) + "\n")
    connection.chmod(0o600)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics require a POSIX host: "
                    "Git Bash cannot show an NTFS file is owner-only, so, as in "
                    "coordination-start.sh, the managed files are refused there")
def test_a_tokenless_managed_connection_sends_no_bearer(tmp_path):
    """Setup records an empty token file for a daemon without auth; the gate
    then sends no bearer and ignores the explicit settings, as the sibling
    hooks do."""
    server, requests = _gate_daemon("allow\n")
    try:
        _managed_connection(tmp_path, f"http://127.0.0.1:{server.server_port}", "")
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        env = _codex_env(tmp_path, PSEUDOLIFE_MCP_TOKEN="ambient-token",
                         PSEUDOLIFE_MCP_DAEMON_URL="http://127.0.0.1:1")
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert requests == [("/api/hook/park-gate?agent=" + "a" * 32, None)]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics require a POSIX host: "
                    "Git Bash cannot show an NTFS file is owner-only, so, as in "
                    "coordination-start.sh, the managed files are refused there")
def test_an_explicit_token_file_needs_the_matching_managed_url(tmp_path):
    """Beside a managed connection, an explicit PSEUDOLIFE_MCP_TOKEN_FILE is
    honoured only with the managed URL named explicitly too; otherwise the
    gate asks nothing (a credential must not go to a daemon it was not
    recorded for)."""
    server, requests = _gate_daemon("allow\n")
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        managed_token = tmp_path / "home" / "pseudolife" / "token"
        managed_token.parent.mkdir(parents=True, exist_ok=True)
        managed_token.write_text("managed-token\n")
        managed_token.chmod(0o600)
        _managed_connection(tmp_path, url, managed_token.as_posix())
        explicit = tmp_path / "explicit-token"
        explicit.write_text("explicit-token\n")
        explicit.chmod(0o600)
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        env = _codex_env(tmp_path, PSEUDOLIFE_MCP_TOKEN_FILE=str(explicit))
        env.pop("PSEUDOLIFE_MCP_TOKEN", None)
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr, requests) == (0, "", "", [])
        env["PSEUDOLIFE_MCP_DAEMON_URL"] = url
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert requests == [("/api/hook/park-gate?agent=" + "a" * 32, "Bearer explicit-token")]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics require a POSIX host: "
                    "Git Bash cannot show an NTFS file is owner-only, so, as in "
                    "coordination-start.sh, the managed files are refused there")
def test_the_codex_bash_gate_uses_the_managed_connection(tmp_path):
    """Setup records the daemon URL and a rotatable bearer file in the Codex
    home's connection file; the gate reads them the way the sibling bash
    hooks do, and an explicit daemon URL that disagrees stops the request."""
    from tests.test_codex_hooks import connection_payload
    server, requests = _gate_daemon("allow\n")
    try:
        home = tmp_path / "home"
        token_file = home / "pseudolife" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("file-token\n")
        token_file.chmod(0o600)
        connection = token_file.with_name("connection.json")
        connection.write_text(json.dumps(connection_payload(
            f"http://127.0.0.1:{server.server_port}", token_file.as_posix()), indent=2) + "\n")
        connection.chmod(0o600)
        _digest(tmp_path, 3, "", ring=False)
        _agent(tmp_path)
        env = _codex_env(tmp_path)
        env.pop("PSEUDOLIFE_MCP_TOKEN", None)
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert requests == [("/api/hook/park-gate?agent=" + "a" * 32, "Bearer file-token")]
        env["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:1"
        result, _ = _run(env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name != "nt", reason="the Windows PID probe")
def test_exits_when_claude_code_is_gone_on_windows(tmp_path):
    """Under Git Bash CLAUDE_PID is a Windows PID that kill -0 cannot see
    (hooks ran under Git Bash /usr/bin/bash on 2.1.280, 2026-09-27). The hook
    lists it through `ps -W` at arm time and then once a minute, so an
    orphaned watcher ends within that minute instead of running its hour."""
    _digest(tmp_path, 3, BODY)
    _seen(tmp_path, 3)
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        process = _start(_env(tmp_path, wait=3540, CLAUDE_PID=str(parent.pid)))
        time.sleep(3)
        assert process.poll() is None
    finally:
        parent.kill()
        parent.wait()
    code, out, err = _finish(process, timeout=_constant("PARENT_CHECK") + DEADLINE)
    assert (code, out, err) == (0, "", "")
