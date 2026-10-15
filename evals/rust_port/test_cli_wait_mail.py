"""Public subprocess coverage added for the native leaf, separate from tests/."""
import os
from pathlib import Path
import sys

import pytest

from evals.rust_port.cli_process import paired_cases, reset_home
from evals.rust_port.cli_wait_mail import cases
from evals.rust_port.oracle_pins import require_pinned_files


@pytest.mark.parametrize("name", ["help", "short-help-value", "short-help-equals", "unknown",
                                 "conflict", "negative-exponent", "unicode-negative",
                                 "float-control-space", "invalid-unicode-basename",
                                 "missing-digest", "plain-mail", "seen-ring", "host-record",
                                 "help-before-ambiguous", "help-columns-40", "help-columns-80",
                                 "help-columns-120", "error-columns-40", "error-columns-80",
                                 "error-columns-120", "help-columns-1", "help-columns-2",
                                 "help-columns-3", "help-columns-7", "help-columns-12",
                                 "help-columns-13"])
def test_public_wait_mail_deterministic_bytes_and_candidate_controls(tmp_path, name):
    binary = os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if not binary:
        pytest.skip("native binary must be selected explicitly")
    root = Path(os.environ.get("PSEUDOLIFE_WAIT_MAIL_ORACLE_ROOT", Path(__file__).resolve().parents[2]))
    require_pinned_files(root)
    case = next(case for case in cases() if case["id"] == "wait-mail-" + name)
    commands = {"oracle": [sys.executable, "-m", "pseudolife_memory.cli"], "candidate": [binary]}
    home = tmp_path / "home"
    try:
        result = paired_cases([case], commands, root=root, home=home, url="http://127.0.0.1:29871")
        assert result["passed"], result["records"][0]["differences"]
        assert all(control["rejected"] for control in result["candidate_output_controls"])
    finally:
        reset_home(home)
