"""TEMPORARY diagnostic for PR #897 (to be removed before merge): records what
the hosted runner's token can do in a Modify-only folder, as junit
properties. It asserts nothing about the outcome."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_credentials import (_current_user_sid, _file_owner_sid, _modify_only_folder,
                                    _token_privilege)

PRIVS = ("SeTakeOwnershipPrivilege", "SeRestorePrivilege", "SeBackupPrivilege",
         "SeSecurityPrivilege", "SeChangeNotifyPrivilege")


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_record_runner_owner_facts(tmp_path: Path, record_property) -> None:
    import ctypes

    from pseudolife_memory import credentials

    whoami = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
    priv = subprocess.run([str(whoami), "/priv", "/fo", "csv", "/nh"], capture_output=True,
                          text=True, timeout=60).stdout
    record_property("whoami_priv", " | ".join(line for line in priv.splitlines() if line))
    groups = subprocess.run([str(whoami), "/groups", "/fo", "csv", "/nh"], capture_output=True,
                            text=True, timeout=60).stdout
    record_property("integrity", " | ".join(g for g in groups.splitlines() if "Mandatory" in g))
    record_property("is_user_an_admin", bool(ctypes.windll.shell32.IsUserAnAdmin()))
    sid = _current_user_sid()
    record_property("user_is_well_known", not sid.startswith("S-1-5-21-"))
    record_property("tmp_drive", str(tmp_path)[:3])
    folder = _modify_only_folder(tmp_path, sid)
    try:
        record_property("folder_acl", subprocess.run(["icacls", str(folder)], capture_output=True,
                                                     text=True, timeout=60).stdout.replace(sid, "<user>"))
        for label, states in (("as-is", {}),
                              ("takeown-off", {"SeTakeOwnershipPrivilege": False}),
                              ("takeown-restore-off", {"SeTakeOwnershipPrivilege": False,
                                                       "SeRestorePrivilege": False}),
                              ("takeown-restore-backup-security-off",
                               {p: False for p in PRIVS[:4]})):
            before = {p: _token_privilege(p) for p in PRIVS}
            for name, on in states.items():
                _token_privilege(name, enable=on)
            during = {p: _token_privilege(p) for p in PRIVS}
            probe = folder / f"{label}.token"
            probe.write_text("x", encoding="utf-8")
            owner = _file_owner_sid(probe)
            record_property(f"{label}_privs", ",".join(p for p, v in during.items() if v))
            record_property(f"{label}_owner_class", "user" if owner == sid else owner
                            if not owner.startswith("S-1-5-21-") else "other-account")
            record_property(f"{label}_file_acl", subprocess.run(
                ["icacls", str(probe)], capture_output=True, text=True,
                timeout=60).stdout.replace(sid, "<user>"))
            try:
                credentials._secure_windows_file(probe)
                record_property(f"{label}_outcome", "protected")
            except credentials.CredentialError as exc:
                record_property(f"{label}_outcome", f"refused: {str(exc)[:80]}")
            after = _file_owner_sid(probe)
            record_property(f"{label}_owner_after", "user" if after == sid else after
                            if not after.startswith("S-1-5-21-") else "other-account")
            for name, was in before.items():
                if name in states:
                    _token_privilege(name, enable=was)
    finally:
        subprocess.run(["icacls", str(folder), "/reset", "/T"], capture_output=True, timeout=60)
