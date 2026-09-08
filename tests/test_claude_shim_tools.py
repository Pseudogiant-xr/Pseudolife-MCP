"""``--emulate-tools``: OpenAI function calling over a CLI that has none.

``claude -p`` is a pure completion — the shim launches it with ``--tools ""``
and no MCP servers. The tau2-bench harness (litellm) needs OpenAI-format tool
calling on the same endpoint: ``tools`` in, ``choices[0].message.tool_calls``
out with ``arguments`` as a JSON STRING it parses with ``json.loads`` (a parse
failure aborts the episode). The shim emulates that in the prompt.

The flag is opt-in because the deployed dream primary on :8082 must keep its
byte-for-byte wire format: with the flag off a request carrying ``tools`` is
served exactly as before, tools unrendered and no ``tool_calls`` key.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "claude_shim", REPO / "evals" / "claude_shim.py")
shim = importlib.util.module_from_spec(spec)
sys.modules["claude_shim"] = shim
spec.loader.exec_module(shim)


TOOLS = [
    {"type": "function", "function": {
        "name": "get_reservation",
        "description": "Look up a reservation by its identifier.",
        "parameters": {"type": "object",
                       "properties": {"reservation_id": {"type": "string"}},
                       "required": ["reservation_id"]}}},
    {"type": "function", "function": {
        "name": "cancel_reservation",
        "description": "Cancel a reservation the user owns.",
        "parameters": {"type": "object",
                       "properties": {"reservation_id": {"type": "string"}}}}},
]


def _cli(replies, emulate=True):
    """A ClaudeCli whose ``chat`` returns ``replies`` in order and records the
    (system, user) pair it was handed. The CLI itself never runs."""
    cli = shim.ClaudeCli(Path("claude.exe"), "claude-sonnet-5", 30.0,
                         emulate_tools=emulate)
    seen: list[dict] = []
    pending = list(replies)

    def _chat(system, user, model=None, effort=None):
        seen.append({"system": system, "user": user,
                     "model": model, "effort": effort})
        return pending.pop(0) if pending else replies[-1]

    cli.chat = _chat                                   # type: ignore[method-assign]
    return cli, seen


def _post(cli, body: dict) -> dict:
    srv = shim.ThreadingHTTPServer(("127.0.0.1", 0), shim.make_handler(cli))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    finally:
        srv.shutdown()


def _request(**extra) -> dict:
    body = {"model": "claude-sonnet-5",
            "messages": [{"role": "system", "content": "You are an agent."},
                         {"role": "user", "content": "cancel booking B1"}],
            "tools": TOOLS, "tool_choice": "auto"}
    body.update(extra)
    return body


# ── rendering ─────────────────────────────────────────────────────────────


def test_tools_are_rendered_into_the_system_prompt():
    cli, seen = _cli(["I can help with that."])
    _post(cli, _request())
    system = seen[0]["system"]
    assert "You are an agent." in system                # caller's own prompt
    assert "tool_call" in system                        # the preamble contract
    for spec_ in TOOLS:
        fn = spec_["function"]
        assert fn["name"] in system
        assert fn["description"] in system
    # The JSON-schema parameters reach the model, not just the names.
    assert "reservation_id" in system
    assert '"required"' in system or "required" in system


def test_tool_choice_none_suppresses_rendering():
    cli, seen = _cli(["No tools for you."])
    out = _post(cli, _request(tool_choice="none"))
    system = seen[0]["system"]
    assert "get_reservation" not in system
    assert "tool_call" not in system
    assert out["choices"][0]["message"] == {"role": "assistant",
                                            "content": "No tools for you."}


def test_tool_choice_dict_is_treated_as_auto():
    cli, seen = _cli(["ok"])
    _post(cli, _request(tool_choice={"type": "function",
                                     "function": {"name": "get_reservation"}}))
    assert "get_reservation" in seen[0]["system"]


# ── the emitted wire format ───────────────────────────────────────────────


def test_json_tool_call_reply_becomes_an_openai_tool_call():
    reply = json.dumps({"tool_call": {"name": "get_reservation",
                                      "arguments": {"reservation_id": "B1"}}})
    cli, _ = _cli([reply])
    out = _post(cli, _request())
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    msg = choice["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] is None
    calls = msg["tool_calls"]
    assert len(calls) == 1
    call = calls[0]
    assert call["type"] == "function"
    assert call["id"].startswith("call_") and len(call["id"]) == len("call_") + 8
    assert int(call["id"][len("call_"):], 16) >= 0      # 8 hex digits
    assert call["function"]["name"] == "get_reservation"
    args = call["function"]["arguments"]
    assert isinstance(args, str), "litellm json.loads()es this field"
    assert json.loads(args) == {"reservation_id": "B1"}


def test_fenced_tool_call_reply_still_parses():
    reply = ("```json\n"
             + json.dumps({"tool_call": {"name": "cancel_reservation",
                                         "arguments": {}}})
             + "\n```")
    cli, _ = _cli([reply])
    out = _post(cli, _request())
    call = out["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "cancel_reservation"
    assert json.loads(call["function"]["arguments"]) == {}


def test_plain_text_reply_is_served_as_text():
    cli, _ = _cli(["Your booking is cancelled."])
    out = _post(cli, _request())
    choice = out["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {"role": "assistant",
                                 "content": "Your booking is cancelled."}
    assert "tool_calls" not in choice["message"]


# ── transcript folding ────────────────────────────────────────────────────


def test_tool_history_is_folded_into_the_stdin_transcript():
    cli, seen = _cli(["Done."])
    _post(cli, _request(messages=[
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "cancel booking B1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_aaaaaaaa", "type": "function", "function": {
                "name": "get_reservation",
                "arguments": '{"reservation_id": "B1"}'}}]},
        {"role": "tool", "tool_call_id": "call_aaaaaaaa",
         "content": '{"status": "active"}'},
        {"role": "user", "content": "go ahead"},
    ]))
    user = seen[0]["user"]
    called = user.index("[assistant called tool get_reservation with "
                        '{"reservation_id": "B1"}]')
    returned = user.index('[tool get_reservation returned: '
                          '{"status": "active"}]')
    assert called < returned
    assert "cancel booking B1" in user and "go ahead" in user
    assert "You are an agent." not in user      # system stays the system prompt


# ── the near-JSON retry ───────────────────────────────────────────────────


def test_near_json_reply_is_retried_once_and_then_parses():
    bad = '{"tool_call": {"name": "get_reservation", "arguments": {,}}'
    good = json.dumps({"tool_call": {"name": "get_reservation",
                                     "arguments": {"reservation_id": "B1"}}})
    cli, seen = _cli([bad, good])
    out = _post(cli, _request())
    assert len(seen) == 2, "a near-JSON tool call must be retried exactly once"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert (out["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
            == "get_reservation")


def test_two_bad_replies_fall_back_to_text_without_raising():
    bad = '{"tool_call": {"name": "get_reservation", "arguments": {,}}'
    cli, seen = _cli([bad, bad])
    out = _post(cli, _request())
    assert len(seen) == 2                        # retried once, then gave up
    choice = out["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == bad
    assert "tool_calls" not in choice["message"]


def test_plain_text_is_never_retried():
    cli, seen = _cli(["Sure, one moment."])
    _post(cli, _request())
    assert len(seen) == 1


# ── a tool call the model introduced with prose ───────────────────────────


def test_a_tool_call_wrapped_in_prose_is_still_a_tool_call():
    """Models narrate. "Sure, let me check." in front of the object is the
    single most common way the contract is broken, and treating it as a chat
    turn silently costs the episode the call it was making."""
    reply = ("Sure, let me look that up.\n\n```json\n"
             + json.dumps({"tool_call": {"name": "get_reservation",
                                         "arguments": {"reservation_id": "B1"}}})
             + "\n```\nI'll follow up once I have it.")
    cli, seen = _cli([reply])
    out = _post(cli, _request())
    assert len(seen) == 1, "a parseable call needs no retry"
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_reservation"
    assert json.loads(call["function"]["arguments"]) == {"reservation_id": "B1"}


def test_near_json_wrapped_in_prose_is_retried_like_a_bare_one():
    bad = 'One moment.\n{"tool_call": {"name": "get_reservation", "arguments": {,}}'
    good = json.dumps({"tool_call": {"name": "get_reservation",
                                     "arguments": {"reservation_id": "B1"}}})
    cli, seen = _cli([bad, good])
    out = _post(cli, _request())
    assert len(seen) == 2
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_prose_carrying_unrelated_json_is_left_alone():
    """The fallback keys on ``tool_call``, so an answer that merely contains
    JSON stays an answer — and is never retried."""
    reply = 'Your booking record looks like {"status": "active"} right now.'
    cli, seen = _cli([reply])
    out = _post(cli, _request())
    assert len(seen) == 1
    assert out["choices"][0]["message"]["content"] == reply
    assert out["choices"][0]["finish_reason"] == "stop"


# ── the flag is off by default and inert when off ─────────────────────────


def test_emulate_tools_defaults_off():
    assert shim._parse_args([]).emulate_tools is False
    assert shim._parse_args(["--emulate-tools"]).emulate_tools is True


def test_emulate_tools_refuses_every_deployed_shim_port():
    """:8082 is the deployed Claude shim and :8086 the deployed Codex shim
    (tests/test_bench_production_port_guard.py). Launching the emulating
    build over either serves a wire format its callers do not expect, and
    the bind can even succeed on Windows while the incumbent keeps the
    traffic."""
    assert set(shim.PRODUCTION_SHIM_PORTS) == {8082, 8086}
    for port in shim.PRODUCTION_SHIM_PORTS:
        message = shim.production_port_refusal(port)
        assert message and str(port) in message, port
    assert shim.production_port_refusal(8092) is None


def test_with_the_flag_off_a_tools_request_is_served_exactly_as_today():
    cli, seen = _cli(["Your booking is cancelled."], emulate=False)
    out = _post(cli, _request())
    system = seen[0]["system"]
    assert system == "You are an agent."          # no rendering whatsoever
    assert "tool_call" not in system
    assert seen[0]["user"] == "cancel booking B1"  # no role labels either
    choice = out["choices"][0]
    assert choice["message"] == {"role": "assistant",
                                 "content": "Your booking is cancelled."}
    assert "tool_calls" not in choice["message"]
    assert choice["finish_reason"] == "stop"


def test_with_the_flag_off_a_json_tool_call_reply_stays_text():
    reply = json.dumps({"tool_call": {"name": "get_reservation",
                                      "arguments": {}}})
    cli, _ = _cli([reply], emulate=False)
    out = _post(cli, _request())
    assert out["choices"][0]["message"]["content"] == reply
    assert out["choices"][0]["finish_reason"] == "stop"


# ── the per-request log line (how the smoke run proves the path) ──────────


def test_one_info_line_per_request_reports_the_emulation_fields(caplog):
    reply = json.dumps({"tool_call": {"name": "get_reservation",
                                      "arguments": {"reservation_id": "B1"}}})
    cli, _ = _cli([reply])
    with caplog.at_level(logging.INFO, logger="claude_shim"):
        _post(cli, _request())
    lines = [r.getMessage() for r in caplog.records
             if r.levelno == logging.INFO]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "emulate=on" in line
    assert "n_tools=2" in line
    assert "tool_call=yes" in line
    assert "retried=no" in line


def test_the_log_line_reports_the_off_state_and_the_retry(caplog):
    bad = '{"tool_call": {"name": "get_reservation", "arguments": {,}}'
    cli, _ = _cli([bad, bad])
    with caplog.at_level(logging.INFO, logger="claude_shim"):
        _post(cli, _request())
    line = [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO][0]
    assert "retried=yes" in line and "tool_call=no" in line

    cli, _ = _cli(["hi"], emulate=False)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="claude_shim"):
        _post(cli, _request())
    line = [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO][0]
    assert "emulate=off" in line and "n_tools=2" in line


# ── the folded-history markers must not be imitated (2026-09-08 smoke) ──────

def test_preamble_tells_the_model_the_history_markers_are_not_its_format():
    """In the first tau2 smoke the model twice answered with the shim's own
    transcript marker — ``[assistant called tool KB_search with {...}]`` —
    as plain text instead of a JSON call, and the customer then reacted to
    that garbage. The preamble names the markers and says never to write
    them."""
    text = shim.render_tools(TOOLS)
    assert "[assistant called tool" in text
    assert "[tool" in text and "returned:" in text
    assert "never" in text.lower()


def test_an_imitated_history_marker_is_recovered_as_a_tool_call():
    """Belt and braces for the same failure: a reply that IS a marker still
    becomes the call it names, not a chat turn."""
    reply = '[assistant called tool get_reservation with {"reservation_id": "R1"}]'
    calls = shim.parse_tool_reply(reply)
    assert calls and calls[0]["function"]["name"] == "get_reservation"
    assert json.loads(calls[0]["function"]["arguments"]) == {"reservation_id": "R1"}
    assert shim.looks_like_tool_call(reply)

    cli, _ = _cli([reply])
    out = _post(cli, _request())
    msg = out["choices"][0]["message"]
    assert msg["content"] is None
    assert msg["tool_calls"][0]["function"]["name"] == "get_reservation"
    assert out["choices"][0]["finish_reason"] == "tool_calls"


# A wrapper tool whose own ``arguments`` field is a JSON-encoded STRING —
# the shape under which the 2026-09-09 full baseline lost five of its first
# 37 episodes: the model wrote the escaped inner object correctly and then
# closed one brace short (713 chars, "Expecting ',' delimiter" at the very
# end), the retry closed one short again, the raw JSON went back to the
# customer as prose, and the customer played along until max_steps.
_INNER = json.dumps({"record_id": "rec_0001", "action": "keep",
                     "reason": "mismatch", "confirmed": True})
_WRAPPED_SHORT = json.dumps({"tool_call": {
    "name": "call_wrapped_tool",
    "arguments": {"inner_tool": "file_record_42", "arguments": _INNER}}})[:-1]


def test_a_tool_call_closed_one_brace_short_is_repaired():
    assert not _WRAPPED_SHORT.endswith("}}}")
    assert shim.looks_like_tool_call(_WRAPPED_SHORT)
    calls = shim.parse_tool_reply(_WRAPPED_SHORT)
    assert calls and calls[0]["function"]["name"] == "call_wrapped_tool"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["inner_tool"] == "file_record_42"
    # The inner string survives verbatim — braces inside strings are not
    # what the repair counts.
    assert json.loads(args["arguments"]) == json.loads(_INNER)


def test_brace_repair_counts_only_structural_braces():
    """Braces inside JSON strings (the inner object, an escaped quote, a
    literal brace in prose) must not be balanced against."""
    assert shim._balance_braces('{"a": "x}y"') == '{"a": "x}y"}'
    assert shim._balance_braces('{"a": "\\"{{"') == '{"a": "\\"{{"}'
    assert shim._balance_braces('{"a": {"b": 1}') == '{"a": {"b": 1}}'
    # Already balanced or over-closed: unchanged.
    assert shim._balance_braces('{"a": 1}') == '{"a": 1}'
    assert shim._balance_braces('{"a": 1}}') == '{"a": 1}}'


def test_a_short_close_is_served_as_the_call_without_a_retry():
    """The repair means no second CLI call: one reply, one tool call."""
    cli, calls_made = _cli([_WRAPPED_SHORT])
    out = _post(cli, _request())
    msg = out["choices"][0]["message"]
    assert msg["content"] is None
    assert msg["tool_calls"][0]["function"]["name"] == "call_wrapped_tool"
    assert len(calls_made) == 1
