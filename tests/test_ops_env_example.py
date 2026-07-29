"""Guard: ``ops/.env.example`` documents every env override compose reads.

The template's own stated contract is that it covers every environment
override the compose stack reads. Nothing enforced it, and the contract has
now been false twice: ``PSEUDOLIFE_WRITER_ID`` (which ``ops/install.ps1``
*writes into* ``ops/.env``, and which ``PSEUDOLIFE_MCP_TIER_MAP`` matches
on) and ``PSEUDOLIFE_MCP_TOOLSET`` (whose compose default decides how many
tools a session can even see) were both live for releases while absent from
the template — the second one while a neighbouring comment *named the
variable*, a dangling reference nobody could act on.

This is the cheap half of the docs-currency problem: whether a documented
default is still *correct* needs a human reading code, but whether a key is
documented *at all* is pure parsing. An undocumented key contradicts
nothing, so no currency pass and no reader ever surfaces it — the same
absence-not-contradiction failure that ``test_eval_evidence`` exists for on
the benchmark side.

Deliberately one-directional: a key may appear in the template without
appearing in compose (the daemon reads some settings compose never
forwards, and documenting those is a feature, not drift).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "ops" / "docker-compose.yml"
ENV_EXAMPLE = REPO / "ops" / ".env.example"

# ``${VAR}`` / ``${VAR:-default}`` — compose's own interpolation syntax.
_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")

# Substitutions that are supplied by the environment rather than by the
# operator, so the template has no business documenting them.
_NOT_OPERATOR_SETTABLE: frozenset[str] = frozenset()


def _compose_vars() -> set[str]:
    return {
        m.group(1)
        for m in _VAR.finditer(COMPOSE.read_text(encoding="utf-8"))
    } - _NOT_OPERATOR_SETTABLE


def _documented_keys() -> set[str]:
    """Keys in ASSIGNMENT position (``KEY=`` or the commented-out
    ``#KEY=default`` this template uses), not merely named in prose.

    The distinction is the whole point: ``PSEUDOLIFE_MCP_TOOLSET`` went
    undocumented while a neighbouring comment *mentioned* it, so a plain
    substring check would have called that file compliant.
    """
    return {
        m.group(1)
        for m in re.finditer(
            r"^\s*#?\s*([A-Z][A-Z0-9_]*)=",
            ENV_EXAMPLE.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    }


def test_every_compose_variable_appears_in_the_env_template() -> None:
    documented = _documented_keys()
    missing = sorted(v for v in _compose_vars() if v not in documented)
    assert not missing, (
        "ops/docker-compose.yml reads these env overrides, but "
        "ops/.env.example never mentions them: " + ", ".join(missing) +
        " — add each with a comment saying what it does and its default, "
        "or the template's 'every override' claim is false again."
    )


def test_the_guard_can_see_something() -> None:
    """A regex that silently stopped matching would make the guard above
    vacuously true forever. Pin that it still finds the real key set."""
    found = _compose_vars()
    assert len(found) >= 10, f"only found {len(found)} compose vars: {found}"
    assert "PSEUDOLIFE_MCP_TOOLSET" in found
    assert "PSEUDOLIFE_WRITER_ID" in found
