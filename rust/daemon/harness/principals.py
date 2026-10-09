"""Principal-store differential harness, using the existing disposable DB and
catalog/row dumper. Run `live --rust-bin ... --record`, `golden`, or `mutants`.

Secrets are generated in memory, never included in results or failure text.
Clock values are checked against each arm's database-clock window before they
become event labels; unchanged timestamps keep their original event labels.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
os.environ["PL_HARNESS_SLICE"] = "prn"
import psycopg
import dbstate
import pgdisposable as pg
from pseudolife_memory import principal_store as oracle
from pseudolife_memory import principals as identities
from pseudolife_memory.storage.schema import PRINCIPALS_SCHEMA_SQL, SCHEMA_SQL

GOLDEN = HERE / "goldens" / "principals.json"
MUTANTS = ["principal-shadow", "principal-unavailable", "principal-add-race",
           "principal-add-replace", "principal-revoked", "principal-redeem-expiry",
           "principal-redeem-retry", "principal-redeem-excluded"]
CLOCK_COLUMNS = {"created_at", "code_expires_at", "paired_at", "revoked_at"}
NOW = "SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision"


class ClockState:
    """Reject wrong timestamps before normalizing a declared write event."""
    def __init__(self, fixture_role="fixture-role"):
        self.values = {}
        self.fixture_role = fixture_role

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
        return state


def normalizer_controls():
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
        return 8
    raise AssertionError("clock normalizer hid an undeclared rewrite")


def prefix_admission_controls(check=pg._check):
    if check("pl_cf_prn_1") != "pl_cf_prn_1":
        raise AssertionError("principal fixture prefix refused")
    for name in ("pl_cf_prn2_1", "pl_cf_pr_1", "pl_cf_principal_1"):
        try:
            check(name)
        except ValueError:
            continue
        raise AssertionError("near-miss disposable prefix admitted")
    return 3


def prefix_mutant_control():
    refused = prefix_admission_controls()
    source = (HERE / "pgdisposable.py").read_text(encoding="utf-8")
    original = "if not DISPOSABLE_NAME.fullmatch(name):"
    if source.count(original) != 1:
        raise AssertionError("disposable admission mutant anchor changed")
    mutant = source.replace(original, "if not name.startswith(PREFIX[:-2]):")
    namespace = {}
    exec(compile(mutant, "<disposable-admission-mutant>", "exec"), namespace)
    try:
        prefix_admission_controls(namespace["_check"])
    except AssertionError as exc:
        if str(exc) != "near-miss disposable prefix admitted":
            raise
        return {"near_miss_refusals": refused, "mutant_caught": True}
    raise AssertionError("disposable admission source mutant survived")


class PythonArm:
    def __init__(self, dsn):
        self.dsn = dsn
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.env = ({}, None)
        self.clock = 10000.0
        self.snapshot = oracle.PrincipalSnapshot(clock=lambda: self.clock)
        self.pending = None

    def call(self, v):
        op = v["op"]
        if op == "reset":
            self.env = (identities.parse_token_map(v.get("env_map")), v.get("single"))
            self.snapshot = oracle.PrincipalSnapshot(
                shadowed=set(v.get("shadowed", [])) | set(self.env[0].values()), clock=lambda: self.clock)
            self.pending = None
        elif op in ("refresh", "begin_refresh", "finish_refresh"):
            rows = [oracle.StoredPrincipal(**r) for r in v.get("rows", [])]
            if op == "begin_refresh":
                # A blocked loader captures Python's real refresh entry path.
                entered, release = threading.Event(), threading.Event()
                result = {"rows": [], "bank": None}
                old_clock = self.clock
                self.clock -= v.get("age_s", 0)
                def loader():
                    entered.set()
                    if not release.wait(15):
                        raise TimeoutError("refresh fixture timed out")
                    return result["rows"], result["bank"]
                thread = threading.Thread(target=lambda: self.snapshot.refresh(loader), daemon=True)
                thread.start()
                if not entered.wait(10):
                    raise TimeoutError("refresh fixture did not enter")
                self.clock = old_clock
                self.pending = (release, thread, result)
            elif op == "finish_refresh":
                release, thread, result = self.pending
                result.update(rows=rows, bank=v.get("bank"))
                release.set(); thread.join(10)
                if thread.is_alive():
                    raise TimeoutError("refresh fixture did not finish")
                self.pending = None
            else:
                self.clock -= v.get("age_s", 0)
                self.snapshot.refresh(lambda: (rows, v.get("bank")))
                self.clock += v.get("age_s", 0)
        elif op == "add":
            self.snapshot.add(oracle.StoredPrincipal(**v["row"]))
        elif op == "load":
            try:
                self.snapshot.refresh(lambda: oracle.load_rows(self.conn))
            except psycopg.Error:
                return {"ok": False, "error": "database"}
        elif op == "list":
            return {"rows": oracle.list_principals(self.conn)}
        elif op == "revoke":
            return {"found": oracle.revoke(self.conn, v["name"])}
        elif op == "redeem":
            row = oracle.redeem(self.dsn, v["code_hash"], v["token_hash"],
                                excluded=v.get("excluded", self.snapshot.excluded_names))
            if row and v.get("add"):
                self.snapshot.add(row)
            return {"row": asdict(row) if row else None}
        elif op == "redeem_race":
            results = []
            failures = []
            pids = []
            connections = [psycopg.connect(self.dsn, autocommit=True) for _ in range(2)]
            for conn in connections:
                pids.append(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            # Route the oracle's two _connect calls to the already observed
            # connections, so the queued-backend check proves this very race.
            local = threading.local()
            old_connect = oracle._connect
            oracle._connect = lambda *a, **kw: local.conn
            holder = psycopg.connect(self.dsn)
            holder.execute("SELECT principal FROM principals WHERE principal=%s FOR UPDATE", (v["name"],))
            def compete(conn, token):
                try:
                    local.conn = conn
                    conn.autocommit = False
                    results.append(oracle.redeem(self.dsn, v["code_hash"], token))
                except Exception as exc:
                    failures.append(type(exc).__name__)
            threads = [threading.Thread(target=compete, args=(conn, token))
                       for conn, token in zip(connections, v["token_hashes"])]
            try:
                for thread in threads:
                    thread.start()
                deadline = time.monotonic() + 4
                queued = False
                while time.monotonic() < deadline:
                    count = self.conn.execute("SELECT count(*) FROM pg_stat_activity "
                                              "WHERE pid=ANY(%s) AND wait_event_type='Lock'", (pids,)).fetchone()[0]
                    if count == 2:
                        queued = True
                        break
                    time.sleep(.01)
                holder.rollback()
                for thread in threads:
                    thread.join(10)
                    if thread.is_alive():
                        raise TimeoutError("concurrent oracle redemption did not finish")
            finally:
                holder.close()
                oracle._connect = old_connect
                for conn in connections:
                    conn.close()
            winners = [row for row in results if row]
            durable = self.conn.execute("SELECT token_hash FROM principals WHERE principal=%s", (v["name"],)).fetchone()[0]
            valid = (len(winners) == 1 and winners[0].principal == v["name"]
                     and winners[0].token_hash == durable and durable in v["token_hashes"])
            if failures or len(results) != 2 or not queued or not valid:
                raise AssertionError("oracle race did not prove two queued callers and one durable winner")
            return {"both_queued": queued, "winners": len(winners), "durable_winner_matches": valid}
        elif op == "inspect":
            resolved = []
            for header in v.get("headers", []):
                try:
                    principal, source = identities.resolve_principal_detailed(header, *self.env, self.snapshot)
                    status = "principal" if principal is not None else "unauthorized"
                except identities.PrincipalsUnavailable:
                    principal = source = None
                    status = "unavailable"
                resolved.append({"status": status, "principal": principal, "source": source,
                                 "config_allowed": source != identities.SOURCE_STORE,
                                 "notice_allowed": source != identities.SOURCE_STORE,
                                 "stats_allowed": True})
            return {"available": self.snapshot.available(), "len": len(self.snapshot),
                    "bank": self.snapshot.bank, "shadowed": self.snapshot.shadowed_rows,
                    "invalid": self.snapshot.invalid_rows, "excluded": sorted(self.snapshot.excluded_names),
                    "names": [{"principal": n, "has": self.snapshot.has(n),
                               "admitted": self.snapshot.admitted(n), "tier": self.snapshot.tier_of(n),
                               "board_allowed": identities.principal_admitted(
                                   SimpleNamespace(allowed_principals=v.get("allowed", [])), n, store=self.snapshot)}
                              for n in v.get("names", [])], "resolved": resolved}
        else:
            raise AssertionError("unknown oracle operation")
        return {"ok": True}

    def close(self):
        if self.pending:
            self.pending[0].set(); self.pending[1].join(10)
        self.conn.close()


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


class Paired:
    def __init__(self, binary=None, mutant=None, golden=None):
        self.names = [f"pl_cf_prn_{os.getpid()}_{i}" for i in (1, 2)]
        self.dsns = []
        self.arms = []
        self.trackers = []
        self.records = []
        self.differences = []
        self.golden = golden
        self.codes = {}
        self.raw_secrets = []
        self.race_tokens = {}
        self.race_winners = [{}, {}]
        self.rng = random.Random(53)
        try:
            for name in self.names:
                dsn = pg.create(name)
                self.dsns.append(dsn)
                with psycopg.connect(dsn, autocommit=True) as conn:
                    fixture_role = conn.execute("SELECT current_user").fetchone()[0]
                    self.trackers.append(ClockState(fixture_role))
                    conn.execute(PRINCIPALS_SCHEMA_SQL)
            self.arms = ([PythonArm(self.dsns[0])] if golden is None else [None]) + [
                RustArm(binary, self.dsns[1], mutant) if binary else PythonArm(self.dsns[1])]
        except BaseException:
            self.close()
            raise

    def secret(self, label):
        value = hashlib.sha256(("principal-harness-synthetic:" + label).encode()).hexdigest()
        self.raw_secrets.append(value)
        return value

    def code(self):
        value = "".join(self.rng.choice(identities.PAIRING_CODE_ALPHABET) for _ in range(12))
        self.raw_secrets.append(value)
        return value

    def safe(self, value):
        text = json.dumps(value, allow_nan=False)
        if any(secret in text for secret in self.raw_secrets):
            raise AssertionError("raw synthetic credential reached output")
        return value

    def step(self, label, v=None, *, mutation=None, offsets=None):
        index = len(self.records)
        outputs, states = [], []
        for i, (arm, dsn) in enumerate(zip(self.arms, self.dsns)):
            if arm is None:
                outputs.append(self.golden[index]["output"])
                states.append(self.golden[index].get("state"))
                continue
            with psycopg.connect(dsn, autocommit=True) as conn:
                before = conn.execute(NOW).fetchone()[0]
                answer = mutation(conn, i) if mutation else arm.call(v)
                after = conn.execute(NOW).fetchone()[0]
            state = None
            if mutation is not None or (v and v["op"] in ("redeem", "redeem_race", "revoke")):
                state = self.trackers[i].normalize(dbstate.dump(dsn), label,
                                                  (before, after), offsets)
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
                            winner = self.race_winners[i].setdefault(name, row[idx])
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
                            stored, normalized = self.trackers[i].values[(row["principal"], column)]
                            if value != stored:
                                raise AssertionError("list timestamp differs from durable row")
                            row[column] = normalized
            outputs.append(self.safe(answer)); states.append(self.safe(state))
        diffs = dbstate.diff(outputs[0], outputs[1])
        if states[0] is not None or states[1] is not None:
            diffs += dbstate.diff(states[0], states[1], "/database")
        if diffs:
            # Paths only: never print credential-bearing inputs or DB errors.
            self.differences.append({"case": label, "diffs": diffs})
        self.records.append({"case": label, "output": outputs[0], "state": states[0]})

    def invite(self, name, tier=None, board=True, replace=False, ttl=900, adversarial=False):
        code = self.code()
        self.codes[name] = identities.secret_sha256(code)
        def mutate(conn, _i):
            old = oracle.new_pairing_code
            oracle.new_pairing_code = lambda: code
            try:
                result = oracle.create_invite(conn, name, tier=tier, board=board,
                                              ttl_seconds=ttl, replace=replace)
                return {"state": result["state"], "tier": result["tier"], "board": result["board"]}
            finally:
                oracle.new_pairing_code = old
        offsets = {(name, "created_at"): 0, (name, "code_expires_at"): ttl}
        self.step(("adversarial-invite-" if adversarial else "invite-") + name +
                  ("-replace" if replace else ""), mutation=mutate, offsets=offsets)

    def adversarial(self, label, query, offsets=None, params=()):
        def mutate(conn, _i):
            conn.execute(query, params)
            return {"adversarial_fixture": True}
        self.step("adversarial-" + label, mutation=mutate, offsets=offsets)

    def close(self):
        errors = []
        for arm in self.arms:
            if arm is not None:
                try:
                    arm.close()
                except Exception as exc:
                    errors.append(type(exc).__name__)
        for name in self.names:
            try:
                pg.drop(name)
            except Exception as exc:
                errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError("principal harness cleanup failed")


def scenarios(p):
    p.step("initial-oracle-schema", mutation=lambda _conn, _i: {"oracle_schema": True})
    env_token, single = p.secret("env"), p.secret("single")
    a, b, bad = (p.secret(n) for n in ("a", "b", "bad"))
    unicode_token = p.secret("nonascii") + "é"
    nbsp_token = unicode_token + "\u00a0"
    p.raw_secrets.extend((unicode_token, nbsp_token))
    h = identities.secret_sha256
    def row(name, token=None, tier=None, board=True, revoked=False):
        return {"principal": name, "token_hash": h(token) if token else None,
                "tier": tier, "board": board, "revoked": revoked}
    def inspect(label, names=(), tokens=(), headers=(), allowed=()):
        p.step(label, {"op": "inspect", "names": list(names),
                       "headers": ["Bearer " + token for token in tokens] + list(headers),
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
            row("encoded", unicode_token), row("nbsp", nbsp_token)]
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
    p.adversarial("meta-table", SCHEMA_SQL.split(";", 1)[0] + ";")
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
    for name in ("default", "daemon", "maintainer"):
        p.invite(name, adversarial=True)
    p.adversarial("expiry", "UPDATE principals SET code_expires_at=1000 WHERE principal='expired'",
                  {("expired", "code_expires_at"): None})
    p.adversarial("unpaired-row", "INSERT INTO principals(principal,created_at) VALUES('unpaired',1000)",
                  {("unpaired", "created_at"): None})
    p.step("revoke-pending", {"op": "revoke", "name": "gone"}, offsets={("gone", "revoked_at"): 0})
    p.step("list-pending-expired-revoked-unpaired", {"op": "list"})
    p.step("load-pending-states", {"op": "load"})
    def redeem(label, name, token, **kw):
        p.step(label, {"op": "redeem", "code_hash": p.codes[name], "token_hash": h(token), **kw},
               offsets={(name, "paired_at"): 0})
    redeem("expired-code-refused", "expired", a)
    redeem("revoked-code-refused", "gone", a)
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


def run(binary, mutant=None, golden=None):
    p = Paired(binary, mutant, golden)
    try:
        scenarios(p)
        return {"cases": len(p.records), "db_mutations": sum(r["state"] is not None for r in p.records),
                "diffs": p.differences, "records": p.records}
    finally:
        p.close()


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
    parser.add_argument("mode", choices=("live", "golden", "mutants", "oracle"))
    parser.add_argument("--rust-bin", type=Path)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mutants", nargs="+", choices=MUTANTS)
    args = parser.parse_args()
    normalizer_controls()
    prefix_controls = prefix_mutant_control()
    if args.mode != "oracle" and args.rust_bin is None:
        parser.error("--rust-bin required")
    try:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["records"] if args.mode == "golden" else None
        result = run(args.rust_bin, golden=golden)
        if args.record and not result["diffs"]:
            GOLDEN.write_text(json.dumps({"normalizers": "per-arm database-clock windows; verified fixture owners; negative controls=8",
                                         "records": result["records"]}, indent=1) + "\n", encoding="utf-8")
        if args.mode == "mutants":
            if result["diffs"]:
                raise AssertionError("mutant control differs")
            results = {}
            for mutant in args.mutants or MUTANTS:
                changed = run(args.rust_bin, mutant)
                results[mutant] = {"caught": bool(changed["diffs"]), "diff_cases": changed["diffs"]}
                print(f"{mutant}: {'caught' if changed['diffs'] else 'SURVIVED'}", flush=True)
            result = {"control_cases": result["cases"], "control_db_mutations": result["db_mutations"],
                      "mutants": results, "survivors": [m for m, r in results.items() if not r["caught"]]}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        result["provenance"] = provenance(args.rust_bin)
        result["prefix_controls"] = prefix_controls
        args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k not in ("records", "mutants")}))
        return int(bool(result.get("diffs") or result.get("survivors")))
    except Exception as exc:
        # Exceptions from database drivers can carry connection parameters.
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"error": type(exc).__name__}) + "\n", encoding="utf-8")
        print("principal harness failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
