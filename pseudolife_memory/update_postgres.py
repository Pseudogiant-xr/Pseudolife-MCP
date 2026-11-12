"""Bring the stack's PostgreSQL container in line with this release's Compose
definition during a release-mode ``pseudolife-mcp update``.

The daemon recreate is ``up -d --no-deps <daemon>``, so a change to the
PostgreSQL service (0.23.1: the ``postgres`` account, no capabilities,
``no-new-privileges``, the binary healthcheck) never reached a host that
only updates. This step does, in two halves:

* :func:`plan` is read-only and runs before anything changes. It decides
  whether the container is this update's to touch (the daemon's Compose
  project, the ``pseudolife-pg`` service, created from the installer's
  packaged compose files whose originals are still on disk), whether it
  differs (Compose's own ``com.docker.compose.config-hash`` label against
  ``docker compose config --hash`` for the service under the release's
  files), whether the files it was created from still render the
  definition it runs (the same hash over its labelled files, so a restore
  brings back what was running), and whether the new definition can start
  on the existing data: the same volume, or the same bind directory as its
  own files render, at the same mount, the same data directory (PGDATA)
  when the definition sets one, and a data directory owned by the account
  the definition runs PostgreSQL as. A host already in line, or one whose
  container is not this update's, gets one line and ``None``.
* :func:`pull` runs before the backup, beside the daemon image pull (a pull
  changes nothing on the host, and an unattended run's board re-check after
  the backup must be the last thing before the recreate): pull the
  release's image and, when the definition leaves PGDATA to the image,
  refuse an image whose default differs from the running cluster's.
* :func:`apply` runs after the backup: tag the running image for rollback,
  write an overlay that restores it, then ``docker compose ... up -d
  --no-deps --no-build pseudolife-pg`` and wait until the new container is
  healthy on the same cluster: the system identifier in the control file
  of the data directory the new container itself names (its PGDATA), not
  the directory the old one used. On any failure after the recreate started
  it recreates the previous container from its own files and the rollback
  tag, waits for it to be healthy, and fails the update with the cause.

Nothing here runs ``down``, removes a volume or changes ownership.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from pseudolife_memory import __version__
from pseudolife_memory import update_cli as up

PG_SERVICE = "pseudolife-pg"
PG_IMAGE = "ghcr.io/pseudogiant-xr/pseudolife-pg"
# The files the installers and the bundled fallback create the stack from.
PACKAGED_FILES = ("docker-compose.yml", "docker-compose.ghcr.yml")
# The PostgreSQL healthcheck is every 10 s with 5 retries; a start on an
# existing cluster takes seconds. 90 polls 2 s apart outlast a failing
# healthcheck's whole retry budget with room to spare.
HEALTH_POLLS = 90
HEALTH_POLL_SECONDS = 2.0
_IDENTITY = ('exec "/usr/lib/postgresql/$PG_MAJOR/bin/pg_controldata" "$1" '
             '| sed -n "s/^Database system identifier: *//p"')


@dataclass
class Plan:
    container_id: str
    image_id: str
    image: str                      # the release definition's image reference
    user: str
    pgdata: str
    identity: str
    release_args: list[str]
    previous_args: list[str]
    environment: dict
    # The definition leaves PGDATA to the image: :func:`pull` reads the
    # pulled image's default before anything changes.
    pgdata_from_image: bool = False
    rollback_tag: str = ""
    rollback_overlay: Path | None = None


def _docker(update, *argv, timeout: int = 120, env: dict | None = None, stream: bool = False) -> tuple[int, str]:
    return up.run_cli([update.docker, *argv], timeout=timeout, env=env, stream=stream)


def _packaged_dir(directory: Path) -> bool:
    """The installers' ``<clone>/ops`` or this updater's own compose copies."""
    cached = up.data_dir() / "compose"
    if directory == cached or directory.parent == cached:
        return True
    return directory.name == "ops" and (directory.parent / "pyproject.toml").is_file()


def _release_files() -> list[Path]:
    """This package's compose base and GHCR overlay, in a directory named by
    its version and content. A file a container was created from is never
    rewritten, so restoring that container's definition later reads what it
    was created with (a version-named directory alone was overwritten by a
    rebuilt package of the same version, and the restore then re-applied the
    failing definition)."""
    contents = [(name, (up.BUNDLED_COMPOSE / name).read_bytes()) for name in PACKAGED_FILES]
    digest = hashlib.sha256(b"\0".join(body for _name, body in contents)).hexdigest()[:12]
    directory = up.data_dir() / "compose" / f"{__version__}-{digest}"
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for name, body in contents:
        target = directory / name
        if not target.is_file() or target.read_bytes() != body:
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)
        files.append(target)
    return files


def _args(project: str, working_dir: str | None, env_files, files) -> list[str]:
    args = ["-p", project]
    if working_dir:
        # Relative paths in a kept override keep their original meaning.
        args += ["--project-directory", working_dir]
    for path in env_files:
        args += ["--env-file", str(path)]
    for path in files:
        args += ["-f", str(path)]
    return args


def _leave(update, why: str) -> None:
    update.step(f"PostgreSQL container left as it is: {why}")


def plan(update, project: str, env_files, environment: dict, target: str) -> Plan | None:
    """Read-only. ``None`` with one step line when there is nothing to do or
    the container is not this update's; a :class:`Plan` when it differs and
    the new definition can start on the existing data; ``UpdateError``
    (nothing changed) when it differs and cannot."""
    from pseudolife_memory.update_engine import transient_overlay
    if up.version_key(target) and up.version_key(__version__) and up.version_key(target) < up.version_key(__version__):
        _leave(update, f"this updater ({__version__}) is newer than {target}, so its PostgreSQL definition "
                       f"is not {target}'s")
        return None
    code, out = _docker(update, "inspect", "-f", "{{json .}}", up.PG_CONTAINER, timeout=60)
    if code != 0:
        if "no such" in out.lower():
            _leave(update, f"there is no {up.PG_CONTAINER} container here (an external database is not this "
                           f"update's to change)")
        else:
            _leave(update, f"docker could not inspect {up.PG_CONTAINER} ({up._last_line(out)}); rerun "
                           f"pseudolife-mcp update once it answers")
        return None
    try:
        running = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        running = None
    if not isinstance(running, dict):
        _leave(update, f"docker's description of {up.PG_CONTAINER} could not be read")
        return None
    config = running.get("Config") or {}
    labels = config.get("Labels") or {}
    if not labels.get("com.docker.compose.project"):
        _leave(update, "it was not created by Docker Compose, so it has no definition this update can apply")
        return None
    if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.service") != PG_SERVICE:
        _leave(update, f"it belongs to Compose project {labels.get('com.docker.compose.project')!r} service "
                       f"{labels.get('com.docker.compose.service')!r}, not this stack's {project!r} {PG_SERVICE!r}")
        return None
    labelled = [Path(p) for p in (labels.get("com.docker.compose.project.config_files") or "").split(",") if p]
    original = [p for p in labelled if not transient_overlay(p) and not p.name.startswith("docker-compose.rollback-pg")]
    base = next((p for p in original if p.name == "docker-compose.yml"), None)
    if base is None or not _packaged_dir(base.parent):
        _leave(update, "it was created from a stack file this update does not manage "
                       f"({', '.join(map(str, labelled)) or 'none labelled'}); its stack owns its definition "
                       "(a stack manager: redeploy the stack with this release's ops/docker-compose.stack.yml)")
        return None
    missing = [p for p in labelled if not p.is_file()]
    if missing:
        _leave(update, f"its compose files are no longer on this host ({', '.join(map(str, missing))}), so its "
                       "current definition could not be restored if the new one failed; restore them there and "
                       "rerun pseudolife-mcp update")
        return None
    current_hash = labels.get("com.docker.compose.config-hash")
    working_dir = labels.get("com.docker.compose.project.working_dir")
    kept = [p for p in original if not (p.name in PACKAGED_FILES and _packaged_dir(p.parent))]
    release = _release_files()
    release_args = _args(project, working_dir, env_files, [release[0], *kept, release[1]])
    previous_args = _args(project, working_dir, env_files, labelled)
    code, out = up.run_cli([update.docker, "compose", *release_args, "config", "--hash", PG_SERVICE],
                           timeout=60, env=environment, stdout_only=True)
    found = re.findall(rf"^{re.escape(PG_SERVICE)}\s+([0-9a-f]{{16,}})\s*$", out or "", re.M) if code == 0 else []
    if not found or not current_hash:
        # Compose errors can carry interpolated credentials: never print them.
        _leave(update, "Docker Compose could not compute its definition's hash (docker compose config --hash "
                       f"{PG_SERVICE}); check the stack's env file, then rerun pseudolife-mcp update")
        return None
    if found[-1] == current_hash:
        update.step("PostgreSQL container already matches this release's definition; nothing to change")
        return None
    if update.o.no_backup:
        _leave(update, "it differs from this release's definition, but this run takes no backup (--no-backup) "
                       "and PostgreSQL is recreated only after one; rerun pseudolife-mcp update without "
                       "--no-backup to apply it")
        return None
    # A restore recreates from the labelled files: they must still render the
    # definition the container runs (a clone edited by hand, or an env file
    # changed, since it was created would restore something else).
    code, out = up.run_cli([update.docker, "compose", *previous_args, "config", "--hash", PG_SERVICE],
                           timeout=60, env=environment, stdout_only=True)
    found = re.findall(rf"^{re.escape(PG_SERVICE)}\s+([0-9a-f]{{16,}})\s*$", out or "", re.M) if code == 0 else []
    if not found or found[-1] != current_hash:
        _leave(update, "the compose files it was created from no longer render the definition it was created "
                       "with (their docker compose config --hash differs from its com.docker.compose.config-hash "
                       "label: they or the env file changed since, or this Docker Compose hashes differently from "
                       "the one that created it), so a restore could not bring back the container that is running. "
                       "Recreate it once from its current files ("
                       + _compose_line(previous_args, f"up -d --no-deps --no-build {PG_SERVICE}")
                       + "), or put back the files it was created from, then rerun pseudolife-mcp update")
        return None
    rendered, service = _render(update, release_args, environment, "this release's")
    previous, _previous_service = _render(update, previous_args, environment, "the running container's")
    image = service.get("image")
    if not image:
        raise up.UpdateError(f"this release's {PG_SERVICE} definition names no image (docker compose config); "
                             "check the stack's override files, then rerun. Nothing was changed", 2)
    _check_mounts(running, rendered, service, previous)
    pgdata = _pgdata(config.get("Env"))
    if not pgdata:
        raise up.UpdateError(f"{up.PG_CONTAINER} does not name its data directory (PGDATA); this update cannot "
                             "check the new definition can start on it. Nothing was changed", 2)
    environment_set = service.get("environment") if isinstance(service.get("environment"), dict) else {}
    wanted = environment_set.get("PGDATA")
    if wanted:
        _check_pgdata(str(wanted), pgdata, "set by its definition")
    user = str(service.get("user") or "")
    account = user.split(":", 1)[0]
    if account and account not in ("0", "root"):
        code, owner = _docker(update, "exec", up.PG_CONTAINER, "stat", "-c", "%u", pgdata, timeout=60)
        owner = owner.strip().splitlines()[-1].strip() if code == 0 and owner.strip() else ""
        if not owner.isdigit():
            raise up.UpdateError(f"cannot read who owns PostgreSQL's data directory {pgdata} in {up.PG_CONTAINER}; "
                                 "start the container and rerun pseudolife-mcp update. Nothing was changed", 2)
        if account.isdigit() and owner != account:
            raise up.UpdateError(
                f"PostgreSQL's data directory {pgdata} is owned by uid {owner}, but this release runs PostgreSQL as "
                f"uid {account}" + (" (the image's postgres account)" if account == "999" else "")
                + ", which could not start on it. Nothing was changed. "
                f"Make the data directory owned by uid {account} (docker exec -u 0 {up.PG_CONTAINER} chown -R "
                f"{user} {pgdata}), then rerun pseudolife-mcp update", 2)
    identity = _identity(update, pgdata)
    if not identity:
        raise up.UpdateError(f"cannot read the PostgreSQL cluster's identity in {up.PG_CONTAINER} (pg_controldata), "
                             "which this update compares after the recreate; start the container and rerun "
                             "pseudolife-mcp update. Nothing was changed", 2)
    return Plan(container_id=str(running.get("Id") or ""), image_id=str(running.get("Image") or ""), image=image,
                user=user, pgdata=pgdata, identity=identity, release_args=release_args,
                previous_args=previous_args, environment=environment, pgdata_from_image=not wanted)


def _render(update, args: list[str], environment: dict, whose: str) -> tuple[dict, dict]:
    code, out = up.run_cli([update.docker, "compose", *args, "config", "--format", "json"],
                           timeout=60, env=environment, stdout_only=True)
    try:
        rendered = json.loads(out) if code == 0 else None
        service = rendered["services"][PG_SERVICE]
        if not isinstance(service, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        # Compose errors can carry interpolated credentials: never print them.
        raise up.UpdateError(f"cannot render {whose} {PG_SERVICE} definition (docker compose config); check "
                             "the stack's env file and override files, then rerun. Nothing was changed", 2) from None
    return rendered, service


def _pgdata(env) -> str:
    return next((e.split("=", 1)[1] for e in env or [] if isinstance(e, str) and e.startswith("PGDATA=")), "")


def _check_pgdata(wanted: str, pgdata: str, source: str) -> None:
    """PostgreSQL initialises an empty cluster in a data directory that has
    none, inside the same volume, while the old directory keeps the old
    cluster: a different PGDATA is refused before anything changes."""
    if wanted.rstrip("/") != pgdata.rstrip("/"):
        raise up.UpdateError(
            f"this release's {PG_SERVICE} definition runs PostgreSQL on the data directory {wanted} (PGDATA, "
            f"{source}), but the bank's cluster is in {pgdata}; recreating it would start PostgreSQL on a new, "
            f"empty cluster. Nothing was changed. Set PGDATA: {pgdata} for {PG_SERVICE} in the stack's override "
            "file (or remove the override that moves it), then rerun pseudolife-mcp update", 2)


def _mounts(rendered: dict, service: dict) -> dict:
    """``{target: (type, source)}`` as Compose renders the service, a named
    volume by its real name."""
    names = {key: (value or {}).get("name") or key for key, value in (rendered.get("volumes") or {}).items()}
    planned = {}
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict) and volume.get("target"):
            source = volume.get("source")
            planned[volume["target"]] = (volume.get("type"), names.get(source, source) if volume.get("type") == "volume"
                                         else source)
    return planned


def _check_mounts(running: dict, rendered: dict, service: dict, previous: dict) -> None:
    """Every persistent mount the running container has must come back from
    the same source at the same place: a changed volume name, or a bind from
    another host directory (a copied cluster keeps its system identifier, so
    the check after the recreate cannot tell), would start a healthy
    PostgreSQL on other (or empty) data.

    A volume is compared by name, which inspect reports as Compose created
    it. A bind is compared Compose against Compose: the release's rendering
    against the rendering of the files the container was created from, which
    :func:`plan` has checked still hash to its label. Inspect's
    ``Mounts[].Source`` is never compared: Docker Desktop can report it
    translated (``/run/desktop/mnt/host/c/...``), not as Compose spelled it."""
    planned = _mounts(rendered, service)
    before = _mounts(previous, (previous.get("services") or {}).get(PG_SERVICE) or {})
    for mount in running.get("Mounts") or []:
        kind, target = mount.get("Type"), mount.get("Destination")
        if kind == "volume":
            source = mount.get("Name")
        elif kind == "bind":
            source = mount.get("Source")
        else:
            continue
        if kind == "volume" and re.fullmatch(r"[0-9a-f]{64}", source or "") and target not in planned:
            continue        # an image VOLUME the named mount above it covers
        want = planned.get(target)
        had = before.get(target)
        if kind == "bind" and had is not None and had[0] == "bind":
            source = had[1]     # the running bind as Compose spells it
        if (want is None or want[0] != kind or (kind == "volume" and want[1] != source)
                or (kind == "bind" and (had is None or had[0] != "bind" or want[1] != had[1]))):
            got = f"{want[0]} {want[1]}" if want else "nothing"
            fix = ("Check PSEUDOLIFE_BANK_VOLUME in the stack's env file (it must name the bank volume)"
                   if kind == "volume" else
                   f"Check the host directory the stack's override files bind at {target} (it must be the one the "
                   "running container uses)")
            raise up.UpdateError(
                f"this release's {PG_SERVICE} definition mounts {got} at {target}, where the running container has "
                f"{kind} {source}; recreating it would start PostgreSQL on other data. Nothing was changed. "
                f"{fix}, then rerun", 2)


def _identity(update, pgdata: str) -> str:
    code, out = _docker(update, "exec", up.PG_CONTAINER, "sh", "-c", _IDENTITY, "sh", pgdata, timeout=60)
    value = (out or "").strip().splitlines()[-1].strip() if code == 0 and (out or "").strip() else ""
    return value if value.isdigit() else ""


def _state(update) -> tuple[str, str, str]:
    code, out = _docker(update, "inspect", "-f",
                        "{{.Id}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                        up.PG_CONTAINER, timeout=60)
    parts = (out.strip().splitlines()[-1].split("|") + ["", "", ""])[:3] if code == 0 and out.strip() else ["", "", ""]
    return parts[0], parts[1], parts[2]


def _wait_healthy(update, previous_id: str) -> str:
    """'' once a new container is running and healthy, else why not."""
    last = "it never started"
    for _ in range(HEALTH_POLLS):
        container, status, health = _state(update)
        if container and container != previous_id:
            if status in ("exited", "dead"):
                return f"it stopped ({status})"
            if status == "running" and health == "healthy":
                return ""
            if status == "running" and not health:
                code, _ = _docker(update, "exec", up.PG_CONTAINER, "pg_isready", "-q", timeout=30)
                if code == 0:
                    return ""
            if health == "unhealthy":
                return "its healthcheck reports unhealthy"
            last = f"it is {status or 'unknown'}" + (f" and {health}" if health else "")
        up.sleep(HEALTH_POLL_SECONDS)
    return f"it was not healthy after {int(HEALTH_POLLS * HEALTH_POLL_SECONDS)} s ({last})"


def _compose_line(args: list[str], tail: str) -> str:
    shown = " ".join(f'"{a}"' if " " in a else a for a in args)
    return f"docker compose {shown} {tail}"


def pull(update, p: Plan) -> None:
    """Before the backup, beside the daemon image pull: nothing on the host
    changes, so a failure or refusal here leaves everything as it was, and
    nothing slow sits between an unattended run's board re-check (after the
    backup) and the recreate. When the definition leaves PGDATA to the image,
    the pulled image's default is compared with the running cluster's; an
    image whose environment cannot be read leaves that to the check after
    the recreate, which reads the new container's own PGDATA."""
    update.step(f"pulling {p.image} (this release's PostgreSQL image)...")
    code, out = _docker(update, "pull", p.image, timeout=7200, stream=not update.o.as_json)
    if code != 0:
        raise up.UpdateError(f"docker pull {p.image} failed" + (f" ({up._last_line(out)})" if out.strip() else "")
                             + "; nothing was changed. Check this host reaches ghcr.io, then rerun "
                               "pseudolife-mcp update")
    if not p.pgdata_from_image:
        return
    code, out = _docker(update, "image", "inspect", "-f", "{{json .Config.Env}}", p.image, timeout=60)
    try:
        env = json.loads(out.strip().splitlines()[-1]) if code == 0 and out.strip() else []
    except ValueError:
        env = []
    default = _pgdata(env if isinstance(env, list) else [])
    if default:
        _check_pgdata(default, p.pgdata, f"{p.image}'s default")


def _verify_cluster(update, p: Plan) -> str:
    """'' when the container now running serves the bank's cluster: the
    identifier in the control file of the data directory it names itself,
    since a different PGDATA in the same volume starts an empty cluster
    while the old directory still holds the old identifier."""
    code, out = _docker(update, "inspect", "-f", "{{json .}}", up.PG_CONTAINER, timeout=60)
    try:
        now = json.loads(out.strip().splitlines()[-1]) if code == 0 and out.strip() else {}
    except ValueError:
        now = {}
    pgdata = _pgdata(((now if isinstance(now, dict) else {}).get("Config") or {}).get("Env"))
    if not pgdata:
        return "its data directory (PGDATA) could not be read"
    if pgdata.rstrip("/") != p.pgdata.rstrip("/"):
        return f"it runs PostgreSQL on {pgdata}, not {p.pgdata} where the bank's cluster is"
    if _identity(update, pgdata) != p.identity:
        return "it came up on a different cluster than the one it replaced"
    return ""


def apply(update, p: Plan) -> None:
    """After the backup (and :func:`pull` before it). Raises ``UpdateError``
    with the cause; PostgreSQL is left running on its previous container
    unless the restore failed too, which the error then says with the
    command that restores it."""
    container, status, _health = _state(update)
    if container != p.container_id or status != "running":
        raise up.UpdateError(f"{up.PG_CONTAINER} changed during the backup (it is now {status or 'gone'}); "
                             "rerun pseudolife-mcp update. Nothing was deployed")
    repository, _, tag = p.image.rpartition(":")
    if not repository or "/" in tag:
        repository, tag = p.image, "latest"
    suffix = update.o.rollback_suffix or f"pre-update-{up._stamp()}"
    p.rollback_tag = f"{repository}:{tag}-{suffix}"
    code, prior = _docker(update, "image", "inspect", "-f", "{{.Id}}", p.rollback_tag, timeout=60)
    if code == 0 and prior.strip() and prior.strip() != p.image_id and not update.o.force_rollback_tag:
        raise up.UpdateError(f"the rollback tag {p.rollback_tag} already names another image; choose a new "
                             "--rollback-tag suffix or pass --force-rollback-tag. Nothing was changed", 2)
    if _docker(update, "tag", p.image_id, p.rollback_tag, timeout=60)[0]:
        raise up.UpdateError(f"could not tag the running PostgreSQL image as {p.rollback_tag} for rollback; "
                             "nothing was changed. Check docker image ls, then rerun pseudolife-mcp update")
    directory = up.data_dir() / "update-engine"
    p.rollback_overlay = directory / f"docker-compose.rollback-pg-{up._stamp()}-{secrets.token_hex(8)}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = p.rollback_overlay.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"services": {PG_SERVICE: {"image": p.rollback_tag}}}, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(p.rollback_overlay)
    except OSError as exc:
        raise up.UpdateError(f"cannot write the PostgreSQL rollback overlay in {directory} "
                             f"({exc.strerror or exc}); restore write access and rerun. Nothing was changed") from exc
    restore_args = [*p.previous_args, "-f", str(p.rollback_overlay)]
    restore = _compose_line(restore_args, f"up -d --no-deps --no-build --force-recreate {PG_SERVICE}")
    update.step(f"tagged the PostgreSQL rollback image: {p.rollback_tag}")
    update.step(f"recreating the PostgreSQL container on {p.image}"
                + (f" as {p.user}" if p.user else "") + " (same bank volume; the daemon waits)...")
    code, out = _docker(update, "compose", *p.release_args, "up", "-d", "--no-deps", "--no-build", PG_SERVICE,
                        timeout=1800, env=p.environment, stream=not update.o.as_json)
    why = (f"docker compose up failed" + (f" ({up._last_line(out)})" if out.strip() else "")) if code else ""
    if not why:
        why = _wait_healthy(update, p.container_id)
    if not why:
        why = _verify_cluster(update, p)
    if not why:
        update.step("PostgreSQL is healthy on this release's definition (same cluster)")
        # Added to the daemon's rollback text once the daemon step writes it.
        update.postgres_rollback_lines = ["      PostgreSQL's previous container, if it is wanted back too:",
                                          f"      {restore}"]
        return
    _code, logs = _docker(update, "logs", "--tail", "5", up.PG_CONTAINER, timeout=60)
    detail = up._last_line(logs) if (logs or "").strip() else ""
    update.warn(f"the new PostgreSQL container did not come up: {why}" + (f" (last log line: {detail})" if detail else "")
                + "; restoring the previous one...")
    _new_id, _status, _health = _state(update)
    code, out = _docker(update, "compose", *restore_args, "up", "-d", "--no-deps", "--no-build", "--force-recreate",
                        PG_SERVICE, timeout=1800, env=p.environment, stream=not update.o.as_json)
    back = (f"docker compose up failed" + (f" ({up._last_line(out)})" if out.strip() else "")) if code else ""
    if not back:
        back = _wait_healthy(update, _new_id)
    if not back:
        back = _verify_cluster(update, p)
    if back:
        raise up.UpdateError(
            f"PostgreSQL did not start on this release's definition ({why}), and restoring its previous container "
            f"failed too ({back}). The bank volume is unchanged and the backup above holds it. Bring the previous "
            f"container back with:\n      {restore}\nthen rerun pseudolife-mcp update; the daemon was not changed")
    daemon = ""
    try:
        update.daemon_after_postgres()      # the daemon was not recreated: it must still answer
    except up.UpdateError as exc:
        daemon = f" The daemon did not recover on the restored container: {str(exc).splitlines()[0]}."
    raise up.UpdateError(
        f"PostgreSQL did not start on this release's definition ({why}); its previous container is restored and "
        f"healthy, and the daemon was not changed.{daemon} Nothing else was deployed. See docker logs "
        f"{up.PG_CONTAINER} for the restored server, and report the cause above at "
        "https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues before rerunning pseudolife-mcp update")
