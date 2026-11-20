"""Prepare an isolated filesystem and run the fixed acceptance stand-in."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from .matrix import known_archive


TOOLS = frozenset(
    "awk basename bash cat chmod cmd cp curl cut date dirname env find git grep gzip "
    "head id install mkdir mktemp mv openssl powershell pwsh readlink rm sed sh "
    "shasum sha256sum sort stat tar tail tr uname wc".split()
)
WINDOWS_EXTENSIONS = (".exe", ".com", ".bat", ".cmd")
CHECKSUM_ROW = re.compile(r"([0-9a-fA-F]{64}) [ *](\S+)")
STAND_IN = """\
set -eCu
for name in python python3 pip py; do
    if command -v "$name" >/dev/null; then
        exit 2
    fi
done
command -v tar >/dev/null
test -s "$PSEUDOLIFE_NATIVE_ARCHIVE_URL/SHA256SUMS"
printf 'stand-in installed\n' > "$HOME/fixture-installed"
"""


class PreparationError(ValueError):
    """The fixture could not be prepared or safely removed."""


@dataclass(frozen=True)
class CleanupReceipt:
    """Private filesystem inventory. Public records carry only its verdict."""

    before: tuple[str, ...]
    after: tuple[str, ...]
    removed: bool

    @property
    def verified(self) -> bool:
        return self.removed and self.before == self.after


def _inventory(folder: Path) -> tuple[str, ...]:
    return tuple(sorted(entry.name for entry in folder.iterdir()))


def _directory_identity(path: Path) -> tuple[int, int]:
    entry = path.lstat()
    attributes = getattr(entry, "st_file_attributes", 0)
    if not stat.S_ISDIR(entry.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PreparationError("owned directory was replaced by a link or other entry")
    return entry.st_dev, entry.st_ino


def _manifest(source: Path) -> dict[str, str]:
    entries = {}
    try:
        lines = (source / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise PreparationError("read the release's ASCII SHA256SUMS before staging archives") from None
    for line in lines:
        match = CHECKSUM_ROW.fullmatch(line)
        if not match or not known_archive(match[2]):
            raise PreparationError("SHA256SUMS contains an invalid release asset row")
        digest, name = match.groups()
        if name in entries:
            raise PreparationError("SHA256SUMS contains a duplicate asset name")
        entries[name] = digest.lower()
    return entries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DisposableHome:
    """Own one new directory under the caller's fixture scratch directory."""

    def __init__(self, scratch: Path):
        self._parent = scratch.resolve(strict=True)
        if os.pathsep in str(self._parent):
            raise PreparationError("scratch path contains the executable search path separator")
        self._before = _inventory(self._parent)
        self._root = Path(tempfile.mkdtemp(prefix="installed-acceptance-", dir=self._parent))
        self._identity = _directory_identity(self._root)
        self.cleanup_receipt: CleanupReceipt | None = None
        try:
            for folder in (self.profile, self.root / "work", self.root / "tmp"):
                folder.mkdir()
            for folder in (
                ".config", ".cache", ".local/share", ".local/state", ".claude", ".codex",
                "AppData/Local", "AppData/Roaming",
            ):
                (self.profile / folder).mkdir(parents=True, exist_ok=True)
        except OSError:
            self.cleanup(self.root)
            raise

    @property
    def root(self) -> Path:
        return self._root

    @property
    def profile(self) -> Path:
        return self.root / "home"

    @property
    def archives(self) -> Path:
        return self.root / "archives"

    def __enter__(self) -> DisposableHome:
        return self

    def __exit__(self, *_):
        if self.cleanup_receipt is None:
            self.cleanup(self.root)

    def _check_ownership(self) -> None:
        if _directory_identity(self.root) != self._identity:
            raise PreparationError("owned directory was replaced")

    def environment(self) -> dict[str, str]:
        """Build the fixture environment without inheriting host settings."""
        folders = {
            "HOME": self.profile,
            "USERPROFILE": self.profile,
            "APPDATA": self.profile / "AppData/Roaming",
            "LOCALAPPDATA": self.profile / "AppData/Local",
            "CLAUDE_CONFIG_DIR": self.profile / ".claude",
            "CODEX_HOME": self.profile / ".codex",
            "XDG_CONFIG_HOME": self.profile / ".config",
            "XDG_CACHE_HOME": self.profile / ".cache",
            "XDG_DATA_HOME": self.profile / ".local/share",
            "XDG_STATE_HOME": self.profile / ".local/state",
            "TMP": self.root / "tmp",
            "TEMP": self.root / "tmp",
            "TMPDIR": self.root / "tmp",
            "PATH": self.root / "bin",
            "PSEUDOLIFE_NATIVE_ARCHIVE_URL": self.archives,
        }
        return {
            **{name: str(path) for name, path in folders.items()},
            "LC_ALL": "C",
            "PATHEXT": ";".join(WINDOWS_EXTENSIONS),
            "NoDefaultCurrentDirectoryInExePath": "1",
        }

    def prepare_path(self, tools: dict[str, Path]) -> Path:
        """Copy caller-selected tools by allowlisted name into a fresh search path."""
        self._check_ownership()
        if not tools.keys() <= TOOLS:
            raise PreparationError("choose executable names from the fixture tool allowlist")
        path = self.root / "bin"
        with tempfile.TemporaryDirectory(prefix="tools-", dir=self.root) as directory:
            stage = Path(directory)
            for name, source in tools.items():
                suffix = source.suffix.lower() if os.name == "nt" else ""
                if (
                    not source.is_absolute()
                    or not source.is_file()
                    or (os.name == "nt" and suffix not in WINDOWS_EXTENSIONS)
                    or source.name.lower() != name + suffix
                ):
                    raise PreparationError("select an absolute executable with its canonical name")
                shutil.copy2(source, stage / (name + suffix))
            stage.rename(path)
        return path

    def prepare_archives(self, source: Path, names: list[str]) -> Path:
        """Stage selected release assets only after checking the copied bytes."""
        self._check_ownership()
        manifest = _manifest(source)
        if not names or len(set(names)) != len(names) or any(name not in manifest for name in names):
            raise PreparationError("select distinct archive names present in SHA256SUMS")
        with tempfile.TemporaryDirectory(prefix="archives-", dir=self.root) as directory:
            stage = Path(directory)
            for name in names:
                if not (source / name).is_file():
                    raise PreparationError("a selected release archive is missing")
                copied = stage / name
                shutil.copyfile(source / name, copied)
                if _sha256(copied) != manifest[name]:
                    raise PreparationError("release archive checksum mismatch")
            (stage / "SHA256SUMS").write_text(
                "".join(f"{manifest[name]}  {name}\n" for name in names),
                encoding="ascii", newline="\n",
            )
            stage.rename(self.archives)
        return self.archives

    def cleanup(self, path: Path) -> CleanupReceipt:
        """Remove only the directory this object created and check the inventory."""
        if path != self.root or self.cleanup_receipt is not None:
            raise PreparationError("cleanup path is not the currently owned directory")
        self._check_ownership()
        shutil.rmtree(self.root)
        receipt = CleanupReceipt(self._before, _inventory(self._parent), not os.path.lexists(self.root))
        self.cleanup_receipt = receipt
        return receipt


def run_installer(home: DisposableHome) -> int:
    """Installer execution boundary. This slice runs a fixed Bash stand-in."""
    if os.name == "nt":
        raise PreparationError("run the Bash stand-in on a POSIX host")
    home._check_ownership()
    script = home.root / "stand-in.sh"
    with script.open("x", encoding="ascii", newline="\n") as stream:
        stream.write(STAND_IN)
    return subprocess.run(
        [str(home.root / "bin/bash"), "--noprofile", "--norc", str(script)],
        cwd=home.root / "work",
        env=home.environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    ).returncode
