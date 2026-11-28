"""Docker-tier engine selection in the installers (G2-B)."""
import importlib.util
import os
import sys
import json
import subprocess
from pathlib import Path

import pytest

from pseudolife_memory import update_cli as up
from pseudolife_memory import update_engine as eng
from tests.test_installer_client_only import ROOT, _Shell
from tests.test_installer_client_only_ps import _PowerShell, _output
from tests.test_installer_existing_upgrade import _bash_variants


@pytest.fixture
def helper(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("installer_engine_under_test", ROOT / "ops/installer_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "data"))
    # prepare creates the documents folder under the home directory.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    state = {"labels": None, "calls": [], "fail": None}

    def docker(*args, capture=True, timeout=60):
        state["calls"].append(list(args))
        code, out, err = 0, "", ""
        if args[:1] == ("inspect",) and "{{json .Config.Env}}" in args:
            code = 0 if state["labels"] is not None else 1
            out = json.dumps(state.get("container_env", ["PATH=/bin", "PSEUDOLIFE_MCP_TOKEN=fixture-bearer"]))
        elif args[:1] == ("inspect",):
            if state["labels"] is None:
                code, err = 1, "Error: No such object: pseudolife-mcp-daemon"
            else:
                out = json.dumps(state["labels"])
        elif args[:2] == ("volume", "inspect"):
            code = 0 if args[2] in state.setdefault("volumes", set()) else 1
        elif args[:2] == ("volume", "create"):
            state["volumes"].add(args[-1])
        elif args[:1] == ("pull",):
            code = int(state["fail"] == "pull")
        elif args[:1] == ("run",) and "--schema-version" in args:
            out = str(state.get("image_schema", 58))
        elif args[:1] == ("run",):
            code = int(state["fail"] == "provision")
        return subprocess.CompletedProcess(args, code, out, err)

    monkeypatch.setattr(module, "docker", docker)
    monkeypatch.setattr(module, "preflight_disk", lambda: state["calls"].append(["disk-check"]))
    state["health"] = {"status": "ok", "ready": True, "version": "0.23.0", "schema": 58}
    state["read"] = True
    monkeypatch.setattr(module, "health", lambda url: state["health"])
    monkeypatch.setattr(eng, "authenticated_read", lambda url, token: state["read"] and token == "fixture-bearer")
    env = tmp_path / "ops.env"
    env.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-bearer\n")
    state["env"] = env
    repo = tmp_path / "checkout"
    (repo / "ops").mkdir(parents=True)
    _write_crate(repo, "0.23.0")
    return module, state, repo


def _write_crate(repo, version):
    """The checkout's release version lives in its native client crate."""
    (repo / "rust" / "shim").mkdir(parents=True, exist_ok=True)
    (repo / "rust" / "shim" / "Cargo.toml").write_text(f'[package]\nname = "pseudolife-stdio"\nversion = "{version}"\n')


def _started(module, state):
    """What Compose does with the overlay: the container carries its labels."""
    state["labels"] = {"com.docker.compose.project": "pseudolife-mcp", **module.read_overlay()["labels"]}


def test_fresh_rust_install_pulls_provisions_then_records_only_after_health(helper):
    module, state, repo = helper
    assert module.detect() == "none"
    [overlay] = module.prepare(repo, "rust")
    order = [c[0] for c in state["calls"] if c[0] in ("disk-check", "pull", "run")]
    assert order == ["disk-check", "pull", "run"]
    pull, provision = [c for c in state["calls"] if c[0] in ("pull", "run")]
    assert pull == ["pull", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.23.0"]
    assert "pseudolife-mcp_pseudolife_rust_models:/models" in provision
    assert "PSEUDOLIFE_DAEMON_MODELS_DIR=/models" in provision and not any(":/data" in a for a in provision)
    create = next(c for c in state["calls"] if c[:2] == ["volume", "create"])
    assert "com.docker.compose.project=pseudolife-mcp" in create
    assert provision[-3:] == ["provision", "models", "--if-missing"]
    text = overlay.read_text(encoding="utf-8")
    assert overlay.name == "docker-compose.engine-installer.yml" and eng.transient_overlay(overlay)
    assert "build: !reset null" in text and '"pseudolife_rust_models:/models"' in text and '"ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.23.0"' in text
    assert not eng.release_accepted().exists()
    _started(module, state)
    assert module.detect() == "rust-unaccepted"
    assert module.accept() == "accepted"
    assert module.detect() == "rust"
    # update verifies the same record the installer wrote.
    assert eng._validated_engine_selection(state["labels"])["image"].endswith("-rust:0.23.0")


def test_a_rust_install_creates_the_documents_folder_once_and_mounts_it(helper, tmp_path, capsys):
    module, state, repo = helper
    folder = tmp_path / "home" / ".pseudolife-mcp" / "documents"
    [overlay] = module.prepare(repo, "rust")
    assert folder.is_dir()
    if os.name != "nt":
        assert folder.stat().st_mode & 0o777 == 0o755
        assert folder.parent.stat().st_mode & 0o777 == 0o700
    assert f"Created the documents folder {folder}" in capsys.readouterr().err
    text = overlay.read_text(encoding="utf-8")
    assert '      - "~/.pseudolife-mcp/documents:/documents:ro"' in text
    assert '      PSEUDOLIFE_DAEMON_DOCUMENT_ROOTS: "/documents"' in text
    # A rerun (the accepted selection, as on a set-up host) changes nothing
    # and says nothing about the folder.
    (folder / "kept.md").write_text("kept", encoding="utf-8")
    _started(module, state)
    module.accept()
    assert module.prepare(repo, "rust") == [overlay]
    assert "documents folder" not in capsys.readouterr().err
    assert (folder / "kept.md").read_text(encoding="utf-8") == "kept"
    # An accepted host whose folder is gone gets it back on a rerun.
    (folder / "kept.md").unlink()
    folder.rmdir()
    assert module.prepare(repo, "rust") == [overlay]
    assert folder.is_dir()
    # The Python engine gets no overlay.
    assert module.prepare(repo, "python") == []


def _root_owned_folder(module, state, tmp_path, monkeypatch, repaired=True):
    """The documents folder as Docker leaves a bind source it created: root
    owns it until the fake Docker's chown helper runs (and, with
    ``repaired``, works)."""
    folder = tmp_path / "home" / ".pseudolife-mcp" / "documents"
    folder.mkdir(parents=True)
    folder.chmod(0o700)
    owner = {"uid": 0}
    monkeypatch.setattr(module, "owner_of", lambda path: owner["uid"])
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getegid", lambda: 1000)
    wrapped = module.docker

    def docker(*args, capture=True, timeout=60):
        if args[:1] == ("run",) and "chown" in args and state.get("helper_code", 0) == 0 and repaired:
            owner["uid"] = 1000
        result = wrapped(*args, capture=capture, timeout=timeout)
        if args[:1] == ("run",) and "chown" in args:
            return subprocess.CompletedProcess(args, state.get("helper_code", 0), "", state.get("helper_err", ""))
        return result

    monkeypatch.setattr(module, "docker", docker)
    return folder, owner


@pytest.mark.skipif(os.name == "nt", reason="Docker Desktop creates the folder as the user")
def test_a_documents_folder_root_owns_is_given_back_through_a_one_off_container(helper, tmp_path, monkeypatch,
                                                                                  capsys):
    module, state, repo = helper
    folder, owner = _root_owned_folder(module, state, tmp_path, monkeypatch)
    module.prepare(repo, "rust")
    helpers = [c for c in state["calls"] if c[:1] == ["run"] and "chown" in c]
    assert len(helpers) == 1, state["calls"]
    args = helpers[0]
    # Never pulled, no network, a read-only root, uid 0 with CHOWN alone,
    # only this folder mounted, its owner alone changed.
    for pair in (["--pull", "never"], ["--network", "none"], ["--user", "0:0"], ["--cap-drop", "ALL"],
                 ["--cap-add", "CHOWN"], ["--entrypoint", "chown"]):
        assert any(args[i:i + 2] == pair for i in range(len(args))), (pair, args)
    assert "--read-only" in args and args.count("--mount") == 1
    assert args[args.index("--mount") + 1] == f"type=bind,source={folder},dst=/documents"
    assert args[-3:] == [f"{eng.RUST_IMAGE}:0.23.0", "1000:1000", "/documents"]
    assert owner["uid"] == 1000 and folder.stat().st_mode & 0o777 == 0o755
    assert "it is yours again" in capsys.readouterr().err
    # A rerun on the repaired folder does nothing and says nothing.
    state["calls"].clear()
    module.prepare(repo, "rust")
    assert not [c for c in state["calls"] if "chown" in c]
    assert "documents" not in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="Docker Desktop creates the folder as the user")
def test_without_docker_a_documents_folder_root_owns_gets_the_sudo_line(helper, tmp_path, monkeypatch, capsys):
    module, state, repo = helper
    folder, owner = _root_owned_folder(module, state, tmp_path, monkeypatch)
    state["helper_code"] = 125
    state["helper_err"] = "\ndocker: Error response from daemon: no such image\nRun 'docker run --help'"
    module.prepare(repo, "rust")
    err = capsys.readouterr().err
    # Docker's own reason, then the fix.
    assert ("the automatic repair failed (docker: Error response from daemon: no such image); "
            f"run sudo chown 1000:1000 {folder} once") in err
    assert owner["uid"] == 0 and folder.stat().st_mode & 0o777 == 0o700
    assert any(c[:2] == ["rm", "-f"] for c in state["calls"])
    # Docker unreachable or timed out (the engine's docker raises): the same
    # line with that reason, and the leftover still removed by name.
    wrapped = module.docker
    removed = []

    def unreachable(*args, **kwargs):
        if args[:1] == ("run",) and "chown" in args:
            raise module.Refusal("Cannot reach Docker; start Docker and rerun the installer.")
        if args[:2] == ("rm", "-f"):
            removed.append(args[2])
            raise module.Refusal("Cannot reach Docker")
        return wrapped(*args, **kwargs)

    monkeypatch.setattr(module, "docker", unreachable)
    module.documents_folder("img")
    err = capsys.readouterr().err
    assert "the automatic repair failed (Cannot reach Docker; start Docker and rerun the installer.)" in err
    assert "run sudo chown 1000:1000" in err and "Traceback" not in err
    assert len(removed) == 1 and removed[0].startswith("pseudolife-documents-owner-")


@pytest.mark.skipif(os.name == "nt", reason="Docker Desktop creates the folder as the user")
def test_a_mode_that_cannot_be_set_after_the_repair_names_its_chmod(helper, tmp_path, monkeypatch, capsys):
    module, state, repo = helper
    folder, owner = _root_owned_folder(module, state, tmp_path, monkeypatch)
    real = Path.chmod

    def chmod(path, mode, **kwargs):
        if path == folder:
            raise PermissionError(1, "Operation not permitted")
        return real(path, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", chmod)
    module.prepare(repo, "rust")
    err = capsys.readouterr().err
    assert "is yours again, but its mode could not be set" in err and f"run chmod 755 {folder} once" in err
    assert owner["uid"] == 1000


@pytest.mark.skipif(os.name == "nt", reason="no owner check on Windows")
def test_a_documents_link_is_never_repaired_or_named(helper, tmp_path, monkeypatch, capsys):
    module, state, repo = helper
    target = tmp_path / "elsewhere"
    target.mkdir()
    folder = tmp_path / "home" / ".pseudolife-mcp" / "documents"
    folder.parent.mkdir(parents=True)
    folder.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(module, "owner_of", lambda path: 0)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    module.prepare(repo, "rust")
    assert not [c for c in state["calls"] if "chown" in c]
    assert "documents" not in capsys.readouterr().err
    assert folder.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="no owner check on Windows")
def test_a_folder_that_changes_before_the_run_is_never_handed_to_docker(helper, tmp_path, monkeypatch, capsys):
    module, state, repo = helper
    folder, owner = _root_owned_folder(module, state, tmp_path, monkeypatch)
    reads = []

    def owner_of(path):
        reads.append(path)
        # Root's on the first read, another owner on the re-read before the run.
        return 0 if len(reads) == 1 else 1001

    monkeypatch.setattr(module, "owner_of", owner_of)
    module.prepare(repo, "rust")
    assert not [c for c in state["calls"] if "chown" in c]
    assert "changed while it was being checked" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="no sudo on Windows")
def test_a_run_as_root_under_sudo_says_whose_folder_it_checks(helper, monkeypatch, capsys):
    module, state, repo = helper
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", "1000")
    module.prepare(repo, "rust")
    err = capsys.readouterr().err
    assert err.count("this runs as root under sudo (from uid 1000)") == 1, err
    # Near miss: root without sudo says nothing about it.
    monkeypatch.delenv("SUDO_UID")
    module.prepare(repo, "rust")
    assert "under sudo" not in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="Docker Desktop creates the folder as the user")
def test_a_helper_that_leaves_the_owner_unchanged_gets_the_sudo_line(helper, tmp_path, monkeypatch, capsys):
    module, state, repo = helper
    # The helper exits 0 but the owner stays root (a remapped user namespace):
    # only the re-read owner counts.
    folder, owner = _root_owned_folder(module, state, tmp_path, monkeypatch, repaired=False)
    module.prepare(repo, "rust")
    assert [c for c in state["calls"] if c[:1] == ["run"] and "chown" in c]
    assert f"run sudo chown 1000:1000 {folder} once" in capsys.readouterr().err
    assert folder.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="no owner check on Windows")
def test_a_documents_folder_another_account_owns_is_named_not_repaired(helper, tmp_path, monkeypatch, capsys):
    module, state, repo = helper
    folder = tmp_path / "home" / ".pseudolife-mcp" / "documents"
    folder.mkdir(parents=True)
    # A non-root account's folder may be meant that way: named, never chowned.
    monkeypatch.setattr(os, "geteuid", lambda: folder.stat().st_uid + 1)
    module.prepare(repo, "rust")
    err = capsys.readouterr().err
    assert "belongs to another account" in err and "automatic repair" not in err
    assert not [c for c in state["calls"] if "chown" in c]


@pytest.mark.skipif(os.name == "nt", reason="no owner check on Windows")
def test_a_documents_folder_the_user_owns_is_left_alone_on_a_rerun(helper, tmp_path, capsys):
    module, state, repo = helper
    folder = tmp_path / "home" / ".pseudolife-mcp" / "documents"
    folder.mkdir(parents=True)
    folder.chmod(0o750)
    module.prepare(repo, "rust")
    assert not [c for c in state["calls"] if "chown" in c]
    assert folder.stat().st_mode & 0o777 == 0o750
    assert "documents" not in capsys.readouterr().err


def test_a_file_where_the_documents_folder_goes_refuses_the_install(helper, tmp_path):
    module, state, repo = helper
    folder = tmp_path / "home" / ".pseudolife-mcp" / "documents"
    folder.parent.mkdir(parents=True)
    folder.write_text("not a folder", encoding="utf-8")
    with pytest.raises(module.Refusal, match="exists but is not a folder"):
        module.prepare(repo, "rust")
    assert folder.read_text(encoding="utf-8") == "not a folder"


def test_accept_waits_for_a_ready_daemon_at_its_release(helper):
    module, state, repo = helper
    module.prepare(repo, "rust")
    _started(module, state)
    good = {"status": "ok", "ready": True, "version": "0.23.0", "schema": 58}
    # Each refusal names its own cause.
    for bad, read, cause in (({**good, "ready": False}, True, "not ready"),
                             ({**good, "version": "0.22.1"}, True, "reports version 0.22.1"),
                             ({**good, "counters_import_required": True}, True, "counters still to import"),
                             ({**good, "schema": 59}, True, "reports schema 59"),
                             (good, False, "refused an authenticated bank read")):
        state["health"], state["read"] = bad, read
        with pytest.raises(module.Refusal, match=cause):
            module.accept()
        assert not eng.release_accepted().exists()
    # The bearer is the one the daemon was started with, however Compose got
    # it (ops/.env, the caller's environment). An open daemon answers any
    # bearer, so a container without one is refused outright.
    state["health"], state["read"] = good, True
    state["container_env"] = ["PATH=/bin"]
    with pytest.raises(module.Refusal, match="without a bearer token"):
        module.accept()
    state["container_env"] = ["PSEUDOLIFE_MCP_TOKENS=fixture-bearer:agent"]
    assert module.accept() == "accepted"


def test_a_removed_container_keeps_the_accepted_rust_selection(helper):
    module, state, repo = helper
    [overlay] = module.prepare(repo, "rust")
    _started(module, state)
    module.accept()
    before, calls = overlay.read_bytes(), len(state["calls"])
    state["labels"] = None  # docker compose down
    assert module.detect() == "rust"
    assert module.prepare(repo, "rust") == [overlay] and overlay.read_bytes() == before
    assert not any(c[0] in ("pull", "run") for c in state["calls"][calls:])


def test_a_later_checkout_selection_supersedes_a_removed_release_selection(helper):
    import os as _os
    module, state, repo = helper
    module.prepare(repo, "rust")
    _started(module, state)
    module.accept()
    state["labels"] = None  # docker compose down after a checkout-mode switch back
    accepted = repo / eng.PYTHON_ACCEPTED
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_text("{}")
    later = eng.release_accepted().stat().st_mtime_ns + 1_000_000_000
    _os.utime(accepted, ns=(later, later))
    assert module.detect(repo) == "python"
    assert module.detect() == "rust"
    # The reverse: a release record newer than the checkout file wins.
    earlier = eng.release_accepted().stat().st_mtime_ns - 1_000_000_000
    _os.utime(accepted, ns=(earlier, earlier))
    assert module.detect(repo) == "rust"
    # A release-mode switch back to Python leaves a python record, which also counts.
    eng._write_overlay(eng.release_accepted(), {"image": "py", "labels": {eng.ENGINE_LABEL: "python",
                                                                          eng.ACTIVATION_LABEL: "b"}})
    assert module.detect(repo) == "python"
    assert module.detect() == "python"


def test_an_unaccepted_rust_daemon_is_redone_only_for_this_checkouts_release(helper):
    module, state, repo = helper
    module.prepare(repo, "rust")
    _started(module, state)  # never accepted: its health check failed
    assert module.detect() == "rust-unaccepted"
    pulls = len([c for c in state["calls"] if c[0] == "pull"])
    module.prepare(repo, "rust")
    assert len([c for c in state["calls"] if c[0] == "pull"]) == pulls + 1
    assert module.prepare(repo, "python") == []
    for release in ("0.24.0", None):  # another release, or a Rust container without labels
        state["labels"] = {eng.ENGINE_LABEL: "rust", **({eng.RELEASE_LABEL: release} if release else {})}
        for engine in ("rust", "python"):
            with pytest.raises(module.Refusal, match="docker rm -f pseudolife-mcp-daemon"):
                module.prepare(repo, engine)


def test_rerun_on_an_accepted_host_reproduces_the_overlay_without_pulling(helper):
    module, state, repo = helper
    [overlay] = module.prepare(repo, "rust")
    _started(module, state)
    module.accept()
    before, calls = overlay.read_bytes(), len(state["calls"])
    _write_crate(repo, "0.24.0")
    assert module.prepare(repo, "rust") == [overlay]
    assert overlay.read_bytes() == before
    assert not any(c[0] in ("pull", "run") for c in state["calls"][calls:])
    assert module.accept() == "unchanged"


@pytest.mark.parametrize("failure,expected", [("pull", "registry access"), ("provision", "models")])
def test_rust_refuses_before_any_container_when_the_image_or_models_are_unavailable(helper, failure, expected):
    module, state, repo = helper
    state["fail"] = failure
    with pytest.raises(module.Refusal, match=expected):
        module.prepare(repo, "rust")
    assert not module.overlay_path().exists()


@pytest.mark.parametrize("failure", ["pull", "provision"])
def test_a_defaulted_rust_install_falls_back_to_python_when_rust_is_unavailable(helper, failure, capsys):
    module, state, repo = helper
    state["fail"] = failure
    assert module.prepare(repo, "rust", default=True) == []
    assert "this install uses the Python daemon" in capsys.readouterr().err
    assert not module.overlay_path().exists()


def test_the_default_falls_back_only_when_rust_is_unavailable(helper, monkeypatch):
    module, state, repo = helper

    def low_disk():
        raise module.Refusal("less than 8 GiB is free. Nothing was started.")
    monkeypatch.setattr(module, "preflight_disk", low_disk)
    with pytest.raises(module.Refusal, match="8 GiB"):
        module.prepare(repo, "rust", default=True)


def test_an_explicit_python_install_records_its_choice(helper, capsys):
    module, state, repo = helper
    assert module.main(["choose", "--engine", "python"]) == 0
    assert eng.recorded_choice() == "python"
    assert module.main(["choose", "--engine", "rust"]) == 1


def test_a_failed_choice_write_warns_and_the_install_succeeds(helper, capsys, tmp_path):
    module, state, repo = helper
    # A non-empty directory where the choice file belongs: no write can replace it.
    blocker = tmp_path / "data" / eng.CHOICE
    blocker.mkdir(parents=True)
    (blocker / "keep").write_text("x")
    assert module.main(["choose", "--engine", "python"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "unrecorded"
    assert "could not record the Python engine choice" in err


def _record_stage(tmp_path, script, engine, compose_engine, helper_exit=0):
    """Run the post-health engine record stage with a logging helper stub."""
    if script.endswith(".ps1"):
        stage = _stage(script, "# >>> engine record >>>", "# <<< engine record <<<")
        ps = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{ps.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                f"    $global:LASTEXITCODE = {helper_exit}\n"
                "    if ($args[0] -eq 'accept') { return 'accepted' }\n    return 'unrecorded'\n}\n"
                "function Step($text) { Write-Output \"STEP: $text\" }\n")
        proc = ps.run(f"$Engine='{engine}'\n$composeEngine='{compose_engine}'\n" + stub + stage
                      + "Write-Output 'RESULT:done'\n")
        return proc, ps.logged(), _output(proc)
    variants = _bash_variants()
    if not variants:
        pytest.skip("Bash unavailable")
    stage = _stage(script, "# >>> engine record >>>", "# <<< engine record <<<")
    shell = _Shell(variants[0], tmp_path)
    stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
            f'[ "$1" = accept ] && echo accepted || echo unrecorded; return {helper_exit}; }}\n'
            'step() { echo "STEP: $*"; }\n')
    proc = shell.run(f"ENGINE='{engine}'\nCOMPOSE_ENGINE='{compose_engine}'\n" + stub + stage + 'echo "RESULT:done"\n')
    return proc, shell.logged(), proc.stdout + proc.stderr


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("engine,compose_engine,expected", [
    ("", "rust", "accept"), ("rust", "rust", "accept"), ("", "python", None),
])
def test_the_install_accepts_rust_and_records_no_python_choice(tmp_path, script, engine, compose_engine,
                                                               expected):
    # The Python daemon is retired: a host brought up on Python (to move after
    # the install) records nothing, and no install writes a Python choice.
    proc, calls, output = _record_stage(tmp_path, script, engine, compose_engine)
    assert proc.returncode == 0, output
    assert "RESULT:done" in output
    recorded = [call.split("|", 1)[1] for call in calls if call.startswith("helper|")]
    assert recorded == ([expected] if expected else []), recorded
    assert not any("choose" in call for call in recorded), recorded


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("flag,installed,bank", [("", "none", False), ("rust", "none", False),
                                                 ("", "rust-unaccepted", False), ("", "rust", False),
                                                 ("", "python", True), ("", "none", True)],
                         ids=["default", "explicit-rust", "unfinished-rust", "rust-installed",
                              "python-installed", "existing-bank"])
def test_no_shim_tooling_is_decided_before_anything_is_pulled(tmp_path, script, flag, installed, bank):
    # Conductor round-4 delta: with the shim transport, no token set and no
    # pipx or pip, stage 5b mints none. Stage 4b knows that before its
    # prepare pulls the image and fills the models volume. The Python daemon
    # is retired, so no path falls back to it: a new install, an existing bank
    # and a host still on Python (moved after the install) all refuse here.
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        stage = stage.replace(definition, "").replace("function Test-ShimToolingReady(", "function Test-ShimToolingReady-Real(")
        ps = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{ps.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                f"    if ($args[0] -eq 'detect') {{ return '{installed}' }}\n}}\n"
                "function Test-ShimToolingReady($python) { return $false }\n"
                "function Get-EnvValue($name) { return $null }\n"
                "function Get-InstallerPython { return 'python' }\n")
        proc = ps.run(f"$ClientOnly=$false\n$Engine='{flag}'\n$rustBearer=$true\n$Transport='shim'\n"
                      "$env:PSEUDOLIFE_MCP_TOKEN=$null\n$env:PSEUDOLIFE_MCP_TOKENS=$null\n"
                      f"$bankExisted=${str(bank).lower()}\n" + stub + stage
                      + "Write-Output \"RESULT:$composeEngine\"\n")
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        stage = stage.replace(definition, "").replace("shim_tooling_ready() {", "shim_tooling_ready_real() {")
        shell = _Shell(variants[0], tmp_path)
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                f'case "$1" in detect) echo {installed} ;; esac; }}\n'
                'step() { echo "STEP: $*"; }\nshim_tooling_ready() { return 1; }\nget_env() { :; }\n')
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nRUST_BEARER=1\nTRANSPORT=shim\n"
                         f"BANK_EXISTED='{'1' if bank else ''}'\n"
                         "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS\n" + stub + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE"\n')
        calls, output = shell.logged(), proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "cannot mint one" in output and "Nothing was pulled or started" in output
    assert "--engine python" not in output and "-Engine python" not in output, output
    assert "no bearer token for the default Rust daemon" not in output, output
    assert ("moves to the Rust daemon after the install, and that move needs the token too" in output) == (installed == "python" or bank), output
    assert [call for call in calls if "prepare" in call] == []


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("token_state,compose,switch,expected", [
    ("minted", "rust", "", "rust"), ("present", "rust", "", "rust"), ("no-shim", "rust", "", "refused"),
    ("failed", "rust", "", "refused"), ("minted", "python", "rust", "python"),
    ("no-shim", "python", "rust", "refused"),
])
def test_the_rust_engine_follows_the_token_the_install_produced(tmp_path, script, token_state, compose, switch,
                                                                expected):
    # Codex P1 (round 3): with the shim transport but no pipx or pip, stage 4b
    # predicted a bearer that stage 5b never minted. No default falls back to
    # Python any more: a Rust install, or a Python host's move after the
    # install, without a bearer refuses.
    stage = (_stage(script, "# >>> bearer refusal ways >>>", "# <<< bearer refusal ways <<<")
             + _stage(script, "# >>> engine token >>>", "# <<< engine token <<<"))
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        proc = ps.run("function Step($text) { Write-Output \"STEP: $text\" }\n"
                      f"$composeEngine='{compose}'\n$switchEngine='{switch}'\n"
                      f"$engineFiles=@('C:\\fixture\\engine.yml')\n$tokenState='{token_state}'\n" + stage
                      + "Write-Output \"RESULT:$composeEngine|$($engineFiles -join ',')\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run('step() { echo "STEP: $*"; }\n'
                         f"COMPOSE_ENGINE={compose}\nSWITCH_ENGINE='{switch}'\n"
                         f"engine_files=(/fixture/engine.yml)\nTOKEN_STATE='{token_state}'\n" + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE|${engine_files[*]-}"\n')
        output = proc.stdout + proc.stderr
    assert "python after all" not in output, output
    if expected == "refused":
        assert proc.returncode == 2, output
        assert "needs its bearer token" in output and "Nothing was started" in output
        assert "--engine python" not in output and "-Engine python" not in output, output
        assert ("moves to the Rust daemon after the install, and that move needs the token too" in output) == (switch == "rust"), output
        return
    assert proc.returncode == 0, output
    result = next(line for line in output.splitlines() if line.startswith("RESULT:"))
    engine, files = result[len("RESULT:"):].split("|")
    assert engine == expected and files


MOVE_NEEDS_TOKEN = "moves to the Rust daemon after the install, and that move needs the token too"


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("installed,bank", [("python", True), ("none", True), ("rust", True)],
                         ids=["python-installed", "existing-bank", "rust-installed"])
@pytest.mark.parametrize("token", ["set", "none"])
def test_a_host_still_on_python_moves_to_rust_after_the_install(tmp_path, script, installed, bank, token):
    # The Python daemon is retired: a host still on it, or a bank that exists
    # without a daemon, is brought up as it is and moved to Rust after the
    # install through pseudolife-mcp update from the shim; an accepted Rust
    # host stays. Without a bearer to come (no pipx or pip to mint one), the
    # move is refused before anything is pulled, and the refusal says so.
    ps = script.endswith(".ps1")
    if ps:
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        stage = stage.replace(definition, "").replace("function Test-ShimToolingReady(", "function Test-ShimToolingReady-Real(")
        shell = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{shell.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                f"    if ($args[0] -eq 'detect') {{ return '{installed}' }}\n"
                "    if ($args[0] -eq 'prepare') { return 'C:\\fixture\\engine.yml' }\n}\n"
                "function Test-ShimToolingReady($python) { return $false }\n"
                "function Get-EnvValue($name) { return $null }\n")
        token_env = "'fixture-bearer'" if token == "set" else "$null"
        proc = shell.run("$ClientOnly=$false\n$Engine=''\n$rustBearer=$true\n"
                         f"$Transport='shim'\n$env:PSEUDOLIFE_MCP_TOKEN={token_env}\n$env:PSEUDOLIFE_MCP_TOKENS=$null\n"
                         f"$bankExisted=${str(bank).lower()}\n" + stub + stage
                         + "Write-Output \"RESULT:$composeEngine|$switchEngine\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        stage = stage.replace(definition, "").replace("shim_tooling_ready() {", "shim_tooling_ready_real() {")
        shell = _Shell(variants[0], tmp_path)
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                f'case "$1" in detect) echo {installed} ;; prepare) echo /fixture/engine.yml ;; esac; }}\n'
                'step() { echo "STEP: $*"; }\nshim_tooling_ready() { return 1; }\nget_env() { :; }\n')
        token_env = "export PSEUDOLIFE_MCP_TOKEN=fixture-bearer\n" if token == "set" else "unset PSEUDOLIFE_MCP_TOKEN\n"
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE=''\nRUST_BEARER=1\nTRANSPORT=shim\n"
                         f"BANK_EXISTED='{'1' if bank else ''}'\nunset PSEUDOLIFE_MCP_TOKENS\n{token_env}" + stub + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE|$SWITCH_ENGINE"\n')
        output = proc.stdout + proc.stderr
    moves = installed != "rust"
    if token == "set":
        assert proc.returncode == 0, output
        assert (f"RESULT:{'python|rust' if moves else 'rust|'}" in output), output
        assert ("switching to rust after the install" in output) == moves, output
        return
    assert proc.returncode == 2, output
    assert "needs its bearer token" in output and "Nothing was pulled or started" in output, output
    assert "cannot mint one (no pipx or pip-capable Python" in output, output
    assert "--engine python" not in output and "-Engine python" not in output, output
    assert (MOVE_NEEDS_TOKEN in output) == moves, output
    if moves:
        assert "and the shim (pipx or a pip-capable Python >= 3.10)" in output, output
    assert [call for call in shell.logged() if "prepare" in call] == []


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("switch", ["rust", ""])
@pytest.mark.parametrize("token_state", ["no-shim", "failed"])
def test_stage_5b_tells_a_python_host_the_move_needs_the_token(tmp_path, script, switch, token_state):
    # A predicted bearer that was never minted, at stage 5b.
    ways = ("# >>> bearer refusal ways >>>", "# <<< bearer refusal ways <<<")
    stage = _stage(script, *ways) + _stage(script, "# >>> engine token >>>", "# <<< engine token <<<")
    compose = "python" if switch else "rust"
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        proc = ps.run("function Step($text) { Write-Output \"STEP: $text\" }\n"
                      f"$switchEngine='{switch}'\n$composeEngine='{compose}'\n"
                      f"$engineFiles=@('C:\\fixture\\engine.yml')\n$tokenState='{token_state}'\n" + stage
                      + "Write-Output \"RESULT:$composeEngine\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run('step() { echo "STEP: $*"; }\n'
                         f"SWITCH_ENGINE='{switch}'\nCOMPOSE_ENGINE={compose}\n"
                         f"engine_files=(/fixture/engine.yml)\nTOKEN_STATE='{token_state}'\n" + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE"\n')
        output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "needs its bearer token" in output and "Nothing was started" in output, output
    assert "--engine python" not in output and "-Engine python" not in output, output
    assert (MOVE_NEEDS_TOKEN in output) == (switch == "rust"), output



def test_a_token_only_change_recreates_the_daemon_before_the_switch(tmp_path, monkeypatch):
    # #1968 case 2's route: once the bearer is restored (set by hand or
    # minted), the rerun's stage 7 must recreate the Rust daemon with it
    # before the switch's update reads the daemon with that bearer
    # (rust/cli_harness/test_update_transactions.py
    # test_release_switch_off_rust_needs_the_bearer refuses a mismatch). The
    # plan sees the token through the resolved Compose config's hash.
    spec = importlib.util.spec_from_file_location("installer_state_token_plan", ROOT / "ops/installer_state.py")
    state = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(state)
    env = tmp_path / ".env"
    env.write_text("# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>\n"
                   "PSEUDOLIFE_INSTALL_EXTRACTOR=sidecar\n# <<< pseudolife-mcp install <<<\n", encoding="utf-8")
    override = tmp_path / "absent.yml"
    daemon = {"container_name": "pseudolife-mcp-daemon", "environment": {"PSEUDOLIFE_MCP_TOKEN": "old-bearer"}}
    config = {"services": {"daemon": daemon}}
    inventory = [{"Id": "fixture-id", "Name": "/pseudolife-mcp-daemon", "Image": "fixture-image",
                  "State": {"Running": True}, "Config": {"Labels": {"com.docker.compose.project": "pseudolife-mcp",
                  "com.docker.compose.service": "daemon", "com.docker.compose.config-hash": "fixture-config-hash"}}}]

    def run(argv):
        if argv[0] == "git":
            return "fixture-head" if "rev-parse" in argv else ""
        if argv[1] == "compose":
            return "daemon fixture-config-hash\n" if "--hash" in argv else json.dumps(config)
        if argv[1] == "ps":
            return "fixture-id"
        if argv[1] == "inspect":
            return json.dumps(inventory)
        pytest.fail("Unexpected Docker mutation")

    monkeypatch.setattr(state, "run", run)
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    recorded = state.started(tmp_path, env, override, expected=state.plan(tmp_path, env, override, snapshot=True))
    state.record(tmp_path, env, override, expected=recorded)
    assert state.plan(tmp_path, env, override) == "unchanged"
    daemon["environment"]["PSEUDOLIFE_MCP_TOKEN"] = "new-bearer"
    assert state.plan(tmp_path, env, override) == "start"


def test_foreign_states_prepares_refusal_and_touches_nothing(helper, capsys):
    # #1968: stage 4b asks this before its bearer refusal, so it must be
    # prepare's own refusal, read with no pull, run or write.
    module, state, repo = helper
    for release in ("0.24.0", None):  # another release, or a Rust container without labels
        state["labels"] = {eng.ENGINE_LABEL: "rust", eng.ACTIVATION_LABEL: "a",
                           **({eng.RELEASE_LABEL: release} if release else {})}
        with pytest.raises(module.Refusal) as prepared:
            module.prepare(repo, "rust")
        calls = len(state["calls"])
        assert module.main(["foreign", "--repo", str(repo)]) == 1
        out, err = capsys.readouterr()
        assert out == "" and err.strip() == str(prepared.value)
        assert "docker rm -f pseudolife-mcp-daemon" in err
        assert [c[0] for c in state["calls"][calls:]] == ["inspect"]
    # This checkout's own unfinished install, a Python container and no
    # container at all pass, as prepare would.
    for labels in ({eng.ENGINE_LABEL: "rust", eng.ACTIVATION_LABEL: "a", eng.RELEASE_LABEL: "0.23.0"},
                   {eng.ENGINE_LABEL: "python"}, None):
        state["labels"] = labels
        assert module.main(["foreign", "--repo", str(repo)]) == 0
        assert capsys.readouterr() == ("", "")
    assert not any(c[0] in ("pull", "run") or c[:2] == ["volume", "create"] for c in state["calls"])
    assert not module.overlay_path().exists()
    assert module.main(["foreign"]) == 1
    assert capsys.readouterr().err.strip() == "foreign needs --repo."


def _checkout_release():
    from rust.ci import release_version
    return release_version.from_manifest((ROOT / release_version.AUTHORITY).read_text(encoding="utf-8-sig"))


def _foreign_refusal(tmp_path, monkeypatch, labels):
    """What ops/installer_engine.py itself refuses for this container, or None."""
    spec = importlib.util.spec_from_file_location("installer_engine_refusal", ROOT / "ops/installer_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "refusal-data"))
    try:
        module.refuse_foreign_unaccepted(labels, _checkout_release())
    except module.Refusal as refusal:
        return str(refusal)
    return None


def _fake_docker(tmp_path, labels, calls):
    """A docker program for the real engine helper: `inspect` answers with
    the daemon container's labels (``None``: no such container), and anything
    else fails. Every call is logged to ``calls`` as ``docker|<args>``."""
    script = tmp_path / "fake_docker.py"
    script.write_text(
        "import json, sys\n"
        f"with open({str(calls)!r}, 'a', encoding='utf-8') as log:\n"
        "    log.write('docker|' + ' '.join(sys.argv[1:]) + '\\n')\n"
        f"labels = json.loads({json.dumps(json.dumps(labels))})\n"
        "if sys.argv[1:2] == ['inspect'] and '{{json .Config.Labels}}' in sys.argv:\n"
        "    if labels is None:\n"
        "        sys.exit('Error: No such object: pseudolife-mcp-daemon')\n"
        "    print(json.dumps(labels))\n"
        "    sys.exit(0)\n"
        "sys.exit('fake docker: unexpected call')\n", encoding="utf-8")
    if os.name == "nt":
        program = tmp_path / "docker.cmd"
        program.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        program = tmp_path / "docker"
        program.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        program.chmod(0o755)
    return str(program)


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("container,flag", [("foreign", ""), ("foreign", "rust"), ("unlabelled", ""),
                                            ("same-release", ""), ("same-release", "rust")])
def test_a_foreign_unfinished_rust_container_is_refused_before_the_bearer(tmp_path, monkeypatch, script, container,
                                                                           flag):
    # #1968 case 1: another release's unfinished Rust container meets the
    # same refusal from prepare whatever the token, so the bearer refusal's
    # fixes would only lead to it. The real helper answers every step here,
    # against a docker reporting that container's labels.
    labels = {eng.ENGINE_LABEL: "rust", eng.ACTIVATION_LABEL: "fixture-activation"}
    if container == "foreign":
        labels[eng.RELEASE_LABEL] = "0.0.1"
    elif container == "same-release":
        labels[eng.RELEASE_LABEL] = _checkout_release()
    expected = _foreign_refusal(tmp_path, monkeypatch, labels)
    assert (expected is None) == (container == "same-release")
    calls = tmp_path / "docker-calls.txt"
    docker = _fake_docker(tmp_path, labels, calls)
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        stage = stage.replace("function Test-ShimToolingReady(", "function Test-ShimToolingReady-Real(")
        ps = _PowerShell(tmp_path)
        proc = ps.run(f"$ClientOnly=$false\n$Engine='{flag}'\n$rustBearer=$true\n$Transport='shim'\n"
                      "$env:PSEUDOLIFE_MCP_TOKEN=$null\n$env:PSEUDOLIFE_MCP_TOKENS=$null\n"
                      "$engineDefaulted=$false\n$bankExisted=$true\n"
                      "function Test-ShimToolingReady($python) { return $false }\n"
                      "function Get-EnvValue($name) { return $null }\n" + stage
                      + "Write-Output \"RESULT:$composeEngine\"\n", extra_env={"PSEUDOLIFE_DOCKER": docker})
        output, python_way = _output(proc), "or rerun with -Engine python."
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        stage = stage.replace("shim_tooling_ready() {", "shim_tooling_ready_real() {")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nRUST_BEARER=1\nTRANSPORT=shim\nBANK_EXISTED=1\n"
                         "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS\n"
                         'step() { echo "STEP: $*"; }\nshim_tooling_ready() { return 1; }\nget_env() { :; }\n'
                         + stage + 'echo "RESULT:$COMPOSE_ENGINE"\n', extra_env={"PSEUDOLIFE_DOCKER": docker})
        output, python_way = proc.stdout + proc.stderr, "or rerun with --engine python."
    logged = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    # The real detection ran, and nothing but inspection reached docker.
    assert logged and all(line.startswith("docker|inspect") for line in logged), logged
    if expected is not None:
        assert proc.returncode == 1, output
        assert expected in output, output
        assert "needs its bearer token" not in output, output
    else:
        # The near miss: this checkout's own unfinished install keeps the
        # bearer refusal, and no Python rerun is offered (the Python daemon
        # is retired).
        assert proc.returncode == 2, output
        assert "needs its bearer token" in output and "Nothing was pulled or started" in output, output
        assert python_way not in output, output
        assert "engine python" not in output, output
        assert "docker rm -f" not in output, output
    assert "RESULT:" not in output


def test_accept_refuses_a_container_this_install_did_not_start(helper):
    module, state, repo = helper
    module.prepare(repo, "rust")
    state["labels"] = {eng.ENGINE_LABEL: "rust", eng.RELEASE_LABEL: "0.23.0", eng.ACTIVATION_LABEL: "someone-else"}
    with pytest.raises(module.Refusal, match="not the one"):
        module.accept()
    assert not eng.release_accepted().exists()


def test_python_and_checkout_selections(helper):
    module, state, repo = helper
    assert module.prepare(repo, "python") == []
    state["labels"] = {eng.ENGINE_LABEL: "python"}
    assert module.detect() == "python"
    state["labels"] = {eng.ENGINE_LABEL: "rust", eng.CHECKOUT_LABEL: str(repo), eng.ACTIVATION_LABEL: "a"}
    assert module.detect() == "rust-checkout"
    # The installer never rebuilds a checkout-mode Rust daemon: update does.
    with pytest.raises(module.Refusal, match="update --checkout"):
        module.prepare(repo, "rust-checkout")
    # With its container removed, the checkout's own selection still says Rust.
    state["labels"] = None
    (repo / "data/update-engine").mkdir(parents=True)
    (repo / eng.SELECTION).write_text("{}")
    assert module.detect(repo) == "rust-checkout"
    assert module.detect() == "none"


def test_unreleased_checkout_version_refuses_rust(helper):
    module, state, repo = helper
    _write_crate(repo, "0.23.0.dev1")
    with pytest.raises(module.Refusal, match="check out a release tag"):
        module.prepare(repo, "rust")
    assert not state["calls"] or all(c[0] == "inspect" for c in state["calls"])


def test_the_checkout_version_is_the_native_crate_not_pyproject(helper):
    """pyproject.toml leaves with the Python package; the crate's version is
    the one read, and a checkout without it is refused naming the file."""
    module, state, repo = helper
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.99.0"\n')
    assert module.checkout_version(repo) == "0.23.0"
    message = "this checkout's rust/shim/Cargo.toml; check out a release tag"
    (repo / "rust" / "shim" / "Cargo.toml").write_text('[package]\nname = "pseudolife-stdio"\n')
    with pytest.raises(module.Unavailable, match=message):
        module.checkout_version(repo)
    # A missing or undecodable crate file means a broken checkout: refused
    # with the step that repairs it, never the default's fallback to Python.
    restore = r"restore it \(git checkout -- rust/shim/Cargo.toml\) and rerun"
    (repo / "rust" / "shim" / "Cargo.toml").write_bytes(b"[package]\nversion = \"\xff\"\n")
    with pytest.raises(module.Refusal, match=r"Cannot read rust/shim/Cargo.toml in this checkout \(not UTF-8 text\); "
                       + restore) as refused:
        module.checkout_version(repo)
    assert not isinstance(refused.value, module.Unavailable)
    (repo / "rust" / "shim" / "Cargo.toml").unlink()
    with pytest.raises(module.Refusal, match=restore) as refused:
        module.checkout_version(repo)
    assert not isinstance(refused.value, module.Unavailable)


def test_planner_checks_the_engine_overlays_it_was_given(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("installer_state_overlays", ROOT / "ops/installer_state.py")
    state = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(state)
    seen = []

    def run(argv):
        seen.append(argv)
        if argv[0] == "git":
            return "head" if "rev-parse" in argv else ""
        if "--hash" in argv:
            return ""
        if argv[1] == "compose":
            return json.dumps({"services": {}})
        return ""

    monkeypatch.setattr(state, "run", run)
    overlay = tmp_path / "docker-compose.engine-installer.yml"
    state.deployment(tmp_path, tmp_path / ".env", tmp_path / "absent.yml", [overlay])
    compose = [argv for argv in seen if argv[1:2] == ["compose"]]
    assert compose and all(argv[argv.index(str(overlay)) - 1] == "-f" for argv in compose)


def _stage(script, start, end):
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    return source[source.index(start):source.index(end)]


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("installed,flag,bank,compose_engine,switch", [
    # A new install runs Rust.
    ("none", "", "", "rust", ""), ("none", "rust", "", "rust", ""),
    # An existing bank with no daemon, and a host still on the retired Python
    # daemon, come up as they are and reach Rust through update's checks.
    ("none", "", "1", "python", "rust"), ("none", "rust", "1", "python", "rust"),
    ("python", "", "1", "python", "rust"), ("python", "rust", "1", "python", "rust"),
    ("rust", "", "1", "rust", ""), ("rust", "rust", "1", "rust", ""),
    ("rust-checkout", "", "1", "rust-checkout", ""),
    ("rust-unaccepted", "", "1", "rust", ""),
])
def test_engine_stage_keeps_the_installed_engine_and_defers_a_switch(tmp_path, script, installed, flag, bank,
                                                                     compose_engine, switch):
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        ps = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{ps.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                f"    if ($args[0] -eq 'detect') {{ return '{installed}' }}\n"
                "    if ($args[4] -ne 'python') { return 'C:\\fixture\\engine.yml' }\n}\n")
        proc = ps.run(f"$ClientOnly=$false\n$Engine='{flag}'\n$rustBearer=$true\n$engineDefaulted=$false\n"
                      "$env:PSEUDOLIFE_MCP_TOKEN='fixture-bearer'\n"
                      f"$bankExisted=${'true' if bank else 'false'}\n" + stub
                      + stage.replace(definition, "")
                      + "Write-Output \"RESULT:$composeEngine|$switchEngine|$($engineFiles -join ',')\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        shell = _Shell(variants[0], tmp_path)
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                f'case "$1" in detect) echo {installed} ;; prepare) [ "$5" = python ] || echo /fixture/engine.yml ;; esac; }}\n'
                'step() { echo "STEP: $*"; }\n')
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nRUST_BEARER=1\nBANK_EXISTED='{bank}'\n"
                         "PSEUDOLIFE_MCP_TOKEN=fixture-bearer\n" + stub
                         + stage.replace(definition, "")
                         + 'echo "RESULT:$COMPOSE_ENGINE|$SWITCH_ENGINE|${engine_files[*]-}"\n')
        output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    result = next(line for line in output.splitlines() if line.startswith("RESULT:"))
    engine, switched, files = result[len("RESULT:"):].split("|")
    assert (engine, switched) == (compose_engine, switch)
    assert bool(files) == (compose_engine != "python")
    assert ("unchanged" in output) == (installed not in ("none", "rust-unaccepted") and not switch)
    assert ("switching to" in output) == bool(switch)


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_compose_and_planner_carry_the_engine_overlays(tmp_path, script):
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 7. bring the stack up", "# -- 8b. Claude Code plugin")
        ps = _PowerShell(tmp_path)
        state = (f"function Invoke-InstallerState {{ Add-Content -LiteralPath '{ps.calls}' -Value ('state|' + (@($args) -join ' ')); "
                 "'{\"action\":\"start\"}' }\n")
        proc = ps.run("$ClientOnly=$false\n$envFile='fixture.env'\n$composeFile='fixture.yml'\n$overrideFile='missing.yml'\n"
                      "$engineFiles=@('C:\\fixture\\engine.yml')\n" + state + ps.stub_function("docker") + stage)
        calls, output, overlay = ps.logged(), _output(proc), "C:\\fixture\\engine.yml"
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        stage = _stage(script, "# ── 7. bring the stack up", "# ── 8b. Claude Code plugin")
        proc = shell.run("CLIENT_ONLY=''\nenv_file=fixture.env\ncompose_file=fixture.yml\noverride_file=missing.yml\n"
                         "engine_files=(/fixture/engine.yml)\n"
                         'installer_state() { printf "state|%s\\n" "$*" >>"$CALL_LOG"; echo \'{"action":"start"}\'; }\n'
                         + stage)
        calls, output, overlay = shell.logged(), proc.stdout + proc.stderr, "/fixture/engine.yml"
    assert proc.returncode == 0, output
    [up_call] = [call for call in calls if " up " in call]
    assert f"-f {overlay}" in up_call
    planner = [call for call in calls if call.startswith("state|")]
    assert len(planner) == 2 and all(f"--overlay {overlay}" in call for call in planner)


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("case", ["client-only", "no-token", "http-without-token", "allowed", "http-with-token",
                                  "http-env-token"])
def test_engine_flag_refusals(tmp_path, script, case):
    client, token = case == "client-only", case == "no-token"
    http = case.startswith("http")
    # A bearer exported in the caller's environment reaches Compose too.
    extra = {"PSEUDOLIFE_MCP_TOKEN": "fixture"} if case == "http-env-token" else {}
    env = tmp_path / "fixture.env"
    env.write_text("PSEUDOLIFE_MCP_TOKEN=fixture\n" if case == "http-with-token" else "# no token\n")
    if script.endswith(".ps1"):
        stage = _stage(script, "# >>> engine flag >>>", "# <<< engine flag <<<")
        ps = _PowerShell(tmp_path)
        # PowerShell's ValidateSet lets "Rust" through: the block lowercases it.
        proc = ps.run(f"$ClientOnly=${str(client).lower()}\n$NoToken=${str(token).lower()}\n$Engine='Rust'\n"
                      f"$Transport='{'http' if http else 'shim'}'\n$envFile='{env}'\n"
                      + stage + "Write-Output \"ENGINE:$Engine\"\n", extra_env=extra)
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# >>> engine flag >>>", "# <<< engine flag <<<")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run(f"CLIENT_ONLY='{'1' if client else ''}'\nNO_TOKEN='{'1' if token else ''}'\n"
                         f"TRANSPORT={'http' if http else 'shim'}\nenv_file='{shell.path(env)}'\n"
                         "ENGINE=rust\n" + stage + 'echo "ENGINE:$ENGINE"\n', extra_env=extra)
        output = proc.stdout + proc.stderr
    if case in ("allowed", "http-with-token", "http-env-token"):
        assert proc.returncode == 0, output
        assert "ENGINE:rust" in output
    else:
        assert proc.returncode == 2
        assert ("client-only" if client else "token") in output.lower()
        assert "Nothing was changed" in output


@pytest.mark.parametrize("script", ["install.sh", "install.ps1", "install-native.sh", "install-native.ps1"])
def test_engine_python_is_refused_before_anything_changes(tmp_path, script):
    # The Python daemon is retired (maintainer, 27 November 2026): every
    # installer refuses --engine python in its flag block, before preflight,
    # so nothing is pulled, created or switched; the near miss --engine rust
    # passes the same block (test_engine_flag_refusals "allowed").
    stage = _stage(script, "# >>> engine flag >>>", "# <<< engine flag <<<")
    env = tmp_path / "fixture.env"
    env.write_text("PSEUDOLIFE_MCP_TOKEN=fixture\n")
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        # ValidateSet lets "Python" through: the block lowercases it first.
        proc = ps.run("$ClientOnly=$false\n$NoToken=$false\n$Engine='Python'\n"
                      f"$Transport='shim'\n$envFile='{env}'\n"
                      + stage + "Write-Output \"ENGINE:$Engine\"\n")
        output, flag = _output(proc), "-Engine python"
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run(f"CLIENT_ONLY=''\nNO_TOKEN=''\nTRANSPORT=shim\nenv_file='{shell.path(env)}'\n"
                         "ENGINE=python\n" + stage + 'echo "ENGINE:$ENGINE"\n')
        output, flag = proc.stdout + proc.stderr, "--engine python"
    assert proc.returncode == 2, output
    assert f"{flag} is retired" in output and "Nothing was changed" in output, output
    assert "ENGINE:" not in output, output


def test_no_volume_is_created_before_the_engine_stage_can_refuse():
    # A refused Rust install must leave nothing a rerun's preflight would refuse.
    for script in ("install.sh", "install.ps1"):
        source = (ROOT / "ops" / script).read_text(encoding="utf-8")
        engine = source.index("# >>> daemon engine >>>")
        create = source.index("# >>> create volumes >>>")
        assert engine < create, script
        calls = [i for i, line in enumerate(source.splitlines()) if line.strip().startswith("docker volume create")]
        assert len(calls) == 1, script
        assert calls[0] > len(source[:create].splitlines()), script


def test_the_engine_flag_is_checked_after_client_only_is_inferred():
    for script, flag, first in (("install.sh", "# >>> engine flag >>>", "# ── 1. provider selection"),
                                ("install.ps1", "# >>> engine flag >>>", "# -- 1. provider selection")):
        source = (ROOT / "ops" / script).read_text(encoding="utf-8")
        inferred = source.rindex("CLIENT_ONLY=1" if script.endswith(".sh") else "$script:ClientOnly = [switch]$true")
        assert inferred < source.index(flag) < source.index(first), script


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_the_container_probe_uses_what_the_daemon_image_has(tmp_path, script, engine):
    # The Rust image has curl and no Python.
    if script.endswith(".ps1"):
        source = (ROOT / "ops" / script).read_text(encoding="utf-8")
        start = source.index("function Invoke-EndpointContainerProbe")
        end = source.index("\n}\n", start) + 3
        ps = _PowerShell(tmp_path)
        proc = ps.run(f"$composeEngine='{engine}'\n$Extractor='endpoint'\n$ExtractorUrl='http://127.0.0.1:11434/v1'\n"
                      "function ConvertTo-ExtractorContainerUrl($u) { 'http://host.docker.internal:11434/v1' }\n"
                      "function Test-LinuxHost { $false }\n" + ps.stub_function("docker") + source[start:end]
                      + "Invoke-EndpointContainerProbe\n")
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        source = (ROOT / "ops" / script).read_text(encoding="utf-8")
        start = source.index("container_http_probe() {")
        end = source.index("\n}\n", source.index("    esac", start)) + 3
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run(f"COMPOSE_ENGINE={engine}\n" + source[start:end]
                         + "container_http_probe http://host.docker.internal:11434/v1\n")
        calls, output = shell.logged(), proc.stdout + proc.stderr
    [probe] = [call for call in calls if call.startswith("docker|exec")]
    assert (" curl " in probe) == (engine == "rust"), output
    assert (" python " in probe) == (engine == "python"), output


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("version", ["0.22.1", ""])
def test_the_delegated_switch_runs_update_with_stdin_closed(tmp_path, script, version):
    if script.endswith(".ps1"):
        stage = _stage(script, "# >>> engine switch >>>", "# <<< engine switch <<<")
        ps = _PowerShell(tmp_path)
        # A native program, as the real shim is: `$null |` closes its stdin.
        script = tmp_path / "fake_shim.py"
        script.write_text("import sys\nread = sys.stdin.read()\n"
                          f"open({str(ps.calls)!r}, 'a').write('shim|' + ' '.join(sys.argv[1:]) + '|stdin=' + str(len(read)) + chr(10))\n")
        if os.name == "nt":
            fake = tmp_path / "fake_shim.cmd"
            fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        else:
            fake = tmp_path / "fake_shim"
            fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
            fake.chmod(0o755)
        proc = ps.run(f"$switchEngine='rust'\n$installedEngine='python'\n"
                      f"function Install-ShimOnce {{ $true }}\n$script:shimInstallPath='{fake}'\n"
                      f"function global:Invoke-RestMethod {{ @{{ version = '{version}' }} }}\n" + stage,
                      stdin_text="y\n")
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        fake = tmp_path / "fake_shim.sh"
        fake.write_text('#!/bin/sh\nread -r answer || answer=EOF\nprintf "shim|%s|stdin=%s\\n" "$*" "$answer" >>"$CALL_LOG"\n')
        fake.chmod(0o755)
        stage = _stage(script, "# >>> engine switch >>>", "# <<< engine switch <<<")
        proc = shell.run(f"SWITCH_ENGINE=rust\nINSTALLED_ENGINE=python\nrepo=/fixture/repo\n"
                         f"SHIM_OK=1\nSHIM_PATH='{shell.path(fake)}'\nensure_shim() {{ :; }}\n"
                         "step() { echo \"STEP: $*\"; }\n"
                         f"export FAKE_HEALTH='{{\"status\":\"ok\",\"version\":\"{version}\"}}'\n" + stage)
        calls, output = shell.logged(), proc.stdout + proc.stderr
    if not version:
        # Without a version, update would move to the newest release instead.
        assert proc.returncode == 1, output
        assert "could not be read" in output and not [c for c in calls if c.startswith("shim|")]
        return
    assert proc.returncode == 0, output
    [call] = [c for c in calls if c.startswith("shim|")]
    assert "update --engine rust --daemon-only --tag 0.22.1" in call
    assert call.endswith("stdin=EOF") or call.endswith("stdin=0"), call


def test_both_installers_pass_the_helper_its_repo():
    # detect needs the checkout to see a checkout-mode selection after `down`.
    for script, call in (("install.sh", 'engine_helper detect --repo "$repo"'),
                         ("install.ps1", "Invoke-EngineHelper detect --repo $repo")):
        assert call in (ROOT / "ops" / script).read_text(encoding="utf-8"), script


def test_update_treats_the_installer_overlay_as_its_own_transient_file(tmp_path):
    assert eng.transient_overlay(up.data_dir() / "update-engine" / "docker-compose.engine-installer.yml")


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("case", ["no-bearer", "rust-unavailable"])
def test_no_default_falls_back_to_python(tmp_path, script, case):
    # The Python daemon is retired: without a bearer the install refuses
    # before it asks for anything, and an engine step that prepares no Rust
    # overlay stops the install instead of starting the base file's image.
    bearer = case != "no-bearer"
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        ps = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{ps.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                "    if ($args[0] -eq 'detect') { return 'none' }\n}\n")
        proc = ps.run(f"$ClientOnly=$false\n$Engine=''\n$rustBearer=${str(bearer).lower()}\n$engineDefaulted=$false\n"
                      "$env:PSEUDOLIFE_MCP_TOKEN='fixture-bearer'\n"
                      "$bankExisted=$false\n" + stub + stage.replace(definition, "")
                      + "Write-Output \"RESULT:$composeEngine\"\n")
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        shell = _Shell(variants[0], tmp_path)
        # The helper's prepare prints no overlay: Rust was unavailable.
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                'case "$1" in detect) echo none ;; esac; }\n'
                'step() { echo "STEP: $*"; }\n')
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE=''\nRUST_BEARER='{'1' if bearer else ''}'\nBANK_EXISTED=''\n"
                         "PSEUDOLIFE_MCP_TOKEN=fixture-bearer\n" + stub
                         + stage.replace(definition, "") + 'echo "RESULT:$COMPOSE_ENGINE"\n')
        calls, output = shell.logged(), proc.stdout + proc.stderr
    assert "RESULT:" not in output, output
    prepares = [call for call in calls if "prepare" in call]
    if not bearer:
        assert proc.returncode == 2, output
        assert "needs its bearer token" in output and prepares == [], output
        return
    assert proc.returncode == 1, output
    assert "prepared no Rust daemon overlay" in output, output
    [prepare] = prepares
    assert "--engine rust" in prepare and "--default" not in prepare, prepare
