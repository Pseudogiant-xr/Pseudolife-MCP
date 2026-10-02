"""``pseudolife-mcp move``: move a Docker-tier bank to another host.

Every external effect of a move (``ssh`` to the target, ``docker`` on the
source, the schedule and ``connect`` subprocesses, the HTTP health reads)
goes through one runner. Here that runner is a recording fake backed by a
small model of both hosts (:class:`World`): it answers each command from
the model's state, changes the state the way the real command would, and
records every call, so a test can assert what ran, in what order, and what
never ran. A failure is injected by naming a substring of the command that
should fail (or a predicate over the call); ``fail255`` makes ssh itself
fail on that call, the way a dropped connection does. The target's ``/data``
volume is modelled apart from its host files, and the fake ``restore.sh``
wipes it, as the real state restore does.

The PG-backed test at the end checks the fence itself against the bench
Postgres: a database with ``ALLOW_CONNECTIONS false`` refuses a new
connection and accepts one once the fence is lifted.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import signal
import tarfile
from pathlib import Path

import pytest

from pseudolife_memory import move_cli

TARGET = "root@box"
CHECKOUT = "~/src/Pseudolife-MCP"
TARGET_URL = "http://100.64.0.9:8765"
LOCAL = "http://127.0.0.1:8765"
SOURCE_BANK = "a" * 16
TARGET_BANK = "b" * 16
SOURCE_META = '"5f0c6a0e-1111-4222-8333-944444444444"'
TARGET_META = '"5f0c6a0e-9999-4888-8777-966666666666"'
SOURCE_TOKEN = "src-singular-token-0123456789abcdef"
LAPTOP_TOKEN = "laptop-token-0123456789abcdef0123"
CI_TOKEN = "ci-token-fedcba9876543210fedcba98"
TARGET_OLD_TOKEN = "target-installer-token-5555555555"
SOURCE_PG_PASSWORD = "pgsecret-source-1234"
TARGET_PG_PASSWORD = "pgsecret-target-5678"
SECRETS = (SOURCE_TOKEN, LAPTOP_TOKEN, CI_TOKEN, TARGET_OLD_TOKEN, SOURCE_PG_PASSWORD, TARGET_PG_PASSWORD)
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
REGISTRATION = "/root/.pseudolife-mcp/default.token"

SOURCE_ENV = [
    f"PSEUDOLIFE_MCP_DATABASE_URL=postgresql://pseudolife:{SOURCE_PG_PASSWORD}@pseudolife-pg:5432/pseudolife_memory",
    f"PSEUDOLIFE_MCP_TOKEN={SOURCE_TOKEN}",
    f"PSEUDOLIFE_MCP_TOKENS={LAPTOP_TOKEN}:laptop,{CI_TOKEN}:ci",
    "PSEUDOLIFE_MCP_TIER_MAP=laptop:core,ci:minimal",
    "PSEUDOLIFE_MCP_TOOLSET=core",
    "PSEUDOLIFE_MCP_DATA_DIR=/data",
]
TARGET_ENV = [
    f"PSEUDOLIFE_MCP_DATABASE_URL=postgresql://pseudolife:{TARGET_PG_PASSWORD}@pseudolife-pg:5432/pseudolife_memory",
    f"PSEUDOLIFE_MCP_TOKEN={TARGET_OLD_TOKEN}",
    "PSEUDOLIFE_MCP_DATA_DIR=/data",
]
TARGET_DOTENV = (f"POSTGRES_PASSWORD={TARGET_PG_PASSWORD}\r\n"
                 f"PSEUDOLIFE_MCP_TOKEN={TARGET_OLD_TOKEN}\r\n"
                 "TZ=UTC\r\n")

DUMP = (
    "-- PostgreSQL database dump\n"
    "COPY public.entries (id, text) FROM stdin;\n1\tone\n2\ttwo\n\\.\n"
    "COPY public.facts (id) FROM stdin;\n1\n\\.\n"
    "COPY public.principals (principal) FROM stdin;\nlaptop\n\\.\n"
    "COPY public.coordination_leases (name) FROM stdin;\n\\.\n"
    "-- PostgreSQL database dump complete\n"
)
DUMP_COUNTS = {"public.entries": 2, "public.facts": 1, "public.principals": 1, "public.coordination_leases": 0}


def _tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _policy(text: str) -> dict:
    name, _, count = text.partition(":")
    return {"Name": name, "MaximumRetryCount": int(count or 0)}


def _env_from_dotenv(text: str) -> list[str]:
    env = dict(item.split("=", 1) for item in TARGET_ENV)
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'")
    return [f"{k}={v}" for k, v in env.items()]


class Call:
    def __init__(self, argv, stdin, new_group=False):
        self.argv = argv
        self.stdin = stdin
        self.new_group = new_group
        self.text = " ".join(argv)

    @property
    def remote(self) -> str | None:
        """The remote command of an ``ssh`` call, else ``None``."""
        if self.argv[:len(SSH)] == SSH and len(self.argv) == len(SSH) + 2:
            return self.argv[-1]
        return None

    def __repr__(self):
        return f"Call({self.text!r})"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.5)


class World:
    """Both hosts, as far as a move can see them."""

    def __init__(self, *, fail=(), fail255=(), target_empty=True, exposed=True, target_schema=52,
                 target_bank=TARGET_BANK, source_bank=SOURCE_BANK, bank_after_start=SOURCE_BANK,
                 bank_appears_at=None, count_override=None, corrupt_copy=False, markers=None,
                 connect_exit=0, connect_reply=None, checkout=True, posix=True, source_healthy=True,
                 source_docker=True, target_healthy=True, expose_exit=0, stop_target_fails=False,
                 tar_members=None, target_pg_running="true", target_meta_after=SOURCE_META,
                 target_up_after_start=True, env_mismatch=False, still_running_after_stop=False,
                 stop_exit_code=0, lingering_backends=0, target_timer="enabled", source_timer="enabled",
                 source_policy="unless-stopped", target_policy="unless-stopped", grep_hits=(REGISTRATION,),
                 dotenv=TARGET_DOTENV, hooks=None, target_respawns=0, restore_fails_after_drop=False):
        self.fail = tuple(fail)
        self.fail255 = tuple(fail255)
        self.calls: list[Call] = []
        self.gets: list[tuple[str, bool, float]] = []
        self.clock = Clock()
        self.source_running = True
        self.source_healthy = source_healthy
        self.source_docker = source_docker
        self.fenced = False
        self.moved = False
        self.target_running = True
        self.target_healthy = target_healthy
        self.target_empty = target_empty
        self.target_schema = target_schema
        self.target_bank = target_bank
        self.source_bank = source_bank
        self.bank_after_start = bank_after_start
        self.bank_appears_at = bank_appears_at
        self.started_at = None
        self.target_restored = False
        self.markers = dict(markers or {})          # host path -> marker dict
        self.files: dict[str, bytes] = {}           # target host files
        self.data_files: dict[str, bytes] = {"/data/config.yaml": b"target\n"}   # target /data volume
        self.dotenv = dotenv
        self.exposed = exposed
        self.count_override = count_override or {}
        self.corrupt_copy = corrupt_copy
        self.connect_exit = connect_exit
        self.connect_reply = connect_reply
        self.checkout = checkout
        self.posix = posix
        self.expose_exit = expose_exit
        self.stop_target_fails = stop_target_fails
        self.tar_members = tar_members if tar_members is not None else {
            "./config.yaml": b"memory: {}\n", "./last-backup.json": b"{}\n"}
        self.target_pg_running = target_pg_running
        self.target_meta_after = target_meta_after
        self.target_up_after_start = target_up_after_start
        self.env_mismatch = env_mismatch
        self.still_running_after_stop = still_running_after_stop
        self.stop_exit_code = stop_exit_code
        self.lingering_backends = lingering_backends
        self.target_timer = target_timer
        self.source_timer = source_timer
        self.source_policy = source_policy
        self.target_policy = target_policy
        self.grep_hits = tuple(grep_hits)
        self.hooks = hooks or {}
        self.target_respawns = target_respawns     # a remote update.sh that keeps going brings it back
        self.restore_fails_after_drop = restore_fails_after_drop
        self.target_live_env = list(TARGET_ENV)
        self.overlap = False   # ever a running target beside a running source on the moved bank

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _matches(needles, call) -> bool:
        return any(needle(call) if callable(needle) else needle in call.text for needle in needles)

    def _check_overlap(self):
        if self.source_running and self.target_running and self.target_restored:
            self.overlap = True

    # -- the runner surface ----------------------------------------------
    def run(self, argv, stdin=None, new_group=False):
        call = Call([str(a) for a in argv], stdin, new_group)
        self.calls.append(call)
        for needle, hook in self.hooks.items():
            if needle in call.text:
                hook(self, call)
        if call.remote is not None and self._matches(self.fail255, call):
            return 255, b"", b"Connection to box closed by remote host."
        if self._matches(self.fail, call):
            return 1, b"", b"injected failure"
        if call.remote is not None:
            return self.remote(call.remote, stdin)
        return self.local(call.argv, stdin)

    def get_json(self, url, token=None):
        self.gets.append((url, token is not None, self.clock.now))
        if url == LOCAL + "/health":
            if not (self.source_running and self.source_healthy):
                return None
            return {"status": "ok", "schema": 52, "auth": True, "bank": self.source_bank, "version": "0.15.0"}
        if url == TARGET_URL + "/health":
            if not self.target_running or self._matches(self.fail, Call(["GET", url], None)):
                return None
            if self.target_restored and not self.target_up_after_start:
                return None
            bank = self.target_bank
            if self.target_restored:
                bank = self.bank_after_start
                if self.bank_appears_at is not None and self.clock.now - self.started_at < self.bank_appears_at:
                    bank = None
            return {"status": "ok", "schema": self.target_schema, "auth": True, "bank": bank}
        if url == TARGET_URL + "/api/stats":
            return {"entries": 2}
        return None

    def local(self, argv, stdin):
        text = " ".join(argv)
        if argv[:2] == ["docker", "inspect"]:
            if "--type" in argv:
                if not self.source_docker:
                    return 1, b"", b"Error: No such container: pseudolife-mcp-daemon"
                return 0, json.dumps([{"Config": {"Env": SOURCE_ENV, "Image": "pseudolife-daemon:0.15.0"},
                                       "State": {"Running": self.source_running},
                                       "HostConfig": {"RestartPolicy": _policy(self.source_policy)}}]).encode(), b""
            running = self.source_running or self.still_running_after_stop
            return 0, f"{'true' if running else 'false'} {0 if running else self.stop_exit_code}\n".encode(), b""
        if argv[:2] == ["docker", "stop"]:
            self.source_running = False
            return 0, b"", b""
        if argv[:2] == ["docker", "start"]:
            self.source_running = True
            self._check_overlap()
            return 0, b"", b""
        if argv[:2] == ["docker", "update"]:
            self.source_policy = argv[2].split("=", 1)[1]
            return 0, b"", b""
        if argv[:3] == ["docker", "exec", "pseudolife-mcp-postgres"]:
            if "pg_dump" in argv:
                return 0, gzip.compress(DUMP.encode()), b""
            sql = argv[-1]
            if "ALLOW_CONNECTIONS false" in sql:
                self.fenced = True
            elif "ALLOW_CONNECTIONS true" in sql:
                self.fenced = False
            elif "pg_database_size" in sql:
                return 0, b"10485760\n", b""
            elif "to_regclass" in sql:
                return 0, b"t\n", b""
            elif "FROM principals" in sql:
                return 0, b"laptop\nphone\n", b""
            elif "coordination_bank_id" in sql:
                return 0, (SOURCE_META + "\n").encode(), b""
            elif "count(*) FROM pg_stat_activity" in sql:
                return 0, f"{self.lingering_backends}\n".encode(), b""
            return 0, b"", b""
        if argv[:3] == ["docker", "exec", "pseudolife-mcp-daemon"] and "du" in argv:
            return 0, b"2048\t/data\n", b""
        if argv[:2] == ["docker", "run"] and "tar" in argv:
            return 0, _tar(self.tar_members), b""
        if argv[:2] == ["docker", "run"] and "rm" in argv:
            self.moved = False
            return 0, b"", b""
        if argv[:2] == ["docker", "cp"] and argv[-1].endswith(":/data/moved.json"):
            self.moved = True
            return 0, b"", b""
        if "connect" in argv:
            reply = self.connect_reply if self.connect_reply is not None else {"exit": self.connect_exit, "rows": []}
            return self.connect_exit, json.dumps(reply).encode(), b""
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return (0 if self.source_timer == "enabled" else 1), f"{self.source_timer}\n".encode(), b""
        if argv[:3] == ["systemctl", "--user", "disable"]:
            self.source_timer = "disabled"
            return 0, b"", b""
        if argv[:3] == ["systemctl", "--user", "enable"]:
            self.source_timer = "enabled"
            return 0, b"", b""
        if argv[:2] == ["schtasks", "/Query"]:
            enabled = "true" if self.source_timer == "enabled" else "false"
            return 0, f"<Task><Settings><Enabled>{enabled}</Enabled></Settings></Task>".encode(), b""
        if argv[:2] == ["schtasks", "/Change"]:
            self.source_timer = "enabled" if "/ENABLE" in argv else "disabled"
            return 0, b"", b""
        raise AssertionError(f"unexpected local command: {text}")

    def remote(self, cmd, stdin):
        remote_dir = None
        if cmd == "true":
            return 0, b"", b""
        if cmd.startswith("command -v curl"):
            return (0 if self.posix else 127), b"", b""
        if cmd.startswith("test -f"):
            return (0 if self.checkout else 1), b"", b""
        if cmd.startswith("grep -q -- --no-start"):
            return 0, b"", b""
        if cmd.startswith("grep -lsF -f -"):
            if stdin and TARGET_OLD_TOKEN.encode() in stdin:
                return 0, "".join(f"{hit}\n" for hit in self.grep_hits).encode(), b""
            return 1, b"", b""
        if cmd.startswith("docker inspect --type container pseudolife-mcp-daemon"):
            return 0, json.dumps([{"Config": {"Env": self.target_live_env, "Image": "pseudolife-daemon:0.15.0"},
                                   "State": {"Running": self.target_running},
                                   "HostConfig": {"RestartPolicy": _policy(self.target_policy)}}]).encode(), b""
        if cmd.startswith("docker inspect -f") and cmd.endswith("pseudolife-mcp-daemon"):
            return 0, (b"true\n" if self.target_running else b"false\n"), b""
        if cmd.startswith("docker inspect -f"):
            return 0, f"{self.target_pg_running}\n".encode(), b""
        if cmd.startswith("curl -fsS"):
            if not (self.target_running and self.target_healthy):
                return 7, b"", b"connection refused"
            return 0, json.dumps({"status": "ok", "schema": self.target_schema, "auth": True,
                                  "bank": self.target_bank}).encode(), b""
        if cmd.startswith("systemctl --user is-enabled"):
            if self.target_timer == "no-bus":
                return 1, b"Failed to connect to bus: No medium found\n", b""
            return (0 if self.target_timer == "enabled" else 1), f"{self.target_timer}\n".encode(), b""
        if cmd.startswith("systemctl --user disable"):
            self.target_timer = "disabled"
            return 0, b"", b""
        if cmd.startswith("systemctl --user enable"):
            self.target_timer = "enabled"
            return 0, b"", b""
        if cmd.startswith("docker update --restart="):
            self.target_policy = cmd.split()[2].split("=", 1)[1]
            return 0, b"", b""
        if "expose status" in cmd:
            body = {"exposed": True, "url": TARGET_URL} if self.exposed else {"exposed": False, "url": None}
            return 0, json.dumps(body).encode(), b""
        if "expose tailscale" in cmd:
            if self.expose_exit:
                return self.expose_exit, b"", b"expose: run sudo tailscale set --operator=$USER"
            self.exposed = True
            return 0, json.dumps({"exposed": True, "url": TARGET_URL}).encode(), b""
        if cmd.startswith("mkdir -p"):
            return 0, b"", b""
        if cmd.startswith("cat > "):
            path = cmd[len("cat > "):]
            data = stdin or b""
            if path.endswith("/marker.json"):
                self.markers[path] = json.loads(data.decode())
                return 0, b"", b""
            if self.corrupt_copy:
                data = data + b"x"
            self.files[path] = data
            return 0, b"", b""
        if cmd.startswith("sha256sum"):
            path = cmd.split()[1]
            data = self.files.get(path)
            if data is None:
                return 1, b"", b"no such file"
            return 0, f"{hashlib.sha256(data).hexdigest()}  {path}\n".encode(), b""
        if cmd.startswith("cat ") and "marker.json" in cmd:
            if not self.markers:
                return 1, b"", b""
            return 0, "".join(json.dumps(m) + "\n" for m in self.markers.values()).encode(), b""
        if "cat > /data/move.json" in cmd:
            self.data_files["/data/move.json"] = stdin or b""
            return 0, b"", b""
        if cmd.startswith("docker run --rm --entrypoint rm") and "/data/move.json" in cmd:
            self.data_files.pop("/data/move.json", None)
            return 0, b"", b""
        if "ops/restore.sh" in cmd and self.restore_fails_after_drop:
            self.target_running = False
            self.target_restored = True
            self.data_files = {}
            return 1, (b"==> Safety-dumping the current bank first...\n==> Stopping the daemon...\n"
                       b"==> Dropping + recreating pseudolife_memory...\n"), b"RESTORE FAILED mid-way"
        if "ops/restore.sh" in cmd:
            self.target_running = False
            self.target_restored = True
            self.target_empty = False
            self.data_files = {"/data/config.yaml": b"memory: {}\n", "/data/last-backup.json": b"{}\n"}
            return 0, b"==> Restore complete; the daemon was left stopped (--no-start).\n", b""
        if "coordination_bank_id" in cmd:
            meta = self.target_meta_after if self.target_restored else TARGET_META
            return 0, (meta + "\n").encode(), b""
        if "UNION ALL" in cmd:
            if not self.target_restored:
                return 0, b"", b""
            counts = dict(DUMP_COUNTS, **self.count_override)
            return 0, "".join(f"{t}|{n}\n" for t, n in counts.items()).encode(), b""
        if "relname IN" in cmd:
            return 0, b"entries\nfacts\nprincipals\n", b""
        if "count(*) FROM entries" in cmd:
            return 0, (b"0|0|0\n" if self.target_empty else b"5|3|0\n"), b""
        if "coordination_leases" in cmd:
            return 0, b"full-suite|laptop\n", b""
        if "coordination_messages" in cmd:
            return 0, b"0123456789abcdef0123456789abcdef|3\n", b""
        if cmd.startswith("if [ -e"):
            if self.dotenv is None:
                return 3, b"", b""
            return 0, self.dotenv.encode(), b""
        if "&& mv -f" in cmd:
            self.dotenv = (stdin or b"").decode()
            self.files[cmd.split()[-1]] = stdin or b""
            return 0, b"", b""
        if cmd.startswith("cp -p "):
            return 0, b"", b""
        if "ops/update.sh" in cmd:
            self.target_running = True
            self.started_at = self.clock.now
            self.target_live_env = _env_from_dotenv(self.dotenv or "")
            if self.env_mismatch:
                self.target_live_env = [e for e in self.target_live_env if not e.startswith("PSEUDOLIFE_MCP_TOKEN=")]
                self.target_live_env.append("PSEUDOLIFE_MCP_TOKEN=something-else")
            self._check_overlap()
            return 0, b"", b""
        if cmd.startswith("rm -f ") and cmd.endswith("/marker.json"):
            self.markers.pop(cmd[len("rm -f "):], None)
            return 0, b"", b""
        if cmd.startswith("rm -f ") and cmd.endswith("ops/.env"):
            self.dotenv = None
            return 0, b"", b""
        if cmd == "docker stop pseudolife-mcp-daemon":
            if self.stop_target_fails:
                return 1, b"", b"daemon unreachable"
            self.target_running = self.target_respawns > 0
            self.target_respawns = max(0, self.target_respawns - 1)
            return 0, b"", b""
        raise AssertionError(f"unexpected remote command: {cmd}")


class FakeRunner(move_cli.Runner):
    def __init__(self, world: World):
        self.world = world

    def run(self, argv, *, stdin=None, stdin_file=None, stdout_file=None, timeout=600, new_group=False):
        data = stdin if stdin is not None else (Path(stdin_file).read_bytes() if stdin_file else None)
        code, out, err = self.world.run(argv, data, new_group)
        if stdout_file is not None:
            Path(stdout_file).write_bytes(out if code == 0 else b"")
            out = b""
        return move_cli.Result(code, out, err)

    def get_json(self, url, *, token=None, timeout=5.0):
        return self.world.get_json(url, token)


class FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


SCHEDULE = {"where": "unit", "time": "03:30", "url": LOCAL}


def mover(world, tmp_path, *, schedule=SCHEDULE, interactive=False, answer=True, platform="linux", **options):
    opts = move_cli.Options(target=TARGET, checkout=CHECKOUT, yes=options.pop("yes", True),
                            health_delay=2.0, **options)
    return move_cli.Mover(opts, FakeRunner(world), detect_schedule=lambda: schedule,
                          interactive=lambda: interactive, ask=lambda question: answer,
                          sleep=world.clock.sleep, clock=world.clock, data_dir=tmp_path / "home",
                          lock=FakeLock, platform=platform)


def run_move(world, tmp_path, **kw):
    return mover(world, tmp_path, **kw).run()


def index(world, predicate) -> int:
    for i, call in enumerate(world.calls):
        if predicate(call):
            return i
    return -1


def first(world, needle) -> int:
    return index(world, lambda c: needle in c.text)


def remote_is(prefix):
    return lambda c: c.remote is not None and c.remote.startswith(prefix)


def local_is(*argv):
    return lambda c: c.remote is None and c.argv[:len(argv)] == list(argv)


def is_env_write(c) -> bool:
    return c.remote is not None and "&& mv -f" in c.remote and "ops/.env" in c.remote


MUTATING = ("docker stop", "docker start", "docker run", "docker update", "docker cp", "ALTER DATABASE",
            "pg_dump", "pg_terminate_backend", "bash ops/restore.sh", "bash ops/update.sh", "cat > ",
            "expose tailscale", " connect ", "systemctl --user disable", "systemctl --user enable",
            "schtasks /Change", "rm -f", "mkdir", "cp -p", "mv -f")


def mutating_calls(world):
    return [call for call in world.calls if any(word in call.text for word in MUTATING)]


# ── the whole move ───────────────────────────────────────────────────────────

def test_a_move_runs_every_step_in_order_and_reports(tmp_path, capsys):
    world = World()
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert report["exit"] == 0
    steps = [
        index(world, remote_is("systemctl --user disable")),          # the target's own update paused
        index(world, remote_is("docker update --restart=no")),        # ... and its autostart
        index(world, local_is("systemctl", "--user", "disable")),     # the source's schedule
        index(world, local_is("docker", "stop")),
        index(world, lambda c: "pg_terminate_backend" in c.text),    # writers cut off before the dump
        first(world, "pg_dump"),
        first(world, "ALLOW_CONNECTIONS false"),
        first(world, "count(*) FROM pg_stat_activity"),
        index(world, lambda c: c.argv[:2] == ["docker", "run"] and "tar" in c.argv),
        first(world, f"cat > {CHECKOUT}/data/move-"),
        index(world, lambda c: c.remote is not None and c.remote.endswith("/marker.json")
              and c.remote.startswith("cat > ")),
        first(world, "bash ops/restore.sh"),
        index(world, lambda c: c.remote is not None and "coordination_bank_id" in c.remote),
        first(world, "UNION ALL"),
        index(world, lambda c: c.remote is not None and "cat > /data/move.json" in c.remote),
        index(world, is_env_write),
        index(world, remote_is("docker run --rm --entrypoint rm")),  # the /data gate lifted
        first(world, "bash ops/update.sh"),
        index(world, lambda c: c.remote is not None and c.remote.startswith("docker inspect --type container")
              and world.calls.index(c) > first(world, "bash ops/update.sh")),
        index(world, local_is("docker", "update", "--restart=no")),  # the source never restarts itself
        index(world, lambda c: c.argv[:2] == ["docker", "cp"] and c.argv[-1].endswith(":/data/moved.json")),
        index(world, remote_is("docker update --restart=unless-stopped")),   # after the commit point,
        index(world, remote_is("systemctl --user enable")),                 # the target as it was
        index(world, lambda c: c.remote is not None and c.remote.startswith("rm -f ")
              and c.remote.endswith("/marker.json")),
        index(world, lambda c: "connect" in c.argv),
    ]
    assert -1 not in steps, steps
    assert steps == sorted(steps), steps
    assert not world.source_running and world.fenced and world.moved
    assert world.source_policy == "no"
    assert world.target_timer == "enabled" and world.target_policy == "unless-stopped"
    assert world.source_timer == "disabled"                          # left paused: a leftover to retire
    assert not any(c.argv[:2] == ["docker", "rm"] or " down" in c.text for c in world.calls)
    stop = world.calls[index(world, local_is("docker", "stop"))]
    assert stop.argv == ["docker", "stop", "-t", "120", "pseudolife-mcp-daemon"]
    restore_cmd = world.calls[first(world, "bash ops/restore.sh")].remote
    assert "--apply --no-start" in restore_cmd and "--backup-file" in restore_cmd
    assert "--state-archive" in restore_cmd
    assert world.markers == {} and "/data/move.json" not in world.data_files
    assert world.target_running and not world.overlap
    assert world.calls[steps[-1]].argv[-3:] == [TARGET_URL, "--yes", "--json"]


def test_the_report_lists_rollback_principals_leases_mail_registrations_and_leftovers(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)["report"]
    rollback = "\n".join(report["rollback"])
    assert f"ssh {TARGET} docker stop pseudolife-mcp-daemon" in rollback
    assert "ALLOW_CONNECTIONS true" in rollback
    assert "--volumes-from pseudolife-mcp-daemon" in rollback and "/data/moved.json" in rollback
    assert "docker update --restart=unless-stopped pseudolife-mcp-daemon" in rollback
    assert "docker start pseudolife-mcp-daemon" in rollback
    principals = {p["principal"]: p for p in report["principals"]}
    assert {"default", "laptop", "ci", "phone"} <= set(principals)
    assert principals["phone"]["kind"] == "stored"
    assert principals["laptop"]["line"] == f"pseudolife-mcp connect {TARGET_URL}"
    assert report["leases"] == [{"name": "full-suite", "holder_principal": "laptop",
                                 "line": f"ssh {TARGET} docker exec pseudolife-mcp-daemon python -m "
                                         "pseudolife_memory.cli lease break full-suite"}]
    assert report["mail"]["addresses"] == [{"agent_id": "0123456789abcdef0123456789abcdef", "pending": 3}]
    assert "coordination-recovery rebind" in report["mail"]["pointer"]
    assert report["target_registrations"] == [REGISTRATION]
    leftovers = " ".join(item["what"] + " " + item["command"] for item in report["leftovers"])
    for needle in ("backup", "extractor", "tunnel", "autostart", "expose off", "update --unschedule"):
        assert needle in leftovers, needle
    replaced = " ".join(report["replaced_on_target"])
    assert "config.yaml" in replaced and "last-backup.json" in replaced
    assert any("installer" in note and REGISTRATION in note for note in report["notes"])


def test_the_manual_rollback_quotes_work_in_every_shell(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path, as_json=True) == 0
    rollback = json.loads(capsys.readouterr().out)["report"]["rollback"]
    psql = next(line for line in rollback if "ALLOW_CONNECTIONS true" in line)
    assert psql.endswith('-c "ALTER DATABASE pseudolife_memory WITH ALLOW_CONNECTIONS true"'), psql
    assert psql.count('"') == 2 and "'" not in psql


def test_text_output_names_the_rollback_and_the_next_steps(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path) == 0
    out = capsys.readouterr().out
    assert "ALLOW_CONNECTIONS true" in out
    assert f"pseudolife-mcp connect {TARGET_URL}" in out
    assert "lease break full-suite" in out
    assert REGISTRATION in out


def test_every_remote_command_is_batch_mode_ssh_with_keepalives(tmp_path):
    world = World()
    assert run_move(world, tmp_path) == 0
    for call in world.calls:
        if call.argv[0] == "ssh":
            assert call.argv[:len(SSH) + 1] == SSH + [TARGET] and len(call.argv) == len(SSH) + 2, call


def test_files_are_hash_checked_on_the_target(tmp_path):
    world = World()
    assert run_move(world, tmp_path) == 0
    copies = [c for c in world.calls if c.remote and c.remote.startswith("cat > ")
              and not c.remote.endswith("/marker.json")]
    assert len(copies) == 3
    for copy in copies:
        path = copy.remote[len("cat > "):]
        assert any(c.remote and c.remote.startswith(f"sha256sum {path}") for c in world.calls), path


def test_the_manifest_carries_per_table_row_counts(tmp_path):
    world = World()
    assert run_move(world, tmp_path) == 0
    manifest_copy = next(c for c in world.calls if c.remote and c.remote.startswith("cat > ")
                         and c.remote.endswith("manifest.json"))
    manifest = json.loads(manifest_copy.stdin)
    assert manifest["tables"] == DUMP_COUNTS
    assert manifest["source_bank"] == SOURCE_BANK


def test_the_target_env_is_replaced_in_place_keeping_its_owner_and_mode(tmp_path):
    world = World()
    assert run_move(world, tmp_path) == 0
    write = next(c for c in world.calls if is_env_write(c))
    env = f"{CHECKOUT}/ops/.env"
    temporary = write.remote.split()[3]
    assert temporary.startswith(f"{env}.move-"), write.remote
    # A copy of the original (cp -p keeps its owner and mode) is overwritten
    # in place, then renamed over it.
    assert write.remote == f"cp -p {env} {temporary} && cat > {temporary} && mv -f {temporary} {env}"


# ── secrets ──────────────────────────────────────────────────────────────────

def _scan_for_secrets(world, out: str, err: str):
    for call in world.calls:
        for secret in SECRETS:
            assert secret not in call.text, (secret, call)
    for secret in SECRETS:
        assert secret not in out, secret
        assert secret not in err, secret


def test_no_token_or_password_ever_appears_in_a_command_line(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path, as_json=True) == 0
    captured = capsys.readouterr()
    _scan_for_secrets(world, captured.out, captured.err)
    env_write = next(c for c in world.calls if is_env_write(c))
    written = env_write.stdin.decode()
    assert f"PSEUDOLIFE_MCP_TOKEN={SOURCE_TOKEN}" in written
    assert f"PSEUDOLIFE_MCP_TOKENS={LAPTOP_TOKEN}:laptop,{CI_TOKEN}:ci" in written
    assert "PSEUDOLIFE_MCP_TIER_MAP=laptop:core,ci:minimal" in written
    assert TARGET_OLD_TOKEN not in written
    assert f"POSTGRES_PASSWORD={TARGET_PG_PASSWORD}" in written
    assert "\r" not in written
    grep = next(c for c in world.calls if c.remote and c.remote.startswith("grep -lsF -f -"))
    assert TARGET_OLD_TOKEN.encode() in grep.stdin


# ── preflight: refused, nothing changed ──────────────────────────────────────

PREFLIGHT_CASES = {
    "source_unhealthy": ({"source_healthy": False}, "/health"),
    "source_not_docker": ({"source_docker": False}, "Docker"),
    "source_no_fingerprint": ({"source_bank": None}, "fingerprint"),
    "ssh": ({"fail255": ("true",)}, "ssh"),
    "not_posix": ({"posix": False}, "Linux or macOS"),
    "checkout": ({"checkout": False}, "install"),
    "checkout_ssh_drop": ({"fail255": ("test -f",)}, "ssh"),
    "target_postgres_down": ({"target_pg_running": "false"}, "postgres"),
    "target_unhealthy": ({"target_healthy": False}, "target daemon"),
    "target_schema": ({"target_schema": 40}, "schema"),
    "same_bank": ({"target_bank": SOURCE_BANK}, "same bank"),
    "not_empty": ({"target_empty": False}, "not empty"),
}


@pytest.mark.parametrize("case", sorted(PREFLIGHT_CASES))
def test_preflight_refusals_exit_4_and_change_nothing(tmp_path, capsys, case):
    world_kw, needle = PREFLIGHT_CASES[case]
    world = World(**world_kw)
    code = run_move(world, tmp_path, as_json=True)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 4, report
    assert needle.lower() in report["error"].lower(), report["error"]
    assert mutating_calls(world) == []
    assert world.source_running and not world.fenced
    assert not (tmp_path / "home" / "moves").exists()
    _scan_for_secrets(world, captured.out, captured.err)


def test_a_null_target_fingerprint_counts_as_different(tmp_path):
    world = World(target_bank=None)
    assert run_move(world, tmp_path) == 0


def test_a_non_empty_target_with_a_marker_points_at_resume(tmp_path, capsys):
    world = World(target_empty=False,
                  markers={f"{CHECKOUT}/data/move-x/marker.json": {"move_id": "20261001-000000-00000000",
                                                                   "source_bank": SOURCE_BANK}})
    assert run_move(world, tmp_path, as_json=True) == 4
    assert "--resume" in json.loads(capsys.readouterr().out)["error"]


# ── dry run, confirmation ────────────────────────────────────────────────────

def test_dry_run_prints_the_plan_and_runs_only_read_only_commands(tmp_path, capsys):
    world = World(exposed=False)
    code = run_move(world, tmp_path, dry_run=True, yes=False)
    out = capsys.readouterr().out
    assert code == 0
    assert mutating_calls(world) == [], mutating_calls(world)
    assert "expose tailscale" in out
    assert "pg_dump" in out
    assert "10.0 MiB" in out
    assert "unattended update" in out.lower()
    assert "tmux" in out or "nohup" in out
    assert not (tmp_path / "home" / "moves").exists()


def test_no_tty_without_yes_exits_2_after_the_plan(tmp_path, capsys):
    world = World()
    code = run_move(world, tmp_path, yes=False, interactive=False, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and "--yes" in report["error"]
    assert mutating_calls(world) == []


def test_declining_exits_2_and_changes_nothing(tmp_path):
    world = World()
    assert run_move(world, tmp_path, yes=False, interactive=True, answer=False) == 2
    assert mutating_calls(world) == []


def test_usage_errors_exit_2(capsys):
    assert move_cli.main([]) == 2
    assert move_cli.main(["--to", "-oProxyCommand=evil"]) == 2
    assert move_cli.main(["--to", TARGET, "--target-url", "ftp://x/y"]) == 2


# ── exposure ─────────────────────────────────────────────────────────────────

def test_an_unexposed_target_is_exposed_before_the_source_stops(tmp_path):
    world = World(exposed=False)
    assert run_move(world, tmp_path) == 0
    expose = index(world, lambda c: c.remote is not None and "expose tailscale --yes" in c.remote)
    stop = index(world, local_is("docker", "stop"))
    assert -1 < expose < stop


def test_an_expose_refusal_stops_before_the_source_is_touched(tmp_path, capsys):
    world = World(exposed=False, expose_exit=4)
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 4 and "operator" in report["error"]
    assert world.source_running and index(world, local_is("docker", "stop")) == -1


def test_an_unreachable_target_url_stops_before_the_source_is_touched(tmp_path):
    world = World(fail=(f"GET {TARGET_URL}/health",))
    assert run_move(world, tmp_path) == 4
    assert world.source_running and index(world, local_is("docker", "stop")) == -1


def test_an_explicit_target_url_skips_the_exposure_read(tmp_path):
    world = World()
    assert run_move(world, tmp_path, target_url=TARGET_URL) == 0
    assert not any(c.remote and "expose" in c.remote for c in world.calls)


# ── failures in steps 4-9 roll back, in order ────────────────────────────────

def _env_backup(c) -> bool:
    return c.remote is not None and c.remote.startswith("cp -p") and len(c.remote.split()) == 4 \
        and c.remote.split()[3].split("/")[-1].startswith(".env.pre-move-")


ROLLBACK_CASES = {
    # name: (fail needle, what the rollback undoes)
    # (Before the source stops, nothing on it needs undoing: no schedule to
    # resume, no daemon to start.)
    "target_timer_pause": (remote_is("systemctl --user disable"),
                           {"target_started": False, "env": False, "fenced": False, "moved": False,
                            "resume": False, "start": False}),
    "schedule_pause": (local_is("systemctl", "--user", "disable"),
                       {"target_started": False, "env": False, "fenced": False, "moved": False,
                        "start": False}),
    "docker_stop": (local_is("docker", "stop"),
                    {"target_started": False, "env": False, "fenced": False, "moved": False}),
    "pg_dump": ("pg_dump", {"target_started": False, "env": False, "fenced": False, "moved": False}),
    "fence": ("ALLOW_CONNECTIONS false", {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "state_archive": ("czf -", {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "copy": (f"cat > {CHECKOUT}/data/move-", {"target_started": False, "env": False, "fenced": True,
                                              "moved": False}),
    "marker": (lambda c: c.remote is not None and c.remote.startswith("cat > ") and c.remote.endswith("marker.json"),
               {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "restore": ("bash ops/restore.sh", {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "env_backup": (_env_backup, {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "env_write": (is_env_write, {"target_started": False, "env": True, "fenced": True, "moved": False}),
    "update": ("bash ops/update.sh", {"target_started": True, "env": True, "fenced": True, "moved": False}),
    "moved_marker": (":/data/moved.json", {"target_started": True, "env": True, "fenced": True, "moved": True}),
}


@pytest.mark.parametrize("case", sorted(ROLLBACK_CASES))
def test_a_failure_rolls_back_in_order_and_restarts_the_source(tmp_path, capsys, case):
    needle, expect = ROLLBACK_CASES[case]
    world = World(fail=(needle,))
    code = run_move(world, tmp_path)
    captured = capsys.readouterr()
    assert code == 1, captured.err
    failed_at = index(world, needle if callable(needle) else (lambda c: needle in c.text))
    after = world.calls[failed_at + 1:]
    texts = [c.text for c in after]

    def at(pred):
        return next((i for i, c in enumerate(after) if pred(c)), None)

    stop_target = at(lambda c: c.remote == "docker stop pseudolife-mcp-daemon")
    env_restore = at(lambda c: c.remote is not None and c.remote.startswith("cp -p")
                     and ".pre-move-" in c.remote.split()[2])
    lift = at(lambda c: "ALLOW_CONNECTIONS true" in c.text)
    unmoved = at(lambda c: c.argv[:2] == ["docker", "run"] and "rm" in c.argv and "/data/moved.json" in c.argv)
    resume = at(local_is("systemctl", "--user", "enable"))
    start = at(local_is("docker", "start"))
    assert (stop_target is not None) == expect["target_started"], texts
    assert (env_restore is not None) == expect["env"], texts
    assert (lift is not None) == expect["fenced"], texts
    assert (unmoved is not None) == expect["moved"], texts
    assert (resume is not None) == expect.get("resume", True), texts
    assert (start is not None) == expect.get("start", True), texts
    order = [i for i in (stop_target, env_restore, lift, unmoved, resume, start) if i is not None]
    assert order == sorted(order), texts
    assert world.source_running and not world.fenced and not world.moved
    assert world.source_policy == "unless-stopped" and world.source_timer == "enabled"
    assert world.target_timer == "enabled" and world.target_policy == "unless-stopped"
    assert not world.overlap
    _scan_for_secrets(world, captured.out, captured.err)


def test_the_rollback_report_is_in_the_json(tmp_path, capsys):
    world = World(fail=("bash ops/update.sh",))
    assert run_move(world, tmp_path, as_json=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert [step["ok"] for step in report["rollback"]] and all(step["ok"] for step in report["rollback"])


def test_a_container_still_running_after_stop_rolls_back(tmp_path):
    world = World(still_running_after_stop=True)
    assert run_move(world, tmp_path) == 1
    assert first(world, "pg_dump") == -1


def test_a_daemon_killed_at_the_stop_timeout_is_reported(tmp_path, capsys):
    world = World(stop_exit_code=137)
    assert run_move(world, tmp_path, as_json=True) == 0
    warnings = json.loads(capsys.readouterr().out)["warnings"]
    assert any("137" in w for w in warnings), warnings


def test_a_dropped_ssh_connection_never_counts_as_a_missing_env_file(tmp_path):
    """Exit 255 is ssh's own failure, not the remote command's answer: the
    move stops instead of treating the target's ops/.env as absent, writing
    the carried keys over it with no backup and removing it on rollback."""
    world = World(fail255=(lambda c: c.remote is not None and c.remote.startswith("if [ -e"),))
    assert run_move(world, tmp_path) == 1
    assert not any(is_env_write(c) for c in world.calls)
    assert not any(c.remote and c.remote.startswith("rm -f") and "ops/.env" in c.remote for c in world.calls)
    assert world.dotenv == TARGET_DOTENV
    assert world.source_running and not world.fenced


def test_a_missing_target_env_is_written_owner_only_and_removed_on_rollback(tmp_path):
    world = World(dotenv=None, fail=("bash ops/update.sh",))
    assert run_move(world, tmp_path) == 1
    write = next(c for c in world.calls if is_env_write(c))
    assert write.remote.startswith("umask 077 && cat > ")
    assert any(c.remote and c.remote.startswith("rm -f") and c.remote.endswith("ops/.env") for c in world.calls)


def test_an_unreadable_target_env_is_never_replaced_or_removed(tmp_path):
    world = World(fail=(lambda c: c.remote is not None and c.remote.startswith("if [ -e"),))
    assert run_move(world, tmp_path) == 1
    assert not any(is_env_write(c) for c in world.calls)
    assert not any(c.remote and c.remote.startswith("rm -f") and "ops/.env" in c.remote for c in world.calls)
    assert world.source_running and not world.fenced


def test_lingering_backends_after_the_fence_fail_the_step(tmp_path, capsys):
    world = World(lingering_backends=2)
    assert run_move(world, tmp_path, as_json=True) == 1
    assert "pg_stat_activity" in json.loads(capsys.readouterr().out)["error"]
    assert index(world, lambda c: c.argv[:2] == ["docker", "run"] and "tar" in c.argv) == -1
    assert world.source_running and not world.fenced


def test_a_hash_mismatch_on_the_target_rolls_back(tmp_path):
    world = World(corrupt_copy=True)
    assert run_move(world, tmp_path) == 1
    assert world.source_running and not world.fenced
    assert first(world, "bash ops/restore.sh") == -1


def test_a_row_count_mismatch_rolls_back_before_the_target_starts(tmp_path, capsys):
    world = World(count_override={"public.entries": 1})
    assert run_move(world, tmp_path, as_json=True) == 1
    assert "public.entries" in json.loads(capsys.readouterr().out)["error"]
    assert first(world, "bash ops/update.sh") == -1
    assert world.source_running and not world.fenced


def test_a_restored_bank_identity_that_differs_rolls_back_before_the_start(tmp_path, capsys):
    world = World(target_meta_after=TARGET_META)
    assert run_move(world, tmp_path, as_json=True) == 1
    assert "coordination_bank_id" in json.loads(capsys.readouterr().out)["error"]
    assert first(world, "bash ops/update.sh") == -1


def test_the_restored_target_cannot_start_outside_the_move(tmp_path):
    """Between the restore and the move's own start, the target's /data
    holds move.json, which a daemon refuses to start on; the rollback puts
    it back after stopping a target it had started."""
    seen = {}

    def at_update(world, call):
        seen["gate_before_update"] = "/data/move.json" in world.data_files

    world = World(fail=("bash ops/update.sh",), hooks={"bash ops/update.sh": at_update})
    assert run_move(world, tmp_path) == 1
    assert seen == {"gate_before_update": False}   # lifted just before the move's own start
    assert "/data/move.json" in world.data_files    # and back once the rollback stopped the target
    stop = index(world, lambda c: c.remote == "docker stop pseudolife-mcp-daemon")
    gate = index(world, lambda c: c.remote is not None and "cat > /data/move.json" in c.remote
                 and world.calls.index(c) > stop)
    assert -1 < stop < gate


def test_a_target_reporting_another_bank_after_start_rolls_back(tmp_path):
    world = World(bank_after_start=TARGET_BANK)
    assert run_move(world, tmp_path) == 1
    assert world.source_running and not world.target_running
    stop_target = index(world, lambda c: c.remote == "docker stop pseudolife-mcp-daemon")
    start_source = index(world, local_is("docker", "start"))
    assert -1 < stop_target < start_source
    # The resume marker survives, outside /data.
    assert len(world.markers) == 1


def test_health_never_coming_up_after_the_start_fails_after_the_deadline(tmp_path):
    world = World(target_up_after_start=False)
    assert run_move(world, tmp_path) == 1
    started = world.started_at
    last_poll = max(t for url, _token, t in world.gets if url == f"{TARGET_URL}/health")
    assert last_poll - started >= 180
    assert world.source_running and not world.target_running


def test_a_slow_fingerprint_is_waited_for_with_a_nudge_a_minute(tmp_path):
    world = World(bank_appears_at=130)
    assert run_move(world, tmp_path) == 0
    nudges = [t for url, token, t in world.gets if url == f"{TARGET_URL}/api/stats"]
    assert len(nudges) >= 2 and all(b - a >= 60 for a, b in zip(nudges, nudges[1:]))
    assert all(token for url, token, _t in world.gets if url.endswith("/api/stats"))


def test_a_null_fingerprint_until_the_deadline_rolls_back(tmp_path):
    world = World(bank_after_start=None)
    assert run_move(world, tmp_path) == 1
    assert world.source_running


def test_a_carried_value_the_target_did_not_take_rolls_back(tmp_path, capsys):
    world = World(env_mismatch=True)
    assert run_move(world, tmp_path, as_json=True) == 1
    captured = capsys.readouterr()
    assert "PSEUDOLIFE_MCP_TOKEN" in json.loads(captured.out)["error"]
    assert world.source_running and not world.target_running
    _scan_for_secrets(world, captured.out, captured.err)


def test_a_state_archive_holding_a_marker_is_refused(tmp_path):
    world = World(tar_members={"./config.yaml": b"x", "./moved.json": b"{}"})
    assert run_move(world, tmp_path) == 1
    assert world.source_running


def test_when_the_target_cannot_be_stopped_the_source_stays_down(tmp_path, capsys):
    world = World(fail=("bash ops/update.sh",), stop_target_fails=True)
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 6
    assert index(world, local_is("docker", "start")) == -1
    assert not world.source_running
    assert "incomplete" in report["error"].lower()


def test_a_fence_that_cannot_be_lifted_keeps_the_source_down(tmp_path, capsys):
    world = World(fail=("bash ops/restore.sh", "ALLOW_CONNECTIONS true"))
    assert run_move(world, tmp_path, as_json=True) == 6
    assert index(world, local_is("docker", "start")) == -1
    assert world.fenced and not world.source_running


def test_a_moved_marker_that_cannot_be_removed_keeps_the_source_down(tmp_path):
    world = World(fail=(":/data/moved.json",
                        lambda c: c.argv[:2] == ["docker", "run"] and "/data/moved.json" in c.argv))
    assert run_move(world, tmp_path) == 6
    assert index(world, local_is("docker", "start")) == -1
    assert not world.source_running


# ── kills and the record ─────────────────────────────────────────────────────

def test_a_sigterm_during_the_move_rolls_back_with_sigint_ignored(tmp_path):
    seen = {}

    def terminate(world, call):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler), handler
        handler(signal.SIGTERM, None)

    def at_start(world, call):
        seen["sigint"] = signal.getsignal(signal.SIGINT)

    world = World(hooks={"bash ops/restore.sh": terminate, "docker start": at_start})
    before = signal.getsignal(signal.SIGTERM)
    move = mover(world, tmp_path, as_json=True)
    assert move.run() == 1
    assert "interrupted by SIGTERM" in move.data["error"]
    assert seen["sigint"] is signal.SIG_IGN
    assert world.source_running and not world.fenced
    assert signal.getsignal(signal.SIGTERM) is before


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_a_sighup_during_the_move_rolls_back(tmp_path):
    def hangup(world, call):
        signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)

    world = World(hooks={"pg_dump": hangup})
    assert run_move(world, tmp_path) == 1
    assert world.source_running


def test_the_record_holds_the_progress_and_the_manual_rollback_as_it_goes(tmp_path):
    snapshots = {}

    def at_fence(world, call):
        records = list((tmp_path / "home" / "moves").glob("*/record.json"))
        snapshots["record"] = json.loads(records[0].read_text())

    world = World(hooks={"ALLOW_CONNECTIONS false": at_fence})
    assert run_move(world, tmp_path) == 0
    record = snapshots["record"]
    assert record["flags"]["source_stopped"] is True and record["flags"]["fenced"] is True
    assert record["flags"]["target_started"] is False
    assert any("ALLOW_CONNECTIONS true" in line for line in record["manual_rollback"])


# ── schedules and restart policies ───────────────────────────────────────────

def test_a_disabled_schedule_or_timer_is_left_alone(tmp_path):
    world = World(source_timer="disabled", target_timer="disabled", fail=("bash ops/update.sh",))
    assert run_move(world, tmp_path) == 1
    assert index(world, local_is("systemctl", "--user", "enable")) == -1
    assert index(world, local_is("systemctl", "--user", "disable")) == -1
    assert index(world, remote_is("systemctl --user enable")) == -1
    assert world.source_timer == "disabled" and world.target_timer == "disabled"


def test_the_windows_schedule_is_queried_then_paused_with_schtasks(tmp_path):
    world = World()
    assert run_move(world, tmp_path, platform="win32") == 0
    assert any(c.argv[:2] == ["schtasks", "/Query"] for c in world.calls)
    assert any(c.argv[:2] == ["schtasks", "/Change"] and "/DISABLE" in c.argv for c in world.calls)


# ── after the bank moved ─────────────────────────────────────────────────────

def test_a_failed_repoint_exits_5_without_rolling_back(tmp_path, capsys):
    world = World(connect_exit=4)
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 5
    assert f"pseudolife-mcp connect {TARGET_URL}" in report["error"]
    assert not world.source_running and world.fenced and world.moved
    assert index(world, local_is("docker", "start")) == -1


def test_a_connect_reply_that_is_not_an_object_still_reports(tmp_path, capsys):
    world = World(connect_exit=1, connect_reply=["unexpected"])
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 5 and report["report"]["rollback"]


def test_no_registration_to_repoint_is_not_a_failure(tmp_path):
    world = World(connect_exit=3)
    assert run_move(world, tmp_path) == 0


def test_no_keep_tokens_writes_no_env_and_lists_who_to_reinvite(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path, keep_tokens=False, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)["report"]
    assert not any(is_env_write(c) for c in world.calls)
    reinvite = {p["principal"] for p in report["reinvite"]}
    assert reinvite == {"default", "laptop", "ci"}
    assert all("pseudolife-mcp invite" in p["line"] for p in report["reinvite"])


# ── --resume ─────────────────────────────────────────────────────────────────

def _record(tmp_path, move_id, bank=SOURCE_BANK):
    directory = tmp_path / "home" / "moves" / move_id
    directory.mkdir(parents=True)
    (directory / "record.json").write_text(json.dumps({"move_id": move_id, "target": TARGET,
                                                       "source_bank": bank, "state": "rolled_back"}))


def _marker(move_id, bank=SOURCE_BANK):
    return {f"{CHECKOUT}/data/move-{move_id}/marker.json": {"move_id": move_id, "source_bank": bank}}


def test_resume_is_refused_without_a_marker(tmp_path, capsys):
    world = World(target_empty=False)
    assert run_move(world, tmp_path, resume=True, as_json=True) == 4
    assert "marker" in json.loads(capsys.readouterr().out)["error"]
    assert mutating_calls(world) == []


def test_resume_is_refused_over_another_moves_marker(tmp_path):
    _record(tmp_path, "20261001-000000-11111111")
    world = World(target_empty=False, markers=_marker("20261001-000000-22222222"))
    assert run_move(world, tmp_path, resume=True) == 4
    assert mutating_calls(world) == []


def test_resume_is_refused_over_a_marker_from_another_source(tmp_path):
    _record(tmp_path, "20261001-000000-11111111")
    world = World(target_empty=False, markers=_marker("20261001-000000-11111111", bank="c" * 16))
    assert run_move(world, tmp_path, resume=True) == 4


def test_resume_takes_over_this_moves_half_restored_bank(tmp_path, capsys):
    move_id = "20261001-000000-11111111"
    _record(tmp_path, move_id)
    world = World(target_empty=False, target_healthy=False, markers=_marker(move_id))
    world.target_running = False
    code = run_move(world, tmp_path, resume=True, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert report["move_id"] == move_id
    assert first(world, f"data/move-{move_id}") > -1
    assert world.markers == {}


# ── pieces ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    (json.dumps({"exposed": True, "url": TARGET_URL}), ("exposed", TARGET_URL)),
    (json.dumps({"url": TARGET_URL, "state": "exposed"}, indent=2), ("exposed", TARGET_URL)),
    (json.dumps({"exposed": False, "url": None}), ("not-exposed", None)),
    (json.dumps({"state": "not exposed"}), ("not-exposed", None)),
    ("note: something\n" + json.dumps({"url": TARGET_URL}), ("exposed", TARGET_URL)),
    ("not exposed", ("not-exposed", None)),
    ("", ("unknown", None)),
    ("unknown mode 'expose'", ("unknown", None)),
    (json.dumps({"url": "ftp://x"}), ("unknown", None)),
])
def test_the_expose_status_parser_tolerates_its_shapes(text, expected):
    assert move_cli.parse_expose_status(text) == expected


def test_merge_env_replaces_only_the_carried_keys():
    merged = move_cli.merge_env(
        "A=1\r\nexport PSEUDOLIFE_MCP_TOKEN=old\r\n# keep me\r\nPSEUDOLIFE_MCP_TOOLSET=full\r\n",
        {"PSEUDOLIFE_MCP_TOKEN": "new", "PSEUDOLIFE_MCP_TOKENS": "t:p", "PSEUDOLIFE_MCP_TIER_MAP": "",
         "PSEUDOLIFE_MCP_TOOLSET": "core"}, "m1")
    assert "\r" not in merged
    lines = merged.splitlines()
    assert "A=1" in lines and "# keep me" in lines
    assert "PSEUDOLIFE_MCP_TOKEN=new" in lines and "PSEUDOLIFE_MCP_TOKENS=t:p" in lines
    assert "PSEUDOLIFE_MCP_TIER_MAP=" in lines and "PSEUDOLIFE_MCP_TOOLSET=core" in lines
    assert not any("old" in line or line.endswith("=full") for line in lines)
    assert merged.endswith("\n")


def test_merge_env_refuses_a_value_it_cannot_write_safely():
    with pytest.raises(move_cli.MoveError):
        move_cli.merge_env("", {"PSEUDOLIFE_MCP_TOKEN": "a'b\nc"}, "m1")


def test_scan_dump_counts_rows_and_needs_the_end_marker(tmp_path):
    good = tmp_path / "good.sql.gz"
    good.write_bytes(gzip.compress(DUMP.encode()))
    assert move_cli.scan_dump(good) == (True, DUMP_COUNTS)
    cut = tmp_path / "cut.sql.gz"
    cut.write_bytes(gzip.compress(DUMP.split("COPY public.facts")[0].encode()))
    complete, _counts = move_cli.scan_dump(cut)
    assert complete is False


def test_the_move_module_is_wired_into_the_console_script():
    from pseudolife_memory import cli
    assert any(line.startswith("  move ") for line in cli._USAGE.splitlines())


# ── the fence, against a real Postgres ───────────────────────────────────────

def test_a_fenced_database_refuses_new_connections_until_lifted():
    psycopg = pytest.importorskip("psycopg")
    from tests.helpers import pg_reachable
    from tests.pg_defaults import bench_admin_url, conninfo_with_dbname

    admin = conninfo_with_dbname(os.environ.get("PSEUDOLIFE_BENCH_ADMIN_URL") or bench_admin_url(), "postgres")
    if not pg_reachable(admin):
        pytest.skip("the bench Postgres is not reachable")
    name = f"pseudolife_move_fence_{os.getpid()}"
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        target = conninfo_with_dbname(admin, name)
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            conn.execute(move_cli.fence_sql(name, allow=False))
        with pytest.raises(psycopg.OperationalError) as refused:
            psycopg.connect(target, connect_timeout=5).close()
        assert "not currently accepting connections" in str(refused.value)
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            conn.execute(move_cli.fence_sql(name, allow=True))
        with psycopg.connect(target, connect_timeout=5) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ── after the commit point (moved.json written): follow-ups, never a rollback ──

FOLLOW_UPS = {
    "target_policy": remote_is("docker update --restart=unless-stopped"),
    "target_timer": remote_is("systemctl --user enable"),
    "marker": lambda c: c.remote is not None and c.remote.startswith("rm -f ") and c.remote.endswith("/marker.json"),
}


@pytest.mark.parametrize("case", sorted(FOLLOW_UPS))
def test_a_dropped_ssh_after_the_commit_point_never_rolls_back(tmp_path, capsys, case):
    world = World(fail255=(FOLLOW_UPS[case],))
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 5, report
    assert report["rollback"] == []
    assert not world.source_running and world.fenced and world.moved and world.target_running
    assert index(world, local_is("docker", "start")) == -1
    assert "follow-up" in report["error"]
    follow_ups = report["report"]["follow_ups"]
    assert len(follow_ups) == 1 and follow_ups[0]["command"].startswith(f"ssh {TARGET} "), follow_ups


# ── the rollback: gate, a target that comes back, the restore boundary ──────

def test_a_gate_the_rollback_cannot_write_leaves_the_targets_update_and_policy_paused(tmp_path, capsys):
    world = World(fail=("bash ops/update.sh",))
    stopped = []
    world.hooks = {"docker stop pseudolife-mcp-daemon": lambda w, c: stopped.append(c) if c.remote else None}
    world.fail = ("bash ops/update.sh",
                  lambda c: bool(stopped) and c.remote is not None and "cat > /data/move.json" in c.remote)
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 6, report
    after = world.calls[index(world, lambda c: c.remote == "docker stop pseudolife-mcp-daemon"):]
    assert not any(c.remote and c.remote.startswith(("systemctl --user enable", "docker update --restart=unless"))
                   for c in after)
    assert world.target_timer == "disabled" and world.target_policy == "no"
    assert any("left paused" in step["step"] or "left paused" in step.get("detail", "")
               for step in report["rollback"]), report["rollback"]


def test_a_target_that_comes_back_after_the_stop_is_stopped_again_first(tmp_path):
    world = World(fail=("bash ops/update.sh",), target_respawns=1)
    assert run_move(world, tmp_path) == 1
    stops = [i for i, c in enumerate(world.calls) if c.remote == "docker stop pseudolife-mcp-daemon"]
    start = index(world, local_is("docker", "start"))
    assert len(stops) == 2 and stops[-1] < start
    checks = [i for i, c in enumerate(world.calls) if c.remote is not None
              and c.remote.startswith("docker inspect -f") and c.remote.endswith("pseudolife-mcp-daemon")]
    assert checks and checks[-1] < start
    assert not world.overlap and world.source_running and not world.target_running


def test_a_target_that_will_not_stay_stopped_keeps_the_source_down(tmp_path, capsys):
    world = World(fail=("bash ops/update.sh",), target_respawns=10)
    assert run_move(world, tmp_path, as_json=True) == 6
    assert "still running" in json.loads(capsys.readouterr().out)["error"]
    assert index(world, local_is("docker", "start")) == -1
    assert not world.source_running and not world.overlap


def test_a_restore_that_failed_before_touching_the_bank_is_not_gated(tmp_path):
    """restore.sh's own safety dump failed: the target still holds its own
    bank, so the rollback must not gate it."""
    world = World(fail=("bash ops/restore.sh",))
    assert run_move(world, tmp_path) == 1
    assert not any(c.remote and "cat > /data/move.json" in c.remote for c in world.calls)


def test_a_restore_that_failed_after_dropping_the_bank_is_gated(tmp_path):
    world = World(restore_fails_after_drop=True)
    assert run_move(world, tmp_path) == 1
    assert "/data/move.json" in world.data_files


def test_a_dropped_ssh_during_the_restore_counts_as_a_replaced_bank(tmp_path):
    world = World(fail255=("bash ops/restore.sh",))
    assert run_move(world, tmp_path) == 1
    assert "/data/move.json" in world.data_files


# ── the manual rollback lists only what is needed ───────────────────────────

def test_the_recorded_manual_rollback_follows_the_progress(tmp_path):
    snapshots = {}

    def at_fence(world, call):
        records = list((tmp_path / "home" / "moves").glob("*/record.json"))
        snapshots["fence"] = json.loads(records[0].read_text())["manual_rollback"]

    world = World(hooks={"ALLOW_CONNECTIONS false": at_fence})
    assert run_move(world, tmp_path) == 0
    lines = snapshots["fence"]
    # The target was not started, restored or rewritten yet: only what step 4
    # paused there needs putting back.
    target_lines = [line for line in lines if line.startswith(f"ssh {TARGET}")]
    assert target_lines == [f"ssh {TARGET} systemctl --user enable --now pseudolife-update.timer",
                            f"ssh {TARGET} docker update --restart=unless-stopped pseudolife-mcp-daemon"], lines
    assert any("ALLOW_CONNECTIONS true" in line for line in lines)
    assert lines.index(next(l for l in lines if "ALLOW_CONNECTIONS true" in l)) < \
        lines.index("docker start pseudolife-mcp-daemon")


def test_the_reported_manual_rollback_regates_the_target_before_the_source_starts(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path, as_json=True) == 0
    lines = json.loads(capsys.readouterr().out)["report"]["rollback"]
    gate = next(i for i, line in enumerate(lines) if "/data/move.json" in line and line.startswith(f"ssh {TARGET}"))
    env = next(i for i, line in enumerate(lines) if line.startswith(f"ssh {TARGET} cp -p") and ".pre-move-" in line)
    start = lines.index("docker start pseudolife-mcp-daemon")
    assert lines[0] == f"ssh {TARGET} docker stop pseudolife-mcp-daemon"
    assert 0 < gate < start and env < start


# ── Windows: SIGBREAK, and a rollback a second Ctrl-C cannot kill ────────────

@pytest.mark.skipif(not hasattr(signal, "SIGBREAK"), reason="SIGBREAK is Windows-only")
def test_a_sigbreak_during_the_move_rolls_back(tmp_path):
    def brk(world, call):
        signal.getsignal(signal.SIGBREAK)(signal.SIGBREAK, None)

    world = World(hooks={"pg_dump": brk})
    move = mover(world, tmp_path, as_json=True)
    assert move.run() == 1
    assert "SIGBREAK" in move.data["error"] and world.source_running


def test_rollback_steps_run_in_their_own_process_group_on_windows(tmp_path):
    world = World(fail=("bash ops/update.sh",))
    assert run_move(world, tmp_path, platform="win32") == 1
    failed = first(world, "bash ops/update.sh")
    assert not any(c.new_group for c in world.calls[:failed + 1])
    assert world.calls[failed + 1:] and all(c.new_group for c in world.calls[failed + 1:])


def test_no_process_groups_off_windows(tmp_path):
    world = World(fail=("bash ops/update.sh",))
    assert run_move(world, tmp_path) == 1
    assert not any(c.new_group for c in world.calls)


# ── nits ─────────────────────────────────────────────────────────────────────

def test_an_on_failure_restart_policy_comes_back_with_its_count(tmp_path):
    world = World(source_policy="on-failure:3", target_policy="on-failure:5", fail=(":/data/moved.json",))
    world.fail = (lambda c: c.argv[:2] == ["docker", "cp"] and c.argv[-1].endswith(":/data/moved.json"),)
    assert run_move(world, tmp_path) == 1
    assert world.source_policy == "on-failure:3" and world.target_policy == "on-failure:5"


def test_a_target_timer_that_cannot_be_read_is_reported(tmp_path, capsys):
    world = World(target_timer="no-bus")
    assert run_move(world, tmp_path, as_json=True) == 0
    notes = " ".join(json.loads(capsys.readouterr().out)["report"]["notes"])
    assert "systemctl --user" in notes and "another user" in notes
