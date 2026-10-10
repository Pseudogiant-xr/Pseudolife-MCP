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
from typing import Any, Callable

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


_TITLE = re.compile(rb'( - )([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})(")')


def _minute_in(stamp: str, lo: float, hi: float, offset: int) -> bool:
    return any(time.strftime("%Y-%m-%d %H:%M", time.gmtime(t + offset)) == stamp
               for t in range(int(lo) - 1, int(hi) + 2))


@rule("episode-title-minute")
def episode_title_minute(obs: dict) -> None:
    """The episode title's ``YYYY-MM-DD HH:MM`` stamp (session_title.py:103-109)
    in a POSTed body: a local minute inside the arm's own window, on the
    clock of the host that ran it. The name before it stays exact."""
    lo, hi = obs["window"]
    offset = obs.get("utc_offset", 0)

    def stamp(match: re.Match) -> bytes:
        if _minute_in(match.group(2).decode(), lo, hi, offset):
            return match.group(1) + b"<minute>" + match.group(3)
        return match.group(0)
    for request in obs.get("requests", []):
        request["body"] = _TITLE.sub(stamp, request["body"].encode()).decode()


def episode_rows(rows: list[str], run_start: float, case_start: float, case_end: float,
                 offset: int, ids: dict[str, str]) -> list:
    """Episode and client-session rows from a bank, with generated values
    replaced after validation: uuid4-hex episode ids by symbols first seen in
    ``ids`` (shared across the case's tables), wall-clock seconds by
    ``<t:case>`` when written during this case and ``<t:earlier>`` when
    written by an earlier case of the run (so a refresh, or a missed one,
    still shows), and title minutes inside the run by ``<minute>``. Anything
    that fails validation stays raw."""
    import json as _json  # noqa: PLC0415

    def walk(value):
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, float):
            # Same host clock on both sides; the arm's start is read before
            # launch, so the lower bound is strict (arms run < 1 s apart).
            if case_start <= value <= case_end + 0.5:
                return "<t:case>"
            if run_start - 1 <= value < case_start:
                return "<t:earlier>"
        if isinstance(value, str):
            if re.fullmatch(r"[0-9a-f]{32}", value):
                return ids.setdefault(value, f"<episode-{len(ids) + 1}>")
            match = re.fullmatch(r"(.* - )([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})", value)
            if match and _minute_in(match.group(2), run_start, case_end, offset):
                return match.group(1) + "<minute>"
        return value
    return [walk(_json.loads(row)) for row in rows]


_LEGACY = re.compile(rb'"legacy_first_seen":([0-9]+\.[0-9]+)|"expires_at":([0-9]+\.[0-9]+)')


@rule("doorbell-legacy-first-seen")
def doorbell_legacy_first_seen(obs: dict) -> None:
    """A legacy pending record's migration stamps ``legacy_first_seen`` with
    the reader's clock and ``expires_at`` 86400 s later
    (codex_doorbell_state.py:150-158). Both values are validated (first seen
    inside the arm's window, expiry exactly first seen + 86400) before being
    tokenized; key order and every other byte stay exact."""
    start, end = obs["window"]
    for rel in list(obs["files"]):
        if not rel.endswith(".bell-pending"):
            continue
        data = _file(obs, rel) or b""
        values = {m.group(0).split(b":")[0]: m.group(1) or m.group(2)
                  for m in _LEGACY.finditer(data)}
        seen, expiry = values.get(b'"legacy_first_seen"'), values.get(b'"expires_at"')
        try:
            ok = (seen is not None and expiry is not None
                  and start - 1 <= float(seen) <= end + 1
                  and float(expiry) == float(seen) + 86400)
        except ValueError:
            ok = False
        if ok:
            data = data.replace(b'"legacy_first_seen":' + seen, b'"legacy_first_seen":<t>')
            data = data.replace(b'"expires_at":' + expiry, b'"expires_at":<t+86400>')
            _set_file(obs, rel, data)


_SHUTDOWN_FLUSH = re.compile(rb"Exception ignored in: <_io\.TextIOWrapper name='<stdout>'"
                             rb"[^\r\n]*\r?\n(?:BrokenPipeError|OSError): [^\r\n]*\r?\n\Z")


def _shutdown_flush(obs: dict, intended: int) -> None:
    stderr = _get(obs, "stderr")
    if obs["exit"] == 120 and _SHUTDOWN_FLUSH.search(stderr):
        _put(obs, "stderr", _SHUTDOWN_FLUSH.sub(b"", stderr))
        obs["exit"] = intended


@rule("python-shutdown-flush")
def python_shutdown_flush(obs: dict) -> None:
    """Declared substitution ``wait-mail-direct-stdout``: after its own exit 2
    for a stdout that cannot take the mail, CPython's interpreter shutdown
    fails to flush the same stdout, prints an ignored-exception trailer and
    turns the exit into 120. The native CLI keeps the documented exit 2 and
    emits no synthetic trailer. Only that exact trailer with exit 120 maps."""
    _shutdown_flush(obs, 2)


@rule("python-shutdown-flush-silent")
def python_shutdown_flush_silent(obs: dict) -> None:
    """Declared substitution ``hook-native-output-failure``: prompt-hook
    swallows its own write failure to stay silent with exit 0
    (briefing_cli.py:248-251), then CPython's shutdown flush prints the
    trailer and exits 120. The native hook stays silent with exit 0."""
    _shutdown_flush(obs, 0)


# A PostgreSQL SCRAM verifier (a server echoing a CREATE/ALTER ROLE statement).
# The repo is public and secret scanners flag these, so no golden keeps one:
# the recorder writes this fixed token instead, and compare-time rules that
# validate a verifier's shape map it to the same token.
SCRAM_VERIFIER = re.compile(rb"SCRAM-SHA-256\$\d+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+")
SCRAM_TOKEN = b"<scram-sha-256-verifier>"
SCRAM_MARK = b"SCRAM-SHA-256$"


def redact_scram_verifiers(obs: dict) -> None:
    """Replace every SCRAM verifier in a recorded observation's streams and
    files with ``SCRAM_TOKEN`` (the recorder runs this on every row)."""
    for field in ("stdout", "stderr"):
        if field in obs:
            _put(obs, field, SCRAM_VERIFIER.sub(SCRAM_TOKEN, _get(obs, field)))
    for rel, value in list(obs.get("files", {}).items()):
        if value.startswith("file:"):
            _set_file(obs, rel, SCRAM_VERIFIER.sub(SCRAM_TOKEN, base64.b64decode(value[5:])))


def scram_marks(node: Any, where: str = "") -> list[str]:
    """Where ``SCRAM-SHA-256$`` appears in a golden, raw or inside any base64
    string (streams, ``file:`` contents), so a writer can refuse it."""
    if isinstance(node, dict):
        return [hit for key, value in node.items() for hit in scram_marks(value, f"{where}/{key}")]
    if isinstance(node, list):
        return [hit for index, value in enumerate(node)
                for hit in scram_marks(value, f"{where}[{index}]")]
    if not isinstance(node, str):
        return []
    if SCRAM_MARK in node.encode():
        return [where]
    text = node[5:] if node.startswith("file:") else node
    try:
        decoded = base64.b64decode(text, validate=True)
    except ValueError:
        return []
    return [where] if SCRAM_MARK in decoded else []


@rule("python-stdout-closed-trailer")
def python_stdout_closed_trailer(obs: dict) -> None:
    """Declared substitution: a run whose stdout refused its buffered prints
    completes, then CPython's interpreter-shutdown flush fails, prints an
    ignored-exception trailer and exits 120. The native CLI exits 120 with no
    synthetic trailer. Only that exact final trailer, with exit 120, is
    removed; the exit stays 120, so a candidate that returns its ordinary
    code still differs."""
    stderr = _get(obs, "stderr")
    if obs["exit"] == 120 and _SHUTDOWN_FLUSH.search(stderr):
        _put(obs, "stderr", _SHUTDOWN_FLUSH.sub(b"", stderr))


def home_tokens(obs: dict, home: str) -> None:
    """Replace the disposable home path with {HOME} everywhere, so goldens
    recorded under one home compare against a run under another."""
    from urllib.parse import quote  # noqa: PLC0415
    # Raw, JSON-escaped and URL-quoted (a launcher query carries the path).
    forms = {home.encode(), json.dumps(home)[1:-1].encode(), quote(home, safe="").encode()}

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
    for request in obs.get("requests", []):
        request["target"] = swap(request["target"].encode()).decode()
        request["body"] = swap(request["body"].encode()).decode()


# Request header fields left out of the wire comparison. Empty: every field
# is compared by case-insensitive name, so a field one arm adds or drops
# (Accept, Origin, Cookie, Referer) is a difference. A repeated field name is
# always a difference. A row whose transport has a recorded header
# disposition names it here, with that disposition.
FREE_HEADERS: tuple[str, ...] = ()


def wire(obs: dict) -> None:
    projected = []
    for request in obs.get("requests", []):
        if isinstance(request["headers"], dict):  # already projected (a golden)
            projected.append(request)
            continue
        names = [k.lower() for k, _ in request["headers"]]
        fields = {k.lower(): v for k, v in request["headers"] if k.lower() not in FREE_HEADERS}
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
    port = host.rsplit(":", 1)[1]
    # Longest first: the URL, then the host:port spellings a case may use.
    forms = (url.encode(), f"http://localhost:{port}".encode(),
             f"https://localhost:{port}".encode(), host.encode(),
             f"localhost:{port}".encode())

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
