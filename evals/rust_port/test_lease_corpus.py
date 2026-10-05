"""The additive lease spec declares inputs without inventing oracle outputs."""
import base64
from contextlib import contextmanager
import sys
import shutil

import pytest

from evals.rust_port import cli_process, lease_corpus


def test_lease_cases_are_opt_in_and_do_not_admit_deferred_operators():
    cases = lease_corpus.cases()
    assert len(cases) == len({case["id"] for case in cases})
    assert {case["mode"] for case in cases} == {"lease-check", "lease-list", "lease-run", "lease-parser"}
    assert all("response" not in case and case["normalizations"] == [] for case in cases)
    assert all(case["argv"][1] not in {"hold", "break", "delegate"} for case in cases)
    assert cli_process.cases(["lease"]) == cases
    assert all(not case["mode"].startswith("lease-") for case in cli_process.cases())
    assert all(base64.b64decode(case["pre_files_b64"][".pseudolife-mcp/locks/instance.id"]) == b"0123456789ab\n" for case in cases)


def test_local_cases_preserve_owned_daemon_admission_and_have_each_field_control():
    for case in lease_corpus.cases():
        assert not any(key.upper() in {"PSEUDOLIFE_MCP_DAEMON_URL", "PSEUDOLIFE_MCP_NO_SPAWN"}
                       for key in case["environment_deltas"])
        record = {"id": case["id"], "mode": case["mode"],
                  "oracle": {"response": {"exit_code": 0, "stdout_b64": "", "stderr_b64": "", "post_files_b64": {}}},
                  "candidate": {"response": {"exit_code": 0, "stdout_b64": "", "stderr_b64": "", "post_files_b64": {}}}}
        controls = cli_process.candidate_controls([record])
        assert {control["field"] for control in controls} == {"exit_code", "stdout_b64", "stderr_b64", "post_files_b64"}
        assert all(control["rejected"] and control["mutation_arm"] == "candidate" for control in controls)


def test_owned_lock_spans_process_but_snapshots_read_real_unlocked_bytes(tmp_path):
    from pseudolife_memory.os_lock import OsLock, probe
    case = {"id": "owned-lock-scope", "mode": "lease-check", "argv": [], "stdin_b64": "",
            "environment_deltas": {}, "pre_files_b64": {"fixture.lock": ""}, "normalizations": []}
    entered = []

    @contextmanager
    def scope(spec, home):
        assert (home / "fixture.lock").read_bytes() == b""
        lock = OsLock(home / "fixture.lock")
        assert lock.acquire()
        assert probe(lock.path) is True
        entered.append(spec["id"])
        try:
            yield
        finally:
            lock.release()

    result = cli_process.paired_cases([case], {arm: [sys.executable, "-c", "pass"]
                                              for arm in ("oracle", "candidate")},
                                     root=tmp_path, home=tmp_path / "home", url="http://127.0.0.1:49152",
                                     process_scope=scope)
    assert entered == [case["id"], case["id"]] and result["passed"]
    for arm in ("oracle", "candidate"):
        record = result["records"][0][arm]
        assert record["pre_files_b64"]["fixture.lock"] == record["response"]["post_files_b64"]["fixture.lock"] == ""


@pytest.mark.parametrize("key,value", [("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:49153"),
                                     ("PSEUDOLIFE_MCP_NO_SPAWN", None)])
def test_process_context_cannot_change_owned_daemon_after_admission(tmp_path, monkeypatch, key, value):
    environment = {}
    original = cli_process.fixture_env

    def fixture_env(*args):
        env = original(*args)
        environment["env"] = env
        return env

    @contextmanager
    def scope(_case, _home):
        if value is None:
            environment["env"].pop(key)
        else:
            environment["env"][key] = value
        yield

    monkeypatch.setattr(cli_process, "fixture_env", fixture_env)
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **kw: pytest.fail("inadmissible child launched"))
    spec = lease_corpus.cases()[0]
    with pytest.raises(ValueError, match="owned daemon"):
        cli_process.observe(spec, [sys.executable, "-c", "pass"], {"oracle": [sys.executable]},
                            root=tmp_path, home=tmp_path / "home", url="http://127.0.0.1:49152", process_scope=scope)


def test_process_context_cannot_replace_selected_program_after_admission(tmp_path, monkeypatch):
    copied = []

    def prepare(_case, home, _env, prefix, _commands):
        target = home / "program"
        shutil.copyfile(prefix[0], target)
        copied.append(target)
        return [str(target), *prefix[1:]]

    @contextmanager
    def scope(_case, _home):
        copied[-1].write_bytes(b"replaced-program")
        yield

    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **kw: pytest.fail("replaced program launched"))
    with pytest.raises(ValueError, match="command changed"):
        cli_process.observe(lease_corpus.cases()[0], [sys.executable, "-c", "pass"], {"oracle": [sys.executable]},
                            root=tmp_path, home=tmp_path / "home", url="http://127.0.0.1:49152",
                            prepare=prepare, process_scope=scope)


@pytest.mark.parametrize("placement", ["root", "nested", "cycle"])
def test_process_context_cannot_replace_owned_home_with_link(tmp_path, monkeypatch, placement):
    from evals.rust_port.test_cli_process import link_directory
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")

    @contextmanager
    def scope(_case, home):
        if placement == "root":
            shutil.rmtree(home)
            link_directory(outside, home)
        else:
            link_directory(home if placement == "cycle" else outside, home / "link")
        yield

    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **kw: pytest.fail("linked home child launched"))
    with pytest.raises(ValueError, match="link or junction"):
        cli_process.observe(lease_corpus.cases()[0], [sys.executable, "-c", "pass"], {"oracle": [sys.executable]},
                            root=tmp_path, home=tmp_path / "home", url="http://127.0.0.1:49152", process_scope=scope)
    assert sentinel.read_bytes() == b"unchanged"
    if placement == "root":
        assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("stage", ["prepare", "context"])
@pytest.mark.parametrize("key,value", [
    ("HOME", "outside"), ("USERPROFILE", "outside"), ("LOCALAPPDATA", "outside"),
    ("PSEUDOLIFE_LEASE_LOCK_DIR", "outside"), ("PSEUDOLIFE_SUITE_LOCK_DIR", "outside"),
    ("PSEUDOLIFE_MCP_TOKEN_FILE", "outside/token"), ("pseudolife_mcp_token_file", "outside/token"),
    ("PSEUDOLIFE_SHIM_MANIFEST_FILE", "outside/manifest.json"),
    ("XDG_RUNTIME_DIR", "outside"), ("PATH", "outside"), ("PYTHONPATH", "outside"),
    ("CUDA_VISIBLE_DEVICES", "0"), ("OMP_NUM_THREADS", "99"), ("NEW_RUNTIME_ENV", "added"),
])
def test_callbacks_cannot_mutate_any_admitted_environment(tmp_path, monkeypatch, stage, key, value):
    saved = {}
    def prepare(_case, _home, env, prefix, _commands):
        saved["env"] = env
        if stage == "prepare":
            env[key] = value
        return prefix

    @contextmanager
    def scope(_case, _home):
        if stage == "context":
            saved["env"][key] = value
        yield

    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **kw: pytest.fail("mutated environment launched"))
    with pytest.raises(ValueError, match="environment"):
        cli_process.observe(lease_corpus.cases()[0], [sys.executable, "-c", "pass"], {"oracle": [sys.executable]},
                            root=tmp_path, home=tmp_path / "home", url="http://127.0.0.1:49152",
                            prepare=prepare, process_scope=scope)
