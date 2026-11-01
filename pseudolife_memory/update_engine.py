"""Opt-in Docker engine changes, using the existing update lock and backup scripts.

The host selection is a Compose overlay, saved only after the health gate.
Rollback overlays name the actual running image ID's tag and carry the old
engine selection, so an unhealthy replacement cannot become the next rollback.
"""
from __future__ import annotations

import json
import gzip
import os
import re
import secrets
import tempfile
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pseudolife_memory import update_cli as up

SELECTION = "data/update-engine/docker-compose.engine.json"
ENGINE_LABEL = "org.pseudolife.engine"
CHECKOUT_LABEL = "org.pseudolife.checkout"
SHADOW_SERVICE = "pseudolife-rust-shadow"


def require_imported_bank(schema, pending: bool) -> None:
    if type(schema) is not int or schema < 57 or pending:
        raise up.UpdateError("Rust activation requires schema v57 or later and imported counters: run "
                             "update --engine python --checkout <repo>, let the Python daemon import "
                             "memory_state/weights.pt into cms_counters, then retry; nothing was changed", 2)


def _selection(repo: Path) -> dict | None:
    path = repo / SELECTION
    if not path.exists():
        return None
    try:
        service = json.loads(path.read_text(encoding="utf-8"))["services"][up.DAEMON_SERVICE]
        if service["labels"][ENGINE_LABEL] not in ("rust", "python"):
            raise ValueError("engine")
        return service
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise up.UpdateError(f"invalid engine selection at {path}; restore its rollback copy before updating", 2) from exc


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
        labels = json.loads(out) if code == 0 else {}
    except ValueError:
        labels = {}
    current = "rust" if labels.get(ENGINE_LABEL) == "rust" else "python"
    if selected and selected["labels"][ENGINE_LABEL] != current:
        raise up.UpdateError("this checkout's engine selection disagrees with the host; use --engine python --checkout <repo> "
                             "to restore the default, or --engine rust --checkout <repo> to explicitly activate Rust; nothing was changed", 2)
    if labels.get(ENGINE_LABEL) != "rust":
        if o.engine == "python":
            o.daemon_only = True
        return None
    if o.clients_only:
        return None
    origin = Path(labels.get(CHECKOUT_LABEL, ""))
    selected = _selection(origin) if labels.get(CHECKOUT_LABEL) else None
    if not selected or selected["labels"][ENGINE_LABEL] != "rust":
        raise up.UpdateError("the running Rust container has no validated engine selection; use the previous "
                             "update's rollback commands to restore Python before retrying; nothing was changed", 2)
    if o.checkout is not None:
        update._selected_engine = selected["labels"][ENGINE_LABEL]
        return repo
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
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            result = json.loads(response.read())
            return isinstance(result, dict) and isinstance(result.get("entries"), list) and not result.get("error")
    except (OSError, ValueError, urllib.error.URLError):
        return False


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
        # Read the complete tar and gzip trailer, without extracting anything.
        with gzip.open(state, "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for member in archive:
                    if member.isfile():
                        with archive.extractfile(member) as content:
                            while content.read(1024 * 1024):
                                pass
            while stream.read(1024 * 1024):
                pass
    except (OSError, ValueError, EOFError, tarfile.TarError) as exc:
        raise up.UpdateError("the bank or state backup is missing or incomplete; fix the backup error and retry; nothing was deployed") from exc
    return {"dump": str(dump), "state_archive": str(state)}


def deploy_engine(update, repo: Path) -> None:
    EngineUpdate(update, repo).run()


class EngineUpdate:
    def __init__(self, update, repo: Path):
        self.u, self.o, self.repo = update, update.o, repo
        self.ops = repo / "ops"
        self.env_file = self.ops / ".env"
        self.selected = _selection(repo)
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
        code, out = self.command(["exec", self.pg, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", self.pg_user,
                                  "-d", self.pg_db, "-Atc", query], timeout=60)
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
        if not self.running.get("State", {}).get("Running"):
            raise up.UpdateError("the current daemon must be running for the backup and rollback tag; start it and retry", 2)
        daemon_env = config["services"][up.DAEMON_SERVICE].get("environment", {})
        running_env = dict(item.split("=", 1) for item in self.running["Config"].get("Env", []) if "=" in item)
        if running_env.get("PSEUDOLIFE_MCP_DATABASE_URL") != daemon_env.get("PSEUDOLIFE_MCP_DATABASE_URL"):
            raise up.UpdateError("the host Compose configuration names a different bank from the running daemon; restore its original env file before switching; nothing was changed", 2)
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
        self.token = daemon_env.get("PSEUDOLIFE_MCP_TOKEN") or next(iter(parse_token_map(daemon_env.get("PSEUDOLIFE_MCP_TOKENS"))), "")
        if not self.token or "\r" in self.token or "\n" in self.token:
            raise up.UpdateError("the engine health gate needs bearer auth; set PSEUDOLIFE_MCP_TOKEN in the host env file and retry; nothing was changed", 2)
        if self.o.shadow:
            if self.running["Config"].get("Labels", {}).get(ENGINE_LABEL) == "rust":
                raise up.UpdateError("shadow mode needs the Python daemon; run update --engine python --checkout <repo> first", 2)
            if not (self.ops / "sql/shadow_reader_role.sql").is_file() or "PSEUDOLIFE_READ_ONLY" not in (self.repo / "rust/daemon/src/config.rs").read_text(encoding="utf-8"):
                raise up.UpdateError("shadow support is missing (ops/sql/shadow_reader_role.sql and PSEUDOLIFE_READ_ONLY); update the checkout after the read-only support lands, then retry; nothing was changed", 2)
        if self.engine == "rust":
            raw = self.sql("SELECT value::text FROM meta WHERE key = 'schema_version'")
            try:
                schema = int(raw.strip('"'))
            except ValueError:
                schema = None
            code, _ = self.command(["exec", self.daemon, "sh", "-c",
                                    "test ! -e /data/memory_state/weights.pt && test ! -e /data/memory_state/weights.pt.bak"], timeout=60)
            require_imported_bank(schema, code != 0)
        before = self.u.daemon_health() or {}
        if before.get("status") != "ok" or not authenticated_read(self.o.daemon_url, self.token):
            raise up.UpdateError("the running daemon did not pass an authenticated bank read; use the previous update's rollback commands before retrying; no rollback tag was moved", 2)
        self.u.schema_before = before.get("schema")
        self.previous_digest = before.get("hooks_digest")
        self.running_version = before.get("version") or "unknown"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.running_version):
            raise up.UpdateError("the running daemon reported an invalid version; restore its previous image before retrying", 2)
        self.version = re.search(r'^version\s*=\s*"([^"]+)"', (self.repo / "pyproject.toml").read_text(), re.M).group(1)
        self.schema = int(re.search(r"^SCHEMA_META_VERSION\s*=\s*(\d+)", (self.repo / "pseudolife_memory/storage/schema.py").read_text(), re.M).group(1))

    def backup(self):
        script = self.u._checkout_script(self.repo, "backup")
        if script is None:
            raise up.UpdateError("the checkout backup script is missing; restore ops/backup.sh or ops/backup.ps1 and retry", 2)
        keys = (["-Container", "-DaemonContainer", "-User", "-Db"] if script.suffix == ".ps1"
                else ["--container", "--daemon-container", "--user", "--db"])
        args = self.u._script_argv(script)
        for key, value in zip(keys, (self.pg, self.daemon, self.pg_user, self.pg_db)):
            args.extend([key, value])
        directory = self.repo / "data/backups"
        before = _backup_files(directory)
        args.extend(["-OutDir" if script.suffix == ".ps1" else "--out-dir", str(directory)])
        self.u.step("backing up the bank and /data before switching engines...")
        code, _ = up.run_cli(args, timeout=3600, stream=not self.o.as_json)
        if code:
            raise up.UpdateError("the backup failed; nothing was deployed")
        self.backup_paths = _verify_backup(directory, before)

    def gate(self, url):
        for attempt in range(self.o.health_retries):
            health = up.fetch_json(url.rstrip("/") + "/health", timeout=3)
            if (health and health.get("status") == "ok" and health.get("version") == self.version
                    and health.get("schema") == self.schema and not health.get("counters_import_required")
                    and (not self.o.shadow or health.get("read_only") is True)
                    and authenticated_read(url, self.token)):
                self.u.step(f"{self.engine} health gate passed: version {self.version}, schema {self.schema}, authenticated bank read")
                return health
            if attempt + 1 < self.o.health_retries:
                up.sleep(self.o.health_delay_ms / 1000)
        raise up.UpdateError("the engine failed its version, schema or authenticated-read gate")

    def run(self):
        self.preflight()
        if self.o.shadow:
            return self.shadow()
        self.backup()
        old_engine = self.running["Config"].get("Labels", {}).get(ENGINE_LABEL, "python")
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
        old_service = {"image": rollback, "build": {"dockerfile": f"ops/Dockerfile.daemon{'-rust' if old_engine == 'rust' else ''}"},
                       "labels": {ENGINE_LABEL: old_engine, CHECKOUT_LABEL: str(self.repo)}}
        rollback_file = self.repo / "data/update-engine" / f"docker-compose.rollback-{up._stamp()}.json"
        _write_overlay(rollback_file, old_service)
        selected_file = self.repo / SELECTION
        rollback_args = [*self.base, "-f", str(selected_file if old_engine == "rust" else rollback_file)]
        shown = " ".join('"' + str(a).replace('"', '\\"') + '"' for a in rollback_args)
        copy = (f'Copy-Item -LiteralPath "{rollback_file}" -Destination "{selected_file}"' if os.name == "nt"
                else f'cp -- "{rollback_file}" "{selected_file}"')
        clear = ("Remove-Item Env:PSEUDOLIFE_MCP_TOKEN,Env:PSEUDOLIFE_MCP_TOKENS -ErrorAction SilentlyContinue" if os.name == "nt"
                 else "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS")
        restore = f"docker compose {shown} up -d --no-deps --no-build {up.DAEMON_SERVICE}"
        if old_engine == "python":
            restore += (f'; if ($LASTEXITCODE -eq 0) {{ Remove-Item -LiteralPath "{selected_file}" -ErrorAction SilentlyContinue }}'
                        if os.name == "nt" else f' && rm -f -- "{selected_file}"')
        self.u.rollback_lines = ([copy] if old_engine == "rust" else []) + [clear, restore, *self.u._schema_rollback_note(None)]
        self.u.report.rollback = {"tag": rollback, "state": "tagged", "overlay": str(rollback_file), **self.backup_paths}
        # Retention protects images used by running containers. Prune while
        # the image this run tagged is still running, even after a downgrade.
        for engine in ("python", "rust"):
            self.u.prune_rollbacks(self.repo, f"pseudolife-daemon-{engine}")
        image = f"pseudolife-daemon-{self.engine}:checkout"
        service = {"image": image, "build": {"dockerfile": f"ops/Dockerfile.daemon{'-rust' if self.engine == 'rust' else ''}"},
                   "labels": {ENGINE_LABEL: self.engine, CHECKOUT_LABEL: str(self.repo)}}
        candidate = self.repo / "data/update-engine/docker-compose.engine-candidate.json"
        _write_overlay(candidate, service)
        compose = list(self.base)
        if self.engine == "rust":
            compose += ["-f", str(self.ops / "docker-compose.rust-daemon.yml")]
        compose += ["-f", str(candidate)]
        try:
            self.u.step(f"building the {self.engine} daemon; rollback image {rollback}")
            now = up.tree_state(self.repo)
            if now["sha"] != self.tree["sha"] or now["dirty"] != self.tree["dirty"]:
                raise up.UpdateError("the checkout moved during backup; retry from a stable checkout")
            code, _ = self.command(["compose", *compose, "build", up.DAEMON_SERVICE], env=self.env, timeout=7200, stream=not self.o.as_json)
            if code:
                raise up.UpdateError("engine build failed; the running daemon is unchanged")
            code, _ = self.command(["compose", *compose, "up", "-d", "--no-deps", "--no-build", up.DAEMON_SERVICE], env=self.env, timeout=120, stream=not self.o.as_json)
            if code:
                raise up.UpdateError("engine recreate failed")
            health = self.gate(self.o.daemon_url)
            if self.engine == "rust":
                _write_overlay(selected_file, service)
            else:
                selected_file.unlink(missing_ok=True)
        except up.UpdateError as exc:
            raise up.UpdateError(str(exc) + "\nto roll back:\n" + "\n".join(self.u.rollback_lines)) from exc
        finally:
            candidate.unlink(missing_ok=True)
        self.u.step("selected Rust; future updates retain this engine; rollback commands:" if self.engine == "rust"
                    else "restored Python; future updates use the ordinary update path; rollback commands:")
        for line in self.u.rollback_lines:
            self.u.step(line)
        self.u.checkout_post_deploy(self.repo, self.previous_digest, health)
        self.u.prune_cache(self.repo)

    def shadow(self):
        from pseudolife_memory.client_config import _write_private

        # The password is generated once and is never returned in a report or
        # placed in argv. Retaining it after failure makes retry idempotent.
        original = self.env_file.read_text(encoding="utf-8") if self.env_file.is_file() else ""
        match = re.search(r"^PSEUDOLIFE_SHADOW_DB_PASSWORD=([A-Za-z0-9_-]+)\s*$", original, re.M)
        if "PSEUDOLIFE_SHADOW_DB_PASSWORD=" in original and not match:
            raise up.UpdateError("PSEUDOLIFE_SHADOW_DB_PASSWORD must be a URL-safe value in the host env file; fix it and retry", 2)
        password = match.group(1) if match else secrets.token_urlsafe(32)
        if not match:
            _write_private(self.env_file, (original.rstrip() + "\nPSEUDOLIFE_SHADOW_DB_PASSWORD=" + password + "\n").encode())
        self.env.pop("PSEUDOLIFE_SHADOW_DB_PASSWORD", None)
        compose = [*self.u._compose_args([self.ops / "docker-compose.yml", self.ops / "docker-compose.override.yml",
                                         self.ops / "docker-compose.rust-shadow.yml"], self.env_file)]
        config = self.config(compose)
        port = config["services"][SHADOW_SERVICE]["ports"][0]
        if port.get("host_ip") != "127.0.0.1":
            raise up.UpdateError("shadow publication must use loopback; restore 127.0.0.1 in the shadow overlay and retry", 2)
        url = f"http://127.0.0.1:{port['published']}"
        self.u.step("building the read-only Rust shadow (Python remains running)...")
        code, _ = self.command(["compose", *compose, "build", SHADOW_SERVICE], env=self.env, timeout=7200, stream=not self.o.as_json)
        if code:
            raise up.UpdateError("shadow build failed; Python is unchanged; retry update --engine rust --shadow --checkout <repo>")
        remote = "/tmp/pseudolife-shadow-" + secrets.token_hex(12) + ".sql"
        with tempfile.TemporaryDirectory(prefix="pseudolife-shadow-") as tmp:
            script = Path(tmp) / "role.sql"
            _write_private(script, ("\\set shadow_password '" + password + "'\n" +
                                    (self.ops / "sql/shadow_reader_role.sql").read_text(encoding="utf-8")).encode())
            try:
                code, _ = self.command(["cp", str(script), f"{self.pg}:{remote}"], timeout=60)
                if not code:
                    code, _ = self.command(["exec", self.pg, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", self.pg_user,
                                            "-d", self.pg_db, "-f", remote], timeout=60)
                if code:
                    raise up.UpdateError("could not apply the SELECT-only shadow role; Python is unchanged; fix PostgreSQL access and retry")
            finally:
                self.command(["exec", self.pg, "rm", "-f", remote], timeout=60)
        code, _ = self.command(["compose", *compose, "up", "-d", "--no-deps", "--no-build", SHADOW_SERVICE],
                               env=self.env, timeout=120, stream=not self.o.as_json)
        self.u.rollback_lines = ["Python is unchanged; stop the companion with docker compose " +
                                 " ".join('"' + str(a) + '"' for a in compose) + " stop " + SHADOW_SERVICE]
        if code:
            raise up.UpdateError("shadow start failed; " + self.u.rollback_lines[0])
        try:
            self.gate(url)
        except up.UpdateError as exc:
            raise up.UpdateError(str(exc) + "\n" + self.u.rollback_lines[0]) from exc
        self.u.step(f"read-only Rust shadow answers at {url}; Python and the selected engine are unchanged")
