"""Named episode header dispositions; preserve values, duplicates and raw inputs."""
import copy
from collections.abc import Mapping
import json
from pathlib import Path
import re

from .harness import Policy, compare


_LEDGER = json.loads(Path(__file__).with_name("episode_policy_cases.json").read_text(encoding="utf-8"))
ACCEPT_CASE_IDS = frozenset(_LEDGER["outside_policy_raw_header_differences"]["case_ids_per_os"])
if len(ACCEPT_CASE_IDS) != 57:
    raise ValueError("episode Accept disposition requires the exact 57-case ledger")
_FIELD_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z", re.ASCII)


def associated_headers(headers):
    """http-field-name-case-insensitive; collisions fail instead of overwriting.

    Historical mapping captures cannot prove duplicate-line absence. New pair
    captures retain order; any duplicate or case collision is incomplete and
    rejected, even when values agree. No coalescing or whitespace change occurs.
    """
    mapping = isinstance(headers, Mapping)
    pairs = list(headers.items()) if mapping else headers
    if not isinstance(pairs, (list, tuple)):
        raise ValueError("headers need a mapping or ordered name/value pairs")
    result, seen = [], set()
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("headers need exact name/value pairs")
        name, value = pair
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name) or not isinstance(value, str):
            raise ValueError("header names must be ASCII HTTP tokens and values retained strings")
        name = name.lower()
        if name in seen:
            raise ValueError("duplicate or case-colliding headers require lossless disposition")
        seen.add(name)
        result.append([name, value])
    return dict(result) if mapping else result


def _value(headers, name):
    if isinstance(headers, Mapping):
        return headers.get(name)
    return next((value for field, value in headers if field == name), None)


def _without(headers, name):
    if isinstance(headers, Mapping):
        return {field: value for field, value in headers.items() if field != name}
    return [pair for pair in headers if pair[0] != name]


def wire_observations_match(case_id, oracle, candidate):
    """Compare exact observations with only the two explicitly named dispositions.

    Header-name association applies to episode wire fields. Absent-oracle Accept
    versus candidate exactly */* applies only to the recorded 57 IDs. All other
    fields stay exact, including Referer, auth, routing, bodies and framing.
    """
    expected, actual = copy.deepcopy(oracle), copy.deepcopy(candidate)
    if len(expected["wire"]) != len(actual["wire"]):
        return False
    for left, right in zip(expected["wire"], actual["wire"]):
        left["headers"] = associated_headers(left["headers"])
        right["headers"] = associated_headers(right["headers"])
        if case_id in ACCEPT_CASE_IDS and _value(left["headers"], "accept") is None \
                and _value(right["headers"], "accept") == "*/*":
            right["headers"] = _without(right["headers"], "accept")
    return not compare(expected, actual, Policy(source_text_paths=(), ignored_values=()))
