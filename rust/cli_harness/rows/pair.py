"""CLI-PAIRING (pair): ``pseudolife-mcp pair`` (pair_cli.py).

Canonical argv (the producers: ``ops/install.sh`` / ``ops/install.ps1``'s
client-only pair, ``docs/guide/remote-bank.md``, the agent-isolation guide):
``pair <url> <code>`` and ``pair <url> --read-code --token-file P --json``.

The stand-in daemon answers ``/health``, ``POST /api/pair`` with the real
daemon's outcomes and response shape (``web/api.py`` ``_pair`` /
``_send_json``: 200 ``{principal, tier, bank}``, 400 ``pairing_refused``,
429 ``rate_limited``, 503 ``pairing_unavailable``, the other refusals), an
idempotent repeat for the same token hash, a dropped connection (outcome
unknown), and the bearer-checked ``/api/episodes`` the token check reads.
Every request is recorded (method, path, content type, whether ``Origin``
or ``Authorization`` was sent and with what, body).

Every file lands in the disposable home: HOME / USERPROFILE are the home
(the harness), HOMEDRIVE / HOMEPATH are removed, the arm's cwd is inside
it, and each case's ``after`` hook fails the run if a token file appeared
in, or vanished from, this user's real ``~/.pseudolife-mcp``.

Named rules: ``pair-token`` (the minted token and its SHA-256, the random
``pairing-<8 hex>.token`` name) and ``pair-deferral`` (a declared deferral
case's expectation). Owner-only is the oracle's own check
(``client_config.check_token_file``) on every token file, recorded in the
compared ``db`` field, plus the POSIX modes the harness records.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import core, normalize
from ..mutants import Mutant

CODE = "abcd-efgh-jkmn"
CANONICAL = "ABCDEFGHJKMN"
SEP = os.sep
DEFERRAL = "pseudolife-stdio: mode 'pair' is deferred in this candidate\n"
ERRORS = {400: "pairing_refused", 429: "rate_limited", 503: "pairing_unavailable",
          401: "unauthorized", 403: "forbidden_origin", 404: "not_found",
          405: "method_not_allowed", 413: "request_too_large",
          415: "content_type_must_be_application_json", 500: "internal_error"}


# ── the stand-in daemon ────────────────────────────────────────────────────

class Daemon:
    """``answers`` is consumed one per ``POST /api/pair``: ``"pair"``
    redeems (or repeats the original 200 for the same hash), ``"drop"``
    redeems and closes the connection unanswered, ``"garbage"`` answers 200
    with a body that is not JSON, an int answers that status with the real
    daemon's error body. ``default`` applies once ``answers`` is empty."""

    def __init__(self, port: int, *, url: str, answers=(), default="pair", principal="laptop",
                 tier="writer", bank="7f8402238397365c", health=None, accept_bearers=True,
                 result=None, hold_on_post=False):
        self.url = url
        self.health = health if health is not None else {
            "status": "ok", "auth": True, "version": "0.0.0-fixture"}
        self.answers = list(answers)
        self.default = default
        self.result = result if result is not None else {
            "principal": principal, "tier": tier, "bank": bank}
        self.accept_bearers = accept_bearers
        self.hold_on_post = hold_on_post
        self.held: list = []
        self.home: Path | None = None   # the arm's home, set by the case's setup
        self.paired_hash: str | None = None
        self._seen: list[dict] = []
        self._lock = threading.Lock()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self, body: bytes = b"") -> None:
                headers = {"content-type": self.headers.get("Content-Type")}
                for name in ("Origin", "Authorization"):
                    if self.headers.get(name) is not None:
                        headers[name.lower()] = self.headers.get(name)
                if self.command == "POST":
                    # The minted file still exists while its POST is in
                    # flight: whether one of the arm's token files hashes to
                    # the posted digest is the hash's only proof.
                    headers["x-fixture-digest-of-a-token-file"] = daemon._digest_matches(body)
                with daemon._lock:
                    daemon._seen.append({
                        "method": self.command, "target": self.path,
                        "headers": {k: v for k, v in headers.items() if v is not None},
                        "body": body.decode("utf-8", "backslashreplace")})

            def _send(self, status: int, payload) -> None:
                body = (payload if isinstance(payload, bytes)
                        else json.dumps(payload, default=str).encode("utf-8"))
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.send_header("cache-control", "no-store")
                self.send_header("x-content-type-options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._record()
                if self.path == "/health":
                    return self._send(200, daemon.health)
                if self.path.startswith("/api/episodes"):
                    bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                    digest = hashlib.sha256(bearer.encode()).hexdigest()
                    ok = daemon.accept_bearers and bearer and digest == daemon.paired_hash
                    return self._send(200 if ok else 401,
                                      {"episodes": []} if ok else {"error": "unauthorized"})
                return self._send(404, {"error": "not_found"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._record(body)
                if self.path != "/api/pair":
                    return self._send(404, {"error": "not_found"})
                if daemon.hold_on_post and daemon.home is not None:
                    # Hold every pairing file open as the CRT does (read and
                    # write sharing, no delete): the CLI's unlink then fails.
                    for path in daemon.home.rglob("pairing-*.token"):
                        daemon.held.append(open(path, "rb"))  # noqa: SIM115 (closed in close())
                with daemon._lock:
                    answer = daemon.answers.pop(0) if daemon.answers else daemon.default
                if answer in ("pair", "drop"):
                    digest = json.loads(body)["token_sha256"]
                    if daemon.paired_hash is None:
                        daemon.paired_hash = digest
                    elif daemon.paired_hash != digest:
                        return self._send(400, {"error": "pairing_refused"})
                    if answer == "drop":
                        self.close_connection = True
                        return None
                    return self._send(200, daemon.result)
                if answer == "garbage":
                    return self._send(200, b"<html>proxy page</html>")
                if isinstance(answer, tuple):  # (status, raw body)
                    if answer[0] == 200:
                        daemon.paired_hash = daemon.paired_hash or json.loads(body)["token_sha256"]
                    return self._send(*answer)
                return self._send(answer, {"error": ERRORS.get(answer, "refused")})

        class V4Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        class V6Server(V4Server):
            address_family = socket.AF_INET6

        self.servers = [V4Server(("127.0.0.1", port), Handler)]
        try:
            self.servers.append(V6Server(("::1", port), Handler))
        except OSError:
            pass
        self.threads = []
        for server in self.servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def _digest_matches(self, body: bytes) -> str:
        try:
            digest = json.loads(body)["token_sha256"]
        except (ValueError, KeyError, TypeError):
            return "unreadable"
        for path in sorted((self.home or Path("/nonexistent")).rglob("*.token")):
            try:
                if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    return "matches"
            except OSError:
                continue
        return "none"

    def requests(self) -> list:
        with self._lock:
            return [dict(entry) for entry in self._seen]

    def close(self) -> None:
        for handle in self.held:
            handle.close()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ── the real-home guard ─────────────────────────────────────────────────────

def _real_tokens() -> dict | None:
    """Every token file in this user's real ``~/.pseudolife-mcp`` (pair's
    only write on master): name, size, mtime, mode, the oracle's owner-only
    verdict and the content's SHA-256. The digest lives only in this
    process, for an equality check; it is never printed or stored."""
    from pseudolife_memory import client_config  # noqa: PLC0415 (the oracle's own check)
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    try:
        paths = sorted((home / ".pseudolife-mcp").glob("*.token"))
    except OSError:
        return None
    found = {}
    for path in paths:
        try:
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).digest()
        except OSError as error:
            found[path.name] = ("unreadable", type(error).__name__)
            continue
        found[path.name] = (info.st_size, info.st_mtime_ns, info.st_mode,
                            client_config.check_token_file(path).get("status"), digest)
    return found


_BASELINE = _real_tokens()


def _check_real_home(arm) -> None:
    now = _real_tokens()
    if now != _BASELINE:
        changed = sorted(name for name in set(now or {}) | set(_BASELINE or {})
                         if (now or {}).get(name) != (_BASELINE or {}).get(name))
        raise AssertionError(f"pair row: the real ~/.pseudolife-mcp token files changed "
                             f"({arm.name} arm): {changed}")


# ── named rules ─────────────────────────────────────────────────────────────

_PAIRING = re.compile(rb"pairing-([0-9a-f]{8})\.token")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def _swap_all(obs: dict, old: bytes, new: bytes) -> None:
    for field in ("stdout", "stderr"):
        normalize._put(obs, field, normalize._get(obs, field).replace(old, new))
    for record in ("files", "modes"):
        table = obs.get(record) or {}
        obs[record] = {k.replace(old.decode(), new.decode()): v for k, v in table.items()}
    acl = (obs.get("db") or {}).get("owner_only") if isinstance(obs.get("db"), dict) else None
    if acl is not None:
        obs["db"]["owner_only"] = {k.replace(old.decode(), new.decode()): v
                                   for k, v in acl.items()}


@normalize.rule("pair-token")
def pair_token(obs: dict) -> None:
    """The token is random by design. Every ``POST /api/pair`` of this arm
    must carry the same ``token_sha256``, and the stand-in daemon must have
    found, while that POST was in flight, a token file in the arm's home
    whose SHA-256 it is (the request's ``x-fixture-digest-of-a-token-file``
    is ``matches``); only then does the hash become ``<token-sha256>``. A
    token file left in the home (43 URL-safe base64 characters) becomes
    ``<token>`` only when it hashes to that digest, and then the
    verification's ``Bearer <that token>`` becomes ``Bearer <token>``. A
    ``pairing-<8 hex>.token`` name becomes ``pairing-<hex>.token`` only when
    that file exists in this arm's home, or when no stream names it."""
    posts = [r for r in obs.get("requests", []) if r.get("method") == "POST"]
    hashes = set()
    for post in posts:
        try:
            hashes.add(json.loads(post["body"])["token_sha256"])
        except (ValueError, KeyError, TypeError):
            return
    if len(hashes) > 1:
        return
    digest = next(iter(hashes)) if hashes else None
    minted = {}
    for rel, value in obs.get("files", {}).items():
        if rel.endswith(".token") and value.startswith("file:"):
            text = base64.b64decode(value[5:]).decode("utf-8", "replace")
            if _TOKEN.fullmatch(text):
                minted[rel] = text
    for rel, token in minted.items():
        if digest is not None and hashlib.sha256(token.encode()).hexdigest() == digest:
            obs["files"][rel] = "file:" + base64.b64encode(b"<token>").decode()
            for request in obs.get("requests", []):
                if request["headers"].get("authorization") == f"Bearer {token}":
                    request["headers"]["authorization"] = "Bearer <token>"
    proven = all(post["headers"].get("x-fixture-digest-of-a-token-file") == "matches"
                 for post in posts)
    if digest is not None and re.fullmatch(r"[0-9a-f]{64}", digest) and proven:
        for post in posts:
            post["body"] = post["body"].replace(digest, "<token-sha256>")
    names = {m.group(0) for field in ("stdout", "stderr")
             for m in _PAIRING.finditer(normalize._get(obs, field))}
    names |= {m.group(0) for rel in obs.get("files", {})
              for m in _PAIRING.finditer(rel.encode())}
    present = {m.group(0) for rel in obs.get("files", {})
               for m in _PAIRING.finditer(rel.encode())}
    if len(names) == 1 and (names <= present or not present):
        _swap_all(obs, next(iter(names)), b"pairing-<hex>.token")


_TRACEBACK = re.compile(rb"\ATraceback \(most recent call last\):\r?\n.*\n(?P<last>[A-Za-z]+"
                        rb"Error: [^\r\n]*)\r?\n\Z", re.DOTALL)
DECLARED_CRASHES = {
    # /health nested past CPython's recursion limit (`_json` outside a try).
    "pair-recursion-traceback": rb"RecursionError: maximum recursion depth exceeded",
    # A refusal whose `target.unlink(missing_ok=True)` raises (the file is
    # held open without delete sharing): the oracle dies after the POST.
    "pair-cleanup-traceback": rb"PermissionError: [WinError 32] ",
}


def _declared_crash(name: str, marker: bytes):
    def rule(obs: dict) -> None:
        """Declared: the oracle dies with this uncaught exception (exit 1, a
        traceback naming its own source files); the candidate prints its
        deferral line, exit 1. Only that traceback with exit 1 maps; the
        requests, the files and the owner-only record stay compared."""
        if obs.get("arm") != "python" or obs["exit"] != 1:
            return
        match = _TRACEBACK.match(normalize._get(obs, "stderr"))
        if match and match.group("last").startswith(marker) and not normalize._get(obs, "stdout"):
            native = DEFERRAL.replace("\n", "\r\n") if core.WINDOWS else DEFERRAL
            normalize._put(obs, "stderr", native.encode())
    normalize.rule(name)(rule)


for _name, _marker in DECLARED_CRASHES.items():
    _declared_crash(_name, _marker)


@normalize.rule("pair-deferral")
def pair_deferral(obs: dict) -> None:
    """A declared deferral case: the candidate must print the dispatcher's
    deferral line, exit 1, send no request and create no file. The oracle
    arm's observation is replaced by that expectation."""
    if obs.get("arm") != "python":
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    native = DEFERRAL.replace("\n", "\r\n") if core.WINDOWS else DEFERRAL
    obs["stderr"] = base64.b64encode(native.encode()).decode()
    obs["files"] = dict(obs.get("before", {}))
    obs["modes"] = dict(obs.get("before_modes", {}))
    if "requests" in obs:
        obs["requests"] = []
    obs["db"] = {"owner_only": dict(obs.get("before_owner_only", {}))}


# ── cases ───────────────────────────────────────────────────────────────────

def _owner_only(home: Path) -> dict:
    from pseudolife_memory import client_config  # noqa: PLC0415 (the oracle's own check)
    found = {}
    for path in sorted(home.rglob("*.token")):
        rel = path.relative_to(home).as_posix()
        found[rel] = client_config.check_token_file(path).get("status")
    return found


def _seed_token(path: Path, token: str) -> None:
    from pseudolife_memory.credentials import _write_token_file  # noqa: PLC0415 (oracle writer)
    _write_token_file(path, token)


def _env(**extra) -> dict:
    env = {"HOMEDRIVE": None, "HOMEPATH": None, "PSEUDOLIFE_MCP_TOKEN": None,
           "PSEUDOLIFE_MCP_TOKEN_FILE": None, "PSEUDOLIFE_MCP_TOKENS": None,
           "HTTP_PROXY": None, "HTTPS_PROXY": None, "ALL_PROXY": None,
           "http_proxy": None, "https_proxy": None, "all_proxy": None}
    env.update(extra)
    return env


def case(case_id: str, argv: list[str], *, port: int | None = None, url: str | None = None,
         stdin: bytes = b"", seed=None, rules=("pair-token",), env=None, timeout=60,
         platforms=("windows", "linux"), **daemon) -> core.Case:
    def setup(arm):
        if arm.daemon is not None:
            arm.daemon.home = arm.home
        if seed:
            seed(arm.home)
        arm.state["before"] = core.snapshot(arm.home)
        arm.state["before_modes"] = core.modes(arm.home)
        arm.state["before_owner_only"] = _owner_only(arm.home)

    def after(arm, obs):
        obs["arm"] = arm.name
        obs["before"] = arm.state["before"]
        obs["before_modes"] = arm.state["before_modes"]
        obs["before_owner_only"] = arm.state["before_owner_only"]
        obs["db"] = {"owner_only": _owner_only(arm.home)}
        _check_real_home(arm)

    factory = None
    if port is not None:
        def factory():
            return Daemon(port, url=url, **daemon)
    return core.Case(case_id, ["pair", *argv], env=_env(**(env or {})), stdin=stdin,
                     setup=setup, after=after, rules=rules, daemon=factory, timeout=timeout,
                     platforms=platforms)


def target(remote: bool = False) -> tuple[int, str]:
    port = free_port()
    return port, (f"http://localhost.:{port}" if remote else f"http://127.0.0.1:{port}")


def seed_file(rel: str, token: str):
    def seed(home: Path):
        _seed_token(home / rel, token)
    return seed


def cases() -> list[core.Case]:
    out: list[core.Case] = []
    add = out.append

    def live(case_id, argv_tail, *, remote=False, **options):
        port, url = target(remote)
        add(case(case_id, [url, *argv_tail], port=port, url=url, **options))

    # Success.
    live("success", [CODE])
    live("success-json", [CODE, "--json"])
    live("success-canonical-code", ["ABCDEFGHJKMN"])
    live("success-lookalikes", ["oil0-1234-5678"])
    live("installer-read-code", ["--read-code", "--token-file", "{HOME}" + SEP + "chosen" + SEP
                                 + "bank.token", "--json"], stdin=f"{CODE}\n".encode())
    live("read-code-crlf", ["--read-code"], stdin=f"  {CODE.upper()}  \r\n".encode())
    live("read-code-no-newline", ["--read-code", "--json"], stdin=CODE.encode())
    live("token-file-relative", [CODE, "--token-file", "rel.token"])
    live("token-file-tilde", [CODE, "--token-file", "~" + SEP + "tilde.token", "--json"])
    live("tier-null", [CODE], tier=None)
    live("tier-not-printable-json", [CODE, "--json"], tier="a\nb", bank="x" * 129)
    live("tier-non-ascii-json", [CODE, "--json"], tier="écrit ✓", bank="")
    live("remote-plain-http", [CODE], remote=True)
    live("remote-plain-http-json", [CODE, "--json"], remote=True)

    # Usage.
    for case_id, tail, stdin in (
            ("usage-code-and-read-code", [CODE, "--read-code", "--json"], f"{CODE}\n".encode()),
            ("usage-no-code", [], b""),
            ("usage-no-code-json", ["--json"], b""),
            ("usage-empty-stdin", ["--read-code"], b""),
            ("usage-blank-stdin-json", ["--read-code", "--json"], b"  \t \n"),
            ("usage-short-code", ["ABCD-EFGH"], b""),
            ("usage-bad-letter", ["ABCD-EFGH-JKMU"], b""),
            ("usage-long-code-json", ["ABCD-EFGH-JKMN-P", "--json"], b""),
            ("usage-punctuation", ["!!!!-????-****"], b""),
            ("usage-empty-code", [""], b"")):
        live(case_id, tail, stdin=stdin)
    for case_id, url in (("url-scheme", "ftp://example.com"),
                         ("url-credentials", "http://user:secret@example.com:8765"),
                         ("url-path-query", "http://example.com:8765/mcp?key=secret"),
                         ("url-no-scheme", "100.64.0.2:8765"),
                         ("url-bad-port", "http://h:99999")):
        add(case(case_id, [url, CODE]))
        add(case(case_id + "-json", [url, CODE, "--json"]))

    # Refused before any change.
    live("refused-no-auth-json", [CODE, "--json"],
         health={"status": "ok", "auth": False, "version": "x"})
    live("refused-auth-absent", [CODE], health={"status": "ok"})
    live("refused-auth-text", [CODE], health={"status": "ok", "auth": "yes ✓", "n": 1})
    live("refused-auth-object-json", [CODE, "--json"],
         health={"status": "ok", "auth": {"a": [1, 2.5, None]}})
    live("refused-degraded", [CODE], health={"status": "degraded", "auth": True})
    live("refused-health-not-object", [CODE], health=[1])
    # A closed loopback port (core.DEAD_DAEMON_URL): the same URL in every run,
    # so the case replays as a golden.
    add(case("refused-unreachable", [core.DEAD_DAEMON_URL, CODE, "--json"]))
    live("refused-existing-token-file", [CODE, "--token-file", "{HOME}" + SEP + "existing.token",
                                         "--json"],
         seed=seed_file("existing.token", "kept-" + "k" * 32))

    # Refused by the daemon (the file is removed).
    live("daemon-refused-code", [CODE, "--json"], default=400)
    live("daemon-rate-limited", [CODE], default=429)
    for status in (401, 403, 404, 405, 413, 415):
        live(f"daemon-refused-{status}", [CODE, "--json"], default=status)

    # Lost answers.
    live("lost-then-paired", [CODE, "--json"], answers=["drop"])
    live("garbage-then-paired", [CODE], answers=["garbage"])
    live("unavailable-then-paired-json", [CODE, "--json"], answers=[503])
    live("unknown-dropped", [CODE], default="drop")
    live("unknown-503-json", [CODE, "--json"], default=503)
    live("unknown-500", [CODE], default=500)
    live("lost-then-rate-limited", [CODE], answers=["drop", 429])
    live("unavailable-then-refused-json", [CODE, "--json"], answers=[503, 400])
    live("unverified-json", [CODE, "--json"], accept_bearers=False)
    live("unverified-explicit-file", [CODE, "--token-file", "{HOME}" + SEP + "x.token"],
         accept_bearers=False)

    # The principal name cannot steer the path.
    for index, name in enumerate(["../evil", "..", "a/b", "C:\\x", "default", "daemon", "",
                                  "a" * 65, 123, None, ["laptop"], ".hidden", "Laptop"]):
        live(f"rogue-name-{index}", [CODE, "--json"] if index % 2 else [CODE], principal=name)
    live("existing-principal-file-kept-json", [CODE, "--json"],
         seed=seed_file(".pseudolife-mcp/laptop.token", "older-" + "o" * 32))
    live("existing-principal-file-kept", [CODE],
         seed=seed_file(".pseudolife-mcp/laptop.token", "older-" + "o" * 32))

    # Daemon answers only CPython's reader holds: lone surrogates, nesting up
    # to its measured recursion limit (987 containers on the pair paths),
    # integers past 4300 digits.
    def nest(depth: int) -> bytes:
        return b"[" * depth + b"]" * depth

    live("health-surrogate-auth", [CODE], health={"status": "ok", "auth": "\ud800"})
    live("health-surrogate-auth-json", [CODE, "--json"],
         health={"status": "ok", "auth": ["x\udc00", {"\ud800": 1}]})
    live("health-nested-at-limit-json", [CODE, "--json"],
         health=b'{"status": "ok", "auth": ' + nest(986) + b"}")
    live("health-nested-past-limit", [CODE],
         health=b'{"status": "ok", "auth": ' + nest(987) + b"}",
         rules=("pair-token", "pair-recursion-traceback"))
    live("health-int-past-limit", [CODE],
         health=b'{"status": "ok", "auth": true, "x": 1' + b"0" * 4300 + b"}")
    live("health-int-at-limit-json", [CODE, "--json"],
         health=b'{"status": "ok", "auth": true, "x": -' + b"9" * 4300 + b"}")
    live("answer-nested-at-limit-json", [CODE, "--json"],
         answers=[(200, b'{"principal": "laptop", "tier": "writer", "x": ' + nest(986) + b"}")])
    live("answer-nested-past-limit-then-paired", [CODE],
         answers=[(200, b'{"principal": "laptop", "x": ' + nest(987) + b"}")])
    live("answer-refusal-nested-past-limit", [CODE],
         answers=[(400, b'{"error": ' + nest(990) + b"}")])
    live("answer-surrogates-json", [CODE, "--json"],
         answers=[(200, b'{"principal": "lap\\ud800", "tier": "\\udc00w", "bank": "b"}')])
    live("answer-int-past-limit-then-paired-json", [CODE, "--json"],
         answers=[(200, b'{"principal": "laptop", "x": 1' + b"0" * 4300 + b"}")])

    # A refusal whose file the oracle cannot remove (held open without
    # delete sharing, which only Windows enforces): declared crash.
    live("refusal-cleanup-fails", [CODE], default=400, hold_on_post=True,
         rules=("pair-token", "pair-cleanup-traceback"), platforms=("windows",))

    # Declared deferrals: before any effect.
    deferred = ("pair-deferral",)
    port = free_port()
    add(case("defer-https", [f"https://127.0.0.1:{port}", CODE], rules=deferred))
    live("defer-proxy-environment", [CODE], rules=deferred,
         env={"HTTP_PROXY": "http://127.0.0.1:9"})
    live("defer-non-ascii-code", ["abcd-efgh-jkmñ"], rules=deferred)
    live("defer-non-ascii-stdin", ["--read-code"], stdin="abcd-efgh-jkmñ\n".encode(),
         rules=deferred)
    live("defer-help", ["--help"], rules=deferred)
    live("defer-code-after-option", ["--json", CODE], rules=deferred)
    live("defer-repeated-option", [CODE, "--json", "--json"], rules=deferred)
    live("defer-equals-spelling", [CODE, "--token-file=x.token"], rules=deferred)
    add(case("defer-no-arguments", [], rules=deferred))
    return out


MUTANTS = [
    Mutant("pair-file-kept-on-refusal", "pair", "shim/src/cli/pairing/pair.rs",
           "match std::fs::remove_file(&target) {",
           "match Ok::<(), std::io::Error>(()) {",
           cases=("daemon-refused-code", "daemon-rate-limited")),
    Mutant("pair-constant-digest", "pair", "shim/src/cli/pairing/pair.rs",
           "super::sha256_hex(&token)\n", "\"0\".repeat(64)\n",
           cases=("daemon-refused-code",)),
    Mutant("pair-retry-flipped", "pair", "shim/src/cli/pairing/pair.rs",
           "if uncertain && (300..500).contains(&status) {",
           "if !uncertain && (300..500).contains(&status) {",
           cases=("daemon-refused-code", "lost-then-rate-limited")),
    Mutant("pair-unknown-exit", "pair", "shim/src/cli/pairing/pair.rs",
           "const EXIT_UNKNOWN: u8 = 5;", "const EXIT_UNKNOWN: u8 = 1;",
           cases=("unverified-json",)),
    Mutant("pair-message-token", "pair", "shim/src/cli/pairing/pair.rs",
           "pairing is rate-limited on the daemon, try again in a minute",
           "pairing is rate limited on the daemon, try again in a minute",
           cases=("daemon-rate-limited",)),
    Mutant("pair-hash-of-wrong-text", "pair", "shim/src/cli/pairing/pair.rs",
           "super::sha256_hex(&token)\n", "super::sha256_hex(&code)\n",
           cases=("success-json",)),
    Mutant("pair-reserved-name-moves", "pair", "shim/src/cli/pairing/pair.rs",
           ".filter(|name| super::valid_principal_name(name) && !super::RESERVED.contains(name));",
           ".filter(|name| super::valid_principal_name(name));",
           cases=("rogue-name-4",)),
]
# Owner-only is made differently per platform, so each has its own break.
if core.WINDOWS:
    # The protected DACL is the whole protection (the CRT ignores the mode).
    MUTANTS.append(Mutant("pair-not-owner-only", "pair", "shim/src/cli/pairing/token_file.rs",
                          "        protect(&file)?;\n", "", cases=("success",)))
    # Only Windows refuses to delete a file another process holds open.
    MUTANTS.append(Mutant(
        "pair-cleanup-error-swallowed", "pair", "shim/src/cli/pairing/pair.rs",
        "Err(problem) if problem.kind() != std::io::ErrorKind::NotFound => Err(Defer),",
        "Err(problem) if problem.kind() == std::io::ErrorKind::NotFound => Err(Defer),",
        cases=("refusal-cleanup-fails",)))
else:
    # `.mode(0o600)` at creation would hide a removed protect(): widen the
    # fchmod instead, which the owner-only validation then refuses.
    MUTANTS.append(Mutant("pair-mode-not-owner-only", "pair",
                          "shim/src/cli/pairing/token_file.rs",
                          "file.set_permissions(fs::Permissions::from_mode(0o600))",
                          "file.set_permissions(fs::Permissions::from_mode(0o640))",
                          cases=("success",)))
