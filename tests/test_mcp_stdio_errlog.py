"""stdio_client's default errlog must not depend on which test imports mcp first.

Found 2026-09-23 on PR #352: ``mcp.client.stdio.stdio_client`` declares
``errlog: TextIO = sys.stderr``, a default evaluated once, when the module is
first imported — and ``import mcp`` imports it eagerly, so the first import of
*any* ``mcp`` module binds it. When that first import ran inside a test using
``capsys`` (PR #352's ``tests/test_codex_doorbell.py`` reaching
``pseudolife_memory.coordination_adapter`` -> ``.channel`` -> ``mcp``), the
default became capsys's ``CaptureIO``, which has no file descriptor. Every
later ``stdio_client(params)`` without an explicit ``errlog=`` in that pytest
process then failed with ``io.UnsupportedOperation: fileno`` — six tests in
``tests/test_shim.py``, which passed on their own.

A full ``pytest tests/`` run was only safe by accident: ``tests/test_channel.py``
imports ``mcp`` at module level, so collection bound the default first. Any
subset without such a file exposed the ordering. ``tests/conftest.py`` now
imports ``mcp.client.stdio`` before any test module is collected, binding
pytest's session-long fd-capture file (the terminal under ``-s``).
``--capture=sys``/``tee-sys`` offer no fileno at any point and stay
unsupported; the capsys guard below fails loudly there.

This module must not import ``mcp`` — or a ``pseudolife_memory`` module that
reaches it — at module level: its own collection would do conftest's job and
the capsys guard below would pass vacuously when run alone.
"""

from __future__ import annotations

import importlib.util
import inspect
import io
import sys

import pytest


def _default_errlog(stdio_client) -> object:
    return inspect.signature(stdio_client).parameters["errlog"].default


def test_conftest_imports_mcp_at_module_level() -> None:
    """Order-proof, not lucky: conftest must bind the default while it
    loads, before collection reaches any test file. The capsys guard below
    cannot see a lost conftest import in a full run — test_channel.py's
    module-level ``import mcp`` binds the default at collection anyway — so
    this pins the import itself. (Checking where ``mcp.client.stdio`` sits in
    ``sys.modules`` instead depends on the import mode: pytest's
    ``importlib`` mode registers conftest before executing it.)"""
    bound = vars(sys.modules["tests.conftest"]).get("mcp")
    assert bound is not None and bound is sys.modules.get("mcp"), (
        "tests/conftest.py must `import mcp.client.stdio` at module level"
    )


def test_default_errlog_survives_a_first_import_under_capsys(capsys) -> None:
    """The regression: the import a capsys test would do must find a default
    that can still spawn a child process."""
    from mcp.client.stdio import stdio_client

    errlog = _default_errlog(stdio_client)
    assert errlog is not sys.stderr, (
        "stdio_client's default errlog is this test's capsys stream: "
        "mcp.client.stdio was first imported inside a test, not at collection"
    )
    try:
        # The child's stderr is passed by file descriptor on every platform.
        errlog.fileno()
    except io.UnsupportedOperation:
        pytest.fail(f"stdio_client's default errlog {errlog!r} has no fileno; "
                    "every stdio_client(params) call would fail")


def test_premise_the_sdk_binds_errlog_at_first_import(capsys) -> None:
    """Why conftest's import is needed: executing the module under capsys
    binds the capture stream. If this fails, the premise changed — the SDK
    resolves errlog per call, stdio_client moved out of mcp.client.stdio, or
    CaptureIO grew a fileno — so re-derive whether conftest's import (and
    this module) is still needed.

    The fresh copy is executed detached — never registered in
    ``sys.modules`` — so the suite's module, and its import order, stay
    untouched."""
    spec = importlib.util.find_spec("mcp.client.stdio")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    assert fresh is not sys.modules["mcp.client.stdio"]
    assert _default_errlog(fresh.stdio_client) is sys.stderr
    with pytest.raises(io.UnsupportedOperation):
        sys.stderr.fileno()
