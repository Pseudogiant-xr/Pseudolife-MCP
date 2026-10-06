"""Offline lease policy preparation; not wired into executable acceptance."""
from __future__ import annotations

import base64
import math
import re


TRACEBACK = b"Traceback (most recent call last):"
FRAME = re.compile(rb'  File "[^\r\n]+", line [0-9]+, in [^\r\n]+(?:\r?\n)')
TERMINAL = re.compile(rb"[A-Za-z_][A-Za-z_0-9.]*: [^\r\n]*\r?\n")
RUN_CLOCK = re.compile(
    rb'\{"held": "suite,gpu", "t": (?P<clock>[0-9]+\.[0-9]+), "credential": false\}'
)
HEADER_FIELDS = frozenset({"agent_id", "credential", "bearer"})


def traceback_response(oracle, candidate, terminal):
    """Compare exact response bytes, deferring only validated Python frames.

    The recorded terminal line includes its platform newline. Prefix/suffix
    diagnostics remain exact; a malformed traceback is not an admitted case.
    """
    if TERMINAL.fullmatch(terminal) is None:
        raise ValueError("terminal exception line required")
    stderr = base64.b64decode(oracle["stderr_b64"], validate=True)
    lines = stderr.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == TRACEBACK]
    ends = [i for i, line in enumerate(lines) if line == terminal]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("one captured traceback and terminal line required")
    start, end = starts[0], ends[0]
    index = start + 1
    frames = 0
    while index < end:
        if FRAME.fullmatch(lines[index]) is None:
            raise ValueError("unrecognized stderr is not a deferred frame")
        frames += 1
        index += 1
        # Python emits a source line and optionally its position indicators.
        if index < end and lines[index].startswith(b"    "):
            index += 1
            if index < end and re.fullmatch(rb"    [ ~^]+\r?\n", lines[index]):
                index += 1
    if not frames:
        raise ValueError("captured traceback has no frames")
    expected = dict(oracle)
    expected["stderr_b64"] = base64.b64encode(b"".join(lines[:start] + lines[end:])).decode()
    return expected == candidate


def forbidden_header(field, value):
    """This intentional substitution has exactly three approved input fields."""
    if field not in HEADER_FIELDS:
        raise ValueError("field outside http-forbidden-input-refused")
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def refusal_response(field, pre_files, newline):
    """Candidate-arm expectation only; the Python oracle remains unmodified."""
    if field not in HEADER_FIELDS or newline not in {b"\n", b"\r\n"}:
        raise ValueError("invalid refusal contract")
    diagnostic = f"lease: HTTP_FORBIDDEN_INPUT_REFUSED: invalid {field} header".encode() + newline
    return {"exit_code": 1, "stdout_b64": "",
            "stderr_b64": base64.b64encode(diagnostic).decode(),
            "post_files_b64": dict(pre_files)}


def successful_child_clock(oracle, candidate, windows, oracle_repeat=None):
    """Return incomplete without both arms' retained invocation windows.

    Only ran.json /t is free. Every other response/file byte stays exact, and
    each decimal timestamp must be finite and inside its own inclusive window.
    Raw input dictionaries are never changed.
    """
    if (oracle_repeat is None or windows is None
            or set(windows) != {"oracle", "candidate", "oracle_repeat"}):
        return "incomplete"
    compared = []
    clocks = []
    for arm, response in (("oracle", oracle), ("candidate", candidate), ("oracle_repeat", oracle_repeat)):
        window = windows[arm]
        if (not isinstance(window, (list, tuple)) or len(window) != 2
                or any(type(value) not in {int, float} or not math.isfinite(value) for value in window)
                or window[0] > window[1]):
            return "incomplete"
        if response["exit_code"] != 0:
            return "failed"
        if "ran.json" not in response["post_files_b64"]:
            return "failed"
        raw = base64.b64decode(response["post_files_b64"]["ran.json"], validate=True)
        match = RUN_CLOCK.fullmatch(raw)
        if match is None:
            return "failed"
        stamp = float(match["clock"])
        if not math.isfinite(stamp) or not window[0] <= stamp <= window[1]:
            return "failed"
        clocks.append(match["clock"])
        copy = dict(response)
        copy["post_files_b64"] = dict(response["post_files_b64"])
        copy["post_files_b64"]["ran.json"] = raw[:match.start("clock")] + b"<clock>" + raw[match.end("clock"):]
        compared.append(copy)
    if clocks[0] == clocks[2]:
        return "incomplete"
    return "matched" if compared[0] == compared[1] == compared[2] else "failed"
