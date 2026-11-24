"""The archives consumed by native installers have one fixed layout."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile
from unittest.mock import patch

import pytest

import release_assets as assets
import release_version


@pytest.mark.parametrize("target", assets.TARGETS)
def test_binary_archive_has_only_the_installed_executable(tmp_path, target):
    binary = tmp_path / "pseudolife-stdio"
    binary.write_bytes(b"native executable fixture")
    out = tmp_path / "out"
    archive = assets.package(binary, "1.2.3", target, out)
    assert archive.name == assets.asset_name("1.2.3", target)
    name, data = assets.binary_contents(archive, target)
    assert name == ("pseudolife-mcp.exe" if target.endswith("msvc") else "pseudolife-mcp")
    assert data == binary.read_bytes()
    if archive.suffix == ".gz":
        with tarfile.open(archive) as stream:
            assert stream.getmembers()[0].mode == 0o755


def test_windows_smoke_checks_imports_in_the_packaged_bytes_before_running(tmp_path, monkeypatch):
    target = "x86_64-pc-windows-msvc"
    binary = tmp_path / "pseudolife-stdio.exe"
    packed = b"Windows fixture bytes"
    binary.write_bytes(packed)
    archive = assets.package(binary, "1.2.3", target, tmp_path / "out")
    binary.write_bytes(b"changed after packaging")

    def refuse_imports(data):
        assert data == packed
        raise ValueError("test import refusal")

    monkeypatch.setattr(assets, "check_windows_imports", refuse_imports)
    monkeypatch.setattr(assets.subprocess, "run", lambda *args, **kwargs: pytest.fail("executed a refused archive"))
    with pytest.raises(ValueError, match="test import refusal"):
        assets.smoke(archive, "1.2.3", target)


def test_assemble_hashes_the_complete_set_and_preserves_compose_bytes(tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    out = tmp_path / "out"
    root = tmp_path / "repo"
    (root / "ops").mkdir(parents=True)
    for name in assets.BUNDLE_FILES:
        (root / "ops" / name).write_bytes((name + "\n").encode())
    for target in assets.TARGETS:
        assets.package(binary, "1.2.3", target, out)
    assets.assemble(root, "1.2.3", out)
    sums = (out / "SHA256SUMS").read_text().splitlines()
    assert len(sums) == 6
    assert [line.split("  ")[1] for line in sums] == sorted(path.name for path in out.iterdir() if path.name != "SHA256SUMS")
    for line in sums:
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((out / name).read_bytes()).hexdigest()
    with tarfile.open(out / "pseudolife-compose-1.2.3.tar.gz") as stream:
        assert stream.getnames() == list(assets.BUNDLE_FILES)
        for member in stream:
            assert member.isfile()
            assert stream.extractfile(member).read() == (root / "ops" / member.name).read_bytes()


def test_extractor_build_needs_no_unbundled_copy_inputs():
    source = (assets.ROOT / "ops/Dockerfile.extractor").read_text()
    for line in source.splitlines():
        if line.startswith(("COPY ", "ADD ")):
            assert line.startswith("COPY --from="), "add local extractor inputs to BUNDLE_FILES before release"
    overlay = (assets.ROOT / "ops/docker-compose.rust-ghcr.yml").read_text()
    assert "pseudolife-extractor:\n    build:\n      context: .\n      dockerfile: Dockerfile.extractor" in overlay


def test_assemble_refuses_missing_or_extra_targets_before_checksums(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="asset set"):
        assets.assemble(tmp_path, "1.2.3", tmp_path)
    assert not (tmp_path / "SHA256SUMS").exists()


@pytest.mark.parametrize("version", ["../bad", "v1.2.3", "1.2.3\n", "1.2.3/a"])
def test_unsafe_or_tag_shaped_versions_are_refused(version):
    with pytest.raises(ValueError):
        assets.asset_name(version, assets.TARGETS[0])


def test_wrong_member_and_symlink_are_refused(tmp_path):
    target = assets.TARGETS[0]
    archive = tmp_path / assets.asset_name("1.2.3", target)
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("pseudolife-mcp")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        stream.addfile(member)
    with pytest.raises(ValueError, match="regular executable"):
        assets.binary_contents(archive, target)
    archive = tmp_path / assets.asset_name("1.2.3", assets.TARGETS[-1])
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("bin/pseudolife-mcp.exe", b"fixture")
    with pytest.raises(ValueError, match="archive members"):
        assets.binary_contents(archive, assets.TARGETS[-1])

def test_archive_bytes_are_independent_of_packaging_time(tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    root = tmp_path / "repo"
    (root / "ops").mkdir(parents=True)
    for name in assets.BUNDLE_FILES:
        (root / "ops" / name).write_bytes(name.encode())
    snapshots = []
    for index, timestamp in enumerate((1700000000, 1800000000)):
        out = tmp_path / str(index)
        with patch("time.time", return_value=timestamp):
            for target in assets.TARGETS:
                assets.package(binary, "1.2.3", target, out)
            assets.assemble(root, "1.2.3", out)
        snapshots.append({path.name: path.read_bytes() for path in out.iterdir()})
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("crate", ["shim", "daemon"])
def test_version_refuses_either_mismatched_native_crate(tmp_path, crate):
    # Every field the version cut writes, copied from this checkout.
    for path in {release_version.AUTHORITY, *(row[1] for row in release_version.FIELDS)}:
        if (assets.ROOT / path).is_file():
            (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / path).write_bytes((assets.ROOT / path).read_bytes())
    value = release_version.read(assets.ROOT)
    assert assets.version(tmp_path) == value
    manifest = tmp_path / "rust" / crate / "Cargo.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace(f'\nversion = "{value}"', '\nversion = "1.2.4"', 1), encoding="utf-8")
    with pytest.raises(ValueError, match="1.2.4" if crate == "daemon" else "but the release version is 1.2.4"):
        assets.version(tmp_path)


def complete_assets(tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"fixture")
    out = tmp_path / "out"
    for target in assets.TARGETS:
        assets.package(binary, "1.2.3", target, out)
    assets.assemble(assets.ROOT, "1.2.3", out)
    return out


def fake_release(monkeypatch, remote, fail_upload=None):
    calls = []

    def view(args):
        assert args == ["gh", "release", "view", "v1.2.3", "--repo", "owner/repo", "--json", "assets"]
        calls.append(args)
        return json.dumps({"assets": [{"name": name} for name in remote]}).encode()

    def run(args, *, check):
        assert check and args[:2] == ["gh", "release"]
        assert "--clobber" not in args
        calls.append(args)
        if args[2] == "download":
            name = args[args.index("--pattern") + 1]
            (Path(args[args.index("--dir") + 1]) / name).write_bytes(remote[name])
        else:
            assert args[2] == "upload"
            path = Path(args[4])
            if path.name == fail_upload:
                raise subprocess.CalledProcessError(1, args)
            assert path.name not in remote, "existing assets must never be overwritten"
            remote[path.name] = path.read_bytes()

    monkeypatch.setattr(assets.subprocess, "check_output", view)
    monkeypatch.setattr(assets.subprocess, "run", run)
    return calls


def test_publication_recovers_partial_upload_without_replacing_bytes(tmp_path, monkeypatch):
    out = complete_assets(tmp_path)
    expected = {path.name: path.read_bytes() for path in out.iterdir()}
    names = sorted(expected)
    remote = {"unrelated-wheel.whl": b"leave untouched"}
    fake_release(monkeypatch, remote, fail_upload=names[1])
    with pytest.raises(subprocess.CalledProcessError):
        assets.publish_assets("1.2.3", out, "v1.2.3", "owner/repo")
    assert remote[names[0]] == expected[names[0]]
    calls = fake_release(monkeypatch, remote)
    assets.publish_assets("1.2.3", out, "v1.2.3", "owner/repo")
    assert remote == {**expected, "unrelated-wheel.whl": b"leave untouched"}
    assert [call[2] for call in calls].count("download") == 1
    assert [call[2] for call in calls].count("upload") == 6


def test_ready_marker_is_idempotent_and_refuses_different_published_bytes(monkeypatch):
    remote = {}
    calls = fake_release(monkeypatch, remote)
    images = {
        "pseudolife-daemon": "sha256:" + "a" * 64,
        "pseudolife-pg": "sha256:" + "b" * 64,
        "pseudolife-daemon-rust": "sha256:" + "c" * 64,
    }
    args = ("1.2.3", "v1.2.3", "owner/repo", "f" * 40, images)
    assets.publish_ready(*args)
    expected = {"version": "1.2.3", "commit": "f" * 40, "images": images}
    assert json.loads(remote["RELEASE-READY.json"]) == expected
    published = remote.copy()
    assets.publish_ready(*args)
    assert remote == published
    assert [call[2] for call in calls if call[:2] == ["gh", "release"]] == [
        "view", "upload", "view", "download",
    ]
    with pytest.raises(ValueError, match="published asset differs"):
        assets.publish_ready(*args[:3], "0" * 40, images)
    assert remote == published
    assert sum(call[2] == "upload" for call in calls) == 1


@pytest.mark.parametrize("damage, diagnostic", [
    ("tag", "tag differs"),
    ("commit", "commit SHA"),
    ("missing-image", "image digests"),
    ("bad-rust-digest", "image digests"),
    ("unconfigured-image", "image digests"),
])
def test_ready_marker_refuses_invalid_attestation_before_github(monkeypatch, damage, diagnostic):
    tag, commit = "v1.2.3", "f" * 40
    images = {
        "pseudolife-daemon": "sha256:" + "a" * 64,
        "pseudolife-pg": "sha256:" + "b" * 64,
        "pseudolife-daemon-rust": "sha256:" + "c" * 64,
    }
    if damage == "tag":
        tag = "v1.2.4"
    elif damage == "commit":
        commit = "short"
    elif damage == "missing-image":
        images.pop("pseudolife-pg")
    elif damage == "bad-rust-digest":
        images["pseudolife-daemon-rust"] = ""
    else:
        images["unknown"] = "sha256:" + "c" * 64
    calls = fake_release(monkeypatch, {})
    with pytest.raises(ValueError, match=diagnostic):
        assets.publish_ready("1.2.3", tag, "owner/repo", commit, images)
    assert not calls


def test_marker_publisher_refuses_duplicate_remote_markers(monkeypatch):
    images = {
        "pseudolife-daemon": "sha256:" + "a" * 64,
        "pseudolife-pg": "sha256:" + "b" * 64,
        "pseudolife-daemon-rust": "sha256:" + "c" * 64,
    }
    marker = json.dumps({"version": "1.2.3", "commit": "f" * 40, "images": images},
                        sort_keys=True, indent=2).encode("utf-8") + b"\n"
    calls = fake_release(monkeypatch, {"RELEASE-READY.json": marker})

    def view(_args):
        return json.dumps({"assets": [{"name": "RELEASE-READY.json"}] * 2}).encode()

    monkeypatch.setattr(assets.subprocess, "check_output", view)
    with pytest.raises(ValueError, match="duplicate assets"):
        assets.publish_ready("1.2.3", "v1.2.3", "owner/repo", "f" * 40, images)
    assert not calls


def test_ready_marker_refuses_missing_rust_digest_before_github(monkeypatch):
    images = {"pseudolife-daemon": "sha256:" + "a" * 64, "pseudolife-pg": "sha256:" + "b" * 64}
    calls = fake_release(monkeypatch, {})
    with pytest.raises(ValueError, match="image digests"):
        assets.publish_ready("1.2.3", "v1.2.3", "owner/repo", "f" * 40, images)
    assert not calls


@pytest.mark.parametrize("has_rust_digest", [True, False])
def test_ready_marker_cli_requires_the_rust_image_digest(monkeypatch, has_rust_digest):
    import sys

    monkeypatch.setattr(sys, "argv", ["release_assets.py", "publish-ready"])
    monkeypatch.setattr(assets, "version", lambda: "1.2.3")
    for name, value in {
        "GITHUB_EVENT_NAME": "release",
        "GITHUB_REF_NAME": "v1.2.3",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_SHA": "f" * 40,
        "DAEMON_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "PG_IMAGE_DIGEST": "sha256:" + "b" * 64,
    }.items():
        monkeypatch.setenv(name, value)
    remote = {}
    calls = fake_release(monkeypatch, remote)
    if not has_rust_digest:
        monkeypatch.delenv("RUST_IMAGE_DIGEST", raising=False)
        with pytest.raises(ValueError, match="image digests"):
            assets.main()
        assert not calls
    else:
        monkeypatch.setenv("RUST_IMAGE_DIGEST", "sha256:" + "c" * 64)
        assets.main()
        marker = json.loads(remote["RELEASE-READY.json"])
        assert marker["images"]["pseudolife-daemon-rust"] == "sha256:" + "c" * 64


@pytest.mark.parametrize("name", ["SHA256SUMS", assets.asset_name("1.2.3", assets.TARGETS[0])])
def test_publication_refuses_existing_mismatch_before_any_upload(tmp_path, monkeypatch, name):
    out = complete_assets(tmp_path)
    calls = fake_release(monkeypatch, {name: b"different published bytes"})
    with pytest.raises(ValueError, match="published asset differs"):
        assets.publish_assets("1.2.3", out, "v1.2.3", "owner/repo")
    assert not any(call[2] == "upload" for call in calls)


@pytest.mark.parametrize("damage", ["extra", "missing", "corrupt", "duplicate"])
def test_publication_refuses_invalid_local_set_before_contacting_github(tmp_path, monkeypatch, damage):
    out = complete_assets(tmp_path)
    binary = out / assets.asset_name("1.2.3", assets.TARGETS[0])
    if damage == "extra":
        (out / "unlisted.txt").write_bytes(b"extra")
    elif damage == "missing":
        binary.unlink()
    elif damage == "corrupt":
        binary.write_bytes(b"corrupt")
    else:
        sums = out / "SHA256SUMS"
        sums.write_text(sums.read_text() * 2)
    calls = fake_release(monkeypatch, {})
    with pytest.raises(ValueError):
        assets.publish_assets("1.2.3", out, "v1.2.3", "owner/repo")
    assert not calls
