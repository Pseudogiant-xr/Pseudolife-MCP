"""Process fixture admission, stdin/state bytes and candidate controls."""
import base64
import copy
import sys
import os
from pathlib import Path
import shutil

import pytest

from evals.rust_port import cli_process


def case():
    return {"id": "fixture-process", "mode": "fixture", "argv": [],
            "stdin_b64": base64.b64encode(b"peer m\xc3\xa9moire\x00\r\n").decode(),
            "environment_deltas": {"CLI_FIXTURE_VALUE": "non-ascii-\u00e9"},
            "pre_files_b64": {"seed.dat": base64.b64encode(b"seed\x00").decode()},
            "normalizations": []}


def test_paired_process_records_raw_stdin_and_poststate(tmp_path):
    code = ("import os,pathlib,sys; raw=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(raw); sys.stderr.buffer.write(b'err\\r\\n'); "
            "pathlib.Path(os.environ['HOME'],'new.dat').write_bytes(raw); "
            "assert pathlib.Path(os.environ['HOME'],'seed.dat').read_bytes()==b'seed\\x00'; "
            "assert os.environ['CLI_FIXTURE_VALUE']=='non-ascii-\u00e9'; "
            "assert os.environ['CUDA_VISIBLE_DEVICES']=='-1'")
    commands = {arm: [sys.executable, "-c", code] for arm in ("oracle", "candidate")}
    result = cli_process.paired_cases([case()], commands, root=tmp_path,
                                      home=tmp_path / "home", url="http://127.0.0.1:49152")
    assert result["passed"]
    record = result["records"][0]
    observed = record["candidate"]["response"]
    assert observed["stdout_b64"] == case()["stdin_b64"]
    assert observed["stderr_b64"] == base64.b64encode(b"err\r\n").decode()
    assert observed["post_files_b64"]["new.dat"] == case()["stdin_b64"]
    assert record["candidate"]["pre_files_b64"]["seed.dat"] == case()["pre_files_b64"]["seed.dat"]
    assert {control["field"] for control in result["candidate_output_controls"]} == {
        "exit_code", "stdout_b64", "stderr_b64", "post_files_b64"}
    assert all(control["mutation_arm"] == "candidate" for control in result["candidate_output_controls"])


def test_home_is_reset_between_arms_and_exact_changes_fail(tmp_path):
    seed = "import os,pathlib; pathlib.Path(os.environ['HOME'],'extra.bin').write_bytes(b'candidate')"
    commands = {"oracle": [sys.executable, "-c", "pass"], "candidate": [sys.executable, "-c", seed]}
    result = cli_process.paired_cases([case()], commands, root=tmp_path,
                                      home=tmp_path / "home", url="http://127.0.0.1:49152")
    assert not result["passed"]
    record = result["records"][0]
    assert record["oracle"]["pre_files_b64"] == record["candidate"]["pre_files_b64"]
    assert any(difference["path"] == "/response/post_files_b64/extra.bin" for difference in record["differences"])


@pytest.mark.parametrize("path", ["../escape", "", "/escape", "part\\escape"])
def test_seed_paths_cannot_escape_home(tmp_path, path):
    with pytest.raises(ValueError):
        cli_process.file_path(tmp_path, path)


@pytest.mark.parametrize("key", ["HOME", "USERPROFILE", "XDG_CACHE_HOME", "CUDA_VISIBLE_DEVICES",
                                "PYTHONIOENCODING", "OMP_NUM_THREADS", "PATH"])
def test_case_cannot_weaken_isolation(tmp_path, key):
    spec = case()
    spec["environment_deltas"] = {key: "host"}
    commands = {arm: [sys.executable] for arm in ("oracle", "candidate")}
    with pytest.raises(ValueError, match="cannot override"):
        cli_process.observe(spec, commands["oracle"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152")


def test_controls_are_not_satisfied_by_an_unrelated_existing_difference():
    response = {"exit_code": 0, "stdout_b64": "", "stderr_b64": "",
                "post_files_b64": {".pseudolife-mcp/token": base64.b64encode(b"synthetic").decode()}}
    record = {"id": "case", "mode": "help", "oracle": {"response": response},
              "candidate": {"response": {**response, "exit_code": 3}}}
    before = copy.deepcopy(record)
    controls = cli_process.candidate_controls([record])
    assert record == before
    assert all(control["rejected"] for control in controls)
    assert all(any(d["path"].startswith("/" + c["field"]) for d in c["differences"]) for c in controls)


def test_default_corpus_does_not_claim_unported_mode_coverage():
    assert {case["mode"] for case in cli_process.cases()} == {"help", "unknown-dispatch"}
    versions = cli_process.cases(("version",))
    assert len(versions) == 11
    assert len({case["id"] for case in versions}) == 11
    assert all("response" not in case for case in versions)


def test_snapshot_refuses_a_link_before_reading_outside_home(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.bin").write_bytes(b"outside")
    home = tmp_path / "home"
    home.mkdir()
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(outside), str(home / "link"))
    else:
        (home / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="link or junction"):
        cli_process.snapshot(home)


def link_directory(target, link):
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("placement", ["root", "nested", "cycle"])
def test_home_links_refuse_before_target_traversal(tmp_path, monkeypatch, placement):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    home = tmp_path / "home"
    if placement == "root":
        link_directory(outside, home)
    else:
        home.mkdir()
        link_directory(home if placement == "cycle" else outside, home / "link")
    original = os.scandir
    scanned = []

    def scan(path):
        scanned.append(str(path))
        assert str(path) not in (str(outside), str(home / "link"))
        assert placement != "root" or str(path) != str(home)
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    with pytest.raises(ValueError, match="link or junction"):
        cli_process.snapshot(home)
    assert sentinel.read_bytes() == b"unchanged"


def test_replaced_home_cleanup_removes_only_owned_link(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    home = tmp_path / "home"
    link_directory(outside, home)
    commands = {"oracle": [sys.executable]}
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="link or junction"):
        cli_process.observe(case(), commands["oracle"], commands, root=tmp_path,
                            home=home, url="http://127.0.0.1:49152")
    assert sentinel.read_bytes() == b"unchanged"
    assert not os.path.lexists(home)


@pytest.mark.parametrize("key", ["PSEUDOLIFE_MCP_DAEMON_URL", "PSEUDOLIFE_MCP_NO_SPAWN",
                                "pseudolife_mcp_daemon_url", "pseudolife_mcp_no_spawn"])
@pytest.mark.parametrize("value", [None, "http://127.0.0.1:49999", "0"])
def test_owned_daemon_delta_refuses_before_launch(tmp_path, monkeypatch, key, value):
    spec = case()
    spec["environment_deltas"] = {key: value}
    commands = {"oracle": [sys.executable]}
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="cannot override"):
        cli_process.observe(spec, commands["oracle"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152")


@pytest.mark.parametrize("key,value", [("PSEUDOLIFE_MCP_DAEMON_URL", None),
                                      ("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:49999"),
                                      ("PSEUDOLIFE_MCP_NO_SPAWN", "0"),
                                      ("pseudolife_mcp_no_spawn", "0")])
def test_prepare_cannot_change_owned_daemon(tmp_path, monkeypatch, key, value):
    def prepare(spec, home, env, command, commands):
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
        return command

    commands = {"oracle": [sys.executable]}
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="owned daemon"):
        cli_process.observe(case(), commands["oracle"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152", prepare=prepare)


def test_prepare_cannot_substitute_wrong_arm(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.exe"
    candidate.write_bytes(b"distinct candidate")
    commands = {"oracle": [sys.executable, "-c", "pass"], "candidate": [str(candidate)]}
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="original arm"):
        cli_process.observe(case(), commands["candidate"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152",
                            prepare=lambda *args: commands["oracle"])


def test_prepared_copy_records_effective_command_outside_byte_comparison(tmp_path, monkeypatch):
    original = tmp_path / "candidate.exe"
    original.write_bytes(b"distinct candidate")
    commands = {"oracle": [sys.executable], "candidate": [str(original)]}

    def prepare(spec, home, env, command, commands):
        copied = home / "installed" / Path(command[0]).name
        copied.parent.mkdir()
        shutil.copyfile(command[0], copied)
        return [str(copied), *command[1:]]

    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: {
        "exit_code": 0, "stdout_b64": "", "stderr_b64": ""})
    observed = cli_process.observe(case(), commands["candidate"], commands, root=tmp_path,
                                   home=tmp_path / "home", url="http://127.0.0.1:49152", prepare=prepare)
    assert observed["execution"]["effective_argv"] == [str(tmp_path / "home/installed/candidate.exe")]
    assert observed["execution"]["original_prefix"] == commands["candidate"]
    assert observed["execution"]["command_identity"]["executable_sha256"] == \
        cli_process.command_identity(commands["candidate"], tmp_path)["executable_sha256"]
    other = copy.deepcopy(observed)
    other["execution"]["effective_argv"] = ["distinct-other-arm"]
    assert cli_process.byte_payload(other) == cli_process.byte_payload(observed)


def test_post_capture_root_replacement_cleanup_preserves_sentinel(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    home = tmp_path / "home"

    def replace(*args, **kwargs):
        shutil.rmtree(home)
        link_directory(outside, home)
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    monkeypatch.setattr(cli_process, "run_cli", replace)
    commands = {"oracle": [sys.executable]}
    with pytest.raises(ValueError, match="link or junction"):
        cli_process.observe(case(), commands["oracle"], commands, root=tmp_path,
                            home=home, url="http://127.0.0.1:49152")
    assert not os.path.lexists(home)
    assert sentinel.read_bytes() == b"unchanged"


def test_corpus_run_refuses_python_candidate_before_daemon(tmp_path, monkeypatch):
    import evals.rust_baseline.daemon as daemon
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="test"\n')
    monkeypatch.setattr(cli_process, "require_phase1_source", lambda root: {})
    monkeypatch.setattr(cli_process, "require_import_root", lambda root: None)
    monkeypatch.setattr(cli_process, "runtime_metadata", lambda root: {
        "source_origin_matches_selected_root": True, "package_runtime_version": "test",
        "distribution_versions": {"pseudolife-mcp": "test"}})
    monkeypatch.setattr(cli_process, "candidate_identity", cli_process.command_identity)
    monkeypatch.setattr(daemon, "disposable_database", lambda: pytest.fail("must not launch daemon"))
    with pytest.raises(ValueError, match="distinct native executable"):
        cli_process.run(tmp_path, [sys.executable, "-m", "pseudolife_memory.cli"], tmp_path, [case()], {})
