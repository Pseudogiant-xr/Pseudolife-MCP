"""``pseudolife-mcp move``: move a Docker-tier bank to another host.

Every external effect of a move (``ssh`` to the target, ``docker`` on the
source, the schedule and ``connect`` subprocesses, the HTTP health reads)
goes through one runner. Here that runner is a recording fake backed by a
small model of both hosts (:class:`World`): it answers each command from
the model's state, changes the state the way the real command would, and
records every call, so a test can assert what ran, in what order, and what
never ran. A failure is injected by naming a substring of the command that
should fail.

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
SOURCE_TOKEN = "src-singular-token-0123456789abcdef"
LAPTOP_TOKEN = "laptop-token-0123456789abcdef0123"
CI_TOKEN = "ci-token-fedcba9876543210fedcba98"
TARGET_OLD_TOKEN = "target-installer-token-5555555555"
SOURCE_PG_PASSWORD = "pgsecret-source-1234"
TARGET_PG_PASSWORD = "pgsecret-target-5678"
SECRETS = (SOURCE_TOKEN, LAPTOP_TOKEN, CI_TOKEN, TARGET_OLD_TOKEN, SOURCE_PG_PASSWORD, TARGET_PG_PASSWORD)

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


def _plain_tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Call:
    def __init__(self, argv, stdin):
        self.argv = argv
        self.stdin = stdin
        self.text = " ".join(argv)

    @property
    def remote(self) -> str | None:
        """The remote command of an ``ssh`` call, else ``None``."""
        if self.argv[:3] == ["ssh", "-o", "BatchMode=yes"] and len(self.argv) == 5:
            return self.argv[4]
        return None

    def __repr__(self):
        return f"Call({self.text!r})"


class World:
    """Both hosts, as far as a move can see them."""

    def __init__(self, *, fail=(), target_empty=True, exposed=True, target_schema=52,
                 target_bank=TARGET_BANK, source_bank=SOURCE_BANK, bank_after_start=SOURCE_BANK,
                 count_override=None, corrupt_copy=False, marker=None, connect_exit=0,
                 checkout=True, ssh_ok=True, source_healthy=True, source_docker=True,
                 target_healthy=True, expose_exit=0, stop_target_fails=False,
                 tar_members=None):
        self.fail = tuple(fail)
        self.calls: list[Call] = []
        self.gets: list[tuple[str, bool]] = []
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
        self.target_restored = False
        self.marker = marker
        self.files: dict[str, bytes] = {}
        self.dotenv = TARGET_DOTENV
        self.exposed = exposed
        self.count_override = count_override or {}
        self.corrupt_copy = corrupt_copy
        self.connect_exit = connect_exit
        self.checkout = checkout
        self.ssh_ok = ssh_ok
        self.expose_exit = expose_exit
        self.stop_target_fails = stop_target_fails
        self.tar_members = tar_members if tar_members is not None else {
            "./config.yaml": b"memory: {}\n", "./last-backup.json": b"{}\n"}
        self.overlap = False   # ever a running target beside a running source on the moved bank

    # -- helpers ---------------------------------------------------------
    def failing(self, text: str) -> bool:
        return any(needle in text for needle in self.fail)

    def _check_overlap(self):
        if self.source_running and self.target_running and self.target_restored:
            self.overlap = True

    # -- the runner surface ----------------------------------------------
    def run(self, argv, stdin=None):
        call = Call([str(a) for a in argv], stdin)
        self.calls.append(call)
        if self.failing(call.text):
            return 1, b"", b"injected failure"
        if call.remote is not None:
            return self.remote(call.remote, stdin)
        return self.local(call.argv, stdin)

    def get_json(self, url, token=None):
        self.gets.append((url, token is not None))
        if url == LOCAL + "/health":
            if not (self.source_running and self.source_healthy):
                return None
            return {"status": "ok", "schema": 52, "auth": True, "bank": self.source_bank, "version": "0.15.0"}
        if url == TARGET_URL + "/health":
            if not self.target_running or self.failing("GET " + url):
                return None
            bank = self.bank_after_start if self.target_restored else self.target_bank
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
                                       "State": {"Running": self.source_running}}]).encode(), b""
            return 0, (b"true\n" if self.source_running else b"false\n"), b""
        if argv[:2] == ["docker", "stop"]:
            self.source_running = False
            return 0, b"", b""
        if argv[:2] == ["docker", "start"]:
            self.source_running = True
            self._check_overlap()
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
            return self.connect_exit, json.dumps({"exit": self.connect_exit, "rows": []}).encode(), b""
        if argv[0] in ("systemctl", "schtasks"):
            return 0, b"", b""
        raise AssertionError(f"unexpected local command: {text}")

    def remote(self, cmd, stdin):
        if not self.ssh_ok:
            return 255, b"", b"Permission denied (publickey)."
        if cmd == "true":
            return 0, b"", b""
        if cmd.startswith("test -e"):
            return (0 if self.dotenv is not None else 1), b"", b""
        if cmd.startswith("test -f"):
            return (0 if self.checkout else 1), b"", b""
        if cmd.startswith("grep -q -- --no-start"):
            return 0, b"", b""
        if cmd.startswith("docker inspect --type container pseudolife-mcp-daemon"):
            return 0, json.dumps([{"Config": {"Env": TARGET_ENV, "Image": "pseudolife-daemon:0.15.0"},
                                   "State": {"Running": self.target_running}}]).encode(), b""
        if cmd.startswith("docker inspect -f"):
            return 0, b"true\n", b""
        if cmd.startswith("curl -fsS"):
            if not (self.target_running and self.target_healthy):
                return 7, b"", b"connection refused"
            return 0, json.dumps({"status": "ok", "schema": self.target_schema, "auth": True,
                                  "bank": self.target_bank}).encode(), b""
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
            data = stdin or b""
            if self.corrupt_copy:
                data = data + b"x"
            self.files[cmd[len("cat > "):]] = data
            return 0, b"", b""
        if cmd.startswith("sha256sum"):
            path = cmd.split()[1]
            data = self.files.get(path)
            if data is None:
                return 1, b"", b"no such file"
            return 0, f"{hashlib.sha256(data).hexdigest()}  {path}\n".encode(), b""
        if cmd.startswith("docker cp pseudolife-mcp-daemon:/data/move.json -"):
            if self.marker is None:
                return 1, b"", b"Error: Could not find the file /data/move.json"
            return 0, _plain_tar({"move.json": json.dumps(self.marker).encode()}), b""
        if "cat > /data/move.json" in cmd:
            self.marker = json.loads(stdin.decode())
            return 0, b"", b""
        if "ops/restore.sh" in cmd:
            self.target_running = False
            self.target_restored = True
            self.target_empty = False
            return 0, b"==> Restore complete; the daemon was left stopped (--no-start).\n", b""
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
        if cmd.startswith("cat "):
            return 0, self.dotenv.encode(), b""
        if cmd.startswith("cp -p "):
            return 0, b"", b""
        if cmd.startswith("umask 077"):
            self.dotenv = (stdin or b"").decode()
            self.files[cmd.split()[-1]] = stdin or b""   # ... && mv -f <tmp> <env>
            return 0, b"", b""
        if "ops/update.sh" in cmd:
            self.target_running = True
            self._check_overlap()
            return 0, b"", b""
        if cmd == "docker exec pseudolife-mcp-daemon rm -f /data/move.json":
            self.marker = None
            return 0, b"", b""
        if cmd == "docker stop pseudolife-mcp-daemon":
            if self.stop_target_fails:
                return 1, b"", b"daemon unreachable"
            self.target_running = False
            return 0, b"", b""
        raise AssertionError(f"unexpected remote command: {cmd}")


class FakeRunner(move_cli.Runner):
    def __init__(self, world: World):
        self.world = world

    def run(self, argv, *, stdin=None, stdin_file=None, stdout_file=None, timeout=600):
        data = stdin if stdin is not None else (Path(stdin_file).read_bytes() if stdin_file else None)
        code, out, err = self.world.run(argv, data)
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


def mover(world, tmp_path, *, schedule=None, interactive=False, answer=True, platform="linux", **options):
    opts = move_cli.Options(target=TARGET, checkout=CHECKOUT, yes=options.pop("yes", True),
                            health_retries=3, health_delay=0, **options)
    return move_cli.Mover(opts, FakeRunner(world), detect_schedule=lambda: schedule,
                          interactive=lambda: interactive, ask=lambda question: answer,
                          sleep=lambda s: None, data_dir=tmp_path / "home", lock=FakeLock,
                          platform=platform)


def run_move(world, tmp_path, **kw):
    return mover(world, tmp_path, **kw).run()


def index(world, predicate) -> int:
    for i, call in enumerate(world.calls):
        if predicate(call):
            return i
    return -1


def first(world, needle) -> int:
    return index(world, lambda c: needle in c.text)


MUTATING = ("docker stop", "docker start", "docker run", "ALTER DATABASE", "pg_dump", "pg_terminate_backend",
            "bash ops/restore.sh", "bash ops/update.sh", "cat > ", "expose tailscale", " connect ",
            "systemctl", "schtasks", "rm -f", "mkdir", "cp -p", "umask")


def mutating_calls(world):
    out = []
    for call in world.calls:
        text = call.text
        if any(word in text for word in MUTATING):
            out.append(call)
        elif text.startswith("docker cp") and "move.json -" not in text:
            out.append(call)
        elif call.remote and call.remote.startswith("docker cp") and "move.json -" not in call.remote:
            out.append(call)
    return out


@pytest.fixture(autouse=True)
def _quiet(capsys):
    yield


# ── the whole move ───────────────────────────────────────────────────────────

def test_a_move_runs_every_step_in_order_and_reports(tmp_path, capsys):
    world = World()
    code = run_move(world, tmp_path, as_json=True)
    out = capsys.readouterr().out
    report = json.loads(out)
    assert code == 0, report
    assert report["exit"] == 0
    stop = first(world, "docker stop pseudolife-mcp-daemon")
    dump = first(world, "pg_dump")
    fence = first(world, "ALLOW_CONNECTIONS false")
    state = index(world, lambda c: c.argv[:2] == ["docker", "run"] and "tar" in c.argv)
    copy = first(world, "cat > ~/src/Pseudolife-MCP/data/move-")
    restore = first(world, "bash ops/restore.sh")
    compare = first(world, "UNION ALL")
    env = first(world, "umask 077")
    update = first(world, "bash ops/update.sh")
    unmark = first(world, "rm -f /data/move.json")
    moved = index(world, lambda c: c.argv[:2] == ["docker", "cp"] and c.argv[-1].endswith(":/data/moved.json"))
    connect = index(world, lambda c: "connect" in c.argv)
    assert -1 not in (stop, dump, fence, state, copy, restore, compare, env, update, unmark, moved, connect)
    assert stop < dump < fence < state < copy < restore < compare < env < update < unmark < moved < connect
    # The source is stopped (never removed), fenced, and marked moved.
    assert not world.source_running and world.fenced and world.moved
    assert not any(c.argv[:2] == ["docker", "rm"] or " down" in c.text for c in world.calls)
    # The target restore names the dump and never starts the daemon itself.
    restore_cmd = world.calls[restore].remote
    assert "--apply --no-start" in restore_cmd and "--backup-file" in restore_cmd
    assert "--state-archive" in restore_cmd
    # The marker was removed after the bank check and the target runs the source's bank.
    assert world.marker is None and world.target_running
    assert not world.overlap
    assert world.calls[connect].argv[-3:] == [TARGET_URL, "--yes", "--json"]


def test_the_report_lists_rollback_principals_leases_mail_and_leftovers(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)["report"]
    rollback = "\n".join(report["rollback"])
    assert f"ssh {TARGET} docker stop pseudolife-mcp-daemon" in rollback
    assert "ALLOW_CONNECTIONS true" in rollback
    assert "--volumes-from pseudolife-mcp-daemon" in rollback and "/data/moved.json" in rollback
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
    leftovers = " ".join(item["what"] + " " + item["command"] for item in report["leftovers"])
    for needle in ("backup", "extractor", "tunnel", "autostart", "expose off", "update --unschedule"):
        assert needle in leftovers, needle
    replaced = " ".join(report["replaced_on_target"])
    assert "config.yaml" in replaced and "last-backup.json" in replaced
    assert any("installer" in note for note in report["notes"])


def test_text_output_names_the_rollback_and_the_next_steps(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path) == 0
    out = capsys.readouterr().out
    assert "ALLOW_CONNECTIONS true" in out
    assert f"pseudolife-mcp connect {TARGET_URL}" in out
    assert "lease break full-suite" in out


def test_no_token_or_password_ever_appears_in_a_command_line(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path, as_json=True) == 0
    for call in world.calls:
        for secret in SECRETS:
            assert secret not in call.text, (secret, call)
    # The carried identities did reach the target, over stdin.
    env_write = next(c for c in world.calls if c.remote and c.remote.startswith("umask 077"))
    written = env_write.stdin.decode()
    assert f"PSEUDOLIFE_MCP_TOKEN={SOURCE_TOKEN}" in written
    assert f"PSEUDOLIFE_MCP_TOKENS={LAPTOP_TOKEN}:laptop,{CI_TOKEN}:ci" in written
    assert "PSEUDOLIFE_MCP_TIER_MAP=laptop:core,ci:minimal" in written
    assert TARGET_OLD_TOKEN not in written
    assert f"POSTGRES_PASSWORD={TARGET_PG_PASSWORD}" in written
    assert "\r" not in written
    # ...and the JSON report carries none of them either.
    out = capsys.readouterr().out
    for secret in SECRETS:
        assert secret not in out


def test_every_remote_command_is_batch_mode_ssh(tmp_path):
    world = World()
    assert run_move(world, tmp_path) == 0
    for call in world.calls:
        if call.argv[0] == "ssh":
            assert call.argv[:4] == ["ssh", "-o", "BatchMode=yes", TARGET] and len(call.argv) == 5, call


def test_files_are_hash_checked_on_the_target(tmp_path):
    world = World()
    assert run_move(world, tmp_path) == 0
    copies = [c for c in world.calls if c.remote and c.remote.startswith("cat > ")]
    assert len(copies) == 3
    for copy in copies:
        path = copy.remote[len("cat > "):]
        assert any(c.remote and c.remote.startswith(f"sha256sum {path}") for c in world.calls), path


def test_the_manifest_carries_per_table_row_counts(tmp_path):
    world = World()
    assert run_move(world, tmp_path) == 0
    manifest_copy = next(c for c in world.calls if c.remote and c.remote.endswith("manifest.json"))
    manifest = json.loads(manifest_copy.stdin)
    assert manifest["tables"] == DUMP_COUNTS
    assert manifest["source_bank"] == SOURCE_BANK


# ── preflight: refused, nothing changed ──────────────────────────────────────

@pytest.mark.parametrize("world_kw, needle", [
    ({"source_healthy": False}, "/health"),
    ({"source_docker": False}, "Docker"),
    ({"source_bank": None}, "fingerprint"),
    ({"ssh_ok": False}, "ssh"),
    ({"checkout": False}, "install"),
    ({"target_healthy": False}, "target daemon"),
    ({"target_schema": 40}, "schema"),
    ({"target_bank": SOURCE_BANK}, "same bank"),
    ({"target_empty": False}, "not empty"),
])
def test_preflight_refusals_exit_4_and_change_nothing(tmp_path, capsys, world_kw, needle):
    world = World(**world_kw)
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 4, report
    assert needle.lower() in report["error"].lower(), report["error"]
    assert mutating_calls(world) == []
    assert world.source_running and not world.fenced
    assert not (tmp_path / "home" / "moves").exists()


def test_a_null_target_fingerprint_counts_as_different(tmp_path):
    world = World(target_bank=None)
    assert run_move(world, tmp_path) == 0


def test_a_non_empty_target_with_a_marker_points_at_resume(tmp_path, capsys):
    world = World(target_empty=False, marker={"move_id": "20261001-000000-00000000", "source_bank": SOURCE_BANK})
    assert run_move(world, tmp_path, as_json=True) == 4
    assert "--resume" in json.loads(capsys.readouterr().out)["error"]


# ── dry run, confirmation ────────────────────────────────────────────────────

def test_dry_run_prints_the_plan_and_runs_only_read_only_commands(tmp_path, capsys):
    world = World(exposed=False)
    code = run_move(world, tmp_path, dry_run=True, yes=False, schedule={"where": "x", "time": "03:30",
                                                                        "url": LOCAL})
    out = capsys.readouterr().out
    assert code == 0
    assert mutating_calls(world) == [], mutating_calls(world)
    assert "expose tailscale" in out          # the target is not exposed: the plan says so
    assert "pg_dump" in out or "final backup" in out.lower()
    assert "10.0 MiB" in out                   # the database size
    assert "unattended update" in out.lower()
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
    stop = first(world, "docker stop pseudolife-mcp-daemon")
    assert -1 < expose < stop


def test_an_expose_refusal_stops_before_the_source_is_touched(tmp_path, capsys):
    world = World(exposed=False, expose_exit=4)
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 4 and "operator" in report["error"]
    assert world.source_running and not any(c.text.startswith("docker stop") for c in world.calls)


def test_an_unreachable_target_url_stops_before_the_source_is_touched(tmp_path):
    world = World(fail=(f"GET {TARGET_URL}/health",))
    assert run_move(world, tmp_path) == 4
    assert world.source_running and not any(c.text.startswith("docker stop") for c in world.calls)


def test_an_explicit_target_url_skips_the_exposure_read(tmp_path):
    world = World()
    assert run_move(world, tmp_path, target_url=TARGET_URL) == 0
    assert not any(c.remote and "expose" in c.remote for c in world.calls)


# ── failures in steps 5-9 roll back, in order ────────────────────────────────

ROLLBACK_CASES = {
    "pg_dump": ("pg_dump", {"target_started": False, "env": False, "fenced": False, "moved": False}),
    "fence": ("ALLOW_CONNECTIONS false", {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "state_archive": ("czf -", {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "copy": ("cat > ~/src", {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "restore": ("bash ops/restore.sh", {"target_started": False, "env": False, "fenced": True, "moved": False}),
    "env_write": ("umask 077", {"target_started": False, "env": True, "fenced": True, "moved": False}),
    "update": ("bash ops/update.sh", {"target_started": True, "env": True, "fenced": True, "moved": False}),
    "unmark": ("rm -f /data/move.json", {"target_started": True, "env": True, "fenced": True, "moved": False}),
    "moved_marker": (":/data/moved.json", {"target_started": True, "env": True, "fenced": True, "moved": True}),
}


@pytest.mark.parametrize("case", sorted(ROLLBACK_CASES))
def test_a_failure_rolls_back_in_order_and_restarts_the_source(tmp_path, capsys, case):
    needle, expect = ROLLBACK_CASES[case]
    world = World(fail=(needle,))
    code = run_move(world, tmp_path, as_json=True, schedule={"where": "unit", "time": "03:30", "url": LOCAL})
    report = json.loads(capsys.readouterr().out)
    assert code == 1, report
    failed_at = first(world, needle)
    after = world.calls[failed_at + 1:]
    texts = [c.text for c in after]

    def at(pred):
        return next((i for i, c in enumerate(after) if pred(c)), None)

    stop_target = at(lambda c: c.remote == "docker stop pseudolife-mcp-daemon")
    env_restore = at(lambda c: c.remote is not None and c.remote.startswith("cp -p")
                     and ".pre-move-" in c.remote.split()[2])
    lift = at(lambda c: "ALLOW_CONNECTIONS true" in c.text)
    unmoved = at(lambda c: c.argv[:2] == ["docker", "run"] and "rm" in c.argv and "/data/moved.json" in c.argv)
    resume = at(lambda c: c.argv[:2] == ["systemctl", "--user"] and "enable" in c.argv)
    start = at(lambda c: c.argv[:2] == ["docker", "start"])
    assert (stop_target is not None) == expect["target_started"], texts
    assert (env_restore is not None) == expect["env"], texts
    assert (lift is not None) == expect["fenced"], texts
    assert (unmoved is not None) == expect["moved"], texts
    assert resume is not None and start is not None, texts
    order = [i for i in (stop_target, env_restore, lift, unmoved, resume, start) if i is not None]
    assert order == sorted(order), texts
    assert world.source_running and not world.fenced and not world.moved
    assert not world.overlap
    assert report["rollback"], report


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


def test_a_target_reporting_another_bank_after_start_rolls_back(tmp_path):
    world = World(bank_after_start=None)
    assert run_move(world, tmp_path) == 1
    assert world.source_running and not world.target_running
    stop_target = index(world, lambda c: c.remote == "docker stop pseudolife-mcp-daemon")
    start_source = index(world, lambda c: c.argv[:2] == ["docker", "start"])
    assert -1 < stop_target < start_source
    # The marker stays, so --resume may take this half-restored bank over.
    assert world.marker is not None


def test_a_state_archive_holding_a_marker_is_refused(tmp_path):
    world = World(tar_members={"./config.yaml": b"x", "./moved.json": b"{}"})
    assert run_move(world, tmp_path) == 1
    assert world.source_running


def test_when_the_target_cannot_be_stopped_the_source_stays_down(tmp_path, capsys):
    world = World(fail=("bash ops/update.sh",), stop_target_fails=True)
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert not any(c.argv[:2] == ["docker", "start"] for c in world.calls)
    assert not world.source_running
    assert "incomplete" in report["error"].lower()


def test_the_schedule_is_paused_before_the_stop_and_stays_paused_after_a_move(tmp_path):
    world = World()
    assert run_move(world, tmp_path, schedule={"where": "unit", "time": "03:30", "url": LOCAL}) == 0
    pause = index(world, lambda c: c.argv[:2] == ["systemctl", "--user"] and "disable" in c.argv)
    stop = first(world, "docker stop pseudolife-mcp-daemon")
    assert -1 < pause < stop
    assert index(world, lambda c: c.argv[:2] == ["systemctl", "--user"] and "enable" in c.argv) == -1


def test_the_windows_schedule_is_paused_with_schtasks(tmp_path):
    world = World()
    assert run_move(world, tmp_path, platform="win32",
                    schedule={"where": "task", "time": "03:30", "url": LOCAL}) == 0
    assert any(c.argv[:2] == ["schtasks", "/Change"] and "/DISABLE" in c.argv for c in world.calls)


# ── after the bank moved ─────────────────────────────────────────────────────

def test_a_failed_repoint_exits_5_without_rolling_back(tmp_path, capsys):
    world = World(connect_exit=4)
    code = run_move(world, tmp_path, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 5
    assert f"pseudolife-mcp connect {TARGET_URL}" in report["error"]
    assert not world.source_running and world.fenced and world.moved
    assert not any(c.argv[:2] == ["docker", "start"] for c in world.calls)


def test_no_registration_to_repoint_is_not_a_failure(tmp_path):
    world = World(connect_exit=3)
    assert run_move(world, tmp_path) == 0


def test_no_keep_tokens_writes_no_env_and_lists_who_to_reinvite(tmp_path, capsys):
    world = World()
    assert run_move(world, tmp_path, keep_tokens=False, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)["report"]
    assert not any(c.remote and c.remote.startswith("umask 077") for c in world.calls)
    reinvite = {p["principal"] for p in report["reinvite"]}
    assert reinvite == {"default", "laptop", "ci"}
    assert all("pseudolife-mcp invite" in p["line"] for p in report["reinvite"])


# ── --resume ─────────────────────────────────────────────────────────────────

def _record(tmp_path, move_id, bank=SOURCE_BANK):
    directory = tmp_path / "home" / "moves" / move_id
    directory.mkdir(parents=True)
    (directory / "record.json").write_text(json.dumps({"move_id": move_id, "target": TARGET,
                                                       "source_bank": bank, "state": "rolled_back"}))


def test_resume_is_refused_without_a_marker(tmp_path, capsys):
    world = World(target_empty=False)
    assert run_move(world, tmp_path, resume=True, as_json=True) == 4
    assert "marker" in json.loads(capsys.readouterr().out)["error"]
    assert mutating_calls(world) == []


def test_resume_is_refused_over_another_moves_marker(tmp_path, capsys):
    _record(tmp_path, "20261001-000000-11111111")
    world = World(target_empty=False, marker={"move_id": "20261001-000000-22222222", "source_bank": SOURCE_BANK})
    assert run_move(world, tmp_path, resume=True, as_json=True) == 4
    assert mutating_calls(world) == []


def test_resume_is_refused_over_a_marker_from_another_source(tmp_path):
    _record(tmp_path, "20261001-000000-11111111")
    world = World(target_empty=False, marker={"move_id": "20261001-000000-11111111", "source_bank": "c" * 16})
    assert run_move(world, tmp_path, resume=True) == 4


def test_resume_takes_over_this_moves_half_restored_bank(tmp_path, capsys):
    move_id = "20261001-000000-11111111"
    _record(tmp_path, move_id)
    world = World(target_empty=False, target_healthy=False,
                  marker={"move_id": move_id, "source_bank": SOURCE_BANK})
    world.target_running = False
    code = run_move(world, tmp_path, resume=True, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert report["move_id"] == move_id
    assert first(world, f"data/move-{move_id}") > -1


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


def test_an_unreadable_target_env_is_never_replaced_or_removed(tmp_path):
    """It holds the target's Postgres password and volume names: when it
    exists but cannot be read, the move stops (and rolls back) without
    writing the carried keys over it, and the rollback does not delete it."""
    world = World(fail=(f"cat {CHECKOUT}/ops/.env",))
    assert run_move(world, tmp_path) == 1
    assert not any(c.remote and c.remote.startswith("umask 077") for c in world.calls)
    assert not any(c.remote and c.remote.startswith("rm -f") and "ops/.env" in c.remote for c in world.calls)
    assert world.source_running and not world.fenced
