"""The extractor sidecar must not hold its model resident while idle.

Measured 2026-08-04: the fallback sidecar kept ~6.9 GB of model weights
resident around the clock — the single biggest steady-state resident on the
box — while serving only the *fallback* dream path (the daemon's primary is
a host-side shim; the sidecar answers only when the primary probe fails).
llama-server's ``--sleep-idle-seconds`` (llama.cpp PR #18228, in the shipped
image since the 2026-07-06 build) fixes this without changing the binary,
model, or prompts: after the idle window the server unloads the model and
frees its memory, ``GET /health`` keeps answering 200 *without* waking it
(so the compose healthcheck neither breaks nor keeps it awake), and the
next completion request blocks until the model reloads — which is why an
unattended dream sweep that falls back mid-run just waits inside
``PSEUDOLIFE_DREAM_TIMEOUT_SECONDS`` instead of erroring.

The flag has no ``LLAMA_ARG_*`` env equivalent in the shipped build, so
compose carries a ``command:`` override — which REPLACES the image CMD
entirely. That duplication is the drift risk these guards pin: an arg
added to one list but not the other (say, a future ctx-size bump in the
Dockerfile) would silently not apply to the stack, exactly the class of
gap that made ``--parallel 1`` explicit in the first place.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_COMPOSE = _REPO / "ops" / "docker-compose.yml"
_DOCKERFILE = _REPO / "ops" / "Dockerfile.extractor"

_KNOB = "PSEUDOLIFE_EXTRACTOR_SLEEP_IDLE_SECONDS"


def _extractor_service_block() -> str:
    """The ``pseudolife-extractor:`` service body (up to the next top-level
    or sibling key at 2-space indent)."""
    text = _COMPOSE.read_text(encoding="utf-8")
    match = re.search(
        r"^  pseudolife-extractor:\n(.*?)(?=^\S|^  \S)", text, re.M | re.S
    )
    assert match, "no pseudolife-extractor service in ops/docker-compose.yml"
    return match.group(1)


def _compose_command_args() -> list[str]:
    """The extractor's ``command:`` list (block style, one ``- "arg"`` per
    line)."""
    block = _extractor_service_block()
    match = re.search(
        r"^    command:\n((?:      - .+\n)+)", block, re.M
    )
    assert match, (
        "the pseudolife-extractor service has no block-style command: list — "
        "without it the image CMD runs, which does not sleep when idle"
    )
    return [
        line.strip()[2:].strip().strip('"')
        for line in match.group(1).splitlines()
    ]


def _dockerfile_cmd_args() -> list[str]:
    """The Dockerfile ``CMD [...]`` exec-form list (backslash-continued)."""
    text = _DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"^CMD (\[.*?\])\s*$", text.replace("\\\n", ""), re.M | re.S)
    assert match, "no exec-form CMD in ops/Dockerfile.extractor"
    return json.loads(match.group(1))


def test_compose_puts_the_sidecar_to_sleep_when_idle() -> None:
    args = _compose_command_args()
    assert "--sleep-idle-seconds" in args, (
        "the extractor's compose command: no longer passes "
        "--sleep-idle-seconds — the sidecar holds ~7 GB of model weights "
        "resident around the clock to serve a rare fallback path."
    )
    value = args[args.index("--sleep-idle-seconds") + 1]
    assert value == "${%s:-300}" % _KNOB, (
        f"--sleep-idle-seconds is {value!r} — expected the env-substituted "
        f"knob '${{{_KNOB}:-300}}' so operators can retune (or disable with "
        f"-1) via ops/.env without editing the compose file."
    )


def test_compose_command_matches_the_dockerfile_cmd() -> None:
    """compose ``command:`` REPLACES the image CMD, so the two arg lists are
    duplicated by construction. They must say the same thing: normalize the
    compose env substitution to its default and compare exactly (order
    included — these are positional flag/value pairs)."""
    normalized = [
        re.sub(r"^\$\{[A-Z0-9_]+:-(.*)\}$", r"\1", arg)
        for arg in _compose_command_args()
    ]
    assert normalized == _dockerfile_cmd_args(), (
        "ops/docker-compose.yml command: and ops/Dockerfile.extractor CMD "
        "have drifted apart. The compose list is what the stack actually "
        "runs; the Dockerfile list is what a bare `docker run` gets. An arg "
        f"changed in one must change in both.\ncompose:    {normalized}\n"
        f"dockerfile: {_dockerfile_cmd_args()}"
    )
