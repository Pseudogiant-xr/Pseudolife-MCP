"""Release gates for version checks, native assets and protected publication."""
from fnmatch import fnmatchcase
from graphlib import TopologicalSorter
from pathlib import Path

import pytest
import yaml

from rust.ci.ci_budget import can_start, needs_of


NATIVE_BUILD_JOBS = {"rust-assets", "rust-assets-bundle"}
NATIVE_ASSET_JOBS = NATIVE_BUILD_JOBS | {"rust-assets-publish"}
PROTECTED_PUBLISHERS = {"publish", "images", "rust-image", "registry", "release-ready"}

EXPECTED_GATES = {
    **{name: "github.event_name == 'release'" for name in ("guards", "build", "publish", "images", "registry")},
    "rust-image": "github.event_name == 'release' && vars.PSEUDOLIFE_PUBLISH_RUST_DAEMON == 'true'",
    "rust-assets": "!cancelled() && (github.event_name == 'workflow_dispatch' || (github.event_name == 'release' && needs.guards.result == 'success'))",
    "rust-assets-bundle": "!cancelled() && needs.rust-assets.result == 'success'",
    "rust-assets-publish": "!cancelled() && github.event_name == 'release' && needs.guards.result == 'success' && needs.rust-assets-bundle.result == 'success'",
    "release-ready": (
        "!cancelled() && github.event_name == 'release' "
        "&& needs.publish.result == 'success' && needs.images.result == 'success' "
        "&& needs.rust-image.result == 'success'"
    ),
}


@pytest.fixture
def workflow():
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))


@pytest.fixture
def jobs(workflow):
    return workflow["jobs"]


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


def test_rust_release_image_requires_maintainer_decision(jobs):
    job = jobs["rust-image"]
    assert job["needs"] == "publish"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    build = next(s for s in job["steps"] if s.get("uses", "").startswith("docker/build-push-action@"))
    assert build["with"]["file"] == "ops/Dockerfile.daemon-rust"
    assert build["with"]["push"] is True
    assert build["with"]["tags"].splitlines() == [
        "ghcr.io/${{ env.OWNER_LC }}/pseudolife-daemon-rust:${{ env.VERSION }}"]
    assert "BUILD_SOURCE=release" in build["with"]["build-args"]


def test_privileged_jobs_only_run_on_release_event(workflow, jobs):
    assert workflow["permissions"] == {"contents": "read"}
    # PyYAML also accepts the YAML 1.1 boolean spelling `on`.
    assert workflow.get("on", workflow.get(True))["release"] == {"types": ["published"]}
    privileged = set()
    for name, job in jobs.items():
        permissions = job.get("permissions", workflow["permissions"])
        has_write = permissions == "write-all" or (
            isinstance(permissions, dict) and "write" in permissions.values()
        )
        if has_write or "environment" in job:
            privileged.add(name)
            assert not can_start(job, set(), "workflow_dispatch"), name
    assert privileged == PROTECTED_PUBLISHERS | {"rust-assets-publish"}
    assert_release_gates(jobs)


def _reachable_jobs(jobs, event, unsuccessful=()):
    """Find jobs that can start given prerequisites that did not succeed.

    The budget evaluator treats those results as skipped. For the conditions
    pinned here, failed and skipped prerequisites both block success checks.
    Repository variables are unknown, so this includes the opt-in image job
    whenever its prerequisites can succeed.
    """
    blocked = set(unsuccessful)
    graph = {name: needs_of(job) for name, job in jobs.items()}
    for name in TopologicalSorter(graph).static_order():
        if not can_start(jobs[name], blocked, event):
            blocked.add(name)
    return jobs.keys() - blocked


def test_dispatch_cannot_reach_any_publisher(jobs):
    assert _reachable_jobs(jobs, "workflow_dispatch") == NATIVE_BUILD_JOBS
    for name in NATIVE_BUILD_JOBS:
        assert jobs[name]["permissions"] == {"contents": "read"}


def test_bundle_rerun_downloads_only_target_archives(jobs):
    uploaded = next(step["with"]["name"] for step in jobs["rust-assets"]["steps"]
                    if step.get("uses", "").startswith("actions/upload-artifact@"))
    pattern = next(step["with"]["pattern"] for step in jobs["rust-assets-bundle"]["steps"]
                   if step.get("uses", "").startswith("actions/download-artifact@"))
    complete = next(step["with"]["name"] for step in jobs["rust-assets-bundle"]["steps"]
                    if step.get("uses", "").startswith("actions/upload-artifact@"))
    for row in jobs["rust-assets"]["strategy"]["matrix"]["include"]:
        assert fnmatchcase(uploaded.replace("${{ matrix.target }}", row["target"]), pattern)
    assert not fnmatchcase(complete, pattern), "reruns must not ingest the previous complete bundle"


def test_version_guards_gate_both_builds(jobs):
    guards = jobs["guards"]
    assert guards["permissions"] == {"contents": "read"}
    assert [step["name"] for step in guards["steps"] if "run" in step] == [
        "Guard — release tag must match the release version",
        "Guard — server.json versions must match the release tag",
        "Guard — native crate versions must match the release",
    ]
    assert guards["steps"][-3]["run"] == 'python rust/ci/release_version.py check --tag "$GITHUB_REF_NAME"'
    assert guards["steps"][-1]["run"] == "python rust/ci/release_assets.py version"
    assert not any(step.get("uses", "").startswith("actions/upload-artifact@") for step in guards["steps"])
    assert needs_of(jobs["build"]) == ["guards"]
    assert any("python -m build" in step.get("run", "") for step in jobs["build"]["steps"])
    for name in NATIVE_ASSET_JOBS:
        assert "guards" in _upstream(jobs, name), name
    assert _reachable_jobs(jobs, "release", {"guards"}) == set()


def test_asset_upload_verifies_downloads_and_supports_partial_retry(jobs):
    steps = jobs["rust-assets-publish"]["steps"]
    download = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/download-artifact@"))
    verify = next(i for i, step in enumerate(steps) if step.get("run", "").strip() == "sha256sum --check SHA256SUMS")
    upload = next(i for i, step in enumerate(steps)
                  if step.get("run", "").strip() == "python rust/ci/release_assets.py publish")
    assert download < verify < upload
    assert steps[verify]["working-directory"] == "release-assets"
    assert any(step.get("uses", "").startswith("actions/checkout@") for step in steps[:upload])
    assert any(step.get("uses", "").startswith("actions/setup-python@") for step in steps[:upload])


def _upstream(jobs, name) -> set[str]:
    """Every job `name` waits for, directly or through another job."""
    seen, todo = set(), needs_of(jobs[name])
    while todo:
        job = todo.pop()
        if job not in seen:
            seen.add(job)
            todo.extend(needs_of(jobs[job]))
    return seen


def assert_ready_job_dependencies(jobs):
    assert sorted(needs_of(jobs["release-ready"])) == ["images", "publish", "rust-image"]


def test_release_ready_depends_on_the_protected_publish_and_image_jobs(jobs):
    assert_ready_job_dependencies(jobs)
    ready = jobs["release-ready"]
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
    }


def test_release_ready_dependency_guard_rejects_early_publication():
    with pytest.raises(AssertionError):
        assert_ready_job_dependencies({"release-ready": {"needs": ["images", "rust-image"]}})


def test_release_ready_does_not_run_when_the_rust_image_is_skipped(jobs):
    reachable = _reachable_jobs(jobs, "release", {"rust-image"})
    assert reachable == jobs.keys() - {"rust-image", "release-ready"}


def test_publishers_wait_for_the_human_approval_gate(jobs):
    assert {name for name, job in jobs.items() if "environment" in job} == {"publish"}
    assert jobs["publish"]["environment"]["name"] == "pypi"
    for name in PROTECTED_PUBLISHERS - {"publish"}:
        assert "publish" in _upstream(jobs, name), name
    assert not PROTECTED_PUBLISHERS & _reachable_jobs(jobs, "release", {"publish"})


def test_native_archives_can_attach_when_the_wheel_fails(jobs):
    reachable = _reachable_jobs(jobs, "release", {"build"})
    assert reachable == {"guards"} | NATIVE_ASSET_JOBS


def test_native_jobs_do_not_depend_on_the_wheel(jobs):
    for name in NATIVE_ASSET_JOBS:
        assert "build" not in _upstream(jobs, name), name


def test_native_archives_reach_the_release_before_pypi(jobs):
    # Older updaters learn that a release exists from PyPI, so keep the
    # archives and SHA256SUMS available before the irreversible wheel upload.
    assert set(needs_of(jobs["publish"])) == {"build", "rust-assets-publish"}
    for name in NATIVE_ASSET_JOBS:
        assert not PROTECTED_PUBLISHERS & _upstream(jobs, name), name
    assert not PROTECTED_PUBLISHERS & _reachable_jobs(jobs, "release", {"rust-assets-publish"})
    # A job or step that masks failure could let incomplete builds publish.
    for name in NATIVE_ASSET_JOBS | {"guards", "build"}:
        assert "continue-on-error" not in jobs[name], name
        assert not any("continue-on-error" in step for step in jobs[name]["steps"]), name
