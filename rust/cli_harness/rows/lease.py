"""Canonical process lease leaves, using real OS locks and oracle board writers.

The fixture clock and random producers are deterministic; the database is
real, disposable PostgreSQL. Every mutating HTTP action has a complete
post-state dump, including the audit chain, in the compared observation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from .. import core, normalize
from ..mutants import Mutant
from . import _bank

NAME = "fixture"
TOKEN = "lease-fixture-bearer"
T0 = 1_700_000_000.0

_LOCK_CHILD = """
import sys
from pathlib import Path
from pseudolife_memory.os_lock import OsLock
lock = OsLock(Path(sys.argv[1]))
print('HELD' if lock.acquire() else 'BUSY', flush=True)
sys.stdin.readline()
lock.release()
"""


class _Holder:
    """Owned fixture process; retain PID and OS start time at launch."""

    def __init__(self, path):
        import psutil
        from .. import producers
        env = {key: os.environ[key] for key in core._INHERIT if key in os.environ}
        env.update(PYTHONPATH=str(producers._SOURCE), PYTHONUTF8="1")
        self.proc = subprocess.Popen([sys.executable, "-c", _LOCK_CHILD, str(path)], env=env,
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


class _Storage:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction():
            yield


class Board:
    """Loopback HTTP boundary backed by the oracle CoordinationStore."""

    def __init__(self, scenario="free", bank=True):
        from pseudolife_memory.storage.coordination import CoordinationStore
        self.name = _bank.name(f"pl_cf_lease_{uuid.uuid4().int % 10**18}") if bank else None
        self.conn = None
        self.records = []
        self.states = []
        self.locals = []
        if not bank:
            self.url = core.DEAD_DAEMON_URL
            return
        with _bank._admin() as conn:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (self.name,)).fetchone():
                raise RuntimeError("disposable name collision")
        _bank.create(self.name)
        self.conn = _bank.connect(self.name, autocommit=True)
        self.store = CoordinationStore(_Storage(self.conn), clock=lambda: T0)
        self.lock = threading.RLock()
        self.counter = 0
        self.scenario = scenario
        self.calls = {}
        self.holder = None
        if scenario in ("held", "queue", "claim", "other-mirror", "same-mirror"):
            label = {"other-mirror": "lease-hold@0123456789ab", "same-mirror": "lease-hold@abcdef123456"}.get(scenario, "codex")
            self.holder = self.register(label=label, project="fixture", task="peer work")
            self.store.acquire_lease("alice", self.holder["agent_id"], self.holder["credential"],
                                     name=NAME, ttl=120, expect=60, purpose="peer work")
            if scenario == "queue":
                self.waiter = self.register(label="codex", project="fixture", task="first waiter")
                self.store.acquire_lease("alice", self.waiter["agent_id"], self.waiter["credential"],
                                         name=NAME, ttl=120, purpose="first waiter")
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

    def register(self, **fields):
        self.counter += 1
        # Fixed test entropy at the writer boundary, rather than SQL inserts
        # or erasing identity/hash columns from compared database state.
        identifier = uuid.UUID(int=self.counter, version=4)
        credential = base64.urlsafe_b64encode(hashlib.sha256(f"fixture-{self.counter}".encode()).digest()).decode().rstrip("=")
        with patch("pseudolife_memory.storage.coordination.uuid.uuid4", return_value=identifier), \
                patch("pseudolife_memory.storage.coordination.secrets.token_urlsafe", return_value=credential):
            return self.store.register("alice", **fields)

    def dispatch(self, action, body, headers):
        from pseudolife_memory.storage.coordination import CoordinationError
        self.calls[action] = self.calls.get(action, 0) + 1
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"error": "unauthorized"}
        if self.scenario == "refused" and action == "register":
            return 403, {"error": "forbidden"}
        if self.scenario == "malformed" and action == "register":
            return 200, {"registered": True}
        if self.scenario == "transient" and action == "lease" and self.calls[action] == 1:
            return 503, {"error": "temporarily_unavailable"}
        creds = ("alice", headers.get("X-PL-Agent"), headers.get("X-PL-Agent-Key"))
        try:
            if action == "register":
                reply = self.register(**body)
            elif action == "lease":
                reply = self.store.acquire_lease(*creds, **body)
            elif action == "release":
                reply = self.store.release_lease(*creds, **body)
            elif action == "leases":
                reply = self.store.list_leases(**body)
            else:
                raise RuntimeError(f"unexpected fixture action {action}")
        except CoordinationError as error:
            return 400, {"error": error.code}
        if action in ("register", "lease", "release"):
            self.states.append({"action": action, "state": _bank.dump(self.name)})
        if self.scenario == "queue" and action == "lease" and reply["state"] == "queued":
            self.store.release_lease("alice", self.holder["agent_id"], self.holder["credential"], name=NAME)
            self.states.append({"action": "fixture-release", "state": _bank.dump(self.name)})
            self.store.release_lease("alice", self.waiter["agent_id"], self.waiter["credential"], name=NAME)
            self.states.append({"action": "fixture-release-waiter", "state": _bank.dump(self.name)})
        return 200, reply

    def requests(self):
        return self.records

    def close(self):
        for holder in self.locals:
            holder.stop()
        if self.conn is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.conn.close()
        _bank.drop(self.name)


def _local(state="absent", name=NAME):
    def setup(arm):
        from pseudolife_memory.os_lock import OsLock, lock_file_name
        from ..producers import write_private
        write_private(arm.home / "locks" / "instance.id", "abcdef123456\n")
        path = arm.home / "locks" / lock_file_name(name)
        if state == "free":
            lock = OsLock(path)
            assert lock.acquire()
            lock.release()
        elif state == "held":
            holder = _Holder(path)
            assert holder.line() == "HELD"
            arm.state["holder"] = holder
            arm.daemon.locals.append(holder)
    return setup


def _suite(state="absent", slot=0):
    def setup(arm):
        from ..producers import write_private
        from pseudolife_memory.os_lock import OsLock
        write_private(arm.home / "suite" / "instance.id", "abcdef123456\n")
        path = arm.home / "suite" / (f"full-suite.{slot}.lock" if slot else "full-suite.lock")
        if state == "free":
            lock = OsLock(path)
            assert lock.acquire()
            lock.release()
        elif state == "held":
            holder = _Holder(path)
            assert holder.line() == "HELD"
            arm.daemon.locals.append(holder)
    return setup


def coordination_state(state):
    return {"tables": {key: value for key, value in state["tables"].items() if key.startswith("coordination_")},
            "sequences": {key: value for key, value in state["sequences"].items() if key.startswith("coordination_")}}


def _after(arm, obs):
    # Every write, rather than only the final released state.
    if arm.daemon.name:
        obs["db"] = {"writes": [{"action": item["action"], "state": coordination_state(item["state"])}
                                 for item in arm.daemon.states],
                     "final": coordination_state(_bank.dump(arm.daemon.name))}
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


@normalize.rule("lease-seeded-clock")
def _seeded_clock(obs):
    """Validate displays against this arm's clock and the fixed seed clock."""
    from pseudolife_memory.lease_cli import _span
    # The fixture clock is injected only into the board writer; the CLI
    # still reads its actual local clock. Keep every surrounding byte.
    for field in ("stdout", "stderr"):
        raw = base64.b64decode(obs[field])
        for seconds in range(int(obs["window"][0]), int(obs["window"][1]) + 2):
            age = _span(seconds - T0).encode()
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
    return result


MUTANTS = [
    Mutant("lease-lock-name", "lease", "shim/src/cli/lease/lock.rs",
           'format!("lease-{safe}{suffix}.lock")', 'format!("lease-{safe}.lock")', ("run-hashed-name",)),
    Mutant("lease-child-status", "lease", "shim/src/cli/lease/run.rs",
           "Ok(exit_status(status))", "Ok(0)", ("run-child-status",)),
    Mutant("lease-os-truth", "lease", "shim/src/cli/lease/view.rs",
           'let held = local["state"] == "held" || (!report["holder"].is_null() && !stale);',
           'let held = false;', ("check-held",)),
    Mutant("lease-stale-mirror", "lease", "shim/src/cli/lease/view.rs",
           'let stale = local["state"] == "free"', 'let stale = false && local["state"] == "free"',
           ("check-board-same-mirror",)),
    Mutant("lease-timeout-status", "lease", "shim/src/cli/lease/run.rs",
           '            75\n        }\n        Err(RunError::Lock', '            0\n        }\n        Err(RunError::Lock',
           ("run-timeout",)),
    Mutant("lease-release-mirror", "lease", "shim/src/cli/lease/run.rs",
           "board.release(&name).await;", "let _ = &name;", ("run-board-free",)),
    Mutant("lease-list-filter", "lease", "shim/src/cli/lease/view.rs",
           "wanted.as_ref().is_none_or(|w| w == &file)", "wanted.as_ref().is_none_or(|_w| true)",
           ("list-board-filter-missing",)),
]
