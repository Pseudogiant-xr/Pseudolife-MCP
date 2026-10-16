"""Hook-only HTTP header dispositions; raw observations remain unchanged."""
from collections.abc import Mapping
import copy
import re

from .harness import Policy, compare


ACCEPT_CASE_IDS = frozenset({
    "briefing-plain", "briefing-env-only", "briefing-custom-negative-unknown",
    "briefing-help", "briefing-error-int", "briefing-error-missing",
    "briefing-error-ambiguous", "briefing-error-flag", "briefing-hook-json",
    "briefing-coordination-hook", "briefing-coordination-off",
    "briefing-health-error-json", "briefing-health-non-json", "briefing-health-null",
    "briefing-content-redirect", "briefing-content-invalid-utf8",
    "briefing-markdown-false", "briefing-hook-plain-context", "briefing-launcher-default",
    "briefing-launcher-override", "briefing-token-latin1", "briefing-token-nonlatin",
    "briefing-token-del", "briefing-token-control", "briefing-token-fold",
    "prompt-baseline", "prompt-changed-note", "prompt-quiet-advance",
    "prompt-body-without-lf", "prompt-malformed-preserves", "prompt-invalid-utf8-preserves",
    "prompt-redirect-preserves", "prompt-token-file-wins", "prompt-env-token",
    "prompt-token-file-missing", "prompt-token-file-no-token", "prompt-mark-invalid-ascii",
    "prompt-mark-nonascii-after-lf", "prompt-stdin-replacement", "prompt-json-surrogate-extra",
    "prompt-json-nan-extra", "prompt-invalid-input-0", "prompt-invalid-input-1",
    "prompt-invalid-input-2", "prompt-invalid-input-3", "prompt-invalid-input-4",
    "prompt-invalid-input-5", "prompt-invalid-input-6", "prompt-invalid-input-7",
    "prompt-invalid-input-8", "prompt-invalid-input-9", "prompt-session-max",
    "prompt-session-too-long",
})
_FIELD_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z", re.ASCII)


def associated_headers(headers):
    """Associate distinct names, preserving every same-name value in order."""
    pairs = list(headers.items()) if isinstance(headers, Mapping) else headers
    if not isinstance(pairs, (list, tuple)):
        raise ValueError("headers need a mapping or ordered name/value pairs")
    result = {}
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("headers need exact name/value pairs")
        name, value = pair
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name) or not isinstance(value, str):
            raise ValueError("header names must be ASCII HTTP tokens and values retained strings")
        result.setdefault(name.lower(), []).append(value)
    return result


def associated_wire_observations_match(case_id, oracle, candidate):
    """Allow only recorded absent-oracle Accept versus native exactly */*."""
    expected, actual = copy.deepcopy(oracle), copy.deepcopy(candidate)
    if len(expected["wire"]) != len(actual["wire"]):
        return False
    for left, right in zip(expected["wire"], actual["wire"]):
        left["headers"] = associated_headers(left["headers"])
        right["headers"] = associated_headers(right["headers"])
        if case_id in ACCEPT_CASE_IDS and "accept" not in left["headers"] \
                and right["headers"].get("accept") == ["*/*"]:
            del right["headers"]["accept"]
    return not compare(expected, actual, Policy(source_text_paths=(), ignored_values=()))
