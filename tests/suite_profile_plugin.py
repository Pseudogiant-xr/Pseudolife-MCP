"""Opt-in cost attribution for the test suite (``-p tests.suite_profile_plugin``).

Writes one JSON line per test to ``$PSEUDOLIFE_SUITE_PROFILE_DIR/profile-<worker>.jsonl``
with the phase split (setup / call / teardown), each phase's start and end on
the monotonic clock, and the counters that explain where the time went: real
embedding-model loads and forward passes, service initialisations,
``ensure_schema`` runs, database creation, drops, truncations and backend
terminations, subprocess spawns, ``time.sleep`` requested on the main thread,
the slow fixture teardowns, and resident memory after the test. A final
``fixtures-<worker>.json`` totals setup and teardown time per fixture name.

The controlling process (the xdist controller, or the only process) also
writes ``manifest.json`` (an allowlisted description of the run: revision,
arguments, workers, runtime and package versions, CPU and memory limits, the
test server's version and settings, the collected inventory's size and hash,
outcome counts), ``inventory.txt`` (the collected node ids) and
``samples.jsonl``: one line per interval with the run's process-tree memory,
system memory, pressure and CPU wait, and the test server's session wait
events and checkpoint / fsync counters. The worker timeline and the samples
share the monotonic clock, so a slow teardown can be matched with what the
server was waiting on at the time (``evals/suite_profile_summary.py``).

What is never recorded: environment dumps, connection strings, SQL text,
hostnames, absolute paths (arguments keep only a path's last component), and
sessions of other database roles than the test login's (the server may also
serve a live bank). Nothing is patched unless the plugin is loaded
explicitly, and it never changes the run's outcome or exit status: a
measurement that fails is recorded as an error class and skipped.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import pytest

_counters: dict[str, float] = defaultdict(float)
_fixture_totals: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
_teardown_totals: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
_teardown_start: dict[int, float] = {}
_slow_teardowns: list[list] = []
_phase: dict[str, float] = {}
_span: dict[str, list[float]] = {}
# The phase the main thread is in ("setup", "call", "teardown"; "none"
# between tests). A statement a background thread runs is credited to it.
_current_phase = ["none"]
_out = None
_out_dir: Path | None = None
_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
_main_thread = threading.main_thread()
_real_sleep = time.sleep
_real_perf = time.perf_counter
_mono = time.monotonic

# A fixture teardown at least this long is named in its test's record.
SLOW_TEARDOWN_S = 0.5
# Seconds between samples; PSEUDOLIFE_SUITE_PROFILE_INTERVAL overrides.
DEFAULT_INTERVAL_S = 1.0

# Statement kinds timed per test. Matched on the statement's start only; the
# text itself is never kept.
_SQL_KINDS = (
    ("db_create", re.compile(r"\s*CREATE\s+DATABASE\b", re.I)),
    ("db_drop", re.compile(r"\s*DROP\s+DATABASE\b", re.I)),
    ("truncate", re.compile(r"\s*TRUNCATE\b", re.I)),
    ("terminate", re.compile(r"\s*SELECT\s+pg_terminate_backend\b", re.I)),
    ("checkpoint", re.compile(r"\s*CHECKPOINT\b", re.I)),
)


def _bump(name: str, value: float = 1.0) -> None:
    _counters[name] += value


def _timed(name: str, fn):
    def wrapper(*args, **kwargs):
        start = _real_perf()
        try:
            return fn(*args, **kwargs)
        finally:
            _bump(name + "_n")
            _bump(name + "_s", _real_perf() - start)
    wrapper.__wrapped__ = fn
    return wrapper


def _rss() -> dict[str, int]:
    try:
        import psutil
    except ImportError:
        return {}
    info = psutil.Process().memory_info()
    out = {"rss": int(info.rss)}
    private = getattr(info, "private", None)
    if private is not None:
        out["private"] = int(private)
    return out


def _sql_kind(query, context=None) -> str | None:
    """The kind of statement ``query`` starts with, or None. A composed
    query is rendered with ``context`` (the cursor running it); psycopg 3.1
    cannot render one without a context."""
    if isinstance(query, bytes):
        query = query[:64].decode("ascii", "replace")
    elif not isinstance(query, str):
        try:
            query = query.as_string(context)
        except Exception:  # noqa: BLE001 - a composed query it cannot render
            return None
    head = query[:64]
    for kind, pattern in _SQL_KINDS:
        if pattern.match(head):
            return kind
    return None


def _patch_execute(cls) -> None:
    real = cls.execute

    def execute(self, query, *args, **kwargs):
        kind = _sql_kind(query, self)
        if kind is None:
            return real(self, query, *args, **kwargs)
        start = _real_perf()
        try:
            return real(self, query, *args, **kwargs)
        finally:
            seconds = _real_perf() - start
            _bump(kind + "_n")
            _bump(kind + "_s", seconds)
            # Also per phase, so a slow teardown is credited with its own
            # drop and not with the database its setup created.
            _bump(f"{_current_phase[0]}_{kind}_s", seconds)

    execute.__wrapped__ = real
    cls.execute = execute


def _install_patches() -> None:
    # Each group is optional: a lane without the embedding stack (or without
    # the service) still gets the timeline and the other counters.
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        SentenceTransformer = None
    if SentenceTransformer is not None:
        real_init = SentenceTransformer.__init__
        SentenceTransformer.__init__ = _timed("model_load", real_init)
        real_encode = SentenceTransformer.encode

        def encode(self, sentences, *args, **kwargs):
            n = 1 if isinstance(sentences, str) else len(sentences)
            _bump("encode_texts", n)
            return real_encode(self, sentences, *args, **kwargs)

        SentenceTransformer.encode = _timed("encode", encode)

    try:
        from pseudolife_memory import service as service_module
        from pseudolife_memory.storage import postgres as postgres_module
        from pseudolife_memory.storage import schema as schema_module
    except ImportError:
        service_module = None
    if service_module is not None:
        cls = service_module.MemoryService
        real_svc_init = cls.__init__
        cls.__init__ = _timed("service_new", real_svc_init)
        real_ensure_init = cls._ensure_init

        def ensure_init(self, *args, **kwargs):
            if getattr(self, "_cms", None) is not None:
                return real_ensure_init(self, *args, **kwargs)
            return _timed("service_init", real_ensure_init)(self, *args, **kwargs)

        cls._ensure_init = ensure_init
        wrapped_schema = _timed("ensure_schema", schema_module.ensure_schema)
        schema_module.ensure_schema = wrapped_schema
        postgres_module.ensure_schema = wrapped_schema

    try:
        import psycopg
    except ImportError:
        psycopg = None
    if psycopg is not None:
        # Cursor only: Connection.execute runs through a cursor, so patching
        # both would count each statement twice.
        _patch_execute(psycopg.Cursor)

    real_popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **kw):
        _bump("spawn_n")
        try:
            argv = [args] if isinstance(args, (str, bytes)) else list(args)
            head = os.path.basename(str(argv[0])).lower()
            if "-m" in argv:
                head += ":" + str(argv[argv.index("-m") + 1])
            _counters.setdefault("_spawns", [])  # type: ignore[arg-type]
            _counters["_spawns"].append(head)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — attribution only
            pass
        return real_popen_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = popen_init

    def sleep(seconds):
        if threading.current_thread() is _main_thread:
            _bump("sleep_main_s", float(seconds))
        else:
            _bump("sleep_bg_s", float(seconds))
        return _real_sleep(seconds)

    time.sleep = sleep


# ---------------------------------------------------------------------------
# The run description (controller only).

_PACKAGES = ("pytest", "pytest-xdist", "psycopg", "psutil", "torch",
             "sentence-transformers", "transformers", "numpy")
_ENV_ALLOWLIST = ("PSEUDOLIFE_TEST_EMBEDDER", "PSEUDOLIFE_SUITE_WORKERS",
                  "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                  "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "CUDA_VISIBLE_DEVICES",
                  "PSEUDOLIFE_SUITE_LOCK", "PSEUDOLIFE_SUITE_SLOTS",
                  "PSEUDOLIFE_SUITE_DISPATCHED", "GITHUB_EVENT_NAME", "GITHUB_SHA",
                  "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "ImageOS",
                  "ImageVersion")
_PG_SETTINGS = ("fsync", "synchronous_commit", "full_page_writes", "shared_buffers",
                "checkpoint_timeout", "max_wal_size", "track_io_timing",
                "wal_level", "max_connections")
_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|~)")
_PATHISH = re.compile(r"""[^\s=,;'"]*[\\/][^\s=,;'"]*""")


def _scrub(text: str) -> str:
    """``text`` with every absolute path, and every relative one passing
    through a home directory, reduced to its last component
    (``--junitxml=/home/<user>/x.xml`` -> ``--junitxml=<abs>/x.xml``), and
    every URL withheld (it may carry a password)."""
    def repl(match: re.Match) -> str:
        path = match.group(0)
        if "://" in path:
            return "<url>"
        parts = [p for p in re.split(r"[\\/]", path) if p]
        if _ABS.match(path) or any(p.lower() in ("users", "home") for p in parts):
            return "<abs>/" + (parts[-1] if parts else "")
        return path
    return _PATHISH.sub(repl, text)



def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _cgroup_limits() -> dict:
    out = {}
    rel = None
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                rel = line[3:]
    except OSError:
        return out
    if rel is not None:
        base = "/sys/fs/cgroup" + rel.rstrip("/")
        for name in ("memory.max", "cpu.max", "memory.high"):
            value = _read(f"{base}/{name}")
            if value is not None:
                out[name] = value
    return out


def _git_head(root: Path) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def _machine() -> dict:
    out = {"platform": sys.platform, "python": platform.python_version(),
           "cpu_count": os.cpu_count()}
    if hasattr(os, "sched_getaffinity"):
        out["cpu_affinity"] = len(os.sched_getaffinity(0))
    try:
        import psutil

        out["memory_total"] = int(psutil.virtual_memory().total)
        out["swap_total"] = int(psutil.swap_memory().total)
    except Exception:  # noqa: BLE001
        pass
    out["wsl"] = "microsoft" in platform.uname().release.lower()
    out["cgroup"] = _cgroup_limits()
    return out


def _packages() -> dict:
    out = {}
    for name in _PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


class _Run:
    """What the controller learns about the run, written as manifest.json."""

    def __init__(self, config: pytest.Config) -> None:
        root = Path(str(config.rootpath))
        self.data = {
            "complete": False,
            "commit": os.environ.get("PSEUDOLIFE_SUITE_COMMIT") or _git_head(root),
            "args": [_scrub(str(a)) for a in config.invocation_params.args],
            "workers_requested": getattr(config.option, "numprocesses", None),
            "dist": getattr(config.option, "dist", None),
            "environment": {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ},
            "packages": _packages(),
            "machine": _machine(),
            "interval_s": _interval(),
            "started_wall": time.time(),
            "started_mono": _mono(),
        }
        self.inventory: list[str] | None = None
        self.outcomes: Counter = Counter()
        # Where each skip was raised (file:line), never its message: a
        # message can carry a server address or an OS error's path.
        self.skips: Counter = Counter()
        self.workers: set[str] = set()

    def write(self, out_dir: Path) -> None:
        """Write manifest.json (and inventory.txt once known); a failure is
        recorded on the next write, never raised into the run."""
        try:
            data = dict(self.data)
            data["outcomes"] = dict(self.outcomes)
            data["skips"] = dict(sorted(self.skips.items()))
            data["workers_seen"] = sorted(self.workers)
            if self.inventory is not None:
                ids = sorted(self.inventory)
                data["inventory"] = {"count": len(ids), "sha256": hashlib.sha256(
                    "\n".join(ids).encode("utf-8")).hexdigest()}
                (out_dir / "inventory.txt").write_text(
                    "".join(i + "\n" for i in ids), encoding="utf-8")
            tmp = out_dir / "manifest.json.tmp"
            tmp.write_text(json.dumps(data, indent=1, default=str) + "\n",
                           encoding="utf-8")
            os.replace(tmp, out_dir / "manifest.json")
        except Exception as exc:  # noqa: BLE001 - never fail the run
            self.data["write_error"] = type(exc).__name__


def _interval() -> float:
    raw = os.environ.get("PSEUDOLIFE_SUITE_PROFILE_INTERVAL", "")
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL_S
    except ValueError:
        value = DEFAULT_INTERVAL_S
    return value if value > 0 else DEFAULT_INTERVAL_S


# ---------------------------------------------------------------------------
# The sampler (controller only).

# Ungrouped so each row's database can be checked against the production
# banks before it is counted; the database name itself is never written.
_SESSIONS_SQL = """
SELECT coalesce(backend_type, ''), coalesce(state, ''), coalesce(wait_event_type, ''),
       coalesce(wait_event, ''), datname
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
  AND (usename = current_user
       OR backend_type IN ('checkpointer', 'background writer', 'walwriter'))
"""
_CHECKPOINTER_COLUMNS = (
    # PostgreSQL 17+ (pg_stat_checkpointer)
    "num_timed", "num_requested", "num_done", "write_time", "sync_time",
    "buffers_written", "slru_written",
    # before 17 (pg_stat_bgwriter)
    "checkpoints_timed", "checkpoints_req", "checkpoint_write_time",
    "checkpoint_sync_time", "buffers_checkpoint", "buffers_backend_fsync",
)


def _pressure() -> dict:
    out = {}
    for res in ("cpu", "io", "memory"):
        text = _read(f"/proc/pressure/{res}")
        if not text:
            continue
        for line in text.splitlines():
            kind, _, rest = line.partition(" ")
            fields = dict(kv.split("=", 1) for kv in rest.split() if "=" in kv)
            if "avg10" in fields:
                out[f"{res}_{kind}_avg10"] = float(fields["avg10"])
    return out


def _resolve_server() -> tuple[str | None, str | None]:
    """The test server's admin URL and the login's source, resolved on the
    main thread (an import from the sampler thread could contend with
    collection's imports). (None, None) when there is no test server
    configuration to read."""
    try:
        from tests.pg_defaults import login_source_kind
        from tests.pg_fixtures import _admin_url

        return _admin_url(), login_source_kind()
    except Exception:  # noqa: BLE001 - a run outside this repository
        return None, None


def _production(name: str | None) -> bool:
    """Whether a session's database is a production bank; raises when the
    bank cannot be identified (the caller then counts no sessions)."""
    if not name:
        return False
    from pseudolife_memory.storage.schema import is_production_database

    return is_production_database(name)


class _Sampler(threading.Thread):
    def __init__(self, out_dir: Path, run: _Run, interval: float,
                 admin_url: str | None) -> None:
        super().__init__(name="suite-profile-sampler", daemon=True)
        self.out = open(out_dir / "samples.jsonl", "w", encoding="utf-8")
        self.run_info = run
        self.interval = interval
        self.admin_url = admin_url
        self.stopping = threading.Event()
        self.conn = None
        self.pg_error: str | None = None if admin_url else "NoTestServerConfigured"
        self.pg_tried = 0.0
        self.checkpointer_view: str | None = None
        self.has_io_view = False
        try:
            import psutil

            self.psutil = psutil
            self.root = psutil.Process()
            psutil.cpu_times_percent(None)
        except Exception:  # noqa: BLE001
            self.psutil = None

    def _connect(self) -> None:
        # Retried at most every 30 s: the server may come up after the run
        # starts, or not be configured at all (a run without PostgreSQL).
        now = _mono()
        if self.admin_url is None or self.conn is not None or now - self.pg_tried < 30:
            return
        self.pg_tried = now
        try:
            import psycopg

            conn = psycopg.connect(self.admin_url, connect_timeout=3, autocommit=True,
                                   application_name="suite-profile-sampler")
            conn.execute("SET statement_timeout = '2s'")
            version = int(conn.execute("SHOW server_version_num").fetchone()[0])
            settings = {}
            for name in _PG_SETTINGS:
                settings[name] = conn.execute(
                    "SELECT current_setting(%s, true)", (name,)).fetchone()[0]
            self.checkpointer_view = ("pg_stat_checkpointer" if version >= 170000
                                      else "pg_stat_bgwriter")
            self.has_io_view = version >= 160000
            # Without superuser or pg_read_all_stats the server hides the
            # background processes' rows, so "no checkpointer waits" then
            # means "not visible", not "none".
            visible = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE backend_type = 'checkpointer'").fetchone()[0]
            self.run_info.data["postgres"] = {
                "server_version_num": version, "settings": settings,
                "background_processes_visible": bool(visible)}
            self.conn = conn
            self.pg_error = None
        except Exception as exc:  # noqa: BLE001 - recorded by class only
            self.pg_error = type(exc).__name__

    def _sessions(self) -> list[list] | None:
        counts: Counter = Counter()
        for backend, state, kind, event, datname in self.conn.execute(
                _SESSIONS_SQL).fetchall():
            try:
                if _production(datname):
                    continue
            except Exception:  # noqa: BLE001 - bank unknown: count nothing
                return None
            counts[(backend, state, kind, event)] += 1
        return [[*key, n] for key, n in sorted(counts.items())]

    def _pg(self) -> dict:
        self._connect()
        if self.conn is None:
            return {"error": self.pg_error} if self.pg_error else {}
        try:
            out = {}
            sessions = self._sessions()
            if sessions is None:
                out["sessions_withheld"] = "production bank unidentified"
            else:
                out["sessions"] = sessions
            cur = self.conn.execute(f"SELECT * FROM {self.checkpointer_view}")
            names = [d.name for d in cur.description]
            row = cur.fetchone()
            out["checkpointer"] = {n: (float(v) if v is not None else None)
                                   for n, v in zip(names, row)
                                   if n in _CHECKPOINTER_COLUMNS}
            out["databases"] = self.conn.execute(
                "SELECT count(*) FROM pg_database").fetchone()[0]
            if self.has_io_view:
                out["fsyncs"] = {r[0]: [float(r[1] or 0), float(r[2] or 0)]
                                 for r in self.conn.execute(
                                     "SELECT backend_type, sum(fsyncs), sum(fsync_time) "
                                     "FROM pg_stat_io GROUP BY 1").fetchall()}
            return out
        except Exception as exc:  # noqa: BLE001
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
            self.pg_error = type(exc).__name__
            return {"error": self.pg_error}

    def _host(self) -> dict:
        out = {}
        if hasattr(os, "getloadavg"):
            out["load1"] = os.getloadavg()[0]
        out.update(_pressure())
        if self.psutil is None:
            return out
        ps = self.psutil
        try:
            vm = ps.virtual_memory()
            out["mem_available"] = int(vm.available)
            out["swap_used"] = int(ps.swap_memory().used)
            times = ps.cpu_times_percent(None)
            out["cpu_idle_pct"] = times.idle
            if hasattr(times, "iowait"):
                out["cpu_iowait_pct"] = times.iowait
            tree = [self.root, *self.root.children(recursive=True)]
            rss = 0
            for proc in tree:
                try:
                    rss += proc.memory_info().rss
                except (ps.NoSuchProcess, ps.AccessDenied):
                    pass
            # A sum of resident sets: pages shared between processes (the
            # interpreter, torch) count once per process, so this overstates
            # the tree's use. mem_available is the system's own figure.
            out["tree_rss_sum"] = rss
            out["tree_procs"] = len(tree)
            pg_rss = pg_n = 0
            for proc in ps.process_iter(["name", "memory_info"]):
                if (proc.info.get("name") or "").startswith("postgres"):
                    info = proc.info.get("memory_info")
                    if info is not None:
                        pg_rss += info.rss
                        pg_n += 1
            # None, not 0, when the server runs where this cannot see it
            # (another VM or container host).
            out["postgres_rss_visible"] = pg_rss if pg_n else None
            out["postgres_procs_visible"] = pg_n
        except Exception as exc:  # noqa: BLE001
            out["host_error"] = type(exc).__name__
        return out

    def sample(self) -> None:
        record = {"t": round(_mono(), 3), "wall": round(time.time(), 3)}
        record.update(self._host())
        pg = self._pg()
        if pg:
            record["pg"] = pg
        self.out.write(json.dumps(record) + "\n")
        self.out.flush()

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - never fail the run
                pass
            self.stopping.wait(self.interval)

    def stop(self) -> None:
        self.stopping.set()
        self.join(timeout=10)
        if self.is_alive():
            return  # still inside a sample: leave the file to it
        try:
            self.sample()  # the closing counters
        except Exception:  # noqa: BLE001
            pass
        self.out.close()
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass


_run: _Run | None = None
_sampler: _Sampler | None = None
_root: str | None = None
# What a reused profile directory may still hold from an earlier run.
_OWN_FILES = ("profile-*.jsonl", "fixtures-*.json", "samples.jsonl", "manifest.json",
              "manifest.json.tmp", "inventory.txt", "summary.json")


def pytest_configure(config: pytest.Config) -> None:
    global _out, _out_dir, _run, _sampler, _root
    _out_dir = Path(os.environ.get("PSEUDOLIFE_SUITE_PROFILE_DIR", "suite-profile"))
    _out_dir.mkdir(parents=True, exist_ok=True)
    _install_patches()
    if _worker == "main":
        # Before any worker starts: an earlier run's files would be folded
        # into this run's summary.
        for pattern in _OWN_FILES:
            for stale in _out_dir.glob(pattern):
                try:
                    stale.unlink()
                except OSError:
                    pass
        _root = str(config.rootpath)
        _run = _Run(config)
        admin_url, login = _resolve_server()
        if login is not None:
            _run.data["login_source"] = login
        _run.write(_out_dir)
        _sampler = _Sampler(_out_dir, _run, _run.data["interval_s"], admin_url)
        _sampler.start()
    # An xdist controller's per-test records would repeat its workers'
    # without the counters; it keeps only the manifest and the samples.
    if not (_worker == "main" and getattr(config.option, "numprocesses", None)):
        _out = open(_out_dir / f"profile-{_worker}.jsonl", "w", encoding="utf-8")


def pytest_collection_finish(session: pytest.Session) -> None:
    if _run is not None and not getattr(session.config.option, "numprocesses", None):
        _run.inventory = [item.nodeid for item in session.items]
        _run.write(_out_dir)  # now, so a run that dies still has it


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids) -> None:
    if _run is not None and _run.inventory is None:
        _run.inventory = list(ids)
        _run.write(_out_dir)


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    start = _real_perf()
    yield
    key = f"{fixturedef.argname}@{fixturedef.scope}"
    rec = _fixture_totals[key]
    rec[0] += 1
    rec[1] += _real_perf() - start
    # Added after the fixture's own teardown, so it runs first (finalizers
    # run last-in first-out) and marks where that teardown starts;
    # pytest_fixture_post_finalizer, registered first, marks its end.
    ident = id(fixturedef)

    def mark() -> None:
        _teardown_start[ident] = _real_perf()

    try:
        fixturedef.addfinalizer(mark)
    except Exception:  # noqa: BLE001 - a pytest without it: no teardown split
        pass


def pytest_fixture_post_finalizer(fixturedef, request) -> None:
    start = _teardown_start.pop(id(fixturedef), None)
    if start is None:
        return
    seconds = _real_perf() - start
    key = f"{fixturedef.argname}@{fixturedef.scope}"
    rec = _teardown_totals[key]
    rec[0] += 1
    rec[1] += seconds
    if seconds >= SLOW_TEARDOWN_S:
        _slow_teardowns.append([key, round(seconds, 3)])


def _phase_span(when: str):
    _span[when] = [_mono(), 0.0]
    _current_phase[0] = when
    yield
    _span[when][1] = _mono()
    _current_phase[0] = "none"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield from _phase_span("setup")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield from _phase_span("call")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    yield from _phase_span("teardown")


def pytest_runtest_logstart(nodeid, location) -> None:
    _counters.clear()
    _phase.clear()
    _span.clear()
    _slow_teardowns.clear()
    _current_phase[0] = "none"
    if _out is not None:
        # A start line: the last one without its record names the test a
        # killed or hung worker was in.
        _out.write(json.dumps({"start": nodeid, "worker": _worker,
                               "t": round(_mono(), 4)}) + "\n")
        _out.flush()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _phase[report.when] = report.duration
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        _phase["outcome"] = report.outcome  # type: ignore[assignment]
    if report.when == "teardown" and report.outcome == "failed":
        _phase["outcome"] = "error"  # type: ignore[assignment]
    if _run is not None:
        worker = getattr(report, "worker_id", None) or _worker
        _run.workers.add(worker)
        if report.when == "???":
            # xdist's report for a test whose worker died under it.
            _run.outcomes["crashed"] += 1
        elif report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
            _run.outcomes[report.outcome] += 1
            if report.skipped and isinstance(report.longrepr, tuple):
                _run.skips[_skip_location(report.longrepr)] += 1
        elif report.when == "teardown" and report.outcome == "failed":
            _run.outcomes["teardown_error"] += 1


def _skip_location(longrepr: tuple) -> str:
    """``file:line`` of a skip, relative to the rootdir when inside it."""
    path, line = str(longrepr[0]), longrepr[1]
    if _root is not None and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, _root)
            if not rel.startswith(".."):
                path = rel.replace(os.sep, "/")
        except ValueError:  # another drive
            pass
    return f"{_scrub(path)}:{line}"


def pytest_runtest_logfinish(nodeid, location) -> None:
    if _out is None:
        return
    record = {"id": nodeid, "worker": _worker,
              **{k: round(v, 4) for k, v in _phase.items() if isinstance(v, float)}}
    record["outcome"] = _phase.get("outcome", "passed")
    # Monotonic [start, end] per phase: the clock is system-wide, so workers'
    # timelines and the controller's samples line up.
    for when, (start, end) in _span.items():
        if end:
            record[when + "_t"] = [round(start, 4), round(end, 4)]
    # A copy: background threads may add counters meanwhile.
    for key, value in list(_counters.items()):
        if key == "_spawns":
            record["spawns"] = list(value)
        else:
            record[key] = round(value, 4)
    if _slow_teardowns:
        record["slow_teardowns"] = list(_slow_teardowns)
    record.update(_rss())
    _out.write(json.dumps(record) + "\n")
    _out.flush()


def pytest_sessionfinish(session, exitstatus) -> None:
    # Nothing here may raise: pytest would turn a green run red and skip the
    # rest of its shutdown (the suite lock's release among it).
    try:
        if _out is not None:
            _out.close()

        def table(totals):
            return {name: {"n": int(n), "s": round(s, 3)}
                    for name, (n, s) in sorted(totals.items(), key=lambda kv: -kv[1][1])}

        if _out_dir is not None:
            (_out_dir / f"fixtures-{_worker}.json").write_text(
                json.dumps({"setup": table(_fixture_totals),
                            "teardown": table(_teardown_totals)}, indent=1),
                encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if _run is not None:
        try:
            if _sampler is not None:
                _sampler.stop()
        except Exception:  # noqa: BLE001
            pass
        _run.data["complete"] = True
        _run.data["exitstatus"] = int(exitstatus)
        _run.data["ended_wall"] = time.time()
        _run.data["ended_mono"] = _mono()
        _run.write(_out_dir)
