"""Positive GET /api/stats parity using the existing memory_stats scenarios.

Parent-only heavy command (prebuilt daemon and offline models required):
  python rust/daemon/harness/test_move_stats.py --rust-bin <binary> \
      --host-port 127.0.0.1:<port> --login-file <test-login.env> --out <result.json>

No shared registry edit is needed. This driver loads a private w2d_search module,
reuses its config, seed, requests, complete stats comparator, coverage and bank
diff, and confines every created/cloned/dropped fixture database to r2_* names.
--self-check is pure: no database, daemon, network, model or subprocess access.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def test_retrieval_event_clock_normalization_keeps_other_columns_exact():
    import copy
    sys.path.insert(0, str(HERE))
    import run

    before = {"rows": {}}
    oracle = {"rows": {"public.retrieval_events": {
        "columns": ["id", "created_at", "query_text", "episode_id", "served"],
        "rows": [[1, 100.25, "fixture query", "fixture episode", [2, 3]]],
    }}}
    candidate = copy.deepcopy(oracle)
    candidate["rows"]["public.retrieval_events"]["rows"][0][1] = 200.5

    def compare(left, right, prior=before):
        return run.dbstate.diff(
            run.dbstate.normalize(left, run.DB_NONDETERMINISTIC, prior),
            run.dbstate.normalize(right, run.DB_NONDETERMINISTIC, prior),
        )

    assert not compare(oracle, candidate)
    for column, value in ((2, "changed query"), (3, "changed episode"), (4, [3, 2])):
        changed = copy.deepcopy(candidate)
        changed["rows"]["public.retrieval_events"]["rows"][0][column] = value
        assert compare(oracle, changed)
    # Existing timestamps and invalid new timestamps remain observable.
    assert compare(oracle, candidate, oracle)
    for value in (None, True, "200.5"):
        changed = copy.deepcopy(candidate)
        changed["rows"]["public.retrieval_events"]["rows"][0][1] = value
        assert compare(oracle, changed)


def test_existing_stats_comparator_accepts_only_clock_differences():
    """Exercise the same comparator used by tokens/tokenless live and replay."""
    import copy
    sys.path.insert(0, str(HERE))
    import run

    case = {"name": "stats", "method": "GET", "path": "/api/stats", "declared": None}

    def response(clock):
        body = {"entry_count": 3, "retrieval_log": {"last_event_at": clock},
                "last_true_drop": {"at": clock}}
        return {"status": 200, "headers": {}, "json": body, "raw": json.dumps(body)}

    oracle, candidate = response(100.25), response(200.5)
    assert not run.compare_case(case, oracle, candidate)["diffs"]
    recorded = run.normalize_response(oracle, case["path"], [])
    recorded.pop("raw")
    recorded["number_tokens"] = run.number_tokens(oracle, recorded)
    assert not run.compare_case(case, recorded, candidate, golden=True)["diffs"]
    # Old recordings containing numeric clock values remain replayable.
    assert not run.compare_case(case, oracle, candidate, golden=True)["diffs"]
    for parent, leaf in (("retrieval_log", "last_event_at"), ("last_true_drop", "at")):
        for invalid in (None, True, 200, "200.5", "<stats clock>", float("nan"), float("inf")):
            changed = copy.deepcopy(candidate)
            changed["json"][parent][leaf] = invalid
            changed["raw"] = json.dumps(changed["json"])
            assert run.compare_case(case, recorded, changed, golden=True)["diffs"]
        changed = copy.deepcopy(candidate)
        del changed["json"][parent][leaf]
        changed["raw"] = json.dumps(changed["json"])
        assert run.compare_case(case, recorded, changed, golden=True)["diffs"]
    changed = copy.deepcopy(candidate)
    changed["json"]["entry_count"] = 4
    changed["raw"] = json.dumps(changed["json"])
    assert run.compare_case(case, recorded, changed, golden=True)["diffs"]
    assert not run.compare_case(case, response(None), response(None))["diffs"]
    for invalid in (True, 100, "<stats clock>", float("nan")):
        assert run.compare_case(case, response(invalid), response(invalid))["diffs"]
    # The same fields on unrelated routes are still exact.
    other = dict(case, path="/api/other")
    assert run.compare_case(other, oracle, candidate)["diffs"]


def register(harness):
    """Reuse the exact three memory_stats scenarios; add REST/MCP/REST pairs.

    A REST case deliberately retains tool='memory_stats' as comparator metadata:
    w2d dispatch selects MCP only by the presence of the 'mcp' key, so these
    requests really hit GET /api/stats and retain the complete stats rules.
    """
    result = {}
    for base in (harness.McpStats, harness.McpStatsContinuum, harness.McpStatsCapacity):
        def cases(self, base=base):
            out = []
            for original in base.cases(self):
                if original.get("tool") != "memory_stats":
                    out.append(original)
                    continue
                name = original["name"]
                headers = [harness.bearer(harness.TOKEN), *original["headers"]]
                rest = {**harness.case(name + " REST before", "/api/stats", headers),
                        "tool": "memory_stats"}
                out.extend([rest, dict(original, name=name + " MCP checkpoint"),
                            dict(rest, name=name + " REST after")])
            return out

        name = base.name.replace("mcp-stats", "api-stats", 1)
        required = dict(base.require)
        required["stats.responses"] *= 3
        result[name] = type(name.replace("-", "_").title(), (base,), {
            "name": name, "cases": cases, "require": required,
            "env": {**base.env, "PSEUDOLIFE_MCP_HOST": "127.0.0.1",
                    "PL_HARNESS_LOOPBACK_BIND": "1"},
        })
    return result


def load_harness():
    # The private module instance owns its pg handle and response observers;
    # imported shared modules and their implementations remain unchanged.
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("_r2_move_stats_service", HERE / "w2d_search.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def bank_space(harness, host_port: str, login_file: Path):
    """Bind the existing guarded DB helpers to explicit loopback test settings."""
    from urllib.parse import urlsplit
    import psycopg
    from rust.cli_harness.run_names import RunNames

    address = urlsplit("//" + host_port)
    host = address.hostname or ""
    if not ipaddress.ip_address(host).is_loopback or address.port is None:
        raise ValueError("--host-port must name a loopback test PostgreSQL endpoint")
    credentials = {}
    for line in login_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if key in ("PSEUDOLIFE_TEST_PG_USER", "PSEUDOLIFE_TEST_PG_PASSWORD"):
                credentials[key] = value.strip()
    user = credentials.get("PSEUDOLIFE_TEST_PG_USER")
    password = credentials.get("PSEUDOLIFE_TEST_PG_PASSWORD")
    if not user or not password:
        raise ValueError("--login-file must contain the two test-login fields")

    class Banks(harness.pgdisposable.Slice):
        def __init__(self):
            super().__init__("w2d")
            self.PREFIX = "r2_"
            self.DISPOSABLE_NAME = re.compile(r"r2_[a-z0-9_]{1,40}")
            self.NAMES = RunNames()
            self.HOST_PORT = host_port

        def name(self, label):
            if not label.startswith("pl_cf_w2d_"):
                raise ValueError("unexpected service fixture label")
            label = "r2_" + label.removeprefix("pl_cf_w2d_")
            return self.NAMES.name(self._check(label), max_length=43)

        def _login(self):
            return user, password

        def dsn(self, name):
            self.NAMES.require_owned(name)
            return super().dsn(name)

        def create(self, name, template=None):
            self.NAMES.require_owned(name)
            if template is not None:
                self.NAMES.require_owned(template)
            return super().create(name, template=template)

        def _admin(self):
            self.NAMES.require_process()
            return psycopg.connect(host=host, port=address.port, user=user, password=password,
                                   dbname="postgres", autocommit=True, connect_timeout=10)

    return Banks()


@contextmanager
def observed_stats(harness, expected_groups: int):
    """Assert success and compare each REST/MCP/REST group within each arm.

    Cross-language responses and all durable state still use run_scenario's
    existing comparison. These additional observations reject two matching
    refusals and any counter or stats-field mutation caused by a route read.
    """
    original_call, original_mcp = harness.call, harness.mcp_call
    pending = {}
    counts = {}

    def body(port, response):
        if response["status"] != 200:
            raise AssertionError("authenticated stats must succeed with HTTP 200")
        value = response.get("json")
        if not isinstance(value, dict) or not isinstance(value.get("bands"), list) \
                or not isinstance(value.get("total_memories"), int):
            raise AssertionError("stats response lacks the complete service snapshot")
        if not {"reference_bank_size", "reference_document_count", "retrieval_log", "read_audit",
                "communities", "graph_digest_at"} <= value.keys():
            raise AssertionError("stats response omitted service or telemetry fields")
        counts.setdefault(port, {"rest": 0, "mcp": 0, "unchanged": 0})
        return value

    def unchanged(before, after):
        response = lambda value: {"status": 200, "headers": {"content-type": "stats"}, "json": value}
        differences, _ = harness.compare(response(before), response(after), stats=True)
        if differences:
            raise AssertionError("stats read changed its service snapshot: " + "; ".join(differences[:3]))

    def rest(port, method, path, *args, **kwargs):
        response = original_call(port, method, path, *args, **kwargs)
        if method == "GET" and path == "/api/stats":
            value = body(port, response)
            if counts[port]["rest"] % 2 == 0:
                pending[port] = value
            else:
                unchanged(pending.pop(port), value)
                counts[port]["unchanged"] += 1
            counts[port]["rest"] += 1
        return response

    def mcp(port, case, *args, **kwargs):
        response = original_mcp(port, case, *args, **kwargs)
        if case.get("tool") == "memory_stats":
            value = body(port, response)
            if port not in pending:
                raise AssertionError("MCP stats checkpoint was not preceded by its REST snapshot")
            unchanged(pending[port], value)
            counts[port]["mcp"] += 1
        return response

    harness.call, harness.mcp_call = rest, mcp
    evidence = {}
    try:
        yield evidence
        expected = {"rest": expected_groups * 2, "mcp": expected_groups,
                    "unchanged": expected_groups}
        if pending or len(counts) != 2 or any(value != expected for value in counts.values()):
            raise AssertionError("positive stats observations did not cover both daemon arms")
        evidence.update({"arms": 2, "http_200": expected_groups * 4,
                         "mcp_checkpoints": expected_groups * 2,
                         "unchanged_read_groups": expected_groups * 2})
    finally:
        harness.call, harness.mcp_call = original_call, original_mcp


def self_check():
    # Execute only the existing declarative cases, not harness imports or DB code.
    source = ast.parse((HERE / "w2d_search.py").read_text(encoding="utf-8"))
    selected = {"case", "mcp_case", "McpStats", "McpStatsContinuum", "McpStatsCapacity"}
    nodes = [node for node in source.body if getattr(node, "name", None) in selected]
    from types import SimpleNamespace
    scope = {"Scenario": type("Scenario", (), {"env": {}, "config_yaml": None}),
             "TOKEN": "fixture-token", "bearer": lambda token: ("Authorization", "Bearer " + token),
             "json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "stats-declarative-cases", "exec"), scope)
    harness = SimpleNamespace(**scope)
    scenarios = register(harness)
    for name, scenario in scenarios.items():
        cases = scenario().cases()
        route = [case for case in cases if case.get("path") == "/api/stats"]
        assert len(route) == scenario.require["stats.responses"] // 3 * 2
        assert all(case["method"] == "GET" and "mcp" not in case and case["headers"]
                   == [("Authorization", "Bearer fixture-token")] for case in route)
        if name == "api-stats":
            assert scenario.seed_flags == harness.McpStats.seed_flags
        assert scenario.config_yaml == next(base.config_yaml for base in scenario.__bases__)
        assert scenario.env["PSEUDOLIFE_MCP_HOST"] == "127.0.0.1"
        print(name + ": original seed/config/operations retained; authenticated REST checkpoints present")
    snapshot = {"bands": [], "total_memories": 1, "interaction_count": 3,
                "reference_bank_size": 0, "reference_document_count": 0,
                "retrieval_log": {}, "read_audit": {}, "communities": 1, "graph_digest_at": 1}

    def observer_fixture(response):
        fake = SimpleNamespace(call=lambda *a, **k: response(),
                               mcp_call=lambda *a, **k: response())
        fake.compare = lambda a, b, **k: ([], []) if a["json"] == b["json"] else (["changed field"], [])
        return fake

    answer = lambda: {"status": 200, "json": dict(snapshot)}
    fake = observer_fixture(answer)
    with observed_stats(fake, 1) as evidence:
        for arm in (1, 2):
            fake.call(arm, "GET", "/api/stats")
            fake.mcp_call(arm, {"tool": "memory_stats"})
            fake.call(arm, "GET", "/api/stats")
    assert evidence == {"arms": 2, "http_200": 4, "mcp_checkpoints": 2, "unchanged_read_groups": 2}
    for response in (lambda: {"status": 501, "json": {}},
                     lambda: {"status": 200, "json": {"bands": [], "total_memories": 1}}):
        fake = observer_fixture(response)
        try:
            with observed_stats(fake, 1):
                fake.call(1, "GET", "/api/stats")
        except AssertionError:
            pass
        else:
            raise AssertionError("stats observer accepted a refusal or incomplete shape")
    current = {"body": dict(snapshot)}
    fake = observer_fixture(lambda: {"status": 200, "json": dict(current["body"])})
    try:
        with observed_stats(fake, 1):
            fake.call(1, "GET", "/api/stats")
            current["body"]["interaction_count"] = 4
            fake.mcp_call(1, {"tool": "memory_stats"})
    except AssertionError as error:
        assert str(error).startswith("stats read changed its service snapshot:")
    else:
        raise AssertionError("stats observer accepted a changing snapshot")
    print("observer controls: both-arm success required; 501, missing fields and changing snapshots rejected")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--rust-bin", type=Path)
    parser.add_argument("--host-port")
    parser.add_argument("--login-file", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", nargs="+", default=["api-stats", "api-stats-continuum", "api-stats-capacity"])
    parser.add_argument("--paragraphs", type=int, default=150)
    args = parser.parse_args()
    if args.self_check:
        return self_check()
    if not args.rust_bin or not args.rust_bin.is_file() or not args.host_port \
            or not args.login_file or not args.login_file.is_file() or not args.out:
        parser.error("supply --rust-bin, --host-port, --login-file and --out; no installed config is read")
    with tempfile.TemporaryDirectory(prefix="r2_stats_") as directory:
        os.environ["PL_HARNESS_SCRATCH"] = directory
        os.environ["PSEUDOLIFE_TEST_PG_HOST_PORT"] = args.host_port
        os.environ["PSEUDOLIFE_TEST_PG_LOGIN_FILE"] = str(args.login_file.resolve())
        harness = load_harness()
        harness.pg = banks = bank_space(harness, args.host_port, args.login_file)
        scenarios = register(harness)
        if set(args.only) - scenarios.keys():
            parser.error("unknown stats scenario")
        results = []
        args.out.write_text(json.dumps({"complete": False, "results": []}), encoding="utf-8")
        try:
            template = banks.name("pl_cf_w2d_template")
            harness.seed_template(template, args.paragraphs)
            for name in args.only:
                scenario = scenarios[name]()
                groups = sum("mcp" in case and case.get("tool") == "memory_stats"
                             for case in scenario.cases())
                with observed_stats(harness, groups) as evidence:
                    result = harness.run_scenario(scenario, args.rust_bin.resolve(), template, "live", False)
                result["route_assertions"] = evidence
                results.append(result)
                args.out.write_text(json.dumps({"complete": False, "results": results}, indent=2), encoding="utf-8")
        finally:
            for name in reversed(list(banks.NAMES.labels)):
                banks.drop(name)
        failures = sum(bool(row["db_diffs"]) or any(case["diffs"] for case in row["cases"])
                       for row in results)
        args.out.write_text(json.dumps({"complete": True, "results": results,
                                       "failed_scenarios": failures}, indent=2), encoding="utf-8")
        return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
