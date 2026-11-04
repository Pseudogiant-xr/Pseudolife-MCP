"""An installer clone cannot replace an existing stack's configuration authority."""
import json

import pytest

from tests.test_installer_resources import BEGIN, END, container, guard


def daemon(working_dir=None, *, token="fixture-original-token", tokens=""):
    result = container()
    result["Config"]["Env"] = [f"PSEUDOLIFE_MCP_TOKEN={token}", f"PSEUDOLIFE_MCP_TOKENS={tokens}"]
    if working_dir is not None:
        result["Config"]["Labels"]["com.docker.compose.project.working_dir"] = str(working_dir)
    return result


def saved(path, *, token="fixture-original-token", tokens=""):
    path.write_text(f"{BEGIN}\n# extractor: sidecar\n{END}\n"
                    f"PSEUDOLIFE_MCP_TOKEN={token}\nPSEUDOLIFE_MCP_TOKENS='{tokens}'\n", encoding="utf-8")


def test_a_second_clone_is_refused_before_credentials_or_config_are_written(guard, tmp_path):
    module, env, containers, _, _, calls = guard
    original = tmp_path / "original/ops"
    original.mkdir(parents=True)
    saved(env)
    before = env.read_bytes()
    containers.append(daemon(original))
    with pytest.raises(module.Refusal, match="different install directory"):
        module.check(env)
    assert env.read_bytes() == before
    assert not any(call[0] in ("rm", "create", "up") for call in calls)


def test_a_missing_recorded_checkout_names_recovery_without_minting(guard, tmp_path):
    module, env, containers, _, _, _ = guard
    saved(env)
    containers.append(daemon(tmp_path / "removed-clone/ops"))
    with pytest.raises(module.Refusal, match="recorded install directory.*missing"):
        module.check(env)


def test_a_local_env_cannot_lose_the_existing_bearer_and_mint_another(guard):
    module, env, containers, _, _, _ = guard
    saved(env, token="")
    containers.append(daemon(env.parent))
    with pytest.raises(module.Refusal, match="existing daemon authentication") as result:
        module.check(env)
    assert "fixture-original-token" not in str(result.value)
    assert "fixture-different-token" not in str(result.value)


def test_unknown_origin_cannot_replace_an_existing_bearer(guard):
    module, env, containers, _, _, _ = guard
    saved(env, token="fixture-different-token")
    containers.append(daemon())
    with pytest.raises(module.Refusal, match="existing daemon authentication"):
        module.check(env)


def test_explicit_nonempty_auth_change_in_original_env_is_allowed(guard):
    module, env, containers, _, _, _ = guard
    saved(env, token="fixture-replacement-token")
    containers.append(daemon(env.parent))
    module.check(env)


def test_our_original_checkout_with_matching_credentials_is_allowed(guard):
    module, env, containers, _, _, _ = guard
    saved(env)
    containers.append(daemon(env.parent))
    module.check(env)


def test_valid_multi_principal_entries_are_allowed_on_rerun(guard):
    module, env, containers, _, _, _ = guard
    saved(env, token="", tokens="fixture-a:client-a,fixture-b:client-b")
    containers.append(daemon(env.parent, token="", tokens="fixture-a:client-a,fixture-b:client-b"))
    module.check(env)


def test_equivalent_multi_principal_order_and_case_do_not_change_auth(guard):
    module, env, containers, _, _, _ = guard
    saved(env, token="", tokens="fixture-a:CLIENT-A, fixture-b:client-b")
    containers.append(daemon(token="", tokens="fixture-b:client-b,fixture-a:client-a"))
    module.check(env)


def test_multiple_env_files_are_named_as_context_not_as_a_missing_file(guard, tmp_path):
    module, env, containers, _, _, _ = guard
    saved(env)
    extra = tmp_path / "extra.env"
    extra.write_text("EXTRA_SETTING=fixture\n", encoding="utf-8")
    value = daemon(env.parent)
    value["Config"]["Labels"]["com.docker.compose.project.environment_file"] = f"{env},{extra}"
    containers.append(value)
    with pytest.raises(module.Refusal, match="multiple environment files") as error:
        module.check(env)
    assert "missing" not in str(error.value)


def test_packaged_compose_working_dir_keeps_the_original_env_authority(guard, tmp_path):
    module, env, containers, _, _, _ = guard
    packaged = tmp_path / "runtime/site-packages/pseudolife_memory/compose"
    packaged.mkdir(parents=True)
    saved(env)
    value = daemon(packaged)
    value["Config"]["Labels"]["com.docker.compose.project.environment_file"] = str(env)
    containers.append(value)
    module.check(env)


def test_missing_packaged_compose_path_warns_with_matching_original_env(guard, tmp_path, capsys):
    module, env, containers, _, _, _ = guard
    saved(env)
    value = daemon(tmp_path / "pruned-runtime/compose")
    value["Config"]["Labels"]["com.docker.compose.project.environment_file"] = str(env)
    containers.append(value)
    module.check(env)
    assert "working directory is unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("client_only", [False, True])
def test_epilogue_only_promises_checkout_independence_for_clients(tmp_path, script, client_only):
    from tests.test_installer_client_only import _Shell
    from tests.test_installer_client_only_ps import _PowerShell, _output
    from tests.test_installer_existing_upgrade import _bash_variants
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    source = (root / "ops" / script).read_text(encoding="utf-8")
    code = source.split("# >>> update line >>>\n", 1)[1].split("# <<< update line <<<", 1)[0]
    if script.endswith(".ps1"):
        proc = _PowerShell(tmp_path).run(code + f"\nWrite-UpdateLine -ClientOnly:${str(client_only).lower()}\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        proc = _Shell(variants[0], tmp_path).run(f"CLIENT_ONLY='{'1' if client_only else ''}'\n" + code + "\nprint_update_line\n")
        output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    if client_only:
        assert "no checkout needed" in output
    else:
        assert "no checkout needed" not in output
        assert "Keep this checkout" in output and "ops/.env" in output
