"""Release gates: the Rust image stays opt-in; the native client archives publish with every release, before PyPI."""
from pathlib import Path
from fnmatch import fnmatchcase

import yaml
import pytest


EXPECTED_GATES = {
    **{name: "github.event_name == 'release'" for name in ("build", "publish", "images", "registry")},
    "rust-image": "github.event_name == 'release' && vars.PSEUDOLIFE_PUBLISH_RUST_DAEMON == 'true'",
    "rust-assets": "!cancelled() && (github.event_name == 'workflow_dispatch' || (github.event_name == 'release' && needs.build.result == 'success'))",
    "rust-assets-bundle": "!cancelled() && needs.rust-assets.result == 'success'",
    "rust-assets-publish": "!cancelled() && github.event_name == 'release' && needs.build.result == 'success' && needs.rust-assets-bundle.result == 'success'",
    "release-ready": (
        "!cancelled() && github.event_name == 'release' "
        "&& needs.publish.result == 'success' && needs.images.result == 'success' "
        "&& (needs.rust-image.result == 'success' "
        "|| (needs.rust-image.result == 'skipped' && vars.PSEUDOLIFE_PUBLISH_RUST_DAEMON != 'true'))"
    ),
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
    assert jobs["rust-assets"]["needs"] == "build"
    assert jobs["rust-assets-publish"]["needs"] == ["build", "rust-assets-bundle"]


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
    assert jobs["publish"]["needs"] == ["build", "rust-assets-publish"]


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


def _needs(job) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _upstream(jobs, name) -> set[str]:
    """Every job `name` waits for, directly or through another job."""
    seen, todo = set(), _needs(jobs[name])
    while todo:
        job = todo.pop()
        if job not in seen:
            seen.add(job)
            todo.extend(_needs(jobs[job]))
    return seen


def assert_ready_job_dependencies(jobs):
    assert sorted(_needs(jobs["release-ready"])) == ["images", "publish", "rust-image"]


def test_release_ready_depends_on_the_protected_publish_and_image_jobs():
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]
    assert_ready_job_dependencies(jobs)
    ready = jobs["release-ready"]
    assert_release_gates(jobs)
    assert jobs["publish"]["environment"]["name"] == "pypi"
    assert ready["permissions"] == {"contents": "write"}
    assert jobs["images"]["outputs"] == {
        "daemon-digest": "${{ steps.daemon.outputs.digest }}",
        "pg-digest": "${{ steps.pg.outputs.digest }}",
    }
    assert jobs["rust-image"]["outputs"] == {"digest": "${{ steps.daemon.outputs.digest }}"}
    for name, step_ids in (("images", {"daemon", "pg"}), ("rust-image", {"daemon"})):
        builds = [s for s in jobs[name]["steps"] if s.get("uses", "").startswith("docker/build-push-action@")]
        assert {s["id"] for s in builds} == step_ids
        assert all(s["with"]["push"] is True for s in builds)
        assert "continue-on-error" not in jobs[name]
        assert not any("continue-on-error" in s for s in jobs[name]["steps"])
    steps = [s for s in ready["steps"] if s.get("run") == "python rust/ci/release_assets.py publish-ready"]
    assert len(steps) == 1
    assert steps[0]["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "DAEMON_IMAGE_DIGEST": "${{ needs.images.outputs.daemon-digest }}",
        "PG_IMAGE_DIGEST": "${{ needs.images.outputs.pg-digest }}",
        "RUST_IMAGE_DIGEST": "${{ needs.rust-image.outputs.digest }}",
        "RUST_IMAGE_RESULT": "${{ needs.rust-image.result }}",
    }


def test_release_ready_dependency_guard_rejects_early_publication():
    with pytest.raises(AssertionError):
        assert_ready_job_dependencies({"release-ready": {"needs": ["images", "rust-image"]}})


def test_native_archives_reach_the_release_before_pypi():
    # Older updaters learn that a release exists from PyPI, so keep the
    # archives and SHA256SUMS available before the irreversible wheel upload.
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]
    for publisher in ("publish", "images", "rust-image", "registry"):
        assert "rust-assets-publish" in _upstream(jobs, publisher), publisher
    for native in ("rust-assets", "rust-assets-bundle", "rust-assets-publish"):
        assert not {"publish", "images", "rust-image", "registry"} & _upstream(jobs, native), native
        assert "build" in _upstream(jobs, native), native
    # No status function: publish runs only when every job it needs succeeded.
    gate = jobs["publish"]["if"]
    assert not any(word in gate for word in ("always()", "!cancelled()", "failure()", ".result")), gate
    # A native job or step that may fail without failing its job would let
    # PyPI follow an incomplete attachment.
    for native in ("rust-assets", "rust-assets-bundle", "rust-assets-publish"):
        assert "continue-on-error" not in jobs[native], native
        assert not any("continue-on-error" in step for step in jobs[native]["steps"]), native
