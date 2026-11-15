"""install.ps1 probes the Windows `py` launcher with `-3` as one argument.

`$x = if (...) { @("-3") }` unrolls the one-item array into the string "-3",
and splatting a string passes its characters: `py - 3 -c ...` read a script
from stdin instead of probing, so a host whose only Python is `py` was never
found (and an interactive run waited on the console).
"""
import os
import sys

import pytest

from tests.test_installer_client_only import ROOT
from tests.test_installer_client_only_ps import _PowerShell, _output



def _fake_py(tmp_path):
    """A native `py` on an isolated PATH that records its argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "py-argv.txt"
    script = bin_dir / "fake_py.py"
    script.write_text("import json, sys\n"
                      f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + chr(10))\n"
                      "print(sys.executable)\n")
    (bin_dir / "py.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    return bin_dir, log


def _isolated_path(bin_dir):
    system = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32")
    return f"{bin_dir}{os.pathsep}{system}"


@pytest.mark.skipif(os.name != "nt", reason="the py launcher probe is Windows-only")
def test_installer_python_probe_passes_minus_3_to_py_as_one_argument(tmp_path):
    import json
    source = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
    start = source.index("function Get-InstallerPython {")
    end = source.index("\n}\n", start) + 3
    bin_dir, log = _fake_py(tmp_path)
    ps = _PowerShell(tmp_path)
    proc = ps.run(f"$env:PATH = '{_isolated_path(bin_dir)}'\n" + source[start:end]
                  + "Write-Output \"FOUND:$(Get-InstallerPython)\"\n")
    output = _output(proc)
    assert proc.returncode == 0, output
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls and calls[0][:2] == ["-3", "-c"], calls
    assert f"FOUND:{sys.executable}" in output


@pytest.mark.parametrize("script,probes", [("install.ps1", 2), ("preflight.ps1", 2), ("update.ps1", 1)])
def test_every_py_probe_builds_its_arguments_by_assignment(script, probes):
    # No ops script may use the if-expression form, which unrolls the
    # one-item array (preflight runs on every install; update.ps1 would run
    # `py - 3 update.py`, an empty script that exits 0).
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    assert '$probeArgs = if ($candidate -eq "py")' not in source
    assert source.count('if ($candidate -eq "py") { $probeArgs = @("-3") }') == probes


def test_no_ops_script_splats_a_one_item_array_from_an_if_expression():
    import re
    pattern = re.compile(r"=\s*if\s*\(.*\)\s*\{\s*@\(\s*\"[^\",]*\"\s*\)\s*\}")
    offenders = [f"{path.name}:{number}" for path in sorted((ROOT / "ops").glob("*.ps1"))
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if pattern.search(line)]
    assert not offenders
