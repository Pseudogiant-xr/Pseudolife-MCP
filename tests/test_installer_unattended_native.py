"""The native installer (ops/install-native.*): the tests of test_installer_unattended.py
that the native client path changed, run against the native installer.

test_installer_unattended.py keeps the default (Python client) installer's tests.
"""
from pathlib import Path

import pytest

from tests.test_installer_existing_upgrade import user_path_unchanged

from tests.test_installer_client_only import BASH, ROOT
from tests.test_installer_client_only_native import _Shell
from tests.test_installer_client_only_ps import _q, _output
from tests.test_installer_client_only_ps_native import _PowerShell
from tests.test_installer_extractor_modes_native import _release_env


@BASH
def test_sh_yes_is_accepted_before_invalid_client_refuses(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    # The installer stages the native client before it reads --client.
    with user_path_unchanged(shell.home):
        proc = shell.run(f"'{shell.path(ROOT / 'ops/install-native.sh')}' --yes --client invalid --extractor sidecar --no-art\n",
                         extra_env=_release_env(shell))
    assert proc.returncode == 2
    assert "Unattended install" in proc.stdout
    assert "invalid --client" in proc.stderr
    assert shell.logged() == []
