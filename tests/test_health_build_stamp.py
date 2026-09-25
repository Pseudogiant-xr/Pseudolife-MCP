"""``/health`` names the commit the running daemon image was built from.

The 2026-09-23 fresh-eyes review could not answer "what commit is
deployed": ``/health`` carried the package version, which only moves with
a release, and the main checkout sat two merges behind the deployed image.
``ops/Dockerfile.daemon`` now bakes ``PSEUDOLIFE_BUILD_GIT_SHA``,
``PSEUDOLIFE_BUILD_DIRTY`` and ``PSEUDOLIFE_BUILD_TIME`` from build args
that ``ops/update.ps1|.sh`` fill from the checkout, and ``/health`` reports
them as ``build: {git_sha, dirty, built_at}``.

An image built without the args (a plain ``docker compose up --build``)
reports ``"unknown"`` rather than nothing; a pip install, which has no
image, omits the key. Like ``last_backup``, the block never touches
``status``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pseudolife_memory.daemon import _build_health_payload

_ENV = ("PSEUDOLIFE_BUILD_GIT_SHA", "PSEUDOLIFE_BUILD_DIRTY",
        "PSEUDOLIFE_BUILD_TIME")


class _Svc:
    """Minimal MemoryService stand-in for _build_health_payload."""

    _db_url = "postgresql://fake"
    _persist_errors = 0
    _init_refusal = None
    _storage = None


@pytest.fixture(autouse=True)
def _no_ambient_stamp(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_a_stamped_image_names_its_commit(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_BUILD_GIT_SHA", "59b87631" + "0" * 32)
    monkeypatch.setenv("PSEUDOLIFE_BUILD_DIRTY", "false")
    monkeypatch.setenv("PSEUDOLIFE_BUILD_TIME", "2026-09-25T03:00:00Z")

    payload = _build_health_payload(_Svc(), token_present=False)

    assert payload["build"] == {
        "git_sha": "59b87631" + "0" * 32,
        "dirty": False,
        "built_at": "2026-09-25T03:00:00Z",
    }
    assert payload["status"] == "ok"


def test_a_dirty_build_says_so(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_BUILD_GIT_SHA", "a" * 40)
    monkeypatch.setenv("PSEUDOLIFE_BUILD_DIRTY", "true")
    monkeypatch.setenv("PSEUDOLIFE_BUILD_TIME", "2026-09-25T03:00:00Z")

    build = _build_health_payload(_Svc(), token_present=False)["build"]

    assert build["dirty"] is True


def test_an_unstamped_image_reports_unknown_not_a_guess(monkeypatch):
    # The Dockerfile's ARG defaults: what a build without the update script
    # bakes in.
    for name in _ENV:
        monkeypatch.setenv(name, "unknown")

    build = _build_health_payload(_Svc(), token_present=False)["build"]

    assert build == {"git_sha": "unknown", "dirty": None,
                     "built_at": "unknown"}


def test_a_pip_install_has_no_image_and_no_build_block():
    payload = _build_health_payload(_Svc(), token_present=False)

    assert "build" not in payload


# --- The image carries the stamp ------------------------------------------

_REPO = Path(__file__).resolve().parents[1]
_DOCKERFILE = (_REPO / "ops" / "Dockerfile.daemon").read_text(encoding="utf-8")
_ARGS = {"GIT_SHA": "PSEUDOLIFE_BUILD_GIT_SHA",
         "GIT_DIRTY": "PSEUDOLIFE_BUILD_DIRTY",
         "BUILD_TIME": "PSEUDOLIFE_BUILD_TIME"}


def _instructions() -> list[str]:
    """Dockerfile instructions, continuation lines joined, comments dropped."""
    joined = re.sub(r"\\\n", " ", _DOCKERFILE)
    return [line.strip() for line in joined.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_the_image_bakes_the_stamp_into_labels_and_environment():
    text = " ".join(_instructions())
    for arg, env in _ARGS.items():
        assert re.search(rf"\bARG {arg}=unknown\b", text), arg
        assert re.search(rf"\b{env}=\${arg}\b", text), env
    assert re.search(r"org\.opencontainers\.image\.revision=\$GIT_SHA\b", text)
    assert re.search(r"org\.opencontainers\.image\.created=\$BUILD_TIME\b", text)


def test_the_stamp_is_declared_after_every_run():
    # A changed ARG invalidates every later RUN, and the build time changes
    # on every build: declared any earlier, the multi-GB dependency and
    # model layers would never be cached again.
    instructions = _instructions()
    last_run = max(i for i, s in enumerate(instructions) if s.startswith("RUN "))
    first_arg = min(i for i, s in enumerate(instructions)
                    if s.startswith("ARG ") and s.split()[1].split("=")[0] in _ARGS)
    assert first_arg > last_run


def test_compose_passes_the_update_scripts_stamp_to_the_build():
    compose = (_REPO / "ops" / "docker-compose.yml").read_text(encoding="utf-8")
    for arg, env in _ARGS.items():
        assert re.search(rf"^\s+{arg}: \$\{{{env}:-unknown\}}\s*$", compose,
                         re.MULTILINE), arg


def test_release_images_are_stamped_too():
    workflow = (_REPO / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    assert "GIT_SHA=${{ github.sha }}" in workflow
    assert "GIT_DIRTY=false" in workflow
    assert re.search(r"BUILD_TIME=\$\{\{ env\.BUILD_TIME \}\}", workflow)
