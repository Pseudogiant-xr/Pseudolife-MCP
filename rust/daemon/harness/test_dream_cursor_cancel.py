"""A cancelled waiter must leave the admitted cursor transaction usable."""
from __future__ import annotations

import json
from contextlib import ExitStack
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PL_HARNESS_SLICE", "w3d")
import dbstate
import daemons
import pgdisposable as pg
from dream_cursor_oracle import make_service, seed


def stop_owned(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    proc.stdin.close()
    proc.stdout.close()


class CancelledWaiter(unittest.TestCase):
    def test_panicked_writer_rolls_back_before_next_pull(self):
        binary = Path(os.environ["PL_DREAM_CONTRACT_BINARY"]).resolve()
        names = [pg.name(pg.PREFIX + "panic_" + side) for side in ("t", "py", "rs")]
        try:
            template = pg.create(names[0])
            seed(template, {"meta": {"dream_ack_secret_v1": "11" * 32},
                            "entries": [{"text": "panic recovery probe", "ts": 1.0}]})
            py_dsn, rs_dsn = [pg.create(n, template=names[0]) for n in names[1:]]
            from pseudolife_memory.storage.postgres import PostgresStorage
            storage = PostgresStorage(py_dsn)
            try:
                expected = make_service(storage).dream_pull()
            finally:
                storage.close()
            expected_state = dbstate.dump(py_dsn)
            with tempfile.TemporaryDirectory(prefix="pl-w3d-panic-") as directory:
                home = Path(directory)
                result = subprocess.run(
                    [str(binary)], input='{"op":"pull"}\n{"op":"panic"}\n{"op":"pull"}\n{"op":"writer-state"}\n{"op":"exit"}\n',
                    capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=home,
                    env=daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": rs_dsn}))
                self.assertEqual(result.returncode, 0, result.stderr)
                answers = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(answers[0], expected)
                self.assertEqual(answers[1], {"panicked": True})
                self.assertEqual(answers[2], expected)
                self.assertEqual(answers[3], {"idle": True})
                self.assertEqual(dbstate.diff(expected_state, dbstate.dump(rs_dsn)), [])
        finally:
            for name in reversed(names):
                pg.drop(name)

    def test_admitted_commit_survives_cancel_and_next_pull(self):
        binary = Path(os.environ["PL_DREAM_CONTRACT_BINARY"]).resolve()
        names = [pg.name(pg.PREFIX + "cancel_" + side) for side in ("t", "py", "rs")]
        proc = None
        try:
            template = pg.create(names[0])
            seed(template, {"meta": {"dream_ack_secret_v1": "11" * 32},
                            "entries": [{"text": "cancellation probe", "ts": 1.0}]})
            py_dsn, rs_dsn = [pg.create(n, template=names[0]) for n in names[1:]]
            from pseudolife_memory.storage.postgres import PostgresStorage
            storage = PostgresStorage(py_dsn)
            try:
                service = make_service(storage)
                pulled = service.dream_pull()
                service.dream_commit(pulled["commit_token"])
                expected_next = service.dream_pull()
            finally:
                storage.close()
            expected_state = dbstate.dump(py_dsn)
            with tempfile.TemporaryDirectory(prefix="pl-w3d-cancel-") as directory:
                home = Path(directory)
                extra = {"PSEUDOLIFE_MCP_DATABASE_URL": rs_dsn}
                if os.environ.get("PSEUDOLIFE_DAEMON_MUTANT"):
                    extra["PSEUDOLIFE_DAEMON_MUTANT"] = os.environ["PSEUDOLIFE_DAEMON_MUTANT"]
                with (home / "adapter.log").open("wb") as log, ExitStack() as cleanup:
                    proc = subprocess.Popen([str(binary)], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=log, text=True,
                                            encoding="utf-8", cwd=home,
                                            env=daemons.base_env(home, extra))
                    cleanup.callback(stop_owned, proc)
                    # A live Popen object pins the owned process for cleanup.
                    replies = queue.Queue()
                    def read():
                        for line in proc.stdout:
                            replies.put(line)
                    threading.Thread(target=read, daemon=True).start()
                    def call(action):
                        proc.stdin.write(json.dumps(action) + "\n")
                        proc.stdin.flush()
                        return json.loads(replies.get(timeout=20))
                    pulled = call({"op": "pull"})
                    result = call({"op": "cancel", "action": {
                        "op": "commit", "commit_token": pulled["commit_token"]}})
                    self.assertTrue(result["admitted"])
                    self.assertTrue(result["cancelled"])
                    self.assertEqual(result["next"], expected_next)
                    self.assertEqual(dbstate.diff(expected_state, dbstate.dump(rs_dsn)), [])
                    proc.stdin.write('{"op":"exit"}\n')
                    proc.stdin.flush()
                    self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            for name in reversed(names):
                pg.drop(name)


if __name__ == "__main__":
    unittest.main()
