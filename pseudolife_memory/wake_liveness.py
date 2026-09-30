"""Short local listener leases, authenticated to the board by the adapter."""

from __future__ import annotations

from contextlib import suppress
import math
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

from .private_state import _private_fd

LEASE_SECONDS = 60.0


def armed_until(digest: Path | None) -> float:
    """Expired, malformed or non-regular listener records never arm a ring."""
    if digest is None:
        return 0.0
    now = time.time()
    latest = 0.0
    legacy = (digest.with_suffix(".wake-armed"), digest.with_suffix(".wait-armed"))
    try:
        paths = (*legacy, *digest.parent.glob(f"{digest.stem}.*.wake-armed"),
                 *digest.parent.glob(f"{digest.stem}.*.wait-armed"))
    except OSError:
        paths = legacy
    for path in paths:
        try:
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256:
                continue
            lines = path.read_text(encoding="ascii").splitlines()
            if len(lines) != 2 or not lines[0]:
                continue
            if path not in legacy and path.name != f"{digest.stem}.{lines[0]}{path.suffix}":
                continue
            if path.suffix == ".wake-armed":
                owner = digest.with_suffix(".wake")
                if owner.is_symlink() or owner.read_text(encoding="ascii").strip() != lines[0]:
                    continue
            expiry = float(lines[1])
            if math.isfinite(expiry) and now < expiry <= now + LEASE_SECONDS:
                latest = max(latest, expiry)
        except (OSError, ValueError, UnicodeError):
            pass
    return latest


class WaitListener:
    """A waiter renews its own record; losing it expires within one minute."""

    def __init__(self, digest: Path, timeout: float):
        self.token = uuid.uuid4().hex
        self.path = digest.with_name(f"{digest.stem}.{self.token}.wait-armed")
        self.deadline = time.monotonic() + timeout

    def renew(self) -> None:
        expiry = time.time() + min(LEASE_SECONDS, max(0.0, self.deadline - time.monotonic()))
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-", suffix=".wait-armed")
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
                _private_fd(handle.fileno(), Path(temporary), RuntimeError)
                handle.write(f"{self.token}\n{expiry}\n")
            os.replace(temporary, self.path)
        except (OSError, RuntimeError):
            # Delivery still works if liveness cannot be advertised.
            pass
        finally:
            if temporary is not None:
                with suppress(OSError):
                    os.unlink(temporary)

    def close(self) -> None:
        # The pathname belongs only to this listener; another waiter's renew
        # can never replace it between a token check and an unlink.
        with suppress(OSError):
            self.path.unlink()
