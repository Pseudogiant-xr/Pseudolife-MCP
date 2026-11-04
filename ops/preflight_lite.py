"""Read-only lite-bank detection for the Docker installer (stdlib only)."""
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import sys

_PLATFORM = sys.platform


def data_dirs() -> list[Path]:
    """Match embedded_pg.default_lite_data_dir without importing the package.

    Preflight runs before the shim/package is installed. Also retain the
    historical home location and inspect an explicitly selected data dir.
    """
    home = Path.home()
    if _PLATFORM == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
    elif _PLATFORM == "darwin":
        base = home / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    candidates = [base / "pseudolife-mcp", home / ".pseudolife-mcp"]
    if custom := os.environ.get("PSEUDOLIFE_MCP_DATA_DIR"):
        candidates.insert(0, Path(custom))
    return list(dict.fromkeys(candidates))


def refusal(containers: list[dict], *, configured: bool, recorded_volumes: bool,
            port_busy: Callable[[int], bool]) -> str | None:
    banks = [path for path in data_dirs() if (path / "embedded_pg/PG_VERSION").is_file()]
    if not banks:
        return None
    own_daemons = [c for c in containers
                   if c.get("Name", "").lstrip("/") == "pseudolife-mcp-daemon"
                   and (c.get("Config", {}).get("Labels") or {}).get(
                       "com.docker.compose.project") == "pseudolife-mcp"]
    own_listener = any(
        c.get("State", {}).get("Running")
        and any(binding.get("HostPort") == "8765"
                and binding.get("HostIp", "") in ("", "127.0.0.1", "0.0.0.0")
                for bindings in (c.get("NetworkSettings", {}).get("Ports") or {}).values()
                for binding in (bindings or []))
        for c in own_daemons)
    # A completed Docker migration can leave a stopped lite cluster behind.
    # It must not prevent an ordinary rerun, but a non-Docker listener still
    # needs attention before Docker can reclaim its port.
    if configured and (own_daemons or recorded_volumes) and (own_listener or not port_busy(8765)):
        return None
    bank = banks[0]
    return (
        f'An initialized lite bank exists at {bank}. Docker installation would not migrate it. '
        f'Migrate the lite bank first: run pseudolife-mcp backup --data-dir "{bank}", '
        'then restore its database dump and state archive into Docker using the lite-to-Docker '
        'backup/restore path in docs/guide/configuration.md#backups '
        '(https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/master/docs/guide/configuration.md#backups). '
        'Keep the lite bank and its backups until the Docker bank is verified. Nothing changed.'
    )
