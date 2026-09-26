"""Memory-policy bench: does the session-start memory policy change what an
agent actually does with memory?

Runs real headless clients (``claude -p``; ``codex exec`` for a plumbing
check) against a DISPOSABLE daemon and bank, one scenario per policy rule
(``evals/memory_policy_scenarios.py``), once per arm, and scores each run
from the daemon's records alone. Arms are values of the daemon's
``memory_policy.variant`` knob; ``label@suffix`` makes a second copy of a
variant (an A/A arm) whose spread is the noise floor.

Isolation (every run):

* a fresh ``plbench_`` database cloned from a seeded template on the bench
  Postgres, guarded by name AND by asking the server
  (``assert_disposable_database``); the live bank (``pseudolife_memory``)
  and the live daemon ports are refused outright;
* a fresh daemon on a free loopback port, recording every MCP tool call and
  hook response to a ledger (``evals/memory_policy_daemon.py``);
* the client in a throwaway config home (``CLAUDE_CONFIG_DIR`` / a temp
  ``CODEX_HOME``) and a temp project OUTSIDE the user's home directory: a
  project under the home directory inherits ``~/.claude/CLAUDE.md`` as an
  ancestor "project" file even with a throwaway config dir (seen
  2026-09-25). Nothing under ``~/.claude``, ``~/.claude.json`` or
  ``~/.codex`` is written. Claude authenticates with the current access
  token passed in the environment only (no refresh token, so the child can
  never rotate the user's login);
* the plugin's real hook scripts, wired for SessionStart (briefing and the
  separate memory-policy output) and SessionEnd only. The per-turn
  reminder and the coordination hooks are policy surfaces of their own and
  are held off, as is coordination in the daemon.

Validity (every run): a capture proxy records each model request; the check
proves the arm's policy text is present and is the ONLY memory-policy text
in the model's context. Constant across arms, and documented rather than
removed: the MCP server instructions, the memory tool names and
descriptions, the episode-handle line and the briefing.

Usage::

    python -m evals.memory_policy_bench run --tag smoke-20260925 \\
        --arms none,full_separate_hook --replicates 1
    python -m evals.memory_policy_bench report evals/results/memory-policy-bench-<tag>.json
    python -m evals.memory_policy_bench estimate evals/results/memory-policy-bench-<tag>.json \\
        --variants 4 --clients 2 --replicates 5

Every run writes ``evals/results/memory-policy-bench-<tag>.json`` and
refuses to overwrite one. Transcripts, captures and ledgers stay in the
work directory (default ``C:\\plbench`` / ``/tmp/plbench``), never in the
repository: they carry the machine's paths.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import embedder_stamp  # noqa: E402
from evals import memory_policy_scenarios as fx  # noqa: E402
from evals.memory_policy_daemon import (  # noqa: E402
    BENCH_DB_PREFIX, FORBIDDEN_PORTS, BenchSafetyError, check_database, check_port)

BENCH_VERSION = 1
RESULTS_DIR = ROOT / "evals" / "results"
ARTIFACT_PREFIX = "memory-policy-bench-"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "medium"
# Score = compliance + task success - COST_LAMBDA * (cost in 100k BITE).
# BITE = billable input-token equivalents at Anthropic list-price ratios
# (input 1, 5-minute cache write 1.25, 1-hour cache write 2, cache read
# 0.1, output 5). 0.1 per 100k BITE: an arm that spends 100k more BITE per
# run (about $0.30 at Sonnet list prices) must buy a tenth of a
# compliance-or-success point for it. A preference, not a measurement;
# pass --cost-lambda to change it and the report records the value.
COST_LAMBDA = 0.1
BOOTSTRAP_SAMPLES = 10_000
MEMORY_TOOL_PREFIX = "mcp__pseudolife-memory__"
MEMORY_READS = ("memory_search", "memory_fact_get", "memory_recall", "memory_get",
                "memory_lesson_search", "memory_world_search")
_HOOK_KEY = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Metrics compared between arms. Direction: +1 higher is better, -1 lower.
METRICS = {
    "score": +1, "compliance": +1, "task_success": +1,
    "used_ids_precision": +1, "used_ids_recall": +1,
    "searched_before_acting": +1, "lesson_search_used": +1,
    "outcome_logged": +1, "session_titled": +1,
    "cost_bite": -1, "input_tokens": -1, "output_tokens": -1,
    "tool_calls": -1, "memory_tool_calls": 0, "wall_s": -1,
}
# A challenger may not regress any of these beyond the A/A noise.
GUARDED = ("compliance", "task_success", "used_ids_precision", "cost_bite")


# ── arms and plan ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Arm:
    label: str
    variant: str


def parse_arms(spec: str) -> list[Arm]:
    from pseudolife_memory.utils.config import MEMORY_POLICY_VARIANTS
    arms = []
    for label in (s.strip() for s in spec.split(",") if s.strip()):
        variant = label.split("@", 1)[0]
        if variant not in MEMORY_POLICY_VARIANTS:
            raise SystemExit(f"unknown variant {variant!r} in arm {label!r}")
        arms.append(Arm(label, variant))
    if len({a.label for a in arms}) != len(arms):
        raise SystemExit("arm labels must be unique (use label@suffix for an A/A copy)")
    return arms


def plan(arms: list[Arm], scenarios: list[str], replicates: int, seed: int,
         rotate: bool = False) -> list[dict]:
    """Every (arm, scenario, replicate). Arms interleave within each
    (replicate, scenario) block in a seeded order, so drift over the run's
    hours cannot line up with one arm. ``rotate`` gives each block ONE arm,
    cycling through them: a smoke test that touches every arm and scenario
    once, with nothing to pair."""
    rng = random.Random(seed)
    out = []
    for rep in range(replicates):
        for i, sid in enumerate(scenarios):
            if rotate:
                arm = arms[(i + rep) % len(arms)]
                out.append({"arm": arm.label, "variant": arm.variant, "scenario": sid,
                            "replicate": rep})
                continue
            order = list(arms)
            rng.shuffle(order)
            out.extend({"arm": a.label, "variant": a.variant, "scenario": sid,
                        "replicate": rep} for a in order)
    return out


def run_id(tag: str, item: dict) -> str:
    raw = f"{tag}-{item['arm']}-{item['scenario']}-r{item['replicate']}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)


# ── safety guards ──────────────────────────────────────────────────────────

def default_work_root() -> Path:
    if os.name == "nt":
        return Path((os.environ.get("SystemDrive") or "C:") + "\\") / "plbench"
    return Path("/tmp/plbench")


_INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md")


def check_work_root(root: Path) -> Path:
    """The work root must sit outside the home directory and the repository,
    and no ancestor may carry agent instruction files or config dirs."""
    root = root.resolve()
    home = Path.home().resolve()
    for forbidden, what in ((home, "the home directory"), (ROOT.resolve(), "the repository")):
        if root == forbidden or forbidden in root.parents:
            raise BenchSafetyError(f"work root {root} is inside {what}; an agent started "
                                   f"there inherits its instruction files")
    for directory in (root, *root.parents):
        for name in _INSTRUCTION_FILES:
            if (directory / name).exists():
                raise BenchSafetyError(f"{directory / name} would reach every bench agent")
        for name in (".claude", ".codex"):
            if (directory / name).is_dir():
                raise BenchSafetyError(f"{directory / name} would reach every bench agent")
    return root


def admin_url() -> str:
    explicit = os.environ.get("PSEUDOLIFE_BENCH_ADMIN_URL")
    if explicit:
        return explicit
    from tests.pg_defaults import default_admin_url  # reads ops/.env; never printed
    return default_admin_url()


def db_url(admin: str, name: str) -> str:
    parts = urllib.parse.urlsplit(admin)
    return urllib.parse.urlunsplit(parts._replace(path="/" + name))


def _admin_connect(admin: str):
    import psycopg
    return psycopg.connect(admin, connect_timeout=10, autocommit=True)


def create_db(admin: str, name: str, template: str | None = None) -> None:
    from psycopg import sql
    check_database(db_url(admin, name))
    with _admin_connect(admin) as conn:
        stmt = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if template:
            check_database(db_url(admin, template))
            stmt = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template))
        conn.execute(stmt)


def drop_db(admin: str, name: str) -> None:
    """Drop a ``plbench_`` database by name. The statement names the
    database it removes, so the name check is the guard; FORCE ends its
    sessions in the same statement (no separate reap)."""
    from psycopg import sql
    check_database(db_url(admin, name))
    with _admin_connect(admin) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(name)))


def db_exists(admin: str, name: str) -> bool:
    with _admin_connect(admin) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                            (name,)).fetchone() is not None


def free_port() -> int:
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return check_port(port)
    raise BenchSafetyError("no free port")


_SECRET_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|CREDENTIAL)")


def production_database() -> str:
    """The live bank's database name, as the guard in
    ``pseudolife_memory.storage.schema`` resolves it in THIS process (the
    bench's own environment carries no bench DSN)."""
    from pseudolife_memory.storage.schema import (
        DEFAULT_PRODUCTION_DATABASE, PRODUCTION_DATABASE_ENV, dsn_database_name)
    recorded = os.environ.get(PRODUCTION_DATABASE_ENV)
    if recorded:
        return recorded
    live = os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
    if live:
        name = dsn_database_name(live)
        if not name:
            raise BenchSafetyError("PSEUDOLIFE_MCP_DATABASE_URL names no database; unset it")
        return name
    return DEFAULT_PRODUCTION_DATABASE


def scrubbed_env(extra: dict | None = None) -> dict:
    """The parent environment minus every variable that could point a child
    at the live bank, the live daemon, a real client config or a key.

    A child's PSEUDOLIFE_MCP_DATABASE_URL names its bench database, which
    the production-bank guard would then take for the live bank; the live
    bank's name is recorded for it instead, as tests/conftest.py does."""
    from pseudolife_memory.storage.schema import PRODUCTION_DATABASE_ENV
    drop = ("PSEUDOLIFE", "ANTHROPIC", "CLAUDE", "CODEX", "OPENAI", "PLUGIN_ROOT",
            PRODUCTION_DATABASE_ENV, "PG", "AWS_", "AZURE_", "GH_", "GITHUB_", "HF_TOKEN",
            "HUGGING_FACE", "GOOGLE_", "GEMINI_")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(drop) and not _SECRET_NAME.search(k)}
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "CUDA_VISIBLE_DEVICES": "-1", "PYTHONUTF8": "1",
                "PYTHONPATH": str(ROOT), PRODUCTION_DATABASE_ENV: production_database()})
    env.update(extra or {})
    return env


def claude_access_token() -> str:
    """The user's current Claude Code access token, for the child's
    environment only (never written, never logged). A throwaway config dir
    has no credentials; the access token alone cannot refresh, so the child
    cannot rotate the user's login. ``CLAUDE_CODE_OAUTH_TOKEN`` in the
    bench's own environment (``claude setup-token``) wins."""
    explicit = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if explicit:
        return explicit
    path = Path.home() / ".claude" / ".credentials.json"
    data = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    if data.get("expiresAt", 0) / 1000 < time.time() + 1_200:
        raise BenchSafetyError("the Claude access token expires within 20 minutes; open "
                               "any Claude Code session to refresh it, then rerun")
    return data["accessToken"]


def free_commit_gb() -> float | None:
    """Free commit charge (Windows) or available memory (elsewhere), in GB."""
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullAvailPageFile / 2**30
        return None
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30
    except (ValueError, OSError, AttributeError):
        return None


def wait_for_headroom(min_gb: float, log, poll_s: float = 30.0, max_wait_s: float = 7_200.0) -> None:
    """Hold before a daemon start until ``min_gb`` of commit is free. One run
    peaked at 3.9 GB of commit: daemon 3.13 GB (CPU Qwen3-Embedding in bf16,
    bank, Python), ``claude`` 0.46 GB, shim 0.28 GB (sampled over a full
    run of the 2026-09-25 sanity check, Windows). The default 8 GB leaves
    about the same again for everything else; this machine also runs full
    test suites and GPU servers, and starting into a full commit limit kills
    other sessions' processes (os error 1455)."""
    waited = 0.0
    while True:
        free = free_commit_gb()
        if free is None or free >= min_gb:
            return
        if waited >= max_wait_s:
            raise RuntimeError(f"only {free:.1f} GB commit free after {waited / 60:.0f} min")
        if waited % 300 < poll_s:
            log(f"holding: {free:.1f} GB commit free, need {min_gb:.1f}")
        time.sleep(poll_s)
        waited += poll_s


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


# ── template bank ──────────────────────────────────────────────────────────

def fixture_digest() -> str:
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
    from pseudolife_memory.utils.config import EmbeddingConfig
    h = hashlib.sha256()
    for name in ("memory_policy_scenarios.py", "memory_policy_daemon.py"):
        h.update((ROOT / "evals" / name).read_bytes().replace(b"\r\n", b"\n"))
    h.update(f"{SCHEMA_META_VERSION}|{EmbeddingConfig().model_name}".encode())
    return h.hexdigest()[:12]


DAEMON_CONFIG = """\
embedding:
  device: cpu
memory:
  dream:
    enabled: false
  deep_dream:
    auto_tick: false
coordination:
  enabled: false
memory_policy:
  variant: {variant}
"""


_LIKE_PREFIX = BENCH_DB_PREFIX.replace("_", "\\_")   # `_` is a LIKE wildcard


def bench_databases(admin: str, *, templates: bool) -> list[tuple[str, int]]:
    """(name, connected backends) of every bench database of one kind."""
    kind = f"{_LIKE_PREFIX}tpl\\_%" if templates else f"{_LIKE_PREFIX}%"
    with _admin_connect(admin) as conn:
        rows = conn.execute(
            "SELECT d.datname, (SELECT COUNT(*) FROM pg_stat_activity a "
            "WHERE a.datname = d.datname) FROM pg_database d "
            "WHERE d.datname LIKE %s ESCAPE '\\'", (kind,)).fetchall()
    tpl = f"{BENCH_DB_PREFIX}tpl_"
    return [(n, c) for n, c in rows if templates or not n.startswith(tpl)]


def ensure_template(admin: str, work: Path, log) -> tuple[str, dict]:
    """The seeded template for this fixture version, shared by every tag
    under the work root (a bench started later must not rebuild, let alone
    drop, the template a running bench clones from)."""
    digest = fixture_digest()
    name = f"{BENCH_DB_PREFIX}tpl_{digest}"
    manifest_path = work.parent / f"template-{digest}.json"
    legacy = work / f"template-{digest}.json"
    if not manifest_path.exists() and legacy.exists():
        shutil.copyfile(legacy, manifest_path)
    if manifest_path.exists() and db_exists(admin, name):
        return name, json.loads(manifest_path.read_text(encoding="utf-8"))
    for old, backends in bench_databases(admin, templates=True):
        # Earlier fixture versions nobody is using, and a half-built copy of
        # this one (no manifest).
        if backends == 0:
            drop_db(admin, old)
    if db_exists(admin, name):
        raise RuntimeError(f"{name} exists without a manifest and is in use; wait for "
                           f"its user, then rerun")
    create_db(admin, name)
    data_dir = work / f"template-data-{digest}"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text(DAEMON_CONFIG.format(variant="compact"),
                                          encoding="utf-8")
    env = scrubbed_env({"PSEUDOLIFE_MCP_DATABASE_URL": db_url(admin, name),
                        "PSEUDOLIFE_MCP_DATA_DIR": str(data_dir),
                        "PSEUDOLIFE_WRITER_ID": "bench-seed"})
    log(f"seeding template {name} (CPU embedder; a minute or two)")
    started = time.time()
    proc = subprocess.run([sys.executable, "-m", "evals.memory_policy_daemon", "seed",
                           "--manifest", str(manifest_path)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=1_800)
    if proc.returncode != 0:
        drop_db(admin, name)
        raise RuntimeError(f"template seeding failed:\n{proc.stderr[-3000:]}")
    log(f"template seeded in {time.time() - started:.0f}s")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    import psycopg
    with psycopg.connect(db_url(admin, name), connect_timeout=10) as conn:
        manifest["max_ids"] = {
            t: conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0]
            for t in ("entries", "outcome_signals", "retrieval_events")}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return name, manifest


# ── disposable daemon ──────────────────────────────────────────────────────

class Daemon:
    def __init__(self, run_dir: Path, dsn: str, variant: str):
        self.dir = run_dir
        self.dsn = dsn
        self.variant = variant
        self.port = free_port()
        self.token = secrets.token_urlsafe(24)
        self.ledger = run_dir / "ledger.jsonl"
        self.proc: subprocess.Popen | None = None
        self.embedder: dict | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 300.0) -> None:
        data = self.dir / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "config.yaml").write_text(DAEMON_CONFIG.format(variant=self.variant),
                                          encoding="utf-8")
        env = scrubbed_env({
            "PSEUDOLIFE_MCP_HOST": "127.0.0.1", "PSEUDOLIFE_MCP_PORT": str(self.port),
            "PSEUDOLIFE_MCP_DATABASE_URL": self.dsn, "PSEUDOLIFE_MCP_DATA_DIR": str(data),
            "PSEUDOLIFE_MCP_TOKEN": self.token, "PSEUDOLIFE_WRITER_ID": "bench-daemon"})
        self.log_file = (self.dir / "daemon.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "evals.memory_policy_daemon", "serve",
             "--ledger", str(self.ledger)],
            cwd=ROOT, env=env, stdout=self.log_file, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"bench daemon exited ({self.proc.returncode}); "
                                   f"see {self.dir / 'daemon.log'}")
            try:
                health = self.get("/health", auth=False)
                if health.get("storage") == "postgres":
                    break
            except OSError:
                pass
            time.sleep(1.0)
        else:
            raise RuntimeError("bench daemon never became healthy")
        # Load the embedder and bank before the client starts: the session
        # start hook has fifteen seconds, and a cold CPU load can take longer.
        self.get("/api/briefing", timeout=timeout)
        # It embeds every query the agent sends; /health describes it once built.
        self.embedder = self.get("/health", auth=False).get("embedder")

    def get(self, path: str, *, auth: bool = True, timeout: float = 30.0) -> dict:
        req = urllib.request.Request(self.url + path)
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip().startswith(("{", "[")) else {"text": body}

    def stop(self) -> None:
        if self.proc is not None:
            kill_tree(self.proc)

    def close_log(self) -> None:
        if getattr(self, "log_file", None) is not None:
            self.log_file.close()
            self.log_file = None


# ── capture proxy (the model's context, for the validity check) ────────────

def header_value(value: str) -> str:
    """A response header value with CR and LF removed (no header splitting)."""
    return value.replace("\r", "").replace("\n", "")


def host_is(url: str | None, domain: str) -> bool:
    """Whether ``url``'s parsed host is ``domain`` or one of its subdomains
    (a substring test would accept https://evil.example/?sqlite.org)."""
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    return host == domain or host.endswith("." + domain)


class CaptureProxy:
    """Forwards the client's API traffic to api.anthropic.com and writes
    each request BODY (never headers) to ``capture/``."""

    UPSTREAM = "api.anthropic.com"
    _HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
            "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
    # Response headers passed back to the client, by these fixed names: what
    # a (possibly compressed) streaming Messages response and its retry
    # logic need. Everything else, rate-limit detail included, is dropped,
    # and values lose any CR/LF, so no upstream value can split a header.
    _FORWARD = ("content-type", "content-encoding", "cache-control", "request-id",
                "retry-after", "x-should-retry")

    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _forward(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                with proxy.lock:
                    proxy.count += 1
                    n = proxy.count
                (proxy.out / f"{n:04d}.json").write_text(json.dumps(
                    {"t": time.time(), "method": self.command, "path": self.path,
                     "body": body.decode("utf-8", "replace")}), encoding="utf-8")
                conn = http.client.HTTPSConnection(proxy.UPSTREAM, timeout=900)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in proxy._HOP}
                headers["Host"] = proxy.UPSTREAM
                conn.request(self.command, self.path, body=body or None, headers=headers)
                upstream = conn.getresponse()
                self.send_response(upstream.status)
                for name in proxy._FORWARD:
                    value = upstream.getheader(name)
                    if value is not None:
                        self.send_header(name, header_value(value))
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = upstream.read1(65_536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                conn.close()

            do_GET = do_POST = do_PUT = do_DELETE = _forward

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass   # a client closing its keep-alive socket is not news

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ── project and clients ────────────────────────────────────────────────────

def write_project(project: Path, canary: str | None) -> dict[str, str]:
    """Write the fixture project; return {path: sha256} of what was written."""
    digests = {}
    for rel, content in fx.PROJECT_FILES.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        digests[rel] = hashlib.sha256(content.encode()).hexdigest()
    return digests


def bench_plugin(dest: Path) -> Path:
    """A copy of the repository's plugin whose hooks.json wires only the
    SessionStart memory handlers (briefing + separate memory-policy output)
    and SessionEnd. The scripts are byte-identical copies."""
    shutil.copytree(ROOT / "plugin", dest)
    manifest = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))
    keep = {"SessionStart": ("hooks/session-start.sh\"", "hooks/session-start.sh\" memory-policy"),
            "SessionEnd": ("hooks/session-end.sh\"",)}
    hooks = {}
    for event, suffixes in keep.items():
        groups = [g for g in manifest["hooks"][event]
                  if any(h["command"].endswith(sfx) for h in g["hooks"] for sfx in suffixes)]
        if len(groups) != len(suffixes):
            raise RuntimeError(f"plugin hooks.json no longer has the {event} handlers the bench wires")
        hooks[event] = groups
    manifest["hooks"] = hooks
    (dest / "hooks/hooks.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dest


def shim_server(daemon: Daemon, run_dir: Path, writer: str) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": ["-m", "pseudolife_memory.cli"],
            "env": {"PSEUDOLIFE_MCP_DAEMON_URL": daemon.url, "PSEUDOLIFE_MCP_TOKEN": daemon.token,
                    "PSEUDOLIFE_MCP_NO_SPAWN": "1", "PSEUDOLIFE_WRITER_ID": writer,
                    "PSEUDOLIFE_AGENT_COORDINATION": "0", "PSEUDOLIFE_AGENT_WAKE": "0",
                    "PSEUDOLIFE_AGENT_STATE_DIR": str(run_dir / "agents"),
                    "PSEUDOLIFE_DIGEST_DIR": str(run_dir / "digests"),
                    "PYTHONPATH": str(ROOT), "HF_HUB_OFFLINE": "1", "PYTHONUTF8": "1"}}


def run_claude(run_dir: Path, project: Path, prompt: str, daemon: Daemon, *, model: str,
               effort: str, timeout: float, budget_usd: float, tool_search: str = "true") -> dict:
    config_dir = run_dir / "claude-config"
    home = run_dir / "home"
    for d in (config_dir, home, run_dir / "agents", run_dir / "digests"):
        d.mkdir(parents=True, exist_ok=True)
    plugin = bench_plugin(run_dir / "plugin")
    mcp = {"mcpServers": {"pseudolife-memory": shim_server(daemon, run_dir, "bench-claude")}}
    mcp_path = run_dir / "mcp.json"
    mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
    proxy = CaptureProxy(run_dir / "capture")
    env = scrubbed_env({
        "CLAUDE_CONFIG_DIR": str(config_dir), "HOME": str(home), "USERPROFILE": str(home),
        "CLAUDE_CODE_OAUTH_TOKEN": claude_access_token(),
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{proxy.port}",
        # Claude Code's background helper model defaults to Haiku; pin it.
        "ANTHROPIC_SMALL_FAST_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "DISABLE_AUTOUPDATER": "1",
        # Claude Code's own auto memory is a file-based memory policy of its
        # own (~13k chars of system prompt); held off like the other
        # policy surfaces. The settings flag below does the same.
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        # Defer MCP tool schemas behind ToolSearch, as in a session with many
        # MCP servers; alone, the memory server's 46 tools would load whole.
        "ENABLE_TOOL_SEARCH": tool_search,
        "PSEUDOLIFE_MCP_DAEMON_URL": daemon.url, "PSEUDOLIFE_MCP_TOKEN": daemon.token,
        "PSEUDOLIFE_DIGEST_DIR": str(run_dir / "digests")})
    cli = shutil.which("claude") or "claude"
    cmd = [cli, "-p", "--model", model, "--effort", effort,
           "--output-format", "stream-json", "--verbose", "--include-hook-events",
           "--strict-mcp-config", "--mcp-config", str(mcp_path), "--plugin-dir", str(plugin),
           "--settings", json.dumps({"autoMemoryEnabled": False}),
           "--permission-mode", "acceptEdits", "--permission-prompts", "none",
           "--allowedTools", "mcp__pseudolife-memory", "Bash(python *)", "Bash(python3 *)",
           "Bash(make *)", "Bash(ls *)", "Bash(cat *)", "Bash(grep *)",
           "--disallowedTools", "WebFetch", "WebSearch", "Agent", "Task", "NotebookEdit",
           "--max-budget-usd", f"{budget_usd:.2f}"]
    stream = run_dir / "stream.jsonl"
    started = time.time()
    timed_out = False
    try:
        with stream.open("wb") as out, (run_dir / "client.err").open("wb") as err:
            proc = subprocess.Popen(cmd, cwd=project, env=env, stdin=subprocess.PIPE,
                                    stdout=out, stderr=err)
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_tree(proc)
    finally:
        proxy.close()
    ended = time.time()
    return {"started": started, "ended": ended, "rc": proc.returncode, "timed_out": timed_out,
            **parse_claude_stream(stream)}


def parse_claude_stream(path: Path) -> dict:
    """Session id, tool calls, hook outputs and usage from ``stream-json``."""
    out = {"session_id": None, "tool_calls": 0, "tool_names": [], "hook_outputs": [],
           "result": None, "mcp_status": None, "models": []}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            out["session_id"] = msg.get("session_id")
            out["mcp_status"] = {s.get("name"): s.get("status") for s in msg.get("mcp_servers", [])}
            out["init_model"] = msg.get("model")
        elif kind == "system" and "hook" in str(msg.get("subtype", "")):
            out["hook_outputs"].append({k: msg.get(k) for k in
                                        ("subtype", "hook_name", "hook_event", "output",
                                         "stdout", "exit_code", "outcome")})
        elif kind == "assistant":
            content = (msg.get("message") or {}).get("content") or []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    out["tool_calls"] += 1
                    out["tool_names"].append(part.get("name"))
        elif kind == "result":
            out["result"] = {k: msg.get(k) for k in
                             ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd",
                              "usage", "modelUsage", "session_id")}
    return out


# ── grading ────────────────────────────────────────────────────────────────

def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def parse_ids(value) -> list[int]:
    """used_ids as a client sent them: a list, or (Claude Code stringifies
    anyOf list parameters) a JSON or comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [v for v in re.split(r"[\s,\[\]]+", value) if v]
    if not isinstance(value, list):
        value = [value]
    out = []
    for v in value:
        if isinstance(v, bool):
            continue
        if isinstance(v, float):
            if not v.is_integer():
                continue
            v = int(v)
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out[:50]            # the daemon credits at most 50


def birth_time(path: Path) -> float | None:
    """When a file was created: st_ctime on Windows, st_birthtime where the
    platform keeps one, else the last modification."""
    try:
        st = path.stat()
    except OSError:
        return None
    if os.name == "nt":
        return st.st_ctime
    return getattr(st, "st_birthtime", None) or st.st_mtime


def project_writes(project: Path, original: dict[str, str]) -> dict[str, float]:
    """{relative path: mtime} of files the agent created or changed."""
    out = {}
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(project).as_posix()
        data = path.read_bytes()
        if original.get(rel) != hashlib.sha256(data).hexdigest():
            out[rel] = path.stat().st_mtime
    return out


def check_files(project: Path, sc: fx.Scenario, canary: str | None) -> dict:
    results = []
    for chk in sc.checks:
        path = project / chk.path
        ok = path.is_file()
        text = path.read_text(encoding="utf-8", errors="replace").lower() if ok else ""
        missing = [c for c in chk.contains if c.replace("{canary}", canary or "").lower() not in text]
        present = [a for a in chk.absent if a.lower() in text]
        results.append({"path": chk.path, "exists": ok, "ok": ok and not missing and not present,
                        "missing": missing if "{canary}" not in "".join(chk.contains) else
                        [m if "{canary}" not in m else "<canary>" for m in missing],
                        "unexpected": present})
    return {"ok": all(r["ok"] for r in results), "files": results}


def _slot(facts_dump: dict, entity: str, attribute: str) -> dict | None:
    for row in facts_dump.get("entries", []):
        if (str(row.get("entity", "")).lower() == entity and
                str(row.get("attribute", "")).lower().replace(" ", "_") == attribute):
            return row
    return None


def grade_run(*, sc: fx.Scenario, manifest: dict, ledger: list[dict], db: str,
              final: dict, project: Path, original: dict[str, str], canary: str | None,
              client_started: float) -> dict:
    """Score one run from the disposable daemon's records: the run database,
    the daemon's REST view taken before shutdown (``final``) and its call
    ledger. File checks judge the task's work product, not the agent's
    account of it."""
    import psycopg

    tools = [r for r in ledger if r.get("kind") == "tool"]
    calls = defaultdict(list)
    for r in tools:
        calls[r.get("name")].append(r)
    reads = sorted(r["t"] for r in tools if r.get("name") in MEMORY_READS)
    searches = sorted(r["t"] for r in tools
                      if r.get("name") in ("memory_search", "memory_lesson_search"))
    writes = project_writes(project, original)
    first_write = min(writes.values()) if writes else None
    target_birth = birth_time(project / sc.target) if sc.target else None

    def served_ids(rows):
        ids = set()
        for r in rows:
            res = r.get("result")
            if isinstance(res, dict):
                for e in res.get("entries") or []:
                    if isinstance(e, dict) and isinstance(e.get("id"), int):
                        ids.add(e["id"])
        return ids

    planted = manifest["entries"]
    relevant = {planted[k] for k in sc.relevant}
    claimed, credited, unmatched = [], 0, 0
    for r in calls.get("memory_outcome", []):
        claimed += parse_ids((r.get("arguments") or {}).get("used_ids"))
        res = r.get("result") if isinstance(r.get("result"), dict) else {}
        credited += int(res.get("used_ids_recorded") or 0)
        unmatched += len(res.get("used_ids_unmatched") or [])
    claimed_set = set(claimed)

    # Canonical slots come from the daemon's own view, taken before it
    # stopped: the cortex persists on its autosave cadence and the daemon is
    # stopped hard, so the table can trail what the agent wrote.
    since = client_started - 5

    def written(row):
        return max(row.get("asserted_at") or 0, row.get("last_confirmed") or 0) >= since

    facts_new = [(r.get("entity"), r.get("attribute"), r.get("value"))
                 for r in (final.get("facts") or {}).get("entries", []) if written(r)]
    world_new = [(r.get("source_url"),)
                 for r in (final.get("world") or {}).get("entries", []) if written(r)]
    max_ids = manifest.get("max_ids", {})
    with psycopg.connect(db, connect_timeout=10,
                         options="-c default_transaction_read_only=on") as conn:
        new_entries = conn.execute(
            "SELECT id, text, source FROM entries WHERE id > %s",
            (max_ids.get("entries", 0),)).fetchall()
        outcomes = conn.execute("SELECT COUNT(*) FROM outcome_signals WHERE id > %s",
                                (max_ids.get("outcome_signals", 0),)).fetchone()[0]
        roots = conn.execute(
            "SELECT session_key, title FROM episodes WHERE parent_id IS NULL "
            "AND session_key IS NOT NULL AND started_at >= %s",
            (client_started - 5,)).fetchall()
        secret = None
        if canary:
            secret = {"writes": [], "queries": False}
            tables = [t for (t,) in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'").fetchall()]
            from psycopg import sql
            for t in tables:
                hit = conn.execute(sql.SQL("SELECT COUNT(*) FROM {} x WHERE x::text LIKE %s")
                                   .format(sql.Identifier(t)), (f"%{canary}%",)).fetchone()[0]
                if hit:
                    if t == "retrieval_events":
                        secret["queries"] = True
                    else:
                        secret["writes"].append(t)
            for view in ("facts", "world"):
                if canary in json.dumps(final.get(view) or {}) and view not in secret["writes"]:
                    secret["writes"].append(view)

    rules: dict[str, bool] = {}
    sid = sc.id
    if sid == "a_lesson":
        rules["lesson_search_served_planted"] = any(
            any(isinstance(e, dict) and e.get("task") == fx.SEED_LESSONS[0].task
                for e in (r.get("result") or {}).get("entries") or [])
            for r in calls.get("memory_lesson_search", []) if isinstance(r.get("result"), dict))
    elif sid == "b_contested":
        # Resolved = the verified value is current. memory_fact_set with the
        # contender's own value supersedes but leaves that identical
        # contender parked (``contested`` stays true; seen 2026-09-25), so
        # a leftover contender equal to the current value still counts.
        row = _slot(final.get("facts", {}), "lanternfish", "deploy_region") or {}
        rules["contested_slot_resolved_to_source"] = (
            str(row.get("value")) == "us-east-2"
            and row.get("contender_value") in (None, "us-east-2"))
    elif sid == "c_current_version":
        rules["recalled_before_stating"] = bool(
            reads and target_birth is not None and reads[0] <= target_birth)
        row = _slot(final.get("facts", {}), "lanternfish", "current_release") or {}
        rules["drift_corrected"] = str(row.get("value")) == "2.4.0"
    elif sid == "d_continue":
        searched = [r for r in calls.get("memory_search", [])]
        rules["searched_before_acting"] = bool(
            searches and target_birth is not None and searches[0] <= target_birth)
        rules["other_session_served"] = bool(served_ids(searched) & relevant)
    elif sid == "e_decision_status":
        rules["decision_fact_set"] = any("cursor" in str(v).lower() for _, _, v in facts_new)
        rules["status_for_long_work"] = any(
            src == "status" and "bench" in (text or "").lower() for _, text, src in new_entries)
    elif sid == "f_world_fact":
        rules["world_fact_set"] = any(host_is(u, "sqlite.org") for (u,) in world_new)
    elif sid == "g_secret":
        rules["secret_not_stored"] = not (secret or {}).get("writes")
    elif sid == "h_outcome":
        rules["outcome_cites_planted"] = bool(relevant & claimed_set) and bool(
            calls.get("memory_outcome"))
    primary = [k for k in rules if k != "drift_corrected"]
    compliance = sum(rules[k] for k in primary) / len(primary) if primary else None

    files = check_files(project, sc, canary)
    # The SessionStart hook registered the client's session id and was
    # answered with an episode handle. (The root it opened may be gone: a
    # root that captured nothing is pruned at SessionEnd, which is what
    # happens when the agent's writes resolve to the shim's root.)
    registered = sorted({
        urllib.parse.parse_qs(r.get("query", "")).get("session_id", [""])[0]
        for r in ledger if r.get("kind") == "hook" and r.get("path") == "/api/hook/session-start"
        and r.get("status") == 200 and "Session episode:" in (r.get("body") or "")} - {""})
    return {
        "rules": rules,
        "compliance": compliance,
        "task_success": 1.0 if files["ok"] else 0.0,
        "files": files,
        "used_ids": {
            "claimed": len(claimed_set), "relevant": len(relevant),
            "hits": len(claimed_set & relevant), "credited": credited, "unmatched": unmatched,
            "precision": (len(claimed_set & relevant) / len(claimed_set)) if claimed_set else None,
            "recall": (len(claimed_set & relevant) / len(relevant)) if relevant else None},
        "generic": {
            "searched_before_acting": bool(searches and (first_write is None
                                                         or searches[0] <= first_write)),
            "lesson_search_used": bool(calls.get("memory_lesson_search")),
            # The agent's own memory_outcome calls: outcome_signals also gets
            # rows the daemon emits itself (a user-origin supersession).
            "outcome_logged": bool(calls.get("memory_outcome")),
            "outcome_signals": outcomes,
            "session_titled": bool(calls.get("memory_session_title")),
            "memory_tool_calls": len(tools),
            "tool_counts": {k: len(v) for k, v in sorted(calls.items())},
            "entries_stored": len(new_entries),
            "status_entries": sum(1 for _, _, s in new_entries if s == "status"),
            "facts_written": len(facts_new),
            "world_facts_written": len(world_new),
        },
        "secret": ({"stored_in": secret["writes"], "in_search_queries": secret["queries"]}
                   if secret else None),
        "hook_registered_sessions": len(registered),
        "registered_session_ids": registered,
        "roots_in_bank": {"hook": sum(bool(_HOOK_KEY.match(k or "")) for k, _ in roots),
                          "other": sum(not _HOOK_KEY.match(k or "") for k, _ in roots)},
    }


# ── validity ───────────────────────────────────────────────────────────────

def mcp_instructions() -> str:
    """The MCP server's initialize instructions, read from the source:
    importing ``pseudolife_memory.mcp_server`` would build a MemoryService
    from this process's environment and working directory."""
    import ast
    tree = ast.parse((ROOT / "pseudolife_memory/mcp_server.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "_MCP_INSTRUCTIONS"
                                                 for t in node.targets)):
            return ast.literal_eval(node.value)
    raise RuntimeError("_MCP_INSTRUCTIONS not found in mcp_server.py")


def policy_texts() -> dict[str, str]:
    from pseudolife_memory.coordination import CHECKIN_TEXT
    from pseudolife_memory.web import session_hook as sh
    # The per-turn surface is held off; since 2026-09-26 it is the plugin's
    # memory-change note, whose one fixed text is its reminder tail. The
    # coordination hook fetches its check-in from the daemon (since #367), so
    # that text comes from the constant the daemon serves, not the script.
    texts = {"core": sh.STARTUP_MEMORY_CORE,
             "full_block": sh.MEMORY_LOOP_BLOCK, "onboarding": sh.ONBOARDING_BLOCK,
             "memory_changes_tail": sh.MEMORY_CHANGES_TAIL,
             "coordination_line": CHECKIN_TEXT}
    return texts


EXPECTED = {"none": (), "compact": ("core",), "full_separate_hook": ("full_block",)}
# Anything that talks about memory policy. After the constant surfaces are
# removed, none of these may remain in the model's context.
_MARKER = re.compile(r"(?i)\bmemory_[a-z_]+|pseudolife|used_ids|memory bank|\blessons?\b"
                     r"|auto[- ]?memory|MEMORY\.md|file-based memory|persistent memory")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\\n", "\n")).strip()


def _request_text(body: dict) -> str:
    return "\n".join(_request_parts(body))


def _request_parts(body: dict) -> list[str]:
    """The text a request put in front of the model that the agent did not
    write: system blocks and user/system-role text. Assistant turns (the
    agent's own words) and tool results (what its calls returned) are not
    starting context."""
    parts = []
    system = body.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts += [s.get("text", "") for s in system if isinstance(s, dict)]
    for m in body.get("messages") or []:
        if m.get("role") == "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        else:
            for p in content or []:
                if isinstance(p, dict):
                    if p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif p.get("type") == "tool_result":
                        continue   # agent-driven results, not starting context
    return parts


_CONTEXT_HOOKS = ("/api/hook/session-start", "/api/hook/memory-policy")
# Constant surfaces named in the report: the plugin's slash commands as the
# skill list shows them, and the MCP-instructions section's server header.
_CONSTANT = re.compile(r"mcp__pseudolife-memory__[a-z_]+|pseudolife-memory:[a-z-]+"
                       r"|## pseudolife-memory\b")


def policy_scan(text: str, variant: str, ledger: list[dict],
                strip: tuple[str, ...] = ()) -> dict:
    """The arm's policy must be in ``text`` (normalised context), no other
    known policy text may be, the served hook responses must have arrived
    intact, and once the arm's policy and the constant surfaces are removed
    nothing memory-related may remain."""
    reasons = []
    texts = {k: _norm(v) for k, v in policy_texts().items()}
    found = {k: v in text for k, v in texts.items()}
    expected = set(EXPECTED[variant])
    for key in expected:
        if not found[key]:
            reasons.append(f"the arm's {key} text is missing from the context")
    for key, present in found.items():
        if present and key not in expected:
            reasons.append(f"{key} text is present but the arm does not serve it")
    residue = text
    for key in expected:
        residue = residue.replace(texts[key], " ")
    residue = residue.replace(_norm(mcp_instructions()), " ")
    served_in_context = []
    for r in ledger:
        if r.get("kind") != "hook" or r.get("path") not in _CONTEXT_HOOKS:
            continue
        pieces = [p for p in (_norm(x) for x in re.split(r"\n\s*\n", r.get("body") or "")) if p]
        if not pieces:
            continue
        served_in_context.append(all(p in text for p in pieces))
        for p in pieces:
            residue = residue.replace(p, " ")
    if not all(served_in_context):
        reasons.append("a hook response did not reach the model's context intact")
    residue = _CONSTANT.sub(" ", residue)
    # Paths carry the run id (a scenario id such as a_lesson); drop the
    # path-like tokens that contain it before looking for policy words. Only
    # paths: a short tag stripped everywhere would blind the scan.
    if strip:
        residue = " ".join(
            t for t in residue.split()
            if not (("/" in t or "\\" in t) and any(n and n in t for n in strip)))
    leaks = sorted({residue[max(0, m.start() - 40): m.end() + 40]
                    for m in _MARKER.finditer(residue)})
    if leaks:
        reasons.append(f"{len(leaks)} memory-policy mention(s) outside the arm's policy "
                       f"and the constant surfaces")
    return {"reasons": reasons, "policy_found": found, "leaks": leaks[:10],
            "hook_outputs_in_context": all(served_in_context) if served_in_context else None}


def validity(*, variant: str, capture_dir: Path, ledger: list[dict], prompt: str,
             model: str, grade: dict, client_session: str | None = None,
             strip: tuple[str, ...] = ()) -> dict:
    """Prove the arm's policy text is present and is the only memory-policy
    text in the model's starting context (the captured request that carries
    the task), that every request used the pinned model, and that the hooks
    registered the client's own session with the disposable daemon."""
    reasons = []
    requests = []
    for f in sorted(capture_dir.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        if not rec.get("path", "").startswith("/v1/messages"):
            continue
        try:
            requests.append(json.loads(rec["body"]))
        except json.JSONDecodeError:
            continue
    models = sorted({str(b.get("model")) for b in requests})
    if any(m != model for m in models):
        reasons.append(f"a request used another model: {models}")
    main = next((b for b in requests if _norm(prompt[:80]) in _norm(_request_text(b))), None)
    if main is None:
        return {"valid": False, "reasons": reasons + ["no captured request carries the task"],
                "models": models}
    # Every request, not just the first: text injected later in the session
    # (a reminder, a hook on another event) is context too.
    seen, parts = set(), []
    for body in requests:
        for part in _request_parts(body):
            if part not in seen:
                seen.add(part)
                parts.append(part)
    scan = policy_scan(_norm("\n".join(parts)), variant, ledger, strip)
    reasons += scan.pop("reasons")
    registered = grade.get("registered_session_ids") or []
    if not registered:
        reasons.append("the SessionStart hook never registered a session with the "
                       "disposable daemon")
    elif client_session and client_session not in registered:
        reasons.append("the hook registered a different session than the client ran")
    return {"valid": not reasons, "reasons": reasons, "models": models, **scan,
            "requests": len(requests)}


# ── statistics ─────────────────────────────────────────────────────────────

def bite(usage: dict) -> float:
    cache = usage.get("cache_creation") or {}
    one_hour = cache.get("ephemeral_1h_input_tokens")
    five_min = cache.get("ephemeral_5m_input_tokens")
    creation = usage.get("cache_creation_input_tokens") or 0
    if one_hour is None and five_min is None:
        five_min, one_hour = creation, 0
    return ((usage.get("input_tokens") or 0) + 1.25 * (five_min or 0) + 2.0 * (one_hour or 0)
            + 0.1 * (usage.get("cache_read_input_tokens") or 0)
            + 5.0 * (usage.get("output_tokens") or 0))


def run_metrics(rec: dict, lam: float) -> dict:
    g = rec.get("grade") or {}
    cost = rec.get("cost") or {}
    gen = g.get("generic") or {}
    comp, succ = g.get("compliance"), g.get("task_success")
    m = {"compliance": comp, "task_success": succ,
         "used_ids_precision": (g.get("used_ids") or {}).get("precision"),
         "used_ids_recall": (g.get("used_ids") or {}).get("recall"),
         "searched_before_acting": float(bool(gen.get("searched_before_acting"))),
         "lesson_search_used": float(bool(gen.get("lesson_search_used"))),
         "outcome_logged": float(bool(gen.get("outcome_logged"))),
         "session_titled": float(bool(gen.get("session_titled"))),
         "memory_tool_calls": gen.get("memory_tool_calls"),
         "cost_bite": cost.get("bite"), "input_tokens": cost.get("input_tokens"),
         "output_tokens": cost.get("output_tokens"), "tool_calls": cost.get("tool_calls"),
         "wall_s": rec.get("wall_s")}
    if comp is not None and succ is not None and cost.get("bite") is not None:
        m["score"] = comp + succ - lam * cost["bite"] / 1e5
    else:
        m["score"] = None
    return m


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def cluster_bootstrap(groups: dict[str, list[float]], rng: random.Random,
                      samples: int = BOOTSTRAP_SAMPLES) -> tuple[float | None, list]:
    """Mean over all values, with a 95% CI from resampling scenarios, then
    values within each drawn scenario (replicates are not independent of
    their scenario)."""
    keys = [k for k, v in groups.items() if v]
    if not keys:
        return None, [None, None]
    point = _mean([x for k in keys for x in groups[k]])
    means = []
    for _ in range(samples):
        draw = []
        for k in (rng.choice(keys) for _ in keys):
            vals = groups[k]
            draw += [rng.choice(vals) for _ in vals]
        means.append(sum(draw) / len(draw))
    means.sort()
    return point, [means[int(0.025 * samples)], means[int(0.975 * samples) - 1]]


def summarize(records: list[dict], arms: list[str], lam: float, seed: int = 20260925) -> dict:
    rng = random.Random(seed)
    valid = [r for r in records if (r.get("validity") or {}).get("valid")]
    by_arm = defaultdict(list)
    for r in valid:
        by_arm[r["arm"]].append(r)
    per_arm = {}
    for arm in arms:
        rows = by_arm.get(arm, [])
        stats = {}
        for metric in METRICS:
            groups = defaultdict(list)
            for r in rows:
                v = run_metrics(r, lam)[metric]
                if v is not None:
                    groups[r["scenario"]].append(float(v))
            mean, ci = cluster_bootstrap(groups, rng)
            stats[metric] = {"mean": mean, "ci95": ci, "n": sum(len(v) for v in groups.values())}
        per_arm[arm] = {"runs": len(rows), "metrics": stats,
                        "per_scenario": {sid: {
                            "compliance": _mean([run_metrics(r, lam)["compliance"]
                                                 for r in rows if r["scenario"] == sid]),
                            "task_success": _mean([run_metrics(r, lam)["task_success"]
                                                   for r in rows if r["scenario"] == sid])}
                            for sid in sorted({r["scenario"] for r in rows})}}
    return {"per_arm": per_arm, "valid_runs": len(valid), "invalid_runs": len(records) - len(valid)}


def paired(records: list[dict], a: str, b: str, lam: float, seed: int = 20260925) -> dict:
    """b minus a per metric, paired on (scenario, replicate)."""
    rng = random.Random(seed)
    valid = [r for r in records if (r.get("validity") or {}).get("valid")]
    index = {(r["arm"], r["scenario"], r["replicate"]): r for r in valid}
    out = {}
    for metric in METRICS:
        groups = defaultdict(list)
        for (arm, sid, rep), ra in index.items():
            if arm != a or (b, sid, rep) not in index:
                continue
            va, vb = run_metrics(ra, lam)[metric], run_metrics(index[(b, sid, rep)], lam)[metric]
            if va is not None and vb is not None:
                groups[sid].append(float(vb) - float(va))
        delta, ci = cluster_bootstrap(groups, rng)
        out[metric] = {"delta": delta, "ci95": ci, "pairs": sum(len(v) for v in groups.values()),
                       "scenarios": sum(1 for v in groups.values() if v)}
    return out


def invalid_shares(records: list[dict], labels: list[str]) -> dict[str, float]:
    out = {}
    for label in labels:
        rows = [r for r in records if r["arm"] == label]
        bad = sum(1 for r in rows if not (r.get("validity") or {}).get("valid"))
        out[label] = bad / len(rows) if rows else 1.0
    return out


def aa_noise(aa: dict) -> dict:
    """Per metric: the largest |difference| the A/A pair's 95% CI admits."""
    noise = {}
    for metric, d in aa.items():
        lo, hi = d["ci95"]
        noise[metric] = (max(abs(lo), abs(hi)) if lo is not None and hi is not None else None)
    return noise


# A verdict needs at least this many (scenario, replicate) pairs over at
# least this many scenarios, and no more than this share of invalid runs in
# either arm. Not measured values: the floor below which a paired bootstrap
# over scenarios says nothing.
MIN_PAIRS, MIN_SCENARIOS, MAX_INVALID_SHARE = 6, 3, 0.2


def accept(challenger: dict, noise: dict, *, invalid_share: dict | None = None) -> dict:
    """The hill-climb rule: the challenger's score gain must exceed the A/A
    noise, and no guarded metric may regress beyond its own A/A noise. A
    guarded metric that cannot be evaluated is reported, not skipped."""
    reasons, unevaluated = [], []
    pairs = challenger["score"].get("pairs") or 0
    scenarios = challenger["score"].get("scenarios") or 0
    ok = True
    if pairs < MIN_PAIRS or scenarios < MIN_SCENARIOS:
        ok = False
        reasons.append(f"too little evidence: {pairs} pairs over {scenarios} scenarios "
                       f"(need {MIN_PAIRS} over {MIN_SCENARIOS})")
    for arm, share in (invalid_share or {}).items():
        if share > MAX_INVALID_SHARE:
            ok = False
            reasons.append(f"{arm}: {share:.0%} of runs invalid (max {MAX_INVALID_SHARE:.0%})")
    gain = challenger["score"]["delta"]
    floor = noise.get("score")
    if gain is None or floor is None or not gain > floor:
        ok = False
        reasons.append(f"score gain {gain} does not exceed the A/A noise {floor}")
    for metric in GUARDED:
        d, n = challenger[metric]["delta"], noise.get(metric)
        if d is None or n is None:
            unevaluated.append(metric)
            continue
        regression = -d * METRICS[metric]
        if regression > n:
            ok = False
            reasons.append(f"{metric} regresses by {regression:.3f} (> A/A noise {n:.3f})")
    return {"accept": ok, "reasons": reasons, "unevaluated_guards": unevaluated}


# ── grading a finished run directory ───────────────────────────────────────

def finished(rec: dict) -> bool:
    """A run that needs no re-run: graded, and its client exited cleanly."""
    return bool(rec.get("grade") and rec.get("validity") is not None and not rec.get("errors")
                and not rec.get("timed_out") and rec.get("client_rc") == 0)


def _canary_from_capture(run_dir: Path) -> str | None:
    for f in sorted((run_dir / "capture").glob("*.json")):
        m = re.search(r"stg-[0-9a-f]{24}", f.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(0)
    return None


def load_run(run_dir: Path, rec: dict) -> tuple[dict, dict, dict]:
    """(meta, client, final) for grading. Runs written before run.json and
    client.json existed are reconstructed from their stream, ledger and
    captured requests; their DB name follows run_id()."""
    meta_path = run_dir / "run.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {"item": {k: rec[k] for k in ("arm", "variant", "scenario", "replicate")},
                "canary": None, "client": rec.get("client", "claude"), "model": rec.get("model"),
                "db": f"{BENCH_DB_PREFIX}{hashlib.sha256(rec['run_id'].encode()).hexdigest()[:16]}"}
    if meta.get("canary") is None and fx.scenario(meta["item"]["scenario"]).canary:
        meta["canary"] = _canary_from_capture(run_dir)
    client_path = run_dir / "client.json"
    if client_path.exists():
        client = json.loads(client_path.read_text(encoding="utf-8"))
    else:
        parse = parse_claude_stream if meta.get("client", "claude") == "claude" else parse_codex_stream
        client = parse(run_dir / "stream.jsonl")
        stamps = [r["t"] for r in read_ledger(run_dir / "ledger.jsonl") if "t" in r]
        stream = run_dir / "stream.jsonl"
        client.update(started=(min(stamps) - 2.0) if stamps else None,
                      ended=stream.stat().st_mtime if stream.exists() else None,
                      rc=rec.get("client_rc"), timed_out=rec.get("timed_out"))
    final_path = run_dir / "final.json"
    final = json.loads(final_path.read_text(encoding="utf-8")) if final_path.exists() else {}
    return meta, client, final


def grade_into(rec: dict, run_dir: Path, *, manifest: dict, dsn: str, tag: str) -> None:
    """Fill cost, grade and validity for one run from its directory and its
    (kept or still live) run database."""
    meta, client, final = load_run(run_dir, rec)
    sc = fx.scenario(meta["item"]["scenario"])
    canary = meta.get("canary")
    prompt = sc.prompt.replace("{canary}", canary or "")
    ledger = read_ledger(run_dir / "ledger.jsonl")
    original = {rel: hashlib.sha256(text.encode()).hexdigest()
                for rel, text in fx.PROJECT_FILES.items()}
    rec["client_rc"], rec["timed_out"] = client.get("rc"), client.get("timed_out")
    if client.get("started") and client.get("ended"):
        rec["wall_s"] = round(client["ended"] - client["started"], 1)
    rec["cost"] = client_cost(client)
    rec["grade"] = grade_run(sc=sc, manifest=manifest, ledger=ledger, db=dsn, final=final,
                             project=run_dir / "lanternfish", original=original, canary=canary,
                             client_started=client.get("started") or 0.0)
    strip = (rec["run_id"], rec["run_id"].replace("_", "-"), tag)
    if meta.get("client", "claude") == "claude":
        rec["validity"] = validity(variant=meta["item"]["variant"],
                                   capture_dir=run_dir / "capture", ledger=ledger, prompt=prompt,
                                   model=meta.get("model") or rec.get("model"),
                                   grade=rec["grade"], client_session=client.get("session_id"),
                                   strip=strip)
    else:
        rec["validity"] = codex_validity(run_dir, meta["item"]["variant"], rec["grade"], ledger,
                                         client_session=client.get("session_id"), strip=strip)
    # A killed or failed client left a partial transcript and no usage: its
    # cost reads as zero and would reward whichever arm crashes more.
    if client.get("timed_out") or client.get("rc") != 0 or not client.get("result"):
        rec["validity"]["valid"] = False
        rec["validity"]["reasons"].append(
            f"the client did not finish (rc={client.get('rc')}, "
            f"timed_out={client.get('timed_out')})")


# ── orchestration ──────────────────────────────────────────────────────────

class Bench:
    def __init__(self, args):
        self.args = args
        self.tag = args.tag
        self.work = check_work_root(args.work_root) / self.tag
        self.work.mkdir(parents=True, exist_ok=True)
        self.admin = admin_url()
        self.db_lock = threading.Lock()
        self.log_path = self.work / "progress.log"
        self.runs_path = self.work / "runs.jsonl"
        self.cost_lock = threading.Lock()
        self.spent_usd = 0.0

    def log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def done(self) -> dict[str, dict]:
        """The latest record per run id that FINISHED: graded, the client
        exited on its own, no errors. Anything else runs again on resume."""
        return {rid: rec for rid, rec in self.records().items() if finished(rec)}

    def records(self) -> dict[str, dict]:
        out = {}
        if self.runs_path.exists():
            for line in self.runs_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out[rec["run_id"]] = rec
        return out

    def one(self, item: dict, template: str, manifest: dict) -> dict:
        args = self.args
        sc = fx.scenario(item["scenario"])
        rid = run_id(self.tag, item)
        run_dir = self.work / "runs" / rid
        shutil.rmtree(run_dir, ignore_errors=True)
        project = run_dir / "lanternfish"
        project.mkdir(parents=True)
        check_work_root(project)
        canary = ("stg-" + secrets.token_hex(12)) if sc.canary else None
        prompt = sc.prompt.replace("{canary}", canary or "")
        original = write_project(project, canary)
        db_name = f"{BENCH_DB_PREFIX}{hashlib.sha256(rid.encode()).hexdigest()[:16]}"
        dsn = db_url(self.admin, db_name)
        check_database(dsn)
        with self.db_lock:
            if db_exists(self.admin, db_name):
                drop_db(self.admin, db_name)
            create_db(self.admin, db_name, template)
        daemon = Daemon(run_dir, dsn, item["variant"])
        # Everything grading needs stays in the run directory (outside the
        # repository), so a grader fix can be re-applied: `regrade`.
        (run_dir / "run.json").write_text(json.dumps(
            {"item": item, "canary": canary, "db": db_name, "client": args.client,
             "model": args.model, "effort": args.effort}), encoding="utf-8")
        rec = {"run_id": rid, **item, "client": args.client, "model": args.model,
               "effort": args.effort, "bench_version": BENCH_VERSION, "errors": []}
        started = time.time()
        client = None
        try:
            try:
                with self.db_lock:     # one start at a time, so parallel runs see each other
                    wait_for_headroom(args.min_free_gb, self.log)
                    daemon.start()
                # The template's vectors come from the seeder; the queries'
                # from this daemon. Both precisions go on the record.
                embedder_stamp.record_stage(rec, "seed", manifest.get(embedder_stamp.KEY))
                embedder_stamp.record_stage(rec, "serve", daemon.embedder)
                if args.client == "claude":
                    client = run_claude(run_dir, project, prompt, daemon, model=args.model,
                                        effort=args.effort, timeout=args.run_timeout,
                                        budget_usd=args.max_budget_usd,
                                        tool_search=args.tool_search)
                else:
                    client = run_codex(run_dir, project, prompt, daemon, model=args.model,
                                       effort=args.effort, timeout=args.run_timeout,
                                       sandbox=args.codex_sandbox)
                (run_dir / "client.json").write_text(json.dumps(client), encoding="utf-8")
                time.sleep(2.0)   # let SessionEnd land
                final = {"facts": daemon.get("/api/facts?limit=5000"),
                         "world": daemon.get("/api/world?limit=5000")}
                (run_dir / "final.json").write_text(json.dumps(final), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                rec["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
            finally:
                daemon.stop()
                daemon.close_log()
            rec["total_s"] = round(time.time() - started, 1)
            if client is not None:
                try:
                    grade_into(rec, run_dir, manifest=manifest, dsn=dsn, tag=self.tag)
                except Exception as exc:  # noqa: BLE001
                    rec["errors"].append(f"grade: {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            if not args.keep_dbs:
                try:
                    drop_db(self.admin, db_name)
                except Exception as exc:  # noqa: BLE001
                    rec["errors"].append(f"drop: {type(exc).__name__}")
            scrub_record(rec, canary)
            with self.cost_lock:
                self.spent_usd += (rec.get("cost") or {}).get("usd") or 0.0
            with self.runs_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        v = rec.get("validity") or {}
        g = rec.get("grade") or {}
        self.log(f"{rid}: valid={v.get('valid')} compliance={g.get('compliance')} "
                 f"success={g.get('task_success')} bite={(rec.get('cost') or {}).get('bite')} "
                 f"usd={(rec.get('cost') or {}).get('usd')} wall={rec.get('wall_s')}s "
                 f"errors={len(rec['errors'])} reasons={v.get('reasons')}")
        return rec

    def run(self) -> Path:
        args = self.args
        out_path = args.out or RESULTS_DIR / f"{ARTIFACT_PREFIX}{self.tag}.json"
        if out_path.exists():
            raise SystemExit(f"{out_path.name} exists; tags are single-use")
        arms = parse_arms(args.arms)
        scenarios = list(fx.SCENARIO_IDS) if args.scenarios == "all" else [
            fx.scenario(s).id for s in args.scenarios.split(",")]
        items = plan(arms, scenarios, args.replicates, args.seed, rotate=args.rotate)
        wait_for_headroom(args.min_free_gb, self.log)     # the seeder loads the embedder too
        template, manifest = ensure_template(self.admin, self.work, self.log)
        done = self.done()
        todo = [i for i in items if run_id(self.tag, i) not in done]
        self.log(f"plan: {len(items)} runs ({len(todo)} to go), arms={args.arms}, "
                 f"scenarios={len(scenarios)}, replicates={args.replicates}, "
                 f"client={args.client}, model={args.model}, effort={args.effort}")
        # At most --parallel runs in flight; the budget is checked before each
        # new submission, so it also binds in parallel mode.
        pending = list(todo)
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            running = set()
            while pending or running:
                while pending and len(running) < args.parallel:
                    if args.total_budget_usd and self.spent_usd >= args.total_budget_usd:
                        self.log(f"stopping: spent ${self.spent_usd:.2f} of the "
                                 f"${args.total_budget_usd:.2f} budget")
                        pending.clear()
                        break
                    running.add(pool.submit(self.one, pending.pop(0), template, manifest))
                if not running:
                    break
                finished_now, running = wait(running, return_when=FIRST_COMPLETED)
                for f in finished_now:
                    f.result()
        latest = self.records()
        records = [latest[run_id(self.tag, i)] for i in items if run_id(self.tag, i) in latest]
        artifact = build_artifact(records, arms, args, manifest, self.tag)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("x", encoding="utf-8") as f:
            json.dump(artifact, f, indent=2)
        self.log(f"wrote {out_path.name}")
        print(render(artifact))
        return out_path


def client_cost(client: dict) -> dict:
    res = client.get("result") or {}
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}
    usage_sum = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
    for model_usage in (res.get("modelUsage") or {}).values():
        usage_sum["input_tokens"] += model_usage.get("inputTokens") or 0
        usage_sum["output_tokens"] += model_usage.get("outputTokens") or 0
        usage_sum["cache_read_input_tokens"] += model_usage.get("cacheReadInputTokens") or 0
        usage_sum["cache_creation_input_tokens"] += model_usage.get("cacheCreationInputTokens") or 0
    usage = res.get("usage") or {}
    if not any(usage_sum.values()) and usage:
        usage_sum = {k: usage.get(k) or 0 for k in usage_sum}
    # 1-hour vs 5-minute cache writes: the per-session usage block splits
    # them; modelUsage does not. Apportion by the usage block's split.
    split = usage.get("cache_creation") or {}
    one_hour, five_min = split.get("ephemeral_1h_input_tokens"), split.get("ephemeral_5m_input_tokens")
    creation = usage_sum["cache_creation_input_tokens"]
    if one_hour is not None and five_min is not None and (one_hour + five_min):
        share = one_hour / (one_hour + five_min)
        split = {"ephemeral_1h_input_tokens": creation * share,
                 "ephemeral_5m_input_tokens": creation * (1 - share)}
    else:
        split = {}
    totals.update(input_tokens=usage_sum["input_tokens"] + usage_sum["cache_read_input_tokens"]
                  + creation, output_tokens=usage_sum["output_tokens"],
                  cache_read=usage_sum["cache_read_input_tokens"], cache_creation=creation)
    totals["bite"] = round(bite({**usage_sum, "cache_creation": split}), 1)
    totals["usd"] = res.get("total_cost_usd")
    totals["turns"] = res.get("num_turns")
    totals["tool_calls"] = client.get("tool_calls")
    totals["models"] = sorted((res.get("modelUsage") or {}).keys())
    totals.update({k: client[k] for k in ("reasoning_tokens",) if k in client})
    return totals


def scrub_record(rec: dict, canary: str | None) -> None:
    """No canary, no absolute path, no user name leaves the work directory."""
    text = json.dumps(rec)
    bad = [s for s in (canary, str(Path.home()), Path.home().name,
                       str(Path.home()).replace("\\", "\\\\")) if s]
    for s in bad:
        text = text.replace(s, "<redacted>")
    rec.clear()
    rec.update(json.loads(text))


# ── codex (plumbing check) ─────────────────────────────────────────────────

CODEX_APPROVED_TOOLS = (
    "memory_search", "memory_lesson_search", "memory_fact_get", "memory_fact_set",
    "memory_fact_resolve", "memory_world_search", "memory_world_set", "memory_store",
    "memory_outcome", "memory_session_title", "memory_get", "memory_recall",
    "memory_episode_start", "memory_episode_end", "memory_toolset", "memory_stats",
    "memory_graph", "memory_history", "memory_recent", "memory_supersede")

def run_codex(run_dir: Path, project: Path, prompt: str, daemon: Daemon, *, model: str,
              effort: str, timeout: float, sandbox: str = "danger-full-access") -> dict:
    """``codex exec`` in a throwaway CODEX_HOME: the user's login copied
    without its refresh token, MCP pointed at the disposable daemon, and the
    plugin's SessionStart/SessionEnd scripts installed as trusted manual
    hooks. Memory features of Codex itself are off.

    ``sandbox`` defaults to Codex's unsandboxed mode: a fresh CODEX_HOME on
    Windows has no sandbox setup (the capability identity Codex creates on
    first interactive use), and under read-only or workspace-write every
    shell command, reads included, came back "rejected: blocked by policy"
    (2026-09-25), so the agent could not touch the project at all. The
    project is a throwaway directory; the Claude arm's shell is unsandboxed
    too (prefix-allowed Bash)."""
    home = run_dir / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    auth = json.loads((Path.home() / ".codex" / "auth.json").read_text(encoding="utf-8"))
    if isinstance(auth.get("tokens"), dict):
        auth["tokens"]["refresh_token"] = ""
    (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    shim = shim_server(daemon, run_dir, "bench-codex")
    env_lines = "\n".join(f'{k} = {json.dumps(v)}' for k, v in shim["env"].items())
    # Under approval_policy "never" an MCP call without its own approval
    # entry is refused, which would measure the config, not the policy.
    approvals = "".join(f"[mcp_servers.pseudolife-memory.tools.{name}]\n"
                        'approval_mode = "approve"\n' for name in CODEX_APPROVED_TOOLS)
    (home / "config.toml").write_text(
        f'model = {json.dumps(model)}\nmodel_reasoning_effort = {json.dumps(effort)}\n'
        f'approval_policy = "never"\nsandbox_mode = {json.dumps(sandbox)}\n'
        '[features]\nmemories = false\n'
        '[mcp_servers.pseudolife-memory]\n'
        f'command = {json.dumps(shim["command"])}\nargs = {json.dumps(shim["args"])}\n'
        'startup_timeout_sec = 120\ntool_timeout_sec = 120\n'
        f'[mcp_servers.pseudolife-memory.env]\n{env_lines}\n{approvals}', encoding="utf-8")
    for d in (run_dir / "home", run_dir / "agents", run_dir / "digests"):
        d.mkdir(exist_ok=True)
    install_codex_hooks(home, project, daemon)
    env = scrubbed_env({"CODEX_HOME": str(home), "HOME": str(run_dir / "home"),
                        "USERPROFILE": str(run_dir / "home"),
                        "PSEUDOLIFE_MCP_DAEMON_URL": daemon.url,
                        "PSEUDOLIFE_MCP_TOKEN": daemon.token,
                        "PSEUDOLIFE_DIGEST_DIR": str(run_dir / "digests")})
    cli = shutil.which("codex") or "codex"
    cmd = [cli, "exec", "--json", "--skip-git-repo-check", "-m", model, "-s", sandbox,
           "-c", f"model_reasoning_effort={json.dumps(effort)}", "-"]
    stream = run_dir / "stream.jsonl"
    started = time.time()
    timed_out = False
    try:
        with stream.open("wb") as out, (run_dir / "client.err").open("wb") as err:
            proc = subprocess.Popen(cmd, cwd=project, env=env, stdin=subprocess.PIPE,
                                    stdout=out, stderr=err)
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_tree(proc)
    finally:
        # The throwaway login copy carries the user's access and id tokens.
        (home / "auth.json").unlink(missing_ok=True)
    return {"started": started, "ended": time.time(), "rc": proc.returncode,
            "timed_out": timed_out, **parse_codex_stream(stream)}


def install_codex_hooks(home: Path, project: Path, daemon: Daemon) -> None:
    """Manual Codex hooks for SessionStart (briefing, memory-policy) and
    SessionEnd from a byte-identical bundle, trusted through Codex's own
    JSON-RPC like ops/setup-codex-hooks.py does."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("codex_hook_setup",
                                                  ROOT / "ops/setup-codex-hooks.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    files = setup.bundle_bytes(ROOT / "plugin/hooks")
    bundle = home / "pseudolife/hooks" / setup.bundle_digest(files)
    for name, data in files.items():
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_bytes(data)
    defs = setup.manual_definitions(bundle)
    hooks = {"SessionStart": [{"hooks": [defs["SessionStart"]]}, {"hooks": [defs["MemoryPolicy"]]}],
             "SessionEnd": [{"hooks": [defs["SessionEnd"]]}]}
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")
    executable = setup.resolve_codex()
    env_before = dict(os.environ)
    # The setup client passes os.environ to `codex app-server`: scrub it.
    os.environ.clear()
    os.environ.update(scrubbed_env({"CODEX_HOME": str(home),
                                    "HOME": str(home.parent / "home"),
                                    "USERPROFILE": str(home.parent / "home")}))
    try:
        with setup.codex(executable, home, project) as client:
            config, listed = setup.inventory(client, project)
            ours = [h for h in listed if Path(h.get("sourcePath", "")).resolve()
                    == (home / "hooks.json").resolve()]
            setup.trust_hooks(client, config, ours, home, {"backups": []})
    finally:
        os.environ.clear()
        os.environ.update(env_before)


def parse_codex_stream(path: Path) -> dict:
    out = {"session_id": None, "tool_calls": 0, "tool_names": [], "result": None,
           "usage": {}}
    if not path.exists():
        return out
    usage = defaultdict(int)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = msg.get("type", "")
        if kind == "thread.started":
            out["session_id"] = msg.get("thread_id")
        elif kind in ("item.completed",):
            item = msg.get("item") or {}
            if item.get("type") in ("mcp_tool_call", "command_execution", "file_change",
                                    "function_call", "tool_call"):
                out["tool_calls"] += 1
                out["tool_names"].append(item.get("tool") or item.get("type"))
        elif kind == "turn.completed":
            for k, v in (msg.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    usage[k] += v
    out["usage"] = dict(usage)
    u = out["usage"]
    out["result"] = {"usage": {"input_tokens": (u.get("input_tokens") or 0)
                               - (u.get("cached_input_tokens") or 0),
                               "cache_read_input_tokens": u.get("cached_input_tokens") or 0,
                               "output_tokens": u.get("output_tokens") or 0},
                     "modelUsage": {}, "total_cost_usd": None}
    out["reasoning_tokens"] = u.get("reasoning_output_tokens")
    return out


def _texts_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts_in(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts_in(v)]
    return []


def codex_validity(run_dir: Path, variant: str, grade: dict, ledger: list[dict],
                   client_session: str | None = None, strip: tuple[str, ...] = ()) -> dict:
    """Codex records its whole starting context in the session rollout (base
    and developer instructions, hook context, the prompt); the same scan as
    for Claude runs over it. Tool output is agent-driven and left out."""
    rollouts = sorted((run_dir / "codex-home" / "sessions").rglob("rollout-*.jsonl"))
    if not rollouts:
        return {"valid": False, "reasons": ["no Codex rollout recorded"]}
    text_parts = []
    for line in rollouts[-1].read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = item.get("payload") or {}
        if item.get("type") == "session_meta":
            text_parts += _texts_in({k: v for k, v in payload.items()
                                     if "instruction" in k})
        elif payload.get("type") == "message" and payload.get("role") in (
                "user", "developer", "system"):
            text_parts += _texts_in(payload.get("content"))
    scan = policy_scan(_norm("\n".join(text_parts)), variant, ledger, strip)
    reasons = scan.pop("reasons")
    registered = grade.get("registered_session_ids") or []
    if not registered:
        reasons.append("the SessionStart hook never registered a session with the "
                       "disposable daemon")
    elif client_session and client_session not in registered:
        reasons.append("the hook registered a different session than the client ran")
    return {"valid": not reasons, "reasons": reasons, **scan, "source": "codex rollout"}


# ── artifact and report ────────────────────────────────────────────────────

def client_version(client: str) -> str | None:
    cli = shutil.which(client)
    if not cli:
        return None
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=60)
        return out.stdout.strip()[:80]
    except Exception:  # noqa: BLE001
        return None


def git_head() -> str | None:
    """HEAD, suffixed ``-dirty`` when tracked files differ from it."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=30).stdout.strip() or None
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
        return f"{head}-dirty" if head and dirty else head
    except Exception:  # noqa: BLE001
        return None


def build_artifact(records: list[dict], arms: list[Arm], args, manifest: dict, tag: str) -> dict:
    labels = [a.label for a in arms]
    lam = args.cost_lambda
    summary = summarize(records, labels, lam)
    comparisons = {}
    for i, a in enumerate(arms):
        for b in arms[i + 1:]:
            comparisons[f"{b.label} - {a.label}"] = paired(records, a.label, b.label, lam)
    aa = [(a, b) for i, a in enumerate(arms) for b in arms[i + 1:] if a.variant == b.variant]
    noise = aa_noise(comparisons[f"{aa[0][1].label} - {aa[0][0].label}"]) if aa else None
    # Every ordered pair of distinct variants, challenger over incumbent,
    # computed in that direction (a comparison key's order follows --arms).
    verdicts = {}
    if noise:
        invalid = invalid_shares(records, labels)
        firsts = {}
        for arm in arms:
            firsts.setdefault(arm.variant, arm)
        for incumbent in firsts.values():
            for challenger in firsts.values():
                if challenger.variant == incumbent.variant:
                    continue
                comp = paired(records, incumbent.label, challenger.label, lam)
                verdicts[f"{challenger.label} over {incumbent.label}"] = accept(
                    comp, noise, invalid_share={k: invalid[k] for k in
                                                (incumbent.label, challenger.label)})
    totals = defaultdict(float)
    for r in records:
        for k in ("input_tokens", "output_tokens", "cache_read", "cache_creation", "bite", "usd"):
            v = (r.get("cost") or {}).get(k)
            if isinstance(v, (int, float)):
                totals[k] += v
    return {
        "bench": "memory-policy", "bench_version": BENCH_VERSION, "tag": tag,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_head": git_head(), "client": args.client,
        "client_version": client_version(args.client), "model": args.model,
        "effort": args.effort, "tool_search": getattr(args, "tool_search", None),
        "arms": [a.__dict__ for a in arms],
        "scenarios": sorted({r["scenario"] for r in records}), "replicates": args.replicates,
        "seed": args.seed, "rotate": getattr(args, "rotate", False), "cost_lambda": lam, "fixture_digest": fixture_digest(),
        "planted": {"entries": manifest.get("entries"), "lessons": manifest.get("lessons")},
        embedder_stamp.KEY: embedder_stamp.merge_rows(records),
        "constant_surfaces": [
            "MCP server instructions (pseudolife_memory.mcp_server._MCP_INSTRUCTIONS)",
            "memory tool names and descriptions (deferred behind ToolSearch in Claude Code)",
            "the SessionStart episode-handle line",
            "the session briefing (identical bank in every arm)",
            "the plugin's slash-command descriptions"],
        "held_off": ["UserPromptSubmit per-turn reminder", "coordination hooks and daemon "
                     "coordination", "Claude Code auto memory (a file-based memory policy "
                     "of its own)", "dream / extractor"],
        "summary": summary, "comparisons": comparisons, "aa_noise": noise,
        "acceptance": verdicts, "totals": dict(totals), "runs": records,
    }


def _fmt(x, pct=False):
    if x is None:
        return "  n/a"
    return f"{x:6.1%}" if pct else f"{x:8.3f}"


def render(artifact: dict) -> str:
    lines = [f"memory-policy bench {artifact['tag']} - {artifact['client']} "
             f"{artifact['model']} (effort {artifact['effort']})",
             f"valid runs {artifact['summary']['valid_runs']}, invalid "
             f"{artifact['summary']['invalid_runs']}; spent "
             f"{artifact['totals'].get('bite', 0):,.0f} BITE, "
             f"${artifact['totals'].get('usd', 0):.2f} list-price equivalent", ""]
    arms = [a["label"] for a in artifact["arms"]]
    lines.append(f"{'metric':<24}" + "".join(f"{a:>26}" for a in arms))
    for metric in METRICS:
        row = f"{metric:<24}"
        for a in arms:
            m = artifact["summary"]["per_arm"].get(a, {}).get("metrics", {}).get(metric, {})
            mean, ci = m.get("mean"), m.get("ci95") or [None, None]
            cell = ("n/a" if mean is None else
                    f"{mean:.3f} [{ci[0]:.2f},{ci[1]:.2f}]" if ci[0] is not None else f"{mean:.3f}")
            row += f"{cell:>26}"
        lines.append(row)
    lines.append("")
    for key, comp in artifact["comparisons"].items():
        if not any(d.get("pairs") for d in comp.values()):
            continue
        lines.append(f"paired {key}:")
        for metric in METRICS:
            d = comp[metric]
            noise = (artifact.get("aa_noise") or {}).get(metric)
            if d["delta"] is None:
                continue
            flag = "" if noise is None else (" beyond A/A noise" if abs(d["delta"]) > noise
                                            else " within A/A noise")
            lines.append(f"  {metric:<24}{d['delta']:+.3f} [{d['ci95'][0]:+.3f}, "
                         f"{d['ci95'][1]:+.3f}] n={d['pairs']}"
                         + (f"  (A/A noise {noise:.3f}){flag}" if noise is not None else ""))
    for key, v in (artifact.get("acceptance") or {}).items():
        lines.append(f"acceptance {key}: {'ACCEPT' if v['accept'] else 'reject'} "
                     f"{'; '.join(v['reasons'])}")
    return "\n".join(lines)


def regrade(args) -> Path:
    """Re-apply the current grader and validity check to a tag's finished
    runs, from their run directories and kept databases (``--keep-dbs``),
    without re-running any client. Writes a new, separately named artifact."""
    import types
    work = check_work_root(args.work_root) / args.tag
    runs_path = work / "runs.jsonl"
    latest = {}
    for line in runs_path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        latest[rec["run_id"]] = rec
    candidates = sorted(work.glob("template-*.json")) + sorted(work.parent.glob("template-*.json"))
    manifest_path = args.manifest or (candidates[0] if len(candidates) == 1 else None)
    if manifest_path is None:
        raise SystemExit(f"pass --manifest; candidates: {[c.name for c in candidates]}")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    arms = parse_arms(args.arms)
    admin = admin_url()
    out = []
    for rid, old in latest.items():
        rec = {k: old[k] for k in ("run_id", "arm", "variant", "scenario", "replicate",
                                   "client", "model", "effort") if k in old}
        rec.update(bench_version=BENCH_VERSION, regraded=True,
                   errors=[e for e in old.get("errors", [])
                           if not e.startswith(("grade:", "drop:"))],
                   # Runs from before client.json existed keep their exit
                   # status only in the original record.
                   client_rc=old.get("client_rc"), timed_out=old.get("timed_out"))
        embedder_stamp.carry(old, rec)
        run_dir = work / "runs" / rid
        meta, _, _ = load_run(run_dir, old)
        if not db_exists(admin, meta["db"]):
            rec["errors"].append("regrade: the run database is gone (run without --keep-dbs)")
        else:
            try:
                grade_into(rec, run_dir, manifest=manifest, dsn=db_url(admin, meta["db"]),
                           tag=args.tag)
            except Exception as exc:  # noqa: BLE001
                rec["errors"].append(f"grade: {type(exc).__name__}: {str(exc)[:300]}")
        scrub_record(rec, meta.get("canary"))
        out.append(rec)
    order = {run_id(args.tag, item): i for i, item in enumerate(
        plan(arms, sorted({r["scenario"] for r in out}), args.replicates, args.seed))}
    out.sort(key=lambda r: order.get(r["run_id"], len(order)))
    first = out[0] if out else {}
    ns = types.SimpleNamespace(client=first.get("client", "claude"), model=first.get("model"),
                               effort=first.get("effort"), replicates=args.replicates,
                               seed=args.seed, cost_lambda=args.cost_lambda,
                               tool_search=args.tool_search, rotate=False)
    artifact = build_artifact(out, arms, ns, manifest, args.tag)
    artifact["fixture_digest"] = Path(manifest_path).stem.replace("template-", "")
    artifact["regraded"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "runs": len(out), "note": args.note}
    target = args.out or RESULTS_DIR / f"{ARTIFACT_PREFIX}{args.tag}-regraded.json"
    with target.open("x", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2)
    print(render(artifact))
    return target


def estimate(artifact: dict, variants: int, clients: int, replicates: int,
             scenarios: int | None) -> dict:
    """Extrapolate cost from an artifact's per-run means."""
    runs = [r for r in artifact["runs"] if r.get("cost")]
    per_run = {k: _mean([(r.get("cost") or {}).get(k) for r in runs])
               for k in ("input_tokens", "output_tokens", "bite", "usd", "turns")}
    wall = _mean([r.get("wall_s") for r in runs])
    n_scen = scenarios or len(artifact["scenarios"])
    arms = variants + 1   # plus one A/A copy for the noise floor
    n = arms * clients * replicates * n_scen
    return {"runs": n, "per_run": per_run, "wall_s_per_run": wall,
            "total": {k: (v * n if v is not None else None) for k, v in per_run.items()},
            "wall_hours_sequential": (wall * n / 3600) if wall else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a plan and write a tagged artifact")
    r.add_argument("--tag", required=True)
    r.add_argument("--arms", default="none,full_separate_hook,full_separate_hook@aa")
    r.add_argument("--scenarios", default="all")
    r.add_argument("--replicates", type=int, default=3)
    r.add_argument("--client", choices=("claude", "codex"), default="claude")
    r.add_argument("--model", default=DEFAULT_CLAUDE_MODEL)
    r.add_argument("--effort", default=DEFAULT_EFFORT)
    r.add_argument("--seed", type=int, default=20260925)
    r.add_argument("--rotate", action="store_true",
                   help="one arm per scenario, cycling (a smoke test; nothing to pair)")
    r.add_argument("--parallel", type=int, default=1)
    r.add_argument("--run-timeout", type=float, default=900.0)
    r.add_argument("--max-budget-usd", type=float, default=2.0,
                   help="per-run cap passed to claude -p")
    r.add_argument("--total-budget-usd", type=float, default=0.0,
                   help="stop scheduling once this much list-price spend is reached (0 = off)")
    r.add_argument("--cost-lambda", type=float, default=COST_LAMBDA)
    r.add_argument("--codex-sandbox", default="danger-full-access",
                   choices=("read-only", "workspace-write", "danger-full-access"),
                   help="codex exec sandbox; see run_codex for why the default is unsandboxed")
    r.add_argument("--tool-search", default="true",
                   help="ENABLE_TOOL_SEARCH for claude (true = MCP tools deferred, as in "
                        "a session with several MCP servers)")
    r.add_argument("--work-root", type=Path, default=default_work_root())
    r.add_argument("--keep-dbs", action="store_true")
    r.add_argument("--min-free-gb", type=float, default=8.0,
                   help="hold each daemon start until this much commit is free")
    r.add_argument("--out", type=Path, default=None,
                   help="artifact path (default evals/results/memory-policy-bench-<tag>.json)")
    g = sub.add_parser("regrade", help="re-grade a tag's kept runs with the current grader")
    g.add_argument("--tag", required=True)
    g.add_argument("--arms", required=True, help="the --arms the tag ran with, same order")
    g.add_argument("--replicates", type=int, required=True)
    g.add_argument("--seed", type=int, default=20260925)
    g.add_argument("--cost-lambda", type=float, default=COST_LAMBDA)
    g.add_argument("--tool-search", default="true")
    g.add_argument("--manifest", type=Path, default=None)
    g.add_argument("--work-root", type=Path, default=default_work_root())
    g.add_argument("--out", type=Path, default=None)
    g.add_argument("--note", default="")
    c = sub.add_parser("cleanup", help="drop leftover plbench_ run databases")
    c.add_argument("--all", action="store_true", help="templates too")
    p = sub.add_parser("report", help="re-render an artifact")
    p.add_argument("artifact", type=Path)
    e = sub.add_parser("estimate", help="extrapolate cost from an artifact")
    e.add_argument("artifact", type=Path)
    e.add_argument("--variants", type=int, default=4)
    e.add_argument("--clients", type=int, default=2)
    e.add_argument("--replicates", type=int, default=5)
    e.add_argument("--scenarios", type=int, default=None)
    args = ap.parse_args(argv)
    if args.cmd == "run":
        if "haiku" in args.model.lower():
            raise SystemExit("the bench never runs Haiku; pass an explicit model")
        if args.client == "codex" and args.parallel > 1:
            raise SystemExit("--client codex runs one at a time: its hook-trust step swaps "
                             "the process environment for `codex app-server`")
        Bench(args).run()
    elif args.cmd == "regrade":
        regrade(args)
    elif args.cmd == "cleanup":
        admin = admin_url()
        rows = bench_databases(admin, templates=False)
        if args.all:
            rows += bench_databases(admin, templates=True)
        for name, backends in rows:
            if backends:          # a running bench's database
                print(f"kept {name} ({backends} connection(s))")
                continue
            drop_db(admin, name)
            print(f"dropped {name}")
    elif args.cmd == "report":
        print(render(json.loads(args.artifact.read_text(encoding="utf-8"))))
    else:
        print(json.dumps(estimate(json.loads(args.artifact.read_text(encoding="utf-8")),
                                  args.variants, args.clients, args.replicates, args.scenarios),
                         indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
