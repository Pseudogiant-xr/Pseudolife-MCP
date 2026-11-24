"""Principal-store golden harness, using the existing disposable DB and
catalog/row dumper. Run `golden`, `golden --record-candidate [--write]`, or
`mutants`; all three run the native principal probe alone and compare each
step with the committed golden (the frozen Python arm is retired).

Secrets are generated in memory, never included in results or failure text.
Clock values are checked against the bank's database-clock window before they
become event labels; unchanged timestamps keep their original event labels.
The fixture DDL is the daemon's own (``schema.sql``), the operator routes the
daemon's own (``http.rs``), and invites are written by the SQL fixture below.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

_DATABASE_TURN_MARKER = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"
import queue
import random
import re
import subprocess
import sys
import threading

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
import psycopg
import candidate_record
import dbstate
import pgdisposable

pg = pgdisposable.Slice("prn")
SCHEMA_SOURCE = REPO / "rust/daemon/src/storage/schema.sql"
HTTP_SOURCE = REPO / "rust/daemon/src/http.rs"
AUTH_SOURCE = REPO / "rust/daemon/src/auth.rs"
PAIR_SOURCE = REPO / "rust/daemon/src/routes/pair.rs"
# The probe's clock column (principal_probe.rs): epoch seconds from the database.
DB_NOW = "EXTRACT(EPOCH FROM clock_timestamp())::double precision"
OWNED_SQL = "COALESCE((to_jsonb(p) ->> 'owned')::boolean, TRUE)"


def secret_sha256(value):
    """How the daemon stores a token or pairing code: lowercase SHA-256 hex."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def schema_statement(start, end, source=None):
    """The one statement of the daemon's schema that begins ``start`` and ends
    at the first ``end`` after it. No DDL is copied into this harness."""
    text = SCHEMA_SOURCE.read_text(encoding="utf-8") if source is None else source
    if text.count(start) != 1:
        raise AssertionError(f"daemon schema has no single {start!r}")
    begin = text.index(start)
    stop = text.find(end, begin)
    if stop < 0:
        raise AssertionError(f"daemon schema statement {start!r} does not end")
    return text[begin:stop + len(end)]


META_TABLE_SQL = schema_statement("CREATE TABLE IF NOT EXISTS meta (", ");")
PRINCIPALS_TABLE_SQL = schema_statement("CREATE TABLE IF NOT EXISTS principals (", "END $$;")


def operator_post_paths(source=None, auth=None):
    """The operator routes the daemon gates (``http.rs``
    ``OPERATOR_POST_PATHS``), read from the one exact declaration, which
    may wrap over lines; they must be the routes ``auth.rs``
    ``operator_route`` (what the probe answers with) names."""
    text = HTTP_SOURCE.read_text(encoding="utf-8") if source is None else source
    found = re.findall(r'^const OPERATOR_POST_PATHS: \[&str; (\d+)\] = \[([^\]]*)\];$', text, re.M)
    if len(found) != 1:
        raise AssertionError("daemon operator route declaration changed")
    count, body = found[0]
    paths = re.findall(r'"([^"\\]+)"', body)
    if (re.sub(r'"[^"\\]+"|[\s,]', "", body) or len(paths) != int(count)
            or len(set(paths)) != len(paths) or not all(path.startswith("/api/") for path in paths)):
        raise AssertionError("daemon operator routes are not a plain list of distinct /api/ paths")
    text = AUTH_SOURCE.read_text(encoding="utf-8") if auth is None else auth
    policy = re.findall(r'fn operator_route\(path: &str\) -> bool \{\s*matches!\(path, ([^)]*)\)\s*\}', text)
    if len(policy) != 1 or set(re.findall(r'"([^"\\]+)"', policy[0])) != set(paths) \
            or re.sub(r'"[^"\\]+"|[\s|]', "", policy[0]):
        raise AssertionError("auth.rs operator_route and http.rs OPERATOR_POST_PATHS name different routes")
    return frozenset(paths)


def pairing_code_alphabet(source=None):
    """The pairing-code alphabet the daemon redeems (``routes/pair.rs``). The
    seeded codes drawn from it reach the golden only as stored hashes."""
    text = PAIR_SOURCE.read_text(encoding="utf-8") if source is None else source
    found = re.findall(r'^const PAIRING_CODE_ALPHABET: &str = "([0-9A-Z]+)";$', text, re.M)
    if len(found) != 1 or len(set(found[0])) != len(found[0]):
        raise AssertionError("routes/pair.rs PAIRING_CODE_ALPHABET declaration changed")
    return found[0]


OPERATOR_POST_PATHS = operator_post_paths()
PAIRING_CODE_ALPHABET = pairing_code_alphabet()
OPERATOR_REQUESTS = [{"method": method, "path": path}
                     for path in sorted(OPERATOR_POST_PATHS) + ["/api/stats"]
                     for method in ("GET", "POST")]

GOLDEN = HERE / "goldens" / "principals.json"
MUTANTS = ["principal-shadow", "principal-unavailable", "principal-add-race",
           "principal-add-replace", "principal-revoked", "principal-redeem-expiry",
           "principal-redeem-retry", "principal-redeem-excluded"]
CLOCK_COLUMNS = {"created_at", "code_expires_at", "paired_at", "revoked_at"}
NOW = "SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision"


class ClockState:
    """Reject wrong timestamps before normalizing a declared write event."""
    def __init__(self, fixture_role="fixture-role", vector_extension=None):
        self.values = {}
        self.fixture_role = fixture_role
        self.vector_extension = vector_extension

    def normalize(self, state, event, window, offsets=None):
        state = json.loads(json.dumps(state))
        offsets = offsets or {}
        table = state["rows"].get("public.principals", {"columns": [], "rows": []})
        columns = table["columns"]
        for row in table["rows"]:
            name = row[columns.index("principal")]
            for column in CLOCK_COLUMNS & set(columns):
                index = columns.index(column)
                value = row[index]
                key = (name, column)
                old = self.values.get(key)
                if value is None:
                    self.values[key] = (None, None)
                    continue
                if old and value == old[0]:
                    row[index] = old[1]
                    continue
                if key not in offsets:
                    raise AssertionError("undeclared timestamp mutation")
                offset = offsets[key]
                if offset is None:
                    # Explicit adversarial fixture clocks remain exact.
                    if not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise AssertionError("invalid adversarial clock")
                    label = value
                else:
                    low, high = window
                    if (not isinstance(value, (int, float)) or isinstance(value, bool)
                            or not math.isfinite(value) or not low + offset <= value <= high + offset):
                        raise AssertionError("timestamp outside database-clock write window")
                    label = f"<{event}:{column}{offset:+g}>"
                self.values[key] = (value, label)
                row[index] = label
        # Role names are installation details; catalog contents still compare.
        for entry in state["catalog"].get("owners_and_comments", []):
            if entry[3] != self.fixture_role:
                raise AssertionError("catalog owner differs from verified fixture role")
            entry[3] = "<test-role>"
        # The fixture may preinstall vector even though this store never uses
        # it. Its initial version is host metadata, but a change during an
        # arm is a real catalog mutation and must still refuse comparison.
        for entry in state["catalog"].get("extensions", []):
            if entry[0] == "vector" and self.vector_extension is not None:
                if tuple(entry[1:]) != self.vector_extension:
                    raise AssertionError("vector extension changed from the verified fixture baseline")
                entry[1] = "<fixture-vector-version>"
        return state


def normalizer_controls():
    vector_fixture = {"rows": {}, "catalog": {"extensions": [["vector", "0.8.6", "public"]]}}
    first, second = ClockState(), ClockState()
    first.vector_extension = ("0.8.6", "public")
    second.vector_extension = ("0.8.5", "public")
    first_state = first.normalize(vector_fixture, "fixture-vector", (100.0, 101.0))
    vector_fixture["catalog"]["extensions"][0][1] = "0.8.5"
    second_state = second.normalize(vector_fixture, "fixture-vector", (100.0, 101.0))
    if first_state != second_state:
        raise AssertionError("fixture extension versions made compatible principals differ")
    try:
        first.normalize(vector_fixture, "changed-vector", (100.0, 101.0))
    except AssertionError:
        pass
    else:
        raise AssertionError("normalizer hid a vector extension change during the arm")
    owner_tracker = ClockState()
    owned = {"rows": {}, "catalog": {"owners_and_comments": [
        ["public", "principals", "r", "fixture-role", None]]}}
    owner_tracker.normalize(owned, "initial-owner", (100.0, 101.0))
    owned["catalog"]["owners_and_comments"][0][3] = "unexpected-owner"
    try:
        owner_tracker.normalize(owned, "changed-owner", (100.0, 101.0))
    except AssertionError:
        pass
    else:
        raise AssertionError("owner normalizer hid a catalog ownership change")
    def fixture(value):
        return {"catalog": {}, "rows": {"public.principals": {
            "columns": ["principal", "created_at"], "rows": [["probe", value]]}}}
    for value in (99.0, 102.0, float("nan"), float("inf"), "100", True):
        try:
            ClockState().normalize(fixture(value), "probe", (100.0, 101.0),
                                   {("probe", "created_at"): 0})
        except AssertionError:
            continue
        raise AssertionError("clock normalizer accepted negative control")
    tracker = ClockState()
    tracker.normalize(fixture(100.5), "first", (100.0, 101.0), {("probe", "created_at"): 0})
    try:
        tracker.normalize(fixture(100.6), "second", (100.0, 101.0))
    except AssertionError:
        return 9
    raise AssertionError("clock normalizer hid an undeclared rewrite")


def prefix_admission_controls(check=pg._check):
    if check("pl_cf_prn_1") != "pl_cf_prn_1":
        raise AssertionError("principal fixture prefix refused")
    for name in ("pl_cf_prn2_1", "pl_cf_pr_1", "pl_cf_principal_1",
                 "pl_cf_prn_", "pl_cf_prn_A", "pl_cf_prn_x-y"):
        try:
            check(name)
        except ValueError:
            continue
        raise AssertionError("near-miss disposable prefix admitted")
    return 6


def prefix_mutant_control():
    refused = prefix_admission_controls()
    source = (HERE / "pgdisposable.py").read_text(encoding="utf-8")
    original = "if not DISPOSABLE_NAME.fullmatch(name):"
    if source.count(original) != 1:
        raise AssertionError("disposable admission mutant anchor changed")
    # Read the prefix from the same slice that supplies the admission pattern.
    for predicate in ("name.startswith((space or current()).PREFIX[:-2])",
                      "name.startswith((space or current()).PREFIX)"):
        mutant = source.replace(original, "if not " + predicate + ":")
        namespace = {"__file__": pgdisposable.__file__}
        exec(compile(mutant, "<disposable-admission-mutant>", "exec"), namespace)
        try:
            # The mutant admits under this harness's slice, not the launcher's.
            prefix_admission_controls(lambda name: namespace["_check"](name, pg))
        except AssertionError as exc:
            if str(exc) != "near-miss disposable prefix admitted":
                raise
            continue
        raise AssertionError("disposable admission source mutant survived")
    return {"near_miss_refusals": refused, "mutant_caught": True, "mutants_caught": 2}


KEEP = object()


def create_invite(conn, name, code_hash, *, tier, board, ttl_seconds, replace, owned=KEEP):
    """The invite fixture, modelled on the native ``invite``'s statements
    (``rust/shim/src/cli/pairing/invite_db.rs`` ``create_invite``) for the
    states this scenario reaches. A new name is inserted; a pending one, or
    a paired one with ``replace``, gets the new code. Unlike the native
    command (where no tier means keep it), ``tier`` and ``board`` are always
    written, as the recorded scenario's invites gave them; ``owned`` only
    when the scenario sets it. A revoked name (whose reset also ends its
    Console sessions) and a paired name without ``replace`` are refused,
    since no step here invites one."""
    with conn.transaction():
        existing = conn.execute(
            "SELECT token_hash IS NOT NULL, revoked_at IS NOT NULL "
            "FROM public.principals WHERE principal = %s FOR UPDATE", (name,)).fetchone()
        params = {"name": name, "code": code_hash, "ttl": float(ttl_seconds), "tier": tier,
                  "board": bool(board), "owned": None if owned is KEEP else bool(owned)}
        if existing is None:
            state = "new"
            column, value = ("", "") if owned is KEEP else (", owned", ", %(owned)s")
            row = conn.execute(
                f"INSERT INTO public.principals AS p (principal, tier, board{column}, code_hash, "
                f"code_expires_at, created_at) VALUES (%(name)s, %(tier)s, %(board)s{value}, "
                f"%(code)s, {DB_NOW} + %(ttl)s, {DB_NOW}) "
                f"RETURNING code_expires_at, tier, board, {OWNED_SQL}", params).fetchone()
        else:
            paired, revoked = existing
            state = "revoked" if revoked else "paired" if paired else "pending"
            if state == "revoked" or (state == "paired" and not replace):
                raise AssertionError(f"invite fixture does not model a {state} name without its reset")
            given = ", tier = %(tier)s, board = %(board)s" + ("" if owned is KEEP else ", owned = %(owned)s")
            row = conn.execute(
                f"UPDATE public.principals p SET code_hash = %(code)s, code_expires_at = {DB_NOW} + "
                f"%(ttl)s, paired_code_hash = NULL{given} WHERE principal = %(name)s "
                f"RETURNING code_expires_at, tier, board, {OWNED_SQL}", params).fetchone()
    return {"state": state, "tier": row[1], "board": bool(row[2]), "owned": bool(row[3])}


class RustArm:
    def __init__(self, binary, dsn, mutant=None):
        env = os.environ.copy()
        env.update(PSEUDOLIFE_PRINCIPAL_HARNESS="1", PSEUDOLIFE_MCP_DATABASE_URL=dsn)
        env.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
        if mutant:
            env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
        self.process = subprocess.Popen([str(binary)], env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding="utf-8")
        self.lines = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=read, daemon=True).start()

    def call(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=True) + "\n")
        self.process.stdin.flush()
        line = self.lines.get(timeout=20)
        if line is None:
            raise RuntimeError("principal probe exited before answering")
        try:
            return json.loads(line)
        except ValueError:
            raise RuntimeError("principal probe produced non-JSON output") from None

    def close(self):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait()
            raise RuntimeError("principal probe did not stop") from None
        if code:
            raise RuntimeError("principal probe exited unsuccessfully")


class Replay:
    """The native probe on one disposable bank, each step compared with the
    golden's record at the same position (case label, output and state)."""

    def __init__(self, binary, golden, mutant=None):
        self.name = pg.name(f"pl_cf_prn_{os.getpid()}_1")
        self.dsn = None
        self.arm = None
        self.records = []
        self.differences = []
        self.golden = golden
        self.codes = {}
        self.raw_secrets = []
        self.race_tokens = {}
        self.race_winners = {}
        self.rng = random.Random(53)
        try:
            self.dsn = pg.create(self.name)
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                fixture_role = conn.execute("SELECT current_user").fetchone()[0]
                vector_extension = conn.execute(
                    "SELECT e.extversion,n.nspname FROM pg_extension e "
                    "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'").fetchone()
                self.tracker = ClockState(fixture_role, vector_extension)
                conn.execute(PRINCIPALS_TABLE_SQL)
            self.arm = RustArm(binary, self.dsn, mutant)
        except BaseException:
            self.close()
            raise

    def secret(self, label):
        value = hashlib.sha256(("principal-harness-synthetic:" + label).encode()).hexdigest()
        self.raw_secrets.append(value)
        return value

    def code(self):
        value = "".join(self.rng.choice(PAIRING_CODE_ALPHABET) for _ in range(12))
        self.raw_secrets.append(value)
        return value

    def safe(self, value):
        text = json.dumps(value, allow_nan=False)
        if any(secret in text for secret in self.raw_secrets):
            raise AssertionError("raw synthetic credential reached output")
        return value

    def step(self, label, v=None, *, mutation=None, offsets=None):
        index = len(self.records)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            before = conn.execute(NOW).fetchone()[0]
            answer = mutation(conn) if mutation else self.arm.call(v)
            after = conn.execute(NOW).fetchone()[0]
        state = None
        if mutation is not None or (v and v["op"] in ("redeem", "redeem_race", "revoke")):
            state = self.tracker.normalize(dbstate.dump(self.dsn), label, (before, after), offsets)
            if v and v["op"] == "redeem_race":
                self.race_tokens[v["name"]] = v["token_hashes"]
            if self.race_tokens and "public.principals" in state["rows"]:
                table = state["rows"]["public.principals"]
                columns = table["columns"]
                for row in table["rows"]:
                    name = row[columns.index("principal")]
                    if name in self.race_tokens:
                        idx = columns.index("token_hash")
                        if row[idx] not in self.race_tokens[name]:
                            raise AssertionError("race stored a token outside competing candidates")
                        winner = self.race_winners.setdefault(name, row[idx])
                        if winner != row[idx]:
                            raise AssertionError("durable race winner changed after redemption")
                        row[idx] = "<concurrent-winner>"
        if v and v["op"] == "list":
            # List times must equal the normalized durable DB times; this
            # catches fabricated timestamps as well as wrong state labels.
            for row in answer["rows"]:
                for column in CLOCK_COLUMNS:
                    value = row[column]
                    if value is not None:
                        stored, normalized = self.tracker.values[(row["principal"], column)]
                        if value != stored:
                            raise AssertionError("list timestamp differs from durable row")
                        row[column] = normalized
        record = {"case": label, "output": self.safe(answer), "state": self.safe(state)}
        self.records.append(record)
        # Paths only: never print credential-bearing inputs or DB errors.
        diffs = step_diffs(self.golden[index] if index < len(self.golden) else None, record)
        if diffs:
            self.differences.append({"case": label, "diffs": diffs})

    def finish(self):
        """A golden record past the scenario's last step is a difference too."""
        for record in self.golden[len(self.records):]:
            self.differences.append({"case": record.get("case"), "diffs": ["/: only in the golden"]})

    def invite(self, name, tier=None, board=True, replace=False, ttl=900, adversarial=False,
               owned=KEEP):
        code_hash = secret_sha256(self.code())
        self.codes[name] = code_hash
        def mutate(conn):
            return create_invite(conn, name, code_hash, tier=tier, board=board, owned=owned,
                                 ttl_seconds=ttl, replace=replace)
        offsets = {(name, "created_at"): 0, (name, "code_expires_at"): ttl}
        self.step(("adversarial-invite-" if adversarial else "invite-") + name +
                  ("-replace" if replace else ""), mutation=mutate, offsets=offsets)

    def adversarial(self, label, query, offsets=None, params=()):
        def mutate(conn):
            conn.execute(query, params)
            return {"adversarial_fixture": True}
        self.step("adversarial-" + label, mutation=mutate, offsets=offsets)

    def close(self):
        errors = []
        if self.arm is not None:
            try:
                self.arm.close()
            except Exception as exc:
                errors.append(type(exc).__name__)
        try:
            pg.drop(self.name)
        except Exception as exc:
            errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError("principal harness cleanup failed")


def step_diffs(expected, record):
    """The differences between a golden record and the candidate's step:
    the case label at that position, its output, then its database state."""
    if expected is None:
        return ["/: only in the candidate"]
    diffs = [] if expected.get("case") == record["case"] else ["/case: golden and candidate steps differ"]
    # section_diff descends lists of equal length, so a value inside a list
    # compares by type as well (true is not 1), as everywhere else.
    diffs += candidate_record.section_diff(expected.get("output"), record["output"], "")
    if expected.get("state") is not None or record["state"] is not None:
        diffs += candidate_record.section_diff(expected.get("state"), record["state"], "/database")
    return candidate_record.labelled(diffs)


def scenarios(p):
    # The label predates the Python arm's retirement; the golden pins it.
    p.step("initial-oracle-schema", mutation=lambda _conn: {"oracle_schema": True})
    env_token, single = p.secret("env"), p.secret("single")
    a, b, bad = (p.secret(n) for n in ("a", "b", "bad"))
    unicode_token = p.secret("nonascii") + "é"
    nbsp_token = unicode_token + "\u00a0"
    p.raw_secrets.extend((unicode_token, nbsp_token))
    h = secret_sha256
    def row(name, token=None, tier=None, board=True, revoked=False, owned=True):
        return {"principal": name, "token_hash": h(token) if token else None,
                "tier": tier, "board": board, "revoked": revoked, "owned": owned}
    def inspect(label, names=(), tokens=(), headers=(), allowed=()):
        p.step(label, {"op": "inspect", "names": list(names),
                       "headers": ["Bearer " + token for token in tokens] + list(headers),
                       "operator_requests": OPERATOR_REQUESTS,
                       "allowed": list(allowed)})
    reset = {"op": "reset", "env_map": env_token + ":desk", "single": single}
    p.step("configured", reset)
    inspect("never-loaded-fail-closed", tokens=(a, env_token, single),
            headers=(None, "Basic rejected", "Bearer", "Bearer \t", "", "Digest rejected"))
    p.step("add-before-first-load", {"op": "add", "row": row("early", a)})
    for name in ("desk", "default", "daemon", "maintainer", "../invalid"):
        p.step("add-refuses-" + name.replace("../", ""), {"op": "add", "row": row(name, bad)})
    inspect("adds-do-not-renew-availability-or-admit-exclusions", names=("early", "desk", "default", "daemon", "maintainer", "../invalid"),
            tokens=(a, bad))
    rows = [row(" Laptop ", a, "core"), row("quiet", b, "full", False),
            row("gone", bad, "minimal", revoked=True), row("pending", tier="admin"),
            row("DESK", bad), row("default", bad), row("Daemon", bad), row("Maintainer", bad),
            row("../invalid", bad), row(".hidden", bad), row("env-conflict", env_token),
            row("encoded", unicode_token), row("nbsp", nbsp_token),
            row("guest", p.secret("guest"), owned=False)]
    p.step("refresh-normalization", {"op": "refresh", "rows": rows, "bank": "0123456789abcdef"})
    inspect("normalization-exclusion-board-tier", names=("laptop", "quiet", "gone", "pending", "desk", "daemon"),
            tokens=(a, b, bad, env_token, single))
    inspect("environment-precedes-stored-same-secret", names=("env-conflict",), tokens=(env_token,))
    inspect("nonascii-and-latin1-header-candidates-http-whitespace", tokens=(unicode_token, nbsp_token),
            headers=("bearer \t" + unicode_token + " \t", "Bearer " + unicode_token.encode("utf-8").decode("latin-1"),
                     "Bearer " + nbsp_token.encode("utf-8").decode("latin-1")))
    inspect("allowlist-before-stored-board-reserved-refused", names=("quiet", "gone", "listed", "daemon", "maintainer"),
            allowed=("quiet", "gone", "listed", "daemon", "maintainer"))
    p.step("stale-read", {"op": "refresh", "rows": rows, "age_s": 61})
    inspect("stale-fail-closed-env-survives", tokens=(a, env_token, single))
    p.step("recovered-read", {"op": "refresh", "rows": rows, "age_s": 59})
    inspect("fresh-recovery", tokens=(a,))
    p.step("begin-pre-add-refresh", {"op": "begin_refresh", "rows": []})
    p.step("add-during-refresh", {"op": "add", "row": row("race", a, "minimal")})
    p.step("finish-pre-add-refresh", {"op": "finish_refresh", "rows": []})
    inspect("add-survives-older-read", names=("race",), tokens=(a,))
    p.step("replace-added-hash", {"op": "add", "row": row("race", b, "core")})
    inspect("old-hash-is-dropped", tokens=(a, b))
    p.step("authoritative-later-refresh", {"op": "refresh", "rows": []})
    inspect("later-read-removes-pending-add", names=("race",), tokens=(b,))
    p.step("open-mode", {"op": "reset"})
    p.step("open-mode-stored-row", {"op": "refresh", "rows": [row("laptop", a)]})
    inspect("store-does-not-enable-auth", tokens=(a, b), headers=(None,))
    p.step("reset-for-db", reset)
    p.step("load-empty-db", {"op": "load"})
    inspect("missing-meta-empty-bank", tokens=(a,))
    p.adversarial("meta-table", META_TABLE_SQL)
    p.adversarial("bank-id", "INSERT INTO meta(key,value) VALUES('coordination_bank_id', '" +
                  '"principal-harness-bank"' + "'::jsonb)")
    p.step("load-real-bank-fingerprint", {"op": "load"})
    inspect("bank-fingerprint-from-json-string")
    p.adversarial("nonstring-bank-id", "UPDATE meta SET value='42'::jsonb")
    p.step("load-nonstring-bank-id", {"op": "load"})
    inspect("nonstring-bank-id-has-no-fingerprint")
    p.adversarial("empty-bank-id", "UPDATE meta SET value='\"\"'::jsonb")
    p.step("load-empty-bank-id", {"op": "load"})
    inspect("empty-bank-id-has-no-fingerprint")
    for name, tier, board in (("laptop", "core", True), ("quiet", "full", False),
                              ("expired", None, True), ("desk", None, True),
                              ("pending", "minimal", True), ("gone", None, True)):
        p.invite(name, tier, board)
    # v59: someone else's machine, kept so by a re-invite without the flag.
    p.invite("guest", owned=False)
    p.invite("guest")
    for name in ("default", "daemon", "maintainer"):
        p.invite(name, adversarial=True)
    p.adversarial("expiry", "UPDATE principals SET code_expires_at=1000 WHERE principal='expired'",
                  {("expired", "code_expires_at"): None})
    p.adversarial("unpaired-row", "INSERT INTO principals(principal,created_at) VALUES('unpaired',1000)",
                  {("unpaired", "created_at"): None})
    p.step("revoke-pending", {"op": "revoke", "name": "gone"}, offsets={("gone", "revoked_at"): 0})
    revoked_token = p.secret("revoked-live-code")
    p.adversarial("revoked-with-live-codes",
                  "UPDATE principals SET code_hash=%s, code_expires_at="
                  "EXTRACT(EPOCH FROM clock_timestamp())::double precision+600, "
                  "paired_code_hash=%s, token_hash=%s, paired_at="
                  "EXTRACT(EPOCH FROM clock_timestamp())::double precision WHERE principal='gone'",
                  {("gone", "code_expires_at"): 600, ("gone", "paired_at"): 0},
                  (p.codes["gone"], p.codes["gone"], h(revoked_token)))
    p.step("list-pending-expired-revoked-unpaired", {"op": "list"})
    p.step("load-pending-states", {"op": "load"})
    def redeem(label, name, token, **kw):
        p.step(label, {"op": "redeem", "code_hash": p.codes[name], "token_hash": h(token), **kw},
               offsets={(name, "paired_at"): 0})
    redeem("expired-code-refused", "expired", a)
    redeem("revoked-code-refused", "gone", a)
    redeem("revoked-live-code-refused", "gone", p.secret("revoked-replacement"))
    redeem("revoked-live-retry-refused", "gone", revoked_token)
    redeem("shadowed-code-unspent", "desk", a)
    for name in ("default", "daemon", "maintainer"):
        redeem("reserved-code-unspent-" + name, name, a)
    redeem("redeem-immediate-add", "laptop", a, add=True)
    inspect("immediately-visible-operator-refusal", names=("laptop",), tokens=(a, env_token, single))
    redeem("same-token-retry", "laptop", a)
    redeem("excluded-same-token-retry-still-answers", "laptop", a, excluded=["laptop"])
    redeem("different-token-retry-refused", "laptop", b)
    redeem("unique-token-refused-code-unspent", "quiet", a)
    redeem("unique-refusal-rollback-proven", "quiet", b, add=True)
    p.step("list-paired", {"op": "list"})
    p.invite("laptop", "minimal", False, replace=True)
    p.step("load-replacement-pending", {"op": "load"})
    inspect("old-token-lives-until-new-redemption", tokens=(a,))
    c = p.secret("c")
    redeem("replacement-redemption", "laptop", c, add=True)
    inspect("replacement-drops-old-token", names=("laptop",), tokens=(a, c))
    p.adversarial("retry-window-aged", "UPDATE principals SET paired_at=1000 WHERE principal='laptop'",
                  {("laptop", "paired_at"): None})
    redeem("retry-window-expired-clears-hash", "laptop", c)
    p.step("revoke-paired", {"op": "revoke", "name": "quiet"}, offsets={("quiet", "revoked_at"): 0})
    p.step("revoke-idempotent-time-preserved", {"op": "revoke", "name": "quiet"})
    p.step("revoke-missing", {"op": "revoke", "name": "absent"})
    inspect("revocation-visible-after-refresh-only", tokens=(b,))
    p.step("load-revoked", {"op": "load"})
    inspect("revoked-identity-refused", names=("quiet",), tokens=(b,))
    p.step("list-final-states", {"op": "list"})
    p.invite("raced")
    p.step("two-redeemers-one-conditional-winner", {"op": "redeem_race", "name": "raced",
        "code_hash": p.codes["raced"], "token_hashes": [h(p.secret("race-a")), h(p.secret("race-b"))]},
        offsets={("raced", "paired_at"): 0})
    p.adversarial("load-error", "ALTER TABLE principals RENAME COLUMN board TO broken_board")
    p.step("failed-load-keeps-snapshot", {"op": "load"})
    inspect("failed-load-preserves-state", names=("laptop", "quiet"), tokens=(c, b))
    p.adversarial("load-restored", "ALTER TABLE principals RENAME COLUMN broken_board TO board")
    p.step("load-recovered", {"op": "load"})
    p.adversarial("missing-table", "DROP TABLE principals")
    p.step("missing-table-loads-empty", {"op": "load"})
    inspect("missing-table-no-ddl", names=("laptop",), tokens=(c,))


def run(binary, golden, mutant=None):
    p = Replay(binary, golden, mutant)
    try:
        scenarios(p)
        p.finish()
        return {"cases": len(p.records), "db_mutations": sum(r["state"] is not None for r in p.records),
                "diffs": p.differences, "records": p.records}
    finally:
        p.close()


RACE_CASE = "two-redeemers-one-conditional-winner"
RACE_PROOF = {"both_queued": True, "winners": 1, "durable_winner_matches": True}


def run_failures(records):
    """The harness's own checks a recording needs beyond the golden: the
    redemption race proved two queued callers and one durable winner."""
    race = [record["output"] for record in records if record["case"] == RACE_CASE]
    return [] if race == [RACE_PROOF] else [f"{RACE_CASE} did not prove {RACE_PROOF}: {race}"]


def record_candidate(records, write, binary, failures=()):
    """``--record-candidate``: class each candidate step against the golden
    record at its position with the replay's own comparison (``step_diffs``),
    then, with ``write``, write the golden: unchanged records kept exactly as
    committed, changed and new ones from the candidate, provenance under
    ``candidate_record.PROVENANCE_KEY``. A dry run exits 1 when the golden
    would change; a failed own check, a host path or a record that would not
    survive the file is refused and nothing is written."""
    committed = json.loads(GOLDEN.read_text(encoding="utf-8"))
    old = committed["records"]
    report = {"unchanged": 0, "changed": [], "new": [], "removed": [], "refused": list(failures),
              "written": False}
    if candidate_record.host_paths(records):
        report["refused"].append("a record names a host path, which a golden must not hold")
    if candidate_record.round_trip(records):
        report["refused"].append("a record does not survive the JSON file")
    merged = []
    for index, record in enumerate(records):
        diffs = step_diffs(old[index] if index < len(old) else None, record)
        if not diffs:
            report["unchanged"] += 1
            merged.append(old[index])
            continue
        report["changed" if index < len(old) else "new"].append(f"{index}:{record['case']}")
        merged.append(record)
        print(f"  {'CHANGED' if index < len(old) else 'new'} {index}:{record['case']}", flush=True)
        for difference in diffs[:20]:
            print(f"        {difference}", flush=True)
    report["removed"] = [f"{index}:{record.get('case')}" for index, record
                         in enumerate(old[len(records):], len(records))]
    for case in report["removed"]:
        print(f"  removed {case}", flush=True)
    if report["refused"]:
        for why in report["refused"]:
            print(f"  REFUSED: {why}", flush=True)
        print(f"  nothing written to {GOLDEN.name}", flush=True)
        return 1, report
    if not (report["changed"] or report["new"] or report["removed"]):
        print(f"  {GOLDEN.name}: nothing to change", flush=True)
        return 0, report
    if not write:
        print(f"  dry run: {GOLDEN.name} would change ({len(report['changed'])} changed, "
              f"{len(report['new'])} new, {len(report['removed'])} removed); review the differences "
              "above, then add --write", flush=True)
        return 1, report
    committed["records"] = merged
    committed[candidate_record.PROVENANCE_KEY] = {"principals": candidate_record.provenance(Path(binary))}
    GOLDEN.write_text(json.dumps(committed, indent=1) + "\n", encoding="utf-8", newline="\n")
    report["written"] = True
    print(f"  wrote {GOLDEN}; one golden serves both operating systems, so check it here and on the "
          f"other OS before committing: {CHECK_COMMAND}", flush=True)
    return 0, report


CHECK_COMMAND = "python rust/daemon/harness/principals.py golden --rust-bin <binary> --out <scratch>/principals.json"


def provenance(binary):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
    binary_hash = None
    if binary:
        digest = hashlib.sha256()
        with binary.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        binary_hash = digest.hexdigest()
    return {"platform": sys.platform, "source_head": head, "source_dirty": bool(dirty),
            "binary_sha256": binary_hash}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("golden", "mutants"))
    parser.add_argument("--rust-bin", type=Path, required=True)
    parser.add_argument("--record-candidate", action="store_true",
                        help="golden only: class each step against the golden; a dry run unless --write")
    parser.add_argument("--write", action="store_true", help="with --record-candidate: write the golden")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mutants", nargs="+", choices=MUTANTS)
    args = parser.parse_args()
    if args.record_candidate and args.mode != "golden":
        parser.error("--record-candidate records the golden run; check mutants afterwards with mutants")
    if args.write and not args.record_candidate:
        parser.error("--write needs --record-candidate")
    if args.record_candidate and candidate_record.refuse_mutant_environment():
        parser.error(candidate_record.refuse_mutant_environment())
    if args.out.resolve() == GOLDEN.resolve():
        parser.error("--out names the committed golden; write the report elsewhere")
    if _DATABASE_TURN_MARKER.exists():
        _turn_sys = __import__("sys")
        _turn_root = str(__import__("pathlib").Path(__file__).resolve().parents[3])
        _turn_sys.path.insert(0, _turn_root)
        try:
            from rust.harness_support.database_turn import require_turn

            require_turn(holder="standalone database harness", contain=True)
        finally:
            _turn_sys.path.remove(_turn_root)
    normalizer_controls()
    prefix_controls = prefix_mutant_control()
    try:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["records"]
        result = run(args.rust_bin, golden)
        recorded = None
        if args.record_candidate:
            # The run's differences from the golden are what it would record.
            recorded, result["record_candidate"] = record_candidate(
                result["records"], args.write, args.rust_bin, run_failures(result["records"]))
            result["recorded_diffs"], result["diffs"] = result["diffs"], []
        if args.mode == "mutants":
            if result["diffs"]:
                raise AssertionError("mutant control differs")
            results = {}
            for mutant in args.mutants or MUTANTS:
                changed = run(args.rust_bin, golden, mutant)
                results[mutant] = {"caught": bool(changed["diffs"]), "diff_cases": changed["diffs"]}
                print(f"{mutant}: {'caught' if changed['diffs'] else 'SURVIVED'}", flush=True)
            result = {"control_cases": result["cases"], "control_db_mutations": result["db_mutations"],
                      "mutants": results, "survivors": [m for m, r in results.items() if not r["caught"]]}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        result["provenance"] = provenance(args.rust_bin)
        result["prefix_controls"] = prefix_controls
        args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k not in ("records", "mutants", "recorded_diffs")}))
        if recorded is not None:
            return recorded
        return int(bool(result.get("diffs") or result.get("survivors")))
    except Exception as exc:
        # Exceptions from database drivers can carry connection parameters.
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"error": type(exc).__name__}) + "\n", encoding="utf-8")
        print("principal harness failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
