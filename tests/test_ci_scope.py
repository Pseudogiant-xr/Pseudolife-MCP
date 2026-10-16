"""The CI ``scope`` job's lanes, and the Console lane's test list.

``.github/workflows/ci.yml`` skips the four Python suite lanes for a pull
request whose net change is Rust-only (2026-10-09) or Console-only
(2026-10-16). These tests run the job's own classification script on
representative file lists, so a widened allowlist cannot quietly skip the
suite for a change that touches the daemon. They also keep the
``console-guards`` job's file list in step with every test file that reads
the Console source or its committed bundle.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", _workflow(),
                      re.MULTILINE | re.DOTALL)
    assert match, f"ci.yml has no {name!r} job"
    return match.group(1)


def _classify_script() -> str:
    lines = _job("scope").splitlines()
    start = next(i for i, line in enumerate(lines) if "name: Classify the change" in line)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body)


def _bash() -> str:
    # Windows' bash.exe on PATH is usually WSL's, which cannot see this
    # checkout's temp files the same way; the script only ever runs on an
    # ubuntu runner.
    if os.name == "nt":
        pytest.skip("the scope script runs on an ubuntu runner; no POSIX bash here")
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash on PATH")
    return bash


def _classify(files: list[str], tmp_path: Path, event: str = "pull_request") -> dict[str, str]:
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    # `git diff` answers with the given file list, as the merge commit's
    # diff against its base parent would.
    stub = "git() { [ -n \"$PL_FILES\" ] && printf '%s\\n' \"$PL_FILES\"; return 0; }\n"
    env = {**os.environ, "EVENT_NAME": event, "GITHUB_OUTPUT": str(out), "PL_FILES": "\n".join(files)}
    # GitHub runs a `shell: bash` step as `bash --noprofile --norc -eo pipefail`.
    subprocess.run([_bash(), "--noprofile", "--norc", "-eo", "pipefail", "-c", stub + _classify_script()],
                   env=env, check=True, capture_output=True, text=True, timeout=60)
    return dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)


CASES = [
    # Rust-only, unchanged from 2026-10-09.
    (["rust/src/main.rs"], "rust"),
    (["rust/Cargo.toml", ".github/workflows/rust.yml", "CHANGELOG.md"], "rust"),
    # Console-only.
    (["frontend/src/components/Topbar.svelte"], "console"),
    (["frontend/src/lib/dreamChip.ts", "pseudolife_memory/web/static/index.html",
      "pseudolife_memory/web/static/assets/index-abc.js", "CHANGELOG.md"], "console"),
    (["frontend/package.json", "changelog.d/2026-10-16-x.md"], "console"),
    # Everything else runs the full suite.
    (["pseudolife_memory/web/static/index.html"], "full"),  # a bundle with no source change
    (["CHANGELOG.md"], "full"),
    (["changelog.d/2026-10-16-x.md"], "full"),
    (["frontend/src/App.svelte", "pseudolife_memory/web/routes.py"], "full"),
    (["frontend/src/App.svelte", "tests/test_web.py"], "full"),
    (["frontend/src/App.svelte", ".github/workflows/ci.yml"], "full"),
    (["frontend/src/App.svelte", "rust/src/main.rs"], "full"),
    (["pseudolife_memory/service.py"], "full"),
    ([], "full"),
]


@pytest.mark.parametrize(("files", "lane"), CASES)
def test_the_scope_job_picks_the_lane(files, lane, tmp_path):
    result = _classify(files, tmp_path)
    assert result == {"lane": lane, "python": "true" if lane == "full" else "false"}, files


def test_a_push_runs_the_full_suite(tmp_path):
    assert _classify(["frontend/src/App.svelte"], tmp_path, event="push") == {"lane": "full", "python": "true"}


def test_the_suite_lanes_skip_only_on_an_affirmative_scope():
    for job in ("test", "test-lite-linux", "test-lite-windows", "test-lite-macos"):
        assert "needs.scope.outputs.python != 'false'" in _job(job), job
    assert "needs.scope.outputs.lane == 'console'" in _job("console-guards")


# What a test file looks like when it reads the Console source or its build.
_READS_CONSOLE = re.compile(
    r'/ "frontend"|"frontend" /|"frontend/|_read\("frontend|"web" / "static"|web/static|"static" /')


def test_console_guards_runs_every_test_that_reads_the_console():
    readers = {
        p.name for p in (REPO / "tests").glob("test_*.py")
        if p.name != Path(__file__).name and _READS_CONSOLE.search(p.read_text(encoding="utf-8"))
    }
    listed = set(re.findall(r"tests/(test_[a-z0-9_]+\.py)", _job("console-guards")))
    assert readers, "no test reads the Console source; the scan pattern is stale"
    missing = readers - listed
    assert not missing, (
        f"{sorted(missing)} read frontend/ or the committed bundle but the console-guards job "
        f"does not run them, so a Console-only PR would merge without them")
    assert Path(__file__).name in listed
