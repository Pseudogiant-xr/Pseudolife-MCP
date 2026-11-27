"""The paste-ready stack accounts for every upstream service and runtime option."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "ops/docker-compose.stack.yml"
SIDECAR = ROOT / "ops/docker-compose.stack-extractor.yml"
DAEMON = "pseudolife-daemon"
EXTRACTOR = "pseudolife-extractor"


class _ComposeLoader(yaml.SafeLoader):
    """Safe YAML plus Compose's ``!reset`` merge tag (the Rust overlay's
    ``build: !reset null``), read as the value it resets to."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def load(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)


def release_version():
    import tomllib
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def expected_stack():
    base = load(ROOT / "ops/docker-compose.yml")
    expected = copy.deepcopy(base)
    del expected["services"][EXTRACTOR]
    for service in expected["services"].values():
        del service["build"]
    ghcr = load(ROOT / "ops/docker-compose.ghcr.yml")
    # Image replacement is the only supported overlay difference. A new
    # runtime override must be accounted for here rather than silently lost.
    assert set(ghcr) == {"services"}, "unhandled GHCR overlay top-level settings"
    assert set(ghcr["services"]) == set(expected["services"]), "unhandled GHCR overlay services"
    for service in ghcr["services"].values():
        assert set(service) == {"image"}, "unhandled GHCR overlay runtime settings"
    expected["services"]["pseudolife-pg"]["image"] = ghcr["services"]["pseudolife-pg"]["image"]
    daemon = expected["services"][DAEMON]
    daemon["image"] = ghcr["services"][DAEMON]["image"]
    # Only the extractor defaults differ: the unpublished sidecar is opt-in.
    daemon["environment"].update({
        "PSEUDOLIFE_DREAM_BASE_URL": "${PSEUDOLIFE_DREAM_BASE_URL:-}",
        "PSEUDOLIFE_DREAM_EXTRACTOR_MODE": "${PSEUDOLIFE_DREAM_EXTRACTOR_MODE:-primary}",
    })
    # The Rust release overlay the updater applies: the stack runs the Rust
    # daemon (the Python daemon is retired). Every setting of the overlay is
    # accounted for here, so a new one cannot be silently lost.
    rust = load(ROOT / "ops/docker-compose.rust-ghcr.yml")
    assert set(rust) == {"services", "volumes"}, "unhandled Rust overlay top-level settings"
    assert set(rust["services"]) == {DAEMON, EXTRACTOR}, "unhandled Rust overlay services"
    overlay = rust["services"][DAEMON]
    assert set(overlay) == {"pull_policy", "build", "image", "labels", "environment", "volumes"},         "unhandled Rust overlay runtime settings"
    # The overlay requires an explicit tag the updater supplies; the stack is
    # pasted by hand, so it defaults to this release, which the cut moves.
    image, _ = overlay["image"].split(":${", 1)
    daemon["image"] = f"{image}:${{PSEUDOLIFE_IMAGE_TAG:-{release_version()}}}"
    # A pinned version needs no re-pull on every redeploy, and the stack builds nothing.
    assert overlay["pull_policy"] == "always" and overlay["build"] is None
    daemon["labels"] = dict(overlay["labels"])
    daemon["environment"].update(overlay["environment"])
    daemon["volumes"] = [*daemon["volumes"], *overlay["volumes"]]
    expected["volumes"].update(rust["volumes"])
    return expected


def test_fresh_stack_needs_no_extractor_process(monkeypatch):
    from pseudolife_memory.daemon import _extractor_status
    from pseudolife_memory.memory.dream import NoOpExtractor, build_extractor_with_fallback
    from pseudolife_memory.utils.config import DreamConfig

    env = load(STACK)["services"][DAEMON]["environment"]
    for key, value in env.items():
        if key.startswith("PSEUDOLIFE_DREAM_"):
            # Use exactly the compose defaults, including empty strings.
            monkeypatch.setenv(key, value.split(":-", 1)[1][:-1])
    extractor, which = build_extractor_with_fallback(DreamConfig())
    assert isinstance(extractor, NoOpExtractor)
    assert which == "primary"
    svc = SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(dream=DreamConfig())))
    assert _extractor_status(svc) == "none"


def test_stack_matches_every_upstream_runtime_setting():
    # Whole-document comparison pins service names, all env keys/defaults,
    # volumes (including legacy overrides), ports, health, caps and logging.
    assert load(STACK) == expected_stack()
    stack = load(STACK)
    assert stack["name"] == "pseudolife-mcp"
    for service in stack["services"].values():
        assert service["image"].startswith("ghcr.io/pseudogiant-xr/")
        assert not ({"build", "extends", "env_file"} & service.keys())
    assert "include" not in stack
    # The Rust daemon, labelled as the updater recognises it.
    assert stack["services"][DAEMON]["image"].startswith("ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:")
    assert stack["services"][DAEMON]["labels"] == {"org.pseudolife.engine": "rust"}


def test_opt_in_sidecar_accounts_for_the_remaining_upstream_service():
    base = load(ROOT / "ops/docker-compose.yml")
    sidecar = load(SIDECAR)
    assert sidecar == {"services": {EXTRACTOR: base["services"][EXTRACTOR]}}
    assert set(load(STACK)["services"]) | set(sidecar["services"]) == set(base["services"])


def test_ghcr_runtime_additions_are_not_silently_ignored(monkeypatch):
    original = load

    def with_extra_setting(path):
        document = original(path)
        if path.name == "docker-compose.ghcr.yml":
            document["services"][DAEMON]["environment"] = {"NEW_RUNTIME_SETTING": "fixture"}
        return document

    monkeypatch.setitem(expected_stack.__globals__, "load", with_extra_setting)
    with pytest.raises(AssertionError, match="GHCR overlay"):
        expected_stack()


def test_release_updater_recognises_stack_and_preserves_env(tmp_path, monkeypatch):
    from pseudolife_memory import update_cli as up

    compose = tmp_path / STACK.name
    compose.write_bytes(STACK.read_bytes())
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=fixture\nPSEUDOLIFE_BANK_VOLUME=ops_pseudolife_pgdata\n", encoding="utf-8")
    labels = {
        "com.docker.compose.project": "pseudolife-mcp",
        "com.docker.compose.project.config_files": str(compose),
        "com.docker.compose.project.working_dir": str(tmp_path),
    }
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: (0, json.dumps(labels)))
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "data")
    context = up.Update(up.Options(env_file=env_file)).compose_context()
    assert context["project"] == "pseudolife-mcp"
    assert context["env_file"] == [env_file]
    assert context["args"][:4] == ["-p", "pseudolife-mcp", "--env-file", str(env_file)]
    assert context["files"][0] == str(compose)
    assert Path(context["files"][-1]).name == "docker-compose.ghcr.yml"
