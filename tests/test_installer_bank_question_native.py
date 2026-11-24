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
def test_a_failed_connect_skips_only_the_failed_clients_setup(bash, tmp_path):
    proc, calls = _connect_stages_sh(_Shell(bash, tmp_path), plan=PLAN_EXISTING, apply_exit=4)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "MCP_CODEX=failed" in proc.stdout
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


# -- issue #1908: an identical client-only rerun leaves Codex's token copy ------
# The fresh install registered Codex's own copy of the token file's token. On
# the identical command, connect's dry run plans to re-point Codex at the
# --token-file. When that plan changes nothing but Codex's rows and
# `installer codex-token-copy` answers "same", the installer treats it as a
# current plan: it verifies the credential and applies nothing. Here a fake
# shim's connect re-points the fake Codex registration (a file naming the
# registered token file) at --token-file when Codex is in its --client list,
# as the real connect does, and can add a change row of another kind
# (FAKE_EXTRA_CHANGE, as client/place); the fake step answers from the same files.

TOKEN_COPY_SHIM = r'''printf 'shim|%s\n' "$*" >>"$CALL_LOG"
list="" file="" prev=""
for arg in "$@"; do
    [ "$prev" = --client ] && list="$arg"
    [ "$prev" = --token-file ] && file="$arg"
    prev="$arg"
done
case ",$list," in *,codex,*) codex=1 ;; *) codex="" ;; esac
case " $* " in *' --dry-run '*)
    rows="" sep=""
    for client in $(printf '%s' "$list" | tr ',' ' '); do
        state=current
        if [ "$client" = codex ] && [ "$(cat "$FAKE_REG")" != "$file" ]; then state=change; fi
        rows="$rows$sep{\"client\":\"$client\",\"place\":\"registration\",\"state\":\"$state\"}"
        sep=","
        if [ "$client" = codex ]; then
            rows="$rows,{\"client\":\"codex\",\"place\":\"connection\",\"state\":\"$state\"}"
        fi
    done
    if [ -n "${FAKE_EXTRA_CHANGE:-}" ]; then
        rows="$rows$sep{\"client\":\"${FAKE_EXTRA_CHANGE%%/*}\",\"place\":\"${FAKE_EXTRA_CHANGE#*/}\",\"state\":\"change\"}"
    fi
    if [ -n "${FAKE_EXTRA_MANUAL:-}" ]; then
        rows="$rows$sep{\"client\":\"environment\",\"place\":\"process\",\"state\":\"manual\",\"detail\":\"$FAKE_EXTRA_MANUAL\"}"
    fi
    printf '{"exit":0,"rows":[%s]}\n' "$rows"
    exit 0 ;;
esac
[ -z "$codex" ] || printf '%s' "$file" >"$FAKE_REG"
echo 'fixture connect: applied'
'''


TOKEN_COPY_HELPER_SH = native_stub.HELPER_SH.replace("native_helper()", "standin_helper()") + r'''
native_helper() {
    case "$1" in
        codex-token-copy)
            printf 'step|%s\n' "$*" >>"$CALL_LOG"
            case "$FAKE_STEP" in
                real)
                    if [ "$(cat "$FAKE_REG")" != "$FAKE_COPY" ]; then
                        echo '{"state":"not-copy","detail":"fixture"}'
                    elif cmp -s "$FAKE_COPY" "$TOKEN_FILE"; then
                        echo '{"state":"same","detail":"fixture"}'
                    else
                        echo '{"state":"rotated","detail":"fixture"}'
                    fi ;;
                standin) standin_helper "$@"; return $? ;;
                *) printf '%s\n' "$FAKE_STEP"; return "${FAKE_STEP_EXIT:-0}" ;;
            esac ;;
        connect-report)
            # The native modes, over the fake shim's one-line rows.
            report=$(cat)
            changes=$(printf '%s' "$report" | grep -o '"client":"[^"]*","place":"[^"]*","state":"change"' || true)
            case "$2" in
                current) [ -n "$changes" ] || echo current ;;
                codex-only)
                    if [ -n "$changes" ] && ! printf '%s\n' "$changes" | grep -vq '^"client":"codex"'; then
                        echo codex-only
                    fi ;;
                *) printf '%s' "$report" | standin_helper "$@" ;;
            esac ;;
        credentials)
            printf 'verify|%s\n' "$*" >>"$CALL_LOG"
            printf '%s\n' "$FAKE_VERIFY" ;;
        *) standin_helper "$@" ;;
    esac
}
'''


VERIFIED = '{"status":"ready"}'
REFUSED = json.dumps({"status": "failed", "recovery": "The fixture credential was refused. Rerun the installer."})


class _TokenCopy:
    """Codex's home after a fresh client-only install: its own token copy,
    and a registration (``reg``) naming that copy."""

    def __init__(self, tmp_path: Path, token: Path, *, copy_token: str = FIXTURE_TOKEN):
        self.copy = tmp_path / "codex-home" / "pseudolife" / "token"
        self.copy.parent.mkdir(parents=True)
        # As _write_token_file writes the operator's file: the token alone.
        self.copy.write_text(copy_token, encoding="utf-8")
        self.reg = tmp_path / "codex-home" / "registered-token-file"
        self.token = token

    def register(self, path: Path | str) -> None:
        self.reg.write_bytes(str(path).encode("utf-8"))


def _block_sh(name: str) -> str:
    return SH.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _token_copy_sh(shell: _Shell, state: _TokenCopy, *, clients: str = "codex gemini",
                   step: str = "real", step_exit: int = 0, verify: str = VERIFIED, extra: str = "",
                   manual: str = ""):
    shim = shell.bin / "fake-shim"
    _stub(shim, TOKEN_COPY_SHIM)
    for name in clients.split():
        _stub(shell.bin / name, "exit 0\n")
    body = (f"DAEMON_URL='{REMOTE}'\nTOKEN_FILE='{shell.path(state.token)}'\nCLIENTS='{clients}'\n"
            "MCP_CODEX='' MCP_GEMINI=''\n"
            f"ensure_shim() {{ SHIM_OK=1; SHIM_PATH='{shell.path(shim)}'; }}\nSHIM_HELD=''\n"
            + TOKEN_COPY_HELPER_SH + _block_sh("client-only connect")
            + "connect_existing_registrations\n"
            "printf 'MCP_CODEX=%s\\nCONNECTED=%s\\n' \"$MCP_CODEX\" \"$CONNECTED_CLIENTS\"\n")
    proc = shell.run(body, strict="set -euo pipefail", extra_env={
        "FAKE_REG": shell.path(state.reg), "FAKE_COPY": shell.path(state.copy),
        "FAKE_STEP": step, "FAKE_STEP_EXIT": str(step_exit), "FAKE_VERIFY": verify,
        "FAKE_EXTRA_CHANGE": extra, "FAKE_EXTRA_MANUAL": manual})
    return proc, shell.logged()


def _connect_calls(calls: list[str]) -> list[str]:
    """Each connect call's client list and kind: 'codex,gemini dry-run'."""
    return [call.split("--client ", 1)[1].split()[0] + (" dry-run" if "--dry-run" in call else " apply")
            for call in calls if call.startswith("shim|connect")]


COPY_CURRENT = ("Verified the current credential for {names} at {url}: Codex's registration already "
                "names its own copy of the token in {file}. Nothing to write.")


@BASH
def test_an_identical_rerun_leaves_codexs_token_copy_registered(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    state = _TokenCopy(tmp_path, _sh_token(shell))
    state.register(shell.path(state.copy))
    registered = state.reg.read_bytes()
    for run in (1, 2):
        proc, calls = _token_copy_sh(shell, state)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert state.reg.read_bytes() == registered, f"run {run} re-pointed Codex's registration"
        assert COPY_CURRENT.format(names="codex,gemini", url=REMOTE, file=shell.path(state.token)) in proc.stdout
        assert "MCP_CODEX=\n" in proc.stdout
        assert proc.stdout.split("CONNECTED=", 1)[1].split() == ["codex", "gemini"]
    # Today's grouped dry run, and nothing applied; the credential still checked.
    assert _connect_calls(calls) == ["codex,gemini dry-run"] * 2
    assert len([call for call in calls if call.startswith("verify|")]) == 2
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr


@BASH
def test_codex_alone_applies_nothing_when_its_token_copy_is_current(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    state = _TokenCopy(tmp_path, _sh_token(shell))
    state.register(shell.path(state.copy))
    proc, calls = _token_copy_sh(shell, state, clients="codex")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [call.split("|")[0] for call in calls] == ["shim", "step", "verify"]
    assert _connect_calls(calls) == ["codex dry-run"]
    assert COPY_CURRENT.format(names="codex", url=REMOTE, file=shell.path(state.token)) in proc.stdout


@BASH
def test_a_refused_credential_on_a_current_token_copy_fails_codex(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    state = _TokenCopy(tmp_path, _sh_token(shell))
    state.register(shell.path(state.copy))
    registered = state.reg.read_bytes()
    proc, calls = _token_copy_sh(shell, state, verify=REFUSED)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "MCP_CODEX=failed" in proc.stdout
    assert "The fixture credential was refused. Rerun the installer." in proc.stderr
    assert "Nothing to write." not in proc.stdout
    assert not any(call.endswith(" apply") for call in _connect_calls(calls))
    assert state.reg.read_bytes() == registered


MANUAL_ROW = "this shell sets PSEUDOLIFE_MCP_DAEMON_URL=http://10.0.0.9:8765: unset it"


@BASH
def test_a_current_token_copy_still_shows_what_connect_cannot_rewrite(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    state = _TokenCopy(tmp_path, _sh_token(shell))
    state.register(shell.path(state.copy))
    registered = state.reg.read_bytes()
    proc, calls = _token_copy_sh(shell, state, manual=MANUAL_ROW)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert state.reg.read_bytes() == registered
    assert _connect_calls(calls) == ["codex,gemini dry-run"]
    assert "change them by hand" in proc.stderr and MANUAL_ROW in proc.stderr


TOKEN_COPY_NEAR_MISSES = [
    pytest.param({"copy_token": "fixture-rotated-token-9876543210"}, "real", 0, id="rotated"),
    pytest.param({"other": True}, "real", 0, id="another-file"),
    # The reviewer's case: connect also changes something of another kind
    # (the environment, a schedule), so the plan is applied as before.
    pytest.param({"extra": "environment/user"}, "real", 0, id="environment-changes-too"),
    # The row connect writes for an unattended-update schedule (connect/schedule.rs).
    pytest.param({"extra": "unattended-update/client-schedule"}, "real", 0, id="schedule-changes-too"),
    pytest.param({}, '{"state":"other-changes","detail":"fixture"}', 0, id="other-changes"),
    pytest.param({}, '{"state":"unknown","detail":"fixture"}', 1, id="could-not-tell"),
    pytest.param({}, '{"state":"same","detail":"fixture"}', 1, id="same-but-failed"),
    pytest.param({}, "not a report", 0, id="garbage"),
    pytest.param({}, "standin", 0, id="older-client"),
]


@pytest.mark.parametrize("setup,step,step_exit", TOKEN_COPY_NEAR_MISSES)
@BASH
def test_any_other_answer_applies_the_plan_as_before(bash, tmp_path, setup, step, step_exit):
    shell = _Shell(bash, tmp_path)
    state = _TokenCopy(tmp_path, _sh_token(shell), copy_token=setup.get("copy_token", FIXTURE_TOKEN))
    state.register(shell.path(tmp_path / "other.token") if setup.get("other") else shell.path(state.copy))
    proc, calls = _token_copy_sh(shell, state, step=step, step_exit=step_exit, extra=setup.get("extra", ""))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Today's grouped call, and connect moves the registration to the file given.
    assert _connect_calls(calls) == ["codex,gemini dry-run", "codex,gemini apply"]
    assert state.reg.read_text(encoding="utf-8") == shell.path(state.token)
    assert "Codex's registration already names its own copy" not in proc.stdout


# The same in install-native.ps1.

PS_TOKEN_COPY = r"""$clients = @(__CLIENTS__)
$requestedClients = $clients
$DaemonUrl = '__URL__'
$TokenFile = '__TOKEN__'
$hookState = @{}
$instrState = @{}
$mcpState = @{}
__STUBS__
function Step($message) { Write-Output "STEP: $message" | Out-Host }
function Install-ShimOnce { $script:shimInstallPath = 'fake-shim'; return $true }
function fake-shim {
    Add-Content -LiteralPath '__CALLS__' -Value ('shim|' + ($args -join ' '))
    $list = [string]$args[[array]::IndexOf($args, '--client') + 1]
    $file = [string]$args[[array]::IndexOf($args, '--token-file') + 1]
    $global:LASTEXITCODE = 0
    if ($args -contains '--dry-run') {
        $rows = @(foreach ($client in ($list -split ',')) {
            $state = if ($client -eq 'codex' -and [IO.File]::ReadAllText('__REG__') -ne $file) { 'change' } else { 'current' }
            @{ client = $client; place = 'registration'; state = $state }
            if ($client -eq 'codex') { @{ client = 'codex'; place = 'connection'; state = $state } }
        })
        if ($env:FAKE_EXTRA_CHANGE) {
            $extraClient, $extraPlace = $env:FAKE_EXTRA_CHANGE -split '/', 2
            $rows += @{ client = $extraClient; place = $extraPlace; state = 'change' }
        }
        if ($env:FAKE_EXTRA_MANUAL) { $rows += @{ client = 'environment'; place = 'process'; state = 'manual'; detail = $env:FAKE_EXTRA_MANUAL } }
        return (@{ exit = 0; rows = $rows } | ConvertTo-Json -Depth 5 -Compress)
    }
    if (@($list -split ',') -contains 'codex') { [IO.File]::WriteAllText('__REG__', $file) }
    return 'fixture connect: applied'
}
function Invoke-NativeHelper {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'codex-token-copy') {
        Add-Content -LiteralPath '__CALLS__' -Value ('step|' + ($args -join ' '))
        if ($env:FAKE_STEP -ne 'real') {
            $global:LASTEXITCODE = [int]$env:FAKE_STEP_EXIT
            return $env:FAKE_STEP
        }
        if ([IO.File]::ReadAllText('__REG__') -ne '__COPY__') { return '{"state":"not-copy","detail":"fixture"}' }
        if ([IO.File]::ReadAllText('__COPY__') -eq [IO.File]::ReadAllText($TokenFile)) { return '{"state":"same","detail":"fixture"}' }
        return '{"state":"rotated","detail":"fixture"}'
    }
    if ($args[0] -eq 'connect-report') {
        $report = (@($input) -join "`n") | ConvertFrom-Json
        $changes = @($report.rows | Where-Object { $_.state -eq 'change' })
        if ($args[1] -eq 'current') { if ($changes.Count -eq 0) { return 'current' }; return '' }
        if ($args[1] -eq 'codex-only') {
            if ($changes.Count -gt 0 -and @($changes | Where-Object { $_.client -ne 'codex' }).Count -eq 0) { return 'codex-only' }
            return ''
        }
        return ''
    }
    if ($args[0] -eq 'credentials') {
        Add-Content -LiteralPath '__CALLS__' -Value ('verify|' + ($args -join ' '))
        $global:LASTEXITCODE = [int]$env:FAKE_VERIFY_EXIT
        return $env:FAKE_VERIFY
    }
    $global:LASTEXITCODE = 2
}
"""


def _block_ps(name: str) -> str:
    return PS.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _token_copy_ps(ps: _PowerShell, state: _TokenCopy, *, clients: tuple[str, ...] = ("codex", "gemini"),
                   step: str = "real", step_exit: int = 0, verify: str = VERIFIED, extra: str = "",
                   manual: str = ""):
    body = PS_TOKEN_COPY
    for key, value in {"__CLIENTS__": ", ".join(f"'{name}'" for name in clients),
                       "__STUBS__": "\n".join(f"function {name} {{ }}" for name in clients),
                       "__URL__": REMOTE, "__TOKEN__": _ps_q(state.token), "__CALLS__": _ps_q(ps.calls),
                       "__REG__": _ps_q(state.reg), "__COPY__": _ps_q(state.copy)}.items():
        body = body.replace(key, value)
    body += (_block_ps("client-only connect") + "Invoke-ClientOnlyConnect\n"
             "Write-Output \"FAILED=$($script:clientSetupFailures.Keys -join ',')\"\n"
             "Write-Output \"CONNECTED=$($script:connectedClients -join ',')\"\n")
    proc = ps.run(body, extra_env={"FAKE_STEP": step, "FAKE_STEP_EXIT": str(step_exit), "FAKE_VERIFY": verify,
                                   "FAKE_VERIFY_EXIT": "0" if verify == VERIFIED else "1",
                                   "FAKE_EXTRA_CHANGE": extra, "FAKE_EXTRA_MANUAL": manual})
    return proc, ps.logged()


def _ps_line(proc, name: str) -> str:
    return next(line for line in proc.stdout.splitlines() if line.startswith(name + "="))[len(name) + 1:]


def test_an_identical_rerun_leaves_codexs_token_copy_registered_in_powershell(tmp_path):
    ps = _PowerShell(tmp_path)
    state = _TokenCopy(tmp_path, _ps_token(ps))
    state.register(state.copy)
    registered = state.reg.read_bytes()
    for run in (1, 2):
        proc, calls = _token_copy_ps(ps, state)
        assert proc.returncode == 0, _output(proc)
        assert state.reg.read_bytes() == registered, f"run {run} re-pointed Codex's registration"
        assert COPY_CURRENT.format(names="codex,gemini", url=REMOTE, file=state.token) in _output(proc)
        assert _ps_line(proc, "FAILED") == ""
        assert _ps_line(proc, "CONNECTED").split(",") == ["codex", "gemini"]
    assert _connect_calls(calls) == ["codex,gemini dry-run"] * 2
    assert len([call for call in calls if call.startswith("verify|")]) == 2


def test_codex_alone_applies_nothing_when_its_token_copy_is_current_in_powershell(tmp_path):
    ps = _PowerShell(tmp_path)
    state = _TokenCopy(tmp_path, _ps_token(ps))
    state.register(state.copy)
    proc, calls = _token_copy_ps(ps, state, clients=("codex",))
    assert proc.returncode == 0, _output(proc)
    assert [call.split("|")[0] for call in calls] == ["shim", "step", "verify"]
    assert COPY_CURRENT.format(names="codex", url=REMOTE, file=state.token) in _output(proc)


def test_a_refused_credential_on_a_current_token_copy_fails_codex_in_powershell(tmp_path):
    ps = _PowerShell(tmp_path)
    state = _TokenCopy(tmp_path, _ps_token(ps))
    state.register(state.copy)
    registered = state.reg.read_bytes()
    proc, calls = _token_copy_ps(ps, state, verify=REFUSED)
    assert proc.returncode == 0, _output(proc)
    assert "codex" in _ps_line(proc, "FAILED").split(",")
    assert "The fixture credential was refused. Rerun the installer." in _output(proc)
    assert "Nothing to write." not in proc.stdout
    assert not any(call.endswith(" apply") for call in _connect_calls(calls))
    assert state.reg.read_bytes() == registered


def test_a_current_token_copy_still_shows_what_connect_cannot_rewrite_in_powershell(tmp_path):
    ps = _PowerShell(tmp_path)
    state = _TokenCopy(tmp_path, _ps_token(ps))
    state.register(state.copy)
    registered = state.reg.read_bytes()
    proc, calls = _token_copy_ps(ps, state, manual=MANUAL_ROW)
    assert proc.returncode == 0, _output(proc)
    assert state.reg.read_bytes() == registered
    assert _connect_calls(calls) == ["codex,gemini dry-run"]
    assert "change them by hand" in _output(proc) and MANUAL_ROW in _output(proc)


@pytest.mark.parametrize("setup,step,step_exit", TOKEN_COPY_NEAR_MISSES)
def test_any_other_answer_applies_the_plan_as_before_in_powershell(tmp_path, setup, step, step_exit):
    ps = _PowerShell(tmp_path)
    state = _TokenCopy(tmp_path, _ps_token(ps), copy_token=setup.get("copy_token", FIXTURE_TOKEN))
    state.register(tmp_path / "other.token" if setup.get("other") else state.copy)
    if step == "standin":
        step, step_exit = "", 2  # what a native client without the step leaves
    proc, calls = _token_copy_ps(ps, state, step=step, step_exit=step_exit, extra=setup.get("extra", ""))
    assert proc.returncode == 0, _output(proc)
    assert _connect_calls(calls) == ["codex,gemini dry-run", "codex,gemini apply"]
    assert state.reg.read_text(encoding="utf-8") == str(state.token)
    assert "Codex's registration already names its own copy" not in _output(proc)
