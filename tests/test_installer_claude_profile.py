"""Installer notices explain Claude's per-profile MCP registration scope."""
import os
from pathlib import Path
import shutil
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
@pytest.mark.parametrize("profile", ["default", "secondary", "failed"])
def test_registration_notice_names_only_the_selected_profile(tmp_path, shell, profile):
    executable = shutil.which(shell)
    if shell == "bash" and os.name == "nt":
        git = shutil.which("git")
        executable = str(Path(git).parents[1] / "bin" / "bash.exe") if git else None
    if executable is None:
        pytest.skip(f"{shell} not installed")
    suffix = "sh" if shell == "bash" else "ps1"
    source = (ROOT / f"ops/install.{suffix}").read_text(encoding="utf-8")
    begin, end = "# >>> claude profile scope >>>", "# <<< claude profile scope <<<"
    # The old installer has no notice; execute its empty epilogue to observe
    # the missing user-facing result before adding the marked block.
    block = source.split(begin, 1)[1].split(end, 1)[0] if begin in source else ""
    home = tmp_path / "home"
    selected = home / ".claude-b"
    env = dict(os.environ, HOME=home.as_posix(), USERPROFILE=home.as_posix())
    env.pop("CLAUDE_CONFIG_DIR", None)
    if profile != "default":
        env["CLAUDE_CONFIG_DIR"] = selected.as_posix()
    state = "failed" if profile == "failed" else "shim-env"
    setup = (f"HOME={shlex.quote(home.as_posix())}\nMCP_CLAUDE={state}\n" if shell == "bash"
             else f"$mcpState = @{{claude='{state}'}}\n")
    script = tmp_path / f"scope.{suffix}"
    script.write_text(setup + block, encoding="utf-8")
    args = [executable, script.as_posix()] if shell == "bash" else [
        executable, "-NoProfile", "-NonInteractive", "-File", str(script)]
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    if profile == "failed":
        assert "MCP registration file:" not in result.stdout
        return
    target = (home if profile == "default" else selected) / ".claude.json"
    assert str(target).replace("\\", "/") in result.stdout.replace("\\", "/")
    assert "MCP registration is per-profile" in result.stdout
    assert "shared plugins or settings do not share memory tools" in result.stdout
    assert "rerun this installer" in result.stdout
    assert "CLAUDE_CONFIG_DIR" in result.stdout
