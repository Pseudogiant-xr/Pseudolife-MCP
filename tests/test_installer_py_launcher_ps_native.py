"""The native installer (ops/install-native.*): the tests of test_installer_py_launcher_ps.py
that the native client path changed, run against the native installer.

test_installer_py_launcher_ps.py keeps the default (Python client) installer's tests.
"""
import pytest

from tests.installer_fragments import ROOT



# install-native.ps1 and preflight-native.ps1 run no Python since their helpers and the
# Docker ownership check became native client steps, so they have no probe
# left; a count of 0 keeps one from returning unseen.
@pytest.mark.parametrize("script,probes", [("install-native.ps1", 0), ("preflight-native.ps1", 0), ("update.ps1", 1)])
def test_every_py_probe_builds_its_arguments_by_assignment(script, probes):
    # No ops script may use the if-expression form, which unrolls the
    # one-item array (preflight runs on every install; update.ps1 would run
    # `py - 3 update.py`, an empty script that exits 0).
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    assert '$probeArgs = if ($candidate -eq "py")' not in source
    assert source.count('if ($candidate -eq "py") { $probeArgs = @("-3") }') == probes
