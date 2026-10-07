"""Additive scripted-peer diagnostic cases; real-daemon acceptance stays separate."""
from __future__ import annotations

import base64
import hashlib
import json


def cases():
    encode = lambda raw: base64.b64encode(raw).decode("ascii")
    mark = ".pseudolife-mcp/digests/" + hashlib.sha256(b"sess-1").hexdigest() + ".mark"
    result = []

    def add(name, mode="briefing", argv=None, stdin=b"", env=None, files=None,
            health=(200, b"{}"), content=(200, b'{"markdown":"  hello\\nworld  "}')):
        result.append({"id": name, "mode": mode, "argv": [mode, *(argv or [])],
                       "stdin_b64": encode(stdin), "environment_deltas": env or {},
                       "pre_files_b64": {k: encode(v) for k, v in (files or {}).items()},
                       "normalizations": [], "peer": {"health": [health[0], encode(health[1])],
                                                        "content": [content[0], encode(content[1])]}})

    add("briefing-plain")
    add("briefing-env-only", env={"PSEUDOLIFE_MCP_TOKEN": "synthetic-env-bearer"})
    add("briefing-custom-negative-unknown", argv=["--max-u", "-2", "--max-lessons=００_４", "--max-world", "+005", "extra", "--unknown"])
    add("briefing-help", argv=["--help"])
    add("briefing-error-int", argv=["--max-world", "nope"])
    add("briefing-error-missing", argv=["--max-lessons", "--help"])
    add("briefing-error-ambiguous", argv=["--max"])
    add("briefing-error-flag", argv=["--hook-json=yes"])
    add("briefing-hook-json", argv=["--hook-json"], content=(200, " \u001c café — 🐍 \n".encode()))
    add("briefing-coordination-hook", argv=["--coordination", "--hook-json"], content=(200, b"check in\n"))
    add("briefing-coordination-off", argv=["--coordination"], env={"PSEUDOLIFE_AGENT_COORDINATION": " false "})
    add("briefing-health-error-json", health=(503, b"false"))
    add("briefing-health-non-json", health=(200, b"oops"))
    add("briefing-health-null", health=(200, b"null"))
    add("briefing-content-redirect", content=(302, b"not followed"))
    add("briefing-content-invalid-utf8", content=(200, b"\xff"))
    add("briefing-markdown-false", content=(200, b'{"markdown": false}'))
    add("briefing-hook-plain-context", argv=["--hook-json"], content=(200, b"surrogate "))
    add("briefing-launcher-default", argv=["--hook-json"], files={"data/pseudolife-mcp/bin/pseudolife-mcp": b"launcher"}, content=(200, b"context"))
    add("briefing-launcher-override", argv=["--hook-json"], env={"PSEUDOLIFE_SHIM_RUNTIMES": "{home}/owned-runtime", "PSEUDOLIFE_SHIM_LAUNCHER": "{home}/launch space/pseudolife-mcp.exe"}, files={"launch space/pseudolife-mcp.exe": b"launcher"}, content=(200, b"context"))
    add("briefing-token-latin1", env={"PSEUDOLIFE_MCP_TOKEN": "synthetic-café"})
    add("briefing-token-nonlatin", env={"PSEUDOLIFE_MCP_TOKEN": "synthetic-🐍"})
    add("briefing-token-del", env={"PSEUDOLIFE_MCP_TOKEN": "synthetic-\x7f"})
    add("briefing-token-control", env={"PSEUDOLIFE_MCP_TOKEN": "synthetic-\x01"})
    add("briefing-token-fold", env={"PSEUDOLIFE_MCP_TOKEN": "synthetic\r\n folded"})

    def prompt(name, raw=b'{"session_id":"sess-1"}', body=b"100.000000\n", files=None, env=None, status=200):
        add(name, "prompt-hook", argv=["--help", "ignored"], stdin=raw, env=env, files=files, content=(status, body))

    prompt("prompt-baseline")
    prompt("prompt-changed-note", body="101..\r\n café — 🐍  \r\n".encode(), files={mark: b" 100.000000 \r\n"})
    prompt("prompt-quiet-advance", body=b"102.0\n", files={mark: b"101.0\n"})
    prompt("prompt-body-without-lf", body=b"103.0", files={mark: b"102.0\n"})
    prompt("prompt-malformed-preserves", body=b"not-a-cursor\nnote", files={mark: b"100.0\n"})
    prompt("prompt-invalid-utf8-preserves", body=b"104.0\n\xff", files={mark: b"100.0\n"})
    prompt("prompt-redirect-preserves", body=b"104.0\nnot followed", files={mark: b"100.0\n"}, status=302)
    prompt("prompt-token-file-wins", env={"PSEUDOLIFE_MCP_TOKEN": "synthetic-other"})
    prompt("prompt-env-token", env={"PSEUDOLIFE_MCP_TOKEN_FILE": None, "PSEUDOLIFE_MCP_TOKEN": "synthetic-env"})
    prompt("prompt-token-file-missing", env={"PSEUDOLIFE_MCP_TOKEN_FILE": "{home}/missing-token"})
    prompt("prompt-token-file-no-token", env={"PSEUDOLIFE_MCP_TOKEN_FILE": None, "PSEUDOLIFE_MCP_TOKEN": None})
    prompt("prompt-mark-invalid-ascii", files={mark: b"junk\n"})
    prompt("prompt-mark-nonascii-after-lf", files={mark: b"100.0\n\xff"})
    prompt("prompt-stdin-replacement", raw=b'{"session_id":"sess-1","prompt":"\xff"}')
    prompt("prompt-json-surrogate-extra", raw=b'{"session_id":"sess-1","other":"\\ud800"}')
    prompt("prompt-json-nan-extra", raw=b'{"session_id":"sess-1","other":NaN}')
    for index, raw in enumerate([b"", b"null", b"[]", b"{}", b'{"session_id":1}', b'{"session_id":"bad id"}', b'{"session_id":""}', b'{"session_id":"\\ud800"}', b'{"nested":{"session_id":"sess-1"}}', b'\xef\xbb\xbf{"session_id":"sess-1"}']):
        prompt(f"prompt-invalid-input-{index}", raw=raw)
    prompt("prompt-session-max", raw=json.dumps({"session_id": "a" * 128}).encode())
    prompt("prompt-session-too-long", raw=json.dumps({"session_id": "a" * 129}).encode())
    return result


def expected(case, env):
    """Independent wire, output and cursor assertions for these frozen cells."""
    encode = lambda raw: base64.b64encode(raw).decode("ascii")
    name = case["id"]
    marks = {k: v for k, v in case["pre_files_b64"].items() if k.endswith(".mark")}
    wire = []
    output = b""
    code = 0
    token = env.get("PSEUDOLIFE_MCP_TOKEN") or None
    if case["mode"] == "briefing":
        if name == "briefing-help":
            # Exact help text is separately compared with the public oracle.
            return {"wire": [], "exit_code": 0, "marks": marks}
        if name.startswith("briefing-error"):
            code = 2
        elif name != "briefing-coordination-off":
            wire.append({"method": "GET", "target": "/health", "body_b64": "", "authorization": None})
            if name not in {"briefing-health-non-json", "briefing-health-null", "briefing-token-nonlatin"}:
                target = "/api/briefing?max_unsure=3&max_lessons=3&max_world=3"
                if name == "briefing-custom-negative-unknown":
                    target = "/api/briefing?max_unsure=-2&max_lessons=4&max_world=5"
                elif "--coordination" in case["argv"]:
                    target = "/api/hook/coordination-start"
                elif "--hook-json" in case["argv"]:
                    target = "/api/hook/session-start"
                    if name == "briefing-launcher-default" and __import__("os").name != "nt":
                        from urllib.parse import quote
                        target += "?launcher=" + quote(env["XDG_DATA_HOME"] + "/pseudolife-mcp/bin/pseudolife-mcp", safe="")
                    elif name == "briefing-launcher-override" and __import__("os").name == "nt":
                        from urllib.parse import quote
                        from pathlib import Path
                        target += "?launcher=" + quote(str(Path(env["PSEUDOLIFE_SHIM_LAUNCHER"])), safe="")
                wire.append({"method": "GET", "target": target, "body_b64": "", "authorization": "Bearer " + token if token else None})
                status, body = case["peer"]["content"]
                if status == 200 and name != "briefing-content-invalid-utf8":
                    text = base64.b64decode(body).decode()
                    if "--hook-json" not in case["argv"] and "--coordination" not in case["argv"]:
                        text = json.loads(text).get("markdown") or ""
                    text = text.strip()
                    if text:
                        if "--hook-json" in case["argv"]:
                            text = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
                        output = (text + "\n").encode()
    else:
        payload = None
        try:
            payload = json.loads(base64.b64decode(case["stdin_b64"]).decode("utf-8", "replace"))
        except ValueError:
            pass
        session = payload.get("session_id") if isinstance(payload, dict) else None
        import re
        if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session) and name != "prompt-token-file-missing":
            mark = ".pseudolife-mcp/digests/" + hashlib.sha256(session.encode()).hexdigest() + ".mark"
            since = ""
            if mark in marks:
                raw = base64.b64decode(marks[mark])
                try:
                    since = raw.decode("ascii").splitlines()[0].strip()
                except (UnicodeError, IndexError):
                    pass
                if re.fullmatch(r"[0-9.]{1,22}", since) is None:
                    since = ""
            target = "/api/hook/memory-changes?session_id=" + session
            if since:
                target += "&since=" + since
            if "PSEUDOLIFE_MCP_TOKEN_FILE" in env:
                token = "synthetic-baseline-fixture"
            wire.append({"method": "GET", "target": target, "body_b64": "", "authorization": "Bearer " + token if token else None})
            status, body = case["peer"]["content"]
            if status == 200:
                try:
                    text = base64.b64decode(body).decode("utf-8")
                    cursor, _, note = text.partition("\n")
                    cursor = cursor.rstrip("\r")
                    note = note.rstrip("\r\n")
                    if re.fullmatch(r"[0-9.]{1,22}", cursor):
                        marks[mark] = encode((cursor + "\n").encode())
                        if note:
                            output = (json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}) + "\n").encode()
                except UnicodeError:
                    pass
    if __import__("os").name == "nt":
        output = output.replace(b"\n", b"\r\n")
    return {"wire": wire, "stdout_b64": encode(output), "exit_code": code, "marks": marks}


def reduction_cases():
    """Append native policy controls while retaining every historical oracle case."""
    import copy
    result = cases()
    plain = next(case for case in result if case["id"] == "briefing-plain")
    for name, body in [
        ("briefing-json-nan-extra", b'{"markdown":"ok","other":NaN}'),
        ("briefing-json-infinity-extra", b'{"markdown":"ok","other":Infinity}'),
        ("briefing-json-surrogate-extra", b'{"markdown":"ok","other":"\\ud800"}'),
        ("briefing-json-beyond-u64-extra", b'{"markdown":"ok","other":18446744073709551616}'),
        ("briefing-json-deep-extra", b'{"markdown":"ok","other":' + b'[' * 200 + b'0' + b']' * 200 + b'}'),
        ("briefing-json-malformed", b'{"markdown":"ok",}'),
        ("briefing-markdown-list", b'{"markdown":["ok"]}'),
        ("briefing-markdown-object", b'{"markdown":{"text":"ok"}}'),
        ("briefing-markdown-zero", b'{"markdown":0}'),
        ("briefing-markdown-null", b'{"markdown":null}'),
        ("briefing-markdown-missing", b'{"other":true}'),
    ]:
        case = copy.deepcopy(plain)
        case["id"] = name
        case["peer"]["content"] = [200, base64.b64encode(body).decode("ascii")]
        result.append(case)
    prompt = next(case for case in result if case["id"] == "prompt-baseline")
    case = copy.deepcopy(prompt)
    case["id"] = "prompt-json-beyond-u64-extra"
    case["stdin_b64"] = base64.b64encode(b'{"session_id":"sess-1","other":18446744073709551616}').decode("ascii")
    result.append(case)
    return result


def native_expected(case, env):
    """Named candidate substitutions; expected() remains the raw Python contract."""
    import copy
    encode = lambda raw: base64.b64encode(raw).decode("ascii")
    marks = {k: v for k, v in case["pre_files_b64"].items() if k.endswith(".mark")}
    name = case["id"]
    policies = []
    changed = copy.deepcopy(case)
    diagnostic = None
    if name == "briefing-custom-negative-unknown":
        policies.append("hook-ascii-numeric")
        usage = "usage: pseudolife-mcp briefing [-h] [--max-unsure MAX_UNSURE]\n                               [--max-lessons MAX_LESSONS]\n                               [--max-world MAX_WORLD] [--hook-json]\n                               [--coordination]\n"
        diagnostic = usage + "pseudolife-mcp briefing: error: argument --max-lessons: invalid int value: '００_４'\n"
        result = {"wire": [], "stdout_b64": "", "exit_code": 2, "marks": marks}
    elif case["mode"] == "prompt-hook" and name in {"prompt-json-nan-extra", "prompt-json-surrogate-extra"}:
        policies.append("hook-strict-json-refusal")
        result = {"wire": [], "stdout_b64": "", "exit_code": 0, "marks": marks}
    elif name == "briefing-health-non-json":
        policies.append("hook-strict-json-refusal")
        result = expected(case, env)
        result["exit_code"] = 1
        diagnostic = "pseudolife-mcp briefing: daemon reply not understood\n"
    elif name in {"briefing-json-nan-extra", "briefing-json-infinity-extra", "briefing-json-surrogate-extra",
                  "briefing-json-malformed", "briefing-json-deep-extra", "briefing-markdown-false",
                  "briefing-markdown-list", "briefing-markdown-object", "briefing-markdown-zero"}:
        policies.append("hook-typed-markdown" if name.startswith("briefing-markdown") else
                        "hook-bounded-json-nesting" if name == "briefing-json-deep-extra" else "hook-strict-json-refusal")
        changed["peer"]["content"] = [200, encode(b'{"markdown":""}')]
        result = expected(changed, env)
        result["exit_code"] = 1
        diagnostic = "pseudolife-mcp briefing: daemon reply not understood\n"
    else:
        result = expected(case, env)
        if name == "prompt-mark-nonascii-after-lf":
            policies.append("hook-first-line-cursor")
            result["wire"][0]["target"] += "&since=100.0"
    if diagnostic is not None:
        if __import__("os").name == "nt":
            diagnostic = diagnostic.replace("\n", "\r\n")
        result["stderr_b64"] = encode(diagnostic.encode())
    return {**result, "policies": policies}
