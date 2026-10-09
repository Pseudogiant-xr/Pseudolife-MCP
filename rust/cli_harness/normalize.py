"""Named comparison rules. Exact is the default; a case opts into a rule by
name, and a rule rewrites only the span it names after validating it. A rule
that cannot validate its span leaves it untouched, so the diff still shows.

Every rule here is recorded in rust/PARITY.md beside the row that uses it.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Callable

Rule = Callable[[dict], None]
RULES: dict[str, Rule] = {}


def rule(name: str):
    def register(func: Rule) -> Rule:
        RULES[name] = func
        return func
    return register


def _get(obs: dict, field: str) -> bytes:
    return base64.b64decode(obs[field])


def _put(obs: dict, field: str, value: bytes) -> None:
    obs[field] = base64.b64encode(value).decode()


def _file(obs: dict, rel: str) -> bytes | None:
    value = obs["files"].get(rel)
    if value is None or not value.startswith("file:"):
        return None
    return base64.b64decode(value[5:])


def _set_file(obs: dict, rel: str, value: bytes) -> None:
    obs["files"][rel] = "file:" + base64.b64encode(value).decode()


def _local_seconds_in_window(clock: bytes, window: list[float], offset: int) -> bool:
    """HH:MM:SS names a second inside [start, end] (whole seconds) on the
    clock of the host that ran the arm (``offset`` seconds east of UTC)."""
    start, end = int(window[0]), int(window[1]) + 1
    wanted = clock.decode()
    return any(time.strftime("%H:%M:%S", time.gmtime(t + offset)) == wanted
               for t in range(start, end + 1))


_RANG = re.compile(rb"(wait-mail: the daemon rang for addressed mail at )([0-9]{2}:[0-9]{2}:"
                   rb"[0-9]{2})( \([^\r\n]*, watermark [0-9]+, )([0-9]+)( s after arming\):)")


@rule("mail-clock")
def mail_clock(obs: dict) -> None:
    """wait-mail delivery: the stderr wall clock and elapsed seconds, and the
    ledger's epoch column, each validated against the arm's own window."""
    window = obs["window"]
    offset = obs.get("utc_offset", 0)
    stderr = _get(obs, "stderr")

    def clock(match: re.Match) -> bytes:
        if not _local_seconds_in_window(match.group(2), window, offset):
            return match.group(0)
        if int(match.group(4)) > int(window[1] - window[0]) + 1:
            return match.group(0)
        return match.group(1) + b"<clock>" + match.group(3) + b"<elapsed>" + match.group(5)

    _put(obs, "stderr", _RANG.sub(clock, stderr))
    for rel in list(obs["files"]):
        if not rel.endswith("ledger.log"):
            continue
        lines = []
        for line in (_file(obs, rel) or b"").split(b"\n"):
            head, sep, rest = line.partition(b"\t")
            if sep and head.isdigit() and int(window[0]) <= int(head) <= int(window[1]) + 1:
                line = b"<epoch>\t" + rest
            lines.append(line)
        _set_file(obs, rel, b"\n".join(lines))


_WRITE_FAILED = re.compile(rb"(wait-mail: could not write the mail to stdout \()[^\r\n]*(\); "
                           rb"left it unshown\.)")


@rule("mail-stdout-error-text")
def mail_stdout_error_text(obs: dict) -> None:
    """The OS error inside the closed-stdout diagnostic: its class and the
    exit code stay exact, the interpreter's error wording does not."""
    _put(obs, "stderr", _WRITE_FAILED.sub(rb"\1<os-error>\2", _get(obs, "stderr")))


_SHUTDOWN_FLUSH = re.compile(rb"Exception ignored in: <_io\.TextIOWrapper name='<stdout>'"
                             rb"[^\r\n]*\r?\n(?:BrokenPipeError|OSError): [^\r\n]*\r?\n\Z")


@rule("python-shutdown-flush")
def python_shutdown_flush(obs: dict) -> None:
    """Declared substitution ``wait-mail-direct-stdout``: after its own exit 2
    for a stdout that cannot take the mail, CPython's interpreter shutdown
    fails to flush the same stdout, prints an ignored-exception trailer and
    turns the exit into 120. The native CLI keeps the documented exit 2 and
    emits no synthetic trailer. Only that exact trailer with exit 120 maps."""
    stderr = _get(obs, "stderr")
    if obs["exit"] == 120 and _SHUTDOWN_FLUSH.search(stderr):
        _put(obs, "stderr", _SHUTDOWN_FLUSH.sub(b"", stderr))
        obs["exit"] = 2


def home_tokens(obs: dict, home: str) -> None:
    """Replace the disposable home path with {HOME} everywhere, so goldens
    recorded under one home compare against a run under another."""
    forms = {home.encode(), json.dumps(home)[1:-1].encode()}

    def swap(data: bytes) -> bytes:
        for form in sorted(forms, key=len, reverse=True):
            data = data.replace(form, b"{HOME}")
        return data

    for field in ("stdout", "stderr"):
        _put(obs, field, swap(_get(obs, field)))
    for rel, value in list(obs["files"].items()):
        if value.startswith("file:"):
            _set_file(obs, rel, swap(base64.b64decode(value[5:])))
        elif value.startswith("link:"):
            obs["files"][rel] = "link:" + swap(value[5:].encode()).decode()


# Request header fields compared on the wire. Accept, Accept-Encoding and
# Connection are transport choices with recorded dispositions in PORTING.md
# (urllib's identity/close defaults dropped, native Accept */*); field names
# associate case-insensitively. A repeated field name is always a difference.
WIRE_HEADERS = ("authorization", "user-agent", "host", "content-type", "content-length",
                "x-pl-agent", "x-pl-agent-key", "x-pl-session", "x-pl-writer", "x-pl-principal")


def wire(obs: dict) -> None:
    projected = []
    for request in obs.get("requests", []):
        if isinstance(request["headers"], dict):  # already projected (a golden)
            projected.append(request)
            continue
        names = [k.lower() for k, _ in request["headers"]]
        fields = {k.lower(): v for k, v in request["headers"] if k.lower() in WIRE_HEADERS}
        repeated = sorted({n for n in names if names.count(n) > 1})
        entry = {"method": request["method"], "target": request["target"],
                 "headers": dict(sorted(fields.items())), "body": request["body"]}
        if repeated:
            entry["repeated_fields"] = repeated
        projected.append(entry)
    obs["requests"] = projected


def daemon_tokens(obs: dict, url: str) -> None:
    """The fixture daemon's per-arm port is not the CLI's behaviour."""
    host = url.split("://", 1)[1]
    forms = (url.encode(), host.encode())

    def swap(data: bytes) -> bytes:
        for form in forms:
            data = data.replace(form, b"{DAEMON}")
        return data

    for field in ("stdout", "stderr"):
        _put(obs, field, swap(_get(obs, field)))
    for rel, value in list(obs["files"].items()):
        if value.startswith("file:"):
            _set_file(obs, rel, swap(base64.b64decode(value[5:])))
    for request in obs.get("requests", []):
        if isinstance(request["headers"], dict):
            continue
        request["headers"] = [[k, swap(v.encode()).decode()] for k, v in request["headers"]]
        request["target"] = swap(request["target"].encode()).decode()
        request["body"] = swap(request["body"].encode()).decode()


def apply(obs: dict, rules: tuple[str, ...], home: str | None) -> dict:
    out = json.loads(json.dumps(obs))
    out.setdefault("files", {})
    if home:
        home_tokens(out, home)
    if out.get("daemon_url"):
        daemon_tokens(out, out["daemon_url"])
    if "requests" in out:
        wire(out)
    for name in rules:
        RULES[name](out)
    return out
