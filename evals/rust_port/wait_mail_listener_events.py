"""Replay retained listener events after their required native poll boundary."""
import base64
import os
import threading
import time

from .cli_process import file_path


class ListenerEvents:
    def __init__(self):
        self.records = []

    def prepare(self, case, home, env, command, commands):
        if "listener_event" not in case:
            return command
        event = {"case": case["id"], "arm": "oracle" if command == commands["oracle"] else "candidate",
                 "leases": [], "completed": False}

        def action():
            try:
                deadline = time.monotonic() + 3
                first = None
                while time.monotonic() < deadline:
                    leases = [path for path in (home / ".pseudolife-mcp/digests").glob("*.wait-armed")
                              if not path.name.startswith(".")]
                    if leases:
                        lease = leases[0]
                        try:
                            info = lease.stat()
                            raw = lease.read_bytes()
                        except (FileNotFoundError, PermissionError):
                            # Atomic replacement can hide the old pathname or
                            # briefly deny access while Windows changes it.
                            time.sleep(0.001)
                            continue
                        now = time.time()
                        lines = raw.splitlines()
                        expiry = float(lines[1])
                        assert len(lines) == 2 and len(lines[0]) == 32 and expiry <= now + 60
                        if os.name != "nt":
                            assert info.st_mode & 0o777 == 0o600
                        signature = (info.st_ino, info.st_mtime_ns, info.st_size)
                        if first is None:
                            first = signature
                            event["leases"].append({"path": lease.relative_to(home).as_posix(),
                                                    "raw_b64": base64.b64encode(raw).decode("ascii"),
                                                    "observed_wall": now, "expiry": expiry,
                                                    "mode": info.st_mode, "signature": list(signature)})
                        # renew() precedes each poll. A replacement after the
                        # first observed lease proves an earlier poll completed.
                        if case["id"] != "wait-mail-seen-only-cache" or signature != first:
                            event["initial_poll_completed"] = case["id"] == "wait-mail-seen-only-cache"
                            event["event_signature"] = list(signature)
                            break
                    time.sleep(0.001)
                else:
                    raise RuntimeError("listener event boundary did not appear")
                for relative, encoded in case["listener_event"].get("write", {}).items():
                    target = file_path(home, relative)
                    temporary = target.with_name(".scenario.tmp")
                    temporary.write_bytes(base64.b64decode(encoded, validate=True))
                    os.replace(temporary, target)
                for relative in case["listener_event"].get("remove", []):
                    file_path(home, relative).unlink()
                event["completed"] = True
            except Exception as error:
                event["error_type"] = type(error).__name__

        event["thread"] = threading.Thread(target=action)
        self.records.append(event)
        event["thread"].start()
        return command

    def finish(self):
        for event in self.records:
            if "thread" in event:
                thread = event.pop("thread")
                thread.join(timeout=4)
                assert not thread.is_alive() and event["completed"], event
