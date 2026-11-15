"""The native installer (ops/install-native.*): the tests of test_installer_reruns.py
that the native client path changed, run against the native installer.

test_installer_reruns.py keeps the default (Python client) installer's tests.
"""
import importlib.util
import json
import inspect

import pytest

from tests.native_client_stub import HELPER_PS, HELPER_SH
from tests.test_installer_client_only import ROOT, _block
from tests.test_installer_client_only_native import _Shell
from tests.test_installer_client_only_ps import _q, _output
from tests.test_installer_client_only_ps_native import _PowerShell
from tests.test_installer_existing_upgrade import _bash_variants

BEGIN = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"


END = "# <<< pseudolife-mcp install <<<"


def managed(**values):
    return "\n".join([BEGIN, *(key + "=" + value for key, value in values.items()), END]) + "\n"


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
def test_managed_block_rewrite_is_quiet_and_keeps_timestamp_on_rerun(tmp_path, script):
    env = tmp_path / ".env"
    env.write_text("POSTGRES_PASSWORD=fixture-only\n", encoding="utf-8")
    common = {"EXTRACTOR": "sidecar", "MODEL": "", "EXTRACTOR_URL": "", "CLIENT": "codex", "SHIM_PORT": "8082",
              "INSTRUCTIONS": "skip", "CODEX_HOOKS": "skip", "CLAUDE_PLUGIN": "skip", "CLAUDE_LEGACY_HOOKS": "keep",
              "TRANSPORT": "shim", "NO_TOKEN": "1"}
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        prefix = HELPER_PS + f"$envFile='{_q(env)}'\n$EnvBegin='{BEGIN}'\n$EnvEnd='{END}'\n"
        names = {"EXTRACTOR": "Extractor", "MODEL": "Model", "EXTRACTOR_URL": "ExtractorUrl", "CLIENT": "Client",
                 "SHIM_PORT": "ShimPort", "INSTRUCTIONS": "Instructions", "CODEX_HOOKS": "CodexHooks",
                 "CLAUDE_PLUGIN": "ClaudePlugin", "CLAUDE_LEGACY_HOOKS": "ClaudeLegacyHooks", "TRANSPORT": "Transport", "NO_TOKEN": "NoToken"}
        prefix += "".join(f"${names[key]}='{value}'\n" for key, value in common.items())
        prefix += "$ClientOnly=$false\n$DaemonUrl=''\n$script:BankLocation='local'\n$clientList='codex'\n$writerId='codex'\n"
        body = prefix + block(script, "recorded choices") + block(script, "extractor env") + block(script, "managed configuration")
        first = ps.run(body)
        stamp, before = env.stat().st_mtime_ns, env.read_bytes()
        second = ps.run(body)
        outputs = _output(first), _output(second)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        prefix = HELPER_SH + f"env_file='{shell.path(env)}'\nENV_BEGIN='{BEGIN}'\nENV_END='{END}'\n"
        prefix += "".join(f"{key}='{value}'\n" for key, value in common.items())
        prefix += "CLIENT_ONLY=''\nDAEMON_URL=''\nEXPLICIT_ARGS=''\nCLIENT_LIST=codex\nWRITER_ID=codex\nBANK_LOCATION=local\nCLAUDE_MD=''\ncheck_extractor_args() { :; }\n"
        body = prefix + block(script, "recorded choices") + _block("extractor env") + block(script, "managed configuration")
        first = shell.run(body)
        stamp, before = env.stat().st_mtime_ns, env.read_bytes()
        second = shell.run(body)
        outputs = first.stdout + first.stderr, second.stdout + second.stderr
    assert first.returncode == second.returncode == 0, outputs
    assert "Managed configuration unchanged" in outputs[1]
    assert env.stat().st_mtime_ns == stamp and env.read_bytes() == before
    assert "fixture-only" not in " ".join(outputs)


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
def test_explicit_auto_port_overrides_a_recorded_custom_port(tmp_path, script):
    env = tmp_path / ".env"
    env.write_text(managed(PSEUDOLIFE_INSTALL_EXTRACTOR="openai-only", PSEUDOLIFE_INSTALL_MODEL="gpt-future",
                            PSEUDOLIFE_INSTALL_SHIM_PORT="8099"), encoding="utf-8")
    if script.endswith(".ps1"):
        body = (HELPER_PS + "function Invoke-Fixture { param([int]$ShimPort)\n"
                f"$envFile='{_q(env)}'\n$ClientOnly=$false\n$DaemonUrl=''\n$Extractor=''\n"
                "$Model=''\n$ExtractorUrl=''\n$Client=''\nfunction Assert-ExtractorArgs { }\n"
                + block(script, "recorded choices") + '\n"PORT=$ShimPort"\n}\nInvoke-Fixture -ShimPort 0\n')
        proc = _PowerShell(tmp_path).run(body)
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run(HELPER_SH + f"env_file='{shell.path(env)}'\nCLIENT_ONLY=''\nDAEMON_URL=''\nEXTRACTOR=''\n"
                         "MODEL=''\nEXTRACTOR_URL=''\nCLIENT=''\nSHIM_PORT=0\nEXPLICIT_ARGS=' --shim-port '\n"
                         "check_extractor_args() { :; }\n" + block(script, "recorded choices") + '\necho "PORT=$SHIM_PORT"\n')
        output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "PORT=0" in output


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("client", ["gemini", "generic"])
def test_resolved_instruction_choice_and_custom_target_survive_a_rerun(tmp_path, script, client):
    repo = tmp_path / "fixture-repo"
    (repo / "ops").mkdir(parents=True)
    (repo / "examples").mkdir()
    (repo / "examples/CLAUDE.memory.md").write_text("pseudolife-memory\n", encoding="utf-8")
    env = repo / "ops/.env"
    env.write_text(managed(PSEUDOLIFE_INSTALL_EXTRACTOR="sidecar", PSEUDOLIFE_INSTALL_CLIENT=client,
                            PSEUDOLIFE_INSTALL_INSTRUCTIONS="auto"), encoding="utf-8")
    helper = repo / "ops/installer_state.py"
    helper.write_text(
        "import importlib.util,json,sys\nfrom pathlib import Path\n"
        "sys.stdout.reconfigure(encoding='utf-8',newline='\\n')\n"
        f"s=importlib.util.spec_from_file_location('state',{str(ROOT / 'ops/installer_state.py')!r})\n"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
        "config={'services':{'daemon':{'container_name':'pseudolife-mcp-daemon'}}}\n"
        "inventory=[{'Id':'fixture','Name':'/pseudolife-mcp-daemon','Image':'fixture-image','State':{'Running':True},'Config':{'Labels':{'com.docker.compose.project':'pseudolife-mcp','com.docker.compose.config-hash':'fixture-hash','com.docker.compose.service':'daemon'}}}]\n"
        "def run(argv):\n"
        " if argv[0]=='git': return 'f'*40 if 'rev-parse' in argv else ''\n"
        " if argv[1]=='compose': return 'daemon fixture-hash\\n' if '--hash' in argv else json.dumps(config)\n"
        " if argv[1]=='ps': return 'fixture'\n"
        " if argv[1]=='inspect': return json.dumps(inventory)\n"
        " raise RuntimeError('unexpected Docker operation')\n"
        "m.run=run\n"
        "if sys.argv[1]=='fixture-plan':\n"
        " repo=Path(sys.argv[sys.argv.index('--repo')+1]);env=Path(sys.argv[sys.argv.index('--env-file')+1]);override=repo/'absent.yml'\n"
        " value={'action':'build',**m.deployment(repo,env,override)[0]}\n"
        " if hasattr(m,'started'): value=m.started(repo,env,override,expected=value)\n"
        " print(json.dumps(value));sys.exit(0)\n"
        "sys.exit(m.main())\n", encoding="utf-8")
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    target = tmp_path / "custom notes/AGENTS.md"
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        stage = source[source.index("# -- 10. standing"):source.index("# -- 11. wire")]
        invoke = block(script, "recorded choices").split("$script:installerRerun =", 1)[0]
        prefix = (HELPER_PS + f"$repo='{_q(repo)}'\n$envFile='{_q(env)}'\n$overrideFile='{_q(repo / 'absent.yml')}'\n"
                  f"$clients=@('{client}')\n$Client='{client}'\n$Extractor='sidecar'\n$Model=''\n$ExtractorUrl=''\n$ShimPort=0\n"
                  "$ClientOnly=$false\n$DaemonUrl=''\n$instructionChoice='auto'\n$Instructions='auto'\n$AgentsFile=''\n$claudePluginInstalled=$false\n"
                  + invoke
                  + "$composeSnapshotJson=Invoke-InstallerState fixture-plan --repo $repo --env-file $envFile\n")
        first = ps.run(prefix + "$interactive=$true\n$script:installerRerun=$false\n"
                       + f"function Read-Host {{ '{'n' if client == 'gemini' else _q(target)}' }}\n"
                       + stage + block(script, "record successful deployment"))
        second = ps.run(prefix + "$interactive=$true\nfunction Assert-ExtractorArgs { }\n"
                        + block(script, "recorded choices") + "$instructionChoice=$Instructions\n"
                        + stage + block(script, "record successful deployment"))
        output = _output(first) + _output(second)
        default = ps.home / (".gemini/GEMINI.md" if client == "gemini" else "AGENTS.md")
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        stage = source[source.index("# ── 10. standing"):source.index("# ── 11. wire")]
        invoke = block(script, "recorded choices").split('INSTALLER_RERUN=""', 1)[0]
        prefix = (HELPER_SH + f"repo='{shell.path(repo)}'\nenv_file='{shell.path(env)}'\noverride_file='{shell.path(repo / 'absent.yml')}'\n"
                  f"CLIENTS={client}\nCLIENT={client}\nEXTRACTOR=sidecar\nMODEL=''\nEXTRACTOR_URL=''\nSHIM_PORT=0\n"
                  "CLIENT_ONLY=''\nDAEMON_URL=''\nEXPLICIT_ARGS=''\nINSTRUCTIONS=auto\ninstruction_choice=auto\nAGENTS_FILE=''\nCLAUDE_PLUGIN_INSTALLED=''\n"
                  + invoke + 'compose_snapshot_json="$(installer_state fixture-plan --repo "$repo" --env-file "$env_file")"\n')
        tty = "[() { if test \"${1:-}\" = -t; then return 0; fi; if test \"${1:-}\" = '!' && test \"${2:-}\" = -t; then return 1; fi; builtin [ \"$@\"; }\n"
        reply = "read() { yn=n; }\n" if client == "gemini" else f"read() {{ answer='{shell.path(target)}'; }}\n"
        first = shell.run(prefix + tty + "INSTALLER_UNATTENDED=''\nINSTALLER_RERUN=''\n" + reply
                          + stage + block(script, "record successful deployment"))
        second = shell.run(prefix + tty + "INSTALLER_UNATTENDED=''\ncheck_extractor_args() { :; }\n"
                           + block(script, "recorded choices") + 'instruction_choice="$INSTRUCTIONS"\n'
                           + stage + block(script, "record successful deployment"))
        output = first.stdout + first.stderr + second.stdout + second.stderr
        default = shell.home / (".gemini/GEMINI.md" if client == "gemini" else "AGENTS.md")
    assert first.returncode == second.returncode == 0, output
    assert not default.exists(), output
    if client == "generic":
        assert target.read_text(encoding="utf-8").count("pseudolife-memory") == 1


def block(script, name):
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    marker = f"# >>> {name} >>>\n"
    return source.split(marker, 1)[1].split(f"# <<< {name} <<<", 1)[0] if marker in source else ""


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
def test_a_recorded_endpoint_is_reused_without_an_extractor_flag(tmp_path, script):
    env = tmp_path / ".env"
    content = (f"{BEGIN}\nPSEUDOLIFE_DREAM_BASE_URL=http://example.invalid:1234/v1\n"
               f"PSEUDOLIFE_DREAM_MODEL=fixture-model\nPSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary\n{END}\n")
    env.write_text(content, encoding="utf-8")
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    if script.endswith(".ps1"):
        stage = source[source.index("# -- 3. extractor choice"):source.index("$claudeShimMode =")]
        proc = _PowerShell(tmp_path).run(
            HELPER_PS + f"$envFile='{_q(env)}'\n$EnvBegin='{BEGIN}'\n$EnvEnd='{END}'\n"
            "$Extractor=''\n$Model=''\n$ExtractorUrl=''\n$Client=''\n$ShimPort=0\n"
            "$ClientOnly=$false\n$DaemonUrl=''\n$interactive=$false\n"
            "function Assert-ExtractorArgs { }\n" + block(script, "recorded choices") + stage
            + '\n"MODE=$Extractor|$Model|$ExtractorUrl"\n')
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        stage = source[source.index("# ── 3. extractor choice"):source.index('claude_shim_mode="";')]
        proc = shell.run(HELPER_SH + f"env_file='{shell.path(env)}'\nENV_BEGIN='{BEGIN}'\nENV_END='{END}'\n"
                         "EXTRACTOR=''\nMODEL=''\nEXTRACTOR_URL=''\nCLIENT=''\nSHIM_PORT=0\n"
                         "CLIENT_ONLY=''\nDAEMON_URL=''\nEXPLICIT_ARGS=''\ncheck_extractor_args() { :; }\n"
                         + block(script, "recorded choices") + stage
                         + '\nprintf "MODE=%s|%s|%s\\n" "$EXTRACTOR" "$MODEL" "$EXTRACTOR_URL"\n')
        output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "MODE=endpoint|fixture-model|http://example.invalid:1234/v1" in output
    assert "Choose " not in output
    assert env.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("plan", ["unchanged", "start", "build"])
def test_compose_runs_only_the_needed_step(tmp_path, script, plan):
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    if script.endswith(".ps1"):
        stage = source[source.index("# -- 7. bring the stack up"):source.index("# -- 8b. Claude Code plugin")]
        ps = _PowerShell(tmp_path)
        proc = ps.run("$ClientOnly=$false\n$envFile='fixture.env'\n$composeFile='fixture.yml'\n$overrideFile='missing.yml'\n"
                      + f"function Invoke-InstallerState {{ '{{\"action\":\"{plan}\"}}' }}\n" + ps.stub_function("docker") + stage)
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        stage = source[source.index("# ── 7. bring the stack up"):source.index("# ── 8b. Claude Code plugin")]
        proc = shell.run(HELPER_SH + "CLIENT_ONLY=''\nenv_file=fixture.env\ncompose_file=fixture.yml\noverride_file=missing.yml\n"
                         + f"installer_state() {{ echo '{{\"action\":\"{plan}\"}}'; }}\n" + stage)
        calls, output = shell.logged(), proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    up = [call for call in calls if " up " in call]
    if plan == "unchanged":
        assert not up
        assert "unchanged" in output.lower()
    else:
        assert len(up) == 1
        assert ("--build" in up[0]) == (plan == "build")
        assert ("--no-build" in up[0]) == (plan == "start")
