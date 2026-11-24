"""Start the disposable PostgreSQL fixture used by the CLI lease row.

The cluster comes from ``rust/ci/disposable_postgres.py`` (the pg0 wheel,
not the Python package) and the isolated test login from the native
client's ``test-login create``: the release binary the job built or
downloaded, or ``PSEUDOLIFE_LEASE_CI_BINARY`` when set.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_DATABASE_TURN_MARKER = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"
_ROOT = Path(__file__).resolve().parents[2]
import sys
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


def _helper():
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from rust.ci import disposable_postgres  # noqa: PLC0415
    return disposable_postgres


def main() -> int:
    if _DATABASE_TURN_MARKER.exists():
        _turn_sys = __import__("sys")
        _turn_root = str(__import__("pathlib").Path(__file__).resolve().parents[2])
        _turn_sys.path.insert(0, _turn_root)
        try:
            from rust.harness_support.database_turn import require_turn

            require_turn(holder="standalone database harness", contain=True)
        finally:
            _turn_sys.path.remove(_turn_root)
    postgres = _helper()

    runner_temp = Path(os.environ["RUNNER_TEMP"])
    login_file = runner_temp / "test-pg.env"
    binary = Path(os.environ.get("PSEUDOLIFE_LEASE_CI_BINARY") or postgres.native_binary(_ROOT))
    secret = ""

    try:
        if not binary.is_file():
            raise postgres.SetupError(f"the native client is missing at {binary}; build or download it first")
        if any(character in str(login_file) for character in "\r\n"):
            raise RuntimeError("fixture values cannot be written to GITHUB_ENV")
        # The runner staging root's group ACL can deny initdb's chmod. Use the
        # user profile like the existing lite CI lane; leave test TEMP unchanged.
        server = postgres.start(Path.home(), "pl-cli-lease-postgres-")
        secret = server.password
        host_port = f"{server.host}:{server.port}"  # a checked IPv4 address and an int
        login_file.parent.mkdir(parents=True, exist_ok=True)
        result = postgres.create_test_login(binary, server, login_file)
        if result.returncode != 0:
            print(f"test-login fixture provisioning failed (exit {result.returncode})", file=sys.stderr)
            reason = postgres.failure_reason(result)
            print(diagnostic(RuntimeError(reason), f"password={secret}"), file=sys.stderr)
            return result.returncode

        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8", newline="") as env_file:
            env_file.write(f"PSEUDOLIFE_TEST_PG_HOST_PORT={host_port}\n")
            env_file.write(f"PSEUDOLIFE_TEST_PG_LOGIN_FILE={login_file}\n")
    except Exception as exc:  # noqa: BLE001 - never expose a connection string
        print(f"lease PostgreSQL fixture setup failed ({type(exc).__name__})", file=sys.stderr)
        print(diagnostic(exc, f"password={secret}" if secret else ""), file=sys.stderr)
        return 1

    print("Disposable PostgreSQL and isolated test login are ready for the lease row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
