"""Offline image evidence fails closed and cleans every owned resource."""
import json
import subprocess
import traceback
from types import SimpleNamespace

import pytest

from rust.daemon.harness import image_model_proof as proof


def instance(tmp_path):
    return proof.Proof("image:fixture", "pg:fixture", tmp_path / "models", tmp_path, False)


def test_every_container_has_network_none(tmp_path, monkeypatch):
    trial = instance(tmp_path)
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=b"owned-id\n", returncode=0)

    monkeypatch.setattr(trial, "docker", docker)
    cid = trial.container("image:fixture")
    assert calls == [("run", "-d", "--name", cid, "--network", "none", "image:fixture")]
    assert trial.containers == [cid]


@pytest.mark.parametrize("failure", ["timeout", "spawn"])
def test_docker_execution_errors_do_not_print_disposable_credentials(tmp_path, monkeypatch, failure):
    trial = instance(tmp_path)
    credential = "synthetic-disposable-credential"

    def run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 240)
        raise OSError("cannot launch " + repr(command))

    monkeypatch.setattr(proof.subprocess, "run", run)
    try:
        trial.docker("exec", "owned-id", "curl", "-H", "Authorization: Bearer " + credential)
    except Exception:
        rendered = traceback.format_exc()
    else:
        pytest.fail("failed Docker invocation was accepted")
    assert credential not in rendered
    assert "docker exec" in rendered


def test_uncertain_container_launch_is_still_cleaned(tmp_path, monkeypatch):
    trial = instance(tmp_path)
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "run":
            raise RuntimeError("docker run could not complete")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(trial, "docker", docker)
    with pytest.raises(RuntimeError, match="could not complete"):
        trial.container("image:fixture")
    assert trial.cleanup()
    launch = calls[0]
    assert "--name" in launch
    name = launch[launch.index("--name") + 1]
    assert calls[1:] == [("rm", "-f", name)]


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_cleanup_attempts_all_owned_resources_after_failure(tmp_path, monkeypatch, failure):
    trial = instance(tmp_path)
    trial.containers = ["pg-owned", "daemon-owned"]
    trial.volume = "data-owned"
    trial.envfile.write_text("disposable fixture environment")
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[-1] == "daemon-owned":
            if failure == "timeout":
                raise subprocess.TimeoutExpired("docker", 240)
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(trial, "docker", docker)
    assert trial.cleanup() is False
    assert calls == [("rm", "-f", "daemon-owned"), ("rm", "-f", "pg-owned"),
                     ("volume", "rm", "data-owned")]
    assert not trial.envfile.exists()


@pytest.mark.parametrize("run_fails,cleanup_ok", [(True, True), (False, False)])
def test_failed_execution_or_cleanup_never_publishes_pass(tmp_path, monkeypatch, run_fails, cleanup_ok):
    out = tmp_path / "proof.json"
    monkeypatch.setattr(proof.sys, "argv", ["proof", "--image", "image:fixture",
                                          "--models", str(tmp_path / "models"), "--out", str(out)])
    cleaned = []

    def run(self):
        if run_fails:
            raise RuntimeError("offline read failed")
        return {"status": "pass"}

    def cleanup(self):
        cleaned.append(True)
        return cleanup_ok

    monkeypatch.setattr(proof.Proof, "run", run)
    monkeypatch.setattr(proof.Proof, "cleanup", cleanup)
    if run_fails:
        with pytest.raises(RuntimeError, match="offline read failed"):
            proof.main()
    else:
        assert proof.main() == 1
    assert cleaned == [True]
    assert json.loads(out.read_text()) == {"status": "fail", "cleanup": cleanup_ok}


def test_temporary_directory_cleanup_failure_cannot_publish_pass(tmp_path, monkeypatch):
    out = tmp_path / "proof.json"
    monkeypatch.setattr(proof.sys, "argv", ["proof", "--image", "image:fixture",
                                          "--models", str(tmp_path / "models"), "--out", str(out)])
    monkeypatch.setattr(proof.Proof, "run", lambda self: {"status": "pass"})
    monkeypatch.setattr(proof.Proof, "cleanup", lambda self: True)
    original = proof.tempfile.TemporaryDirectory
    owned = []

    class RefusedCleanup(original):
        def __init__(self, **kwargs):
            super().__init__(dir=tmp_path, **kwargs)
            owned.append(self)

        def cleanup(self):
            raise PermissionError("container changed socket directory ownership")

    monkeypatch.setattr(proof.tempfile, "TemporaryDirectory", RefusedCleanup)
    try:
        assert proof.main() == 1
        assert json.loads(out.read_text()) == {"status": "fail", "cleanup": False}
    finally:
        for directory in owned:
            original.cleanup(directory)
