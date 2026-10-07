"""Prove Python process replay and reject an already compiled broken fixture."""
import argparse
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from evals.rust_port.fixtures import corpus
from evals.rust_port.harness import HttpClient, execute, isolated_env, replay, write_new
from evals.rust_port.provenance import require_historical_source, module_command, runtime_metadata, runtime_probe_command
from evals.rust_port.processes import owned_process

ROOT = Path(__file__).resolve().parents[2]


def probe_runtime(home, root=ROOT):
    """A separate untimed observer with the CLI's interpreter, home and cwd."""
    with owned_process(runtime_probe_command(root), cwd=root, env=isolated_env(home)) as process:
        stdout, stderr = process.communicate(timeout=10)
    if process.returncode or stderr:
        raise RuntimeError("isolated runtime observer failed")
    return json.loads(stdout)


def plugin_proof(directory, binary, root=ROOT):
    manifest = json.loads((ROOT / "evals/rust_port/oracle_tests.json").read_text(encoding="utf-8"))
    selected = list(manifest["mapped"])
    watched = ["tests/test_cli_dispatch.py", "tests/conftest.py"]
    hashes = lambda: {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in watched}
    before = hashes()
    results = {}
    runtimes = {}
    for name, prefix, nodes in (
        ("python", [sys.executable, "-m", "pseudolife_memory.cli"], selected),
        ("broken_rust", [str(binary)], selected),
        ("unmapped_refused", [str(binary)], [*selected, *manifest["unmapped"]]),
    ):
        xml = directory / f"{name}.xml"
        runtimes[name] = probe_runtime(directory / f"plugin-{name}", root)
        with owned_process(module_command("pytest", root, ["-p", "evals.rust_port.pytest_plugin",
                               "--port-cli-json", json.dumps(prefix), *nodes, "-q", "--junitxml", str(xml)]),
                              cwd=root, env=isolated_env(directory / f"plugin-{name}"),
                              stdin=subprocess.PIPE) as proc:
            proc.communicate(input=b"", timeout=60)
        suites = ET.parse(xml).getroot().findall("testsuite") if xml.exists() else []
        results[name] = {"exit_code": proc.returncode, **{
            key: sum(int(s.get(key, "0")) for s in suites)
            for key in ("tests", "failures", "errors", "skipped")}}
    assert results["python"] == {"exit_code": 0, "tests": 5, "failures": 0, "errors": 0, "skipped": 0}
    assert results["broken_rust"] == {"exit_code": 1, "tests": 5, "failures": 5, "errors": 0, "skipped": 0}
    assert results["unmapped_refused"]["exit_code"] == 4
    assert before == hashes(), "existing test sources must remain unchanged"
    return {"results": results, "unchanged_sources_sha256": before,
            "adapter_runtime_probes": {"observation": "separate process with the pytest adapter's interpreter, environment and source bootstrap",
                                       "runtimes": runtimes},
            "adapter": "whole CLI process; original assertions and SystemExit expectations preserved"}


@contextmanager
def oracle_process(directory, root=ROOT):
    env = isolated_env(directory / "oracle-home")
    log = directory / "oracle-output"
    with log.open("wb") as stdout, (directory / "oracle-errors").open("wb") as stderr:
        with owned_process(module_command("evals.rust_port.oracle", root,
                                ["--port", "0", "--oracle-root", root]),
                                cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr) as proc:
            deadline = time.monotonic() + 60
            url = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("owned Python oracle exited before readiness")
                lines = log.read_text(encoding="utf-8").splitlines()
                if lines:
                    ready = json.loads(lines[0])
                    port = ready["port"]
                    url = f"http://127.0.0.1:{port}"
                    try:
                        response = HttpClient(url, timeout=0.3).execute({"path": "/health"}, "http")
                        if response["status"] == 200 and response["body"] == {"status": "ok", "fixture": True}:
                            break
                    except (OSError, TimeoutError):
                        pass
                time.sleep(0.1)
            else:
                raise RuntimeError("owned Python oracle readiness timed out")
            yield url, ready["actual_child_runtime"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broken-binary", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--validate-plugin", action="store_true")
    parser.add_argument("--oracle-root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.oracle_root.resolve()
    source = require_historical_source(root)
    if not args.broken_binary.is_file():
        parser.error("broken fixture must already be compiled")
    binary = args.broken_binary.resolve()
    with tempfile.TemporaryDirectory(prefix="rust-port-selfcheck-") as tmp:
        directory = Path(tmp)
        with oracle_process(directory, root) as (url, child_runtime):
            spec = corpus()
            python = [sys.executable, "-m", "pseudolife_memory.cli"]
            cli_runtimes = {label: probe_runtime(directory / home, root) for label, home in (
                ("record", "record-home"), ("replay", "control-home"))}
            records = execute(spec["cases"], cli_prefix=python, base_url=url,
                              cwd=root, home=directory / "record-home")
            transcript = {"schema": 1, "fixture": spec["fixture"], "records": records}
            control = replay(transcript, cli_prefix=python, base_url=url,
                             cwd=root, home=directory / "control-home")
            broken = replay(transcript, cli_prefix=[str(binary)], base_url=url,
                            cwd=root, home=directory / "candidate-home")
            # A transport error response cannot masquerade as successful fixture
            # coverage merely because the same error occurred in both runs.
            indexed = {r["id"]: r["response"] for r in records}
            assert indexed["http-search"]["body"]["count"] == 3
            negotiated = indexed["mcp-initialize"]["body"]["result"]["protocolVersion"]
            assert isinstance(negotiated, str) and negotiated
            assert "result" in indexed["mcp-tools"]["body"]
            search = indexed["mcp-search"]["body"]["result"]
            assert not search.get("isError", False)
            assert json.loads(search["content"][0]["text"])["count"] == 3
            assert control["passed"], "Python control replay must pass"
            assert not broken["passed"], "deliberately broken Rust fixture must fail"
            assert {d["case"] for d in broken["differences"]} == {"cli-help", "cli-unknown"}
        summary = {"schema": 1, "python_control": control, "broken_rust": broken,
                   "negative_control": {"language": "Rust",
                       "source_sha256": hashlib.sha256((ROOT / "evals/rust_port/broken_fixture.rs").read_bytes()).hexdigest(),
                       "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                       "scope": "deliberately broken CLI fixture; no production Rust implementation"},
                   "owned_oracle_stopped": True,
                   "fixture": spec["fixture"],
                   "environment": {"parent_runtime": runtime_metadata(ROOT),
                                   "actual_child_runtime": child_runtime,
                                   "python_cli_runtime_probes": {
                                       "observation": "separate process with the CLI's interpreter, environment and selected source",
                                       "runtimes": cli_runtimes},
                                   **source, "oracle_commit": source["source_head"],
                                   "requested_protocol": "2026-07-28", "negotiated_protocol": negotiated},
                   "scope": "CLI process and real Python HTTP/MCP handlers with in-memory fixture service; no storage or model parity"}
        if args.validate_plugin:
            summary["external_pytest_plugin"] = plugin_proof(directory, binary, root)
        write_new(args.out_dir / "corpus.json", spec)
        write_new(args.out_dir / "python-oracle.json", transcript)
        write_new(args.out_dir / "selfcheck.json", summary)
    print(json.dumps({"python_passed": True, "broken_rust_rejected": True,
                      "cases": len(records), "oracle_stopped": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
