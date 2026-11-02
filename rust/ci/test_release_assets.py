"""The archives consumed by native installers have one fixed layout."""
import hashlib
import tarfile
import zipfile

import pytest

import release_assets as assets


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
