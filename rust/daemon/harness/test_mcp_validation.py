"""Keep generated validation tied to SDK types and require observable state parity."""
import json

import pytest

import mcp_validation_cases as corpus
import mcp_validation_schema as schema
import mcp_validation_sweep as sweep


def test_recorded_shapes_match_the_pinned_sdk():
    recorded = json.loads(corpus.TABLE.read_text(encoding='utf-8'))
    generated = schema.generate()
    for key in ('requests', 'handlers', 'notifications', 'sealed_state_methods'):
        assert recorded[key] == generated[key]


def test_each_generated_base_is_accepted_by_its_sdk_validation_layer():
    from mcp.types import methods
    table = schema.generate()
    for layer, validate in (
        ('requests', methods.validate_client_request),
        ('handlers', methods.parse_client_request),
        ('notifications', methods.validate_client_notification),
    ):
        for method, model in table[layer]['params'].items():
            params = corpus.sample(model, table[layer]['definitions'])
            if method == 'tools/call':
                params.update(name='memory_toolset', arguments={'action': 'expand'})
            if method == 'initialize':
                params['protocolVersion'] = '2025-11-25'
            validate(method, '2025-11-25', params)


def test_sweep_cannot_pass_when_only_state_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, 'cases', lambda: iter([{'method': 'tools/call', 'notification': False,
        'params': {}, 'field': 'fixture'}]))
    monkeypatch.setattr(sweep, 'EXPECTED_CASES', 1)
    monkeypatch.setattr(sweep, 'observe', lambda port, *args, **kwargs: {'http': 200, 'code': -32602,
        'is_error': False, 'ready': True, 'tier': 'core' if port == 1 else 'full'})
    path = tmp_path / 'report.json'
    with pytest.raises(AssertionError, match='validation sweep differs'):
        sweep.run(1, 2, path)
    report = json.loads(path.read_text(encoding='utf-8'))
    assert report['completed_observations'] == 2
    assert len(report['diffs']) == 2


def test_sweep_failure_cannot_reuse_previous_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, 'cases', lambda: iter([{'method': 'ping', 'notification': False,
        'params': {}, 'field': 'fixture'}]))
    monkeypatch.setattr(sweep, 'EXPECTED_CASES', 1)
    def failed(*args, **kwargs):
        raise RuntimeError('fixture unavailable')
    monkeypatch.setattr(sweep, 'observe', failed)
    path = tmp_path / 'report.json'
    path.write_text('{"diffs": []}', encoding='utf-8')
    with pytest.raises(RuntimeError, match='fixture unavailable'):
        sweep.run(1, 2, path)
    assert not path.exists()


def test_sweep_rejects_an_incomplete_generated_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, 'cases', lambda: iter([]))
    with pytest.raises(AssertionError, match='generated corpus count'):
        sweep.run(1, 2, tmp_path / 'report.json')


def test_untrusted_state_does_not_mask_other_generated_fields():
    table = schema.generate()
    for case in corpus.cases(table):
        if (case['method'] in table['sealed_state_methods']
                and '.requestState' not in case['field']):
            assert case['params'].get('requestState') is None, case['field']


def test_generated_table_preserves_core_url_constraints():
    from mcp.types import Root
    from pydantic import ValidationError
    table = schema.generate()
    uri = table['handlers']['definitions']['Root']['properties']['uri']
    assert uri['x-allowed-schemes'] == ['file']
    assert corpus.sample(uri, {}) == 'file:///fixture'
    Root(uri=corpus.sample(uri, {}))
    with pytest.raises(ValidationError):
        Root(uri='https://example.test/resource')


def test_generator_refuses_unreviewed_core_version(monkeypatch):
    version = schema.importlib.metadata.version
    monkeypatch.setattr(schema.importlib.metadata, 'version',
                        lambda name: '99.0.0' if name == 'pydantic-core' else version(name))
    with pytest.raises(AssertionError, match='pydantic-core'):
        schema.generate()


def test_overflow_values_keep_their_json_number_spelling():
    for value in (v for v in corpus.VALUES if isinstance(v, corpus.RawNumber)):
        decoded = json.loads(corpus.wire_json({'value': value}),
                             parse_float=lambda text: {'literal': text})
        assert decoded == {'value': {'literal': value.text}}
        assert json.loads(json.dumps(value, default=corpus.report_default)) == {'raw_json_number': value.text}


def test_ci_subset_contains_every_scalar_class_and_review_trigger():
    cases = corpus.coercion_cases()
    assert len(cases) == sweep.EXPECTED_COERCION_CASES
    fields = {part for case in cases for part in case['field'].split('.')}
    assert {'integer-coercion', 'float-coercion', 'uri-coercion', 'raw-number-coercion'} <= fields
    ttls = [case['params'].get('task', {}).get('ttl') for case in cases
            if case['method'] == 'tools/call' and isinstance(case['params'].get('task'), dict)]
    assert '0-1' in ttls and '-' + '9' * 4300 in ttls
    assert corpus.RawNumber('1e400') in ttls and corpus.RawNumber('-1e400') in ttls


def test_ci_retains_accepted_file_urls_after_deduplication():
    from mcp.types import methods
    from pydantic import ValidationError

    selected = {corpus.wire_json([case['method'], case['params']])
                for case in corpus.coercion_cases()}
    accepted = []
    for case in corpus.cases():
        encoded = corpus.wire_json(case['params'])
        if case['method'] != 'tools/call' or 'file:///fixture' not in encoded:
            continue
        params = json.loads(encoded)
        try:
            methods.validate_client_request('tools/call', '2025-11-25', params)
            methods.parse_client_request('tools/call', '2025-11-25', params)
        except ValidationError:
            continue
        accepted.append(corpus.wire_json([case['method'], case['params']]))
    assert accepted
    assert set(accepted) & selected


@pytest.mark.parametrize('fail', [False, True])
def test_sweep_reuses_source_ports_and_cleans_up(tmp_path, monkeypatch, fail):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import socket

    class Server(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True
        opened = 0
        sessions_created = 0

        def get_request(self):
            sock, address = super().get_request()
            self.opened += 1
            return sock, address

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            writer = self.headers['X-PL-Writer']
            sid = self.headers.get('Mcp-Session-Id')
            with self.server.guard:
                if not sid:
                    self.server.sessions_created += 1
                    sid = str(self.server.sessions_created)
                    self.server.sessions[sid] = [writer, False]
                owner, ready = self.server.sessions[sid]
                assert writer == owner
                if body['method'] == 'initialize' and body['params']:
                    ready = True
                    self.server.sessions[sid][1] = True
            response = ({'result': {'structuredContent': {'current': 'core'}}} if ready
                        else {'error': {'code': -32602}})
            self.reply(response, sid)

        def do_DELETE(self):
            with self.server.guard:
                del self.server.sessions[self.headers['Mcp-Session-Id']]
            self.reply({}, None)

        def reply(self, body, sid):
            data = json.dumps(body).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            if sid:
                self.send_header('mcp-session-id', sid)
            self.end_headers()
            self.wfile.write(data)

    servers, threads = [], []
    for _ in range(2):
        server = Server(('127.0.0.1', 0), Handler)
        server.sessions, server.guard = {}, threading.Lock()
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        servers.append(server)
        threads.append(thread)
    reservations = []
    for _ in range(8):
        reserved = socket.socket()
        reserved.bind(('127.0.0.1', 0))
        reservations.append(reserved)
    port_lock = threading.Lock()
    original_connection = sweep.SweepConnection
    class FixedSourceConnection(original_connection):
        def __init__(self, port):
            super().__init__(port)
            with port_lock:
                reserved = reservations.pop()
                self.source_address = reserved.getsockname()
                reserved.close()
    monkeypatch.setattr(sweep, 'SweepConnection', FixedSourceConnection)
    monkeypatch.setattr(sweep, 'EXPECTED_CASES', 8)
    monkeypatch.setattr(sweep, 'cases', lambda: iter([{'method': 'ping', 'notification': False,
        'params': {}, 'field': 'fixture'}] * 8))
    if fail:
        signature = sweep.signature
        def failed(response):
            signature(response)
            raise RuntimeError('injected response failure')
        monkeypatch.setattr(sweep, 'signature', failed)
    try:
        args = [server.server_port for server in servers] + [tmp_path / 'sweep.json']
        if fail:
            with pytest.raises(RuntimeError, match='injected response failure'):
                sweep.run(*args)
        else:
            report = sweep.run(*args)
            assert report['completed_observations'] == 16 and not report['diffs']
    finally:
        for reserved in reservations:
            reserved.close()
        for server, thread in zip(servers, threads):
            server.shutdown()
            thread.join()
            server.server_close()
    assert all(not server.sessions for server in servers)
    if not fail:
        assert all(server.sessions_created == 16 for server in servers)
        assert all(server.opened > server.sessions_created for server in servers)


@pytest.mark.parametrize('platform,layout', [('nt', 'HH'), ('posix', 'ii')])
def test_sweep_reset_close_uses_native_linger_layout(monkeypatch, platform, layout):
    import socket
    import struct
    from types import SimpleNamespace

    options, closed = [], []
    sock = SimpleNamespace(setsockopt=lambda *args: options.append(args),
                           close=lambda: closed.append(True))
    connection = sweep.SweepConnection(1)
    connection.sock = sock
    monkeypatch.setattr(sweep, 'os', SimpleNamespace(name=platform))
    connection.close()
    assert options == [(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(layout, 1, 0))]
    assert closed == [True] and connection.sock is None


@pytest.mark.parametrize('failures', [0, 2, 4, 5])
def test_sweep_retries_a_connect_refused_with_address_in_use(monkeypatch, failures):
    """Hosted Windows run 38535960260 (2026-10-14) lost a whole sweep to one
    connect() refused with WinError 10048. A refused connect sends nothing,
    so a short retry cannot change what the sweep compares; each retry is
    counted so the report shows it happening."""
    import errno
    import socket

    monkeypatch.setattr(sweep, 'CONNECT_BACKOFF', (0, 0, 0, 0))
    attempts = []
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        connection = sweep.SweepConnection(listener.getsockname()[1])
        real = connection._create_connection

        def create(*args, **kwargs):
            attempts.append(True)
            if len(attempts) <= failures:
                raise OSError(errno.EADDRINUSE, 'address in use')
            return real(*args, **kwargs)

        connection._create_connection = create
        try:
            if failures > len(sweep.CONNECT_BACKOFF):
                with pytest.raises(OSError) as raised:
                    connection.connect()
                assert raised.value.errno == errno.EADDRINUSE
                assert connection.opened == 0
            else:
                connection.connect()
                assert connection.opened == 1
            assert connection.connect_retries == min(failures, len(sweep.CONNECT_BACKOFF))
            assert len(attempts) == min(failures + 1, len(sweep.CONNECT_BACKOFF) + 1)
        finally:
            connection.close()


def test_sweep_does_not_retry_other_connect_failures(monkeypatch):
    attempts = []
    connection = sweep.SweepConnection(1)

    def refused(*args, **kwargs):
        attempts.append(True)
        raise ConnectionRefusedError('refused')

    connection._create_connection = refused
    with pytest.raises(ConnectionRefusedError):
        connection.connect()
    assert len(attempts) == 1 and connection.connect_retries == 0
