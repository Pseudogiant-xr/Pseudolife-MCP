"""Generate field-by-field adversarial cases from the pinned SDK schemas."""
from __future__ import annotations

import copy
import json
from pathlib import Path

TABLE = Path(__file__).resolve().parents[1] / 'src/mcp/validation-schema.json'
VALUES = (None, False, True, 0, 1, -1, 1.5, 1e20, 100000000000000000000,
          '', 'fixture', 'bad', '1.0', [], {}, [None])
INTEGER_STRINGS = ('0-1', '00-1', '-0-1', '+0-1', '0_0-1', '0_-1', '0__1',
                   '0__0', '0_', '00_.0', '0_.0', '1_.0', '1_0.0', '1_0.0_0',
                   '0-1.0', '0-0', '-00', '0-_1', '0-_1_2', '1e0', '+-1', '--1',
                   ' 1 ', '1_2', '01', '0.00', '-1', '9' * 4299, '9' * 4300,
                   '9' * 4301, '-' + '9' * 4299, '-' + '9' * 4300)

FLOAT_STRINGS = ('0_._1', ' 0_1 ', '+_0.1', '1_e-1', '0_.1', '0._1',
                 '1__0', '_1', '1_', 'NaN', 'inf', '0.1', ' 0.1 ', '0_1',
                 '1e999', '-inf')


def resolve(schema, defs):
    while '$ref' in schema:
        schema = defs[schema['$ref'].removeprefix('#/$defs/')]
    return schema


def sample(schema, defs):
    schema = resolve(schema, defs)
    if 'const' in schema:
        return schema['const']
    if 'enum' in schema:
        return schema['enum'][0]
    if 'anyOf' in schema:
        return sample(next((s for s in schema['anyOf'] if s.get('type') != 'null'),
                           schema['anyOf'][0]), defs)
    kind = schema.get('type')
    if kind == 'object':
        result = {key: sample(child, defs) for key, child in schema.get('properties', {}).items()}
        if isinstance(schema.get('additionalProperties'), dict):
            result['fixture'] = sample(schema['additionalProperties'], defs)
        return result
    if kind == 'array':
        return [sample(schema.get('items', {}), defs)]
    if kind == 'string':
        if schema.get('format') == 'uri':
            scheme = schema.get('x-allowed-schemes', ['https'])[0]
            return 'file:///fixture' if scheme == 'file' else scheme + '://example.test/resource'
        return 'fixture'
    if kind in ('integer', 'number'):
        return schema.get('minimum', 1)
    if kind == 'boolean':
        return True
    return None


def variants(schema, defs, base=None, path=()):
    """Vary each declared field and each union alternative independently."""
    schema = resolve(schema, defs)
    if base is None:
        base = sample(schema, defs)
    yield path, base
    for value in VALUES:
        yield path, value
    if schema.get('type') == 'integer':
        for value in INTEGER_STRINGS:
            yield path, value
    if schema.get('type') == 'number':
        for value in FLOAT_STRINGS:
            yield path, value
    if schema.get('format') == 'uri':
        for value in ('file:///fixture', 'https://example.test/resource', 'fixture:opaque'):
            yield path, value
    if 'enum' in schema:
        for value in schema['enum']:
            yield path, value
    for key in ('minimum', 'maximum'):
        if key in schema:
            for delta in (-1, 0, 1):
                yield path, schema[key] + delta
    if 'anyOf' in schema:
        for index, branch in enumerate(schema['anyOf']):
            yield from variants(branch, defs, path=(*path, f'union:{index}'))
    if schema.get('type') == 'object':
        base = copy.deepcopy(base) if isinstance(base, dict) else sample(schema, defs)
        for key, child in schema.get('properties', {}).items():
            missing = copy.deepcopy(base)
            missing.pop(key, None)
            yield (*path, key, 'missing'), missing
            for child_path, value in variants(child, defs, base.get(key), (*path, key)):
                changed = copy.deepcopy(base)
                changed[key] = value
                yield child_path, changed
        extra = schema.get('additionalProperties')
        if isinstance(extra, dict):
            for child_path, value in variants(extra, defs, path=(*path, 'fixture')):
                changed = copy.deepcopy(base)
                changed['fixture'] = value
                yield child_path, changed
        else:
            yield (*path, 'extension'), {**base, 'vendor-extension': [None, {}, 1]}
    if schema.get('type') == 'array':
        for child_path, value in variants(schema.get('items', {}), defs, path=(*path, 'item')):
            yield child_path, [value]


def cases(table=None):
    table = table or json.loads(TABLE.read_text(encoding='utf-8'))
    seen = set()
    for layer_name in ('requests', 'handlers', 'notifications'):
        layer = table[layer_name]
        notification = layer_name == 'notifications'
        for method, schema in sorted(layer['params'].items()):
            base = sample(schema, layer['definitions'])
            if method == 'tools/call':
                base.update(name='memory_toolset', arguments={'action': 'expand'})
            if method == 'initialize':
                base['protocolVersion'] = '2025-11-25'
            if 'requestState' in base:
                # Opaque state is minted by the server; a fabricated value
                # would mask every other field behind the middleware refusal.
                base['requestState'] = None
            for path, params in variants(schema, layer['definitions'], base):
                # Non-object top-level params belong to JSON-RPC transport
                # validation; this sweep isolates SDK parameter validation.
                if not isinstance(params, dict):
                    continue
                key = json.dumps([notification, method, params], sort_keys=True, allow_nan=False)
                if key in seen:
                    continue
                seen.add(key)
                yield {'method': method, 'notification': notification, 'params': params,
                       'field': '.'.join((layer_name, *path))}
    for params in ({'_meta': []}, {'_meta': {'progressToken': []}}, {}):
        yield {'method': 'unknown/method', 'notification': False, 'params': params,
               'field': 'unknown-method'}


if __name__ == '__main__':
    from collections import Counter
    all_cases = list(cases())
    print(json.dumps({'cases': len(all_cases), 'methods': Counter(c['method'] for c in all_cases)}, indent=2))
