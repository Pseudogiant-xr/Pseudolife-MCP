"""The manual channel probe records explicit receipts without message content."""

import asyncio
import importlib.metadata
import json
import platform

import pytest

from tests.test_channel import channel_wire, handshake, initialized, request


def test_probe_requires_emission_then_exact_id_and_nonce(tmp_path):
    async def drive():
        from ops.coordination_probe import Probe
        out = tmp_path / "probe.jsonl"
        with Probe(out, nonce="synthetic-nonce", delay=0) as probe:
            async with channel_wire(probe.inbox, probe.server) as (incoming, outgoing):
                await handshake(incoming, outgoing)
                await incoming.send(request("tools/call", 2, {"name": "coordination_probe_ack", "arguments": {
                    "message_id": probe.message_id, "nonce": "synthetic-nonce"}}))
                assert (await outgoing.receive()).message.result["isError"] is True
                await incoming.send(initialized())
                event = (await outgoing.receive()).message
                assert event.method == "notifications/claude/channel"
                assert event.params["meta"]["message_id"] == probe.message_id
                for request_id, arguments in [(3, {"message_id": "wrong", "nonce": "synthetic-nonce"}),
                                              (4, {"message_id": probe.message_id, "nonce": "wrong"}),
                                              (6, {"message_id": probe.message_id, "nonce": "wrong-\u2603"})]:
                    await incoming.send(request("tools/call", request_id, {
                        "name": "coordination_probe_ack", "arguments": arguments}))
                    assert (await outgoing.receive()).message.result["isError"] is True
                assert not probe.acknowledged.is_set()
                await incoming.send(request("tools/call", 5, {"name": "coordination_probe_ack", "arguments": {
                    "message_id": probe.message_id, "nonce": "synthetic-nonce"}}))
                assert (await outgoing.receive()).message.result.get("isError", False) is False
                assert probe.acknowledged.is_set()
        trace = out.read_text()
        assert "synthetic-nonce" not in trace
        assert "Acknowledge only this synthetic event" not in trace
        records = [json.loads(line) for line in trace.splitlines()]
        assert [r["event"] for r in records if r["event"] != "ack_rejected"] == [
            "started", "host", "sdk_initialized", "event_attempted", "explicit_ack", "closed"]
        assert all(set(r) <= {"event", "elapsed_seconds", "message_id", "protocol",
                              "python_version", "mcp_sdk_version", "host_name", "host_version"}
                   for r in records)
        started = records[0]
        assert started["python_version"] == platform.python_version()
        assert started["mcp_sdk_version"] == importlib.metadata.version("mcp")
        host = next(r for r in records if r["event"] == "host")
        assert host["host_name"] == "fixture" and host["host_version"] == "1"

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_probe_does_not_overwrite_trace(tmp_path):
    from ops.coordination_probe import Probe, ProbeError
    out = tmp_path / "probe.jsonl"
    out.write_text("original")
    with pytest.raises(ProbeError):
        Probe(out, nonce="synthetic", delay=0)
    assert out.read_text() == "original"


def test_probe_ack_duplicate_is_one_receipt(tmp_path):
    async def drive():
        from ops.coordination_probe import Probe
        with Probe(tmp_path / "probe.jsonl", nonce="synthetic", delay=0) as probe:
            async with channel_wire(probe.inbox, probe.server) as (incoming, outgoing):
                await handshake(incoming, outgoing)
                await incoming.send(initialized())
                await outgoing.receive()
                for request_id in (2, 3):
                    await incoming.send(request("tools/call", request_id, {"name": "coordination_probe_ack", "arguments": {
                        "message_id": probe.message_id, "nonce": "synthetic"}}))
                    await outgoing.receive()
        records = [json.loads(line) for line in (tmp_path / "probe.jsonl").read_text().splitlines()]
        assert len([r for r in records if r["event"] == "explicit_ack"]) == 1

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_probe_timeout_records_unverified_receipt_and_closes(tmp_path, monkeypatch, capsys):
    async def drive():
        import ops.coordination_probe as module

        async def silent_server(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(module, "serve_channel", silent_server)
        with module.Probe(tmp_path / "probe.jsonl", nonce="synthetic", delay=0) as probe:
            assert await module.run_probe(probe, None, None, timeout=0.01) == 1
        trace = (tmp_path / "probe.jsonl").read_text()
        assert '"event": "timeout"' in trace
        assert "explicit_ack" not in trace
        assert "unverified" in capsys.readouterr().err

    asyncio.run(asyncio.wait_for(drive(), 4))
