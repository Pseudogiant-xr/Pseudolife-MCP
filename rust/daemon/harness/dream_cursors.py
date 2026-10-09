"""Dream cursor differential runner; live, recorded goldens and source mutants.

Usage: python dream_cursors.py live --rust-bin PATH [--record] [--out FILE]
       python dream_cursors.py golden --rust-bin PATH [--goldens DIR]
       python dream_cursors.py mutants --rust-bin PATH [--only NAME ...]
Every action, including rejection, injected fault and restart, compares the
response by type/value and all database tables/catalogs through dbstate.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("PL_HARNESS_SLICE", "w3d")
import daemons
import dbstate
import pgdisposable as pg
from run import DB_NONDETERMINISTIC, diff_values, golden_scrub

SECRET = "11" * 32  # Synthetic fixture secret, never an operator credential.
MUTANTS = ("dream-pull-reverse", "dream-pull-ignore-source", "dream-token-no-signature",
           "dream-ack-all-pending", "dream-cursor-rewind", "dream-ack-skip-write",
           "dream-init-classify-explicit")
MUTANT_SCENARIOS = {
    "dream-pull-reverse": "membership", "dream-pull-ignore-source": "membership",
    "dream-token-no-signature": "tokens", "dream-ack-all-pending": "membership",
    "dream-cursor-rewind": "classification", "dream-ack-skip-write": "membership",
    "dream-init-classify-explicit": "classification",
}


def scenarios() -> list[dict]:
    entries = [
        {"text": "legacy old", "ts": 9.0, "dream_state": None},
        {"text": "legacy boundary", "ts": 10.0, "dream_state": None},
        {"text": "legacy new", "ts": 11.0, "dream_state": None},
        {"text": "excluded old", "ts": 1.0, "source": "status", "dream_state": None},
        {"text": "explicit pending", "ts": 5.0, "dream_state": "pending"},
        {"text": "tied first", "ts": 100.0, "episode_id": "synthetic-episode",
         "authority": "directive", "distortion_tolerance": "constraint"},
        {"text": "tied second", "ts": 100.0},
        {"text": "backdated new", "ts": 1.0},
        {"text": "settled", "ts": 2.0, "dream_state": "acknowledged"},
        {"text": "explicit covered", "ts": 3.0, "dream_state": "legacy-covered"},
    ]
    fixture = {"meta": {"cortex_dream_cursor": 10.0, "dream_ack_secret_v1": SECRET},
               "entries": entries}
    def case(name, actions, seed=None, generated=False):
        return {"name": name, "fixture": copy.deepcopy(fixture if seed is None else seed),
                "actions": actions, "generated_secret": generated}
    token_args = {"secret": SECRET, "backend": "postgres", "generation": "synthetic-generation"}
    return [
        case("membership", [
            {"op": "pull", "limit": 2, "save": "batch"},
            {"op": "commit", "token_from": "batch"},
            {"op": "commit", "token_from": "batch"},
            {"op": "pull", "eligible_sources": ["notes"], "limit": 20, "save": "rest"},
            {"op": "fault", "kind": "delete", "entry_ids": [7]},
            {"op": "commit", "token_from": "rest"},
            {"op": "restart"}, {"op": "pull"},
            {"op": "pull", "exclude_sources": []},
        ]),
        case("classification", [
            {"op": "initialize", "eligible_sources": ["notes"]},
            {"op": "initialize", "exclude_sources": ["status"]},
            {"op": "pull", "eligible_sources": ["notes"]},
            {"op": "acknowledge", "entry_ids": [5, 1], "display_timestamp": 20.0},
            {"op": "acknowledge", "entry_ids": [5, 5], "display_timestamp": 20.0},
            {"op": "acknowledge", "entry_ids": [0], "display_timestamp": 20.0},
            {"op": "acknowledge", "entry_ids": [], "display_timestamp": 20.0},
            {"op": "acknowledge", "entry_ids": [7, 6], "display_timestamp": 100.0},
            {"op": "acknowledge", "entry_ids": [7, 6], "display_timestamp": 50.0},
            {"op": "acknowledge", "entry_ids": [3, 999999999], "display_timestamp": 11.0},
            {"op": "acknowledge", "entry_ids": [999999998, 999999999], "display_timestamp": 200.0},
            {"op": "restart"}, {"op": "pull", "limit": 0}, {"op": "pull", "limit": 20},
        ]),
        case("tokens", [
            {"op": "issue", **token_args, "entry_ids": [7, 3], "display_timestamp": -100.0, "save": "pure"},
            {"op": "verify", **token_args, "token_from": "pure"},
            {"op": "verify", **token_args, "token_from": "pure", "tamper": True},
            {"op": "verify", **token_args, "generation": "foreign", "token_from": "pure"},
            {"op": "verify", **token_args, "token": "v1.bad.bad"},
            {"op": "issue", **token_args, "entry_ids": [1, 1], "display_timestamp": 1.0},
            {"op": "issue", **token_args, "entry_ids": [0], "display_timestamp": 1.0},
            {"op": "issue", **token_args, "entry_ids": [], "display_timestamp": 1.0},
            {"op": "issue", **token_args, "entry_ids": list(range(1, 4098)), "display_timestamp": 1.0},
            {"op": "commit", "commit_token": "v1.bad.bad"},
        ]),
        case("rollback", [
            {"op": "initialize"}, {"op": "pull", "save": "batch"},
            {"op": "fault", "kind": "cursor-trigger"},
            {"op": "commit", "token_from": "batch"},
            {"op": "fault", "kind": "drop-cursor-trigger"},
            {"op": "commit", "token_from": "batch"},
            {"op": "restart"}, {"op": "pull"},
        ]),
        case("corrupt-secret", [{"op": "initialize"}, {"op": "pull"}, {"op": "restart"}, {"op": "pull"}],
             {"meta": {"dream_ack_secret_v1": "corrupt", "cortex_dream_cursor": 10.0}, "entries": entries}),
        case("corrupt-cursor", [
            {"op": "initialize"},
            {"op": "fault", "kind": "cursor", "value": "inf"},
            {"op": "acknowledge", "entry_ids": [5], "display_timestamp": 20.0},
            {"op": "fault", "kind": "cursor", "value": 10.0},
            {"op": "restart"}, {"op": "initialize"},
        ]),
        case("invalid-legacy-cursor", [
            {"op": "initialize"}, {"op": "pull"}, {"op": "restart"}, {"op": "pull"},
        ], {"meta": {"cortex_dream_cursor": "inf"},
            "entries": [{"text": "unclassified", "ts": 1.0, "dream_state": None}]}),
        case("generated-secret", [{"op": "initialize"}, {"op": "initialize"}, {"op": "restart"}, {"op": "initialize"}],
             {"entries": [{"text": "legacy", "ts": 1.0, "dream_state": None}]}, True),
    ]


def normalize_secret(value):
    try:
        if isinstance(value, str) and len(value) == 64 and len(bytes.fromhex(value)) == 32:
            return "<generated-32-byte-secret>"
    except ValueError:
        pass
    return value


def stored_secret(state):
    meta = state["rows"].get("public.meta", {})
    cols = meta.get("columns", [])
    if "key" in cols and "value" in cols:
        for row in meta["rows"]:
            if row[cols.index("key")] == "dream_ack_secret_v1":
                return row[cols.index("value")]
    return None


def normalize_state(state: dict, before: dict, generated_secret: bool = False) -> dict:
    normalized = dbstate.normalize(state, DB_NONDETERMINISTIC, before)
    # A fresh golden template seeds relation clocks at a different instant.
    # Only unchanged seed values normalize; rewriting a seed still differs.
    for qualified, prior in before["rows"].items():
        table = normalized["rows"].get(qualified)
        if not table:
            continue
        for index, column in enumerate(prior["columns"]):
            if (qualified.split(".", 1)[1], column) not in DB_NONDETERMINISTIC:
                continue
            old = {json.dumps(row[0], sort_keys=True): row[index] for row in prior["rows"]}
            for row in table["rows"]:
                if json.dumps(row[0], sort_keys=True) in old and row[index] == old[json.dumps(row[0], sort_keys=True)]:
                    row[index] = "<unchanged-seed-clock>"
    if generated_secret:
        meta = normalized["rows"].get("public.meta", {})
        cols = meta.get("columns", [])
        if "key" in cols and "value" in cols:
            for row in meta["rows"]:
                if row[cols.index("key")] == "dream_ack_secret_v1":
                    row[cols.index("value")] = normalize_secret(row[cols.index("value")])
    return normalized


def compare_step(python, rust, python_state, rust_state) -> dict:
    return {"diffs": diff_values(python, rust), "db_diffs": dbstate.diff(python_state, rust_state)}


def golden_state(state: dict, before: dict) -> dict:
    """Keep all row values and catalog changes, with the existing digests.

    The cursor slice does not own constructor DDL or a server's extension
    versions and role name. An unchanged catalog category must equal its own
    seeded template exactly before it can become an invariant marker; changed
    categories retain their complete value digest. Live comparisons stay raw.
    """
    state = copy.deepcopy(state)
    for name, value in state["catalog"].items():
        if value == before["catalog"].get(name):
            state["catalog"][name] = {"unchanged_from_template": True}
        else:
            state["catalog"][name] = golden_scrub({"catalog": {name: value}})["catalog"][name]
    return golden_scrub(state)


class Adapter:
    def __init__(self, argv: list[str], env: dict[str, str], home: Path):
        self.argv, self.env, self.home = argv, env, home
        self.log = home / "adapter.log"
        self.proc = None

    def start(self):
        self.stderr = self.log.open("ab")
        self.proc = subprocess.Popen(self.argv, cwd=self.home, env=self.env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.stderr, text=True, encoding="utf-8")
        self.answers = queue.Queue()
        def reader():
            for line in self.proc.stdout:
                self.answers.put(line)
            self.answers.put(None)
        self.reader = threading.Thread(target=reader, daemon=True)
        self.reader.start()
        return self

    def call(self, action):
        self.proc.stdin.write(json.dumps(action, allow_nan=False) + "\n")
        self.proc.stdin.flush()
        try:
            line = self.answers.get(timeout=60)
        except queue.Empty:
            raise RuntimeError(f"adapter response timeout; log {self.log}") from None
        if line is None:
            raise RuntimeError(f"adapter exited without answer; log {self.log}")
        return json.loads(line)

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write('{"op":"exit"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=20)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                self.proc.kill()  # This Popen handle owns this exact launched process.
                self.proc.wait(timeout=10)
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.reader.join(timeout=2)
        self.stderr.close()
        self.proc = None


def fault(dsn, action):
    import psycopg
    from psycopg.types.json import Jsonb
    with psycopg.connect(dsn, autocommit=True) as conn:
        kind = action["kind"]
        if kind == "delete":
            conn.execute("DELETE FROM entries WHERE id=ANY(%s)", (action["entry_ids"],))
        elif kind == "cursor":
            conn.execute("INSERT INTO meta(key,value) VALUES ('cortex_dream_cursor',%s) "
                         "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (Jsonb(action["value"]),))
        elif kind == "cursor-trigger":
            conn.execute("CREATE FUNCTION fail_dream_cursor_write() RETURNS trigger LANGUAGE plpgsql AS "
                         "$$ BEGIN IF NEW.key = 'cortex_dream_cursor' THEN RAISE EXCEPTION "
                         "'injected cursor failure'; END IF; RETURN NEW; END $$")
            conn.execute("CREATE TRIGGER fail_dream_cursor_write BEFORE INSERT OR UPDATE ON meta "
                         "FOR EACH ROW EXECUTE FUNCTION fail_dream_cursor_write()")
        elif kind == "drop-cursor-trigger":
            conn.execute("DROP TRIGGER fail_dream_cursor_write ON meta")
            conn.execute("DROP FUNCTION fail_dream_cursor_write()")
        else:
            raise ValueError(f"unknown fixture fault: {kind}")
    return {"fault": kind}


def wire_action(action, saved):
    request = {key: value for key, value in action.items() if key not in ("save", "token_from", "tamper")}
    if action.get("token_from"):
        response = saved[action["token_from"]]
        token = response.get("commit_token", response.get("token"))
        if not isinstance(token, str):
            raise RuntimeError(f"oracle did not issue token for {action['token_from']}")
        if action.get("tamper"):
            token = token[:-1] + ("A" if token[-1] != "A" else "B")
        request["commit_token" if action["op"] == "commit" else "token"] = token
    return request


def run_scenario(scenario, binary, root, mode, golden_dir, record=False, mutant=None):
    name = scenario["name"]
    tag = f"{name.replace('-', '_')}_{os.getpid()}"
    template = pg.PREFIX + tag + "_t"
    dbs = {side: pg.PREFIX + tag + suffix for side, suffix in (("python", "_py"), ("rust", "_rs"))}
    if mode == "golden":
        dbs.pop("python")
    procs, dsns = {}, {}
    golden_path = golden_dir / f"dream-cursors-{name}.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8")) if mode == "golden" else None
    try:
        dsn = pg.create(template)
        seed_home = daemons.make_home(root, tag + "_seed", None)
        fixture_path = seed_home / "fixture.json"
        fixture_path.write_text(json.dumps(scenario["fixture"]), encoding="utf-8")
        env = daemons.base_env(seed_home, {"PSEUDOLIFE_MCP_DATABASE_URL": dsn})
        seeded = subprocess.run([sys.executable, str(HERE / "dream_cursor_oracle.py"), "--seed", str(fixture_path)],
                                cwd=seed_home, env=env, capture_output=True, text=True, timeout=60)
        if seeded.returncode:
            raise RuntimeError("Python template seed failed: " + seeded.stderr[-2000:])
        before = dbstate.dump(dsn)
        for side, bank in dbs.items():
            dsns[side] = pg.create(bank, template=template)
            home = daemons.make_home(root, tag + "_" + side, None)
            env = daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": dsns[side]})
            if side == "rust" and mutant:
                env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
            argv = [str(binary)] if side == "rust" else [sys.executable, str(HERE / "dream_cursor_oracle.py")]
            procs[side] = Adapter(argv, env, home).start()
        saved, rows, recordings = {}, [], []
        prior_secrets, prior_database_secrets = {}, {}
        for index, action in enumerate(scenario["actions"]):
            request = wire_action(action, saved)
            answers = {}
            for side, proc in procs.items():
                if action["op"] == "restart":
                    proc.stop()
                    proc.start()
                    # A pure request answers only after Storage has opened;
                    # dumping immediately after Popen races the lease epoch.
                    ready = proc.call({"op": "verify", "backend": "postgres", "secret": SECRET,
                                       "generation": "restart-readiness", "token": "v1.bad.bad"})
                    answers[side] = {"restarted": True, "readiness": ready}
                elif action["op"] == "fault":
                    answers[side] = fault(dsns[side], action)
                else:
                    answers[side] = proc.call(request)
            if mode == "golden":
                answers["python"] = golden["steps"][index]["response"]
            raw_python = answers["python"]
            if action.get("save"):
                saved[action["save"]] = raw_python
            if scenario["generated_secret"]:
                for side, response in answers.items():
                    if "secret" in response:
                        secret = response["secret"]
                        if side in prior_secrets and secret != prior_secrets[side]:
                            raise RuntimeError(f"{side} changed persisted generation on repeat/restart")
                        prior_secrets[side] = secret
                        answers[side] = {**response, "secret": normalize_secret(secret)}
            states = {}
            for side, url in dsns.items():
                raw_state = dbstate.dump(url)
                if scenario["generated_secret"]:
                    secret = stored_secret(raw_state)
                    if side in prior_database_secrets and secret != prior_database_secrets[side]:
                        raise RuntimeError(f"{side} changed stored generation between actions")
                    if secret is not None:
                        prior_database_secrets[side] = secret
                states[side] = normalize_state(raw_state, before, scenario["generated_secret"])
            if mode == "golden":
                states["python"] = golden["steps"][index]["db_state"]
                states["rust"] = golden_state(states["rust"], before)
            result = compare_step(answers["python"], answers["rust"], states["python"], states["rust"])
            rows.append({"index": index, "op": action["op"], **result})
            recordings.append({"response": answers["python"], "db_state": golden_state(states["python"], before)})
        if record:
            golden_dir.mkdir(parents=True, exist_ok=True)
            golden_path.write_text(json.dumps({"scenario": name,
                "normalizers": {"catalog": "exact unchanged-template invariant or changed-category value",
                                "vectors": "existing run.golden_scrub digest",
                                "clocks": "normalize_state preserves changed seeded clocks"},
                "steps": recordings}, indent=2), encoding="utf-8")
        return {"scenario": name, "steps": rows, "actions": len(rows),
                "diff_actions": sum(bool(row["diffs"] or row["db_diffs"]) for row in rows)}
    finally:
        for proc in procs.values():
            proc.stop()
        for bank in [*dbs.values(), template]:
            pg.drop(bank)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("live", "golden", "mutants"))
    parser.add_argument("--rust-bin", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--goldens", type=Path, default=HERE / "goldens")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--ci-fixture", action="store_true",
                        help="allow the isolated loopback fixture and test login under RUNNER_TEMP in CI")
    args = parser.parse_args()
    valid_host = pg.HOST_PORT == "127.0.0.1:5433"
    if args.ci_fixture:
        host, _, port = pg.HOST_PORT.rpartition(":")
        runner_temp = os.environ.get("RUNNER_TEMP")
        valid_host = (os.environ.get("GITHUB_ACTIONS") == "true" and host == "127.0.0.1"
                      and port.isascii() and port.isdigit() and 1 <= int(port) <= 65535
                      and bool(runner_temp)
                      and pg.LOGIN_FILE.resolve().is_relative_to(Path(runner_temp).resolve()))
    if pg.SLICE != "w3d" or not valid_host:
        parser.error("requires slice w3d and local bench 127.0.0.1:5433, or an explicit isolated CI fixture")
    if args.record and args.mode != "live":
        parser.error("--record requires live mode")
    root = Path(tempfile.mkdtemp(prefix="pl-w3d-cursors-"))
    out = args.out or root / "result.json"
    selected = scenarios()
    if args.mode != "mutants" and args.only:
        unknown = set(args.only) - {s["name"] for s in selected}
        if unknown:
            parser.error(f"unknown scenarios: {sorted(unknown)}")
        selected = [s for s in selected if s["name"] in args.only]
    results = []
    if args.mode == "mutants":
        controls = args.only or MUTANTS
        if set(controls) - set(MUTANTS):
            parser.error("unknown dream cursor mutant")
        baseline = [run_scenario(s, args.rust_bin.resolve(), root, "live", args.goldens)
                    for s in selected if s["name"] in {MUTANT_SCENARIOS[m] for m in controls}]
        if any(r["diff_actions"] for r in baseline):
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"mode": args.mode, "baseline": baseline,
                                       "error": "clean control differs; mutants not run"}, indent=2), encoding="utf-8")
            print("clean control differs; mutants not run; result", out, flush=True)
            return 1
        for mutant in controls:
            rows = [run_scenario(s, args.rust_bin.resolve(), root, "live", args.goldens, mutant=mutant)
                    for s in selected if s["name"] == MUTANT_SCENARIOS[mutant]]
            results.append({"mutant": mutant, "caught": any(r["diff_actions"] for r in rows), "scenarios": rows})
            print(mutant, "caught" if results[-1]["caught"] else "SURVIVED", flush=True)
        failed = any(not r["caught"] for r in results)
    else:
        for scenario in selected:
            result = run_scenario(scenario, args.rust_bin.resolve(), root, args.mode, args.goldens, args.record)
            results.append(result)
            print(result["scenario"], result["actions"], "actions", result["diff_actions"], "differ", flush=True)
        failed = any(r["diff_actions"] for r in results)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"mode": args.mode, "results": results}, indent=2), encoding="utf-8")
    print("result", out, flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
