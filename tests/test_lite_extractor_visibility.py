"""The lite tier ships no extractor — say so where a user can see it.

``pip install "pseudolife-mcp[lite]"`` gives a full Postgres bank but no
dream extractor, so the cortex (the flagship layer) auto-populates with
nothing: only ``memory_fact_set`` writes canonical facts. The daemon has
always logged that at startup, but on the lite path the shim spawns the
daemon with ``stderr=DEVNULL`` (``shim.spawn_daemon``) — nobody ever reads
that line. A silently fact-less cortex reads as "the product doesn't
work".

These pin the two surfaces a lite user actually meets:

* ``/health`` reports ``extractor`` (``none`` / ``configured`` /
  ``disabled``), so "is my cortex live?" is one curl — the same probe the
  README's Troubleshooting and the bug-report issue form already ask for;
* the stdio shim prints the consequence and the fix once per session when
  the daemon it attached to reports ``extractor: "none"``.

Both are additive: a caller that never looks at the field is unaffected.
"""

from __future__ import annotations

import pytest


# ── /health: extractor visibility ─────────────────────────────────────────


class _Svc:
    """Minimal MemoryService stand-in for _build_health_payload."""

    _db_url = "postgresql://fake"
    _persist_errors = 0
    _init_refusal = None
    _storage = None

    def __init__(self, config):
        self.config = config


def _config(*, enabled=True, base_url=None, model=None,
            fallback_base_url=None, fallback_model=None):
    from types import SimpleNamespace

    dream = SimpleNamespace(
        enabled=enabled,
        extractor_source="config",
        extractor_base_url=base_url,
        extractor_model=model,
        fallback_base_url=fallback_base_url,
        fallback_model=fallback_model,
        extractor_mode="auto",
        extractor_max_tokens=2048,
        extractor_timeout_seconds=240.0,
        extractor_model_override=None,
    )
    return SimpleNamespace(memory=SimpleNamespace(dream=dream))


def test_health_reports_no_extractor_on_the_lite_default() -> None:
    """The lite tier's shape: dreaming enabled, no endpoint configured.
    ``extractor: "none"`` is the one machine-readable statement that the
    cortex will not fill itself."""
    from pseudolife_memory.daemon import _build_health_payload

    payload = _build_health_payload(_Svc(_config()), token_present=False)
    assert payload["extractor"] == "none"
    # Not an outage: the bank serves normally, so status stays ok (a 503
    # here would brick the Docker healthcheck and ops/update.ps1).
    assert payload["status"] == "ok"


def test_health_reports_a_configured_extractor() -> None:
    from pseudolife_memory.daemon import _build_health_payload

    cfg = _config(base_url="http://127.0.0.1:8080/v1", model="gemma-4-e4b")
    payload = _build_health_payload(_Svc(cfg), token_present=False)
    assert payload["extractor"] == "configured"


def test_health_counts_a_fallback_only_endpoint_as_configured() -> None:
    """``build_extractor_with_fallback`` will use a fallback-only setup, so
    reporting "none" for it would be a lie."""
    from pseudolife_memory.daemon import _build_health_payload

    cfg = _config(fallback_base_url="http://127.0.0.1:8123/v1",
                  fallback_model="claude-opus-5")
    payload = _build_health_payload(_Svc(cfg), token_present=False)
    assert payload["extractor"] == "configured"


def test_health_reports_dreaming_turned_off_distinctly() -> None:
    """A deliberately dream-disabled bank is not the same complaint as a
    missing extractor — don't send that operator hunting for an endpoint."""
    from pseudolife_memory.daemon import _build_health_payload

    payload = _build_health_payload(_Svc(_config(enabled=False)),
                                    token_present=False)
    assert payload["extractor"] == "disabled"


def test_health_omits_extractor_when_config_is_unreachable() -> None:
    """Defensive: the payload builder is called with bare stubs elsewhere in
    the suite and must not start requiring a full config object."""
    from pseudolife_memory.daemon import _build_health_payload

    class _Bare:
        _db_url = None
        _persist_errors = 0
        _init_refusal = None
        _storage = None

    payload = _build_health_payload(_Bare(), token_present=False)
    assert "extractor" not in payload


# ── the shim: one honest line where the user can read it ──────────────────


def test_shim_names_what_is_inert_and_the_fix(capsys, monkeypatch) -> None:
    """``ensure_daemon`` returns the health payload it probed; when that
    payload says the cortex cannot fill itself, the shim says so once —
    what still works, what does not, and the env vars that fix it."""
    from pseudolife_memory import shim

    monkeypatch.setattr(
        shim, "probe_health",
        lambda url, timeout=0.25: {"status": "ok", "extractor": "none"})
    health = shim.ensure_daemon("http://127.0.0.1:8765")
    assert health["extractor"] == "none"

    err = capsys.readouterr().err
    assert "PSEUDOLIFE_DREAM_BASE_URL" in err
    assert "PSEUDOLIFE_DREAM_MODEL" in err
    # States the consequence, not just the config gap.
    assert "memory_fact_set" in err


@pytest.mark.parametrize("extractor", ["configured", "disabled", None])
def test_shim_stays_quiet_when_there_is_nothing_to_report(
        capsys, monkeypatch, extractor) -> None:
    """No notice for a healthy extractor, a deliberately dream-disabled
    bank, or an older daemon whose /health predates the field."""
    from pseudolife_memory import shim

    payload = {"status": "ok"}
    if extractor is not None:
        payload["extractor"] = extractor
    monkeypatch.setattr(shim, "probe_health",
                        lambda url, timeout=0.25: dict(payload))
    shim.ensure_daemon("http://127.0.0.1:8765")
    assert "PSEUDOLIFE_DREAM_BASE_URL" not in capsys.readouterr().err
