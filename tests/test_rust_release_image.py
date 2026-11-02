"""The proposed Rust publisher stays disabled until the maintainer opts in."""
from pathlib import Path

import yaml


def test_rust_release_image_requires_maintainer_decision():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["rust-image"]
    assert job["needs"] == "publish"
    assert job["if"] == "${{ vars.PSEUDOLIFE_PUBLISH_RUST_DAEMON == 'true' }}"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    build = next(s for s in job["steps"] if s.get("uses", "").startswith("docker/build-push-action@"))
    assert build["with"]["file"] == "ops/Dockerfile.daemon-rust"
    assert build["with"]["push"] is True
    assert "pseudolife-daemon-rust:${{ env.VERSION }}" in build["with"]["tags"]
    assert "BUILD_SOURCE=release" in build["with"]["build-args"]
