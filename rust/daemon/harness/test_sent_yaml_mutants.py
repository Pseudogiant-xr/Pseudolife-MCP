"""A mutant build must never overwrite a caller's trusted executable."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_inherited_target_keeps_trusted_binary_unchanged(tmp_path, monkeypatch):
    script = Path(__file__).with_name("sent_yaml_mutants.py")
    spec = importlib.util.spec_from_file_location("sent_yaml_mutants", script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    fake_repo = tmp_path / "repo"
    config = fake_repo / "rust/shim/src/sent_config.rs"
    config.parent.mkdir(parents=True)
    config.write_text((runner.ROOT / "rust/shim/src/sent_config.rs").read_text(encoding="utf-8"),
                      encoding="utf-8")
    scratch = tmp_path / "scratch"
    shared = tmp_path / "trusted-target"
    suffix = ".exe" if os.name == "nt" else ""
    trusted = shared / "debug" / ("pseudolife-stdio" + suffix)
    trusted.parent.mkdir(parents=True)
    trusted.write_bytes(b"trusted executable")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(shared))
    monkeypatch.setattr(runner, "ROOT", fake_repo)
    monkeypatch.setattr(sys, "argv", [str(script), "--scratch", str(scratch)])

    def external_tool(argv, *, env, stdout, **_):
        if argv[:2] == ["cargo", "test"]:
            stdout.write(b"test result: FAILED.\n")
            return subprocess.CompletedProcess(argv, 101)
        if argv[:2] == ["cargo", "build"]:
            target = Path(env["CARGO_TARGET_DIR"]) / "debug" / trusted.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"mutant executable")
            return subprocess.CompletedProcess(argv, 0)
        assert Path(env["PSEUDOLIFE_SENT_YAML_BIN"]).read_bytes() == b"mutant executable"
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(runner.subprocess, "run", external_tool)
    with pytest.raises(SystemExit) as stopped:
        runner.main()
    assert stopped.value.code == 0
    assert trusted.read_bytes() == b"trusted executable"
    assert (scratch / "target/debug" / trusted.name).read_bytes() == b"mutant executable"
