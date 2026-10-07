"""Client-side Git identity and portable, exact repository file claims.

The daemon never opens a caller's checkout. A shim prepares the repository
identity and path locally; the existing lease queue owns the resulting key.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import subprocess
import unicodedata


class FileClaimError(ValueError):
    """A stable refusal that never contains local paths or Git stderr."""


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value.casefold())


def normalize_claim_path(path: str, *, case_sensitive: bool = True) -> str:
    """Normalize separators and dots; refuse traversal and non-file syntax.

    Both separators are accepted on every host. The portable subset excludes
    Windows device/stream names, trailing dots/spaces, globs and Git metadata.
    Missing files are valid: deletions and both sides of a rename need claims.
    """
    if (not isinstance(path, str) or not path or len(path) > 4096
            or any(unicodedata.category(c)[0] == "C" for c in path)
            or any(c in path for c in '*?[]{}<>|":')
            or ntpath.splitdrive(path)[0]):
        raise FileClaimError("invalid_claim_path")
    path = path.replace("\\", "/")
    if path.startswith("/") or path.endswith("/"):
        raise FileClaimError("invalid_claim_path")
    parts = [part for part in path.split("/") if part not in {"", "."}]
    devices = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
               *(f"lpt{i}" for i in range(1, 10)), "com¹", "com²", "com³",
               "lpt¹", "lpt²", "lpt³"}
    if not parts or any(part == ".." or part.endswith((".", " "))
                        or _fold(part) == ".git"
                        or _fold(part.split(".")[0].rstrip(" ")) in devices for part in parts):
        raise FileClaimError("invalid_claim_path")
    normalized = unicodedata.normalize("NFC", "/".join(parts))
    return normalized if case_sensitive else _fold(normalized)


def file_claim_name(repository_id: str, path: str) -> str:
    """Bounded, opaque lease name; unrelated clones never share an identity."""
    if not isinstance(repository_id, str) or not re.fullmatch("[0-9a-f]{64}", repository_id):
        raise FileClaimError("invalid_repository_id")
    if normalize_claim_path(path) != path:
        raise FileClaimError("invalid_claim_path")
    payload = json.dumps([repository_id, path], ensure_ascii=True, separators=(",", ":"))
    return "claim:file:" + hashlib.sha256(payload.encode("ascii")).hexdigest()


def _git(worktree: str, *args: str, missing_ok: bool = False) -> str:
    # An inherited GIT_DIR/WORK_TREE/config override must not redirect the
    # caller's explicit checkout. Git stderr may contain private host paths.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    try:
        result = subprocess.run(["git", "-C", worktree, *args], env=env,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="strict", timeout=5, check=False)
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        raise FileClaimError("invalid_repository") from None
    if result.returncode and not (missing_ok and result.returncode == 1):
        raise FileClaimError("invalid_repository")
    return result.stdout.rstrip("\r\n")


def _check_file(root: Path, path: str, *, case_sensitive: bool) -> None:
    current = root
    parts = path.split("/")
    for index, part in enumerate(parts):
        try:
            if current.is_dir():
                # Also covers a POSIX checkout with core.ignorecase=true and
                # a Windows directory with per-directory case sensitivity.
                def spelling(value):
                    value = unicodedata.normalize("NFC", value)
                    return value if case_sensitive else _fold(value)
                with os.scandir(current) as entries:
                    matches = [e.name for e in entries if spelling(e.name) == spelling(part)]
                if len(matches) > 1:
                    raise FileClaimError("invalid_claim_path")
                part = matches[0] if matches else part
            current = current / part
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise FileClaimError("invalid_claim_path") from None
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            # Reject aliases, including junctions, even when the target is
            # inside the repository. A changed target cannot retarget a claim.
            raise FileClaimError("invalid_claim_path")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise FileClaimError("invalid_claim_path")
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise FileClaimError("invalid_claim_path")
    if current.is_dir():
        raise FileClaimError("invalid_claim_path")


def prepare_file_claim(worktree: str, path: str) -> dict[str, str]:
    """Use the resolved Git common directory to unite linked worktrees.

    Windows folds case conservatively, including case-sensitive NTFS
    directories. POSIX preserves case unless the common Git config explicitly
    sets core.ignorecase. This shared policy ignores worktree-local overrides.
    Claims describe path names, not inodes; hard links remain separate names.
    """
    if (not isinstance(worktree, str) or not os.path.isabs(worktree)
            or len(worktree) > 4096 or any(unicodedata.category(c)[0] == "C" for c in worktree)):
        raise FileClaimError("invalid_repository")
    # Validate syntax before consulting the filesystem, including .. inside
    # a lexically contained path. resolve() would otherwise erase traversal.
    normalized = normalize_claim_path(path)
    # DOS short-name aliases disappear on deletion and cannot be normalized
    # consistently for missing paths. Refuse their syntax on Windows.
    if os.name == "nt" and re.search(r"~[0-9]", normalized):
        raise FileClaimError("invalid_claim_path")
    lines = _git(worktree, "rev-parse", "--path-format=absolute", "--show-toplevel",
                 "--git-common-dir", "--git-dir").splitlines()
    if len(lines) != 3:
        raise FileClaimError("invalid_repository")
    root, common, git_dir = (Path(os.path.realpath(p)) for p in lines)
    ignorecase = _git(worktree, "config", "--no-includes", "--file", str(common / "config"),
                      "--type=bool", "--get", "core.ignorecase", missing_ok=True)
    insensitive = os.name == "nt" or ignorecase == "true"
    # Git administration may have an ordinary directory name when created
    # with --separate-git-dir. Refuse its entire in-checkout subtree, including
    # descendants that do not exist yet, under the shared path spelling policy.
    canonical = normalize_claim_path(normalized, case_sensitive=not insensitive)
    for metadata in (common, git_dir):
        try:
            relative = metadata.relative_to(root).as_posix()
        except ValueError:
            continue
        relative = unicodedata.normalize("NFC", relative)
        relative = relative if not insensitive else _fold(relative)
        if relative == "." or canonical == relative or canonical.startswith(relative + "/"):
            raise FileClaimError("invalid_claim_path")
    _check_file(root, normalized, case_sensitive=not insensitive)
    try:
        directory = common.stat()
    except OSError:
        raise FileClaimError("invalid_repository") from None
    if not stat.S_ISDIR(directory.st_mode) or not directory.st_ino:
        raise FileClaimError("invalid_repository")
    # The resolved directory name partitions filesystem views; its file ID
    # keeps distinct casefold-equivalent repository names separate. Linked
    # worktrees resolve and stat the same common directory.
    identity = json.dumps([os.path.normcase(str(common)), directory.st_dev, directory.st_ino],
                          ensure_ascii=True, separators=(",", ":"))
    return {"repository_id": hashlib.sha256(identity.encode("ascii")).hexdigest(),
            "path": normalize_claim_path(normalized, case_sensitive=not insensitive)}


def prepare_claim_arguments(name: str, arguments: dict) -> dict:
    """Consume local tool inputs before forwarding to a remote daemon."""
    if name != "memory_agents" or arguments.get("worktree") is None:
        return arguments
    if (arguments.get("action") not in {"claim", "release"}
            or arguments.get("lease") is not None or arguments.get("repository_id") is not None):
        raise FileClaimError("unexpected_parameter")
    claim = prepare_file_claim(arguments["worktree"], arguments.get("path"))
    return {**{k: v for k, v in arguments.items() if k != "worktree"}, **claim}
