"""Bounded process-exit observations for disposable harness children."""
import psutil


def assert_child_stopped(pid_file, message="owned descendant survived"):
    assert pid_file.exists(), "toy child did not start"
    try:
        child = psutil.Process(int(pid_file.read_text()))
    except psutil.NoSuchProcess:
        return
    try:
        child.wait(timeout=2)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass
    try:
        alive = child.is_running() and child.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        alive = False
    try:
        assert not alive, message
    finally:
        # A watched RED must not leave its synthetic child behind.
        if alive:
            try:
                child.kill()
                child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
