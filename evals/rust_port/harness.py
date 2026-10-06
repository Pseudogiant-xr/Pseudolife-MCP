"""Record/replay CLI bytes and HTTP/MCP responses without weakening order."""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import fnmatch
import json
import math
import os
import platform
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from evals.rust_port.processes import owned_process

# Deliberate application and MCP SDK headers. Content-Length is retained when
# both peers use fixed framing; a fixed/chunked pair is compared by body instead.
HTTP_HEADER_ALLOWLIST = ("content-type", "content-length", "cache-control", "location",
    "www-authenticate", "allow", "retry-after", "mcp-protocol-version", "mcp-session-id", "x-pl-board")
NORMALIZATION_RULES = ("source-text-lf", "session-id-bijection", "epoch-type-unit-magnitude",
                       "raw-mcp-retained-not-compared", "content-length-fixed-framing", "content-length-authorized-wire-spans")


class DuplicateJSONKey(ValueError):
    """A JSON object repeated a member name before parsing could discard it."""


def observe_boundary(operation, client=None):
    try:
        return operation()
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        result = {"boundary_error": type(error).__name__}
        raw = getattr(client, "last_wire_body", None)
        if raw is not None:
            result["boundary_raw_b64"] = base64.b64encode(raw).decode("ascii")
        return result


def strict_json_loads(raw):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateJSONKey()
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=object_pairs)


def capture_platform():
    return {"os": platform.system(), "architecture": platform.machine()}


def require_capture_platform(receipt):
    captured = receipt.get("capture_platform")
    if not isinstance(captured, dict) or captured != capture_platform():
        raise ValueError("capture platform missing or differs from replay platform")
    return captured


@dataclass(frozen=True)
class Policy:
    # Tolerance is opt-in per score field, never global to numeric fields.
    abs_tol: float = 1e-6
    rel_tol: float = 0.0
    score_paths: tuple[str, ...] = ()
    ignored_values: tuple[str, ...] = ("/raw_mcp_body_b64", "/content_length/raw", "/content_length/adjustment")
    json_text_paths: tuple[str, ...] = ()
    ranking_paths: tuple[str, ...] = ()
    ranking_key: str = "id"
    source_text_paths: tuple[str, ...] = ("/body/result/tools/*/description",)

    def __post_init__(self):
        for value in (self.abs_tol, self.rel_tol):
            if not math.isfinite(value) or value < 0:
                raise ValueError("tolerance must be finite and nonnegative")


def _matches(path: str, patterns) -> bool:
    # A wildcard occupies ONE path segment; it cannot swallow nested fields.
    parts = path.split("/")
    return any(len(parts) == len(p.split("/")) and all(
        fnmatch.fnmatchcase(a, b) for a, b in zip(parts, p.split("/")))
        for p in patterns)


def _segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def compare(expected: Any, actual: Any, policy: Policy, path="") -> list[dict]:
    """Return safe path/reason diagnostics, never response values or credentials."""
    def error(reason):
        return [{"path": path or "/", "reason": reason}]

    if not path and isinstance(expected, dict) and isinstance(actual, dict) and \
            {expected.get("framing"), actual.get("framing")} == {"fixed", "chunked"}:
        expected, actual = dict(expected), dict(actual)
        for value in (expected, actual):
            value["headers"] = {k: v for k, v in value["headers"].items() if k != "content-length"}
            value.pop("content_length", None)
            value["framing"] = "<fixed-or-chunked>"

    if isinstance(actual, dict) and "boundary_error" in actual:
        return error("duplicate_json_key" if actual["boundary_error"] == "DuplicateJSONKey" else "boundary_error")
    if path.endswith("/isError") and expected is True and actual is False:
        return error("tool_error_envelope")
    if _matches(path, policy.source_text_paths) and isinstance(expected, str) and isinstance(actual, str):
        expected, actual = (v.replace("\r\n", "\n").replace("\r", "\n") for v in (expected, actual))
    if isinstance(expected, str) and isinstance(actual, str) and expected != actual and \
            ":epoch-unit=" in expected and ":epoch-unit=" in actual:
        if expected.split(":epoch-unit=")[0].rsplit(":", 1)[-1] != actual.split(":epoch-unit=")[0].rsplit(":", 1)[-1]:
            return error("type")
        if expected.split(":epoch-unit=")[1].split(":")[0] != actual.split(":epoch-unit=")[1].split(":")[0]:
            return error("epoch_unit")
        if expected.split(":magnitude=")[-1] != actual.split(":magnitude=")[-1]:
            return error("epoch_magnitude")

    if any(type(value) is float and not math.isfinite(value) for value in (expected, actual)):
        return error("nonfinite_number")
    if _matches(path, policy.ignored_values):
        # Presence is checked by the parent; ignoring never deletes a field.
        if isinstance(expected, (dict, list)) or isinstance(actual, (dict, list)):
            return error("ignored_field_not_scalar")
        return [] if type(expected) is type(actual) else error("ignored_field_type")
    if _matches(path, policy.json_text_paths):
        if not isinstance(expected, str) or not isinstance(actual, str):
            return error("json_text_type")
        try:
            expected, actual = strict_json_loads(expected), strict_json_loads(actual)
        except DuplicateJSONKey:
            return error("duplicate_json_key")
        except (ValueError, TypeError):
            return error("invalid_embedded_json")
    numeric = lambda v: type(v) in (int, float)
    if _matches(path, policy.score_paths):
        if not numeric(expected) or not numeric(actual):
            return error("score_type")
        if not math.isfinite(expected) or not math.isfinite(actual):
            return error("nonfinite_score")
        return [] if math.isclose(expected, actual, abs_tol=policy.abs_tol,
                                  rel_tol=policy.rel_tol) else error("score_tolerance")
    if type(expected) is not type(actual):
        return error("type")
    if isinstance(expected, dict):
        out = []
        for key in sorted(expected.keys() | actual.keys()):
            child = path + "/" + _segment(key)
            if key not in expected or key not in actual:
                out.append({"path": child, "reason": "field_presence"})
            else:
                out.extend(compare(expected[key], actual[key], policy, child))
        return out
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return error("list_length")
        out = []
        if _matches(path, policy.ranking_paths):
            key = policy.ranking_key
            if any(not isinstance(v, dict) or key not in v for v in expected + actual):
                out.extend(error("ranking_identity_missing"))
            elif [v[key] for v in expected] != [v[key] for v in actual]:
                out.extend(error("ranking_order"))
        for i, (a, b) in enumerate(zip(expected, actual)):
            out.extend(compare(a, b, policy, path + f"/{i}"))
        return out
    if isinstance(expected, float) and (not math.isfinite(expected) or not math.isfinite(actual)):
        return error("nonfinite_number")
    return [] if expected == actual else error("value")


def isolated_env(home: Path) -> dict[str, str]:
    """Allow only process runtime variables; fixture home replaces all settings."""
    home.mkdir(parents=True, exist_ok=True)
    names = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "USERNAME": "fixture", "USER": "fixture", "LOGNAME": "fixture",
                "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "local"),
                "XDG_CONFIG_HOME": str(home / "config"), "CODEX_HOME": str(home / "codex"),
                "TMP": str(home), "TEMP": str(home), "TMPDIR": str(home),
                "PSEUDOLIFE_MCP_DATA_DIR": str(home / "bank"),
                "PSEUDOLIFE_MCP_CONFIG": str(home / "fixture.yaml"),
                "PSEUDOLIFE_MCP_STORAGE": "files", "CUDA_VISIBLE_DEVICES": "-1",
                "HF_HUB_OFFLINE": "1", "HF_HOME": str(home / "huggingface"),
                "TORCH_HOME": str(home / "torch"), "TORCHINDUCTOR_CACHE_DIR": str(home / "inductor"),
                "PYTHONIOENCODING": "utf-8",
                "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    return env


class AbnormalTermination(OSError):
    """The CLI did not terminate with a portable ordinary exit code."""


def _ordinary_cli_exit(value):
    # Keep ordinary CLI errors (including argparse/pytest errors), while
    # excluding POSIX signals and Windows exception/NTSTATUS termination.
    return type(value) is int and 0 <= value <= 255


def run_cli(prefix, argv, *, cwd: Path, env: dict, timeout: float) -> dict:
    with owned_process([*prefix, *argv], cwd=cwd, env=env, stdin=subprocess.PIPE) as proc:
        stdout, stderr = proc.communicate(input=b"", timeout=timeout)
    if not _ordinary_cli_exit(proc.returncode):
        raise AbnormalTermination()
    return {"exit_code": proc.returncode,
            "stdout_b64": base64.b64encode(stdout).decode("ascii"),
            "stderr_b64": base64.b64encode(stderr).decode("ascii")}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _base_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("target must be a disposable loopback HTTP origin")
    return url.rstrip("/")


class HttpClient:
    """An independent session per record/replay pass, with no proxy or redirects."""
    def __init__(self, base_url, timeout=10, *, retain_wire=False):
        self.base_url = _base_url(base_url)
        self.timeout = timeout
        self.session = None
        self.protocol = None
        self.retain_wire = retain_wire
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def execute(self, request: dict, surface: str, *, runtime_headers=None) -> dict:
        self.last_wire_body = None
        path = request["path"]
        if not path.startswith("/") or path.startswith("//") or urllib.parse.urlsplit(path).netloc:
            raise ValueError("request path must be relative to the disposable origin")
        headers = dict(request.get("headers", {}))
        if any(k.lower() in {"authorization", "cookie", "proxy-authorization", "mcp-session-id", "x-pl-agent-key"}
               for k in headers):
            raise ValueError("credential and session headers cannot be recorded")
        # Runtime credentials belong to the private caller vault, never a case.
        headers.update(runtime_headers or {})
        if surface == "mcp":
            headers.update({"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
            if self.session:
                headers["Mcp-Session-Id"] = self.session
            if self.protocol:
                headers["MCP-Protocol-Version"] = self.protocol
        data = (json.dumps(request["body"], allow_nan=False).encode("utf-8")
                if "body" in request else None)
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=headers, method=request.get("method", "GET"))
        try:
            response = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            self.last_wire_body = raw
            ctype = response.headers.get("Content-Type", "")
            if surface == "mcp":
                observed_session = response.headers.get("Mcp-Session-Id", self.session)
                if self.session is not None and observed_session != self.session:
                    raise ValueError("MCP session identity changed")
                self.session = observed_session
            selected = {key: response.headers[key] for key in HTTP_HEADER_ALLOWLIST
                        if key in response.headers}
            if "mcp-session-id" in selected:
                selected["mcp-session-id"] = "<mcp-session>"
            if "application/json" in ctype and raw:
                body = strict_json_loads(raw)
            elif "text/event-stream" in ctype:
                # Parse events in arrival order, retaining every data envelope.
                events, chunks = [], []
                for line in raw.decode("utf-8").replace("\r\n", "\n").split("\n"):
                    if line.startswith("data:"):
                        chunks.append(line[5:].lstrip(" "))
                    elif not line and chunks:
                        events.append(strict_json_loads("\n".join(chunks)))
                        chunks = []
                if chunks:
                    events.append(strict_json_loads("\n".join(chunks)))
                body = events[0] if len(events) == 1 else events
            elif not raw:
                body = None
            else:
                body = {"bytes_b64": base64.b64encode(raw).decode("ascii")}
            if surface == "mcp" and isinstance(body, dict):
                result = body.get("result", {})
                if isinstance(result, dict) and "protocolVersion" in result:
                    self.protocol = result["protocolVersion"]
            framing = ("chunked" if "chunked" in response.headers.get("Transfer-Encoding", "").lower()
                       else "fixed" if "Content-Length" in response.headers else "connection-close")
            observed = {"status": response.status, "headers": selected, "body": body, "framing": framing}
            if self.retain_wire:
                observed["_wire_body"] = raw
            if surface == "mcp":
                observed["raw_mcp_body_b64"] = base64.b64encode(raw).decode("ascii")
            return observed


def execute(cases, *, cli_prefix, base_url, cwd, home, timeout=10):
    env = isolated_env(home)
    client = HttpClient(base_url, timeout) if base_url else None
    records = []
    for case in cases:
        surface, request = case["surface"], case["request"]
        def operation():
            if surface == "cli":
                return run_cli(cli_prefix, request["argv"], cwd=cwd, env=env, timeout=timeout)
            elif surface in {"http", "mcp"} and client:
                return client.execute(request, surface)
            else:
                raise ValueError("surface requires an explicit configured target")
        response = observe_boundary(operation, client if surface in {"http", "mcp"} else None)
        records.append({**case, "response": response})
    return records


def replay(transcript, *, cli_prefix, base_url, cwd, home, timeout=10):
    records = transcript["records"]
    if not records or any("boundary_error" in r["response"] or (
            r["surface"] == "cli" and not _ordinary_cli_exit(r["response"].get("exit_code")))
            for r in records):
        raise ValueError("oracle transcript must contain successful boundary observations")
    require_capture_platform(transcript)
    actual = execute([{k: v for k, v in r.items() if k != "response"} for r in records],
                     cli_prefix=cli_prefix, base_url=base_url, cwd=cwd, home=home, timeout=timeout)
    differences = []
    for oracle, candidate in zip(records, actual):
        policy = Policy(**oracle.get("policy", {}))
        for item in compare(oracle["response"], candidate["response"], policy):
            differences.append({"case": oracle["id"], **item})
    return {"schema": 1, "capture_platform": capture_platform(), "cases": len(records), "differences": differences,
            "passed": not differences}


def write_new(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "replay"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cli-json", required=True, help="JSON array containing executable and fixed arguments")
    parser.add_argument("--base-url")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    prefix = json.loads(args.cli_json)
    if not isinstance(prefix, list) or not prefix or not all(isinstance(s, str) for s in prefix):
        parser.error("--cli-json must be a nonempty string array")
    with tempfile.TemporaryDirectory(prefix="rust-port-") as directory:
        options = dict(cli_prefix=prefix, base_url=args.base_url,
                       cwd=Path.cwd(), home=Path(directory), timeout=args.timeout)
        if args.mode == "record":
            result = {"schema": 1, "fixture": payload.get("fixture"),
                      "capture_platform": capture_platform(),
                      "records": execute(payload["cases"], **options)}
            result["passed"] = not any("boundary_error" in r["response"] for r in result["records"])
        else:
            result = replay(payload, **options)
        write_new(args.out, result)
    print(json.dumps({"mode": args.mode, "passed": result.get("passed"),
                      "cases": len(result.get("records", [])) or result.get("cases")}))
    return 1 if result.get("passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
