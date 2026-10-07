"""Listener cleanup preserves other live waiters, including renewal races."""

from pathlib import Path
import time

from pseudolife_memory.wake_liveness import WaitListener, armed_until


def test_an_overlapping_waiter_stays_advertised_when_the_other_waiter_exits(tmp_path):
    digest = tmp_path / "digest.txt"
    first = WaitListener(digest, 100)
    second = WaitListener(digest, 100)
    try:
        first.renew()
        second.renew()
        second.close()
        assert first.path.exists()
        assert armed_until(digest) > time.time()
    finally:
        first.close()
        second.close()


def test_cleanup_cannot_unlink_a_waiter_that_renews_at_the_unlink_boundary(tmp_path, monkeypatch):
    digest = tmp_path / "digest.txt"
    first = WaitListener(digest, 100)
    second = WaitListener(digest, 100)
    first.renew()
    original_unlink = Path.unlink
    raced = []

    def unlink(path, *args, **kwargs):
        if path == first.path and not raced:
            raced.append(True)
            second.renew()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        first.close()
        assert raced
        assert second.path.exists()
        assert armed_until(digest) > time.time()
    finally:
        first.close()
        second.close()
