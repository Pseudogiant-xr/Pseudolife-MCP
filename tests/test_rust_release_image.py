"""The proposed Rust publisher stays disabled until the maintainer opts in."""
from pathlib import Path
from fnmatch import fnmatchcase

import yaml


def test_rust_release_image_requires_maintainer_decision():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["rust-image"]
    assert job["needs"] == "publish"
    assert "github.event_name == 'release'" in job["if"]
    assert "vars.PSEUDOLIFE_PUBLISH_RUST_DAEMON == 'true'" in job["if"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    build = next(s for s in job["steps"] if s.get("uses", "").startswith("docker/build-push-action@"))
    assert build["with"]["file"] == "ops/Dockerfile.daemon-rust"
    assert build["with"]["push"] is True
    assert "pseudolife-daemon-rust:${{ env.VERSION }}" in build["with"]["tags"]
    assert ":latest" not in build["with"]["tags"]
    assert "BUILD_SOURCE=release" in build["with"]["build-args"]


def test_dispatch_cannot_reach_any_publisher():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    for name in ("build", "publish", "images", "registry", "rust-image", "rust-assets-publish"):
        assert "github.event_name == 'release'" in jobs[name]["if"], name
    for name in ("rust-assets", "rust-assets-bundle"):
        assert jobs[name]["permissions"] == {"contents": "read"}
    assert jobs["rust-assets"]["needs"] == "publish"
    assert "needs.publish.result == 'success'" in jobs["rust-assets"]["if"]
    assert "vars.PSEUDOLIFE_PUBLISH_RUST_ASSETS == 'true'" in jobs["rust-assets"]["if"]
    assert "publish" in jobs["rust-assets-publish"]["needs"]
    assert "vars.PSEUDOLIFE_PUBLISH_RUST_ASSETS == 'true'" in jobs["rust-assets-publish"]["if"]


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
