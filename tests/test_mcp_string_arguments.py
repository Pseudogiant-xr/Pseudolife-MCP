"""String arguments must survive the SDK's JSON compatibility pre-parser."""
from __future__ import annotations

import pytest

from tests.helpers import invoke_tool, reload_mcp_filemode


@pytest.mark.parametrize("value", [
    '{"a": 1}', '[1, 2]', '123', 'true', 'false', 'null',
    '"quoted"', '  {"a": 1}\n', 'ordinary text', '',
])
@pytest.mark.parametrize("tool,method,parameter,extra", [
    ("memory_supersede", "supersede", "old_text", {"new_text": "replacement"}),
    ("memory_store", "store", "text", {}),
])
def test_json_shaped_string_reaches_service(
        tmp_path, monkeypatch, value, tool, method, parameter, extra):
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    received = []

    def capture(**kwargs):
        received.append(kwargs[parameter])
        return {"received": kwargs[parameter]}

    monkeypatch.setattr(mod.service, method, capture)
    invoke_tool(tool, {parameter: value, **extra})
    assert received == [value]
    assert type(received[0]) is str


def test_all_registered_string_fields_preserve_json_text(tmp_path, monkeypatch):
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    for tool in mod.mcp._tool_manager.list_tools():
        for name, field in tool.fn_metadata.arg_model.model_fields.items():
            if field.annotation is str or field.annotation == str | None:
                for value in ['{"a": 1}', '[1, 2]', 'null']:
                    arguments = {name: value}
                    assert tool.fn_metadata.pre_parse_json(arguments) == arguments
                    assert arguments == {name: value}


def test_optional_none_and_encoded_lists_keep_their_meaning(tmp_path, monkeypatch):
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.service, "supersede", lambda **kwargs: kwargs)
    result = invoke_tool("memory_supersede", {
        "old_text": None, "entry_id": 7, "new_text": '{"a": 2}',
    })
    assert result == {"old_text": None, "entry_id": 7, "new_text": '{"a": 2}'}
    monkeypatch.setattr(mod.service, "store", lambda **kwargs: kwargs)
    for tags in ['["decision", "123"]', ["decision", "123"]]:
        result = invoke_tool("memory_store", {"text": "null", "tags": tags})
        assert result["text"] == "null"
        assert result["tags"] == ["decision", "123"]


@pytest.mark.parametrize("value", [{"a": 1}, [1, 2], 123, True])
def test_non_string_old_text_is_still_rejected(tmp_path, monkeypatch, value):
    from mcp.server.mcpserver.exceptions import ToolError

    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    received = []
    monkeypatch.setattr(mod.service, "supersede", lambda **kwargs: received.append(kwargs))
    with pytest.raises(ToolError, match="Input should be a valid string"):
        invoke_tool("memory_supersede", {"old_text": value, "new_text": "replacement"})
    assert received == []
