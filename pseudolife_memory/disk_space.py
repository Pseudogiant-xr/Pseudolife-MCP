"""Free-space checks for Docker updates and their backup destinations."""
from pathlib import Path
import shutil
import uuid

from pseudolife_memory import update_cli as up

MIN_FREE = 8 * 1024**3


def require(free: int, location: str) -> None:
    if free < MIN_FREE:
        raise up.UpdateError(f"less than 8 GiB is free on {location}; free at least 8 GiB there, "
                             "then retry. Nothing was changed", 2)


def host(path: Path) -> None:
    target = Path(path).resolve()
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        available = shutil.disk_usage(target).free
    except OSError as exc:
        raise up.UpdateError(f"cannot check free space for {path}; restore access to that filesystem "
                             "and retry before updating or backing up. Nothing was changed", 2) from exc
    require(available, str(path))


def _available(output: str, location: str) -> int:
    try:
        free = int(output.splitlines()[-1].split()[3])
        if free < 0 or free > ((1 << 64) - 1) // 1024:
            raise ValueError("invalid free-space count")
        return free * 1024
    except (ValueError, IndexError) as exc:
        raise up.UpdateError(f"cannot read free space on {location}; make `df -Pk` work in the "
                             "container and retry. Nothing was changed", 2) from exc


def container(docker: str, name: str, path: str = "/tmp") -> None:
    location = f"{name}:{path}"
    code, output = up.run_cli([docker, "exec", name, "df", "-Pk", path], timeout=60, stdout_only=True)
    if code:
        raise up.UpdateError(f"cannot check free space on {location}; run `docker exec {name} df -Pk {path}` "
                             "successfully, then retry. Nothing was changed", 2)
    require(_available(output, location), location)


def docker_storage(docker: str, names, image: str | None = None, warn=lambda _: None) -> None:
    for name in names:
        code, output = up.run_cli([docker, "exec", name, "df", "-Pk", "/"], timeout=60, stdout_only=True)
        if code == 0:
            require(_available(output, f"Docker storage ({name})"), "Docker storage")
            return
    if image is None:
        # A first checkout build may have no existing container or image.
        warn("Docker storage space could not be checked before this first build; the host filesystem "
             "check passed. Keep at least 8 GiB free in Docker's data storage")
        return
    helper = "pseudolife-diskcheck-" + uuid.uuid4().hex
    code, _ = up.run_cli([docker, "create", "--name", helper, "--read-only", "--network", "none", "--entrypoint", "df",
                              image, "-Pk", "/"], timeout=60, stdout_only=True)
    created = code == 0
    try:
        if code:
            raise up.UpdateError("cannot check Docker storage space; restore Docker's ability to create a "
                                 "container from the current daemon image and retry. Nothing was changed", 2)
        code, output = up.run_cli([docker, "start", "-a", helper], timeout=60, stdout_only=True)
        if code:
            raise up.UpdateError("cannot check Docker storage space; make `df -Pk /` work in the current "
                                 "daemon image and retry. Nothing was changed", 2)
        require(_available(output, "Docker storage"), "Docker storage")
    finally:
        cleanup, _ = up.run_cli([docker, "rm", "-f", "-v", helper], timeout=120, stdout_only=True)
        if created and cleanup:
            raise up.UpdateError(f"the disk-check helper could not be removed; run `docker rm -f -v {helper}` "
                                 "and retry. The daemon was not changed", 2)
