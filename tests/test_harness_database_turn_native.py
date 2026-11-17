"""Real process/handle probes: run on a dev machine, never this PC."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import time

import psutil
import pytest

from tests.client_environment import isolate_client_environment

ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
runpy.run_path(str(ROOT / "tests/windowless_children.py"))["install"]()


def environment(home):
    home.mkdir(exist_ok=True)
    secret = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|DSN|DATABASE_URL", re.I)
    env = {name: value for name, value in os.environ.items()
           if not secret.search(name) and not name.startswith(("PYTEST_", "PYTHON"))}
    isolate_client_environment(env, home / "codex")
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(ROOT))
    return env


def marker(home):
    locks = home / ".pseudolife-mcp/locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "test-postgres.turn-enabled").touch()
    return locks / "test-postgres.lock"


@contextmanager
def live_holder(tmp_path, env):
    ready = tmp_path / "holder-ready"
    script = ("from pathlib import Path\nimport json,os,psutil,sys,time\n"
              "from rust.harness_support.database_turn import ensure_turn\n"
              "lease=ensure_turn(holder='held native test')\n"
              "ready=Path(sys.argv[1]); temporary=ready.with_suffix('.writing')\n"
              "temporary.write_text(json.dumps({'pid':os.getpid(),'started':psutil.Process().create_time()})); temporary.replace(ready)\n"
              "time.sleep(90)\n")
    with (tmp_path / "holder.log").open("w", encoding="utf-8") as log:
        owner = subprocess.Popen([sys.executable, "-u", "-c", script, str(ready)],
                                 env=env, stdout=log, stderr=subprocess.STDOUT)
        identity = psutil.Process(owner.pid).create_time()
        holder = None
        holder_identity = None
        try:
            deadline = time.monotonic() + 30
            while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert ready.exists(), (tmp_path / "holder.log").read_text(encoding="utf-8")
            holder_identity = json.loads(ready.read_text())
            holder = psutil.Process(holder_identity["pid"])
            assert holder.create_time() == holder_identity["started"]
            assert holder.pid == owner.pid or owner.pid in [parent.pid for parent in holder.parents()]
            yield holder  # Windows' venv launcher is a different process.
        finally:
            if holder is not None and holder.is_running():
                assert holder.create_time() == holder_identity["started"]
                holder.terminate()
                holder.wait(timeout=15)
            if owner.poll() is None:
                assert psutil.Process(owner.pid).create_time() == identity
                owner.terminate()  # This owned Popen handle, never a name/pattern.
                owner.wait(timeout=15)


def child(env, code, *args):
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=30)


def test_unconfigured_child_keeps_old_behavior_without_importing_engine(tmp_path):
    env = environment(tmp_path / "home")
    run = child(env, "import sys; from rust.harness_support.database_turn import ensure_turn; "
                     "assert ensure_turn() is None; "
                     "assert 'rust.harness_support._database_turn' not in sys.modules; print('unchanged')")
    assert run.returncode == 0, run.stderr
    assert "unchanged" in run.stdout


def test_native_sibling_holder_excludes_another_run_even_with_a_claimed_flag(tmp_path):
    home = tmp_path / "home"
    env = environment(home)
    marker(home)
    with live_holder(tmp_path, env) as owner:
        run = child(dict(env, PSEUDOLIFE_DATABASE_TURN_HELD="1"),
                    "from rust.harness_support.database_turn import require_turn; require_turn()")
        assert run.returncode != 0
        assert "held native test" in run.stderr and str(owner.pid) in run.stderr
        assert "held for" in run.stderr and "bounded wait" in run.stderr
    resumed = child(env, "from rust.harness_support.database_turn import require_turn; "
                         "lease=require_turn(); assert not lease.delegated; lease.close()")
    assert resumed.returncode == 0, resumed.stderr


@pytest.mark.parametrize("corrupt", [False, True])
def test_native_child_reuses_only_the_real_parent_identity(tmp_path, corrupt):
    home = tmp_path / "home"
    env = environment(home)
    lock = marker(home)
    script = """
import json, subprocess, sys
from rust.harness_support.database_turn import ensure_turn
lease = ensure_turn(holder='native parent')
if sys.argv[2] == 'corrupt':
    lease.stream.seek(0)
    record = json.loads(lease.stream.read().decode())
    record['process_started'] = str(int(record['process_started']) + 1)
    if 'process_start_ticks' in record:
        record['process_start_ticks'] = record['process_started']
    lease.stream.seek(0)
    lease.stream.truncate()
    lease.stream.write(json.dumps(record).encode())
    lease.stream.flush()
code = 'from rust.harness_support.database_turn import require_turn; lease=require_turn(); assert lease.delegated; lease.close()'
result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)
if sys.argv[2] == 'corrupt':
    assert result.returncode != 0
    assert 'could not be verified' in result.stderr
else:
    assert result.returncode == 0, result.stderr
lease.close()
"""
    run = child(env, script, lock, "corrupt" if corrupt else "valid")
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("strict", [False, True])
def test_rust_pytest_entry_enforces_the_turn_without_requiring_the_database_option(tmp_path, strict):
    home = tmp_path / "home"
    env = environment(home)
    marker(home)
    repo = tmp_path / "repo"
    (repo / "rust").mkdir(parents=True)
    (repo / "tests").mkdir()
    shutil.copyfile(ROOT / "rust/conftest.py", repo / "rust/conftest.py")
    shutil.copyfile(ROOT / "tests/windowless_children.py", repo / "tests/windowless_children.py")
    (repo / "rust/test_entry.py").write_text("def test_bare_run():\n    assert True\n", encoding="utf-8")
    command = [sys.executable, "-m", "pytest", "-q", str(repo / "rust/test_entry.py")]
    if strict:
        command.append("--require-databases")
    with live_holder(tmp_path, env):
        run = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert run.returncode == 4, run.stdout + run.stderr  # pytest UsageError.
        assert "held native test" in run.stderr and "database turn" in run.stderr
    resumed = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert resumed.returncode == 0 and "1 passed" in resumed.stdout, resumed.stdout + resumed.stderr


@pytest.mark.skipif(PWSH is None, reason="PowerShell interoperation")
def test_powershell_holder_and_python_child_share_the_exact_turn_identity(tmp_path):
    home = tmp_path / "home"
    env = environment(home)
    lock = marker(home)
    probe = tmp_path / "probe.py"
    probe.write_text("""
import json, sys
from pathlib import Path
from rust.harness_support.database_turn import ensure_turn
path = Path(sys.argv[1])
before = json.loads(path.read_text(encoding='utf-8-sig'))
lease = ensure_turn(holder='gate child', contain=True)
assert lease.delegated
lease.close()
assert json.loads(path.read_text(encoding='utf-8-sig')) == before
""", encoding="utf-8")
    script = tmp_path / "parent.ps1"
    script.write_text("""
param($helper, $lock, $python, $probe)
. $helper
$hold = Enter-DevDatabaseLock -Path $lock -Holder 'PowerShell parent' -WaitSeconds 0
try { & $python $probe $lock; $result = $LASTEXITCODE }
finally { Exit-DevDatabaseLock $hold }
exit $result
""", encoding="utf-8")
    run = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script),
                          str(ROOT / "ops/dev-database-lock.ps1"), str(lock), sys.executable, str(probe)],
                         env=env, capture_output=True, text=True, timeout=45)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(PWSH is None, reason="PowerShell interoperation")
def test_python_holder_excludes_the_powershell_worker_wrapper(tmp_path):
    home = tmp_path / "home"
    env = environment(home)
    lock = marker(home)
    with live_holder(tmp_path, env):
        run = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File",
                              str(ROOT / "ops/dev-database-lock.ps1"), "-LockPath", str(lock),
                              "-WaitSeconds", "0", "--", sys.executable, "-c", "pass"],
                             env=env, capture_output=True, text=True, timeout=30)
        assert run.returncode == 3 and "held native test" in run.stderr, run.stdout + run.stderr


@pytest.mark.skipif(os.name != "nt" or PWSH is None, reason="Windows job containment")
def test_dot_sourced_lock_does_not_capture_unrelated_interactive_children(tmp_path):
    env = environment(tmp_path / "home")
    record = tmp_path / "child.json"
    script = tmp_path / "dot-source.ps1"
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import json,os,psutil,sys,time\nfrom pathlib import Path\n"
                       "ready=Path(sys.argv[1]); temporary=ready.with_suffix('.writing')\n"
                       "temporary.write_text(json.dumps({'pid':os.getpid(),'started':psutil.Process().create_time()})); temporary.replace(ready)\n"
                       "time.sleep(90)\n", encoding="utf-8")
    script.write_text("""
param($helper, $lock, $python, $sleeper, $record)
. $helper
$hold = Enter-DevDatabaseLock -Path $lock -Holder 'interactive test' -WaitSeconds 0
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $python
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.ArgumentList.Add($sleeper)
$start.ArgumentList.Add($record)
$child = [Diagnostics.Process]::Start($start)
$deadline = [DateTimeOffset]::UtcNow.AddSeconds(20)
while (-not (Test-Path -LiteralPath $record) -and [DateTimeOffset]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 50 }
Exit-DevDatabaseLock $hold
if (-not (Test-Path -LiteralPath $record)) { exit 2 }
exit 0
""", encoding="utf-8")
    process = None
    identity = None
    output = tmp_path / "dot-source.log"
    try:
        # A surviving child's inherited handles must not make communicate()
        # wait for pipe EOF after the interactive parent's process has exited.
        with output.open("w", encoding="utf-8") as log:
            run = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script),
                                  str(ROOT / "ops/dev-database-lock.ps1"), str(tmp_path / "own.lock"),
                                  sys.executable, str(sleeper), str(record)], env=env,
                                 stdout=log, stderr=log, timeout=40)
        assert run.returncode == 0, output.read_text(encoding="utf-8")
        identity = json.loads(record.read_text())
        process = psutil.Process(identity["pid"])
        assert process.create_time() == identity["started"] and process.is_running()
    finally:
        if identity is None and record.exists():
            identity = json.loads(record.read_text())
        if identity:
            try:
                process = psutil.Process(identity["pid"])
                if process.create_time() == identity["started"]:
                    process.terminate()
                    process.wait(timeout=15)
            except psutil.NoSuchProcess:
                pass
