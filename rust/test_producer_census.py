"""Current-tree census currency and conservative producer extraction."""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location("producer_census", Path(__file__).with_name("producer_census.py"))
census = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(census)


def test_committed_census_matches_current_tree():
    assert json.loads(census.OUTPUT.read_text(encoding="utf-8")) == census.snapshot()


def test_tools_preserve_false_null_and_omitted_parameters():
    calls = census.tool_calls('memory_search(query="topic", rerank=False, sources=None)', "examples/demo.py")
    assert calls[0]["shape"]["parameters"] == {
        "query": {"literal": "topic"}, "rerank": {"literal": False}, "sources": {"literal": None}}
    assert calls[0]["shape"]["complete"]


def test_forwarded_keywords_never_claim_complete_shape():
    call = census.tool_calls('memory_search(query=q, **kwargs)', "examples/demo.py")[0]
    assert not call["shape"]["complete"]
    assert call["dynamic"] == "unresolved"


def test_balanced_objects_keep_nested_values_and_quoted_commas():
    fields, complete = census.object_fields('{id: row.id, label: "a,b", options: {a: true, b: [1,2]}}')
    assert complete
    assert set(fields) == {"id", "label", "options"}
    assert fields["label"] == {"literal": "a,b"}


def test_console_helpers_methods_and_bodies_are_not_route_mentions():
    text = 'function X(e,t){return R(`GET`,e,{params:t})} function Y(e,t={}){return R(`POST`,e,{body:t})} X(`/api/facts`,{limit:1000}); Y(`/api/facts/set`,{entity:e, value:null}); // /api/search'
    calls = census.console_calls(text, "pseudolife_memory/web/static/assets/demo.js")
    assert [(c["name"], c["method"]) for c in calls] == [("/api/facts", "GET"), ("/api/facts/set", "POST")]
    assert calls[1]["shape"]["parameters"]["value"] == {"literal": None}


def test_spread_body_has_unknown_omissions():
    call = census.console_calls('post("/api/facts/set", {...form})', "frontend/src/lib/api/facts.ts")[0]
    assert not call["shape"]["complete"]
    assert call["dynamic"] == "unresolved"


def test_new_producer_and_changed_literal_make_snapshot_stale(tmp_path):
    (tmp_path / "examples").mkdir()
    path = tmp_path / "examples/demo.md"
    path.write_text('`memory_search(query="one")`', encoding="utf-8")
    before = census.scan_files(tmp_path, ["examples/demo.md"])
    path.write_text('`memory_search(query="two")`', encoding="utf-8")
    assert before != census.scan_files(tmp_path, ["examples/demo.md"])
    (tmp_path / "examples/new.md").write_text('`memory_stats()`', encoding="utf-8")
    assert "examples/new.md" in census.producer_paths(tmp_path)


def test_inventory_gaps_and_unreached_items_are_separate():
    surface = [{"id": "tool:memory_search", "kind": "tool", "name": "memory_search", "optional_parameters": ["rerank"]},
               {"id": "tool:memory_stats", "kind": "tool", "name": "memory_stats", "optional_parameters": []}]
    calls = census.tool_calls('memory_search(query="one"); memory_unknown(action="list")', "examples/demo.py")
    matched, gaps = census.match_surface(surface, calls)
    assert matched[0]["producers"][0]["shape"]["omitted_optional"] == ["rerank"]
    assert matched[1]["producers"] == []
    assert gaps[0]["name"] == "memory_unknown"


def test_unresolved_shape_does_not_invent_omitted_optional():
    surface = [{"id": "tool:memory_search", "kind": "tool", "name": "memory_search", "optional_parameters": ["rerank"]}]
    matched, _ = census.match_surface(surface, census.tool_calls('memory_search(**params)', "examples/demo.py"))
    assert matched[0]["producers"][0]["shape"]["omitted_optional"] is None


def test_input_hash_detects_even_unrecognized_dynamic_source_changes(tmp_path):
    (tmp_path / "plugin").mkdir()
    path = tmp_path / "plugin/demo.sh"
    path.write_text('curl "$URL/$ROUTE"', encoding="utf-8")
    before = census.input_hashes(tmp_path, ["plugin/demo.sh"])
    path.write_text('curl "$URL/$OTHER"', encoding="utf-8")
    assert before != census.input_hashes(tmp_path, ["plugin/demo.sh"])


def test_checkout_line_endings_do_not_change_source_identity(tmp_path):
    path = tmp_path / "demo.md"
    path.write_bytes(b'one\r\ntwo\r\n')
    windows = census.input_hashes(tmp_path, ["demo.md"])
    path.write_bytes(b'one\ntwo\n')
    assert windows == census.input_hashes(tmp_path, ["demo.md"])


def test_shell_quoted_variable_is_expression_not_literal():
    call = census.env_calls('PSEUDOLIFE_MCP_TOKEN="$TOKEN"', "ops/demo.sh")[0]
    assert call["shape"]["parameters"]["value"] == {"expression": '"$TOKEN"'}


def test_package_mentions_and_newline_prose_are_not_cli_invocations():
    assert not census.cli_calls('# pseudolife-mcp venv\nx="bare pseudolife-mcp to the launcher"', "ops/demo.sh")
    assert not census.cli_calls('`pseudolife-mcp`\nstill works', "docs/demo.md")
    assert not census.cli_calls('```sh\npipx runpip pseudolife-mcp install mcp\n```', "docs/demo.md")


def test_console_pure_parameter_helper_resolves_literals():
    calls = census.console_calls('function P(e,t){return{q:e.q,top_k:t,rerank:e.rerank?!0:void 0}} get("/api/search",P(a,25))', "console.js")
    assert calls[0]["shape"]["parameters"]["top_k"] == {"literal": 25}
    assert calls[0]["shape"]["parameters"]["rerank"] == {"expression": "a.rerank?!0:void 0"}


def test_manual_resolution_refuses_source_drift(tmp_path):
    path = tmp_path / "plugin/hooks/lifecycle.ps1"
    path.parent.mkdir(parents=True)
    path.write_text('changed request construction', encoding="utf-8")
    import pytest
    with pytest.raises(AssertionError, match="manual resolution needs source review"):
        census.manual_calls(tmp_path)


def test_shell_assignment_inside_output_string_excludes_closing_quote():
    call = census.env_calls('Write-Host "  PSEUDOLIFE_DREAM_MODEL=extractor"', "ops/demo.ps1")[0]
    assert call["shape"]["parameters"]["value"] == {"literal": "extractor"}


def test_cli_documented_optional_groups_are_unresolved():
    call = census.cli_calls('```sh\npseudolife-mcp expose status [--port 8765] [--json]\n```', "docs/demo.md")[0]
    assert not call["shape"]["complete"]


def test_module_import_is_not_a_config_key_reference():
    assert not census.config_calls('from pseudolife_memory.memory.dream import NoOpExtractor', "docs/demo.md", {"memory.dream": "MemoryConfig.dream"})
