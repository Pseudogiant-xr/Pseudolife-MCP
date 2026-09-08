"""Pseudolife memory plugin for the taubench harness.

The plugin is a separate distribution installed into the harness venv. It never
imports ``pseudolife_memory``: it talks to the running memory daemon over MCP
(streamable HTTP) plus two REST endpoints for episode lifecycle, exactly the way
any other client would.

Only ``agent`` and ``memory_env`` need the harness itself; every other module
(``context``, ``session``, ``client``, ``telemetry``, ``prompts``,
``reflection``, ``reflection_diff``, ``dream``) imports standalone so it can be
tested outside the harness venv.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
