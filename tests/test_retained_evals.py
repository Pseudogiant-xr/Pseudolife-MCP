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


SERVED_GGUF = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
SERVED_PATH = "C:\\Users\\<user>\\llama.cpp\\models\\" + SERVED_GGUF


@contextmanager
def llama_cpp_server(model_id):
    """A local HTTP server answering with the shapes llama.cpp b10488 returned when probed.

    Started with `-m <path>` and no --alias, it advertised the full path in /v1/models
    and reported it as the chat response's model, whatever model the request named.
    """
    import http.server
    import threading

    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            requests.append(("GET", self.path, None))
            self.reply({"models": [{"name": model_id, "model": model_id, "type": "model"}],
                        "object": "list",
                        "data": [{"id": model_id, "aliases": [model_id], "object": "model",
                                  "owned_by": "llamacpp"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("POST", self.path, body["model"]))
            self.reply({"choices": [{"finish_reason": "stop", "index": 0,
                                     "message": {"role": "assistant", "content": "ok"}}],
                        "model": model_id, "system_fingerprint": "b10488-9d77fa172",
                        "object": "chat.completion"})

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("model_id", [SERVED_PATH, "/home/<user>/models/" + SERVED_GGUF, SERVED_GGUF],
                         ids=["windows path", "posix path", "file name"])
def test_endpoint_accepts_llama_cpp_naming_the_pinned_file_by_its_path(model_id):
    with llama_cpp_server(model_id) as (url, requests):
        endpoint = retained_daemon.Endpoint(url, "bench", "fixture-profile", SERVED_GGUF)
        endpoint.verify()
        assert endpoint.chat("Reply with one word.", "Say ok.", max_tokens=8) == "ok"
    assert requests == [("GET", "/v1/models", None), ("POST", "/v1/chat/completions", "bench")]


@pytest.mark.parametrize("model_id", [
    "C:\\Users\\<user>\\llama.cpp\\models\\Qwen3.8-27B-UD-Q8_K_XL.gguf",
    "C:\\Users\\<user>\\llama.cpp\\models\\old-" + SERVED_GGUF,
    SERVED_GGUF + ".bak",
], ids=["another file", "file name as a suffix", "file name as a prefix"])
def test_endpoint_refuses_another_file_and_names_both_files_without_the_path(model_id):
    with llama_cpp_server(model_id) as (url, _):
        endpoint = retained_daemon.Endpoint(url, "bench", "fixture-profile", SERVED_GGUF)
        with pytest.raises(BenchError) as refused:
            endpoint.verify()
        leaf = retained_daemon.served_name(model_id)
        assert f"advertises {leaf!r}, not the expected {SERVED_GGUF!r}" in str(refused.value)
        with pytest.raises(BenchError) as served:
            endpoint.chat("Reply with one word.", "Say ok.", max_tokens=8)
        assert str(served.value) == f"chat completion was served by {leaf!r}, not the expected {SERVED_GGUF!r}"
    for message in (str(refused.value), str(served.value)):
        assert "<user>" not in message and "llama.cpp" not in message


def test_endpoint_pinned_by_a_path_matches_the_file_and_prints_only_its_name():
    pinned = "D:\\Users\\<user>\\models\\" + SERVED_GGUF
    with llama_cpp_server(SERVED_PATH) as (url, _):
        endpoint = retained_daemon.Endpoint(url, "bench", "fixture-profile", pinned)
        endpoint.verify()
        assert endpoint.chat("Reply with one word.", "Say ok.", max_tokens=8) == "ok"
    with llama_cpp_server(SERVED_PATH.replace("Q4_K_XL", "Q8_K_XL")) as (url, _):
        endpoint = retained_daemon.Endpoint(url, "bench", "fixture-profile", pinned)
        with pytest.raises(BenchError) as refused:
            endpoint.verify()
        with pytest.raises(BenchError) as served:
            endpoint.chat("Reply with one word.", "Say ok.", max_tokens=8)
    for message in (str(refused.value), str(served.value)):
        assert f"not the expected {SERVED_GGUF!r}" in message and "<user>" not in message


@pytest.mark.parametrize(("request_model", "served_model", "model_id"), [
    ("bench", None, "/any/dir/bench"),
    ("bench", "Qwen3-8B", "someorg/Qwen3-8B"),
    ("bench", None, "C:\\Users\\<user>\\models\\bench"),
], ids=["request model under a directory", "repo id ending in the pinned name", "windows directory"])
def test_endpoint_keeps_exact_matching_for_ids_that_are_not_gguf_files(request_model, served_model, model_id):
    with llama_cpp_server(model_id) as (url, _):
        endpoint = retained_daemon.Endpoint(url, request_model, "fixture-profile", served_model)
        with pytest.raises(BenchError, match="not the expected"):
            endpoint.verify()
        with pytest.raises(BenchError, match="not the expected"):
            endpoint.chat("Reply with one word.", "Say ok.", max_tokens=8)
    with llama_cpp_server(served_model or request_model) as (url, _):
        retained_daemon.Endpoint(url, request_model, "fixture-profile", served_model).verify()


def test_endpoint_errors_print_hub_ids_that_differ_only_in_org_in_full():
    with llama_cpp_server("org-b/model") as (url, _):
        endpoint = retained_daemon.Endpoint(url, "bench", "fixture-profile", "org-a/model")
        with pytest.raises(BenchError) as refused:
            endpoint.verify()
        assert "advertises 'org-b/model', not the expected 'org-a/model'" in str(refused.value)
        with pytest.raises(BenchError) as served:
            endpoint.chat("Reply with one word.", "Say ok.", max_tokens=8)
        assert str(served.value) == "chat completion was served by 'org-b/model', not the expected 'org-a/model'"


@pytest.mark.parametrize("model_id", [
    "/home/<user>/models/other-model",
    "C:\\Users\\<user>\\models\\other-model",
    "~/models/other-model",
    "\\\\<host>\\models\\other-model",
], ids=["posix", "drive letter", "home", "unc"])
def test_endpoint_errors_print_an_absolute_directory_by_its_leaf(model_id):
    with llama_cpp_server(model_id) as (url, _):
        endpoint = retained_daemon.Endpoint(url, "bench", "fixture-profile")
        with pytest.raises(BenchError) as refused:
            endpoint.verify()
        assert "advertises 'other-model', not the expected 'bench'" in str(refused.value)
        with pytest.raises(BenchError) as served:
            endpoint.chat("Reply with one word.", "Say ok.", max_tokens=8)
        assert str(served.value) == "chat completion was served by 'other-model', not the expected 'bench'"
    for message in (str(refused.value), str(served.value)):
        assert "<user>" not in message and "<host>" not in message and "models" not in message


@pytest.mark.parametrize("primary", [SERVED_PATH, SERVED_GGUF, "/home/<user>/models/" + SERVED_GGUF])
def test_rejudge_refuses_the_primary_judge_pinned_by_path_or_file_name(tmp_path, monkeypatch, primary):
    monkeypatch.setattr(lme, "validated_scores",
                        lambda source: ({}, {"judge": {"served_model": primary}}, []))
    judge = retained_daemon.Endpoint("http://127.0.0.1:9/v1", "bench", "fixture-profile", SERVED_GGUF)
    with pytest.raises(BenchError, match="choose a different judge model"):
        lme_rejudge.run(tmp_path / "primary", tmp_path / "rejudge", judge)
    other = retained_daemon.Endpoint("http://127.0.0.1:9/v1", "bench", "fixture-profile", "other-judge.gguf")
    with pytest.raises(BenchError, match="separate directory"):
        lme_rejudge.run(tmp_path / "same", tmp_path / "same", other)


def test_extractor_readback_accepts_the_served_file_by_its_path(monkeypatch):
    endpoint = retained_daemon.Endpoint("http://127.0.0.1:9/v1", "custom", "premapped", SERVED_GGUF)
    status = {"extractor_mode": "primary", "primary_url": endpoint.url, "primary_model": endpoint.model,
              "primary_model_served": SERVED_PATH}
    bank = retained_daemon.Bank(SimpleNamespace(port=9), "test-token")
    monkeypatch.setattr(bank, "tool", lambda *args, **kwargs: status)
    bank.verify_extractor(endpoint)
    status["primary_model_served"] = SERVED_PATH.replace("Q4_K_XL", "Q8_K_XL")
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


class SlotBank:
    """A public scalar slot whose writes the test scripts: ``echo`` values are
    confirmed onto the standing value; ``lose`` values are parked (contested,
    the standing value returned) and never reach the history; ``replace``
    values are answered "confirmed" with the written value although the
    history keeps the old one; ``misfiled`` values are answered "confirmed" with
    the standing value but name another attribute."""

    def __init__(self, echo=(), lose=(), replace=(), misfiled=()):
        self.echo, self.lose, self.replace = set(echo), set(lose), set(replace)
        self.misfiled = set(misfiled)
        self.versions = []

    def tool(self, name, **args):
        if name == "memory_fact_set":
            value, current = args["value"], self.versions[-1] if self.versions else None
            key = {"entity": args["entity"], "attribute": args["attribute"]}
            if current is None:
                self.versions.append(value)
                return {"action": "inserted", **key, "value": value}
            if value in self.misfiled:
                return {"action": "confirmed", **key, "attribute": "other", "value": current}
            if value == current or value in self.echo:
                return {"action": "confirmed", **key, "value": current}
            if value in self.replace:
                return {"action": "confirmed", **key, "value": value}
            if value in self.lose:
                return {"action": "contested", **key, "value": current}
            self.versions.append(value)
            return {"action": "superseded", **key, "value": value}
        if name == "memory_fact_get":
            return {"record": {"value": self.versions[-1]}}
        assert name == "memory_history"
        return {"versions": [{"value": value} for value in self.versions]}


def scalar(*values):
    return [{"entity": "user", "attribute": "plan", "kind": "scalar", "values": list(values)}]


def test_gate_replay_records_a_compression_echo_the_daemon_kept():
    from evals import regression_gate as gate

    bank = SlotBank(echo={"Plans to sign up"})
    echoes = gate.replay_slots(bank, scalar("Planning to sign up", "Plans to sign up", "Signed up"), "q7")
    assert echoes == [{"question_id": "q7", "entity": "user", "attribute": "plan",
                       "written": "Plans to sign up", "kept": "Planning to sign up"}]
    assert bank.versions == ["Planning to sign up", "Signed up"]
    assert gate.replay_slots(SlotBank(), scalar("old", "old", "new"), "q8") == []
    # A case-only rewrite is the same value to the daemon: kept, and listed the same way.
    assert gate.replay_slots(SlotBank(echo={"ACME"}), scalar("Acme", "ACME"), "q9") == [
        {"question_id": "q9", "entity": "user", "attribute": "plan", "written": "ACME", "kept": "Acme"}]


@pytest.mark.parametrize("bank", [
    SlotBank(lose={"new"}),
    SlotBank(replace={"new"}),
    SlotBank(misfiled={"new"}),
], ids=["a different value lost", "confirmed with a changed value", "confirmed on another key"])
def test_gate_replay_still_refuses_every_other_history_difference(bank):
    from evals import regression_gate as gate

    with pytest.raises(BenchError, match="public scalar history"):
        gate.replay_slots(bank, scalar("old", "new"), "q1")


def test_gate_contexts_and_candidate_list_the_echoes(tmp_path):
    from evals import regression_gate as gate

    class EchoBank(Bank):
        def __init__(self):
            super().__init__(True)
            self.slots = SlotBank(echo={"new tool"})

        def tool(self, name, **args):
            if name.startswith(("memory_fact_", "memory_history")):
                return self.slots.tool(name, **args)
            return super().tool(name, **args)

    @contextmanager
    def open_bank():
        yield EchoBank()

    identity = {"revision": "0" * 40, "binary_sha256": "1" * 64}
    manifest = {"engine": "rust", "question_ids": ["q1"]}
    slots = {"q1": [{"entity": "build", "attribute": "tool", "kind": "scalar",
                     "values": ["the new tool", "new tool"]}]}
    echo = {"question_id": "q1", "entity": "build", "attribute": "tool", "written": "new tool",
            "kept": "the new tool"}
    judge = Instrument("judge", lambda prompt: "yes" if prompt.endswith("response: new") else "no")
    answerer = Instrument("answerer", lambda _prompt: "new")
    args = (manifest, identity, [question()], {"q1": "frozen raw"}, slots, open_bank, answerer, judge)
    root = tmp_path / "establish"
    assert gate.run(root, *args, repetitions=2, baseline=None, contexts_only=True) == 0
    assert not (root / "answers-1.json").exists() and not answerer.calls
    assert read_json(root / "contexts.json")[0]["echo_confirmed"] == [echo]
    assert gate.run(root, *args, repetitions=2, baseline=None) == 0
    candidate = read_json(root / "baseline-candidate.json")
    assert candidate["echo_confirmed"] == [echo]
    baseline = gate.baseline_for(root / "baseline-candidate.json", manifest)
    assert gate.run(tmp_path / "same", *args, repetitions=2, baseline=baseline) == 0
    assert read_json(tmp_path / "same" / "verdict.json")["warnings"] == []
    assert gate.run(tmp_path / "moved", *args, repetitions=2,
                    baseline={**baseline, "echo_confirmed": []}) == 0
    assert read_json(tmp_path / "moved" / "verdict.json")["warnings"] == [
        "fixture writes the daemon kept (echo or equal after normalisation) changed from 0 to 1",
    ]
    unrecorded = tmp_path / "unrecorded"
    record(unrecorded / "contexts.json",
           [{k: v for k, v in row.items() if k != "echo_confirmed"} for row in read_json(root / "contexts.json")])
    with pytest.raises(BenchError, match="checkpoint is incomplete"):
        gate.run(unrecorded, *args, repetitions=2, baseline=None)


def test_gate_parallel_contexts_keep_question_order_and_match_serial(capsys):
    import threading
    import time
    from evals import regression_gate as gate

    questions = [{**question(), "question_id": f"q{index}", "question": f"Which tool {index}?"}
                 for index in range(6)]
    controls = {q["question_id"]: f"frozen {q['question_id']}" for q in questions}
    slots = {q["question_id"]: [] for q in questions}
    active, peak, opened, lock = [0], [0], [0], threading.Lock()

    @contextmanager
    def open_bank():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            opened[0] += 1
            delay = 0.02 * (8 - opened[0] % 8)  # earlier cells finish later
        time.sleep(delay)
        try:
            yield Bank(True)
        finally:
            with lock:
                active[0] -= 1

    serial = gate.build_contexts(questions, controls, slots, open_bank, 1)
    assert peak[0] == 1
    capsys.readouterr()
    parallel = gate.build_contexts(questions, controls, slots, open_bank, 4)
    assert peak[0] > 1
    assert parallel == serial
    assert [row["question_id"] for row in parallel] == [q["question_id"] for q in questions]
    # One progress line per built cell, counted in completion order.
    progress = capsys.readouterr().out.splitlines()
    assert [line.split(" built: ")[0] for line in progress] == [
        f"Context cell {count}/6" for count in range(1, 7)]
    assert sorted(line.split(" built: ")[1] for line in progress) == sorted(controls)


def test_gate_parallel_contexts_wait_in_bounded_steps(monkeypatch):
    """A bounded wait lets Ctrl+C reach the build on Windows between cells."""
    from evals import regression_gate as gate

    timeouts, real_wait = [], gate.wait

    def recording_wait(futures, timeout=None, return_when=None):
        timeouts.append(timeout)
        return real_wait(futures, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(gate, "wait", recording_wait)
    questions = [{**question(), "question_id": f"q{index}"} for index in range(3)]

    @contextmanager
    def open_bank():
        yield Bank(True)

    gate.build_contexts(questions, {q["question_id"]: "raw" for q in questions},
                        {q["question_id"]: [] for q in questions}, open_bank, 2)
    assert timeouts and all(timeout is not None and timeout <= 1 for timeout in timeouts)


class HeldBank(Bank):
    """A cell whose first stored turn holds until the build's stop flag is set."""

    def __init__(self, stop, stored=None):
        super().__init__(True)
        self.stop, self.stored, self.held = stop, stored if stored is not None else [], False

    def store(self, text):
        self.stored.append(text)
        if not self.held:
            self.held = True
            self.stop.wait(10)


def test_gate_parallel_contexts_stop_on_the_first_failed_cell():
    import itertools
    import threading
    import time
    from evals import regression_gate as gate

    questions = [{**question(), "question_id": f"q{index}"} for index in range(12)]
    counter, opened, lock, stop = itertools.count(), [], threading.Lock(), threading.Event()

    @contextmanager
    def open_bank():
        with lock:
            number = next(counter)
            opened.append(number)
        if number == 1:  # a later cell fails while the first is still running
            raise BenchError("cell failed")
        yield HeldBank(stop)

    started = time.monotonic()
    with pytest.raises(BenchError, match="cell failed"):
        gate.build_contexts(questions, {q["question_id"]: "raw" for q in questions},
                            {q["question_id"]: [] for q in questions}, open_bank, 2, stop=stop)
    assert stop.is_set()
    assert len(opened) < len(questions)
    # The failure is seen when it completes, not after the held first cell's 10 s hold.
    assert time.monotonic() - started < 5


def test_gate_parallel_contexts_tell_running_cells_to_stop_at_their_next_turn():
    import threading
    from evals import regression_gate as gate

    long = {**question(), "question_id": "long",
            "haystack_dates": ["2024/01/01 (Mon) 00:00"],
            "haystack_sessions": [[{"role": "user", "content": f"turn {i}"} for i in range(40)]]}
    failing = {**question(), "question_id": "failing"}
    stored, opened, lock, stop = [], [], threading.Lock(), threading.Event()

    @contextmanager
    def open_bank():
        with lock:
            opened.append(1)
            second = len(opened) == 2
        if second:  # fail only once the other cell is storing
            for _ in range(500):
                if stored:
                    break
                threading.Event().wait(0.01)
            raise BenchError("cell failed")
        yield HeldBank(stop, stored)

    with pytest.raises(BenchError, match="cell failed"):
        gate.build_contexts([long, failing], {"long": "raw", "failing": "raw"},
                            {"long": [], "failing": []}, open_bank, 2, stop=stop)
    assert len(stored) == 1  # released by the stop flag, it stored no further turn


def test_gate_context_workers_follow_the_request_or_free_memory(monkeypatch):
    from evals import regression_gate as gate

    cell, reserve = gate.CELL_MEMORY_BYTES, gate.MEMORY_RESERVE_BYTES
    assert gate.context_workers(3, available=reserve + 20 * cell, cpus=1) == 3
    # An explicit count above what free memory allows is lowered to it.
    assert gate.context_workers(12, available=reserve + 3 * cell, cpus=64) == 3
    assert gate.context_workers(3, available=0, cpus=64) == 1
    assert gate.context_workers(0, available=reserve + 20 * cell, cpus=32) == gate.MAX_CONTEXT_WORKERS
    assert gate.context_workers(0, available=reserve + 3 * cell, cpus=32) == 3
    assert gate.context_workers(0, available=reserve + 20 * cell, cpus=4) == 2
    assert gate.context_workers(0, available=0, cpus=32) == 1
    monkeypatch.setattr(gate, "available_memory", lambda: None)
    assert gate.context_workers(0, cpus=32) == 1
    assert gate.context_workers(5, cpus=1) == 5  # nothing to cap an explicit count by
    with pytest.raises(BenchError, match="context-workers"):
        gate.context_workers(-1)


def test_a_stopped_cell_raises_the_stop_not_a_failure():
    import threading
    from evals import regression_gate as gate

    stop = threading.Event()
    stop.set()

    @contextmanager
    def open_bank():
        yield Bank(True)

    with pytest.raises(retained_daemon.CellStopped):
        gate.contexts_for(question(), "raw", [], open_bank, stop)


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped by another cell", "failed"])
def test_a_cell_stopped_by_another_saves_no_daemon_failure_log(tmp_path, monkeypatch, stopped):
    log, saved = tmp_path / "daemon.log", tmp_path / "out" / "daemon-error"
    log.write_text("daemon output\n", encoding="utf-8")
    daemon = SimpleNamespace(start=lambda: None, stop=lambda: None, proc=object(), log=log)
    monkeypatch.setattr(retained_daemon.daemons, "rust_daemon", lambda *args: daemon)
    session = SimpleNamespace(
        init={"status": 200, "headers": [], "body": json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})},
        port=9, sid="fixture-session", initialized={"status": 202}, close=lambda: None)
    monkeypatch.setattr(retained_daemon.mcp_wire, "Session", lambda port, token: session)
    setup = SimpleNamespace(binary=tmp_path / "daemon", onnx_dir=tmp_path, ort_library=tmp_path / "ort",
                            failure_log=saved)
    serving = retained_daemon.DaemonSetup._serve_on(setup, tmp_path / "home", "postgresql://f@127.0.0.1/f",
                                                    None, 9, "fixture-token")
    next(serving)
    error = (retained_daemon.CellStopped("gate context build stopped") if stopped
             else BenchError("cell failed"))
    with pytest.raises(BenchError) as raised:
        serving.throw(error)
    assert isinstance(raised.value, retained_daemon.CellStopped) is stopped
    logs = list(saved.parent.glob("daemon-error-*.log")) if saved.parent.exists() else []
    assert len(logs) == (0 if stopped else 1)


def test_parallel_daemons_never_share_a_reserved_port(monkeypatch):
    probes = iter([41000, 41000, 41001, 41000])
    monkeypatch.setattr(retained_daemon.daemons, "free_port", lambda: next(probes))
    monkeypatch.setattr(retained_daemon, "_RESERVED_PORTS", set())
    assert [retained_daemon._reserve_port(), retained_daemon._reserve_port()] == [41000, 41001]
    retained_daemon._RESERVED_PORTS.clear()

    seen = []

    def serve_on(home, dsn, extractor, port, token):
        seen.append(port in retained_daemon._RESERVED_PORTS)
        yield "bank"

    serving = retained_daemon.DaemonSetup._serve(SimpleNamespace(_serve_on=serve_on), None, None, None)
    assert next(serving) == "bank" and seen == [True]
    serving.close()
    assert retained_daemon._RESERVED_PORTS == set()


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
