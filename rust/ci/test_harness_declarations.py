"""run.py's one-file-each declarations and scenario families (run_scenarios/README.md)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rust/daemon/harness"))

import run  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def test_every_declared_case_names_a_case_and_marks_it():
    assert run.stale_declared_cases() == []
    marked = {c["name"]: c["declared"] for scn in (run.Tokens(), run.Tokenless()) for c in scn.cases()}
    for name, declared in run.DECLARED_CASES.items():
        assert marked[name] == declared


def test_the_declared_health_keys_are_withheld_and_reported():
    declared: list[str] = []
    body = {key: 1 for key in run.HEALTH_DECLARED_ONLY_PYTHON} | {"ok": True}
    assert run.normalize_health(body, declared) == {"ok": True}
    assert declared == [f"health.{k} (python only)" for k in sorted(run.HEALTH_DECLARED_ONLY_PYTHON)]


def test_with_declared_marks_only_named_cases_and_keeps_an_explicit_marker(monkeypatch):
    monkeypatch.setattr(run, "DECLARED_CASES", {"served later": "W9: owner"})
    cases = run.with_declared([run.case("served later", "GET", "/a"), run.case("other", "GET", "/b"),
                               run.case("served later", "GET", "/c", declared="explicit")])
    assert [c["declared"] for c in cases] == ["W9: owner", None, "explicit"]


@pytest.mark.parametrize("name, body, error", [
    ("wrong-name.json", {"case": "maintainer known GET", "declared": "W2-F"}, "belongs in maintainer-known-get.json"),
    ("maintainer-known-get.json", {"declared": "W2-F"}, r"missing \['case'\]"),
    ("maintainer-known-get.json", {"case": "maintainer known GET"}, r"missing \['declared'\]"),
])
def test_a_misnamed_or_incomplete_declaration_is_refused(tmp_path, name, body, error):
    write(tmp_path / "cases" / name, json.dumps(body) + "\n")
    with pytest.raises(ValueError, match=error):
        run.load_declarations("cases", "case", tmp_path)


def test_a_stale_declared_case_is_reported(monkeypatch):
    monkeypatch.setattr(run, "DECLARED_CASES", {**run.DECLARED_CASES, "renamed away": "W9: owner"})
    assert run.stale_declared_cases() == ["renamed away"]


def test_scenario_families_register_in_file_order_and_refuse_a_taken_name(tmp_path):
    write(tmp_path / "b_family.py", "def register(harness):\n"
          "    class B(harness.Scenario):\n        name = 'family-b'\n"
          "    class C(harness.Scenario):\n        name = 'family-c'\n    return [B, C]\n")
    write(tmp_path / "a_family.py", "def register(harness):\n"
          "    class A(harness.Scenario):\n        name = 'family-a'\n"
          "        def cases(self):\n            return [harness.case('x', 'GET', '/health')]\n"
          "    return [A]\n")
    found = run.load_scenario_families(tmp_path)
    assert list(found) == ["family-a", "family-b", "family-c"]
    assert issubclass(found["family-a"], run.Scenario)
    assert found["family-a"]().cases()[0]["path"] == "/health"
    write(tmp_path / "c_family.py", "def register(harness):\n"
          "    class T(harness.Scenario):\n        name = 'tokens'\n    return [T]\n")
    with pytest.raises(ValueError, match="'tokens' is already registered"):
        run.load_scenario_families(tmp_path)


def test_the_committed_families_load_and_the_harness_knows_them(monkeypatch):
    # Loaded again against empty registries: the import already registered them.
    monkeypatch.setattr(run, "RESPONSE_SCRUBS", {})
    found = run.load_scenario_families(registered={})
    assert set(found) <= set(run.SCENARIOS)
    assert all(run.SCENARIOS[name].__name__ == cls.__name__ for name, cls in found.items())


def test_a_family_module_may_use_dataclasses(tmp_path):
    write(tmp_path / "typed.py", "from __future__ import annotations\nfrom dataclasses import dataclass\n\n"
          "@dataclass\nclass Row:\n    name: str\n\n"
          "def register(harness):\n    class Typed(harness.Scenario):\n        name = 'typed'\n"
          "    return [Typed]\n")
    assert list(run.load_scenario_families(tmp_path)) == ["typed"]


def test_a_response_scrub_applies_to_its_path_only_and_once(monkeypatch):
    monkeypatch.setattr(run, "RESPONSE_SCRUBS", {})

    def scrub(resp):
        resp["json"]["config_path"] = "<home>/data/config.yaml"
        return resp

    run.register_response_scrub("/api/config", scrub)
    answer = {"status": 200, "json": {"config_path": "/home/x/data/config.yaml"}}
    assert run.normalize_response(answer, "/api/config?x=1", [])["json"]["config_path"] == "<home>/data/config.yaml"
    assert run.normalize_response(answer, "/api/stats", [])["json"]["config_path"] == "/home/x/data/config.yaml"
    with pytest.raises(ValueError, match="already registered"):
        run.register_response_scrub("/api/config", scrub)
