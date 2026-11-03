"""Console link sign-in (schema v59; spec 2026-11-02, sections 3 and 4;
cutover blocker 8): where a Console session resolves and where it
must not, the operator allowlist, the three bearer-gated routes and the
pre-auth ``POST /api/console/session``, Python against Rust over HTTP.

The template carries sessions and sign-in codes written as Python's
``console_sessions`` stores them (hashes only), so both arms hold the same
rows: a session an environment token issued, one a paired machine issued,
and ones that must not resolve (idle, past 90 days, its issuer gone,
revoked). Live only: the rows carry the template's clock.
"""
import hashlib
import json
import re
import sys
import time

DAY = 86400.0
ORIGIN_KEY = "http://localhost:8765"
CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}")
SESSION_ID = re.compile(r"[0-9a-f]{16}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _clock(value):
    return "<clock>" if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def register(harness):
    T_DEFAULT = harness.T_DEFAULT
    LAPTOP = "signin-laptop-token-w1a"
    # Seeded sessions: id -> (browser token, principal, source, issuer, created
    # offset, last-use offset, expiry offset, revoked)
    SESSIONS = {
        "seed-env": ("signin-browser-env", "default", "environment", sha(T_DEFAULT), -3600, None, None, False),
        "seed-store": ("signin-browser-store", "laptop", "store", sha(LAPTOP), -3600, None, None, False),
        "seed-revoke-me": ("signin-browser-revoke", "default", "environment", sha(T_DEFAULT), -1800, None, None, False),
        "seed-idle": ("signin-browser-idle", "default", "environment", sha(T_DEFAULT), -40 * DAY, -31 * DAY, None, False),
        "seed-old": ("signin-browser-old", "default", "environment", sha(T_DEFAULT), -91 * DAY, -DAY, DAY, False),
        "seed-issuer": ("signin-browser-issuer", "default", "environment", sha("a-removed-token"), -3600, None, None, False),
        "seed-revoked": ("signin-browser-revoked", "default", "environment", sha(T_DEFAULT), -3600, None, None, True),
    }
    # Seeded codes: code -> (principal, source, issuer)
    CODES = {
        "ABCDEFGHJKMN": ("default", "environment", sha(T_DEFAULT)),
        "BCDEFGHJKMNP": ("laptop", "store", sha(LAPTOP)),
        "CDEFGHJKMNPQ": ("default", "environment", sha("a-removed-token")),
    }

    def token(sid):
        return SESSIONS[sid][0]

    class ConsoleSignin(harness.Scenario):
        """The gate's session call sites and the four sign-in routes."""
        name = "console-signin"
        seeds_principals = True
        live_only = True

        def __init__(self):
            self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT}

        def prepare_template(self, dsn: str) -> None:
            import psycopg
            sys.path.insert(0, str(harness.REPO))
            from pseudolife_memory import principal_store
            from pseudolife_memory.storage.postgres import PostgresStorage
            PostgresStorage(dsn).close()   # the bank, its v59 tables included
            now = time.time()
            with psycopg.connect(dsn, autocommit=True) as conn:
                invite = principal_store.create_invite(conn, "laptop", ttl_seconds=3600, replace=False)
                principal_store.redeem(dsn, sha(invite["code"]), sha(LAPTOP))
                for sid, (tok, principal, source, issuer, created, used, expires, revoked) in SESSIONS.items():
                    created_at = now + created
                    conn.execute(
                        "INSERT INTO console_sessions (session_id, token_hash, principal, source, "
                        "issuer, method, label, created_at, last_used_at, expires_at, revoked_at) "
                        "VALUES (%s, %s, %s, %s, %s, 'link', %s, %s, %s, %s, %s)",
                        (sid, sha(tok), principal, source, issuer, f"Seeded {sid}", created_at,
                         None if used is None else now + used,
                         now + expires if expires is not None else created_at + 90 * DAY,
                         now - 60 if revoked else None))
                for code, (principal, source, issuer) in CODES.items():
                    conn.execute(
                        "INSERT INTO console_signin_codes (code_hash, principal, source, issuer, "
                        "origin, expires_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (sha(code), principal, source, issuer, ORIGIN_KEY, now + DAY, now))

        def normalize_db(self, state: dict, before: dict, side: str) -> dict:
            """New session and code rows carry a random id and the database
            clock; a seeded session's last use moves on the refresher's own
            minute; a revocation or a spend stamps its time. Only those cells
            are free, and the rows are sorted after."""
            state = super().normalize_db(state, before, side)
            for name, key in (("console_sessions", "token_hash"), ("console_signin_codes", "code_hash")):
                table = state["rows"].get(f"public.{name}")
                prior = before["rows"].get(f"public.{name}")
                if not table:
                    continue
                cols = table["columns"]
                old = {}
                if prior:
                    pk = prior["columns"].index(key)
                    old = {r[pk]: dict(zip(prior["columns"], r)) for r in prior["rows"]}
                for row in table["rows"]:
                    record = dict(zip(cols, row))
                    was = old.get(record[key])
                    for i, col in enumerate(cols):
                        value = row[i]
                        if was is None:
                            if col in ("session_id", "code_hash") or col.endswith("_at"):
                                row[i] = _clock(value) if col.endswith("_at") else f"<new {col}>"
                        elif was[col] != value and col in ("last_used_at", "revoked_at", "used_at"):
                            row[i] = _clock(value)
                table["rows"].sort(key=lambda r: json.dumps(r, sort_keys=True))
            return state

        def cases(self):
            js = ("Content-Type", "application/json")
            env = harness.bearer(T_DEFAULT)
            laptop = harness.bearer(LAPTOP)
            case = harness.case

            def as_(sid):
                return [harness.bearer(token(sid))]

            def link(label, auth, body, headers=()):
                return case(label, "POST", "/api/console/link", [auth, js, *headers], json.dumps(body).encode())

            def signin(label, origin, body, ctype=js):
                headers = ([("Origin", origin)] if origin else []) + ([ctype] if ctype else [])
                return case(label, "POST", "/api/console/session", headers,
                            body if isinstance(body, bytes) else json.dumps(body).encode())

            def redeem_body(code, browser, label="Firefox  on\tLinux"):
                return {"code": code, "token_sha256": sha(browser), "label": label}

            return [
                case("health offers the link sign-in", "GET", "/health"),
                # Where a session resolves.
                case("environment session reads /api", "GET", "/api/nope", as_("seed-env")),
                case("paired machine's session reads /api", "GET", "/api/nope", as_("seed-store")),
                case("idle session", "GET", "/api/nope", as_("seed-idle")),
                case("session past 90 days", "GET", "/api/nope", as_("seed-old")),
                case("session whose issuer is gone", "GET", "/api/nope", as_("seed-issuer")),
                case("revoked session", "GET", "/api/nope", as_("seed-revoked")),
                # ... and where it must not (the near misses).
                case("session on /mcp", "POST", "/mcp", as_("seed-env") + [js], b"{}"),
                case("session on /mcp/", "POST", "/mcp/", as_("seed-env") + [js], b"{}"),
                case("session on coordination receive", "POST", "/api/coordination/receive", as_("seed-env") + [js], b"{}"),
                case("session on coordination send", "POST", "/api/coordination/send", as_("seed-env") + [js], b"{}"),
                case("session on daemon-notice", "POST", "/api/daemon-notice", as_("seed-env") + [js], b"{}"),
                case("session on the board check-in hook", "GET", "/api/hook/coordination-start", as_("seed-env")),
                case("session on the woke hook", "POST", "/api/hook/woke?agent=" + "a" * 32, as_("seed-env")),
                case("environment session cannot issue", "POST", "/api/console/link", as_("seed-env") + [js],
                     json.dumps({"origin": "http://127.0.0.1:8765"}).encode()),
                case("paired session cannot issue", "POST", "/api/console/link", as_("seed-store") + [js],
                     json.dumps({"origin": "http://127.0.0.1:8765"}).encode()),
                case("session GET link is refused before the verb", "GET", "/api/console/link", as_("seed-env")),
                # The operator allowlist.
                case("paired session POST /api/config", "POST", "/api/config", as_("seed-store")),
                case("paired session POST /api/daemon-notice", "POST", "/api/daemon-notice", as_("seed-store")),
                case("paired session GET /api/config", "GET", "/api/config", as_("seed-store")),
                case("environment session POST /api/config", "POST", "/api/config", as_("seed-env")),
                # The gate's other call sites.
                case("session board view", "GET", "/api/agents?view=coordination", as_("seed-env")),
                case("session maintainer status", "GET", "/api/maintainer", as_("seed-env")),
                # POST /api/console/link.
                link("link for a loopback Console", env, {"origin": "http://127.0.0.1:8765"}),
                link("link landing on a page", env, {"origin": "http://127.0.0.1:8765/", "to": "board"}),
                link("link for a paired machine", laptop, {"origin": "http://localhost:8765"}),
                link("link bad origin", env, {"origin": "http://127.0.0.1:8765/ui"}),
                link("link no origin", env, {}),
                link("link bad page", env, {"origin": "http://127.0.0.1:8765", "to": "../evil"}),
                link("link extra key", env, {"origin": "http://127.0.0.1:8765", "x": 1}),
                link("link remote origin without passkeys", env, {"origin": "http://100.64.0.1:8765"}),
                case("link GET", "GET", "/api/console/link", [env]),
                case("link unauthenticated", "POST", "/api/console/link", [js], b"{}"),
                case("unknown console route", "GET", "/api/console/nope", [env]),
                case("console prefix alone", "GET", "/api/console", [env]),
                # POST /api/console/session.
                case("sign-in GET", "GET", "/api/console/session"),
                signin("sign-in without Origin", None, redeem_body("ABCD-EFGH-JKMN", "signin-new-a")),
                signin("sign-in text/plain", "http://[::1]:8765", redeem_body("ABCD-EFGH-JKMN", "signin-new-a"),
                       ("Content-Type", "text/plain")),
                signin("sign-in malformed", "http://[::1]:8765", {"code": "ABCD-EFGH-JKMN"}),
                signin("sign-in at another origin", "http://127.0.0.1:9999", redeem_body("ABCD-EFGH-JKMN", "signin-new-a")),
                signin("sign-in at a loopback alias", "http://[::1]:8765", redeem_body("abcd-efgh-jkmn", "signin-new-a")),
                signin("sign-in code spent", "http://[::1]:8765", redeem_body("ABCD-EFGH-JKMN", "signin-new-b")),
                case("the new browser token works at once", "GET", "/api/nope", [harness.bearer("signin-new-a")]),
                signin("sign-in issuer gone", "http://localhost:8765", redeem_body("CDEF-GHJK-MNPQ", "signin-new-c")),
                signin("sign-in as a paired machine", "http://localhost:8765", redeem_body("BCDE-FGHJ-KMNP", "signin-new-d")),
                case("paired sign-in token works", "GET", "/api/nope", [harness.bearer("signin-new-d")]),
                case("new paired session POST /api/config", "POST", "/api/config", [harness.bearer("signin-new-d")]),
                # GET /api/console/sessions and revoke.
                case("operator lists every session", "GET", "/api/console/sessions", [env]),
                case("paired machine lists its own", "GET", "/api/console/sessions", [laptop]),
                case("session sees which is current", "GET", "/api/console/sessions", as_("seed-env")),
                case("revoke bad body", "POST", "/api/console/sessions/revoke", [env, js], b"{}"),
                case("paired machine cannot revoke another's", "POST", "/api/console/sessions/revoke", [laptop, js],
                     json.dumps({"session_id": "seed-revoke-me"}).encode()),
                case("operator revokes", "POST", "/api/console/sessions/revoke", [env, js],
                     json.dumps({"session_id": "seed-revoke-me"}).encode()),
                case("revoked session signed out at once", "GET", "/api/nope", as_("seed-revoke-me")),
                case("revoke again", "POST", "/api/console/sessions/revoke", [env, js],
                     json.dumps({"session_id": "seed-revoke-me"}).encode()),
            ]

    def scrub_link(resp):
        body = resp.get("json")
        if isinstance(body, dict) and isinstance(body.get("code"), str) and CODE.fullmatch(body["code"]):
            code = body["code"]
            body["code"] = "<code>"
            if isinstance(body.get("url"), str):
                body["url"] = body["url"].replace(code, "<code>")
            body["expires_at"] = _clock(body.get("expires_at"))
        return resp

    def scrub_session(resp):
        body = resp.get("json")
        if isinstance(body, dict) and isinstance(body.get("session_id"), str) \
                and SESSION_ID.fullmatch(body["session_id"]):
            body["session_id"] = "<session id>"
            body["expires_at"] = _clock(body.get("expires_at"))
        return resp

    def scrub_sessions(resp):
        body = resp.get("json")
        if isinstance(body, dict) and isinstance(body.get("sessions"), list):
            for item in body["sessions"]:
                if not isinstance(item, dict):
                    continue
                # A seeded row's last use moves with this run's uses; a new
                # row's id and times are the run's own.
                item["last_used_at"] = _clock(item.get("last_used_at"))
                if item.get("session_id") not in SESSIONS:
                    item["session_id"] = "<new session>" if SESSION_ID.fullmatch(
                        str(item.get("session_id"))) else item.get("session_id")
                    item["created_at"] = _clock(item.get("created_at"))
                    item["expires_at"] = _clock(item.get("expires_at"))
        return resp

    harness.register_response_scrub("/api/console/link", scrub_link)
    harness.register_response_scrub("/api/console/session", scrub_session)
    harness.register_response_scrub("/api/console/sessions", scrub_sessions)
    return [ConsoleSignin]
