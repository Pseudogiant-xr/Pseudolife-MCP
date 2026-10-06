"""Compare stdio wire bytes through named, narrow Phase 1 policies."""
import base64
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from .harness import DuplicateJSONKey, Policy, compare, strict_json_loads
from .wire import replacements

STDERR_EVIDENCE_LIMIT = 64 * 1024
READINESS_WAIT_RULE = "completed-readiness-wait-notice"
_READINESS_WAIT_NOTICE = re.compile(
    rb"\[shim\] no daemon at http://127\.0\.0\.1:([1-9][0-9]{0,4})" + re.escape(
        " and PSEUDOLIFE_MCP_NO_SPAWN is set — waiting up to 5s for it instead of spawning a fallback "
        "(Docker may still be starting)...".encode("utf-8")) + rb"\r?\n")


def needs_stderr_evidence(difference, *, include_boundaries=True):
    path = difference.get("path", "")
    prefixes = ("/stderr", "/frames", "/exit_code") + (("/json",) if include_boundaries else ())
    return (include_boundaries and path == "/") or any(
        path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def retain_stderr(differences, expected, actual, *, include_boundaries=True):
    evidence = {}
    for arm, transcript in (("oracle", expected), ("candidate", actual)):
        record = {"arm": arm, "era": transcript.get("era"), "pid": transcript.get("pid"),
                  "captured_at_utc": transcript.get("captured_at_utc"),
                  "capture_kind": transcript.get("capture_kind")}
        try:
            raw = base64.b64decode(transcript["stderr_b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            # Never turn absent bytes into an apparently empty capture.
            record["missing_stderr_bytes"] = True
        else:
            record.update(stderr_b64=base64.b64encode(raw[:STDERR_EVIDENCE_LIMIT]).decode("ascii"),
                          byte_count=len(raw), truncated=len(raw) > STDERR_EVIDENCE_LIMIT)
        evidence[arm] = record
    return [{**difference, "stderr_evidence": evidence} if needs_stderr_evidence(
                difference, include_boundaries=include_boundaries)
            else difference for difference in differences]


def stderr_evidence_complete(differences):
    for difference in differences:
        if not needs_stderr_evidence(difference):
            continue
        evidence = difference.get("stderr_evidence", {})
        for arm in ("oracle", "candidate"):
            try:
                record = evidence[arm]
                raw = base64.b64decode(record["stderr_b64"], validate=True)
                count = record["byte_count"]
                stamp = record["captured_at_utc"]
                captured = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if (record["capture_kind"] != "process" or record["arm"] != arm or
                        not isinstance(record["era"], str) or not record["era"] or
                        type(record["pid"]) is not int or record["pid"] <= 0 or
                        not stamp.endswith("Z") or captured.utcoffset() != timezone.utc.utcoffset(captured) or
                        type(count) is not int or count < 0 or len(raw) != min(count, STDERR_EVIDENCE_LIMIT) or
                        type(record["truncated"]) is not bool or
                        record["truncated"] != (count > STDERR_EVIDENCE_LIMIT)):
                    return False
            except (KeyError, TypeError, ValueError, AttributeError):
                return False
    return True


@dataclass(frozen=True)
class StdioPolicy:
    name: str = "stdio-raw-compared"
    source_text_paths: tuple = ("/body/result/tools/*/description", "/body/result/instructions")
    stderr_allowlist: tuple = ()
    eof_orders: tuple = ()
    non_eof_orders: tuple = ()
    readiness_wait_notice: bool = False


def normalized_frame(raw, policy):
    text = raw.decode("utf-8")
    original = strict_json_loads(text)
    # Identical trees authorize only declared source LF token spans. No JSON
    # reserialization: spacing, member order, numeric spelling and escapes stay.
    changes = replacements(text, original, original, source_text_paths=policy.source_text_paths)
    for left, right, value in sorted(changes, reverse=True):
        text = text[:left] + value + text[right:]
    return text.encode("utf-8")


def observation(transcript, policy):
    if "boundary_error" in transcript:
        return {"boundary_error": transcript["boundary_error"]}
    try:
        frames = [normalized_frame(base64.b64decode(value, validate=True), policy)
                  for value in transcript["stdout_frames_b64"]]
    except DuplicateJSONKey:
        return {"boundary_error": "DuplicateJSONKey"}
    except (ValueError, UnicodeError):
        return {"boundary_error": "InvalidStdioJSON"}
    stderr = base64.b64decode(transcript["stderr_b64"], validate=True)
    retained = []
    for line in stderr.splitlines(keepends=True):
        if not any(re.fullmatch(pattern, line) for pattern in policy.stderr_allowlist):
            retained.append(line)
    return {"frames": frames, "json": [strict_json_loads(frame) for frame in frames],
            "stderr": b"".join(retained), "exit_code": transcript["exit_code"]}


def _readiness_wait_matches(expected, actual, policy, left, right):
    if (not policy.readiness_wait_notice or policy.stderr_allowlist or
            policy.eof_orders or policy.non_eof_orders or
            "boundary_error" in left or "boundary_error" in right or not left["frames"]):
        return False
    if any(type(side["exit_code"]) is not int or side["exit_code"] != 0 for side in (left, right)):
        return False
    # Establish all other observations under the existing comparator first.
    if compare({key: value for key, value in left.items() if key != "stderr"},
               {key: value for key, value in right.items() if key != "stderr"},
               Policy(ignored_values=(), source_text_paths=())):
        return False
    raw = [base64.b64decode(transcript["stderr_b64"], validate=True) for transcript in (expected, actual)]
    if raw[0] == raw[1]:
        return False
    for stderr in raw:
        if stderr:
            notice = _READINESS_WAIT_NOTICE.fullmatch(stderr)
            if notice is None or int(notice[1]) > 65535:
                return False
    # Accepted differences need the same genuine evidence as failed ones.
    return stderr_evidence_complete(retain_stderr([{"path": "/stderr"}], expected, actual))


def readiness_wait_evidence(expected, actual, policy):
    """Retain raw bytes and process provenance for this accepted stderr change."""
    for transcript in (expected, actual):
        try:
            base64.b64decode(transcript["stderr_b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            return []
    left, right = observation(expected, policy), observation(actual, policy)
    if not _readiness_wait_matches(expected, actual, policy, left, right):
        return []
    return retain_stderr([{"path": "/stderr", "rule": READINESS_WAIT_RULE,
                           "condition": "matching stdout and successful exit 0; sole exact notice or empty stderr"}],
                         expected, actual)


def _judge(expected, actual, policy):
    left, right = observation(expected, policy), observation(actual, policy)
    if "boundary_error" in left:
        raise ValueError("oracle stdio transcript is malformed")
    if _readiness_wait_matches(expected, actual, policy, left, right):
        left["stderr"] = right["stderr"] = b""
    if "boundary_error" not in right and policy.eof_orders:
        # Only the final two EOF errors can permute; every frame retains exact
        # bytes and multiplicity. The accepted tuples come from frozen evidence.
        frames = right["frames"]
        order = tuple(strict_json_loads(frame).get("id", strict_json_loads(frame).get("method")) for frame in frames)
        if order not in policy.eof_orders:
            return [{"path": "/frames", "reason": "unobserved_eof_order"}]
        if len(left["frames"]) >= 2 and Counter(left["frames"][-2:]) == Counter(frames[-2:]):
            right["frames"] = [*frames[:-2], *left["frames"][-2:]]
            right["json"] = [strict_json_loads(frame) for frame in right["frames"]]
    if "boundary_error" not in right and policy.non_eof_orders:
        # Only A/B's final call responses in this held non-EOF scenario can
        # permute. Prefix bytes, payloads, spacing and multiplicity stay exact.
        frames = right["frames"]
        order = tuple(frame.get("id") for frame in right["json"])
        if len(frames) != 3 or order not in policy.non_eof_orders:
            return [{"path": "/frames", "reason": "unobserved_non_eof_order"}]
        if len(left["frames"]) == 3 and Counter(left["frames"][-2:]) == Counter(frames[-2:]):
            right["frames"] = [frames[0], *left["frames"][-2:]]
            right["json"] = [strict_json_loads(frame) for frame in right["frames"]]
    return compare(left, right, Policy(ignored_values=(), source_text_paths=()))


def judge(expected, actual, policy):
    """Keep the original comparator result schema for existing harness callers."""
    for transcript in (expected, actual):
        try:
            base64.b64decode(transcript["stderr_b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            return retain_stderr([{"path": "/stderr", "reason": "missing_stderr_bytes"}], expected, actual)
    return retain_stderr(_judge(expected, actual, policy), expected, actual, include_boundaries=False)


def judge_with_evidence(expected, actual, policy):
    """Retained receipts require both arms' evidence for every stdio boundary."""
    return retain_stderr(judge(expected, actual, policy), expected, actual)


def verifies_frozen_capture(frozen, live, policy):
    """A live process supplies provenance only after its frozen bytes match."""
    return frozen.get("stderr_b64") == live.get("stderr_b64") and not _judge(frozen, live, policy)


def eof_policy(evidence, era, case):
    groups = [group for group in evidence["groups"] if group["era"] == era and group["case"] == case]
    if len(groups) != 1:
        raise ValueError("missing or ambiguous observed EOF order evidence")
    orders = tuple(tuple(item["order"]) for item in groups[0]["orders"])
    return StdioPolicy(eof_orders=orders if case == "two" else ())


def judge_sensitivity_controls(transcript, policy):
    """In-memory mutations exercise the comparator, not a candidate process."""
    import copy
    import json
    controls = {}
    source = next(index for index, frame in enumerate(transcript["stdout"])
                  if frame.get("id") == "list")
    error = next(index for index, frame in enumerate(transcript["stdout"])
                 if frame.get("id") == "invalid")
    for name, index, reason in (("tool-description", source, "value"),
                                ("error-as-success", error, "tool_error_envelope"),
                                ("duplicate-key", source, "duplicate_json_key"),
                                ("raw-whitespace", source, "value"),
                                ("wrong-exit", source, "value"),
                                ("unexpected-stderr", source, "value")):
        candidate = copy.deepcopy(transcript)
        candidate["capture_kind"] = "judge-sensitivity"
        candidate.pop("pid", None)
        candidate.pop("captured_at_utc", None)
        raw = base64.b64decode(candidate["stdout_frames_b64"][index])
        value = strict_json_loads(raw)
        if name == "tool-description":
            value["result"]["tools"][0]["description"] += " Altered contract."
            raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        elif name == "error-as-success":
            value["result"]["isError"] = False
            raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        elif name == "duplicate-key":
            raw = b'{"id":"duplicate",' + raw[1:]
        elif name == "raw-whitespace":
            raw = b" " + raw
        elif name == "wrong-exit":
            candidate["exit_code"] = 1
        else:
            candidate["stderr_b64"] = base64.b64encode(b"unexpected stderr\n").decode()
        candidate["stdout_frames_b64"][index] = base64.b64encode(raw).decode()
        differences = _judge(transcript, candidate, policy)
        rejected = reason in {difference["reason"] for difference in differences}
        controls[name] = {"kind": "judge-sensitivity",
                          "source_capture": {key: transcript.get(key) for key in
                                             ("arm", "era", "capture_kind", "pid", "captured_at_utc")},
                          "expected_difference": reason, "rejected": rejected,
                          "differences": differences, "policy": policy.name}
        if not rejected:
            raise RuntimeError("stdio judge-sensitivity control was accepted: " + name)
    return controls


def concurrent_policy(evidence, platform, era, release_order):
    groups = [group for group in evidence["groups"] if
              (group["platform"], group["era"], group["release_order"]) ==
              (platform, era, release_order)]
    if len(groups) != 1:
        raise ValueError("missing or ambiguous observed non-EOF order evidence")
    orders = tuple(("open", *order) for order in groups[0]["orders"])
    if not orders or any(order not in (("open", "A", "B"), ("open", "B", "A")) for order in orders):
        raise ValueError("non-EOF evidence contains an unrelated response")
    return StdioPolicy(name="non-eof-observed-final-call-pair-orders", non_eof_orders=orders)
