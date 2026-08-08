"""ladder_replicate: pure unit tests — no docker, no subprocess, no GPU.

Pins the fresh-container replication runner's load-bearing mechanics
(evlora followup: a warm llama.cpp candidate container corrupted ladder
passes 2-3, stale_leak 1.0 warm vs 0.1 fresh — all campaign numbers were
fresh-container):

* agreement is computed over the deterministic metrics only
  (gold_recoverable / stale_leak), never latency/tokens;
* a disagreeing pass set is reported loudly, never averaged away;
* the docker lifecycle args are constructed exactly (rm -f before run,
  loopback publish only).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import ladder_replicate as lr  # noqa: E402


def _pass(gold=1.0, stale=0.1, tokens=42.0):
    return {"rung": "e4b-v3", "status": "ok", "gold_recoverable": gold,
            "stale_leak": stale, "tokens_per_query": tokens,
            "search_latency_ms": 3.0}


def test_agreement_over_deterministic_metrics_only():
    # Latency/token jitter must not break agreement.
    out = lr.passes_agree([_pass(tokens=42.0), _pass(tokens=99.0),
                           _pass(tokens=7.0)])
    assert out["agree"] is True
    assert out["gold_values"] == [1.0, 1.0, 1.0]
    assert out["stale_values"] == [0.1, 0.1, 0.1]


def test_disagreement_is_flagged_not_averaged():
    out = lr.passes_agree([_pass(), _pass(stale=1.0), _pass(stale=1.0)])
    assert out["agree"] is False
    assert out["stale_values"] == [0.1, 1.0, 1.0]
    assert "disagree" in out["detail"]


def test_unreachable_pass_fails_agreement():
    bad = {"rung": "e4b-v3", "status": "unreachable"}
    out = lr.passes_agree([_pass(), bad])
    assert out["agree"] is False
    assert "unreachable" in out["detail"]


def test_docker_lifecycle_args():
    rm, run = lr.docker_commands("img:tag", "bench-name", 8081)
    assert rm == ["docker", "rm", "-f", "bench-name"]
    assert run[:2] == ["docker", "run"]
    assert "-d" in run
    assert "127.0.0.1:8081:8081" in " ".join(run)
    assert run[-1] == "img:tag"


def test_artifact_written_with_prereg_and_passes(tmp_path):
    out = tmp_path / "ladder-replicate-x.json"
    lr.write_artifact(out, {"tag": "x", "passes": [_pass()],
                            "agreement": {"agree": True}})
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tag"] == "x"
    assert data["agreement"]["agree"] is True
    assert "warm-container" in data["hazard_note"]
