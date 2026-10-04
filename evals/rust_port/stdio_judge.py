"""Compare stdio wire bytes through named, narrow Phase 1 policies."""
import base64
from collections import Counter
from dataclasses import dataclass
import re

from .harness import DuplicateJSONKey, Policy, compare, strict_json_loads
from .wire import replacements


@dataclass(frozen=True)
class StdioPolicy:
    name: str = "stdio-raw-compared"
    source_text_paths: tuple = ("/body/result/tools/*/description", "/body/result/instructions")
    stderr_allowlist: tuple = ()
    eof_orders: tuple = ()
    non_eof_orders: tuple = ()


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


def judge(expected, actual, policy):
    left, right = observation(expected, policy), observation(actual, policy)
    if "boundary_error" in left:
        raise ValueError("oracle stdio transcript is malformed")
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


def eof_policy(evidence, era, case):
    groups = [group for group in evidence["groups"] if group["era"] == era and group["case"] == case]
    if len(groups) != 1:
        raise ValueError("missing or ambiguous observed EOF order evidence")
    orders = tuple(tuple(item["order"]) for item in groups[0]["orders"])
    return StdioPolicy(eof_orders=orders if case == "two" else ())


def graded_controls(transcript, policy):
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
        differences = judge(transcript, candidate, policy)
        rejected = reason in {difference["reason"] for difference in differences}
        controls[name] = {"expected_difference": reason, "rejected": rejected,
                          "differences": differences, "policy": policy.name}
        if not rejected:
            raise RuntimeError("stdio graded control was accepted: " + name)
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
