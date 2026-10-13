"""A full-bank capture binds both caller and launcher execution sources."""
import sys
from evals.rust_port import full_bank
from evals.rust_baseline import common


def test_full_bank_retains_complete_caller_and_launcher_hashes(monkeypatch):
    monkeypatch.setattr(full_bank, "source_metadata", lambda root: {
        "instrument_sha256": {"processes.py": "caller-hash", "full_seed.json": "seed-hash"},
        "process_helper_sha256": {"processes.py": "caller-hash"}})
    monkeypatch.setattr(common, "provenance", lambda **kw: {
        "instrument_sha256": {"evals/rust_baseline/daemon.py": "launcher-hash"}})
    monkeypatch.setattr(full_bank, "runtime_metadata", lambda root: {"python": "parent-fixture"})
    metadata = full_bank.environment_metadata()
    assert metadata["instrument_sha256"] == {"processes.py": "caller-hash", "full_seed.json": "seed-hash"}
    assert metadata["baseline_instrument_sha256"] == {"evals/rust_baseline/daemon.py": "launcher-hash"}
    assert metadata["parent_runtime"] == {"python": "parent-fixture"}


def test_full_bank_cli_forwards_offline_clearance_without_a_board_timestamp(tmp_path, monkeypatch):
    calls = []
    def run(corpus, board_checked_at, tolerance, root, *, offline_resource_checked_at, **candidate_options):
        calls.append((board_checked_at, offline_resource_checked_at))
        return {}, {"status": "passed"}
    monkeypatch.setattr(full_bank, "run", run)
    monkeypatch.setattr(full_bank, "write_new", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["full_bank", "run", "--out-dir", str(tmp_path),
                                     "--offline-resource-checked-at", "verified-offline-clearance"])
    assert full_bank.main() == 0
    assert calls == [(None, "verified-offline-clearance")]


def test_full_bank_uses_launcher_private_directory_cleanup(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from evals.rust_baseline import daemon
    entered = []

    @contextmanager
    def database():
        yield "synthetic-not-connected"

    @contextmanager
    def private_directory():
        entered.append("entered")
        try:
            yield str(tmp_path)
        finally:
            entered.append("cleaned")

    @contextmanager
    def launcher(dsn, private, *, env_extra=None, source_root=None):
        cleanup = {"actual_child_runtime": {"package_runtime_version": "0.7.0"}}
        yield None, "http://127.0.0.1:1", cleanup
        cleanup.update(daemon_stopped=True, children_stopped=True)

    monkeypatch.setattr(daemon, "disposable_database", database)
    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(daemon, "launched_daemon", launcher)
    monkeypatch.setattr(common, "lease_gate", lambda stamp: {})
    monkeypatch.setattr(full_bank, "require_historical_source", lambda root: None)
    monkeypatch.setattr(full_bank, "private_home_overrides", lambda private: {})
    monkeypatch.setattr(full_bank, "environment_metadata", lambda root: {})
    monkeypatch.setattr(full_bank, "observe", lambda *a: [
        {"id": "synthetic", "expect": {}, "response": {"status": 200, "body": {}}}])
    _, receipt = full_bank.run({"seed": 1, "scope": "synthetic stub"}, "verified")
    assert receipt["status"] == "passed"
    assert all(row["actual_child_runtime"] == {"package_runtime_version": "0.7.0"}
               for row in receipt["cleanup"])
    assert entered == ["entered", "cleaned", "entered", "cleaned"]
