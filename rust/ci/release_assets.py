"""Build and check the native release archives without publishing anything."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
TARGETS = (
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
    "x86_64-apple-darwin", "aarch64-apple-darwin", "x86_64-pc-windows-msvc",
)
COMPOSE_FILES = (
    "docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.rust-daemon.yml",
    "docker-compose.rust-shadow.yml", "docker-compose.rust-ghcr.yml",
)
BUNDLE_FILES = (*COMPOSE_FILES, "Dockerfile.extractor")


def checked_version(version: str) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.-]*)", version):
        raise ValueError("expected a release version without a leading v or path separators")
    return version


def version(root: Path = ROOT) -> str:
    value = checked_version(tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
    for crate in ("shim", "daemon"):
        native = tomllib.loads((root / "rust" / crate / "Cargo.toml").read_text(encoding="utf-8"))["package"]["version"]
        if native != value:
            raise ValueError(f"{crate} version differs from the release")
    return value


def asset_name(value: str, target: str) -> str:
    checked_version(value)
    if target not in TARGETS:
        raise ValueError("unsupported release target")
    suffix = ".zip" if target.endswith("msvc") else ".tar.gz"
    return f"pseudolife-mcp-{value}-{target}{suffix}"


def tar_member(stream: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    member = tarfile.TarInfo(name)
    member.size, member.mode = len(data), mode
    stream.addfile(member, io.BytesIO(data))


@contextmanager
def tar_archive(path: Path):
    # Normalize archive metadata: retries with identical inputs have identical bytes.
    with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as stream:
            yield stream


def package(binary: Path, value: str, target: str, out: Path) -> Path:
    archive = out / asset_name(value, target)
    if binary.is_symlink() or not binary.is_file() or binary.stat().st_size == 0:
        raise ValueError("expected a nonempty regular native binary")
    out.mkdir(parents=True, exist_ok=True)
    if target.endswith("msvc"):
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
            member = zipfile.ZipInfo("pseudolife-mcp.exe", date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            stream.writestr(member, binary.read_bytes())
    else:
        with tar_archive(archive) as stream:
            tar_member(stream, "pseudolife-mcp", binary.read_bytes(), 0o755)
    return archive


def binary_contents(archive: Path, target: str) -> tuple[str, bytes]:
    if target not in TARGETS:
        raise ValueError("unsupported release target")
    if target.endswith("msvc"):
        name = "pseudolife-mcp.exe"
        with zipfile.ZipFile(archive) as stream:
            if stream.namelist() != [name]:
                raise ValueError("unexpected binary archive members")
            data = stream.read(name)
    else:
        name = "pseudolife-mcp"
        with tarfile.open(archive) as stream:
            members = stream.getmembers()
            if len(members) != 1 or members[0].name != name:
                raise ValueError("unexpected binary archive members")
            if not members[0].isfile() or members[0].mode != 0o755:
                raise ValueError("expected a regular executable with mode 0755")
            data = stream.extractfile(members[0]).read()
    if not data:
        raise ValueError("empty binary archive")
    return name, data


def smoke(archive: Path, value: str, target: str) -> None:
    name, data = binary_contents(archive, target)
    with tempfile.TemporaryDirectory(prefix="native-release-") as directory:
        binary = Path(directory) / name
        binary.write_bytes(data)
        binary.chmod(0o755)
        result = subprocess.run([str(binary), "version"], check=True, capture_output=True, text=True, timeout=30)
        if result.stdout.splitlines()[0] != "pseudolife-mcp " + checked_version(value):
            raise ValueError("archive executable reports a different release version")
    print(f"archive executable version verified: {archive.name}")


def assemble(root: Path, value: str, out: Path) -> None:
    expected = {asset_name(value, target) for target in TARGETS}
    if {path.name for path in out.iterdir()} != expected:
        raise ValueError("native asset set must contain exactly one archive for every target")
    for target in TARGETS:
        binary_contents(out / asset_name(value, target), target)
    compose = out / f"pseudolife-compose-{checked_version(value)}.tar.gz"
    with tar_archive(compose) as stream:
        for name in BUNDLE_FILES:
            tar_member(stream, name, (root / "ops" / name).read_bytes(), 0o644)
    names = sorted(expected | {compose.name})
    lines = [hashlib.sha256((out / name).read_bytes()).hexdigest() + "  " + name + "\n" for name in names]
    (out / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")
    print("complete native release asset set: " + ", ".join(names))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("version", "package", "smoke", "assemble"))
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--out", type=Path, default=Path("release-assets"))
    args = parser.parse_args()
    value = version()
    if os.environ.get("GITHUB_EVENT_NAME") == "release" and os.environ.get("GITHUB_REF_NAME") != "v" + value:
        raise ValueError("release tag differs from native package version")
    if args.command == "version":
        print(value)
    elif args.command == "assemble":
        assemble(ROOT, value, args.out)
    else:
        if not args.target or (args.command == "package" and not args.binary):
            parser.error("package needs --binary and --target; smoke needs --target")
        if args.command == "package":
            print(package(args.binary, value, args.target, args.out))
        else:
            smoke(args.out / asset_name(value, args.target), value, args.target)


if __name__ == "__main__":
    main()
