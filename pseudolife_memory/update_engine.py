"""Opt-in Docker engine changes, using the existing update lock and backup scripts.

The host selection is a Compose overlay, saved only after the health gate.
Python's separate acceptance record validates retries without selecting a checkout.
Rollback overlays name the actual running image ID's tag and carry the old
engine selection, so an unhealthy replacement cannot become the next rollback.
"""
from __future__ import annotations

import json
import gzip
import os
import re
import secrets
import shlex
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pseudolife_memory import update_cli as up

SELECTION = "data/update-engine/docker-compose.engine.json"
PYTHON_ACCEPTED = "data/update-engine/docker-compose.python-accepted.json"
ENGINE_LABEL = "org.pseudolife.engine"
CHECKOUT_LABEL = "org.pseudolife.checkout"
ACTIVATION_LABEL = "org.pseudolife.activation"
RELEASE_LABEL = "org.pseudolife.release"
RELEASE_ACCEPTED = "update-engine/docker-compose.release-accepted.json"
SHADOW_SERVICE = "pseudolife-rust-shadow"


def require_imported_bank(schema, pending: bool) -> None:
    if type(schema) is not int or schema < 57 or pending:
        raise up.UpdateError("Rust activation requires schema v57 or later and imported counters: run "
                             "update --engine python --checkout <repo>, let the Python daemon import "
                             "memory_state/weights.pt into cms_counters, then retry; nothing was changed", 2)


def _selection(repo: Path, filename: str = SELECTION) -> dict | None:
    path = repo / filename
    if not path.exists():
        return None
    try:
        service = json.loads(path.read_text(encoding="utf-8"))["services"][up.DAEMON_SERVICE]
        if service["labels"][ENGINE_LABEL] not in ("rust", "python"):
            raise ValueError("engine")
        return service
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise up.UpdateError(f"invalid engine selection at {path}; restore its rollback copy before updating", 2) from exc


def _validated_engine_selection(labels: dict) -> dict:
    engine = labels.get(ENGINE_LABEL, "python")
    origin = labels.get(CHECKOUT_LABEL)
    release = labels.get(RELEASE_LABEL)
    selected = (_selection(up.data_dir(), RELEASE_ACCEPTED) if release else
                _selection(Path(origin), SELECTION if engine == "rust" else PYTHON_ACCEPTED) if origin else None)
    if (not selected or selected["labels"][ENGINE_LABEL] != engine
            or not labels.get(ACTIVATION_LABEL)
            or selected["labels"].get(ACTIVATION_LABEL) != labels.get(ACTIVATION_LABEL)
            or release and selected["labels"].get(RELEASE_LABEL) != release):
        raise up.UpdateError(f"the running {engine} container has no valid accepted activation; use the previous update's "
                             "rollback commands (the Python rollback for a first activation) before retrying; nothing was changed", 2)
    return selected


def selected_checkout(update) -> Path | None:
    o = update.o
    selected = None
    container = up.DAEMON_CONTAINER
    if o.checkout is not None:
        repo = Path(o.checkout).resolve()
        selected = _selection(repo)
        if o.engine:
            return repo
        if not o.allow_dirty and up.tree_state(repo)["dirty"] != "false":
            # Keep the existing clean-tree refusal ahead of all Docker calls.
            return None
        env_file = repo / "ops/.env"
        compose = update._compose_args([repo / "ops/docker-compose.yml", repo / "ops/docker-compose.override.yml"], env_file)
        code, out = up.run_cli([update.docker, "compose", *compose, "ps", "-a", "-q", up.DAEMON_SERVICE],
                               env=up.compose_environment(env_file), timeout=60, stdout_only=True)
        ids = out.split() if code == 0 else []
        if len(ids) == 1:
            container = ids[0]
        elif selected:
            raise up.UpdateError("cannot find this checkout's selected daemon; restore its Compose project and retry; nothing was changed", 2)
    code, out = up.run_cli([update.docker, "inspect", "-f", "{{json .Config.Labels}}", container], timeout=60, stdout_only=True)
    try:
        labels = (json.loads(out) or {}) if code == 0 else {}
    except ValueError:
        labels = {}
    if labels.get(ENGINE_LABEL) == "python" and labels.get(ACTIVATION_LABEL):
        _validated_engine_selection(labels)
    current = "rust" if labels.get(ENGINE_LABEL) == "rust" else "python"
    if selected and selected["labels"][ENGINE_LABEL] != current:
        raise up.UpdateError("this checkout's engine selection disagrees with the host; use --engine python --checkout <repo> "
                             "to restore the default, or --engine rust --checkout <repo> to explicitly activate Rust; nothing was changed", 2)
    if labels.get(ENGINE_LABEL) != "rust":
        if o.allow_auth_change and o.checkout is not None:
            update._selected_engine = "python"
            return repo
        if o.engine == "python":
            o.daemon_only = True
        return None
    if o.clients_only:
        return None
    origin = Path(labels.get(CHECKOUT_LABEL, ""))
    selected = _validated_engine_selection(labels)
    if o.checkout is not None:
        update._selected_engine = selected["labels"][ENGINE_LABEL]
        return repo
    if labels.get(RELEASE_LABEL):
        # A release-mode activation stays on the release path.
        return None
    if o.tag or o.reinstall or o.allow_downgrade or o.env_file:
        raise up.UpdateError("this host selected a checkout engine; use update --checkout <repo> "
                             "to keep it, or --engine python --checkout <repo> to switch back; nothing was changed", 2)
    return origin.resolve()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authenticated_read(url: str, token: str) -> bool:
    """A bank read, with redirects disabled so its bearer stays on this daemon."""
    request = urllib.request.Request(url.rstrip("/") + "/api/facts?limit=1",
                                     headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=10) as response:
            result = json.loads(response.read())
            return isinstance(result, dict) and isinstance(result.get("entries"), list) and not result.get("error")
    except (OSError, ValueError, urllib.error.URLError):
        return False


def gate_failure(health, version, schema, read, *, shadow: bool = False) -> str | None:
    """The first health-gate check this /health answer fails, or None.

    ``version`` or ``schema`` None skips that check; ``read`` performs the
    authenticated bank read, attempted only once every other check passed.
    Values print as JSON, as the native updater prints them.
    """
    if not isinstance(health, dict):
        return "/health did not answer"
    if health.get("status") != "ok":
        return f"/health reported status {json.dumps(health.get('status'))} instead of \"ok\""
    if version is not None and health.get("version") != version:
        return f"version {json.dumps(health.get('version'))} instead of {json.dumps(version)}"
    if schema is not None and health.get("schema") != schema:
        return f"schema {json.dumps(health.get('schema'))} instead of {json.dumps(schema)}"
    if health.get("counters_import_required"):
        return "counters_import_required: the daemon has not imported its counters"
    if shadow and not (health.get("read_only") is True and health.get("ready") is True and health.get("db") == "ok"):
        return (f"the shadow is not ready read-only (read_only={json.dumps(health.get('read_only'))}, "
                f"ready={json.dumps(health.get('ready'))}, db={json.dumps(health.get('db'))})")
    if not read():
        return "the authenticated bank read was refused"
    return None


def _write_overlay(path: Path, service: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"services": {up.DAEMON_SERVICE: service}}, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _backup_files(directory: Path) -> dict:
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in directory.glob("pseudolife_*") if p.is_file()}


def _verify_backup(directory: Path, before: dict) -> dict:
    after = _backup_files(directory)
    dumps = [name for name in after if re.fullmatch(r"pseudolife_memory-\d{8}-\d{6}\.sql\.gz", name)
             and after[name] != before.get(name)]
    if len(dumps) != 1:
        raise up.UpdateError("cannot identify this run's bank and state backup; check the backup directory and retry; nothing was deployed")
    dump = directory / dumps[0]
    state_name = dumps[0].replace("pseudolife_memory-", "pseudolife_state-").replace(".sql.gz", ".tgz")
    state = directory / state_name
    try:
        if after.get(state_name) == before.get(state_name) or not up._dump_complete(dump):
            raise ValueError("missing or unchanged backup")
        # A zero-exit backup script may still have warned that /data failed.
        _read_state_archive(state)
    except (OSError, ValueError, EOFError, tarfile.TarError) as exc:
        raise up.UpdateError("the bank or state backup is missing or incomplete; fix the backup error and retry; nothing was deployed") from exc
    return {"dump": str(dump), "state_archive": str(state)}


def _read_state_archive(state: Path) -> None:
    """Read the complete tar and gzip trailer, without extracting anything;
    raises on a truncated or corrupt archive."""
    with gzip.open(state, "rb") as stream:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                if member.isfile():
                    with archive.extractfile(member) as content:
                        while content.read(1024 * 1024):
                            pass
        while stream.read(1024 * 1024):
            pass


def deploy_engine(update, repo: Path) -> None:
    EngineUpdate(update, repo).run()


RUST_IMAGE = "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust"
RUST_RELEASE_OVERLAY = "docker-compose.rust-ghcr.yml"
# A rollback or candidate overlay that a previous engine run left in the
# daemon's Compose labels: never carried into the next release update.
TRANSIENT_OVERLAYS = ("docker-compose.engine", "docker-compose.rollback")


def transient_overlay(path: Path) -> bool:
    """Engine overlays a release update supplies itself, never reuses."""
    name = Path(path).name
    return name.startswith(TRANSIENT_OVERLAYS) or name == RUST_RELEASE_OVERLAY


def host_labels(update) -> dict:
    code, out = up.run_cli([update.docker, "inspect", "-f", "{{json .Config.Labels}}", up.DAEMON_CONTAINER],
                           timeout=60, stdout_only=True)
    try:
        labels = json.loads(out.strip().splitlines()[-1]) if code == 0 and out.strip() else {}
    except (ValueError, IndexError):
        labels = {}
    return labels if isinstance(labels, dict) else {}


def release_engine_choice(update) -> tuple[str, str]:
    """``(selected, choice)``: the engine the host runs and the one this run deploys."""
    selected = "rust" if host_labels(update).get(ENGINE_LABEL) == "rust" else "python"
    return selected, update.o.engine or selected


def release_accepted() -> Path:
    return up.data_dir() / RELEASE_ACCEPTED


def _quote(value) -> str:
    text = str(value)
    if os.name == "nt":
        return "'" + text.replace("'", "''") + "'"
    return shlex.quote(text)


def rollback_commands(base: list[str], env_file, overlay: Path, accepted: Path, image_tag: str) -> list[str]:
    """Recreate the retained image without pulling, then record it as accepted.

    The release Rust overlay requires an image tag even when the rollback
    overlay names a local image, so the command supplies the target's tag.
    """
    args = " ".join(_quote(a) for a in [*base, "-f", str(overlay)])
    restore = f"docker compose {args} up -d --no-deps --no-build --pull never {up.DAEMON_SERVICE}"
    if os.name == "nt":
        restore = f"$env:PSEUDOLIFE_IMAGE_TAG={_quote(image_tag)}; {restore}"
        copy = f"Copy-Item -LiteralPath {_quote(overlay)} -Destination {_quote(accepted)} -ErrorAction Stop"
        restore += (f"; if ($LASTEXITCODE -eq 0) {{ {copy} }} else "
                    "{ throw 'Rollback recreation failed; accepted engine selection was not changed' }")
    else:
        restore = f"PSEUDOLIFE_IMAGE_TAG={_quote(image_tag)} {restore} && cp -- {_quote(overlay)} {_quote(accepted)}"
    if up._env_file_sets_credentials(env_file):
        clear = ("Remove-Item Env:PSEUDOLIFE_MCP_TOKEN,Env:PSEUDOLIFE_MCP_TOKENS -ErrorAction SilentlyContinue"
                 if os.name == "nt" else "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS")
        return [clear, restore]
    fix = "Restore PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKENS in this shell before rollback"
    if os.name == "nt":
        return [f"if ($env:PSEUDOLIFE_MCP_TOKEN -or $env:PSEUDOLIFE_MCP_TOKENS) {{ {restore} }} else {{ throw '{fix}' }}"]
    return ['if [ -n "${PSEUDOLIFE_MCP_TOKEN:-}${PSEUDOLIFE_MCP_TOKENS:-}" ]; then ' + restore
            + f"; else echo '{fix}' >&2; false; fi"]


def _mount_spec(fields) -> str:
    """A ``docker run --mount`` value: Docker parses it as CSV, so a field
    holding a comma or quote is quoted, with its quotes doubled."""
    out = []
    for key, value in fields:
        field = f"{key}={value}"
        out.append('"' + field.replace('"', '""') + '"' if any(c in field for c in ',"\n\r') else field)
    return ",".join(out)


def model_volumes(config: dict) -> tuple[str, list[tuple[str, str, str | None]]] | None:
    """``(models dir, [(docker run flag, value, compose volume key or None)])``
    for the mounts covering the daemon's models directory; None when the
    daemon names its own ONNX model directory. A named volume uses
    ``--mount`` (it keeps a subpath); a bind uses ``-v``, which creates a
    missing host path as Compose does."""
    service = config["services"][up.DAEMON_SERVICE]
    env = service.get("environment", {}) or {}
    if env.get("PSEUDOLIFE_DAEMON_ONNX_DIR"):
        return None
    models_dir = (env.get("PSEUDOLIFE_DAEMON_MODELS_DIR") or "/data/models").rstrip("/")
    mounts = []
    for volume in service.get("volumes", []) or []:
        target = str(volume.get("target", "")).rstrip("/")
        source = volume.get("source")
        if not source or not target or not (models_dir == target or models_dir.startswith(target + "/")):
            continue
        if volume.get("type") == "volume":
            name = (config.get("volumes", {}).get(source) or {}).get("name") or source
            fields = [("type", "volume"), ("src", name), ("dst", target)]
            subpath = (volume.get("volume") or {}).get("subpath")
            mounts.append(("--mount", _mount_spec(fields + ([("volume-subpath", subpath)] if subpath else [])), source))
        elif volume.get("type") == "bind":
            mounts.append(("-v", f"{source}:{target}", None))
    if not mounts:
        raise up.UpdateError(f"the Rust daemon's model directory {models_dir} is on no persistent volume of this "
                             "project; restore the release Compose files and retry; nothing was changed", 2)
    return models_dir, mounts


def ensure_project_volume(command, config: dict, key: str) -> None:
    """Create a project volume as Compose would (its driver, options and
    labels), so ``up`` adopts it; an external one must already exist."""
    definition = config.get("volumes", {}).get(key) or {}
    name = definition.get("name") or key
    code, out = command(["volume", "inspect", name], timeout=60)
    try:
        if code == 0 and json.loads(out):
            return
    except ValueError:
        pass
    if definition.get("external"):
        raise up.UpdateError(f"the external model volume {name} does not exist; create it as the project expects, "
                             "then retry; nothing was deployed", 2)
    args = ["volume", "create", "--label", f"com.docker.compose.project={config.get('name') or 'pseudolife-mcp'}",
            "--label", f"com.docker.compose.volume={key}"]
    if definition.get("driver"):
        args += ["--driver", str(definition["driver"])]
    for option, value in sorted((definition.get("driver_opts") or {}).items()):
        args += ["--opt", f"{option}={value}"]
    for label, value in sorted((definition.get("labels") or {}).items()):
        args += ["--label", f"{label}={value}"]
    if command([*args, name], timeout=60)[0]:
        raise up.UpdateError(f"could not create the model volume {name}; check Docker and retry; nothing was deployed")


def kept_note(message: str, rollback: str, built: str | None) -> str:
    """A failure before the recreate: the rollback tag stays, and after a
    successful build its tag names an image that never passed the gate."""
    note = f"{message}; the running image keeps its rollback tag {rollback}"
    return note + (f"; {built} now names the unvalidated build" if built else "")


def uses_models_volume(running: dict) -> bool:
    """Whether a running Rust daemon reads its models from the overlay's
    /models (its environment names it, or a volume is mounted there),
    rather than an earlier checkout's /data/models."""
    env = (running.get("Config") or {}).get("Env") or []
    return (any(isinstance(e, str) and e.startswith("PSEUDOLIFE_DAEMON_MODELS_DIR=")
                and e.split("=", 1)[1].rstrip("/") == "/models" for e in env)
            or any(isinstance(m, dict) and m.get("Destination") == "/models" for m in running.get("Mounts") or []))


def provision_models(update, command, image: str, config: dict) -> None:
    """Install the pinned models into the daemon's model volume before backup.

    A first start would otherwise download them inside the health gate.
    The running daemon keeps serving; a rerun only verifies the cache.
    ``config`` is the Compose config the replacement will run with.
    """
    plan = model_volumes(config)
    if plan is None:
        update.step("the daemon names its own ONNX model directory; leaving model provisioning to it")
        return
    models_dir, mounts = plan
    for _flag, _value, key in mounts:
        if key is not None:
            ensure_project_volume(command, config, key)
    update.step(f"provisioning the Rust daemon's pinned models into {models_dir} (a first activation downloads them)...")
    args = ["run", "--rm", "-e", f"PSEUDOLIFE_DAEMON_MODELS_DIR={models_dir}"]
    for flag, value, _key in mounts:
        args += [flag, value]
    code, out = command([*args, "--entrypoint", "pseudolife-daemon", image, "provision", "models", "--if-missing"],
                        timeout=3600, stream=not update.o.as_json)
    if code:
        raise up.UpdateError("could not provision the Rust daemon's models" + (f" ({up._last_line(out)})" if out.strip() else "")
                             + "; check this host's network access and disk space, then retry; nothing was deployed")


def deploy_release_engine(update, target: str, current: str | None, previous_digest: str | None,
                          before_recreate, choice: str) -> None:
    ReleaseEngineUpdate(update, target, current, previous_digest, choice).run(before_recreate)


class ReleaseEngineUpdate:
    """Release-mode engine selection: the published image of either engine.

    The order matches the native updater's ``deploy_release``: verify the
    project, refuse an unimported bank, pull and probe the image, provision
    the Rust models, back up, tag the running image, recreate behind a
    candidate overlay, gate on version, schema and an authenticated read,
    and only then record the accepted selection.
    """

    def __init__(self, update, target, current, previous_digest, choice):
        self.u, self.o = update, update.o
        self.target, self.current, self.previous_digest = target, current, previous_digest
        self.engine = choice
        self.host_engine = "rust" if host_labels(update).get(ENGINE_LABEL) == "rust" else "python"

    def command(self, args, **kw):
        return up.run_cli([self.u.docker, *args], stdout_only=not kw.get("stream", False), **kw)

    def project_container(self, compose, env, service):
        code, out = self.command(["compose", *compose, "ps", "-a", "-q", service], env=env, timeout=60)
        ids = out.split()
        if code or len(ids) != 1:
            raise up.UpdateError(f"the project needs one existing {service} container; restore that service in this "
                                 "Compose project before updating; nothing was changed", 2)
        return ids[0]

    def sql(self, pg, user, db, query):
        code, out = self.command(["exec", pg, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db, "-Atc", query],
                                 timeout=60)
        if code:
            raise up.UpdateError("cannot check the bank schema; start the project's PostgreSQL and retry; nothing was changed", 2)
        return out.strip()

    def verify_project(self, config, running):
        """The recreated daemon keeps this bank, /data volume and loopback endpoint."""
        service = config["services"][up.DAEMON_SERVICE]
        daemon_env = service.get("environment", {}) or {}
        entries = (running.get("Config") or {}).get("Env")
        if not isinstance(entries, list) or not all(isinstance(item, str) and "=" in item for item in entries):
            raise up.UpdateError("cannot verify the daemon's Compose bearer settings; restore the original compose "
                                 "and env files, then retry. Nothing was changed", 2)
        running_env = dict(item.split("=", 1) for item in entries)
        if running_env.get("PSEUDOLIFE_MCP_DATABASE_URL") != daemon_env.get("PSEUDOLIFE_MCP_DATABASE_URL"):
            raise up.UpdateError("the host Compose configuration names a different bank from the running daemon; "
                                 "restore its original env file before switching; nothing was changed", 2)
        data_mount = next((v for v in service.get("volumes", []) if v.get("target") == "/data"), None)
        actual = next((v for v in running.get("Mounts", []) if v.get("Destination") == "/data"), None)
        expected = (config.get("volumes", {}).get(data_mount.get("source"), {}).get("name")
                    if data_mount and data_mount.get("type") == "volume" else None)
        if not expected or not actual or actual.get("Name") != expected:
            raise up.UpdateError("the host Compose configuration must keep the running daemon's named /data volume; "
                                 "restore its volume setting and retry; nothing was changed", 2)
        published = next((p for p in service.get("ports", []) if p.get("target") == 8765), None)
        endpoint = urllib.parse.urlsplit(self.o.daemon_url)
        if (not published or published.get("host_ip") != "127.0.0.1"
                or endpoint.hostname not in ("127.0.0.1", "localhost")
                or endpoint.scheme != "http" or endpoint.port != int(published["published"])):
            port = published.get("published", 8765) if published else 8765
            raise up.UpdateError(f"engine verification must reach this project's loopback port; use --daemon-url "
                                 f"http://127.0.0.1:{port}; nothing was changed", 2)
        from pseudolife_memory.principals import parse_token_map

        def bearer(env):
            token = env.get("PSEUDOLIFE_MCP_TOKEN") or next(iter(parse_token_map(env.get("PSEUDOLIFE_MCP_TOKENS"))), "")
            return token if token and "\r" not in token and "\n" not in token else ""
        token = bearer(daemon_env)
        if not token:
            raise up.UpdateError("the engine health gate needs bearer auth; set PSEUDOLIFE_MCP_TOKEN in the host env "
                                 "file and retry; nothing was changed", 2)
        # A daemon running without a bearer (--allow-auth-change adding one)
        # answers any bearer: read it with the configured one.
        return token, bearer(running_env) or token

    def backup(self, checkout):
        """The bank dump and the /data archive, both verified complete."""
        u = self.u
        if self.o.no_backup:
            u.warn("skipping backup (--no-backup)")
            return
        if u._checkout_script(checkout, "backup") is not None:
            directory = Path(checkout) / "data" / "backups"
            before = _backup_files(directory) if directory.is_dir() else {}
            u.backup(checkout)
            _verify_backup(directory, before)
            return
        u.step("backing up the bank (pg_dump in the container + state volume tar)...")
        state = u.builtin_backup().get("state")
        try:
            if not state:
                raise ValueError("no state archive")
            # A failed copy can leave a partial archive behind: read it whole.
            _read_state_archive(Path(state))
        except (OSError, ValueError, EOFError, tarfile.TarError) as exc:
            raise up.UpdateError("the state volume backup is missing or incomplete; the bank dump is retained; fix the "
                                 "backup error and retry; nothing was deployed") from exc

    def require_imported(self, pg, user, db, daemon):
        raw = self.sql(pg, user, db, "SELECT value::text FROM meta WHERE key = 'schema_version'")
        try:
            schema = int(raw.strip('"'))
        except ValueError:
            schema = None
        pending = True
        if type(schema) is int and schema >= 57:
            pending = (self.sql(pg, user, db, "SELECT EXISTS (SELECT 1 FROM cms_counters WHERE id = 1)") != "t"
                       and self.command(["exec", daemon, "sh", "-c", "test ! -e /data/memory_state/weights.pt && "
                                         "test ! -e /data/memory_state/weights.pt.bak"], timeout=60)[0] != 0)
        if type(schema) is not int or schema < 57 or pending:
            raise up.UpdateError("Rust activation requires schema v57 or later and imported counters: run update "
                                 "--engine python, let the Python daemon import memory_state/weights.pt into "
                                 "cms_counters, then retry; nothing was changed", 2)

    def probe_schema(self, image):
        if self.engine == "rust":
            probe = ["run", "--rm", "--network", "none", "--entrypoint", "/usr/local/bin/pseudolife-mcp", image,
                     "update", "--schema-version"]
        else:
            probe = ["run", "--rm", "--network", "none", "--entrypoint", "sh", image, "-c",
                     "cat /app/pseudolife_memory/storage/schema.py"]
        code, out = self.command(probe, timeout=60)
        match = (re.fullmatch(r"\s*(\d+)\s*", out) if self.engine == "rust"
                 else re.search(r"^SCHEMA_META_VERSION\s*=\s*(\d+)", out, re.M))
        if code or not match:
            raise up.UpdateError("cannot read the pulled image's schema version; verify its published CLI/image "
                                 "contract and retry; nothing was deployed", 2)
        return int(match.group(1))

    def provision_models(self, image, config):
        provision_models(self.u, self.command, image, config)

    def gate(self, token, schema):
        self.u.step("waiting for the daemon to report healthy...")
        reason = "/health did not answer"
        for attempt in range(self.o.health_retries):
            health = self.u.daemon_health()
            reason = gate_failure(health, self.target, schema, lambda: authenticated_read(self.o.daemon_url, token))
            if reason is None:
                before, after = self.u.schema_before, health.get("schema")
                if type(before) is int and type(after) is int and after > before:
                    self.u.report.rollback["restore_required"] = True
                self.u.step(f"healthy. version={health.get('version')} schema={health.get('schema')} "
                            f"persist_errors={health.get('persist_errors')}")
                if self.u.rollback_lines and not self.o.as_json:
                    print("    Rolled-back deploy if ever needed:")
                    for line in self.u.rollback_lines:
                        print(line)
                    sys.stdout.flush()
                return health
            if attempt + 1 < self.o.health_retries:
                up.sleep(self.o.health_delay_ms / 1000)
        raise up.UpdateError("the daemon failed its expected version, schema or authenticated-read health gate at the "
                             f"configured endpoint ({reason}, last of {self.o.health_retries} checks); the client side "
                             "was not moved\n" + self.u._health_endpoint_hint())

    def run(self, before_recreate) -> None:
        u, o = self.u, self.o
        # A switch away from Rust still resolves the Rust overlay for its rollback.
        context = u.compose_context(engine=self.engine, rust_overlay=self.host_engine == "rust")
        compose = context["args"]
        u._compose_shown = compose
        environment = up.compose_environment(context["env_file"], {"PSEUDOLIFE_IMAGE_TAG": self.target})
        code, out = self.command(["compose", *compose, "config", "--format", "json"], env=environment, timeout=60)
        try:
            if code:
                raise ValueError("compose")
            config = json.loads(out)
            config["services"][up.DAEMON_SERVICE]
        except (ValueError, KeyError, TypeError) as exc:
            # Compose errors can include resolved credentials: never print them.
            raise up.UpdateError("cannot resolve the Compose project; check its files and env file, then retry; "
                                 "nothing was changed", 2) from exc
        u.verify_compose_authentication(compose, environment)
        u._compose_clear_bearers = up._env_file_sets_credentials(context["env_file"])
        code, out = self.command(["inspect", up.DAEMON_CONTAINER], timeout=60)
        try:
            running = json.loads(out)[0] if code == 0 else None
            running["Id"]
        except (ValueError, IndexError, TypeError, KeyError) as exc:
            raise up.UpdateError("cannot inspect the daemon's Compose project; start it and retry; nothing was changed", 2) from exc
        token, running_token = self.verify_project(config, running)
        before = u.daemon_health() or {}
        # The running daemon still holds its own bearer (--allow-auth-change
        # replaces it); the configured one is checked after recreation.
        if before.get("status") != "ok" or not authenticated_read(o.daemon_url, running_token):
            raise up.UpdateError("the running daemon did not pass an authenticated bank read; use the previous update's "
                                 "rollback commands before retrying; no rollback tag was moved", 2)
        daemon = running["Id"]
        if self.engine == "rust":
            pg_env = config["services"].get("pseudolife-pg", {}).get("environment", {}) or {}
            user, db = pg_env.get("POSTGRES_USER"), pg_env.get("POSTGRES_DB")
            if not user or not db:
                raise up.UpdateError("cannot resolve the project's PostgreSQL user and database; set POSTGRES_USER and "
                                     "POSTGRES_DB in its Compose environment and retry; nothing was changed", 2)
            pg = self.project_container(compose, environment, "pseudolife-pg")
            self.require_imported(pg, user, db, daemon)
            model_volumes(config)  # refuse a misplaced model directory before the pull
        image = f"{RUST_IMAGE if self.engine == 'rust' else up.GHCR_IMAGE}:{self.target}"
        u.preflight_disk(up.data_dir())
        u.step(f"pulling {image}...")
        code, out = self.command(["pull", image], timeout=7200, stream=not o.as_json)
        if code != 0:
            if self.engine != "rust":
                hint = f"; nothing was changed. Is {self.target} a published release?"
            elif o.engine:
                hint = (f"; nothing was changed. Is a Rust daemon image published for {self.target}? Use --engine python "
                        "to stay on the Python daemon")
            else:
                hint = (f"; nothing was changed: this host keeps its current Rust daemon. Retry once {self.target} "
                        "publishes its Rust daemon image, or move to it on Python with --engine python")
            raise up.UpdateError(f"docker pull {image} failed" + (f" ({up._last_line(out)})" if out.strip() else "") + hint)
        schema = self.probe_schema(image)
        bank = before.get("schema")
        if type(bank) is int and bank > schema:
            raise up.UpdateError(f"the bank is at schema v{bank}, newer than {image}'s v{schema}, which would refuse it; "
                                 f"update to a release at schema v{bank} or later; nothing was changed", 2)
        if self.engine == "rust":
            self.provision_models(image, config)
        self.backup(context["checkout"])
        if before_recreate is not None:
            before_recreate()
        running_id, _ref = u.running_image()
        if not running_id:
            raise up.UpdateError(f"the {up.DAEMON_CONTAINER} container vanished during the backup; nothing was deployed")
        if running_id != running.get("Image"):
            raise up.UpdateError("the running daemon changed during backup; retry; nothing was deployed")
        labels = (running.get("Config") or {}).get("Labels") or {}
        old_engine = "rust" if labels.get(ENGINE_LABEL) == "rust" else "python"
        suffix = o.rollback_suffix or f"pre-update-{up._stamp()}"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", suffix):
            raise up.UpdateError("--rollback-tag must contain only letters, digits, dots, underscores or hyphens; "
                                 "nothing was deployed", 2)
        rollback = f"{RUST_IMAGE if old_engine == 'rust' else up.GHCR_IMAGE}:{self.current or 'previous'}-{suffix}"
        exists, prior = self.command(["image", "inspect", "-f", "{{.Id}}", rollback], timeout=60)
        if exists == 0 and prior.strip() and prior.strip() != running_id:
            if not o.force_rollback_tag:
                raise up.UpdateError("that rollback tag already names another image; choose a new --rollback-tag suffix "
                                     "or use --force-rollback-tag to replace it with the validated running image; "
                                     "nothing was deployed", 2)
            u.warn("--force-rollback-tag: replacing the existing rollback tag with the validated running image")
        if self.command(["tag", running_id, rollback], timeout=60)[0]:
            raise up.UpdateError("could not tag the running image for rollback; nothing was deployed")
        u.step(f"tagged rollback image: {rollback}")
        old_service = {"image": rollback, "labels": {ENGINE_LABEL: old_engine}}
        for key in (CHECKOUT_LABEL, ACTIVATION_LABEL, RELEASE_LABEL):
            if isinstance(labels.get(key), str):
                old_service["labels"][key] = labels[key]
        directory = up.data_dir() / "update-engine"
        rollback_file = directory / f"docker-compose.rollback-{up._stamp()}-{secrets.token_hex(16)}.json"
        try:
            _write_overlay(rollback_file, old_service)
        except OSError as exc:
            raise up.UpdateError(f"cannot write the rollback overlay in {directory}; restore write access and retry. "
                                 f"The running image is tagged {rollback}; nothing was deployed") from exc
        restore_compose = list(compose)
        if old_engine == "rust" and self.engine != "rust":
            # The restored Rust daemon needs its overlay's model volume again.
            restore_compose += ["-f", context["rust_overlay"]]
        u.rollback_lines = [*rollback_commands(restore_compose, context["env_file"], rollback_file, release_accepted(),
                                               self.target),
                            *u._schema_rollback_note(None)]
        u.report.rollback = {"tag": rollback, "state": "tagged", "overlay": str(rollback_file)}
        u.prune_rollbacks(context["checkout"], up.GHCR_IMAGE)
        activation = secrets.token_hex(16)
        service = {"image": image, "labels": {ENGINE_LABEL: self.engine, ACTIVATION_LABEL: activation,
                                              RELEASE_LABEL: self.target}}
        candidate = directory / f"docker-compose.engine-candidate-{activation}.json"
        try:
            _write_overlay(candidate, service)
            u.step(f"recreating the daemon container on {image} (Postgres + extractor untouched)...")
            code, out = self.command(["compose", *compose, "-f", str(candidate), "up", "-d", "--no-deps", "--no-build",
                                      up.DAEMON_SERVICE], env=environment, timeout=3600, stream=not o.as_json)
            if code:
                raise up.UpdateError("daemon recreate failed" + (f" ({up._last_line(out)})" if out.strip() else ""))
            health = self.gate(token, schema)
            _write_overlay(release_accepted(), service)
        except (up.UpdateError, OSError) as exc:
            raise up.UpdateError(str(exc) + "\nto roll back:\n" + "\n".join(u.rollback_lines)) from exc
        finally:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                u.warn("could not remove the temporary engine overlay; the rollback instructions remain usable")
        u.step(f"selected the {self.engine} daemon {self.target}; plain updates keep this engine"
               + ("" if self.engine == "rust" else " (--engine rust switches it)"))
        if not o.daemon_only:
            u.clients(None, f"pseudolife-mcp=={self.target}", health)
        else:
            u.hooks_changed_note(self.previous_digest, health)
        u.console_notes(health)


class EngineUpdate:
    def __init__(self, update, repo: Path):
        self.u, self.o, self.repo = update, update.o, repo
        self.ops = repo / "ops"
        self.env_file = self.ops / ".env"
        self.selected = None if self.o.shadow else _selection(repo)
        self.engine = self.o.engine or (self.selected["labels"][ENGINE_LABEL] if self.selected
                                        else update._selected_engine)
        self.base = update._compose_args([self.ops / "docker-compose.yml", self.ops / "docker-compose.override.yml"], self.env_file)
        self.env = up.compose_environment(self.env_file)

    def command(self, args, **kw):
        return up.run_cli([self.u.docker, *args], stdout_only=not kw.get("stream", False), **kw)

    def config(self, compose, env=None):
        code, out = self.command(["compose", *compose, "config", "--format", "json"], env=env or self.env, timeout=60)
        try:
            if code:
                raise ValueError("compose")
            return json.loads(out)
        except ValueError as exc:
            # Compose errors can include resolved credentials: never print them.
            raise up.UpdateError("cannot resolve the Compose project; check its files and env file, then retry; nothing was changed", 2) from exc

    def container(self, service):
        code, out = self.command(["compose", *self.base, "ps", "-a", "-q", service], env=self.env, timeout=60)
        ids = out.split()
        if code or len(ids) != 1:
            raise up.UpdateError(f"the project needs one existing {service} container; start its Python stack before switching engines", 2)
        return ids[0]

    def inspect(self, container):
        code, out = self.command(["inspect", container], timeout=60)
        try:
            if code:
                raise ValueError("inspect")
            return json.loads(out)[0]
        except (ValueError, IndexError, TypeError) as exc:
            raise up.UpdateError("cannot inspect the project's running daemon; start it and retry", 2) from exc

    def sql(self, query):
        prefix = ["exec", self.pg]
        database = self.pg_db
        if getattr(self, "stopped_handover", False):
            from psycopg.conninfo import make_conninfo
            prefix += ["env", "-u", "PGHOSTADDR", "-u", "PGSERVICE", "-u", "PGSERVICEFILE"]
            database = make_conninfo(dbname=database)
        client = ["psql", "-X", "-v", "ON_ERROR_STOP=1"]
        if getattr(self, "stopped_handover", False):
            client += ["-h", "/var/run/postgresql", "-p", str(self.pg_port)]
        code, out = self.command([*prefix, *client, "-U", self.pg_user, "-d", database, "-Atc", query], timeout=60)
        if code:
            raise up.UpdateError("cannot check the bank schema; start the project's PostgreSQL and retry; nothing was changed", 2)
        return out.strip()

    def preflight(self):
        if self.o.no_backup:
            raise up.UpdateError("engine changes require a backup; remove --no-backup and retry; nothing was changed", 2)
        tree = up.tree_state(self.repo)
        if tree["dirty"] != "false" and not self.o.allow_dirty:
            raise up.UpdateError("commit or stash the checkout before switching engines (or pass --allow-dirty); nothing was changed", 2)
        self.tree = tree
        self.env.update({"PSEUDOLIFE_BUILD_GIT_SHA": tree["sha"], "PSEUDOLIFE_BUILD_DIRTY": tree["dirty"],
                         "PSEUDOLIFE_BUILD_TIME": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        config = self.config(self.base)
        pg_env = config["services"]["pseudolife-pg"]["environment"]
        self.pg_user, self.pg_db = pg_env["POSTGRES_USER"], pg_env["POSTGRES_DB"]
        self.daemon, self.pg = self.container(up.DAEMON_SERVICE), self.container("pseudolife-pg")
        self.running = self.inspect(self.daemon)
        self.running_labels = self.running["Config"].get("Labels") or {}
        if self.running_labels.get(ENGINE_LABEL) == "rust" or self.running_labels.get(ACTIVATION_LABEL):
            # Explicit engine flags cannot promote a replacement of either
            # engine that never passed its gate.
            _validated_engine_selection(self.running_labels)
        self.stopped_handover = (self.o.allow_auth_change and self.o.checkout is not None
                                and self.running.get("State", {}).get("Running") is False)
        if self.o.allow_auth_change and self.o.checkout is not None and not self.stopped_handover:
            raise up.UpdateError(f"checkout --allow-auth-change requires a stopped daemon; run docker stop {self.daemon} "
                                 "after restoring the target, then retry. Nothing was changed", 2)
        if not self.running.get("State", {}).get("Running") and not self.stopped_handover:
            raise up.UpdateError("the current daemon must be running for the backup and rollback tag; start it and retry", 2)
        daemon_env = config["services"][up.DAEMON_SERVICE].get("environment", {})
        entries = self.running["Config"].get("Env")
        if not isinstance(entries, list) or not all(isinstance(item, str) and "=" in item for item in entries):
            raise up.UpdateError("cannot verify the daemon's Compose bearer settings; restore the original compose "
                                 "and env files, then retry. Nothing was changed", 2)
        running_env = dict(item.split("=", 1) for item in entries)
        up.verify_compose_bearers(daemon_env, running_env, allow_change=self.o.allow_auth_change)
        if running_env.get("PSEUDOLIFE_MCP_DATABASE_URL") != daemon_env.get("PSEUDOLIFE_MCP_DATABASE_URL"):
            raise up.UpdateError("the host Compose configuration names a different bank from the running daemon; restore its original env file before switching; nothing was changed", 2)
        if self.stopped_handover:
            pg = self.inspect(self.pg)
            try:
                self.pg_user, self.pg_db, self.pg_port = up._backup_connection_details(self.running, pg)
            except (ValueError, KeyError, TypeError, up.UpdateError) as exc:
                raise up.UpdateError("cannot verify the handover bank on the project's shared PostgreSQL network; "
                                     "restore the daemon's original direct database connection and retry; nothing was deployed", 2) from exc
        data_mount = next((v for v in config["services"][up.DAEMON_SERVICE].get("volumes", []) if v.get("target") == "/data"), None)
        actual_mount = next((v for v in self.running.get("Mounts", []) if v.get("Destination") == "/data"), None)
        expected_volume = (config.get("volumes", {}).get(data_mount.get("source"), {}).get("name")
                           if data_mount and data_mount.get("type") == "volume" else None)
        if not expected_volume or not actual_mount or actual_mount.get("Name") != expected_volume:
            raise up.UpdateError("the host Compose configuration must keep the running daemon's named /data volume; restore its volume setting and retry; nothing was changed", 2)
        ports = config["services"][up.DAEMON_SERVICE].get("ports", [])
        published = next((p for p in ports if p.get("target") == 8765), None)
        endpoint = urllib.parse.urlsplit(self.o.daemon_url)
        if (not published or published.get("host_ip") != "127.0.0.1"
                or endpoint.hostname not in ("127.0.0.1", "localhost")
                or endpoint.scheme != "http" or endpoint.port != int(published["published"])):
            port = published.get("published", 8765) if published else 8765
            raise up.UpdateError(f"engine verification must reach this project's loopback port; use --daemon-url http://127.0.0.1:{port}; nothing was changed", 2)
        from pseudolife_memory.principals import parse_token_map
        if self.o.allow_auth_change and (daemon_env.get("PSEUDOLIFE_MCP_TOKENS") or "").strip() and not parse_token_map(daemon_env.get("PSEUDOLIFE_MCP_TOKENS")):
            raise up.UpdateError("the carried bearer map is invalid; repair PSEUDOLIFE_MCP_TOKENS in ops/.env before retrying; nothing was deployed", 2)
        self.token = daemon_env.get("PSEUDOLIFE_MCP_TOKEN") or next(iter(parse_token_map(daemon_env.get("PSEUDOLIFE_MCP_TOKENS"))), "")
        if not self.token and self.stopped_handover:
            self.token = self.stored_probe_token()
        if not self.token or "\r" in self.token or "\n" in self.token:
            raise up.UpdateError("the engine health gate needs bearer auth; set PSEUDOLIFE_MCP_TOKEN in the host env file and retry; nothing was changed", 2)
        if self.o.shadow:
            if self.running_labels.get(ENGINE_LABEL) == "rust":
                raise up.UpdateError("shadow mode needs the Python daemon; run update --engine python --checkout <repo> first", 2)
            from pseudolife_memory.update_shadow import require_support
            require_support(self.repo)
        if self.engine == "rust":
            raw = self.sql("SELECT value::text FROM meta WHERE key = 'schema_version'")
            try:
                schema = int(raw.strip('"'))
            except ValueError:
                schema = None
            require_imported_bank(schema, False)
            has_row = self.sql("SELECT EXISTS (SELECT 1 FROM cms_counters WHERE id = 1)") == "t"
            if not has_row:
                script = "test ! -e /data/memory_state/weights.pt && test ! -e /data/memory_state/weights.pt.bak"
                code = self.stopped_probe(script)[0] if self.stopped_handover else self.command(
                    ["exec", self.daemon, "sh", "-c", script], timeout=60)[0]
                require_imported_bank(schema, code != 0)
        before = self.stopped_preflight() if self.stopped_handover else self.u.daemon_health() or {}
        if not self.stopped_handover and (before.get("status") != "ok" or not authenticated_read(self.o.daemon_url, self.token)):
            raise up.UpdateError("the running daemon did not pass an authenticated bank read; use the previous update's rollback commands before retrying; no rollback tag was moved", 2)
        self.u.schema_before = before.get("schema")
        self.previous_digest = before.get("hooks_digest")
        self.running_version = before.get("version") or "unknown"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.running_version):
            raise up.UpdateError("the running daemon reported an invalid version; restore its previous image before retrying", 2)
        self.version = re.search(r'^version\s*=\s*"([^"]+)"', (self.repo / "pyproject.toml").read_text(), re.M).group(1)
        self.schema = int(re.search(r"^SCHEMA_META_VERSION\s*=\s*(\d+)", (self.repo / "pseudolife_memory/storage/schema.py").read_text(), re.M).group(1))
        if self.stopped_handover and self.u.schema_before > self.schema:
            raise up.UpdateError("the restored bank is newer than this checkout; update the checkout before retrying; nothing was deployed", 2)

    def stopped_bank(self):
        try:
            from pseudolife_memory.docker_backup_restore import DockerBank
        except ImportError as exc:
            raise up.UpdateError("stopped checkout handover needs complete stopped-daemon backups; upgrade the update runtime to a version with DockerBank backup support, then retry; nothing was deployed", 2) from exc
        return DockerBank(container=self.pg, daemon_container=self.daemon, user=self.pg_user,
                          db=self.pg_db, db_port=self.pg_port, log=self.u.step)

    def stopped_probe(self, script):
        bank = self.stopped_bank()
        with bank.helper("timeout --kill-after=5s 45s sh -c " + shlex.quote(script)) as helper:
            return bank.command("start", "-a", helper, check=False, timeout=60)

    def stored_probe_token(self):
        from pseudolife_memory.credentials import CredentialError, CredentialProvider
        from pseudolife_memory.principals import secret_sha256
        try:
            token = CredentialProvider.from_environment().snapshot().token
        except CredentialError:
            token = None
        hashes = self.sql("SELECT token_hash FROM principals WHERE revoked_at IS NULL AND token_hash IS NOT NULL")
        if not token or secret_sha256(token) not in hashes.splitlines():
            raise up.UpdateError("stored-principal verification needs a carried bearer; set PSEUDOLIFE_MCP_TOKEN_FILE to its existing private token file and retry; nothing was deployed", 2)
        return token

    def stopped_preflight(self):
        config = self.config(self.base)
        pg = self.inspect(self.pg)
        try:
            running_env = dict(value.split("=", 1) for value in self.running["Config"]["Env"])
            if (not pg.get("State", {}).get("Running")
                    or config["services"][up.DAEMON_SERVICE]["environment"].get("PSEUDOLIFE_MCP_DATABASE_URL") != running_env.get("PSEUDOLIFE_MCP_DATABASE_URL")
                    or up._backup_connection_details(self.running, pg) != (self.pg_user, self.pg_db, self.pg_port)):
                raise ValueError("bank")
        except Exception:
            # Driver errors may include the credential-bearing connection URL.
            raise up.UpdateError("the restored bank cannot be matched to this project's PostgreSQL; restore its original database configuration before retrying; nothing was deployed", 2) from None
        image = self.running.get("Image")
        code, out = self.command(["image", "inspect", "-f", "{{.Id}}", image or ""], timeout=60)
        if not image or code or out.strip() != image:
            raise up.UpdateError("the stopped daemon's existing image is unavailable; restore that local image before retrying; nothing was deployed", 2)
        raw = self.sql("SELECT value::text FROM meta WHERE key = 'schema_version'")
        try:
            schema = int(raw.strip('"'))
        except ValueError as exc:
            raise up.UpdateError("cannot verify the restored bank schema; restore a complete bank before retrying; nothing was deployed", 2) from exc
        # The image-backed helper also proves /data is readable without starting
        # the daemon or allowing network access to the restored bank.
        rust = self.running_labels.get(ENGINE_LABEL) == "rust"
        metadata = ("/usr/local/bin/pseudolife-mcp update --schema-version" if rust else
                    "cat /app/pseudolife_memory/storage/schema.py")
        code, output = self.stopped_probe("test -d /data && test -r /data && " + metadata)
        match = re.fullmatch(r"\s*(\d+)\s*", output) if rust else re.search(r"^SCHEMA_META_VERSION\s*=\s*(\d+)", output, re.M)
        if code or not match or int(match.group(1)) < schema:
            raise up.UpdateError("the stopped image cannot verify its state or support the restored schema; restore an accepted image compatible with this bank before retrying; nothing was deployed", 2)
        return {"schema": schema, "version": "previous"}

    def backup(self):
        if self.stopped_handover:
            result = self.stopped_bank().backup(out=self.repo / "data/backups", keep_days=None)
            dump, state = Path(result["dump"]), Path(result["state"])
            if not up._dump_complete(dump) or not state.is_file():
                raise up.UpdateError("the stopped bank and state backup is incomplete; fix the backup error and retry; nothing was deployed")
            self.backup_paths = {"dump": str(dump), "state_archive": str(state)}
            return
        script = self.u._checkout_script(self.repo, "backup")
        if script is None:
            raise up.UpdateError("the checkout backup script is missing; restore ops/backup.sh or ops/backup.ps1 and retry", 2)
        keys = (["-Container", "-DaemonContainer", "-User", "-Db"] if script.suffix == ".ps1"
                else ["--container", "--daemon-container", "--user", "--db"])
        args = self.u._script_argv(script)
        for key, value in zip(keys, (self.pg, self.daemon, self.pg_user, self.pg_db)):
            args.extend([key, value])
        directory = self.repo / "data/backups"
        from pseudolife_memory import disk_space
        disk_space.host(directory)
        disk_space.container(self.u.docker, self.pg)
        disk_space.container(self.u.docker, self.daemon)
        before = _backup_files(directory)
        args.extend(["-OutDir" if script.suffix == ".ps1" else "--out-dir", str(directory)])
        self.u.step("backing up the bank and /data before switching engines...")
        code, _ = up.run_cli(args, timeout=3600, stream=not self.o.as_json)
        if code:
            raise up.UpdateError("the backup failed; nothing was deployed")
        self.backup_paths = _verify_backup(directory, before)

    def gate(self, url, *, retries=None, announce=True):
        retries = self.o.health_retries if retries is None else retries
        reason = "/health did not answer"
        for attempt in range(retries):
            health = up.fetch_json(url.rstrip("/") + "/health", timeout=3)
            reason = gate_failure(health, self.version, self.schema, lambda: authenticated_read(url, self.token),
                                  shadow=self.o.shadow)
            if reason is None:
                if announce:
                    self.u.step(f"{self.engine} health gate passed: version {self.version}, schema {self.schema}, authenticated bank read")
                return health
            if attempt + 1 < retries:
                up.sleep(self.o.health_delay_ms / 1000)
        raise up.UpdateError(f"the engine failed its version, schema or authenticated-read gate: {reason} "
                             f"(last of {retries} checks)")

    def build(self, compose, image):
        """Build the replacement, then install its pinned models: both before
        the backup, so a first activation's model download never runs inside
        the health gate, and a failure leaves the running daemon untouched."""
        now = up.tree_state(self.repo)
        if now["sha"] != self.tree["sha"] or now["dirty"] != self.tree["dirty"]:
            raise up.UpdateError("the checkout moved since this update started; retry from a stable checkout; "
                                 "nothing was deployed")
        self.u.step(f"building the {self.engine} daemon (the running daemon keeps serving)...")
        code, _ = self.command(["compose", *compose, "build", up.DAEMON_SERVICE], env=self.env, timeout=7200, stream=not self.o.as_json)
        if code:
            raise up.UpdateError("engine build failed; the running daemon is unchanged; nothing was deployed")
        self.built = True
        if self.engine == "rust":
            # The config the replacement runs with: its model directory and volume.
            provision_models(self.u, self.command, image, self.config(compose))

    def run(self):
        if self.o.shadow and self.engine == "python":
            from pseudolife_memory.update_shadow import ShadowUpdate
            return ShadowUpdate(self).remove()
        self.preflight()
        from pseudolife_memory import disk_space
        disk_space.host(self.repo)
        disk_space.docker_storage(self.u.docker, [self.pg, self.daemon], self.running.get("Image"), self.u.warn)
        if self.o.shadow:
            from pseudolife_memory.update_shadow import ShadowUpdate
            return ShadowUpdate(self).start()
        if not self.stopped_handover:
            # The backup stages in both containers' /tmp: refuse before the build.
            disk_space.container(self.u.docker, self.pg)
            disk_space.container(self.u.docker, self.daemon)
        # Tag first: rebuilding the same engine moves its :checkout tag, and
        # Docker's containerd image store then no longer resolves the
        # running image's ID, so it could not be tagged afterwards.
        rollback = self.tag_rollback()
        image = f"pseudolife-daemon-{self.engine}:checkout"
        service = {"image": image, "build": {"dockerfile": f"ops/Dockerfile.daemon{'-rust' if self.engine == 'rust' else ''}"},
                   "labels": {ENGINE_LABEL: self.engine, CHECKOUT_LABEL: str(self.repo), ACTIVATION_LABEL: secrets.token_hex(16)}}
        candidate = self.repo / "data/update-engine/docker-compose.engine-candidate.json"
        rust_overlay = ["-f", str(self.ops / "docker-compose.rust-daemon.yml")]
        compose = [*self.base, *(rust_overlay if self.engine == "rust" else []), "-f", str(candidate)]
        self.built = False
        try:
            try:
                _write_overlay(candidate, service)
                self.build(compose, image)
                self.back_up(rollback)
            except (up.UpdateError, OSError) as exc:
                raise up.UpdateError(kept_note(str(exc), rollback, image if self.built else None),
                                     getattr(exc, "exit_code", 1)) from exc
            health = self.activate(compose, service, rust_overlay, rollback)
        finally:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                self.u.warn("could not remove the temporary engine overlay; the rollback instructions remain usable")
        self.u.step("selected Rust; future updates retain this engine; rollback commands:" if self.engine == "rust"
                    else "restored Python; future updates use the ordinary update path; rollback commands:")
        for line in self.u.rollback_lines:
            self.u.step(line)
        self.u.checkout_post_deploy(self.repo, self.previous_digest, health)
        self.u.prune_cache(self.repo)

    def tag_rollback(self):
        """Tag the validated running image for rollback, by its ID."""
        old_engine = self.running_labels.get(ENGINE_LABEL, "python")
        suffix = self.o.rollback_suffix or f"pre-update-{up._stamp()}"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", suffix):
            raise up.UpdateError("--rollback-tag must contain only letters, digits, dots, underscores or hyphens; nothing was deployed", 2)
        rollback = f"pseudolife-daemon-{old_engine}:{self.running_version}-{suffix}"
        exists, prior = self.command(["image", "inspect", "-f", "{{.Id}}", rollback], timeout=60)
        if exists == 0 and prior.strip() and prior.strip() != self.running["Image"]:
            if not self.o.force_rollback_tag:
                raise up.UpdateError("that rollback tag already names another image; choose a new --rollback-tag suffix "
                                     "or use --force-rollback-tag to replace it with the validated running image; nothing was deployed", 2)
            self.u.warn("--force-rollback-tag: replacing the existing rollback tag with the validated running image")
        code, _ = self.command(["tag", self.running["Image"], rollback], timeout=60)
        if code:
            raise up.UpdateError("could not tag the running image for rollback; nothing was deployed")
        self.u.step(f"tagged rollback image: {rollback}")
        self.u.report.rollback = {"tag": rollback, "state": "tagged"}
        return rollback

    def back_up(self, rollback):
        """The backup, after the build and models, just before the recreate."""
        from pseudolife_memory import disk_space
        self.backup()
        disk_space.host(self.repo)
        # By tag: after a same-engine rebuild the running image's ID may not resolve.
        disk_space.docker_storage(self.u.docker, [self.pg, self.daemon], rollback, self.u.warn)
        if self.stopped_handover:
            current = self.inspect(self.daemon)
            if current.get("Image") != self.running.get("Image") or current.get("State", {}).get("Running") is not False or current.get("Config") != self.running.get("Config"):
                raise up.UpdateError("the stopped daemon changed during backup; stop other updates and retry; nothing was deployed", 2)

    def activate(self, compose, service, rust_overlay, rollback):
        """Write the rollback overlay and commands, recreate on the built image and gate it."""
        old_engine = self.running_labels.get(ENGINE_LABEL, "python")
        old_service = {"image": rollback, "build": {"dockerfile": f"ops/Dockerfile.daemon{'-rust' if old_engine == 'rust' else ''}"},
                       "labels": {ENGINE_LABEL: old_engine, CHECKOUT_LABEL: str(self.repo)}}
        activation = self.running_labels.get(ACTIVATION_LABEL)
        if activation:
            old_service["labels"][ACTIVATION_LABEL] = activation
        rollback_file = self.repo / "data/update-engine" / f"docker-compose.rollback-{up._stamp()}.json"
        _write_overlay(rollback_file, old_service)
        selected_file = self.repo / SELECTION
        # A restored Rust daemon keeps the model directory it ran with: the
        # overlay's /models volume only when it already used it.
        rollback_args = [*self.base, *(rust_overlay if old_engine == "rust" and uses_models_volume(self.running) else []),
                         "-f", str(rollback_file)]
        shown = " ".join('"' + str(a).replace('"', '\\"') + '"' for a in rollback_args)
        accepted_file = selected_file if old_engine == "rust" else self.repo / PYTHON_ACCEPTED
        copy = (f'Copy-Item -LiteralPath "{rollback_file}" -Destination "{accepted_file}" -ErrorAction Stop' if os.name == "nt"
                else f'cp -- "{rollback_file}" "{accepted_file}"')
        clear = ("Remove-Item Env:PSEUDOLIFE_MCP_TOKEN,Env:PSEUDOLIFE_MCP_TOKENS -ErrorAction SilentlyContinue" if os.name == "nt"
                 else "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS")
        restore = f"docker compose {shown} up -d --no-deps --no-build {up.DAEMON_SERVICE}"
        commit = copy
        if old_engine == "python":
            commit += (f'; Remove-Item -LiteralPath "{selected_file}" -ErrorAction SilentlyContinue'
                       if os.name == "nt" else f' && rm -f -- "{selected_file}"')
        # The accepted deployment stays valid if Compose cannot replace it.
        restore += (f'; if ($LASTEXITCODE -eq 0) {{ {commit} }} else {{ throw "Rollback recreation failed; accepted engine selection was not changed" }}'
                    if os.name == "nt" else " && " + commit)
        if up._env_file_sets_credentials(self.env_file):
            commands = [clear, restore]
        else:
            fix = "Restore PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKENS in this shell before rollback"
            restore = (f'if ($env:PSEUDOLIFE_MCP_TOKEN -or $env:PSEUDOLIFE_MCP_TOKENS) {{ {restore} }} else {{ throw "{fix}" }}'
                       if os.name == "nt" else
                       'if [ -n "${PSEUDOLIFE_MCP_TOKEN:-}${PSEUDOLIFE_MCP_TOKENS:-}" ]; then ' + restore +
                       f'; else echo "{fix}" >&2; false; fi')
            commands = [restore]
        self.u.rollback_lines = [*commands, *self.u._schema_rollback_note(None)]
        self.u.report.rollback = {"tag": rollback, "state": "tagged", "overlay": str(rollback_file), **self.backup_paths}
        # Retention protects images used by running containers. Prune while
        # the image this run tagged is still running, even after a downgrade.
        for engine in ("python", "rust"):
            self.u.prune_rollbacks(self.repo, f"pseudolife-daemon-{engine}")
        try:
            self.u.step(f"recreating the {self.engine} daemon; rollback image {rollback}")
            code, _ = self.command(["compose", *compose, "up", "-d", "--no-deps", "--no-build", up.DAEMON_SERVICE], env=self.env, timeout=120, stream=not self.o.as_json)
            if code:
                raise up.UpdateError("engine recreate failed")
            health = self.gate(self.o.daemon_url)
            if self.engine == "rust":
                _write_overlay(selected_file, service)
            else:
                _write_overlay(self.repo / PYTHON_ACCEPTED, service)
                selected_file.unlink(missing_ok=True)
        except (up.UpdateError, OSError) as exc:
            raise up.UpdateError(str(exc) + "\nto roll back:\n" + "\n".join(self.u.rollback_lines)) from exc
        return health
