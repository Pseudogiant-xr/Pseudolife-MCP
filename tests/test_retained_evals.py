"""Model-free contracts of the retained Rust quality drivers."""
import ast
import builtins
from collections import deque
from contextlib import contextmanager, nullcontext
from fnmatch import fnmatchcase
import gzip
import json
from os.path import normpath
from pathlib import Path
import re
import subprocess
import tomllib
from types import SimpleNamespace

import pytest

from evals import lme_rejudge, lme_scoring, longmemeval_bench as lme, retained_daemon
from evals.retained_artifacts import read_json, record
from evals.retained_daemon import BenchError, drain_dream


class Bank:
    def __init__(self, extractor):
        self.extractor = extractor
        self.operations = []
        self.pending = 0
        self.cursor = 0

    def store(self, text):
        self.operations.append(("store", text))
        self.pending += 1

    def tool(self, name, **args):
        self.operations.append((name, args))
        if name == "memory_dream":
            count, self.pending = self.pending, 0
            self.cursor += count
            return {"pulled": count, "extractor": "primary", "cursor": self.cursor}
        assert name == "memory_history"
        return {"versions": [{"value": "old"}, {"value": "new"}]}

    def search(self, query, top_k):
        self.operations.append(("search", query, top_k))
        return {"entries": [{"text": "live raw" if self.extractor else "control raw"}],
                "cortex": ([{"entity": "build", "attribute": "tool", "value": "new"}]
                           if self.extractor else [])}


def question():
    return {"question_id": "q1", "question_type": "knowledge-update", "question": "Which tool?",
            "answer": "new", "question_date": "2024/01/03 (Wed) 00:00",
            "haystack_dates": ["2024/01/02 (Tue) 00:00", "2024/01/01 (Mon) 00:00"],
            "haystack_sessions": [[{"role": "user", "content": "new"}],
                                  [{"role": "user", "content": "old"}]]}


def test_longmemeval_uses_isolated_no_dream_control_and_drains_each_session():
    banks = []

    @contextmanager
    def open_bank(extractor):
        bank = Bank(extractor)
        banks.append(bank)
        yield bank

    row = lme.extract_question(question(), open_bank)

    active, control = banks
    assert [step[0] for step in active.operations] == [
        "store", "memory_dream", "memory_dream", "store", "memory_dream", "memory_dream",
        "search", "memory_history",
    ]
    assert active.operations[0][1].endswith("user: old")
    assert active.operations[3][1].endswith("user: new")
    assert [step[0] for step in control.operations] == ["store", "store", "search"]
    assert row["contexts"]["rag_control"] == "control raw"
    assert row["contexts"]["rag"] == "live raw"
    assert "old" in row["contexts"]["cortex"]
    assert "live raw" in row["contexts"]["hybrid"]


@pytest.mark.parametrize("removed_again", [False, True])
def test_set_history_labels_only_removed_members_as_former(removed_again):
    class SetHistory:
        def tool(self, name, **args):
            assert name == "memory_history"
            versions = [
                {"value": "lint", "event": "added"},
                {"value": "format", "event": "added"},
                {"value": "scan", "event": "added"},
                {"value": "format", "event": "removed"},
                {"value": "scan", "event": "removed"},
                {"value": "FORMAT", "event": "added"},
            ]
            if removed_again:
                versions.append({"value": "FORMAT", "event": "removed"})
            return {"kind": "set", "versions": versions}

    current = "lint" if removed_again else "lint; FORMAT (2 members)"
    former = "scan; FORMAT" if removed_again else "scan"
    hits = {"cortex": [{"entity": "build", "attribute": "checks", "value": current}]}
    assert lme.fact_lines(SetHistory(), hits) == [f"build / checks: {current} (former members: {former})"]


def test_dream_retries_a_held_batch_and_counts_only_acknowledged_work():
    replies = iter([
        {"pulled": 2, "extractor_failed": True, "hold_phase": "extract", "extractor": "primary"},
        {"pulled": 2, "claims": 1, "quarantined": 1, "cursor": 3.0, "extractor": "primary"},
        {"pulled": 0, "extractor": "primary"},
    ])

    def tool(name, **args):
        assert name == "memory_dream" and args == {"action": "run", "limit": 100}
        return next(replies)

    tally = drain_dream(tool, stored=2)
    assert tally["claims"] == tally["quarantined"] == 1
    with pytest.raises(StopIteration):
        next(replies)


@pytest.mark.parametrize(("reply", "cause"), [
    ({"pulled": 0, "error": "offline"}, "offline"),
    ({"pulled": 0, "skipped": "turn closed"}, "skipped: turn closed"),
    ({"pulled": 0, "acknowledgement_failed": True}, "acknowledgement_failed"),
    ({"pulled": 0, "events_pass_failed": True}, "events_pass_failed"),
    ({"pulled": 0, "extractor": "fallback"}, "received fallback"),
    ({"pulled": 0, "extractor": "primary"}, "before acknowledging"),
])
def test_dream_does_not_treat_an_incomplete_cell_as_measured(reply, cause):
    with pytest.raises(BenchError, match=cause):
        drain_dream(lambda name, **args: reply, stored=1)


@pytest.mark.parametrize(("verdict", "expected"), [
    ("  (YES), because...", True), ("'No!'\n", False), (" _YES_ followed by detail", True),
])
def test_judge_uses_the_first_casefolded_word(verdict, expected):
    assert lme_scoring.judge_answer(question(), "answer", lambda *a, **kw: verdict) is expected


@pytest.mark.parametrize("verdict", ["unsettled: yes", "yesplease", " ... "])
def test_unparseable_judge_verdict_reports_the_raw_reply(verdict):
    with pytest.raises(BenchError, match=re.escape(repr(verdict))):
        lme_scoring.judge_answer(question(), "answer", lambda *a, **kw: verdict)


class Instrument:
    def __init__(self, name, reply):
        self.name = name
        self.reply = reply
        self.calls = []

    def provenance(self):
        return {"served_model": self.name}

    def verify(self):
        return None

    def chat(self, system, user, *, max_tokens):
        self.calls.append((system, user))
        return self.reply(user)


def test_answer_resume_and_independent_judge_use_the_saved_rust_contexts(tmp_path, monkeypatch):
    source, alternative = tmp_path / "primary", tmp_path / "alternative"
    q1, q2 = question(), {**question(), "question_id": "q2_abs", "answer": "I don't know"}
    manifest = {"daemon": {"engine": "rust"}, "question_ids": ["q1", "q2_abs"]}

    @contextmanager
    def open_bank(active):
        yield Bank(active)

    def answer(user):
        return "new" if "build / tool:" in user else "I don't know"

    def grade(user):
        expected = user.split("Correct answer: ")[1].split("\n")[0]
        return "yes" if user.endswith(f"Model response: {expected}") else "no"

    lme.run_extract(source, manifest, [q1, q2], open_bank)
    old = read_json(source / "contexts" / "0000.json")
    with pytest.raises(BenchError, match="already records different"):
        record(source / "contexts" / "0000.json", {**old, "question": "altered"})

    answerer, judge = Instrument("answerer", answer), Instrument("judge", grade)
    assert lme.run_answer(source, answerer, judge)["n"] == 2
    assert len(answerer.calls) == len(judge.calls) == 8
    assert lme.run_answer(source, answerer, judge)["arms"]["cortex"]["accuracy"] == 0.5
    assert len(answerer.calls) == len(judge.calls) == 8

    with pytest.raises(BenchError, match="different judge model"):
        lme_rejudge.run(source, alternative, judge)

    def record_outside_archive(path, value):
        assert not path.is_relative_to(source), "rejudge wrote into the archived primary run"
        return record(path, value)

    monkeypatch.setattr(lme, "record", record_outside_archive)
    second = Instrument("second", lambda _user: "no")
    assert lme_rejudge.run(source, alternative, second)["arms"]["cortex"]["accuracy"] == 0
    assert len(answerer.calls) == 8 and len(second.calls) == 8
    assert read_json(alternative / "comparison.json")["disagreements"]["cortex"] == 1


def test_projected_gate_fixture_keeps_only_public_write_inputs(tmp_path):
    from evals import rebuild_contexts

    q = question()
    dump = {**q, "facts": [
        {"entity": "build", "attribute": "tool", "value": "new", "history": ["old", "new"],
         "embedding": [0.1, 0.9], "tx_time": 100},
        {"entity": "build", "attribute": "checks", "kind": "member", "value": "lint"},
        {"entity": "build", "attribute": "checks", "kind": "member", "value": "format"},
    ]}
    with gzip.open(tmp_path / "q1.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(dump, fh)
    fixture = rebuild_contexts.project(tmp_path, [q])
    cells = rebuild_contexts.fixture_cells(fixture, [q])
    assert cells["q1"] == [
        {"entity": "build", "attribute": "tool", "kind": "scalar", "values": ["old", "new"]},
        {"entity": "build", "attribute": "checks", "kind": "set", "values": ["lint", "format"]},
    ]
    with pytest.raises(BenchError, match="question"):
        rebuild_contexts.fixture_cells(fixture, [{**q, "question": "wrong corpus"}])


def test_gate_refuses_missing_or_mismatched_baseline_before_answering(tmp_path):
    from evals import regression_gate as gate

    manifest = {"engine": "rust", "policy": "test", "question_ids": ["q1"]}
    path = tmp_path / "baseline.json"
    with pytest.raises(BenchError, match="missing"):
        gate.baseline_for(path, manifest)
    baseline = {"manifest": manifest, "revision": "0" * 40, "control_score_sha256": "1" * 64,
                "binary_sha256": "2" * 64, "replicates": 2,
                "arms": {arm: {"mean": 0.5, "margin": 0.03} for arm in gate.MEASURED_ARMS}}
    record(path, baseline)
    assert gate.baseline_for(path, manifest) == baseline
    with pytest.raises(BenchError, match="does not match"):
        gate.baseline_for(path, {**manifest, "engine": "python"})


def test_gate_instrument_digests_cover_acquisition_rendering_and_scoring(monkeypatch):
    from evals import regression_gate as gate

    source = gate.instrument_digests()
    assert set(source) == {"regression_gate.py", "longmemeval_bench.py", "context_format.py",
                           "replicate.py", "lme_scoring.py", "rebuild_contexts.py", "retained_daemon.py",
                           "regression_gate.ps1", "qwen_server.ps1"}
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, **kw: original(path, **kw) + "# changed\n"
                        if path.name == "context_format.py" else original(path, **kw))
    changed = gate.instrument_digests()
    assert {name for name in source if source[name] != changed[name]} == {"context_format.py"}


@pytest.mark.parametrize(("fault", "expected"), [
    ("login", "pseudolife-mcp test-login create"),
    ("driver", "Install psycopg"),
    ("connection", "Start the test PostgreSQL"),
    ("role", "cannot create a disposable database"),
    ("turn", "fixture turn is held"),
    ("stale", "settings changed after harness import"),
    ("unexpected", "LookupError"),
])
def test_gate_preflight_rejects_database_failures_as_incomplete(tmp_path, monkeypatch, capsys, fault, expected):
    from evals import regression_gate as gate
    from rust.daemon import harness

    class ConnectionFailure(Exception):
        pass

    def admin():
        if fault == "connection":
            raise ConnectionFailure("connection refused")
        if fault == "role":
            def query(sql):
                assert "rolcreatedb OR rolsuper" in sql
                return SimpleNamespace(fetchone=lambda: (False,))

            return nullcontext(SimpleNamespace(execute=query))
        if fault == "turn":
            raise SystemExit("fixture turn is held")
        if fault == "unexpected":
            raise LookupError("fixture connection state unavailable")
        pytest.fail("a refused environment must not connect")

    login = tmp_path / "test-login.env"
    if fault != "login":
        login.touch()
    fake = SimpleNamespace(HOST_PORT="127.0.0.1:5434", LOGIN_FILE=login,
                           BASE_ENV="PL_HARNESS_BASE_DATABASE", _admin=admin,
                           psycopg=SimpleNamespace(OperationalError=ConnectionFailure))
    fake.Slice = lambda name: fake
    monkeypatch.setattr(harness, "pgdisposable", fake, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_TEST_PG_HOST_PORT", fake.HOST_PORT)
    if fault == "stale":
        monkeypatch.setenv("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5435")
    monkeypatch.setenv("PSEUDOLIFE_TEST_PG_LOGIN_FILE", str(login))
    for name in (fake.BASE_ENV, "PGHOSTADDR", "PGSERVICE", "PGOPTIONS"):
        monkeypatch.delenv(name, raising=False)
    if fault == "driver":
        importer = builtins.__import__

        def missing_driver(name, *args, **kwargs):
            if name == "rust.daemon.harness":
                raise ImportError("no psycopg")
            return importer(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", missing_driver)

    q = {**question(), "contexts": {"rag": "fixture control"}}
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}", encoding="utf-8")
    binary = tmp_path / "binary"
    binary.touch()
    monkeypatch.setattr(gate.rebuild_contexts, "pinned_rows", lambda: [q])
    monkeypatch.setattr(gate.rebuild_contexts, "fixture_cells", lambda fixture, rows: {"q1": []})
    monkeypatch.setattr(lme, "load_questions", lambda *args: ("synthetic", [q]))
    monkeypatch.setattr(retained_daemon.DaemonSetup, "provenance",
                        lambda self: {"embedding_files": {}, "ort_sha256": "fixture"})
    monkeypatch.setattr(gate, "run", lambda *a, **kw: pytest.fail("preflight must not judge"))
    args = ["--preflight", "--establish", "--fixture", str(fixture), "--data", str(fixture),
            "--out", str(tmp_path / "out"), "--daemon", str(binary), "--daemon-revision", "0" * 40,
            "--onnx-dir", str(tmp_path), "--ort-library", str(binary), "--scratch", str(tmp_path)]
    for role in ("answerer", "judge"):
        args += [f"--{role}-url", "http://127.0.0.1:9/v1", f"--{role}-model", "unused",
                 f"--{role}-profile", "fixture"]
    assert gate.main(args) == 2
    assert expected in capsys.readouterr().err


def test_daemon_error_excerpts_and_saved_log_remove_credentials(tmp_path, capsys, monkeypatch):
    token = "fixture-bearer-secret"
    dsn = "postgresql://fixture:fixture-db-password@127.0.0.1:5434/fixture"
    errors = {"status": 200, "headers": [], "body": json.dumps({"error": {"message": f"bad slot {dsn} {token}"}})}
    with pytest.raises(BenchError) as raised:
        retained_daemon.rpc_result(errors, "memory_search", secrets=(token,))
    message = str(raised.value)
    assert "bad slot" in message and token not in message and dsn not in message
    tool_error = {"status": 200, "headers": [], "body": json.dumps({"result": {
        "isError": True, "content": [{"text": f"slot rejected {token} {dsn}"}],
    }})}
    bank = retained_daemon.Bank(SimpleNamespace(port=9, tool=lambda *args: tool_error), token)
    with pytest.raises(BenchError) as raised:
        bank.tool("memory_fact_set")
    assert "slot rejected" in str(raised.value) and token not in str(raised.value) and dsn not in str(raised.value)

    response = SimpleNamespace(status=503, read=lambda size: f"bank unavailable {token} {dsn}".encode())
    connection = SimpleNamespace(request=lambda *args, **kw: None, getresponse=lambda: response,
                                 close=lambda: None)
    monkeypatch.setattr(retained_daemon.http.client, "HTTPConnection", lambda *a, **kw: connection)
    with pytest.raises(BenchError) as raised:
        bank.get("/api/search")
    assert "HTTP 503: bank unavailable" in str(raised.value)
    assert token not in str(raised.value) and dsn not in str(raised.value)
    source, output = tmp_path / "daemon.log", tmp_path / "output" / "daemon-error"
    source.write_text(f"cursor failed for {token} and {dsn}\n", encoding="utf-8")
    retained_daemon.save_failure_log(source, output, (token,))
    [saved] = list(output.parent.glob("daemon-error-*.log"))
    detail = saved.read_text(encoding="utf-8")
    assert "cursor failed" in detail and token not in detail and dsn not in detail
    assert str(saved) in capsys.readouterr().err


def test_extractor_readback_distinguishes_request_alias_and_served_id(monkeypatch):
    endpoint = retained_daemon.Endpoint("http://127.0.0.1:9/v1", "custom", "premapped", "fixture.gguf")
    status = {"extractor_mode": "primary", "primary_url": endpoint.url, "primary_model": endpoint.model}
    bank = retained_daemon.Bank(SimpleNamespace(port=9), "test-token")
    monkeypatch.setattr(bank, "tool", lambda *args, **kwargs: status)
    calls = []

    def models(origin, method, path, **kwargs):
        calls.append((origin.netloc, method, path))
        return {"data": [{"id": endpoint.expected_model}]}

    monkeypatch.setattr(retained_daemon, "request_json", models)
    bank.verify_extractor(endpoint)
    assert calls == [("127.0.0.1:9", "GET", "/v1/models")]
    status["primary_model_served"] = endpoint.expected_model
    bank.verify_extractor(endpoint)
    assert len(calls) == 1
    status["primary_model"] = "wrong-alias"
    with pytest.raises(BenchError, match="pinned primary extractor identity"):
        bank.verify_extractor(endpoint)
    status.update(primary_model=endpoint.model, primary_model_served="wrong.gguf")
    with pytest.raises(BenchError, match="pinned primary extractor identity"):
        bank.verify_extractor(endpoint)


@pytest.mark.parametrize("spawn_failed", [True, False])
def test_daemon_startup_keeps_failure_detail_and_releases_log(tmp_path, monkeypatch, spawn_failed):
    dsn = "postgresql://fixture:private-test-password@127.0.0.1:5434/fixture"
    token = "private-test-token"
    binary = tmp_path / "daemon"
    setup = retained_daemon.DaemonSetup(binary, "0" * 40, tmp_path, binary,
                                        tmp_path, tmp_path / "output" / "daemon-error")
    daemon = SimpleNamespace(proc=None, log=tmp_path / "daemon.log")

    def start():
        daemon._out = daemon.log.open("w", encoding="utf-8")
        daemon._out.write(f"fixture startup detail {dsn} {token}\n")
        if spawn_failed:
            raise PermissionError(f"spawn refused {dsn}")
        daemon.proc = object()

    def stop():
        if daemon.proc is not None:
            daemon._out.close()

    daemon.start, daemon.stop = start, stop
    session = SimpleNamespace(init={"status": 200, "headers": [], "body": '{"result": {}}'},
                              sid="fixture-session",
                              initialized={"status": 404, "body": f"unknown route {token}"}, close=lambda: None)
    monkeypatch.setattr(retained_daemon.daemons, "free_port", lambda: 9)
    monkeypatch.setattr(retained_daemon.daemons, "rust_daemon", lambda *args: daemon)
    monkeypatch.setattr(retained_daemon, "token_urlsafe", lambda n: token)
    monkeypatch.setattr(retained_daemon.mcp_wire, "Session", lambda *args: session)
    expected = "PermissionError: spawn refused" if spawn_failed else "HTTP 404: unknown route"
    with pytest.raises(BenchError, match=expected) as error:
        list(setup._serve(tmp_path, dsn, None))
    assert daemon._out.closed
    [saved] = list(setup.failure_log.parent.glob("daemon-error-*.log"))
    log = saved.read_text(encoding="utf-8")
    assert "fixture startup detail" in log
    assert all(secret not in str(error.value) + log for secret in (dsn, token, "private-test-password"))


def test_gate_detects_row_level_replica_drift_even_when_averages_match():
    from evals import regression_gate as gate

    def row(qid, yes):
        return {"question_id": qid, "contexts": dict.fromkeys(lme.ARMS, "a"),
                **{f"{arm}_response": "a" for arm in lme.ARMS},
                **{f"{arm}_correct": yes for arm in lme.ARMS},
                **{f"{arm}_context_tokens": 1 for arm in lme.ARMS}}

    good = [row("q1", True), row("q2", False)]
    flipped = [row("q1", False), row("q2", True)]
    with pytest.raises(BenchError, match="replicates disagree"):
        gate.aggregate([good, flipped])


def test_gate_scores_regression_and_rejects_control_drift(tmp_path):
    from evals import regression_gate as gate

    @contextmanager
    def open_bank():
        yield Bank(True)

    identity = {"revision": "0" * 40, "binary_sha256": "1" * 64}
    manifest = {"engine": "rust", "question_ids": ["q1"]}
    fixed = {"q1": "frozen raw"}
    slots = {"q1": []}
    judge = Instrument("judge", lambda prompt: "yes" if prompt.endswith("response: new") else "no")
    all_correct = Instrument("answerer", lambda _prompt: "new")
    establish = tmp_path / "establish"
    assert gate.run(establish, manifest, identity, [question()], fixed, slots, open_bank,
                    all_correct, judge, repetitions=2, baseline=None) == 0
    baseline = gate.baseline_for(establish / "baseline-candidate.json", manifest)
    assert gate.run(tmp_path / "pass", manifest, identity, [question()], fixed, slots, open_bank,
                    all_correct, judge, repetitions=2, baseline=baseline) == 0
    assert gate.run(tmp_path / "quarantine-warning", manifest, identity, [question()], fixed,
                    slots, open_bank, all_correct, judge, repetitions=2,
                    baseline={**baseline, "quarantined": 2}) == 0
    assert read_json(tmp_path / "quarantine-warning" / "verdict.json")["warnings"] == [
        "quarantined entries changed from 2 to 0",
    ]

    regression = Instrument("answerer", lambda prompt: "new" if "frozen raw" in prompt else "old")
    assert gate.run(tmp_path / "regression", manifest, identity, [question()], fixed, slots, open_bank,
                    regression, judge, repetitions=2, baseline=baseline) == 1

    drift = Instrument("answerer", lambda _prompt: "old")
    with pytest.raises(BenchError, match="drifted on the pinned raw control"):
        gate.run(tmp_path / "drift", manifest, identity, [question()], fixed, slots, open_bank,
                 drift, judge, repetitions=2, baseline=baseline)


def test_ladder_keeps_the_naive_control_fixed_across_rungs():
    from evals import ladder_sweep as ladder

    contexts = [[pair["gold"]] for pair in ladder.PAIRS]
    naive = ladder.measure(contexts)
    assert naive["gold_recoverable"] == 1
    assert naive["stale_leak"] == 0
    control = ladder.control_measurement(contexts, {"engine": "rust"})
    assert ladder.check_control(control, {"engine": "rust"}) == naive
    with pytest.raises(BenchError, match="control"):
        ladder.check_control(control, {"engine": "python"})


def test_ladder_abstention_has_the_historical_grid_rates_and_per_cell_tallies(monkeypatch):
    from evals import ladder_sweep as ladder

    @contextmanager
    def open_bank(config, extractor):
        yield Bank(extractor)

    monkeypatch.setattr(ladder, "drain_dream", lambda *a, **kw: {"claims": 2, "quarantined": 1})
    rows = ladder.abstention(open_bank, "stub", {})
    assert {(row["guard"], row["floor"]) for row in rows} == {
        (guard, floor) for guard in (0.3, 0.5, 0.65, 0.75, 0.85)
        for floor in (0.0, 0.5, 0.65, 0.70, 0.75, 0.80)
    }
    assert all(row["abstain_recall_unanswerable"] == row["false_abstain_answerable"] == 0 for row in rows)
    assert all(row["consolidation"] == {"claims": 2, "quarantined": 1} for row in rows)


def local_dependencies(root, path, tracked):
    source = (root / path).read_text(encoding="utf-8-sig")
    if path.suffix == ".ps1":
        code = re.sub(r"<#.*?#>", "", source, flags=re.DOTALL)
        names = re.findall(r"['\"][^'\"\r\n]*?\b([\w.-]+\.(?:py|ps1))['\"]",
                           "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#")))
        return {path.parent / name for name in names} & tracked

    def resolve(name, level=0):
        dirs = [path.parents[level - 1]] if level else [path.parent, Path(), Path("evals")]
        for directory in dirs:
            prefix = directory.joinpath(*name.split("."))
            if prefix.name and prefix.with_suffix(".py") in tracked:
                return prefix.with_suffix(".py")
            if prefix / "__init__.py" in tracked:
                return prefix / "__init__.py"
        return None

    dependencies = set()
    for node in ast.walk(ast.parse(source, filename=path.as_posix())):
        if isinstance(node, ast.Import):
            dependencies.update(resolve(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            dependencies.add(resolve(module, node.level))
            prefix = module + "." if module else ""
            dependencies.update(resolve(prefix + alias.name, node.level) for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            arg = node.args[0]
            if (name in {"__import__", "import_module", "run_module"}
                    and isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                dependencies.add(resolve(arg.value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.fullmatch(r"(?:[\w.-]+[/\\])*[\w.-]+\.(?:py|ps1)", node.value):
                relative = Path(node.value.replace("\\", "/"))
                candidates = {Path(normpath(relative)), Path(normpath(path.parent / relative))}
                dependencies.update(candidates & tracked)
    return dependencies - {None}


def frozen_imports_from(root, kept, tracked):
    edges = {}
    violations = []
    for start in kept:
        pending = deque([(start,)])
        visited = set()
        while pending:
            chain = pending.popleft()
            path = chain[-1]
            if path.parts[0] == "evals" and path.name.startswith("_python_"):
                violations.append(" -> ".join(item.as_posix() for item in chain))
                break
            if path in visited:
                continue
            visited.add(path)
            if path not in edges:
                edges[path] = local_dependencies(root, path, tracked)
            pending.extend((*chain, dependency) for dependency in sorted(edges[path]))
    return violations


def test_kept_evals_do_not_import_frozen_implementations():
    root = Path(__file__).resolve().parents[1]
    tracking = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=True)
    tracked = {Path(path) for path in tracking.split("\0") if path}
    with (root / "rust/ci/package_consumers.toml").open("rb") as stream:
        rules = tomllib.load(stream)["rules"]
    retiring = [glob for rule in rules if rule.get("retire_with_package") for glob in rule["globs"]]
    kept = [path for path in sorted(tracked) if path.parts[0] == "evals" and path.suffix in {".py", ".ps1"}
            and not any(fnmatchcase(path.as_posix(), pattern) for pattern in retiring)]
    assert retiring and Path("evals/longmemeval_bench.py") in kept
    assert not frozen_imports_from(root, kept, tracked)


def test_frozen_import_check_follows_indirect_and_literal_dynamic_imports(tmp_path):
    sources = {"evals/active.py": "from evals import helper\n",
               "evals/helper.py": "import importlib\nimportlib.import_module('evals._python_old')\n",
               "evals/_python_old.py": ""}
    tracked = {Path(path) for path in sources}
    (tmp_path / "evals").mkdir()
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    assert frozen_imports_from(tmp_path, [Path("evals/active.py")], tracked) == [
        "evals/active.py -> evals/helper.py -> evals/_python_old.py",
    ]


@pytest.mark.parametrize(("active", "literal"), [
    (Path("evals/active.py"), "_python_old.py"),
    (Path("evals/nested/active.py"), "../_python_old.py"),
])
def test_frozen_import_check_follows_python_script_path_literals(tmp_path, active, literal):
    evals = tmp_path / "evals"
    (tmp_path / active.parent).mkdir(parents=True)
    (tmp_path / active).write_text(f'Path(__file__).parent / "{literal}"', encoding="utf-8")
    (evals / "_python_old.py").write_text("", encoding="utf-8")
    tracked = {active, Path("evals/_python_old.py")}
    assert frozen_imports_from(tmp_path, [active], tracked) == [
        f"{active.as_posix()} -> evals/_python_old.py",
    ]
