"""ASGI plumbing for the Cortex Console.

Composes, in front of the FastMCP streamable app, three additional surfaces:

* ``/health``        — open liveness probe (unchanged behaviour).
* ``/`` , ``/ui/*``  — the static SPA shell (open; it's just code, no data).
* ``/api/*``         — JSON over ``MemoryService``, behind the same bearer-token
                       gate as ``/mcp``.

Everything else (``/mcp`` and any MCP sub-path) is forwarded to the wrapped MCP
app untouched. Implemented against the raw ASGI protocol (no Starlette
middleware classes) to match :mod:`pseudolife_memory.daemon` and avoid coupling
to the MCP SDK's internal middleware surface.

Sync ``service`` calls are dispatched to a threadpool so a slow retrieval can't
stall the event loop (and the concurrent ``/mcp`` traffic).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import mimetypes
import ntpath
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from pseudolife_memory.web.routes import ConsoleRoutes
from pseudolife_memory.web.session_hook import (
    hook_memory_changes, hook_memory_policy, hook_session_end, hook_session_start)

logger = logging.getLogger("pseudolife-mcp.web")

STATIC_DIR = Path(__file__).parent / "static"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Control patches and graph actions are small JSON objects. Text-bearing
# writes need room for whole documents; coordination keeps its own 32 KiB
# contract. These are wire-byte limits, including JSON escaping/metadata.
_CONTROL_BODY_LIMIT = 256 * 1024
_TEXT_BODY_LIMIT = 4 * 1024 * 1024
_SESSION_END_BODY_LIMIT = 16 * 1024
# A pairing body is {"code": 14 chars, "token_sha256": 64 hex}: ~100 bytes.
_PAIR_BODY_LIMIT = 1024
# A sign-in body is {"code", "token_sha256", "label" (64 chars)}: under
# 300 bytes (spec 2026-11-02, section 4).
_SIGNIN_BODY_LIMIT = 1024
# A passkey sign-in carries a WebAuthn assertion: an RS256 one alone is over
# 1 KiB, so its body gets 8 KiB (spec 2026-11-02, section 4).
_PASSKEY_SIGNIN_BODY_LIMIT = 8192
# Console sessions (schema v59) resolve on /api/* except these: the board's
# mailbox routes (a session must never become an agent credential), the
# sign-in link (a session cannot issue a session: it is resolved there only
# to be refused by name) and the daemon notice
# (spec 2026-11-02, section 3). /api/hook/* and /mcp never reach the
# session resolver at all.
_SESSIONLESS_API_PATHS = frozenset({"/api/console/link", "/api/daemon-notice"})
_CONSOLE_PREFIX = "/api/console/"
_BANK_UNREACHABLE = ("the daemon could not reach its bank just now; try again in a minute, "
                     "and if it keeps failing, check the daemon log")
_TEXT_BODY_PATHS = {"/api/facts/set", "/api/consolidate", "/api/supersede"}
# Routes that reconfigure the daemon or speak as it: an invited (stored)
# principal is refused them (spec 2026-10-02, "What an invited principal
# can do"); environment principals keep them.
_OPERATOR_POST_PATHS = frozenset({"/api/config", "/api/daemon-notice"})


# v54 (addendum 2026-10-04-board-roles-passkey.md, section 3): script in the
# Console's origin could request its own maintainer challenge at the moment
# the maintainer expects a passkey prompt, so every /ui/ response forbids
# inline and foreign script, plugins, <base> rewrites, framing and foreign
# form targets. ``style-src 'unsafe-inline'`` stays: Svelte and the vendored
# 3D engine set style attributes; script is the attack this blocks.
CONSOLE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
               "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
               "form-action 'self'")
CONSOLE_SECURITY_HEADERS = (
    (b"content-security-policy", CONSOLE_CSP.encode("ascii")),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    # Served types are meant: never sniff one as HTML or script (security
    # review, 2026-10-04). JSON answers send it too (``_send_json``).
    (b"x-content-type-options", b"nosniff"),
)
_MAINTAINER_PREFIX = "/api/maintainer"


def _is_maintainer_path(path: str) -> bool:
    return path == _MAINTAINER_PREFIX or path.startswith(_MAINTAINER_PREFIX + "/")


def _maintainer_error(exc: Exception) -> tuple[int, dict]:
    """A maintainer route's refusal: its public code and status, never the
    text of an unexpected exception. A plain ``ValueError`` whose text is a
    known code (the Console's fixture service raises those) maps the same."""
    from pseudolife_memory.storage.maintainer import MaintainerError
    code = getattr(exc, "code", None) if isinstance(exc, MaintainerError) else str(exc)
    if code not in MaintainerError.STATUS:
        code = "invalid_request" if isinstance(exc, ValueError) else "coordination_unavailable"
    public = exc.public if isinstance(exc, MaintainerError) and exc.public else {}
    return MaintainerError.STATUS[code], {**public, "error": code}


def _body_limit(path: str) -> int:
    if path.startswith("/api/coordination/"):
        return 32768
    return _TEXT_BODY_LIMIT if path in _TEXT_BODY_PATHS else _CONTROL_BODY_LIMIT

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")


async def _send_json(send, status: int, payload: Any) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff")],
    })
    await send({"type": "http.response.body", "body": body})


async def _send_bytes(send, status: int, body: bytes, content_type: str,
                      cache: str = "no-cache", extra_headers=()) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", content_type.encode()),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", cache.encode()), *extra_headers],
    })
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive, max_bytes: int | None = None) -> bytes:
    chunks = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunk = message.get("body", b"") or b""
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ValueError("request_too_large")
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


async def _wait_while_connected(operation, receive):
    """Cancel a long mailbox request when its HTTP client disconnects."""
    async def disconnected():
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            # ASGI normally blocks after the body. Tolerate a repeated request
            # event without starving the handler (also used by simple drivers).
            await asyncio.sleep(0)

    work = asyncio.create_task(operation)
    gone = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
        if gone in done:
            return False, None
        return True, work.result()
    finally:
        for task in (work, gone):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, gone, return_exceptions=True)


async def _send_coordination_error(send, exc):
    from pseudolife_memory.coordination import public_detail, public_error
    code = public_error(exc)
    detail = public_detail(exc)
    status = (401 if code in {"unauthorized", "authentication_required",
                             "instance_authentication_required"}
              else 403 if code in {"principal_not_allowed", "invalid_credential",
                                   "child_send_refused"}
              else 404 if code == "instance_not_found"
              else 409 if code == "bank_identity_mismatch"
              else 429 if code in {"wait_capacity_exceeded", "rate_limited", "queue_full",
                                   "lease_queue_full"}
              else 500 if code in {"coordination_unavailable", "invalid_bank_identity"}
              else 503 if code == "principals_unavailable"
              else 400)
    if status == 500:
        # Database errors can include complete rows. Neither exception messages
        # nor tracebacks are appropriate diagnostics for private mail failures.
        logger.error("coordination handler failed (%s)", type(exc).__name__)
    await _send_json(send, status, {"error": code} if detail is None
                     else {"error": code, "detail": detail})


def _parse_query(scope) -> dict[str, str]:
    raw = scope.get("query_string", b"").decode("utf-8", "replace")
    out: dict[str, str] = {}
    if not raw:
        return out
    from urllib.parse import parse_qsl
    for k, v in parse_qsl(raw, keep_blank_values=True):
        out[k] = v  # last value wins; handlers that want lists split on ","
    return out


# Names Windows maps to devices in every directory, with or without an
# extension (``NUL``, ``com1.txt``); opening one reaches the device, not a file.
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{kind}{n}" for kind in ("COM", "LPT") for n in "123456789¹²³"})


def _inside_static_root(root: str, rel: str, pathmod: Any = os.path) -> bool:
    """Whether ``rel`` joined to ``root`` stays under it, decided from the
    strings alone. An absolute, drive, UNC or device form replaces the root
    in the join and so lands outside it. Under Windows path rules a device
    name or an alternate data stream (``name:stream``) is refused too."""
    base = pathmod.normcase(pathmod.normpath(root))
    joined = pathmod.normcase(pathmod.normpath(pathmod.join(root, rel)))
    try:
        if pathmod.commonpath([base, joined]) != base:
            return False
    except ValueError:  # different drives, or absolute mixed with relative
        return False
    # A path that climbs above the root and comes back ("../x/../static/a")
    # normalizes inside it, but resolving it would still visit the outside
    # directory on the way. Refuse any ".." that rises above the root.
    # A same-drive "C:name" joins under the root, so only its tail counts.
    tail = ntpath.splitdrive(rel)[1] if pathmod is ntpath else rel
    parts = tail.replace("\\", "/").split("/") if pathmod is ntpath else tail.split("/")
    depth = 0
    for part in parts:
        if part in ("", "."):
            continue
        depth += -1 if part == ".." else 1
        if depth < 0:
            return False
    if pathmod is ntpath:
        for part in tail.replace("/", "\\").split("\\"):
            stem = part.split(".", 1)[0].rstrip(" .").upper()
            if ":" in part or stem in _WINDOWS_DEVICE_NAMES:
                return False
    return True


def _serve_static(rel_path: str) -> tuple[int, bytes, str]:
    """Resolve ``/ui/<rel_path>`` under STATIC_DIR with traversal protection."""
    rel = rel_path.strip("/")
    if rel in ("", "ui", "ui/"):
        rel = "index.html"
    elif rel.startswith("ui/"):
        rel = rel[3:]
    # Refuse before touching the filesystem: resolving first would open
    # whatever the join names, which on Windows may be a UNC or pipe path.
    if not _inside_static_root(str(STATIC_DIR), rel):
        return 403, b"forbidden", "text/plain"
    root = STATIC_DIR.resolve()

    def contained(resolved: Path) -> bool:
        return resolved.is_relative_to(root)

    target = (STATIC_DIR / rel).resolve()
    if not contained(target):
        return 403, b"forbidden", "text/plain"
    if target.is_dir():
        # The appended index may itself link outside the folder.
        target = (target / "index.html").resolve()
        if not contained(target):
            return 403, b"forbidden", "text/plain"
    if not target.is_file():
        # SPA fallback: unknown sub-route -> index.html (hash router handles it).
        index = (STATIC_DIR / "index.html").resolve()
        if not contained(index):
            return 403, b"forbidden", "text/plain"
        if index.is_file():
            return 200, index.read_bytes(), "text/html; charset=utf-8"
        return 404, b"not found", "text/plain"
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in (
            "application/javascript", "image/svg+xml", "application/json"):
        ctype += "; charset=utf-8" if "charset" not in ctype else ""
    return 200, target.read_bytes(), ctype


def build_console_app(
    mcp_app: Callable,
    token: str | None,
    health_payload: Callable[[], dict],
    service: Any,
    token_map: dict[str, str] | None = None,
) -> Callable:
    """Return the composed ASGI app. ``mcp_app`` is forwarded for non-console
    paths; ``health_payload`` powers ``/health``; ``service`` backs ``/api``.
    ``token`` is the singular bearer (default principal); ``token_map`` maps
    per-principal tokens (spec 2026-08-10) — either alone closes the gate."""
    from pseudolife_memory import console_sessions, principal_activity
    from pseudolife_memory.principal_store import FailureLimiter, RedemptionGate
    from pseudolife_memory.principals import (
        DEFAULT_PRINCIPAL, SOURCE_ENVIRONMENT, SOURCE_OPEN, SOURCE_SESSION, SOURCE_STORE,
        PrincipalsUnavailable, bearer_presented, environment_principals, installed_store,
        resolve_principal_detailed)

    token = token or None
    token_map = dict(token_map or {})
    auth_configured = token is not None or bool(token_map)
    routes = ConsoleRoutes(service)
    from pseudolife_memory.web.coordination import CoordinationHub
    coordination = CoordinationHub(service)
    pairing_failures = FailureLimiter()
    pairing_gate = RedemptionGate()
    # Sign-in has its own budget and slots, so a burst of bad pairing codes
    # cannot lock the Console's sign-in out, nor the reverse.
    signin_failures = FailureLimiter()
    signin_gate = RedemptionGate()
    passkey_failures = FailureLimiter()
    passkey_gate = RedemptionGate()
    # Issued challenges are capped too, not only failures: each opens a
    # database connection, and a well-formed request needs no secret. A
    # design bound (a person signs in a few times a day), not a measurement.
    passkey_issued = FailureLimiter(limit=30)
    env_issuers = console_sessions.environment_issuers(token_map, token)
    env_names = environment_principals(token_map, token)

    def _resolve_detailed(scope, *, sessions: bool = False
                          ) -> tuple[str | None, str | None, str | None]:
        """``(principal, source, issued_by)``: ``(None, None, None)`` when
        unauthorized; raises ``PrincipalsUnavailable`` when the bearer may
        be a stored one, or a Console session, the daemon cannot check
        right now. ``sessions`` opts this call site into Console sessions
        (schema v59): a hit is ``source`` ``session`` with ``issued_by`` the
        issuing credential's source. The transport gate's one resolution per
        request, so it is also where the outcome is noted for ``invite
        --list`` (v58), a session's under its principal."""
        if not auth_configured:
            return DEFAULT_PRINCIPAL, None, None
        auth = _authorization(scope)
        unavailable = False
        try:
            principal, source = resolve_principal_detailed(auth, token_map, token,
                                                           installed_store())
        except PrincipalsUnavailable:
            principal, source, unavailable = None, None, True
        issued_by = None
        if principal is None and sessions:
            hit = console_sessions.resolve(
                auth, sessions=console_sessions.installed_sessions(), env_issuers=env_issuers,
                env_principals=env_names, store=installed_store())
            if hit is not None:
                principal, source, issued_by = hit.principal, SOURCE_SESSION, hit.issued_by
        if principal is None and unavailable:
            raise PrincipalsUnavailable()
        principal_activity.note(principal_activity.installed_recorder(), auth, principal,
                                issued_by if source == SOURCE_SESSION else source)
        return principal, source, issued_by

    def _operator(source: str | None, issued_by: str | None) -> bool:
        """The operator routes' allowlist (spec 2026-11-02, section 3): an
        environment principal, an open daemon, or a Console session an
        environment principal issued. A paired machine, and any session it
        issued, is refused: a pairing code never grants operator rights."""
        if source in (None, SOURCE_OPEN, SOURCE_ENVIRONMENT):
            return True
        return source == SOURCE_SESSION and issued_by == SOURCE_ENVIRONMENT

    def _authorization(scope) -> str | None:
        """The ``Authorization`` value the resolver judges: the last one
        sent. latin-1 like every other header read here: it cannot fail,
        and resolve_principal compares the bytes the client sent."""
        headers = {k.decode().lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        return headers.get("authorization")

    def _resolve(scope) -> str | None:
        return _resolve_detailed(scope)[0]

    def _token_rejected(scope) -> tuple[str | None, bool]:
        """``(principal, rejected)`` for the session-start hook: ``rejected``
        only when the caller presented a bearer and the daemon matched no
        principal to it. A bearer the daemon cannot check right now (the
        principal store is unavailable) is unauthorized but not rejected:
        it may be a valid stored one."""
        try:
            principal = _resolve(scope)
        except PrincipalsUnavailable:
            return None, False
        return principal, principal is None and bearer_presented(_authorization(scope))

    def _principal(scope) -> str | None:
        """:func:`_resolve` for the always-200 hooks: a principal that
        cannot be checked now is treated as unauthorized."""
        try:
            return _resolve(scope)
        except PrincipalsUnavailable:
            return None

    def _authorized(scope) -> bool:
        return _principal(scope) is not None

    async def _principals_unavailable(send) -> None:
        await _send_json(send, 503, {"error": "principals_unavailable"})

    def _hdr(scope, name: bytes) -> str | None:
        for k, v in scope.get("headers", []):
            if k.lower() == name:
                return v.decode("latin-1")
        return None

    def _host_part(value: str) -> str:
        """Lower-cased host from a Host header or an Origin URL —
        strips scheme, port, and IPv6 brackets."""
        if "://" in value:
            value = urlsplit(value).netloc or value
        value = value.strip().lower()
        if value.startswith("["):
            return value.partition("]")[0].lstrip("[")
        if value.count(":") == 1:
            return value.rsplit(":", 1)[0]
        return value

    def _browser_gate(scope) -> str | None:
        """Tokenless /api is loopback-only (2026-07-02 review H2). Browsers
        always send Host — and Origin on cross-site requests — so a foreign
        Origin is CSRF and a foreign Host is DNS rebinding; both are
        rejected. Non-browser clients (curl, scripts, tests) send neither
        and pass. With a token set, Authorization already proves intent (it
        cannot be attached cross-origin without a failing preflight), so
        remote/LAN hosts stay legitimate."""
        if auth_configured:
            return None
        origin = _hdr(scope, b"origin")
        if origin is not None and _host_part(origin) not in _LOOPBACK:
            return "forbidden_origin"
        host = _hdr(scope, b"host")
        if host is not None and _host_part(host) not in _LOOPBACK:
            return "forbidden_host"
        return None

    async def _pair(scope, receive, send, method: str) -> None:
        """``POST /api/pair``: redeem a pairing code for the SHA-256 of a
        token the client minted. Every refusal is the same ``400
        pairing_refused`` (no oracle for which codes exist) and counts
        against a global budget of failed redemptions; past it, ``429``
        without consulting the store. Nothing from the body or an exception
        is ever logged or returned."""
        if method != "POST":
            await _send_json(send, 405, {"error": "method_not_allowed"})
            return
        # No browser page may reach this: every browser fetch or form post
        # from a page carries Origin.
        if _hdr(scope, b"origin") is not None:
            await _send_json(send, 403, {"error": "forbidden_origin"})
            return
        ctype = (_hdr(scope, b"content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            await _send_json(send, 415, {"error": "content_type_must_be_application_json"})
            return
        # The attempt is counted before anything else can happen to it, so
        # concurrent bad codes cannot all pass a check made before any of
        # them failed; a success gives it back.
        attempt = pairing_failures.reserve()
        if attempt is None:
            await _send_json(send, 429, {"error": "rate_limited"})
            return

        async def refused() -> None:
            await _send_json(send, 400, {"error": "pairing_refused"})

        try:
            raw = await _read_body(receive, max_bytes=_PAIR_BODY_LIMIT)
        except ValueError:
            await _send_json(send, 413, {"error": "request_too_large"})
            return
        from pseudolife_memory import principal_store
        from pseudolife_memory.principals import (
            is_sha256_hex, normalize_pairing_code, secret_sha256)
        store = installed_store()
        dsn = getattr(service, "_db_url", None)
        try:
            # Any parse or validation failure, including a nesting deep
            # enough for RecursionError, is the same refusal.
            body = json.loads(raw.decode("utf-8"))
            if not (isinstance(body, dict) and set(body) == {"code", "token_sha256"}):
                raise ValueError
            code = normalize_pairing_code(body["code"])
            token_hash = body["token_sha256"]
            if code is None or not is_sha256_hex(token_hash):
                raise ValueError
        except Exception:  # noqa: BLE001 - never echoed, never logged
            await refused()
            return
        if not auth_configured or store is None or not dsn:
            await refused()
            return
        if not pairing_gate.try_enter():
            # Never reached the store: not a failed redemption.
            pairing_failures.refund(attempt)
            await _send_json(send, 429, {"error": "rate_limited"})
            return
        try:
            paired = await asyncio.get_running_loop().run_in_executor(
                pairing_gate.executor,
                functools.partial(principal_store.redeem, dsn, secret_sha256(code), token_hash,
                                  excluded=store.excluded_names))
        except Exception as exc:  # noqa: BLE001 - the outcome is unknown; the type only
            logger.error("pairing redemption failed (%s)", type(exc).__name__)
            await _send_json(send, 503, {"error": "pairing_unavailable"})
            return
        finally:
            pairing_gate.leave()
        if paired is None:
            await refused()
            return
        pairing_failures.refund(attempt)
        # Usable at once: the snapshot keeps this entry until a refresh
        # that started after it, and drops the principal's previous hash.
        store.add(paired)
        await _send_json(send, 200, {"principal": paired.principal, "tier": paired.tier,
                                     "bank": store.bank})

    def _issuing_hash(scope, principal: str, source: str | None) -> str | None:
        """The SHA-256 of the bearer that resolved to ``principal`` from
        ``source``: a link session's issuer, which must keep naming the
        same principal for the session to resolve."""
        for token_hash in console_sessions.bearer_hashes(_authorization(scope)):
            if source == SOURCE_ENVIRONMENT and env_issuers.get(token_hash) == principal:
                return token_hash
            if source == SOURCE_STORE:
                store = installed_store()
                row = store.lookup(token_hash) if store is not None else None
                if row is not None and row.principal == principal:
                    return token_hash
        return None

    def _issuer_still_valid(principal: str, source: str, issuer: str) -> bool:
        """At redemption: the credential that asked for the code still
        resolves to the same principal (a stale stored view refuses)."""
        if source == SOURCE_ENVIRONMENT:
            return env_issuers.get(issuer) == principal
        if source == SOURCE_STORE:
            store = installed_store()
            if store is None:
                return False
            if not store.available():
                # Unknown, not refused: the caller answers 503.
                raise PrincipalsUnavailable()
            row = store.lookup(issuer)
            return row is not None and row.principal == principal
        return False

    async def _signin(scope, receive, send, method: str) -> None:
        """``POST /api/console/session``: redeem a sign-in code for the
        SHA-256 of a token the browser minted (spec 2026-11-02, section 4).
        Browser-called, so the request's ``Origin`` must equal the origin
        the code was issued for; a JSON body only (a preflight, and no CORS
        headers are served); exactly ``{code, token_sha256, label}`` in 1
        KiB. Every bad code is the same ``400 signin_refused`` and counts
        against sign-in's own failure budget; nothing from the body or an
        exception is logged or returned."""
        if method != "POST":
            await _send_json(send, 405, {"error": "method_not_allowed"})
            return
        origin = console_sessions.normalize_origin(_hdr(scope, b"origin"))
        if origin is None:
            await _send_json(send, 403, {"error": "forbidden_origin"})
            return
        ctype = (_hdr(scope, b"content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            await _send_json(send, 415, {"error": "content_type_must_be_application_json"})
            return
        attempt = signin_failures.reserve()
        if attempt is None:
            await _send_json(send, 429, {"error": "rate_limited"})
            return

        async def refused() -> None:
            await _send_json(send, 400, {"error": "signin_refused"})

        try:
            raw = await _read_body(receive, max_bytes=_PASSKEY_SIGNIN_BODY_LIMIT)
        except ValueError:
            await _send_json(send, 413, {"error": "request_too_large"})
            return
        from pseudolife_memory.principals import (
            is_sha256_hex, normalize_pairing_code, secret_sha256)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - never echoed, never logged
            body = None
        if isinstance(body, dict) and set(body) == {"payload", "mac", "assertion"}:
            await _passkey_signin(send, origin, body, attempt)
            return
        if len(raw) > _SIGNIN_BODY_LIMIT:
            await _send_json(send, 413, {"error": "request_too_large"})
            return
        try:
            if not (isinstance(body, dict) and set(body) == {"code", "token_sha256", "label"}):
                raise ValueError
            code = normalize_pairing_code(body["code"])
            token_hash = body["token_sha256"]
            label = console_sessions.clean_label(body["label"])
            if code is None or not is_sha256_hex(token_hash) or label is None:
                raise ValueError
        except Exception:  # noqa: BLE001 - never echoed, never logged
            await refused()
            return
        sessions = console_sessions.installed_sessions()
        dsn = getattr(service, "_db_url", None)
        if not auth_configured or sessions is None or not dsn:
            await refused()
            return
        if not signin_gate.try_enter():
            signin_failures.refund(attempt)
            await _send_json(send, 429, {"error": "rate_limited"})
            return
        try:
            row = await asyncio.get_running_loop().run_in_executor(
                signin_gate.executor,
                functools.partial(console_sessions.redeem_code, dsn, secret_sha256(code),
                                  token_hash, origin=console_sessions.origin_key(origin),
                                  label=label, still_valid=_issuer_still_valid))
        except PrincipalsUnavailable:
            # The issuing token could not be checked: nothing was spent.
            signin_failures.refund(attempt)
            await _send_json(send, 503, {"error": "signin_unavailable"})
            return
        except Exception as exc:  # noqa: BLE001 - the outcome is unknown; the type only
            logger.error("console sign-in failed (%s)", type(exc).__name__)
            await _send_json(send, 503, {"error": "signin_unavailable"})
            return
        finally:
            signin_gate.leave()
        if row is None:
            await refused()
            return
        signin_failures.refund(attempt)
        sessions.add(row)
        await _send_json(send, 200, {"principal": row.principal, "session_id": row.session_id,
                                     "expires_at": row.expires_at})

    def _dead_session_ids(sessions) -> set[str]:
        """The ids of the sessions in the daemon's view that no longer
        resolve; none when that cannot be told now (a stale view)."""
        if not sessions.available():
            return set()
        import time as _time
        now = _time.time()
        try:
            return {row.session_id for row in sessions.rows()
                    if not console_sessions.still_resolves(
                        row, sessions=sessions, env_issuers=env_issuers,
                        env_principals=env_names, store=installed_store(), now=now)}
        except PrincipalsUnavailable:
            return set()

    def _principal_source(name: str) -> str | None:
        """Where ``name`` resolves on its own now: the environment, the
        store, or nowhere (``None``)."""
        if name in env_names:
            return SOURCE_ENVIRONMENT
        store = installed_store()
        if store is not None:
            if not store.available():
                raise PrincipalsUnavailable()
            if store.resolves(name):
                return SOURCE_STORE
        return None

    def _passkey_origin() -> str | None:
        """The passkey origin in its stored form, when passkeys are set up."""
        m = service.config.coordination.maintainer
        if not m.configured:
            return None
        normal = console_sessions.normalize_origin(m.origin)
        return console_sessions.origin_key(normal) if normal else None

    async def _passkey_signin(send, origin: str, body: dict, attempt) -> None:
        """The passkey half of ``POST /api/console/session`` (spec 2026-11-02,
        section 5): at the passkey origin only; the maintainer store's
        ``complete()`` verifies the assertion and the session is created in
        its transaction. Every bad assertion is the same ``signin_refused``."""
        from pseudolife_memory import console_passkey
        from pseudolife_memory.storage.maintainer import MaintainerError
        sessions = console_sessions.installed_sessions()
        passkey_origin = _passkey_origin()
        if (not auth_configured or sessions is None or not getattr(service, "_db_url", None)
                or passkey_origin is None
                or console_sessions.origin_key(origin) != passkey_origin):
            await _send_json(send, 400, {"error": "signin_refused"})
            return
        if not signin_gate.try_enter():
            signin_failures.refund(attempt)
            await _send_json(send, 429, {"error": "rate_limited"})
            return
        try:
            row = await asyncio.get_running_loop().run_in_executor(
                signin_gate.executor, functools.partial(
                    console_passkey.complete_signin, service, body,
                    principal_source=_principal_source, publish=sessions.add))
        except PrincipalsUnavailable:
            signin_failures.refund(attempt)
            await _send_json(send, 503, {"error": "signin_unavailable"})
            return
        except MaintainerError as exc:
            if exc.code == "passkey_signin_unavailable":
                signin_failures.refund(attempt)
                await _send_json(send, 409, {"error": exc.code,
                                             "hint": console_passkey.UNAVAILABLE_HINT})
            elif exc.code in ("coordination_unavailable", "principals_unavailable"):
                signin_failures.refund(attempt)
                await _send_json(send, 503, {"error": "signin_unavailable"})
            else:
                await _send_json(send, 400, {"error": "signin_refused"})
            return
        except Exception as exc:  # noqa: BLE001 - the outcome is unknown; the type only
            logger.error("console passkey sign-in failed (%s)", type(exc).__name__)
            await _send_json(send, 503, {"error": "signin_unavailable"})
            return
        finally:
            signin_gate.leave()
        signin_failures.refund(attempt)
        # Published by complete_signin under the coordination lock.
        await _send_json(send, 200, {"principal": row.principal, "session_id": row.session_id,
                                     "expires_at": row.expires_at})

    async def _passkey_challenge(scope, receive, send, method: str) -> None:
        """``POST /api/console/passkey-challenge`` (pre-auth, spec 2026-11-02
        section 5): a ``sign-in`` challenge for the browser's token hash,
        at the passkey origin only, read on its own connection; never
        creates the challenge secret. Its own failure budget and slots."""
        if method != "POST":
            await _send_json(send, 405, {"error": "method_not_allowed"})
            return
        origin = console_sessions.normalize_origin(_hdr(scope, b"origin"))
        if origin is None:
            await _send_json(send, 403, {"error": "forbidden_origin"})
            return
        ctype = (_hdr(scope, b"content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            await _send_json(send, 415, {"error": "content_type_must_be_application_json"})
            return
        attempt = passkey_failures.reserve()
        if attempt is None or passkey_issued.reserve() is None:
            await _send_json(send, 429, {"error": "rate_limited"})
            return
        try:
            raw = await _read_body(receive, max_bytes=_SIGNIN_BODY_LIMIT)
        except ValueError:
            await _send_json(send, 413, {"error": "request_too_large"})
            return
        from pseudolife_memory.principals import is_sha256_hex
        try:
            body = json.loads(raw.decode("utf-8"))
            if not (isinstance(body, dict) and set(body) == {"token_sha256", "label"}):
                raise ValueError
            token_hash = body["token_sha256"]
            label = console_sessions.clean_label(body["label"])
            if not is_sha256_hex(token_hash) or label is None:
                raise ValueError
        except Exception:  # noqa: BLE001 - never echoed, never logged
            await _send_json(send, 400, {"error": "invalid_request"})
            return
        from pseudolife_memory import console_passkey
        passkey_origin = _passkey_origin()
        dsn = getattr(service, "_db_url", None)
        if (not auth_configured or passkey_origin is None or not dsn
                or console_sessions.installed_sessions() is None):
            passkey_failures.refund(attempt)
            await _send_json(send, 409, {"error": "passkey_signin_unavailable",
                                         "hint": console_passkey.NO_KEYS_HINT})
            return
        if console_sessions.origin_key(origin) != passkey_origin:
            await _send_json(send, 403, {"error": "forbidden_origin"})
            return
        if not passkey_gate.try_enter():
            passkey_failures.refund(attempt)
            await _send_json(send, 429, {"error": "rate_limited"})
            return
        try:
            issued = await asyncio.get_running_loop().run_in_executor(
                passkey_gate.executor, functools.partial(
                    console_passkey.issue_signin_challenge, dsn, token_hash=token_hash,
                    label=label, rp_id=service.config.coordination.maintainer.rp_id))
        except Exception as exc:  # noqa: BLE001 - the type only
            logger.error("console passkey challenge failed (%s)", type(exc).__name__)
            await _send_json(send, 503, {"error": "signin_unavailable"})
            return
        finally:
            passkey_gate.leave()
        passkey_failures.refund(attempt)
        if "error" in issued:
            await _send_json(send, 409, {
                "error": "passkey_signin_unavailable",
                "hint": (console_passkey.UNAVAILABLE_HINT if issued["error"] == "unbound"
                         else console_passkey.NO_KEYS_HINT)})
            return
        await _send_json(send, 200, issued)

    async def _passkey_bind(scope, send, body: dict, principal: str) -> None:
        """``POST /api/console/passkey-bind``: "Use this passkey to sign in"
        (purpose ``bind-sign-in``), through the maintainer routes' gate and
        lock, for an owner principal only. On success the key's passkey
        sessions under any other principal are signed out at once."""
        from pseudolife_memory import console_passkey
        from pseudolife_memory.principals import MAINTAINER_PRINCIPAL, owner_principals
        from pseudolife_memory.writer_context import bind_request_headers, unbind_request_headers
        cfg = service.config.coordination

        def is_owner(name):
            return name != MAINTAINER_PRINCIPAL and name in owner_principals(
                cfg, token_map=token_map, single_token=token)

        def run():
            headers = {k.decode().lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            binding = bind_request_headers(headers, principal=principal)
            try:
                return console_passkey.bind(service, body, principal, is_owner=is_owner)
            finally:
                unbind_request_headers(binding)
        try:
            result = await asyncio.get_running_loop().run_in_executor(None, run)
        except Exception as exc:  # noqa: BLE001 - mapped to a public code, never echoed
            await _send_json(send, *_maintainer_error(exc))
            return
        # The key's other sessions left the view under the coordination
        # lock (console_passkey._bind_complete).
        await _send_json(send, 200, result)

    async def _console_route(scope, send, method: str, path: str, body: dict,
                             principal: str, source: str | None,
                             issued_by: str | None) -> None:
        """The bearer-gated Console sign-in routes (spec 2026-11-02):
        ``POST /api/console/link`` asks for a sign-in code, ``GET
        /api/console/sessions`` lists signed-in browsers and ``POST
        /api/console/sessions/revoke`` ends one. An operator (see
        :func:`_operator`) sees and revokes every session; anyone else only
        their own principal's."""
        loop = asyncio.get_running_loop()
        dsn = getattr(service, "_db_url", None)
        sessions = console_sessions.installed_sessions()
        operator = _operator(source, issued_by)
        routes_here = {"/api/console/link": "POST", "/api/console/sessions": "GET",
                       "/api/console/sessions/revoke": "POST",
                       "/api/console/passkey-bind": "POST"}
        if path not in routes_here:
            await _send_json(send, 404, {"error": "not_found", "path": path})
            return
        if method != routes_here[path]:
            await _send_json(send, 405, {"error": "method_not_allowed", "path": path})
            return
        unavailable = None
        if not auth_configured:
            unavailable = ("this daemon has no token configured, so the Console needs no "
                           "sign-in: open it on this machine")
        elif sessions is None or not dsn:
            unavailable = "Console sign-in needs the daemon's Postgres bank"
        if path == "/api/console/sessions":
            if unavailable:
                await _send_json(send, 200, {"sessions": [], "operator": operator,
                                             "available": False})
                return
            current = None
            if source == SOURCE_SESSION:
                for token_hash in console_sessions.bearer_hashes(_authorization(scope)):
                    hit = sessions.lookup(token_hash)
                    if hit is not None:
                        current = hit.session_id
                        break
            try:
                rows = await loop.run_in_executor(None, functools.partial(
                    console_sessions.list_sessions, dsn,
                    principal=None if operator else principal))
            except Exception as exc:  # noqa: BLE001 - the type only
                logger.error("console sessions: list failed (%s)", type(exc).__name__)
                await _send_json(send, 503, {"error": "signin_unavailable",
                                             "hint": _BANK_UNREACHABLE})
                return
            rows = console_sessions.with_last_use(rows, sessions)
            # A session whose issuer or principal is gone no longer signs
            # anything in, though its row has not been revoked yet.
            dead = _dead_session_ids(sessions)
            rows = [r for r in rows if r["session_id"] not in dead]
            await _send_json(send, 200, {
                "sessions": [{**r, "current": r["session_id"] == current} for r in rows],
                "operator": operator, "available": True})
            return
        if unavailable:
            await _send_json(send, 409, {"error": "signin_unavailable", "hint": unavailable})
            return
        if path == "/api/console/passkey-bind":
            await _passkey_bind(scope, send, body, principal)
            return
        if path == "/api/console/sessions/revoke":
            session_id = body.get("session_id")
            if set(body) != {"session_id"} or not isinstance(session_id, str) or not (
                    0 < len(session_id) <= 64):
                await _send_json(send, 400, {"error": "invalid_request",
                                             "hint": "send {\"session_id\": \"<id>\"}"})
                return
            try:
                revoked = await loop.run_in_executor(None, functools.partial(
                    console_sessions.revoke_session, dsn, session_id,
                    principal=None if operator else principal))
            except Exception as exc:  # noqa: BLE001 - the type only
                logger.error("console sessions: revoke failed (%s)", type(exc).__name__)
                await _send_json(send, 503, {"error": "signin_unavailable",
                                             "hint": _BANK_UNREACHABLE})
                return
            if revoked:
                sessions.drop([session_id])
            await _send_json(send, 200, {"revoked": bool(revoked)})
            return
        # POST /api/console/link
        if not set(body) <= {"origin", "to"}:
            await _send_json(send, 400, {"error": "invalid_request",
                                         "hint": "send {\"origin\": \"<daemon origin>\"} and "
                                                 "optionally \"to\": \"<Console page>\""})
            return
        origin = console_sessions.normalize_origin(body.get("origin"))
        if origin is None:
            await _send_json(send, 400, {
                "error": "invalid_origin",
                "hint": "origin is the daemon address the client reached: "
                        "http(s)://host[:port], with no path"})
            return
        to = body.get("to")
        if to is not None and not console_sessions.valid_route_id(to):
            await _send_json(send, 400, {"error": "invalid_route",
                                         "hint": "to is a Console page id, such as board"})
            return
        maintainer = service.config.coordination.maintainer
        if maintainer.configured:
            # Passkeys set up: the Console's one HTTPS address, wherever the
            # client reached the daemon.
            stored = console_sessions.normalize_origin(maintainer.origin)
        elif console_sessions.is_loopback_origin(origin):
            stored = origin
        else:
            await _send_json(send, 409, {
                "error": "signin_origin_unsupported",
                "hint": f"without passkeys, a sign-in link opens the Console only on the "
                        f"daemon's own machine, and {origin} is not a local address: on the "
                        f"daemon's host run `pseudolife-mcp console` with "
                        f"PSEUDOLIFE_MCP_DAEMON_URL set to its local address (such as "
                        f"http://127.0.0.1:8765), or give the Console an HTTPS address with "
                        f"`pseudolife-mcp maintainer setup` there"})
            return
        issuer = _issuing_hash(scope, principal, source)
        if issuer is None:
            await _send_json(send, 403, {"error": "session_cannot_issue",
                                         "hint": "a sign-in link needs a client's own token"})
            return
        try:
            issued = await loop.run_in_executor(None, functools.partial(
                console_sessions.issue_code, dsn, principal=principal, source=source,
                issuer=issuer, origin=console_sessions.origin_key(stored)))
        except Exception as exc:  # noqa: BLE001 - the type only
            logger.error("console sign-in link failed (%s)", type(exc).__name__)
            await _send_json(send, 503, {"error": "signin_unavailable",
                                         "hint": _BANK_UNREACHABLE})
            return
        from pseudolife_memory.principals import format_pairing_code
        shown = format_pairing_code(issued["code"])
        url = f"{stored}/ui/#/signin?code={shown}" + (f"&to={to}" if to else "")
        await _send_json(send, 200, {"code": shown, "url": url, "origin": stored,
                                     "principal": principal,
                                     "expires_at": issued["expires_at"],
                                     "expires_in": console_sessions.CODE_TTL_S})

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await mcp_app(scope, receive, send)
            return

        path = scope.get("path", "") or "/"
        method = scope.get("method", "GET").upper()

        # 1) open liveness probe. A degraded payload (e.g. DB unreachable)
        # surfaces as 503 so orchestration/healthchecks can actually see it.
        # Off the event loop like every other blocking handler: the payload
        # includes a blocking Postgres ping, and inline it would freeze the
        # whole web surface for the ping's timeout on a DB stall
        # (2026-09-01 review).
        if path == "/health":
            try:
                payload = await asyncio.get_running_loop().run_in_executor(
                    None, health_payload)
                ok = payload.get("status", "ok") == "ok"
                await _send_json(send, 200 if ok else 503, payload)
            except Exception as exc:  # noqa: BLE001
                await _send_json(send, 500, {"status": "error", "error": str(exc)})
            return

        # 2) root -> console
        if path == "/":
            await send({"type": "http.response.start", "status": 307,
                        "headers": [(b"location", b"/ui/"), *CONSOLE_SECURITY_HEADERS]})
            await send({"type": "http.response.body", "body": b""})
            return

        # 3) static SPA shell (open)
        if path == "/ui" or path.startswith("/ui/"):
            try:
                status, body, ctype = await asyncio.get_running_loop().run_in_executor(
                    None, _serve_static, path)
                # Fonts/images are immutable; markup, styles and scripts must
                # never be cached so updates are picked up without a hard reload.
                cache = ("max-age=86400" if ctype.startswith(("font/", "image/"))
                         else "no-store")
                await _send_bytes(send, status, body, ctype, cache,
                                  extra_headers=CONSOLE_SECURITY_HEADERS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("static serve error for %s: %s", path, exc)
                await _send_bytes(send, 500, b"static error", "text/plain",
                                  extra_headers=CONSOLE_SECURITY_HEADERS)
            return

        # 4) plugin SessionStart hook context (plain text, 200 always; also
        # read by `pseudolife-mcp briefing --hook-json`). The
        # instructions half is public repo content, so an unauthorized
        # request (token set, no bearer) still gets it — but never the
        # briefing, which is memory content. Loopback gating as for /api.
        # An optional ?session_id= (+ ?source=) registers the session's
        # episode and active-session pointer (identity tier 3) and prepends
        # the episode-handle advertisement line — but only when authorized:
        # unauthorized-with-token callers must not be able to mint/hijack the
        # active-session pointer just by hitting this endpoint, so session_id
        # and source are dropped entirely (byte-identical to the plain
        # instructions-only response) rather than merely gated on output.
        # A caller whose bearer was rejected gets that answer after one
        # notice saying so: on 2026-10-30 two machines with rotated tokens
        # had run 11 days without memory while their sessions looked normal.
        if path == "/api/hook/session-start":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "GET":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            principal, token_rejected = _token_rejected(scope)
            authorized = principal is not None
            params = _parse_query(scope)
            session_id = params.get("session_id") if authorized else None
            source = params.get("source") if authorized else None
            # The version comparison touches no memory content, so an
            # unauthorized hook still learns it is out of date; the value
            # is shape-checked before it can reach the model's context.
            plugin_version = params.get("plugin_version")
            plugin_hooks_digest = params.get("plugin_hooks_digest")
            launcher = params.get("launcher")   # shape-checked by session_hook.launcher_command

            def start_hook():
                # Bind the request headers so the briefing's awareness
                # section can resolve the caller's principal the same way
                # /api/agents and /api/briefing do.
                from pseudolife_memory.writer_context import (
                    bind_request_headers, unbind_request_headers)
                headers = {k.decode().lower(): v.decode("latin-1")
                           for k, v in scope.get("headers", [])}
                binding = bind_request_headers(headers, principal=principal)
                try:
                    return hook_session_start(service, session_id, source, authorized,
                                              plugin_version=plugin_version,
                                              plugin_hooks_digest=plugin_hooks_digest,
                                              launcher=launcher,
                                              host=headers.get("host"),
                                              token_rejected=token_rejected)
                finally:
                    unbind_request_headers(binding)
            text = await asyncio.get_running_loop().run_in_executor(None, start_hook)
            await _send_bytes(send, 200, text.encode("utf-8"),
                              "text/plain; charset=utf-8", "no-store")
            return

        # 4a) plugin memory-policy SessionStart hook: the full memory-loop
        # block when the session's configured variant is full_separate_hook,
        # else an empty body (the hook then adds nothing). Its own hook
        # output, so the block never shares the briefing's budget. Public
        # repo text, 200 always; session_id only picks an online A/B arm,
        # source=resume|compact serves nothing, and both are honoured only
        # when authorized, as on session-start.
        if path == "/api/hook/memory-policy":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "GET":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            params = _parse_query(scope) if _authorized(scope) else {}
            text = await asyncio.get_running_loop().run_in_executor(
                None, hook_memory_policy, service, params.get("session_id"),
                params.get("source"))
            await _send_bytes(send, 200, text.encode("utf-8"),
                              "text/plain; charset=utf-8", "no-store")
            return

        # 4a') plugin UserPromptSubmit hook: the per-turn memory-change note.
        # Line 1 is the next cursor, then a note only when memory changed
        # since the hook's ?since= (see hook_memory_changes). Status notes
        # and lessons are memory content, so an unauthorized caller gets an
        # empty body: the hook then prints nothing and keeps its cursor.
        # 200 always.
        if path == "/api/hook/memory-changes":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "GET":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            text = ""
            if _authorized(scope):
                params = _parse_query(scope)
                text = await asyncio.get_running_loop().run_in_executor(
                    None, hook_memory_changes, service, params.get("session_id"),
                    params.get("since"))
                # The maintainer's conductor's one-line reminder (spec
                # 2026-10-14), after the cursor and any note: only beside a
                # cursor, since an empty answer must stay empty for the hook.
                # The lookup waits at most CONDUCTOR_LOOKUP_BUDGET.
                if text and params.get("agent"):
                    from pseudolife_memory.coordination import conductor_reminder
                    headers = {k.decode().lower(): v.decode("latin-1")
                               for k, v in scope.get("headers", [])}
                    text += await conductor_reminder(service, headers,
                                                    agent=params.get("agent"),
                                                    token_map=token_map, token=token)
            await _send_bytes(send, 200, text.encode("utf-8"),
                              "text/plain; charset=utf-8", "no-store")
            return

        # 4b) plugin SessionEnd hook: closes the session's episode and clears
        # the active-session pointer (only if still owned). Fail-open, always
        # 200 — mirrors session-start's contract. Unlike session-start there
        # is no unauthorized-safe subset of this call (it's pure mutation),
        # so a configured token is enforced outright, same shape as /api's
        # 401.
        if path == "/api/hook/session-end":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "POST":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            try:
                ended_by = _resolve(scope)
            except PrincipalsUnavailable:
                await _principals_unavailable(send)
                return
            if ended_by is None:
                await _send_json(send, 401, {
                    "error": "unauthorized",
                    "hint": "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"})
                return
            try:
                raw = await _read_body(receive, max_bytes=_SESSION_END_BODY_LIMIT)
            except ValueError:
                await _send_json(send, 413, {"error": "request_too_large"})
                return
            body: dict = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    body = {}
            if not isinstance(body, dict):
                body = {}
            result = await asyncio.get_running_loop().run_in_executor(
                None, hook_session_end, service, body.get("session_id"))
            await _send_json(send, 200, result)
            return

        # 4c) plugin coordination SessionStart hook: the board check-in as
        # plain text, or an empty body when this bearer cannot use the board
        # (disabled, no or unknown bearer, unlisted principal, file mode).
        # 200 always, like session-start; the check touches no storage (only
        # the conductor lookup below does, and only given ?agent=).
        if path == "/api/hook/coordination-start":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "GET":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            from pseudolife_memory.coordination import CHECKIN_TEXT, unavailable_reason
            headers = {k.decode().lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            reason = unavailable_reason(service, headers, token_map=token_map, token=token)
            text = "" if reason else CHECKIN_TEXT + "\n"
            # ?agent=<id> (the session's board address, spec 2026-10-14):
            # the maintainer's conductor gets its brief as a block after the
            # unchanged check-in. The lookup is the one storage read here, and
            # waits at most CONDUCTOR_LOOKUP_BUDGET.
            agent = _parse_query(scope).get("agent") if not reason else None
            if agent:
                from pseudolife_memory.coordination import conductor_brief
                text += await conductor_brief(service, headers, agent=agent,
                                             token_map=token_map, token=token)
            # X-PL-Board names why the board is off, so the installer and
            # doctor report the daemon's reason instead of guessing one.
            # The codes reveal no more than /api's own 401 already does.
            state = f"off; reason={reason}" if reason else "on"
            await _send_bytes(send, 200, text.encode("utf-8"),
                              "text/plain; charset=utf-8", "no-store",
                              [(b"x-pl-board", state.encode("ascii"))])
            return

        # 4d) plugin Stop hook: the park gate (v49). "allow" or "block" plus
        # the message, for the address the hook names; an empty body for a
        # bearer that cannot use the board, which the hook reads as allow.
        # 200 always; the daemon call is the hook's one request.
        if path == "/api/hook/park-gate":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "GET":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            text = ""
            if _authorized(scope):
                from pseudolife_memory.coordination import park_gate
                headers = {k.decode().lower(): v.decode("latin-1")
                           for k, v in scope.get("headers", [])}
                params = _parse_query(scope)
                text = await asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(park_gate, service, headers,
                                            agent=params.get("agent"), since=params.get("since"),
                                            token_map=token_map, token=token))
            await _send_bytes(send, 200, text.encode("utf-8"),
                              "text/plain; charset=utf-8", "no-store")
            return

        # 4e) plugin Stop hook: the woke marker. After a wake fires the hook
        # posts that the address's turn is starting; the daemon logs a
        # ``woke`` audit event for it. A write, so POST only; "ok" when
        # recorded, an empty body otherwise. 200 always: the hook does not
        # read the answer.
        if path == "/api/hook/woke":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "POST":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            text = ""
            if _authorized(scope):
                from pseudolife_memory.coordination import woke
                headers = {k.decode().lower(): v.decode("latin-1")
                           for k, v in scope.get("headers", [])}
                params = _parse_query(scope)
                text = await asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(woke, service, headers, agent=params.get("agent"),
                                            token_map=token_map, token=token))
            await _send_bytes(send, 200, text.encode("utf-8"),
                              "text/plain; charset=utf-8", "no-store")
            return

        # 4f) plugin SubagentStart / SubagentStop hooks (v50): list or unlist
        # a Claude Code subagent among its session's children. A write, so
        # POST only; "ok" when recorded, an empty body otherwise. 200
        # always: the hook does not read the answer.
        if path == "/api/hook/subagent":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "POST":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            text = ""
            if _authorized(scope):
                from pseudolife_memory.coordination import subagent
                headers = {k.decode().lower(): v.decode("latin-1")
                           for k, v in scope.get("headers", [])}
                params = _parse_query(scope)
                text = await asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(subagent, service, headers, agent=params.get("agent"),
                                            event=params.get("event"), child=params.get("child"),
                                            kind=params.get("type"),
                                            token_map=token_map, token=token))
            await _send_bytes(send, 200, text.encode("utf-8"),
                              "text/plain; charset=utf-8", "no-store")
            return

        # 4g) pairing-code redemption (`pseudolife-mcp pair`, spec
        # 2026-10-02): unauthenticated by design, so it is routed before the
        # bearer gate and refuses everything a browser could send.
        if path == "/api/pair":
            await _pair(scope, receive, send, method)
            return

        # 4h) Console sign-in-code redemption (spec 2026-11-02, section 4):
        # the second unauthenticated route, browser-called, so it is routed
        # before the bearer gate and checks the Origin against the code's.
        if path == "/api/console/session":
            await _signin(scope, receive, send, method)
            return
        if path == "/api/console/passkey-challenge":
            await _passkey_challenge(scope, receive, send, method)
            return

        # 5) console REST API (token-gated like /mcp)
        if path.startswith("/api/") or path == "/api":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {
                    "error": denied,
                    "hint": "tokenless /api serves loopback browsers only; "
                            "set PSEUDOLIFE_MCP_TOKEN for remote access"})
                return
            # /api/console/link resolves a session only to refuse it by name
            # (a session cannot issue a session), rather than a bare 401.
            session_site = path == "/api/console/link" or not (
                path.startswith("/api/coordination/") or path in _SESSIONLESS_API_PATHS)
            try:
                principal, source, issued_by = _resolve_detailed(scope, sessions=session_site)
                if path == "/api/console/link" and source == SOURCE_SESSION:
                    await _send_json(send, 403, {
                        "error": "session_cannot_issue",
                        "hint": "a sign-in link needs a client's own token: run "
                                "`pseudolife-mcp console` on a machine that uses Pseudolife"})
                    return
            except PrincipalsUnavailable:
                await _principals_unavailable(send)
                return
            if principal is None:
                await _send_json(send, 401, {
                    "error": "unauthorized",
                    "hint": "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"})
                return
            if method not in ("GET", "POST"):
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            # An invited machine reads and writes the bank and the board, but
            # does not reconfigure the daemon or speak as it; nor does a
            # Console session it signed in (an allowlist, spec 2026-11-02).
            if (method == "POST" and path in _OPERATOR_POST_PATHS
                    and not _operator(source, issued_by)):
                # The body stays the bare code: the Rust daemon's goldens pin
                # it, and the Console's Settings view explains it.
                await _send_json(send, 403, {"error": "operator_principal_required"})
                return
            params = _parse_query(scope)
            body: dict = {}
            coordination_path = path.startswith("/api/coordination/")
            coordination_errors = coordination_path or (
                path == "/api/agents" and params.get("view") == "coordination")
            maintainer_path = _is_maintainer_path(path)
            if coordination_path and method != "POST":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            if method == "POST":
                try:
                    raw = await _read_body(receive, max_bytes=_body_limit(path))
                except ValueError:
                    await _send_json(send, 413, {"error": "request_too_large"})
                    return
                if raw:
                    # A cross-site form/fetch can send text/plain or
                    # urlencoded WITHOUT a CORS preflight; application/json
                    # cannot. 415 here forces the preflight (which fails —
                    # no CORS headers are served). Bodyless POSTs are
                    # covered by the Origin gate above.
                    ctype = (_hdr(scope, b"content-type") or "")
                    if ctype.split(";")[0].strip().lower() != "application/json":
                        await _send_json(send, 415, {
                            "error": "content_type_must_be_application_json"})
                        return
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except UnicodeDecodeError:
                        if not coordination_path:
                            raise
                        await _send_json(send, 400, {"error": "invalid_json"})
                        return
                    except json.JSONDecodeError:
                        await _send_json(send, 400, {"error": "invalid_json"})
                        return
                    if not isinstance(body, dict):
                        await _send_json(send, 400, {"error": "body_must_be_object"})
                        return
            if path.startswith(_CONSOLE_PREFIX):
                await _console_route(scope, send, method, path, body, principal, source,
                                     issued_by)
                return
            try:
                if coordination_path:
                    from pseudolife_memory.coordination import authenticated_principal
                    headers = {k.decode().lower(): v.decode("latin-1")
                               for k, v in scope.get("headers", [])}
                    principal = authenticated_principal(headers, token_map=token_map, token=token)
                    action = path.removeprefix("/api/coordination/")
                    wait = body.get("wait_seconds", 0)
                    operation = coordination.handle(action, body, headers, principal)
                    if action == "receive" and type(wait) in (int, float) and wait > 0:
                        connected, result = await _wait_while_connected(operation, receive)
                        if not connected:
                            return
                    else:
                        result = await operation
                    await _send_json(send, 200, result)
                    return
                def dispatch():
                    # run_in_executor does not propagate contextvars: bind in
                    # the worker for every route and always restore its context.
                    from pseudolife_memory.writer_context import (
                        bind_request_headers, unbind_request_headers)
                    headers = {k.decode().lower(): v.decode("latin-1")
                               for k, v in scope.get("headers", [])}
                    if (coordination_errors or maintainer_path) and source != SOURCE_SESSION:
                        # The board requires a configured bearer even on an
                        # otherwise open loopback Console installation, and
                        # so do the maintainer routes (v54): a tokenless
                        # daemon refuses them outright. A Console session
                        # exists only where a token is configured, and the
                        # gate above already resolved it.
                        from pseudolife_memory.coordination import authenticated_principal
                        authenticated_principal(headers, token_map=token_map, token=token)
                    # Bind the transport-validated principal, restoring the
                    # prior worker context after every handler result.
                    binding = bind_request_headers(headers, principal=principal)
                    try:
                        return routes.dispatch(method, path, params, body)
                    finally:
                        unbind_request_headers(binding)
                result = await asyncio.get_running_loop().run_in_executor(
                    None, dispatch)
                await _send_json(send, 200, result)
            except KeyError as exc:
                if coordination_path:
                    await _send_coordination_error(send, exc)
                    return
                if maintainer_path and routes.has(path):
                    # A KeyError out of a known maintainer route is a bug or
                    # a missing body field; never echo it.
                    logger.error("maintainer route failed (KeyError)")
                    await _send_json(send, 400, {"error": "invalid_request"})
                    return
                # unknown path, or a wrong-verb hit on a known path
                status = 405 if routes.has(path) else 404
                await _send_json(send, status, {
                    "error": "not_found" if status == 404 else "method_not_allowed",
                    "path": path})
            except ValueError as exc:
                if maintainer_path:
                    await _send_json(send, *_maintainer_error(exc))
                elif coordination_errors:
                    await _send_coordination_error(send, exc)
                else:
                    await _send_json(send, 400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                if maintainer_path:
                    logger.error("maintainer route failed (%s)", type(exc).__name__)
                    await _send_json(send, *_maintainer_error(exc))
                elif coordination_errors:
                    await _send_coordination_error(send, exc)
                else:
                    logger.exception("api handler error: %s %s", method, path)
                    await _send_json(send, 500, {"error": str(exc)})
            return

        # 6) everything else -> the MCP app (token gate preserved)
        try:
            caller = _resolve(scope)
        except PrincipalsUnavailable:
            await _principals_unavailable(send)
            return
        if caller is None:
            await _send_json(send, 401, {
                "error": "unauthorized",
                "hint": "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"})
            return
        if path == "/mcp" or path.startswith("/mcp/"):
            from pseudolife_memory.coordination import (
                authenticated_principal, bound_identity,
                dispatch as coordination_dispatch, enforce_bound_identity,
            )
            headers = {k.decode().lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            try:
                binding = bound_identity(headers)
                if binding is not None:
                    # Fail closed on an open install: open loopback resolves
                    # everyone to "default", which the board admits by
                    # default, and a binding must never reach the store
                    # without a bearer.
                    principal = authenticated_principal(
                        headers, token_map=token_map, token=token)

                    def validate_binding():
                        context = coordination_dispatch(
                            service, "context", {}, headers=headers,
                            principal=principal)
                        enforce_bound_identity(binding, context)

                    await asyncio.get_running_loop().run_in_executor(
                        None, validate_binding)
            except Exception as exc:  # noqa: BLE001
                await _send_coordination_error(send, exc)
                return
        await mcp_app(scope, receive, send)

    return app
