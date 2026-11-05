"""The autostart epilogue follows the daemon deployment, not its checkout."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("overlay", ["", "docker-compose.rust-shadow.yml", "docker-compose.rust.yml", "docker-compose.rust-daemon.yml"])
def test_a_non_default_checkout_keeps_its_own_cutover_advice(tmp_path, overlay):
    import runpy
    advice = runpy.run_path(str(ROOT / "ops/shim_cutover.py"))["advice"]
    repo = tmp_path / "non-default checkout"
    files = str(repo / "ops/docker-compose.yml")
    if overlay:
        files += "," + str(repo / "ops" / overlay)
    text = advice(repo, 8099, {"com.docker.compose.project.config_files": files,
                             "com.docker.compose.project": "pseudolife-mcp"})
    assert "checkout daemon" in text and str(repo / "ops/.env") in text
    assert f'update --checkout "{repo}"' in text


@pytest.mark.parametrize("rust", [False, True])
def test_ghcr_release_overlay_keeps_release_update_advice(tmp_path, rust):
    import runpy
    advice = runpy.run_path(str(ROOT / "ops/shim_cutover.py"))["advice"]
    files = ",".join(str(tmp_path / "ops" / name) for name in ("docker-compose.yml", "docker-compose.ghcr.yml"))
    if rust:
        files += "," + str(tmp_path / "ops/docker-compose.rust-ghcr.yml")
    text = advice(tmp_path, 8082, {"com.docker.compose.project.config_files": files,
                                 "com.docker.compose.project": "pseudolife-mcp"})
    assert "--checkout" not in text
    assert "release-mode" in text and "pseudolife-mcp update" in text


def test_a_packaged_compose_copy_is_not_this_checkout(tmp_path):
    import runpy
    advice = runpy.run_path(str(ROOT / "ops/shim_cutover.py"))["advice"]
    text = advice(tmp_path / "checkout", 8082, {
        "com.docker.compose.project.config_files": str(tmp_path / "release/ops/docker-compose.yml"),
        "com.docker.compose.project": "pseudolife-mcp"})
    assert "stack manager or Compose" in text
    assert "--checkout" not in text
    unknown = advice(tmp_path / "checkout", 8082, {})
    assert "No local daemon deployment detected" in unknown and "--checkout" not in unknown


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_stack_manager_epilogue_uses_the_deployments_environment(tmp_path, shell):
    executable = shutil.which(shell)
    if shell == "bash" and os.name == "nt":
        git = shutil.which("git")
        executable = str(Path(git).parents[1] / "bin" / "bash.exe") if git else None
    if not executable:
        pytest.skip(f"{shell} unavailable")
    suffix = "sh" if shell == "bash" else "ps1"
    source = (ROOT / f"ops/install-shim-autostart.{suffix}").read_text(encoding="utf-8")
    marker = "# >>> shim cutover >>>"
    if marker in source:
        block = source.split(marker, 1)[1].split("# <<< shim cutover <<<", 1)[0]
    else:
        start = 'echo "Cutover env' if shell == "bash" else 'Write-Host "Cutover env'
        block = start + source.split(start, 1)[1]
    labels = {"com.docker.compose.project": "example-stack",
              "com.docker.compose.project.config_files": "/stacks/example/compose.yml"}
    docker = tmp_path / ("docker.cmd" if os.name == "nt" else "docker")
    docker.write_text(("@echo off\necho " if os.name == "nt" else "#!/bin/sh\nprintf '%s\\n' '")
                      + json.dumps(labels) + ("\n" if os.name == "nt" else "'\n"), encoding="utf-8")
    docker.chmod(0o755)
    if shell == "bash":
        import shlex
        setup = f"repo={shlex.quote(ROOT.as_posix())}\nPYTHON_EXE={shlex.quote(Path(sys.executable).as_posix())}\nPORT=8099\n"
    else:
        q = lambda p: str(p).replace("'", "''")
        setup = f"$repo='{q(ROOT)}'\n$PythonExe='{q(sys.executable)}'\n$Port=8099\n"
    script = tmp_path / f"cutover.{suffix}"
    script.write_text(setup + block, encoding="utf-8")
    args = [executable, script.as_posix()] if shell == "bash" else [
        executable, "-NoProfile", "-NonInteractive", "-File", str(script)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, PSEUDOLIFE_DOCKER=str(docker)))
    assert result.returncode == 0, result.stderr
    assert "stack manager" in result.stdout
    assert "ops/.env" not in result.stdout and "ops/update.sh" not in result.stdout
    assert "http://host.docker.internal:8099/v1" in result.stdout
