"""Narrow episode policies; raw wire outside the title minute stays exact."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import math
import re


TITLE_CASE_IDS = frozenset(("episode-key-11-episode-start", "episode-repair-cwd-12"))
FORBIDDEN_CASE_IDS = frozenset(("token-del", "token-control", "token-fold",
                               "bearer-del", "bearer-control", "bearer-fold",
                               "token-invalid-line"))
HEADER_INACTIVITY_CASE_IDS = frozenset(("active-health-head", "active-health-chunk-size"))
HTTP_FORBIDDEN_INPUT_REFUSED = "[shim] invalid PSEUDOLIFE_MCP_TOKEN: forbidden HTTP header bytes."
_TITLE = re.compile(rb'"title": "(?:[^"\\]|\\.)* - ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})"')


def refusal_response(case_id, *, windows):
    """Candidate-labelled oracle only, never a replacement for Python receipts."""
    if case_id not in FORBIDDEN_CASE_IDS:
        raise ValueError("case has no approved forbidden-input substitution")
    stderr = (HTTP_FORBIDDEN_INPUT_REFUSED + ("\r\n" if windows else "\n")).encode()
    return {"exit_code": 1, "stdout_b64": "",
            "stderr_b64": base64.b64encode(stderr).decode("ascii")}


def _minutes(binding):
    before, after = binding["before"], binding["after"]
    if any(type(value) not in (int, float) or not math.isfinite(value)
           for value in (before, after)) or not 0 <= after - before < 60:
        raise ValueError("title policy needs finite ordered numeric windows shorter than a minute")
    offsets = binding["utc_offsets_seconds"]
    if len(offsets) != 2 or any(type(value) is not int or abs(value) > 14 * 3600 for value in offsets):
        raise ValueError("title policy needs per-endpoint local UTC offsets")
    minutes = [datetime.fromtimestamp(value, timezone(timedelta(seconds=offset))).strftime("%Y-%m-%d %H:%M")
               for value, offset in zip((before, after), offsets)]
    if binding["local_minutes"] != minutes:
        raise ValueError("local minutes do not bind the numeric invocation window")
    return minutes


def _title_payload(observation):
    """Only strip the admitted clock field, preserving every other captured byte."""
    minutes = _minutes(observation["clock_binding"])
    payload = copy.deepcopy({key: value for key, value in observation.items() if key != "clock_binding"})
    starts = [request for request in payload["wire"]
              if request["method"] == "POST" and request["path"] == "/api/episode/start"]
    if len(starts) != 1:
        raise ValueError("title policy requires exactly one episode-start POST")
    request = starts[0]
    raw = base64.b64decode(request["body_b64"], validate=True)
    matches = list(_TITLE.finditer(raw))
    if len(matches) != 1:
        raise ValueError("title must retain the exact JSON field shape")
    match = matches[0]
    minute = match[1].decode("ascii")
    parsed = datetime.strptime(minute, "%Y-%m-%d %H:%M")
    if parsed.strftime("%Y-%m-%d %H:%M") != minute or minute not in minutes:
        raise ValueError("title is outside its own captured local invocation window")
    request["body_b64"] = base64.b64encode(raw[:match.start(1)] + b"<title-minute>" + raw[match.end(1):]).decode("ascii")
    return payload, minute


def title_observations_match(case_id, python_first, python_second, candidate):
    """Require two distinct raw Python minutes and exact remaining observations.

    Observations contain the exact response, wire, pre/post files and declared
    inputs. Executable identities are retained separately by the capture caller.
    Missing metadata is an incomplete proof, never an implicit clock allowance.
    """
    if case_id not in TITLE_CASE_IDS:
        raise ValueError("case has no approved title-minute policy instance")
    first, first_minute = _title_payload(python_first)
    second, second_minute = _title_payload(python_second)
    actual, _ = _title_payload(candidate)
    if first_minute == second_minute:
        raise ValueError("two raw Python captures do not prove nondeterministic title bytes")
    return first == second == actual
