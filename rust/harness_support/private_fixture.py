"""Place private test markers with the same atomic replace as the frozen writer."""
from contextlib import suppress
import os
from pathlib import Path
import tempfile


def _check_directory(directory: Path) -> None:
    absolute = directory.absolute()
    if any(path.is_symlink() for path in (absolute, *absolute.parents)):
        raise OSError("fixture state path has a symbolic-link ancestor")


def write_private(path: Path, body: str) -> None:
    """Write a UTF-8 marker in its disposable home and preserve reader retries."""
    try:
        directory = path.parent
        _check_directory(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_directory(directory)
        if os.name != "nt":
            directory.chmod(0o700)
        fd, name = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=path.suffix)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                if os.name == "nt":
                    from ._windows_fixture_acl import secure_windows_file

                    secure_windows_file(temporary)
                else:
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(body)
            os.replace(temporary, path)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"producer could not write {path}") from error
