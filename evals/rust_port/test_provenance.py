"""Historical evidence stays distinct from the source a new process imports."""
import pytest

from evals.rust_port import provenance


def test_schema_is_read_without_importing_production_code(tmp_path):
    path = tmp_path / "schema.py"
    path.write_text("raise RuntimeError('must not import')\nSCHEMA_META_VERSION = 53\n")
    assert provenance.schema_version(path) == 53
    path.write_text("SCHEMA_META_VERSION = dynamic_value\n")
    with pytest.raises(ValueError):
        provenance.schema_version(path)


def test_current_source_and_historical_oracle_are_distinct():
    metadata = provenance.source_metadata()
    assert metadata["historical_oracle"] == {"source_head": provenance.HISTORICAL_HEAD, "source_schema": 52}
    assert len(metadata["source_head"]) == 40
    assert type(metadata["source_schema"]) is int
    assert type(metadata["production_source_matches_historical"]) is bool


def test_instrument_only_commit_does_not_require_old_git_head(monkeypatch):
    monkeypatch.setattr(provenance, "source_metadata", lambda root: {
        "source_head": "a" * 40, "source_schema": 52,
        "production_source_matches_historical": True})
    assert provenance.require_historical_source()["source_head"] == "a" * 40


@pytest.mark.parametrize("schema,matches", [(53, True), (52, False)])
def test_historical_execution_refuses_schema_or_source_drift(monkeypatch, schema, matches):
    monkeypatch.setattr(provenance, "source_metadata", lambda root: {
        "source_schema": schema, "production_source_matches_historical": matches})
    with pytest.raises(RuntimeError, match="historical schema-52 oracle"):
        provenance.require_historical_source()


def test_full_bank_refuses_before_importing_or_launching_daemon(monkeypatch):
    from evals.rust_port import full_bank
    def refuse(root):
        raise RuntimeError("historical schema-52 oracle")
    monkeypatch.setattr(full_bank, "require_historical_source", refuse)
    with pytest.raises(RuntimeError, match="historical schema-52 oracle"):
        full_bank.run({}, "unused")


def test_declared_root_cannot_relabel_imports_from_another_tree(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(provenance.importlib.util, "find_spec", lambda name: SimpleNamespace(
        origin=str(tmp_path / "actual" / "pseudolife_memory" / "__init__.py")))
    with pytest.raises(RuntimeError, match="production import root"):
        provenance.require_import_root(tmp_path / "declared")
    provenance.require_import_root(tmp_path / "actual")


def test_module_routing_uses_selected_source_without_writing_it(tmp_path):
    import json
    import subprocess
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "__init__.py").write_text("")
    (tmp_path / "pseudolife_memory").mkdir()
    (tmp_path / "pseudolife_memory" / "__init__.py").write_text("SCHEMA = 52\n")
    (tmp_path / "pseudolife_memory" / "probe.py").write_text(
        "import json; from pseudolife_memory import SCHEMA; print(json.dumps({'schema':SCHEMA}))\n")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.py")}
    result = subprocess.run(provenance.module_command("pseudolife_memory.probe", tmp_path),
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"schema": 52}
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.py")}


def test_full_bank_forwards_oracle_root_to_both_fresh_arm_launches(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import sys
    import tempfile
    from types import ModuleType
    from evals.rust_port import full_bank
    common, daemon, transport = (ModuleType(name) for name in ("common", "daemon", "transport"))
    common.lease_gate = lambda stamp: {"checked": True}
    transport.TOKEN = "synthetic"
    roots = []

    @contextmanager
    def database():
        yield "synthetic-disposable"

    @contextmanager
    def launcher(dsn, private, *, env_extra=None, source_root=None):
        roots.append(source_root)
        cleanup = {}
        yield None, "http://127.0.0.1:1", cleanup
        cleanup.update(daemon_stopped=True, children_stopped=True)

    daemon.disposable_database, daemon.launched_daemon = database, launcher
    daemon.private_directory = tempfile.TemporaryDirectory
    for name, module in (("common", common), ("daemon", daemon), ("transport", transport)):
        monkeypatch.setitem(sys.modules, "evals.rust_baseline." + name, module)
    monkeypatch.setattr(full_bank, "require_historical_source", lambda root: None)
    monkeypatch.setattr(full_bank, "environment_metadata", lambda root: {"selected": root.name})
    monkeypatch.setattr(full_bank, "private_home_overrides", lambda private: {})
    monkeypatch.setattr(full_bank, "observe", lambda corpus, url, token: [
        {"id": "probe", "expect": {}, "response": {"status": 200, "body": {}}}])
    _, receipt = full_bank.run({"seed": 1, "scope": "synthetic test"}, "verified", oracle_root=tmp_path)
    assert roots == [tmp_path, tmp_path]
    assert receipt["status"] == "passed"
    assert all(c["database_dropped"] for c in receipt["cleanup"])
