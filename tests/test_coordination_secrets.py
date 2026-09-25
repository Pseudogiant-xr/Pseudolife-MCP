"""The board refuses credential-shaped text where it would keep it.

A message body stays in the audit log for its retention window, and a status
line or a lease purpose is hashed into the chain for good, so a secret pasted
into any of them outlives the mistake. ``looks_like_secret`` is a heuristic
net for the common shapes, not a guarantee; the refusal names only its code.

Every credential-shaped sample below is assembled at run time, so no
contiguous token lands in the tracked tree for a secret scanner to find.
"""
from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from pseudolife_memory.storage.coordination import (
    CoordinationError, looks_like_secret, secret_kind,
)
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_coordination_storage import creds, pair, store  # noqa: F401


def j(*parts: str) -> str:
    return "".join(parts)


GITHUB = j("gh", "p_", "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2")
GITHUB_OAUTH = j("gh", "o_", "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8")
GITHUB_PAT = j("github", "_pat_", "11AB2CD3EF4GH5IJ6KL7MN", "_", "q8Rs9Tu0Vw1Xy2Za3Bc4De5Fg6Hi7Jk8Lm9No0")
ANTHROPIC = j("sk-", "ant-", "api03-", "Xy9_Kq2-Lm7Pz4Rt6Wv8Bc3Dd1Ee5Ff0Gg")
OPENAI = j("sk", "-", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2")
OPENAI_PROJECT = j("sk", "-proj-", "Zq9-Wd4_Rk7Pm2Xs8Lt3Nv6Hb1Jc5Gf0Ky9Ua4Ie7Ow2")
AWS_KEY_ID = j("AK", "IA", "Q3R7T2W9Y4U8P6L1")
AWS_SESSION_KEY_ID = j("AS", "IA", "Z9X8C7V6B5N4M3K2")
SLACK = j("xo", "xb-", "123456789012-1234567890123-", "AbCdEfGhIjKlMnOpQrStUvWx")
JWT = j("ey", "JhbGciOiJIUzI1NiJ9", ".", "ey", "JzdWIiOiIxMjM0NTY3ODkwIn0", ".",
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
PEM_RSA = j("-----BEGIN ", "RSA PRIVATE ", "KEY-----")
PEM_PLAIN = j("-----BEGIN ", "PRIVATE ", "KEY-----")
PEM_OPENSSH = j("-----BEGIN ", "OPENSSH PRIVATE ", "KEY-----")
PEM_PGP = j("-----BEGIN ", "PGP PRIVATE ", "KEY BLOCK-----")
URLSAFE = j("q7Hd2kLm9Pz4", "Rt6Wv8Xy1Bc3", "Ns5Jf0Ge")          # like token_urlsafe
URLSAFE_2 = j("Lx2eG0fJ5sN", "3cB1yX8vW6t", "R4zP9mLk2dH7q")
AWS_SECRET = j("wJalrXUtnFEMI", "/K7MDENG/", "bPxRfiCYEXAMPLEKEY")
HUGGINGFACE = j("h", "f_", "AbCdEfGhIjKlMnOpQrStUvWxYz01234567")
STRIPE = j("sk", "_live_", "51HAbCdEfGh2IjKl3MnOp4QrSt")
GOOGLE = j("AI", "za", "SyAbCdEfGh1IjKlMn2OpQrSt3UvWxYz4AbC")
GITLAB = j("gl", "pat-", "Ab1Cd2Ef3Gh4Ij5Kl6Mn")
DSN = j("postgresql://pseudo:", "Zq8wKd3mRt", "@127.0.0.1:5433/db")

SECRETS = [
    ("github_token", f"use {GITHUB} for the push"),
    ("github_token", GITHUB_OAUTH),
    ("github_pat", f"GITHUB_TOKEN is {GITHUB_PAT}"),
    ("anthropic_key", f"key {ANTHROPIC} ok?"),
    ("openai_key", OPENAI),
    ("openai_key", f"({OPENAI_PROJECT})"),
    ("aws_access_key_id", f"id={AWS_KEY_ID}"),
    ("aws_access_key_id", AWS_SESSION_KEY_ID),
    ("aws_secret_key", f"aws_secret_access_key={AWS_SECRET}"),
    ("aws_secret_key", f"AWS_SECRET_ACCESS_KEY: '{AWS_SECRET}'"),
    ("slack_token", f"bot {SLACK}"),
    ("huggingface_token", f"HF_TOKEN is {HUGGINGFACE}"),
    ("stripe_key", STRIPE),
    ("google_api_key", f"maps key {GOOGLE}"),
    ("gitlab_token", GITLAB),
    ("jwt", f"Authorization: Bearer {JWT}"),
    ("bearer_token", f"Authorization: Bearer {URLSAFE}"),
    ("dsn_password", f"connect with {DSN}"),
    ("private_key", f"{PEM_RSA}\nMIIEow..."),
    ("private_key", PEM_PLAIN),
    ("private_key", PEM_OPENSSH),
    ("private_key", PEM_PGP),
    ("cli_flag", f"pseudolife-mcp shim --token {URLSAFE}"),
    ("cli_flag", f"--api-key '{URLSAFE}'"),
    ("key_value", f"PSEUDOLIFE_MCP_TOKEN={URLSAFE}"),
    # This deployment's own shapes: a principal token map, the adapter's
    # instance-key header, keys named for what they hold.
    ("key_value", f"PSEUDOLIFE_MCP_TOKENS=codex:{URLSAFE},claude:{URLSAFE_2}"),
    ("key_value", f"X-PL-Agent-Key: {URLSAFE}"),
    ("key_value", f"private_key={URLSAFE}"),
    ("key_value", f"access_key={URLSAFE}"),
    ("key_value", f"auth={URLSAFE}"),
    ("key_value", f'{{"api_key": "{URLSAFE}"}}'),
    ("key_value", f"password: {j('Correct7', 'Horse9', 'Battery2')}"),
    ("key_value", f"client_secret = '{URLSAFE}'"),
    ("key_value", f"x-api-key: {URLSAFE}"),
    ("key_value", f"apiKey={URLSAFE}"),
    ("key_value", f"passwd={URLSAFE}"),
    ("key_value", f"credentials:\t{URLSAFE}"),
    # A long single-case generated key still counts when it is one run.
    ("key_value", "api_key=" + j("k3v9x2m7q8w1z5r4", "t6y0u2i8o4p7a1s3", "d5f9")),
]

ORDINARY = [
    "I rotated the token; the secret now lives in the vault, not here.",
    "token budget: core 11,495 of 11,500 chars",
    "Pass the password prompt, then paste nothing: tokens and secrets stay local.",
    # Digests and ids: 40-hex git SHAs, 64-hex sha256, UUIDs, 32-hex ids.
    "merged 5057829364fdbf9e2ebfe007d7722c3001bfd471 onto master",
    "sha256=" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "credential_hash: " + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "token=" + "5057829364fdbf9e2ebfe007d7722c3001bfd471",
    "session 123e4567-e89b-12d3-a456-426614174000 closed",
    "session_token=123e4567-e89b-12d3-a456-426614174000",
    "acked message 0123456789abcdef0123456789abcdef from agent fedcba9876543210fedcba9876543210",
    "agent_key=0123456789abcdef0123456789abcdef",
    "api_key=0123456789ABCDEF0123456789ABCDEF",
    # Names, placeholders, paths and counts after a keyword.
    "PSEUDOLIFE_MCP_TOKEN=<bearer>",
    "token: PSEUDOLIFE_MCP_TOKEN_FILE",
    "PSEUDOLIFE_MCP_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx",
    "password_file=/run/secrets/db_password_2026_prod",
    "token_file=C:/Users/example/token.txt",
    "max_tokens=4096, tokens: 123456789012345678901234",
    "tokenizer=all-MiniLM-L6-v2-onnx-quantized-int8",
    "status: suite=running; secret_like_body=0 of 816",
    "credential = secrets.token_urlsafe(32)",
    "token_map=parse_token_map(os.environ.get('PSEUDOLIFE_MCP_TOKENS'))",
    # Prefixes mentioned in prose, and sk- inside a longer word.
    "keys start with sk-ant- or ghp_ and PEM files with BEGIN PRIVATE KEY",
    "the task-a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7 branch",
    "risk-a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7 note",
    "sk-learn-style-estimator-wrapper-for-the-bench",
    # Assembled like the samples above: the tracked-tree guard in
    # test_release_ux flags these prefixes followed by any long run.
    j("xo", "xb-", "token-placeholder-in-the-docs"),
    j("github", "_pat_", "tokens_are_refused_by_the_board"),
    # Found by review (2026-09-26): branches, paths, truncated digests,
    # subresource hashes, ids with a suffix, names and identifiers.
    "secrets-scan: claude/elated-bartik-bcc1a4",
    "token-fix: feat/v46-redactable-bodies",
    "token-limit: example/claude/branch-name-2026",
    "credential_file=secrets/2026/bench_pg.txt",
    "credential_hash_prefix=" + "9f86d081884c7d659a2feaa0",
    "credential_hash=sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "token_digest=" + "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce"
                      "47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e",
    "token=sha256-" + "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=",
    "integrity: sha384-" + "oqVuAfXRKap7fdgcCY5uykM6Kz4yPzI4aW2yF3Q9cGo",
    "session_token=123e4567-e89b-12d3-a456-426614174000-2",
    "token_source=readTheTokenFromTheEnvironmentVariableNow",
    "tokeniser=sentencepiece-Model2026-Variant7",
    "sk-learn-v2-estimator-wrapper-for-the-bench-2026",
    "/tmp/sk-" + "0123456789abcdef0123456789abcdef01234567",
    "logs.sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8",
    j("AS", "IA", "PACIFICSALESTEAM"),
    j("gh", "s_", "abcdefghijklmnopqrstuvwxyz0123456789"),
    j("github", "_pat_", "rotation_2026_q3_followup_note"),
    "author: Pseudogiant-xr",
    "cache_key=memory_agents_2026_09_26_roster",
    # Long word-joined identifiers after a key-named key (found 2026-09-26
    # after the key names were widened to key/auth/bearer).
    "cache_key=memory_agents_2026_09_26_roster_long_version",
    "sort_key: created_at_2026_09_26_then_agent_id_asc_v2",
    "idempotency_key=send-2026-09-26-relay-6-retry-3-of-5",
    "keyboard=us-intl-2026-layout-with-dead-keys",
    "set PSEUDOLIFE_MCP_TOKENS=editor:<token>,reviewer:<token>",
    "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>",
    "Authorization: Bearer $PSEUDOLIFE_MCP_TOKEN",
    "postgresql://pseudolife:<password>@127.0.0.1:5433/pseudolife",
    "postgresql://pseudolife:${POSTGRES_PASSWORD}@db:5432/pseudolife",
    "",
]


@pytest.mark.parametrize("kind,text", SECRETS)
def test_credential_shapes_are_recognised(kind, text):
    assert looks_like_secret(text)
    assert secret_kind(text) == kind


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_board_text_is_not(text):
    assert not looks_like_secret(text), secret_kind(text)


def test_a_long_body_of_identifier_characters_is_scanned_quickly():
    """The scan runs on every send, up to 8192 bytes: no pattern may rescan
    quadratically over a long run of key characters. Measured 2026-09-26 on
    the maintainer's Windows host, best of three: the slowest of these took
    1.0 ms; rescanning from each key instead of resuming after the value it
    consumed made one take 77 ms (``auth:x,`` repeated). The bound sits well
    between the two, with room for a machine several times slower."""
    import time
    for text in ("token" * 1638, "a" * 8192, "sk-" + "a" * 8189, "tokenX" * 1365 + "=",
                 "token=" * 1365, "token:a1" * 1024, ("secret_" * 1170)[:8192],
                 "auth:x," * 1170, ("key=" + "Ab1," * 5) * 400,
                 "-----BEGIN A " * 630, "eyJ" * 2730, "sk-" * 2730):
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            looks_like_secret(text)
            timings.append(time.perf_counter() - started)
        assert min(timings) < 0.025, (text[:12], min(timings))


def _refused(call, secret):
    with pytest.raises(CoordinationError) as caught:
        call()
    assert caught.value.code == "secret_like_body"
    assert str(caught.value) == "secret_like_body"
    assert secret not in repr(caught.value) and secret not in str(caught.value.args)


def _log(store):
    return store.storage.conn.execute(
        "SELECT event, payload, body FROM coordination_events ORDER BY seq").fetchall()


def test_send_refuses_a_secret_body_and_keeps_nothing(store):
    a, b = pair(store)
    before = _log(store)
    body = f"here is the deploy key {GITHUB}, thanks"
    _refused(lambda: store.send(*creds(a), to=b["agent_id"], text=body, request_id="r"), GITHUB)
    assert _log(store) == before
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_messages").fetchone() == (0,)
    assert store.receive(*creds(b))["messages"] == []


def test_status_updates_and_registration_refuse_a_secret(store):
    a = store.register("alice", status="starting")
    before = _log(store)
    status = f"using PSEUDOLIFE_MCP_TOKEN={URLSAFE}"
    _refused(lambda: store.update(*creds(a), status=status), URLSAFE)
    _refused(lambda: store.register("alice", status=status), URLSAFE)
    assert _log(store) == before
    assert store.authenticate(*creds(a))["status"] == "starting"
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_agents").fetchone() == (1,)
    # Ordinary text still goes through on both paths.
    store.update(*creds(a), status="suite=running, token budget fine")
    assert store.authenticate(*creds(a))["status"] == "suite=running, token budget fine"


def test_a_lease_purpose_refuses_a_secret(store):
    store.storage.conn.execute("TRUNCATE coordination_leases, coordination_lease_waiters")
    a = store.register("alice")
    before = _log(store)
    _refused(lambda: store.acquire_lease(*creds(a), name="gpu", purpose=f"with {SLACK}"), SLACK)
    assert _log(store) == before
    assert store.list_leases()["leases"] == []
    assert store.acquire_lease(*creds(a), name="gpu", purpose="bench run")["state"] == "held"


@pytest.mark.parametrize("field", ["label", "project", "task", "episode", "status"])
def test_every_scope_field_an_agent_sets_refuses_a_secret(store, field):
    """Registration fields are hashed into the register event, and project
    and task into the columns of every later event by that agent: none of
    them could ever be redacted."""
    a = store.register("alice")
    before = _log(store)
    _refused(lambda: store.register("alice", **{field: f"see {GITHUB}"}), GITHUB)
    if field in ("project", "task", "status"):
        _refused(lambda: store.update(*creds(a), **{field: f"see {GITHUB}"}), GITHUB)
    assert _log(store) == before


def test_a_lease_name_or_request_id_refuses_a_secret(store):
    store.storage.conn.execute("TRUNCATE coordination_leases, coordination_lease_waiters")
    a, b = pair(store)
    before = _log(store)
    name = f"claim:{GITHUB}"
    _refused(lambda: store.acquire_lease(*creds(a), name=name), GITHUB)
    _refused(lambda: store.release_lease(*creds(a), name=name), GITHUB)
    _refused(lambda: store.list_leases(name=name), GITHUB)
    _refused(lambda: store.send(*creds(a), to=b["agent_id"], text="hello",
                                request_id=GITHUB), GITHUB)
    assert _log(store) == before


def test_secret_like_body_is_a_public_code():
    from pseudolife_memory.coordination import PUBLIC_ERROR_CODES, public_error
    assert "secret_like_body" in PUBLIC_ERROR_CODES
    assert public_error(CoordinationError("secret_like_body")) == "secret_like_body"


# ── REST: a 400 that names the code and never the text ───────────────────

BEARER = {"Authorization": "Bearer fixture-bearer"}


def _app(pg_url):
    from pseudolife_memory.memory.hlc import HybridLogicalClock
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.web.fixtures import FixtureService
    from tests.asgi_helpers import stub_mcp
    storage = PostgresStorage(pg_url)
    service = FixtureService()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["default"]
    service._db_url = pg_url
    service._storage = storage
    service._lock = threading.Lock()
    service._hlc = HybridLogicalClock()
    service._ensure_init = lambda: None
    return storage, build_console_app(stub_mcp, "fixture-bearer", lambda: {}, service,
                                      token_map={})


def test_rest_refuses_a_secret_with_400_and_no_echo(pg_conn, pg_url):
    storage, app = _app(pg_url)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            async def register(label):
                response = await client.post(
                    "http://fixture/api/coordination/register", headers=BEARER,
                    json={"label": label, "capabilities": {"resumable": False}})
                assert response.status_code == 200, response.text
                body = response.json()
                return body["agent_id"], {**BEARER, "X-PL-Agent": body["agent_id"],
                                          "X-PL-Agent-Key": body["credential"]}

            _, sender = await register("sender")
            recipient, _ = await register("recipient")
            for action, payload, secret in (
                    ("send", {"to": recipient, "text": f"key: {ANTHROPIC}",
                              "request_id": "secret-1"}, ANTHROPIC),
                    ("update", {"status": f"jwt {JWT}"}, JWT),
                    ("lease", {"name": "gpu", "purpose": PEM_RSA}, PEM_RSA)):
                response = await client.post(f"http://fixture/api/coordination/{action}",
                                             headers=sender, json=payload)
                assert response.status_code == 400, (action, response.text)
                assert response.json() == {"error": "secret_like_body"}
                assert secret not in response.text
            # The operator's redaction is not an agent action on any transport.
            response = await client.post("http://fixture/api/coordination/redact",
                                         headers=sender,
                                         json={"message_id": "0" * 32, "reason": "no"})
            assert response.status_code == 400
            assert response.json() == {"error": "unknown_coordination_action"}

    try:
        asyncio.run(asyncio.wait_for(drive(), 20))
    finally:
        storage.close()


def test_the_refusal_is_documented_where_operators_read_it():
    """The core tool manifest has no room for it (11,495 of 11,500
    characters), so the configuration guide carries the contract."""
    from pathlib import Path
    guide = (Path(__file__).resolve().parents[1] / "docs" / "guide"
             / "configuration.md").read_text(encoding="utf-8")
    assert "secret_like_body" in guide and "board-audit redact" in guide
