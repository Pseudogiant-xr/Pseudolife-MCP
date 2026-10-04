"""Strict startup bytes and concurrent non-EOF public CLI scenario corpus."""
import base64
import hashlib
import json
from pathlib import Path
import socket
import sys

from .harness import capture_platform
from .stdio import capture
from .stdio_capture import initialize, modern_meta
from .stdio_corpus import ERAS
from .stdio_judge import StdioPolicy, concurrent_policy, judge
from .stdio_scenario_fixture import ScenarioFixture


def expected_stderr(contract, case, url, credential_file=None):
    matches = [item for item in contract["cases"] if item["case"] == case]
    if len(matches) != 1:
        raise ValueError("missing or ambiguous startup byte contract")
    text = matches[0]["stderr_lf_template"].replace("{fixture_url}", url)
    if "{credential_file}" in text:
        if credential_file is None:
            raise ValueError("credential-file fixture parameter required")
        text = text.replace("{credential_file}", str(credential_file))
    # Generate the expected text writer's platform newline. Captured streams
    # are never rewritten; source LF and exact fixture parameters are explicit.
    if sys.platform == "win32":
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def startup_difference(result, contract, case, url, credential_file, records):
    expected = next(item for item in contract["cases"] if item["case"] == case)
    differences = []
    if base64.b64decode(result["stderr_b64"], validate=True) != expected_stderr(
            contract, case, url, credential_file):
        differences.append({"path": "/stderr", "reason": "startup_stderr_bytes"})
    if result["exit_code"] != expected["exit_code"]:
        differences.append({"path": "/exit_code", "reason": "startup_exit"})
    if len(result["stdout_frames_b64"]) != expected["stdout_frame_count"]:
        differences.append({"path": "/frames", "reason": "startup_frame_count"})
    if case in ("degraded-200", "auth-invalid", "auth-missing", "version-mismatch", "preframe-refusal") and not any(
            record["method"] == "GET" and record["path"] == "/health" for record in records):
        differences.append({"path": "/fixture", "reason": "startup_health_not_observed"})
    if expected["exit_code"] == 1 and any(record["method"] == "POST" for record in records):
        differences.append({"path": "/fixture", "reason": "startup_refusal_sent_traffic"})
    return differences


def startup(root, command, contract):
    from evals.rust_baseline.daemon import private_directory
    from pseudolife_memory import __version__
    from pseudolife_memory.credentials import _write_token_file

    cells, differences = [], []
    for expected in contract["cases"]:
        case = expected["case"]
        fixture = None
        socket_owner = None
        if case in ("loopback-no-spawn", "remote-origin"):
            socket_owner = socket.socket()
            socket_owner.bind(("127.0.0.1", 0))
            origin = "0.0.0.0" if case == "remote-origin" else "127.0.0.1"
            url = f"http://{origin}:{socket_owner.getsockname()[1]}"
        else:
            health = {"status": "ok", "auth": False, "version": __version__}
            if case == "degraded-200":
                health.update(status="degraded", db="synthetic unavailable")
            elif case in ("auth-invalid", "auth-missing"):
                health["auth"] = True
            elif case == "version-mismatch":
                # Disable unattended updates: this pins the existing manual
                # note, separately from the decided post-first-frame update.
                health["version"] = "99.0.0"
            elif case == "preframe-refusal":
                health["init_refusal"] = "synthetic initialization refusal"
            fixture = ScenarioFixture(health)
            url = fixture.url
        pair = []
        try:
            with private_directory() as private:
                credential_file = None
                extras = {}
                if case == "auth-invalid":
                    credential_file = Path(private) / "synthetic-token"
                    _write_token_file(credential_file, "synthetic-placeholder")
                    credential_file.write_bytes(b"\n")
                    extras["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(credential_file)
                if case == "remote-origin":
                    extras["PSEUDOLIFE_MCP_NO_SPAWN"] = "0"
                for arm, prefix in (("oracle", [sys.executable, "-m", "pseudolife_memory.cli"]),
                                    ("candidate", command)):
                    before = len(fixture.records) if fixture else 0
                    exercise = ((lambda wire: wire.process.wait(timeout=10))
                                if expected["exit_code"] == 1 else initialize)
                    result = capture(prefix, cwd=root, home=Path(private) / arm, url=url,
                                     exercise=exercise, env_extra=extras, boundary_errors=arm == "candidate")
                    records = fixture.records[before:] if fixture else []
                    result.update(arm=arm, case=case, upstream_requests=records)
                    differences.extend({"case": case, "arm": arm, **difference} for difference in
                                       startup_difference(result, contract, case, url, credential_file, records))
                    pair.append(result)
                differences.extend({"case": case, **difference} for difference in judge(*pair, StdioPolicy()))
        finally:
            cleanup = fixture.close() if fixture else {"closed_endpoint_released": True}
            if socket_owner:
                socket_owner.close()
            for result in pair:
                result["fixture_cleanup"] = cleanup
            cells.extend(pair)
    return {"cells": cells, "differences": differences,
            "policy": "stdio-raw-compared",
            "limitation": "Storage-free startup; remote fixture is an unspecified IPv4 origin, no external host contacted. No updater is executed."}


def observe_concurrent(root, home, command, era, release_order):
    fixture = ScenarioFixture(together=release_order == "together")
    events = []
    result = None
    try:
        def exercise(wire):
            initialize(wire, era)
            for name in ("A", "B"):
                params = {"name": name, "arguments": {}}
                if era == ERAS[1]:
                    params["_meta"] = modern_meta()
                wire.send({"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": params})
                events.append({"event": "stdin-sent", "id": name})
                fixture.wait_arrival(name)
                events.append({"event": "upstream-arrived", "id": name})
            if release_order == "together":
                fixture.release["A"].set()
                events.append({"event": "released-both"})
                order = None
            else:
                order = release_order
            for index in range(2):
                if order:
                    fixture.release[order[index]].set()
                    events.append({"event": "released", "id": order[index]})
                frame = wire.until(lambda value: value.get("id") in ("A", "B"))
                events.append({"event": "stdout-response", "id": frame["id"],
                               "stdin_open": not wire.process.stdin.closed})
                if order and frame["id"] != order[index]:
                    raise RuntimeError("response preceded its controlled release")
        result = capture(command, cwd=root, home=home, url=fixture.url, exercise=exercise,
                         boundary_errors=True)
        result.update(era=era, release_order=release_order, events=events)
        return result
    finally:
        cleanup = fixture.close()
        if result is not None:
            result["fixture_cleanup"] = cleanup


def concurrent_difference(result):
    events = result["events"]
    prefix = [{"event": "stdin-sent", "id": "A"}, {"event": "upstream-arrived", "id": "A"},
              {"event": "stdin-sent", "id": "B"}, {"event": "upstream-arrived", "id": "B"}]
    replies = [event for event in events if event["event"] == "stdout-response"]
    if (events[:4] != prefix or len(replies) != 2 or
            sorted(event["id"] for event in replies) != ["A", "B"] or
            not all(event["stdin_open"] for event in replies)):
        return [{"path": "/events", "reason": "non_eof_causal_contract"}]
    return []


def run(root, command):
    from evals.rust_baseline.daemon import private_directory
    directory = Path(__file__).parent
    contract_path = directory / "stdio_startup_contract.json"
    orders_path = directory / "stdio_concurrent_orders.json"
    contract = json.loads(contract_path.read_text())
    evidence = json.loads(orders_path.read_text())
    startup_result = startup(root, command, contract)
    cells, differences = [], list(startup_result["differences"])
    for era in ERAS:
        for release_order in ("AB", "BA", "together"):
            pair = []
            for arm, prefix in (("oracle", [sys.executable, "-m", "pseudolife_memory.cli"]),
                                ("candidate", command)):
                with private_directory() as private:
                    result = observe_concurrent(root, Path(private) / arm, prefix, era, release_order)
                result["arm"] = arm
                differences.extend({"era": era, "release_order": release_order, "arm": arm, **difference}
                                   for difference in concurrent_difference(result))
                pair.append(result)
            policy = concurrent_policy(evidence, capture_platform()["os"].lower(), era, release_order)
            differences.extend({"era": era, "release_order": release_order, **difference}
                               for difference in judge(*pair, policy))
            cells.extend(pair)
    for cell in [*startup_result["cells"], *cells]:
        cleanup = cell["fixture_cleanup"]
        if (cleanup.get("fixture_errors") or
                any(not state["deleted"] for state in cleanup.get("sessions", {}).values()) or
                not all(cell["cleanup"].values())):
            differences.append({"case": cell.get("case", "non-eof-concurrent"),
                                "arm": cell["arm"], "path": "/cleanup", "reason": "scenario_cleanup"})
    return {"startup": startup_result, "concurrent": {"cells": cells}, "differences": differences,
            "contract_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (contract_path, orders_path)},
            "normalization_rule": "non-eof-observed-final-call-pair-orders; source-text-lf",
            "byte_oracle": "same-platform raw streams; startup expectations use exact fixture parameters"}
