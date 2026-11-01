"""Manage the opt-in read-only companion without recreating the writer."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import quote

from pseudolife_memory import update_cli as up
from pseudolife_memory.client_config import _write_private

SERVICE = "pseudolife-rust-shadow"
CONFIG_LABEL = "org.pseudolife.shadow.configuration"
PASSWORD = "PSEUDOLIFE_SHADOW_DB_PASSWORD"
REMOVE = "update --engine python --shadow --checkout <repo>"
RETRY = "update --engine rust --shadow --checkout <repo>"


def require_support(repo):
    required = ("ops/docker-compose.rust-shadow.yml", "ops/Dockerfile.daemon-rust",
                "ops/sql/shadow_reader_role.sql", "rust/daemon/src/config.rs")
    if (any(not (repo / name).is_file() for name in required)
            or "PSEUDOLIFE_READ_ONLY" not in (repo / required[-1]).read_text(encoding="utf-8")):
        raise up.UpdateError("shadow support is missing (overlay, Rust image, SELECT-only role or PSEUDOLIFE_READ_ONLY); "
                             "update the checkout to current master and retry; nothing was changed", 2)


def _dsn(value):
    from psycopg import Error
    from psycopg.conninfo import conninfo_to_dict
    try:
        return conninfo_to_dict(value or "")
    except Error:
        # Driver errors can include the supplied URL and password.
        raise up.UpdateError("cannot resolve the shadow bank URL; restore the project's PostgreSQL URL and retry; nothing was changed", 2) from None


class ShadowUpdate:
    def __init__(self, engine):
        self.e, self.u = engine, engine.u
        self.env = dict(engine.env)
        self.overlay = engine.ops / "docker-compose.rust-shadow.yml"
        if not self.overlay.is_file():
            raise up.UpdateError("shadow overlay is missing; restore ops/docker-compose.rust-shadow.yml and retry; nothing was changed", 2)
        self.compose = [*engine.base, "-f", str(self.overlay)]

    def command(self, args, **kw):
        return self.e.command(["compose", *self.compose, *args], env=self.env, **kw)

    def container(self):
        code, out = self.command(["ps", "-a", "-q", SERVICE], timeout=60)
        ids = out.split()
        if code or len(ids) > 1:
            raise up.UpdateError("cannot identify the project's shadow; restore its Compose configuration and retry; nothing was changed", 2)
        return ids[0] if ids else None

    def remove(self):
        # No bank, token, model or healthy-writer dependency on the recovery
        # path. The placeholder only resolves Compose; rm creates no service.
        self.env[PASSWORD] = "unused-for-removal"
        if not self.container():
            self.u.step("no Rust shadow is installed; Python is unchanged")
            return
        code, _ = self.command(["rm", "--stop", "--force", SERVICE], timeout=120)
        if code:
            raise up.UpdateError(f"could not remove the Rust shadow; retry {REMOVE}; Python is unchanged")
        self.u.step("removed the Rust shadow; Python is unchanged (model cache and password retained for reuse)")

    def prepare(self):
        e = self.e
        self.original_bytes = e.env_file.read_bytes() if e.env_file.is_file() else None
        original = self.original_bytes.decode("utf-8") if self.original_bytes is not None else ""
        assignments = re.findall(r"^\s*(?:export\s+)?PSEUDOLIFE_SHADOW_DB_PASSWORD\s*=(.*)$", original, re.M)
        if assignments and (len(assignments) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", assignments[0].strip())):
            raise up.UpdateError("PSEUDOLIFE_SHADOW_DB_PASSWORD must be one URL-safe value in the host env file; fix it and retry; nothing was changed", 2)
        self.password = assignments[0].strip() if assignments else secrets.token_urlsafe(32)
        self.original, self.save_password = original, not assignments
        self.env[PASSWORD] = self.password  # Host file wins over an inherited shell value.
        self.env["PSEUDOLIFE_SHADOW_DB_NAME"] = quote(e.pg_db, safe="")
        base = e.config(e.base)
        config = e.config(self.compose, self.env)
        main = base["services"][up.DAEMON_SERVICE]
        service = config["services"].get(SERVICE, {})
        settings = service.get("environment", {})
        writer = _dsn(main.get("environment", {}).get("PSEUDOLIFE_MCP_DATABASE_URL"))
        reader = _dsn(settings.get("PSEUDOLIFE_MCP_DATABASE_URL"))
        expected = {"host": "pseudolife-pg", "port": "5432", "dbname": e.pg_db}
        if (any(writer.get(k) != v or reader.get(k) != v for k, v in expected.items())
                or reader.get("user") != "pseudolife_shadow" or reader.get("password") != self.password):
            raise up.UpdateError("shadow must use pseudolife_shadow on the writer's project PostgreSQL bank; restore its Compose bank settings and retry; nothing was changed", 2)
        if (settings.get("PSEUDOLIFE_READ_ONLY") != "1"
                or any(settings.get(k, "") != main.get("environment", {}).get(k, "")
                       for k in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS"))):
            raise up.UpdateError("shadow needs read-only mode and the writer's bearer configuration; restore the shadow overlay and retry; nothing was changed", 2)
        actual = next(v for v in e.running["Mounts"] if v.get("Destination") == "/data")
        mounts = [v for v in service.get("volumes", []) if v.get("target") == "/data"]
        if (len(mounts) != 1 or not mounts[0].get("read_only") or mounts[0].get("type") != "volume"
                or config.get("volumes", {}).get(mounts[0].get("source"), {}).get("name") != actual["Name"]
                or config["services"].get(up.DAEMON_SERVICE) != main):
            raise up.UpdateError("shadow must mount the writer's /data read-only and leave Python unchanged; restore the shadow overlay and retry; nothing was changed", 2)
        ports = service.get("ports", [])
        if (len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or ports[0].get("target") != 8765
                or str(ports[0].get("published")) in {str(p.get("published")) for p in main.get("ports", [])}):
            raise up.UpdateError("shadow needs its own loopback port; restore PSEUDOLIFE_SHADOW_PORT=8766 and the shadow overlay, then retry; nothing was changed", 2)
        self.url = f"http://127.0.0.1:{ports[0]['published']}"
        # Bind no-op detection to the complete resolved service and volume
        # configuration, excluding only this label and the build timestamp.
        stable = json.loads(json.dumps(service))
        stable.get("labels", {}).pop(CONFIG_LABEL, None)
        stable.get("build", {}).get("args", {}).pop("BUILD_TIME", None)
        payload = {"service": stable, "volumes": config.get("volumes", {}), "revision": e.tree["sha"]}
        self.fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.env["PSEUDOLIFE_SHADOW_CONFIGURATION"] = self.fingerprint
        self.existing = self.container()

    def apply_role(self):
        e = self.e
        remote = "/tmp/pseudolife-shadow-" + secrets.token_hex(12) + ".sql"
        with tempfile.TemporaryDirectory(prefix="pseudolife-shadow-") as tmp:
            script = Path(tmp) / "role.sql"
            _write_private(script, ("\\set shadow_password '" + self.password + "'\n" +
                                    (e.ops / "sql/shadow_reader_role.sql").read_text(encoding="utf-8")).encode())
            try:
                code, _ = e.command(["cp", str(script), f"{e.pg}:{remote}"], timeout=60)
                if not code:
                    code, _ = e.command(["exec", e.pg, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", e.pg_user,
                                         "-d", e.pg_db, "-f", remote], timeout=60)
                if code:
                    raise up.UpdateError("could not apply the SELECT-only shadow role; check PostgreSQL access and the role script's prerequisites, then retry")
            finally:
                code, _ = e.command(["exec", e.pg, "rm", "-f", remote], timeout=60)
                if code:
                    raise up.UpdateError(f"could not remove the private role script from the PostgreSQL container; remove {remote} there before retrying")

    def start(self):
        self.prepare()
        e = self.e
        if self.existing and e.tree["dirty"] == "false":
            current = e.inspect(self.existing)
            if (current.get("State", {}).get("Running")
                    and (current.get("Config", {}).get("Labels") or {}).get(CONFIG_LABEL) == self.fingerprint):
                try:
                    e.gate(self.url, retries=1, announce=False)
                except up.UpdateError:
                    pass
                else:
                    self.u.step(f"read-only Rust shadow is already healthy at {self.url}; nothing changed")
                    return
        self.u.step("building the read-only Rust shadow (Python remains running)...")
        code, _ = self.command(["build", SERVICE], timeout=7200)
        if code:
            raise up.UpdateError(f"shadow build failed; Python is unchanged; retry {RETRY}")
        now = up.tree_state(e.repo)
        if now["sha"] != e.tree["sha"] or now["dirty"] != e.tree["dirty"]:
            raise up.UpdateError(f"the checkout moved during the shadow build; Python is unchanged; retry {RETRY}")
        started = False
        try:
            current_env = e.env_file.read_bytes() if e.env_file.is_file() else None
            if current_env != self.original_bytes:
                raise up.UpdateError("the host env file changed during the shadow build; its edits were preserved; retry after the configuration is settled")
            if self.save_password:
                _write_private(e.env_file, (self.original.rstrip() + "\n" + PASSWORD + "=" + self.password + "\n").encode())
            self.apply_role()
            started = True
            # S1 permits 30 minutes of first-start provisioning plus 90 seconds
            # of init. Compose's real readiness must precede the read gate.
            code, _ = self.command(["up", "-d", "--no-deps", "--no-build", "--wait", "--wait-timeout", "1920", SERVICE], timeout=1980)
            if code:
                raise up.UpdateError("shadow startup did not become ready within its provisioning budget")
            e.gate(self.url, announce=False)
        except (up.UpdateError, OSError) as exc:
            cleanup = 0
            if started:
                cleanup, _ = self.command(["rm", "--stop", "--force", SERVICE], timeout=120)
            recovery = f"remove it with {REMOVE}, then retry {RETRY}" if cleanup else f"retry {RETRY}"
            reason = str(exc) if isinstance(exc, up.UpdateError) else "cannot save the private shadow setup files"
            raise up.UpdateError(f"{reason}; Python is unchanged; {recovery} (the saved password is retained)") from None
        self.u.step(f"read-only Rust shadow answers at {self.url}; Python is unchanged; remove it with {REMOVE}")
