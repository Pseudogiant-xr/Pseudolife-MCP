"""The executed parent isolation guards participate in receipt admission."""
from pathlib import Path
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evals.rust_port import cli_process, provenance
from evals.rust_port.test_cli_public_summary import receipt


PARENTS = ("evals/memory_policy_bench.py", "evals/memory_policy_daemon.py")


def helper_maps(monkeypatch):
    maps = []
    monkeypatch.setattr(cli_process, "require_instrument_binding",
                        lambda root, helpers: maps.append((Path(root), helpers)) or {})
    cli_process.cli_binding(provenance.ROOT, provenance.ROOT)
    return maps


def test_binding_includes_actual_parent_guards_and_child_adapter(monkeypatch):
    from evals import memory_policy_bench, memory_policy_daemon
    from evals.rust_baseline import common, daemon_child
    from evals.rust_port import stdio_daemon
    instrument = helper_maps(monkeypatch)[0][1]
    assert memory_policy_bench.scrubbed_env in instrument[PARENTS[0]]
    assert memory_policy_bench.production_database in instrument[PARENTS[0]]
    assert common.child_environment in instrument["evals/rust_baseline/common.py"]
    for guard in (memory_policy_daemon.check_database, memory_policy_daemon.check_port,
                  memory_policy_daemon._server_check):
        assert guard in instrument[PARENTS[1]]
    for guard in (daemon_child.check_database, daemon_child.check_port, daemon_child._server_check):
        assert guard in instrument[PARENTS[1]]
    assert stdio_daemon.main in instrument["evals/rust_port/stdio_daemon.py"]


def test_transitive_production_guard_and_admin_resolver_have_selected_root(monkeypatch):
    from pseudolife_memory.storage import schema
    from tests.pg_defaults import default_admin_url
    from tests.fake_embedder import FakeSentenceTransformer
    maps = helper_maps(monkeypatch)
    assert len(maps) == 2
    root, production = maps[1]
    assert root == provenance.ROOT
    for guard in (schema.dsn_database_name, schema.refuse_production_database, schema.assert_disposable_database):
        assert guard in production["pseudolife_memory/storage/schema.py"]
    assert default_admin_url in production["tests/pg_defaults.py"]
    assert FakeSentenceTransformer in production["tests/fake_embedder.py"]
    assert "tests/fake_embedder.py" in provenance.SOURCE_PATHS


@pytest.mark.parametrize("name", PARENTS)
@pytest.mark.parametrize("field", ["source_files_sha256", "source_files_git_blob"])
def test_public_receipt_refuses_omitted_parent_identity(name, field):
    raw, _, _ = receipt()
    raw["cli_instrument_binding"]["instrument"][field].pop(name, None)
    with pytest.raises(ValueError):
        cli_process.public_summary(raw)


@pytest.mark.parametrize("name", PARENTS)
def test_source_metadata_includes_parent_dirty_scope_and_hashes(tmp_path, monkeypatch, name):
    for relative in PARENTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# disposable parent helper\n")
    monkeypatch.setattr(provenance, "ROOT", tmp_path)

    def git(root, *args):
        dirty = args[:2] == ("status", "--porcelain") and name in args
        return SimpleNamespace(returncode=0, stdout=" M " + name if dirty else "")

    monkeypatch.setattr(provenance, "_git", git)
    result = provenance.source_metadata(Path(__file__).resolve().parents[2])
    assert result["instrument_dirty"] is True
    assert Path(name).name in result["instrument_sha256"]


def committed_paths(tmp_path, names):
    root = tmp_path / "instrument"
    root.mkdir()
    paths = {}
    for relative in names:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# disposable committed helper\n")
        paths[relative] = [path]
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    tree = subprocess.check_output(["git", "write-tree"], cwd=root).decode().strip()
    commit = subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                      "commit-tree", tree, "-m", "fixture"], cwd=root).decode().strip()
    subprocess.run(["git", "update-ref", "HEAD", commit], cwd=root, check=True)
    return root, paths


@pytest.mark.parametrize("name", PARENTS)
@pytest.mark.parametrize("mutation", ["dirty", "missing", "foreign"])
def test_parent_counterfactual_refuses_binding_without_service(tmp_path, monkeypatch, name, mutation):
    instrument = helper_maps(monkeypatch)[0][1]
    assert name in instrument
    root, paths = committed_paths(tmp_path, instrument)
    provenance.require_instrument_binding(root, paths)
    helper = root / name
    if mutation == "dirty":
        helper.write_text("# changed isolation implementation\n")
    elif mutation == "missing":
        helper.unlink()
    else:
        foreign = tmp_path / "foreign.py"
        foreign.write_text("def foreign_guard():\n    return None\n")
        namespace = {}
        exec(compile(foreign.read_text(), str(foreign), "exec"), namespace)
        paths[name] = [namespace["foreign_guard"]]
    with pytest.raises(RuntimeError):
        provenance.require_instrument_binding(root, paths)


@pytest.mark.parametrize("name", ["pseudolife_memory/storage/schema.py", "tests/pg_defaults.py"])
@pytest.mark.parametrize("field", ["source_files_sha256", "source_files_git_blob"])
def test_public_receipt_requires_transitive_production_guard_identity(name, field):
    raw, _, _ = receipt()
    raw["cli_instrument_binding"]["production_ownership"][field].pop(name)
    with pytest.raises(ValueError):
        cli_process.public_summary(raw)


def test_baseline_provenance_hashes_the_executed_parent_files():
    from evals.rust_baseline import common
    metadata = common.provenance(source_root=provenance.ROOT)
    for name in PARENTS:
        assert metadata["instrument_sha256"][name] == hashlib.sha256((provenance.ROOT / name).read_bytes()).hexdigest()


def test_public_projection_retains_parent_and_transitive_hashes_without_paths():
    raw, private_path, encoded = receipt()
    summary = cli_process.public_summary(raw)
    for field in ("source_files_sha256", "source_files_git_blob"):
        for name in (*PARENTS, "evals/rust_port/stdio_daemon.py"):
            assert summary["instrument_binding"][field][name] == raw["cli_instrument_binding"]["instrument"][field][name]
        for name in ("pseudolife_memory/storage/schema.py", "tests/pg_defaults.py"):
            assert summary["production_ownership"][field][name] == raw["cli_instrument_binding"]["production_ownership"][field][name]
    import json
    serialized = json.dumps(summary)
    assert private_path not in serialized and encoded not in serialized


def test_binding_checks_the_environment_alias_actually_used_by_launcher(tmp_path, monkeypatch):
    from evals.rust_baseline import daemon
    foreign = tmp_path / "foreign.py"
    foreign.write_text("def child_environment(private):\n    return {}\n")
    namespace = {}
    exec(compile(foreign.read_text(), str(foreign), "exec"), namespace)
    monkeypatch.setattr(daemon, "child_environment", namespace["child_environment"])
    instrument = helper_maps(monkeypatch)[0][1]
    assert namespace["child_environment"] in instrument["evals/rust_baseline/common.py"]
    root, _ = committed_paths(tmp_path, ["evals/rust_baseline/common.py"])
    with pytest.raises(RuntimeError, match="another tree"):
        provenance.require_instrument_binding(root, {
            "evals/rust_baseline/common.py": [namespace["child_environment"]]})


def test_actual_loaded_references_belong_to_their_mapped_source(monkeypatch):
    import contextlib
    maps = helper_maps(monkeypatch)
    for root, helpers in maps:
        for relative, loaded in helpers.items():
            for helper in loaded:
                # The collector is the only replaced reference; all guard,
                # adapter and production references remain the actual imports.
                if helper is cli_process.require_instrument_binding:
                    continue
                if callable(helper):
                    if inspect.getsourcefile(helper) == contextlib.__file__:
                        helper = helper.__wrapped__
                    helper = inspect.getsourcefile(helper)
                assert Path(helper).resolve() == (root / relative).resolve()


@pytest.mark.parametrize("field", ["source_files_sha256", "source_files_git_blob"])
def test_fake_embedder_source_identity_is_required_in_public_summary(field):
    raw, _, _ = receipt()
    raw["cli_instrument_binding"]["production_ownership"][field].pop("tests/fake_embedder.py", None)
    with pytest.raises(ValueError):
        cli_process.public_summary(raw)


def test_actual_corpus_helper_binding_admits_committed_clean_disposable_tree(tmp_path):
    root = tmp_path / "actual-instrument"
    for directory in ("rust_port", "rust_baseline"):
        shutil.copytree(provenance.ROOT / "evals" / directory, root / "evals" / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("memory_policy_bench.py", "memory_policy_daemon.py",
                 "memory_policy_scenarios.py", "embedder_stamp.py"):
        shutil.copy2(provenance.ROOT / "evals" / name, root / "evals" / name)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    tree = subprocess.check_output(["git", "write-tree"], cwd=root).decode().strip()
    commit = subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                      "commit-tree", tree, "-m", "fixture"], cwd=root).decode().strip()
    subprocess.run(["git", "update-ref", "HEAD", commit], cwd=root, check=True)
    code = ("import evals,sys,json; "
            f"evals.__path__=[{str(root / 'evals')!r}]; "
            f"sys.path.insert(0,{str(provenance.ROOT)!r}); "
            "from evals.rust_port.cli_process import cli_binding; "
            "from evals.rust_baseline import daemon,transport; "
            "from evals.rust_port.full_bank import private_home_overrides; "
            "from pathlib import Path; "
            "helpers={'evals/rust_baseline/daemon.py':[daemon.disposable_database,daemon.launched_daemon,daemon.private_directory],"
            "'evals/rust_baseline/daemon_child.py':[Path(daemon.__file__).with_name('daemon_child.py')],"
            "'evals/rust_baseline/transport.py':[Path(transport.__file__)],"
            "'evals/rust_port/full_bank.py':[private_home_overrides]}; "
            f"binding=cli_binding(Path.cwd(),Path({str(provenance.ROOT)!r}),extra_helpers=helpers); "
            "print(json.dumps(binding))")
    process = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    binding = json.loads(process.stdout)
    assert binding["instrument"]["source_head"] == commit
    assert binding["instrument"]["source_dirty"] is False
    assert len(binding["instrument"]["source_files_sha256"]) == 17
    assert "tests/fake_embedder.py" in binding["production_ownership"]["source_files_sha256"]
