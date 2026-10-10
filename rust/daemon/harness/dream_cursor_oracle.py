"""NDJSON adapter over the shipped Python dream/storage implementation.

The service resident is deliberately small: all entries already have database
identities, and no embedder, model, scheduler or extractor is constructed.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


def seed(dsn: str, fixture: dict) -> None:
    """Build the template with production insert_entry/meta_set paths."""
    import numpy as np
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(dsn)
    try:
        for key, value in fixture.get("meta", {}).items():
            storage.meta_set(key, value)
        for value in fixture.get("entries", []):
            entry = {
                "band": "flat", "text": "synthetic dream input",
                "embedding": np.zeros(1024, dtype=np.float32),
                "surprise": 0.5, "ts": 0.0, "access_count": 0,
                "source": "notes", "superseded_at": None,
                "superseded_by_text": None, "last_logical_turn": None,
                "episode_id": None, "episode_title": None,
                "tags": [], "slots": [], "authority": None,
                "distortion_tolerance": None,
            }
            entry.update(value)
            storage.insert_entry(entry)
    finally:
        storage.close()


def make_service(storage):
    from pseudolife_memory.utils.config import DreamConfig
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.service_dream import DreamOps

    class ResidentDream(DreamOps):
        def __init__(self):
            self._storage = storage
            self._lock = threading.RLock()
            self._cms = None
            self._cortex = SimpleNamespace(dream_cursor=0.0)
            self._dream_tracking_error = None
            self._dream_tracking_retryable = False
            self._persist_errors = 0
            self.config = SimpleNamespace(memory=SimpleNamespace(dream=DreamConfig()))

        def load(self):
            entries = []
            for row in storage.load_entries():
                entries.append(SimpleNamespace(
                    db_id=row["id"], dream_id=None, text=row["text"],
                    timestamp=row["ts"], source=row["source"],
                    dream_state=row["dream_state"], episode_id=row["episode_id"],
                    authority=row["authority"],
                    distortion_tolerance=row["distortion_tolerance"]))
            self._cms = SimpleNamespace(bands=[SimpleNamespace(entries=entries)],
                                        dream_ack_secret=None, dream_display_cursor=0.0)

        def _ensure_init(self):
            if self._cms is None:
                self.load()
                self._initialize_dream_tracking()

        def _initialize_dream_tracking(self):
            # Use the production error-latching and state synchronization too.
            MemoryService._initialize_dream_tracking(self)

        def policy(self, action):
            cfg = DreamConfig()
            for key in ("eligible_sources", "exclude_sources"):
                if key in action:
                    setattr(cfg, key, action[key])
            self.config.memory.dream = cfg

        def initialize(self, action):
            self.policy(action)
            result = storage.initialize_dream_tracking(
                eligible_sources=action.get("eligible_sources"),
                exclude_sources=action.get("exclude_sources"))
            self.load()
            self._cms.dream_ack_secret = result["secret"]
            self._cms.dream_display_cursor = float(result["dream_cursor"])
            self._cortex.dream_cursor = self._cms.dream_display_cursor
            self._dream_tracking_error = None
            self._dream_tracking_retryable = False
            return result

    return ResidentDream()


def dispatch(storage, service, action: dict):
    from pseudolife_memory.dream_token import issue_commit_token, verify_commit_token

    op = action["op"]
    if op == "initialize":
        return service.initialize(action)
    if op == "pull":
        service.policy(action)
        return service.dream_pull(action.get("limit", 20))
    if op == "commit":
        return service.dream_commit(action.get("commit_token"))
    if op == "acknowledge":
        return storage.acknowledge_dream_entries(action["entry_ids"], action["display_timestamp"])
    if op in ("issue", "verify"):
        args = {key: action.get(key) for key in ("secret", "backend", "generation")}
        if op == "issue":
            return {"token": issue_commit_token(**args, entry_ids=action.get("entry_ids"),
                                                 display_timestamp=action.get("display_timestamp"))}
        payload = verify_commit_token(action.get("token"), **args)
        return {"entry_ids": list(payload.entry_ids), "display_timestamp": payload.display_timestamp}
    raise ValueError(f"unknown operation: {op}")


def main() -> int:
    dsn = os.environ["PSEUDOLIFE_MCP_DATABASE_URL"]
    if len(sys.argv) == 3 and sys.argv[1] == "--seed":
        if sys.argv[2] != "-":
            raise SystemExit("fixture setup accepts stdin only")
        seed(dsn, json.load(sys.stdin))
        print("dream template seeded", flush=True)
        return 0
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(dsn)
    service = make_service(storage)
    try:
        for line in sys.stdin:
            action = json.loads(line)
            if action.get("op") == "exit":
                break
            try:
                result = dispatch(storage, service, action)
            except Exception as exc:
                result = {"error": str(exc)}
            print(json.dumps(result, allow_nan=False, separators=(",", ":")), flush=True)
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
