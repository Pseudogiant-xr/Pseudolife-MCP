"""``pseudolife-mcp maintainer``: the host side of maintainer passkeys (schema v54).

``setup`` does the whole first setup, guided (maintainer_setup.py).
``enrol-code`` prints the one-time code that admits the first passkey (10
base32 characters, 10 minutes, stored hashed; refused while any passkey is
pending or active) and waits for the Console to redeem it, then prints the
enrolled credential id prefix and label. ``confirm <prefix>`` makes that
pending key active, after the maintainer checked it matches what the
Console shows. ``revoke <prefix>`` revokes any key; ``reset`` revokes every
key, rotates the challenge secret and reopens bootstrap; ``list`` prints
the keys.

These reach the bank directly, through ``PSEUDOLIFE_MCP_DATABASE_URL`` or
the lite tier's embedded instance, so they need the database owner's
credentials, never a bearer token: that is what keeps an agent holding only
a bearer from enrolling, confirming or revoking a key. Run them on the
daemon host; with neither there, they re-run inside the Docker tier's daemon
container (daemon_exec.py). Exit status: 0 done, 1 refused, 2 nothing could be done.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import sys
import time

from pseudolife_memory.daemon_exec import NoBank, no_bank_message, run_in_daemon

EXIT_OK, EXIT_REFUSED, EXIT_ERROR = 0, 1, 2

_REFUSALS = {
    "enrolment_closed": "a passkey is already pending or active; revoke it or run "
                        "`pseudolife-mcp maintainer reset` first",
    "credential_not_found": "no single passkey matches that prefix (in the state this "
                            "command needs); run `pseudolife-mcp maintainer list`",
    "invalid_request": "the prefix must be at least 6 characters of the credential id",
}


class MaintainerCliError(ValueError):
    """Operator-safe diagnostic: never a connection string or a code hash."""


class _Storage:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction() as transaction:
            yield
        if transaction.status is not transaction.Status.COMMITTED:
            raise MaintainerCliError("the database did not confirm the commit")


@contextmanager
def _bank():
    from pseudolife_memory.backup_cli import _default_data_dir
    from pseudolife_memory.transfer_cli import _resolve_dsn
    dsn, own_instance = _resolve_dsn(_default_data_dir(os.environ))
    try:
        if not dsn:
            raise NoBank
        import psycopg
        conn = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        try:
            conn.execute("SET lock_timeout = '5s'")
            conn.execute("SET search_path TO public")
            present = conn.execute(
                "SELECT to_regclass('public.maintainer_passkeys') IS NOT NULL").fetchone()[0]
            if not present:
                raise MaintainerCliError("this bank has no maintainer tables yet (schema older "
                                         "than v54); start a v54 daemon once to create them")
            yield _Storage(conn)
        finally:
            conn.close()
    finally:
        if own_instance is not None:
            own_instance.stop()


def _store(storage):
    from pseudolife_memory.storage.maintainer import MaintainerStore
    # Host operations never verify an assertion, so no RP ID or origin.
    return MaintainerStore(storage, rp_id="", origin="")


def _when(value):
    return "-" if value is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def _enrol_code(store, args, out):
    from pseudolife_memory.storage.maintainer import BOOTSTRAP_TTL
    code = store.bootstrap_code()
    print(f"One-time enrolment code: {code}", file=out)
    print(f"Valid for {BOOTSTRAP_TTL // 60} minutes. Enter it in the Console with a label, "
          "then register your passkey.", file=out)
    if args.no_wait:
        return EXIT_OK
    deadline = time.monotonic() + BOOTSTRAP_TTL
    print("Waiting for the Console to redeem it...", file=out, flush=True)
    while time.monotonic() < deadline:
        redeemed = store.bootstrap_redeemed(code)
        if redeemed:
            prefix = redeemed["credential_id"][:12]
            print(f"Enrolled (pending): {prefix}  label: {redeemed['label']}", file=out)
            print("Check that the Console shows the same prefix and label, then run:\n"
                  f"  pseudolife-mcp maintainer confirm {prefix}\n"
                  "If it does not match, someone else redeemed the code: run\n"
                  f"  pseudolife-mcp maintainer revoke {prefix}", file=out)
            return EXIT_OK
        if store.bootstrap_burned(code):
            print("The code was burned after too many wrong guesses in the Console. If they "
                  "were not yours, someone else is trying to enrol: check `pseudolife-mcp "
                  "maintainer list`, then run enrol-code again.", file=out)
            return EXIT_REFUSED
        time.sleep(args.poll)
    print("The code expired unredeemed.", file=out)
    return EXIT_REFUSED


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["setup"]:
        # Guided, and mostly not a bank operation: it runs the others in
        # the daemon's environment (pseudolife_memory/maintainer_setup.py).
        from pseudolife_memory import maintainer_setup
        return maintainer_setup.main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp maintainer",
        description="Host-side management of the maintainer's passkeys (operator-only). "
                    "Reads PSEUDOLIFE_MCP_DATABASE_URL, or the lite tier's bank, else runs "
                    "inside the pseudolife-mcp-daemon container when it runs here.")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("setup", help="guided: the Console's HTTPS name, the daemon's config and "
                                     "the first passkey (see `maintainer setup --help`)")
    code = actions.add_parser("enrol-code", help="print a one-time code for the first passkey")
    code.add_argument("--no-wait", action="store_true",
                      help="print the code and exit without waiting for the Console")
    code.add_argument("--poll", type=float, default=2.0, help=argparse.SUPPRESS)
    confirm = actions.add_parser("confirm", help="activate the pending bootstrap passkey")
    confirm.add_argument("prefix")
    revoke = actions.add_parser("revoke", help="revoke one passkey")
    revoke.add_argument("prefix")
    reset = actions.add_parser("reset", help="revoke every passkey, rotate the secret, "
                                             "reopen bootstrap")
    reset.add_argument("--yes", action="store_true",
                       help="confirm: every passkey stops working at once")
    actions.add_parser("list", help="print every passkey")
    # A base64url id starts with '-' one time in 64; typed as `list` printed
    # it, it is the prefix, not an option.
    if (len(argv) == 2 and argv[0] in ("confirm", "revoke")
            and argv[1].startswith("-") and argv[1] not in ("-h", "--help", "--")):
        argv.insert(1, "--")
    args = parser.parse_args(argv)
    from pseudolife_memory.storage.maintainer import MaintainerError
    try:
        with _bank() as storage:
            store = _store(storage)
            if args.action == "enrol-code":
                return _enrol_code(store, args, out)
            if args.action == "confirm":
                row = store.confirm(args.prefix)
                print(f"Active: {row['credential_id'][:12]}  label: {row['label']}", file=out)
            elif args.action == "revoke":
                row = store.revoke(args.prefix)
                print(f"Revoked: {row['credential_id'][:12]}  label: {row['label']}", file=out)
            elif args.action == "reset":
                if not args.yes:
                    print("reset revokes every passkey and rotates the secret; pass --yes",
                          file=sys.stderr)
                    return EXIT_REFUSED
                result = store.reset()
                print(f"Revoked {result['revoked']} passkey(s); secret rotated; run "
                      "`pseudolife-mcp maintainer enrol-code` to enrol again.", file=out)
            else:
                keys = store.passkeys()
                if not keys:
                    print("No passkeys.", file=out)
                for key in keys:
                    print(f"{key['credential_id'][:12]}  {key['state']:8} {key['label']!r}  "
                          f"enrolled_by={key['enrolled_by'][:12]}  "
                          f"active_from={_when(key['active_from'])}  "
                          f"last_used={_when(key['last_used_at'])}"
                          + ("  FLAGGED (sign count went backwards)" if key["flagged_at"]
                             else ""), file=out)
            return EXIT_OK
    except MaintainerError as exc:
        print(f"refused: {_REFUSALS.get(exc.code, exc.code)}", file=sys.stderr)
        return EXIT_REFUSED
    except NoBank:
        ran = run_in_daemon("maintainer", argv)
        if ran is not None:
            return ran.returncode
        print(f"error: {no_bank_message('maintainer')}", file=sys.stderr)
        return EXIT_ERROR
    except MaintainerCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - never print a DSN
        print(f"error: {type(exc).__name__}; check PSEUDOLIFE_MCP_DATABASE_URL", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
