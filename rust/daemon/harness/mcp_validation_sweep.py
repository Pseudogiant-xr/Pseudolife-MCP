"""Compare SDK response codes, readiness and tier for every generated case.

Used by mcp_run.py --validation-sweep against disposable live daemons.
Four workers are the shared-host ceiling. Every case gets its own session
and writer tier key, before and after initialization, on both daemons.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import errno
import http.client
import json
import os
import socket
import struct
from pathlib import Path
import time
import threading

from mcp_validation_cases import cases, coercion_cases, report_default, wire_json
from mcp_validation_schema import source_versions
from mcp_wire import ACCEPT, PROTOCOL, header, request, sse_data

EXPECTED_CASES = 18515
EXPECTED_COERCION_CASES = 2137
# Hosted Windows run 38535960260 (2026-10-14): one connect() of a coercion
# sweep's ~46,000 was refused with WinError 10048 and failed the job. A
# refused connect sends nothing, so it is retried after these pauses.
CONNECT_BACKOFF = (0.05, 0.1, 0.2, 0.4)


class SweepConnection(http.client.HTTPConnection):
    """One-shot sweep sockets: reset on close to avoid client TIME_WAIT."""

    def __init__(self, port):
        super().__init__('127.0.0.1', port, timeout=30)
        self.opened = 0
        self.connect_retries = 0

    def connect(self):
        for pause in (*CONNECT_BACKOFF, None):
            try:
                super().connect()
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE or pause is None:
                    raise
            self.connect_retries += 1
            time.sleep(pause)
        self.opened += 1

    def close(self):
        try:
            if self.sock is not None:
                # Winsock's linger fields are u_short; POSIX uses int.
                layout = 'HH' if os.name == 'nt' else 'ii'
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                     struct.pack(layout, 1, 0))
        finally:
            super().close()


class ValidationMismatch(AssertionError):
    """A completed live corpus differs from the oracle."""

INIT = {'protocolVersion': PROTOCOL, 'capabilities': {},
        'clientInfo': {'name': 'validation-fixture', 'version': '1'}}
STATUS = {'name': 'memory_toolset', 'arguments': {'action': 'status'}}


def decoded(response):
    data = sse_data(response)
    value = json.loads(data[0] if data else response['body']) if response['body'] else {}
    return value


def signature(response):
    value = decoded(response)
    return {'http': response['status'], 'code': value.get('error', {}).get('code'),
            'is_error': value.get('result', {}).get('isError', False)}


def observe(port, case, ready, index, *, connection=None):
    headers = {'Content-Type': 'application/json', 'Accept': ACCEPT,
               'Authorization': 'Bearer tok-default-0001',
               'X-PL-Writer': f'validation-{index}-{int(ready)}',
               'MCP-Protocol-Version': PROTOCOL}

    def call(method, params, notification=False):
        body = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            body['id'] = 1
        return request(port, 'POST', wire_json(body).encode('utf-8'), headers, timeout=30, connection=connection)

    first = call('initialize', {})
    sid = header(first, 'mcp-session-id')
    assert sid, first
    headers['Mcp-Session-Id'] = sid
    try:
        if ready:
            assert 'result' in decoded(call('initialize', INIT))
        response = call(case['method'], case['params'], case['notification'])
        result = signature(response)
        status = decoded(call('tools/call', STATUS))
        result['ready'] = 'result' in status
        if not result['ready']:
            assert status.get('error', {}).get('code') == -32602, status
            assert 'result' in decoded(call('initialize', INIT))
            status = decoded(call('tools/call', STATUS))
        result['tier'] = status['result']['structuredContent']['current']
        return result
    finally:
        request(port, 'DELETE', None, headers, timeout=30, connection=connection)


def run(py_port, rs_port, output, *, coercions_only=False):
    output = Path(output)
    output.unlink(missing_ok=True)
    versions = source_versions()
    all_cases = coercion_cases() if coercions_only else list(cases())
    expected = EXPECTED_COERCION_CASES if coercions_only else EXPECTED_CASES
    assert len(all_cases) == expected, "generated corpus count changed"
    workload = [(index, case, ready) for index, case in enumerate(all_cases) for ready in (False, True)]
    started = time.monotonic()
    failures = []
    completed = 0
    counts = Counter()
    local = threading.local()
    connections = []
    connections_lock = threading.Lock()

    def connection(port):
        if not hasattr(local, 'clients'):
            local.clients = {}
        if port not in local.clients:
            client = SweepConnection(port)
            with connections_lock:
                connections.append(client)
            local.clients[port] = client
        return local.clients[port]

    def compare(item):
        index, case, ready = item
        try:
            py = observe(py_port, case, ready, index, connection=connection(py_port))
            rs = observe(rs_port, case, ready, index, connection=connection(rs_port))
        except Exception as exc:
            raise RuntimeError(f"validation case {index}, {case['method']}, {case['field']}, ready={ready}: {exc}") from exc
        return index, case, ready, py, rs

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            # Keep at most four observations in flight, including when one fails.
            for offset in range(0, len(workload), 4):
                for index, case, ready, py, rs in pool.map(compare, workload[offset:offset + 4]):
                    completed += 1
                    counts[case['method']] += 1
                    if py != rs:
                        failures.append({'index': index, 'case': case, 'before_ready': ready,
                                         'python': py, 'rust': rs})
                        if len(failures) <= 3:
                            print(json.dumps(failures[-1], default=report_default), flush=True)
                    if completed % 100 == 0:
                        print(f'validation sweep: {completed}/{len(workload)}, {len(failures)} differences', flush=True)
    finally:
        # Each exchange closes its socket; join workers before final cleanup.
        for client in connections:
            client.close()
    report = {'mode': 'coercions' if coercions_only else 'full', 'source': versions,
              'generated_cases': len(all_cases), 'expected_observations': len(workload),
              'completed_observations': completed, 'methods': dict(counts),
              'tcp_connections': {name: sum(c.opened for c in connections if c.port == port)
                                  for name, port in [('python', py_port), ('rust', rs_port)]},
              'connect_retries': sum(c.connect_retries for c in connections),
              'seconds': round(time.monotonic() - started, 1), 'diffs': failures}
    output.write_text(json.dumps(report, indent=2, default=report_default) + '\n', encoding='utf-8')
    print(f'validation sweep complete: {completed} observations, {len(failures)} differences', flush=True)
    if report['connect_retries']:
        print(f"validation sweep: {report['connect_retries']} connects retried after address in use", flush=True)
    assert completed == len(workload), 'incomplete validation sweep'
    if failures:
        raise ValidationMismatch(f'validation sweep differs: {output}')
    return report
