"""Canonical process lease leaves, using real OS locks and a recorded board.

The board fake answers the leases API as the frozen Python store did
(``CoordinationStore`` on disposable PostgreSQL, a fixed clock and fixed
identities): ``fixtures/lease-board.json`` holds, per scenario, the
coordination state the scenario starts from and every store-backed request
the clients made, with the store's answer and the coordination state after
it (the complete post-state, audit chain included). The fake replays it: a
request that is not the next one recorded for that history is answered 500
and fails the case, so a client that changes what it sends is still caught.
The bearer check and the scripted refusals stay live.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import core, lease_fixture, normalize, producers

NAME = "fixture"
TOKEN = "lease-fixture-bearer"
T0 = 1_700_000_000.0

class _Holder:
    """Owned fixture process; retain PID and OS start time at launch."""

    def __init__(self, path):
        import psutil
        env = {key: os.environ[key] for key in core._INHERIT if key in os.environ}
        self.proc = subprocess.Popen([sys.executable, "-I", "-S", "-X", "utf8",
                                      str(Path(lease_fixture.__file__).resolve()), str(path)], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.pid = self.proc.pid
        self.started = psutil.Process(self.pid).create_time()
        self.lines = queue.Queue()
        threading.Thread(target=lambda: self.lines.put(self.proc.stdout.readline().strip()),
                         daemon=True).start()

    def line(self):
        return self.lines.get(timeout=30)

    def release(self):
        if self.proc.poll() is None:
            self.proc.stdin.write("\n")
            self.proc.stdin.flush()

    def stop(self):
        import psutil
        if self.proc.poll() is None:
            self.release()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                assert psutil.Process(self.pid).create_time() == self.started
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


RECORDING = Path(__file__).resolve().parents[1] / "fixtures" / "lease-board.json"
_RECORDED: dict | None = None


def _recorded(scenario: str) -> dict:
    global _RECORDED
    if _RECORDED is None:
        _RECORDED = json.loads(RECORDING.read_text(encoding="utf-8"))["scenarios"]
    return _RECORDED[scenario]


def _key(action, body, headers) -> list:
    """What decides the store's answer: the action, the body and the caller."""
    return [action, body, headers.get("X-PL-Agent"), headers.get("X-PL-Agent-Key")]


class Board:
    """Loopback HTTP boundary answering as the recorded frozen store."""

    def __init__(self, scenario="free", bank=True):
        self.name = None
        self.records = []
        self.states = []
        self.locals = []
        self.server = None
        if not bank:
            self.url = core.DEAD_DAEMON_URL
            return
        self.name = "recorded:" + scenario
        recorded = _recorded(scenario)
        self.initial = recorded["initial"]
        self.transcripts = recorded["transcripts"]
        self.history: list = []
        self.unrecorded: list = []
        self.lock = threading.RLock()
        self.scenario = scenario
        self.calls = {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                action = self.path.rsplit("/", 1)[-1]
                with fixture.lock:
                    fixture.records.append({"method": "POST", "target": self.path,
                                            "headers": list(self.headers.items()),
                                            "body": raw.decode()})
                    status, reply = fixture.dispatch(action, body, self.headers)
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def dispatch(self, action, body, headers):
        self.calls[action] = self.calls.get(action, 0) + 1
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"error": "unauthorized"}
        if self.scenario == "refused" and action == "register":
            return 403, {"error": "forbidden"}
        if self.scenario == "malformed" and action == "register":
            return 200, {"registered": True}
        if self.scenario == "transient" and action == "lease" and self.calls[action] == 1:
            return 503, {"error": "temporarily_unavailable"}
        key = _key(action, body, headers)
        steps = [t[len(self.history)] for t in self.transcripts
                 if len(t) > len(self.history)
                 and [step["key"] for step in t[:len(self.history)]] == self.history
                 and t[len(self.history)]["key"] == key]
        if not steps or any(step != steps[0] for step in steps):
            self.unrecorded.append(key)
            return 500, {"error": "fixture: request not in the recording"}
        step = steps[0]
        self.history.append(key)
        self.states.extend(step["states"])
        return step["status"], step["reply"]

    def final_state(self) -> dict:
        return self.states[-1]["state"] if self.states else self.initial

    def requests(self):
        return self.records

    def close(self):
        for holder in self.locals:
            holder.stop()
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _setup_lock(arm, path, state):
    if state == "free":
        handle = lease_fixture.take(path)
        assert handle is not None
        lease_fixture.release(handle)
    elif state == "held":
        holder = _Holder(path)
        arm.daemon.locals.append(holder)
        assert holder.line() == "HELD"
        return holder
    return None


def _local(state="absent", name=NAME):
    def setup(arm):
        directory = arm.home / "locks"
        producers.write_private(directory / "instance.id", "abcdef123456\n", root=arm.home)
        holder = _setup_lock(arm, directory / lease_fixture.lock_file_name(name), state)
        if holder is not None:
            arm.state["holder"] = holder
    return setup


def _suite(state="absent", slot=0):
    def setup(arm):
        directory = arm.home / "suite"
        producers.write_private(directory / "instance.id", "abcdef123456\n", root=arm.home)
        path = directory / (f"full-suite.{slot}.lock" if slot else "full-suite.lock")
        _setup_lock(arm, path, state)
    return setup


def _after(arm, obs):
    # Every write, rather than only the final released state (the recorded
    # coordination tables and sequences after each store-backed request).
    if arm.daemon.name:
        if arm.daemon.unrecorded:
            raise AssertionError(f"lease row: {arm.name} sent a request the recorded board never "
                                 f"answered: {arm.daemon.unrecorded[0]!r}")
        obs["db"] = {"writes": [{"action": item["action"], "state": item["state"]}
                                 for item in arm.daemon.states],
                     "final": arm.daemon.final_state()}
    obs["seed_offsets"] = {str(stamp): time.localtime(stamp).tm_gmtoff for stamp in (T0, T0 + 60)}
    for holder in arm.daemon.locals:
        holder.stop()
    if "waited" in arm.state:
        obs["listener"] = {"wait_observed": arm.state["waited"]}
        if arm.name == "python":
            assert arm.state["waited"]


def _release_local(arm, proc):
    # Release only after this wrapper's actual busy-lock notice. Its stderr
    # prefix is handed back to the shared collector without losing bytes.
    prefix = b""
    arm.state["waited"] = False
    while True:
        line = proc.stderr.readline()
        prefix += line
        if line.startswith(b"lease: waiting for the local lock on "):
            break
        if not line:
            return prefix
    arm.state["holder"].release()
    arm.state["waited"] = True
    return prefix


@normalize.rule("lease-http-json")
def _command_path(obs):
    """Compare decoded request JSON; validate its original byte length."""
    import re
    for request in obs.get("requests", []):
        body = request["body"]
        headers = request["headers"]
        if headers.get("content-length") != str(len(body.encode())):
            raise ValueError("request Content-Length differs from its captured body")
        parsed = json.loads(body)
        request["body"] = parsed
        headers["content-length"] = "<json-length>"
        if re.fullmatch(r"python-httpx/[0-9.]+", headers.get("user-agent", "")):
            del headers["user-agent"]
        if re.fullmatch(r"gzip, deflate(?:, br)?(?:, zstd)?", headers.get("accept-encoding", "")):
            del headers["accept-encoding"]
        if headers.get("connection") == "keep-alive":
            del headers["connection"]


def _held_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02d}h"


@normalize.rule("lease-seeded-clock")
def _seeded_clock(obs):
    """Validate displays against this arm's clock and the fixed seed clock."""
    # The fixture clock is injected only into the board writer; the CLI
    # still reads its actual local clock. Keep every surrounding byte.
    for field in ("stdout", "stderr"):
        raw = base64.b64decode(obs[field])
        for seconds in range(int(obs["window"][0]), int(obs["window"][1]) + 2):
            age = _held_age(seconds - T0).encode()
            raw = raw.replace(b" for " + age + b", purpose", b" for <held-age>, purpose")
        for stamp in (T0, T0 + 60):
            offset = obs["seed_offsets"][str(stamp)]
            display = time.strftime("%Y-%m-%d %H:%M", time.gmtime(stamp + offset)).encode()
            raw = raw.replace(display, f"<seed-clock:{stamp:.0f}>".encode())
        obs[field] = base64.b64encode(raw).decode()


@normalize.rule("lease-native-stdout")
def _stdout_policy(obs):
    """Declared native closed-stdout diagnostic, preserving exit 120."""
    eol = b"\r\n" if core.WINDOWS else b"\n"
    trailer = (b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>" + eol
               + (b"OSError: [Errno 22] Invalid argument" if core.WINDOWS
                  else b"BrokenPipeError: [Errno 32] Broken pipe") + eol)
    raw = base64.b64decode(obs["stderr"])
    if obs["exit"] == 120 and not base64.b64decode(obs["stdout"]) and raw in (trailer, b"lease: stdout write failed" + eol):
        obs["stderr"] = base64.b64encode(b"<closed-stdout>" + eol).decode()


def cases():
    env = {"PSEUDOLIFE_LEASE_LOCK_DIR": "{HOME}/locks",
           "PSEUDOLIFE_SUITE_LOCK_DIR": "{HOME}/suite",
           "PSEUDOLIFE_SUITE_LEASE": "full-suite",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = []

    def add(identifier, argv, *, local="absent", scenario="free", board=False, **kwargs):
        result.append(core.Case(identifier, ["lease", *argv],
                                env={**env, **({"PSEUDOLIFE_MCP_TOKEN": TOKEN} if board else {}),
                                     **kwargs.pop("env", {})},
                                setup=kwargs.pop("setup", _local(local)), after=_after,
                                daemon=lambda: Board(scenario, bank=board),
                                rules=kwargs.pop("rules", ("lease-http-json", "lease-seeded-clock")), **kwargs))

    for action in (None, "check", "list", "run"):
        add("help-" + (action or "lease"), [*([action] if action else []), "--help"])
    for local in ("absent", "free", "held"):
        for flag in ([], ["--json"]):
            suffix = "-json" if flag else ""
            add("check-" + local + suffix, ["check", NAME, *flag], local=local)
            add("list-" + local + suffix, ["list", *flag], local=local)
    for state, slot in (("absent", 0), ("free", 0), ("held", 0), ("held", 7)):
        for flag in ([], ["--json"]):
            add(f"check-suite-{state}-{slot}" + ("-json" if flag else ""),
                ["check", "full-suite", *flag], setup=_suite(state, slot))
    add("check-suite-elsewhere", ["check", "full-suite@box", "--json"], setup=_suite("held"))
    # `lease hold full-suite` takes the lease's own lock (lease-full-suite.lock)
    # in the lease lock directory, which here differs from the suite's; check
    # reads either as held (a reproduction loop reserving the machine).
    for where in ("locks", "suite"):
        def reserved(arm, where=where):
            _suite("free")(arm)
            if where == "locks":
                _local("held", name="full-suite")(arm)
            else:
                _setup_lock(arm, arm.home / "suite" / lease_fixture.lock_file_name("full-suite"), "held")
        add(f"check-suite-reserved-in-{where}", ["check", "full-suite", "--json"],
            setup=reserved, golden=False)
    # lease_cli.py:1391 copies these holder fields after json.loads.
    for suffix, value in (("text", "not a number"), ("numeric", "123")):
        def private_holder(arm, value=value):
            from ..producers import write_private
            _suite("held")(arm)
            write_private(arm.home / "suite" / "full-suite.holder.json", json.dumps(
                {"pid": 1, "worktree": {"$serde_json::private::Number": value},
                 "started": "fixture"}), root=arm.home)
        add(f"check-suite-private-number-{suffix}", ["check", "full-suite", "--json"],
            setup=private_holder, golden=False)
    for scenario in ("free", "claim", "other-mirror", "same-mirror"):
        for flag in ([], ["--json"]):
            add("check-board-" + scenario + ("-json" if flag else ""),
                ["check", NAME, *flag], local="free" if scenario.endswith("mirror") else "absent",
                scenario=scenario, board=True)
    for scenario in ("free", "claim", "queue"):
        for name in (None, NAME):
            for flag in ([], ["--json"]):
                add("list-board-" + scenario + ("-named" if name else "-all") + ("-json" if flag else ""),
                    ["list", *([name] if name else []), *flag], local="free", scenario=scenario, board=True)
    add("list-board-filter-missing", ["list", "missing", "--json"], local="free", scenario="claim", board=True)
    code = ("import os,sys,pathlib; pathlib.Path(os.environ['HOME'],'ran').write_bytes("
            "os.environ['PSEUDOLIFE_LEASES_HELD'].encode()); "
            "sys.stdout.buffer.write(b'out\\x00\\xff'); sys.stderr.buffer.write(b'err\\x00\\xfe'); sys.exit(3)")
    command = ["--", "python.exe" if core.WINDOWS else "python3", "-c", code]
    # These cases deliberately run the host's interpreter from the host PATH.
    host_python = {"real_programs": (command[1],)}
    add("run-child-status", ["run", NAME, "--no-board", *command],
        env={"PSEUDOLIFE_LEASES_HELD": "suite"}, **host_python)
    add("run-hashed-name", ["run", "claim:a/b", "--no-board", *command], **host_python)
    add("run-missing-executable", ["run", NAME, "--no-board", "--", "fixture-no-such-program"],
        programs=("fixture-no-such-program",))
    add("run-timeout", ["run", NAME, "--no-board", "--timeout", "0", *command], local="held",
        **host_python)
    add("run-local-wait", ["run", NAME, "--no-board", "--timeout", "10", *command],
        local="held", before_capture=_release_local, **host_python)
    add("run-nested", ["run", NAME, "--no-board", *command], local="held",
        env={"PSEUDOLIFE_LEASES_HELD": NAME}, **host_python)
    add("run-child-closed-stdout", ["run", NAME, "--no-board", *command], stdout_closed=True,
        **host_python)
    for identifier, argv in (("check-closed-stdout", ["check", NAME]),
                             ("list-closed-stdout", ["list", "--json"]),
                             ("help-closed-stdout", ["--help"])):
        add(identifier, argv, stdout_closed=True, rules=("lease-http-json", "lease-native-stdout"))
    for scenario in ("free", "refused", "malformed", "queue", "transient"):
        add("run-board-" + scenario, ["run", NAME, "--expect", "2m", "--ttl", "30", "--purpose", "proof", *command],
            scenario=scenario, board=True)
    add("run-board-timeout", ["run", NAME, "--timeout", "0", *command], scenario="held", board=True)
    for flag, value in (("--expect", "8d"), ("--expect", "0"), ("--ttl", "29"), ("--timeout", "-1")):
        add("usage-" + flag[2:] + "-" + value, ["run", NAME, flag, value, *command])
    # check NAME -- COMMAND: free runs the child and passes its own exit 1
    # through, after the flushed report; held refuses with 75 and never runs
    # it (no 'ran' file). The child exits 1, the code a refusal must not be.
    gated = ["--", command[1], "-c",
             "import os,sys,pathlib; pathlib.Path(os.environ['HOME'],'ran').write_bytes("
             "os.environ.get('PSEUDOLIFE_LEASES_HELD','<unset>').encode()); "
             "sys.stdout.buffer.write(b'out\\x00\\xff'); sys.stderr.buffer.write(b'err\\x00\\xfe'); "
             "sys.exit(1)"]
    for local in ("absent", "free", "held"):
        add("check-gated-" + local, ["check", NAME, *gated], local=local, **host_python)
    add("check-gated-board-claim", ["check", NAME, *gated], scenario="claim", board=True,
        **host_python)
    add("check-gated-board-free", ["check", NAME, *gated], scenario="free", board=True,
        **host_python)
    add("check-gated-suite-held", ["check", "full-suite", *gated], setup=_suite("held", 7),
        **host_python)
    add("check-gated-missing-executable", ["check", NAME, "--", "fixture-no-such-program"],
        programs=("fixture-no-such-program",))
    add("check-gated-empty-command", ["check", NAME, "--"])
    add("check-gated-json", ["check", NAME, "--json", *gated])
    return result
