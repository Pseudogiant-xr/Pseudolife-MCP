"""A stand-in for ``pseudolife-mcp installer <step>`` in sliced installer tests.

    python tests/native_helper_standin.py <repo> <step> [args...]

The installers run their helpers as steps of the native client
(``native_helper`` in ``ops/install.sh``, ``Invoke-NativeHelper`` in
``ops/install.ps1``). A test that slices installer code has no native binary,
so ``native_client_stub.HELPER_SH`` / ``HELPER_PS`` define those functions to
run this file, which answers each step with the Python helper the native step
replaced (``<repo>/ops/...``, so a test's fixture repository still supplies
its own fakes). ``tests/test_installer_native_goldens.py`` and
``rust/shim/tests/cli_installer_goldens.rs`` hold the native steps to the same
answers.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = {
    "state": ("ops/installer_state.py",),
    "credentials": ("ops/client_credentials.py",),
    "engine": ("ops/installer_engine.py",),
    "codex-credentials": ("ops/setup-codex-coordination.py", "--credentials"),
    "codex-runtime-defaults": ("ops/setup-codex-coordination.py", "--runtime-defaults"),
    "codex-hooks": ("ops/setup-codex-hooks.py",),
    "claude-desktop": ("ops/register_claude_desktop.py",),
    "plugin-marketplace": ("ops/plugin_marketplace.py",),
}


def _report():
    try:
        return json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        return None


def _connect_report(mode: str) -> int:
    report = _report()
    valid = isinstance(report, dict) and isinstance(report.get("rows"), list) and "exit" in report
    if mode == "valid":
        return 0 if valid else 1
    if not valid:
        return 0
    if mode == "error":
        print(report.get("error") or "")
        return 0
    rows = [row for row in report["rows"] if isinstance(row, dict)]
    if mode == "handled":
        print(" ".join(row["client"] for row in rows if row.get("place") == "registration"
                       and row.get("state") in ("current", "change")))
        return 0
    for row in rows:
        if row.get("state") == "absent" or (mode == "manual" and row.get("state") != "manual"):
            continue
        where = " ".join(part for part in (row.get("file") or "", "[%s]" % row["key"] if row.get("key") else "") if part)
        print("    %-8s %s %s%s" % (row.get("state"), row.get("client"), row.get("place"), ": " + where if where else ""))
        for key, change in (row.get("changes") or {}).items():
            old, new = (list(change) + [None, None])[:2]
            print("             %s: %s -> %s" % (key, "(unset)" if old is None else old, "(removed)" if new is None else new))
        if row.get("detail"):
            print("             " + row["detail"])
    return 0


def _codex_fields(kind: str) -> int:
    r = _report()
    try:
        if kind == "credentials":
            assert r["status"] in ("ready", "tokenless")
            assert isinstance(r.get("credential_file_configured"), bool)
            assert isinstance(r.get("connection_configured"), bool)
            print("1" if r["credential_file_configured"] else "0")
            print("1" if r["connection_configured"] else "0")
            print(r.get("credential_file_path") or "")
            print(r.get("daemon_url") or "")
        elif kind == "hooks":
            assert r["status"] in ("ready", "pending", "unavailable", "skipped")
            assert r["source"] in ("manual", "plugin", "skip")
            assert r["instructions"] in ("present", "appended", "skipped", "covered-by-hooks")
            for key in ("status", "source", "instructions", "recovery"):
                print(" ".join(str(r.get(key) or "").splitlines()))
        else:
            assert r["status"] == "ready"
            assert r["runtime_defaults"] in ("configured", "preserved")
            print(r["runtime_defaults"])
    except (AssertionError, KeyError, TypeError):
        return 1
    return 0


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    repo, step, args = Path(argv[0]), argv[1], argv[2:]
    if step in SCRIPTS:
        script, *fixed = SCRIPTS[step]
        if step == "codex-hooks" and args[:1] == ["--checkout"]:
            args = args[2:]  # the Python helper reads the checkout it sits in
        return subprocess.call([sys.executable, str(repo / script), *fixed, *args])
    if step == "volume-names":
        options = dict(zip(args[::2], args[1::2]))
        return subprocess.call([sys.executable, str(Path(options["--repo"]) / "ops/preflight.py"),
                                "--env-file", options["--env-file"], "--volume-names"])
    if step == "json-field":
        report = _report()
        value = report.get(args[0]) if isinstance(report, dict) else None
        print(value if isinstance(value, str) else "")
        return 0
    if step == "json-string":
        value = _report()
        if not isinstance(value, str):
            return 1
        sys.stdout.buffer.write(value.encode("utf-8"))
        return 0
    if step == "connect-report":
        return _connect_report(args[0])
    if step == "codex-fields":
        return _codex_fields(args[0])
    sys.path.insert(0, str(repo))
    if step == "is-loopback":
        from pseudolife_memory.daemon_url import _is_loopback_url
        return 10 if _is_loopback_url(args[0]) else 11
    if step == "pairing-code-valid":
        from pseudolife_memory.principals import normalize_pairing_code
        line = sys.stdin.buffer.readline().decode("utf-8", "replace").rstrip("\r\n")
        return 10 if normalize_pairing_code(line) else 11
    print(f"native helper stand-in: unknown step {step}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
