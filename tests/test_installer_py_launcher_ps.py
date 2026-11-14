"""The ops scripts probe the Windows `py` launcher with `-3` as one argument.

`$x = if (...) { @("-3") }` unrolls the one-item array into the string "-3",
and splatting a string passes its characters: `py - 3 -c ...` read a script
from stdin instead of probing, so a host whose only Python is `py` was never
found (and an interactive run waited on the console).
"""
import pytest

from tests.test_installer_client_only import ROOT



# install.ps1 and preflight.ps1 run no Python since their helpers and the
# Docker ownership check became native client steps, so they have no probe
# left; a count of 0 keeps one from returning unseen.
@pytest.mark.parametrize("script,probes", [("install.ps1", 0), ("preflight.ps1", 0), ("update.ps1", 1)])
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
