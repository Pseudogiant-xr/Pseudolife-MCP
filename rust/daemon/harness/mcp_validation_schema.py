"""Record pinned MCP wire and handler parameter schemas for Rust validation.

Run with the repository's MCP dependency: python mcp_validation_schema.py.
The generated table covers all handshake requests and client notifications;
handler schemas cover the methods the Rust MCP surface serves.
"""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

from mcp.types import methods
from pydantic.json_schema import GenerateJsonSchema

VERSIONS = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25')
SERVED = ('initialize', 'ping', 'tools/list', 'tools/call', 'prompts/list',
          'prompts/get', 'resources/list', 'resources/templates/list', 'resources/read')
DOCUMENTATION = {'title', 'description', 'default', 'examples', 'deprecated'}
KEYWORDS = {'type', '$ref', 'anyOf', 'properties', 'required', 'additionalProperties',
            'items', 'enum', 'const', 'minimum', 'maximum', 'minLength', 'maxLength', 'format',
            'x-allowed-schemes'}


class ValidationSchema(GenerateJsonSchema):
    """Preserve URL constraints omitted from the standard JSON Schema projection."""

    def url_schema(self, schema):
        result = super().url_schema(schema)
        if "allowed_schemes" in schema:
            result["x-allowed-schemes"] = schema["allowed_schemes"]
        return result


def compact(schema):
    if isinstance(schema, bool):
        return schema
    assert not (schema.keys() - DOCUMENTATION - KEYWORDS), schema.keys()
    result = {}
    for key, value in schema.items():
        if key in DOCUMENTATION:
            continue
        if key == 'properties':
            value = {name: compact(child) for name, child in value.items()}
        elif key in ('items', 'additionalProperties') and isinstance(value, dict):
            value = compact(value)
        elif key == 'anyOf':
            value = [compact(child) for child in value]
        result[key] = value
    return result


def collect(models):
    definitions, params = {}, {}
    for method, model in sorted(models.items()):
        schema = model.model_json_schema(by_alias=True, schema_generator=ValidationSchema)
        for name, definition in schema.get('$defs', {}).items():
            value = compact(definition)
            assert name not in definitions or definitions[name] == value, name
            definitions[name] = value
        params[method] = compact(schema['properties']['params'])
    return {'definitions': definitions, 'params': params}


def source_versions():
    versions = {name: importlib.metadata.version(name)
                for name in ('mcp', 'mcp-types', 'pydantic', 'pydantic-core')}
    assert versions['mcp'] == versions['mcp-types'] == '2.2.0', versions
    assert versions['pydantic-core'] == '2.50.0', {'pydantic-core': versions['pydantic-core']}
    return versions


def generate():
    out = {'source': source_versions(), 'handshake_versions': VERSIONS,
           'sealed_state_methods': sorted(methods.INPUT_REQUIRED_METHODS)}
    for name, mapping in [('requests', methods.CLIENT_REQUESTS),
                          ('notifications', methods.CLIENT_NOTIFICATIONS)]:
        arms = [collect({method: model for (method, version), model in mapping.items()
                         if version == target}) for target in VERSIONS]
        assert all(arm == arms[0] for arm in arms), name
        out[name] = arms[0]
    out['handlers'] = collect({method: methods.MONOLITH_REQUESTS[method] for method in SERVED})
    return out


if __name__ == '__main__':
    target = Path(__file__).resolve().parents[1] / 'src/mcp/validation-schema.json'
    target.write_text(json.dumps(generate(), indent=2, sort_keys=True) + '\n', encoding='utf-8')
