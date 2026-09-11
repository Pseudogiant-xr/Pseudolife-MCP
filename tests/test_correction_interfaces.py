"""Selected entry identity survives MCP, HTTP, and Console correction calls."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from tests.helpers import invoke_tool
from pseudolife_memory.web.routes import ConsoleRoutes


@pytest.mark.parametrize("method,selector", [
    ("supersede", {"entry_id": 41}),
    ("consolidate", {"entry_ids": [41, 42]}),
])
@pytest.mark.parametrize("transport", ["mcp", "http"])
def test_correction_dispatch_preserves_selected_ids(monkeypatch, method, selector, transport):
    calls = []

    def record(**kwargs):
        calls.append(kwargs)
        return {"new_memory_stored": True, "superseded_ids": [41]}

    svc = SimpleNamespace(supersede=record, consolidate=record)
    payload = {**selector, "new_text": "The revised note"}
    if transport == "mcp":
        from pseudolife_memory import mcp_server
        monkeypatch.setattr(mcp_server, "service", svc)
        result = invoke_tool(f"memory_{method}", payload)
    else:
        result = ConsoleRoutes(svc).dispatch("POST", f"/api/{method}", {}, payload)
    assert result["new_memory_stored"] is True
    assert len(calls) == 1
    assert all(calls[0][key] == value for key, value in selector.items())
    assert calls[0].get("old_text" if method == "supersede" else "replaces") is None


@pytest.mark.parametrize("method,key,bad", [
    ("supersede", "entry_id", True),
    ("supersede", "entry_id", "41"),
    ("consolidate", "entry_ids", [41, True]),
    ("consolidate", "entry_ids", [41.0]),
])
def test_mcp_does_not_coerce_correction_ids(monkeypatch, method, key, bad):
    from pseudolife_memory import mcp_server
    calls = []
    monkeypatch.setattr(mcp_server, "service", SimpleNamespace(**{
        method: lambda **kwargs: calls.append(kwargs) or {"called": True},
    }))
    with pytest.raises(Exception):
        invoke_tool(f"memory_{method}", {key: bad, "new_text": "The revised note"})
    assert not calls


_NODE = shutil.which("node")
_JS = Path(__file__).resolve().parents[1] / "pseudolife_memory/web/static/js"


@pytest.mark.parametrize("tool", ["memory_supersede", "memory_consolidate"])
def test_replacement_text_stays_a_required_tool_argument(tool):
    """A correction without replacement text is a client error, not a no-op call."""
    from pseudolife_memory import mcp_server

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    required = set(tools[tool].input_schema.get("required") or [])
    assert "new_text" in required
    assert not required & {"old_text", "entry_id", "replaces", "entry_ids"}


@pytest.mark.skipif(_NODE is None, reason="Node.js is needed for Console behavior checks")
@pytest.mark.parametrize("method", ["supersede", "consolidate"])
@pytest.mark.parametrize("mode", ["ids", "files", "rejected", "unchanged", "filtered"])
def test_console_correction_preserves_identity_and_reports_rejection(method, mode):
    """Execute the real action body with only UI/network dependencies replaced."""
    filename = "views/stream.js" if method == "supersede" else "consolidation.js"
    function = "doSupersede" if method == "supersede" else "doConsolidate"
    entries = [{"id": 41, "text": "same text"}, {"id": 42, "text": "same text"}]
    if mode == "files":
        entries = [{"id": None, "text": "first note"}, {"id": None, "text": "second note"}]
    retired = 1 if method == "supersede" else len(entries)
    # "filtered": the target was retired and written through, but the meta
    # filter dropped the replacement text. The correction happened.
    response = {
        "ids": {"new_memory_stored": True, "superseded_count": retired},
        "files": {"new_memory_stored": True, "superseded_count": retired},
        "filtered": {"new_memory_stored": False, "superseded_count": retired},
        "rejected": {"new_memory_stored": False, "superseded_count": 0,
                     "reason": "target_superseded",
                     "error": "This entry changed; search again."},
        # Belt for the braces above: nothing retired is a rejection even if
        # some future caller loses the diagnostic.
        "unchanged": {"new_memory_stored": False, "superseded_count": 0},
    }[mode]
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [], notices = [];
let closed = 0;
const noop = () => ({});
const context = vm.createContext({});
const source = fs.readFileSync(input.path, 'utf8') + `\nexport { ${input.function} };`;
const module = new vm.SourceTextModule(source, {context});
const exports = {
  el: noop, mount: noop, clear: noop, fmtAge: noop, fmtTime: noop,
  truncate: noop, loadingBlock: noop, emptyBlock: noop, errorBlock: noop,
  debounce: noop, openDrawer: noop, setDrawerBody: noop, openModal: noop,
  closeModal: () => { closed++; }, toast: (...args) => notices.push(args),
  confirmDialog: noop, badge: noop, reVerifyBadge: noop, searchBox: noop, facetBar: noop,
  api: {post: async (url, payload) => {
    calls.push({url, payload});
    return input.response;
  }, get: async () => ({clusters: []})},
};
(async () => {
  await module.link(() => new vm.SyntheticModule(Object.keys(exports), function() {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, {context}));
  await module.evaluate();
  await module.namespace[input.function](input.function === 'doSupersede'
    ? input.entries[0] : input.entries, 'The revised note', {});
  process.stdout.write(JSON.stringify({calls, notices, closed}));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
'''
    result = subprocess.run(
        [_NODE, "--experimental-vm-modules", "-e", script],
        input=json.dumps({"path": str(_JS / filename), "function": function,
                          "entries": entries, "response": response}),
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    payload = out["calls"][0]["payload"]
    expected = ({"old_text": entries[0]["text"]} if method == "supersede"
                else {"replaces": [entry["text"] for entry in entries]}) if mode == "files" else (
                    {"entry_id": 41} if method == "supersede" else {"entry_ids": [41, 42]})
    assert payload == {**expected, "new_text": "The revised note"}
    if mode in ("rejected", "unchanged"):
        assert out["closed"] == 0
        assert out["notices"][-1][1] == "bad"
        assert ("search again" if mode == "rejected" else "try again") in out["notices"][-1][0]
    elif mode == "filtered":
        assert out["closed"] == 1
        assert out["notices"][-1][1] == "warn"
        assert "not stored" in out["notices"][-1][0]
    else:
        assert out["closed"] == 1
        assert out["notices"][-1][1] == "ok"
