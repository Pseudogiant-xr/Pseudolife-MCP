"""The native installer (ops/install-native.*): the tests of test_installer_bank_question.py
that the native client path changed, run against the native installer.

test_installer_bank_question.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from pseudolife_memory.credentials import CredentialProvider
from tests import native_client_stub as native_stub
from tests.test_client_install_ux import _heredoc_payload, _marker_block
from tests.test_installer_client_only import BASH, CRED_READY, FIXTURE_TOKEN, HEALTH_ON, REMOTE, _stub, _q as _sh_q, _token as _sh_token
from tests.test_installer_client_only_native import _Shell
from tests.test_installer_client_only_ps import _output, _q as _ps_q, _token as _ps_token
from tests.test_installer_client_only_ps_native import _PowerShell
from tests.test_installer_existing_upgrade import _bash_fixture_path


ROOT = Path(__file__).resolve().parents[1]


SH = (ROOT / "ops" / "install-native.sh").read_text(encoding="utf-8")


PS = (ROOT / "ops" / "install-native.ps1").read_text(encoding="utf-8")


PLAN_EXISTING = json.dumps({"url": REMOTE, "exit": 0, "rows": [
    {"client": "codex", "place": "registration", "file": "/fixture/codex/config.toml",
     "key": "pseudolife-memory", "state": "change",
     "changes": {"PSEUDOLIFE_MCP_DAEMON_URL": ["http://127.0.0.1:8765", REMOTE]},
     "detail": "", "notes": []},
    {"client": "codex", "place": "connection", "file": "/fixture/codex/pseudolife/connection.json",
     "key": None, "state": "change", "changes": {"daemon_url": ["http://127.0.0.1:8765", REMOTE]},
     "detail": "", "notes": []},
    {"client": "gemini", "place": "registration", "file": "/fixture/gemini/settings.json",
     "key": "pseudolife-memory", "state": "current", "changes": {}, "detail": "", "notes": []},
    {"client": "claude-code", "place": "registration", "file": None, "key": None,
     "state": "absent", "changes": {},
     "detail": "no registration; connect never creates one. To register: fixture-register",
     "notes": []}]})


NO_REGISTRATION = json.dumps({"url": REMOTE, "exit": 3, "rows": [], "error": "fixture-none"})


SHIM_STUB = ("printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
             "case \" $* \" in *' --dry-run '*)\n"
             "    printf '%s\\n' \"$FAKE_PLAN\"; exit \"${FAKE_PLAN_EXIT:-0}\" ;; esac\n"
             "echo 'fixture connect: applied'\n"
             "exit \"${FAKE_APPLY_EXIT:-0}\"\n")


CODEX_SETUP_SH = native_stub.HELPER_SH.replace("native_helper()", "standin_helper()") + r'''
native_helper() {
    case "$1" in
        codex-credentials)
            printf 'codex-setup|%s\n' "$*" >>"$CALL_LOG"
            case " $* " in *" --installer-token-stdin "*) cat >/dev/null ;; esac
            printf '%s\n' "$FAKE_CRED" ;;
        codex-runtime-defaults)
            printf 'codex-setup|%s\n' "$*" >>"$CALL_LOG"
            printf '{"status":"ready","runtime_defaults":"configured"}\n' ;;
        codex-hooks)
            printf '{"status":"ready","source":"manual","instructions":"covered-by-hooks","recovery":null}\n' ;;
        *) standin_helper "$@" ;;
    esac
}
'''


def _connect_stages_sh(shell: _Shell, *, plan: str, plan_exit: int = 0, apply_exit: int = 0,
                       cred_url: str = REMOTE, shim: str = SHIM_STUB):
    """Sections 9 to 11 for a Codex-only client-only install: the installed
    shim, the native Codex setup steps and codex all log to one file, in
    order."""
    token = _sh_token(shell)
    installed = shell.tmp / "installed-bin"
    installed.mkdir()
    _stub(installed / "pseudolife-mcp", shim)
    _stub(shell.bin / "codex",
          "printf 'codex|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1 $2\" = 'mcp get' ]; then exit 1; fi\n"
          "if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo '  --env <KEY=VALUE>'; exit 0; fi\n"
          "exit 0\n")
    stages = re.search(r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)"
                       r"^# [^\n]*12\. health", SH)[1]
    from tests.test_installer_existing_upgrade import _between
    proc = shell.run(
        f"env_file='{shell.path(shell.tmp / 'absent.env')}'\n"
        f"CLIENT_ONLY=1\nDAEMON_URL='{REMOTE}'\nTOKEN_FILE='{shell.path(token)}'\n"
        "CLIENTS=codex CODEX_HOOKS=auto CODEX_HOOK_TRUST=yes INSTRUCTIONS=auto CLAUDE_MD=''\n"
        "AGENTS_FILE='' TRANSPORT=shim\n"
        + _between("ops/install-native.sh", "get_env() {", "\n") + "\n" + native_stub.BASH + "\n"
        + CODEX_SETUP_SH + stages
        + "\nprintf 'MCP_CODEX=%s\\n' \"$MCP_CODEX\"\n",
        strict="set -euo pipefail",
        extra_env={"FAKE_CRED": CRED_READY % cred_url, "FAKE_PLAN": plan,
                   "FAKE_PLAN_EXIT": str(plan_exit), "FAKE_APPLY_EXIT": str(apply_exit),
                   "FAKE_LAUNCHER": _bash_fixture_path(shell.bash, installed / "pseudolife-mcp", shell.env),
                   "FAKE_KEEP_LAUNCHER": "1"})
    return proc, shell.logged()


def _first(calls: list[str], prefix: str) -> int:
    return next(i for i, call in enumerate(calls) if call.startswith(prefix))


@BASH
def test_connect_runs_before_the_codex_setup_and_the_registrars(bash, tmp_path):
    proc, calls = _connect_stages_sh(_Shell(bash, tmp_path), plan=PLAN_EXISTING)
    assert proc.returncode == 0, proc.stderr
    shim = [i for i, call in enumerate(calls) if call.startswith("shim|")]
    assert len(shim) == 2
    assert "--dry-run" in calls[shim[0]] and "--yes" in calls[shim[1]]
    assert shim[1] < _first(calls, "codex-setup|") < _first(calls, "codex|")
    assert "MCP_CODEX=shim-env" in proc.stdout


@BASH
def test_without_a_registration_the_codex_setup_follows_the_dry_run(bash, tmp_path):
    proc, calls = _connect_stages_sh(_Shell(bash, tmp_path), plan=NO_REGISTRATION, plan_exit=3)
    assert proc.returncode == 0, proc.stderr
    shim = [call for call in calls if call.startswith("shim|")]
    assert len(shim) == 1 and "--dry-run" in shim[0]
    assert _first(calls, "shim|") < _first(calls, "codex-setup|")
    assert "MCP_CODEX=shim-env" in proc.stdout


@BASH
def test_a_failed_connect_stops_before_the_codex_setup(bash, tmp_path):
    proc, calls = _connect_stages_sh(_Shell(bash, tmp_path), plan=PLAN_EXISTING, apply_exit=4)
    assert proc.returncode == 1
    assert not any(call.startswith(("codex-setup|", "codex|")) for call in calls)


OLDER_SHIM = ("printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
              "echo \"unknown mode 'connect'\"; exit 2\n")


@BASH
def test_a_codex_mismatch_connect_left_says_connect_did_not_re_point_it(bash, tmp_path):
    """connect checked (a report came back) and the Codex registration still
    names another daemon: one connect could not write."""
    proc, _ = _connect_stages_sh(_Shell(bash, tmp_path), plan=NO_REGISTRATION, plan_exit=3,
                                 cred_url="http://127.0.0.1:8765")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert f"not the daemon at {REMOTE}, and pseudolife-mcp connect did not re-point it" in out
    assert "Edit it in place" not in out
    assert "MCP_CODEX=failed" in proc.stdout


@BASH
def test_a_codex_mismatch_without_connect_keeps_the_earlier_advice(bash, tmp_path):
    proc, _ = _connect_stages_sh(_Shell(bash, tmp_path), plan="", cred_url="http://127.0.0.1:8765",
                                 shim=OLDER_SHIM)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert "Edit it in place in the Codex config.toml" in out
    assert "did not re-point" not in out


def test_connect_runs_before_the_codex_credential_setup_in_both_installers():
    """Static half of the order contract (the bash stages test above runs
    it): the call sits before the Codex credential setup and the registrars."""
    assert (SH.index("\n[ -z \"${CLIENT_ONLY:-}\" ] || connect_existing_registrations\n")
            < SH.index("native_helper codex-credentials ")
            < SH.index("# ── 11. wire into selected MCP clients"))
    assert (PS.index("\nif ($ClientOnly) { Invoke-ClientOnlyConnect }\n")
            < PS.index('$credentialArgs = @("codex-credentials")')
            < PS.index("# -- 11. wire into selected MCP clients"))
