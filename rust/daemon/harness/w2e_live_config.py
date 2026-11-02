"""Live Console settings: compare exact responses and their next consumer."""
from __future__ import annotations

import json
import re

OPS = ("live_config",)
BACKUP = re.compile(r".+[\\/]config\.yaml\.\d{8}-\d{6}\.[a-z0-9_]{8}\.bak")


def patch(**values):
    return {"op": "live_config", "patch": values}


def run(op, port, h):
    body = json.dumps(op["patch"]).encode() if "patch" in op else None
    headers = [h.AUTH] + ([("Content-Type", "application/json")] if body else [])
    result = h.w1a.call(port, "POST" if body else "GET", "/api/config", headers, body)
    value = result.get("json", result.get("bytes"))
    if isinstance(value, dict) and "config_path" in value:
        # Only process-local filenames differ; applied/restart lists and values
        # remain exact, unlike the historical restart-only Console comparator.
        assert str(value["config_path"]).replace("\\", "/").endswith("/data/config.yaml")
        value = {**value, "config_path": "<home>/data/config.yaml"}
        if value.get("backup") is not None:
            assert BACKUP.fullmatch(str(value["backup"])), value["backup"]
            value["backup"] = "<backup>"
    return {"isError": result["status"] != 200, "status": result["status"], "value": value}


def register(h):
    quiet = "memory:\n  dream:\n    enabled: false\n    sweep_interval_seconds: 1\n"
    read = {"op": "live_config"}
    slot = "entity_norm = 'live-config-host' AND attribute_norm = 'os'"
    facts = [h.fact("Live Config Host", "OS", v) for v in ("9", "10", "11", "12")]
    facts += [h.sql("UPDATE facts SET superseded_at = superseded_at - 100 * 86400 "
                    f"WHERE {slot} AND status = 'superseded'", after_stop=True)]
    result = {}
    for field, boot, applied in (("enabled", "false", True),
                                 ("keep_per_slot", "3", 1),
                                 ("min_age_days", "365", 30)):
        settings = {"enabled": "true", "keep_per_slot": "1", "min_age_days": "30"}
        settings[field] = boot
        result[f"config-live-compaction-{field}"] = {
            "config": quiet + "  compaction:\n" + "".join(
                f"    {key}: {value}\n" for key, value in settings.items()),
            "seed": facts,
            "ops": [{**patch(**{f"memory.compaction.{field}": applied}),
                     "settle": {"quiet_s": 3},
                     "expect": [f"SELECT count(*) = 2 FROM facts WHERE {slot}"]}, read,
                    h.fact_get("Live Config Host", "OS"),
                    h.history("Live Config Host", "OS")],
        }
    sentence = "I have a Ragdoll cat named Jacque"
    result["config-live-surprise"] = {
        "config": quiet,
        "seed": [h.store(sentence)],
        "ops": [patch(**{"memory.surprise_threshold": 0.99}), read,
                h.store(sentence + ".")],
    }
    result["config-live-retrieval-retention"] = {
        "config": quiet + "  retrieval_log:\n    retention_days: 365\n",
        "seed": [h.sql("INSERT INTO retrieval_events (query_text, origin, served, created_at) "
                       "VALUES ('old fixture query', 'search', '[]', "
                       "EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) - 100 * 86400)", after_stop=True)],
        "ops": [{**patch(**{"memory.retrieval_log.retention_days": 30}),
                 "settle": {"quiet_s": 3},
                 "expect": ["SELECT count(*) = 0 FROM retrieval_events "
                            "WHERE query_text = 'old fixture query'"]}, read],
    }
    result["config-live-runs-keep"] = {
        "config": quiet,
        "seed": [h.sql("INSERT INTO dream_runs (started_at, cursor_before, status) "
                       "SELECT 100 + n, 0, 'committed' FROM generate_series(1, 5) n",
                       after_stop=True)],
        "ops": [{**patch(**{"memory.dream.runs_keep": 1}),
                 "settle": {"quiet_s": 3},
                 "expect": ["SELECT count(*) = 1 FROM dream_runs"]}, read],
    }
    result["config-live-signal-retention"] = {
        "config": h.stages_config("    infer_outcomes: false\n"
                                  "    signal_retention_days: 365\n"),
        "extractor": {"model": "stub-model", "replies": []},
        "seed": [h.sql("INSERT INTO outcome_signals (task, outcome, created_at, consumed_at) "
                       "VALUES ('old fixture task', 'success', "
                       "EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) - 100 * 86400, 1)", after_stop=True)],
        "ops": [patch(**{"memory.lessons.signal_retention_days": 30}), read,
                {**h.dream("run"), "expect": ["SELECT count(*) = 0 FROM outcome_signals"]}],
    }
    return result
