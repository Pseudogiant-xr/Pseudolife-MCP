"""Record every MCP tool's argument binding for the Rust daemon to embed.

usage: python mcp_binding_table.py [--check]

Reads each registered tool's argument model from `mcp_server` (the
`_StringSafeMetadata` the daemon binds with) and writes
`src/mcp/binding.json`: per tool, its parameters in declaration order, each
with its pydantic-core validation schema reduced to the closed vocabulary the
Rust binder interprets (`src/mcp/binding.rs`). A schema outside that
vocabulary refuses generation rather than being approximated. `--check`
exits 1 when the committed table differs from the source.

Messages that pydantic formats from the schema alone (literal choices, numeric
bounds) are recorded as pydantic produces them, by validating a probe value,
so the Rust side never re-derives Python's repr of a bound.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("PSEUDOLIFE_MCP_TOOLSET", "full")

from pydantic import ValidationError  # noqa: E402
from pydantic_core import SchemaValidator  # noqa: E402

from pseudolife_memory import mcp_server  # noqa: E402

TARGET = Path(__file__).resolve().parents[1] / "src/mcp/binding.json"
AFTER_VALIDATORS = {"_non_blank", "_check_as_of"}
NUMERIC_BOUNDS = ("gt", "ge", "lt", "le")


def source_versions():
    versions = {name: importlib.metadata.version(name)
                for name in ("mcp", "pydantic", "pydantic-core")}
    assert versions["mcp"] == "2.1.1", versions
    assert versions["pydantic-core"] == "2.50.0", versions
    return versions


def _errors(schema, value):
    try:
        SchemaValidator(schema).validate_python(value)
    except ValidationError as exc:
        return exc.errors(include_url=False, include_input=False, include_context=False)
    return []


def _bound_message(schema, bound, value):
    """pydantic's sentence for a value that breaks one numeric bound."""
    errors = _errors(schema, value)
    assert len(errors) == 1 and errors[0]["type"].endswith(
        {"gt": "greater_than", "ge": "greater_than_equal",
         "lt": "less_than", "le": "less_than_equal"}[bound]), errors
    return errors[0]["msg"]


def node(schema):
    kind = schema["type"]
    known = {"type", "metadata", "ref", "serialization"}
    if kind == "nullable":
        assert set(schema) <= known | {"schema"}, schema
        return {"kind": "nullable", "inner": node(schema["schema"])}
    if kind == "union":
        assert set(schema) <= known | {"choices", "mode"}, schema
        assert schema.get("mode", "smart") == "smart", schema
        choices = [c if isinstance(c, dict) else c[0] for c in schema["choices"]]
        assert all(isinstance(c, dict) for c in schema["choices"]), schema
        # The choice tags pydantic puts in each error's location.
        tags = [e["loc"][0] for e in _errors(schema, object())]
        assert len(tags) == len(choices), (tags, schema)
        return {"kind": "union",
                "choices": [{"tag": t, "node": node(c)} for t, c in zip(tags, choices)]}
    if kind == "function-after":
        assert set(schema) <= known | {"function", "schema"}, schema
        name = schema["function"]["function"].__name__
        assert name in AFTER_VALIDATORS, name
        return {"kind": "after", "validator": name, "inner": node(schema["schema"])}
    if kind == "literal":
        assert set(schema) <= known | {"expected"}, schema
        expected = schema["expected"]
        assert all(isinstance(v, str) for v in expected), expected
        message = _errors(schema, object())[0]["msg"]
        return {"kind": "literal", "expected": expected, "message": message}
    if kind == "list":
        assert set(schema) <= known | {"items_schema", "max_length", "min_length", "strict"}, schema
        assert "strict" not in schema and "min_length" not in schema, schema
        out = {"kind": "list",
               "items": node(schema["items_schema"]) if "items_schema" in schema else {"kind": "any"}}
        if "max_length" in schema:
            out["max_length"] = schema["max_length"]
        return out
    if kind == "dict":
        assert set(schema) <= known | {"keys_schema", "values_schema"}, schema
        for side in ("keys_schema", "values_schema"):
            assert schema.get(side, {"type": "any"})["type"] == "any", schema
        return {"kind": "dict"}
    if kind == "any":
        assert set(schema) <= known, schema
        return {"kind": "any"}
    if kind == "str":
        assert set(schema) <= known | {"min_length", "max_length", "strict"}, schema
        assert not schema.get("strict"), schema
        return {"kind": "str", **{k: schema[k] for k in ("min_length", "max_length") if k in schema}}
    if kind == "bool":
        assert set(schema) <= known | {"strict"}, schema
        return {"kind": "bool", "strict": bool(schema.get("strict"))}
    if kind in ("int", "float"):
        assert set(schema) <= known | {"strict", *NUMERIC_BOUNDS}, schema
        out = {"kind": kind, "strict": bool(schema.get("strict"))}
        for bound in NUMERIC_BOUNDS:
            if bound in schema:
                limit = schema[bound]
                assert type(limit) in (int, float), (bound, limit)
                # A value that breaks only this bound; the limit itself
                # breaks a strict bound, one step past it a closed one.
                step = {"gt": 0, "ge": -1, "lt": 0, "le": 1}[bound]
                probe = limit + step if kind == "int" else limit + step * 0.5
                if kind == "int" and type(limit) is not int:
                    raise AssertionError(f"non-integer int bound {limit!r}")
                out[bound] = limit
                out[bound + "_message"] = _bound_message(schema, bound, probe)
        return out
    raise AssertionError(f"unsupported pydantic-core schema {kind}: {schema}")


def literal_text(field) -> bool:
    """`_StringSafeMetadata.pre_parse_json`'s literal keys: annotation `str`
    or `str | None` exactly."""
    return field.annotation is str or field.annotation == str | None


def tool_table(name, tool):
    meta = tool.fn_metadata
    assert isinstance(meta, mcp_server._StringSafeMetadata), name
    core = meta.arg_model.__pydantic_core_schema__
    while core["type"] in ("definitions", "model"):
        core = core["schema"]
    assert core["type"] == "model-fields", (name, core["type"])
    assert core.get("extra_behavior") in (None, "forbid", "ignore"), core
    model_fields = meta.arg_model.model_fields
    params = []
    for field_name, entry in core["fields"].items():
        field = model_fields[field_name]
        assert field.alias in (None, field_name), (name, field_name, field.alias)
        assert not entry.get("validation_alias"), (name, field_name)
        schema = entry["schema"]
        param = {"name": field_name, "literal_text": literal_text(field)}
        if schema["type"] == "default":
            assert set(schema) <= {"type", "schema", "default", "metadata", "ref",
                                   "serialization"}, schema
            default = schema["default"]
            assert default is None or isinstance(default, (bool, int, float, str)), default
            param["default"] = default
            schema = schema["schema"]
        param["schema"] = node(schema)
        params.append(param)
    assert [p["name"] for p in params] == meta._accepted(), name
    return {"params": params}


def generate():
    tools = mcp_server.mcp._tool_manager._tools
    return {"source": source_versions(),
            "tools": {name: tool_table(name, tool) for name, tool in tools.items()}}


def render():
    return json.dumps(generate(), indent=1, ensure_ascii=False) + "\n"


def main(argv):
    text = render()
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET} is stale; run python {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"{TARGET}: {len(json.loads(text)['tools'])} tools")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
