"""Named wait-mail delivery clocks; every surrounding byte remains exact."""
import base64
import copy
import math
import re
import time


POLICY = "nondeterministic-bytes-semantic"
PREFIX = b"wait-mail: the daemon rang for addressed mail at "


def delivery_projection(response, window, ledger_path):
    """Validate each clock against its own arm's captured wall-time window."""
    start, end = window
    if not all(math.isfinite(value) for value in window) or end < start:
        raise ValueError("invalid invocation wall-time window")
    first, last = math.floor(start), math.floor(end)
    stderr = base64.b64decode(response["stderr_b64"], validate=True)
    match = re.match(re.escape(PREFIX) + rb"([0-9]{2}:[0-9]{2}:[0-9]{2})(?= \()", stderr)
    if response["exit_code"] != 0 or match is None:
        raise ValueError("not a successful wait-mail delivery")
    clock = match.group(1)
    clocks = {time.strftime("%H:%M:%S", time.localtime(epoch)).encode("ascii")
              for epoch in range(first, last + 1)}
    if clock not in clocks:
        raise ValueError("delivery clock lies outside its invocation window")
    ledger = base64.b64decode(response["post_files_b64"][ledger_path], validate=True)
    epoch, separator, rest = ledger.partition(b"\t")
    if not separator or re.fullmatch(rb"0|[1-9][0-9]*", epoch) is None \
            or not first <= int(epoch) <= last:
        raise ValueError("ledger clock lies outside its invocation window")
    # This projection admits one delivery into a freshly seeded home. Existing
    # ledger content needs its own captured prefix; never erase it implicitly.
    if not rest.startswith(b"wait\t") or len(ledger.splitlines()) != 1:
        raise ValueError("expected exactly one fresh wait ledger line")
    projected = copy.deepcopy(response)
    projected["stderr_b64"] = base64.b64encode(
        stderr[:match.start(1)] + b"<local-clock>" + stderr[match.end(1):]).decode("ascii")
    projected["post_files_b64"][ledger_path] = base64.b64encode(
        b"<epoch>\t" + rest).decode("ascii")
    instance = {"policy": POLICY, "fields": ["stderr delivery HH:MM:SS", "ledger column 1"],
                "wall_window": [start, end], "raw_clock": clock.decode("ascii"),
                "raw_epoch": int(epoch)}
    return projected, instance
