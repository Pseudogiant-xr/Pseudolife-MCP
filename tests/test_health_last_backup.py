"""``/health`` reports how old the last backup is.

The bank went from 2026-09-14 13:28 to 09-20 12:18 with no dump anywhere,
and nothing said so: the nightly replica push had failed since 09-13, and
the only other backups came from deploys (2026-09-23 fresh-eyes review).
``ops/backup.ps1|.sh`` now copy each dump's manifest into the daemon as
``<data_dir>/last-backup.json``, and ``/health`` turns it into
``last_backup: {at, age_hours, rotation}``. A stalled schedule shows as a
growing age; a row-count hold shows as ``rotation: "held"``.

The field is informational only and never touches ``status``: web/api.py
serves any non-ok payload as HTTP 503, and the Docker healthcheck restarts
on that. An old backup is no reason to restart the daemon.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pseudolife_memory.daemon import _build_health_payload


class _Svc:
    """Minimal MemoryService stand-in for _build_health_payload."""

    _db_url = "postgresql://fake"
    _persist_errors = 0
    _init_refusal = None
    _storage = None

    def __init__(self, data_dir=None):
        if data_dir is not None:
            self.data_dir = data_dir


def _stamp(hours_ago: float) -> str:
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(tmp_path, **fields) -> None:
    manifest = {
        "dump": "pseudolife_memory-20260923-030000.sql.gz",
        "created_at": _stamp(1),
        "rotation": "ok",
        "baseline": None,
        "note": "",
        "tables": {"public.entries": 2181},
    }
    manifest.update(fields)
    (tmp_path / "last-backup.json").write_text(json.dumps(manifest, indent=2),
                                               encoding="utf-8")


def test_the_age_and_the_rotation_state_are_reported(tmp_path):
    created = _stamp(30)
    _record(tmp_path, created_at=created, rotation="held")
    payload = _build_health_payload(_Svc(tmp_path), token_present=False)
    last = payload["last_backup"]
    assert last["at"] == created
    assert 29.9 <= last["age_hours"] <= 30.1, last
    assert last["rotation"] == "held"
    # A held rotation or an old backup is loud here, but never a 503.
    assert payload["status"] == "ok"


def test_no_record_means_no_field(tmp_path):
    """No backup script has run against this daemon (or it is a pip-tier
    bank, which ``pseudolife-mcp backup`` does not record here yet)."""
    payload = _build_health_payload(_Svc(tmp_path), token_present=False)
    assert "last_backup" not in payload
    assert payload["status"] == "ok"


def test_a_service_without_a_data_dir_is_fine():
    """Other health tests build bare stubs with no data_dir at all."""
    payload = _build_health_payload(_Svc(), token_present=False)
    assert "last_backup" not in payload


@pytest.mark.parametrize("text", [
    "{not json",
    "{}",
    json.dumps({"created_at": "yesterday", "rotation": "ok"}),
    json.dumps(["not", "an", "object"]),
])
def test_an_unreadable_record_never_breaks_health(tmp_path, text):
    (tmp_path / "last-backup.json").write_text(text, encoding="utf-8")
    payload = _build_health_payload(_Svc(tmp_path), token_present=False)
    assert "last_backup" not in payload
    assert payload["status"] == "ok"
