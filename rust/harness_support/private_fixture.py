"""Place private test markers with the same atomic replace as the frozen writer."""
from contextlib import suppress
import os
from pathlib import Path
import stat
import tempfile


def _check_directory(root: Path, directory: Path) -> None:
    if not directory.is_relative_to(root):
        raise OSError("fixture state path is outside its home")
    for ancestor in (*reversed(directory.parents), directory):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise OSError("fixture state path has a symbolic-link or reparse-point ancestor")
    if not directory.resolve().is_relative_to(root.resolve()):
        raise OSError("fixture state path is outside its home")


def write_owner_only(path: Path, data: bytes) -> None:
    """Atomically create or replace ``path`` as an owner-only file: mode 0600,
    or on Windows the current user as owner with a protected owner-only
    access list. Missing parents are created with the default mode, as the
    client's own private-file writers leave them, so a fixture written here
    looks like one the client wrote. Unlike ``write_private`` it neither
    tightens nor checks the parents: use it only inside a disposable home."""
    target = Path(os.path.abspath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "nt":
                from ._windows_fixture_acl import secure_windows_file

                secure_windows_file(temporary)
            else:
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
        os.replace(temporary, target)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def write_private(path: Path, body: str, *, root: Path) -> None:
    """Write a UTF-8 marker in its disposable home and preserve reader retries."""
    try:
        home, target = root.absolute(), path.absolute()
        if ".." in path.parts or ".." in root.parts or target == home:
            raise OSError("fixture state path is outside its home")
        directory = target.parent
        _check_directory(home, directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_directory(home, directory)
        if os.name != "nt":
            directory.chmod(0o700)
        fd, name = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=target.suffix)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                if os.name == "nt":
                    from ._windows_fixture_acl import secure_windows_file

                    secure_windows_file(temporary)
                else:
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(body)
            os.replace(temporary, target)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"producer could not write {path}") from error
