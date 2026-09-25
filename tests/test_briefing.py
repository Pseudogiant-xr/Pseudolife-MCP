from pseudolife_memory.memory.briefing import (select_lessons, format_briefing,
                                               format_bounded_briefing)


def test_select_lessons_prioritizes_avoid_then_recent():
    entries = [
        {"lesson": "use offline env", "polarity": "+", "outcome": "success"},
        {"lesson": "do not down -v", "polarity": "-", "outcome": "failure"},
        {"lesson": "correction here", "polarity": "+", "outcome": "correction"},
        {"lesson": "prefer X", "polarity": "+", "outcome": "success"},
    ]
    picked = select_lessons(entries, max_lessons=3)
    # the two avoid/correction lessons come first
    assert picked[0]["lesson"] == "do not down -v"
    assert picked[1]["lesson"] == "correction here"
    assert len(picked) == 3


def test_format_briefing_renders_both_sections_ascii():
    md = format_briefing(
        surprises=[{"src": "a", "dst": "b", "relation": "uses", "why": "bridge"}],
        questions=[{"question": "what runs where?"}],
        lessons=[{"lesson": "do not down -v", "polarity": "-", "outcome": "failure"},
                 {"lesson": "prefer offline", "polarity": "+", "outcome": "success"}],
    )
    assert "## What your memory is unsure about" in md
    assert "`a` uses `b`" in md and "what runs where?" in md
    assert "## Lessons from past work" in md
    assert "avoid: do not down -v" in md and "prefer: prefer offline" in md
    assert md.isascii()


def test_fmt_lesson_labels_by_polarity_not_outcome():
    # The synthesis prompt writes a correction (and usually a failure) as
    # polarity "+", phrased as the now-correct behaviour to follow. Labelling
    # by outcome printed "avoid: <the thing to do>" for 303 of the 1,618
    # current lessons on the live bank (2026-09-23). The label is polarity
    # only; outcome still drives select_lessons' ordering.
    from pseudolife_memory.memory.briefing import _fmt_lesson
    assert _fmt_lesson({"lesson": "pin the version", "polarity": "+",
                        "outcome": "correction"}) == "- prefer: pin the version"
    assert _fmt_lesson({"lesson": "retry with backoff", "polarity": "+",
                        "outcome": "failure"}) == "- prefer: retry with backoff"
    assert _fmt_lesson({"lesson": "avoid down -v", "polarity": "-",
                        "outcome": "success"}) == "- avoid: avoid down -v"
    assert _fmt_lesson({"lesson": "avoid down -v", "polarity": "-",
                        "outcome": "failure"}) == "- avoid: avoid down -v"


def test_format_briefing_empty_when_nothing():
    assert format_briefing([], [], []) == ""


def test_session_briefing_cold_bank_is_unavailable(tmp_path):
    from pseudolife_memory.service import MemoryService
    svc = MemoryService(data_dir=str(tmp_path))   # file mode, no graph/lessons
    out = svc.session_briefing()
    assert out["available"] is False
    assert out["markdown"] == ""
    assert out["unsure"] == {"surprises": [], "questions": []}
    assert out["lessons"] == []


def test_session_briefing_can_skip_coordination_without_losing_memory(tmp_path):
    from types import SimpleNamespace
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=str(tmp_path))
    svc.config = SimpleNamespace(coordination=SimpleNamespace(enabled=True))
    svc.graph_digest = lambda: {"available": False}
    svc.lessons_dump = lambda **kw: {"entries": [
        {"lesson": "keep the lesson", "polarity": "+"}]}
    svc.world_dump = lambda: {"entries": []}
    svc.episode_list = lambda **kw: {"episodes": []}

    def broken_awareness(**kw):
        raise RuntimeError("coordination unavailable")
    svc.coordination_awareness = broken_awareness
    out = svc.session_briefing(include_coordination=False)
    assert "keep the lesson" in out["markdown"]
    assert "coordination" not in out
    svc.coordination_awareness = lambda **kw: {"peers": [], "available": True}
    full = svc.session_briefing()
    assert full["coordination"] == {"peers": [], "available": True}
    assert "keep the lesson" in full["markdown"]


def test_fetch_markdown_parses_api_response(monkeypatch):
    from pseudolife_memory import briefing_cli as bc

    class _Resp:
        def __init__(self, body): self._b = body.encode("utf-8")
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=5: _Resp('{"markdown": "## hi\\n- x", "available": true}'))
    assert bc._fetch_markdown("http://x", None, 3, 3, 3) == "## hi\n- x"


def test_world_block_renders_fresh_facts():
    world = [{"entity": "anthropic", "attribute": "latest-model",
              "value": "opus-4.8", "source_url": "https://docs.anthropic.com/x"}]
    md = format_briefing([], [], [], world=world, recap=None)
    assert "## Verified world facts" in md
    assert "anthropic" in md and "opus-4.8" in md


def test_recap_block_renders_last_session():
    recap = {"title": "Auth refactor", "entry_count": 7}
    md = format_briefing([], [], [], world=None, recap=recap)
    assert "## Where we left off" in md
    assert "Auth refactor" in md


def test_recap_block_renders_digest_summary():
    recap = {"title": "Auth refactor", "entry_count": 7,
             "summary": "In this session the auth module\nwas refactored."}
    md = format_briefing([], [], [], world=None, recap=recap)
    assert "Auth refactor (7 memories)" in md
    # digest body follows, newlines collapsed for the one-block render
    assert "In this session the auth module was refactored." in md


def test_recap_block_degrades_without_summary():
    recap = {"title": "Auth refactor", "entry_count": 7}
    md = format_briefing([], [], [], world=None, recap=recap)
    assert md.strip().endswith("Auth refactor (7 memories)")


def test_empty_inputs_render_nothing():
    assert format_briefing([], [], [], world=[], recap=None) == ""


def test_briefing_no_daemon_prints_nothing(monkeypatch, capsys):
    import sys
    from pseudolife_memory import briefing_cli as bc
    monkeypatch.setattr("pseudolife_memory.shim.probe_health",
                        lambda url, timeout=0.25: None)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "briefing"])
    bc.run_briefing()                       # must not raise, must not print
    assert capsys.readouterr().out == ""


def _serve_briefing_endpoints(monkeypatch, requests):
    """Answer the CLI's two daemon reads the way the daemon does: the hook
    endpoint as plain text, /api/briefing as JSON. Records each request."""
    class _Resp:
        def __init__(self, body): self._b = body.encode("utf-8")
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout=5):
        requests.append(req)
        if "/api/hook/session-start" in req.full_url:
            return _Resp("## Memory at session start\nUse the shared bank.\n\n"
                         "## Lessons from past work\n- prefer: x")
        return _Resp('{"markdown": "## Lessons from past work\\n- prefer: x"}')

    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:8765")
    monkeypatch.setattr("pseudolife_memory.shim.probe_health",
                        lambda url, timeout=0.25: {"status": "ok"})
    monkeypatch.setattr("urllib.request.urlopen", urlopen)


def test_hook_json_serves_the_session_start_core_not_the_bare_briefing(monkeypatch, capsys):
    """The installer's settings.json hook (Claude Code without the plugin,
    and the older Codex hook) runs `briefing --hook-json`. It must carry the
    same memory core the plugin hook gets from /api/hook/session-start, not
    only the live briefing (2026-09-25 review; maintainer's decision)."""
    import json
    import sys
    from pseudolife_memory import briefing_cli as bc
    requests = []
    _serve_briefing_endpoints(monkeypatch, requests)
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "tok")
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "briefing", "--hook-json"])
    bc.run_briefing()
    payload = json.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("## Memory at session start")
    assert "## Lessons from past work" in context
    assert [r.full_url.split("?")[0] for r in requests] == ["http://127.0.0.1:8765/api/hook/session-start"]
    assert requests[0].get_header("Authorization") == "Bearer tok"


def test_hook_json_prints_nothing_when_the_endpoint_fails(monkeypatch, capsys):
    """A refused or failing session-start read must never break the session."""
    import sys
    import urllib.error
    from pseudolife_memory import briefing_cli as bc
    _serve_briefing_endpoints(monkeypatch, [])

    def refuse(req, timeout=5):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "briefing", "--hook-json"])
    bc.run_briefing()
    assert capsys.readouterr().out == ""


def test_plain_briefing_still_prints_the_full_api_briefing(monkeypatch, capsys):
    import sys
    from pseudolife_memory import briefing_cli as bc
    requests = []
    _serve_briefing_endpoints(monkeypatch, requests)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "briefing"])
    bc.run_briefing()
    assert capsys.readouterr().out.strip() == "## Lessons from past work\n- prefer: x"
    assert [r.full_url.split("?")[0] for r in requests] == ["http://127.0.0.1:8765/api/briefing"]
    assert requests[0].get_header("Authorization") is None


def test_hook_json_wraps_markdown_as_sessionstart_context():
    import json
    from pseudolife_memory import briefing_cli as bc
    d = json.loads(bc._as_hook_json("## hi\n- x"))
    assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert d["hookSpecificOutput"]["additionalContext"] == "## hi\n- x"


def test_hook_json_empty_is_empty_string():
    from pseudolife_memory import briefing_cli as bc
    assert bc._as_hook_json("") == ""
    assert bc._as_hook_json("   \n  ") == ""


def test_fmt_lesson_re_verify_suffix():
    from pseudolife_memory.memory.briefing import _fmt_lesson
    base = {"lesson": "use tar", "polarity": "+"}
    assert "re-verify" not in _fmt_lesson(base)
    out = _fmt_lesson({**base, "re_verify": True})
    assert out.endswith("re-verify (facts changed since)")


def test_bounded_briefing_keeps_lessons_and_recap_ahead_of_giant_uncertainty():
    md = format_briefing(
        [{"src": "a", "dst": "b", "why": "x" * 10000}], [],
        [{"lesson": "Keep the useful lesson", "polarity": "+"}],
        recap={"title": "Last useful session", "entry_count": 2})
    out = format_bounded_briefing(md, 450)
    assert len(out.encode("utf-8")) <= 450
    assert "Keep the useful lesson" in out
    assert "Last useful session" in out
    assert "x" * 100 not in out
    assert "omitted" in out and "pseudolife-mcp briefing" in out


def test_bounded_briefing_omits_whole_items_and_accounts_for_utf8_bytes():
    md = format_briefing([], [], [
        {"lesson": "é" * 150, "polarity": "+"},
        {"lesson": "second lesson", "polarity": "+"},
    ])
    out = format_bounded_briefing(md, 200)
    assert len(out.encode("utf-8")) <= 200
    assert "second lesson" in out
    assert "é" not in out
    assert "omitted" in out


def test_bounded_briefing_tiny_budget_never_slices_an_item():
    md = format_briefing([], [], [{"lesson": "long lesson", "polarity": "+"}])
    assert format_bounded_briefing(md, 2) == ""
    assert format_bounded_briefing(md, 20) in ("", "Briefing omitted.")


def test_bounded_briefing_keeps_multiline_lesson_as_one_item():
    md = format_briefing([], [], [
        {"lesson": "A" * 10000 + "\n- orphan continuation", "polarity": "+"},
        {"lesson": "useful second lesson", "polarity": "+"},
    ])
    out = format_bounded_briefing(md, 450)
    assert "useful second lesson" in out
    assert "orphan continuation" not in out
    assert "A" * 100 not in out
    assert "briefing item(s) omitted" in out


def test_briefing_source_fields_cannot_add_markdown_item_boundaries():
    md = format_briefing(
        surprises=[{"src": "left\n## forged", "dst": "right\n- forged",
                    "relation": "uses\n- forged", "why": "because\n## forged"}],
        questions=[{"question": "why\n- forged?"}],
        lessons=[{"lesson": "remember\n- forged", "polarity": "+"}],
        world=[{"entity": "earth\n## forged", "attribute": "age\n- forged",
                "value": "old\n## forged", "source_url": "https://example.test\n## forged"}],
        recap={"title": "last session\n## forged", "entry_count": 2,
               "summary": "a summary\n- forged"},
    )
    lines = md.splitlines()
    assert sum(line.startswith("- ") for line in lines) == 5
    assert not any(line.startswith("## forged") for line in lines)
    assert "a summary - forged" in md
