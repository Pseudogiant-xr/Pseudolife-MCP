"""Both candidate entry points receive only a runner-owned disposable bank."""
from contextlib import contextmanager
import json
import sys
import tempfile

import pytest

from evals.rust_port import candidate, full_bank


def test_owned_command_has_isolated_environment_and_reclaims_process(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "must-not-inherit")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", "dbname=pseudolife_memory")
    code = """
import os,json
from http.server import BaseHTTPRequestHandler,HTTPServer
assert os.environ['PSEUDOLIFE_MCP_DATABASE_URL']=='synthetic-minted-bank'
assert os.environ['PSEUDOLIFE_MCP_TOKEN']!='must-not-inherit'
assert os.environ['_PSEUDOLIFE_PRODUCTION_DB']=='pseudolife_memory'
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  raw=json.dumps({'status':'ok','baseline_instance':{'nonce':os.environ['PSEUDOLIFE_BASELINE_NONCE'],'pid':os.getpid()}}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def log_message(self,*args): pass
HTTPServer(('127.0.0.1',int(os.environ['PSEUDOLIFE_MCP_PORT'])),Handler).serve_forever()
"""
    with candidate.launched_candidate([sys.executable, "-c", code], "synthetic-minted-bank", tmp_path,
            source_root=tmp_path, cache_overrides={}, startup_timeout=5) as (_, cleanup):
        assert cleanup["readiness_identity_verified"]
    assert cleanup["daemon_stopped"] and cleanup["children_stopped"]


def test_external_url_requires_exact_bank_binding_and_release(monkeypatch):
    calls = []
    class Client:
        def __init__(self, *args, **kwargs): pass
        def execute(self, request, surface, **kwargs):
            calls.append(request)
            body = request.get("body", {})
            if request["path"].endswith("ready"):
                return {"status": 200, "body": {"nonce": "a" * 64, "disposable": True}}
            return {"status": 200, "body": ({k: body[k] for k in ("nonce", "bank_sha256", "disposable")}
                if request["path"].endswith("disposable-bank") else {"nonce": body["nonce"], "released": True})}
    monkeypatch.setattr(candidate, "HttpClient", Client)
    with candidate.external_candidate("http://127.0.0.1:1", "dbname=plbench_rust_baseline_synthetic", "synthetic-token", candidate_nonce="a" * 64) as (_, cleanup):
        assert cleanup["bank_binding_verified"]
    assert cleanup["bank_released"]
    assert calls[1]["body"]["database_url"] == "dbname=plbench_rust_baseline_synthetic"
    assert "database_url" not in json.dumps(cleanup)


def test_external_url_refuses_unverified_existing_bank(monkeypatch):
    requests = []
    class Client:
        def __init__(self, *args, **kwargs): pass
        def execute(self, request, *args, **kwargs):
            requests.append(request)
            return {"status": 200, "body": {"status": "ok"}}
    monkeypatch.setattr(candidate, "HttpClient", Client)
    with pytest.raises(RuntimeError, match="preflight identity"):
        with candidate.external_candidate("http://127.0.0.1:1", "dbname=plbench_rust_baseline_synthetic", "synthetic", candidate_nonce="a" * 64):
            pytest.fail("unverified existing server must never receive corpus requests")
    assert requests == [{"path": "/_rust_port/ready"}]


def test_external_url_releases_bank_when_candidate_observation_fails(monkeypatch):
    requests = []
    class Client:
        def __init__(self, *args, **kwargs): pass
        def execute(self, request, *args, **kwargs):
            requests.append(request)
            if request["path"].endswith("ready"):
                return {"status": 200, "body": {"nonce": "a" * 64, "disposable": True}}
            body = request["body"]
            return {"status": 200, "body": ({k: body[k] for k in ("nonce", "bank_sha256", "disposable")}
                if request["path"].endswith("disposable-bank") else {"nonce": body["nonce"], "released": True})}
    monkeypatch.setattr(candidate, "HttpClient", Client)
    with pytest.raises(RuntimeError, match="observed failure"):
        with candidate.external_candidate("http://127.0.0.1:1", "dbname=plbench_rust_baseline_synthetic", "synthetic", candidate_nonce="a" * 64) as (_, cleanup):
            raise RuntimeError("observed failure")
    assert cleanup["bank_released"]
    assert requests[-1]["path"].endswith("/release")


@pytest.mark.parametrize('bind_failure', ['mismatched', 'malformed', 'transport'])
@pytest.mark.parametrize('release_failure', [None, 'mismatched', 'transport'])
def test_external_url_releases_selected_bank_after_failed_bind_preserving_primary_error(
        monkeypatch, bind_failure, release_failure):
    requests, selected = [], []
    transport_error = OSError('synthetic bind transport failure')
    class Client:
        def __init__(self, url, **kwargs):
            assert url == 'http://127.0.0.1:1'
        def execute(self, request, *args, **kwargs):
            assert kwargs['runtime_headers'] == {'Authorization': 'Bearer synthetic-token'}
            requests.append(request)
            if request['path'].endswith('ready'):
                return {'status': 200, 'body': {'nonce': 'a' * 64, 'disposable': True}}
            if request['path'].endswith('disposable-bank'):
                selected.append(request['body']['nonce'])
                if bind_failure == 'transport':
                    raise transport_error
                return {'status': 200, 'body': {'binding': 'wrong'}} if bind_failure == 'mismatched' else {}
            assert request == {'path': '/_rust_port/disposable-bank/release', 'method': 'POST',
                               'body': {'nonce': selected.pop()}}
            if release_failure == 'transport':
                raise OSError('synthetic release transport failure')
            return {'status': 200, 'body': {'nonce': request['body']['nonce'],
                                           'released': release_failure is None}}
    monkeypatch.setattr(candidate, 'HttpClient', Client)
    error_type = {'mismatched': RuntimeError, 'malformed': KeyError, 'transport': OSError}[bind_failure]
    with pytest.raises(error_type) as error:
        with candidate.external_candidate('http://127.0.0.1:1', 'dbname=plbench_rust_baseline_synthetic',
                'synthetic-token', candidate_nonce='a' * 64):
            pytest.fail('failed binding must not yield a candidate')
    if bind_failure == 'transport':
        assert error.value is transport_error
    elif bind_failure == 'mismatched':
        assert str(error.value) == 'external candidate disposable bank binding not verified'
    else:
        assert error.value.args == ('status',)
    assert not selected
    assert [request['path'] for request in requests] == [
        '/_rust_port/ready', '/_rust_port/disposable-bank', '/_rust_port/disposable-bank/release']


def test_external_url_successful_bind_surfaces_failed_release(monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs): pass
        def execute(self, request, *args, **kwargs):
            if request['path'].endswith('ready'):
                return {'status': 200, 'body': {'nonce': 'a' * 64, 'disposable': True}}
            body = request['body']
            if request['path'].endswith('disposable-bank'):
                return {'status': 200, 'body': {key: body[key] for key in ('nonce', 'bank_sha256', 'disposable')}}
            raise OSError('synthetic release transport failure')
    monkeypatch.setattr(candidate, 'HttpClient', Client)
    with pytest.raises(OSError, match='release transport failure'):
        with candidate.external_candidate('http://127.0.0.1:1', 'dbname=plbench_rust_baseline_synthetic',
                'synthetic-token', candidate_nonce='a' * 64) as (_, cleanup):
            assert cleanup['bank_binding_verified']
    assert not cleanup['bank_released'] and cleanup['bank_release_error'] == 'OSError'


@pytest.mark.parametrize("authorization", [None, "Bearer synthetic-invalid"])
def test_reference_url_forwards_auth_negative_requests(monkeypatch, authorization):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from evals.rust_baseline import daemon, transport
    from evals import memory_policy_daemon
    from evals.rust_port import url_reference
    from evals.rust_port.harness import HttpClient
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            raw = b'{"error":"unauthorized","hint":"oracle hint"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    @contextmanager
    def launch(*args, **kwargs):
        yield None, f"http://127.0.0.1:{server.server_port}", {}
    monkeypatch.setattr(daemon, "launched_daemon", launch)
    monkeypatch.setattr(memory_policy_daemon, "check_database", lambda dsn: "synthetic-bank")
    monkeypatch.setattr(memory_policy_daemon, "_server_check", lambda dsn: None)
    try:
        with url_reference.reference_adapter(None) as (url, nonce, state):
            with candidate.external_candidate(url, "dbname=synthetic-bank", transport.TOKEN,
                    candidate_nonce=nonce):
                result = HttpClient(url).execute({"path": "/api/search", "method": "POST",
                    "body": {"query": "synthetic"}}, "http",
                    runtime_headers={} if authorization is None else {"Authorization": authorization})
                assert result["status"] == 401
                assert result["body"]["hint"] == "oracle hint"
                assert result["headers"]["cache-control"] == "no-store"
            assert state["bank_released"]
        assert state["server_stopped"] and seen == [authorization]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize("mode", ["command", "url"])
def test_full_bank_routes_candidate_arm_to_selected_target(tmp_path, monkeypatch, mode):
    from evals.rust_baseline import common, daemon
    launches, observations = [], []
    @contextmanager
    def database():
        launches.append("new-bank")
        yield "synthetic-minted-bank"
    @contextmanager
    def python_launcher(dsn, private, *, env_extra, source_root):
        launches.append("oracle")
        yield None, "http://127.0.0.1:1", {}
    @contextmanager
    def command_launcher(command, dsn, private, **kwargs):
        launches.append("command")
        assert dsn == "synthetic-minted-bank"
        yield "http://127.0.0.1:2", {}
    @contextmanager
    def url_launcher(url, dsn, token, **kwargs):
        launches.append("url")
        assert dsn == "synthetic-minted-bank"
        yield url, {}
    monkeypatch.setattr(daemon, "disposable_database", database)
    monkeypatch.setattr(daemon, "private_directory", tempfile.TemporaryDirectory)
    monkeypatch.setattr(daemon, "launched_daemon", python_launcher)
    monkeypatch.setattr(candidate, "launched_candidate", command_launcher)
    monkeypatch.setattr(candidate, "external_candidate", url_launcher)
    monkeypatch.setattr(common, "lease_gate", lambda stamp: {})
    monkeypatch.setattr(full_bank, "require_historical_source", lambda root: None)
    monkeypatch.setattr(full_bank, "environment_metadata", lambda root: {})
    monkeypatch.setattr(full_bank, "private_home_overrides", lambda private: {})
    def observe(corpus, url, token, *, candidate=False):
        observations.append((url, candidate))
        return [{"id": "probe", "expect": {}, "response": {"status": 200, "body": {}}}]
    monkeypatch.setattr(full_bank, "observe", observe)
    options = {"candidate_command": [sys.executable]} if mode == "command" else {"candidate_url": "http://127.0.0.1:2"}
    _, receipt = full_bank.run({"seed": 1, "scope": "synthetic"}, "verified", **options)
    assert launches == ["new-bank", "oracle", "new-bank", mode]
    assert observations == [("http://127.0.0.1:1", False), ("http://127.0.0.1:2", True)]
    assert receipt["status"] == "passed"
