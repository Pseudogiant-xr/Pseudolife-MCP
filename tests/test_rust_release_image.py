"""Release gates: the Rust image stays opt-in; the native client archives publish with every approved release."""
from pathlib import Path
from fnmatch import fnmatchcase

import yaml
import pytest


EXPECTED_GATES = {
    **{name: "github.event_name == 'release'" for name in ("build", "publish", "images", "registry")},
    "rust-image": "github.event_name == 'release' && vars.PSEUDOLIFE_PUBLISH_RUST_DAEMON == 'true'",
    "rust-assets": "!cancelled() && (github.event_name == 'workflow_dispatch' || (github.event_name == 'release' && needs.publish.result == 'success'))",
    "rust-assets-bundle": "!cancelled() && needs.rust-assets.result == 'success'",
    "rust-assets-publish": "!cancelled() && github.event_name == 'release' && needs.publish.result == 'success' && needs.rust-assets-bundle.result == 'success'",
}


def assert_release_gates(jobs):
    for name, expected in EXPECTED_GATES.items():
        expression = jobs[name]["if"].strip().removeprefix("${{").removesuffix("}}").strip()
        assert " ".join(expression.split()) == expected, name


@pytest.mark.parametrize("name", EXPECTED_GATES)
def test_release_gate_rejects_added_dispatch_bypass(name):
    jobs = {key: {"if": value} for key, value in EXPECTED_GATES.items()}
    jobs[name]["if"] += " || github.event_name == 'workflow_dispatch'"
    with pytest.raises(AssertionError, match=name):
        assert_release_gates(jobs)


def test_rust_release_image_requires_maintainer_decision():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["rust-image"]
    assert job["needs"] == "publish"
    assert_release_gates(workflow["jobs"])
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    build = next(s for s in job["steps"] if s.get("uses", "").startswith("docker/build-push-action@"))
    assert build["with"]["file"] == "ops/Dockerfile.daemon-rust"
    assert build["with"]["push"] is True
    assert build["with"]["tags"].splitlines() == [
        "ghcr.io/${{ env.OWNER_LC }}/pseudolife-daemon-rust:${{ env.VERSION }}"]
    assert "BUILD_SOURCE=release" in build["with"]["build-args"]


def test_dispatch_cannot_reach_any_publisher():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert_release_gates(jobs)
    for name in ("rust-assets", "rust-assets-bundle"):
        assert jobs[name]["permissions"] == {"contents": "read"}
    assert jobs["rust-assets"]["needs"] == "publish"
    assert "publish" in jobs["rust-assets-publish"]["needs"]


def test_bundle_rerun_downloads_only_target_archives():
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]
    uploaded = next(step["with"]["name"] for step in jobs["rust-assets"]["steps"]
                    if step.get("uses", "").startswith("actions/upload-artifact@"))
    pattern = next(step["with"]["pattern"] for step in jobs["rust-assets-bundle"]["steps"]
                   if step.get("uses", "").startswith("actions/download-artifact@"))
    complete = next(step["with"]["name"] for step in jobs["rust-assets-bundle"]["steps"]
                    if step.get("uses", "").startswith("actions/upload-artifact@"))
    for row in jobs["rust-assets"]["strategy"]["matrix"]["include"]:
        assert fnmatchcase(uploaded.replace("${{ matrix.target }}", row["target"]), pattern)
    assert not fnmatchcase(complete, pattern), "reruns must not ingest the previous complete bundle"


def test_native_versions_are_checked_before_pypi_publish():
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]
    steps = jobs["build"]["steps"]
    guard = next(i for i, step in enumerate(steps)
                 if step.get("run", "").strip() == "python rust/ci/release_assets.py version")
    build = next(i for i, step in enumerate(steps) if "python -m build" in step.get("run", ""))
    assert guard < build
    assert jobs["publish"]["needs"] == "build"


def test_asset_upload_verifies_downloads_and_supports_partial_retry():
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]
    steps = jobs["rust-assets-publish"]["steps"]
    download = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/download-artifact@"))
    verify = next(i for i, step in enumerate(steps) if step.get("run", "").strip() == "sha256sum --check SHA256SUMS")
    upload = next(i for i, step in enumerate(steps)
                  if step.get("run", "").strip() == "python rust/ci/release_assets.py publish")
    assert download < verify < upload
    assert steps[verify]["working-directory"] == "release-assets"
    assert any(step.get("uses", "").startswith("actions/checkout@") for step in steps[:upload])
    assert any(step.get("uses", "").startswith("actions/setup-python@") for step in steps[:upload])
