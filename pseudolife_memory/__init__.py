"""Pseudolife-MCP — persistent long-term memory wrapped as an MCP server.

Top-level re-exports for convenience. The MCP server itself lives in
:mod:`pseudolife_memory.mcp_server`; the high-level wrapper is in
:mod:`pseudolife_memory.service`.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read the installed distribution's metadata rather than carrying a
    # second hand-maintained literal (issue #180: the literal drifted eight
    # releases behind pyproject.toml with nothing to catch it). Every build
    # backend stamps dist-info from pyproject.toml at build time, so this is
    # correct for anything actually shipped; only a checkout with no
    # installed metadata at all falls through to the sentinel below.
    __version__ = version("pseudolife-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
