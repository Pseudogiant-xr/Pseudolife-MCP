"""Opt-in cost attribution for the test suite (``-p tests.suite_profile_plugin``).

Writes one JSON line per test to ``$PSEUDOLIFE_SUITE_PROFILE_DIR/profile-<worker>.jsonl``
with the phase split (setup / call / teardown) and the counters that explain
where the time went: real embedding-model loads and forward passes, service
initialisations, ``ensure_schema`` runs, subprocess spawns, ``time.sleep``
requested on the main thread, and resident memory after the test. A final
``fixtures-<worker>.json`` totals setup time per fixture name. Nothing is
patched unless the plugin is loaded explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path

import pytest

_counters: dict[str, float] = defaultdict(float)
_fixture_totals: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
_phase: dict[str, float] = {}
_out = None
_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
_main_thread = threading.main_thread()
_real_sleep = time.sleep
_real_perf = time.perf_counter


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


def _install_patches() -> None:
    from sentence_transformers import SentenceTransformer

    real_init = SentenceTransformer.__init__
    SentenceTransformer.__init__ = _timed("model_load", real_init)
    real_encode = SentenceTransformer.encode

    def encode(self, sentences, *args, **kwargs):
        n = 1 if isinstance(sentences, str) else len(sentences)
        _bump("encode_texts", n)
        return real_encode(self, sentences, *args, **kwargs)

    SentenceTransformer.encode = _timed("encode", encode)

    from pseudolife_memory import service as service_module
    from pseudolife_memory.storage import postgres as postgres_module
    from pseudolife_memory.storage import schema as schema_module

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


def pytest_configure(config: pytest.Config) -> None:
    global _out
    out_dir = Path(os.environ.get("PSEUDOLIFE_SUITE_PROFILE_DIR", "suite-profile"))
    out_dir.mkdir(parents=True, exist_ok=True)
    _out = open(out_dir / f"profile-{_worker}.jsonl", "w", encoding="utf-8")
    _install_patches()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    start = _real_perf()
    yield
    rec = _fixture_totals[f"{fixturedef.argname}@{fixturedef.scope}"]
    rec[0] += 1
    rec[1] += _real_perf() - start


def pytest_runtest_logstart(nodeid, location) -> None:
    _counters.clear()
    _phase.clear()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _phase[report.when] = report.duration
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        _phase["outcome"] = report.outcome  # type: ignore[assignment]


def pytest_runtest_logfinish(nodeid, location) -> None:
    if _out is None:
        return
    record = {"id": nodeid, **{k: round(v, 4) for k, v in _phase.items()
                                if isinstance(v, float)}}
    record["outcome"] = _phase.get("outcome", "passed")
    for key, value in _counters.items():
        if key == "_spawns":
            record["spawns"] = value
        else:
            record[key] = round(value, 4)
    record.update(_rss())
    _out.write(json.dumps(record) + "\n")
    _out.flush()


def pytest_sessionfinish(session, exitstatus) -> None:
    if _out is None:
        return
    _out.close()
    out_dir = Path(_out.name).parent
    totals = {name: {"n": int(n), "s": round(s, 3)}
              for name, (n, s) in sorted(_fixture_totals.items(),
                                         key=lambda kv: -kv[1][1])}
    (out_dir / f"fixtures-{_worker}.json").write_text(
        json.dumps(totals, indent=1), encoding="utf-8")
