import base64
from copy import deepcopy
import json
import os
import sys

import pytest

from evals.rust_port import cli_argv
from evals.rust_port.harness import isolated_env, run_cli
from evals.rust_port.provenance import ROOT


def test_raw_invalid_argument_survives_the_pinned_python_process_boundary(tmp_path):
    cli_argv.require_phase1_source(ROOT)
    shadow = tmp_path / "pseudolife_memory"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "cli.py").write_text("raise RuntimeError('wrong source selected')", encoding="utf-8")
    for case in cli_argv.cases():
        observed = run_cli(cli_argv.oracle_command(ROOT), cli_argv.native_argv(case),
                           cwd=tmp_path, env=isolated_env(tmp_path / "home"), timeout=10)
        assert observed == cli_argv.expected_result(case)
        if os.name == "posix":
            assert cli_argv.native_argv(case) == [b"bad\xff"]


@pytest.mark.parametrize("arm", ["oracle", "candidate"])
@pytest.mark.parametrize("field,value", [
    ("exit_code", 1), ("stdout_b64", "Cg=="),
    ("stderr_b64", base64.b64encode(b"unknown mode 'bad\xef\xbf\xbd'\n").decode()),
])
def test_corrupted_arm_cannot_pass_even_when_other_arm_matches(arm, field, value):
    case = cli_argv.cases()[0]
    observations = {name: cli_argv.expected_result(case) for name in ("oracle", "candidate")}
    observations[arm][field] = value
    assert cli_argv.compare_case(case, **observations) == [
        {"arm": arm, "path": "/" + field, "reason": "value"}]


def test_matching_corruption_in_both_arms_still_fails():
    case = cli_argv.cases()[0]
    observed = cli_argv.expected_result(case)
    observed["stderr_b64"] = ""
    assert len(cli_argv.compare_case(case, observed, observed)) == 2


def test_process_corpus_receipt_retains_raw_argv_and_both_arms(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_argv, "require_phase1_source", lambda root: {"oracle_head": "fixture-pin"})
    monkeypatch.setattr(cli_argv, "command_identity", lambda command: {"executable_sha256": "fixture"})
    captured = []
    case = cli_argv.cases()[0]

    def capture(prefix, argv, **kwargs):
        captured.append(argv)
        return cli_argv.expected_result(case)

    monkeypatch.setattr(cli_argv, "run_cli", capture)
    out = tmp_path / "receipt.json"
    receipt = cli_argv.run(ROOT, ["fixture"], out)
    assert receipt["status"] == "passed"
    row = json.loads(out.read_text(encoding="utf-8"))["cases"][0]
    assert row["case"] == case
    assert row["oracle"] == row["candidate"] == cli_argv.expected_result(case)
    assert captured == [cli_argv.native_argv(case)] * 2
    assert receipt["normalizations"] == []


def test_case_cannot_be_transcoded_for_another_platform():
    case = deepcopy(cli_argv.cases()[0])
    case["platform"] = "nt" if os.name == "posix" else "posix"
    with pytest.raises(ValueError, match="native operating system"):
        cli_argv.native_argv(case)


def test_module_entrypoint_writes_public_exact_bytes_and_binds_executables(tmp_path):
    private = tmp_path / "private.json"
    public = tmp_path / "public.json"
    command = [sys.executable, "-m", "evals.rust_port.cli_argv", "--oracle-root", str(ROOT),
               "--candidate-json", json.dumps(cli_argv.oracle_command(ROOT)),
               "--out", str(private), "--public-out", str(public)]
    observed = run_cli(command, [], cwd=ROOT, env=isolated_env(tmp_path / "home"), timeout=20)
    assert observed["exit_code"] == 0
    assert observed["stderr_b64"] == ""
    receipt = json.loads(public.read_text(encoding="utf-8"))
    assert receipt == json.loads(private.read_text(encoding="utf-8"))
    assert receipt["production_source_matches_pin"]
    assert receipt["candidate"]["executable_sha256"] == receipt["oracle_python"]["executable_sha256"]
    assert receipt["cases"][0]["oracle"] == receipt["cases"][0]["candidate"]
    assert str(ROOT) not in public.read_text(encoding="utf-8")
    assert str(tmp_path) not in public.read_text(encoding="utf-8")
