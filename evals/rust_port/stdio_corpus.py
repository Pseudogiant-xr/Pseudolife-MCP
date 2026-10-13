"""Real disposable-bank stdio cases, driven through the public CLI."""
from pathlib import Path
import sys

from .stdio import capture
from .stdio_capture import initialize, modern_meta

ERAS = ("2025-11-25", "2026-07-28")


def exercise(wire, era):
    initialize(wire, era)
    if era == ERAS[1]:
        wire.send({"jsonrpc": "2.0", "id": "listener", "method": "subscriptions/listen",
                   "params": {"_meta": modern_meta(), "notifications": {"toolsListChanged": True}}})
        wire.until(lambda frame: frame.get("method") == "notifications/subscriptions/acknowledged")
    def request(identifier, method, params):
        if era == ERAS[1]:
            params = {**params, "_meta": modern_meta()}
        wire.send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        return wire.response(identifier)

    listed = request("list", "tools/list", {})
    tools = listed["result"]["tools"]
    if not tools or "memory_world_search" in {item["name"] for item in tools}:
        raise RuntimeError("real daemon must start at minimal toolset")
    request("cursor", "tools/list", {"cursor": "synthetic-cursor"})
    stored = request("store", "tools/call", {"name": "memory_store", "arguments": {
        "text": "Apricot calibration uses amber settings.", "source": "synthetic-stdio", "tags": '["decision"]'}})
    if stored["result"].get("isError"):
        raise RuntimeError("stringified list param failed")
    request("search", "tools/call", {"name": "memory_search", "arguments": {"query": "apricot calibration", "limit": 1}})
    for identifier, tool, arguments in (("unknown", "synthetic_unknown_tool", {}),
                                        ("invalid", "memory_recent", {"n": "many"})):
        result = request(identifier, "tools/call", {"name": tool, "arguments": arguments})
        if not result["result"].get("isError"):
            raise RuntimeError("real daemon error envelope missing")
    request("tier", "tools/call", {"name": "memory_toolset", "arguments": {"action": "expand"}})
    # The response may arrive after the notification; retained frames establish
    # that order. A later list verifies the durable per-session tier change.
    expanded = request("expanded", "tools/list", {})
    if "memory_world_search" not in {item["name"] for item in expanded["result"]["tools"]}:
        raise RuntimeError("toolset expansion failed")
    if not any(frame.get("method") == "notifications/tools/list_changed"
               for frame in [__import__("json").loads(raw) for raw in wire.frames]):
        raise RuntimeError("list_changed notification missing")


def observe(root, home, url, token, era, command=None, *, candidate=False):
    result = capture(command or [sys.executable, "-m", "pseudolife_memory.cli"],
        cwd=root, home=Path(home), url=url, token=token, exercise=lambda wire: exercise(wire, era),
        boundary_errors=candidate)
    result["era"] = era
    result["case"] = "real-bank"
    return result
