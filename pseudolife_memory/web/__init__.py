"""Cortex Console — the operator web frontend for PseudoLife-MCP.

A read-mostly instrument panel served by the daemon itself: a thin REST API
(:mod:`pseudolife_memory.web.routes`) over the existing ``MemoryService``, plus
a no-build vanilla SPA (``static/``). Wired into the daemon's ASGI app by
:func:`pseudolife_memory.web.api.build_console_app` so it lives behind the same
bearer-token gate as ``/mcp`` while ``/health`` and the static shell stay open.

No torch import here — the package stays light so it can be imported anywhere
(including the fixture :mod:`pseudolife_memory.web.devserver` used for UI QA).
"""

from __future__ import annotations

__all__ = ["build_console_app"]


def build_console_app(*args, **kwargs):
    """Lazy re-export so ``import pseudolife_memory.web`` stays cheap."""
    from pseudolife_memory.web.api import build_console_app as _b

    return _b(*args, **kwargs)
