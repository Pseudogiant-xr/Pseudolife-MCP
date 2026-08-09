"""Production extractor cache_prompt pin (deferred decision from the
2026-08-09 warm-cache root cause, resolved by measurement).

warm-cache-probe-0809 proved llama-server's default prompt cache changes
extractor OUTPUT once populated; sidecar-cache-latency-0809 measured the
cost of pinning it off on the live sidecar at +7.25s/call (3.4s -> 10.65s)
— noise for a 600s background sweep with a 480s timeout budget. The daemon
therefore pins ``cache_prompt: false`` by default on every extractor it
builds; ``memory.dream.extractor_cache_prompt = None`` restores the server
default for deployments that prefer the latency.
"""
from __future__ import annotations

from pseudolife_memory.memory.dream import build_extractor
from pseudolife_memory.utils.config import DreamConfig


def _cfg(**over) -> DreamConfig:
    cfg = DreamConfig(extractor_base_url="http://127.0.0.1:9/v1",
                      extractor_model="extractor",
                      extractor_source="config")
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_daemon_extractor_pins_cache_off_by_default():
    ex = build_extractor(_cfg())
    assert ex.extra_body == {"cache_prompt": False}


def test_none_restores_the_server_default():
    ex = build_extractor(_cfg(extractor_cache_prompt=None))
    assert ex.extra_body == {}


def test_true_forces_the_cache_on_explicitly():
    ex = build_extractor(_cfg(extractor_cache_prompt=True))
    assert ex.extra_body == {"cache_prompt": True}
