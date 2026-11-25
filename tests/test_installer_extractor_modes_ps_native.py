"""The native installer (ops/install-native.*): the tests of test_installer_extractor_modes_ps.py
that the native client path changed, run against the native installer.

test_installer_extractor_modes_ps.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

import http.server
import re
import subprocess
import threading

import pytest

from tests.user_path_guard import user_path_unchanged

from tests import installer_sandbox
from tests.test_installer_client_only_ps_native import ROOT, _PowerShell, _output

INSTALL = "ops/install-native.ps1"


def test_an_empty_model_parameter_is_refused(tmp_path):
    ps = _PowerShell(tmp_path)
    with user_path_unchanged(ps.home):
        proc = installer_sandbox.run(
            [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / INSTALL),
             "-Extractor", "claude-only", "-Model", "", "-NoArt"],
            capture_output=True, text=True, timeout=120, env=installer_sandbox.layout(ps.env, tmp_path), root=tmp_path, check=False,
            stdin=subprocess.DEVNULL)
    assert proc.returncode == 2
    assert "-Model" in _output(proc)


def test_the_script_refuses_an_endpoint_mode_without_its_url(tmp_path):
    ps = _PowerShell(tmp_path)
    with user_path_unchanged(ps.home):
        proc = installer_sandbox.run(
            [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / INSTALL),
             "-Extractor", "endpoint", "-Model", "m", "-NoArt"],
            capture_output=True, text=True, timeout=120, env=installer_sandbox.layout(ps.env, tmp_path), root=tmp_path, check=False,
            stdin=subprocess.DEVNULL)
    assert proc.returncode == 2
    assert "-ExtractorUrl" in _output(proc)


def test_client_only_refuses_an_extractor_url(tmp_path):
    ps = _PowerShell(tmp_path)
    with user_path_unchanged(ps.home):
        proc = installer_sandbox.run(
            [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / INSTALL),
             "-DaemonUrl", "http://100.64.0.2:8765",
             "-ExtractorUrl", "http://pl.example.invalid:1234/v1", "-NoArt"],
            capture_output=True, text=True, timeout=120, env=installer_sandbox.layout(ps.env, tmp_path), root=tmp_path, check=False,
            stdin=subprocess.DEVNULL)
    assert proc.returncode == 2
    # The client-only refusal itself, not a binding error.
    [line] = [line for line in _output(proc).splitlines() if "client-only install" in line]
    assert "-ExtractorUrl" in line
