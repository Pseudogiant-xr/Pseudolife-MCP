"""The native installer (ops/install-native.*): the tests of test_installer_resources.py
that the native client path changed, run against the native installer.

test_installer_resources.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from string import Template

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_ps_volume_creation_consumes_resolved_names_and_stops_on_failure(tmp_path):
    from tests.test_installer_client_only_ps import _q, _output
    from tests.test_installer_client_only_ps_native import _PowerShell
    text = (ROOT / "ops/install-native.ps1").read_text(encoding="utf-8")
    block = text.split("# >>> volumes >>>\n", 1)[1].split("# <<< volumes <<<", 1)[0]
    # Creation runs in its own block, after the engine stage can refuse.
    block += text.split("# >>> create volumes >>>\n", 1)[1].split("# <<< create volumes <<<", 1)[0]
    ps = _PowerShell(tmp_path)
    proc = ps.run(
        "$ClientOnly=$false\n$envFile='fixture.env'\n"
        # The native client's volume-names step, answering resolved names.
        "function Invoke-NativeHelper {\n"
        "    if ($args[0] -ne 'volume-names') { throw \"unexpected native step $($args[0])\" }\n"
        "    'custom-bank'; 'custom-state'; $global:LASTEXITCODE=0\n"
        "}\n"
        + ps.stub_function("docker", "if ($args[1] -eq 'ls') { $global:LASTEXITCODE=0 } else { $global:LASTEXITCODE=1 }")
        + block + "\n'CONTINUED'\n")
    assert proc.returncode != 0
    assert "Could not create volume custom-bank" in _output(proc)
    assert ps.logged() == ["docker|volume ls --format {{.Name}}", "docker|volume create custom-bank"]
    assert "CONTINUED" not in _output(proc)


@pytest.mark.parametrize("project", ["user-stack", "", "pseudolife-mcp"])
@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
def test_extractor_removal_rechecks_project_and_uses_verified_id(tmp_path, script, project):
    from tests.native_client_stub import HELPER_PS, HELPER_SH
    from tests.test_installer_client_only import _stub
    from tests.test_installer_client_only_native import _Shell
    from tests.test_installer_existing_upgrade import _bash_variants
    from tests.test_installer_client_only_ps import _q, _output
    from tests.test_installer_client_only_ps_native import _PowerShell

    text = (ROOT / "ops" / script).read_text(encoding="utf-8")
    block = text.split("# >>> sidecar override >>>\n", 1)[1].split("# <<< sidecar override <<<", 1)[0]
    override = tmp_path / "override.yml"
    marker = "# fixture managed override"
    override.write_text(marker + "\n")
    identifier = "a" * 64
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        proc = ps.run(
            ps.stub_function("docker", "if ($args[0] -eq 'inspect') { "
                             f"'{identifier} {project}'" + " }\n$global:LASTEXITCODE=0")
            + f"$ClientOnly=$false\n$overrideFile='{_q(override)}'\n"
            f"$OverrideMarker='{marker}'\n$LegacyOverrideMarker='old'\n$LegacyShimOverrideMarker='older'\n"
            "function Test-ExtractorDisablesSidecar { $true }\n" + HELPER_PS + block)
        output, calls = _output(proc), ps.logged()
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        _stub(shell.bin / "docker", 'printf "docker|%s\\n" "$*" >>"$CALL_LOG"\n'
              f'if [ "$1" = inspect ]; then echo "{identifier} {project}"; fi\n')
        proc = shell.run(f"CLIENT_ONLY=''\noverride_file='{shell.path(override)}'\n"
                         f"OVERRIDE_MARKER='{marker}'\nLEGACY_OVERRIDE_MARKER=old\nLEGACY_SHIM_OVERRIDE_MARKER=older\n"
                         "extractor_disables_sidecar() { return 0; }\n" + HELPER_SH
                         # The override is written through the installer's own state step.
                         + next(line for line in text.splitlines() if line.startswith("installer_state() {"))
                         + "\n" + block)
        output, calls = proc.stdout + proc.stderr, shell.logged()
    assert proc.returncode == 0, output
    removed = [call for call in calls if call.startswith("docker|rm")]
    assert removed == ([f"docker|rm -f {identifier}"] if project == "pseudolife-mcp" else [])
