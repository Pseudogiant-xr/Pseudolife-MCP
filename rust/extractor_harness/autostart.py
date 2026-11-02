"""Installed native extractor proof on a dev machine, using only stub CLIs.

Registers unique temporary tasks/units, verifies replacement and config reload,
then removes precisely those registrations. Never run on the maintainer's PC.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import run


def checked(argv, **kwargs):
    p = subprocess.run(argv, capture_output=True, **kwargs)
    assert p.returncode == 0, (argv, p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace"))
    return p


def windows_cleanup(task, native, repo):
    script = r'''
param($Task,$Native,$Repo)
Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
$pattern = [regex]::Escape($Repo)
$owned = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $Native -and $_.CommandLine -match $pattern })
foreach ($old in $owned) {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($old.ProcessId)"
    if ($current -and $current.CreationDate -eq $old.CreationDate) { Stop-Process -Id $old.ProcessId -Force }
}
if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) { throw 'fixture task survived cleanup' }
'''
    path = repo / "cleanup.ps1"
    path.write_text(script)
    checked(["pwsh", "-NoProfile", "-File", str(path), task, str(native), str(repo)])



def wait_closed(port):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(.1)
    raise AssertionError("uninstall left its listener alive")


def legacy_windows(native, root, kind, cli, env):
    """Remove a real, uniquely named task whose Python runner detaches."""
    repo = root / (kind + " legacy checkout")
    (repo / "ops").mkdir(parents=True)
    (repo / "evals/prompts").mkdir(parents=True)
    for relative in ("ops/shim_autostart.py", f"evals/{kind}_shim.py",
                     "evals/prompts/assistant_facts_provenance.txt", "evals/prompts/sonnet_extractor_v5.md"):
        shutil.copyfile(run.ROOT / relative, repo / relative)
    port = run.unused_port()
    values = {"python": sys.executable, "cli": str(cli), "host": "127.0.0.1", "port": str(port),
              "log": str(repo / "fixture.log"), "prompt_file": str(repo / "evals/prompts/sonnet_extractor_v5.md")}
    run.autostart_oracle.write_config(repo, kind, values)
    name = "r6-legacy-" + uuid.uuid4().hex[:12]
    config = root / f"{kind}-legacy.json"
    config.write_text(json.dumps({"Name": name, "Python": sys.executable, "Repo": str(repo), "Kind": kind}))
    helper = root / "legacy-task.ps1"
    helper.write_text(r'''
param($ConfigPath, [switch]$Cleanup)
$ErrorActionPreference='Stop'
$c=Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
if ($Cleanup) {
    Stop-ScheduledTask -TaskName $c.Name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $c.Name -Confirm:$false -ErrorAction SilentlyContinue
    $pattern=[regex]::Escape((Join-Path $c.Repo "evals/$($c.Kind)_shim.py"))
    foreach ($old in @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $pattern })) {
        $current=Get-CimInstance Win32_Process -Filter "ProcessId = $($old.ProcessId)"
        if ($current -and $current.CreationDate -eq $old.CreationDate) { Stop-Process -Id $old.ProcessId -Force }
    }
    return
}
$runner=Join-Path $c.Repo 'ops/shim_autostart.py'
$inner=@"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = '$($c.Python.Replace("'", "''"))'
`$psi.Arguments = '"$($runner.Replace("'", "''"))" run $($c.Kind)'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
[System.Diagnostics.Process]::Start(`$psi) | Out-Null
"@
$encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType S4U
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $c.Name -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $c.Name
''', encoding="utf-8")
    try:
        checked(["pwsh", "-NoProfile", "-File", str(helper), str(config)], env=env)
        deadline = time.monotonic() + 25
        while True:
            try:
                assert run.call(port, "GET", "/models")[0] == 200
                break
            except OSError:
                assert time.monotonic() < deadline, "legacy stub shim did not bind"
                time.sleep(.1)
        # The last-start record must still identify the old listener after an edit.
        run.autostart_oracle.write_config(repo, kind, {"port": str(run.unused_port())})
        checked([str(native), "extractor-shim", "uninstall-autostart", kind, "--name", name], env=env)
        wait_closed(port)
        print(f"{kind}: detached Python task cleanup passed", flush=True)
    finally:
        checked(["pwsh", "-NoProfile", "-File", str(helper), str(config), "-Cleanup"], env=env)
        run.autostart_oracle.pid_file(kind, repo).unlink(missing_ok=True)


def wrapper_forwarding(root):
    stub = root / "argv.ps1"
    stub.write_text('param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Rest)\n'
                    'ConvertTo-Json -Compress -InputObject @($Rest)\n$global:LASTEXITCODE=0\n')
    output = checked(["pwsh", "-NoProfile", "-File", str(run.ROOT / "ops/install-shim-autostart.ps1"),
                      "-NativeExe", str(stub), "-NativeTaskName", "r6-argv-only", "-StartupTimeoutSec", "321"])
    args = json.loads(output.stdout)
    assert args[args.index("--startup-timeout") + 1] == "321", args


def proof(binary, root):
    windows = os.name == "nt"
    if windows:
        wrapper_forwarding(root)
    repo = root / "settings with spaces"
    repo.mkdir()
    bin_dir = root / "bin"
    bin_dir.mkdir()
    native = bin_dir / ("pseudolife-mcp.exe" if windows else "pseudolife-mcp")
    shutil.copy2(binary, native)
    launcher = bin_dir / ("launcher.exe" if windows else "launcher")
    assert shutil.disk_usage(root).free >= 8 * 1024**3
    checked(["rustc", "--edition=2024", str(Path(__file__).with_name("launcher.rs")), "-o", str(launcher)])
    launcher.with_suffix(".target").write_text(str(native))
    env = os.environ.copy()
    # Registration is real, but providers are always pinned to the fixture.
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    for k in list(env):
        if k.startswith(("PSEUDOLIFE_", "CLAUDE_", "CODEX_")) or k in ("CLAUDECODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            env.pop(k)
    if not windows:
        (bin_dir / "ip").write_text("#!/bin/sh\nprintf '    inet 127.0.0.1/8 scope host docker0\\n'\n")
        (bin_dir / "ip").chmod(0o700)
    fixture = None
    if windows:
        assert shutil.disk_usage(root).free >= 8 * 1024**3
        fixture = root / "fixture.exe"
        checked(["rustc", "--edition=2024", str(Path(__file__).with_name("stub.rs")), "-o", str(fixture)])
    for kind in ("claude", "codex"):
        token = "r6-" + uuid.uuid4().hex[:12] + "-" + kind
        plan, journal = root / f"{kind}-plan.json", root / f"{kind}-journal.jsonl"
        plan.write_text(json.dumps(run.canned(kind, "OK")))
        stub_source = "import os\nos.environ['R6_PLAN']=" + repr(str(plan)) + "\nos.environ['R6_JOURNAL']=" + repr(str(journal)) + "\n" + run.STUB
        cli = bin_dir / (kind + (".exe" if windows else ""))
        if windows:
            shutil.copyfile(fixture, cli)
            script = bin_dir / f"{kind}-stub.py"
            script.write_text(stub_source)
            cli.with_suffix(".python").write_text(sys.executable)
            cli.with_suffix(".script").write_text(str(script))
        else:
            cli.write_text(f"#!{sys.executable}\n" + stub_source)
            cli.chmod(0o700)
        if windows:
            legacy_windows(native, root, kind, cli, env)
        port = run.unused_port()
        model = "claude-future" if kind == "claude" else "gpt-future"
        registration = token if windows else token + ".service"
        entry = native if kind == "claude" else launcher
        args = [str(entry), "extractor-shim", "install-autostart", kind, "--native-exe", str(entry),
                "--env-file", str(repo / "stable config.env"), "--name", registration,
                "--cli", str(cli), "--host", "127.0.0.1", "--port", str(port),
                "--log", str(root / f"{kind}.log"), "--startup-timeout", "25"]
        if windows:
            args += ["--task-logon-type", "S4U"]
        model_flag = "--model"
        try:
            with socket.socket() as blocker:
                blocker.bind(("127.0.0.1", port))
                blocker.listen()
                refused = subprocess.run(args, env=env, capture_output=True)
                assert refused.returncode != 0 and b"another process" in refused.stdout + refused.stderr
                assert not (repo / "stable config.env").exists() or kind == "codex"
            print(f"{kind}: registering owned fixture {token}", flush=True)
            checked(args, env=env)
            status, body = run.call(port, "POST", "/chat/completions", {})
            assert status == 200, (status, body)
            assert Path(run.journal_rows(journal)[-1]["cwd"]).resolve() == repo.resolve()
            initial_calls = len(run.journal_rows(journal))
            checked(args + [model_flag, model], env=env)
            status, body = run.call(port, "GET", "/models")
            assert status == 200 and json.loads(body)["data"][0]["id"] == model
            assert len(run.journal_rows(journal)) > initial_calls, "replacement never warmed the new process"
            status, body = run.call(port, "POST", "/chat/completions", {"messages": [{"role": "user", "content": "proof"}]})
            assert status == 200 and json.loads(body)["model"] == model
            print(f"{kind}: installed launch, replacement, config reload and completion passed", flush=True)
        except Exception:
            if windows:
                diagnostic = subprocess.run(["pwsh", "-NoProfile", "-Command", f"(Get-ScheduledTaskInfo -TaskName '{token}').LastTaskResult"], capture_output=True)
                print("fixture task LastTaskResult:", diagnostic.stdout.decode(errors="replace"), flush=True)
            log = root / f"{kind}.log"
            print("fixture log:", log.read_text(errors="replace") if log.exists() else "no log", flush=True)
            raise
        finally:
            try:
                checked([str(native), "extractor-shim", "uninstall-autostart", kind, "--name", registration], env=env)
                wait_closed(port)
            finally:
                if windows:
                    windows_cleanup(token, native, repo)
                else:
                    subprocess.run(["systemctl", "--user", "disable", "--now", token + ".service"], capture_output=True)
                    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
                    (unit_dir / (token + ".service")).unlink(missing_ok=True)
                    checked(["systemctl", "--user", "daemon-reload"])
        print(f"{kind}: owned registration removed", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", required=True, type=Path)
    args = p.parse_args()
    with tempfile.TemporaryDirectory(prefix="r6-autostart-") as directory:
        proof(args.binary.resolve(), Path(directory))


if __name__ == "__main__":
    main()
