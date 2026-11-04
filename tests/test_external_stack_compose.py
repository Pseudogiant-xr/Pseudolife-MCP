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


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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
