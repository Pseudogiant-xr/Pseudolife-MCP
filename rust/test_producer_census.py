"""Current-tree census currency and conservative producer extraction."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

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
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "examples").mkdir()
    path = tmp_path / "examples/demo.md"
    path.write_text('`memory_search(query="one")`', encoding="utf-8")
    subprocess.run(["git", "add", "examples/demo.md"], cwd=tmp_path, check=True)
    before = census.scan_files(tmp_path, ["examples/demo.md"])
    path.write_text('`memory_search(query="two")`', encoding="utf-8")
    assert before != census.scan_files(tmp_path, ["examples/demo.md"])
    (tmp_path / "examples/new.md").write_text('`memory_stats()`', encoding="utf-8")
    subprocess.run(["git", "add", "examples/new.md"], cwd=tmp_path, check=True)
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


def test_manual_resolution_reports_source_drift_without_trusting_old_shape(tmp_path):
    path = tmp_path / "plugin/hooks/lifecycle.ps1"
    path.parent.mkdir(parents=True)
    path.write_text('changed request construction', encoding="utf-8")
    assert census.manual_calls(tmp_path) == []
    assert any(row["path"] == "plugin/hooks/lifecycle.ps1" for row in census.manual_drift(tmp_path))


def test_shell_assignment_inside_output_string_excludes_closing_quote():
    call = census.env_calls('Write-Host "  PSEUDOLIFE_DREAM_MODEL=extractor"', "ops/demo.ps1")[0]
    assert call["shape"]["parameters"]["value"] == {"literal": "extractor"}


def test_cli_documented_optional_groups_are_unresolved():
    call = census.cli_calls('```sh\npseudolife-mcp expose status [--port 8765] [--json]\n```', "docs/demo.md")[0]
    assert not call["shape"]["complete"]


def test_module_import_is_not_a_config_key_reference():
    assert not census.config_calls('from pseudolife_memory.memory.dream import NoOpExtractor', "docs/demo.md", {"memory.dream": "MemoryConfig.dream"})


def test_powershell_function_result_is_not_an_environment_literal():
    call = census.env_calls('$env:PSEUDOLIFE_DESKTOP_TOKEN_SOURCE = Get-DesktopTokenSource', "ops/demo.ps1")[0]
    assert call["shape"]["parameters"]["value"] == {"expression": "Get-DesktopTokenSource"}


def test_command_failure_message_is_not_a_cli_call():
    assert not census.cli_calls('echo "WARNING: pseudolife-mcp expose tailscale exited $rc"', "ops/demo.sh")


def test_coordination_transport_method_does_not_turn_known_action_into_gap():
    surface = [{"id": "coordination:receive", "kind": "coordination", "name": "receive", "optional_parameters": ["after", "limit"]}]
    call = census.record("coordination", "receive", "rust/shim/src/board/adapter.rs", 1230,
                         {"limit": {"literal": 50}}, method="POST", channel="body")
    matched, gaps = census.match_surface(surface, [call])
    assert gaps == []
    assert matched[0]["producers"][0]["shape"]["omitted_optional"] == ["after"]


def test_console_local_constants_do_not_replace_unrelated_parameter_bindings():
    text = 'function unrelated(){var e=0,t=0,n=30000;} post("/api/facts/resolve",{entity:e,attribute:t,accept:n})'
    fields = census.console_calls(text, "console.js")[0]["shape"]["parameters"]
    assert fields == {"entity": {"expression": "e"}, "attribute": {"expression": "t"}, "accept": {"expression": "n"}}


def test_multiline_rust_targets_are_at_least_unresolved():
    text = 'post_json(\n &url,\n "/api/episode/end",\n body.as_bytes(),\n);'
    calls = census.http_mentions(text, "rust/shim/src/lifecycle.rs")
    assert len(calls) == 1
    assert calls[0]["name"] == "/api/episode/end"
    assert calls[0]["dynamic"] == "unresolved"


def test_comments_in_copyable_tool_calls_preserve_following_keywords():
    text = 'memory_consolidate(query="topic", entry_ids=[10,20], # chosen rows\n new_text="merged")'
    call = census.tool_calls(text, "docs/demo.md")[0]
    assert set(call["shape"]["parameters"]) == {"query", "entry_ids", "new_text"}
    assert not call["shape"].get("positional")
    matched, _ = census.match_surface([{"id": "tool:memory_consolidate", "kind": "tool", "name": "memory_consolidate", "parameters": ["query", "entry_ids", "new_text", "replaces"], "optional_parameters": ["replaces"]}], [call])
    assert matched[0]["producers"][0]["shape"]["omitted_optional"] == ["replaces"]


def test_installer_argv_preserves_adjacent_flags_and_quoted_expressions():
    call = census.cli_calls('pseudolife-mcp connect "$URL" --token-file "$FILE" --client "$NAMES" --dry-run --json', "ops/demo.sh")[0]
    assert call["shape"]["parameters"] == {"--token-file": {"expression": '"$FILE"'}, "--client": {"expression": '"$NAMES"'}, "--dry-run": {"literal": True}, "--json": {"literal": True}}


def test_installer_alias_does_not_match_arguments_assignments_or_next_line():
    text = 'printf "%s" "$SHIM_PATH"\nfi\nx="$SHIM_PATH"\nif true; then\n "$SHIM_PATH" connect "$URL" --json\nfi\n'
    calls = census.installer_cli_calls(text, "ops/install.sh")
    assert [call["name"] for call in calls] == ["connect"]
    assert calls[0]["location"] == "ops/install.sh:5"


def test_installer_alias_after_rejected_same_line_candidate_is_retained():
    calls = census.installer_cli_calls('echo "$SHIM_PATH" x; "$SHIM_PATH" connect "$URL" --json', "ops/install.sh")
    assert [call["name"] for call in calls] == ["connect"]


def test_shell_assignment_to_shim_path_is_not_an_invocation():
    assert not census.installer_cli_calls('X="$SHIM_PATH" connect', "ops/install.sh")


def test_installer_arguments_stop_at_the_next_command():
    calls = census.installer_cli_calls('"$SHIM_PATH" connect "$URL"; "$SHIM_PATH" expose tailscale --yes', "ops/install.sh")
    assert [call["name"] for call in calls] == ["connect", "expose"]
    assert "--yes" not in calls[0]["shape"]["parameters"]


def test_real_installer_pair_keeps_json_switch_after_command_substitution():
    text = (census.ROOT / "ops/install.sh").read_text(encoding="utf-8-sig")
    pairs = [call for call in census.installer_cli_calls(text, "ops/install.sh") if call["name"] == "pair"]
    assert pairs and all(call["shape"]["parameters"].get("--json") == {"literal": True} for call in pairs)


def test_documented_optional_switches_keep_bracketed_names():
    call = census.cli_calls('```sh\npseudolife-mcp pair URL [--read-code] [--json]\n```', "docs/demo.md")[0]
    assert set(call["shape"]["parameters"]) == {"--read-code", "--json"}
    assert not call["shape"]["complete"]


def test_command_tail_stops_at_and_close_or_comment_without_losing_literal_values():
    for end in [' && other --yes', ') || other --yes', ' # other --yes']:
        call = census.installer_cli_calls('"$SHIM_PATH" connect "$URL" --json' + end, "ops/install.sh")[0]
        assert call["shape"]["parameters"] == {"--json": {"literal": True}}
    call = census.installer_cli_calls('"$SHIM_PATH" connect "value#with)paren&&text" --json', "ops/install.sh")[0]
    assert call["shape"]["parameters"] == {"--json": {"literal": True}}


def test_powershell_assignment_and_shell_env_prefix_retain_installer_call():
    ps = census.installer_cli_calls('$planOutput = & $shim connect $URL --dry-run --json', "ops/install.ps1")
    sh = census.installer_cli_calls('PSEUDOLIFE_MCP_TOKEN_FILE="$board_file" "$SHIM_PATH" maintainer setup --yes', "ops/install.sh")
    assert [call["name"] for call in ps] == ["connect"]
    assert [call["name"] for call in sh] == ["maintainer"]


def test_installer_prose_about_package_source_is_not_a_cli_mode():
    assert not census.cli_calls('Write-Host "Python shim: pseudolife-mcp from this checkout"', "ops/install.ps1")


def test_windows_shell_path_is_not_decoded_as_python_escapes():
    call = census.env_calls(r'PSEUDOLIFE_MCP_CONFIG="C:\temp\config.yaml"', "ops/demo.ps1")[0]
    assert call["shape"]["parameters"]["value"] == {"expression": '"C:\\temp\\config.yaml"'}


def test_real_help_placeholders_are_full_unresolved_operands():
    cases = [
        ("rust/shim/src/lifecycle.rs", "PSEUDOLIFE_MCP_TOKEN", "<the token>"),
        ("rust/shim/src/lifecycle.rs", "PSEUDOLIFE_MCP_TOKEN_FILE", "<absolute path to a private file holding the token>"),
        ("rust/shim/src/cli_help.txt", "PSEUDOLIFE_MCP_TOKEN_FILE", "<owner-only file holding it>"),
    ]
    for path, name, operand in cases:
        calls = census.env_calls((census.ROOT / path).read_text(encoding="utf-8-sig"), path)
        call = next(row for row in calls if row["name"] == name and operand in row["shape"].get("expression", ""))
        assert call["shape"]["parameters"]["value"] == {"expression": operand, "placeholder": True}
        assert not call["shape"]["complete"]
        assert call["dynamic"] == "unresolved"
        assert call["usage"] == "assignment-example"


def test_compound_and_quoted_env_placeholders_are_not_literals():
    for operand in ['<token-1>:<machine>-client', '"<a multiword path>"']:
        call = census.env_calls('PSEUDOLIFE_MCP_TOKEN=' + operand, "docs/demo.md")[0]
        assert call["shape"]["parameters"]["value"] == {"expression": operand, "placeholder": True}
        assert not call["shape"]["complete"]


def test_actual_env_scalar_remains_literal():
    call = census.env_calls('PSEUDOLIFE_MCP_PORT=8765', "ops/demo.sh")[0]
    assert call["shape"]["parameters"]["value"] == {"literal": "8765"}
    assert call["shape"]["complete"]


def test_multiword_placeholder_keeps_compound_suffix():
    operand = '<absolute path>/token.txt'
    call = census.env_calls('PSEUDOLIFE_MCP_TOKEN_FILE=' + operand, "docs/demo.md")[0]
    assert call["shape"]["parameters"]["value"] == {"expression": operand, "placeholder": True}


def test_csv_placeholder_operand_is_whole_but_help_punctuation_is_not_value():
    operand = '<token-1>:<machine>-client,<token-2>:<machine>-other'
    call = census.env_calls('PSEUDOLIFE_MCP_TOKENS=' + operand, "docs/demo.md")[0]
    assert call["shape"]["parameters"]["value"] == {"expression": operand, "placeholder": True}
    help_call = census.env_calls('PSEUDOLIFE_MCP_TOKEN=<bearer>, or use a file', "rust/shim/src/cli_help.txt")[0]
    assert help_call["shape"]["parameters"]["value"]["expression"] == '<bearer>'


def test_less_than_in_actual_scalar_path_is_not_placeholder_syntax():
    for operand in ['"/tmp/a<b.yaml"', '"/tmp/a<b>.yaml"']:
        call = census.env_calls('PSEUDOLIFE_MCP_TOKEN_FILE=' + operand, "ops/demo.sh")[0]
        assert call["shape"]["parameters"]["value"] == {"literal": operand[1:-1]}
        assert call["shape"]["complete"]
        assert call["usage"] == "assignment"


def test_prose_parenthetical_is_not_a_tool_call():
    assert not census.tool_calls('memory_fact_resolve (core); memory_set_add (a set-valued slot errors here)', "docs/demo.md")


def test_internal_docstring_negative_examples_are_not_mcp_producers():
    text = 'def _bind():\n    """memory_search(limit=3)"""\n\n@_tool()\ndef memory_search(query):\n    """Try memory_search(query="topic")."""\n'
    calls = census.description_calls(text)
    assert len(calls) == 1
    assert calls[0]["shape"]["parameters"] == {"query": {"literal": "topic"}}


def test_malformed_repeated_env_prefix_finishes_in_a_bounded_child():
    # CodeQL's counterexample: adjacent empty quoted values can make the
    # executable-position regex backtrack exponentially. Own child, capped at 10s.
    script = ('import importlib.util; '
              f's=importlib.util.spec_from_file_location("census", {str(Path(census.__file__))!r}); '
              'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
              'assert m.installer_cli_calls("A=" + (\'""\\tA=\' * 26) + "#", "ops/install.sh") == []')
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_untracked_and_ignored_files_cannot_enter_shipped_scope(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples/public.md").write_text('memory_stats()', encoding="utf-8")
    (tmp_path / "examples/private.md").write_text('memory_search(query="private")', encoding="utf-8")
    (tmp_path / "examples/ignored.md").write_text('memory_search(query="ignored")', encoding="utf-8")
    (tmp_path / ".gitignore").write_text('examples/ignored.md\n', encoding="utf-8")
    subprocess.run(["git", "add", "examples/public.md", ".gitignore"], cwd=tmp_path, check=True)
    assert census.producer_paths(tmp_path) == ["examples/public.md"]


def test_snapshot_is_independent_of_source_commit_history(monkeypatch):
    original = census.subprocess.check_output
    def history(first):
        def output(argv, **kwargs):
            if argv[:2] == ["git", "log"]:
                return first + "\n"
            return original(argv, **kwargs)
        return output
    monkeypatch.setattr(census.subprocess, "check_output", history("a" * 40))
    before = census.snapshot()
    monkeypatch.setattr(census.subprocess, "check_output", history("b" * 40))
    assert census.snapshot() == before


def test_module_form_cli_real_tree_unattended_update_is_present():
    text = (census.ROOT / "rust/shim/src/lifecycle.rs").read_text(encoding="utf-8-sig")
    calls = census.module_cli_calls(text.split("#[cfg(test)]")[0], "rust/shim/src/lifecycle.rs")
    update = next(call for call in calls if call["name"] == "update")
    assert update["shape"]["parameters"]["--clients-only"] == {"literal": True}
    assert "--result-file" in update["shape"]["parameters"]
    assert update["shape"]["parameters"]["--result-file"] == {"expression": "result.to_string_lossy().into_owned()"}


def test_module_form_cli_text_array_and_forwarded_modes():
    assert census.module_cli_calls('python -m pseudolife_memory.cli serve', "ops/demo.sh")[0]["name"] == "serve"
    assert census.module_cli_calls('CMD ["python", "-m", "pseudolife_memory.cli", "serve"]', "ops/Dockerfile")[0]["name"] == "serve"
    forwarded = census.module_cli_calls('["-m".into(), "pseudolife_memory.cli".into(), mode.into()]', "rust/shim/src/pg/resolution.rs")[0]
    assert forwarded["dynamic"] == "unresolved"


def test_coordination_tool_forwarding_is_annotated_without_inventing_wire_shape():
    calls = census.tool_calls('memory_agents(action="update", status="ready"); memory_message(action="ack", message_id=id)', "plugin/demo.md")
    forwarded = census.forwarded_coordination(calls)
    assert [call["name"] for call in forwarded] == ["update", "ack"]
    assert all(not call["shape"]["complete"] for call in forwarded)
    assert forwarded[0]["forwarded_from_tool"] == "memory_agents"


def test_bare_documented_action_is_only_unverified_forwarding():
    calls = census.tool_calls('memory_message(action=ack, message_id=id)', "ops/install-hook.sh")
    forwarded = census.forwarded_coordination(calls)
    assert forwarded[0]["name"] == "ack"
    assert forwarded[0]["evidence"] == "unverified"


def test_current_tree_census_has_real_producer_floors():
    result = census.snapshot()
    surface = {item["id"]: item for item in result["surface"]}
    console = [producer for item in result["surface"] if item["kind"] == "route" for producer in item["producers"]
               if producer["location"].startswith("pseudolife_memory/web/static/")]
    # The 2026-10-10 shipped index-DNrrQi0Q.js has 51 call/plan records;
    # a floor of 40 catches substantial loss while allowing normal bundle changes.
    assert len(console) >= 40
    assert any(p["location"].startswith("plugin/") for p in surface["tool:memory_search"]["producers"])
    manual = [p for item in result["surface"] for p in item["producers"] if p["evidence"] == "manual"]
    drifted = {item["path"] for item in result.get("manual_drift", [])}
    if not drifted:
        assert len(manual) == 17
    assert not any(p["location"].rsplit(":", 1)[0] in drifted for p in manual)


def test_ci_reports_source_drift_but_gates_census_changes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "examples").mkdir()
    (tmp_path / "rust").mkdir()
    doc = tmp_path / "examples/demo.md"
    generated = tmp_path / "rust/producer-census.json"
    doc.write_text('memory_stats()', encoding="utf-8")
    generated.write_text('{}', encoding="utf-8")
    def commit():
        subprocess.run(["git", "add", "examples/demo.md", "rust/producer-census.json"], cwd=tmp_path, check=True)
        subprocess.run(["git", "-c", "user.name=Census fixture", "-c", "user.email=census@example.com",
                        "commit", "-qm", "Fixture source update"], cwd=tmp_path, check=True)
    commit()
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": base}), encoding="utf-8")
    doc.write_text('memory_stats(); memory_search(query="new")', encoding="utf-8")
    commit()
    assert not census.ci_currency_required(tmp_path, {"GITHUB_EVENT_PATH": str(event)})
    generated.write_text('{"regenerated": true}', encoding="utf-8")
    commit()
    assert census.ci_currency_required(tmp_path, {"GITHUB_EVENT_PATH": str(event)})
    event.write_text(json.dumps({"before": "f" * 40}), encoding="utf-8")
    assert not census.ci_currency_required(tmp_path, {"GITHUB_EVENT_PATH": str(event)})
