"""Additive public episode process inputs; existing Python tests stay in-process."""
import base64
import json


def cases():
    rows = []

    def add(name, mode, raw, **extra):
        rows.append({'id': 'episode-' + name, 'mode': mode,
                     'argv': [mode, '--help', 'ignored'],
                     'stdin_b64': base64.b64encode(raw).decode('ascii'),
                     'environment_deltas': {}, 'normalizations': [], **extra})

    for name, raw in [('blank', b''), ('invalid', b'{'), ('array', b'[]'),
                      ('null', b'null'), ('missing', b'{}'), ('invalid-utf8', b'\xff'),
                      ('bom', b'\xef\xbb\xbf{"session_id":"key"}')]:
        for mode in ('episode-start', 'episode-end'):
            add(name + '-' + mode, mode, raw)
    for index, key in enumerate([None, False, 0, '', [], {}, True, 42, 1e-7,
                                 ['a', False], {'x': 'a'}, ' key ', 'mémoire 🧠']):
        for mode in ('episode-start', 'episode-end'):
            add('key-' + str(index) + '-' + mode, mode,
                json.dumps({'session_id': key}).encode('utf-8'))
    for name, raw in [('duplicate', b'{"session_id":"old","session_id":"new"}'),
                      ('surrogate', b'{"session_id":"\\ud800"}'),
                      ('nan', b'{"session_id":NaN}'),
                      ('infinity', b'{"session_id":Infinity}'),
                      ('overflow', b'{"session_id":1e400}'),
                      ('bigint', b'{"session_id":123456789012345678901234567890}')]:
        add(name, 'episode-end', raw)
    for index, cwd in enumerate([None, '', '/tmp/project', r'C:\project\System32',
                                 r'C:\project\repo', '/', False, 42, [], {'x': 1},
                                 'C:\\', 'C:project', '1:/project']):
        add('cwd-' + str(index), 'episode-start', json.dumps({'session_id': 'key', 'cwd': cwd}).encode())
    return rows


def repair_cases():
    """Public process regressions for recursion and Python path codepoints."""
    rows = []

    def add(name, mode, raw):
        rows.append({'id': 'episode-repair-' + name, 'mode': mode,
                     'argv': [mode, '--help', 'ignored'],
                     'stdin_b64': base64.b64encode(raw).decode('ascii'),
                     'environment_deltas': {}, 'normalizations': []})

    for mode in ('episode-start', 'episode-end'):
        for depth, leaf, shape in [(700, b'0', 'scalar'), (988, b'0', 'scalar'),
                                   (989, b'0', 'scalar'), (987, b'[]', 'empty'),
                                   (988, b'[]', 'empty'), (997, b'0', 'scalar'),
                                   (1001, b'0', 'scalar')]:
            raw = b'{"session_id":"key","ignored":' + b'[' * depth + leaf + b']' * depth + b'}'
            add('nested-' + str(depth) + '-' + shape + '-' + mode, mode, raw)
        for depth in (988, 989):
            raw = b'{"session_id":"key","ignored":' + b'{"x":' * depth + b'0' + b'}' * depth + b'}'
            add('objects-' + str(depth) + '-' + mode, mode, raw)
    for index, cwd in enumerate(['.', '..', '../..', 'a/..', 'a/../../..',
                                 r'\\server\share', 'é:leaf', '🧠:leaf',
                                 '\ud800', '\udc80', r'C:\missing-' + '\ud800',
                                 '/missing-' + '\ud800', '/missing-' + '\ud800' + 'system32']):
        add('cwd-' + str(index), 'episode-start', json.dumps({'session_id': 'key', 'cwd': cwd}).encode())
    return rows
