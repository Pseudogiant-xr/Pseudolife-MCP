"""Static controls for the narrowly approved episode policy instances."""
import base64
import copy
from datetime import datetime, timezone

import pytest

from .episode_policy import refusal_response, title_observations_match


def observation(minute, *, prefix="session", headers=None):
    epoch = datetime.strptime(minute, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
    raw = ('{"session_key": "key", "title": "' + prefix + ' - ' + minute + '"}').encode()
    return {"request": {"stdin_b64": "retained", "argv": ["episode-start", "--help", "ignored"]},
            "response": {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""},
            "pre_files_b64": {}, "post_files_b64": {},
            "wire": [{"method": "POST", "path": "/api/episode/start",
                      "headers": headers or {"Content-Type": "application/json"},
                      "body_b64": base64.b64encode(raw).decode()}],
            "clock_binding": {"before": epoch + 1, "after": epoch + 2,
                              "utc_offsets_seconds": [0, 0], "local_minutes": [minute, minute]}}


def test_title_policy_uses_each_arm_window_and_keeps_surrogate_prefix_bytes():
    rows = [observation(minute, prefix=r"missing-\ud800system32") for minute in
            ("2026-10-06 05:14", "2026-10-06 05:17", "2026-10-06 05:15")]
    assert title_observations_match("episode-repair-cwd-12", *rows)
    with pytest.raises(ValueError, match="outside its own"):
        rows[2]["clock_binding"] = rows[0]["clock_binding"]
        title_observations_match("episode-repair-cwd-12", *rows)


@pytest.mark.parametrize("field,value", [("before", float("nan")), ("after", float("inf")),
                                         ("before", 9999999999), ("utc_offsets_seconds", []),
                                         ("local_minutes", ["2026-10-06 05:16"] * 2)])
def test_title_policy_rejects_incomplete_or_unbound_clock_metadata(field, value):
    rows = [observation(minute) for minute in
            ("2026-10-06 05:14", "2026-10-06 05:17", "2026-10-06 05:15")]
    rows[2]["clock_binding"][field] = value
    with pytest.raises(ValueError):
        title_observations_match("episode-key-11-episode-start", *rows)


@pytest.mark.parametrize("field", ["headers", "prefix", "stdout", "files", "stdin", "body-format"])
def test_title_policy_never_clears_surrounding_differences(field):
    rows = [observation(minute) for minute in
            ("2026-10-06 05:14", "2026-10-06 05:17", "2026-10-06 05:15")]
    actual = rows[2]
    if field == "headers":
        actual["wire"][0]["headers"] = {"content-type": "application/json"}
    elif field == "prefix":
        rows[2] = observation("2026-10-06 05:15", prefix="other")
    elif field == "stdout":
        actual["response"]["stdout_b64"] = "eA=="
    elif field == "files":
        actual["post_files_b64"] = {"changed": "eA=="}
    elif field == "stdin":
        actual["request"]["stdin_b64"] = "changed"
    else:
        request = actual["wire"][0]
        raw = base64.b64decode(request["body_b64"]).replace(b'"key", ', b'"key",  ')
        request["body_b64"] = base64.b64encode(raw).decode()
    assert not title_observations_match("episode-key-11-episode-start", *rows)


def test_title_policy_needs_raw_python_nondeterminism_and_named_case():
    first, second = observation("2026-10-06 05:14"), observation("2026-10-06 05:17")
    with pytest.raises(ValueError, match="do not prove"):
        title_observations_match("episode-key-11-episode-start", first, copy.deepcopy(first), second)
    with pytest.raises(ValueError, match="no approved"):
        title_observations_match("other", first, second, first)
    del second["clock_binding"]["before"]
    with pytest.raises(KeyError):
        title_observations_match("episode-key-11-episode-start", first, second, first)


def test_title_policy_accepts_boundary_minute_with_explicit_local_offset():
    first, second, actual = [observation(minute) for minute in
                            ("2026-10-06 05:14", "2026-10-06 05:17", "2026-10-06 05:15")]
    stamp = actual["clock_binding"]["before"] - 1
    actual["clock_binding"] = {"before": stamp - 0.5 - 36000, "after": stamp + 0.5 - 36000,
                               "utc_offsets_seconds": [36000, 36000],
                               "local_minutes": ["2026-10-06 05:14", "2026-10-06 05:15"]}
    assert title_observations_match("episode-key-11-episode-start", first, second, actual)


def test_refusal_oracle_has_named_candidate_bytes_and_no_unapproved_cases():
    for windows, ending in [(False, b"\n"), (True, b"\r\n")]:
        result = refusal_response("token-invalid-line", windows=windows)
        assert result["exit_code"] == 1 and result["stdout_b64"] == ""
        assert base64.b64decode(result["stderr_b64"]) == (
            b"[shim] invalid PSEUDOLIFE_MCP_TOKEN: forbidden HTTP header bytes." + ending)
    for case in ("token-tab", "latin1-token", "stalled-health-body"):
        with pytest.raises(ValueError):
            refusal_response(case, windows=False)
