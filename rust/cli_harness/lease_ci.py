"""Start the disposable PostgreSQL fixture used by the CLI lease row."""

from __future__ import annotations

import io
import ipaddress
import os
import re
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote


def diagnostic(exc, dsn=""):
    from psycopg.conninfo import conninfo_to_dict
    text = str(exc) or type(exc).__name__
    if dsn:
        text = text.replace(dsn, "<redacted dsn>")
        try:
            options = conninfo_to_dict(dsn)
        except Exception:
            options = {}
        for field in ("password", "passfile"):
            if options.get(field):
                text = text.replace(options[field], "***").replace(quote(options[field], safe=""), "***")
    text = re.sub(r"(postgres(?:ql)?://)[^\s/@]+@", r"\1***@", text, flags=re.I)
    text = re.sub(r"(?i)(password|passfile|token|api[_-]?key)(?:\s*[=:]\s*|\s+)"
                  r"(?:'[^']*'|\"[^\"]*\"|[^\s,)]+)", r"\1=***", text)
    text = text.replace(str(Path.home()), "{HOME}")
    return text[-2000:]


def main() -> int:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from pseudolife_memory.storage.embedded_pg import attach_or_start
    from pseudolife_memory.test_login_cli import main as test_login_main

    runner_temp = Path(os.environ["RUNNER_TEMP"])
    # The runner staging root's group ACL can deny initdb's chmod. Use the
    # user profile like the existing lite CI lane; leave test TEMP unchanged.
    data_dir = Path(tempfile.mkdtemp(prefix="pl-cli-lease-postgres-", dir=Path.home()))
    login_file = runner_temp / "test-pg.env"
    dsn = ""

    try:
        dsn, owned = attach_or_start(data_dir)
        if owned is None:
            raise RuntimeError("embedded PostgreSQL was unexpectedly already running")

        options = conninfo_to_dict(dsn)
        host = options.get("host", "127.0.0.1")
        port = options.get("port", "")
        if host == "localhost":
            host = "127.0.0.1"
        address = ipaddress.ip_address(host.strip("[]"))
        if address.version != 4 or not address.is_loopback or not port.isdigit():
            raise RuntimeError("embedded PostgreSQL did not provide a loopback TCP endpoint")
        host = str(address)

        login_file.parent.mkdir(parents=True, exist_ok=True)
        admin_url = make_conninfo(
            host=host,
            port=port,
            user=options.get("user", ""),
            dbname="postgres",
        )
        old_pgpassword = os.environ.get("PGPASSWORD")
        old_daemon_dsn = os.environ.pop("PSEUDOLIFE_MCP_DATABASE_URL", None)
        if options.get("password"):
            os.environ["PGPASSWORD"] = options["password"]
        try:
            code = test_login_main(
                ["create", "--rotate", "--file", str(login_file), "--admin-url", admin_url],
                out=io.StringIO(),
            )
        finally:
            if old_pgpassword is None:
                os.environ.pop("PGPASSWORD", None)
            else:
                os.environ["PGPASSWORD"] = old_pgpassword
            if old_daemon_dsn is not None:
                os.environ["PSEUDOLIFE_MCP_DATABASE_URL"] = old_daemon_dsn

        if code != 0:
            print(f"test-login fixture provisioning failed (exit {code})", file=sys.stderr)
            return code

        host_port = f"{host}:{port}"
        if any(character in host_port or character in str(login_file) for character in "\r\n"):
            raise RuntimeError("fixture values cannot be written to GITHUB_ENV")
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8", newline="") as env_file:
            env_file.write(f"PSEUDOLIFE_TEST_PG_HOST_PORT={host_port}\n")
            env_file.write(f"PSEUDOLIFE_TEST_PG_LOGIN_FILE={login_file}\n")
    except Exception as exc:  # noqa: BLE001 - never expose a connection string
        print(f"lease PostgreSQL fixture setup failed ({type(exc).__name__})", file=sys.stderr)
        print(diagnostic(exc, dsn), file=sys.stderr)
        return 1

    print("Disposable PostgreSQL and isolated test login are ready for the lease row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
