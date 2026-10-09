"""Generate the shipped-producer census using source text only (no daemon imports)."""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "rust/producer-census.json"
INVENTORY = "rust/contract-inventory.json"
CURRENCY_INPUTS = {"rust/producer_census.py", "rust/producer-census.json", INVENTORY}
SCOPES = ("plugin", "pseudolife_memory/web/static", "evals/claude_shim.py",
          "evals/codex_shim.py", "rust/shim/src", "ops", "examples", "docs")
EXTENSIONS = {".py", ".sh", ".ps1", ".bat", ".cmd", ".md", ".js", ".json", ".example",
              ".yaml", ".yml", ".toml", ".rs", ".html", ".txt"}
CONTRACT_SOURCES = (INVENTORY, "pseudolife_memory/mcp_server.py",
                    "pseudolife_memory/web/routes.py", "pseudolife_memory/web/api.py",
                    "pseudolife_memory/coordination.py", "pseudolife_memory/utils/config.py")

# Changed fingerprints withdraw trusted manual shapes and report unresolved drift.
# Recheck the cited construction/conditions before refreshing any fingerprint.
MANUAL_SHA256 = {
    'plugin/hooks/lifecycle.ps1': 'ae43493784145e5808512f2e3e4142d59a4d7902706f6758612ab0673430938e',
    'plugin/hooks/session-start.sh': 'd98b867a79d74049802aee8a6457708c0c28844b11a679bb198d90b2c2d2e798',
    'plugin/hooks/session-end.sh': 'dd4b3363c862443c89e613887c551179f76b874a1f8098002730532c6da30463',
    'plugin/hooks/subagent-board.sh': '6c62da28e230afc022d3424cf2c49449b6cf2a103daa80849871ab7e28d69b8b',
    'plugin/hooks/stop-wake.sh': '15a13bde5ec2dd2de9dc5ef1c8250bf8fcd404be0f68308eb28456fa26253347',
    'rust/shim/src/cli/episode.rs': '2e91f8a822f0cf79daa16276ded3430394e8606d08ab259d7ac9dbfa8022729a',
    'rust/shim/src/board/adapter.rs': 'e033c719f4f32ee2a3513923481e5ef9c75617a143981ca2c814e4b6e2c4fffc',
}


def manual_calls(root: Path) -> list[dict]:
    result = []

    def add(path, anchor, name, method, fields, *, kind="route", channel="query", conditions=None, source_anchor=None):
        if not (root / path).is_file() or input_hashes(root, [path])[path] != MANUAL_SHA256[path]:
            return
        text = (root / path).read_text(encoding="utf-8-sig")
        positions = [m.start() for m in re.finditer(re.escape(anchor), text)]
        assert len(positions) == 1, f"manual anchor is ambiguous or absent: {path}: {anchor}"
        line = text.count("\n", 0, positions[0]) + 1
        call = record(kind, name, path, line, {key: value_shape(value) for key, value in fields.items()},
                      method=method, channel=channel)
        call["evidence"] = "manual"
        call["resolution"] = "Reviewed request construction and conditional fields; guarded by whole-file source fingerprint."
        call["shape"]["conditions"] = conditions or {}
        if source_anchor:
            position = text.index(source_anchor)
            call["construction_source"] = f"{path}:{text.count(chr(10), 0, position) + 1}"
        result.append(call)

    ps = "plugin/hooks/lifecycle.ps1"
    add(ps, '"$daemonUrl/api/hook/park-gate?$gateQuery"', "/api/hook/park-gate", "GET",
        {"agent": "$agentId", "since": "$since"}, conditions={"since": "Only when the saved turn-start is 1-12 ASCII digits."}, source_anchor='$gateQuery = "agent=$agentId"')
    add(ps, '"$daemonUrl/api/hook/coordination-start"', "/api/hook/coordination-start", "GET", {})
    add(ps, '"$daemonUrl/api/hook/memory-changes$query"', "/api/hook/memory-changes", "GET",
        {"session_id": "$sessionId", "since": "$since"}, conditions={"since": "Only when the saved cursor matches 1-22 digits/dots."}, source_anchor="$query = '?session_id='")
    add(ps, '"$daemonUrl/api/hook/memory-policy$query"', "/api/hook/memory-policy", "GET",
        {"session_id": "$sid", "source": "$startReason"}, conditions={"session_id,source": "Both omitted when sid is empty."}, source_anchor="$query = if ($sid)")
    add(ps, '"$daemonUrl/api/hook/session-start$query"', "/api/hook/session-start", "GET",
        {"session_id": "$sid", "source": "$startReason", "plugin_version": "$pluginVersion", "plugin_hooks_digest": "$hooksDigest", "launcher": "$launcher"},
        conditions={"session_id,source": "Only with sid.", "plugin_version": "Only with a plugin version.", "plugin_hooks_digest": "Only with a 64-character lowercase hex digest.", "launcher": "Only with an off-PATH launcher."}, source_anchor="$pairs = @()")
    add(ps, '"$daemonUrl/api/hook/session-end"', "/api/hook/session-end", "POST", {"session_id": "$sid"}, channel="body", source_anchor="$body = @{session_id = $sid}")
    sh = "plugin/hooks/session-start.sh"
    add(sh, '"${URL}/api/hook/memory-changes?session_id=', "/api/hook/memory-changes", "GET",
        {"session_id": "${SID}", "since": "${SINCE}"}, conditions={"since": "Only when SINCE is nonempty after cursor validation."})
    add(sh, '"${URL}/api/hook/memory-policy${PQS}"', "/api/hook/memory-policy", "GET",
        {"session_id": "${SID}", "source": "${SRC}"}, conditions={"session_id,source": "Both omitted when SID is empty."}, source_anchor='PQS=""')
    add(sh, '"${URL}/api/hook/session-start${QS}"', "/api/hook/session-start", "GET",
        {"session_id": "${SID}", "source": "${SRC}", "plugin_version": "${PLUGIN_VERSION}", "plugin_hooks_digest": "${PLUGIN_HOOKS_DIGEST}", "launcher": "${ENCODED}"},
        conditions={"session_id,source": "Only with SID.", "plugin_version": "Only with a parsed nonempty version.", "plugin_hooks_digest": "Only with a 64-character lowercase hex digest.", "launcher": "Only when the launcher is a file and PATH does not find that same file."}, source_anchor='QS=""')
    add("plugin/hooks/session-end.sh", '"${URL}/api/hook/session-end"', "/api/hook/session-end", "POST", {"session_id": "${SID}"}, channel="body", source_anchor='-d "{')
    add("plugin/hooks/subagent-board.sh", '"$URL/api/hook/subagent?', "/api/hook/subagent", "POST", {"agent": "$AGENT", "event": "$EVENT", "child": "$CHILD", "type": "$KIND"})
    add("plugin/hooks/stop-wake.sh", '"$GATE_URL/api/hook/park-gate?$1"', "/api/hook/park-gate", "GET", {"agent": "$AGENT_ID", "since": "$SINCE"}, conditions={"since": "Only with nonempty SINCE."}, source_anchor='QUERY="agent=$AGENT_ID"')
    add("plugin/hooks/stop-wake.sh", '"$GATE_URL/api/hook/woke?agent=', "/api/hook/woke", "POST", {"agent": "$agent"})
    rs = "rust/shim/src/cli/episode.rs"
    add(rs, 'format!("/api/episode/{path}"', "/api/episode/start", "POST", {"session_key": "key", "title": "title"}, channel="body", source_anchor='let (path, body) = if mode')
    add(rs, 'format!("/api/episode/{path}"', "/api/episode/end", "POST", {"session_key": "key"}, channel="body", source_anchor='let (path, body) = if mode')
    rs = "rust/shim/src/board/adapter.rs"
    add(rs, 'self.post_retry("heartbeat", &body)', "heartbeat", "POST",
        {"attachment_id": "self.attachment", "generation": "generation", "ring_armed_until": "self.ring_armed_until()", "active": "true"}, kind="coordination", channel="body",
        conditions={"ring_armed_until": "Only with ring+liveness, removed on unexpected_parameter before retry.", "active": "Only for an observed active turn, removed on unexpected_parameter before retry."}, source_anchor='pub async fn heartbeat(&self)')
    add(rs, 'self.post_retry("receive",&body)', "receive", "POST",
        {"after": "after", "limit": "50", "wait_seconds": "30", "attachment_id": "self.attachment", "generation": "generation"}, kind="coordination", channel="body", source_anchor='let body = json!({"after":after')
    return result


def manual_drift(root: Path) -> list[dict]:
    result = []
    for path, expected in sorted(MANUAL_SHA256.items()):
        actual = input_hashes(root, [path])[path] if (root / path).is_file() else None
        if actual != expected:
            result.append({"path": path, "location": f"{path}:1", "dynamic": "unresolved",
                           "reason": "Manual source fingerprint changed or source is missing; prior trusted shapes withdrawn.",
                           "expected_sha256": expected, "actual_sha256": actual})
    return result


def producer_paths(root: Path = ROOT) -> list[str]:
    """Enumerate only Git-tracked shipped text; never ingest private scratch files."""
    paths = []
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--", *SCOPES], cwd=root).decode("utf-8").split("\0")
    for relative in tracked:
        if not relative:
            continue
        path = root / relative
        if (path.is_file() and (path.suffix in EXTENSIONS or path.name.startswith("Dockerfile"))
                and not any(part in {"__pycache__", "node_modules", "vendor", "target", ".git"}
                            for part in Path(relative).parts)
                and not path.name.endswith(("_tests.rs", ".test.ts"))):
            paths.append(relative)
    return sorted(set(paths))


def input_hashes(root: Path, paths: list[str]) -> dict[str, str]:
    # Git's platform checkout newline conversion must not invalidate source identity.
    return {path: hashlib.sha256((root / path).read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()
            for path in sorted(paths)}


def value_shape(expr: str) -> dict:
    expr = expr.strip()
    if expr in {"!0", "!1"}:
        return {"literal": expr == "!0"}
    if expr in {"true", "false", "null", "None", "True", "False"}:
        return {"literal": {"true": True, "false": False, "null": None,
                            "None": None, "True": True, "False": False}[expr]}
    # Template strings with interpolation are expressions, never literals.
    if len(expr) >= 2 and expr[0] == expr[-1] == "`" and "${" not in expr:
        return {"literal": expr[1:-1]}
    try:
        value = ast.literal_eval(expr)
        json.dumps(value, allow_nan=False)
        if isinstance(value, (str, int, float, bool, list, dict, type(None))):
            return {"literal": value}
    except (ValueError, SyntaxError, TypeError):
        pass
    return {"expression": expr}


def split_top(text: str, delimiter: str = ",") -> list[str]:
    """Split a supported expression without splitting strings or nested containers."""
    result, stack, quote, start, escaped = [], [], None, 0, False
    pairs = {"(": ")", "[": "]", "{": "}"}
    for i, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'`":
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == delimiter and not stack:
            result.append(text[start:i].strip())
            start = i + 1
    result.append(text[start:].strip())
    return result


def balanced(text: str, start: int) -> tuple[str, int]:
    """Return a balanced call/container; an incomplete example stays unresolved."""
    stack, quote, escaped = [], None, False
    pairs = {"(": ")", "[": "]", "{": "}"}
    for i in range(start, len(text)):
        char = text[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return text[start + 1:i], i + 1
    return text[start + 1:text.find("\n", start) if "\n" in text[start:] else len(text)], -1


def object_fields(expr: str) -> tuple[dict, bool]:
    expr = expr.strip()
    if not expr.startswith("{") or not expr.endswith("}"):
        return {}, False
    fields, complete = {}, True
    for item in split_top(expr[1:-1]):
        if not item:
            continue
        if item.startswith(("...", "**")):
            complete = False
            continue
        pair = split_top(item, ":")
        key = pair[0].strip("\"'`")
        if not re.fullmatch(r"[A-Za-z_][\w-]*", key):
            complete = False
            continue
        fields[key] = value_shape(":".join(pair[1:]) if len(pair) > 1 else key)
    return fields, complete


def record(kind: str, name: str, path: str, line: int, parameters: dict | None = None,
           *, complete: bool = True, method: str | None = None,
           channel: str = "parameters", expression: str | None = None) -> dict:
    result = {"kind": kind, "name": name, "location": f"{path}:{line}",
              "evidence": "documented" if Path(path).suffix == ".md" else "static",
              "shape": {"channel": channel, "parameters": parameters or {}, "complete": complete}}
    if method:
        result["method"] = method
    if not complete:
        result["dynamic"] = "unresolved"
        if expression:
            result["shape"]["expression"] = expression
    return result


def tool_calls(text: str, path: str) -> list[dict]:
    calls = []
    for match in re.finditer(r"\b((?:memory_|document_)[a-z_]+)\s*\(", text):
        # Definitions and prose saying a name without call syntax are not calls.
        if re.search(r"(?:def|fn)\s+$", text[max(0, match.start() - 20):match.start()]):
            continue
        args, end = balanced(text, match.end() - 1)
        gap = text[match.start() + len(match[1]):match.end() - 1]
        if gap and not path.endswith(".py") and not re.search(r"=|^[\s]*[\"']|^[\s]*$", args):
            continue  # Prose tier/semantic parentheticals have no confirmed call syntax.
        fields, positional, complete = {}, [], end != -1
        try:
            parsed = ast.parse(f"{match[1]}({args})", mode="eval").body
            items = ([f"{kw.arg}={ast.unparse(kw.value)}" if kw.arg else "**" + ast.unparse(kw.value)
                      for kw in parsed.keywords] + [ast.unparse(arg) for arg in parsed.args])
        except SyntaxError:
            items = split_top(args)
            complete = False  # Unsupported documentation syntax cannot establish omissions.
        for item in items:
            if not item:
                continue
            assignment = re.match(r"^([A-Za-z_]\w*)\s*=(?!=)(.*)$", item, re.S)
            if assignment:
                fields[assignment[1]] = value_shape(assignment[2])
            else:
                positional.append(value_shape(item))
                if item.startswith(("*", "...")) or "<" in item or "|" in item:
                    complete = False
        call = record("tool", match[1], path, text.count("\n", 0, match.start()) + 1,
                      fields, complete=complete, expression=args)
        if positional:
            call["shape"]["positional"] = positional
        calls.append(call)
    return calls


def console_calls(text: str, path: str) -> list[dict]:
    """Resolve the compiled Console's get/post helper names from their definitions."""
    helpers = {"get": "GET", "post": "POST"}
    for match in re.finditer(r"function\s+(\w+)\([^)]*\)\s*\{return\s+\w+\([`\"'](GET|POST)[`\"']", text):
        helpers[match[1]] = match[2]
    calls = []
    functions = {}
    for match in re.finditer(r"function\s+(\w+)\(([^)]*)\)\s*\{return\s*\{", text):
        body, end = balanced(text, match.end() - 1)
        if end != -1:
            functions[match[1]] = (split_top(match[2]), "{" + body + "}")
    pattern = r"\b(" + "|".join(map(re.escape, helpers)) + r")\s*(?:<[^;\n]*?>)?\s*\(\s*([`\"'])(/api/[^`\"']+|/health)\2"
    for match in re.finditer(pattern, text):
        args, end = balanced(text, text.index("(", match.start()))
        parts = split_top(args)
        expr = parts[1] if len(parts) > 1 else "{}"
        fields, complete = object_fields(expr)
        resolution = re.fullmatch(r"(\w+)\((.*)\)", expr, re.S)
        if resolution and resolution[1] in functions:
            names, returned = functions[resolution[1]]
            fields, complete = object_fields(returned)
            substitutions = dict(zip(names, split_top(resolution[2])))
            for key, value in fields.items():
                if "expression" in value:
                    expression = value["expression"]
                    for name, arg in substitutions.items():
                        expression = re.sub(r"\b" + re.escape(name) + r"\b", lambda _: arg, expression)
                    fields[key] = value_shape(expression)
        route = match[3]
        calls.append(record("route", route, path, text.count("\n", 0, match.start()) + 1,
                            fields, method=helpers[match[1]], complete=complete and end != -1,
                            channel="query" if helpers[match[1]] == "GET" else "body", expression=expr))
    # The review dispatcher consumes {path, body} plans and calls post(path, body).
    for match in re.finditer(r"\bpath\s*:\s*([`\"'])(/api/[^`\"']+)\1\s*,\s*body\s*:\s*", text):
        start = match.end()
        if start < len(text) and text[start] == "{":
            body, end = balanced(text, start)
            fields, complete = object_fields("{" + body + "}")
        else:
            fields, complete, end = {}, False, -1
        calls.append(record("route", match[2], path, text.count("\n", 0, match.start()) + 1,
                            fields, method="POST", complete=complete and end != -1, channel="body"))
    return sorted(calls, key=lambda call: (int(call["location"].rsplit(":", 1)[1]), call["name"]))


def http_mentions(text: str, path: str) -> list[dict]:
    """Keep unsupported request syntax as evidence with unknown method/body."""
    calls = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("#", "//")) and Path(path).suffix != ".md":
            continue
        for match in re.finditer(r"(?:/api/[A-Za-z0-9_/-]+(?:\{[^}\n]+\})?|/health\b|/mcp\b)", line):
            route = match[0].rstrip("/-")
            if not (re.search(r"curl|Invoke-WebRequest|Invoke-RestMethod|fetch|urlopen|Request\(|\.get\(|\.post\(|format!|https?://|\bGET\b|\bPOST\b", line)
                    or Path(path).suffix in {".md", ".rs"}):
                continue
            method_match = re.search(r"(?:-X\s*|-Method\s+|\b)(GET|POST)\b", line, re.I)
            method = method_match[1].upper() if method_match else None
            if method is None and re.search(r"Invoke-WebRequest", line):
                method = "POST" if re.search(r"--data|\s-d\s|-Body\b", line) else "GET"
            query = re.split(r"[\s`\"']", line[match.end():], maxsplit=1)[0]
            params = {k: value_shape(repr(v)) if "$" not in v else {"expression": v}
                      for k, v in parse_qsl(query.lstrip("?"), keep_blank_values=True)} if query.startswith("?") else {}
            call = record("route", route, path, number, params, method=method,
                                complete=False, channel="query" if method == "GET" else "body",
                                expression="Request helper, URL suffix or body requires resolution at this location.")
            if method is None:
                call["evidence"] = "unverified"
                call["method"] = None
            calls.append(call)
    return calls


def cli_calls(text: str, path: str) -> list[dict]:
    calls = []
    fenced_lines, fenced = set(), False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced:
            fenced_lines.add(number)
    # Literal executable invocations and documented commands, never the package name alone.
    pattern = r"\bpseudolife-mcp(?:\.exe)?[\"']?[ \t]+([a-z][a-z-]*|--help|--version)([^\n]*)"
    for match in re.finditer(pattern, text):
        number = text.count("\n", 0, match.start()) + 1
        prefix = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
        if not path.endswith(".md") and (prefix.lstrip().startswith(("#", "//")) or "#" in prefix or re.search(r"bare\s+$|pipx'?s\s+$|pipx\s+runpip\s+$", prefix)):
            continue
        if re.search(r"\bpipx?\s+runpip\s+$", prefix):
            continue  # Package argument to another CLI, not our executable.
        if path.endswith(".md") and number not in fenced_lines and prefix.count("`") % 2 == 0:
            continue  # Ordinary prose names the executable but is not a copied command.
        tail = match[2].split("`", 1)[0].strip() if path.endswith(".md") else match[2].strip()
        if not path.endswith(".md") and match[1] in {"from", "to", "still", "venv"}:
            continue
        if not path.endswith(".md") and re.search(r"\bexited\b|\bcould not\b|\bfailed\b|\bwas not\b", tail):
            continue
        mode = {"--help": "help", "--version": "version"}.get(match[1], match[1])
        complete = Path(path).suffix == ".md" and not any(x in tail for x in ("$", "...", "<", "[", "]", "|"))
        flags = cli_flags(tail)
        call = record("cli", mode, path, text.count("\n", 0, match.start()) + 1,
                      flags, complete=complete, channel="argv", expression=tail)
        # Raw argv evidence is intentionally retained: option parsing is not emulated.
        call["shape"]["argv_source"] = match[1] + (" " + tail if tail else "")
        calls.append(call)
    return calls


def shell_tokens(text: str) -> list[str]:
    """Linear token boundaries for supported quoted argv; preserve source spelling."""
    tokens, start, quote, escaped = [], None, None, False
    for i, char in enumerate(text):
        if start is None:
            if char.isspace():
                continue
            start = i
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char.isspace():
            tokens.append(text[start:i])
            start = None
    if start is not None:
        tokens.append(text[start:])
    return tokens


def cli_flags(tail: str) -> dict:
    """Resolve supported argv tokens, retaining quoted shell/PowerShell expansions."""
    tokens = shell_tokens(tail)
    switches = {"--yes", "--json", "--dry-run", "--read-code", "--read-token", "--hook-json",
                "--check", "--all", "--daemon-only", "--clients-only", "--force", "--quiet"}
    flags = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # Synopsis groups and shell command-substitution closers surround a
        # switch name; never strip parentheses from an expression-valued operand.
        surrounded = re.fullmatch(r"\[*(--[a-z][a-z-]*)[\])]*", token)
        if surrounded:
            token = surrounded[1]
        match = re.fullmatch(r"(--[a-z][a-z-]*)(?:=(.*))?", token)
        if not match:
            i += 1
            continue
        name, assigned = match.groups()
        if name in switches and assigned is None:
            flags[name] = {"literal": True}
        else:
            if assigned is None and i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                i += 1
                assigned = tokens[i]
            flags[name] = ({"expression": assigned} if assigned is not None and "$" in assigned
                           else value_shape(assigned) if assigned is not None
                           else {"expression": "value not established"})
        i += 1
    return flags


def installer_cli_calls(text: str, path: str) -> list[dict]:
    calls = []
    # The named installer resolvers bind these executable variables to the installed CLI.
    # Restrict to command position (line/start, substitution, separator, then/try), never argv.
    pattern = r'(?:"\$SHIM_PATH"|&[ \t]+\$(?:script:shimInstallPath|shim))[ \t]+([a-z][a-z-]*)'
    for match in re.finditer(pattern, text, re.M):
        prefix = text[text.rfind("\n", 0, match.start()) + 1:match.start()].strip()
        tokens = shell_tokens(prefix)
        env_prefix = bool(tokens) and all(re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", token) for token in tokens)
        command_position = (not prefix or prefix.endswith(("$(", "{", "|", ";"))
                            or re.search(r"\bthen$", prefix)
                            or (match[0].startswith("&") and prefix.endswith("=")) or env_prefix)
        if match[0].startswith('"') and prefix.endswith("="):
            command_position = False
        if not command_position:
            continue
        tail, quote, escaped = [], None, False
        remaining = text[match.end():]
        for index, char in enumerate(remaining):
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\" and quote == '"':
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char in ";|\n)#" or remaining.startswith("&&", index):
                break
            tail.append(char)
        snippet = "pseudolife-mcp " + match[1] + "".join(tail)
        extracted = cli_calls(snippet, path)
        for call in extracted:
            call["location"] = f"{path}:{text.count(chr(10), 0, match.start()) + 1}"
            call["shape"]["executable_expression"] = match[0].split(match[1], 1)[0].strip()
        calls.extend(extracted)
    return calls


def module_cli_calls(text: str, path: str) -> list[dict]:
    """Recognize text/argv-array launches through Python's -m CLI entry point."""
    result = []
    for match in re.finditer(r"-m[ \t]+pseudolife_memory\.cli[ \t]+([a-z][a-z-]*)([^\n]*)", text):
        calls = cli_calls("pseudolife-mcp " + match[1] + match[2], path)
        for call in calls:
            call["location"] = f"{path}:{text.count(chr(10), 0, match.start()) + 1}"
            call["shape"]["executable_expression"] = "python -m pseudolife_memory.cli"
        result.extend(calls)
    # Static argv containers in Dockerfile/Python/Rust; -m immediately precedes the module.
    pattern = r'''["']-m["'](?:\.into\(\))?\s*,\s*["']pseudolife_memory\.cli["'](?:\.into\(\))?\s*,'''
    for match in re.finditer(pattern, text):
        tail = text[match.end():]
        # The next closing array bracket bounds forwarding evidence as well as literals.
        finish = tail.find("]")
        argv_source = tail[:finish] if finish >= 0 else tail.split("\n", 1)[0]
        parts = split_top(argv_source)
        if not parts or not parts[0]:
            continue
        cleaned = [re.sub(r"\.into\(\)$", "", token.strip()) for token in parts if token]
        mode = value_shape(cleaned[0]).get("literal")
        if isinstance(mode, str):
            tokens = [value_shape(value).get("literal", value) for value in cleaned[1:]]
            flags_source = " ".join(str(value) for value in tokens)
            call = record("cli", mode, path, text.count("\n", 0, match.start()) + 1,
                          cli_flags(flags_source), complete=False, channel="argv", expression=argv_source)
        else:
            call = record("cli", "<forwarded-mode>", path, text.count("\n", 0, match.start()) + 1,
                          complete=False, channel="argv", expression=argv_source)
        call["shape"]["argv"] = [value_shape(value) for value in cleaned]
        call["shape"]["executable_expression"] = "python -m pseudolife_memory.cli"
        result.append(call)
    return result


def forwarded_coordination(calls: list[dict]) -> list[dict]:
    result = []
    for call in calls:
        if call["kind"] != "tool" or call["name"] not in {"memory_agents", "memory_message"}:
            continue
        action = call["shape"]["parameters"].get("action", {}).get("literal")
        bare = call["shape"]["parameters"].get("action", {}).get("expression")
        unverified = action is None and bare in {"list", "update", "claim", "release", "send", "receive", "ack", "history"}
        if unverified:
            action = bare
        if call["name"] == "memory_agents":
            if action is None and "action" not in call["shape"]["parameters"] and call["shape"]["complete"]:
                action = "list"
            target = {"list": "agents", "update": "update", "claim": "lease", "release": "release"}.get(action)
            mapping = "pseudolife_memory/coordination.py:835"
        else:
            target = action if action in {"send", "receive", "ack", "history"} else None
            mapping = "pseudolife_memory/mcp_server.py:650"
        if target:
            forwarded = record("coordination", target, call["location"].rsplit(":", 1)[0],
                               int(call["location"].rsplit(":", 1)[1]), complete=False, channel="body",
                               expression="Tool forwarding filters defaults/nulls and may rename fields; inspect tool_shape and mapping_source.")
            forwarded["evidence"] = "unverified" if unverified else call["evidence"]
            forwarded["forwarded_from_tool"] = call["name"]
            forwarded["tool_shape"] = call["shape"]
            forwarded["mapping_source"] = mapping
            result.append(forwarded)
    return result


def env_calls(text: str, path: str) -> list[dict]:
    result = []
    for line, content in enumerate(text.splitlines(), 1):
        for name in sorted(set(re.findall(r"(?<![A-Z0-9])_?PSEUDOLIFE_[A-Z][A-Z0-9_]*\b", content))):
            if name.endswith("_"):
                continue
            assignment = re.search(re.escape(name) + r"[\"']?\s*[:=]\s*([^\n]+)", content)
            # A reference does not prove an environment write or literal value.
            call = record("environment", name, path, line, complete=False,
                          channel="environment", expression=assignment[1].strip() if assignment else "reference")
            call["usage"] = "assignment-candidate" if assignment else "reference"
            # Only a single shell/env scalar is resolvable without executing it.
            direct = re.search(re.escape(name) + r"(?:\s*=|[\"']\s*:)\s*(\"[^\"\n]*\"|'[^'\n]*'|[^\s`\"';,}\)]+)", content)
            if direct:
                token = direct[1]
                operand = token.strip("\"'")
                closing = content.find(">", direct.start(1))
                candidate = content[direct.start(1):closing + 1] if token.startswith("<") and closing >= direct.end(1) else token
                if operand.startswith("<") and re.search(r"<[^<>\r\n]+>", candidate):
                    # Help operands can contain spaces; preserve the whole
                    # placeholder rather than promoting its first word to a value.
                    if token.startswith("<"):
                        end = max(closing + 1, direct.end(1))
                        while end < len(content) and not content[end].isspace() and content[end] not in "`\"';})":
                            if content[end] == "," and (end + 1 == len(content) or content[end + 1].isspace()):
                                break
                            if content.startswith(("\\n", "\\r", "\\t"), end):
                                break
                            end += 1
                        token = content[direct.start(1):end]
                    call["shape"]["parameters"] = {"value": {"expression": token, "placeholder": True}}
                    call["usage"] = "assignment-example"
                    call["evidence"] = "documented"
                    result.append(call)
                    continue
                scalar = value_shape(token)
                if "$" in token or "\\" in token:
                    scalar = {"expression": token}
                printed = bool(re.search(r"Write-(?:Host|Output)\b", content))
                powershell = path.endswith(".ps1") or bool(re.search(r"\$env:" + re.escape(name), content))
                if "literal" not in scalar and (not powershell or printed) and not any(x in token for x in ("$", "\\", "{", "(", "+")):
                    scalar = {"literal": token}
                if "literal" in scalar:
                    scalar["literal"] = str(scalar["literal"])
                call["shape"]["parameters"] = {"value": scalar}
                call["shape"]["complete"] = True
                call.pop("dynamic", None)
                call["shape"].pop("expression", None)
                call["usage"] = "assignment"
                if printed or content.lstrip().startswith("#"):
                    call["usage"] = "assignment-example"
                    call["evidence"] = "documented"
            result.append(call)
    return result


def python_calls(text: str, path: str) -> list[dict]:
    """Extract actual Python calls, resolving only literal dicts and CLI argv lists."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name.endswith((".get", ".post", ".request", "Request")) and node.args:
            url_expr = ast.unparse(node.args[0])
            match = re.search(r"(/api/[\w/-]+)", url_expr)
            if match:
                method = "POST" if name.endswith(".post") else "GET" if name.endswith(".get") else None
                fields, complete = {}, False
                for kw in node.keywords:
                    if kw.arg == "method" and isinstance(kw.value, ast.Constant):
                        method = kw.value.value
                    if kw.arg in {"json", "params"}:
                        fields, complete = object_fields(ast.unparse(kw.value))
                result.append(record("route", match[1], path, node.lineno, fields, method=method,
                                     complete=complete, channel="query" if method == "GET" else "body",
                                     expression=url_expr))
        if name.endswith((".run", ".Popen", ".check_call", ".check_output")) and node.args:
            argv = node.args[0]
            if isinstance(argv, (ast.List, ast.Tuple)):
                for i, item in enumerate(argv.elts):
                    if isinstance(item, ast.Constant) and item.value == "pseudolife-mcp" and i + 1 < len(argv.elts):
                        mode = argv.elts[i + 1]
                        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                            call = record("cli", mode.value, path, node.lineno, complete=False,
                                          channel="argv", expression=ast.unparse(argv))
                            call["shape"]["argv"] = [value_shape(ast.unparse(x)) for x in argv.elts[i + 1:]]
                            result.append(call)
    return result


def scan_files(root: Path, paths: list[str]) -> list[dict]:
    calls = []
    for path in paths:
        text = (root / path).read_text(encoding="utf-8-sig")
        if path.endswith(".rs"):
            # Inline #[cfg(test)] modules are not shipped producers.
            text = re.split(r"#\[cfg\(test\)\]", text)[0]
        if path.endswith(".js"):
            calls.extend(console_calls(text, path))
        else:
            calls.extend(tool_calls(text, path))
            calls.extend(http_mentions(text, path))
            calls.extend(cli_calls(text, path))
            calls.extend(module_cli_calls(text, path))
            if path in {"ops/install.sh", "ops/install.ps1"}:
                calls.extend(installer_cli_calls(text, path))
            if path.endswith(".py"):
                calls.extend(python_calls(text, path))
                if path == "ops/shim_autostart.py":
                    tree = ast.parse(text)
                    kinds_node = next(n for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "KINDS")
                    kinds = ast.literal_eval(kinds_node.value)
                    for kind, spec in kinds.items():
                        for key in spec["keys"]:
                            call = record("environment", spec["prefix"] + key.upper(), path, kinds_node.lineno,
                                          {"value": {"expression": f"{kind} setting {key}: env/CLI override"}},
                                          channel="environment")
                            call["usage"] = "constructed-key"
                            call["construction_source"] = f"{path}:{text.count(chr(10), 0, text.index('values.get(prefix + key.upper())')) + 1}"
                            calls.append(call)
            if path.endswith(".rs"):
                calls.extend(rust_coordination_calls(text, path))
        calls.extend(env_calls(text, path))
    return calls


def rust_coordination_calls(text: str, path: str) -> list[dict]:
    """The two shipped Rust board transports accept literal actions at call sites."""
    if path not in {"rust/shim/src/board/adapter.rs", "rust/shim/src/cli/lease/board.rs"}:
        return []
    result = []
    for match in re.finditer(r"\b(request|post_retry|post)\s*\(", text):
        if re.search(r"fn\s+$", text[max(0, match.start() - 15):match.start()]):
            continue
        args, end = balanced(text, match.end() - 1)
        parts = split_top(args)
        index = 3 if match[1] == "request" else 0
        if len(parts) <= index + 1:
            continue
        action = value_shape(parts[index]).get("literal")
        if not isinstance(action, str):
            continue  # The shared forwarding call is listed in unresolved_patterns.
        body = parts[index + 1].lstrip("&")
        if body.startswith("json!(") and body.endswith(")"):
            fields, complete = object_fields(body[6:-1])
        else:
            fields, complete = {}, False
        result.append(record("coordination", action, path, text.count("\n", 0, match.start()) + 1,
                             fields, complete=complete and end != -1, channel="body", expression=body))
    return result


def config_aliases(root: Path) -> dict[str, str]:
    """Map actual YAML/dotted paths to the inventory's dataclass field identities."""
    tree = ast.parse((root / "pseudolife_memory/utils/config.py").read_text(encoding="utf-8-sig"))
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    aliases = {}

    def walk(name, prefix, seen):
        if name in seen or name not in classes:
            return
        for node in classes[name].body:
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            field = node.target.id
            key = f"{prefix}.{field}" if prefix else field
            aliases[key] = f"{name}.{field}"
            child = ast.unparse(node.annotation)
            if child in classes:
                aliases[key + "."] = child
                walk(child, key, seen | {name})
    walk("AppConfig", "", set())
    return aliases


def config_calls(text: str, path: str, aliases: dict[str, str]) -> list[dict]:
    """Read explicit dotted config names and YAML examples; retain uncertain syntax."""
    result, stack, fenced, yaml = [], [], False, False
    is_document = path.endswith(".md")
    for line, content in enumerate(text.splitlines(), 1):
        if content.lstrip().startswith("```"):
            fenced = not fenced
            yaml = fenced and content.strip() in {"```yaml", "```yml"}
            stack = []
            continue
        if yaml or path.endswith((".yaml", ".yml", ".yaml.example")):
            match = re.match(r"^(\s*)([A-Za-z_]\w*):\s*(.*)$", content)
            if match:
                indent = len(match[1])
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                key = ".".join([item[1] for item in stack] + [match[2]])
                rhs = match[3].split(" #", 1)[0].strip()
                if not rhs:
                    stack.append((indent, match[2]))
                if key in aliases:
                    name = aliases.get(key + ".", aliases[key]) if not rhs else aliases[key]
                    fields = {"value": value_shape(rhs)} if rhs else {}
                    result.append(record("config", name, path, line, fields, complete=bool(rhs),
                                         channel="config", expression="nested YAML section" if not rhs else rhs))
                elif key.startswith(("embedding.", "memory.", "storage.", "coordination.", "updates.")) and rhs:
                    result.append(record("config", key, path, line, {"value": value_shape(rhs)}, channel="config"))
        # A dotted reference is evidence of the key, not a complete config write.
        for match in re.finditer(r"\b(?:embedding|memory|storage|coordination|updates|context|time|memory_policy)\.[a-z_][a-z_0-9.]*", content):
            if content[:match.start()].endswith("pseudolife_memory.") or content.lstrip().startswith(("from ", "import ")):
                continue
            key = match[0].rstrip(".")
            if key in aliases:
                call = record("config", aliases[key], path, line, complete=False, channel="config",
                              expression="documented key" if is_document else "config access/reference")
                call["usage"] = "reference"
                result.append(call)
    return result


def parameter_contracts(root: Path) -> tuple[dict, dict]:
    """Current advertised tool signatures and route optional reads, statically."""
    tools = {}
    tree = ast.parse((root / "pseudolife_memory/mcp_server.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(("memory_", "document_")):
            args = node.args.posonlyargs + node.args.args
            required_count = len(args) - len(node.args.defaults)
            tools[node.name] = {"parameters": [a.arg for a in args + node.args.kwonlyargs],
                                "optional": [a.arg for a in args[required_count:]] +
                                [a.arg for a, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None],
                                "source": f"pseudolife_memory/mcp_server.py:{node.lineno}"}
    tree = ast.parse((root / "pseudolife_memory/web/routes.py").read_text(encoding="utf-8-sig"))
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    routes = {}

    def reads(node, seen):
        optional, required = set(), set()
        for part in ast.walk(node):
            if isinstance(part, ast.Subscript) and isinstance(part.value, ast.Name) and part.value.id in {"q", "b", "params", "body"}:
                if isinstance(part.slice, ast.Constant) and isinstance(part.slice.value, str):
                    required.add(part.slice.value)
            if not isinstance(part, ast.Call):
                continue
            if isinstance(part.func, ast.Name) and part.func.id in {"_s", "_i", "_f", "_list", "_tribool"} and len(part.args) > 1:
                if isinstance(part.args[1], ast.Constant):
                    optional.add(part.args[1].value)
            if isinstance(part.func, ast.Attribute):
                if part.func.attr == "get" and isinstance(part.func.value, ast.Name) and part.func.value.id in {"q", "b", "params", "body"} and part.args:
                    if isinstance(part.args[0], ast.Constant):
                        optional.add(part.args[0].value)
                if isinstance(part.func.value, ast.Name) and part.func.value.id == "self" and part.func.attr in methods and part.func.attr not in seen:
                    extra, needed = reads(methods[part.func.attr], seen | {part.func.attr})
                    optional.update(extra)
                    required.update(needed)
        return optional - required, required

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"g", "p"} and len(node.args) == 2 and isinstance(node.args[0], ast.Constant):
            method = "GET" if node.func.id == "g" else "POST"
            optional, required = reads(node.args[1], set())
            routes[(method, node.args[0].value)] = {"optional": sorted(optional), "parameters": sorted(optional | required),
                                                  "source": f"pseudolife_memory/web/routes.py:{node.lineno}"}
    api = ast.parse((root / "pseudolife_memory/web/api.py").read_text(encoding="utf-8-sig"))
    for node in ast.walk(api):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if not isinstance(test.left, ast.Name) or test.left.id != "path" or len(test.comparators) != 1:
            continue
        target = test.comparators[0]
        if not isinstance(target, ast.Constant) or not isinstance(target.value, str) or not target.value.startswith("/api/hook/"):
            continue
        optional = set()
        method = None
        for part in ast.walk(node):
            if isinstance(part, ast.Compare) and isinstance(part.left, ast.Name) and part.left.id == "method" and isinstance(part.comparators[0], ast.Constant):
                method = part.comparators[0].value
            if isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute) and part.func.attr == "get" and isinstance(part.func.value, ast.Name) and part.func.value.id in {"params", "body"} and part.args and isinstance(part.args[0], ast.Constant):
                optional.add(part.args[0].value)
        if method:
            routes[(method, target.value)] = {"optional": sorted(optional), "parameters": sorted(optional), "source": f"pseudolife_memory/web/api.py:{node.lineno}"}
    return tools, routes


def inventory_surface(root: Path, inventory: dict) -> list[dict]:
    tools, routes = parameter_contracts(root)
    surface = []
    for item in inventory["tools"]:
        contract = tools.get(item["name"], {})
        surface.append({"id": "tool:" + item["name"], "kind": "tool", "name": item["name"],
                        "parameters": contract.get("parameters"), "optional_parameters": contract.get("optional"),
                        "parameter_source": contract.get("source")})
    for kind, key in (("route", "console_routes"), ("route", "hook_endpoints")):
        for item in inventory[key]:
            contract = routes.get((item["method"], item["path"]), {})
            surface.append({"id": f"route:{item['method']} {item['path']}", "kind": kind, "name": item["path"], "method": item["method"],
                            "parameters": contract.get("parameters"), "optional_parameters": contract.get("optional"),
                            "parameter_source": contract.get("source")})
    for kind, key in (("environment", "environment_variables"), ("cli", "cli_modes"), ("coordination", "coordination_actions")):
        for name in inventory[key]:
            surface.append({"id": f"{kind}:{name}", "kind": kind, "name": name, "optional_parameters": None})
    coordination = ast.parse((root / "pseudolife_memory/coordination.py").read_text(encoding="utf-8-sig"))
    constants = {n.targets[0].id: (ast.literal_eval(n.value), n.lineno) for n in coordination.body
                 if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id in {"_PARAMETERS", "_REQUIRED"}}
    for item in surface:
        if item["kind"] == "coordination":
            params = constants["_PARAMETERS"][0].get(item["name"], set())
            required = constants["_REQUIRED"][0].get(item["name"], set())
            item["parameters"] = sorted(params)
            item["optional_parameters"] = sorted(params - required)
            item["parameter_source"] = f"pseudolife_memory/coordination.py:{constants['_PARAMETERS'][1]}"
    for section, fields in inventory["config_sections"].items():
        surface.append({"id": "config:" + section, "kind": "config", "name": section, "optional_parameters": fields})
        for field in fields:
            surface.append({"id": f"config:{section}.{field}", "kind": "config", "name": f"{section}.{field}", "optional_parameters": []})
    return sorted(surface, key=lambda item: item["id"])


def match_surface(surface: list[dict], calls: list[dict]) -> tuple[list[dict], list[dict]]:
    lookup = defaultdict(list)
    for item in surface:
        item["producers"] = []
        lookup[(item["kind"], item["name"])].append(item)
    gaps = []
    for call in calls:
        candidates = lookup.get((call["kind"], call["name"]), [])
        candidates = [item for item in candidates if item["kind"] != "route" or call.get("method") in {None, item.get("method")}]
        if not candidates:
            gaps.append(call)
            continue
        for item in candidates:
            producer = {key: value for key, value in call.items() if key not in {"kind", "name", "method"}}
            if call.get("kind") == "route" and call.get("method") is None:
                producer["method"] = None
            producer["shape"] = dict(call["shape"])
            contract = item.get("optional_parameters")
            supplied = set(producer["shape"]["parameters"])
            positional = producer["shape"].get("positional", [])
            if positional and item.get("parameters"):
                supplied.update(item["parameters"][:len(positional)])
            producer["shape"]["omitted_optional"] = (sorted(set(contract) - supplied)
                if contract is not None and call["shape"]["complete"] and call.get("method", "known") is not None else None)
            item["producers"].append(producer)
    for item in surface:
        item["producers"] = unique(item["producers"])
    return surface, unique(gaps)


def unique(items: list[dict]) -> list[dict]:
    return [json.loads(value) for value in sorted({json.dumps(item, sort_keys=True, ensure_ascii=False) for item in items})]


def unresolved_patterns(text: str, path: str) -> list[dict]:
    result = []
    for line, content in enumerate(text.splitlines(), 1):
        if content.lstrip().startswith(("#", "//")):
            continue
        reasons = []
        if re.search(r"\*\*\w+|\.\.\.\w+", content) and re.search(r"call|request|body|params|post|memory_", content, re.I):
            reasons.append("forwarded parameters")
        if "/api/" in content and re.search(r"\$|\{\w+\}|format!|\+", content):
            reasons.append("built URL")
        if re.search(r"curl\s|Invoke-WebRequest|Invoke-RestMethod|fetch\(", content) and "/api/" not in content:
            reasons.append("request target supplied dynamically or external")
        if re.search(r"(?:run_shim|Run-Shim|\$shim\b|\$SHIM(?:_PATH)?\b|\$cmd\b|\$command\b).*?(?:--|\$args|\$Args|\"[a-z-]+\")", content):
            reasons.append("shell-built command")
        if re.search(r"PSEUDOLIFE_[A-Z_]+_[\"']|prefix\s*\+\s*key", content):
            reasons.append("environment key family construction")
        if path.endswith(".rs") and re.search(r"\brequest\(|\.post_retry\(|\.post\(", content) and "action" in content:
            reasons.append("forwarded request action/body")
        if reasons:
            result.append({"location": f"{path}:{line}", "dynamic": "unresolved", "reason": ", ".join(reasons)})
    return result


def description_calls(text: str) -> list[dict]:
    """Only registered tool docstrings are shipped descriptions, not private helpers."""
    tree = ast.parse(text)
    manually_registered = set(re.findall(r'_TOOL_TIERS\["([^"]+)"\]', text))
    calls = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        registered = node.name in manually_registered or any(
            isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "_tool"
            for dec in node.decorator_list)
        if registered and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            doc = node.body[0].value
            for call in tool_calls(doc.value, "description.md"):
                call["location"] = f"pseudolife_memory/mcp_server.py:{doc.lineno + int(call['location'].rsplit(':', 1)[1]) - 1}"
                call["evidence"] = "description-example"
                calls.append(call)
    return calls


def snapshot(root: Path = ROOT) -> dict:
    inventory = json.loads((root / INVENTORY).read_text(encoding="utf-8"))
    paths = producer_paths(root)
    calls = scan_files(root, paths)
    known_tools = {item["name"] for item in inventory["tools"]}
    # Hooks/commands also tell the model to use tools without supplying an argv shape.
    # These are explicitly unverified instruction references, never literal call cases.
    for path in paths:
        if not path.startswith("plugin/"):
            continue
        text = (root / path).read_text(encoding="utf-8-sig")
        observed = {(call["name"], call["location"]) for call in calls if call["kind"] == "tool"}
        for line, content in enumerate(text.splitlines(), 1):
            if content.lstrip().startswith(("#", "//")) and not path.endswith(".md"):
                continue
            for name in sorted(set(re.findall(r"\bmemory_[a-z_]+\b", content)) & known_tools):
                if (name, f"{path}:{line}") in observed:
                    continue
                call = record("tool", name, path, line, complete=False,
                              expression="Shipped instruction reference supplies no confirmed parameter shape.")
                call["evidence"] = "unverified"
                call["usage"] = "instruction-reference"
                calls.append(call)
    aliases = config_aliases(root)
    for path in paths:
        if not path.endswith(".js"):
            calls.extend(config_calls((root / path).read_text(encoding="utf-8-sig"), path, aliases))
    for call in list(calls):
        if call["kind"] == "route" and call["name"].startswith("/api/coordination/") and "{" not in call["name"]:
            action = call["name"].removeprefix("/api/coordination/")
            calls.append({**call, "kind": "coordination", "name": action})
    # Tool docstrings contain copyable examples, not function implementations.
    mcp_text = (root / "pseudolife_memory/mcp_server.py").read_text(encoding="utf-8-sig")
    calls.extend(description_calls(mcp_text))
    calls.extend(forwarded_coordination(calls))
    manual = manual_calls(root)
    resolved_locations = {(call["kind"], call["name"], call["location"]) for call in manual}
    calls = [call for call in calls if (call["kind"], call["name"], call["location"]) not in resolved_locations]
    calls.extend(manual)
    for call in list(calls):
        if call["kind"] == "config" and "." in call["name"]:
            section, field = call["name"].split(".", 1)
            if section in inventory["config_sections"]:
                parent = {**call, "name": section, "shape": {"channel": "config", "complete": False,
                    "parameters": {field: call["shape"]["parameters"].get("value", {"expression": "field reference"})}}}
                parent["dynamic"] = "unresolved"
                calls.append(parent)
    surface, gaps = match_surface(inventory_surface(root, inventory), calls)
    drift = manual_drift(root)
    unresolved = list(drift)
    for path in paths:
        text = (root / path).read_text(encoding="utf-8-sig")
        if path.endswith(".rs"):
            text = re.split(r"#\[cfg\(test\)\]", text)[0]
        unresolved.extend(unresolved_patterns(text, path))
    for item in surface:
        for producer in item["producers"]:
            if producer.get("dynamic") == "unresolved":
                unresolved.append({"location": producer["location"], "surface": item["id"], "dynamic": "unresolved",
                                   "reason": "Producer parameters, method or config/environment use is not fully resolved."})
    for call in gaps:
        if call.get("dynamic") == "unresolved":
            unresolved.append({"location": call["location"], "surface": f"inventory-gap:{call['kind']}:{call['name']}",
                               "dynamic": "unresolved", "reason": "Unknown or forwarded inventory target/shape."})
    for entry in unresolved:
        resolutions = [call for call in manual if call["location"] == entry["location"]]
        if resolutions:
            entry["resolved_by"] = [f"{call['kind']}:{call.get('method', '')} {call['name']}".strip() for call in resolutions]
    hashes = input_hashes(root, sorted(set(paths) | set(CONTRACT_SOURCES)))
    return {"schema": 1, "inventory": INVENTORY, "oracle_commit": inventory["oracle_commit"],
            "input_sha256": hashes, "scopes": list(SCOPES), "manual_drift": drift,
            "surface": surface, "inventory_gaps": gaps,
            "no_observed_producer": [item["id"] for item in surface if not item["producers"]],
            "unresolved": unique(unresolved),
            "limitations": ["No observed producer is a provisional defer candidate, not proof of no caller.",
                            "Unknown method, forwarded parameters and constructed URLs retain unresolved evidence; omitted_optional is null when not established.",
                            "Environment references are a conservative superset, not proof of writes."]}


def ci_currency_required(root: Path = ROOT, env: dict | None = None) -> bool:
    """Gate currency for census/inventory changes; other input drift is a report."""
    env = os.environ if env is None else env
    event_path = env.get("GITHUB_EVENT_PATH")
    if not event_path:
        return False
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    base = event.get("pull_request", {}).get("base", {}).get("sha") or event.get("before")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base) or not base.strip("0"):
        return False  # A manual dispatch has no changed-file range; still report currency.
    try:
        changed = subprocess.check_output(["git", "diff", "--name-only", "-z", base, "HEAD"],
                                          cwd=root, stderr=subprocess.PIPE).decode("utf-8").split("\0")
    except subprocess.CalledProcessError:
        print("currency range unavailable (for example, a force-push); reporting drift only")
        return False
    return bool(CURRENCY_INPUTS.intersection(changed))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed census is stale")
    parser.add_argument("--ci-check", action="store_true", help="check currency; gate only census/inventory changes from the GitHub event")
    args = parser.parse_args()
    result = snapshot()
    if args.check or args.ci_check:
        if not OUTPUT.exists() or json.loads(OUTPUT.read_text(encoding="utf-8")) != result:
            if args.check or ci_currency_required():
                parser.exit(1, "producer census is stale; run python rust/producer_census.py\n")
            print("producer census is stale (report only); regenerate before using it in a port slice")
    else:
        OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"surfaces": len(result["surface"]), "gaps": len(result["inventory_gaps"]),
                      "unreached": len(result["no_observed_producer"]), "unresolved": len(result["unresolved"])}))


if __name__ == "__main__":
    main()
