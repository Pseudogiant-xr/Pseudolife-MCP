"""Guard: the daemon container's memory sizing in ops/docker-compose.yml.

2026-09-23: the 4g cap (set 2026-08-20 against a 2.8 GB steady RSS measured
on a smaller bank) had no allowance for bursts: the daemon held ~3.1-3.3
GiB anon at rest, and an ordinary request burst grew it to the limit and
OOM-killed it. The cap moved to 6g; memory+swap stays pinned to the same
value so hitting it is a clean restart, never host swap. Right after a
concurrent burst, RSS was ~430 MB higher with glibc's default per-thread
malloc arenas than with two, so the daemon runs with two.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "ops" / "docker-compose.yml"
ENV_EXAMPLE = REPO / "ops" / ".env.example"
CONFIG_DOC = REPO / "docs" / "guide" / "configuration.md"


def _daemon() -> dict:
    stack = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return stack["services"]["pseudolife-daemon"]


def test_the_daemon_cap_defaults_to_6g_with_no_swap() -> None:
    daemon = _daemon()
    assert daemon["mem_limit"] == "${PSEUDOLIFE_DAEMON_MEM_LIMIT:-6g}"
    assert daemon["memswap_limit"] == daemon["mem_limit"], (
        "memswap_limit is the memory+swap TOTAL; equal to mem_limit means "
        "no swap, so the cap is a clean OOM restart, not host swap thrash")


def test_the_daemon_runs_with_two_malloc_arenas() -> None:
    assert _daemon()["environment"]["MALLOC_ARENA_MAX"] == "2"


def test_the_env_template_and_config_doc_state_the_same_default() -> None:
    template = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"^#PSEUDOLIFE_DAEMON_MEM_LIMIT=6g$", template, re.MULTILINE)
    row = next(line for line in CONFIG_DOC.read_text(encoding="utf-8").splitlines()
               if line.startswith("| `PSEUDOLIFE_DAEMON_MEM_LIMIT`"))
    assert "| `6g` |" in row
