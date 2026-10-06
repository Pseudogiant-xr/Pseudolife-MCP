"""Executable lease header substitution cells; the pinned Python arm stays raw."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading

from .lease_help import ORACLE_HEAD, ORACLE_SOURCES, committed_bytes
from .lease_policy_preparation import forbidden_header, refusal_response


def cases():
    """Retain the eight D3 instances and add all approved fields/C0 values."""
    values = [("DEL", "\x7f"), ("CR", "\r"), ("LF", "\n"), ("NUL", "\0"),
              ("FOLDED", "\r\n ")]
    values += [(f"C0-{code:02X}", chr(code)) for code in range(32)
               if code not in (0, 10, 13)]
    result = [{"id": f"D3-{tag}-{field}", "field": field, "value": "bad" + value + "value",
               "action": "run", "token_file": field == "bearer" and tag == "NUL"}
              for field in ("agent_id", "credential", "bearer") for tag, value in values]
    result += [{"id": f"D3-{tag}-bearer-file", "field": "bearer", "value": "bad" + value + "value",
                "action": "run", "token_file": True} for tag, value in values]
    result += [{"id": f"D3-{tag}-bearer-{action}", "field": "bearer", "value": "bad" + value + "value",
                "action": action, "token_file": tag == "NUL"}
               for action in ("check", "list") for tag, value in values[:5]]
    result += [{"id": f"D3-DEL-{field}-{action}-empty-home", "field": field, "value": "bad\x7fvalue",
                "action": action, "token_file": False, "empty_home": True}
               for field, action in (("agent_id", "run"), ("credential", "run"),
                                     ("bearer", "run"), ("bearer", "check"), ("bearer", "list"))]
    # These are exact raw-Python comparisons, outside the substitution.
    result += [{"id": f"ordinary-{field}-{tag}", "field": field, "value": value,
                "action": "run", "token_file": False}
               for field in ("agent_id", "credential")
               for tag, value in (("ascii", "fixture-value"), ("nonascii", "nonascii-é"),
                                  ("surrogate", "surrogate-\ud800"))]
    result += [{"id": "ordinary-bearer-nonascii", "field": "bearer", "value": "bearer-é",
                "action": "run", "token_file": False},
               {"id": "ordinary-bearer-file-newline", "field": "bearer", "value": "fixture-bearer\r\n",
                "action": "run", "token_file": True, "file_terminator": True},
               {"id": "ordinary-bearer-file-private-check", "field": "bearer", "value": "bad\x7fvalue",
                "action": "run", "token_file": True, "missing_file": True}]
    for action in ("run", "check", "list"):
        for source, token_file in (("env", False), ("file", True)):
            for tag, value in (("forbidden", "bad\x7fvalue"), ("valid", "fixture-bearer")):
                result.append({"id": f"combined-url-{action}-{source}-{tag}", "field": "bearer",
                               "value": value, "action": action, "token_file": token_file,
                               "invalid_url": True, "shared_home": True})
        for tag, value, token_file, missing in (("credential-error", "bad value", False, False),
                                               ("private-check", "bad\x7fvalue", True, True)):
            result.append({"id": f"combined-url-{action}-{tag}", "field": "bearer",
                           "value": value, "action": action, "token_file": token_file,
                           "missing_file": missing, "invalid_url": True, "shared_home": True})
    for field, other in (("agent_id", "credential"), ("credential", "agent_id")):
        for tag, value in (("forbidden", "bad\x7fvalue"), ("empty", ""),
                           ("nonascii", "nonascii-é"), ("surrogate", "surrogate-\ud800")):
            result.append({"id": f"combined-partial-{field}-{tag}", "field": field,
                           "value": value, "action": "run", "token_file": False,
                           "registration_overrides": {other: ""}, "shared_home": True})
        for tag, value in (("forbidden", "bad\x7fvalue"), ("valid", "fixture-value")):
            result.append({"id": f"combined-missing-{field}-{tag}", "field": field,
                           "value": value, "action": "run", "token_file": False,
                           "registration_missing": [other], "shared_home": True})
    return result


def expected(case, oracle, pre_files, newline):
    """Substitute only a governed header value, after private-file admission."""
    if (not case.get("file_terminator") and not case.get("missing_file")
            and forbidden_header(case["field"], case["value"])):
        return refusal_response(case["field"], pre_files, newline)
    return oracle


def comparison(case, oracle, candidate, pre_files, newline, requests):
    target = expected(case, oracle, pre_files, newline)
    substituted = target is not oracle
    required_requests = (["/api/coordination/register"]
                         if case["field"] != "bearer" else [])
    return (target == candidate
            and (not substituted or requests == required_requests))


def controls(case, oracle, candidate, pre_files, newline, requests):
    """Every retained response/effect dimension must affect acceptance."""
    mutations = []
    for key, value in (("exit_code", 0), ("stdout_b64", "eA=="),
                       ("stderr_b64", ""), ("post_files_b64", {**pre_files, "child-ran": "eA=="})):
        changed = copy.deepcopy(candidate)
        changed[key] = value
        mutations.append((changed, requests))
    mutations.append((candidate, [*requests, "/api/coordination/lease"]))
    mutations.append((candidate, [*requests, "/api/coordination/release"]))
    return [not comparison(case, oracle, response, pre_files, newline, effects)
            for response, effects in mutations]


def files(home):
    return {path.relative_to(home).as_posix(): base64.b64encode(path.read_bytes()).decode()
            for path in sorted(home.rglob("*")) if path.is_file()}


def observe(command, case, directory, oracle_package, home=None):
    if home is None:
        home = Path(tempfile.mkdtemp(prefix="lease-header-arm-", dir=directory))
    else:
        home.mkdir()
    if not case.get("empty_home"):
        (home / "instance.id").write_bytes(b"0123456789ab\n")
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment.update(HOME=str(home), USERPROFILE=str(home), COLUMNS="80",
                       PSEUDOLIFE_LEASE_LOCK_DIR=str(home), PSEUDOLIFE_SUITE_LOCK_DIR=str(home),
                       PSEUDOLIFE_MCP_NO_SPAWN="1", PSEUDOLIFE_MCP_TOKEN="fixture-bearer")
    if case["field"] == "bearer":
        if case["token_file"]:
            token = home / "token"
            if not case.get("missing_file"):
                # Apply the pinned private-file permissions to a valid token,
                # then write the deliberately invalid input through that file.
                script = ("import sys; sys.path.insert(0,sys.argv[1]); "
                          "from pseudolife_memory.credentials import _write_token_file; "
                          "_write_token_file(sys.argv[2],'fixture-bearer')")
                subprocess.run([sys.executable, "-I", "-c", script, str(oracle_package), str(token)],
                               check=True, capture_output=True)
                token.write_bytes(case["value"].encode())
            environment["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(token)
        else:
            environment["PSEUDOLIFE_MCP_TOKEN"] = case["value"]
    requests = []

    class Peer(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(self.path)
            if self.path.endswith("/register"):
                reply = {"agent_id": "fixture-agent", "credential": "fixture-key"}
                if case["field"] != "bearer":
                    reply[case["field"]] = case["value"]
                    reply.update(case.get("registration_overrides", {}))
                    for field in case.get("registration_missing", []):
                        reply.pop(field, None)
            elif self.path.endswith("/leases"):
                reply = {"leases": []}
            else:
                reply = {"state": "held"}
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    peer = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    environment["PSEUDOLIFE_MCP_DAEMON_URL"] = f"http://127.0.0.1:{peer.server_port}"
    if case.get("invalid_url"):
        environment["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:1/invalid-path"
    argv = ["lease", case["action"]]
    if case["action"] == "run":
        child = "from pathlib import Path; Path('child-ran').write_bytes(b'ran'); raise SystemExit(3)"
        argv += ["resource", "--timeout", "0", "--", sys.executable, "-c", child]
    elif case["action"] == "check":
        argv += ["resource"]
    pre = files(home)
    try:
        result = subprocess.run([*command, *argv], cwd=home, env=environment,
                                capture_output=True, timeout=30)
    finally:
        peer.shutdown()
        thread.join()
        peer.server_close()
    response = {"exit_code": result.returncode, "stdout_b64": base64.b64encode(result.stdout).decode(),
                "stderr_b64": base64.b64encode(result.stderr).decode(), "post_files_b64": files(home)}
    return response, pre, requests


def run(root, candidate, out, selected=None):
    from .provenance import require_instrument_binding
    binding = require_instrument_binding(root, {
        "evals/rust_port/lease_headers.py": [Path(__file__)],
        "evals/rust_port/lease_help.py": [committed_bytes],
        "evals/rust_port/lease_policy_preparation.py": [forbidden_header, refusal_response],
        "evals/rust_port/provenance.py": [require_instrument_binding],
    })
    directory = Path(tempfile.mkdtemp(prefix="lease-header-corpus-"))
    package = directory / "oracle" / "pseudolife_memory"
    package.mkdir(parents=True)
    for name in (*ORACLE_SOURCES, "credentials.py"):
        (package / name).write_bytes(committed_bytes(root, ORACLE_HEAD, "pseudolife_memory/" + name))
    script = ("import sys; sys.path.insert(0,sys.argv[1]); "
              "from pseudolife_memory.lease_cli import main; raise SystemExit(main(sys.argv[3:]))")
    oracle_command = [sys.executable, "-I", "-c", script, str(package.parent)]
    newline = b"\r\n" if os.name == "nt" else b"\n"
    records = []
    for case in cases():
        if selected and case["id"] not in selected:
            continue
        # New ordinary controls retain path bytes by resetting the same home.
        # Move each completed home aside so its actual state also stays raw.
        home = directory / (case["id"] + "-home") if case.get("shared_home") else None
        oracle, pre, oracle_requests = observe(oracle_command, case, directory, package.parent, home)
        if home is not None:
            home.rename(directory / (case["id"] + "-oracle-home"))
        candidate_response, candidate_pre, requests = observe([str(candidate)], case, directory, package.parent, home)
        if home is not None:
            home.rename(directory / (case["id"] + "-candidate-home"))
        assert pre == candidate_pre
        passed = comparison(case, oracle, candidate_response, candidate_pre, newline, requests)
        substituted = expected(case, oracle, pre, newline) is not oracle
        if not substituted:
            passed = passed and requests == oracle_requests
        rejected = controls(case, oracle, candidate_response, pre, newline, requests) if substituted else []
        record = {"case": case, "oracle_raw": oracle, "candidate_raw": candidate_response,
                  "pre_files_b64": pre, "oracle_requests": oracle_requests,
                  "candidate_requests": requests, "substitution": "http-forbidden-input-refused" if substituted else None,
                  "passed": passed, "controls_rejected": rejected}
        records.append(record)
        print(json.dumps({"id": case["id"], "passed": passed, "controls_rejected": sum(rejected)}), flush=True)
    receipt = {"binding": binding, "oracle_head": ORACLE_HEAD, "platform": platform.platform(),
               "python": sys.version, "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
               "normalizations": [], "scratch": str(directory), "cases": records}
    out.write_text(json.dumps(receipt, indent=2, ensure_ascii=True) + "\n")
    return int(not records or any(not r["passed"] or not all(r["controls_rejected"]) for r in records))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    return run(args.root.resolve(), args.candidate.resolve(), args.out.resolve(), args.case)


if __name__ == "__main__":
    raise SystemExit(main())
