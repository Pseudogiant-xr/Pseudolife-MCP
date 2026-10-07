"""The extractor API key must never follow a redirect to another host.

``PSEUDOLIFE_DREAM_API_KEY`` / ``extractor_api_key`` rides every
OpenAI-compatible extractor POST as a bearer header. urllib's default
redirect handler (every supported Python) re-issues a POST answered
301/302/303 as a body-less GET to the Location target and copies every
header except Content-Length/Content-Type -- Authorization included -- so a
redirecting endpoint handed the key to whatever host it named. A body-less GET can never
return a chat completion, so no working extractor configuration depended on
that redirect; refusing it costs nothing and closes the leak.

These tests stand up a real redirector and a real target on loopback and
assert the target never receives a request through any credentialed call
site. 307/308 are already refused for POST by the stdlib; they are pinned
here so a future stdlib that starts following them cannot reopen the leak.
"""
from __future__ import annotations

import ast
import http.server
import inspect
import json
import threading
from types import SimpleNamespace

import pytest

from pseudolife_memory.memory import dream as D
from pseudolife_memory.memory import recall

KEY = "sk-test-redirect-canary"

# One reply shape every call site accepts: claims/events/lessons/relations/
# verdicts parse to empty lists, so the pre-fix code "succeeds" through the
# redirect and the only observable is what the target received.
_CONTENT = json.dumps({"claims": [], "events": [], "lessons": [],
                       "relations": [], "verdicts": [], "outcomes": []})
_REPLY = json.dumps({"model": "m", "choices": [
    {"message": {"content": _CONTENT}}]}).encode()


class _Target(http.server.BaseHTTPRequestHandler):
    """The host a redirect points at. Records every request it receives."""
    seen: list[dict] = []

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        type(self).seen.append({"method": self.command, "path": self.path,
                                "auth": self.headers.get("Authorization"),
                                "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_REPLY)))
        self.end_headers()
        self.wfile.write(_REPLY)

    do_GET = do_POST = do_HEAD = _record

    def log_message(self, *a):
        pass


class _Redirector(http.server.BaseHTTPRequestHandler):
    """The configured endpoint. ``/<code>/v1/...`` answers ``<code>`` with a
    Location on the target, so one server covers every redirect status."""
    target_port = 0
    seen: list[dict] = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        type(self).seen.append({"path": self.path,
                                "auth": self.headers.get("Authorization")})
        code = int(self.path.split("/")[1])
        self.send_response(code)
        self.send_header(
            "Location",
            f"http://127.0.0.1:{self.target_port}/v1/chat/completions")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def servers():
    target = _serve(_Target)
    _Redirector.target_port = target.server_address[1]
    redirector = _serve(_Redirector)
    yield SimpleNamespace(target=target.server_address[1],
                          redirector=redirector.server_address[1])
    for srv in (target, redirector):
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def wire(servers):
    _Target.seen.clear()
    _Redirector.seen.clear()
    return servers


def _extractor(url):
    return D.OpenAICompatExtractor(url, "m", api_key=KEY, timeout_seconds=10)


_SIG = {"id": 1, "task": "t", "outcome": "success", "about": "a",
        "detail": "d", "polarity": None, "origin": "action",
        "created_at": 1.0}
_PROPOSAL = {"n": 1, "from": {"display": "a"}, "into": {"display": "b"},
             "reason": "test"}

# One entry per credentialed transport site in OpenAICompatExtractor (the
# judges share _judge_request; extract_lessons/extract_rules share
# _lessons_completion).
_SITES = {
    "extract": lambda ex: ex.extract(["note"], []),
    "extract_events": lambda ex: ex.extract_events(["note"]),
    "lessons_completion": lambda ex: ex.extract_lessons([_SIG]),
    "extract_relations": lambda ex: ex.extract_relations(["note"],
                                                         [("uses", "d")]),
    "judge_request": lambda ex: ex.judge_merges([_PROPOSAL]),
    "infer_outcomes": lambda ex: ex.infer_outcomes("episode text"),
    "summarize_session": lambda ex: ex.summarize_session("episode text"),
}

_CODES = (301, 302, 303, 307, 308)


@pytest.mark.parametrize("code", _CODES)
@pytest.mark.parametrize("site", sorted(_SITES))
def test_extractor_refuses_redirect_and_never_forwards_the_key(
        wire, site, code):
    ex = _extractor(f"http://127.0.0.1:{wire.redirector}/{code}/v1")
    raised = None
    try:
        _SITES[site](ex)
    except D.ExtractorError as exc:
        raised = exc
    # The configured endpoint was reached with the key (the request really
    # went out) ...
    assert [r["auth"] for r in _Redirector.seen] == [f"Bearer {KEY}"]
    # ... the redirect target received nothing at all ...
    assert _Target.seen == []
    # ... and the call failed loudly through the no-redirect handler (the
    # stdlib's own 307/308 refusal says neither "refused" nor the target),
    # rather than reporting an empty result that would advance the cursor.
    assert raised is not None
    assert str(code) in str(raised) and "refused" in str(raised)


def _recall_cfg(monkeypatch, url):
    """A dream config for ``simple_complete``; env overrides cleared so the
    config's endpoint and key are the ones used."""
    for name in ("PSEUDOLIFE_DREAM_BASE_URL", "PSEUDOLIFE_DREAM_MODEL",
                 "PSEUDOLIFE_DREAM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(extractor_base_url=url, extractor_model="m",
                           extractor_api_key=KEY)


@pytest.mark.parametrize("code", _CODES)
def test_recall_simple_complete_refuses_redirect(wire, monkeypatch, code):
    cfg = _recall_cfg(monkeypatch,
                      f"http://127.0.0.1:{wire.redirector}/{code}/v1")
    assert recall.simple_complete(cfg, "which entities?") == ""
    assert [r["auth"] for r in _Redirector.seen] == [f"Bearer {KEY}"]
    assert _Target.seen == []


def test_direct_endpoint_still_receives_the_post_with_its_key(wire):
    """Positive control: without a redirect the request is unchanged -- one
    POST, bearer intact, JSON body sent."""
    ex = _extractor(f"http://127.0.0.1:{wire.target}/v1")
    assert ex.extract(["note"], []) == []
    assert len(_Target.seen) == 1
    got = _Target.seen[0]
    assert (got["method"], got["path"], got["auth"]) == (
        "POST", "/v1/chat/completions", f"Bearer {KEY}")
    assert json.loads(got["body"])["model"] == "m"


def test_recall_direct_endpoint_still_answers(wire, monkeypatch):
    """Positive control for ``simple_complete``: nothing else exercises its
    success path against a real server."""
    cfg = _recall_cfg(monkeypatch, f"http://127.0.0.1:{wire.target}/v1")
    assert recall.simple_complete(cfg, "which entities?") == _CONTENT
    assert [(r["method"], r["auth"]) for r in _Target.seen] == [
        ("POST", f"Bearer {KEY}")]


# Unauthenticated GETs (endpoint health, served-model listing) that may keep
# the stdlib opener: they carry no credential for a redirect to forward.
_PLAIN_OPEN_ALLOWED = {"probe_endpoint", "fetch_served_model"}


def _plain_opens(source: str) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` of every call in ``source`` that opens a
    URL without the helper: ``urlopen`` by bare name or as an attribute of
    anything but ``no_redirect``, and any ``build_opener`` call (the helper is
    the one place an opener is built). Keyed on the call, not on where the
    Authorization header is set, so moving the header into a shared helper
    cannot hide a plain ``urlopen`` from it."""
    found: list[tuple[str, int]] = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call):
                f = child.func
                name = (f.id if isinstance(f, ast.Name)
                        else f.attr if isinstance(f, ast.Attribute) else None)
                via_helper = (isinstance(f, ast.Attribute)
                              and isinstance(f.value, ast.Name)
                              and f.value.id == "no_redirect")
                if name in ("urlopen", "build_opener") and not via_helper:
                    found.append((fn, child.lineno))
            visit(child, fn)

    visit(ast.parse(source), "<module>")
    return found


def test_extractor_modules_open_urls_only_through_the_helper():
    """Structural pin for call sites added later: a plain opener in these
    modules reopens the leak without failing any loopback test above. A new
    unauthenticated GET joins ``_PLAIN_OPEN_ALLOWED`` deliberately."""
    offenders = [(mod.__name__, fn, line) for mod in (D, recall)
                 for fn, line in _plain_opens(inspect.getsource(mod))
                 if fn not in _PLAIN_OPEN_ALLOWED]
    assert offenders == []
    defined = {n.name for n in ast.walk(ast.parse(inspect.getsource(D)))
               if isinstance(n, ast.FunctionDef)}
    assert _PLAIN_OPEN_ALLOWED <= defined      # the allowlist cannot rot


@pytest.mark.parametrize("src", [
    "def f(req):\n    return urllib.request.urlopen(req)\n",
    "from urllib.request import urlopen\n"
    "def f(req):\n    return urlopen(req)\n",
    "def f(req):\n    return urllib.request.build_opener().open(req)\n",
], ids=["attribute", "bare-name", "build-opener"])
def test_guard_catches_each_bypass_shape(src):
    assert _plain_opens(src)
    assert not _plain_opens(
        "def f(req):\n    return no_redirect.urlopen(req, timeout=1)\n")
