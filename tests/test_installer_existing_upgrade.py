"""Executable contracts for upgrading shims behind existing registrations.

The fixtures run only the installer client-wiring loop with fake MCP clients and
package managers.  They never touch live registrations or installed packages.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLIENTS = ("codex", "claude", "gemini")
VISIBLE_ENV_CLIENTS = ("codex", "claude")
TRUTHY_NO_SPAWN = ("1", "true", "yes", "on")
FALSEY_NO_SPAWN = ("0", "false", "", "*****")


def _fixture_env(tmp_path: Path) -> dict[str, str]:
    """Build a credential-free environment for the disposable client harness."""
    env = {
        name: os.environ[name]
        for name in (
            "COMSPEC", "LANG", "LC_ALL", "PATHEXT", "SystemRoot", "TEMP",
            "TERM", "TMP", "TMPDIR", "WINDIR",
        )
        if name in os.environ
    }
    fixture_home = tmp_path / "home"
    fixture_home.mkdir(parents=True, exist_ok=True)
    shim = fixture_home / "shim"
    env.update({
        "CODEX_HOME": str(fixture_home / "codex"),
        "HOME": str(fixture_home),
        "USERPROFILE": str(fixture_home),
        "XDG_CONFIG_HOME": str(fixture_home / "config"),
        "LOCALAPPDATA": str(fixture_home / "AppData" / "Local"),
        "APPDATA": str(fixture_home / "AppData" / "Roaming"),
        # Both shim layout overrides, inside the fixture home: with both set,
        # `ops/shim_runtime.py expose` puts the launcher on no PATH
        # (pseudolife_memory/runtimes.py default_layout). Without them, a
        # whole-installer run that reaches the real runtime install writes
        # the real HKCU\Environment Path on Windows, whatever USERPROFILE
        # says, since the registry is not under the fixture home.
        "PSEUDOLIFE_SHIM_RUNTIMES": str(shim / "runtimes"),
        "PSEUDOLIFE_SHIM_LAUNCHER": str(
            shim / "bin" / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")),
    })
    return env


def _inside(entry: str, home: str) -> bool:
    """Whether a user PATH entry names ``home`` or a path under it, in
    Windows terms (case-insensitive, either separator, %VARS% expanded)."""
    import ntpath

    def norm(path: str) -> str:
        return ntpath.normcase(ntpath.normpath(ntpath.expandvars(path)))

    if not entry.strip():
        return False
    entry, home = norm(entry), norm(home)
    return entry == home or entry.startswith(home.rstrip("\\") + "\\")


@contextlib.contextmanager
def user_path_unchanged(fixture_home, broadcast=None):
    """On Windows, fail when the real user PATH (HKCU\\Environment Path)
    changed during the block; elsewhere a no-op. A whole-installer test wraps
    its run in it, because a fixture home does not contain the registry.

    It changes the value back only on the leak's own signature: the new value
    is the old one plus entries inside ``fixture_home``, none removed. Then it
    removes exactly those entries from the value as it is now, announces the
    change (WM_SETTINGCHANGE, as the leaking write did) and fails. Any other
    difference may be someone else's legitimate change on a shared machine
    (another session, a real installer run): it is left as it is, and the test
    fails naming the entries added and removed, so a person can look."""
    if os.name != "nt":
        yield
        return
    import winreg

    if broadcast is None:
        from pseudolife_memory.runtimes import _broadcast_environment_change as broadcast
    home = str(fixture_home)

    def read():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try:
                return winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return None

    def entries(value):
        return [] if value is None else str(value[0]).split(";")

    def difference(old, new):
        remaining = list(old)
        added = []
        for entry in new:
            if entry in remaining:
                remaining.remove(entry)
            else:
                added.append(entry)
        return added, remaining   # remaining: entries the new value lost

    before = read()
    try:
        yield
    finally:
        after = read()
        if after != before:
            added, removed = difference(entries(before), entries(after))
            leak = (bool(added) and not removed and all(_inside(entry, home) for entry in added)
                    and (before is None or after[1] == before[1]))
            if not leak:
                raise AssertionError(
                    "the real user PATH changed during the test in a way this test did not cause "
                    f"(nothing was changed back): added {added!r}, removed {removed!r}")
            current = read()   # the value as it is now, which may have moved since
            kept = [entry for entry in entries(current) if entry not in added]
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
                if before is None and not [entry for entry in kept if entry]:
                    winreg.DeleteValue(key, "Path")
                else:
                    winreg.SetValueEx(key, "Path", 0, (current or after)[1], ";".join(kept))
            broadcast()
            raise AssertionError(f"the test put {added!r} on the real user PATH; removed them again")

def _bash_fixture_path(
    bash: str, path: Path, env: dict[str, str],
) -> str:
    """Return the path spelling understood by the selected Bash runtime."""
    if os.name != "nt":
        return str(path)
    converted = subprocess.run(
        [bash, "-c", 'cygpath -u -- "$1"', "fixture", str(path)],
        capture_output=True, check=True, text=True, timeout=5,
        env=env,
    )
    return converted.stdout.strip()


def _managed_config(client: str, no_spawn: str = "1") -> str:
    if client == "gemini":
        return "pseudolife-memory: pseudolife-mcp (stdio) - Connected"
    if client == "codex":
        return json.dumps({
            "name": "pseudolife-memory",
            "transport": {
                "type": "stdio",
                "command": "pseudolife-mcp",
                "args": [],
                "env": {"PSEUDOLIFE_MCP_NO_SPAWN": no_spawn},
            },
        }, indent=2)
    return (
        "Type: stdio\nCommand: pseudolife-mcp\nEnvironment:\n"
        f"  PSEUDOLIFE_MCP_NO_SPAWN={no_spawn}\n"
        "  PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765"
    )


def _custom_config(client: str, command: str, no_spawn: str | None = None) -> str:
    if client == "gemini":
        return f"pseudolife-memory: {command} (stdio) - Connected"
    env = ("" if no_spawn is None else
           f"\nEnvironment:\n  PSEUDOLIFE_MCP_NO_SPAWN={no_spawn}")
    return f"transport: stdio\ncommand: {command}\nargs: -m private_bridge{env}"


def _http_config(client: str) -> str:
    if client == "gemini":
        return "pseudolife-memory: http://127.0.0.1:8765/mcp (http) - Connected"
    if client == "codex":
        return json.dumps({
            "name": "pseudolife-memory",
            "transport": {
                "type": "streamable_http",
                "url": "http://127.0.0.1:8765/mcp",
            },
        }, indent=2)
    return (
        "Type: http\nURL: http://127.0.0.1:8765/mcp"
    )


def _lookalike_guard_config(client: str) -> str:
    if client == "codex":
        return json.dumps({
            "name": "pseudolife-memory",
            "transport": {
                "type": "stdio",
                "command": "pseudolife-mcp",
                "args": ["--label=PSEUDOLIFE_MCP_NO_SPAWN=1"],
                "env": {"OTHER_PSEUDOLIFE_MCP_NO_SPAWN": "1"},
            },
        }, indent=2)
    return (
        "Type: stdio\nCommand: pseudolife-mcp\n"
        "Arguments: --label=PSEUDOLIFE_MCP_NO_SPAWN=1\nEnvironment:\n"
        "  OTHER_PSEUDOLIFE_MCP_NO_SPAWN=1"
    )


def _between(path: str, start: str, end: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    return start + text.split(start, 1)[1].split(end, 1)[0]


def _bash_variants() -> list[str]:
    variants: list[str] = []
    for candidate in (
        shutil.which("bash") if os.name != "nt" else None,
        r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else None,
    ):
        if candidate and Path(candidate).is_file() and candidate not in variants:
            variants.append(candidate)
    return variants


def _run_bash_existing(
    bash: str, tmp_path: Path, *, client: str, config: str, manager_exit: int,
    shadowed_path: bool = False, existing: bool = True, held: bool = False,
    runtime: bool = False, migrate_exit: int = 3,
) -> subprocess.CompletedProcess[bytes]:
    """``held``: the Windows process table shows a session running the
    installed shim; otherwise the harness is not on Windows. ``runtime``: a
    python that installs the side-by-side runtime (its fake creates the
    launcher and answers ``migrate`` with ``migrate_exit``); otherwise the
    pipx fallback is the only shim path."""
    install = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    helper = _between("ops/install.sh", "resolve_installed_shim() {", "\n}\n\n# Two env pairs") + "\n}"
    loop_start = 'for selected_client in $CLIENTS; do\n    if [ "$selected_client" = claude-desktop ]; then'
    loop = _between("ops/install.sh", loop_start, "\ndone\n\n# ── 12. health") + "\ndone"
    describe = _between("ops/install.sh", "describe_mcp() {", "\n}\ndescribe_instr()") + "\n}"
    marker = _between("ops/install.sh", "mcp_marker() {", "\n}\ndescribe_mcp()") + "\n}"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    installed_bin = tmp_path / "installed-bin"
    installed_bin.mkdir()
    installed_shim = installed_bin / "pseudolife-mcp"
    installed_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    installed_shim.chmod(0o755)
    if shadowed_path:
        old_shim = fake_bin / "pseudolife-mcp"
        old_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        old_shim.chmod(0o755)
    call_log = tmp_path / "calls.txt"
    for name in CLIENTS:
        query = "list" if name == "gemini" else "get"
        script = (
            "#!/bin/sh\n"
            "printf '%s|%s\\n' \"$(basename \"$0\")\" \"$*\" >>\"$CALL_LOG\"\n"
            f"if [ \"$1\" = mcp ] && [ \"$2\" = {query} ]; then\n"
            "  if [ \"$FAKE_EXISTING\" = yes ]; then printf '%s\\n' \"$FAKE_CONFIG\"; exit 0; fi\n"
            "  exit 1\n"
            "fi\n"
            "exit 91\n"
        )
        path = fake_bin / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    pipx = fake_bin / "pipx"
    pipx.write_text(
        "#!/bin/sh\n"
        "printf 'pipx|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
        "if [ \"$1\" = environment ]; then printf '%s\\n' \"$FAKE_INSTALL_BIN\"; exit 0; fi\n"
        "exit \"$FAKE_MANAGER_EXIT\"\n",
        encoding="utf-8",
    )
    pipx.chmod(0o755)
    # The runtime python: ops/shim_runtime.py install writes the launcher
    # (the installed shim path) and prints it; launcher --if-installed
    # prints it once it exists; migrate answers with FAKE_MIGRATE_EXIT.
    python3 = fake_bin / "python3"
    python3.write_text(
        "#!/bin/sh\n"
        "printf 'python3|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
        "case \"$1\" in *shim_runtime.py) ;; *) exit 91 ;; esac\n"
        "if [ \"$2\" = install ]; then printf '#!/bin/sh\\nexit 0\\n' >\"$FAKE_LAUNCHER\"; chmod +x \"$FAKE_LAUNCHER\"; fi\n"
        "if [ \"$2\" = install ] || [ \"$2\" = launcher ]; then\n"
        "  if [ -f \"$FAKE_LAUNCHER\" ]; then printf '%s\\n' \"$FAKE_LAUNCHER\"; exit 0; fi\n"
        "  exit 3\n"
        "fi\n"
        "if [ \"$2\" = migrate ]; then exit \"$FAKE_MIGRATE_EXIT\"; fi\n"
        "exit 91\n",
        encoding="utf-8",
    )
    python3.chmod(0o755)
    repo = str(ROOT).replace("'", "'\\''")
    fixture_env = _fixture_env(tmp_path / "bash-env")
    fake_bin_shell = _bash_fixture_path(bash, fake_bin, fixture_env).replace("'", "'\\''")
    installed_bin_shell = _bash_fixture_path(bash, installed_bin, fixture_env).replace("'", "'\\''")
    installed_shim_shell = _bash_fixture_path(bash, installed_shim, fixture_env).replace("'", "'\\''")
    call_log_shell = _bash_fixture_path(bash, call_log, fixture_env).replace("'", "'\\''")
    config_shell = ("pseudolife-memory\n" + config).replace("'", "'\\''")
    path_prefix = f"{fake_bin_shell}:{installed_bin_shell}" if shadowed_path else f"{installed_bin_shell}:{fake_bin_shell}"
    # CRLF rows, as powershell.exe writes them under Git Bash.
    held_table = (
        "shim_process_table() {\n"
        "    printf '4242|1|%s\\r\\n' \"$(cygpath -m \"$fixture_shim\" 2>/dev/null || printf '%s' \"$fixture_shim\")\"\n"
        "}" if held else "shim_process_table() { return 2; }")
    script = f"""set -u
PATH='{path_prefix}:/usr/bin:/bin'
export PATH CALL_LOG='{call_log_shell}'
FAKE_CONFIG='{config_shell}'
fixture_shim=$(cygpath -u '{installed_shim_shell}' 2>/dev/null || printf '%s' '{installed_shim_shell}')
FAKE_CONFIG=${{FAKE_CONFIG//__INSTALLED_SHIM__/$fixture_shim}}
  export FAKE_CONFIG FAKE_MANAGER_EXIT='{manager_exit}' FAKE_INSTALL_BIN='{installed_bin_shell}' FAKE_EXISTING='{"yes" if existing else "no"}'
export FAKE_LAUNCHER='{installed_bin_shell}/pseudolife-mcp' FAKE_MIGRATE_EXIT='{migrate_exit}'
export PSEUDOLIFE_SHIM_RUNTIMES='{installed_bin_shell}/runtimes' PSEUDOLIFE_SHIM_LAUNCHER='{installed_bin_shell}/pseudolife-mcp'
repo='{repo}'
CLIENTS='{client}'
TRANSPORT=shim
CODEX_CREDENTIAL_BOOTSTRAP_FAILED=''
CODEX_CONNECTION_CONFIGURED=''
CODEX_CREDENTIAL_FILE=''
CODEX_CREDENTIAL_URL=''
CODEX_RUNTIME_DEFAULTS=''
MCP_CODEX='' MCP_CLAUDE='' MCP_GEMINI=''
SHIM_TRIED='' SHIM_OK='' SHIM_PATH='{installed_shim_shell}'
step() {{ printf 'STEP: %s\\n' "$*"; }}
configure_codex_runtime_defaults() {{ CODEX_RUNTIME_DEFAULTS=preserved; }}
{helper}
{held_table}
shim_runtime_python() {{ {"echo python3" if runtime else ":"}; }}
{marker}
{describe}
{loop}
case '{client}' in
  codex) state="$MCP_CODEX" ;;
  claude) state="$MCP_CLAUDE" ;;
  gemini) state="$MCP_GEMINI" ;;
esac
printf 'STATE=%s\\n' "$state"
printf 'STATUS=%s %s\\n' "$(mcp_marker "$state")" "$(describe_mcp "$state")"
"""
    return subprocess.run(
        [bash], input=script.encode(), capture_output=True, check=False, timeout=15,
        env=fixture_env,
    )


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_upgrades_managed_shim_without_rewriting_registration(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client),
        manager_exit=0,
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert f"pipx|install --force {ROOT}" in calls
    assert not any(" mcp add" in call or " mcp remove" in call for call in calls)
    assert "STATE=present-upgraded" in output
    assert "[x] already wired; checkout shim upgraded" in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_reports_existing_shim_upgrade_failure_in_status(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client), manager_exit=7,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "[!] registration or shim upgrade FAILED" in output
    assert "registration was preserved" in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_never_calls_a_shim_sessions_run_upgraded(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client), manager_exit=0, held=True,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0, output
    assert not any(call.startswith("pipx|install") for call in calls)
    assert not any(" mcp add" in call or " mcp remove" in call for call in calls)
    assert "1 session is running the pseudolife-mcp shim" in output
    assert "registration was preserved, but its pseudolife-mcp shim was not upgraded" in output
    assert "STATE=failed" in output
    assert "upgraded its pseudolife-mcp shim" not in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_preserves_custom_stdio_registration_and_warns(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_custom_config(client, "/opt/private/pseudolife-mcp"),
        manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert not any(call.startswith("pipx|install ") for call in calls)
    assert "custom registered command" in output
    assert "/opt/private/pseudolife-mcp" not in output
    assert "STATE=failed" in output
    assert "PSEUDOLIFE_MCP_NO_SPAWN=1" in output
    assert "mcp remove pseudolife-memory" not in output
    assert "mcp add pseudolife-memory --env" not in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_reports_shadowed_bare_shim_without_rewriting_registration(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client, config=_managed_config(client),
        manager_exit=0, shadowed_path=True,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "still resolves to a different executable" in output
    assert "put the installed scripts directory first" in output
    assert not any("mcp add" in call or "mcp remove" in call for call in calls)


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_rerun_upgrades_exact_manager_absolute_registration(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_custom_config(client, "__INSTALLED_SHIM__", "1"), manager_exit=0,
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "STATE=present-upgraded" in output
    assert "custom registered command" not in output


@pytest.mark.parametrize("value", TRUTHY_NO_SPAWN)
@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_accepts_only_effective_no_spawn_values(
    bash: str, tmp_path: Path, client: str, value: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client, value), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=present-upgraded" in output
    assert "no-spawn guard is missing or cannot be verified" not in output


@pytest.mark.parametrize("value", FALSEY_NO_SPAWN)
@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_rejects_false_no_spawn_values_without_rewriting(
    bash: str, tmp_path: Path, client: str, value: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client, value), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output
    assert not any("mcp remove" in call for call in calls)
    assert not any("mcp add" in call and "--help" not in call for call in calls)
    assert "mcp remove pseudolife-memory" not in output
    assert "mcp add pseudolife-memory --env" not in output


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_requires_exact_no_spawn_environment_key(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_lookalike_guard_config(client), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output


@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_gemini_checks_only_pseudolife_transport(
    bash: str, tmp_path: Path,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client="gemini",
        config="other-server: http://127.0.0.1:9999/mcp (http) - Connected\n"
        + _managed_config("gemini"),
        manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_http_registration_needs_no_spawn_guard(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client, config=_http_config(client), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=present" in output
    assert "PSEUDOLIFE_MCP_NO_SPAWN" not in output
    assert not any(call.startswith("pipx|install ") for call in calls)


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_flagless_fresh_cli_fails_before_registration(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client, config="", manager_exit=0, existing=False,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no env flag" in output
    assert "registration was skipped" in output
    assert not any("mcp add" in call and "--help" not in call for call in calls)
    assert not any("mcp remove" in call for call in calls)


def _run_powershell_existing(
    tmp_path: Path, *, client: str, config: str, manager_exit: int,
    shadowed_path: bool = False, existing: bool = True, held: bool = False,
    runtime: bool = False, migrate_exit: int = 3,
) -> subprocess.CompletedProcess[bytes]:
    """``held``, ``runtime`` and ``migrate_exit`` as for ``_run_bash_existing``."""
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell is unavailable")
    helper = _between(
        "ops/install.ps1", "function Resolve-InstalledShimPath", "\n}\n\n# Two env pairs"
    ) + "\n}"
    loop_start = 'foreach ($selectedClient in $clients) {\n    if ($selectedClient -eq "claude-desktop") {'
    loop = _between("ops/install.ps1", loop_start, "\n}\n\n# -- 12. health") + "\n}"
    describe = _between("ops/install.ps1", "function Describe-Mcp($state) {", "\n}\nfunction Get-McpMarker") + "\n}"
    marker = _between("ops/install.ps1", "function Get-McpMarker($state) {", "\n}\nfunction Describe-Instr") + "\n}"
    log = tmp_path / "calls.txt"
    installed_bin = tmp_path / "installed-bin"
    installed_bin.mkdir()
    shim_name = "pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp"
    installed_shim = installed_bin / shim_name
    installed_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    installed_shim.chmod(0o755)
    old_bin = tmp_path / "old-bin"
    old_bin.mkdir()
    if shadowed_path:
        old_shim = old_bin / shim_name
        old_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        old_shim.chmod(0o755)
    escaped_log = str(log).replace("'", "''")
    escaped_repo = str(ROOT).replace("'", "''")
    escaped_config = ("pseudolife-memory\n" + config.replace("__INSTALLED_SHIM__", str(installed_shim))).replace("'", "''")
    escaped_installed_shim = str(installed_shim).replace("'", "''")
    escaped_path_bin = str(old_bin if shadowed_path else installed_bin).replace("'", "''")
    held_table = (
        "function Get-ShimProcessTable { @([pscustomobject]@{ ProcessId = 4242; ParentProcessId = 1; "
        f"ExecutablePath = '{escaped_installed_shim}' }}) }}" if held
        else "function Get-ShimProcessTable { $null }")
    function_defs = []
    for name in CLIENTS:
        query = "list" if name == "gemini" else "get"
        function_defs.append(f"""function global:{name} {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{escaped_log}' -Value ('{name}|' + ($callArgs -join ' '))
    if (($callArgs[0] -eq 'mcp') -and ($callArgs[1] -eq '{query}')) {{
        if ($env:FAKE_EXISTING -eq 'yes') {{
            Write-Output $env:FAKE_CONFIG
            $global:LASTEXITCODE = 0
            return
        }}
        $global:LASTEXITCODE = 1
        return
    }}
    $global:LASTEXITCODE = 91
}}""")
    script = f"""$ErrorActionPreference = 'Stop'
$repo = '{escaped_repo}'
$clients = @('{client}')
$Transport = 'shim'
$codexCredentialBootstrapFailed = $false
$codexConnectionConfigured = $false
$codexCredentialFile = $null
$codexCredentialUrl = $null
$codexRuntimeDefaults = $null
$mcpState = @{{}}
$script:shimInstallResult = $null
$script:shimInstallPath = '{escaped_installed_shim}'
$script:shimUpgradeHeld = $null
$script:shimPython = $null
$script:shimManagerKind = $null
$script:shimPipxVenv = $null
$env:PATH = '{escaped_path_bin}' + [IO.Path]::PathSeparator + $env:PATH
$env:FAKE_CONFIG = '{escaped_config}'
$env:FAKE_EXISTING = '{"yes" if existing else "no"}'
$env:FAKE_MANAGER_EXIT = '{manager_exit}'
function Step($message) {{ Write-Output "STEP: $message" }}
function Set-CodexRuntimeDefaults {{ $script:codexRuntimeDefaults = 'preserved' }}
{''.join(function_defs)}
function global:pipx {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{escaped_log}' -Value ('pipx|' + ($callArgs -join ' '))
    if ($callArgs[0] -eq 'environment') {{ Write-Output '{str(installed_bin).replace("'", "''")}'; $global:LASTEXITCODE = 0; return }}
    $global:LASTEXITCODE = [int]$env:FAKE_MANAGER_EXIT
}}
function global:python {{
    # The runtime python: ops/shim_runtime.py install writes the launcher (the
    # installed shim path) and prints it; launcher --if-installed prints it once
    # it exists; migrate answers with FAKE_MIGRATE_EXIT.
    $callArgs = @($args)
    Add-Content -LiteralPath '{escaped_log}' -Value ('python|' + ($callArgs -join ' '))
    if ($callArgs[0] -notlike '*shim_runtime.py') {{ $global:LASTEXITCODE = 91; return }}
    if ($callArgs[1] -eq 'install') {{ Set-Content -LiteralPath '{escaped_installed_shim}' -Value 'fake' }}
    if (($callArgs[1] -eq 'install') -or ($callArgs[1] -eq 'launcher')) {{
        if (Test-Path -LiteralPath '{escaped_installed_shim}') {{ Write-Output '{escaped_installed_shim}'; $global:LASTEXITCODE = 0; return }}
        $global:LASTEXITCODE = 3; return
    }}
    if ($callArgs[1] -eq 'migrate') {{ $global:LASTEXITCODE = {migrate_exit}; return }}
    $global:LASTEXITCODE = 91
}}
$env:PSEUDOLIFE_SHIM_RUNTIMES = '{str(installed_bin / "runtimes").replace("'", "''")}'
$env:PSEUDOLIFE_SHIM_LAUNCHER = '{escaped_installed_shim}'
{helper}
{held_table}
function Get-ShimRuntimePython {{ {"'python'" if runtime else "$null"} }}
{describe}
{marker}
{loop}
$state = $mcpState['{client}']
Write-Output "STATE=$state"
Write-Output ("STATUS=" + (Get-McpMarker $state) + " " + (Describe-Mcp $state))
"""
    runner = tmp_path / "run-existing.ps1"
    runner.write_text(script, encoding="utf-8")
    return subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
        capture_output=True, check=False, timeout=15,
        env=_fixture_env(tmp_path / "powershell-env"),
    )


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_upgrades_managed_shim_without_rewriting_registration(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client),
        manager_exit=0,
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert f"pipx|install --force {ROOT}" in calls
    assert not any("mcp add" in call or "mcp remove" in call for call in calls)
    assert "STATE=present-upgraded" in output
    assert "[x] already wired; checkout shim upgraded" in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_reports_existing_shim_upgrade_failure_in_status(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client), manager_exit=7,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "[!] registration or shim upgrade FAILED" in output
    assert "registration was preserved" in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_never_calls_a_shim_sessions_run_upgraded(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client), manager_exit=0, held=True,
    )
    output = " ".join((proc.stdout + proc.stderr).decode(errors="replace").split())
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0, output
    assert not any(call.startswith("pipx|install") for call in calls)
    assert not any(" mcp add" in call or " mcp remove" in call for call in calls)
    assert "1 session is running the pseudolife-mcp shim" in output
    assert "registration was preserved, but its pseudolife-mcp shim was not upgraded" in output
    assert "STATE=failed" in output
    assert "upgraded its pseudolife-mcp shim" not in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_preserves_custom_stdio_registration_and_warns(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_custom_config(client, r"D:\Private\pseudolife-mcp.exe"),
        manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert not any(call.startswith("pipx|install ") for call in calls)
    assert "custom registered command" in output
    assert r"D:\Private\pseudolife-mcp.exe" not in output
    assert "STATE=failed" in output
    assert "PSEUDOLIFE_MCP_NO_SPAWN=1" in output
    assert "mcp remove pseudolife-memory" not in output
    assert "mcp add pseudolife-memory --env" not in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_reports_shadowed_bare_shim_without_rewriting_registration(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client, config=_managed_config(client), manager_exit=0,
        shadowed_path=True,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "still resolves to a different executable" in output
    assert "put the installed scripts directory first" in output
    assert not any("mcp add" in call or "mcp remove" in call for call in calls)


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_rerun_upgrades_exact_manager_absolute_registration(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_custom_config(client, "__INSTALLED_SHIM__", "1"), manager_exit=0,
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "STATE=present-upgraded" in output
    assert "custom registered command" not in output


@pytest.mark.parametrize("value", TRUTHY_NO_SPAWN)
@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_accepts_only_effective_no_spawn_values(
    tmp_path: Path, client: str, value: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client, value), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=present-upgraded" in output
    assert "no-spawn guard is missing or cannot be verified" not in output


@pytest.mark.parametrize("value", FALSEY_NO_SPAWN)
@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_rejects_false_no_spawn_values_without_rewriting(
    tmp_path: Path, client: str, value: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client, value), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output
    assert not any("mcp remove" in call for call in calls)
    assert not any("mcp add" in call and "--help" not in call for call in calls)
    assert "mcp remove pseudolife-memory" not in output
    assert "mcp add pseudolife-memory --env" not in output


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_requires_exact_no_spawn_environment_key(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_lookalike_guard_config(client), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output


def test_install_ps1_gemini_checks_only_pseudolife_transport(tmp_path: Path) -> None:
    proc = _run_powershell_existing(
        tmp_path, client="gemini",
        config="other-server: http://127.0.0.1:9999/mcp (http) - Connected\n"
        + _managed_config("gemini"),
        manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_http_registration_needs_no_spawn_guard(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client, config=_http_config(client), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=present" in output
    assert "PSEUDOLIFE_MCP_NO_SPAWN" not in output
    assert not any(call.startswith("pipx|install ") for call in calls)


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_flagless_fresh_cli_fails_before_registration(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client, config="", manager_exit=0, existing=False,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no env flag" in output
    assert "registration was skipped" in output
    assert not any("mcp add" in call and "--help" not in call for call in calls)
    assert not any("mcp remove" in call for call in calls)


class _NoRegistry(dict):
    """A user environment that fails any test reaching it."""

    def get(self, *args):
        raise AssertionError("the user PATH was read for exposure")

    def __setitem__(self, *args):
        raise AssertionError("the user PATH was written")


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_the_installer_fixture_environment_exposes_the_shim_on_no_path(tmp_path, windows):
    # The fixture home holds HOME and USERPROFILE, but not the Windows
    # registry: a whole-installer harness built from this environment, whose
    # python runs the real `ops/shim_runtime.py expose`, would otherwise
    # write the developer's real HKCU Environment Path. Every name a default
    # layout or a Windows path is derived from lies inside the fixture home,
    # and both layout overrides are set, so exposure is skipped.
    from pseudolife_memory import runtimes

    env = _fixture_env(tmp_path)
    home = Path(env["HOME"])
    for name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "PSEUDOLIFE_SHIM_RUNTIMES",
                 "PSEUDOLIFE_SHIM_LAUNCHER"):
        assert home == Path(env[name]) or home in Path(env[name]).parents, name
    if windows != (os.name == "nt"):
        suffix = ".exe" if windows else ""
        env["PSEUDOLIFE_SHIM_LAUNCHER"] = str(Path(env["PSEUDOLIFE_SHIM_LAUNCHER"]).with_suffix(suffix))
    layout = runtimes.default_layout(env, windows=windows)
    assert not layout.user_path and layout.user_bin is None
    exposed = runtimes.expose_launcher(layout, env=env, registry=_NoRegistry(),
                                       broadcast=lambda: pytest.fail("broadcast"))
    assert exposed["state"] == "skipped", exposed


def test_the_client_only_whole_installer_harnesses_use_the_fixture_environment():
    # The two tests that run a whole installer with the real interpreter
    # build their environment from _fixture_env and guard the user PATH.
    for name in ("test_installer_client_only.py", "test_installer_client_only_ps.py"):
        source = (ROOT / "tests" / name).read_text(encoding="utf-8")
        assert "self.env = _fixture_env(" in source, name
        start = source.index("def test_a_client_only_install_wires_clients_to_the_remote_daemon(")
        body = source[start:source.find("\ndef ", start + 1)]
        assert "with user_path_unchanged(" in body, name


class _FakeWinreg:
    """Just enough of winreg for user_path_unchanged, over a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2

    def __init__(self, path):
        self.values = {} if path is None else {"Path": path}

    @contextlib.contextmanager
    def OpenKey(self, *args):
        yield self

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = (value, kind)

    def DeleteValue(self, key, name):
        del self.values[name]


HOME = "C:\\Users\\<user>\\AppData\\Local\\Temp\\pytest-1\\env\\home"
BEFORE = ("C:\\tools;%USERPROFILE%\\.local\\bin", 2)


def _guarded(monkeypatch, before):
    """A fake winreg (never the real registry), os.name nt, and a recorder
    of the guard's broadcasts."""
    import sys
    fake = _FakeWinreg(before)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(os, "name", "nt")
    announced = []
    return fake, announced, lambda: user_path_unchanged(HOME, broadcast=lambda: announced.append(1))


@pytest.mark.parametrize("before", [BEFORE, None], ids=["set", "absent"])
def test_the_user_path_guard_takes_back_only_the_tests_own_entries(monkeypatch, before):
    fake, announced, guard = _guarded(monkeypatch, before)
    with guard():
        pass
    assert announced == []
    leaked = HOME + "\\shim\\bin"
    with pytest.raises(AssertionError, match="put .* on the real user PATH; removed them again"):
        with guard():
            # What ensure_user_path writes: the directory first, the rest kept.
            fake.values["Path"] = (f"{leaked};{before[0]}" if before else leaked, 2)
    assert fake.values.get("Path") == before
    # Codex review of faea97a1a: the leak was announced, so the restore is too.
    assert announced == [1]


def test_the_user_path_guard_leaves_someone_elses_change_alone(monkeypatch):
    # The conductor's review of d4daf4358: on a shared PC another session or
    # a real installer may change the user PATH meanwhile. That is not
    # undone; the test fails naming it.
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    changed = ("C:\\someone-else\\bin;" + BEFORE[0], 2)
    with pytest.raises(AssertionError, match="in a way this test did not cause") as raised:
        with guard():
            fake.values["Path"] = changed
    assert "C:\\\\someone-else\\\\bin" in str(raised.value)
    assert fake.values["Path"] == changed and announced == []


@pytest.mark.parametrize("change", ["mixed", "removed", "type"])
def test_the_user_path_guard_touches_nothing_unless_the_change_is_only_the_leak(monkeypatch, change):
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    changed = {"mixed": (HOME + "\\shim\\bin;C:\\someone-else\\bin;" + BEFORE[0], 2),
               "removed": ("C:\\tools", 2),
               "type": (BEFORE[0], 1)}[change]
    with pytest.raises(AssertionError, match="in a way this test did not cause"):
        with guard():
            fake.values["Path"] = changed
    assert fake.values["Path"] == changed and announced == []


def test_the_user_path_guard_keeps_a_later_unrelated_change_when_it_restores(monkeypatch):
    # The leak, then someone else's entry before the guard rereads: only the
    # test's own entry goes.
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    leaked = HOME + "\\shim\\bin"
    real_read = fake.QueryValueEx
    reads = []

    def query(key, name):
        reads.append(name)
        if len(reads) == 3:   # the reread just before the write
            fake.values["Path"] = ("C:\\later\\bin;" + leaked + ";" + BEFORE[0], 2)
        return real_read(key, name)

    fake.QueryValueEx = query
    with pytest.raises(AssertionError, match="removed them again"):
        with guard():
            fake.values["Path"] = (leaked + ";" + BEFORE[0], 2)
    assert fake.values["Path"] == ("C:\\later\\bin;" + BEFORE[0], 2) and announced == [1]


def test_entries_inside_the_fixture_home_are_matched_as_windows_paths():
    assert _inside(HOME.upper() + "/shim/bin", HOME)
    assert _inside(HOME, HOME)
    assert not _inside(HOME + "-other\\bin", HOME)
    assert not _inside("C:\\tools", HOME) and not _inside("", HOME)
