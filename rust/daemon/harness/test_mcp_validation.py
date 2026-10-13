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
    monkeypatch.setattr(sweep, 'observe', lambda port, *args: {'http': 200, 'code': -32602,
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
    def failed(*args):
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
