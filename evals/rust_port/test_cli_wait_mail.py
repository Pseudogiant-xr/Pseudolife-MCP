"""Public subprocess coverage added for the native leaf, separate from tests/."""
import os
import base64
import copy
from pathlib import Path
import subprocess
import sys

import pytest

from evals.rust_port.cli_process import paired_cases, reset_home, byte_payload, candidate_controls
from evals.rust_port.harness import compare, Policy
from evals.rust_port.cli_wait_mail import cases


# Explicit candidate expectations for approved producer substitutions. Captured
# Python responses and their original differences remain in each record.
PRODUCER_SUBSTITUTIONS = {'wait-mail-help-columns-1': {'exit_code': 0, 'stdout': 'usage:\npseudolife-mcp\nwait-mail\n[-h]\n[--session-id\nSESSION_ID\n|\n--digest\nDIGEST]\n[--timeout\nTIMEOUT]\n[--interval\nINTERVAL]\n\nWait\nuntil\nthe\ndaemon\nrings\nthis\nsession\nfor\naddressed\nmail\n(plain\nmail\nnever\nwakes\nit),\nprint\nthe\nmail,\nexit.\nExit\n0:\na\nring\n(the\nmail\non\nstdout);\n3:\ntimeout;\n2:\nnothing\nto\nwait\non.\n\noptions:\n-h,\n--help\nshow\nthis\nhelp\nmessage\nand\nexit\n--session-id\nSESSION_ID\nhost\nsession\nid\nkeying\nthe\ndigest,\ne.g.\na\nCodex\nthread\nid\n(default:\nCLAUDE_CODE_SESSION_ID)\n--digest\nDIGEST\ncoordination\ndigest\nfile\n(<64\nhex\ndigits>.txt)\nto\nwatch\ninstead\n--timeout\nTIMEOUT\nseconds\nto\nwait,\nat\nmost\n86400\n(default\n14400)\n--interval\nINTERVAL\nseconds\nbetween\nchecks,\n0.01\nto\n60\n(default\n2)\n', 'stderr': ''}, 'wait-mail-help-columns-2': {'exit_code': 0, 'stdout': 'usage:\npseudolife-mcp\nwait-mail\n[-h]\n[--session-id\nSESSION_ID\n|\n--digest\nDIGEST]\n[--timeout\nTIMEOUT]\n[--interval\nINTERVAL]\n\nWait\nuntil\nthe\ndaemon\nrings\nthis\nsession\nfor\naddressed\nmail\n(plain\nmail\nnever\nwakes\nit),\nprint\nthe\nmail,\nexit.\nExit\n0:\na\nring\n(the\nmail\non\nstdout);\n3:\ntimeout;\n2:\nnothing\nto\nwait\non.\n\noptions:\n-h,\n--help\nshow\nthis\nhelp\nmessage\nand\nexit\n--session-id\nSESSION_ID\nhost\nsession\nid\nkeying\nthe\ndigest,\ne.g.\na\nCodex\nthread\nid\n(default:\nCLAUDE_CODE_SESSION_ID)\n--digest\nDIGEST\ncoordination\ndigest\nfile\n(<64\nhex\ndigits>.txt)\nto\nwatch\ninstead\n--timeout\nTIMEOUT\nseconds\nto\nwait,\nat\nmost\n86400\n(default\n14400)\n--interval\nINTERVAL\nseconds\nbetween\nchecks,\n0.01\nto\n60\n(default\n2)\n', 'stderr': ''}, 'wait-mail-help-columns-3': {'exit_code': 0, 'stdout': 'usage:\npseudolife-mcp\nwait-mail\n[-h]\n[--session-id\nSESSION_ID\n|\n--digest\nDIGEST]\n[--timeout\nTIMEOUT]\n[--interval\nINTERVAL]\n\nWait\nuntil\nthe\ndaemon\nrings\nthis\nsession\nfor\naddressed\nmail\n(plain\nmail\nnever\nwakes\nit),\nprint\nthe\nmail,\nexit.\nExit\n0:\na\nring\n(the\nmail\non\nstdout);\n3:\ntimeout;\n2:\nnothing\nto\nwait\non.\n\noptions:\n-h,\n--help\nshow\nthis\nhelp\nmessage\nand\nexit\n--session-id\nSESSION_ID\nhost\nsession\nid\nkeying\nthe\ndigest,\ne.g.\na\nCodex\nthread\nid\n(default:\nCLAUDE_CODE_SESSION_ID)\n--digest\nDIGEST\ncoordination\ndigest\nfile\n(<64\nhex\ndigits>.txt)\nto\nwatch\ninstead\n--timeout\nTIMEOUT\nseconds\nto\nwait,\nat\nmost\n86400\n(default\n14400)\n--interval\nINTERVAL\nseconds\nbetween\nchecks,\n0.01\nto\n60\n(default\n2)\n', 'stderr': ''}, 'wait-mail-help-columns-7': {'exit_code': 0, 'stdout': 'usage:\npseudolife-mcp\nwait-mail\n[-h]\n[--session-id\nSESSION_ID\n|\n--digest\nDIGEST]\n[--timeout\nTIMEOUT]\n[--interval\nINTERVAL]\n\nWait\nuntil\nthe\ndaemon\nrings\nthis\nsession\nfor\naddressed\nmail\n(plain\nmail\nnever\nwakes\nit),\nprint\nthe\nmail,\nexit.\nExit 0:\na ring\n(the\nmail on\nstdout);\n3:\ntimeout;\n2:\nnothing\nto wait\non.\n\noptions:\n-h,\n--help\nshow\nthis\nhelp\nmessage\nand\nexit\n--session-id\nSESSION_ID\nhost\nsession\nid\nkeying\nthe\ndigest,\ne.g. a\nCodex\nthread\nid\n(default:\nCLAUDE_CODE_SESSION_ID)\n--digest\nDIGEST\ncoordination\ndigest\nfile\n(<64\nhex\ndigits>.txt)\nto\nwatch\ninstead\n--timeout\nTIMEOUT\nseconds\nto\nwait,\nat most\n86400\n(default\n14400)\n--interval\nINTERVAL\nseconds\nbetween\nchecks,\n0.01 to\n60\n(default\n2)\n', 'stderr': ''}, 'wait-mail-help-columns-12': {'exit_code': 0, 'stdout': 'usage:\npseudolife-mcp\nwait-mail\n[-h]\n[--session-id\nSESSION_ID |\n--digest\nDIGEST]\n[--timeout\nTIMEOUT]\n[--interval\nINTERVAL]\n\nWait until\nthe daemon\nrings this\nsession for\naddressed\nmail (plain\nmail never\nwakes it),\nprint the\nmail, exit.\nExit 0: a\nring (the\nmail on\nstdout); 3:\ntimeout; 2:\nnothing to\nwait on.\n\noptions:\n-h, --help\nshow this\nhelp message\nand exit\n--session-id\nSESSION_ID\nhost session\nid keying\nthe digest,\ne.g. a Codex\nthread\nid (default:\nCLAUDE_CODE_SESSION_ID)\n--digest\nDIGEST\ncoordination\ndigest file\n(<64 hex\ndigits>.txt)\nto\nwatch\ninstead\n--timeout\nTIMEOUT\nseconds to\nwait, at\nmost 86400\n(default\n14400)\n--interval\nINTERVAL\nseconds\nbetween\nchecks, 0.01\nto 60\n(default 2)\n', 'stderr': ''}, 'wait-mail-help-columns-13': {'exit_code': 0, 'stdout': 'usage:\npseudolife-mcp\nwait-mail\n[-h]\n[--session-id\nSESSION_ID |\n--digest\nDIGEST]\n[--timeout\nTIMEOUT]\n[--interval\nINTERVAL]\n\nWait until\nthe daemon\nrings this\nsession for\naddressed\nmail (plain\nmail never\nwakes it),\nprint the\nmail, exit.\nExit 0: a\nring (the\nmail on\nstdout); 3:\ntimeout; 2:\nnothing to\nwait on.\n\noptions:\n-h, --help\nshow this\nhelp message\nand exit\n--session-id\nSESSION_ID\nhost session\nid keying the\ndigest, e.g.\na Codex\nthread\nid (default:\nCLAUDE_CODE_SESSION_ID)\n--digest\nDIGEST\ncoordination\ndigest file\n(<64 hex\ndigits>.txt)\nto\nwatch instead\n--timeout\nTIMEOUT\nseconds to\nwait, at most\n86400\n(default\n14400)\n--interval\nINTERVAL\nseconds\nbetween\nchecks, 0.01\nto 60\n(default 2)\n', 'stderr': ''}, 'wait-mail-help-columns-40': {'exit_code': 0, 'stdout': 'usage: pseudolife-mcp wait-mail [-h]\n[--session-id SESSION_ID | --digest\nDIGEST]\n[--timeout TIMEOUT] [--interval\nINTERVAL]\n\nWait until the daemon rings this session\nfor addressed mail (plain mail never\nwakes it), print the mail, exit. Exit 0:\na ring (the mail on stdout); 3:\ntimeout; 2: nothing to wait on.\n\noptions:\n-h, --help show this help message and\nexit\n--session-id SESSION_ID\nhost session id keying the digest, e.g.\na Codex thread\nid (default: CLAUDE_CODE_SESSION_ID)\n--digest DIGEST coordination digest file\n(<64 hex digits>.txt) to\nwatch instead\n--timeout TIMEOUT seconds to wait, at\nmost 86400 (default 14400)\n--interval INTERVAL seconds between\nchecks, 0.01 to 60 (default 2)\n', 'stderr': ''}, 'wait-mail-help-columns-120': {'exit_code': 0, 'stdout': 'usage: pseudolife-mcp wait-mail [-h]\n[--session-id SESSION_ID | --digest DIGEST]\n[--timeout TIMEOUT] [--interval INTERVAL]\n\nWait until the daemon rings this session for addressed mail (plain mail never\nwakes it), print the mail, exit. Exit 0: a ring (the mail on stdout); 3:\ntimeout; 2: nothing to wait on.\n\noptions:\n-h, --help show this help message and exit\n--session-id SESSION_ID\nhost session id keying the digest, e.g. a Codex thread\nid (default: CLAUDE_CODE_SESSION_ID)\n--digest DIGEST coordination digest file (<64 hex digits>.txt) to\nwatch instead\n--timeout TIMEOUT seconds to wait, at most 86400 (default 14400)\n--interval INTERVAL seconds between checks, 0.01 to 60 (default 2)\n', 'stderr': ''}, 'wait-mail-error-columns-40': {'exit_code': 2, 'stdout': '', 'stderr': 'usage: pseudolife-mcp wait-mail [-h]\n[--session-id SESSION_ID | --digest\nDIGEST] [--timeout TIMEOUT] [--interval\nINTERVAL]\npseudolife-mcp wait-mail: error: unrecognized arguments: --bogus\n'}, 'wait-mail-error-columns-120': {'exit_code': 2, 'stdout': '', 'stderr': 'usage: pseudolife-mcp wait-mail [-h] [--session-id SESSION_ID | --digest DIGEST] [--timeout TIMEOUT] [--interval\nINTERVAL]\npseudolife-mcp wait-mail: error: unrecognized arguments: --bogus\n'}, 'wait-mail-unicode-negative': {'exit_code': 2, 'stdout': '', 'stderr': 'usage: pseudolife-mcp wait-mail [-h]\n                                [--session-id SESSION_ID | --digest DIGEST]\n                                [--timeout TIMEOUT] [--interval INTERVAL]\npseudolife-mcp wait-mail: error: argument --timeout: expected one argument\n'}}


def declared_candidate_expectation(result):
    result = copy.deepcopy(result)
    controls = []
    for record in result["records"]:
        expected = PRODUCER_SUBSTITUTIONS.get(record["id"])
        if expected is None:
            controls.append(record)
            continue
        response = copy.deepcopy(record["oracle"]["response"])
        response["exit_code"] = expected["exit_code"]
        for field in ("stdout", "stderr"):
            text = expected[field].replace("\n", "\r\n") if os.name == "nt" else expected[field]
            response[field + "_b64"] = base64.b64encode(text.encode("utf-8")).decode("ascii")
        exact = Policy(source_text_paths=(), ignored_values=())
        inputs = [byte_payload({**record[arm], "response": {}}) for arm in ("oracle", "candidate")]
        differences = compare(*inputs, exact)
        differences += [{**cell, "path": "/response" + cell["path"]}
                        for cell in compare(response, record["candidate"]["response"], exact)]
        record["raw_oracle_differences"] = record["differences"]
        record["candidate_expected_response"] = response
        record["candidate_contract_substitution"] = "approved wait-mail producer scope"
        record["differences"] = differences
        record["passed"] = not differences
        # Sensitivity controls use the declared candidate contract, never a
        # permissive comparison against an already different Python response.
        controls.append({**record, "oracle": {**record["oracle"], "response": response}})
    result["candidate_output_controls"] = candidate_controls(controls)
    result["passed"] = all(row["passed"] for row in result["records"]) and all(
        row["rejected"] for row in result["candidate_output_controls"])
    return result


@pytest.mark.parametrize("name", ["help", "short-help-value", "short-help-equals", "unknown",
                                 "conflict", "negative-exponent", "unicode-negative",
                                 "float-control-space", "invalid-unicode-basename",
                                 "missing-digest", "plain-mail", "seen-ring", "host-record",
                                 "help-before-ambiguous", "help-columns-40", "help-columns-80",
                                 "help-columns-120", "error-columns-40", "error-columns-80",
                                 "error-columns-120", "help-columns-1", "help-columns-2",
                                 "help-columns-3", "help-columns-7", "help-columns-12",
                                 "help-columns-13"])
def test_public_wait_mail_deterministic_bytes_and_candidate_controls(tmp_path, name):
    binary = os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if not binary:
        pytest.skip("native binary must be selected explicitly")
    root = Path(os.environ.get("PSEUDOLIFE_WAIT_MAIL_ORACLE_ROOT", Path(__file__).resolve().parents[2]))
    # Both selected production modules must still equal the immutable pin.
    for relative in ("pseudolife_memory/cli.py", "pseudolife_memory/wait_mail_cli.py"):
        pin = subprocess.check_output(["git", "show", "eb0c13e9c5036aa2b95e7fccb77f41ca1c095493:" + relative], cwd=root)
        assert (root / relative).read_bytes().replace(b"\r\n", b"\n") == pin
    case = next(case for case in cases() if case["id"] == "wait-mail-" + name)
    commands = {"oracle": [sys.executable, "-m", "pseudolife_memory.cli"], "candidate": [binary]}
    home = tmp_path / "home"
    try:
        result = paired_cases([case], commands, root=root, home=home, url="http://127.0.0.1:29871")
        result = declared_candidate_expectation(result)
        assert result["passed"], result["records"][0]["differences"]
        assert all(control["rejected"] for control in result["candidate_output_controls"])
    finally:
        reset_home(home)
